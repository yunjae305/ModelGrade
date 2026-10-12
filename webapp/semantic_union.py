"""모델 간 요소 합집합 — 같은 질문을 평가한 여러 모델(제공자)의 펼치기 항목 요소를 대조해, 빠진 요소만 재확인하고 다시 집계한다.

설계 3단계 '모델 간 요소 합집합': 펼치기 항목(조건별·대상별·요구별 …)은 평가 모델이 질문에서 요소를 나열하는데, 같은 질문인데도
한 평가에서만 나열된 요소가 있으면 다른 평가는 그 요소를 빠뜨린 것이다. 네 판정이 모두 끝난 뒤 ``semantic_quotes.missing_elements``
로 질문 원문 위치가 겹치지 않는 요소를 찾고, 빠진 평가에만 그 항목을 ``union_recheck_questions`` 로 다시 묻는다(요소 목록 전부를
다시 나열하게 하고, 응답은 ``finalize_items`` 의 인용 검증·재확인을 그대로 거친다). 그 뒤 레코드를 다시 집계하고 배치 요약을 새로 쓴다.

- 대상은 질문에서 생기는 펼치기 항목(J2-2.a·J2-3.a·J4-1.a·J4-1.b·J4-2.a·J4-3.a·J4-4.a·J5-2.a)뿐이다. 답변에서 생기는 항목(J5-1.a)과
  이전 턴을 인용하는 J6 항목은 질문 원문 위치로 대조할 수 없어 제외한다.
- 평가 모델에게는 다른 평가의 이름(모델·제공자)을 보내지 않는다 — '평가 1·2·…' 라벨만 보낸다.
- 재확인 호출이 실패하면 그 평가의 이전 판정을 그대로 두고 ``record["union"]`` 에 오류를 남긴다. 중단 요청은 CancelledError 로 끝낸다.
- legacy 레코드(pipeline 없음)나 SUCCESS 가 아닌 레코드는 대조에서 뺀다. 판정 값은 서버 집계가 정하고 이 모듈은 호출·교체·재집계만 한다.
"""
from __future__ import annotations

import copy
from concurrent.futures import CancelledError, ThreadPoolExecutor
from threading import Event
from typing import Any, Callable, Dict, List, Optional, Tuple

from code_eval.combined import combined_summary
from code_eval.jev_client import JevCall
from code_eval.jev_eval import build_history, conversation_contexts, summarize_records
from code_eval.semantic_items import EXPAND_IDS, SPECS_BY_ID, applicable_items
from code_eval.semantic_items_eval import aggregate_items, apply_overrides, parse_item_answers
from code_eval.semantic_quotes import _items_in, _split_id, finalize_items, missing_elements, union_recheck_questions

__all__ = ["UNION_VERSION", "UNION_BASES", "plan", "recheck", "reconcile"]

UNION_VERSION = "semantic-union-v1"
#: 대조하는 펼치기 항목 — 질문에서 생기고 현재 질문 원문을 인용하는 것만
UNION_BASES: Tuple[str, ...] = tuple(sorted(base for base in EXPAND_IDS
                                            if SPECS_BY_ID[base].origin == "question" and not base.startswith("J6")))


def _items_records(evaluations: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """items 파이프라인으로 평가된 제공자 배치만 — {provider: batch}."""
    return {provider: batch for provider, batch in (evaluations or {}).items()
            if isinstance(batch, dict) and isinstance(batch.get("jev_eval"), dict)
            and batch["jev_eval"].get("pipeline") == "items" and isinstance(batch["jev_eval"].get("records"), list)}


def _labels(providers: List[str], blind_labels: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """평가 모델에 보낼 평가 이름 — 모델·제공자 이름 대신 '평가 n' (블라인드 별칭이 있으면 그 글자)."""
    return {provider: f"평가 {blind_labels[provider]}" if isinstance(blind_labels, dict) and blind_labels.get(provider) else f"평가 {n}"
            for n, provider in enumerate(providers, 1)}


def plan(evaluations: Dict[str, Any], *, blind_labels: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """재확인할 (provider, index) 목록 — [{"provider", "index", "missing": {base: [{question_quote, seen_in}]}}].

    같은 index 의 질문이 제공자마다 같다는 전제(같은 질문 세트)이며, 질문 원문이 다르면 그 index 는 건너뛴다.
    """
    batches = _items_records(evaluations)
    if len(batches) < 2:
        return []
    labels = _labels(sorted(batches), blind_labels)
    length = min(len(batch["jev_eval"]["records"]) for batch in batches.values())
    tasks: List[Dict[str, Any]] = []
    for index in range(length):
        records: Dict[str, Dict[str, Any]] = {}
        questions = set()
        for provider, batch in batches.items():
            record = batch["jev_eval"]["records"][index]
            case = batch["cases"][index] if index < len(batch.get("cases") or []) else None
            if (isinstance(record, dict) and record.get("status") == "SUCCESS" and record.get("pipeline") == "items"
                    and isinstance(case, dict) and isinstance(case.get("question"), str)):
                records[provider] = record
                questions.add(case["question"])
        if len(records) < 2 or len(questions) != 1:
            continue
        question = next(iter(questions))
        missing: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for base in UNION_BASES:
            for provider, elements in missing_elements(records, base, question).items():
                missing.setdefault(provider, {})[base] = [
                    {"question_quote": e["question_quote"], "seen_in": [labels[p] for p in e["seen_in"]]} for e in elements]
        for provider in sorted(missing):
            tasks.append({"provider": provider, "index": index, "missing": missing[provider]})
    return tasks


def _state_of(batch: Dict[str, Any], index: int, context: Optional[Dict[str, Any]], collected_at: Optional[str]) -> Dict[str, Any]:
    case = batch["cases"][index]
    state: Dict[str, Any] = {"question": case["question"], "answer": case["answer"]}
    if case.get("reference") is not None:
        state["reference"] = case["reference"]
    if context and context.get("prior"):
        state["history"] = build_history(context["prior"])[0]
    if collected_at:
        state["collected_at"] = collected_at
    return state


def _questions_of(case: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
    """레코드에 실제로 들어 있는 항목만의 questions — 보내지 않은 항목(J5-1.a 등)을 '응답 없음' 으로 잡지 않게."""
    present = {_split_id(it.get("id"))[0] for it in _items_in(record)}
    return {spec.id: spec.to_prompt_dict(evidence) for spec, evidence in applicable_items(case, record.get("applicability"))
            if spec.id in present}


def _reaggregate(record: Dict[str, Any], items: List[Dict[str, Any]], questions: Dict[str, Any]) -> None:
    """교체된 항목 목록으로 decisions·items 통계·criteria 를 다시 쓴다 (jev_eval._items_record 와 같은 규칙)."""
    verification = record.get("verification") if isinstance(record.get("verification"), dict) else {}
    not_sent = [entry for entry in (verification.get("items_not_sent") or []) if isinstance(entry, dict)]
    items_by_cid = parse_item_answers(items, questions)
    overrides = apply_overrides(items_by_cid)
    decisions, stats = aggregate_items(items_by_cid, questions, applicability=record.get("applicability"),
                                       rechecks=record.get("recheck"), not_sent=not_sent)
    stats["overrides"] = overrides
    record["decisions"] = decisions
    record["items"] = stats
    record["criteria"] = [cid for cid in decisions if decisions[cid].get("status") != "NOT_APPLICABLE"]
    j5 = decisions.get("J5")
    basis = (j5.get("fact_check") or {}).get("basis") if isinstance(j5, dict) and j5.get("status") != "NOT_APPLICABLE" else None
    record["j5_basis"] = basis if basis in ("web", "reference", "input", "mixed") else None
    record["review_required"] = False


def recheck(batch: Dict[str, Any], index: int, missing: Dict[str, List[Dict[str, Any]]], client: Any, *,
            context: Optional[Dict[str, Any]] = None, collected_at: Optional[str] = None,
            cancel_event: Optional[Event] = None, round_no: int = 1) -> Dict[str, Any]:
    """한 평가(provider 배치의 index 질문)에 빠진 요소를 다시 묻고 레코드를 제자리에서 갱신한다. 반환은 record["union"] 기록."""
    record = batch["jev_eval"]["records"][index]
    case = batch["cases"][index]
    questions = _questions_of(case, record)
    subset = union_recheck_questions(questions, missing, round_no)
    union: Dict[str, Any] = {"version": UNION_VERSION, "round": round_no, "bases": {}, "status": None, "attempts": 0, "error": None}
    record["union"] = union
    if not subset:
        union["status"] = "NO_TARGET"
        return union
    state = _state_of(batch, index, context, collected_at)
    old_items = copy.deepcopy(_items_in(record))
    for base in subset:
        union["bases"][base] = {"elements": copy.deepcopy(missing[base]),
                                "before": [it["id"] for it in old_items if _split_id(it.get("id"))[0] == base], "after": None}
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("semantic union cancelled")
    request = {"state": state, "questions": subset}
    if cancel_event is not None and getattr(client, "supports_cancellation", False):
        request["cancel_event"] = cancel_event
    try:
        follow = client.system_one(**request)
    except CancelledError:
        raise
    except Exception as exc:   # 호출 오류는 이 평가의 이전 판정을 유지하고 기록만 남긴다
        union.update(status="API_ERROR", error=f"{type(exc).__name__}: {exc}")
        return union
    union.update(status=follow.status, attempts=int(follow.attempts or 0), error=follow.error)
    if follow.status != "OK" or not isinstance(follow.body, dict):
        return union

    def ask(questions_subset: Dict[str, Any]) -> JevCall:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("semantic union cancelled")
        return client.system_one(**{**request, "questions": questions_subset})

    # 응답은 빠진 요소를 포함해 요소 전부를 다시 나열한 것 — 새 항목만 인용 검증·재확인을 거친다
    body = {"model": follow.body.get("model") or record.get("model"), "verification": copy.deepcopy(follow.body.get("verification") or {}),
            "items": [copy.deepcopy(it) for it in (follow.body.get("items") or []) if isinstance(it, dict)
                      and _split_id(it.get("id"))[0] in subset]}
    verified = finalize_items(JevCall("OK", body=body, attempts=follow.attempts, latency_ms=follow.latency_ms, usage=follow.usage,
                                      accounting=follow.accounting), state, subset, ask=ask, cancel_event=cancel_event)
    new_items = [it for it in verified.body.get("items") or [] if isinstance(it, dict)]
    union["attempts"] = int(verified.attempts or 0)
    union["rechecks"] = copy.deepcopy((verified.body.get("verification") or {}).get("rechecks"))
    replaced: List[Dict[str, Any]] = []
    for base in subset:
        fresh = [it for it in new_items if _split_id(it.get("id"))[0] == base]
        previous = [it for it in old_items if _split_id(it.get("id"))[0] == base]
        fresh_ids = {it.get("id") for it in fresh}
        if fresh and fresh_ids <= {it.get("id") for it in previous} and len(fresh_ids) < len(previous):
            fresh.extend(copy.deepcopy(it) for it in previous if it.get("id") not in fresh_ids)   # 일부만 답하면 나머지는 보존
        chosen = fresh or previous
        for it in chosen:
            it["union_round"] = round_no
        union["bases"][base]["after"] = [it["id"] for it in chosen]
        replaced.extend(chosen)
    kept = [it for it in old_items if _split_id(it.get("id"))[0] not in subset]
    _reaggregate(record, kept + replaced, questions)
    verification = record.setdefault("verification", {})
    if isinstance(verification, dict):
        verification.setdefault("union_calls", []).append({"round": round_no, "items": sorted(subset), "status": follow.status,
                                                           "attempts": union["attempts"], "usage": copy.deepcopy(verified.usage or {})})
    return union


def reconcile(evaluations: Dict[str, Any], client: Any, *, semantic_contexts: Optional[Dict[str, Dict[str, Any]]] = None,
              blind_labels: Optional[Dict[str, str]] = None, cancel_event: Optional[Event] = None, workers: int = 1,
              on_record: Optional[Callable[[str, int, Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    """모든 제공자의 평가가 끝난 뒤 — 대조 → 빠진 요소 재확인 → 레코드 갱신 → 배치 요약 재집계. 반환은 run 에 남길 요약.

    ``evaluations`` 는 {provider: batch} 이며 제자리에서 바뀐다. ``semantic_contexts`` 는 {provider: {answer_tools, collected_at}}.
    중단 요청이 오면 그때까지 끝난 교체만 반영하고 status 'CANCELLED' 로 돌려준다.
    """
    summary: Dict[str, Any] = {"version": UNION_VERSION, "status": "COMPLETED", "providers": sorted(_items_records(evaluations)),
                               "questions": 0, "rechecked": 0, "updated": 0, "errors": 0, "tasks": []}
    tasks = plan(evaluations, blind_labels=blind_labels)
    summary["questions"] = len({task["index"] for task in tasks})
    if not tasks:
        return summary
    if client is None:
        summary.update(status="NOT_CONFIGURED")
        return summary
    contexts = {provider: conversation_contexts(batch["cases"]) for provider, batch in _items_records(evaluations).items()}

    def run(task: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[BaseException]]:
        provider, index = task["provider"], task["index"]
        batch = evaluations[provider]
        before = copy.deepcopy(batch["jev_eval"]["records"][index].get("decisions"))
        collected = (semantic_contexts or {}).get(provider, {}).get("collected_at")
        try:
            union = recheck(batch, index, task["missing"], client, context=contexts[provider].get(index),
                            collected_at=collected if isinstance(collected, str) else None, cancel_event=cancel_event)
        except CancelledError as exc:
            return task, None, exc
        union["changed"] = batch["jev_eval"]["records"][index].get("decisions") != before
        return task, union, None

    cancelled = False
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(tasks))), thread_name_prefix="semantic-union") as pool:
        for task, union, error in pool.map(run, tasks):
            if error is not None:
                cancelled = True
                continue
            summary["rechecked"] += union.get("status") == "OK"
            summary["errors"] += union.get("status") not in ("OK", "NO_TARGET")
            summary["updated"] += bool(union.get("changed"))
            summary["tasks"].append({"provider": task["provider"], "index": task["index"], "bases": sorted(task["missing"]),
                                     "status": union.get("status"), "changed": bool(union.get("changed")), "error": union.get("error")})
            if on_record and union.get("status") == "OK":
                on_record(task["provider"], task["index"], evaluations[task["provider"]]["jev_eval"]["records"][task["index"]])
    for provider, batch in _items_records(evaluations).items():
        jev = batch["jev_eval"]
        final = [r for r in jev["records"] if isinstance(r, dict)]
        summarized = summarize_records(final, contexts[provider], batch["cases"], pipeline="items")
        jev.update(summary=summarized["summary"], completed=summarized["completed"], review_required=summarized["review_required"],
                   unverified=summarized["unverified"], conversations=summarized["conversations"], fulfillment=summarized["fulfillment"])
        jev["union"] = {"version": UNION_VERSION, "rechecked": sum(1 for t in summary["tasks"] if t["provider"] == provider and t["status"] == "OK"),
                        "updated": sum(1 for t in summary["tasks"] if t["provider"] == provider and t["changed"])}
        batch["combined"] = combined_summary(batch)
    if cancelled:
        summary["status"] = "CANCELLED"
    return summary
