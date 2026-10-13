"""웹 답변 HTML → 평가용 Markdown. 원본 HTML 은 호출하는 쪽이 따로 보관한다.

제목·문단·목록·표·코드 블록·인용문·링크·각주·줄바꿈·수식(KaTeX 주석)을 Markdown 구조로 옮긴다.
Markdown 으로 같은 구조를 나타낼 수 없는 부분(병합 셀, 표 칸 안의 블록 등)은 고치거나 추측하지 않고
수집 품질 기록(quality)에 남긴다. 외부 라이브러리 없이 표준 html.parser 만 쓴다.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

CONVERTER_VERSION = "html-markdown-v1"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
SKIP = {"script", "style", "noscript", "template", "svg", "math", "button", "iframe", "canvas", "head", "title", "select",
        "textarea", "object"}
BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "aside", "nav", "ul", "ol", "li", "pre", "blockquote",
         "table", "thead", "tbody", "tfoot", "tr", "td", "th", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "dl", "dt", "dd",
         "figure", "figcaption", "details", "summary", "body", "html", "form", "fieldset", "caption", "address"}
KNOWN_INLINE = {"a", "strong", "b", "em", "i", "code", "del", "s", "strike", "sup", "sub", "span", "img", "mark", "u", "kbd",
                "abbr", "small", "time", "q", "cite", "var", "samp", "br", "wbr", "label", "font", "bdi", "bdo", "data",
                "dfn", "ins", "big", "tt", "input"}
# 부모가 닫히지 않아도 새 형제가 열리면 닫히는 요소 (HTML 암시적 종료).
AUTO_CLOSE = {"p": BLOCK - {"span"}, "li": {"li"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"}, "tr": {"tr"},
              "td": {"td", "th", "tr"}, "th": {"td", "th", "tr"}, "thead": {"tbody", "tfoot"}, "tbody": {"tbody", "tfoot"},
              "option": {"option"}}
_WS = re.compile(r"[ \t\n\r\f]+")


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Optional[Dict[str, str]] = None, parent: "Node" = None):
        self.tag, self.attrs, self.children, self.parent = tag, attrs or {}, [], parent

    def classes(self) -> List[str]:
        return (self.attrs.get("class") or "").split()

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def find(self, tag: str) -> Optional["Node"]:
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == tag:
                    return child
                found = child.find(tag)
                if found is not None:
                    return found
        return None


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.current = self.root
        self.stray_end_tags = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        while self.current.tag in AUTO_CLOSE and tag in AUTO_CLOSE[self.current.tag]:
            self.current = self.current.parent
        node = Node(tag, {k.lower(): v or "" for k, v in attrs}, self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        self.current.children.append(Node(tag, {k.lower(): v or "" for k, v in attrs}, self.current))

    def handle_endtag(self, tag):
        tag = tag.lower()
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is self.root:
            if tag not in VOID:
                self.stray_end_tags += 1
            return
        self.current = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def parse_html(html: str) -> Tuple[Node, int, int]:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    unclosed, node = 0, builder.current
    while node is not builder.root:
        if node.tag not in AUTO_CLOSE and node.tag not in ("html", "body"):
            unclosed += 1
        node = node.parent
    return builder.root, builder.stray_end_tags, unclosed


class _Converter:
    def __init__(self):
        self.quality: List[Dict[str, str]] = []
        self.unknown: Dict[str, int] = {}
        self.footnotes: List[Tuple[str, str]] = []
        self.stats = {"tables": 0, "code_blocks": 0, "lists": 0, "links": 0, "headings": 0, "footnotes": 0, "math": 0}

    def note(self, code: str, message: str, severity: str = "warning") -> None:
        if not any(q["code"] == code for q in self.quality):
            self.quality.append({"code": code, "severity": severity, "message": message})

    # --- 인라인 -----------------------------------------------------------------------
    def inline(self, node: Node, ctx: Dict[str, Any]) -> str:
        parts: List[str] = []
        for child in node.children:
            if isinstance(child, str):
                parts.append(child if ctx.get("pre") else _WS.sub(" ", child))
            else:
                parts.append(self.inline_node(child, ctx))
        return "".join(parts)

    def inline_node(self, node: Node, ctx: Dict[str, Any]) -> str:
        tag = node.tag
        if tag in SKIP or self.hidden(node):
            return self.math(node) if self.is_math(node) else ""
        if self.is_math(node):
            return self.math(node)
        if tag == "br":
            return "<br>" if ctx.get("cell") else "\n"
        if tag == "wbr":
            return ""
        if tag == "img":
            alt, src = node.attrs.get("alt", ""), node.attrs.get("src", "")
            return f"![{alt}]({src})" if src else alt
        if tag == "input" and node.attrs.get("type") == "checkbox":
            return "[x] " if "checked" in node.attrs else "[ ] "
        if tag in BLOCK:
            # 인라인 자리에 온 블록(표 칸 안의 문단·목록 등) — 줄을 합쳐 담고 기록한다.
            if ctx.get("cell") and tag in ("p", "div", "ul", "ol", "li", "pre", "blockquote", "table"):
                self.note("table_cell_blocks_flattened", "표 칸 안의 문단·목록 등 블록 구조는 Markdown 표에 담을 수 없어 "
                                                         "<br> 로 이어 붙였습니다.")
                inner = self.inline(node, ctx).strip()
                return inner + "<br>" if inner else ""
            return self.inline(node, ctx)
        inner = self.inline(node, ctx)
        if tag == "a":
            href = node.attrs.get("href", "").strip()
            label = inner.strip()
            if self.footnote_ref(node):
                return ""   # 처리는 sup 에서
            if "footnote-backref" in node.classes() or href.startswith("#fnref") or href.startswith("#user-content-fnref"):
                return ""
            if not href or href.startswith("javascript:"):
                return inner
            self.stats["links"] += 1
            if label == href:
                return href
            title = node.attrs.get("title")
            return f"[{label or href}]({href}" + (f' "{title}"' if title else "") + ")"
        if tag in ("strong", "b"):
            return f"**{inner.strip()}**" if inner.strip() else inner
        if tag in ("em", "i", "cite", "var", "dfn"):
            return f"*{inner.strip()}*" if inner.strip() else inner
        if tag in ("del", "s", "strike"):
            return f"~~{inner.strip()}~~" if inner.strip() else inner
        if tag in ("code", "kbd", "samp", "tt"):
            text = node.text()
            fence = "`" * (max((len(m) for m in re.findall(r"`+", text)), default=0) + 1)
            pad = " " if text.startswith("`") or text.endswith("`") else ""
            return f"{fence}{pad}{text}{pad}{fence}" if text else ""
        if tag == "sup":
            ref = next((c for c in node.children if isinstance(c, Node) and self.footnote_ref(c)), None)
            if ref is not None:
                self.stats["footnotes"] += 1
                return f"[^{self.footnote_label(ref.attrs.get('href', ''))}]"
            return inner
        if tag == "q":
            return f"“{inner}”"
        if tag not in KNOWN_INLINE:
            self.unknown[tag] = self.unknown.get(tag, 0) + 1
        return inner

    @staticmethod
    def hidden(node: Node) -> bool:
        style = node.attrs.get("style", "").replace(" ", "").lower()
        return "hidden" in node.attrs or "display:none" in style or node.attrs.get("aria-hidden") == "true"

    @staticmethod
    def is_math(node: Node) -> bool:
        return "katex" in node.classes() or "katex-display" in node.classes()

    def math(self, node: Node) -> str:
        annotation = self._annotation(node)
        if annotation is None:
            return ""
        self.stats["math"] += 1
        display = "katex-display" in node.classes() or (node.parent is not None and "katex-display" in node.parent.classes())
        return f"$${annotation}$$" if display else f"${annotation}$"

    def _annotation(self, node: Node) -> Optional[str]:
        for child in node.children:
            if isinstance(child, Node):
                if child.tag == "annotation" and "tex" in child.attrs.get("encoding", ""):
                    return child.text().strip()
                found = self._annotation(child)
                if found is not None:
                    return found
        return None

    @staticmethod
    def footnote_ref(node: Node) -> bool:
        href = node.attrs.get("href", "")
        return node.tag == "a" and (href.startswith("#fn") and not href.startswith("#fnref")
                                    or href.startswith("#user-content-fn-") or "data-footnote-ref" in node.attrs)

    @staticmethod
    def footnote_label(href: str) -> str:
        label = re.sub(r"^#(?:user-content-)?fn-?", "", href)
        return label or "1"

    # --- 블록 -------------------------------------------------------------------------
    def blocks(self, node: Node, ctx: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        buffer: List[str] = []

        def flush():
            text = "".join(buffer)
            buffer.clear()
            text = "\n".join(line.strip(" ") for line in text.split("\n")).strip("\n")
            if text.strip():
                out.append(_escape_line_starts(text))

        for child in node.children:
            if isinstance(child, str):
                buffer.append(_WS.sub(" ", child))
                continue
            if child.tag in SKIP or self.hidden(child):
                if self.is_math(child):
                    buffer.append(self.math(child))
                continue
            if self.is_math(child) and "katex-display" in child.classes():
                flush()
                out.append(self.math(child))
                continue
            if child.tag in BLOCK and child.tag not in ("td", "th", "tr", "thead", "tbody", "tfoot", "caption"):
                flush()
                out.extend(self.block(child, ctx))
            else:
                buffer.append(self.inline_node(child, ctx))
        flush()
        return out

    def block(self, node: Node, ctx: Dict[str, Any]) -> List[str]:
        tag = node.tag
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.stats["headings"] += 1
            text = " ".join(self.inline(node, ctx).split())
            return [("#" * int(tag[1])) + " " + text] if text else []
        if tag == "hr":
            return ["---"]
        if tag == "pre":
            return [self.code_block(node)]
        if tag in ("ul", "ol"):
            return [self.list_block(node, ctx)] if node.find("li") is not None else []
        if tag == "blockquote":
            inner = "\n\n".join(self.blocks(node, ctx))
            return ["\n".join("> " + line if line else ">" for line in inner.split("\n"))] if inner else []
        if tag == "table":
            return [self.table(node)]
        if tag == "section" and ("footnotes" in node.classes() or "data-footnotes" in node.attrs):
            return self.footnote_section(node, ctx)
        if tag == "dl":
            return self.blocks(node, ctx)
        if tag == "dt":
            text = self.inline(node, ctx).strip()
            return [f"**{text}**"] if text else []
        if tag in ("details",):
            return self.blocks(node, ctx)
        return self.blocks(node, ctx)

    def code_block(self, node: Node) -> str:
        self.stats["code_blocks"] += 1
        code = node.find("code")
        source = code if code is not None else node
        text = source.text()
        if text.endswith("\n"):
            text = text[:-1]
        language = ""
        for candidate in ([code] if code is not None else []) + [node]:
            for cls in candidate.classes():
                match = re.fullmatch(r"(?:language|lang)-([A-Za-z0-9_+#.-]+)", cls)
                if match:
                    language = match[1]
                    break
            language = language or candidate.attrs.get("data-language", "")
            if language:
                break
        if code is not None:
            chrome = "".join(c if isinstance(c, str) else c.text() for c in node.children if c is not code
                             and not (isinstance(c, Node) and c.find("code") is code)).strip()
            if chrome:
                self.note("code_block_chrome_removed", "코드 블록 머리글(언어 표시·복사 버튼 등) 글자는 코드가 아니어서 제외했습니다.",
                          "info")
        fence_char = "`"
        longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
        fence = fence_char * max(3, longest + 1)
        return f"{fence}{language}\n{text}\n{fence}"

    def list_block(self, node: Node, ctx: Dict[str, Any]) -> str:
        self.stats["lists"] += 1
        ordered = node.tag == "ol"
        try:
            number = int(node.attrs.get("start", "1"))
        except ValueError:
            number = 1
        items = []
        for child in node.children:
            if not isinstance(child, Node) or child.tag != "li":
                if isinstance(child, Node) and child.tag in ("ul", "ol") and items:
                    nested = self.list_block(child, ctx)
                    items[-1] += "\n" + _indent(nested, items[-1].index(" ") + 1)
                continue
            marker = f"{number}." if ordered else "-"
            number += 1
            paragraphs = any(isinstance(c, Node) and c.tag in ("p", "pre", "blockquote", "table") for c in child.children)
            content = ("\n\n" if paragraphs else "\n").join(self.blocks(child, {**ctx, "list": True})) or ""
            first, _, rest = content.partition("\n")
            item = f"{marker} {first}".rstrip()
            if rest:
                item += "\n" + _indent(rest, len(marker) + 1)
            items.append(item)
        loose = any(isinstance(c, Node) and c.tag == "li" and any(isinstance(g, Node) and g.tag == "p" for g in c.children)
                    for c in node.children)
        return ("\n\n" if loose else "\n").join(items)

    def table(self, node: Node) -> str:
        self.stats["tables"] += 1
        rows: List[Tuple[List[str], bool, List[str]]] = []
        spans = False

        def collect(parent: Node, header_section: bool) -> None:
            nonlocal spans
            for child in parent.children:
                if not isinstance(child, Node):
                    continue
                if child.tag in ("thead", "tbody", "tfoot"):
                    collect(child, child.tag == "thead")
                elif child.tag == "tr":
                    cells, aligns, kinds = [], [], []
                    for cell in child.children:
                        if not isinstance(cell, Node) or cell.tag not in ("td", "th"):
                            continue
                        text = " ".join(self.inline(cell, {"cell": True}).replace("\n", " ").split())
                        text = re.sub(r"(?:\s*<br>)+$", "", text).replace("|", "\\|")
                        width = 1
                        for attr in ("colspan", "rowspan"):
                            value = cell.attrs.get(attr, "1").strip()
                            if value not in ("", "1"):
                                spans = True
                                if attr == "colspan" and value.isdigit():
                                    width = max(1, min(int(value), 50))
                        cells.extend([text] + [""] * (width - 1))
                        aligns.extend([_align(cell)] * width)
                        kinds.append(cell.tag)
                    if cells:
                        rows.append((cells, header_section or all(k == "th" for k in kinds), aligns))
                elif child.tag == "caption":
                    continue
                else:
                    collect(child, header_section)

        collect(node, False)
        caption = node.find("caption")
        if not rows:
            self.note("table_empty", "내용이 없는 표가 있어 비워 두었습니다.")
            return ""
        if spans:
            self.note("table_span_unrepresentable", "병합 셀(colspan·rowspan)은 Markdown 표로 나타낼 수 없어 빈 칸으로 펼쳤습니다. "
                                                    "원본 HTML 을 함께 확인하세요.")
        width = max(len(r[0]) for r in rows)
        if any(len(r[0]) != width for r in rows):
            self.note("table_ragged_rows", "행마다 칸 수가 달라 빈 칸을 채워 맞췄습니다.")
        header_index = 0 if rows[0][1] else None
        if header_index is None:
            self.note("table_header_missing", "표에 머리행이 없어 첫 행을 Markdown 표의 머리행으로 썼습니다.", "info")
        padded = [r[0] + [""] * (width - len(r[0])) for r in rows]
        aligns = rows[0][2] + [""] * (width - len(rows[0][2]))
        line = lambda cells: "| " + " | ".join(cells) + " |"
        separator = "| " + " | ".join({"left": ":---", "right": "---:", "center": ":---:"}.get(a, "---") for a in aligns) + " |"
        body = [line(padded[0]), separator] + [line(r) for r in padded[1:]]
        text = "\n".join(body)
        if caption is not None and caption.text().strip():
            text = " ".join(caption.text().split()) + "\n\n" + text
        return text

    def footnote_section(self, node: Node, ctx: Dict[str, Any]) -> List[str]:
        out = []
        for item in _walk(node):
            if item.tag == "li" and item.attrs.get("id", ""):
                label = self.footnote_label("#" + item.attrs["id"])
                text = " ".join(" ".join(self.blocks(item, ctx)).split())
                out.append(f"[^{label}]: {text}")
        return ["\n".join(out)] if out else self.blocks(node, ctx)


def _walk(node: Node):
    for child in node.children:
        if isinstance(child, Node):
            yield child
            yield from _walk(child)


def _align(cell: Node) -> str:
    value = (cell.attrs.get("align") or "").lower()
    style = cell.attrs.get("style", "").replace(" ", "").lower()
    match = re.search(r"text-align:(left|right|center)", style)
    return match[1] if match else value if value in ("left", "right", "center") else ""


def _indent(text: str, width: int) -> str:
    pad = " " * width
    return "\n".join(pad + line if line.strip() else line for line in text.split("\n"))


def _escape_line_starts(text: str) -> str:
    """문단 글자가 줄 첫머리에서 Markdown 표시로 읽히지 않게 한다 (화면에 보인 글자는 그대로 둔다)."""
    out = []
    for line in text.split("\n"):
        ordered = re.match(r"(\d{1,9})([.)])(\s)", line)
        if ordered:
            line = ordered[1] + "\\" + line[len(ordered[1]):]
        elif re.match(r"(?:#{1,6}\s|>|[-+*]\s|={3,}\s*$|-{3,}\s*$|`{3,}|~{3,}|\|)", line):
            line = "\\" + line
        out.append(line)
    return "\n".join(out)


def html_to_markdown(html: str) -> Dict[str, Any]:
    """{"markdown", "quality", "stats", "converter"} — 품질 기록은 code·severity·message."""
    if not isinstance(html, str):
        raise ValueError("HTML 은 문자열이어야 합니다.")
    converter = _Converter()
    try:
        root, stray, unclosed = parse_html(html)
    except Exception as exc:  # html.parser 는 거의 실패하지 않지만, 실패해도 원본을 지어내지 않는다
        return {"markdown": "", "converter": CONVERTER_VERSION, "stats": converter.stats,
                "quality": [{"code": "html_parse_failed", "severity": "error",
                             "message": f"HTML 을 해석하지 못했습니다 ({type(exc).__name__})."}]}
    body = root.find("body") or root
    markdown = "\n\n".join(block for block in converter.blocks(body, {}) if block.strip())
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip("\n")
    if stray or unclosed:
        converter.note("html_structure_repaired", f"짝이 맞지 않는 HTML 태그가 있었습니다 (닫는 태그만 있음 {stray}개, "
                                                  f"닫히지 않음 {unclosed}개). 구조를 추정해 변환했으니 원본을 확인하세요.")
    if converter.unknown:
        names = ", ".join(f"<{k}> {v}개" for k, v in sorted(converter.unknown.items())[:8])
        converter.note("html_unknown_elements", f"알 수 없는 요소는 글자만 옮겼습니다: {names}.", "info")
    visible = " ".join(body.text().split())
    if visible and not markdown.strip():
        converter.note("html_conversion_empty", "HTML 에 글자가 있지만 변환 결과가 비었습니다. 원본 구조를 복구하지 못했습니다.", "error")
    return {"markdown": markdown, "quality": converter.quality, "stats": converter.stats, "converter": CONVERTER_VERSION}


def visible_links(html: str) -> List[Dict[str, Any]]:
    """HTML 안의 링크(href·글자). 출처 후보 기록용 — 판정하지 않는다."""
    root, _, _ = parse_html(html)
    links = []
    for node in _walk(root):
        if node.tag == "a":
            href = node.attrs.get("href", "").strip()
            if href.startswith(("http://", "https://")):
                links.append({"url": href, "text": " ".join(node.text().split())})
    return links
