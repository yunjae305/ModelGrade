# -*- coding: utf-8 -*-
"""멀티턴 Codex 다시 묻기 (10-22): 한 턴을 2번 물어도 빈 항목이 줄지 않으면 그 턴은 건너뛰고 뒤 턴을 묻는다
(실행 50ca3ccccace Claude CLI 비교 Q2 gemini: 7턴만 상한까지 15번 → 8~10턴 미질문, 항목 109개 보류)."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_judge_screen import FakeCodex, FakeDecisions
from tools import judge_v4 as jv


class StuckTurnCodex(FakeCodex):
    """첫 호출(모든 턴)은 턴 1 · 턴 3 을 비워 답하고, 다시 물으면 턴 1 만 늘 비워 답한다."""

    def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
        st, out, err = super().judge(prompt, input_text, search, tag, output_schema, images)
        empty = {1, 3} if len(self.inputs) == 1 else {1}
        for t in out["turns"]:
            if t["turn"] in empty:
                t["items"] = []
        return st, out, err


def test_multi_turn_reask_skips_a_turn_that_stays_empty_and_still_asks_later_turns(tmp_path: Path, monkeypatch):
    for sub in ("inputs", "codex", "jev", "decisions"):
        (tmp_path / sub).mkdir(exist_ok=True)
    fixed = json.loads(jv.JEV_V4.read_text(encoding="utf-8"))
    turns = [{"case_id": f"Q1_T{n}", "question": f"존댓말로 답해 주세요. {n}번째 회의 시간을 알려 주세요.", "turn": n} for n in (1, 2, 3)]
    answers = {f"Q1_T{n}": f"네, {n}번째 회의는 10시입니다." for n in (1, 2, 3)}
    logs = []
    monkeypatch.setattr(jv, "say", lambda *a, **k: logs.append(" ".join(map(str, a))))
    dec = FakeDecisions({"J4-2.a": {"choice": "PASS", "probabilities": {"PASS": 0.6, "FAIL": 0.4}, "confidence": 0.6}})
    codex = StuckTurnCodex()
    line = jv.screen_one(1, "gpt", turns, answers, {}, [], "PROMPT", fixed, codex, dec, tmp_path)
    assert [list(c["판정할_항목"]) for c in codex.inputs[1:]] == [["1"], ["1"], ["3"]]   # 턴 1 은 2번 묻고 건너뜀, 뒤 턴 3 도 물음
    assert any("턴 1 은 2번 물어도 답이 늘지 않아 건너뜀" in s for s in logs)
    c = line["calls"]["codex"]
    assert c["rounds"] == 4 < 2 * 3 + 6 and c["skipped_turns"] == [1]                   # 상한(2×걸린 턴 수+6) 안에서 끝남
    assert list(c["missing_items"]) == ["1"] and c["error"].startswith("끝내 답하지 않은 항목")
    assert line["turn_results"]["1"]["items"]["J4-2.a"]["decision"] == "UNVERIFIED"         # 건너뛴 턴의 남은 항목은 지금처럼(판정 없음 → 보류)
    t3 = line["turn_results"]["3"]["items"]["J4-2.a"]
    assert t3["codex"] == "PASS" and t3["decision"] == "PASS"                             # 뒤 턴은 물어서 판정됨
