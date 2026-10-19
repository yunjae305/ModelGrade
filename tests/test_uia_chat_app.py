"""Claude 앱 UIA 수집기의 순수 도우미 — 앱 없이 도는 부분만."""
from pathlib import Path

from webapp.uia_chat_app import ClaudeAppChat, file_dialog_text, model_ok


def test_file_dialog_text_quotes_only_when_several_files():
    one = file_dialog_text([Path("C:/a/보고서.pdf")])
    assert one.endswith("보고서.pdf") and '"' not in one
    two = file_dialog_text([Path("C:/a/x.txt"), Path("C:/a/y.png")])
    assert two.count('"') == 4 and 'x.txt" "' in two


def test_model_ok_requires_sonnet_55_and_medium_effort():
    assert model_ok("Sonnet 5.5 중간")
    assert not model_ok("Opus 5.5 엑스트라") and not model_ok("Sonnet 5.5 높음") and not model_ok("")
    assert ClaudeAppChat.METHOD == "claude-app-uia-v1" and "Sonnet 5.5" in ClaudeAppChat.MODEL_LABEL
