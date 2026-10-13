"""이전 턴의 지속 형식·분량 지시를 다음 턴 CODE 검사에 이어받는다 (G-3, 모델 호출 없음)."""
from __future__ import annotations

import pytest

from code_eval.config import load_aiona_config
from code_eval.engine import evaluate_batch, evaluate_case
from code_eval.extract import extract_conditions, persistent_conditions

NO_TABLE = "한국에는 봄, 여름, 가을, 겨울이 있습니다."
TABLE = "| 계절 | 특징 |\n|---|---|\n| 봄 | 따뜻함 |"


def conversation(*turns):
    """(사용자 질문, AI 답변) 목록 → 대화 입력."""
    return [{"case_id": f"T{i}", "conversation_id": "c1", "turn": i, "question": q, "answer": a}
            for i, (q, a) in enumerate(turns, 1)]


def last_case(*turns):
    cfg = load_aiona_config(); cfg.enabled = False
    return evaluate_batch(conversation(*turns), cfg)["cases"][-1]


def rows(case, prefix):
    return [r for r in case["results"] if r["check_id"].startswith(prefix)]


def test_table_instruction_from_turn_one_is_checked_in_turn_two():
    case = last_case(("앞으로 모든 답변은 표 형식으로 작성해 줘.", TABLE), ("한국의 사계절을 설명해 줘.", NO_TABLE))
    [row] = rows(case, "2-2.table")
    assert row["verdict"] == "FAIL" and row["source"] == "history_instruction"
    assert row["details"]["inherited_from"]["turn"] == 1 and "1턴 지시 유지" in row["title"]
    assert row["evidence"][0]["target"] == "history" and row["explain"]["requirement"].startswith("(1턴 사용자 지시 유지)")
    assert case["meta"]["inherited_conditions"] == [{"turn": 1, "instruction": "앞으로 모든 답변은 표 형식으로 작성해 줘.",
                                                     "requirement": "표 형식으로 작성", "kind": "table_required",
                                                     "applied": True}]


def test_long_conversation_keeps_a_distant_instruction():
    turns = [("앞으로 답변은 2문장 이내로 작성해줘.", "알겠습니다.")]
    turns += [(f"{i}번째 질문에 답해줘.", "짧게 답합니다.") for i in range(2, 12)]
    turns.append(("봄을 설명해줘.", "봄은 따뜻합니다. 꽃이 핍니다. 바람이 붑니다."))
    case = last_case(*turns)
    [row] = rows(case, "2-1.count:sentence_count")
    assert row["verdict"] == "FAIL" and row["details"]["inherited_from"]["turn"] == 1


@pytest.mark.parametrize("turns,expected", [
    # 해제 · 변경 · 충돌(뒤 지시 우선) · 이번만 · 현재 질문 우선
    ([("앞으로 모든 답변은 표로 작성해 줘.", TABLE), ("이제 표로 안 해도 돼. 봄을 설명해줘.", NO_TABLE),
      ("여름을 설명해줘.", NO_TABLE)], []),
    ([("앞으로 3문장 이내로 답해줘.", "네."), ("앞으로는 5문장 이내로 답해줘.", "네."), ("봄을 설명해줘.", "a. b. c. d.")],
     [("2-1.count:sentence_count", "PASS", 2)]),
    ([("앞으로 표로 정리해줘.", TABLE), ("앞으로 표는 쓰지 마.", "네."), ("봄을 설명해줘.", NO_TABLE)],
     [("2-2.table:forbidden", "PASS", 2)]),
    ([("앞으로 3문장 이내로 답해줘.", "네."), ("이번에만 5문장으로 길게 설명해줘.", "a. b. c. d. e."),
      ("봄을 설명해줘.", "a. b. c. d.")], [("2-1.count:sentence_count", "FAIL", 1)]),
    ([("앞으로 모든 답변은 표로 작성해 줘.", TABLE), ("봄을 목록으로 정리해줘.", "- 따뜻함")], []),
])
def test_cancel_change_conflict_once_and_current_priority(turns, expected):
    case = last_case(*turns)
    inherited = [(r["check_id"], r["verdict"], r["details"]["inherited_from"]["turn"]) for r in case["results"]
                 if r["source"] == "history_instruction"]
    assert inherited == expected


@pytest.mark.parametrize("first", [
    "표로 정리해줘.",                                                   # 지속 표현이 없는 한 턴 요청
    "친구가 \"앞으로 모든 답변은 표로 해\"라고 했는데 맞는 말이야?",          # 과거 발언의 단순 인용
    "봄과 여름의 차이를 알려줘.",                                         # 형식 요구가 없는 일반 대화
])
def test_non_persistent_or_quoted_requests_are_not_inherited(first):
    case = last_case((first, "앞으로 모든 답변을 표로 정리해 드릴게요."), ("봄을 설명해줘.", NO_TABLE))
    assert not [r for r in case["results"] if r["source"] == "history_instruction"]


def test_assistant_promise_is_not_a_user_instruction():
    # 이전 AI 답변에만 있는 '앞으로 표로' 약속은 사용자 지시가 아니다.
    case = last_case(("봄을 설명해줘.", "앞으로 모든 답변은 표 형식으로 작성하겠습니다."), ("여름을 설명해줘.", NO_TABLE))
    assert "inherited_conditions" not in case["meta"]


def test_task_scoped_instruction_applies_only_to_that_task():
    first = ("앞으로 요약할 때는 3줄로 해줘.", "네.")
    applied = last_case(first, ("이 글을 요약해줘: 봄은 따뜻하다.", "봄은\n따뜻하고\n꽃이\n핀다"))
    skipped = last_case(first, ("봄을 설명해줘.", "봄은\n따뜻하고\n꽃이\n핀다"))
    assert [r["verdict"] for r in rows(applied, "2-1.count:line_count")] == ["FAIL"]
    assert rows(skipped, "2-1.count:line_count") == []
    assert skipped["meta"]["inherited_conditions"][0]["why"] == "'요약' 작업에만 적용하는 지시"


def test_five_turn_literal_instruction_and_missing_turn_is_not_guessed():
    turns = [("앞으로 답변 끝은 '감사합니다.'로 끝내줘.", "네. 감사합니다.")]
    turns += [(f"질문 {i}", f"답 {i}. 감사합니다.") for i in range(2, 5)]
    turns.append(("봄을 설명해줘.", "봄은 따뜻합니다."))
    case = last_case(*turns)
    assert [r["verdict"] for r in rows(case, "2-1.literal:ends_with")] == ["FAIL"]
    gapped = conversation(*turns)
    del gapped[2]   # 3턴 누락 — 그 사이 해제 지시가 있었는지 알 수 없다
    cfg = load_aiona_config(); cfg.enabled = False
    case = evaluate_batch(gapped, cfg)["cases"][-1]
    assert rows(case, "2-1.literal:ends_with") == []
    assert case["meta"]["inherited_conditions"][0]["applied"] is False


ENDING = "앞으로 모든 답변은 '감사합니다'로 끝내고 '참고용'이라는 문구를 넣어줘."
TABLE_ENDING = "앞으로 모든 답변은 표로 작성하고 '감사합니다'로 끝내줘."
TABLE_ONLY = "앞으로 모든 답변은 표로 작성해줘."


@pytest.mark.parametrize("prior,question,expected", [
    # 따옴표 문구를 가리킨 해제는 그 문구 지시만 지운다 (F3, G3-04)
    ([ENDING, "이제 '감사합니다'로 끝내지 않아도 돼."], "봄을 설명해줘.", [("required_literal", "참고용")]),
    ([ENDING, "'참고용' 문구는 이제 빼줘."], "봄을 설명해줘.", [("ends_with", "감사합니다")]),
    ([ENDING, "더 이상 '감사합니다'를 붙일 필요 없어."], "봄을 설명해줘.", [("required_literal", "참고용")]),
    ([TABLE_ENDING, "앞으로 '감사합니다'로 끝내지 마."], "봄을 설명해줘.", [("table_required", None)]),
    ([TABLE_ENDING, "끝맺음 문구는 이제 안 붙여도 돼."], "봄을 설명해줘.", [("table_required", None)]),
    # 대상 뒤에 '형식·제한'이 붙어도 그 대상만 해제한다
    ([TABLE_ENDING, "표 형식은 이제 안 써도 돼."], "봄을 설명해줘.", [("ends_with", "감사합니다")]),
    (["앞으로 300자 이내로 쓰고 표로 정리해줘.", "이제 글자 수 제한은 신경 쓰지 않아도 돼."], "봄을 설명해줘.",
     [("table_required", None)]),
    # 이번 턴만의 해제·예외는 그 턴에만 적용된다
    ([TABLE_ENDING], "이번 답변만 예외로 해줘. 봄을 설명해줘.", []),
    ([TABLE_ENDING, "이번 답변만 예외로 해줘. 봄을 설명해줘."], "여름을 설명해줘.",
     [("ends_with", "감사합니다"), ("table_required", None)]),
    ([TABLE_ONLY, "이번에는 표 안 써도 돼. 봄을 설명해줘."], "여름을 설명해줘.", [("table_required", None)]),
    # 인용 속 해제 표현 · '무시하지 마' 같은 부정문 · 다른 낱말은 해제가 아니다
    ([TABLE_ENDING, "다음 문장을 번역해줘: \"'감사합니다'로 끝내지 않아도 돼.\""], "봄을 설명해줘.",
     [("ends_with", "감사합니다"), ("table_required", None)]),
    ([TABLE_ONLY, "친구가 '이제 표로 안 해도 돼'라고 했는데 무슨 뜻이야?"], "봄을 설명해줘.", [("table_required", None)]),
    ([TABLE_ONLY, "그 조건을 무시하지 마."], "봄을 설명해줘.", [("table_required", None)]),
    ([TABLE_ONLY, "표현은 신경 쓰지 않아도 돼."], "봄을 설명해줘.", [("table_required", None)]),
    # 전체 취소 · 금지했던 문구 허용 · 5턴 넘게 지난 지시 해제
    ([TABLE_ENDING, "이전 조건은 취소해."], "봄을 설명해줘.", []),
    (["앞으로 '음'이라는 말은 쓰지 마.", "이제 '음'은 써도 돼."], "봄을 설명해줘.", []),
    (["앞으로 답변은 2문장 이내로 해줘."] + [f"{i}번 질문에 답해줘." for i in range(2, 7)]
     + ["이제 문장 수는 신경 쓰지 않아도 돼."], "봄을 설명해줘.", []),
])
def test_cancellation_removes_only_the_named_instruction(prior, question, expected):
    reqs, _ = persistent_conditions(list(enumerate(prior, 1)), question)
    assert sorted((r.kind, r.value.get("literal")) for r in reqs) == expected


def test_negated_ending_instruction_is_not_an_ending_requirement():
    # '…로 끝내지 마 / 끝내지 않아도 돼'를 그 문구로 끝내라는 요구로 읽지 않는다.
    for question in ("'감사합니다'로 끝내지 마. 봄을 설명해줘.", "이제 '이상입니다.'로 끝내지 않아도 돼."):
        assert not [r for r in extract_conditions(question).reqs if r.kind in ("ends_with", "starts_with")]
    assert [r.kind for r in extract_conditions("'감사합니다'로 끝내줘.").reqs] == ["ends_with"]


def test_single_case_evaluation_is_unchanged():
    case = evaluate_case({"question": "한국의 사계절을 설명해 줘.", "answer": NO_TABLE})
    assert "inherited_conditions" not in case["meta"] and not rows(case, "2-2.table")
