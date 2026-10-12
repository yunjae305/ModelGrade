"""Regression cases found by the v19 implementation review."""

import pytest

from code_eval.engine import evaluate_case
from code_eval.model import Status
from code_eval.textparse import split_lines


def evaluate(question, answer):
    return evaluate_case({"question": question, "answer": answer})


def row(case, prefix):
    return next(r for r in case["results"] if r["check_id"].startswith(prefix))


def rows(case, prefix):
    return [r for r in case["results"] if r["check_id"].startswith(prefix)]


@pytest.mark.parametrize("answer,verdict,claimed", [
    ("결과는 5 - 8 = -3입니다.", "PASS", -3),
    ("결과는 5 - 8 = **-3**입니다.", "PASS", -3),
    ("결과는 5 - 8 = −3입니다.", "PASS", -3),
    ("결과는 5 - 8 = 3입니다.", "FAIL", 3),
])
def test_signed_result_after_labelled_equation(answer, verdict, claimed):
    result = row(evaluate("5에서 8을 빼면?", answer), "3-1.question_calculation")
    assert result["verdict"] == verdict
    assert result["measured"]["claimed"] == claimed
    assert result["required"]["expected"] == -3


@pytest.mark.parametrize("question,answer,expected", [
    ("-5에서 3을 빼면?", "결과는 -8입니다.", -8),
    ("5에서 -3을 빼면?", "결과는 8입니다.", 8),
    ("-5와 3을 더해줘.", "결과는 -2입니다.", -2),
    ("-5에 -3을 곱해줘.", "결과는 15입니다.", 15),
    ("-6을 3으로 나누면?", "결과는 -2입니다.", -2),
    ("-5 + 3은?", "-2", -2),
    ("5 + -3은?", "2", 2),
    ("5-3은?", "2", 2),
    ("−5 + 3은?", "−2", -2),
    ("-200의 10%는?", "-20", -20),
    ("200의 -10%는?", "-20", -20),
    ("-5에서 3을 빼면?", "-5 - 3 = -8", -8),
])
def test_question_arithmetic_keeps_operand_signs(question, answer, expected):
    result = row(evaluate(question, answer), "3-1.question_calculation")
    assert result["verdict"] == "PASS"
    assert result["required"]["expected"] == expected


@pytest.mark.parametrize("answer", ["1 + 1 = 2%", r"1 + 1 = 2\%", "1 + 1 = 2 %"])
def test_percent_suffix_is_not_silently_dropped_from_equation(answer):
    case = evaluate("계산해줘.", answer)
    assert rows(case, "3-1.answer_equation") == []  # 보류 등식은 PASS 도 FAIL 도 아니고 행이 없다
    assert any(s["check_id"] == "3-1.answer_equation:skipped" for s in case["meta"]["skipped_conditions"])


@pytest.mark.parametrize("answer", ["2%", "결과는 2%입니다.", "결과는 1 + 1 = 2%입니다.",
                                    "결과는 1 + 1 = **2**%입니다."])
def test_percent_claim_is_not_linked_as_plain_numeric_answer(answer):
    case = evaluate("1 + 1은?", answer)
    assert rows(case, "3-1.question_calculation") == []
    assert any(s["check_id"] == "3-1.question_calculation" for s in case["meta"]["skipped_conditions"])


@pytest.mark.parametrize("answer,verdict", [
    ("80 / 2000 * 100 = 4%", "PASS"),
    (r"80 / 2000 * 100 = 5\%", "FAIL"),
])
def test_explicit_ratio_to_percent_conversion_still_recomputed(answer, verdict):
    assert row(evaluate("클릭률을 계산해줘.", answer), "3-1.answer_equation")["verdict"] == verdict


@pytest.mark.parametrize("between,body", [
    ("구체적인 내용입니다.\n", "1. 개념\n2. 문제\n3. 복습"),
    ("", "1. 개념\n   개념을 충분히 이해하세요.\n2. 문제\n3. 복습"),
    ("", "1. 개념\n\n   개념을 충분히 이해하세요.\n\n2. 문제\n3. 복습"),
    ("", "1. 개념\n개념을 충분히 이해하세요.\n2. 문제\n3. 복습"),
    ("", "1. 개념\n   - 정의\n   - 원리\n2. 문제\n3. 복습"),
])
def test_declared_count_covers_complete_list_with_description_lines(between, body):
    case = evaluate("공부 방법을 알려줘.", "다음 세 가지를 추천합니다.\n" + between + body)
    result = row(case, "3-2.")
    assert result["verdict"] == "PASS"
    assert result["measured"]["count"] == 3
    assert len(result["details"]["counted_items"]) == 3


def test_list_continuation_does_not_hide_extra_item():
    answer = "다음 세 가지를 추천합니다.\n1. 개념\n   설명입니다.\n2. 문제\n3. 복습\n4. 휴식"
    result = row(evaluate("공부 방법을 알려줘.", answer), "3-2.")
    assert result["verdict"] == "FAIL" and result["measured"]["count"] == 4


def test_separate_paragraph_ends_declared_list():
    answer = "다음 세 가지를 추천합니다.\n1. 개념\n2. 문제\n3. 복습\n\n별도의 준비물입니다.\n1. 펜\n2. 노트"
    result = row(evaluate("공부 방법을 알려줘.", answer), "3-2.")
    assert result["verdict"] == "PASS" and result["measured"]["count"] == 3


@pytest.mark.parametrize("outer,inner", [("````", "```"), ("~~~~", "~~~")])
def test_shorter_fence_stays_inside_longer_code_block(outer, inner):
    answer = f"{outer}markdown\n{inner}python\n- 예시 안의 불릿\n{inner}\n{outer}\n- 실제 설명"
    result = row(evaluate("불릿 정확히 1개로 설명해줘.", answer), "2-1.count:bullet_count")
    assert result["verdict"] == "PASS"
    assert result["measured"]["items"] == [6]
    assert [line.in_code for line in split_lines(answer)] == [True] * 5 + [False]


def test_fence_with_info_does_not_close_existing_block():
    answer = "```markdown\n```python\n- 코드 안\n```\n- 본문"
    result = row(evaluate("불릿 정확히 1개로 설명해줘.", answer), "2-1.count:bullet_count")
    assert result["verdict"] == "PASS" and result["measured"]["items"] == [5]
