# -*- coding: utf-8 -*-
"""tools/judge_compare_runs — 같은 실행의 두 판정 폴더 전후 비교 (10-22 밤)."""
import json
from pathlib import Path

from tools import judge_compare_runs as jcr


def _line(provider, q, verdict, items, turns=1, code_fails=None):
    return {"provider": provider, "question": q, "turns": turns, "verdict": verdict, "code_fails": code_fails or [],
            "calls": {"codex": {"rounds": 1, "turn_asks": {"1": 2}}}, "turn_results": {"1": {"items": items}}}   # turn_asks 값은 횟수(정수)


def _write(path: Path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")


def test_compare_counts_changed_questions_and_item_deltas(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(jcr, "ROOT", tmp_path)
    before = [_line("gpt", 1, "FAIL", {"J2-3.a": {"decision": "FAIL"}, "J1-4.a": {"decision": "PASS"}}),
              _line("gpt", 2, "HOLD", {"J5-1.a": {"decision": "UNVERIFIED", "kind": "확인 못함"}}),
              _line("gpt", 1, "PASS", {"J2-3.a": {"decision": "PASS"}})]   # 같은 (모델, 질문)은 마지막 줄만 → Q1 은 PASS 가 전
    after = [_line("gpt", 1, "PASS", {"J2-3.a": {"decision": "PASS"}, "J1-4.a": {"decision": "PASS"}}),
             _line("gpt", 2, "FAIL", {"J5-1.a": {"decision": "FAIL"}})]
    _write(tmp_path / "data" / "judge_runs" / "r" / "a" / "results.jsonl", before)
    _write(tmp_path / "data" / "judge_runs" / "r" / "b" / "results.jsonl", after)
    out = jcr.write_compare("r", "a", "b")
    c = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert c["verdicts"]["gpt"]["전"] == {"PASS": 1, "HOLD": 1} and c["verdicts"]["gpt"]["후"] == {"PASS": 1, "FAIL": 1}
    assert [(x["question"], x["전"], x["후"]) for x in c["changed"]] == [(2, "HOLD", "FAIL")]
    assert c["changed"][0]["FAIL 생김"] == ["T1:J5-1.a"] and c["changed"][0]["보류 사라짐"] == ["T1:J5-1.a"]
    assert c["fail_by_item"]["후"] == {"J5-1.a": 1} and c["hold_by_kind"]["전"] == {"확인 못함": 1}
    md = out.read_text(encoding="utf-8")
    assert "| gpt | 1 → 1 | 0 → 1 | 1 → 0 |" in md and "Q2 | gpt | 보류 → FAIL" in md


def test_compare_identity_has_no_changes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(jcr, "ROOT", tmp_path)
    lines = [_line("claude", 5, "PASS", {"J1-4.b": {"decision": "PASS"}}, turns=2)]
    for name in ("a", "b"):
        _write(tmp_path / "data" / "judge_runs" / "r" / name / "results.jsonl", lines)
    c = jcr.compare(jcr.last_lines(tmp_path / "data/judge_runs/r/a/results.jsonl"), jcr.last_lines(tmp_path / "data/judge_runs/r/b/results.jsonl"))
    assert c["changed"] == [] and c["turn_fail"]["claude"] == {"전": [0, 1], "후": [0, 1]} and c["turn_pass"]["claude"] == {"전": [1, 1], "후": [1, 1]}
