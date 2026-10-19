"""k 별 갈림 표 — 평가가 끝난 실행에서 단일 질문을 k(질문당 기준 수)별로 묶어, 네 모델 판정이 '모두 PASS / 갈림 / 모두 FAIL' 인 비율을 센다 (AI 호출 없음).
보류를 사람이 정리한 뒤(`tools/export_review.py <run>.json --held` 로 낸 CSV 의 gold 열에 PASS/FAIL 기입) ``--gold`` 로 넘기면 그 판정을 반영해 다시 센다.

    python -X utf8 tools/k_split_from_run.py 8c61f117e661
    python -X utf8 tools/k_split_from_run.py 8c61f117e661 --gold 참고자료/보류목록/8c61f117e661-held.csv

- 질문 단위(대화 1개 = 1개). 여기서는 단일 질문(긴 문서 제외)만 k 별로 묶는다.
- gold 반영 규칙: 그 (모델, 질문)의 보류 행 중 하나라도 gold=FAIL 이면 FAIL. 보류 행이 모두 gold=PASS 이고 다른 FAIL 이 없으면 PASS.
  gold 가 비었거나 UNVERIFIABLE 인 행이 남으면 그대로 보류(표에서 제외).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from webapp import question_generation as qg  # noqa: E402
import question_gradient as qgr  # noqa: E402

PROVIDERS = ("aiona", "gpt", "claude", "gemini")


def _qid(case_id: str) -> int:
    return int(str(case_id).split("_T")[0].lstrip("Q"))


def apply_gold(verdicts, gold_path: Path):
    """보류 목록 CSV(gold 열)로 질문 판정을 갱신한 사본과 반영 통계."""
    rows = list(csv.DictReader(open(gold_path, encoding="utf-8-sig")))
    held = defaultdict(list)   # (model, qid) -> [gold …]
    for r in rows:
        held[(r["system"], _qid(r["case_id"]))].append((r.get("gold") or "").strip().upper())
    out = {p: dict(v) for p, v in verdicts.items()}
    stats = {"rows": len(rows), "filled": sum(1 for r in rows if (r.get("gold") or "").strip()), "to_fail": 0, "to_pass": 0, "still_hold": 0}
    for (p, q), golds in held.items():
        if p not in out or q not in out[p] or out[p][q] != "HOLD":
            continue
        if any(g == "FAIL" for g in golds):
            out[p][q] = "FAIL"; stats["to_fail"] += 1
        elif all(g == "PASS" for g in golds):
            out[p][q] = "PASS"; stats["to_pass"] += 1
        else:
            stats["still_hold"] += 1
    return out, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--gold", help="보류 목록 CSV (gold 열에 PASS/FAIL)")
    args = ap.parse_args(argv)
    run = json.loads((ROOT / "data" / "comparison_runs" / f"{args.run_id}.json").read_text(encoding="utf-8"))
    set_id = run["question_set"]["set_id"]
    groups = json.loads((qg.STORE / set_id / "questions.json").read_text(encoding="utf-8"))
    report = qg.check(groups)
    kmap = qg.k_var_by_group(groups, report)
    verdicts = qgr.verdicts_from_run(run)
    stats = None
    if args.gold:
        verdicts, stats = apply_gold(verdicts, Path(args.gold))
    providers = [p for p in PROVIDERS if p in verdicts]
    singles = [g["id"] for g in groups if len(g["turns"]) == 1 and not g["turns"][0].get("reference")]
    by_k = defaultdict(lambda: [0, 0, 0, 0])
    excluded = 0
    for q in singles:
        vs = [verdicts[p][q] for p in providers]
        if "HOLD" in vs:
            excluded += 1; continue
        k = kmap[q]["k"]; row = by_k[k]; row[0] += 1
        if all(v == "PASS" for v in vs):
            row[1] += 1
        elif all(v == "FAIL" for v in vs):
            row[3] += 1
        else:
            row[2] += 1
    lines = [f"# k 별 갈림 — 실행 {args.run_id} ({'보류 정리 반영' if args.gold else '보류 있는 질문 제외'})", "",
             f"단일 질문 {len(singles)}개(긴 문서 제외) 중 네 모델({' · '.join(providers)}) 판정이 모두 확정된 {len(singles) - excluded}개. 보류가 남은 질문 {excluded}개는 제외.", ""]
    if stats:
        lines += [f"보류 목록 반영: 행 {stats['rows']} · gold 기입 {stats['filled']} · 질문 판정 FAIL 로 {stats['to_fail']} · PASS 로 {stats['to_pass']} · 아직 보류 {stats['still_hold']}", ""]
    lines += ["| k | 질문 수 | 모두 PASS | 갈림 | 모두 FAIL |", "|---|---|---|---|---|"]
    for k in sorted(by_k):
        n, a, s, f = by_k[k]
        lines.append(f"| {k} | {n} | {a / n * 100:.0f}% | **{s / n * 100:.0f}%** | {f / n * 100:.0f}% |")
    lines += ["", "모델별 질문 판정 (/ 326): " + " · ".join(
        f"{p} PASS {sum(1 for v in verdicts[p].values() if v == 'PASS')}·FAIL {sum(1 for v in verdicts[p].values() if v == 'FAIL')}·보류 {sum(1 for v in verdicts[p].values() if v == 'HOLD')}" for p in providers)]
    text = "\n".join(lines) + "\n"
    out = ROOT / "참고자료" / "최소평가" / f"k별_갈림_{args.run_id}{'_보류반영' if args.gold else ''}.md"
    out.write_text(text, encoding="utf-8")
    print(text); print("저장:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
