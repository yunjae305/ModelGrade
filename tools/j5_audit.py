"""J5 점검 — 저장된 판정의 일관성(consistency)과 적용 범위(scope)를 본다 (API 호출 없음, 실행 파일은 읽기만).

    python tools/j5_audit.py consistency data/comparison_runs/<id>.json      # j5_outcome·사유 코드·상태가 서로 맞는지
    python tools/j5_audit.py scope data/comparison_runs/<id>.json [--json]   # items 가 J5 를 묻지 않는 답변에 검증할 주장이 있는지

consistency 규칙 (어긋나면 위반으로 센다):
- PASS ↔ verified · NOT_APPLICABLE ↔ not_applicable · INCOMPLETE ↔ 판정 없음 + 미완료 코드(search_unavailable·search_error·fetch_error·incomplete)
- FAIL 이면 FAIL 코드, factual_error_confirmed 는 contradicted 일 때만
- SOURCE_ORIGINAL_UNAVAILABLE ↔ source_unavailable, EVALUATION_EVIDENCE_INSUFFICIENT → insufficient_evidence·search_limit_reached
- 저장된 j5_outcome 을 지금 규칙으로 다시 계산해도 같은 code (판정 값은 바꾸지 않고 비교만)
scope: 적용 규칙이 셋이다 — legacy 규칙(코드 밖 수치·날짜·통계, CLI 평가자) · legacy API 평가자의 LLM 선별(모든 답변) ·
items(질문의 사실 질의 표현 J5-1, 질문 값 J5-2, 확인 언급 J5-3). items 가 묻지 않는 답변마다 legacy 평가자가 뽑은 외부 사실 주장을
'코드 블록 안' 과 '본문'(문장 속 인라인 코드 포함) 으로 나누고, 본문 주장을 기술 사실(라이브러리·언어 동작·복잡도) · 수치·날짜 · 그 밖으로 나눈다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval.jev_applicability import _answer_data_evidence  # noqa: E402
from code_eval.semantic_policy import items_j5_outcome, j5_outcome  # noqa: E402

AUDIT_VERSION = "j5-audit-v1"
PENDING = {"search_unavailable", "search_error", "fetch_error", "incomplete"}
FAIL_CODES = {"source_unavailable", "insufficient_evidence", "search_limit_reached", "citation_inaccessible", "source_mismatch",
              "contradicted", "process_misrepresented", "unclassified"}
_TECH = re.compile(r"`[^`]+`|\(\)|O\s*\(|\$O\(|라이브러리|모듈|함수|메서드|표준|JOIN|SELECT|NULL|인덱스|정렬|복잡도|파이썬|Python|SQL|HTML|CSS|JSON|정규식")
_NUMERIC = re.compile(r"\d")


def consistency(record: Dict[str, Any]) -> List[str]:
    """J5 레코드 하나의 위반 목록 (J5 가 없거나 j5_outcome 이 없으면 빈 목록)."""
    j5 = (record.get("decisions") or {}).get("J5")
    outcome = (j5 or {}).get("j5_outcome") if isinstance(j5, dict) else None
    if not isinstance(outcome, dict):
        return []
    problems, code, decision, status = [], outcome.get("code"), j5.get("decision"), j5.get("status")
    if decision == "PASS" and code != "verified":
        problems.append(f"PASS 인데 code={code}")
    if code == "verified" and decision != "PASS":
        problems.append(f"verified 인데 decision={decision}")
    if status == "NOT_APPLICABLE" and code != "not_applicable":
        problems.append(f"해당 없음인데 code={code}")
    if status == "INCOMPLETE" and (decision is not None or code not in PENDING):
        problems.append(f"미완료인데 decision={decision}, code={code}")
    if code in PENDING and status != "INCOMPLETE":
        problems.append(f"미완료 코드 {code} 인데 status={status}")
    if decision == "FAIL" and code not in FAIL_CODES:
        problems.append(f"FAIL 인데 code={code}")
    if bool(outcome.get("factual_error_confirmed")) != (code == "contradicted"):
        problems.append("factual_error_confirmed 와 code 가 맞지 않음")
    reason = j5.get("reason_code")
    if reason == "SOURCE_ORIGINAL_UNAVAILABLE" and code != "source_unavailable":
        problems.append(f"SOURCE_ORIGINAL_UNAVAILABLE 인데 code={code}")
    if reason == "EVALUATION_EVIDENCE_INSUFFICIENT" and code not in ("insufficient_evidence", "search_limit_reached"):
        problems.append(f"EVALUATION_EVIDENCE_INSUFFICIENT 인데 code={code}")
    again = items_j5_outcome(record) if record.get("pipeline") == "items" else j5_outcome(record)
    if again and again.get("code") != code:
        problems.append(f"다시 계산하면 {again.get('code')} (저장 {code})")
    return problems


def audit_consistency(run: Dict[str, Any]) -> Dict[str, Any]:
    counts, violations = Counter(), []
    for system, batch in sorted((run.get("evaluations") or {}).items()):
        for index, record in enumerate((batch.get("jev_eval") or {}).get("records") or []):
            if not isinstance(record, dict):
                continue
            j5 = (record.get("decisions") or {}).get("J5")
            if not isinstance(j5, dict):
                continue
            outcome = j5.get("j5_outcome") if isinstance(j5.get("j5_outcome"), dict) else {}
            counts[(j5.get("decision") or j5.get("status") or "-", outcome.get("code") or "기록 없음")] += 1
            for problem in consistency(record):
                violations.append({"system": system, "case_id": record.get("case_id"), "problem": problem})
    return {"version": AUDIT_VERSION, "run_id": run.get("run_id"), "records": sum(counts.values()),
            "by_decision_and_code": {f"{d} · {c}": n for (d, c), n in sorted(counts.items())}, "violations": violations}


_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n.*?^[ \t]{0,3}\1[ \t]*$", re.M | re.S)
_MARKUP = re.compile(r"[*_`$]")


def _claim_place(claim: str, answer: str) -> str:
    """주장이 답변의 코드 블록(펜스) 안에 있는지 본문에 있는지. 본문 문장 속 인라인 코드(`LEFT JOIN` 등)는 본문으로 본다.
    강조·인라인 코드 표시를 지운 글자로 위치를 찾는다 (평가자가 표시를 빼고 옮긴 주장도 찾게)."""
    probe = _MARKUP.sub("", claim or "").strip()[:20]
    if not probe:
        return "unknown"
    plain, offsets = [], []
    for i, ch in enumerate(answer or ""):
        if not _MARKUP.match(ch):
            plain.append(ch)
            offsets.append(i)
    at = "".join(plain).find(probe)
    if at < 0:
        return "unknown"
    position = offsets[at]
    return "code" if any(m.start() <= position < m.end() for m in _FENCE.finditer(answer or "")) else "prose"


def _claim_kind(claim: str) -> str:
    if _TECH.search(claim or ""):
        return "기술 사실"
    if _NUMERIC.search(claim or ""):
        return "수치·날짜"
    return "그 밖"


def scope(run: Dict[str, Any]) -> Dict[str, Any]:
    """items 가 J5 를 묻지 않는 답변(legacy 는 물었던 답변)의 주장 분류 — legacy 평가자가 뽑은 주장 기준(사람 검수 전)."""
    from tools.pipeline_compare import _contexts_for, _conversation_args, answers
    from code_eval.jev_applicability import sub_applicability
    rows = answers(run)
    cases, contexts = _contexts_for(rows)
    out, totals = [], Counter()
    for i, a in enumerate(rows):
        record = a["record"]
        j5 = (record.get("decisions") or {}).get("J5")
        if not isinstance(j5, dict) or "J5" not in (record.get("criteria") or []) or cases[i].get("case_status") != "EVALUATED":
            continue
        history, conversation = _conversation_args(contexts, i)
        subs = sub_applicability(a["question"], a["answer"], conversation=conversation,
                                 answer_tools="unknown" if a["system"] == "aiona" else "none", history=history)
        if any(sub.get("items") for sid, sub in subs.items() if sid.startswith("J5-")):
            continue   # items 도 J5 를 묻는다
        fact = j5.get("fact_check") or (j5.get("policy_original_assessment") or {}).get("fact_check") or {}
        claims = [c for c in fact.get("claims") or [] if isinstance(c, dict) and c.get("scope") in (None, "external_fact")]
        places = Counter(_claim_place(c.get("claim"), a["answer"]) for c in claims)
        prose = [c for c in claims if _claim_place(c.get("claim"), a["answer"]) == "prose"]
        kinds = Counter(_claim_kind(c.get("claim")) for c in prose)
        rule = bool(_answer_data_evidence(a["answer"]))
        kind = ("should_apply_prose_claims" if prose else "code_only_claims" if claims else "no_claims")
        totals[kind] += 1
        totals["legacy_fail"] += j5.get("decision") == "FAIL"
        out.append({"system": a["system"], "case_id": a["case_id"], "legacy_decision": j5.get("decision"), "kind": kind,
                    "claims": len(claims), "places": dict(places), "prose_kinds": dict(kinds),
                    "legacy_rule_applies": rule, "j5_1_reason": (subs.get("J5-1") or {}).get("not_applicable"),
                    "prose_claim_verdicts": dict(Counter(str(c.get("verdict")) for c in prose))})
    return {"version": AUDIT_VERSION, "run_id": run.get("run_id"), "answers_items_skips_j5": len(out), "totals": dict(totals), "rows": out}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("consistency", "scope"))
    parser.add_argument("run", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    run = json.loads(args.run.read_text(encoding="utf-8-sig"))
    report = audit_consistency(run) if args.command == "consistency" else scope(run)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.json or args.command == "consistency":
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=1))
        for row in report["rows"]:
            if row["legacy_decision"] == "FAIL" or row["kind"] == "should_apply_prose_claims":
                print(f"{row['system']:6s} {row['case_id']:8s} {row['legacy_decision']:4s} {row['kind']:26s} 주장 {row['claims']} {row['places']} "
                      f"본문 {row['prose_kinds']} 판정 {row['prose_claim_verdicts']} legacy 규칙 {row['legacy_rule_applies']}")
    return 1 if args.command == "consistency" and report["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
