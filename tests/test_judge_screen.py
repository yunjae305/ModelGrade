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


def _run(tmp_path: Path, decisions, codex, codex_all=False, reuse_decisions=False, code_measured=None):
    for sub in ("inputs", "codex", "jev", "decisions"):
        (tmp_path / sub).mkdir(exist_ok=True)
    fixed = json.loads(jv.JEV_V4.read_text(encoding="utf-8"))
    turns = [{"case_id": "Q1_T1", "question": "존댓말로 답해 주세요. 회의 시간을 알려 주세요.", "turn": 1}]
    answers = {"Q1_T1": "네, 회의는 10시입니다."}
    jv.say = lambda *a, **k: None
    return jv.screen_one(1, "gpt", turns, answers, {}, [], "PROMPT", fixed, codex, decisions, tmp_path, codex_all=codex_all, reuse_decisions=reuse_decisions, code_measured=code_measured)


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
    assert not jv.misapplied_rule("J1-4.b", "J1-2.a가 거절·회피 FAIL이므로 이 규칙은 판정하지 않습니다.")   # 10-25 밤: 거절 턴은 말투 빼고 전부 대상(REFUSAL_NA)
    assert jv.misapplied_rule("J2-2.a", "J1-2.a가 거절·회피 FAIL이므로 이 규칙은 판정하지 않습니다.")       # 말투는 거절 문장 자체를 본다
    assert not jv.misapplied_rule("J2-3.a", "J1-2.a가 거절·회피 FAIL이므로 이 규칙은 판정하지 않습니다.")
    assert jv.misapplied_rule("J2-3.a", "J1-1.a는 FAIL이므로 해당 없음입니다.") and jv.misapplied_rule("J6-1.a", "J1-2.a가 아니오이므로 해당 없음입니다.")
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


def test_turn_plan_gives_per_turn_targets_and_checklist(tmp_path: Path, monkeypatch):
    # 10-22 ③: 멀티턴은 턴마다 목표 기준 · 확인 목록 — 목록의 질문 원문 지문이 질문 세트와 다르면 그 턴은 쓰지 않는다
    set_dir = tmp_path / "set"
    (set_dir / "multiturn_v3").mkdir(parents=True)
    groups = [{"id": 1, "turns": [{"turn": 1, "question": "[붙여 넣은 자료]\n…\n[자료 끝]\n\n존댓말로 답해 주세요. 회의 시간을 알려 주세요."}]},
              {"id": 2, "turns": [{"turn": 1, "question": "다른 질문"}]}]
    plan = {"questions": {"1": {"turns": [{"turn": 1, "question_sha": jv.turn_sha([groups[0]["turns"][0]["question"]]), "situation": "1턴 조건 정리",
                                            "checks": ["회의는 10시"], "keep": [], "decided": ["10시와 10시 정각을 구별하면 맞음"],
                                            "not_applicable": {"J4-2.a": "흐름상 해당 없음(시험용)"}}]},
                          "2": {"turns": [{"turn": 1, "question_sha": jv.turn_sha(["질문을 다시 만들기 전"]), "target_items": ["J1-1.a"]}]}}}
    (set_dir / "multiturn_v3" / "turn_checklists.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    got, stale = jv.load_turn_plan(set_dir, groups)
    assert list(got) == [(1, 1)] and stale == ["Q2 T1"]
    monkeypatch.setattr(jv, "TURN_PLAN", got)
    monkeypatch.setattr(jv, "QUESTION_TARGETS", {1: {"J1-1.a", "J1-2.a", "J1-4.a", "J2-2.a", "J4-2.a"}})
    assert jv.targets_for(1, 1) == {"J1-1.a", "J1-2.a", "J1-4.a", "J2-2.a"} and jv.targets_for(2, 1) is None   # 질문 목표 − 목록의 not_applicable
    jv.QUESTION_TARGETS[1].add("J2-1.a")
    assert "J2-1.a" in jv.targets_for(1, 1)   # 질문 목표가 바뀌면 판정 때 바로 반영 (Codex 검토 10-22)
    jv.QUESTION_TARGETS[1].discard("J2-1.a")
    doc = jv.question_json(1, [{"case_id": "Q1_T1", "question": groups[0]["turns"][0]["question"], "turn": 1}], {"Q1_T1": "네, 10시입니다."}, {})
    assert doc["턴"][0]["확인_목록"] == {"상황": "1턴 조건 정리", "확인할_것": ["회의는 10시"], "이어지는_조건": [],
                                     "정한_판정": ["10시와 10시 정각을 구별하면 맞음"]} and list(doc["턴"][0])[-1] == "답변"   # 사용자가 정한 판정 기준은 따로 (10-22)
    dec = FakeDecisions()
    _run(tmp_path, dec, FakeCodex())
    assert dec.calls == [([1], 5)]   # 그 턴의 목표 4개 + 늘 보는 J5-3.a


def test_repair_rejudges_turn_when_input_changed_and_drops_old_items(tmp_path: Path, monkeypatch):
    # Codex 검토(③): --repair 는 1차 심판 입력 지문이 같으면 저장된 답을 쓰고(지금 목표 밖 옛 항목은 뺌), 확인 목록이 바뀌면 그 턴을 통째로 다시 묻는다
    q1 = "존댓말로 답해 주세요. 회의 시간을 알려 주세요."
    plan = {(1, 1): {"turn": 1, "question_sha": jv.turn_sha([q1]), "situation": "s", "checks": ["회의는 10시"], "keep": []}}
    monkeypatch.setattr(jv, "TURN_PLAN", plan)
    monkeypatch.setattr(jv, "QUESTION_TARGETS", {1: {"J1-4.a", "J2-2.a"}})
    _run(tmp_path, FakeDecisions(), FakeCodex())
    saved = tmp_path / "decisions" / "Q1_gpt.json"
    data = json.loads(saved.read_text(encoding="utf-8"))
    data["1"]["J4-2.a"] = {"choice": "FAIL", "probabilities": {"FAIL": 0.9, "PASS": 0.1}, "confidence": 0.9}   # 지금 목표 밖의 옛 항목
    data["2"] = {"J1-4.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.9, "PASS": 0.1}, "confidence": 0.9}}   # 대화에서 없어진 턴
    saved.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    same = FakeDecisions()
    line = _run(tmp_path, same, FakeCodex(), reuse_decisions=True)
    assert same.calls == [] and "J4-2.a" not in line["turn_results"]["1"]["items"] and list(line["turn_results"]) == ["1"] and line["verdict"] == "PASS"
    plan[(1, 1)]["checks"] = ["회의는 11시"]   # 확인 목록이 바뀜 → 그 턴은 저장된 답을 버리고 다시
    changed = FakeDecisions()
    _run(tmp_path, changed, FakeCodex(), reuse_decisions=True)
    assert changed.calls == [([1], 3)]
    import copy   # 항목 판정 정의만 바뀌면 그 항목만 다시 (Codex 검토 10-22)
    fixed = copy.deepcopy(json.loads(jv.JEV_V4.read_text(encoding="utf-8")))
    fixed["items"]["J2-2.a"]["instructions"] += " (정의 고침)"
    redef = FakeDecisions()
    jv.screen_one(1, "gpt", [{"case_id": "Q1_T1", "question": q1, "turn": 1}], {"Q1_T1": "네, 회의는 10시입니다."}, {}, [], "PROMPT", fixed,
                  FakeCodex(), redef, tmp_path, reuse_decisions=True)
    assert redef.calls == [([1], 1)]


def test_saved_codex_output_is_reused_only_for_the_same_input(tmp_path: Path, monkeypatch):
    # Codex 검토(③): 저장된 Codex 출력은 질문 · 답 · 확인 목록 지문이 같을 때만 다시 쓴다
    for sub in ("inputs", "codex", "jev", "decisions"):
        (tmp_path / sub).mkdir(exist_ok=True)
    q1 = "존댓말로 답해 주세요. 회의 시간을 알려 주세요."
    plan = {(1, 1): {"turn": 1, "question_sha": jv.turn_sha([q1]), "situation": "s", "checks": ["회의는 10시"], "keep": []}}
    monkeypatch.setattr(jv, "TURN_PLAN", plan)
    monkeypatch.setattr(jv, "QUESTION_TARGETS", {1: {"J1-4.a", "J2-2.a"}})
    jv.say = lambda *a, **k: None
    fixed = json.loads(jv.JEV_V4.read_text(encoding="utf-8"))
    turns = [{"case_id": "Q1_T1", "question": q1, "turn": 1}]
    dec = lambda: FakeDecisions({"J2-2.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.8, "PASS": 0.2}, "confidence": 0.8}})
    run = lambda codex, prompt="PROMPT": jv.screen_one(1, "gpt", turns, {"Q1_T1": "네, 10시입니다."}, {}, [], prompt, fixed, codex, dec(), tmp_path, reuse_codex=True)
    first = FakeCodex({"J2-2.a": "FAIL"})
    run(first)
    assert len(first.inputs) == 1 and (tmp_path / "codex" / "Q1_gpt.json.fp").exists()
    again = FakeCodex({"J2-2.a": "FAIL"})
    run(again)
    assert again.inputs == []   # 입력이 같으면 저장된 출력을 다시 씀
    newp = FakeCodex({"J2-2.a": "FAIL"})
    run(newp, "PROMPT v2")
    assert len(newp.inputs) == 1   # 판정 프롬프트가 바뀌면 다시 묻는다 (Codex 검토 10-22)
    saved_out = (tmp_path / "codex" / "Q1_gpt.json").read_text(encoding="utf-8")
    line = run(None, "PROMPT v2")   # Codex 없이 복구 — 확인 목록은 같고 프롬프트도 같으면 저장된 출력을 쓴다
    assert line["calls"]["codex"]["status"] == "OK"
    plan[(1, 1)]["checks"] = ["회의는 11시"]
    line = run(None, "PROMPT v2")   # 입력이 바뀌었는데 Codex 가 없으면 오류로 남기고 저장된 출력은 지우지 않는다
    assert line["calls"]["codex"]["status"] == "ERROR" and (tmp_path / "codex" / "Q1_gpt.json").read_text(encoding="utf-8") == saved_out
    changed = FakeCodex({"J2-2.a": "FAIL"})
    run(changed, "PROMPT v2")
    assert len(changed.inputs) == 1   # 확인 목록이 바뀌면 다시 묻는다


def test_repair_fingerprints_definitions_attachments_and_empty_targets(tmp_path: Path, monkeypatch):
    # Codex 검토(③ 3차): 정의 지문이 없는 새 방식 저장은 다시 · 옛 저장은 믿되 지문을 붙이지 않음 · 첨부 원본이 바뀌면 다시 · 빈 목표는 J5-3.a 만
    q1 = "존댓말로 답해 주세요. 회의 시간을 알려 주세요."
    monkeypatch.setattr(jv, "TURN_PLAN", {})
    monkeypatch.setattr(jv, "QUESTION_TARGETS", {1: {"J1-4.a", "J2-2.a"}})
    _run(tmp_path, FakeDecisions(), FakeCodex())
    saved = tmp_path / "decisions" / "Q1_gpt.json"
    data = json.loads(saved.read_text(encoding="utf-8"))
    no_item = {k: v for k, v in data.items() if k != "_item_fp"}
    saved.write_text(json.dumps(no_item, ensure_ascii=False), encoding="utf-8")
    d1 = FakeDecisions()
    _run(tmp_path, d1, FakeCodex(), reuse_decisions=True)
    assert d1.calls == [([1], 3)]   # 입력 지문은 있는데 정의 지문이 없음 → 모두 다시
    old = {k: v for k, v in json.loads(saved.read_text(encoding="utf-8")).items() if k not in ("_item_fp", "_input_fp")}
    saved.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    d2 = FakeDecisions()
    _run(tmp_path, d2, FakeCodex(), reuse_decisions=True)
    assert d2.calls == [] and set(json.loads(saved.read_text(encoding="utf-8"))["_item_fp"]["1"].values()) == {jv.TRUSTED_OLD}   # 믿되 확인 지문은 안 붙임
    d2b = FakeDecisions()
    _run(tmp_path, d2b, FakeCodex(), reuse_decisions=True)
    assert d2b.calls == []   # 다음 복구에서도 같은 정책(믿음) — 전부 다시 묻지 않는다 (Codex 검토 ③ 4차)
    saved.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(jv, "REPAIR_TRUST_OLD", False)
    d3 = FakeDecisions()
    _run(tmp_path, d3, FakeCodex(), reuse_decisions=True)
    assert d3.calls == [([1], 3)]   # JUDGE_REPAIR_TRUST_OLD=0 이면 옛 저장을 다시 판정
    data = json.loads(saved.read_text(encoding="utf-8"))
    data["1"]["J2-2.a"] = None   # 답이 빠진 항목
    data["_item_fp"]["1"]["J2-2.a"] = jv.doc_fp(jv.all_item_questions(json.loads(jv.JEV_V4.read_text(encoding="utf-8")))["J2-2.a"])
    saved.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    d4 = FakeDecisions()
    _run(tmp_path, d4, FakeCodex(), reuse_decisions=True)
    assert d4.calls == [([1], 1)]   # 정의 지문이 같아도 답이 유효하지 않으면 다시 (Codex 검토 ③ 4차)
    files = tmp_path / "files"
    files.mkdir()
    (files / "a.txt").write_text("10", encoding="utf-8")
    monkeypatch.setattr(jv, "FILES_DIR", files)
    fixed = json.loads(jv.JEV_V4.read_text(encoding="utf-8"))
    turns = [{"case_id": "Q1_T1", "question": q1, "turn": 1, "attachments": [{"name": "a.txt", "path": "a.txt", "type": "txt", "content": "10"}]}]
    att = lambda dec: jv.screen_one(1, "gpt", turns, {"Q1_T1": "네, 10시입니다."}, {}, [], "PROMPT", fixed, FakeCodex(), dec, tmp_path, reuse_decisions=True)
    att(FakeDecisions())
    same = FakeDecisions()
    att(same)
    assert same.calls == []
    (files / "a.txt").write_text("11", encoding="utf-8")   # 첨부 원본만 바뀜(옮겨 적은 내용은 그대로)
    changed = FakeDecisions()
    att(changed)
    assert changed.calls == [([1], 3)]
    monkeypatch.setattr(jv, "TURN_PLAN", {(1, 1): {"turn": 1, "not_applicable": {"J1-4.a": "x", "J2-2.a": "x"}}})
    empty = FakeDecisions()
    (tmp_path / "e").mkdir()
    _run(tmp_path / "e", empty, FakeCodex())
    assert empty.calls == [([1], 1)]   # 빈 목표 → 늘 보는 J5-3.a 만


def test_codex_strict_mode_and_attach_pages_change_the_fingerprint(tmp_path: Path, monkeypatch):
    # Codex 검토(③ 4차): JUDGE_REPAIR_TRUST_OLD=0 이면 지문 없는 Codex 옛 출력도 다시 묻고, 믿고 쓴 출력에는 지문을 붙이지 않음 · 쪽 이미지 설정이 지문에 듦
    for sub in ("inputs", "codex", "jev", "decisions"):
        (tmp_path / sub).mkdir(exist_ok=True)
    q1 = "존댓말로 답해 주세요. 회의 시간을 알려 주세요."
    monkeypatch.setattr(jv, "TURN_PLAN", {})
    monkeypatch.setattr(jv, "QUESTION_TARGETS", {1: {"J1-4.a", "J2-2.a"}})
    jv.say = lambda *a, **k: None
    fixed = json.loads(jv.JEV_V4.read_text(encoding="utf-8"))
    turns = [{"case_id": "Q1_T1", "question": q1, "turn": 1}]
    dec = lambda: FakeDecisions({"J2-2.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.8, "PASS": 0.2}, "confidence": 0.8}})
    run = lambda codex: jv.screen_one(1, "gpt", turns, {"Q1_T1": "네, 10시입니다."}, {}, [], "PROMPT", fixed, codex, dec(), tmp_path, reuse_codex=True)
    run(FakeCodex({"J2-2.a": "FAIL"}))
    fp = tmp_path / "codex" / "Q1_gpt.json.fp"
    fp.unlink()   # 지문 없는 옛 저장으로
    trusted = FakeCodex({"J2-2.a": "FAIL"})
    run(trusted)
    assert trusted.inputs == [] and not fp.exists()   # 믿고 쓰되 지문은 붙이지 않음
    monkeypatch.setattr(jv, "REPAIR_TRUST_OLD", False)
    strict = FakeCodex({"J2-2.a": "FAIL"})
    run(strict)
    assert len(strict.inputs) == 1 and fp.exists()
    files = tmp_path / "files"
    files.mkdir()
    (files / "a.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(jv, "FILES_DIR", files)
    att = [{"case_id": "Q1_T1", "question": q1, "turn": 1, "attachments": [{"name": "a.pdf", "path": "a.pdf", "type": "pdf", "content": "(내용)"}]}]
    monkeypatch.setenv("CODEX_ATTACH_PAGES", "0")
    off = jv.files_fp(att)
    monkeypatch.setenv("CODEX_ATTACH_PAGES", "1")
    assert off != jv.files_fp(att) and jv.files_fp(turns) == {}


def test_keep_first_reuses_saved_first_judge_without_fingerprint(tmp_path: Path, monkeypatch):
    # Codex 검토(③ 5차): 1차 심판 없이 2차만 바꿔 비교하는 도구는 저장된 1차 판정을 그대로 — 확인 목록이 붙은 문맥에서도 지우지 않는다
    for sub in ("inputs", "codex", "jev", "decisions"):
        (tmp_path / sub).mkdir(exist_ok=True)
    q1 = "존댓말로 답해 주세요. 회의 시간을 알려 주세요."
    monkeypatch.setattr(jv, "TURN_PLAN", {(1, 1): {"turn": 1, "situation": "s", "checks": ["회의는 10시"], "keep": []}})
    monkeypatch.setattr(jv, "QUESTION_TARGETS", {1: {"J1-4.a", "J2-2.a"}})
    jv.say = lambda *a, **k: None
    (tmp_path / "decisions" / "Q1_gpt.json").write_text(json.dumps(   # 지문 없는 옛 저장, 그 항목만 남긴 것 (judge_item_rerun 이 만드는 모양)
        {"1": {"J2-2.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.9, "PASS": 0.1}, "confidence": 0.9}}, "_first_judge": {"1": "decisions"}},
        ensure_ascii=False), encoding="utf-8")
    fixed = json.loads(jv.JEV_V4.read_text(encoding="utf-8"))
    codex = FakeCodex({"J2-2.a": "FAIL"})
    line = jv.screen_one(1, "gpt", [{"case_id": "Q1_T1", "question": q1, "turn": 1}], {"Q1_T1": "네, 10시입니다."}, {}, [], "PROMPT", fixed,
                         codex, None, tmp_path, reuse_codex=True, reuse_decisions=True, keep_first=True)
    item = line["turn_results"]["1"]["items"]["J2-2.a"]
    assert codex.inputs[0]["판정할_항목"] == {"1": ["J2-2.a"]} and item["jev"] == "FAIL" and item["decisions_prob"]["FAIL"] == 0.9


def test_question_determined_includes_j4():
    assert {"J4-1.a", "J4-1.b", "J4-2.a", "J4-3.a", "J4-4.a"} <= jv.QUESTION_DETERMINED   # 10-22 밤: 빠져 있던 J4


def test_data_provider_footer_is_tool_attribution_not_fabricated_source():
    # 10-22 밤: AIONA 답 끝 '데이터 제공: MET Norway …' 줄을 꾸민 출처로 FAIL 하던 것 — 그 주장만 해당 없음, 진짜 꾸민 출처는 그대로
    only = {"J5-3.a": {"decision": "FAIL", "problems": [], "entries": [
        {"id": "J5-3.a", "decision": "FAIL", "reason": "질문 자료에 없는 외부 제공자를 출처로 덧붙였습니다.", "answer_quote": "데이터 제공: MET Norway (Weather data from MET Norway (CC BY 4.0))"}]}}
    jv.apply_guards(only, {"턴": []}, 1)
    assert only["J5-3.a"]["decision"] == "NOT_APPLICABLE"
    mixed = {"J5-3.a": {"decision": "FAIL", "problems": [], "entries": [
        {"id": "J5-3.a#1", "decision": "FAIL", "reason": "데이터 제공 문구를 붙였지만 입력 자료에 그런 출처가 없습니다.", "answer_quote": "데이터 제공: 한국천문연구원 (공공데이터포털 특일 정보(공휴일))"},
        {"id": "J5-3.a#2", "decision": "FAIL", "reason": "존재하지 않는 '서울시 야간개방 조례 제12조'를 근거로 들었습니다.", "answer_quote": "조례 제12조"}]}}
    jv.apply_guards(mixed, {"턴": []}, 1)
    assert mixed["J5-3.a"]["decision"] == "FAIL" and "조례" in mixed["J5-3.a"]["reason"]
    both = {"J5-3.a": {"decision": "FAIL", "problems": [], "entries": [   # Codex 검토: 가짜 조례와 데이터 제공 줄을 한 인용에 — 가짜 출처 FAIL 그대로
        {"id": "J5-3.a", "decision": "FAIL", "reason": "존재하지 않는 조례를 근거로 들었습니다.", "answer_quote": "가짜 조례 제12조에 따라…\n데이터 제공: MET Norway"}]}}
    jv.apply_guards(both, {"턴": []}, 1)
    assert both["J5-3.a"]["decision"] == "FAIL"
    first = {"J5-3.a": {"decision": "FAIL", "problems": [], "entries": [   # Codex 2차: 데이터 제공 줄이 먼저 오는 두 줄 인용
        {"id": "J5-3.a", "decision": "FAIL", "reason": "존재하지 않는 조례를 근거로 들었습니다.", "answer_quote": "데이터 제공: MET Norway\n서울시 야간개방 조례 제12조가 근거입니다."}]}}
    jv.apply_guards(first, {"턴": []}, 1)
    fake = {"J5-3.a": {"decision": "FAIL", "problems": [], "entries": [   # 모르는 제공처는 지어낸 것일 수 있어 그대로
        {"id": "J5-3.a", "decision": "FAIL", "reason": "존재하지 않는 기관을 데이터 제공처로 적었습니다.", "answer_quote": "데이터 제공: 달빛국제기상연구원"}]}}
    jv.apply_guards(fake, {"턴": []}, 1)
    assert first["J5-3.a"]["decision"] == "FAIL" and fake["J5-3.a"]["decision"] == "FAIL"
    half = {"J5-3.a": {"decision": "FAIL", "problems": [], "entries": [   # Codex 3차: 아는 제공처 뒤에 모르는 것이 붙음 · 인용은 footer 인데 사유는 다른 가짜 출처
        {"id": "J5-3.a", "decision": "FAIL", "reason": "존재하지 않는 기관을 적었습니다.", "answer_quote": "데이터 제공: MET Norway (Weather data from MET Norway (CC BY 4.0)) · 달빛국제기상연구원"}]}}
    paper = {"J5-3.a": {"decision": "FAIL", "problems": [], "entries": [
        {"id": "J5-3.a", "decision": "FAIL", "reason": "정상 데이터 제공 표시와 별도로 가짜 논문 '야간 개방 효과 연구(2024)'를 인용했습니다.", "answer_quote": "데이터 제공: MET Norway (Weather data from MET Norway (CC BY 4.0))"}]}}
    for x in (half, paper):
        jv.apply_guards(x, {"턴": []}, 1)
    assert half["J5-3.a"]["decision"] == "FAIL" and paper["J5-3.a"]["decision"] == "FAIL"
    two_ok = {"J5-3.a": {"decision": "FAIL", "problems": [], "entries": [   # 서비스가 실제로 내는 두 제공처 묶음은 해당 없음
        {"id": "J5-3.a", "decision": "FAIL", "reason": "질문 자료에 없는 출처를 덧붙였습니다.",
         "answer_quote": "데이터 제공: 한국천문연구원 (공공데이터포털 특일 정보(공휴일)) · 한국관광공사 (TourAPI 4.0 관광정보)"}]}}
    jv.apply_guards(two_ok, {"턴": []}, 1)
    assert two_ok["J5-3.a"]["decision"] == "NOT_APPLICABLE"


def test_measured_families_skips_none_and_marks_fail():
    case = {"results": [{"check_id": "2-1.charset", "verdict": "PASS", "counted": True, "title": "허용 문자 (한글만)"},
                        {"check_id": "2-1.charset", "verdict": "FAIL", "counted": True, "title": "허용 문자 (한글만)"},
                        {"check_id": "1-3.internal_link:none", "verdict": "PASS", "counted": True, "title": "내부 링크 · 검사 대상 없음"},
                        {"check_id": "2-1.count:bullet_count", "verdict": "PASS", "counted": False, "title": "불릿 개수"}]}
    assert jv.measured_families(case) == {"2-1.charset": ("FAIL", "허용 문자 (한글만)")}
    two = {"results": [{"check_id": "2-1.literal:a", "verdict": "PASS", "counted": True, "title": "금지 문자열 ‘무료’"},
                       {"check_id": "2-1.literal:b", "verdict": "FAIL", "counted": True, "title": "필수 문자열 ‘접수’"}]}
    assert jv.measured_families(two) == {"2-1.literal": ("FAIL", "금지 문자열 ‘무료’ · 필수 문자열 ‘접수’")}   # 조건 제목은 모두 남긴다
    from code_eval import engine
    from code_eval.config import load_aiona_config
    real = engine.evaluate_case({"question": "공백 포함 300자 이내로 답해 주세요. 제목은 10자 이내로 하세요.", "answer": "# 짧은 제목\n짧은 답입니다."}, load_aiona_config())
    got = jv.measured_families(real)["2-1.char_count"]   # 실제 엔진의 required 는 객체 — 잰 요구값 · 범위가 보이게 (Codex 3차)
    assert got[0] == "PASS" and "300자 이내(공백 포함)" in got[1] and "코드 펜스 밖" in got[1]
    real2 = engine.evaluate_case({"question": "본문은 불릿으로 정확히 3개만 작성하고 '확인'이라는 단어를 꼭 넣어 주세요.", "answer": "- 하나" + chr(10) + "- 둘" + chr(10) + "- 셋" + chr(10) + "확인했습니다."}, load_aiona_config())
    m2 = jv.measured_families(real2)   # Codex 4차: 개수 조건은 '개' 단위, 문구 조건은 제목에 이미 있어 중복 없이
    assert "3개 정확히" in m2["2-1.count"][1] and "코드 밖 최상위 불릿" in m2["2-1.count"][1] and "자" not in m2["2-1.count"][1].split("(")[1].split("·")[0]
    assert m2["2-1.literal"][1].startswith("필수 문자열 ‘확인’") and "{" not in m2["2-1.literal"][1]
    assert jv._required_text(["담당 부서 이름", "예상 완료 날짜", "작업 진행 상태", "추가 확인 사항"], "표 열 이름", "2-2.table") == "담당 부서 이름, 예상 완료 날짜, 작업 진행 상태, 추가 확인 사항"
    m = {"2-2.table": ("PASS", "표 존재"), "1-3.footnote_link": ("PASS", "각주 ↔ 정의 연결"), "1-2.code_block": ("PASS", "코드블록 개수"), "2-1.count": ("PASS", "불릿 개수")}
    assert jv.format_fail_families("목표를 제시하지 않았고 비용 지표가 없습니다.", m) == []          # 목표 · 지표는 표가 아님
    assert jv.format_fail_families("각주에 적힌 논문은 존재하지 않습니다.", m) == []                   # 출처 문제
    assert jv.format_fail_families("코드 블록의 계산식이 잘못됐습니다.", m) == []                       # 코드 동작 문제
    assert jv.format_fail_families("불릿 개수는 맞지만 B안이 빠졌습니다.", m) == []                     # 준수 인정
    assert jv.format_fail_families("항목 개수는 맞지만 B안이 빠졌습니다.", m) == []
    assert jv.format_fail_families("불릿 개수는 안 맞지만 내용은 충분합니다.", m) == ["2-1.count"]       # 부정은 인정이 아님 (Codex 2차)
    assert jv.format_fail_families("불릿 개수가 맞지 않습니다.", m) == ["2-1.count"]
    m2 = {**m, "2-1.char_count": ("PASS", "글자 수(300자 이내(공백 포함))")}
    assert jv.format_fail_families("글자 수는 안 맞지만 어조는 지켰습니다.", m2) == ["2-1.char_count"]   # 다른 조건의 '지켰'은 인정이 아님 (Codex 3차)
    assert jv.format_fail_families("불릿 개수를 맞추라는 요구를 어겼습니다.", m2) == ["2-1.count"]       # '맞추라는'은 지시
    assert jv.format_fail_families("불릿 개수는 맞습니다. 다만 B안이 빠졌습니다.", m2) == []
    assert jv.format_fail_families("표를 제시하지 않았고 각주 표시가 정의와 연결되지 않습니다.", m) == ["2-2.table", "1-3.footnote_link"]


class FormatCodex(FakeCodex):
    """첫 호출에서 J2-3.a 를 '한글만 조건 위반'으로 FAIL, 다시 물으면 PASS."""

    def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
        doc = json.loads(input_text)
        self.inputs.append(doc)
        first = len(self.inputs) == 1
        turns = [{"turn": int(n), "situations": [], "items": [
            {"id": i, "decision": "FAIL" if first and i == "J2-3.a" else "PASS", "question_quote": "", "answer_quote": "", "quote_turn": None, "fact": None,
             "reason": "질문은 한글만 허용했지만 답변에 영문 표기가 있습니다." if first and i == "J2-3.a" else "r"} for i in items]}
            for n, items in doc["판정할_항목"].items()]
        return "OK", {"turns": turns, "conclusion": {"verdict": "PASS", "reason": ""}}, ""


def test_format_fail_is_reasked_only_when_code_measured_that_condition(tmp_path: Path):
    # 10-22 밤: CODE 가 실제로 잰 형식 조건(한글만)을 LLM 이 또 FAIL 하면 CODE 결과를 적어 한 번 다시 묻고, CODE 가 재지 않았으면 그대로 둔다
    dec = FakeDecisions({"J2-3.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.8, "PASS": 0.2}, "confidence": 0.8}})
    codex = FormatCodex()
    measured = {"Q1_T1": {"2-1.charset": ("FAIL", "허용 문자 (한글만)")}}
    item = _run(tmp_path, dec, codex, code_measured=measured)["turn_results"]["1"]["items"]["J2-3.a"]
    assert len(codex.inputs) == 2 and jv.MEASURED_NOTE in codex.inputs[1]["주의"]
    assert codex.inputs[1]["프로그램이_잰_조건"] == {"T1": {"2-1.charset": {"결과": "FAIL", "조건": "허용 문자 (한글만)"}}} and item["decision"] == "PASS"
    sub = tmp_path / "b"
    sub.mkdir()
    codex2 = FormatCodex()
    item2 = _run(sub, dec, codex2, code_measured={"Q1_T1": {}})["turn_results"]["1"]["items"]["J2-3.a"]
    assert len(codex2.inputs) == 1 and item2["decision"] == "FAIL"


class FormatThenQuoteCodex(FakeCodex):
    """1회: J2-3.a 형식 FAIL(한글만). 2회(측정 안내): 인용이 원문에 없는 PASS. 3회: 안내가 또 오면 PASS, 안 오면 형식 FAIL 로 되돌아감."""

    def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
        doc = json.loads(input_text)
        self.inputs.append(doc)
        k = len(self.inputs)
        noted = jv.MEASURED_NOTE in str(doc.get("주의") or "")
        if k == 1:
            dec, reason, quote = "FAIL", "질문은 한글만 허용했지만 답변에 영문 표기가 있습니다.", ""
        elif k == 2:
            dec, reason, quote = "PASS", "조건을 지켰습니다.", "답변에 없는 문장"
        else:
            dec, reason, quote = ("PASS", "조건을 지켰습니다.", "") if noted else ("FAIL", "질문은 한글만 허용했지만 답변에 영문 표기가 있습니다.", "")
        turns = [{"turn": int(n), "situations": [], "items": [
            {"id": i, "decision": dec if i == "J2-3.a" else "PASS", "reason": reason if i == "J2-3.a" else "r", "question_quote": "",
             "answer_quote": quote if i == "J2-3.a" else "", "quote_turn": None, "fact": None} for i in items]} for n, items in doc["판정할_항목"].items()]
        return "OK", {"turns": turns, "conclusion": {"verdict": "PASS", "reason": ""}}, ""


def test_measured_note_is_kept_on_later_reasks(tmp_path: Path):
    # Codex 검토 10-22: 측정 안내로 고친 뒤 인용 오류로 다시 물을 때 안내가 빠져 형식 FAIL 이 되살아나던 것
    dec = FakeDecisions({"J2-3.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.8, "PASS": 0.2}, "confidence": 0.8}})
    codex = FormatThenQuoteCodex()
    item = _run(tmp_path, dec, codex, code_measured={"Q1_T1": {"2-1.charset": ("FAIL", "허용 문자 (한글만)")}})["turn_results"]["1"]["items"]["J2-3.a"]
    assert len(codex.inputs) == 3 and all(jv.MEASURED_NOTE in str(d.get("주의") or "") for d in codex.inputs[1:]) and item["decision"] == "PASS"


def test_j4_target_not_applicable_from_first_judge_goes_to_codex(tmp_path: Path, monkeypatch):
    # Codex 검토 10-22(제안): 목표 기준인 J4-2.a 를 1차 심판이 확신 있게 해당 없음이라 해도 Codex 가 다시 본다 · 목표 밖 항목은 묻지 않는다
    monkeypatch.setattr(jv, "JUDGE_TARGET_ONLY", True)
    monkeypatch.setattr(jv, "QUESTION_TARGETS", {1: {"J1-1.a", "J1-2.a", "J1-4.a", "J1-4.b", "J2-2.a", "J4-2.a"}})
    dec = FakeDecisions({"J4-2.a": {"choice": "NOT_APPLICABLE", "probabilities": {"NOT_APPLICABLE": 0.97}, "confidence": 0.97}})
    codex = FakeCodex({"J4-2.a": "FAIL"})
    line = _run(tmp_path, dec, codex)
    assert dec.calls == [([1], 7)] and codex.inputs[0]["판정할_항목"]["1"] == ["J4-2.a"]   # 목표 6 + 늘 보는 J5-3.a · 목표 J4 의 해당 없음만 Codex 로
    assert line["turn_results"]["1"]["items"]["J4-2.a"]["decision"] == "FAIL" and "J3-1.a" not in line["turn_results"]["1"]["items"]


def test_j14_fail_reask_is_default():
    assert {"J1-4.a", "J1-4.b"} <= jv.REASK_FAIL_ITEMS   # 10-22 밤: 옵션을 빠뜨리면 J1-4 FAIL 이 부풀어 기본값으로


def test_line_records_tokens_per_question(tmp_path: Path):
    # 사용자 10-23: 질문당 평가된 토큰 수 — 판정 줄마다 심판별 입력 · 출력 · 호출과 합계
    dec = FakeDecisions({"J2-2.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.8, "PASS": 0.2}, "confidence": 0.8}})
    line = _run(tmp_path, dec, FakeCodex({"J2-2.a": "FAIL"}))
    tok = line["tokens"]
    assert tok["Decisions"]["입력"] == 1000 and tok["Decisions"]["호출"] == 1 and tok["합계"]["입력"] >= 1000 and tok["다시_씀"] is False
    calls = {"decisions": {"reused": True, "first_judge": {"1": "jev"}, "usage": {}}, "codex": {"reused": True, "rounds": 0}}
    assert jv.question_tokens_used(calls)["합계"]["입력"] == 0 and jv.question_tokens_used(calls)["다시_씀"] is True


def test_error_case_turn_judges_only_j1_1_and_skips_codex(tmp_path: Path):
    """에러 케이스 턴(CODE 1-1 FAIL): Codex 에 보내지 않고 J1-1.a 만 FAIL, 나머지는 해당 없음 — 오류 문구 하나를 항목 넷이 세던 것 (10-25 밤)."""
    dec = FakeDecisions({"J2-2.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.9, "PASS": 0.1}, "confidence": 0.9},
                         "J1-1.a": {"choice": "PASS", "probabilities": {"PASS": 0.9, "FAIL": 0.1}, "confidence": 0.9}})
    codex = FakeCodex()
    line = _run(tmp_path, dec, codex, code_measured={"Q1_T1": {"1-1.response_nonempty": ("FAIL", "에러 케이스 없음")}})
    items = line["turn_results"]["1"]["items"]
    assert codex.inputs == [] and items["J1-1.a"]["decision"] == "FAIL" and "에러 케이스" in items["J1-1.a"]["how"]
    assert items["J2-2.a"]["decision"] == "NOT_APPLICABLE" and items["J2-2.a"]["overridden_from"] == "FAIL"
    assert all(v["decision"] == "NOT_APPLICABLE" for i, v in items.items() if i != "J1-1.a")


def test_duplicate_quote_fails_are_reasked_once_with_item_list(tmp_path: Path):
    """한 턴에서 두 항목이 같은 답 구절을 FAIL 근거로 쓰면 '같은_구절_항목'을 주고 한 번 다시 묻는다 (10-25 밤)."""
    quote = "네, 회의는 10시입니다."

    class DupCodex(FakeCodex):
        def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
            doc = json.loads(input_text)
            self.inputs.append(doc)
            first = len(self.inputs) == 1
            turns = []
            for n, items in doc["판정할_항목"].items():
                turns.append({"turn": int(n), "situations": [], "items": [
                    {"id": i, "decision": "FAIL" if (first or i == "J2-2.a") else "PASS", "reason": "r", "question_quote": "",
                     "answer_quote": quote if (first or i == "J2-2.a") else "", "quote_turn": None, "fact": None} for i in items]})
            return "OK", {"turns": turns, "conclusion": {"verdict": "FAIL", "reason": ""}}, ""

    dec = FakeDecisions({"J2-2.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.9, "PASS": 0.1}, "confidence": 0.9},
                         "J2-3.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.9, "PASS": 0.1}, "confidence": 0.9}})
    codex = DupCodex()
    items = _run(tmp_path, dec, codex)["turn_results"]["1"]["items"]
    assert len(codex.inputs) == 2 and set(codex.inputs[1]["같은_구절_항목"]["T1"]) >= {"J2-2.a", "J2-3.a"} and "같은 답 구절" in codex.inputs[1]["주의"]   # J5-1.a(늘 Codex) 도 같은 구절이면 함께
    assert items["J2-2.a"]["decision"] == "FAIL" and items["J2-3.a"]["decision"] == "PASS"
    assert jv.duplicate_quote_items({"turns": [{"turn": 1, "items": [{"id": "J2-2.a", "decision": "FAIL", "answer_quote": quote}, {"id": "J2-3.a", "decision": "FAIL", "answer_quote": "회의는 10시입니다."}]}]},
                                    [{"case_id": "Q1_T1", "question": "q", "turn": 1}], {"Q1_T1": quote}, {1: ["J2-2.a", "J2-3.a"]}) == [(1, "J2-2.a"), (1, "J2-3.a")]


def test_false_premise_and_misrule_guard_hold_na_with_wrong_rule():
    """연결 규칙의 조건이 그 턴에서 거짓이거나(J1-1.a 가 FAIL 이 아닌데 'J1-1.a가 FAIL인 경우'), J1-3 규칙을 다른 항목에 쓴 해당 없음은
    다시 묻고 남으면 보류(규칙 오적용) — 10-25 밤 AIONA Q6 7턴 9칸."""
    import tools.judge_v4 as jv
    decisions = {"J1-1.a": "NOT_APPLICABLE", "J1-3.a": "NOT_APPLICABLE", "J4-3.a": "NOT_APPLICABLE"}
    why = "J1-1.a가 FAIL인 경우 J4 항목은 해당 없음입니다."
    assert jv.false_premise(why, decisions)
    assert not jv.false_premise(why, {**decisions, "J1-1.a": "FAIL"})
    assert not jv.false_premise(why, {"J4-3.a": "NOT_APPLICABLE"})            # 조건 항목의 판정을 모르면 거짓으로 보지 않음
    assert not jv.false_premise(why, {**decisions, "J1-1.a": "UNVERIFIED"})   # 조건 항목이 보류여도
    for w in ("J1-3 모순이 적용되므로 다른 항목은 해당하지 않습니다.", "모순이 있는 질문에서 J1-3이 우선합니다.",
              "J1-3 상황이므로 수정 유지 항목은 해당하지 않습니다.", "모순이 있는 질문이므로 다른 항목은 해당하지 않습니다."):
        assert jv.misapplied_rule("J6-3.b", w), w
        assert not jv.misapplied_rule("J1-1.a", w), w
    assert jv.false_premise("J1-3이 적용되어 J1-2.a는 NOT_APPLICABLE입니다.", decisions)   # J1-3.a 가 해당 없음이면 J1-3 은 적용된 것이 아님
    assert not jv.false_premise("J1-3이 적용되어 J1-2.a는 NOT_APPLICABLE입니다.", {**decisions, "J1-3.a": "PASS"})
    # 조건 항목 자신의 해당 없음 · 규칙을 부정한 말은 규칙을 쓴 것이 아님 (v4-r8 점검: J1-3.b 5칸 · Gemini Q2 1턴 J4-4.a)
    assert not jv.false_premise("질문에 모순 조건이나 빠진 자료가 없어 J1-3 상황은 적용되지 않습니다.", decisions, "J1-3.b")
    assert not jv.false_premise("J1-3 상황의 모순·자료 누락이 없어 가능한 범위 결과는 해당하지 않습니다.", decisions, "J1-3.b")
    assert not jv.false_premise("상황표 2: J1-1.a가 FAIL이면 J4 항목은 NOT_APPLICABLE입니다. 이번 선별 목록에는 J1-1.a가 없어서 이 예외를 적용할 수 없고, 요약 요구가 없습니다.",
                                {**decisions, "J1-1.a": "PASS"}, "J4-4.a")
    assert jv.false_premise("J1-2.a가 FAIL인 거절·회피 답변이므로 결과물 품질 항목은 판정하지 않습니다.", {"J1-2.a": "PASS"}, "J2-3.a")   # '판정하지 않습니다'는 규칙을 쓴 말
    items = {"J1-1.a": {"decision": "NOT_APPLICABLE", "reason": ""}, "J1-3.a": {"decision": "NOT_APPLICABLE", "reason": ""},
             "J4-3.a": {"decision": "NOT_APPLICABLE", "reason": why},
             "J6-3.b": {"decision": "NOT_APPLICABLE", "reason": "J1-3 상황이므로 수정 유지 항목은 해당하지 않습니다."},
             "J1-2.a": {"decision": "NOT_APPLICABLE", "reason": "J1-3이 적용되어 J1-2.a는 NOT_APPLICABLE입니다."},
             "J2-4.b": {"decision": "NOT_APPLICABLE", "reason": "J2-4.a가 FAIL이므로 해당 없음", "override": "프로그램"},
             "J6-1.a": {"decision": "PASS", "reason": "존댓말을 지켰습니다."}}
    jv.misrule_guard(items)
    assert items["J4-3.a"]["decision"] == "UNVERIFIED" and items["J4-3.a"]["kind"] == "규칙 오적용"
    assert items["J6-3.b"]["decision"] == "UNVERIFIED" and items["J1-2.a"]["decision"] == "UNVERIFIED"
    assert items["J2-4.b"]["decision"] == "NOT_APPLICABLE" and items["J6-1.a"]["decision"] == "PASS"   # 프로그램이 건 해당 없음 · 통과는 그대로
    from tools import report_html as R
    assert R.hold_kind(items["J4-3.a"]) == "규칙 오적용"


def test_reason_contradicts_for_not_applicable_with_own_verdict():
    """해당 없음인데 사유가 제 판정을 PASS/FAIL 로 말함('J1-3.a는 PASS') — 다른 항목의 판정을 든 말 · 조건 미충족 말은 아님 (10-25 밤)."""
    import tools.judge_v4 as jv
    assert jv.reason_contradicts("NOT_APPLICABLE", "답변이 이를 지적해 J1-3.a는 PASS입니다.", "J1-3.a")
    assert jv.reason_contradicts("NOT_APPLICABLE", "질문의 모순을 밝혔으므로 J1-3.a는 PASS", "J1-3.a")
    assert not jv.reason_contradicts("NOT_APPLICABLE", "J1-1.a가 FAIL인 경우 J4 항목은 해당 없음입니다.", "J4-3.a")
    assert not jv.reason_contradicts("NOT_APPLICABLE", "적용 기준을 충족하지 않아 해당 없음입니다.", "J2-4.b")
    assert not jv.reason_contradicts("NOT_APPLICABLE", "J1-3이 적용되어 J1-2.a는 NOT_APPLICABLE입니다.", "J1-2.a")
    items = {"J1-3.a": {"decision": "NOT_APPLICABLE", "reason": "답변이 이를 지적해 J1-3.a는 PASS입니다."},
             "J2-3.a": {"decision": "NOT_APPLICABLE", "reason": "J1-2.a가 FAIL이므로 해당 없음", "override": "거절"}}
    jv.reason_decision_guard(items)
    assert items["J1-3.a"]["decision"] == "UNVERIFIED" and items["J1-3.a"]["kind"] == "사유 모순"
    assert items["J2-3.a"]["decision"] == "NOT_APPLICABLE"


def test_recode_marks_error_case_turns_from_current_code_eval(tmp_path: Path):
    """--recode: 지금 CODE 1-1 이 잡은 에러 케이스 턴(서비스 오류 문구)은 심판을 다시 부르지 않고 J1-1.a 만 FAIL · 나머지 해당 없음 (10-25 밤)."""
    import json
    import tools.judge_v4 as jv
    rows = [{"case_id": "Q6_T1", "question": "q1", "answer": "a1", "conversation_id": "c6", "turn": 1},
            {"case_id": "Q6_T2", "question": "q2", "answer": "응답 생성 중 오류가 발생했습니다. 다시 시도해주세요.", "conversation_id": "c6", "turn": 2}]

    def case(cid, verdict):
        return {"case_id": cid, "case_status": "EVALUATED", "summary": {"common": {"pass": 1 if verdict == "PASS" else 0, "fail": 1 if verdict == "FAIL" else 0, "unjudged": 0}},
                "results": [{"check_id": "1-1.response_nonempty", "verdict": verdict, "counted": True, "area": "common", "title": "응답 존재"}]}
    run = {"evaluations": {"aiona": {"cases": [case("Q6_T1", "PASS"), case("Q6_T2", "FAIL")]}}}
    line = {"version": jv.VERSION, "provider": "aiona", "question": 6, "status": "judged", "verdict": "FAIL", "semantic": "FAIL", "code_fails": [],
            "turn_results": {"1": {"items": {"J1-1.a": {"decision": "PASS"}, "J3-1.a": {"decision": "PASS"}}},
                             "2": {"items": {"J1-1.a": {"decision": "NOT_APPLICABLE", "reason": ""}, "J1-3.a": {"decision": "NOT_APPLICABLE"},
                                             "J2-3.a": {"decision": "FAIL", "reason": "x"}, "J6-1.a": {"decision": "PASS"}}}}}
    rp = tmp_path / "results.jsonl"
    rp.write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
    assert jv.recode(run, rows, rp, {"aiona"}, {6}) == 0
    last = json.loads(rp.read_text(encoding="utf-8").splitlines()[-1])
    t2 = last["turn_results"]["2"]["items"]
    assert last["recoded"]["error_case_turns"] == [2] and "1-1.response_nonempty" in last["code_fails"]
    assert t2["J1-1.a"]["decision"] == "FAIL" and t2["J2-3.a"]["decision"] == "NOT_APPLICABLE" and t2["J6-1.a"]["decision"] == "NOT_APPLICABLE"
    assert last["turn_results"]["1"]["items"]["J1-1.a"]["decision"] == "PASS"   # 다른 턴은 그대로


def test_hold_analysis_guards_resolve_only_with_two_pass_signals():
    """10-25 밤 보류 분석 반영 — 출처 없는 J5-1.a FAIL · 사유 모순은 1차 심판이 확신한 PASS 일 때만 PASS, 아니면 보류. FAIL 은 새로 만들지 않음."""
    import tools.judge_v4 as jv
    fact = lambda first, conf, entries: {"J5-1.a": {"decision": "FAIL", "jev": first, "decisions_confidence": conf, "entries": entries}}
    nosrc = {"decision": "FAIL", "reason": "전문 정의와 다름", "fact": {"verdict": "FALSE", "sources": []}}
    items = fact("PASS", 0.86, [nosrc]); jv.fact_fail_guard(items)
    assert items["J5-1.a"]["decision"] == "PASS" and "1차 심판 PASS" in items["J5-1.a"]["how"]
    items = fact("PASS", 0.5, [nosrc]); jv.fact_fail_guard(items)
    assert items["J5-1.a"]["decision"] == "UNVERIFIED" and items["J5-1.a"]["kind"] == "확인 못함"
    true_fail = {"decision": "FAIL", "reason": "J5-3 에서 따로", "fact": {"verdict": "TRUE", "sources": []}}
    items = fact("FAIL", 0.9, [true_fail]); jv.fact_fail_guard(items)
    assert items["J5-1.a"]["decision"] == "UNVERIFIED" and items["J5-1.a"]["kind"] == "사유 모순" and true_fail["contradiction_from"] == "FAIL"
    backed = {"decision": "FAIL", "reason": "반감기 거짓", "fact": {"verdict": "FALSE", "sources": [{"url": "u", "quote": "q"}]}}
    items = fact("PASS", 0.95, [backed]); jv.fact_fail_guard(items)
    assert items["J5-1.a"]["decision"] == "FAIL"   # 반박 출처가 있으면 1차 심판과 상관없이 FAIL
    # 사유 모순: 결론 PASS + 1차 PASS 확신 → PASS / 1차 FAIL → 보류 / PASS 인데 사유 FAIL → 보류(FAIL 을 만들지 않음)
    its = {"J1-4.a": {"decision": "FAIL", "reason": "인용한 줄은 요청한 순서라 판정은 PASS여야 합니다.", "jev": "PASS", "decisions_confidence": 0.8}}
    jv.reason_decision_guard(its); assert its["J1-4.a"]["decision"] == "PASS"
    its = {"J1-4.a": {"decision": "FAIL", "reason": "판정은 PASS여야 합니다.", "jev": "FAIL", "decisions_confidence": 0.8}}
    jv.reason_decision_guard(its); assert its["J1-4.a"]["decision"] == "UNVERIFIED"
    its = {"J3-1.a": {"decision": "PASS", "reason": "실제로는 FAIL 입니다.", "jev": "FAIL", "decisions_confidence": 0.9}}
    jv.reason_decision_guard(its); assert its["J3-1.a"]["decision"] == "UNVERIFIED"


def test_codex_never_answered_item_follows_confident_first_judge_and_quote_bullets():
    """Codex 가 다시 물어도 답하지 않은 항목은 1차 심판이 확신한 PASS · 해당 없음이면 그 판정, FAIL 이면 보류 · 인용 대조는 줄머리 글머리 기호를 뗌 (10-25 밤)."""
    import tools.judge_v4 as jv
    from tools import judge_split as js
    none = {"decision": "UNVERIFIED", "entries": [], "problems": ["판정 없음"]}
    assert jv.combine_screen(none, {"choice": "NOT_APPLICABLE", "confidence": 0.82}, True, item="J2-5.a")[0] == "NOT_APPLICABLE"
    assert jv.combine_screen(none, {"choice": "PASS", "confidence": 0.5}, True, item="J2-5.a")[0] == "UNVERIFIED"
    assert jv.combine_screen(none, {"choice": "FAIL", "confidence": 0.9, "probabilities": {"FAIL": 0.9}}, True, item="J2-5.a")[0] == "UNVERIFIED"
    assert js.quoted_in("- 오늘 날짜가 10월 21일이라 확인이 필요합니다", ["오늘 날짜가 10월 21일이라 확인이 필요합니다"])
    assert not js.quoted_in("1. 오늘 날짜가 10월 22일", ["오늘 날짜가 10월 21일"])


def test_quote_locations_tell_where_a_misquote_actually_is():
    """인용 불일치로 다시 묻을 때 그 글이 실제로 있는 턴 · 쪽(질문 · 답변)을 알려 준다 (10-25 밤 보류 분석 인용 6칸)."""
    import tools.judge_v4 as jv
    turns = [{"case_id": "Q1_T1", "turn": 1, "question": "첫 질문입니다."}, {"case_id": "Q1_T2", "turn": 2, "question": "두 번째 질문입니다."}]
    answers = {"Q1_T1": "A안 소요 시간은 총 38분입니다.", "Q1_T2": "B안으로 정리했습니다."}
    out = {"turns": [{"turn": 2, "items": [{"id": "J3-2.b", "decision": "FAIL", "answer_quote": "A안 소요 시간은 총 38분입니다."}]}]}
    locs = jv.quote_locations(out, turns, answers, [(2, "J3-2.b")])
    assert "T1 답변" in locs["T2:J3-2.b"] and "T2 답변에서만" in locs["T2:J3-2.b"]
    assert jv.quote_locations(out, turns, answers, [(2, "J1-1.a")]) == {}

