"""외부 답변 수집 변환 (4단계) — 공통 스키마·출처·HTML·웹·에이전트·API 어댑터. 모델·네트워크 호출 없음."""
from __future__ import annotations

import copy
import json

import pytest

from code_eval.engine import evaluate_case
from webapp.ingestion import BundleError, import_files, validate_bundle
from webapp.ingestion.html_markdown import html_to_markdown
from webapp.ingestion.sources import classify_url, evaluation_sources, normalize_sources, resolve_relays

SYSTEM = {"system_key": "chatgpt-web", "provider": "openai", "platform": "ChatGPT", "model": "gpt-5", "access_mode": "web"}


def bundle(**overrides):
    value = {"schema": "modelgrade-collection/v1",
             "questions": [{"case_id": "Q1", "question": "서울 인구는?"}],
             "systems": [copy.deepcopy(SYSTEM)],
             "responses": [{"case_id": "Q1", "system_key": "chatgpt-web", "answer": "약 930만 명입니다.",
                            "collection_method": "manual_copy"}]}
    value.update(overrides)
    return value


def load(*items, **kw):
    return import_files([{"name": name, "text": text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)}
                         for name, text in items], **kw)


# --- 공통 스키마 ---------------------------------------------------------------------------

def test_unknown_environment_values_stay_null_instead_of_being_guessed():
    b = validate_bundle(bundle())
    system, response = b["systems"][0], b["responses"][0]
    assert system["environment"]["web_search_available"] is None and system["environment"]["tools_available"] is None
    assert system["environment"]["system_instruction"] == {"known": False}
    assert response["collected_at"] is None and response["environment"] == {"web_search_used": None, "tools_used": None}
    assert response["usage"]["reported_cost"] is None and b["questions"][0]["dialogue"] == "single"


@pytest.mark.parametrize("change,message", [
    (lambda b: b["systems"][0].pop("provider"), "제공자(provider)"),
    (lambda b: b["systems"][0].pop("model"), "모델(model)"),
    (lambda b: b["systems"][0].update(system_key="gpt"), "예약 키"),
    (lambda b: b["responses"][0].update(answer="  "), "빈 답변"),
    (lambda b: b["responses"][0].pop("collection_method"), "수집 방식"),
    (lambda b: b["responses"][0].update(case_id="Q9"), "없는 case_id"),
    (lambda b: b["questions"].append({"case_id": "Q1", "question": "다른 질문"}), "중복"),
    (lambda b: b["questions"].extend([{"case_id": "A", "question": "a", "conversation_id": "c", "turn": 1},
                                      {"case_id": "B", "question": "b", "conversation_id": "c", "turn": 1}]), "턴이 중복"),
    (lambda b: b["responses"].append({**b["responses"][0], "answer": "다른 답변"}), "재실행"),
])
def test_missing_or_conflicting_inputs_return_explicit_validation_errors(change, message):
    value = bundle()
    change(value)
    with pytest.raises(BundleError) as error:
        validate_bundle(value)
    assert message in str(error.value)


def test_empty_answer_is_allowed_only_when_the_model_really_answered_nothing():
    value = bundle()
    value["responses"][0].update(answer="", empty_answer_confirmed=True)
    assert validate_bundle(value)["responses"][0]["status"] == "SUCCESS"
    value["responses"][0] = {"case_id": "Q1", "system_key": "chatgpt-web", "status": "ERROR", "collection_method": "api_live"}
    assert validate_bundle(value)["responses"][0]["error"] == "응답 수집에 실패했습니다."


def test_invalid_json_and_identical_duplicate_imports():
    with pytest.raises(ValueError, match="JSON"):
        load(("broken.json", '{"schema": '))
    value = bundle()
    value["responses"].append(copy.deepcopy(value["responses"][0]))
    b = load(("a.json", value), ("b.json", bundle()))
    assert len(b["responses"]) == 1 and b["report"]["duplicate_responses_dropped"] == 2


def test_same_question_from_different_platforms_is_one_question_and_case_id_clashes_are_errors():
    api = bundle(systems=[{**SYSTEM, "system_key": "gpt-api", "access_mode": "api", "platform": "OpenAI API"}],
                 questions=[{"case_id": "OTHER-ID", "question": "서울 인구는?"}],
                 responses=[{"case_id": "OTHER-ID", "system_key": "gpt-api", "answer": "930만", "collection_method": "api_live"}])
    merged = load(("web.json", bundle()), ("api.json", api))
    assert [q["case_id"] for q in merged["questions"]] == ["Q1"] and merged["questions"][0]["aliases"] == ["OTHER-ID"]
    assert {r["case_id"] for r in merged["responses"]} == {"Q1"}
    clash = copy.deepcopy(api)
    clash["questions"][0].update(case_id="Q1", question="부산 인구는?")
    clash["responses"][0]["case_id"] = "Q1"
    with pytest.raises(BundleError, match="다른 질문과 겹칩니다"):
        load(("web.json", bundle()), ("api.json", clash))


def test_a_partly_matching_conversation_is_not_grafted_onto_another():
    turns = lambda second: [{"case_id": f"T{i}", "question": q, "conversation_id": "c", "turn": i}
                            for i, q in enumerate(["첫 질문", second], 1)]
    a = bundle(questions=turns("둘째 질문"), responses=[
        {"case_id": f"T{i}", "system_key": "chatgpt-web", "answer": "답", "collection_method": "manual_copy"} for i in (1, 2)])
    b = bundle(systems=[{**SYSTEM, "system_key": "claude-web", "platform": "Claude.ai"}], questions=[
        {**q, "case_id": "X" + q["case_id"]} for q in turns("다른 둘째 질문")], responses=[
        {"case_id": f"XT{i}", "system_key": "claude-web", "answer": "답", "collection_method": "manual_copy"} for i in (1, 2)])
    merged = load(("a.json", a), ("b.json", b))
    assert sorted({(q["conversation_id"], q["turn"]) for q in merged["questions"]}) == [("c", 1), ("c", 2), ("c~2", 1), ("c~2", 2)]


# --- 출처 ---------------------------------------------------------------------------------

def test_platform_relay_and_original_urls_are_kept_apart():
    assert classify_url("https://a.example/x?utm_source=chatgpt.com&id=3")["original_url"] == "https://a.example/x?id=3"
    assert classify_url("https://a.example/x?utm_source=newsletter")["url_status"] == "original"
    unwrapped = classify_url("https://www.google.com/url?q=https://kosis.kr/p&sa=D")
    assert (unwrapped["relay_url"], unwrapped["original_url"], unwrapped["url_status"]) == (
        "https://www.google.com/url?q=https://kosis.kr/p&sa=D", "https://kosis.kr/p", "relay_unwrapped")
    opaque = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AbC"
    assert classify_url(opaque)["url_status"] == "relay_unresolved" and classify_url(opaque)["original_url"] is None


def test_unresolved_relay_missing_and_invalid_urls_are_recorded_not_used_as_evidence():
    answer = "서울 인구는 약 930만 명입니다. 부산은 330만 명입니다."
    sources, notes = normalize_sources([
        {"url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AbC", "claims": ["서울 인구는 약 930만 명입니다."]},
        {"title": "통계청 보고서"}, {"url": "htp:/broken"}, {"url": "https://kosis.kr/a", "marker": "부산은"},
        {"url": "https://kosis.kr/b", "claims": ["답변에 없는 문장"]}], answer, "sources")
    assert [s.get("skip_reason") for s in sources] == ["relay_unresolved", "no_url", None, None, None]
    assert sources[3]["claims"] == ["부산은 330만 명입니다."] and sources[4]["claims"] == []
    assert {n["code"] for n in notes} == {"relay_unresolved", "source_url_missing", "source_url_invalid"}
    passed = evaluation_sources(sources)
    assert [s.get("skip_reason") for s in passed] == ["relay_unresolved", None, None, None]


def test_resolving_a_relay_is_explicit_and_records_the_redirect():
    sources, _ = normalize_sources([{"url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AbC"}], "답", "s")
    notes = resolve_relays(sources, fetcher=lambda url: {"status": "redirect", "location": "https://kosis.kr/p?utm_source=gemini.google.com",
                                                         "checked_at": "2026-10-13T00:00:00+00:00"})
    assert not notes and sources[0]["url"] == "https://kosis.kr/p" and sources[0]["url_status"] == "relay_resolved"
    assert sources[0]["redirect"]["resolved_url"].startswith("https://kosis.kr/p") and "skip_reason" not in sources[0]
    failed, _ = normalize_sources([{"url": "https://t.co/x"}], "답", "s")
    assert resolve_relays(failed, fetcher=lambda url: {"status": "blocked_address"})[0]["code"] == "relay_resolve_failed"
    assert failed[0]["skip_reason"] == "relay_unresolved"


# --- HTML → Markdown ---------------------------------------------------------------------

HTML_ANSWER = """<div class="markdown"><h2>요약</h2><p>서울 인구는 <strong>약 930만</strong> 명입니다.<br>출처는 아래에 있습니다.</p>
<ul><li>하나<ul><li>하위</li></ul></li><li>둘</li></ul>
<table><thead><tr><th>도시</th><th style="text-align:right">인구</th></tr></thead>
<tbody><tr><td>서울</td><td>930만</td></tr><tr><td>부산</td><td>330만</td></tr></tbody></table>
<pre><div>python<button>Copy code</button></div><code class="language-python">def total(a, b):
    return a + b
</code></pre><blockquote><p>인용문</p></blockquote>
<p><a href="https://kosis.kr/a">통계청</a> 자료입니다<sup><a href="#fn1">1</a></sup>. 식 <span class="katex"><annotation encoding="application/x-tex">x^2</annotation><span aria-hidden="true">x2</span></span></p>
<section class="footnotes"><ol><li id="fn1"><p>2025년 기준 <a href="#fnref1" class="footnote-backref">↩</a></p></li></ol></section></div>"""


def test_html_structure_is_preserved_in_markdown():
    result = html_to_markdown(HTML_ANSWER)
    md = result["markdown"]
    for piece in ("## 요약", "**약 930만**", "- 하나\n  - 하위\n- 둘", "| 도시 | 인구 |\n| --- | ---: |\n| 서울 | 930만 |",
                  "```python\ndef total(a, b):\n    return a + b\n```", "> 인용문", "[통계청](https://kosis.kr/a)",
                  "자료입니다[^1]", "[^1]: 2025년 기준", "$x^2$", "명입니다.\n출처는"):
        assert piece in md
    assert "Copy code" not in md and [q["code"] for q in result["quality"]] == ["code_block_chrome_removed"]


def test_converted_tables_and_code_blocks_do_not_fail_code_checks():
    md = html_to_markdown(HTML_ANSWER)["markdown"]
    case = evaluate_case({"case_id": "H1", "question": "서울과 부산 인구를 표로 정리하고, 합계 함수를 python 코드 블록으로 보여줘.",
                          "answer": md})
    fails = [r["check_id"] for r in case["results"] if r["verdict"] == "FAIL"]
    assert case["case_status"] == "EVALUATED" and fails == []
    assert any(r["check_id"].startswith("1-2") and r["verdict"] == "PASS" for r in case["results"])


def test_unrepresentable_or_broken_html_is_recorded_as_a_collection_quality_issue():
    spans = html_to_markdown("<table><tr><th colspan='2'>합계</th></tr><tr><td>1</td><td>2</td></tr></table>")
    assert spans["markdown"].startswith("| 합계 |  |") and spans["quality"][0]["code"] == "table_span_unrepresentable"
    broken = html_to_markdown("<div><p>문단</div></span><b>굵게")
    assert "html_structure_repaired" in {q["code"] for q in broken["quality"]}
    empty = html_to_markdown("<div><button>복사</button><script>x()</script></div>")
    assert empty["markdown"] == "" and empty["quality"][-1]["severity"] == "error"


# --- 웹 내보내기·대화 기록·HTML 파일 ---------------------------------------------------------

def chatgpt_export():
    def node(nid, parent, children, role=None, text=None, **message):
        msg = None if role is None else {"id": "m" + nid, "author": {"role": role, **({"name": message.pop("name")} if "name" in message else {})},
                                          "content": {"content_type": message.pop("content_type", "text"), "parts": [text]},
                                          "metadata": message.pop("metadata", {}), **message}
        return nid, {"id": nid, "message": msg, "parent": parent, "children": children}
    cite = "citeturn0search1"
    return [{"title": "인구", "conversation_id": "conv-1", "current_node": "u2", "mapping": dict([
        node("root", None, ["u1"]),
        node("u1", "root", ["old", "call"], "user", "서울 인구 알려줘", create_time=1760000000),
        node("old", "u1", [], "assistant", "재생성 전 답변", recipient="all"),
        node("call", "u1", ["tool"], "assistant", "{}", recipient="web.run"),
        node("tool", "call", ["a1"], "tool", "검색 결과", name="web.run"),
        node("a1", "tool", ["u2"], "assistant", f"서울 인구는 약 930만 명입니다. {cite} 다음 문장.", recipient="all",
             create_time=1760000005, metadata={"model_slug": "gpt-5", "content_references": [
                 {"matched_text": cite, "type": "grouped_webpages",
                  "items": [{"url": "https://data.seoul.go.kr/p?utm_source=chatgpt.com", "title": "서울 열린데이터"}]}]}),
        node("u2", "a1", [], "user", "앞으로는 한 문장으로 답해줘. 부산은?", create_time=1760000010)])}]


def test_chatgpt_official_export_uses_the_visible_branch_and_keeps_the_original_text():
    b = load(("conversations.json", chatgpt_export()))
    first, second = b["responses"]
    assert [q["case_id"] for q in b["questions"]] == ["conv-1-T01", "conv-1-T02"]
    assert first["answer"] == "서울 인구는 약 930만 명입니다. 다음 문장." and "" in first["original"]["content"]
    assert first["sources"][0]["url"] == "https://data.seoul.go.kr/p"
    assert first["sources"][0]["claims"] == ["서울 인구는 약 930만 명입니다."]
    assert first["environment"] == {"web_search_used": True, "tools_used": ["web.run"]}
    assert first["collection_method"] == "official_export" and first["collected_at"].startswith("2025-10-09")
    assert second["status"] == "NOT_COLLECTED" and b["systems"][0]["model"] == "gpt-5"
    assert any("다른 분기 1개" in note for note in b["import"]["notes"])
    reordered = chatgpt_export()
    reordered[0]["mapping"]["u2"]["message"]["create_time"] = 1
    with pytest.raises(ValueError, match="시각이 뒤바뀌어"):
        load(("conversations.json", reordered))


def test_claude_export_and_html_page_with_message_roles():
    claude = [{"uuid": "c-1", "name": "t", "chat_messages": [
        {"sender": "human", "text": "질문", "created_at": "2026-10-01T00:00:00Z"},
        {"sender": "assistant", "created_at": "2026-10-01T00:00:05Z", "content": [
            {"type": "tool_use", "name": "web_search"},
            {"type": "text", "text": "답변입니다.", "citations": [{"url": "https://a.example/p", "title": "A"}]}]}]}]
    b = load(("conversations.json", claude), options={"system": {"model": "claude-opus-5-5"}})
    response = b["responses"][0]
    assert (b["systems"][0]["system_key"], b["systems"][0]["model"]) == ("claude-web", "claude-opus-5-5")
    assert response["sources"][0]["claims"] == ["답변입니다."] and response["environment"]["web_search_used"] is True
    page = ('<html><body><div data-message-author-role="user"><p>표로 줘</p></div>'
            '<div data-message-author-role="assistant"><div class="markdown"><table><tr><th>a</th></tr><tr><td>1</td></tr>'
            '</table></div></div></body></html>')
    html = load(("chat.html", page), options={"system": SYSTEM})
    assert html["responses"][0]["answer"] == "| a |\n| --- |\n| 1 |" and html["responses"][0]["original"]["format"] == "html"
    with pytest.raises(ValueError, match="질문 원문을 함께"):
        load(("answer.html", "<p>답변만 있는 HTML</p>"), options={"system": SYSTEM})


def transcript(*messages, **conversation):
    return {"schema": "modelgrade-transcript/v1", "system": SYSTEM,
            "conversations": [{"id": "c1", "messages": list(messages), **conversation}]}


def test_transcript_keeps_roles_apart_and_marks_missing_turns():
    b = load(("t.json", transcript({"role": "system", "content": "항상 존댓말"}, {"role": "user", "content": "셋째 질문"},
                                   {"role": "tool", "name": "search"}, {"role": "assistant", "content": "답"},
                                   missing_turns_before=2)))
    assert b["questions"][0]["question"] == "셋째 질문" and b["questions"][0]["turn"] == 3
    assert b["report"]["conversation_gaps"] == {"c1": [1, 2]}
    assert b["systems"][0]["environment"]["system_instruction"] == {"known": True, "text": "항상 존댓말"}
    assert b["responses"][0]["environment"]["tools_used"] == ["search"]
    middle = load(("t.json", transcript({"role": "user", "content": "1", "turn": 1}, {"role": "assistant", "content": "a"},
                                        {"role": "user", "content": "4", "turn": 4}, {"role": "assistant", "content": "b"})))
    assert middle["report"]["conversation_gaps"] == {"c1": [2, 3]}


@pytest.mark.parametrize("messages,message", [
    ([{"role": "assistant", "content": "먼저"}, {"role": "user", "content": "질문"}], "AI 답변이 먼저"),
    ([{"role": "user", "content": "a", "turn": 2}, {"role": "user", "content": "b", "turn": 2}], "순서 오류"),
    ([{"role": "user", "content": "a", "created_at": "2026-10-02T00:00:00Z"},
      {"role": "assistant", "content": "x", "created_at": "2026-10-01T00:00:00Z"}], "시각"),
    ([{"role": "user", "content": "a"}, {"role": "assistant", "content": "x"}, {"role": "assistant", "content": "y"}], "여러 개"),
])
def test_transcript_order_errors_are_rejected_instead_of_reordered(messages, message):
    with pytest.raises(ValueError, match=message):
        load(("t.json", transcript(*messages)))


# --- 에이전트 -----------------------------------------------------------------------------

def agent_run(**run):
    return {"schema": "modelgrade-agent-run/v1", "system": {"system_key": "research-agent", "provider": "openai",
                                                             "platform": "사내 에이전트", "model": "gpt-5"},
            "runs": [{"run_id": "r1", "request": "서울 인구 조사", "status": "completed", "final_answer": "약 930만 명입니다.",
                      "started_at": "2026-10-13T00:00:00Z", "ended_at": "2026-10-13T00:00:03Z",
                      "steps": [{"type": "tool", "name": "web_search", "status": "ok", "output": {"hits": 3},
                                 "sources": [{"url": "https://kosis.kr/a"}]},
                                {"type": "tool", "name": "fetch_page", "status": "error", "error": "HTTP 403"}], **run}]}


def test_agent_final_answer_is_evaluated_and_tool_steps_are_kept_as_evidence():
    response = load(("agent.json", agent_run()))["responses"][0]
    agent = response["agent"]
    assert response["status"] == "SUCCESS" and response["answer"] == "약 930만 명입니다."
    assert (agent["tool_calls"], agent["tool_errors"], agent["failed_steps"], agent["duration_ms"]) == (2, 1, [2], 3000.0)
    assert agent["retrieved_sources"] == [{"url": "https://kosis.kr/a", "title": None}] and response["sources"] == []
    assert response["environment"] == {"web_search_used": True, "tools_used": ["web_search", "fetch_page"]}
    assert response["collection_quality"][0]["code"] == "agent_tool_failures" and agent["evaluation_scope"] == "final_answer_only"
    failed = load(("agent.json", agent_run(status="failed", final_answer=None, error="timeout")))["responses"][0]
    assert failed["status"] == "ERROR" and "timeout" in failed["error"]
    partial = load(("agent.json", agent_run(status="cancelled")))["responses"][0]
    assert partial["status"] == "INCOMPLETE"


def test_agent_framework_traces_are_read_with_their_official_field_names():
    sdk = {"trace": {"id": "trace_1", "workflow_name": "Research"}, "spans": [
        {"object": "trace.span", "id": "s1", "trace_id": "trace_1", "started_at": "2026-10-13T00:00:00Z",
         "ended_at": "2026-10-13T00:00:09Z", "span_data": {"type": "agent", "name": "R", "tools": ["web_search"]}},
        {"object": "trace.span", "id": "s2", "started_at": "2026-10-13T00:00:01Z", "ended_at": "2026-10-13T00:00:02Z",
         "span_data": {"type": "generation", "model": "gpt-5", "input": [{"role": "user", "content": "요약해줘"}],
                       "output": [{"role": "assistant", "content": [{"type": "output_text", "text": "요약입니다."}]}],
                       "usage": {"input_tokens": 3, "output_tokens": 4}}},
        {"object": "trace.span", "id": "s3", "started_at": "2026-10-13T00:00:03Z", "ended_at": "2026-10-13T00:00:04Z",
         "span_data": {"type": "function", "name": "lookup", "input": "{}", "output": "ok"}}]}
    sdk_response = load(("trace.json", sdk))["responses"][0]
    assert sdk_response["answer"] == "요약입니다." and sdk_response["usage"]["input_tokens"] == 3
    assert sdk_response["agent"]["tools_available"] == ["web_search"] and sdk_response["agent"]["tool_calls"] == 1
    langsmith = {"id": "root", "name": "qa", "run_type": "chain", "inputs": {"question": "질문"}, "outputs": {"output": "답"},
                 "start_time": "2026-10-13T00:00:00", "end_time": "2026-10-13T00:00:01", "status": "success",
                 "child_runs": [{"id": "c", "parent_run_id": "root", "run_type": "tool", "name": "search", "error": "boom"}]}
    ls = load(("run.json", langsmith))["responses"][0]
    assert ls["answer"] == "답" and ls["agent"]["tool_errors"] == 1
    otel = [{"spanId": "a", "name": "invoke_agent", "attributes": [
        {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
        {"key": "gen_ai.input.messages", "value": {"stringValue": json.dumps([{"role": "user", "parts": [{"type": "text", "content": "질문"}]}])}},
        {"key": "gen_ai.output.messages", "value": {"stringValue": json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": "답"}]}])}}]},
        {"spanId": "b", "parentSpanId": "a", "name": "tool", "attributes": {"gen_ai.operation.name": "execute_tool",
                                                                           "gen_ai.tool.name": "search"}}]
    ot = load(("otel.json", otel), options={"system": {"model": "m"}})["responses"][0]
    assert ot["answer"] == "답" and ot["environment"]["tools_used"] == ["search"]


# --- API 응답 -----------------------------------------------------------------------------

def api_file(response, request=None, **system):
    return {"schema": "modelgrade-api-responses/v1", "system": {"system_key": "api-model", "platform": "API", **system},
            "items": [{"case_id": "Q1", "question": "서울 인구는?", "response": response, **({"request": request} if request else {})}]}


def test_api_responses_keep_usage_citations_and_tool_use():
    openai = {"object": "response", "status": "completed", "model": "gpt-5-2026", "created_at": 1760000100,
              "tools": [{"type": "web_search"}], "output": [{"type": "web_search_call", "status": "completed"},
              {"type": "message", "content": [{"type": "output_text", "text": "서울 인구는 약 930만 명입니다.", "annotations": [
                  {"type": "url_citation", "url": "https://kosis.kr/a", "title": "KOSIS", "start_index": 0, "end_index": 17}]}]}],
              "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}}
    b = load(("api.json", api_file(openai, {"tools": [{"type": "web_search"}], "instructions": "간결히"})))
    response, system = b["responses"][0], b["systems"][0]
    assert (system["provider"], system["model"], system["environment"]["web_search_available"]) == ("openai", "gpt-5-2026", True)
    assert system["environment"]["system_instruction"] == {"known": True, "text": "간결히"}
    assert response["sources"][0]["claims"] == ["서울 인구는 약 930만 명입니다."] and response["usage"]["total_tokens"] == 30
    assert response["environment"] == {"web_search_used": True, "tools_used": ["web_search"]} and response["raw"] == openai
    anthropic = {"type": "message", "model": "claude-x", "stop_reason": "end_turn", "content": [
        {"type": "server_tool_use", "name": "web_search"},
        {"type": "text", "text": "약 930만 명입니다.", "citations": [{"type": "web_search_result_location", "url": "https://a.example"}]}],
        "usage": {"input_tokens": 1, "output_tokens": 2, "server_tool_use": {"web_search_requests": 1}}}
    a = load(("api.json", api_file(anthropic)))["responses"][0]
    assert a["sources"][0]["claims"] == ["약 930만 명입니다."] and a["usage"]["search_requests"] == 1
    gemini = {"modelVersion": "gemini-x", "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "약 930만 명입니다."}]},
              "groundingMetadata": {"webSearchQueries": ["서울 인구"], "groundingChunks": [
                  {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/X", "title": "kosis.kr"}}],
                  "groundingSupports": [{"segment": {"text": "약 930만 명입니다."}, "groundingChunkIndices": [0]}]}}]}
    g = load(("api.json", api_file(gemini)))["responses"][0]
    assert g["sources"][0]["skip_reason"] == "relay_unresolved" and g["sources"][0]["claims"] == ["약 930만 명입니다."]
    assert {q["code"] for q in g["collection_quality"]} >= {"gemini_grounding_terms", "relay_unresolved"}
    sonar = {"choices": [{"finish_reason": "stop", "message": {"content": "약 930만 명입니다[1]."}}], "model": "sonar",
             "search_results": [{"title": "KOSIS", "url": "https://kosis.kr/a", "date": "2026-01-01"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 2, "cost": {"total_cost": 0.006}}}
    p = load(("api.json", api_file(sonar)))["responses"][0]
    assert p["sources"][0]["marker"] == "[1]" and p["usage"]["reported_cost"] == {"amount": 0.006, "currency": "USD",
                                                                                  "source": "provider_response"}
    with pytest.raises(ValueError, match="질문과 함께"):
        load(("raw.json", openai))


def test_api_call_failure_and_truncation_are_collection_states_not_answers():
    b = load(("api.json", {"schema": "modelgrade-api-responses/v1", "system": {"system_key": "x", "provider": "openai",
                                                                                "platform": "API", "model": "m"},
                           "items": [{"case_id": "Q1", "question": "질문", "error": "HTTP 429"},
                                     {"case_id": "Q2", "question": "질문2", "response": {"object": "response", "status": "incomplete",
                                      "incomplete_details": {"reason": "max_output_tokens"}, "output": [
                                          {"type": "message", "content": [{"type": "output_text", "text": "중간까"}]}]}}]}))
    assert [(r["status"], r["error"]) for r in b["responses"]] == [
        ("ERROR", "HTTP 429"), ("INCOMPLETE", "응답 미완료 (incomplete:max_output_tokens). 원본 응답은 보존했습니다.")]


# --- 기존 형식 호환 -----------------------------------------------------------------------

def test_legacy_qa_files_need_model_information_and_old_runs_convert_without_new_versions():
    with pytest.raises(BundleError, match="제공자"):
        load(("qa.json", [{"question": "질문", "answer": "답"}]))
    qa = load(("qa.md", "Q: 질문\nA: 답\n"), options={"system": {**SYSTEM, "system_key": "legacy-web"}})
    assert qa["responses"][0]["collection_method"] == "legacy_import" and qa["responses"][0]["answer"] == "답\n"
    run = {"run_id": "abc", "providers": ["aiona", "gpt"], "models": {"aiona": "업로드 원문", "gpt": "gpt-x"}, "max_tokens": 8192,
           "aiona_release": {"version": "v3"},
           "datasets": {"aiona": [{"case_id": "Q1", "question": "질문", "answer": "AIONA 답"}],
                        "gpt": [{"case_id": "Q1", "question": "질문", "answer": "GPT 답", "system_id": "gpt",
                                 "collection": {"status": "SUCCESS", "latency_ms": 12.0, "raw": {
                                     "object": "response", "status": "completed", "output": [{"type": "message", "content": [
                                         {"type": "output_text", "text": "GPT 답"}]}], "usage": {"input_tokens": 1, "output_tokens": 2}}}}]}}
    b = load(("run.json", run))
    systems = {s["system_key"]: s for s in b["systems"]}
    assert set(systems) == {"aiona-service", "gpt-api"} and systems["aiona-service"]["model_version"] == "v3"
    gpt = next(r for r in b["responses"] if r["system_key"] == "gpt-api")
    assert gpt["usage"]["latency_ms"] == 12.0 and gpt["environment"]["web_search_used"] is False and gpt["collected_at"] is None


def test_public_samples_load_and_the_invalid_sample_reports_every_missing_field():
    from pathlib import Path
    folder = Path(__file__).resolve().parent.parent / "examples" / "external"
    valid = sorted(p for p in folder.glob("*.sample.json") if p.name != "invalid.sample.json")
    merged = import_files([{"name": p.name, "text": p.read_text(encoding="utf-8")} for p in valid])
    assert {s["system_key"] for s in merged["systems"]} == {"chatgpt-web", "gpt-api", "perplexity-web", "gpt-api-search", "research-agent"}
    assert merged["report"]["conversation_gaps"] == {"PX-01": [1]}
    with pytest.raises(BundleError) as error:
        load(("invalid.sample.json", (folder / "invalid.sample.json").read_text(encoding="utf-8")))
    assert {e["path"].split("› ")[-1] for e in error.value.errors} == {"systems[0].provider", "systems[0].model"}


def test_relay_lookup_keeps_the_public_address_and_port_guards_without_network():
    from webapp.ingestion.sources import single_hop
    assert single_hop("http://127.0.0.1/redirect")["status"] == "blocked_address"
    assert single_hop("https://example.com:8443/redirect")["status"] == "blocked_port"
    assert single_hop("file:///etc/passwd")["status"] == "blocked_url"
