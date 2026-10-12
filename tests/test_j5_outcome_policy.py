"""J5 상세 판정 사유와 검색 불가 처리 — 사실 오류와 '확인하지 못함'을 구분한다 (모델 호출 없음)."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from code_eval.combined import combined_summary
from code_eval.config import load_aiona_config
from code_eval.engine import evaluate_batch
from code_eval.jev_client import JevCall
from code_eval.jev_eval import evaluate_jev_batch, evaluate_jev_case
from code_eval.semantic_policy import (FETCH_ERROR_CODE, SEARCH_ERROR_CODE, SEARCH_UNAVAILABLE_CODE,
                                       apply_final_decision_policy)

ANSWER = "서울의 인구는 약 935만 명입니다."


def claim(verdict, scope="external_fact", sources=None):
    return {"claim": "서울의 인구는 약 935만 명", "verdict": verdict, "reason": "fixture", "scope": scope,
            "scope_quote": "" if scope == "external_fact" else "서울 인구", "sources": sources or []}


def j5(choice, claims, basis="web"):
    return {"choice": choice, "confidence": .5, "probabilities": {} if choice == "UNVERIFIED" else {"PASS": .5, "FAIL": .5},
            "reason": "fixture", "fact_check": {"basis": basis, "claims": claims}}


class Judge:
    config = SimpleNamespace(engine="anthropic_api", to_manifest=lambda: {"engine": "anthropic_api"})

    def __init__(self, answer, verification):
        self.answer, self.verification = answer, verification

    def system_one(self, state, questions):
        answers = {cid: {"choice": "PASS", "confidence": .9, "probabilities": {"PASS": .9, "FAIL": .1}, "reason": "ok"}
                   for cid in questions}
        answers["J5"] = copy.deepcopy(self.answer)
        return JevCall("OK", body={"model": "fixture", "answers": answers, "verification": copy.deepcopy(self.verification)})


def run(answer, **verification):
    verification = {"search_mode": "live", "search_performed": True, "search_calls": 1,
                    "search_limits": {"searches": 4, "fetches": 8}, "tool_errors": [], **verification}
    record = evaluate_jev_case("q", "서울 인구 알려줘", ANSWER, Judge(answer, verification),
                               criteria=("J1", "J5"), fail_evidence_check=True)
    return record, record["decisions"]["J5"]


@pytest.mark.parametrize("answer,verification,decision,status,code", [
    (j5("PASS", [claim("PASS")]), {}, "PASS", None, "verified"),
    (j5("FAIL", [claim("FAIL")]), {}, "FAIL", None, "contradicted"),
    (j5("UNVERIFIED", [claim("UNVERIFIED")]), {}, "FAIL", None, "insufficient_evidence"),
    (j5("UNVERIFIED", [claim("UNVERIFIED")]), {"search_calls": 4, "tool_errors": ["max_uses_exceeded"]},
     "FAIL", None, "search_limit_reached"),
    (j5("UNVERIFIED", [claim("UNVERIFIED")], basis="none"), {"search_mode": "disabled", "search_performed": False, "search_calls": 0},
     None, "INCOMPLETE", "search_unavailable"),
    (j5("UNVERIFIED", [claim("UNVERIFIED")], basis="none"), {"search_mode": "unsupported", "search_performed": False},
     None, "INCOMPLETE", "search_unavailable"),
    (j5("UNVERIFIED", [claim("UNVERIFIED")], basis="none"), {"search_performed": False, "search_calls": 0,
                                                            "tool_errors": ["unavailable"]}, None, "INCOMPLETE", "search_error"),
    ({"choice": "NOT_APPLICABLE", "confidence": .9, "reason": "사실 없음", "fact_check": {"basis": "none", "claims": []}},
     {}, None, "NOT_APPLICABLE", "not_applicable"),
])
def test_j5_outcome_separates_errors_from_unchecked_claims(answer, verification, decision, status, code):
    record, decision_j5 = run(answer, **verification)
    assert decision_j5["decision"] == decision and decision_j5.get("status") == status
    assert decision_j5["j5_outcome"]["code"] == code
    assert decision_j5["j5_outcome"]["factual_error_confirmed"] is (code == "contradicted")
    if status == "INCOMPLETE":
        assert decision_j5["reason_code"] in (SEARCH_UNAVAILABLE_CODE, SEARCH_ERROR_CODE)
        assert "사실 오류 판정이 아닙니다" in decision_j5["reason"] and record["review_required"]
        assert decision_j5["policy_original_assessment"]["status"] == "UNVERIFIED"


@pytest.mark.parametrize("errors,code,outcome", [
    ({"search_errors": ["unavailable"], "fetch_errors": [], "tool_errors": ["unavailable"]}, SEARCH_ERROR_CODE, "search_error"),
    ({"search_errors": [], "fetch_errors": ["url_not_accessible"], "tool_errors": ["url_not_accessible"]},
     FETCH_ERROR_CODE, "fetch_error"),
    ({"tool_errors": ["url_not_accessible"]}, SEARCH_ERROR_CODE, "search_error"),   # 나눠 기록하지 않은 옛 결과
])
def test_fetch_tool_errors_are_named_separately_from_search_errors(errors, code, outcome):
    # 원문 열기(web_fetch) 실패만 있고 검색은 하지 않은 경우 — 판정 미완료는 그대로, 사유만 구분한다.
    record, decision_j5 = run(j5("UNVERIFIED", [claim("UNVERIFIED")], basis="none"),
                              search_performed=False, search_calls=0, **errors)
    assert decision_j5["status"] == "INCOMPLETE" and decision_j5["reason_code"] == code
    assert decision_j5["j5_outcome"]["code"] == outcome and decision_j5["j5_outcome"]["factual_error_confirmed"] is False


def test_input_only_claims_do_not_need_search_and_keep_the_evidence_policy():
    # 검색이 꺼져 있어도 사용자 제공 값의 일치 확인은 검색 없이 가능하므로 미완료로 만들지 않는다.
    record, decision_j5 = run(j5("UNVERIFIED", [claim("UNVERIFIED", scope="input_consistency")], basis="input"),
                              search_mode="disabled", search_performed=False)
    assert decision_j5["decision"] == "FAIL" and decision_j5["j5_outcome"]["code"] == "insufficient_evidence"


def test_search_unavailable_question_is_pending_not_failed():
    judge = Judge(j5("UNVERIFIED", [claim("UNVERIFIED")], basis="none"),
                  {"search_mode": "disabled", "search_performed": False, "search_calls": 0})
    cfg = load_aiona_config(); cfg.enabled = False
    batch = evaluate_batch([{"case_id": "q", "question": "서울 인구 알려줘", "answer": ANSWER}], cfg)
    batch["jev_eval"] = evaluate_jev_batch(batch, judge)
    summary = combined_summary(batch)
    assert (summary["pass"], summary["fail"], summary["pending"]) == (0, 0, 1)
    assert batch["jev_eval"]["summary"]["J5"]["incomplete"] == 1


def test_saved_records_without_search_mode_keep_the_previous_outcome():
    # Re-applying the policy to an old record (manual migration tool) must not reinterpret it.
    record = {"status": "SUCCESS", "criteria": ["J5"], "verification": {"search_performed": False},
              "decisions": {"J5": {"criterion_id": "J5", "name": "DATA 신뢰도", "decision": None, "status": "UNVERIFIED",
                                   "fact_check": {"basis": "none", "claims": []}}}}
    apply_final_decision_policy(record)
    assert record["decisions"]["J5"]["decision"] == "FAIL" and "j5_outcome" not in record["decisions"]["J5"]
