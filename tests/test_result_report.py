"""결과 폴더 (result-dirs-v2, 사용자 10-27) — 질문 · 답변 JSON 하나와 그것을 평가한 report.html 하나. 순위는 질문 단위, 보류는 보고서 안에서 정함."""
import json
from collections import Counter
from datetime import datetime


def _D():
    units = [(1, 0), (2, 0), (3, 1), (3, 2)]
    uv = {("aiona", 1, 0): "HOLD", ("aiona", 2, 0): "PASS", ("aiona", 3, 1): "PASS", ("aiona", 3, 2): "PASS",
          ("gpt", 1, 0): "HOLD", ("gpt", 2, 0): "HOLD", ("gpt", 3, 1): "PASS", ("gpt", 3, 2): "FAIL"}
    tally = {p: Counter(v for (pp, _q, _t), v in uv.items() if pp == p) for p in ("aiona", "gpt")}
    item = {"item": "J3-5.a", "decision": "FAIL", "reason": "", "turn": 2, "problems": [], "how": "", "answer_quote": "", "question_quote": "", "kind": ""}
    held = {"item": "J5-2.a", "decision": "UNVERIFIED", "reason": "확인 못 함", "turn": 1, "problems": [], "how": "", "answer_quote": "", "question_quote": "", "kind": "확인 못함"}
    lines = {("aiona", 1): {"verdict": "HOLD", "items": [], "line": {"status": "held"}, "empty_hold": "PASS"},
             ("gpt", 1): {"verdict": "HOLD", "items": [], "line": {"status": "held"}, "empty_hold": "HOLD"},
             ("aiona", 2): {"verdict": "PASS", "items": [], "line": {}}, ("gpt", 2): {"verdict": "HOLD", "items": [held], "line": {"status": "judged"}},
             ("aiona", 3): {"verdict": "PASS", "items": [], "line": {}}, ("gpt", 3): {"verdict": "FAIL", "items": [item], "line": {}}}
    qtally = {p: Counter(L["verdict"] for (pp, _q), L in lines.items() if pp == p) for p in ("aiona", "gpt")}
    qset = {1: {"turns": [{"turn": 1, "question": "아무것도 출력하지 마"}]}, 2: {"turns": [{"turn": 1, "question": "긴 질문 " * 100}]},
            3: {"turns": [{"turn": 1, "question": "첫 턴"}, {"turn": 2, "question": "둘째 턴"}]}}
    turns = {(p, q, 1): {"answer": "" if q == 1 else "답", "status": "SUCCESS"} for p in ("aiona", "gpt") for q in (1, 2, 3)}
    return {"d": {"models": {}}, "run_id": "r1", "order": ["aiona", "gpt"], "tally": tally, "qtally": qtally, "n": 4, "nq": 3, "units": units,
            "utype": {(1, 0): "싱글턴", (2, 0): "싱글턴", (3, 1): "멀티턴", (3, 2): "멀티턴"}, "uverdict": uv, "qset": qset, "turns": turns,
            "qtypes": {1: "명시적 빈 출력", 2: "단일 질문", 3: "10턴 멀티턴 대화"}, "names": {"J5-2.a": {"name": "수치 정확성", "group": "J5"}},
            "lines": lines}


def test_report_ranks_by_questions_and_decides_holds_in_place():
    from tools import build_result_summary as brs
    D = _D()
    rank = brs.rank_card(D)
    assert "질문 3개 중" in rank and "<b>2</b><small> / 3</small>" in rank   # AIONA 2 / 3 질문 (사용자 10-27)
    assert "빈 출력 요구 1 포함" in rank and "<b>3</b><small> / 4</small>" in rank   # 문항 단위는 같은 공간의 둘째 버튼 (10-27)
    assert '<label for="rk0">질문 단위</label>' in rank and '<label for="rk1">문항 단위</label>' in rank
    assert "잠정" not in rank and "ds-ln" not in rank   # 보류도 평가 결과 — '잠정' · 점선 없음 (사용자 10-27)
    D["qstore"] = {}
    sec = brs.decide_section(D, brs.decide_blocks(D))
    assert 'id="hold"' in sec and 'id="finish"' in sec and 'data-key="gpt|2|1|J5-2.a"' in sec   # 보류 기준마다 단추
    assert "data-v='ERROR_CASE'" in sec and "NOT_APPLICABLE" not in sec and "사유(선택)" in sec   # PASS · FAIL · Error + 사유는 선택 (10-27)
    assert "<b>왜 보류</b> — 확인 못 함" in sec and "<b>어디를 보고</b>" in sec   # 어디를 보고 · 왜 보류
    assert "__question__" not in sec and "빈 출력 요구 — 순위에서 늘 보류로 세는 칸 2개" in sec   # 빈 출력 요구는 단추 없이 접어서
    assert 'data-qt="2-1"' in sec and D["qstore"]["2-1"].startswith("긴 질문")   # 긴 질문 글은 쪽 끝 묶음에 한 번
    fails = brs.scene_fail_list(D)
    assert 'href="#f-gpt-3"' in fails and "턴 2 · 답의 특정 문장을 짚지 않음" in fails and "FAIL 질문 1개 · 어긴 기준 1건" in fails   # FAIL 목록


def test_result_folder_is_found_by_its_json_and_skips_reruns(tmp_path):
    from tools import result_dirs
    main = result_dirs.ensure({"run_id": "r1"}, tmp_path, now=datetime(2026, 10, 27, 7, 0))
    assert not any(main.iterdir())   # 판정 전에는 빈 폴더 — run.json 같은 다른 파일 없음
    assert result_dirs.find("r1", tmp_path) is None
    (main / "r1_결과_판정결과.json").write_text(json.dumps({"format": "x", "run_id": "r1"}), encoding="utf-8")
    rerun = tmp_path / "2026-10-27_09-00-00-000"
    rerun.mkdir()
    (rerun / "r1_결과_판정결과.json").write_text(json.dumps({"run_id": "r1", "result": {"kind": "rerun", "rerun_of": "r1"}}), encoding="utf-8")
    assert result_dirs.find("r1", tmp_path) == main and [r for r, _d in result_dirs.runs(tmp_path)] == ["r1"]


def test_export_puts_questions_answers_and_human_decisions_in_one_json(tmp_path):
    from tools.export_results_json import export
    set_dir = tmp_path / "set"
    set_dir.mkdir()
    qs = [{"id": "1", "type": "단일 질문", "turns": [{"turn": 1, "question": "물음", "attachments": [{"name": "a.pdf"}]}]},
          {"id": "2", "type": "단일 질문", "turns": [{"turn": 1, "question": "안 받은 질문"}]}]
    (set_dir / "questions.json").write_text(json.dumps(qs, ensure_ascii=False), encoding="utf-8")
    runs, jd = tmp_path / "runs", tmp_path / "jd"
    runs.mkdir(), jd.mkdir()
    run = {"run_id": "r1", "question_set": {"dir": str(set_dir)}, "models": {"gpt": "ChatGPT"},
           "datasets": {"gpt": [{"case_id": "Q1_T1", "answer": "답입니다.", "collection": {"status": "SUCCESS"}}]}}
    (runs / "r1.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    (jd / "results.jsonl").write_text(json.dumps({"provider": "gpt", "question": 1, "verdict": "PASS"}) + "\n", encoding="utf-8")
    (jd / "human_decisions.json").write_text(json.dumps({"decisions": [{"provider": "gpt", "question": 1, "turn": 1, "item": "J1-1.a", "new": "PASS"}]}),
                                            encoding="utf-8")
    out = export("r1", "결과_판정결과", out_dir=tmp_path / "out", runs_dir=runs, judge_dir=jd, result={"kind": "rerun", "rerun_of": "r1"})
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["questions"] == [{"id": 1, "type": "단일 질문", "turns": [{"turn": 1, "question": "물음", "attachments": ["a.pdf"]}]}]   # 받은 질문만
    assert doc["providers"]["gpt"]["turns"][0]["answer"] == "답입니다." and doc["human_decisions"][0]["new"] == "PASS"
    assert '"rerun_of"' in out.read_text(encoding="utf-8")[:4096]   # 다시 판정 표시는 머리에 (result_dirs 가 앞부분만 읽음)
