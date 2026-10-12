"""v16.2.x 까지 저장된 ``jev_eval.records[].code_completion``(LLM 보완 판정)을 화면·보고서가 읽기 위한 투영.

읽기 전용·옛 결과 호환 모듈이다. 새 평가는 공통 결과 행을 PASS/FAIL 로만 만들고(판정 불가 조건은 행 없음,
``meta.skipped_conditions``) C 기준·code_completion 을 만들지 않으므로, 새 레코드에는 ``code_completion`` 키가
없고 이 투영은 입력을 그대로 돌려준다. 저장된 옛 run(미판정 행 + code_completion SUCCESS)은 지금처럼
보완 판정(PASS/FAIL, evaluation_method LLM_COMPLETION)으로 보인다. 저장된 CODE 결과·해시는 바꾸지 않는다.
"""
from __future__ import annotations

from .engine import summarize_batch

#: 옛 결과 행(v16.2.x 이전)이 쓰던 미판정 상태값. engine 쪽이 바뀌어도 옛 JSON 은 이 고정 집합으로 읽는다.
LEGACY_UNJUDGED = frozenset({"UNSUPPORTED", "INCOMPLETE", "DEPENDENCY_FAILED", "CHECK_ERROR",
                             "LIMIT_EXCEEDED", "CONFIG_MISSING", "DISPLAY_MISSING"})


def resolved_case(case, record):
    """옛 code_completion(SUCCESS·resolutions)이 있으면 그 행을 보완 판정으로 바꾼 사본을 돌려준다. 없으면 그대로."""
    final = (record or {}).get("code_completion") or {}
    if final.get("status") != "SUCCESS" or not final.get("resolutions"):
        return case
    rows = list(case["results"])
    for resolution in final["resolutions"]:
        index = resolution["index"]
        if not 0 <= index < len(rows) or rows[index].get("condition_id") != resolution["condition_id"]:
            return case
        row = rows[index]
        if row.get("area") != "common" or row.get("verdict") is not None or row.get("status") not in LEGACY_UNJUDGED:
            # Allow an already projected report view, but never override deterministic verdicts.
            if row == resolution["result"]:
                continue
            return case
        rows[index] = resolution["result"]
    return {**case, "results": rows, "summary": final["summary"]}


def resolved_batch(batch):
    records = (batch.get("jev_eval") or {}).get("records") or []
    cases = [resolved_case(case, records[i] if i < len(records) else None) for i, case in enumerate(batch["cases"])]
    return {**batch, "cases": cases, "summary": summarize_batch(cases)}
