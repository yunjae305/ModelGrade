"""1-1 외국 문자 구간 기록 — Unicode Script / Script_Extensions 기반.

이 모듈은 구간을 **기록만** 한다. 맥락 판정(PASS/FAIL)은 ``checks_integrity`` 의
``1-1.foreign_script`` 검사가 이 기록을 읽어 한다. 관측 레코드의 맥락 관련성은 ``null`` 이다.
문자 종류(Script)는 언어가 아니다 (UAX #24). Han 문자를 중국어라고 부르지 않는다.

Python 표준 ``unicodedata`` 에는 Script 속성이 없고 외부 패키지를 추가하지 않기 위해
Unicode 15.1.0 ``Scripts.txt`` 의 Han · Hiragana · Katakana 구간을 그대로 옮겼다.
Script_Extensions 는 이 세 문자 종류와 함께 쓰이는 Common/Inherited 문자 가운데
아래 ``_SCX`` 에 등록한 코드 포인트만 반영한다 (전체 표가 아님).
키릴·태국·아랍 문자 등 ``OTHER_SCRIPTS`` 는 글자·결합 부호의 문자 이름 첫 단어로 판별한다.
라틴·그리스(수식 기호로 흔히 쓰임)·한글은 외국 문자로 기록하지 않는다.
"""

from __future__ import annotations

import bisect
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "SCRIPT_DATA_VERSION",
    "OBSERVED_SCRIPTS",
    "OTHER_SCRIPTS",
    "script_of",
    "script_name",
    "observe_scripts",
]

#: 구간 표의 출처 버전. 실행 환경의 unicodedata 버전은 결과에 따로 남긴다.
SCRIPT_DATA_VERSION = ("Unicode 15.1.0 Scripts.txt (Han/Hiragana/Katakana) + 부분 Script_Extensions"
                       " + 문자 이름 접두어 (Cyrillic·Thai·Arabic 등)")

OBSERVED_SCRIPTS = ("Han", "Hiragana", "Katakana")

#: 문자 이름 첫 단어 → 문자 종류. 글자(L*)·결합 부호(M*)만 센다.
OTHER_SCRIPTS: Dict[str, str] = {
    "CYRILLIC": "Cyrillic", "THAI": "Thai", "ARABIC": "Arabic", "HEBREW": "Hebrew",
    "DEVANAGARI": "Devanagari", "BENGALI": "Bengali", "GURMUKHI": "Gurmukhi", "GUJARATI": "Gujarati",
    "ORIYA": "Oriya", "TAMIL": "Tamil", "TELUGU": "Telugu", "KANNADA": "Kannada", "MALAYALAM": "Malayalam",
    "SINHALA": "Sinhala", "LAO": "Lao", "TIBETAN": "Tibetan", "MYANMAR": "Myanmar", "KHMER": "Khmer",
    "MONGOLIAN": "Mongolian", "GEORGIAN": "Georgian", "ARMENIAN": "Armenian", "ETHIOPIC": "Ethiopic",
    "SYRIAC": "Syriac", "THAANA": "Thaana",
}

_RANGES: Dict[str, Tuple[Tuple[int, int], ...]] = {
    "Han": (
        (0x2E80, 0x2E99), (0x2E9B, 0x2EF3), (0x2F00, 0x2FD5), (0x3005, 0x3005),
        (0x3007, 0x3007), (0x3021, 0x3029), (0x3038, 0x303B), (0x3400, 0x4DBF),
        (0x4E00, 0x9FFF), (0xF900, 0xFA6D), (0xFA70, 0xFAD9), (0x16FE2, 0x16FE3),
        (0x16FF0, 0x16FF1), (0x20000, 0x2A6DF), (0x2A700, 0x2B739), (0x2B740, 0x2B81D),
        (0x2B820, 0x2CEA1), (0x2CEB0, 0x2EBE0), (0x2EBF0, 0x2EE5D), (0x2F800, 0x2FA1D),
        (0x30000, 0x3134A), (0x31350, 0x323AF),
    ),
    "Hiragana": (
        (0x3041, 0x3096), (0x309D, 0x309F), (0x1B001, 0x1B11F), (0x1B132, 0x1B132),
        (0x1B150, 0x1B152), (0x1F200, 0x1F200),
    ),
    "Katakana": (
        (0x30A1, 0x30FA), (0x30FD, 0x30FF), (0x31F0, 0x31FF), (0x32D0, 0x32FE),
        (0x3300, 0x3357), (0xFF66, 0xFF6F), (0xFF71, 0xFF9D), (0x1AFF0, 0x1AFF3),
        (0x1AFF5, 0x1AFFB), (0x1AFFD, 0x1AFFE), (0x1B000, 0x1B000), (0x1B120, 0x1B122),
        (0x1B155, 0x1B155), (0x1B164, 0x1B167),
    ),
}

#: 등록한 Script_Extensions (Script 값은 Common 또는 Inherited).
#: 한글과도 함께 쓰이는 문장부호(、。「」 등)는 Hang 을 포함하므로 단독으로 구간을 열지 않는다.
_SCX: Dict[int, Tuple[str, ...]] = {}
for _cp in (0x3099, 0x309A, 0x309B, 0x309C, 0x30A0, 0x30FC, 0xFF70, 0xFF9E, 0xFF9F,
            0x3031, 0x3032, 0x3033, 0x3034, 0x3035):
    _SCX[_cp] = ("Hiragana", "Katakana")
for _cp in (0x3006, *range(0x3190, 0x31A0), *range(0x31C0, 0x31E4)):
    _SCX[_cp] = ("Han",)
for _cp in (0x303C, 0x303D):
    _SCX[_cp] = ("Han", "Hiragana", "Katakana")
for _cp in (0x3001, 0x3002, 0x3003, 0x30FB, *range(0x3008, 0x3012), *range(0x3013, 0x3020)):
    _SCX[_cp] = ("Bopomofo", "Hangul", "Han", "Hiragana", "Katakana", "Yi")

_STARTS: List[int] = []
_TABLE: List[Tuple[int, int, str]] = []
for _name, _spans in _RANGES.items():
    for _lo, _hi in _spans:
        _TABLE.append((_lo, _hi, _name))
_TABLE.sort()
_STARTS = [row[0] for row in _TABLE]


def script_of(ch: str) -> Optional[str]:
    """등록한 세 Script 중 하나면 그 이름, 아니면 None (다른 Script 는 판별하지 않는다)."""
    cp = ord(ch)
    index = bisect.bisect_right(_STARTS, cp) - 1
    if index >= 0:
        lo, hi, name = _TABLE[index]
        if lo <= cp <= hi:
            return name
    return None


def script_name(ch: str) -> Optional[str]:
    """외국 문자로 기록하는 문자 종류 이름. 한자·가나는 구간 표, 그 밖은 문자 이름으로 판별한다."""
    script = script_of(ch)
    if script is not None:
        return script
    if not unicodedata.category(ch).startswith(("L", "M")):
        return None
    return OTHER_SCRIPTS.get(unicodedata.name(ch, "").split(" ", 1)[0])


def _extension_of(ch: str) -> Tuple[str, ...]:
    scx = _SCX.get(ord(ch), ())
    return tuple(s for s in scx if s in OBSERVED_SCRIPTS)


def observe_scripts(text: str, excluded: Sequence[Tuple[int, int]] = (),
                    region_of=None, question: str = "",
                    margin: int = 20) -> Dict[str, Any]:
    """코드 영역을 뺀 본문에서 외국 문자(한자·가나와 ``OTHER_SCRIPTS``) 구간을 기록한다.

    - ``excluded`` : 본문 투영에서 제외하는 코드 구획·인라인 코드 범위 [(start, end)].
    - ``region_of`` : 위치 → 구조 표지 dict 를 돌려주는 함수 (인용·괄호 등). 판정이 아니다.
    반환값의 ``context_relevance`` 는 항상 None 이다.
    """
    excluded = sorted(excluded)
    ex_starts = [s for s, _ in excluded]

    def in_excluded(i: int) -> bool:
        k = bisect.bisect_right(ex_starts, i) - 1
        return k >= 0 and excluded[k][0] <= i < excluded[k][1]

    spans: List[Dict[str, Any]] = []
    counts = {name: 0 for name in OBSERVED_SCRIPTS}
    ext_counts = {name: 0 for name in OBSERVED_SCRIPTS}
    current: Optional[Dict[str, Any]] = None
    for i, ch in enumerate(text):
        if in_excluded(i):
            current = None
            continue
        script = script_name(ch)
        if script is not None:
            counts[script] = counts.get(script, 0) + 1
            if current is None:
                current = {"start": i, "end": i + 1, "scripts": {script: 1}}
                spans.append(current)
            else:
                current["end"] = i + 1
                current["scripts"][script] = current["scripts"].get(script, 0) + 1
            continue
        ext = _extension_of(ch)
        # 확장 문자는 이미 열린 구간을 잇기만 하고, 한글 문맥과 공유되는 부호는 구간을 열지 않는다.
        if ext and current is not None and current["end"] == i:
            current["end"] = i + 1
            for name in ext:
                ext_counts[name] += 1
            continue
        current = None

    out = []
    for index, span in enumerate(spans, start=1):
        start, end = span["start"], span["end"]
        item = {
            "span_id": f"S{index}",
            "start": start,
            "end": end,
            "span_text": text[start:end],
            "scripts": span["scripts"],
            "surrounding_text": text[max(0, start - margin):min(len(text), end + margin)],
            "same_text_in_question": bool(question) and text[start:end] in question,
        }
        if region_of is not None:
            item["structure"] = region_of(start)
        out.append(item)
    return {
        "type": "observation",
        "observation_id": "foreign_script_spans",
        "script_data_version": SCRIPT_DATA_VERSION,
        "runtime_unicodedata_version": unicodedata.unidata_version,
        "script_counts": counts,
        "script_extension_counts": ext_counts,
        "span_count": len(out),
        "script_spans": out,
        "excluded_regions": [list(r) for r in excluded],
        "context_relevance": None,
        "context_relevance_reason": (
            "관측 레코드는 위치만 기록함: 맥락 판정은 1-1.foreign_script 결과 "
            "(질문 주제·주변 설명·괄호 병기가 없으면 FAIL)에 남김"),
        "counted_in_pass_rate": False,
    }
