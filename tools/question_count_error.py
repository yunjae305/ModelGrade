"""질문 수 N 에 따른 점수 오차·기준 커버·순위 안정 — 끝난 실행 기록만으로(AI 호출 없음) 10/15 보고(CODE 평가·세 모델)와 같은 방법을 최종 결과(CODE+의미·네 모델)에 다시 적용한다.

- 점수 오차: 326개 결과에서 질문 N개를 복원 추출로 뽑아 통과율을 다시 세기를 draws 번 반복 → 95% 구간의 반폭(±%p). "같은 개수의 다른 질문으로 다시 하면 점수가 위아래로 얼마나 달라지나".
- 기준 커버: N개를 비복원으로 뽑았을 때 64개 기준(질문으로 셀 수 있는 것) 중 1·2·10·20번 이상 평가되는 기준 수(평균). N > 세트 크기면 없음.
- 순위: 그 N개만으로 매긴 순위가 전체 순위와 같은 비율(네 모델 전부 / 1·2위 두 모델).
통과율 = PASS 질문 수 / N (보류는 PASS 아님). 대화 1개 = 질문 1개.

    python -X utf8 tools/question_count_error.py <run_id> [--sizes 10,25,50,78,100,150,200,250,300,326,400,500] [--draws 3000]
    → 참고자료/최소평가/질문수_오차_<run_id>.json (발표 08장이 읽음)
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from webapp import question_generation as qg  # noqa: E402
import question_gradient as qgr  # noqa: E402
import min_eval_list as mel  # noqa: E402

DEFAULT_SIZES = "10,25,50,78,100,150,200,250,300,326,400,500"
LEVELS = (1, 2, 10, 20)


def half_width(values):
    values = sorted(values)
    lo = values[int(0.025 * (len(values) - 1))]
    hi = values[int(0.975 * (len(values) - 1))]
    return (hi - lo) / 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--sizes", default=DEFAULT_SIZES)
    ap.add_argument("--draws", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    run = json.loads((ROOT / "data" / "comparison_runs" / f"{args.run_id}.json").read_text(encoding="utf-8"))
    set_id = run["question_set"]["set_id"]
    groups = json.loads((qg.STORE / set_id / "questions.json").read_text(encoding="utf-8"))
    applied = mel.applied_by_question(groups, qg.check(groups))
    universe = set(qg.target_counts(qg.check(groups)).keys())
    verdicts = qgr.verdicts_from_run(run)
    ids = sorted(applied)
    total = len(ids)
    providers = list(verdicts)
    full = {p: sum(1 for q in ids if verdicts[p][q] == "PASS") / total for p in providers}
    rank = sorted(providers, key=lambda p: -full[p])
    rng = random.Random(args.seed)
    rows = []
    for n in sorted({int(x) for x in args.sizes.split(",") if x.strip()}):
        rates = {p: [] for p in providers}
        same_all = same_top2 = 0
        cover = Counter()
        for _ in range(args.draws):
            sample = [rng.choice(ids) for _ in range(n)]
            r = {p: sum(1 for q in sample if verdicts[p][q] == "PASS") / n for p in providers}
            for p in providers:
                rates[p].append(r[p])
            rk = sorted(providers, key=lambda p: -r[p])
            same_all += rk == rank
            same_top2 += r[rank[0]] > r[rank[1]]
            if n <= total:
                c = Counter(t for q in rng.sample(ids, n) for t in applied[q])
                for lv in LEVELS:
                    cover[lv] += sum(1 for t in universe if c[t] >= lv)
        row = {"n": n, "error_pp": {p: round(half_width(rates[p]) * 100, 1) for p in providers},
               "rank_all": round(same_all / args.draws, 3), "rank_top2": round(same_top2 / args.draws, 3),
               "criteria": {str(lv): round(cover[lv] / args.draws, 1) for lv in LEVELS} if n <= total else None}
        row["error_max_pp"] = max(row["error_pp"].values())
        rows.append(row)
        crit = " · ".join(f"{lv}번↑ {row['criteria'][str(lv)]:.0f}" for lv in LEVELS) if row["criteria"] else "—"
        print(f"{n:4d} | 오차 " + " ".join(f"{p} ±{row['error_pp'][p]:.1f}" for p in providers) + f" | 최대 ±{row['error_max_pp']:.1f} | 순위 전부 {row['rank_all']:.0%} · 1·2위 {row['rank_top2']:.0%} | 기준 {crit}")
    out = ROOT / "참고자료" / "최소평가" / f"질문수_오차_{args.run_id}.json"
    out.write_text(json.dumps({"run": args.run_id, "set": set_id, "total": total, "draws": args.draws, "providers": providers,
                               "full": {p: round(full[p], 4) for p in providers}, "rank": rank, "universe": len(universe), "rows": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print("전체 통과율:", " · ".join(f"{p} {full[p]:.1%}" for p in rank), "| 저장:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
