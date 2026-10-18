"""최종 판정 정책(semantic-hold-unverified-v3) — 근거가 부족해 확인하지 못한 기준은 FAIL 이 아니라 보류(UNVERIFIED 유지)로 두고
원래 판정을 policy_original_assessment 에 보존한다. 확인된 PASS/FAIL·입력 범위 재검증 PASS 는 그대로다."""
import copy
from types import SimpleNamespace

import pytest

from code_eval.combined import combined_summary
from code_eval.engine import evaluate_batch
from code_eval.jev_client import JevCall
from code_eval.jev_eval import evaluate_jev_batch, evaluate_jev_case, evaluation_log
from code_eval.semantic_evidence import finalize_answers
from code_eval.semantic_policy import (
    DECISION_POLICY, FINAL_DECISION_POLICY_VERSION, FINAL_EVIDENCE_FAILURE_CODE, FINAL_EVIDENCE_FAILURE_REASON,
    INPUT_SCOPE_REVALIDATED_CODE, SOURCE_ORIGINAL_FAILURE_CODE,
    apply_final_decision_policy,
)


def held(decision):
    """보류 — 판정 값 없이 UNVERIFIED 를 유지하고 근거 부족 사유 코드를 단 기준인가."""
    return (decision.get("decision") is None and decision.get("status") == "UNVERIFIED"
            and decision.get("reason_code") == FINAL_EVIDENCE_FAILURE_CODE)


SCOPE_FAILURE = "사용자 원문 인용은 확인했지만, 요청에 주어진 자료·설정의 일치를 확인하는 주장인지 불명확합니다."


def undecided(cid="J5"):
    return {"criterion_id": cid, "decision": None, "status": "UNVERIFIED", "confidence": 0.0,
            "probabilities": {}, "reason": "Could not establish evidence", "context_checks": []}


def record(cid="J5"):
    return {"case_id": "q", "status": "SUCCESS", "criteria": [cid], "decisions": {cid: undecided(cid)},
            "review_required": True}


def input_scope_record(question, answer, source_quote=None):
    value = record()
    j5 = value["decisions"]["J5"]
    j5.update(reason=SCOPE_FAILURE, model_original_assessment={"choice": "PASS", "reason": "Input and answer agree"},
              fact_check={"basis": "input", "claims": [{"claim": answer, "verdict": "UNVERIFIED",
                "reason": SCOPE_FAILURE, "scope": "input_consistency", "scope_quote": question,
                "sources": [{"origin": "question", "turn": None, "url": "", "quote": source_quote or question,
                    "input_quote_verified": True, "input_scope_verified": False, "quote_verified": False,
                    "verification_reason": SCOPE_FAILURE}]}]})
    return value


@pytest.mark.parametrize("cid", ["J1", "J2", "J3", "J4", "J5", "J6"])
def test_successful_held_judgment_stays_held_for_review_with_unchanged_original(cid):
    value = record(cid)
    before = copy.deepcopy(value["decisions"][cid])
    result = apply_final_decision_policy(value)
    assert result["changed"] and result["policy_version"] == FINAL_DECISION_POLICY_VERSION
    assert result["changes"] == [{"criterion_id": cid, "status": "UNVERIFIED", "reason_code": FINAL_EVIDENCE_FAILURE_CODE}]
    decision = value["decisions"][cid]
    assert held(decision)   # FAIL 로 바꾸지 않고 보류 — 사람이 PASS/FAIL 을 정한다
    assert decision["reason"] == FINAL_EVIDENCE_FAILURE_REASON + " · Could not establish evidence"
    assert decision["policy_original_assessment"] == before
    assert decision["confidence"] == 0.0 and decision["probabilities"] == {}
    assert decision["decision_policy"] == {"version": FINAL_DECISION_POLICY_VERSION, "outcome_basis": "held_for_review",
                                           "factual_error_confirmed": False}
    assert value["review_required"] is True and value["decision_policy"] == DECISION_POLICY
    snapshot = copy.deepcopy(value)
    assert not apply_final_decision_policy(value)["changed"]
    assert value == snapshot


@pytest.mark.parametrize("status", ["API_ERROR", "TIMEOUT", "INVALID_RESPONSE", "RUNNING", "PENDING", "SKIPPED", "NOT_CONFIGURED"])
def test_execution_failure_or_active_work_never_becomes_model_failure(status):
    value = record()
    value["status"] = status
    before = copy.deepcopy(value)
    assert not apply_final_decision_policy(value)["changed"]
    assert value == before


def test_existing_binary_outcomes_not_applicable_and_unrequested_input_error_are_preserved():
    value = record("J6")
    value["not_requested"] = ["J6"]
    value["criteria"] += ["J1", "J3"]
    value["decisions"].update(J1={"decision": "PASS", "reason": "Confirmed"},
                               J3={"decision": "FAIL", "reason": "Confirmed contradiction"},
                               J5={"decision": None, "status": "NOT_APPLICABLE", "reason": "No facts"})
    before = copy.deepcopy(value)
    assert not apply_final_decision_policy(value)["changed"]
    assert value == before


@pytest.mark.parametrize("question,answer", [
    ("처음 배부한 내역을 정정할게. 노트는 6권이 아니라 실제로는 4권을 배부했어.", "배부 4권"),
    ("품목 이름 ‘파일’을 ‘서류철’로 바꿔줘. 새로운 품목을 추가하는 게 아니라 기존 품목의 이름만 바꾸는 거야.",
     "서류철은 기존 파일 재고를 그대로 이어받았으며, **새 품목을 추가하지 않았어요.**"),
])
def test_input_gate_false_negative_is_restored_only_after_exact_original_revalidation(question, answer):
    value = input_scope_record(question, answer)
    original = copy.deepcopy(value["decisions"]["J5"])
    state = {"question": question, "answer": answer}
    snapshot = copy.deepcopy(state)
    result = apply_final_decision_policy(value, state=state)
    decision = value["decisions"]["J5"]
    assert result["changed"] and result["changes"][0]["decision"] == "PASS"
    assert decision["decision"] == "PASS" and decision["reason_code"] == INPUT_SCOPE_REVALIDATED_CODE
    assert decision["reason"] == "사용자 제공 정보와 일치"
    assert decision["policy_original_assessment"] == original
    assert decision["confidence"] is None and decision["probabilities"] == {}
    checked = decision["fact_check"]["claims"][0]["sources"][0]
    assert checked["input_quote_verified"] and checked["input_scope_verified"] and checked["quote_verified"]
    assert state == snapshot and not value["review_required"]


@pytest.mark.parametrize("violation", ["model_unknown", "wrong_source", "wrong_answer", "scope_outside_question",
                                       "another_unknown", "another_fail", "missing_history", "no_state", "different_failure",
                                       "invalid_second_source", "empty_second_source", "invalid_prior_pass_source"])
def test_input_pass_cannot_be_invented_from_a_previous_model_pass_alone(violation):
    question, answer = "처음 배부한 내역을 정정할게. 노트는 4권을 배부했어.", "배부 4권"
    value = input_scope_record(question, answer)
    state = {"question": question, "answer": answer}
    decision = value["decisions"]["J5"]
    claim = decision["fact_check"]["claims"][0]
    if violation == "model_unknown":
        decision["model_original_assessment"]["choice"] = "UNVERIFIED"
    elif violation == "wrong_source":
        claim["sources"][0]["quote"] = "노트는 400권을 배부했어."
    elif violation == "wrong_answer":
        state["answer"] = "배부 400권"
    elif violation == "scope_outside_question":
        claim["scope_quote"] = "존재하지 않는 내역을 정정할게"
    elif violation in ("another_unknown", "another_fail"):
        another = copy.deepcopy(claim)
        another.update(verdict="FAIL" if violation == "another_fail" else "UNVERIFIED", reason="Independent unresolved claim")
        decision["fact_check"]["claims"].append(another)
    elif violation == "missing_history":
        claim["sources"][0].update(origin="history_user", turn=99)
    elif violation == "no_state":
        state = None
    elif violation in ("invalid_second_source", "empty_second_source", "invalid_prior_pass_source"):
        invalid = copy.deepcopy(claim["sources"][0])
        invalid["quote"] = "" if violation == "empty_second_source" else "This exact source quote is absent from the original input."
        if violation == "invalid_prior_pass_source":
            prior_pass = copy.deepcopy(claim)
            prior_pass.update(verdict="PASS", reason="Previously accepted input claim")
            prior_pass["sources"].append(invalid)
            decision["fact_check"]["claims"].append(prior_pass)
        else:
            claim["sources"].append(invalid)
    else:
        claim["reason"] = "No confirmed original quotation"
    original = copy.deepcopy(decision)
    apply_final_decision_policy(value, state=state)
    assert held(decision) and value["review_required"] is True   # PASS 로 되살리지 않고 보류로 남는다
    assert decision["policy_original_assessment"] == original


def test_truth_check_request_cannot_use_input_edit_as_proof():
    question = "자료의 수량을 정정해줘. 노트가 실제로 4권인지 정확한지 검증해줘."
    value = input_scope_record(question, "배부 4권")
    apply_final_decision_policy(value, state={"question": question, "answer": "배부 4권"})
    assert held(value["decisions"]["J5"]) and value["review_required"] is True


def test_new_evidence_gate_keeps_valid_input_correction_as_pass_before_final_policy():
    question, answer = "처음 배부한 내역을 정정할게. 노트는 4권을 배부했어.", "배부 4권"
    source = input_scope_record(question, answer)["decisions"]["J5"]
    source.update(choice="PASS", reason="Current user input agrees")
    source["fact_check"]["claims"][0].update(verdict="PASS", reason="Current user input agrees")
    finalized = finalize_answers({"answers": {"J5": source}}, {"question": question, "answer": answer}, {"J5": {}})
    assert finalized["answers"]["J5"]["choice"] == "PASS"


def test_final_held_outcomes_reach_events_aggregates_and_log_consistently():
    """보류된 J5 는 배치 집계(unverified·pending)·결합 요약(semantic_unverified·review_required)·평가 로그에 FAIL 이 아니라 보류로 남는다."""
    answers = {"J1": {"choice": "PASS", "confidence": .8, "probabilities": {"PASS": .9, "FAIL": .1}},
               "J5": {"choice": "UNVERIFIED", "confidence": 0.0, "probabilities": {},
                      "reason": "No final evidence", "fact_check": {"basis": "none", "claims": []}}}
    judge = SimpleNamespace(config=SimpleNamespace(engine="openai_api", to_manifest=lambda: {}),
        system_one=lambda **_: JevCall("OK", body={"model": "fixture", "answers": answers}))
    batch = evaluate_batch([{"case_id": "q", "question": "Describe Alpha.", "answer": "Alpha is blue."}])
    original = copy.deepcopy(batch)
    published = []
    batch["jev_eval"] = evaluate_jev_batch(batch, judge, workers=1, on_record=lambda _, item: published.append(copy.deepcopy(item)))
    assert batch["cases"] == original["cases"] and batch["result_digest"] == original["result_digest"]
    jev = batch["jev_eval"]
    assert jev["status"] == "PARTIAL" and jev["completed"] == 0 and jev["review_required"] == jev["unverified"] == 1
    assert jev["summary"]["J5"]["fail"] == 0 and jev["summary"]["J5"]["unverified"] == jev["summary"]["J5"]["pending"] == 1
    assert held(published[0]["decisions"]["J5"]) and published[0]["review_required"] is True
    summary = combined_summary(batch)
    assert summary["pending"] == 1 and summary["fail"] == summary["pass"] == 0
    assert summary["semantic_unverified"] == summary["review_required"] == 1
    assert [d["criterion_id"] for d in summary["review_items"][0]["criteria"]] == ["J5"]
    logged = evaluation_log(batch)[0]["jev"]
    assert held(logged["decisions"]["J5"]) and logged["review_required"] is True
    assert logged["decision_policy"] == DECISION_POLICY


def test_direct_case_remains_an_error_when_provider_request_failed():
    judge = SimpleNamespace(system_one=lambda **_: JevCall("API_ERROR", error="Fixture provider failure"))
    result = evaluate_jev_case("q", "Question", "Answer", judge, criteria=("J1", "J5"))
    assert result["status"] == "API_ERROR" and result["decisions"] is None
