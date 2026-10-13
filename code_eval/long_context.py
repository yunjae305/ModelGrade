"""롱컨텍스트 판별 — 질문 하나의 입력(현재 질문 + 참조 자료)을 고정 공개 토크나이저로 세어 J7 적용 여부와 길이 구간을 정한다.

사용자 결정(2026-10-13):
- 롱컨텍스트 = 입력이 길이 긴 것. 여러 번 주고받는 대화(멀티턴)는 J6 가 보고, 이전 대화 턴은 여기서 세지 않는다.
- 경계는 8K 토큰 이상. 연구에서는 4K 를 짧은 입력의 기준선으로 쓰고(RULER), 롱컨텍스트 벤치마크 LongBench 의 평균이 약 8K 다.
  공인된 단일 경계는 없으므로 결과에 길이 구간(8K~32K · 32K~128K · 128K 이상 — LV-Eval·RULER·∞Bench 의 단계)을 함께 남긴다.
- 토큰 수는 모델과 상관없이 같은 입력이면 늘 같은 값이 나오도록 고정 공개 토크나이저(tiktoken o200k_base)로 센다.
  처음 한 번 사전 파일을 내려받는다(이후 캐시). 토크나이저가 없으면 J7 을 판별할 수 없다고 기록한다 — 추정으로 대신하지 않는다.
- tiktoken 은 사전을 시간 제한 없이 내려받으므로, 별도 프로세스가 먼저 tiktoken 캐시를 채우게 하고 ``LOAD_TIMEOUT`` 초가
  지나면 그 프로세스를 끝낸다(멈춘 내려받기가 남지 않는다). 이 프로세스는 캐시에서 불러온다. 실패·시간 초과는
  ``RETRY_AFTER`` 초 동안 기억해 질문마다 다시 받으려 하지 않고, 그 뒤에는 새로 시도한다.
"""
from __future__ import annotations

import functools
import json
import re
import subprocess
import sys
import threading
import time
import unicodedata
from typing import Any, Dict, List, Optional

__all__ = ["LONG_CONTEXT_POLICY_VERSION", "MIN_TOKENS", "TOKENIZER", "TokenizerUnavailable", "input_text", "count_tokens",
           "tier", "measure", "quote_positions"]

LONG_CONTEXT_POLICY_VERSION = "long-context-v1"
MIN_TOKENS = 8000
TOKENIZER = "tiktoken o200k_base"
#: (하한, 이름) — 결과에 남기는 길이 구간
TIERS = ((128_000, "128K 이상"), (32_000, "32K~128K"), (MIN_TOKENS, "8K~32K"))

LOAD_TIMEOUT = 20.0   # 사전 내려받기(별도 프로세스)를 기다리는 최대 초
RETRY_AFTER = 300.0   # 실패를 기억하는 초 — 그동안은 바로 '판별 못 함'
_PREFETCH = "import tiktoken; tiktoken.get_encoding('o200k_base')"

_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {"encoder": None, "error": None, "failed_at": 0.0}


class TokenizerUnavailable(RuntimeError):
    """tiktoken 이 없거나 사전 파일을 불러오지 못함 — J7 을 판별할 수 없다."""


def _load() -> Any:
    """별도 프로세스로 사전을 tiktoken 캐시에 받아 둔 뒤(최대 LOAD_TIMEOUT 초, 넘으면 그 프로세스를 끝냄) 캐시에서 불러온다."""
    import tiktoken   # 설치 안 됨 → ImportError
    subprocess.run([sys.executable, "-c", _PREFETCH], timeout=LOAD_TIMEOUT, check=True, capture_output=True)
    return tiktoken.get_encoding("o200k_base")


def _encoder() -> Any:
    with _LOCK:   # 기다리는 동안 다른 스레드도 같은 결과를 기다린다 — 최대 LOAD_TIMEOUT 초
        if _STATE["encoder"] is not None:
            return _STATE["encoder"]
        if _STATE["error"] and time.monotonic() - _STATE["failed_at"] < RETRY_AFTER:
            raise TokenizerUnavailable(f"{TOKENIZER} 를 불러오지 못함: {_STATE['error']}")
        try:
            encoder = _load()
        except subprocess.TimeoutExpired as exc:
            _STATE.update(error=f"TimeoutError({LOAD_TIMEOUT:g}초)", failed_at=time.monotonic())
            raise TokenizerUnavailable(f"{TOKENIZER} 를 불러오지 못함: {_STATE['error']}") from exc
        except Exception as exc:   # 설치 안 됨·내려받기 실패(네트워크·해시 불일치)
            _STATE.update(error=type(exc).__name__, failed_at=time.monotonic())
            raise TokenizerUnavailable(f"{TOKENIZER} 를 불러오지 못함: {_STATE['error']}") from exc
        _STATE.update(encoder=encoder, error=None)
        return encoder


def input_text(question: Any, reference: Any = None) -> str:
    """롱컨텍스트를 재는 입력 — 현재 질문과 참조 자료(문자열이 아니면 JSON). 이전 대화 턴은 넣지 않는다(멀티턴은 J6)."""
    parts = [question if isinstance(question, str) else ""]
    if reference not in (None, "", {}, []):
        parts.append(reference if isinstance(reference, str) else json.dumps(reference, ensure_ascii=False))
    return "\n".join(parts)


@functools.lru_cache(maxsize=32)   # 같은 질문을 legacy·items 적용 판정에서 거듭 센다 — 긴 입력을 다시 토큰화하지 않게
def count_tokens(text: str) -> int:
    return len(_encoder().encode(text or "", disallowed_special=()))


def tier(tokens: int) -> Optional[str]:
    return next((name for floor, name in TIERS if tokens >= floor), None)


def measure(question: Any, reference: Any = None) -> Dict[str, Any]:
    """{"policy", "tokenizer", "tokens", "long", "tier", "min_tokens"} — 토크나이저가 없으면 TokenizerUnavailable."""
    tokens = count_tokens(input_text(question, reference))
    return {"policy": LONG_CONTEXT_POLICY_VERSION, "tokenizer": TOKENIZER, "tokens": tokens, "long": tokens >= MIN_TOKENS,
            "tier": tier(tokens), "min_tokens": MIN_TOKENS}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"\*\*|__|`", "", unicodedata.normalize("NFKC", text or "")).split())


def quote_positions(quotes: List[str], question: Any, reference: Any = None) -> List[Dict[str, Any]]:
    """입력에서 찾은 인용의 위치 — [{"quote", "position"}]. position 은 입력 앞에서부터의 상대 위치(0~100, 정규화 텍스트 기준).

    Lost in the Middle(입력 중간에 있는 정보에서 성능이 떨어짐)을 확인하려고 남기는 기록이며 판정에는 쓰지 않는다.
    """
    text = _norm(input_text(question, reference))
    out = []
    for quote in quotes:
        q = _norm(quote)
        if len(q) < 2:
            continue
        at = text.find(q)
        if at >= 0:
            out.append({"quote": quote.strip(), "position": round(100 * at / max(1, len(text)), 1)})
    return out
