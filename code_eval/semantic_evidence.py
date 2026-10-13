"""Validate evidence returned by generative judges without modifying source answers.

Provider tool events establish retrieval provenance, not the truth of a claim.
The model still judges source quality/relevance; missing evidence stays unverified.
"""
from __future__ import annotations

import copy
import json
import re
from urllib.parse import urlsplit, urlunsplit

#: v3.12 — 추천·평가·선호 표현과 교정·번역 결과 문장 자체는 J5 주장이 아니다 (F7, J5 지시문 한 문단).
FACT_CHECK_POLICY = "evidence-v3.15-quote-markup"   # v3.15: 출처 인용 대조 — 인용의 Markdown 굵게 표시를 벗긴 형태도 원문과 대조 (원문은 그대로)
#: v3 — 코드 검사가 이어받는 형식·분량 지시는 J6 context_checks 에서 뺀다 (F6, J6 지시문 한 문단).
CONTEXT_CHECK_POLICY = "history-quotes-v3-code-boundary"
SEARCH_POLICY = "J5_without_reference_grouped_evidence_v3"


def has_reference(state):
    value = state.get("reference") if isinstance(state, dict) else None
    return value is not None and value not in ("", {}, []) and (not isinstance(value, str) or bool(value.strip()))


def search_mode(questions, state, web_search, supported=True):
    """J5 웹 검색 상태. 판정 정책이 '검색 불가'와 '검색했지만 근거 부족'을 구분할 때 쓴다.

    live: 검색 사용 · reference: 참조 자료로만 판정(검색 안 함) · disabled: 설정으로 끔 ·
    unsupported: 이 평가자에 검색 도구 없음 · not_requested: J5 를 묻지 않음 (items 파이프라인은 J5-* 항목)
    """
    if "J5" not in questions and not any(str(q).startswith("J5-") for q in questions):
        return "not_requested"
    if has_reference(state):
        return "reference"
    if not supported:
        return "unsupported"
    return "live" if web_search == "live" else "disabled"


def source_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return None
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))
    except ValueError:
        return None


def _text(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def _contained(quote, text):
    return bool(_text(quote)) and _text(quote) in _text(text)


def _unverified(entry, reason):
    entry.update(choice="UNVERIFIED", probabilities={}, confidence=0.0, reason=reason)


def _verdict(entry, decision):
    if decision == "UNVERIFIED":
        entry.update(choice=decision, probabilities={}, confidence=0.0)
    elif entry.get("choice") != decision:
        # Aggregation is deterministic, never manufacture a confidence probability.
        entry.update(choice=decision, confidence=0.0, probabilities={"PASS": 0.5, "FAIL": 0.5})


def _aggregate(values):
    return "FAIL" if "FAIL" in values else "PASS" if values and all(v == "PASS" for v in values) else "UNVERIFIED"


def _input_scope(claim, state, source=None):
    """Accept task inputs as inputs, never as proof of their real-world truth.

    evidence-v3.13 (2026-10-13 사용자 결정): J5 는 답변이 말한 값·정보가 실제 데이터와 맞는가를 본다. 사용자가 준 값이면
    그 실제 데이터는 사용자 원문이다 — 요청 종류(요약·계산·작성·추천 …)를 키워드로 알아보지 못했다고 일치 확인을 막지 않는다
    (v3.12 까지는 키워드가 안 맞으면 '확인 필요'로 남아 저장 실행의 미완료 대부분을 만들었다).
    남는 경계: 평가 모델이 입력 일치로 분류하고 요청 인용이 사용자 원문(현재 질문 또는 이전 사용자 턴)에 있을 것,
    진위 확인 요청·실제(외부) 데이터 요청이 아닐 것.
    정정·이름 변경처럼 요청 문장 자체가 값을 주는 경우도 입력이다. 사용자 원문·턴의 정확한 인용 대조는 호출부(_input_source)가 한다.
    """
    quote = claim.get("scope_quote")
    question = state.get("question", "")
    # 멀티턴에서는 값을 준 요청이 앞 턴에 있다 — 이전 사용자 턴의 요청 인용도 사용자 원문이다 (이전 AI 답변은 아님)
    history = [h for h in state.get("history") or [] if isinstance(h, dict) and isinstance(h.get("question"), str)]
    if claim.get("scope") != "input_consistency":
        return False
    if _contained(quote, question):
        checked = [question]
    else:
        earlier = [h["question"] for h in sorted(history, key=lambda h: h.get("turn") or 0) if _contained(quote, h["question"])]
        if not earlier:
            return False
        checked = [question, earlier[-1]]          # 요청 인용이 나온 가장 가까운 앞 사용자 턴 하나
    if isinstance(source, dict) and source.get("origin") == "history_user":
        # 값을 가져온 사용자 턴 자체가 진위 확인을 요청했으면 그 값은 스스로를 증명하지 못한다
        checked += [h["question"] for h in history if h.get("turn") == source.get("turn")]
    verification = (r"사실인지|참인지|정확한지|맞는지|진위|정확성|사실\s*(?:확인|검증)|팩트\s*체크|검증해|검색해|조사해|"
                    # 직접 묻는 진위 질문: '이 말이 사실이야?', '맞아?', '진짜야?', 'Is this true?'
                    r"(?:사실|진짜|정말|참)(?:이야|이에요|인가요|입니까|이니|일까|인지)?\s*\?|맞(?:아|나요|니|습니까|을까|죠|지)\s*\?|"
                    r"\b(?:is|are) (?:this|that|it|these|they) (?:true|correct|accurate|right)\b|true or false|"
                    r"fact[ -]?check|verify|whether.{0,35}(?:true|correct)|"
                    r"(?:check|confirm).{0,50}(?:truth|accuracy|correct|authentic)")
    if any(re.search(verification, text, re.I) for text in checked):
        return False   # 주어진 자료가 사실인지 묻는 요청에서는 자료가 스스로를 증명하지 못한다
    external_request = (r"(?:국내|전국|전\s*세계|실제\s*(?:운영|시장|판매)).{0,60}"
                        r"(?:정원|가격|요금|통계|현황)|(?:nationwide|worldwide|real.world).{0,60}(?:price|data|statistic)")
    if any(re.search(external_request, text, re.I) for text in checked):
        return False   # 실제 데이터를 요청했으면 사용자 입력은 그 근거가 아니다 — 자료 문장만 인용해 우회하지 못하게 질문 전체에서 본다
    return True


def _input_source(source, claim, state):
    retained = copy.deepcopy(source)
    retained.update(quote_verified=False, verified_at=None, resolved_url=None)
    origin, turn = source.get("origin"), source.get("turn")
    text = None
    if origin == "question" and turn is None:
        text = state.get("question", "")
    elif origin == "history_user" and type(turn) is int:
        matches = [h for h in state.get("history", []) if isinstance(h, dict) and h.get("turn") == turn]
        if len(matches) == 1:
            text = matches[0].get("question", "")
    scope_verified = _input_scope(claim, state, source)
    quote_verified = bool(not source.get("url") and text is not None and _contained(source.get("quote"), text))
    valid = scope_verified and quote_verified
    # Diagnostic fields are server-owned. The existing proof gate still requires
    # both checks, so an exact quote alone cannot verify a self-supporting premise.
    retained.update(input_quote_verified=quote_verified, input_scope_verified=scope_verified)
    retained["quote_verified"] = valid
    retained["verification_reason"] = ("제공 자료의 원문에서 인용문을 확인했습니다. 자료 자체의 사실성은 검증하지 않습니다."
                                       if valid else "사용자 원문 인용은 확인했지만, 요청에 주어진 자료·설정의 일치를 확인하는 주장인지 불명확합니다."
                                       if quote_verified else "지정한 사용자 원문·턴에서 유효한 인용 근거를 확인하지 못했습니다.")
    return retained, valid


def _fact_check(entry, state, verification):
    fact = entry.get("fact_check")
    if entry.get("choice") == "NOT_APPLICABLE":
        if isinstance(fact, dict) and fact.get("basis") == "none" and fact.get("claims") == [] and _text(entry.get("reason")):
            entry["probabilities"] = {}
            return
        _unverified(entry, "사실 정보 미적용 판정과 검증 주장 목록이 일치하지 않습니다.")
        return
    if not isinstance(fact, dict) or not isinstance(fact.get("claims"), list) or not fact["claims"]:
        entry["fact_check"] = {"basis": "none", "claims": []}
        _unverified(entry, "확인할 사실 주장과 출처 근거가 없어 정보 신뢰도를 확정하지 않았습니다.")
        return
    basis = fact.get("basis")
    reference = state.get("reference")
    reference_text = reference if isinstance(reference, str) else json.dumps(reference, ensure_ascii=False)
    observed = {u for value in verification.get("source_urls", []) if (u := source_url(value))}
    # Pages the provider opened with web_fetch count as retrieved even without a search.
    fetched = {u for value in verification.get("fetched_urls", []) if (u := source_url(value))} & observed
    search_ok = verification.get("search_performed") is True
    normalized = []
    for claim in fact["claims"]:
        if not isinstance(claim, dict):
            normalized.append({"claim": "", "verdict": "UNVERIFIED", "reason": "잘못된 주장 형식", "sources": []})
            continue
        item = copy.deepcopy(claim)
        sources = []
        supported_sources = 0
        source_problems = []
        problem = None
        if not _contained(item.get("claim"), state.get("answer", "")):
            problem = "검증 주장이 평가 대상 답변 원문에 없습니다."
        if item.get("verdict") not in ("PASS", "FAIL", "UNVERIFIED"):
            problem = "주장 판정 형식이 올바르지 않습니다."
        for source in item.get("sources", []) if isinstance(item.get("sources"), list) else []:
            if not isinstance(source, dict) or not _text(source.get("quote")):
                continue
            url = source.get("url", "")
            origin = source.get("origin") or (basis if basis in ("web", "reference") else None)
            if origin in ("question", "history_user") and basis in ("input", "mixed"):
                retained, valid = _input_source(source, item, state)
                sources.append(retained)
                supported_sources += int(valid)
                if not valid:
                    source_problems.append(retained["verification_reason"])
            elif origin == "reference" and basis in ("reference", "mixed") and has_reference(state):
                if _contained(source["quote"], reference_text) and (not url or source_url(url)):
                    retained = {k: str(source.get(k) or "") for k in ("url", "title", "quote")}
                    if source.get("origin") is not None:
                        retained.update(origin="reference", turn=None)
                    sources.append(retained)
                    supported_sources += 1
                else:
                    source_problems.append("참조 자료에서 인용문을 확인하지 못했습니다.")
            elif (origin == "web" and basis in ("web", "mixed") and source_url(url) in observed
                  and (search_ok or source_url(url) in fetched)):
                retained = copy.deepcopy(source)
                # These flags are populated by the transport's independent retriever,
                # never by the model's structured answer schema.
                original_checked = (verification.get("source_validation") == "provider_and_content"
                                    and source.get("quote_verified") is True)
                credible = (source.get("authority") in ("primary", "reliable_secondary")
                            and bool(_text(source.get("authority_reason"))))
                timely = bool(_text(source.get("temporal_relevance")))
                if original_checked and credible and timely:
                    supported_sources += 1
                elif original_checked:
                    retained["assessment_warning"] = "출처의 공신력 또는 기준 시점 적합성을 판단한 근거가 부족합니다."
                if not (original_checked and credible and timely):
                    source_problems.append(retained.get("assessment_warning") or retained.get("verification_reason")
                                           or "검색 출처의 원문 인용을 확인하지 못했습니다.")
                sources.append(retained)
            else:
                source_problems.append("완료된 검색·원문 열기 기록 또는 허용된 제공 자료에서 이 출처를 확인하지 못했습니다.")
        if item.get("verdict") in ("PASS", "FAIL") and not supported_sources:
            fallback = ("검색 출처의 원문 인용·공신력·기준 시점 근거를 모두 확인하지 못했습니다."
                        if basis == "web" else "제공 자료 또는 검색 출처의 원문 근거가 없습니다."
                        if basis in ("input", "mixed") else "참조 자료의 원문 근거가 없습니다.")
            problem = problem or " · ".join(dict.fromkeys(source_problems)) or fallback
        item["sources"] = sources
        if problem:
            item.update(verdict="UNVERIFIED", reason=problem)
        normalized.append(item)
    fact["claims"] = normalized
    if basis not in ("web", "reference", "input", "mixed"):
        fact["basis"] = "none"
    verdict = _aggregate([it["verdict"] for it in normalized])
    # Keep model abstention: a subset of cited claims must not erase acknowledged gaps.
    if entry.get("choice") in ("UNVERIFIED", "FAIL") and verdict == "PASS":
        verdict = "UNVERIFIED"
    _verdict(entry, verdict)
    if verdict == "UNVERIFIED":
        reasons = " · ".join(dict.fromkeys(it.get("reason") or "근거 부족" for it in normalized if it["verdict"] == "UNVERIFIED"))
        entry["reason"] = "정보 신뢰도 확인 필요: " + (reasons or "전체 판정과 주장별 근거가 일치하지 않습니다.")
    elif verdict == "FAIL":
        entry["reason"] = " · ".join(it.get("reason") or "출처와 모순되는 정보" for it in normalized if it["verdict"] == "FAIL")


def _context_check(entry, state):
    checks = entry.get("context_checks")
    history = {item.get("turn"): item for item in state.get("history", []) if isinstance(item, dict)}
    if not isinstance(checks, list) or not checks:
        entry["context_checks"] = []
        _unverified(entry, "이전 대화의 지시·설정과 현재 답변을 대조한 근거가 없습니다.")
        return
    normalized = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        item = copy.deepcopy(check)
        turn = item.get("turn")
        prior = history.get(turn, {}) if isinstance(turn, int) and not isinstance(turn, bool) else {}
        valid_prior = any(_contained(item.get("instruction_quote"), prior.get(k, "")) for k in ("question", "answer"))
        quote = item.get("answer_quote")
        valid_answer = isinstance(quote, str) and (
            _contained(quote, state.get("answer", "")) or (not quote.strip() and item.get("decision") != "PASS"))
        problems = []
        if not valid_prior:
            problems.append("지정한 이전 턴이 없거나 그 턴의 인용문이 원문과 일치하지 않습니다.")
        if not valid_answer:
            problems.append("현재 답변의 인용문이 없거나 원문과 일치하지 않습니다.")
        if item.get("decision") not in ("PASS", "FAIL", "UNVERIFIED"):
            problems.append("대화 맥락 판정 형식이 올바르지 않습니다.")
        if problems:
            item.update(decision="UNVERIFIED", reason=" · ".join(problems))
        normalized.append(item)
    entry["context_checks"] = normalized
    verdict = _aggregate([it["decision"] for it in normalized])
    if entry.get("choice") in ("UNVERIFIED", "FAIL") and verdict == "PASS":
        verdict = "UNVERIFIED"
    _verdict(entry, verdict)
    if verdict == "UNVERIFIED":
        entry["reason"] = "대화 맥락 확인 필요: 이전 지시와 현재 답변의 검증 가능한 근거가 부족합니다."
    elif verdict == "FAIL":
        entry["reason"] = " · ".join(it.get("reason") or "이전 지시를 유지하지 못함" for it in normalized if it["decision"] == "FAIL")


CITATION_LABELS = {"supported": "출처가 주장을 뒷받침", "contradicted": "출처가 주장과 모순",
                   "unrelated": "출처가 주장과 무관", "not_found": "출처에 해당 내용 없음",
                   "inaccessible": "출처 원문 접근 불가", "unverified": "판정 확정 안 함", "unchecked": "확인하지 않음"}
_CITATION_MISMATCH = ("contradicted", "unrelated", "not_found")
#: evidence-v3.14 (2026-10-13 사용자 결정) — 답변이 준 URL 은 서버가 열어 확인한다. 열 수 없으면 '접근할 수 없는 URL 을 준 것'이라
#: FAIL 이고(평가자 web_fetch 원문으로 대신 확인된 경우는 열린 것), PASS 도 답변이 든 모든 출처가 주장을 뒷받침한다고 확인될 때만이다.
_CITATION_FAILURES = _CITATION_MISMATCH + ("inaccessible",)


def _citation_check(entry, state, verification=None):
    """평가자의 답변 출처 판정을 서버 수집 결과로 확정한다.

    답변이 든 URL 을 열 수 없으면(inaccessible) 출처 실패로 FAIL, 출처가 주장과 모순·무관·내용 없음이어도 FAIL 이다.
    평가자가 PASS 라도 답변이 든 출처 가운데 '주장을 뒷받침'으로 확인되지 않은 것이 있으면 PASS 로 확정하지 않는다(확인 필요)."""
    citations = state.get("answer_citations") if isinstance(state, dict) else None
    if not isinstance(citations, list) or not citations:
        return
    by_url = {source_url(c.get("url")): c for c in citations if isinstance(c, dict)}
    answer = state.get("answer", "")
    normalized, covered = [], set()
    for check in entry.get("citation_checks") if isinstance(entry.get("citation_checks"), list) else []:
        if not isinstance(check, dict):
            continue
        url = source_url(check.get("url"))
        citation = by_url.get(url)
        model = check.get("verdict")
        # retrieval is written by verify_answer_citations only: "server" or, when the server's fetch failed for an
        # access reason, "provider_document" (the provider's own web_fetch text of the same URL).
        provider = check.get("retrieval") == "provider_document" and check.get("server_status") not in (None, "ok")
        item = {"id": citation.get("id") if citation else None, "url": check.get("url"), "claim": check.get("claim"),
                "model_verdict": model, "quote": check.get("quote") or "", "reason": check.get("reason") or "",
                "server_status": check.get("server_status"), "quote_verified": check.get("quote_verified") is True}
        if provider:
            item["retrieval"] = "provider_document"
        where = "평가자가 web_fetch 로 받은 출처 원문" if provider else "출처 원문"
        if citation is None:
            verdict, note = "unverified", "답변이 제시한 출처 URL이 아닙니다."
        elif not _contained(check.get("claim"), answer):
            verdict, note = "unverified", "주장 구절이 답변 원문에 없습니다."
        elif item["server_status"] is None:
            verdict, note = "unchecked", "서버가 출처 원문을 받지 않아 판정에 쓰지 않았습니다."
        elif item["server_status"] != "ok" and not provider:
            verdict, note = "inaccessible", "서버가 출처 원문을 받지 못했습니다 (" + str(item["server_status"]) + ")."
        elif model in ("supported", "contradicted"):
            verdict, note = ((model, f"서버가 {where}에서 인용문을 확인했습니다.") if item["quote_verified"] else
                             ("unverified", f"평가자가 인용한 구절을 {where}에서 찾지 못했습니다."))
        elif model in ("unrelated", "not_found") and not item["reason"].strip():
            verdict, note = "unverified", "판단 이유가 없습니다."
        elif model in ("unrelated", "not_found") and check.get("claim_numbers_in_source") is True:
            verdict, note = "unverified", f"주장의 수치가 {where}에 있어 '해당 내용 없음' 판단을 확정하지 않았습니다."
        elif model in ("unrelated", "not_found"):
            verdict, note = model, ("평가자 판단 — 서버는 평가자가 web_fetch 로 받은 원문의 존재만 확인했습니다." if provider else
                                    "평가자 판단 — 서버는 출처 원문 수신만 확인했습니다.")
        else:
            verdict, note = "unverified", ("평가자가 원문 접근 불가로 판단해 판정에 쓰지 않았습니다." if provider else
                                           "평가자는 원문을 열지 못했지만 서버는 받았으므로 판정에 쓰지 않았습니다.")
        item.update(verdict=verdict, note=note)
        if citation is not None:
            covered.add(url)
        normalized.append(item)
    # 평가자가 기록하지 않은 출처도 서버 수집 상태는 판정에 쓴다 — 열리지 않았으면 접근 불가
    fetched = {source_url(s.get("url")): s for s in (verification or {}).get("citation_sources") or [] if isinstance(s, dict)}
    for url, citation in by_url.items():
        if url not in covered:
            source = fetched.get(url) or {}
            status = source.get("status")
            inaccessible = status not in (None, "ok") and not source.get("provider_document")
            normalized.append({"id": citation.get("id"), "url": citation.get("url"), "claim": None, "model_verdict": None,
                               "quote": "", "reason": "", "server_status": status, "quote_verified": False,
                               "verdict": "inaccessible" if inaccessible else "unchecked",
                               "note": ("서버가 출처 원문을 받지 못했습니다 (" + str(status) + ")." if inaccessible else
                                        "평가자가 이 출처의 지지 여부를 기록하지 않았습니다.")})
    for url in state.get("answer_citations_unchecked") or [] if isinstance(state, dict) else []:
        normalized.append({"id": None, "url": url, "claim": None, "model_verdict": None, "quote": "", "reason": "",
                           "server_status": None, "quote_verified": False, "verdict": "unchecked",
                           "note": "확인 한도 밖이라 열어 보지 못한 출처입니다."})
    entry["citation_checks"] = normalized
    if entry.get("choice") == "NOT_APPLICABLE":
        return
    mismatched = [item for item in normalized if item["verdict"] in _CITATION_FAILURES]
    if not mismatched:
        unconfirmed = [item for item in normalized if item["verdict"] != "supported"]
        if entry.get("choice") == "PASS" and unconfirmed:
            _unverified(entry, "답변이 든 출처에서 주장을 확인하지 못해 PASS 로 확정하지 않음: " + " · ".join(
                f"{item['url']} ({CITATION_LABELS[item['verdict']]})" for item in unconfirmed[:3]))
        return
    entry["citation_mismatch"] = True
    if any(item["verdict"] == "inaccessible" for item in mismatched):
        entry["citation_inaccessible"] = True
    note = "답변이 든 출처가 주장을 뒷받침하지 않거나 열리지 않음: " + " · ".join(
        f"{item['url']} ({CITATION_LABELS[item['verdict']]})" for item in mismatched[:3])
    if entry.get("choice") != "FAIL":
        _verdict(entry, "FAIL")
        entry["reason"] = note
    else:
        entry["reason"] = (entry.get("reason") or "").rstrip() + " · " + note


def finalize_answers(body, state, questions):
    """Called only by LLM transports. Native Jev and saved legacy results stay unchanged."""
    if isinstance(body, dict) and isinstance(body.get("items"), list) and not body.get("answers"):
        return copy.deepcopy(body)   # items 파이프라인 본문: 인용 검증은 semantic_quotes.finalize_items 가 한다
    result = copy.deepcopy(body)
    answers = result.get("answers") or {}
    verification = result.get("verification") or {}
    originals = {}
    for cid in ("J5", "J6"):
        entry = answers.get(cid)
        if cid in questions and isinstance(entry, dict):
            originals[cid] = {k: entry.get(k) if isinstance(entry.get(k), str) else None for k in ("choice", "reason")}
            # This audit field is server-owned, never accepted from model output.
            entry.pop("model_original_assessment", None)
    if "J5" in questions and isinstance(answers.get("J5"), dict):
        _fact_check(answers["J5"], state, verification)
        _citation_check(answers["J5"], state, verification)
    if "J6" in questions and isinstance(answers.get("J6"), dict):
        _context_check(answers["J6"], state)
    for cid, original in originals.items():
        if any(answers[cid].get(k) != original[k] for k in ("choice", "reason")):
            answers[cid]["model_original_assessment"] = original
    return result
