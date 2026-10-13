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
