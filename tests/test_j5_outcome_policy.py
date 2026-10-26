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
from code_eval.semantic_policy import (FETCH_ERROR_CODE, FINAL_EVIDENCE_FAILURE_CODE, SEARCH_ERROR_CODE, SEARCH_UNAVAILABLE_CODE,
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
    # 검색은 했지만 근거가 부족한 경우 — v3 부터 FAIL 이 아니라 보류(held). 검색 한도 도달도 보류로 같이 묶인다.
    (j5("UNVERIFIED", [claim("UNVERIFIED")]), {}, None, "UNVERIFIED", "held"),
    (j5("UNVERIFIED", [claim("UNVERIFIED")]), {"search_calls": 4, "tool_errors": ["max_uses_exceeded"]},
     None, "UNVERIFIED", "held"),
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
    if status == "UNVERIFIED":   # 보류 — 사람이 PASS/FAIL 을 정한다
        assert decision_j5["reason_code"] == FINAL_EVIDENCE_FAILURE_CODE and record["review_required"]
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


def test_input_only_claims_do_not_need_search_and_are_held_not_incomplete():
    # 검색이 꺼져 있어도 사용자 제공 값의 일치 확인은 검색 없이 가능하므로 미완료(INCOMPLETE)로 만들지 않고 보류로 둔다.
    record, decision_j5 = run(j5("UNVERIFIED", [claim("UNVERIFIED", scope="input_consistency")], basis="input"),
                              search_mode="disabled", search_performed=False)
    assert decision_j5["decision"] is None and decision_j5["status"] == "UNVERIFIED"
    assert decision_j5["reason_code"] == FINAL_EVIDENCE_FAILURE_CODE and decision_j5["j5_outcome"]["code"] == "held"


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
    j5_saved = record["decisions"]["J5"]
    assert j5_saved["decision"] is None and j5_saved["status"] == "UNVERIFIED"   # 보류 — 미완료(INCOMPLETE)로 재해석하지 않음
    assert j5_saved["reason_code"] == FINAL_EVIDENCE_FAILURE_CODE and "j5_outcome" not in j5_saved


# --- 지시서 10-13 B-4: 다섯 원인을 따로 시험 — 판정은 그대로, 사실 오류 확정과 검증 한계를 구분하는 기록만 본다 -------------

def _fail_record(**j5_fields):
    return {"status": "SUCCESS", "criteria": ["J5"], "verification": {"search_mode": "live", "search_performed": True},
            "decisions": {"J5": {"criterion_id": "J5", "decision": "FAIL", **j5_fields}}}


def _fail_claim(origin, quote="정원은 12명이야"):
    return {"claim": "정원은 15명", "verdict": "FAIL", "scope": "input_consistency" if origin != "web" else "external_fact",
            "sources": [{"origin": origin, "quote": quote, "url": "https://example.org/a" if origin == "web" else ""}]}


def test_contradiction_records_whether_the_evidence_was_the_web_or_the_users_own_input():
    from code_eval.semantic_policy import j5_outcome
    external = j5_outcome(_fail_record(fact_check={"basis": "web", "claims": [_fail_claim("web")]}))
    assert external["code"] == "contradicted" and external["factual_error_confirmed"] is True
    assert external["contradiction"]["basis"] == "external" and "외부" in external["contradiction"]["label"]
    user = j5_outcome(_fail_record(fact_check={"basis": "input", "claims": [_fail_claim("question")]}))
    assert user["code"] == "contradicted" and user["contradiction"]["basis"] == "user_input"
    assert "사용자" in user["contradiction"]["label"] and "외부 사실 확인 결과가 아님" in user["contradiction"]["label"]
    earlier = j5_outcome(_fail_record(fact_check={"basis": "mixed", "claims": [_fail_claim("history_user"), _fail_claim("web")]}))
    assert earlier["contradiction"]["basis"] == "mixed" and earlier["contradiction"]["bases"] == ["external", "user_input"]
    cited = j5_outcome(_fail_record(citation_mismatch=True, fact_check={"basis": "web", "claims": [_fail_claim("web")]},
                                    citation_checks=[{"url": "https://example.org/a", "verdict": "contradicted"}]))
    assert cited["contradiction"]["basis"] == "external"


@pytest.mark.parametrize("statuses,side", [
    (["timeout"], "evaluation_environment"), (["dns_timeout", "http_error"], "evaluation_environment"),
    (["http_error"], "unclear"), (["dns_error"], "unclear"), (["blocked_address"], "answer_citation"),
])
def test_page_access_failure_keeps_fail_but_records_who_could_not_open_the_page(statuses, side):
    from code_eval.semantic_policy import j5_outcome
    checks = [{"url": f"https://example.org/{n}", "verdict": "inaccessible", "server_status": s} for n, s in enumerate(statuses)]
    outcome = j5_outcome(_fail_record(citation_mismatch=True, citation_inaccessible=True, citation_checks=checks,
                                      fact_check={"basis": "web", "claims": []}))
    assert outcome["code"] == "citation_inaccessible" and outcome["factual_error_confirmed"] is False
    access = outcome["citation_access"]
    assert access["side"] == side and access["statuses"] == {s: statuses.count(s) for s in statuses}
    if side == "evaluation_environment":
        assert "답변 결함으로 단정하지 않" in access["label"]


def test_five_causes_are_told_apart_end_to_end():
    """검색 미지원 · 웹 페이지 접근 실패 · 검색 한도(보류) · 실제 틀린 사실 · 사용자 제공 정보 불일치 — 사실 오류 확정은 마지막 둘뿐."""
    from code_eval.semantic_policy import j5_outcome
    _, no_search = run(j5("UNVERIFIED", [claim("UNVERIFIED")], basis="none"), search_mode="unsupported", search_performed=False)
    source = _fail_record(reason_code="SOURCE_ORIGINAL_UNAVAILABLE", decision_policy={"failures": [
        {"status": "timeout", "url": "https://example.org/x", "claim_index": 0, "source_index": 0}]})   # v2 때 저장된 FAIL 레코드
    _, limit = run(j5("UNVERIFIED", [claim("UNVERIFIED")]), search_calls=4, tool_errors=["max_uses_exceeded"])
    wrong = j5_outcome(_fail_record(fact_check={"basis": "web", "claims": [_fail_claim("web")]}))
    mismatch = j5_outcome(_fail_record(fact_check={"basis": "input", "claims": [_fail_claim("question")]}))
    got = [no_search["j5_outcome"]["code"], j5_outcome(source)["code"], limit["j5_outcome"]["code"], wrong["code"], mismatch["code"]]
    assert got == ["search_unavailable", "source_unavailable", "held", "contradicted", "contradicted"]
    assert no_search["decision"] is None and no_search["status"] == "INCOMPLETE"          # 검색 미지원은 FAIL 이 아님
    assert limit["decision"] is None and limit["status"] == "UNVERIFIED"                  # 검색 한도 도달은 보류 (v3)
    assert [o["factual_error_confirmed"] for o in (no_search["j5_outcome"], j5_outcome(source), limit["j5_outcome"])] == [False] * 3
    assert (wrong["contradiction"]["basis"], mismatch["contradiction"]["basis"]) == ("external", "user_input")
    assert j5_outcome(source)["source_failure"]["kind"] == "access_failed"


def _items_record(decision, *items):
    subs = {}
    for item in items:
        subs.setdefault(item["id"].split(".", 1)[0], {"items": []})["items"].append(item)
    return {"status": "SUCCESS", "pipeline": "items", "criteria": ["J5"],
            "decisions": {"J5": {"criterion_id": "J5", "decision": decision, "reason_code": next(
                (i.get("reason_code") for i in items if i.get("reason_code")), None), "subs": subs}}}


@pytest.mark.parametrize("items,code,basis,confirmed", [
    ([{"id": "J5-1.a#1", "decision": "NO", "reason_code": "FACT_FALSE_CONFIRMED"}], "contradicted", "external", True),
    ([{"id": "J5-2.a#1", "decision": "NO"}], "contradicted", "user_input", True),
    ([{"id": "J5-1.a#1", "decision": "NO", "unverifiable": True, "reason_code": "FACT_UNVERIFIABLE"}], "insufficient_evidence", None, False),
    ([{"id": "J5-3.a", "decision": "NO", "reason_code": "SOURCE_NOT_FOUND"}], "citation_inaccessible", None, False),
    ([{"id": "J5-3.a", "decision": "NO"}], "process_misrepresented", None, False),
])
def test_items_j5_fail_gets_the_same_outcome_shape_without_changing_the_verdict(items, code, basis, confirmed):
    from code_eval.semantic_policy import items_j5_outcome
    record = _items_record("FAIL", *items)
    outcome = items_j5_outcome(record)
    assert (outcome["code"], outcome["factual_error_confirmed"]) == (code, confirmed)
    assert (outcome.get("contradiction") or {}).get("basis") == basis
    assert record["decisions"]["J5"]["decision"] == "FAIL"
    assert items_j5_outcome(_items_record("PASS", {"id": "J5-2.a#1", "decision": "YES"}))["code"] == "verified"
    assert items_j5_outcome({"decisions": {}}) is None


