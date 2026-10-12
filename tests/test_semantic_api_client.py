"""Responses evaluator transport tests. Every HTTP request uses MockTransport."""

import asyncio
import copy
import json
import threading
import time
from datetime import datetime

import httpx
import pytest

from code_eval.semantic_api_client import SemanticApiClient, SemanticApiConfig
from code_eval import semantic_cli_common as semantic
from code_eval.jev_eval import evaluate_jev_case, jev_request


def entries(*ids):
    return {cid: {"choice": "PASS", "confidence": 0.8, "reason": "질문·답변과 근거가 일치합니다."} for cid in ids}


def completed_response(values=None, *, events=(), annotations=(), **extra):
    values = entries("J1") if values is None else values
    return {"id": "response-fixture", "status": "completed", "model": "resolved-judge-v1",
            "output": [*events, {"type": "message", "role": "assistant", "status": "completed", "content": [
                {"type": "output_text", "text": json.dumps(values, ensure_ascii=False), "annotations": list(annotations)}]}],
            "usage": {"input_tokens": 150, "output_tokens": 60}, **extra}


def call_with_response(raw, *, state=None, ids=("J1",), config=None, status_code=200, source_fetcher=None):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status_code, json=raw)

    # These cases isolate one transport/validation pass. Recovery has its own
    # sequence-response coverage in test_semantic_recovery.py.
    client = SemanticApiClient(config or SemanticApiConfig(evidence_recovery_rounds=0), "sk-semantic-fixture-only",
                               transport=httpx.MockTransport(handler),
                               source_fetcher=source_fetcher or (lambda url: {"status": "fetch_error"}))
    state = {"question": "질문", "answer": "답변"} if state is None else state
    questions = jev_request("질문", "답변", ids)["questions"]
    return client.system_one(state, questions), requests


def test_responses_request_uses_semantic_key_pinned_model_and_preserves_all_original_inputs():
    state = {"question": "  질문\r\n😀  ", "answer": "  답변\r\n" + "가😀" * 40_000,
             "history": [{"turn": 1, "question": " 이전 질문\r\n", "answer": "```json\n{\"literal\":\"\\n\"}\n```  "}],
             "reference": {"source_url": "https://evidence.example/source", "as_of": "2026-10-09", "text": " 참조 원문\r\n"}}
    snapshot = copy.deepcopy(state)
    config = SemanticApiConfig(model="requested-judge-v1", max_output_tokens=9999, max_tool_calls=2, timeout=95)
    call, requests = call_with_response(completed_response(entries("J1", "J5", "J6")), state=state,
                                       ids=("J1", "J5", "J6"), config=config)
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST" and str(request.url) == "https://api.openai.com/v1/responses"
    assert request.headers["Authorization"] == "Bearer sk-semantic-fixture-only"
    body = json.loads(request.content)
    assert json.dumps(state, ensure_ascii=False, indent=1) in body["input"]
    assert len(body["input"]) > 60_000 and state == snapshot
    assert body["model"] == "requested-judge-v1" and body["reasoning"] == {"effort": "high"}
    assert body["max_output_tokens"] == 9999 and body["store"] is False and body["truncation"] == "disabled"
    assert body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["schema"]["required"] == ["J1", "J5", "J6"]
    assert body["instructions"]
    assert not {"tools", "tool_choice", "include", "previous_response_id", "conversation", "background", "stream"} & body.keys()
    assert call.status == "OK" and call.attempts == 1
    assert call.body["model"] == "resolved-judge-v1"
    assert call.body["answers"]["J1"]["probabilities"] == {"PASS": 0.9, "FAIL": 0.1}
    assert call.usage == {"input_tokens": 150, "output_tokens": 60}
    assert call.body["verification"]["search_performed"] is False
    assert call.body["verification"]["source_urls"] == []
    assert datetime.fromisoformat(call.body["verification"]["checked_at"]).utcoffset().total_seconds() == 0


def test_gpt_explicit_cache_preserves_all_six_criteria_and_long_original_data_outside_breakpoint():
    state = {"question": "  original question\r\n\U0001f600  ",
             "answer": "  original answer\r\n" + "\uac00\U0001f600" * 40_000,
             "history": [{"turn": 1, "question": " earlier question\r\n",
                          "answer": '```json\n{"literal":"\\n"}\n```  '}],
             "reference": {"text": " original reference\r\n", "as_of": "2026-10-12"}}
    snapshot = copy.deepcopy(state)
    ids = tuple(f"J{index}" for index in range(1, 7))
    call, requests = call_with_response(completed_response(entries(*ids)), state=state, ids=ids,
                                       config=SemanticApiConfig(reasoning_effort="medium"))
    assert call.status == "OK" and len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert payload["instructions"] == semantic.system_prompt(search_enabled=False)
    assert len(payload["input"]) == 1 and payload["input"][0]["role"] == "user"
    static, dynamic = payload["input"][0]["content"]
    assert static["type"] == dynamic["type"] == "input_text"
    assert static["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert set(dynamic) == {"type", "text"}
    # Decode the complete transmitted JSON, not a substring or shortened fixture.
    serialized = dynamic["text"].split("## \ud3c9\uac00 \ub300\uc0c1 (\ub370\uc774\ud130)\n", 1)[1]
    assert serialized == json.dumps(snapshot, ensure_ascii=False, indent=1)
    assert json.loads(serialized) == snapshot and state == snapshot
    assert len(serialized) > 60_000
    assert "original question" not in static["text"] and "original reference" not in static["text"]
    timestamp_label = "\uc2e4\ud589 \uae30\uc900 \uc2dc\uac01(UTC):"
    assert timestamp_label not in static["text"] and timestamp_label in dynamic["text"]
    for cid in ids:
        assert f"### {cid}\n" in static["text"]
    assert payload["text"]["format"]["schema"]["required"] == list(ids)
    assert payload["reasoning"] == {"effort": "medium"} and payload["max_output_tokens"] == 12000
    assert payload["store"] is False and payload["truncation"] == "disabled"
    # No customer/input-derived routing key and no separately billed prewarm.
    assert "prompt_cache_key" not in payload and "prompt_cache_retention" not in payload


def test_gpt_reuses_same_static_boundary_for_different_original_questions_and_cache_usage_survives():
    usage = {"input_tokens": 2400, "output_tokens": 60,
             "input_tokens_details": {"cached_tokens": 1800, "cache_write_tokens": 200}}
    captured = []
    for label in ("first-private-question", "second-private-question"):
        call, requests = call_with_response(completed_response(entries("J1", "J5"), usage=usage),
                                           state={"question": label, "answer": label + " answer"},
                                           ids=("J1", "J5"))
        payload = json.loads(requests[0].content)
        captured.append(payload)
        static, dynamic = payload["input"][0]["content"]
        assert label not in static["text"] and label in dynamic["text"]
        assert "prompt_cache_key" not in payload
        assert call.usage == usage
        assert call.body["verification"]["usage_requests"][0]["usage"] == usage
        assert payload["tools"] == [{"type": "web_search"}] and "max_tool_calls" not in payload
    assert captured[0]["input"][0]["content"][0] == captured[1]["input"][0]["content"][0]
    assert captured[0]["input"][0]["content"][1] != captured[1]["input"][0]["content"][1]


@pytest.mark.parametrize("model", ["gpt-5.4", "gpt-6.1-sol-2026-10-01", "requested-judge-v1"])
def test_unverified_models_keep_legacy_string_request_without_explicit_cache_fields(model):
    state = {"question": "private question", "answer": "original answer\r\n"}
    _, requests = call_with_response(completed_response(), state=state, config=SemanticApiConfig(model=model))
    payload = json.loads(requests[0].content)
    assert isinstance(payload["input"], str)
    assert json.dumps(state, ensure_ascii=False, indent=1) in payload["input"]
    assert not {"prompt_cache_options", "prompt_cache_key", "prompt_cache_retention"} & payload.keys()
    assert "prompt_cache_breakpoint" not in payload
    assert SemanticApiConfig(model=model).to_manifest()["prompt_cache_policy"] == "provider_default"


@pytest.mark.parametrize("reference", [None, "", "   ", {}, []])
def test_j5_without_reference_allows_provider_web_search_without_application_call_cap(reference):
    state = {"question": "현재 사실 확인", "answer": "2026년 자료입니다.", "reference": reference}
    call, requests = call_with_response(completed_response(entries("J5")), state=state, ids=("J5",))
    payload = json.loads(requests[0].content)
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["tool_choice"] == "auto"
    assert payload["include"] == ["web_search_call.action.sources"]
    assert "max_tool_calls" not in payload
    # Offering the tool does not prove it ran; facts still need verified evidence.
    assert call.body["verification"]["search_performed"] is False
    assert call.body["answers"]["J5"]["choice"] == "UNVERIFIED"


def test_explicit_finite_search_cap_is_still_respected():
    _, requests = call_with_response(completed_response(entries("J5")), ids=("J5",),
                                     config=SemanticApiConfig(max_tool_calls=9, evidence_recovery_rounds=0))
    assert json.loads(requests[0].content)["max_tool_calls"] == 9


def test_nonfactual_greeting_is_not_applicable_without_searching():
    values = entries("J1", "J5")
    values["J5"].update(choice="NOT_APPLICABLE", reason="인사만 포함되어 검증할 사실 주장이 없습니다.",
                        fact_check={"basis": "none", "claims": []})
    state = {"question": "안녕", "answer": "안녕하세요! 반가워요."}
    call, requests = call_with_response(completed_response(values), ids=("J1", "J5"), state=state)
    assert call.status == "OK" and len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["tool_choice"] == "auto"
    assert call.body["answers"]["J5"]["choice"] == "NOT_APPLICABLE"
    assert call.body["answers"]["J5"]["fact_check"] == {"basis": "none", "claims": []}
    assert call.body["verification"]["search_performed"] is False
    assert call.body["verification"]["source_urls"] == []


def test_factual_claim_cannot_pass_when_auto_search_was_not_used():
    values = entries("J5")
    values["J5"]["fact_check"] = {"basis": "web", "claims": [{"claim": "지구는 둥글다", "verdict": "PASS", "reason": "사실 확인",
        "sources": [{"url": "https://model-invented.example/earth", "title": "지구", "quote": "지구는 둥글다"}]}]}
    call, requests = call_with_response(completed_response(values), ids=("J5",),
                                       state={"question": "지구의 모양은?", "answer": "지구는 둥글다"})
    assert json.loads(requests[0].content)["tool_choice"] == "auto"
    assert call.body["verification"]["search_performed"] is False
    assert call.body["answers"]["J5"]["choice"] == "UNVERIFIED"
    assert call.body["answers"]["J5"]["fact_check"]["claims"][0]["sources"] == []


@pytest.mark.parametrize("ids,state,config", [
    (("J1", "J6"), {"question": "질문", "answer": "답변"}, SemanticApiConfig()),
    (("J5",), {"question": "질문", "answer": "답변", "reference": {"text": "확인할 근거"}}, SemanticApiConfig()),
    (("J5",), {"question": "질문", "answer": "답변"}, SemanticApiConfig(web_search="disabled")),
])
def test_search_is_not_enabled_for_other_criteria_reference_comparisons_or_disabled_config(ids, state, config):
    _, requests = call_with_response(completed_response(entries(*ids)), state=state, ids=ids, config=config)
    payload = json.loads(requests[0].content)
    assert not {"tools", "tool_choice", "include", "max_tool_calls"} & payload.keys()


def test_search_provenance_uses_only_completed_provider_events_and_citations():
    value = entries("J5")
    value["J5"]["reason"] = "검색했습니다 https://model-invented.example/fake"
    value["J5"]["fact_check"] = {"basis": "web", "claims": [{"claim": "원문 주장", "verdict": "PASS", "reason": "출처 확인",
        "sources": [{"url": "https://official.example/fact", "title": "공식 자료", "quote": "주장에 관한 자료",
                     "authority": "primary", "authority_reason": "원자료 작성 기관", "temporal_relevance": "변하지 않는 정의"}]}]}
    raw = completed_response(value, events=[
        {"type": "web_search_call", "status": "completed", "action": {"type": "search", "sources": [
            {"type": "url", "url": "https://official.example/fact"}, {"type": "url", "url": "https://official.example/fact"},
            {"type": "url", "url": "javascript:alert(1)"}, {"type": "url", "url": "https://user:secret@example.com/fact"}]}},
        {"type": "web_search_call", "status": "completed", "action": {"type": "open_page", "url": "https://official.example/page"}},
        {"type": "web_search_call", "status": "failed", "action": {"sources": [{"url": "https://failed.example/"}]}},
    ], annotations=[{"type": "url_citation", "url": "https://cited.example/article", "start_index": 0, "end_index": 5},
                    {"type": "other", "url": "https://ignored.example/"}])
    snapshot = copy.deepcopy(raw)
    call, _ = call_with_response(raw, ids=("J5",), state={"question": "사실 확인", "answer": "원문 주장"},
                                source_fetcher=lambda url: {"status": "ok", "text": "주장에 관한 자료",
                                    "final_url": url, "checked_at": "2026-10-09T00:00:00Z"})
    verification = call.body["verification"]
    assert verification["search_performed"] is True and verification["source_validation"] == "provider_and_content"
    assert verification["source_urls"] == ["https://cited.example/article", "https://official.example/fact", "https://official.example/page"]
    source = call.body["answers"]["J5"]["fact_check"]["claims"][0]["sources"][0]
    assert source["quote_verified"] is True and source["quote"] == "주장에 관한 자료"
    assert verification["source_checks"][0]["verified_quotes"] == 1
    assert call.body["answers"]["J5"]["choice"] == "PASS" and raw == snapshot


def test_original_claim_with_unobserved_source_url_stays_unverified_after_a_real_search():
    values = entries("J5")
    values["J5"]["fact_check"] = {"basis": "web", "claims": [{
        "claim": "Earth is round", "verdict": "PASS", "reason": "Source checked",
        "sources": [{"url": "https://invented.example/earth", "title": "Earth", "quote": "Earth is round"}],
    }]}
    raw = completed_response(values, events=[{
        "type": "web_search_call", "status": "completed", "action": {
            "type": "search", "sources": [{"url": "https://provider.example/earth"}],
        },
    }])
    call, _ = call_with_response(raw, ids=("J5",), state={
        "question": "What shape is Earth?", "answer": "Earth is round",
    })
    assert call.body["verification"]["search_performed"] is True
    assert call.body["verification"]["source_urls"] == ["https://provider.example/earth"]
    answer = call.body["answers"]["J5"]
    assert answer["choice"] == "UNVERIFIED"
    assert answer["fact_check"]["claims"][0]["verdict"] == "UNVERIFIED"
    assert answer["fact_check"]["claims"][0]["sources"] == []


@pytest.mark.parametrize("status", ["failed", "in_progress", "searching", None])
def test_failed_or_unfinished_tool_calls_and_text_urls_never_prove_search(status):
    values = entries("J5"); values["J5"]["reason"] = "웹 검색 완료: https://invented.example/"
    raw = completed_response(values, events=[{"type": "web_search_call", "status": status,
        "action": {"sources": [{"url": "https://unfinished.example/"}]}}],
        annotations=[{"type": "url_citation", "url": "https://unproven.example/"}])
    call, _ = call_with_response(raw, ids=("J5",))
    assert call.body["verification"]["search_performed"] is False
    assert call.body["verification"]["source_urls"] == []


def test_valid_fact_and_history_extensions_survive_shared_evidence_validation_without_source_mutation():
    values = entries("J5", "J6")
    values["J5"]["fact_check"] = {"basis": "reference", "claims": [{"claim": "인구는 1,000명", "verdict": "PASS", "reason": "참조 일치",
        "sources": [{"url": "", "title": "주어진 자료", "quote": "인구는 1,000명"}]}]}
    values["J6"]["context_checks"] = [{"turn": 1, "instruction_quote": "반말로 답해", "answer_quote": "좋아",
                                       "decision": "PASS", "reason": "이전 말투 지시를 유지"}]
    state = {"question": "어떻게 말할까?", "answer": "인구는 1,000명. 좋아.", "reference": "인구는 1,000명",
             "history": [{"turn": 1, "question": "반말로 답해", "answer": "알겠어"}]}
    snapshot = copy.deepcopy(values)
    call, _ = call_with_response(completed_response(values), ids=("J5", "J6"), state=state)
    assert call.status == "OK"
    assert call.body["answers"]["J5"]["fact_check"] == snapshot["J5"]["fact_check"]
    assert call.body["answers"]["J6"]["context_checks"] == snapshot["J6"]["context_checks"]
    assert call.body["answers"]["J5"]["choice"] == call.body["answers"]["J6"]["choice"] == "PASS"
    assert values == snapshot


def test_web_fact_without_provider_source_or_original_claim_and_fabricated_history_are_unverified():
    values = entries("J5", "J6")
    values["J5"]["fact_check"] = {"basis": "web", "claims": [{"claim": "원문에 없는 주장", "verdict": "PASS", "reason": "확인함",
        "sources": [{"url": "https://invented.example/fake", "title": "가짜 출처", "quote": "가짜 인용"}]}]}
    values["J6"]["context_checks"] = [{"turn": 99, "instruction_quote": "없는 지시", "answer_quote": "없는 답변",
                                       "decision": "PASS", "reason": "확인함"}]
    raw = completed_response(values, events=[{"type": "web_search_call", "status": "completed", "action": {
        "type": "search", "sources": [{"url": "https://official.example/fact"}]}}])
    call, _ = call_with_response(raw, ids=("J5", "J6"), state={"question": "확인", "answer": "실제 원문", "history": []})
    assert call.status == "OK"  # Valid transport, explicitly unresolved quality judgments.
    assert call.body["answers"]["J5"]["choice"] == call.body["answers"]["J6"]["choice"] == "UNVERIFIED"
    assert call.body["answers"]["J5"]["fact_check"]["claims"][0]["sources"] == []


@pytest.mark.parametrize("http_status", [301, 400, 401, 403, 413, 429, 500, 503])
def test_http_errors_are_unjudged_without_retry_redirect_or_credential_leaks(http_status):
    call, requests = call_with_response({"error": {"code": "context_length_exceeded", "message": "secret-private-response"}},
                                       status_code=http_status)
    assert len(requests) == 1 and call.attempts == 1 and call.status == "API_ERROR"
    assert call.http_status == http_status
    assert "secret-private-response" not in call.error and "sk-semantic-fixture-only" not in str(call.body)
    assert call.body["verification"]["search_performed"] is False


@pytest.mark.parametrize("mode,status", [("timeout", "TIMEOUT"), ("connection", "API_ERROR")])
def test_network_failures_are_not_retried_and_do_not_leak_exception_details(mode, status):
    requests = []

    def handler(request):
        requests.append(request)
        error = httpx.ReadTimeout if mode == "timeout" else httpx.ConnectError
        raise error("Authorization: Bearer secret-private-value", request=request)

    call = SemanticApiClient(SemanticApiConfig(), "private-semantic-key", transport=httpx.MockTransport(handler)).system_one(
        {"question": "원문", "answer": "답변"}, jev_request("질문", "답변", ("J1",))["questions"])
    assert call.status == status and call.attempts == 1 and len(requests) == 1
    assert "secret-private-value" not in call.error and "private-semantic-key" not in call.error


@pytest.mark.parametrize("mode", ["refusal", "incomplete", "failed", "message_incomplete", "error_object"])
def test_refusals_and_incomplete_outputs_preserve_search_provenance_but_never_become_judgments(mode):
    raw = completed_response(events=[{"type": "web_search_call", "status": "completed", "action": {"type": "search",
        "sources": [{"url": "https://official.example/"}]}}])
    if mode == "refusal":
        raw["output"][-1]["content"] = [{"type": "refusal", "refusal": "private refusal text"}]
    elif mode == "message_incomplete":
        raw["output"][-1]["status"] = "incomplete"
    elif mode == "error_object":
        raw["error"] = {"message": "private error text"}
    else:
        raw["status"] = mode
        raw["incomplete_details"] = {"reason": "max_output_tokens"}
    call, requests = call_with_response(raw)
    assert call.status == "API_ERROR" and len(requests) == 1
    assert "answers" not in call.body
    assert call.body["verification"]["search_performed"] is True
    assert call.body["verification"]["source_urls"] == ["https://official.example/"]
    assert "private" not in call.error


@pytest.mark.parametrize("mode", ["not_json", "not_object", "missing_output", "empty_output", "malformed_content",
                                  "nonstring_text", "malformed_judgment", "missing_id", "extra_id", "nonobject_entry",
                                  "missing_model", "nonfinite_number"])
def test_invalid_response_shapes_are_reported_without_fabricating_decisions(mode):
    raw = completed_response()
    if mode == "not_object": raw = []
    elif mode == "missing_output": del raw["output"]
    elif mode == "empty_output": raw["output"] = []
    elif mode == "malformed_content": raw["output"][0]["content"] = None
    elif mode == "nonstring_text": raw["output"][0]["content"][0]["text"] = 1
    elif mode == "malformed_judgment": raw["output"][0]["content"][0]["text"] = "not JSON"
    elif mode == "missing_id": raw = completed_response({})
    elif mode == "extra_id": raw = completed_response(entries("J1", "J2"))
    elif mode == "nonobject_entry": raw = completed_response({"J1": True})
    elif mode == "missing_model": del raw["model"]
    elif mode == "nonfinite_number": raw["output"][0]["content"][0]["text"] = '{"J1":{"confidence":NaN}}'
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content="not JSON") if mode == "not_json" else httpx.Response(200, json=raw)

    call = SemanticApiClient(SemanticApiConfig(), "semantic-test", transport=httpx.MockTransport(handler)).system_one(
        {"question": "질문", "answer": "답변"}, jev_request("질문", "답변", ("J1",))["questions"])
    assert call.status == "INVALID_RESPONSE" and len(requests) == 1
    assert "answers" not in call.body and "verification" in call.body


def test_calls_are_independent_and_never_reuse_prior_response_or_collection_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "collection-key-do-not-use")
    monkeypatch.setenv("TYPESAFE_API_KEY", "jev-key-do-not-use")
    with pytest.raises(ValueError, match="SEMANTIC_API_KEY"):
        SemanticApiClient(SemanticApiConfig(), "")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completed_response(), headers={"set-cookie": "prior=do-not-reuse"})

    client = SemanticApiClient(SemanticApiConfig(), "semantic-only", transport=httpx.MockTransport(handler))
    for question in ("첫 질문", "두 번째 질문"):
        assert client.system_one({"question": question, "answer": "답"}, jev_request(question, "답", ("J1",))["questions"]).status == "OK"
    assert len(requests) == 2
    for request in requests:
        assert request.headers["Authorization"] == "Bearer semantic-only" and "cookie" not in request.headers
        assert not {"previous_response_id", "conversation"} & json.loads(request.content).keys()


def test_manifest_is_available_without_a_client_and_pins_policy_and_limits():
    manifest = SemanticApiConfig().to_manifest()
    assert manifest["engine"] == "openai_api" and manifest["requested_model"] == "gpt-6.1-sol"
    assert manifest["api_base"] == "https://api.openai.com/v1" and manifest["max_attempts"] == 1
    assert manifest["fact_check_policy"] == "evidence-v3.5-task-input-edits" and manifest["context_check_policy"] == "history-quotes-v2-current-relevance"
    assert manifest["fact_check_scope"] == "all_answers"
    assert manifest["search_policy"] == "J5_without_reference_grouped_evidence_v3" and manifest["web_search"] == "live"
    assert manifest["max_tool_calls"] is None and manifest["max_output_tokens"] == 12000
    assert manifest["evidence_recovery_max_tool_calls"] is None
    assert manifest["store"] is False and manifest["truncation"] == "disabled"
    assert manifest["prompt_cache_policy"] == "explicit_static_prefix_v1"
    assert manifest["credentials_source"] == "SEMANTIC_API_KEY" and manifest["credentials_recorded"] is False


@pytest.mark.parametrize("kwargs", [
    {"model": ""}, {"timeout": 0}, {"timeout": float("inf")}, {"timeout": True},
    {"max_output_tokens": 0}, {"max_output_tokens": True}, {"max_tool_calls": 0}, {"max_tool_calls": 1.5},
    {"reasoning_effort": ""}, {"web_search": "automatic"}, {"api_base": "file:///tmp/test"},
    {"api_base": None}, {"api_base": "https://example.test:invalid/v1"},
    {"api_base": "https://name:secret@example.test/v1"}, {"api_base": "https://example.test/v1?api_key=secret"},
    {"api_base": "https://example.test/v1#secret"},
])
def test_invalid_configuration_is_rejected_before_any_request(kwargs):
    with pytest.raises(ValueError):
        SemanticApiConfig(**kwargs)


def priced_usage():
    return {"input_tokens": 1000, "output_tokens": 100,
            "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 100},
            "output_tokens_details": {"reasoning_tokens": 75}}


def web_fact():
    values = entries("J5")
    values["J5"]["fact_check"] = {"basis": "web", "claims": [{
        "claim": "Alpha is blue.", "verdict": "PASS", "reason": "The primary source confirms the definition.",
        "scope": "external_fact", "scope_quote": "", "sources": [{
            "url": "https://official.example/alpha", "title": "Alpha definition", "quote": "Alpha is blue.",
            "origin": "web", "turn": None, "authority": "primary",
            "authority_reason": "The originating institution defines Alpha.",
            "temporal_relevance": "This is a stable definition."}]}]}
    return values


def observed_search(url="https://official.example/alpha", kind="search"):
    return {"type": "web_search_call", "status": "completed",
            "action": {"type": kind, "sources": [{"url": url}]}}


def test_each_request_retains_nonmonetary_usage_and_actual_tier_without_source_mutation():
    raw = completed_response(model="gpt-6.1-sol", usage={**priced_usage(), "cost_usd": 99,
        "nested": {"price": 99, "count": 3}}, service_tier="default")
    original = copy.deepcopy(raw)
    call, sent = call_with_response(raw, config=SemanticApiConfig(reasoning_effort="medium"))
    parts = call.body["verification"]["usage_requests"]
    assert call.status == "OK" and len(sent) == call.attempts == len(parts) == 1
    assert json.loads(sent[0].content)["reasoning"] == {"effort": "medium"}
    assert parts == call.accounting["cost_parts"] and parts is not call.accounting["cost_parts"]
    assert parts[0]["model"] == "gpt-6.1-sol" and parts[0]["service_tier"] == "default"
    assert parts[0]["usage"]["output_tokens_details"]["reasoning_tokens"] == 75
    assert parts[0]["usage"]["input_tokens_details"]["cache_write_tokens"] == 100
    assert parts[0]["usage"]["nested"] == {"count": 3} and "cost_usd" not in json.dumps(parts)
    assert parts[0]["web_search_calls"] == 0 and raw == original
    assert call.body["verification"]["output_transport"] == "responses_json_schema"


@pytest.mark.parametrize("mode", ["incomplete", "http_error", "invalid_json", "unfinished_search"])
def test_incomplete_search_accounting_never_assumes_zero(mode):
    raw = completed_response(entries("J5"), model="gpt-6.1-sol", usage=priced_usage(), service_tier="default")
    status = 200
    if mode == "incomplete": raw["status"] = "incomplete"
    if mode == "http_error": status = 500
    if mode == "invalid_json": raw["output"][-1]["content"][0]["text"] = "{"
    if mode == "unfinished_search": raw["output"].insert(0, {**observed_search(), "status": "in_progress"})
    call, sent = call_with_response(raw, ids=("J5",), status_code=status)
    part = call.body["verification"]["usage_requests"][0]
    assert len(sent) == call.attempts == 1 and part["usage"] == priced_usage()
    assert part["web_search_calls"] is None and part["service_tier"] == "default"


def test_only_completed_search_actions_are_priced_not_page_opening_or_finding():
    raw = completed_response(entries("J5"), events=[observed_search(kind=k) for k in
        ("search", "open_page", "find_in_page", "search")], service_tier="default")
    call, _ = call_with_response(raw, ids=("J5",))
    assert call.accounting["cost_parts"][0]["web_search_calls"] == 2


def test_pre_cancelled_openai_request_has_zero_attempts_and_no_usage_parts():
    token = threading.Event(); token.set()
    client = SemanticApiClient(SemanticApiConfig(), "fixture", transport=httpx.MockTransport(
        lambda req: pytest.fail("A pre-cancelled call must not open HTTP")))
    call = client.system_one({}, {}, cancel_event=token)
    assert call.status == "SKIPPED" and call.attempts == 0
    assert call.body["verification"]["usage_requests"] == call.accounting["cost_parts"] == []


def test_evaluation_cancellation_reaches_async_http_and_closes_request():
    token, entered, cleaned = threading.Event(), threading.Event(), threading.Event()
    async def handler(request):
        entered.set()
        try:
            await asyncio.sleep(30)
            return httpx.Response(200, json=completed_response())
        finally:
            cleaned.set()
    client = SemanticApiClient(SemanticApiConfig(), "fixture", transport=httpx.MockTransport(handler))
    records = []
    thread = threading.Thread(target=lambda: records.append(evaluate_jev_case(
        "cancel", "q", "a", client, criteria=("J1",), cancel_event=token)))
    thread.start()
    try:
        assert entered.wait(2)
        token.set(); thread.join(3)
        assert not thread.is_alive() and cleaned.is_set()
        assert records[0]["status"] == "SKIPPED" and records[0]["attempts"] == 1
        assert records[0]["verification"]["usage_requests"][0]["usage"] == {}
    finally:
        token.set(); thread.join(3)


def test_async_deadline_cancels_wait_and_keeps_unknown_request_accounting():
    cleaned = []
    async def handler(request):
        try:
            await asyncio.sleep(30)
            return httpx.Response(200, json=completed_response())
        finally:
            cleaned.append(True)
    client = SemanticApiClient(SemanticApiConfig(timeout=.03), "fixture", transport=httpx.MockTransport(handler))
    start = time.monotonic()
    call = client.system_one({"question": "q", "answer": "a"}, jev_request("q", "a", ("J1",))["questions"])
    assert call.status == "TIMEOUT" and call.attempts == 1 and cleaned == [True]
    assert time.monotonic() - start < 2
    assert call.body["verification"]["usage_requests"][0]["usage"] == {}


def test_cancelled_completed_response_preserves_usage_before_discarding_judgment():
    token = threading.Event()
    def handler(request):
        token.set()
        return httpx.Response(200, json=completed_response(model="gpt-6.1-sol", usage=priced_usage(), service_tier="default"))
    client = SemanticApiClient(SemanticApiConfig(), "fixture", transport=httpx.MockTransport(handler))
    call = client.system_one({"question": "q", "answer": "a"}, jev_request("q", "a", ("J1",))["questions"], cancel_event=token)
    assert call.status == "SKIPPED" and "answers" not in call.body
    assert call.body["verification"]["usage_requests"][0]["usage"] == priced_usage()


def test_source_cancellation_stops_additional_fetches_but_keeps_completed_api_usage():
    token, fetched = threading.Event(), []
    values = web_fact()
    values["J5"]["fact_check"]["claims"][0]["sources"].append({
        **values["J5"]["fact_check"]["claims"][0]["sources"][0], "url": "https://official.example/second"})
    raw = completed_response(values, events=[observed_search(), observed_search("https://official.example/second")],
        model="gpt-6.1-sol", usage=priced_usage(), service_tier="default")
    def fetcher(url):
        fetched.append(url); token.set()
        return {"status": "ok", "text": "Alpha is blue.", "final_url": url}
    client = SemanticApiClient(SemanticApiConfig(), "fixture", source_fetcher=fetcher,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=raw)))
    call = client.system_one({"question": "Describe Alpha.", "answer": "Alpha is blue."},
                            jev_request("q", "a", ("J5",))["questions"], cancel_event=token)
    assert call.status == "SKIPPED" and "answers" not in call.body and len(fetched) == 1
    assert call.body["verification"]["usage_requests"][0]["web_search_calls"] == 2
    assert call.body["verification"]["usage_requests"][0]["usage"] == priced_usage()


@pytest.mark.parametrize("observed,quote_matches", [(True, True), (True, False), (False, True)])
def test_mixed_input_and_web_evidence_keep_separate_validation_and_original_inputs(observed, quote_matches):
    values = web_fact(); values["J5"]["fact_check"]["basis"] = "mixed"
    scope = "Summarize the following document: The group has 12 members."
    values["J5"]["fact_check"]["claims"].append({"claim": "The group has 12 members.", "verdict": "PASS",
        "reason": "Matches the provided document", "scope": "input_consistency", "scope_quote": scope, "sources": [{
            "origin": "question", "turn": None, "url": "", "title": "Provided document", "quote": "The group has 12 members.",
            "authority": "unestablished", "authority_reason": "Only document consistency", "temporal_relevance": "Fictional setting"}]})
    state = {"question": scope, "answer": "The group has 12 members. Alpha is blue."}
    originals, fetched = copy.deepcopy((state, values)), []
    def fetcher(url):
        fetched.append(url)
        return {"status": "ok", "text": "Alpha is blue." if quote_matches else "Different original text.",
                "final_url": url, "content_type": "text/plain"}
    call, _ = call_with_response(completed_response(values, events=[observed_search()] if observed else []),
                                 ids=("J5",), state=state, source_fetcher=fetcher)
    judgment = call.body["answers"]["J5"]
    assert call.status == "OK" and (state, values) == originals
    assert fetched == (["https://official.example/alpha"] if observed else [])
    assert judgment["choice"] == ("PASS" if observed and quote_matches else "UNVERIFIED")
    assert judgment["fact_check"]["claims"][1]["verdict"] == "PASS"


def test_response_and_source_timings_are_separate_and_saved_on_error_too():
    async def handler(request):
        await asyncio.sleep(.025)
        return httpx.Response(200, json=completed_response(web_fact(), events=[observed_search()]))
    def fetcher(url):
        time.sleep(.015)
        return {"status": "ok", "text": "Alpha is blue.", "final_url": url}
    client = SemanticApiClient(SemanticApiConfig(), "fixture", transport=httpx.MockTransport(handler), source_fetcher=fetcher)
    call = client.system_one({"question": "Describe Alpha.", "answer": "Alpha is blue."},
                            jev_request("q", "a", ("J5",))["questions"])
    timing = call.body["verification"]["timings_ms"]
    assert call.status == "OK" and timing["api"] >= 20 and timing["source_verification"] >= 10
    assert timing["total"] >= timing["api"] + timing["source_verification"] - .2
    failed, _ = call_with_response({"error": {"message": "private"}}, status_code=429)
    timing = failed.body["verification"]["timings_ms"]
    assert timing["api"] >= 0 and timing["source_verification"] == 0 and timing["total"] >= timing["api"]
