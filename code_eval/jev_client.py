"""Jev(TypeSafe System One) 호출 — 자격 증명은 환경변수 또는 ``.env`` 에서만 읽는다.

    TYPESAFE_API_KEY   인증값. 코드·로그·결과에 남기지 않는다
    TYPESAFE_API_BASE  비우면 https://api.typesafe.ai/v1
    JEV_MODEL          비우면 jev-latest

공식 REST 인터페이스: ``POST {base}/systemone`` · ``Authorization: Bearer <키>`` ·
본문 ``{"state", "model", "questions"}`` → 응답 ``{"model", "answers", "usage"}``.
키가 없으면 호출하지 않고 ``NOT_CONFIGURED`` 로 끝낸다. 판정을 지어내지 않는다.

    SEMANTIC_ENGINE    jev(기본) | claude_cli | codex_cli — CLI 로그인으로 의미 평가 가능
    SEMANTIC_PIPELINE  legacy(기본, 기준 7개를 한 번에 묻기) | items(세부 기준·점검 항목에 예/아니오+인용) — ``resolve_semantic_pipeline``
"""

from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Protocol

from .llm_client import env_value, redact

__all__ = ["JevStatus", "JevClientConfig", "JevCall", "JevClient", "HttpJevClient", "resolve_jev_client",
           "SEMANTIC_PIPELINES", "resolve_semantic_pipeline"]

DEFAULT_API_BASE = "https://api.typesafe.ai/v1"
DEFAULT_MODEL = "jev-latest"

#: 의미 평가 방식 — legacy(기준 7개 한 번에, 기본) | items(세부 기준 27·점검 항목 41 에 예/아니오+인용, 서버가 인용 검증·집계)
SEMANTIC_PIPELINES = ("legacy", "items")

#: 다시 시도할 응답 — 속도 제한(429)·과부하(529)·일시 서버 오류
_RETRYABLE = {429, 500, 502, 503, 504, 529}
_HTTP_NOTES = {401: "인증 실패 — TYPESAFE_API_KEY 확인", 403: "권한 없음", 422: "요청 형식 오류",
               429: "속도 제한", 529: "Jev 과부하"}


class JevStatus:
    """Jev 단계의 실행 상태. CODE 판정과 무관하다."""

    SUCCESS = "SUCCESS"
    API_ERROR = "API_ERROR"
    TIMEOUT = "TIMEOUT"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    SKIPPED = "SKIPPED"                 # CODE 입력 오류 또는 중단 요청 — 판정하지 않음


@dataclass(frozen=True)
class JevClientConfig:
    """호출 설정. 인증값 자체는 담지 않는다."""

    model: str = DEFAULT_MODEL
    api_base: str = DEFAULT_API_BASE
    connect_timeout: float = 10
    read_timeout: float = 30
    max_attempts: int = 3
    engine: str = "jev"

    def to_manifest(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "requested_model": self.model,
            "api_base": redact(self.api_base),
            "endpoint": "POST /systemone",
            "connect_timeout_s": self.connect_timeout,
            "read_timeout_s": self.read_timeout,
            "max_attempts": self.max_attempts,
            "credentials_source": "environment variable only (TYPESAFE_API_KEY)",
            "credentials_recorded": False,
        }


@dataclass
class JevCall:
    """호출 한 건의 결과. 실패도 결과로 남긴다. status 는 OK 또는 JevStatus 의 실패 값."""

    status: str
    body: Optional[Dict[str, Any]] = None
    attempts: int = 0
    latency_ms: Optional[int] = None
    http_status: Optional[int] = None
    error: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    # Runtime-only: jev_eval deliberately copies usage, never this field.
    accounting: Dict[str, Any] = field(default_factory=dict, repr=False)


def _amount(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and (isinstance(value, int) or math.isfinite(value)) and value >= 0)


def _token_usage(value):
    """Keep usage metadata compatible without exporting provider-reported prices."""
    if not isinstance(value, dict):
        return {}
    return {key: _token_usage(item) if isinstance(item, dict) else copy.deepcopy(item)
            for key, item in value.items()
            if not any(word in str(key).lower() for word in ("cost", "usd", "price", "billing"))}


def _sum_usage(target, usage):
    for key, value in usage.items():
        if isinstance(value, dict):
            nested = {}
            _sum_usage(nested, value)
            if nested:
                existing = target.setdefault(key, {})
                if isinstance(existing, dict):
                    _sum_usage(existing, nested)
        elif _amount(value):
            existing = target.get(key, 0)
            if _amount(existing):
                target[key] = existing + value


class _CallAccounting:
    """Collect observed attempts separately from the persisted evaluation result.

    Missing responses stay unknown; attempts are never a usage multiplier.
    Model-level totals and call-level totals describe the same work, not additions.
    """
    def __init__(self, requested_model=None):
        self.requested_model = requested_model
        self.entries = []

    def add(self, *, model=None, usage=None, web_search_calls=None, reported_cost_usd=None, model_usage=None):
        entry = {"model": model if isinstance(model, str) and model.strip() else None,
                 "usage": _token_usage(usage), "web_search_calls": web_search_calls}
        if _amount(reported_cost_usd):
            entry["reported_cost_usd"] = reported_cost_usd
        if isinstance(model_usage, dict):
            entry["model_usage"] = copy.deepcopy(model_usage)
        self.entries.append(entry)

    def snapshot(self, attempts):
        data = {"model": self.requested_model, "usage": {}, "web_search_calls": None,
                "unreported_attempts": max(0, attempts - len(self.entries))}
        models, per_model = set(), {}
        explicit_models = False
        complete_cost = bool(self.entries) and len(self.entries) == attempts
        missing_model_cost = set()
        web_counts = []
        for entry in self.entries:
            usage = {}
            _sum_usage(usage, entry["usage"])
            _sum_usage(data["usage"], usage)
            cost = entry.get("reported_cost_usd")
            if _amount(cost):
                data["reported_cost_usd"] = data.get("reported_cost_usd", 0) + cost
            else:
                complete_cost = False
            parts = entry.get("model_usage")
            explicit_models = explicit_models or bool(parts)
            if not parts:
                parts = {entry["model"]: {"usage": usage, **({"reported_cost_usd": cost} if _amount(cost) else {})}}
            observed = bool(usage) or _amount(cost)
            for model, part in parts.items():
                if not isinstance(part, dict):
                    continue
                model = model if isinstance(model, str) and model.strip() else None
                models.add(model)
                subtotal = per_model.setdefault(model, {"usage": {}})
                part_usage = {}
                _sum_usage(part_usage, _token_usage(part.get("usage")))
                _sum_usage(subtotal["usage"], part_usage)
                part_cost = part.get("reported_cost_usd")
                if _amount(part_cost):
                    subtotal["reported_cost_usd"] = subtotal.get("reported_cost_usd", 0) + part_cost
                else:
                    missing_model_cost.add(model)
                observed = observed or bool(part_usage) or _amount(part_cost)
            if not observed:
                data["unreported_attempts"] += 1
            calls = entry.get("web_search_calls")
            if isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0:
                web_counts.append(calls)
        if self.entries:
            data["model"] = next(iter(models)) if len(models) == 1 else None
        if len(web_counts) == attempts and self.entries:
            data["web_search_calls"] = sum(web_counts)
        if not complete_cost:
            data.pop("reported_cost_usd", None)
        for model in missing_model_cost:
            per_model[model].pop("reported_cost_usd", None)
        if explicit_models or len(models) > 1:
            data["model_usage"] = per_model
        return data


class JevClient(Protocol):
    """Jev 호출 인터페이스. 시험용 대역도 같은 모양을 쓴다."""

    config: JevClientConfig

    def system_one(self, state: Any, questions: Dict[str, Any]) -> JevCall:
        ...


class HttpJevClient:
    """공식 REST 엔드포인트 호출. 자격 증명이 있을 때만 만들어진다."""

    def __init__(self, config: JevClientConfig, api_key: str, transport: Any = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not api_key:
            raise ValueError("TYPESAFE_API_KEY 가 없습니다")
        self.config = config
        self._api_key = api_key
        self._transport = transport
        self._sleep = sleep
        self._session: Any = None

    def _client(self) -> Any:
        if self._session is None:
            import httpx  # 지연 import — 자격 증명이 없으면 쓰지 않는다

            self._session = httpx.Client(
                base_url=self.config.api_base,
                timeout=httpx.Timeout(self.config.read_timeout, connect=self.config.connect_timeout),
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                transport=self._transport,
            )
        return self._session

    def _wait(self, attempt: int, retry_after: Optional[str] = None) -> None:
        if attempt >= self.config.max_attempts:
            return
        try:
            delay = float(retry_after) if retry_after else 0.5 * 2 ** (attempt - 1)
        except ValueError:
            delay = 0.5 * 2 ** (attempt - 1)
        self._sleep(min(max(delay, 0.0), 10.0))

    def system_one(self, state: Any, questions: Dict[str, Any]) -> JevCall:
        import httpx

        payload = {"state": state, "model": self.config.model, "questions": questions}
        started = time.monotonic()
        status, error, http_status = JevStatus.API_ERROR, None, None
        attempt = 0
        accounting = _CallAccounting(self.config.model)
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = self._client().post("/systemone", json=payload)
            except httpx.TimeoutException as exc:
                status, error = JevStatus.TIMEOUT, f"{type(exc).__name__}: {exc}"
                self._wait(attempt)
                continue
            except httpx.HTTPError as exc:  # 연결 실패 등 네트워크 오류도 결과로 남긴다
                status, error = JevStatus.API_ERROR, f"{type(exc).__name__}: {exc}"
                self._wait(attempt)
                continue
            http_status = response.status_code
            try:
                body = response.json()
                decoded = True
            except ValueError:
                body = None
                decoded = False
            if isinstance(body, dict):
                raw_usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
                reported_cost = next((v for v in (body.get("total_cost_usd"), body.get("cost_usd"),
                                                   raw_usage.get("cost_usd")) if _amount(v)), None)
                accounting.add(model=body.get("model") or self.config.model, usage=raw_usage,
                               web_search_calls=0, reported_cost_usd=reported_cost)
            note = _HTTP_NOTES.get(http_status)
            if http_status in _RETRYABLE:
                status, error = JevStatus.API_ERROR, f"HTTP {http_status}" + (f" ({note})" if note else "")
                self._wait(attempt, response.headers.get("retry-after"))
                continue
            elapsed = int((time.monotonic() - started) * 1000)
            if http_status >= 400:
                return JevCall(JevStatus.API_ERROR, attempts=attempt, latency_ms=elapsed, http_status=http_status,
                               error=f"HTTP {http_status}" + (f" ({note})" if note else ""),
                               accounting=accounting.snapshot(attempt))
            if not decoded:
                return JevCall(JevStatus.INVALID_RESPONSE, attempts=attempt, latency_ms=elapsed,
                               http_status=http_status, error="응답 본문이 JSON 이 아님", accounting=accounting.snapshot(attempt))
            if not isinstance(body, dict):
                return JevCall(JevStatus.INVALID_RESPONSE, attempts=attempt, latency_ms=elapsed,
                               http_status=http_status, error="응답 최상위가 객체가 아님", accounting=accounting.snapshot(attempt))
            return JevCall("OK", body=body, attempts=attempt, latency_ms=elapsed, http_status=http_status,
                           usage=_token_usage(body.get("usage")), accounting=accounting.snapshot(attempt))
        return JevCall(status, attempts=attempt, latency_ms=int((time.monotonic() - started) * 1000),
                       http_status=http_status, error=redact(error), accounting=accounting.snapshot(attempt))


def resolve_semantic_pipeline(environment: Optional[Dict[str, str]] = None, env_path: Optional[Path] = None,
                              pipeline: Optional[str] = None) -> str:
    """의미 평가 방식을 정한다 — ``pipeline`` 인자 > SEMANTIC_PIPELINE(환경변수·.env) > 기본 legacy.

    legacy·items 외의 값은 ValueError (조용히 기본값으로 바꾸지 않는다). 엔진 설정과 무관하며 client config 에 두지 않는다.
    """
    value = (pipeline or env_value("SEMANTIC_PIPELINE", environment, env_path) or "legacy").strip().lower()
    if value not in SEMANTIC_PIPELINES:
        raise ValueError(f"알 수 없는 의미 평가 방식: {value!r} (legacy 또는 items)")
    return value


def resolve_jev_client(
    environment: Optional[Dict[str, str]] = None,
    env_path: Optional[Path] = None,
    engine: Optional[str] = None,
) -> tuple[Optional[Any], Any, Optional[str]]:
    """환경변수(또는 ``.env``)를 보고 client 를 만든다. 쓸 수 없으면 (None, 설정, 이유).

    ``engine`` (또는 SEMANTIC_ENGINE) 에 따라 Jev API 또는 Claude/Codex CLI client 를 만든다.
    """
    def read(name: str) -> Optional[str]:
        return env_value(name, environment, env_path)

    engine = (engine or read("SEMANTIC_ENGINE") or "jev").strip().lower().replace("-", "_")
    if engine == "auto":
        # Dedicated evaluation credentials only; collection keys never select a judge.
        engine = "openai_api" if read("SEMANTIC_API_KEY") else "codex_cli"
    if engine == "anthropic_api":
        from .anthropic_semantic_client import AnthropicSemanticClient, AnthropicSemanticConfig, prompt_cache_setting
        api_config = AnthropicSemanticConfig(
            model=read("SEMANTIC_ANTHROPIC_MODEL") or "claude-sonnet-5-5",
            reasoning_effort=read("SEMANTIC_ANTHROPIC_EFFORT") or "medium",
            api_base=read("SEMANTIC_ANTHROPIC_BASE") or "https://api.anthropic.com/v1",
            timeout=float(read("SEMANTIC_ANTHROPIC_TIMEOUT") or 180),
            max_output_tokens=int(read("SEMANTIC_ANTHROPIC_MAX_OUTPUT_TOKENS") or 12000),
            web_search=read("SEMANTIC_WEB_SEARCH") or "live",
            max_tool_calls=int(read("SEMANTIC_ANTHROPIC_MAX_SEARCHES") or 4),
            max_fetches=int(read("SEMANTIC_ANTHROPIC_MAX_FETCHES") or 8),
            max_continuations=int(read("SEMANTIC_ANTHROPIC_MAX_CONTINUATIONS") or 2),
            prompt_cache=prompt_cache_setting(read("SEMANTIC_ANTHROPIC_PROMPT_CACHE")),
        )
        # Reuse the registered Claude credential only for this explicitly
        # selected evaluator. The historical auto-selection remains unchanged.
        api_key = read("ANTHROPIC_API_KEY")
        if not api_key:
            return None, api_config, "Claude API 평가에 사용할 ANTHROPIC_API_KEY가 등록되지 않았습니다."
        return AnthropicSemanticClient(api_config, api_key), api_config, None
    if engine == "openai_api":
        from .semantic_api_client import SemanticApiClient, SemanticApiConfig, optional_tool_call_limit
        api_config = SemanticApiConfig(
            model=read("SEMANTIC_API_MODEL") or "gpt-6.1-sol",
            api_base=read("SEMANTIC_API_BASE") or "https://api.openai.com/v1",
            timeout=float(read("SEMANTIC_API_TIMEOUT") or 180),
            max_output_tokens=int(read("SEMANTIC_API_MAX_OUTPUT_TOKENS") or 12000),
            reasoning_effort=read("SEMANTIC_API_REASONING") or "high",
            web_search=read("SEMANTIC_WEB_SEARCH") or "live",
            max_tool_calls=optional_tool_call_limit(read("SEMANTIC_API_MAX_TOOL_CALLS")),
            evidence_recovery_rounds=int(read("SEMANTIC_API_EVIDENCE_RECOVERY_ROUNDS") or 1),
            evidence_recovery_timeout=float(read("SEMANTIC_API_EVIDENCE_RECOVERY_TIMEOUT") or 120),
            evidence_recovery_max_tool_calls=optional_tool_call_limit(read("SEMANTIC_API_EVIDENCE_RECOVERY_MAX_TOOL_CALLS")),
        )
        api_key = read("SEMANTIC_API_KEY")
        if not api_key:
            return None, api_config, "의미평가용 SEMANTIC_API_KEY가 없습니다. 답변 수집용 키는 대신 사용하지 않습니다."
        return SemanticApiClient(api_config, api_key), api_config, None
    if engine == "gpt_cli":
        engine = "codex_cli"
    if engine == "codex_cli":
        from .codex_cli_client import CodexCliClient, CodexCliConfig, find_codex_executable, check_codex_login

        executable = find_codex_executable(read("CODEX_CLI_PATH"))
        cli_config = CodexCliConfig(executable=executable or "codex",
                                    model=read("CODEX_CLI_MODEL") or "gpt-6.1-sol",
                                    reasoning_effort=read("CODEX_CLI_REASONING") or "high",
                                    web_search=read("SEMANTIC_WEB_SEARCH") or "live",
                                    timeout=float(read("CODEX_CLI_TIMEOUT") or 600))
        if not executable:
            return None, cli_config, "GPT CLI(Codex) 실행 파일을 찾지 못해 의미 평가를 실행하지 않았습니다. codex 설치와 CODEX_CLI_PATH를 확인하세요. CODE 평가 결과는 그대로입니다."
        access_reason = check_codex_login(executable)
        if access_reason:
            return None, cli_config, access_reason
        return CodexCliClient(cli_config), cli_config, None
    if engine == "claude_cli":
        from .claude_cli_client import ClaudeCliClient, ClaudeCliConfig, find_claude_executable

        executable = find_claude_executable(read("CLAUDE_CLI_PATH"))
        cli_config = ClaudeCliConfig(executable=executable or "claude", model=read("CLAUDE_CLI_MODEL"))
        if not executable:
            return None, cli_config, "Claude CLI(claude) 실행 파일을 찾지 못해 의미 평가를 실행하지 않았습니다. CODE 평가 결과는 그대로입니다."
        return ClaudeCliClient(cli_config), cli_config, None
    if engine != "jev":
        raise ValueError(f"알 수 없는 의미 평가 엔진: {engine!r} (auto, anthropic_api, openai_api, jev, claude_cli 또는 codex_cli)")

    config = JevClientConfig(model=read("JEV_MODEL") or DEFAULT_MODEL,
                             api_base=read("TYPESAFE_API_BASE") or DEFAULT_API_BASE)
    api_key = read("TYPESAFE_API_KEY")
    if not api_key:
        return None, config, ("TYPESAFE_API_KEY 를 찾지 못해 Jev 의미 평가를 실행하지 않았습니다 "
                              "(환경변수와 .env 둘 다 확인). CODE 평가 결과는 그대로입니다.")
    return HttpJevClient(config, api_key), config, None
