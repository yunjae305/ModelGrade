"""items 파이프라인의 J5 서버 검증(semantic_facts) — 주입 fetcher 만 쓰고 모델·네트워크 호출 없음.

test_semantic_evidence_content.py 의 SourceStub 패턴을 재사용한다. 반박 출처 대조는 legacy verify_web_sources 와 같은
_verify_sources 를 타므로 관측되지 않은 URL 은 열지 않고, 인용은 semantic_sources._normalize 로 대조한다.
"""
import copy

import pytest

from code_eval import semantic_facts as sf, semantic_quotes as sq, semantic_sources
from code_eval.jev_client import JevCall
from code_eval.semantic_items import SPECS_BY_ID
from code_eval.semantic_policy import apply_final_decision_policy

URL = "https://official.example/caffeine/half-life"
OTHER = "https://unrelated.example/page"
STAMP = "2026-10-09T03:00:00+00:00"
QUESTION = "저녁에 커피 마시면 잠을 잘 못 자는 이유가 뭐야? 카페인이 몸에 얼마나 오래 남아? 참고로 나는 하루 3잔 마셔."
ANSWER = ("카페인은 뇌에서 졸음을 일으키는 아데노신이 수용체에 붙는 것을 막아 각성 상태를 유지시킵니다. "
          "성인 기준 카페인의 반감기는 약 12시간이라, 오후 3시에 마신 커피의 절반이 새벽 3시까지 남아 있습니다. "
          "하루 3잔이면 더 오래 남습니다. 자세한 내용은 https://official.example/caffeine/half-life 와 https://gone.example/x 를 보세요.\n"
          "```\nhttps://code.example/ignored\n```")
REBUTTAL = "The average half-life of caffeine in healthy adults is about 5 hours."
STATE = {"question": QUESTION, "answer": ANSWER,
         "history": [{"turn": 1, "question": "커피는 하루에 몇 잔까지 괜찮아?", "answer": "성인은 하루 400mg, 커피 약 4잔까지가 권장 상한입니다."}]}
VERIFICATION = {"search_performed": True, "source_urls": [URL], "checked_at": STAMP}


class SourceStub:
    def __init__(self, pages=None, *, status="ok"):
        self.pages = pages if pages is not None else {URL: REBUTTAL}
        self.status = status
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.pages:
            return {"status": "http_error", "text": "", "final_url": None, "checked_at": STAMP, "content_type": None}
        return {"status": self.status, "text": self.pages[url], "final_url": url, "checked_at": STAMP, "content_type": "text/plain"}


@pytest.fixture(autouse=True)
def forbid_real_fetch(monkeypatch):
    monkeypatch.setattr(semantic_sources, "fetch_public_source", lambda *a, **k: pytest.fail("real fetch forbidden"))


def questions(*ids):
    return {item_id: SPECS_BY_ID[item_id].to_prompt_dict({"needs_search": True}) for item_id in ids}


def fact(claim="성인 기준 카페인의 반감기는 약 12시간", verdict="FALSE", correct="약 5시간", sources=None, quote=REBUTTAL):
    return {"claim": claim, "verdict": verdict, "correct": correct, "search_queries": ["caffeine half-life"],
            "sources": [{"url": URL, "quote": quote}] if sources is None else sources}


def item(item_id, decision, answer_quote="", question_quote="", reason="r", **extra):
    return {"id": item_id, "decision": decision, "question_quote": question_quote, "answer_quote": answer_quote, "reason": reason,
            "turn": None, "fact": None, **extra}


def claim_item(n, verdict="FALSE", decision=None, **fact_overrides):
    f = fact(verdict=verdict, **fact_overrides)
    return item(f"J5-1.a#{n}", decision or ("YES" if verdict == "TRUE" else "NO"), answer_quote=f["claim"], fact=f)


def body(*items, verification=None):
    return {"model": "fixture-judge", "items": list(items), "verification": copy.deepcopy(VERIFICATION if verification is None else verification)}


def check(items, *, fetcher=None, state=None, qs=None, verification=None):
    fetcher = SourceStub() if fetcher is None else fetcher
    original = body(*items, verification=verification)
    snapshot = copy.deepcopy(original)
    result = sf.server_check(original, state or STATE, qs or questions("J5-1.a", "J5-2.a", "J5-3.a"),
                             copy.deepcopy(original["verification"]), fetcher=fetcher)
    assert original == snapshot
    return result, fetcher


def problems(result, item_id):
    entry = next(it for it in result["items"] if it["id"] == item_id)
    return [p["code"] for p in entry["server"]["problems"]]


def test_claim_must_be_verbatim_in_answer_normalized():
    ok = claim_item(1, claim="성인 기준 **카페인의**  반감기는 약 12시간")   # Markdown 강조·공백 차이는 통과
    other = claim_item(2, claim="카페인의 반감기는 약 7시간")
    result, _ = check([ok, other])
    assert problems(result, "J5-1.a#1") == [] and problems(result, "J5-1.a#2") == ["claim_not_in_answer"]
    assert result["verification"]["fact_policy"] == "j5-items-facts-v1" == sf.FACT_POLICY_VERSION
    missing_fact = item("J5-1.a#3", "NO", answer_quote="x")
    result, _ = check([missing_fact])
    assert problems(result, "J5-1.a#3") == ["fact_required"]
    none = item("J5-1.a", "NOT_APPLICABLE")
    result, _ = check([none])
    assert problems(result, "J5-1.a") == []


def test_excluded_claims_from_j3_pairs_history_ai_and_question_values_are_dropped_not_rechecked():
    contradiction = item("J3-1.a#1", "NO", answer_quote="성인 기준 카페인의 반감기는 약 12시간")
    causal = item("J3-3.a", "YES", answer_quote="아데노신이 수용체에 붙는 것을 막아 각성 상태를 유지시킵니다")
    items = [contradiction, causal,
             claim_item(1, claim="카페인의 반감기는 약 12시간"),                       # J3-1.a 모순 쌍 안의 문장
             claim_item(2, claim="아데노신이 수용체에 붙는 것을 막아", verdict="TRUE"),   # J3-3.a 인과 문장
             claim_item(3, claim="하루 400mg, 커피 약 4잔", verdict="TRUE"),            # 이전 턴 AI 답변의 값
             claim_item(4, claim="하루 3잔", verdict="TRUE"),                            # 질문 원문의 값
             claim_item(5, claim="오후 3시에 마신 커피의 절반이 새벽 3시까지 남아 있습니다", verdict="FALSE")]
    result, _ = check(items)
    kept = [it["id"] for it in result["items"]]
    assert kept == ["J3-1.a#1", "J3-3.a", "J5-1.a#5"]
    dropped = result["verification"]["j5_dropped"]
    assert [d["id"] for d in dropped] == ["J5-1.a#1", "J5-1.a#2", "J5-1.a#3", "J5-1.a#4"]
    assert dropped[0]["reason"].startswith("J3-1.a") and dropped[1]["reason"].startswith("J3-3.a")
    assert "이전 턴" in dropped[2]["reason"] and "질문" in dropped[3]["reason"]
    assert problems(result, "J5-1.a#5") == [] and "server" not in result["items"][0]


def test_ninth_claim_is_dropped_with_limit_note():
    items = [claim_item(n, claim="성인 기준 카페인의 반감기는 약 12시간", verdict="TRUE") for n in range(1, 11)]
    result, _ = check(items)
    assert [it["id"] for it in result["items"]] == [f"J5-1.a#{n}" for n in range(1, 9)]
    assert [d["id"] for d in result["verification"]["j5_dropped"]] == ["J5-1.a#9", "J5-1.a#10"]
    assert "최대 8개" in result["verification"]["j5_dropped"][0]["reason"] and sf.MAX_CLAIMS == 8


def test_false_claim_needs_correct_and_observed_refutation_url():
    no_correct = claim_item(1, correct="")
    unobserved = claim_item(2, sources=[{"url": OTHER, "quote": REBUTTAL}])
    no_sources = claim_item(3, sources=[])
    result, fetcher = check([no_correct, unobserved, no_sources])
    assert problems(result, "J5-1.a#1") == ["refutation_missing"] and problems(result, "J5-1.a#2") == ["refutation_missing"]
    assert problems(result, "J5-1.a#3") == ["refutation_missing"]
    assert fetcher.calls == [URL]   # 관측되지 않은 URL 은 열지 않는다
    assert next(it for it in result["items"] if it["id"] == "J5-1.a#2")["fact"]["sources"][0]["verification_status"] == "not_observed"


def test_false_refutation_quote_verified_through_verify_web_sources_adapter():
    a, b = claim_item(1), claim_item(2, claim="오후 3시에 마신 커피의 절반이 새벽 3시까지 남아 있습니다")
    result, fetcher = check([a, b])
    assert fetcher.calls == [URL]   # 같은 URL 은 한 번만 (캐시)
    for item_id in ("J5-1.a#1", "J5-1.a#2"):
        entry = next(it for it in result["items"] if it["id"] == item_id)
        assert problems(result, item_id) == []
        assert entry["fact"]["sources"][0]["quote_verified"] is True and entry["fact"]["sources"][0]["verification_status"] == "verified"
        assert entry["fact"]["sources"][0]["verified_at"] == STAMP and entry["decision"] == "NO"
    assert result["verification"]["source_validation"] == "provider_and_content"
    assert result["verification"]["source_checks"][0]["verified_quotes"] == 2
    assert "FULL" not in str(result)
    sf.finalize(result["items"], 0)
    assert all(it["reason_code"] == "FACT_FALSE_CONFIRMED" for it in result["items"])
    projected = sf.legacy_projection(result["items"])
    assert projected["decision"] == "FAIL" and projected["reason_code"] == "FACT_FALSE_CONFIRMED"


def test_false_refutation_quote_not_found_is_recheck_then_unverifiable_after_three():
    result, _ = check([claim_item(1, quote="A quotation the page does not contain.")])
    entry = next(it for it in result["items"] if it["id"] == "J5-1.a#1")
    assert problems(result, "J5-1.a#1") == ["refutation_unverified"]
    assert entry["server"]["problems"][0]["statuses"] == ["quote_not_found"] and "원문" in entry["server"]["problems"][0]["message"]
    # verify_items 가 서버 문제를 재확인 실패로 읽는다
    _, failures = sq.verify_items(result, STATE, questions("J5-1.a"))
    assert sorted(f["code"] for f in failures) == ["refutation_unverified", "source_quote_not_found"]
    for attempts in (0, 1, 2):
        items = copy.deepcopy(result["items"])
        sf.finalize(items, attempts)
        assert items[0]["decision"] == "NO" and "unverifiable" not in items[0] and "reason_code" not in items[0]
    items = copy.deepcopy(result["items"])
    sf.finalize(items, 3)
    assert items[0]["decision"] == "NO" and items[0]["unverifiable"] is True and items[0]["reason_code"] == "FACT_UNVERIFIABLE"
    assert items[0]["reason"].startswith("확인 불가") and items[0]["fact"]["verdict"] == "FALSE" and items[0]["model_original"]["decision"] == "NO"
    # 재확인 루프(finalize_items)가 3회 뒤 같은 결과를 낸다 — ask 는 매번 같은 틀린 인용을 돌려주는 대역
    ask_calls = []

    def ask(subset):
        ask_calls.append(sorted(subset))
        again = body(claim_item(1, quote="A quotation the page does not contain."))
        again = sf.server_check(again, STATE, questions("J5-1.a"), copy.deepcopy(VERIFICATION), fetcher=SourceStub())
        return JevCall("OK", body=again, attempts=1)
    done = sq.finalize_items(JevCall("OK", body=result, attempts=1), STATE, questions("J5-1.a"), ask=ask)
    assert ask_calls == [["J5-1.a"]] * 3 and done.body["items"][0]["reason_code"] == "FACT_UNVERIFIABLE"
    assert done.body["verification"]["rechecks"]["rounds"] == 3 and done.body["verification"]["rechecks"]["unverifiable"] == ["J5-1.a#1"]
    assert sf.legacy_projection(done.body["items"])["reason_code"] == "FACT_UNVERIFIABLE"


def test_true_claim_without_observed_source_is_recheck_not_pass():
    result, fetcher = check([claim_item(1, verdict="TRUE", sources=[]), claim_item(2, verdict="TRUE", sources=[{"url": OTHER, "quote": "x"}]),
                             claim_item(3, verdict="TRUE")])
    assert problems(result, "J5-1.a#1") == ["support_missing"] and problems(result, "J5-1.a#2") == ["support_missing"]
    assert problems(result, "J5-1.a#3") == [] and fetcher.calls == []   # 참 판정은 관측 URL 만 요구하고 원문을 다시 받지 않는다
    items = copy.deepcopy(result["items"])
    sf.finalize(items, 3)
    assert items[0]["decision"] == "NO" and items[0]["reason_code"] == "FACT_UNVERIFIABLE" and items[2]["decision"] == "YES"
    subs = sf.aggregate_j5(items)
    assert subs["J5-1"]["decision"] == "FAIL"
    projected = sf.legacy_projection(items, subs)
    assert projected["decision"] == "FAIL" and projected["reason_code"] == "FACT_UNVERIFIABLE"
    assert [c["verdict"] for c in projected["fact_check"]["claims"]] == ["FAIL", "FAIL", "PASS"]
    # decision 과 verdict 가 어긋나면 verdict 로 맞춘다
    result, _ = check([claim_item(1, verdict="TRUE", decision="NO"), claim_item(2, verdict="FALSE", decision="YES")])
    first, second = result["items"]
    assert first["decision"] == "YES" and first["decision_adjusted"] == {"from": "NO", "to": "YES", "by": "fact.verdict"}
    assert second["decision"] == "NO" and second["decision_adjusted"]["from"] == "YES"
    result, _ = check([claim_item(1, verdict="UNVERIFIABLE")])
    assert problems(result, "J5-1.a#1") == ["verdict_invalid"]


def test_unobserved_url_is_never_fetched():
    result, fetcher = check([claim_item(1, sources=[{"url": OTHER, "quote": REBUTTAL}])],
                            fetcher=SourceStub({OTHER: REBUTTAL}), qs=questions("J5-1.a"))
    assert fetcher.calls == [] and problems(result, "J5-1.a#1") == ["refutation_missing"]
    assert sf.verify_refutations(copy.deepcopy(result["items"]), {"search_performed": False, "source_urls": [URL]}, fetcher=fetcher) == {
        "J5-1.a#1": [{"code": "refutation_unverified", "message": sf.PROBLEM_MESSAGES["refutation_unverified"], "statuses": ["not_observed"]}]}
    assert fetcher.calls == []


def test_answer_links_that_do_not_open_are_inaccessible_and_links_beyond_the_limit_are_unchecked():
    pages = {URL: REBUTTAL, "https://gone.example/x": ""}
    fetcher = SourceStub(pages)
    links = sf.check_answer_links(ANSWER, fetcher=fetcher)
    assert links == {URL: "exists", "https://gone.example/x": "exists"} and "https://code.example/ignored" not in fetcher.calls
    fetcher = SourceStub({URL: REBUTTAL})   # gone.example → http_error
    links = sf.check_answer_links(ANSWER, fetcher=fetcher)
    assert links == {URL: "exists", "https://gone.example/x": "not_found"}
    timeout = SourceStub({URL: REBUTTAL, "https://gone.example/x": ""}, status="timeout")
    # evidence-v3.14 (사용자 결정): 열리지 않으면(시간 초과 포함) 접근 불가 — 판정에서 NO
    assert sf.check_answer_links(ANSWER, fetcher=timeout) == {URL: "inaccessible", "https://gone.example/x": "inaccessible"}
    assert sf.check_answer_links("출처: ftp://x 와 http://user:pw@host/ 를 보세요.", fetcher=SourceStub({})) == {"http://user:pw@host/": "not_found"}
    assert sf.check_answer_links("링크 없음", fetcher=SourceStub({})) == {}
    many = sf.check_answer_links(" ".join(f"https://h{i}.example/" for i in range(12)), fetcher=SourceStub({}))
    assert sf.MAX_LINKS == 8 and len(many) == 12 and list(many.values()).count("unchecked") == 4   # 한도 밖은 열지 않고 확인 안 함으로
    # server_check: J5-3.a 에 links 가 붙고 finalize 가 모델 YES 를 NO + SOURCE_NOT_FOUND 로 바꾼다
    yes = item("J5-3.a", "YES", answer_quote="자세한 내용은")
    result, _ = check([yes], fetcher=SourceStub({URL: REBUTTAL}), qs=questions("J5-3.a"))
    entry = result["items"][0]
    assert entry["server"] == {"problems": [], "links": {URL: "exists", "https://gone.example/x": "not_found"}}
    sf.finalize(result["items"], 0)
    assert entry["decision"] == "NO" and entry["reason_code"] == "SOURCE_NOT_FOUND" and "gone.example" in entry["reason"]
    assert entry["model_original"]["decision"] == "YES"
    sf.finalize(result["items"], 3)   # 반복 호출 안전
    assert entry["reason_code"] == "SOURCE_NOT_FOUND" and entry["model_original"]["decision"] == "YES"
    # 열리지 않는 URL(시간 초과)도 접근 불가 → 모델 YES 를 NO + SOURCE_NOT_FOUND 로 (evidence-v3.14)
    result, _ = check([item("J5-3.a", "YES", answer_quote="자세한 내용은")], fetcher=timeout, qs=questions("J5-3.a"))
    sf.finalize(result["items"], 0)
    assert result["items"][0]["decision"] == "NO" and result["items"][0]["reason_code"] == "SOURCE_NOT_FOUND"
    # 모든 URL 이 열리면 모델 YES 유지
    result, _ = check([item("J5-3.a", "YES", answer_quote="자세한 내용은")],
                      fetcher=SourceStub({URL: REBUTTAL, "https://gone.example/x": "ok"}), qs=questions("J5-3.a"))
    sf.finalize(result["items"], 0)
    assert result["items"][0]["decision"] == "YES" and "reason_code" not in result["items"][0]
    assert sf.legacy_projection(result["items"]) == {"decision": "PASS", "reason": "적용 항목 모두 예", "fact_check": {"basis": "none", "claims": []}}


def test_j5_3_answer_tools_unknown_skips_process_claims_in_item_text():
    from code_eval.jev_applicability import j5_items_applicability
    claims = "방금 카드사 시스템에서 결제 내역을 조회해 보니 두 건이 확인됩니다."
    unknown = j5_items_applicability("카드값이 두 번 빠져나갔어. 어떻게 해야 해?", claims, answer_tools="unknown")["subs"]["J5-3"]
    none = j5_items_applicability("카드값이 두 번 빠져나갔어. 어떻게 해야 해?", claims, answer_tools="none")["subs"]["J5-3"]
    assert unknown["items"] == [] and none["items"] == ["J5-3.a"]
    prompt = SPECS_BY_ID["J5-3.a"].to_prompt_dict(none)
    assert prompt["answer_tools"] == "none" and prompt["matches"] == ["조회해 보니"] and prompt["source"] == "answer"


def test_input_value_question_quote_normalized_against_question_and_reference():
    ok = item("J5-2.a#1", "YES", question_quote="하루 **3잔**", answer_quote="하루 3잔이면")
    missing_q = item("J5-2.a#2", "NO", question_quote="하루 5잔", answer_quote="하루 3잔이면")
    missing_a = item("J5-2.a#3", "NO", question_quote="하루 3잔", answer_quote="하루 9잔")
    empty = item("J5-2.a#4", "YES")
    skipped = item("J5-2.a#5", "NOT_APPLICABLE", question_quote="없는 값")
    result, fetcher = check([ok, missing_q, missing_a, empty, skipped], qs=questions("J5-2.a"))
    assert fetcher.calls == []
    assert problems(result, "J5-2.a#1") == [] and problems(result, "J5-2.a#2") == ["question_quote_not_found"]
    assert problems(result, "J5-2.a#3") == ["answer_quote_not_found"]
    assert problems(result, "J5-2.a#4") == ["question_quote_required", "answer_quote_required"] and problems(result, "J5-2.a#5") == []
    with_reference = {**STATE, "reference": {"population": 51550000}}
    found = sf.check_input_values([item("J5-2.a#1", "YES", question_quote="51,550,000", answer_quote="하루 3잔")], with_reference)
    assert found == {"J5-2.a#1": [{"code": "question_quote_not_found", "message": sf.PROBLEM_MESSAGES["question_quote_not_found"]}]}
    assert sf.check_input_values([item("J5-2.a#1", "YES", question_quote="51550000", answer_quote="하루 3잔")], with_reference) == {}
    assert "server" not in sf.server_check(body(ok), STATE, {"J1-1.a": {}}, {})["items"][0]   # J5 항목을 보내지 않았으면 그대로


def test_aggregate_j5_one_false_or_unverifiable_claim_fails_j5_1_and_criterion():
    items = [claim_item(n, verdict="TRUE") for n in range(1, 8)] + [claim_item(8)]
    result, _ = check(items, qs=questions("J5-1.a"))
    sf.finalize(result["items"], 0)
    subs = sf.aggregate_j5(result["items"])
    assert subs["J5-1"]["decision"] == "FAIL" and subs["J5-2"]["decision"] == "NOT_APPLICABLE" and subs["J5-3"]["decision"] == "NOT_APPLICABLE"
    projected = sf.legacy_projection(result["items"], subs)
    assert projected["decision"] == "FAIL" and projected["reason_code"] == "FACT_FALSE_CONFIRMED" and "J5-1.a#8" in projected["reason"]
    claims = projected["fact_check"]["claims"]
    assert projected["fact_check"]["basis"] == "web" and [c["verdict"] for c in claims].count("PASS") == 7 and claims[-1]["verdict"] == "FAIL"
    assert claims[-1]["reason_code"] == "FACT_FALSE_CONFIRMED"
    all_true = [claim_item(n, verdict="TRUE") for n in range(1, 4)]
    result, _ = check(all_true, qs=questions("J5-1.a"))
    assert sf.aggregate_j5(result["items"])["J5-1"]["decision"] == "PASS" and sf.legacy_projection(result["items"])["decision"] == "PASS"
    mixed = [claim_item(1, verdict="TRUE"), item("J5-3.a", "NO", answer_quote="자세한 내용은")]
    assert sf.legacy_projection(mixed, {"J5-1": "PASS", "J5-2": "NOT_APPLICABLE", "J5-3": "FAIL"})["decision"] == "FAIL"


def test_aggregate_all_subs_not_applicable_gives_criterion_none():
    items = [item("J5-1.a", "NOT_APPLICABLE"), item("J5-2.a", "NOT_APPLICABLE"), item("J5-3.a", "NOT_APPLICABLE")]
    subs = sf.aggregate_j5(items)
    assert all(sub["decision"] == "NOT_APPLICABLE" for sub in subs.values())
    assert sf.legacy_projection(items, subs) == {"decision": None, "status": "NOT_APPLICABLE", "reason": "적용 항목 없음",
                                                 "fact_check": {"basis": "none", "claims": []}}
    assert sf.legacy_projection([])["status"] == "NOT_APPLICABLE"


def test_legacy_projection_keeps_top_level_keys_and_fact_check_shape():
    result, _ = check([claim_item(1), claim_item(2, verdict="TRUE")], qs=questions("J5-1.a"))
    sf.finalize(result["items"], 0)
    projected = sf.legacy_projection(result["items"])
    assert set(projected) == {"decision", "reason", "reason_code", "fact_check"} and projected["fact_check"]["basis"] == "web"
    claim = projected["fact_check"]["claims"][0]
    assert set(claim) >= {"claim", "verdict", "reason", "scope", "scope_quote", "sources", "reason_code"}
    assert claim["scope"] == "external_fact" and claim["scope_quote"] == "" and claim["verdict"] == "FAIL"
    assert claim["sources"] == [{"url": URL, "title": "", "quote": REBUTTAL, "origin": "web", "quote_verified": True, "verification_status": "verified"}]
    assert projected["fact_check"]["claims"][1]["verdict"] == "PASS" and "reason_code" not in projected["fact_check"]["claims"][1]


def test_apply_final_decision_policy_is_noop_on_items_record():
    result, _ = check([claim_item(1)], qs=questions("J5-1.a"))
    sf.finalize(result["items"], 0)
    projected = sf.legacy_projection(result["items"])
    record = {"case_id": "c", "status": "SUCCESS", "criteria": ["J5"], "pipeline": "items",
              "decisions": {"J5": {"criterion_id": "J5", "name": "DATA 신뢰도", "confidence": None, "probabilities": {}, **projected,
                                   "subs": {"J5-1": sf.aggregate_j5(result["items"])["J5-1"]}}},
              "verification": result["verification"]}
    snapshot = copy.deepcopy(record)
    outcome = apply_final_decision_policy(record, state=STATE)
    assert outcome["changed"] is False and record == snapshot


def test_all_claims_excluded_leaves_a_server_not_applicable_placeholder_instead_of_item_missing():
    # 이전 턴 AI 답변에서 비롯한 값만 주장으로 나열하면 모두 제외되어 '응답에 J5-1.a 없음' → 재확인 3회 → 확인 불가 FAIL 이 되던 경로
    items = [claim_item(1, verdict="TRUE", claim="커피 약 4잔까지가 권장 상한")]
    kept, problems, dropped = sf.validate_fact_items(items, STATE, VERIFICATION)
    assert [d["id"] for d in dropped] == ["J5-1.a#1"] and dropped[0]["reason"].startswith("이전 턴(1) AI 답변")
    assert problems == {} and len(kept) == 1
    placeholder = kept[0]
    assert placeholder["id"] == "J5-1.a" and placeholder["decision"] == "NOT_APPLICABLE" and placeholder["server_placeholder"] is True
    checked, failures = sq.verify_items({"items": kept}, STATE, questions("J5-1.a"))
    assert failures == [] and checked["items"][0]["decision"] == "NOT_APPLICABLE"
    # 주장이 하나라도 남으면 자리표시를 만들지 않는다
    kept, _, dropped = sf.validate_fact_items([claim_item(1, verdict="TRUE", claim="커피 약 4잔까지가 권장 상한"), claim_item(2, verdict="TRUE")],
                                              STATE, VERIFICATION)
    assert [it["id"] for it in kept] == ["J5-1.a#2"] and len(dropped) == 1
