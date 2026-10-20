# -*- coding: utf-8 -*-
"""Codex 검토 반영(2026-10-20 밤): 항목 단위 다시 묻기 · Codex 실패는 보류 · PASS 의 인용 불일치는 표시만 · 보고서 v2 의 보류 분류와 사람 판정 반영."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_judge_screen import FakeCodex, FakeDecisions, _run
from tools import judge_split as js
from tools import judge_v4 as jv
from tools import report_html as rh


def test_missing_items_are_per_item_and_merge_replaces_only_asked():
    flagged = {1: ["J2-2.a", "J4-2.a"], 2: ["J6-1.a"]}
    out = {"turns": [{"turn": 1, "situations": [{"id": "J2-2"}], "items": [{"id": "J2-2.a", "decision": "PASS"}]}]}
    assert jv.missing_codex_items(out, flagged) == {1: ["J4-2.a"], 2: ["J6-1.a"]}     # 턴 1 에 항목이 있어도 빠진 항목은 빠진 것
    extra = {"turns": [{"turn": 1, "situations": [{"id": "J2-2"}, {"id": "J4-2"}], "items": [{"id": "J4-2.a", "decision": "FAIL"}, {"id": "J2-2.a", "decision": "FAIL"}]},
                       {"turn": 2, "situations": [], "items": [{"id": "J6-1.a", "decision": "PASS"}]}], "conclusion": {"verdict": "FAIL"}}
    merged = jv.merge_codex_output(out, extra, replace={(1, "J4-2.a")})
    t1 = {e["id"]: e["decision"] for e in merged["turns"][0]["items"]}
    assert t1 == {"J2-2.a": "PASS", "J4-2.a": "FAIL"}                                   # 묻지 않은 J2-2.a 는 원래 답 유지
    assert [s["id"] for s in merged["turns"][0]["situations"]] == ["J2-2", "J4-2"] and merged["turns"][1]["turn"] == 2
    assert jv.missing_codex_items(merged, flagged) == {} and merged["conclusion"]["verdict"] == "FAIL"


class PartialCodex(FakeCodex):
    """첫 호출은 요청한 항목의 절반만 답하고, 다시 물으면 다 답한다."""

    def judge(self, prompt, input_text, search, tag="", output_schema=None):
        st, out, err = super().judge(prompt, input_text, search, tag, output_schema)
        if len(self.inputs) == 1:
            for t in out["turns"]:
                t["items"] = t["items"][: max(1, len(t["items"]) // 2)]
        return st, out, err


def test_screen_one_reasks_only_missing_items(tmp_path: Path):
    dec = FakeDecisions({"J2-2.a": {"choice": "FAIL", "probabilities": {"FAIL": 0.8}, "confidence": 0.8},
                         "J4-2.a": {"choice": "PASS", "probabilities": {"PASS": 0.6, "FAIL": 0.4}, "confidence": 0.6},
                         "J3-2.a": {"choice": "PASS", "probabilities": {"PASS": 0.6, "FAIL": 0.4}, "confidence": 0.6}})
    codex = PartialCodex({"J2-2.a": "FAIL"})   # Codex 도 FAIL 이면 심판 갈림(보류)이 없다
    line = _run(tmp_path, dec, codex)
    first = set(codex.inputs[0]["판정할_항목"]["1"])
    again = set(codex.inputs[1]["판정할_항목"]["1"])
    assert again and again < first                                                       # 두 번째는 빠진 항목만
    assert line["calls"]["codex"]["rounds"] == 2 and line["calls"]["codex"]["missing_items"] == {}
    assert not any(v["decision"] == "UNVERIFIED" for v in line["turn_results"]["1"]["items"].values())


class BrokenCodex(FakeCodex):
    def judge(self, prompt, input_text, search, tag="", output_schema=None):
        self.inputs.append(json.loads(input_text))
        return "ERROR", None, "codex exec 실패"


def test_screen_one_holds_when_codex_fails_even_if_decisions_pass(tmp_path: Path):
    dec = FakeDecisions({"J4-2.a": {"choice": "PASS", "probabilities": {"PASS": 0.6, "FAIL": 0.4}, "confidence": 0.6}})
    line = _run(tmp_path, dec, BrokenCodex())
    assert line["verdict"] == "HOLD" and "Codex 판정 실행 오류" in line["judge_error"] and line["reason"] == line["judge_error"]


def test_check_gpt_quote_mismatch_holds_fail_but_only_notes_pass():
    turns = [{"case_id": "Q1_T1", "question": "회의 시간을 알려 주세요.", "turn": 1}]
    answers = {"Q1_T1": "회의는 10시입니다."}
    out = {"turns": [{"turn": 1, "items": [
        {"id": "J1-1.a", "decision": "PASS", "answer_quote": "원문에 없는 문장", "question_quote": ""},
        {"id": "J2-2.a", "decision": "FAIL", "answer_quote": "원문에 없는 문장", "question_quote": ""}]}]}
    got = js.check_gpt(out, turns, answers, {1: ["J1-1.a", "J2-2.a", "J3-1.a"]})[1]
    assert got["J1-1.a"]["decision"] == "PASS" and got["J1-1.a"]["notes"] and not got["J1-1.a"]["problems"]
    assert got["J2-2.a"]["decision"] == "UNVERIFIED" and "answer_quote" in got["J2-2.a"]["problems"][0]
    assert got["J3-1.a"]["decision"] == "UNVERIFIED" and got["J3-1.a"]["problems"] == ["판정 없음"]


def _line(items, provider="gpt", q=1, code_fails=None, status="judged"):
    return {"version": jv.SCREEN_VERSION, "provider": provider, "question": q, "turns": 1, "status": status, "verdict": "HOLD",
            "code_fails": code_fails or [], "turn_results": {"1": {"items": items}}}


def test_report_normalize_hold_kinds_and_human_decisions(tmp_path: Path):
    items = {
        "J2-2.a": {"decision": "UNVERIFIED", "how": "Codex 보류 · Decisions PASS", "codex": "UNVERIFIED", "problems": ["판정 없음"], "entries": []},
        "J3-2.a": {"decision": "UNVERIFIED", "how": "Codex 보류 · Decisions PASS", "codex": "UNVERIFIED", "problems": ["J3-2.a: answer_quote 가 답변 원문에 없음"],
                   "entries": [{"decision": "FAIL", "reason": "근거 없음", "answer_quote": "x"}]},
        "J5-1.a": {"decision": "UNVERIFIED", "how": "Codex 보류 · Decisions 해당 없음", "codex": "UNVERIFIED", "problems": [], "entries": [{"decision": "UNVERIFIED", "reason": "확인 못함"}]},
        "J1-1.a": {"decision": "PASS", "how": "Decisions PASS 0.99", "codex": None, "problems": [], "entries": []},
    }
    norm = {it["item"]: it for it in rh.normalize_items(_line(items))}
    assert norm["J2-2.a"]["kind"] == "심판 출력 누락" and norm["J3-2.a"]["kind"] == "인용 불일치" and norm["J5-1.a"]["kind"] == "확인 못함" and norm["J1-1.a"]["kind"] == ""
    assert norm["J3-2.a"]["reason"] == "근거 없음"                                          # entries 에서 사유를 꺼낸다
    human = {("gpt", 1, 1, "J2-2.a"): {"new": "PASS"}, ("gpt", 1, 1, "J3-2.a"): {"new": "NOT_APPLICABLE"}, ("gpt", 1, 1, "J5-1.a"): {"new": "PASS"}}
    verdict, after, changed = rh.effective(_line(items), list(norm.values()), human)
    assert verdict == "PASS" and changed == 3                                             # 보류 셋을 사람이 정하면 통과
    verdict2, _, _ = rh.effective(_line(items), list(norm.values()), {("gpt", 1, 1, "J2-2.a"): {"new": "FAIL"}})
    assert verdict2 == "FAIL"
    verdict3, _, _ = rh.effective(_line(items, code_fails=["1-2.code_block"]), list(norm.values()), human)
    assert verdict3 == "FAIL"                                                             # CODE FAIL 은 사람 판정으로 안 바뀜


def test_held_apply_merges_decisions_and_regenerates_report(tmp_path: Path, monkeypatch):
    import tools.modelgrade as mg
    out = tmp_path / "judge" / "abc123" / "v4"
    out.mkdir(parents=True)
    items = {"J2-2.a": {"decision": "UNVERIFIED", "how": "Codex 보류 · Decisions PASS", "codex": "UNVERIFIED", "problems": ["판정 없음"], "entries": []}}
    (out / "results.jsonl").write_text(json.dumps(_line(items), ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(rh, "RUNS", tmp_path / "runs")
    f = tmp_path / "held_decisions_abc123.json"
    f.write_text(json.dumps({"run_id": "abc123", "reviewer": "검토자", "decisions": [
        {"provider": "gpt", "question": 1, "turn": 1, "item": "J2-2.a", "old": "UNVERIFIED", "new": "PASS", "note": "문제 없음"},
        {"provider": "gpt", "question": 1, "turn": 1, "item": "J9-9.z", "new": "PASS"}]}, ensure_ascii=False), encoding="utf-8")
    assert mg.main(["held-apply", "--dir", str(out), "--file", str(f)]) == 0
    saved = json.loads((out / "human_decisions.json").read_text(encoding="utf-8"))
    assert saved["run_id"] == "abc123" and len(saved["decisions"]) == 1 and saved["decisions"][0]["reviewer"] == "검토자"   # 없는 항목은 거부
    text = (out / "report.html").read_text(encoding="utf-8")
    assert "사람 판정 전 통과" in text and "후 통과" in text
    bad = tmp_path / "other.json"
    bad.write_text(json.dumps({"run_id": "zzz", "decisions": []}), encoding="utf-8")
    try:
        mg.main(["held-apply", "--dir", str(out), "--file", str(bad)])
        assert False, "다른 실행의 파일은 거부해야 한다"
    except SystemExit as exc:
        assert "다른 실행" in str(exc)
