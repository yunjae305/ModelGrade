"""Bounded J5 evidence recovery with mocked provider and source requests only."""
import asyncio
import copy
import json
import threading
import time

import httpx
import pytest

from code_eval.jev_eval import jev_request
from code_eval.semantic_api_client import SemanticApiClient, SemanticApiConfig
from tests.test_semantic_api_client import transmitted_state


STATE = {"question": "Describe Alpha.", "answer": "Alpha is blue.",
         "history": [{"turn": 1, "question": "An earlier original question.", "answer": "Earlier answer."}]}
IDS = ("J1", "J3", "J5")
INITIAL_URL = "https://official.example/blocked"
RECOVERED_URL = "https://official.example/recovered"


def judgment(*, url=INITIAL_URL, verdict="PASS", ids=IDS):
    answers = {cid: {"choice": "PASS", "confidence": .8, "reason": "Initial judgment."} for cid in ids}
    answers["J5"] = {"choice": verdict, "confidence": .8, "reason": "Primary evidence supports this decision.",
        "fact_check": {"basis": "web", "claims": [{
            "claim": "Alpha is blue.", "verdict": verdict, "reason": "Primary evidence supports this decision.",
            "scope": "external_fact", "scope_quote": "", "sources": [{
                "url": url, "title": "Alpha definition", "quote": "Alpha is red." if verdict == "FAIL" else "Alpha is blue.",
                "origin": "web", "turn": None, "authority": "primary",
                "authority_reason": "The institution defines Alpha.", "temporal_relevance": "Stable definition."}]}]}}
    return answers


def response(answers=None, *, url=INITIAL_URL, tokens=100, status="completed"):
    answers = judgment() if answers is None else answers
    return {"id": "fixture-response", "status": status, "model": "gpt-6.1-sol", "service_tier": "default",
        "usage": {"input_tokens": tokens, "output_tokens": tokens // 10,
                  "input_tokens_details": {"cached_tokens": tokens // 2},
                  "output_tokens_details": {"reasoning_tokens": tokens // 20}},
        "output": [{"type": "web_search_call", "status": "completed",
                    "action": {"type": "search", "sources": [{"url": url}]}},
                   {"type": "message", "role": "assistant", "status": "completed", "content": [
                       {"type": "output_text", "text": json.dumps(answers), "annotations": []}]}]}


def run_sequence(replies, *, state=None, config=None, ids=IDS, cancel_event=None):
    sent, fetched = [], []

    def handler(request):
        sent.append(request)
        assert len(sent) <= len(replies), "Recovery exceeded the supplied request bound"
        reply = replies[len(sent) - 1]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    def fetcher(url):
        fetched.append(url)
        return {"status": "ok", "text": "Alpha is blue. Alpha is red.", "final_url": url} if url == RECOVERED_URL else {"status": "fetch_error"}

    client = SemanticApiClient(config or SemanticApiConfig(), "fixture-only", transport=httpx.MockTransport(handler), source_fetcher=fetcher)
    original = copy.deepcopy(STATE if state is None else state)
    call = client.system_one(original, jev_request(original.get("question", ""), original.get("answer", ""), ids)["questions"],
                             cancel_event=cancel_event)
    return call, sent, fetched, original


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_recovery_targets_j5_preserves_original_inputs_and_aggregates_actual_usage(verdict):
    state = {**copy.deepcopy(STATE), "question": "Describe Alpha.\r\n" + "original private text " * 4000}
    initial = response()
    recovered = response(judgment(url=RECOVERED_URL, verdict=verdict, ids=("J5",)), url=RECOVERED_URL, tokens=60)
    snapshots = copy.deepcopy((state, initial, recovered))
    call, sent, fetched, original = run_sequence([initial, recovered], state=state)
    assert call.status == "OK" and call.attempts == len(sent) == 2
    assert call.body["answers"]["J5"]["choice"] == verdict
    assert all(call.body["answers"][cid]["choice"] == "PASS" for cid in ("J1", "J3"))
    assert all(call.body["answers"][cid]["reason"] == "Initial judgment." for cid in ("J1", "J3"))
    assert (state, initial, recovered) == snapshots and original == state
    first, followup = [json.loads(request.content) for request in sent]
    assert first["text"]["format"]["schema"]["required"] == list(IDS)
    assert followup["text"]["format"]["schema"]["required"] == ["J5"]
    assert "max_tool_calls" not in first and "max_tool_calls" not in followup
    for payload in (first, followup):
        assert payload["store"] is False and payload["truncation"] == "disabled"
        assert "previous_response_id" not in payload and "conversation" not in payload
        static, *dynamic = payload["input"][0]["content"]
        assert static["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert "original private text" not in static["text"]
        combined = "".join(item["text"] for item in payload["input"][0]["content"])
        assert transmitted_state(combined) == state   # 대화 순서 블록으로 나눠도 원문은 그대로
        assert "prompt_cache_breakpoint" not in dynamic[-1]   # 매번 다른 실행 시각·답변 블록은 캐시 표시 없음
    assert RECOVERED_URL in fetched
    parts = call.body["verification"]["usage_requests"]
    assert len(parts) == 2 and parts == call.accounting["cost_parts"]
    assert [part["usage"]["input_tokens"] for part in parts] == [100, 60]
    assert [part["web_search_calls"] for part in parts] == [1, 1]
    assert call.usage["input_tokens"] == 160 and call.usage["output_tokens"] == 16
    assert call.usage["input_tokens_details"]["cached_tokens"] == 80
    assert call.usage["output_tokens_details"]["reasoning_tokens"] == 8
    assert [part["stage"] for part in parts] == ["evaluation", "evidence_recovery"]
    audit = call.body["verification"]["evidence_recovery"]
    assert audit["status"] == "RESOLVED" and audit["rounds"] == 1
    assert audit["initial_choice"] == "UNVERIFIED" and audit["final_choice"] == verdict
    assert audit["initial_j5"]["choice"] == "UNVERIFIED" and audit["supplemental_j5"]["choice"] == verdict


def test_unresolved_recovery_stops_at_two_calls_and_does_not_invent_a_verdict():
    call, sent, _, _ = run_sequence([response(), response(judgment(ids=("J5",)))])
    assert call.status == "OK" and call.attempts == len(sent) == 2
    assert call.body["answers"]["J5"]["choice"] == "UNVERIFIED"
    assert call.body["answers"]["J1"]["choice"] == "PASS"
    assert call.body["verification"]["evidence_recovery"]["status"] == "UNRESOLVED"


@pytest.mark.parametrize("mode", ["http", "incomplete", "invalid_json", "timeout"])
def test_failed_recovery_preserves_initial_criteria_and_accounts_for_sent_request(mode):
    followup = response(judgment(ids=("J5",)), tokens=40)
    if mode == "http":
        followup = httpx.Response(503, json=followup)
    elif mode == "incomplete":
        followup["status"] = "incomplete"
    elif mode == "invalid_json":
        followup["output"][-1]["content"][0]["text"] = "{"
    else:
        followup = httpx.ReadTimeout("private provider error")
    call, sent, _, _ = run_sequence([response(), followup])
    assert call.status == "OK" and call.attempts == len(sent) == 2
    assert call.body["answers"]["J1"]["reason"] == "Initial judgment."
    assert call.body["answers"]["J5"]["choice"] == "UNVERIFIED"
    parts = call.body["verification"]["usage_requests"]
    assert len(parts) == 2 and parts[0]["usage"]["input_tokens"] == 100
    assert parts[1]["usage"] == ({} if mode == "timeout" else followup.json()["usage"] if isinstance(followup, httpx.Response) else followup["usage"])
    assert "private provider error" not in json.dumps(call.body)
    assert call.body["verification"]["evidence_recovery"]["status"] == "FAILED"


@pytest.mark.parametrize("mode", ["disabled", "no_search", "reference", "not_applicable", "invalid_claim", "missing_claims"])
def test_recovery_is_skipped_when_ineligible(mode):
    config, state, values = SemanticApiConfig(), copy.deepcopy(STATE), judgment()
    if mode == "disabled":
        config = SemanticApiConfig(evidence_recovery_rounds=0)
    elif mode == "no_search":
        config = SemanticApiConfig(web_search="disabled")
    elif mode == "reference":
        state["reference"] = "An original reference without supporting this assertion."
    elif mode == "not_applicable":
        values["J5"] = {"choice": "NOT_APPLICABLE", "confidence": .8, "reason": "No factual claim.", "fact_check": {"basis": "none", "claims": []}}
    elif mode == "invalid_claim":
        values["J5"]["fact_check"]["claims"][0]["claim"] = "Invented absent claim."
    else:
        values["J5"]["fact_check"]["claims"] = []
    call, sent, _, _ = run_sequence([response(values)], state=state, config=config)
    assert call.status == "OK" and call.attempts == len(sent) == 1


def test_initial_transport_error_is_not_retried_as_evidence_recovery():
    call, sent, _, _ = run_sequence([httpx.Response(429, json=response())])
    assert call.status == "API_ERROR" and call.attempts == len(sent) == 1
    assert "answers" not in call.body


def test_cancellation_interrupts_followup_and_keeps_both_usage_parts():
    token, entered, closed = threading.Event(), threading.Event(), threading.Event()
    sent, result = [], []

    async def handler(request):
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(200, json=response())
        entered.set()
        try:
            await asyncio.sleep(30)
            return httpx.Response(200, json=response(judgment(ids=("J5",))))
        finally:
            closed.set()

    client = SemanticApiClient(SemanticApiConfig(), "fixture-only", transport=httpx.MockTransport(handler),
                               source_fetcher=lambda url: {"status": "fetch_error"})
    thread = threading.Thread(target=lambda: result.append(client.system_one(copy.deepcopy(STATE),
        jev_request(STATE["question"], STATE["answer"], IDS)["questions"], cancel_event=token)))
    thread.start()
    try:
        assert entered.wait(3)
        token.set()
        thread.join(3)
        assert not thread.is_alive() and closed.is_set()
        call = result[0]
        assert call.status == "SKIPPED" and call.attempts == len(sent) == 2
        assert "answers" not in call.body
        parts = call.body["verification"]["usage_requests"]
        assert len(parts) == 2 and parts[0]["usage"]["input_tokens"] == 100 and parts[1]["usage"] == {}
        assert call.body["verification"]["evidence_recovery"]["status"] == "CANCELLED"
    finally:
        token.set()
        thread.join(3)


def test_followup_has_its_own_bounded_deadline_and_keeps_initial_result():
    sent, closed = [], []
    async def handler(request):
        sent.append(request)
        if len(sent) == 1:
            await asyncio.sleep(.02)
            return httpx.Response(200, json=response())
        try:
            await asyncio.sleep(30)
        finally:
            closed.append(True)
    client = SemanticApiClient(SemanticApiConfig(timeout=.1, evidence_recovery_timeout=.03), "fixture-only",
        transport=httpx.MockTransport(handler), source_fetcher=lambda url: {"status": "fetch_error"})
    started = time.monotonic()
    call = client.system_one(copy.deepcopy(STATE), jev_request(STATE["question"], STATE["answer"], IDS)["questions"])
    assert .04 <= time.monotonic() - started < 2
    assert call.status == "OK" and call.attempts == len(sent) == 2 and closed == [True]
    assert call.body["answers"]["J5"]["choice"] == "UNVERIFIED"


def test_explicit_finite_primary_and_recovery_search_caps_are_independent():
    config = SemanticApiConfig(max_tool_calls=7, evidence_recovery_max_tool_calls=11)
    _, requests, _, _ = run_sequence([response(), response(judgment(url=RECOVERED_URL, ids=("J5",)), url=RECOVERED_URL)], config=config)
    assert [json.loads(request.content)["max_tool_calls"] for request in requests] == [7, 11]


@pytest.mark.parametrize("kwargs", [
    {"evidence_recovery_rounds": -1}, {"evidence_recovery_rounds": 2}, {"evidence_recovery_rounds": True},
    {"evidence_recovery_rounds": .5}, {"evidence_recovery_timeout": 0}, {"evidence_recovery_timeout": True},
    {"evidence_recovery_timeout": float("inf")}, {"evidence_recovery_timeout": float("nan")},
    {"evidence_recovery_max_tool_calls": 0}, {"evidence_recovery_max_tool_calls": True},
    {"evidence_recovery_max_tool_calls": 1.5},
])
def test_invalid_recovery_configuration_is_rejected_before_any_request(kwargs):
    with pytest.raises(ValueError):
        SemanticApiConfig(**kwargs)
