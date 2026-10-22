"""답변 텍스트 구조 파싱 도구 — 판정은 하지 않고 측정 재료만 만든다.

- 줄·코드 펜스, 제목·목록·GFM 표·문단·문장·단어
- 깨진 문자 서명, 비내용 문자 제거
- 코드 펜스 구획, JSON 값 종류
- 각주·참조 번호·내부 링크와 링크 대상
- 코드 제외·강조 제거 투영
- 개수 수사와 출력 동사 판별
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote

__all__ = ["Line", "ListItem", "Heading", "MarkdownTable", "Paragraph", "split_lines", "content_lines",
           "find_headings", "heading_slug", "find_list_items", "top_level_list_items",
           "find_markdown_tables", "find_paragraphs", "split_sentences", "count_words",
           "CORRUPTION_SIGNATURES", "find_corruption", "strip_non_content", "internal_links",
           "link_targets", "scannable_text"]


# --- 줄·코드 펜스·제목·목록·GFM 표·문단·문장·단어 ----------------------------------------

#: 들여쓰기 비교 시 탭을 몇 칸으로 볼지
TAB_WIDTH = 4

_LIST_ITEM = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>\d{1,9}[.)]|[-*+])(?P<gap>[ \t]+)(?P<body>\S.*)$"
)


def _content_column(indent: int, marker: str, gap: str) -> int:
    """CommonMark 목록 항목의 내용이 시작하는 열. 표시 뒤 공백이 5칸 이상이면 1칸만 표시에 속한다."""
    width = len(gap.expandtabs(TAB_WIDTH))
    return indent + len(marker) + (width if width <= 4 else 1)


@dataclass(frozen=True)
class Line:
    """답변의 한 줄. 위치를 그대로 보존해 실패 위치를 지목할 수 있게 한다."""

    number: int   # 1부터 세는 줄 번호
    start: int    # 답변 원문에서의 시작 위치
    end: int      # 줄바꿈을 제외한 끝 위치
    text: str
    in_code: bool  # 코드 펜스 안쪽인가


def split_lines(text: str) -> List[Line]:
    """답변을 줄 단위로 나누고 각 줄이 코드 펜스 안쪽인지 표시한다."""
    lines: List[Line] = []
    offset = 0
    blocks = _fenced_code_blocks(text)
    block_index = 0
    for number, raw in enumerate(text.split("\n"), start=1):
        while block_index < len(blocks) and offset >= blocks[block_index]["end"]:
            block_index += 1
        # 구문 검사와 같은 펜스 길이·종류·닫는 줄 규칙을 사용한다.
        # 펜스를 여는 줄과 닫는 줄 자체도 본문으로 보지 않는다.
        in_code = (block_index < len(blocks) and
                   blocks[block_index]["start"] <= offset < blocks[block_index]["end"])
        # CRLF 의 \r 은 줄바꿈의 일부다. 원문은 그대로 두고 줄 본문에서만 뺀다.
        body = raw[:-1] if raw.endswith("\r") else raw
        lines.append(Line(number=number, start=offset, end=offset + len(body), text=body, in_code=in_code))
        offset += len(raw) + 1
    return lines


@dataclass(frozen=True)
class ListItem:
    """번호 또는 불릿이 붙은 목록 줄 하나."""

    marker: str   # "1." · "2)" · "-" 처럼 줄에 적힌 표시 그대로
    indent: int   # 같은 목록의 형제 항목끼리 맞춘 들여쓰기 (find_list_items 참고)
    body: str     # 표시를 뗀 나머지 텍스트
    line: Line

    @property
    def start(self) -> int:
        return self.line.start

    @property
    def end(self) -> int:
        return self.line.end


def find_list_items(text: str, lines: Optional[Sequence[Line]] = None) -> List[ListItem]:
    """코드 블록 밖에 있는 목록 줄을 모두 찾는다 (번호 목록 + 불릿 목록).

    CommonMark 에서 앞 항목의 내용 시작 열보다 덜 들여쓴 항목은 하위 항목이 아니라 형제 항목이다.
    같은 종류(불릿·번호) 형제가 1~3칸 어긋나 있으면 ``indent`` 를 앞 형제와 같게 맞춰 하위로 세지 않는다.
    종류가 다른 항목은 줄에 적힌 들여쓰기를 그대로 쓴다.
    """
    items: List[ListItem] = []
    stacks: Dict[int, List[Tuple[int, int, bool]]] = {}   # 인용 깊이 → (내용 시작 열, 맞춘 들여쓰기, 불릿 여부)
    quote_fences: Dict[int, bool] = {}
    for line in lines if lines is not None else split_lines(text):
        if line.in_code:
            continue
        # 인용문(>) 안 목록도 목록이다. 인용문 안 코드 펜스 사이의 줄은 목록으로 보지 않는다.
        depth, content = 0, line.text
        prefix = _QUOTE_PREFIX.match(line.text)
        if prefix:
            depth, content = prefix.group(0).count(">"), line.text[prefix.end():]
            if _FENCE_LINE.match(content):
                quote_fences[depth] = not quote_fences.get(depth, False)
                continue
            if quote_fences.get(depth):
                continue
        match = _LIST_ITEM.match(content)
        if not match:
            continue
        open_items = stacks.setdefault(depth, [])
        written = len(match.group("indent").expandtabs(TAB_WIDTH))
        bullet = match.group("marker") in ("-", "*", "+")
        sibling = None
        while open_items and written < open_items[-1][0]:
            sibling = open_items.pop()
        indent = sibling[1] if sibling is not None and sibling[2] == bullet else written
        open_items.append((_content_column(written, match.group("marker"), match.group("gap")), indent, bullet))
        items.append(
            ListItem(
                marker=match.group("marker"),
                indent=indent,
                body=match.group("body"),
                line=line,
            )
        )
    return items


def top_level_list_items(items: Sequence[ListItem]) -> List[ListItem]:
    """가장 얕게 들여쓴 목록 줄만 최상위로 본다."""
    if not items:
        return []
    shallowest = min(item.indent for item in items)
    return [item for item in items if item.indent == shallowest]


#: 문장 끝으로 보는 문자.
# 문장 끝 부호 뒤에 마크다운 강조 닫기(``**``·``__``·``*``·``_``)가 붙어도 문장 끝이다 — '중단됩니다.**' 다음 줄.
# 따옴표·괄호 닫기는 넣지 않는다: '그가 "좋다." 말했다' 의 안쪽 마침표는 문장 끝이 아니다.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])(?:\*{1,3}|_{1,3})?[ \t]*(?:\n|$)|(?<=[.!?。！？])(?:\*{1,3}|_{1,3})?\s+")

#: 마침표가 붙어도 문장 끝이 아닌 등록 약어. **여기 있는 것만** 예외로 둔다.
SENTENCE_ABBREVIATIONS = frozenset({
    "dr", "mr", "mrs", "ms", "prof", "st", "jr", "sr", "vs", "etc", "e.g", "i.e",
    "no", "fig", "inc", "ltd", "co", "approx", "vol", "ed", "pp",
})

#: 약어 판정을 위해 경계 바로 앞 낱말을 뽑는다 (마침표 제외).
_TRAILING_WORD = re.compile(r"([A-Za-z][A-Za-z.]*)\.$")

#: ATX 제목 줄
_HEADING = re.compile(r"^(?P<indent>[ ]{0,3})(?P<hashes>#{1,6})[ \t]+(?P<title>\S.*?)[ \t]*#*[ \t]*$")

#: 표 구분선 (GFM). 최소 한 칸 이상의 하이픈과 선택적 정렬 표시.
_TABLE_DELIMITER = re.compile(r"^[ ]{0,3}\|?[ \t]*:?-{1,}:?[ \t]*(?:\|[ \t]*:?-{1,}:?[ \t]*)*\|?[ \t]*$")

#: 표 줄
_TABLE_ROW = re.compile(r"^[ ]{0,3}\|.*\|[ \t]*$")

#: GFM 은 표 양끝 파이프를 생략할 수 있다. 그때는 줄 안의 파이프로 칸을 나눈다.
_TABLE_ROW_BARE = re.compile(r"^[ ]{0,3}\S.*\|")

#: 인용문 표시. 인용문 안의 표도 GFM 표다 (같은 깊이의 인용 줄끼리만 이어 본다).
_QUOTE_PREFIX = re.compile(r"^(?:[ ]{0,3}>[ ]?)+")

#: 슬러그에서 지우는 문자 (GitHub 기준에 맞춘 최소 규칙)
_SLUG_STRIP = re.compile(r"[^\w\s가-힣-]", re.UNICODE)


@dataclass(frozen=True)
class Heading:
    """제목 줄 하나."""

    level: int
    title: str
    line: Line

    @property
    def start(self) -> int:
        return self.line.start

    @property
    def end(self) -> int:
        return self.line.end


@dataclass(frozen=True)
class Paragraph:
    """빈 줄로 구분되는 본문 덩어리 하나."""

    text: str
    start: int
    end: int
    lines: Tuple[int, ...]


@dataclass(frozen=True)
class MarkdownTable:
    """GFM 표 하나. 머리글과 본문 행을 셀 단위로 나눠 담는다."""

    headers: Tuple[str, ...]
    rows: Tuple[Tuple[str, ...], ...]
    start: int
    end: int
    header_line: int

    @property
    def column_count(self) -> int:
        return len(self.headers)

    @property
    def row_count(self) -> int:
        return len(self.rows)


def content_lines(text: str, lines: Optional[Sequence[Line]] = None) -> List[Line]:
    """코드 펜스 밖의 줄만 남긴다. 본문을 세는 검사가 모두 이 기준을 쓴다."""
    return [line for line in (lines if lines is not None else split_lines(text)) if not line.in_code]


def count_words(text: str, lines: Optional[Sequence[Line]] = None) -> int:
    """낱말 수를 센다.

    한국어에는 영어 같은 낱말 경계가 없다. 이 프로젝트는 **공백으로 나뉜
    덩어리(어절)** 를 한 낱말로 센다. 형태소 분석을 쓰지 않는다. 같은 입력이면
    항상 같은 값이 나와야 하고, 형태소 분석기는 버전에 따라 결과가 달라지기
    때문이다. 코드 블록은 세지 않는다.
    """
    return sum(len(line.text.split()) for line in content_lines(text, lines))


def split_sentences(text: str, lines: Optional[Sequence[Line]] = None) -> List[str]:
    """코드 밖 본문을 문장 단위로 나눈다. 빈 조각은 버린다.

    마침표가 늘 문장 끝인 것은 아니다. 등록된 약어(``Dr.`` · ``Mr.`` …)와 URL·도메인 안의
    마침표는 경계로 보지 않는다. 등록되지 않은 약어는 추측하지 않는다 — 목록에 있는 것만 뺀다.
    """
    body = "\n".join(line.text for line in content_lines(text, lines))
    pieces: List[str] = []
    cursor = 0
    for match in _SENTENCE_SPLIT.finditer(body):
        if _protected_period(body, match.start()):
            continue
        pieces.append(body[cursor:match.start()])
        cursor = match.end()
    pieces.append(body[cursor:])
    return [piece.strip() for piece in pieces if piece and piece.strip()]


def _protected_period(body: str, boundary: int) -> bool:
    """``boundary`` 바로 앞의 마침표를 문장 끝으로 보지 않아야 하는가.

    등록 약어만 예외로 둔다. URL 안의 마침표는 따로 볼 필요가 없다 — 이 분리기는 마침표
    **뒤에 공백이나 줄끝이 올 때만** 경계로 보므로, 주소 도중의 마침표는 애초에 걸리지 않는다.
    주소 뒤에 붙은 마침표는 실제 문장 끝이다.
    """
    if boundary == 0 or body[boundary - 1] != ".":
        return False
    word = _TRAILING_WORD.search(body[:boundary])
    return bool(word and word.group(1).lower() in SENTENCE_ABBREVIATIONS)


def find_paragraphs(text: str, lines: Optional[Sequence[Line]] = None) -> List[Paragraph]:
    """빈 줄로 구분되는 본문 덩어리를 찾는다. 코드 블록은 제외한다."""
    paragraphs: List[Paragraph] = []
    current: List[Line] = []

    def flush() -> None:
        if not current:
            return
        paragraphs.append(
            Paragraph(
                text="\n".join(line.text for line in current).strip(),
                start=current[0].start,
                end=current[-1].end,
                lines=tuple(line.number for line in current),
            )
        )

    for line in (lines if lines is not None else split_lines(text)):
        if line.in_code or not line.text.strip():
            flush()
            current = []
            continue
        current.append(line)
    flush()
    return [p for p in paragraphs if p.text]


def find_headings(text: str, lines: Optional[Sequence[Line]] = None) -> List[Heading]:
    """코드 밖의 ATX 제목(``#`` ~ ``######``)을 찾는다.

    밑줄식 제목(``===``)은 목록·구분선과 헷갈리므로 이 구현에서 세지 않는다.
    """
    headings: List[Heading] = []
    for line in content_lines(text, lines):
        match = _HEADING.match(line.text)
        if not match:
            continue
        headings.append(
            Heading(level=len(match.group("hashes")), title=match.group("title").strip(), line=line)
        )
    return headings


def _split_row(raw: str) -> Tuple[str, ...]:
    """표 한 줄을 셀로 나눈다. 양끝 파이프는 버린다."""
    stripped = raw.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return tuple(cell.strip() for cell in stripped.split("|"))


def find_markdown_tables(text: str, lines: Optional[Sequence[Line]] = None) -> List[MarkdownTable]:
    """GFM 표를 찾는다. 머리글 + 구분선이 갖춰진 것만 표로 본다.

    양끝 파이프가 없는 표는 구분선에도 파이프가 있어야 표로 본다 (``---`` 한 줄은 구분선·제목 밑줄과 같다).
    인용문 안의 표는 같은 깊이의 ``>`` 를 뗀 줄로 읽는다.
    """
    body = content_lines(text, lines)

    def unquoted(line: Line) -> Tuple[int, str]:
        prefix = _QUOTE_PREFIX.match(line.text)
        return (prefix.group(0).count(">"), line.text[prefix.end():]) if prefix else (0, line.text)

    tables: List[MarkdownTable] = []
    index = 0
    while index < len(body) - 1:
        header_line, delimiter_line = body[index], body[index + 1]
        depth, header = unquoted(header_line)
        delimiter_depth, delimiter = unquoted(delimiter_line)
        piped = bool(_TABLE_ROW.match(header))
        bare = not piped and bool(_TABLE_ROW_BARE.match(header)) and "|" in delimiter
        if depth != delimiter_depth or not (piped or bare) or not _TABLE_DELIMITER.match(delimiter):
            index += 1
            continue
        headers = _split_row(header)
        if len(headers) != len(_split_row(delimiter)):
            index += 1
            continue
        rows: List[Tuple[str, ...]] = []
        end = delimiter_line.end
        cursor = index + 2
        while cursor < len(body):
            row_depth, row = unquoted(body[cursor])
            if row_depth != depth or not (_TABLE_ROW.match(row) or bare and _TABLE_ROW_BARE.match(row)):
                break
            rows.append(_split_row(row))
            end = body[cursor].end
            cursor += 1
        tables.append(
            MarkdownTable(
                headers=headers,
                rows=tuple(rows),
                start=header_line.start,
                end=end,
                header_line=header_line.number,
            )
        )
        index = cursor
    return tables


def heading_slug(title: str) -> str:
    """제목을 내부 링크 대상 이름으로 바꾼다 (GitHub 방식에 맞춘 최소 규칙).

    소문자로 바꾸고, 낱말·한글·공백·하이픈이 아닌 문자를 지우고, 공백을
    하이픈으로 바꾼다. 이 규칙으로 맞지 않는 링크는 FAIL 로 확정하지 않고
    검사기가 판정 보류로 남긴다.
    """
    lowered = unicodedata.normalize("NFC", title).strip().lower()
    cleaned = _SLUG_STRIP.sub("", lowered)
    return re.sub(r"\s+", "-", cleaned).strip("-")


# --- 깨진 문자 서명·비내용 문자 ------------------------------------------------------------------

#: 실질 내용으로 보지 않는 문자 범주.
#: Cc = 제어문자, Cf = 서식 제어문자(zero-width space, BOM 등).
_NON_CONTENT_CATEGORIES = frozenset({"Cc", "Cf"})


def strip_non_content(text: str) -> str:
    """공백·탭·개행·제어문자를 제거하고 실질 문자만 남긴다.

    1-1 응답 정상성의 "공백·제어문자 제거 후 실질 응답 없음" 을
    코드로 고정한 것이다. 의미 판단은 하지 않는다.
    """
    return "".join(
        ch
        for ch in text
        if not ch.isspace() and unicodedata.category(ch) not in _NON_CONTENT_CATEGORIES
    )


#: 깨짐으로 확정할 수 있는 고정밀 표시만 등록한다.
#: 여기 없는 문자는 깨짐으로 보지 않는다. 한국어 답변에 한자·가나·영문이
#: 섞여 있다는 이유로는 절대 FAIL 하지 않는다.
CORRUPTION_SIGNATURES = (
    {
        "id": "replacement-character",
        "pattern": r"�",
        "description": "U+FFFD 대체 문자. 디코딩에 실패한 자리에만 나타난다.",
    },
    {
        "id": "utf8-as-latin1",
        # UTF-8 바이트열을 Latin-1/CP1252 로 잘못 읽었을 때 생기는 짝.
        # 선행 바이트 자리(U+00C2~U+00FF)에 이어 바이트 자리(U+0080~U+00BF)가
        # 붙는 형태는 어떤 자연어 표기에도 나타나지 않는다.
        "pattern": r"[Â-ÿ][-¿]",
        "description": "UTF-8 을 Latin-1 로 잘못 읽은 깨짐(모지바케) 서명.",
    },
    {
        "id": "c1-control",
        # C1 제어 구간이 본문에 그대로 남은 경우.
        "pattern": r"[-]",
        "description": "C1 제어 문자. 정상 본문에는 나타나지 않는다.",
    },
    {
        "id": "lone-surrogate",
        "pattern": r"[\ud800-\udfff]",
        "description": "짝이 맞지 않는 서로게이트. 인코딩이 깨진 자리다.",
    },
)

_COMPILED_SIGNATURES = tuple(
    (item["id"], re.compile(item["pattern"]), item["description"])
    for item in CORRUPTION_SIGNATURES
)


def find_corruption(text: str) -> List[Dict[str, Any]]:
    """등록된 깨짐 서명이 걸린 위치를 모두 돌려준다."""
    hits: List[Dict[str, Any]] = []
    for rule_id, pattern, description in _COMPILED_SIGNATURES:
        for match in pattern.finditer(text):
            hits.append(
                {
                    "signature_id": rule_id,
                    "char_range": [match.start(), match.end()],
                    "codepoints": [f"U+{ord(ch):04X}" for ch in match.group(0)],
                    "description": description,
                }
            )
    hits.sort(key=lambda hit: (hit["char_range"][0], hit["signature_id"]))
    return hits


# --- 코드 펜스 구획·JSON 값 종류 ------------------------------------------------------------

_FENCE_LINE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>`{3,}|~{3,})(?P<info>[^\r\n]*)$")


def _indent_width(line: str) -> int:
    return len(line[:len(line) - len(line.lstrip(" \t"))].expandtabs(TAB_WIDTH))


def _dedent_content(content: str, width: int) -> str:
    """CommonMark: 여는 펜스가 N칸 들여쓰였으면 내용 각 줄에서 앞 공백을 N칸까지 뺀다."""
    if not width:
        return content
    return "".join(part[min(width, len(part) - len(part.lstrip(" "))):]
                   for part in content.splitlines(keepends=True))


def _fenced_code_blocks(text: str) -> List[Dict[str, Any]]:
    """CommonMark fence 길이·종류를 지키며 코드 구획을 추출한다.

    CommonMark 에서는 파일 끝의 닫히지 않은 fence도 유효한 code block이다.
    따라서 ``closed``를 근거에 남기되 구문 실패로 만들지 않는다.

    목록 밖 펜스는 0~3칸 들여쓴다. 목록 항목 안 펜스는 그 항목 내용이 시작하는 열에서 0~3칸이므로
    줄 기준으로 4칸 이상일 수 있다 (CommonMark 컨테이너 규칙). 빈 줄 뒤에 덜 들여쓴 본문이 오면 그 항목은 닫힌다.
    ``indent`` 는 여는 줄의 들여쓰기, ``dedented`` 는 내용에서 그만큼 앞 공백을 뺀 값이다 (구문 검사용).
    위치 계산은 원문 그대로인 ``content`` 를 쓴다.
    """
    lines = text.splitlines(keepends=True)
    blocks: List[Dict[str, Any]] = []
    offset = 0
    opened: Optional[Dict[str, Any]] = None
    content_start = 0
    content_parts: List[str] = []
    columns: List[int] = []   # 열려 있는 목록 항목의 내용 시작 열
    blank_before = False
    for raw in lines:
        line = raw.rstrip("\r\n")
        match = _FENCE_LINE.match(line)
        if opened is None:
            width = _indent_width(line)
            base = 0 if width <= 3 else max((c for c in columns if 0 <= width - c <= 3), default=None)
            if match and base is not None:
                marker = match.group("marker")
                info = match.group("info").strip()
                if marker[0] == "`" and "`" in info:
                    offset += len(raw)
                    blank_before = False
                    continue
                if width <= 3:
                    base = max((c for c in columns if c <= width), default=0)
                opened = {
                    "marker": marker[0], "length": len(marker), "info": info,
                    "start": offset, "opening": line, "indent": width, "base": base,
                }
                content_start = offset + len(raw)
                content_parts = []
            elif line.strip():
                item = _LIST_ITEM.match(line)
                if item or blank_before:
                    while columns and width < columns[-1]:
                        columns.pop()
                if item:
                    columns.append(_content_column(width, item.group("marker"), item.group("gap")))
            blank_before = not line.strip()
        else:
            if match:
                marker = match.group("marker")
                is_close = (
                    marker[0] == opened["marker"]
                    and len(marker) >= opened["length"]
                    and not match.group("info").strip()
                    and _indent_width(line) <= opened["base"] + 3
                )
                if is_close:
                    content = "".join(content_parts)
                    blocks.append({**opened, "content": content, "dedented": _dedent_content(content, opened["indent"]),
                                   "content_start": content_start, "end": offset + len(raw),
                                   "closed": True})
                    opened = None
                    content_parts = []
                    offset += len(raw)
                    blank_before = False
                    continue
            content_parts.append(raw)
        offset += len(raw)
    if opened is not None:
        content = "".join(content_parts)
        blocks.append({**opened, "content": content, "dedented": _dedent_content(content, opened["indent"]),
                       "content_start": content_start, "end": len(text), "closed": False})
    return blocks


def _reject_json_constant(name: str) -> None:
    """RFC 8259 에 없는 상수(NaN, Infinity)를 유효한 JSON 으로 보지 않는다."""
    raise ValueError(f"RFC 8259 에 없는 상수입니다: {name}")


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):  # bool 은 int 보다 먼저 판별해야 한다
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


# --- 각주·참조 번호·내부 링크 --------------------------------------------------------------------

#: 본문 인용 표시.
#: ``arr[1]`` 처럼 ASCII 식별자 바로 뒤에 오는 표기는 배열 색인으로 보고
#: 제외한다. ``list[0][1]`` 처럼 색인이 이어지는 경우도 함께 제외한다.
#: 한글 바로 뒤의 ``[1]`` 과 연속 인용 ``[1][2]`` 는 인용으로 인정한다.
_CITATION = re.compile(r"(?<![A-Za-z0-9_])\[(?P<number>\d{1,3})\]")

#: 출처 목록 줄. 줄 시작에서 [n] 뒤에 내용이 오는 형태만 인정한다. [n] 바로 뒤에 조사가 붙은 줄('[2]는 예산 메모의 …')은
#: 본문 문장이라 뺀다 (10-22 점검: Claude Q2 T8 등 거짓 '출처 누락').
_SOURCE_LINE = re.compile(r"^[ \t]{0,3}\[(?P<number>\d{1,3})\](?![은는과와이가의을를도에로만])[ \t]*(?P<body>\S.*)$")
#: 목록형 · 굵은 글씨형 출처 줄('- [1] 답사 메모', '1. [1] …', '**[1]** …') — 위 형식으로 정의되지 않은 번호에만 출처로 쓰고
#: 중복으로 세지 않는다. 번호가 이어지는 설명 줄('- [1]·[2]는 …', '- [1], [2]는 …')은 뺀다 (10-22: Claude Q6 T3 · Q7 T5 · Q9 T3)
_SOURCE_LINE_LISTED = re.compile(r"^[ \t]{0,3}(?:[-*+•][ \t]+|\d{1,2}[.)][ \t]+)?[*_]{0,2}\[(?P<number>\d{1,3})\][*_]{0,2}"
                                 r"(?![은는과와이가의을를도에로만])(?![ \t]*[·,、/]?[ \t]*\[\d)[ \t]*(?P<body>\S.*)$")


def _parse_references(text: str) -> Tuple[List[Tuple[int, int]], Dict[int, int], List[int]]:
    """(본문 인용 [(번호, 위치)], 출처 {번호: 줄 번호}, 중복 출처 번호) 를 돌려준다."""
    lines = split_lines(text)

    sources: Dict[int, int] = {}
    duplicated: List[int] = []
    source_marker_spans: List[Tuple[int, int]] = []
    listed: List[Tuple[int, int, int]] = []   # (번호, 줄 번호, 표시 시작) — 원래 형식 뒤에 따로 본다
    for line in lines:
        if line.in_code:
            continue
        match = _SOURCE_LINE.match(line.text)
        if not match:
            alt = _SOURCE_LINE_LISTED.match(line.text)
            if alt:
                listed.append((int(alt.group("number")), line.number, line.start + alt.start(0) + alt.group(0).index("[")))
            continue
        number = int(match.group("number"))
        start = line.start + match.start(0) + match.group(0).index("[")
        source_marker_spans.append((start, start + len(match.group("number")) + 2))
        if number in sources:
            if number not in duplicated:
                duplicated.append(number)
        else:
            sources[number] = line.number
    for number, line_number, start in listed:
        if number not in sources:
            sources[number] = line_number
            source_marker_spans.append((start, start + len(str(number)) + 2))

    code_spans = [(line.start, line.end) for line in lines if line.in_code]
    definition_starts = {marker_start for marker_start, _ in source_marker_spans}
    citations: List[Tuple[int, int]] = []

    for match in _CITATION.finditer(text):
        start = match.start()
        if any(span_start <= start < span_end for span_start, span_end in code_spans):
            continue
        if start in definition_starts:
            continue  # 출처 목록의 정의 표시는 본문 인용으로 세지 않는다
        if _is_index_notation(text, start):
            continue
        citations.append((int(match.group("number")), start))
    return citations, sources, duplicated


def _is_index_notation(text: str, start: int) -> bool:
    """``list[0][1]`` 처럼 ASCII 식별자에 이어지는 색인 표기인가."""
    position = start
    while position > 0 and text[position - 1] == "]":
        opening = text.rfind("[", 0, position - 1)
        if opening < 0 or not text[opening + 1 : position - 1].isdigit():
            return False
        position = opening
    if position == 0:
        return False
    previous = text[position - 1]
    return previous.isascii() and (previous.isalnum() or previous == "_")


#: GFM 각주 표시와 정의
_GFM_FOOTNOTE_MARKER = re.compile(r"\[\^(?P<label>[^\]\s]{1,64})\]")

_GFM_FOOTNOTE_DEF = re.compile(r"^[ ]{0,3}\[\^(?P<label>[^\]\s]{1,64})\][ \t]*:[ \t]*(?P<body>\S.*)$")

#: "[각주1]" 형태의 한국어 표시와, 줄 첫머리의 같은 표시 + 설명
_KO_FOOTNOTE_MARKER = re.compile(r"\[(?P<label>각주\s*\d{1,3})\]")

_KO_FOOTNOTE_DEF = re.compile(r"^[ ]{0,3}\[(?P<label>각주\s*\d{1,3})\][ \t]+(?P<body>\S.*)$")


def _footnote_label(raw: str) -> str:
    """비교용 각주 이름. 공백만 걷어낸다."""
    return re.sub(r"\s+", "", raw)


def _parse_footnotes(text: str):
    """(본문 표시, 정의) 를 돌려준다.

    본문 표시는 [(이름, 위치)], 정의는 {이름: 줄 번호} 다. 코드 블록은 보지 않는다.
    정의 줄에 있는 표시 자체는 본문 표시로 세지 않는다.
    """
    lines = split_lines(text)
    definitions: Dict[str, int] = {}
    duplicated: List[str] = []
    definition_spans: List[Tuple[int, int]] = []

    for line in lines:
        if line.in_code:
            continue
        for pattern in (_GFM_FOOTNOTE_DEF, _KO_FOOTNOTE_DEF):
            match = pattern.match(line.text)
            if not match:
                continue
            label = _footnote_label(match.group("label"))
            definition_spans.append((line.start, line.end))
            if label in definitions:
                if label not in duplicated:
                    duplicated.append(label)
            else:
                definitions[label] = line.number
            break

    code_spans = [(line.start, line.end) for line in lines if line.in_code]
    markers: List[Tuple[str, int]] = []
    for pattern in (_GFM_FOOTNOTE_MARKER, _KO_FOOTNOTE_MARKER):
        for match in pattern.finditer(text):
            start = match.start()
            if any(begin <= start < end for begin, end in code_spans):
                continue
            if any(begin <= start < end for begin, end in definition_spans):
                continue  # 정의 줄의 표시는 본문 인용이 아니다
            markers.append((_footnote_label(match.group("label")), start))
    markers.sort(key=lambda item: item[1])
    return markers, definitions, duplicated


#: Markdown 내부 링크. ``[보이는 글](#대상)`` 형태만 본다.
_INTERNAL_LINK = re.compile(r"\[(?P<text>[^\]\n]*)\]\(#(?P<anchor>[^)\s]+)\)")

#: 답변이 직접 심어 둔 앵커
_EXPLICIT_ANCHOR = re.compile(r"<[^>]*\b(?:id|name)\s*=\s*[\"'](?P<anchor>[^\"']+)[\"']")


def internal_links(text: str) -> List[Tuple[str, int, int]]:
    """문서 안을 가리키는 링크 ``[..](#앵커)`` → (앵커, 시작, 끝). 코드 안의 링크는 제외."""
    code_spans = [(line.start, line.end) for line in split_lines(text) if line.in_code]
    found = []
    for match in _INTERNAL_LINK.finditer(text):
        if any(start <= match.start() < end for start, end in code_spans):
            continue
        found.append((unquote(match.group("anchor")).lower(), match.start(), match.end()))
    return found


def link_targets(text: str) -> Tuple[Dict[str, int], List[str]]:
    """대상 이름 → 등장 횟수, 그리고 중복된 이름 목록. 제목 슬러그와 직접 심은 앵커를 합친다."""
    counts: Dict[str, int] = {}
    for heading in find_headings(text):
        slug = heading_slug(heading.title)
        if slug:
            counts[slug] = counts.get(slug, 0) + 1
    code_spans = [(block["start"], block["end"]) for block in _fenced_code_blocks(text)]
    for match in _EXPLICIT_ANCHOR.finditer(text):
        if any(start <= match.start() < end for start, end in code_spans):
            continue
        anchor = match.group("anchor").strip().lower()
        if anchor:
            counts[anchor] = counts.get(anchor, 0) + 1
    duplicated = sorted(name for name, count in counts.items() if count > 1)
    return counts, duplicated


# --- 코드 제외·강조 제거 투영 --------------------------------------------------------------------

#: 강조 표기로 보고 걷어낼 문자. 밑줄은 낱말 안에 있으면 남긴다.
_EMPHASIS_CHARS = "*~"
#: 마크다운이 역슬래시로 이스케이프하는 글자 — '\x' 는 화면에 'x' 한 글자로 보인다
_MD_ESCAPABLE = set(r"\`*_{}[]()#+-.!~|<>")
#: HTML 문자 참조 — 숫자형(&#x20; &#32;)과 자주 쓰는 이름형
_HTML_ENTITY = re.compile(r"&(?:#[xX][0-9A-Fa-f]{1,6}|#\d{1,7}|nbsp|amp|lt|gt|quot|apos);")


def scannable_text(text: str) -> Tuple[str, List[int]]:
    """검사용 문자열과 원문 위치 대응표를 만든다.

    코드 블록·인라인 코드를 빼고 강조 마커를 걷어낸다. 대응표가 있어야
    걸린 위치를 원문 좌표로 되돌려 실패 근거에 남길 수 있다.
    """
    chars: List[str] = []
    offsets: List[int] = []

    for line in split_lines(text):
        if line.in_code:
            continue
        raw = line.text
        index = 0
        in_inline_code = False
        while index < len(raw):
            char = raw[index]
            if char == "`":
                in_inline_code = not in_inline_code
                index += 1
                continue
            if in_inline_code:
                index += 1
                continue
            # 화면에는 한 글자로 보이는 표기 — 마크다운 역슬래시 이스케이프('\~' → '~')와 HTML 문자 참조('&#x20;' → 공백).
            # 그대로 세면 'x20' 이 영문으로, '\' 가 기호로 잡힌다 (10-22 점검: GPT Q9 T2 '한글만' 거짓 FAIL)
            if char == "\\" and index + 1 < len(raw) and raw[index + 1] in _MD_ESCAPABLE:
                chars.append(raw[index + 1])
                offsets.append(line.start + index + 1)
                index += 2
                continue
            if char == "&":
                entity = _HTML_ENTITY.match(raw, index)
                if entity:
                    chars.append(html.unescape(entity.group(0)))
                    offsets.append(line.start + index)
                    index = entity.end()
                    continue
            if char in _EMPHASIS_CHARS:
                index += 1
                continue
            if char == "_" and not _is_intraword(raw, index):
                index += 1
                continue
            chars.append(char)
            offsets.append(line.start + index)
            index += 1
        chars.append("\n")
        offsets.append(line.end)

    return "".join(chars), offsets


def _is_intraword(raw: str, index: int) -> bool:
    """밑줄이 낱말 안에 있는가. 낱말 문자는 영숫자만 본다."""
    before = raw[index - 1] if index > 0 else ""
    after = raw[index + 1] if index + 1 < len(raw) else ""
    return bool(before) and bool(after) and before.isalnum() and after.isalnum()


# --- 개수 수사·출력 동사 --------------------------------------------------------------------------

_NUM_WORD = {"한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "셋": 3, "네": 4, "넷": 4,
             "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}

_OUTPUT_VERB = r"추천|제안|작성|나열|정리|골라|선정|만들|적어|써|알려"


def _count_governed_by_output_verb(text: str, end: int) -> Optional[str]:
    """수량 구절이 출력 동사의 **직접 목적어**인지 본다.

    "여행지 정확히 3개 추천해 줘" 는 3개가 추천의 목적어다.
    "입력 정수 두 개를 합산하는 Python 코드를 작성해 줘" 는 두 개가 합산의 목적어이고
    작성의 목적어는 코드다. 뒤엣것을 답변 목록 수량 요구로 승격하지 않는다.
    """
    rest = re.sub(r"^\s*(?:를|을|만|씩|의|는|은|이|가)\s*", "", text[end:])
    clause = re.split(r"[.!?\n]", rest)[0]
    verb = re.search(_OUTPUT_VERB, clause)
    if verb is None:
        return None
    between = clause[:verb.start()]
    if re.search(r"(?:을|를)\s", between) or re.search(r"(?:을|를)$", between.strip()):
        return None
    return clause[verb.start():]
