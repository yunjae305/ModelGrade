"""보류·오류 추출(judge-held-v1) — 판정 결과 results.jsonl 의 보류 항목·오류를 held.csv 하나로 (체크리스트 t6). 호출 없음."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from tools import export_held as eh
from tools import modelgrade as mg

LONG = "가" * 300


def _set(tmp_path: Path) -> Path:
    d = tmp_path / "set"; d.mkdir()
    groups = [{"id": 1, "turns": [{"turn": 1, "question": "물이 끓는 온도를 알려줘."}]},
              {"id": 2, "turns": [{"turn": 1, "question": "가게 이름을 정해줘."}, {"turn": 2, "question": "그 이름으로 간판 문구를 써줘."}]},
              {"id": 3, "turns": [{"turn": 1, "question": "아무것도 출력하지 마."}]}]
    (d / "questions.json").write_text(json.dumps(groups, ensure_ascii=False), encoding="utf-8")
    return d


def _judge_dir(tmp_path: Path) -> Path:
    d = tmp_path / "judge_runs" / "run1"; d.mkdir(parents=True)
    split = {"decision": "UNVERIFIED", "judge": "jev+gpt", "jev": {"decision": "FAIL", "probabilities": {"FAIL": 0.81}},
             "entries": [{"reason": "조건을 지킴", "answer_quote": "100도"}], "problems": ["심판 갈림: Jev FAIL(확률 0.81) · GPT PASS — 사람이 확인"]}
    quote = {"decision": "UNVERIFIED", "judge": "gpt", "entries": [{"reason": "출처 없음"}], "problems": ["J5-3.a: answer_quote 가 답변 원문에 없음"]}
    lines = [
        {"provider": "gpt", "question": 1, "status": "judged", "verdict": "HOLD", "calls": {"jev": {"status": "OK"}, "gpt": {"status": "OK"}},
         "results": {"1": {"J1-1.a": {"decision": "PASS"}, "J2-3.a": split, "J5-3.a": quote}}},
        {"provider": "claude", "question": 1, "status": "judged", "verdict": "FAIL", "calls": {"jev": {"status": "OK"}},
         "results": {"1": {"J1-1.a": {"decision": "FAIL"}}}},   # FAIL 만 — 줄 없음
        {"provider": "aiona", "question": 1, "status": "judged", "verdict": "HOLD",
         "calls": {"jev": {"status": "ERROR", "error": "HTTP 400"}, "gpt_fallback": {"status": "OK"}, "jev_flagged": {}},
         "results": {"1": {"J5-1.a": {"decision": "UNVERIFIED", "judge": "gpt", "entries": [{}], "problems": ["GPT CLI 실패: 429"]}}}},
        {"provider": "claude", "question": 2, "status": "held_copy_failed", "verdict": "HOLD", "reason": "복사 실패 턴 Q2_T2", "collection_note": "Q2_T2: COPY_FAILED"},
        {"provider": "gemini", "question": 2, "status": "no_answer", "verdict": "FAIL", "reason": "답을 받지 못한 턴 Q2_T2"},
        {"provider": "aiona", "question": 3, "status": "held_empty_output", "verdict": "HOLD", "reason": "빈 출력 요구 — 복사할 답 없음"},
    ]
    (d / "results.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")
    return d


def _runs(tmp_path: Path) -> Path:
    d = tmp_path / "runs"; d.mkdir()
    run = {"run_id": "run1", "datasets": {"gpt": [{"case_id": "Q1_T1", "answer": LONG}], "claude": [{"case_id": "Q2_T1", "answer": "가게"}]}}
    (d / "run1.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    return d


def test_held_rows_cover_item_and_question_level_holds_and_errors(tmp_path):
    path, rows, summary = eh.export(_judge_dir(tmp_path), set_dir=_set(tmp_path), runs_dir=_runs(tmp_path), results_dir=tmp_path / "none")
    by = {(r["system"], r["question"], r["item"]): r for r in rows}
    assert len(rows) == 7   # gpt 항목 2 · aiona Jev 실패 1 + Codex 실패 항목 1 · claude 복사 실패 · gemini 답 없음 · aiona 빈 출력
    assert by[("gpt", 1, "J2-3.a")]["cause"] == "judge_disagreement" and by[("gpt", 1, "J2-3.a")]["jev"] == "FAIL (FAIL 확률 0.81)"
    assert by[("gpt", 1, "J5-3.a")]["cause"] == "quote_mismatch" and by[("gpt", 1, "J5-3.a")]["kind"] == "보류"
    assert by[("aiona", 1, "J1~J4")]["kind"] == "오류" and "Codex 가 J1~J4 대신" in by[("aiona", 1, "J1~J4")]["cause_label"]
    assert by[("aiona", 1, "J5-1.a")]["cause"] == "call_error" and by[("aiona", 1, "J5-1.a")]["kind"] == "오류"
    assert by[("claude", 2, "")]["cause"] == "copy_failed" and by[("gemini", 2, "")]["kind"] == "오류"
    assert by[("aiona", 3, "")]["cause"] == "empty_output"
    assert not any(r["system"] == "claude" and r["question"] == 1 for r in rows)   # FAIL 만 있는 질문은 내지 않음
    assert summary["aiona"]["call_error"] == 2 and summary["gpt"]["judge_disagreement"] == 1
    gpt = by[("gpt", 1, "J2-3.a")]
    assert gpt["question_text"] == "물이 끓는 온도를 알려줘." and gpt["answer"].startswith("가" * 200) and "300자 중 200자" in gpt["answer"]
    with path.open(encoding="utf-8-sig", newline="") as f:
        saved = list(csv.DictReader(f))
    assert tuple(saved[0]) == eh.COLUMNS and len(saved) == 7 and all(r["gold"] == "" for r in saved)


def test_answers_fall_back_to_repo_results_json_and_cli_command(tmp_path, capsys, monkeypatch):
    results = tmp_path / "결과"; results.mkdir()
    (results / "run1_간단78.json").write_text(json.dumps({"providers": {"gpt": {"turns": [{"case_id": "Q1_T1", "answer": "100도에서 끓습니다."}]}}},
                                                     ensure_ascii=False), encoding="utf-8")
    assert eh.load_answers("run1", tmp_path / "no-runs", results) == {"gpt": {"Q1_T1": "100도에서 끓습니다."}}
    monkeypatch.setattr(eh, "DEFAULT_SET", _set(tmp_path))
    monkeypatch.setattr(eh, "RUNS", tmp_path / "no-runs")
    monkeypatch.setattr(eh, "RESULTS_DIR", results)
    judge = _judge_dir(tmp_path)
    assert mg.main(["held", "--dir", str(judge), "--text", "none"]) == 0
    out = capsys.readouterr().out
    assert "보류 4줄 · 오류 3줄" in out and "심판 갈림" in out
    saved = list(csv.DictReader((judge / "held.csv").open(encoding="utf-8-sig", newline="")))
    assert all(r["text_scope"] == "none" for r in saved)
