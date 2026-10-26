"""items 파이프라인의 서버 인용 검증·재확인(최대 3회)·모델 간 요소 대조 — 순수 함수, 네트워크·모델 호출 없음.

설계 '서버 검증'·'재확인' 절을 코드로 옮긴다.
- 대조는 공백·제어문자·문장부호·기호(Markdown 강조·목록 표식 포함)를 지우고 NFKC·casefold 한 정규화 본문으로 한다.
  숫자 사이의 문장부호(3.5 / 1,200 / 10:30)만 남겨 '3.5시간' 이 '35시간' 과 일치하지 않게 한다. legacy 의 '입력 범위'
  정규식 게이트(semantic_evidence._input_scope)는 쓰지 않는다.
- question_quote 는 현재 질문(과 reference) 또는 item["turn"] 이 가리키는 history 턴 원문(질문+답변)에서 찾는다.
  펼친 요소(id 에 '#n')는 question_quote 가 필수다. YES 는 answer_quote 가 필수, NO 는 누락형(questions[id]["missing_type"])
  항목만 빈 answer_quote 를 허용, NOT_APPLICABLE 은 인용이 없어도 통과한다.
- 실패한 항목만 다시 묻는다(최대 ``MAX_RECHECK_ROUNDS``). 재확인 호출이 TIMEOUT·API_ERROR 여도 그 회차는 소진된 것으로 치고
  남은 회차가 있으면 계속한다. 3회 뒤에도 남은 문제는 decision NO · unverifiable True · reason_code 로 '확인 불가' 를 남겨
  '결함 확인 FAIL' 과 구분한다 (판정에 '모른다' 값은 없다).
- 서버 소유 필드(quote_check·origin·rechecks·unverifiable·reason_code·model_original)는 모델 출력에서 먼저 지운다.
"""

from __future__ import annotations

import copy
import json
import time
import unicodedata
from concurrent.futures import CancelledError
from threading import Event
from typing import Any, Callable, Dict, List, Optional, Tuple

from .jev_client import JevCall, _sum_usage
from .llm_client import redact
from .semantic_items import DECISIONS, FACT_UNVERIFIABLE, ITEM_UNVERIFIABLE

__all__ = ["ITEM_QUOTE_POLICY", "RECHECK_POLICY", "MAX_RECHECK_ROUNDS", "RECHECK_NOTICE", "FAILURE_MESSAGES",
           "normalize_text", "locate_quote", "verify_items", "recheck_questions", "merge_recheck", "mark_unverifiable",
           "finalize_items",]

#: v2 — 펼친 요소 단위 해당 없음(element_na, J5-2.a 의 '쓰지 않은 값')을 허용한다. 항목 전체의 해당 없음은 그대로 막는다.
ITEM_QUOTE_POLICY = "items-normalized-quotes-v2"
RECHECK_POLICY = "items-recheck-3-then-fail-v1"
MAX_RECHECK_ROUNDS = 3
RECHECK_NOTICE = "이전 판정의 인용이 원문에 없습니다. 원문을 글자 그대로 복사해 같은 항목을 다시 판정하세요."
UNVERIFIABLE_PREFIX = "확인 불가: "
#: 모델 출력에 같은 이름 키가 있어도 서버가 다시 쓴다
SERVER_FIELDS = ("quote_check", "origin", "rechecks", "unverifiable", "reason_code", "model_original")
#: 검증 실패 코드와 재확인 요청에 싣는 문구. 아래 셋은 semantic_facts.server_check 가 item["server"]["problems"] 에 남긴 것을 읽는다.
FAILURE_MESSAGES = {
    "decision_invalid": "decision 이 YES / NO / NOT_APPLICABLE 중 하나가 아닙니다.",
    "unrequested_item": "보내지 않은 항목에 답했습니다.",
    "item_missing": "보낸 항목에 대한 판정이 없습니다.",
    "question_quote_required": "펼친 요소에는 질문 원문 인용(question_quote)이 반드시 있어야 합니다.",
    "question_quote_not_found": "question_quote 가 질문(또는 지정한 이전 턴) 원문에 없습니다.",
    "answer_quote_required": "이 판정에는 답변 원문 인용(answer_quote)이 반드시 있어야 합니다.",
    "answer_quote_not_found": "answer_quote 가 답변 원문에 없습니다.",
    "source_quote_not_found": "반박 출처 본문에서 인용문을 확인하지 못했습니다.",
    "fact_required": "J5-1.a 판정에는 fact 블록(claim·verdict·correct·sources)이 반드시 있어야 합니다.",
    "not_applicable_disallowed": "코드 규칙이 이 질문에 적용한 항목입니다. 적용 여부를 바꾸지 말고 YES 또는 NO 로 판정하세요(펼치기 항목은 요소를 나열).",
    "refutation_unverified": "거짓 판정의 반박 출처 원문에서 인용을 확인하지 못했습니다. 같은 주장을 다시 검색·판정하세요.",
    "support_missing": "참 판정에 확인한 출처가 없습니다. 기억만으로 판정하지 말고 검색한 출처를 적으세요.",
}


# --- 정규화 인용 대조 ------------------------------------------------------------------------------

def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalize_text(text: Any) -> Tuple[str, List[int]]:
    """(정규화 문자열, 정규화 i → 원문 offset). 공백·제어문자(Z/C)·문장부호(P)·기호(S) 제거, NFKC, casefold.

    숫자 사이의 문장부호는 남긴다 — '3.5시간' 이 '35시간' 과, '10:30' 이 '1030' 과 일치하지 않게.
    """
    if not isinstance(text, str):
        return "", []
    out: List[str] = []
    offsets: List[int] = []
    n = len(text)
    for i, ch in enumerate(text):
        for piece in unicodedata.normalize("NFKC", ch):
            category = unicodedata.category(piece)[0]
            if category in ("Z", "C"):
                continue
            if category in ("P", "S"):
                if category == "P" and 0 < i < n - 1 and text[i - 1].isdigit() and text[i + 1].isdigit():
                    out.append(piece)
                    offsets.append(i)
                continue
            for folded in piece.casefold():
                out.append(folded)
                offsets.append(i)
    return "".join(out), offsets


def locate_quote(quote: Any, text: Any) -> Optional[Tuple[int, int]]:
    """정규화 대조로 quote 가 text 안에 있으면 원문 (start, end), 없으면 None. 빈 인용은 None."""
    needle, _ = normalize_text(quote)
    if not needle:
        return None
    haystack, offsets = normalize_text(text)
    position = haystack.find(needle)
    if position < 0:
        return None
    return offsets[position], offsets[position + len(needle) - 1] + 1


# --- 항목 단위 검증 ---------------------------------------------------------------------------------

def _split_id(item_id: Any) -> Tuple[str, Optional[str]]:
    """'J4-1.a#2' → ('J4-1.a', '2'); 펼치지 않은 항목은 ('J1-1.a', None)."""
    if not isinstance(item_id, str):
        return "", None
    base, sep, element = item_id.partition("#")
    return base.strip(), (element.strip() if sep else None)


def _snapshot(item: Dict[str, Any]) -> Dict[str, Any]:
    snap = {key: item.get(key) for key in ("id", "decision", "question_quote", "answer_quote", "reason")}
    if item.get("turn") is not None:
        snap["turn"] = item.get("turn")
    if isinstance(item.get("fact"), dict):
        snap["fact"] = copy.deepcopy(item["fact"])
    return snap


def _failure(item_id: str, base: str, code: str, item: Optional[Dict[str, Any]], message: Optional[str] = None) -> Dict[str, Any]:
    return {"id": item_id, "base": base, "code": code, "message": message or FAILURE_MESSAGES.get(code, code),
            "item": _snapshot(item) if isinstance(item, dict) else None}


def _question_sources(item: Dict[str, Any], state: Dict[str, Any]) -> List[Tuple[str, Optional[int], str]]:
    """question_quote 를 찾을 원문 — [(source, turn, text)]. turn 이 있으면 그 history 턴(질문+답변)만, 없으면 현재 질문(과 reference)."""
    turn = item.get("turn")
    if isinstance(turn, int) and not isinstance(turn, bool):
        for prior in state.get("history") or []:
            if isinstance(prior, dict) and prior.get("turn") == turn:
                return [("history", turn, f"{prior.get('question') or ''}\n{prior.get('answer') or ''}")]
        return []
    sources = [("question", None, state.get("question") or "")]
    reference = state.get("reference")
    if reference not in (None, "", {}, []):
        sources.append(("reference", None, reference if isinstance(reference, str) else json.dumps(reference, ensure_ascii=False)))
    for attachment in state.get("attachments") or []:   # J8 — 첨부파일에 든 내용도 질문 쪽 원문으로 대조한다
        if isinstance(attachment, dict) and attachment.get("content"):
            sources.append(("attachment", None, attachment["content"]))
    return sources


def _check_question_quote(item: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    quote = _text(item.get("question_quote"))
    check: Dict[str, Any] = {"ok": False, "source": None, "turn": None, "span": None}
    if not quote:
        return check
    for source, turn, text in _question_sources(item, state):
        span = locate_quote(quote, text)
        if span is not None:
            check.update(ok=True, source=source, turn=turn, span=list(span))
            break
    return check


def _server_problem_codes(item: Dict[str, Any]) -> List[Tuple[str, Optional[str]]]:
    server = item.get("server")
    problems = server.get("problems") if isinstance(server, dict) else None
    codes: List[Tuple[str, Optional[str]]] = []
    for problem in problems if isinstance(problems, list) else []:
        if isinstance(problem, dict) and isinstance(problem.get("code"), str):
            codes.append((problem["code"], problem.get("message") if isinstance(problem.get("message"), str) else None))
        elif isinstance(problem, str) and problem:
            codes.append((problem, None))
    return codes


def verify_items(body: Dict[str, Any], state: Dict[str, Any], questions: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """items 마다 서버 검증 → (검증 필드가 채워진 body 사본, failures[{id, base, code, message, item}]).

    각 item 에 서버 소유 필드를 기록한다: ``origin``(questions 의 origin), ``quote_check`` =
    {"question": {ok, source, turn, span}, "answer": {ok, span, required}}. 보내지 않은 id 의 항목은 body.items 에서 제거하고
    failures 에 unrequested_item 로 남긴다. 요청했는데 응답에 없는 base id 는 item_missing (재확인 대상).
    """
    result = copy.deepcopy(body) if isinstance(body, dict) else {}
    raw_items = result.get("items")
    items = [it for it in raw_items if isinstance(it, dict)] if isinstance(raw_items, list) else []
    answer_text = state.get("answer") or "" if isinstance(state, dict) else ""
    state = state if isinstance(state, dict) else {}
    failures: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    answered: set = set()
    for item in items:
        for key in ("quote_check", "origin"):
            item.pop(key, None)
        item_id = item.get("id") if isinstance(item.get("id"), str) else ""
        base, element = _split_id(item_id)
        spec = questions.get(base) if base else None
        if not isinstance(spec, dict):
            failures.append(_failure(item_id or "?", base or item_id or "?", "unrequested_item", item))
            continue
        kept.append(item)
        answered.add(base)
        item["origin"] = spec.get("origin") or "question"
        decision = item.get("decision")
        question_check = _check_question_quote(item, state)
        answer_quote = _text(item.get("answer_quote"))
        answer_span = locate_quote(answer_quote, answer_text) if answer_quote else None
        answer_required = decision == "YES" or (decision == "NO" and not spec.get("missing_type"))
        item["quote_check"] = {"question": question_check,
                               "answer": {"ok": answer_span is not None, "span": list(answer_span) if answer_span else None,
                                          "required": bool(answer_required)}}
        codes: List[Tuple[str, Optional[str]]] = []
        if decision not in DECISIONS:
            codes.append(("decision_invalid", None))
        elif decision == "NOT_APPLICABLE":
            # 적용 여부는 코드 규칙이 정한다. 평가 모델이 적용을 정하는 항목(J1-3·J2-4.b·J3-3.a·J6), 주장이 없는 J5-1.a,
            # 서버가 둔 자리표시 항목, 정의에 요소 단위 해당 없음이 있는 항목의 펼친 요소(J5-2.a#n — 답변에 쓰지 않은 값)만
            # 해당 없음을 허용한다 — 그 밖의 해당 없음은 인용 검증을 피하는 답이라 재확인 대상이다.
            element_na = bool(spec.get("element_na")) and element is not None
            allowed = spec.get("model_applies") or base == "J5-1.a" or item.get("server_placeholder")
            if not (allowed or element_na):
                codes.append(("not_applicable_disallowed", None))
            elif not allowed:
                # 요소 단위 해당 없음은 그 요소(현재 질문·함께 보낸 첨부가 준 값)를 그 원문 그대로 인용해야 한다 — '#1' 만 붙여 항목
                # 전체를 비우거나, 이전 턴·참조 자료의 값을 인용해 현재 질문의 값을 피하지 못하게 (첨부 원문 허용: Codex 리뷰)
                question_quote = _text(item.get("question_quote"))
                # 허용 출처(현재 질문·첨부)에서 직접 찾는다 — 전체 검색은 참조 자료가 먼저 맞으면 첨부를 보지 않는다 (Codex 리뷰)
                allowed = [text for source, _, text in _question_sources(item, state) if source in ("question", "attachment")]
                if not question_quote:
                    codes.append(("question_quote_required", None))
                elif not any(locate_quote(question_quote, text) is not None for text in allowed):
                    codes.append(("question_quote_not_found", None))
        else:
            question_quote = _text(item.get("question_quote"))
            if element is not None and not question_quote and spec.get("origin") != "answer":
                codes.append(("question_quote_required", None))   # 답변에서 펼친 항목(J5-1.a 주장별)은 질문 인용이 없어도 된다
            elif decision == "NO" and spec.get("input_quote") and not question_quote:
                codes.append(("question_quote_required", None))   # J7 — 긴 입력의 어느 부분인지 인용해야 FAIL 근거가 된다
            elif question_quote and not question_check["ok"]:
                codes.append(("question_quote_not_found", None))
            elif question_quote and spec.get("input_quote") and question_check["source"] not in ("question", "reference"):
                codes.append(("question_quote_not_found", None))  # J7 은 이전 턴이 아닌 현재 입력에서 인용
            if answer_required and not answer_quote:
                codes.append(("answer_quote_required", None))
            elif answer_quote and answer_span is None:
                codes.append(("answer_quote_not_found", None))
            fact = item.get("fact")
            sources = fact.get("sources") if isinstance(fact, dict) else None
            if decision == "NO" and isinstance(sources, list) and sources and not any(
                    isinstance(s, dict) and s.get("quote_verified") is True for s in sources):
                codes.append(("source_quote_not_found", None))
        for code, message in _server_problem_codes(item):
            if code not in [c for c, _ in codes]:
                codes.append((code, message))
        for code, message in codes:
            failures.append(_failure(item_id, base, code, item, message))
    result["items"] = kept
    for base in questions:
        if base not in answered:
            failures.append({"id": base, "base": base, "code": "item_missing", "message": FAILURE_MESSAGES["item_missing"], "item": None})
    return result, failures


def recheck_questions(questions: Dict[str, Any], failures: List[Dict[str, Any]], round_no: int, *,
                      notice: Optional[str] = None, items: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """실패한 base id 만 골라 questions 부분집합을 만들고 항목마다 'recheck' 블록을 덧붙인다 (프롬프트가 항목 아래에 렌더링).

    ``items``(현재 body 의 항목)를 주면 그 base 의 요소 전부를 ``previous`` 에 싣는다 — 펼치기 항목은 통과한 요소까지
    다시 나열해 판정하므로(응답이 요소 일부만 돌려주면 merge_recheck 가 나머지를 보존한다) 모델이 전체 목록을 본다.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for failure in failures:
        base = failure.get("base")
        if base in questions:
            grouped.setdefault(base, []).append(failure)
    subset: Dict[str, Any] = {}
    for base, fails in grouped.items():
        previous: List[Dict[str, Any]] = []
        seen: set = set()
        for current in items if isinstance(items, list) else []:
            if isinstance(current, dict) and _split_id(current.get("id"))[0] == base and current.get("id") not in seen:
                previous.append(_snapshot(current))
                seen.add(current.get("id"))
        for failure in fails:
            snap = failure.get("item")
            if isinstance(snap, dict) and snap.get("id") not in seen:
                previous.append(snap)
                seen.add(snap.get("id"))
        subset[base] = {**copy.deepcopy(questions[base]),
                        "recheck": {"round": round_no, "notice": notice or RECHECK_NOTICE,
                                    "problems": [{"id": f["id"], "code": f["code"], "message": f["message"]} for f in fails],
                                    "previous": previous}}
    return subset


def merge_recheck(body: Dict[str, Any], follow_body: Any, requested_ids: Any, *, round_no: Optional[int] = None) -> Dict[str, Any]:
    """requested base id 에 속한 items(펼친 요소 포함)를 follow_body.items 로 통째로 교체한 body 사본.

    요청하지 않은 id 의 항목은 무시한다. 교체된 item 마다 ``rechecks`` 에 {round, replaced: 이전 판정·인용} 을 덧붙인다.
    follow 에 그 base 항목이 하나도 없으면 이전 항목을 그대로 둔다 (다음 검증에서 다시 실패로 잡힌다).
    """
    result = copy.deepcopy(body) if isinstance(body, dict) else {}
    items = result.get("items")
    if not isinstance(items, list):
        items = result["items"] = []
    requested = set(requested_ids or ())
    replacements: Dict[str, List[Dict[str, Any]]] = {}
    follow_items = follow_body.get("items") if isinstance(follow_body, dict) else None
    for raw in follow_items if isinstance(follow_items, list) else []:
        if not isinstance(raw, dict):
            continue
        base, _ = _split_id(raw.get("id"))
        if base in requested:
            replacements.setdefault(base, []).append(copy.deepcopy(raw))
    for base, new_items in replacements.items():
        old = [it for it in items if isinstance(it, dict) and _split_id(it.get("id"))[0] == base]
        old_by_id = {it.get("id"): it for it in old}
        number = round_no if round_no is not None else max((len(it.get("rechecks") or []) for it in old), default=0) + 1
        for new in new_items:
            for key in SERVER_FIELDS:
                new.pop(key, None)
            prior = old_by_id.get(new.get("id"))
            history = list(prior.get("rechecks") or []) if prior else []
            history.append({"round": number, "replaced": _snapshot(prior) if prior else None})
            new["rechecks"] = history
        new_ids = {new.get("id") for new in new_items}
        if new_ids <= set(old_by_id) and len(new_ids) < len(old_by_id):
            # 펼친 요소 일부만 다시 답했다(이전 id 의 부분집합): 답하지 않은 요소는 이미 검증을 거친 이전 판정을 보존한다 —
            # 통과한 요소나 '아니오' 요소가 재확인 응답에서 소리 없이 사라져 세부 기준이 PASS 로 바뀌지 않게.
            new_items.extend(copy.deepcopy(it) for it in old if it.get("id") not in new_ids)
        position = next((i for i, it in enumerate(items) if isinstance(it, dict) and _split_id(it.get("id"))[0] == base), len(items))
        items[:] = [it for it in items if not (isinstance(it, dict) and _split_id(it.get("id"))[0] == base)]
        items[position:position] = new_items
    return result


def mark_unverifiable(body: Dict[str, Any], failures: List[Dict[str, Any]], rounds: int) -> None:
    """남은 실패 항목을 '확인 불가로 FAIL' 로 확정한다 — decision NO · unverifiable True · reason_code · reason '확인 불가: …'.

    원래 모델 판정은 ``model_original`` 에 보존한다. 응답에 없던 항목(item_missing)은 빈 인용의 자리표시 항목을 만든다.
    """
    items = body.get("items")
    if not isinstance(items, list):
        items = body["items"] = []
    by_id = {it.get("id"): it for it in items if isinstance(it, dict)}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for failure in failures:
        if failure.get("code") == "unrequested_item":
            continue   # 이미 제거된 항목
        grouped.setdefault(failure["id"], []).append(failure)
    for item_id, fails in grouped.items():
        base, _ = _split_id(item_id)
        item = by_id.get(item_id)
        if item is None:
            item = {"id": item_id, "decision": "NO", "question_quote": "", "answer_quote": "", "reason": "", "turn": None, "fact": None}
            items.append(item)
            by_id[item_id] = item
            item["model_original"] = None
        elif "model_original" not in item:
            item["model_original"] = _snapshot(item)
        messages = list(dict.fromkeys(f["message"] for f in fails))
        item["decision"] = "NO"
        item["unverifiable"] = True
        item["reason_code"] = FACT_UNVERIFIABLE if base == "J5-1.a" else ITEM_UNVERIFIABLE
        item["reason"] = f"{UNVERIFIABLE_PREFIX}재확인 {rounds}회 뒤에도 확인하지 못함 — " + " · ".join(messages)


# --- 재확인 루프 ------------------------------------------------------------------------------------

def _usage_parts(call: JevCall) -> List[Dict[str, Any]]:
    body = call.body if isinstance(call.body, dict) else {}
    verification = body.get("verification") if isinstance(body.get("verification"), dict) else {}
    parts = verification.get("usage_requests")
    if not isinstance(parts, list):
        parts = (call.accounting or {}).get("cost_parts") if isinstance(call.accounting, dict) else None
    return [copy.deepcopy(p) for p in parts if isinstance(p, dict)] if isinstance(parts, list) else []


def finalize_items(call: JevCall, state: Dict[str, Any], questions: Dict[str, Any], *, ask: Callable[[Dict[str, Any]], JevCall],
                   max_rounds: int = MAX_RECHECK_ROUNDS, cancel_event: Optional[Event] = None) -> JevCall:
    """JevCall(OK) → 인용 검증·재확인을 마친 JevCall. ``ask(questions_subset) -> JevCall`` 는 jev_eval 이 client.system_one 을 감싸 준다.

    회차별 usage·attempts 를 합산하고 verification.usage_requests[].stage 를 'recheck#n' 으로, verification.rechecks 를
    {policy, rounds, calls, unverifiable} 로 남긴다. ask 가 TIMEOUT/API_ERROR 를 돌려주거나 예외를 내면 그 회차는 소진된
    것으로 치고 계속한다. CancelledError 는 그대로 전파한다 (jev_eval 이 SKIPPED 처리).
    """
    if call.status != "OK" or not isinstance(call.body, dict):
        return call
    started = time.monotonic()
    body = copy.deepcopy(call.body)
    attempts = int(call.attempts or 0)
    usage: Dict[str, Any] = {}
    _sum_usage(usage, call.usage or {})
    parts = [{**part, "stage": part.get("stage") or "evaluation"} for part in _usage_parts(call)]
    # 서버 소유 필드는 모델 출력에서 먼저 지운다 (transport 의 server_check 가 쓴 'server' 블록은 서버 것이라 둔다)
    for item in body.get("items") if isinstance(body.get("items"), list) else []:
        if isinstance(item, dict):
            for key in SERVER_FIELDS:
                item.pop(key, None)
    body, failures = verify_items(body, state, questions)
    calls: List[Dict[str, Any]] = []
    round_no = 0
    extra_latency = 0
    while failures and round_no < max_rounds:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("items recheck cancelled")
        round_no += 1
        subset = recheck_questions(questions, failures, round_no, items=body.get("items"))
        entry: Dict[str, Any] = {"round": round_no, "items": sorted(subset), "status": None, "attempts": 0, "error": None}
        calls.append(entry)
        asked_at = time.monotonic()
        try:
            follow = ask(subset)
        except CancelledError:
            raise
        except Exception as exc:   # 재확인 호출의 오류는 그 회차 소진으로만 남긴다
            entry.update(status="API_ERROR", error=redact(f"{type(exc).__name__}: {exc}"))
            extra_latency += int((time.monotonic() - asked_at) * 1000)
            continue
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("items recheck cancelled")
        extra_latency += int(follow.latency_ms) if isinstance(follow.latency_ms, int) else int((time.monotonic() - asked_at) * 1000)
        entry.update(status=follow.status, attempts=int(follow.attempts or 0), error=redact(follow.error) if follow.error else None)
        attempts += int(follow.attempts or 0)
        _sum_usage(usage, follow.usage or {})
        parts.extend({**part, "stage": f"recheck#{round_no}"} for part in _usage_parts(follow))
        if follow.status != "OK" or not isinstance(follow.body, dict):
            continue
        body = merge_recheck(body, follow.body, list(subset), round_no=round_no)
        body, failures = verify_items(body, state, questions)
    if failures:
        mark_unverifiable(body, failures, round_no)
    if any(isinstance(spec, dict) and (spec.get("criterion") == "J5" or key.startswith("J5")) for key, spec in questions.items()):
        from .semantic_facts import finalize as finalize_facts   # 지연 import — semantic_facts 가 locate_quote 를 쓴다
        finalize_facts(body["items"], round_no)
    verification = body.get("verification")
    if not isinstance(verification, dict):
        verification = body["verification"] = {}
    verification["quote_policy"] = ITEM_QUOTE_POLICY
    verification["usage_requests"] = parts
    verification["rechecks"] = {"policy": RECHECK_POLICY, "rounds": round_no, "calls": calls,
                                "unverifiable": list(dict.fromkeys(f["id"] for f in failures if f.get("code") != "unrequested_item"))}
    accounting = dict(call.accounting or {})
    accounting.update(cost_parts=parts, usage=usage)
    latency = (int(call.latency_ms) if isinstance(call.latency_ms, int) else 0) + extra_latency
    if latency == 0:
        latency = int((time.monotonic() - started) * 1000)
    return JevCall("OK", body=body, attempts=attempts, latency_ms=latency, http_status=call.http_status,
                   usage=usage, accounting=accounting)


# --- 모델 간 요소 대조 (순수 함수) -------------------------------------------------------------------


