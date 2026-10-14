"""통합 평가기준 목록 — CODE 검사 단위와 J1~J8 의미 기준을 한 목록으로 묶는다. 판정에는 쓰지 않는다.

기준 원문은 지금 위치에 그대로 둔다. 이 모듈은 그 정의를 읽어 같은 모양의 항목으로 모을 뿐이다.
- CODE 상위·소분류 설명: ``taxonomy.CATEGORIES`` · 검사 단위와 규칙 버전: ``core.CHECK_VERSIONS``
- 질문 조건 종류(``extract`` 의 Req.kind) → 검사 단위: ``CODE_UNITS``
- 의미 기준: ``jev_criteria.describe()`` · 적용 조건: ``jev_applicability.APPLIES_WHEN``
- 미판정·근거 부족의 최종 처리: ``semantic_policy.DECISION_POLICY`` (공통 CODE 행은 PASS/FAIL 뿐, 보완 기준 없음)
기준 ID·결과 스키마·판정·집계는 바꾸지 않는다. ``tests/test_criteria_registry.py`` 가 이 목록과 실제 실행 경로가
어긋나지 않는지(검사 단위·조건 종류·의미 기준) 확인한다.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

from .core import CHECK_VERSIONS
from .jev_applicability import APPLIES_WHEN
from .jev_criteria import JEV_CRITERIA_VERSION, describe as jev_describe
from .model import SPECIALIZED, SUBCATEGORIES
from .semantic_policy import DECISION_POLICY
from .taxonomy import CATEGORIES

__all__ = ["REGISTRY_VERSION", "CODE_UNITS", "MODIFIER_KINDS", "SUBSUMED_KINDS", "RELATIONS", "registry", "unit_of"]

#: 두 작업 줄에서 v2 가 다른 뜻으로 쓰였다 — main v2(semantic_items·changelog)와 브랜치 v2(이전 턴 승계·출처 대조 경계, 통합 v3).
#: 둘을 합친 목록 → v4. 기존 번호의 뜻은 바꾸지 않는다 (CHANGELOG 참고).
REGISTRY_VERSION = "criteria-registry-v10"   # v10: 세부 기준(평가 대상) 목록 고정 — CODE 28 검사 → 질문 조건 37, 의미 8 → 30 → 44 · v9: J6 멀티턴 = 10턴 이상 · 대화 전체 10K 토큰 · v8: J8 첨부파일 활용 추가 · v7: J7 272K 확정 · v6: J7 250K 이상(테스트 기간)·J6 멀티턴 길이 조건 · v5: J6 이름 변경, J7 추가

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
    "markdown_forbidden": "Markdown 금지 — 표·코드블록·목록·제목 요구와 충돌하면 해당 구조 조건을 만들지 않음 (행 없음)",
}

#: 같은 요구를 한 번만 검사하도록 다른 단위가 함께 보는 조건 — (조건, 함께 추출된 조건, 대신 보는 단위, 이유)
SUBSUMED_KINDS = (
    ("list_required", "bullet_count", "2-1.count", "불릿 형식 요구는 불릿 개수 검사가 형식과 개수를 함께 봄 — 2-2 목록 조건을 따로 만들지 않음"),
)

_TRIGGER = {"all": "모든 답변", "question": "질문 또는 대화 앞 턴의 지속 지시(앞으로·계속·모든 답변…)에 명시 요구가 있을 때",
            "answer": "답변에 해당 구조가 있을 때 — 없으면 '검사 대상 없음' PASS 1조건 (답변마다 조건 1개, 엔진 v16.5.0)",
            "question_or_answer": "질문 요구 또는 답변의 해당 코드블록이 있을 때 — 없으면 '검사 대상 없음' PASS 1조건 (답변마다 조건 1개, 엔진 v16.5.0)",
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
    ("1-3.citation_link", "J5", "boundary",
     "1-3은 답변 안 인용 번호·각주 연결만, 답변이 든 출처가 주장을 뒷받침하는지는 J5 citation_checks 가 서버 원문 대조로 확인 "
     "(answer-citations-v1 부터, 2026-10-12 이전에는 공백. v2 는 서버가 받지 못한 출처를 공급자 web_fetch 원문으로 대조)"),
    ("2-1", "J6", "boundary",
     "이전 턴의 지속 분량·형식 지시는 CODE 가 다음 턴 조건으로 이어받아 검사(작업 브랜치 엔진 v16.3.0, 통합 v16.4.0), J6는 의미 맥락만. "
     "2026-10-12 이전에는 어느 쪽도 판정하지 않던 공백"),
    ("2-2", "J6", "boundary", "이전 턴의 지속 표·목록·JSON 형식 지시도 위와 같음"),
)

_CODE_EVIDENCE = ("결과 행마다 required(질문에서 읽은 요구)·measured(측정값)·comparison(비교식)·reason·"
                  "evidence(답변·질문 위치)·explain(쉬운 설명). 코드 규칙으로 만들며 LLM을 쓰지 않음")
_CODE_UNJUDGED = ("공통 결과 행은 PASS/FAIL 뿐. 코드로 확정할 수 없는 조건·선행 FAIL 에 종속된 조건은 행을 만들지 않음"
                  "(통과율 분모 제외, 사유는 meta.skipped_conditions). 검사기 오류·입력 한도 초과는 사례의 errors 에 "
                  "실행 오류로 기록(품질 FAIL 아님, 그 질문은 PASS 로 확정하지 않음). "
                  "답변 구조에서 생기는 검사 9개(적용 계기 answer·question_or_answer)는 답변마다 조건 1개로 집계 — 대상이 몇 개든 "
                  "하나라도 FAIL 이면 FAIL, 대상이 없으면 '검사 대상 없음' PASS 행(check_id 끝 ':none'), 판정 제외만 있으면 "
                  "'확인된 위반 없음' PASS (엔진 v16.5.0)")
_SEMANTIC_EVIDENCE = {
    "J5": ("fact_check.claims[] — 답변 원문 발췌 주장별 verdict·reason·sources(url·quote·authority·temporal_relevance·origin). "
           "서버가 검색 도구 기록의 URL, 출처 원문·입력 원문의 인용 일치를 확인하고 맞지 않으면 그 주장을 확인 필요로 바꿈. "
           "서버가 접근 문제로 받지 못한 출처는 같은 요청에서 평가자 web_fetch 가 실제로 받은 원문으로만 대조(retrieval=provider_document). "
           "답변이 직접 든 출처는 citation_checks — 서버가 그 URL 을 받아 인용·수치를 대조하고 확정된 불일치만 FAIL. "
           "판정마다 j5_outcome(사실 오류·검증 기준 미충족·출처 불일치·검색 불가 등)"),
    "J6": ("context_checks[] — 이전 턴 번호·instruction_quote·answer_quote·decision·reason. "
           "서버가 두 인용문을 이전 턴·현재 답변 원문과 대조하고 맞지 않으면 확인 필요로 바꿈. 인용 대조 실패로 확인 필요가 되면 "
           "'원문 그대로 복사' 안내를 붙여 J6 만 1회 다시 묻고, 그래도 맞지 않으면 그대로 둠 (history-quotes-v4, context_reevaluation)"),
    "J7": ("reason — FAIL 이면 입력(질문·참조 자료) 원문과 답변을 각각 따옴표로 인용(둘 다 원문과 맞아야 함), 서버가 인용 일치를 형식 검증하고 근거가 없으면 1회 재평가 "
           "(j1-j4-j7-j8-fail-evidence-v3). long_context — 입력 토큰 수(tiktoken o200k_base)·"
           "근거 인용의 입력 내 위치(0~100, 판정에는 쓰지 않음). 토크나이저를 못 불러오면 J7 은 확인 필요(UNVERIFIED)로 집계. items 는 J7-1.a·J7-2.a 가 NO 이면 입력 원문 인용이 필수이고 서버가 현재 질문·참조 자료와 대조"),
    "J8": ("reason — FAIL 이면 답변의 문제 부분을 따옴표로 인용(답변 원문과 맞아야 함), 첨부 내용(attachments[].content) 인용도 근거로 셈. "
           "서버가 인용 일치를 형식 검증하고 근거가 없으면 1회 재평가 (j1-j4-j7-j8-fail-evidence-v3). items 는 첨부 내용 인용을 question_quote 로 받아 "
           "첨부 content 와 대조. 답변이 첨부를 열 수 없다고 밝히면 J8-1·J8-2 는 해당 없음, J8-3 이 봄"),
}
_SEMANTIC_REASON = ("reason — FAIL 이면 서버가 이유의 존재와 질문·답변 원문 인용 일치를 형식 검증하고, 근거가 없으면 "
                    "그 기준만 1회 재평가 (j1-j4-fail-evidence-v1). 이유의 의미적 타당성은 검증하지 않음")
_SEMANTIC_UNJUDGED = ("INCOMPLETE(판정 미완료) — 재평가 뒤에도 근거 없는 J1~J4·J7·J8 FAIL(JUDGE_FAIL_EVIDENCE_MISSING), "
                      "검색 불가·검색 실행 오류·검색 없이 연 원문의 도구 오류로 확인하지 못한 J5(J5_SEARCH_UNAVAILABLE·J5_SEARCH_ERROR·J5_FETCH_ERROR). "
                      "그 밖의 UNVERIFIED는 최종 정책으로 FAIL(EVALUATION_EVIDENCE_INSUFFICIENT, 사실 오류 확정 아님), "
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
            "method": "CODE",
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
        trigger = {"J1": "all", "J5": "answer", "J6": "history", "J7": "input_length", "J8": "attachment"}.get(c["id"], "question")
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


#: 세부 기준(평가 대상) — 질문 생성·적용 수 점검·최소 질문 수가 함께 세는 단위. 정의는 여기 한 곳 (사용자 결정 2026-10-14: 평가 기준을
#: 확실하게 고정한다). CODE: 검사 28개 중 질문 요구로 생기는 검사의 질문 조건 종류('검사:종류'). 같은 종류를 두 검사가 함께 보면 처음 검사에만
#: 둔다. 판정하지 않는 종류(표 행 수가 모호함)는 뺀다. 의미 평가: 점검 항목(semantic_items).
CONDITION_KIND_NAMES = {
    "code_block_required": "코드블록 요구", "code_block_count": "코드블록 개수", "code_block_forbidden": "코드블록 금지",
    "code_language_label": "코드블록 언어 표지", "json_required": "JSON 출력 요구", "python_code_requested": "파이썬 코드 요구",
    "explicit_empty": "빈 출력 요구", "bullet_count": "불릿 개수", "list_count": "항목 개수", "sentence_count": "문장 수",
    "paragraph_count": "문단 수", "line_count": "줄 수", "word_count": "어절 수", "char_count": "글자 수",
    "required_literal": "필수 문구", "forbidden_literal": "금지 문구", "starts_with": "시작 문구", "ends_with": "끝 문구",
    "charset": "허용 문자", "table_required": "표 요구", "table_forbidden": "표 금지", "table_count": "표 개수",
    "table_columns": "표 열 이름", "table_column_count": "표 열 수", "table_header": "표 머리글", "table_data_rows": "표 데이터 행 수",
    "table_total_rows": "표 전체 행 수", "list_required": "목록 요구", "list_forbidden": "목록 금지", "heading_required": "제목 요구",
    "heading_count": "제목 개수", "json_required_keys": "JSON 필수 키", "json_field_type": "JSON 필드 자료형", "calculation": "질문 계산값",
    "transform": "정렬·필터·변환", "unique_values": "중복 없이", "aggregate": "합계·평균 집계",
}
UNSCORED_KINDS = frozenset({"table_rows_ambiguous"})


def condition_kinds() -> List[Dict[str, str]]:
    """CODE 세부 조건 목록 [{id: '검사:종류', check, kind, name}] — CODE_UNITS 순서, 검사 안에서는 종류 이름순."""
    out: List[Dict[str, str]] = []
    seen = set()
    for unit, _, trigger, kinds in CODE_UNITS:
        if trigger == "aiona":
            continue
        for kind in sorted(kinds):
            if kind in UNSCORED_KINDS or kind in seen:
                continue
            seen.add(kind)
            out.append({"id": f"{unit}:{kind}", "check": unit, "kind": kind, "name": CONDITION_KIND_NAMES[kind]})
    return out


def evaluation_targets() -> Dict[str, Any]:
    """평가 기준 전체를 단계별로 — CODE 대분류 → 소분류 → 검사 → 세부 조건, 의미 기준 → 세부 기준 → 점검 항목 (개수 포함)."""
    from .semantic_items import ITEMS, SUBS
    checks = [unit for unit, _, trigger, _ in CODE_UNITS if trigger != "aiona"]
    conditions = condition_kinds()
    criteria = sorted({s.criterion for s in SUBS}, key=lambda c: int(c[1:]))
    return {"code": {"categories": [c["id"] for c in CATEGORIES], "subcategories": list(SUBCATEGORIES), "checks": checks,
                     "conditions": conditions,
                     # 질문 요구로 생기는 검사 18 — 세부 조건 37 로 나눠 센다(코드 구획 표시 수는 코드블록 요구 조건을 함께 쓴다)
                     "question_checks": [u for u, _, trigger, _ in CODE_UNITS if trigger in ("question", "question_or_answer")],
                     "always_checks": [u for u, _, trigger, _ in CODE_UNITS if trigger == "all"],
                     "answer_checks": [u for u, _, trigger, _ in CODE_UNITS if trigger == "answer"]},
            "semantic": {"criteria": criteria, "subs": [s.id for s in SUBS], "items": [i.id for i in ITEMS]},
            "counts": {"code_categories": len(CATEGORIES), "code_subcategories": len(SUBCATEGORIES), "code_checks": len(checks),
                       "code_conditions": len(conditions), "semantic_criteria": len(criteria), "semantic_subs": len(SUBS),
                       "semantic_items": len(ITEMS)}}


def unit_of(check_id: str) -> Optional[str]:
    """결과 행의 check_id(예: ``2-1.count:bullet_count``)가 속한 검사 단위. 엔진 오류 행은 None."""
    unit = check_id.split(":")[0]
    return unit if unit in CHECK_VERSIONS else None


#: 기준 변경 이력 — 버전마다 바뀐 항목과 이유 (이력 관리 '기준 변경' 의 근거). 새 항목은 끝에 더한다.
CHANGELOG: List[Dict[str, Any]] = [
    {"version": "criteria-registry-v1", "date": "2026-10-12", "scope": "registry",
     "changes": ["기준 정의를 한 목록으로 묶음 (CODE 검사 단위 28 + 의미 기준 J1~J6)", "테스트가 실행 경로와 목록을 대조"]},
    {"version": "engine v16.3.0", "date": "2026-10-13", "scope": "code",
     "changes": ["공통 결과 행은 PASS/FAIL 뿐 — 판정할 수 없는 조건은 meta.skipped_conditions, 검사기 오류는 case.errors",
                 "C(LLM 보완) 기준 제거, 저장된 code_completion 은 읽기 전용", "2-1.literal: 정확 일치 요구가 아니면 공백·줄바꿈을 접어 비교(G-1)",
                 "3-1 답변 등식 재계산 v16-1.4 (줄 경계·괄호·TeX·단위·연결 어미·명사 부정 범위)",
                 "3-3·2-2: 코드 작성 요구·원본 없음·자료형 목록은 조건을 만들지 않음"]},
    {"version": "semantic-items-v1", "date": "2026-10-13", "scope": "semantic",
     "changes": ["의미 평가를 기준 6 → 세부 기준 25 → 점검 항목 39 로 세분 (SEMANTIC_PIPELINE=items)",
                 "판정에 '모른다' 없음 — 인용 검증 실패는 재확인 ≤3회 뒤 확인 불가 FAIL", "J5: 주장별 참·거짓, 반박 출처 서버 대조, 출처 링크 유무 미사용",
                 "적용 여부는 코드 규칙이 정함 (sub-applicability-v1, j5-items-applicability-v1)"]},
    {"version": "criteria-registry-v2", "date": "2026-10-13", "scope": "registry",
     "changes": ["semantic_items(세부 25·항목 39·'판정하는 곳' 규칙·나열 규칙·reason_code) 를 목록에 포함", "changelog 추가"]},
    {"version": "engine v16.4.0", "date": "2026-10-12", "scope": "code",
     "changes": ["이전 턴의 지속 분량·형식 지시를 다음 턴 조건으로 이어받아 검사 (source=history_instruction, G-3)",
                 "이전 턴 지시 해제·일시 해제 인식 (F3)", "필수 문구 공백은 main 의 접기 방식으로 일원화 (G-1)",
                 "'N개 열'은 항목 수가 아님, 인용문 안 목록 인식",
                 "main v16.3.0 과 작업 브랜치 v16.3.x 통합 (integrate/main-eval-reliability)"]},
    {"version": "criteria-registry-v3", "date": "2026-10-12", "scope": "registry",
     "changes": ["작업 브랜치 목록(이전 턴 승계·출처 대조 경계)과 main 의 C 보완 제거·공백 접기·행 없음 설명을 합침 (브랜치 integrate/main-eval-reliability)",
                 "main 의 criteria-registry-v2 와는 다른 목록"]},
    {"version": "engine v16.4.1", "date": "2026-10-13", "scope": "code",
     "changes": ["3-1 답변 등식 v16-1.5: 좌변 앞 화살표('20일 → 26일 = 26 ÷ 20')·연산자(단위에 끊긴 앞 피연산자)와 "
                 "'185분 / 190분 이하' 같은 '값 / 한도' 표기는 등식으로 보지 않고 보류 (저장 실행의 가짜 FAIL)",
                 "2-1.count v16-1.7: 문장 끝 부호 뒤 강조 닫기(**·__)도 문장 끝 ('중단됩니다.**' 를 다음 문장과 붙여 세던 가짜 FAIL)"]},
    {"version": "evidence-v3.13-input-values-match", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J5: 답변이 사용자가 준 값을 그대로 쓰면 그 값의 실제 데이터는 사용자 원문 — 요청 종류(요약·계산·작성·추천 …)를 "
                 "키워드로 알아보지 못해도 정확한 원문 인용이면 '제공 자료와 일치'로 본다 (사용자 결정, 저장 실행 미완료 대부분의 원인)",
                 "남는 경계: 진위 확인 요청·실제 데이터 요청에서는 입력이 근거가 아님, 원문·턴 인용은 그대로 대조"]},
    {"version": "evidence-v3.14-cited-url-access", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J5: 답변이 준 출처 URL 은 서버가 열어 확인 — 열 수 없으면 '접근할 수 없는 URL 을 준 것'으로 FAIL (사용자 결정)",
                 "PASS 도 답변이 든 모든 출처가 주장을 뒷받침한다고 확인될 때만, 하나라도 확인되지 않으면 확인 필요",
                 "J5 판정 사유에 '출처 접근 불가' 추가 (j5-outcome-detail-v4)"]},
    {"version": "criteria-registry-v4", "date": "2026-10-13", "scope": "registry",
     "changes": ["main v2(semantic_items·changelog)와 브랜치 v3(이전 턴 승계·출처 대조·판정 미완료 정책)을 병합",
                 "의미 평가 jev-semantic-eval-v2.8 (main v2.6 items 기록 + 브랜치 v2.7)"]},
    {"version": "evidence-v3.15-quote-markup", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J5 출처 인용 대조: 평가 모델이 web_fetch(Markdown 변환) 원문에서 옮긴 인용의 굵게(**…**) 표시를 벗긴 형태도 "
                 "원문과 대조 (원문 쪽은 그대로, 앞뒤가 공백·문장 경계인 짝만) — 페이지에 그대로 있는 문장을 '인용문 미확인'으로 FAIL 하던 문제 "
                 "(중간 점검 실행 557a50d8baf1, Claude Q15)",
                 "백틱·이스케이프가 있거나 2,000자를 넘는 인용은 글자 그대로만 대조 (코드 기호 '2**3'·'func(**kw)' 는 벗기지 않음)"]},
    {"version": "evidence-v3.16-answer-markup", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J5 주장-답변 대조: 평가 모델이 답변의 '- **시간 복잡도**: O(log N)' 을 '시간 복잡도: O(log N)' 처럼 화면에 보이는 "
                 "글자로 옮긴 주장도 답변에 있는 것으로 봄 (답변의 굵게 표시 '**…**' 만 벗김, 앞뒤 공백·문장 경계, 안쪽 200자 이내) — "
                 "중간 점검 재실행 553a9e8c90b6 의 Gemini Q15 '평가 근거 부족' FAIL"]},
    {"version": "long-context-v1", "date": "2026-10-13", "scope": "semantic",
     "changes": ["롱컨텍스트 판별: 입력(현재 질문 + 참조 자료, 이전 턴 제외)을 tiktoken o200k_base 로 세어 8K 토큰 이상이면 J7 적용 (사용자 결정)",
                 "길이 구간 8K~32K·32K~128K·128K 이상을 판정 기록에 남김 (LV-Eval·RULER·∞Bench 단계), 토크나이저를 못 불러오면 '판별하지 못함'",
                 "J7 근거 인용의 입력 내 위치(0~100)를 기록 — Lost in the Middle 분석용, 판정에는 쓰지 않음"]},
    {"version": "semantic-items-v2", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J7-1 위치와 관계없는 정보 찾기(J7-1.a)·J7-2 흩어진 근거 연결(J7-2.a) 추가 — 세부 27·항목 41",
                 "J6 세부 기준 이름 표기를 '멀티턴 맥락 유지'로"]},
    {"version": "criteria-registry-v5", "date": "2026-10-13", "scope": "registry",
     "changes": ["J6 이름을 '멀티턴 맥락 유지'로 바꿈 (판정 내용은 그대로, 대화 2번째 턴부터) — 사용자 결정",
                 "J7 롱컨텍스트 활용 추가: 입력(현재 질문 + 참조 자료) 8K 토큰 이상(tiktoken o200k_base), 세부 J7-1 위치와 관계없는 정보 찾기·"
                 "J7-2 흩어진 근거 연결. 근거: Lost in the Middle·RULER·NoLiMa·LooGLE·BABILong. 경계: 입력에 없는 내용·값 오기는 J5, 요구 항목 누락은 J4",
                 "의미 평가 기준 J1~J7, items 세부 27·항목 41 (semantic-items-v2), 적용 판정 jev-applicability-v2.4·sub-applicability-v2"]},
    {"version": "history-quotes-v4-quote-retry", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J6: context_checks 인용이 이전 턴·현재 답변 원문과 맞지 않아 확인 필요가 되면, 같은 질문·답변·이전 턴에 "
                 "'인용은 원문 그대로 복사' 안내만 붙여 J6 만 1회 다시 묻는다 (legacy, CLI·API 평가자) — 평가 모델이 답변의 "
                 "'$\\rightarrow$' 를 '→' 로 바꿔 인용해 10턴 대화가 '평가 근거 부족' FAIL 이 된 문제 (중간 점검 557a50d8baf1, Gemini Q29 7턴)",
                 "다시 받은 J6 가 인용 대조를 통과해 PASS·FAIL 이면 그것을 쓰고, 아니면 지금 판정을 그대로 둔다 — 근거를 확인하지 못한 판정은 "
                 "여전히 PASS 가 아님. 시도는 레코드의 context_reevaluation(첫 판정·재질문 결과·쓴 쪽)에 남김"]},
    {"version": "engine v16.6.0", "date": "2026-10-14", "scope": "code",
     "changes": ["질문 본문 속 붙여 넣은 자료('[붙여 넣은 자료]' 줄 ~ '[자료 끝]' 줄)는 사용자가 준 자료이지 지시가 아니다 — 요구 조건 추출, "
                 "앞 턴 지속 지시·작업명 판별, J5 사실 질의·가상 설정·코드 요청·진위 요청 판별은 그 구간을 가린 요청 부분에서만 한다. "
                 "값 대조(J5 입력 일치 인용·J5-2.a 값·3-3 원본)는 자료까지 본다 (첫 실제 실행에서 J6 자료 속 '…라고 기록하지 않는다' 가 "
                 "필수 문자열 요구로 읽힌 문제, 사용자 결정 2026-10-14: 붙여 넣은 글은 첨부가 아니라 질문 내용)",
                 "표시는 독립된 줄로만 인식하고, 자료를 먼저 가린 뒤 펜스·표·인용 구조를 본다. 끝 표시가 없는 v1 세트는 저장된 계획으로 변환"]},
    {"version": "engine v16.5.0", "date": "2026-10-13", "scope": "code",
     "changes": ["답변 구조에서 생기는 검사 9개(코드·수식 구획 짝, JSON 구문, Python 구문, 짝 없는 닫는 태그, 각주·인용·내부 링크 연결, "
                 "답변 등식 재계산, 선언 개수)는 답변마다 조건 1개로 집계 — 대상(코드블록·등식…)이 몇 개든 하나라도 FAIL 이면 FAIL, "
                 "대상이 없으면 '검사 대상 없음' PASS 행(check_id 끝 ':none'). 질문이 같으면 모델마다 조건 수가 같다 "
                 "(사용자 결정 — 실행 c692db1e6452 에서 CODE 3 조건이 AIONA 33 대 GPT 3 으로 통과율을 견줄 수 없던 문제)",
                 "대상별 행(블록·등식마다 1행)은 상세 판정에 그대로 남는다. 판정 제외(meta.skipped_conditions)만 있는 단위는 "
                 "'확인된 위반 없음' PASS, 실행 오류가 있는 단위는 행을 두지 않는다(그 질문은 PASS 로 확정하지 않음). "
                 "질문이 만드는 검사(2-1·2-2·3-3·3-1 질문 계산값 등)와 항상 검사(1-1)는 그대로"]},
    {"version": "long-context-v2-250k-test", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J7 적용 경계 8K → 250K 토큰 (사용자 결정, 테스트 기간): 후보 경계는 272K, 250K~500K 입력을 실제로 평가해 확정",
                 "구간 250K~272K·272K~300K·300K~350K·350K~400K·400K~500K·500K 이상 — 구간별 통과율로 성능이 떨어지기 시작하는 곳을 봄",
                 "질문·참조 자료 입력 한도 10만 → 300만 자 (250K~500K 토큰 입력이 CODE 입력 검증에서 막히지 않도록)"]},
    {"version": "multi-turn-v1", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J6 멀티턴 = 한 턴(질문 + 답변)이 10K 토큰 이상인 턴이 10개 이상인 대화 (사용자 결정, 예전에는 2턴 이상)",
                 "각 모델이 실제로 주고받은 대화(같은 질문 + 그 모델 답변)로 잼 — 같은 대화도 모델마다 다를 수 있음 (사용자 결정)",
                 "멀티턴이면 2번째 턴부터 J6, 아니면 해당 없음(대화 유지율에서 '멀티턴 아님'으로 따로 셈), 토크나이저가 없으면 확인 필요"]},
    {"version": "semantic-items-v3", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J7-1·J7-2 적용 조건 문구를 입력 250K 토큰 이상으로", "적용 판정 jev-applicability-v2.5-length-rules·sub-applicability-v3"]},
    {"version": "criteria-registry-v6", "date": "2026-10-13", "scope": "registry",
     "changes": ["J7 경계 250K(테스트 기간)·J6 멀티턴 길이 조건 반영 (long-context-v2-250k-test·multi-turn-v1)"]},
    {"version": "long-context-v3-272k", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J7 적용 경계 250K(테스트 기간) → 272K 토큰 확정 (사용자 결정)",
                 "근거: 250K~500K 를 RULER 방식으로 시험(기본·어려운 버전, 세 비교 모델)한 결과 500K 까지 정답률 하락이 없어 성능으로는 "
                 "경계를 가를 수 없었다 → 실제로 쓰이는 긴 입력 경계인 GPT(OpenAI) 롱컨텍스트 요금 경계 272K 를 따름. "
                 "우리 셈(tiktoken o200k_base)과 GPT 가 보고한 입력 토큰은 거의 같음(500K 문서 500,028)",
                 "구간 272K~300K·300K~350K·350K~400K·400K~500K·500K 이상 (250K~272K 비교 구간·후보 경계 기록 삭제)",
                 "적용 판정 jev-applicability-v2.6-272k·sub-applicability-v4",
                 "LLM 선별 엔진(CLI·OpenAI·Anthropic)의 적용 버전도 코드 규칙 버전을 담음 — jev-applicability-v2.6-272k+llm-screening (예전 v2.3-llm-screening 고정값은 길이 규칙이 바뀌어도 같아 버전 비교에서 차이가 안 보였음)"]},
    {"version": "long-context-v3.1-272k", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J7 결과를 길이 구간(272K~300K … 500K 이상)으로 나눠 남기지 않음 (사용자 결정) — 판정 기록의 long_context 에서 tier 삭제, 근거 문구는 '입력 N토큰', 요약의 J7 구간별 통과율(summary.J7.tiers)·질문 세트 보고서의 길이 구간 표시 삭제. 입력 토큰 수와 근거 인용 위치는 그대로"]},
    {"version": "semantic-items-v4", "date": "2026-10-13", "scope": "semantic", "changes": ["J7-1·J7-2 적용 조건 문구를 입력 272K 토큰 이상으로", "J7 예시 첨부 길이를 272K 토큰 이상(한국어 약 40만·45만 자)으로 — 설계안 v1.5"]},
    {"version": "criteria-registry-v7", "date": "2026-10-13", "scope": "registry", "changes": ["J7 경계 272K 확정 반영 (long-context-v3-272k)"]},
    {"version": "jev-criteria-v5", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J8 첨부파일 활용 추가 (사용자 결정) — 질문과 함께 보낸 첨부파일(이미지·TXT·DOCX·PDF 등)을 실제로 읽고 형식에 맞게 해석해 썼는지, "
                 "읽지 못한 부분을 밝혔는지 본다. 경계: 값을 다르게 옮김 J5, 272K 이상 긴 입력의 찾기·연결 J7, 요약 누락 J4, "
                 "파일을 열 수 없다고 밝힌 것은 거절(J1)이 아니라 J8",
                 "입력 attachments=[{name, type, content, sha256?, size?, path?}] — content 는 파일에 실제로 든 내용(글자·표 값·그림 속 글자). "
                 "평가 모델에는 파일 대신 name·type·content 만 보냄 (code_eval.attachments)"]},
    {"version": "semantic-items-v5", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J8-1 첨부 내용 반영(J8-1.a)·J8-2 파일 형식 읽기(J8-2.a)·J8-3 첨부 접근 처리(J8-3.a) — 세부 30·항목 44",
                 "J1-2.a 에 경계 메모: 첨부파일을 열 수 없다고 밝힌 것은 J8-3"]},
    {"version": "jev-applicability-v2.7-attachments", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J8 은 첨부파일이 있을 때 적용 (sub-applicability-v5: J8-1·J8-2 는 해당 여부를 평가 모델이 정함, J8-3 은 첨부가 있으면 보냄)"]},
    {"version": "j1-j4-j7-j8-fail-evidence-v3", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J8 FAIL 도 이유 + 원문 인용 필요 — 답변 인용이 하나 이상 맞아야 하고, 첨부 내용(attachments[].content) 인용도 근거로 셈"]},
    {"version": "criteria-registry-v8", "date": "2026-10-13", "scope": "registry", "changes": ["J8 첨부파일 활용 등록 (jev-criteria-v5·semantic-items-v5)"]},
    {"version": "multi-turn-v2", "date": "2026-10-14", "scope": "semantic",
     "changes": ["J6 멀티턴 = 턴 10개 이상이고 대화 전체(모든 턴의 질문 + 답변)가 10K 토큰 이상인 대화 (사용자 결정으로 바로잡음 — "
                 "v1 은 '한 턴이 10K 토큰 이상인 턴 10개'로 읽어 질문 생성기가 턴마다 약 1.5만 자 자료를 붙였다)",
                 "각 모델의 실제 대화로 재는 것·멀티턴이 아니면 해당 없음·길이를 모르는 턴이 있으면 확인 필요는 그대로"]},
    {"version": "criteria-registry-v9", "date": "2026-10-14", "scope": "registry", "changes": ["J6 멀티턴 길이 조건 바로잡음 (multi-turn-v2)"]},
    {"version": "criteria-registry-v10", "date": "2026-10-14", "scope": "registry",
     "changes": ["세부 기준(평가 대상) 목록을 한 곳에 고정: CODE 3 대분류 · 8 소분류 · 28 검사 → 질문 조건 37 (+ 늘 적용 3·답변 구조 7), "
                 "의미 8 기준 → 30 세부 기준 → 44 점검 항목 — 질문 생성기·질문 세트 점검·최소 질문 수 도구가 같은 목록을 쓴다"]},
    {"version": "j5-outcome-detail-v5", "date": "2026-10-13", "scope": "semantic",
     "changes": ["J5 '사실 오류 — 근거와 충돌'에 충돌 근거를 기록 (contradiction.basis: 외부 근거·사용자가 준 정보·참고 자료·답변 인용 출처) — "
                 "사용자 제공 정보와의 불일치를 외부 사실 확인 결과로 오해하지 않게 (지시서 10-13 B)",
                 "J5 '출처 접근 불가'에 서버 수집 실패 상태와 주체를 기록 (citation_access.side: 확인 환경 문제일 수 있음·구분 불가·답변 주소 문제)",
                 "판정(PASS/FAIL)·사유 코드·라벨·factual_error_confirmed 는 바꾸지 않음 — 기록용 세부 구분만"]},
    {"version": "judge-attachment-images-v1", "date": "2026-10-14", "scope": "semantic",
     "changes": ["J8 이미지 첨부: 파일을 가리키는(path·sha256) 이미지는 허용 폴더·sha256·파일 서명(형식)·크기를 확인해 평가 모델(Claude API)에도 "
                 "실제 그림을 보냄 — 답변 수집과 같은 확인 규칙(code_eval.attachment_images)",
                 "확인 실패(파일 누락·sha256 불일치·형식 오류·5MB 초과)나 이미지를 받지 않는 평가 연결이면 그림이 있어야 하는 J8 은 묻지 않고 "
                 "판정 미완료(ATTACHMENT_IMAGE_UNAVAILABLE) — 다른 기준은 글자로 그대로 평가",
                 "파일을 가리키지 않는 이미지 항목(name·type·content 만)은 예전처럼 content 로 J8 판정 (기록 content_only)",
                 "레코드 attachment_images 에 보낸 그림의 이름·형식·sha256 과 못 보낸 사유 — 그림 원본은 저장하지 않음. J8 기준 정의는 그대로"]},
]


#: 평가 기준 화면에 쓰는 짧은 적용 조건 — CODE 검사의 trigger 별
_TRIGGER_LABELS = {"all": "모든 답변", "question": "질문·앞 턴 지시에 요구가 있을 때", "answer": "답변에 그 구조가 있을 때 (없으면 대상 없음 PASS)",
                   "question_or_answer": "질문 요구 또는 답변의 해당 구조 (없으면 대상 없음 PASS)"}
#: 점검 질문 끝의 괄호 — 다른 기준과의 경계 메모로 따로 작게 보인다
_BOUNDARY_NOTE = re.compile(r"\s*\(([^()]*)\)\s*$")


def reference_details() -> Dict[str, Any]:
    """평가 기준 화면용 요약 — 판정에는 쓰지 않는다.

    code_checks: CODE 검사 단위(소분류 1-1~3-3)의 이름과 짧은 적용 조건.
    semantic: 의미 세부 기준과 점검 항목 — 점검 질문(끝 괄호는 note 로 분리)·적용 조건·PASS/FAIL 예시 설명 한 줄씩.
    긴 예시 원문(질문·답변)은 넣지 않는다 (설계안 HTML 에 있음)."""
    from .semantic_items import ITEMS, SUBS
    checks = [{"id": c["id"], "group": c["group"], "name": c["name"], "applies": _TRIGGER_LABELS.get(c["trigger"], c["applies_when"])}
              for c in _code_entries() if c["layer"] == "CODE" and re.fullmatch(r"\d-\d", c["group"] or "")]
    items = []
    for item in ITEMS:
        found = _BOUNDARY_NOTE.search(item.question)
        notes = {ex.kind: ex.note for ex in item.examples}
        items.append({"id": item.id, "sub": item.sub, "criterion": item.criterion,
                      "question": item.question[:found.start()] if found else item.question,
                      "note": found.group(1) if found else "", "applies": item.applies,
                      "pass": notes.get("PASS", ""), "fail": notes.get("FAIL", "")})
    return {"version": REGISTRY_VERSION, "code_checks": checks,
            "semantic": {"subs": [{"id": s.id, "criterion": s.criterion, "name": s.name} for s in SUBS], "items": items}}


def registry() -> Dict[str, Any]:
    from .semantic_items import describe as describe_semantic_items   # 의미 27/41 정의는 semantic_items.describe() 한 곳만 쓴다
    return {"version": REGISTRY_VERSION, "criteria": _code_entries() + _semantic_entries(), "changelog": copy.deepcopy(CHANGELOG),
            # items 파이프라인(SEMANTIC_PIPELINE=items)의 기준 8 → 세부 30 → 항목 44 정의·'판정하는 곳' 규칙·나열 규칙·reason_code
            "semantic_items": describe_semantic_items(),
            "targets": evaluation_targets(),   # 세부 기준 목록과 개수 — 질문 생성·적용 수 점검이 세는 단위
            "modifier_kinds": copy.deepcopy(MODIFIER_KINDS),
            "subsumed_kinds": [{"kind": k, "when": w, "checked_by": u, "note": n} for k, w, u, n in SUBSUMED_KINDS],
            "relations": [{"a": a, "b": b, "kind": kind, "note": note} for a, b, kind, note in RELATIONS],
            "decision_policy": copy.deepcopy(DECISION_POLICY),
            "aggregation": ("질문 최종 판정: 적용된 CODE 조건·의미 기준 중 하나라도 FAIL이면 FAIL, 모두 판정되고 FAIL이 없으면 PASS, "
                            "그 밖은 미완료(판정 미완료 기준 포함). 모델 점수 = PASS 질문 수 / 전체 질문 수이며 미완료 질문이 있으면 "
                            "점수를 확정하지 않음. 조건·기준 통과율 = PASS / (PASS + FAIL), 미완료는 분모에서 빼고 따로 표시. "
                            "같은 질문의 여러 FAIL은 질문 점수에 한 번만 반영됨. 답변 구조 검사 9개는 답변마다 조건 1개(대상 없음은 PASS)로 "
                            "세어 같은 질문이면 모델마다 CODE 조건 수가 같음 (엔진 v16.5.0)")}
