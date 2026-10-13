"""롱컨텍스트 경계 테스트 도구 — 문서 생성·채점 (API 호출 없음)."""
import pytest

from code_eval import long_context
from tools.long_context_threshold_test import build_case, grade

try:
    long_context.count_tokens("확인")
except long_context.TokenizerUnavailable:   # 토크나이저가 없으면 길이를 맞출 수 없다
    pytest.skip("tiktoken o200k_base 를 불러올 수 없음", allow_module_level=True)


def test_document_hits_the_target_length_and_places_facts_at_fixed_depths():
    case = build_case(32_000)
    assert abs(case["tokens"] - 32_000) < 300 and build_case(32_000)["sha256"] == case["sha256"]   # 같은 시드면 같은 문서
    prompt = case["prompt"]
    depth = lambda key: prompt.find(key) / len(prompt)
    assert abs(depth("은하수-7") - 0.10) < 0.02 and abs(depth("'청람'") - 0.50) < 0.02 and abs(depth("해오름") - 0.90) < 0.02
    assert abs(depth("윤서진 책임자는") - 0.25) < 0.02 and abs(depth("윤서진 책임자가 관리") - 0.75) < 0.02
    assert all(prompt.count(code) == 1 for code in case["answers"].values())


def test_grading_reads_numbered_lines_and_falls_back_to_the_whole_answer():
    answers = {1: "KX-1111", 2: "CR-2222", 3: "HD-3333", 4: "BG-4444"}
    assert grade("1) KX-1111\n2) CR-2222\n3) 모름\n4) BG-9999", answers) == {1: True, 2: True, 3: False, 4: False}
    assert grade("**1)** KX-1111\n2. CR-2222\n3: HD-3333\n4번 BG-4444", answers) == {1: True, 2: True, 3: True, 4: True}
    assert grade("Q1: KX-1111\nQ2: CR-2222", answers) == {1: True, 2: True, 3: False, 4: False}
    assert grade("KX-1111, CR-2222, HD-3333, BG-4444", answers) == {1: False, 2: False, 3: False, 4: False}   # 번호 줄 없으면 틀림
    assert grade("Q1: CR-2222\nQ2: KX-1111", answers) == {1: False, 2: False, 3: False, 4: False}   # 다른 문항의 코드로 못 맞힘
    assert grade("1) KX-11110\n2) CR-2222 또는 CR-9999\n3) hd - 3333", answers) == {1: False, 2: False, 3: True, 4: False}
    assert grade("1) KX-1111A\n2) CR-2222-2\n3) HD-3333 또는 HD-99999", answers) == {1: False, 2: False, 3: False, 4: False}


def test_count_answers_need_one_whole_count():
    from tools.long_context_threshold_test import _count_in
    assert [_count_in(t) for t in ("7", "**총 7번.**", "7회 발생했습니다", "다섯 번", "다섯", "두 번")] == [7, 7, 7, 5, 5, 2]
    assert [_count_in(t) for t in ("-4", "4.9", "4 또는 8", "4-5", "5 또는 여섯", "모르겠네요", "네", "1월 7일")] == [None] * 8


def test_hard_document_codes_and_outage_days_never_repeat():
    from tools.long_context_threshold_test import build_hard_case
    collided = build_hard_case(32_000, seed=299)   # 예전에는 1번 정답과 오답 연구원의 사원 번호가 같았다
    assert collided["prompt"].count(collided["answers"][1]) == 1
    outages = [line for line in build_hard_case(32_000, seed=1)["prompt"].split("\n") if "정전이 일어나" in line]
    assert len(outages) == len(set(outages))   # 예전에는 같은 날 정전이 두 줄이었다


def test_run_checks_the_folder_first_saves_each_answer_and_marks_incomplete(tmp_path, monkeypatch):
    import json
    import webapp.comparison_service as service
    from tools import long_context_threshold_test as tool
    calls = []

    def fake(provider, config, messages, max_tokens, client):
        calls.append(provider)
        if len(calls) == 2 and fail_second:
            raise RuntimeError("끊김")
        return {"status": "INCOMPLETE", "answer": "1) 모름", "finish_reason": "max_tokens", "raw": {"usage": [1, 2]}}

    monkeypatch.setattr(service, "request_answer", fake)
    monkeypatch.setattr(tool, "_configs", lambda: {"gpt": {"key": "k", "model": "m"}})
    monkeypatch.setattr(tool, "LENGTHS", (32_000, 33_000))
    (tmp_path / "file").write_text("x")
    fail_second = False
    with pytest.raises(OSError):
        tool.run(["gpt"], tmp_path / "file" / "out")
    assert calls == []   # 저장할 곳이 없으면 호출 전에 멈춘다
    fail_second = True
    with pytest.raises(RuntimeError):
        tool.run(["gpt"], tmp_path / "cut")
    assert len((tmp_path / "cut" / "partial.jsonl").read_text(encoding="utf-8").splitlines()) == 1   # 끊기기 전 답은 남는다
    fail_second, calls[:] = False, []
    with pytest.raises(SystemExit):
        tool.run(["gpt"], tmp_path / "cut")   # 같은 폴더로 다시 돌리면 남은 답을 지우지 않고 멈춘다
    assert calls == [] and len((tmp_path / "cut" / "partial.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    report = tool.run(["gpt"], tmp_path / "done")
    assert report["results"][0]["usage"] == {"input_tokens": None, "output_tokens": None}
    assert "(INCOMPLETE: max_tokens)" in (tmp_path / "done" / "summary.md").read_text(encoding="utf-8")
    assert not (tmp_path / "done" / "partial.jsonl").exists() and json.loads((tmp_path / "done" / "results.json").read_text(encoding="utf-8"))["grader"]


def test_hard_document_hides_unmarked_facts_with_one_resolvable_answer_each():
    import re
    from tools.long_context_threshold_test import build_hard_case
    case = build_hard_case(32_000)
    prompt, answers = case["prompt"], case["answers"]
    assert abs(case["tokens"] - 32_000) < 300 and "[특별 기록]" not in prompt and case["kinds"] == {5: "count"}
    assert "부산" not in prompt.split("위 기록만 보고")[0] and "제주" not in prompt.split("위 기록만 보고")[0]   # 질문의 도시는 문서에 없음
    team = re.search(r"모래시계 사업은 (\S+)이 맡는다", prompt).group(1)
    leader = re.search(team + r"의 팀장은 (\S+)이다", prompt).group(1)
    assert re.search(leader + r" 팀장이 승인한 예산 코드는 (\S+)이다", prompt).group(1) == answers[4]   # 3단계 연결은 하나로만 이어짐
    assert prompt.count("정전이 일어나") == int(answers[5])
    assert grade("1) a\n2) b\n3) c\n4) d\n5) 다섯 번", answers, case["kinds"])[5] is (answers[5] == "5")
