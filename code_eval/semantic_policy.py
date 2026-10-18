"""Apply explicit evaluation policy after evidence recovery has finished.

A source-verification policy failure does not assert that the underlying factual
claim is false. The original unresolved assessment and evidence remain available
for audit; only the final J5 evaluation outcome changes.
"""
from __future__ import annotations

import copy

from .semantic_evidence import finalize_answers
from .semantic_reasons import REASON_EVIDENCE_POLICY_VERSION
from .semantic_sources import _REASONS


#: v2 — 서버가 받지 못한 출처는 같은 요청의 공급자 web_fetch 원문으로 대조한다 (F5b). 둘 다 없을 때의 FAIL 규칙은 v1 과 같다.
SOURCE_ORIGINAL_FAILURE_POLICY_VERSION = "j5-source-original-failure-v3"   # v3: 확인 실패는 FAIL 대신 보류 (semantic-hold-unverified-v3 와 함께)
#: v3(2026-10-18, 사용자 결정) — 근거가 부족해 확인하지 못한 기준은 FAIL 이 아니라 **보류**(UNVERIFIED 유지, review_required)로 두고
#:      사람이 PASS/FAIL 을 정한다. 확인된 사실 오류·출처 불일치·답변이 든 URL 접근 불가는 그대로 FAIL. v2 까지는 근거 부족을 FAIL 로 확정했다.
FINAL_DECISION_POLICY_VERSION = "semantic-hold-unverified-v3"
#: v2 — 원문 열기(web_fetch) 도구 오류를 검색 오류와 따로 기록한다 (판정 값은 v1 과 같음).
#: v3 — 출처 원문 확인 실패(source_unavailable)의 세부 사유를 접근 실패와 인용문 미확인으로 나눠 기록한다
#:      (code·label·판정 값은 v2 와 같음 — source_failure 상세 항목만 추가).
J5_OUTCOME_POLICY_VERSION = "j5-outcome-detail-v6"   # v4: 답변이 든 URL 접근 불가(citation_inaccessible) 구분 · v5: 충돌 근거·URL 접근 실패 주체 기록 · v6: 보류(held) 사유 추가
SOURCE_DECISION_POLICY = {"j5_source_original_failure": SOURCE_ORIGINAL_FAILURE_POLICY_VERSION}
#: items 파이프라인의 재확인 정책 — 3회 뒤 남은 문제는 '확인 불가로 FAIL' (semantic_quotes.RECHECK_POLICY 와 같은 값)
ITEMS_RECHECK_POLICY_VERSION = "items-recheck-3-then-fail-v1"
DECISION_POLICY = {**SOURCE_DECISION_POLICY, "semantic_final_outcome": FINAL_DECISION_POLICY_VERSION,
                   "judge_fail_evidence": REASON_EVIDENCE_POLICY_VERSION, "j5_outcome": J5_OUTCOME_POLICY_VERSION,
                   "items_recheck": ITEMS_RECHECK_POLICY_VERSION}
SOURCE_ORIGINAL_FAILURE_REASON = "보류 — 출처 원문을 확인하지 못함 (사람이 PASS/FAIL 을 정함)"
SOURCE_ORIGINAL_FAILURE_CODE = "SOURCE_ORIGINAL_UNAVAILABLE"
FINAL_EVIDENCE_FAILURE_CODE = "EVALUATION_EVIDENCE_INSUFFICIENT"
FINAL_EVIDENCE_FAILURE_REASON = "보류 — 평가 근거가 부족해 확인하지 못함 (사람이 PASS/FAIL 을 정함)"
INPUT_SCOPE_REVALIDATED_CODE = "INPUT_SCOPE_REVALIDATED"
INPUT_SCOPE_REVALIDATED_REASON = "사용자 제공 정보와 일치"
SEARCH_UNAVAILABLE_CODE = "J5_SEARCH_UNAVAILABLE"
SEARCH_ERROR_CODE = "J5_SEARCH_ERROR"
FETCH_ERROR_CODE = "J5_FETCH_ERROR"
_SEARCH_BLOCKED = {
    SEARCH_UNAVAILABLE_CODE: ("search_unavailable", "판정 미완료 — 웹 검색을 사용할 수 없는 평가 환경(설정 꺼짐 또는 검색 도구 없는 평가자)이라 "
                                                    "외부 사실 주장을 확인하지 못했습니다. 사실 오류 판정이 아닙니다."),
    SEARCH_ERROR_CODE: ("search_error", "판정 미완료 — 웹 검색 도구 호출이 실패해 외부 사실 주장을 확인하지 못했습니다. "
                                        "사실 오류 판정이 아닙니다."),
    FETCH_ERROR_CODE: ("fetch_error", "판정 미완료 — 검색 없이 연 출처 원문(web_fetch)을 평가자 도구가 받지 못해 "
                                      "외부 사실 주장을 확인하지 못했습니다. 사실 오류 판정이 아닙니다."),
}
#: J5 상세 판정 사유. PASS/FAIL·미완료는 그대로 두고 그 이유만 구분한다.
J5_OUTCOMES = {
    "verified": "검증 완료",
    "contradicted": "사실 오류 — 근거와 충돌",
    "insufficient_evidence": "검증 기준 미충족 — 근거 부족",
    "held": "보류 — 확인하지 못해 사람이 판단",
    "search_limit_reached": "검증 기준 미충족 — 검색 한도 도달",
    "source_unavailable": "검증 기준 미충족 — 출처 원문 확인 실패",
    "source_mismatch": "출처 불일치 — 답변이 든 출처가 주장을 뒷받침하지 않음",
    "citation_inaccessible": "출처 접근 불가 — 답변이 열 수 없는 URL 을 출처로 제시",
    "process_misrepresented": "확인 과정 꾸밈 — 하지 않은 확인·조회를 했다고 씀 (items J5-3, 사실 오류 아님)",
    "search_unavailable": "판정 미완료 — 검색 불가",
    "search_error": "판정 미완료 — 검색 실행 오류",
    "fetch_error": "판정 미완료 — 원문 열기 도구 오류",
    "not_applicable": "해당 없음",
    "incomplete": "판정 미완료",
    "unclassified": "분류 정보 없음",
}
#: source_unavailable 의 세부 사유 (판정은 바꾸지 않는 기록용 구분).
SOURCE_FAILURE_KINDS = {
    "access_failed": "출처 원문 접근 실패",
    "quote_not_found": "원문은 받았으나 인용문 미확인",
    "mixed": "원문 접근 실패와 인용문 미확인이 함께 있음",
}
#: contradicted 의 충돌 근거 (판정·factual_error_confirmed 는 바꾸지 않는 기록용 구분 — v5).
CONTRADICTION_BASES = {
    "external": "외부 근거(검색·원문)와 충돌하는 사실 오류",
    "user_input": "사용자가 준 정보(질문·앞 대화)와 불일치 — 외부 사실 확인 결과가 아님",
    "reference": "제공된 참고 자료와 불일치",
    "citation": "답변이 출처로 든 문서의 내용과 불일치",
    "mixed": "여러 근거와 불일치",
}
_ORIGIN_BASIS = {"web": "external", "question": "user_input", "history_user": "user_input", "reference": "reference"}
#: citation_inaccessible 의 서버 수집 실패 상태 묶음 (판정은 그대로 — v5). 확인 환경(시간·연결·크기·형식) 쪽일 수 있는 상태,
#: 어느 쪽인지 상태만으로 알 수 없는 상태(HTTP 오류 — 404 와 접근 차단 403 이 같은 상태, 주소 없음), 답변이 든 주소 자체의 문제.
_ENVIRONMENT_STATUSES = frozenset({"timeout", "dns_timeout", "dns_busy", "source_limit", "fetch_error", "response_too_large",
                                   "decode_error", "unsupported_content_type", "unsupported_content_encoding", "unsupported_encoding",
                                   "invalid_compressed_body", "incomplete_compressed_body", "trailing_compressed_data", "incomplete_body"})
_UNCLEAR_STATUSES = frozenset({"http_error", "dns_error"})
CITATION_ACCESS_SIDES = {
    "evaluation_environment": "서버가 열지 못함 — 시간 초과·연결 등 확인 환경 문제일 수 있어 답변 결함으로 단정하지 않음",
    "unclear": "서버가 열지 못함 — 없는 주소인지 접근 차단인지 상태만으로 구분되지 않음",
    "answer_citation": "답변이 든 주소가 공개 웹 주소가 아니거나 이동이 올바르지 않음",
}
_INPUT_SCOPE_FAILURE_REASON = "사용자 원문 인용은 확인했지만, 요청에 주어진 자료·설정의 일치를 확인하는 주장인지 불명확합니다."

# These are independent retriever results, not judge-generated explanations.
# Search budgets, whole-request errors, absent citations and input-scope uncertainty
# deliberately remain unresolved instead of becoming factual failures.
SOURCE_ORIGINAL_FAILURE_STATUSES = frozenset({
    "http_error", "fetch_error", "quote_not_found", "unsupported_content_encoding",
    "unsupported_content_type", "response_too_large", "decode_error", "unsupported_encoding",
    "invalid_compressed_body", "incomplete_compressed_body", "trailing_compressed_data",
    "incomplete_body", "dns_error", "invalid_redirect",
    "timeout", "dns_timeout", "blocked_url", "blocked_port", "blocked_address",
    "redirect_limit", "redirect_host_changed",
})
_LEGACY_FAILURE_REASONS = {
    _REASONS[status]: status for status in SOURCE_ORIGINAL_FAILURE_STATUSES
}


def unresolved(decision):
    """판정이 확정되지 않은 기준 — 확인 필요(UNVERIFIED) 또는 판정 미완료(INCOMPLETE)."""
    return (isinstance(decision, dict) and decision.get("status") in ("UNVERIFIED", "INCOMPLETE")
            and decision.get("decision") not in ("PASS", "FAIL"))


def _has_verified_alternative(sources, basis):
    for source in sources:
        if not isinstance(source, dict):
            continue
        origin = source.get("origin") or ("web" if basis == "web" else None)
        if origin == "reference":
            # The finalizer retains a reference source only after exact-quote
            # verification. An unavailable optional web link does not negate it.
            return True
        if (origin == "web" and source.get("quote_verified") is True
                and source.get("authority") in ("primary", "reliable_secondary")
                and isinstance(source.get("authority_reason"), str) and source["authority_reason"].strip()
                and isinstance(source.get("temporal_relevance"), str) and source["temporal_relevance"].strip()):
            return True
    return False


def source_original_failures(record):
    """Return unresolved external-source failures with verified provenance.

    Older saved records do not contain ``verification_status`` on each source.
    For those records only exact, server-owned diagnostic text is accepted. A
    free-form model reason containing words such as '원문' is never sufficient.
    """
    if not isinstance(record, dict) or record.get("status") != "SUCCESS":
        return []
    decisions = record.get("decisions")
    j5 = decisions.get("J5") if isinstance(decisions, dict) else None
    if (not isinstance(j5, dict) or j5.get("status") != "UNVERIFIED"
            or j5.get("decision") in ("PASS", "FAIL")
            or "J5" not in (record.get("criteria") or [])):
        return []
    fact = j5.get("fact_check")
    verification = record.get("verification")
    if (not isinstance(fact, dict) or fact.get("basis") not in ("web", "mixed")
            or not isinstance(verification, dict)
            or verification.get("source_validation") != "provider_and_content"):
        return []
    failures = []
    claims = fact.get("claims")
    if not isinstance(claims, list):
        return []
    for claim_index, claim in enumerate(claims):
        if (not isinstance(claim, dict) or claim.get("verdict") != "UNVERIFIED"
                or claim.get("scope") not in (None, "external_fact")):
            continue
        sources = claim.get("sources")
        if not isinstance(sources, list):
            continue
        if _has_verified_alternative(sources, fact["basis"]):
            continue
        for source_index, source in enumerate(sources):
            if not isinstance(source, dict) or source.get("quote_verified") is not False:
                continue
            origin = source.get("origin") or ("web" if fact["basis"] == "web" else None)
            if origin != "web" or not isinstance(source.get("url"), str) or not source["url"]:
                continue
            status = source.get("verification_status")
            if status is None:
                reason = source.get("verification_reason")
                status = _LEGACY_FAILURE_REASONS.get(reason) if isinstance(reason, str) else None
            if not isinstance(status, str) or status not in SOURCE_ORIGINAL_FAILURE_STATUSES:
                continue
            failures.append({"claim_index": claim_index, "source_index": source_index,
                             "status": status, "url": source["url"]})
    return failures


def apply_source_original_failure_policy(record):
    """Update a normalized final record in place; safely repeatable for replay.

    Call after ``client.system_one`` returns, which includes any configured
    evidence recovery. Neither the answer nor the claim-level evidence changes.
    No probability or confidence is invented for the deterministic policy rule.
    """
    failures = source_original_failures(record)
    result = {"changed": bool(failures), "policy_version": SOURCE_ORIGINAL_FAILURE_POLICY_VERSION,
              "failures": failures}
    if not failures:
        return result
    j5 = record["decisions"]["J5"]
    if j5.get("reason_code") == SOURCE_ORIGINAL_FAILURE_CODE:   # 이미 보류로 정리한 레코드 — 다시 적용해도 바뀌지 않는다
        result["changed"] = False
        return result
    j5["policy_original_assessment"] = copy.deepcopy(j5)
    # 보류 — 확인하지 못한 것이지 사실 오류가 아니다. 판정 값을 비우고 UNVERIFIED 로 두어 사람이 정하게 한다 (v3)
    j5.update(status="UNVERIFIED", decision=None, confidence=None, probabilities={}, reason=SOURCE_ORIGINAL_FAILURE_REASON,
              reason_code=SOURCE_ORIGINAL_FAILURE_CODE,
              decision_policy={"version": SOURCE_ORIGINAL_FAILURE_POLICY_VERSION,
                               "outcome_basis": "held_for_review",
                               "factual_error_confirmed": False, "failures": copy.deepcopy(failures)})
    record["decision_policy"] = copy.deepcopy(SOURCE_DECISION_POLICY)
    record["review_required"] = any(unresolved(decision) for decision in record["decisions"].values())
    return result


def _revalidate_input_scope(record, state):
    """Restore the original PASS only after rechecking the rejected input gate.

    An old global PASS alone is insufficient. Every unresolved claim must have
    been rejected solely by the exact-input scope gate, and the complete current
    evidence check must pass against the unchanged question/answer/history.
    """
    if not isinstance(state, dict) or not isinstance(state.get("question"), str) or not isinstance(state.get("answer"), str):
        return None
    j5 = record["decisions"].get("J5")
    if (not isinstance(j5, dict) or j5.get("status") != "UNVERIFIED"
            or j5.get("decision") in ("PASS", "FAIL")
            or (j5.get("model_original_assessment") or {}).get("choice") != "PASS"):
        return None
    fact = j5.get("fact_check")
    if not isinstance(fact, dict) or fact.get("basis") not in ("input", "mixed"):
        return None
    claims = fact.get("claims")
    if not isinstance(claims, list) or not claims:
        return None
    rejected = []
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or claim.get("verdict") not in ("PASS", "UNVERIFIED"):
            return None
        if claim["verdict"] == "PASS":
            continue
        sources = claim.get("sources")
        if (claim.get("scope") != "input_consistency" or claim.get("reason") != _INPUT_SCOPE_FAILURE_REASON
                or not isinstance(sources, list) or not sources):
            return None
        if any(not isinstance(source, dict)
               or source.get("origin") not in ("question", "history_user")
               or source.get("input_quote_verified") is not True
               or source.get("input_scope_verified") is not False
               or source.get("verification_reason") != _INPUT_SCOPE_FAILURE_REASON
               for source in sources):
            return None
        rejected.append(index)
    if not rejected:
        return None
    candidate = copy.deepcopy(j5)
    candidate.update(choice="PASS", reason=j5["model_original_assessment"].get("reason") or INPUT_SCOPE_REVALIDATED_REASON)
    for index in rejected:
        candidate["fact_check"]["claims"][index].update(verdict="PASS", reason=INPUT_SCOPE_REVALIDATED_REASON)
    checked = finalize_answers({"answers": {"J5": candidate},
                                "verification": copy.deepcopy(record.get("verification") or {})}, state, {"J5": {}})
    checked_j5 = checked["answers"]["J5"]
    if (checked_j5.get("choice") != "PASS"
            or any(claim.get("verdict") != "PASS" for claim in checked_j5["fact_check"]["claims"])):
        return None
    # The ordinary evidence gate permits an adequate source to support a claim
    # even if an optional citation fails. Restoring a saved PASS is stricter:
    # every supplied-input citation must survive and independently revalidate.
    for before, after in zip(claims, checked_j5["fact_check"]["claims"]):
        original_inputs = [source for source in before.get("sources", [])
                           if isinstance(source, dict) and source.get("origin") in ("question", "history_user")]
        checked_inputs = [source for source in after.get("sources", [])
                          if isinstance(source, dict) and source.get("origin") in ("question", "history_user")]
        identity = lambda source: tuple(source.get(key) for key in ("origin", "turn", "url", "quote"))
        if ([identity(source) for source in original_inputs] != [identity(source) for source in checked_inputs]
                or any(source.get("input_quote_verified") is not True
                       or source.get("input_scope_verified") is not True
                       or source.get("quote_verified") is not True for source in checked_inputs)):
            return None
    return {"fact_check": checked_j5["fact_check"], "claim_indices": rejected}


def _codes(value):
    return [str(e) for e in value or [] if isinstance(e, str)]


def j5_search(record):
    """기록된 J5 검색 상태. 옛 결과에는 search_mode 가 없어 None 이다.

    errors 는 검색 도구 오류, fetch_errors 는 원문 열기(web_fetch) 도구 오류다. 둘을 나눠 기록하지 않은
    옛 결과는 tool_errors 전체를 검색 오류로 본다 (이전 해석 그대로).
    """
    verification = record.get("verification") if isinstance(record.get("verification"), dict) else {}
    split = "search_errors" in verification or "fetch_errors" in verification
    errors = _codes(verification.get("search_errors") if split else verification.get("tool_errors"))
    fetch_errors = _codes(verification.get("fetch_errors")) if split else []
    limits = verification.get("search_limits") if isinstance(verification.get("search_limits"), dict) else {}
    calls, limit = verification.get("search_calls"), limits.get("searches")
    reached = "max_uses_exceeded" in errors or (type(calls) is int and type(limit) is int and calls >= limit)
    return {"mode": verification.get("search_mode"), "performed": verification.get("search_performed") is True,
            "calls": calls if type(calls) is int else None, "limit": limit if type(limit) is int else None,
            "limit_reached": reached, "errors": [e for e in errors if e != "max_uses_exceeded"][:5],
            "fetch_errors": [e for e in fetch_errors if e != "max_uses_exceeded"][:5]}


def _external_unresolved(j5):
    """외부 사실 확인이 필요한데 끝나지 않은 주장이 있는가. 주장 목록이 없으면 판단할 수 없으므로 있다고 본다."""
    fact = j5.get("fact_check")
    claims = fact.get("claims") if isinstance(fact, dict) else None
    if not isinstance(claims, list) or not claims:
        return True
    return any(isinstance(c, dict) and c.get("verdict") == "UNVERIFIED" and c.get("scope") in (None, "external_fact")
               for c in claims)


def _search_blocked(record, j5):
    """검색이 불가능해서 J5 를 확인하지 못했으면 그 사유 코드. 검색을 했거나 기록이 없으면 None."""
    search = j5_search(record)
    if not _external_unresolved(j5):
        return None
    if search["mode"] in ("disabled", "unsupported"):
        return SEARCH_UNAVAILABLE_CODE
    if search["mode"] == "live" and not search["performed"]:
        # Same trigger as before (any tool error without a completed search); only the named tool differs.
        if search["errors"]:
            return SEARCH_ERROR_CODE
        if search["fetch_errors"]:
            return FETCH_ERROR_CODE
    return None


def j5_outcome(record):
    """J5 상세 판정 사유 — 사실 오류와 검증 불가·불충분을 구분한다. 판정 값은 바꾸지 않는다."""
    j5 = (record.get("decisions") or {}).get("J5")
    if not isinstance(j5, dict):
        return None
    status, decision, code = j5.get("status"), j5.get("decision"), j5.get("reason_code")
    fact = j5.get("fact_check") if isinstance(j5.get("fact_check"), dict) else None
    claims = fact.get("claims") if fact and isinstance(fact.get("claims"), list) else []
    if status == "NOT_APPLICABLE":
        outcome = "not_applicable"
    elif status == "INCOMPLETE":
        outcome = _SEARCH_BLOCKED.get(code, ("incomplete",))[0]
    elif status == "UNVERIFIED" and decision not in ("PASS", "FAIL"):
        outcome = "source_unavailable" if code == SOURCE_ORIGINAL_FAILURE_CODE else "held"
    elif decision == "PASS":
        outcome = "verified"
    elif decision == "FAIL" and code == SOURCE_ORIGINAL_FAILURE_CODE:
        outcome = "source_unavailable"
    elif decision == "FAIL" and code == FINAL_EVIDENCE_FAILURE_CODE:
        outcome = "search_limit_reached" if j5_search(record)["limit_reached"] else "insufficient_evidence"
    elif decision == "FAIL" and j5.get("citation_inaccessible") and not any(
            isinstance(c, dict) and c.get("verdict") == "FAIL" for c in claims):
        outcome = "citation_inaccessible"
    elif decision == "FAIL" and j5.get("citation_mismatch") and not any(
            isinstance(c, dict) and c.get("verdict") == "FAIL" for c in claims):
        outcome = "source_mismatch"
    elif decision == "FAIL" and (fact is not None or j5.get("citation_mismatch")):
        outcome = "contradicted"
    else:
        outcome = "unclassified"
    detail = {"policy": J5_OUTCOME_POLICY_VERSION, "code": outcome, "label": J5_OUTCOMES[outcome],
              "factual_error_confirmed": outcome == "contradicted", "search": j5_search(record)}
    if outcome == "source_unavailable":
        failure = source_failure_detail(j5)
        if failure:
            detail["source_failure"] = failure
    if outcome == "contradicted":
        detail["contradiction"] = contradiction_detail(j5)
    if outcome == "citation_inaccessible":
        access = citation_access_detail(j5)
        if access:
            detail["citation_access"] = access
    checks = [c for c in j5.get("citation_checks") or [] if isinstance(c, dict)]
    if checks:
        verdicts = [c.get("verdict") for c in checks]
        detail["citations"] = {"checked": len(checks), "supported": verdicts.count("supported"),
                               "mismatch": sum(v in ("contradicted", "unrelated", "not_found") for v in verdicts),
                               "inaccessible": verdicts.count("inaccessible"),
                               "unresolved": sum(v in ("unverified", "unchecked") for v in verdicts)}
    return detail


def items_j5_outcome(record):
    """items 파이프라인 J5 의 상세 판정 사유 — legacy 와 같은 모양(code·label·factual_error_confirmed). 판정 값은 바꾸지 않는다.

    점검 항목 결과로 나눈다: J5-1 거짓 확인(FACT_FALSE_CONFIRMED) → 외부 근거와 충돌 · J5-2 아니오 → 사용자가 준 자료와 불일치 ·
    재확인 3회 뒤 확인 불가(FACT_/ITEM_UNVERIFIABLE) → 근거 부족 · 답변이 든 URL 없음(SOURCE_NOT_FOUND) → 출처 접근 불가 ·
    J5-3 아니오 → 확인 과정 꾸밈. 확정된 결함이 여럿이면 사실 오류 쪽을 앞에 둔다.
    """
    j5 = (record.get("decisions") or {}).get("J5")
    if not isinstance(j5, dict):
        return None
    decision = j5.get("decision")
    failed = [item for sub in (j5.get("subs") or {}).values() if isinstance(sub, dict)
              for item in sub.get("items") or [] if isinstance(item, dict) and item.get("decision") == "NO"]
    base = lambda item: str(item.get("id") or "").split("#", 1)[0]
    confirmed = [i for i in failed if not i.get("unverifiable")]
    bases = []
    if any(i.get("reason_code") == "FACT_FALSE_CONFIRMED" for i in confirmed):
        bases.append("external")
    if any(base(i).startswith("J5-2") for i in confirmed):
        bases.append("user_input")
    if j5.get("status") == "NOT_APPLICABLE":
        outcome = "not_applicable"
    elif decision == "PASS":
        outcome = "verified"
    elif decision != "FAIL":
        outcome = "incomplete"
    elif bases:
        outcome = "contradicted"
    elif any(i.get("reason_code") == "SOURCE_NOT_FOUND" for i in confirmed):
        outcome = "citation_inaccessible"
    elif any(base(i).startswith("J5-3") for i in confirmed):
        outcome = "process_misrepresented"
    elif failed and all(i.get("unverifiable") for i in failed):
        outcome = "insufficient_evidence"
    else:
        outcome = "unclassified"
    detail = {"policy": J5_OUTCOME_POLICY_VERSION, "code": outcome, "label": J5_OUTCOMES[outcome],
              "factual_error_confirmed": outcome == "contradicted", "search": j5_search(record), "pipeline": "items",
              "items": sorted({base(i) + (" (확인 불가)" if i.get("unverifiable") else "") for i in failed})}
    if outcome == "contradicted":
        kind = bases[0] if len(bases) == 1 else "mixed"
        detail["contradiction"] = {"basis": kind, "label": CONTRADICTION_BASES[kind], "bases": sorted(bases)}
    return detail


def contradiction_detail(j5):
    """contradicted 의 충돌 근거 — FAIL 주장이 기댄 출처의 origin 으로 외부(web)·사용자 입력(question·history_user)·참고 자료를 나눈다.
    FAIL 주장 없이 답변 인용 출처 불일치만 있으면 citation. 판정 값은 보지 않고 바꾸지 않는다."""
    fact = j5.get("fact_check") if isinstance(j5.get("fact_check"), dict) else {}
    basis = fact.get("basis")
    bases = set()
    for claim in fact.get("claims") or []:
        if not isinstance(claim, dict) or claim.get("verdict") != "FAIL":
            continue
        origins = {s.get("origin") or (basis if basis in ("web", "reference") else None)
                   for s in claim.get("sources") or [] if isinstance(s, dict)}
        found = {_ORIGIN_BASIS[o] for o in origins if o in _ORIGIN_BASIS}
        if not found:   # 출처 기록이 없는 FAIL 주장 — 주장 범위로만 구분
            found = {"user_input" if claim.get("scope") == "input_consistency" else "external"}
        bases |= found
    if not bases and j5.get("citation_mismatch"):
        bases = {"citation"}
    kind = next(iter(bases)) if len(bases) == 1 else "mixed" if bases else "external"
    return {"basis": kind, "label": CONTRADICTION_BASES[kind], "bases": sorted(bases)}


def citation_access_detail(j5):
    """citation_inaccessible 의 서버 수집 실패 상태별 수와 주체 — 확인 환경 쪽 상태가 하나라도 있으면 evaluation_environment."""
    statuses = [str(c.get("server_status")) for c in j5.get("citation_checks") or []
                if isinstance(c, dict) and c.get("verdict") == "inaccessible" and c.get("server_status")]
    if not statuses:
        return None
    side = ("evaluation_environment" if any(s in _ENVIRONMENT_STATUSES for s in statuses)
            else "unclear" if any(s in _UNCLEAR_STATUSES for s in statuses) else "answer_citation")
    return {"side": side, "label": CITATION_ACCESS_SIDES[side], "statuses": {s: statuses.count(s) for s in dict.fromkeys(statuses)}}


def source_failure_detail(j5):
    """SOURCE_ORIGINAL_UNAVAILABLE 보류(v2 까지는 FAIL)의 실패 출처를 접근 실패와 인용문 미확인으로 나눈다. 판정 값은 보지 않고 바꾸지 않는다.

    인용문 미확인(quote_not_found)은 서버가 원문을 받았거나, 서버는 못 받았지만 평가자 web_fetch 원문(provider_document)을
    대신 대조한 경우다. 나머지 실패 상태는 원문을 받지 못한 접근 실패다.
    """
    policy = j5.get("decision_policy") if isinstance(j5.get("decision_policy"), dict) else {}
    failures = [f for f in policy.get("failures") or [] if isinstance(f, dict)]
    if not failures:
        return None
    fact = j5.get("fact_check") if isinstance(j5.get("fact_check"), dict) else {}
    claims = fact.get("claims") if isinstance(fact.get("claims"), list) else []
    access, quote, via_provider = [], 0, 0
    for failure in failures:
        if failure.get("status") == "quote_not_found":
            quote += 1
            try:
                source = claims[failure["claim_index"]]["sources"][failure["source_index"]]
            except (KeyError, IndexError, TypeError):
                source = None
            via_provider += isinstance(source, dict) and source.get("retrieval") == "provider_document"
        else:
            access.append({"status": failure.get("status"), "url": failure.get("url")})
    kind = "mixed" if access and quote else "access_failed" if access else "quote_not_found"
    return {"kind": kind, "label": SOURCE_FAILURE_KINDS[kind], "access_failures": access,
            "quote_not_found": quote, "quote_not_found_in_provider_document": via_provider}


def apply_final_decision_policy(record, *, state=None):
    """Finish successfully evaluated criteria: PASS/FAIL stay, unverified ones are held for review (v3), with an audit.

    Provider/tool errors and unrequested criteria (for example a missing-history
    J6 check) remain evaluation errors. Existing binary outcomes are never
    overwritten. A lack of evidence is never turned into a FAIL (it is held, UNVERIFIED,
    for a person to decide), and a recovered input PASS requires revalidated original input.
    """
    result = {"changed": False, "policy_version": FINAL_DECISION_POLICY_VERSION, "changes": []}
    if not isinstance(record, dict) or record.get("status") != "SUCCESS" or not isinstance(record.get("decisions"), dict):
        return result
    if record.get("pipeline") == "items":
        # items 레코드는 세부 기준 집계가 이미 PASS/FAIL 을 확정했고(재확인 3회 뒤 확인 불가 FAIL), UNVERIFIED 상태를 만들지 않는다.
        return result
    requested = set(record.get("criteria") or []) - set(record.get("not_requested") or [])
    restored = _revalidate_input_scope(record, state) if "J5" in requested else None
    if restored:
        j5 = record["decisions"]["J5"]
        j5["policy_original_assessment"] = copy.deepcopy(j5)
        j5.pop("status", None)
        j5.update(decision="PASS", confidence=None, probabilities={}, reason=INPUT_SCOPE_REVALIDATED_REASON,
                  reason_code=INPUT_SCOPE_REVALIDATED_CODE, fact_check=restored["fact_check"],
                  decision_policy={"version": FINAL_DECISION_POLICY_VERSION,
                                   "outcome_basis": "validated_input_consistency", "factual_error_confirmed": False,
                                   "revalidated_claim_indices": restored["claim_indices"]})
        result["changes"].append({"criterion_id": "J5", "decision": "PASS", "reason_code": INPUT_SCOPE_REVALIDATED_CODE,
                                   "revalidated_claim_indices": restored["claim_indices"]})
    if "J5" in requested:
        source_change = apply_source_original_failure_policy(record)
        if source_change["changed"]:
            result["changes"].append({"criterion_id": "J5", "status": "UNVERIFIED", "reason_code": SOURCE_ORIGINAL_FAILURE_CODE,
                                       "failures": source_change["failures"]})
    j5 = record["decisions"].get("J5")
    if ("J5" in requested and isinstance(j5, dict) and j5.get("status") == "UNVERIFIED"
            and j5.get("decision") not in ("PASS", "FAIL")):
        blocked = _search_blocked(record, j5)
        if blocked:
            # Search could not run, so the claim was never checked: an evaluation gap, not an answer FAIL.
            j5["policy_original_assessment"] = copy.deepcopy(j5)
            j5.update(status="INCOMPLETE", decision=None, reason=_SEARCH_BLOCKED[blocked][1], reason_code=blocked,
                      decision_policy={"version": J5_OUTCOME_POLICY_VERSION, "outcome_basis": "search_not_available",
                                       "factual_error_confirmed": False})
            result["changes"].append({"criterion_id": "J5", "status": "INCOMPLETE", "reason_code": blocked})
    for cid, decision in record["decisions"].items():
        if (cid not in requested or not isinstance(decision, dict) or decision.get("status") != "UNVERIFIED"
                or decision.get("decision") in ("PASS", "FAIL")
                or decision.get("reason_code") in (FINAL_EVIDENCE_FAILURE_CODE, SOURCE_ORIGINAL_FAILURE_CODE)):   # 이미 보류
            continue
        decision["policy_original_assessment"] = copy.deepcopy(decision)
        # 보류(v3) — UNVERIFIED 를 유지하고 사유만 적는다. 평가 모델이 남긴 사유는 policy_original_assessment 에 그대로 있다.
        original_reason = decision.get("reason") if isinstance(decision.get("reason"), str) else ""
        decision.update(decision=None, reason=FINAL_EVIDENCE_FAILURE_REASON + (f" · {original_reason}" if original_reason else ""),
                        reason_code=FINAL_EVIDENCE_FAILURE_CODE,
                        decision_policy={"version": FINAL_DECISION_POLICY_VERSION,
                                         "outcome_basis": "held_for_review", "factual_error_confirmed": False})
        result["changes"].append({"criterion_id": cid, "status": "UNVERIFIED", "reason_code": FINAL_EVIDENCE_FAILURE_CODE})
    result["changed"] = bool(result["changes"])
    if result["changed"] or record.get("reevaluation") or record.get("context_reevaluation"):
        record["decision_policy"] = copy.deepcopy(DECISION_POLICY)
        record["review_required"] = any(unresolved(decision) for decision in record["decisions"].values())
    return result
