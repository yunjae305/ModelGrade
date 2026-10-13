"""Claude evaluator wiring/configuration only; all transport calls are forbidden or mocked."""

import copy
import json
import socket
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from code_eval import anthropic_semantic_client as anthropic
from code_eval import semantic_check
from code_eval.combined import combined_summary
from code_eval.engine import evaluate_batch
from code_eval.jev_client import JevCall, resolve_jev_client
from code_eval.jev_eval import evaluate_jev_batch
from code_eval.semantic_setup import semantic_setup
from webapp.version_registry import evaluation_profile


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
            config.max_continuations) == (180, 12000, 1, 8, 2)
    assert "secret" not in json.dumps(config.to_manifest())
    assert env["ANTHROPIC_MODEL"] == "collection-model"


def test_missing_registered_anthropic_key_never_creates_client_or_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(anthropic, "AnthropicSemanticClient", lambda *_: pytest.fail("Missing key"))
    result, config, error = resolve_jev_client(environment={"SEMANTIC_ENGINE": "anthropic_api",
        "SEMANTIC_API_KEY": "wrong-provider", "OPENAI_API_KEY": "wrong-provider"}, env_path=tmp_path / "missing")
    assert result is None and config.engine == "anthropic_api"
    assert "ANTHROPIC_API_KEY" in error and "wrong-provider" not in error


def test_setup_and_resolver_use_identical_overrides_without_connection(tmp_path, monkeypatch):
    env = environment(SEMANTIC_ANTHROPIC_MODEL="claude-sonnet-5-5", SEMANTIC_ANTHROPIC_EFFORT="high",
        SEMANTIC_ANTHROPIC_BASE="https://api.anthropic.com/v1", SEMANTIC_ANTHROPIC_TIMEOUT="91",
        SEMANTIC_ANTHROPIC_MAX_OUTPUT_TOKENS="4000", SEMANTIC_ANTHROPIC_MAX_SEARCHES="2",
        SEMANTIC_ANTHROPIC_MAX_FETCHES="3", SEMANTIC_ANTHROPIC_MAX_CONTINUATIONS="1",
        SEMANTIC_WEB_SEARCH="disabled")
    config_only = []
    original = anthropic.AnthropicSemanticConfig
    def config(**kwargs):
        config_only.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(anthropic, "AnthropicSemanticConfig", config)
    monkeypatch.setattr(anthropic, "AnthropicSemanticClient", lambda c, k: SimpleNamespace(config=c))
    _, resolved, _ = resolve_jev_client(environment=env, env_path=tmp_path / "missing")
    setup = semantic_setup(environment=env, env_path=tmp_path / "missing")
    assert config_only[0] == config_only[1]
    assert setup["engine_label"] == "Claude API" and setup["model"] == resolved.model
    assert setup["reasoning_effort"] == "high" and setup["can_evaluate"] is True
    assert setup["connection_status"] == "not_checked" and setup["search_status"] == "disabled"
    assert "fixture-anthropic-secret" not in json.dumps(setup)


@pytest.mark.parametrize("overrides", [
    {"ANTHROPIC_API_KEY": ""}, {"SEMANTIC_ANTHROPIC_EFFORT": "private-invalid"},
    {"SEMANTIC_ANTHROPIC_TIMEOUT": "private-invalid"}, {"SEMANTIC_ANTHROPIC_MAX_SEARCHES": "0"},
    {"SEMANTIC_ANTHROPIC_BASE": "https://user:private-invalid@example.test/v1"},
])
def test_anthropic_setup_contains_configuration_errors_without_echoing_secrets(tmp_path, overrides):
    setup = semantic_setup(environment=environment(**overrides), env_path=tmp_path / "missing")
    assert setup["can_evaluate"] is False and setup["configuration_status"] in ("missing", "invalid")
    assert setup["connection_status"] == "not_checked"
    assert "private-invalid" not in json.dumps(setup) and "fixture-anthropic-secret" not in json.dumps(setup)


def test_existing_auto_does_not_silently_switch_to_anthropic(tmp_path, monkeypatch):
    from code_eval import codex_cli_client
    monkeypatch.setattr(codex_cli_client, "find_codex_executable", lambda *_: "fixture-codex")
    monkeypatch.setattr(codex_cli_client, "check_codex_login", lambda *_: None)
    result, config, error = resolve_jev_client(environment=environment(SEMANTIC_ENGINE="auto"),
                                               env_path=tmp_path / "missing")
    assert error is None and config.engine == "codex_cli"
    assert result is not None


def test_anthropic_batch_screens_fact_claims_without_numbers_and_profiles_actual_settings():
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
    profile = evaluation_profile({"evaluations": {"aiona": batch}})
    assert profile["complete"] is True
    expected = {"max_fetches", "max_continuations", "thinking", "output_mode", "search_tool", "fetch_tool"}
    assert expected <= profile["fields"]["judge"].keys()
    for field in expected:
        changed = copy.deepcopy(batch)
        changed["jev_eval"]["client"][field] = "changed-condition"
        assert evaluation_profile({"evaluations": {"aiona": changed}}) != profile


@pytest.mark.parametrize("configured", [True, False])
def test_semantic_check_default_is_configuration_only(tmp_path, monkeypatch, capsys, configured):
    env = environment(ANTHROPIC_API_KEY="fixture-anthropic-secret" if configured else "")
    monkeypatch.setattr(semantic_check, "semantic_setup", lambda **_: semantic_setup(
        environment=env, env_path=tmp_path / "missing"))
    monkeypatch.setattr(semantic_check, "resolve_jev_client", lambda **_: pytest.fail("No default resolution/login"))
    assert semantic_check.main([]) == (0 if configured else 2)
    output = json.loads(capsys.readouterr().out)
    assert output["ready"] is configured
    assert output["configuration"]["connection_status"] == "not_checked"
    assert "fixture-anthropic-secret" not in json.dumps(output)


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
