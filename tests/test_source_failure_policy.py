"""Final J5 source policy, using fake judges and HTTP transports only."""
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from code_eval.combined import combined_summary
from code_eval.engine import evaluate_batch
from code_eval.jev_client import JevCall
from code_eval.jev_eval import evaluate_jev_batch, evaluate_jev_case, evaluation_log
from code_eval.jev_criteria import describe, jev_questions
from code_eval.semantic_api_client import SemanticApiClient, SemanticApiConfig
from code_eval.semantic_policy import (
    DECISION_POLICY, SOURCE_DECISION_POLICY, SOURCE_ORIGINAL_FAILURE_CODE, SOURCE_ORIGINAL_FAILURE_REASON,
    SOURCE_ORIGINAL_FAILURE_STATUSES, apply_source_original_failure_policy,
)
from code_eval.semantic_sources import _REASONS, verify_web_sources


URL = "https://official.example/facts"
ALTERNATIVE = "https://official.example/alternative"


def record(status="http_error", *, legacy=False):
    source = {"origin": "web", "url": URL, "title": "Source", "quote": "Alpha is blue.",
              "quote_verified": False, "verification_reason": _REASONS[status],
              "verification_status": status, "authority": "primary",
              "authority_reason": "Official definition", "temporal_relevance": "Current"}
    if legacy:
        source.pop("verification_status")
    return {"case_id": "q", "status": "SUCCESS", "criteria": ["J1", "J5"],
            "review_required": True, "verification": {"source_validation": "provider_and_content",
                "source_urls": [URL], "search_performed": True,
                "source_checks": [{"url": URL, "status": status}]},
            "decisions": {"J1": {"decision": "PASS", "confidence": .8, "reason": "Relevant answer"},
                "J5": {"criterion_id": "J5", "decision": None, "status": "UNVERIFIED", "confidence": 0.0,
                       "probabilities": {}, "reason": "정보 신뢰도 확인 필요: " + _REASONS[status],
                       "model_original_assessment": {"choice": "PASS", "reason": "Original judgment"},
                       "fact_check": {"basis": "web", "claims": [{"claim": "Alpha is blue.",
                            "verdict": "UNVERIFIED", "reason": _REASONS[status],
                            "scope": "external_fact", "scope_quote": "", "sources": [source]}]}}}}


@pytest.mark.parametrize("status", sorted(SOURCE_ORIGINAL_FAILURE_STATUSES))
@pytest.mark.parametrize("legacy", [False, True])
def test_unavailable_original_is_final_fail_and_preserves_entire_original(status, legacy):
    value = record(status, legacy=legacy)
    before = copy.deepcopy(value)
    result = apply_source_original_failure_policy(value)
    assert result["changed"] and result["failures"][0]["status"] == status
    j5 = value["decisions"]["J5"]
    assert j5["decision"] == "FAIL" and "status" not in j5
    assert j5["reason"] == SOURCE_ORIGINAL_FAILURE_REASON
    assert j5["reason_code"] == SOURCE_ORIGINAL_FAILURE_CODE
    assert j5["policy_original_assessment"] == before["decisions"]["J5"]
    assert j5["confidence"] == 0.0 and j5["probabilities"] == {}
    assert j5["fact_check"] == before["decisions"]["J5"]["fact_check"]
    assert j5["decision_policy"]["factual_error_confirmed"] is False
    assert value["verification"] == before["verification"]
    assert value["decisions"]["J1"] == before["decisions"]["J1"]
    assert value["decision_policy"] == SOURCE_DECISION_POLICY and value["review_required"] is False
    repeated = copy.deepcopy(value)
    assert not apply_source_original_failure_policy(value)["changed"]
    assert value == repeated


@pytest.mark.parametrize("status", ["source_limit", "dns_busy", "not_observed", "empty_quote"])
def test_never_attempted_or_missing_source_is_not_original_retrieval_failure(status):
    value = record(status)
    before = copy.deepcopy(value)
    assert not apply_source_original_failure_policy(value)["changed"]
    assert value == before


@pytest.mark.parametrize("status", ["API_ERROR", "TIMEOUT", "INVALID_RESPONSE", "SKIPPED"])
def test_whole_request_error_is_not_quality_failure(status):
    value = record()
    value["status"] = status
    before = copy.deepcopy(value)
    assert not apply_source_original_failure_policy(value)["changed"]
    assert value == before


@pytest.mark.parametrize("mode", ["input", "input_scope", "reference", "verified", "no_provenance",
                                 "model_reason", "explicit_other_status", "recovered_pass", "confirmed_fail"])
def test_unrelated_uncertainty_or_recovered_evidence_is_unchanged(mode):
    value = record(legacy=True)
    j5 = value["decisions"]["J5"]
    fact = j5["fact_check"]
    claim = fact["claims"][0]
    source = claim["sources"][0]
    if mode == "input":
        fact["basis"] = "input"
        source["origin"] = "question"
    elif mode == "input_scope":
        fact["basis"] = "mixed"
        claim["scope"] = "input_consistency"
    elif mode == "reference":
        fact["basis"] = "reference"
    elif mode == "verified":
        source["quote_verified"] = True
    elif mode == "no_provenance":
        value["verification"].pop("source_validation")
    elif mode == "model_reason":
        source["verification_reason"] = "The model says it could not verify the original source."
    elif mode == "explicit_other_status":
        source["verification_status"] = "not_observed"
    else:
        j5.pop("status")
        j5["decision"] = "PASS" if mode == "recovered_pass" else "FAIL"
    before = copy.deepcopy(value)
    assert not apply_source_original_failure_policy(value)["changed"]
    assert value == before


def test_another_unresolved_criterion_still_requires_review():
    value = record()
    value["decisions"]["J6"] = {"status": "UNVERIFIED", "decision": None, "reason": "Missing prior turn"}
    value["criteria"].append("J6")
    original = copy.deepcopy(value["decisions"]["J6"])
    assert apply_source_original_failure_policy(value)["changed"]
    assert value["review_required"] and value["decisions"]["J6"] == original


@pytest.mark.parametrize("origin", ["web", "reference"])
def test_unavailable_optional_link_does_not_negate_an_adequate_original_source(origin):
    value = record()
    fact = value["decisions"]["J5"]["fact_check"]
    fact["basis"] = "mixed"
    alternative = copy.deepcopy(fact["claims"][0]["sources"][0])
    alternative.update(url=ALTERNATIVE, origin=origin, quote_verified=True,
                       verification_status="verified", verification_reason="Verified")
    fact["claims"][0]["sources"].append(alternative)
    before = copy.deepcopy(value)
    assert not apply_source_original_failure_policy(value)["changed"]
    assert value == before


def test_retriever_writes_status_and_overrides_model_supplied_flags():
    initial = record()
    j5 = initial["decisions"]["J5"]
    source = j5["fact_check"]["claims"][0]["sources"][0]
    source.update(verification_status="verified", quote_verified=True)
    body = {"answers": {"J5": j5}, "verification": initial["verification"]}
    result = verify_web_sources(body, fetcher=lambda _: {"status": "http_error"})
    checked = result["answers"]["J5"]["fact_check"]["claims"][0]["sources"][0]
    assert checked["verification_status"] == "http_error" and checked["quote_verified"] is False
    assert source["verification_status"] == "verified"


def test_policy_runs_before_summary_log_and_progress_event_publication():
    original = record()
    j5 = original["decisions"]["J5"]
    answers = {"J1": {"choice": "PASS", "confidence": .8, "probabilities": {"PASS": .9, "FAIL": .1}},
               "J5": {**j5, "choice": "UNVERIFIED"}}
    judge = SimpleNamespace(config=SimpleNamespace(engine="openai_api", to_manifest=lambda: {}),
        system_one=lambda **_: JevCall("OK", body={"model": "fixture", "answers": answers,
            "verification": original["verification"]}))
    batch = evaluate_batch([{"case_id": "q", "question": "Describe Alpha.", "answer": "Alpha is blue."}])
    source = copy.deepcopy(batch)
    published = []
    batch["jev_eval"] = evaluate_jev_batch(batch, judge, workers=1,
                                          on_record=lambda _, result: published.append(copy.deepcopy(result)))
    assert batch["cases"] == source["cases"] and batch["result_digest"] == source["result_digest"]
    jev = batch["jev_eval"]
    assert jev["status"] == "SUCCESS" and jev["completed"] == 1
    assert jev["review_required"] == jev["unverified"] == 0
    assert jev["decision_policy"] == DECISION_POLICY
    assert jev["summary"]["J5"]["fail"] == 1 and jev["summary"]["J5"]["pending"] == 0
    assert published[0]["decisions"]["J5"]["decision"] == "FAIL"
    combined = combined_summary(batch)
    assert combined["fail"] == 1 and combined["pending"] == combined["pass"] == 0
    assert evaluation_log(batch)[0]["jev"]["decisions"]["J5"]["reason_code"] == SOURCE_ORIGINAL_FAILURE_CODE


def test_criteria_display_describes_final_policy_without_suppressing_evidence_recovery():
    displayed = next(item for item in describe()["criteria"] if item["id"] == "J5")
    assert "추가 검증 후에도" in displayed["fail"] and "검증 기준 미충족" in displayed["fail"]
    assert "UNVERIFIED" in jev_questions()["J5"]["criteria"]["FAIL"]


@pytest.mark.parametrize("recovered", [False, True])
def test_policy_waits_for_real_transport_recovery_and_retains_recovery_audit(recovered):
    sent = []
    def provider(request):
        sent.append(json.loads(request.content))
        url = ALTERNATIVE if recovered and len(sent) == 2 else URL
        ids = ("J5",) if len(sent) == 2 else ("J1", "J5")
        answers = {cid: {"choice": "PASS", "confidence": .8, "reason": "Verified"} for cid in ids}
        answers["J5"]["fact_check"] = copy.deepcopy(record()["decisions"]["J5"]["fact_check"])
        claim = answers["J5"]["fact_check"]["claims"][0]
        claim.update(verdict="PASS", reason="Verified")
        claim["sources"][0]["url"] = url
        return httpx.Response(200, json={"status": "completed", "model": "fixture-model", "output": [
            {"type": "web_search_call", "status": "completed", "action": {"type": "search", "sources": [{"url": url}]}},
            {"type": "message", "status": "completed", "content": [{"type": "output_text", "text": json.dumps(answers)}]}]})
    client = SemanticApiClient(SemanticApiConfig(), "fixture", transport=httpx.MockTransport(provider),
        source_fetcher=lambda url: {"status": "ok", "text": "Alpha is blue.", "final_url": url}
        if url == ALTERNATIVE else {"status": "http_error"})
    final = evaluate_jev_case("q", "Describe Alpha.", "Alpha is blue.", client, criteria=("J1", "J5"))
    assert len(sent) == 2 and final["attempts"] == 2
    assert final["decisions"]["J5"]["decision"] == ("PASS" if recovered else "FAIL")
    assert not final["review_required"]
    recovery = final["verification"]["evidence_recovery"]
    assert recovery["initial_choice"] == "UNVERIFIED"
    assert recovery["status"] == ("RESOLVED" if recovered else "UNRESOLVED")
    if not recovered:
        assert final["decisions"]["J5"]["policy_original_assessment"]["status"] == "UNVERIFIED"
        assert recovery["final_choice"] == "UNVERIFIED"
