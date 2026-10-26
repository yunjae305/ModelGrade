"""API 요청 하나(webapp.api_request.request_answer) — 질문 자동 생성기가 쓴다. 제공자 HTTP 는 모두 대역, 유료 호출 없음.

답 글 그대로 받기 · 미완료 표시 · 키 숨김 / 사용량 기록 / 추론 강도 medium.
"""
from __future__ import annotations

import json

import httpx
import pytest

from webapp.api_request import COLLECTION_REASONING_EFFORT, ProviderError, request_answer

RESPONSES = {
    "gpt": {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "답"}]}]},
    "claude": {"stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "답"}]},
    "gemini": {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "답"}]}}]},
}


# --- 답 글 그대로 받기 ---------------------------------------------------------------------

@pytest.mark.parametrize('provider,raw', [
    ('gpt', {'status': 'completed', 'output': [{'type': 'reasoning'}, {'type': 'message', 'content': [
        {'type': 'output_text', 'text': '  ```json\n'}, {'type': 'output_text', 'text': '{"a":"\\n"}\n```\n  '}]}]}),
    ('claude', {'stop_reason': 'end_turn', 'content': [{'type': 'thinking', 'thinking': 'hidden'},
        {'type': 'text', 'text': '  ```json\n'}, {'type': 'text', 'text': '{"a":"\\n"}\n```\n  '}]}),
    ('gemini', {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
        {'text': 'hidden', 'thought': True}, {'text': '  ```json\n'}, {'text': '{"a":"\\n"}\n```\n  '}]}}]})
])
def test_provider_adapter_preserves_text_and_raw_response(provider, raw):
    requests = []
    def handle(req):
        requests.append(req)
        return httpx.Response(200, json=raw)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = request_answer(provider, {'key': 'secret-key', 'model': 'test-model'},
                                [{'role': 'user', 'content': '  원문 질문\r\n'}], 8192, client)
    assert result['answer'] == '  ```json\n{"a":"\\n"}\n```\n  '
    assert result['raw'] == raw
    assert result['status'] == 'SUCCESS'
    body = json.loads(requests[0].content)
    sent = body.get('input') or body.get('messages') or body.get('contents')
    assert (sent[0].get('content') or sent[0]['parts'][0]['text']) == '  원문 질문\r\n'
    assert 'secret-key' not in str(requests[0].url)


@pytest.mark.parametrize('provider,raw', [
    ('gpt', {'status': 'incomplete', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'partial\n'}]}]}),
    ('claude', {'stop_reason': 'max_tokens', 'content': [{'type': 'text', 'text': 'partial\n'}]}),
    ('gemini', {'candidates': [{'finishReason': 'MAX_TOKENS', 'content': {'parts': [{'text': 'partial\n'}]}}]})
])
def test_truncated_answers_are_preserved_and_marked_incomplete(provider, raw):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw))) as client:
        result = request_answer(provider, {'key': 'key', 'model': 'model'}, [], 4096, client)
    assert result['status'] == 'INCOMPLETE'
    assert result['answer'] == 'partial\n'


def test_api_error_does_not_leak_keys_or_provider_error_body():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401, json={'error': 'secret-key'}))) as client:
        with pytest.raises(ProviderError) as exc:
            request_answer('gpt', {'key': 'secret-key', 'model': 'model'}, [], 1024, client)
    assert '401' in str(exc.value)
    assert 'secret-key' not in str(exc.value)


# --- 사용량 기록 -------------------------------------------------------------------------

def response_raw(answer='answer', *, complete=True):
    return {'model': 'resolved-fixture-model', 'usage': {'input_tokens': 13, 'output_tokens': 3},
            'status': 'completed' if complete else 'incomplete',
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': answer}]}],
            'stop_reason': 'end_turn' if complete else 'max_tokens',
            'content': [{'type': 'text', 'text': answer}],
            'candidates': [{'finishReason': 'STOP' if complete else 'MAX_TOKENS',
                            'content': {'parts': [{'text': answer}]}}]}


@pytest.mark.parametrize('provider', ['gpt', 'claude', 'gemini'])
@pytest.mark.parametrize('complete', [True, False])
def test_usage_observed_once_for_success_and_incomplete(provider, complete):
    raw, observed = response_raw(complete=complete), []
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw))) as client:
        result = request_answer(provider, {'key': 'fixture', 'model': 'configured'}, [], 1024, client,
                                usage_observer=observed.append)
    assert observed == [raw]
    assert result['status'] == ('SUCCESS' if complete else 'INCOMPLETE')
    assert result['raw'] == raw


@pytest.mark.parametrize('failure', ['answer', 'json', 'http', 'network'])
def test_usage_keeps_parsed_raw_or_reports_unknown_once(failure):
    raw, observed = response_raw(answer=123), []

    def handle(request):
        if failure == 'network':
            raise httpx.ReadTimeout('fixture timeout', request=request)
        if failure == 'http':
            return httpx.Response(429, json={'private_error': 'must not be observed'})
        if failure == 'json':
            return httpx.Response(200, text='not JSON')
        return httpx.Response(200, json=raw)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError):
            request_answer('gpt', {'key': 'fixture', 'model': 'configured'}, [], 1024, client,
                           usage_observer=observed.append)
    assert observed == [raw if failure == 'answer' else None]


def test_usage_observer_failure_does_not_change_answer():
    def broken(_):
        raise RuntimeError('reporter unavailable')

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response_raw()))) as client:
        result = request_answer('gpt', {'key': 'fixture', 'model': 'configured'}, [], 1024, client,
                                usage_observer=broken)
    assert result['status'] == 'SUCCESS' and result['answer'] == 'answer'


# --- 추론 강도 ---------------------------------------------------------------------------


def reasoning_of(provider, body):
    if provider == "gpt":
        return body["reasoning"]["effort"]
    if provider == "claude":
        assert body["thinking"] == {"type": "adaptive"}
        return body["output_config"]["effort"]
    return body["generationConfig"]["thinkingConfig"]["thinkingLevel"]


@pytest.mark.parametrize("provider", ["gpt", "claude", "gemini"])
def test_plain_collection_sends_medium_and_records_it(provider):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=RESPONSES[provider])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        outcome = request_answer(provider, {"key": "k", "model": "m"}, [{"role": "user", "content": "질문"}], 8000, client)
    assert reasoning_of(provider, sent[0]) == "medium" == COLLECTION_REASONING_EFFORT
    assert outcome["status"] == "SUCCESS" and outcome["answer"] == "답" and outcome["reasoning_effort"] == "medium"
    assert sent[0].get("max_output_tokens", sent[0].get("max_tokens", sent[0].get("generationConfig", {}).get("maxOutputTokens"))) == 8000


def test_gateway_collection_is_left_without_reasoning_fields():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "답"}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        outcome = request_answer("factchat", {"key": "k", "model": "m", "base_url": "https://gw.example/v1"},
                                 [{"role": "user", "content": "질문"}], 100, client)
    assert not {"reasoning", "reasoning_effort", "thinking", "output_config"} & set(sent[0]) and "reasoning_effort" not in outcome
