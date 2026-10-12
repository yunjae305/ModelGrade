"""Apply explicit evaluation policy after evidence recovery has finished.

A source-verification policy failure does not assert that the underlying factual
claim is false. The original unresolved assessment and evidence remain available
for audit; only the final J5 evaluation outcome changes.
"""
from __future__ import annotations

import copy

from .semantic_evidence import finalize_answers
from .semantic_sources import _REASONS


SOURCE_ORIGINAL_FAILURE_POLICY_VERSION = "j5-source-original-failure-v1"
FINAL_DECISION_POLICY_VERSION = "semantic-binary-outcome-v1"
SOURCE_DECISION_POLICY = {"j5_source_original_failure": SOURCE_ORIGINAL_FAILURE_POLICY_VERSION}
#: items 파이프라인의 재확인 정책 — 3회 뒤 남은 문제는 '확인 불가로 FAIL' (semantic_quotes.RECHECK_POLICY 와 같은 값)
ITEMS_RECHECK_POLICY_VERSION = "items-recheck-3-then-fail-v1"
DECISION_POLICY = {**SOURCE_DECISION_POLICY, "semantic_final_outcome": FINAL_DECISION_POLICY_VERSION,
                   "items_recheck": ITEMS_RECHECK_POLICY_VERSION}
SOURCE_ORIGINAL_FAILURE_REASON = "출처 원문 확인 실패로 검증 기준 미충족"
SOURCE_ORIGINAL_FAILURE_CODE = "SOURCE_ORIGINAL_UNAVAILABLE"
FINAL_EVIDENCE_FAILURE_CODE = "EVALUATION_EVIDENCE_INSUFFICIENT"
FINAL_EVIDENCE_FAILURE_REASON = "평가 근거 부족으로 검증 기준 미충족"
INPUT_SCOPE_REVALIDATED_CODE = "INPUT_SCOPE_REVALIDATED"
INPUT_SCOPE_REVALIDATED_REASON = "사용자 제공 정보와 일치"
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
    record["review_required"] = any(
        isinstance(decision, dict) and decision.get("status") == "UNVERIFIED"
        for decision in record["decisions"].values())
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
            result["changes"].append({"criterion_id": "J5", "decision": "FAIL", "reason_code": SOURCE_ORIGINAL_FAILURE_CODE,
                                       "failures": source_change["failures"]})
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
    if result["changed"]:
        record["decision_policy"] = copy.deepcopy(DECISION_POLICY)
        record["review_required"] = any(isinstance(decision, dict) and decision.get("status") == "UNVERIFIED"
                                         for decision in record["decisions"].values())
    return result
