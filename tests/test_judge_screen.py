# -*- coding: utf-8 -*-
"""선별 판정(B, judge-v5-screen) — Decisions 전수 → 걸린 항목만 Codex · 0.7 규칙. 심판은 가짜(호출 없음)."""
from __future__ import annotations

import json
from pathlib import Path

from tools import judge_v4 as jv


def test_fail_prob_reads_dict_or_list_or_defaults():
    assert jv.fail_prob({"decision": "FAIL", "probabilities": {"FAIL": 0.55, "PASS": 0.45}}) == 0.55
    assert jv.fail_prob({"decision": "FAIL", "probabilities": [{"value": "FAIL", "probability": 0.8}]}) == 0.8
    assert jv.fail_prob({"decision": "FAIL"}) == 1.0      # 확률 없는 옛 Jev 응답 — 확신한 것으로
    assert jv.fail_prob({"decision": "PASS"}) == 0.0


def test_combine_weighted_applies_0_7_rule():
    codex_pass = {"decision": "PASS"}
    strong = {"decision": "FAIL", "probabilities": {"FAIL": 0.85}}
    weak = {"decision": "FAIL", "probabilities": {"FAIL": 0.55}}
    assert jv.combine_weighted({"decision": "FAIL"}, weak, "Decisions", 0.7)[0] == "FAIL"           # Codex FAIL 은 확정
    dec, how = jv.combine_weighted(codex_pass, strong, "Decisions", 0.7)
    assert dec == "UNVERIFIED" and "심판 갈림" in how and "0.85" in how                        # 강한 FAIL → 보류
    dec, how = jv.combine_weighted(codex_pass, weak, "Decisions", 0.7)
    assert dec == "PASS" and "약한 FAIL 0.55" in how                                             # 약한 FAIL → Codex 따름
    assert jv.combine_weighted(codex_pass, strong, "Jev", 0.7, jev_trimmed=True)[0] == "PASS"    # 줄인 자료로 본 둘째 심판은 참고만
    assert jv.combine_weighted(None, strong, "Decisions", 0.7)[0] == "UNVERIFIED"


def test_screen_flag_rules():
    assert jv.screen_flag("J2-2.a", {"choice": "PASS", "probabilities": {"PASS": 0.95, "FAIL": 0.05}, "confidence": 0.95}) == (False, "")
    assert jv.screen_flag("J2-2.a", {"choice": "FAIL", "probabilities": {"FAIL": 0.6}, "confidence": 0.6})[0]
    assert jv.screen_flag("J2-2.a", {"choice": "PASS", "probabilities": {"PASS": 0.65, "FAIL": 0.35}, "confidence": 0.65})[0]   # FAIL 0.35 ≥ 0.3
    assert jv.screen_flag("J2-2.a", {"choice": "NOT_APPLICABLE", "probabilities": {"NOT_APPLICABLE": 0.6, "PASS": 0.3, "FAIL": 0.1}, "confidence": 0.6})[0]   # 확신 < 0.7
    assert jv.screen_flag("J5-1.a", {"choice": "PASS", "probabilities": {"PASS": 0.99}, "confidence": 0.99}) == (True, "사실 확인은 Codex(웹 검색)가 봄")
    assert jv.screen_flag("J2-2.a", None) == (True, "Decisions 답 없음")


def test_all_item_questions_cover_44_and_turn_filters():
    fixed = json.loads(jv.JEV_V4.read_text(encoding="utf-8"))
    allq = jv.all_item_questions(fixed)
    assert len(allq) == 44 and all(set(q["criteria"]) >= {"PASS", "FAIL"} for q in allq.values())
    assert "J8-2.a" in allq and allq["J8-2.a"]["type"] == "choice" and "적용:" in allq["J8-2.a"]["instructions"]
    ids = list(allq)
    plain = {"case_id": "Q1_T1", "question": "q", "turn": 1}
    assert not any(i.startswith(("J6", "J7", "J8")) for i in jv.turn_item_ids(plain, 1, ids))
    with_files = {"case_id": "Q1_T2", "question": "q", "turn": 2, "attachments": [{"name": "a.txt", "type": "txt", "content": "x"}]}
    got = jv.turn_item_ids(with_files, 2, ids)
    assert any(i.startswith("J8") for i in got) and any(i.startswith("J6") for i in got) and not any(i.startswith("J7") for i in got)


class FakeDecisions:
    """모든 항목을 PASS 0.95 로, 지정한 항목만 지정한 답으로."""

    def __init__(self, special=None):
        self.special, self.calls = special or {}, []

    def judge(self, state, questions):
        self.calls.append((state["판정할_턴"], len(questions)))
        ans = {}
        for k in questions:
            item = k.split(":", 1)[1]
            ans[k] = self.special.get(item) or {"choice": "PASS", "probabilities": {"PASS": 0.95, "FAIL": 0.03, "NOT_APPLICABLE": 0.02}, "confidence": 0.95}
        return "OK", {"answers": ans, "usage": {"input_tokens": 1000}}, ""


class FakeCodex:
    """걸린 항목만 받아 지정한 판정을 낸다 — 받은 입력의 '판정할_항목' 을 기록."""

    model, reasoning = "fake-codex", "low"

    def __init__(self, verdicts=None):
        self.verdicts, self.inputs = verdicts or {}, []

    def judge(self, prompt, input_text, search, tag="", output_schema=None):
        doc = json.loads(input_text)
        self.inputs.append(doc)
        assert "판정할_항목" in doc and "선별 판정 (v5)" in prompt
        turns = []
        for n, items in doc["판정할_항목"].items():
            turns.append({"turn": int(n), "situations": [], "items": [
                {"id": i, "decision": self.verdicts.get(i, "PASS"), "question_quote": "", "answer_quote": "", "reason": "r", "quote_turn": None, "fact": None} for i in items]})
        return "OK", {"turns": turns, "conclusion": {"verdict": "PASS", "reason": ""}}, ""


def _run(tmp_path: Path, decisions, codex, codex_all=False):
    for sub in ("inputs", "codex", "jev", "decisions"):
        (tmp_path / sub).mkdir(exist_ok=True)
    fixed = json.loads(jv.JEV_V4.read_text(encoding="utf-8"))
    turns = [{"case_id": "Q1_T1", "question": "존댓말로 답해 주세요. 회의 시간을 알려 주세요.", "turn": 1}]
    answers = {"Q1_T1": "네, 회의는 10시입니다."}
    jv.say = lambda *a, **k: None
    return jv.screen_one(1, "gpt", turns, answers, {}, [], "PROMPT", fixed, codex, decisions, tmp_path, codex_all=codex_all)


def test_screen_one_sends_only_flagged_items_to_codex(tmp_path: Path):
    dec = FakeDecisions({"J2-2.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.8, "PASS": 0.2}, "confidence": 0.8},
                         "J4-2.a": {"choice": "PASS", "probabilities": {"PASS": 0.6, "FAIL": 0.4}, "confidence": 0.6}})
    codex = FakeCodex({"J2-2.a": "FAIL"})
    line = _run(tmp_path, dec, codex)
    assert line["mode"] == "screen" and line["version"] == jv.SCREEN_VERSION
    sent = codex.inputs[0]["판정할_항목"]["1"]
    assert "J2-2.a" in sent and "J4-2.a" in sent and "J1-4.a" not in sent          # 걸린 항목만, 확신한 PASS 는 안 보냄
    items = line["turn_results"]["1"]["items"]
    assert items["J2-2.a"]["decision"] == "FAIL" and items["J2-2.a"]["how"].startswith("Codex FAIL")
    assert items["J4-2.a"]["decision"] == "PASS" and items["J4-2.a"]["how"] == "Codex PASS · Decisions PASS" and items["J4-2.a"]["flagged"].startswith("FAIL 확률 0.40")
    assert items["J1-4.a"]["decision"] == "PASS" and items["J1-4.a"]["how"].startswith("Decisions") and items["J1-4.a"]["codex"] is None
    assert line["verdict"] == "FAIL" and line["screen"]["flagged"] == len(sent) and line["calls"]["decisions"]["input_tokens"] == 1000
    assert (tmp_path / "decisions" / "Q1_gpt.json").exists()


def test_screen_one_holds_when_codex_passes_but_decisions_strong_fail(tmp_path: Path):
    dec = FakeDecisions({"J2-2.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.9}, "confidence": 0.9}})
    line = _run(tmp_path, dec, FakeCodex({"J2-2.a": "PASS"}))
    item = line["turn_results"]["1"]["items"]["J2-2.a"]
    assert item["decision"] == "UNVERIFIED" and "심판 갈림" in item["how"] and line["verdict"] == "HOLD"


def test_screen_one_skips_codex_when_nothing_flagged(tmp_path: Path):
    codex = FakeCodex()
    dec = FakeDecisions({"J5-1.a": {"choice": "NOT_APPLICABLE", "probabilities": {"NOT_APPLICABLE": 0.97}, "confidence": 0.97}})
    line = _run(tmp_path, dec, codex)
    assert codex.inputs == [] and line["calls"]["codex"]["status"] == "SKIPPED" and line["verdict"] == "PASS"


def test_screen_one_codex_all_for_calibration(tmp_path: Path):
    codex = FakeCodex()
    line = _run(tmp_path, FakeDecisions(), codex, codex_all=True)
    assert line["screen"]["codex_all"] and line["screen"]["flagged"] == line["screen"]["items"] and codex.inputs
