"""Claude Messages semantic judge with web provenance and cancellable HTTP.

Web search always enables citations, which cannot be combined with JSON output
format. A strict submit_evaluation tool carries the final schema instead. Only
provider server-tool result blocks authorize source URLs; user/model prose cannot.
Completed text JSON is accepted only after the original schema is checked locally.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import time
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from . import semantic_cli_common as semantic
from .io import validate_json_numbers
from .jev_client import JevCall, JevStatus, _sum_usage, _token_usage
from .llm_client import redact
from .semantic_evidence import (CONTEXT_CHECK_POLICY, FACT_CHECK_POLICY, SEARCH_POLICY,
                                finalize_answers, has_reference, search_mode, source_url)
from .semantic_sources import verify_answer_citations, verify_web_sources

SEARCH_TOOL = "web_search_20250305"
FETCH_TOOL = "web_fetch_20250910"
FINAL_TOOL = "submit_evaluation"


def _system_prompt(search_enabled):
    # The shared CLI prompt asks for text JSON. Messages uses a final tool, so
    # replace that instruction instead of appending a contradictory exception.
    prompt = semantic.system_prompt(search_enabled).replace("지정된 JSON 형식으로만 답합니다.", "")
    prompt = prompt.replace("J5 사실 확인을 위한 웹검색만 허용합니다. 파일·명령·다른 도구는 사용하지 않습니다.",
                            "J5 사실 확인에 web_search와 web_fetch 도구를 사용할 수 있습니다. 파일·명령 도구는 사용하지 않습니다.")
    prompt = prompt.replace("파일·명령·검색·외부 도구를 사용하지 않습니다.",
                            "파일·명령·검색 도구를 사용하지 않습니다.")
    return prompt + (
        "\n최종 판정은 submit_evaluation 도구를 정확히 한 번 호출하여 제출하세요. "
        "일반 텍스트나 Markdown 코드 블록으로 최종 JSON을 출력하지 마세요. "
        "필요한 검색과 원문 확인을 먼저 마치고 결과를 읽은 뒤 제출하세요. "
        "검색·원문 도구와 submit_evaluation을 동시에 호출하지 마세요. "
        "최종 도구 인자에는 모든 요청 기준과 필요한 증거를 포함하되 원문 전문이나 같은 근거를 불필요하게 반복하지 마세요. "
        "이유는 짧은 한두 문장으로, 인용은 판정에 필요한 연속된 구절로 적으세요. "
        "근거가 부족하면 UNVERIFIED로 남기세요. confidence는 0~1입니다."
    )


def prompt_cache_setting(value):
    """SEMANTIC_ANTHROPIC_PROMPT_CACHE — 비어 있으면 on. on/off(true/false, 1/0)만 받는다."""
    text = (value or "on").strip().lower()
    if text in ("on", "true", "1", "yes"):
        return True
    if text in ("off", "false", "0", "no"):
        return False
    raise ValueError("SEMANTIC_ANTHROPIC_PROMPT_CACHE 는 on 또는 off 여야 합니다.")


@dataclass(frozen=True)
class AnthropicSemanticConfig:
    model: str = "claude-sonnet-5-5"
    reasoning_effort: str = "medium"
    api_base: str = "https://api.anthropic.com/v1"
    timeout: float = 180
    max_output_tokens: int = 12000
    web_search: str = "live"
    max_tool_calls: int = 4
    max_fetches: int = 8
    max_continuations: int = 2
    prompt_cache: bool = True
    engine: str = field(default="anthropic_api", init=False)

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("Claude 의미 평가 모델명이 필요합니다.")
        if self.reasoning_effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ValueError("Claude effort 설정이 올바르지 않습니다.")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Claude API 제한 시간은 양의 유한한 초여야 합니다.")
        for name in ("max_output_tokens", "max_tool_calls", "max_fetches", "max_continuations"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "max_continuations" else 1):
                raise ValueError(f"{name} 설정이 올바르지 않습니다.")
        if self.max_continuations > 5:
            raise ValueError("Claude pause_turn 이어가기는 최대 5회입니다.")
        if type(self.prompt_cache) is not bool:
            raise ValueError("prompt_cache 설정은 true 또는 false여야 합니다.")
        if self.web_search not in ("live", "disabled"):
            raise ValueError("검색 설정은 live 또는 disabled여야 합니다.")
        try:
            parsed = urlsplit(self.api_base)
            parsed.port
            valid = (parsed.scheme in ("https", "http") and parsed.hostname and
                     not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("Claude API 주소는 인증값·쿼리·프래그먼트 없는 HTTP(S) 주소여야 합니다.")

    def to_manifest(self):
        return {"engine": self.engine, "requested_model": self.model, "api_base": redact(self.api_base),
                "endpoint": "POST /messages", "reasoning_effort": self.reasoning_effort,
                "thinking": "adaptive", "output_mode": "strict_submit_evaluation_tool_or_validated_text_json_v3",
                "web_search": self.web_search, "search_tool": SEARCH_TOOL, "fetch_tool": FETCH_TOOL,
                "search_policy": SEARCH_POLICY, "fact_check_policy": FACT_CHECK_POLICY,
                "fact_check_scope": "all_answers", "context_check_policy": CONTEXT_CHECK_POLICY,
                "max_output_tokens": self.max_output_tokens, "max_tool_calls": self.max_tool_calls,
                "max_fetches": self.max_fetches, "max_continuations": self.max_continuations,
                "prompt_cache_policy": "static_prefix_ephemeral_5m" if self.prompt_cache else "disabled",
                "timeout_s": self.timeout, "max_attempts": 1, "truncation": "disabled",
                "credentials_source": "ANTHROPIC_API_KEY", "credentials_recorded": False,
                "confidence_note": "모델이 스스로 적은 확신도 — 보정된 확률 아님"}


def _strict_schema(value):
    """Move unsupported numerical bounds to descriptions; local verdict validation remains."""
    if isinstance(value, list):
        return [_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _strict_schema(item) for key, item in value.items() if key not in ("minimum", "maximum")}
    bounds = [f"{key}: {value[key]}" for key in ("minimum", "maximum") if key in value]
    if bounds:
        result["description"] = (result.get("description", "") + " " + "; ".join(bounds)).strip()
    types = result.get("type")
    if isinstance(types, list):
        # Messages rejects enum + a type array even for a nullable string.
        # Equivalent typed anyOf branches keep the exact allowed values.
        choices = result.pop("enum", None)
        result.pop("type")
        branches = []
        for kind in types:
            branch = {"type": kind}
            if choices is not None:
                matching = [choice for choice in choices if (
                    (kind == "null" and choice is None) or
                    (kind == "string" and isinstance(choice, str)) or
                    (kind == "integer" and type(choice) is int) or
                    (kind == "number" and type(choice) in (int, float)) or
                    (kind == "boolean" and type(choice) is bool))]
                if not matching:
                    continue
                branch["enum"] = matching
            branches.append(branch)
        result["anyOf"] = branches
    return result


def _validate_output(value, schema, depth=0):
    """Validate our original schema subset; unfamiliar constraints fail closed.

    This deliberately uses semantic.output_schema, not the provider translation
    which moves numeric bounds into descriptions. It is not a general JSON Schema
    implementation: adding a new schema keyword requires explicit support here.
    """
    supported = {"type", "properties", "required", "additionalProperties", "items",
                 "enum", "minimum", "maximum", "description"}
    if depth > 64 or not isinstance(schema, dict) or set(schema) - supported:
        raise ValueError("Unsupported evaluation schema")
    kinds = schema.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    checks = {"object": lambda: isinstance(value, dict), "array": lambda: isinstance(value, list),
              "string": lambda: isinstance(value, str), "integer": lambda: type(value) is int,
              "number": lambda: type(value) in (int, float) and (type(value) is int or math.isfinite(value)),
              "null": lambda: value is None, "boolean": lambda: type(value) is bool}
    if not kinds or any(not isinstance(kind, str) or kind not in checks for kind in kinds):
        raise ValueError("Unsupported evaluation type")
    if not any(checks[kind]() for kind in kinds):
        raise ValueError("Invalid evaluation value type")
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or not any(
            value == choice and (type(value) is type(choice) or
                (type(value) in (int, float) and type(choice) in (int, float))) for choice in choices
        ):
            raise ValueError("Invalid evaluation enum")
    for bound in ("minimum", "maximum"):
        if bound in schema:
            limit = schema[bound]
            if (type(limit) not in (int, float) or (type(limit) is float and not math.isfinite(limit))
                    or type(value) not in (int, float)
                    or (bound == "minimum" and value < limit) or (bound == "maximum" and value > limit)):
                raise ValueError("Invalid evaluation numeric bound")
    if isinstance(value, dict):
        properties, required = schema.get("properties"), schema.get("required", [])
        if (not isinstance(properties, dict) or schema.get("additionalProperties") is not False
                or not isinstance(required, list) or any(not isinstance(key, str) for key in required)
                or not set(required) <= set(properties) or not set(required) <= set(value)
                or set(value) - set(properties)):
            raise ValueError("Invalid evaluation object fields")
        for key, item in value.items():
            _validate_output(item, properties[key], depth + 1)
    elif isinstance(value, list):
        if not isinstance(schema.get("items"), dict):
            raise ValueError("Unsupported evaluation array schema")
        for item in value:
            _validate_output(item, schema["items"], depth + 1)


def _text_submission(content, questions, citations=False):
    """Parse the complete text, never extract a convenient JSON substring."""
    fragments = [block.get("text") for block in content if block.get("type") == "text"]
    if not fragments or any(not isinstance(text, str) for text in fragments):
        raise ValueError("Missing evaluation text")
    text = "".join(fragments).strip()
    if text.startswith("```"):
        fenced = re.fullmatch(r"```json[ \t]*\r?\n(.*)\r?\n```", text, flags=re.S)
        if not fenced:
            raise ValueError("Incomplete evaluation JSON fence")
        text = fenced.group(1)
    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("Duplicate evaluation JSON key")
            obj[key] = value
        return obj
    def invalid_constant(_):
        raise ValueError("Nonfinite evaluation JSON number")
    try:
        value = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        validate_json_numbers(value)
        _validate_output(value, semantic.output_schema(questions, citations=citations))
    except RecursionError:
        raise ValueError("Evaluation JSON nesting limit exceeded") from None
    return value


def _complete_text_turn(blocks):
    """Text fallback cannot hide a client call or an orphan/malformed server result."""
    calls, finished = {}, set()
    result_types = {"web_search_tool_result": "web_search", "web_fetch_tool_result": "web_fetch"}
    for block in blocks:
        kind = block.get("type")
        if kind in ("text", "thinking", "redacted_thinking"):
            continue
        if kind == "server_tool_use":
            identity = block.get("id")
            if (not isinstance(identity, str) or not identity or identity in calls
                    or block.get("name") not in result_types.values()):
                return False
            calls[identity] = block["name"]
        elif kind in result_types:
            identity = block.get("tool_use_id")
            if not isinstance(identity, str) or calls.get(identity) != result_types[kind] or identity in finished:
                return False
            value = block.get("content")
            error = isinstance(value, dict) and value.get("type") == kind + "_error"
            valid = (isinstance(value, list) and all(isinstance(item, dict) and item.get("type") == "web_search_result"
                                                   for item in value)) if kind == "web_search_tool_result" else (
                isinstance(value, dict) and value.get("type") == "web_fetch_result")
            if not (valid or error):
                return False
            finished.add(identity)
        else:
            return False
    return set(calls) == finished


def _provenance(blocks):
    calls = {b.get("id"): b for b in blocks if isinstance(b, dict) and b.get("type") == "server_tool_use"}
    urls, fetched, searches, errors, finished = set(), set(), 0, [], set()
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in ("web_search_tool_result", "web_fetch_tool_result"):
            continue
        call = calls.get(block.get("tool_use_id"))
        if not call:
            continue
        content = block.get("content")
        search = block["type"] == "web_search_tool_result"
        if call.get("name") != ("web_search" if search else "web_fetch"):
            continue
        finished.add(block.get("tool_use_id"))
        if search and isinstance(content, list):
            searches += 1
            for item in content:
                if isinstance(item, dict) and item.get("type") == "web_search_result":
                    if url := source_url(item.get("url")):
                        urls.add(url)
        elif not search and isinstance(content, dict) and content.get("type") == "web_fetch_result":
            if url := source_url(content.get("url")):
                urls.add(url)
                fetched.add(url)
        elif isinstance(content, dict) and str(content.get("type", "")).endswith("_error"):
            errors.append(str(content.get("error_code", "unknown"))[:80])
    # fetched_urls: pages the provider itself opened (web_fetch). They are retrieval
    # provenance even without a search; search_performed still counts searches only.
    return {"search_performed": searches > 0, "source_urls": sorted(urls), "fetched_urls": sorted(fetched),
            "source_validation": "provider", "search_calls": searches, "tool_errors": errors,
            "checked_at": datetime.now(timezone.utc).isoformat()}, set(calls) - finished


class AnthropicSemanticClient:
    supports_cancellation = True

    def __init__(self, config, api_key, transport=None, *, source_fetcher=None):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("ANTHROPIC_API_KEY가 없습니다.")
        self.config, self._api_key = config, api_key
        self._transport, self._source_fetcher = transport, source_fetcher

    async def _post(self, payload, cancel_event, deadline):
        async with httpx.AsyncClient(base_url=self.config.api_base.rstrip("/") + "/",
                                     timeout=max(.01, deadline - time.monotonic()),
                                     follow_redirects=False, transport=self._transport) as client:
            task = asyncio.create_task(client.post("messages", json=payload, headers={
                "x-api-key": self._api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}))
            try:
                while not task.done():
                    if cancel_event is not None and cancel_event.is_set():
                        raise CancelledError()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise httpx.ReadTimeout("evaluation deadline")
                    await asyncio.wait({task}, timeout=min(.1, remaining))
                return task.result()
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def system_one(self, state, questions, *, cancel_event=None):
        started = time.monotonic()
        deadline = started + self.config.timeout
        parts, all_blocks, usage, attempts = [], [], {}, 0
        timings = {"api": 0.0, "source_verification": 0.0}
        verification, _ = _provenance([])
        search_enabled = self.config.web_search == "live" and "J5" in questions and not has_reference(state)
        mode = search_mode(questions, state, self.config.web_search)
        review = semantic.citation_review(state, questions, search_enabled)
        def result(status, *, body=None, error=None, http_status=None):
            body = body or {"verification": verification}
            body.setdefault("verification", {}).update(
                search_mode=mode, search_limits={"searches": self.config.max_tool_calls, "fetches": self.config.max_fetches})
            body.setdefault("verification", {})["timings_ms"] = {
                **{name: round(seconds * 1000, 1) for name, seconds in timings.items()},
                "total": round((time.monotonic() - started) * 1000, 1)}
            # Token/tool counts permit later terminal-only cost audits. Prices,
            # credentials, prompts and raw response blocks are not stored here.
            body["verification"]["usage_requests"] = copy.deepcopy(parts)
            return JevCall(status, body=body, error=error, http_status=http_status, attempts=attempts,
                           usage=usage, latency_ms=int((time.monotonic() - started) * 1000),
                           accounting={"model": self.config.model, "cost_parts": parts,
                                       "unreported_attempts": 0, "usage": usage})
        def check_cancel():
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError()
        try:
            check_cancel()
            tools = [{"name": FINAL_TOOL, "description": "검증을 끝낸 최종 기준별 판정을 제출합니다. 외부 작업을 실행하지 않습니다.",
                      "strict": True, "input_schema": _strict_schema(semantic.output_schema(questions, citations=review))}]
            if search_enabled:
                tools[:0] = [{"type": SEARCH_TOOL, "name": "web_search", "max_uses": self.config.max_tool_calls},
                             {"type": FETCH_TOOL, "name": "web_fetch", "max_uses": self.config.max_fetches,
                              "citations": {"enabled": True}}]
            static, dynamic = semantic.build_prompt_parts(state, questions, search_enabled)
            content = static + "\n" + dynamic
            if self.config.prompt_cache:
                # Same text, split at the end of the reusable instructions. The marker caches
                # tools + system + static rules; the timestamp and evaluation data follow it.
                # With caching on, server tools also cache their results between iterations.
                content = [{"type": "text", "text": static + "\n", "cache_control": {"type": "ephemeral"}},
                           {"type": "text", "text": dynamic}]
            payload = {"model": self.config.model, "max_tokens": self.config.max_output_tokens,
                       "thinking": {"type": "adaptive"}, "output_config": {"effort": self.config.reasoning_effort},
                       "service_tier": "standard_only", "tools": tools, "tool_choice": {"type": "auto"},
                       "system": _system_prompt(search_enabled),
                       "messages": [{"role": "user", "content": content}]}
            validate_json_numbers(payload)
            for continuation in range(self.config.max_continuations + 1):
                check_cancel()
                if time.monotonic() >= deadline:
                    return result(JevStatus.TIMEOUT, error="Claude 의미 평가 제한 시간을 초과했습니다.")
                part = {"model": self.config.model, "usage": {}, "web_search_calls": None,
                        "service_tier": None, "inference_geo": None}
                parts.append(part)
                attempts += 1
                request_started = time.monotonic()
                try:
                    response = asyncio.run(self._post(payload, cancel_event, deadline))
                finally:
                    timings["api"] += time.monotonic() - request_started
                try:
                    raw = response.json()
                    validate_json_numbers(raw)
                    if not isinstance(raw, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    return result(JevStatus.INVALID_RESPONSE, http_status=response.status_code,
                                  error="Claude API 응답이 유효한 JSON 객체가 아닙니다.")
                raw_usage = _token_usage(raw.get("usage"))
                _sum_usage(usage, raw_usage)
                server_usage = raw_usage.get("server_tool_use") or {}
                count = server_usage.get("web_search_requests") if isinstance(server_usage, dict) else None
                part.update(model=raw.get("model") or self.config.model, usage=raw_usage,
                            web_search_calls=count if type(count) is int and count >= 0 else None if search_enabled else 0,
                            service_tier=raw_usage.get("service_tier"), inference_geo=raw_usage.get("inference_geo"))
                if not 200 <= response.status_code < 300:
                    return result(JevStatus.API_ERROR, http_status=response.status_code,
                                  error=f"Claude 의미 평가 API HTTP {response.status_code}. 자동 재시도하지 않았습니다.")
                content = raw.get("content")
                if not isinstance(content, list) or not all(isinstance(b, dict) for b in content):
                    return result(JevStatus.INVALID_RESPONSE, error="Claude API 출력 항목이 올바르지 않습니다.")
                all_blocks.extend(content)
                verification, pending = _provenance(all_blocks)
                stop_reason = raw.get("stop_reason")
                verification["provider_stop_reason"] = stop_reason if stop_reason in (
                    "tool_use", "pause_turn", "end_turn", "max_tokens", "stop_sequence", "refusal",
                    "model_context_window_exceeded") else "unknown"
                if any(b.get("type") in ("compaction", "context_compaction") for b in all_blocks):
                    return result(JevStatus.INVALID_RESPONSE, error="원문 이력이 축약되어 평가를 확정하지 않았습니다.")
                if raw.get("stop_reason") == "pause_turn":
                    check_cancel()
                    if continuation == self.config.max_continuations:
                        return result(JevStatus.API_ERROR, error="Claude 검색 이어가기 한도를 초과해 미판정으로 남깁니다.")
                    # max_uses is per HTTP request. Preserve the case's budget
                    # across pause_turn instead of granting a fresh tool budget.
                    completed_ids = {b.get('tool_use_id') for b in all_blocks
                                     if b.get('type') in ('web_search_tool_result', 'web_fetch_tool_result')}
                    for tool in payload['tools']:
                        if tool['name'] not in ('web_search', 'web_fetch'):
                            continue
                        used = {b.get('id') for b in all_blocks if b.get('type') == 'server_tool_use'
                                and b.get('name') == tool['name'] and b.get('id') in completed_ids}
                        limit = self.config.max_tool_calls if tool['name'] == 'web_search' else self.config.max_fetches
                        remaining = limit - len(used)
                        if remaining <= 0:
                            return result(JevStatus.API_ERROR, error="Claude 검색·원문 확인 한도에 도달한 미완료 응답입니다. 추가 호출하지 않았습니다.")
                        tool['max_uses'] = remaining
                    # This resumes the exact paused turn, not a retry or a fresh evaluation.
                    payload["messages"].append({"role": "assistant", "content": copy.deepcopy(content)})
                    continue
                submissions = [b for b in content if b.get("type") == "tool_use"]
                output_transport = "strict_tool"
                if stop_reason == "end_turn" and not submissions and not pending and _complete_text_turn(all_blocks):
                    try:
                        structured = _text_submission(content, questions, review)
                    except (ValueError, TypeError, UnicodeError):
                        return result(JevStatus.INVALID_RESPONSE, error="Claude 일반 텍스트 최종 판정이 완전한 JSON 또는 요청된 평가 형식과 일치하지 않습니다.")
                    output_transport = "validated_text_json"
                elif stop_reason == "tool_use" and len(submissions) == 1 and submissions[0].get("name") == FINAL_TOOL and not pending:
                    structured = submissions[0].get("input")
                else:
                    reason = {
                        "max_tokens": "Claude 평가 출력 토큰 한도에 도달해 최종 판정이 잘렸습니다.",
                        "model_context_window_exceeded": "Claude 평가 문맥 한도에 도달해 최종 판정을 완료하지 못했습니다.",
                        "refusal": "Claude가 평가 요청에 대한 판정 제공을 거절했습니다.",
                        "end_turn": "Claude 응답이 최종 판정 제출 도구 없이 종료됐습니다.",
                    }.get(stop_reason, "Claude 최종 판정이 미완료이거나 검색·도구 응답이 끝나지 않았습니다.")
                    return result(JevStatus.INVALID_RESPONSE, error=reason)
                model = raw.get("model")
                if (not isinstance(structured, dict) or set(structured) != set(questions)
                        or not all(isinstance(entry, dict) for entry in structured.values())
                        or not isinstance(model, str) or not model.strip()):
                    return result(JevStatus.INVALID_RESPONSE, error="Claude 판정의 기준 ID·모델 기록이 올바르지 않습니다.")
                verification["output_transport"] = output_transport
                if part["web_search_calls"] is None and not any(
                    b.get("type") == "web_search_tool_result" or
                    (b.get("type") == "server_tool_use" and b.get("name") == "web_search") for b in content
                ):
                    # Anthropic omits server_tool_use usage on completed turns
                    # with no search. Complete provider blocks establish zero;
                    # errors, pending tools and missing counts after a search do not.
                    part["web_search_calls"] = 0
                body = {"model": model, "verification": verification,
                        "answers": {cid: semantic.jev_answer(structured[cid]) for cid in questions}}
                fact = body["answers"].get("J5", {}).get("fact_check")
                if isinstance(fact, dict) and fact.get("basis") in ("web", "mixed"):
                    source_started = time.monotonic()
                    try:
                        body = verify_web_sources(body, fetcher=self._source_fetcher, cancel_event=cancel_event)
                    finally:
                        timings["source_verification"] += time.monotonic() - source_started
                if review:
                    source_started = time.monotonic()
                    try:
                        body = verify_answer_citations(body, state, fetcher=self._source_fetcher, cancel_event=cancel_event)
                    finally:
                        timings["source_verification"] += time.monotonic() - source_started
                return result("OK", body=finalize_answers(body, state, questions), http_status=response.status_code)
        except CancelledError:
            return result(JevStatus.SKIPPED, error="평가 중단 요청으로 Claude API 평가를 종료했습니다.")
        except httpx.TimeoutException:
            return result(JevStatus.TIMEOUT, error="Claude 의미 평가 제한 시간을 초과했습니다. 자동 재시도하지 않았습니다.")
        except (httpx.HTTPError, httpx.InvalidURL):
            return result(JevStatus.API_ERROR, error="Claude 의미 평가 API 연결 실패. 자동 재시도하지 않았습니다.")
        except (ValueError, TypeError, UnicodeError):
            return result(JevStatus.INVALID_RESPONSE, error="Claude 의미 평가 요청·응답을 원문 그대로 처리할 수 없습니다.")
