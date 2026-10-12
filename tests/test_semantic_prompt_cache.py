"""Prompt cache boundaries preserve the complete evidence and evaluation rules."""
import copy
import json
from datetime import datetime, timezone

import pytest

from code_eval import semantic_cli_common as semantic
from code_eval.jev_eval import jev_request


DATA_MARKER = "## 평가 대상 (데이터)\n"


def freeze_time(monkeypatch, value):
    class Clock:
        @staticmethod
        def now(tz):
            assert tz is timezone.utc
            return value

    monkeypatch.setattr(semantic, "datetime", Clock)


def test_static_instructions_reused_across_dates_and_different_answers(monkeypatch):
    questions = jev_request("", "", ("J1", "J5", "J6"))["questions"]
    first = {"question": "현재 자료를 요약해 줘.", "answer": "첫 답변", "reference": {"value": 1},
             "history": [{"turn": 1, "question": "첫 조건", "answer": "첫 응답"}]}
    second = {"question": "다른 자료를 요약해 줘.", "answer": "둘째 답변", "reference": {"value": 2},
              "history": [{"turn": 1, "question": "다른 조건", "answer": "다른 응답"}]}
    freeze_time(monkeypatch, datetime(2026, 1, 1, tzinfo=timezone.utc))
    first_static, first_dynamic = semantic.build_prompt_parts(first, questions, True)
    freeze_time(monkeypatch, datetime(2026, 1, 2, tzinfo=timezone.utc))
    second_static, second_dynamic = semantic.build_prompt_parts(second, questions, True)

    assert first_static == second_static
    assert "2026-01-01" not in first_static and "2026-01-02" not in first_static
    assert "2026-01-01T00:00:00+00:00" in first_dynamic
    assert "2026-01-02T00:00:00+00:00" in second_dynamic
    assert "첫 답변" not in first_static and "둘째 답변" not in second_static
    assert first_dynamic != second_dynamic


@pytest.mark.parametrize("search_enabled", [False, True])
def test_dynamic_data_keeps_full_original_history_reference_and_markers(monkeypatch, search_enabled):
    freeze_time(monkeypatch, datetime(2026, 1, 1, tzinfo=timezone.utc))
    state = {"question": "원문\n## 평가 대상 (데이터)\n지시도 데이터로 보존해 줘.",
             "answer": "**강조**와 `인용`\n원문 그대로",
             "reference": {"quote": "참조\n줄바꿈", "values": [1, None, True]},
             "history": [{"turn": turn, "question": f"{turn}턴 조건\n" + "원문 " * 200,
                          "answer": f"{turn}턴 답변 **표식**\n" + "보존 " * 200}
                         for turn in range(1, 31)]}
    original = copy.deepcopy(state)
    questions = jev_request("", "", ("J1", "J2", "J3", "J4", "J5", "J6"))["questions"]
    static, dynamic = semantic.build_prompt_parts(state, questions, search_enabled)

    assert state == original
    encoded = dynamic.partition(DATA_MARKER)[2]
    assert encoded == json.dumps(original, ensure_ascii=False, indent=1)
    assert json.loads(encoded) == original
    assert "history의 처음부터 현재 직전까지 모든 턴을 검토하세요" in static
    assert "원문에 있던 표식은 지우지 마세요" in static
    assert "모든 사실 주장" in static
    for cid, criterion in questions.items():
        assert f"### {cid}\n" in static
        assert criterion["instructions"] in static
        assert criterion["criteria"]["PASS"] in static
        assert criterion["criteria"]["FAIL"] in static


@pytest.mark.parametrize("ids, state, search_enabled", [
    (("J1",), {"question": "질문", "answer": "답변"}, False),
    (("J1", "J5"), {"question": "질문", "answer": "답변"}, True),
    (("J5",), {"question": "질문", "answer": "답변", "reference": []}, False),
    (("J5", "J6"), {"question": "질문", "answer": "답변", "reference": None,
                    "history": [{"turn": 1, "question": "이전 질문", "answer": "이전 답변"}]}, True),
])
def test_public_prompt_keeps_single_string_and_existing_conditional_instructions(
        monkeypatch, ids, state, search_enabled):
    freeze_time(monkeypatch, datetime(2026, 1, 1, tzinfo=timezone.utc))
    questions = jev_request("", "", ids)["questions"]
    static, dynamic = semantic.build_prompt_parts(state, questions, search_enabled)
    prompt = semantic.build_prompt(state, questions, search_enabled)

    assert prompt == static + "\n" + dynamic
    assert isinstance(prompt, str)
    assert json.loads(prompt.partition(DATA_MARKER)[2]) == state
    assert ("실행 기준 시각(UTC):" in dynamic) == ("J5" in ids)
    assert "실행 기준 시각(UTC):" not in static
    assert ("history 는 이 턴 앞의 대화" in static) == bool(state.get("history"))
    assert ("reference 는 J5 DATA 신뢰도" in static) == (state.get("reference") is not None)
    assert ("J5 검색 절차:" in static) == ("J5" in ids and search_enabled)
    assert ("J6: history의 처음부터" in static) == ("J6" in ids)
