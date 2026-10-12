"""Evidence-v2 decisions after independent source checks; no network/model calls.

Publisher authority and temporal relevance remain model assessments. These tests
check required evidence and conservative aggregation, not proof of factual truth.
"""
import copy
import json

import pytest

from code_eval import semantic_sources
from code_eval.codex_cli_client import _verification as cli_verification
from code_eval.jev_eval import parse_decisions
from code_eval.semantic_api_client import _verification as api_verification
from code_eval.semantic_cli_common import jev_answer
from code_eval.semantic_evidence import finalize_answers

URL = "https://official.example/observatory/history"
STAMP = "2026-10-09T03:00:00+00:00"
FIRST = "The fixture observatory opened in 1960."
SECOND = "Its first public visit was in 1962."
FIRST_QUOTE = "The observatory opened its doors in 1960."
SECOND_QUOTE = "The first public visit took place in 1962."
STATE = {"question": "  Give the opening and first public visit dates as of 2026-10-09.\r\n",
         "answer": f"  {FIRST}\r\n{SECOND}\n  ",
         "history": [{"turn": 1, "question": "Keep the reference date.", "answer": "2026-10-09"}],
         "reference": None}


@pytest.fixture(autouse=True)
def forbid_real_fetch(monkeypatch):
    monkeypatch.setattr(semantic_sources, "fetch_public_source", lambda *a, **k: pytest.fail("real fetch forbidden"))


def source(quote=FIRST_QUOTE, **overrides):
    return {"url": URL, "title": "Fixture observatory institutional history", "quote": quote,
            "authority": "primary", "authority_reason": "The institution publishes its own history.",
            "temporal_relevance": "The historical dates apply to the requested 2026-10-09 reference date.",
            **overrides}


def claim(text=FIRST, *, verdict="PASS", reason="The quoted history supports the stated date.", sources=None):
    return {"claim": text, "verdict": verdict, "reason": reason,
            "sources": [source()] if sources is None else sources}


def body(claims=None, *, choice="PASS", searched=True, observed=None, verification=None, model="fixture-judge"):
    return {"model": model, "answers": {
        "J1": jev_answer({"choice": "FAIL", "confidence": 0.7, "reason": "A separate requested detail is missing."}),
        "J5": jev_answer({"choice": choice, "confidence": 0.9, "reason": "Model's complete factual assessment.",
                          "fact_check": {"basis": "web", "claims": [claim()] if claims is None else claims}})},
        "verification": verification if verification is not None else {
            "search_performed": searched, "source_urls": [URL] if observed is None else observed,
            "source_validation": "provider", "checked_at": STAMP}}


class SourceStub:
    def __init__(self, text=FIRST_QUOTE + " " + SECOND_QUOTE, *, status="ok", final_url=URL):
        self.text, self.status, self.final_url = text, status, final_url
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        assert url == URL
        return {"status": self.status, "text": self.text, "final_url": self.final_url,
                "checked_at": STAMP, "content_type": "text/plain"}


def evaluate(original, fetcher=None, state=None):
    state = copy.deepcopy(STATE) if state is None else state
    fetcher = SourceStub() if fetcher is None else fetcher
    original_snapshot, state_snapshot = copy.deepcopy(original), copy.deepcopy(state)
    checked = semantic_sources.verify_web_sources(original, fetcher=fetcher)
    checked_snapshot = copy.deepcopy(checked)
    result = finalize_answers(checked, state, {"J1": {}, "J5": {}})
    assert original == original_snapshot and state == state_snapshot and checked == checked_snapshot
    assert result["answers"]["J1"] == original["answers"]["J1"]
    return result, checked, fetcher


def j5(result):
    return result["answers"]["J5"]


def claims(result):
    return j5(result)["fact_check"]["claims"]


def assert_unverified(result):
    assert j5(result)["choice"] == "UNVERIFIED"
    assert j5(result)["confidence"] == 0 and j5(result)["probabilities"] == {}
    decisions = parse_decisions(result, ("J1", "J5"))
    assert decisions["J5"]["decision"] is None and decisions["J5"]["status"] == "UNVERIFIED"
    assert decisions["J1"]["decision"] == "FAIL"


def test_supported_quote_and_explained_authority_and_date_can_pass_without_saving_page():
    page = FIRST_QUOTE + " FULL PAGE TEXT MUST NOT BE SAVED"
    original = body()
    result, checked, fetcher = evaluate(original, SourceStub(page))
    assert j5(result)["choice"] == "PASS" and fetcher.calls == [URL]
    evidence = claims(result)[0]["sources"][0]
    assert evidence["quote_verified"] is True and evidence["verified_at"] == STAMP
    assert evidence["authority_reason"] == original["answers"]["J5"]["fact_check"]["claims"][0]["sources"][0]["authority_reason"]
    assert "FULL PAGE TEXT MUST NOT BE SAVED" not in json.dumps(result)
    assert result["verification"] == checked["verification"]


def test_invented_quote_on_real_search_url_cannot_pass_even_with_forged_verified_flags():
    forged = source("An invented quotation.", quote_verified=True, verified_at="model-invented timestamp",
                    resolved_url=URL, verification_reason="Model claimed independent verification")
    original = body([claim(sources=[forged])])
    original["verification"].update(source_validation="provider_and_content", source_checks=[{"verified_quotes": 999}])
    result, checked, _ = evaluate(original)
    assert_unverified(result)
    evidence = claims(result)[0]["sources"][0]
    assert evidence["quote"] == "An invented quotation." and evidence["quote_verified"] is False
    assert evidence["verified_at"] is None
    assert result["verification"]["source_checks"][0]["verified_quotes"] == 0
    assert checked["verification"]["source_checks"][0]["unverified_quotes"] == 1


def test_unobserved_url_is_never_fetched_and_cannot_supply_pass_evidence():
    original = body([claim(sources=[source(quote_verified=True)])], observed=["https://unrelated.example/"])
    result, checked, fetcher = evaluate(original)
    assert_unverified(result)
    assert fetcher.calls == [] and claims(result)[0]["sources"] == []
    assert checked["answers"]["J5"]["fact_check"]["claims"][0]["sources"][0]["quote_verified"] is False


def test_incomplete_search_cannot_use_citations_or_forged_verification_metadata():
    original = body([claim(sources=[source(quote_verified=True)])], searched=False)
    original["verification"]["source_validation"] = "provider_and_content"
    result, _, fetcher = evaluate(original)
    assert_unverified(result)
    assert fetcher.calls == [] and result["verification"]["source_checks"] == []


@pytest.mark.parametrize("status", ["timeout", "http_error", "unsupported_content_type", "response_too_large", "blocked_address"])
def test_unavailable_or_truncated_source_keeps_the_claim_unverified(status):
    result, _, _ = evaluate(body(), SourceStub(status=status))
    assert_unverified(result)
    assert claims(result)[0]["sources"][0]["quote_verified"] is False
    assert result["verification"]["source_checks"][0]["status"] == status


@pytest.mark.parametrize("authority", ["unknown", "", None, "official-looking"])
def test_matching_quote_does_not_replace_a_supported_publisher_assessment(authority):
    result, _, _ = evaluate(body([claim(sources=[source(authority=authority)])]))
    assert_unverified(result)
    evidence = claims(result)[0]["sources"][0]
    assert evidence["quote_verified"] is True and evidence["assessment_warning"]


@pytest.mark.parametrize("field", ["authority_reason", "temporal_relevance"])
@pytest.mark.parametrize("value", [None, "", " \r\n\t"])
def test_missing_authority_reason_or_reference_date_explanation_prevents_pass(field, value):
    candidate = source()
    if value is None:
        candidate.pop(field)
    else:
        candidate[field] = value
    result, _, _ = evaluate(body([claim(sources=[candidate])]))
    assert_unverified(result)
    assert claims(result)[0]["sources"][0]["quote_verified"] is True


def test_a_claim_absent_from_the_answer_cannot_become_valid_through_a_real_quote():
    result, _, _ = evaluate(body([claim("A fact which the answer never states.")]))
    assert_unverified(result)
    assert claims(result)[0]["verdict"] == "UNVERIFIED"


def test_one_pass_and_one_unverified_claim_keep_j5_unverified_and_other_fail_unchanged():
    mixed = [claim(), claim(SECOND, verdict="UNVERIFIED", reason="Conflicting historical accounts remain unresolved.",
                           sources=[source(SECOND_QUOTE)])]
    result, _, fetcher = evaluate(body(mixed))
    assert_unverified(result)
    assert [c["verdict"] for c in claims(result)] == ["PASS", "UNVERIFIED"]
    assert "Conflicting historical accounts" in j5(result)["reason"]
    assert fetcher.calls == [URL]


def test_a_verified_error_survives_another_unverified_claim():
    contrary_quote = "The fixture observatory opened in 1961, not 1960."
    reason = "The answer says '1960', but the institutional history records 1961."
    mixed = [claim(verdict="FAIL", reason=reason, sources=[source(contrary_quote)]),
             claim(SECOND, verdict="UNVERIFIED", reason="No dated evidence for the first public visit.", sources=[])]
    result, _, _ = evaluate(body(mixed, choice="UNVERIFIED"), SourceStub(contrary_quote))
    assert j5(result)["choice"] == "FAIL" and j5(result)["reason"] == reason
    assert [c["verdict"] for c in claims(result)] == ["FAIL", "UNVERIFIED"]
    assert parse_decisions(result, ("J5",))["J5"]["decision"] == "FAIL"


@pytest.mark.parametrize("original_choice", ["FAIL", "UNVERIFIED"])
def test_pass_only_subset_does_not_erase_the_models_acknowledged_gaps(original_choice):
    result, _, _ = evaluate(body([claim()], choice=original_choice))
    assert claims(result)[0]["verdict"] == "PASS"
    assert_unverified(result)


def test_partial_quote_verification_of_two_claims_on_one_page_does_not_hide_the_gap():
    both = [claim(), claim(SECOND, sources=[source(SECOND_QUOTE)])]
    result, _, fetcher = evaluate(body(both), SourceStub(FIRST_QUOTE))
    assert_unverified(result)
    assert [c["verdict"] for c in claims(result)] == ["PASS", "UNVERIFIED"]
    assert fetcher.calls == [URL]
    assert result["verification"]["source_checks"][0]["verified_quotes"] == 1
    assert result["verification"]["source_checks"][0]["unverified_quotes"] == 1


def test_one_supported_source_can_support_a_claim_while_failed_citation_stays_visible():
    original = body([claim(sources=[source("Fabricated quotation."), source()])])
    result, _, _ = evaluate(original)
    assert j5(result)["choice"] == "PASS"
    assert [s["quote_verified"] for s in claims(result)[0]["sources"]] == [False, True]


def test_redirected_host_cannot_reuse_the_models_original_publisher_assessment():
    result, _, _ = evaluate(body(), SourceStub(final_url="https://different.example/history"))
    assert_unverified(result)
    assert result["verification"]["source_checks"][0]["status"] == "redirect_host_changed"


@pytest.mark.parametrize("transport", ["api", "cli"])
def test_provider_event_bodies_use_the_same_independent_quote_gate(transport):
    if transport == "api":
        verification = api_verification([{"type": "web_search_call", "status": "completed",
                                         "action": {"sources": [{"url": URL}]}}])
        model = "resolved-api-judge"
    else:
        verification = cli_verification([{"type": "item.completed", "item": {
            "type": "web_search", "status": "completed", "sources": [{"url": URL}]}}])
        model = "requested-cli-judge"
    original = body(verification=verification, model=model)
    result, _, fetcher = evaluate(original)
    assert result["model"] == model and j5(result)["choice"] == "PASS"
    assert result["verification"]["search_performed"] is True and fetcher.calls == [URL]
    assert result["verification"]["source_validation"] == "provider_and_content"
    failure, _, _ = evaluate(original, SourceStub("The actual page does not contain the quotation."))
    assert_unverified(failure)


def test_no_facts_protocol_is_preserved_without_claiming_independent_fact_detection():
    original = body(claims=[], choice="NOT_APPLICABLE", searched=False)
    j5(original)["fact_check"]["basis"] = "none"
    j5(original)["reason"] = "A greeting with no factual assertions."
    state = {"question": "Say hello.", "answer": "Hello!"}
    result, _, fetcher = evaluate(original, state=state)
    assert j5(result)["choice"] == "NOT_APPLICABLE" and fetcher.calls == []
    assert parse_decisions(result, ("J5",))["J5"]["status"] == "NOT_APPLICABLE"
