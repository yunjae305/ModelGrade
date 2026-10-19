"""고정 판정 프롬프트 v2 · 판정 상황표 · 채우기 도구 — 본문은 고정이고 질문마다는 색인한 상황·질문·답변만 바뀐다 (2026-10-19 미팅 결정). AI 호출 없음."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools import build_judge_prompt_v1 as v1
from tools import build_judge_prompt_v2 as v2
from tools import build_judge_situations as bs
from tools import judge_prompts as jp

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "참고자료"
SET = DOCS / "질문세트_326"
EXAMPLE = DOCS / "프롬프트" / "예시_판정프롬프트_Q36_AIONA_v1.txt"


def test_fixed_prompt_files_match_their_builders():
    """v1 은 그대로(고정), v2 는 저장된 본문과 도구가 같은 글을 만든다 — 손으로 고친 흔적이 없다."""
    assert v1.prompt_text() == (DOCS / "프롬프트" / "judge_prompt_v1.txt").read_text(encoding="utf-8")
    body = v2.prompt_text()
    assert body == (DOCS / "프롬프트" / "judge_prompt_v2.txt").read_text(encoding="utf-8")
    assert sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", body))) == sorted(v2.SLOTS)


def test_v2_fixes_the_always_label_and_names_no_model():
    body = v2.prompt_text()
    labelled = re.findall(r"- \*\*(J\d-\d\.[a-z])\*\*[^\n]*\n(?:  ·[^\n]*\n)*?  · 표시: 항상 적용", body)
    assert labelled == ["J1-4.a", "J1-4.b", "J3-1.a"]   # v1 은 조건부인 J1-1.a·J1-2.a·J1-3.a·J1-3.b 에도 붙였다
    assert "AIONA" not in body   # 심판은 어느 모델 답변인지 모른다 — 조건에 모델 이름을 쓰지 않는다
    assert "## 1. 질문으로 정해지는 상황" in body and "## 2. 답변을 보고 정하는 경우" in body and "## 3. 판정하지 않는 경우" in body
    assert body.count("\n- J") >= 30   # 상황표 1 의 세부 기준 30개


@pytest.mark.multi_turn_rule   # 실제 멀티턴 길이 조건(10턴·10K 토큰)으로 — conftest 는 기본으로 끈다
def test_situation_index_is_fixed_and_matches_the_items_table():
    saved = json.loads((SET / "판정상황표_v2.json").read_text(encoding="utf-8"))
    fresh = bs.build(SET)
    assert fresh["rows"] == saved["rows"] and len(saved["rows"]) == 487   # 다시 만들어도 글자 하나 같다
    table = {r["case_id"]: r["items"] for r in json.loads((SET / "판정항목표_v1.json").read_text(encoding="utf-8"))["rows"]}
    assert all(table[r["case_id"]] == r["items"] for r in saved["rows"])
    assert "AIONA" not in json.dumps(saved, ensure_ascii=False)   # 대화 id(AIONA_ID_…) 가 근거 글에 새지 않는다
    q36 = next(r for r in saved["rows"] if r["case_id"] == "Q36_T1")["case_situations"]
    assert q36.startswith("판정할 항목 (17개): J1-1.a") and '근거: "존댓말로 답해줘." (찾은 말: 존댓말)' in q36


def _example_answer() -> str:
    text = EXAMPLE.read_text(encoding="utf-8")
    return re.search(r"<<\w+ 답변 시작>>\n(.*)\n<<\w+ 답변 끝>>", text, re.S).group(1)


def _example_question() -> str:
    text = EXAMPLE.read_text(encoding="utf-8")
    return re.search(r"<<\w+ 질문 시작>>\n(.*)\n<<\w+ 질문 끝>>", text, re.S).group(1)


def test_fill_reproduces_the_hand_made_example_and_keeps_the_body_fixed(tmp_path):
    answer = _example_answer()
    run = {"run_id": "t", "providers": ["aiona", "gpt"],
           "datasets": {"aiona": [{"case_id": "Q36_T1", "answer": answer}],
                        "gpt": [{"case_id": "Q36_T1", "answer": "답 {{ANSWER}} {{RUN_MARK}}", "collection": {"status": "SUCCESS"}}]}}
    manifest = jp.build(run, SET, tmp_path, only={"Q36_T1"}, mark="abc123")
    filled = (tmp_path / "aiona" / "Q36_T1.txt").read_text(encoding="utf-8")
    assert re.search(r"<<abc123 질문 시작>>\n(.*)\n<<abc123 질문 끝>>", filled, re.S).group(1) == _example_question()
    assert re.search(r"<<abc123 답변 시작>>\n(.*)\n<<abc123 답변 끝>>", filled, re.S).group(1) == answer
    template = (DOCS / "프롬프트" / "judge_prompt_v2.txt").read_text(encoding="utf-8")
    head = template.split("{{RUN_MARK}}", 1)[0]
    assert filled.startswith(head) and "aiona" not in filled.lower()   # 본문은 그대로, 모델 이름은 없음
    other = (tmp_path / "gpt" / "Q36_T1.txt").read_text(encoding="utf-8")
    assert "답 {{ANSWER}} {{RUN_MARK}}" in other   # 넣은 글 안의 슬롯 모양 글자는 다시 바뀌지 않는다
    assert manifest["counts"] == {"aiona": {"ready": 1}, "gpt": {"ready": 1}} and manifest["prompt_version"] == "judge-prompt-v2"


def test_multi_turn_uses_the_same_models_previous_answers_and_skips_held_or_missing(tmp_path):
    rows = jp.flatten(json.loads((SET / "questions.json").read_text(encoding="utf-8")))
    conv = [r for r in rows if r.get("conversation_id") == rows[[r["case_id"] for r in rows].index("Q11_T2")]["conversation_id"]]
    empty = next(r["case_id"] for r in rows if jp.asks_empty_output(None, r["question"]))
    run = {"run_id": "t", "providers": ["aiona", "gpt"],
           "datasets": {"aiona": [{"case_id": r["case_id"], "answer": f"AIONA답{r['turn']}"} for r in conv] + [{"case_id": empty, "answer": ""}],
                        "gpt": [{"case_id": "Q11_T1", "answer": "GPT답1", "collection": {"status": "SUCCESS"}},
                                {"case_id": "Q11_T2", "answer": "", "collection": {"status": "ERROR"}}]}}
    jp.build(run, SET, tmp_path, only={"Q11_T2", empty}, mark="m0m0m0")
    filled = (tmp_path / "aiona" / "Q11_T2.txt").read_text(encoding="utf-8")
    block = re.search(r"<<m0m0m0 질문 시작>>\n(.*)\n<<m0m0m0 질문 끝>>", filled, re.S).group(1)
    assert block.startswith("[앞 대화]\n1턴 질문: ") and "1턴 답변: AIONA답1" in block and "[현재 질문]\n" in block
    assert "GPT답" not in filled   # 다른 모델의 답변은 섞이지 않는다
    index = [json.loads(line) for line in (tmp_path / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    status = {(e["provider"], e["case_id"]): e["status"] for e in index}
    assert status[("aiona", empty)] == "held_empty_output" and status[("gpt", "Q11_T2")] == "no_answer"
    assert not (tmp_path / "gpt" / "Q11_T2.txt").exists() and not (tmp_path / "aiona" / f"{empty}.txt").exists()
