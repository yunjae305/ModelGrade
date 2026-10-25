"""공개본 내보내기 — 제외 목록 · 결과 폴더 선택 · 복사본 점검 (할일 14번 준비, 10-25 밤)."""
from __future__ import annotations

from pathlib import Path

import pytest

from tools import export_public as ep


def test_plan_drops_internal_files_and_results_unless_asked():
    files = ["README.md", "CLAUDE.md", "tools/a.py", "참고자료/이어서진행_2026-10-23.md", "참고자료/분석/회의록_2026-10-20_원문.txt",
             "참고자료/결과/2026-10-25_12-37-03-450/report.html", "참고자료/기준/채팅품질 평가기준_v2.md"]
    keep, dropped = ep.plan(files, include_results=False)
    assert keep == ["README.md", "tools/a.py", "참고자료/기준/채팅품질 평가기준_v2.md"]
    assert sum(len(v) for v in dropped.values()) == 4
    keep2, _ = ep.plan(files, include_results=True)
    assert "참고자료/결과/2026-10-25_12-37-03-450/report.html" in keep2


def test_export_copies_and_flags_real_key_shapes_and_user_paths(tmp_path: Path):
    src = tmp_path / "src"; (src / "tools").mkdir(parents=True)
    (src / "README.md").write_text("시험용 가짜 값 sk-semantic-test-0001 은 괜찮다\n", encoding="utf-8")
    (src / "tools" / "a.py").write_text("KEY = 'sk-" + "A" * 40 + "'\npath = r'C:" + "\\" + "Users" + "\\" + "someone" + "\\" + "x'\n", encoding="utf-8")
    (src / "CLAUDE.md").write_text("내부", encoding="utf-8")
    rep = ep.export(tmp_path / "out", root=src, files=["README.md", "tools/a.py", "CLAUDE.md"])
    assert rep["kept"] == 2 and not (tmp_path / "out" / "CLAUDE.md").exists()
    assert {(h["file"], h["kind"]) for h in rep["hits"]} == {("tools/a.py", "OpenAI · Anthropic 키 모양"), ("tools/a.py", "사용자 절대 경로")}
    assert (tmp_path / "out" / "EXPORT_MANIFEST.json").is_file()
    with pytest.raises(SystemExit):   # 비어 있지 않은 폴더에는 내보내지 않음
        ep.export(tmp_path / "out", root=src, files=["README.md"])
