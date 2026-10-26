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

    def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
        st, out, err = super().judge(prompt, input_text, search, tag, output_schema, images)
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
    def judge(self, prompt, input_text, search, tag="", output_schema=None, images=None):
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
    assert verdict2 == "HOLD"                                                             # 보류가 남으면 FAIL 이 있어도 보류 (사용자 10-21)
    verdict2b, _, _ = rh.effective(_line(items), list(norm.values()), {**human, ("gpt", 1, 1, "J2-2.a"): {"new": "FAIL"}})
    assert verdict2b == "FAIL"                                                            # 보류를 모두 정한 뒤 FAIL 이 남으면 FAIL
    verdict3, _, _ = rh.effective(_line(items, code_fails=["1-2.code_block"]), list(norm.values()), human)
    assert verdict3 == "FAIL"                                                             # CODE FAIL 은 사람 판정으로 안 바뀜


def test_report_keeps_hold_kind_set_by_judge():
    """판정 안전장치가 붙인 보류 종류(반박 출처 없는 사실 FAIL → 확인 못함, 사유 모순)는 보고서·결과 요약이 다시 짐작하지 않는다 (10-22)."""
    from tools import build_result_summary as brs
    items = {
        "J5-1.a": {"decision": "UNVERIFIED", "kind": "확인 못함", "how": "Codex FAIL → 반박 출처 없음 → 확인 못 함", "codex": "FAIL", "problems": [],
                   "entries": [{"decision": "FAIL", "reason": "틀림"}]},
        "J1-4.a": {"decision": "UNVERIFIED", "kind": "사유 모순", "how": "Codex FAIL → 사유 모순", "codex": "FAIL", "problems": [],
                   "entries": [{"decision": "FAIL", "reason": "불필요한 단락은 없습니다"}]},
    }
    norm = {it["item"]: it for it in rh.normalize_items(_line(items))}
    assert norm["J5-1.a"]["kind"] == "확인 못함" and norm["J1-4.a"]["kind"] == "사유 모순"
    exported = {k: {kk: vv for kk, vv in v.items() if kk != "entries"} for k, v in items.items()}   # 내보낸 JSON 은 entries 가 없다
    assert {brs.hold_kind(it) for it in rh.normalize_items(_line(exported))} == {"확인 못함", "사유 모순"}


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


def test_error_suspect_turn_is_held_and_a_person_picks_error_case_pass_or_fail(tmp_path: Path, monkeypatch):
    """에러 케이스 의심(사용자 10-26): CODE 0-1 이 보류한 턴에 '에러 케이스 확인' 보류 항목이 붙어 질문은 보류 →
    사람이 에러 케이스를 고르면 FAIL(J1-1.a 만 FAIL · 나머지 해당 없음), held-apply 가 고른 값을 에러 케이스 기록에 쌓고 같은 글은 기록대로."""
    import tools.modelgrade as mg
    from code_eval import error_cases
    monkeypatch.setenv("MODELGRADE_ERROR_CASES", str(tmp_path / "labels.json"))
    refusal = "저는 언어 모델입니다. 그것은 제가 할 수 있는 범위를 넘어서는 것이라 할 수 없어요."
    cases = {"Q1_T1": {"answer": refusal, "meta": {"skipped_conditions": [
        {"check_id": "1-1.response_nonempty", "subcategory": "0-1", "error_suspect": "오류 · 거절 말", "phrase": "언어 모델", "reason": "에러 케이스 의심"}]}}}
    sus = jv.error_suspects([{"case_id": "Q1_T1", "turn": 1}], cases)
    assert set(sus) == {1}
    items = {"J1-1.a": {"decision": "FAIL", "how": "Decisions FAIL", "problems": [], "entries": []},
             "J2-2.a": {"decision": "FAIL", "how": "Decisions FAIL", "problems": [], "entries": []}}
    line = jv.apply_error_suspects({**_line(items), "semantic": "FAIL", "verdict": "FAIL"}, sus)
    hold = line["turn_results"]["1"]["items"][jv.ERROR_SUSPECT_ITEM]
    assert line["verdict"] == "HOLD" and hold["decision"] == "UNVERIFIED" and hold["kind"] == "에러 케이스 확인" and line["error_suspect_turns"] == [1]
    assert jv.apply_error_suspects(line, {})["verdict"] == "FAIL"   # 의심이 사라지면(기록이 생김) 보류 항목도 지운다
    norm = rh.normalize_items(line)
    assert any(it["kind"] == "에러 케이스 확인" and it["answer_text"] == refusal for it in norm)
    verdict, after, _ = rh.effective(line, norm, {("gpt", 1, 1, jv.ERROR_SUSPECT_ITEM): {"new": "ERROR_CASE"}})
    dec = {it["item"]: it["decision"] for it in after}
    assert verdict == "FAIL" and dec["J1-1.a"] == "FAIL" and dec["J2-2.a"] == "NOT_APPLICABLE"   # 한 오류 한 항목
    out = tmp_path / "judge" / "abc123" / "v4"
    out.mkdir(parents=True)
    (out / "results.jsonl").write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(rh, "RUNS", tmp_path / "runs")
    f = tmp_path / "held.json"
    f.write_text(json.dumps({"run_id": "abc123", "reviewer": "검토자", "decisions": [
        {"provider": "gpt", "question": 1, "turn": 1, "item": jv.ERROR_SUSPECT_ITEM, "old": "UNVERIFIED", "new": "ERROR_CASE"}]}, ensure_ascii=False), encoding="utf-8")
    assert mg.main(["held-apply", "--dir", str(out), "--file", str(f)]) == 0
    lab = error_cases.lookup("저는 언어 모델입니다.\n그것은 제가 할 수 있는 범위를 넘어서는 것이라 할 수 없어요.")   # 공백만 다른 같은 글
    assert lab and lab["label"] == "ERROR_CASE" and lab["provider"] == "gpt" and lab["reviewer"] == "검토자"
    assert "에러 케이스" in (out / "report.html").read_text(encoding="utf-8")
    verdict, _, _ = rh.effective(line, norm, {})   # 사람 판정이 없어도 기록의 같은 글이면 기록대로
    assert verdict == "FAIL"


def test_empty_reply_and_no_answer_are_held_for_a_person():
    """빈 응답(수집은 됐는데 글이 없음 · 옛 수집기의 '복사 다시 해도 빈 답') · 답 없음은 바로 FAIL 이 아니라 보류 — 대화 화면을 보고 사람이 정함 (사용자 10-26)."""
    turns = [{"case_id": "Q1_T1", "question": "회의 시간을 알려 주세요.", "turn": 1}]
    got = jv.pre_status(turns, {"Q1_T1": {"answer": "", "collection": {"status": "SUCCESS"}}}, {"Q1_T1": ""})
    assert got["status"] == "held_error_case" and jv.PRE_VERDICT["held_error_case"] == "HOLD"
    old = {"Q1_T1": {"answer": "", "collection": {"status": "COPY_FAILED", "error": "답이 비어 있음 — 복사를 다시 시도했으나 여전히 비어 있어 보류"}}}
    assert jv.pre_status(turns, old, {"Q1_T1": ""})["status"] == "held_error_case"
    gone = {"Q1_T1": {"answer": "", "collection": {"status": "ERROR", "error": "응답 시간 초과"}}}
    assert jv.pre_status(turns, gone, {"Q1_T1": ""})["status"] == "held_no_answer" and jv.PRE_VERDICT["held_no_answer"] == "HOLD"
    copy = {"Q1_T1": {"answer": "", "collection": {"status": "COPY_FAILED", "error": "복사 버튼을 찾지 못함"}}}
    assert jv.pre_status(turns, copy, {"Q1_T1": ""})["status"] == "held_copy_failed"
    two = turns + [{"case_id": "Q1_T2", "question": "장소는요?", "turn": 2}]
    data = {"Q1_T1": {"answer": "", "collection": {"status": "SUCCESS"}}, "Q1_T2": {"answer": "3층입니다.", "collection": {"status": "SUCCESS"}}}
    assert jv.pre_status(two, data, {"Q1_T1": "", "Q1_T2": "3층입니다."}) is None   # 일부 턴만 비면 심판이 보고, 빈 턴은 CODE 0-1 FAIL(에러 케이스 턴)


def test_code_hold_is_a_held_item_and_the_persons_pass_fail_and_reason_are_recorded(tmp_path: Path, monkeypatch):
    """코드 판정 확인(사용자 10-26): CODE 가 보류한 애매한 조건(code_hold)은 판정 줄의 보류 항목이 되어 질문은 보류 → 사람이 PASS · FAIL 과 사유를 고르면
    질문 판정에 반영되고, held-apply 가 기록(코드확인_기록.json)에 쌓는다 — 기준을 고치는 재료."""
    import tools.modelgrade as mg
    from tools import human_records
    monkeypatch.setenv("MODELGRADE_CODE_HOLDS", str(tmp_path / "holds.json"))
    monkeypatch.setenv("MODELGRADE_ERROR_CASES", str(tmp_path / "labels.json"))
    cases = {"Q1_T1": {"answer": "본문", "meta": {"skipped_conditions": [
        {"check_id": "2-1.charset", "subcategory": "2-1", "code_hold": "서비스 꼬리글", "reason": "꼬리글에만 있음", "excerpt": "데이터 제공: MET Norway"}]}}}
    holds = jv.human_holds([{"case_id": "Q1_T1", "turn": 1}], cases)
    assert holds["code"] == {1: [{"check_id": "2-1.charset", "kind": "서비스 꼬리글", "reason": "꼬리글에만 있음", "excerpt": "데이터 제공: MET Norway"}]}
    items = {"J1-1.a": {"decision": "PASS", "how": "Decisions PASS", "problems": [], "entries": []}}
    line = jv.apply_human_holds({**_line(items), "semantic": "PASS", "verdict": "PASS"}, holds)
    it = line["turn_results"]["1"]["items"]["2-1.charset"]
    assert line["verdict"] == "HOLD" and it["kind"] == "코드 판정 확인" and it["code_hold"] == "서비스 꼬리글" and line["code_hold_turns"] == [1]
    assert jv.apply_human_holds(line, {"error": {}, "code": {}})["verdict"] == "PASS"   # 보류가 사라지면 항목도 지운다
    norm = rh.normalize_items(line)
    assert rh.effective(line, norm, {("gpt", 1, 1, "2-1.charset"): {"new": "PASS"}})[0] == "PASS"
    assert rh.effective(line, norm, {("gpt", 1, 1, "2-1.charset"): {"new": "FAIL"}})[0] == "FAIL"
    out = tmp_path / "judge" / "abc123" / "v4"
    out.mkdir(parents=True)
    (out / "results.jsonl").write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(rh, "RUNS", tmp_path / "runs")
    page = rh.write_report(out, None).read_text(encoding="utf-8")   # 고르기 전 — 보류 칸에 PASS · FAIL 버튼과 사유 칸
    assert "코드 판정 확인" in page and "사유(기준 고칠 때 씀)" in page
    f = tmp_path / "held.json"
    f.write_text(json.dumps({"run_id": "abc123", "reviewer": "검토자", "decisions": [
        {"provider": "gpt", "question": 1, "turn": 1, "item": "2-1.charset", "old": "UNVERIFIED", "new": "PASS", "note": "서비스가 붙인 줄이라 답이 아님"}]},
        ensure_ascii=False), encoding="utf-8")
    assert mg.main(["held-apply", "--dir", str(out), "--file", str(f)]) == 0
    rec = json.loads((tmp_path / "holds.json").read_text(encoding="utf-8"))["records"]
    assert len(rec) == 1 and rec[0]["label"] == "PASS" and rec[0]["kind"] == "서비스 꼬리글" and rec[0]["note"].startswith("서비스가")
    assert human_records.summary() == [{"kind": "서비스 꼬리글", "PASS": 1, "FAIL": 0, "notes": ["PASS: 서비스가 붙인 줄이라 답이 아님"]}]
