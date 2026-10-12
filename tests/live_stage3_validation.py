"""3단계 판정 정책(D-1·D-2·G-1·G-3·G-4) 실사용 검증 — 고정 20문항을 Claude 평가기로 평가한다.

기본 실행은 모의 검증이다(유료 호출 없음). 실제 Claude 클라이언트 코드에 가짜 Messages 응답과
가짜 출처 원문을 넣어 20문항 흐름·강제 실패 상황·저장·비용 상한·재개를 확인한다.
실제 호출은 --live 와 --max-usd 가 모두 있어야 한다. --count-tokens 는 무료 토큰 계산 API 만 쓴다.

- 문항·사전 기대 결과: tests/fixtures/stage3_live_validation.json (실행 결과를 보고 고치지 않는다)
- 결과: results/stage3-live-validation/<시각>/ 에만 쓴다. 기존 평가 결과·이력은 읽거나 바꾸지 않는다.
- CODE 설정은 웹 서비스와 같다(AIONA 특화 일시 중지). 의미 평가는 운영 클라이언트·설정 그대로다.
- 대화 문항은 평가 대상 턴만 의미 평가를 호출한다. 앞 턴은 CODE 평가와 대화 이력으로만 쓰인다.
- 호출마다 결과를 calls/ 에 즉시 저장한다. --resume 으로 다시 실행하면 저장된 호출은 다시 하지 않는다.
- 추정 비용이 --max-usd 에, 새 호출 수가 --max-calls 에 닿으면 새 호출을 시작하지 않는다.
  진행 중인 호출은 끊지 않고 끝까지 받는다(이미 과금된 결과를 버리지 않기 위해).
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import statistics
import subprocess
import sys
import threading
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from code_eval.anthropic_semantic_client import AnthropicSemanticClient, AnthropicSemanticConfig, FINAL_TOOL
from code_eval.combined import _code_fails, combined_summary
from code_eval.common_adjudication import resolved_batch
from code_eval.config import load_aiona_config
from code_eval.cost_reporting import estimate
from code_eval.engine import evaluate_batch
from code_eval.jev_client import JevCall, JevStatus, resolve_jev_client
from code_eval.jev_eval import CANCEL_REASON, evaluate_jev_batch

FIXTURE = ROOT / "tests" / "fixtures" / "stage3_live_validation.json"
OUTPUT = ROOT / "results" / "stage3-live-validation"
LIVE_MODEL = "claude-opus-5-5"
NOT_TARGET = "검증 대상이 아닌 앞 턴 — 의미 평가 호출 생략 (CODE·대화 이력에만 사용)"
CAP_REASON = "검증 비용·호출 상한에 도달해 호출하지 않았습니다."
#: 단가를 확인하지 못한 호출은 상한 계산에서 이 금액을 쓴 것으로 본다 (보수적으로)
UNPRICED_RESERVE = Decimal("0.50")
GROUPS = ("D-1", "D-2", "G-4", "G-3", "general")
REEVALUATED = ("J1", "J2", "J3", "J4")


# --- 문항 ------------------------------------------------------------------------------

def load_fixture(path=FIXTURE):
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = fixture["cases"]
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("문항 ID 중복")
    for case in cases:
        expected = case["expected"]
        if expected["verdict"] not in ("PASS", "FAIL", "INCOMPLETE") or not expected.get("key"):
            raise ValueError(f"{case['id']}: 기대 판정·핵심 기대 누락")
        if case.get("turns"):
            turns = [t["turn"] for t in case["turns"]]
            if turns != list(range(1, len(turns) + 1)) or case["target_turn"] != turns[-1]:
                raise ValueError(f"{case['id']}: 대화 턴은 1부터 빠짐없이, 평가 대상은 마지막 턴")
            if any(not t["question"].strip() or not isinstance(t["answer"], str) for t in case["turns"]):
                raise ValueError(f"{case['id']}: 질문·답변 누락")
        elif not case["question"].strip() or not isinstance(case["answer"], str):
            raise ValueError(f"{case['id']}: 질문·답변 누락")
    return fixture


def target_id(case):
    return f"{case['id']}-T{case['target_turn']:02d}" if case.get("turns") else case["id"]


def target_text(case):
    if case.get("turns"):
        turn = case["turns"][case["target_turn"] - 1]
        return turn["question"], turn["answer"]
    return case["question"], case["answer"]


def build_inputs(cases):
    """엔진 입력(대화는 모든 턴)과 평가 대상 ID. 원문은 복사만 하고 바꾸지 않는다."""
    payloads = []
    for case in cases:
        if case.get("turns"):
            payloads += [{"case_id": f"{case['id']}-T{t['turn']:02d}", "conversation_id": case["id"], "turn": t["turn"],
                          "question": t["question"], "answer": t["answer"]} for t in case["turns"]]
        else:
            payloads.append({"case_id": case["id"], "question": case["question"], "answer": case["answer"]})
    return payloads, {target_id(case): case for case in cases}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


# --- 비용 ------------------------------------------------------------------------------

def price(call, model):
    """요청별 사용량으로 USD 추정. 확인하지 못하면 (None, 이유)."""
    parts = (call.accounting or {}).get("cost_parts")
    if not isinstance(parts, list) or not parts:
        return None, "사용량 없음", {}
    total, tokens = Decimal(0), Counter()
    for part in parts:
        usage = part.get("usage") or {}
        name = part.get("model") or model
        name = model if isinstance(name, str) and name.startswith(model) else name
        try:
            usd, _, _ = estimate("anthropic_api", name, usage, web_search_calls=part.get("web_search_calls"),
                                 service_tier=part.get("service_tier"), inference_geo=part.get("inference_geo"))
        except ValueError as exc:
            return None, str(exc), {}
        total += usd
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            tokens[key] += usage.get(key) or 0
        tokens["web_search_requests"] += part.get("web_search_calls") or 0
        tokens["requests"] += 1
    return total, None, dict(tokens)


# --- 호출 보호(상한·저장·재개) ---------------------------------------------------------------

class GuardedClient:
    """평가 대상 턴만 실제 클라이언트로 보내고, 호출마다 즉시 저장·상한 확인한다."""
    supports_cancellation = True

    def __init__(self, client, config, folder, index, targets, *, max_usd, max_calls, stop):
        self.client, self.config = client, config
        self.calls_dir = Path(folder) / "calls"
        self.calls_dir.mkdir(parents=True, exist_ok=True)
        self.index, self.targets = index, targets
        self.max_usd, self.max_calls, self.stop = Decimal(str(max_usd)), max_calls, stop
        self.lock = threading.Lock()
        self.spent = Decimal(0)
        self.new_calls = 0
        self.rows = []
        self.seen = Counter()
        self.model = getattr(config, "model", LIVE_MODEL)

    def system_one(self, state, questions, *, cancel_event=None):
        case_id = self.index.get((state.get("question"), state.get("answer")))
        if case_id not in self.targets:
            return JevCall(JevStatus.SKIPPED, error=NOT_TARGET)
        key = digest({"state": state, "questions": questions})
        path = self.calls_dir / f"{key}.json"
        with self.lock:
            kind = "initial" if not self.seen[case_id] else "reevaluation"
            self.seen[case_id] += 1
        row = {"case_id": case_id, "kind": kind, "criteria": list(questions), "key": key}
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            call = JevCall(**saved["call"])
            with self.lock:
                self.rows.append({**saved["meta"], "replayed": True})
            return call
        with self.lock:
            if self.stop.is_set() or self.spent >= self.max_usd or self.new_calls >= self.max_calls:
                self.stop.set()
                self.rows.append({**row, "status": "NOT_CALLED", "reason": CAP_REASON, "replayed": False})
                return JevCall(JevStatus.SKIPPED, error=CAP_REASON)
            self.new_calls += 1
        started = datetime.now(timezone.utc).isoformat()
        # 진행 중 호출은 상한으로 끊지 않는다 — 실제 클라이언트에는 중단 신호를 넘기지 않는다.
        call = self.client.system_one(state, questions)
        usd, unpriced, tokens = price(call, self.model)
        verification = (call.body or {}).get("verification") or {}
        meta = {**row, "status": call.status, "error": call.error, "started_at": started,
                "latency_ms": call.latency_ms, "attempts": call.attempts, "http_status": call.http_status,
                "usd": None if usd is None else str(usd), "unpriced_reason": unpriced, "tokens": tokens,
                "timings_ms": verification.get("timings_ms"), "search_calls": verification.get("search_calls"),
                "tool_errors": verification.get("tool_errors"), "stop_reason": verification.get("provider_stop_reason")}
        write_json(path, {"meta": meta, "call": dataclasses.asdict(call)})
        with self.lock:
            self.spent += usd if usd is not None else UNPRICED_RESERVE
            self.rows.append({**meta, "replayed": False})
            if self.spent >= self.max_usd or self.new_calls >= self.max_calls:
                self.stop.set()
        return call


def run_pipeline(cases, client, config, folder, *, max_usd, max_calls, workers=4):
    folder = Path(folder)
    payloads, targets = build_inputs(cases)
    index = {(p["question"], p["answer"]): p["case_id"] for p in payloads}
    if len(index) != len(payloads):
        raise ValueError("질문·답변 쌍이 중복되어 호출 대상을 구분할 수 없습니다.")
    cfg = load_aiona_config()
    cfg.enabled = False                      # 웹 서비스와 같은 CODE 설정 (AIONA 특화 일시 중지)
    cfg.raw_hash = "paused-" + cfg.raw_hash
    stop = threading.Event()
    guard = GuardedClient(client, config, folder, index, targets, max_usd=max_usd, max_calls=max_calls, stop=stop)
    started = time.perf_counter()
    batch = evaluate_batch(payloads, cfg)
    code_ms = (time.perf_counter() - started) * 1000

    def saved(i, record):
        write_json(folder / "records" / f"{batch['cases'][i]['case_id']}.json", record)
    semantic_started = time.perf_counter()
    batch["jev_eval"] = evaluate_jev_batch(batch, guard, config, None, workers=workers, on_record=saved, cancel_event=stop)
    semantic_ms = (time.perf_counter() - semantic_started) * 1000
    batch["combined"] = combined_summary(batch)
    write_json(folder / "batch.json", batch)
    write_json(folder / "calls.json", sorted(guard.rows, key=lambda r: (r["case_id"], r.get("started_at") or "")))
    return batch, guard, {"code_ms": round(code_ms, 1), "semantic_wall_ms": round(semantic_ms, 1),
                          "workers": workers, "stopped_by_cap": stop.is_set() and guard.new_calls > 0 and any(
                              r.get("status") == "NOT_CALLED" for r in guard.rows)}


# --- 결과 분석 -------------------------------------------------------------------------

def question_verdict(case, record):
    """combined_summary 와 같은 질문 판정. 검사 실행 오류(case["errors"])가 있으면 PASS 로 확정하지 않는다."""
    decisions = (record or {}).get("decisions") or {}
    if _code_fails(case) or any(d.get("decision") == "FAIL" for d in decisions.values()):
        return "FAIL"
    complete = bool(record and record.get("status") == "SUCCESS" and record.get("criteria") and all(
        decisions.get(cid, {}).get("decision") in ("PASS", "FAIL") for cid in record["criteria"]))
    common = (case.get("summary") or {}).get("common") or {}
    if (complete and not common.get("unjudged") and not case.get("errors")
            and common.get("decided", common.get("pass", 0))):
        return "PASS"
    return "INCOMPLETE"


def _row_matches(row, spec):
    inherited = (row.get("details") or {}).get("inherited_from") or {}
    return (row["check_id"].startswith(spec["check_id"])
            and ("verdict" not in spec or row.get("verdict") == spec["verdict"])
            and ("inherited_from" not in spec or inherited.get("turn") == spec["inherited_from"])
            and (not spec.get("inherited") or row.get("source") == "history_instruction"))


def _status(decision):
    return decision.get("decision") or decision.get("status")


def observe(case, record, calls):
    """한 문항의 실제 결과 — 판정·근거·재평가·J5·출처·CODE·시간·비용."""
    record = record or {}
    decisions = record.get("decisions") or {}
    semantic = {cid: _status(d) for cid, d in decisions.items()}
    reasons = {cid: d.get("reason") for cid, d in decisions.items()}
    first_fail, reevaluated = {}, {}
    for cid in REEVALUATED:
        d = decisions.get(cid)
        if not isinstance(d, dict):
            continue
        if d.get("reevaluation"):
            first = d["reevaluation"]["first"]
            first_fail[cid] = {"valid": False, "problems": first["fail_evidence"]["problems"], "reason": first.get("reason")}
            reevaluated[cid] = {"retry": d["reevaluation"].get("retry"), "final": d["reevaluation"]["final"],
                                "status": record.get("reevaluation", {}).get("status")}
        elif d.get("decision") == "FAIL" and isinstance(d.get("fail_evidence"), dict):
            first_fail[cid] = {"valid": d["fail_evidence"]["valid"], "problems": d["fail_evidence"]["problems"],
                               "quotes": d["fail_evidence"].get("matched_quotes")}
    j5 = decisions.get("J5") if isinstance(decisions.get("J5"), dict) else None
    outcome = (j5 or {}).get("j5_outcome") or {}
    claims = (((j5 or {}).get("fact_check") or {}).get("claims") or [])
    citations = [{k: c.get(k) for k in ("url", "model_verdict", "verdict", "server_status", "quote_verified", "note")}
                 for c in (j5 or {}).get("citation_checks") or [] if isinstance(c, dict)]
    rows = case.get("results") or []
    verification = record.get("verification") or {}
    reeval = record.get("reevaluation") or {}
    costs = [Decimal(c["usd"]) for c in calls if c.get("usd") is not None]
    return {
        "verdict": question_verdict(case, record),
        "semantic_status": record.get("status"), "error": record.get("error"),
        "criteria": record.get("criteria") or [], "semantic": semantic, "reasons": reasons,
        "reason_codes": {cid: d.get("reason_code") for cid, d in decisions.items() if d.get("reason_code")},
        "first_fail_evidence": first_fail, "reevaluated": reevaluated,
        "reevaluation": {k: reeval.get(k) for k in ("criteria", "status", "latency_ms", "error")} if reeval else None,
        "j5": None if j5 is None else {
            "status": _status(j5), "outcome": outcome.get("code"), "label": outcome.get("label"),
            "factual_error_confirmed": outcome.get("factual_error_confirmed"), "search": outcome.get("search"),
            "citation_summary": outcome.get("citations"), "citation_mismatch": bool(j5.get("citation_mismatch")),
            "basis": ((j5.get("fact_check") or {}).get("basis")),
            "claims": [{"claim": c.get("claim"), "verdict": c.get("verdict"), "reason": c.get("reason"),
                        "sources": len(c.get("sources") or []),
                        "quote_verified": sum(s.get("quote_verified") is True for s in c.get("sources") or [])}
                       for c in claims if isinstance(c, dict)]},
        "answer_citations": [{k: c.get(k) for k in ("url", "checked", "claims")}
                             for c in (record.get("answer_citations") or {}).get("citations") or []],
        "citation_checks": citations,
        "j6_checks": (decisions.get("J6") or {}).get("context_checks"),
        "code": {"fail": [{"check_id": r["check_id"], "title": r.get("title"), "source": r.get("source")}
                          for r in _code_fails(case)],
                 "inherited": [{"check_id": r["check_id"], "verdict": r.get("verdict"),
                                "turn": ((r.get("details") or {}).get("inherited_from") or {}).get("turn")}
                               for r in rows if r.get("source") == "history_instruction"],
                 "inherited_conditions": (case.get("meta") or {}).get("inherited_conditions"),
                 "summary": (case.get("summary") or {}).get("common")},
        "verification": {k: verification.get(k) for k in ("search_mode", "search_performed", "search_calls", "tool_errors",
                                                          "timings_ms", "provider_stop_reason", "output_transport")},
        "calls": len([c for c in calls if c.get("status") not in ("NOT_CALLED",)]),
        "latency_ms": (record.get("latency_ms") or 0) + (reeval.get("latency_ms") or 0) if record.get("latency_ms") else None,
        "usd": str(sum(costs)) if costs else None,
        "unpriced_calls": sum(1 for c in calls if c.get("usd") is None and c.get("status") != "NOT_CALLED"),
    }


def compare(case, actual, rows):
    """사전 기대 결과와 비교. key 는 검증 목적의 핵심, detail 은 기준별 허용 결과·CODE·출처 전체."""
    expected = case["expected"]
    checks = []

    def add(kind, name, ok, want, got):
        checks.append({"kind": kind, "name": name, "ok": bool(ok), "expected": want, "actual": got})
    j5 = actual["j5"] or {}
    cited = {c["url"]: c["verdict"] for c in actual["citation_checks"]}

    def key_checks(spec, kind):
        if "any_fail" in spec:
            got = {cid: actual["semantic"].get(cid) for cid in spec["any_fail"]}
            add(kind, "FAIL 기준 하나 이상: " + "/".join(spec["any_fail"]), "FAIL" in got.values(), "FAIL", got)
        for cid, allowed in (spec.get("semantic") or {}).items():
            add(kind, cid, actual["semantic"].get(cid) in allowed, allowed, actual["semantic"].get(cid))
        if "j5_outcome" in spec:
            add(kind, "J5 상세 사유", j5.get("outcome") in spec["j5_outcome"], spec["j5_outcome"], j5.get("outcome"))
        for url, allowed in (spec.get("citations") or {}).items():
            add(kind, "출처 " + url, cited.get(url) in allowed, allowed, cited.get(url))
        if "citation_mismatch" in spec:
            add(kind, "출처 불일치", j5.get("citation_mismatch", False) == spec["citation_mismatch"],
                spec["citation_mismatch"], j5.get("citation_mismatch"))
        for item in spec.get("code_rows") or spec.get("rows") or []:
            add(kind, "CODE " + item["check_id"], any(_row_matches(r, item) for r in rows), item,
                [r["check_id"] + ":" + str(r.get("verdict")) for r in rows if r["check_id"].startswith(item["check_id"])])
        for item in spec.get("code_absent") or spec.get("absent") or []:
            add(kind, "CODE 없음 " + item["check_id"], not any(_row_matches(r, item) for r in rows), item,
                [r["check_id"] + ":" + str(r.get("verdict")) for r in rows if _row_matches(r, item)])
    add("verdict", "질문 판정", actual["verdict"] == expected["verdict"], expected["verdict"], actual["verdict"])
    key_checks(expected["key"], "key")
    detail = {k: expected[k] for k in ("semantic", "j5_outcome", "citations", "citation_mismatch") if k in expected}
    key_checks(detail, "detail")
    code = expected.get("code") or {}
    if "fail_expected" in code:
        add("detail", "CODE FAIL 존재", bool(actual["code"]["fail"]) == code["fail_expected"], code["fail_expected"],
            [r["check_id"] for r in actual["code"]["fail"]])
    key_checks({k: code[k] for k in ("rows", "absent") if k in code}, "detail")
    if "factual_error_confirmed" in expected:
        add("detail", "사실 오류 확정", j5.get("factual_error_confirmed") == expected["factual_error_confirmed"],
            expected["factual_error_confirmed"], j5.get("factual_error_confirmed"))
    allowed = expected.get("semantic") or {}
    unexpected = [cid for cid, value in actual["semantic"].items()
                  if value == "FAIL" and "FAIL" not in allowed.get(cid, ())]
    add("detail", "예상 밖 FAIL 없음", not unexpected, [], unexpected)
    return {"verdict_match": checks[0]["ok"],
            "key_match": all(c["ok"] for c in checks if c["kind"] in ("verdict", "key")),
            "detail_match": all(c["ok"] for c in checks), "unexpected_fail": unexpected,
            "checks": checks}


def analyze(fixture_cases, batch, guard, timing, *, mode):
    resolved = resolved_batch(batch)
    records = batch["jev_eval"]["records"]
    by_id = {c["case_id"]: (i, c) for i, c in enumerate(resolved["cases"])}
    items = []
    for case in fixture_cases:
        i, code_case = by_id[target_id(case)]
        calls = [r for r in guard.rows if r["case_id"] == target_id(case)]
        actual = observe(code_case, records[i], calls)
        items.append({"id": case["id"], "group": case["group"], "purpose": case["purpose"], "target": target_id(case),
                      "expected": case["expected"], "actual": actual,
                      "comparison": compare(case, actual, code_case.get("results") or [])})
    calls = [r for r in guard.rows if r.get("status") != "NOT_CALLED"]
    priced = [Decimal(r["usd"]) for r in calls if r.get("usd") is not None]
    latencies = [it["actual"]["latency_ms"] for it in items if it["actual"]["latency_ms"]]
    verdicts = Counter(it["actual"]["verdict"] for it in items)
    criteria = {}
    for it in items:
        for cid, value in it["actual"]["semantic"].items():
            criteria.setdefault(cid, Counter())[value] += 1
    initial_fail = [(it["id"], cid, ev) for it in items for cid, ev in it["actual"]["first_fail_evidence"].items()]
    reevaluated = [(it["id"], cid, info) for it in items for cid, info in it["actual"]["reevaluated"].items()]
    reeval_calls = [r for r in calls if r["kind"] == "reevaluation"]
    j5 = [it for it in items if it["actual"]["j5"]]
    cited = [it for it in items if it["actual"]["answer_citations"]]
    checks = [c for it in items for c in it["actual"]["citation_checks"]]
    groups = {g: {"cases": 0, "verdict_match": 0, "key_match": 0, "detail_match": 0} for g in GROUPS}
    for it in items:
        part = groups[it["group"]]
        part["cases"] += 1
        for key in ("verdict_match", "key_match", "detail_match"):
            part[key] += int(it["comparison"][key])
    summary = {
        "mode": mode, "cases": len(items),
        "completed": sum(1 for it in items if it["actual"]["semantic_status"] == "SUCCESS"),
        "not_run": [it["id"] for it in items if it["actual"]["semantic_status"] != "SUCCESS"],
        "verdicts": dict(verdicts), "incomplete_rate": round(verdicts["INCOMPLETE"] / len(items), 4) if items else None,
        "criteria": {cid: dict(v) for cid, v in sorted(criteria.items())},
        "groups": groups,
        "matches": {k: sum(it["comparison"][k] for it in items) for k in ("verdict_match", "key_match", "detail_match")},
        "reevaluation": {
            "initial_calls": sum(1 for r in calls if r["kind"] == "initial"),
            "initial_j1_j4_fail": len(initial_fail),
            "initial_fail_without_evidence": sum(1 for _, _, ev in initial_fail if not ev["valid"]),
            "cases": len({case_id for case_id, _, _ in reevaluated}), "criteria": len(reevaluated),
            "rate_by_case": round(len({c for c, _, _ in reevaluated}) / len(items), 4) if items else None,
            "rate_by_fail": round(len(reevaluated) / len(initial_fail), 4) if initial_fail else None,
            "final": dict(Counter(info["final"] for _, _, info in reevaluated)),
            "calls": len(reeval_calls),
            "usd": str(sum((Decimal(r["usd"]) for r in reeval_calls if r.get("usd")), Decimal(0))),
            "latency_ms": sum(r.get("latency_ms") or 0 for r in reeval_calls)},
        "j5": {"evaluated": len(j5), "outcomes": dict(Counter(it["actual"]["j5"]["outcome"] for it in j5)),
               "factual_error_confirmed": sum(1 for it in j5 if it["actual"]["j5"]["factual_error_confirmed"]),
               "search_calls": sum(it["actual"]["verification"].get("search_calls") or 0 for it in items),
               "tool_errors": sum(len(it["actual"]["verification"].get("tool_errors") or []) for it in items)},
        "citations": {"answers_with_citations": len(cited),
                      "extracted": sum(len(it["actual"]["answer_citations"]) for it in cited),
                      "judged": len(checks),
                      "server_fetched_ok": sum(1 for c in checks if c.get("server_status") == "ok"),
                      "quote_verified": sum(1 for c in checks if c.get("quote_verified")),
                      "verdicts": dict(Counter(c["verdict"] for c in checks)),
                      "model_verdicts": dict(Counter(c["model_verdict"] for c in checks))},
        "performance": {
            **timing, "calls": len(calls), "new_calls": sum(1 for r in calls if not r.get("replayed")),
            "replayed_calls": sum(1 for r in calls if r.get("replayed")),
            "not_called": sum(1 for r in guard.rows if r.get("status") == "NOT_CALLED"),
            "latency_mean_ms": round(statistics.mean(latencies), 1) if latencies else None,
            "latency_median_ms": round(statistics.median(latencies), 1) if latencies else None,
            "latency_max_ms": max(latencies) if latencies else None,
            "slowest": max(items, key=lambda it: it["actual"]["latency_ms"] or 0)["id"] if latencies else None,
            "usd_total": str(sum(priced, Decimal(0))), "unpriced_calls": len(calls) - len(priced),
            "usd_per_case": str((sum(priced, Decimal(0)) / len(items)).quantize(Decimal("0.0001"))) if items else None,
            "input_tokens": sum((r.get("tokens") or {}).get("input_tokens", 0) for r in calls),
            "cache_read_tokens": sum((r.get("tokens") or {}).get("cache_read_input_tokens", 0) for r in calls),
            "cache_write_tokens": sum((r.get("tokens") or {}).get("cache_creation_input_tokens", 0) for r in calls),
            "output_tokens": sum((r.get("tokens") or {}).get("output_tokens", 0) for r in calls),
            "web_search_requests": sum((r.get("tokens") or {}).get("web_search_requests", 0) for r in calls),
            "api_errors": [r["case_id"] + ":" + str(r["status"]) for r in calls if r.get("status") != "OK"]},
    }
    return {"summary": summary, "items": items}


def report_markdown(analysis):
    s = analysis["summary"]
    lines = [f"# 3단계 실사용 검증 결과 ({s['mode']})", "",
             f"- 문항 {s['cases']} · 의미 평가 완료 {s['completed']} · 판정 {s['verdicts']} · 미완료율 {s['incomplete_rate']}",
             f"- 기대 일치: 질문 판정 {s['matches']['verdict_match']}/{s['cases']} · 핵심 {s['matches']['key_match']}/{s['cases']}"
             f" · 세부 {s['matches']['detail_match']}/{s['cases']}",
             f"- 호출 {s['performance']['calls']}회(새 호출 {s['performance']['new_calls']}) · 추정 ${s['performance']['usd_total']}"
             f" · 평균 {s['performance']['latency_mean_ms']}ms · 중앙값 {s['performance']['latency_median_ms']}ms",
             f"- 토큰: 입력 {s['performance']['input_tokens']} · 캐시 읽기 {s['performance']['cache_read_tokens']}"
             f" · 캐시 쓰기 {s['performance']['cache_write_tokens']} · 출력 {s['performance']['output_tokens']}"
             f" · 검색 {s['performance']['web_search_requests']}회", "",
             "| 문항 | 기대 | 실제 | 핵심 | 기준별 결과 | J5 사유 | 재평가 | 출처 판정 | CODE FAIL | 시간(s) | 비용($) |",
             "|---|---|---|---|---|---|---|---|---|---:|---:|"]
    for it in analysis["items"]:
        a = it["actual"]
        sem = " ".join(f"{k}:{v}" for k, v in sorted(a["semantic"].items()))
        cites = " ".join(f"{c['verdict']}" for c in a["citation_checks"]) or "-"
        reeval = ", ".join(f"{cid}→{info['final']}" for cid, info in a["reevaluated"].items()) or "-"
        code = ", ".join(r["check_id"] for r in a["code"]["fail"]) or "-"
        lines.append(f"| {it['id']} | {it['expected']['verdict']} | {a['verdict']} | {'일치' if it['comparison']['key_match'] else '불일치'}"
                     f" | {sem} | {(a['j5'] or {}).get('outcome') or '-'} | {reeval} | {cites} | {code}"
                     f" | {round((a['latency_ms'] or 0) / 1000, 1)} | {a['usd'] or '-'} |")
    misses = [(it["id"], c) for it in analysis["items"] for c in it["comparison"]["checks"] if not c["ok"]]
    if misses:
        lines += ["", "## 기대와 다른 항목", ""]
        lines += [f"- {cid} · {c['kind']} · {c['name']}: 기대 {c['expected']} / 실제 {c['actual']}" for cid, c in misses]
    return "\n".join(lines) + "\n"


# --- 모의 평가기 ------------------------------------------------------------------------

def _first_sentence(text):
    match = re.search(r".+?[.!?](?=\s|$)", text, re.S)
    return (match.group(0) if match else text).strip()


def _evidence(case):
    for ref in case.get("references") or []:
        if ref.get("quote"):
            return ref["url"], ref["quote"]
    return f"https://evidence.mock/{case['id'].lower()}", f"mock evidence for {case['id']}"


MOCK_PAGES = {
    "https://docs.python.org/3/howto/sorting.html": "Sorts are guaranteed to be stable. That means that when multiple "
                                                     "records have the same key, their original order is preserved.",
    "https://docs.python.org/3/library/os.path.html": "This module implements some useful functions on pathnames.",
    "https://peps.python.org/pep-0008/": "Use 4 spaces per indentation level.",
}


def prompt_text(body):
    """요청의 사용자 메시지 원문 — 캐시 표시로 나뉜 텍스트 블록은 이어 붙인다."""
    content = body["messages"][0]["content"]
    return content if isinstance(content, str) else "".join(block["text"] for block in content)


class MockJudge:
    """기대 결과대로 판단하는 Claude Messages 대역. behaviors 로 근거 누락·검색 오류 등을 강제한다.

    behaviors[case_id] = {"no_evidence": [기준], "retry": "valid"|"pass"|"none"|"api_error",
                          "search": "error"|"limit"|"fetch_only", "invalid": True, "citation_model": {url: verdict}}
    """

    def __init__(self, cases, behaviors=None, *, usage=(6000, 1500)):
        self.cases = {target_text(c)[1]: c for c in cases}
        self.behaviors = behaviors or {}
        self.usage = usage
        self.calls = Counter()
        self.histories = {}
        self.lock = threading.Lock()

    def pages(self):
        pages = dict(MOCK_PAGES)
        for case in self.cases.values():
            url, quote = _evidence(case)
            pages[url] = (pages.get(url, "") + " " + quote + " (모의 원문)").strip()
        return pages

    def fetcher(self, url):
        text = self.pages().get(url)
        if text is None:
            return {"status": "http_error"}
        return {"status": "ok", "text": text, "final_url": url, "content_type": "text/html",
                "checked_at": "2026-10-12T00:00:00Z"}

    def handler(self, request):
        body = json.loads(request.content)
        state = json.loads(prompt_text(body).split("## 평가 대상 (데이터)\n", 1)[1])
        ids = body["tools"][-1]["input_schema"]["required"]
        searching = any(t.get("name") == "web_search" for t in body["tools"])
        case = self.cases[state["answer"]]
        behavior = self.behaviors.get(case["id"], {})
        with self.lock:
            self.calls[case["id"]] += 1
            attempt = self.calls[case["id"]]
            self.histories[case["id"]] = [(h["turn"], h["question"], h["answer"]) for h in state.get("history") or []]
        if attempt > 1 and behavior.get("retry") == "api_error":
            return httpx.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "mock"}})
        blocks, searches = [], 0
        values = {}
        for cid in ids:
            entry, extra, count = self.entry(cid, case, state, attempt, behavior, searching)
            values[cid] = entry
            blocks += extra
            searches += count
        if behavior.get("invalid"):
            values.pop(ids[0])
        fetches = sum(1 for b in blocks if b.get("name") == "web_fetch")
        usage = {"input_tokens": self.usage[0] + 3000 * fetches, "output_tokens": self.usage[1],
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                 "server_tool_use": {"web_search_requests": searches}, "service_tier": "standard", "inference_geo": "global"}
        return httpx.Response(200, json={"model": LIVE_MODEL, "stop_reason": "tool_use", "usage": usage, "content": [
            *blocks, {"type": "tool_use", "id": "final", "name": FINAL_TOOL, "input": values}]})

    def entry(self, cid, case, state, attempt, behavior, searching):
        answer = state["answer"]
        if not cid.startswith("J"):                      # CODE 공통 미판정 보완 기준
            return {"choice": "PASS", "confidence": .8, "reason": f"모의 보완: '{answer[:8]}' 확인"}, [], 0
        allowed = case["expected"]["semantic"].get(cid, ["PASS"])
        choice = allowed[0] if allowed[0] in ("PASS", "FAIL", "NOT_APPLICABLE") else "PASS"
        if cid == "J5":
            return self.j5(case, state, choice, behavior, searching)
        if cid == "J6":
            history = state.get("history") or []
            first = history[0]
            return {"choice": "PASS", "confidence": .8, "reason": "앞 지시를 유지했습니다.", "context_checks": [
                {"turn": first["turn"], "instruction_quote": first["question"][:10], "answer_quote": answer[:8],
                 "decision": "PASS", "reason": "모의 확인"}]}, [], 0
        if cid == "J1" and choice == "NOT_APPLICABLE":
            choice = "PASS"
        if choice == "FAIL":
            missing = cid in behavior.get("no_evidence", ())
            retry = behavior.get("retry", "none")
            if missing and (attempt == 1 or retry == "none"):
                return {"choice": "FAIL", "confidence": .7, "reason": "요청을 충족하지 않습니다."}, [], 0
            if missing and retry == "pass":
                return {"choice": "PASS", "confidence": .7, "reason": "다시 보니 요청을 충족합니다."}, [], 0
            return {"choice": "FAIL", "confidence": .8, "reason": f"답변의 '{answer[:10]}' 부분이 요청을 충족하지 않습니다."}, [], 0
        return {"choice": choice, "confidence": .8, "reason": "요청을 충족합니다."}, [], 0

    def j5(self, case, state, choice, behavior, searching):
        claim = _first_sentence(state["answer"])
        outcomes = case["expected"].get("j5_outcome") or []
        if choice == "NOT_APPLICABLE":
            return {"choice": "NOT_APPLICABLE", "confidence": .9, "reason": "검증할 사실 주장이 없습니다.",
                    "fact_check": {"basis": "none", "claims": []}}, [], 0
        url, quote = _evidence(case)
        cited = [c["url"] for c in state.get("answer_citations") or []]
        blocks, searches = [], 0
        mode = behavior.get("search")
        unverified = (not searching or mode in ("error", "limit")
                      or set(outcomes) & {"insufficient_evidence", "search_limit_reached"})
        if searching and mode == "error":
            blocks += [{"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "mock"}},
                       {"type": "web_search_tool_result", "tool_use_id": "s1",
                        "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"}}]
        elif searching and mode == "fetch_only":       # 검색 없이 답변이 든 URL 원문만 연다
            for n, u in enumerate(dict.fromkeys([url, *cited])):
                blocks += [{"type": "server_tool_use", "id": f"f{n}", "name": "web_fetch", "input": {"url": u}},
                           {"type": "web_fetch_tool_result", "tool_use_id": f"f{n}", "content": {
                               "type": "web_fetch_result", "url": u,
                               "content": {"type": "document", "source": {"type": "text", "data": "mock"}}}}]
        elif searching:
            rounds = 4 if mode == "limit" else 1
            for n in range(rounds):
                blocks += [{"type": "server_tool_use", "id": f"s{n}", "name": "web_search", "input": {"query": "mock"}},
                           {"type": "web_search_tool_result", "tool_use_id": f"s{n}", "content": [
                               {"type": "web_search_result", "url": u, "title": "mock", "encrypted_content": "x"}
                               for u in [url, *cited]]}]
            searches = rounds
            for n, u in enumerate([url, *cited]):
                blocks += [{"type": "server_tool_use", "id": f"f{n}", "name": "web_fetch", "input": {"url": u}},
                           {"type": "web_fetch_tool_result", "tool_use_id": f"f{n}", "content": {
                               "type": "web_fetch_result", "url": u,
                               "content": {"type": "document", "source": {"type": "text", "data": "mock"}}}}]
        if unverified:
            entry = {"choice": "UNVERIFIED", "confidence": .5, "reason": "근거를 확인하지 못했습니다.",
                     "fact_check": {"basis": "web" if searching else "none", "claims": [
                         {"claim": claim, "verdict": "UNVERIFIED", "reason": "근거 없음", "sources": [],
                          "scope": "external_fact", "scope_quote": ""}]}}
        else:
            entry = {"choice": choice, "confidence": .9, "reason": "출처와 대조했습니다.",
                     "fact_check": {"basis": "web", "claims": [
                         {"claim": claim, "verdict": "FAIL" if "contradicted" in outcomes else "PASS",
                          "reason": "모의 근거", "scope": "external_fact", "scope_quote": "",
                          "sources": [{"url": url, "title": "mock", "quote": quote, "authority": "primary",
                                       "authority_reason": "공식 문서", "temporal_relevance": "변하지 않는 정의",
                                       "origin": "web", "turn": None}]}]}}
            if "source_mismatch" in outcomes:
                entry["choice"] = "PASS"            # 주장은 참 — 출처 불일치만 서버가 FAIL 로 바꾼다
        if searching and state.get("answer_citations"):
            wanted = case["expected"].get("citations") or {}
            model = behavior.get("citation_model") or {}
            entry["citation_checks"] = []
            for citation in state["answer_citations"]:
                verdict = model.get(citation["url"]) or (wanted.get(citation["url"]) or ["supported"])[0]
                verdict = "inaccessible" if verdict in ("unverified", "unchecked") else verdict
                page = MOCK_PAGES.get(citation["url"], "")
                entry["citation_checks"].append({
                    "url": citation["url"], "claim": (citation.get("claims") or [claim])[0], "verdict": verdict,
                    "quote": page if verdict in ("supported", "contradicted") else "", "reason": "모의 출처 판단"})
        return entry, blocks, searches


def mock_client(cases, behaviors=None, *, web_search="live", fetcher=None):
    judge = MockJudge(cases, behaviors)
    config = AnthropicSemanticConfig(model=LIVE_MODEL, reasoning_effort="high", web_search=web_search)
    client = AnthropicSemanticClient(config, "mock-key-not-used", transport=httpx.MockTransport(judge.handler),
                                     source_fetcher=fetcher or judge.fetcher)
    return client, config, judge


def _verdicts(analysis):
    return {it["id"]: it["actual"] for it in analysis["items"]}


#: 강제 실패 재현 — (이름, 문항, behaviors, 검색 설정, 출처 수집기 실패, 확인할 결과)
SCENARIOS = (
    ("근거 없는 FAIL → 재평가에서 근거 있는 FAIL", ["D1-03"], {"D1-03": {"no_evidence": ["J4"], "retry": "valid"}}, "live", False,
     {"D1-03": {"verdict": "FAIL", "J4": "FAIL", "reevaluated": {"J4": "FAIL"}}}),
    ("근거 없는 FAIL → 재평가에서 PASS", ["D1-03"], {"D1-03": {"no_evidence": ["J4"], "retry": "pass"}}, "live", False,
     {"D1-03": {"verdict": "PASS", "J4": "PASS", "reevaluated": {"J4": "PASS"}}}),
    ("근거 없는 FAIL → 재평가에서도 근거 없음", ["D1-03"], {"D1-03": {"no_evidence": ["J4"], "retry": "none"}}, "live", False,
     {"D1-03": {"verdict": "INCOMPLETE", "J4": "INCOMPLETE", "reevaluated": {"J4": "INCOMPLETE"}}}),
    ("근거 없는 FAIL 하나만 재평가, 다른 근거 있는 FAIL 유지", ["D1-01"], {"D1-01": {"no_evidence": ["J4"], "retry": "none"}},
     "live", False, {"D1-01": {"verdict": "FAIL", "J1": "FAIL", "J4": "INCOMPLETE", "reevaluated": {"J4": "INCOMPLETE"}}}),
    ("재평가 API 오류", ["D1-03"], {"D1-03": {"no_evidence": ["J4"], "retry": "api_error"}}, "live", False,
     {"D1-03": {"verdict": "INCOMPLETE", "J4": "INCOMPLETE", "reevaluated": {"J4": "INCOMPLETE"}}}),
    ("검색 불가(검색 꺼짐)", ["D2-04"], {}, "disabled", False,
     {"D2-04": {"verdict": "INCOMPLETE", "J5": "INCOMPLETE", "outcome": "search_unavailable"}}),
    ("검색 오류", ["D2-04"], {"D2-04": {"search": "error"}}, "live", False,
     {"D2-04": {"verdict": "INCOMPLETE", "J5": "INCOMPLETE", "outcome": "search_error"}}),
    ("검색 한도 초과", ["D2-03"], {"D2-03": {"search": "limit"}}, "live", False,
     {"D2-03": {"verdict": "FAIL", "J5": "FAIL", "outcome": "search_limit_reached", "factual": False}}),
    ("출처 원문 확보 실패", ["G4-01"], {}, "live", True,
     {"G4-01": {"verdict": "FAIL", "J5": "FAIL", "outcome": "source_unavailable", "factual": False, "mismatch": False}}),
    ("구조화 판정 응답 오류", ["GEN-01"], {"GEN-01": {"invalid": True}}, "live", False,
     {"GEN-01": {"verdict": "INCOMPLETE", "status": JevStatus.INVALID_RESPONSE}}),
)


def run_scenarios(fixture, folder):
    by_id = {c["id"]: c for c in fixture["cases"]}
    results = []
    for n, (name, ids, behaviors, web_search, broken_fetch, want) in enumerate(SCENARIOS, 1):
        cases = [by_id[i] for i in ids]
        client, config, _ = mock_client(cases, behaviors, web_search=web_search,
                                        fetcher=(lambda url: {"status": "fetch_error"}) if broken_fetch else None)
        batch, guard, timing = run_pipeline(cases, client, config, Path(folder) / f"scenario-{n:02d}", max_usd=100, max_calls=10)
        actual = _verdicts(analyze(cases, batch, guard, timing, mode="mock-scenario"))
        problems = []
        for cid, expect in want.items():
            got = actual[cid]
            for key, value in expect.items():
                observed = {"verdict": got["verdict"], "status": got["semantic_status"],
                            "outcome": (got["j5"] or {}).get("outcome"),
                            "factual": (got["j5"] or {}).get("factual_error_confirmed"),
                            "mismatch": (got["j5"] or {}).get("citation_mismatch"),
                            "reevaluated": {k: v["final"] for k, v in got["reevaluated"].items()}}.get(key, got["semantic"].get(key))
                if observed != value:
                    problems.append(f"{cid}.{key}: 기대 {value} / 실제 {observed}")
        results.append({"scenario": name, "cases": ids, "ok": not problems, "problems": problems,
                        "calls": guard.new_calls})
    return results


def run_mock(fixture, folder):
    """20문항 이상적 모의 + 강제 실패 + 비용 상한·재개. 모두 통과하면 True."""
    folder = Path(folder)
    cases = fixture["cases"]
    client, config, judge = mock_client(cases)
    batch, guard, timing = run_pipeline(cases, client, config, folder / "ideal", max_usd=100, max_calls=100)
    ideal = analyze(cases, batch, guard, timing, mode="mock")
    write_json(folder / "ideal" / "analysis.json", ideal)
    (folder / "ideal" / "report.md").write_text(report_markdown(ideal), encoding="utf-8")
    # 기대와 다른 문항은 흐름 오류가 아니라 판정 차이로 따로 남긴다 (평가기와 무관한 CODE 결과 포함).
    mismatches = {it["id"]: [c["name"] for c in it["comparison"]["checks"] if not c["ok"]]
                  for it in ideal["items"] if not it["comparison"]["detail_match"]}
    flow = {
        "one_call_per_target": guard.new_calls == len(cases) and sum(judge.calls.values()) == len(cases),
        "all_priced": ideal["summary"]["performance"]["unpriced_calls"] == 0,
        "records_saved": len(list((folder / "ideal" / "records").glob("*.json"))) == len(batch["cases"]),
        "history_turn_order": all(
            judge.histories[c["id"]] == [(t["turn"], t["question"], t["answer"]) for t in c["turns"][:-1]]
            for c in cases if c.get("turns")),
        "originals_unchanged": all(
            (c["question"], c["answer"]) == (p["question"], p["answer"])
            for c, p in zip(batch["cases"], build_inputs(cases)[0])),
    }
    # 비용 상한: 첫 호출 뒤 멈추고, 재개하면 저장된 호출은 다시 하지 않는다.
    capped = folder / "capped"
    client, config, judge = mock_client(cases)
    batch, guard, timing = run_pipeline(cases, client, config, capped, max_usd=Decimal("0.0001"), max_calls=100, workers=1)
    first = analyze(cases, batch, guard, timing, mode="mock-capped")["summary"]
    flow["cap_stops_new_calls"] = guard.new_calls == 1 and first["completed"] == 1 and first["verdicts"].get("PASS", 0) <= 1
    client, config, judge = mock_client(cases)
    batch, guard, timing = run_pipeline(cases, client, config, capped, max_usd=100, max_calls=100)
    resumed = analyze(cases, batch, guard, timing, mode="mock-resumed")["summary"]
    flow["resume_reuses_saved_calls"] = (guard.new_calls == len(cases) - 1 and resumed["performance"]["replayed_calls"] == 1
                                         and resumed["matches"] == ideal["summary"]["matches"])
    scenarios = run_scenarios(fixture, folder / "scenarios")
    result = {"flow": flow, "scenarios": scenarios, "ideal": ideal["summary"], "ideal_mismatches": mismatches,
              "ok": all(flow.values()) and all(s["ok"] for s in scenarios)}
    write_json(folder / "mock-summary.json", result)
    return result


# --- 무료 토큰 계산 ----------------------------------------------------------------------

def count_tokens(fixture, folder):
    """실제 첫 요청과 같은 본문을 무료 count_tokens API 로 센다. 평가 호출은 하지 않는다."""
    from code_eval.llm_client import env_value
    key = env_value("ANTHROPIC_API_KEY", env_path=ROOT / ".env")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY 가 없습니다.")
    cases = fixture["cases"]
    sent = []
    judge = MockJudge(cases)

    def capture(request):
        sent.append(json.loads(request.content))
        return judge.handler(request)
    config = AnthropicSemanticConfig(model=LIVE_MODEL, reasoning_effort="high", web_search="live")
    client = AnthropicSemanticClient(config, "capture-only", transport=httpx.MockTransport(capture),
                                     source_fetcher=judge.fetcher)
    run_pipeline(cases, client, config, Path(folder) / "capture", max_usd=100, max_calls=100, workers=1)
    rows = []
    with httpx.Client(timeout=30) as http:
        for body in sent:
            answer = json.loads(prompt_text(body).split("## 평가 대상 (데이터)\n", 1)[1])["answer"]
            case = judge.cases[answer]
            request = {k: body[k] for k in ("model", "system", "messages", "tools", "thinking", "tool_choice")}
            response = http.post("https://api.anthropic.com/v1/messages/count_tokens", json=request,
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            if response.status_code != 200:
                rows.append({"case": case["id"], "error": response.status_code, "detail": response.text[:300]})
                continue
            row = {"case": case["id"], "input_tokens": response.json().get("input_tokens"),
                   "criteria": body["tools"][-1]["input_schema"]["required"],
                   "search_tools": any(t.get("name") == "web_search" for t in body["tools"])}
            content = body["messages"][0]["content"]
            if isinstance(content, list):     # 캐시 표시까지의 고정 부분(도구·시스템·평가 규칙)만 센다
                cached = [block for block in content if "cache_control" in block]
                prefix = http.post("https://api.anthropic.com/v1/messages/count_tokens",
                                   json={**request, "messages": [{"role": "user", "content": cached}]},
                                   headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
                row["cacheable_prefix_tokens"] = prefix.json().get("input_tokens") if prefix.status_code == 200 else None
            rows.append(row)
    write_json(Path(folder) / "count-tokens.json", rows)
    return rows


# --- 실행 ------------------------------------------------------------------------------

def git_state():
    def run(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    return {"commit": run("rev-parse", "HEAD"), "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(run("status", "--porcelain"))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="실제 Claude 평가 호출 (유료) — --max-usd 필수")
    parser.add_argument("--max-usd", type=float, help="추정 비용 상한(USD). 닿으면 새 호출을 시작하지 않음")
    parser.add_argument("--max-calls", type=int, default=40, help="새 호출 수 상한 (재평가 포함)")
    parser.add_argument("--workers", type=int, default=4, help="동시 평가 수 (운영 기본값 4)")
    parser.add_argument("--resume", help="이전 실행 폴더 — 저장된 호출은 다시 하지 않음")
    parser.add_argument("--count-tokens", action="store_true", help="무료 count_tokens API 로 첫 요청 입력 토큰만 계산")
    parser.add_argument("--cases", help="쉼표로 구분한 문항 ID만 실제 실행·토큰 계산 (예: G4-01,G4-02) — 단계별 재검증")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    fixture = load_fixture()
    if args.cases and not (args.live or args.count_tokens):
        parser.error("--cases 는 --live 또는 --count-tokens 와 함께 쓴다 (모의 검증은 강제 실패 시나리오 때문에 전체 문항)")
    if args.cases:
        wanted = [cid.strip() for cid in args.cases.split(",") if cid.strip()]
        unknown = sorted(set(wanted) - {case["id"] for case in fixture["cases"]})
        if unknown or not wanted:
            parser.error("알 수 없는 문항 ID: " + ", ".join(unknown or ["(없음)"]))
        fixture = {**fixture, "cases": [case for case in fixture["cases"] if case["id"] in wanted]}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.count_tokens:
        rows = count_tokens(fixture, OUTPUT / f"count-{stamp}")
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not args.live:
        folder = OUTPUT / f"mock-{stamp}"
        result = run_mock(fixture, folder)
        print(json.dumps({"ok": result["ok"], "flow": result["flow"],
                          "scenarios": [{k: s[k] for k in ("scenario", "ok", "problems")} for s in result["scenarios"]],
                          "ideal": {k: result["ideal"][k] for k in ("verdicts", "matches")},
                          "ideal_mismatches": result["ideal_mismatches"], "folder": str(folder)},
                         ensure_ascii=False, indent=1))
        return 0 if result["ok"] else 1
    if args.max_usd is None or args.max_usd <= 0:
        parser.error("--live 실행에는 양수 --max-usd 가 필요합니다.")
    client, config, reason = resolve_jev_client(env_path=ROOT / ".env")
    if client is None or getattr(config, "engine", None) != "anthropic_api" or config.model != LIVE_MODEL:
        raise SystemExit(f"설정된 평가기가 Claude API {LIVE_MODEL} 가 아닙니다: {reason or getattr(config, 'model', None)}")
    folder = Path(args.resume) if args.resume else OUTPUT / f"live-{stamp}"
    manifest = {"started_at": datetime.now(timezone.utc).isoformat(), "fixture": str(FIXTURE.relative_to(ROOT)),
                "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), "git": git_state(),
                "client": config.to_manifest(), "max_usd": args.max_usd, "max_calls": args.max_calls,
                "workers": args.workers, "resumed": bool(args.resume), "cases": [case["id"] for case in fixture["cases"]]}
    write_json(folder / ("manifest-resume.json" if args.resume else "manifest.json"), manifest)
    started = time.perf_counter()
    batch, guard, timing = run_pipeline(fixture["cases"], client, config, folder, max_usd=args.max_usd,
                                        max_calls=args.max_calls, workers=args.workers)
    timing["total_wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
    analysis = analyze(fixture["cases"], batch, guard, timing, mode="live")
    analysis["summary"]["versions"] = {"batch": batch.get("versions"), "jev_eval": batch["jev_eval"].get("version"),
                                       "decision_policy": batch["jev_eval"].get("decision_policy")}
    write_json(folder / "analysis.json", analysis)
    (folder / "report.md").write_text(report_markdown(analysis), encoding="utf-8")
    print(report_markdown(analysis))
    print(str(folder))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
