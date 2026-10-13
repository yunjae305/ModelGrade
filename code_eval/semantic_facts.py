"""items 파이프라인의 J5 서버 검증 — 주장별 fact 블록 검증·제외 규칙, 반박 출처 원문 대조, 값별 대조, 출처 URL 존재 확인, 집계·호환 투영.

설계 'J5 사실 검증 절차' 를 코드로 옮긴다. 모델 호출·검색은 하지 않고 네트워크는 주입 ``fetcher`` 로만 쓴다.
- J5-1.a#n(주장별): claim 은 답변 원문에 정규화 대조로 있어야 하고, 의견·J3-1.a 모순 쌍·J3-3.a 인과 문장·이전 턴 AI 답변 값·질문 원문
  값은 항목에서 뺀다(dropped, 재확인 아님). 답변당 최대 ``MAX_CLAIMS``. verdict 는 TRUE/FALSE 뿐('판단 불가' 없음).
  FALSE 는 올바른 사실(correct)과 제공자가 관측한 URL 의 반박 출처가 있어야 하고, 서버가 그 본문을 다시 받아 인용을 대조한다.
  TRUE 도 관측된 출처가 있어야 한다(기억만으로 판정 금지). 답변에 출처 링크가 있고 없고는 판정에 쓰지 않는다.
- J5-2.a#n(값별): question_quote 는 질문(또는 reference) 원문에, answer_quote 는 답변 원문에 있어야 한다.
- J5-3.a: 답변(코드 밖)의 URL 을 서버가 열어 확인한다. 열 수 없으면(시간 초과 등 포함) '접근 불가 URL 을 준 것'이라 NO 이고,
  확인 한도(MAX_LINKS) 밖 URL 이 남아 있으면 예로 확정하지 않는다 (evidence-v3.14, 사용자 결정 2026-10-13).
- 확인되지 않은 문제는 item["server"]["problems"] 로 남겨 ``semantic_quotes.verify_items`` 가 재확인 대상에 올린다.
  3회 뒤 남은 문제는 FACT_UNVERIFIABLE(J5-1) / ITEM_UNVERIFIABLE 로 '확인 불가 FAIL', 거짓 확인은 FACT_FALSE_CONFIRMED 로 구분한다.
"""

from __future__ import annotations

import copy
import json
import re
import time
from concurrent.futures import CancelledError
from threading import Event
from typing import Any, Dict, List, Optional, Tuple

from . import semantic_sources
from .jev_applicability import _data_text
from .semantic_evidence import source_url
from .semantic_items import (FACT_FALSE_CONFIRMED, FACT_UNVERIFIABLE, ITEM_UNVERIFIABLE, REASON_CODE_PRIORITY, SOURCE_NOT_FOUND,
                             SPECS_BY_ID)
from .semantic_quotes import MAX_RECHECK_ROUNDS, UNVERIFIABLE_PREFIX, _snapshot, _split_id, locate_quote

__all__ = ["FACT_POLICY_VERSION", "MAX_CLAIMS", "MAX_LINKS", "PROBLEM_MESSAGES", "excluded_claims", "validate_fact_items",
           "verify_refutations", "check_input_values", "check_answer_links", "server_check", "finalize", "aggregate_j5",
           "legacy_projection"]

FACT_POLICY_VERSION = "j5-items-facts-v1"
MAX_CLAIMS = 8
MAX_LINKS = 8
#: 서버가 J5 항목에 남기는 문제 코드 → 재확인 요청 문구
PROBLEM_MESSAGES = {
    "fact_required": "J5-1.a 판정에는 fact 블록(claim·verdict·correct·sources)이 반드시 있어야 합니다.",
    "claim_not_in_answer": "claim 이 평가 대상 답변 원문에 없습니다. 답변 원문을 글자 그대로 복사하세요.",
    "verdict_invalid": "fact.verdict 는 TRUE 또는 FALSE 여야 합니다 ('판단 불가' 는 없습니다).",
    "refutation_missing": "거짓 판정에는 올바른 사실(correct)과 검색으로 확인한 반박 출처 URL·원문 인용이 있어야 합니다.",
    "refutation_unverified": "거짓 판정의 반박 출처 원문에서 인용을 확인하지 못했습니다. 같은 주장을 다시 검색·판정하세요.",
    "support_missing": "참 판정에 확인한 출처가 없습니다. 기억만으로 판정하지 말고 검색한 출처를 적으세요.",
    "question_quote_required": "값별 판정에는 질문 원문 인용(question_quote)이 반드시 있어야 합니다.",
    "question_quote_not_found": "question_quote 가 질문(또는 참조 자료) 원문에 없습니다.",
    "answer_quote_required": "이 판정에는 답변 원문 인용(answer_quote)이 반드시 있어야 합니다.",
    "answer_quote_not_found": "answer_quote 가 답변 원문에 없습니다.",
}
#: 출처 URL 존재 확인에서 '없음' 으로 보는 상태 — 그 밖(timeout·fetch_error·unsupported_content_type 등)은 판정에 쓰지 않는다
NOT_FOUND_STATUSES = frozenset({"http_error", "dns_error", "blocked_url"})
_LINK = re.compile(r"https?://[^\s<>\"'()\[\]{}「」『』“”]+")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _problem(code: str, message: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    return {"code": code, "message": message or PROBLEM_MESSAGES.get(code, code), **extra}


def _observed(verification: Dict[str, Any]) -> Tuple[set, bool]:
    urls = verification.get("source_urls") if isinstance(verification, dict) else None
    observed = {source_url(u) for u in urls if isinstance(u, str)} if isinstance(urls, list) else set()
    observed.discard(None)
    return observed, bool(isinstance(verification, dict) and verification.get("search_performed") is True)


def _fact_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [it for it in items if isinstance(it, dict) and _split_id(it.get("id"))[0] == "J5-1.a"]


# --- J5-1.a 주장별 -----------------------------------------------------------------------------------

def excluded_claims(items: List[Dict[str, Any]], state: Dict[str, Any]) -> List[Tuple[str, str]]:
    """J5-1 에서 보지 않는 문장·값 — [(원문, 이유)]: J3-1.a 모순 쌍, J3-3.a 인과 문장, 이전 턴 AI 답변, 질문 원문."""
    out: List[Tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        base, _ = _split_id(item.get("id"))
        quote = _text(item.get("answer_quote"))
        if base == "J3-1.a" and quote:
            out.append((quote, "J3-1.a 모순 쌍 — J3-1 에서만 판정"))
        elif base == "J3-3.a" and quote:
            out.append((quote, "J3-3.a 인과 연결 문장 — J3-3 에서만 판정"))
    for prior in state.get("history") or [] if isinstance(state, dict) else []:
        if isinstance(prior, dict) and _text(prior.get("answer")):
            out.append((prior["answer"], f"이전 턴({prior.get('turn')}) AI 답변에서 비롯한 값 — 이전 턴 결과로 기록"))
    if isinstance(state, dict) and _text(state.get("question")):
        out.append((state["question"], "질문이 준 값 — J5-2 에서 대조"))
    return out


def _exclusion_reason(claim: str, exclusions: List[Tuple[str, str]]) -> Optional[str]:
    for text, reason in exclusions:
        if reason.startswith("J3"):
            if locate_quote(claim, text) is not None or locate_quote(text, claim) is not None:
                return reason
        elif locate_quote(claim, text) is not None:
            return reason
    return None


def validate_fact_items(items: List[Dict[str, Any]], state: Dict[str, Any],
                        verification: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """J5-1.a#n 항목 검증 → (정리된 items, {id: [problem…]}, dropped[{id, claim, reason}]).

    제외 집합에 든 주장과 9번째 이후 주장은 항목에서 뺀다(재확인 아님). decision 과 fact.verdict 가 어긋나면 verdict 로 맞추고
    ``decision_adjusted`` 에 남긴다. 다른 기준의 항목은 그대로 둔다.
    """
    observed, search_ok = _observed(verification)
    exclusions = excluded_claims(items, state)
    answer = state.get("answer") if isinstance(state, dict) else ""
    kept: List[Dict[str, Any]] = []
    problems: Dict[str, List[Dict[str, Any]]] = {}
    dropped: List[Dict[str, Any]] = []
    claims = 0
    for item in items:
        if not isinstance(item, dict) or _split_id(item.get("id"))[0] != "J5-1.a":
            kept.append(item)
            continue
        item_id = item.get("id")
        decision = item.get("decision")
        fact = item.get("fact")
        if decision == "NOT_APPLICABLE":
            kept.append(item)   # 검증할 외부 사실 주장이 없음
            continue
        if not isinstance(fact, dict) or not _text(fact.get("claim")):
            kept.append(item)
            problems[item_id] = [_problem("fact_required")]
            continue
        claim = _text(fact["claim"])
        excluded = _exclusion_reason(claim, exclusions)
        if excluded:
            dropped.append({"id": item_id, "claim": claim, "reason": excluded})
            continue
        claims += 1
        if claims > MAX_CLAIMS:
            dropped.append({"id": item_id, "claim": claim, "reason": f"답변당 주장 최대 {MAX_CLAIMS}개 — 9번째 이후 제외"})
            continue
        kept.append(item)
        issues: List[Dict[str, Any]] = []
        if locate_quote(claim, answer or "") is None:
            issues.append(_problem("claim_not_in_answer"))
        verdict = fact.get("verdict")
        if verdict not in ("TRUE", "FALSE"):
            issues.append(_problem("verdict_invalid"))
        else:
            expected = "YES" if verdict == "TRUE" else "NO"
            if decision != expected:
                item["decision_adjusted"] = {"from": decision, "to": expected, "by": "fact.verdict"}
                item["decision"] = expected
            sources = [s for s in (fact.get("sources") if isinstance(fact.get("sources"), list) else []) if isinstance(s, dict)]
            supported = [s for s in sources if search_ok and source_url(s.get("url")) in observed]
            if verdict == "FALSE" and (not _text(fact.get("correct")) or not supported):
                issues.append(_problem("refutation_missing"))
            elif verdict == "TRUE" and not supported:
                issues.append(_problem("support_missing"))
        if issues:
            problems[item_id] = issues
    if dropped and not any(isinstance(it, dict) and _split_id(it.get("id"))[0] == "J5-1.a" for it in kept):
        # 나열된 주장이 모두 제외 규칙에 걸려 빠지면 '응답에 J5-1.a 가 없음' 이 되어 재확인 3회 뒤 확인 불가 FAIL 이 된다.
        # 검증할 외부 사실 주장이 없는 경우이므로 서버가 해당 없음 자리표시 항목을 둔다 (제외 내역은 j5_dropped 에 남는다).
        kept.append({"id": "J5-1.a", "decision": "NOT_APPLICABLE", "question_quote": "", "answer_quote": "", "turn": None, "fact": None,
                     "reason": "서버: 나열된 주장이 모두 제외 규칙(J3 모순 쌍·인과 문장·이전 턴 AI 답변·질문이 준 값)에 해당 — 검증할 외부 사실 주장 없음",
                     "server_placeholder": True})
    return kept, problems, dropped


def verify_refutations(items: List[Dict[str, Any]], verification: Dict[str, Any], *, fetcher: Any = None,
                       cancel_event: Optional[Event] = None) -> Dict[str, List[Dict[str, Any]]]:
    """FALSE 주장의 반박 출처 본문을 다시 받아 인용을 대조한다 (semantic_sources._verify_sources 재사용 — legacy 와 같은 규칙).

    sources[] 에 quote_verified/verification_status/… 를 제자리에 기록하고, 하나도 확인되지 않은 주장은 refutation_unverified.
    """
    targets: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    flat: List[Dict[str, Any]] = []
    for item in _fact_items(items):
        fact = item.get("fact")
        if not isinstance(fact, dict) or fact.get("verdict") != "FALSE":
            continue
        sources = [s for s in (fact.get("sources") if isinstance(fact.get("sources"), list) else []) if isinstance(s, dict)]
        for source in sources:
            source.setdefault("origin", "web")
        flat.extend(sources)
        targets.append((item, sources))
    if not flat:
        return {}
    semantic_sources._verify_sources(flat, verification, fetcher=fetcher, cancel_event=cancel_event)
    problems: Dict[str, List[Dict[str, Any]]] = {}
    for item, sources in targets:
        if sources and not any(s.get("quote_verified") is True for s in sources):
            problems[item.get("id")] = [_problem("refutation_unverified", statuses=[s.get("verification_status") for s in sources])]
    return problems


# --- J5-2.a 값별 · J5-3.a 출처 존재 ------------------------------------------------------------------

def check_input_values(items: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """J5-2.a#n: question_quote 는 질문(reference 가 있으면 reference 도) 원문에, answer_quote 는 답변 원문에 있어야 한다."""
    question = state.get("question") or "" if isinstance(state, dict) else ""
    answer = state.get("answer") or "" if isinstance(state, dict) else ""
    reference = state.get("reference") if isinstance(state, dict) else None
    reference_text = "" if reference in (None, "", {}, []) else (reference if isinstance(reference, str) else json.dumps(reference, ensure_ascii=False))
    problems: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict) or _split_id(item.get("id"))[0] != "J5-2.a" or item.get("decision") not in ("YES", "NO"):
            continue
        issues: List[Dict[str, Any]] = []
        question_quote = _text(item.get("question_quote"))
        if not question_quote:
            issues.append(_problem("question_quote_required"))
        elif locate_quote(question_quote, question) is None and (not reference_text or locate_quote(question_quote, reference_text) is None):
            issues.append(_problem("question_quote_not_found"))
        answer_quote = _text(item.get("answer_quote"))
        if not answer_quote:
            issues.append(_problem("answer_quote_required"))
        elif locate_quote(answer_quote, answer) is None:
            issues.append(_problem("answer_quote_not_found"))
        if issues:
            problems[item.get("id")] = issues
    return problems


def _link_status(status: Any) -> str:
    if status == "ok":
        return "exists"
    return "not_found" if status in NOT_FOUND_STATUSES else "inaccessible"   # 열리지 않으면 접근 불가 — 일시 장애도 포함


def check_answer_links(answer: str, *, fetcher: Any = None, cancel_event: Optional[Event] = None) -> Dict[str, str]:
    """J5-3.a: 답변(코드 밖)의 http(s) URL 최대 MAX_LINKS 개를 열어 {url: 'exists' | 'not_found' | 'inaccessible' | 'unchecked'}.

    한도 밖 URL 은 열지 않고 'unchecked' 로 남긴다 — 확인하지 못한 출처가 있으면 예로 확정하지 않는다."""
    text = _data_text(answer) if isinstance(answer, str) and answer else ""
    urls: List[str] = []
    for match in _LINK.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}>'\"")
        if url and url not in urls:
            urls.append(url)
    links: Dict[str, str] = {}
    deadline = time.monotonic() + semantic_sources.TOTAL_TIMEOUT
    for url in urls[:MAX_LINKS]:
        semantic_sources._check_cancelled(cancel_event)
        normalized = semantic_sources._url(url)
        if not normalized:
            links[url] = "not_found"   # blocked_url — 공개 웹 주소 형식이 아님
            continue
        try:
            item = fetcher(normalized) if fetcher is not None else semantic_sources._SOURCE_TEXT_CACHE.fetch(normalized, deadline=deadline)
            status = item.get("status") if isinstance(item, dict) else "fetch_error"
        except CancelledError:
            raise
        except Exception:
            status = "fetch_error"
        semantic_sources._check_cancelled(cancel_event)
        links[url] = _link_status(status)
    for url in urls[MAX_LINKS:]:
        links[url] = "unchecked"
    return links


# --- transport 가 제출 직후 호출 -----------------------------------------------------------------------

def server_check(body: Dict[str, Any], state: Dict[str, Any], questions: Dict[str, Any], verification: Optional[Dict[str, Any]], *,
                 fetcher: Any = None, cancel_event: Optional[Event] = None) -> Dict[str, Any]:
    """items 본문의 J5 항목에 server={"problems": [...], "links": {...}} 를 덧붙인 body 사본 (fact.sources[].quote_verified 기록).

    J5 항목을 보내지 않았으면 본문을 그대로(사본) 돌려준다. ``verification`` 은 transport 가 모은 {search_performed, source_urls, …}
    이며 결과 본문의 verification 이 된다 (source_checks·fact_policy·j5_dropped 를 기록).
    """
    result = copy.deepcopy(body) if isinstance(body, dict) else {}
    state = state if isinstance(state, dict) else {}
    if not isinstance(verification, dict):
        verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    result["verification"] = verification
    j5_sent = [key for key in questions if isinstance(key, str) and key.startswith("J5")]
    items = [it for it in (result.get("items") if isinstance(result.get("items"), list) else []) if isinstance(it, dict)]
    if not j5_sent:
        result["items"] = items
        return result
    verification["fact_policy"] = FACT_POLICY_VERSION
    problems: Dict[str, List[Dict[str, Any]]] = {}

    def merge(found: Dict[str, List[Dict[str, Any]]]) -> None:
        for item_id, issues in found.items():
            problems.setdefault(item_id, []).extend(issues)

    if "J5-1.a" in questions:
        items, found, dropped = validate_fact_items(items, state, verification)
        merge(found)
        unverified = verify_refutations(items, verification, fetcher=fetcher, cancel_event=cancel_event)
        # 반박 출처 자체가 없거나 관측되지 않은 주장(refutation_missing)에는 같은 문제를 겹쳐 적지 않는다
        merge({item_id: issues for item_id, issues in unverified.items()
               if not any(p.get("code") == "refutation_missing" for p in problems.get(item_id, []))})
        verification["j5_dropped"] = dropped
    if "J5-2.a" in questions:
        merge(check_input_values(items, state))
    links: Dict[str, str] = {}
    if "J5-3.a" in questions and any(_split_id(it.get("id"))[0] == "J5-3.a" for it in items):
        links = check_answer_links(state.get("answer") or "", fetcher=fetcher, cancel_event=cancel_event)
    for item in items:
        base, _ = _split_id(item.get("id"))
        if base.startswith("J5"):
            item["server"] = {"problems": list(problems.get(item.get("id"), [])), "links": dict(links) if base == "J5-3.a" else {}}
    result["items"] = items
    return result


# --- 확정·집계 ----------------------------------------------------------------------------------------

def _mark(item: Dict[str, Any], code: str, reason: str) -> None:
    if "model_original" not in item:
        item["model_original"] = _snapshot(item)
    item.update(decision="NO", reason_code=code, reason=reason)


def finalize(items: List[Dict[str, Any]], attempts: int) -> None:
    """재확인 루프가 끝난 J5 항목의 서버 확정 (제자리, 반복 호출 안전).

    - J5-1.a: 거짓 판정의 반박 인용이 확인됐으면 FACT_FALSE_CONFIRMED. attempts(재확인 회차)가 한도에 이르렀는데 server.problems 가
      남았으면 decision NO · unverifiable · FACT_UNVERIFIABLE (fact.verdict 는 모델 값을 보존).
    - J5-3.a: server.links 에 'not_found'·'inaccessible' 이 있으면 모델 판정과 무관하게 decision NO · SOURCE_NOT_FOUND,
      예인데 'unchecked'(확인 한도 밖)가 남았으면 NO · unverifiable · ITEM_UNVERIFIABLE (확인 불가).
    - 그 밖의 J5 항목: 한도 뒤 남은 problems 는 ITEM_UNVERIFIABLE 확인 불가.
    """
    for item in items:
        if not isinstance(item, dict):
            continue
        base, _ = _split_id(item.get("id"))
        if not base.startswith("J5"):
            continue
        server = item.get("server") if isinstance(item.get("server"), dict) else {}
        problems = [p for p in (server.get("problems") or []) if p]
        if base == "J5-3.a":
            links = server.get("links") or {}
            dead = [url for url, status in links.items() if status in ("not_found", "inaccessible")]
            if dead and item.get("reason_code") != SOURCE_NOT_FOUND:
                original = _text(item.get("reason"))
                _mark(item, SOURCE_NOT_FOUND, "답변이 든 출처 URL 을 열 수 없음: " + ", ".join(dead) + (f" ({original})" if original else ""))
                continue
            unchecked = [url for url, status in links.items() if status == "unchecked"]
            if unchecked and item.get("decision") == "YES" and not item.get("unverifiable"):
                _mark(item, ITEM_UNVERIFIABLE, "답변이 든 출처를 모두 열어 확인하지 못함(확인 한도 밖): " + ", ".join(unchecked[:3]))
                item["unverifiable"] = True
                continue
        if item.get("unverifiable"):
            if base == "J5-1.a" and item.get("reason_code") != FACT_UNVERIFIABLE:
                item["reason_code"] = FACT_UNVERIFIABLE
            continue
        if problems and attempts >= MAX_RECHECK_ROUNDS:
            messages = list(dict.fromkeys(_text(p.get("message")) if isinstance(p, dict) else str(p) for p in problems))
            _mark(item, FACT_UNVERIFIABLE if base == "J5-1.a" else ITEM_UNVERIFIABLE,
                  f"{UNVERIFIABLE_PREFIX}재확인 {attempts}회 뒤에도 확인하지 못함 — " + " · ".join(m for m in messages if m))
            item["unverifiable"] = True
            continue
        if base == "J5-1.a" and item.get("decision") == "NO":
            fact = item.get("fact")
            sources = fact.get("sources") if isinstance(fact, dict) and isinstance(fact.get("sources"), list) else []
            if fact and fact.get("verdict") == "FALSE" and any(isinstance(s, dict) and s.get("quote_verified") is True for s in sources):
                item["reason_code"] = FACT_FALSE_CONFIRMED


def aggregate_j5(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """세부 기준별 집계 — {sid: {"decision": PASS|FAIL|NOT_APPLICABLE, "items": [...]}}. 적용 항목(N/A 제외)이 모두 YES 면 PASS."""
    grouped: Dict[str, List[Dict[str, Any]]] = {"J5-1": [], "J5-2": [], "J5-3": []}
    for item in items:
        if not isinstance(item, dict):
            continue
        base, _ = _split_id(item.get("id"))
        spec = SPECS_BY_ID.get(base)
        if spec is not None and spec.sub in grouped:
            grouped[spec.sub].append(item)
    out: Dict[str, Dict[str, Any]] = {}
    for sid, its in grouped.items():
        applied = [it for it in its if it.get("decision") != "NOT_APPLICABLE"]
        if not applied:
            decision = "NOT_APPLICABLE"
        elif any(it.get("decision") == "NO" for it in applied):
            decision = "FAIL"
        else:
            decision = "PASS"
        out[sid] = {"decision": decision, "items": its}
    return out


def _sub_decision(value: Any) -> Optional[str]:
    return value.get("decision") if isinstance(value, dict) else value if isinstance(value, str) else None


def legacy_projection(j5_items: List[Dict[str, Any]], subs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """decisions["J5"] 호환 투영 — {decision, reason, reason_code?, fact_check{basis, claims[...]}} (기존 화면이 읽는 키 유지).

    ``subs`` 는 {sid: {"decision": PASS|FAIL|NOT_APPLICABLE}} 또는 {sid: decision}; 없으면 ``aggregate_j5`` 로 계산한다.
    claims 는 J5-1.a 항목에서 만들고 verdict 는 PASS(참)/FAIL(거짓·확인 불가), reason_code 로 거짓 확인과 확인 불가를 구분한다.
    """
    items = [it for it in j5_items if isinstance(it, dict)]
    sub_decisions = {sid: _sub_decision(value) for sid, value in (subs or aggregate_j5(items)).items()}
    claims: List[Dict[str, Any]] = []
    for item in _fact_items(items):
        fact = item.get("fact")
        if not isinstance(fact, dict) or item.get("decision") == "NOT_APPLICABLE":
            continue
        sources = []
        for source in fact.get("sources") if isinstance(fact.get("sources"), list) else []:
            if isinstance(source, dict):
                sources.append({"url": _text(source.get("url")), "title": _text(source.get("title")), "quote": _text(source.get("quote")),
                                "origin": "web", "quote_verified": source.get("quote_verified") is True,
                                "verification_status": source.get("verification_status")})
        reason = _text(item.get("reason")) or (f"올바른 사실: {_text(fact.get('correct'))}" if _text(fact.get("correct")) else "")
        claim = {"claim": _text(fact.get("claim")), "verdict": "PASS" if item.get("decision") == "YES" else "FAIL", "reason": reason,
                 "scope": "external_fact", "scope_quote": "", "sources": sources}
        if item.get("reason_code"):
            claim["reason_code"] = item["reason_code"]
        if item.get("unverifiable"):
            claim["unverifiable"] = True
        claims.append(claim)
    fact_check = {"basis": "web" if claims else "none", "claims": claims}
    decisions = [d for d in sub_decisions.values() if d]
    if not decisions or all(d == "NOT_APPLICABLE" for d in decisions):
        return {"decision": None, "status": "NOT_APPLICABLE", "reason": "적용 항목 없음", "fact_check": fact_check}
    failed = [it for it in items if it.get("decision") == "NO"]
    decision = "FAIL" if any(d == "FAIL" for d in decisions) else "PASS"
    reason = " · ".join(f"{it.get('id')}: {_text(it.get('reason')) or '아니오'}" for it in failed[:3]) or "적용 항목 모두 예"
    projected: Dict[str, Any] = {"decision": decision, "reason": reason, "fact_check": fact_check}
    codes = {it.get("reason_code") for it in failed}
    reason_code = next((code for code in REASON_CODE_PRIORITY if code in codes), None)
    if reason_code:
        projected["reason_code"] = reason_code
    return projected
