"""멀티턴 판별 — 대화가 J6(멀티턴 맥락 유지)를 물을 만큼 긴 대화인지 정한다.

사용자 결정(2026-10-13):
- 멀티턴 = 한 턴(그 턴의 질문 + 답변)이 10K 토큰 이상인 턴이 10개 이상인 대화 (합계 100K 이상). 2턴 이상이면 멀티턴으로 보던
  예전 규칙을 바꾼 것이다. 10K 미만인 짧은 턴이 섞여 있어도 조건을 채운 턴이 10개 이상이면 멀티턴이다.
- 각 모델이 실제로 주고받은 대화로 잰다 — 질문은 모든 모델이 같고 답변은 그 모델의 답변이므로, 같은 대화도 모델마다
  멀티턴 여부가 다를 수 있다.
- 멀티턴이면 지금처럼 대화 2번째 턴부터 J6 를 묻고, 아니면 그 대화의 J6 는 해당 없음이다.
- 토큰은 롱컨텍스트와 같은 고정 공개 토크나이저(``long_context``)로 센다. 토크나이저가 없으면 판별하지 못했다고 남긴다.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

from . import long_context

__all__ = ["MULTI_TURN_POLICY_VERSION", "MIN_TURN_TOKENS", "MIN_LONG_TURNS", "measure_conversation", "describe"]

MULTI_TURN_POLICY_VERSION = "multi-turn-v1"
MIN_TURN_TOKENS = 10_000   # 긴 턴 — 그 턴의 질문 + 답변
MIN_LONG_TURNS = 10        # 긴 턴이 이만큼 있어야 멀티턴


def measure_conversation(turns: Iterable[Tuple[int, str, str]], uncertain: int = 0) -> Dict[str, Any]:
    """[(turn, 질문, 답변)…] → {"policy", "turns", "long_turns", "uncertain_turns", "tokens", "multi_turn", …}.

    ``turns`` 는 확실한 턴(번호마다 평가된 행 하나), ``uncertain`` 은 길이를 알 수 없는 턴 수(빠진 번호·같은 번호가 겹친 턴·
    평가되지 않은 턴) — 모두 긴 턴일 수 있다. ``multi_turn`` 은 True(확실한 긴 턴만으로 충족) · False(불확실한 턴이 모두 길어도
    부족) · None(모름 — J6 확인 필요). 토크나이저를 불러오지 못하면 {"policy", "error", "multi_turn": None}."""
    sizes = []
    try:
        for _, question, answer in turns:
            sizes.append(long_context.count_tokens(f"{question or ''}\n{answer or ''}"))
    except long_context.TokenizerUnavailable as exc:
        return {"policy": MULTI_TURN_POLICY_VERSION, "error": f"멀티턴 여부를 판별하지 못함 — {exc}", "multi_turn": None}
    long_turns = sum(1 for size in sizes if size >= MIN_TURN_TOKENS)
    multi = True if long_turns >= MIN_LONG_TURNS else False if long_turns + uncertain < MIN_LONG_TURNS else None
    return {"policy": MULTI_TURN_POLICY_VERSION, "turns": len(sizes), "long_turns": long_turns, "uncertain_turns": uncertain,
            "tokens": sum(sizes), "multi_turn": multi, "min_turn_tokens": MIN_TURN_TOKENS, "min_long_turns": MIN_LONG_TURNS}


def describe(measure: Dict[str, Any]) -> str:
    """판정 기록·해당 없음 사유에 쓰는 한 줄."""
    if measure.get("error"):
        return measure["error"]
    uncertain = measure.get("uncertain_turns") or 0
    text = (f"{measure['min_turn_tokens']:,}토큰 이상 턴 {measure['long_turns']}개 / {measure['turns']}턴 "
            f"(총 {measure['tokens']:,}토큰, 멀티턴은 {measure['min_long_turns']}개 이상)")
    if uncertain:
        text += f" · 길이를 알 수 없는 턴 {uncertain}개(빠짐·겹침·평가 안 됨)"
    return ("멀티턴 여부를 판별하지 못함 — " + text) if measure.get("multi_turn") is None else text
