"""최소 평가 리스트 — 정밀 평가(질문 세트 전체, 예: 326개)에서 질문당 평가 기준이 많은 질문부터 골라, 적은 질문으로 평가 기준을 모두 덮는 목록을 만든다.
(사용자 생각 2026-10-18: 기준이 많은 질문부터 평가하면 적은 질문과 답변으로도 우리 평가 기준을 전부 평가할 수 있다.) 모델 호출 없음.

    python tools/min_eval_list.py <세트 id>                         # 목록만 (기본: 기준마다 1번 이상)
    python tools/min_eval_list.py <세트 id> --min 3                 # 기준마다 3번 이상
    python tools/min_eval_list.py <세트 id> --run <실행 id>          # 그 실행의 판정으로 최소 vs 정밀 결과 비교
    python tools/min_eval_list.py <세트 id> --save-set               # 최소 평가 질문 세트를 data/question_sets 에 만든다(화면에서 수집·평가 가능)

고르는 법 (욕심 방식, 기준 수 우선):
- 질문마다 적용 기준 = 코드 규칙(check)이 그 질문(대화는 모든 턴)에서 찾은 기준 중 질문으로 셀 수 있는 것(72 중 64 — 답변 구조로만 정해지는 검사 7·J5-3.a 제외).
- 남은 질문 가운데 '적용 기준 수'가 가장 많은 질문부터 본다. 아직 목표 횟수를 못 채운 기준을 하나도 더하지 못하면 건너뛴다.
- 모든 기준이 목표 횟수에 닿으면 멈춘다. 대화 1개 = 1개.
결과: 참고자료/최소평가/최소평가_리스트.md · min_eval.json (질문 id·기준 수·새로 덮은 기준), --run 이면 결과 비교 표도.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp import question_generation as qg  # noqa: E402

OUT = ROOT / "참고자료" / "최소평가"
MIN_LIST_VERSION = "min-eval-list-v1"


def _qid(case_id: str) -> int:
    return int(str(case_id).split("_T")[0][1:])


def applied_by_question(groups: List[Dict[str, Any]], report: Dict[str, Any]) -> Dict[int, set]:
    """질문(대화는 모든 턴 합집합)마다 적용되는, 질문으로 셀 수 있는 기준 id 집합."""
    countable = set(qg.target_counts(report).keys())
    applied: Dict[int, set] = defaultdict(set)
    for row in report.get("cases") or []:
        applied[_qid(row["case_id"])].update(t for t in (row.get("checks") or []) + (row.get("items") or []) if t in countable)
    return {g["id"]: applied.get(g["id"], set()) for g in groups}


def select(groups: List[Dict[str, Any]], applied: Dict[int, set], need: int = 1, universe: Optional[set] = None) -> Dict[str, Any]:
    """기준 수 많은 순으로 보며 새 기준을 더하는 질문만 고른다 — 반환 {chosen: [{id, k, new, kind}], counts, short}.
    ``universe`` 는 덮어야 할 기준 전체(기본: 질문으로 셀 수 있는 64개). 목표 횟수는 낮추지 않는다 — 세트에 모자라면 short 에 남긴다 (Codex 검토)."""
    universe = set(universe) if universe is not None else (set().union(*applied.values()) if applied else set())
    available = {t: sum(1 for s in applied.values() if t in s) for t in universe}
    target = {t: min(need, available[t]) for t in universe}   # 고르는 동안의 멈춤 조건(세트에 있는 만큼) — 보고는 need 기준
    kind = {g["id"]: ("긴 문서" if g["turns"][0].get("reference") else "대화" if len(g["turns"]) > 1 else "단일") for g in groups}
    counts: Counter = Counter()
    chosen = []
    order = sorted(applied, key=lambda q: (-len(applied[q]), q))   # 기준 수 많은 순, 같으면 번호 순
    for q in order:
        if all(counts[t] >= target[t] for t in universe):
            break
        new = sorted(t for t in applied[q] if counts[t] < target[t])
        if not new:
            continue
        chosen.append({"id": q, "k": len(applied[q]), "new": new, "kind": kind[q]})
        counts.update(applied[q])
    short = {t: (counts[t], need) for t in universe if counts[t] < need}   # 요청 횟수 기준 — 세트에 모자란 기준도 숨기지 않는다
    return {"chosen": chosen, "counts": dict(counts), "short": short, "universe": len(universe), "need": need}


def compare(run: Dict[str, Any], ids: List[int]) -> Dict[str, Any]:
    """실행의 질문별 판정으로 최소 vs 정밀(전체) 통과율·순위 비교 — run_summary 와 같은 질문·단위 규칙(보류는 분모 유지)."""
    from tools.question_gradient import verdicts_from_run
    verdicts = verdicts_from_run(run)
    out = {}
    for label, pick in (("정밀", None), ("최소", set(ids))):
        row = {}
        for p, v in verdicts.items():
            qs = [q for q in v if pick is None or q in pick]
            tally = Counter(v[q] for q in qs)
            row[p] = {"n": len(qs), "pass": tally["PASS"], "fail": tally["FAIL"], "hold": tally["HOLD"],
                      "rate": round(tally["PASS"] / len(qs), 4) if qs else None}
        row["_ranking"] = sorted((p for p in verdicts), key=lambda p: (-(row[p]["rate"] or 0), p))
        out[label] = row
    out["same_ranking"] = out["정밀"]["_ranking"] == out["최소"]["_ranking"]
    return out


def save_set(set_id: str, groups: List[Dict[str, Any]], ids: List[int], need: int) -> str:
    """최소 평가 질문 세트를 만든다 — 질문 번호는 원래 번호 그대로(정밀 결과와 맞대기 쉽게), 첨부 폴더는 복사."""
    src = qg.STORE / set_id
    new_id = "qs-" + uuid.uuid4().hex[:12]
    out = qg.STORE / new_id
    out.mkdir(parents=True)
    if (src / "files").is_dir():
        shutil.copytree(src / "files", out / "files")
    picked = [g for g in groups if g["id"] in set(ids)]
    (out / "questions.json").write_text(json.dumps(picked, ensure_ascii=False, indent=1), encoding="utf-8")
    report = qg.check(picked)
    job = {"id": new_id, "version": MIN_LIST_VERSION, "source": "min_eval", "source_name": f"최소 평가 ({len(picked)}개, 정밀 {set_id} 에서 기준 {need}번 이상)",
           "parent_set": set_id, "status": "COMPLETED", "phase": "done", "message": "완료", "created_at": datetime.now(timezone.utc).isoformat(),
           "model": None, "note": "", "usage": {}, "warnings": [], "summary": qg._summary(picked, report), "coverage": report["coverage"],
           "error": None, "target_questions": None, "weak_from_run": None, "weak_units": [], "auto_run": False, "run_id": None, "run_error": None,
           "calls": 0, "rounds": 0}
    (out / "set.json").write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    return new_id


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("set_id")
    ap.add_argument("--min", type=int, default=1, help="기준마다 채울 횟수 (기본 1)")
    ap.add_argument("--run", help="결과를 비교할 실행 id")
    ap.add_argument("--save-set", action="store_true")
    args = ap.parse_args(argv)
    groups = json.loads((qg.STORE / args.set_id / "questions.json").read_text(encoding="utf-8"))
    report = qg.check(groups)
    if report.get("errors") or report.get("undetermined"):
        raise SystemExit("질문 세트 점검에서 판별 불가 — 멈춤")
    applied = applied_by_question(groups, report)
    universe = set(qg.target_counts(report).keys())   # 질문으로 셀 수 있는 기준 64 (세트에 한 번도 없는 기준도 목표에 넣는다)
    result = select(groups, applied, args.min, universe)
    sizes = {m: len(select(groups, applied, m, universe)["chosen"]) for m in (1, 3, 5, 10)}
    ids = [c["id"] for c in result["chosen"]]
    cmp = None
    if args.run:
        run = json.loads((ROOT / "data" / "comparison_runs" / f"{args.run}.json").read_text(encoding="utf-8"))
        cmp = compare(run, ids)
    new_set = save_set(args.set_id, groups, ids, args.min) if args.save_set else None
    OUT.mkdir(parents=True, exist_ok=True)
    stem = "min_eval" if args.min == 1 else f"min_eval_m{args.min}"
    (OUT / f"{stem}.json").write_text(json.dumps({"version": MIN_LIST_VERSION, "set_id": args.set_id, "need": args.min, "ids": ids,
                                                  "chosen": result["chosen"], "short": result["short"], "sizes": sizes, "compare": cmp,
                                                  "min_set_id": new_set}, ensure_ascii=False, indent=1, default=list), encoding="utf-8")
    kinds = Counter(c["kind"] for c in result["chosen"])
    lines = [f"# 최소 평가 리스트 ({MIN_LIST_VERSION})", "",
             f"정밀 평가 = 질문 세트 `{args.set_id}` 전체 {len(groups)}개. 최소 평가 = 그중 **{len(ids)}개** — 질문당 평가 기준이 많은 질문부터 보며, "
             f"평가 기준({result['universe']}개, 질문으로 셀 수 있는 것)이 모두 **{args.min}번 이상** 적용될 때까지 고름(새 기준을 더하지 못하는 질문은 건너뜀).",
             f"구성: {dict(kinds)}. 기준마다 목표 횟수별 필요 질문 수: " + ", ".join(f"{m}번↑ {n}개" for m, n in sizes.items()) + ".",
             *( [f"목표를 못 채운 기준: {result['short']}"] if result["short"] else ["모든 기준이 목표 횟수를 채움."] ),
             *( [f"최소 평가 질문 세트: `{new_set}` (화면 '질문 세트'에서 수집·평가 가능)"] if new_set else [] ), ""]
    if cmp:
        provs = [p for p in cmp["정밀"] if not p.startswith("_")]
        lines += ["## 정밀 vs 최소 결과 (같은 실행의 판정, 질문 단위 · 보류는 분모에 둠)", "",
                  "| 모델 | 정밀 PASS·FAIL·보류 / n (통과율) | 최소 PASS·FAIL·보류 / n (통과율) |", "|---|---|---|"]
        for p in provs:
            a, b = cmp["정밀"][p], cmp["최소"][p]
            lines.append(f"| {p} | {a['pass']}·{a['fail']}·{a['hold']} / {a['n']} ({a['rate']:.0%}) | {b['pass']}·{b['fail']}·{b['hold']} / {b['n']} ({b['rate']:.0%}) |")
        lines += ["", f"순위 — 정밀: {' > '.join(cmp['정밀']['_ranking'])} · 최소: {' > '.join(cmp['최소']['_ranking'])} · "
                      f"{'같음' if cmp['same_ranking'] else '다름'}", ""]
    lines += ["## 고른 질문 (고른 순서)", "", "| 순서 | 질문 | 종류 | 기준 수 | 새로 덮은 기준 |", "|---|---|---|---|---|"]
    for n, c in enumerate(result["chosen"], 1):
        lines.append(f"| {n} | Q{c['id']} | {c['kind']} | {c['k']} | {', '.join(c['new'])} |")
    (OUT / f"{stem}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"최소 평가 {len(ids)}개 / 정밀 {len(groups)}개 | 구성 {dict(kinds)} | 목표별 {sizes} | 못 채움 {len(result['short'])}"
          + (f" | 세트 {new_set}" if new_set else "") + (f" | 순위 같음 {cmp['same_ranking']}" if cmp else ""))
    print("저장:", OUT / f"{stem}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
