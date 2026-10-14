"""고정 질문 세트 만들기 — 간단 평가(모든 세부 기준 1번 이상, 최소 개수)·정밀 평가(모든 세부 조건 3번 이상) (사용자 결정 2026-10-14).

후보: 공개 세트만 — IFEval-Ko(allganize, Apache-2.0, 342문항) + KoMT-Bench(LGAI-EXAONE, LGPL-3.0, 80개 2턴 대화) (사용자 결정 2026-10-14:
'공개 링크에 있는 것들로'). 두 세트에 없는 기준(10턴 대화 J6·긴 문서 J7·첨부 J8·코드블록·표·JSON 형식 등)은 이 세트로 평가되지 않고
selection.json 의 short 에 남는다. 적용 여부는 평가 때와 같은 코드 규칙(tools/check_question_set.check_items — CODE 질문 조건 추출·의미 기준
적용 규칙)으로 정하고 모델은 부르지 않는다. 답변 모양으로만 적용이 정해지는 검사(코드 펜스·각주 등)는 고르는 기준에서 뺀다.

고르기: 세부 기준마다 k번(간단 1 · 정밀 3, 후보에 그보다 적으면 있는 만큼)을 채우는 탐욕 다중 덮개를 순서를 바꿔 여러 번 돌려 가장 적은
질문 수를 쓴다(근사 최소). 결과는 examples/fixed_sets/{simple,precise}/questions.json(질문 생성 세트와 같은 id/turns 모양) + files/ +
selection.json(출처·적용 수·모자란 기준).

    python tools/build_fixed_sets.py
"""
from __future__ import annotations

import json
import random
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.check_question_set import check_items  # noqa: E402
from tools.min_question_count import all_targets, question_units  # noqa: E402
from webapp.question_generation import _summary, check, flatten  # noqa: E402

FIXED_SET_VERSION = "fixed-set-v1"
OUT = ROOT / "examples" / "fixed_sets"
SOURCES = OUT / "sources"
KINDS = {"simple": {"k": 1, "label": "간단 평가"}, "precise": {"k": 3, "label": "정밀 평가"}}
TRIES = 300


def candidates():
    """후보 질문 묶음(id/turns) — 출처를 source 에 남긴다."""
    groups = []
    for row in json.loads((SOURCES / "ifeval_ko.json").read_text(encoding="utf-8")):
        groups.append({"type": "지시 따르기", "topic": ", ".join(row.get("instruction_id_list") or []), "difficulty": "", "trap": "",
                       "target_items": [], "turns": [{"turn": 1, "question": row["prompt"]}], "source": f"IFEval-Ko key {row['key']}"})
    for line in (SOURCES / "komt_question_ko.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            groups.append({"type": row.get("category") or "", "topic": row.get("category") or "", "difficulty": "", "trap": "",
                           "target_items": [], "turns": [{"turn": i, "question": q} for i, q in enumerate(row["turns"], 1)],
                           "source": f"KoMT-Bench {row['question_id']}"})
    for i, g in enumerate(groups, 1):   # 후보 안에서 고유 번호 — 업로드 변환이 Q{id}_T{턴} 을 만든다
        g["id"] = i
    return groups


def cover(order, targets, names, k, rng):
    need = {t: k for t in names}
    left = list(order)
    rng.shuffle(left)
    chosen = []
    while any(need.values()):
        gain = {u: sum(1 for t in targets[u] if need.get(t, 0) > 0) for u in left}
        best = max(gain.values(), default=0)
        if best == 0:
            break
        pick = rng.choice([u for u in left if gain[u] == best])
        chosen.append(pick)
        left.remove(pick)
        for t in targets[pick]:
            if need.get(t, 0) > 0:
                need[t] -= 1
    return chosen


def main() -> int:
    groups = candidates()
    by_id = {str(g["id"]): g for g in groups}
    report = check_items(flatten(groups), pipeline="items")
    order, targets, _ = question_units(report)
    names = all_targets(report)
    unit_group = {}
    for row in report["cases"]:   # 질문 단위 → 후보 묶음 id
        from tools.check_question_set import unit_of
        unit_group[unit_of(row)] = str(row.get("case_id", "")).split("_T")[0].lstrip("Q")
    available = Counter(t for u in order for t in targets[u])
    print(f"후보 {len(groups)}개 · 세부 기준 {len(names)}개 · 후보에 없는 기준 {[t for t in names if not available[t]]}")
    rng = random.Random(20261014)
    for kind, spec in KINDS.items():
        k = spec["k"]
        wanted = {t: min(k, available[t]) for t in names}
        best = None
        for _ in range(TRIES):
            chosen = cover(order, targets, [t for t in names if wanted[t]], k, rng)
            if best is None or len(chosen) < len(best):
                best = chosen
        picked = sorted({unit_group[u] for u in best}, key=int)
        out = OUT / kind
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)   # 공개 세트에는 첨부파일이 없다
        chosen_groups = []
        for new_id, gid in enumerate(picked, 1):
            g = json.loads(json.dumps(by_id[gid], ensure_ascii=False))
            g["id"] = new_id
            chosen_groups.append(g)
        (out / "questions.json").write_text(json.dumps(chosen_groups, ensure_ascii=False, indent=1), encoding="utf-8")
        got = Counter(t for u in best for t in targets[u])
        short = {t: f"{got[t]}/{k}" for t in names if got[t] < k}
        sources = Counter(g["source"].split(" ")[0] for g in chosen_groups)
        set_report = check(chosen_groups)   # 버튼을 누를 때 다시 점검하지 않게 요약·적용 현황을 미리 둔다
        selection = {"version": FIXED_SET_VERSION, "kind": kind, "label": spec["label"], "k": k, "questions": len(chosen_groups),
                     "summary": _summary(chosen_groups, set_report), "coverage": set_report["coverage"],
                     "turns": sum(len(g["turns"]) for g in chosen_groups), "targets": len(names), "sources": dict(sources),
                     "short": short, "candidates": len(groups),
                     "licenses": {"IFEval-Ko": "allganize/IFEval-Ko · Apache-2.0", "KoMT-Bench": "LGAI-EXAONE/KoMT-Bench · LGPL-3.0"}}
        (out / "selection.json").write_text(json.dumps(selection, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{spec['label']}: 질문 {selection['questions']}개(턴 {selection['turns']}) · 출처 {dict(sources)} · k 못 채운 기준 {len(short)}개 {short}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
