"""modelgrade CLI — collect import (웹브라우저 답변 CSV → 실행 JSON + CODE 평가) · judge 묶음 크기 규칙."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import modelgrade as mg



def _runs(tmp_path):
    """실행 JSON 만 (작은 색인 .meta.json 은 뺀다)."""
    return [f for f in sorted((tmp_path / "runs").glob("*.json")) if not f.name.endswith(".meta.json")]

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
    monkeypatch.setattr(mg, "RESULTS_JSON_DIR", tmp_path / "결과")
    assert mg.main(["collect", "import", "--provider", "claude", "--csv", str(_csv(tmp_path)), "--set", str(_set(tmp_path)), "--new", "시험"]) == 0
    out = capsys.readouterr().out
    run = json.loads(_runs(tmp_path)[0].read_text(encoding="utf-8"))
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
    monkeypatch.setattr(mg, "RESULTS_JSON_DIR", tmp_path / "결과")
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


def test_collect_app_collects_turns_in_order_skips_done_and_code_evaluates(tmp_path, monkeypatch, capsys):
    """웹 수집 — 대화는 새 채팅 하나에서 턴 순서대로, 이미 받은 질문은 건너뜀, 받은 행은 CODE 평가."""
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(mg, "RESULTS_JSON_DIR", tmp_path / "결과")
    calls = []

    class FakeChat:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def new_chat(self): calls.append("new")
        def ask(self, question, files=None, reference=None):
            calls.append(("ask", question[:10], [f.name for f in files or []]))
            return {"answer": "답입니다. 두 문장입니다.", "status": "SUCCESS", "capture": "copy-button", "latency_ms": 1200, "model": "GPT-6 · Medium (앱)", "method": "chatgpt-app-v1", "error": None, "url": None, "sent": []}
    monkeypatch.setattr(mg, "web_chat_class", lambda provider: FakeChat)
    set_dir = _set(tmp_path)
    assert mg.main(["collect", "app", "--provider", "gpt", "--set", str(set_dir), "--only", "2,1", "--new", "앱 시험"]) == 0
    assert calls == ["new", ("ask", "가게 이름을 하나 정해줘."[:10], []), ("ask", "아까 정한 이름으로 간판 문구를 써줘."[:10], []), "new", ("ask", "정확히 2개의 문장으로 인사말을 써줘. 존댓말로 답해줘."[:10], [])]
    run = json.loads(_runs(tmp_path)[0].read_text(encoding="utf-8"))
    rows = {r["case_id"]: r for r in run["datasets"]["gpt"]}
    assert [r["case_id"] for r in run["datasets"]["gpt"]] == ["Q1_T1", "Q2_T1", "Q2_T2", "Q3_T1"]
    assert rows["Q2_T2"]["collection"]["status"] == "SUCCESS" and rows["Q2_T2"]["turn"] == 2
    assert rows["Q3_T1"]["collection"]["status"] == "ERROR"            # 아직 안 받음 → 판정에서 FAIL 이 아니라 다음 묶음 대상
    assert {c["case_id"] for c in run["evaluations"]["gpt"]["cases"]} == {"Q1_T1", "Q2_T1", "Q2_T2"}
    calls.clear()
    run_id = run["run_id"]
    assert mg.main(["collect", "app", "--provider", "gpt", "--set", str(set_dir), "--run", run_id, "--next", "20"]) == 0
    assert calls == ["new", ("ask", "아무것도 출력하지 마. 공백 포함 정확히 0자로 유지해."[:10], [])]            # 1·2번은 받았으니 3번만
    out = capsys.readouterr().out
    assert "이미 받은 질문 2개" in out


def test_collect_app_runs_apps_together_on_one_run_and_claude_maps_to_uia_collector(tmp_path, monkeypatch):
    """--provider gpt,claude: 실행을 먼저 만들고 앱마다 자식 프로세스로 동시에(시험에서는 같은 프로세스에서 차례로) — 둘 다 같은 실행에 자기 자리만 쓴다.
    claude 는 웹 수집기(ClaudeChat)."""
    import argparse
    import tools.modelgrade as mg
    monkeypatch.setattr(mg, "ensure_collection_chrome", lambda: None, raising=False)
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(mg, "RESULTS_JSON_DIR", tmp_path / "결과")
    seen = []
    places = []

    def fake_parallel(providers, args, run_id):
        for i, provider in enumerate(providers):
            places.append(mg.PLACES[i % 2])
            assert mg.collect_app_one(argparse.Namespace(**{**vars(args), "provider": provider, "run": run_id, "place": mg.PLACES[i % 2]})) == 0
        return 0
    monkeypatch.setattr(mg, "run_collectors_parallel", fake_parallel)

    def fake_cls(provider):
        class FakeChat:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): return None
            def new_chat(self): pass
            def ask(self, question, files=None, reference=None):
                seen.append(provider)
                return {"answer": f"{provider} 답입니다.", "status": "SUCCESS", "capture": "copy-button", "latency_ms": 10, "model": provider, "method": "x", "error": None, "url": None, "sent": []}
        return FakeChat
    monkeypatch.setattr(mg, "web_chat_class", fake_cls)
    set_dir = _set(tmp_path)
    assert mg.main(["collect", "app", "--provider", "gpt,claude", "--set", str(set_dir), "--only", "1", "--new", "두 앱"]) == 0
    runs = list(_runs(tmp_path))
    assert len(runs) == 1 and seen == ["gpt", "claude"] and places == ["left", "right"]
    run = json.loads(runs[0].read_text(encoding="utf-8"))
    assert run["datasets"]["gpt"][0]["answer"] == "gpt 답입니다." and run["datasets"]["claude"][0]["answer"] == "claude 답입니다."
    assert run["models"] == {"gpt": "ChatGPT 웹 (GPT-6 · Medium)", "claude": "Claude 웹 (Sonnet 5.5 · 노력 중간)"}
    assert not list((tmp_path / "runs").glob("*.lock"))
    with pytest.raises(SystemExit):
        mg.main(["collect", "app", "--provider", "factchat", "--set", str(set_dir), "--only", "1"])
    monkeypatch.undo()
    assert mg.web_chat_class("claude").__name__ == "ClaudeChat"


def test_merge_provider_into_run_keeps_other_providers_written_meanwhile(tmp_path, monkeypatch):
    """동시 수집: 두 자식이 같은 실행 파일에 쓴다 — 잠금 아래 다시 읽어 자기 모델 자리만 바꾸므로 먼저 쓴 쪽이 안 지워진다."""
    import tools.modelgrade as mg
    monkeypatch.setattr(mg, "ensure_collection_chrome", lambda: None, raising=False)
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(mg, "RESULTS_JSON_DIR", tmp_path / "결과")
    set_dir = _set(tmp_path)
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    run = mg.new_run("동시", set_dir, groups)
    mg.save_run(run)
    row = lambda p: [{"case_id": "Q1_T1", "question": "q", "answer": f"{p} 답", "system_id": p, "source": p, "case_index": 0, "collection": {"status": "SUCCESS"}}]
    mg.merge_provider_into_run(run["run_id"], "gpt", "GPT 앱", row("gpt"), {"cases": []}, {"provider": "gpt"})
    mg.merge_provider_into_run(run["run_id"], "claude", "Claude 앱", row("claude"), {"cases": []}, {"provider": "claude"})
    saved = mg.load_run(run["run_id"])
    assert saved["providers"] == ["gpt", "claude"] and saved["datasets"]["gpt"][0]["answer"] == "gpt 답" and saved["datasets"]["claude"][0]["answer"] == "claude 답"
    assert [e["provider"] for e in saved["imports"]] == ["gpt", "claude"]
    (tmp_path / "runs" / f"{run['run_id']}.lock").write_text("")   # 남은 잠금은 기다리다 오류
    monkeypatch.setattr(mg.time, "monotonic", lambda c=[0]: c.__setitem__(0, c[0] + 100) or c[0])
    with pytest.raises(SystemExit):
        mg.merge_provider_into_run(run["run_id"], "gpt", "GPT 앱", row("gpt"), {"cases": []}, {"provider": "gpt"})


def test_start_collects_all_set_questions_in_set_order_then_judges_v4_and_reuses_run(tmp_path, monkeypatch, capsys):
    """start: 세트의 모든 질문을 세트 순서 그대로(질문을 다시 검사·선별하지 않음), 네 모델 동시 수집(이미 받은 질문 건너뜀) → 판정 v4 → 결과.
    다시 부르면 같은 실행. (10-20: 질문 78개 고정, 판정상황표 없음)"""
    import tools.modelgrade as mg
    monkeypatch.setattr(mg, "ensure_collection_chrome", lambda: None, raising=False)
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(mg, "RESULTS_JSON_DIR", tmp_path / "결과")
    monkeypatch.setattr(mg, "ROOT", tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "" if "Enter = 0" in prompt else "1")   # 평가 종류 1 · 모델은 Enter(전체)
    calls = {"collect": [], "judge": []}

    def fake_parallel(providers, args, run_id):
        calls["collect"].append((providers, args.only, args.skip_done, args.allow_large, run_id))
        return 0

    def fake_judge_main(argv):
        calls["judge"].append(argv)
        return 0
    monkeypatch.setattr(mg, "run_collectors_parallel", fake_parallel)
    import tools.judge_v4 as jv
    monkeypatch.setattr(jv, "main", fake_judge_main)
    set_dir = _set(tmp_path)
    assert mg.main(["start", "--set", str(set_dir), "--judge", "y"]) == 0
    run_id = calls["collect"][0][4]
    assert calls["collect"] == [(mg.ALL_PROVIDERS, "1,2,3", True, True, run_id)]   # 세트의 질문 전부, 세트 순서
    assert len(calls["judge"]) == 1 and "--questions" in calls["judge"][0] and calls["judge"][0][calls["judge"][0].index("--questions") + 1] == "1,2,3"
    assert "--set" in calls["judge"][0] and calls["judge"][0][calls["judge"][0].index("--set") + 1] == str(set_dir)
    run = mg.load_run(run_id)
    assert run["eval_kind"] == "set78" and "78" in run["name"]
    out = capsys.readouterr().out
    assert "어떤 평가를 진행할까요?" in out and "질문 3개" in out
    assert mg.main(["start", "--set", str(set_dir), "--choice", "1", "--judge", "y"]) == 0   # 다시 부르면 같은 실행
    assert calls["collect"][1][4] == run_id


def test_start_with_one_model_collects_only_that_app_and_aiona_in_its_own_run(tmp_path, monkeypatch):
    """start --model: 고른 앱 모델 하나(그 앱을 그 모델로)와 AIONA 만 받고, 고른 모델은 실행에 적어 전체 실행과 섞지 않는다 (10-22)."""
    import tools.modelgrade as mg
    monkeypatch.setattr(mg, "ensure_collection_chrome", lambda: None, raising=False)
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(mg, "RESULTS_JSON_DIR", tmp_path / "결과")
    monkeypatch.setattr(mg, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(mg, "run_collectors_parallel", lambda providers, args, run_id: calls.append((providers, args.models, run_id)) or 0)
    set_dir = _set(tmp_path)
    assert mg.main(["start", "--set", str(set_dir), "--choice", "1", "--providers", "gpt,claude,gemini,aiona", "--judge", "n"]) == 0
    assert mg.main(["start", "--set", str(set_dir), "--choice", "1", "--model", "claude:Opus 5.5", "--judge", "n"]) == 0
    assert calls[1][:2] == (["claude", "aiona"], {"claude": "Opus 5.5", "aiona": "Claude Opus 5.5"})   # AIONA 도 같은 모델로
    picks = {(p, m) for _k, p, m in mg.model_menu()}   # 앱과 AIONA 에 이름이 똑같은 모델만 (10-22)
    assert len(picks) == 18 and mg.aiona_model_for("gemini", "3.1 Pro") == "Gemini 3.1 Pro Preview"
    assert mg.aiona_model_for("gpt", "GPT-6") == "GPT-6 Sol" and mg.aiona_model_for("gpt", "GPT-5.6 Sol") == "GPT-5.6 Sol"   # 사용자 10-22
    for bad in ("gemini:3.6 Flash", "Sonnet 9"):
        with pytest.raises(SystemExit):
            mg.parse_model(bad)
    assert calls[1][2] != calls[0][2] and mg.load_run(calls[1][2])["model_choice"] == "claude:Opus 5.5"
    import re, json
    run = mg.load_run(calls[1][2])   # 결과 폴더 = 참고자료/결과/<처음 시작한 시각, ms 까지>/ · run.json 으로 실행과 이음 (10-22)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{3}", run["result_dir"])
    assert json.loads((tmp_path / "결과" / run["result_dir"] / "run.json").read_text(encoding="utf-8"))["run_id"] == calls[1][2]
    assert mg.main(["start", "--set", str(set_dir), "--choice", "1", "--model", "claude:Opus 5.5", "--judge", "n"]) == 0   # 이어 돌리기
    assert calls[2][2] == calls[1][2] and mg.load_run(calls[2][2])["result_dir"] == run["result_dir"]   # 처음 폴더 그대로
    assert mg.parse_model("GPT-5.6 Sol") == ("gpt", "GPT-5.6 Sol") and mg.parse_model("0") is None


def test_collect_app_only_with_skip_done_leaves_out_answered_questions(tmp_path, monkeypatch):
    import tools.modelgrade as mg
    monkeypatch.setattr(mg, "ensure_collection_chrome", lambda: None, raising=False)
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(mg, "RESULTS_JSON_DIR", tmp_path / "결과")
    asked = []

    class FakeChat:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def new_chat(self): pass
        def ask(self, question, files=None, reference=None):
            asked.append(question[:6])
            return {"answer": "답.", "status": "SUCCESS", "capture": "copy-button", "latency_ms": 1, "model": "m", "method": "x", "error": None, "url": None, "sent": []}
    monkeypatch.setattr(mg, "web_chat_class", lambda provider: FakeChat)
    set_dir = _set(tmp_path)
    assert mg.main(["collect", "app", "--provider", "gpt", "--set", str(set_dir), "--only", "1", "--new", "x"]) == 0
    run_id = _runs(tmp_path)[0].stem
    asked.clear()
    assert mg.main(["collect", "app", "--provider", "gpt", "--set", str(set_dir), "--only", "1,3", "--run", run_id, "--skip-done"]) == 0
    assert asked == ["아무것도 출력"[:6]]


def test_place_rect_puts_apps_left_right_and_web_windows_in_right_column():
    from webapp.cdp_chat_web import place_rect
    assert place_rect("left", 2880, 1800) == (0, 0, 1728, 1800)
    assert place_rect("right", 2880, 1800) == (1152, 0, 1728, 1800)
    assert place_rect("top-right", 1440, 900) == (864, 0, 576, 450)
    assert place_rect("bottom-right", 1440, 900) == (864, 450, 576, 450)


def test_report_writes_html_with_model_summary_times_and_question_rows(tmp_path, monkeypatch, capsys):
    """report: 결과 폴더의 results.jsonl·summary.json·timing.jsonl 과 실행 JSON 으로 report.html 을 만든다(모델 요약·시간·질문별 FAIL 사유·빈 출력 비고)."""
    import tools.modelgrade as mg
    monkeypatch.setattr(mg, "ensure_collection_chrome", lambda: None, raising=False)
    from tools.report_html import write_report
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(mg, "RESULTS_JSON_DIR", tmp_path / "결과")
    set_dir = _set(tmp_path)
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    run = mg.new_run("보고서", set_dir, groups)
    run["providers"] = ["gpt"]; run["models"] = {"gpt": "ChatGPT 앱"}
    run["datasets"] = {"gpt": [{"case_id": "Q1_T1", "question": "정확히 2개의 문장으로 인사말을 써줘.", "answer": "안녕하세요. 반갑습니다.", "collection": {"status": "SUCCESS", "latency_ms": 30000}},
                               {"case_id": "Q3_T1", "question": "아무것도 출력하지 마.", "answer": "", "collection": {"status": "SUCCESS", "capture": "empty", "latency_ms": 5000}}]}
    run["imports"] = [{"provider": "gpt", "method": "collect-app-v1", "seconds": 70.0}]
    mg.save_run(run)
    out = tmp_path / "judge" / run["run_id"]; out.mkdir(parents=True)
    lines = [{"provider": "gpt", "question": 1, "turns": 1, "status": "judged", "verdict": "FAIL", "code_fails": [], "seconds": 20.0, "calls": {"jev_flagged": {"1": ["J1-4.b"]}},
              "results": {"1": {"J1-4.b": {"decision": "FAIL", "judge": "jev+gpt", "jev": {"decision": "FAIL", "probabilities": {"FAIL": 0.8}}, "entries": [{"reason": "같은 말을 두 번", "answer_quote": "반갑습니다."}], "problems": []},
                                "J5-1.a": {"decision": "PASS", "judge": "gpt", "entries": [], "problems": []}}}},
             {"provider": "gpt", "question": 3, "turns": 1, "status": "empty_output_pass", "verdict": "PASS", "collection_note": "Q3_T1: SUCCESS · 받은 방법 empty"}]
    (out / "results.jsonl").write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps({"judges": {"gpt": {"model": "gpt-6-luna", "reasoning": "low"}, "jev": {"model": "jev-latest"}},
                                                  "models": {"gpt": {"n": 3, "seconds": {"gpt": {"mean": 20.0}, "jev": {"mean": 0.5}}, "jev_flagged": 1, "fallback": 0}}}), encoding="utf-8")
    (out / "timing.jsonl").write_text(json.dumps({"kind": "judge", "judgments": 2, "seconds": 41.0}) + "\n", encoding="utf-8")
    assert mg.main(["report", "--dir", str(out)]) == 0
    text = (out / "report.html").read_text(encoding="utf-8")
    assert "ChatGPT 앱" in text and "1 / 3" not in text or "2 / 3" in text   # 판정한 질문 2 / 전체 3
    assert "같은 말을 두 번" in text and "Jev FAIL 0.8" in text and "받은 방법 empty" in text and "1분 10초" in text and "판정 시간" in text
    assert "HTML 보고서" in capsys.readouterr().out
    assert write_report(out, None).name == "report.html"   # 실행 JSON 없이도 만들어진다


def test_empty_answer_is_success_only_when_the_question_asked_for_empty_output_and_progress_is_saved_per_question(tmp_path, monkeypatch):
    """빈 출력 요구 질문(Q3)의 빈 답은 SUCCESS, 보통 질문(Q1)의 빈 답은 ERROR(복사 실패로 보고 다시 받음). 질문마다 실행 파일에 중간 저장된다."""
    import tools.modelgrade as mg
    monkeypatch.setattr(mg, "ensure_collection_chrome", lambda: None, raising=False)
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(mg, "RESULTS_JSON_DIR", tmp_path / "결과")
    saves = []

    class FakeChat:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def new_chat(self): pass
        def ask(self, question, files=None, reference=None):
            saves.append(len(list(_runs(tmp_path))) and json.loads(_runs(tmp_path)[0].read_text(encoding="utf-8")))
            return {"answer": "", "status": "SUCCESS", "capture": "empty", "latency_ms": 1, "model": "m", "method": "x", "error": None, "url": None, "sent": []}
    monkeypatch.setattr(mg, "web_chat_class", lambda provider: FakeChat)
    set_dir = _set(tmp_path)
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    run = mg.new_run("빈답", set_dir, groups); mg.save_run(run)
    assert mg.main(["collect", "app", "--provider", "gpt", "--set", str(set_dir), "--only", "1,3", "--run", run["run_id"]]) == 0
    rows = {r["case_id"]: r for r in mg.load_run(run["run_id"])["datasets"]["gpt"]}
    assert rows["Q3_T1"]["collection"]["status"] == "SUCCESS" and rows["Q3_T1"]["collection"]["capture"] == "empty"   # 빈 출력 요구 → 빈 답 성공
    assert rows["Q1_T1"]["collection"]["status"] == "COPY_FAILED" and "보류" in rows["Q1_T1"]["collection"]["error"]   # 보통 질문 → 복사 재시도·새 채팅 재시도 뒤에도 비면 보류
    assert len(saves) == 2   # Q1 한 번 + Q3 한 번 (새 채팅 재시도는 하지 않는다 — 복사만 다시)
    # 둘째 질문(Q3)을 물을 때 이미 첫 질문(Q1) 결과가 파일에 저장돼 있다
    assert saves[1] and any(r["case_id"] == "Q1_T1" and r["collection"]["status"] == "COPY_FAILED" for r in saves[1]["datasets"]["gpt"])
    assert mg.main(["collect", "app", "--provider", "gpt", "--set", str(set_dir), "--only", "1,3", "--run", run["run_id"], "--skip-done"]) == 0
    assert len(saves) == 2   # 복사 실패(보류)·빈 출력 성공은 자동으로 다시 받지 않는다


def test_result_dir_keeps_judge_report_apart_and_collection_logs(tmp_path):
    """결과 폴더: report.html 은 결과 보고서 자리라 판정 보고서는 판정보고서.html 로, 답변 수집 기록은 수집기록/ 으로 (빈 로그 뺌, 10-22)."""
    from tools import result_dirs
    judge, logs, dest = tmp_path / "v4", tmp_path / "logs", tmp_path / "결과"
    judge.mkdir(); logs.mkdir()
    (judge / "report.html").write_text("판정", encoding="utf-8")
    (judge / "results.jsonl").write_text("{}", encoding="utf-8")
    (logs / "collect-1-gpt.log").write_text("수집", encoding="utf-8")
    (logs / "collect-1-aiona.log").write_text("", encoding="utf-8")
    (logs / "gpt-recheck.jsonl").write_text("{}", encoding="utf-8")
    assert sorted(result_dirs.publish(judge, dest)) == ["results.jsonl", "판정보고서.html"]
    assert not (dest / "report.html").exists()
    assert result_dirs.publish_collection(logs, dest) == ["collect-1-gpt.log", "gpt-recheck.jsonl"]
    assert (dest / "수집기록" / "collect-1-gpt.log").read_text(encoding="utf-8") == "수집"


def test_code_eval_recomputes_with_current_engine_and_keeps_backup(tmp_path, monkeypatch, capsys):
    """modelgrade code-eval: 실행 JSON 의 CODE 평가를 지금 엔진으로 다시 계산(심판 호출 없음), 이전 파일은 .bak 으로 (10-25 밤)."""
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(mg, "RESULTS_JSON_DIR", tmp_path / "결과")
    assert mg.main(["collect", "import", "--provider", "claude", "--csv", str(_csv(tmp_path)), "--set", str(_set(tmp_path)), "--new", "시험"]) == 0
    run_path = _runs(tmp_path)[0]
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["evaluations"]["claude"]["versions"]["engine"] = "aiona-code-eval-v0.0.0"   # 옛 엔진의 결과인 것처럼
    for c in run["evaluations"]["claude"]["cases"]:
        for r in c["results"]:
            if r["check_id"] == "1-1.response_nonempty":
                r["verdict"] = "FAIL"
    run_path.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()
    assert mg.main(["code-eval", "--run", run["run_id"]]) == 0
    out = capsys.readouterr().out
    new = json.loads(run_path.read_text(encoding="utf-8"))
    assert new["evaluations"]["claude"]["versions"]["engine"] != "aiona-code-eval-v0.0.0"
    assert new["code_reevals"][0]["providers"]["claude"]["engine_before"] == "aiona-code-eval-v0.0.0"
    assert new["code_reevals"][0]["providers"]["claude"]["changed"] >= 1 and "바뀐 검사 결과" in out and "--recode" in out
    assert list((tmp_path / "runs").glob("*.json.bak-*"))

