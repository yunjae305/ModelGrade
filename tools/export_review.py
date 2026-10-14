"""사람 정답 검수용 추출 — 저장된 비교 실행의 기준 판정(기본 J5 FAIL)을 원인 유형별로 CSV·JSONL 로 낸다 (API 호출 없음, 실행 파일은 읽기만).

    python tools/export_review.py data/comparison_runs/<run>.json                       # J5 FAIL → results/review/<run>-J5.csv·.jsonl
    python tools/export_review.py <run>.json --criterion J3 --include-pass 5           # J3 FAIL 전부 + 시스템마다 PASS 5개 (대조군)
    python tools/export_review.py <run>.json --text none                                # 원문 없이 지문(sha256 앞 12자)만

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

from webapp.run_summary import evaluation_method  # noqa: E402

EXPORT_VERSION = "review-export-v1"
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


def cause_of(decision: Dict[str, Any]) -> Dict[str, Any]:
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


def export_rows(run: Dict[str, Any], criterion: str = "J5", include_pass: int = 0, text: str = "excerpt", seed: int = 0) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    rng = random.Random(f"{seed}:{criterion}")
    for system, batch in sorted((run.get("evaluations") or {}).items()):
        data = (run.get("datasets") or {}).get(system) or []
        cases = batch.get("cases") or []
        records = (batch.get("jev_eval") or {}).get("records") or []
        fails, passes = [], []
        for index, record in enumerate(records):
            if not isinstance(record, dict) or criterion not in (record.get("criteria") or []):
                continue
            decision = (record.get("decisions") or {}).get(criterion) or {}
            (fails if decision.get("decision") == "FAIL" else passes if decision.get("decision") == "PASS" else []).append(index)
        chosen = fails + (sorted(rng.sample(passes, min(include_pass, len(passes)))) if include_pass else [])
        for index in chosen:
            record, case = records[index], cases[index] if index < len(cases) else {}
            row = data[index] if index < len(data) and isinstance(data[index], dict) else {}
            decision = record["decisions"][criterion]
            cause = cause_of(decision)
            original = decision.get("model_original_assessment") or {}
            rows.append({"system": system, "case_id": case.get("case_id") or record.get("case_id"),
                         "conversation_id": row.get("conversation_id"), "turn": row.get("turn"), "criterion": criterion,
                         "decision": decision.get("decision"), "cause": cause["cause"], "cause_label": cause["label"],
                         "factual_error_confirmed": cause["confirmed"], "reason_code": decision.get("reason_code"),
                         "judge_reason": decision.get("reason"), "judge_original_choice": original.get("choice"),
                         "judge_original_reason": original.get("reason"), "search_calls": cause["search"].get("calls"),
                         "search_limit": cause["search"].get("limit"), "failed_sources": _failed_sources(decision),
                         "claims": _claims(decision) if text != "none" else _fingerprint(_claims(decision)),
                         "question": _text(case.get("question") or "", "question", text),
                         "answer": _text(case.get("answer") or "", "answer", text), "text_scope": text,
                         "gold": "", "reviewer_note": ""})
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--criterion", default="J5", choices=("J1", "J2", "J3", "J4", "J5", "J6", "J7"))
    parser.add_argument("--include-pass", type=int, default=0, help="시스템마다 무작위 PASS 대조군 수")
    parser.add_argument("--text", choices=("excerpt", "none", "full"), default="excerpt", help="원문 공유 범위")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "results" / "review")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    run = json.loads(args.run.read_text(encoding="utf-8-sig"))
    rows = export_rows(run, args.criterion, args.include_pass, args.text, args.seed)
    method = evaluation_method(run)
    header = {"version": EXPORT_VERSION, "run_id": run.get("run_id"), "criterion": args.criterion, "text_scope": args.text,
              "pipeline": method["pipeline"], "criteria_version": method["criteria_version"],
              "judge_model": ((next(iter((run.get("evaluations") or {}).values()), {}).get("jev_eval") or {}).get("client") or {}).get("requested_model"),
              "created_at": run.get("created_at"), "rows": len(rows),
              "causes": {c: sum(r["cause"] == c for r in rows) for c in dict.fromkeys(r["cause"] for r in rows)}}
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.out_dir / f"{run.get('run_id')}-{args.criterion}"
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
