"""멀티턴 판별 — 대화가 J6(멀티턴 맥락 유지)를 물을 만큼 긴 대화인지 정한다.

사용자 결정:
- 2026-10-14 (multi-turn-v2): 멀티턴 = **턴이 10개 이상**이고 **대화 전체(모든 턴의 질문 + 답변)가 10K 토큰 이상**인 대화.
  10-13 의 v1 은 '한 턴이 10K 토큰 이상인 턴이 10개 이상'으로 읽어 턴마다 약 2만 자 자료를 붙였는데, 사용자 뜻은 대화 전체
  10K 였다 (턴마다 자료를 과하게 붙이지 않는다).
- 2026-10-21 밤 (multi-turn-v3): 멀티턴 = **한 턴(질문 + 답변)이 10K 토큰 이상인 턴이 10개 이상** + **대화 전체 100K 토큰 이상**
  (사용자: "한턴당 10k이어야하고 10턴 이상이어야하고 100k 이상이어야해" — 평가체계 설계안 v1.9 의 정의로 되돌림). 대화 전체 10K(턴당 1~2천 자)로는
  긴 대화에서 맥락을 유지하는지 볼 수 없다. 질문은 사용자가 다시 만들어 수집한다.
- 각 모델이 실제로 주고받은 대화로 잰다 — 질문은 모든 모델이 같고 답변은 그 모델의 답변이므로, 같은 대화도 모델마다
  멀티턴 여부가 다를 수 있다.
- 멀티턴이면 대화 2번째 턴부터 J6 를 묻고, 아니면 그 대화의 J6 는 해당 없음이다.
- 토큰은 롱컨텍스트와 같은 고정 공개 토크나이저(``long_context``)로 센다. 토크나이저가 없으면 판별하지 못했다고 남긴다.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

from . import long_context

__all__ = ["MULTI_TURN_POLICY_VERSION", "MIN_TURNS", "MIN_TURN_TOKENS", "MIN_TOTAL_TOKENS", "measure_conversation", "describe"]

MULTI_TURN_POLICY_VERSION = "multi-turn-v3"
MIN_TURNS = 10              # 10K 토큰 이상인 턴이 이만큼 있어야 멀티턴
MIN_TURN_TOKENS = 10_000    # 한 턴(질문 + 답변) 토큰 (10-21 밤)
MIN_TOTAL_TOKENS = 100_000   # 대화 전체(모든 턴의 질문 + 답변) 토큰 (10-21 밤: 10K → 100K)


def measure_conversation(turns: Iterable[Tuple[int, str, str]], uncertain: int = 0) -> Dict[str, Any]:
    """[(turn, 질문, 답변)…] → {"policy", "turns", "uncertain_turns", "tokens", "multi_turn", "min_turns", "min_tokens"}.

    ``turns`` 는 확실한 턴(번호마다 평가된 행 하나), ``uncertain`` 은 길이를 알 수 없는 턴 수(빠진 번호·같은 번호가 겹친 턴·
    평가되지 않은 턴). ``multi_turn`` 은 True(확실한 턴만으로 턴 수·토큰 둘 다 충족) · False(불확실한 턴을 더해도 턴 수가 모자라거나,
    불확실한 턴 없이 토큰이 모자람) · None(모름 — J6 확인 필요). 토크나이저를 불러오지 못하면 {"policy", "error", "multi_turn": None}."""
    sizes = []
    try:
        for _, question, answer in turns:
            sizes.append(long_context.count_tokens(f"{question or ''}\n{answer or ''}"))
    except long_context.TokenizerUnavailable as exc:
        return {"policy": MULTI_TURN_POLICY_VERSION, "error": f"멀티턴 여부를 판별하지 못함 — {exc}", "multi_turn": None}
    count, tokens = len(sizes), sum(sizes)
    long_turns = sum(1 for x in sizes if x >= MIN_TURN_TOKENS)   # 10K 토큰 이상인 턴 (10-21 밤)
    if long_turns >= MIN_TURNS and tokens >= MIN_TOTAL_TOKENS:
        multi = True
    elif long_turns + uncertain < MIN_TURNS or (not uncertain and tokens < MIN_TOTAL_TOKENS):
        multi = False
    else:
        multi = None   # 길이를 모르는 턴이 있어 턴 수나 토큰이 찰 수도 있다
    return {"policy": MULTI_TURN_POLICY_VERSION, "turns": count, "long_turns": long_turns, "uncertain_turns": uncertain, "tokens": tokens,
            "multi_turn": multi, "min_turns": MIN_TURNS, "min_turn_tokens": MIN_TURN_TOKENS, "min_tokens": MIN_TOTAL_TOKENS}


def describe(measure: Dict[str, Any]) -> str:
    """판정 기록·해당 없음 사유에 쓰는 한 줄."""
    if measure.get("error"):
        return measure["error"]
    uncertain = measure.get("uncertain_turns") or 0
    text = (f"{measure['turns']}턴(그중 {measure.get('min_turn_tokens', MIN_TURN_TOKENS):,}토큰 이상 {measure.get('long_turns', 0)}턴) · 대화 전체 {measure['tokens']:,}토큰 "
            f"(멀티턴은 한 턴 {measure.get('min_turn_tokens', MIN_TURN_TOKENS):,}토큰 이상인 턴 {measure['min_turns']}개 이상 · 대화 전체 {measure['min_tokens']:,}토큰 이상)")
    if uncertain:
        text += f" · 길이를 알 수 없는 턴 {uncertain}개(빠짐·겹침·평가 안 됨)"
    return ("멀티턴 여부를 판별하지 못함 — " + text) if measure.get("multi_turn") is None else text
