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


def test_collect_app_collects_turns_in_order_skips_done_and_code_evaluates(tmp_path, monkeypatch, capsys):
    """데스크톱 앱 수집 — 대화는 새 채팅 하나에서 턴 순서대로, 이미 받은 질문은 건너뜀, 받은 행은 CODE 평가."""
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
    calls = []

    class FakeChat:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def new_chat(self): calls.append("new")
        def ask(self, question, files=None, reference=None):
            calls.append(("ask", question[:10], [f.name for f in files or []]))
            return {"answer": "답입니다. 두 문장입니다.", "status": "SUCCESS", "capture": "copy-button", "latency_ms": 1200, "model": "GPT-6 · Medium (앱)", "method": "chatgpt-app-v1", "error": None, "url": None, "sent": []}
    monkeypatch.setattr(mg, "app_chat_class", lambda provider: FakeChat)
    set_dir = _set(tmp_path)
    assert mg.main(["collect", "app", "--provider", "gpt", "--set", str(set_dir), "--only", "2,1", "--new", "앱 시험"]) == 0
    assert calls == ["new", ("ask", "가게 이름을 하나 정해줘."[:10], []), ("ask", "아까 정한 이름으로 간판 문구를 써줘."[:10], []), "new", ("ask", "정확히 2개의 문장으로 인사말을 써줘. 존댓말로 답해줘."[:10], [])]
    run = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
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
    claude 는 UIA 앱 수집기(ClaudeAppChat)."""
    import argparse
    import tools.modelgrade as mg
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
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
    monkeypatch.setattr(mg, "app_chat_class", fake_cls)
    set_dir = _set(tmp_path)
    assert mg.main(["collect", "app", "--provider", "gpt,claude", "--set", str(set_dir), "--only", "1", "--new", "두 앱"]) == 0
    runs = list((tmp_path / "runs").glob("*.json"))
    assert len(runs) == 1 and seen == ["gpt", "claude"] and places == ["left", "right"]
    run = json.loads(runs[0].read_text(encoding="utf-8"))
    assert run["datasets"]["gpt"][0]["answer"] == "gpt 답입니다." and run["datasets"]["claude"][0]["answer"] == "claude 답입니다."
    assert run["models"] == {"gpt": "ChatGPT 앱 (GPT-6 · Medium)", "claude": "Claude 앱 (Sonnet 5.5 · 노력 중간)"}
    assert not list((tmp_path / "runs").glob("*.lock"))
    with pytest.raises(SystemExit):
        mg.main(["collect", "app", "--provider", "gemini", "--set", str(set_dir), "--only", "1"])
    monkeypatch.undo()
    assert mg.app_chat_class("claude").__name__ == "ClaudeAppChat"


def test_merge_provider_into_run_keeps_other_providers_written_meanwhile(tmp_path, monkeypatch):
    """동시 수집: 두 자식이 같은 실행 파일에 쓴다 — 잠금 아래 다시 읽어 자기 모델 자리만 바꾸므로 먼저 쓴 쪽이 안 지워진다."""
    import tools.modelgrade as mg
    monkeypatch.setattr(mg, "RUNS", tmp_path / "runs")
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
