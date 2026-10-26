"""LLM 재검토 호출 — 자격 증명은 환경변수 또는 ``.env`` 에서만 읽는다.

    JUDGE_API_BASE   OpenAI 호환 엔드포인트 기준 주소 (비우면 https://api.openai.com/v1)
    JUDGE_API_KEY    인증값. 코드·로그·결과에 남기지 않는다
    JUDGE_MODEL      기본 gpt-5.6-sol
    JUDGE_REASONING  기본 high

키가 없으면 호출하지 않고 ``JUDGE_MODEL_ACCESS_REQUIRED`` 로 끝낸다. 자격 증명이 없다는 이유로
다른 모델로 바꾸거나 판정을 지어내지 않는다.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

__all__ = ["ACCESS_REQUIRED", "HttpJudgeClient", "JudgeCall", "JudgeClient", "JudgeClientConfig", "env_value",
           "redact", "resolve_client"]

DEFAULT_MODEL = "gpt-5.6-sol"

DEFAULT_REASONING = "high"

DEFAULT_API_BASE = "https://api.openai.com/v1"

ACCESS_REQUIRED = "JUDGE_MODEL_ACCESS_REQUIRED"

#: 실행 경로. 평가 대상 서비스가 아니라 제공자 API 를 직접 부른다.
JUDGE_EXECUTION_PATH = "DIRECT_PROVIDER_API"

TARGET_EXECUTION_PATH = "AIONA_SERVICE"

#: manifest·로그에서 가릴 값의 형태
_SECRET_PATTERN = re.compile(
    r"(?i)(sk-[A-Za-z0-9_\-]{8,}|bearer\s+[A-Za-z0-9._\-]{8,}|"
    r"(api[_-]?key|token|cookie|authorization)\s*[=:]\s*(?:bearer\s+)?[^\s,;}\"']+)"
)


def redact(text: Optional[str]) -> Optional[str]:
    """인증값으로 보이는 부분을 가린다. 기록 전에 반드시 통과시킨다."""
    if not text:
        return text
    return _SECRET_PATTERN.sub("[REDACTED]", text)


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def default_env_path() -> Path:
    """.env — 지금 폴더에 있으면 그것, 없으면 modelgrade 명령이 알려 준 설치 폴더(MODELGRADE_HOME)의 것.
    어느 폴더에서 modelgrade 를 쳐도 같은 키를 읽게 (전역 명령, 사용자 10-26 저녁)."""
    here = Path(".env")
    home = os.environ.get("MODELGRADE_HOME")
    return Path(home) / ".env" if not here.exists() and home else here


def env_value(
    name: str, environment: Optional[Dict[str, str]] = None,
    env_path: Optional[Path] = None,
) -> Optional[str]:
    """환경변수 → ``.env`` 순으로 읽는다. 값을 절대 출력하지 않는다.

    프로젝트가 ``AIONA_COOKIE`` 를 읽는 방식과 같게 둔다. 값은 메모리에서만 쓰고 기록하지
    않는다.
    """
    env = environment if environment is not None else dict(os.environ)
    direct = env.get(name)
    if direct:
        return _strip_quotes(direct)
    path = env_path if env_path is not None else default_env_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.strip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            found = _strip_quotes(value)
            return found or None
    return None


@dataclass(frozen=True)
class JudgeClientConfig:
    """호출 설정. 인증값 자체는 담지 않는다."""

    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING
    api_base: Optional[str] = None
    web: bool = False
    search: bool = False
    tools: bool = False
    connect_timeout: int = 20
    read_timeout: int = 300
    max_attempts: int = 2

    def to_manifest(self) -> Dict[str, Any]:
        return {
            "judge_model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "api_base": redact(self.api_base),
            "judge_execution_path": JUDGE_EXECUTION_PATH,
            "target_execution_path": TARGET_EXECUTION_PATH,
            "web": self.web,
            "search": self.search,
            "tools": self.tools,
            "external_knowledge_lookup": False,
            "max_attempts": self.max_attempts,
            "credentials_source": "environment variable only (JUDGE_API_KEY)",
            "credentials_recorded": False,
        }


@dataclass
class JudgeCall:
    """호출 한 건의 결과. 실패도 결과로 남긴다."""

    case_id: str
    serialization: str
    status: str
    raw_output: Optional[str] = None
    attempts: int = 0
    latency_ms: Optional[int] = None
    error: Optional[str] = None
    judge_model_version: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "serialization": self.serialization,
            "status": self.status,
            "attempts": self.attempts,
            "latency_ms": self.latency_ms,
            "error": redact(self.error),
            "judge_model_version": self.judge_model_version,
            "usage": self.usage,
        }


class JudgeClient(Protocol):
    """Judge 호출 인터페이스. 시험용 대역도 같은 모양을 쓴다."""

    config: JudgeClientConfig

    def judge(self, case_id: str, serialization: str, prompt: str) -> JudgeCall:
        ...


class HttpJudgeClient:
    """OpenAI 호환 endpoint 호출. 자격 증명이 있을 때만 만들어진다."""

    def __init__(self, config: JudgeClientConfig, api_key: str) -> None:
        if not config.api_base:
            raise ValueError("JUDGE_API_BASE 가 없습니다")
        if not api_key:
            raise ValueError("JUDGE_API_KEY 가 없습니다")
        self.config = config
        self._api_key = api_key
        self._session: Any = None

    def _client(self) -> Any:
        if self._session is None:
            import httpx  # 지연 import — 자격 증명이 없으면 쓰지 않는다

            self._session = httpx.Client(
                base_url=self.config.api_base,
                timeout=httpx.Timeout(self.config.read_timeout,
                                       connect=self.config.connect_timeout),
                headers={"Authorization": f"Bearer {self._api_key}",
                          "Content-Type": "application/json"},
            )
        return self._session

    def judge(self, case_id: str, serialization: str, prompt: str) -> JudgeCall:
        payload = {
            "model": self.config.model,
            "reasoning": {"effort": self.config.reasoning_effort},
            "input": [{"role": "user", "content": prompt}],
            "tools": [],
            "store": False,
        }
        started = time.monotonic()
        error: Optional[str] = None
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = self._client().post("/responses", json=payload)
            except Exception as exc:  # 네트워크 오류도 결과로 남긴다
                error = f"{type(exc).__name__}: {exc}"
                continue
            if response.status_code >= 400:
                error = f"HTTP {response.status_code}"
                continue
            body = response.json()
            return JudgeCall(
                case_id=case_id, serialization=serialization, status="OK",
                raw_output=_extract_text(body), attempts=attempt,
                latency_ms=int((time.monotonic() - started) * 1000),
                judge_model_version=body.get("model"),
                usage=body.get("usage") or {},
            )
        return JudgeCall(
            case_id=case_id, serialization=serialization, status="CALL_FAILED",
            attempts=self.config.max_attempts,
            latency_ms=int((time.monotonic() - started) * 1000), error=error,
        )


def _extract_text(body: Dict[str, Any]) -> str:
    """응답 본문에서 텍스트만 꺼낸다. 형태가 달라도 JSON 문자열을 찾는다."""
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    chunks = []
    for item in body.get("output") or []:
        for part in item.get("content") or []:
            if isinstance(part.get("text"), str):
                chunks.append(part["text"])
    if chunks:
        return "\n".join(chunks)
    choices = body.get("choices") or []
    if choices:
        return (choices[0].get("message") or {}).get("content") or ""
    return json.dumps(body, ensure_ascii=False)


def resolve_client(
    environment: Optional[Dict[str, str]] = None,
    env_path: Optional[Path] = None,
) -> tuple[Optional[HttpJudgeClient], JudgeClientConfig, Optional[str]]:
    """환경변수(또는 ``.env``)를 보고 client 를 만든다. 없으면 이유를 돌려준다.

    자격 증명이 없을 때 다른 모델로 바꾸지 않는다. 호출 없이 멈춘다.
    """
    def read(name: str) -> Optional[str]:
        return env_value(name, environment, env_path)

    config = JudgeClientConfig(
        model=read("JUDGE_MODEL") or DEFAULT_MODEL,
        reasoning_effort=read("JUDGE_REASONING") or DEFAULT_REASONING,
        api_base=read("JUDGE_API_BASE") or DEFAULT_API_BASE,
    )
    api_key = read("JUDGE_API_KEY")
    if not api_key:
        return None, config, (
            f"{ACCESS_REQUIRED}: JUDGE_API_KEY 를 찾지 못했습니다 (환경변수와 .env 둘 다 "
            f"확인). {config.model} 을 부를 경로가 없어 calibration 을 실행하지 "
            "않았습니다. 다른 모델로 바꾸거나 판정을 지어내지 않습니다."
        )
    return HttpJudgeClient(config, api_key), config, None
