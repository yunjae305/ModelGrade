"""답변 수집 요청에 추론 강도 medium 이 들어가는지 — GPT·Claude·Gemini 모두, 일반 수집과 도구 수집 둘 다 (모델 호출 없음)."""
from __future__ import annotations

import json

import httpx
import pytest

from webapp import agent_collection as ac
from webapp.comparison_service import request_answer

RESPONSES = {
    "gpt": {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "답"}]}]},
    "claude": {"stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "답"}]},
    "gemini": {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "답"}]}}]},
}


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
    assert reasoning_of(provider, sent[0]) == "medium" == ac.COLLECTION_REASONING_EFFORT
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


@pytest.mark.parametrize("builder,provider", [(ac._openai, "gpt"), (ac._anthropic, "claude"), (ac._gemini, "gemini")])
def test_tool_collection_requests_carry_medium(builder, provider):
    _, _, body = builder({"key": "k", "model": "m"}, [], [("text", "질문")], 8000)
    assert reasoning_of(provider, body) == "medium"
