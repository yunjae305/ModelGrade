"""공개본 내보내기 — 제외 목록 · 결과 폴더 선택 · 복사본 점검 (할일 14번 준비, 10-25 밤)."""
from __future__ import annotations

from pathlib import Path

import pytest

from tools import export_public as ep


def test_plan_drops_only_minutes_and_keeps_answers_and_results():
    """사용자 결정(10-25 밤): 회의록만 빼고 질문 목록 · 수집 답변 · 평가 결과는 넣는다. 결과 폴더는 --no-results 일 때만 뺀다."""
    files = ["README.md", "CLAUDE.md", "tools/a.py", "참고자료/분석/회의록_2026-10-20_원문.txt", "참고자료/질문세트_78/questions.json",
             "참고자료/질문세트_326_생성프롬프트v2/답변/aiona.csv", "result/2026-10-25_12-37-03-450/report.html"]
    keep, dropped = ep.plan(files, include_results=True)
    assert "참고자료/분석/회의록_2026-10-20_원문.txt" not in keep and len(keep) == 6
    keep2, _ = ep.plan(files, include_results=False)
    assert "result/2026-10-25_12-37-03-450/report.html" not in keep2 and "참고자료/질문세트_326_생성프롬프트v2/답변/aiona.csv" in keep2


def test_export_copies_and_flags_real_key_shapes_and_user_paths(tmp_path: Path):
    src = tmp_path / "src"; (src / "tools").mkdir(parents=True)
    (src / "README.md").write_text("시험용 가짜 값 sk-semantic-test-0001 은 괜찮다\n", encoding="utf-8")
    (src / "tools" / "a.py").write_text("KEY = 'sk-" + "A" * 40 + "'\npath = r'C:" + "\\" + "Users" + "\\" + "someone" + "\\" + "x'\n", encoding="utf-8")
    (src / "참고자료" / "분석").mkdir(parents=True)
    (src / "참고자료" / "분석" / "회의록_x.txt").write_text("내부", encoding="utf-8")
    rep = ep.export(tmp_path / "out", root=src, files=["README.md", "tools/a.py", "참고자료/분석/회의록_x.txt"])
    assert rep["kept"] == 2 and not (tmp_path / "out" / "참고자료" / "분석" / "회의록_x.txt").exists()
    assert {(h["file"], h["kind"]) for h in rep["hits"]} == {("tools/a.py", "OpenAI · Anthropic 키 모양"), ("tools/a.py", "사용자 절대 경로")}
    assert (tmp_path / "out" / "EXPORT_MANIFEST.json").is_file()
    with pytest.raises(SystemExit):   # 비어 있지 않은 폴더에는 내보내지 않음
        ep.export(tmp_path / "out", root=src, files=["README.md"])
