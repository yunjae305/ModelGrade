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


def test_copied_markdown_list_matches_rendered_screen_text():
    """번호 목록·표의 마크다운 원문은 화면 글과 번호·테두리가 달라도 같은 글로 본다; 전혀 다른 글은 아니다."""
    from webapp.uia_chat_app import _matches_screen, _squash
    copied = "1. 9:20~9:30 물품 운반(10분): 물품을 먼저 옮겨 주세요.\n2. 9:30~9:45 진열(15분): 가장 오래 걸리니 이어서 진열해 주세요."
    screen = _squash("Claude 응답: 9:20~9:30 물품 운반(10분): 물품을 먼저 옮겨 주세요.\n4개 항목 목록 형식을 정리 중.\n9:30~9:45 진열(15분): 가장 오래 걸리니 이어서 진열해 주세요.")
    assert _matches_screen(copied, screen)
    assert not _matches_screen("네, 판정은 그렇게 돕니다. Codex 는 터미널 명령으로 부르고 Jev 는 API 로 묻습니다. 둘을 동시에 돌립니다.", screen)
