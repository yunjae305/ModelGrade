"""기준별 FAIL — 질문 단위(모델마다 /326)로 센다 (AI 호출 없음). 발표 06장 '기준별 FAIL' 표와 결과 HTML 이 읽는다.

- 분모(적용 질문): **질문에서 정해지는** 그 기준의 적용 질문 수 — 질문 세트를 코드 규칙으로 읽은 결과(질문으로 셀 수 있는 64개 기준,
  tools/min_eval_list.applied_by_question)라 네 모델이 같다. 의미 기준은 J 묶음(J1~J8)으로 합친다(그 묶음의 항목 하나라도 걸리면 적용).
  질문으로 셀 수 없는 검사(답변 구조로만 정해지는 7개 등)는 네 모델 중 하나라도 판정·건너뜀 행이 있는 질문의 합집합을 분모로 쓴다.
- 분자(FAIL 질문): 그 모델에서 이 기준이 어느 턴에서든 FAIL 인 질문 수. 나머지는 PASS 또는 보류(판정 못 함·확인 못 함·건너뜀·빈 출력 보류) — 보류는 분모에 남긴다(사용자 규칙).
- 률: 모델마다 FAIL / 적용 을 구해 네 모델 평균 (모델 합산 금지). CODE 검사는 보완 판정(code_completion)을 반영한다.

    python -X utf8 tools/criterion_fail_by_question.py <run_id>
    → 참고자료/최소평가/criterion_fail_rates_<run_id>.json
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from code_eval import criteria_registry as reg  # noqa: E402
from code_eval.common_adjudication import resolved_case  # noqa: E402
from webapp import question_generation as qg  # noqa: E402
import min_eval_list as mel  # noqa: E402

J_NAMES = {"J1": "요청 충실", "J2": "지시 준수", "J3": "일관성·논리", "J4": "완결성", "J5": "DATA 신뢰도", "J6": "멀티턴 맥락 유지", "J7": "롱컨텍스트", "J8": "첨부파일 활용"}
CODE_NAMES = {u[0]: u[1] for u in reg.CODE_UNITS}
qid = lambda cid: int(str(cid).split("_T")[0][1:])
crit_of = lambda item_id: item_id.split("-")[0] if item_id.startswith("J") else item_id   # J2-2.a → J2, CODE id 는 그대로


def main(run_id: str) -> int:
    run = json.loads((ROOT / "data" / "comparison_runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    providers = list(run["providers"])
    set_id = run["question_set"]["set_id"]
    groups = json.loads((qg.STORE / set_id / "questions.json").read_text(encoding="utf-8"))
    total = len(groups)
    # 1) 질문에서 정해지는 적용 질문 (모델 공통)
    applied_q: dict = defaultdict(set)
    for q, items in mel.applied_by_question(groups, qg.check(groups)).items():
        for it in items:
            applied_q[crit_of(it)].add(q)
    # 2) 모델별 판정 — FAIL 질문, 그리고 분모 보조용(질문으로 셀 수 없는 검사)의 '행이 있는 질문'
    failed = {p: defaultdict(set) for p in providers}
    seen = {p: defaultdict(set) for p in providers}
    for p in providers:
        batch = run["evaluations"][p]
        records = (batch.get("jev_eval") or {}).get("records") or []
        for i, case in enumerate(batch.get("cases") or []):
            if not isinstance(case, dict) or case.get("case_status") != "EVALUATED":
                continue
            q = qid(case["case_id"])
            record = records[i] if i < len(records) and isinstance(records[i], dict) else {}
            try:
                case = resolved_case(case, record)
            except (KeyError, TypeError, IndexError):
                pass
            for row in case.get("results") or []:
                if row.get("area") != "common" or not row.get("counted"):
                    continue
                cid = str(row.get("check_id") or "").split(":")[0]
                seen[p][cid].add(q)
                if row.get("verdict") == "FAIL":
                    failed[p][cid].add(q)
            for sk in (case.get("meta") or {}).get("skipped_conditions") or []:
                seen[p][str(sk.get("check_id") or "").split(":")[0]].add(q)
            if record.get("status") == "SUCCESS":
                for cid, d in (record.get("decisions") or {}).items():
                    seen[p][cid].add(q)
                    if isinstance(d, dict) and d.get("decision") == "FAIL":
                        failed[p][cid].add(q)
    cids = sorted({c for p in providers for c in seen[p]} | set(applied_q), key=lambda c: (not c.startswith("J"), c))
    rows = []
    for cid in cids:
        denom_q = applied_q.get(cid) or set().union(*(seen[p][cid] for p in providers))   # 질문으로 못 세는 검사만 합집합
        basis = "question" if cid in applied_q else "union"
        n = len(denom_q)
        fl = {p: len(failed[p][cid] & denom_q) for p in providers}
        rates = [fl[p] / n for p in providers] if n else []
        rows.append({"id": cid, "name": J_NAMES.get(cid) or CODE_NAMES.get(cid, ""), "applied": n, "applied_basis": basis,
                     "fail": sum(fl.values()), "by_model": fl, "rate": round(sum(rates) / len(rates), 4) if rates else 0.0,
                     "unit": "question-per-model, denominator from question (same for all models), holds stay in denominator"})
    rows.sort(key=lambda r: (-r["rate"], -r["applied"]))
    out = ROOT / "참고자료" / "최소평가" / f"criterion_fail_rates_{run_id}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in rows[:14]:
        print(f"{r['id']:28s} 적용 {r['applied']:3d}({r['applied_basis'][0]}) | " + " ".join(f"{p} {r['by_model'][p]}" for p in providers) + f" | 평균 FAIL 률 {r['rate']:.0%}")
    print("FAIL 0:", [r["id"] for r in rows if r["fail"] == 0])
    print(f"질문 {total}개 | 저장: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "8c61f117e661"))
