"""Codex CLI contract tests; every subprocess is a test double."""

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_eval.codex_cli_client import (CodexCliClient, CodexCliConfig, find_codex_executable,
                                       check_codex_login, _verification, DISABLED_FEATURES, SEARCH_HOST_FEATURES)
from code_eval.engine import evaluate_batch
from code_eval.jev_client import resolve_jev_client
from code_eval.jev_eval import evaluate_jev_batch, evaluate_jev_case, jev_request
from code_eval.report_html import render_report


def cli_output(cmd, entries=None, events=None, returncode=0, stderr=""):
    schema = json.loads(Path(cmd[cmd.index("--output-schema") + 1]).read_text(encoding="utf-8"))
    entries = entries if entries is not None else {
        cid: {"choice": "PASS", "confidence": 0.8, "reason": "기준 충족"} for cid in schema["required"]}
    Path(cmd[cmd.index("--output-last-message") + 1]).write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    events = events if events is not None else [{"type": "turn.completed", "usage": {
        "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30}}]
    return SimpleNamespace(returncode=returncode, stdout="\n".join(json.dumps(e) for e in events), stderr=stderr)


def test_codex_cli_uses_requested_model_high_and_preserves_complete_qa_history_reference(tmp_path):
    state = {"question": "  질문\r\n😀  ", "answer": "  답변\r\n" + "원문😀" * 25_000 + " 끝  ",
             "history": [{"turn": 1, "question": "  이전 질문\r\n", "answer": "```json\r\n{\"v\":\"\\n\"}\r\n```  "}],
             "reference": {"원본": "  참조\r\n"}}
    questions = jev_request("질문", "답", ("J1", "J5", "J6"))["questions"]
    captured = []

    def runner(cmd, **kw):
        captured.append((cmd, kw))
        return cli_output(cmd)

    call = CodexCliClient(CodexCliConfig(executable="native-codex"), runner, tmp_path).system_one(state, questions)
    cmd, kw = captured[0]
    assert json.loads(kw["input"].split("## 평가 대상 (데이터)\n", 1)[1]) == state
    assert len(kw["input"]) > 60_000 and state["answer"] not in " ".join(cmd)
    assert cmd[cmd.index("--model") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="high"' in cmd
    assert {"--ignore-user-config", "--ephemeral", "--skip-git-repo-check"} <= set(cmd)
    assert 'project_doc_max_bytes=0' in cmd and 'web_search="disabled"' in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only" and kw["shell"] is False
    disabled = {cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--disable"}
    assert {"shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent"} <= disabled
    assert kw["encoding"] == "utf-8" and kw["timeout"] == 600
    assert not Path(kw["cwd"]).exists()  # request files are removed after processing
    assert call.status == "OK" and call.body["model"] == "gpt-6.1-sol"
    assert set(call.body["answers"]) == {"J1", "J5", "J6"}
    assert call.usage == {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30}
    assert call.body["answers"]["J1"]["probabilities"] == {"PASS": 0.9, "FAIL": 0.1}


def test_codex_batch_preserves_history_parallel_outputs_progress_and_report(tmp_path):
    rows = [{"id": "T1", "conversation_id": "C", "turn": 1, "question": "  첫 질문\r\n",
             "answer": "  원문\r\n" + "가😀" * 35_000 + " 끝  "},
            {"id": "T2", "conversation_id": "C", "turn": 2, "question": " 이어서 설명해줘.\r\n", "answer": " 답  "}]
    inputs, output_paths, progress = [], [], []
    gate = threading.Barrier(2)

    def runner(cmd, **kw):
        inputs.append(json.loads(kw["input"].split("## 평가 대상 (데이터)\n", 1)[1]))
        output_paths.append(cmd[cmd.index("--output-last-message") + 1])
        gate.wait(timeout=5)
        schema = json.loads(Path(cmd[cmd.index("--output-schema") + 1]).read_text(encoding="utf-8"))
        entries = {cid: {"choice": "PASS", "confidence": 0.8, "reason": "기준 충족"} for cid in schema["required"]}
        if "J5" in entries:
            entries["J5"].update(choice="NOT_APPLICABLE", reason="이 원문 보존 시험에는 검증할 사실 주장이 없습니다.",
                                 fact_check={"basis": "none", "claims": []})
        if "J6" in entries:
            entries["J6"]["context_checks"] = [{"turn": 1, "instruction_quote": rows[0]["question"],
                "answer_quote": rows[1]["answer"], "decision": "PASS", "reason": "대화 원문 검토"}]
        return cli_output(cmd, entries=entries)

    batch = evaluate_batch(rows)
    config = CodexCliConfig(executable="test-native-codex")
    j = evaluate_jev_batch(batch, CodexCliClient(config, runner, tmp_path), workers=2,
                           on_record=lambda i, record: progress.append((i, record["status"])))
    followup = next(s for s in inputs if s["question"] == rows[1]["question"])
    assert followup["history"] == [{"turn": 1, "question": rows[0]["question"], "answer": rows[0]["answer"]}]
    assert len(set(output_paths)) == 2
    assert j["completed"] == 2 and j["status"] == "SUCCESS" and j["engine"] == "codex_cli"
    assert j["engine_label"] == "GPT CLI (Codex)" and j["client"]["reasoning_effort"] == "high"
    assert j["client"]["requested_model"] == "gpt-6.1-sol" and j["history_policy"] == "full_original_text"
    assert "J6" in j["records"][1]["criteria"] and j["records"][1]["context"]["history_truncated"] is False
    assert sorted(progress) == [(0, "SUCCESS"), (1, "SUCCESS")]
    html = render_report({**batch, "jev_eval": j})
    assert "GPT CLI (Codex)" in html and "gpt-6.1-sol" in html and "high" in html
    assert "보정된 확률이 아닙니다" in html


@pytest.mark.parametrize("mode,status", [
    ("timeout", "TIMEOUT"), ("missing-exe", "API_ERROR"), ("unsupported-model", "API_ERROR"),
    ("missing-output", "INVALID_RESPONSE"), ("not-json", "INVALID_RESPONSE"),
    ("missing-criterion", "INVALID_RESPONSE"), ("bad-confidence", "INVALID_RESPONSE"),
    ("no-completion", "INVALID_RESPONSE"), ("failed-event", "API_ERROR"),
    ("encoding-error", "API_ERROR"), ("decoding-error", "INVALID_RESPONSE"),
])
def test_failures_are_incomplete_without_shortening_retry_or_model_fallback(tmp_path, mode, status):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        if mode == "timeout":
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        if mode == "missing-exe":
            raise FileNotFoundError("test executable missing")
        if mode == "encoding-error":
            raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "test surrogate")
        if mode == "decoding-error":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "test output")
        if mode == "unsupported-model":
            return cli_output(cmd, returncode=1, events=[{"type": "error", "message": "Model gpt-6.1-sol unavailable; token=secret-test-value"}])
        if mode == "failed-event":
            return cli_output(cmd, events=[{"type": "turn.failed", "error": {"message": "Not logged in"}}])
        if mode == "missing-criterion":
            return cli_output(cmd, entries={})
        if mode == "bad-confidence":
            return cli_output(cmd, entries={"J1": {"choice": "PASS", "confidence": 2, "reason": "test"}})
        if mode == "no-completion":
            return cli_output(cmd, events=[{"type": "thread.started", "thread_id": "test"}])
        output = cli_output(cmd)
        path = Path(cmd[cmd.index("--output-last-message") + 1])
        if mode == "missing-output":
            path.unlink()
        elif mode == "not-json":
            path.write_text("PASS", encoding="utf-8")
        return output

    client = CodexCliClient(CodexCliConfig(executable="test-native-codex"), runner, tmp_path)
    record = evaluate_jev_case("test", "질문", "답", client, criteria=("J1",))
    assert record["status"] == status and record["decisions"] is None
    assert len(calls) == 1 and calls[0][calls[0].index("--model") + 1] == "gpt-6.1-sol"
    assert "secret-test-value" not in (record["error"] or "")


@pytest.mark.parametrize("engine", ["codex_cli", "codex-cli", "gpt_cli", "gpt-cli"])
def test_resolver_accepts_codex_gpt_aliases_and_explicit_environment(tmp_path, monkeypatch, engine):
    monkeypatch.setattr("code_eval.codex_cli_client.find_codex_executable", lambda _: "native-codex")
    monkeypatch.setattr("code_eval.codex_cli_client.check_codex_login", lambda _: None)
    client, config, reason = resolve_jev_client(environment={"SEMANTIC_ENGINE": engine,
        "CODEX_CLI_MODEL": "gpt-6.1-sol", "CODEX_CLI_REASONING": "high", "CODEX_CLI_TIMEOUT": "120"},
        env_path=tmp_path / "missing.env")
    assert isinstance(client, CodexCliClient) and reason is None
    assert config.engine == "codex_cli" and config.model == "gpt-6.1-sol"
    assert config.reasoning_effort == "high" and config.timeout == 120
    monkeypatch.setattr("code_eval.codex_cli_client.find_codex_executable", lambda _: None)
    missing, config, reason = resolve_jev_client(environment={}, env_path=tmp_path / "missing.env", engine=engine)
    assert missing is None and config.model == "gpt-6.1-sol" and "CODEX_CLI_PATH" in reason


def test_missing_cli_login_is_not_configured_before_any_evaluation(tmp_path, monkeypatch):
    monkeypatch.setattr("code_eval.codex_cli_client.find_codex_executable", lambda _: "native-codex")
    monkeypatch.setattr("code_eval.codex_cli_client.check_codex_login", lambda exe: check_codex_login(exe,
        runner=lambda cmd, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="Not logged in")))
    client, config, reason = resolve_jev_client(environment={}, env_path=tmp_path / "missing.env", engine="codex-cli")
    assert client is None and config.engine == "codex_cli" and "codex.cmd login" in reason
    j = evaluate_jev_batch(evaluate_batch([{"question": "질문", "answer": "답"}]), client,
                           config=config, access_reason=reason)
    assert j["status"] == "NOT_CONFIGURED" and j["records"][0]["attempts"] == 0
    assert j["completed"] == 0 and j["client"]["requested_model"] == "gpt-6.1-sol"


def test_recovered_transient_cli_event_does_not_discard_completed_valid_result(tmp_path):
    def runner(cmd, **kw):
        return cli_output(cmd, events=[{"type": "error", "message": "Reconnecting"}, {"type": "turn.completed"}])

    record = evaluate_jev_case("test", "질문", "답", CodexCliClient(CodexCliConfig(), runner, tmp_path), criteria=("J1",))
    assert record["status"] == "SUCCESS"


def test_windows_npm_wrapper_resolves_native_without_shell_or_execution_policy_change(tmp_path, monkeypatch):
    wrapper = tmp_path / "codex.cmd"
    wrapper.write_text("test wrapper", encoding="utf-8")
    native = tmp_path / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"test native")
    monkeypatch.setattr("code_eval.codex_cli_client.platform.machine", lambda: "AMD64")
    monkeypatch.setattr("code_eval.codex_cli_client.shutil.which", lambda _: str(wrapper))
    assert find_codex_executable() == str(native)
    assert find_codex_executable(str(wrapper)) == str(native)
    native.unlink()
    assert find_codex_executable() is None


@pytest.mark.parametrize("kwargs", [{"reasoning_effort": "wrong"}, {"timeout": 0}, {"timeout": float("nan")},
                                    {"timeout": float("inf")}, {"model": " "}, {"max_attempts": 2}])
def test_invalid_cli_configuration_rejected(kwargs):
    with pytest.raises(ValueError):
        CodexCliConfig(**kwargs)


@pytest.mark.parametrize("ids,reference,web_search,search_enabled", [
    (("J5",), None, "live", True),
    (("J5",), "", "live", True),
    (("J5",), "  ", "live", True),
    (("J5",), {}, "live", True),
    (("J5",), [], "live", True),
    (("J5",), "기준 자료", "live", False),
    (("J5",), {"text": "기준 자료"}, "live", False),
    (("J5",), None, "disabled", False),
    (("J1", "J6"), None, "live", False),
])
def test_search_hosts_are_enabled_only_for_unreferenced_j5_while_all_other_limits_remain(
        tmp_path, ids, reference, web_search, search_enabled):
    captured = []

    def runner(cmd, **kwargs):
        captured.append((cmd, kwargs))
        entries = {cid: {"choice": "PASS", "confidence": 0.8, "reason": "fixture"} for cid in ids}
        if "J5" in entries:
            entries["J5"].update(choice="NOT_APPLICABLE", reason="No factual claims in greeting fixture",
                                  fact_check={"basis": "none", "claims": []})
        return cli_output(cmd, entries=entries)

    client = CodexCliClient(CodexCliConfig(web_search=web_search), runner, tmp_path,
                           source_fetcher=lambda _: pytest.fail("Greeting fixture must not fetch sources"))
    client.system_one({"question": "안녕", "answer": "안녕하세요.", "reference": reference},
                      jev_request("안녕", "안녕하세요.", ids)["questions"])
    assert len(captured) == 1
    cmd, kwargs = captured[0]
    enabled = {cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--enable"}
    disabled = {cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--disable"}
    assert enabled == (set(SEARCH_HOST_FEATURES) if search_enabled else set())
    assert disabled == set(DISABLED_FEATURES) | (set() if search_enabled else set(SEARCH_HOST_FEATURES))
    assert {"shell_tool", "unified_exec", "browser_use", "computer_use", "apps", "plugins", "hooks",
            "multi_agent", "multi_agent_v2", "image_generation", "view_image"} <= disabled
    assert not enabled & disabled
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert {"--ignore-user-config", "--ephemeral", "--skip-git-repo-check", 'mcp_servers={}',
            'project_doc_max_bytes=0', 'approval_policy="never"'} <= set(cmd)
    assert f'web_search="{"live" if search_enabled else "disabled"}"' in cmd
    assert kwargs["shell"] is False and cmd[-1] == "-"
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    prompt = kwargs["input"]
    assert ("J5 검색 절차:" in prompt) is search_enabled
    if search_enabled:
        assert "claims에는 모든 사실 주장을 원문 발췌로 남기고" in prompt
        assert "해당 URL과 인용문을 재사용하세요" in prompt
        assert "추가 탐색 없이 즉시 최종 JSON" in prompt
        assert "끝내 근거를 확보하지 못한 주장은 UNVERIFIED" in prompt
        assert "서버가 해당 URL 본문을 다시 받아 인용문 일치를 확인" in prompt


def test_manifest_records_conditional_search_host_policy_separately_from_always_disabled_tools():
    manifest = CodexCliConfig().to_manifest()
    assert manifest["search_host_policy"] == "J5_without_reference_and_live"
    assert manifest["search_host_features"] == ["code_mode", "code_mode_host"]
    assert not set(manifest["search_host_features"]) & set(manifest["disabled_features"])
    assert "shell_tool" in manifest["disabled_features"]
    assert CodexCliConfig(web_search="disabled").to_manifest()["search_host_features"] == []


def search_event(url="https://docs.python.org/3/library/stdtypes.html", **item_extra):
    # Shape captured from CLI 0.159.3: no item.status, results include snippets.
    return {"type": "item.completed", "item": {"id": "search-fixture", "type": "web_search",
        "query": "site:docs.python.org list mutable sequence", "action": {
            "type": "search", "query": "site:docs.python.org list mutable sequence"},
        "results": [{"type": "text_result", "domain": "docs.python.org", "ref_id": "fixture-ref",
                     "title": "Built-in Types", "snippet": "Lists are mutable sequences.", "url": url}],
        **item_extra}}


def test_completed_provider_event_shape_is_recognized_but_agent_prose_is_not_evidence():
    events = [search_event(), {"type": "item.completed", "item": {"type": "agent_message",
              "text": "Search completed https://invented.example/", "url": "https://invented.example/"}},
              {"type": "item.started", "item": {"type": "web_search", "results": [{"url": "https://started.example/"}]}},
              {"type": "item.completed", "item": "malformed"}]
    verification = _verification(events)
    assert verification["search_performed"] is True and verification["search_calls"] == 1
    assert verification["source_urls"] == ["https://docs.python.org/3/library/stdtypes.html"]


@pytest.mark.parametrize("item_extra", [
    {"status": "failed"}, {"status": "in_progress"}, {"status": "searching"},
    {"status": "queued"}, {"status": "cancelled"}, {"status": "unknown"},
    {"error": "provider search failure"},
])
def test_unfinished_or_failed_search_event_never_claims_verified_search(item_extra):
    verification = _verification([search_event(**item_extra)])
    assert verification["search_performed"] is False and verification["search_calls"] == 0
    assert verification["source_urls"] == []


@pytest.mark.parametrize("failure", ["turn_failed", "timeout", "invalid_output"])
def test_cli_failures_preserve_completed_search_provenance_without_a_verdict(tmp_path, failure):
    event = search_event()

    def runner(cmd, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output=json.dumps(event).encode("utf-8"))
        if failure == "turn_failed":
            return cli_output(cmd, events=[event, {"type": "turn.failed", "error": {"message": "fixture failed"}}])
        return cli_output(cmd, entries={}, events=[event, {"type": "turn.completed"}])

    client = CodexCliClient(CodexCliConfig(), runner, tmp_path,
                           source_fetcher=lambda _: pytest.fail("Failed evaluation must not fetch sources"))
    call = client.system_one({"question": "질문", "answer": "답변"}, jev_request("질문", "답변", ("J5",))["questions"])
    assert call.status in ("API_ERROR", "TIMEOUT", "INVALID_RESPONSE")
    assert call.attempts == 1 and "answers" not in call.body
    assert call.body["verification"]["search_performed"] is True
    assert call.body["verification"]["source_urls"] == ["https://docs.python.org/3/library/stdtypes.html"]


@pytest.mark.parametrize("observed_source,body_contains_quote", [(True, True), (True, False), (False, True)])
def test_web_claim_requires_observed_url_and_fetched_source_quote(tmp_path, observed_source, body_contains_quote):
    url = "https://docs.python.org/3/library/stdtypes.html"
    entries = {"J5": {"choice": "PASS", "confidence": 0.8, "reason": "Official source agrees",
        "fact_check": {"basis": "web", "claims": [{"claim": "Python lists are mutable.", "verdict": "PASS",
            "reason": "The official Python documentation identifies lists as mutable sequences.",
            "sources": [{"url": url, "title": "Built-in Types", "quote": "Lists are mutable sequences.",
                         "authority": "primary",
                         "authority_reason": "The Python project's official language documentation.",
                         "temporal_relevance": "The documented built-in list behavior matches the question's scope."}]}]}}}
    fetched = []

    def fetcher(address):
        fetched.append(address)
        return {"status": "ok", "text": "Python built-in types. Lists are mutable sequences." if body_contains_quote else "Unrelated page content.",
                "final_url": address, "checked_at": "2026-10-09T00:00:00Z", "content_type": "text/plain"}

    def runner(cmd, **kwargs):
        return cli_output(cmd, entries=entries, events=[
            search_event(url if observed_source else "https://docs.python.org/other"), {"type": "turn.completed"}])

    client = CodexCliClient(CodexCliConfig(), runner, tmp_path, source_fetcher=fetcher)
    call = client.system_one({"question": "Are Python lists mutable?", "answer": "Python lists are mutable."},
                            jev_request("question", "answer", ("J5",))["questions"])
    assert call.status == "OK"
    answer = call.body["answers"]["J5"]
    assert fetched == ([url] if observed_source else [])
    if observed_source and body_contains_quote:
        assert answer["choice"] == "PASS"
        assert answer["fact_check"]["claims"][0]["sources"][0]["quote_verified"] is True
    else:
        assert answer["choice"] == "UNVERIFIED"
