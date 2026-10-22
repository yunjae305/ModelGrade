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
    assert dec == "PASS" and "강한 FAIL 0.85" in how and "갈림, Codex 따름" in how                 # 갈려도 Codex 확정 (사용자 10-21), 갈림은 기록
    dec, how = jv.combine_weighted(codex_pass, weak, "Decisions", 0.7)
    assert dec == "PASS" and "약한 FAIL 0.55" in how                                             # 약한 FAIL → Codex 따름
    assert jv.combine_weighted(codex_pass, strong, "Jev", 0.7, jev_trimmed=True)[0] == "PASS"    # 줄인 자료로 본 둘째 심판은 참고만
    assert jv.combine_weighted(None, strong, "Decisions", 0.7)[0] == "UNVERIFIED"


def test_screen_flag_rules():
    assert jv.screen_flag("J2-2.a", {"choice": "PASS", "probabilities": {"PASS": 0.95, "FAIL": 0.05}, "confidence": 0.95}) == (False, "")
    assert jv.screen_flag("J2-2.a", {"choice": "FAIL", "probabilities": {"FAIL": 0.6}, "confidence": 0.6})[0]
    assert jv.screen_flag("J2-2.a", {"choice": "PASS", "probabilities": {"PASS": 0.65, "FAIL": 0.35}, "confidence": 0.65})[0]   # FAIL 0.35 ≥ 0.3
    assert not jv.screen_flag("J2-2.a", {"choice": "NOT_APPLICABLE", "probabilities": {"NOT_APPLICABLE": 0.6, "PASS": 0.3, "FAIL": 0.1}, "confidence": 0.6})[0]   # 해당 없음 저확신은 안 보냄 (10-21)
    assert jv.screen_flag("J2-2.a", {"choice": "PASS", "probabilities": {"PASS": 0.6, "NOT_APPLICABLE": 0.3, "FAIL": 0.1}, "confidence": 0.6})[0]   # PASS 저확신은 보냄
    assert jv.combine_screen({"decision": "UNVERIFIED", "problems": []}, {"choice": "NOT_APPLICABLE", "probabilities": {"NOT_APPLICABLE": 0.6}}, True, item="J5-1.a")[0] == "NOT_APPLICABLE"
    assert jv.combine_screen({"decision": "UNVERIFIED", "problems": []}, {"choice": "PASS", "probabilities": {"PASS": 0.6}}, True, item="J5-1.a")[0] == "UNVERIFIED"
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

    def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
        doc = json.loads(input_text)
        self.inputs.append(doc)
        assert "판정할_항목" in doc and "선별 판정 (v5)" in prompt
        turns = []
        for n, items in doc["판정할_항목"].items():
            turns.append({"turn": int(n), "situations": [], "items": [
                {"id": i, "decision": self.verdicts.get(i, "PASS"), "question_quote": "", "answer_quote": "", "reason": "r", "quote_turn": None, "fact": None} for i in items]})
        return "OK", {"turns": turns, "conclusion": {"verdict": "PASS", "reason": ""}}, ""


def _run(tmp_path: Path, decisions, codex, codex_all=False, reuse_decisions=False):
    for sub in ("inputs", "codex", "jev", "decisions"):
        (tmp_path / sub).mkdir(exist_ok=True)
    fixed = json.loads(jv.JEV_V4.read_text(encoding="utf-8"))
    turns = [{"case_id": "Q1_T1", "question": "존댓말로 답해 주세요. 회의 시간을 알려 주세요.", "turn": 1}]
    answers = {"Q1_T1": "네, 회의는 10시입니다."}
    jv.say = lambda *a, **k: None
    return jv.screen_one(1, "gpt", turns, answers, {}, [], "PROMPT", fixed, codex, decisions, tmp_path, codex_all=codex_all, reuse_decisions=reuse_decisions)


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


def test_screen_one_follows_codex_when_decisions_strong_fail(tmp_path: Path):
    dec = FakeDecisions({"J2-2.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.9}, "confidence": 0.9}})
    line = _run(tmp_path, dec, FakeCodex({"J2-2.a": "PASS"}))
    item = line["turn_results"]["1"]["items"]["J2-2.a"]
    assert item["decision"] == "PASS" and "갈림, Codex 따름" in item["how"] and line["verdict"] == "PASS"   # 사용자 결정 10-21


def test_screen_one_skips_codex_when_nothing_flagged(tmp_path: Path):
    codex = FakeCodex()
    dec = FakeDecisions({"J5-1.a": {"choice": "NOT_APPLICABLE", "probabilities": {"NOT_APPLICABLE": 0.97}, "confidence": 0.97}})
    line = _run(tmp_path, dec, codex)
    assert codex.inputs == [] and line["calls"]["codex"]["status"] == "SKIPPED" and line["verdict"] == "PASS"


def test_screen_one_codex_all_for_calibration(tmp_path: Path):
    codex = FakeCodex()
    line = _run(tmp_path, FakeDecisions(), codex, codex_all=True)
    assert line["screen"]["codex_all"] and line["screen"]["flagged"] == line["screen"]["items"] and codex.inputs


def test_add_usage_keeps_every_token_field_and_drops_cost(tmp_path, monkeypatch):
    """판정 토큰 (10-22 미팅 7번): 심판이 준 사용량 칸을 전부 더하고(안쪽 칸은 '바깥.안쪽') 값 · 단가 칸은 뺀다 — 결과 보고서 표가 심판별로 읽는다."""
    total = {"input_tokens": 0, "output_tokens": 0}
    jv.add_usage(total, {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20, "reasoning_output_tokens": 15, "cost_usd": 0.4})
    jv.add_usage(total, {"input_tokens": 50, "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 3}})
    assert total == {"input_tokens": 150, "cached_input_tokens": 60, "output_tokens": 25, "reasoning_output_tokens": 15,
                     "output_tokens_details.reasoning_tokens": 3}
    from tools import build_result_summary as brs
    lines = [{"provider": "gpt", "status": "judged", "calls": {
        "decisions": {"first_judge": {"1": "jev"}, "usage": {"jev": {"input_tokens": 10, "output_tokens": 2}}},
        "codex": {"rounds": 2, **total}}},
        {"provider": "gpt", "status": "judged", "calls": {"decisions": {"first_judge": {"1": "decisions"}, "input_tokens": 7}}}]   # 옛 기록 — 1차 입력만
    monkeypatch.setattr(brs, "judged_lines", lambda D: lines)
    T = brs.judge_tokens({"order": ["gpt"]})
    assert (T["Jev"]["in"], T["Jev"]["out"], T["Jev"]["calls"]) == (10, 2, 1)
    assert (T["Codex"]["cached"], T["Codex"]["reason"], T["Codex"]["calls"]) == (60, 15, 2)
    assert T[brs.FIRST_SUM]["in"] == 7 and T[brs.FIRST_SUM]["seen"] == {"in"}   # 출력 · 캐시는 '기록 안 됨'


def test_reason_contradicting_decision_is_held_both_ways():
    # 10-22 기준 점검: 사유의 결론과 판정이 반대면(다시 물어도 남으면) 사유 모순 보류
    items = {"J3-1.a": {"decision": "FAIL", "reason": "두 시각이 같아 모순이 없습니다. J3-1은 PASS여야 합니다."},
             "J1-3.a": {"decision": "PASS", "reason": "부재를 밝히지 않아 실제로는 FAIL입니다."},
             "J1-4.a": {"decision": "PASS", "reason": "무관한 단락이 없어 FAIL 기준을 충족하지 않습니다."},
             "J1-4.b": {"decision": "FAIL", "reason": "같은 표를 두 번 반복했습니다."}}
    jv.reason_decision_guard(items)
    assert [items[k]["decision"] for k in ("J3-1.a", "J1-3.a", "J1-4.a", "J1-4.b")] == ["UNVERIFIED", "UNVERIFIED", "PASS", "FAIL"]
    assert items["J3-1.a"]["kind"] == items["J1-3.a"]["kind"] == "사유 모순"


def test_reason_conclusion_skips_negation_condition_and_other_items():
    # Codex 검토 10-22: 부정 · 양보 · 조건 · 다른 항목을 결론으로 읽던 것과, 공백 두 칸 · 'FAIL입니다' 를 놓치던 것
    c = jv.reason_contradicts
    assert not c("FAIL", "PASS로 판정할 수 없습니다. 요청한 파일이 빠졌습니다.", "J4-2.a")
    assert not c("FAIL", "모순은 없지만, 요청한 주의사항이 빠져 있습니다.", "J4-2.a")
    assert not c("FAIL", "모순은 없지만, 요청한 주의사항이 빠져 있습니다.", "J3-1.a")
    assert not c("PASS", "무관한 단락이 없어 FAIL  기준을 충족하지 않습니다.", "J1-4.a")
    assert c("PASS", "필수 안내가 빠졌으므로 FAIL입니다.", "J4-2.a")
    assert not c("PASS", "안내를 빠뜨렸다면 FAIL입니다. 여기서는 모두 있습니다.", "J4-2.a")
    assert not c("FAIL", "J1-1.a는 PASS입니다. 그러나 주의사항 요청을 빠뜨렸습니다.", "J4-2.a")
    assert c("FAIL", "말투 요청을 지켰습니다.", "J2-2.a", strong=False) and not c("FAIL", "말투 요청을 지켰습니다.", "J2-2.a")
    # Codex 2차 검토: 짧은 결론이 먼저 잡혀 뒤 부정을 놓치던 것 · '아닙니다' · 다른 항목이 주어가 아닌 말
    assert not c("FAIL", "이 항목은 PASS로 판정할 수 없습니다. 요청한 파일이 빠졌습니다.", "J4-2.a")
    assert not c("PASS", "이 항목은 FAIL이 아닙니다. 요구 사항을 모두 충족합니다.", "J4-2.a")
    assert c("PASS", "J1-1.a와 달리 이 항목은 FAIL입니다.", "J4-2.a")
    assert c("FAIL", "J1-4.b는 PASS입니다(반복 아님).", "J1-4.b")   # Codex 3차: 괄호 속 근거는 결론의 부정이 아님


def test_guards_read_every_entry_and_keep_contradiction_hold():
    # Codex 검토 10-22: 뒤 요소의 모순을 놓치던 것 · 사유 모순 보류를 fact_true_guard 가 PASS 로 되돌리던 것
    items = {"J4-2.a": {"decision": "PASS", "reason": "이유를 제시했습니다.", "entries": [
                 {"id": "J4-2.a#1", "decision": "PASS", "reason": "이유를 제시했습니다."},
                 {"id": "J4-2.a#2", "decision": "PASS", "reason": "예시를 빠뜨려 실제로는 FAIL입니다."}]},
             "J5-1.a": {"decision": "FAIL", "reason": "주장은 검색한 원문과 일치합니다. 판정은 PASS입니다.", "entries": [
                 {"id": "J5-1.a#1", "decision": "FAIL", "reason": "주장은 검색한 원문과 일치합니다. 판정은 PASS입니다.",
                  "fact": {"verdict": "FALSE", "sources": ["https://example.org/a"]}}]}}
    jv.apply_guards(items, {"턴": []}, 1)
    assert items["J4-2.a"]["decision"] == "UNVERIFIED" and "예시를" in items["J4-2.a"]["reason"]
    assert items["J5-1.a"]["decision"] == "UNVERIFIED" and items["J5-1.a"]["kind"] == "사유 모순"


def test_fact_na_reason_drops_only_that_claim():
    # Codex 검토 10-22: 한 주장의 '자료 안의 사실' 사유가 다른 외부 사실 FAIL 까지 지우던 것
    keep = {"J5-1.a": {"decision": "FAIL", "problems": [], "entries": [
        {"id": "J5-1.a#1", "decision": "NOT_APPLICABLE", "reason": "자료 내 사실이므로 J5-2에서 판단합니다."},
        {"id": "J5-1.a#2", "decision": "FAIL", "reason": "개관 연도가 다릅니다.", "fact": {"verdict": "FALSE", "sources": ["https://example.org/b"]}}]}}
    jv.apply_guards(keep, {"턴": []}, 1)
    assert keep["J5-1.a"]["decision"] == "FAIL"
    drop = {"J5-1.a": {"decision": "FAIL", "problems": [], "entries": [
        {"id": "J5-1.a#1", "decision": "FAIL", "reason": "입력 자료의 값이라 외부 사실이 아닙니다.", "fact": {"verdict": "FALSE", "sources": []}},
        {"id": "J5-1.a#2", "decision": "PASS", "reason": "맞습니다."}]}}
    jv.apply_guards(drop, {"턴": []}, 1)
    assert drop["J5-1.a"]["decision"] == "PASS" and "주장 1/2개" in drop["J5-1.a"]["how"]
    odd = {"J5-1.a": {"decision": "FAIL", "problems": [], "entries": [   # Codex 3차: 반박 출처까지 든 FAIL 은 사유 문구만으로 지우지 않고 사유 모순 보류
        {"id": "J5-1.a#1", "decision": "FAIL", "reason": "입력 자료의 값이라 외부 사실이 아닙니다.", "fact": {"verdict": "FALSE", "sources": ["x"]}},
        {"id": "J5-1.a#2", "decision": "PASS", "reason": "맞습니다."}]}}
    jv.apply_guards(odd, {"턴": []}, 1)
    assert odd["J5-1.a"]["decision"] == "UNVERIFIED" and odd["J5-1.a"]["kind"] == "사유 모순"


def test_overrides_run_after_condition_item_is_checked():
    # Codex 검토 10-22: 사유 모순으로 보류된 J1-2.a 가 J4 FAIL 을 지우지 않는다 · 진짜 거절이면 J1-1.a · J4 해당 없음
    held = {"J1-2.a": {"decision": "FAIL", "entries": [{"id": "J1-2.a", "decision": "FAIL", "reason": "결과물이 있으므로 PASS여야 합니다."}]},
            "J1-1.a": {"decision": "PASS", "entries": []},
            "J4-2.a": {"decision": "FAIL", "entries": [{"id": "J4-2.a", "decision": "FAIL", "reason": "주의사항 요청을 빠뜨렸습니다."}]}}
    jv.apply_guards(held, {"턴": []}, 1)
    assert (held["J1-2.a"]["decision"], held["J1-1.a"]["decision"], held["J4-2.a"]["decision"]) == ("UNVERIFIED", "PASS", "FAIL")
    real = {"J1-2.a": {"decision": "FAIL", "reason": "전문가에게 문의하라고만 했습니다.", "entries": []},
            "J1-1.a": {"decision": "PASS", "entries": []}, "J4-2.a": {"decision": "FAIL", "reason": "주의사항 요청을 빠뜨렸습니다.", "entries": []}}
    jv.apply_guards(real, {"턴": []}, 1)
    assert real["J1-1.a"]["decision"] == real["J4-2.a"]["decision"] == "NOT_APPLICABLE"


class ReaskCodex(FakeCodex):
    """첫 호출에서 J3-1.a 를 사유와 반대인 FAIL 로, 다시 물으면 PASS 로."""

    def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
        doc = json.loads(input_text)
        self.inputs.append(doc)
        first = len(self.inputs) == 1
        turns = [{"turn": int(n), "situations": [], "items": [
            {"id": i, "decision": "FAIL" if first and i == "J3-1.a" else "PASS", "question_quote": "", "answer_quote": "", "quote_turn": None, "fact": None,
             "reason": "두 시각이 같아 모순이 없습니다. J3-1은 PASS여야 합니다." if first and i == "J3-1.a" else "r"} for i in items]}
            for n, items in doc["판정할_항목"].items()]
        return "OK", {"turns": turns, "conclusion": {"verdict": "PASS", "reason": ""}}, ""


def test_screen_one_reasks_item_whose_reason_contradicts_decision(tmp_path: Path):
    # Codex 검토 10-22: 다시 묻기 후보가 entries 안의 사유를 읽지 않아 한 번도 돌지 않던 것
    dec = FakeDecisions({"J3-1.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.8, "PASS": 0.2}, "confidence": 0.8}})
    codex = ReaskCodex()
    line = _run(tmp_path, dec, codex)
    assert len(codex.inputs) == 2 and codex.inputs[1]["판정할_항목"] == {"1": ["J3-1.a"]} and "사유와 판정이 반대" in codex.inputs[1]["주의"]
    assert line["turn_results"]["1"]["items"]["J3-1.a"]["decision"] == "PASS"


def test_repair_asks_first_judge_only_for_items_missing_from_saved_answers(tmp_path: Path):
    # Codex 검토 10-22: --repair 가 저장된 답이 있는 턴을 통째로 건너뛰어 새로 넣은 J5-3.a 를 묻지 않던 것
    _run(tmp_path, FakeDecisions(), FakeCodex())
    saved = tmp_path / "decisions" / "Q1_gpt.json"
    data = json.loads(saved.read_text(encoding="utf-8"))
    del data["1"]["J5-3.a"]
    saved.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    dec = FakeDecisions()
    line = _run(tmp_path, dec, FakeCodex(), reuse_decisions=True)
    assert dec.calls == [([1], 1)] and "J5-3.a" in line["turn_results"]["1"]["items"]
    dc = line["calls"]["decisions"]   # Codex 2차 검토: 새 호출 · 사용량을 따로 기록, 결과 요약은 새 호출만 센다
    assert dc["reused"] and dc["fresh_judge"] == {"1": "decisions"} and dc["calls"] == 1 and dc["usage"]["decisions"]["input_tokens"] == 1000
    from tools import build_result_summary as brs
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(brs, "judged_lines", lambda D: [{"provider": "gpt", "calls": {"decisions": dc}},
                                               {"provider": "gpt", "calls": {"decisions": {"reused": True, "first_judge": {"1": "jev"}, "usage": {}}}}])
    try:
        T = brs.judge_tokens({"order": ["gpt"]})
        W = brs.judge_work({"order": ["gpt"]})
    finally:
        mp.undo()
    assert (T["Decisions"]["in"], T["Decisions"]["calls"]) == (1000, 1) and "Jev" not in T
    assert (W["gpt"]["dec"], W["gpt"]["jev"]) == (1, 0)   # Codex 3차: 호출 수 표도 새 호출만


def test_dropped_claim_does_not_release_quote_hold():
    # Codex 2차 검토: 뺀 주장(자료 안의 사실)의 '일치합니다'를 대표 사유로 골라 인용 오류 보류가 PASS 로 풀리던 것
    doc = {"턴": [{"턴": 1, "질문": "푸른자료실은 3층에 있습니다. 개관 안내를 써 주세요."}]}
    items = {"J5-1.a": {"decision": "UNVERIFIED", "problems": ["J5-1.a#2: answer_quote 가 답변 원문에 없음"], "entries": [
        {"id": "J5-1.a#1", "decision": "PASS", "reason": "자료 내용과 일치합니다.", "answer_quote": "푸른자료실은 3층에 있습니다"},
        {"id": "J5-1.a#2", "decision": "FAIL", "reason": "개관 연도가 다릅니다.", "answer_quote": "1999년 개관",
         "fact": {"verdict": "FALSE", "sources": ["https://example.org/c"]}}]}}
    jv.apply_guards(items, doc, 1)
    it = items["J5-1.a"]
    assert it["decision"] == "UNVERIFIED" and it["reason"] == "개관 연도가 다릅니다." and it["problems"]


def test_negated_material_reason_keeps_external_fail_and_each_fail_needs_its_own_source():
    # Codex 2차 검토: '자료 내 사실이 아니라 …'를 해당 없음으로 읽던 것 · 뺀 주장의 출처가 다른 주장의 FAIL 을 받치던 것
    neg = {"J5-1.a": {"decision": "FAIL", "problems": [], "entries": [
        {"id": "J5-1.a#1", "decision": "FAIL", "reason": "이 주장은 자료 내 사실이 아니라 별도로 덧붙인 외부 통계이며, 검색한 공식 수치와 다릅니다.",
         "fact": {"verdict": "FALSE", "sources": ["https://example.org/d"]}}]}}
    jv.apply_guards(neg, {"턴": []}, 1)
    assert neg["J5-1.a"]["decision"] == "FAIL"
    assert jv.not_external("입력 자료의 값이라 외부 사실이 아닙니다.") and not jv.not_external("자료 내 사실로 볼 수 없습니다.")
    assert not jv.not_external("자료 내 사실과는 별개인 외부 통계이며 공식 수치와 다릅니다.")   # Codex 3차: 대조
    assert jv.not_external("외부 사실이 아니므로 검색할 수 없습니다.") and not jv.not_external("외부 사실이 아니라고 단정할 수 없습니다.")
    split = {"J5-1.a": {"decision": "FAIL", "problems": [], "entries": [   # Codex 3차: 사유 검사 · 출처 검사를 서로 다른 주장이 통과시키지 않게
        {"id": "J5-1.a#1", "decision": "FAIL", "reason": "이 설명의 판정은 PASS입니다.", "fact": {"verdict": "FALSE", "sources": ["https://example.org/f"]}},
        {"id": "J5-1.a#2", "decision": "FAIL", "reason": "공전 주기가 틀렸습니다.", "fact": {"verdict": "FALSE", "sources": []}}]}}
    jv.apply_guards(split, {"턴": []}, 1)
    assert split["J5-1.a"]["decision"] == "UNVERIFIED" and split["J5-1.a"]["kind"] == "확인 못함"
    mixed = {"J5-1.a": {"decision": "FAIL", "problems": [], "entries": [   # Codex 4차: 요소의 사유 모순 보류를 다른 요소의 '맞다'로 덮지 않음
        {"id": "J5-1.a#1", "decision": "FAIL", "reason": "입력 자료의 값과 일치합니다. 외부 사실이 아닙니다.", "fact": {"verdict": "FALSE", "sources": ["x"]}},
        {"id": "J5-1.a#2", "decision": "FAIL", "reason": "기관의 설립 연도가 틀렸습니다.", "fact": {"verdict": "FALSE", "sources": []}}]}}
    jv.apply_guards(mixed, {"턴": []}, 1)
    assert mixed["J5-1.a"]["decision"] == "UNVERIFIED" and mixed["J5-1.a"]["reason"] == "기관의 설립 연도가 틀렸습니다."
    single = {"J5-1.a": {"decision": "FAIL", "problems": [], "entries": [   # 10-21 밤 그대로: 출처 없는 FAIL 하나가 '맞습니다'고만 하면 PASS
        {"id": "J5-1.a", "decision": "FAIL", "reason": "통상적인 과학 지식에 맞습니다.", "fact": {"verdict": "FALSE", "sources": []}}]}}
    jv.apply_guards(single, {"턴": []}, 1)
    assert single["J5-1.a"]["decision"] == "PASS"
    held = {"J5-1.a": {"decision": "UNVERIFIED", "problems": [], "entries": [   # Codex 5차: 보류 항목 안의 사유 모순 요소도 표시 — PASS 로 풀지 않음
        {"id": "J5-1.a#1", "decision": "PASS", "reason": "공전 주기가 틀렸으므로 실제로는 FAIL입니다."},
        {"id": "J5-1.a#2", "decision": "UNVERIFIED", "reason": "통상적인 과학 지식에 맞습니다."}]}}
    jv.apply_guards(held, {"턴": []}, 1)
    assert held["J5-1.a"]["decision"] == "UNVERIFIED" and held["J5-1.a"]["kind"] == "사유 모순"
    own = {"J5-1.a": {"decision": "FAIL", "problems": [], "entries": [
        {"id": "J5-1.a#1", "decision": "FAIL", "reason": "입력 자료의 값입니다.", "fact": {"verdict": "FALSE", "sources": ["https://example.org/e"]}},
        {"id": "J5-1.a#2", "decision": "FAIL", "reason": "일반 지식과 다릅니다.", "fact": {"verdict": "FALSE", "sources": []}}]}}
    jv.apply_guards(own, {"턴": []}, 1)
    assert own["J5-1.a"]["decision"] == "UNVERIFIED" and own["J5-1.a"]["kind"] == "확인 못함"
    unbacked = {"J5-1.a": {"decision": "FAIL", "problems": [], "entries": [   # 출처 없는 FAIL 이라도 사유가 반대면 사유 모순 보류(PASS 로 풀리지 않음)
        {"id": "J5-1.a#1", "decision": "FAIL", "reason": "주장은 일반 지식과 일치합니다. 판정은 PASS입니다.", "fact": {"verdict": "FALSE", "sources": []}}]}}
    jv.apply_guards(unbacked, {"턴": []}, 1)
    assert unbacked["J5-1.a"]["decision"] == "UNVERIFIED" and unbacked["J5-1.a"]["kind"] == "사유 모순"


class SiblingCodex(FakeCodex):
    """첫 호출에서 J4-2.a 를 두 요소로(#1 정상 FAIL · #2 사유와 반대인 FAIL), 다시 물으면 reask_id 하나만 PASS 로."""

    def __init__(self, reask_id="J4-2.a#2"):
        super().__init__()
        self.reask_id = reask_id

    def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
        doc = json.loads(input_text)
        self.inputs.append(doc)
        first = len(self.inputs) == 1

        def elems(i):
            if i != "J4-2.a":
                return [{"id": i, "decision": "PASS", "reason": "r"}]
            if first:
                return [{"id": "J4-2.a#1", "decision": "FAIL", "reason": "예시를 빠뜨렸습니다."},
                        {"id": "J4-2.a#2", "decision": "FAIL", "reason": "이유를 제시했으므로 PASS입니다."}]
            return [{"id": self.reask_id, "decision": "PASS", "reason": "이유를 제시했습니다."}]
        turns = [{"turn": int(n), "situations": [], "items": [dict(e, question_quote="", answer_quote="", quote_turn=None, fact=None) for i in items for e in elems(i)]}
                 for n, items in doc["판정할_항목"].items()]
        return "OK", {"turns": turns, "conclusion": {"verdict": "FAIL", "reason": ""}}, ""


def test_reask_of_one_element_keeps_sibling_fail(tmp_path: Path):
    # Codex 2차 검토: 사유 모순 요소만 다시 답이 와도 정상 FAIL 요소를 지우지 않는다
    dec = FakeDecisions({"J4-2.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.8, "PASS": 0.2}, "confidence": 0.8}})
    codex = SiblingCodex()
    item = _run(tmp_path, dec, codex)["turn_results"]["1"]["items"]["J4-2.a"]
    assert len(codex.inputs) == 2 and item["decision"] == "FAIL" and item["reason"] == "예시를 빠뜨렸습니다."
    assert [e["id"] for e in codex.inputs[1]["이전_판정"]["T1:J4-2.a"]] == ["J4-2.a#1", "J4-2.a#2"]   # 이전 요소 번호를 함께 준다
    assert codex.inputs[1].get("이전_판정_안내")
    base_only = SiblingCodex("J4-2.a")   # Codex 3차: 번호 없이 하나로 온 답은 대체로 받지 않는다(정상 FAIL 요소가 지워지지 않게)
    sub = tmp_path / "b"
    sub.mkdir()
    item2 = _run(sub, dec, base_only)["turn_results"]["1"]["items"]["J4-2.a"]
    assert item2["decision"] == "FAIL" and item2["reason"] == "예시를 빠뜨렸습니다."


class RenumberCodex(FakeCodex):
    """첫 호출: J4-2.a#1 FAIL(예시 누락 — 정상) · #2 PASS(이유 — 인용이 원문에 없음). 다시 물으면 번호를 바꿔 #1 PASS 하나만."""

    def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
        doc = json.loads(input_text)
        self.inputs.append(doc)
        first = len(self.inputs) == 1

        def elems(i):
            if i != "J4-2.a":
                return [{"id": i, "decision": "PASS", "reason": "r", "answer_quote": ""}]
            if first:
                return [{"id": "J4-2.a#1", "decision": "FAIL", "reason": "예시를 빠뜨렸습니다.", "answer_quote": ""},
                        {"id": "J4-2.a#2", "decision": "PASS", "reason": "이유를 제시했습니다.", "answer_quote": "답변에 없는 문장"}]
            return [{"id": "J4-2.a#1", "decision": "PASS", "reason": "이유를 제시했습니다.", "answer_quote": ""}]
        turns = [{"turn": int(n), "situations": [], "items": [dict(e, question_quote="", quote_turn=None, fact=None) for i in items for e in elems(i)]}
                 for n, items in doc["판정할_항목"].items()]
        return "OK", {"turns": turns, "conclusion": {"verdict": "FAIL", "reason": ""}}, ""


def test_partial_renumbered_reask_does_not_erase_fail(tmp_path: Path):
    # Codex 4차: 인용 오류로 다시 물었는데 번호를 바꾼 일부 답(#1 PASS 하나)이 와도 앞 답의 정상 FAIL 을 지우지 않는다
    dec = FakeDecisions({"J4-2.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.8, "PASS": 0.2}, "confidence": 0.8}})
    codex = RenumberCodex()
    item = _run(tmp_path, dec, codex)["turn_results"]["1"]["items"]["J4-2.a"]
    assert len(codex.inputs) == 2 and item["decision"] == "FAIL" and item["reason"] == "예시를 빠뜨렸습니다."


def test_misapplied_link_rule_is_reasked(tmp_path: Path):
    # 10-22 J1-4.b 재판정: 거절 턴에서 'J1-1.a FAIL → J4 해당 없음'을 J1-4.b 에 써서 해당 없음 — 규칙 대상을 짚어 한 번 다시 묻는다
    why = "J1-1.a가 FAIL이므로 상황표 2에 따라 J4 항목은 NOT_APPLICABLE입니다."
    assert jv.misapplied_rule("J1-4.b", why) and not jv.misapplied_rule("J4-2.a", why) and not jv.misapplied_rule("J1-4.b", "반복이 없습니다.")
    assert not jv.misapplied_rule("J1-3.b", "질문에 모순되는 조건이나 빠진 자료가 없어 J1-3이 적용되지 않습니다.")   # 조건 항목 · '적용되지 않'
    assert jv.misapplied_rule("J2-3.a", "J1-1.a가 FAIL이므로 J4 항목만 해당 없음 처리합니다.")
    # Codex 검토: 실제 표현 · 규칙마다 따로 · 부정한 언급
    assert jv.misapplied_rule("J2-3.a", "J1-2.a가 거절·회피 FAIL이므로 이 규칙은 판정하지 않습니다.")
    assert jv.misapplied_rule("J2-3.a", "J1-1.a는 FAIL이므로 해당 없음입니다.") and jv.misapplied_rule("J2-3.a", "J1-2.a가 아니오이므로 해당 없음입니다.")
    assert jv.misapplied_rule("J4-2.a", "J1-1.a가 FAIL은 아닙니다. J2-4.a가 FAIL이므로 이 항목도 해당 없음입니다.")
    assert jv.misapplied_rule("J1-2.a", "J1-1.a가 FAIL이므로 해당 없음입니다.") and not jv.misapplied_rule("J1-1.a", "J1-2.a가 FAIL이므로 해당 없음입니다.")
    assert jv.misapplied_rule("J1-2.a", "J1-1.a가 FAIL이므로 J4 항목과 함께 이 항목도 해당 없음입니다.")   # Codex 2차: 'J4 항목' 언급이 가리지 않게
    assert jv.misapplied_rule("J4-2.a", "J1-1.a가 FAIL은 아닙니다. J4 항목은 J2-4.a가 FAIL이므로 해당 없음입니다.")
    assert not jv.misapplied_rule("J1-1.a", "J1-2.a가 FAIL이므로 J1-1.a와 J4 항목은 해당 없음입니다.")

    class MisruleCodex(FakeCodex):
        def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
            doc = json.loads(input_text)
            self.inputs.append(doc)
            first = len(self.inputs) == 1
            turns = [{"turn": int(n), "situations": [], "items": [
                {"id": i, "decision": "NOT_APPLICABLE" if first and i == "J1-4.b" else "PASS", "reason": why if first and i == "J1-4.b" else "r",
                 "question_quote": "", "answer_quote": "", "quote_turn": None, "fact": None} for i in items]} for n, items in doc["판정할_항목"].items()]
            return "OK", {"turns": turns, "conclusion": {"verdict": "PASS", "reason": ""}}, ""

    dec = FakeDecisions({"J1-4.b": {"choice": "FAIL", "probabilities": {"FAIL": 0.8, "PASS": 0.2}, "confidence": 0.8}})
    codex = MisruleCodex()
    item = _run(tmp_path, dec, codex)["turn_results"]["1"]["items"]["J1-4.b"]
    assert len(codex.inputs) == 2 and "연결 규칙" in codex.inputs[1]["주의"] and item["decision"] == "PASS"


def test_misrule_after_other_reask_is_still_reasked(tmp_path: Path, monkeypatch):
    # Codex 검토: 다른 이유(FAIL 다시 묻기)로 이미 다시 물은 항목이 새로 연결 규칙을 잘못 쓰면 규칙 안내와 함께 한 번 더 묻는다
    monkeypatch.setattr(jv, "REASK_FAIL_ITEMS", {"J1-4.b"})
    why = "J1-1.a가 FAIL이므로 상황표 2에 따라 J4 항목은 NOT_APPLICABLE입니다."

    class ThreeCodex(FakeCodex):
        def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
            doc = json.loads(input_text)
            self.inputs.append(doc)
            k = len(self.inputs)
            dec, why_i = {1: ("FAIL", "같은 표를 반복했습니다."), 2: ("NOT_APPLICABLE", why)}.get(k, ("PASS", "반복이 없습니다."))
            turns = [{"turn": int(n), "situations": [], "items": [
                {"id": i, "decision": dec if i == "J1-4.b" else "PASS", "reason": why_i if i == "J1-4.b" else "r",
                 "question_quote": "", "answer_quote": "", "quote_turn": None, "fact": None} for i in items]} for n, items in doc["판정할_항목"].items()]
            return "OK", {"turns": turns, "conclusion": {"verdict": "PASS", "reason": ""}}, ""

    dec = FakeDecisions({"J1-4.b": {"choice": "FAIL", "probabilities": {"FAIL": 0.8, "PASS": 0.2}, "confidence": 0.8}})
    codex = ThreeCodex()
    item = _run(tmp_path, dec, codex)["turn_results"]["1"]["items"]["J1-4.b"]
    assert len(codex.inputs) == 3 and "연결 규칙" in codex.inputs[2]["주의"] and item["decision"] == "PASS"


def test_misrule_reask_does_not_use_up_the_fail_reask(tmp_path: Path, monkeypatch):
    # Codex 2차: 연결 규칙 오적용으로 다시 물은 뒤 FAIL 이 나오면 정의를 짚는 FAIL 다시 묻기도 한다(두 이력 분리)
    monkeypatch.setattr(jv, "REASK_FAIL_ITEMS", {"J1-4.b"})
    why = "J1-1.a가 FAIL이므로 상황표 2에 따라 J4 항목은 NOT_APPLICABLE입니다."

    class ThreeCodex(FakeCodex):
        def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
            doc = json.loads(input_text)
            self.inputs.append(doc)
            dec, why_i = {1: ("NOT_APPLICABLE", why), 2: ("FAIL", "표에 적힌 가격을 설명 문단에서 반복했습니다.")}.get(len(self.inputs), ("PASS", "반복이 없습니다."))
            turns = [{"turn": int(n), "situations": [], "items": [
                {"id": i, "decision": dec if i == "J1-4.b" else "PASS", "reason": why_i if i == "J1-4.b" else "r",
                 "question_quote": "", "answer_quote": "", "quote_turn": None, "fact": None} for i in items]} for n, items in doc["판정할_항목"].items()]
            return "OK", {"turns": turns, "conclusion": {"verdict": "PASS", "reason": ""}}, ""

    dec = FakeDecisions({"J1-4.b": {"choice": "FAIL", "probabilities": {"FAIL": 0.8, "PASS": 0.2}, "confidence": 0.8}})
    codex = ThreeCodex()
    item = _run(tmp_path, dec, codex)["turn_results"]["1"]["items"]["J1-4.b"]
    assert len(codex.inputs) == 3 and jv.MISRULE_NOTE in codex.inputs[1]["주의"] and jv.REASK_NOTE in codex.inputs[2]["주의"] and item["decision"] == "PASS"
