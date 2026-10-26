"""Claude evaluator wiring/configuration only; all transport calls are forbidden or mocked."""

import copy
import json
import socket
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from code_eval import anthropic_semantic_client as anthropic
from code_eval.combined import combined_summary
from code_eval.engine import evaluate_batch
from code_eval.jev_client import JevCall, resolve_jev_client
from code_eval.jev_eval import evaluate_jev_batch


@pytest.fixture(autouse=True)
def forbid_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Wiring checks must not create a process or HTTP connection")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.Client, "__init__", forbidden)


def environment(**extra):
    return {"SEMANTIC_ENGINE": "anthropic_api", "ANTHROPIC_API_KEY": "fixture-anthropic-secret", **extra}


def test_explicit_anthropic_reuses_registered_key_and_keeps_collection_model_separate(tmp_path, monkeypatch):
    constructions = []
    def client(config, key):
        constructions.append((config, key))
        return SimpleNamespace(config=config)
    monkeypatch.setattr(anthropic, "AnthropicSemanticClient", client)
    env = environment(ANTHROPIC_MODEL="collection-model", SEMANTIC_API_KEY="openai-evaluation-key")
    result, config, error = resolve_jev_client(environment=env, env_path=tmp_path / "missing.env")
    assert error is None and result.config is config
    assert constructions == [(config, "fixture-anthropic-secret")]
    assert config.engine == "anthropic_api" and config.model == "claude-sonnet-5-5"
    assert config.reasoning_effort == "medium" and config.web_search == "live"
    assert (config.timeout, config.max_output_tokens, config.max_tool_calls, config.max_fetches,
            config.max_continuations) == (180, 20000, 1, 8, 2)
    assert "secret" not in json.dumps(config.to_manifest())
    assert env["ANTHROPIC_MODEL"] == "collection-model"


def test_missing_registered_anthropic_key_never_creates_client_or_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(anthropic, "AnthropicSemanticClient", lambda *_: pytest.fail("Missing key"))
    result, config, error = resolve_jev_client(environment={"SEMANTIC_ENGINE": "anthropic_api",
        "SEMANTIC_API_KEY": "wrong-provider", "OPENAI_API_KEY": "wrong-provider"}, env_path=tmp_path / "missing")
    assert result is None and config.engine == "anthropic_api"
    assert "ANTHROPIC_API_KEY" in error and "wrong-provider" not in error


def test_existing_auto_does_not_silently_switch_to_anthropic(tmp_path, monkeypatch):
    from code_eval import codex_cli_client
    monkeypatch.setattr(codex_cli_client, "find_codex_executable", lambda *_: "fixture-codex")
    monkeypatch.setattr(codex_cli_client, "check_codex_login", lambda *_: None)
    result, config, error = resolve_jev_client(environment=environment(SEMANTIC_ENGINE="auto"),
                                               env_path=tmp_path / "missing")
    assert error is None and config.engine == "codex_cli"
    assert result is not None


def test_anthropic_batch_screens_fact_claims_without_numbers():
    class Judge:
        config = anthropic.AnthropicSemanticConfig()
        requests = []
        def system_one(self, state, questions):
            self.requests.append(copy.deepcopy({"state": state, "questions": questions}))
            answers = {cid: {"choice": "PASS", "confidence": 0.8,
                            "probabilities": {"PASS": 0.9, "FAIL": 0.1}} for cid in questions}
            answers["J5"].update(choice="NOT_APPLICABLE", reason="검증용 창작 답변", probabilities={},
                                  fact_check={"basis": "none", "claims": []})
            return JevCall("OK", body={"model": self.config.model, "answers": answers})
    judge = Judge()
    batch = evaluate_batch([{"question": "짧은 시를 써줘.", "answer": "  바람이 노래하네.\r\n"}])
    batch["jev_eval"] = evaluate_jev_batch(batch, judge)
    assert "J5" in judge.requests[0]["questions"]
    assert judge.requests[0]["state"]["answer"] == "  바람이 노래하네.\r\n"
    assert set(judge.requests[0]["state"]) == {"question", "answer"}
    assert batch["jev_eval"]["engine_label"] == "Claude API"
    assert batch["jev_eval"]["applicability_version"].endswith("llm-screening")
    assert combined_summary(batch)["semantic_engine"] == "Claude API"
    del batch["jev_eval"]["engine_label"]
    assert combined_summary(batch)["semantic_engine"] == "Claude API"


@pytest.mark.parametrize("command", ["single", "run"])
def test_cli_anthropic_option_reaches_resolver_without_fallback(command, tmp_path, monkeypatch):
    from code_eval import __main__ as cli, jev_eval

    selected = []
    output = []

    def resolve(**kwargs):
        selected.append(kwargs["engine"])
        return resolve_jev_client(environment={}, env_path=tmp_path / "missing.env", **kwargs)

    monkeypatch.setattr(jev_eval, "resolve_jev_client", resolve)
    monkeypatch.setattr(cli, "_print", output.append)
    if command == "single":
        args = ["single", "--question", "짧은 시를 써줘.", "--answer", "바람이 노래하네.", "--json"]
    else:
        source = tmp_path / "input.json"
        source.write_text(json.dumps({"question": "짧은 시를 써줘.", "answer": "바람이 노래하네."}), encoding="utf-8")
        args = ["run", "--input", str(source), "--out", str(tmp_path / "output")]

    assert cli.main([*args, "--semantic-engine", "anthropic-api"]) == 0
    result = json.loads(output[0] if command == "single" else
                        (tmp_path / "output" / "results.json").read_text(encoding="utf-8"))
    assert selected == ["anthropic-api"]
    assert result["jev_eval"]["client"]["engine"] == "anthropic_api"
    assert result["jev_eval"]["client"]["requested_model"] == "claude-sonnet-5-5"
    assert result["jev_eval"]["client"]["reasoning_effort"] == "medium"
    assert result["jev_eval"]["status"] == "NOT_CONFIGURED"
    assert "ANTHROPIC_API_KEY" in result["jev_eval"]["access_reason"]
