"""items 파이프라인의 집계(semantic_items_eval) — 응답 파싱 → '판정하는 곳' 규칙 → 세부 기준·기준 집계 → decisions[cid]+subs·통계. 순수 함수.

설계 '판정·집계': 세부 기준은 적용 항목이 모두 예면 PASS, 하나라도 아니오면 FAIL, 적용 항목 없음은 NOT_APPLICABLE.
기준은 세부 기준이 모두 PASS 면 PASS. 판정에 '모른다' 는 없다 — 응답에 없는 항목은 확인 불가 NO 로 채운다.
"""
import copy

import pytest

from code_eval import semantic_items_eval as ev
from code_eval.jev_criteria import CRITERIA
from code_eval.semantic_items import (FACT_FALSE_CONFIRMED, FACT_UNVERIFIABLE, ITEM_UNVERIFIABLE, OVERRIDES, SOURCE_NOT_FOUND,
                                      SPECS_BY_ID)

NAMES = {c.id: c.name for c in CRITERIA}


def questions(*ids, **overrides):
    out = {item_id: SPECS_BY_ID[item_id].to_prompt_dict({"rules": ["규칙"], "matches": ["근거"]}) for item_id in ids}
    for item_id, extra in overrides.items():
        out[item_id].update(extra)
    return out


def item(item_id, decision="YES", question_quote="q", answer_quote="a", reason="r", **extra):
    return {"id": item_id, "decision": decision, "question_quote": question_quote, "answer_quote": answer_quote, "reason": reason,
            "turn": None, "fact": None, **extra}


def grouped(qs, *items):
    return ev.parse_item_answers(list(items), qs)


def decision_of(items_by_cid, item_id):
    return next(it["decision"] for its in items_by_cid.values() for it in its if it["id"] == item_id)


# --- 파싱 -------------------------------------------------------------------------------------------------

def test_parse_groups_items_by_criterion_in_table_order_and_fills_missing_answers_as_unverifiable_no():
    qs = questions("J4-2.a", "J1-1.a", "J1-4.a", "J2-1.a")
    by_cid = grouped(qs, item("J4-2.a#2"), item("J4-2.a#10", "NO"), item("J4-2.a#1"), item("J1-1.a", "NOT_APPLICABLE"), item("J2-1.a"))
    assert list(by_cid) == ["J1", "J2", "J4"]
    assert [it["id"] for it in by_cid["J4"]] == ["J4-2.a#1", "J4-2.a#2", "J4-2.a#10"]   # 요소 번호는 수로 정렬
    assert by_cid["J4"][0]["item_id"] == "J4-2.a" and by_cid["J4"][0]["element"] == "1" and by_cid["J1"][0]["element"] is None
    filled = next(it for it in by_cid["J1"] if it["id"] == "J1-4.a")
    assert filled["decision"] == "NO" and filled["problems"] == ["missing_answer"] and filled["unverifiable"] is True
    assert filled["reason_code"] == ITEM_UNVERIFIABLE and filled["origin"] == "question" and filled["model_original"] is None
    assert [it["id"] for it in by_cid["J1"]] == ["J1-1.a", "J1-4.a"]
    raw = item("J2-1.a")
    assert grouped(questions("J2-1.a"), raw)["J2"][0] is not raw   # 입력은 복사한다


def test_parse_rejects_unrequested_ids_bad_decisions_and_non_list_bodies():
    qs = questions("J1-1.a")
    with pytest.raises(ValueError, match="보내지 않은 항목"):
        grouped(qs, item("J1-2.a"))
    with pytest.raises(ValueError, match="보내지 않은 항목"):
        grouped(qs, item("X#1"))
    with pytest.raises(ValueError, match="decision"):
        grouped(qs, item("J1-1.a", "PASS"))
    with pytest.raises(ValueError, match="객체가 아님"):
        grouped(qs, "J1-1.a")
    with pytest.raises(ValueError, match="items 배열 없음"):
        ev.parse_item_answers({"J1-1.a": {}}, qs)
    # 기준표에 없는 항목도 questions 가 criterion 을 주면 받는다
    custom = {"X-1.a": {"type": "item", "criterion": "J3", "sub": "X-1", "origin": "question"}}
    assert list(ev.parse_item_answers([item("X-1.a")], custom)) == ["J3"]
    with pytest.raises(ValueError, match="보내지 않은 기준"):
        ev.parse_item_answers([item("X-1.a")], {"X-1.a": {"type": "item", "criterion": "Z9"}})


# --- '판정하는 곳' 규칙 ------------------------------------------------------------------------------------

def test_overrides_table_matches_the_design_rules():
    assert OVERRIDES == (("J1-2.a", "NO", ("J1-1.a",)), ("J1-3", "APPLIED", ("J1-1.a", "J1-2.a")),
                         ("J1-1.a", "NO", ("J4-1.a", "J4-1.b", "J4-2.a", "J4-3.a", "J4-4.a")),
                         ("J2-4.a", "NO", ("J2-4.b",)), ("J6-4.b", "APPLIED", ("J6-4.a",)))


def test_refusal_no_makes_task_item_not_applicable_and_keeps_the_model_decision():
    by_cid = grouped(questions("J1-1.a", "J1-2.a", "J4-2.a"), item("J1-1.a", "NO"), item("J1-2.a", "NO"), item("J4-2.a#1", "YES"))
    applied = ev.apply_overrides(by_cid)
    assert applied == [{"trigger": "J1-2.a", "when": "NO", "targets": ["J1-1.a"]}]
    task = by_cid["J1"][0]
    assert task["id"] == "J1-1.a" and task["decision"] == "NOT_APPLICABLE" and task["model_decision"] == "NO" and task["override"] == "J1-2.a"
    assert task["reason"] == "서버 집계 규칙: J1-2.a 아니오 → J1-1.a 해당 없음 (원래 판정 NO: r)"
    # J1-1.a 가 규칙으로 해당 없음이 된 뒤에는 'J1-1.a 아니오 → J4' 규칙이 연쇄로 발동하지 않는다 (거절은 J1-2 에서만 본다)
    assert decision_of(by_cid, "J4-2.a#1") == "YES" and by_cid["J4"][0]["model_decision"] == "YES" and "override" not in by_cid["J4"][0]
    assert all("model_decision" in it for its in by_cid.values() for it in its)


def test_task_no_makes_all_j4_elements_not_applicable_and_infeasible_request_disables_j1_1_and_j1_2():
    by_cid = grouped(questions("J1-1.a", "J1-2.a", "J4-1.a", "J4-2.a", "J4-4.a"),
                     item("J1-1.a", "NO"), item("J1-2.a", "YES"), item("J4-1.a#1", "YES", reason=""), item("J4-1.a#2", "NO"),
                     item("J4-2.a#1", "NOT_APPLICABLE"), item("J4-4.a#1", "YES"))
    applied = ev.apply_overrides(by_cid)
    assert applied == [{"trigger": "J1-1.a", "when": "NO", "targets": ["J4-1.a#1", "J4-1.a#2", "J4-4.a#1"]}]   # 이미 N/A 인 요소는 건드리지 않음
    assert {it["id"]: it["decision"] for it in by_cid["J4"]} == {"J4-1.a#1": "NOT_APPLICABLE", "J4-1.a#2": "NOT_APPLICABLE",
                                                                  "J4-2.a#1": "NOT_APPLICABLE", "J4-4.a#1": "NOT_APPLICABLE"}
    assert by_cid["J4"][0]["reason"] == "서버 집계 규칙: J1-1.a 아니오 → J4-1.a 해당 없음"   # 원래 reason 이 비면 괄호 없음
    assert "override" not in by_cid["J4"][2]
    infeasible = grouped(questions("J1-1.a", "J1-2.a", "J1-3.a", "J1-3.b"),
                         item("J1-1.a", "NO"), item("J1-2.a", "YES"), item("J1-3.a", "YES"), item("J1-3.b", "NOT_APPLICABLE"))
    assert ev.apply_overrides(infeasible) == [{"trigger": "J1-3", "when": "APPLIED", "targets": ["J1-1.a", "J1-2.a"]}]
    assert {it["id"]: it["decision"] for it in infeasible["J1"]} == {"J1-1.a": "NOT_APPLICABLE", "J1-2.a": "NOT_APPLICABLE",
                                                                      "J1-3.a": "YES", "J1-3.b": "NOT_APPLICABLE"}
    assert infeasible["J1"][0]["reason"] == "서버 집계 규칙: J1-3 적용 → J1-1.a 해당 없음 (원래 판정 NO: r)"
    # J1-3 항목이 모두 NOT_APPLICABLE 이면 '적용' 이 아니다
    untouched = grouped(questions("J1-1.a", "J1-3.a"), item("J1-1.a", "YES"), item("J1-3.a", "NOT_APPLICABLE"))
    assert ev.apply_overrides(untouched) == [] and decision_of(untouched, "J1-1.a") == "YES"
    # J1-3 이 NO(적용됐지만 처리가 나쁨)여도 APPLIED 로 센다
    bad = grouped(questions("J1-1.a", "J1-3.a"), item("J1-1.a", "YES"), item("J1-3.a", "NO"))
    assert ev.apply_overrides(bad)[0]["trigger"] == "J1-3" and decision_of(bad, "J1-1.a") == "NOT_APPLICABLE"


def test_explanation_method_and_pushback_turn_overrides():
    by_cid = grouped(questions("J2-4.a", "J2-4.b"), item("J2-4.a", "NO"), item("J2-4.b", "YES"))
    assert ev.apply_overrides(by_cid) == [{"trigger": "J2-4.a", "when": "NO", "targets": ["J2-4.b"]}]
    assert decision_of(by_cid, "J2-4.b") == "NOT_APPLICABLE"
    kept = grouped(questions("J2-4.a", "J2-4.b"), item("J2-4.a", "YES"), item("J2-4.b", "NO"))
    assert ev.apply_overrides(kept) == [] and decision_of(kept, "J2-4.b") == "NO"
    conv = grouped(questions("J6-4.a", "J6-4.b"), item("J6-4.a", "NO"), item("J6-4.b", "YES"))
    assert ev.apply_overrides(conv) == [{"trigger": "J6-4.b", "when": "APPLIED", "targets": ["J6-4.a"]}]
    assert decision_of(conv, "J6-4.a") == "NOT_APPLICABLE"
    quiet = grouped(questions("J6-4.a", "J6-4.b"), item("J6-4.a", "NO"), item("J6-4.b", "NOT_APPLICABLE"))
    assert ev.apply_overrides(quiet) == [] and decision_of(quiet, "J6-4.a") == "NO"


# --- 세부 기준·기준 판정 ----------------------------------------------------------------------------------

def test_sub_and_criterion_decisions():
    assert ev.sub_decision([]) == "NOT_APPLICABLE" and ev.sub_decision([item("x", "NOT_APPLICABLE")]) == "NOT_APPLICABLE"
    assert ev.sub_decision([item("x", "YES"), item("y", "NOT_APPLICABLE")]) == "PASS"
    assert ev.sub_decision([item("x", "YES"), item("y", "NO")]) == "FAIL"
    assert ev.criterion_decision({}) == (None, "NOT_APPLICABLE")
    assert ev.criterion_decision({"J4-1": {"decision": "NOT_APPLICABLE"}, "J4-2": "NOT_APPLICABLE"}) == (None, "NOT_APPLICABLE")
    assert ev.criterion_decision({"J4-1": {"decision": "PASS"}, "J4-2": {"decision": "NOT_APPLICABLE"}}) == ("PASS", None)
    assert ev.criterion_decision({"J4-1": {"decision": "PASS"}, "J4-2": {"decision": "FAIL"}}) == ("FAIL", None)
    assert ev.criterion_decision({"J4-1": {"decision": None}}) == (None, "NOT_APPLICABLE")


# --- 집계 -------------------------------------------------------------------------------------------------

def test_aggregate_builds_legacy_shaped_decisions_with_ordered_subs_and_question_based_fulfillment():
    qs = questions("J1-1.a", "J1-2.a", "J1-4.a", "J1-4.b", "J4-2.a", "J4-1.a", "J3-1.a")
    by_cid = grouped(qs, item("J1-1.a"), item("J1-2.a"), item("J1-4.a", "NO", reason=" 광고 문구 "), item("J1-4.b", "NO", reason=""),
                     item("J4-2.a#1"), item("J4-2.a#2", "NO", reason="이유 누락"), item("J4-1.a#1", "NOT_APPLICABLE"),
                     item("J3-1.a#1", "NO", reason="모순"))
    ev.apply_overrides(by_cid)
    decisions, stats = ev.aggregate_items(by_cid, qs, applicability={"version": "sub-applicability-v1"},
                                          rechecks={"rounds": 2, "unverifiable": []})
    assert list(decisions) == ["J1", "J3", "J4"]
    j1 = decisions["J1"]
    assert {k: j1[k] for k in ("criterion_id", "name", "decision", "confidence", "probabilities")} == {
        "criterion_id": "J1", "name": NAMES["J1"], "decision": "FAIL", "confidence": None, "probabilities": {}}
    assert "status" not in j1 and "reason_code" not in j1 and "fact_check" not in j1
    assert j1["reason"] == "J1-4.a: 광고 문구 · J1-4.b: 아니오"
    assert list(j1["subs"]) == ["J1-1", "J1-2", "J1-4"]
    assert {sid: sub["decision"] for sid, sub in j1["subs"].items()} == {"J1-1": "PASS", "J1-2": "PASS", "J1-4": "FAIL"}
    assert j1["subs"]["J1-4"]["name"] == "불필요한 내용 없음" and [it["id"] for it in j1["subs"]["J1-4"]["items"]] == ["J1-4.a", "J1-4.b"]
    assert j1["subs"]["J1-4"]["items"][0] is not by_cid["J1"][2]   # 복사본
    j4 = decisions["J4"]
    assert j4["decision"] == "FAIL" and list(j4["subs"]) == ["J4-1", "J4-2"]
    assert j4["subs"]["J4-1"]["decision"] == "NOT_APPLICABLE" and j4["subs"]["J4-2"]["decision"] == "FAIL"
    assert j4["reason"] == "J4-2.a#2: 이유 누락"
    assert decisions["J3"]["decision"] == "FAIL" and decisions["J3"]["subs"]["J3-1"]["items"][0]["origin"] == "answer"
    # 통계: 적용 7 (N/A 1 제외) · 예 3 · 아니오 4 · 질문 기준 항목 6 (J3-1.a 는 답변 기준) · 충족률 3/6
    assert {k: stats[k] for k in ("applied", "yes", "no", "not_applicable", "question_items", "answer_items", "question_yes")} == {
        "applied": 7, "yes": 3, "no": 4, "not_applicable": 1, "question_items": 6, "answer_items": 1, "question_yes": 3}
    assert stats["fulfillment"] == 0.5 and stats["rechecks"] == 2 and stats["unverifiable"] == 0 and stats["overrides"] == []
    assert stats["applicability_version"] == "sub-applicability-v1"


def test_aggregate_marks_all_not_applicable_criteria_and_counts_unverifiable_with_reason_code_priority():
    qs = questions("J2-1.a", "J2-1.b", "J2-5.a", "J5-2.a", "J5-3.a")
    by_cid = grouped(qs, item("J2-1.a", "NOT_APPLICABLE"), item("J2-1.b", "NOT_APPLICABLE"),
                     item("J2-5.a", "NO", reason="확인 불가: 재확인 3회", unverifiable=True, reason_code=ITEM_UNVERIFIABLE),
                     item("J5-2.a#1", "NO", reason="값 오기", unverifiable=True, reason_code=ITEM_UNVERIFIABLE),
                     item("J5-3.a", "NO", reason="출처 없음", reason_code=SOURCE_NOT_FOUND))
    decisions, stats = ev.aggregate_items(by_cid, qs)
    j2 = decisions["J2"]
    assert j2["decision"] == "FAIL" and j2["reason_code"] == ITEM_UNVERIFIABLE and j2["subs"]["J2-1"]["decision"] == "NOT_APPLICABLE"
    j5 = decisions["J5"]
    assert j5["decision"] == "FAIL" and j5["reason_code"] == SOURCE_NOT_FOUND   # 우선순위: 거짓 확인 > 사실 확인 불가 > 출처 없음 > 항목 확인 불가
    assert j5["fact_check"] == {"basis": "none", "claims": []}
    assert stats["unverifiable"] == 2 and stats["fulfillment"] == 0.0
    only_na = grouped(questions("J2-1.a"), item("J2-1.a", "NOT_APPLICABLE"))
    decisions, stats = ev.aggregate_items(only_na, questions("J2-1.a"))
    assert decisions["J2"]["decision"] is None and decisions["J2"]["status"] == "NOT_APPLICABLE" and decisions["J2"]["reason"] == "적용 항목 없음"
    assert stats["fulfillment"] is None and stats["applied"] == 0 and stats["not_applicable"] == 1
    assert "applicability_version" not in stats


def test_aggregate_summary_reason_lists_at_most_three_failures():
    qs = questions("J4-2.a")
    by_cid = grouped(qs, *[item(f"J4-2.a#{n}", "NO", reason=f"누락{n}") for n in range(1, 6)])
    decisions, _ = ev.aggregate_items(by_cid, qs)
    assert decisions["J4"]["reason"] == "J4-2.a#1: 누락1 · J4-2.a#2: 누락2 · J4-2.a#3: 누락3 · 외 2개"
    passed = grouped(qs, item("J4-2.a#1"))
    assert ev.aggregate_items(passed, qs)[0]["J4"]["reason"] == "적용 항목 모두 예"


def test_aggregate_projects_j5_items_into_legacy_fact_check_shape():
    qs = questions("J5-1.a", "J5-2.a")
    false_fact = {"claim": "반감기는 약 12시간", "verdict": "FALSE", "correct": "약 5시간", "search_queries": ["caffeine"],
                  "sources": [{"url": "https://official.example/x", "title": "", "quote": "about 5 hours", "quote_verified": True,
                               "verification_status": "verified"}]}
    true_fact = {"claim": "아데노신 수용체를 막는다", "verdict": "TRUE", "correct": "", "search_queries": ["adenosine"],
                 "sources": [{"url": "https://official.example/y", "quote": "blocks adenosine"}]}
    by_cid = grouped(qs, item("J5-1.a#1", "NO", "", "반감기는 약 12시간", reason="거짓", fact=false_fact, reason_code=FACT_FALSE_CONFIRMED),
                     item("J5-1.a#2", "YES", "", "아데노신 수용체를 막는다", fact=true_fact),
                     item("J5-2.a#1", "YES", "하루 3잔", "하루 3잔이면"))
    decisions, stats = ev.aggregate_items(by_cid, qs)
    j5 = decisions["J5"]
    assert j5["decision"] == "FAIL" and j5["reason_code"] == FACT_FALSE_CONFIRMED
    assert {sid: sub["decision"] for sid, sub in j5["subs"].items()} == {"J5-1": "FAIL", "J5-2": "PASS"}
    assert j5["fact_check"]["basis"] == "web" and [c["verdict"] for c in j5["fact_check"]["claims"]] == ["FAIL", "PASS"]
    first = j5["fact_check"]["claims"][0]
    assert first["claim"] == "반감기는 약 12시간" and first["reason_code"] == FACT_FALSE_CONFIRMED and first["scope"] == "external_fact"
    assert first["sources"][0] == {"url": "https://official.example/x", "title": "", "quote": "about 5 hours", "origin": "web",
                                   "quote_verified": True, "verification_status": "verified"}
    assert "unverifiable" not in first and j5["fact_check"]["claims"][1]["sources"][0]["quote_verified"] is False
    assert stats["answer_items"] == 2 and stats["question_items"] == 1 and stats["fulfillment"] == 1.0
    # J5-1 주장이 하나도 없고(N/A) 값 대조도 없으면 J5 는 해당 없음이며 fact_check 는 비어 있다
    na = grouped(qs, item("J5-1.a", "NOT_APPLICABLE"), item("J5-2.a#1", "NOT_APPLICABLE"))
    decisions, _ = ev.aggregate_items(na, qs)
    assert decisions["J5"]["decision"] is None and decisions["J5"]["status"] == "NOT_APPLICABLE"
    assert decisions["J5"]["fact_check"] == {"basis": "none", "claims": []}
    unverifiable = grouped(qs, item("J5-1.a#1", "NO", "", "반감기", reason="확인 불가", unverifiable=True, reason_code=FACT_UNVERIFIABLE,
                                    fact={**true_fact, "sources": []}))
    decisions, _ = ev.aggregate_items(unverifiable, {"J5-1.a": qs["J5-1.a"]})
    assert decisions["J5"]["reason_code"] == FACT_UNVERIFIABLE and decisions["J5"]["fact_check"]["claims"][0]["unverifiable"] is True


def test_aggregate_keeps_not_sent_items_as_not_run_sub_and_can_create_the_criterion_from_them():
    qs = questions("J5-2.a", "J1-1.a")
    by_cid = grouped(qs, item("J5-2.a#1"), item("J1-1.a"))
    not_sent = [{"id": "J5-1.a", "reason": "검색 도구 없음"}, {"id": "X", "reason": "무시"}]
    decisions, stats = ev.aggregate_items(by_cid, qs, not_sent=not_sent)
    j5 = decisions["J5"]
    assert list(j5["subs"]) == ["J5-1", "J5-2"] and j5["decision"] == "PASS"
    assert j5["subs"]["J5-1"] == {"sub_id": "J5-1", "name": "사실 정확성", "decision": "NOT_APPLICABLE", "items": [],
                                  "not_run": {"items": ["J5-1.a"], "reason": "검색 도구 없음"}}
    assert stats["applied"] == 2
    # 보낸 항목이 하나도 없어도 not_sent 만으로 기준이 생기고 해당 없음으로 남는다
    decisions, _ = ev.aggregate_items({"J1": by_cid["J1"]}, {"J1-1.a": qs["J1-1.a"]}, not_sent=[{"id": "J5-1.a", "reason": "r"}])
    assert list(decisions) == ["J1", "J5"] and decisions["J5"]["status"] == "NOT_APPLICABLE" and decisions["J5"]["decision"] is None
    assert decisions["J5"]["subs"]["J5-1"]["not_run"]["reason"] == "r"


def test_fulfillment_over_records_counts_only_items_pipeline_question_based_applied_items():
    def record(pipeline, decisions):
        return {"pipeline": pipeline, "decisions": decisions}
    j1 = {"subs": {"J1-1": {"items": [item("J1-1.a", "YES", origin="question")]},
                   "J1-4": {"items": [item("J1-4.a", "NO", origin="question"), item("J1-4.b", "NOT_APPLICABLE", origin="question")]}}}
    j3 = {"subs": {"J3-1": {"items": [item("J3-1.a#1", "NO", origin="answer")]}}}
    records = [record("items", {"J1": j1, "J3": j3}), record("items", {"J1": {"subs": {"J1-1": {"items": [item("J1-1.a", "YES")]}}}}),
               record("legacy", {"J1": {"decision": "PASS"}}), "junk", {"pipeline": "items", "decisions": None}]
    rates = ev.fulfillment(records)
    assert {k: rates[k] for k in ("applied", "yes", "rate")} == {"applied": 3, "yes": 2, "rate": 0.6667}
    assert rates["by_criterion"] == {"J1": {"applied": 3, "yes": 2, "rate": 0.6667}}
    assert rates["by_sub"] == {"J1-1": {"applied": 2, "yes": 2, "rate": 1.0}, "J1-4": {"applied": 1, "yes": 0, "rate": 0.0}}
    assert ev.fulfillment([]) == {"applied": 0, "yes": 0, "rate": None, "by_criterion": {}, "by_sub": {}}


def test_aggregate_does_not_mutate_its_inputs():
    qs = questions("J1-1.a", "J1-2.a")
    by_cid = grouped(qs, item("J1-1.a", "NO"), item("J1-2.a", "NO"))
    snapshot = copy.deepcopy(by_cid)
    ev.apply_overrides(by_cid)
    assert by_cid != snapshot   # 규칙 적용은 제자리
    frozen = copy.deepcopy(by_cid)
    decisions, _ = ev.aggregate_items(by_cid, qs)
    decisions["J1"]["subs"]["J1-1"]["items"][0]["decision"] = "YES"
    assert by_cid == frozen
