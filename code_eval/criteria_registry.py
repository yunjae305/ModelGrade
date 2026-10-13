"""통합 평가기준 목록 — CODE 검사 단위와 J1~J6 의미 기준을 한 목록으로 묶는다. 판정에는 쓰지 않는다.

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
REGISTRY_VERSION = "criteria-registry-v4"

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

_TRIGGER = {"all": "모든 답변", "question": "질문 또는 대화 앞 턴의 지속 지시(앞으로·계속·모든 답변…)에 명시 요구가 있을 때", "answer": "답변에 해당 구조가 있을 때",
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
                  "실행 오류로 기록(품질 FAIL 아님, 그 질문은 PASS 로 확정하지 않음)")
_SEMANTIC_EVIDENCE = {
    "J5": ("fact_check.claims[] — 답변 원문 발췌 주장별 verdict·reason·sources(url·quote·authority·temporal_relevance·origin). "
           "서버가 검색 도구 기록의 URL, 출처 원문·입력 원문의 인용 일치를 확인하고 맞지 않으면 그 주장을 확인 필요로 바꿈. "
           "서버가 접근 문제로 받지 못한 출처는 같은 요청에서 평가자 web_fetch 가 실제로 받은 원문으로만 대조(retrieval=provider_document). "
           "답변이 직접 든 출처는 citation_checks — 서버가 그 URL 을 받아 인용·수치를 대조하고 확정된 불일치만 FAIL. "
           "판정마다 j5_outcome(사실 오류·검증 기준 미충족·출처 불일치·검색 불가 등)"),
    "J6": ("context_checks[] — 이전 턴 번호·instruction_quote·answer_quote·decision·reason. "
           "서버가 두 인용문을 이전 턴·현재 답변 원문과 대조하고 맞지 않으면 확인 필요로 바꿈"),
}
_SEMANTIC_REASON = ("reason — FAIL 이면 서버가 이유의 존재와 질문·답변 원문 인용 일치를 형식 검증하고, 근거가 없으면 "
                    "그 기준만 1회 재평가 (j1-j4-fail-evidence-v1). 이유의 의미적 타당성은 검증하지 않음")
_SEMANTIC_UNJUDGED = ("INCOMPLETE(판정 미완료) — 재평가 뒤에도 근거 없는 J1~J4 FAIL(JUDGE_FAIL_EVIDENCE_MISSING), "
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
]


def registry() -> Dict[str, Any]:
    from .semantic_items import describe as describe_semantic_items   # 의미 25/39 정의는 semantic_items.describe() 한 곳만 쓴다
    return {"version": REGISTRY_VERSION, "criteria": _code_entries() + _semantic_entries(), "changelog": copy.deepcopy(CHANGELOG),
            # items 파이프라인(SEMANTIC_PIPELINE=items)의 기준 6 → 세부 25 → 항목 39 정의·'판정하는 곳' 규칙·나열 규칙·reason_code
            "semantic_items": describe_semantic_items(),
            "modifier_kinds": copy.deepcopy(MODIFIER_KINDS),
            "subsumed_kinds": [{"kind": k, "when": w, "checked_by": u, "note": n} for k, w, u, n in SUBSUMED_KINDS],
            "relations": [{"a": a, "b": b, "kind": kind, "note": note} for a, b, kind, note in RELATIONS],
            "decision_policy": copy.deepcopy(DECISION_POLICY),
            "aggregation": ("질문 최종 판정: 적용된 CODE 조건·의미 기준 중 하나라도 FAIL이면 FAIL, 모두 판정되고 FAIL이 없으면 PASS, "
                            "그 밖은 미완료(판정 미완료 기준 포함). 모델 점수 = PASS 질문 수 / 전체 질문 수이며 미완료 질문이 있으면 "
                            "점수를 확정하지 않음. 조건·기준 통과율 = PASS / (PASS + FAIL), 미완료는 분모에서 빼고 따로 표시. "
                            "같은 질문의 여러 FAIL은 질문 점수에 한 번만 반영됨")}
