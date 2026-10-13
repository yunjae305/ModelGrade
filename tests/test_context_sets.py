"""J6 멀티턴·J7 롱컨텍스트 시험 세트 생성기 (tools/context_sets.py, 10-14 4차) — API 호출 없음.

공식 규칙(multi-turn-v1·long-context-v3.1-272k)으로 적용 여부를 그대로 세는지, 정답 조건이 올바른 답은 통과·의도적 실패 답은 걸러내는지,
근거 문장이 그 턴·문서에 실제로 있는지, 같은 시드면 같은 데이터인지 본다.
"""
import hashlib

import pytest

from code_eval import long_context, multi_turn
from tools.context_sets import (build_j6_set, build_j7_case, build_j7_set, check_answer, check_set, duplicate_ratio, grade,
                                write_set)

# 공식 J6 길이 조건(10K 토큰 이상 턴 10개)을 실제 값으로 둔다 — conftest 는 기본으로 끈다
pytestmark = pytest.mark.multi_turn_rule


def test_stress_conversation_is_multi_turn_under_the_official_rule_for_any_answer():
    built = build_j6_set("stress", conversations=1)
    rows = built["rows"]
    assert len(rows) == 11 and {r["conversation_id"] for r in rows} == {"CTX_J6S_01"}
    # 답변 없이 재도 사용자 턴만으로 긴 턴 11개 — 어떤 모델 답변이 붙어도 J6 대상 (모델별 자기 답변 길이와 무관)
    measure = multi_turn.measure_conversation([(r["turn"], r["question"], "") for r in rows])
    assert measure["multi_turn"] is True and measure["long_turns"] == 11
    assert all(long_context.count_tokens(r["question"]) >= multi_turn.MIN_TURN_TOKENS for r in rows)
    assert all(r["answer"] == "" for r in rows)   # 질문 초안


def test_every_turn_has_checks_that_pass_the_good_answer_and_catch_the_intentional_failure():
    built = build_j6_set("stress", conversations=1)
    text = {r["id"]: r["question"] for r in built["rows"]}
    for turn in built["expect"]["CTX_J6S_01"]["turns"].values():
        assert check_answer(turn["good_answer"], turn["checks"])["passed"], turn["case_id"]
        bad = check_answer(turn["bad_answer"], turn["checks"])
        assert not bad["passed"] and (bad["missing"] or bad["forbidden"]), turn["case_id"]
        assert all(e["quote"] in text[turn["case_id"]] for e in turn["evidence"])
        assert turn["expected_behavior"] and turn["context"]
    turns = built["expect"]["CTX_J6S_01"]["turns"]
    assert {item for t in turns.values() for item in t["applies"]} >= {"J6-1.a", "J6-2.a", "J6-2.b", "J6-2.c", "J6-3.a", "J6-4.a", "J6-4.b"}


def test_general_conversations_are_short_and_only_the_two_turn_rule_applies():
    built = build_j6_set("general")
    convs = {r["conversation_id"] for r in built["rows"]}
    assert len(convs) == 6
    for conv in convs:
        rows = [r for r in built["rows"] if r["conversation_id"] == conv]
        assert len(rows) == 6
        assert multi_turn.measure_conversation([(r["turn"], r["question"], "") for r in rows])["multi_turn"] is False


def test_j7_document_answers_evidence_and_failures():
    case = build_j7_case(32_000)
    assert abs(case["tokens"] - 32_000) < 400 and case["duplicate_ratio"] < 0.02
    answers = {int(k): v for k, v in case["answers"].items()}
    assert all(grade(case["good_answer"], answers).values())
    for n, answer in case["bad_answers"].items():
        assert grade(answer, answers)[int(n)] is False
    positions = [case["evidence"]["1"]["position"], case["evidence"]["2"]["position"], case["evidence"]["3"]["position"]]
    assert positions == sorted(positions) and positions[0] < 0.1 and positions[-1] > 0.9   # 초반·중간·후반
    assert grade("6) OK-EM-1234", {6: "OK-EM-1234"}) == {6: True}   # 6번 줄도 읽는다


def test_j7_official_applicability_follows_the_272k_rule():
    small, large = build_j7_case(250_000)["tokens"], build_j7_case(long_context.MIN_TOKENS + 500)["tokens"]
    assert small < long_context.MIN_TOKENS <= large


def test_written_set_is_reproducible_and_prechecked(tmp_path):
    folder = tmp_path / "j7"
    write_set(build_j7_set([32_000]), folder)
    report = check_set(folder)
    assert report["reproducible"] and report["problems"] == [] and report["j7_official_units"] == 0
    j6 = tmp_path / "j6"
    write_set(build_j6_set("general"), j6)
    report = check_set(j6)
    assert report["reproducible"] and report["problems"] == []
    assert (report["j6_official_units"], report["j6_two_turn_units"]) == (0, 6)
    a, b = build_j6_set("general")["rows"][0]["question"], build_j6_set("general")["rows"][0]["question"]
    assert hashlib.sha256(a.encode()).digest() == hashlib.sha256(b.encode()).digest()
    assert duplicate_ratio("가. 가. 나.") > 0
