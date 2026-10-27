# -*- coding: utf-8 -*-
"""기준 다듬기 루프(tools/refine_loop.py)의 모델 호출 없는 부분 — 표본 고르기 · 안 적용(임시 파일) · 점수 · 채택 판단 · 사람 표시 · 교훈 기록."""
import json
from pathlib import Path

import pytest

from tools import refine_loop as rl


def _cell(q, p, decision, item="J1-4.b", turn=1, kind="싱글", first=None):
    return {"type": kind, "question": q, "provider": p, "turn": turn, "item": item, "decision": decision, "first": first or decision,
            "codex": None, "reason": "", "answer": "답", "request": "요청", "facts": [], "flagged": ""}


def test_select_cases_balances_fail_and_pass_and_spreads_models():
    cells = [_cell(q, p, "FAIL") for q in range(1, 7) for p in ("aiona", "gpt")] + [_cell(q, p, "PASS") for q in range(1, 7) for p in ("claude", "gemini")]
    cells += [_cell(9, "aiona", "UNVERIFIED")]
    got = rl.select_cases(cells, n_fail=4, n_pass=4, seed=3)
    assert len(got) == 8 and sum(1 for c in got if c["decision"] == "PASS") == 4
    assert len({(c["provider"], c["question"]) for c in got}) == 8          # 같은 (모델, 질문)을 두 번 뽑지 않음
    assert [c["no"] for c in got] == list(range(1, 9))
    only_multi = rl.select_cases(cells, 2, 2, types=["멀티턴"])
    assert only_multi == []


def test_truth_from_marks_and_human_decisions(tmp_path: Path):
    f = tmp_path / "표시결과.json"
    f.write_text(json.dumps({"rows": [{"provider": "gpt", "question": 3, "turn": 1, "item": "J1-4.b", "decision": "FAIL", "mark": "틀림"},
                                       {"provider": "gpt", "question": 4, "turn": 1, "item": "J1-4.b", "decision": "PASS", "mark": "맞음"},
                                       {"provider": "gpt", "question": 5, "turn": 1, "item": "J1-4.b", "decision": "PASS", "mark": "애매"}],
                             "decisions": [{"provider": "aiona", "question": 1, "turn": 1, "item": "J1-4.b", "decision": "FAIL"}]}, ensure_ascii=False), encoding="utf-8")
    marks = rl.load_marks(f)
    assert rl.truth_of(_cell(3, "gpt", "FAIL"), marks[("gpt", 3, 1, "J1-4.b")]) == "PASS"      # 틀림 → 반대
    assert rl.truth_of(_cell(4, "gpt", "PASS"), marks[("gpt", 4, 1, "J1-4.b")]) == "PASS"      # 맞음 → 그대로
    assert rl.truth_of(_cell(5, "gpt", "PASS"), marks[("gpt", 5, 1, "J1-4.b")]) is None        # 애매 → 정답 없음
    assert rl.truth_of(_cell(1, "aiona", "PASS"), marks[("aiona", 1, 1, "J1-4.b")]) == "FAIL"  # 사람 판정 파일


def test_truth_from_pass_fail_error_marks(tmp_path: Path):
    """표시용 표(10-26)는 심판 대비 맞음 · 틀림이 아니라 사람이 본 결과 PASS · FAIL · 에러 케이스를 고른다."""
    f = tmp_path / "표시결과_사람.json"
    f.write_text(json.dumps({"rows": [{"provider": "gpt", "question": 3, "turn": 2, "item": "J3-1.a", "mark": "FAIL"},
                                      {"provider": "gpt", "question": 4, "turn": 1, "item": "J3-1.a", "mark": "PASS"},
                                      {"provider": "gpt", "question": 5, "turn": 1, "item": "J3-1.a", "mark": "에러 케이스"}]}, ensure_ascii=False), encoding="utf-8")
    marks = rl.load_marks(f)
    assert rl.truth_of(_cell(3, "gpt", "PASS"), marks[("gpt", 3, 2, "J3-1.a")]) == "FAIL"      # 심판 판정과 상관없이 표시 그대로
    assert rl.truth_of(_cell(4, "gpt", "FAIL"), marks[("gpt", 4, 1, "J3-1.a")]) == "PASS"
    assert rl.truth_of(_cell(5, "gpt", "PASS"), marks[("gpt", 5, 1, "J3-1.a")]) is None        # 에러 케이스 → 정답률에서 뺌


def test_apply_proposal_prompt_jev_and_checklist(tmp_path: Path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("# 원칙\n- **J1-4.b** · 본문에서 같은 내용이 두 번\n  · 적용: 모든 질문\n", encoding="utf-8")
    jev = tmp_path / "j.json"
    jev.write_text(json.dumps({"items": {"J1-4.b": {"instructions": "[J1-4.b] 반복 없음 — 같은 내용이 두 번", "criteria": {"FAIL": "아니오 — 같은 내용이 두 번"}}}}), encoding="utf-8")
    chk = tmp_path / "c.json"
    chk.write_text(json.dumps({"questions": {"2": {"turns": [{"turn": 2, "checks": ["사실 하나"], "decided": []}]}}}), encoding="utf-8")
    out = rl.apply_proposal({"target": "판정 프롬프트", "edits": [{"old": "같은 내용이 두 번", "new": "같은 내용이 새 정보 없이 두 번"}]}, tmp_path / "w1", prompt, jev, chk)
    assert "새 정보 없이" in Path(out["prompt"]).read_text(encoding="utf-8") and out["jev_questions"] == jev
    out = rl.apply_proposal({"target": "1차 항목 정의", "edits": [{"item": "J1-4.b", "append": "반복 아님: 표와 그 풀이"}]}, tmp_path / "w2", prompt, jev, chk)
    got = json.loads(Path(out["jev_questions"]).read_text(encoding="utf-8"))["items"]["J1-4.b"]
    assert got["instructions"].endswith("/ 반복 아님: 표와 그 풀이")
    out = rl.apply_proposal({"target": "1차 항목 정의", "edits": [{"item": "J1-4.b", "old": "같은 내용이 두 번", "new": "같은 내용이 새 정보 없이 두 번"}]}, tmp_path / "w3", prompt, jev, chk)
    got = json.loads(Path(out["jev_questions"]).read_text(encoding="utf-8"))["items"]["J1-4.b"]
    assert "새 정보 없이" in got["instructions"] and "새 정보 없이" in got["criteria"]["FAIL"]   # 기준 글도 함께
    out = rl.apply_proposal({"target": "확인 목록", "edits": [{"question": 2, "turn": 2, "add_decided": "번호가 없어도 인용"}]}, tmp_path / "w4", prompt, jev, chk)
    t = json.loads(Path(out["turn_checklists"]).read_text(encoding="utf-8"))["questions"]["2"]["turns"][0]
    assert t["decided"] == ["번호가 없어도 인용"] and t["checks"] == ["사실 하나"]
    with pytest.raises(rl.EditError):   # 확인할 것에는 판정 지시를 못 쓴다
        rl.apply_proposal({"target": "확인 목록", "edits": [{"question": 2, "turn": 2, "add_check": "번호 없으면 FAIL"}]}, tmp_path / "w5", prompt, jev, chk)
    with pytest.raises(rl.EditError):   # old 가 없거나 여러 번이면 거부
        rl.apply_proposal({"target": "판정 프롬프트", "edits": [{"old": "없는 글", "new": "x"}]}, tmp_path / "w6", prompt, jev, chk)
    with pytest.raises(rl.EditError):
        rl.apply_proposal({"target": "기준 문서", "edits": []}, tmp_path / "w7", prompt, jev, chk)


def _probe(decisions, firsts=None, calls=1, secs=10.0):
    out = {}
    for (p, q, t, i), d in decisions.items():
        out[(p, q, t, i)] = {"decision": d, "first": (firsts or {}).get((p, q, t, i), d), "codex": None, "reason": f"{i} 사유", "flagged": ""}
        out[(p, q, 0, "_line")] = {"verdict": "PASS", "codex_calls": calls, "codex_seconds": secs, "seconds": secs, "codex_in": 1000}
    return out


def test_score_and_adoption_with_truth_and_without():
    cases = [{**_cell(1, "gpt", "FAIL"), "truth": "PASS"}, {**_cell(2, "gpt", "FAIL"), "truth": "FAIL"}, {**_cell(3, "gpt", "PASS"), "truth": "PASS"}, _cell(4, "gpt", "PASS")]
    K = lambda q: ("gpt", q, 1, "J1-4.b")
    base1 = _probe({K(1): "FAIL", K(2): "FAIL", K(3): "PASS", K(4): "PASS", ("gpt", 1, 1, "J2-3.a"): "PASS"})
    base2 = _probe({K(1): "PASS", K(2): "FAIL", K(3): "PASS", K(4): "PASS", ("gpt", 1, 1, "J2-3.a"): "PASS"})   # 1번 칸이 회차마다 뒤집힘
    base = rl.score_label(cases, [base1, base2], None)
    assert base["accuracy"] == pytest.approx(2 / 3) and base["truth_n"] == 3 and base["flips"] == 1 and base["n"] == 4
    better1 = _probe({K(1): "PASS", K(2): "FAIL", K(3): "PASS", K(4): "PASS", ("gpt", 1, 1, "J2-3.a"): "PASS"})
    better = rl.score_label(cases, [better1, better1], base1)
    assert better["accuracy"] == 1.0 and better["flips"] == 0 and len(better["changed_vs_base"]) == 1 and better["side_effect"] == 0.0
    assert rl.adoption(base, better)[0] == "채택"
    harm1 = _probe({K(1): "PASS", K(2): "FAIL", K(3): "PASS", K(4): "PASS", ("gpt", 1, 1, "J2-3.a"): "FAIL"})   # 다른 항목이 바뀜
    harm = rl.score_label(cases, [harm1], base1)
    assert harm["side_effect"] == 1.0 and rl.adoption(base, harm)[0] == "기각"
    # 사람 표시 없음 → 1차 ↔ 최종 갈림이 줄면 '사람 확인 필요'
    cases2 = [_cell(1, "gpt", "FAIL"), _cell(2, "gpt", "PASS")]
    b = rl.score_label(cases2, [_probe({K(1): "FAIL", K(2): "PASS"}, firsts={K(1): "PASS"})], None)
    n = rl.score_label(cases2, [_probe({K(1): "PASS", K(2): "PASS"})], _probe({K(1): "FAIL", K(2): "PASS"}, firsts={K(1): "PASS"}))
    assert b["first_final_split"] == 1 and n["first_final_split"] == 0 and rl.adoption(b, n)[0] == "사람 확인 필요"
    assert rl.adoption(b, {"error": "적용 불가"})[0] == "기각"


def test_append_lesson_creates_section_once(tmp_path: Path):
    doc = tmp_path / "v2.md"
    doc.write_text("# 기준\n\n## 11. 바뀐 규칙\n\n| a |\n", encoding="utf-8")
    lesson = {"at": "2026-10-25T12:00:00+00:00", "item": "J1-4.b", "adoption": {"P1": {"판단": "채택", "이유": "정답률 ↑"}},
              "cue": "표 뒤 요약", "lesson": "요구된 구성은 반복 아님", "evidence": "#3 · #5", "confidence": "높음"}
    rl.append_lesson(lesson, doc)
    rl.append_lesson({**lesson, "item": "J2-3.a", "adoption": {}}, doc)
    text = doc.read_text(encoding="utf-8")
    assert text.count(rl.LESSON_HEADING) == 1 and text.count("| 2026-10-25 |") == 2 and "P1 채택" in text and "안 없음" in text


def test_prompt_block_and_doc_row_read_real_files():
    block = rl.prompt_block("J1-4.b")
    assert block.startswith("- **J1-4.b**") and "적용" in block
    assert rl.doc_row("J1-4.b").startswith("| **J1-4.b**")
    assert "[J1-4.b]" in rl.first_judge_definition("J1-4.b")
    assert "지켜야 할 원칙" in rl.principles()


def test_proposal_schema_is_strict_and_lists_targets():
    s = rl.proposal_schema()
    assert s["additionalProperties"] is False and set(s["required"]) == {"keep_base", "notes", "proposals"}
    prop = s["properties"]["proposals"]["items"]
    assert set(prop["required"]) == set(prop["properties"]) and set(prop["properties"]["target"]["enum"]) == set(rl.TARGETS)
    edit = prop["properties"]["edits"]["items"]
    assert set(edit["required"]) == set(edit["properties"])


def test_select_cases_max_lines_keeps_lines_with_most_fail_cells():
    cells = ([_cell(1, "aiona", "FAIL", turn=t) for t in (2, 3, 4)] + [_cell(2, "aiona", "FAIL", turn=2)] + [_cell(3, "aiona", "FAIL", turn=5), _cell(3, "aiona", "FAIL", turn=6)]
             + [_cell(q, "aiona", "PASS", turn=1) for q in (1, 2, 3, 4)])
    got = rl.select_cases(cells, n_fail=6, n_pass=6, max_lines=2)
    assert {(c["provider"], c["question"]) for c in got} == {("aiona", 1), ("aiona", 3)}   # FAIL 칸이 많은 두 대화만
    assert sum(1 for c in got if c["decision"] == "FAIL") == 5 and sum(1 for c in got if c["decision"] == "PASS") == 2


def test_side_effect_counts_only_verdict_relevant_changes_and_uses_base_noise_floor():
    K = lambda q: ("gpt", q, 1, "J1-4.b")
    O = lambda q, i: ("gpt", q, 1, i)
    cases = [_cell(1, "gpt", "FAIL"), _cell(2, "gpt", "PASS")]
    base1 = _probe({K(1): "FAIL", K(2): "PASS", O(1, "J2-3.a"): "PASS", O(1, "J3-1.a"): "NOT_APPLICABLE", O(2, "J2-3.a"): "PASS", O(2, "J3-1.a"): "PASS"})
    base2 = _probe({K(1): "FAIL", K(2): "PASS", O(1, "J2-3.a"): "FAIL", O(1, "J3-1.a"): "PASS", O(2, "J2-3.a"): "PASS", O(2, "J3-1.a"): "PASS"})
    base = rl.score_label(cases, [base1, base2], None)
    assert base["side_total"] == 4 and base["side_effect"] == 0.25 and base["side_effect_all"] == 0.5   # base 자신의 흔들림: FAIL 낀 변화 1 · 전체 2
    prop1 = _probe({K(1): "PASS", K(2): "PASS", O(1, "J2-3.a"): "FAIL", O(1, "J3-1.a"): "PASS", O(2, "J2-3.a"): "PASS", O(2, "J3-1.a"): "PASS"})
    p = rl.score_label(cases, [prop1], base1)
    assert p["side_effect"] == 0.25 and p["side_effect_all"] == 0.5 and len(p["changed_vs_base"]) == 1
    assert rl.adoption(base, p)[0] != "기각"           # base 흔들림(25%)과 같은 수준이면 다른 항목 때문에 기각하지 않음
    base_still = rl.score_label(cases, [base1], None)   # 1회차뿐이면 바닥을 모름 → 고정 5%
    assert rl.adoption(base_still, p)[0] == "기각"


def test_prompt_edit_with_repeated_old_applies_inside_the_item_block(tmp_path: Path):
    """old 가 프롬프트에 여러 번 나오면(상황표 · 항목 정의) 그 항목의 정의 블록 안에서만 바꾼다 (10-25 밤 J2-3.a P1)."""
    prompt = tmp_path / "p.txt"
    prompt.write_text("# 상황표\n- J2-3 조건 (모순 조건 쌍은 J1-3)\n# 점검 항목\n- **J2-3.a** · 조건을 어긴 문장이 없는가\n  · 적용: 조건 (모순 조건 쌍은 J1-3)\n"
                      "- **J2-4.a** · 방식\n  · 적용: (모순 조건 쌍은 J1-3)\n", encoding="utf-8")
    jev = tmp_path / "j.json"
    jev.write_text("{}", encoding="utf-8")
    out = rl.apply_proposal({"target": "판정 프롬프트", "edits": [{"old": "(모순 조건 쌍은 J1-3)", "new": "(모순 조건 쌍과 질문에서 온 충돌은 J1-3)"}]},
                            tmp_path / "w", prompt, jev, None, item="J2-3.a")
    text = Path(out["prompt"]).read_text(encoding="utf-8")
    assert text.count("질문에서 온 충돌") == 1 and text.index("질문에서 온 충돌") > text.index("- **J2-3.a**") and text.index("질문에서 온 충돌") < text.index("- **J2-4.a**")
    with pytest.raises(rl.EditError):   # 항목을 모르면 여전히 거부
        rl.apply_proposal({"target": "판정 프롬프트", "edits": [{"old": "(모순 조건 쌍은 J1-3)", "new": "x"}]}, tmp_path / "w2", prompt, jev, None)



def test_apply_proposal_edits_the_three_judging_stances(tmp_path: Path, monkeypatch):
    """판정 태도 3종(사용자 10-26 저녁) — 태도 하나의 글 · 가중치나 파일 전체(min_choice_prob)를 바꾼 임시 파일을 만들고, 시험 판정은 FIRST_JUDGE_STANCES 로 그 파일을 쓴다."""
    stances = tmp_path / "s.json"
    stances.write_text(json.dumps({"version": "t", "min_choice_prob": 0.6, "stances": [
        {"id": "critical", "name": "비판적", "text": "[비판적] 어긴 곳을 먼저", "weights": {"PASS": 1.2, "FAIL": 0.8, "NOT_APPLICABLE": 1.0}},
        {"id": "friendly", "name": "친화적", "text": "[친화적] 지킨 점을 먼저", "weights": {"PASS": 0.8, "FAIL": 1.2, "NOT_APPLICABLE": 1.0}}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rl.jv, "STANCES_PATH", stances)
    prop = {"target": "판정 태도 3종", "edits": [{"item": "friendly", "old": '"FAIL": 1.2', "new": '"FAIL": 1.1'},
                                              {"item": None, "old": '"min_choice_prob": 0.6', "new": '"min_choice_prob": 0.5'}]}
    out = rl.apply_proposal(prop, tmp_path / "w", tmp_path / "p.txt", tmp_path / "j.json", None)
    got = json.loads(Path(out["stances"]).read_text(encoding="utf-8"))
    assert got["stances"][1]["weights"]["FAIL"] == 1.1 and got["stances"][0]["weights"]["PASS"] == 1.2 and got["min_choice_prob"] == 0.5
    assert json.loads(stances.read_text(encoding="utf-8"))["min_choice_prob"] == 0.6   # 실제 파일은 그대로(시험용 복사본만)
    with pytest.raises(rl.EditError):
        rl.apply_proposal({"target": "판정 태도 3종", "edits": [{"item": "guided", "old": "x", "new": "y"}]}, tmp_path / "w2", tmp_path / "p.txt", tmp_path / "j.json", None)
    assert "판정 태도 3종" in rl.AUTO_TARGETS


def test_auto_reads_human_decisions_new_and_picks_items_with_most_evidence(tmp_path: Path):
    """평가 끝 자동 하네스2 (10-27) — human_decisions.json 의 사람 판정(new)을 정답으로 읽고, 사람 표시 → 보류 → 갈림 → FAIL 순으로 기준을 고른다."""
    f = tmp_path / "human_decisions.json"
    f.write_text(json.dumps({"decisions": [{"provider": "gpt", "question": 2, "turn": 1, "item": "J2-3.a", "new": "PASS"}]}), encoding="utf-8")
    marks = rl.load_marks(f)
    assert rl.truth_of(_cell(2, "gpt", "UNVERIFIED", item="J2-3.a"), marks[("gpt", 2, 1, "J2-3.a")]) == "PASS"
    cells = ([_cell(q, "gpt", "FAIL", item="J1-4.b") for q in range(1, 9)] + [_cell(q, "aiona", "PASS", item="J1-4.b") for q in range(1, 5)]
             + [_cell(q, "gpt", "UNVERIFIED", item="J2-3.a") for q in range(1, 4)] + [_cell(q, "aiona", "PASS", item="J2-3.a") for q in range(1, 5)]
             + [_cell(q, "gpt", "FAIL", item="J5-1.a") for q in range(1, 3)] + [_cell(q, "aiona", "PASS", item="J5-1.a") for q in range(1, 9)]
             + [_cell(1, "gpt", "FAIL", item="2-1.length")] * 5)
    picks = rl.rank_items(cells, marks, 3)
    assert [i for i, _s in picks] == ["J2-3.a", "J1-4.b"]   # 사람 표시가 있는 기준 먼저 · FAIL 이 2칸뿐인 J5-1.a · CODE 는 뺌
    got = rl.select_cases(cells, n_fail=1, n_pass=1, prefer=set(marks))
    assert got[0]["item"] == "J2-3.a" and got[0]["question"] == 2   # 사람 표시 칸이 표본에 먼저


def test_start_runs_refine_after_report_without_asking(monkeypatch, capsys):
    """평가 끝 — 묻지 않고 하네스2(auto)를 부르고, 막혀도 평가는 그대로 끝난다 (사용자 10-27)."""
    from tools import modelgrade as mg
    calls = []
    monkeypatch.setattr(rl, "main", lambda argv: calls.append(argv) or 0)
    mg.run_refine("r1", Path("set"), 2)
    assert calls[0][:3] == ["auto", "--run", "r1"] and calls[0][calls[0].index("--items") + 1] == "2"
    monkeypatch.setattr(rl, "main", lambda argv: (_ for _ in ()).throw(RuntimeError("막힘")))
    mg.run_refine("r1", Path("set"), 2)
    assert "하네스2 멈춤" in capsys.readouterr().out
