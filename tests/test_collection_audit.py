"""수집 재현성 점검 — 성공·실패·미요청, 응답 모델명, 요청 시각·호출 방식 기록 유무 (API 호출 없음)."""
from tools.collection_audit import audit


def test_audit_counts_failures_and_reports_missing_records_honestly():
    ok = {"status": "SUCCESS", "finish_reason": "completed", "requested_at": "2026-10-13T01:00:00+00:00",
          "raw": {"model": "gpt-x-2026", "usage": {"input_tokens": 3}}}
    run = {"run_id": "r", "models": {"gpt": "gpt-x"}, "datasets": {
        "aiona": [{"case_id": "Q1", "import_source": "file"}],
        "gpt": [{"collection": ok}, {"collection": {"status": "ERROR", "error": "HTTP 529", "requested_at": "2026-10-13T01:00:05+00:00"}},
                {"collection": {"status": "ERROR", "error": "한도", "not_requested": True}}]},
        "collection_execution": {"request": {"access_mode": "api", "parameters": {"max_output_tokens": 8}}}}
    report = audit(run)
    gpt = report["providers"]["gpt"]
    assert gpt["status"] == {"SUCCESS": 1, "ERROR": 1, "NOT_REQUESTED": 1} and gpt["errors"] == {"HTTP 529": 1, "한도": 1}
    assert gpt["access_mode"] == "api" and gpt["response_models"] == {"gpt-x-2026": 1} and gpt["configured_model"] == "gpt-x"
    assert gpt["requested_at"] == {"first": "2026-10-13T01:00:00+00:00", "last": "2026-10-13T01:00:05+00:00", "rows": 2}
    assert (gpt["raw_saved"], gpt["usage_saved"]) == (1, 1)
    assert report["providers"]["aiona"]["source"] == "업로드 원문 (수집 아님)"
    old = audit({"datasets": {"gpt": [{"collection": {"status": "SUCCESS"}}]}})
    assert old["request_conditions"] is None and old["providers"]["gpt"]["access_mode"] == "기록 없음"
    assert old["providers"]["gpt"]["requested_at"] == "기록 없음"


def test_conversation_inputs_rebuild_each_request_and_flag_a_shrinking_input():
    """지시서 10-14 P0-2 — 같은 대화의 모델별 입력을 원문 없이 비교: 메시지 수·자기 답변 글자·토크나이저 비율·입력 감소."""
    from tools.collection_audit import conversation_inputs
    def row(turn, answer, tokens):
        return {"conversation_id": "C", "turn": turn, "question": f"{turn}번째 질문", "answer": answer,
                "collection": {"status": "SUCCESS", "raw": {"usage": {"input_tokens": tokens}}}}
    run = {"run_id": "r", "datasets": {"aiona": [], "gpt": [row(1, "짧다", 10), row(2, "짧다", 20), row(3, "짧다", 30)],
                                        "claude": [row(1, "아주 길게 쓴 답변" * 20, 16), row(2, "길다", 300), row(3, "길다", 40)]}}
    report = conversation_inputs(run)
    gpt, claude = report["conversations"]["C"]["gpt"], report["conversations"]["C"]["claude"]
    assert gpt["complete"] and gpt["last"]["messages"] == 5 and gpt["last"]["roles"] == {"user": 3, "assistant": 2}
    assert claude["last"]["own_answer_chars"] > gpt["last"]["own_answer_chars"]   # 같은 질문, 자기 답변 길이가 다름
    assert gpt["input_drops_at_turns"] == [] and claude["input_drops_at_turns"] == [3]   # 제공자 집계가 줄면 이력 누락·잘림 의심
    assert gpt["last"]["ratio"] and "1번째 질문" not in str(report)   # 원문 없이 길이·해시만
    assert report["system_prompt"] is None and report["tools"] == []
