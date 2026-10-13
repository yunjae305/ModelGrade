"""외부 수집 답변 → 기존 CODE·J1~J6 평가 → 저장·복원 (4단계). 수집 모델·평가 Judge 는 모두 대역, 유료 호출 없음.

CODE 검사는 실제 엔진이 돌고, 의미 평가(J1~J6)는 고정 대역 판정이다. 실제 Claude 판정 품질은 이 테스트가 확인하지 않는다.
"""
from __future__ import annotations

import copy
import json
import time

import httpx
import pytest

from code_eval.anthropic_semantic_client import FINAL_TOOL, AnthropicSemanticClient, AnthropicSemanticConfig
from code_eval.config import AionaConfig
from code_eval.engine import evaluate_batch, evaluate_case
from code_eval.jev_eval import evaluate_jev_case
from code_eval.semantic_policy import j5_outcome
from semantic_judge_fixture import Judge
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService, _external_semantic_context

WEB = {"system_key": "chatgpt-web", "provider": "openai", "platform": "ChatGPT", "model": "gpt-5", "access_mode": "web"}


def service(tmp_path, judge=None, answer_text="서울 인구는 약 930만 명입니다."):
    judge = judge or Judge()
    evaluator = CodeEvalService(save_dir=tmp_path / "eval", jev_resolver=lambda **_: (judge, judge.config, None))
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "object": "response", "status": "completed", "model": "gpt-x-2026", "created_at": 1760000000,
            "output": [{"type": "message", "content": [{"type": "output_text", "text": answer_text, "annotations": [
                {"type": "url_citation", "url": "https://kosis.kr/a", "title": "KOSIS", "start_index": 0, "end_index": 10}]}]}],
            "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}})
    svc = ComparisonService(evaluator, save_dir=tmp_path / "runs",
                            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(provider)))
    return svc, judge, requests


def wait(svc, run_id):
    for _ in range(300):
        if svc.status(run_id)["status"] in ("COMPLETED", "ERROR", "CANCELLED"):
            return svc.result(run_id)
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def files(*items):
    return [{"name": name, "text": json.dumps(value, ensure_ascii=False)} for name, value in items]


def transcript(*conversations, system=WEB):
    return {"schema": "modelgrade-transcript/v1", "system": system,
            "conversations": [{"id": cid, "messages": messages, **extra} for cid, messages, extra in conversations]}


def test_api_answers_are_collected_converted_evaluated_and_saved(tmp_path):
    svc, judge, requests = service(tmp_path)
    svc.configure("gpt", {"key": "sk-test-not-real", "model": "gpt-x"})
    questions = {"schema": "modelgrade-collection/v1", "questions": [{"case_id": "Q1", "question": "서울 인구를 알려줘"}],
                 "systems": [], "responses": []}
    preview = svc.preview_external({"files": files(("q.json", questions)), "api_providers": ["gpt"]})
    assert preview["api_collection"]["planned_calls"] == 1 and not requests   # 미리보기는 호출하지 않는다
    result = wait(svc, svc.start_external({"files": files(("q.json", questions)), "api_providers": ["gpt"]})["run_id"])
    row = result["datasets"]["gpt-api"][0]
    assert result["status"] == "COMPLETED" and requests[0]["input"] == [{"role": "user", "content": "서울 인구를 알려줘"}]
    assert "tools" not in requests[0] and row["collection"]["collection_method"] == "api_live"
    assert row["collection"]["usage"] == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
                                          "latency_ms": row["collection"]["usage"]["latency_ms"]}
    assert row["collection"]["collected_at"] and row["answer_sources"][0]["url"] == "https://kosis.kr/a"
    system = result["systems"]["gpt-api"]
    assert system["environment"]["web_search_available"] is False and system["collection"]["generation_usage"]["input_tokens"] == 11
    assert result["evaluations"]["gpt-api"]["cases"][0]["case_status"] == "EVALUATED" and len(judge.calls) == 1
    assert "sk-test-not-real" not in (tmp_path / "runs" / f"{result['run_id']}.json").read_text(encoding="utf-8")
    restored = ComparisonService(CodeEvalService(save_dir=tmp_path / "eval2"), save_dir=tmp_path / "runs").result(result["run_id"])
    assert restored["run_kind"] == "external_import" and restored["evaluations"]["gpt-api"]["cases"][0]["answer"] == row["answer"]


def test_web_html_and_json_answers_keep_sources_and_originals_through_evaluation(tmp_path):
    svc, judge, _ = service(tmp_path)
    html = ("<p>서울 인구는 약 930만 명입니다.</p><table><tr><th>도시</th><th>인구</th></tr><tr><td>서울</td><td>930만</td></tr></table>")
    web = transcript(("c1", [{"role": "user", "content": "서울 인구를 표로 알려줘"},
                             {"role": "assistant", "format": "html", "content": html, "created_at": "2026-10-12T00:00:00Z",
                              "sources": [{"url": "https://data.seoul.go.kr/p?utm_source=chatgpt.com",
                                           "claims": ["서울 인구는 약 930만 명입니다."]}]}], {}))
    result = wait(svc, svc.start_external({"files": files(("web.json", web)), "name": "웹 답변"})["run_id"])
    row = result["datasets"]["chatgpt-web"][0]
    assert row["collection"]["original"] == {"format": "html", "content": html}
    assert row["answer"].endswith("| 서울 | 930만 |") and row["answer_sources"][0]["url"] == "https://data.seoul.go.kr/p"
    case = result["evaluations"]["chatgpt-web"]["cases"][0]
    assert case["answer_sources"][0]["display_url"].endswith("utm_source=chatgpt.com")
    assert [r["check_id"] for r in case["results"] if r["verdict"] == "FAIL"] == []
    assert result["evaluation_summary"]["total"] == 1 and result["input_mode"] == "external_import"


def test_agent_final_answer_is_evaluated_and_failed_runs_are_excluded_not_failed(tmp_path):
    svc, judge, _ = service(tmp_path)
    agent = {"schema": "modelgrade-agent-run/v1", "system": {"system_key": "agent-x", "provider": "openai", "platform": "사내",
                                                             "model": "gpt-5"},
             "runs": [{"run_id": "ok", "case_id": "A1", "request": "서울 인구는?", "status": "completed",
                       "final_answer": "약 930만 명입니다.", "steps": [{"type": "tool", "name": "web_search", "status": "ok"}]},
                      {"run_id": "bad", "case_id": "A2", "request": "부산 인구는?", "status": "failed", "error": "tool crash",
                       "steps": [{"type": "tool", "name": "web_search", "status": "error", "error": "500"}]}]}
    result = wait(svc, svc.start_external({"files": files(("agent.json", agent))})["run_id"])
    cases = result["evaluations"]["agent-x"]["cases"]
    assert [c["case_status"] for c in cases] == ["EVALUATED", "INPUT_ERROR"] and len(judge.calls) == 1
    assert result["datasets"]["agent-x"][0]["collection"]["agent"]["tool_calls"] == 1
    assert result["datasets"]["agent-x"][1]["collection"]["status"] == "ERROR"


def test_external_conversations_keep_turns_inheritance_and_missing_history(tmp_path):
    svc, judge, _ = service(tmp_path)
    turns = [{"role": "user", "content": "앞으로 답변은 2문장 이내로 작성해줘."}, {"role": "assistant", "content": "알겠습니다."}]
    for i in range(2, 12):
        turns += [{"role": "user", "content": f"{i}번째 질문에 답해줘."}, {"role": "assistant", "content": "짧게 답합니다."}]
    turns += [{"role": "user", "content": "봄을 설명해줘."}, {"role": "assistant", "content": "봄은 따뜻합니다. 꽃이 핍니다. 바람이 붑니다."}]
    partial = [{"role": "user", "content": "이어서 설명해줘."}, {"role": "assistant", "content": "네."}]
    data = transcript(("long", turns, {}), ("cut", partial, {"missing_turns_before": 2}))
    result = wait(svc, svc.start_external({"files": files(("t.json", data))})["run_id"])
    cases = result["evaluations"]["chatgpt-web"]["cases"]
    records = result["evaluations"]["chatgpt-web"]["jev_eval"]["records"]
    last = next(c for c in cases if c["case_id"] == "long-T12")
    [row] = [r for r in last["results"] if r["check_id"].startswith("2-1.count:sentence_count")]
    assert row["verdict"] == "FAIL" and row["details"]["inherited_from"]["turn"] == 1
    histories = [call["state"].get("history") for call in judge.calls]
    assert max(len(h or []) for h in histories) == 11   # 12턴째는 앞 11턴의 질문·답변을 그대로 받는다
    cut = records[[c["case_id"] for c in cases].index("cut-T03")]
    assert cut["decisions"]["J6"]["status"] == "UNVERIFIED" and "누락 1~2턴" in cut["decisions"]["J6"]["reason"]
    assert result["environment_comparison"]["differences"][0]["code"] == "conversation_gaps"


def test_duplicate_import_is_blocked_and_a_rerun_is_a_separate_record(tmp_path):
    svc, _, _ = service(tmp_path)
    data = transcript(("c1", [{"role": "user", "content": "질문"}, {"role": "assistant", "content": "답변입니다."}], {}))
    first = wait(svc, svc.start_external({"files": files(("t.json", data))})["run_id"])
    assert svc.preview_external({"files": files(("t.json", data))})["duplicate_of"] == first["run_id"]
    with pytest.raises(ValueError, match="이미 있습니다"):
        svc.start_external({"files": files(("again.json", data))})
    second = wait(svc, svc.start_external({"files": files(("t.json", data)), "rerun": True})["run_id"])
    assert second["run_id"] != first["run_id"] and second["external_import"]["rerun_of"] == first["run_id"]
    assert len([r for r in svc.history() if r.get("run_kind") == "external_import"]) == 2


def test_environment_differences_are_reported_and_versions_stay_separate(tmp_path):
    svc, _, _ = service(tmp_path)
    svc.configure("gpt", {"key": "sk-test", "model": "gpt-x"})
    web = transcript(("c1", [{"role": "user", "content": "서울 인구를 알려줘"}, {"role": "assistant", "content": "약 930만 명입니다."}], {}))
    result = wait(svc, svc.start_external({"files": files(("web.json", web)), "api_providers": ["gpt"]})["run_id"])
    comparison = result["environment_comparison"]
    assert comparison["same_conditions"] is False
    assert {d["code"] for d in comparison["differences"]} >= {"access_mode", "web_search_available", "collection_time"}
    assert result["providers"] == ["chatgpt-web", "gpt-api"] and result["aiona_release"] is None
    history = svc.version_history()
    assert history["versions"] == [] and history["current_release"] is None
    # 이력 관리(AIONA 결과 이력)에는 나오지 않고, 실행 목록 API 에는 남는다
    assert svc.aiona_history()["entries"] == []
    assert result["run_id"] in [run["run_id"] for run in svc.history()]
    # items 의미 평가 문맥: 도구가 꺼진 것으로 확인된 API 수집만 none, 수집 시각을 모르면 날짜도 비운다
    api = result["systems"]["gpt-api"]
    assert _external_semantic_context(api) == {"answer_tools": "none", "collected_at": api["collection"]["collected_from"][:10]}
    assert _external_semantic_context(result["systems"]["chatgpt-web"]) == {"answer_tools": "unknown", "collected_at": None}
    gap = transcript(("cut", [{"role": "user", "content": "이어서"}, {"role": "assistant", "content": "네"}], {"missing_turns_before": 1}))
    with pytest.raises(ValueError, match="대화 맥락을 재현할 수 없습니다"):
        svc.start_external({"files": files(("gap.json", gap)), "api_providers": ["gpt"]})


def test_existing_four_model_runs_keep_their_aiona_only_summary(tmp_path):
    svc, _, _ = service(tmp_path)
    run = {"question_count": 1, "providers": ["aiona", "gpt"], "evaluations": {}}
    assert svc._evaluation_summary(run)["total"] == 1
    assert svc._evaluation_summary({"question_count": 2, "providers": ["a-web", "b-api"], "evaluations": {}})["total"] == 4


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
                                                                                     "j5-outcome-detail-v4")
    assert j5_outcome(record("quote_not_found"))["source_failure"]["kind"] == "quote_not_found"
    mixed = j5_outcome(record("http_error", "quote_not_found"))["source_failure"]
    assert (mixed["kind"], mixed["quote_not_found"], mixed["access_failures"][0]["status"]) == ("mixed", 1, "http_error")
    assert record("http_error")["decisions"]["J5"]["decision"] == "FAIL"


def test_five_turn_external_conversation_follows_instruction_changes_and_cancellation(tmp_path):
    svc, _, _ = service(tmp_path)
    table = "| 계절 | 특징 |\n|---|---|\n| 봄 | 따뜻함 |"
    pairs = [("앞으로 3문장 이내로 답해줘.", "네."), ("앞으로는 5문장 이내로 답해줘.", "네."), ("봄을 설명해줘.", "a. b. c. d."),
             ("앞으로 모든 답변은 표로 작성해 줘.", table), ("이제 표로 안 해도 돼. 여름을 설명해줘.", "여름은 덥습니다.")]
    messages = [m for q, a in pairs for m in ({"role": "user", "content": q}, {"role": "assistant", "content": a})]
    result = wait(svc, svc.start_external({"files": files(("t.json", transcript(("five", messages, {}))))})["run_id"])
    cases = {c["case_id"]: c for c in result["evaluations"]["chatgpt-web"]["cases"]}
    inherited = lambda cid: [(r["check_id"], r["verdict"], r["details"]["inherited_from"]["turn"])
                             for r in cases[cid]["results"] if r["source"] == "history_instruction"]
    assert inherited("five-T03") == [("2-1.count:sentence_count", "PASS", 2)]       # 바뀐 지시(5문장)가 적용
    assert not any(k.startswith("2-2.table") for k, _, _ in inherited("five-T05"))   # 해제한 표 지시는 승계하지 않음
    assert [c["conversation"]["turn"] for c in cases.values()] == [1, 2, 3, 4, 5]
