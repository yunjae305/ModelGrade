"""답변이 직접 제시한 출처와 그 출처가 붙은 답변 구절 (답변 출처 검증, G-4).

판정은 하지 않고 검증 대상만 만든다. 출처 링크가 열린다는 사실은 주장의 참을 뜻하지 않으며,
출처가 없다는 이유만으로 답변을 FAIL 로 만들지 않는다. 코드 블록·인라인 코드 안의 URL 은 출처가 아니다.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .analysis import TextAnalysis, mask_ranges
from .semantic_evidence import source_url

__all__ = ["CITATION_POLICY_VERSION", "MAX_CITATION_URLS", "answer_citations", "prompt_citations",
           "source_requested"]

#: v2 — 서버가 접근 문제로 받지 못한 출처는 같은 요청의 공급자 web_fetch 원문으로 대조 (F5b)
CITATION_POLICY_VERSION = "answer-citations-v2"
#: 한 답변에서 원문을 대조할 서로 다른 출처 URL 수. 평가자의 원문 열기 한도(8)를 넘지 않게 둔다.
MAX_CITATION_URLS = 4
MAX_CLAIMS_PER_URL = 3

_LINK = re.compile(r"(?<!!)\[(?P<text>[^\]\n]{1,200})\]\((?P<url>[^)\s]+)(?:\s+\"[^\"\n]*\")?\)")
_BARE = re.compile(r"https?://[^\s<>()\[\]\"'`]+", re.IGNORECASE)
_FOOTNOTE_DEF = re.compile(r"^[ ]{0,3}\[\^(?P<label>[^\]\s]{1,64})\][ \t]*:[ \t]*(?P<body>.+)$", re.M)
_REFERENCE_DEF = re.compile(r"^[ ]{0,3}\[(?P<label>\d{1,3})\][ \t]*:?[ \t]*(?P<body>.+)$", re.M)
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")
_SOURCE_WORDS = re.compile(r"출처|참고(?:\s*자료|\s*문헌)?|자료|근거|링크|source|reference|ref", re.IGNORECASE)
_QUESTION_ASKS = re.compile(r"출처|참고\s*(?:자료|문헌|링크)|레퍼런스|근거\s*(?:링크|자료)|링크(?:를|도)?\s*(?:함께|같이|달아|첨부|포함|붙여|적어)|"
                            r"\bsources?\b|\breferences?\b|\bcite\b", re.IGNORECASE)


def source_requested(question: str) -> bool:
    """질문이 출처·근거 링크를 요구했는가 (기록용 — 이것만으로 판정하지 않는다)."""
    return bool(_QUESTION_ASKS.search(question or ""))


def _clean_url(raw: str) -> str:
    return raw.rstrip(".,;:!?)]}>'\"”’」』")


def _sentence(text: str, position: int) -> Tuple[int, int]:
    """position 이 속한 문장(같은 줄 안)의 범위."""
    line_start = text.rfind("\n", 0, position) + 1
    line_end = text.find("\n", position)
    line_end = len(text) if line_end < 0 else line_end
    start = line_start
    for m in _SENTENCE_END.finditer(text, line_start, line_end):
        if m.end() <= position:
            start = m.end()
        else:
            return start, m.start()
    return start, line_end


def _claim(answer: str, masked: str, start: int, end: int) -> Optional[str]:
    """출처가 붙은 답변 문장 원문. 출처 표기만 있는 줄(‘출처: https://…’)이면 None (답변 전체에 대한 출처)."""
    s, e = _sentence(masked, start)
    e = max(e, min(end, len(answer)))
    sentence = answer[s:e].strip()
    rest = _LINK.sub(lambda m: m.group("text"), sentence)
    rest = _BARE.sub("", rest)
    rest = re.sub(r"\[\^?[^\]\s]{1,64}\]", "", rest)
    rest = _SOURCE_WORDS.sub("", rest)
    if len(re.sub(r"[\W_]+", "", rest)) < 4:
        return None
    return sentence


def answer_citations(answer: str) -> List[Dict[str, Any]]:
    """답변 출처 목록 — [{id, url, kind, valid_url, claims, checked}]. 같은 URL 은 하나로 묶는다."""
    if not isinstance(answer, str) or not answer.strip():
        return []
    masked = mask_ranges(answer, TextAnalysis(answer).code_exclusion_ranges)
    found: List[Tuple[str, str, int, int, Optional[str]]] = []   # (url, kind, start, end, marker)
    definition_lines = set()
    for pattern, kind in ((_FOOTNOTE_DEF, "footnote"), (_REFERENCE_DEF, "reference")):
        for m in pattern.finditer(masked):
            urls = [_clean_url(u.group(0)) for u in _BARE.finditer(m.group("body"))]
            if not urls:
                continue
            definition_lines.add(m.start())
            marker = f"[^{m.group('label')}]" if kind == "footnote" else f"[{m.group('label')}]"
            for url in urls:
                found.append((url, kind, m.start(), m.end(), marker))
    link_spans = []
    for m in _LINK.finditer(masked):
        url = m.group("url")
        if url.startswith("#"):
            continue   # 문서 내부 링크는 1-3 에서 본다
        link_spans.append((m.start(), m.end()))
        if masked.rfind("\n", 0, m.start()) + 1 not in definition_lines:   # 정의 줄의 링크는 정의 쪽에서 셈
            found.append((_clean_url(url), "link", m.start(), m.end(), None))
    for m in _BARE.finditer(masked):
        if any(s <= m.start() < e for s, e in link_spans):
            continue
        line_start = masked.rfind("\n", 0, m.start()) + 1
        if line_start in definition_lines:
            continue
        found.append((_clean_url(m.group(0)), "bare", m.start(), m.end(), None))

    citations: Dict[str, Dict[str, Any]] = {}
    for url, kind, start, end, marker in sorted(found, key=lambda item: item[2]):
        normalized = source_url(url)
        key = normalized or url
        item = citations.get(key)
        if item is None:
            item = citations[key] = {"id": f"S{len(citations) + 1}", "url": normalized or url, "kind": kind,
                                     "valid_url": normalized is not None, "claims": []}
        if marker:
            # 각주·번호 인용은 표시가 붙은 본문 문장이 주장이다.
            for use in re.finditer(re.escape(marker) + (r"(?!\s*:)" if kind == "footnote" else r"(?![:\d])"), masked):
                if answer.rfind("\n", 0, use.start()) + 1 in definition_lines:
                    continue
                claim = _claim(answer, masked, use.start(), use.end())
                if claim and claim not in item["claims"]:
                    item["claims"].append(claim)
        else:
            claim = _claim(answer, masked, start, end)
            if claim and claim not in item["claims"]:
                item["claims"].append(claim)
    out = []
    checked = 0
    for item in citations.values():
        item["claims"] = item["claims"][:MAX_CLAIMS_PER_URL]
        item["checked"] = item["valid_url"] and checked < MAX_CITATION_URLS
        checked += item["checked"]
        out.append(item)
    return out


def prompt_citations(citations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """평가자에게 보내는 형태 — 원문을 대조할 출처만, 주장이 없으면 답변 전체에 대한 출처로 둔다."""
    return [{"id": c["id"], "url": c["url"], "claims": list(c["claims"])} for c in citations if c["checked"]]
