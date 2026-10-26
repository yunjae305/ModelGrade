# -*- coding: utf-8 -*-
"""복사 아이콘이 안 떠 못 받은 답의 글 로그(사용자 10-26 저녁 — 이미지 대신 글로): 답 자리에 보이는 글만 읽고, 결과 보고서 보류 칸에 글로 보인다."""
from __future__ import annotations

from webapp import chat_capture as cc


class FakePage:
    def __init__(self, got):
        self.got = got

    def evaluate(self, js, arg):
        assert arg == ["user", cc.SIDEBAR]   # 질문 말풍선 선택자 · 옆 목록 선택자를 넘긴다 — 옆 목록을 품은 칸은 읽지 않는다
        return self.got


def test_read_last_turn_keeps_text_after_the_last_question_and_skips_without_chat_column():
    long = "응답을 생성하는 중입니다 " * 200
    got = cc.read_last_turn(FakePage({"question": "아무것도 출력하지 마세요.", "after": "  응답 생성 중 오류가 발생했습니다.\n다시 시도  "}), "user", "no-answer")
    assert got["label"] == "no-answer" and got["question_tail"] == "아무것도 출력하지 마세요." and got["answer_area"] == "응답 생성 중 오류가 발생했습니다.\n다시 시도"
    cut = cc.read_last_turn(FakePage({"question": "q", "after": long}), "user")
    assert len(cut["answer_area"]) <= cc.MAX_CHARS + 10 and cut["answer_area"].endswith("…(줄임)")
    assert cc.read_last_turn(FakePage(None), "user") is None and cc.read_last_turn(FakePage({}), "") is None


def test_collection_log_lines_are_shown_as_text_in_reports():
    from tools import report_html as rh
    from tools.collection_log import log_lines, needs_log
    col = {"status": "COPY_FAILED", "error": "빈 출력 요구 — 90초 안에 복사 버튼이 뜨지 않음", "latency_ms": 95_000, "collected_at": "2026-10-26T07:20:31+00:00",
           "turn_log": {"read_at": "2026-10-26T07:20:30+00:00", "answer_area": ""}}
    lines = log_lines(col)
    assert lines[0].endswith("수집 상태: COPY_FAILED (복사 못 함) — 빈 출력 요구 — 90초 안에 복사 버튼이 뜨지 않음") and "기다린 시간: 95초" in lines
    assert lines[-1].endswith("답 자리에 보인 글: (아무 글도 없음)")
    assert "이미지만" in log_lines({"status": "COPY_FAILED", "screenshot": "화면/gpt/Q61_T1.jpg"})[-1]   # 옛 수집은 글이 없다고 밝힘
    assert needs_log(col, "") and not needs_log({"status": "SUCCESS"}, "답") and log_lines(None) == []
    run = {"run_id": "abc123", "datasets": {"gpt": [{"case_id": "Q61_T1", "question": "아무것도 출력하지 마세요.", "answer": "", "collection": col}]}}
    block = rh._orig_block(rh.originals(run)[("gpt", 61)])
    assert "수집 로그" in block and "복사 못 함" in block and "<img" not in block   # 판정보고서 원문에 그림 대신 글
    assert rh.collection_log_html([]) == ""
