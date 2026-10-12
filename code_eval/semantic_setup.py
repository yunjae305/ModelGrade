"""Read-only semantic evaluator configuration, without resolving or calling a judge.

This is a configuration check, not a connection test. It never runs CLI login,
creates an HTTP client, reads CLI credentials, or changes saved evaluations.
Only environment/.env settings and executable existence are inspected.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from .llm_client import env_value, redact

_LABELS = {"openai_api": "OpenAI API", "anthropic_api": "Claude API", "codex_cli": "GPT CLI (Codex)",
           "claude_cli": "Claude CLI", "jev": "Jev", "injected": "주입된 평가자"}
_CONNECTION_NOTE = "설정만 확인했습니다. 실제 연결·로그인·모델 접근은 평가 실행 시 확인합니다."


def _model_label(value: Optional[str]) -> Optional[str]:
    # Model IDs are useful to the UI; arbitrary configured paths/text are not.
    if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value):
        return None
    cleaned = redact(value)
    return cleaned if cleaned == value else None


def _effort_label(value):
    return value if value in ("none", "minimal", "low", "medium", "high", "xhigh", "max") else None


def semantic_setup(environment: Optional[Dict[str, str]] = None,
                   env_path: Optional[Path] = None, *, injected: bool = False) -> Dict[str, Any]:
    """Return safe display fields; ``can_evaluate`` means configuration is present.

    An injected resolver is never executed here. Test/custom evaluators are
    marked explicitly so their readiness is not inferred from host credentials.
    """
    result = {
        "engine": "injected" if injected else "unknown",
        "engine_label": _LABELS["injected"] if injected else "의미 평가자",
        "model": None, "reasoning_effort": None, "configuration_status": "configured" if injected else "invalid",
        "can_evaluate": injected, "connection_status": "not_checked",
        "connection_message": _CONNECTION_NOTE,
        "search_status": "not_checked", "search_message": "검색 지원 여부를 확인하지 않았습니다.",
        "reason": None,
    }
    if injected:
        result["connection_message"] = "주입된 평가자를 사용합니다. 이 조회에서는 평가자나 실제 연결을 검사하지 않습니다."
        return result

    def read(name):
        return env_value(name, environment, env_path)

    def missing(reason):
        result.update(configuration_status="missing", can_evaluate=False, reason=reason)
        return result

    def search(policy, cli=False):
        if policy == "disabled":
            result.update(search_status="disabled", search_message="웹 검색이 꺼져 있습니다. 참조 자료가 없는 외부 사실은 확인 필요로 남을 수 있습니다.")
        elif cli:
            result.update(search_status="configured", search_message="사실 검증에 필요하고 참조 자료가 없을 때 CLI 웹 검색을 허용합니다. 실제 검색 연결과 출처 원문 접근은 아직 확인하지 않았습니다.")
        else:
            result.update(search_status="configured", search_message="사실 검증에 필요하고 참조 자료가 없을 때 웹 검색을 허용합니다. 실제 검색 연결은 아직 확인하지 않았습니다.")

    try:
        engine = (read("SEMANTIC_ENGINE") or "jev").strip().lower().replace("-", "_")
        if engine == "auto":
            engine = "openai_api" if read("SEMANTIC_API_KEY") else "codex_cli"
        if engine == "gpt_cli":
            engine = "codex_cli"
        if engine not in _LABELS or engine == "injected":
            raise ValueError("unsupported engine")
        result.update(engine=engine, engine_label=_LABELS[engine])
        if engine == "anthropic_api":
            from .anthropic_semantic_client import AnthropicSemanticConfig, prompt_cache_setting
            config = AnthropicSemanticConfig(
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
            result.update(model=_model_label(config.model), reasoning_effort=_effort_label(config.reasoning_effort))
            search(config.web_search)
            if not read("ANTHROPIC_API_KEY"):
                return missing("Claude API 평가에 사용할 ANTHROPIC_API_KEY가 등록되지 않았습니다.")
        elif engine == "openai_api":
            from .semantic_api_client import SemanticApiConfig, optional_tool_call_limit
            config = SemanticApiConfig(
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
            result.update(model=_model_label(config.model), reasoning_effort=_effort_label(config.reasoning_effort))
            search(config.web_search)
            if not read("SEMANTIC_API_KEY"):
                return missing("의미 평가용 SEMANTIC_API_KEY가 등록되지 않았습니다. 답변 수집용 키는 대신 사용하지 않습니다.")
        elif engine == "codex_cli":
            from .codex_cli_client import CodexCliConfig, find_codex_executable
            executable = find_codex_executable(read("CODEX_CLI_PATH"))
            config = CodexCliConfig(
                executable=executable or "codex", model=read("CODEX_CLI_MODEL") or "gpt-6.1-sol",
                reasoning_effort=read("CODEX_CLI_REASONING") or "high",
                web_search=read("SEMANTIC_WEB_SEARCH") or "live",
                timeout=float(read("CODEX_CLI_TIMEOUT") or 600),
            )
            result.update(model=_model_label(config.model), reasoning_effort=_effort_label(config.reasoning_effort))
            search(config.web_search, cli=True)
            if not executable:
                return missing("GPT CLI 실행 파일을 찾지 못했습니다. 설치 또는 CODEX_CLI_PATH 설정을 확인해 주세요.")
        elif engine == "claude_cli":
            from .claude_cli_client import find_claude_executable
            result["model"] = _model_label(read("CLAUDE_CLI_MODEL"))
            search("disabled")
            if not find_claude_executable(read("CLAUDE_CLI_PATH")):
                return missing("Claude CLI 실행 파일을 찾지 못했습니다. 설치 또는 CLAUDE_CLI_PATH 설정을 확인해 주세요.")
        else:
            base = urlsplit(read("TYPESAFE_API_BASE") or "https://api.typesafe.ai/v1")
            if base.scheme not in ("http", "https") or not base.hostname:
                raise ValueError("invalid base")
            base.port
            result["model"] = _model_label(read("JEV_MODEL") or "jev-latest")
            result.update(search_status="not_supported", search_message="Jev 연결에서는 별도 웹 검색을 실행하지 않습니다. 사실 검증용 참조 자료를 확인해 주세요.")
            if not read("TYPESAFE_API_KEY"):
                return missing("Jev 의미 평가용 TYPESAFE_API_KEY가 등록되지 않았습니다.")
        result.update(configuration_status="configured", can_evaluate=True)
    except Exception:
        # Keep info available and do not echo paths, key values, or invalid input.
        result.update(configuration_status="invalid", can_evaluate=False,
                      reason="의미 평가 설정을 읽을 수 없거나 값이 올바르지 않습니다. 평가 엔진·모델·시간·검색 설정을 확인해 주세요.")
    return result
