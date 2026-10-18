"""Configuration/GET tests: no evaluator, CLI process, network, or saved run writes."""
import json
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from code_eval import claude_cli_client, codex_cli_client, jev_client, semantic_api_client
from code_eval.semantic_setup import semantic_setup
from webapp.code_eval_service import CodeEvalService
from webapp.server import Handler


@pytest.fixture(autouse=True)
def prohibit_model_process_and_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A configuration query must not resolve/run a judge, process, or network request")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    monkeypatch.setattr(jev_client, "resolve_jev_client", forbidden)
    monkeypatch.setattr(codex_cli_client, "check_codex_login", forbidden)
    for client in (semantic_api_client.SemanticApiClient, codex_cli_client.CodexCliClient,
                   claude_cli_client.ClaudeCliClient, jev_client.HttpJevClient):
        monkeypatch.setattr(client, "system_one", forbidden)


def status(tmp_path, **environment):
    return semantic_setup(environment=environment, env_path=tmp_path / "not-present.env")


def assert_not_connected(value):
    assert value["connection_status"] == "not_checked"
    assert "설정만 확인" in value["connection_message"]
    assert "실제 연결" in value["connection_message"]


def test_auto_dedicated_key_selects_api_without_accessing_cli_or_collection_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_cli_client, "find_codex_executable", lambda *_: pytest.fail("API does not inspect CLI"))
    value = status(tmp_path, SEMANTIC_ENGINE="auto", SEMANTIC_API_KEY="sk-dedicated-secret",
                   OPENAI_API_KEY="sk-collection-secret", TYPESAFE_API_KEY="typesafe-secret")
    assert value["engine"] == "openai_api" and value["model"] == "gpt-6.1-sol"
    assert value["configuration_status"] == "configured" and value["can_evaluate"] is True
    assert value["search_status"] == "configured" and "아직 확인하지" in value["search_message"]
    assert_not_connected(value)
    assert "secret" not in json.dumps(value)


def test_auto_without_dedicated_key_uses_only_cli_file_existence(tmp_path):
    executable = tmp_path / "configured-codex.exe"
    executable.write_bytes(b"not an executable; never run this fixture")
    value = status(tmp_path, SEMANTIC_ENGINE="auto", CODEX_CLI_PATH=str(executable),
                   OPENAI_API_KEY="collection-secret", TYPESAFE_API_KEY="not-an-auto-judge")
    assert value["engine"] == "codex_cli" and value["can_evaluate"] is True
    assert value["search_status"] == "configured" and "CLI 웹 검색을 허용" in value["search_message"]
    assert "실제 검색 연결과 출처 원문 접근은 아직 확인하지" in value["search_message"]
    assert_not_connected(value)
    assert str(executable) not in json.dumps(value)


@pytest.mark.parametrize("engine,path_key", [("codex_cli", "CODEX_CLI_PATH"), ("claude_cli", "CLAUDE_CLI_PATH")])
def test_missing_cli_is_a_known_configuration_block_not_a_guessed_login_failure(tmp_path, engine, path_key):
    value = status(tmp_path, SEMANTIC_ENGINE=engine, **{path_key: str(tmp_path / "missing-private-path.exe")})
    assert value["configuration_status"] == "missing" and value["can_evaluate"] is False
    assert "실행 파일" in value["reason"] and "로그인" not in value["reason"]
    assert "missing-private-path" not in json.dumps(value)
    assert_not_connected(value)


@pytest.mark.parametrize("engine,key", [("openai_api", "SEMANTIC_API_KEY"), ("jev", "TYPESAFE_API_KEY")])
def test_api_requires_its_own_key_even_if_collection_credentials_exist(tmp_path, engine, key):
    value = status(tmp_path, SEMANTIC_ENGINE=engine, OPENAI_API_KEY="collection-only")
    assert value["configuration_status"] == "missing" and value["can_evaluate"] is False
    assert key in value["reason"] and "collection-only" not in json.dumps(value)
    assert_not_connected(value)


@pytest.mark.parametrize("engine", ["openai_api", "codex_cli"])
def test_disabled_search_is_a_limitation_not_a_configuration_failure(tmp_path, engine):
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    value = status(tmp_path, SEMANTIC_ENGINE=engine, SEMANTIC_API_KEY="fixture-secret",
                   CODEX_CLI_PATH=str(executable), SEMANTIC_WEB_SEARCH="disabled")
    assert value["can_evaluate"] is True and value["search_status"] == "disabled"
    assert "참조 자료" in value["search_message"]


@pytest.mark.parametrize("overrides", [
    {"SEMANTIC_ENGINE": "private-invalid-engine"},
    {"SEMANTIC_API_TIMEOUT": "secret-invalid-number"},
    {"SEMANTIC_API_MAX_TOOL_CALLS": "-1"},
    {"SEMANTIC_API_MAX_OUTPUT_TOKENS": "-1"},   # 0 은 한도 없음 (사용자 결정 2026-10-19)
    {"SEMANTIC_WEB_SEARCH": "private-invalid-policy"},
    {"SEMANTIC_API_BASE": "https://name:private-credential@example.test/v1"},
    {"SEMANTIC_ENGINE": "codex_cli", "CODEX_CLI_REASONING": "private-invalid-reasoning"},
])
def test_invalid_settings_are_contained_without_echoing_values(tmp_path, overrides):
    value = status(tmp_path, **({"SEMANTIC_ENGINE": "openai_api", "SEMANTIC_API_KEY": "private-key"} | overrides))
    assert value["configuration_status"] == "invalid" and value["can_evaluate"] is False
    assert value["reason"] and "private-" not in json.dumps(value) and "secret-" not in json.dumps(value)
    assert_not_connected(value)


def test_sensitive_or_path_shaped_model_labels_are_not_exposed(tmp_path):
    for model in (str(tmp_path / "private-model"), "sk-private-credential"):
        value = status(tmp_path, SEMANTIC_ENGINE="openai_api", SEMANTIC_API_KEY="key", SEMANTIC_API_MODEL=model)
        assert value["model"] is None and model not in json.dumps(value)


def test_bad_env_encoding_becomes_its_own_status(tmp_path):
    source = tmp_path / ".env"
    source.write_bytes(b"\xffinvalid-file")
    value = semantic_setup(environment={}, env_path=source)
    assert value["configuration_status"] == "invalid" and value["can_evaluate"] is False


def call_info_route(service):
    handler = object.__new__(Handler)
    handler.server = SimpleNamespace(code_eval_service=service)
    captured = []
    handler.respond = lambda code, body: captured.append((code, body))
    handler.code_eval_get("/api/code-eval/info")
    assert len(captured) == 1 and captured[0][0] == 200
    return captured[0][1]


@pytest.mark.parametrize("settings", [
    "SEMANTIC_ENGINE=openai_api\nSEMANTIC_API_KEY=private-fixture-key\n",
    "SEMANTIC_ENGINE=openai_api\nSEMANTIC_API_TIMEOUT=private-invalid\n",
])
def test_actual_info_route_never_runs_a_model_and_preserves_files(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(os, "environ", {})
    (tmp_path / ".env").write_text(settings, encoding="utf-8")
    saved = tmp_path / "runs" / "existing.json"
    saved.parent.mkdir()
    saved.write_bytes(b'{"saved":"unchanged"}')
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    service = CodeEvalService(root=tmp_path, save_dir=saved.parent)
    for _ in range(2):
        info = call_info_route(service)
        assert {"versions", "aiona_config", "taxonomy", "semantic_setup"} <= info.keys()
        assert info["semantic_setup"]["connection_status"] == "not_checked"
        assert "private-" not in json.dumps(info)
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after


def test_injected_evaluator_is_explicit_and_does_not_read_host_or_resolve(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_ENGINE", "broken-host-setting")
    service = CodeEvalService(root=tmp_path, jev_resolver=lambda: pytest.fail("Do not run injected resolver"))
    value = call_info_route(service)["semantic_setup"]
    assert value["engine"] == "injected" and value["configuration_status"] == "configured"
    assert value["can_evaluate"] is True and value["connection_status"] == "not_checked"
    assert value["search_status"] == "not_checked"
    assert not list(tmp_path.iterdir())
