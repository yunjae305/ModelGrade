# -*- coding: utf-8 -*-
"""처음 설정(aiona-chat-eval setup, 사용자 10-26 저녁) — .env 키를 물어 넣되 값은 화면에 쓰지 않고, 다른 줄은 그대로 · 어느 폴더에서나 설치 폴더의 .env 를 읽음."""
from __future__ import annotations

from pathlib import Path

from code_eval import llm_client
from tools import setup_wizard as sw


def test_write_env_replaces_only_the_key_line_and_keeps_comments(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("# 주석 OPENAI_API_KEY=예시\nOPENAI_API_KEY=\n# AIONA_LOGIN_ID=\nAIONA_URL=https://x\n", encoding="utf-8")
    sw.write_env({"OPENAI_API_KEY": "sk-test", "TYPESAFE_API_KEY": "ts"}, env)
    assert env.read_text(encoding="utf-8") == "# 주석 OPENAI_API_KEY=예시\nOPENAI_API_KEY=sk-test\n# AIONA_LOGIN_ID=\nAIONA_URL=https://x\nTYPESAFE_API_KEY=ts\n"
    assert sw.read_env(env) == {"OPENAI_API_KEY": "sk-test", "AIONA_URL": "https://x", "TYPESAFE_API_KEY": "ts"}


def test_setup_keys_asks_only_empty_keys_and_never_prints_values(tmp_path: Path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=\nTYPESAFE_API_KEY=old\nAIONA_LOGIN_ID=\nAIONA_LOGIN_PASSWORD=\n", encoding="utf-8")
    monkeypatch.setattr(sw, "ENV", env)
    asked = []

    def reader(prompt: str, secret: bool) -> str:
        asked.append(secret)
        return {0: "sk-secret-value", 1: "", 2: "pw-secret"}[len(asked) - 1]
    missing = sw.setup_keys(reader=reader)
    out = capsys.readouterr().out
    assert asked == [True, False, True]   # OPENAI(숨김) · AIONA 아이디(보임) · AIONA 비밀번호(숨김) — 이미 있는 Jev 키는 묻지 않음
    assert "sk-secret-value" not in out and "pw-secret" not in out and "TYPESAFE_API_KEY 있음" in out
    assert sw.read_env(env)["OPENAI_API_KEY"] == "sk-secret-value" and sw.read_env(env)["AIONA_LOGIN_ID"] == "" and missing == []


def test_noninteractive_setup_only_creates_env(tmp_path: Path, monkeypatch, capsys):
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(sw, "ENV", tmp_path / ".env")
    monkeypatch.setattr(sw, "EXAMPLE", tmp_path / ".env.example")
    monkeypatch.setenv("MODELGRADE_NONINTERACTIVE", "1")
    assert sw.main([]) == 0 and (tmp_path / ".env").read_text(encoding="utf-8") == "OPENAI_API_KEY=\n"
    assert "aiona-chat-eval setup" in capsys.readouterr().out


def test_env_value_falls_back_to_the_install_folder(tmp_path: Path, monkeypatch):
    """전역 modelgrade 명령 — 지금 폴더에 .env 가 없으면 MODELGRADE_HOME 의 .env (지금 폴더에 있으면 그것)."""
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir(), work.mkdir()
    (home / ".env").write_text("JEV_MODEL=jev-from-home\n", encoding="utf-8")
    monkeypatch.chdir(work)
    monkeypatch.delenv("JEV_MODEL", raising=False)
    monkeypatch.delenv("MODELGRADE_HOME", raising=False)
    assert llm_client.env_value("JEV_MODEL") is None
    monkeypatch.setenv("MODELGRADE_HOME", str(home))
    assert llm_client.env_value("JEV_MODEL") == "jev-from-home"
    (work / ".env").write_text("JEV_MODEL=jev-here\n", encoding="utf-8")
    assert llm_client.env_value("JEV_MODEL") == "jev-here"


def test_claude_login_state_reads_auth_status_json(monkeypatch):
    monkeypatch.setattr(sw, "_run", lambda cmd, **k: (0, 'note\n{"loggedIn": false, "authMethod": "none"}'))
    assert sw.claude_logged_in("claude") is False
    monkeypatch.setattr(sw, "_run", lambda cmd, **k: (0, '{"loggedIn": true}'))
    assert sw.claude_logged_in("claude") is True
