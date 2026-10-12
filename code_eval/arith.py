"""3-1 산술 검산용 계산 도구 — ``Fraction`` 정확 계산과 재귀 하강 파서만 쓴다. eval/exec 없음.

반올림 허용 오차, 값 뒤 단위 접미사, 측정 단위, 부정 표현 목록을 함께 둔다.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, List, Optional

__all__ = ["MEASUREMENT_SUFFIXES", "NEGATION_MARKERS", "ROUNDING_TOLERANCE"]

#: 소수로 보고된 값에만 적용하는 반올림 허용 계수.
#: 보고값이 정수면 허용 오차는 0이다 (정확 일치).
#: 보고값이 소수면 마지막 표시 자리의 절반까지 허용한다 ("3.33" → ±0.005).
ROUNDING_TOLERANCE = Fraction(1, 2)

#: 계산식이 주장이 아닐 수 있음을 알리는 표현. 발견되면 판정하지 않는다.
NEGATION_MARKERS = (
    "틀린", "틀립니다", "틀렸", "틀리", "잘못", "오답", "오류",
    "아닙니다", "아니라", "아니고", "아님", "거짓", "반례", "착각",
    "실수", "옳지 않", "맞지 않",
)

#: 숫자 뒤에 바로 붙어도 **값을 바꾸지 않는** 한국어 어미·조사.
_VALUE_SUFFIXES = ("입니다", "이다", "이고", "이며", "예요", "이에요", "임", "이라고",
                   "이었", "였", "인데", "이지", "라고")

#: 등록된 측정 단위. 값 뒤에 붙어도 숫자 자체는 그대로다. 등록된 것만 예외로 둔다.
#: 여기 없는 접미사는 여전히 '확정 불가' 로 남긴다.
MEASUREMENT_SUFFIXES = ("원", "개월", "개", "명", "분", "시간", "초", "일", "년", "월",
                        "점", "회", "번", "배", "자", "줄", "건", "권", "장", "쪽",
                        "km", "cm", "mm", "kg", "g", "m")


def _tokenize(expression: str) -> List[Any]:
    tokens: List[Any] = []
    index = 0
    text = expression.replace(",", "")
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char in "+-*/()":
            tokens.append(char)
            index += 1
            continue
        if char.isdigit():
            start = index
            while index < len(text) and (text[index].isdigit() or text[index] == "."):
                index += 1
            tokens.append(Fraction(text[start:index]))
            continue
        raise ValueError(f"허용되지 않은 문자: {char}")
    return tokens


def _evaluate(expression: str) -> Fraction:
    """+ - * / 와 괄호만 지원하는 재귀 하강 파서."""
    tokens = _tokenize(expression)
    position = 0

    def peek() -> Any:
        return tokens[position] if position < len(tokens) else None

    def parse_expression() -> Fraction:
        nonlocal position
        value = parse_term()
        while peek() in ("+", "-"):
            operator = tokens[position]
            position += 1
            right = parse_term()
            value = value + right if operator == "+" else value - right
        return value

    def parse_term() -> Fraction:
        nonlocal position
        value = parse_factor()
        while peek() in ("*", "/"):
            operator = tokens[position]
            position += 1
            right = parse_factor()
            if operator == "*":
                value = value * right
            else:
                if right == 0:
                    raise ZeroDivisionError("0으로 나눌 수 없습니다")
                value = value / right
        return value

    def parse_factor() -> Fraction:
        nonlocal position
        token = peek()
        if token in ("+", "-"):
            position += 1
            value = parse_factor()
            return value if token == "+" else -value
        if token == "(":
            position += 1
            value = parse_expression()
            if peek() != ")":
                raise ValueError("괄호가 닫히지 않았습니다")
            position += 1
            return value
        if isinstance(token, Fraction):
            position += 1
            return token
        raise ValueError("계산식 형태가 아닙니다")

    result = parse_expression()
    if position != len(tokens):
        raise ValueError("계산식 뒤에 해석되지 않은 부분이 있습니다")
    return result


def _tolerance_for(literal: str) -> Fraction:
    """보고값 표기에 따른 허용 오차.

    정수로 보고했으면 정확 일치를 요구한다 (허용 오차 0). 정수라는 이유로
    ±0.5 를 자동 허용하면 ``74.6 = 75`` 같은 관계까지 통과하게 된다.
    소수로 보고했으면 마지막 표시 자리의 절반까지 반올림으로 인정한다.
    """
    text = literal.replace(",", "").strip()
    if "." not in text:
        return Fraction(0)
    decimals = len(text.split(".", 1)[1])
    return ROUNDING_TOLERANCE * Fraction(1, 10**decimals)


def _as_number(value: Optional[Fraction]) -> Any:
    """JSON 으로 저장할 수 있는 형태로 바꾼다."""
    if value is None:
        return None
    if value.denominator == 1:
        return int(value)
    return float(value)
