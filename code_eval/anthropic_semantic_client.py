"""Claude Messages semantic judge with web provenance and cancellable HTTP.

Web search always enables citations, which cannot be combined with JSON output
format. A strict submit_evaluation tool carries the final schema instead. Only
provider server-tool result blocks authorize source URLs; user/model prose cannot.
Completed text JSON is accepted only after the original schema is checked locally.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import threading
import time
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from . import attachment_images
from . import semantic_cli_common as semantic
from .io import validate_json_numbers
from .jev_client import JevCall, JevStatus, _sum_usage, _token_usage
from .llm_client import redact
from .semantic_evidence import (CONTEXT_CHECK_POLICY, FACT_CHECK_POLICY, SEARCH_POLICY,
                                finalize_answers, has_reference, search_mode, source_url)
from .semantic_sources import verify_answer_citations, verify_web_sources

#: 이보다 긴 현재 질문·자료(J7 참조 자료 등)는 같은 앞부분을 쓰는 요청 하나가 먼저 캐시를 만든 뒤 나머지가 보낸다 — 동시에 보내면
#: 캐시가 생기기 전이라 모두 정가를 낸다 (4개 모델이 같은 50만 토큰 자료를 각자 쓰던 문제, 2026-10-14)
SHARED_GATE_CHARS = 50_000
SHARED_GATE_WAIT_S = 900    # 첫 요청을 기다리는 최대 시간
SHARED_GATE_WARM_S = 240    # 첫 요청이 성공한 뒤 '캐시 있음'으로 보는 시간 — 5분 캐시보다 짧게, 지나면 다음 요청이 다시 첫 요청
SHARED_GATE_POLL_S = 0.5    # 기다리는 동안 중단 요청·제한 시간을 확인하는 간격
_SHARED_GATES: dict = {}    # key → {"event": Event, "warm_until": None(첫 요청 진행 중) | 시각}
_SHARED_GATES_LOCK = threading.Lock()


def _enter_shared_gate(key, cancel_event=None, deadline=None):
    """같은 앞부분의 첫 요청이면 event 를 돌려준다 — 응답 뒤 ``_leave_shared_gate``. 캐시가 있으면 None(바로 보냄).

    첫 요청이 진행 중이면 끝날 때까지 기다린다. 기다리는 동안 중단 요청이면 CancelledError, 평가 제한 시간이나
    SHARED_GATE_WAIT_S 가 지나면 기다리지 않고 None. 첫 요청이 실패하면 기다리던 요청 하나가 새 첫 요청이 된다 (Codex 리뷰)."""
    wait_until = time.monotonic() + SHARED_GATE_WAIT_S
    if deadline is not None:
        wait_until = min(wait_until, deadline)
    while True:
        with _SHARED_GATES_LOCK:
            now = time.monotonic()
            for stale in [k for k, g in _SHARED_GATES.items() if g["warm_until"] is not None and g["warm_until"] <= now]:
                del _SHARED_GATES[stale]   # 캐시가 식었을 만한 항목 — 쌓이지 않게 지운다
            gate = _SHARED_GATES.get(key)
            if gate is None:
                event = threading.Event()
                _SHARED_GATES[key] = {"event": event, "warm_until": None}
                return event
            if gate["warm_until"] is not None:
                return None
            event = gate["event"]
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()
        remaining = wait_until - time.monotonic()
        if remaining <= 0:
            return None
        event.wait(min(SHARED_GATE_POLL_S, remaining))


def _leave_shared_gate(key, event, ok, started=None):
    """첫 요청이 끝남 — 성공이면 요청을 시작한 때(``started``)부터 SHARED_GATE_WARM_S 동안 '캐시 있음', 실패면 항목을 지워
    기다리던 요청이 다시 첫 요청이 된다. 캐시 수명은 요청 시작부터 재므로 응답이 늦게 끝나도 그만큼 줄어든다 (Codex 리뷰)."""
    with _SHARED_GATES_LOCK:
        gate = _SHARED_GATES.get(key)
        if gate is not None and gate["event"] is event:
            if ok:
                gate["warm_until"] = (time.monotonic() if started is None else started) + SHARED_GATE_WARM_S
            else:
                del _SHARED_GATES[key]
    event.set()


SEARCH_TOOL = "web_search_20250305"
FETCH_TOOL = "web_fetch_20250910"
FINAL_TOOL = "submit_evaluation"


def _system_prompt(search_enabled, questions=None):
    # The shared CLI prompt asks for text JSON. Messages uses a final tool, so
    # replace that instruction instead of appending a contradictory exception.
    # items 파이프라인 지시문도 같은 문장을 품고 있어 치환이 그대로 동작한다.
    prompt = semantic.system_prompt(search_enabled, questions).replace("\n- 지정된 JSON 형식으로만 답합니다.", "")
    prompt = prompt.replace("지정된 JSON 형식으로만 답합니다.", "")
    prompt = prompt.replace("J5 사실 확인을 위한 웹검색만 허용합니다. 파일·명령·다른 도구는 사용하지 않습니다.",
                            "J5 사실 확인에 web_search와 web_fetch 도구를 사용할 수 있습니다. 파일·명령 도구는 사용하지 않습니다.")
    prompt = prompt.replace("파일·명령·검색·외부 도구를 사용하지 않습니다.",
                            "파일·명령·검색 도구를 사용하지 않습니다.")
    closing = ("항목마다 YES / NO / NOT_APPLICABLE 과 원문 인용을 적으세요." if semantic.is_items(questions)
               else "근거가 부족하면 UNVERIFIED로 남기세요. confidence는 0~1입니다.")
    return prompt + (
        "\n최종 판정은 submit_evaluation 도구를 정확히 한 번 호출하여 제출하세요. "
        "일반 텍스트나 Markdown 코드 블록으로 최종 JSON을 출력하지 마세요. "
        "필요한 검색과 원문 확인을 먼저 마치고 결과를 읽은 뒤 제출하세요. "
        "검색·원문 도구와 submit_evaluation을 동시에 호출하지 마세요. "
        "최종 도구 인자에는 모든 요청 기준과 필요한 증거를 포함하되 원문 전문이나 같은 근거를 불필요하게 반복하지 마세요. "
        "이유는 짧은 한두 문장으로, 인용은 판정에 필요한 연속된 구절로 적으세요. "
        + closing
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
    max_output_tokens: int = 20000   # 12000 에서 J6 대화 평가가 추론까지 한도에 닿아 판정이 잘렸다 (2026-10-14 실행)
    web_search: str = "live"
    max_tool_calls: int = 4
    max_fetches: int = 8
    max_continuations: int = 2
    #: 호출 방식 — realtime(질문마다 Messages 호출) | batch(Message Batches 에 묶어 제출, 폴링). 판정 규칙은 같다.
    dispatch: str = "realtime"
    batch_questions: int = 20          # 묶음 하나에 넣는 질문 수
    batch_poll_seconds: float = 15.0   # 묶음 상태 조회 주기(초, 10~30 권장)
    prompt_cache: bool = True
    engine: str = field(default="anthropic_api", init=False)

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("Claude 의미 평가 모델명이 필요합니다.")
        if self.dispatch not in ("realtime", "batch"):
            raise ValueError("SEMANTIC_DISPATCH 는 realtime 또는 batch 여야 합니다.")
        if type(self.batch_questions) is not int or not 1 <= self.batch_questions <= 1000:
            raise ValueError("묶음 질문 수(batch_questions)는 1~1000 사이의 정수여야 합니다.")
        if (isinstance(self.batch_poll_seconds, bool) or not isinstance(self.batch_poll_seconds, (int, float))
                or not math.isfinite(self.batch_poll_seconds) or not 1 <= self.batch_poll_seconds <= 300):
            raise ValueError("묶음 상태 조회 주기(batch_poll_seconds)는 1~300초여야 합니다.")
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
                "image_input": attachment_images.JUDGE_IMAGE_POLICY_VERSION,
                "max_output_tokens": self.max_output_tokens, "max_tool_calls": self.max_tool_calls,
                "max_fetches": self.max_fetches, "max_continuations": self.max_continuations,
                "dispatch": self.dispatch,
                **({"batch_questions": self.batch_questions, "batch_poll_seconds": self.batch_poll_seconds} if self.dispatch == "batch" else {}),
                "prompt_cache_policy": "static_history_question_ephemeral_5m" if self.prompt_cache else "disabled",
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
                 "enum", "minimum", "maximum", "description", "anyOf"}
    if depth > 64 or not isinstance(schema, dict) or set(schema) - supported:
        raise ValueError("Unsupported evaluation schema")
    if "anyOf" in schema:
        # items 스키마의 null 허용 필드(turn·fact): 분기 중 하나라도 통과하면 OK. 분기 밖의 다른 키워드는 함께 두지 않는다.
        branches = schema["anyOf"]
        if set(schema) - {"anyOf", "description"} or not isinstance(branches, list) or not branches:
            raise ValueError("Unsupported evaluation schema")
        for branch in branches:
            try:
                _validate_output(value, branch, depth + 1)
                return
            except ValueError:
                continue
        raise ValueError("Invalid evaluation value for anyOf")
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
    urls, fetched, searches, finished = set(), set(), 0, set()
    errors, search_errors, fetch_errors = [], [], []
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
            code = str(content.get("error_code", "unknown"))[:80]
            errors.append(code)
            (search_errors if search else fetch_errors).append(code)
    # fetched_urls: pages the provider itself opened (web_fetch). They are retrieval
    # provenance even without a search; search_performed still counts searches only.
    # tool_errors keeps every tool error; search_errors / fetch_errors say which tool failed.
    return {"search_performed": searches > 0, "source_urls": sorted(urls), "fetched_urls": sorted(fetched),
            "source_validation": "provider", "search_calls": searches, "tool_errors": errors,
            "search_errors": search_errors, "fetch_errors": fetch_errors,
            "checked_at": datetime.now(timezone.utc).isoformat()}, set(calls) - finished


#: 공급자 원문 한 건의 최대 글자 수 — 서버 수집 한도(2 MiB)와 같은 크기. 넘으면 대조에 쓰지 않는다.
MAX_PROVIDER_DOCUMENT_CHARS = 2 * 1024 * 1024


def _provider_documents(blocks):
    """같은 요청에서 공급자 web_fetch 가 실제로 받은 텍스트 원문 — (URL → [{text, retrieved_at}], 기록용 목록).

    server_tool_use(web_fetch) 의 id·요청 URL 과 결과 블록의 tool_use_id·URL 이 모두 맞는 결과만 쓴다.
    평가자가 확인했다고 적은 내용은 원문이 아니다. PDF(base64) 등 텍스트가 아닌 원문은 대조하지 않는다.
    원문은 메모리에서만 쓰고 결과에는 URL·받은 시각·글자 수만 남긴다.
    """
    calls = {b.get("id"): b for b in blocks
             if isinstance(b, dict) and b.get("type") == "server_tool_use" and b.get("name") == "web_fetch"}
    documents, audit = {}, []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "web_fetch_tool_result":
            continue
        call, content = calls.get(block.get("tool_use_id")), block.get("content")
        if not call or not isinstance(content, dict) or content.get("type") != "web_fetch_result":
            continue
        url = source_url(content.get("url"))
        requested = source_url(call["input"].get("url")) if isinstance(call.get("input"), dict) else None
        document = content.get("content")
        source = document.get("source") if isinstance(document, dict) and document.get("type") == "document" else None
        if (not url or url != requested or not isinstance(source, dict) or source.get("type") != "text"
                or not isinstance(source.get("data"), str) or not source["data"].strip()
                or len(source["data"]) > MAX_PROVIDER_DOCUMENT_CHARS):
            continue
        retrieved = content.get("retrieved_at") if isinstance(content.get("retrieved_at"), str) else None
        documents.setdefault(url, []).append({"text": source["data"], "retrieved_at": retrieved})
        audit.append({"url": url, "retrieved_at": retrieved, "chars": len(source["data"])})
    return documents, audit


#: Anthropic 오류 종류 → 고정 설명. 목록 밖의 종류·응답 문구는 기록하지 않는다.
_API_ERROR_TYPES = {"invalid_request_error": "요청 오류", "authentication_error": "인증 오류", "permission_error": "권한 오류",
                    "billing_error": "결제·크레딧 오류", "not_found_error": "모델·경로 없음", "request_too_large": "요청 크기 초과",
                    "rate_limit_error": "요청 한도 초과", "api_error": "Anthropic 서버 오류", "overloaded_error": "Anthropic 과부하"}


def _api_error_label(raw: Any) -> str:
    """오류 응답 → ' (요청 오류 · 크레딧 잔액 부족)' 같은 고정 설명. 응답 문구 자체는 돌려주지 않는다."""
    error = raw.get("error") if isinstance(raw, dict) and isinstance(raw.get("error"), dict) else {}
    label = _API_ERROR_TYPES.get(error.get("type"))
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    credit = bool(re.search(r"credit balance|billing|purchase credits", message, re.I))
    parts = [p for p in (label, "크레딧 잔액 부족 — Anthropic 콘솔에서 충전 필요" if credit else None) if p]
    return f" ({' · '.join(parts)})" if parts else ""


class AnthropicSemanticClient:
    supports_cancellation = True
    supports_images = True   # 첨부 이미지를 base64 이미지 블록으로 함께 보낸다 (judge-attachment-images-v1)

    def __init__(self, config, api_key, transport=None, *, source_fetcher=None):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("ANTHROPIC_API_KEY가 없습니다.")
        self.config, self._api_key = config, api_key
        self._transport, self._source_fetcher = transport, source_fetcher

    @property
    def ordered_turns(self):
        """캐시를 쓰면 같은 대화의 턴을 앞 턴이 끝난 뒤 평가한다 — 앞 턴이 만든 캐시를 다음 턴이 읽게 (evaluate_jev_batch 가 본다)."""
        return bool(self.config.prompt_cache)

    def batch_client(self):
        """같은 키·주소·transport 로 Message Batches 클라이언트를 만든다 (jev_batch 디스패처용)."""
        from .anthropic_batch_client import AnthropicBatchClient
        return AnthropicBatchClient(self.config.api_base, self._api_key, transport=self._transport, timeout=self.config.timeout)

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

    # --- 요청 조립 · 응답 처리 · 실시간/Batch 입구 ---------------------------------------------------------
    # 실시간(system_one)과 Message Batches(finish_batch_result)가 같은 조립·응답 처리를 쓴다. 차이는 HTTP 를 누가 보내느냐뿐이다.

    def build_request(self, state, questions, images=None):
        """(payload, context) — 첫 HTTP 요청 본문과, 응답 처리에 필요한 문맥 {questions(보낸 것), not_sent, search_enabled, items_mode}.

        ``images`` (jev_eval 이 확인한 첨부 그림 [{name, media_type, data}]) 가 있으면 공통 지시문 뒤·평가 대상 앞에 그림을 넣는다.
        Batches 디스패처는 payload 를 ``params`` 로 제출하고, 결과 메시지를 ``finish_batch_result(raw, context)`` 에 넘긴다.
        """
        search_enabled = self.config.web_search == "live" and semantic.search_requested(questions) and not has_reference(state)
        mode = search_mode(questions, state, self.config.web_search)
        items_mode = semantic.is_items(questions)
        not_sent = []
        if items_mode and not search_enabled:
            # 검색이 꺼진 설정: 라이브 검색이 있어야 하는 항목(J5-1.a)은 기억으로 판정시키지 않고 보내지 않는다
            questions, removed = semantic.strip_search_items(questions)
            not_sent = [{"id": item_id, "reason": semantic.SEARCH_NOT_AVAILABLE_REASON} for item_id in removed]
        review = semantic.citation_review(state, questions, search_enabled)
        tools = [{"name": FINAL_TOOL, "description": "검증을 끝낸 최종 기준별 판정을 제출합니다. 외부 작업을 실행하지 않습니다.",
                  "strict": True, "input_schema": _strict_schema(semantic.output_schema(questions, citations=review))}]
        if search_enabled:
            tools[:0] = [{"type": SEARCH_TOOL, "name": "web_search", "max_uses": self.config.max_tool_calls},
                         {"type": FETCH_TOOL, "name": "web_fetch", "max_uses": self.config.max_fetches,
                          "citations": {"enabled": True}}]
        static, dynamic = semantic.build_prompt_parts(state, questions, search_enabled)
        pictures = attachment_images.labelled_blocks("claude", images) if images else []
        content = static + "\n" + dynamic
        if pictures:
            content = [{"type": "text", "text": static + "\n"}, *pictures, {"type": "text", "text": dynamic}]
        shared_key = None
        # items 는 질문마다 앞부분(항목 목록·적용 근거·제출 스키마)이 달라 질문끼리 캐시를 나눠 쓰지 못한다 — 검색 도구가 붙어
        # 같은 요청 안의 검색 반복 입력을 캐시로 읽을 때만 표시한다(검색 없는 요청은 저장 비용만 든다). legacy 는 기준 조합별로 공유된다.
        if self.config.prompt_cache and items_mode and search_enabled:
            # Same text, split at the end of the reusable instructions. The marker caches
            # tools + system + static rules; the timestamp and evaluation data follow it.
            # With caching on, server tools also cache their results between iterations.
            content = [{"type": "text", "text": static + "\n", "cache_control": {"type": "ephemeral"}},
                       *pictures, {"type": "text", "text": dynamic}]
        elif self.config.prompt_cache and not items_mode:
            # legacy — 지시문 → history 턴 블록 → 현재 질문·자료 → 답변 순서로 나눠, 같은 대화의 다음 턴은 앞 턴을, 같은 질문의
            # 다른 모델은 긴 자료를 캐시로 읽는다 (2026-10-14 비용 결정, 내용은 같은 원문). 캐시 표시는 최대 3개(한도 4).
            # 첨부 그림은 캐시 없는 요청과 같이 지시문 뒤·평가 대상 앞에 둔다.
            blocks = semantic.build_prompt_blocks(state, questions, search_enabled)
            content = [{"type": "text", "text": text, **({"cache_control": {"type": "ephemeral"}} if cached else {})}
                       for text, cached in blocks]
            content[1:1] = pictures
            history = state.get("history") if isinstance(state, dict) else None
            shared = blocks[-2][0]
            if not history and len(shared) >= SHARED_GATE_CHARS:   # 긴 자료를 4개 모델이 같이 읽는 질문(J7) — 한 모델이 먼저 캐시를 만든다
                shared_key = hashlib.sha256(json.dumps([self.config.model, tools, _system_prompt(search_enabled, questions),
                                                        blocks[0][0], pictures, shared], ensure_ascii=False).encode()).hexdigest()
        payload = {"model": self.config.model, "max_tokens": self.config.max_output_tokens,
                   "thinking": {"type": "adaptive"}, "output_config": {"effort": self.config.reasoning_effort},
                   "service_tier": "standard_only", "tools": tools, "tool_choice": {"type": "auto"},
                   "system": _system_prompt(search_enabled, questions),
                   "messages": [{"role": "user", "content": content}]}
        validate_json_numbers(payload)
        return payload, {"questions": questions, "not_sent": not_sent, "search_enabled": search_enabled, "items_mode": items_mode,
                         "review": review, "search_mode": mode, "shared_key": shared_key}

    def _session(self, context):
        """한 질문의 응답 처리 상태 — 요청 조각(parts)·누적 블록·usage·시도 수·시간 기록·결과 만들기."""
        started = time.monotonic()
        session = {"parts": [], "all_blocks": [], "usage": {}, "attempts": 0, "timings": {"api": 0.0, "source_verification": 0.0},
                   "verification": _provenance([])[0], "not_sent": context["not_sent"], "search_mode": context.get("search_mode"),
                   "started": started}

        def result(status, *, body=None, error=None, http_status=None):
            body = body or {"verification": session["verification"]}
            body.setdefault("verification", {}).update(
                search_mode=session["search_mode"], search_limits={"searches": self.config.max_tool_calls, "fetches": self.config.max_fetches})
            body.setdefault("verification", {})["timings_ms"] = {
                **{name: round(seconds * 1000, 1) for name, seconds in session["timings"].items()},
                "total": round((time.monotonic() - started) * 1000, 1)}
            if session["not_sent"]:
                body["verification"]["items_not_sent"] = copy.deepcopy(session["not_sent"])
            # Token/tool counts permit later terminal-only cost audits. Prices,
            # credentials, prompts and raw response blocks are not stored here.
            body["verification"]["usage_requests"] = copy.deepcopy(session["parts"])
            return JevCall(status, body=body, error=error, http_status=http_status, attempts=session["attempts"],
                           usage=session["usage"], latency_ms=int((time.monotonic() - started) * 1000),
                           accounting={"model": self.config.model, "cost_parts": session["parts"],
                                       "unreported_attempts": 0, "usage": session["usage"]})
        session["result"] = result
        return session

    def _begin_attempt(self, session):
        """요청 조각(part)을 열고 시도 수를 센다 — HTTP 를 보내기 전에 호출해, 중단·오류로 응답을 못 받아도 시도로 남는다."""
        part = {"model": self.config.model, "usage": {}, "web_search_calls": None, "service_tier": None, "inference_geo": None}
        session["parts"].append(part)
        session["attempts"] += 1
        return part

    def _consume(self, raw, status_code, session, state, context, payload, *, continuation, cancel_event, part=None):
        """응답 하나를 처리한다 → ("done", JevCall) | ("continue", None: pause_turn 이어가기 — payload 는 제자리에서 갱신됨).

        실시간 루프는 "continue" 면 다시 POST 하고, Batches 는 실시간 재전송으로 넘긴다. ``part`` 가 없으면 여기서 시도를 연다.
        """
        questions, search_enabled, items_mode = context["questions"], context["search_enabled"], context["items_mode"]
        result = session["result"]
        if part is None:
            part = self._begin_attempt(session)
        if not isinstance(raw, dict):
            return "done", result(JevStatus.INVALID_RESPONSE, http_status=status_code, error="Claude API 응답이 유효한 JSON 객체가 아닙니다.")
        raw_usage = _token_usage(raw.get("usage"))
        _sum_usage(session["usage"], raw_usage)
        server_usage = raw_usage.get("server_tool_use") or {}
        count = server_usage.get("web_search_requests") if isinstance(server_usage, dict) else None
        part.update(model=raw.get("model") or self.config.model, usage=raw_usage,
                    web_search_calls=count if type(count) is int and count >= 0 else None if search_enabled else 0,
                    service_tier=raw_usage.get("service_tier"), inference_geo=raw_usage.get("inference_geo"))
        if not 200 <= status_code < 300:
            # 'HTTP 400' 만으로는 크레딧 부족·요청 형식 오류를 구분하지 못했다 (2026-10-14 실행) — 응답 문구는 기록하지 않고
            # (질문·답변이 되돌아올 수 있음) 정해진 오류 종류와 크레딧 부족 여부만 고정 문구로 남긴다
            return "done", result(JevStatus.API_ERROR, http_status=status_code,
                                  error=f"Claude 의미 평가 API HTTP {status_code}{_api_error_label(raw)}. 자동 재시도하지 않았습니다.")
        content = raw.get("content")
        if not isinstance(content, list) or not all(isinstance(b, dict) for b in content):
            return "done", result(JevStatus.INVALID_RESPONSE, error="Claude API 출력 항목이 올바르지 않습니다.")
        all_blocks = session["all_blocks"]
        all_blocks.extend(content)
        verification, pending = _provenance(all_blocks)
        session["verification"] = verification
        stop_reason = raw.get("stop_reason")
        verification["provider_stop_reason"] = stop_reason if stop_reason in (
            "tool_use", "pause_turn", "end_turn", "max_tokens", "stop_sequence", "refusal",
            "model_context_window_exceeded") else "unknown"
        if any(b.get("type") in ("compaction", "context_compaction") for b in all_blocks):
            return "done", result(JevStatus.INVALID_RESPONSE, error="원문 이력이 축약되어 평가를 확정하지 않았습니다.")
        if stop_reason == "pause_turn":
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError()
            if continuation == self.config.max_continuations:
                return "done", result(JevStatus.API_ERROR, error="Claude 검색 이어가기 한도를 초과해 미판정으로 남깁니다.")
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
                    return "done", result(JevStatus.API_ERROR, error="Claude 검색·원문 확인 한도에 도달한 미완료 응답입니다. 추가 호출하지 않았습니다.")
                tool['max_uses'] = remaining
            # This resumes the exact paused turn, not a retry or a fresh evaluation.
            payload["messages"].append({"role": "assistant", "content": copy.deepcopy(content)})
            return "continue", None
        submissions = [b for b in content if b.get("type") == "tool_use"]
        output_transport = "strict_tool"
        if stop_reason == "end_turn" and not submissions and not pending and _complete_text_turn(all_blocks):
            try:
                structured = _text_submission(content, questions, context.get("review", False))
            except (ValueError, TypeError, UnicodeError):
                return "done", result(JevStatus.INVALID_RESPONSE, error="Claude 일반 텍스트 최종 판정이 완전한 JSON 또는 요청된 평가 형식과 일치하지 않습니다.")
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
            return "done", result(JevStatus.INVALID_RESPONSE, error=reason)
        model = raw.get("model")
        if items_mode:
            shape_errors = semantic.validate_items_shape(structured, questions)
        else:
            shape_errors = [] if (isinstance(structured, dict) and set(structured) == set(questions)
                                  and all(isinstance(entry, dict) for entry in structured.values())) else ["criterion mismatch"]
        if shape_errors or not isinstance(model, str) or not model.strip():
            return "done", result(JevStatus.INVALID_RESPONSE, error="Claude 판정의 기준 ID·모델 기록이 올바르지 않습니다.")
        verification["output_transport"] = output_transport
        if part["web_search_calls"] is None and not any(
            b.get("type") == "web_search_tool_result" or
            (b.get("type") == "server_tool_use" and b.get("name") == "web_search") for b in content
        ):
            # Anthropic omits server_tool_use usage on completed turns
            # with no search. Complete provider blocks establish zero;
            # errors, pending tools and missing counts after a search do not.
            part["web_search_calls"] = 0
        timings = session["timings"]
        if items_mode:
            # items 본문: {model, items, verification}. J5 서버 검증(server_check)이 verification 을 제자리에서 채운다.
            from .semantic_facts import server_check
            body = {"model": model, "items": copy.deepcopy(structured["items"]), "verification": verification}
            # 평가 모델의 web_fetch 가 실제로 받은 원문 — 서버가 접근 문제로 못 연 URL 을 '열린 것'으로 볼 근거 (메모리에서만)
            documents, verification["provider_documents"] = _provider_documents(all_blocks)
            source_started = time.monotonic()
            try:
                body = server_check(body, state, questions, verification, fetcher=self._source_fetcher, cancel_event=cancel_event,
                                    provider_documents=documents)
            finally:
                timings["source_verification"] += time.monotonic() - source_started
            return "done", result("OK", body=finalize_answers(body, state, questions), http_status=status_code)
        body = {"model": model, "verification": verification,
                "answers": {cid: semantic.jev_answer(structured[cid]) for cid in questions}}
        # Texts the provider's web_fetch actually returned: memory only, used when the server cannot fetch.
        documents, verification["provider_documents"] = _provider_documents(all_blocks)
        fact = body["answers"].get("J5", {}).get("fact_check")
        if isinstance(fact, dict) and fact.get("basis") in ("web", "mixed"):
            source_started = time.monotonic()
            try:
                body = verify_web_sources(body, fetcher=self._source_fetcher, cancel_event=cancel_event,
                                          provider_documents=documents)
            finally:
                timings["source_verification"] += time.monotonic() - source_started
        if context.get("review"):
            source_started = time.monotonic()
            try:
                body = verify_answer_citations(body, state, fetcher=self._source_fetcher, cancel_event=cancel_event,
                                               provider_documents=documents)
            finally:
                timings["source_verification"] += time.monotonic() - source_started
        return "done", result("OK", body=finalize_answers(body, state, questions), http_status=status_code)

    def system_one(self, state, questions, *, cancel_event=None, images=None):
        """실시간: 요청 조립 → POST(pause_turn 이어가기 포함) → 응답 처리."""
        deadline = time.monotonic() + self.config.timeout
        payload, context = None, {"questions": questions, "not_sent": [], "search_enabled": False, "items_mode": False}
        session = self._session(context)
        result = session["result"]

        def check_cancel():
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError()
        gate = None
        try:
            check_cancel()
            payload, context = self.build_request(state, questions, images)
            session["not_sent"], session["search_mode"] = context["not_sent"], context["search_mode"]
            if context.get("shared_key"):   # 긴 공유 자료 — 첫 요청이 캐시를 만든 뒤 보낸다
                event = _enter_shared_gate(context["shared_key"], cancel_event, deadline)
                gate = (context["shared_key"], event) if event is not None else None
                check_cancel()
            for continuation in range(self.config.max_continuations + 1):
                check_cancel()
                if time.monotonic() >= deadline:
                    return result(JevStatus.TIMEOUT, error="Claude 의미 평가 제한 시간을 초과했습니다.")
                part = self._begin_attempt(session)
                request_started = time.monotonic()
                response = None
                try:
                    response = asyncio.run(self._post(payload, cancel_event, deadline))
                finally:
                    session["timings"]["api"] += time.monotonic() - request_started
                    if gate is not None:   # 첫 응답이 끝났다 — 성공이면 캐시가 생겼으니 기다리던 요청들을 보낸다
                        _leave_shared_gate(*gate, ok=response is not None and 200 <= response.status_code < 300,
                                           started=request_started)
                        gate = None
                try:
                    raw = response.json()
                    validate_json_numbers(raw)
                    if not isinstance(raw, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    return result(JevStatus.INVALID_RESPONSE, http_status=response.status_code,
                                  error="Claude API 응답이 유효한 JSON 객체가 아닙니다.")
                outcome, call = self._consume(raw, response.status_code, session, state, context, payload,
                                              continuation=continuation, cancel_event=cancel_event, part=part)
                if outcome == "done":
                    return call
            return result(JevStatus.API_ERROR, error="Claude 검색 이어가기 한도를 초과해 미판정으로 남깁니다.")
        except CancelledError:
            return result(JevStatus.SKIPPED, error="평가 중단 요청으로 Claude API 평가를 종료했습니다.")
        except httpx.TimeoutException:
            return result(JevStatus.TIMEOUT, error="Claude 의미 평가 제한 시간을 초과했습니다. 자동 재시도하지 않았습니다.")
        except (httpx.HTTPError, httpx.InvalidURL):
            return result(JevStatus.API_ERROR, error="Claude 의미 평가 API 연결 실패. 자동 재시도하지 않았습니다.")
        except (ValueError, TypeError, UnicodeError):
            return result(JevStatus.INVALID_RESPONSE, error="Claude 의미 평가 요청·응답을 원문 그대로 처리할 수 없습니다.")
        finally:
            if gate is not None:   # 첫 요청이 보내기 전에 중단·시간 초과로 끝나도 기다리는 요청을 풀어 준다
                _leave_shared_gate(*gate, ok=False)

    def finish_batch_result(self, raw, context, *, payload=None, cancel_event=None, carry=None):
        """Message Batches 결과 메시지 하나 → JevCall. pause_turn(검색 이어가기가 필요) 이면 None — 디스패처가 실시간으로 다시 보낸다.

        ``raw`` 는 결과 줄의 ``result.message`` (Messages 응답과 같은 모양). 검색·원문 확인은 Batches 안에서도 서버 도구로 실행되므로
        결과 블록을 실시간과 같은 규칙으로 읽고, 반박 출처 원문 대조·J5 서버 검증은 여기서(디스패처 프로세스에서) 한다.
        ``carry`` (list) 를 주면 None 을 돌려줄 때 이 묶음 요청의 사용량 조각을 담는다 — 실시간 재전송 레코드에 더하라고.
        """
        session = self._session(context)
        result = session["result"]
        try:
            outcome, call = self._consume(raw, 200, session, context["state"], context, payload or {"tools": []},
                                          continuation=self.config.max_continuations if payload is None else 0, cancel_event=cancel_event)
        except CancelledError:
            return result(JevStatus.SKIPPED, error="평가 중단 요청으로 Claude API 평가를 종료했습니다.")
        except (ValueError, TypeError, UnicodeError):
            return result(JevStatus.INVALID_RESPONSE, error="Claude 의미 평가 요청·응답을 원문 그대로 처리할 수 없습니다.")
        if outcome != "done" and isinstance(carry, list):
            carry.extend(copy.deepcopy(session["parts"]))
        return call if outcome == "done" else None
