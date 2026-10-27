"""모델 자리 — 기본 네 자리(aiona · gpt · claude · gemini)와 AIONA 두 번째 자리(aiona2, 사용자 10-26).

AIONA 모델 두 개를 한 실행에서 나란히 받거나(`aiona-chat-eval start --aiona … --aiona2 …`), 업데이트 전 · 후 실행의 AIONA 답을
한 비교 실행에 묶을 때(`aiona-chat-eval pair`) 두 번째 AIONA 를 aiona2 자리에 둔다. aiona2 는 AIONA 수집기 · 로고를 쓰고,
보이는 이름은 실행 파일의 models(예: 'AIONA 웹브라우저 (Claude Sonnet 5.5)')에서 온다.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

SLOTS = ("aiona", "aiona2", "gpt", "claude", "gemini")   # 보고서 · 판정에서 보이는 순서


def family(provider: str) -> str:
    """수집기 · 로고 · AIONA 특화 설정을 고르는 이름 — aiona2 도 aiona."""
    return "aiona" if str(provider).startswith("aiona") else str(provider)


def ordered(providers: Iterable[str]) -> List[str]:
    """정해진 순서로 — 모르는 자리는 맨 뒤(이름순)."""
    return sorted(set(providers), key=lambda p: (SLOTS.index(p) if p in SLOTS else len(SLOTS), p))


def short_label(label: Optional[str], provider: str) -> str:
    """보고서 짧은 이름 — 'AIONA 웹브라우저 (Claude Sonnet 5.5)' → 'AIONA · Claude Sonnet 5.5'. 괄호가 없으면 'AIONA' / 'AIONA 2'."""
    m = re.match(r"[^(]*\((.*)\)\s*$", label or "")   # 바깥 괄호 전체 — '(10-25 수집(같은 답))' 처럼 겹쳐도
    if family(provider) == "aiona" and m:
        return f"AIONA · {m.group(1).strip()}"
    return "AIONA 2" if provider == "aiona2" else ("AIONA" if provider == "aiona" else provider)
