"""사람 정답 검수용 추출 — 저장된 비교 실행의 기준 판정(기본 J5 FAIL)을 원인 유형별로 CSV·JSONL 로 낸다 (API 호출 없음, 실행 파일은 읽기만).

    python tools/export_review.py data/comparison_runs/<run>.json                       # J5 FAIL → results/review/<run>-J5.csv·.jsonl
    python tools/export_review.py <run>.json --criterion J3 --include-pass 5           # J3 FAIL 전부 + 시스템마다 PASS 5개 (대조군)
    python tools/export_review.py <run>.json --text none                                # 원문 없이 지문(sha256 앞 12자)만
    python tools/export_review.py <run>.json --held                                     # 보류(확인 필요) 판정 전부 → <run>-held.csv·.jsonl (사람이 gold 에 PASS/FAIL)

- 원문 공유 범위: 기본 excerpt(질문 120자·답변 200자·주장 원문), none(지문만), full(전체 — 명시할 때만). 결과는 gitignore 된 results/ 아래.
- gold 열(PASS·FAIL·UNVERIFIABLE)과 reviewer_note 열은 비워 둔다. 채운 파일은 tools/sample_size_study.py --gold 로 그대로 읽는다.
- 저장된 판정·사유는 바꾸지 않고 그대로 옮긴다. 원인 유형(cause)은 저장된 j5_outcome 의 code·세부 구분이다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.run_summary import EMPTY_OUTPUT_HOLD_POLICY, evaluation_method, question_outcomes  # noqa: E402

EXPORT_VERSION = "review-export-v3-held"   # v3: --held 에 빈 출력 요구 질문(질문 전체 보류, 10-19) · v2: --held 보류 목록(기준 무관, 심판 갈림·J5 확인 실패 사유), J8 선택 가능
LIMITS = {"question": 120, "answer": 200}
COLUMNS = ("system", "case_id", "conversation_id", "turn", "criterion", "decision", "cause", "cause_label", "factual_error_confirmed",
           "reason_code", "judge_reason", "judge_original_choice", "judge_original_reason", "search_calls", "search_limit",
           "failed_sources", "claims", "question", "answer", "text_scope", "gold", "reviewer_note")


def _fingerprint(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _text(value: str, kind: str, scope: str) -> str:
    value = value or ""
    if scope == "none":
        return _fingerprint(value)
    if scope == "excerpt" and len(value) > LIMITS[kind]:
        return value[:LIMITS[kind]] + f"… ({len(value)}자 중 {LIMITS[kind]}자, {_fingerprint(value)})"
    return value


HELD_CAUSES = {"judge_disagreement": "심판 갈림 (Jev·GPT-6 Luna·재판정이 다름)", "held": "J5 근거 부족 — 확인 못 함",
               "source_unavailable": "J5 출처 원문 확인 실패", "search_limit_reached": "J5 검색 한도", "unverified": "확인 필요",
               "empty_output_hold": "빈 출력 요구 질문 — 질문 전체 보류 (사용자 결정 10-19)"}


def is_held(decision: Dict[str, Any]) -> bool:
    """보류 — 확인하지 못해 PASS/FAIL 을 정하지 않은 판정 (semantic-hold-unverified-v3)."""
    return isinstance(decision, dict) and decision.get("status") in ("UNVERIFIED", "INCOMPLETE") and decision.get("decision") not in ("PASS", "FAIL")


def cause_of(decision: Dict[str, Any]) -> Dict[str, Any]:
    if is_held(decision):
        dual = decision.get("dual_judge") or {}
        if dual.get("outcome") == "held":
            return {"cause": "judge_disagreement", "label": HELD_CAUSES["judge_disagreement"], "confirmed": False,
                    "search": {}, "judges": f"Jev {dual.get('jev')} · Luna {dual.get('luna')} · 재판정 {dual.get('recheck')}"}
        outcome = decision.get("j5_outcome") or {}
        code = outcome.get("code") or ("incomplete" if decision.get("status") == "INCOMPLETE" else "unverified")
        return {"cause": code, "label": outcome.get("label") or HELD_CAUSES.get(code, code), "confirmed": False,
                "search": outcome.get("search") or {}}
    """원인 유형 — j5_outcome 의 code 와 세부 구분(원문 확인 실패 종류·충돌 근거·URL 접근 주체). 다른 기준은 판정 근거 유형."""
    outcome = decision.get("j5_outcome") or {}
    if outcome.get("code"):
        sub = ((outcome.get("source_failure") or {}).get("kind") or (outcome.get("contradiction") or {}).get("basis")
               or (outcome.get("citation_access") or {}).get("side"))
        sub_label = ((outcome.get("source_failure") or {}).get("label") or (outcome.get("contradiction") or {}).get("label")
                     or (outcome.get("citation_access") or {}).get("label"))
        return {"cause": outcome["code"] + (f":{sub}" if sub else ""), "label": (outcome.get("label") or "") + (f" ({sub_label})" if sub_label else ""),
                "confirmed": outcome.get("factual_error_confirmed"), "search": outcome.get("search") or {}}
    if decision.get("decision") == "FAIL":
        evidence = decision.get("fail_evidence")
        return {"cause": "fail_with_evidence" if isinstance(evidence, dict) and evidence.get("valid") else "fail",
                "label": "판정 근거가 있는 FAIL" if evidence else "FAIL", "confirmed": None, "search": {}}
    return {"cause": str(decision.get("decision") or decision.get("status") or "").lower(), "label": "", "confirmed": None, "search": {}}


def _failed_sources(decision: Dict[str, Any]) -> str:
    outcome = decision.get("j5_outcome") or {}
    rows = [f"{f.get('status')} {f.get('url')}" for f in (outcome.get("source_failure") or {}).get("access_failures") or []]
    rows += [f"{c.get('server_status') or c.get('verdict')} {c.get('url')}" for c in decision.get("citation_checks") or []
             if isinstance(c, dict) and c.get("verdict") not in (None, "supported")]
    return " | ".join(dict.fromkeys(rows))


def _claims(decision: Dict[str, Any]) -> str:
    claims = ((decision.get("fact_check") or {}).get("claims") or
              (((decision.get("policy_original_assessment") or {}).get("fact_check") or {}).get("claims")) or [])
    return " | ".join(f"[{c.get('verdict')}] {c.get('claim')}" for c in claims if isinstance(c, dict))


def export_rows(run: Dict[str, Any], criterion: str = "J5", include_pass: int = 0, text: str = "excerpt", seed: int = 0,
                held: bool = False) -> List[Dict[str, Any]]:
    """``held`` 면 기준에 관계없이 보류(확인 필요) 판정만 — (index, criterion) 쌍으로 모은다. 아니면 기준 하나의 FAIL(+PASS 대조군)."""
    rows: List[Dict[str, Any]] = []
    rng = random.Random(f"{seed}:{criterion}")
    for system, batch in sorted((run.get("evaluations") or {}).items()):
        data = (run.get("datasets") or {}).get(system) or []
        cases = batch.get("cases") or []
        records = (batch.get("jev_eval") or {}).get("records") or []
        chosen: List[tuple] = []
        if held:
            for index, record in enumerate(records):
                if not isinstance(record, dict) or record.get("status") != "SUCCESS":
                    continue
                for cid in record.get("criteria") or []:
                    if is_held((record.get("decisions") or {}).get(cid)):
                        chosen.append((index, cid))
        else:
            fails, passes = [], []
            for index, record in enumerate(records):
                if not isinstance(record, dict) or criterion not in (record.get("criteria") or []):
                    continue
                decision = (record.get("decisions") or {}).get(criterion) or {}
                (fails if decision.get("decision") == "FAIL" else passes if decision.get("decision") == "PASS" else []).append(index)
            picked = fails + (sorted(rng.sample(passes, min(include_pass, len(passes)))) if include_pass else [])
            chosen = [(index, criterion) for index in picked]
        for index, cid in chosen:
            record, case = records[index], cases[index] if index < len(cases) else {}
            row = data[index] if index < len(data) and isinstance(data[index], dict) else {}
            decision = record["decisions"][cid]
            cause = cause_of(decision)
            original = decision.get("model_original_assessment") or {}
            rows.append({"system": system, "case_id": case.get("case_id") or record.get("case_id"),
                         "conversation_id": row.get("conversation_id"), "turn": row.get("turn"), "criterion": cid,
                         "decision": decision.get("decision"), "cause": cause["cause"], "cause_label": cause["label"],
                         "factual_error_confirmed": cause["confirmed"], "reason_code": decision.get("reason_code"),
                         "judge_reason": decision.get("reason"), "judge_original_choice": original.get("choice"),
                         "judge_original_reason": original.get("reason") or cause.get("judges"), "search_calls": cause["search"].get("calls"),
                         "search_limit": cause["search"].get("limit"), "failed_sources": _failed_sources(decision),
                         "claims": _claims(decision) if text != "none" else _fingerprint(_claims(decision)),
                         "question": _text(case.get("question") or "", "question", text),
                         "answer": _text(case.get("answer") or "", "answer", text), "text_scope": text,
                         "gold": "", "reviewer_note": ""})
        if held:   # 빈 출력 요구 질문 — 판정·수집 결과와 상관없이 질문 전체가 보류라 사람이 PASS/FAIL 을 정한다 (empty-output-hold-v1)
            for index, outcome in enumerate(question_outcomes(batch, len(data) or None, data)):
                if outcome.get("hold") != "empty_output":
                    continue
                case = cases[index] if index < len(cases) else {}
                row = data[index] if index < len(data) and isinstance(data[index], dict) else {}
                collection = row.get("collection") if isinstance(row.get("collection"), dict) else {}
                status = collection.get("status", "SUCCESS")
                answer = row.get("answer") if isinstance(row.get("answer"), str) else case.get("answer") or ""
                rows.append({**{key: "" for key in COLUMNS}, "system": system, "case_id": case.get("case_id") or row.get("case_id"),
                             "conversation_id": row.get("conversation_id"), "turn": row.get("turn"), "criterion": "2-1.explicit_empty",
                             "decision": None, "cause": "empty_output_hold", "cause_label": HELD_CAUSES["empty_output_hold"],
                             "factual_error_confirmed": False, "reason_code": EMPTY_OUTPUT_HOLD_POLICY,
                             "judge_reason": (f"평가 판정 {outcome.get('judged_outcome')}" + (f" · 수집 {status}: {collection.get('error') or ''}".rstrip(": ")
                                                                                         if status != "SUCCESS" else f" · 답변 {len(answer.strip())}자")),
                             "question": _text(case.get("question") or row.get("question") or "", "question", text),
                             "answer": _text(answer, "answer", text), "text_scope": text})
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--criterion", default="J5", choices=("J1", "J2", "J3", "J4", "J5", "J6", "J7", "J8"))
    parser.add_argument("--held", action="store_true", help="기준에 관계없이 보류(확인 필요) 판정만 — 사람이 gold 에 PASS/FAIL 을 적는다")
    parser.add_argument("--include-pass", type=int, default=0, help="시스템마다 무작위 PASS 대조군 수")
    parser.add_argument("--text", choices=("excerpt", "none", "full"), default="excerpt", help="원문 공유 범위")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "results" / "review")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    run = json.loads(args.run.read_text(encoding="utf-8-sig"))
    rows = export_rows(run, args.criterion, args.include_pass, args.text, args.seed, held=args.held)
    method = evaluation_method(run)
    header = {"version": EXPORT_VERSION, "run_id": run.get("run_id"), "criterion": "held" if args.held else args.criterion, "text_scope": args.text,
              "pipeline": method["pipeline"], "criteria_version": method["criteria_version"],
              "judge_model": ((next(iter((run.get("evaluations") or {}).values()), {}).get("jev_eval") or {}).get("client") or {}).get("requested_model"),
              "created_at": run.get("created_at"), "rows": len(rows),
              "causes": {c: sum(r["cause"] == c for r in rows) for c in dict.fromkeys(r["cause"] for r in rows)}}
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.out_dir / f"{run.get('run_id')}-{'held' if args.held else args.criterion}"
    with open(stem.with_suffix(".csv"), "w", encoding="utf-8-sig", newline="") as handle:   # 엑셀이 한글을 깨지 않게 BOM
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    stem.with_suffix(".jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""), encoding="utf-8")
    stem.with_suffix(".meta.json").write_text(json.dumps(header, ensure_ascii=False, indent=1), encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({**header, "files": [str(stem.with_suffix(s)) for s in (".csv", ".jsonl", ".meta.json")]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
