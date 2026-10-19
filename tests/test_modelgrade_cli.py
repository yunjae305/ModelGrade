"""modelgrade CLI — collect import (웹브라우저 답변 CSV → 실행 JSON + CODE 평가) · judge 묶음 크기 규칙."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import modelgrade as mg


def _set(tmp_path: Path) -> Path:
    d = tmp_path / "set"; d.mkdir()
    groups = [{"id": 1, "topic": "인사", "turns": [{"turn": 1, "question": "정확히 2개의 문장으로 인사말을 써줘. 존댓말로 답해줘."}]},
              {"id": 2, "topic": "대화", "turns": [{"turn": 1, "question": "가게 이름을 하나 정해줘."}, {"turn": 2, "question": "아까 정한 이름으로 간판 문구를 써줘."}]},
              {"id": 3, "topic": "빈 출력", "turns": [{"turn": 1, "question": "아무것도 출력하지 마. 공백 포함 정확히 0자로 유지해."}]}]
    (d / "questions.json").write_text(json.dumps(groups, ensure_ascii=False), encoding="utf-8")
    (d / "set.json").write_text(json.dumps({"id": "qs-test"}), encoding="utf-8")
    return d


def _csv(tmp_path: Path) -> Path:
    p = tmp_path / "answers_claude.csv"
    p.write_text("case_id,answer,chat_url,note\n"
                 "Q1_T1,\"안녕하세요. 반갑습니다.\",https://claude.ai/chat/1,\n"
                 "Q2_T1,\"가게 이름은 '달빛상회'로 하겠습니다.\",,\n"
                 "Q2_T2,,,오류 화면\n"
                 "Q3_T1,,,빈 답\n"
                 "Q9_T1,엉뚱한 줄,,\n", encoding="utf-8")
    return p


def test_collect_import_builds_run_with_collection_status_and_code_eval(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    assert mg.main(["collect", "import", "--provider", "claude", "--csv", str(_csv(tmp_path)), "--set", str(_set(tmp_path)), "--new", "시험"]) == 0
    out = capsys.readouterr().out
    run = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
    rows = {r["case_id"]: r for r in run["datasets"]["claude"]}
    assert list(rows) == ["Q1_T1", "Q2_T1", "Q2_T2", "Q3_T1"]            # 질문 세트 순서, 세트에 없는 Q9 는 무시
    assert rows["Q1_T1"]["collection"]["status"] == "SUCCESS" and rows["Q1_T1"]["collection"]["url"] == "https://claude.ai/chat/1"
    assert rows["Q2_T2"]["collection"]["status"] == "ERROR" and "오류" in rows["Q2_T2"]["collection"]["error"]   # 답 없음 → 판정에서 FAIL
    assert rows["Q3_T1"]["collection"]["status"] == "SUCCESS" and rows["Q3_T1"]["answer"] == ""                 # 빈 답은 받은 답(빈 출력 요구 → 보류)
    assert rows["Q2_T2"]["conversation_id"] and rows["Q2_T2"]["turn"] == 2
    assert run["providers"] == ["claude"] and run["models"]["claude"] == "Claude 웹브라우저"
    cases = {c["case_id"]: c for c in run["evaluations"]["claude"]["cases"]}
    assert set(cases) == {"Q1_T1", "Q2_T1", "Q3_T1"}                       # CODE 평가는 답을 받은 행만
    assert run["evaluations"]["claude"]["run"]["skipped_no_answer"] == 1
    assert "질문 세트에 없는 case_id 1개" in out and "답 있음 3 · 답 없음 1" in out


def test_collect_import_into_existing_run_keeps_other_providers(tmp_path, monkeypatch):
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "abc.json").write_text(json.dumps({"run_id": "abc", "providers": ["aiona"], "models": {"aiona": "AIONA"},
                                                             "datasets": {"aiona": [{"case_id": "Q1_T1", "answer": "x"}]}, "evaluations": {}}), encoding="utf-8")
    assert mg.main(["collect", "import", "--provider", "gemini", "--csv", str(_csv(tmp_path)), "--set", str(_set(tmp_path)), "--run", "abc"]) == 0
    run = json.loads((tmp_path / "runs" / "abc.json").read_text(encoding="utf-8"))
    assert run["providers"] == ["aiona", "gemini"] and run["datasets"]["aiona"][0]["answer"] == "x"
    assert len(run["datasets"]["gemini"]) == 4 and run["imports"][0]["provider"] == "gemini"
    assert list((tmp_path / "runs").glob("abc.json.bak-*"))                 # 덮어쓰기 전 백업


def test_judge_refuses_more_than_twenty_questions_at_once(tmp_path):
    with pytest.raises(SystemExit, match="20개까지"):
        mg.main(["judge", "--run", "abc", "--only", ",".join(str(i) for i in range(1, 22))])
    with pytest.raises(SystemExit, match="--only"):
        mg.main(["judge", "--run", "abc"])


def test_next_questions_skips_judged_ones_and_caps_batch(tmp_path):
    run = {"question_count": 6, "providers": ["aiona", "gpt"], "datasets": {}}
    out = tmp_path / "out"; out.mkdir()
    (out / "results.jsonl").write_text('{"provider":"aiona","question":1}\n{"provider":"gpt","question":1}\n{"provider":"aiona","question":2}\n', encoding="utf-8")
    picked, pending = mg.next_questions(run, ["aiona", "gpt"], out, 3)
    assert picked == [2, 3, 4] and pending == 5          # 1번은 두 모델 다 끝남, 2번은 gpt 가 남음
    picked, pending = mg.next_questions(run, ["aiona"], out, 20)
    assert picked == [3, 4, 5, 6] and pending == 4
