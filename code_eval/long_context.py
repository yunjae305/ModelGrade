"""롱컨텍스트 판별 — 질문 하나의 입력(현재 질문 + 참조 자료)을 고정 공개 토크나이저로 세어 J7 적용 여부와 길이 구간을 정한다.

사용자 결정(2026-10-13):
- 롱컨텍스트 = 입력이 길이 긴 것. 여러 번 주고받는 대화(멀티턴)는 J6 가 보고, 이전 대화 턴은 여기서 세지 않는다.
- 경계(사용자 결정 2026-10-13, 테스트 기간): 후보는 272K 토큰. 250K~500K 입력을 실제로 평가해 몇 K 부터 롱컨텍스트로 볼지
  정하려고, 지금은 250K 이상에 J7 을 적용하고 결과를 구간(250K~272K · 272K~300K · 300K~350K · 350K~400K · 400K~500K ·
  500K 이상)으로 나눠 남긴다. 구간별 통과율이 떨어지기 시작하는 곳을 보고 MIN_TOKENS 를 확정한다.
  (예전 경계 8K 는 RULER 4K 기준선·LongBench 평균 약 8K 를 근거로 했다 — long-context-v1)
- 토큰 수는 모델과 상관없이 같은 입력이면 늘 같은 값이 나오도록 고정 공개 토크나이저(tiktoken o200k_base)로 센다.
  처음 한 번 사전 파일을 내려받는다(이후 캐시). 토크나이저가 없으면 J7 을 판별할 수 없다고 기록한다 — 추정으로 대신하지 않는다.
- tiktoken 은 사전을 시간 제한 없이 내려받으므로, 별도 프로세스가 먼저 tiktoken 캐시를 채우게 하고 ``LOAD_TIMEOUT`` 초가
  지나면 그 프로세스를 끝낸다(멈춘 내려받기가 남지 않는다). 이 프로세스는 캐시 파일의 해시를 확인한 뒤에만 불러온다 —
  캐시를 끈 설정이거나 캐시를 쓰지 못했으면 내려받지 않고 판별 못 함으로 둔다. 실패·시간 초과는 ``RETRY_AFTER`` 초 동안
  기억해 질문마다 다시 받으려 하지 않고, 그 뒤에는 새로 시도한다.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["LONG_CONTEXT_POLICY_VERSION", "MIN_TOKENS", "CANDIDATE_TOKENS", "TIERS", "TOKENIZER", "TokenizerUnavailable", "input_text", "count_tokens",
           "tier", "measure", "quote_positions"]

LONG_CONTEXT_POLICY_VERSION = "long-context-v2-250k-test"   # v2: 경계 8K → 250K(테스트 기간), 구간 250K~500K
MIN_TOKENS = 250_000          # 테스트 기간의 적용 하한 — 이 이상이면 J7 을 묻는다
CANDIDATE_TOKENS = 272_000    # 지금 생각하는 경계 — 테스트 결과로 MIN_TOKENS 를 이 값(또는 다른 값)으로 확정한다
TOKENIZER = "tiktoken o200k_base"
#: (하한, 이름) — 결과에 남기는 길이 구간 (큰 것부터). 250K~272K 는 후보 경계 아래의 비교 구간
TIERS = ((500_000, "500K 이상"), (400_000, "400K~500K"), (350_000, "350K~400K"), (300_000, "300K~350K"),
         (CANDIDATE_TOKENS, "272K~300K"), (MIN_TOKENS, "250K~272K"))

LOAD_TIMEOUT = 20.0   # 사전 내려받기(별도 프로세스)를 기다리는 최대 초
RETRY_AFTER = 300.0   # 실패를 기억하는 초 — 그동안은 바로 '판별 못 함'
_PREFETCH = "import tiktoken; tiktoken.get_encoding('o200k_base')"
#: tiktoken_ext.openai_public.o200k_base 가 쓰는 사전 주소와 SHA-256 — 캐시 파일을 이 값으로 확인한다 (다르면 판별 못 함)
_VOCAB_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
_VOCAB_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"

_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {"encoder": None, "error": None, "failed_at": 0.0}


class TokenizerUnavailable(RuntimeError):
    """tiktoken 이 없거나 사전 파일을 불러오지 못함 — J7 을 판별할 수 없다."""


class _VocabUnavailable(RuntimeError):
    """확인된 사전 캐시가 없음 — 메시지를 그대로 기록한다."""


def _cache_file() -> Optional[Path]:
    """tiktoken 이 사전을 캐시하는 파일 (tiktoken.load.read_file_cached 와 같은 규칙). 캐시를 끈 설정이면 None."""
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        cache_dir = os.environ["TIKTOKEN_CACHE_DIR"]
    elif "DATA_GYM_CACHE_DIR" in os.environ:
        cache_dir = os.environ["DATA_GYM_CACHE_DIR"]
    else:
        cache_dir = os.path.join(tempfile.gettempdir(), "data-gym-cache")
    if cache_dir == "":
        return None
    return Path(cache_dir) / hashlib.sha1(_VOCAB_URL.encode()).hexdigest()


def _verified(path: Path) -> bool:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == _VOCAB_SHA256
    except OSError:
        return False


def _load() -> Any:
    """확인된 사전 캐시에서만 불러온다. 캐시가 없으면 별도 프로세스로 받아 둔다(최대 LOAD_TIMEOUT 초, 넘으면 그 프로세스를 끝냄)."""
    import tiktoken   # 설치 안 됨 → ImportError
    path = _cache_file()
    if path is None:
        raise _VocabUnavailable("tiktoken 캐시가 꺼져 있음(TIKTOKEN_CACHE_DIR='') — 시간 제한 없이 내려받게 되므로 쓰지 않음")
    if not _verified(path):
        subprocess.run([sys.executable, "-c", _PREFETCH], timeout=LOAD_TIMEOUT, check=True, capture_output=True)
        if not _verified(path):
            raise _VocabUnavailable(f"사전 캐시를 쓰지 못함 ({path.parent})")
    return tiktoken.get_encoding("o200k_base")   # 해시를 확인한 캐시에서 읽는다 — 내려받지 않는다


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
        except Exception as exc:   # 설치 안 됨·내려받기 실패(네트워크·해시 불일치)·캐시 없음
            _STATE.update(error=str(exc) if isinstance(exc, _VocabUnavailable) else type(exc).__name__, failed_at=time.monotonic())
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
            "tier": tier(tokens), "min_tokens": MIN_TOKENS, "candidate_tokens": CANDIDATE_TOKENS,
            "above_candidate": tokens >= CANDIDATE_TOKENS}


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
