"""의미 평가 방식 비교 도구 — 대표 답변 선택·적용 기준 비교·모의 실행 후 판정 차이 표 (모델·네트워크 호출 없음)."""
import json

import pytest

from tools.pipeline_compare import diff, main, outcome_class, routing, run_items, select

Q1 = "무지개는 왜 여러 색으로 보여? 초등학생이 이해할 수 있게 설명해줘."
A1 = "햇빛에는 여러 색이 섞여 있고, 물방울이 색마다 다르게 빛을 꺾어서 색이 나뉘어 보여요."
C1 = "가상의 모임 계획을 세울게. 정원은 12명이야. 계획을 세 줄로 정리해줘."
C2 = "정원을 15명으로 바꿔서 다시 정리해줘."


def _run():
    def batch(system, j5_fail):
        rows = [(Q1, A1 + system, None, None), (C1, "1. 장소 2. 시간 3. 정원 12명 " + system, "C", 1),
                (C2, "1. 장소 2. 시간 3. 정원 15명 " + system, "C", 2)]
        cases, records, data = [], [], []
        for n, (q, a, cid, turn) in enumerate(rows):
            case_id = f"Q{n + 1}"
            cases.append({"case_id": case_id, "case_status": "EVALUATED", "question": q, "answer": a, "results": []})
            data.append({"case_id": case_id, "question": q, "answer": a, **({"conversation_id": cid, "turn": turn} if cid else {})})
            decisions = {"J1": {"decision": "PASS"}}
            if n == 0:
                decisions["J5"] = ({"decision": "FAIL", "reason_code": "SOURCE_ORIGINAL_UNAVAILABLE",
                                    "j5_outcome": {"code": "source_unavailable", "factual_error_confirmed": False,
                                                   "source_failure": {"kind": "access_failed"}}}
                                   if j5_fail else {"decision": "PASS"})
            if n == 2:
                decisions["J6"] = {"decision": "FAIL", "fail_evidence": {"valid": True}} if j5_fail else {"decision": "PASS"}
            records.append({"case_id": case_id, "status": "SUCCESS", "criteria": list(decisions), "decisions": decisions})
        return data, {"cases": cases, "jev_eval": {"pipeline": "legacy", "records": records}}
    a_rows, a = batch("aiona", True)
    g_rows, g = batch("gpt", False)
    return {"run_id": "t", "question_count": 3, "collection_completed_at": "2026-10-13T00:00:00+00:00",
            "datasets": {"aiona": a_rows, "gpt": g_rows}, "evaluations": {"aiona": a, "gpt": g}}


def test_outcome_classes_keep_policy_fail_apart_from_confirmed_errors():
    assert outcome_class({"decision": "PASS"}) == "pass"
    assert outcome_class({"decision": "FAIL", "j5_outcome": {"code": "source_unavailable", "factual_error_confirmed": False,
                                                             "source_failure": {"kind": "quote_not_found"}}}) == "fail_policy_source_unavailable:quote_not_found"
    assert outcome_class({"decision": "FAIL", "j5_outcome": {"code": "contradicted", "factual_error_confirmed": True}}) == "fail_fact_confirmed"
    assert outcome_class({"decision": "FAIL", "reason_code": "FACT_UNVERIFIABLE"}) == "fail_policy_unverifiable"
    assert outcome_class({"decision": None, "status": "UNVERIFIED"}) == "unverified" and outcome_class(None) == "missing"


def test_select_routing_mock_run_and_diff_round_trip(tmp_path):
    run = _run()
    picked = select(run, count=5)
    assert [p["why"] for p in picked[:2]] == ["legacy J5 fail_policy_source_unavailable:access_failed", "legacy J6 fail_with_evidence"]
    assert any(p["why"].endswith("(대화 턴)") and p["turn"] == 2 for p in picked)   # 모두 PASS 대화 턴은 J6 가 적용되는 2턴부터
    report = routing(run)
    assert report["answers"] == 6 and report["criteria"]["J1"] == {"both": 6}
    assert report["criteria"]["J6"].get("both") == 2 and report["legacy_outcome_classes"]["J6"] == {"fail_with_evidence": 1, "pass": 1}
    result = run_items(run, picked, tmp_path / "mock", mode="mock")
    assert result["meta"]["mode"] == "mock" and result["meta"]["calls"]["new"] == len(picked)   # 고른 답변만 호출, 앞 턴은 이력으로만
    assert all(b["jev_eval"]["pipeline"] == "items" for b in result["batches"].values())
    table = diff(run, tmp_path / "mock")
    assert table["mode"] == "mock" and "판정 품질 비교 아님" in table["note"]
    j6 = next(r for r in table["rows"] if r["system"] == "aiona" and r["criterion"] == "J6")
    assert (j6["legacy"], j6["items"], j6["kind"]) == ("fail_with_evidence", "pass", "items_cleared_fail")


def test_live_run_requires_both_caps(tmp_path):
    run = tmp_path / "run.json"
    run.write_text(json.dumps(_run(), ensure_ascii=False), encoding="utf-8")
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps([{"system": "gpt", "index": 0}]), encoding="utf-8")
    with pytest.raises(SystemExit) as stopped:
        main(["run", str(run), "--selection", str(sel), "--out", str(tmp_path / "o"), "--live", "--max-usd", "1"])
    assert stopped.value.code == 2 and not (tmp_path / "o").exists()   # 호출 수 상한이 없으면 아무것도 시작하지 않음


def test_j5_routing_separates_scope_gaps_from_questions_items_still_judges():
    """지시서 10-14 P0-1 — legacy J5 FAIL 이 items 에서 사라지는 것이 판정 개선인지 평가 대상 차이인지 나눈다."""
    from tools.pipeline_compare import j5_routing
    code_q = "파이썬 표준 라이브러리만 사용해서 URL의 쿼리 매개변수를 사전으로 읽는 함수를 작성해줘."
    fact_q = "무지개는 왜 여러 색으로 보여? 초등학생이 이해할 수 있게 설명해줘."
    fail = {"decision": "FAIL", "reason_code": "SOURCE_ORIGINAL_UNAVAILABLE",
            "j5_outcome": {"code": "source_unavailable", "factual_error_confirmed": False, "source_failure": {"kind": "quote_not_found"}},
            "fact_check": {"claims": [{"claim": "`+`는 공백으로 해석됩니다.", "verdict": "UNVERIFIED", "scope": "external_fact"}]}}
    cases = [{"case_id": f"Q{n}", "case_status": "EVALUATED", "question": q, "answer": "def f(url): ...  `+`는 공백으로 해석됩니다.", "results": []}
             for n, q in ((1, code_q), (2, fact_q))]
    records = [{"case_id": c["case_id"], "status": "SUCCESS", "criteria": ["J1", "J5"], "decisions": {"J1": {"decision": "PASS"}, "J5": fail}}
               for c in cases]
    run = {"run_id": "t", "datasets": {"gpt": [{"case_id": c["case_id"]} for c in cases]},
           "evaluations": {"gpt": {"cases": cases, "jev_eval": {"records": records}}}}
    report = j5_routing(run)
    kinds = {r["case_id"]: r["kind"] for r in report["rows"]}
    assert kinds == {"Q1": "scope_gap_no_fact_query", "Q2": "items_routed"}
    gap = next(r for r in report["rows"] if r["case_id"] == "Q1")
    assert gap["items_j5"]["J5-1"]["reason"] == "요청 문장에 사실 질의 표현 없음" and not gap["items_j5"]["J5-2"]["items"]
    assert gap["claims"] == 1 and gap["claim_scopes"] == {"external_fact": 1}   # 기준 정의(답변 데이터)상으로는 확인할 주장이 있었다
