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


SOURCE_ORIGINAL_FAILURE_POLICY_VERSION = "j5-source-original-failure-v1"
FINAL_DECISION_POLICY_VERSION = "semantic-binary-outcome-v2"
J5_OUTCOME_POLICY_VERSION = "j5-outcome-detail-v1"
SOURCE_DECISION_POLICY = {"j5_source_original_failure": SOURCE_ORIGINAL_FAILURE_POLICY_VERSION}
DECISION_POLICY = {**SOURCE_DECISION_POLICY, "semantic_final_outcome": FINAL_DECISION_POLICY_VERSION,
                   "judge_fail_evidence": REASON_EVIDENCE_POLICY_VERSION, "j5_outcome": J5_OUTCOME_POLICY_VERSION}
SOURCE_ORIGINAL_FAILURE_REASON = "출처 원문 확인 실패로 검증 기준 미충족"
SOURCE_ORIGINAL_FAILURE_CODE = "SOURCE_ORIGINAL_UNAVAILABLE"
FINAL_EVIDENCE_FAILURE_CODE = "EVALUATION_EVIDENCE_INSUFFICIENT"
FINAL_EVIDENCE_FAILURE_REASON = "평가 근거 부족으로 검증 기준 미충족"
INPUT_SCOPE_REVALIDATED_CODE = "INPUT_SCOPE_REVALIDATED"
INPUT_SCOPE_REVALIDATED_REASON = "사용자 제공 정보와 일치"
SEARCH_UNAVAILABLE_CODE = "J5_SEARCH_UNAVAILABLE"
SEARCH_ERROR_CODE = "J5_SEARCH_ERROR"
_SEARCH_BLOCKED = {
    SEARCH_UNAVAILABLE_CODE: ("search_unavailable", "판정 미완료 — 웹 검색을 사용할 수 없는 평가 환경(설정 꺼짐 또는 검색 도구 없는 평가자)이라 "
                                                    "외부 사실 주장을 확인하지 못했습니다. 사실 오류 판정이 아닙니다."),
    SEARCH_ERROR_CODE: ("search_error", "판정 미완료 — 웹 검색 도구 호출이 실패해 외부 사실 주장을 확인하지 못했습니다. "
                                        "사실 오류 판정이 아닙니다."),
}
#: J5 상세 판정 사유. PASS/FAIL·미완료는 그대로 두고 그 이유만 구분한다.
J5_OUTCOMES = {
    "verified": "검증 완료",
    "contradicted": "사실 오류 — 근거와 충돌",
    "insufficient_evidence": "검증 기준 미충족 — 근거 부족",
    "search_limit_reached": "검증 기준 미충족 — 검색 한도 도달",
    "source_unavailable": "검증 기준 미충족 — 출처 원문 확인 실패",
    "source_mismatch": "출처 불일치 — 답변이 든 출처가 주장을 뒷받침하지 않음",
    "search_unavailable": "판정 미완료 — 검색 불가",
    "search_error": "판정 미완료 — 검색 실행 오류",
    "not_applicable": "해당 없음",
    "incomplete": "판정 미완료",
    "unclassified": "분류 정보 없음",
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
    j5["policy_original_assessment"] = copy.deepcopy(j5)
    j5.pop("status", None)
    j5.update(decision="FAIL", reason=SOURCE_ORIGINAL_FAILURE_REASON,
              reason_code=SOURCE_ORIGINAL_FAILURE_CODE,
              decision_policy={"version": SOURCE_ORIGINAL_FAILURE_POLICY_VERSION,
                               "outcome_basis": "source_verification_requirement",
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


def j5_search(record):
    """기록된 J5 검색 상태. 옛 결과에는 search_mode 가 없어 None 이다."""
    verification = record.get("verification") if isinstance(record.get("verification"), dict) else {}
    errors = [str(e) for e in verification.get("tool_errors") or [] if isinstance(e, str)]
    limits = verification.get("search_limits") if isinstance(verification.get("search_limits"), dict) else {}
    calls, limit = verification.get("search_calls"), limits.get("searches")
    reached = "max_uses_exceeded" in errors or (type(calls) is int and type(limit) is int and calls >= limit)
    return {"mode": verification.get("search_mode"), "performed": verification.get("search_performed") is True,
            "calls": calls if type(calls) is int else None, "limit": limit if type(limit) is int else None,
            "limit_reached": reached, "errors": [e for e in errors if e != "max_uses_exceeded"][:5]}


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
    if search["mode"] == "live" and not search["performed"] and search["errors"]:
        return SEARCH_ERROR_CODE
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
    elif decision == "PASS":
        outcome = "verified"
    elif decision == "FAIL" and code == SOURCE_ORIGINAL_FAILURE_CODE:
        outcome = "source_unavailable"
    elif decision == "FAIL" and code == FINAL_EVIDENCE_FAILURE_CODE:
        outcome = "search_limit_reached" if j5_search(record)["limit_reached"] else "insufficient_evidence"
    elif decision == "FAIL" and j5.get("citation_mismatch") and not any(
            isinstance(c, dict) and c.get("verdict") == "FAIL" for c in claims):
        outcome = "source_mismatch"
    elif decision == "FAIL" and (fact is not None or j5.get("citation_mismatch")):
        outcome = "contradicted"
    else:
        outcome = "unclassified"
    return {"policy": J5_OUTCOME_POLICY_VERSION, "code": outcome, "label": J5_OUTCOMES[outcome],
            "factual_error_confirmed": outcome == "contradicted", "search": j5_search(record)}


def apply_final_decision_policy(record, *, state=None):
    """Finish successfully evaluated criteria as PASS or FAIL, with an audit.

    Provider/tool errors and unrequested criteria (for example a missing-history
    J6 check) remain evaluation errors. Existing binary outcomes are never
    overwritten. A deterministic lack-of-evidence FAIL is not a factual-error
    assertion, and a recovered input PASS requires revalidated original input.
    """
    result = {"changed": False, "policy_version": FINAL_DECISION_POLICY_VERSION, "changes": []}
    if not isinstance(record, dict) or record.get("status") != "SUCCESS" or not isinstance(record.get("decisions"), dict):
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
            result["changes"].append({"criterion_id": "J5", "decision": "FAIL", "reason_code": SOURCE_ORIGINAL_FAILURE_CODE,
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
                or decision.get("decision") in ("PASS", "FAIL")):
            continue
        decision["policy_original_assessment"] = copy.deepcopy(decision)
        decision.pop("status", None)
        decision.update(decision="FAIL", reason=FINAL_EVIDENCE_FAILURE_REASON,
                        reason_code=FINAL_EVIDENCE_FAILURE_CODE,
                        decision_policy={"version": FINAL_DECISION_POLICY_VERSION,
                                         "outcome_basis": "evidence_requirement", "factual_error_confirmed": False})
        result["changes"].append({"criterion_id": cid, "decision": "FAIL", "reason_code": FINAL_EVIDENCE_FAILURE_CODE})
    result["changed"] = bool(result["changes"])
    if result["changed"] or record.get("reevaluation"):
        record["decision_policy"] = copy.deepcopy(DECISION_POLICY)
        record["review_required"] = any(unresolved(decision) for decision in record["decisions"].values())
    return result
