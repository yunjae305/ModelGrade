"""Message Batches 디스패처 — 의미 평가 요청을 묶어 제출하고 폴링해 레코드를 만든다 (판정 규칙은 실시간과 같다).

설계 3c: pilot 1건 → 나머지를 묶음(기본 20문항)으로 전부 제출 → 10~30초 폴링·진행률 → custom_id 로 대응 → 결과가 만료·오류·
검색 이어가기(pause_turn)·취소이면 그 질문만 실시간으로 다시 보냄 → 중단 요청이 오면 열린 묶음을 취소하고 남은 질문은 SKIPPED.

- client 는 ``build_request(state, questions)``·``finish_batch_result(raw, context, payload=…)``·``system_one`` 을 가진 Claude API 평가자다.
  묶음 결과의 인용 검증·재확인(items)과 J5 출처 대조는 실시간과 같은 코드(``jev_eval._record_from_call``)가 맡고, 재확인 호출은
  실시간 ``system_one`` 으로 돈다.
- ``existing_batches`` 를 주면(서버 재시작 뒤 이어받기) 그 묶음은 다시 제출하지 않고 폴링만 한다. custom_id 는 ``custom_id_prefix``
  + 질문 index 라 같은 입력이면 같은 id 다 (Batches API 규격: 영문·숫자·_·- 64자 이하). 제출할 때 질문마다 판정 조건 지문
  (파이프라인·호출 방식을 뺀 평가 설정·보낸 입력)을 남기고, 이어받기는 지문이 같은 질문만 한다 — 조건이 바뀌면 새로 제출한다.
- 상태 조회가 영구 오류(400·401·403·404)이거나 연속 5회 실패하면 그 묶음은 포기(가능하면 취소)하고 질문을 실시간으로 다시 보낸다.
  실패가 이어지면 조회 간격을 2배씩(최대 300초) 늘린다. 제출 직전마다 중단 요청을 확인한다.
- 진행 상황은 ``on_event({"type": "batch_progress", …})`` 로 알린다(묶음 ID·상태·건수·제출/완료 수). 결과 dict 에 ``dispatch``·``batches`` 를 남긴다.
- 폴링 대기는 ``sleep`` 콜백(테스트 대역)이며 중단 요청을 0.1초 단위로 확인한다.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any, Callable, Dict, List, Optional, Tuple

from .anthropic_batch_client import BatchApiError
from .answer_citations import prompt_citations
from .jev_client import JevStatus, _sum_usage
from .jev_eval import (CANCEL_REASON, DEFAULT_WORKERS, _case_request, _finish_batch, _prepare_batch, _publish_record, _record,
                       _record_from_call, _start_events, case_citations, evaluate_jev_case)

__all__ = ["BATCH_DISPATCH_VERSION", "supports_batches", "evaluate_jev_batch_batched"]

BATCH_DISPATCH_VERSION = "jev-batch-dispatch-v1"
#: 실시간으로 다시 보내는 결과 종류와 사유
FALLBACK_REASONS = {"expired": "묶음 처리 시간(24시간) 안에 끝나지 않아 실시간으로 다시 보냄",
                    "errored": "묶음 결과가 오류라 실시간으로 다시 보냄",
                    "canceled": "묶음이 취소되어 실시간으로 다시 보냄",
                    "invalid": "묶음 결과 줄을 읽을 수 없어 실시간으로 다시 보냄",
                    "missing": "묶음 결과에 이 질문이 없어 실시간으로 다시 보냄",
                    "pause_turn": "검색 이어가기(pause_turn)가 필요해 실시간으로 다시 보냄",
                    "submit_failed": "묶음 제출에 실패해 실시간으로 보냄",
                    "retrieve_failed": "묶음 상태를 조회할 수 없어 실시간으로 다시 보냄"}
#: 다시 조회해도 소용없는 상태 조회 오류(요청·인증·권한·없는 묶음)와 일시 오류의 연속 허용 횟수
RETRIEVE_PERMANENT_STATUSES = (400, 401, 403, 404)
MAX_RETRIEVE_FAILURES = 5
MAX_POLL_SECONDS = 300.0
#: 판정에 영향이 없는 호출 방식 설정 — 지문에서 뺀다 (묶음 크기·조회 주기를 바꿔 이어받아도 같은 판정 조건)
_DISPATCH_ONLY = ("dispatch", "batch_questions", "batch_poll_seconds")
_STORED_KEYS = ("id", "status", "counts", "custom_ids", "fingerprints", "submitted_at", "ended_at", "fallbacks", "resumed", "cancel_status")


def request_fingerprint(pipeline: str, config: Any, request: Dict[str, Any]) -> str:
    """질문 하나의 판정 조건 지문 — 파이프라인·평가 설정(호출 방식 제외)·보낸 state·questions. 실행 시각은 넣지 않는다."""
    manifest = config.to_manifest() if callable(getattr(config, "to_manifest", None)) else {}
    manifest = {key: value for key, value in manifest.items() if key not in _DISPATCH_ONLY}
    body = json.dumps({"pipeline": pipeline, "client": manifest, "state": request.get("state"), "questions": request.get("questions")},
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


def _merge_carried(record: Dict[str, Any], parts: List[Dict[str, Any]]) -> None:
    """묶음에서 이미 쓴 요청(pause_turn 으로 끝나 실시간으로 넘긴 것)의 토큰·시도·요청별 사용량을 재전송 레코드에 더한다."""
    if not parts:
        return
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    for part in parts:
        _sum_usage(usage, part.get("usage") or {})
    record["usage"] = usage
    record["attempts"] = (record.get("attempts") or 0) + len(parts)
    verification = record.get("verification")
    if not isinstance(verification, dict):
        verification = record["verification"] = {}
    verification["usage_requests"] = copy.deepcopy(parts) + list(verification.get("usage_requests") or [])


def supports_batches(client: Any, config: Any) -> bool:
    return (getattr(config, "dispatch", "realtime") == "batch" and client is not None
            and all(callable(getattr(client, name, None)) for name in ("build_request", "finish_batch_result", "batch_client")))


def _wait(seconds: float, cancel_event: Optional[Event], sleep: Callable[[float], None]) -> bool:
    """seconds 동안 기다리되 중단 요청이면 바로 True. 대역 sleep(테스트)은 한 번만 불러 그 자체가 시계 역할을 한다."""
    if sleep is not time.sleep:
        sleep(max(0.0, seconds))
        return cancel_event is not None and cancel_event.is_set()
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        sleep(min(0.1, remaining))


def evaluate_jev_batch_batched(batch: Dict[str, Any], client: Any, config: Any = None, access_reason: Optional[str] = None,
                               workers: int = DEFAULT_WORKERS, progress: Optional[Callable[[int, int], None]] = None,
                               on_record: Optional[Callable[[int, Dict[str, Any]], None]] = None,
                               on_start: Optional[Callable[[int], None]] = None, on_event: Optional[Callable] = None,
                               cancel_event: Optional[Event] = None, *, pipeline: str = "legacy",
                               context: Optional[Dict[str, Any]] = None, custom_id_prefix: str = "q",
                               existing_batches: Optional[List[Dict[str, Any]]] = None,
                               sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """evaluate_jev_batch 와 같은 입력·출력. 호출만 Message Batches 로 한다."""
    prep = _prepare_batch(batch, client, config, pipeline=pipeline, context=context)
    config = prep.config
    cases, targets, applies = prep.cases, prep.targets, prep.applies
    poll_seconds = float(getattr(config, "batch_poll_seconds", 15) or 15)
    chunk = int(getattr(config, "batch_questions", 20) or 20)
    done_count = 0
    batches: List[Dict[str, Any]] = []        # 묶음 기록 [{id, status, counts, custom_ids{custom_id: index}, submitted_at, ended_at, fallbacks}]
    fallback_reasons: Dict[int, str] = {}
    carried: Dict[int, List[Dict[str, Any]]] = {}   # pause_turn 으로 실시간에 넘긴 질문의 묶음 요청 사용량

    def publish(i: int, record: Dict[str, Any], *, batch_info: Optional[Dict[str, Any]] = None) -> None:
        nonlocal done_count
        if batch_info is not None:
            record["batch"] = batch_info
        _publish_record(prep, i, record, on_record)
        done_count += 1
        if progress:
            progress(done_count, len(targets))

    def emit_progress(note: Optional[str] = None) -> None:
        if on_event:
            on_event({"type": "batch_progress", "version": BATCH_DISPATCH_VERSION, "total": len(targets), "done": done_count,
                      "submitted": sum(len(b["custom_ids"]) for b in batches),
                      "batches": [{k: copy.deepcopy(b.get(k)) for k in _STORED_KEYS} for b in batches],
                      **({"note": note} if note else {})})

    for i, record in enumerate(prep.records):
        if record is not None:
            _publish_record(prep, i, record, on_record)
    if client is None or not targets:
        for i in targets:
            publish(i, _record(cases[i]["case_id"], JevStatus.NOT_CONFIGURED, tuple(applies[i]["criteria"]), error=access_reason))
        return _finish_batch(prep, batch, client, access_reason, dispatch="batch", batch_dispatch=BATCH_DISPATCH_VERSION, batches=[])

    # --- 요청 조립 (호출 전, 메인 스레드) ---------------------------------------------------------------
    requests: Dict[int, Dict[str, Any]] = {}    # index → {"request", "payload", "context", "custom_id", "started"}
    for i in targets:
        case = cases[i]
        citations, structured_sources = case_citations(case["answer"], tuple(applies[i]["criteria"]), prep.fact_screening,
                                                       case.get("answer_sources"))
        # 실시간(evaluate_jev_case)과 같은 입력 — 첨부파일(J8)·확인 한도 밖 출처 URL(J5 PASS 확정 막음)까지 (Codex 리뷰)
        request = _case_request(case["question"], case["answer"], tuple(applies[i]["criteria"]), case.get("reference"),
                                prep.histories.get(i) or None, client=client, cancel_event=None, pipeline=pipeline,
                                item_specs=prep.item_specs.get(i), collected_at=prep.collected_at,
                                citations=prompt_citations(citations), attachments=case.get("attachments"),
                                unchecked_citations=[c["url"] for c in citations if c.get("valid_url") and not c.get("checked")])
        try:
            payload, ctx = client.build_request(request["state"], request["questions"])
        except (ValueError, TypeError, UnicodeError) as exc:
            publish(i, _record(case["case_id"], JevStatus.INVALID_RESPONSE, tuple(applies[i]["criteria"]),
                               error=f"요청 조립 실패: {type(exc).__name__}"))
            continue
        ctx["state"] = request["state"]
        requests[i] = {"request": request, "payload": payload, "context": ctx, "custom_id": f"{custom_id_prefix}{i}",
                       "started": time.monotonic(), "citations": citations, "structured_sources": structured_sources,
                       "fingerprint": request_fingerprint(pipeline, config, request)}
    by_custom_id = {entry["custom_id"]: i for i, entry in requests.items()}
    batch_client = client.batch_client()

    def realtime(i: int, reason: str) -> Dict[str, Any]:
        """묶음으로 끝내지 못한 질문을 실시간으로 — 레코드에 fallback 사유를 남긴다."""
        case = cases[i]
        fallback_reasons[i] = reason
        if cancel_event is not None and cancel_event.is_set():
            record = _record(case["case_id"], JevStatus.SKIPPED, tuple(applies[i]["criteria"]), error=CANCEL_REASON)
            _merge_carried(record, carried.pop(i, []))   # 묶음에서 이미 쓴 사용량은 중단돼도 남긴다
            return record
        _start_events(prep, i, on_start, on_event)
        record = evaluate_jev_case(case["case_id"], case["question"], case["answer"], client, access_reason,
                                   tuple(applies[i]["criteria"]), reference=case.get("reference"),
                                   history=prep.histories.get(i) or None, cancel_event=cancel_event,
                                   fail_evidence_check=prep.fact_screening, citation_check=prep.fact_screening, pipeline=pipeline,
                                   item_specs=prep.item_specs.get(i), applicability=applies[i], collected_at=prep.collected_at,
                                   sources=case.get("answer_sources"), attachments=case.get("attachments"))
        _merge_carried(record, carried.pop(i, []))
        return record

    def from_batch(i: int, message: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """묶음 결과 메시지 → 레코드. 이어가기가 필요하면 None (실시간으로)."""
        entry = requests[i]
        case = cases[i]
        _start_events(prep, i, on_start, on_event)
        carry: List[Dict[str, Any]] = []
        call = client.finish_batch_result(message, entry["context"], payload=copy.deepcopy(entry["payload"]), cancel_event=cancel_event,
                                          carry=carry)
        if call is None:
            # 이어가기가 필요해 실시간으로 넘긴다 — 묶음 요청에서 이미 쓴 사용량은 재전송 레코드에 더한다
            carried[i] = [{**part, "dispatch": "batch"} for part in carry]
            return None
        request = entry["request"]

        def ask(subset: Dict[str, Any]):
            if cancel_event is not None and cancel_event.is_set():
                from concurrent.futures import CancelledError
                raise CancelledError("items recheck cancelled")
            return client.system_one(**{**request, "questions": subset, **({"cancel_event": cancel_event} if cancel_event is not None else {})})
        basis = {"j5_basis": ("reference" if request["state"].get("reference") is not None else "engine")
                 if "J5" in applies[i]["criteria"] and pipeline != "items" else None}
        return _record_from_call(case["case_id"], tuple(applies[i]["criteria"]), call, request, entry["started"], basis,
                                 pipeline=pipeline, ask=ask, applicability=applies[i], cancel_event=cancel_event,
                                 client=client, fail_evidence_check=prep.fact_screening, citation_check=prep.fact_screening,
                                 question=case["question"], citations=entry["citations"],
                                 structured_sources=entry["structured_sources"])

    def submit(indices: List[int]) -> Optional[Dict[str, Any]]:
        try:
            summary = batch_client.create([{"custom_id": requests[i]["custom_id"], "params": requests[i]["payload"]} for i in indices])
        except (BatchApiError, ValueError) as exc:
            emit_progress(f"묶음 제출 실패: {exc}")
            return None
        info = {**summary, "custom_ids": {requests[i]["custom_id"]: i for i in indices},
                "fingerprints": {requests[i]["custom_id"]: requests[i]["fingerprint"] for i in indices},
                "submitted_at": time.time(), "ended_at": None, "fallbacks": {}}
        batches.append(info)
        emit_progress()
        return info

    def cancel_open() -> None:
        for info in batches:
            if info["status"] in ("in_progress", "canceling") and info.get("id"):
                try:
                    info.update({k: v for k, v in batch_client.cancel(info["id"]).items() if k in ("status", "counts")})
                except BatchApiError:
                    info["status"] = "cancel_failed"
        emit_progress("중단 요청으로 묶음을 취소함")

    def settle(info: Dict[str, Any]) -> None:
        """끝난 묶음의 결과를 받아 질문마다 레코드를 만든다. 실시간 재전송은 작업자 수만큼 동시에."""
        pending = dict(info["custom_ids"])
        results: Dict[int, Tuple[str, Optional[Dict[str, Any]], Optional[str]]] = {}
        try:
            rows = list(batch_client.results(info["results_url"])) if info.get("results_url") else []
        except BatchApiError as exc:
            rows = []
            emit_progress(f"묶음 결과 받기 실패: {exc}")
        for row in rows:
            i = pending.pop(row.get("custom_id"), None) if isinstance(row.get("custom_id"), str) else None
            if i is None:
                continue
            results[i] = (row["type"], row.get("message"), row.get("error"))
        for custom_id, i in pending.items():
            results[i] = ("missing", None, None)
        fallbacks: List[Tuple[int, str]] = []
        for i, (kind, message, error) in sorted(results.items()):
            if cancel_event is not None and cancel_event.is_set():
                publish(i, _record(cases[i]["case_id"], JevStatus.SKIPPED, tuple(applies[i]["criteria"]), error=CANCEL_REASON),
                        batch_info={"id": info["id"], "custom_id": requests[i]["custom_id"], "result": kind})
                continue
            if kind == "succeeded" and isinstance(message, dict):
                record = from_batch(i, message)
                if record is not None:
                    publish(i, record, batch_info={"id": info["id"], "custom_id": requests[i]["custom_id"], "result": "succeeded"})
                    continue
                kind = "pause_turn"
            fallbacks.append((i, kind if kind in FALLBACK_REASONS else "invalid"))
        resend(info, fallbacks)
        info["ended_at"] = time.time()
        emit_progress()

    def resend(info: Dict[str, Any], fallbacks: List[Tuple[int, str]]) -> None:
        """묶음으로 끝내지 못한 질문을 실시간으로 — 작업자 수만큼 동시에."""
        if not fallbacks:
            return
        info["fallbacks"] = {requests[i]["custom_id"]: FALLBACK_REASONS[kind] for i, kind in fallbacks}
        emit_progress(f"묶음 {info['id']}: 실시간 재전송 {len(fallbacks)}건")
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(fallbacks)))) as pool:
            for (i, kind), record in zip(fallbacks, pool.map(lambda pair: realtime(*pair), [(i, FALLBACK_REASONS[k]) for i, k in fallbacks])):
                publish(i, record, batch_info={"id": info["id"], "custom_id": requests[i]["custom_id"], "result": kind,
                                               "fallback": FALLBACK_REASONS[kind]})

    def abandon(info: Dict[str, Any], exc: BatchApiError) -> None:
        """상태를 더 조회할 수 없는 묶음 — 가능하면 취소하고(이중 과금 방지) 그 질문들을 실시간으로 다시 보낸다."""
        if getattr(exc, "http_status", None) not in RETRIEVE_PERMANENT_STATUSES:
            try:
                batch_client.cancel(info["id"])
            except BatchApiError:
                pass
        info["status"] = "retrieve_failed"
        emit_progress(f"묶음 {info['id']} 상태 조회 포기: {exc}")
        resend(info, [(i, "retrieve_failed") for i in info["custom_ids"].values() if prep.records[i] is None])
        info["ended_at"] = time.time()
        emit_progress()

    def poll_until_ended(targets_info: List[Dict[str, Any]]) -> bool:
        """주어진 묶음이 모두 끝날 때까지 폴링. 중단 요청이면 True."""
        while any(b["status"] in ("in_progress", "canceling") for b in targets_info):
            backoff = max((b.get("retrieve_failures", 0) for b in targets_info if b["status"] in ("in_progress", "canceling")), default=0)
            if _wait(min(MAX_POLL_SECONDS, poll_seconds * (2 ** min(backoff, 5))), cancel_event, sleep):
                return True
            for info in targets_info:
                if info["status"] not in ("in_progress", "canceling"):
                    continue
                try:
                    info.update({k: v for k, v in batch_client.retrieve(info["id"]).items() if k in ("status", "counts", "results_url", "expires_at", "ended_at")})
                except BatchApiError as exc:
                    info["retrieve_failures"] = info.get("retrieve_failures", 0) + 1
                    if (getattr(exc, "http_status", None) in RETRIEVE_PERMANENT_STATUSES
                            or info["retrieve_failures"] >= MAX_RETRIEVE_FAILURES):
                        abandon(info, exc)
                    else:
                        emit_progress(f"묶음 상태 조회 실패({info['retrieve_failures']}/{MAX_RETRIEVE_FAILURES}): {exc}")
                    continue
                info["retrieve_failures"] = 0
                if info["status"] == "ended":
                    settle(info)
            emit_progress()
        return False

    # --- 이어받기: 저장된 묶음은 다시 제출하지 않는다 ------------------------------------------------------
    def supersede(saved: Dict[str, Any]) -> None:
        """다시 쓰지 않는 저장된 묶음 — 이중 실행·과금을 막으려 취소한다. 취소를 확인하지 못하면 ID 를 남겨 다음 이어받기에서 다시 취소한다."""
        try:
            cancel_status = batch_client.cancel(saved["id"]).get("status")
        except BatchApiError:
            cancel_status = "cancel_failed"
        batches.append({"id": saved["id"], "status": "superseded", "cancel_status": cancel_status, "counts": {}, "custom_ids": {},
                        "fingerprints": {}, "submitted_at": saved.get("submitted_at"), "ended_at": None, "fallbacks": {}, "resumed": False})

    resumed: set = set()
    changed_requests = 0
    superseded: List[Dict[str, Any]] = []
    for saved in existing_batches or []:
        if not isinstance(saved, dict) or not isinstance(saved.get("id"), str):
            continue
        saved_prints = saved.get("fingerprints") if isinstance(saved.get("fingerprints"), dict) else {}
        known = [cid for cid in (saved.get("custom_ids") or {}) if cid in by_custom_id]
        # 제출 때와 판정 조건(파이프라인·평가 설정·입력)이 같은 질문만 이어받는다 — 다르면 새로 제출
        mapping = {cid: by_custom_id[cid] for cid in known if saved_prints.get(cid) == requests[by_custom_id[cid]]["fingerprint"]}
        changed_requests += len(known) - len(mapping)
        if not mapping:
            # 쓸 질문이 없는 묶음: 끝났거나 이미 취소를 확인한 것이 아니면 취소한다 (추적에서 빠져 계속 과금되지 않게)
            if saved.get("status") != "ended" and saved.get("cancel_status") not in ("canceling", "ended"):
                superseded.append(saved)
            continue
        info = {"id": saved["id"], "status": "in_progress", "counts": {}, "results_url": None, "expires_at": None,
                "created_at": None, "ended_at": None, "custom_ids": mapping, "submitted_at": saved.get("submitted_at"), "fallbacks": {},
                "resumed": True, "fingerprints": {cid: saved_prints[cid] for cid in mapping}}
        batches.append(info)
        resumed.update(mapping.values())
    remaining = [i for i in targets if i in requests and i not in resumed]

    cancelled = False
    try:
        for saved in superseded:
            supersede(saved)
        if changed_requests or superseded:
            emit_progress(f"판정 조건이 바뀐 {changed_requests}건은 저장된 묶음을 쓰지 않고 새로 제출"
                          + (f" · 쓰지 않는 묶음 {len(superseded)}개 취소" if superseded else ""))
        if resumed:
            emit_progress("저장된 묶음 이어받기")
            cancelled = poll_until_ended(batches)
        # --- pilot 1건: 묶음 경로가 동작하는지 먼저 확인한다 ----------------------------------------------
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
        if not cancelled and remaining:
            pilot = submit(remaining[:1])
            if pilot is None:
                # 제출 자체가 안 되면 전부 실시간으로 (설정·권한 문제를 한 번만 겪게)
                for i in remaining:
                    publish(i, realtime(i, FALLBACK_REASONS["submit_failed"]),
                            batch_info={"id": None, "custom_id": requests[i]["custom_id"], "result": "submit_failed", "fallback": FALLBACK_REASONS["submit_failed"]})
                remaining = []
            else:
                remaining = remaining[1:]
                cancelled = poll_until_ended([pilot])
        # --- 나머지를 묶음으로 전부 제출하고 함께 폴링 -------------------------------------------------------
        if not cancelled and remaining:
            open_batches = []
            for start in range(0, len(remaining), chunk):
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True   # 제출하지 않은 질문은 아래에서 SKIPPED, 이미 제출한 묶음만 취소한다
                    break
                indices = remaining[start:start + chunk]
                info = submit(indices)
                if info is None:
                    for i in indices:
                        publish(i, realtime(i, FALLBACK_REASONS["submit_failed"]),
                                batch_info={"id": None, "custom_id": requests[i]["custom_id"], "result": "submit_failed", "fallback": FALLBACK_REASONS["submit_failed"]})
                else:
                    open_batches.append(info)
            if not cancelled:
                cancelled = poll_until_ended(open_batches)
    finally:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
    if cancelled:
        cancel_open()
    for i in targets:
        if prep.records[i] is None:
            publish(i, _record(cases[i]["case_id"], JevStatus.SKIPPED, tuple(applies[i]["criteria"]), error=CANCEL_REASON))
    stored = [{k: copy.deepcopy(b.get(k)) for k in _STORED_KEYS} for b in batches]
    return _finish_batch(prep, batch, client, access_reason, dispatch="batch", batch_dispatch=BATCH_DISPATCH_VERSION, batches=stored)
