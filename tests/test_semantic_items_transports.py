"""네 transport(Claude API · OpenAI API · Claude CLI · Codex CLI)의 items 분기 — HTTP 는 MockTransport, CLI 는 대역 runner. 유료 호출 없음.

확인하는 것: items 계약(questions 값이 type=item)이면 items 시스템 지시문·항목 프롬프트·평평한 {"items": […]} strict 스키마를 쓰고,
검색이 꺼진 설정(엔진에 검색이 없거나 web_search=disabled 이거나 reference 가 있음)에서는 J5-1.a 를 보내지 않고
verification.items_not_sent 에 남기며, 응답 본문은 {model, items, verification} 로 server_check(J5 서버 검증)를 거쳐 돌아온다.
"""
import json
from types import SimpleNamespace

import httpx
import pytest

from code_eval import semantic_cli_common as common
from code_eval.anthropic_semantic_client import FINAL_TOOL, AnthropicSemanticClient, AnthropicSemanticConfig
from code_eval.claude_cli_client import ClaudeCliClient, ClaudeCliConfig
from code_eval.codex_cli_client import CodexCliClient, CodexCliConfig
from code_eval.semantic_api_client import SemanticApiClient, SemanticApiConfig
from code_eval.semantic_items import SPECS_BY_ID
from code_eval.semantic_items_prompt import ITEMS_SYSTEM_PROMPT, items_output_schema, items_system_prompt

QUESTION = "저녁에 커피 마시면 잠을 잘 못 자는 이유가 뭐야? 카페인이 몸에 얼마나 오래 남아?"
ANSWER = "카페인은 아데노신 수용체를 막습니다. 성인 기준 반감기는 약 5시간입니다. 출처 https://official.example/caffeine 를 확인했습니다."
STATE = {"question": QUESTION, "answer": ANSWER, "collected_at": "2026-10-01"}
IDS = ("J1-1.a", "J3-3.b", "J5-1.a", "J5-3.a")


def questions(*ids, needs_search=("J5-1.a",)):
    return {item_id: SPECS_BY_ID[item_id].to_prompt_dict({"rules": ["규칙"], "matches": ["이유가 뭐"], "needs_search": item_id in needs_search,
                                                          **({"answer_tools": "none"} if item_id == "J5-3.a" else {})})
            for item_id in ids}


def item(item_id, decision="YES", question_quote="이유가 뭐", answer_quote="카페인은 아데노신", reason="r", fact=None):
    return {"id": item_id, "decision": decision, "question_quote": question_quote, "answer_quote": answer_quote, "reason": reason,
            "turn": None, "fact": fact}


def answered(ids):
    out = []
    for item_id in ids:
        if item_id == "J5-1.a":
            out.append(item("J5-1.a", "NOT_APPLICABLE", "", "", "주장 없음"))
        elif item_id == "J3-3.b":
            out.append(item("J3-3.b", "NOT_APPLICABLE", "", "", "해당 없음"))
        else:
            out.append(item(item_id))
    return out


def fetcher_not_found(url):
    return {"status": "http_error", "text": "", "final_url": None, "checked_at": "2026-10-09T00:00:00+00:00", "content_type": None}


# --- Claude Messages API ----------------------------------------------------------------------------------------

def anthropic_response(items, **extra):
    return {"model": "claude-opus-5-5", "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "final", "name": FINAL_TOOL, "input": {"items": items}}],
            "usage": {"input_tokens": 100, "output_tokens": 50, "server_tool_use": {"web_search_requests": 0}}, **extra}


def anthropic_call(raw, qs, *, config=None, state=None, fetcher=fetcher_not_found):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=raw)
    client = AnthropicSemanticClient(config or AnthropicSemanticConfig(model="claude-opus-5-5", reasoning_effort="high"), "secret-test-key",
                                     transport=httpx.MockTransport(handler), source_fetcher=fetcher)
    return client.system_one(state or STATE, qs), sent


def test_anthropic_items_request_uses_item_prompt_flat_schema_and_search_tools_when_live():
    qs = questions(*IDS)
    call, sent = anthropic_call(anthropic_response(answered(IDS)), qs)
    body = json.loads(sent[0].content)
    assert [tool["name"] for tool in body["tools"]] == ["web_search", "web_fetch", FINAL_TOOL]
    schema = body["tools"][-1]["input_schema"]
    assert schema["required"] == ["items"] and schema["properties"]["items"]["items"]["properties"]["decision"]["enum"] == ["YES", "NO", "NOT_APPLICABLE"]
    assert schema["properties"]["items"]["items"]["properties"]["id"]["description"].endswith("보낸 항목: J1-1.a, J3-3.b, J5-1.a, J5-3.a")
    assert body["system"].startswith("당신은 AI 답변 품질 평가기의 의미 평가 단계입니다.\n- 아래 점검 항목에만 답합니다.")
    assert "web_search와 web_fetch 도구를 사용할 수 있습니다" in body["system"] and "UNVERIFIED" not in body["system"]
    assert body["system"].endswith("항목마다 YES / NO / NOT_APPLICABLE 과 원문 인용을 적으세요.")
    prompt = body["messages"][0]["content"]
    assert prompt.startswith("## 점검 항목 (코드 규칙이 이 질문에 적용한 것)") and "### J5-1.a " in prompt and "## J5 사실 검증" in prompt
    assert "## 평가 대상\ncollected_at: 2026-10-01\n" in prompt and json.loads(prompt.split("collected_at: 2026-10-01\n", 1)[1]) == STATE
    assert "실행 기준 시각" not in prompt and "## 기준" not in prompt
    assert call.status == "OK" and call.body["model"] == "claude-opus-5-5" and "answers" not in call.body
    assert [it["id"] for it in call.body["items"]] == list(IDS) and "items_not_sent" not in call.body["verification"]
    assert call.body["verification"]["output_transport"] == "strict_tool" and call.body["verification"]["fact_policy"]
    # J5 서버 검증: 항목에 server 블록, J5-3.a 는 답변의 출처 URL 존재 확인
    j53 = next(it for it in call.body["items"] if it["id"] == "J5-3.a")
    assert j53["server"] == {"problems": [], "links": {"https://official.example/caffeine": "not_found"}}
    assert next(it for it in call.body["items"] if it["id"] == "J1-1.a").get("server") is None
    assert call.body["verification"]["usage_requests"][0]["web_search_calls"] == 0


def test_anthropic_items_without_live_search_or_with_reference_drops_the_fact_item_and_records_it():
    qs = questions(*IDS)
    disabled = AnthropicSemanticConfig(web_search="disabled")
    call, sent = anthropic_call(anthropic_response(answered(("J1-1.a", "J3-3.b", "J5-3.a"))), qs, config=disabled)
    body = json.loads(sent[0].content)
    assert [tool["name"] for tool in body["tools"]] == [FINAL_TOOL]
    assert body["tools"][0]["input_schema"]["properties"]["items"]["items"]["properties"]["id"]["description"].endswith("보낸 항목: J1-1.a, J3-3.b, J5-3.a")
    assert "### J5-1.a" not in body["messages"][0]["content"] and "이 요청에서는 웹검색 도구를 쓸 수 없습니다." in body["messages"][0]["content"]
    assert "파일·명령·검색 도구를 사용하지 않습니다." in body["system"]
    assert call.status == "OK" and call.body["verification"]["items_not_sent"] == [{"id": "J5-1.a", "reason": common.SEARCH_NOT_AVAILABLE_REASON}]
    assert [it["id"] for it in call.body["items"]] == ["J1-1.a", "J3-3.b", "J5-3.a"]
    assert "J5-1.a" in qs   # 호출자의 questions 는 그대로
    with_reference = {**STATE, "reference": {"half_life_hours": 5}}
    call, sent = anthropic_call(anthropic_response(answered(("J1-1.a", "J3-3.b", "J5-3.a"))), qs, state=with_reference)
    assert [tool["name"] for tool in json.loads(sent[0].content)["tools"]] == [FINAL_TOOL]
    assert call.body["verification"]["items_not_sent"][0]["id"] == "J5-1.a"
    # 오류 응답에도 items_not_sent 가 남아 jev_eval 이 '실행하지 않음' 으로 집계할 수 있다
    call, _ = anthropic_call({"model": "claude-opus-5-5", "stop_reason": "refusal", "content": [], "usage": {}}, qs, config=disabled)
    assert call.status == "INVALID_RESPONSE" and call.body["verification"]["items_not_sent"][0]["id"] == "J5-1.a"


def test_anthropic_items_rejects_non_item_shapes_and_accepts_validated_text_json():
    qs = questions("J1-1.a")
    wrong = anthropic_response([{"id": "J1-1.a", "decision": "PASS"}])
    call, _ = anthropic_call(wrong, qs)
    assert call.status == "INVALID_RESPONSE" and "기준 ID·모델 기록" in call.error
    legacy_shape = {"model": "claude-opus-5-5", "stop_reason": "tool_use", "usage": {},
                    "content": [{"type": "tool_use", "id": "f", "name": FINAL_TOOL, "input": {"J1-1.a": {"choice": "PASS"}}}]}
    assert anthropic_call(legacy_shape, qs)[0].status == "INVALID_RESPONSE"
    text = {"model": "claude-opus-5-5", "stop_reason": "end_turn", "usage": {},
            "content": [{"type": "text", "text": json.dumps({"items": [item("J1-1.a")]}, ensure_ascii=False)}]}
    call, _ = anthropic_call(text, qs)
    assert call.status == "OK" and call.body["verification"]["output_transport"] == "validated_text_json"
    assert call.body["items"] == [item("J1-1.a")] and "server" not in call.body["items"][0]   # J5 를 보내지 않으면 서버 검증 없음
    assert "fact_policy" not in call.body["verification"]
    bad_text = {**text, "content": [{"type": "text", "text": json.dumps({"items": [item("J1-1.a", fact={"claim": "x"})]})}]}
    assert anthropic_call(bad_text, qs)[0].status == "INVALID_RESPONSE"


# --- OpenAI Responses API ---------------------------------------------------------------------------------------

def openai_response(items, **extra):
    return {"id": "resp", "status": "completed", "model": "resolved-judge-v1",
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps({"items": items}, ensure_ascii=False), "annotations": []}]}],
            "usage": {"input_tokens": 150, "output_tokens": 60}, **extra}


def openai_call(raw, qs, *, config=None, state=None):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=raw)
    client = SemanticApiClient(config or SemanticApiConfig(), "sk-semantic-fixture-only", transport=httpx.MockTransport(handler),
                               source_fetcher=fetcher_not_found)
    return client.system_one(state or STATE, qs), requests


def test_openai_items_request_schema_prompt_and_no_j5_recovery_round():
    qs = questions(*IDS)
    call, requests = openai_call(openai_response(answered(IDS)), qs)
    assert len(requests) == 1   # items 는 J5 전용 보강(evidence recovery)을 돌지 않는다
    body = json.loads(requests[0].content)
    assert body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["schema"]["required"] == ["items"]
    assert body["instructions"] == items_system_prompt(True)
    static, dynamic = (part["text"] for part in body["input"][0]["content"])   # 기본 모델은 static(캐시 경계)·dynamic 두 조각
    assert static.startswith("## 점검 항목") and "### J5-1.a " in static and [t["type"] for t in body["tools"]] == ["web_search"]
    assert dynamic.startswith("\n## 평가 대상\ncollected_at: 2026-10-01\nevaluation_time: ") and "실행 기준 시각" not in dynamic
    assert call.status == "OK" and call.body["model"] == "resolved-judge-v1" and "answers" not in call.body
    assert [it["id"] for it in call.body["items"]] == list(IDS) and "evidence_recovery" not in call.body["verification"]
    assert call.body["verification"]["output_transport"] == "responses_json_schema"
    assert next(it for it in call.body["items"] if it["id"] == "J5-3.a")["server"]["links"] == {"https://official.example/caffeine": "not_found"}
    disabled, requests = openai_call(openai_response(answered(("J1-1.a", "J3-3.b", "J5-3.a"))), qs, config=SemanticApiConfig(web_search="disabled"))
    body = json.loads(requests[0].content)
    assert "tools" not in body and body["instructions"] == ITEMS_SYSTEM_PROMPT and "### J5-1.a" not in body["input"][0]["content"][0]["text"]
    assert disabled.body["verification"]["items_not_sent"] == [{"id": "J5-1.a", "reason": common.SEARCH_NOT_AVAILABLE_REASON}]
    wrong, _ = openai_call(openai_response([{"id": "J1-1.a"}]), qs)
    assert wrong.status == "INVALID_RESPONSE"


# --- Claude CLI (검색 없음) ---------------------------------------------------------------------------------------

def test_claude_cli_items_never_sends_the_fact_item_and_passes_items_schema_and_prompt(tmp_path):
    calls = []

    def runner(cmd, **kw):
        calls.append((cmd, kw))
        schema = json.loads(cmd[cmd.index("--json-schema") + 1])
        sent = schema["properties"]["items"]["items"]["properties"]["id"]["description"].split("보낸 항목: ")[1].split(", ")
        out = {"is_error": False, "usage": {"input_tokens": 900, "output_tokens": 80},
               "modelUsage": {"claude-judge": {"outputTokens": 80}}, "structured_output": {"items": answered(sent)}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(out, ensure_ascii=False), stderr="")
    qs = questions(*IDS)
    client = ClaudeCliClient(ClaudeCliConfig(executable="claude"), runner=runner, workdir=tmp_path, source_fetcher=fetcher_not_found)
    call = client.system_one(STATE, qs)
    cmd, kw = calls[0]
    schema = json.loads(cmd[cmd.index("--json-schema") + 1])
    assert schema == items_output_schema({k: v for k, v in qs.items() if k != "J5-1.a"})
    assert cmd[cmd.index("--system-prompt") + 1] == ITEMS_SYSTEM_PROMPT and cmd[cmd.index("--tools") + 1] == ""
    assert kw["input"].startswith("## 점검 항목") and "### J5-1.a" not in kw["input"] and "### J5-3.a" in kw["input"]
    assert "이 요청에서는 웹검색 도구를 쓸 수 없습니다." in kw["input"]
    assert call.status == "OK" and call.body["model"] == "claude-judge" and "answers" not in call.body
    assert [it["id"] for it in call.body["items"]] == ["J1-1.a", "J3-3.b", "J5-3.a"]
    assert call.body["verification"]["items_not_sent"] == [{"id": "J5-1.a", "reason": common.SEARCH_NOT_AVAILABLE_REASON}]
    assert call.body["verification"]["search_performed"] is False and call.body["verification"]["fact_policy"]
    assert next(it for it in call.body["items"] if it["id"] == "J5-3.a")["server"]["links"] == {"https://official.example/caffeine": "not_found"}

    def legacy_shape(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"structured_output": {"J1-1.a": {"choice": "PASS"}}}), stderr="")
    bad = ClaudeCliClient(ClaudeCliConfig(executable="claude", max_attempts=1), runner=legacy_shape, workdir=tmp_path).system_one(STATE, questions("J1-1.a"))
    assert bad.status == "INVALID_RESPONSE" and "items 응답 형식" in bad.error


# --- Codex CLI ------------------------------------------------------------------------------------------------------

def codex_runner(calls):
    from pathlib import Path

    def runner(cmd, **kw):
        calls.append((cmd, kw))
        schema = json.loads(Path(cmd[cmd.index("--output-schema") + 1]).read_text(encoding="utf-8"))
        sent = schema["properties"]["items"]["items"]["properties"]["id"]["description"].split("보낸 항목: ")[1].split(", ")
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text(json.dumps({"items": answered(sent)}, ensure_ascii=False), encoding="utf-8")
        events = [{"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30}}]
        return SimpleNamespace(returncode=0, stdout="\n".join(json.dumps(e) for e in events), stderr="")
    return runner


def test_codex_items_enables_search_only_when_the_fact_item_is_sent_and_records_not_sent_otherwise(tmp_path):
    calls = []
    qs = questions(*IDS)
    client = CodexCliClient(CodexCliConfig(executable="native-codex"), codex_runner(calls), tmp_path, source_fetcher=fetcher_not_found)
    call = client.system_one(STATE, qs)
    cmd, kw = calls[0]
    assert 'web_search="live"' in cmd and "-c" in cmd
    assert f"developer_instructions={json.dumps(items_system_prompt(True), ensure_ascii=False)}" in cmd
    assert kw["input"].startswith("## 점검 항목") and "### J5-1.a " in kw["input"]
    assert call.status == "OK" and call.body["model"] == "gpt-6.1-sol" and [it["id"] for it in call.body["items"]] == list(IDS)
    assert "items_not_sent" not in call.body["verification"] and call.usage == {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30}
    assert next(it for it in call.body["items"] if it["id"] == "J5-3.a")["server"]["links"] == {"https://official.example/caffeine": "not_found"}
    disabled = CodexCliClient(CodexCliConfig(executable="native-codex", web_search="disabled"), codex_runner(calls), tmp_path,
                              source_fetcher=fetcher_not_found).system_one(STATE, qs)
    cmd, kw = calls[1]
    assert 'web_search="disabled"' in cmd and "### J5-1.a" not in kw["input"]
    assert f"developer_instructions={json.dumps(ITEMS_SYSTEM_PROMPT, ensure_ascii=False)}" in cmd
    assert disabled.body["verification"]["items_not_sent"] == [{"id": "J5-1.a", "reason": common.SEARCH_NOT_AVAILABLE_REASON}]
    assert [it["id"] for it in disabled.body["items"]] == ["J1-1.a", "J3-3.b", "J5-3.a"]
    # 검색이 필요 없는 항목만 보내면 live 설정이어도 검색을 켜지 않는다
    plain = CodexCliClient(CodexCliConfig(executable="native-codex"), codex_runner(calls), tmp_path).system_one(STATE, questions("J1-1.a", needs_search=()))
    assert 'web_search="disabled"' in calls[2][0] and plain.status == "OK" and "items_not_sent" not in plain.body["verification"]
