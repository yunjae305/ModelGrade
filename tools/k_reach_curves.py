"""평가 횟수 k 와 질문 수 (k-reach-v1) — 모든 기준이 최소 k번 평가되려면 질문을 몇 개 던져야 하나, 던지는 순서 세 가지로 (사용자 10-20 미팅 준비:
"평가기준 k(세로축)가 10개·20개일 때 질문 수(가로축) — 번호순 · 적용 기준 많은 순 · 모자란 기준순, 세 선이 특정 지점에서 만나지 않을까").
AI 호출 없음, 질문 세트만 읽는다(05장 tools/order_curves.py 와 같은 '질문으로 셀 수 있는 기준 64개').

    python -X utf8 tools/k_reach_curves.py
    → 참고자료/최소평가/평가횟수k_곡선_326.json · .md

- 세로축 k: 질문을 n개 던졌을 때 '가장 적게 평가된 기준'의 평가 횟수 = 모든 기준이 최소 k번 평가됨.
  · 그대로(raw): 기준 64개 모두 k번 — 326 안에 질문이 적은 기준이 천장을 정한다.
  · 있는 만큼(cap): 326 안에 k번보다 적게 나오는 기준은 있는 질문을 모두 던지면 채운 것으로 — 198·155 목록을 고를 때와 같은 셈.
- 순서: 번호순(1→326) · 기준 많은 순(적용 기준 수 많은 순, 같으면 번호) · 모자란 기준부터(그때까지 가장 적게 평가된 기준을 채우는 질문부터 —
  그다음 적은 기준 · 기준 수 · 번호 순으로 동점 처리). 참고로 05장·간단 78 의 순서(10번 목표 탐욕)와 155 목록 순서(20번 목표)의 도달 지점도 함께 센다.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from tools.min_eval_list import applied_by_question  # noqa: E402
from tools.question_gradient import order_by_k  # noqa: E402
from webapp import question_generation as qg  # noqa: E402

VERSION = "k-reach-v1"
SET_DIR = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"
OUT = ROOT / "참고자료" / "최소평가"
ORDERS = [("number", "번호순"), ("kdesc", "기준 많은 순"), ("level", "모자란 기준부터")]
REFS = [("greedy10", "간단 78 순서 (10번 목표)"), ("greedy20", "155 목록 순서 (20번 목표)")]
TARGETS = (1, 3, 5, 10, 15, 20)
CAP_MAX = 30


def level_order(applied: Dict[int, set], universe: set, avail: Counter) -> List[int]:
    """모자란 기준부터 — 매번 '지금 가장 적게 평가된 기준'을 가장 많이 담은 질문(동점: 그다음 적은 기준 수 → 기준 수 → 번호).
    질문을 다 쓴 기준(326 안 질문이 모두 나옴)은 '모자란 기준'에서 뺀다 — 안 빼면 k=10 천장 뒤로 고르기가 멈춘다."""
    counts: Counter = Counter()
    remaining = set(applied)
    order: List[int] = []
    while remaining:
        open_ = [t for t in universe if counts[t] < avail[t]]
        low = min((counts[t] for t in open_), default=0)
        q = max(remaining, key=lambda q: (sum(1 for t in applied[q] if counts[t] == low and counts[t] < avail[t]),
                                          sum(1 for t in applied[q] if counts[t] == low + 1 and counts[t] < avail[t]), len(applied[q]), -q))
        remaining.remove(q)
        order.append(q)
        counts.update(applied[q])
    return order


def curves(order: List[int], applied: Dict[int, set], universe: set, avail: Counter) -> Dict[str, List[int]]:
    """n = 1..326 마다 raw k(가장 적게 평가된 기준의 횟수) · cap k(있는 만큼 채운 셈, CAP_MAX 까지)."""
    counts: Counter = Counter()
    raw, cap = [], []
    for q in order:
        counts.update(applied[q])
        raw.append(min(counts[t] for t in universe))
        k = 0
        while k < CAP_MAX and all(counts[t] >= min(k + 1, avail[t]) for t in universe):
            k += 1
        cap.append(k)
    return {"raw": raw, "cap": cap}


def first_reach(values: List[int], k: int) -> Optional[int]:
    return next((i + 1 for i, v in enumerate(values) if v >= k), None)


def build(set_dir: Path = SET_DIR) -> Dict[str, Any]:
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    report = qg.check(groups)
    applied = applied_by_question(groups, report)
    universe = set(qg.target_counts(report).keys())
    avail = Counter(t for s in applied.values() for t in s)
    orders = {"number": sorted(applied), "kdesc": sorted(applied, key=lambda q: (-len(applied[q]), q)),
              "level": level_order(applied, universe, avail),
              "greedy10": order_by_k(groups, report, 10)[0], "greedy20": order_by_k(groups, report, 20)[0]}
    cv = {k: curves(o, applied, universe, avail) for k, o in orders.items()}
    reach = {k: {kind: {str(t): first_reach(cv[k][kind], t) for t in TARGETS} for kind in ("raw", "cap")} for k in orders}
    ceiling = min(avail[t] for t in universe)
    rare = sorted(((t, avail[t]) for t in universe if avail[t] < 20), key=lambda x: (x[1], x[0]))
    n = len(applied)
    # 세 선이 같은 값이 되는 첫 질문 수(그 뒤로 끝까지 같은) — raw
    same_from = next((i + 1 for i in range(n) if all(len({cv[k]["raw"][j] for k, _ in ORDERS}) == 1 for j in range(i, n))), None)
    return {"version": VERSION, "questions": n, "criteria": len(universe), "ceiling_raw": ceiling,
            "rare_criteria": [{"id": t, "questions": c} for t, c in rare],
            "orders": {k: {"label": label, "first": orders[k][:20]} for k, label in ORDERS + REFS},
            "reach": reach, "same_from_raw": same_from,
            "curves": {k: cv[k] for k, _ in ORDERS + REFS}}   # 참고 순서(간단 78·155 목록)의 곡선도 저장 — 보고 00장 그래프


def markdown(r: Dict[str, Any]) -> str:
    md = [f"# 평가 횟수 k 와 질문 수 ({r['version']})", "",
          f"질문 {r['questions']}개 · 질문으로 셀 수 있는 기준 {r['criteria']}개. 모든 기준이 최소 k번 평가되는 데 필요한 질문 수.", "",
          f"- 그대로 셀 때 천장: k = {r['ceiling_raw']} — 326 안에 질문이 {r['ceiling_raw']}개뿐인 기준이 있어서.",
          f"- 세 순서가 같은 값이 되는 지점(그대로): {r['same_from_raw']}개부터.", "",
          "| 순서 | " + " | ".join(f"k={t} 그대로" for t in TARGETS) + " | k=10 있는 만큼 | k=20 있는 만큼 |",
          "|---|" + "---|" * (len(TARGETS) + 2)]
    for k, v in r["reach"].items():
        label = r["orders"][k]["label"]
        md.append(f"| {label} | " + " | ".join(str(v["raw"][str(t)] or "닿지 않음") for t in TARGETS)
                  + f" | {v['cap']['10']} | {v['cap']['20']} |")
    md += ["", "20번 미만 기준(326 안 질문 수): " + ", ".join(f"{x['id']} {x['questions']}" for x in r["rare_criteria"])]
    return "\n".join(md) + "\n"


def main() -> int:
    r = build()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "평가횟수k_곡선_326.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "평가횟수k_곡선_326.md").write_text(markdown(r), encoding="utf-8")
    print(markdown(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
