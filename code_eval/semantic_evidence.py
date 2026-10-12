"""Validate evidence returned by generative judges without modifying source answers.

Provider tool events establish retrieval provenance, not the truth of a claim.
The model still judges source quality/relevance; missing evidence stays unverified.
"""
from __future__ import annotations

import copy
import json
import re
from urllib.parse import urlsplit, urlunsplit

FACT_CHECK_POLICY = "evidence-v3.5-task-input-edits"
CONTEXT_CHECK_POLICY = "history-quotes-v2-current-relevance"
SEARCH_POLICY = "J5_without_reference_grouped_evidence_v3"


def has_reference(state):
    value = state.get("reference") if isinstance(state, dict) else None
    return value is not None and value not in ("", {}, []) and (not isinstance(value, str) or bool(value.strip()))


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

    Supplied values remain valid evidence when the requested work is writing,
    calculation or a simulated plan, not only when it contains 'summarize'. The
    judge still assesses entailment; this gate corroborates the scope and the
    caller separately checks the exact user quote and source turn.
    """
    quote = claim.get("scope_quote")
    if claim.get("scope") != "input_consistency" or not _contained(quote, state.get("question", "")):
        return False
    material = r"가상|허구|제공|주어진|다음|아래|위의|위\s*자료|앞서|원문|공지|문서|자료|메모|following|provided|given|fictional|hypothetical|above|document|passage"
    transform = r"요약|추출|변환|재작성|재구성|정리|표로|표를\s*(?:만들|작성)|summari[sz]|extract|convert|rewrite|reformat"
    # Keep the established document/structured-transformation path.
    structured_transform = (r"(?:이|주어진|제공된)\s*(?:정보|자료|값|기록)(?:를|을|만(?:을)?)?"
                            r".{0,120}?JSON\s*(?:배열|객체)(?:로|으로)\s*(?:작성|출력)")
    grounded_intro = (r"(?:이|주어진|제공된)\s*(?:정보|자료)만(?:을)?\s*"
                      r"(?:사용|이용)(?:해서|해)\s*(?:온라인\s*)?소개\s*문구를\s*(?:작성|만들)")
    verification = (r"사실인지|참인지|정확한지|맞는지|진위|정확성|사실\s*(?:확인|검증)|팩트\s*체크|검증해|검색해|조사해|"
                    r"fact[ -]?check|verify|whether.{0,35}(?:true|correct)|"
                    r"(?:check|confirm).{0,50}(?:truth|accuracy|correct|authentic)")
    # A document's introduction and its transformation request may be separate
    # sentences. The exact request quote still has to identify a transformation;
    # material context and any truth-verification request are checked globally.
    question = state.get("question", "")
    recognized_transform = any(re.search(pattern, quote, re.I)
                               for pattern in (transform, structured_transform, grounded_intro))
    if re.search(verification, question, re.I):
        return False
    external_request = (r"(?:국내|전국|전\s*세계|실제\s*(?:운영|시장|판매)).{0,60}"
                        r"(?:정원|가격|요금|통계|현황)|(?:nationwide|worldwide|real.world).{0,60}(?:price|data|statistic)")
    if re.search(external_request, quote, re.I):
        return False
    # A user may correct supplied inventory values or rename an existing item
    # without asking for a summary. These are updates to task inputs; accepting
    # their exact source quote does not independently verify real-world truth.
    input_edit = (r"(?:내역|정보|내용|값|수량|배부|기록|입력).{0,40}정정(?:할|해|합|했|하)|"
                  r"(?:이름|명칭|품목명).{0,80}(?:바꿔|바꿀|변경|바꾸)|"
                  r"(?:correct|amend).{0,40}(?:input|record|quantity)|"
                  r"rename.{0,60}(?:item|name|label)")
    if re.search(input_edit, quote, re.I):
        return True
    if recognized_transform and re.search(material, question, re.I):
        return True
    # These operations can repeat supplied inputs verbatim as part of their
    # result. Accepting that quote does not verify a derived total or an added
    # real-world assertion (those have their own criteria/scope).
    calculation = r"계산|산출|추론|calculate|compute|derive"
    writing = (r"(?:이메일|메일|메시지|문자|안내\s*문구|안내문|소개\s*문구|사과문|공지문|편지)"
               r".{0,50}(?:작성|써\s*줘|써\s*주|만들)|"
               r"(?:write|draft|compose).{0,50}(?:email|message|notice|letter)")
    if re.search(calculation, quote, re.I) or re.search(writing, quote, re.I):
        return True
    # A simulated multi-turn task carries its user-defined world forward. Do
    # not require every follow-up to repeat the initial 'fictional' introduction
    # or a particular retrospective keyword before accepting supplied values.
    scenario = r"가상|허구|가정(?:할|해|한|으로)|fictional|hypothetical|assume"
    scenario_task = (r"작성|출력|써\s*줘|만들|정리|계산|추론|추천|골라|고르|선택|구분|표시|"
                     r"수정|고치|갱신|교체|변경|유지|설명|알려|보여|확인|적용|"
                     r"write|output|select|choose|update|keep|show|explain|check")
    context = [question] + [h.get("question", "") for h in state.get("history", [])
                            if isinstance(h, dict) and isinstance(h.get("question"), str)]
    continuation = (r"현재|이번|기존|최신|조건|계획|다시|지금|앞서|이전|마지막|나머지|"
                    r"선택|바꿀|바꿔|정정|추가|포함|남은|남는|그대로|상한|"
                    r"current|previous|earlier|same|remaining|selected")
    if (re.search(scenario_task, quote, re.I)
            and (re.search(scenario, question, re.I) or re.search(continuation, question, re.I))
            and any(re.search(scenario, text, re.I) for text in context)):
        return True
    # A later request may only name the current plan while its user-provided
    # fictional/material introduction is in an earlier turn. Limit that fallback
    # to a retrospective request with evidence from a specific user turn; the
    # caller still checks the exact quote and rejects missing/duplicate turns.
    retrospective = (r"앞서|지금까지|마지막(?:으로)?|"
                     r"(?:이전|현재).{0,40}(?:계획|조건|설정|선택|인원|예산|할인|대화)|"
                     r"(?:계획|조건|설정).{0,20}그대로|"
                     r"previous|earlier|so far|last selected|current (?:plan|conditions|settings)")
    return bool(recognized_transform and isinstance(source, dict) and source.get("origin") == "history_user"
                and re.search(retrospective, question, re.I)
                and any(isinstance(prior, dict) and isinstance(prior.get("question"), str)
                        and re.search(material, prior["question"], re.I)
                        for prior in state.get("history", [])))


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
            elif origin == "web" and basis in ("web", "mixed") and search_ok and source_url(url) in observed:
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
                source_problems.append("완료된 검색 기록 또는 허용된 제공 자료에서 이 출처를 확인하지 못했습니다.")
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
    if "J6" in questions and isinstance(answers.get("J6"), dict):
        _context_check(answers["J6"], state)
    for cid, original in originals.items():
        if any(answers[cid].get(k) != original[k] for k in ("choice", "reason")):
            answers[cid]["model_original_assessment"] = original
    return result
