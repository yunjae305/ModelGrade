"""Migration configuration and CLI evidence provenance, without live services."""
import copy
import json
from pathlib import Path


from code_eval.codex_cli_client import CodexCliClient, CodexCliConfig, _verification
from code_eval.jev_client import resolve_jev_client
from code_eval.semantic_api_client import SemanticApiClient
from code_eval.semantic_cli_common import output_schema
from code_eval.semantic_evidence import finalize_answers


def test_auto_only_switches_with_dedicated_semantic_key(tmp_path, monkeypatch):
    monkeypatch.setattr("code_eval.codex_cli_client.find_codex_executable", lambda _: "codex")
    monkeypatch.setattr("code_eval.codex_cli_client.check_codex_login", lambda _: None)
    env = {"SEMANTIC_ENGINE": "auto", "OPENAI_API_KEY": "collection-only-key"}
    client, cfg, error = resolve_jev_client(environment=env, env_path=tmp_path / ".env")
    assert isinstance(client, CodexCliClient) and error is None
    env["SEMANTIC_API_KEY"] = "evaluation-only-key"
    client, cfg, error = resolve_jev_client(environment=env, env_path=tmp_path / ".env")
    assert isinstance(client, SemanticApiClient) and error is None
    assert "evaluation-only-key" not in json.dumps(cfg.to_manifest())
    assert "collection-only-key" not in json.dumps(cfg.to_manifest())


def test_explicit_api_never_falls_back_to_collection_or_cli(tmp_path):
    client, cfg, error = resolve_jev_client(environment={"SEMANTIC_ENGINE": "openai_api", "OPENAI_API_KEY": "collection"},
                                           env_path=tmp_path / ".env")
    assert client is None and cfg.engine == "openai_api" and "SEMANTIC_API_KEY" in error


def test_only_completed_search_tool_urls_establish_provenance():
    events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "I searched https://invented.example"}},
              {"type": "item.started", "item": {"type": "web_search", "results": [{"url": "https://started.example"}]}},
              {"type": "item.completed", "item": {"type": "web_search", "results": [{"url": "https://actual.example/a"}]}}]
    assert _verification(events)["source_urls"] == ["https://actual.example/a"]
    assert _verification(events[:2])["search_performed"] is False


def test_shared_schema_keeps_supplemental_code_binary_but_supports_pending_semantics():
    schema = output_schema(["J1", "J5", "J6", "C1"])
    assert "UNVERIFIED" in schema["properties"]["J5"]["properties"]["choice"]["enum"]
    assert schema["properties"]["C1"]["properties"]["choice"]["enum"] == ["PASS", "FAIL"]
    assert "fact_check" in schema["properties"]["J5"]["required"]
    assert "context_checks" in schema["properties"]["J6"]["required"]


def test_inconsistent_whole_answer_failure_is_not_promoted_to_pass():
    body = {"answers": {"J5": {"choice": "FAIL", "confidence": .8, "reason": "uncovered error",
        "fact_check": {"basis": "reference", "claims": [{"claim": "known", "verdict": "PASS", "reason": "matches",
            "sources": [{"url": "", "title": "reference", "quote": "known"}]}]}}}}
    original = copy.deepcopy(body)
    result = finalize_answers(body, {"answer": "known", "reference": "known"}, {"J5": {}})
    assert result["answers"]["J5"]["choice"] == "UNVERIFIED"
    assert body == original


def test_cli_cannot_claim_search_success_without_tools(tmp_path):
    from types import SimpleNamespace
    def run(cmd, **kwargs):
        assert 'web_search="live"' in cmd
        assert [cmd[i + 1] for i, value in enumerate(cmd) if value == "--enable"] == ["code_mode", "code_mode_host"]
        schema = json.loads(Path(cmd[cmd.index("--output-schema") + 1]).read_text(encoding="utf-8"))
        result = {"J5": {"choice": "PASS", "confidence": .99, "reason": "trust me", "fact_check": {
            "basis": "web", "claims": [{"claim": "test claim", "verdict": "PASS", "reason": "trust me",
                "sources": [{"url": "https://invented.example", "title": "claimed source", "quote": "claim"}]}]}}}
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text(json.dumps(result), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"type": "turn.completed"}), stderr="")
    call = CodexCliClient(CodexCliConfig(), run, tmp_path).system_one({"answer": "test claim"}, {"J5": {"instructions": "check", "criteria": {"PASS": "true", "FAIL": "false"}}})
    assert call.body["answers"]["J5"]["choice"] == "UNVERIFIED"
