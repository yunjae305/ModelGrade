"""답의 구조화 출처(answer_sources) → CODE · J5 — 출처가 J5 에 먼저 닿고, 잘못된 출처는 입력 오류, 접근 실패와 인용 없음은 따로 남는다.

의미 평가 Judge 는 대역(유료 호출 없음), CODE 검사는 실제 엔진.
"""
from __future__ import annotations

import json

import httpx

from code_eval import jev_batch
from code_eval.anthropic_semantic_client import FINAL_TOOL, AnthropicSemanticClient, AnthropicSemanticConfig
from code_eval.config import AionaConfig
from code_eval.engine import evaluate_batch, evaluate_case
from code_eval.jev_eval import evaluate_jev_case
from code_eval.semantic_policy import j5_outcome
from tests.test_jev_batch import FakeBatches, judge as batch_judge


# --- J5 출처 연결 ----------------------------------------------------------------------------

def claude_client(j5=None):
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        tool = next(t for t in body["tools"] if t.get("name") == FINAL_TOOL)
        answers = {cid: {"choice": "PASS", "confidence": .9, "reason": "대역 판정"} for cid in tool["input_schema"]["properties"]}
        if "J5" in answers:
            answers["J5"] = j5 or {"choice": "NOT_APPLICABLE", "confidence": .9, "reason": "대역",
                                   "fact_check": {"basis": "none", "claims": []}, "citation_checks": []}
        return httpx.Response(200, json={"model": "claude-opus-5-5", "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "f", "name": FINAL_TOOL, "input": answers}], "usage": {"input_tokens": 1, "output_tokens": 1}})
    client = AnthropicSemanticClient(AnthropicSemanticConfig(model="claude-opus-5-5"), "k", transport=httpx.MockTransport(handler),
                                     source_fetcher=lambda url: {"status": "ok", "text": "서울 인구는 약 930만 명입니다.", "final_url": url})
    return client, sent


def test_structured_sources_reach_j5_first_and_unresolved_relays_are_not_checked():
    answer = "서울 인구는 약 930만 명입니다. 자세한 내용은 [통계청](https://kosis.kr/text) 참고."
    sources = [{"id": "S1", "url": "https://data.seoul.go.kr/p", "display_url": "https://data.seoul.go.kr/p?utm_source=chatgpt.com",
                "claims": ["서울 인구는 약 930만 명입니다.", "답변에 없는 구절"], "origin": "export_metadata"},
               {"id": "S2", "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/X",
                "relay_url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/X", "claims": [],
                "origin": "api_annotation", "skip_reason": "relay_unresolved"}]
    client, sent = claude_client()
    record = evaluate_jev_case("Q1", "서울 인구 출처와 함께", answer, client, criteria=("J1", "J5"),
                               fail_evidence_check=True, citation_check=True, sources=sources)
    prompt = "".join(block["text"] for block in sent[0]["messages"][0]["content"])
    citations = record["answer_citations"]
    assert citations["policy"] == "answer-citations-v3" and citations["structured_sources"] == 2
    assert [(c["url"], c["checked"]) for c in citations["citations"]] == [
        ("https://data.seoul.go.kr/p", True), ("https://vertexaisearch.cloud.google.com/grounding-api-redirect/X", False),
        ("https://kosis.kr/text", True)]
    assert citations["citations"][0]["claims"] == ["서울 인구는 약 930만 명입니다."]
    assert "https://data.seoul.go.kr/p" in prompt and "grounding-api-redirect" not in prompt
    client, sent = claude_client()
    plain = evaluate_jev_case("Q1", "q", answer, client, criteria=("J1", "J5"), fail_evidence_check=True, citation_check=True)
    assert plain["answer_citations"]["policy"] == "answer-citations-v2"


def test_message_batches_use_the_same_structured_sources_as_realtime():
    fake = FakeBatches()
    client, config = batch_judge(fake)
    case = {"case_id": "Q1", "question": "서울 인구 출처와 함께", "answer": "서울 인구는 약 930만 명입니다.",
            "answer_sources": [{"id": "S1", "url": "https://data.seoul.go.kr/p", "claims": ["서울 인구는 약 930만 명입니다."],
                                "origin": "export_metadata"}]}
    jev = jev_batch.evaluate_jev_batch_batched(evaluate_batch([case], AionaConfig()), client, config, sleep=lambda _: None,
                                               custom_id_prefix="run1_chatgpt-web_")
    params = next(iter(fake.batches.values()))["requests"][0]["params"]
    assert "https://data.seoul.go.kr/p" in json.dumps(params, ensure_ascii=False) and not fake.realtime
    citations = jev["records"][0]["answer_citations"]
    assert citations["policy"] == "answer-citations-v3" and citations["structured_sources"] == 1


def test_invalid_answer_sources_are_input_errors_and_valid_ones_do_not_change_code_results():
    base = {"case_id": "Q1", "question": "서울 인구는?", "answer": "약 930만 명입니다."}
    assert evaluate_case({**base, "answer_sources": "https://x"})["case_status"] == "INPUT_ERROR"
    plain, linked = evaluate_case(base), evaluate_case({**base, "answer_sources": [{"url": "https://kosis.kr/a"}]})
    assert linked["answer_sources"] == [{"url": "https://kosis.kr/a"}] and "answer_sources" not in plain
    assert linked["results"] == plain["results"] and linked["summary"] == plain["summary"]


def test_source_failure_detail_separates_access_failures_from_missing_quotes_without_changing_the_verdict():
    def record(*statuses):
        sources = [{"url": f"https://s{i}.example", "origin": "web", "quote_verified": False, "verification_status": s,
                    **({"retrieval": "provider_document"} if s == "quote_not_found" and i == 9 else {})}
                   for i, s in enumerate(statuses)]
        j5 = {"decision": "FAIL", "reason_code": "SOURCE_ORIGINAL_UNAVAILABLE",
              "fact_check": {"basis": "web", "claims": [{"verdict": "UNVERIFIED", "sources": sources}]},
              "decision_policy": {"failures": [{"claim_index": 0, "source_index": i, "status": s, "url": f"https://s{i}.example"}
                                               for i, s in enumerate(statuses)]}}
        return {"decisions": {"J5": j5}, "verification": {}}
    access = j5_outcome(record("http_error", "timeout"))
    assert (access["code"], access["source_failure"]["kind"], access["policy"]) == ("source_unavailable", "access_failed",
                                                                                     "j5-outcome-detail-v6")   # v6: 보류(held) 사유 추가
    assert j5_outcome(record("quote_not_found"))["source_failure"]["kind"] == "quote_not_found"
    mixed = j5_outcome(record("http_error", "quote_not_found"))["source_failure"]
    assert (mixed["kind"], mixed["quote_not_found"], mixed["access_failures"][0]["status"]) == ("mixed", 1, "http_error")
    assert record("http_error")["decisions"]["J5"]["decision"] == "FAIL"


