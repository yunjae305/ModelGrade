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


def test_codex_calls_count_first_call_and_reasks_like_claude_calls(tmp_path, monkeypatch):
    """Codex 호출은 칸마다 1이 아니라 그 판정의 첫 호출 + 다시 묻기(rounds) — Claude 호출(claude_calls.jsonl 줄 수)과 같은 단위 (10-26).
    전에는 칸마다 1로 세어 실제 Codex 578번을 206번(칸 수)으로 적었다."""
    monkeypatch.setattr(jc, "ROOT", tmp_path)
    base, out = tmp_path / "data" / "judge_runs" / "r1" / "v4", tmp_path / "data" / "judge_runs" / "r1" / "cl"
    base.mkdir(parents=True), out.mkdir(parents=True)
    line = lambda q, codex: json.dumps({"provider": "gpt", "question": q, "verdict": "PASS", "calls": {"codex": codex}}) + "\n"
    (base / "results.jsonl").write_text(line(1, {"status": "OK", "rounds": 3, "seconds": 30.0}) + line(2, {"status": "OK", "rounds": 1, "seconds": 10.0})
                                        + line(1, {"status": "OK", "rounds": 9, "reused": True}), encoding="utf-8")   # 저장 출력을 다시 쓴 줄은 세지 않음
    (out / "results.jsonl").write_text(line(1, {"status": "OK", "rounds": 2, "seconds": 20.0}) + line(2, {"status": "OK", "rounds": 2, "seconds": 20.0}),
                                       encoding="utf-8")
    (out / "claude_calls.jsonl").write_text("".join(json.dumps({"tag": f"gpt Q{q}", "ok": True, "seconds": 10.0}) + "\n" for q in (1, 1, 2, 2)), encoding="utf-8")
    res, _md = jc.compare_core("r1", "v4", "cl")
    assert res["codex_first_calls"]["gpt"]["cells"] == 2 and res["codex_first_calls"]["gpt"]["calls"] == 4
    assert res["claude_tokens"]["gpt"]["calls"] == 4
