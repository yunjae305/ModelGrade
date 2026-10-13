"""J8 첨부파일 활용 — 첨부 입력·적용 판정·평가 모델에 보내는 내용·인용 대조. 모델·네트워크 호출 없음."""
from code_eval import engine, semantic_quotes as sq
from code_eval.config import AionaConfig
from code_eval.jev_applicability import jev_applicability, sub_applicability
from code_eval.jev_eval import jev_request
from code_eval.semantic_items import SPECS_BY_ID
from code_eval.semantic_reasons import fail_evidence

QUESTION = "첨부한 견적서에서 합계 금액이랑 노트북 단가 알려줘."
ANSWER = "합계 금액은 1,200,000원이고, 노트북 단가는 1,200,000원입니다."
PDF = {"name": "견적서.pdf", "type": "PDF", "content": "노트북 | 2 | 600,000 | 1,200,000\n합계 | | | 1,250,000",
       "sha256": "ab12", "size": 2048, "path": "data/attachments/ab12.pdf"}


def test_engine_keeps_attachments_for_the_judge_and_rejects_malformed_ones():
    case = engine.evaluate_batch([{"case_id": "q", "question": QUESTION, "answer": ANSWER, "attachments": [PDF]}],
                                 AionaConfig())["cases"][0]
    assert case["case_status"] == "EVALUATED" and case["attachments"][0]["type"] == "pdf"   # 종류는 소문자로
    bad = engine.evaluate_batch([{"case_id": "q", "question": QUESTION, "answer": ANSWER, "attachments": [{"name": "", "type": "pdf"}]}],
                                AionaConfig())["cases"][0]
    assert bad["case_status"] == "INPUT_ERROR" and "attachments 1번" in bad["input_error"]
    plain = engine.evaluate_batch([{"case_id": "q", "question": QUESTION, "answer": ANSWER}], AionaConfig())["cases"][0]
    assert "attachments" not in plain   # 첨부가 없으면 키를 두지 않는다 (기존 결과 해시 그대로)


def test_j8_applies_only_with_attachments_and_the_judge_gets_name_type_content_only():
    with_file = jev_applicability(QUESTION, ANSWER, attachments=[PDF])
    assert "J8" in with_file["criteria"] and with_file["evidence"]["J8"]["matches"] == ["견적서.pdf (PDF)"]
    without = jev_applicability(QUESTION, ANSWER)
    assert "J8" not in without["criteria"] and without["not_applicable"]["J8"] == "질문에 첨부파일이 없음"
    subs = sub_applicability(QUESTION, attachments=[PDF])
    assert [subs[s]["items"] for s in ("J8-1", "J8-2", "J8-3")] == [["J8-1.a"], ["J8-2.a"], ["J8-3.a"]]
    assert subs["J8-1"]["model_applies"] and "model_applies" not in subs["J8-3"]   # 열 수 없다고 밝히면 J8-1·J8-2 는 해당 없음
    state = jev_request(QUESTION, ANSWER, ("J1", "J8"), attachments=[PDF])["state"]
    assert state["attachments"] == [{"name": "견적서.pdf", "type": "PDF", "content": PDF["content"]}]   # 경로·지문은 보내지 않는다


def test_j8_quotes_may_come_from_the_attachment_but_a_fail_needs_an_answer_quote():
    state = {"question": QUESTION, "answer": ANSWER, "attachments": [{"name": "견적서.pdf", "type": "pdf", "content": PDF["content"]}]}
    qs = {"J8-2.a": SPECS_BY_ID["J8-2.a"].to_prompt_dict({})}
    item = {"id": "J8-2.a", "decision": "NO", "answer_quote": "합계 금액은 1,200,000원이고", "question_quote": "합계 | | | 1,250,000", "reason": "r"}
    checked, failures = sq.verify_items({"items": [item]}, state, qs)
    assert failures == [] and checked["items"][0]["quote_check"]["question"]["source"] == "attachment"
    only_file = fail_evidence({"decision": "FAIL", "reason": "첨부의 '합계 | | | 1,250,000' 을 읽지 못함"}, state, "J8")
    assert not only_file["valid"] and only_file["problems"] == ["answer_quote_missing"]
    both = fail_evidence({"decision": "FAIL", "reason": "첨부의 '합계 | | | 1,250,000' 대신 '합계 금액은 1,200,000원이고' 라고 답함"}, state, "J8")
    assert both["valid"] and {q["found_in"] for q in both["matched_quotes"]} == {"attachment", "answer"}
