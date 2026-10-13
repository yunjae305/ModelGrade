"""질문·답변 구조 분석. 한 번 읽어 여러 검사가 공유한다.

사용하는 파싱 도구 (``textparse``)
- 줄·코드 펜스 판별, 목록·제목·문단·GFM 표·문장 분리
- ``_fenced_code_blocks`` : CommonMark 펜스 코드 구획
- ``scannable_text`` : 코드 제외·강조 제거 투영

같은 입력 범위·같은 규칙이면 결과를 캐시하고, 범위가 다르면 (원문 / 표시본 / 투영) 따로 만든다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .textparse import (
    Line,
    ListItem,
    _fenced_code_blocks,
    content_lines,
    find_headings,
    find_list_items,
    find_markdown_tables,
    find_paragraphs,
    scannable_text,
    split_lines,
    split_sentences,
    top_level_list_items,
)

__all__ = ["TextAnalysis", "TableInfo", "ANALYSIS_VERSION", "mask_ranges"]

ANALYSIS_VERSION = "v16-analysis-1.4"

_INLINE_RUN = re.compile(r"`+")
_MATH_DELIM = re.compile(r"\$\$")
_BOLD_ONLY_LINE = re.compile(r"^\s*(\*\*|__)[^*_\n]{1,80}\1\s*:?\s*$")


def mask_ranges(text: str, ranges: Sequence[Tuple[int, int]], fill: str = " ") -> str:
    """주어진 범위를 같은 길이의 채움 문자로 가린다 (위치 보존)."""
    chars = list(text)
    for start, end in ranges:
        for i in range(max(0, start), min(len(chars), end)):
            if chars[i] != "\n":
                chars[i] = fill
    return "".join(chars)


#: 사용자가 대화창에 붙여 넣은 긴 글의 표시 — 질문 생성기가 J6 턴을 10K 토큰 이상으로 만들 때 요청 앞에 붙인다.
#: 붙여 넣은 글은 사용자가 준 자료이지 AI 에게 한 지시가 아니다 — 요구 조건·지속 지시·사실 질의·진위 요청은 그 밖에서만 찾고,
#: 값 대조(J5 입력 일치·3-3 원본)는 자료까지 본다 (2026-10-14 실제 수집: 자료 속 '~라고 기록하지 않는다' 가 필수 문자열 요구로 읽힘).
PASTE_BEGIN = "[붙여 넣은 자료]"
PASTE_END = "[자료 끝]"


#: 표시는 독립된 줄로만 인식한다 — 요청 문장 안에서 표시 문자열을 언급해도 구간이 생기지 않는다 (Codex 리뷰)
_PASTE_BEGIN_LINE = re.compile(r"^[ \t]*" + re.escape(PASTE_BEGIN) + r"[ \t]*\r?$", re.M)
_PASTE_END_LINE = re.compile(r"^[ \t]*" + re.escape(PASTE_END) + r"[ \t]*\r?$", re.M)
_PASTE_MARK_LINE = re.compile(r"^([ \t]*)(" + re.escape(PASTE_BEGIN) + "|" + re.escape(PASTE_END) + r")([ \t]*\r?)$", re.M)


def pasted_ranges(text: str) -> List[Tuple[int, int]]:
    """``[붙여 넣은 자료]`` 줄부터 ``[자료 끝]`` 줄까지의 구간들 (위치). 끝 표시 줄이 없는 시작 표시는 구간을 만들지 않는다 —
    마지막 문단만 요청이라고 추측하면 여러 문단 요청을 삼킨다 (끝 표시가 없는 v1 세트는 저장된 계획으로 변환해 둔다)."""
    out: List[Tuple[int, int]] = []
    cursor = 0
    while True:
        begin = _PASTE_BEGIN_LINE.search(text, cursor)
        if begin is None:
            return out
        end = _PASTE_END_LINE.search(text, begin.end())
        if end is None:
            return out
        following = _PASTE_BEGIN_LINE.search(text, begin.end())
        if following is not None and following.start() < end.start():   # 끝 표시 전에 다른 시작 표시 — 앞 구간은 미완성, 다음 시작부터 다시
            cursor = following.start()
            continue
        out.append((begin.start(), end.end()))
        cursor = end.end()


def escape_paste_markers(material: str) -> str:
    """자료 본문 안에 표시와 같은 줄이 있으면 '※ ' 를 붙여 표시로 읽히지 않게 한다 (구간이 조기에 끝나지 않도록)."""
    return _PASTE_MARK_LINE.sub(lambda m: f"{m.group(1)}※ {m.group(2)}{m.group(3)}", material)


def seal_pasted(text: str) -> str:
    """자료 구간 안에서 닫히지 않은 코드 펜스를 구간 끝에서 닫은 글 (길이·위치 보존) — 변환 원본(배열·표) 분석 전용.

    구간 본문을 실제 펜스 파서(``_fenced_code_blocks``)로 읽어 마지막 블록이 열려 있으면 끝 표시 줄을 같은 글자·길이의 닫는 펜스로
    바꾼다(같은 길이 공백 채움). 끝 표시 줄보다 긴 펜스는 닫을 수 없으므로 그 여는 펜스 줄을 공백으로 지운다 — 자료 속 닫히지 않은
    펜스는 어느 쪽이든 구간 밖으로 번지지 않는다 (Codex 리뷰). 끝 표시가 사라지므로 지시 추출에는 쓰지 않는다 — 지시 구간은 원문의
    ``pasted_ranges`` 로 가린다."""
    chars = list(text)
    for start, end in pasted_ranges(text):
        marker = _PASTE_END_LINE.search(text, start, end)
        if marker is None:
            continue
        blocks = _fenced_code_blocks(text[start:marker.start()])
        if not blocks or blocks[-1].get("closed", True):
            continue
        closer = str(blocks[-1]["marker"]) * int(blocks[-1]["length"])
        line_start = marker.start()
        line_end = marker.end() - (1 if text[marker.end() - 1:marker.end()] == "\r" else 0)
        if line_end - line_start >= len(closer):
            chars[line_start:line_end] = list(closer + " " * (line_end - line_start - len(closer)))
        else:   # 긴 펜스 — 여는 줄을 지워 펜스 자체를 없앤다 (줄바꿈은 둔다)
            open_start = start + int(blocks[-1]["start"])
            open_end = open_start + len(str(blocks[-1].get("opening") or "").rstrip("\r\n"))
            chars[open_start:open_end] = [" "] * (open_end - open_start)
    return "".join(chars)


@dataclass
class TableInfo:
    kind: str                       # markdown | html
    start: int
    end: int
    header_rows: int
    data_rows: int
    headers: Tuple[str, ...]
    rows: Tuple[Tuple[str, ...], ...]

    @property
    def total_rows(self) -> int:
        return self.header_rows + self.data_rows

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "start": self.start, "end": self.end,
                "header_rows": self.header_rows, "data_rows": self.data_rows,
                "total_rows": self.total_rows, "headers": list(self.headers),
                "column_count": len(self.headers) if self.headers else
                (len(self.rows[0]) if self.rows else 0)}


class _HtmlTables(HTMLParser):
    """HTML 표 구조만 모은다. 스크립트·리소스를 실행하거나 불러오지 않는다."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: List[Dict[str, Any]] = []
        self._stack: List[Dict[str, Any]] = []
        self._row: Optional[List[Tuple[str, bool]]] = None
        self._cell: Optional[List[str]] = None
        self._cell_th = False
        self._section = ""

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append({"rows": [], "start": self.getpos()})
        elif not self._stack:
            return
        elif tag in ("thead", "tbody", "tfoot"):
            self._section = tag
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell, self._cell_th = [], tag == "th"

    def handle_endtag(self, tag):
        if not self._stack:
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(("".join(self._cell).strip(), self._cell_th))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._stack[-1]["rows"].append((self._section, self._row))
            self._row = None
        elif tag in ("thead", "tbody", "tfoot"):
            self._section = ""
        elif tag == "table":
            self.tables.append(self._stack.pop())

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _html_tables(text: str) -> List[TableInfo]:
    if "<table" not in text.lower():
        return []
    parser = _HtmlTables()
    parser.feed(text)
    parser.close()
    out: List[TableInfo] = []
    for table in parser.tables:
        header: List[Tuple[str, ...]] = []
        data: List[Tuple[str, ...]] = []
        for section, row in table["rows"]:
            cells = tuple(c for c, _ in row)
            is_header = section == "thead" or (row and all(th for _, th in row))
            (header if is_header and not data else data).append(cells)
        out.append(TableInfo(kind="html", start=-1, end=-1, header_rows=len(header),
                             data_rows=len(data), headers=header[0] if header else (),
                             rows=tuple(data)))
    return out


def _clean_cell(cell: str) -> str:
    cell = cell.strip()
    cell = re.sub(r"^(\*\*|__)(.*)\1$", r"\2", cell)
    cell = re.sub(r"^`(.*)`$", r"\1", cell)
    return cell.strip()


class TextAnalysis:
    """하나의 텍스트(답변·질문·표시본)에 대한 구조 분석 캐시."""

    def __init__(self, text: str) -> None:
        self.text = text

    # --- 줄·코드 ---------------------------------------------------------------
    @cached_property
    def lines(self) -> List[Line]:
        return split_lines(self.text)

    @cached_property
    def fenced_blocks(self) -> List[Dict[str, Any]]:
        blocks = _fenced_code_blocks(self.text)
        for block in blocks:
            info = block.get("info", "")
            block["language_label"] = info.split()[0].lower() if info.split() else ""
        return blocks

    @cached_property
    def fence_ranges(self) -> List[Tuple[int, int]]:
        return [(b["start"], b["end"]) for b in self.fenced_blocks]

    def in_fence(self, index: int) -> bool:
        return any(s <= index < e for s, e in self.fence_ranges)

    @cached_property
    def inline_code_spans(self) -> List[Tuple[int, int]]:
        """펜스 밖 한 줄 안에서 같은 길이의 백틱 묶음으로 닫힌 인라인 코드 (CommonMark 의 한 줄 부분집합)."""
        spans: List[Tuple[int, int]] = []
        for line in self.lines:
            if line.in_code:
                continue
            runs = list(_INLINE_RUN.finditer(line.text))
            used = set()
            for i, run in enumerate(runs):
                if i in used:
                    continue
                for j in range(i + 1, len(runs)):
                    if j not in used and len(runs[j].group(0)) == len(run.group(0)):
                        spans.append((line.start + run.start(), line.start + runs[j].end()))
                        used.update({i, j})
                        break
        return spans

    @cached_property
    def code_exclusion_ranges(self) -> List[Tuple[int, int]]:
        return sorted(self.fence_ranges + self.inline_code_spans)

    @cached_property
    def code_fence_delimiters(self) -> List[Dict[str, Any]]:
        """코드 구획 표시(여는 줄·닫는 줄) 목록. 블록 수가 아니라 표시 수다."""
        marks = []
        for block in self.fenced_blocks:
            marks.append({"role": "open", "start": block["start"], "line": block["opening"].strip()})
            if block["closed"]:
                content_end = block["content_start"] + len(block["content"])
                marks.append({"role": "close", "start": content_end,
                              "line": self.text[content_end:block["end"]].strip()})
        return marks

    @cached_property
    def math_delimiters(self) -> List[int]:
        """코드 밖 ``$$`` 표시 위치. 한 번 나올 때마다 표시 1개로 센다."""
        masked = mask_ranges(self.text, self.code_exclusion_ranges)
        return [m.start() for m in _MATH_DELIM.finditer(masked)]

    # --- 본문 투영 -------------------------------------------------------------
    @cached_property
    def projection(self) -> Tuple[str, List[int]]:
        """코드 구획·인라인 코드 제외, 강조 마커 제거 (``scannable_text`` 투영)."""
        return scannable_text(self.text)

    @cached_property
    def body_without_code(self) -> str:
        """글자 수 정의: 코드 펜스 밖 줄을 줄바꿈 없이 이은 본문."""
        return "".join(line.text for line in content_lines(self.text, self.lines))

    @cached_property
    def inline_masked(self) -> str:
        """인라인 코드 안의 부호가 문장 경계로 오인되지 않도록 같은 길이의 문자로 가린 원문."""
        return mask_ranges(self.text, self.inline_code_spans, fill="x")

    # --- 목록·제목·문단·문장 ----------------------------------------------------
    @cached_property
    def list_items(self) -> List[ListItem]:
        return find_list_items(self.text, self.lines)

    @cached_property
    def top_level_items(self) -> List[ListItem]:
        return top_level_list_items(self.list_items)

    @cached_property
    def bullet_items(self) -> List[ListItem]:
        return [i for i in self.list_items if i.marker in ("-", "*", "+")]

    @cached_property
    def top_level_bullets(self) -> List[ListItem]:
        return top_level_list_items(self.bullet_items)

    @cached_property
    def numbered_items(self) -> List[ListItem]:
        return [i for i in self.list_items if i.marker not in ("-", "*", "+")]

    @cached_property
    def headings(self):
        return find_headings(self.text, self.lines)

    @cached_property
    def bold_only_lines(self) -> List[str]:
        return [l.text for l in self.lines if not l.in_code and _BOLD_ONLY_LINE.match(l.text)]

    @cached_property
    def paragraphs(self):
        return find_paragraphs(self.text, self.lines)

    @cached_property
    def sentences(self) -> List[str]:
        return split_sentences(self.inline_masked, split_lines(self.inline_masked))

    @cached_property
    def content_nonempty_lines(self) -> List[Line]:
        return [l for l in content_lines(self.text, self.lines) if l.text.strip()]

    # --- 표 --------------------------------------------------------------------
    @cached_property
    def tables(self) -> List[TableInfo]:
        out: List[TableInfo] = []
        for t in find_markdown_tables(self.text, self.lines):
            headers = tuple(_clean_cell(c) for c in t.headers)
            blank = not any(headers)   # 헤더 칸이 모두 비면 헤더 없는 표 (마크다운은 헤더 줄을 생략할 수 없음)
            out.append(TableInfo(kind="markdown", start=t.start, end=t.end, header_rows=0 if blank else 1,
                                 data_rows=len(t.rows), headers=() if blank else headers,
                                 rows=tuple(tuple(_clean_cell(c) for c in r) for r in t.rows)))
        masked = mask_ranges(self.text, self.fence_ranges)
        out.extend(_html_tables(masked))
        return out

    @cached_property
    def pipe_lines_without_table(self) -> List[Line]:
        """표 문법이 확인되지 않은 파이프 줄 (표로 인정하지 않음, 관측만)."""
        table_ranges = [(t.start, t.end) for t in self.tables if t.kind == "markdown"]
        return [l for l in self.lines if not l.in_code and l.text.count("|") >= 2
                and not any(s <= l.start <= e for s, e in table_ranges)]

    def structure_summary(self) -> Dict[str, Any]:
        return {
            "characters": len(self.text),
            "fenced_code_blocks": len(self.fenced_blocks),
            "code_fence_delimiters": len(self.code_fence_delimiters),
            "math_delimiters": len(self.math_delimiters),
            "inline_code_spans": len(self.inline_code_spans),
            "list_items": len(self.list_items),
            "top_level_list_items": len(self.top_level_items),
            "headings": len(self.headings),
            "tables": [t.to_dict() for t in self.tables],
            "sentences": len(self.sentences),
            "paragraphs": len(self.paragraphs),
        }
