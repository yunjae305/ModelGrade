"""멀티턴 턴별 확인 목록 생성기 — 목표 항목 규칙 (10-25 밤)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "참고자료" / "질문세트_78" / "multiturn_v3" / "build_turn_checklists.py"


def _builder():
    spec = importlib.util.spec_from_file_location("build_turn_checklists", BUILDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_recommendation_item_only_on_turns_that_ask_for_one():
    """J2-5.a(추천 적합)의 적용은 '추천 · 제안 요청' — 1턴은 추천을 묻는 대화(종류 A)만, 안내문 초안 턴에는 두지 않음 (10-25 밤 보류 분석)."""
    b = _builder()
    assert b.asks_recommendation("setup", "A", "") and not b.asks_recommendation("setup", "B", "A안과 B안 가운데 무엇을 고를지는 아직 정하지 마세요.")
    assert not b.asks_recommendation("draft", "A", "참가 안내문 초안 전체를 제시해 주세요.")
    assert b.asks_recommendation("final", "A", "어느 쪽이 나은지 추천해 주세요.")
    plan = json.loads((BUILDER.parent / "turn_checklists.json").read_text(encoding="utf-8"))
    for q in plan["questions"].values():
        for t in q["turns"]:
            if "J2-5.a" in t["target_items"]:
                assert t["flow"] == "setup" and q["kind"] == "A", (q["question"], t["turn"])

