"""Money stays runtime-only; API request usage can be saved. No live requests."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from code_eval.claude_cli_client import ClaudeCliClient, ClaudeCliConfig
from code_eval.codex_cli_client import CodexCliClient, CodexCliConfig
from code_eval.jev_client import HttpJevClient, JevCall, JevClientConfig
from code_eval.jev_eval import evaluate_jev_case, jev_request
from code_eval.semantic_api_client import SemanticApiClient, SemanticApiConfig
from code_eval.cost_reporting import CostLedger


STATE = {"question": "질문", "answer": "답변"}
QUESTIONS = jev_request("질문", "답변", ("J1",))["questions"]
ENTRIES = {"J1": {"choice": "PASS", "confidence": 0.8, "reason": "검증용 판정"}}
TOKENS = {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30}


def assert_runtime_only(call):
    client = SimpleNamespace(system_one=lambda **kwargs: call)
    record = evaluate_jev_case("Q1", **STATE, client=client, criteria=("J1",))
    assert record["usage"] == call.usage
    serialized = json.dumps(record)
    assert not any(key in serialized for key in ("accounting", "cost_usd", "costUSD", "reported_cost_usd"))
    assert "accounting=" not in repr(call)


@pytest.mark.parametrize("mode,status", [
    ("success", "OK"), ("bad-json", "INVALID_RESPONSE"), ("compacted", "INVALID_RESPONSE"),
    ("turn-failed", "API_ERROR"), ("timeout", "TIMEOUT")])
def test_codex_observed_usage_survives_invalid_result_and_partial_timeout(tmp_path, mode, status):
    events = [{"type": "item.completed", "item": {"type": "web_search", "action": {"type": kind}}}
              for kind in ("search", "open_page", "find_in_page")]
    events.append({"type": "turn.completed", "usage": TOKENS})
    if mode == "compacted":
        events.append({"type": "context.compacted"})
    elif mode == "turn-failed":
        events.append({"type": "turn.failed", "error": {"message": "fixture error"}})

    def runner(cmd, **kwargs):
        stdout = "\n".join(json.dumps(event) for event in events)
        if mode == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output=stdout.encode())
        path = Path(cmd[cmd.index("--output-last-message") + 1])
        path.write_text("{" if mode == "bad-json" else json.dumps(ENTRIES), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    call = CodexCliClient(CodexCliConfig(), runner=runner, workdir=tmp_path).system_one(STATE, QUESTIONS)
    assert call.status == status and call.usage == TOKENS
    assert call.accounting["usage"] == TOKENS and call.accounting["unreported_attempts"] == 0
    assert call.accounting["model"] == "gpt-6.1-sol"
    assert call.accounting["web_search_calls"] == (None if mode == "timeout" else 1)
    assert call.body["verification"]["search_calls"] == 3
    assert "reported_cost_usd" not in call.accounting
    assert_runtime_only(call)


@pytest.mark.parametrize("mode", ["timeout", "missing-executable"])
def test_codex_missing_usage_stays_unknown(tmp_path, mode):
    def runner(cmd, **kwargs):
        if mode == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        raise FileNotFoundError("fixture missing")
    call = CodexCliClient(CodexCliConfig(), runner=runner, workdir=tmp_path).system_one(STATE, QUESTIONS)
    assert call.accounting["usage"] == {} and call.accounting["unreported_attempts"] == 1
    assert call.accounting["web_search_calls"] is None


@pytest.mark.parametrize("mode", ["success", "incomplete", "refusal", "bad-json", "http-error"])
def test_responses_accounting_retains_actual_model_tier_usage_and_only_search_actions(mode):
    usage = {"input_tokens": 150, "output_tokens": 60,
             "input_tokens_details": {"cached_tokens": 40}, "output_tokens_details": {"reasoning_tokens": 20}}
    output = [{"type": "web_search_call", "status": "completed", "action": {"type": kind}}
              for kind in ("search", "open_page", "find_in_page")]
    content = [{"type": "refusal", "refusal": "fixture"}] if mode == "refusal" else [
        {"type": "output_text", "text": "{" if mode == "bad-json" else json.dumps(ENTRIES)}]
    output.append({"type": "message", "status": "completed", "content": content})
    raw = {"status": "incomplete" if mode == "incomplete" else "completed", "model": "resolved-model",
           "service_tier": "flex", "usage": usage, "output": output}
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(429 if mode == "http-error" else 200, json=raw)
    client = SemanticApiClient(SemanticApiConfig(model="requested-model"), "fixture-key",
                               transport=httpx.MockTransport(handler))
    call = client.system_one(STATE, QUESTIONS)
    assert len(requests) == 1 and call.attempts == 1
    assert call.status == "OK" if mode == "success" else call.status != "OK"
    assert call.usage == usage
    part = {"model": "resolved-model", "usage": usage, "web_search_calls": 1,
            "observed_web_search_calls": 1, "service_tier": "flex", "inference_geo": None}
    assert call.accounting == {"model": "resolved-model", "usage": usage, "cost_parts": [part],
                               "unreported_attempts": 0}
    assert call.body["verification"]["usage_requests"] == [part]
    assert_runtime_only(call)


@pytest.mark.parametrize("event", [
    {"type": "web_search_call", "status": "completed"},
    {"type": "web_search_call", "status": "in_progress", "action": {"type": "search"}},
])
def test_responses_unknown_search_action_does_not_become_zero_cost(event):
    raw = {"status": "incomplete", "model": "actual", "usage": {"input_tokens": 10}, "output": [event]}
    client = SemanticApiClient(SemanticApiConfig(), "fixture-key",
                               transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw)))
    call = client.system_one(STATE, QUESTIONS)
    assert call.accounting["cost_parts"][0]["web_search_calls"] is None
    assert call.body["verification"]["usage_requests"][0]["web_search_calls"] is None


def test_responses_transport_timeout_has_no_invented_usage_or_duplicate_attempt(capsys):
    def handler(request):
        raise httpx.ReadTimeout("fixture", request=request)
    client = SemanticApiClient(SemanticApiConfig(), "fixture-key", transport=httpx.MockTransport(handler))
    call = client.system_one(STATE, QUESTIONS)
    assert call.status == "TIMEOUT" and call.attempts == 1 and call.accounting["unreported_attempts"] == 0
    assert call.accounting["usage"] == {}
    assert len(call.accounting["cost_parts"]) == 1
    assert call.accounting["cost_parts"][0]["usage"] == {}
    assert call.accounting["cost_parts"][0]["web_search_calls"] is None
    ledger = CostLedger(); ledger._semantic(client.config, call); ledger.finish('timeout', 'ERROR')
    output = capsys.readouterr().out
    assert '호출 1건 | 미확인 1건' in output and '호출 2건' not in output


def test_responses_partial_search_events_preserve_lower_bound_without_claiming_known_total():
    raw = {"status": "incomplete", "model": "actual", "usage": {"input_tokens": 10}, "output": [
        {"type": "web_search_call", "status": "completed", "action": {"type": "search"}},
        {"type": "web_search_call", "status": "in_progress", "action": {"type": "search"}}]}
    client = SemanticApiClient(SemanticApiConfig(), "fixture-key",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw)))
    call = client.system_one(STATE, QUESTIONS)
    part = call.body["verification"]["usage_requests"][0]
    assert part["observed_web_search_calls"] == 1 and part["web_search_calls"] is None


def claude_result(output):
    return SimpleNamespace(returncode=0, stdout=json.dumps(output), stderr="")


def test_claude_reported_cost_and_multiple_models_never_enter_saved_record(tmp_path):
    output = {"structured_output": ENTRIES, "total_cost_usd": .03,
        "usage": {"input_tokens": 900, "output_tokens": 83, "cache_read_input_tokens": 20},
        "modelUsage": {
            "helper": {"inputTokens": 100, "outputTokens": 3, "costUSD": .01},
            "judge": {"inputTokens": 800, "outputTokens": 80, "cacheReadInputTokens": 20,
                      "cacheCreationInputTokens": 10, "costUSD": .02}}}
    call = ClaudeCliClient(ClaudeCliConfig(), runner=lambda *args, **kwargs: claude_result(output),
                           workdir=tmp_path).system_one(STATE, QUESTIONS)
    assert call.status == "OK" and call.body["model"] == "judge"
    assert call.usage == output["usage"] and call.accounting["reported_cost_usd"] == .03
    assert call.accounting["model"] is None and call.accounting["web_search_calls"] == 0
    assert call.accounting["model_usage"]["helper"] == {
        "usage": {"input_tokens": 100, "output_tokens": 3}, "reported_cost_usd": .01}
    assert call.accounting["model_usage"]["judge"] == {
        "usage": {"input_tokens": 800, "output_tokens": 80, "cached_input_tokens": 20,
                  "cache_creation_input_tokens": 10}, "reported_cost_usd": .02}
    assert_runtime_only(call)


@pytest.mark.parametrize("first", ["billed-error", "unknown-timeout"])
def test_claude_retries_sum_only_observed_attempts(tmp_path, first):
    attempts = []
    def runner(cmd, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            if first == "unknown-timeout":
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return claude_result({"is_error": True, "usage": {"input_tokens": 40, "output_tokens": 2},
                                  "total_cost_usd": .01})
        return claude_result({"structured_output": ENTRIES, "usage": {"input_tokens": 20, "output_tokens": 3},
                              "total_cost_usd": .02})
    call = ClaudeCliClient(ClaudeCliConfig(model="known-model"), runner=runner, workdir=tmp_path).system_one(STATE, QUESTIONS)
    assert call.attempts == 2 and call.usage == {"input_tokens": 20, "output_tokens": 3}
    assert call.accounting["usage"]["input_tokens"] == (60 if first == "billed-error" else 20)
    if first == "billed-error":
        assert call.accounting["reported_cost_usd"] == pytest.approx(.03)
    else:
        assert "reported_cost_usd" not in call.accounting
    assert call.accounting["unreported_attempts"] == (0 if first == "billed-error" else 1)
    assert_runtime_only(call)


def test_claude_partial_reported_price_is_not_misrepresented_as_total(tmp_path):
    attempts = []
    def runner(cmd, **kwargs):
        attempts.append(True)
        first = len(attempts) == 1
        tokens = 40 if first else 20
        output = {"is_error": first, "structured_output": ENTRIES, "usage": {"input_tokens": tokens},
                  "modelUsage": {"judge": {"inputTokens": tokens}}}
        if not first:
            output["total_cost_usd"] = .02
            output["modelUsage"]["judge"]["costUSD"] = .02
        return claude_result(output)
    call = ClaudeCliClient(ClaudeCliConfig(model="judge"), runner=runner, workdir=tmp_path).system_one(STATE, QUESTIONS)
    assert call.status == "OK" and call.accounting["unreported_attempts"] == 0
    assert call.accounting["usage"]["input_tokens"] == 60
    assert "reported_cost_usd" not in call.accounting
    assert call.accounting["model_usage"]["judge"] == {"usage": {"input_tokens": 60}}


def test_jev_retry_accounting_does_not_multiply_final_usage_or_persist_cost():
    attempts = []
    def handler(request):
        attempts.append(True)
        first = len(attempts) == 1
        body = {"model": "jev-resolved", "answers": {},
                "usage": {"input_tokens": 200 if first else 300, "output_tokens": 0, "cost_usd": .01 if first else .02}}
        return httpx.Response(503 if first else 200, json=body)
    client = HttpJevClient(JevClientConfig(), "fixture-key", transport=httpx.MockTransport(handler), sleep=lambda _: None)
    call = client.system_one(STATE, QUESTIONS)
    assert call.attempts == 2 and call.usage == {"input_tokens": 300, "output_tokens": 0}
    assert call.accounting["usage"] == {"input_tokens": 500, "output_tokens": 0}
    assert call.accounting["reported_cost_usd"] == pytest.approx(.03)
    assert call.accounting["model"] == "jev-resolved" and call.accounting["unreported_attempts"] == 0
    assert_runtime_only(call)


def test_runtime_accounting_default_is_not_shared_between_calls():
    one, two = JevCall("OK"), JevCall("OK")
    one.accounting["reported_cost_usd"] = 1.0
    assert two.accounting == {} and "reported_cost_usd" not in repr(one)
