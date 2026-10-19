"""질문 몇 개면 되는가 — 평가가 끝난 실행 기록만으로(AI 호출 없음) 질문 수 N 에 따라 (1) 기준 커버리지 (2) 모델 순위·통과율이
전체와 같아질 확률을 센다. 대화 1개 = 질문 1개. 다음 세트는 내용이 달라지므로 특정 질문 번호가 아니라 "몇 개를 어떤 순서로"를 답한다.

    python -X utf8 tools/sample_size_from_run.py <run_id> [--samples 3000] [--tol 0.03]

순서 세 가지를 비교한다:
- 기준 많은 순: 질문당 적용 기준 수(64 중) 많은 것부터 (사용자 가설)
- 무작위: 326 에서 N 개를 무작위로 (반복 추출 → 확률)
- 기준 많은 순 + 희귀 기준 보강: 기준 많은 순으로 가되, 기준이 적은 질문에만 있는 기준(빈 출력 요구 등)을 먼저 넣음
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from webapp import question_generation as qg  # noqa: E402
import question_gradient as qgr  # noqa: E402
import min_eval_list as mel  # noqa: E402


def pass_rates(verdicts, ids):
    return {p: sum(1 for q in ids if verdicts[p][q] == "PASS") / len(ids) for p in verdicts}


def ranking(rates):
    return tuple(sorted(rates, key=lambda p: -rates[p]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--samples", type=int, default=3000)
    ap.add_argument("--tol", type=float, default=0.03, help="통과율 허용 오차 (기본 ±3pp)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    run = json.loads((ROOT / "data" / "comparison_runs" / f"{args.run_id}.json").read_text(encoding="utf-8"))
    set_id = run["question_set"]["set_id"]
    groups = json.loads((qg.STORE / set_id / "questions.json").read_text(encoding="utf-8"))
    report = qg.check(groups)
    applied = mel.applied_by_question(groups, report)
    universe = set(qg.target_counts(report).keys())
    verdicts = qgr.verdicts_from_run(run)
    ids = [g["id"] for g in groups]
    full = pass_rates(verdicts, ids)
    full_rank = ranking(full)
    gaps = sorted(((full[a] - full[b]) * 100, a, b) for i, a in enumerate(full_rank) for b in full_rank[i + 1:] if full_rank.index(b) == i + 1)
    print(f"전체 {len(ids)}개 — 통과율 " + " · ".join(f"{p} {full[p] * 100:.1f}%" for p in full_rank))
    print("인접 순위 차이: " + " · ".join(f"{a}>{b} {d:.1f}pp" for d, a, b in gaps))
    print(f"기준(질문으로 셀 수 있는 것) {len(universe)}개\n")

    by_count = sorted(ids, key=lambda q: (-len(applied[q]), q))
    rarity = Counter(c for q in ids for c in applied[q])
    rare_first = sorted(ids, key=lambda q: (min((rarity[c] for c in applied[q]), default=10**6), -len(applied[q]), q))
    # 희귀 기준 보강: 적용 질문이 가장 적은 기준부터 그 기준을 가진 질문 하나씩 앞에, 나머지는 기준 많은 순
    boosted, seen = [], set()
    for c, _ in sorted(rarity.items(), key=lambda x: x[1]):
        if rarity[c] <= 12:
            best = max((q for q in ids if c in applied[q] and q not in seen), key=lambda q: len(applied[q]), default=None)
            if best is not None:
                boosted.append(best); seen.add(best)
    boosted += [q for q in by_count if q not in seen]

    def coverage_at(order, n):
        cov = Counter(c for q in order[:n] for c in applied[q])
        return sum(1 for c in universe if cov[c] >= 1), sum(1 for c in universe if cov[c] >= 3)

    Ns = [10, 17, 20, 30, 37, 40, 50, 60, 80, 100, 120, 150, 180, 200, 250, 300, 326]
    rng = random.Random(args.seed)
    print("질문 수 | 기준 커버 1회↑ (기준순 / 보강순 / 무작위 평균) | 기준 3회↑ (기준순 / 보강순 / 무작위) | 순위 일치 확률(무작위) | 1위 일치 | 통과율 ±3pp 안(무작위) | 기준순 순위 일치")
    rows = []
    for n in Ns:
        c1a, c3a = coverage_at(by_count, n)
        c1b, c3b = coverage_at(boosted, n)
        rank_ok = top_ok = within = 0
        cov1 = cov3 = 0
        for _ in range(args.samples):
            s = rng.sample(ids, n)
            r = pass_rates(verdicts, s)
            rk = ranking(r)
            rank_ok += rk == full_rank
            top_ok += rk[0] == full_rank[0]
            within += all(abs(r[p] - full[p]) <= args.tol for p in r)
            a, b = coverage_at(s, n)
            cov1 += a; cov3 += b
        kr = ranking(pass_rates(verdicts, by_count[:n])) == full_rank
        rows.append((n, c1a, c1b, cov1 / args.samples, c3a, c3b, cov3 / args.samples, rank_ok / args.samples, top_ok / args.samples, within / args.samples, kr))
        print(f"{n:4d} | {c1a:2d} / {c1b:2d} / {cov1 / args.samples:5.1f} | {c3a:2d} / {c3b:2d} / {cov3 / args.samples:5.1f} | {rank_ok / args.samples * 100:5.1f}% | {top_ok / args.samples * 100:5.1f}% | {within / args.samples * 100:5.1f}% | {'같음' if kr else '다름'}")
    out = ROOT / "참고자료" / "최소평가" / "질문수_결정_2026-10-19.json"
    out.write_text(json.dumps({"run": args.run_id, "set": set_id, "full": full, "rank": full_rank, "samples": args.samples, "tol": args.tol,
                               "rows": [dict(zip(("n", "cov1_bycount", "cov1_boosted", "cov1_random", "cov3_bycount", "cov3_boosted", "cov3_random",
                                                  "rank_match_random", "top1_match_random", "within_tol_random", "rank_match_bycount"), r)) for r in rows]},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n저장:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
