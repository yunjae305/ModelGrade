"""잘못된 대화 턴 입력, 누락 구간 크기, 소수점 앞 요청 조건의 회귀 검사."""

from __future__ import annotations

import builtins

import pytest

from code_eval import jev_eval
from code_eval.config import AionaConfig
from code_eval.core import LIMITS
from code_eval.engine import evaluate_batch, evaluate_case
from code_eval.jev_applicability import APPLIES_WHEN, jev_applicability
from code_eval.jev_eval import conversation_contexts, evaluate_jev_batch


def _payload(turn, case_id="conversation"):
    return {"case_id": case_id, "conversation_id": "C1", "turn": turn, "question": "인사해줘.", "answer": "안녕!"}


@pytest.mark.parametrize("turn", ["²", "1" * 5000, 1_000_000_000, True, 1.5, -1, ""])
def test_invalid_turn_is_one_input_error_without_losing_other_batch_items(turn):
    payloads = [{"case_id": "before", "question": "인사해줘.", "answer": "안녕!"},
                _payload(turn, "bad"),
                {"case_id": "after", "question": "인사해줘.", "answer": "안녕!"}]
    batch = evaluate_batch(payloads, AionaConfig())

    assert [c["case_id"] for c in batch["cases"]] == ["before", "bad", "after"]
    assert [c["case_status"] for c in batch["cases"]] == ["EVALUATED", "INPUT_ERROR", "EVALUATED"]
    assert "turn" in batch["cases"][1]["input_error"]
    semantic = evaluate_jev_batch(batch, None)
    assert [r["status"] for r in semantic["records"]] == ["NOT_CONFIGURED", "SKIPPED", "NOT_CONFIGURED"]


@pytest.mark.parametrize("turn,normalized", [(1, 1), ("0002", 2), (" ２ ", 2), (20_000, 20_000)])
def test_supported_turn_forms_and_upper_boundary_remain_valid(turn, normalized):
    result = evaluate_case(_payload(turn), AionaConfig())

    assert result["case_status"] == "EVALUATED"
    assert result["conversation"] == {"id": "C1", "turn": normalized}


def test_turn_limit_follows_the_existing_batch_capacity():
    result = evaluate_case(_payload(LIMITS["batch_cases"] + 1), AionaConfig())

    assert result["case_status"] == "INPUT_ERROR"
    assert f"{LIMITS['batch_cases']:,}" in result["input_error"]


def test_sparse_huge_turn_uses_compact_missing_ranges_without_large_allocation(monkeypatch):
    # 방어 경로를 직접 확인한다. 과거 구현이 돌아와도 테스트가 거대한 목록을 만들지 못하게 막는다.
    def bounded_range(*args):
        value = builtins.range(*args)
        assert len(value) <= 10_000, "누락 턴 전체를 나열하면 안 됨"
        return value

    monkeypatch.setattr(jev_eval, "range", bounded_range, raising=False)
    cases = [{"case_id": "first", "case_status": "EVALUATED", "question": "q", "answer": "a",
              "conversation": {"id": "C1", "turn": 1}},
             {"case_id": "last", "case_status": "EVALUATED", "question": "q", "answer": "a",
              "conversation": {"id": "C1", "turn": 1_000_000_000}}]
    contexts = conversation_contexts(cases)

    assert contexts[1]["complete"] is False
    assert contexts[1]["prior"] == [(1, "q", "a")]
    assert contexts[1]["issues"] == ["누락 2~999999999턴"]


def test_missing_turn_ranges_keep_existing_single_gap_and_detect_separate_gaps():
    def case(turn):
        return {"case_id": str(turn), "case_status": "EVALUATED", "question": "q", "answer": "a",
                "conversation": {"id": "C1", "turn": turn}}

    contexts = conversation_contexts([case(1), case(3), case(6)])
    assert contexts[0]["complete"] is True
    assert contexts[1]["issues"] == ["누락 2턴"]
    assert contexts[2]["issues"] == ["누락 2턴·4~5턴"]


@pytest.mark.parametrize("number", ["3.14", "0.25", "-0.5", "12.75"])
def test_decimal_does_not_drop_audience_and_level_requirements(number):
    result = jev_applicability(f"초등학생도 이해할 수 있게 파이 {number}의 의미를 설명해줘.")

    assert result["criteria"] == ["J1", "J2"]
    assert result["evidence"]["J2"]["rules"] == ["대상", "말투·수준"]
    assert "초등학생도" in result["evidence"]["J2"]["excerpt"]


def test_decimal_keeps_reason_before_number_but_actual_sentences_remain_separate():
    result = jev_applicability("원리를 3.14라는 숫자로 설명해줘!")
    assert "J3" in result["criteria"]
    assert "원리" in result["evidence"]["J3"]["matches"]
    # 실제 마침표 앞의 상황 설명은 여전히 요청 조건이 아니다.
    statement = "초등학생도 이유와 단계를 알고 있어. 인사해줘!"
    assert jev_applicability(statement)["criteria"] == ["J1"]


def test_semantic_criteria_snapshot_contains_the_applied_routing_policy():
    batch = evaluate_batch([_payload(1)], AionaConfig())
    result = evaluate_jev_batch(batch, None)

    assert {c["id"]: c["applies_when"] for c in result["criteria"]} == APPLIES_WHEN
    assert all(c["question"] and c["pass"] and c["fail"] for c in result["criteria"])
