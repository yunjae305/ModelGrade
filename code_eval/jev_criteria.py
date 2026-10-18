"""Jev 의미 평가 기준 J1~J8 — 정의는 이 파일 한 곳에서만 관리한다.

J1~J8는 CODE 영역(``model.SUBCATEGORIES``)과 중복 판정하지 않는다.
Jev 요청 문구, 보고서·화면 설명은 모두 이 정의에서 만든다. Jev 가 새 기준을 만들지 않도록
의미 평가는 이 여덟 기준만 묻는다. 적용 기준은 ``jev_applicability`` 가 코드 규칙으로 정한다.
J5 DATA 신뢰도는 참조 데이터(reference)가 있으면 그것을, 없으면 확립된 사실을 기준으로 삼는다.
J6 멀티턴 맥락 유지는 멀티턴 대화(10턴 이상 · 대화 전체 10K 토큰 이상, ``multi_turn`` v2)의 2번째 턴부터만 묻는다
(2026-10-13 이름 변경, 옛 이름 LONG CONTEXT).
J7 롱컨텍스트 활용은 입력(현재 질문 + 참조 자료)이 272K 토큰 이상일 때만 묻는다 (``long_context``).
J8 첨부파일 활용은 질문에 첨부파일(이미지·TXT·DOCX·PDF 등)이 있을 때만 묻는다 (``attachments``, 2026-10-13 추가).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from .model import SUBCATEGORIES

__all__ = ["JEV_CRITERIA_VERSION", "DECISIONS", "JevCriterion", "CRITERIA", "CODE_SCOPE_NOTE",
           "jev_questions", "describe"]

JEV_CRITERIA_VERSION = "jev-criteria-v6"   # v6: J1·J4 에 빠진 자료·모순 질문 예외(J1-3 과 같은 규칙, Jev 관찰 시험 2026-10-15) · v5: J8 첨부파일 활용 추가 · v4: J6 이름 변경(멀티턴 맥락 유지), J7 롱컨텍스트 활용 추가

#: Jev choice 질문의 선택지. AIONA 판정 표기(PASS/FAIL)와 같게 둔다.
DECISIONS = ("PASS", "FAIL")

#: CODE 가 이미 검사하는 영역. 모든 Jev 지시문에 붙여 중복 판정을 막는다.
CODE_SCOPE_NOTE = (
    "다음은 코드 검사가 따로 확인하므로 이 판단에 넣지 않는다: "
    + ", ".join(SUBCATEGORIES.values())
    + " (예: 요청한 개수와 실제 개수, 글자·문장 수, 표·목록·JSON·코드블록 형식과 문법, 계산값, 각주·링크 연결)."
)


@dataclass(frozen=True)
class JevCriterion:
    id: str            # J1 … J7 — Jev 질문 키이자 결과 키
    key: str
    name: str
    name_en: str
    question: str      # Jev 에 묻는 문장
    pass_rule: str
    fail_rule: str
    boundary: str      # CODE 와 나누는 경계
    example: str       # 보고서·화면용 FAIL 예

    def instructions(self) -> str:
        return f"{self.name}({self.name_en}): {self.question} {self.boundary} {CODE_SCOPE_NOTE}"


CRITERIA = (
    JevCriterion(
        "J1", "intent_fulfillment", "의도 충족성", "Intent Fulfillment",
        question="질문이 시킨 일(비교·추천·설명·작성 등)을 답변이 실제로 했는가?",
        pass_rule="요청한 작업의 결과물(비교 결과·추천 대상·해결 방법·요청한 글)이 답변에 있음. "
                  "질문에 꼭 필요한 자료가 빠졌거나 조건이 서로 모순되면, 그 사실을 밝히고 자료를 요청하거나 가능한 범위만 답한 것도 PASS",
        fail_rule="요청과 다른 일을 함 — 비교 요청에 각각 설명만, 추천 요청에 추천 없이 고르는 법만, 해결 방법 요청에 문제 설명만",
        boundary="‘했는가’만 본다. 내용이 대상·상황에 맞는지는 J2, 핵심이 빠졌는지는 J4 가 본다. 개수·분량·형식은 판단하지 않는다. "
                 "‘이 계약서를 요약해줘’처럼 대상 자료가 질문에 없으면 요약할 수 없으므로, 자료가 없다고 밝히고 요청한 답변은 할 일을 한 것이다(J1-3 과 같은 규칙).",
        example="요리법을 물었는데 재료 설명만 하고 만드는 법은 주지 않았는지"),
    JevCriterion(
        "J2", "content_appropriateness", "내용 적절성", "Content Appropriateness",
        question="답변의 내용이 질문이 정한 대상·상황·조건·목적에 맞는가?",
        pass_rule="설명 수준·말투·제약·목적이 질문이 정한 대상과 상황에 맞음",
        fail_rule="대상·상황과 어긋남 — 초등학생에게 전문 용어로 설명, 대학생용 추천에 수백만 원대 장비, ‘하지 마’라고 한 것을 함",
        boundary="‘누구에게·어떤 상황에 맞는가’를 본다. 일을 했는지는 J1, 빠진 내용은 J4 가 본다. 추천 질문만의 기준이 아니다.",
        example="대학생용 노트북 추천에 수백만 원대 작업용 장비만 제시했는지"),
    JevCriterion(
        "J3", "logical_validity", "논리 타당성", "Logical Validity",
        question="답변의 주장·이유·설명·결론 사이에 논리적 모순이나 부적절한 인과관계가 없는가?",
        pass_rule="주장·근거·결론이 서로 맞고 명백한 논리 비약이 없음",
        fail_rule="앞부분과 결론이 모순되거나, 제시한 근거로 결론을 낼 수 없거나, 설명 과정에 명백한 논리 비약이 있음",
        boundary="단순 수치 계산 오류는 코드 검사가 맡으므로 판단하지 않는다.",
        example="앞에서 A가 더 빠르다고 한 뒤 결론에서 B가 더 빠르다고 했는지"),
    JevCriterion(
        "J4", "sufficiency", "충분성", "Sufficiency",
        question="요청한 목적을 이루는 데 꼭 필요한 핵심 내용이 빠짐없이 들어 있는가?",
        pass_rule="목적 달성에 필요한 핵심 요소(양쪽 비교 항목·핵심 단계·판단 근거)가 모두 있음",
        fail_rule="일은 했지만 핵심이 빠짐 — 장단점 요청에 장점만, 해결 방법에 핵심 단계 누락, 선택 질문에 결론만",
        boundary="‘빠진 것이 있는가’를 본다. 일을 아예 안 한 것은 J1, 내용이 맞지 않는 것은 J2 가 본다. "
                 "'5개 요청에 4개 제공' 같은 개수 누락은 코드 검사가 맡으므로 개수만으로 FAIL 하지 않는다. "
                 "질문에 꼭 필요한 자료가 빠져 있으면, 자료가 없다는 사실과 무엇을 주면 되는지를 담은 답변은 그 상황에서 필요한 핵심을 갖춘 것이다(J1-3 과 같은 규칙).",
        example="장단점을 물었는데 장점만 설명했는지"),
    JevCriterion(
        "J5", "data_reliability", "DATA 신뢰도", "Data Reliability",
        question="답변의 수치·날짜·명칭·통계 등 데이터가 실제(참조 데이터가 있으면 참조 데이터)와 맞는가?",
        pass_rule="제시한 데이터가 참조 데이터 또는 확립된 사실·질문에 주어진 값과 일치하고, 근거 없는 수치를 만들어 넣지 않음",
        fail_rule="참조 데이터·검색한 신뢰할 수 있는 출처·질문의 값과 다른 사실을 제시함 — 근거로 틀렸다고 확인되는 경우만 FAIL. 확인할 수 없으면 미판정(UNVERIFIED)",
        boundary="계산·개수·원본 변환은 코드 검사가 맡으므로 값의 출처와의 일치만 본다. 출처의 신뢰성·정보의 기준 시점·적용 조건을 함께 확인한다.",
        example="인구·가격 같은 사실 수치를 실제와 다르게 적었는지"),
    JevCriterion(
        "J6", "multi_turn_context", "멀티턴 맥락 유지", "Multi-turn Context Retention",
        question="앞선 대화(history)에서 준 지시·설정을 이 답변이 계속 지키고, 앞 내용과 모순 없이 문맥을 유지하는가?",
        pass_rule="앞 대화의 지시·설정(역할·말투·금지 사항·기억할 정보)을 지키고, 앞서 정한 이름·조건·결정과 일관됨",
        fail_rule="앞 지시·설정을 잊거나 어김, 앞서 정한 내용과 모순, 앞 대화를 잊고 엉뚱한 맥락으로 답함",
        boundary="분량·형식 지시는 코드 검사 영역이고, 이 턴 질문 자체의 의도·논리는 J1~J4 가 본다.",
        example="1턴의 ‘반말로, 이름은 민수’ 지시가 15턴에서 존댓말·다른 이름으로 바뀌었는지"),
    # 근거: Lost in the Middle(TACL 2024 — 입력 중간의 정보에서 성능 하락), RULER(COLM 2024 — 검색·다단계 추적·집계),
    # NoLiMa(ICML 2025 — 질문과 표현이 다른 정보 찾기), LooGLE(ACL 2024 — 장거리 의존), BABILong(NeurIPS 2024 — 흩어진 사실 추론)
    JevCriterion(
        "J7", "long_context", "롱컨텍스트 활용", "Long-Context Utilization",
        question="긴 입력(문서·자료)에서 질문이 묻는 정보를 위치와 관계없이 정확히 찾고, 여러 곳에 흩어진 근거를 빠짐없이 연결해 답했는가?",
        pass_rule="질문이 묻는 정보를 입력의 앞·중간·뒤 어디에 있든(질문과 표현이 달라도) 찾아 썼고, 여러 부분을 함께 써야 할 때 필요한 근거를 모두 연결함",
        fail_rule="입력의 다른 부분을 근거로 듦, 입력에 있는 정보를 없다고 하거나 놓침, 흩어진 근거를 일부만 연결해 결론이 달라짐",
        boundary="‘긴 입력 속에서 찾고 연결했는가’만 본다. 입력에 없는 내용을 만들거나 맞는 부분을 보고도 값을 다르게 옮긴 것은 J5, "
                 "질문이 요구한 항목을 빠뜨린 것은 J4, 이전 대화 턴은 J6 가 본다. FAIL 이유에는 입력 원문의 해당 부분과 답변을 따옴표로 인용한다.",
        example="계약서 중간의 ‘해지 통보는 30일 전’ 조항 대신 뒷부분의 갱신 조항을 근거로 ‘60일 전’이라고 답했는지"),
    # 사용자 결정(2026-10-13): 질문과 함께 보낸 첨부파일을 실제로 읽고 썼는지 따로 본다 — 세부 J8-1 첨부 내용 반영·
    # J8-2 파일 형식 읽기·J8-3 첨부 접근 처리
    JevCriterion(
        "J8", "attachment_use", "첨부파일 활용", "Attachment Utilization",
        question="질문에 첨부한 파일(이미지·TXT·DOCX·PDF 등)의 내용을 실제로 읽고 형식에 맞게 해석해 답했으며, 읽지 못한 부분이 있으면 그 사실을 밝혔는가?",
        pass_rule="첨부 내용을 근거로 답했고, 표의 행·열·그림 속 글자·페이지·목록 같은 구조를 맞게 읽었으며, 열 수 없거나 일부만 읽은 경우 그 사실을 밝힘",
        fail_rule="첨부를 무시하고 일반론으로 답함, 첨부에 없는 내용을 있다고 함, 표의 행·열이나 다른 페이지 내용을 섞어 읽음, 읽지 못한 파일을 읽은 것처럼 답함",
        boundary="‘첨부파일을 읽고 썼는가’만 본다. 맞는 부분을 보고도 값을 다르게 옮긴 것은 J5, 272K 토큰 이상 긴 입력에서 찾기·연결은 J7, "
                 "첨부 요약에서 빠뜨린 것은 J4 가 본다. 파일을 열 수 없다고 밝힌 것은 거절(J1)이 아니라 여기서 본다. "
                 "FAIL 이유에는 답변의 문제 부분을 따옴표로 인용하고, 가능하면 첨부 내용의 해당 부분도 인용한다.",
        example="첨부한 견적서 PDF 표의 ‘합계 1,250,000원’ 대신 바로 위 행의 소계를 합계라고 답했는지"),
)


def jev_questions() -> Dict[str, Dict[str, Any]]:
    """J1~J8 를 Jev System One 의 choice 질문으로 바꾼다. 한 번의 호출에 적용 기준을 함께 보낸다."""
    return {c.id: {"type": "choice", "instructions": c.instructions(),
                   "criteria": {"PASS": c.pass_rule, "FAIL": c.fail_rule}}
            for c in CRITERIA}


def describe() -> Dict[str, Any]:
    """보고서·화면에 보여 줄 기준 정의."""
    items: List[Dict[str, Any]] = [
        {"id": c.id, "key": c.key, "name": c.name, "name_en": c.name_en, "question": c.question,
         "pass": c.pass_rule, "fail": c.fail_rule, "boundary": c.boundary, "example": c.example}
        for c in CRITERIA]
    # Judges retain an unresolved evidence state so source recovery can run.
    # Describe the final server policy separately from those stage instructions.
    for item in items:
        if item["id"] == "J5":
            item["fail"] = ("참조 데이터·신뢰할 수 있는 출처·질문의 값과 다른 사실을 제시한 경우(확인된 사실 오류·출처 불일치)와 "
                            "답변이 든 URL 을 열 수 없는 경우 FAIL. 외부 출처 원문을 읽거나 인용문을 확인하지 못한 것은 FAIL 이 아니라 "
                            "보류 — 사람이 PASS/FAIL 을 정함")
        item["fail"] += ". 평가를 마친 뒤에도 근거가 부족해 확인하지 못한 기준은 보류(사람이 정함) — 확인 실패는 FAIL 로 확정하지 않음 (2026-10-18)"
    return {"version": JEV_CRITERIA_VERSION, "criteria": items, "code_scope_note": CODE_SCOPE_NOTE}
