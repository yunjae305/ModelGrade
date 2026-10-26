# -*- coding: utf-8 -*-
"""회의 흐름(상황별 1차 심판 · 교차 확인, 10-23 회의 · 사용자 10-26) — 단일 질문은 Jev(2+1) + Decisions(1) 교차, 그 밖은 Decisions 1번. 심판은 가짜."""
from __future__ import annotations

import json
from pathlib import Path

from tools import judge_v4 as jv

SURE = {"choice": "PASS", "probabilities": {"PASS": 0.95, "FAIL": 0.03, "NOT_APPLICABLE": 0.02}, "confidence": 0.95}


class FakeJudge:
    """항목마다 정해 둔 답(호출 순서마다 다르게 줄 수도 있음)을 돌려주는 1차 심판."""

    def __init__(self, special=None, per_call=None):
        self.special, self.per_call, self.calls = special or {}, per_call or {}, 0

    def judge(self, state, questions):
        self.calls += 1
        ans = {}
        for k in questions:
            item = k.split(":", 1)[1]
            seq = self.per_call.get(item)
            ans[k] = (seq[min(self.calls, len(seq)) - 1] if seq else None) or self.special.get(item) or SURE
        return "OK", {"answers": ans, "usage": {"input_tokens": 10}}, ""


class FakeCodex:
    model, reasoning = "fake-codex", "low"

    def __init__(self):
        self.inputs = []

    def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
        doc = json.loads(input_text)
        self.inputs.append(doc)
        turns = [{"turn": int(n), "situations": [], "items": [{"id": i, "decision": "PASS", "question_quote": "", "answer_quote": "", "reason": "r", "quote_turn": None, "fact": None}
                                                                for i in items]} for n, items in doc["판정할_항목"].items()]
        return "OK", {"turns": turns, "conclusion": {"verdict": "PASS", "reason": ""}}, ""


def test_jev_two_votes_then_third_only_when_split():
    q = {"T1:J2-2.a": {}, "T1:J1-1.a": {}}
    same = FakeJudge()
    status, body, _, oks, attempts = jv.jev_votes_adaptive({}, q, same)
    assert status == "OK" and attempts == 2 and len(oks) == 2
    fail = {"choice": "FAIL", "probabilities": {"FAIL": 0.6}, "confidence": 0.6}
    split = FakeJudge(per_call={"J2-2.a": [SURE, fail, fail]})
    status, body, _, oks, attempts = jv.jev_votes_adaptive({}, q, split)
    assert attempts == 3 and body["answers"]["T1:J2-2.a"]["choice"] == "FAIL" and body["answers"]["T1:J2-2.a"]["votes"] == ["PASS", "FAIL", "FAIL"]


def test_cross_flag_cases():
    na = {"choice": "NOT_APPLICABLE", "probabilities": {"NOT_APPLICABLE": 0.9}, "confidence": 0.9}
    weak = {"choice": "PASS", "probabilities": {"PASS": 0.75, "FAIL": 0.1}, "confidence": 0.75}
    fail = {"choice": "FAIL", "probabilities": {"FAIL": 0.9}, "confidence": 0.9}
    assert jv.cross_flag("J2-2.a", SURE, SURE) == ""                       # 같은 답 · 둘 다 0.8 이상 → 1차에서 끝
    assert "다름" in jv.cross_flag("J2-2.a", SURE, na)
    assert "애매" in jv.cross_flag("J2-2.a", SURE, weak)
    assert "FAIL" in jv.cross_flag("J2-2.a", fail, SURE)
    assert jv.cross_flag("J5-1.a", SURE, SURE).startswith("사실 확인")      # 사실 확인은 늘 Codex
    assert jv.cross_flag("J2-2.a", None, SURE) == ""                       # 한쪽만 — 선별 규칙(확신한 PASS 는 안 보냄)
    e = jv.cross_entry("J2-2.a", fail, SURE)
    assert e["choice"] == "FAIL" and e["cross"]["jev"]["choice"] == "FAIL" and e["cross"]["decisions"]["choice"] == "PASS" and e["cross_flag"]


def _setup(tmp_path: Path, monkeypatch):
    for sub in ("inputs", "codex", "jev", "decisions"):
        (tmp_path / sub).mkdir(exist_ok=True)
    monkeypatch.setattr(jv, "FIRST_JUDGE_MODE", "situational")
    monkeypatch.setattr(jv, "question_tokens", lambda turns, answers: 500)   # Jev 가 받는 크기
    jv.say = lambda *a, **k: None
    return json.loads(jv.JEV_V4.read_text(encoding="utf-8"))


def test_single_question_cross_checks_and_sends_only_disagreements(tmp_path: Path, monkeypatch):
    fixed = _setup(tmp_path, monkeypatch)
    jev = FakeJudge()
    dec = FakeJudge({"J2-2.a": {"choice": "NOT_APPLICABLE", "probabilities": {"NOT_APPLICABLE": 0.9}, "confidence": 0.9}})
    codex = FakeCodex()
    turns = [{"case_id": "Q1_T1", "question": "존댓말로 답해 주세요. 회의 시간을 알려 주세요.", "turn": 1}]
    line = jv.screen_one(1, "gpt", turns, {"Q1_T1": "네, 회의는 10시입니다."}, {}, [], "PROMPT", fixed, codex, dec, tmp_path, jev=jev)
    sent = codex.inputs[0]["판정할_항목"]["1"]
    assert "J2-2.a" in sent and "J1-4.a" not in sent                         # 두 심판이 다른 칸만(+ 늘 보는 사실 확인), 같고 확실한 칸은 끝
    items = line["turn_results"]["1"]["items"]
    assert "다름" in items["J2-2.a"]["flagged"] and items["J2-2.a"]["first_cross"]["decisions"]["choice"] == "NOT_APPLICABLE"
    assert items["J1-4.a"]["codex"] is None and items["J1-4.a"]["how"].startswith("Jev·Decisions")
    assert jev.calls == 1 and dec.calls == 1                                  # Jev 1번(JEV_VOTES 기본 1, 10-26) · Decisions 1번
    by = line["calls"]["decisions"]["attempts_by"]
    assert by == {"jev": 1, "decisions": 1} and line["calls"]["decisions"]["first_judge"] == {"1": "jev+decisions"}


def test_multi_turn_uses_decisions_once_per_turn(tmp_path: Path, monkeypatch):
    fixed = _setup(tmp_path, monkeypatch)
    jev, dec = FakeJudge(), FakeJudge()
    turns = [{"case_id": "Q1_T1", "question": "존댓말로 답해 주세요. 회의를 잡아 주세요.", "turn": 1},
             {"case_id": "Q1_T2", "question": "그 회의를 30분 늦춰 주세요.", "turn": 2}]
    answers = {"Q1_T1": "네, 10시로 잡겠습니다.", "Q1_T2": "네, 10시 30분으로 바꾸겠습니다."}
    line = jv.screen_one(1, "gpt", turns, answers, {}, [], "PROMPT", fixed, FakeCodex(), dec, tmp_path, jev=jev)
    assert jev.calls == 0 and dec.calls == 2 and set(line["calls"]["decisions"]["first_judge"].values()) == {"decisions"}
