"""던지는 순서 세 가지 (order-curves-v1) — 번호순 · 기준 많은 순 · 모자란 기준부터로 질문을 하나씩 더할 때 무엇이 얼마나 빨리 채워지나
(10-19 오후 미팅 53:53~56:54 · 체크리스트 n4). AI 호출 없음, 질문 세트와 저장소의 326 결과 JSON 만 읽는다.

    python -X utf8 tools/order_curves.py
    → 참고자료/최소평가/던지는순서_세가지_8c61f117e661.json · .md

- 순서: 번호순(1→326) · 기준 많은 순(질문으로 셀 수 있는 적용 기준 수가 많은 순, 같으면 번호순 — 198·110 목록과 같은 기준) ·
  모자란 기준부터(tools/question_gradient.order_by_k, 기준마다 10번 목표 — 78·155 목록과 같은 순서).
- 질문 n 개까지 던졌을 때: ① 10번 이상 평가된 기준 수(64 중) ② 보인 FAIL 종류 — **기준 단위**(어느 모델에서든 그 기준에 FAIL 이 나왔으면 1종류; 사용자 10-20:
  기준이 72개면 FAIL 종류도 기준 수여야 한다) ③ 참고로 (모델, 기준) 조합 수 ④ FAIL 사건(모델·질문 단위 합).
  FAIL 은 min_eval_list.fail_capture 와 같은 규칙(CODE 는 check_id 의 검사 부분, 의미는 J1~J8)으로 결과 JSON 에서 센다 — README 의 28(기준)·87(조합)·586 과 맞춰 본다.
- 세 순서 모두 같은 326 개를 순서만 바꾼 것이라 n = 326 에서 세 선이 같은 점에 모인다.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from tools.min_eval_list import applied_by_question  # noqa: E402
from tools.question_gradient import order_by_k  # noqa: E402
from webapp import question_generation as qg  # noqa: E402

VERSION = "order-curves-v1"
SET_DIR = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"
RESULTS = ROOT / "참고자료" / "결과" / "8c61f117e661_정밀326_2026-10-19.json"
OUT = ROOT / "참고자료" / "최소평가"
NEED = 10
README_FAILS = {"kinds": 28, "pairs": 87, "events": 586}   # 참고자료/최소평가/README — 다시 센 값이 이것과 같아야 같은 규칙 (kinds = 기준 단위 FAIL 종류, 10-20)
KINDS_UNIVERSE = 36   # 10-19 실행의 FAIL 종류 상한: CODE 검사 28 + 의미 대분류 J1~J8 (판정 v4 부터는 72)
ORDERS = [("number", "번호순"), ("kdesc", "기준 많은 순"), ("greedy", "모자란 기준부터")]
MARKS = (10, 20, 50, 78, 100, 110, 155, 198, 250, 326)


def fail_sets(data: Dict[str, Any]) -> Dict[tuple, set]:
    """(모델, 기준) → FAIL 이 나온 질문 번호들 — min_eval_list.fail_capture 와 같은 규칙."""
    fails: Dict[tuple, set] = defaultdict(set)
    for provider, section in (data.get("providers") or {}).items():
        for turn in section.get("turns") or []:
            q = int(turn["question"])
            for row in turn.get("code") or []:
                if row.get("verdict") == "FAIL":
                    fails[(provider, str(row.get("check_id", "")).split(":")[0])].add(q)
            for cid, d in ((turn.get("semantic_legacy") or {}).get("decisions") or {}).items():
                if isinstance(d, dict) and d.get("decision") == "FAIL":
                    fails[(provider, cid)].add(q)
    return fails


def kind_sets(fails: Dict[tuple, set]) -> Dict[str, set]:
    """기준 → FAIL 이 나온 (모델, 질문) — FAIL 종류는 기준 단위로 센다(사용자 2026-10-20: 기준이 72개면 FAIL 종류도 기준 수여야 한다).
    10-19 의 326 실행은 의미 판정이 대분류(J1~J8) 단위라 종류 상한은 CODE 28 + J 8 = 36; 판정 v4(항목 44개)부터는 72."""
    kinds: Dict[str, set] = defaultdict(set)
    for (provider, cid), qs in fails.items():
        kinds[cid].update((provider, q) for q in qs)
    return kinds


def curve(order: List[int], applied: Dict[int, set], universe: set, fails: Dict[tuple, set]) -> Dict[str, List[int]]:
    """n = 1..len(order) 마다 10번 이상 평가된 기준 수 · 보인 FAIL 종류 수(기준 단위) · (모델, 기준) 조합 수 · FAIL 사건 수."""
    kinds = kind_sets(fails)
    counts: Counter = Counter()
    seen_q: set = set()
    covered, kind_n, pairs, events = [], [], [], []
    for q in order:
        counts.update(t for t in applied[q] if t in universe)
        seen_q.add(q)
        covered.append(sum(1 for t in universe if counts[t] >= NEED))
        kind_n.append(sum(1 for v in kinds.values() if any(qq in seen_q for _, qq in v)))
        pairs.append(sum(1 for v in fails.values() if v & seen_q))
        events.append(sum(len(v & seen_q) for v in fails.values()))
    return {"covered10": covered, "fail_kinds": kind_n, "fail_pairs": pairs, "fail_events": events}


def first_reach(values: List[int], target: float) -> Optional[int]:
    return next((i + 1 for i, v in enumerate(values) if v >= target), None)


def build(results_path: Path = RESULTS, set_dir: Path = SET_DIR) -> Dict[str, Any]:
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    report = qg.check(groups)
    applied = applied_by_question(groups, report)
    universe = set(qg.target_counts(report).keys())
    data = json.loads(results_path.read_text(encoding="utf-8"))
    fails = fail_sets(data)
    kinds = kind_sets(fails)
    totals = {"criteria": len(universe), "kinds": len(kinds), "kinds_universe": KINDS_UNIVERSE, "pairs": len(fails), "events": sum(len(v) for v in fails.values())}
    orders = {"number": sorted(applied), "kdesc": sorted(applied, key=lambda q: (-len(applied[q]), q)),
              "greedy": order_by_k(groups, report, NEED)[0]}
    curves = {k: curve(orders[k], applied, universe, fails) for k, _ in ORDERS}
    reach = {k: {"criteria_all": first_reach(c["covered10"], totals["criteria"]),
                 "kinds_80": first_reach(c["fail_kinds"], 0.8 * totals["kinds"]),
                 "kinds_all": first_reach(c["fail_kinds"], totals["kinds"]),
                 "pairs_80": first_reach(c["fail_pairs"], 0.8 * totals["pairs"]),
                 "pairs_all": first_reach(c["fail_pairs"], totals["pairs"]),
                 "events_50": first_reach(c["fail_events"], 0.5 * totals["events"])} for k, c in curves.items()}
    marks = [{"n": n, **{k: {"covered10": curves[k]["covered10"][n - 1], "fail_kinds": curves[k]["fail_kinds"][n - 1],
                             "fail_pairs": curves[k]["fail_pairs"][n - 1], "fail_events": curves[k]["fail_events"][n - 1]} for k, _ in ORDERS}} for n in MARKS]
    return {"version": VERSION, "run_id": data.get("run_id"), "need": NEED, "totals": totals,
            "checks": {"fails_match_readme": totals["kinds"] == README_FAILS["kinds"] and totals["pairs"] == README_FAILS["pairs"] and totals["events"] == README_FAILS["events"],
                       "same_end": all(curves[k]["covered10"][-1] == totals["criteria"] and curves[k]["fail_kinds"][-1] == totals["kinds"] for k in curves)},
            "orders": {k: {"label": label, "first": orders[k][:20]} for k, label in ORDERS},
            "reach": reach, "marks": marks, "curves": curves}


def markdown(result: Dict[str, Any]) -> str:
    t = result["totals"]
    labels = {k: v["label"] for k, v in result["orders"].items()}
    lines = [f"# 던지는 순서 세 가지 ({result['version']})", "",
             f"실행 `{result['run_id']}` · 기준 {t['criteria']}개(질문으로 셀 수 있는 것) · FAIL 종류 {t['kinds']}개(기준 단위, 상한 {t['kinds_universe']} = CODE 28 + 의미 대분류 8 — 이 실행의 의미 판정은 J1~J8 단위) · FAIL 사건 {t['events']}건. "
             f"참고: (모델, 기준) 조합으로 세면 {t['pairs']}개. "
             f"점검: FAIL 수가 README 와 {'같음' if result['checks']['fails_match_readme'] else '**다름**'} · 326 에서 세 순서가 {'같은 점' if result['checks']['same_end'] else '**다른 점**'}.", "",
             "| 도달 | " + " | ".join(labels.values()) + " |", "|---|" + "---|" * len(labels)]
    for key, name in (("criteria_all", f"기준 {t['criteria']}개 모두 10번↑"), ("kinds_80", "FAIL 종류(기준) 80% 보임"),
                      ("kinds_all", "FAIL 종류(기준) 모두 보임"), ("pairs_80", "(모델, 기준) 조합 80% 보임"), ("events_50", "FAIL 사건 50%")):
        lines.append(f"| {name} | " + " | ".join(f"{result['reach'][k][key]}개" for k in labels) + " |")
    lines += ["", "| 질문 수 | " + " | ".join(f"{v} 기준 10번↑ · FAIL 종류(기준) · 조합 · 사건" for v in labels.values()) + " |", "|---|" + "---|" * len(labels)]
    for m in result["marks"]:
        lines.append(f"| {m['n']} | " + " | ".join(f"{m[k]['covered10']} · {m[k]['fail_kinds']} · {m[k]['fail_pairs']} · {m[k]['fail_events']}" for k in labels) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = build()
    stem = OUT / f"던지는순서_세가지_{result['run_id']}"
    stem.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown(result), encoding="utf-8")
    if not all(result["checks"].values()):
        print("경고: 점검이 맞지 않음 —", result["checks"])
    print(markdown(result))
    print("저장:", stem.with_suffix(".json"), "·", stem.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
