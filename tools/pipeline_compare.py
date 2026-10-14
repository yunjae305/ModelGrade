"""같은 입력의 의미 평가 두 방식(legacy·items) 비교 — 저장된 비교 실행의 실제 답변으로 적용 기준·판정 차이를 표로 낸다.

    python tools/pipeline_compare.py select  RUN.json --out sel.json [--count 8]        # 대표 답변 고르기 (무료)
    python tools/pipeline_compare.py routing RUN.json [--selection sel.json] [--json]   # 적용 기준·결과 유형 차이 (무료, 모델 호출 없음)
    python tools/pipeline_compare.py run     RUN.json --selection sel.json --out DIR --mock          # 모의 심판으로 경로만 검증 (무료)
    python tools/pipeline_compare.py run     RUN.json --selection sel.json --out DIR --live --max-usd 3 --max-calls 16
                                                                                         # 실제 평가기 호출 (유료 — 승인 후에만)
    python tools/pipeline_compare.py diff    RUN.json --items DIR [--json]                # 판정 차이 표 (legacy 저장 판정 vs 새 items 판정)
    python tools/pipeline_compare.py j5-routing RUN.json [--json]                        # legacy J5 FAIL 마다 items J5 세부 기준 적용 여부·이유 (무료)
    python tools/pipeline_compare.py plan    RUN.json --selection sel.json --max-usd 8      # 실호출 계획: 재사용·최대 호출 수·예약금·지출 상한 (무료)

- legacy 쪽은 저장된 실제 판정을 그대로 쓴다(다시 호출하지 않음). items 쪽만 새로 평가한다.
- 대화 턴은 같은 대화의 앞 턴을 이력으로만 넣고(CODE·이력용, 의미 평가 호출 없음) 고른 턴만 호출한다.
- 모의 심판은 대본 없는 항목을 모두 YES 로 답한다 — 경로·저장·집계 검증용이고 판정 품질 비교가 아니다. 결과에 mode 를 남긴다.
- 실제 호출은 --live 와 --max-usd 가 모두 있어야 하고, 추정 비용·새 호출 수가 상한에 닿으면 새 호출을 시작하지 않는다.
  호출마다 예약금(--reserve-usd, 기본 = 저장 실행에서 본 호출 하나의 최대 비용 × 1.5)을 잡아 '쓴 금액 + 진행 중 예약 + 새 예약 ≤ 상한' 일 때만
  시작하므로 상한이 곧 지출 상한이다. 호출 하나가 예약을 넘으면 바로 멈추고 기록한다 (API 에는 호출 하나의 비용 상한이 없다).
  호출마다 DIR/calls 에 즉시 저장하고, 같은 DIR 로 다시 실행하면 저장된 호출은 다시 하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval.config import load_aiona_config  # noqa: E402
from code_eval.engine import evaluate_batch  # noqa: E402
from code_eval.jev_applicability import sub_applicability  # noqa: E402
from code_eval.jev_eval import build_history, conversation_contexts, evaluate_jev_batch  # noqa: E402
from code_eval.semantic_items import applicable_items  # noqa: E402

COMPARE_VERSION = "pipeline-compare-v1"
CRITERIA = ("J1", "J2", "J3", "J4", "J5", "J6", "J7")
MOCK_NOTE = ("모의 심판(대본 없는 항목은 모두 YES, 심판이 적용을 정하는 항목은 적용) — 경로·저장·집계 검증용, 판정 품질 비교 아님. "
             "사실 블록 항목(J5-1.a)은 모의에서 '해당 없음'이라 J5 적용 차이는 routing 결과를 본다")


# --- 저장 실행 읽기 ---------------------------------------------------------------------------

def answers(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """시스템·위치별 답변 — 평가 때 쓴 질문·답변 원문(evaluations.cases)과 저장 판정(records)."""
    out = []
    for system, batch in sorted((run.get("evaluations") or {}).items()):
        rows = (run.get("datasets") or {}).get(system) or []
        records = (batch.get("jev_eval") or {}).get("records") or []
        for index, case in enumerate(batch.get("cases") or []):
            row = rows[index] if index < len(rows) and isinstance(rows[index], dict) else {}
            out.append({"system": system, "index": index, "case_id": case.get("case_id"), "question": case.get("question") or "",
                        "answer": case.get("answer") or "", "case_status": case.get("case_status"),
                        "conversation_id": row.get("conversation_id"), "turn": row.get("turn"),
                        "record": records[index] if index < len(records) and isinstance(records[index], dict) else {}})
    return out


def outcome_class(decision: Optional[Dict[str, Any]]) -> str:
    """저장된 기준 판정 하나의 결과 유형 — 단순 PASS · 근거 있는 FAIL · 정책상 FAIL(근거·원문 요건 미충족) · 사실 오류 확정 · 미판정."""
    if not isinstance(decision, dict):
        return "missing"
    value = decision.get("decision")
    if value == "PASS":
        return "pass"
    if value != "FAIL":
        return "unverified" if decision.get("status") == "UNVERIFIED" else "incomplete"
    outcome = decision.get("j5_outcome") or {}
    if outcome:
        if outcome.get("factual_error_confirmed"):
            return "fail_fact_confirmed"
        kind = (outcome.get("source_failure") or {}).get("kind")   # 원문 접근 실패 · 인용문 미확인 · 둘 다
        return "fail_policy_" + str(outcome.get("code")) + (f":{kind}" if kind else "")
    if decision.get("reason_code") in ("ITEM_UNVERIFIABLE", "FACT_UNVERIFIABLE"):
        return "fail_policy_unverifiable"
    basis = (decision.get("decision_policy") or {}).get("outcome_basis")
    if basis in ("evidence_requirement", "source_verification_requirement"):
        return "fail_policy_" + basis
    return "fail_with_evidence"


# --- 대표 답변 고르기 ---------------------------------------------------------------------------

def select(run: Dict[str, Any], count: int = 8) -> List[Dict[str, Any]]:
    """legacy 판정 유형이 겹치지 않게 고른다 — J5 정책상 FAIL 유형별 1개, J2·J3·J4·J6 FAIL, 나머지는 모두 PASS 인 답변."""
    rows = [a for a in answers(run) if a["case_status"] == "EVALUATED" and a["record"].get("status") == "SUCCESS"]
    picked: List[Dict[str, Any]] = []
    seen_kinds = set()

    def take(a, why):
        if len(picked) < count and all((p["system"], p["index"]) != (a["system"], a["index"]) for p in picked):
            picked.append({"system": a["system"], "index": a["index"], "case_id": a["case_id"],
                           "conversation_id": a["conversation_id"], "turn": a["turn"], "why": why})
    for a in rows:
        decisions = a["record"].get("decisions") or {}
        for cid in a["record"].get("criteria") or []:
            kind = outcome_class(decisions.get(cid))
            if kind.startswith("fail") and (cid, kind) not in seen_kinds:
                seen_kinds.add((cid, kind))
                take(a, f"legacy {cid} {kind}")
    for a in rows:   # 모두 PASS — 단일 질문 하나, 대화 턴 하나 (J6 가 적용되는 2턴 이상)
        decisions = a["record"].get("decisions") or {}
        if a["conversation_id"] and (a["turn"] or 0) < 2:
            continue
        if all(outcome_class(decisions.get(c)) == "pass" for c in a["record"].get("criteria") or []):
            kind = "conversation" if a["conversation_id"] else "single"
            if ("pass", kind) not in seen_kinds:
                seen_kinds.add(("pass", kind))
                take(a, f"legacy 모두 PASS ({'대화 턴' if kind == 'conversation' else '단일 질문'})")
    return picked


# --- 적용 기준 비교 (무료) -------------------------------------------------------------------

def _payloads(run_answers: List[Dict[str, Any]], prefix: str = "") -> List[Dict[str, Any]]:
    return [{"case_id": f"{prefix}{a['system']}:{a['case_id']}:{a['index']}", "question": a["question"], "answer": a["answer"],
             **({"conversation_id": f"{a['system']}:{a['conversation_id']}", "turn": a["turn"]} if a["conversation_id"] else {})}
            for a in run_answers]


def _code_cases(payloads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cfg = load_aiona_config()
    cfg.enabled = False                      # 웹 서비스와 같은 CODE 설정 (AIONA 특화 일시 중지)
    cfg.raw_hash = "paused-" + cfg.raw_hash
    return evaluate_batch(payloads, cfg)["cases"] if payloads else []


def routing(run: Dict[str, Any], selection: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """기준별 적용 차이 — legacy 는 저장된 실제 적용(J5 사전 선별 후), items 는 같은 원문에 평가와 같은 항목 규칙(호출 없음)."""
    rows = answers(run)
    cases = _code_cases(_payloads(rows))
    contexts = conversation_contexts(cases)
    wanted = {(s["system"], s["index"]) for s in selection} if selection else None
    table = {cid: Counter() for cid in CRITERIA}
    subs_hit: Counter = Counter()
    item_counts: List[int] = []
    examples: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    classes = {cid: Counter() for cid in CRITERIA}
    for i, a in enumerate(rows):
        if wanted is not None and (a["system"], a["index"]) not in wanted:
            continue
        record = a["record"]
        legacy = set(record.get("criteria") or [])
        for cid in legacy:
            classes[cid][outcome_class((record.get("decisions") or {}).get(cid))] += 1
        if cases[i].get("case_status") != "EVALUATED":
            continue
        ctx, history, conversation = contexts.get(i), [], None
        if ctx:
            history, _ = build_history(ctx["prior"])
            conversation = {"id": ctx["id"], "turn": ctx["turn"], "issues": ctx["issues"],
                            "history_turns": len(history) if ctx["complete"] else 0, "multi_turn": ctx.get("multi_turn")}
        tools = "unknown" if a["system"] == "aiona" else "none"
        subs = sub_applicability(a["question"], a["answer"], conversation=conversation, answer_tools=tools, history=history)
        sent = applicable_items({"question": a["question"], "answer": a["answer"], "answer_tools": tools, "history": history},
                                subs, conversation)
        item_counts.append(len(sent))
        items_criteria = {item.id.split("-", 1)[0] for item, _ in sent}
        subs_hit.update({item.id.split(".", 1)[0] for item, _ in sent})
        for cid in CRITERIA:
            key = ("both" if cid in legacy and cid in items_criteria else "legacy_only" if cid in legacy
                   else "items_only" if cid in items_criteria else "neither")
            table[cid][key] += 1
            if key in ("legacy_only", "items_only") and len(examples[f"{cid}:{key}"]) < 3:
                examples[f"{cid}:{key}"].append({"system": a["system"], "case_id": a["case_id"], "question": a["question"][:80],
                                                 "items": sorted(item.id for item, _ in sent if item.id.startswith(cid + "-"))})
    return {"version": COMPARE_VERSION, "run_id": run.get("run_id"), "answers": len(item_counts),
            "criteria": {cid: dict(table[cid]) for cid in CRITERIA},
            "legacy_outcome_classes": {cid: dict(classes[cid]) for cid in CRITERIA},
            "items_per_answer": {"min": min(item_counts, default=0), "max": max(item_counts, default=0),
                                 "mean": round(sum(item_counts) / len(item_counts), 1) if item_counts else 0},
            "subs_applied": dict(sorted(subs_hit.items())), "examples": dict(examples)}


def _contexts_for(rows: List[Dict[str, Any]]):
    cases = _code_cases(_payloads(rows))
    return cases, conversation_contexts(cases)


def _conversation_args(contexts, i):
    ctx, history, conversation = contexts.get(i), [], None
    if ctx:
        history, _ = build_history(ctx["prior"])
        conversation = {"id": ctx["id"], "turn": ctx["turn"], "issues": ctx["issues"],
                        "history_turns": len(history) if ctx["complete"] else 0, "multi_turn": ctx.get("multi_turn")}
    return history, conversation


def j5_routing(run: Dict[str, Any], *, fails_only: bool = True) -> Dict[str, Any]:
    """legacy J5 FAIL 답변마다 items J5 세부 기준(J5-1 사실 정확성·J5-2 입력 자료 충실성·J5-3 확인 과정)의 적용 여부와 이유.

    분류: items_routed(items 도 J5 를 묻는다 — 판정이 바뀌는지는 실호출 비교로만 안다) ·
    scope_gap_no_fact_query(요청 문장에 사실 질의 표현이 없어 J5-1 미적용, 질문이 준 값·확인 과정 언급도 없어 J5-2·J5-3 미적용) ·
    scope_gap_code_task(사실 질의 표현은 있으나 코드 결과물 요청이라 J5-1 보류 '정할 것') · scope_gap_other(그 밖의 이유).
    scope_gap_* 는 items 가 J5 를 묻지 않아 생긴 차이 — 판정 개선이 아니라 평가 대상 범위 차이다.
    legacy 쪽 주장 수는 평가자가 답변에서 뽑은 외부 사실 주장 — 기준 정의(답변의 데이터가 실제와 맞는가)상 J5 대상이었다는 근거다.
    """
    rows = answers(run)
    cases, contexts = _contexts_for(rows)
    out, counts = [], Counter()
    for i, a in enumerate(rows):
        d = (a["record"].get("decisions") or {}).get("J5")
        if not d or (fails_only and d.get("decision") != "FAIL") or cases[i].get("case_status") != "EVALUATED":
            continue
        history, conversation = _conversation_args(contexts, i)
        tools = "unknown" if a["system"] == "aiona" else "none"
        subs = sub_applicability(a["question"], a["answer"], conversation=conversation, answer_tools=tools, history=history)
        j5 = {sid: {"items": sub.get("items") or [], "reason": sub.get("not_applicable") or sub.get("reason"),
                    "rules": sub.get("rules") or []} for sid, sub in subs.items() if sid.startswith("J5-")}
        routed = any(sub["items"] for sub in j5.values())
        reason1 = j5.get("J5-1", {}).get("reason") or ""
        kind = ("items_routed" if routed else "scope_gap_no_fact_query" if reason1.startswith("요청 문장에 사실 질의 표현 없음")
                else "scope_gap_code_task" if reason1.startswith("코드 결과물") else "scope_gap_other")
        counts[kind] += 1
        fact = d.get("fact_check") or (d.get("policy_original_assessment") or {}).get("fact_check") or {}
        claims = [c for c in fact.get("claims") or [] if isinstance(c, dict)]
        out.append({"system": a["system"], "case_id": a["case_id"], "legacy": outcome_class(d), "kind": kind,
                    "claims": len(claims), "claim_verdicts": dict(Counter(str(c.get("verdict")) for c in claims)),
                    "claim_scopes": dict(Counter(str(c.get("scope")) for c in claims)),
                    "items_j5": j5, "question_excerpt": a["question"][:60]})
    return {"version": COMPARE_VERSION, "run_id": run.get("run_id"), "rows": out, "counts": dict(counts)}


def render_j5_routing(report: Dict[str, Any]) -> str:
    lines = [f"# legacy J5 FAIL 의 items J5 적용 ({report['version']}, 실행 {report['run_id']}, 모델 호출 없음)",
             "분류: " + ", ".join(f"{k} {v}" for k, v in sorted(report["counts"].items())), "",
             "| 시스템 | 질문 | legacy | 분류 | legacy 주장(판정) | J5-1 | J5-2 | J5-3 |", "|---|---|---|---|---|---|---|---|"]
    for r in report["rows"]:
        cell = lambda sid: ",".join(r["items_j5"].get(sid, {}).get("items") or []) or (r["items_j5"].get(sid, {}).get("reason") or "-")
        lines.append(f"| {r['system']} | {r['case_id']} | {r['legacy']} | {r['kind']} | {r['claims']} {r['claim_verdicts']} | "
                     f"{cell('J5-1')} | {cell('J5-2')} | {cell('J5-3')} |")
    return "\n".join(lines)


# --- items 새 평가 ---------------------------------------------------------------------------

def _inputs(run: Dict[str, Any], selection: List[Dict[str, Any]]) -> Tuple[Dict[str, List[Dict[str, Any]]], set]:
    """시스템별 엔진 입력(대화는 고른 턴까지 앞 턴 포함)과 호출 대상 case_id."""
    rows = answers(run)
    by_system: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    targets = set()
    for pick in selection:
        a = next(r for r in rows if (r["system"], r["index"]) == (pick["system"], pick["index"]))
        group = [a]
        if a["conversation_id"]:
            group = sorted((r for r in rows if r["system"] == a["system"] and r["conversation_id"] == a["conversation_id"]
                            and (r["turn"] or 0) <= (a["turn"] or 0)), key=lambda r: r["turn"])
        for payload in _payloads(group):
            if all(p["case_id"] != payload["case_id"] for p in by_system[a["system"]]):
                by_system[a["system"]].append(payload)
        targets.add(_payloads([a])[0]["case_id"])
    for system in by_system:
        by_system[system].sort(key=lambda p: (p.get("conversation_id") or "", p.get("turn") or 0, p["case_id"]))
    return dict(by_system), targets


RECHECK_ROUNDS = 3   # items 재확인 최대 회차 (semantic_quotes.MAX_RECHECK_ROUNDS) — 답변마다 본 평가 1회 + 재확인 최대 3회


def call_costs(run: Dict[str, Any]) -> List[float]:
    """저장 실행의 의미 평가 HTTP 요청마다 비용 (요청별 사용량이 있는 것만)."""
    from code_eval.cost_reporting import estimate
    costs = []
    for batch in (run.get("evaluations") or {}).values():
        semantic = batch.get("jev_eval") or {}
        engine = semantic.get("engine") or (semantic.get("client") or {}).get("engine")
        for record in semantic.get("records") or []:
            for part in ((record or {}).get("verification") or {}).get("usage_requests") or []:
                try:
                    costs.append(float(estimate(engine, part.get("model") or record.get("model"), part.get("usage"),
                                                web_search_calls=part.get("web_search_calls"), service_tier=part.get("service_tier"),
                                                inference_geo=part.get("inference_geo"))[0]))
                except (ValueError, AttributeError):
                    continue
    return costs


def _request_parts(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """저장 실행의 의미 평가 HTTP 요청마다 입력·출력 토큰·검색 수·비용 (요청별 사용량이 있는 것만)."""
    from code_eval.cost_reporting import estimate
    out = []
    for batch in (run.get("evaluations") or {}).values():
        semantic = batch.get("jev_eval") or {}
        engine = semantic.get("engine") or (semantic.get("client") or {}).get("engine")
        for record in semantic.get("records") or []:
            for part in ((record or {}).get("verification") or {}).get("usage_requests") or []:
                usage = part.get("usage") or {}
                try:
                    usd = float(estimate(engine, part.get("model") or record.get("model"), usage, web_search_calls=part.get("web_search_calls"),
                                         service_tier=part.get("service_tier"), inference_geo=part.get("inference_geo"))[0])
                except (ValueError, AttributeError):
                    continue
                out.append({"input_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
                            + int(usage.get("cache_creation_input_tokens") or 0),
                            "output_tokens": int(usage.get("output_tokens") or 0), "searches": int(part.get("web_search_calls") or 0), "usd": usd})
    return out


def _stats(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
    return {"n": len(values), "mean": round(sum(values) / len(values), 4), "median": round(statistics.median(values), 4),
            "p90": round(p90, 4), "max": round(max(values), 4)}


def _judge_prices(model: str = "claude-opus-5-5") -> Dict[str, float]:
    """평가 모델 단가 — code_eval/cost_reporting.estimate 로 1M 토큰씩 넣어 읽는다 (단가를 따로 적지 않는다)."""
    from code_eval.cost_reporting import estimate
    zero = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
    def price(**fields):
        usage = {**zero, **fields}
        if "cache_creation_input_tokens" in fields:
            usage["cache_creation"] = {"ephemeral_5m_input_tokens": fields["cache_creation_input_tokens"], "ephemeral_1h_input_tokens": 0}
        return float(estimate("anthropic_api", model, usage, web_search_calls=0, service_tier="standard", inference_geo="global")[0])
    one_search = float(estimate("anthropic_api", model, zero, web_search_calls=1, service_tier="standard", inference_geo="global")[0])
    return {"model": model, "input_per_m": price(input_tokens=1_000_000), "cache_read_per_m": price(cache_read_input_tokens=1_000_000),
            "cache_write_5m_per_m": price(cache_creation_input_tokens=1_000_000), "output_per_m": price(output_tokens=1_000_000),
            "search_each": one_search}


def plan(run: Dict[str, Any], selection: List[Dict[str, Any]], *, max_usd: float, reserve_usd: Optional[float] = None,
         budgets=(8, 16)) -> Dict[str, Any]:
    """실호출 전 계획 — legacy 저장 판정은 그대로 다시 쓰고(호출 0), items 만 새로 부른다.
    예약금 = 저장 실행에서 본 요청 하나의 최대 비용 × 1.5 를 센트 단위로 올림. 예산마다 최악(호출마다 예약금만큼)에도 시작할 수 있는 호출 수,
    중앙값 비용이면 시작될 호출 수, 전체 최대 호출의 최악 예약액을 낸다. 이 제한은 소프트웨어 제한이고 실제 과금의 하드캡이 아니다."""
    parts = _request_parts(run)
    costs = [p["usd"] for p in parts]
    observed_max = max(costs) if costs else None
    reserve = reserve_usd if reserve_usd is not None else (math.ceil(round(observed_max * 1.5 * 100, 6)) / 100 if observed_max else None)
    saved = {(a["system"], a["index"]): a for a in answers(run)}
    legacy = 0.0
    for pick in selection:
        record = saved[(pick["system"], pick["index"])]["record"]
        legacy += sum(call_costs({"evaluations": {"x": {"jev_eval": {"engine": "anthropic_api", "records": [record]}}}}))
    max_calls = len(selection) * (1 + RECHECK_ROUNDS)
    median = statistics.median(costs) if costs else None
    try:
        from code_eval.llm_client import env_value
        output_cap = int(env_value("SEMANTIC_ANTHROPIC_MAX_OUTPUT_TOKENS") or 12000)
    except (TypeError, ValueError):
        output_cap = 12000
    table = {}
    for budget in budgets:
        guaranteed = min(max_calls, int(budget // reserve)) if reserve else None   # 계획한 최대 호출 수를 넘지 않음
        at_median = (min(max_calls, int((budget - reserve) // median) + 1) if reserve and median and budget >= reserve else 0)
        table[str(budget)] = {"guaranteed_starts": guaranteed, "starts_if_every_call_costs_the_median": at_median,
                              "all_main_calls_guaranteed": bool(guaranteed is not None and guaranteed >= len(selection)),
                              "needed_to_guarantee_all_main_calls": round(len(selection) * reserve, 2) if reserve else None}
    spread = {"max_over_median": round(observed_max / median, 2) if median else None,
              "p90_over_median": round(_stats(costs)["p90"] / median, 2) if median else None}
    return {"version": COMPARE_VERSION, "answers": len(selection), "legacy_reused_calls": 0, "legacy_cost_of_selection": round(legacy, 3),
            "items_max_new_calls": max_calls, "calls_per_answer": f"본 평가 1 + 재확인 최대 {RECHECK_ROUNDS} (요청마다 이어받기 최대 2회 포함)",
            "observed_calls": len(costs), "observed_call_cost_max": round(observed_max, 3) if observed_max else None,
            "per_request": {"input_tokens": _stats([p["input_tokens"] for p in parts]), "output_tokens": _stats([p["output_tokens"] for p in parts]),
                            "searches": _stats([p["searches"] for p in parts]), "usd": _stats(costs)},
            "estimate_error": {**spread, "note": "같은 평가 모델·legacy 요청의 실제 비용 분포 — items 는 점검 항목 설명이 붙어 프롬프트가 길어 위로 치우칠 수 있다"},
            "prices": _judge_prices(), "output_token_cap": output_cap,
            "reserve_per_call": reserve, "max_usd": max_usd, "guaranteed_starts": min(max_calls, int(max_usd // reserve)) if reserve else None,
            "budgets": table, "worst_case_reservation_all_calls": round(max_calls * reserve, 2) if reserve else None,
            "spend_bound": (f"소프트웨어 제한 — 쓴 금액 + 진행 중 예약 + 새 예약 ≤ ${max_usd} 일 때만 새 호출을 시작한다. 실제 과금의 하드캡이 아니다: "
                            f"API 에 호출 하나의 비용 상한이 없어, 한 호출이 예약 ${reserve} 를 넘으면 그만큼 넘을 수 있고 그 즉시 멈춘다") if reserve else None}


def run_items(run: Dict[str, Any], selection: List[Dict[str, Any]], out: Path, *, mode: str,
              max_usd: float = 0.0, max_calls: int = 0, workers: int = 2, reserve_usd: float = 0.0) -> Dict[str, Any]:
    from tests.live_stage3_validation import GuardedClient, write_json
    out.mkdir(parents=True, exist_ok=True)
    by_system, targets = _inputs(run, selection)
    collected = run.get("collection_completed_at")
    batches, guards = {}, []
    if mode == "mock":
        from tools.items_e2e_demo import MockJudge, _no_network

        class _ApplyingMock(MockJudge):
            def script_for(self, state):
                return {"apply_model_items": True}
        client, config, network = _ApplyingMock({}, {}), None, _no_network
    else:
        from contextlib import nullcontext
        from code_eval.jev_client import resolve_jev_client
        client, config, reason = resolve_jev_client()
        if client is None:
            raise SystemExit(f"평가기를 만들 수 없음: {reason}")
        network = nullcontext
    config = config or client.config
    stop = threading.Event()
    spent_guard = {"usd": max_usd if mode == "live" else 1e9, "calls": max_calls if mode == "live" else 10 ** 6}
    with network():
        for system, payloads in by_system.items():
            index = {(p["question"], p["answer"]): p["case_id"] for p in payloads}
            if len(index) != len(payloads):
                raise ValueError(f"{system}: 질문·답변 쌍이 중복되어 호출 대상을 구분할 수 없습니다.")
            guard = GuardedClient(client, config, out / system, index, {t for t in targets if t in index.values()},
                                  max_usd=spent_guard["usd"], max_calls=spent_guard["calls"], stop=stop,
                                  reserve_usd=reserve_usd if mode == "live" else 0)
            guards.append(guard)
            batch = {"cases": _code_cases(payloads)}
            context = {"answer_tools": "unknown" if system == "aiona" else "none",
                       "collected_at": ((run.get("aiona_release") or {}).get("collected_at") if system == "aiona"
                                        else collected[:10] if isinstance(collected, str) else None)}
            batch["jev_eval"] = evaluate_jev_batch(batch, guard, config, None, workers=workers, cancel_event=stop,
                                                   pipeline="items", context=context)
            batches[system] = batch
            # 다음 시스템은 남은 상한만 쓴다 (모든 시스템 합계가 상한을 넘지 않게)
            spent_guard["usd"] = max(0.0, spent_guard["usd"] - float(guard.spent))
            spent_guard["calls"] = max(0, spent_guard["calls"] - guard.new_calls)
    meta = {"version": COMPARE_VERSION, "mode": mode, "note": MOCK_NOTE if mode == "mock" else "실제 평가기 호출",
            "source_run": run.get("run_id"), "created_at": datetime.now(timezone.utc).isoformat(),
            "selection": selection, "targets": sorted(targets),
            "judge": {"engine": getattr(config, "engine", None) or (batches and next(iter(batches.values()))["jev_eval"].get("engine")),
                      "model": getattr(config, "model", None)},
            "calls": {"new": sum(g.new_calls for g in guards), "replayed": sum(1 for g in guards for r in g.rows if r.get("replayed")),
                      "not_called": sum(1 for g in guards for r in g.rows if r.get("status") == "NOT_CALLED"),
                      "usd": round(sum(float(g.spent) for g in guards), 4) if mode == "live" else 0.0},
            "caps": {"max_usd": max_usd, "max_calls": max_calls, "reserve_per_call": reserve_usd} if mode == "live" else None,
            "reservation_exceeded": sum(1 for g in guards for r in g.rows if r.get("reservation_exceeded"))}
    write_json(out / "meta.json", meta)
    write_json(out / "batches.json", batches)
    return {"meta": meta, "batches": batches}


# --- 판정 차이 -------------------------------------------------------------------------------

def _failed_items(decision: Dict[str, Any]) -> List[str]:
    out = []
    for sub in (decision.get("subs") or {}).values():
        for item in sub.get("items") or []:
            if item.get("decision") == "NO" or item.get("unverifiable"):
                out.append(f"{item.get('id')}{' (확인 불가)' if item.get('unverifiable') else ''}: {str(item.get('reason') or '')[:80]}")
    return out


def diff(run: Dict[str, Any], folder: Path) -> Dict[str, Any]:
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    batches = json.loads((folder / "batches.json").read_text(encoding="utf-8"))
    saved = {(a["system"], a["index"]): a for a in answers(run)}
    rows, summary = [], Counter()
    for pick in meta["selection"]:
        a = saved[(pick["system"], pick["index"])]
        target = _payloads([a])[0]["case_id"]
        batch = batches.get(pick["system"]) or {}
        records = (batch.get("jev_eval") or {}).get("records") or []
        position = next((i for i, c in enumerate(batch.get("cases") or []) if c.get("case_id") == target), None)
        new = records[position] if position is not None and position < len(records) else {}
        old_dec, new_dec = a["record"].get("decisions") or {}, new.get("decisions") or {}
        old_set, new_set = set(a["record"].get("criteria") or []), set(new.get("criteria") or [])
        for cid in CRITERIA:
            if cid not in old_set and cid not in new_set:
                continue
            before = outcome_class(old_dec.get(cid)) if cid in old_set else "not_applied"
            after = outcome_class(new_dec.get(cid)) if cid in new_set else "not_applied"
            b_simple, a_simple = before.split("_", 1)[0], after.split("_", 1)[0]
            kind = ("same" if b_simple == a_simple and before != "not_applied" else
                    "applied_only_legacy" if after == "not_applied" else "applied_only_items" if before == "not_applied" else
                    "items_new_fail" if a_simple == "fail" and b_simple == "pass" else
                    "items_cleared_fail" if a_simple == "pass" and b_simple == "fail" else "items_undecided")
            summary[kind] += 1
            rows.append({"system": pick["system"], "case_id": a["case_id"], "why": pick.get("why"), "criterion": cid,
                         "legacy": before, "items": after, "kind": kind,
                         "legacy_reason": str((old_dec.get(cid) or {}).get("reason") or "")[:160],
                         "items_reason": str((new_dec.get(cid) or {}).get("reason") or new.get("error") or "")[:160],
                         "items_failed": _failed_items(new_dec.get(cid) or {})})
    return {"version": COMPARE_VERSION, "mode": meta["mode"], "note": meta["note"], "judge": meta.get("judge"),
            "calls": meta.get("calls"), "summary": dict(summary), "rows": rows}


KIND_TEXT = {"same": "같음", "applied_only_legacy": "legacy 에만 적용", "applied_only_items": "items 에만 적용",
             "items_new_fail": "items 에서 새 FAIL", "items_cleared_fail": "items 에서 통과", "items_undecided": "items 미판정"}


def render_diff(report: Dict[str, Any]) -> str:
    lines = [f"# 의미 평가 방식 판정 차이 ({report['version']}) — {report['note']}",
             f"평가기 {report.get('judge')} · 호출 {report.get('calls')}", "",
             "요약: " + ", ".join(f"{KIND_TEXT.get(k, k)} {v}" for k, v in sorted(report["summary"].items())), "",
             "| 시스템 | 질문 | 기준 | legacy | items | 차이 | items 미충족 항목 |", "|---|---|---|---|---|---|---|"]
    for r in report["rows"]:
        failed = "<br>".join(r["items_failed"]) or "-"
        lines.append(f"| {r['system']} | {r['case_id']} | {r['criterion']} | {r['legacy']} | {r['items']} | {KIND_TEXT.get(r['kind'], r['kind'])} | {failed} |")
    return "\n".join(lines)


def render_routing(report: Dict[str, Any]) -> str:
    lines = [f"# 적용 기준 비교 ({report['version']}, 실행 {report['run_id']}, 답변 {report['answers']}개, 모델 호출 없음)",
             f"items 점검 항목 수/답변: 최소 {report['items_per_answer']['min']} · 평균 {report['items_per_answer']['mean']} · 최대 {report['items_per_answer']['max']}", "",
             "| 기준 | 둘 다 | legacy 만 | items 만 | 둘 다 아님 | legacy 저장 결과 유형 |", "|---|---|---|---|---|---|"]
    for cid, row in report["criteria"].items():
        classes = ", ".join(f"{k} {v}" for k, v in sorted(report["legacy_outcome_classes"][cid].items())) or "-"
        lines.append(f"| {cid} | {row.get('both', 0)} | {row.get('legacy_only', 0)} | {row.get('items_only', 0)} | {row.get('neither', 0)} | {classes} |")
    lines += ["", "세부 기준별 items 적용 답변 수: " + ", ".join(f"{k} {v}" for k, v in report["subs_applied"].items())]
    for key, items in report["examples"].items():
        lines.append(f"- {key}: " + " / ".join(f"{e['system']} {e['case_id']} '{e['question']}' {e['items']}" for e in items))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("select", "routing", "run", "diff", "j5-routing", "plan"))
    parser.add_argument("run", type=Path, help="저장된 비교 실행 JSON (legacy 판정 원본, 읽기만 함)")
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--items", type=Path, help="run 이 만든 폴더")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--live", action="store_true", help="실제 평가기 호출 (유료) — --max-usd·--max-calls 필수")
    parser.add_argument("--max-usd", type=float)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--reserve-usd", type=float, help="호출마다 잡는 예약금 (기본: 저장 실행의 호출 하나 최대 비용 × 1.5)")
    parser.add_argument("--first", type=int, help="선택한 답변 중 앞 N 개만 (승인 뒤 작게 시작할 때)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run = json.loads(args.run.read_text(encoding="utf-8-sig"))
    selection = json.loads(args.selection.read_text(encoding="utf-8")) if args.selection else None
    if selection is not None and args.first:
        selection = selection[:args.first]
    if args.command == "select":
        picked = select(run, args.count)
        text = json.dumps(picked, ensure_ascii=False, indent=1)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text, encoding="utf-8")
        print(text)
    elif args.command == "plan":
        if not selection or not args.max_usd:
            parser.error("plan 은 --selection 과 --max-usd 가 필요")
        print(json.dumps(plan(run, selection, max_usd=args.max_usd, reserve_usd=args.reserve_usd), ensure_ascii=False, indent=1))
    elif args.command == "j5-routing":
        report = j5_routing(run)
        print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else render_j5_routing(report))
    elif args.command == "routing":
        report = routing(run, selection)
        print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else render_routing(report))
    elif args.command == "run":
        if not selection or not args.out or args.mock == args.live:
            parser.error("run 은 --selection·--out 과 --mock 또는 --live 하나가 필요")
        if args.live and (not args.max_usd or not args.max_calls or args.max_usd <= 0 or args.max_calls <= 0):
            parser.error("--live 는 --max-usd 와 --max-calls 가 모두 필요 (양수)")
        reserve = 0.0
        if args.live:
            reserve = args.reserve_usd if args.reserve_usd is not None else plan(run, selection, max_usd=args.max_usd)["reserve_per_call"]
            if not reserve or reserve <= 0:
                parser.error("--live 는 호출마다 잡을 예약금이 필요 (--reserve-usd) — 저장 실행에 요청별 사용량이 없음")
        result = run_items(run, selection, args.out, mode="live" if args.live else "mock",
                           max_usd=args.max_usd or 0.0, max_calls=args.max_calls or 0, workers=args.workers, reserve_usd=reserve)
        print(json.dumps(result["meta"], ensure_ascii=False, indent=1))
    else:
        if not args.items:
            parser.error("diff 는 --items 폴더가 필요")
        report = diff(run, args.items)
        print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else render_diff(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
