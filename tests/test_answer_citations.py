"""답변이 직접 든 출처가 주장을 뒷받침하는지 검증 (G-4, 모델·네트워크 호출 없음)."""
from __future__ import annotations

import copy
import json

import httpx
import pytest

from code_eval.anthropic_semantic_client import FINAL_TOOL, AnthropicSemanticClient, AnthropicSemanticConfig
from code_eval.answer_citations import MAX_CITATION_URLS, answer_citations, prompt_citations, source_requested
from code_eval.jev_eval import evaluate_jev_case
from code_eval.semantic_evidence import finalize_answers
from code_eval.semantic_sources import verify_answer_citations

F = "```"


@pytest.mark.parametrize("answer,expected", [
    ("서울 인구는 약 935만 명입니다([통계청](https://kosis.kr/a)). 부산은 330만 명입니다.",
     [("https://kosis.kr/a", "link", ["서울 인구는 약 935만 명입니다([통계청](https://kosis.kr/a))."])]),
    ("서울 인구는 약 935만 명입니다[^1].\n\n[^1]: 통계청 https://kosis.kr/a",
     [("https://kosis.kr/a", "footnote", ["서울 인구는 약 935만 명입니다[^1]."])]),
    ("서울은 935만 명입니다 [1]. 경기는 1,360만 명입니다 [2].\n\n[1] 통계청 https://kosis.kr/a\n[2] 경기도 https://gg.go.kr/b",
     [("https://kosis.kr/a", "reference", ["서울은 935만 명입니다 [1]."]),
      ("https://gg.go.kr/b", "reference", ["경기는 1,360만 명입니다 [2]."])]),
    ("서울 인구는 약 935만 명입니다.\n\n출처: https://kosis.kr", [("https://kosis.kr/", "bare", [])]),   # 답변 전체 출처
    ("예시입니다.\n\n" + F + "\ncurl https://example.com\n" + F, []),                                  # 코드 안 URL 제외
    ("[개요](#overview)를 보세요.", []),                                                               # 내부 링크는 1-3
])
def test_citations_and_attached_claims_are_extracted_from_the_answer(answer, expected):
    assert [(c["url"], c["kind"], c["claims"]) for c in answer_citations(answer)] == expected


def test_invalid_links_duplicates_and_limit_are_recorded_without_fetching():
    citations = answer_citations("자세한 내용은 [여기](kosis)를 참고하세요. 인구는 935만 명입니다(https://kosis.kr). "
                                 "면적은 605㎢입니다(https://kosis.kr). " +
                                 " ".join(f"사실 {i}입니다 https://ex{i}.com/p." for i in range(1, 6)))
    invalid, dup = citations[0], citations[1]
    assert (invalid["valid_url"], invalid["checked"]) == (False, False)
    assert len(dup["claims"]) == 2
    assert sum(c["checked"] for c in citations) == MAX_CITATION_URLS and not citations[-1]["checked"]
    assert [c["id"] for c in prompt_citations(citations)] == [c["id"] for c in citations if c["checked"]]
    assert source_requested("출처 링크도 함께 적어줘") and not source_requested("서울 인구를 알려줘")


ANSWER = "Alpha is blue [1]. Beta is 42 [2].\n\n[1] https://cited.example/alpha\n[2] https://cited.example/beta"
STATE = {"question": "Alpha와 Beta를 알려줘", "answer": ANSWER,
         "answer_citations": prompt_citations(answer_citations(ANSWER))}
PAGES = {"https://cited.example/alpha": "Today's baseball scores and weather.",
         "https://cited.example/beta": "Beta is 42 according to the registry."}


def fetcher(url):
    text = PAGES.get(url)
    return {"status": "ok", "text": text, "final_url": url} if text else {"status": "http_error", "text": ""}


def check(url, claim, verdict, quote="", reason="fixture"):
    return {"url": url, "claim": claim, "verdict": verdict, "quote": quote, "reason": reason}


def finalized(checks, choice="PASS", page_fetcher=fetcher):
    body = {"answers": {"J5": {"choice": choice, "confidence": .9, "reason": "ok", "probabilities": {"PASS": .9, "FAIL": .1},
                               "fact_check": {"basis": "none", "claims": []}, "citation_checks": checks}},
            "verification": {}}
    body = verify_answer_citations(body, STATE, fetcher=page_fetcher)
    return finalize_answers(body, STATE, {"J5": {}})["answers"]["J5"], body["verification"]


def test_server_fetches_each_cited_url_and_checks_the_judges_quote():
    j5, verification = finalized([
        check("https://cited.example/alpha", "Alpha is blue [1].", "unrelated", reason="야구 기사"),
        check("https://cited.example/beta", "Beta is 42 [2].", "supported", quote="Beta is 42")])
    assert [s["status"] for s in verification["citation_sources"]] == ["ok", "ok"]
    assert "text" not in json.dumps(verification["citation_sources"])   # page text is never stored
    verdicts = {c["url"]: c["verdict"] for c in j5["citation_checks"]}
    assert verdicts == {"https://cited.example/alpha": "unrelated", "https://cited.example/beta": "supported"}
    # An unsupported citation makes J5 fail as a source mismatch, recording the model's original assessment.
    assert j5["choice"] == "FAIL" and j5["citation_mismatch"] is True and "출처가 주장과 무관" in j5["reason"]
    assert j5["model_original_assessment"]["choice"] == "PASS"


@pytest.mark.parametrize("checks,page_fetcher,verdict", [
    # 인용문이 원문에 없으면 '지지'를 확정하지 않는다
    ([check("https://cited.example/beta", "Beta is 42 [2].", "supported", quote="Beta is 7")], fetcher, "unverified"),
    # 주장 수치가 원문에 있으면 '내용 없음' 판단을 확정하지 않는다
    ([check("https://cited.example/beta", "Beta is 42 [2].", "not_found", reason="없음")], fetcher, "unverified"),
    # 평가자가 열지 못했다고 했어도 서버가 받았으면 판정에 쓰지 않는다
    ([check("https://cited.example/beta", "Beta is 42 [2].", "inaccessible")], fetcher, "unverified"),
    # 답변에 없는 구절은 쓰지 않는다
    ([check("https://cited.example/beta", "Beta is 43.", "unrelated", reason="다름")], fetcher, "unverified"),
])
def test_unconfirmed_citation_judgments_never_fail_the_answer(checks, page_fetcher, verdict):
    j5, _ = finalized(checks, page_fetcher=page_fetcher)
    assert j5["citation_checks"][0]["verdict"] == verdict
    assert j5["choice"] != "FAIL" and "citation_mismatch" not in j5
    # evidence-v3.14: 답변이 든 출처가 뒷받침으로 확인되지 않으면 PASS 로 확정하지 않는다
    assert j5["choice"] != "PASS"
    assert j5["citation_checks"][-1]["verdict"] == "unchecked"   # alpha was not recorded by the judge


@pytest.mark.parametrize("status,verdict", [("http_error", "supported"), ("blocked_address", "inaccessible")])
def test_blocked_cited_source_uses_the_providers_fetched_text_only_for_access_failures(status, verdict):
    # F5b: server fetch failed; the provider's web_fetch of the same cited URL returned the page.
    body = {"answers": {"J5": {"choice": "PASS", "confidence": .9, "reason": "ok", "probabilities": {"PASS": .9, "FAIL": .1},
                               "fact_check": {"basis": "none", "claims": []},
                               "citation_checks": [check("https://cited.example/beta", "Beta is 42 [2].", "supported",
                                                         quote="Beta is 42")]}}, "verification": {}}
    documents = {"https://cited.example/beta": [{"text": "Beta is 42 according to the registry.", "retrieved_at": None}]}
    body = verify_answer_citations(body, STATE, fetcher=lambda url: {"status": status, "text": ""}, provider_documents=documents)
    j5 = finalize_answers(body, STATE, {"J5": {}})["answers"]["J5"]
    beta = [c for c in j5["citation_checks"] if c["url"] == "https://cited.example/beta"][0]
    assert beta["verdict"] == verdict and beta["server_status"] == status
    assert (beta.get("retrieval") == "provider_document") is (status == "http_error")
    assert "registry" not in json.dumps(body["verification"])   # provider text is never stored


def unrelated_verdicts(answer, url, claims, page):
    state = {"question": "출처도 알려줘", "answer": answer, "answer_citations": prompt_citations(answer_citations(answer))}
    body = {"answers": {"J5": {"choice": "PASS", "confidence": .9, "reason": "ok", "probabilities": {"PASS": .9, "FAIL": .1},
                               "fact_check": {"basis": "none", "claims": []},
                               "citation_checks": [check(url, claim, "unrelated", reason="다른 주제") for claim in claims]}},
            "verification": {}}
    body = verify_answer_citations(body, state, fetcher=lambda u: {"status": "ok", "text": page, "final_url": u})
    return [c["verdict"] for c in finalize_answers(body, state, {"J5": {}})["answers"]["J5"]["citation_checks"]]


DOC = "https://docs.example/3/library/os.html?v=2"


@pytest.mark.parametrize("answer,claim,page,verdict", [
    # F2 (G4-02): 링크 주소의 경로·쿼리 숫자는 주장 수치가 아니다
    (f"정렬은 제자리에서 합니다 ([출처]({DOC})).", None, "Python 3 path functions, version 2", "unrelated"),
    # 맨 URL·인용 번호의 숫자도 주장 수치가 아니다
    (f"정렬은 제자리에서 합니다 [3].\n\n[3] {DOC}", "정렬은 제자리에서 합니다 [3].", "Python 3 path functions", "unrelated"),
    # 본문 수치가 원문에 있으면 여전히 '무관'을 확정하지 않는다
    (f"인구는 935만 명입니다 ([출처]({DOC})).", None, "population 935 in 2024", "unverified"),
    # 본문 수치와 URL 숫자가 다르면 본문 수치만 대조한다
    (f"인구는 935만 명입니다 ([출처]({DOC})).", None, "Python 3 documentation v2", "unrelated"),
])
def test_only_numbers_in_the_claim_text_block_an_unrelated_verdict(answer, claim, page, verdict):
    assert unrelated_verdicts(answer, DOC, [claim or answer], page) == [verdict]


def test_each_claim_on_the_same_cited_url_is_checked_separately():
    answer = f"인구는 935만 명입니다 ([출처]({DOC})). 정렬은 제자리에서 합니다 ([출처]({DOC}))."
    claims = [f"인구는 935만 명입니다 ([출처]({DOC})).", f"정렬은 제자리에서 합니다 ([출처]({DOC}))."]
    assert unrelated_verdicts(answer, DOC, claims, "population 935") == ["unverified", "unrelated"]


def test_contradicting_source_with_a_verified_quote_is_a_mismatch_even_after_a_fail():
    PAGES_C = {**PAGES, "https://cited.example/beta": "Beta is 7, not 42."}
    j5, _ = finalized([check("https://cited.example/beta", "Beta is 42 [2].", "contradicted", quote="Beta is 7")],
                      choice="FAIL", page_fetcher=lambda url: {"status": "ok", "text": PAGES_C[url], "final_url": url})
    assert j5["choice"] == "FAIL" and j5["citation_mismatch"] is True and "출처가 주장과 모순" in j5["reason"]


def test_not_applicable_j5_records_citations_without_changing_the_verdict():
    body = {"answers": {"J5": {"choice": "NOT_APPLICABLE", "confidence": .9, "reason": "사실 없음",
                               "fact_check": {"basis": "none", "claims": []},
                               "citation_checks": [check("https://cited.example/alpha", "Alpha is blue [1].", "unrelated",
                                                         reason="무관")]}}, "verification": {}}
    body = verify_answer_citations(body, STATE, fetcher=fetcher)
    j5 = finalize_answers(body, STATE, {"J5": {}})["answers"]["J5"]
    assert j5["choice"] == "NOT_APPLICABLE" and "citation_mismatch" not in j5


def test_claude_judge_checks_cited_sources_inside_the_same_j5_call():
    evidence = "https://evidence.example/page"
    blocks = [{"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "alpha"}},
              {"type": "web_search_tool_result", "tool_use_id": "s1", "content": [
                  {"type": "web_search_result", "url": evidence, "title": "Official", "encrypted_content": "x"}]}]
    j5 = {"choice": "PASS", "confidence": .9, "reason": "공식 자료와 일치",
          "fact_check": {"basis": "web", "claims": [{"claim": "Alpha is blue", "verdict": "PASS", "reason": "확인",
                                                    "scope": "external_fact", "scope_quote": "", "sources": [
              {"url": evidence, "title": "Official", "quote": "Alpha is blue.", "authority": "primary",
               "authority_reason": "공식 기관", "temporal_relevance": "불변 정의", "origin": "web", "turn": None}]}]},
          "citation_checks": [check("https://cited.example/alpha", "Alpha is blue [1].", "unrelated", reason="야구 기사")]}
    raw = {"model": "claude-opus-5-5", "stop_reason": "tool_use", "content": [
        *blocks, {"type": "tool_use", "id": "final", "name": FINAL_TOOL,
                  "input": {"J1": {"choice": "PASS", "confidence": .9, "reason": "답함"}, "J5": j5}}],
        "usage": {"input_tokens": 100, "output_tokens": 50, "server_tool_use": {"web_search_requests": 1}}}
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=copy.deepcopy(raw))

    pages = {**PAGES, evidence: "Alpha is blue."}
    client = AnthropicSemanticClient(AnthropicSemanticConfig(model="claude-opus-5-5", reasoning_effort="high"), "test-key",
                                     transport=httpx.MockTransport(handler),
                                     source_fetcher=lambda url: {"status": "ok", "text": pages[url], "final_url": url})
    record = evaluate_jev_case("q", "Alpha 색을 알려줘", ANSWER, client, criteria=("J1", "J5"),
                               fail_evidence_check=True, citation_check=True)
    request = sent[0]
    schema = next(t for t in request["tools"] if t.get("name") == FINAL_TOOL)["input_schema"]
    assert "citation_checks" in schema["properties"]["J5"]["required"]
    prompt = "".join(block["text"] for block in request["messages"][0]["content"])
    assert "answer_citations" in prompt and "citation_checks에 기록" in prompt
    decision = record["decisions"]["J5"]
    assert decision["decision"] == "FAIL" and decision["j5_outcome"]["code"] == "source_mismatch"
    assert decision["j5_outcome"]["factual_error_confirmed"] is False
    assert decision["j5_outcome"]["citations"]["mismatch"] == 1
    assert record["answer_citations"]["requested_by_question"] is False
    assert [c["url"] for c in record["answer_citations"]["citations"]] == list(PAGES)


def test_answers_without_citations_keep_the_previous_request_shape():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "m", "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "f", "name": FINAL_TOOL, "input": {"J1": {"choice": "PASS", "confidence": .9,
                                                                                "reason": "ok"}}}],
            "usage": {"input_tokens": 1, "output_tokens": 1}})

    client = AnthropicSemanticClient(AnthropicSemanticConfig(), "k", transport=httpx.MockTransport(handler))
    record = evaluate_jev_case("q", "질문", "출처 없는 답변입니다.", client, criteria=("J1",), citation_check=True)
    assert "answer_citations" not in json.dumps(sent[0]["messages"][0]["content"], ensure_ascii=False)
    assert "answer_citations" not in record


# evidence-v3.14 (2026-10-13 사용자 결정) — 답변이 준 URL 을 열 수 없으면 FAIL, PASS 는 모든 출처가 확인될 때만
def test_inaccessible_cited_url_fails_and_pass_needs_every_cited_source_supported():
    blocked = lambda url: {"status": "http_error", "text": ""} if url.endswith("alpha") else fetcher(url)
    j5, _ = finalized([check("https://cited.example/alpha", "Alpha is blue [1].", "supported", quote="Alpha is blue"),
                       check("https://cited.example/beta", "Beta is 42 [2].", "supported", quote="Beta is 42")],
                      page_fetcher=blocked)
    assert j5["choice"] == "FAIL" and j5["citation_inaccessible"] is True and "출처 원문 접근 불가" in j5["reason"]
    # 출처 판정만 따로: 모든 출처가 열리고 뒷받침하면 PASS 유지, 하나라도 확인되지 않으면 PASS 로 확정하지 않는다
    from code_eval import semantic_evidence
    ok_pages = {"https://cited.example/alpha": "Alpha is blue.", "https://cited.example/beta": "Beta is 42 according to the registry."}
    ok = lambda url: {"status": "ok", "text": ok_pages[url], "final_url": url}
    def citation_only(checks):
        body = {"answers": {"J5": {"choice": "PASS", "confidence": .9, "reason": "ok", "citation_checks": checks}}, "verification": {}}
        entry = verify_answer_citations(body, STATE, fetcher=ok)["answers"]["J5"]
        semantic_evidence._citation_check(entry, STATE)
        return entry
    both = citation_only([check("https://cited.example/alpha", "Alpha is blue [1].", "supported", quote="Alpha is blue"),
                          check("https://cited.example/beta", "Beta is 42 [2].", "supported", quote="Beta is 42")])
    assert both["choice"] == "PASS" and "citation_mismatch" not in both
    one = citation_only([check("https://cited.example/beta", "Beta is 42 [2].", "supported", quote="Beta is 42")])
    assert one["choice"] == "UNVERIFIED" and "PASS 로 확정하지 않음" in one["reason"]   # alpha 를 확인하지 않음


def test_server_fetch_status_counts_even_when_the_judge_records_no_citation_checks():
    blocked = lambda url: {"status": "http_error", "text": ""} if url.endswith("alpha") else fetcher(url)
    j5, verification = finalized([], page_fetcher=blocked)
    assert {s["url"]: s["status"] for s in verification["citation_sources"]}["https://cited.example/alpha"] == "http_error"
    assert j5["choice"] == "FAIL" and j5["citation_inaccessible"] is True


def test_citations_beyond_the_check_limit_block_a_pass():
    from code_eval import semantic_evidence
    ok = lambda url: {"status": "ok", "text": "Beta is 42 according to the registry.", "final_url": url}
    body = {"answers": {"J5": {"choice": "PASS", "confidence": .9, "reason": "ok",
                               "citation_checks": [check("https://cited.example/beta", "Beta is 42 [2].", "supported", quote="Beta is 42")]}},
            "verification": {}}
    state = {**STATE, "answer_citations": [c for c in STATE["answer_citations"] if c["url"].endswith("beta")],
             "answer_citations_unchecked": ["https://cited.example/fifth"]}
    entry = verify_answer_citations(body, state, fetcher=ok)["answers"]["J5"]
    semantic_evidence._citation_check(entry, state)
    assert entry["choice"] == "UNVERIFIED" and "fifth" in entry["reason"]
