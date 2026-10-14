"""J5 점검 — 정상·오류·검증 불가·적용 제외 대표 사례의 일관성, 어긋난 기록 찾기, 주장 위치(코드 블록 vs 본문) 분류 (API 호출 없음)."""
from code_eval.semantic_policy import j5_outcome
from tools.j5_audit import _claim_place, audit_consistency, consistency, scope


def _record(j5):
    record = {"case_id": "q", "status": "SUCCESS", "criteria": ["J5"], "decisions": {"J5": {"criterion_id": "J5", **j5}},
              "verification": {"search_mode": "live", "search_performed": True}}
    record["decisions"]["J5"]["j5_outcome"] = j5_outcome(record)
    return record


WEB_FAIL = {"basis": "web", "claims": [{"claim": "c", "verdict": "FAIL", "scope": "external_fact",
                                        "sources": [{"origin": "web", "quote": "q", "url": "https://example.org"}]}]}
REPRESENTATIVE = {
    "정상": _record({"decision": "PASS"}),
    "실제 오류": _record({"decision": "FAIL", "fact_check": WEB_FAIL}),
    "검증 불가": _record({"decision": "FAIL", "reason_code": "SOURCE_ORIGINAL_UNAVAILABLE"}),
    "근거 부족": _record({"decision": "FAIL", "reason_code": "EVALUATION_EVIDENCE_INSUFFICIENT"}),
    "적용 제외": _record({"decision": None, "status": "NOT_APPLICABLE"}),
}


def test_representative_cases_are_consistent_and_keep_their_codes():
    codes = {name: r["decisions"]["J5"]["j5_outcome"]["code"] for name, r in REPRESENTATIVE.items()}
    assert codes == {"정상": "verified", "실제 오류": "contradicted", "검증 불가": "source_unavailable",
                     "근거 부족": "insufficient_evidence", "적용 제외": "not_applicable"}
    assert all(consistency(r) == [] for r in REPRESENTATIVE.values())
    run = {"run_id": "r", "evaluations": {"gpt": {"jev_eval": {"records": list(REPRESENTATIVE.values())}}}}
    assert audit_consistency(run)["violations"] == []


def test_mismatched_saved_outcomes_are_reported():
    broken = _record({"decision": "FAIL", "reason_code": "SOURCE_ORIGINAL_UNAVAILABLE"})
    broken["decisions"]["J5"]["j5_outcome"] = {"code": "verified", "factual_error_confirmed": True}   # 정책 뒤 갱신되지 않은 옛 값 모양
    problems = consistency(broken)
    assert any("verified 인데 decision=FAIL" in p for p in problems)
    assert any("SOURCE_ORIGINAL_UNAVAILABLE 인데" in p for p in problems)
    assert any("factual_error_confirmed" in p for p in problems) and any("다시 계산하면 source_unavailable" in p for p in problems)
    pending = _record({"decision": None, "status": "INCOMPLETE", "reason_code": "J5_SEARCH_UNAVAILABLE"})
    pending["decisions"]["J5"]["decision"] = "FAIL"          # 미완료인데 FAIL 로 바뀐 기록
    assert any("미완료" in p for p in consistency(pending))


def test_claims_in_prose_with_inline_code_are_prose_and_block_code_is_code():
    answer = "설명: `LEFT JOIN`으로 주문이 없는 고객도 포함합니다.\n\n```sql\nSELECT * FROM a LEFT JOIN b ON a.id = b.id;\n```\n"
    assert _claim_place("`LEFT JOIN`으로 주문이 없는 고객도 포함합니다", answer) == "prose"
    assert _claim_place("SELECT * FROM a LEFT JOIN b", answer) == "code"
    assert _claim_place("없는 문장", answer) == "unknown"


def test_scope_separates_answers_that_should_get_j5_from_correct_exclusions():
    code_q = "파이썬 표준 라이브러리만 사용해서 URL의 쿼리 매개변수를 사전으로 읽는 함수를 작성해줘."
    with_claim = "def f(url): ...\n\n`parse_qs`는 `+`를 공백으로 해석합니다."
    plain = "```python\ndef f(url):\n    return {}\n```"
    def rec(claims):
        return {"status": "SUCCESS", "criteria": ["J1", "J5"], "decisions": {"J1": {"decision": "PASS"}, "J5": {
            "decision": "PASS", "fact_check": {"claims": claims}}}}
    cases = [{"case_id": f"Q{n}", "case_status": "EVALUATED", "question": code_q, "answer": a, "results": []}
             for n, a in ((1, with_claim), (2, plain))]
    records = [rec([{"claim": "`parse_qs`는 `+`를 공백으로 해석합니다.", "verdict": "PASS", "scope": "external_fact"}]), rec([])]
    run = {"run_id": "t", "datasets": {"gpt": [{"case_id": c["case_id"]} for c in cases]},
           "evaluations": {"gpt": {"cases": cases, "jev_eval": {"records": records}}}}
    report = scope(run)
    kinds = {r["case_id"]: r["kind"] for r in report["rows"]}
    assert kinds == {"Q1": "should_apply_prose_claims", "Q2": "no_claims"}
    assert report["rows"][0]["prose_kinds"] == {"기술 사실": 1} and report["rows"][0]["j5_1_reason"] == "요청 문장에 사실 질의 표현 없음"
