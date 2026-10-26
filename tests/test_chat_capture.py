# -*- coding: utf-8 -*-
"""빈 출력 요구에 복사 아이콘이 안 뜬 대화 화면(사용자 10-26): 답 영역만 찍고, 실행 폴더에 질문 이름으로 옮기고, 보고서 원문에 그림으로 넣는다."""
from __future__ import annotations

from pathlib import Path

from webapp import chat_capture as cc


class FakePage:
    def __init__(self, rect):
        self.rect, self.shots = rect, []

    def evaluate(self, js, arg):
        assert arg[0] == "user" and arg[3] == cc.SIDEBAR   # 질문 말풍선 선택자 · 옆 목록 선택자를 넘긴다
        return self.rect

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path, clip, type, quality):
        self.shots.append((clip, type))
        Path(path).write_bytes(b"\xff\xd8jpeg")


def test_capture_clips_to_the_last_turn_and_skips_when_no_chat_column(tmp_path: Path):
    page = FakePage({"x": 300, "y": 10, "width": 700, "height": 400})
    got = cc.capture_last_turn(page, "user", "answer", tmp_path / "a.jpg")
    assert got and page.shots == [({"x": 300, "y": 10, "width": 700, "height": 400}, "jpeg")]   # 잘라 찍음(화면 전체 아님)
    none = FakePage(None)   # 대화 칸을 못 찾음(옆 목록까지 품은 칸뿐) — 화면 전체로 대신하지 않는다
    assert cc.capture_last_turn(none, "user", "answer", tmp_path / "b.jpg") is None and none.shots == []
    assert cc.capture_last_turn(page, "", "answer", tmp_path / "c.jpg") is None


def test_screenshot_is_kept_per_question_and_shown_in_reports(tmp_path: Path, monkeypatch):
    import tools.modelgrade as mg
    from tools import report_html as rh
    monkeypatch.setattr(mg, "ROOT", tmp_path)
    monkeypatch.setattr(rh, "ROOT", tmp_path)
    src = tmp_path / "debug" / "1-empty-output-no-copy.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"\xff\xd8jpeg")
    rel = mg.keep_screenshot("abc123", "gpt", "Q61_T1", str(src))
    assert rel == "화면/gpt/Q61_T1.jpg" and (tmp_path / "data" / "judge_runs" / "abc123" / rel).is_file() and not src.exists()
    run = {"run_id": "abc123", "datasets": {"gpt": [{"case_id": "Q61_T1", "question": "아무것도 출력하지 마세요.", "answer": "",
                                                    "collection": {"status": "COPY_FAILED", "screenshot": rel}}]}}
    block = rh._orig_block(rh.originals(run)[("gpt", 61)])
    assert "data:image/jpeg;base64," in block and "대화 화면" in block   # 판정보고서 원문에 그림이 들어간다
    assert rh.screenshot_img("abc123", "화면/gpt/없음.jpg") == ""         # 파일이 없으면 그림 없이
