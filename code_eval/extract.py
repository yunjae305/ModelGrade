"""질문 원문에서 명시 조건을 코드로 추출한다 (LLM·사람 태그·기대조건표 없음).

규칙
- 질문 안의 인용 자료(펜스 코드, ``>`` 인용 줄, 표)는 지시가 아니라 작업 데이터다. 추출 대상 문장에서 가린다.
- 다른 따옴표 안에 들어 있는 지시(예: 번역할 문장 속의 "…를 포함해줘")는 실행하지 않는다.
- 실제 지시가 참조하는 값(``"완료"라는 문자열을 포함해줘`` 의 ``완료``)은 제거하지 않는다.
- 모호한 요구는 조건을 만들지 않고 ``unsupported`` 로 남긴다. 질문에 없는 값을 만들지 않는다.

수사 표(``_NUM_WORD``)와 출력 동사 지배 판별(``_count_governed_by_output_verb``)은 ``textparse`` 에 있다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from .analysis import TextAnalysis, mask_ranges
from .textparse import _NUM_WORD, _count_governed_by_output_verb

__all__ = ["Req", "QuestionConditions", "extract_conditions", "EXTRACT_VERSION",
           "parse_number", "NUM_WORDS"]

EXTRACT_VERSION = "v16-extract-1.8"

NUM_WORDS: Dict[str, int] = dict(_NUM_WORD)
NUM_WORDS.update({"열한": 11, "열두": 12, "열세": 13, "열네": 14, "열다섯": 15, "스무": 20})
_NUM_ALT = "|".join(sorted(NUM_WORDS, key=len, reverse=True))
NUM = rf"(?P<n>\d{{1,6}}|{_NUM_ALT})"
PRE = r"(?:(?P<pre>정확히|딱|최소한|최소|적어도|최대|많아야|약|대략)\s*)?"
POST = r"(?:\s*(?P<post>이상|이하|이내|미만|초과|까지|정도|내외|안팎|쯤|가량|넘지\s*않게))?"
NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"


def _pre(tag: str) -> str:
    return PRE.replace("?P<pre>", f"?P<pre_{tag}>")


def _num(tag: str) -> str:
    return NUM.replace("?P<n>", f"?P<n_{tag}>")


def _post(tag: str) -> str:
    return POST.replace("?P<post>", f"?P<post_{tag}>")


def _g(m: "re.Match", name: str) -> Optional[str]:
    """이름이 ``name`` 또는 ``name_태그`` 인 그룹 가운데 값이 있는 첫 그룹."""
    for key, value in m.groupdict().items():
        if value and (key == name or key.startswith(name + "_")):
            return value
    return None

_OPEN_Q = "\"“‘'「『"
_CLOSE_Q = "\"”’'」』"
LIT = rf"[{_OPEN_Q}](?P<lit>[^{_CLOSE_Q}\n]{{1,60}})[{_CLOSE_Q}]"


def parse_number(raw: str) -> Optional[int]:
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    return NUM_WORDS.get(raw)


def _cmp(pre: Optional[str], post: Optional[str], default: str = "==") -> Tuple[Optional[str], bool]:
    """(비교 연산자, 근사 표현 여부)."""
    approximate = pre in ("약", "대략") or post in ("정도", "내외", "안팎", "쯤", "가량")
    post = re.sub(r"\s+", "", post or "")
    if pre in ("정확히", "딱"):
        return "==", approximate
    if pre in ("최소", "최소한", "적어도") or post == "이상":
        return ">=", approximate
    if pre in ("최대", "많아야") or post in ("이하", "이내", "까지", "넘지않게"):
        return "<=", approximate
    if post == "미만":
        return "<", approximate
    if post == "초과":
        return ">", approximate
    return (default if default else None), approximate


@dataclass
class Req:
    kind: str
    value: Dict[str, Any]
    start: int
    end: int
    text: str
    status: str = "ok"                  # ok | unsupported
    reason: str = ""

    def evidence(self) -> Dict[str, Any]:
        return {"target": "question", "start": self.start, "end": self.end, "excerpt": self.text}


@dataclass
class QuestionConditions:
    reqs: List[Req] = field(default_factory=list)
    instruction_text: str = ""
    source_data: Optional[Dict[str, Any]] = None

    def of(self, *kinds: str) -> List[Req]:
        return [r for r in self.reqs if r.kind in kinds]

    def first(self, kind: str) -> Optional[Req]:
        found = self.of(kind)
        return found[0] if found else None

    def has(self, kind: str) -> bool:
        return any(r.kind == kind and r.status == "ok" for r in self.reqs)

    def to_list(self) -> List[Dict[str, Any]]:
        return [{"kind": r.kind, "value": r.value, "status": r.status, "reason": r.reason,
                 "evidence": r.evidence()} for r in self.reqs]


# --- 지시문 범위 -----------------------------------------------------------------

def _quote_spans(text: str) -> List[Tuple[int, int]]:
    """큰따옴표·꺾쇠 인용 구간 (작은따옴표는 값 표기에 흔해서 바깥 인용으로 보지 않는다)."""
    spans = []
    for open_c, close_c in (("“", "”"), ("「", "」"), ("『", "』")):
        for m in re.finditer(re.escape(open_c) + r"[^" + re.escape(close_c) + r"\n]*" + re.escape(close_c), text):
            spans.append((m.start(), m.end()))
    for m in re.finditer(r"\"[^\"\n]*\"", text):
        spans.append((m.start(), m.end()))
    return spans


def _nested(spans: List[Tuple[int, int]], start: int, end: int) -> bool:
    return any(s < start and end <= e and (s, e) != (start, end) for s, e in spans)


def _instruction_text(qa: TextAnalysis) -> str:
    """자료로 제시된 펜스 코드·인용 줄·표를 가린 질문 (위치 보존)."""
    ranges = list(qa.fence_ranges)
    for line in qa.lines:
        if not line.in_code and line.text.lstrip().startswith(">"):
            ranges.append((line.start, line.end))
    for t in qa.tables:
        if t.kind == "markdown":
            ranges.append((t.start, t.end))
    return mask_ranges(qa.text, ranges)


# --- 개수·분량 --------------------------------------------------------------------

_STRUCT_NOUN_BEFORE = re.compile(r"(?:코드\s*블록|코드블록|행|열|문장|문단|단락|줄|키|필드|컬럼|불릿|글머리\S*|소제목|섹션)\s*"
                                 r"(?:은|는|을|를|이|가|으로|로)?\s*(?:정확히|딱|최소|최대|적어도)?\s*$")
#: 답변 전체를 가리키는 대상 명사. 이 밖의 명사가 개수 앞에 붙으면 답변 일부(제목·설명 등)에 대한 요구로 본다.
WHOLE_NOUNS = {"답변", "답", "전체", "분량", "글", "본문", "요약", "응답", "회신", "칼럼", "문서", "내용", "결과",
               "안내", "소개", "소개문", "메일", "이메일", "후기", "초안", "공지", "공지문", "보고서", "설명문", "편지"}
_PART_TOPIC = re.compile(r"(?:제목|헤드라인|항목|챕터|섹션|단계|설명|문구|카드|이름|태그|슬로건|문단)\s*(?:은|는)")
_LIMIT_AFTER = re.compile(r"\s*(?:을|를)?\s*넘(?:기|지)?(?:지\s*(?:않|말|마)|면\s*안|어서는\s*안)")
_COND_AFTER = re.compile(r"\s*(?:이면|면|이라면|라면|일\s*때|(?:을|를)?\s*넘으면|이\s*넘으면)")
_VERBISH = re.compile(r"(?:여|서|고|게|해|하게|으로|로|도록|니까|므로|는데|지만|면서|꼭|반드시|딱|총|모두|전부|정확히|최대|최소|만)$")
_DEMONSTRATIVE = re.compile(r"(?:^|\s)(?:이|그|저|위|아래|다음|위의|아래의|다음의|이런|이러한)\s*$")


def _scope(text: str, start: int, end: int, kind: str) -> Tuple[str, str, Optional[str]]:
    """(scope, 사유, 비교 연산 보정). scope: whole | reference | per_item | part | conditional."""
    after = text[end:end + 12]
    op_fix = "<=" if _LIMIT_AFTER.match(after) else None
    if op_fix is None and _COND_AFTER.match(after):
        return "conditional", "조건·가정 표현(…이면)이라 요구로 보지 않음", None
    if re.match(r"\s*씩", after) or re.search(r"한\s*$", text[max(0, start - 2):start]):
        return "per_item", "항목마다 적용하는 개수(…씩)·범위 표현(한두)은 답변 전체 수로 대체하지 않음", None
    quotes = [(m.start(), m.end()) for m in re.finditer(r"[\"“‘'「『][^\"”’'」』\n]{0,60}[\"”’'」』]", text)]
    bounded = mask_ranges(text, quotes, fill="q")
    sent_start = max(bounded.rfind(c, 0, start) for c in ".!?\n") + 1
    clause = text[sent_start:start]
    if _DEMONSTRATIVE.search(clause):
        return "reference", "지시 표현(이·아래 …)으로 앞뒤에 나열한 대상을 가리킴 — 개수 요구가 아님", None
    if re.search(r"(?:^|\s)각(?:\s|각)|마다|별로", clause):
        return "per_item", "항목별 적용 범위(각·마다)는 지원하지 않음 — 답변 전체 수로 대체하지 않음", None
    if kind in ("list_count", "bullet_count", "heading_count", "literal"):
        return "whole", "", op_fix
    tokens = clause.split()
    last = tokens[-1] if tokens else ""
    if last and not _VERBISH.search(last):
        noun = re.sub(r"(?:은|는|을|를|이|가|의|에서|에|과|와|랑)$", "", last)
        rest = re.split(r"[.!?\n]", text[end:])[0]
        # "회의 시간을 한 문장으로 알려줘" — 목적어 + 출력 동사는 답변 전체. "추천을 한 문단으로 덧붙여"는 일부.
        whole_object = last.endswith(("을", "를")) and not re.search(r"덧붙|추가|곁들|붙여|달아", rest[:20])
        if not whole_object and (last.endswith(("에", "에서")) or noun not in WHOLE_NOUNS):
            return "part", f"적용 대상이 답변 일부(‘{noun}’)로 보임 — 답변 전체 측정으로 대체하지 않음", op_fix
    if not last or re.fullmatch(r"(?:꼭|반드시|딱|총|모두|전부|정확히|최대|최소)", last):
        prev_start = max(bounded.rfind(c, 0, max(0, sent_start - 1)) for c in ".!?\n") + 1
        if _PART_TOPIC.search(text[prev_start:sent_start]):
            return "part", "앞 문장의 주제가 답변 일부(제목·항목 등)라 적용 범위를 확정하지 못함", op_fix
    return "whole", "", op_fix

_COUNT_PATTERNS = (
    ("bullet_count", re.compile(
        rf"(?:불릿|블릿|글머리\s*기호|글머리표|bullet)(?:\s*포인트)?\s*(?:으로|로|을|를|은|는)?\s*{PRE}{NUM}\s*(?:개|가지|줄|항목)?{POST}")),
    ("bullet_count", re.compile(rf"{PRE}{NUM}\s*(?:개|가지)(?:의)?\s*(?:불릿|글머리)")),
    ("sentence_count", re.compile(rf"(?<![가-힣]){PRE}{NUM}\s*(?:개의?\s*)?문장{POST}")),
    ("paragraph_count", re.compile(rf"{PRE}{NUM}\s*(?:개의?\s*)?(?:문단|단락){POST}")),
    ("line_count", re.compile(rf"{PRE}{NUM}\s*줄{POST}")),
    ("word_count", re.compile(rf"{PRE}{NUM}\s*(?:개의?\s*)?(?:단어|어절){POST}")),
)

_CHAR_COUNT = re.compile(
    rf"{PRE}(?P<n>\d{{1,3}}(?:,\d{{3}})+|\d{{1,6}})\s*(?:자|글자)"
    r"(?=\s|$|[.,!?)\]'\"’”]|로|를|을|이|내|안|정|까|의|만|씩|내외)"
    r"(?:\s*(?P<post>이상|이하|이내|미만|초과|까지|정도|내외|안팎|쯤|가량|넘지\s*않게))?")
_WS_RULE = re.compile(r"공백\s*(?P<ws>제외|포함|빼고|빼서)")

_GENERIC_COUNT = re.compile(rf"{PRE}{NUM}\s*(?P<unit>개|가지)(?:의)?\s*(?:항목)?{POST}")

_DEFAULT_OP = {"bullet_count": "==", "sentence_count": "==", "paragraph_count": "==",
               "line_count": "==", "word_count": "==", "list_count": "=="}


def _count_reqs(text: str, out: List[Req]) -> None:
    taken: List[Tuple[int, int]] = []

    def overlaps(s, e):
        return any(s < te and ts < e for ts, te in taken)

    for kind, pattern in _COUNT_PATTERNS:
        for m in pattern.finditer(text):
            if overlaps(m.start(), m.end()):
                continue
            if kind == "line_count" and re.search(r"(?:불릿|글머리)\S*\s*$", text[max(0, m.start() - 8):m.start()]):
                continue
            if kind == "sentence_count" and re.match(r"\s*(?:으로부터|로부터|에서)", text[m.end():]):
                continue  # 주어진 문장 수이지 출력 문장 수가 아님
            n = parse_number(m.group("n"))
            if n is None:
                continue
            op, approx = _cmp(m.groupdict().get("pre"), m.groupdict().get("post"), _DEFAULT_OP[kind])
            taken.append((m.start(), m.end()))
            scope, why, op_fix = _scope(text, m.start(), m.end(), kind)
            if kind == "sentence_count" and scope == "part" and re.match(
                    r"\s*(?:으로|로)\s*(?:요약|작성|답|정리|말|써|적|고쳐)", text[m.end():]):
                scope, why = "whole", ""
            if scope in ("reference", "conditional"):
                continue
            req = Req(kind, {"op": op_fix or op, "n": n, "scope": scope}, m.start(), m.end(), m.group(0).strip())
            if approx:
                req.status, req.reason = "unsupported", "허용 범위가 명시되지 않은 근사 요구(정도·약 등)는 엄격 수치로 비교하지 않음"
            elif scope != "whole":
                req.status, req.reason = "unsupported", why
            out.append(req)

    for m in _CHAR_COUNT.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        n = int(m.group("n").replace(",", ""))
        op, approx = _cmp(m.groupdict().get("pre"), m.groupdict().get("post"), "")
        window = text[max(0, m.start() - 15):m.end() + 15]
        ws = _WS_RULE.search(window)
        exclude_ws = bool(ws and ws.group("ws") in ("제외", "빼고", "빼서"))
        taken.append((m.start(), m.end()))
        scope, why, op_fix = _scope(text, m.start(), m.end(), "char_count")
        if scope in ("reference", "conditional"):
            continue
        op = op_fix or op
        req = Req("char_count", {"op": op, "n": n, "exclude_whitespace": exclude_ws, "scope": scope},
                  m.start(), m.end(), m.group(0).strip())
        if scope != "whole" and not approx:
            req.status, req.reason = "unsupported", why
        elif approx:
            req.status, req.reason = "unsupported", "허용 범위가 명시되지 않은 근사 요구(정도·약 등)는 엄격 수치로 비교하지 않음"
        elif op is None:
            req.status, req.reason = "unsupported", "글자 수 비교 방식(정확히·이하·이상 등)이 명시되지 않음"
        out.append(req)

    if any(r.kind == "bullet_count" for r in out):
        return
    for m in _GENERIC_COUNT.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        if _STRUCT_NOUN_BEFORE.search(text[max(0, m.start() - 14):m.start()]):
            continue
        if _count_governed_by_output_verb(text, m.end()) is None:
            continue
        n = parse_number(m.group("n"))
        if n is None:
            continue
        op, approx = _cmp(m.group("pre"), m.group("post"), "==")
        scope, why, op_fix = _scope(text, m.start(), m.end(), "list_count")
        if scope in ("reference", "conditional"):
            continue
        numbered = bool(re.search(r"번호를?\s*(?:붙|매기|매겨|달)", text))
        req = Req("list_count", {"op": op_fix or op, "n": n, "unit": m.group("unit"), "numbered": numbered},
                  m.start(), m.end(), m.group(0).strip())
        if approx:
            req.status, req.reason = "unsupported", "근사 개수 요구(정도·약)는 엄격 비교하지 않음"
        elif scope != "whole":
            req.status, req.reason = "unsupported", why
        out.append(req)
        break


# --- 문자열 조건 -------------------------------------------------------------------

#: 정확 일치 수식어 — 있으면 공백·줄바꿈을 접지 않고 원문 그대로 대조한다 (G-1).
_EXACT = (r"정확히|그대로|똑같이|글자\s*그대로|토씨\s*하나(?:도)?\s*(?:안\s*틀리게|틀리지\s*않게)"
          r"|한\s*글자도\s*(?:안\s*틀리게|틀리지\s*않게|바꾸지\s*(?:말고|않고))")
_REQ_LIT = re.compile(
    rf"(?:(?P<exact_pre>{_EXACT})\s*)?"
    + LIT + r"\s*(?:라는|이라는|라고|이라고)?\s*(?:문자열|단어|표현|문구|말|문장|키워드|낱말)?\s*"
    r"(?:을|를|이|가|도|은|는)?\s*(?:꼭\s*|반드시\s*|항상\s*|절대\s*)?"
    rf"(?:(?P<exact_post>{_EXACT})\s*)?(?:꼭\s*|반드시\s*)?"
    r"(?P<verb>포함|넣|붙|적|쓰|써|사용|들어가|언급|출력)")
_NEG_AFTER = re.compile(r"^[^.!?\n]{0,6}?(?:지\s*(?:마|말|않)|면\s*안)")
_LIT_LIST = re.compile(rf"(?P<items>(?:[{_OPEN_Q}][^{_CLOSE_Q}\n]{{1,40}}[{_CLOSE_Q}]\s*(?:,|와|과|및|그리고)?\s*){{2,20}})"
                       r"(?P<tail>[^.!?\n]{0,40})")
_LIST_FORBID = re.compile(r"(?:쓰지|사용하지|넣지|포함하지|언급하지|적지)\s*(?:마|말|않)|금지|빼\s*(?:주|고)|쓰면\s*안")
_LIST_REQUIRE = re.compile(r"들어가|포함|넣어|써\s*(?:야|줘|주)|사용해")
_WITHOUT_LIT = re.compile(LIT + r"\s*(?:라는\s*\S+\s*)?(?:없이|빼고|제외하고)")
_BOUNDARY_LIT = re.compile(rf"(?:(?P<exact_pre>{_EXACT})\s*)?" + LIT
                           + rf"\s*(?:으로|로)\s*(?:(?P<exact_post>{_EXACT})\s*)?(?P<be>시작|끝|마무리|끝맺)")


def _exact(m) -> Dict[str, bool]:
    groups = m.groupdict()
    return {"exact": bool(groups.get("exact_pre") or groups.get("exact_post"))}
_CHARSET = re.compile(r"(?P<set>한글|영문|영어\s*알파벳|알파벳|대문자|소문자)\s*(?:문자)?\s*만\s*(?:사용|써|쓰|적|구성|으로)")
_EMPTY = re.compile(r"(?:아무것도|아무\s*것도|아무\s*말도)\s*(?:출력하지|쓰지|답하지|적지|말하지)\s*(?:마|말)"
                    r"|빈\s*(?:문자열|응답|출력|답변)\s*(?:으로|로|을|를|만)?\s*(?:출력|응답|반환|답|보내)")


def _literal_reqs(text: str, out: List[Req]) -> None:
    spans = _quote_spans(text)
    seen = set()

    def add(kind, m, extra=None):
        lit = m.group("lit").strip()
        if not lit or _nested(spans, m.start(), m.start() + len(m.group(0))):
            return
        key = (kind, lit)
        if key in seen:
            return
        seen.add(key)
        out.append(Req(kind, {"literal": lit, **(extra or {})}, m.start(), m.end(), m.group(0).strip()))

    for m in _BOUNDARY_LIT.finditer(text):
        scope, why, _ = _scope(text, m.start(), m.end(), "literal")
        if scope == "per_item":
            out.append(Req("starts_with" if m.group("be") == "시작" else "ends_with",
                           {"literal": m.group("lit").strip(), **_exact(m)},
                           m.start(), m.end(), m.group(0).strip(), status="unsupported", reason=why))
            seen.add(("boundary", m.start()))
            continue
        add("starts_with" if m.group("be") == "시작" else "ends_with", m, _exact(m))
    for m in _REQ_LIT.finditer(text):
        if any(r.start == m.start() for r in out if r.kind in ("starts_with", "ends_with")):
            continue
        negated = bool(_NEG_AFTER.search(text[m.end():m.end() + 12]))
        kind = "forbidden_literal" if negated else "required_literal"
        scope, why, _ = _scope(text, m.start(), m.end(), "literal")
        if scope == "per_item" and kind == "required_literal":
            out.append(Req(kind, {"literal": m.group("lit").strip(), **_exact(m)}, m.start(), m.end(),
                           m.group(0).strip(), status="unsupported", reason=why))
            continue
        add(kind, m, _exact(m))
    for m in _WITHOUT_LIT.finditer(text):
        add("forbidden_literal", m, {"exact": False})
    for m in _LIT_LIST.finditer(text):
        tail = m.group("tail")
        if re.search(r"각\s*(?:\S+\s*)?(?:문단|항목|섹션)(?:에|마다)", tail):
            continue   # 문단·항목마다 들어가야 하는 요구는 답변 전체 출현으로 대체하지 않음
        kind = "forbidden_literal" if _LIST_FORBID.search(tail) else (
            "required_literal" if _LIST_REQUIRE.search(tail) else None)
        if kind is None:
            continue
        scope, why, _ = _scope(text, m.start(), m.end(), "literal")
        per_item = scope == "per_item" and kind == "required_literal"
        # 나열 앞뒤의 '정확히·그대로' 는 나열 전체에 걸린다.
        exact = bool(re.search(_EXACT, tail) or re.search(_EXACT, text[max(0, m.start() - 14):m.start()]))
        for q in _QUOTED.finditer(m.group("items")):
            lit = q.group("v").strip()
            if per_item:
                out.append(Req(kind, {"literal": lit, "from_list": True, "exact": exact}, m.start() + q.start(), m.end(),
                               m.group(0).strip(), status="unsupported", reason=why))
                continue
            qs, qe = m.start() + q.start(), m.start() + q.end()
            if lit and (kind, lit) not in seen and not _nested(spans, qs, qe):
                seen.add((kind, lit))
                out.append(Req(kind, {"literal": lit, "from_list": True, "exact": exact}, qs, m.end(), m.group(0).strip()))


# --- 형식·구성 ---------------------------------------------------------------------

_TABLE_WORD = r"(?:(?<![가-힣])표|(?:시간|일정|비교|요약|정리|계획|진도|점검)표)"
_TABLE_REQ = re.compile(_TABLE_WORD + r"\s*(?:로|를|형식으로|형태로|형식|형태)\s*[^.!?\n]{0,15}?"
                        r"(?:정리|작성|만들|그려|나타내|보여|출력|알려|구성|제시|줘|주세요)")
_TABLE_COUNT_REQ = re.compile(_TABLE_WORD + r"\s*(?P<n>하나|둘|셋|\d+\s*개)\s*(?:로만|로|만)\s*[^.!?\n]{0,15}?"
                              r"(?:정리|작성|만들|그려|나타내|보여|출력|알려|구성|제시)")
_TABLE_ONLY_REQ = re.compile(_TABLE_WORD + r"\s*만\s*(?:출력|보여|작성|정리|제시)")
_TABLE_NEG = re.compile(_TABLE_WORD + r"\s*(?:는|를|은|로|으로|형식으로|형태로)?\s*(?:쓰지|사용하지|만들지|넣지|그리지|정리하지|작성하지)\s*(?:마|말|않)")
_LIST_REQ = re.compile(r"(?P<kind>번호\s*목록|번호를\s*매겨|번호를\s*붙여|목록|리스트|글머리\s*기호|글머리표|불릿|블릿)\s*"
                       r"(?:으로|로|형식|형태|을\s*사용|를\s*사용|을\s*써|를\s*달아)")
_LIST_NEG = re.compile(r"(?:목록|리스트|불릿|글머리\S*)\s*(?:은|는|을|를|으로|로|형태로|형식으로)?\s*"
                       r"(?:쓰지|사용하지|만들지|나열하지|정리하지|작성하지)\s*(?:마|말|않)")
_MARKDOWN_NEG = re.compile(r"마크다운[^.!?\n]{0,30}?(?:쓰지|사용하지|넣지)\s*(?:마|말|않)|순수\s*텍스트로만|마크다운\s*(?:을|를)?\s*지원하지\s*않")
_HEADING_REQ = re.compile(r"(?:소제목|제목|섹션|헤딩)\s*(?:을|를|도)?\s*(?:붙|달|넣|나눠|구분|사용)")
_HEADING_COUNT = re.compile(rf"{_pre('a')}{_num('a')}\s*개의?\s*(?:소제목|섹션)|(?:소제목|섹션)\s*(?:을|를)?\s*{_pre('b')}{_num('b')}\s*개")
#: '배열·나열·진열' 안의 '열'은 표의 열이 아니다 — 앞 글자가 한글이면 열 키워드로 보지 않는다.
_COLUMN_KW = re.compile(r"(?:(?<![가-힣])열|컬럼|칼럼|column)(?:\s*이름|\s*명)?\s*(?:은|는|을|를|이|가|:)?")
_UNQUOTED_COLUMN_LIST = re.compile(r"(?:(?<![가-힣])열|컬럼|칼럼)\s*(?:은|는)\s*(?P<names>[^.!?\n]{3,120}?)\s+(?P<count>\d+)\s*개(?:입니다|예요|다|요)?")
_QUOTED = re.compile(rf"[{_OPEN_Q}](?P<v>[^{_CLOSE_Q}\n]{{1,40}})[{_CLOSE_Q}]")
_COL_COUNT = re.compile(rf"{_pre('a')}{_num('a')}\s*(?:개의?\s*)?(?:열|컬럼|칼럼)(?:로|으로|이|을|를)?(?![가-힣])|(?:(?<![가-힣])열|컬럼)\s*(?:은|는|을|를)?\s*{_pre('b')}{_num('b')}\s*개")
_HEADER_NONE = re.compile(r"헤더\s*(?:행)?\s*(?:없이|는\s*빼고|를\s*빼고|를\s*넣지\s*말)|머리글\s*(?:행)?\s*없이")
_HEADER_YES = re.compile(r"(?:헤더|머리글)\s*(?:행)?\s*(?:을|를)?\s*(?:포함|넣어|두고|달아|붙여|있게)")
_ROWS_DATA = (
    re.compile(rf"(?:헤더\s*(?:행)?\s*(?:을|를)?\s*제외(?:한|하고)\s*)?데이터\s*행\s*(?:은|을|이|는)?\s*{PRE}{NUM}\s*(?:개|행){POST}"),
    re.compile(rf"헤더\s*(?:행)?\s*(?:을|를)?\s*제외(?:한|하고)\s*(?:행\s*(?:은|을|이)?\s*)?{PRE}{NUM}\s*(?:개\s*)?(?:행|개){POST}"),
    re.compile(rf"데이터\s*{PRE}{NUM}\s*행{POST}"),
    re.compile(rf"{PRE}{NUM}\s*개의\s*데이터\s*행"),
)
_ROWS_TOTAL = (
    re.compile(rf"헤더\s*(?:행)?\s*(?:을|를)?\s*포함(?:한|해서|하여)?\s*(?:전체\s*)?(?:행\s*(?:은|을|이)?\s*)?{PRE}{NUM}\s*(?:개\s*)?행{POST}"),
    re.compile(rf"전체\s*(?:행\s*(?:은|을|이)?\s*)?{PRE}{NUM}\s*(?:개\s*)?행{POST}"),
)
_ROWS_AMBIG = re.compile(rf"(?<![가-힣]){_pre('a')}{_num('a')}\s*행(?=\s|$|[.,!?]|으로|로|이|을|를|은|는|짜리|의)|행\s*(?:은|을|이)\s*{_pre('b')}{_num('b')}\s*개")

_JSON_REQ = re.compile(r"JSON\s*(?:으로|형식|형태|만|객체|배열|로)", re.IGNORECASE)
_JSON_ARRAY = re.compile(r"JSON\s*배열|배열\s*형태의?\s*JSON|JSON\s*array", re.IGNORECASE)
_JSON_OBJECT = re.compile(r"JSON\s*객체|JSON\s*object", re.IGNORECASE)
_KEY_KW = re.compile(r"(?:키|필드|속성|key|field)(?:\s*이름)?(?:는|은|로|:|를|을)?", re.IGNORECASE)
_TYPE_WORDS = {"숫자": "number", "수": "number", "정수": "integer", "실수": "number", "문자열": "string",
               "문자": "string", "불리언": "boolean", "boolean": "boolean", "참/거짓": "boolean",
               "배열": "array", "리스트": "array", "객체": "object", "null": "null"}
_FIELD_TYPE = re.compile(LIT + r"\s*(?:필드|키)?\s*(?:은|는|의\s*값은|값은|:)\s*(?P<t>숫자|정수|실수|문자열|불리언|boolean|참/거짓|배열|리스트|객체|null)"
                         r"(?:\s*(?:형|타입|자료형|값))?", re.IGNORECASE)

_CODE_BLOCK_WORD = r"(?:코드\s*블록|코드블록|코드\s*블럭|코드블럭|```)"
_CODE_BLOCK_REQ = re.compile(_CODE_BLOCK_WORD + r"\s*(?:으로|로|에|을|를|안에|형식)")
_CODE_BLOCK_NEG = re.compile(_CODE_BLOCK_WORD + r"\s*(?:은|을|는|를)?\s*(?:쓰지|사용하지|넣지)\s*(?:마|말)")
_CODE_BLOCK_COUNT = re.compile(_CODE_BLOCK_WORD + rf"\s*(?:을|를|은|는|으로|로)?\s*{_pre('a')}{_num('a')}\s*개{_post('a')}"
                               rf"|{_pre('b')}{_num('b')}\s*개의?\s*" + _CODE_BLOCK_WORD)
_LANGS = {"python": "python", "파이썬": "python", "py": "python", "javascript": "javascript",
          "자바스크립트": "javascript", "js": "javascript", "typescript": "typescript", "ts": "typescript",
          "java": "java", "자바": "java", "sql": "sql", "bash": "bash", "shell": "bash", "json": "json",
          "html": "html", "css": "css", "go": "go", "rust": "rust", "c++": "cpp", "cpp": "cpp"}
LANG_ALIASES = {"python": {"python", "py", "python3"}, "javascript": {"javascript", "js"},
                "typescript": {"typescript", "ts"}, "bash": {"bash", "sh", "shell", "zsh"},
                "cpp": {"cpp", "c++"}, "json": {"json"}, "java": {"java"}, "sql": {"sql"},
                "html": {"html"}, "css": {"css"}, "go": {"go", "golang"}, "rust": {"rust", "rs"}}
_LANG_ALT = "|".join(sorted((re.escape(k) for k in _LANGS), key=len, reverse=True))
_LANG_LABEL = re.compile(rf"(?P<lang>{_LANG_ALT})\s*" + _CODE_BLOCK_WORD, re.IGNORECASE)
_LANG_LABEL_KW = re.compile(rf"언어\s*(?:표지|태그|이름|표시)[^.!?\n]{{0,20}}?(?P<lang>{_LANG_ALT})"
                            rf"|(?P<lang2>{_LANG_ALT})[^.!?\n]{{0,10}}?언어\s*(?:표지|태그|이름|표시)", re.IGNORECASE)
_PY_REQ = re.compile(r"(?:python|파이썬)\s*(?:코드|스크립트|함수|프로그램)", re.IGNORECASE)
_CODE_REQ = re.compile(r"(?:코드|프로그램|스크립트|함수)\s*(?:를|을|도|는)?\s*(?:[^.!?\n]{0,8}?)(?:작성|만들|짜|보여)")
_ERROR_EXAMPLE_REQ = re.compile(r"(?:잘못된|틀린|오류가\s*있는|에러가\s*나는|깨진|invalid)\s*(?:JSON|코드|예시|예제)", re.IGNORECASE)


def _format_reqs(text: str, out: List[Req]) -> None:
    def add(kind, m, value=None, **kw):
        out.append(Req(kind, value or {}, m.start(), m.end(), m.group(0).strip(), **kw))

    m = _TABLE_NEG.search(text)
    if m:
        add("table_forbidden", m)
    else:
        m = _TABLE_REQ.search(text) or _TABLE_COUNT_REQ.search(text) or _TABLE_ONLY_REQ.search(text)
        if m:
            add("table_required", m)
        count_match = _TABLE_COUNT_REQ.search(text)
        if count_match:
            raw = count_match.group("n").replace(" ", "")
            n = {"하나": 1, "둘": 2, "셋": 3}.get(raw)
            if n is None:
                n = parse_number(raw.removesuffix("개"))
            if n is not None:
                add("table_count", count_match, {"op": "==", "n": n})
    if not any(r.kind == "table_forbidden" for r in out):
        cols = _columns(text)
        if cols:
            out.append(cols)
        else:
            unquoted = _UNQUOTED_COLUMN_LIST.search(text)
            if unquoted:
                names = [part.strip() for part in unquoted.group("names").split(",")]
                n = int(unquoted.group("count"))
                out.append(Req("table_column_count", {"op": "==", "n": n}, unquoted.start(),
                               unquoted.end(), unquoted.group(0).strip()))
                if len(names) == n and all(re.fullmatch(r"[가-힣A-Za-z0-9 _/\-]{1,30}", name) for name in names):
                    out.append(Req("table_columns", {"names": names, "ordered": False},
                                   unquoted.start(), unquoted.end(), unquoted.group(0).strip()))
                else:
                    out.append(Req("table_columns", {}, unquoted.start(), unquoted.end(),
                                   unquoted.group(0).strip(), status="unsupported",
                                   reason="따옴표 없는 열 이름 목록의 경계를 확정하지 못함"))
        m = _COL_COUNT.search(text)
        if m and not cols:
            n = parse_number(_g(m, "n") or "")
            op, _ = _cmp(_g(m, "pre"), None, "==")
            if n is not None:
                add("table_column_count", m, {"op": op, "n": n})
        m = _HEADER_NONE.search(text)
        if m:
            add("table_header", m, {"header_rows": 0})
        else:
            m = _HEADER_YES.search(text)
            if m:
                add("table_header", m, {"header_rows_min": 1})
        _row_reqs(text, out)

    m = _LIST_NEG.search(text)
    if m:
        add("list_forbidden", m)
    else:
        m = _LIST_REQ.search(text)
        if m:
            k = m.group("kind")
            style = "numbered" if "번호" in k else ("bullet" if any(w in k for w in ("불릿", "블릿", "글머리")) else "any")
            add("list_required", m, {"style": style})
    m = _HEADING_COUNT.search(text)
    if m:
        n = parse_number(_g(m, "n") or "")
        op, _ = _cmp(_g(m, "pre"), None, "==")
        scope, why, _fix = _scope(text, m.start(), m.end(), "heading_count")
        if n is not None and scope not in ("reference", "conditional"):
            add("heading_count", m, {"op": op, "n": n}, **({"status": "unsupported", "reason": why}
                                                          if scope != "whole" else {}))
    else:
        m = _HEADING_REQ.search(text)
        if m:
            add("heading_required", m)

    m = _JSON_REQ.search(text)
    if m:
        top = "array" if _JSON_ARRAY.search(text) else ("object" if _JSON_OBJECT.search(text) else None)
        add("json_required", m, {"top_level": top})
        keys, types = _json_fields(text)
        if keys:
            out.append(keys)
        out.extend(types)

    m = _CODE_BLOCK_NEG.search(text)
    if m:
        add("code_block_forbidden", m)
    else:
        m = _CODE_BLOCK_COUNT.search(text)
        if m:
            n = parse_number(_g(m, "n") or "")
            op, approx = _cmp(_g(m, "pre"), _g(m, "post"), "==")
            if n is not None:
                add("code_block_count", m, {"op": op, "n": n},
                    status="unsupported" if approx else "ok",
                    reason="근사 개수 요구는 엄격 비교하지 않음" if approx else "")
        m2 = _CODE_BLOCK_REQ.search(text)
        if m2 and not m:
            add("code_block_required", m2)
        m3 = _LANG_LABEL_KW.search(text) or _LANG_LABEL.search(text)
        if m3:
            lang = (m3.groupdict().get("lang") or m3.groupdict().get("lang2")).lower()
            add("code_language_label", m3, {"language": _LANGS.get(lang, lang)})
    m = _PY_REQ.search(text)
    if m:
        add("python_code_requested", m)
    m = _CODE_REQ.search(text)
    if m:
        add("code_requested", m)
    m = _ERROR_EXAMPLE_REQ.search(text)
    if m:
        add("error_example_requested", m)


# --- 코드·데이터 결과물 안의 구조 --------------------------------------------------------
# "앱 설정을 YAML로 작성해줘. 지원 언어는 ko와 en의 목록으로 넣어줘" 의 목록은 YAML 안의 자료 구조다.
# 코드·데이터 결과물을 요구한 질문에서 결과물 안을 가리키는 목록·표·제목·항목 수 요구는 Markdown 구조로 판정하지 않는다.

_FORMAT_NAME = (r"(?<![A-Za-z])(?:YAML|YML|JSON|CSV|TSV|XML|TOML|INI|HTML|CSS|SQL|LaTeX|Python|JavaScript|TypeScript|"
                r"Java|Kotlin|Swift|Golang|Rust|C\+\+|C#|PHP|Ruby|Bash|Shell|PowerShell|Dockerfile|regex)(?![A-Za-z])"
                r"|파이썬|자바스크립트|타입스크립트|자바|코틀린|스위프트|러스트|파워셸|셸|쉘|배시|정규\s*표현식|정규식|엑셀\s*(?:수식|함수)")
_FORMAT_NAME_RE = re.compile(_FORMAT_NAME, re.IGNORECASE)
_ARTIFACT = r"(?:코드|함수|스크립트|프로그램|쿼리|클래스|메서드|설정\s*파일|설정|파일|문서|페이지|예제|예시|배열|객체|형식|형태)"
_ARTIFACT_RE = re.compile(r"코드|함수|스크립트|프로그램|쿼리|클래스")
#: 결과물 형식 요구. ``CSV로 저장된 자료`` 처럼 입력 자료를 설명하는 표현은 제외한다.
_OUTPUT_FORMAT_REQ = re.compile(
    rf"(?:{_FORMAT_NAME})\s*{_ARTIFACT}?\s*(?:으로|로)(?!\s*(?:된|저장된|작성된|되어|돼|만든|만들어진|받은|주어진))"
    rf"[^.!?\n]{{0,60}}?(?:작성|만들|짜|구현|생성|변환|바꿔|바꾸|표현|출력|보여|나타내)"
    rf"|(?:{_FORMAT_NAME})\s*{_ARTIFACT}\s*(?:을|를|도)?[^.!?\n]{{0,15}}?(?:작성|만들|짜|구현|생성|보여)"
    rf"|(?:코드|함수|스크립트|프로그램|쿼리|클래스)\s*(?:을|를|도|는)?[^.!?\n]{{0,12}}?(?:작성|만들|짜|구현|보여)",
    re.IGNORECASE)
#: Markdown 에만 쓰는 구조 표현 — 결과물을 요구한 질문에서도 답변 구성 요구로 판정한다.
_MARKDOWN_ONLY = re.compile(r"불릿|블릿|글머리|번호\s*목록|번호를|마크다운|Markdown", re.IGNORECASE)
_CLAUSE_BOUNDARY = re.compile(r"[.!?\n,，;]|(?<=[가-힣])(?:고|며|면서|지만|는데)\s")
_DATA_VERB_AFTER = re.compile(r"[^.!?\n,]{0,20}?(?:넣|담|반환|리턴|저장|지정|할당|선언|전달|받)")
_DATA_TYPE_WORD = re.compile(r"정수|실수|불리언|boolean|문자열|자료형|딕셔너리|배열|튜플|필드|변수", re.IGNORECASE)
_PROSE_VERB_AFTER = re.compile(r"[^.!?\n]{0,20}?(?:정리|나열|요약|비교|설명|구분|보여|알려|제시|그려|나타내|작성|써|적어)")
_FORMAT_STRUCT_KINDS = ("list_required", "list_forbidden", "list_count", "heading_required", "heading_count",
                        "table_required", "table_forbidden", "table_count", "table_columns", "table_column_count",
                        "table_header", "table_data_rows", "table_total_rows", "table_rows_ambiguous")
_STRUCT_LABEL = {"list_required": "목록", "list_forbidden": "목록", "list_count": "항목 수",
                 "heading_required": "제목", "heading_count": "제목"}


def _format_label(s: str) -> str:
    m = _FORMAT_NAME_RE.search(s) or _ARTIFACT_RE.search(s)
    return m.group(0) if m else "코드"


def _clause(text: str, start: int, end: int) -> Tuple[int, int]:
    cs, ce = 0, len(text)
    for b in _CLAUSE_BOUNDARY.finditer(text):
        if b.end() <= start:
            cs = b.end()
        elif b.start() >= end:
            ce = b.start()
            break
    return cs, ce


def _structure_target(text: str, req: Req, out_req: Optional["re.Match"]) -> Optional[Tuple[str, str]]:
    """구조 요구가 가리키는 대상. None = 답변의 Markdown 구조, ("data"|"ambiguous", 형식 이름) = 판정하지 않음."""
    if _MARKDOWN_ONLY.search(req.text) or _MARKDOWN_ONLY.search(text[max(0, req.start - 8):req.start]):
        return None
    attached = re.search(rf"(?:{_FORMAT_NAME})\s*(?:의\s*)?$", text[max(0, req.start - 16):req.start], re.IGNORECASE)
    if attached:                                   # "파이썬 리스트로", "HTML 표로"
        return "data", attached.group(0).strip()
    cs, ce = _clause(text, req.start, req.end)
    local = _OUTPUT_FORMAT_REQ.search(text[cs:ce])
    if local:                                      # "결과를 리스트로 반환하는 파이썬 함수를 작성해줘"
        return "data", _format_label(local.group(0))
    if out_req is None:
        return None
    # 요구 표현 자체에 동사가 들어 있을 수 있다 ("표로 정리") — 요구 시작부터 절 끝까지 본다.
    fmt, after = _format_label(out_req.group(0)), text[req.start:ce]
    if _DATA_VERB_AFTER.match(after) or _DATA_TYPE_WORD.search(text[cs:ce]):
        return "data", fmt                         # "지원 언어는 ko와 en의 목록으로 넣어줘"
    if _PROSE_VERB_AFTER.match(after):
        return None                                # "장단점은 표로 정리해줘" — 설명 부분의 구성
    return "ambiguous", fmt


def _format_internal_structure(text: str, reqs: List[Req]) -> None:
    out_req = _OUTPUT_FORMAT_REQ.search(text)
    targets = [r for r in reqs if r.kind in _FORMAT_STRUCT_KINDS and r.status == "ok"]
    anchor = next((r for r in targets if r.kind in ("table_required", "table_count", "table_forbidden")), None)
    anchor_target = _structure_target(text, anchor, out_req) if anchor else None
    for r in targets:
        found = anchor_target if anchor and r.kind.startswith("table_") else _structure_target(text, r, out_req)
        if found is None:
            continue
        where, fmt = found
        label = _STRUCT_LABEL.get(r.kind, "표")
        r.status = "unsupported"
        r.reason = (f"요청한 {fmt} 결과물 안의 자료 구조를 가리키는 요구로 보임 — 답변의 Markdown {label} 기준으로 판정하지 않음"
                    if where == "data" else
                    f"{fmt} 결과물을 요청한 질문이라 결과물 안의 구조인지 답변 구성인지 확정하지 못함 — "
                    f"Markdown {label} 기준으로 판정하지 않음")


def _columns(text: str) -> Optional[Req]:
    for m in _COLUMN_KW.finditer(text):
        clause_end = min([i for i in (text.find(c, m.end()) for c in ".!?\n") if i >= 0] + [len(text)])
        clause = text[m.end():clause_end]
        names = [q.group("v").strip() for q in _QUOTED.finditer(clause)]
        if len(names) >= 1 and not re.match(r"^\s*(?:은|는)?\s*\d", clause):
            ordered = bool(re.search(r"순서", text[max(0, m.start() - 10):clause_end]))
            return Req("table_columns", {"names": names, "ordered": ordered}, m.start(), clause_end,
                       text[m.start():clause_end].strip())
    # 'A', 'B' 열
    m = re.search(rf"((?:{LIT}\s*(?:,|와|과|및)?\s*){{1,12}})(?:열|컬럼)(?:로|으로|을|를|이|가)?", text)
    if m:
        names = [q.group("v").strip() for q in _QUOTED.finditer(m.group(1))]
        if names:
            ordered = bool(re.search(r"순서", text[max(0, m.start() - 10):m.end() + 10]))
            return Req("table_columns", {"names": names, "ordered": ordered}, m.start(), m.end(), m.group(0).strip())
    return None


def _row_reqs(text: str, out: List[Req]) -> None:
    taken = []
    for pattern in _ROWS_DATA:
        for m in pattern.finditer(text):
            n = parse_number(m.group("n"))
            if n is None:
                continue
            op, approx = _cmp(m.group("pre"), m.groupdict().get("post"), "==")
            taken.append((m.start(), m.end()))
            out.append(Req("table_data_rows", {"op": op, "n": n}, m.start(), m.end(), m.group(0).strip(),
                           status="unsupported" if approx else "ok",
                           reason="근사 요구는 엄격 비교하지 않음" if approx else ""))
            break
        if taken:
            break
    for pattern in _ROWS_TOTAL:
        m = pattern.search(text)
        if m and not any(s <= m.start() < e for s, e in taken):
            n = parse_number(m.group("n"))
            if n is not None:
                op, approx = _cmp(m.group("pre"), m.groupdict().get("post"), "==")
                taken.append((m.start(), m.end()))
                out.append(Req("table_total_rows", {"op": op, "n": n}, m.start(), m.end(), m.group(0).strip(),
                               status="unsupported" if approx else "ok",
                               reason="근사 요구는 엄격 비교하지 않음" if approx else ""))
                break
    for m in _ROWS_AMBIG.finditer(text):
        if any(s <= m.start() < e or s < m.end() <= e for s, e in taken):
            continue
        n = parse_number(_g(m, "n") or "")
        if n is None:
            continue
        out.append(Req("table_rows_ambiguous", {"n": n}, m.start(), m.end(), m.group(0).strip(),
                       status="unsupported",
                       reason="헤더 포함 여부가 명시되지 않은 행 수 — 데이터·전체 행 중 하나로 임의 해석하지 않음"))
        break


def _json_fields(text: str) -> Tuple[Optional[Req], List[Req]]:
    types: List[Req] = []
    typed_keys: List[str] = []
    for m in _FIELD_TYPE.finditer(text):
        t = _TYPE_WORDS.get(m.group("t").lower(), _TYPE_WORDS.get(m.group("t")))
        if t:
            typed_keys.append(m.group("lit").strip())
            types.append(Req("json_field_type", {"key": m.group("lit").strip(), "type": t},
                             m.start(), m.end(), m.group(0).strip()))
    keys: List[str] = []
    start = end = None
    for m in _KEY_KW.finditer(text):
        clause_end = min([i for i in (text.find(c, m.end()) for c in ".!?\n") if i >= 0] + [len(text)])
        before_start = max(0, m.start() - 60)
        before = text[before_start:m.start()]
        seg_after = text[m.end():clause_end]
        found = [q.group("v").strip() for q in _QUOTED.finditer(seg_after)]
        if not found:
            tail = re.search(rf"((?:{LIT}\s*(?:,|와|과|및)?\s*){{1,12}})\s*$", before)
            if tail:
                found = [q.group("v").strip() for q in _QUOTED.finditer(tail.group(1))]
        for k in found:
            if k not in keys:
                keys.append(k)
        if found:
            start = m.start() if start is None else start
            end = clause_end
    for k in typed_keys:
        if k not in keys:
            keys.append(k)
    req = None
    if keys:
        s = start if start is not None else (types[0].start if types else 0)
        e = end if end is not None else (types[-1].end if types else 0)
        req = Req("json_required_keys", {"keys": keys}, s, e, text[s:e].strip())
    return req, types


# --- 계산 -------------------------------------------------------------------------

_PRICE = re.compile(rf"(?:단가|개당|한\s*개에|1개에|하나에|한\s*개당)\s*(?:는|은|가|이|:)?\s*(?P<p>{NUMBER})\s*원"
                    rf"|(?P<p2>{NUMBER})\s*원짜리")
_QTY = re.compile(rf"(?:수량|개수)\s*(?:은|는|이|가|:)?\s*(?P<q>{NUMBER})\s*(?:개|장|권|병|잔|명|벌|켤레)?"
                  rf"|(?P<q2>{NUMBER})\s*(?:개|장|권|병|잔|벌)\s*(?:를|을)?\s*(?:사면|구매|구입|주문|샀)")
_TOTAL_WORD = re.compile(r"총액|총\s*금액|합계\s*금액|총\s*비용|전체\s*금액|총\s*얼마|모두\s*얼마|얼마")
TOTAL_LABELS = ("총액", "총 금액", "총금액", "합계 금액", "합계", "총 비용", "전체 금액", "총")
# Signs belong to arithmetic operands, not to counts or other shared NUMBER users.
_SIGNED_NUMBER = rf"[+−-]?(?:{NUMBER})"
_CALC_START = r"(?<![\w.,+−-])"
_VERB_OPS = (
    ("-", re.compile(rf"{_CALC_START}(?P<a>{_SIGNED_NUMBER})\s*에서\s*(?P<b>{_SIGNED_NUMBER})\s*(?:을|를)?\s*빼")),
    ("+", re.compile(rf"{_CALC_START}(?P<a>{_SIGNED_NUMBER})\s*(?:과|와|에|,)\s*(?P<b>{_SIGNED_NUMBER})\s*(?:을|를)?\s*(?:더하|더해|합하|합해|합치)")),
    ("*", re.compile(rf"{_CALC_START}(?P<a>{_SIGNED_NUMBER})\s*(?:에|과|와)\s*(?P<b>{_SIGNED_NUMBER})\s*(?:을|를)?\s*곱")),
    ("/", re.compile(rf"{_CALC_START}(?P<a>{_SIGNED_NUMBER})\s*(?:을|를)\s*(?P<b>{_SIGNED_NUMBER})\s*(?:으로|로)\s*나누")),
)
_VERB_LABELS = {"+": ("합", "합계", "결과", "답", "값"), "-": ("차", "차이", "결과", "답", "값"),
                "*": ("곱", "결과", "답", "값"), "/": ("몫", "결과", "답", "값")}
_EXPR_Q = re.compile(rf"{_CALC_START}(?P<expr>{_SIGNED_NUMBER}(?:\s*[+−\-×*÷/]\s*{_SIGNED_NUMBER})+)\s*(?:은|는|=|의\s*(?:값|결과))")
_PERCENT = re.compile(rf"{_CALC_START}(?P<a>{_SIGNED_NUMBER})\s*(?:원|명|개|점)?\s*의\s*(?P<b>{_SIGNED_NUMBER})\s*(?:%|퍼센트)")


def _frac(raw: str) -> Fraction:
    return Fraction(raw.replace(",", "").replace("−", "-"))


def _calc_reqs(text: str, out: List[Req]) -> None:
    found: List[Req] = []
    prices = list(_PRICE.finditer(text))
    qtys = list(_QTY.finditer(text))
    total = _TOTAL_WORD.search(text)
    if prices and qtys and total:
        if len(prices) == 1 and len(qtys) == 1:
            p = prices[0].group("p") or prices[0].group("p2")
            q = qtys[0].group("q") or qtys[0].group("q2")
            s = min(prices[0].start(), qtys[0].start())
            e = max(prices[0].end(), qtys[0].end(), total.end())
            found.append(Req("calculation", {
                "template": "unit_price_x_quantity", "operands": [p, q], "operation": "*",
                "expected": str(_frac(p) * _frac(q)), "labels": list(TOTAL_LABELS), "unit": "원"},
                s, e, text[s:e].strip()))
        else:
            found.append(Req("calculation", {}, total.start(), total.end(), total.group(0),
                             status="unsupported", reason="단가·수량 후보가 여러 개라 계산 관계를 확정하지 않음"))
    for op, pattern in _VERB_OPS:
        for m in pattern.finditer(text):
            a, b = m.group("a"), m.group("b")
            if op == "/" and _frac(b) == 0:
                continue
            value = {"+": _frac(a) + _frac(b), "-": _frac(a) - _frac(b),
                     "*": _frac(a) * _frac(b), "/": _frac(a) / _frac(b) if _frac(b) else None}[op]
            found.append(Req("calculation", {"template": "verb_operation", "operands": [a, b], "operation": op,
                                             "expected": str(value), "labels": list(_VERB_LABELS[op])},
                             m.start(), m.end(), m.group(0).strip()))
    if not found:
        for m in _PERCENT.finditer(text):
            value = _frac(m.group("a")) * _frac(m.group("b")) / 100
            found.append(Req("calculation", {"template": "percent_of", "operands": [m.group("a"), m.group("b")],
                                             "operation": "%", "expected": str(value),
                                             "labels": ["결과", "답", "값"]},
                             m.start(), m.end(), m.group(0).strip()))
    if not found:
        from .arith import _evaluate
        for m in _EXPR_Q.finditer(text):
            expr = m.group("expr").replace("×", "*").replace("÷", "/").replace("−", "-")
            try:
                value = _evaluate(expr)
            except (ValueError, ZeroDivisionError):
                continue
            operands = re.findall(NUMBER, m.group("expr"))
            found.append(Req("calculation", {"template": "explicit_expression", "operands": operands,
                                             "operation": m.group("expr"), "expected": str(value),
                                             "labels": ["결과", "답", "값"]},
                             m.start(), m.end(), m.group(0).strip()))
    ok = [r for r in found if r.status == "ok"]
    if len(ok) > 1:
        out.append(Req("calculation", {"candidates": [r.text for r in ok]}, ok[0].start, ok[-1].end,
                       "; ".join(r.text for r in ok), status="unsupported",
                       reason="질문의 계산 관계 후보가 여러 개 — 답변에 맞춰 고르지 않음"))
        out.extend(r for r in found if r.status != "ok")
        return
    out.extend(found)


# --- 원본 자료·변환 ---------------------------------------------------------------

_SORT = re.compile(rf"(?:(?:{LIT}|(?P<key2>[A-Za-z_][A-Za-z0-9_]{{0,30}}|[가-힣]{{1,10}}))\s*(?:을|를|의)?\s*기준(?:으로)?\s*)?"
                   r"(?P<dir>오름차순|내림차순|큰\s*순서|작은\s*순서|높은\s*순|낮은\s*순)\s*(?:으로)?\s*(?:정렬|나열|배열)")
_FILTER = re.compile(rf"(?:{LIT}|(?P<key2>[A-Za-z_][A-Za-z0-9_]{{0,30}}|[가-힣]{{1,10}}))\s*(?:이|가|은|는|값이)?\s*"
                     rf"(?P<v>{NUMBER})\s*(?P<op>이상|이하|초과|미만)인?\s*(?:항목|것|행|사람|데이터|요소|값)?\s*만")
_DEDUPE = re.compile(r"중복(?:을|된\s*값을|된\s*항목을|값을|된\s*것을)?\s*(?:제거|없애|빼)")
_UNIQUE = re.compile(r"중복\s*(?:없이|되지\s*않게|되지\s*않도록)|서로\s*다른\s*값")
_RENAME = re.compile(LIT + r"\s*(?:키|필드)?\s*(?:을|를)\s*[" + _OPEN_Q + r"](?P<to>[^" + _CLOSE_Q + r"\n]{1,40})[" + _CLOSE_Q + r"]\s*(?:으로|로)\s*(?:바꿔|변경|이름을\s*바꿔|바꾸)")
_PRESERVE = re.compile(r"(?:원본|원래|주어진)\s*(?:값|데이터|순서|자료)?\s*(?:을|를|은|는)?\s*(?:그대로|유지|보존|바꾸지)"
                       r"|(?:순서|값|ID|아이디)\s*(?:를|을|는|은)?\s*(?:그대로\s*)?(?:유지|보존)")
_CONVERT_FORMAT = re.compile(r"(?:JSON|표|목록)\s*(?:으로|로)\s*변환", re.IGNORECASE)
_AGG = re.compile(rf"(?:{LIT}|(?P<key2>[A-Za-z_][A-Za-z0-9_]{{0,30}}|[가-힣]{{1,10}}))\s*(?:열|필드|값)?\s*(?:의)?\s*"
                  r"(?P<agg>합계|총합|평균|최댓값|최솟값|최대값|최소값)")


def _find_json_arrays(text: str, forbid: List[Tuple[int, int]]) -> List[Tuple[int, int, Any]]:
    decoder = json.JSONDecoder()
    found = []
    i = 0
    while True:
        i = text.find("[", i)
        if i < 0:
            break
        blocked_end = next((end for start, end in forbid if start <= i < end), None)
        if blocked_end is not None:
            i = blocked_end
            continue
        try:
            value, end = decoder.raw_decode(text, i)
        except (json.JSONDecodeError, RecursionError, ValueError):
            i += 1
            continue
        if isinstance(value, list) and value and not any(start < end and stop > i for start, stop in forbid):
            found.append((i, end, value))
            i = end
        else:
            i += 1
    return found


def _source_data(qa: TextAnalysis, instruction: str) -> Optional[Dict[str, Any]]:
    """질문에 제시된 원본 자료 한 개. 여러 개거나 없으면 None/모호."""
    candidates: List[Dict[str, Any]] = []
    for block in qa.fenced_blocks:
        try:
            value = json.loads(block["content"])
        except (json.JSONDecodeError, RecursionError, ValueError):
            continue
        if isinstance(value, list) and value:
            candidates.append({"kind": "json", "value": value, "start": block["start"], "end": block["end"]})
    # Body arrays are additional source candidates even when a fenced source exists.
    # Exclude fence ranges so one array is not counted again (including nested arrays).
    for s, e, value in _find_json_arrays(qa.text, qa.fence_ranges):
        candidates.append({"kind": "json", "value": value, "start": s, "end": e})
    for t in qa.tables:
        if t.kind == "markdown" and t.headers:
            rows = [dict(zip(t.headers, r)) for r in t.rows]
            candidates.append({"kind": "table", "value": rows, "headers": list(t.headers),
                               "start": t.start, "end": t.end})
    if not candidates:
        items = [i for i in qa.top_level_items]
        if len(items) >= 2:
            candidates.append({"kind": "list", "value": [i.body.strip() for i in items],
                               "start": items[0].start, "end": items[-1].end})
    if len(candidates) != 1:
        return {"ambiguous": True, "count": len(candidates)} if candidates else None
    return candidates[0]


def _transform_reqs(qa: TextAnalysis, text: str, out: List[Req]) -> Optional[Dict[str, Any]]:
    ops: List[Dict[str, Any]] = []
    evidence: List[Tuple[int, int, str]] = []
    m = _FILTER.search(text)
    if m:
        key = m.group("lit") or m.group("key2")
        ops.append({"op": "filter", "key": key, "cmp": m.group("op"), "value": m.group("v")})
        evidence.append((m.start(), m.end(), m.group(0)))
    m = _DEDUPE.search(text)
    if m:
        ops.append({"op": "dedupe"})
        evidence.append((m.start(), m.end(), m.group(0)))
    m = _SORT.search(text)
    if m:
        key = m.group("lit") or m.group("key2")
        direction = "desc" if any(w in m.group("dir") for w in ("내림", "큰", "높은")) else "asc"
        ops.append({"op": "sort", "key": key, "direction": direction})
        evidence.append((m.start(), m.end(), m.group(0)))
    m = _RENAME.search(text)
    if m:
        ops.append({"op": "rename", "from": m.group("lit").strip(), "to": m.group("to").strip()})
        evidence.append((m.start(), m.end(), m.group(0)))
    preserve = _PRESERVE.search(text)
    convert_format = _CONVERT_FORMAT.search(text)
    unique = _UNIQUE.search(text)
    agg = _AGG.search(text)
    if not (ops or preserve or unique or agg or convert_format):
        return None
    source = _source_data(qa, text)
    if convert_format and not (ops or preserve):
        out.append(Req("transform", {}, convert_format.start(), convert_format.end(),
                       convert_format.group(0), status="unsupported",
                       reason="형식 변환에 필요한 원본 필드·값 연결 규칙이 지원되지 않아 비교값을 만들 수 없음"))
    if unique:
        out.append(Req("unique_values", {}, unique.start(), unique.end(), unique.group(0)))
    if source is None:
        if preserve:
            code_task = any(r.kind in ("code_requested", "python_code_requested") for r in out)
            if not (code_task and re.search(r"(?:함수|코드|프로그램|알고리즘)\s*(?:를|을)?\s*(?:작성|구현|만들)", text)):
                out.append(Req("transform", {}, preserve.start(), preserve.end(), preserve.group(0),
                               status="unsupported",
                               reason="원본 보존 요구는 있으나 지원되는 JSON 배열·표·목록 구조에서 원본 대상을 확정하지 못함"))
        return None   # 일반 연산 단어만으로 작업 설명(예: "중복을 제거하는 함수")을 평가 요구로 만들지 않는다
    if agg:
        out.append(Req("aggregate", {"key": agg.group("lit") or agg.group("key2"), "agg": agg.group("agg")},
                       agg.start(), agg.end(), agg.group(0),
                       **({} if source and not source.get("ambiguous") else
                          {"status": "unsupported", "reason": "질문에 원본 자료로 보이는 것이 여러 개라 어느 자료를 계산하라는지 정할 수 없음"})))
    if ops or preserve:
        s = min([e[0] for e in evidence] + ([preserve.start()] if preserve else []))
        e = max([e[1] for e in evidence] + ([preserve.end()] if preserve else []))
        req = Req("transform", {"ops": ops, "preserve": bool(preserve)}, s, e, text[s:e].strip())
        if source.get("ambiguous"):
            req.status = "unsupported"
            req.reason = f"질문에 원본 자료로 보이는 것이 {source['count']}개라 어느 자료를 바꾸라는지 정할 수 없음"
        out.append(req)
    return source


# --- 진입점 ------------------------------------------------------------------------

def extract_conditions(question: str) -> QuestionConditions:
    qa = TextAnalysis(question)
    text = _instruction_text(qa)
    reqs: List[Req] = []
    m = _EMPTY.search(text)
    if m:
        reqs.append(Req("explicit_empty", {}, m.start(), m.end(), m.group(0).strip()))
    _count_reqs(text, reqs)
    _literal_reqs(text, reqs)
    m = _CHARSET.search(text)
    if m:
        reqs.append(Req("charset", {"charset": re.sub(r"\s+", "", m.group("set"))}, m.start(), m.end(),
                        m.group(0).strip()))
    _format_reqs(text, reqs)
    md_neg = _MARKDOWN_NEG.search(text)
    if md_neg:
        reqs.append(Req("markdown_forbidden", {}, md_neg.start(), md_neg.end(), md_neg.group(0).strip()))
        for r in reqs:
            if r.kind in ("table_required", "table_count", "code_block_required", "code_block_count", "list_required",
                          "heading_required", "heading_count", "table_columns", "table_column_count",
                          "table_header", "table_data_rows", "table_total_rows") and r.status == "ok":
                r.status = "unsupported"
                r.reason = ("질문이 마크다운 기호 사용을 금지해 표·코드블록·목록·제목 구조를 Markdown 문법으로 "
                            "확인할 수 없음 (요구 간 충돌 — 판정하지 않음)")
    _format_internal_structure(text, reqs)
    _calc_reqs(text, reqs)
    source = _transform_reqs(qa, text, reqs)
    # 표·목록 요구와 겹치는 일반 개수: 불릿 개수가 있으면 목록 요구는 개수 조건이 포함한다.
    return QuestionConditions(reqs=reqs, instruction_text=text,
                              source_data=source if source and not source.get("ambiguous") else None)
