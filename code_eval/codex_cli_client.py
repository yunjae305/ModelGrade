"""GPT CLI(Codex)로 기존 J1~J6 의미 기준을 평가한다.

전체 Q/A·history·reference는 UTF-8 stdin으로 전달한다. 사용자 CLI 로그인은
Codex가 처리하며, 이 프로그램은 인증 파일을 읽거나 결과에 저장하지 않는다.
개인 설정·프로젝트 지시와 일반 도구를 끄고 매 요청을 독립된 임시 폴더에서 실행한다.
J5 검색이 필요한 호출에서만 승인된 검색 호스트 기능을 켠다.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import CancelledError
from typing import Any, Callable, Dict, Optional

from .jev_client import JevCall, JevStatus, _CallAccounting, _token_usage
from .llm_client import redact
from .semantic_cli_common import system_prompt, build_prompt, jev_answer, output_schema
from .semantic_evidence import (finalize_answers, has_reference, source_url, FACT_CHECK_POLICY,
                                CONTEXT_CHECK_POLICY, SEARCH_POLICY)
from .semantic_sources import verify_web_sources
from .cli_process import CliCancelled, run_cli

DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_REASONING = "high"
REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")

# The installed CLI exposes these as feature switches; do not inherit personal tools.
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent",
    "multi_agent_v2", "browser_use", "computer_use", "image_generation", "view_image",
    "memories", "skill_search", "skill_mcp_dependency_install",
    "unbounded_connection_retries",
)
SEARCH_HOST_FEATURES = ("code_mode", "code_mode_host")
SEARCH_HOST_POLICY = "J5_without_reference_and_live"


def find_codex_executable(configured: Optional[str] = None) -> Optional[str]:
    """Windows npm 래퍼 대신 같은 설치의 native exe를 사용한다 (shell=False)."""
    found = shutil.which(configured or "codex")
    if configured and Path(configured).is_file():
        found = str(Path(configured).resolve())
    if not found:
        return None
    if Path(found).suffix.lower() in (".cmd", ".bat", ".ps1"):
        arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
        triple = f"{'aarch64' if arch == 'arm64' else 'x86_64'}-pc-windows-msvc"
        root = Path(found).parent / "node_modules" / "@openai" / "codex"
        candidates = (
            root / "node_modules" / "@openai" / f"codex-win32-{arch}" / "vendor" / triple / "bin" / "codex.exe",
            root.parent / f"codex-win32-{arch}" / "vendor" / triple / "bin" / "codex.exe",
            root / "vendor" / triple / "bin" / "codex.exe",
        )
        return next((str(p) for p in candidates if p.is_file()), None)
    return found


def check_codex_login(executable: str, runner: Callable[..., Any] = subprocess.run) -> Optional[str]:
    """인증값을 읽지 않고 CLI의 로그인 상태만 확인한다. 평가 묶음 시작 때 한 번 호출."""
    try:
        proc = runner([executable, "login", "status"], capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=10, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Codex CLI 로그인 상태 확인 실패 ({type(exc).__name__}). 터미널에서 codex.cmd login status를 확인하세요."
    if proc.returncode:
        return "Codex CLI 로그인 연결이 필요합니다. 터미널에서 codex.cmd login으로 로그인한 뒤 평가를 시작하세요. CODE 평가 결과는 그대로입니다."
    return None


@dataclass(frozen=True)
class CodexCliConfig:
    executable: str = "codex"
    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING
    timeout: float = 600
    max_attempts: int = 1
    engine: str = "codex_cli"
    web_search: str = "live"

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("CODEX_CLI_MODEL 은 빈 문자열일 수 없습니다")
        if self.reasoning_effort not in REASONING_LEVELS:
            raise ValueError(f"지원하지 않는 CODEX_CLI_REASONING: {self.reasoning_effort!r}")
        if not 0 < self.timeout < float("inf"):
            raise ValueError("CODEX_CLI_TIMEOUT 은 0보다 큰 유한한 초 단위 값이어야 합니다")
        if self.max_attempts != 1:
            raise ValueError("Codex CLI 평가는 자동 재시도 없이 질문당 한 번 실행합니다")
        if self.web_search not in ("live", "disabled"):
            raise ValueError("SEMANTIC_WEB_SEARCH는 live 또는 disabled여야 합니다")

    def to_manifest(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "requested_model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "model_source": "explicit --model (CLI does not report the resolved model in JSONL)",
            "executable": self.executable,
            "flags": "exec --ignore-user-config --ephemeral --skip-git-repo-check --sandbox read-only --json --output-schema --output-last-message",
            "disabled_features": list(DISABLED_FEATURES),
            "search_host_features": list(SEARCH_HOST_FEATURES) if self.web_search == "live" else [],
            "search_host_policy": SEARCH_HOST_POLICY,
            "web_search": self.web_search,
            "fact_check_policy": FACT_CHECK_POLICY,
            "fact_check_scope": "all_answers",
            "context_check_policy": CONTEXT_CHECK_POLICY,
            "search_policy": SEARCH_POLICY,
            "project_doc_max_bytes": 0,
            "timeout_s": self.timeout,
            "max_attempts": self.max_attempts,
            "confidence_note": "모델이 스스로 적은 확신도 — 보정된 확률 아님",
            "credentials_source": "Codex CLI 로그인 상태 (이 프로그램은 인증값을 다루지 않음)",
            "credentials_recorded": False,
        }


def _events(stdout: str) -> list[dict]:
    result = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            result.append(event)
    return result


def _error(events: list[dict], stderr: str, returncode: int) -> str:
    messages = []
    for event in events:
        if event.get("type") in ("error", "turn.failed"):
            detail = event.get("error") or event.get("message")
            if isinstance(detail, dict):
                detail = detail.get("message")
            if isinstance(detail, str):
                messages.append(detail)
    detail = " · ".join(messages) or stderr.strip() or "오류 상세 없음"
    return redact(f"Codex CLI 종료 코드 {returncode}: {detail}")[:1000]


def _verification(events):
    """Record completed provider tool events, not URLs in agent-authored prose.

    The installed CLI reports ``item.completed / web_search`` with URL-bearing
    ``results`` and usually omits item.status. URL provenance is not proof that
    a quoted passage occurs at the URL; semantic_sources verifies that next.
    """
    urls, calls = set(), 0
    def visit(value):
        if isinstance(value, dict):
            if (url := source_url(value.get("url"))):
                urls.add(url)
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
    for event in events:
        item = event.get("item")
        if (event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "web_search"
                and item.get("status") in (None, "completed", "success") and not item.get("error")):
            calls += 1
            visit(item)
    return {"search_performed": calls > 0, "source_urls": sorted(urls), "source_validation": "provider",
            "search_calls": calls, "checked_at": datetime.now(timezone.utc).isoformat()}


class CodexCliClient:
    supports_cancellation = True

    def __init__(self, config: CodexCliConfig, runner: Callable[..., Any] = subprocess.run,
                 workdir: Optional[Path] = None, *, source_fetcher: Optional[Callable] = None) -> None:
        self.config = config
        self._run = runner
        self._workdir = workdir
        self._source_fetcher = source_fetcher

    def system_one(self, state: Any, questions: Dict[str, Any], *, cancel_event=None) -> JevCall:
        if cancel_event is not None and cancel_event.is_set():
            return JevCall(JevStatus.SKIPPED, attempts=0, error="평가 중단 요청으로 실행하지 않았습니다.")
        started = time.monotonic()
        search_enabled = "J5" in questions and not has_reference(state) and self.config.web_search == "live"
        verification = _verification([])
        accounting = _CallAccounting(self.config.model)
        usage = {}

        def observe(events, *, partial=False):
            nonlocal verification, usage
            verification = _verification(events)
            usage = next((_token_usage(e["usage"]) for e in reversed(events)
                          if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict)), {})
            search_calls = 0
            for event in events:
                item = event.get("item")
                if event.get("type") != "item.completed" or not isinstance(item, dict) or item.get("type") != "web_search":
                    continue
                action = item.get("action")
                kind = action.get("type") if isinstance(action, dict) else None
                if (kind not in ("search", "open_page", "find_in_page")
                        or item.get("status") not in (None, "completed", "success") or item.get("error")):
                    search_calls = None
                    break
                search_calls += kind == "search"
            accounting.add(model=self.config.model, usage=usage,
                           web_search_calls=None if partial else search_calls)

        def result(status: str, **kwargs) -> JevCall:
            body = kwargs.pop("body", None) or {}
            body.setdefault("verification", verification)
            return JevCall(status, body=body, attempts=1,
                           usage=kwargs.pop("usage", usage), accounting=accounting.snapshot(1),
                           latency_ms=int((time.monotonic() - started) * 1000), **kwargs)

        try:
            # Each parallel request gets different schema/output paths. No stale output can be reused.
            with tempfile.TemporaryDirectory(prefix="aiona-codex-judge-", dir=self._workdir) as folder:
                cwd = Path(folder)
                schema_path, output_path = cwd / "schema.json", cwd / "answer.json"
                schema_path.write_text(json.dumps(output_schema(questions), ensure_ascii=False), encoding="utf-8")
                cmd = [self.config.executable, "exec", "--ignore-user-config", "--ephemeral",
                       "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never", "--json",
                       "--model", self.config.model,
                       "-c", f"model_reasoning_effort={json.dumps(self.config.reasoning_effort)}",
                       "-c", "approval_policy=\"never\"", "-c", f"web_search={json.dumps('live' if search_enabled else 'disabled')}",
                       "-c", "project_doc_max_bytes=0", "-c", "mcp_servers={}",
                       "-c", f"developer_instructions={json.dumps(system_prompt(search_enabled), ensure_ascii=False)}",
                       "--output-schema", str(schema_path), "--output-last-message", str(output_path)]
                for feature in DISABLED_FEATURES:
                    cmd += ["--disable", feature]
                for feature in SEARCH_HOST_FEATURES:
                    cmd += ["--enable" if search_enabled else "--disable", feature]
                cmd.append("-")
                runner = run_cli if self._run is subprocess.run else self._run
                cancel_options = {"cancel_event": cancel_event} if runner is run_cli else {}
                proc = runner(cmd, input=build_prompt(state, questions, search_enabled), capture_output=True, text=True,
                                 encoding="utf-8", timeout=self.config.timeout, cwd=folder,
                                 shell=False, **cancel_options)
                events = _events(proc.stdout or "")
                observe(events)
                completed = any(e.get("type") == "turn.completed" for e in events)
                if cancel_event is not None and cancel_event.is_set() and (proc.returncode or not completed):
                    return result(JevStatus.SKIPPED, error="평가 중단 요청으로 CLI 실행을 종료했습니다.")
                if proc.returncode or any(e.get("type") == "turn.failed" for e in events) or (
                        not completed and any(e.get("type") == "error" for e in events)):
                    return result(JevStatus.API_ERROR, error=_error(events, proc.stderr or "", proc.returncode))
                if not completed:
                    return result(JevStatus.INVALID_RESPONSE, error="Codex CLI 평가 완료 이벤트가 없음")
                if any(e.get("type") in ("context.compacted", "thread.compacted") or
                       (e.get("item") or {}).get("type") in ("context_compaction", "compaction") for e in events):
                    return result(JevStatus.INVALID_RESPONSE, error="대화 이력이 압축되어 원문 전체 평가를 확인할 수 없습니다")
                try:
                    structured = json.loads(output_path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    return result(JevStatus.INVALID_RESPONSE, error="Codex CLI 최종 결과 파일이 없거나 JSON이 아님")
                if not isinstance(structured, dict) or set(structured) != set(questions):
                    return result(JevStatus.INVALID_RESPONSE, error="Codex CLI 결과의 평가 기준 ID가 요청과 다름")
                body = {"model": self.config.model, "verification": verification,
                        "answers": {cid: jev_answer(structured[cid]) for cid in questions}}
                j5 = body['answers'].get('J5')
                fact_check = j5.get('fact_check') if isinstance(j5, dict) else None
                if isinstance(fact_check, dict) and fact_check.get('basis') in ('web', 'mixed'):
                    body = verify_web_sources(body, fetcher=self._source_fetcher, cancel_event=cancel_event)
                return result("OK", body=finalize_answers(body, state, questions),
                              usage=usage if isinstance(usage, dict) else {})
        except CancelledError as exc:
            if isinstance(exc, CliCancelled):
                partial = exc.stdout or ""
                if isinstance(partial, bytes):
                    partial = partial.decode("utf-8", errors="replace")
                if not exc.started:
                    return JevCall(JevStatus.SKIPPED, attempts=0, error=str(exc))
                observe(_events(partial), partial=True)
            return result(JevStatus.SKIPPED, error="평가 중단 요청으로 완료하지 못했습니다.")
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            events = _events(partial)
            observe(events, partial=True)
            detail = f" · {_error(events, '', -1)}" if any(e.get("type") == "error" for e in events) else ""
            return result(JevStatus.TIMEOUT, error=f"Codex CLI 실행이 {self.config.timeout}초 안에 끝나지 않음{detail}")
        except OSError as exc:
            return result(JevStatus.API_ERROR, error=redact(f"Codex CLI 실행·파일 처리 실패: {type(exc).__name__}: {exc}")[:1000])
        except UnicodeError as exc:
            return result(JevStatus.INVALID_RESPONSE if isinstance(exc, UnicodeDecodeError) else JevStatus.API_ERROR,
                          error=f"Codex CLI UTF-8 처리 실패 ({type(exc).__name__}). 원문을 대체 문자로 변경하지 않았습니다.")
