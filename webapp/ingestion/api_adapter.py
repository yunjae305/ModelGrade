"""AI 모델 API 응답 JSON → 공통 답변 필드.

같은 변환을 두 곳에서 쓴다: (1) 이 서버의 기존 수집 경로(comparison_service.request_answer)가 받은 원본 응답,
(2) 사용자가 다른 곳에서 받아 둔 API 응답 파일. 답변 글자는 바꾸지 않는다. 응답에 없는 값(도구 허용 여부 등)은
추정하지 않고 None 으로 둔다. 비용은 제공자가 응답에 직접 밝힌 금액만 기록한다 (단가표로 계산하지 않음).

지원 형식 (공식 문서 기준, 2026-10-13 확인):
- OpenAI Responses (`object: "response"`) — xAI·Perplexity Agent API 도 같은 모양
- Chat Completions (`choices`) — Perplexity Sonar 의 `citations`·`search_results` 포함
- Anthropic Messages (`type: "message"`)
- Gemini generateContent (`candidates`)
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .schema import parse_time, quality

KIND_LABELS = {"openai_responses": "OpenAI Responses 형식", "chat_completions": "Chat Completions 형식",
               "anthropic_messages": "Anthropic Messages 형식", "gemini_generate": "Gemini generateContent 형식"}
#: 기존 수집 경로의 공급자 키 → (응답 형식, 제공자, 플랫폼 이름)
LIVE_PROVIDERS = {"gpt": ("openai_responses", "openai", "OpenAI API"),
                  "claude": ("anthropic_messages", "anthropic", "Anthropic API"),
                  "gemini": ("gemini_generate", "google", "Gemini API")}
_WEB_TOOLS = ("web_search", "web_fetch", "google_search", "browser", "search")


def detect_kind(raw: Any) -> Optional[str]:
    if not isinstance(raw, dict):
        return None
    if raw.get("object") == "response" or (isinstance(raw.get("output"), list) and "status" in raw and "choices" not in raw):
        return "openai_responses"
    if isinstance(raw.get("choices"), list):
        return "chat_completions"
    if raw.get("type") == "message" and isinstance(raw.get("content"), list):
        return "anthropic_messages"
    if isinstance(raw.get("candidates"), list):
        return "gemini_generate"
    return None


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _tool_names(items: List[str]) -> List[str]:
    return list(dict.fromkeys(i for i in items if isinstance(i, str) and i))


def _web(names: List[str]) -> bool:
    return any(any(w in name.lower() for w in _WEB_TOOLS) for name in names)


def _cost(value: Any, source: str) -> Optional[Dict[str, Any]]:
    """제공자가 응답에 직접 밝힌 금액. 통화가 명시되지 않으면 문서상 USD 인 제공자만 USD 로 둔다."""
    if isinstance(value, dict):
        for key in ("total_cost", "total", "amount"):
            amount = value.get(key)
            if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount >= 0:
                currency = value.get("currency") if isinstance(value.get("currency"), str) else "USD"
                return {"amount": amount, "currency": currency, "source": source}
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return {"amount": value, "currency": "USD", "source": source}
    return None


def parse_api_response(raw: Dict[str, Any], kind: Optional[str] = None) -> Dict[str, Any]:
    """{answer, status, error, finish_reason, model, usage, sources, environment, collected_at, quality, kind}"""
    kind = kind or detect_kind(raw)
    if kind == "openai_responses":
        out = _responses(raw)
    elif kind == "chat_completions":
        out = _chat(raw)
    elif kind == "anthropic_messages":
        out = _anthropic(raw)
    elif kind == "gemini_generate":
        out = _gemini(raw)
    else:
        raise ValueError("알 수 없는 API 응답 형식입니다. OpenAI Responses·Chat Completions·Anthropic Messages·"
                         "Gemini generateContent 응답 JSON 을 넣어 주세요.")
    out["kind"] = kind
    return out


def _result(answer: str, complete: bool, finish: Any, **kw: Any) -> Dict[str, Any]:
    has_text = bool(answer)
    status = "SUCCESS" if complete and has_text else "INCOMPLETE"
    out = {"answer": answer, "status": status, "finish_reason": finish if isinstance(finish, str) else None,
           "error": None if status == "SUCCESS" else f"응답 미완료 ({finish}). 원본 응답은 보존했습니다.",
           "model": None, "usage": {}, "sources": [], "environment": {"web_search_used": None, "tools_used": None},
           "tools_available": None, "collected_at": None, "quality": [], "retrieved_sources": []}
    out.update(kw)
    return out


def _responses(raw: Dict[str, Any]) -> Dict[str, Any]:
    texts: List[str] = []
    sources: List[Dict[str, Any]] = []
    tools: List[str] = []
    retrieved: List[Dict[str, Any]] = []
    searched = False
    offset = 0
    for item in raw.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            for block in item.get("content") or []:
                if not isinstance(block, dict) or block.get("type") not in ("output_text", "refusal"):
                    continue
                text = block.get("text") if block.get("type") == "output_text" else block.get("refusal")
                if not isinstance(text, str):
                    continue
                for note in block.get("annotations") or []:
                    if isinstance(note, dict) and note.get("type") == "url_citation" and isinstance(note.get("url"), str):
                        start, end = _int(note.get("start_index")), _int(note.get("end_index"))
                        sources.append({"url": note["url"], "title": note.get("title") if isinstance(note.get("title"), str) else None,
                                        "spans": [[offset + start, offset + end]] if start is not None and end is not None else [],
                                        "origin": "api_annotation"})
                texts.append(text)
                offset += len(text)
        elif kind == "web_search_call":
            searched = True
            tools.append("web_search")
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            for found in action.get("sources") or []:
                if isinstance(found, dict) and isinstance(found.get("url"), str):
                    retrieved.append({"url": found["url"], "title": found.get("title")})
        elif kind == "search_results":   # Perplexity Agent API
            searched = True
            tools.append("web_search")
            for found in item.get("results") or []:
                if isinstance(found, dict) and isinstance(found.get("url"), str):
                    retrieved.append({"url": found["url"], "title": found.get("title"), "date": found.get("date")})
        elif kind == "function_call":
            tools.append(item.get("name") if isinstance(item.get("name"), str) else "function")
        elif isinstance(kind, str) and kind.endswith("_call"):
            tools.append(kind[:-5])
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    details_out = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    details_in = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    declared = raw.get("tools") if isinstance(raw.get("tools"), list) else None
    available = None if declared is None else _tool_names([t.get("type") or t.get("name") for t in declared if isinstance(t, dict)])
    if isinstance(raw.get("citations"), list):   # xAI: 참조한 전체 URL
        retrieved.extend({"url": u} for u in raw["citations"] if isinstance(u, str))
    finish = raw.get("status", "unknown")
    if finish != "completed" and isinstance(raw.get("incomplete_details"), dict):
        finish = f"{finish}:{raw['incomplete_details'].get('reason')}"
    return _result("".join(texts), raw.get("status") == "completed", finish,
                   model=raw.get("model") if isinstance(raw.get("model"), str) else None,
                   usage={"input_tokens": _int(usage.get("input_tokens")), "output_tokens": _int(usage.get("output_tokens")),
                          "total_tokens": _int(usage.get("total_tokens")),
                          "reasoning_tokens": _int(details_out.get("reasoning_tokens")),
                          "cached_input_tokens": _int(details_in.get("cached_tokens")),
                          "reported_cost": _cost(usage.get("cost"), "provider_response")},
                   sources=sources, retrieved_sources=retrieved, tools_available=available,
                   environment={"web_search_used": True if searched else (False if available is not None and not _web(available) else None),
                                "tools_used": _tool_names(tools) if tools or available is not None else None},
                   collected_at=parse_time(raw.get("created_at")))


def _chat(raw: Dict[str, Any]) -> Dict[str, Any]:
    choice = (raw.get("choices") or [{}])[0]
    choice = choice if isinstance(choice, dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    answer = content if isinstance(content, str) else ""
    sources: List[Dict[str, Any]] = []
    for note in message.get("annotations") or []:
        if not isinstance(note, dict):
            continue
        body = note.get("url_citation") if isinstance(note.get("url_citation"), dict) else note
        if isinstance(body.get("url"), str):
            start, end = _int(body.get("start_index")), _int(body.get("end_index"))
            sources.append({"url": body["url"], "title": body.get("title") if isinstance(body.get("title"), str) else None,
                            "spans": [[start, end]] if start is not None and end is not None else [], "origin": "api_annotation"})
    # Perplexity Sonar: 본문 [n] 표시가 citations / search_results 의 n 번째를 가리킨다.
    listed = raw.get("search_results") if isinstance(raw.get("search_results"), list) else None
    urls = raw.get("citations") if isinstance(raw.get("citations"), list) else None
    retrieved = []
    if listed or urls:
        entries = ([{"url": r.get("url"), "title": r.get("title"), "date": r.get("date")} for r in listed if isinstance(r, dict)]
                   if listed else [{"url": u} for u in urls if isinstance(u, str)])
        for number, entry in enumerate(entries, 1):
            if isinstance(entry.get("url"), str):
                marker = f"[{number}]"
                if marker in answer:
                    sources.append({"url": entry["url"], "title": entry.get("title"), "marker": marker,
                                    "published_at": entry.get("date") if isinstance(entry.get("date"), str) else None,
                                    "origin": "api_annotation"})
                retrieved.append(entry)
    tools = [c.get("function", {}).get("name") for c in message.get("tool_calls") or [] if isinstance(c, dict)]
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    finish = choice.get("finish_reason", "unknown")
    return _result(answer, finish in ("stop", "end_turn"), finish,
                   model=raw.get("model") if isinstance(raw.get("model"), str) else None,
                   usage={"input_tokens": _int(usage.get("prompt_tokens")), "output_tokens": _int(usage.get("completion_tokens")),
                          "total_tokens": _int(usage.get("total_tokens")),
                          "reported_cost": _cost(usage.get("cost"), "provider_response")},
                   sources=sources, retrieved_sources=retrieved,
                   environment={"web_search_used": True if (listed or urls) else None,
                                "tools_used": _tool_names(tools) or None},
                   collected_at=parse_time(raw.get("created")))


def _anthropic(raw: Dict[str, Any]) -> Dict[str, Any]:
    texts: List[str] = []
    sources: List[Dict[str, Any]] = []
    tools: List[str] = []
    retrieved: List[Dict[str, Any]] = []
    offset = 0
    for block in raw.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            text = block["text"]
            for citation in block.get("citations") or []:
                if isinstance(citation, dict) and isinstance(citation.get("url"), str):
                    # 인용이 붙은 text 블록 자체가 출처가 뒷받침하는 구절이다.
                    sources.append({"url": citation["url"], "title": citation.get("title") if isinstance(citation.get("title"), str) else None,
                                    "spans": [[offset, offset + len(text)]], "origin": "api_annotation"})
            texts.append(text)
            offset += len(text)
        elif kind in ("server_tool_use", "tool_use"):
            tools.append(block.get("name") if isinstance(block.get("name"), str) else kind)
        elif kind == "web_search_tool_result" and isinstance(block.get("content"), list):
            for found in block["content"]:
                if isinstance(found, dict) and isinstance(found.get("url"), str):
                    retrieved.append({"url": found["url"], "title": found.get("title"), "page_age": found.get("page_age")})
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    server = usage.get("server_tool_use") if isinstance(usage.get("server_tool_use"), dict) else {}
    finish = raw.get("stop_reason", "unknown")
    tool_names = _tool_names(tools)
    return _result("".join(texts), finish in ("end_turn", "stop_sequence", "refusal"), finish,
                   model=raw.get("model") if isinstance(raw.get("model"), str) else None,
                   usage={"input_tokens": _int(usage.get("input_tokens")), "output_tokens": _int(usage.get("output_tokens")),
                          "cached_input_tokens": _int(usage.get("cache_read_input_tokens")),
                          "search_requests": _int(server.get("web_search_requests"))},
                   sources=sources, retrieved_sources=retrieved,
                   environment={"web_search_used": True if _web(tool_names) else None, "tools_used": tool_names or None})


def _gemini(raw: Dict[str, Any]) -> Dict[str, Any]:
    candidate = (raw.get("candidates") or [{}])[0]
    candidate = candidate if isinstance(candidate, dict) else {}
    parts = (candidate.get("content") or {}).get("parts") if isinstance(candidate.get("content"), dict) else []
    answer = "".join(p["text"] for p in parts or [] if isinstance(p, dict) and isinstance(p.get("text"), str) and not p.get("thought"))
    grounding = candidate.get("groundingMetadata") if isinstance(candidate.get("groundingMetadata"), dict) else {}
    chunks = [c.get("web") if isinstance(c, dict) and isinstance(c.get("web"), dict) else None
              for c in grounding.get("groundingChunks") or []]
    segments: Dict[int, List[str]] = {}
    for support in grounding.get("groundingSupports") or []:
        if not isinstance(support, dict) or not isinstance(support.get("segment"), dict):
            continue
        text = support["segment"].get("text")
        for index in support.get("groundingChunkIndices") or []:
            if isinstance(index, int) and isinstance(text, str):
                segments.setdefault(index, []).append(text)
    sources = [{"url": web.get("uri"), "title": web.get("title"), "segments": segments.get(i, []), "origin": "api_annotation"}
               for i, web in enumerate(chunks) if web and isinstance(web.get("uri"), str)]
    queries = grounding.get("webSearchQueries") if isinstance(grounding.get("webSearchQueries"), list) else []
    notes = []
    if grounding:
        notes.append(quality("gemini_grounding_terms", "Gemini API 약관은 Grounding with Google Search 결과의 분석·저장 목적을 "
                                                       "제한합니다. 이 답변을 평가에 쓰기 전에 약관 검토가 필요합니다.", "warning"))
    usage = raw.get("usageMetadata") if isinstance(raw.get("usageMetadata"), dict) else {}
    finish = candidate.get("finishReason", "blocked")
    return _result(answer, finish == "STOP", finish,
                   model=raw.get("modelVersion") if isinstance(raw.get("modelVersion"), str) else None,
                   usage={"input_tokens": _int(usage.get("promptTokenCount")), "output_tokens": _int(usage.get("candidatesTokenCount")),
                          "total_tokens": _int(usage.get("totalTokenCount")), "reasoning_tokens": _int(usage.get("thoughtsTokenCount"))},
                   sources=sources, quality=notes,
                   environment={"web_search_used": True if queries or sources else None,
                                "tools_used": ["google_search"] if queries or sources else None})


def response_fields(parsed: Dict[str, Any], *, answer: Optional[str] = None) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """parse_api_response 결과를 공통 답변 필드로. ``answer`` 를 주면(기존 수집 경로가 뽑은 글자) 그 글자를 쓴다.

    두 글자가 다르면 출처 위치(span)는 버리고 표시·구절 문자열로만 출처를 잇는다.
    """
    notes = list(parsed["quality"])
    sources = parsed["sources"]
    if answer is not None and answer != parsed["answer"]:
        sources = [{k: v for k, v in s.items() if k != "spans"} for s in sources]
        notes.append(quality("api_text_mismatch", "수집 경로가 저장한 답변과 원본 응답에서 다시 읽은 글자가 달라 출처 위치는 쓰지 않았습니다.",
                             "info"))
    usage = {k: v for k, v in parsed["usage"].items() if v is not None}
    fields = {"answer": parsed["answer"] if answer is None else answer, "status": parsed["status"], "error": parsed["error"],
              "finish_reason": parsed["finish_reason"], "sources": sources, "environment": parsed["environment"],
              "usage": usage}
    if parsed["retrieved_sources"]:
        # 모델이 검색·참조한 전체 목록 — 답변이 든 출처(sources)와 달리 J5 대조 대상이 아니다.
        fields["retrieved_sources"] = parsed["retrieved_sources"]
    return fields, notes


API_RESPONSES_SCHEMA = "modelgrade-api-responses/v1"
_PROVIDER_OF_KIND = {"openai_responses": "openai", "chat_completions": "unknown", "anthropic_messages": "anthropic",
                     "gemini_generate": "google"}


def _request_environment(request: Any) -> Dict[str, Any]:
    """함께 보관한 요청 본문에서 확인되는 실행 환경. 요청이 없으면 모름(None)이다."""
    if not isinstance(request, dict):
        return {}
    declared = []
    for tool in request.get("tools") or []:
        if isinstance(tool, dict):
            name = tool.get("type") or tool.get("name") or (tool.get("function") or {}).get("name")
            if name == "function" and isinstance(tool.get("name"), str):
                name = tool["name"]
            if not name and any(k in tool for k in ("google_search", "googleSearch", "google_search_retrieval")):
                name = "google_search"
            if isinstance(name, str):
                declared.append(name)
    if request.get("web_search_options") is not None:
        declared.append("web_search")
    instruction = request.get("instructions") or request.get("system") or request.get("systemInstruction")
    if instruction is None:
        for message in request.get("messages") or request.get("input") or []:
            if isinstance(message, dict) and message.get("role") in ("system", "developer"):
                instruction = message.get("content")
                break
    text = instruction if isinstance(instruction, str) else json.dumps(instruction, ensure_ascii=False) if instruction else None
    settings = {k: request[k] for k in ("temperature", "top_p", "max_tokens", "max_output_tokens", "reasoning", "seed")
                if k in request and isinstance(request[k], (int, float, dict, str)) and not isinstance(request[k], bool)}
    return {"tools_available": _tool_names(declared), "web_search_available": _web(declared),
            "system_instruction": {"known": True, "text": text} if text else {"known": True, "note": "요청에 시스템 지시 없음"},
            "settings": settings}


def api_responses_file(value: Dict[str, Any], options: Dict[str, Any], name: str) -> Dict[str, Any]:
    """{"schema": "modelgrade-api-responses/v1", "system": {...}, "items": [{case_id, question, response, request?}]}"""
    from .builder import BundleBuilder, system_from
    items = value.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("items 배열에 질문과 API 응답(response)을 넣어 주세요.")
    first = next((i.get("response") for i in items if isinstance(i, dict) and isinstance(i.get("response"), dict)), None)
    kind = detect_kind(first)
    parsed_first = parse_api_response(first, kind) if kind else None
    env = _request_environment(next((i.get("request") for i in items if isinstance(i, dict)), None))
    defaults = {"system_key": None, "provider": _PROVIDER_OF_KIND.get(kind, "unknown"), "platform": "unknown",
                "model": (parsed_first or {}).get("model") or "unknown", "access_mode": "api",
                "settings": env.get("settings", {}),
                "environment": {"web_search_available": env.get("web_search_available"),
                                "tools_available": env.get("tools_available"),
                                "system_instruction": env.get("system_instruction", {"known": False})}}
    system = system_from({"system": {**(value.get("system") or {}), **((options or {}).get("system") or {})}}, defaults)
    builder = BundleBuilder(system, adapter="api_responses", format_label=f"API 응답 파일 ({KIND_LABELS.get(kind, kind)})",
                            source_name=name, collection_method="api_response_import")
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict) or not isinstance(item.get("question"), str):
            raise ValueError(f"items[{number - 1}]: question 문자열과 response 객체가 필요합니다.")
        raw = item.get("response")
        if isinstance(raw, dict):
            parsed = parse_api_response(raw)
            fields, notes = response_fields(parsed)
            response = {**fields, "collection_quality": notes, "raw": raw,
                        "collected_at": item.get("collected_at") or parsed["collected_at"]}
            if isinstance(item.get("latency_ms"), (int, float)):
                response["usage"]["latency_ms"] = item["latency_ms"]
            if parsed["model"] and parsed["model"] != system.get("model"):
                response["platform_ref"] = {"reported_model": parsed["model"]}
        else:
            error = item.get("error") if isinstance(item.get("error"), str) else "API 응답이 없습니다."
            response = {"answer": "", "status": "ERROR", "error": error, "collected_at": item.get("collected_at")}
        key = str(item.get("case_id") or f"Q{number:03d}")
        turn = item.get("turn") if isinstance(item.get("turn"), int) and not isinstance(item.get("turn"), bool) else None
        if item.get("conversation_id") is not None and turn is not None:
            builder.questions.append({"case_id": key, "question": item["question"], "conversation_id": item["conversation_id"],
                                      "turn": turn, **({"reference": item["reference"]} if "reference" in item else {})})
            builder.responses.append(builder._response(key, response))
        else:
            builder.add_conversation(key, [{"question": item["question"], "response": response}],
                                     reference=item.get("reference"))
    return builder.bundle()
