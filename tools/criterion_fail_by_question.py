"""기준별 FAIL — 질문 단위(모델마다 /326)로 센다 (AI 호출 없음). 발표 06장 '기준별 FAIL 률' 표와 결과 HTML 이 읽는다.

- 적용 질문: 그 모델에서 이 기준이 판정(PASS/FAIL)된 질문 수 (대화는 어느 턴에든 있으면 1개).
- FAIL 질문: 그 모델에서 이 기준이 어느 턴에서든 FAIL 인 질문 수.
- 률: 모델마다 FAIL 질문 / 적용 질문을 구해 네 모델 평균 (모델 합산 금지 — 사용자 규칙).
- 빈 출력 요구 질문(질문 전체 보류)은 서버와 같이 뺀다. CODE 검사는 보완 판정(code_completion)을 반영한다.

    python -X utf8 tools/criterion_fail_by_question.py <run_id>
    → 참고자료/최소평가/criterion_fail_rates_<run_id>.json (기존 criterion_fail_rates_326.json 과 같은 키 + applied_by_model)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval import criteria_registry as reg  # noqa: E402
from code_eval.common_adjudication import resolved_case  # noqa: E402
from webapp.run_summary import empty_output_holds  # noqa: E402

J_NAMES = {"J1": "요청 충실", "J2": "지시 준수", "J3": "일관성·논리", "J4": "완결성", "J5": "DATA 신뢰도", "J6": "멀티턴 맥락 유지", "J7": "롱컨텍스트", "J8": "첨부파일 활용"}
CODE_NAMES = {u[0]: u[1] for u in reg.CODE_UNITS}
qid = lambda cid: int(str(cid).split("_T")[0][1:])


def main(run_id: str) -> int:
    run = json.loads((ROOT / "data" / "comparison_runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    held = {p: set(v) for p, v in empty_output_holds(run).items()}
    providers = list(run["providers"])
    applied = {p: defaultdict(set) for p in providers}   # cid -> {질문 id}
    failed = {p: defaultdict(set) for p in providers}
    for p in providers:
        batch = run["evaluations"][p]
        records = (batch.get("jev_eval") or {}).get("records") or []
        for i, case in enumerate(batch.get("cases") or []):
            if not isinstance(case, dict) or case.get("case_status") != "EVALUATED" or i in held[p]:
                continue
            q = qid(case["case_id"])
            record = records[i] if i < len(records) and isinstance(records[i], dict) else {}
            try:
                case = resolved_case(case, record)
            except (KeyError, TypeError, IndexError):
                pass
            for row in case.get("results") or []:
                if row.get("area") != "common" or not row.get("counted") or row.get("verdict") not in ("PASS", "FAIL"):
                    continue
                cid = str(row.get("check_id") or "").split(":")[0]
                applied[p][cid].add(q)
                if row["verdict"] == "FAIL":
                    failed[p][cid].add(q)
            if record.get("status") == "SUCCESS":
                for cid, d in (record.get("decisions") or {}).items():
                    dec = (d or {}).get("decision") if isinstance(d, dict) else None
                    if dec in ("PASS", "FAIL"):
                        applied[p][cid].add(q)
                        if dec == "FAIL":
                            failed[p][cid].add(q)
    cids = sorted({c for p in providers for c in applied[p]}, key=lambda c: (not c.startswith("J"), c))
    rows = []
    for cid in cids:
        ap = {p: len(applied[p][cid]) for p in providers}
        fl = {p: len(failed[p][cid]) for p in providers}
        rates = [fl[p] / ap[p] for p in providers if ap[p]]
        rows.append({"id": cid, "name": J_NAMES.get(cid) or CODE_NAMES.get(cid, ""), "applied": max(ap.values()), "applied_by_model": ap,
                     "fail": sum(fl.values()), "by_model": fl, "rate": round(sum(rates) / len(rates), 4) if rates else 0.0, "unit": "question-per-model"})
    rows.sort(key=lambda r: (-r["rate"], -r["applied"]))
    out = ROOT / "참고자료" / "최소평가" / f"criterion_fail_rates_{run_id}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in rows[:14]:
        print(f"{r['id']:28s} 적용 {r['applied']:3d} | " + " ".join(f"{p} {r['by_model'][p]}/{r['applied_by_model'][p]}" for p in providers) + f" | 평균 FAIL 률 {r['rate']:.0%}")
    print("FAIL 0:", [r["id"] for r in rows if r["fail"] == 0])
    print("저장:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "8c61f117e661"))
