"""실행 요약 — 이력 관리용 한 실행의 조건·결과 요약 (설계 '이력 관리' 표의 '실행 요약').

날짜, 질문 세트 지문, 모델별 이름·모델 ID·AIONA 버전, 기준별·세부 기준별 통과율과 충족률, 평가 방식·호출 방식·평가 모델을
저장된 실행 기록에서만 읽는다(현재 설정을 소급하지 않는다). 판정 값은 바꾸지 않는다.
"""
from __future__ import annotations

import functools
from typing import Any, Dict, List, Optional

from code_eval.common_adjudication import resolved_case
from code_eval.extract import extract_conditions


__all__ = [ "evaluation_method", "EMPTY_OUTPUT_HOLD_POLICY", "asks_empty_output", "unit_verdict", "question_outcome", "question_outcomes", "unit_outcome",]


#: 빈 출력을 요구한 질문('아무것도 출력하지 마' 등, CODE 2-1.explicit_empty)은 판정·수집 결과와 상관없이 우선 보류(PENDING) —
#: 사용자 결정 2026-10-19. AIONA 는 이 질문에 오류 화면을 내 답을 못 받고, 빈 답을 PASS 로 볼지도 정하지 않았다.
#: v2: 원래 평가(CODE·의미 PASS/FAIL)도 보이지 않고 세지 않는다 — 질문 판정·기준별 통과율 모두 보류 하나로 (사용자 결정 2026-10-19)
EMPTY_OUTPUT_HOLD_POLICY = "empty-output-hold-v2"


def asks_empty_output(case: Optional[Dict[str, Any]] = None, question: Optional[str] = None) -> bool:
    """이 질문(턴)이 빈 출력을 요구했는가 — 평가 결과(2-1.explicit_empty 행·질문 조건)가 있으면 그것으로, 없으면(수집 실패로 입력 오류가 된 행)
    질문 원문을 평가와 같은 조건 추출(``extract_conditions``, 붙여 넣은 자료 구간은 가림)로 본다."""
    if isinstance(case, dict):
        if any(str(row.get("check_id") or "").startswith("2-1.explicit_empty") for row in case.get("results") or [] if isinstance(row, dict)):
            return True
        if any(isinstance(c, dict) and c.get("kind") == "explicit_empty" for c in (case.get("meta") or {}).get("question_conditions") or []):
            return True
    return bool(isinstance(question, str) and question.strip() and _question_asks_empty(question))


@functools.lru_cache(maxsize=4096)
def _question_asks_empty(question: str) -> bool:   # 같은 질문을 모델마다·화면 갱신마다 다시 읽지 않게
    return bool(extract_conditions(question).first("explicit_empty"))


def _held_empty(base: Dict[str, Any]) -> Dict[str, Any]:
    """보류 — 원래 평가는 남기지 않는다(FAIL 목록·완료 여부도 비움)."""
    return {"outcome": "PENDING", "code_fails": [], "semantic_fails": [], "code_complete": False, "semantic_complete": False,
            "hold": "empty_output", "hold_policy": EMPTY_OUTPUT_HOLD_POLICY}


def question_outcome(case: Optional[Dict[str, Any]], record: Optional[Dict[str, Any]], question: Optional[str] = None) -> Dict[str, Any]:
    """질문 하나의 최종 판정 — {"outcome": PASS|FAIL|PENDING|EXCLUDED, "code_fails": [check_id…], "semantic_fails": [cid…],
    "code_complete", "semantic_complete"}. 공통 CODE FAIL 이나 의미 기준 FAIL 이 하나라도 확정되면 FAIL, 모두 판정되고 FAIL 이 없으면 PASS.
    빈 출력을 요구한 질문은 우선 보류 — outcome PENDING, hold "empty_output", 원래 평가는 남기지 않음 (``EMPTY_OUTPUT_HOLD_POLICY``).
    ``question`` 은 평가 결과가 없는 행(수집 실패)에서 빈 출력 요구를 알아보는 데만 쓴다."""
    out = _judged_outcome(case, record)
    return _held_empty(out) if asks_empty_output(case, question) else out


def _judged_outcome(case: Optional[Dict[str, Any]], record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(case, dict) or case.get("case_status") != "EVALUATED":
        return {"outcome": "EXCLUDED", "code_fails": [], "semantic_fails": [], "code_complete": False, "semantic_complete": False}
    record = record if isinstance(record, dict) else {}
    try:
        case = resolved_case(case, record)   # 옛 결과의 code_completion 투영 (새 결과는 그대로)
    except (KeyError, TypeError, IndexError):
        pass
    common = (case.get("summary") or {}).get("common") or {}
    code_fails = [row.get("check_id") or row.get("condition_id") for row in case.get("results") or []
                  if row.get("area") == "common" and row.get("counted") and row.get("verdict") == "FAIL"]
    code_complete = bool(common.get("unjudged") == 0 and not case.get("errors") and (common.get("pass", 0) + common.get("fail", 0)) > 0)
    decisions = record.get("decisions") or {}
    criteria = record.get("criteria") if isinstance(record.get("criteria"), list) else list(decisions)
    semantic_fails = [cid for cid in criteria if (decisions.get(cid) or {}).get("decision") == "FAIL"]
    semantic_complete = (record.get("status") == "SUCCESS" and bool(criteria)
                         and all((decisions.get(cid) or {}).get("decision") in ("PASS", "FAIL") for cid in criteria))
    if code_fails or semantic_fails or common.get("fail", 0) > 0:
        outcome = "FAIL"
    elif code_complete and semantic_complete:
        outcome = "PASS"
    else:
        outcome = "PENDING"
    return {"outcome": outcome, "code_fails": [c for c in code_fails if c], "semantic_fails": semantic_fails,
            "code_complete": code_complete, "semantic_complete": semantic_complete}


def question_outcomes(batch: Optional[Dict[str, Any]], total: Optional[int] = None,
                      rows: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    """배치(provider 하나)의 질문별 최종 판정 목록 (index 순). 결과가 없는 index 는 PENDING.
    ``rows`` 는 그 provider 의 입력 행(run["datasets"][provider]) — 평가 결과가 없는 행의 빈 출력 요구를 질문 원문으로 알아본다."""
    batch = batch if isinstance(batch, dict) else {}
    cases = batch.get("cases") or []
    records = (batch.get("jev_eval") or {}).get("records") or []
    count = total if isinstance(total, int) and total > 0 else len(cases)
    out = []
    for index in range(count):
        case = cases[index] if index < len(cases) else None
        record = records[index] if index < len(records) else None
        row = rows[index] if rows and index < len(rows) and isinstance(rows[index], dict) else {}
        if case is None:
            pending = {"outcome": "PENDING", "code_fails": [], "semantic_fails": [], "code_complete": False, "semantic_complete": False}
            out.append(_held_empty(pending) if asks_empty_output(None, row.get("question")) else pending)
        else:
            out.append(question_outcome(case, record, row.get("question")))
    return out


def unit_outcome(outcomes: List[str]) -> str:
    """질문 단위의 판정 — 한 턴이라도 FAIL 이면 FAIL, FAIL 없이 미완료 턴이 있으면 PENDING, 제외 턴이 있으면 EXCLUDED, 모두 PASS 면 PASS."""
    for kind in ("FAIL", "PENDING", "EXCLUDED"):
        if kind in outcomes:
            return kind
    return "PASS" if outcomes else "PENDING"


def unit_verdict(outcomes: List[Dict[str, Any]]) -> str:
    """질문 단위의 판정(턴 판정 dict 목록) — 빈 출력 요구 턴이 있으면 단위 전체를 보류(PENDING, 대화의 다른 턴 FAIL 이 있어도),
    아니면 ``unit_outcome`` (사용자 결정 2026-10-19 '빈 출력을 요구한 질문은 전부 보류')."""
    if any(o.get("hold") == "empty_output" for o in outcomes):
        return "PENDING"
    return unit_outcome([o["outcome"] for o in outcomes])


def _only(values: List[Any]) -> Any:
    """모두 같은 값이면 그 값, 서로 다르면 'mixed', 없으면 None."""
    found = {v for v in values if v is not None}
    return next(iter(found)) if len(found) == 1 else "mixed" if found else None


def evaluation_method(run: Dict[str, Any]) -> Dict[str, Any]:
    """저장된 실행의 의미 평가 방식·기준 버전·실제 판정 수 — 결과 화면(API)·평가 로그·검증 도구가 같은 계산을 쓴다 (현재 설정 소급 없음).

    pipeline_source: evaluation_execution(실행 시작 때 기록) · jev_eval(평가 배치 기록) · inferred(방식 기록이 생기기 전 실행 → legacy).
    catalog 는 저장된 기준 목록 수와, items 판의 세부 기준·점검 항목 수(저장된 items_version 이 현재 코드와 같을 때만).
    judged 는 저장된 레코드에서 센 실제 수 — answers(판정 완료 답변) · criteria(PASS/FAIL 로 확정된 기준 판정) · items(적용된 점검 항목, items 만).
    """
    from code_eval.semantic_items import ITEMS, SEMANTIC_ITEMS_VERSION, SUBS
    batches = [b["jev_eval"] for b in (run.get("evaluations") or {}).values()
               if isinstance(b, dict) and not b.get("live") and isinstance(b.get("jev_eval"), dict)]
    records = [r for jev in batches for r in jev.get("records") or [] if isinstance(r, dict)]
    recorded = (run.get("evaluation_execution") or {}).get("pipeline")
    batch_pipeline = _only([jev.get("pipeline") for jev in batches])
    if recorded in ("legacy", "items"):
        pipeline, source = recorded, "evaluation_execution"
    elif batch_pipeline in ("legacy", "items"):
        pipeline, source = batch_pipeline, "jev_eval"
    elif records and batch_pipeline is None and not any(r.get("pipeline") == "items" for r in records):
        pipeline, source = "legacy", "inferred"
    else:
        pipeline, source = None, None
    items_version = _only([jev.get("items_version") for jev in batches])
    catalog_criteria = _only([len(jev["criteria"]) for jev in batches if isinstance(jev.get("criteria"), list)])
    current_items = pipeline == "items" and items_version == SEMANTIC_ITEMS_VERSION
    judged_criteria = sum(1 for r in records for cid in (r.get("criteria") if isinstance(r.get("criteria"), list) else [])
                          if ((r.get("decisions") or {}).get(cid) or {}).get("decision") in ("PASS", "FAIL"))
    applied = [((r.get("items") or {}).get("applied")) for r in records]
    return {"pipeline": pipeline, "pipeline_source": source, "criteria_version": _only([jev.get("criteria_version") for jev in batches]),
            "items_version": items_version,
            "catalog": {"criteria": catalog_criteria, "subs": len(SUBS) if current_items else None, "items": len(ITEMS) if current_items else None},
            "judged": {"answers": sum(1 for r in records if r.get("status") == "SUCCESS"), "criteria": judged_criteria,
                       "items": sum(a for a in applied if isinstance(a, int)) if pipeline == "items" else None}}


