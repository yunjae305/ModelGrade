"""Jev 대체 시험용 의미 평가 — Claude CLI(``claude -p``)로 J1~J7 중 적용 기준을 판단한다.

Jev 키가 생기기 전 시험용이다. Jev client 와 같은 호출 모양(``system_one``)과 응답 모양
(``answers.{J}.choice / probabilities / confidence``)을 돌려주므로 ``jev_eval`` 을 그대로 쓴다.

    SEMANTIC_ENGINE=claude_cli   (또는 CLI ``--semantic-engine claude-cli``)
    CLAUDE_CLI_MODEL             비우면 Claude CLI 기본 모델
    CLAUDE_CLI_PATH              비우면 PATH 의 claude (npm 래퍼면 옆의 claude.exe 를 직접 부른다)

- 기준 문구는 Jev 와 같은 ``jev_criteria`` 정의(요청에 담긴 questions)를 그대로 쓴다.
- ``--safe-mode`` 로 CLAUDE.md·훅·플러그인을, ``--tools ""`` 로 도구를 끈다. 출력은 ``--json-schema`` 로 고정한다.
- confidence 는 모델이 스스로 적은 값이다. Jev 처럼 보정된 확률이 아니다.
- 인증은 Claude CLI 로그인 상태를 쓴다. 이 프로그램은 인증값을 읽거나 기록하지 않는다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .jev_client import JevCall, JevStatus, _CallAccounting, _token_usage, _amount
from .llm_client import redact
from .semantic_cli_common import (SYSTEM_PROMPT, build_prompt, output_schema, jev_answer as _jev_answer,
                                  system_prompt as _system_prompt, is_items, strip_search_items, validate_items_shape,
                                  SEARCH_NOT_AVAILABLE_REASON)
from .semantic_evidence import finalize_answers, search_mode, FACT_CHECK_POLICY, CONTEXT_CHECK_POLICY
from .cli_process import CliCancelled, run_cli

__all__ = ["ClaudeCliConfig", "ClaudeCliClient", "find_claude_executable", "build_prompt", "output_schema"]

def find_claude_executable(configured: Optional[str] = None) -> Optional[str]:
    """claude 실행 파일 경로. Windows npm 래퍼(claude.cmd)는 인자 따옴표가 깨질 수 있어 옆의 claude.exe 를 쓴다."""
    if configured:
        return configured if Path(configured).exists() or shutil.which(configured) else None
    found = shutil.which("claude")
    if found and Path(found).suffix.lower() in (".cmd", ".bat", ".ps1", ""):
        native = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if native.exists():
            return str(native)
    return found


@dataclass(frozen=True)
class ClaudeCliConfig:
    executable: str = "claude"
    model: Optional[str] = None
    timeout: float = 300
    max_attempts: int = 2
    engine: str = "claude_cli"

    def to_manifest(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "requested_model": self.model or "Claude CLI 기본 모델",
            "executable": self.executable,
            "flags": "-p --output-format json --json-schema --tools '' --safe-mode --no-session-persistence --system-prompt",
            "timeout_s": self.timeout,
            "max_attempts": self.max_attempts,
            "web_search": "disabled",
            "fact_check_policy": FACT_CHECK_POLICY,
            "fact_check_scope": "all_answers",
            "context_check_policy": CONTEXT_CHECK_POLICY,
            "confidence_note": "모델이 스스로 적은 확신도 — 보정된 확률 아님",
            "credentials_source": "Claude CLI 로그인 상태 (이 프로그램은 인증값을 다루지 않음)",
            "credentials_recorded": False,
        }


def _model_of(output: Dict[str, Any]) -> Optional[str]:
    usage = output.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return None
    return max(usage, key=lambda k: (usage[k] or {}).get("outputTokens", 0))


class ClaudeCliClient:
    """``claude -p`` 를 질문마다 한 번 실행한다. 실패도 JevCall 로 돌려준다."""

    supports_cancellation = True

    def __init__(self, config: ClaudeCliConfig, runner: Callable[..., Any] = subprocess.run,
                 workdir: Optional[Path] = None, *, source_fetcher: Optional[Callable] = None) -> None:
        self.config = config
        self._run = runner
        self._workdir = workdir
        self._source_fetcher = source_fetcher   # items 파이프라인의 J5 출처 존재 확인용 (없으면 공개 웹 수집기)

    def _cwd(self) -> str:
        # 프로젝트 밖 빈 폴더에서 실행해 프로젝트 설정·문서를 읽지 않게 한다.
        path = self._workdir or Path(tempfile.gettempdir()) / "aiona-claude-judge"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def system_one(self, state: Any, questions: Dict[str, Any], *, cancel_event=None) -> JevCall:
        if cancel_event is not None and cancel_event.is_set():
            return JevCall(JevStatus.SKIPPED, attempts=0, error="평가 중단 요청으로 실행하지 않았습니다.")
        items_mode = is_items(questions)
        not_sent = []
        if items_mode:
            # 이 엔진은 웹검색이 없다: 라이브 검색이 있어야 하는 항목(J5-1.a)은 기억으로 판정시키지 않고 보내지 않는다
            questions, removed = strip_search_items(questions)
            not_sent = [{"id": item_id, "reason": SEARCH_NOT_AVAILABLE_REASON} for item_id in removed]
        ids = list(questions)
        cmd = [self.config.executable, "-p", "--output-format", "json",
               "--json-schema", json.dumps(output_schema(questions), ensure_ascii=False),
               "--tools", "", "--safe-mode", "--no-session-persistence",
               "--system-prompt", _system_prompt(False, questions) if items_mode else SYSTEM_PROMPT]
        if self.config.model:
            cmd += ["--model", self.config.model]
        prompt = build_prompt(state, questions)
        started = time.monotonic()
        status, error, attempt = JevStatus.API_ERROR, None, 0
        accounting = _CallAccounting(self.config.model)

        def observe(output):
            model_usage = {}
            for model, values in (output.get("modelUsage") or {}).items() if isinstance(output.get("modelUsage"), dict) else []:
                if not isinstance(model, str) or not model.strip() or not isinstance(values, dict):
                    continue
                usage = {target: values[source] for source, target in (
                    ("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                    ("cacheReadInputTokens", "cached_input_tokens"),
                    ("cacheCreationInputTokens", "cache_creation_input_tokens")) if source in values}
                part = {"usage": usage}
                if _amount(values.get("costUSD")):
                    part["reported_cost_usd"] = values["costUSD"]
                model_usage[model] = part
            model = next(iter(model_usage)) if len(model_usage) == 1 else None if model_usage else self.config.model
            accounting.add(model=model, usage=output.get("usage"), web_search_calls=0,
                           reported_cost_usd=output.get("total_cost_usd"), model_usage=model_usage or None)

        for attempt in range(1, self.config.max_attempts + 1):
            if cancel_event is not None and cancel_event.is_set():
                status, error, attempt = JevStatus.SKIPPED, "평가 중단 요청으로 실행하지 않았습니다.", attempt - 1
                break
            try:
                runner = run_cli if self._run is subprocess.run else self._run
                cancel_options = {"cancel_event": cancel_event} if runner is run_cli else {}
                proc = runner(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              timeout=self.config.timeout, cwd=self._cwd(), **cancel_options)
            except CliCancelled as exc:
                try:
                    partial = json.loads(exc.stdout or "")
                except (ValueError, UnicodeError):
                    partial = None
                if isinstance(partial, dict):
                    observe(partial)
                if not exc.started:
                    attempt -= 1
                status, error = JevStatus.SKIPPED, str(exc)
                break
            except subprocess.TimeoutExpired as exc:
                try:
                    partial = json.loads(exc.stdout or "")
                except (ValueError, UnicodeError):
                    partial = None
                if isinstance(partial, dict):
                    observe(partial)
                status, error = JevStatus.TIMEOUT, f"claude 실행이 {self.config.timeout}초 안에 끝나지 않음"
                continue
            except OSError as exc:  # 실행 파일 문제는 다시 시도해도 같다
                status, error = JevStatus.API_ERROR, f"claude 실행 실패: {type(exc).__name__}: {exc}"
                break
            elapsed = int((time.monotonic() - started) * 1000)
            try:
                output = json.loads(proc.stdout or "")
            except ValueError:
                status, error = (JevStatus.API_ERROR, f"claude 종료 코드 {proc.returncode}: {(proc.stderr or '').strip()[:300]}") \
                    if proc.returncode else (JevStatus.INVALID_RESPONSE, "claude 출력이 JSON 이 아님")
                continue
            if not isinstance(output, dict):
                status, error = JevStatus.INVALID_RESPONSE, "claude 출력 최상위가 객체가 아님"
                continue
            observe(output)
            if cancel_event is not None and cancel_event.is_set() and proc.returncode:
                status, error = JevStatus.SKIPPED, "평가 중단 요청으로 CLI 실행을 종료했습니다."
                break
            if proc.returncode or output.get("is_error"):
                status, error = JevStatus.API_ERROR, f"claude 오류: {str(output.get('result') or output.get('subtype'))[:300]}"
                continue
            structured = output.get("structured_output")
            if not isinstance(structured, dict):
                return JevCall(JevStatus.INVALID_RESPONSE, attempts=attempt, latency_ms=elapsed,
                               error="claude 출력에 structured_output 이 없음", accounting=accounting.snapshot(attempt))
            usage = _token_usage(output.get("usage"))
            verification = {"search_performed": False, "source_urls": [], "source_validation": "provider",
                            "search_mode": search_mode(questions, state, "disabled", supported=False)}
            if not_sent:
                verification["items_not_sent"] = list(not_sent)
            if items_mode:
                if validate_items_shape(structured, questions):
                    return JevCall(JevStatus.INVALID_RESPONSE, attempts=attempt, latency_ms=elapsed,
                                   error="claude 출력이 items 응답 형식과 다름", accounting=accounting.snapshot(attempt))
                # items 본문: J5 서버 검증(server_check)이 verification 을 제자리에서 채우고 finalize_answers 는 그대로 통과한다
                from .semantic_facts import server_check
                body = server_check({"model": _model_of(output), "verification": verification, "items": structured["items"]},
                                    state, questions, verification, fetcher=self._source_fetcher, cancel_event=cancel_event)
                return JevCall("OK", attempts=attempt, latency_ms=elapsed, body=finalize_answers(body, state, questions),
                               usage=usage, accounting=accounting.snapshot(attempt))
            return JevCall("OK", attempts=attempt, latency_ms=elapsed,
                           body=finalize_answers({"model": _model_of(output), "verification": verification,
                               "answers": {cid: _jev_answer(structured.get(cid)) for cid in ids}}, state, questions),
                           usage=usage, accounting=accounting.snapshot(attempt))
        return JevCall(status, attempts=attempt, latency_ms=int((time.monotonic() - started) * 1000),
                       error=redact(error), accounting=accounting.snapshot(attempt))
