"""API 로 모델에 요청 하나를 보내고 답 글을 받는다 — 질문 자동 생성기(``question_generation.gpt_llm``)가 쓴다.

답변 수집(채팅 품질 평가)은 웹 화면으로 받으므로 이 모듈을 쓰지 않는다. 답 글은 고치지 않고(앞뒤 공백 · 줄바꿈 · 마크다운 그대로)
원본 응답 JSON 을 함께 돌려준다. 실패 · 미완료 응답은 그대로 결과로 남기고 자동으로 다시 보내지 않는다. 키는 기록하지 않는다.
(2026-10-26: 옛 웹 화면의 comparison_service · agent_collection · attachment_delivery 에서 남은 부분만 모음)
"""
from __future__ import annotations

from typing import Any, Dict, List

import httpx

from code_eval import attachment_images
from code_eval.io import validate_json_numbers

__all__ = ["ProviderError", "request_answer", "apply_reasoning", "image_blocks", "COLLECTION_REASONING_EFFORT"]


#: 답변 수집 추론 강도 — GPT·Claude·Gemini 에 같은 값을 명시한다 (사용자 결정 2026-10-14).
#: 지정하지 않으면 제공자마다 기본값이 달라(Claude Sonnet 5.5 는 high) 모델 비교가 기본값 차이에 흔들린다.
COLLECTION_REASONING_EFFORT = "medium"


def apply_reasoning(provider: str, body: Dict[str, Any], effort: str = COLLECTION_REASONING_EFFORT) -> Dict[str, Any]:
    """요청 본문에 추론 강도를 넣는다 — gpt: ``reasoning.effort`` · claude: adaptive thinking + ``output_config.effort`` ·
    gemini: ``generationConfig.thinkingConfig.thinkingLevel``. 다른 제공자(OpenAI 호환 게이트웨이 등)는 그대로 둔다."""
    if provider == "gpt":
        body["reasoning"] = {"effort": effort}
    elif provider == "claude":
        body["thinking"] = {"type": "adaptive"}
        body["output_config"] = {**(body.get("output_config") or {}), "effort": effort}
    elif provider == "gemini":
        body.setdefault("generationConfig", {})["thinkingConfig"] = {"thinkingLevel": effort}
    return body


def image_blocks(provider: str, text: str, images: List[Dict[str, Any]]) -> Any:
    """마지막 사용자 메시지의 내용 — 이미지가 없으면 글자 그대로(예전 요청과 같음), 있으면 제공자 형식의 여러 부분."""
    if not images:
        return text
    parts = attachment_images.blocks(provider if provider in ("gpt", "claude", "factchat") else "gemini", images)
    if provider == "gpt":   # OpenAI Responses
        return [{"type": "input_text", "text": text}] + parts
    if provider in ("claude", "factchat"):
        return [{"type": "text", "text": text}] + parts
    return {"gemini_parts": [{"text": text}] + parts}


class ProviderError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status  # HTTP 상태 코드 (네트워크 실패·형식 오류는 None)


def request_answer(provider, config, messages, max_tokens, client, *, usage_observer=None, images=None):
    """No answer strip, newline replacement, Markdown rendering or JSON-mode prompt.
    ``images`` (첨부 이미지) 가 있으면 마지막 사용자 메시지에 제공자 형식으로 함께 싣는다 — 없으면 예전 요청 그대로."""
    if images:
        last = messages[-1]
        messages = messages[:-1] + [{"role": last["role"], "content": image_blocks(provider, last["content"], images)}]
    observed = False

    def record_usage(raw):
        nonlocal observed
        if observed:
            return
        observed = True
        if usage_observer is not None:
            try:
                usage_observer(raw)
            except Exception:
                pass  # Terminal cost reporting must never change a model result.

    key, model = config["key"], config["model"]
    if provider == "gpt":
        url = "https://api.openai.com/v1/responses"
        headers = {"Authorization": f"Bearer {key}"}
        body = {"model": model, "input": messages, "max_output_tokens": max_tokens, "store": False}
    elif provider == "claude":
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    elif provider == "factchat":
        # OpenAI 호환 Chat Completions 게이트웨이 — 시스템 프롬프트·도구 없이 같은 메시지만 보낸다.
        url = config["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    else:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": key}
        body = {"contents": [{"role": "model" if m["role"] == "assistant" else "user",
                               "parts": m["content"]["gemini_parts"] if isinstance(m["content"], dict) else [{"text": m["content"]}]}
                              for m in messages],
                "generationConfig": {"maxOutputTokens": max_tokens}}
    apply_reasoning(provider, body)   # GPT·Claude·Gemini 추론 강도 medium 명시 (FactChat 게이트웨이는 그대로)
    try:
        try:
            response = client.post(url, headers=headers, json=body)
        except httpx.HTTPError:
            # Exception strings may include URLs or headers; don't expose them.
            raise ProviderError("네트워크 요청 실패 또는 시간 초과. 자동 재시도하지 않았습니다.") from None
        if response.status_code >= 400:
            reason = {400: "모델 ID·출력 토큰 한도 등 요청 설정을 확인해 주세요.",
                      401: "API 키를 확인해 주세요.", 403: "키 또는 모델 접근 권한을 확인해 주세요.",
                      404: "모델 ID를 확인해 주세요.", 429: "사용량·요청 한도를 확인해 주세요."}.get(
                          response.status_code, "모델 제공자에서 요청을 처리하지 못했습니다.")
            raise ProviderError(f"HTTP {response.status_code}: {reason}", status=response.status_code)
        try:
            raw = response.json()
            record_usage(raw)  # Keep usage even if answer parsing fails below.
            validate_json_numbers(raw)
            if provider == "gpt":
                blocks = [b for m in raw.get("output", []) if m.get("type") == "message"
                          for b in m.get("content", [])]
                texts = [b["text"] if b.get("type") == "output_text" else b["refusal"]
                         for b in blocks if b.get("type") in ("output_text", "refusal")]
                finish = raw.get("status", "unknown")
                complete = finish == "completed"
            elif provider == "claude":
                texts = [b["text"] for b in raw.get("content", []) if b.get("type") == "text"]
                finish = raw.get("stop_reason", "unknown")
                complete = finish in ("end_turn", "stop_sequence", "refusal")
            elif provider == "factchat":
                choice = (raw.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                content = message.get("content")
                if isinstance(content, list):
                    texts = [part["text"] for part in content if part.get("type") == "text"]
                else:
                    texts = [text for text in (content, message.get("refusal")) if isinstance(text, str) and text][:1]
                finish = choice.get("finish_reason") or "unknown"
                complete = finish == "stop"
            else:
                candidate = (raw.get("candidates") or [{}])[0]
                texts = [p["text"] for p in candidate.get("content", {}).get("parts", [])
                         if "text" in p and not p.get("thought")]
                finish = candidate.get("finishReason", "blocked")
                complete = finish == "STOP"
            if not all(isinstance(t, str) for t in texts):
                raise ValueError("text is not a string")
            answer = "".join(texts)
            return {"answer": answer, "raw": raw, "finish_reason": finish,
                    **({"reasoning_effort": COLLECTION_REASONING_EFFORT} if provider in ("gpt", "claude", "gemini") else {}),
                    "status": "SUCCESS" if complete and texts else "INCOMPLETE",
                    "error": None if complete and texts else f"응답 미완료 ({finish}). 원본 응답은 보존했습니다."}
        except (ValueError, TypeError, AttributeError, KeyError):
            raise ProviderError("모델 응답 형식을 해석하지 못했습니다.") from None
    finally:
        record_usage(None)  # Network/HTTP/JSON failures still represent one request.
