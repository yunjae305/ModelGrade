"""General regression cases for CODE evaluation refinement; no production ID rules."""

from __future__ import annotations

import pytest

from code_eval.engine import evaluate_case


def check(question: str, answer: str):
    return evaluate_case({"question": question, "answer": answer})


def result(case, prefix: str):
    return next(r for r in rows(case, prefix))


def rows(case, prefix: str):
    # 답변 구조 검사의 '검사 대상 없음' PASS 행(:none, 엔진 v16.5.0)은 대상이 있을 때의 판정과 무관하므로 뺀다
    return [r for r in case["results"] if r["check_id"].startswith(prefix) and not r["check_id"].endswith(":none")]


def skipped(case, prefix: str):
    return [s for s in case["meta"]["skipped_conditions"] if s["check_id"].startswith(prefix)]


def test_corruption_shown_as_inline_example_is_not_output_damage():
    case = check("문자 인코딩이 잘못되면 어떤 일이 생기는지 설명해줘.",
                 "인코딩이 잘못되면 `�` 같은 대체 문자가 나타날 수 있습니다.")
    assert result(case, "1-1.character_rules")["verdict"] == "PASS"


def test_unexplained_corruption_in_prose_remains_fail():
    case = check("정상적인 안내 문장을 작성해줘.", "정상적인 설명 문장 중간에 이유 없이 � 문자가 섞여 있습니다.")
    assert result(case, "1-1.character_rules")["verdict"] == "FAIL"


@pytest.mark.parametrize("answer,expected", [
    ("`���`\n현재 인코딩으로 해석할 수 없는 바이트가 있어 대체 문자로 바뀐 경우", "PASS"),
    ("현재 인코딩 오류에서 나타나는 대체 문자 예시입니다.\n`���`", "PASS"),
    ("파일을 읽은 결과는 `�`입니다. 인코딩이 깨졌습니다.", "FAIL"),
    ("예를 들어 깨진 문자는 �처럼 표시될 수 있습니다.", "PASS"),
    ("```text\n�\n```\n\n이 문자는 인코딩 오류에서 나타나는 대체 문자 예시입니다.", "PASS"),
    ("다음은 깨진 문자 예시입니다.\n- `�`\n- `��`", "PASS"),
    ("| 표기 | 설명 |\n|---|---|\n| � | 대체 문자 |", "PASS"),
    ("안내를 시작합니다.\n\n`�`\n\n다음 안내입니다.", "FAIL"),
    ("- 실행 결과는 �입니다.", "FAIL"),
])
def test_corruption_counts_as_example_only_with_structural_context(answer, expected):
    case = check("안내 문구를 작성해줘.", answer)
    assert result(case, "1-1.character_rules")["verdict"] == expected


def test_question_about_encoding_lets_answer_show_broken_characters():
    case = check("UTF-8 파일을 EUC-KR로 열면 글자가 어떻게 보이는지 설명해줘.", "한글이 ��처럼 보입니다.")
    assert result(case, "1-1.character_rules")["verdict"] == "PASS"


@pytest.mark.parametrize("question,answer,expected", [
    ("회의 일정을 알려줘.", "회의는 3시입니다. 会議 장소는 2층입니다.", "FAIL"),
    ("회의 일정을 알려줘.", "회의(會議)는 3시입니다.", "PASS"),
    ("사자성어 하나를 알려줘.", "일석이조는 一石二鳥라고 씁니다. 한 번에 두 가지 이익을 얻는다는 뜻입니다.", "PASS"),
    ("안부 인사를 해줘.", "안녕하세요. Привет. 잘 지내세요.", "FAIL"),
    ("러시아어 인사를 알려줘.", "Привет는 안녕이라는 뜻입니다.", "PASS"),
    ("원주율을 알려줘.", "원주율 π는 약 3.14입니다.", "PASS"),
])
def test_foreign_script_needs_question_topic_explanation_or_gloss(question, answer, expected):
    assert result(check(question, answer), "1-1.foreign_script")["verdict"] == expected


def test_foreign_script_defers_to_explicit_charset_condition():
    case = check("한글만 사용해서 인사해줘.", "안녕하세요 こんにちは")
    assert rows(case, "1-1.foreign_script") == []  # 허용 문자 조건이 있으면 2-1.charset 이 판정한다
    assert result(case, "2-1.charset")["verdict"] == "FAIL"


def test_text_fence_example_is_exempt_but_python_code_is_not():
    example = check("문자 깨짐 예시를 설명해줘.", "깨진 문자 예시:\n\n```text\n�\n```")
    assert result(example, "1-1.character_rules")["verdict"] == "PASS"
    code = check("파이썬 코드를 작성해줘.", "```python\nprint('�')\n```")
    assert result(code, "1-1.character_rules")["verdict"] == "FAIL"


def test_declared_numbered_headings_exclude_following_explanation_and_link_question_count():
    answer = ("다음 세 가지 문구를 사용할 수 있습니다.\n\n"
              "### 문구 1: 기본형\n첫 안내입니다.\n\n"
              "### 문구 2: 친절형\n둘째 안내입니다.\n\n"
              "### 문구 3: 간단형\n셋째 안내입니다.\n\n"
              "### 표현 선택 기준\n- 첫째\n- 둘째\n- 셋째")
    case = check("오류 안내 문구를 세 가지 작성해줘.", answer)
    question_count = result(case, "2-1.count:list_count")
    declared = result(case, "3-2.declared_count")
    assert question_count["verdict"] == "PASS"
    assert question_count["measured"]["structure"] == "numbered_headings"
    assert len([e for e in question_count["evidence"] if e["target"] == "answer"]) == 3
    assert declared["verdict"] == "PASS" and declared["measured"]["count"] == 3
    assert "표현 선택 기준" in str(declared["details"].get("excluded_next_heading"))


def test_unrelated_bullets_cannot_satisfy_requested_message_count():
    case = check("안내 문구를 세 가지 작성해줘.",
                 "### 선택 기준\n- 빠름\n- 짧음\n- 쉬움")
    assert rows(case, "2-1.count:list_count") == [] and skipped(case, "2-1.count:list_count")


def test_interrupted_numbered_heading_sequence_remains_undecided():
    answer = "다음 세 가지입니다.\n### 문구 1\n첫째\n### 참고\n부연\n### 문구 2\n둘째\n### 문구 3\n셋째"
    case = check("안내 문구를 세 가지 작성해줘.", answer)
    assert rows(case, "2-1.count:list_count") == [] and skipped(case, "2-1.count:list_count")
    assert rows(case, "3-2.declared_count") == [] and skipped(case, "3-2.declared_count")


def test_real_python_syntax_error_remains_fail():
    answer = ("### 함수\n\n```python\n"
              "def remove_duplicates(numbers: list[int]) -> list[int]:\n"
              " seen = set()\n result = []\n\n"
              " for number in numbers:\n if number not in seen:\n"
              " seen.add(number)\n result.append(number)\n\n return result\n```")
    case = check("정수 목록에서 중복을 제거하되 처음 등장한 순서를 유지하는 파이썬 함수를 작성해줘.", answer)
    assert result(case, "1-2.python_syntax:block1")["verdict"] == "FAIL"


def test_net_income_word_does_not_mark_equation_as_mistake_example():
    case = check("월 실수령액이 300만 원인데 지출 합계를 계산해줘.",
                 "월 실수령액에서 비용을 빼면 됩니다.\n80 + 40 + 10 + 5 + 15 = 150")
    assert result(case, "3-1.answer_equation:1")["verdict"] == "PASS"
    assert len(rows(case, "3-1.answer_equation")) == 1 and skipped(case, "3-1.answer_equation") == []


def test_premise_count_is_not_output_sentence_requirement():
    case = check("다음 두 문장으로부터 결론을 설명해줘. 첫째는 A다. 둘째는 B다.", "결론입니다.")
    assert not any(r["check_id"] == "2-1.count:sentence_count" for r in case["results"])


def test_numbered_headings_are_primary_items_before_internal_numbered_list():
    answer = ("### 1. 속도 게임\n설명\n\n### 2. 카드 게임\n설명\n\n"
              "### 3. 숫자 게임\n설명\n\n### 제작 순서\n1. 화면 만들기\n2. 규칙 만들기")
    case = check("게임 아이디어를 세 개 제안해줘.", answer)
    row = result(case, "2-1.count:list_count")
    assert row["verdict"] == "PASS" and row["measured"]["structure"] == "numbered_headings"


def test_one_sentence_summary_is_whole_answer_requirement():
    case = check("다음 문장을 핵심만 남겨 한 문장으로 요약해줘. “바람이 분다.”", "바람이 붑니다.")
    assert result(case, "2-1.count:sentence_count")["verdict"] == "PASS"


def test_concise_sentence_does_not_imply_exactly_one_sentence():
    case = check("이 표현을 뜻은 유지하면서 간결한 문장으로 고쳐줘.", "짧게 고쳤습니다.")
    assert not any(r["check_id"] == "2-1.count:sentence_count" for r in case["results"])


def test_declaration_connects_to_immediately_following_bullets():
    case = check("병합을 설명해줘.", "다음 세 가지를 기준으로 봅니다.\n\n- 조상\n- 현재\n- 대상")
    row = result(case, "3-2.declared_count")
    assert row["verdict"] == "PASS" and row["measured"]["count"] == 3


def test_unrelated_partial_marker_inside_word_does_not_block_declaration():
    case = check("참여 방식을 설명해줘.", "이 문제에 집중하면 다음 세 가지를 볼 수 있습니다.\n\n- 시점\n- 방식\n- 정보")
    assert result(case, "3-2.declared_count")["verdict"] == "PASS"


def test_numeric_square_and_implicit_numeric_multiplication_are_recomputed():
    case = check("검산해줘.", "2^2-5(2)+6=4-10+6=0\n3²-5(3)+6=9-15+6=0")
    equations = [r for r in case["results"] if r["check_id"].startswith("3-1.answer_equation")]
    assert len(equations) == 2 and all(r["verdict"] == "PASS" for r in equations)


def test_function_behavior_without_source_data_does_not_activate_transform():
    case = check("정수 목록에서 중복을 제거하되 처음 등장한 순서를 유지하는 함수를 작성해줘.",
                 "def unique(values):\n    return values")
    assert not any(r["check_id"] == "3-3.transform" for r in case["results"])


@pytest.mark.parametrize("question", [
    "중간 휴식을 15분에서 20분으로 바꿀게. 시작 시각과 선택한 영화, 상영 순서는 유지해줘. 시간표를 수정하고 끝나는 시각을 알려줘.",
    "영화 G의 상영시간도 120분으로 바꿔줘. 상영 순서를 유지해서 시간표를 다시 짜줘.",
])
def test_schedule_order_preservation_in_conversation_makes_no_transform_row(question):
    # 대화 속 '순서 유지'는 보존 표현이지만 질문에 변환할 원본(JSON 배열·표·목록)이 없다 — 조건 미생성(행·skipped 없음)
    case = check(question, "1회차 10:00 영화 A\n2회차 12:20 영화 G\n끝나는 시각은 14:40입니다.")
    assert not rows(case, "3-3.") and not skipped(case, "3-3.")
    assert not [c for c in case["meta"]["question_conditions"] if c["kind"] == "transform"]


@pytest.mark.parametrize("answer", [
    "- 빈 문자열은 제외합니다.\n- 입력 순서를 유지합니다.\n- 중복은 처음 것만 남깁니다.\n\n```python\n"
    "def keep(words):\n    return [w for w in words if w]\n```",
    "```python\ndef keep(words):\n    return [w for w in words if w]\n```",
])
def test_function_request_with_example_input_array_does_not_activate_transform(answer):
    # 함수 작성 질문의 예시 배열은 변환 원본이 아니다 — 설명 불릿을 변환 결과로 잡던 거짓 FAIL·미검사 재발 방지
    question = ('영문 소문자 문자열 목록을 받아 빈 문자열을 제외한 목록을 돌려주는 파이썬 함수를 작성해줘. '
                '입력 순서를 유지하고 중복은 제거해줘. 입력 예시는 ["cat", "a", "dog", ""]야.')
    case = check(question, answer)
    assert not rows(case, "3-3.") and not skipped(case, "3-3.")
    assert not [c for c in case["meta"]["question_conditions"] if c["kind"] in ("transform", "unique_values", "aggregate")]
    assert any(c["kind"] == "python_code_requested" for c in case["meta"]["question_conditions"])
