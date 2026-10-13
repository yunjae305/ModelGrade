"""결과 이력 재현성 점검 — 저장된 실행의 지문·요약을 저장 데이터만으로 다시 계산해 저장값과 맞는지 본다 (API 호출 없음, 읽기만).

    python tools/history_reproducibility.py                                  # data/comparison_runs/*.json 전부
    python tools/history_reproducibility.py data/comparison_runs/<id>.json --code-recheck [--json]

검사 (같으면 '일치', 다르면 무엇이 다른지):
- 질문 세트 지문(dataset_fingerprint) = 저장된 AIONA 질문으로 다시 낸 지문
- 시스템마다 결과 해시(result_digest) = 저장된 CODE 결과로 다시 낸 해시
- 시스템마다 질문 판정 집계(combined) = 저장된 CODE·의미 판정으로 다시 낸 집계
- 시스템마다 의미 기준 집계(jev_eval.summary 의 targets·pass·fail·unverified·incomplete) = 저장된 레코드로 다시 센 값
- 평가 조건 프로필(evaluation_profile) = 저장된 배치로 다시 낸 프로필
- 같은 실행을 두 번 요약해도(run_summary·evaluation_method) 같은 값 (결정적)
--code-recheck: 저장 입력을 **현재** CODE 엔진으로 다시 검사해 FAIL 조건이 바뀌는지 (webapp.code_recheck, 저장 당시 엔진과 다르면 차이는 엔진 변경 탓).
새 코드가 저장 당시 없던 필드를 더 만들면 '저장 당시 없던 필드'로 따로 표시하고 불일치로 세지 않는다.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval.combined import combined_summary  # noqa: E402
from code_eval.engine import result_digest  # noqa: E402
from webapp.run_summary import evaluation_method, run_summary  # noqa: E402
from webapp.version_registry import evaluation_profile, fingerprint  # noqa: E402

CHECK_VERSION = "history-reproducibility-v1"
_SUMMARY_FIELDS = ("targets", "pass", "fail", "unverified", "incomplete")


def _diff(stored: Any, recomputed: Any, path: str = "") -> Dict[str, List[str]]:
    """{"changed": [경로…], "new_fields": [경로…]} — 저장값에 없던 키는 새 필드로 따로 센다."""
    out: Dict[str, List[str]] = {"changed": [], "new_fields": []}
    if isinstance(stored, dict) and isinstance(recomputed, dict):
        for key in recomputed:
            if key not in stored:
                out["new_fields"].append(f"{path}.{key}".lstrip("."))
            else:
                sub = _diff(stored[key], recomputed[key], f"{path}.{key}")
                out["changed"] += sub["changed"]
                out["new_fields"] += sub["new_fields"]
        out["changed"] += [f"{path}.{key}".lstrip(".") + " (다시 계산에 없음)" for key in stored if key not in recomputed]
    elif stored != recomputed:
        out["changed"].append(path.lstrip(".") or "(값)")
    return out


def _status(diff: Dict[str, List[str]]) -> Dict[str, Any]:
    status = "불일치" if diff["changed"] else "저장 당시 없던 필드" if diff["new_fields"] else "일치"
    return {"status": status, "changed": diff["changed"][:10], "new_fields": diff["new_fields"][:10]}


def _semantic_counts(records: List[Dict[str, Any]], names: List[str]) -> Dict[str, Dict[str, int]]:
    """jev_eval.summarize_records 와 같은 셈 (대상 = 적용 기준, PASS·FAIL·확인 필요·미완료)."""
    out = {cid: {f: 0 for f in _SUMMARY_FIELDS} for cid in names}
    for record in records:
        if not isinstance(record, dict):
            continue
        for cid in record.get("criteria") or []:
            out.setdefault(cid, {f: 0 for f in _SUMMARY_FIELDS})["targets"] += 1
        for cid, decision in (record.get("decisions") or {}).items():
            bucket = out.setdefault(cid, {f: 0 for f in _SUMMARY_FIELDS})
            if not isinstance(decision, dict):
                continue
            if decision.get("decision") in ("PASS", "FAIL"):
                bucket[decision["decision"].lower()] += 1
            elif decision.get("status") == "UNVERIFIED":
                bucket["unverified"] += 1
            elif decision.get("status") == "INCOMPLETE":
                bucket["incomplete"] += 1
    return out


def _safe(compute) -> Dict[str, Any]:
    try:
        return _status(compute())
    except (KeyError, TypeError, ValueError, IndexError) as exc:   # 저장 형식이 달라 다시 계산하지 못함 — 불일치로 세지 않고 따로 표시
        return {"status": "다시 계산할 수 없음", "changed": [f"{type(exc).__name__}: {exc}"[:120]], "new_fields": []}


def check_run(run: Dict[str, Any], *, code_recheck: bool = False) -> Dict[str, Any]:
    checks: Dict[str, Any] = {}
    aiona = (run.get("datasets") or {}).get("aiona") or []
    if run.get("dataset_fingerprint"):
        checks["dataset_fingerprint"] = _safe(lambda: _diff(run["dataset_fingerprint"], fingerprint(aiona)))
    for system, batch in sorted((run.get("evaluations") or {}).items()):
        if not isinstance(batch, dict) or batch.get("live"):
            continue
        cases = batch.get("cases") or []
        if batch.get("result_digest"):
            checks[f"{system}.result_digest"] = _safe(lambda: _diff(batch["result_digest"], result_digest(cases)))
        if isinstance(batch.get("combined"), dict):
            checks[f"{system}.combined"] = _safe(lambda: _diff(batch["combined"], combined_summary(copy.deepcopy(batch))))
        semantic = batch.get("jev_eval") or {}
        stored = {cid: {f: part.get(f) for f in _SUMMARY_FIELDS if f in part}
                  for cid, part in (semantic.get("summary") or {}).items() if isinstance(part, dict)}
        if stored:
            again = _semantic_counts(semantic.get("records") or [], list(stored))
            checks[f"{system}.jev_eval.summary"] = _status(_diff(stored, {cid: {f: again[cid][f] for f in stored[cid]} for cid in stored}))
    if isinstance(run.get("evaluation_profile"), dict):
        checks["evaluation_profile"] = _safe(lambda: _diff(run["evaluation_profile"], evaluation_profile(run)))
    first, second = json.dumps([run_summary(run), evaluation_method(run)], sort_keys=True, ensure_ascii=False), \
        json.dumps([run_summary(copy.deepcopy(run)), evaluation_method(copy.deepcopy(run))], sort_keys=True, ensure_ascii=False)
    checks["run_summary_deterministic"] = {"status": "일치" if first == second else "불일치", "changed": [], "new_fields": []}
    report = {"run_id": run.get("run_id"), "created_at": run.get("created_at"), "checks": checks,
              "mismatches": sum(1 for c in checks.values() if c["status"] == "불일치")}
    if code_recheck:
        from webapp.code_recheck import code_recheck as recheck
        result = recheck(run)
        report["code_recheck"] = {"engine": result.get("engine"), "providers": {
            p: {"stored_engine": part.get("stored_engine"), "same_engine": part.get("same_engine"),
                "changed_cases": len(part.get("cases") or {})}
            for p, part in sorted((result.get("providers") or {}).items()) if isinstance(part, dict)}}
    return report


def render(reports: List[Dict[str, Any]]) -> str:
    lines = [f"결과 이력 재현성 점검 ({CHECK_VERSION}, API 호출 없음, 저장 파일은 읽기만)"]
    for report in reports:
        lines.append(f"\n[{report['run_id']}] {report['created_at']} — 불일치 {report['mismatches']}건")
        for name, check in report["checks"].items():
            extra = (" · 다름: " + ", ".join(check["changed"])) if check["changed"] else (
                " · 새 필드: " + ", ".join(check["new_fields"]) if check["new_fields"] else "")
            lines.append(f"  {check['status']:8s} {name}{extra}")
        if "code_recheck" in report:
            lines.append(f"  CODE 현재 엔진 재검사: {json.dumps(report['code_recheck'], ensure_ascii=False)[:300]}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="*", type=Path)
    parser.add_argument("--code-recheck", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    paths = args.runs or sorted((ROOT / "data" / "comparison_runs").glob("*.json"))
    reports = [check_run(json.loads(p.read_text(encoding="utf-8-sig")), code_recheck=args.code_recheck) for p in paths]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(reports, ensure_ascii=False, indent=1) if args.json else render(reports))
    return 1 if any(r["mismatches"] for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
