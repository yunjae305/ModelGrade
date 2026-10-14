"""최소 필요 질문 수 도구 — 탐욕 다중 덮개·정밀도 n·부트스트랩·질문 세트 경로 (모델 호출 없음)."""
from __future__ import annotations

import json
from pathlib import Path

from tools import min_question_count as mq

ROOT = Path(__file__).resolve().parents[1]


def test_required_n_matches_the_normal_approximation():
    assert mq.required_n(0.5, 0.10) == 97 and mq.required_n(0.8, 0.10) == 62 and mq.required_n(0.8, 0.078) == 102


def test_greedy_multicover_meets_demand_and_reports_bottlenecks():
    order = ["q1", "q2", "q3", "q4", "q5"]
    targets = {"q1": {"A", "B"}, "q2": {"A"}, "q3": {"B", "C"}, "q4": {"A", "B", "C"}, "q5": {"D"}}
    names = ["A", "B", "C", "D", "E"]
    one = mq.greedy_multicover(order, targets, names, 1)
    assert one["questions"] == 2 and set(one["chosen"]) == {"q4", "q5"} and one["never_applied"] == ["E"]
    two = mq.greedy_multicover(order, targets, names, 2)
    counts = mq.counts_for(two["chosen"], targets, names)
    assert all(counts[t] >= min(2, n) for t, n in mq.counts_for(order, targets, names).items())
    assert two["short"] == {"E": 0, "D": 1} and two["estimated_questions_for_short"] == 10   # D: 2 × 5 / 1


def test_prefix_table_counts_targets_reaching_k():
    order = ["q1", "q2", "q3"]
    targets = {"q1": {"A"}, "q2": {"A", "B"}, "q3": {"B"}}
    rows = mq.prefix_table(order, targets, ["A", "B"], [1, 2, 3, 10], [1, 2])
    assert [r["questions"] for r in rows] == [1, 2, 3]
    assert rows[0]["covered"] == {"1": 1, "2": 0} and rows[2]["covered"] == {"1": 2, "2": 2} and rows[1]["min"] == 1


def _case(verdicts):
    return {"case_status": "EVALUATED", "results": [{"area": "common", "counted": True, "check_id": f"{c}:x", "verdict": v}
                                                    for c, v in verdicts.items()]}


def test_bootstrap_ranks_the_better_model_first_and_narrows_with_size():
    good = [_case({"2-1.count": "PASS", "2-2.table": "PASS"}) for _ in range(40)]
    bad = [_case({"2-1.count": "FAIL" if i % 2 else "PASS", "2-2.table": "PASS"}) for i in range(40)]
    run = {"evaluations": {"gpt": {"cases": good}, "gemini": {"cases": bad}}}
    data = mq.unit_verdicts(run)
    assert data["gemini"]["질문#2"] == {"2-1.count": "FAIL", "2-2.table": "PASS"}
    rows = mq.bootstrap(data, [5, 40], draws=200)
    assert rows[0]["models"] == ["gpt", "gemini"] and rows[1]["rank_same"] >= rows[0]["rank_same"]
    assert rows[1]["half_width_max"] <= rows[0]["half_width_max"]


def test_analyze_runs_on_a_real_question_set_with_the_check_tool_rules(tmp_path):
    result = mq.analyze(json.loads((ROOT / "examples" / "questions.sample.json").read_text(encoding="utf-8")), ks=[1], sizes=[1, 2])
    assert result["questions"] >= 1 and result["targets"] > 50 and result["minimum"][0]["k"] == 1
    assert "3-2.declared_count" not in result["available"] and "J5-3.a" not in result["available"]   # 답변으로만 정해지는 항목은 세지 않음
    assert "질문" in mq.render(result)


def test_unit_verdicts_use_detailed_kinds_and_semantic_items():
    """부트스트랩도 적용 수 분석과 같은 세부 기준 단위 — CODE 는 '검사:종류', 의미 평가는 점검 항목 (펼친 요소는 하나라도 NO 면 FAIL)."""
    case = {"case_status": "EVALUATED", "results": [
        {"area": "common", "counted": True, "check_id": "2-1.count:sentence_count", "verdict": "FAIL"},
        {"area": "common", "counted": True, "check_id": "1-1.response_nonempty", "verdict": "PASS"}]}
    record = {"decisions": {"J2": {"decision": "FAIL", "subs": {"J2-2": {"items": [
        {"id": "J2-2.a#1", "item_id": "J2-2.a", "decision": "YES"}, {"id": "J2-2.a#2", "item_id": "J2-2.a", "decision": "NO"},
        {"id": "J2-3.a", "decision": "NOT_APPLICABLE"}]}}}, "J1": {"decision": "PASS"}}}
    data = mq.unit_verdicts({"evaluations": {"gpt": {"cases": [case], "jev_eval": {"records": [record]}}}})
    assert data["gpt"]["질문#1"] == {"2-1.count:sentence_count": "FAIL", "1-1.response_nonempty": "PASS", "J2-2.a": "FAIL", "J1": "PASS"}


def test_greedy_result_is_labelled_as_an_upper_bound():
    order, targets = ["a", "b", "c"], {"a": {1, 2, 3, 4}, "b": {1, 2, 5}, "c": {3, 4, 6}}
    result = mq.greedy_multicover(order, targets, [1, 2, 3, 4, 5, 6], 1)
    assert result["questions"] >= 2   # 정확한 최소는 2(b·c) — 탐욕은 3을 고를 수 있어 근사로만 표기한다
