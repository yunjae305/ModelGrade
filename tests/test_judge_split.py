"""나눠서 판정 — J1~J4 는 Jev, J5~J8 은 GPT CLI. 고정 질문 묶음·고정 프롬프트, 질문 단위, Jev 가 못 하면 GPT 가 대신 (2026-10-19 결정). 대역 심판, 호출 없음."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from tools import judge_run as jr
from tools import judge_split as js

ROOT = Path(__file__).resolve().parents[1]
SET = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"
Q36_ANSWER = "해수면에서 물은 100도에서 끓습니다. 101.325킬로파스칼 기준입니다."


def test_fixed_jev_questions_cover_only_j1_to_j4_and_match_the_builder():
    saved = json.loads(js.JEV_QUESTIONS.read_text(encoding="utf-8"))
    assert saved == js.jev_question_defs()   # 고정 파일 = 도구 결과 (손으로 고치지 않음)
    assert saved["items"] and all(i.startswith(js.JEV_PREFIXES) for i in saved["items"])
    with_na = {i for i, d in saved["items"].items() if "NOT_APPLICABLE" in d["criteria"]}
    assert with_na == set(js.JEV_NOT_APPLICABLE) and "J2-2.a" not in with_na   # 해당 없음은 상황표 2 의 항목만
    assert "AIONA" not in json.dumps(saved, ensure_ascii=False)


def test_split_fold_and_overrides():
    assert js.split(["J1-1.a", "J4-2.a", "J5-3.a", "J6-1.a"]) == {"jev": ["J1-1.a", "J4-2.a"], "gpt": ["J5-3.a", "J6-1.a"]}
    assert js.fold([{"decision": "PASS"}, {"decision": "FAIL"}]) == "FAIL"
    assert js.fold([{"decision": "NOT_APPLICABLE"}, {"decision": "NOT_APPLICABLE"}]) == "NOT_APPLICABLE"
    turn = {"J1-2.a": {"decision": "FAIL"}, "J1-1.a": {"decision": "PASS"}}
    js.apply_overrides(turn)
    assert turn["J1-1.a"]["decision"] == "NOT_APPLICABLE" and turn["J1-1.a"]["overridden_from"] == "PASS"


class FakeCodex:
    """고정 프롬프트 + [입력] 을 받아, 턴마다 '판정할 항목'을 모두 PASS 로 (Q36 J5-3.a 만 원문 인용과 함께 FAIL)."""

    def __init__(self):
        self.inputs = []

    def __call__(self, cmd, input=None, **kwargs):
        self.inputs.append(input)
        prompt, _, text = input.partition("\n[입력]\n")
        assert prompt.startswith("당신은 한국어 AI 답변 품질 평가의 의미 평가 심판입니다.") and text.startswith("실행 표식: ")
        turns = []
        for n, block in re.findall(r"<<\w+ (\d+)턴 시작>>\n(.*?)<<\w+ \1턴 끝>>", text, re.S):
            items = re.search(r"판정할 항목 \(\d+개\): (.*)", block).group(1).split()
            entries = [{"id": i, "decision": "PASS", "question_quote": "", "answer_quote": "", "reason": "", "quote_turn": None, "fact": None}
                       for i in items]
            if "100도에서 끓습니다" in block:
                for e in entries:
                    if e["id"] == "J5-3.a":
                        e.update(decision="FAIL", answer_quote="101.325킬로파스칼 기준입니다.")
            turns.append({"turn": int(n), "items": entries})
        out = Path(cmd[cmd.index("--output-last-message") + 1])
        out.write_text(json.dumps({"turns": turns}, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"type": "turn.completed", "usage": {}}) + "\n", stderr="")


class FakeJev:
    def __init__(self):
        self.requests = []
        self.config = SimpleNamespace(model="jev-test")

    def system_one(self, state, questions):
        self.requests.append((state, questions))
        answers = {qid: {"choice": "FAIL" if qid == "T1:J2-1.a" else "PASS", "probabilities": {"PASS": 0.9}} for qid in questions}
        return SimpleNamespace(status="OK", body={"answers": answers}, error=None)


def _run(tmp_path, jev_max=jr.JEV_MAX_STATE_CHARS):
    rows = jr.ji.flatten(json.loads((SET / "questions.json").read_text(encoding="utf-8")))
    q11 = [r for r in rows if r["case_id"].startswith("Q11_T")]
    empty = next(r for r in rows if jr.ji.asks_empty_output(None, r["question"]))
    data = {"run_id": "t", "providers": ["aiona", "gpt"],
            "datasets": {"aiona": [{"case_id": "Q36_T1", "answer": Q36_ANSWER}, {"case_id": empty["case_id"], "answer": ""}]
                         + [{"case_id": r["case_id"], "answer": f"답{r['turn']}"} for r in q11],
                         "gpt": [{"case_id": "Q36_T1", "answer": "", "collection": {"status": "ERROR"}}]}}
    codex, jev = FakeCodex(), FakeJev()
    gpt = jr.CodexJudge("codex", "gpt-test", "low", 60, "live", runner=codex)
    only = {36, 11, int(empty["case_id"].split("_T")[0][1:])}
    summary = jr.run(data, SET, tmp_path, gpt, jr.JevJudge(jev, jev_max), only=only, workers=1)
    lines = {(l["provider"], l["question"]): l for l in map(json.loads, (tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines())}
    return summary, lines, codex, jev, empty


def test_split_run_sends_j1_to_j4_to_jev_and_the_rest_to_gpt(tmp_path):
    summary, lines, codex, jev, empty = _run(tmp_path)
    q36 = lines[("aiona", 36)]
    assert q36["status"] == "judged" and q36["verdict"] == "FAIL"   # Jev J2-1.a FAIL · GPT J5-3.a FAIL
    items = q36["results"]["1"]
    assert items["J2-1.a"] == {**items["J2-1.a"], "decision": "FAIL", "judge": "jev"} and items["J5-3.a"]["judge"] == "gpt"
    assert all(v["judge"] == ("jev" if k.startswith(js.JEV_PREFIXES) else "gpt") for k, v in items.items())
    # GPT 입력에는 J5~J8 만, Jev 질문에는 J1~J4 만 — 같은 질문을 두 심판이 겹쳐 보지 않는다
    gpt_input = next(i for i in codex.inputs if Q36_ANSWER in i)
    assert "판정할 항목 (2개): J5-1.a J5-3.a" in gpt_input and "J2-1.a" not in gpt_input.partition("\n[입력]\n")[2]
    state, questions = next(r for r in jev.requests if r[0]["turns"][0]["answer"] == Q36_ANSWER)
    assert set(questions) == {f"T1:{i}" for i in items if i.startswith(js.JEV_PREFIXES)} and "guide" in state
    q11 = lines[("aiona", 11)]
    assert q11["turns"] == 10 and set(q11["results"]) == {str(n) for n in range(1, 11)}   # 대화 1개 = 판정 1번, 턴마다 결과
    expected = "FAIL" if "J2-1.a" in q11["results"]["1"] else "PASS"   # 대역 Jev 는 T1:J2-1.a 만 FAIL
    assert q11["verdict"] == expected and len([i for i in codex.inputs if "답10" in i]) == 1
    assert lines[("aiona", int(empty["case_id"].split("_T")[0][1:]))]["verdict"] == "HOLD"
    assert lines[("gpt", 36)]["status"] == "no_answer" and lines[("gpt", 36)]["verdict"] == "FAIL"
    assert summary["models"]["aiona"]["FAIL"] == 1 + (expected == "FAIL") and summary["models"]["aiona"]["HOLD"] == 1
    calls = len(codex.inputs)
    jr.run(json.loads(json.dumps({"run_id": "t", "providers": ["aiona"], "datasets": {"aiona": []}})), SET, tmp_path,
           jr.CodexJudge("codex", "m", "low", 60, "live", runner=codex), None, only={36})
    assert len(codex.inputs) == calls   # 이미 판정한 줄은 건너뛴다(이어서 돌리기)


def test_jev_input_limit_falls_back_to_gpt_with_the_same_fixed_prompt(tmp_path):
    _, lines, codex, jev, _ = _run(tmp_path, jev_max=10)
    q36 = lines[("aiona", 36)]
    assert q36["calls"]["jev"]["status"] == "TOO_LONG" and q36["calls"]["gpt_fallback"]["status"] == "OK"
    assert q36["results"]["1"]["J2-1.a"]["judge"] == "gpt" and q36["results"]["1"]["J2-1.a"]["fallback"] is True
    assert not jev.requests


def test_quote_not_in_the_answer_holds_the_item():
    turns = [{"case_id": "Q1_T1", "turn": 1, "question": "질문"}]
    out = js.check_gpt({"turns": [{"turn": 1, "items": [{"id": "J5-3.a", "decision": "FAIL", "answer_quote": "없는 문장", "question_quote": ""}]}]},
                       turns, {"Q1_T1": "답변"}, {1: ["J5-3.a", "J7-1.a"]})
    assert out[1]["J5-3.a"]["decision"] == "UNVERIFIED" and "answer_quote" in out[1]["J5-3.a"]["problems"][0]
    assert out[1]["J7-1.a"]["problems"] == ["판정 없음"]
    assert js.semantic_verdict(out) == "HOLD"


def test_quotes_pass_when_stitched_lines_or_attachment_header_match_the_rendered_input():
    """2026-10-19 시험: 심판이 떨어진 두 줄을 이어 붙여 인용(Q48)하거나 [첨부파일] 머리줄까지 인용(Q30·Q46)한 경우는 원문 대조 통과."""
    turns = [{"case_id": "Q1_T1", "turn": 1, "question": "첨부를 요약해줘.", "attachments": [{"name": "공지.txt", "type": "txt", "content": "# 안내\n- 기간: 6월"}]}]
    answer = "- 기간은 6월입니다.\n\n### 준비물\n- 활동지를 준비합니다."
    items = [{"id": "J8-1.a", "decision": "PASS", "answer_quote": "- 기간은 6월입니다.\n- 활동지를 준비합니다.",
              "question_quote": "[첨부파일]\n- 공지.txt · txt\n# 안내"},
             {"id": "J8-3.a", "decision": "FAIL", "answer_quote": "원문에 없는 문장", "question_quote": ""}]
    out = js.check_gpt({"turns": [{"turn": 1, "items": items}]}, turns, {"Q1_T1": answer}, {1: ["J8-1.a", "J8-3.a"]})
    assert out[1]["J8-1.a"]["problems"] == [] and out[1]["J8-1.a"]["decision"] == "PASS"
    assert out[1]["J8-3.a"]["decision"] == "UNVERIFIED"


def test_codex_rate_limit_waits_and_retries_instead_of_failing(monkeypatch):
    calls = []
    def runner(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="exceeded retry limit, last status: 429 Too Many Requests")
        Path([c for c in cmd if c.endswith("answer.json")][0]).write_text('{"turns": []}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}', stderr="")
    slept = []
    monkeypatch.setattr(jr.time, "sleep", lambda s: slept.append(s))
    judge = jr.CodexJudge("codex", "m", "low", 0, "live", runner=runner)
    status, body, err = judge.judge("프롬프트", "입력", False)
    assert status == "OK" and body == {"turns": []} and slept == [60] and len(calls) == 2
