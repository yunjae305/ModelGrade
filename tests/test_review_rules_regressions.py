"""Regression cases found by the v19 implementation review."""

import pytest

from code_eval.engine import evaluate_case
from code_eval.textparse import split_lines


def evaluate(question, answer):
    return evaluate_case({"question": question, "answer": answer})


def row(case, prefix):
    return next(r for r in case["results"] if r["check_id"].startswith(prefix))


def rows(case, prefix):
    # 답변 구조 검사의 '검사 대상 없음' PASS 행(:none, 엔진 v16.5.0)은 뺀다
    return [r for r in case["results"] if r["check_id"].startswith(prefix) and not r["check_id"].endswith(":none")]


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


# --- v16.4.1: 저장 실행에서 찾은 가짜 FAIL ---------------------------------------------------------------------

@pytest.mark.parametrize("answer", [
    "- 영업일: 20일 → 26일 = **26 ÷ 20 = 1.3배 (+30%)**",          # 화살표 뒤 값은 등식의 시작이 아니다
    "*(총 소요 시간: 80분 + 20분 + 85분 = 185분 / 190분 이하 충족)*",  # '값 / 한도' 표기
    "필요한 양은 2벌 × 3 + 1 = 7벌입니다.",                           # 단위에 끊긴 앞 피연산자 (2벌 ×)
    "   2벌 × 3 + 1 = 7벌",                                          # 줄 앞 공백이 있어도
    "**2벌** × 3 + 1 = 7벌",                                         # 굵게 표시가 앞에 있어도
    "필요한 양은 2벌 ×3 + 1 = 7입니다.",                               # 붙여 쓴 곱셈 기호는 기울임이 아니다
    "필요한 양은 2 벌 ×3 + 1 = 7입니다.",                              # 숫자와 단위 사이 공백
    "총 넓이: 2㎡ ×3 + 1 = 7입니다.",                                   # 단위 기호
    "2m² ×3 + 1 = 7입니다.",
    '길이(인치): 2" × 3 + 1 = 7"',                                     # 따옴표·대괄호 단위 뒤 곱셈 기호
    "길이(피트): 2' × 3 + 1 = 7'",
    "무게: 2[kg] × 3 + 1 = 7[kg]",
    '길이(인치): 6" / 3 + 1 = 3"',                                     # 나눗셈·뺄셈·TeX 곱셈 앞의 단위도
    '길이(인치): 10" - 3 + 1 = 4"',
    r'길이(인치): 2" \times 3 + 1 = 7"',
    '길이(인치): 2" * 3 + 1 = 7"',                                     # 곱셈으로 쓴 ASCII '*'
    '길이(인치): 2" *3 + 1 = 7"',                                      # 닫는 '*' 가 없으면 붙여 써도 곱셈
    r"총 무게: 2[kg]*3 + 1 = 7[kg]\*",                                 # 이스케이프된 '\*' 는 닫는 강조가 아니다
])
def test_arrow_limit_and_cut_operand_equations_are_held(answer):
    case = evaluate("계산해줘.", answer)
    assert rows(case, "3-1.answer_equation") == []
    assert any(s["check_id"] == "3-1.answer_equation:skipped" for s in case["meta"]["skipped_conditions"])


@pytest.mark.parametrize("answer,verdict", [
    ("- 3 + 4 = 7", "PASS"),          # 줄머리 불릿은 연산자가 아니다
    ("- 3 + 4 = 8", "FAIL"),
    ("   - 3 + 4 = 8", "FAIL"),       # 들여쓴 불릿
    ("설명 *3 + 4 = 8*", "FAIL"),     # 기울임을 여는 * 는 곱셈이 아니다 — 틀린 등식이 빠지지 않는다
    ("계산 결과:*3 + 4 = 8*", "FAIL"),  # 콜론 뒤에 붙은 기울임도
    ("설명 *3 + 4 = 8 개*", "FAIL"),    # 단위 앞 공백이 있는 기울임
    ("설명 *3 + 4 = **8***", "FAIL"),   # 중첩 강조
    ("1. *3 + 4 = 8*", "FAIL"),         # 목록 번호는 앞 피연산자가 아니다
    ("[1] *3 + 4 = 8*", "FAIL"),        # 대괄호 표지도
    ("[1]*3 + 4 = 8*", "FAIL"),         # 붙여 쓴 강조도 닫는 '*' 가 있으면 강조
    ("[1] *3 + 4 = 8 이므로 답은 8입니다.*", "FAIL"),   # 닫는 '*' 가 같은 줄 멀리 있어도 강조
    ("계산 1: +3 + 4 = 8", "FAIL"),     # 라벨 뒤 단항 부호는 잘린 이항 연산자가 아니다
    ("1번: +3 + 4 = 8", "FAIL"),
    ("총 185 / 5 = 37분입니다.", "PASS"),  # 한도 표현이 없으면 나눗셈
])
def test_bullet_and_plain_division_equations_still_judged(answer, verdict):
    assert row(evaluate("계산해줘.", answer), "3-1.answer_equation")["verdict"] == verdict


def test_sentence_end_before_closing_emphasis():
    answer = "10월 20일 오후 1시부터 오후 3시까지 **도서 대출과 반납이 중단됩니다.**\n열람실은 정상 운영합니다.\n이용에 참고해 주시기 바랍니다."
    result = row(evaluate("안내문을 세 문장으로 작성해줘.", answer), "2-1.count:sentence_count")
    assert result["verdict"] == "PASS" and result["measured"]["value"] == 3
    quoted = row(evaluate("두 문장으로 써줘.", '그가 "좋다." 말했다. 끝났다.'), "2-1.count:sentence_count")
    assert quoted["measured"]["value"] == 2   # 따옴표 안 마침표는 문장 끝이 아니다


@pytest.mark.parametrize("answer,held", [
    ("총 무게: 2[kg]*3 + 1 = 7[kg]. 표시 기호는 `*`입니다.", True),   # 인라인 코드 속 '*' 는 닫는 강조가 아님 → 곱셈
    ("총 무게: 2[kg]*3 + 1 = 7[kg]. 표시 기호는 ``*``입니다.", True),  # 백틱 두 개로 감싼 인라인 코드도
    ("[1] *3 + 4 = 8\\\\*", False),                                    # 역슬래시 두 개 뒤 '*' 는 실제 닫는 강조 → 그대로 검사
])
def test_emphasis_ignores_inline_code_and_counts_backslash_parity(answer, held):
    case = evaluate("계산해줘.", answer)
    assert (rows(case, "3-1.answer_equation") == []) is held
