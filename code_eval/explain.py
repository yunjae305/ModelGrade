"""판정 결과를 처음 보는 사람도 읽을 수 있는 설명으로 바꾼다 (고정 템플릿, LLM 없음).

결과마다 다음을 만든다.
- headline  : 무엇이 문제인가 (한 줄)
- summary   : 왜 FAIL 인가 / 왜 판정하지 않았나 (쉬운 문장)
- checked   : 이 검사가 무엇을 보는가
- expected / actual : 기대한 것과 실제
- requirement : 질문에서 읽은 요구 원문 (있을 때)
- locations : 문제 위치 — 줄 번호와 강조할 글자, 코드 줄, 센 항목 목록
- fix       : 고치려면

판정·측정값은 바꾸지 않는다. 이미 계산된 결과를 사람이 읽는 말로 옮길 뿐이다.
"""

from __future__ import annotations

import re
import unicodedata
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional, Tuple

from .core import Ctx
from .model import FAIL, PASS, Status

__all__ = ["explain_result", "python_error_ko", "json_error_ko", "EXPLAIN_VERSION"]

EXPLAIN_VERSION = "v16-explain-1.0"

CHECKED = {
    "1-1.response_nonempty": "답이 비었거나 오류 · 능력 부정 문구만은 아닌지(에러 케이스)",
    "1-1.character_rules": "답변에 깨진 글자(�, Ã¬ 같은 기호)나 눈에 보이지 않는 제어 문자가 섞였는지",
    "1-1.foreign_script": "한자·일본 문자 같은 외국 문자가 질문 주제나 설명·괄호 병기 없이 갑자기 나왔는지",
    "1-2.code_block": "질문이 요구한 코드블록(``` 로 감싼 코드)이 요구대로 있는지",
    "1-2.code_language_label": "코드가 코드블록으로 구분돼 있고, 첫 줄에 언어를 적었다면 그 표시가 요구와 같은지 (표시가 없어도 통과)",
    "1-2.code_fence_min": "코드블록을 여닫는 ``` 표시가 2개(여는 것 + 닫는 것) 이상인지",
    "1-2.fence_imbalance": "``` (코드)와 $$ (수식)의 여는 표시·닫는 표시가 각각 짝이 맞는지",
    "1-2.json_syntax": "JSON 이 문법에 맞아 프로그램이 읽을 수 있는지",
    "1-2.python_syntax": "파이썬 코드가 문법에 맞는지 (코드를 실행하지 않고 문법만 확인)",
    "1-2.orphan_closing_tag": "</태그> 처럼 여는 태그 없이 닫는 태그만 남아 글자로 보이는지",
    "1-3.footnote_link": "본문의 각주 표시([^1], [각주1])마다 설명이 달려 있는지",
    "1-3.citation_link": "본문의 인용 번호([1])마다 출처 목록이 있는지",
    "1-3.internal_link": "문서 안 링크(#제목)가 실제로 있는 제목을 가리키는지",
    "2-1.explicit_empty": "질문이 '아무것도 출력하지 마'라고 했을 때 답변이 정말 비어 있는지",
    "2-1.count": "질문이 요구한 개수(불릿·문장·문단·줄·항목 수)를 지켰는지",
    "2-1.char_count": "질문이 요구한 글자 수를 지켰는지",
    "2-1.literal": "질문이 넣으라고 하거나 쓰지 말라고 한 단어·문구를 지켰는지",
    "2-1.charset": "질문이 허용한 문자(예: 한글만)만 썼는지",
    "2-2.table": "질문이 요구한 표와 표의 열·헤더·행 수",
    "2-2.list": "질문이 요구하거나 금지한 목록 형식",
    "2-2.heading": "질문이 요구한 제목(# 제목)",
    "2-2.json_structure": "질문이 요구한 JSON 형태·키·값 종류",
    "3-1.question_calculation": "질문의 숫자로 직접 계산한 값과 답변의 값이 같은지",
    "3-1.answer_equation": "답변에 적힌 계산식을 다시 계산하면 적힌 결과와 같은지",
    "3-2.declared_count": "답변이 '다음 세 가지'처럼 말한 개수와 바로 이어지는 실제 항목 수가 같은지",
    "3-3.transform": "질문에 준 원본 자료를 요청대로 정렬·필터 등 했을 때의 결과와 답변이 같은지",
    "3-3.unique_values": "'중복 없이' 요구대로 같은 항목이 두 번 나오지 않는지",
    "3-3.aggregate": "원본 자료의 합계·평균 등이 답변과 같은지",
    "AIONA-M.internal_marker": "AIONA 내부 처리용 표시(자리표시자·내부 태그 등)가 사용자 화면에 노출됐는지",
    "AIONA-F.forbidden_expression": "이 질문에 미리 정해 둔 '나오면 안 되는 표현'이 나왔는지",
}

STATUS_HEADLINE = {
    Status.CONFIG_MISSING: "판정하지 않음 — 필요한 설정(목록)이 없음",
    Status.DISPLAY_MISSING: "판정하지 않음 — 실제 화면에 보인 답변(표시본)이 없음",
    Status.INCOMPLETE: "판정하지 않음 — 일부만 검사함",
    Status.UNSUPPORTED: "판정하지 않음 — 코드로 확정할 수 없는 경우",
    Status.DEPENDENCY_FAILED: "판정하지 않음 — 앞선 검사가 실패해 측정할 수 없음",
    Status.CHECK_ERROR: "판정하지 않음 — 검사 프로그램 오류",
    Status.LIMIT_EXCEEDED: "판정하지 않음 — 입력이 너무 커서 안전 한도를 넘음",
    Status.OUT_OF_SCOPE: "적용 대상 아님 — 이 모델에는 쓰지 않는 AIONA 전용 규칙",
    Status.DUPLICATE: "중복 — 같은 조건을 다른 항목에서 이미 셈",
    Status.APPLICABILITY_EXCEPTION: "적용 제외 — 질문의 요청이나 예시 문맥 때문에 이 규칙을 쓰지 않음",
}

STATUS_NOTE = {
    Status.UNSUPPORTED: "틀렸다는 뜻이 아니라, 규칙만으로 확실히 판단할 수 없어 PASS·FAIL 을 만들지 않았다는 뜻입니다.",
    Status.DEPENDENCY_FAILED: "원인이 되는 앞선 FAIL 한 건만 집계하고, 이 항목은 따로 FAIL 로 세지 않습니다.",
    Status.CONFIG_MISSING: "설정이 없는 것을 '문제 없음(PASS)'으로 처리하지 않습니다.",
    Status.DISPLAY_MISSING: "원문으로 대신 판정하지 않습니다.",
}

_OP_WORD = {"==": "정확히", "<=": "이하", "<": "미만", ">=": "이상", ">": "초과"}
_COUNT_UNIT = {"bullet_count": ("불릿", "개"), "sentence_count": ("문장", "문장"), "paragraph_count": ("문단", "문단"),
               "line_count": ("줄", "줄"), "word_count": ("어절(띄어쓰기 단위)", "어절"), "list_count": ("항목", "개")}
_SIGNATURE_KO = {
    "replacement-character": "� (대체 문자 — 원래 글자가 깨져서 알 수 없음)",
    "utf8-as-latin1": "Ã¬·Ã« 같은 글자 (한글·기호가 다른 인코딩으로 잘못 읽혀 깨진 흔적)",
    "c1-control": "눈에 보이지 않는 제어 문자 (정상 문장에는 나오지 않음)",
    "c0-control": "눈에 보이지 않는 제어 문자 (정상 문장에는 나오지 않음)",
    "lone-surrogate": "잘린 이모지·특수문자 조각",
}
_SHORT_SIG = {"replacement-character": "� 대체 문자", "utf8-as-latin1": "인코딩이 깨진 글자",
              "c1-control": "보이지 않는 제어 문자", "c0-control": "보이지 않는 제어 문자", "lone-surrogate": "잘린 특수문자"}
_STRUCT_KO = {"list": "목록 항목", "table_data_rows": "표의 데이터 행(헤더 제외)", "headings": "소제목",
              "numbered_list": "번호 목록 항목", "numbered_headings": "번호 붙은 제목", "bullet_list": "불릿 목록 항목"}


# --- 공용 도우미 ---------------------------------------------------------------------

def _n(v: Any) -> str:
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.6g}"
    return str(v)


_DIGIT_FINAL = {"0": "ㅇ", "1": "ㄹ", "3": "ㅁ", "6": "ㄱ", "7": "ㄹ", "8": "ㄹ"}


def _final(word: str) -> Optional[str]:
    """마지막 글자의 받침: None(받침 없음) / 'ㄹ' / 'o'(그 밖의 받침) / '?'(판단 불가)."""
    w = re.sub(r"[\s’'\")\]」』.…]+$", "", word or "")
    if not w:
        return "?"
    ch = w[-1]
    if "가" <= ch <= "힣":
        jong = (ord(ch) - 0xAC00) % 28
        return None if jong == 0 else ("ㄹ" if jong == 8 else "o")
    if ch.isdigit():
        f = _DIGIT_FINAL.get(ch)
        return None if f is None else ("ㄹ" if f == "ㄹ" else "o")
    return "?"


def _j(word: str, pair: str) -> str:
    """word + 조사. pair: '을를' '이가' '은는' '과와' '으로'."""
    f = _final(word)
    if pair == "으로":
        josa = "로" if f in (None, "ㄹ") else ("으로" if f == "o" else "(으)로")
    else:
        a, b = pair[0], pair[1]
        josa = a if f in ("o", "ㄹ") else (b if f is None else f"{a}({b})")
    return f"{word}{josa}"


def _need(op: str, n: Any, unit: str = "개") -> str:
    word = _OP_WORD.get(op, op)
    return f"정확히 {_n(n)}{unit}" if op == "==" else f"{_n(n)}{unit} {word}"


def _visible(s: str) -> str:
    """보이지 않는 문자를 ⟨U+XXXX⟩ 로 드러낸다."""
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if (cat == "Cc" and ch not in "\t") or cat in ("Cs",):
            out.append(f"⟨U+{ord(ch):04X}⟩")
        else:
            out.append(ch)
    return "".join(out)


def _line_info(text: str, idx: int) -> Tuple[int, int, int]:
    idx = max(0, min(idx, len(text)))
    start = text.rfind("\n", 0, idx) + 1
    end = text.find("\n", idx)
    end = len(text) if end < 0 else end
    return text.count("\n", 0, idx) + 1, start, end


def snippet(text: str, start: Optional[int], end: Optional[int], label: str = "", tag: str = "",
            target: str = "answer", margin: int = 40) -> Optional[Dict[str, Any]]:
    """문제 글자가 있는 줄을 앞·강조·뒤로 나눈다."""
    if start is None or not isinstance(text, str) or start > len(text):
        return None
    end = start + 1 if end is None or end <= start else end
    line, ls, le = _line_info(text, start)
    hit_end = min(end, le) if end > start else start
    if hit_end <= start:
        hit_end = min(start + 1, len(text))
    b0 = max(ls, start - margin)
    a1 = min(le, hit_end + margin)
    return {"kind": "snippet", "target": target, "label": label or f"{line}번째 줄", "line": line,
            "before": ("…" if b0 > ls else "") + _visible(text[b0:start]),
            "hit": _visible(text[start:hit_end]) or "⟨빈 자리⟩",
            "after": _visible(text[hit_end:a1]) + ("…" if a1 < le else ""), "tag": tag}


def line_snippets(text: str, hits: List[Tuple[int, int, str]], target: str = "answer",
                  margin: int = 40) -> List[Dict[str, Any]]:
    """같은 줄에 있는 여러 문제 글자를 한 줄에 모아 segments(강조 여부)로 나눈다."""
    by_line: Dict[int, List[Tuple[int, int, str]]] = {}
    for start, end, tag in hits:
        if start is None or start > len(text):
            continue
        line, ls, le = _line_info(text, start)
        by_line.setdefault(line, []).append((start, max(end, start + 1), tag))
    out = []
    for line, items in sorted(by_line.items()):
        items.sort()
        _, ls, le = _line_info(text, items[0][0])
        lo, hi = max(ls, items[0][0] - margin), min(le, items[-1][1] + margin)
        segs, pos = [], lo
        for start, end, _tag in items:
            end = min(end, le) if end > start else start + 1
            if start < pos:
                continue
            if start > pos:
                segs.append({"text": _visible(text[pos:start]), "mark": False})
            segs.append({"text": _visible(text[start:end]) or "⟨빈 자리⟩", "mark": True})
            pos = end
        if hi > pos:
            segs.append({"text": _visible(text[pos:hi]), "mark": False})
        if lo > ls:
            segs.insert(0, {"text": "…", "mark": False})
        if hi < le:
            segs.append({"text": "…", "mark": False})
        tags = sorted({t for _, _, t in items if t})
        out.append({"kind": "snippet", "target": target, "label": f"{line}번째 줄 · {len(items)}곳", "line": line,
                    "segments": segs, "before": "", "hit": "", "after": "", "tag": " / ".join(tags)})
    return out


def code_view(src: str, err_line: Optional[int], label: str, base_line: Optional[int] = None, radius: int = 2,
              pointer: str = "") -> Dict[str, Any]:
    lines = src.split("\n")
    if err_line is None or err_line < 1:
        err_line = 1
    err_line = min(err_line, max(1, len(lines)))
    lo, hi = max(1, err_line - radius), min(len(lines), err_line + radius)
    return {"kind": "code", "label": label, "pointer": pointer,
            "lines": [{"n": i, "answer_line": base_line + i if base_line is not None else None,
                       "text": _visible(lines[i - 1]), "mark": i == err_line} for i in range(lo, hi + 1)]}


def item_list(label: str, items: List[str], mark: Optional[List[int]] = None, limit: int = 30) -> Dict[str, Any]:
    shown = items[:limit]
    return {"kind": "list", "label": label, "items": [_visible(x)[:160] for x in shown],
            "marks": [i for i in (mark or []) if i < len(shown)], "more": max(0, len(items) - limit)}


def _q(r: Dict[str, Any]) -> str:
    ev = [e for e in r.get("evidence", []) if e["target"] in ("question", "history")]
    if not ev:
        return ""
    return (f"({ev[0]['note']}) " if ev[0]["target"] == "history" and ev[0].get("note") else "") + ev[0]["excerpt"]


def _answer_ev(r: Dict[str, Any], target: str = "answer") -> List[Dict[str, Any]]:
    return [e for e in r.get("evidence", []) if e["target"] == target and e.get("start") is not None]


def _snips(ctx: Ctx, r: Dict[str, Any], tag: str = "", target: str = "answer", limit: int = 8) -> List[Dict[str, Any]]:
    text = ctx.answer if target == "answer" else (ctx.displayed_text or "")
    out = []
    for e in _answer_ev(r, target)[:limit]:
        s = snippet(text, e["start"], e["end"], tag=tag or e.get("note", ""), target=target)
        if s:
            out.append(s)
    return out


def _base(check_id: str) -> str:
    return check_id.split(":")[0]


# --- 오류 메시지 번역 -------------------------------------------------------------------

_PY_RULES: List[Tuple[str, Callable[[re.Match], str]]] = [
    (r"expected an indented block after '(\w+)' statement on line (\d+)",
     lambda m: f"{m.group(2)}번째 줄의 '{m.group(1)}' 문 다음 줄은 한 단계 들여쓰기(보통 공백 4칸)가 되어야 하는데, 들여쓴 줄이 없습니다."),
    (r"expected an indented block after function definition on line (\d+)",
     lambda m: f"{m.group(1)}번째 줄의 함수 정의(def) 아래에 들여쓴 함수 본문이 없습니다."),
    (r"expected an indented block after class definition on line (\d+)",
     lambda m: f"{m.group(1)}번째 줄의 클래스 정의(class) 아래에 들여쓴 본문이 없습니다."),
    (r"expected an indented block", lambda m: "들여쓰기가 필요한 자리에 들여쓴 줄이 없습니다."),
    (r"'(.)' was never closed", lambda m: f"여는 괄호 '{m.group(1)}' 를 열어 놓고 닫지 않았습니다."),
    (r"unmatched '(.)'", lambda m: f"짝이 되는 여는 괄호 없이 닫는 괄호 '{m.group(1)}' 가 있습니다."),
    (r"closing parenthesis '(.)' does not match opening parenthesis '(.)'",
     lambda m: f"괄호 짝이 맞지 않습니다. '{m.group(2)}' 로 열고 '{m.group(1)}' 로 닫았습니다."),
    (r"unterminated triple-quoted string literal", lambda m: "세 따옴표(\"\"\" 또는 ''')로 시작한 문자열이 끝까지 닫히지 않았습니다."),
    (r"unterminated string literal", lambda m: "따옴표로 시작한 문자열을 같은 줄에서 닫지 않았습니다."),
    (r"unexpected indent", lambda m: "들여쓰면 안 되는 줄이 들여쓰기 되어 있습니다."),
    (r"unindent does not match any outer indentation level", lambda m: "들여쓰기 칸 수가 위쪽 줄들과 맞지 않습니다."),
    (r"expected ':'", lambda m: "콜론(:)이 빠졌습니다. if·for·while·def 줄 끝에는 ':' 가 있어야 합니다."),
    (r"Perhaps you forgot a comma", lambda m: "쉼표(,)가 빠진 것 같습니다."),
    (r"Maybe you meant '==' or ':=' instead of '='", lambda m: "비교할 때는 '==' 를 써야 하는데 '=' 를 썼습니다."),
    (r"Missing parentheses in call to '(\w+)'", lambda m: f"{m.group(1)} 뒤에 괄호가 필요합니다 (파이썬 3 문법)."),
    (r"invalid character '(.)' \(U\+([0-9A-Fa-f]+)\)",
     lambda m: f"파이썬 코드에 쓸 수 없는 문자 '{m.group(1)}' (U+{m.group(2)}) 가 들어 있습니다. 전각 기호나 둥근 따옴표일 가능성이 큽니다."),
    (r"invalid decimal literal", lambda m: "숫자 바로 뒤에 글자가 붙어 있어 올바른 숫자가 아닙니다."),
    (r"cannot assign to", lambda m: "값을 넣을 수 없는 곳에 '=' 를 썼습니다."),
    (r"null bytes", lambda m: "코드 안에 보이지 않는 널(NULL) 문자가 들어 있습니다."),
    (r"invalid syntax", lambda m: "파이썬 문법에 맞지 않는 부분이 있습니다."),
]


def python_error_ko(msg: str) -> str:
    for pattern, render in _PY_RULES:
        m = re.search(pattern, msg or "")
        if m:
            return render(m)
    return f"파이썬 문법 오류 ({msg})"


_JSON_RULES = [
    (r"Illegal trailing comma", "마지막 항목 뒤에 쉼표(,)를 붙였습니다. JSON 에서는 마지막 항목 뒤에 쉼표를 쓸 수 없습니다."),
    (r"Expecting ',' delimiter", "항목과 항목 사이의 쉼표(,)가 빠졌습니다."),
    (r"Expecting ':' delimiter", "키와 값 사이의 콜론(:)이 빠졌습니다."),
    (r"Expecting property name enclosed in double quotes",
     "키 이름은 큰따옴표(\")로 감싸야 합니다. 마지막 항목 뒤에 쉼표를 붙였거나 작은따옴표를 쓴 경우에도 이 오류가 납니다."),
    (r"Expecting value", "값이 있어야 할 자리에 값이 없거나, JSON 에서 쓸 수 없는 값(작은따옴표 문자열, 주석, 마지막 쉼표 등)이 있습니다."),
    (r"Extra data", "JSON 이 끝난 뒤에 다른 내용이 더 붙어 있습니다."),
    (r"Unterminated string", "따옴표로 시작한 문자열이 닫히지 않았습니다."),
    (r"Invalid control character", "문자열 안에 줄바꿈·탭 같은 제어 문자가 그대로 들어 있습니다(\\n 처럼 써야 함)."),
    (r"Invalid \\escape", "문자열 안의 역슬래시(\\) 표기가 잘못됐습니다."),
    (r"RFC 8259 에 없는 상수입니다: (\w+)", "NaN·Infinity 는 JSON 표준 값이 아닙니다 (null 이나 숫자를 써야 함)."),
]


def json_error_ko(msg: str) -> str:
    for pattern, ko in _JSON_RULES:
        if re.search(pattern, msg or ""):
            return ko
    return f"JSON 문법 오류 ({msg})"


# --- 검사별 설명 -----------------------------------------------------------------------

def _e(headline, summary, expected="", actual="", locations=None, fix="", note=""):
    return {"headline": headline, "summary": summary, "expected": expected, "actual": actual,
            "locations": [l for l in (locations or []) if l], "fix": fix, "note": note}


def _x_response(r, ctx):
    kind = (r.get("measured") or {}).get("error_case")
    if r["verdict"] == FAIL:
        if kind == "답 없음":
            return _e("답이 오지 않았습니다(에러 케이스)", f"수집 상태 {r['measured'].get('collection_status')}: 오류 코드 · 시간 초과 · 무응답.",
                      "결과물이 있는 답변", "답 없음", fix="모델 · 서비스 쪽 오류입니다. 같은 질문을 다시 보내도 반복되면 모델 문제로 봅니다.")
        if kind == "서비스 오류 문구":
            return _e("서비스 오류 문구만 있습니다(에러 케이스)", "'응답 생성 중 오류가 발생했습니다 · 다시 시도해 주세요' 류의 오류 메시지만 있고 결과물이 없습니다.",
                      "요청에 대한 결과물", f"실제 글자 {r['measured']['content_chars']}자", fix="서비스 · 전송 단계 오류입니다. 이 턴의 LLM 항목은 따로 세지 않습니다(한 오류 한 항목).")
        if kind == "능력 부정 문구":
            return _e("능력 부정 문구만 있습니다(에러 케이스)", "요청과 무관하게 '언어 모델이라 할 수 없다' 류의 고정 문구만 있고 결과물이 없습니다.",
                      "요청에 대한 결과물", f"실제 글자 {r['measured']['content_chars']}자", fix="모델이 이따금 내는 정형 거절입니다. 요청 수행 · 거절 기준도 함께 FAIL 이 됩니다.")
        return _e("답변이 비어 있습니다(에러 케이스)", "공백·줄바꿈만 있고 실제 글자가 하나도 없습니다.",
                  "글자가 1자 이상 있는 답변", f"실제 글자 {r['measured']['content_chars']}자",
                  fix="내용이 있는 답변이 나와야 합니다.")
    return _e("에러 케이스 아님", f"실제 글자 {_n(r['measured']['content_chars'])}자가 있습니다.")


def _x_chars(r, ctx):
    m = r["measured"]
    if r["verdict"] != FAIL:
        if m.get("illustrative_examples"):
            return _e("깨진 글자 없음 (예시는 제외)",
                      f"깨짐 표기 {m['illustrative_examples']}개는 질문 주제나 주변 설명·예시가 있어 예시로 보고 제외했습니다.")
        return _e("깨진 글자 없음", "깨진 글자나 제어 문자가 발견되지 않았습니다.")
    evs = _answer_ev(r)
    hits = [(e["start"], e["end"], _SHORT_SIG.get(e.get("note", ""), e.get("note", ""))) for e in evs]
    locs = line_snippets(ctx.answer, hits)
    lines = [l["line"] for l in locs]
    kinds_ko = [_SIGNATURE_KO[k] for k in _SIGNATURE_KO if any(e.get("note") == k for e in evs)]
    readings = (r.get("details") or {}).get("readings", [])
    why = ("실제 출력 결과로 제시돼 있어" if any(x.get("reading") == "actual" for x in readings)
           else "질문 주제나 주변에 인코딩·예시 설명이 없어")
    return _e(f"깨진 글자 {m['violations']}개가 섞여 있습니다 ({', '.join(map(str, lines[:6]))}번째 줄)",
              f"답변 {', '.join(map(str, lines[:6]))}번째 줄에 정상 문장에서는 나오지 않는 글자가 {m['violations']}개 있습니다. "
              f"{why} 예시가 아닌 실제 깨진 글자로 판정했습니다. 사용자 화면에서는 이상한 기호로 보이거나 아예 보이지 않습니다.",
              "깨진 글자·제어 문자 0개", f"{m['violations']}개",
              locs, "답변을 저장·전송하는 과정의 인코딩(UTF-8) 처리를 확인하세요. 깨짐을 예시로 보여준 것이라면 "
              "바로 앞뒤에 인코딩·예시 설명을 함께 쓰세요. 원래 어떤 글자였는지는 코드로 추정하지 않습니다.",
              "발견된 종류 — " + " / ".join(kinds_ko) + f". 코드 포인트: {', '.join(m['codepoints'][:8])}")


def _x_foreign(r, ctx):
    m = r["measured"] if isinstance(r["measured"], dict) else {}
    if r["verdict"] != FAIL:
        return _e("설명 없는 외국 문자 없음", r["reason"])
    evs = _answer_ev(r)
    locs = line_snippets(ctx.answer, [(e["start"], e["end"], "외국 문자") for e in evs])
    lines = ", ".join(map(str, [l["line"] for l in locs][:6]))
    shown = ", ".join(f"'{e['excerpt']}'" for e in evs[:5])
    return _e(f"설명 없이 외국 문자가 나왔습니다 ({lines}번째 줄)",
              f"답변 {lines}번째 줄에 {shown} 같은 외국 문자가 질문 주제나 앞뒤 설명·괄호 병기 없이 들어가 있습니다. "
              "질문과 관계없는 외국어가 섞였을 수 있습니다.",
              "설명 없는 외국 문자 0곳", f"{m.get('abrupt', len(evs))}곳", locs,
              "필요한 표기라면 뜻이나 읽는 법을 괄호로 함께 쓰고, 필요 없다면 한국어로 바꾸세요.",
              "문자 종류 — " + ", ".join(m.get("scripts", [])))


def _x_code_block(r, ctx):
    m = r["measured"] if isinstance(r["measured"], dict) else {}
    blocks = ctx.aa.fenced_blocks
    locs = [snippet(ctx.answer, b["start"], b["start"] + len(b["opening"]), label=f"코드블록 {i + 1} 시작",
                    tag="여는 ```") for i, b in enumerate(blocks[:8])]
    kind = r["check_id"].split(":")[1] if ":" in r["check_id"] else ""
    req = r["required"]
    n = len(blocks)
    if kind == "forbidden":
        if r["verdict"] == FAIL:
            return _e("쓰지 말라고 한 코드블록을 썼습니다", f"질문이 코드블록을 쓰지 말라고 했는데 답변에 코드블록이 {n}개 있습니다.",
                      "코드블록 0개", f"코드블록 {n}개", locs, "코드를 ``` 로 감싸지 말고 본문 글로 쓰세요.")
        return _e("코드블록 없음 (요구대로)", "요구대로 코드블록을 쓰지 않았습니다.")
    if kind == "count":
        need = _need(req["op"], req["code_blocks"])
        if r["verdict"] == FAIL:
            return _e(f"코드블록 개수가 다릅니다 (요구 {need}, 실제 {n}개)",
                      f"질문은 코드블록을 {need} 요구했는데, 답변에는 ``` 로 감싼 코드블록이 {n}개 있습니다.",
                      f"코드블록 {need}", f"코드블록 {n}개", locs,
                      "요구한 개수만큼 코드를 ``` 로 감싸 나누거나 합치세요.")
        return _e(f"코드블록 개수 맞음 ({n}개)", f"요구({need})대로 코드블록이 {n}개입니다.")
    if r["verdict"] == FAIL:
        return _e("요구한 코드블록이 없습니다", "질문이 코드를 코드블록(``` 로 감싼 부분)으로 달라고 했는데 답변에 코드블록이 없습니다.",
                  "코드블록 1개 이상", "코드블록 0개", fix="코드를 ```언어 … ``` 로 감싸서 보여 주세요.")
    return _e(f"코드블록 있음 ({n}개)", "요구한 코드블록이 있습니다.")


def _x_lang(r, ctx):
    if r["verdict"] == FAIL:
        want = "/".join(r["required"]["language_label"])
        labels = r["measured"]["language_labels"]
        bad = [i for i, l in enumerate(labels) if l and l not in r["required"]["language_label"]]
        blocks = ctx.aa.fenced_blocks
        locs = [snippet(ctx.answer, blocks[i]["start"], blocks[i]["start"] + len(blocks[i]["opening"]),
                        label=f"코드블록 {i + 1} 첫 줄", tag="언어 표시") for i in bad if i < len(blocks)]
        return _e("코드블록의 언어 표시가 요구와 다릅니다",
                  f"질문은 {want} 코드블록을 요구했는데, 코드블록 " + ", ".join(f"{i + 1}(표시: {labels[i]})" for i in bad) +
                  " 의 첫 줄 언어 표시가 다릅니다.", f"```{want.split('/')[0]}", ", ".join(labels), locs,
                  f"코드블록 첫 줄을 ```{want.split('/')[0]} 로 쓰세요. (코드 내용이 그 언어인지는 이 검사에서 보지 않습니다)")
    if any(not l for l in (r.get("measured") or {}).get("language_labels") or []):
        return _e("코드블록으로 구분됨", "코드블록 첫 줄에 언어 표시는 없지만 코드가 코드블록으로 구분돼 있어 통과입니다(언어 표시는 없어도 됨).")
    return _e("언어 표시 맞음", "코드블록의 언어 표시가 요구와 같습니다.")


def _x_fence_min(r, ctx):
    m = r["measured"]
    if r["verdict"] == FAIL:
        marks = ctx.aa.code_fence_delimiters
        locs = [snippet(ctx.answer, d["start"], d["start"] + len(d["line"]), tag="여는 ```" if d["role"] == "open" else "닫는 ```")
                for d in marks]
        return _e(f"코드블록을 여닫는 ``` 표시가 {m['code_fence_delimiters']}개뿐입니다",
                  "코드블록은 여는 ``` 와 닫는 ``` 가 한 쌍(최소 2개) 있어야 합니다. "
                  + ("답변에 ``` 표시가 하나도 없습니다." if not marks else "닫는 ``` 가 빠졌습니다."),
                  "``` 표시 2개 이상", f"``` 표시 {m['code_fence_delimiters']}개", locs,
                  "코드 앞뒤를 ``` 로 한 번씩 감싸세요.", "원본 기준(채팅품질 평가기준 165행) 표시 정책입니다.")
    return _e("코드블록 여닫는 표시 정상", f"``` 표시 {m['code_fence_delimiters']}개.")


def _x_fence_imbalance(r, ctx):
    m = r["measured"]
    if r["verdict"] != FAIL:
        return _e("여닫는 표시 짝 맞음", "``` 와 $$ 표시가 각각 짝수(여는 것과 닫는 것이 한 쌍)입니다.")
    parts, locs, fixes = [], [], []
    if m["code_fence_delimiters"] % 2:
        opened = [b for b in ctx.aa.fenced_blocks if not b["closed"]]
        if opened:
            b = opened[-1]
            line = _line_info(ctx.answer, b["start"])[0]
            parts.append(f"{line}번째 줄에서 ``` 로 코드블록을 열었는데 끝까지 닫는 ``` 가 없습니다")
            locs.append(snippet(ctx.answer, b["start"], b["start"] + len(b["opening"]), tag="닫히지 않은 여는 ```"))
        else:
            parts.append(f"코드 ``` 표시가 {m['code_fence_delimiters']}개(홀수)입니다")
        fixes.append("코드가 끝나는 곳에 ``` 를 한 줄 추가하세요.")
    if m["math_delimiters"] % 2:
        pos = ctx.aa.math_delimiters
        parts.append(f"수식 표시 $$ 가 {m['math_delimiters']}개(홀수)라 여는 $$ 와 닫는 $$ 의 짝이 맞지 않습니다")
        if pos:
            locs.append(snippet(ctx.answer, pos[-1], pos[-1] + 2, tag="짝이 없는 $$ (마지막)"))
        fixes.append("수식 앞뒤를 $$ 로 한 번씩 감싸 짝을 맞추세요.")
    return _e("여닫는 표시의 짝이 맞지 않습니다", ". ".join(parts) + ". 이대로 화면에 보이면 뒤쪽 글 전체가 코드·수식으로 잘못 표시될 수 있습니다.",
              "``` 짝수 개, $$ 짝수 개 (각각)", f"``` {m['code_fence_delimiters']}개, $$ {m['math_delimiters']}개", locs,
              " ".join(fixes), "``` 와 $$ 개수를 합쳐서 보지 않고 각각 따로 봅니다 (원본 기준 170행).")


def _json_target(ctx: Ctx, tid: str):
    from .checks_integrity import json_targets
    return next((t for t in json_targets(ctx) if t["id"] == tid), None)


def _x_json(r, ctx):
    tid = r["check_id"].split(":")[1]
    name = "답변 본문의 JSON" if tid == "answer" else f"코드블록 {tid.replace('block', '')}의 JSON"
    if r["verdict"] != FAIL:
        dup = (r["measured"] or {}).get("duplicate_keys") or []
        return _e("JSON 문법 정상", f"{name}을 프로그램이 읽을 수 있습니다."
                  + (f" 다만 같은 키({', '.join(dup)})가 두 번 나옵니다 — 문법 오류는 아니지만 뒤 값이 앞 값을 덮어씁니다." if dup else ""))
    t = _json_target(ctx, tid)
    msg = r["measured"].get("error", "")
    pos = r["measured"].get("position")
    locs = []
    where = ""
    if t is not None and pos is not None:
        jl = t["text"].count("\n", 0, pos) + 1
        col = pos - (t["text"].rfind("\n", 0, pos) + 1) + 1
        base = _line_info(ctx.answer, t["start"])[0] - 1
        where = f"{name} {jl}번째 줄 {col}번째 글자 근처"
        locs.append(code_view(t["text"], jl, f"{name} · {jl}번째 줄 (답변 {base + jl}번째 줄)", base,
                              pointer=f"{col}번째 글자 근처에서 읽기 실패"))
    return _e("JSON 문법 오류가 있습니다", f"{where + ': ' if where else ''}{json_error_ko(msg)}",
              "표준 JSON 문법 (프로그램이 그대로 읽을 수 있음)", f"읽기 실패 — {json_error_ko(msg)}", locs,
              "표시된 줄의 쉼표·따옴표·괄호를 확인하세요. JSON 에는 주석·작은따옴표·마지막 쉼표를 쓸 수 없습니다.",
              f"원문 오류 메시지: {msg}")


def _x_python(r, ctx):
    tid = r["check_id"].split(":")[1]
    idx = int(tid.replace("block", "")) - 1
    blocks = ctx.aa.fenced_blocks
    if r["verdict"] != FAIL:
        return _e("파이썬 문법 정상", f"코드블록 {idx + 1}이 파이썬 문법 검사를 통과했습니다. (코드를 실행해 결과까지 확인한 것은 아닙니다)")
    msg, line = r["measured"].get("error", ""), r["measured"].get("line")
    ko = python_error_ko(msg)
    locs = []
    ans_line = None
    if 0 <= idx < len(blocks):
        b = blocks[idx]
        base = _line_info(ctx.answer, b["content_start"])[0] - 1
        ans_line = base + (line or 1)
        locs.append(code_view(b["content"], line, f"코드블록 {idx + 1} · {line}번째 줄 (답변 {ans_line}번째 줄)", base,
                              pointer="이 줄에서 문법 오류 발견"))
    return _e(f"파이썬 코드에 문법 오류가 있습니다 (코드블록 {idx + 1}, {line}번째 줄)", ko,
              "파이썬 문법대로 작성된 완성 코드", f"{line}번째 줄에서 문법 오류", locs,
              _py_fix(msg), f"코드는 실행하지 않았습니다. 원문 오류 메시지: {msg}")


def _py_fix(msg: str) -> str:
    if "indented block" in msg:
        return "해당 문(for/if/def 등) 바로 아래 줄을 공백 4칸 들여쓰세요."
    if "was never closed" in msg or "unmatched" in msg or "does not match" in msg:
        return "괄호를 여는 것과 닫는 것의 짝을 맞추세요."
    if "unterminated" in msg:
        return "문자열을 시작한 따옴표와 같은 따옴표로 닫으세요."
    if "indent" in msg:
        return "들여쓰기를 위·아래 줄과 같은 칸 수로 맞추세요 (공백과 탭을 섞지 마세요)."
    if "expected ':'" in msg:
        return "해당 줄 끝에 콜론(:)을 붙이세요."
    return "표시된 줄 근처의 문법을 확인하세요."


def _x_orphan(r, ctx):
    tags = r["measured"].get("orphan_closing_tags", [])
    target = "displayed_text" if r["scope"].startswith("displayed_text") else "answer"
    if r["verdict"] != FAIL:
        return _e("짝이 없는 닫는 태그 없음", "모든 닫는 태그에 여는 태그가 있습니다.")
    return _e(f"여는 태그 없이 닫는 태그만 남아 있습니다: {', '.join(tags[:5])}",
              f"답변에 {', '.join(tags[:5])} 같은 닫는 태그가 여는 태그 없이 들어 있습니다. 사용자 화면에 이 글자가 그대로 보이거나 "
              "내부 처리 흔적으로 보일 수 있습니다.", "짝이 없는 닫는 태그 0개", f"{len(tags)}개",
              _snips(ctx, r, "짝이 없는 닫는 태그", target), "남은 닫는 태그를 지우거나 필요한 여는 태그를 넣으세요.",
              "" if target == "displayed_text" else "실제 화면 표시본이 없어 답변 원문(코드 부분 제외)에서 찾았습니다.")


def _x_footnote(r, ctx):
    m = r["measured"]
    if r["verdict"] != FAIL:
        return _e("각주 연결 정상", f"사용한 각주 {len(m.get('used', []))}개가 모두 설명에 연결됩니다.")
    missing = [l for l in m["used"] if l not in m["defined"] and l not in m.get("empty_definitions", [])]
    parts = []
    if missing:
        parts.append(f"본문에서 각주 {', '.join(missing)} 를 달았는데 그 번호의 설명(정의)이 문서에 없습니다")
    if m.get("empty_definitions"):
        parts.append(f"각주 {', '.join(m['empty_definitions'])} 의 설명이 비어 있습니다")
    if m.get("duplicated_definitions"):
        parts.append(f"각주 {', '.join(m['duplicated_definitions'])} 의 설명이 두 번 이상 있습니다")
    return _e("각주가 설명과 연결되지 않습니다", ". ".join(parts) + ".", "사용한 각주마다 설명 1개",
              f"사용 {m['used']} / 설명 있음 {m['defined']}", _snips(ctx, r, "각주 표시"),
              "문서 아래에 `[^번호]: 설명` 형식으로 각주 설명을 추가하거나, 설명이 없는 각주 표시를 지우세요.")


def _x_citation(r, ctx):
    m = r["measured"]
    if r["verdict"] != FAIL:
        return _e("인용 번호 연결 정상", "본문의 인용 번호가 모두 출처 목록에 있습니다.")
    missing = [n for n in m["used"] if n not in m["sources"]]
    return _e("출처 목록에 없는 인용 번호가 있습니다",
              (f"본문에서 [{'], ['.join(map(str, missing))}] 를 인용했는데 출처 목록에 그 번호가 없습니다. " if missing else "")
              + (f"출처 번호 {m['duplicated_sources']} 가 중복됩니다." if m.get("duplicated_sources") else ""),
              "인용 번호마다 출처 1개", f"인용 {m['used']} / 출처 {m['sources']}", _snips(ctx, r, "인용 번호"),
              "출처 목록에 빠진 번호를 추가하거나 본문의 인용 번호를 고치세요.")


def _x_internal_link(r, ctx):
    m = r["measured"]
    if r["verdict"] != FAIL:
        return _e("문서 안 링크 정상", "문서 안 링크가 모두 실제 제목을 가리킵니다.")
    return _e("가리키는 제목이 없는 링크가 있습니다",
              (f"링크 #{', #'.join(m['missing'])} 가 가리키는 제목이 문서에 없습니다. " if m.get("missing") else "")
              + (f"#{', #'.join(m['ambiguous'])} 는 같은 이름의 제목이 여러 개라 어디로 가는지 정해지지 않습니다." if m.get("ambiguous") else ""),
              "링크마다 같은 이름의 제목 1개", f"링크 {m['links']} / 제목 {m['targets']}", _snips(ctx, r, "링크"),
              "링크 주소(#…)를 실제 제목과 맞추세요.")


def _x_empty(r, ctx):
    if r["verdict"] == FAIL:
        return _e("빈 출력을 요청했는데 내용을 출력했습니다",
                  f"질문이 아무것도 출력하지 말라고 했는데 답변에 글자 {r['measured']['content_chars']}자가 있습니다.",
                  "출력 없음", f"{r['measured']['content_chars']}자", fix="요청대로 아무것도 출력하지 않아야 합니다.")
    return _e("요청대로 출력 없음", "빈 출력 요청을 지켰습니다.")


def _x_count(r, ctx):
    kind = r["check_id"].split(":")[1]
    noun, unit = _COUNT_UNIT.get(kind, ("항목", "개"))
    req, m = r["required"], r["measured"]
    need = _need(req["op"], req["n"], unit if unit != "개" else "개")
    got = m.get("value")
    locs: List[Dict[str, Any]] = []
    if kind == "sentence_count" and m.get("sentences"):
        locs.append(item_list(f"답변을 이렇게 {len(m['sentences'])}개 문장으로 나눠 셌습니다", m["sentences"]))
    else:
        items = [ctx.answer[e["start"]:e["end"]].strip() for e in _answer_ev(r)]
        if items:
            locs.append(item_list(f"이렇게 {got}개로 셌습니다", items))
    structure = {"numbered_list": "번호 목록", "numbered_headings": "번호 붙은 제목", "bullet_list": "불릿 목록",
                 "table_data_rows": "표의 데이터 행"}.get(m.get("structure"), "")
    how = {"bullet_count": "맨 앞이 -, *, + 인 가장 바깥 목록 줄 (안쪽으로 들여쓴 하위 불릿은 제외)",
           "sentence_count": "마침표·물음표·느낌표로 끝나는 문장 (코드 제외, 소수점·약어·링크 안 점은 문장 끝으로 보지 않음)",
           "paragraph_count": "빈 줄로 구분된 덩어리 (줄바꿈만 하고 빈 줄이 없으면 한 문단)",
           "line_count": "코드를 뺀 비어 있지 않은 줄", "word_count": "띄어쓰기로 나뉜 덩어리(어절)",
           "list_count": f"답변의 {structure or '목록'}"}.get(kind, "")
    if r["verdict"] == FAIL:
        diff = f" ({abs(got - req['n'])}{unit if unit != '개' else '개'} {'많음' if got > req['n'] else '부족'})" if req["op"] == "==" else ""
        return _e(f"{noun} 수가 요구와 다릅니다 (요구 {need}, 실제 {_n(got)}{unit})",
                  f"질문은 {noun} 수를 {need}(으)로 요구했는데, 답변은 {_n(got)}{unit}입니다{diff}.".replace(f"{need}(으)로", _j(need, "으로")),
                  need, f"{_n(got)}{unit}", locs,
                  f"{noun} 수를 {_j(need, '으로')} 맞추세요.", f"세는 기준: {how}")
    return _e(f"{noun} 수 맞음 ({_n(got)}{unit})", f"요구({need})를 지켰습니다.", need, f"{_n(got)}{unit}", locs,
              note=f"세는 기준: {how}")


def _x_chars_count(r, ctx):
    req, got = r["required"], r["measured"]["chars"]
    need = _need(req["op"], req["n"], "자")
    rule = "코드와 줄바꿈을 뺀 글자 수" + (", 공백 제외" if req.get("exclude_whitespace") else ", 공백 포함")
    if r["verdict"] == FAIL:
        over = got - req["n"]
        return _e(f"글자 수가 요구를 벗어났습니다 (요구 {need}, 실제 {_n(got)}자)",
                  f"질문은 {_j(need, '을를')} 요구했는데 답변은 {_n(got)}자입니다" + (f" ({_n(over)}자 초과)." if over > 0 else f" ({_n(-over)}자 부족)."),
                  need, f"{_n(got)}자", fix=f"분량을 {_j(need, '으로')} 맞추세요.", note=f"세는 기준: {rule}")
    return _e(f"글자 수 맞음 ({_n(got)}자)", f"요구({need})를 지켰습니다.", need, f"{_n(got)}자", note=f"세는 기준: {rule}")


def _x_literal(r, ctx):
    kind = r["check_id"].split(":")[1]
    lit = r["required"]["literal"] if isinstance(r["required"], dict) else ""
    m = r["measured"]
    exact = bool(r["required"].get("exact")) if isinstance(r["required"], dict) else False  # 과거 결과에는 키가 없다
    if kind == "required_literal":
        if r["verdict"] == FAIL:
            return _e(f"넣으라고 한 {_j(f'‘{lit}’', '이가')} 답변에 없습니다", f"질문이 {_j(f'‘{lit}’', '을를')} 넣으라고 했는데 답변 어디에도 없습니다.",
                      f"‘{lit}’ 1번 이상", "0번",
                      fix=(f"답변에 {_j(f'‘{lit}’', '을를')} 그대로(띄어쓰기까지 같게) 넣으세요." if exact else
                           f"답변에 {_j(f'‘{lit}’', '을를')} 넣으세요 (띄어쓰기·줄바꿈 차이는 같은 것으로 봅니다)."),
                      note=("코드 부분은 빼고, 굵게(**) 같은 강조 표시는 지운 뒤 띄어쓰기까지 원문 그대로 비교했습니다." if exact else
                            "코드 부분은 빼고, 굵게(**) 같은 강조 표시는 지우고, 연속 공백·줄바꿈은 공백 하나로 보고 찾았습니다."))
        return _e(f"‘{lit}’ 포함", f"{_j(f'‘{lit}’', '이가')} {m['occurrences']}번 있습니다.")
    if kind == "forbidden_literal":
        if r["verdict"] == FAIL:
            return _e(f"쓰지 말라고 한 {_j(f'‘{lit}’', '이가')} {m['occurrences']}번 나옵니다",
                      f"질문이 {_j(f'‘{lit}’', '을를')} 쓰지 말라고 했는데 답변에 {m['occurrences']}번 나옵니다. 부정문·인용 안에 있어도 나온 것으로 셉니다.",
                      "0번", f"{m['occurrences']}번", _snips(ctx, r, f"금지 표현 ‘{lit}’"),
                      f"{_j(f'‘{lit}’', '이가')} 들어간 부분을 다른 표현으로 바꾸세요.")
        return _e(f"‘{lit}’ 사용 안 함", f"쓰지 말라고 한 {_j(f'‘{lit}’', '이가')} 나오지 않습니다.")
    head_txt = f"‘{m.get('answer_start', '')}…’"
    tail_txt = f"‘…{m.get('answer_end', '')}’"
    if kind == "starts_with":
        if r["verdict"] == FAIL:
            return _e(f"답변이 {_j(f'‘{lit}’', '으로')} 시작하지 않습니다", f"질문은 {_j(f'‘{lit}’', '으로')} 시작하라고 했는데 답변은 {_j(head_txt, '으로')} 시작합니다.",
                      f"{_j(f'‘{lit}’', '으로')} 시작", f"‘{m.get('answer_start', '')}…’", fix=f"답변 첫머리를 {_j(f'‘{lit}’', '으로')} 바꾸세요.")
        return _e(f"{_j(f'‘{lit}’', '으로')} 시작함", "시작 표현 요구를 지켰습니다.")
    if r["verdict"] == FAIL:
        return _e(f"답변이 {_j(f'‘{lit}’', '으로')} 끝나지 않습니다", f"질문은 {_j(f'‘{lit}’', '으로')} 끝내라고 했는데 답변은 {_j(tail_txt, '으로')} 끝납니다.",
                  f"{_j(f'‘{lit}’', '으로')} 끝남", f"‘…{m.get('answer_end', '')}’", fix=f"답변 마지막을 {_j(f'‘{lit}’', '으로')} 바꾸세요.")
    return _e(f"{_j(f'‘{lit}’', '으로')} 끝남", "끝 표현 요구를 지켰습니다.")


def _x_charset(r, ctx):
    cs = r["required"]["charset"]
    m = r["measured"]
    if r["verdict"] == FAIL:
        return _e(f"{_j(cs, '이가')} 아닌 글자가 섞였습니다", f"질문이 {cs}만 쓰라고 했는데 다른 글자가 {m['violating_letters']}자 있습니다 (예: {' '.join(m['examples'][:10])}).",
                  f"{cs}만", f"다른 글자 {m['violating_letters']}자", _snips(ctx, r, "허용 밖 글자"),
                  f"표시된 글자를 {_j(cs, '으로')} 바꾸세요.", "숫자·문장부호·띄어쓰기는 검사하지 않습니다.")
    return _e(f"{cs}만 사용", "허용 문자 요구를 지켰습니다.")


def _table(ctx):
    t = ctx.aa.tables
    return t[0] if len(t) == 1 else None


def _x_table(r, ctx):
    kind = r["check_id"].split(":")[1]
    m, req = r["measured"] if isinstance(r["measured"], dict) else {}, r["required"]
    t = _table(ctx)
    rows = item_list("답변 표의 데이터 행 (헤더 제외)", [" | ".join(x) for x in t.rows]) if t else None
    if kind == "required":
        if r["verdict"] == FAIL:
            pipes = m.get("pipe_lines_without_table_syntax", 0)
            return _e("요구한 표가 없습니다", "질문이 표로 정리해 달라고 했는데 답변에 표가 없습니다."
                      + (f" ‘|’ 로 나눈 줄 {pipes}개는 있지만 표 머리글 아래 |---| 구분선이 없어 표로 보이지 않습니다." if pipes else ""),
                      "표 1개 이상", "표 0개", fix="| 열1 | 열2 | 와 그 아래 |---|---| 줄을 넣어 Markdown 표로 만드세요.")
        return _e("표 있음", "요구한 표가 있습니다.")
    if kind == "forbidden":
        if r["verdict"] == FAIL:
            return _e("쓰지 말라고 한 표를 썼습니다", "질문이 표를 쓰지 말라고 했는데 답변에 표가 있습니다.", "표 0개",
                      f"표 {len(m.get('tables', []))}개", fix="표 대신 문장이나 목록으로 쓰세요.")
        return _e("표 없음 (요구대로)", "요구대로 표를 쓰지 않았습니다.")
    if kind == "columns":
        want, have = req["columns"], m.get("headers", [])
        if r["verdict"] == FAIL:
            missing = [c for c in want if c not in have]
            return _e("표의 열 이름이 요구와 다릅니다",
                      f"질문이 요구한 열은 {' → '.join(want)} 인데 답변 표의 열은 {' → '.join(have) or '(헤더 없음)'} 입니다."
                      + (f" 빠진 열: {', '.join(missing)}." if missing else " 순서가 다릅니다." if req.get("ordered") else ""),
                      " → ".join(want), " → ".join(have) or "헤더 없음", fix="표 첫 줄(헤더)의 열 이름·순서를 요구대로 맞추세요.")
        return _e("열 이름 맞음", f"열: {' → '.join(have)}")
    if kind == "column_count":
        return _e("표 열 수 " + ("다름" if r["verdict"] == FAIL else "맞음"), f"질문 요구 {_need(req['op'], req['n'])}, 실제 {m.get('columns')}개.",
                  _need(req["op"], req["n"]), f"{m.get('columns')}개")
    if kind == "header":
        if r["verdict"] == FAIL:
            s = "헤더 없이 달라고 했는데 헤더 행이 있습니다." if req.get("header_rows") == 0 else "헤더(열 이름 행)를 넣으라고 했는데 없습니다."
            return _e("표 헤더가 요구와 다릅니다", s, fix="표 첫 줄(헤더)을 요구대로 넣거나 빼세요.")
        return _e("표 헤더 맞음", "헤더 요구를 지켰습니다.")
    basis = "데이터 행(헤더 제외)" if kind == "table_data_rows" else "전체 행(헤더 포함)"
    got = m.get("data_rows") if kind == "table_data_rows" else m.get("total_rows")
    need = _need(req["op"], req["n"], "행")
    if r["verdict"] == FAIL:
        return _e(f"표의 {basis} 수가 요구와 다릅니다 (요구 {need}, 실제 {got}행)",
                  f"질문은 표의 {_j(basis, '을를')} {need} 요구했는데 답변 표는 헤더 {m.get('header_rows')}행 + 데이터 {m.get('data_rows')}행 입니다.",
                  need, f"{got}행", [rows], f"표의 {_j(basis, '을를')} {_j(need, '으로')} 맞추세요.",
                  "Markdown 표의 |---| 구분선은 행으로 세지 않습니다.")
    return _e(f"표 행 수 맞음 ({got}행)", f"요구({need})를 지켰습니다.")


def _x_list(r, ctx):
    kind = r["check_id"].split(":")[1]
    m = r["measured"] if isinstance(r["measured"], dict) else {}
    if kind == "forbidden":
        if r["verdict"] == FAIL:
            items = [x.line.text.strip() for x in ctx.aa.list_items]
            return _e("쓰지 말라고 한 목록 형식을 썼습니다", f"질문이 목록으로 나열하지 말라고 했는데 목록 줄이 {len(items)}개 있습니다.",
                      "목록 줄 0개", f"{len(items)}개", [item_list("목록으로 쓴 줄", items)], "목록 대신 이어지는 문단으로 쓰세요.")
        return _e("목록 안 씀 (요구대로)", "요구대로 목록을 쓰지 않았습니다.")
    style = {"bullet": "불릿(- ) 목록", "numbered": "번호(1. ) 목록"}.get((r["required"] or {}).get("style"), "목록")
    if r["verdict"] == FAIL:
        return _e(f"요구한 {_j(style, '이가')} 없습니다", f"질문이 {_j(style, '으로')} 정리해 달라고 했는데 답변에 {_j(style, '이가')} 없습니다.",
                  style, f"불릿 {m.get('bullet_items', 0)}줄 · 번호 {m.get('numbered_items', 0)}줄",
                  fix=f"줄 맨 앞에 {'- ' if 'bullet' in style or '불릿' in style else '1. '}를 붙여 목록으로 쓰세요.")
    return _e(f"{style} 사용", "요구한 목록 형식을 썼습니다.")


def _x_heading(r, ctx):
    m = r["measured"] if isinstance(r["measured"], dict) else {}
    heads = m.get("headings", [])
    if r["verdict"] == FAIL:
        if r["check_id"] == "2-2.heading" and isinstance(r["required"], dict) and "n" in r["required"]:
            need = _need(r["required"]["op"], r["required"]["n"])
            return _e(f"제목 수가 요구와 다릅니다 (요구 {need}, 실제 {len(heads)}개)", f"질문은 제목을 {need} 요구했는데 {len(heads)}개입니다.",
                      need, f"{len(heads)}개", [item_list("답변의 제목", heads)], "제목(# …) 수를 요구대로 맞추세요.")
        return _e("요구한 제목이 없습니다", "질문이 제목을 붙이라고 했는데 # 로 시작하는 제목 줄이 없습니다.", "제목 1개 이상", "0개",
                  fix="줄 맨 앞에 # 또는 ## 를 붙여 제목을 만드세요.")
    return _e("제목 요구 충족", f"제목 {len(heads)}개.")


def _x_json_struct(r, ctx):
    kind = r["check_id"].split(":")[1]
    m = r["measured"] if isinstance(r["measured"], dict) else {}
    req = r["required"]
    if r["verdict"] != FAIL:
        return _e("JSON 구조 요구 충족", r["reason"])
    if kind == "present":
        return _e("요구한 JSON 출력이 없습니다", "질문이 JSON 으로 달라고 했는데 JSON(코드블록 또는 { } / [ ] 로 된 본문)이 없습니다.",
                  "JSON 출력", "없음", fix="결과를 JSON 형식으로 출력하세요.")
    if kind == "top_level":
        ko = {"object": "객체({ })", "array": "배열([ ])", "string": "문자열", "number": "숫자"}
        return _e("JSON 의 최상위 형태가 다릅니다", f"질문은 {ko.get(req['top_level'], req['top_level'])}을 요구했는데 답변은 {ko.get(m['top_level'], m['top_level'])} 입니다.",
                  ko.get(req["top_level"], ""), ko.get(m["top_level"], ""), fix="JSON 전체를 요구한 형태로 감싸세요.")
    if kind == "keys":
        miss = m.get("missing", [])
        return _e("JSON 에 필수 키가 빠졌습니다",
                  "필수 키 " + ", ".join(sorted({x['key'] for x in miss})) + " 가 " + ("객체에 없습니다." if m.get("objects", 1) == 1 else f"{len({x['index'] for x in miss})}개 항목에서 빠졌습니다."),
                  ", ".join(req["keys"]), "누락 " + ", ".join(f"{x['index'] + 1}번째 항목의 {x['key']}" for x in miss[:5]),
                  fix="빠진 키를 추가하세요.")
    if kind.startswith("type"):
        ko = {"integer": "정수", "number": "숫자", "string": "문자열(따옴표)", "boolean": "true/false", "array": "배열", "object": "객체", "null": "null"}
        return _e(f"JSON 값의 종류가 다릅니다 (‘{req['key']}’)", f"‘{req['key']}’ 값은 {ko.get(req['type'], req['type'])}여야 하는데 {', '.join(m.get('types', []))} 입니다."
                  + (" 숫자를 따옴표로 감싸면 문자열이 됩니다." if req["type"] in ("integer", "number") and "string" in m.get("types", []) else ""),
                  ko.get(req["type"], ""), ", ".join(m.get("types", [])), fix=f"‘{req['key']}’ 값을 {ko.get(req['type'], req['type'])}로 쓰세요.")
    return _e("JSON 구조가 요구와 다릅니다", r["reason"])


def _fmt_frac(v: Any) -> str:
    return _n(v)


def _x_qcalc(r, ctx):
    req, m = r["required"], r["measured"]
    ops = req.get("operands", [])
    op = req.get("operation", "")
    formula = f" {op.replace('*', '×').replace('/', '÷')} ".join(ops) if op in ("+", "-", "*", "/") and ops else op
    link = {"label": f"‘{m.get('label', '')}’ 라벨 뒤의 값", "equation": "질문 숫자로 된 답변 계산식의 결과",
            "whole_answer_number": "답변 전체(숫자 하나)"}.get(m.get("link"), "")
    if r["verdict"] == FAIL:
        return _e(f"계산 결과가 틀렸습니다 (정답 {_n(req['expected'])}, 답변 {_n(m['claimed'])})",
                  f"질문의 숫자로 직접 계산하면 {formula} = {_n(req['expected'])} 인데, 답변은 {_n(m['claimed'])} 이라고 했습니다.",
                  f"{formula} = {_n(req['expected'])}", _n(m["claimed"]), _snips(ctx, r, "답변의 값"),
                  f"값을 {_j(_n(req['expected']), '으로')} 고치세요.", f"답변 값을 찾은 방법: {link}. 답변 안의 계산식이 스스로 맞는지는 '답변 등식' 항목에서 따로 봅니다.")
    return _e(f"계산 결과 맞음 ({_n(m['claimed'])})", f"{formula} = {_n(req['expected'])} 와 같습니다.")


def _x_equation(r, ctx):
    segs = r["measured"].get("segments", [])
    vals = r["required"].get("recomputed", [])
    shown = " = ".join(segs)
    if r["verdict"] == FAIL:
        pairs = [f"‘{s}’ → {_n(v)}" for s, v in zip(segs, vals)]
        return _e(f"답변의 계산식이 틀렸습니다: {shown}",
                  "답변에 적힌 식을 다시 계산하면 양쪽 값이 다릅니다: " + " / ".join(pairs) + ".",
                  "등호 양쪽 값이 같음", " ≠ ".join(_n(v) for v in vals), _snips(ctx, r, "계산식"),
                  "계산 결과를 다시 확인해 고치세요.",
                  "틀린 예시를 일부러 보여 주는 문맥(‘틀린 식’, ‘올바른 식:’ 등)은 검사에서 뺍니다.")
    return _e(f"계산식 맞음: {shown}", "다시 계산해도 같은 값입니다.")


def _x_declared(r, ctx):
    d, m = r["required"]["declared"], r["measured"]
    kind = _STRUCT_KO.get(m.get("structure"), m.get("structure", ""))
    items = [re.sub(r"^(?:#{1,6}\s*|[-*+]\s+|\d{1,3}[.)]\s+)", "", x["text"]).replace("**", "")
             for x in (r.get("details") or {}).get("counted_items", [])]
    sentence = (r.get("details") or {}).get("declaration_sentence", "")
    if r["verdict"] == FAIL:
        more = m["count"] - d
        return _e(f"답변이 말한 개수({d}개)와 실제 {kind} 수({m['count']}개)가 다릅니다",
                  f"답변은 “{sentence[:80]}” 라며 {d}개라고 했지만, 바로 이어지는 {_j(kind, '은는')} {m['count']}개입니다 "
                  f"({abs(more)}개 {'더 많음' if more > 0 else '부족'}).",
                  f"{d}개", f"{m['count']}개", _snips(ctx, r, "개수 선언")[:1] + [item_list(f"바로 이어지는 {kind} {m['count']}개 (색칠한 항목이 선언보다 많은 부분)" if more > 0 else f"바로 이어지는 {kind} {m['count']}개", items,
                                                                                 mark=list(range(d, len(items))) if more > 0 else None)],
                  f"선언한 숫자를 {_j(str(m['count']), '으로')} 고치거나 {kind} 수를 {d}개로 맞추세요.",
                  "목록·표·소제목의 끝은 선언과 상관없이 구조로 정합니다 (선언 개수에 맞춰 잘라 세지 않음).")
    return _e(f"선언 개수 맞음 ({d}개)", f"선언한 {d}개와 {kind} 수가 같습니다.")


def _x_transform(r, ctx):
    if r["verdict"] == FAIL:
        req, m = r["required"], r["measured"]
        ops_ko = {"filter": "조건으로 거르기", "dedupe": "중복 제거", "sort": "정렬", "rename": "키 이름 바꾸기"}
        ops = ", ".join(ops_ko.get(o["op"], o["op"]) for o in req.get("ops", [])) or "원본 유지"
        return _e("원본 자료를 요청대로 바꾼 결과와 답변이 다릅니다",
                  f"질문에 준 원본에 {_j(ops, '을를')} 그대로 적용한 결과와 답변을 비교했더니 다릅니다: {r['reason']}",
                  f"{len(req.get('expected', []))}개 항목", f"{len(m.get('output', []))}개 항목",
                  [item_list("기대한 결과 (원본에서 직접 계산)", [str(x) for x in req.get("expected", [])]),
                   item_list("답변의 결과", [str(x) for x in m.get("output", [])])],
                  "원본 값·자료형(숫자와 문자열 구분)·순서를 요청대로 맞추세요.",
                  "0, false, 빈 문자열, null, 값 없음은 모두 다른 값으로 봅니다.")
    return _e("원본 변환 결과 일치", "원본에 요청 작업을 적용한 결과와 같습니다.")


def _x_unique(r, ctx):
    if r["verdict"] == FAIL:
        return _e("중복된 항목이 있습니다", f"질문이 중복 없이 달라고 했는데 같은 항목이 {len(r['measured']['duplicate_indexes'])}번 더 나옵니다.",
                  "중복 0개", f"중복 {len(r['measured']['duplicate_indexes'])}개", fix="중복된 항목을 지우세요.")
    return _e("중복 없음", "같은 항목이 두 번 나오지 않습니다.")


def _x_aggregate(r, ctx):
    if r["verdict"] == FAIL:
        return _e("집계 값이 틀렸습니다", r["reason"], _n(r["required"]["expected"]), r["measured"]["claimed"],
                  _snips(ctx, r, "답변의 값"), "원본 자료로 다시 계산해 고치세요.")
    return _e("집계 값 맞음", r["reason"])


def _x_marker(r, ctx):
    if r["verdict"] == FAIL:
        return _e("AIONA 내부 표시가 사용자 화면에 노출됐습니다", r["reason"], "노출 0건", f"{r['measured']['hits']}건",
                  _snips(ctx, r, "내부 표시", target="displayed_text"), "내부 자리표시자·태그가 화면에 남지 않도록 후처리를 확인하세요.")
    return _e("내부 표시 노출 없음", r["reason"])


def _x_forbidden_expr(r, ctx):
    if r["verdict"] == FAIL:
        lit = r["required"]["forbidden"]
        return _e(f"이 질문에서 나오면 안 되는 표현 {_j(f'‘{lit}’', '이가')} 나왔습니다",
                  f"미리 등록해 둔 금지 표현 {_j(f'‘{lit}’', '이가')} 화면 답변에 {r['measured']['occurrences']}번 나옵니다. 부정문 안에 있어도 나온 것으로 셉니다.",
                  "0번", f"{r['measured']['occurrences']}번", _snips(ctx, r, "금지 표현", target="displayed_text"),
                  "근거 없는 내용(환각)이 나왔는지 답변을 확인하세요.")
    return _e("금지 표현 없음", r["reason"])


_BY_BASE: Dict[str, Callable] = {
    "1-1.response_nonempty": _x_response, "1-1.character_rules": _x_chars, "1-1.foreign_script": _x_foreign,
    "1-2.code_block": _x_code_block, "1-2.code_language_label": _x_lang, "1-2.code_fence_min": _x_fence_min,
    "1-2.fence_imbalance": _x_fence_imbalance, "1-2.json_syntax": _x_json, "1-2.python_syntax": _x_python,
    "1-2.orphan_closing_tag": _x_orphan, "1-3.footnote_link": _x_footnote, "1-3.citation_link": _x_citation,
    "1-3.internal_link": _x_internal_link, "2-1.explicit_empty": _x_empty, "2-1.count": _x_count,
    "2-1.char_count": _x_chars_count, "2-1.literal": _x_literal, "2-1.charset": _x_charset,
    "2-2.table": _x_table, "2-2.list": _x_list, "2-2.heading": _x_heading, "2-2.json_structure": _x_json_struct,
    "3-1.question_calculation": _x_qcalc, "3-1.answer_equation": _x_equation, "3-2.declared_count": _x_declared,
    "3-3.transform": _x_transform, "3-3.unique_values": _x_unique, "3-3.aggregate": _x_aggregate,
    "AIONA-M.internal_marker": _x_marker, "AIONA-F.forbidden_expression": _x_forbidden_expr,
}


def explain_result(r: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    base = _base(r["check_id"])
    checked = CHECKED.get(base, r["title"])
    requirement = _q(r)
    if r["verdict"] is None:
        out = _e(STATUS_HEADLINE.get(r["status"], "판정하지 않음"), r["reason"], note=STATUS_NOTE.get(r["status"], ""))
    elif (r.get("details") or {}).get("absent"):
        # 답변 구조 검사의 '검사 대상 없음' 행 — 대상별 설명 함수는 대상이 있다고 가정하므로 여기서 끝낸다
        excluded = (r.get("measured") or {}).get("excluded") or 0
        out = _e("검사 대상 없음 — 위반 없음" if not excluded else "판정 제외 대상만 있음 — 확인된 위반 없음", r["reason"],
                 note="답변에 이 구조가 없으면 검사할 것도, 위반도 없습니다. 질문이 같으면 모델마다 조건 수가 같도록 "
                      "답변마다 조건 1개로 세기 때문에 PASS 행을 둡니다.")
    else:
        fn = _BY_BASE.get(base)
        try:
            out = fn(r, ctx) if fn else None
        except Exception:  # 설명 생성 실패가 판정을 바꾸지 않게 한다
            out = None
        if out is None:
            out = _e(r["title"] + (" — 문제 있음" if r["verdict"] == FAIL else " — 통과"), r["reason"])
    out["checked"] = checked
    out["requirement"] = requirement
    out["version"] = EXPLAIN_VERSION
    return out
