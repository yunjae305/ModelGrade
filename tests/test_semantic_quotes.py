"""items 파이프라인의 인용 검증·재확인·요소 대조(semantic_quotes) — 순수 함수, 모델·네트워크 호출 없음."""
import copy
from concurrent.futures import CancelledError
from threading import Event

import pytest

from code_eval import semantic_evidence, semantic_quotes as sq
from code_eval.jev_client import JevCall
from code_eval.semantic_items import SPECS_BY_ID

QUESTION = "출퇴근용으로 전기자전거, 전동킥보드, 일반 자전거 중 뭐가 나을지 비교해 줘. 편도 6km 정도고 언덕이 좀 있어."
ANSWER = ("언덕이 있는 6km 출퇴근이라면 매일 타도 부담이 없는지가 핵심입니다.\n"
          "- **전기자전거**는 모터 보조 덕분에 언덕에서도 땀을 거의 흘리지 않습니다.\n"
          "- 전동킥보드는 가볍고 보관이 쉽지만 작은 바퀴 탓에 노면 충격이 큽니다.\n"
          "정리하면 전기자전거와 전동킥보드 중에서는 전기자전거가 출퇴근에 더 맞습니다.")
HISTORY = [{"turn": 1, "question": "지금부터 헬스장 안내 데스크 직원이라고 생각하고 존댓말로 답해 주세요.", "answer": "네, 안내 데스크입니다."},
           {"turn": 2, "question": "PT 가격은 얼마예요?", "answer": "10회 50만원입니다."}]
STATE = {"question": QUESTION, "answer": ANSWER}


def questions(*ids):
    return {item_id: SPECS_BY_ID[item_id].to_prompt_dict({"rules": ["규칙"], "matches": ["근거"]}) for item_id in ids}


def item(item_id, decision="YES", question_quote="", answer_quote="", reason="r", **extra):
    return {"id": item_id, "decision": decision, "question_quote": question_quote, "answer_quote": answer_quote, "reason": reason, **extra}


def body(*items, verification=None):
    return {"model": "fixture-judge", "items": list(items), "verification": verification if verification is not None else {}}


def codes(failures, item_id=None):
    return [f["code"] for f in failures if item_id is None or f["id"] == item_id]


def test_normalized_locate_ignores_whitespace_markdown_list_markers_and_punctuation():
    assert sq.locate_quote("**정원은** 12명이며", "안내: 정원은 12명이며 사전 신청") == (4, 13)
    assert sq.locate_quote("- 첫째, 둘째", "목록:\n- 첫째,\n- 둘째") == (6, 14)
    text = "안내: 정원은  12명이며\n사전 신청"
    start, end = sq.locate_quote("정원은 12명이며 사전", text)
    assert text[start:end] == "정원은  12명이며\n사전"
    assert sq.locate_quote("3.5시간", "약 35시간") is None and sq.locate_quote("35시간", "약 3.5시간") is None
    assert sq.locate_quote("10:30", "1030분") is None
    assert sq.locate_quote("", "원문") is None and sq.locate_quote("x", "") is None and sq.locate_quote(None, "원문") is None
    assert sq.locate_quote("ＡＢＣ", "abc!") == (0, 3) and sq.locate_quote("Hello", "hello world") == (0, 5)
    normalized, offsets = sq.normalize_text("a, b\t1,200")
    assert normalized == "ab1,200" and offsets == [0, 3, 5, 6, 7, 8, 9]
    assert sq.normalize_text(12) == ("", [])
    assert sq.ITEM_QUOTE_POLICY == "items-normalized-quotes-v2" and sq.RECHECK_POLICY == "items-recheck-3-then-fail-v1" and sq.MAX_RECHECK_ROUNDS == 3


def test_yes_requires_answer_quote_and_no_allows_empty_only_for_missing_type_items():
    qs = questions("J4-2.a", "J2-3.a", "J1-4.a", "J1-3.a")
    checked, failures = sq.verify_items(body(
        item("J4-2.a#1", "YES", "뭐가 나을지", ""),                       # YES 에는 answer_quote 필수
        item("J2-3.a#1", "NO", "편도 6km", ""),                            # 비누락형 NO 에도 필수
        item("J1-4.a", "NO", "", ""),                                      # J1-4.a 는 비누락형
        item("J1-3.a", "NOT_APPLICABLE", "", ""),                          # 평가 모델이 적용을 정하는 항목의 N/A 는 인용 없어도 통과
    ), STATE, qs)
    assert codes(failures, "J4-2.a#1") == ["answer_quote_required"] and codes(failures, "J2-3.a#1") == ["answer_quote_required"]
    assert codes(failures, "J1-4.a") == ["answer_quote_required"] and codes(failures, "J1-3.a") == []
    missing_ok, failures = sq.verify_items(body(item("J4-2.a#1", "NO", "뭐가 나을지", "")), STATE, questions("J4-2.a"))
    assert failures == [] and missing_ok["items"][0]["quote_check"]["answer"] == {"ok": False, "span": None, "required": False}
    assert missing_ok["items"][0]["origin"] == "question"
    _, failures = sq.verify_items(body(item("J1-4.a", "YES", "", "답변에 없는 문장")), STATE, questions("J1-4.a"))
    assert codes(failures) == ["answer_quote_not_found"]
    found, failures = sq.verify_items(body(item("J1-4.a", "YES", "", "**전기자전거**는 모터 보조 덕분에")), STATE, questions("J1-4.a"))
    assert failures == [] and found["items"][0]["quote_check"]["answer"]["ok"] is True
    start, end = found["items"][0]["quote_check"]["answer"]["span"]
    assert ANSWER[start:end] == "전기자전거**는 모터 보조 덕분에"   # 원문 위치는 첫 글자부터 (앞의 표식은 제외)
    _, failures = sq.verify_items(body(item("J1-4.a", "MAYBE", "", "전기자전거")), STATE, questions("J1-4.a"))
    assert codes(failures) == ["decision_invalid"]


def test_question_quote_must_be_in_question_or_identified_history_turn():
    state = {**STATE, "history": HISTORY}
    checked, failures = sq.verify_items(body(item("J2-3.a#1", "YES", "편도 6km 정도고", "전동킥보드는 가볍고")), state, questions("J2-3.a"))
    span = [QUESTION.index("편도 6km 정도고"), QUESTION.index("편도 6km 정도고") + len("편도 6km 정도고")]
    assert failures == [] and checked["items"][0]["quote_check"]["question"] == {"ok": True, "source": "question", "turn": None, "span": span}
    checked, failures = sq.verify_items(body(item("J6-1.a#1", "YES", "존댓말로 답해 주세요", "전동킥보드는 가볍고", turn=1)), state, questions("J6-1.a"))
    assert failures == [] and checked["items"][0]["quote_check"]["question"]["source"] == "history" and checked["items"][0]["quote_check"]["question"]["turn"] == 1
    _, failures = sq.verify_items(body(item("J6-1.a#1", "YES", "존댓말로 답해 주세요", "전동킥보드는 가볍고", turn=2)), state, questions("J6-1.a"))
    assert codes(failures) == ["question_quote_not_found"]
    _, failures = sq.verify_items(body(item("J6-1.a#1", "YES", "존댓말로 답해 주세요", "전동킥보드는 가볍고")), state, questions("J6-1.a"))
    assert codes(failures) == ["question_quote_not_found"]   # turn 없이 history 인용은 받지 않는다
    _, failures = sq.verify_items(body(item("J4-1.a#2", "NO", "", "")), STATE, questions("J4-1.a"))
    assert codes(failures) == ["question_quote_required"]
    _, failures = sq.verify_items(body(item("J1-4.a", "YES", "질문에 없는 문장", "전동킥보드는 가볍고")), STATE, questions("J1-4.a"))
    assert codes(failures) == ["question_quote_not_found"]
    with_reference = {**STATE, "reference": {"population": 51550000}}
    _, failures = sq.verify_items(body(item("J5-2.a#1", "YES", "51550000", "전동킥보드는 가볍고")), with_reference, questions("J5-2.a"))
    assert failures == []


def test_unrequested_items_are_dropped_and_missing_items_are_rechecked():
    qs = questions("J1-4.a", "J4-1.a")
    checked, failures = sq.verify_items(body(
        item("J3-1.a", "YES", "", "전동킥보드는 가볍고", quote_check={"forged": True}, origin="answer"),
        item("J4-1.a#1", "YES", "전기자전거", "전기자전거는 모터 보조", quote_check={"forged": True}, origin="answer")), STATE, qs)
    assert [it["id"] for it in checked["items"]] == ["J4-1.a#1"] and checked["items"][0]["origin"] == "question"
    assert "forged" not in str(checked["items"][0]["quote_check"])
    assert [(f["id"], f["code"]) for f in failures] == [("J3-1.a", "unrequested_item"), ("J1-4.a", "item_missing")]
    recheck = sq.recheck_questions(qs, failures, 1)
    assert list(recheck) == ["J1-4.a"] and recheck["J1-4.a"]["recheck"]["round"] == 1
    assert "원문에 없" in recheck["J1-4.a"]["recheck"]["notice"] and recheck["J1-4.a"]["recheck"]["previous"] == []
    assert recheck["J1-4.a"]["recheck"]["problems"] == [{"id": "J1-4.a", "code": "item_missing", "message": sq.FAILURE_MESSAGES["item_missing"]}]
    assert recheck["J1-4.a"]["question"] == qs["J1-4.a"]["question"] and "recheck" not in qs["J1-4.a"]
    _, failures = sq.verify_items(body(item("J4-1.a#1", "YES", "전기자전거", "없는 인용")), STATE, qs)
    recheck = sq.recheck_questions(qs, failures, 2, notice="다시")
    assert recheck["J4-1.a"]["recheck"]["previous"] == [{"id": "J4-1.a#1", "decision": "YES", "question_quote": "전기자전거", "answer_quote": "없는 인용", "reason": "r"}]
    assert recheck["J4-1.a"]["recheck"]["notice"] == "다시"


class Ask:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, subset):
        self.calls.append(copy.deepcopy(subset))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def initial(*items):
    return JevCall("OK", body=body(*items, verification={"usage_requests": [{"model": "fixture-judge", "usage": {"input_tokens": 100}}]}),
                   attempts=1, usage={"input_tokens": 100, "output_tokens": 10}, latency_ms=50, http_status=200)


def follow(*items, attempts=1, usage=None):
    return JevCall("OK", body=body(*items, verification={"usage_requests": [{"model": "fixture-judge", "usage": {"input_tokens": 40}}]}),
                   attempts=attempts, usage=usage or {"input_tokens": 40, "output_tokens": 4}, latency_ms=20)


def test_recheck_loop_resolves_on_second_round_and_records_calls_and_usage():
    qs = questions("J4-1.a", "J1-4.a")
    ask = Ask(follow(item("J4-1.a#1", "YES", "전기자전거", "또 없는 인용")),
              follow(item("J4-1.a#1", "YES", "전기자전거", "전기자전거는 모터 보조"), attempts=2))
    call = initial(item("J4-1.a#1", "YES", "전기자전거", "없는 인용"), item("J1-4.a", "YES", "", "전동킥보드는 가볍고"))
    snapshot = copy.deepcopy(call.body)
    done = sq.finalize_items(call, STATE, qs, ask=ask)
    assert call.body == snapshot and done.status == "OK"
    rechecks = done.body["verification"]["rechecks"]
    assert rechecks["policy"] == "items-recheck-3-then-fail-v1" and rechecks["rounds"] == 2 and rechecks["unverifiable"] == []
    assert [(c["round"], c["status"], c["attempts"], c["items"]) for c in rechecks["calls"]] == [(1, "OK", 1, ["J4-1.a"]), (2, "OK", 2, ["J4-1.a"])]
    assert done.attempts == 1 + 1 + 2 and done.usage == {"input_tokens": 180, "output_tokens": 18} and done.latency_ms == 90
    assert [p["stage"] for p in done.body["verification"]["usage_requests"]] == ["evaluation", "recheck#1", "recheck#2"]
    assert [sorted(c) for c in ask.calls] == [["J4-1.a"], ["J4-1.a"]] and ask.calls[1]["J4-1.a"]["recheck"]["round"] == 2
    fixed = next(it for it in done.body["items"] if it["id"] == "J4-1.a#1")
    assert fixed["decision"] == "YES" and "unverifiable" not in fixed and fixed["quote_check"]["answer"]["ok"] is True
    assert [r["round"] for r in fixed["rechecks"]] == [1, 2] and fixed["rechecks"][0]["replaced"]["answer_quote"] == "없는 인용"
    untouched = next(it for it in done.body["items"] if it["id"] == "J1-4.a")
    assert "rechecks" not in untouched and done.body["verification"]["quote_policy"] == "items-normalized-quotes-v2"


def test_three_failed_rounds_end_as_no_with_unverifiable_code_distinct_from_defect_fail():
    qs = questions("J4-1.a", "J1-4.a")
    ask = Ask(*[follow(item("J4-1.a#1", "YES", "전기자전거", "계속 없는 인용")) for _ in range(3)])
    done = sq.finalize_items(initial(item("J4-1.a#1", "YES", "전기자전거", "없는 인용"), item("J1-4.a", "NO", "", "전동킥보드는 가볍고")),
                             STATE, qs, ask=ask)
    assert len(ask.calls) == 3
    bad = next(it for it in done.body["items"] if it["id"] == "J4-1.a#1")
    assert bad["decision"] == "NO" and bad["unverifiable"] is True and bad["reason_code"] == "ITEM_UNVERIFIABLE"
    assert bad["reason"].startswith("확인 불가") and "answer_quote" in bad["reason"]
    assert bad["model_original"] == {"id": "J4-1.a#1", "decision": "YES", "question_quote": "전기자전거", "answer_quote": "계속 없는 인용", "reason": "r"}
    assert len(bad["rechecks"]) == 3
    normal_no = next(it for it in done.body["items"] if it["id"] == "J1-4.a")
    assert normal_no["decision"] == "NO" and "unverifiable" not in normal_no and "reason_code" not in normal_no
    assert done.body["verification"]["rechecks"]["rounds"] == 3 and done.body["verification"]["rechecks"]["unverifiable"] == ["J4-1.a#1"]
    assert done.attempts == 4


def test_transport_error_consumes_the_round_and_cancel_propagates_without_changing_body():
    qs = questions("J1-4.a")
    ask = Ask(JevCall("TIMEOUT", error="fixture timeout"), RuntimeError("boom"), JevCall("API_ERROR", error="500"))
    call = initial(item("J1-4.a", "YES", "", "없는 인용"))
    done = sq.finalize_items(call, STATE, qs, ask=ask)
    calls = done.body["verification"]["rechecks"]["calls"]
    assert [c["status"] for c in calls] == ["TIMEOUT", "API_ERROR", "API_ERROR"] and calls[1]["error"] == "RuntimeError: boom"
    assert len(ask.calls) == 3 and done.body["items"][0]["unverifiable"] is True and done.body["items"][0]["decision"] == "NO"
    assert done.attempts == 1 and done.usage == {"input_tokens": 100, "output_tokens": 10}
    # 재확인이 TIMEOUT 뒤 다음 회차에서 고쳐지면 정상 판정
    ask = Ask(JevCall("TIMEOUT", error="fixture timeout"), follow(item("J1-4.a", "YES", "", "전동킥보드는 가볍고")))
    done = sq.finalize_items(initial(item("J1-4.a", "YES", "", "없는 인용")), STATE, qs, ask=ask)
    assert done.body["items"][0]["decision"] == "YES" and "unverifiable" not in done.body["items"][0]
    assert done.body["verification"]["rechecks"]["rounds"] == 2
    # 취소: CancelledError 전파, 원본 body 불변
    cancel = Event()
    cancel.set()
    call = initial(item("J1-4.a", "YES", "", "없는 인용"))
    snapshot = copy.deepcopy(call.body)
    with pytest.raises(CancelledError):
        sq.finalize_items(call, STATE, qs, ask=Ask(), cancel_event=cancel)
    assert call.body == snapshot
    ask = Ask(JevCall("TIMEOUT", error="fixture timeout"))
    with pytest.raises(CancelledError):
        def cancelling(subset):
            cancel_mid.set()
            return ask(subset)
        cancel_mid = Event()
        sq.finalize_items(initial(item("J1-4.a", "YES", "", "없는 인용")), STATE, qs, ask=cancelling, cancel_event=cancel_mid)
    # 실패가 없으면 ask 를 부르지 않고 그대로
    ask = Ask()
    done = sq.finalize_items(initial(item("J1-4.a", "YES", "", "전동킥보드는 가볍고")), STATE, qs, ask=ask)
    assert ask.calls == [] and done.body["verification"]["rechecks"] == {"policy": "items-recheck-3-then-fail-v1", "rounds": 0, "calls": [], "unverifiable": []}
    assert sq.finalize_items(JevCall("TIMEOUT", error="x"), STATE, qs, ask=ask).status == "TIMEOUT"


def test_j5_item_false_verdict_requires_server_verified_rebuttal_source():
    qs = questions("J5-1.a")
    fact = {"claim": "전동킥보드는 가볍고", "verdict": "FALSE", "correct": "무겁다", "search_queries": [], "sources": [{"url": "https://x.example/a", "quote": "q", "quote_verified": False}]}
    _, failures = sq.verify_items(body(item("J5-1.a#1", "NO", "", "전동킥보드는 가볍고", fact=fact)), STATE, qs)
    assert codes(failures) == ["source_quote_not_found"]
    verified = {**fact, "sources": [{"url": "https://x.example/a", "quote": "q", "quote_verified": True}]}
    _, failures = sq.verify_items(body(item("J5-1.a#1", "NO", "", "전동킥보드는 가볍고", fact=verified)), STATE, qs)
    assert failures == []
    _, failures = sq.verify_items(body(item("J5-1.a#1", "YES", "", "전동킥보드는 가볍고", fact={**fact, "verdict": "TRUE", "sources": []})), STATE, qs)
    assert failures == []
    # 서버가 남긴 problems(fact_required·refutation_unverified·support_missing)는 그대로 재확인 실패 코드가 된다
    served = item("J5-1.a#1", "YES", "", "전동킥보드는 가볍고", fact={**fact, "verdict": "TRUE"},
                  server={"problems": [{"code": "support_missing", "message": "출처 없음"}, "fact_required"], "links": {}})
    _, failures = sq.verify_items(body(served), STATE, qs)
    assert [(f["code"], f["message"]) for f in failures] == [("support_missing", "출처 없음"), ("fact_required", sq.FAILURE_MESSAGES["fact_required"])]
    marked = body(copy.deepcopy(served))
    sq.mark_unverifiable(marked, failures, 3)
    assert marked["items"][0]["reason_code"] == "FACT_UNVERIFIABLE" and marked["items"][0]["decision"] == "NO"
    assert marked["items"][0]["model_original"]["fact"]["verdict"] == "TRUE"


def test_merge_recheck_replaces_whole_base_and_keeps_order():
    original = body(item("J1-4.a", "YES", "", "a"), item("J4-1.a#1", "YES", "전기자전거", "b"), item("J4-1.a#2", "NO", "일반 자전거", ""),
                    item("J2-3.a", "YES", "", "c"))
    merged = sq.merge_recheck(original, body(item("J4-1.a#1", "YES", "전기자전거", "x", quote_check={"forged": 1}, unverifiable=True),
                                             item("J4-1.a#2", "YES", "전동킥보드", "y"), item("J4-1.a#3", "YES", "일반 자전거", "z"),
                                             item("J2-3.a", "NO", "", "ignored")), ["J4-1.a"], round_no=2)
    assert [it["id"] for it in merged["items"]] == ["J1-4.a", "J4-1.a#1", "J4-1.a#2", "J4-1.a#3", "J2-3.a"]
    assert merged["items"][4]["answer_quote"] == "c" and "quote_check" not in merged["items"][1] and "unverifiable" not in merged["items"][1]
    assert merged["items"][1]["rechecks"] == [{"round": 2, "replaced": {"id": "J4-1.a#1", "decision": "YES", "question_quote": "전기자전거", "answer_quote": "b", "reason": "r"}}]
    assert merged["items"][3]["rechecks"] == [{"round": 2, "replaced": None}]
    assert original["items"][1]["answer_quote"] == "b"   # 원본 불변
    assert sq.merge_recheck(original, {"items": []}, ["J4-1.a"]) == original


def test_missing_elements_use_question_span_overlap_not_exact_string():
    def record(*quotes):
        return body(*[item(f"J4-1.a#{i}", "YES", quote, "x") for i, quote in enumerate(quotes, 1)])
    records = {"gpt": record("전기자전거", "전동킥보드", "일반 자전거"), "claude": record("전기자전거,", "전동킥보드", "일반 자전거 중"),
               "gemini": record("전기자전거", "전동킥보드", "**일반 자전거**"), "aiona": record("전기자전거", "전동킥보드")}
    missing = sq.missing_elements(records, "J4-1.a", QUESTION)
    assert missing == {"aiona": [{"question_quote": "일반 자전거", "seen_in": ["gpt", "claude", "gemini"]}]}
    long_quote = {**records, "aiona": record("전기자전거, 전동킥보드", "일반 자전거")}
    assert sq.missing_elements(long_quote, "J4-1.a", QUESTION) == {}
    skipped = {**records, "aiona": {"status": "SKIPPED"}, "gpt": body(item("J4-1.a#1", "YES", "질문에 없는 요소", "x"))}
    assert sq.missing_elements(skipped, "J4-1.a", QUESTION) == {}
    assert sq.missing_elements({"gpt": records["gpt"], "claude": records["claude"]}, "J4-1.a", QUESTION) == {}
    assert sq.missing_elements({"gpt": records["gpt"]}, "J4-1.a", QUESTION) == {}
    start = QUESTION.index("일반 자전거")
    assert sq.elements_of(records["gpt"], "J4-1.a", QUESTION)[2] == {"id": "J4-1.a#3", "question_quote": "일반 자전거", "span": [start, start + 6]}
    # record 모양(decisions[cid].subs[sid].items)도 읽는다
    as_record = {"decisions": {"J4": {"subs": {"J4-1": {"items": records["aiona"]["items"]}}}}}
    assert [e["question_quote"] for e in sq.elements_of(as_record, "J4-1.a", QUESTION)] == ["전기자전거", "전동킥보드"]
    union = sq.union_recheck_questions(questions("J4-1.a", "J1-4.a"), {"J4-1.a": missing["aiona"], "J9": []}, 1)
    assert list(union) == ["J4-1.a"] and union["J4-1.a"]["recheck"]["reason"] == "missing_elements"
    assert union["J4-1.a"]["recheck"]["elements"] == [{"question_quote": "일반 자전거", "seen_in": ["gpt", "claude", "gemini"]}]
    assert "빠짐없이" in union["J4-1.a"]["recheck"]["notice"] and union["J4-1.a"]["recheck"]["round"] == 1


def test_legacy_finalize_answers_returns_items_body_untouched_and_input_scope_is_not_consulted(monkeypatch):
    monkeypatch.setattr(semantic_evidence, "_input_scope", lambda *a, **k: pytest.fail("_input_scope must not run for items"))
    monkeypatch.setattr(semantic_evidence, "_context_check", lambda *a, **k: pytest.fail("_context_check must not run for items"))
    monkeypatch.setattr(semantic_evidence, "_fact_check", lambda *a, **k: pytest.fail("_fact_check must not run for items"))
    items_body = body(item("J5-1.a#1", "NO", "", "전동킥보드는 가볍고", fact={"claim": "x", "verdict": "FALSE", "correct": "y", "sources": []}),
                      item("J6-1.a#1", "YES", "존댓말로", "전동킥보드", turn=1))
    result = semantic_evidence.finalize_answers(items_body, {**STATE, "history": HISTORY}, {"J5-1.a": {}, "J6-1.a": {}})
    assert result == items_body and result is not items_body and result["items"] is not items_body["items"]
    legacy = {"model": "m", "answers": {"J1": {"choice": "PASS", "confidence": 0.9, "probabilities": {"PASS": 0.9, "FAIL": 0.1}}}, "verification": {}}
    assert semantic_evidence.finalize_answers(legacy, STATE, {"J1": {}}) == legacy


def test_not_applicable_is_rejected_on_code_applied_items_and_ends_as_unverifiable_no():
    # 적용 여부는 코드 규칙이 정한다 — model_applies 항목·주장 없는 J5-1.a·서버 자리표시만 해당 없음을 허용한다
    qs = questions("J2-2.a", "J1-4.a", "J1-3.a", "J5-1.a", "J3-1.a")
    _, failures = sq.verify_items(body(
        item("J2-2.a", "NOT_APPLICABLE", "", ""), item("J1-4.a", "NOT_APPLICABLE", "", ""), item("J1-3.a", "NOT_APPLICABLE", "", ""),
        item("J5-1.a", "NOT_APPLICABLE", "", ""), item("J3-1.a", "NOT_APPLICABLE", "", "", server_placeholder=True)), STATE, qs)
    assert [(f["id"], f["code"]) for f in failures] == [("J2-2.a", "not_applicable_disallowed"), ("J1-4.a", "not_applicable_disallowed")]
    assert "YES 또는 NO" in sq.FAILURE_MESSAGES["not_applicable_disallowed"]
    ask = Ask(*[follow(item("J1-4.a", "NOT_APPLICABLE", "", "")) for _ in range(3)])
    final = sq.finalize_items(initial(item("J1-4.a", "NOT_APPLICABLE", "", "")), STATE, questions("J1-4.a"), ask=ask)
    assert len(ask.calls) == 3 and ask.calls[0]["J1-4.a"]["recheck"]["problems"][0]["code"] == "not_applicable_disallowed"
    result = final.body["items"][0]
    assert result["decision"] == "NO" and result["unverifiable"] is True and result["model_original"]["decision"] == "NOT_APPLICABLE"
    assert final.body["verification"]["rechecks"]["unverifiable"] == ["J1-4.a"]


def test_value_items_allow_not_applicable_per_element_but_not_for_the_whole_item():
    # J5-2.a 정의: 질문이 준 값 중 답변에 쓰지 않은 값은 해당 없음 — 요소(#n) 단위로만 허용, 항목 전체 해당 없음은 여전히 재확인
    qs = questions("J5-2.a")
    assert qs["J5-2.a"]["element_na"] is True and questions("J2-2.a")["J2-2.a"]["element_na"] is False
    _, failures = sq.verify_items(body(item("J5-2.a#1", "NOT_APPLICABLE", "편도 6km", "")), STATE, qs)
    assert failures == []
    _, failures = sq.verify_items(body(item("J5-2.a", "NOT_APPLICABLE", "", "")), STATE, qs)
    assert [(f["id"], f["code"]) for f in failures] == [("J5-2.a", "not_applicable_disallowed")]
    # 요소 단위라도 그 요소를 질문 원문으로 인용해야 한다 — 인용이 없거나 질문에 없는 값이면 재확인
    _, failures = sq.verify_items(body(item("J5-2.a#1", "NOT_APPLICABLE", "", ""), item("J5-2.a#2", "NOT_APPLICABLE", "편도 9km", "")), STATE, qs)
    assert [(f["id"], f["code"]) for f in failures] == [("J5-2.a#1", "question_quote_required"), ("J5-2.a#2", "question_quote_not_found")]
    # 이전 턴의 값으로는 현재 질문의 값을 '쓰지 않음'으로 비울 수 없다
    history = dict(STATE, history=[{"turn": 1, "question": "예전 거리는?", "answer": "편도 3km"}])
    _, failures = sq.verify_items(body(dict(item("J5-2.a#1", "NOT_APPLICABLE", "편도 3km", ""), turn=1)), history, qs)
    assert [(f["id"], f["code"]) for f in failures] == [("J5-2.a#1", "question_quote_not_found")]


def test_server_owned_fields_in_the_model_response_are_discarded_before_verification():
    forged = item("J1-4.a", "YES", "", "전동킥보드는 가볍고", unverifiable=True, reason_code="FACT_FALSE_CONFIRMED",
                  rechecks=[{"round": 9}], model_original={"decision": "NO"}, quote_check={"forged": True}, origin="answer")
    final = sq.finalize_items(initial(forged), STATE, questions("J1-4.a"), ask=Ask())
    result = final.body["items"][0]
    assert result["decision"] == "YES" and not any(key in result for key in ("unverifiable", "reason_code", "rechecks", "model_original"))
    assert result["origin"] == "question" and result["quote_check"]["answer"]["ok"] is True
    assert final.body["verification"]["rechecks"]["rounds"] == 0


def test_recheck_lists_every_element_and_a_partial_recheck_answer_keeps_the_unanswered_elements():
    qs = questions("J4-1.a")
    first = initial(item("J4-1.a#1", "YES", "전기자전거", "전기자전거는 모터 보조"),
                    item("J4-1.a#2", "YES", "전동킥보드", "답변에 없는 문장"),
                    item("J4-1.a#3", "NO", "일반 자전거", "", reason="일반 자전거 비교 없음"))   # 누락형 NO 는 빈 인용 허용
    ask = Ask(follow(item("J4-1.a#2", "YES", "전동킥보드", "전동킥보드는 가볍고")))          # 실패한 요소만 다시 답함
    final = sq.finalize_items(first, STATE, qs, ask=ask)
    previous = ask.calls[0]["J4-1.a"]["recheck"]["previous"]
    assert [p["id"] for p in previous] == ["J4-1.a#1", "J4-1.a#2", "J4-1.a#3"]          # 요소 전부를 모델에 보여 준다
    assert [p["code"] for p in ask.calls[0]["J4-1.a"]["recheck"]["problems"]] == ["answer_quote_not_found"]
    items = {it["id"]: it for it in final.body["items"]}
    assert set(items) == {"J4-1.a#1", "J4-1.a#2", "J4-1.a#3"}                              # 답하지 않은 요소는 보존
    assert items["J4-1.a#3"]["decision"] == "NO" and "rechecks" not in items["J4-1.a#3"]
    assert items["J4-1.a#2"]["decision"] == "YES" and items["J4-1.a#2"]["rechecks"][0]["replaced"]["answer_quote"] == "답변에 없는 문장"
    assert final.body["verification"]["rechecks"] == {"policy": sq.RECHECK_POLICY, "rounds": 1,
        "calls": [{"round": 1, "items": ["J4-1.a"], "status": "OK", "attempts": 1, "error": None}], "unverifiable": []}
    # 요소 전부를 새로 나열하면(새 id 포함) 통째로 바뀐다 — 부분집합일 때만 보존한다
    merged = sq.merge_recheck(first.body, {"items": [item("J4-1.a#1", "YES", "a", "b"), item("J4-1.a#4", "NO", "c", "d")]}, ["J4-1.a"], round_no=1)
    assert [it["id"] for it in merged["items"]] == ["J4-1.a#1", "J4-1.a#4"]
