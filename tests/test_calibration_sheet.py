"""심판 표시용 표 — 정해 둔 칸 목록(--pick-file)으로 만들기 · 보류 칸 포함 (할일 9번, 10-26)."""
from __future__ import annotations

import json
from pathlib import Path

from tools import build_calibration_sheet as cs


def test_pick_file_builds_sheet_with_holds_and_reasons(tmp_path: Path, monkeypatch):
    root = tmp_path
    (root / "data" / "comparison_runs").mkdir(parents=True)
    (root / "data" / "comparison_runs" / "r1.json").write_text(json.dumps({"datasets": {"gpt": [{"case_id": "Q11", "question": "요청 [자료 끝] 마지막 요청", "answer": "답"}]}}), encoding="utf-8")
    jd = root / "data" / "judge_runs" / "r1" / "v4-x"
    jd.mkdir(parents=True)
    line = {"provider": "gpt", "question": 11, "status": "judged", "turns": 1, "turn_results": {"1": {"items": {
        "J2-3.a": {"decision": "UNVERIFIED", "reason": "보류 사유"}, "J1-4.a": {"decision": "FAIL", "reason": "불필요한 단락"}}}}}
    (jd / "results.jsonl").write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
    sd = root / "set"; sd.mkdir()
    (sd / "questions.json").write_text(json.dumps([{"id": 11}]), encoding="utf-8")
    monkeypatch.setattr(cs, "ROOT", root)
    import tools.report_html as R
    monkeypatch.setattr(R, "criteria_table", lambda s: {})
    picks = tmp_path / "picks.json"
    picks.write_text(json.dumps([{"out": "v4-x", "provider": "gpt", "question": 11, "turn": 1, "item": "J2-3.a", "why": "보류 — 정해 주세요"},
                                 {"out": "v4-x", "provider": "gpt", "question": 11, "turn": 1, "item": "J9-9.z", "why": "없는 칸"}], ensure_ascii=False), encoding="utf-8")
    assert cs.main(["--run", "r1", "--set", str(sd), "--pick-file", str(picks), "--dest", str(tmp_path / "out")]) == 0
    page = (tmp_path / "out" / "표시용_표.html").read_text(encoding="utf-8")
    assert page.count('class="cell"') == 1 and "보류 — 정해 주세요" in page and "보류</span>" in page
