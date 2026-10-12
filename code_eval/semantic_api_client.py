"""Independent OpenAI Responses calls for the semantic evaluator.

The key is supplied explicitly by the semantic resolver; this module never reads
collection-provider credentials. It never retries failed HTTP requests or shortens
original inputs. A completed J5 abstention may receive one focused evidence pass.
Search provenance comes from provider tool events and citations, never answer text.
Source bodies are independently retrieved to verify quotations. The model still
judges publisher authority, temporal relevance, and whether the evidence supports
the claim; failed retrieval never becomes a confirmed factual judgment.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from concurrent.futures import CancelledError
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import semantic_cli_common as semantic
from .io import validate_json_numbers
from .jev_client import JevCall, JevStatus, _sum_usage, _token_usage
from .llm_client import redact
from .semantic_evidence import (CONTEXT_CHECK_POLICY, FACT_CHECK_POLICY, SEARCH_POLICY, finalize_answers, has_reference,
                                search_mode, source_url)
from .semantic_sources import verify_answer_citations, verify_web_sources
from .semantic_recovery import evidence_recovery_prompt, merge_evidence_recovery

DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_API_BASE = "https://api.openai.com/v1"


def optional_tool_call_limit(value: str | None) -> int | None:
    """An omitted/blank/zero environment setting removes the application cap."""
    parsed = int(value) if value and value.strip() else 0
    if parsed < 0:
        raise ValueError("검색 호출 한도는 0(앱 한도 없음) 또는 양의 정수여야 합니다.")
    return parsed or None


@dataclass(frozen=True)
class SemanticApiConfig:
    model: str = DEFAULT_MODEL
    api_base: str = DEFAULT_API_BASE
    timeout: float = 180
    max_output_tokens: int = 12000
    reasoning_effort: str = "high"
    web_search: str = "live"
    max_tool_calls: int | None = None
    evidence_recovery_rounds: int = 1
    evidence_recovery_timeout: float = 120
    evidence_recovery_max_tool_calls: int | None = None
    engine: str = field(default="openai_api", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("의미 평가 API 모델명이 필요합니다.")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("의미 평가 API 제한 시간은 양의 유한한 초여야 합니다.")
        for name in ("max_output_tokens", "max_tool_calls", "evidence_recovery_max_tool_calls"):
            value = getattr(self, name)
            if name != "max_output_tokens" and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name}은 1 이상의 정수여야 합니다.")
        if type(self.evidence_recovery_rounds) is not int or self.evidence_recovery_rounds not in (0, 1):
            raise ValueError("J5 추가 검증 횟수는 0 또는 1이어야 합니다.")
        if (isinstance(self.evidence_recovery_timeout, bool)
                or not isinstance(self.evidence_recovery_timeout, (int, float))
                or not math.isfinite(self.evidence_recovery_timeout) or self.evidence_recovery_timeout <= 0):
            raise ValueError("J5 추가 검증 제한 시간은 양의 유한한 초여야 합니다.")
        if not isinstance(self.reasoning_effort, str) or not self.reasoning_effort.strip():
            raise ValueError("의미 평가 API 추론 수준이 필요합니다.")
        if self.web_search not in ("live", "disabled"):
            raise ValueError("의미 평가 API 검색 설정은 live 또는 disabled여야 합니다.")
        try:
            if not isinstance(self.api_base, str):
                raise ValueError("API base must be a string")
            parsed = urlsplit(self.api_base)
            parsed.port  # Reject malformed ports before constructing a request.
            valid = (parsed.scheme in ("https", "http") and parsed.hostname and
                     not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("의미 평가 API 주소는 인증값·쿼리·프래그먼트 없는 HTTP(S) 주소여야 합니다.")

    def to_manifest(self) -> Dict[str, Any]:
        parsed = urlsplit(self.api_base)
        safe_base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        return {
            "engine": self.engine, "requested_model": self.model,
            "api_base": redact(safe_base), "endpoint": "POST /responses",
            "reasoning_effort": self.reasoning_effort, "web_search": self.web_search,
            "search_policy": SEARCH_POLICY, "fact_check_policy": FACT_CHECK_POLICY,
            "fact_check_scope": "all_answers",
            "context_check_policy": CONTEXT_CHECK_POLICY,
            "max_output_tokens": self.max_output_tokens, "max_tool_calls": self.max_tool_calls,
            "prompt_cache_policy": ("explicit_static_prefix_v1" if self.model == DEFAULT_MODEL
                                    else "provider_default"),
            "timeout_s": self.timeout, "max_attempts": 1,
            "evidence_recovery_policy": "j5_targeted_evidence_v1",
            "evidence_recovery_rounds": self.evidence_recovery_rounds,
            "evidence_recovery_timeout_s": self.evidence_recovery_timeout,
            "evidence_recovery_max_tool_calls": self.evidence_recovery_max_tool_calls,
            "max_requests": 1 + self.evidence_recovery_rounds,
            "store": False, "truncation": "disabled",
            "credentials_source": "SEMANTIC_API_KEY", "credentials_recorded": False,
            "confidence_note": "모델이 스스로 적은 확신도 — 보정된 확률 아님",
        }


def _verification(output: Any) -> Dict[str, Any]:
    """Record provider-observed URLs, not independent quote or truth verification."""
    items = output if isinstance(output, list) else []
    completed = [item for item in items if isinstance(item, dict) and
                 item.get("type") == "web_search_call" and item.get("status") == "completed"]
    urls = set()

    def add(value):
        url = source_url(value)
        if url:
            urls.add(url)

    for item in completed:
        action = item.get("action")
        if not isinstance(action, dict):
            continue
        add(action.get("url"))
        sources = action.get("sources")
        for source in sources if isinstance(sources, list) else []:
            if isinstance(source, dict):
                add(source.get("url"))
    # A citation without any completed web tool event cannot prove a search ran.
    if completed:
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "output_text":
                    continue
                annotations = block.get("annotations")
                for annotation in annotations if isinstance(annotations, list) else []:
                    if isinstance(annotation, dict) and annotation.get("type") == "url_citation":
                        add(annotation.get("url"))
    return {"search_performed": bool(completed), "source_urls": sorted(urls),
            "source_validation": "provider", "checked_at": datetime.now(timezone.utc).isoformat()}


def _search_calls(output):
    """Only search actions are billable searches; opening/finding pages is separate."""
    if not isinstance(output, list):
        return None
    calls = 0
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        action = item.get("action")
        kind = action.get("type") if isinstance(action, dict) else None
        if kind not in ("search", "open_page", "find_in_page") or item.get("status") != "completed":
            return None
        calls += kind == "search"
    return calls


class SemanticApiClient:
    supports_cancellation = True

    def __init__(self, config: SemanticApiConfig, api_key: str, transport: Any = None, *, source_fetcher=None) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("SEMANTIC_API_KEY가 없습니다.")
        self.config = config
        self._api_key = api_key
        self._transport = transport
        self._source_fetcher = source_fetcher

    async def _post(self, payload, cancel_event, deadline):
        # Cancelling this task closes this request's connection, without waiting
        # for the full provider timeout or touching another evaluation's client.
        async with httpx.AsyncClient(base_url=self.config.api_base.rstrip("/") + "/",
                                     timeout=max(.01, deadline - time.monotonic()),
                                     follow_redirects=False, transport=self._transport) as client:
            task = asyncio.create_task(client.post("responses", headers={
                "Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}, json=payload))
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

    def system_one(self, state: Any, questions: Dict[str, Any], *, cancel_event=None) -> JevCall:
        started = time.monotonic()
        evaluation_time = datetime.now(timezone.utc).isoformat()
        initial = self._system_one_once(state, questions, cancel_event=cancel_event,
                                        evaluation_time=evaluation_time)
        if (initial.status != "OK" or not self.config.evidence_recovery_rounds
                or self.config.web_search != "live" or "J5" not in questions or has_reference(state)):
            return initial
        prompt = evidence_recovery_prompt(initial.body, state)
        if not prompt:
            return initial
        # A separate budget is intentional: ordinary evaluation may consume most
        # of its timeout. Neither request is retried; there is one bounded follow-up.
        recovery_config = replace(self.config, timeout=self.config.evidence_recovery_timeout,
                                  max_tool_calls=self.config.evidence_recovery_max_tool_calls,
                                  evidence_recovery_rounds=0)
        client = SemanticApiClient(recovery_config, self._api_key, self._transport,
                                   source_fetcher=self._source_fetcher)
        supplemental = client._system_one_once(state, {"J5": questions["J5"]},
                                               cancel_event=cancel_event, evidence_prompt=prompt,
                                               evaluation_time=evaluation_time,
                                               prior_verification=initial.body.get("verification"))
        cancelled = (cancel_event is not None and cancel_event.is_set()) or supplemental.status == JevStatus.SKIPPED
        body = (merge_evidence_recovery(initial.body, supplemental.body, state, questions)
                if supplemental.status == "OK" and not cancelled else copy.deepcopy(initial.body))
        first_j5 = initial.body.get("answers", {}).get("J5", {})
        final_j5 = body.get("answers", {}).get("J5", {})
        recovery_j5 = (supplemental.body or {}).get("answers", {}).get("J5")
        verification = body.setdefault("verification", {})
        verification["evidence_recovery"] = {
            "policy": "j5_targeted_evidence_v1", "rounds": supplemental.attempts,
            "status": "CANCELLED" if cancelled else "FAILED" if supplemental.status != "OK" else
                      "RESOLVED" if final_j5.get("choice") in ("PASS", "FAIL") else "UNRESOLVED",
            "initial_choice": first_j5.get("choice"), "final_choice": None if cancelled else final_j5.get("choice"),
            "initial_j5": copy.deepcopy(first_j5), "supplemental_j5": copy.deepcopy(recovery_j5),
            "request_status": supplemental.status, "http_status": supplemental.http_status,
            "error": supplemental.error,
            "verification": copy.deepcopy((supplemental.body or {}).get("verification", {})),
        }
        usage, parts = {}, []
        api_ms = source_ms = 0.0
        for stage, call in (("evaluation", initial), ("evidence_recovery", supplemental)):
            _sum_usage(usage, call.usage)
            for part in call.accounting.get("cost_parts", []):
                parts.append({**copy.deepcopy(part), "stage": stage})
            timing = (call.body or {}).get("verification", {}).get("timings_ms", {})
            api_ms += timing.get("api", 0)
            source_ms += timing.get("source_verification", 0)
        latency_ms = int((time.monotonic() - started) * 1000)
        verification["timings_ms"] = {"api": round(api_ms, 1), "source_verification": round(source_ms, 1),
                                       "total": latency_ms}
        verification["usage_requests"] = copy.deepcopy(parts)
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            verification["evidence_recovery"].update(status="CANCELLED", final_choice=None)
        return JevCall(JevStatus.SKIPPED if cancelled else "OK",
                       body={"verification": verification} if cancelled else body,
                       attempts=initial.attempts + supplemental.attempts, latency_ms=latency_ms,
                       http_status=initial.http_status,
                       error="평가 중단 요청으로 J5 추가 검증을 종료했습니다." if cancelled else None,
                       usage=usage, accounting={"model": initial.accounting.get("model"),
                                               "cost_parts": parts, "usage": usage,
                                               "unreported_attempts": 0})

    def _system_one_once(self, state: Any, questions: Dict[str, Any], *, cancel_event=None,
                         evidence_prompt=None, evaluation_time=None, prior_verification=None) -> JevCall:
        started = time.monotonic()
        verification = _verification([])
        parts, usage, attempts = [], {}, 0
        timings = {"api": 0.0, "source_verification": 0.0}
        search_enabled = self.config.web_search == "live" and "J5" in questions and not has_reference(state)

        def cancelled():
            return cancel_event is not None and cancel_event.is_set()

        def observe(raw):
            nonlocal verification, usage
            verification = _verification(raw.get("output"))
            # These URLs come from the first request's actual provider events,
            # never the model's self-reported source list. Reused quotes still
            # pass the independent content verifier in this follow-up.
            if isinstance(prior_verification, dict) and prior_verification.get("search_performed") is True:
                inherited = sorted({url for value in prior_verification.get("source_urls", [])
                                    if (url := source_url(value))})
                verification["inherited_source_urls"] = inherited
                verification["source_urls"] = sorted(set(verification["source_urls"]) | set(inherited))
                verification["search_performed"] = True
            usage = _token_usage(raw.get("usage"))
            resolved = raw.get("model")
            parts[-1].update(model=resolved if isinstance(resolved, str) and resolved.strip() else self.config.model,
                             usage=usage,
                             service_tier=raw.get("service_tier") if isinstance(raw.get("service_tier"), str) else None)
            output = raw.get("output")
            searches = [item for item in output if isinstance(item, dict) and item.get("type") == "web_search_call"] if isinstance(output, list) else []
            if searches:
                # Completed tool use can be billed even when the final judgment
                # is invalid/refused/incomplete. Keep known events before parsing it.
                parts[-1]["web_search_calls"] = _search_calls(output)
                parts[-1]["observed_web_search_calls"] = sum(
                    item.get("status") == "completed" and isinstance(item.get("action"), dict)
                    and item["action"].get("type") == "search" for item in searches)
            return usage

        def result(status: str, **kwargs) -> JevCall:
            body = kwargs.pop("body", None) or {}
            body.setdefault("verification", verification)
            body["verification"]["search_mode"] = search_mode(questions, state, self.config.web_search)
            if cancelled():
                status = JevStatus.SKIPPED
                kwargs["error"] = "평가 중단 요청으로 OpenAI API 평가를 종료했습니다."
                body = {"verification": body["verification"]}
            body["verification"]["timings_ms"] = {
                **{name: round(seconds * 1000, 1) for name, seconds in timings.items()},
                "total": round((time.monotonic() - started) * 1000, 1)}
            # Only nonmonetary request metadata is persisted. Prices and totals
            # remain in the terminal ledger, including after errors or cancellation.
            body["verification"]["usage_requests"] = copy.deepcopy(parts)
            kwargs.setdefault("usage", usage)
            return JevCall(status, body=body, attempts=attempts,
                           accounting={"model": parts[-1]["model"] if parts else self.config.model,
                                       "cost_parts": parts, "unreported_attempts": 0, "usage": usage},
                           latency_ms=int((time.monotonic() - started) * 1000), **kwargs)

        def stopped():
            return result(JevStatus.SKIPPED, error="평가 중단 요청으로 OpenAI API 평가를 종료했습니다.")

        if cancelled():
            return stopped()
        try:
            # Only this verified model supports the explicit-cache request shape.
            # Preserve the user-message role and send every original data byte;
            # only the reusable evaluation instructions receive a breakpoint.
            if self.config.model == DEFAULT_MODEL:
                static, dynamic = semantic.build_prompt_parts(state, questions, search_enabled=search_enabled,
                                                              evaluation_time=evaluation_time)
                if evidence_prompt:
                    dynamic += "\n\n" + evidence_prompt
                prompt_input = [{"role": "user", "content": [
                    {"type": "input_text", "text": static,
                     "prompt_cache_breakpoint": {"mode": "explicit"}},
                    {"type": "input_text", "text": "\n" + dynamic},
                ]}]
            else:
                prompt_input = semantic.build_prompt(state, questions, search_enabled=search_enabled,
                                                      evaluation_time=evaluation_time)
                if evidence_prompt:
                    prompt_input += "\n\n" + evidence_prompt
            payload = {
                "model": self.config.model,
                "instructions": semantic.system_prompt(search_enabled=search_enabled),
                "input": prompt_input,
                "reasoning": {"effort": self.config.reasoning_effort},
                "max_output_tokens": self.config.max_output_tokens,
                "text": {"format": {"type": "json_schema", "name": "aiona_semantic_evaluation",
                                    "strict": True, "schema": semantic.output_schema(
                                        questions, citations=semantic.citation_review(state, questions, search_enabled))}},
                "store": False, "truncation": "disabled",
            }
            if self.config.model == DEFAULT_MODEL:
                payload["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}
            if search_enabled:
                payload.update(tools=[{"type": "web_search"}], tool_choice="auto",
                               include=["web_search_call.action.sources"])
                # Omit the optional API field to remove our application cap.
                # Provider limits and the request timeout/token budget still apply.
                if self.config.max_tool_calls is not None:
                    payload["max_tool_calls"] = self.config.max_tool_calls
            validate_json_numbers(payload)
            if cancelled():
                return stopped()
            parts.append({"model": self.config.model, "usage": {}, "web_search_calls": None,
                          "service_tier": None, "inference_geo": None})
            attempts = 1
            request_started = time.monotonic()
            try:
                response = asyncio.run(self._post(payload, cancel_event, started + self.config.timeout))
            finally:
                timings["api"] += time.monotonic() - request_started
        except CancelledError:
            return stopped()
        except httpx.TimeoutException:
            return result(JevStatus.TIMEOUT, error="의미 평가 API 제한 시간을 초과했습니다. 자동 재시도하지 않았습니다.")
        except (httpx.HTTPError, httpx.InvalidURL):
            return result(JevStatus.API_ERROR, error="의미 평가 API 연결에 실패했습니다. 자동 재시도하지 않았습니다.")
        except (ValueError, TypeError, UnicodeError):
            return result(JevStatus.API_ERROR, error="의미 평가 API 요청을 원문 그대로 구성할 수 없습니다.")

        if not 200 <= response.status_code < 300:
            # Never persist the error body or exception text: they may echo credentials/input.
            error_usage = {}
            try:
                failed = response.json()
                validate_json_numbers(failed)
                if isinstance(failed, dict):
                    error_usage = observe(failed)
            except (ValueError, TypeError):
                pass
            if cancelled():
                return stopped()
            return result(JevStatus.API_ERROR, http_status=response.status_code,
                          usage=error_usage,
                          error=f"의미 평가 API HTTP {response.status_code}. 자동 재시도하지 않았습니다.")
        try:
            raw = response.json()
            validate_json_numbers(raw)
            if not isinstance(raw, dict):
                raise ValueError("response object required")
        except (ValueError, TypeError):
            if cancelled():
                return stopped()
            return result(JevStatus.INVALID_RESPONSE, http_status=response.status_code,
                          error="의미 평가 API 응답이 유효한 JSON 객체가 아닙니다.")
        usage = observe(raw)
        common = {"http_status": response.status_code, "usage": usage}
        if raw.get("status") != "completed" or raw.get("error"):
            return result(JevStatus.API_ERROR, error="의미 평가 API 응답이 완료되지 않았습니다. 원문 축약·재시도 없이 미판정으로 남깁니다.", **common)
        output = raw.get("output")
        if not isinstance(output, list):
            return result(JevStatus.INVALID_RESPONSE, error="의미 평가 API 출력 항목이 없습니다.", **common)
        texts = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            if item.get("status", "completed") != "completed":
                return result(JevStatus.API_ERROR, error="의미 평가 API의 최종 답변이 완료되지 않았습니다.", **common)
            content = item.get("content")
            if not isinstance(content, list):
                return result(JevStatus.INVALID_RESPONSE, error="의미 평가 API 메시지 형식이 올바르지 않습니다.", **common)
            for block in content:
                if not isinstance(block, dict):
                    return result(JevStatus.INVALID_RESPONSE, error="의미 평가 API 출력 형식이 올바르지 않습니다.", **common)
                if block.get("type") == "refusal":
                    return result(JevStatus.API_ERROR, error="의미 평가 API가 평가 요청을 거절하여 미판정으로 남깁니다.", **common)
                if block.get("type") == "output_text":
                    if not isinstance(block.get("text"), str):
                        return result(JevStatus.INVALID_RESPONSE, error="의미 평가 API 텍스트가 문자열이 아닙니다.", **common)
                    texts.append(block["text"])
        try:
            structured = json.loads("".join(texts))
            validate_json_numbers(structured)
            if not isinstance(structured, dict) or set(structured) != set(questions):
                raise ValueError("criterion mismatch")
            if not all(isinstance(entry, dict) for entry in structured.values()):
                raise ValueError("criterion objects required")
            model = raw.get("model")
            if not isinstance(model, str) or not model.strip():
                raise ValueError("resolved model missing")
        except (ValueError, TypeError):
            return result(JevStatus.INVALID_RESPONSE, error="의미 평가 API의 JSON 판정·기준 ID·모델 기록이 올바르지 않습니다.", **common)
        body = {"model": model, "answers": {cid: semantic.jev_answer(structured[cid]) for cid in questions},
                "verification": verification}
        verification["output_transport"] = "responses_json_schema"
        # An absent search event means zero only after a completed response with
        # a usable final judgment. Incomplete/failed responses retain unknown usage.
        parts[-1]["web_search_calls"] = _search_calls(output)
        if cancelled():
            return stopped()
        fact = body["answers"].get("J5", {}).get("fact_check")
        try:
            if isinstance(fact, dict) and fact.get("basis") in ("web", "mixed"):
                source_started = time.monotonic()
                try:
                    body = verify_web_sources(body, fetcher=self._source_fetcher, cancel_event=cancel_event)
                finally:
                    timings["source_verification"] += time.monotonic() - source_started
            if semantic.citation_review(state, questions, search_enabled):
                body = verify_answer_citations(body, state, fetcher=self._source_fetcher, cancel_event=cancel_event)
            body = finalize_answers(body, state, questions)
        except CancelledError:
            return stopped()
        return stopped() if cancelled() else result("OK", body=body, **common)
