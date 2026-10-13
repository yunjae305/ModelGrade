"""3. 수치·데이터 정합성 — 3-1 계산 결과 / 3-2 선언 개수 / 3-3 원본 보존·변환.

- 계산은 ``fractions.Fraction`` 정확 계산과 ``arith`` 의 재귀 하강 파서만 쓴다. eval/exec 없음.
- 답변 값은 라벨·구조로 연결한다. 기대값과 닮았다는 이유로 고르지 않는다.
"""

from __future__ import annotations

import json
import re
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .analysis import mask_ranges
from .arith import (
    MEASUREMENT_SUFFIXES,
    NEGATION_MARKERS,
    _VALUE_SUFFIXES,
    _as_number as _fraction_value,
    _evaluate,
    _tolerance_for,
)
from .checks_integrity import json_targets
from .core import Ctx, cmp_text, excerpt, q_evidence, result, skip_condition, skip_req, verdict_of
from .extract import NUM_WORDS, NUMBER, parse_number
from .model import Evidence, FAIL, PASS, Status
from .structure_counts import heading_series_from

__all__ = ["check_3_1", "check_3_2", "check_3_3"]

SUB31, SUB32, SUB33 = "3-1", "3-2", "3-3"


# 연산자 앞에서 지워도 값이 바뀌지 않는 단위: 등록 측정 단위 전체 + 자루. 줄바꿈은 넘지 않는다.
#: 연산자 앞에서 지우는 단위. 달력의 '월'(3월 - 5월 = 3개월 같은 범위·순환 표기)은 산술 단위가 아니라 뺀다.
_UNITS = sorted((set(MEASUREMENT_SUFFIXES) | {"자루", "%"}) - {"월"}, key=len, reverse=True)
_UNIT_BEFORE_OP = re.compile(r"(?<=\d)(?:" + "|".join(map(re.escape, _UNITS)) + r")(?=[ \t ]*[*/+\-=)])")
_TEX_OPS = {r"\times": "*", r"\cdot": "*", r"\div": "/"}


def _equation_text(ctx: Ctx) -> str:
    """등식 검산용 투영 (위치 보존): 인라인 코드 가림, ×÷ → */, 강조 ** 제거, TeX 구분자·연산자 치환,
    ``1인당`` 수식어 제거, 연산자 앞 단위 제거.

    ``%`` 는 연산자 앞에서도 값이 바뀌므로 지우지 않고 식을 끊는다.
    """
    if "equation_text" not in ctx.shared:
        text = mask_ranges(ctx.answer, ctx.aa.inline_code_spans, fill="§")
        text = text.replace("×", "*").replace("÷", "/").replace("−", "-").replace("**", "  ")
        # TeX delimiters \( \) \[ \] are not arithmetic parentheses; TeX operators keep their length.
        text = re.sub(r"\\[()\[\]]", "  ", text)
        text = re.sub(r"\\(?:times|cdot|div)(?![A-Za-z])", lambda m: _TEX_OPS[m.group()].ljust(len(m.group())), text)
        # TeX groups thousands as 2{,}000. Preserve offsets while restoring the number.
        text = re.sub(r"\d{1,3}(?:\{,\}\d{3})+",
                      lambda m: m.group().replace("{,}", ",").ljust(len(m.group())), text)
        # A price unit such as 520원/100mL is a suffix, not 520 divided by 100.
        text = re.sub(r"(?<=\d)원/\d+(?:mL|L|kg|g|km|cm|mm|m|개|회)",
                      lambda m: " " * len(m.group()), text)
        # Numeric-only shorthand. Keep every replacement the original length so evidence offsets remain valid.
        text = re.sub(r"(?<![\w.])([0-9])(?:\^2|²)(?!\d)",
                      lambda m: str(int(m.group(1)) ** 2).ljust(len(m.group())), text)
        text = re.sub(r"(?<=\d)\(\s*(\d{1,6})\s*\)",
                      lambda m: ("*" + m.group(1)).ljust(len(m.group())), text)
        # '= 1인당 16,000원': a per-person qualifier before the value, not the literal 1.
        text = re.sub(r"(?<![\d.,])1인당(?=[ \t]*\d)", "   ", text)
        text = _UNIT_BEFORE_OP.sub(lambda m: "§" if m.group(0) == "%" else " " * len(m.group(0)), text)
        ctx.shared["equation_text"] = text
    return ctx.shared["equation_text"]


_CORRECTION_AFTER = re.compile(r"올바른\s*(?:식|답|계산|풀이)|바른\s*식|정답은|바르게\s*고치면|고친\s*식")
_MISTAKE = re.compile(r"(?<![가-힣])실수(?=$|[\s.,!?)]|[가를로는인했하였])")
# 식 안의 공백은 수평 공백뿐이다: 줄바꿈 뒤의 불릿 `- 4인`·`* 4인` 을 연산자로 읽지 않는다.
# 줄바꿈은 다음 줄이 `=` 로 시작하는 정렬 유도식(`…\n= 16 + 21\n= 82점`)에서만 이어 읽는다.
_HS = r"[ \t ]*"   # 수평 공백(NBSP 포함). 줄바꿈은 아래 _EQ 의 '=' 줄머리 유도식에서만 넘는다.
_TERM = rf"\(*{_HS}-?(?:{NUMBER}){_HS}\)*"
_SEG = rf"{_TERM}(?:{_HS}[+\-*/]{_HS}{_TERM})*"
_EQ = rf"(?:{_HS}\r?\n)?{_HS}={_HS}"
_SPACED_KO_UNIT = re.compile(r"[ \t ]+(?:만|천|억|조)(?![가-힣])")   # '= 2 만' 처럼 띄어 쓴 한글 수 단위
_NEG_EXAMPLE = re.compile(r"(?<![가-힣])(?:정답|답|예|식|계산|풀이)\s*(?:이|은|는|가|도)?\s*(?:아님|아닌|아닙니다|아니에요|아니다)"
                          r"|(?:아님|아닌|아닙니다)\s*[:：]")   # '방식이 아니라' 의 '식이 아니라' 는 걸리지 않는다
_CHAIN = re.compile(rf"(?<![\w.,§]){_SEG}(?:{_EQ}{_SEG})+(?![\d.,])")
# 명사 부정은 조건 설명이다(‘A가 아니라 B’의 A, ‘X가 아닙니다’의 X): 같은 문장에서 표지 앞의 등식만 보류한다.
# 나머지 오류 예시 표지(틀린·잘못·오답·오류·실수·반례 …)는 자기 줄 + 앞 2줄에서 찾는다.
_NOUN_NEG = ("아니라", "아니고", "아닙니다", "아님", "아닌")
_SENT_END = re.compile(r"[.!?。](?=\s|$)")   # 1.5 의 소수점은 문장 끝이 아니다 (textparse 규칙)
# 좌변 바로 앞(같은 줄)의 화살표는 값의 변화 표시다: '20일 → 26일 = 26 ÷ 20 = 1.3배' 의 26 은 등식의 시작이 아니다.
_ARROW_BEFORE = re.compile(r"(?:→|⇒|->|=>)[ \t\xa0]*$")
# '185분 / 190분 이하' 처럼 '/' 뒤 값에 한도 표현이 붙으면 나눗셈이 아니라 '값 / 한도' 표기다.
_LIMIT_AFTER = re.compile(r"[ \t\xa0]*(?:" + "|".join(map(re.escape, _UNITS)) + r")?[ \t\xa0]*(?:이하|이상|미만|초과|이내)")


def _balance_parens(raw: str) -> Optional[Tuple[int, str]]:
    """매치 끝 공백과 짝 없는 뒤쪽 `)`·앞쪽 `(` 만 잘라낸다. 안쪽 짝이 안 맞으면 None (행 없음).

    `파일(10 - 2 - 3 = 5개)은` 의 끝 `)`, `(24,000 + 27,000 = 51,000원으로` 의 앞 `(` 가 대상이다.
    반환값은 (앞에서 잘라낸 길이, 본문).
    """
    body = raw.strip()
    lead = len(raw) - len(raw.lstrip())   # 선행 공백·치환된 마크업 자리는 근거 발췌에서 뺀다
    while True:
        depth, bad = 0, None
        for i, c in enumerate(body):
            depth += (c == "(") - (c == ")")
            if depth < 0:
                bad = i
                break
        if bad is None:
            break
        if body[bad + 1:].strip():
            return None
        body = body[:bad].rstrip()
    while body.count("(") > body.count(")"):
        if not body.startswith("("):
            return None
        stripped = body[1:].lstrip()
        lead += len(body) - len(stripped)
        body = stripped
    return lead, body


def _negated(lines, line_idx: int, start: int, end: int) -> str:
    """등식을 보류시키는 부정·오류 예시 표지를 돌려준다 (없으면 빈 문자열)."""
    line = lines[line_idx]
    own = line.text
    rel_s, rel_e = start - line.start, end - line.start
    s0 = 0
    for m in _SENT_END.finditer(own):
        if m.end() <= rel_s:
            s0 = m.end()
        else:
            break
    s1 = next((m.start() for m in _SENT_END.finditer(own, rel_e)), len(own))
    sentence = own[s0:s1]
    for w in _NOUN_NEG:
        if any(rel_s - s0 < m.start() for m in re.finditer(re.escape(w), sentence)):
            return w
    context = [own] + [l.text for l in lines[max(0, line_idx - 3):line_idx] if l.text.strip()][-2:]
    # '정답 아님:', '이 식은 답이 아닙니다.' 처럼 명사 부정으로 오류 예시를 소개하는 꼴은 오류 예시 표지로 본다.
    if any(_NEG_EXAMPLE.search(c) for c in context):
        return "정답 아님"
    return next((w for w in NEGATION_MARKERS if w not in _NOUN_NEG for c in context
                 if (_MISTAKE.search(c) if w == "실수" else w in c)), "")


class Chain:
    """``식 = 식 = 값`` 연쇄 등식 하나. 모든 인접 구간이 같은 값이어야 한다."""

    def __init__(self, text: str, start: int, end: int) -> None:
        self.text, self.start, self.end = text, start, end
        self.segments = [seg.strip() for seg in text.split("=")]
        self.values: List[Optional[Fraction]] = []
        self.skip_reason = ""
        self.mismatch: Optional[Tuple[str, str]] = None

    @property
    def has_operation(self) -> bool:
        return any(re.search(r"\d\s*[+\-*/]\s*[\d(]", seg) for seg in self.segments)

    @property
    def final_literal(self) -> Optional[str]:
        last = self.segments[-1]
        return last if re.fullmatch(rf"-?(?:{NUMBER})", last) else None

    def to_detail(self) -> Dict[str, Any]:
        return {"expression": self.text.strip(), "values": [_fmt(v) if v is not None else None for v in self.values],
                "char_range": [self.start, self.end]}


def _find_chains(ctx: Ctx) -> List[Chain]:
    if "chains" in ctx.shared:
        return ctx.shared["chains"]
    text = _equation_text(ctx)
    lines = ctx.aa.lines
    chains: List[Chain] = []
    for m in _CHAIN.finditer(text):
        if ctx.aa.in_fence(m.start()):
            continue
        balanced = _balance_parens(m.group(0))
        if balanced is None:
            # 안쪽 괄호 짝이 안 맞는 조각(괄호 안 등호 등)은 행을 만들지 않고 사유만 남긴다
            ch = Chain(m.group(0).strip(), m.start(), m.end())
            ch.skip_reason = "재계산할 수 없는 식: 괄호 짝이 맞지 않는 조각"
            chains.append(ch)
            continue
        lead, body = balanced
        start, end = m.start() + lead, m.start() + lead + len(body)
        ch = Chain(body, start, end)
        if not ch.has_operation:
            continue
        follows = text[end:end + 6]
        tail = follows[:1]
        line_start = text.rfind("\n", 0, start) + 1
        before = text[line_start:start]
        if _ARROW_BEFORE.search(before):
            ch.skip_reason = "등식 앞의 화살표(→)는 값의 변화 표시라 좌변을 등식의 시작으로 확정할 수 없음"
        elif _cut_operand_before(before, ctx.answer[line_start + len(before.rstrip(" \t\xa0")) - 1:][:1]):
            ch.skip_reason = "등식 앞에 연산자가 있어(단위에 끊긴 앞쪽 피연산자) 좌변을 확정할 수 없음"
        elif "/" in ch.segments[-1] and _LIMIT_AFTER.match(text, end):
            ch.skip_reason = "‘/’ 뒤 값에 이하·이상 같은 한도 표현이 붙어 나눗셈이 아니라 ‘값 / 한도’ 표기로 봄"
        elif _SPACED_KO_UNIT.match(follows):
            ch.skip_reason = "등식 뒤에 띄어 쓴 한글 수 단위(만·천·억)가 붙어 값을 확정할 수 없음"
        elif re.match(r"\s*\\?%", follows):
            # Preserve the supported ratio-to-percent presentation (a / b * 100 = n%).
            # An arbitrary numeric equality must not silently lose a percent suffix.
            percent_conversion = ("/" in ch.segments[0] and
                                  re.search(r"\*\s*100\s*$", ch.segments[0]))
            if not percent_conversion:
                ch.skip_reason = "퍼센트 값은 일반 숫자와 같지 않아 명시적인 백분율 환산식 없이 비교하지 않음"
        elif tail and (tail.isalnum() or tail == "§") and not any(
                follows.startswith(x) for x in _VALUE_SUFFIXES + MEASUREMENT_SUFFIXES):
            ch.skip_reason = "등식 뒤에 등록되지 않은 단위·수식어가 붙어 값을 확정할 수 없음"
        line_idx = next((i for i, l in enumerate(lines) if l.start <= start <= l.end), None)
        if not ch.skip_reason and line_idx is not None:
            marker = _negated(lines, line_idx, start, end)
            following = [l.text for l in lines[line_idx + 1:line_idx + 5] if l.text.strip()][:2]
            if marker:
                ch.skip_reason = f"부정·오류 예시 문맥 표현이 있음: {marker}"
            elif any(_CORRECTION_AFTER.search(t) for t in following):
                ch.skip_reason = "바로 뒤에 ‘올바른 식·정답’ 정정이 이어지는 오류 예시"
        if not ch.skip_reason:
            try:
                ch.values = [_evaluate(seg) for seg in ch.segments]
            except ZeroDivisionError:
                ch.skip_reason = "0으로 나누는 식"
            except (ValueError, RecursionError) as exc:
                ch.skip_reason = f"재계산할 수 없는 식: {exc}"
        if not ch.skip_reason:
            for a, b, sa, sb in zip(ch.values, ch.values[1:], ch.segments, ch.segments[1:]):
                literal = sb if re.fullmatch(rf"-?(?:{NUMBER})", sb) else (sa if re.fullmatch(rf"-?(?:{NUMBER})", sa) else None)
                tolerance = _tolerance_for(literal) if literal else Fraction(0)
                if abs(a - b) > tolerance:
                    ch.mismatch = (sa, sb)
                    break
        chains.append(ch)
    ctx.shared["chains"] = chains
    return chains


def _cut_operand_before(before: str, operator: str = "") -> bool:
    """좌변 바로 앞(같은 줄)에 단위에 끊긴 앞쪽 피연산자가 있는가 ('2벌 × 3 + 1 = 7' 의 '2벌 ×').

    ``before`` 는 등식 투영(×÷ → */, 강조 ** 는 공백)의 줄 앞부분, ``operator`` 는 그 연산자 자리의 답변 원문 글자다.
    연산자 바로 앞이 숫자(뒤에 공백 하나와 짧은 단위 허용)나 닫는 괄호일 때만 그렇다. 목록 표지만 있으면('1.', '1)') 아니다.
    원문 '*' 가 식에 바로 붙고 앞이 공백·구분자면 강조를 여는 표시다('설명 *3 + 4 = 8*', '[1] *3 …') — 이때만 대괄호·따옴표를
    단위로 보지 않는다. 그 밖의 연산자(곱셈 '*'·×·÷·+·-·/·TeX \times)는 따옴표·대괄호 단위도 단위다(2" * · 6" / · 2[kg] ×).
    문장부호(. , : ; ! ?)는 어느 경우에도 단위가 아니다('계산 1: +3 + 4 = 8' 의 라벨).
    """
    stripped = before.rstrip(" \t\xa0")
    if not stripped or stripped[-1] not in "+-*/":
        return False
    body = stripped[:-1]
    if re.fullmatch(r"\s*\d{1,3}[.)]\s*", body):
        return False                       # 목록 표지만 있음
    emphasis = operator == "*" and stripped == before and (not body or body[-1] in " \t\xa0([{:\"'“‘")
    excluded = r"\s\d+\-*/=().,:;!?" + (r"\[\]{}<>\"'“”‘’「」『』" if emphasis else "")
    return bool(re.search(r"[\d)](?:[ \t\xa0]?[^" + excluded + r"]{1,5})?[ \t\xa0]*$", body))


def _num(raw: str) -> Fraction:
    return Fraction(raw.replace(",", "").replace("−", "-"))


def _fmt(value: Fraction) -> Any:
    return _fraction_value(value)


# =====================================================================================
# 3-1 계산 결과 일치
# =====================================================================================

def _claimed_by_label(ctx: Ctx, labels: Sequence[str]) -> List[Tuple[str, int, int, str]]:
    scan = ctx.aa.inline_masked
    alt = "|".join(sorted((re.escape(l) for l in labels), key=len, reverse=True))
    found = []
    for m in re.finditer(rf"(?:{alt})\s*(?:은|는|이|가|:|=)?\s*", scan):
        if ctx.aa.in_fence(m.start()):
            continue
        clause_end = min([i for i in (scan.find(c, m.end()) for c in "\n") if i >= 0] + [len(scan)])
        sentence = re.split(r"(?<=[.!?。])\s", scan[m.end():clause_end])[0]
        if "=" in sentence:
            tail = sentence.rsplit("=", 1)[1]
            nm = re.search(rf"(?<![\w.,+−-])([+−-]?(?:{NUMBER}))", tail)
            base = m.end() + sentence.rfind("=") + 1
        else:
            nm = re.match(rf"\s*(?:약\s*)?([+−-]?(?:{NUMBER}))", sentence)
            base = m.end()
        if nm:
            if re.match(r"[*_]*\s*\\?%", scan[base + nm.end():]):
                continue  # 퍼센트는 질문의 계산 결과 숫자로 바꾸어 연결하지 않는다.
            raw = nm.group(1) if nm.groups() else nm.group(0)
            start = base + nm.start(nm.lastindex or 0)
            found.append((raw, start, start + len(raw), m.group(0).strip()))
    return found


def _question_calculation(ctx: Ctx) -> List:
    out = []
    for req in ctx.conds.of("calculation"):
        if req.status != "ok":
            skip_req(ctx, req, "3-1.question_calculation", SUB31)
            continue
        expected = Fraction(req.value["expected"])
        # Operand membership links the equation; its signs/operators are independently recomputed.
        q_numbers = {abs(_num(x)) for x in req.value["operands"]}
        claims = _claimed_by_label(ctx, req.value["labels"])
        link = "label"
        if not claims:
            link = "equation"
            linked = []
            for ch in _find_chains(ctx):
                if ch.skip_reason or ch.final_literal is None:
                    continue
                operands = {_num(x) for x in re.findall(NUMBER, ch.segments[0])}
                if len(operands) >= 2 and operands <= q_numbers | {Fraction(0), Fraction(1)}:
                    lit = ch.final_literal
                    linked.append((lit, ch.end - len(lit), ch.end, ch.segments[0]))
            claims = linked
        if not claims:
            whole = re.fullmatch(rf"\s*(?:약\s*)?([+−-]?(?:{NUMBER}))\s*(?:원|개|명|점)?\s*[.。]?\s*", ctx.aa.inline_masked)
            if whole:
                link = "whole_answer_number"
                claims = [(whole.group(1), whole.start(1), whole.end(1), "답변 전체")]
        values = sorted({_num(c[0]) for c in claims})
        if not claims:
            skip_condition(ctx, "3-1.question_calculation", SUB31,
                           "답변에서 계산 대상 값을 라벨·구조로 연결하지 못함 (기대값과 비슷한 숫자를 고르지 않음)")
            continue
        if len(values) > 1:
            skip_condition(ctx, "3-1.question_calculation", SUB31,
                           f"라벨·구조로 연결된 값이 여러 개({[_fmt(v) for v in values]})라 답변의 주장값을 확정하지 못함")
            continue
        raw, s, e, label = claims[0]
        claimed = _num(raw)
        ok = abs(claimed - expected) <= _tolerance_for(raw)
        out.append(result("3-1.question_calculation", SUB31, "질문 계산값", "question", Status.EXECUTED, verdict_of(ok),
                          required={"expected": _fmt(expected), "operation": req.value["operation"],
                                    "operands": req.value["operands"], "template": req.value["template"]},
                          measured={"claimed": _fmt(claimed), "link": link, "label": label},
                          comparison=f"{_fmt(claimed)} == {_fmt(expected)}",
                          reason=(f"질문에서 독립 계산한 값 {_fmt(expected)}과 답변 값이 일치" if ok else
                                  f"질문에서 독립 계산한 값은 {_fmt(expected)}, 답변은 {raw}"),
                          evidence=[q_evidence(ctx, req), Evidence("answer", s, e, excerpt(ctx.answer, s, e), label)]))
    return out


def _answer_equations(ctx: Ctx) -> List:
    out = []
    seen = set()
    skipped = []
    for ch in _find_chains(ctx):
        if ch.skip_reason:
            skipped.append({"expression": ch.text.strip(), "reason": ch.skip_reason})
            continue
        key = re.sub(r"\s+", "", ch.text)
        if key in seen:
            continue
        seen.add(key)
        ok = ch.mismatch is None
        shown = " = ".join(ch.segments)
        out.append(result(f"3-1.answer_equation:{len(seen)}", SUB31, f"답변 등식 {shown}", "answer_self",
                          Status.EXECUTED, verdict_of(ok), required={"recomputed": [_fmt(v) for v in ch.values]},
                          measured={"segments": ch.segments},
                          comparison=" == ".join(str(_fmt(v)) for v in ch.values), dedupe_key=f"eq|{key}",
                          reason=("답변의 산술 관계가 재계산과 일치 (질문 요구 충족 여부는 별도)" if ok else
                                  f"‘{ch.mismatch[0]}’과 ‘{ch.mismatch[1]}’의 재계산 값이 다름"),
                          evidence=[Evidence("answer", ch.start, ch.end, ctx.answer[ch.start:ch.end])]))
    if skipped:
        # 단위·부정·% 문맥 등으로 값을 확정할 수 없는 등식은 행을 만들지 않는다 (사례당 사유 한 건만 기록).
        listed = "; ".join(f"‘{item['expression'][:60]}’ — {item['reason']}" for item in skipped[:5])
        skip_condition(ctx, "3-1.answer_equation:skipped", SUB31,
                       f"단위·부정 문맥 등으로 값을 확정할 수 없는 등식 {len(skipped)}개 — 판정하지 않음: {listed}")
    return out


def check_3_1(ctx: Ctx) -> List:
    return _question_calculation(ctx) + _answer_equations(ctx)


# =====================================================================================
# 3-2 선언 개수 일치
# =====================================================================================

_NUM_ALT = "|".join(sorted(NUM_WORDS, key=len, reverse=True))
_DECL = re.compile(
    rf"(?:다음|아래|이하)(?:의|과\s*같은|와\s*같은)?\s*(?P<n>\d{{1,3}}|{_NUM_ALT})\s*(?:가지|개|항목|단계)(?![가-힣]*\s*중)"
    rf"|(?P<n2>\d{{1,3}}|{_NUM_ALT})\s*(?:가지|개)(?:의)?\s*(?:방법|항목|요소|단계|이유|특징|장점|단점|팁|원칙|포인트)?\s*(?:을|를|로|으로|는|은|가|이)?\s*"
    r"(?:다음과\s*같|아래와\s*같|소개|정리|제안|안내|살펴|알려|설명|있습니다|있어요)")
_PARTIAL = re.compile(r"\d+\s*(?:가지|개)[^\n]{0,6}?중(?:\s|$)|(?:^|\s)중\s*[^\n]{0,6}?(?:\d+|" + _NUM_ALT + r")\s*(?:가지|개)|예를\s*들어|예시로|그\s*중|일부|몇\s*가지")


def _next_structure(ctx: Ctx, decl_line) -> Optional[Dict[str, Any]]:
    aa = ctx.aa
    following = [l for l in aa.lines if l.number > decl_line.number and l.text.strip()]
    if not following:
        return None
    gap_lines = []
    for l in following:
        if l.in_code:
            return None
        if aa.list_items and any(i.line.number == l.number for i in aa.list_items):
            break
        if any(t.kind == "markdown" and t.start == l.start for t in aa.tables):
            break
        if any(h.line.number == l.number for h in aa.headings):
            break
        gap_lines.append(l)
        if len(gap_lines) > 1:
            return None
    first = following[len(gap_lines)] if len(gap_lines) < len(following) else None
    if first is None:
        return None
    first_item = next((i for i in aa.list_items if i.line.number == first.number), None)
    if first_item:
        item_lines = {i.line.number: i for i in aa.list_items}
        items = []
        heading_lines = {h.line.number for h in aa.headings}
        table_starts = {t.start for t in aa.tables if t.kind == "markdown"}
        blank_before = False
        for line in aa.lines[first.number - 1:]:
            if not line.text.strip():
                blank_before = True
                continue
            item = item_lines.get(line.number)
            if item is None:
                indent = len(line.text.expandtabs(4)) - len(line.text.expandtabs(4).lstrip())
                if indent > first_item.indent:
                    blank_before = False
                    continue  # 목록 항목 안의 설명·코드·하위 구조
                if (not blank_before and not line.in_code and line.number not in heading_lines and
                        line.start not in table_starts):
                    continue  # 빈 줄 없이 이어지는 목록 문단의 lazy continuation
                break
            if item.indent == first_item.indent:
                items.append(item)
            elif item.indent < first_item.indent:
                break
            blank_before = False
        return {"kind": "list", "count": len(items), "start": items[0].start, "end": items[-1].end,
                "lines": [i.line.number for i in items],
                "items": [{"text": i.line.text.strip(), "start": i.start, "end": i.end} for i in items]}
    table = next((t for t in aa.tables if t.kind == "markdown" and t.start == first.start), None)
    if table:
        return {"kind": "table_data_rows", "count": table.data_rows, "start": table.start, "end": table.end,
                "header_rows": table.header_rows,
                "items": [{"text": " | ".join(r), "start": None, "end": None} for r in table.rows]}
    heading = next((h for h in aa.headings if h.line.number == first.number), None)
    if heading:
        later = [h for h in aa.headings if h.line.number > heading.line.number]
        if later and later[0].level > heading.level:
            heading = later[0]   # 묶음 제목(## FAQ) 바로 아래 하위 제목이 실제 항목이다
        series = heading_series_from(aa.headings, heading)
        if series and series["ambiguous_boundary"]:
            return None
        if series and series["count"] >= 2:
            selected = series["items"]
            return {"kind": "headings", "count": len(selected), "start": selected[0].start,
                    "end": selected[-1].end, "excluded_next_heading": series["excluded_next_heading"],
                    "items": [{"text": h.line.text.strip(), "start": h.start, "end": h.end} for h in selected]}
        run = []
        for h in aa.headings:
            if h.line.number < heading.line.number:
                continue
            if h.level < heading.level:
                break
            if h.level == heading.level:
                run.append(h)
        return {"kind": "headings", "count": len(run), "start": run[0].start, "end": run[-1].end,
                "titles": [h.title for h in run],
                "items": [{"text": h.line.text.strip(), "start": h.start, "end": h.end} for h in run]}
    return None


def check_3_2(ctx: Ctx) -> List:
    out = []
    aa = ctx.aa
    masked = mask_ranges(ctx.answer, aa.code_exclusion_ranges)
    for idx, m in enumerate(_DECL.finditer(masked), start=1):
        raw = m.group("n") or m.group("n2")
        declared = parse_number(raw)
        if declared is None:
            continue
        line = next((l for l in aa.lines if l.start <= m.start() <= l.end), None)
        if line is None:
            continue
        sentence = line.text
        cid = f"3-2.declared_count:{idx}"
        ev = Evidence("answer", m.start(), m.end(), sentence.strip()[:100], "선언")
        if _PARTIAL.search(sentence):
            skip_condition(ctx, cid, SUB32, f"‘{m.group(0).strip()}’ — 부분집합·예시 소개 표현이라 전체 개수 선언으로 보지 않음")
            continue
        structure = _next_structure(ctx, line)
        if structure is None:
            skip_condition(ctx, cid, SUB32,
                           f"‘{m.group(0).strip()}’ — 선언 바로 뒤에 연결된 목록·표·소제목 구조가 없어 대응을 확정하지 못함")
            continue
        ok = structure["count"] == declared
        out.append(result(cid, SUB32, f"선언 개수 ‘{m.group(0).strip()}’", "answer_self", Status.EXECUTED,
                          verdict_of(ok), required={"declared": declared},
                          measured={"structure": structure["kind"], "count": structure["count"],
                                    "start": structure["start"], "end": structure["end"],
                                    **({"header_rows_excluded": structure["header_rows"]} if "header_rows" in structure else {})},
                          comparison=cmp_text(structure["count"], "==", declared),
                          details={"counted_items": structure.get("items", [])[:50],
                                   "excluded_next_heading": structure.get("excluded_next_heading"),
                                   "declaration_sentence": sentence.strip()},
                          reason=(f"선언 {declared}개와 연결 구조의 항목 {structure['count']}개가 일치" if ok else
                                  f"선언은 {declared}개, 연결된 {structure['kind']} 항목은 {structure['count']}개 (구조 경계는 선언과 독립으로 결정)"),
                          evidence=[ev, Evidence("answer", structure["start"], structure["end"],
                                                 ctx.answer[structure["start"]:structure["end"]][:120], structure["kind"])]))
    return out


# =====================================================================================
# 3-3 원본 보존·변환 일치
# =====================================================================================

def _category(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def same(a: Any, b: Any) -> bool:
    """자료형을 구분한 정확 비교: 0·false·빈 문자열·null·누락을 모두 다르게 본다."""
    ca, cb = _category(a), _category(b)
    if ca != cb:
        return False
    if ca == "number":
        return Fraction(str(a)) == Fraction(str(b))
    if ca == "array":
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if ca == "object":
        return set(a) == set(b) and all(same(a[k], b[k]) for k in a)
    return a == b


def _as_number(v: Any) -> Optional[Fraction]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return Fraction(str(v))
    if isinstance(v, str):
        m = re.fullmatch(rf"\s*(-?(?:{NUMBER}))\s*", v)
        return _num(m.group(1)) if m else None
    return None


def _get(item: Any, key: Optional[str]) -> Any:
    if key is None:
        return item
    if isinstance(item, dict):
        return item.get(key, _MISSING)
    return _MISSING


class _Missing:
    def __repr__(self) -> str:
        return "<누락>"


_MISSING = _Missing()


def _apply_ops(source: List[Any], ops: List[Dict[str, Any]]) -> Tuple[Optional[List[Any]], str]:
    data = list(source)
    for op in ops:
        kind = op["op"]
        if kind == "filter":
            threshold = _num(op["value"])
            kept = []
            for item in data:
                v = _as_number(_get(item, op["key"]))
                if v is None:
                    return None, f"필터 키 ‘{op['key']}’ 값이 숫자가 아닌 항목이 있어 비교값을 만들지 않음"
                if {"이상": v >= threshold, "이하": v <= threshold, "초과": v > threshold, "미만": v < threshold}[op["cmp"]]:
                    kept.append(item)
            data = kept
        elif kind == "dedupe":
            unique: List[Any] = []
            for item in data:
                if not any(same(item, u) for u in unique):
                    unique.append(item)
            data = unique
        elif kind == "sort":
            values = [_get(item, op["key"]) for item in data]
            if any(v is _MISSING for v in values):
                return None, f"정렬 키 ‘{op['key']}’ 가 없는 항목이 있음"
            nums = [_as_number(v) for v in values]
            if all(n is not None for n in nums):
                keyed = list(zip(nums, range(len(data)), data))
            elif all(isinstance(v, str) for v in values):
                keyed = list(zip(values, range(len(data)), data))
            else:
                return None, "정렬 값의 자료형이 섞여 있어 비교값을 만들지 않음"
            keyed.sort(key=lambda t: (t[0], t[1]), reverse=False)
            if op["direction"] == "desc":
                keyed = sorted(keyed, key=lambda t: t[0], reverse=True)
            data = [t[2] for t in keyed]
        elif kind == "rename":
            if not all(isinstance(i, dict) for i in data):
                return None, "키 이름 변경 대상이 객체가 아님"
            data = [{(op["to"] if k == op["from"] else k): v for k, v in i.items()} for i in data]
    return data, ""


def _answer_output(ctx: Ctx, source_kind: str) -> Tuple[Optional[List[Any]], str, str]:
    """(출력 자료, 출력 종류, 사유)."""
    parsed = ctx.shared.get("json_parsed", {})
    valid = [parsed[t["id"]]["value"] for t in json_targets(ctx) if parsed.get(t["id"], {}).get("status") == "ok"]
    if not valid:
        blocks = [b for b in ctx.aa.fenced_blocks if b["language_label"] in ("", "json")]
        for b in blocks:
            try:
                v = json.loads(b["content"])
            except (ValueError, RecursionError):
                continue
            if isinstance(v, list):
                valid.append(v)
        if not valid and not ctx.aa.fenced_blocks:
            try:
                v = json.loads(ctx.answer.strip())
                if isinstance(v, list):
                    valid.append(v)
            except (ValueError, RecursionError):
                pass
    if len(valid) == 1 and isinstance(valid[0], list):
        return valid[0], "json", ""
    if len(valid) > 1:
        return None, "", "답변에 JSON 배열 후보가 여러 개"
    tables = ctx.aa.tables
    if len(tables) == 1 and tables[0].headers:
        t = tables[0]
        return [dict(zip(t.headers, r)) for r in t.rows], "table", ""
    if len(tables) > 1:
        return None, "", "답변에 표가 여러 개"
    items = ctx.aa.top_level_items
    if items:
        return [i.body.strip() for i in items], "list", ""
    return None, "", "답변에서 변환 결과 구조(JSON 배열·표·목록)를 찾지 못함"


def _scalar_text(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(_fmt(Fraction(str(v))))
    return str(v).strip()


def _normalize_for_compare(items: List[Any], out_kind: str, src_kind: str) -> List[Any]:
    """표·목록 출력은 자료형이 문자열로만 남으므로 원본 값을 표시 문자열로 바꿔 비교한다."""
    if out_kind == "json" and src_kind == "json":
        return items
    norm = []
    for item in items:
        if isinstance(item, dict):
            norm.append({str(k).strip(): _scalar_text(v) for k, v in item.items()})
        else:
            norm.append(_scalar_text(item).strip("*` "))
    return norm


def check_3_3(ctx: Ctx) -> List:
    out = []
    src = ctx.conds.source_data
    # 추출 단계가 원본 없음·여러 개·코드 작성 질문의 조건을 만들지 않으므로 여기 오는 조건은 원본이 확정된 것뿐이다.
    # 연산을 적용할 수 없거나 답변에서 결과 구조(JSON 배열·표·목록)를 찾지 못하면 FAIL 이 아니라 행 없음(skipped)이다.
    for req in ctx.conds.of("transform"):
        if src is None:
            skip_condition(ctx, "3-3.transform", SUB33, "질문에서 원본 데이터를 확정하지 못함")
            continue
        expected, why = _apply_ops(src["value"], req.value["ops"])
        if expected is None:
            skip_condition(ctx, "3-3.transform", SUB33, why)
            continue
        output, out_kind, why = _answer_output(ctx, src["kind"])
        if output is None:
            skip_condition(ctx, "3-3.transform", SUB33, why)
            continue
        exp_n = _normalize_for_compare(expected, out_kind, src["kind"])
        out_n = _normalize_for_compare(output, out_kind, src["kind"])
        ordered = any(op["op"] == "sort" for op in req.value["ops"]) or req.value["preserve"]
        problems = []
        if len(exp_n) != len(out_n):
            problems.append(f"건수 불일치: 기대 {len(exp_n)}, 실제 {len(out_n)}")
        if ordered:
            for i, (a, b) in enumerate(zip(exp_n, out_n)):
                if not same(a, b):
                    problems.append(f"{i + 1}번째 항목 불일치: 기대 {json.dumps(a, ensure_ascii=False)[:80]}, "
                                    f"실제 {json.dumps(b, ensure_ascii=False)[:80]}")
                    break
        else:
            remaining = list(out_n)
            for a in exp_n:
                hit = next((j for j, b in enumerate(remaining) if same(a, b)), None)
                if hit is None:
                    problems.append(f"기대 항목 누락: {json.dumps(a, ensure_ascii=False)[:80]}")
                    break
                remaining.pop(hit)
            if remaining and not problems:
                problems.append(f"기대에 없는 항목: {json.dumps(remaining[0], ensure_ascii=False)[:80]}")
        ok = not problems
        out.append(result("3-3.transform", SUB33, "원본 보존·변환", "question", Status.EXECUTED, verdict_of(ok),
                          required={"ops": req.value["ops"], "preserve": req.value["preserve"], "ordered": ordered,
                                    "expected": exp_n[:50]},
                          measured={"output_kind": out_kind, "output": out_n[:50], "source_kind": src["kind"],
                                    "type_comparison": "자료형 구분 (JSON→JSON)" if out_kind == "json" and src["kind"] == "json"
                                    else "표시 문자열 비교 (표·목록은 자료형이 남지 않음)"},
                          comparison="출력 == 원본에 명시 연산 적용 결과" + (" (순서 포함)" if ordered else " (순서 무관)"),
                          reason="원본에 명시 연산을 적용한 결과와 일치" if ok else "; ".join(problems),
                          evidence=[q_evidence(ctx, req)]))
    for req in ctx.conds.of("unique_values"):
        output, out_kind, why = _answer_output(ctx, "json")
        if output is None:
            skip_condition(ctx, "3-3.unique_values", SUB33, why)
            continue
        dups = [i for i, v in enumerate(output) if any(same(v, w) for w in output[:i])]
        ok = not dups
        out.append(result("3-3.unique_values", SUB33, "명시 고유성 (중복 없음)", "question", Status.EXECUTED,
                          verdict_of(ok), required="출력 항목 중복 0개 (자료형 구분)",
                          measured={"items": len(output), "duplicate_indexes": dups[:20], "output_kind": out_kind},
                          comparison=cmp_text(len(dups), "==", 0),
                          reason="중복 항목 없음" if ok else f"중복 항목 {len(dups)}개", evidence=[q_evidence(ctx, req)]))
    for req in ctx.conds.of("aggregate"):
        if src is None:
            skip_condition(ctx, "3-3.aggregate", SUB33, "질문에서 원본 데이터를 확정하지 못함")
            continue
        values = [_as_number(_get(i, req.value["key"])) for i in src["value"]]
        if not values or any(v is None for v in values):
            skip_condition(ctx, "3-3.aggregate", SUB33, f"원본의 ‘{req.value['key']}’ 값이 모두 숫자가 아니어서 집계값을 만들지 않음")
            continue
        agg = req.value["agg"]
        expected = {"합계": sum(values), "총합": sum(values), "평균": sum(values) / len(values),
                    "최댓값": max(values), "최대값": max(values), "최솟값": min(values), "최소값": min(values)}[agg]
        claims = _claimed_by_label(ctx, [agg, "합계" if agg in ("합계", "총합") else agg])
        vals = sorted({_num(c[0]) for c in claims})
        if len(vals) != 1:
            skip_condition(ctx, "3-3.aggregate", SUB33, f"답변에서 집계 라벨({agg})로 연결된 값이 없거나 여러 개 ({len(vals)}개)")
            continue
        raw, s, e, label = claims[0]
        ok = abs(_num(raw) - expected) <= _tolerance_for(raw)
        out.append(result("3-3.aggregate", SUB33, f"집계 값 ({agg})", "question", Status.EXECUTED, verdict_of(ok),
                          required={"expected": _fmt(expected), "key": req.value["key"], "agg": agg},
                          measured={"claimed": raw}, comparison=f"{raw} == {_fmt(expected)}",
                          reason="원본 집계값과 일치" if ok else f"원본 {agg}는 {_fmt(expected)}, 답변은 {raw}",
                          evidence=[q_evidence(ctx, req), Evidence("answer", s, e, excerpt(ctx.answer, s, e), label)]))
    return out
