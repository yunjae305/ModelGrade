"""J1~J6 적용 판정 — 질문·답변·입력 정보에서 코드 규칙으로 적용 여부와 근거를 뽑는다 (LLM 없음).

CODE 의 질문 조건 추출(``extract.py``)과 같은 원칙을 쓴다.
- 질문 안의 인용 자료(펜스 코드, ``>`` 인용 줄, 표, 큰따옴표 문장)는 지시가 아니라 데이터이므로 가린다.
- J2~J4 는 요청 문장(물음표나 '~해줘·~마·~해' 등으로 끝나는 문장)에서만 찾는다. 상황 설명 문장은 보지 않는다.
- J1 은 모든 질문에 적용한다. J2~J4 는 근거 문구가 있을 때만 적용하고, 없으면 '해당 없음'과 이유를 남긴다.
- J5 DATA 신뢰도는 **답변**에 검증할 데이터(수치·단위, 연도·날짜, 통계·출처 표현)가 있을 때, 또는 참조 데이터(reference)가
  있을 때 적용한다. 코드 펜스·인라인 코드 안의 숫자와 목록 번호는 데이터로 보지 않는다. 명칭·고유명사는 규칙으로 잡지 못한다.
- J6 LONG CONTEXT 는 대화 입력(conversation)의 2번째 턴부터, 앞 턴 대화가 있을 때 적용한다. 첫 턴은 '해당 없음'이고,
  단일 질문·답변 입력에는 적지 않는다.
- 같은 입력은 언제나 같은 결과를 낸다. 단어 규칙이라 놓치거나 과하게 잡을 수 있으므로 근거 문구와 출처(source)를 함께 남긴다.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .analysis import TextAnalysis, mask_ranges
from .extract import _instruction_text, _quote_spans
from .jev_criteria import CRITERIA

__all__ = ["JEV_APPLICABILITY_VERSION", "APPLIES_WHEN", "DATA_RULES", "jev_applicability"]

JEV_APPLICABILITY_VERSION = "jev-applicability-v2.2"

_AUDIENCE = r"(?:초등학생|중학생|고등학생|대학생|어린이|고객|팀장|상사|동료|사용자|친구|동기|초보자|부모님)"

#: (규칙 이름, 패턴, 이 질문에서 볼 것) — 질문의 요청 문장에서 찾는다
RULES: Dict[str, Tuple[Tuple[str, str, str], ...]] = {
    "J2": (
        ("대상", rf"{_AUDIENCE}(?:에게|한테|도|이|가|을|를|용)|면접", "질문이 정한 대상에게 맞는 내용인지"),
        ("말투·수준", r"쉽게\s*(?:설명|알려|풀어)|이해할\s*수\s*있게|자연스럽|정중|격식|말투|어조|친근|유쾌|기술\s*용어",
         "질문이 정한 말투·수준에 맞는지"),
        ("제약", r"(?:하|쓰|넣|약속하|사용하)지\s*(?:마|말)|않고|않으면서|않도록|않게|말고|만\s*사용|만\s*넣|이\s*정보만|"
                 r"이\s*경험만|주어지지\s*않은|최소화", "질문이 정한 제약을 지켰는지"),
        ("설명 방식", r"(?<!\S)(?!기준)\S+(?:으로|로)\s*설명해\s*줘|비유|예시를\s*들어|연결해서\s*설명|상황을\s*기준|상황에\s*비유",
         "질문이 정한 방식(관점·비유·예시)으로 설명했는지"),
        ("글의 목적", r"(?:안내문|메시지|답글|카드|광고\s*문구|문구|회고|일기|동화|산문|줄거리|이야기|답변|글)(?:의|을|를|으로|로|에)?\s*"
                     r"(?:\S+\s*){0,2}(?:작성|써|만들)", "글의 목적과 받는 사람에 맞는 내용인지"),
        ("추천·제안", r"추천|제안", "추천·제안이 질문의 상황에 맞는지"),
    ),
    "J3": (
        ("이유·원리", r"이유|왜|원리|까닭|원인(?:은|을)|어떻게\s*\S+\s*(?:수\s*있|유지)", "이유·원리 설명의 인과가 맞는지"),
        ("추론·판단", r"추론|맞을까|단정|일반화|볼\s*수\s*있는지|믿어도\s*되는지|설명할\s*수\s*있는지|문제가\s*생길|조건을\s*만족",
         "판단과 그 근거가 논리적으로 맞는지"),
        ("풀이", r"계산|구해|풀어|몇\s*(?:번|초|m|L|가지|개|시간|명|권)|얼마(?:야|인지)|평균\s*(?:속력|점수)",
         "풀이 과정과 결론이 논리적으로 이어지는지"),
    ),
    "J4": (
        ("비교", r"비교|(?:어떻게|무엇이|어떤\s*점(?:이|에서))\s*달라|다른\s*(?:점|변화)|차이(?:는|가)?\s*(?:뭐|무엇|있어)|"
                 r"어떤\s*\S*\s*차이|구분해|구분하고|구분할", "비교 대상과 비교 기준을 모두 다뤘는지"),
        ("추가 요구", r"\S+도\s*(?:설명|알려|짚어|구해|만들어|포함|구분|함께)|각각|장단점|포함해|포함한|기준으로",
         "질문이 함께 요구한 내용을 빠짐없이 다뤘는지"),
        ("방법·절차", r"방법|(?:어떤|작업|이동)\s*순서|순서를\s*정해|단계|절차|과정을\s*보여|과정도",
         "방법·절차의 핵심 단계가 빠지지 않았는지"),
        ("요약", r"요약", "원문의 핵심 정보가 빠지지 않았는지"),
    ),
}

#: 단위·기호 — 긴 표기를 먼저 둬서 'mm' 이 'm' 으로 잡히지 않게 한다. 개·건·회·번 같은 순서·개수 낱말은 데이터로 보지 않는다.
#: '도' 는 조사('403도 서버')와 헷갈려 소수점이 있는 각도(23.5도)만 잡는다. '3분의 1' 같은 분수는 단위로 보지 않는다.
_UNIT = (r"%|퍼센트|달러|유로|원|엔|km|cm|mm|m|kg|mg|g|mL|ml|L|℃|°C|배|명|년|월|일|주|시간|분|초|세|kcal|GB|MB|TB|Hz|W|V|"
         r"㎡|평|인치|px|점|위")
_NUMBER = r"\d[\d,]*(?:\.\d+)?"

#: (규칙 이름, 패턴, 볼 것) — **답변**(코드 제외)에서 찾는다
DATA_RULES: Tuple[Tuple[str, str, str], ...] = (
    ("수치·단위", rf"{_NUMBER}\s*(?:만|억|조)?\s*(?:{_UNIT})(?![A-Za-z]|의)|\d+\.\d+\s*도(?![A-Za-z가-힣])",
     "답변이 제시한 수치가 실제·주어진 데이터와 맞는지"),
    ("연도·날짜", r"(?:1[0-9]|20)\d{2}년|\d{1,2}월\s*\d{1,2}일|\d{4}[-./]\d{1,2}[-./]\d{1,2}", "답변이 제시한 연도·날짜가 실제와 맞는지"),
    ("통계·출처", r"에\s*따르면|통계(?:청|\s*자료|\s*수치|치|상)|(?:조사|연구)\s*결과[^\n\d]{0,12}\d|공식\s*(?:발표|자료|통계)|평균\s*\d|약\s*\d",
     "통계·출처를 든 주장이 실제 자료와 맞는지"),
)

#: 화면·보고서에 보여 줄 적용 조건
APPLIES_WHEN = {
    "J1": "모든 질문",
    "J2": "요청 문장에 대상·말투·수준·제약·설명 방식·글의 목적·추천 표현이 있을 때",
    "J3": "요청 문장에 이유·원리·추론·판단·풀이 요구가 있을 때",
    "J4": "요청 문장에 비교·추가 요구·방법·절차·요약 요구가 있을 때",
    "J5": "답변에 수치·날짜·통계 데이터가 있거나 참조 데이터가 있을 때 (코드 안 숫자 제외)",
    "J6": "대화 입력의 2번째 턴부터 — 앞 턴 대화를 함께 보냄",
}
_NOT_APPLICABLE = {
    "J2": "요청 문장에 대상·말투·제약·설명 방식·글의 목적을 정한 표현이 없음",
    "J3": "요청 문장에 이유·원리·추론·풀이를 요구하는 표현이 없음",
    "J4": "요청 문장에 비교·추가 요구·방법·요약을 요구하는 표현이 없음",
    "J5": "답변에 수치·날짜·통계 데이터가 없음 (참조 데이터도 없음)",
}
_REQUEST_END = re.compile(r"(?:\?|줘|주세요|마|말아|해|하자|봐)[.?!]*\s*$")
_LIST_NUMBER = re.compile(r"^[ \t]*\d+[.)][ \t]", re.M)


def _request_sentences(question: str) -> List[str]:
    qa = TextAnalysis(question)
    text = _instruction_text(qa)
    text = mask_ranges(text, _quote_spans(text))
    # 숫자 사이 소수점은 문장 경계가 아니다. 그 앞의 대상·제약도 같은 요청에 남긴다.
    sentences = (m.group(0).strip() for m in re.finditer(r"(?:[^.?!\n]|(?<=\d)\.(?=\d))+[.?!]*", text))
    return [s for s in sentences if s and _REQUEST_END.search(s)]


def _clip(sentence: str, start: int, end: int, margin: int = 22) -> str:
    """근거 문구 주변을 잘라 보여 준다. 낱말 중간에서 끊지 않도록 띄어쓰기 위치에 맞춘다."""
    left, right = max(0, start - margin), min(len(sentence), end + margin)
    if left:
        space = sentence.rfind(" ", 0, left)
        left = space + 1 if space >= 0 else 0
    if right < len(sentence):
        space = sentence.find(" ", right)
        right = space if space >= 0 else len(sentence)
    return ("…" if left else "") + sentence[left:right].strip() + ("…" if right < len(sentence) else "")


def _data_text(answer: str) -> str:
    """답변에서 코드(펜스·인라인)와 목록 번호를 가린 본문 (위치 보존)."""
    aa = TextAnalysis(answer)
    text = mask_ranges(answer, aa.code_exclusion_ranges)
    return mask_ranges(text, [(m.start(), m.end()) for m in _LIST_NUMBER.finditer(text)])


def _answer_data_evidence(answer: str, per_rule: int = 3) -> Optional[Dict[str, Any]]:
    """답변에서 찾은 데이터 근거. 규칙마다 서로 다른 값을 최대 ``per_rule`` 개 남기고, 발췌는 첫 값이 있는 줄이다."""
    text = _data_text(answer)
    rule_names, matches, looks, excerpt = [], [], [], None
    for name, pattern, look in DATA_RULES:
        found: List[str] = []
        for m in re.finditer(pattern, text):
            value = m.group(0).strip()
            if value not in found:
                found.append(value)
            if excerpt is None:
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.end())
                line_end = len(text) if line_end < 0 else line_end
                excerpt = _clip(answer[line_start:line_end], m.start() - line_start, m.end() - line_start)
            if len(found) >= per_rule:
                break
        if found:
            rule_names.append(name)
            matches.extend(found)
            looks.append(look)
    if not rule_names:
        return None
    return {"rules": rule_names, "matches": matches, "excerpt": excerpt, "look_for": " · ".join(looks), "source": "answer"}


def _reference_excerpt(reference: Any) -> str:
    text = reference if isinstance(reference, str) else json.dumps(reference, ensure_ascii=False)
    text = " ".join(text.split())
    return text[:60] + ("…" if len(text) > 60 else "")


def jev_applicability(question: str, answer: Optional[str] = None, *, reference: Any = None,
                      conversation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """적용할 J 기준과 근거. {"criteria": [...], "evidence": {J: {...}}, "not_applicable": {J: 이유}}

    - ``answer`` 가 있으면 J5 를 답변의 데이터로 판정한다. ``reference`` 가 있으면 J5 는 항상 적용한다.
    - ``conversation`` = {"id", "turn", "history_turns"} 이면 J6 를 2번째 턴부터(앞 턴 대화가 있을 때) 적용한다.
    evidence 의 ``source`` 는 근거를 찾은 곳(question · answer · reference · conversation)이다.
    """
    sentences = _request_sentences(question)
    last = sentences[-1] if sentences else question.strip()
    evidence: Dict[str, Dict[str, Any]] = {
        "J1": {"rules": ["요청"], "matches": [], "excerpt": _clip(last, 0, min(len(last), 60), 0),
               "look_for": "질문의 요청을 실제로 수행했는지", "source": "question"}}
    for cid, rules in RULES.items():
        rule_names, matches, looks, excerpt = [], [], [], None
        for name, pattern, look in rules:
            for sentence in sentences:
                m = re.search(pattern, sentence)
                if m:
                    rule_names.append(name)
                    matches.append(m.group(0).strip())
                    looks.append(look)
                    excerpt = excerpt or _clip(sentence, m.start(), m.end())
                    break
        if rule_names:
            evidence[cid] = {"rules": rule_names, "matches": matches, "excerpt": excerpt, "look_for": " · ".join(looks),
                             "source": "question"}

    data = _answer_data_evidence(answer) if answer else None
    has_reference = reference is not None and reference != "" and reference != {} and reference != []
    if has_reference:
        evidence["J5"] = {"rules": ["참조 데이터"] + (data["rules"] if data else []),
                          "matches": ["reference"] + (data["matches"] if data else []),
                          "excerpt": _reference_excerpt(reference),
                          "look_for": "답변의 데이터·사실이 참조 데이터와 일치하는지", "source": "reference"}
    elif data:
        evidence["J5"] = data

    not_applicable = {cid: reason for cid, reason in _NOT_APPLICABLE.items() if cid not in evidence}
    if conversation:
        turn, history_turns = int(conversation.get("turn") or 0), int(conversation.get("history_turns") or 0)
        issues = list(conversation.get("issues") or [])
        if turn <= 1:
            not_applicable["J6"] = "대화의 첫 턴 — 앞 대화 없음"
        elif issues:  # 앞 턴 누락·입력 오류·중복·길이 초과 — 불완전한 이력으로는 판정하지 않는다
            not_applicable["J6"] = "대화 이력이 불완전함 — " + ", ".join(issues)
        elif history_turns < 1:
            not_applicable["J6"] = "앞 턴 입력이 없거나 입력 오류여서 앞 대화를 만들 수 없음"
        else:
            evidence["J6"] = {"rules": ["대화"], "matches": [], "source": "conversation",
                              "excerpt": f"대화 {conversation.get('id')} · {turn}턴 (앞 턴 {history_turns}개)",
                              "look_for": "앞 대화의 지시·설정·문맥을 이 답변이 유지했는지"}
    criteria = [c.id for c in CRITERIA if c.id in evidence]
    return {"version": JEV_APPLICABILITY_VERSION, "criteria": criteria, "evidence": evidence, "not_applicable": not_applicable}
