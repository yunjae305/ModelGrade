"""items 파이프라인의 jev_eval 통합(E2E) — 대역 client 로 요청 조립 → 인용 검증·재확인 → 집계 → 배치 요약까지. 실제 모델 호출 없음.

legacy 흐름(test_jev_eval.py)은 그대로 두고, pipeline="items" 가 ``sub_applicability`` 로 항목을 고르고 ``finalize_items`` 재확인
콜백을 같은 client 로 돌리며, 레코드·배치 결과에 subs·items 통계·정책 버전을 남기는지를 고정한다.
"""
from __future__ import annotations

import copy
from concurrent.futures import CancelledError
from threading import Event

import pytest

from code_eval import engine, jev_eval
from code_eval.combined import combined_summary
from code_eval.config import AionaConfig
from code_eval.jev_applicability import SUB_APPLICABILITY_VERSION
from code_eval.jev_client import JevCall, JevClientConfig, JevStatus
from code_eval.jev_criteria import jev_questions
from code_eval.jev_eval import (CANCEL_REASON, evaluate_jev_batch, evaluate_jev_case, evaluation_log, jev_after_code,
                                jev_request, summarize_records)
from code_eval.semantic_items import ITEM_UNVERIFIABLE, SEMANTIC_ITEMS_VERSION, SPECS_BY_ID, applicable_items
from code_eval.semantic_policy import apply_final_decision_policy
from code_eval.semantic_quotes import ITEM_QUOTE_POLICY, RECHECK_POLICY
from tests.test_jev_eval import CONV, NORMAL, ScriptedJev, jev_body

NO_CONFIG = AionaConfig()
CAFFEINE = {"case_id": "caffeine",
            "question": "저녁에 커피 마시면 잠을 잘 못 자는 이유가 뭐야? 카페인이 몸에 얼마나 오래 남아?",
            "answer": "카페인은 아데노신 수용체를 막아 각성 상태를 유지시킵니다. 성인 기준 반감기는 약 5시간입니다. "
                      "자세한 내용은 출처 https://official.example/caffeine 를 확인했습니다."}


def default_item(item_id, spec, state, *, apply_all=False):
    """항목 계약대로 통과하는 응답 — 펼치기 항목은 요소 1개, 평가 모델이 적용을 정하는 항목·주장별 항목은 NOT_APPLICABLE.

    ``apply_all`` 이면 평가 모델이 적용을 정하는 항목(J1-3·J2-4.b·J3-3.a·J6)도 적용(YES)으로 답한다.
    """
    answer_quote = (state.get("answer") or "")[:8]
    question_quote = (state.get("question") or "")[:6]
    if (spec.get("model_applies") and not apply_all) or spec.get("fact_block"):
        return {"id": item_id, "decision": "NOT_APPLICABLE", "question_quote": "", "answer_quote": "", "reason": "해당 없음",
                "turn": None, "fact": None}
    if spec.get("expand"):
        return {"id": f"{item_id}#1", "decision": "YES", "question_quote": "" if spec.get("origin") == "answer" else question_quote,
                "answer_quote": answer_quote, "reason": "충족", "turn": None, "fact": None}
    return {"id": item_id, "decision": "YES", "question_quote": question_quote, "answer_quote": answer_quote, "reason": "충족",
            "turn": None, "fact": None}


class ScriptedItems:
    """items 계약의 대역 client. ``rounds[n]`` 은 n번째 호출(0 = 본 평가, 1~ = 재확인)에서 항목 id 별로 덮어쓸 값이다."""

    config = JevClientConfig()
    supports_cancellation = True

    def __init__(self, rounds=None, *, not_sent=(), on_call=None, model="fixture-judge", status="OK", apply_all=False):
        self.rounds = rounds or [{}]
        self.not_sent = list(not_sent)
        self.on_call = on_call
        self.apply_all = apply_all
        self.model, self.status = model, status
        self.calls = []

    def system_one(self, state, questions, *, cancel_event=None):
        number = len(self.calls)
        self.calls.append({"state": copy.deepcopy(state), "questions": copy.deepcopy(questions), "cancel_event": cancel_event})
        if self.on_call:
            self.on_call(number, state, questions)
        if self.status != "OK":
            return JevCall(self.status, error="대역 오류", attempts=1, latency_ms=5)
        overrides = self.rounds[min(number, len(self.rounds) - 1)]
        items = []
        for item_id, spec in questions.items():
            if any(entry["id"] == item_id for entry in self.not_sent):
                continue
            built = default_item(item_id, spec, state, apply_all=self.apply_all)
            patch = overrides.get(built["id"], overrides.get(item_id))
            if patch is None:
                items.append(built)
            elif patch == "omit":
                continue
            elif isinstance(patch, list):
                items.extend(patch)
            else:
                items.append({**built, **patch})
        verification = {"search_performed": False, "source_urls": [], "source_validation": "provider"}
        if self.not_sent:
            verification["items_not_sent"] = [entry for entry in self.not_sent if entry["id"] in questions]
        return JevCall("OK", body={"model": self.model, "items": items, "verification": verification}, attempts=1,
                       latency_ms=100, usage={"input_tokens": 100, "output_tokens": 20}, accounting={"cost_parts": [
                           {"model": self.model, "usage": {"input_tokens": 100, "output_tokens": 20}}]})


def run_items(cases, client, **kw):
    batch = engine.evaluate_batch(cases, NO_CONFIG)
    digest = batch["result_digest"]
    jev = evaluate_jev_batch(batch, client, pipeline="items", **kw)
    assert engine.result_digest(batch["cases"]) == digest   # 의미 평가는 CODE 결과를 바꾸지 않는다
    return batch, jev


def sent_ids(call):
    return list(call["questions"])


def sub_decisions(record, cid):
    return {sid: sub["decision"] for sid, sub in record["decisions"][cid]["subs"].items()}


# --- 요청 조립 ----------------------------------------------------------------------------------------------

def test_jev_request_items_sends_only_applicable_items_with_collected_at_and_leaves_legacy_untouched():
    specs = applicable_items(NORMAL)
    request = jev_request(NORMAL["question"], NORMAL["answer"], ("J1", "J2", "J4"), pipeline="items", item_specs=specs,
                          collected_at="2026-10-01")
    assert request["state"] == {"question": NORMAL["question"], "answer": NORMAL["answer"], "collected_at": "2026-10-01"}
    assert list(request["questions"]) == ["J1-1.a", "J1-2.a", "J1-3.a", "J1-3.b", "J1-4.a", "J1-4.b", "J2-1.a", "J2-1.b", "J2-5.a", "J4-2.a"]
    assert all(q["type"] == "item" and q["criterion"] == item_id[:2] for item_id, q in request["questions"].items())
    assert request["questions"]["J2-1.a"]["matches"] == ["대학생이"] and request["questions"]["J4-2.a"]["expand"] == "요구별"
    legacy = jev_request(NORMAL["question"], NORMAL["answer"], ("J1", "J2"), collected_at="2026-10-01")
    assert legacy == {"state": {"question": NORMAL["question"], "answer": NORMAL["answer"]},
                      "questions": {cid: q for cid, q in jev_questions().items() if cid in ("J1", "J2")}}
    assert "collected_at" not in jev_request("q", "a", pipeline="items", item_specs=specs)["state"]


# --- 배치 E2E ------------------------------------------------------------------------------------------------

def test_items_batch_records_decisions_with_subs_stats_and_pipeline_metadata():
    client = ScriptedItems([{"J1-4.a": {"decision": "NO", "reason": "광고 문구", "answer_quote": "레노버 아이디어패드"}}])
    batch, jev = run_items([NORMAL], client, context={"answer_tools": "none", "collected_at": "2026-10-01"})
    assert len(client.calls) == 1   # 인용이 모두 맞으면 재확인 없음
    call = client.calls[0]
    assert call["state"]["collected_at"] == "2026-10-01" and call["cancel_event"] is None
    assert sent_ids(call) == ["J1-1.a", "J1-2.a", "J1-3.a", "J1-3.b", "J1-4.a", "J1-4.b", "J2-1.a", "J2-1.b", "J2-5.a", "J3-1.a",
                              "J3-2.a", "J3-2.b", "J4-2.a"]
    record = jev["records"][0]
    assert record["status"] == JevStatus.SUCCESS and record["pipeline"] == "items" and record["model"] == "fixture-judge"
    assert record["criteria"] == ["J1", "J2", "J3", "J4"] and record["review_required"] is False and record["j5_basis"] is None
    assert {cid: d["decision"] for cid, d in record["decisions"].items()} == {"J1": "FAIL", "J2": "PASS", "J3": "PASS", "J4": "PASS"}
    assert sub_decisions(record, "J1") == {"J1-1": "PASS", "J1-2": "PASS", "J1-3": "NOT_APPLICABLE", "J1-4": "FAIL"}
    j14 = record["decisions"]["J1"]["subs"]["J1-4"]["items"][0]
    assert j14["decision"] == "NO" and j14["model_decision"] == "NO" and j14["quote_check"]["answer"]["ok"] is True and j14["origin"] == "question"
    assert record["decisions"]["J1"]["reason"] == "J1-4.a: 광고 문구" and record["decisions"]["J1"]["confidence"] is None
    assert record["items"]["applied"] == 11 and record["items"]["yes"] == 10 and record["items"]["no"] == 1 and record["items"]["not_applicable"] == 2
    assert record["items"]["question_items"] == 10 and record["items"]["answer_items"] == 1 and record["items"]["fulfillment"] == 0.9
    assert record["items"]["rechecks"] == 0 and record["items"]["unverifiable"] == 0 and record["items"]["overrides"] == []
    assert record["items"]["applicability_version"] == SUB_APPLICABILITY_VERSION
    assert record["recheck"] == {"policy": RECHECK_POLICY, "rounds": 0, "calls": [], "unverifiable": []}
    assert record["verification"]["quote_policy"] == ITEM_QUOTE_POLICY and record["verification"]["usage_requests"][0]["stage"] == "evaluation"
    assert "decision_policy" not in record and apply_final_decision_policy(record)["changed"] is False
    applicability = record["applicability"]
    assert applicability["version"] == SUB_APPLICABILITY_VERSION and applicability["criteria"] == ["J1", "J2", "J3", "J4"]
    assert applicability["subs"]["J2-1"]["matches"] == ["대학생이"] and applicability["subs"]["J5-1"]["items"] == []
    assert applicability["not_applicable"]["J5"].startswith("J5-1: 요청 문장에 사실 질의 표현 없음")
    assert "J6" in applicability["not_applicable"] and "J5" not in applicability["evidence"]
    assert applicability["evidence"]["J1"]["rules"] == ["요청"] and set(applicability["evidence"]) == {"J1", "J2", "J3", "J4"}   # legacy 근거는 그대로
    # 배치 결과의 방식·정책 기록과 요약
    assert jev["pipeline"] == "items" and jev["items_version"] == SEMANTIC_ITEMS_VERSION
    assert jev["quote_policy"] == ITEM_QUOTE_POLICY and jev["recheck_policy"] == RECHECK_POLICY
    assert jev["applicability_version"] == SUB_APPLICABILITY_VERSION and jev["status"] == "SUCCESS" and jev["completed"] == 1
    assert jev["criteria"][4]["applies_when"].startswith("코드 규칙이 사실 정보를 묻는 질문으로 판정한 질문(J5-1)")
    assert jev["summary"]["J1"]["pass"] == 0 and jev["summary"]["J1"]["fail"] == 1 and jev["summary"]["J1"]["targets"] == 1
    assert jev["summary"]["J1"]["subs"]["J1-4"] == {"name": "불필요한 내용 없음", "targets": 1, "pass": 0, "fail": 1, "not_applicable": 0}
    assert jev["summary"]["J1"]["subs"]["J1-3"]["not_applicable"] == 1 and jev["summary"]["J5"]["targets"] == 0 and "subs" in jev["summary"]["J5"]
    assert jev["summary"]["J1"]["fulfillment"] == {"applied": 4, "yes": 3, "rate": 0.75}
    assert jev["fulfillment"]["applied"] == 10 and jev["fulfillment"]["yes"] == 9 and jev["fulfillment"]["by_sub"]["J1-4"]["rate"] == 0.5
    assert "J3-1" not in jev["fulfillment"]["by_sub"]   # 답변에서 생긴 항목(J3-1.a)은 충족률 분모가 아니다
    # 운영 로그·결합 요약에 subs 가 실린다
    batch["jev_eval"] = jev
    log = evaluation_log(batch)[0]["jev"]
    assert log["pipeline"] == "items" and log["items"]["fulfillment"] == 0.9 and log["recheck"]["rounds"] == 0
    assert list(log["decisions"]["J1"]["subs"]) == ["J1-1", "J1-2", "J1-3", "J1-4"]
    problems = combined_summary(batch)["problems"]
    semantic = [it for it in problems[0]["items"] if it["source"] == "의미 평가"]
    assert semantic[0]["id"] == "J1" and sub_decisions({"decisions": {"J1": semantic[0]}}, "J1")["J1-4"] == "FAIL"


def test_items_recheck_asks_only_failed_items_with_the_previous_judgement_and_sums_usage():
    client = ScriptedItems([{"J4-2.a": {"answer_quote": "답변에 없는 문장"}}, {}])
    batch, jev = run_items([NORMAL], client)
    assert len(client.calls) == 2
    first, second = client.calls
    assert second["state"] == first["state"] and sent_ids(second) == ["J4-2.a"]
    recheck = second["questions"]["J4-2.a"]["recheck"]
    assert recheck["round"] == 1 and [p["code"] for p in recheck["problems"]] == ["answer_quote_not_found"]
    assert recheck["previous"][0]["answer_quote"] == "답변에 없는 문장" and recheck["previous"][0]["id"] == "J4-2.a#1"
    record = jev["records"][0]
    assert record["status"] == JevStatus.SUCCESS and record["decisions"]["J4"]["decision"] == "PASS"
    fixed = record["decisions"]["J4"]["subs"]["J4-2"]["items"][0]
    assert fixed["decision"] == "YES" and fixed["rechecks"][0]["round"] == 1 and fixed["rechecks"][0]["replaced"]["answer_quote"] == "답변에 없는 문장"
    assert record["items"]["rechecks"] == 1 and record["recheck"]["rounds"] == 1 and record["recheck"]["unverifiable"] == []
    assert record["recheck"]["calls"] == [{"round": 1, "items": ["J4-2.a"], "status": "OK", "attempts": 1, "error": None}]
    assert record["attempts"] == 2 and record["usage"] == {"input_tokens": 200, "output_tokens": 40} and record["latency_ms"] == 200
    assert [part["stage"] for part in record["verification"]["usage_requests"]] == ["evaluation", "recheck#1"]


def test_items_recheck_exhausted_after_three_rounds_fails_the_item_as_unverifiable():
    client = ScriptedItems([{"J2-1.a": {"answer_quote": "원문에 없음"}}])   # 매번 같은 잘못된 인용
    batch, jev = run_items([NORMAL], client)
    assert len(client.calls) == 4 and all(sent_ids(call) == ["J2-1.a"] for call in client.calls[1:])
    assert [call["questions"]["J2-1.a"]["recheck"]["round"] for call in client.calls[1:]] == [1, 2, 3]
    record = jev["records"][0]
    j2 = record["decisions"]["J2"]
    assert j2["decision"] == "FAIL" and j2["reason_code"] == ITEM_UNVERIFIABLE and sub_decisions(record, "J2") == {"J2-1": "FAIL", "J2-5": "PASS"}
    bad = j2["subs"]["J2-1"]["items"][0]
    assert bad["decision"] == "NO" and bad["unverifiable"] is True and bad["model_original"]["decision"] == "YES"
    assert bad["reason"].startswith("확인 불가: 재확인 3회 뒤에도 확인하지 못함 — answer_quote 가 답변 원문에 없습니다.")
    assert record["items"]["unverifiable"] == 1 and record["recheck"]["unverifiable"] == ["J2-1.a"] and record["recheck"]["rounds"] == 3
    assert record["review_required"] is False and jev["review_required"] == 0 and jev["unverified"] == 0   # '모른다' 상태는 없다
    assert jev["summary"]["J2"] == {**jev["summary"]["J2"], "pass": 0, "fail": 1, "pending": 0}
    assert jev["status"] == "SUCCESS"


def test_items_recheck_errors_consume_a_round_and_missing_answers_become_unverifiable():
    def flaky(number, state, questions):
        if number == 1:
            raise RuntimeError("일시 오류")
    client = ScriptedItems([{"J1-2.a": "omit"}], on_call=flaky)   # 본 평가에서 J1-2.a 를 빼먹고, 1회차 재확인은 예외
    batch, jev = run_items([NORMAL], client)
    assert len(client.calls) == 4 and [call["questions"]["J1-2.a"]["recheck"]["problems"][0]["code"] for call in client.calls[1:]] == ["item_missing"] * 3
    record = jev["records"][0]
    calls = record["recheck"]["calls"]
    assert calls[0]["status"] == "API_ERROR" and "RuntimeError" in calls[0]["error"] and calls[1]["status"] == "OK"
    missing = record["decisions"]["J1"]["subs"]["J1-2"]["items"][0]
    assert missing["decision"] == "NO" and missing["unverifiable"] is True and missing["model_original"] is None
    assert record["decisions"]["J1"]["decision"] == "FAIL" and record["decisions"]["J1"]["reason_code"] == ITEM_UNVERIFIABLE
    task = record["decisions"]["J1"]["subs"]["J1-1"]["items"][0]   # 확인 불가 NO 도 'J1-2.a 아니오 → J1-1.a 해당 없음' 규칙을 발동한다
    assert task["override"] == "J1-2.a" and task["decision"] == "NOT_APPLICABLE" and task["model_decision"] == "YES"
    assert record["items"]["overrides"] == [{"trigger": "J1-2.a", "when": "NO", "targets": ["J1-1.a"]}]


def test_items_not_sent_fact_item_is_recorded_as_not_run_and_j5_3_applies_from_the_answer():
    not_sent = [{"id": "J5-1.a", "reason": "검색 도구 없음 — 보내지 않음"}]
    client = ScriptedItems(not_sent=not_sent)
    batch, jev = run_items([CAFFEINE], client, context={"answer_tools": "none"})
    call = client.calls[0]
    assert "J5-1.a" in sent_ids(call) and "J5-3.a" in sent_ids(call) and call["questions"]["J5-1.a"]["needs_search"] is True
    assert call["questions"]["J5-3.a"]["answer_tools"] == "none" and call["questions"]["J5-3.a"]["source"] == "answer"
    record = jev["records"][0]
    j5 = record["decisions"]["J5"]
    assert j5["decision"] == "PASS" and sub_decisions(record, "J5") == {"J5-1": "NOT_APPLICABLE", "J5-3": "PASS"}
    assert j5["subs"]["J5-1"]["not_run"] == {"items": ["J5-1.a"], "reason": "검색 도구 없음 — 보내지 않음"}
    assert j5["fact_check"] == {"basis": "none", "claims": []} and record["j5_basis"] is None
    assert "J5" in record["criteria"] and record["verification"]["items_not_sent"] == not_sent
    assert jev["summary"]["J5"]["subs"]["J5-1"]["not_applicable"] == 1 and jev["summary"]["J5"]["pass"] == 1
    unknown = ScriptedItems(not_sent=not_sent)
    run_items([CAFFEINE], unknown, context={"answer_tools": "unknown"})
    assert "J5-3.a" in sent_ids(unknown.calls[0])   # 출처 언급은 도구 불명이어도 적용된다
    assert unknown.calls[0]["questions"]["J5-3.a"]["answer_tools"] == "unknown"


def test_items_cancel_during_recheck_yields_skipped_record_and_partial_batch():
    cancel = Event()

    def stop(number, state, questions):
        if number == 1:
            cancel.set()
    client = ScriptedItems([{"J4-2.a": {"answer_quote": "없는 문장"}}], on_call=stop)
    batch, jev = run_items([NORMAL], client, cancel_event=cancel)
    record = jev["records"][0]
    assert record["status"] == JevStatus.SKIPPED and record["error"] == CANCEL_REASON and record["decisions"] is None
    assert jev["status"] == "PARTIAL" and len(client.calls) == 2
    assert client.calls[0]["cancel_event"] is cancel   # cancel_event 를 지원하는 client 에는 넘긴다
    already = Event()
    already.set()
    skipped = evaluate_jev_case("x", NORMAL["question"], NORMAL["answer"], ScriptedItems(), criteria=("J1",), cancel_event=already,
                                pipeline="items", item_specs=applicable_items(NORMAL))
    assert skipped["status"] == JevStatus.SKIPPED and skipped["j5_basis"] is None and "pipeline" not in skipped


def test_items_conversation_sends_history_with_j6_items_and_marks_missing_history_unverified():
    client = ScriptedItems(apply_all=True)   # J6 항목은 평가 모델이 적용을 정한다 — 대역은 적용(YES)으로 답한다
    batch, jev = run_items([CONV[2], CONV[0], CONV[1]], client)
    by_q = {call["state"]["question"]: call for call in client.calls}
    first, second, third = by_q[CONV[0]["question"]], by_q[CONV[1]["question"]], by_q[CONV[2]["question"]]
    assert "history" not in first["state"] and not any(item_id.startswith("J6") for item_id in sent_ids(first))
    assert second["state"]["history"] == [{"turn": 1, "question": CONV[0]["question"], "answer": CONV[0]["answer"]}]
    assert second["state"]["reference"] == {"course_length_km": 3}
    assert [h["turn"] for h in third["state"]["history"]] == [1, 2] and any(item_id.startswith("J6") for item_id in sent_ids(third))
    records = {r["case_id"]: r for r in jev["records"]}
    assert "J6" not in records["C1-T01"]["criteria"] and records["C1-T01"]["applicability"]["not_applicable"]["J6"]
    assert "J6" in records["C1-T03"]["criteria"] and records["C1-T03"]["decisions"]["J6"]["decision"] == "PASS"
    assert records["C1-T03"]["context"]["history_turns"] == 2
    assert jev["conversations"]["count"] == 1 and jev["conversations"]["items"][0]["result"] == "PASS"
    # 앞 턴이 빠진 대화: J6 는 묻지 않고 UNVERIFIED 로 남긴다 (legacy 와 같은 처리)
    broken = ScriptedItems(apply_all=True)
    batch, jev = run_items([CONV[0], CONV[2]], broken)
    assert not any(item_id.startswith("J6") for item_id in sent_ids(broken.calls[-1]))   # 빠진 앞 턴을 지어내지 않는다
    record = next(r for r in jev["records"] if r["case_id"] == "C1-T03")
    assert record["decisions"]["J6"]["status"] == "UNVERIFIED" and "J6" in record["not_requested"] and record["review_required"] is True
    assert record["decisions"]["J6"]["reason"].startswith("앞 대화 이력을 확인할 수 없습니다: 누락 2턴")
    assert jev["conversations"]["items"][0]["result"] == "INCOMPLETE" and jev["status"] == "PARTIAL"


def test_items_transport_failures_and_missing_configuration_stay_evaluation_errors():
    failed = ScriptedItems(status=JevStatus.TIMEOUT)
    batch, jev = run_items([NORMAL], failed)
    record = jev["records"][0]
    assert record["status"] == JevStatus.TIMEOUT and record["error"] == "대역 오류" and record["decisions"] is None and "pipeline" not in record
    assert jev["status"] == JevStatus.TIMEOUT and jev["pipeline"] == "items" and jev["fulfillment"]["applied"] == 0

    class Broken(ScriptedItems):
        def system_one(self, state, questions, *, cancel_event=None):
            raise RuntimeError("socket closed")
    batch, jev = run_items([NORMAL], Broken())
    assert jev["records"][0]["status"] == JevStatus.API_ERROR and "RuntimeError" in jev["records"][0]["error"]
    batch = engine.evaluate_batch([NORMAL], NO_CONFIG)
    jev = jev_after_code(batch, resolver=lambda **_: (None, None, "키 없음"), pipeline="items")
    assert jev["records"][0]["status"] == JevStatus.NOT_CONFIGURED and jev["access_reason"] == "키 없음" and jev["pipeline"] == "items"
    assert jev["records"][0]["applicability"]["version"] == SUB_APPLICABILITY_VERSION   # 호출 없이도 적용 판정은 남는다
    with pytest.raises(ValueError, match="legacy 또는 items"):
        jev_after_code(batch, resolver=lambda **_: (None, None, None), pipeline="fast")
    with pytest.raises(ValueError, match="legacy 또는 items"):
        evaluate_jev_batch(batch, None, pipeline="fast")


def test_legacy_pipeline_is_unchanged_by_items_support():
    client = ScriptedJev()
    batch = engine.evaluate_batch([NORMAL], NO_CONFIG)
    jev = evaluate_jev_batch(batch, client)
    request = client.requests[0]
    assert request["questions"] == {cid: q for cid, q in jev_questions().items() if cid in request["questions"]}
    assert set(request["state"]) == {"question", "answer"}
    record = jev["records"][0]
    assert "pipeline" not in record and "items" not in record and "subs" not in record["decisions"]["J1"]
    assert jev["pipeline"] == "legacy" and jev["items_version"] is None and jev["quote_policy"] is None and jev["recheck_policy"] is None
    assert "fulfillment" not in jev and "subs" not in jev["summary"]["J1"] and "fulfillment" not in jev["summary"]["J1"]
    assert jev["applicability_version"] == "jev-applicability-v2.2"
    batch["jev_eval"] = jev
    assert "pipeline" not in evaluation_log(batch)[0]["jev"]
    # pipeline 인자 없이 context 를 넘겨도 legacy 는 그것을 쓰지 않는다
    again = evaluate_jev_batch(batch, ScriptedJev(), context={"answer_tools": "none", "collected_at": "2026-10-01"})
    assert again["records"][0]["decisions"] == record["decisions"]
    summarized = summarize_records(jev["records"], {}, batch["cases"])
    assert "fulfillment" not in summarized and summarized["completed"] == 1
