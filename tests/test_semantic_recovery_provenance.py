"""Recovery reuses observed URLs while independently checking every quotation."""
import copy
import json
import threading

import httpx

from code_eval import semantic_api_client
from code_eval.jev_eval import jev_request
from code_eval.semantic_api_client import SemanticApiClient, SemanticApiConfig


ALPHA_URL = "https://official.example/alpha"
BETA_URL = "https://official.example/beta"
STATE = {"question": "Describe Alpha and Beta.", "answer": "Alpha is blue. Beta is green."}


def claim(text, url, *, verdict="PASS", quote=None):
    return {"claim": text, "verdict": verdict, "reason": "Evidence found" if verdict == "PASS" else "Evidence missing",
            "scope": "external_fact", "scope_quote": "", "sources": [] if verdict == "UNVERIFIED" else [{
                "origin": "web", "turn": None, "url": url, "quote": text if quote is None else quote,
                "title": "Official facts", "authority": "primary", "authority_reason": "Original publisher",
                "temporal_relevance": "Stable definition"}]}


def response(claims, choice, observed, *, include_j1=False):
    answers = {"J5": {"choice": choice, "confidence": .8, "reason": "Assessment",
                      "fact_check": {"basis": "web", "claims": claims}}}
    if include_j1:
        answers["J1"] = {"choice": "PASS", "confidence": .8, "reason": "Original independent assessment"}
    return {"status": "completed", "model": "gpt-6.1-sol", "service_tier": "default",
            "usage": {"input_tokens": 100, "output_tokens": 20}, "output": [
                {"type": "web_search_call", "status": "completed",
                 "action": {"type": "search", "sources": [{"url": observed}]}},
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(answers)}]}]}


def run(*, forged_quote=False, cancel_event=None):
    quote = "Fabricated words absent from the official page." if forged_quote else None
    initial = response([claim("Alpha is blue.", ALPHA_URL, quote=quote),
                        claim("Beta is green.", BETA_URL, verdict="UNVERIFIED")],
                       "UNVERIFIED", ALPHA_URL, include_j1=True)
    supplemental = response([claim("Alpha is blue.", ALPHA_URL, quote=quote),
                             claim("Beta is green.", BETA_URL)], "PASS", BETA_URL)
    snapshots = copy.deepcopy((initial, supplemental, STATE))
    sent, fetched = [], []

    def transport(request):
        sent.append(request)
        assert len(sent) <= 2, "Recovery exceeded its bounded request count"
        return httpx.Response(200, json=initial if len(sent) == 1 else supplemental)

    def fetcher(url):
        fetched.append(url)
        assert url in (ALPHA_URL, BETA_URL)
        return {"status": "ok", "text": "Alpha is blue." if url == ALPHA_URL else "Beta is green.",
                "final_url": url, "content_type": "text/plain"}

    client = SemanticApiClient(SemanticApiConfig(), "fixture-only", transport=httpx.MockTransport(transport),
                               source_fetcher=fetcher)
    result = client.system_one(STATE, jev_request(STATE["question"], STATE["answer"], ("J1", "J5"))["questions"],
                               cancel_event=cancel_event)
    assert (initial, supplemental, STATE) == snapshots
    return result, sent, fetched


def test_prior_confirmed_claim_reuses_observed_source_without_repeating_search():
    result, sent, fetched = run()
    assert result.status == "OK" and result.attempts == len(sent) == 2
    j5 = result.body["answers"]["J5"]
    assert j5["choice"] == "PASS"
    assert [item["verdict"] for item in j5["fact_check"]["claims"]] == ["PASS", "PASS"]
    assert result.body["answers"]["J1"]["reason"] == "Original independent assessment"
    audit = result.body["verification"]["evidence_recovery"]
    assert audit["supplemental_j5"]["choice"] == "PASS" and audit["status"] == "RESOLVED"
    assert audit["verification"]["inherited_source_urls"] == [ALPHA_URL]
    assert set(audit["verification"]["source_urls"]) == {ALPHA_URL, BETA_URL}
    assert fetched == [ALPHA_URL, ALPHA_URL, BETA_URL]
    assert [part["web_search_calls"] for part in result.accounting["cost_parts"]] == [1, 1]


def test_observed_prior_url_does_not_make_fabricated_quote_verified():
    result, sent, fetched = run(forged_quote=True)
    assert result.status == "OK" and len(sent) == 2
    assert result.body["answers"]["J5"]["choice"] == "UNVERIFIED"
    audit = result.body["verification"]["evidence_recovery"]
    assert audit["verification"]["inherited_source_urls"] == [ALPHA_URL]
    claims = audit["supplemental_j5"]["fact_check"]["claims"]
    assert [item["verdict"] for item in claims] == ["UNVERIFIED", "PASS"]
    assert claims[0]["sources"][0]["quote_verified"] is False
    assert fetched == [ALPHA_URL, ALPHA_URL, BETA_URL]
    checks = audit["verification"]["source_checks"]
    assert checks[0]["verified_quotes"] == 0 and checks[0]["unverified_quotes"] == 1


def test_cancellation_arriving_during_merge_discards_judgment_and_keeps_usage(monkeypatch):
    token = threading.Event()
    original_merge = semantic_api_client.merge_evidence_recovery

    def cancel_after_merge(*args, **kwargs):
        merged = original_merge(*args, **kwargs)
        token.set()
        return merged

    monkeypatch.setattr(semantic_api_client, "merge_evidence_recovery", cancel_after_merge)
    result, sent, _ = run(cancel_event=token)
    assert result.status == "SKIPPED" and result.attempts == len(sent) == 2
    assert "answers" not in result.body
    audit = result.body["verification"]["evidence_recovery"]
    assert audit["status"] == "CANCELLED" and audit["final_choice"] is None
    parts = result.body["verification"]["usage_requests"]
    assert len(parts) == 2 and all(part["usage"]["input_tokens"] == 100 for part in parts)
    assert result.usage["input_tokens"] == 200 and result.usage["output_tokens"] == 40
