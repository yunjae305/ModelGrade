"""판정 심판 비교(tools/judge_claude_compare) — 결과 폴더 양식(10-22)과 본 결과 폴더 찾기."""
from __future__ import annotations

import json
from datetime import datetime

from tools import judge_claude_compare as jc
from tools import result_dirs


def test_compare_result_dir_keeps_first_start_and_does_not_hide_the_run_result_dir(tmp_path):
    """비교 폴더는 처음 시작 시각 이름 · 이어 돌려도 같은 폴더, run.json 에 run_id 대신 compare_of —
    실행 ID 로 결과 폴더를 찾으면(결과 요약 빌더 · modelgrade report) 더 새로 만든 비교 폴더가 아니라 본 결과 폴더가 나온다."""
    base, out = tmp_path / "결과", tmp_path / "judge"
    out.mkdir()
    main = result_dirs.ensure({"run_id": "r1"}, base, now=datetime(2026, 10, 21, 23, 2, 52, 283000))
    d = jc.ensure_result_dir(out, "r1", base, started=datetime(2026, 10, 22, 14, 25, 55, 176000))
    assert d.name == "2026-10-22_14-25-55-176"
    assert jc.ensure_result_dir(out, "r1", base, started=datetime(2026, 10, 22, 18, 0)) == d   # 이어 돌리면 처음 폴더
    meta = json.loads((d / "run.json").read_text(encoding="utf-8"))
    assert meta["compare_of"] == "r1" and "run_id" not in meta and meta["kind"] == "judge-compare"
    assert result_dirs.find("r1", base) == main
