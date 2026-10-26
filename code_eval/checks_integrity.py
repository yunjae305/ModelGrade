"""1. 출력 무결성 — 1-1 응답·문자 정상성 / 1-2 문서·코드 유효성 / 1-3 참조 연결 유효성."""

from __future__ import annotations

import ast
import json
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from .analysis import TextAnalysis, mask_ranges
from .core import (ERROR_LIMIT_EXCEEDED, LIMITS, MD_POLICY_VERSION, Ctx, case_error, cmp_text, compare, excerpt,
                   q_evidence, result, skip_condition, skip_req, verdict_of)
from .extract import LANG_ALIASES
from .model import Evidence, FAIL, PASS, Status
from .scripts import observe_scripts, script_name
from .textparse import CORRUPTION_SIGNATURES, _json_type_name, _parse_footnotes, _parse_references, _reject_json_constant, find_corruption, find_markdown_tables, internal_links, link_targets, strip_non_content

__all__ = ["check_1_1", "check_1_2", "check_1_3", "json_targets", "foreign_script_observation"]

SUB01 = "0-1"   # 에러 케이스(대분류 0, 10-26) — 1-1.response_nonempty 는 ID 그대로 소분류만 0-1
SUB11, SUB12, SUB13 = "1-1", "1-2", "1-3"


# =====================================================================================
# 1-1 응답·문자 정상성
# =====================================================================================

_ALLOWED_CP = re.compile(r"U\+(?P<cp>[0-9A-Fa-f]{4,6})")
_ALLOW_VERB = re.compile(r"출력|포함|보여|예시|넣어|써\s*줘|적어")
#: 인코딩·문자 깨짐을 다루는 말 — 질문 주제이거나, 깨짐 표기와 같은 구조 범위의 설명이면 예시로 본다.
_ENCODING_TOPIC = re.compile(
    r"인코딩|디코딩|UTF|유니코드|unicode|문자\s*(?:코드|집합|셋)|charset|EUC-?KR|CP949|ASCII|바이트|"
    r"깨진|깨짐|깨져|깨지|대체\s*문자|replacement\s*character|mojibake|모지바케|제어\s*문자|U\+[0-9A-Fa-f]{4}|"
    r"잘못\s*(?:읽|해석|표시)",
    re.IGNORECASE,
)
_EXAMPLE_WORDS = re.compile(r"예시|예를\s*들어|예컨대")
#: 깨짐을 실제 출력 결과로 제시하는 말. 같은 문장에 예시·가정 표현이 없으면 실제 결함으로 본다.
_ACTUAL_OUTPUT = re.compile(r"결과는|결과가|실제\s*출력|출력\s*결과|출력은|현재\s*값|읽은\s*결과|생성된\s*결과|응답에서\s*나타")
_HYPOTHETICAL = re.compile(r"예시|예를\s*들어|예컨대|처럼|수\s*있")
_SENTENCE_GAP = re.compile(r"(?<=[.!?。！？])\s+")
#: 외국 문자를 다룬다고 볼 질문 주제
_SCRIPT_TOPIC = re.compile(
    r"한자|일본어|히라가나|가타카나|가나\s*문자|중국어|간체|번체|러시아어|키릴|태국어|아랍어|히브리어|힌디어|"
    r"외국어|원어|원문|번역|표기|읽는\s*법|발음")
#: 같은 구조 범위에서 외국 문자를 설명하는 말
_SCRIPT_EXPLAINED = re.compile(
    r"뜻|의미|읽(?:는|으면|기|음)|발음|표기|한자|일본어|중국어|러시아어|원어|원문|번역|병기|고유\s*명사|지명|인명|"
    r"인사말|단어|예시|예를\s*들어|예컨대")
#: 외국 문자 바로 뒤의 괄호 병기 — 괄호 안에 한글이 있어야 한다. 예: 浅草寺(센소지)
_GLOSS_AFTER = re.compile(r"\s*[(（][^()（）\n]{0,40}?[가-힣][^()（）\n]{0,40}[)）]")


def _allowed_codepoints(ctx: Ctx) -> Dict[int, str]:
    """질문이 특정 코드 포인트의 출력을 명시적으로 요청한 경우만 예외로 둔다 (단순 언급은 허용이 아님)."""
    allowed: Dict[int, str] = {}
    text = ctx.conds.instruction_text
    for m in _ALLOWED_CP.finditer(text):
        sentence_end = min([i for i in (text.find(c, m.end()) for c in ".!?\n") if i >= 0] + [len(text)])
        if _ALLOW_VERB.search(text[m.end():sentence_end]):
            allowed[int(m.group("cp"), 16)] = text[m.start():sentence_end].strip()
    return allowed


def _c0_controls(text: str) -> List[Dict[str, Any]]:
    hits = []
    for i, ch in enumerate(text):
        if unicodedata.category(ch) == "Cc" and ch not in "\t\n\r" and ord(ch) < 0x80:
            hits.append({"signature_id": "c0-control", "char_range": [i, i + 1],
                         "codepoints": [f"U+{ord(ch):04X}"], "description": "C0 제어 문자 (탭·줄바꿈 제외)"})
    return hits


def _line_index(lines, pos: int) -> int:
    """pos 가 들어 있는 줄의 번호(0부터). 줄 끝의 줄바꿈 위치는 그 줄로 본다."""
    for i, line in enumerate(lines):
        if pos <= line.end:
            return i
    return len(lines) - 1


def _meaningful(line) -> bool:
    return not line.in_code and bool(line.text.strip())


def _near_line(lines, index: int, step: int) -> str:
    """index 줄에서 step(-1/+1) 방향으로 처음 만나는 의미 있는 줄 (빈 줄·코드 줄은 건너뜀)."""
    i = index + step
    while 0 <= i < len(lines):
        if _meaningful(lines[i]):
            return lines[i].text
        i += step
    return ""


def _near_paragraph(lines, index: int, step: int) -> str:
    """index 줄에서 step 방향으로 처음 만나는 본문 덩어리 전체 — 코드블록 앞·뒤 설명."""
    i, found = index + step, []
    while 0 <= i < len(lines) and not _meaningful(lines[i]):
        i += step
    while 0 <= i < len(lines) and _meaningful(lines[i]):
        found.append(lines[i].text)
        i += step
    return "\n".join(reversed(found) if step < 0 else found)


def _list_item_span(ctx: Ctx, index: int) -> Optional[Tuple[int, int, int]]:
    """index 줄이 속한 목록 항목의 (첫 줄, 끝 줄, 목록 블록의 첫 줄). 목록이 아니면 None."""
    lines = ctx.aa.lines
    starts = {_line_index(lines, item.line.start) for item in ctx.aa.list_items}

    def continued(i: int) -> bool:
        return _meaningful(lines[i]) and lines[i].text[:1] in (" ", "\t")

    first = index
    while first not in starts:
        if first <= 0 or not continued(first):
            return None
        first -= 1
    last = index
    while last + 1 < len(lines) and last + 1 not in starts and continued(last + 1):
        last += 1
    top = first
    while top > 0 and _meaningful(lines[top - 1]) and (top - 1 in starts or continued(top - 1)):
        top -= 1
    return first, last, top


def _md_tables(ctx: Ctx):
    if "md_tables" not in ctx.shared:
        ctx.shared["md_tables"] = find_markdown_tables(ctx.answer, ctx.aa.lines)
    return ctx.shared["md_tables"]


def _connected_context(ctx: Ctx, pos: int) -> Tuple[str, str]:
    """pos 와 구조적으로 이어진 범위의 (범위 이름, 글). 앞뒤 몇 줄을 기계적으로 합치지 않는다.

    코드블록 → 블록 내용 + 바로 앞·뒤 설명 문단 / 표 → 머리글 + 같은 행 + 표 앞·뒤 줄 /
    목록 → 같은 항목(이어지는 줄 포함) + 목록을 여는 줄 / 그 밖 → 같은 문단 + 앞·뒤 의미 있는 줄.
    """
    aa, lines = ctx.aa, ctx.aa.lines
    for block in aa.fenced_blocks:
        if block["start"] <= pos < block["end"]:
            first = _line_index(lines, block["start"])
            last = _line_index(lines, max(block["start"], block["end"] - 1))
            return "코드블록과 앞뒤 설명", "\n".join(
                (_near_paragraph(lines, first, -1), block["content"], _near_paragraph(lines, last, 1)))
    index = _line_index(lines, pos)
    for table in _md_tables(ctx):
        if table.start <= pos < table.end:
            first = _line_index(lines, table.start)
            last = _line_index(lines, max(table.start, table.end - 1))
            return "같은 표 행과 머리글", "\n".join(
                (_near_line(lines, first, -1), lines[first].text, lines[index].text, _near_line(lines, last, 1)))
    item = _list_item_span(ctx, index)
    if item is not None:
        first, last, top = item
        return "같은 목록 항목", "\n".join(
            [_near_line(lines, top, -1)] + [lines[i].text for i in range(first, last + 1)])
    para = next((p for p in aa.paragraphs if p.start <= pos <= p.end), None)
    if para is None:
        return "같은 줄", "\n".join((_near_line(lines, index, -1), lines[index].text, _near_line(lines, index, 1)))
    first, last = _line_index(lines, para.start), _line_index(lines, para.end)
    return "같은 문단", "\n".join((_near_line(lines, first, -1), para.text, _near_line(lines, last, 1)))


def _sentence_at(ctx: Ctx, pos: int) -> str:
    line = ctx.aa.lines[_line_index(ctx.aa.lines, pos)]
    offset = pos - line.start
    gaps = list(_SENTENCE_GAP.finditer(line.text))
    start = max([m.end() for m in gaps if m.end() <= offset], default=0)
    end = min([m.start() for m in gaps if m.start() >= offset], default=len(line.text))
    return line.text[start:end]


def _corruption_reading(ctx: Ctx, hit: Dict[str, Any]) -> Tuple[str, str]:
    """깨짐 표기 하나를 ('example' | 'actual' | 'unexplained', 근거) 로 읽는다."""
    pos = hit["char_range"][0]
    sentence = _sentence_at(ctx, pos)
    if _ACTUAL_OUTPUT.search(sentence) and not _HYPOTHETICAL.search(sentence):
        return "actual", "같은 문장이 실제 출력 결과로 제시함"
    topic = _ENCODING_TOPIC.search(ctx.question)
    if topic:
        return "example", f"질문이 다루는 주제 '{topic.group(0)}'"
    unit, text = _connected_context(ctx, pos)
    cue = _ENCODING_TOPIC.search(text) or _EXAMPLE_WORDS.search(text)
    if cue:
        return "example", f"{unit}에 설명·예시 표현 '{cue.group(0)}'"
    return "unexplained", f"{unit}에 설명·예시 없음"


#: 1-1 에러 케이스 ③ — 능력 부정 정형 문구(요청과 무관한 고정 거절). 자기 설명(언어 모델 · 텍스트 기반 AI …)과 거절 말이 함께 있고
#: 결과물 없이 짧을 때만. 발견될 때마다 추가한다 (10-23 미팅 2 · 3번, 10-25: Gemini 3.8 Flash 웹 "저는 언어 모델일 뿐이라 … 도와드릴 수가 없습니다").
ERROR_CASE_PHRASES = (
    re.compile(r"언어\s*모델(일\s*뿐|이라서|이기\s*때문|이라|이므로)"),
    re.compile(r"텍스트\s*기반\s*(AI|인공지능|모델)"),
    re.compile(r"(I'?m|I am)\s+(just\s+|only\s+)?(a|an)\s+(large\s+)?(language\s+model|text-based)", re.I),
    re.compile(r"as an AI( language)? model", re.I),
    # 10-25 밤 v4-r9-chk Gemini Q7 8턴: "그것은 저의 프로그래밍 범위를 넘어서는 것입니다. 저는 텍스트를 생성할 뿐이에요."
    re.compile(r"(프로그래밍|능력|처리|기능)\s*(의\s*)?범위를\s*(넘어|벗어)"),
    re.compile(r"텍스트(만|를)\s*(생성|만들)할\s*뿐"),
)
ERROR_CASE_REFUSAL = re.compile(r"도와\s*드릴\s*수(가)?\s*없|할\s*수(가)?\s*없|이해하지\s*못|도움을\s*드릴\s*수\s*없|범위를\s*(넘어|벗어)|생성할\s*뿐|cannot|can'?t|unable", re.I)
ERROR_CASE_MAX_CHARS = 300   # 이보다 길면 결과물이 있는 것으로 보고 ③ 을 적용하지 않는다
COLLECTION_OK = ("SUCCESS", "COPY_FAILED")   # SUCCESS 는 정상, COPY_FAILED 는 우리 원인(행 없이 보류)


#: 질문이 그 문구 자체를 다루면(번역 · 인용 · 예문) 답에 들어 있어도 거절이 아니다 (Codex 검토 10-25)
ERROR_CASE_QUESTION_SKIP = re.compile(r"번역|옮겨|영어로|한국어로|따옴표|인용|예문|그대로 써|translate|quote", re.I)
ERROR_CASE_HEAD_CHARS = 80   # 거절 문구는 답 머리에 온다 — 결과물 뒤에 붙은 안내문은 거절이 아니다
ERROR_CASE_MAX_LINES = 3     # 줄이 더 많으면 결과물(목록 · 항목)이 있는 것으로 본다
ERROR_CASE_CONTRAST = re.compile(r"하지만|그러나|다만|그래도|대신|\bbut\b|however|though|instead", re.I)   # 거절 뒤 결과물로 이어지는 표지
#: 1-1 에러 케이스 ④ — 서비스 오류 정형 문구(모델이 아니라 서비스 · 전송 단계의 오류 메시지가 답 자리에 온 것). 전체가 오류 · 재시도 · 사과 문장뿐일 때만
#: (10-25 밤: AIONA Q6 5 · 7턴 "응답 생성 중 오류가 발생했습니다. 다시 시도해주세요."를 LLM 항목 3개가 각각 FAIL 로 세던 것 — 한 오류 한 항목, CODE 1-1 이 잡는다)
SERVICE_ERROR_PHRASES = re.compile(r"오류가\s*발생|문제가\s*발생|응답을\s*생성(할\s*수\s*없|하지\s*못)|생성\s*중\s*오류|생성에\s*실패|요청을\s*처리(할\s*수\s*없|하지\s*못)|"
                                   r"서버\s*오류|일시적인?\s*오류|처리\s*중\s*오류|error occurred|something went wrong|internal server error|service (is )?unavailable|"
                                   r"failed to generate", re.I)
SERVICE_RETRY = re.compile(r"다시\s*시도|잠시\s*후|나중에\s*다시|try again|please retry|later", re.I)
#: 거절 문장 뒤에 와도 결과물이 아닌 인사 · 사과 · 안내만 (이 밖의 글이 있으면 거절 답이 아님)
ERROR_CASE_COURTESY = re.compile(r"(?:\s*(?:죄송합니다|죄송해요|양해 부탁드립니다|이해해 주세요|다른 (?:질문|도움)이 (?:있으시면|필요하시면)[^.!?]*|"
                                 r"(?:I'?m )?sorry[^.!?]*|I apologi[sz]e[^.!?]*|let me know[^.!?]*|please ask[^.!?]*)[.!?]*)+\s*", re.I)


def instruction_text(question: str) -> str:
    """붙여 넣은 자료([붙여 넣은 자료] … [자료 끝]) 구간을 뺀 질문 — 번역 · 인용을 시켰는지는 지시문에서만 본다
    (10-25 밤 보류 분석: 자료 속 '1500원을 남은 예상 쪽으로 옮겨야' 의 '옮겨' 로 검사가 꺼져 Gemini 의 정형 거절이 Codex 로 감 — 78 세트 90턴이 자료 속 낱말로 꺼지던 것)."""
    from .analysis import pasted_ranges
    out, cursor = [], 0
    for a, b in pasted_ranges(question or ""):
        out.append(question[cursor:a])
        cursor = b
    out.append((question or "")[cursor:])
    return "".join(out)


def error_case_phrase(answer: str, content_chars: int, question: str = "") -> Optional[str]:
    """③ 에 해당하면 맞은 문구를, 아니면 None — 답 전체가 거절일 때만: 짧고(300자 · 3줄 이하), 코드블록 없고, 문구가 답 머리(80자 안)에 있고,
    질문이 번역 · 인용을 시킨 것이 아님. 애매하면 None(LLM 판정 몫). 문구는 원문(answer)에서 찾는다 — 공백을 지운 글에서는 영어 문구가 안 잡힌다."""
    if content_chars > ERROR_CASE_MAX_CHARS or "```" in answer:
        return None
    if question and ERROR_CASE_QUESTION_SKIP.search(instruction_text(question)):
        return None
    lines = [l for l in answer.splitlines() if l.strip()]
    if len(lines) > ERROR_CASE_MAX_LINES:
        return None
    head = answer.lstrip()
    first = re.split(r"(?<=[.!?。])\s+|\n", head, maxsplit=1)[0]   # 첫 문장 — 거절 답은 첫 문장이 자기 설명이다. 결과물(목록 · 숫자)로 시작하면 거절 아님
    if re.match(r"^\s*(\d+[.)]|[-*•·]|\|)", first):
        return None
    hit = next((m.group(0) for p in ERROR_CASE_PHRASES for m in [p.search(first)] if m and m.start() <= ERROR_CASE_HEAD_CHARS), None)
    if not hit or not ERROR_CASE_REFUSAL.search(head[:200]):
        return None
    if ERROR_CASE_CONTRAST.search(first):   # "…할 수 없지만 2 + 2 = 4" — 거절 뒤에 결과물이 이어지는 문장
        return None
    rest = head[len(first):].strip()
    if rest and not ERROR_CASE_COURTESY.fullmatch(rest):   # 첫 문장 뒤에 인사말 말고 다른 글(숫자 · 답)이 있으면 결과물이 있는 답 (Codex 검토 10-25)
        # 뒤 문장이 모두 인사말이거나 또 다른 능력 부정 문장('저는 텍스트를 생성할 뿐이에요')이면 거절 답 그대로 (10-25 밤 Gemini Q7 8턴)
        sents = [x for x in re.split(r"(?<=[.!?。])\s+|\n", rest) if x.strip()]
        if not all(ERROR_CASE_COURTESY.fullmatch(x + " ") or ERROR_CASE_COURTESY.fullmatch(x) or any(pt.search(x) for pt in ERROR_CASE_PHRASES)
                   for x in sents):
            return None
    return hit


def service_error_phrase(answer: str, content_chars: int) -> Optional[str]:
    """④ 에 해당하면 맞은 문구를, 아니면 None — 답 전체가 서비스 오류 메시지일 때만: 짧고(300자 · 3줄 이하), 코드블록 없고, 문장마다 오류 · 재시도 · 사과 · 인사뿐.
    '오류가 발생했을 때는 로그를 확인하세요' 같은 설명 답은 다른 문장이 있어 걸리지 않는다."""
    if content_chars > ERROR_CASE_MAX_CHARS or "```" in answer:
        return None
    lines = [l for l in answer.splitlines() if l.strip()]
    if len(lines) > ERROR_CASE_MAX_LINES:
        return None
    hit = None
    for sent in re.split(r"(?<=[.!?。])\s+|\n", answer.strip()):
        sent = re.sub(r"^[\s⚠️❗❌!•·\-*]+", "", sent).strip()
        if not sent:
            continue
        m = SERVICE_ERROR_PHRASES.search(sent)
        if m:
            hit = hit or m.group(0)
            continue
        if SERVICE_RETRY.search(sent) or ERROR_CASE_COURTESY.fullmatch(sent + " ") or ERROR_CASE_COURTESY.fullmatch(sent):
            continue
        return None   # 오류 · 재시도 · 사과가 아닌 문장이 있으면 결과물이 있는 답
    return hit


def check_1_1(ctx: Ctx) -> List:
    out = []
    empty_req = ctx.conds.first("explicit_empty")
    content = strip_non_content(ctx.answer)
    status = (ctx.collection_status or "").upper()
    # 질문이 빈 출력을 명시 요청하면 에러 케이스 행을 만들지 않는다 (요청 준수 여부는 2-1.explicit_empty 가 판정).
    if empty_req is None and status == "COPY_FAILED":
        # 복사 실패는 우리 원인 — 행 없이 사유만 남기고 질문은 보류(사람이 정함, 10-25)
        skip_condition(ctx, "1-1.response_nonempty", SUB01, "복사 실패(수집기 원인) — 에러 케이스로 세지 않음, 질문은 보류")
    elif empty_req is None:
        kind, reason = None, "실질 문자 {}자 확인 — 에러 케이스 아님".format(len(content))
        if status and status not in COLLECTION_OK and not content:
            kind, reason = "답 없음", "답 없음 — 수집 상태 {}{} (오류 코드 · 시간 초과 · 무응답)".format(status, f": {ctx.collection_error[:80]}" if ctx.collection_error else "")
        elif not content:
            kind, reason = "빈 답", "공백·제어문자를 제거하면 남는 문자가 없음 (답이 비어 있음)"
        else:
            hit = error_case_phrase(ctx.answer, len(content), ctx.question)
            if hit:
                kind, reason = "능력 부정 문구", "능력 부정 정형 문구만 있고 결과물 없음: '{}'".format(hit)
            else:
                svc = service_error_phrase(ctx.answer, len(content))
                if svc:
                    kind, reason = "서비스 오류 문구", "서비스 오류 정형 문구만 있고 결과물 없음: '{}'".format(svc)
        ok = kind is None
        out.append(result("1-1.response_nonempty", SUB01, "에러 케이스 없음", "operating_policy", Status.EXECUTED,
                          verdict_of(ok), required="빈 답 · 답 없음(수집 오류) · 능력 부정 정형 문구만 · 서비스 오류 문구만이 아님",
                          measured={"content_chars": len(content), "raw_chars": len(ctx.answer), "collection_status": status or None, "error_case": kind},
                          comparison=cmp_text(len(content), ">=", 1),
                          reason=reason, evidence=[Evidence("answer", 0, len(ctx.answer), ctx.answer[:60])]))

    allowed = _allowed_codepoints(ctx)
    hits = find_corruption(ctx.answer) + _c0_controls(ctx.answer)
    hits.sort(key=lambda h: (h["char_range"][0], h["signature_id"]))
    kept, excepted, illustrative, readings = [], [], [], []
    for h in hits:
        cps = [int(c[2:], 16) for c in h["codepoints"]]
        if cps and all(cp in allowed for cp in cps):
            excepted.append(h)
            continue
        kind, basis = _corruption_reading(ctx, h)
        readings.append({"char_range": h["char_range"], "signature_id": h["signature_id"],
                         "reading": kind, "basis": basis})
        (illustrative if kind == "example" else kept).append(h)
    rules = [s["id"] for s in CORRUPTION_SIGNATURES] + ["c0-control"]
    ok = not kept
    why = " · ".join(dict.fromkeys("실제 출력 결과로 제시됨" if r["reading"] == "actual" else "주변에 설명·예시 없음"
                                   for r in readings if r["reading"] != "example"))
    out.append(result(
        "1-1.character_rules", SUB11, "문자 규칙 (깨진 문자·제어 문자)", "operating_policy", Status.EXECUTED,
        verdict_of(ok), required={"registered_rules": rules, "explicit_allowed_codepoints":
                                  [f"U+{cp:04X}" for cp in allowed]},
        measured={"violations": len(kept), "codepoints": sorted({c for h in kept for c in h["codepoints"]}),
                  "excepted_by_question": len(excepted), "illustrative_examples": len(illustrative)},
        comparison=cmp_text(len(kept), "==", 0),
        reason=(f"등록 문자 규칙 위반 없음 (설명·예시 맥락의 깨짐 표기 {len(illustrative)}건 제외)" if ok and illustrative
                else "등록 문자 규칙 위반 없음" if ok else
                f"등록 문자 규칙 위반 {len(kept)}건: " + ", ".join(sorted({h['signature_id'] for h in kept}))
                + f" — {why} (손상 원인은 추정하지 않음)"),
        evidence=[Evidence("answer", h["char_range"][0], h["char_range"][1],
                           "코드 포인트 " + " ".join(h["codepoints"]), h["signature_id"]) for h in kept[:20]],
        details={"allowed_requests": {f"U+{k:04X}": v for k, v in allowed.items()}, "readings": readings[:20]}))
    foreign = _foreign_script_check(ctx)
    if foreign is not None:
        out.append(foreign)
    return out


def _paired_spans(text: str, pairs: Tuple[Tuple[str, str], ...]) -> List[Tuple[int, int]]:
    spans = []
    for o, c in pairs:
        for m in re.finditer(re.escape(o) + r"[^" + re.escape(c) + r"\n]{0,200}" + re.escape(c), text):
            spans.append((m.start(), m.end()))
    return spans


def _script_spans(ctx: Ctx) -> Dict[str, Any]:
    """외국 문자 구간 기록. 1-1 판정과 관측 레코드가 같은 기록을 쓴다."""
    if "foreign_scripts" not in ctx.shared:
        text = ctx.answer
        quotes = _paired_spans(text, (("“", "”"), ("\"", "\""), ("「", "」"), ("『", "』"), ("‘", "’"), ("'", "'")))
        parens = _paired_spans(text, (("(", ")"), ("（", "）")))
        quote_lines = [(l.start, l.end) for l in ctx.aa.lines if l.text.lstrip().startswith(">")]

        def region_of(i: int) -> Dict[str, bool]:
            return {
                "in_blockquote": any(s <= i < e for s, e in quote_lines),
                "in_quotes": any(s < i < e for s, e in quotes),
                "in_parentheses": any(s < i < e for s, e in parens),
            }

        ctx.shared["foreign_scripts"] = observe_scripts(text, excluded=ctx.aa.code_exclusion_ranges,
                                                        region_of=region_of, question=ctx.question)
    return ctx.shared["foreign_scripts"]


def _script_reading(ctx: Ctx, span: Dict[str, Any]) -> Tuple[str, str]:
    """외국 문자 구간 하나를 ('context' | 'abrupt', 근거) 로 읽는다."""
    names = set(span["scripts"])
    if span.get("same_text_in_question") or any(script_name(ch) in names for ch in ctx.question):
        return "context", "질문에 같은 종류의 문자가 있음"
    topic = _SCRIPT_TOPIC.search(ctx.question)
    if topic:
        return "context", f"질문이 다루는 주제 '{topic.group(0)}'"
    region = span.get("structure") or {}
    if region.get("in_quotes") or region.get("in_parentheses") or region.get("in_blockquote"):
        return "context", "따옴표·괄호·인용 안의 표기"
    if _GLOSS_AFTER.match(ctx.answer, span["end"]):
        return "context", "바로 뒤 괄호에 한글 병기"
    unit, text = _connected_context(ctx, span["start"])
    cue = _SCRIPT_EXPLAINED.search(text)
    if cue:
        return "context", f"{unit}에 설명 표현 '{cue.group(0)}'"
    return "abrupt", f"{unit}에 설명·병기 없이 나옴"


def _foreign_script_check(ctx: Ctx):
    spans = _script_spans(ctx)["script_spans"]
    title = "외국 문자 (설명 없는 출현)"
    if ctx.conds.has("charset"):
        return None  # 허용 문자 조건이 문자 종류를 정한다 — 2-1.charset 이 판정하므로 행 없음
    readings, abrupt = [], []
    for span in spans:
        kind, basis = _script_reading(ctx, span)
        readings.append({"span_id": span["span_id"], "span_text": span["span_text"], "scripts": span["scripts"],
                         "reading": kind, "basis": basis})
        if kind == "abrupt":
            abrupt.append(span)
    ok = not abrupt
    if not spans:
        reason = "외국 문자 없음"
    elif ok:
        reason = f"외국 문자 {len(spans)}곳 모두 질문 주제·주변 설명·병기 있음"
    else:
        reason = (f"설명·병기 없이 나온 외국 문자 {len(abrupt)}곳: "
                  + ", ".join(f"'{s['span_text']}'" for s in abrupt[:5]))
    return result("1-1.foreign_script", SUB11, title, "operating_policy", Status.EXECUTED, verdict_of(ok),
                  required="질문이 다루거나 주변에 설명·예시·병기가 있는 외국 문자만 허용",
                  measured={"spans": len(spans), "abrupt": len(abrupt), "with_context": len(spans) - len(abrupt),
                            "scripts": sorted({name for s in abrupt for name in s["scripts"]})},
                  comparison=cmp_text(len(abrupt), "==", 0), reason=reason,
                  evidence=[Evidence("answer", s["start"], s["end"], s["span_text"], "/".join(s["scripts"]))
                            for s in abrupt[:20]],
                  details={"readings": readings[:30]})


def foreign_script_observation(ctx: Ctx) -> Dict[str, Any]:
    obs = dict(_script_spans(ctx))
    obs["subcategory"] = SUB11
    obs["note"] = ("외국 문자 위치 기록. 판정은 1-1.foreign_script 결과에 있음. 한자·가나 등 문자 종류를 "
                   "특정 언어로 단정하지 않음. 명시된 허용 문자 조건은 2-1 결과를 따름")
    return obs


# =====================================================================================
# 1-2 문서·코드 유효성
# =====================================================================================

_ERROR_CONTEXT = re.compile(r"잘못된|틀린|오류\s*예|에러\s*예|오류가\s*있는|에러가\s*나는|수정\s*전|고치기\s*전|문제\s*(?:줄|코드|부분)|"
                            r"버그가\s*있는|문제가\s*있는|invalid|wrong|bad\s+example|before", re.IGNORECASE)
_LOG_LABELS = {"text", "txt", "console", "output", "log", "shell-session", "plaintext", "pycon"}


def _context_before(ctx: Ctx, block: Dict[str, Any]) -> str:
    lines = [l for l in ctx.aa.lines if l.end < block["start"] and l.text.strip() and not l.in_code]
    return " ".join(l.text for l in lines[-2:])


def _exception_for(ctx: Ctx, block: Optional[Dict[str, Any]]) -> Optional[str]:
    if ctx.conds.has("error_example_requested"):
        return "질문이 오류 예시를 요청함 — 의도된 오류 예시는 구문 판정 대상이 아님"
    if block is not None:
        if _ERROR_CONTEXT.search(block.get("info", "")) or _ERROR_CONTEXT.search(_context_before(ctx, block)):
            return "블록 앞 문맥이 오류 예시·수정 전 코드임을 밝힘 — 정상 결과물이 아님"
    return None


def _json_region(answer: str) -> Optional[Tuple[int, int]]:
    """코드블록 없이 JSON 을 요구받은 답변에서 JSON 으로 보이는 줄 범위 (첫 {/[ 줄 ~ 마지막 }/] 줄)."""
    lines = answer.split("\n")
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    first = next((i for i, l in enumerate(lines) if l.lstrip().startswith(("{", "["))), None)
    last = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].rstrip().endswith(("}", "]"))), None)
    if first is None or last is None or last < first:
        return None
    return offsets[first], offsets[last] + len(lines[last])


def json_targets(ctx: Ctx) -> List[Dict[str, Any]]:
    """정상 결과물로 볼 JSON 대상. 한 번 계산해 1-2·2-2·3-3 이 공유한다."""
    if "json_targets" in ctx.shared:
        return ctx.shared["json_targets"]
    targets: List[Dict[str, Any]] = []
    blocks = ctx.aa.fenced_blocks
    for i, b in enumerate(blocks):
        if b["language_label"] in ("json", "jsonc", "json5"):
            targets.append({"id": f"block{i + 1}", "kind": "code_block", "block": b, "text": b["content"],
                            "start": b["content_start"], "label": b["language_label"]})
    if ctx.conds.has("json_required") and not targets:
        unlabeled = [(i, b) for i, b in enumerate(blocks) if not b["language_label"]]
        if len(unlabeled) == 1 and len(blocks) == 1:
            i, b = unlabeled[0]
            targets.append({"id": f"block{i + 1}", "kind": "code_block", "block": b, "text": b["content"],
                            "start": b["content_start"], "label": ""})
        elif not blocks:
            region = _json_region(ctx.answer)
            if region:
                targets.append({"id": "answer", "kind": "answer_region", "block": None,
                                "text": ctx.answer[region[0]:region[1]], "start": region[0], "label": ""})
    ctx.shared["json_targets"] = targets
    return targets


def _parse_json_strict(text: str) -> Tuple[bool, Any, Optional[int], str, List[str]]:
    dup: List[str] = []

    def hook(pairs):
        names = [k for k, _ in pairs]
        dup.extend(sorted({k for k in names if names.count(k) > 1}))
        return dict(pairs)

    try:
        value = json.loads(text, object_pairs_hook=hook, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        return False, None, exc.pos, exc.msg, dup
    except ValueError as exc:
        return False, None, None, str(exc), dup
    return True, value, None, "", sorted(set(dup))


def _code_checks(ctx: Ctx) -> List:
    out = []
    conds, blocks = ctx.conds, ctx.aa.fenced_blocks
    labels = [b["language_label"] or "(없음)" for b in blocks]
    measured_blocks = {"code_blocks": len(blocks), "language_labels": labels,
                       "code_fence_delimiters": len(ctx.aa.code_fence_delimiters)}
    block_ev = [Evidence("answer", b["start"], b["end"], b["opening"].strip(), f"코드블록 {i + 1}")
                for i, b in enumerate(blocks)]

    forbid = conds.first("code_block_forbidden")
    if forbid:
        ok = not blocks
        out.append(result("1-2.code_block:forbidden", SUB12, "코드블록 사용 금지", "question", Status.EXECUTED,
                          verdict_of(ok), required="코드블록 0개", measured=measured_blocks,
                          comparison=cmp_text(len(blocks), "==", 0),
                          reason="코드블록 없음" if ok else f"금지된 코드블록 {len(blocks)}개 사용",
                          evidence=[q_evidence(ctx, forbid)] + block_ev))
    count = conds.first("code_block_count")
    required = conds.first("code_block_required")
    if count is not None:
        if count.status != "ok":
            skip_req(ctx, count, "1-2.code_block:count", SUB12)
        else:
            op, n = count.value["op"], count.value["n"]
            ok = compare(len(blocks), op, n)
            out.append(result("1-2.code_block:count", SUB12, "코드블록 개수", "question", Status.EXECUTED,
                              verdict_of(ok), required={"op": op, "code_blocks": n}, measured=measured_blocks,
                              comparison=cmp_text(len(blocks), op, n),
                              reason=(f"코드블록 {len(blocks)}개가 요구 조건({op} {n})을 충족" if ok else
                                      f"요구 코드블록 {op} {n}, 실제 {len(blocks)}개"),
                              evidence=[q_evidence(ctx, count)] + block_ev))
    elif required is not None and required.status != "ok":
        skip_req(ctx, required, "1-2.code_block:required", SUB12)
    elif required is not None:
        ok = bool(blocks)
        out.append(result("1-2.code_block:required", SUB12, "코드블록 존재", "question", Status.EXECUTED,
                          verdict_of(ok), required="코드블록 1개 이상", measured=measured_blocks,
                          comparison=cmp_text(len(blocks), ">=", 1),
                          reason="요구한 코드블록 있음" if ok else "코드블록을 요구했으나 코드블록이 없음",
                          evidence=[q_evidence(ctx, required)] + block_ev))

    # 코드블록 존재·개수 조건이 FAIL 이면 그에 딸린 언어 표지·구획 표시 행은 만들지 않는다 (같은 결함을 두 번 세지 않음).
    presence_failed = any(r.check_id.startswith("1-2.code_block:") and r.verdict == FAIL for r in out)
    label_req = conds.first("code_language_label")
    if label_req is not None and not forbid:
        lang = label_req.value["language"]
        aliases = LANG_ALIASES.get(lang, {lang})
        if presence_failed:
            skip_condition(ctx, "1-2.code_language_label", SUB12, "코드블록 존재·개수 조건이 FAIL 이라 언어 표지 행을 만들지 않음")
        elif not blocks:
            out.append(result("1-2.code_language_label", SUB12, "코드블록 언어 표지", "question", Status.EXECUTED, FAIL,
                              required={"language_label": sorted(aliases)}, measured=measured_blocks,
                              comparison=cmp_text(len(blocks), ">=", 1),
                              reason="언어 표지를 요구했으나 코드블록이 없음",
                              evidence=[q_evidence(ctx, label_req)]))
        else:
            # 언어 표지가 비어 있어도 코드가 코드블록으로 구분돼 있으면 통과 — 다른 언어를 적은 블록만 FAIL (사용자 결정 2026-10-21:
            # "언어표시는 안되어도 된다, 코드블록이 줄글과 구분되어 수집되면 OK". ChatGPT 앱 복사가 ```json 의 json 을 지우는 일도 이 경우)
            bad = [i + 1 for i, b in enumerate(blocks) if b["language_label"] and b["language_label"] not in aliases]
            unlabeled = [i + 1 for i, b in enumerate(blocks) if not b["language_label"]]
            out.append(result("1-2.code_language_label", SUB12, "코드블록 언어 표지", "question", Status.EXECUTED,
                              verdict_of(not bad), required={"language_label": sorted(aliases), "empty_label_ok": True},
                              measured={"language_labels": labels},
                              comparison=f"코드블록 info ∈ {sorted(aliases)} 또는 빈 표지",
                              reason=(f"코드블록 {bad}의 언어 표지가 요구({lang})와 다름" if bad else
                                      (f"코드블록 {unlabeled}은 언어 표지가 없지만 코드블록으로 구분돼 있어 통과" if unlabeled else
                                       "모든 코드블록의 언어 표지가 요구와 일치 (표지 확인이며 실제 언어 인증 아님)")),
                              evidence=[q_evidence(ctx, label_req)] + block_ev))

    # MD 165행: 코드 구획 표시가 2개 미만 — 코드블록을 요구한 질문에만 적용 (블록 수가 아닌 표시 수)
    if ((count is not None and count.status == "ok" or required is not None and required.status == "ok")
            and not forbid and not presence_failed):
        marks = len(ctx.aa.code_fence_delimiters)
        ok = not marks < 2
        out.append(result("1-2.code_fence_min", SUB12, "형식 위반 — 코드블록 (코드 구획 표시 수)", "md_policy",
                          Status.EXECUTED, verdict_of(ok), required="코드 구획 표시 2개 이상",
                          measured={"code_fence_delimiters": marks, "code_blocks": len(blocks)},
                          comparison=f"{marks} < 2 → {'참' if not ok else '거짓'}",
                          policy_version=MD_POLICY_VERSION,
                          reason=(f"코드 구획 표시 {marks}개 (블록 {len(blocks)}개)" if ok else
                                  f"코드 구획 표시가 {marks}개로 2개 미만"),
                          evidence=[Evidence("answer", d["start"], d["start"] + len(d["line"]), d["line"], d["role"])
                                    for d in ctx.aa.code_fence_delimiters]))

    # MD 170행: 펜스 불균형 — 코드 표시 홀수 OR 수식 표시 홀수 (합산하지 않음)
    code_marks, math_marks = len(ctx.aa.code_fence_delimiters), len(ctx.aa.math_delimiters)
    if code_marks or math_marks:
        odd_code, odd_math = code_marks % 2 == 1, math_marks % 2 == 1
        ok = not (odd_code or odd_math)
        out.append(result("1-2.fence_imbalance", SUB12, "펜스 불균형", "md_policy", Status.EXECUTED,
                          verdict_of(ok), required="코드 구획 표시 짝수 AND 수식 구획 표시 짝수",
                          measured={"code_fence_delimiters": code_marks, "math_delimiters": math_marks},
                          comparison=f"({code_marks} % 2 == 1) OR ({math_marks} % 2 == 1) → {'참' if not ok else '거짓'}",
                          policy_version=MD_POLICY_VERSION,
                          reason=("코드·수식 구획 표시 수가 각각 짝수" if ok else
                                  ("코드 구획 표시 수가 홀수" if odd_code else "") +
                                  (" · " if odd_code and odd_math else "") +
                                  ("수식 구획 표시 수가 홀수" if odd_math else "")),
                          evidence=[Evidence("answer", d["start"], d["start"] + len(d["line"]), d["line"], "코드 " + d["role"])
                                    for d in ctx.aa.code_fence_delimiters] +
                          [Evidence("answer", p, p + 2, excerpt(ctx.answer, p, p + 2, 10), "수식 $$")
                           for p in ctx.aa.math_delimiters]))
    return out


def _json_syntax(ctx: Ctx) -> List:
    out = []
    parsed = ctx.shared.setdefault("json_parsed", {})
    for t in json_targets(ctx):
        exc = _exception_for(ctx, t["block"])
        title = "JSON 구문" + (f" ({t['id']})" if t["id"] != "answer" else "")
        ev = [Evidence("answer", t["start"], t["start"] + len(t["text"]), t["text"][:80], t["id"])]
        cid = f"1-2.json_syntax:{t['id']}"
        # 판정하지 않는 대상은 행을 만들지 않고 parsed 상태만 남긴다 (2-2·3-3 의 종속 판단용).
        if exc:
            parsed[t["id"]] = {"status": "exception"}
            skip_condition(ctx, cid, SUB12, exc)
            continue
        if t["label"] in ("jsonc", "json5"):
            parsed[t["id"]] = {"status": "unsupported"}
            skip_condition(ctx, cid, SUB12, f"{t['label']} 는 RFC 8259 JSON 이 아님 — 지원 범위 밖")
            continue
        if len(t["text"]) > LIMITS["json_chars"]:
            parsed[t["id"]] = {"status": "limit"}
            case_error(ctx, cid, SUB12, ERROR_LIMIT_EXCEEDED, f"JSON 대상이 {LIMITS['json_chars']}자 한도를 넘음",
                       chars=len(t["text"]))
            continue
        try:
            ok, value, pos, msg, dup = _parse_json_strict(t["text"])
        except RecursionError:
            parsed[t["id"]] = {"status": "limit"}
            case_error(ctx, cid, SUB12, ERROR_LIMIT_EXCEEDED, "JSON 중첩 깊이가 파서 한도를 넘음")
            continue
        partial = "..." in t["text"] or "…" in t["text"]
        if not ok and partial:
            parsed[t["id"]] = {"status": "unsupported"}
            skip_condition(ctx, cid, SUB12, "생략 표시(...)가 있는 부분 JSON 으로 보임 — 완성 결과물 구문 판정에서 제외")
            continue
        parsed[t["id"]] = {"status": "ok" if ok else "fail", "value": value}
        if ok:
            out.append(result(f"1-2.json_syntax:{t['id']}", SUB12, title, "format_spec", Status.EXECUTED, PASS,
                              required="RFC 8259 JSON (NaN·Infinity 불허)",
                              measured={"top_level_type": _json_type_name(value), "duplicate_keys": dup},
                              comparison="json.loads 성공",
                              reason="유효한 JSON" + (f" — 중복 키 {dup}는 구문 FAIL 이 아니라 데이터 모호성으로 따로 기록" if dup else ""),
                              details={"duplicate_keys_observation": dup}, evidence=ev))
        else:
            where = None if pos is None else t["start"] + pos
            out.append(result(f"1-2.json_syntax:{t['id']}", SUB12, title, "format_spec", Status.EXECUTED, FAIL,
                              required="RFC 8259 JSON (NaN·Infinity 불허)", measured={"error": msg, "position": pos},
                              comparison="json.loads 실패",
                              reason=f"JSON 구문 오류: {msg}" + (f" (대상 내 {pos}번째 문자)" if pos is not None else ""),
                              evidence=[Evidence("answer", where, None if where is None else where + 1,
                                                 excerpt(ctx.answer, where, where + 1) if where is not None else t["text"][:80],
                                                 t["id"])]))
    return out


_PY_LABELS = LANG_ALIASES["python"]
#: 완성 소스가 아닌 발췌(함수 본문 일부 등)에서만 나는 구문 오류
_FRAGMENT_ERRORS = {"'return' outside function", "'yield' outside function", "'await' outside function",
                    "'break' outside loop", "'continue' not properly in loop", "unexpected indent"}
_PARTIAL_PY = re.compile(r"^\s*(?:\.\.\.|…)\s*$|#\s*\.\.\.|생략|\(중략\)", re.MULTILINE)


def _python_syntax(ctx: Ctx) -> List:
    out = []
    py_requested = ctx.conds.has("python_code_requested")
    for i, b in enumerate(ctx.aa.fenced_blocks):
        label = b["language_label"]
        if label in _LOG_LABELS or label not in _PY_LABELS and not (py_requested and not label):
            continue
        tid = f"block{i + 1}"
        title = f"Python 구문 ({tid})"
        src = b["content"]
        ev = [Evidence("answer", b["content_start"], b["content_start"] + len(src), src[:80], tid)]
        exc = _exception_for(ctx, b)
        if exc is None and re.search(r"^\s*>>>", src, re.MULTILINE):
            exc = "대화형 셸(>>>) 기록 — 완성된 Python 소스가 아님"
        if exc is None and "Traceback (most recent call last)" in src:
            exc = "실행 로그·트레이스백 — 소스 코드가 아님"
        cid = f"1-2.python_syntax:{tid}"
        if exc:
            skip_condition(ctx, cid, SUB12, exc)
            continue
        if len(src) > LIMITS["python_chars"]:
            case_error(ctx, cid, SUB12, ERROR_LIMIT_EXCEEDED, f"코드가 {LIMITS['python_chars']}자 한도를 넘음", chars=len(src))
            continue
        try:
            # 목록 안·들여쓴 펜스는 Markdown 들여쓰기를 뺀 내용으로 읽는다 (CommonMark). 줄 번호는 같다.
            tree = ast.parse(b.get("dedented", src), filename=f"<answer-{tid}>", mode="exec")
            compile(tree, f"<answer-{tid}>", "exec", dont_inherit=True)   # 실행·import 하지 않음
            error = None
        except SyntaxError as exc_:
            error = exc_
        except (RecursionError, MemoryError):
            case_error(ctx, cid, SUB12, ERROR_LIMIT_EXCEEDED, "구문 트리 깊이·메모리 한도를 넘음")
            continue
        except ValueError as exc_:
            error = SyntaxError(str(exc_))
        if error is None:
            out.append(result(f"1-2.python_syntax:{tid}", SUB12, title, "format_spec", Status.EXECUTED, PASS,
                              required="Python 구문 분석·컴파일 성공", measured={"language_label": label or "(없음)"},
                              comparison="ast.parse + compile 성공",
                              reason="Python 구문 유효 (실행·기능 검증 아님)", evidence=ev))
            continue
        fragment = getattr(error, "msg", "") in _FRAGMENT_ERRORS
        if _PARTIAL_PY.search(src) or fragment:
            skip_condition(ctx, cid, SUB12, ("함수·반복문 본문 일부를 발췌한 코드로 보임" if fragment else "생략 표시가 있는 부분 코드로 보임")
                           + " — 완성 소스 구문 판정에서 제외")
            continue
        line = getattr(error, "lineno", None)
        out.append(result(f"1-2.python_syntax:{tid}", SUB12, title, "format_spec", Status.EXECUTED, FAIL,
                          required="Python 구문 분석·컴파일 성공",
                          measured={"error": getattr(error, "msg", str(error)), "line": line,
                                    "offset": getattr(error, "offset", None)},
                          comparison="compile → SyntaxError",
                          reason=f"Python SyntaxError: {getattr(error, 'msg', error)}" + (f" ({line}행)" if line else ""),
                          evidence=ev))
    return out


_TAG = re.compile(r"<(?P<close>/?)(?P<name>[A-Za-z][A-Za-z0-9:_-]{0,40})(?:\s[^<>]*)?(?P<self>/?)>")
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def _orphan_tags(ctx: Ctx) -> List:
    if ctx.da is not None:
        scan, offsets = ctx.da.projection
        target, scope = "displayed_text", "displayed_text (수집 표시본, 코드 제외·강조 제거)"
    else:
        scan, offsets = ctx.aa.projection
        target, scope = "answer", "answer_projection (로컬 텍스트 투영, 실제 표시본 아님)"
    tags = list(_TAG.finditer(scan))
    closings = [m for m in tags if m.group("close")]
    if not closings:
        return []
    stack: List[str] = []
    orphans = []
    for m in tags:
        name = m.group("name").lower()
        if m.group("close"):
            if name in stack:
                while stack and stack.pop() != name:
                    pass
            else:
                orphans.append(m)
        elif not m.group("self") and name not in _VOID:
            stack.append(name)

    def ev(m):
        s = offsets[m.start()] if m.start() < len(offsets) else None
        e = offsets[m.end() - 1] + 1 if m.end() - 1 < len(offsets) else None
        return Evidence(target, s, e, m.group(0), "짝이 없는 닫는 태그")

    ok = not orphans
    return [result("1-2.orphan_closing_tag", SUB12, "짝이 없는 닫는 태그", "md_policy", Status.EXECUTED,
                   verdict_of(ok), required="대응하는 여는 태그 없는 닫는 태그 0개",
                   measured={"closing_tags": len(closings), "orphan_closing_tags": [m.group(0) for m in orphans]},
                   comparison=cmp_text(len(orphans), "==", 0), scope=scope, policy_version=MD_POLICY_VERSION,
                   reason=("모든 닫는 태그에 대응하는 여는 태그가 있음" if ok else
                           f"여는 태그 없이 닫는 태그만 남음: {', '.join(m.group(0) for m in orphans[:5])}"),
                   evidence=[ev(m) for m in orphans[:20]])]


def check_1_2(ctx: Ctx) -> List:
    return _code_checks(ctx) + _json_syntax(ctx) + _python_syntax(ctx) + _orphan_tags(ctx)


# =====================================================================================
# 1-3 참조 연결 유효성
# =====================================================================================

_EMPTY_FOOTNOTE_DEF = re.compile(r"^[ ]{0,3}\[\^(?P<label>[^\]\s]{1,64})\][ \t]*:[ \t]*$")
#: 제목 줄로 쓴 정의 (예: ``### [각주1] 배포 파이프라인``) — 제목 아래 설명이 정의 본문이다.
_HEADING_FOOTNOTE_DEF = re.compile(r"^#{1,6}\s*\[(?P<label>\^?[^\]\s]{1,64}|각주\s*\d{1,3})\][ \t]*\S")


def check_1_3(ctx: Ctx) -> List:
    out = []
    # References and their targets must share the same code-excluded scope.
    # Masking retains source offsets for evidence while keeping literal examples out.
    answer = mask_ranges(ctx.answer, ctx.aa.code_exclusion_ranges)
    reference_analysis = TextAnalysis(answer)
    markers, definitions, duplicated = _parse_footnotes(answer)
    if markers:
        empty = {re.sub(r"\s+", "", m.group("label")) for l in reference_analysis.lines if not l.in_code
                 for m in [_EMPTY_FOOTNOTE_DEF.match(l.text)] if m}
        used = sorted({label for label, _ in markers})
        for l in reference_analysis.lines:
            hm = None if l.in_code else _HEADING_FOOTNOTE_DEF.match(l.text)
            if hm:
                definitions.setdefault(re.sub(r"\s+", "", hm.group("label")).lstrip("^"), l.number)
        missing = [l for l in used if l not in definitions and l not in empty]
        marker_spans = {s for _, s in markers}
        odd_form = [l for l in missing if any(
            m.start() not in marker_spans
            for m in re.finditer(r"\[\^?" + re.escape(l).replace("각주", r"각주\s*") + r"\]", answer))]
        empties = [l for l in used if l in empty and l not in definitions]
        dups = [l for l in used if l in duplicated]
        if odd_form and len(odd_form) == len(missing) and not empties and not dups:
            skip_condition(ctx, "1-3.footnote_link", SUB13,
                           f"각주 이름 {odd_form}이 지원하지 않는 정의 형태로 다른 곳에 나옴 — 정의 누락으로 단정하지 않음")
        else:
            ok = not (missing or empties or dups)
            problems = ([f"정의 누락 {missing}"] if missing else []) + ([f"빈 정의 {empties}"] if empties else []) + \
                       ([f"정의 중복 {dups}"] if dups else [])
            out.append(result("1-3.footnote_link", SUB13, "각주 ↔ 정의 연결", "answer_self", Status.EXECUTED,
                              verdict_of(ok), required="사용한 각주마다 비어 있지 않은 정의 1개",
                              measured={"used": used, "defined": sorted(definitions), "empty_definitions": sorted(empty),
                                        "duplicated_definitions": duplicated,
                                        "unused_definitions": sorted(set(definitions) - set(used))},
                              comparison="used ⊆ defined(유일·비어 있지 않음)",
                              reason="사용한 각주가 모두 하나의 정의에 연결됨" if ok else "; ".join(problems),
                              evidence=[Evidence("answer", s, s + len(l) + 3, excerpt(ctx.answer, s, s + len(l) + 3), l)
                                        for l, s in markers if l in missing + empties + dups][:20]))
    citations, sources, dup_sources = _parse_references(answer)
    if citations:
        used = sorted({n for n, _ in citations})
        if not sources:
            skip_condition(ctx, "1-3.citation_link", SUB13,
                           "답변에 지원하는 출처 목록 문법([n] 설명 줄)이 없어 연결 대상을 확인할 수 없음 — 누락으로 단정하지 않음")
        else:
            missing = [n for n in used if n not in sources]
            dups = [n for n in used if n in dup_sources]
            ok = not (missing or dups)
            out.append(result("1-3.citation_link", SUB13, "인용 번호 ↔ 출처 목록", "answer_self", Status.EXECUTED,
                              verdict_of(ok), required="사용한 인용 번호마다 출처 1개",
                              measured={"used": used, "sources": sorted(sources), "duplicated_sources": dup_sources},
                              comparison="used ⊆ sources(유일)",
                              reason="인용 번호가 모두 출처에 연결됨" if ok else
                              "; ".join(([f"출처 누락 {missing}"] if missing else []) + ([f"출처 번호 중복 {dups}"] if dups else [])),
                              evidence=[Evidence("answer", s, s + len(str(n)) + 2, excerpt(ctx.answer, s, s + 3), f"[{n}]")
                                        for n, s in citations if n in missing + dups][:20]))
    links = internal_links(answer)
    if links:
        targets, dup_targets = link_targets(answer)
        anchors = sorted({a for a, _, _ in links})
        norm = lambda s: re.sub(r"[\W_]+", "", s.lower())
        heading_norms = {norm(h.title) for h in reference_analysis.headings}
        if not targets:
            skip_condition(ctx, "1-3.internal_link", SUB13, "제목·앵커가 없어 링크 대상 목록을 만들 수 없음")
        else:
            missing = [a for a in anchors if a not in targets]
            slug_mismatch = [a for a in missing if norm(a) in heading_norms]
            real_missing = [a for a in missing if a not in slug_mismatch]
            ambiguous = [a for a in anchors if targets.get(a, 0) > 1]
            if slug_mismatch and not real_missing and not ambiguous:
                skip_condition(ctx, "1-3.internal_link", SUB13,
                               f"자동 제목 ID 규칙 차이로 보이는 링크 {slug_mismatch} — 누락으로 단정하지 않음")
            else:
                ok = not (real_missing or ambiguous)
                out.append(result("1-3.internal_link", SUB13, "내부 링크 ↔ 대상", "answer_self", Status.EXECUTED,
                                  verdict_of(ok), required="모든 내부 링크가 유일한 대상에 연결",
                                  measured={"links": anchors, "targets": sorted(targets),
                                            "missing": real_missing, "ambiguous": ambiguous,
                                            "slug_rule_uncertain": slug_mismatch},
                                  comparison="links ⊆ targets(유일)",
                                  reason="내부 링크가 모두 대상에 연결됨" if ok else
                                  "; ".join(([f"대상 없음 {real_missing}"] if real_missing else []) +
                                            ([f"대상 중복 {ambiguous}"] if ambiguous else [])),
                                  evidence=[Evidence("answer", s, e, ctx.answer[s:e]) for a, s, e in links
                                            if a in real_missing + ambiguous][:20]))
    return out
