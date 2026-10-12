"""Evidence recovery keeps source gates, all claims and established judgments."""
import copy

import pytest

from code_eval.semantic_evidence import finalize_answers
from code_eval.semantic_recovery import evidence_recovery_prompt, merge_evidence_recovery


STATE = {"question": "Describe Alpha, Beta and Gamma.",
         "answer": "Alpha is blue. Beta is green. Gamma is red."}
QUESTIONS = {"J1": {}, "J5": {}}
URL = "https://official.example/facts"


def claim(name="Alpha", verdict="UNVERIFIED", **changes):
    text = {"Alpha": "Alpha is blue.", "Beta": "Beta is green.", "Gamma": "Gamma is red."}[name]
    return {"claim": text, "scope": "external_fact", "scope_quote": "", "verdict": verdict,
            "reason": name + " verified" if verdict != "UNVERIFIED" else name + " missing evidence",
            "sources": [] if verdict == "UNVERIFIED" else [{
                "origin": "web", "url": URL, "quote": text, "title": "Official facts", "quote_verified": True,
                "authority": "primary", "authority_reason": "Official author", "temporal_relevance": "Stable definition"}],
            **changes}


def body(claims, choice="UNVERIFIED", **changes):
    return finalize_answers({"model": "fixture-judge", "answers": {
        "J1": {"choice": "FAIL", "confidence": .7, "reason": "Separate criterion", "nested": [1, 2]},
        "J5": {"choice": choice, "confidence": .8, "probabilities": {"PASS": .9, "FAIL": .1},
               "reason": "Complete assessment", "fact_check": {"basis": "web", "claims": claims}}},
        "verification": {"search_performed": True, "source_validation": "provider_and_content",
                         "source_urls": [URL], "source_checks": [{"url": URL, "status": "ok"}],
                         "checked_at": "initial", "output_transport": "fixture"}, **changes}, STATE, QUESTIONS)


def entry(value):
    return value["answers"]["J5"]


def merge(first, second):
    snapshots = copy.deepcopy((first, second, STATE))
    result = merge_evidence_recovery(first, second, STATE, QUESTIONS)
    assert (first, second, STATE) == snapshots
    assert result["answers"]["J1"] == first["answers"]["J1"]
    return result


def test_prompt_preserves_complete_diagnostics_and_rejects_empty_duplicate_or_unquoted_claims():
    first = body([claim(), claim("Beta", "PASS")])
    prompt = evidence_recovery_prompt(first, STATE)
    assert "Alpha is blue." in prompt and "Beta is green." in prompt
    assert "Alpha missing evidence" in prompt and "NOT_APPLICABLE" in prompt
    assert evidence_recovery_prompt(body([claim("Alpha", "PASS")], "PASS"), STATE) is None
    for claims in ([], [claim(), claim()], [claim(claim="Not present in the answer.")]):
        malformed = copy.deepcopy(first)
        entry(malformed)["fact_check"]["claims"] = claims
        assert evidence_recovery_prompt(malformed, STATE) is None


def test_complete_verified_followup_resolves_all_unknowns_and_preserves_established_claim():
    first = body([claim(), claim("Beta", "PASS")])
    second = body([claim("Alpha", "PASS"), claim("Beta", "PASS", reason="Changed commentary")], "PASS")
    result = merge(first, second)
    assert entry(result)["choice"] == "PASS"
    assert entry(result)["fact_check"]["claims"][1] == entry(first)["fact_check"]["claims"][1]


def test_one_verified_error_resolves_whole_criterion_despite_another_unknown():
    first = body([claim(), claim("Beta")])
    second = body([claim("Alpha", "FAIL"), claim("Beta")])
    result = merge(first, second)
    assert entry(result)["choice"] == "FAIL"
    assert [c["verdict"] for c in entry(result)["fact_check"]["claims"]] == ["FAIL", "UNVERIFIED"]


@pytest.mark.parametrize("mode", ["omitted", "scope_changed", "quote_changed", "duplicate", "no_facts"])
def test_followup_cannot_erase_or_change_an_unresolved_claim(mode):
    first = body([claim(), claim("Beta")])
    second = body([claim("Alpha", "PASS"), claim("Beta", "PASS")], "PASS")
    candidates = entry(second)["fact_check"]["claims"]
    if mode == "omitted": candidates.pop()
    elif mode == "scope_changed": candidates[-1]["scope"] = "input_consistency"
    elif mode == "quote_changed": candidates[-1]["scope_quote"] = "Changed request"
    elif mode == "duplicate": candidates.append(copy.deepcopy(candidates[-1]))
    else:
        entry(second).update(choice="NOT_APPLICABLE", fact_check={"basis": "none", "claims": []})
    result = merge(first, second)
    assert entry(result)["choice"] == "UNVERIFIED"
    assert [c["claim"] for c in entry(result)["fact_check"]["claims"]] == ["Alpha is blue.", "Beta is green."]
    assert entry(result)["fact_check"]["claims"][1]["verdict"] == "UNVERIFIED"


def test_failed_source_gate_cannot_be_promoted_by_recovery_merge():
    first = body([claim()])
    forged = claim("Alpha", "PASS")
    forged["sources"][0]["quote_verified"] = False
    second = body([forged], "PASS")
    assert entry(second)["choice"] == "UNVERIFIED"
    assert entry(merge(first, second))["choice"] == "UNVERIFIED"


def test_new_unknown_claim_is_retained_and_blocks_pass():
    first = body([claim()])
    second = body([claim("Alpha", "PASS"), claim("Beta")])
    result = merge(first, second)
    assert entry(result)["choice"] == "UNVERIFIED"
    assert [c["claim"] for c in entry(result)["fact_check"]["claims"]] == ["Alpha is blue.", "Beta is green."]


def test_acknowledged_overall_gap_requires_explicit_complete_pass():
    first = body([claim("Alpha", "PASS")])
    partial = body([claim("Alpha", "PASS")])
    complete = body([claim("Alpha", "PASS")], "PASS")
    assert entry(merge(first, partial))["choice"] == "UNVERIFIED"
    assert entry(merge(first, complete))["choice"] == "PASS"


def test_trusted_verification_is_unioned_without_overwriting_initial_metadata():
    first = body([claim()])
    second = body([claim("Alpha", "PASS")], "PASS")
    second["verification"].update(source_urls=[URL, "https://other.example/facts"],
                                  source_checks=[{"url": "https://other.example/facts", "status": "ok"}],
                                  checked_at="followup", output_transport="followup")
    result = merge(first, second)
    assert result["verification"]["source_urls"] == [URL, "https://other.example/facts"]
    assert len(result["verification"]["source_checks"]) == 2
    assert result["verification"]["checked_at"] == "initial"
    assert result["verification"]["output_transport"] == "fixture"
