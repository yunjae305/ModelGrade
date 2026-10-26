"""빈 출력을 요구한 질문은 판정·수집 결과와 상관없이 우선 보류 (사용자 결정 2026-10-19, empty-output-hold-v1). 모델 호출 없음."""
from __future__ import annotations

from tools import export_review, question_gradient
from webapp import run_summary as rs

EMPTY_Q = "아무것도 출력하지 마."
TABLE_Q = "오늘 할 일을 표로 정리해줘."


def _case(check_id: str, verdict: str, case_id: str = "Q1_T1") -> dict:
    return {"case_id": case_id, "case_status": "EVALUATED", "summary": {"common": {"pass": int(verdict == "PASS"), "fail": int(verdict == "FAIL"),
                                                                                   "unjudged": 0}},
            "results": [{"area": "common", "counted": True, "check_id": check_id, "verdict": verdict}], "meta": {}}


def _record(decision: str = "PASS") -> dict:
    return {"status": "SUCCESS", "criteria": ["J1"], "decisions": {"J1": {"decision": decision}}}


def test_evaluated_empty_output_question_is_only_held_without_the_original_verdict():
    out = rs.question_outcome(_case("2-1.explicit_empty", "FAIL"), _record("FAIL"))
    assert out == {"outcome": "PENDING", "code_fails": [], "semantic_fails": [], "code_complete": False, "semantic_complete": False,
                   "hold": "empty_output", "hold_policy": "empty-output-hold-v2"}   # 원래 평가(FAIL 목록·판정)는 남기지 않는다
    assert rs.EMPTY_OUTPUT_HOLD_POLICY == "empty-output-hold-v2"
    assert rs.question_outcome(_case("2-2.table:table_required", "FAIL"), _record())["outcome"] == "FAIL"   # 다른 질문은 그대로


def test_collection_failure_on_an_empty_output_question_is_held_from_the_question_text():
    batch = {"cases": [{"case_id": "Q1_T1", "case_status": "INPUT_ERROR", "results": [], "meta": {}},
                       {"case_id": "Q2_T1", "case_status": "INPUT_ERROR", "results": [], "meta": {}}], "jev_eval": {"records": [None, None]}}
    rows = [{"case_id": "Q1_T1", "question": EMPTY_Q}, {"case_id": "Q2_T1", "question": TABLE_Q}]
    outcomes = rs.question_outcomes(batch, 2, rows)
    assert outcomes[0]["outcome"] == "PENDING" and outcomes[0]["hold"] == "empty_output"
    assert outcomes[1]["outcome"] == "EXCLUDED" and "hold" not in outcomes[1]
    # 붙여 넣은 자료 속 문장은 지시가 아니다 — 평가와 같은 조건 추출
    pasted = "[붙여 넣은 자료]\n아무것도 출력하지 마.\n[자료 끝]\n위 자료를 요약해줘."
    assert not rs.asks_empty_output(None, pasted) and rs.asks_empty_output(None, EMPTY_Q)


def test_a_held_turn_holds_the_whole_conversation_unit():
    assert rs.unit_verdict([{"outcome": "FAIL"}, {"outcome": "PENDING", "hold": "empty_output"}]) == "PENDING"
    assert rs.unit_verdict([{"outcome": "FAIL"}, {"outcome": "PENDING"}]) == "FAIL"


def test_gradient_verdicts_hold_empty_output_even_when_the_answer_was_not_collected():
    failed = {"status": "ERROR", "error": "AIONA 가 답하지 못했습니다"}
    run = {"providers": ["aiona", "gpt"], "question_set": {},
           "datasets": {p: [{"case_id": "Q1_T1", "question": EMPTY_Q, "collection": failed},
                            {"case_id": "Q2_T1", "question": TABLE_Q, "collection": failed if p == "gpt" else {"status": "SUCCESS"}}]
                        for p in ("aiona", "gpt")},
           "evaluations": {p: {"cases": [{"case_id": "Q1_T1", "case_status": "INPUT_ERROR", "results": []},
                                         _case("2-2.table:table_required", "PASS", "Q2_T1")],
                               "jev_eval": {"records": [None, _record()]}} for p in ("aiona", "gpt")}}
    verdicts = question_gradient.verdicts_from_run(run)
    assert verdicts["aiona"] == {1: "HOLD", 2: "PASS"}
    assert verdicts["gpt"] == {1: "HOLD", 2: "FAIL"}   # 빈 출력 요구가 아닌 수집 실패는 그대로 FAIL


def test_held_review_export_lists_empty_output_questions_for_a_person_to_decide():
    run = {"providers": ["aiona"], "datasets": {"aiona": [{"case_id": "Q1_T1", "question": EMPTY_Q, "answer": "",
                                                           "collection": {"status": "ERROR", "error": "오류 화면"}}]},
           "evaluations": {"aiona": {"cases": [{"case_id": "Q1_T1", "case_status": "INPUT_ERROR", "results": []}],
                                     "jev_eval": {"records": [None]}}}}
    rows = export_review.export_rows(run, held=True)
    assert len(rows) == 1 and rows[0]["cause"] == "empty_output_hold" and rows[0]["criterion"] == "2-1.explicit_empty"
    assert set(rows[0]) == set(export_review.COLUMNS) and rows[0]["judge_reason"] == "수집 ERROR: 오류 화면" and rows[0]["decision"] is None
    assert export_review.export_rows(run, held=False) == []   # 기준별 FAIL 내보내기에는 넣지 않는다
