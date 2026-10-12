"""라우팅 구두점과 긴 대화 이력 경계의 회귀 검사. 실제 의미 평가 엔진은 호출하지 않는다."""

from __future__ import annotations

import pytest

from code_eval.jev_applicability import jev_applicability
from code_eval.jev_client import JevCall, JevClientConfig
from code_eval.jev_eval import build_history, evaluate_jev_batch


class RecordingJev:
    config = JevClientConfig()

    def __init__(self):
        self.requests = []

    def system_one(self, state, questions):
        self.requests.append({"state": state, "questions": questions})
        return JevCall("OK", body={"answers": {
            cid: {"choice": "PASS", "confidence": 0.8, "probabilities": {"PASS": 0.9, "FAIL": 0.1}}
            for cid in questions
        }})


def _case(turn, question, answer="답", case_id=None):
    return {"case_id": case_id or f"T{turn}", "case_status": "EVALUATED", "question": question,
            "answer": answer, "conversation": {"id": "C1", "turn": turn}}


@pytest.mark.parametrize("ending", ["", ".", "!", "!!", "?!", "!?"])
def test_request_punctuation_keeps_semantic_criteria_in_the_actual_request(ending):
    question = "초등학생도 이해할 수 있게 지구가 둥근 이유를 단계별로 설명해줘" + ending
    client = RecordingJev()
    result = evaluate_jev_batch({"cases": [_case(1, question)]}, client)

    assert result["records"][0]["criteria"] == ["J1", "J2", "J3", "J4"]
    assert list(client.requests[0]["questions"]) == ["J1", "J2", "J3", "J4"]


def test_exclamation_does_not_turn_statements_or_quoted_material_into_requests():
    assert jev_applicability("초등학생도 이유와 단계를 알고 있어! 인사해줘!")["criteria"] == ["J1"]
    quoted = '"초등학생도 이해할 수 있게 이유를 단계별로 설명해줘!"를 영어로 번역해줘!'
    assert jev_applicability(quoted)["criteria"] == ["J1"]
    assert jev_applicability("그 이유가 뭘까?!")["criteria"] == ["J1", "J3"]


def test_history_preserves_questions_beyond_the_former_60000_character_limit():
    for length in [59_999, 60_000, 70_000]:
        question = "q" * length
        history, truncated = build_history([(1, question, "a")])
        assert history == [{"turn": 1, "question": question, "answer": "a"}]
        assert not truncated


def test_history_keeps_whitespace_code_and_literal_omission_text_unmodified():
    question = "  q\r\n" + "q" * 60_000 + "  "
    answer = "  ```json\r\n{\"value\":\"\\n\"}\r\n```\n" + "가" * 3000 + " … [원문 생략]  \r\n"
    history, truncated = build_history([(1, question, answer)])
    assert not truncated
    assert history == [{"turn": 1, "question": question, "answer": answer}]
    assert "truncated" not in history[0]


def test_long_history_preserves_older_and_recent_answers_identically():
    prior = [(turn, f"질문 {turn}", "가" * 3000) for turn in range(1, 41)]
    history, truncated = build_history(prior)

    assert not truncated
    assert history == [{"turn": t, "question": q, "answer": a} for t, q, a in prior]
    assert sum(len(it["question"]) + len(it["answer"]) for it in history) > 120_000


def test_many_short_turns_are_sent_in_full_above_the_former_limit():
    prior = [(turn, "q", "a" * 300) for turn in range(1, 201)]
    history, truncated = build_history(prior)
    assert not truncated
    assert history == [{"turn": t, "question": q, "answer": a} for t, q, a in prior]


def test_large_history_keeps_j6_and_sends_every_original_question():
    client = RecordingJev()
    # 3턴의 과거 질문·답변 합계는 옛 60,000자 상한을 넘지만 전문을 보낸다.
    cases = [_case(1, "가" * 30000), _case(2, "나" * 30000), _case(3, "앞 대화를 요약해줘.")]
    result = evaluate_jev_batch({"cases": cases}, client, workers=1)

    second, third = result["records"][1:]
    assert second["decisions"]["J6"]["decision"] == "PASS"
    assert third["decisions"]["J6"]["decision"] == "PASS"
    assert third["status"] == "SUCCESS"
    assert third["context"]["history_complete"] is True
    assert third["context"]["history_turns"] == 2 and third["context"]["history_chars"] == 60_002
    assert not third["context"]["history_truncated"] and not third["context"]["issues"]
    assert result["conversations"]["items"][0]["result"] == "PASS"
    assert result["conversations"]["incomplete"] == 0 and result["conversations"]["pass"] == 1
    assert client.requests[1]["state"]["history"][0]["question"] == cases[0]["question"]
    assert client.requests[2]["state"]["history"] == [
        {"turn": c['conversation']['turn'], "question": c['question'], "answer": c['answer']} for c in cases[:2]]


def test_conversation_turn_links_keep_original_batch_indices_with_duplicate_case_ids():
    cases = [_case(2, "이어서 설명해줘.", case_id="same"), _case(1, "인사해줘.", case_id="same")]
    result = evaluate_jev_batch({"cases": cases}, RecordingJev(), workers=1)

    turns = result["conversations"]["items"][0]["turn_results"]
    assert [(it["turn"], it["case_id"], it["case_index"]) for it in turns] == [(1, "same", 1), (2, "same", 0)]
