"""통합 평가기준 목록 — CODE 검사 단위와 J1~J6 의미 기준을 한 목록으로 묶는다. 판정에는 쓰지 않는다.

기준 원문은 지금 위치에 그대로 둔다. 이 모듈은 그 정의를 읽어 같은 모양의 항목으로 모을 뿐이다.
- CODE 상위·소분류 설명: ``taxonomy.CATEGORIES`` · 검사 단위와 규칙 버전: ``core.CHECK_VERSIONS``
- 질문 조건 종류(``extract`` 의 Req.kind) → 검사 단위: ``CODE_UNITS``
- 의미 기준: ``jev_criteria.describe()`` · 적용 조건: ``jev_applicability.APPLIES_WHEN``
- 미판정·근거 부족의 최종 처리: ``semantic_policy.DECISION_POLICY``, CODE 미판정 보완: ``common_adjudication``
기준 ID·결과 스키마·판정·집계는 바꾸지 않는다. ``tests/test_criteria_registry.py`` 가 이 목록과 실제 실행 경로가
어긋나지 않는지(검사 단위·조건 종류·의미 기준) 확인한다.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from .common_adjudication import VERSION as COMPLETION_VERSION
from .core import CHECK_VERSIONS
from .jev_applicability import APPLIES_WHEN
from .jev_criteria import JEV_CRITERIA_VERSION, describe as jev_describe
from .model import SPECIALIZED, SUBCATEGORIES
from .semantic_policy import DECISION_POLICY
from .taxonomy import CATEGORIES

__all__ = ["REGISTRY_VERSION", "CODE_UNITS", "MODIFIER_KINDS", "SUBSUMED_KINDS", "RELATIONS", "registry", "unit_of"]

REGISTRY_VERSION = "criteria-registry-v1"

#: (검사 단위, 이름, 적용 계기, 이 단위를 만드는 질문 조건 종류)
#: 적용 계기 — all: 모든 답변 · question: 질문의 명시 요구 · answer: 답변에 해당 구조가 있을 때 ·
#: question_or_answer: 둘 중 하나 · aiona: AIONA 설정과 표시본이 있을 때
CODE_UNITS = (
    ("1-1.response_nonempty", "일반 응답 존재", "all", ()),
    ("1-1.character_rules", "깨진 문자·제어 문자", "all", ()),
    ("1-1.foreign_script", "설명 없는 외국 문자", "all", ()),
    ("1-2.code_block", "코드블록 존재·개수·금지", "question", ("code_block_required", "code_block_count", "code_block_forbidden")),
    ("1-2.code_language_label", "코드블록 언어 표지", "question", ("code_language_label",)),
    ("1-2.code_fence_min", "코드 구획 표시 2개 이상 (MD 165)", "question", ("code_block_required", "code_block_count")),
    ("1-2.fence_imbalance", "코드·수식 구획 표시 짝 (MD 170)", "answer", ()),
    ("1-2.json_syntax", "JSON 구문", "question_or_answer", ("json_required",)),
    ("1-2.python_syntax", "Python 구문", "question_or_answer", ("python_code_requested",)),
    ("1-2.orphan_closing_tag", "짝이 없는 닫는 태그 (MD 169)", "answer", ()),
    ("1-3.footnote_link", "각주 사용·정의 연결", "answer", ()),
    ("1-3.citation_link", "인용 번호·출처 목록 연결", "answer", ()),
    ("1-3.internal_link", "문서 내부 링크 연결", "answer", ()),
    ("2-1.explicit_empty", "명시 빈 출력", "question", ("explicit_empty",)),
    ("2-1.count", "불릿·항목·문장·문단·줄·어절 수", "question",
     ("bullet_count", "list_count", "sentence_count", "paragraph_count", "line_count", "word_count")),
    ("2-1.char_count", "글자 수", "question", ("char_count",)),
    ("2-1.literal", "필수·금지·시작·끝 문자열 (정확 일치 요구 외 공백 접기)", "question", ("required_literal", "forbidden_literal", "starts_with", "ends_with")),
    ("2-1.charset", "허용 문자", "question", ("charset",)),
    ("2-2.table", "표 존재·개수·열·헤더·명시 행 수", "question",
     ("table_required", "table_forbidden", "table_count", "table_columns", "table_column_count", "table_header",
      "table_data_rows", "table_total_rows", "table_rows_ambiguous")),
    ("2-2.list", "목록 사용·금지", "question", ("list_required", "list_forbidden")),
    ("2-2.heading", "제목 존재·개수", "question", ("heading_required", "heading_count")),
    ("2-2.json_structure", "JSON 최상위 자료형·필수 키·필드 자료형", "question", ("json_required", "json_required_keys", "json_field_type")),
    ("3-1.question_calculation", "질문이 요구한 계산값", "question", ("calculation",)),
    ("3-1.answer_equation", "답변 등식 재계산", "answer", ()),
    ("3-2.declared_count", "답변이 선언한 개수와 이어지는 구조", "answer", ()),
    ("3-3.transform", "원본 보존·변환", "question", ("transform",)),
    ("3-3.unique_values", "명시 고유성", "question", ("unique_values",)),
    ("3-3.aggregate", "명시 집계값", "question", ("aggregate",)),
    ("AIONA-M.internal_marker", "등록 내부 마커 노출", "aiona", ()),
    ("AIONA-F.forbidden_expression", "질문별 등록 금지 표현", "aiona", ()),
)

#: 검사 단위를 만들지 않고 다른 검사의 적용 범위만 바꾸는 질문 조건
MODIFIER_KINDS = {
    "code_requested": "코드 결과물 요청 — 결과물 안의 목록·표·제목 요구를 Markdown 구조로 세지 않음 (2-1·2-2·3-3)",
    "error_example_requested": "오류 예시 요청 — 1-2 구문 검사를 적용 예외로 둠",
    "markdown_forbidden": "Markdown 금지 — 표·코드블록 요구와 충돌하면 해당 구조 조건을 미판정으로 둠",
}

#: 같은 요구를 한 번만 검사하도록 다른 단위가 함께 보는 조건 — (조건, 함께 추출된 조건, 대신 보는 단위, 이유)
SUBSUMED_KINDS = (
    ("list_required", "bullet_count", "2-1.count", "불릿 형식 요구는 불릿 개수 검사가 형식과 개수를 함께 봄 — 2-2 목록 조건을 따로 만들지 않음"),
)

_TRIGGER = {"all": "모든 답변", "question": "질문에 명시 요구가 있을 때", "answer": "답변에 해당 구조가 있을 때",
            "question_or_answer": "질문 요구 또는 답변의 해당 코드블록이 있을 때",
            "aiona": "AIONA 답변에 운영 설정·실제 표시본이 있을 때 (웹 작업 공간에서는 현재 일시 중지)"}

#: 기준 사이의 관계. kind — same_cause: 한 원인이 두 조건을 함께 FAIL · boundary: 지시문으로 나눈 경계 ·
#: overlap: 같은 값을 두 방식이 함께 봄 · gap: 어느 쪽도 판정하지 않음. 판정·집계에는 쓰지 않는다.
RELATIONS = (
    ("1-2.code_block", "1-2.code_fence_min", "same_cause",
     "코드블록을 요구했는데 없으면 두 조건이 함께 FAIL. MD 165행은 별도 공식 조건이라 질문 결과는 같고 조건 통과율에만 두 번 반영"),
    ("2-1.count", "3-2.declared_count", "same_cause",
     "같은 목록에서 항목이 빠지면 질문 요구 개수와 답변 자체 선언이 함께 FAIL. 비교 대상(질문 요구·답변 선언)이 달라 별도 유지"),
    ("2-1.count", "J4", "boundary", "개수 누락은 CODE, 목적에 필요한 핵심 누락은 J4 (J4 지시문에 명시)"),
    ("2-2.table", "J1", "boundary", "형식 충족은 CODE, 요청한 일을 했는지는 J1 (J1 지시문에 명시)"),
    ("3-1.question_calculation", "J3", "boundary",
     "J3 '풀이' 적용 질문에서 계산 결과 오류를 J3가 함께 FAIL할 수 있음. 지시문은 단순 계산 오류를 CODE 몫으로 둠"),
    ("2-1.literal", "J2", "overlap",
     "'~는 쓰지 마' 같은 금지 문자열은 2-1 문자열 검사와 J2 '제약' 적용 규칙이 함께 봄. CODE 범위 안내 예시에 문자열 조건이 없음"),
    ("3-3.transform", "J5", "overlap",
     "질문 원본 값의 보존은 3-3 정확 비교와 J5 input_consistency 주장이 함께 봄"),
    ("1-3.citation_link", "J5", "gap",
     "1-3은 답변 안 인용 번호 연결만, J5는 평가자의 독립 검색. 답변이 직접 든 출처가 그 주장을 뒷받침하는지는 어느 쪽도 판정하지 않음"),
    ("2-1", "J6", "gap",
     "이전 턴의 분량·형식 지시: CODE는 현재 질문만 읽고 J6 지시문은 분량·형식을 CODE 몫으로 제외. 긴 대화에서 어느 쪽도 판정하지 않음"),
    ("2-2", "J6", "gap", "이전 턴의 표·목록·JSON 형식 지시도 위와 같음"),
)

_CODE_EVIDENCE = ("결과 행마다 required(질문에서 읽은 요구)·measured(측정값)·comparison(비교식)·reason·"
                  "evidence(답변·질문 위치)·explain(쉬운 설명). 코드 규칙으로 만들며 LLM을 쓰지 않음")
_CODE_UNJUDGED = ("UNSUPPORTED·DEPENDENCY_FAILED·INCOMPLETE·CHECK_ERROR·LIMIT_EXCEEDED·CONFIG_MISSING·DISPLAY_MISSING은 "
                  "판정 없음(통과율 분모 제외). 공통 미판정은 같은 의미 평가 호출의 C 기준으로 보완 판정"
                  f"({COMPLETION_VERSION}, 근거 없는 보완 판정은 무효)")
_SEMANTIC_EVIDENCE = {
    "J5": ("fact_check.claims[] — 답변 원문 발췌 주장별 verdict·reason·sources(url·quote·authority·temporal_relevance·origin). "
           "서버가 검색 도구 기록의 URL, 출처 원문·입력 원문의 인용 일치를 확인하고 맞지 않으면 그 주장을 확인 필요로 바꿈"),
    "J6": ("context_checks[] — 이전 턴 번호·instruction_quote·answer_quote·decision·reason. "
           "서버가 두 인용문을 이전 턴·현재 답변 원문과 대조하고 맞지 않으면 확인 필요로 바꿈"),
}
_SEMANTIC_REASON = ("reason — 지시문은 FAIL 이유에 답변 원문 인용을 요구하지만 서버가 인용을 대조하지 않고, "
                    "빈 이유의 FAIL도 저장됨")
_SEMANTIC_UNJUDGED = ("UNVERIFIED는 최종 정책으로 FAIL(EVALUATION_EVIDENCE_INSUFFICIENT, 사실 오류 확정 아님), "
                      "J5 출처 원문 확인 실패는 FAIL(SOURCE_ORIGINAL_UNAVAILABLE). 호출 오류·시간 초과·형식 오류·미연결은 "
                      "판정 없음(미완료)")


def _code_entries() -> List[Dict[str, Any]]:
    subs = {sub["id"]: sub for cat in CATEGORIES for sub in cat["subs"]}
    out = []
    for unit, name, trigger, kinds in CODE_UNITS:
        group = unit.split(".")[0]
        sub = subs.get(group)
        out.append({
            "id": unit, "layer": "CODE", "group": group,
            "group_name": SUBCATEGORIES.get(group) or SPECIALIZED.get(group),
            "category": group.split("-")[0] if group in SUBCATEGORIES else "AIONA",
            "name": name, "trigger": trigger, "applies_when": _TRIGGER[trigger], "requirement_kinds": list(kinds),
            "method": "CODE" if trigger == "aiona" else "CODE (공통 미판정은 LLM 보완)",
            "pass": sub["pass"] if sub else "등록 목록 대조에서 검출 없음",
            "fail": sub["fail"] if sub else "등록 목록의 표현·마커가 실제 표시본에 있음",
            "pass_fail_scope": "소분류 공통 설명 (taxonomy.CATEGORIES)" if sub else "AIONA 특화 항목",
            "unjudged": _CODE_UNJUDGED, "evidence": _CODE_EVIDENCE,
            "counted_in": "공통 통과율·질문 최종 판정" if group in SUBCATEGORIES else "별도 표시 (공통 통과율에 더하지 않음)",
            "version": CHECK_VERSIONS[unit],
        })
    return out


def _semantic_entries() -> List[Dict[str, Any]]:
    out = []
    for c in jev_describe()["criteria"]:
        trigger = {"J1": "all", "J5": "answer", "J6": "history"}.get(c["id"], "question")
        out.append({
            "id": c["id"], "layer": "LLM", "group": c["id"], "group_name": c["name"], "category": "J",
            "name": c["name"], "trigger": trigger, "applies_when": APPLIES_WHEN[c["id"]],
            "applies_when_api": ("CLI/API 평가자는 모든 답변에서 사실 주장을 먼저 선별하고, 없으면 해당 없음"
                                 if c["id"] == "J5" else APPLIES_WHEN[c["id"]]),
            "requirement_kinds": [], "method": "LLM",
            "question": c["question"], "boundary": c["boundary"], "pass": c["pass"], "fail": c["fail"],
            "pass_fail_scope": "기준 (최종 판정 정책 반영 설명)",
            "unjudged": _SEMANTIC_UNJUDGED, "evidence": _SEMANTIC_EVIDENCE.get(c["id"], _SEMANTIC_REASON),
            "counted_in": "기준별 통과율·질문 최종 판정",
            "version": JEV_CRITERIA_VERSION,
        })
    return out


def unit_of(check_id: str) -> Optional[str]:
    """결과 행의 check_id(예: ``2-1.count:bullet_count``)가 속한 검사 단위. 엔진 오류 행은 None."""
    unit = check_id.split(":")[0]
    return unit if unit in CHECK_VERSIONS else None


def registry() -> Dict[str, Any]:
    return {"version": REGISTRY_VERSION, "criteria": _code_entries() + _semantic_entries(),
            "modifier_kinds": copy.deepcopy(MODIFIER_KINDS),
            "subsumed_kinds": [{"kind": k, "when": w, "checked_by": u, "note": n} for k, w, u, n in SUBSUMED_KINDS],
            "relations": [{"a": a, "b": b, "kind": kind, "note": note} for a, b, kind, note in RELATIONS],
            "decision_policy": copy.deepcopy(DECISION_POLICY),
            "aggregation": ("질문 최종 판정: 적용된 CODE 조건·의미 기준 중 하나라도 FAIL이면 FAIL, 모두 판정되고 FAIL이 없으면 PASS, "
                            "그 밖은 미완료. 모델 점수 = PASS 질문 수 / 전체 질문 수. 조건·기준 통과율 = PASS / (PASS + FAIL). "
                            "같은 질문의 여러 FAIL은 질문 점수에 한 번만 반영됨")}
