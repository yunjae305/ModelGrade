"""Message Batches 디스패처(jev_batch)·Batches HTTP 클라이언트 — MockTransport 로 Batches API 와 실시간 Messages 를 함께 흉내 낸다. 유료 호출 없음.

확인하는 것: pilot 1건 → 묶음 제출 → 폴링 → custom_id 대응 → 만료·오류·pause_turn 은 실시간 재전송 → 중단 시 취소,
저장된 묶음 이어받기, 제출 실패 시 전부 실시간, jev_after_code 의 dispatch 분기. 판정 규칙은 실시간과 같다(같은 _record_from_call).
"""
import dataclasses
import json
import threading

import httpx
import pytest

from code_eval import engine, jev_batch
from code_eval.anthropic_batch_client import AnthropicBatchClient, BatchApiError
from code_eval.anthropic_semantic_client import FINAL_TOOL, AnthropicSemanticClient, AnthropicSemanticConfig
from code_eval.config import AionaConfig
from code_eval.jev_eval import CANCEL_REASON, evaluate_jev_batch, jev_after_code
from tests.test_jev_eval import CONTRADICTION, FORMAT_FAIL, INAPPROPRIATE, MISSING, NORMAL

CASES = [NORMAL, FORMAT_FAIL, INAPPROPRIATE, CONTRADICTION, MISSING]


def answers_for(params):
    tools = params.get("tools") or []
    schema = tools[-1]["input_schema"] if tools else {"required": []}
    # J5 는 사실 주장 없음 — 검색이 꺼진 설정에서 확인하지 못한 J5 는 판정 미완료(D-2)라 묶음 처리 흐름 확인과 섞지 않는다
    return {cid: {"choice": "NOT_APPLICABLE", "confidence": 0.8, "reason": "사실 주장 없음", "fact_check": {"basis": "none", "claims": []}}
            if cid == "J5" else {"choice": "PASS", "confidence": 0.8, "reason": f"{cid} 근거"} for cid in schema["required"]}


def message(params, *, stop_reason="tool_use"):
    content = [{"type": "tool_use", "id": "final", "name": FINAL_TOOL, "input": answers_for(params)}] if stop_reason == "tool_use" else []
    return {"model": "claude-opus-5-5", "stop_reason": stop_reason, "content": content,
            "usage": {"input_tokens": 100, "output_tokens": 50, "server_tool_use": {"web_search_requests": 0}}}


class FakeBatches:
    """Batches API 대역 — polls_before_end 번 조회한 뒤 ended. outcome(custom_id, params) 가 결과 줄 종류를 정한다."""

    def __init__(self, *, polls_before_end=1, outcome=None, create_status=200, retrieve_status=None, on_create=None):
        self.polls_before_end, self.outcome, self.create_status = polls_before_end, outcome or (lambda cid, params: "succeeded"), create_status
        self.retrieve_status = retrieve_status or (lambda batch_id: None)   # 상태 조회 HTTP 오류 주입
        self.on_create = on_create or (lambda count: None)
        self.batches = {}
        self.calls = []
        self.realtime = []
        self.lock = threading.Lock()

    def handler(self, request):
        with self.lock:
            path = request.url.path
            self.calls.append((request.method, path))
            if path.endswith("/messages"):
                params = json.loads(request.content)
                self.realtime.append(params)
                return httpx.Response(200, json=message(params))
            if path.endswith("/messages/batches") and request.method == "POST":
                if self.create_status != 200:
                    return httpx.Response(self.create_status, json={"error": "nope"})
                body = json.loads(request.content)
                batch_id = f"msgbatch_{len(self.batches) + 1}"
                self.batches[batch_id] = {"requests": body["requests"], "polls": 0, "status": "in_progress"}
                self.on_create(len(self.batches))
                return httpx.Response(200, json=self.describe(batch_id))
            if path.endswith("/cancel"):
                batch_id = path.split("/")[-2]
                self.batches[batch_id]["status"] = "canceling"
                return httpx.Response(200, json=self.describe(batch_id))
            if path.endswith("/results"):
                batch_id = path.split("/")[-2]
                lines = []
                for entry in self.batches[batch_id]["requests"]:
                    kind = self.outcome(entry["custom_id"], entry["params"])
                    if kind == "succeeded":
                        result = {"type": "succeeded", "message": message(entry["params"])}
                    elif kind == "pause_turn":
                        result = {"type": "succeeded", "message": message(entry["params"], stop_reason="pause_turn")}
                    elif kind == "errored":
                        result = {"type": "errored", "error": {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}}
                    elif kind == "omit":
                        continue
                    else:
                        result = {"type": kind}
                    lines.append(json.dumps({"custom_id": entry["custom_id"], "result": result}))
                lines.append("not json")
                return httpx.Response(200, text="\n".join(lines))
            batch_id = path.split("/")[-1]
            if self.retrieve_status(batch_id):
                return httpx.Response(self.retrieve_status(batch_id), json={"error": "retrieve"})
            info = self.batches[batch_id]
            info["polls"] += 1
            if info["polls"] >= self.polls_before_end and info["status"] != "canceling":
                info["status"] = "ended"
            return httpx.Response(200, json=self.describe(batch_id))

    def describe(self, batch_id):
        info = self.batches[batch_id]
        n = len(info["requests"])
        ended = info["status"] == "ended"
        return {"id": batch_id, "type": "message_batch", "processing_status": info["status"],
                "request_counts": {"processing": 0 if ended else n, "succeeded": n if ended else 0, "errored": 0, "canceled": 0, "expired": 0},
                "results_url": f"https://api.anthropic.com/v1/messages/batches/{batch_id}/results" if ended else None,
                "expires_at": "2026-10-14T00:00:00Z", "created_at": "2026-10-13T00:00:00Z", "ended_at": "2026-10-13T00:10:00Z" if ended else None}


def judge(fake, **config):
    cfg = AnthropicSemanticConfig(model="claude-opus-5-5", reasoning_effort="high", web_search="disabled", dispatch="batch",
                                  batch_questions=2, batch_poll_seconds=1, **config)
    return AnthropicSemanticClient(cfg, "secret-test-key", transport=httpx.MockTransport(fake.handler),
                                   source_fetcher=lambda url: {"status": "fetch_error"}), cfg


def run_batched(fake, cases=CASES, **kw):
    client, cfg = judge(fake)
    batch = engine.evaluate_batch(cases, AionaConfig())
    events = []
    sleeps = []
    jev = jev_batch.evaluate_jev_batch_batched(batch, client, cfg, on_event=events.append, sleep=sleeps.append,
                                               custom_id_prefix="run1_gpt_", **kw)
    return batch, jev, events, sleeps


def test_pilot_then_chunks_poll_and_map_results_by_custom_id():
    fake = FakeBatches(polls_before_end=2)
    batch, jev, events, sleeps = run_batched(fake)
    assert [b["custom_ids"] for b in jev["batches"]] == [{"run1_gpt_0": 0}, {"run1_gpt_1": 1, "run1_gpt_2": 2}, {"run1_gpt_3": 3, "run1_gpt_4": 4}]
    assert all(b["status"] == "ended" and b["fallbacks"] == {} for b in jev["batches"]) and jev["dispatch"] == "batch"
    assert jev["batch_dispatch"] == jev_batch.BATCH_DISPATCH_VERSION and jev["status"] == "SUCCESS" and jev["completed"] == 5
    assert jev["client"]["dispatch"] == "batch" and jev["client"]["batch_questions"] == 2
    records = jev["records"]
    assert [r["status"] for r in records] == ["SUCCESS"] * 5 and all(r["model"] == "claude-opus-5-5" for r in records)
    assert records[0]["batch"] == {"id": "msgbatch_1", "custom_id": "run1_gpt_0", "result": "succeeded"}
    assert records[3]["batch"]["id"] == "msgbatch_3" and records[0]["decisions"]["J1"]["decision"] == "PASS"
    assert records[0]["verification"]["provider_stop_reason"] == "tool_use" and records[0]["attempts"] == 1
    assert not fake.realtime   # 실시간 호출 없음
    assert sum(1 for method, path in fake.calls if method == "POST" and path.endswith("/messages/batches")) == 3
    assert sleeps and all(s == 1 for s in sleeps) and len(sleeps) >= 2   # 폴링 주기(batch_poll_seconds) 만큼 기다린다
    progress = [e for e in events if e["type"] == "batch_progress"]
    assert progress and progress[-1]["done"] == 5 and progress[-1]["submitted"] == 5 and progress[-1]["total"] == 5
    assert progress[0]["batches"][0]["id"] == "msgbatch_1" and "params" not in json.dumps(progress)   # 프롬프트는 이벤트에 없다
    # 실시간 결과와 판정·요약이 같다 (호출 방식만 다르다)
    realtime = evaluate_jev_batch(batch, judge(FakeBatches())[0], judge(FakeBatches())[1])
    assert realtime["dispatch"] == "realtime" and realtime["summary"] == jev["summary"]
    assert [r["decisions"] for r in realtime["records"]] == [r["decisions"] for r in records]


def test_expired_errored_missing_and_pause_turn_results_are_resent_in_realtime():
    outcomes = {"run1_gpt_1": "expired", "run1_gpt_2": "errored", "run1_gpt_3": "pause_turn", "run1_gpt_4": "omit"}
    fake = FakeBatches(outcome=lambda cid, params: outcomes.get(cid, "succeeded"))
    batch, jev, events, _ = run_batched(fake)
    records = jev["records"]
    assert all(r["status"] == "SUCCESS" for r in records) and len(fake.realtime) == 4
    assert records[0]["batch"]["result"] == "succeeded" and "fallback" not in records[0]["batch"]
    assert records[1]["batch"] == {"id": "msgbatch_2", "custom_id": "run1_gpt_1", "result": "expired", "fallback": jev_batch.FALLBACK_REASONS["expired"]}
    assert records[2]["batch"]["result"] == "errored" and records[3]["batch"]["result"] == "pause_turn" and records[4]["batch"]["result"] == "missing"
    assert records[3]["batch"]["fallback"].startswith("검색 이어가기") and records[4]["batch"]["fallback"].startswith("묶음 결과에 이 질문이 없어")
    assert jev["batches"][1]["fallbacks"] == {"run1_gpt_1": jev_batch.FALLBACK_REASONS["expired"], "run1_gpt_2": jev_batch.FALLBACK_REASONS["errored"]}
    assert any("실시간 재전송 2건" in e.get("note", "") for e in events)
    assert jev["status"] == "SUCCESS" and jev["completed"] == 5


def test_cancel_during_polling_cancels_open_batches_and_skips_the_rest():
    fake = FakeBatches(polls_before_end=50)
    cancel = threading.Event()

    def sleep(seconds):
        if len(fake.calls) >= 3:   # 제출 뒤 몇 번 조회한 다음 중단
            cancel.set()
    client, cfg = judge(fake)
    batch = engine.evaluate_batch(CASES, AionaConfig())
    jev = jev_batch.evaluate_jev_batch_batched(batch, client, cfg, cancel_event=cancel, sleep=sleep)
    assert jev["status"] == "PARTIAL" and all(r["status"] == "SKIPPED" and r["error"] == CANCEL_REASON for r in jev["records"])
    assert any(path.endswith("/cancel") for _, path in fake.calls)
    assert jev["batches"][0]["status"] == "canceling" and len(jev["batches"]) == 1   # pilot 만 제출된 채 중단


def test_resume_polls_saved_batches_without_resubmitting_and_submits_only_new_questions():
    fake = FakeBatches()
    client, cfg = judge(fake)
    # 저장된 묶음을 먼저 '제출'해 둔다 (서버 재시작 전 상태) — 디스패처가 만드는 것과 같은 요청 본문
    from code_eval.jev_eval import _case_request, _prepare_batch
    batch = engine.evaluate_batch(CASES, AionaConfig())
    prep = _prepare_batch(batch, client, cfg, pipeline="legacy", context=None)
    first = _case_request(NORMAL["question"], NORMAL["answer"], tuple(prep.applies[0]["criteria"]), None, None, client=client, cancel_event=None)
    saved = client.batch_client().create([{"custom_id": "run1_gpt_0", "params": client.build_request(first["state"], first["questions"])[0]}])
    fake.calls.clear()
    printed = jev_batch.request_fingerprint("legacy", cfg, first)
    jev = jev_batch.evaluate_jev_batch_batched(batch, client, cfg, custom_id_prefix="run1_gpt_", sleep=lambda s: None,
                                               existing_batches=[{"id": saved["id"], "custom_ids": {"run1_gpt_0": 0, "run1_gpt_9": 9},
                                                                  "fingerprints": {"run1_gpt_0": printed}}])
    assert jev["batches"][0]["id"] == saved["id"] and jev["batches"][0]["resumed"] is True and jev["batches"][0]["custom_ids"] == {"run1_gpt_0": 0}
    created = [path for method, path in fake.calls if method == "POST" and path.endswith("/messages/batches")]
    assert len(created) == 3   # 이어받은 1건 외 4건: pilot 1 + 묶음 2 (2·1)
    assert [sorted(b["custom_ids"]) for b in jev["batches"][1:]] == [["run1_gpt_1"], ["run1_gpt_2", "run1_gpt_3"], ["run1_gpt_4"]]
    assert all(r["status"] == "SUCCESS" for r in jev["records"]) and jev["records"][0]["batch"]["id"] == saved["id"]
    assert jev["batches"][0]["fingerprints"] == {"run1_gpt_0": printed} and len(jev["batches"][1]["fingerprints"]) == 1


def test_resume_resubmits_when_judging_conditions_changed():
    fake = FakeBatches()
    client, cfg = judge(fake)
    from code_eval.jev_eval import _case_request, _prepare_batch
    batch = engine.evaluate_batch(CASES, AionaConfig())
    prep = _prepare_batch(batch, client, cfg, pipeline="legacy", context=None)
    first = _case_request(NORMAL["question"], NORMAL["answer"], tuple(prep.applies[0]["criteria"]), None, None, client=client, cancel_event=None)
    saved = client.batch_client().create([{"custom_id": "run1_gpt_0", "params": client.build_request(first["state"], first["questions"])[0]}])
    high = jev_batch.request_fingerprint("legacy", cfg, first)
    # 묶음 크기·조회 주기는 지문에 들지 않고, 평가 설정(effort)이 바뀌면 지문이 다르다
    assert jev_batch.request_fingerprint("legacy", dataclasses.replace(cfg, batch_questions=3, batch_poll_seconds=5), first) == high
    low_cfg =AnthropicSemanticConfig(model="claude-opus-5-5", reasoning_effort="low", web_search="disabled", dispatch="batch",
                                      batch_questions=2, batch_poll_seconds=1)
    low_client = AnthropicSemanticClient(low_cfg, "secret-test-key", transport=httpx.MockTransport(fake.handler))
    assert jev_batch.request_fingerprint("legacy", low_cfg, first) != high
    events = []
    jev = jev_batch.evaluate_jev_batch_batched(batch, low_client, low_cfg, custom_id_prefix="run1_gpt_", sleep=lambda s: None,
                                               on_event=events.append,
                                               existing_batches=[{"id": saved["id"], "custom_ids": {"run1_gpt_0": 0}, "fingerprints": {"run1_gpt_0": high}}])
    # 쓰지 않는 진행 중 묶음은 취소하고 ID 를 기록에 남긴다 (추적에서 빠져 계속 과금되지 않게)
    assert jev["batches"][0]["id"] == saved["id"] and jev["batches"][0]["status"] == "superseded"
    assert jev["batches"][0]["cancel_status"] == "canceling" and any(path.endswith(f"{saved['id']}/cancel") for _, path in fake.calls)
    assert all(not b.get("resumed") for b in jev["batches"]) and sorted(jev["batches"][1]["custom_ids"]) == ["run1_gpt_0"]
    assert jev["client"]["reasoning_effort"] == "low" and any("판정 조건이 바뀐 1건" in e.get("note", "") for e in events)
    # 취소를 이미 확인한 묶음은 다음 이어받기에서 다시 취소하지 않는다
    calls = len(fake.calls)
    jev_batch.evaluate_jev_batch_batched(batch, low_client, low_cfg, custom_id_prefix="run1_gpt_", sleep=lambda s: None,
                                         existing_batches=[jev["batches"][0]])
    assert not any(path.endswith(f"{saved['id']}/cancel") for _, path in fake.calls[calls:])
    # 지문이 없는 옛 기록도 이어받지 않는다 (조건을 확인할 수 없음)
    jev = jev_batch.evaluate_jev_batch_batched(batch, client, cfg, custom_id_prefix="run1_gpt_", sleep=lambda s: None,
                                               existing_batches=[{"id": saved["id"], "custom_ids": {"run1_gpt_0": 0}}])
    assert all(not b.get("resumed") for b in jev["batches"])


def test_custom_ids_must_follow_the_batches_api_format():
    fake = FakeBatches()
    client, _ = judge(fake)
    batches = client.batch_client()
    for bad in ("run:gpt:0", "x" * 65, ""):
        with pytest.raises(ValueError):
            batches.create([{"custom_id": bad, "params": {}}])
    with pytest.raises(ValueError):
        batches.create([{"custom_id": "a_1", "params": {}}, {"custom_id": "a_1", "params": {}}])
    assert not any(method == "POST" for method, _ in fake.calls)   # 규격 위반은 보내지 않는다


@pytest.mark.parametrize("status, limit", [(404, 1), (500, jev_batch.MAX_RETRIEVE_FAILURES)])
def test_retrieve_errors_give_up_and_resend_in_realtime(status, limit):
    fake = FakeBatches(retrieve_status=lambda batch_id: status)
    batch, jev, events, sleeps = run_batched(fake)
    records = jev["records"]
    assert all(r["status"] == "SUCCESS" and r["batch"]["result"] == "retrieve_failed" for r in records) and len(fake.realtime) == 5
    assert [b["status"] for b in jev["batches"]] == ["retrieve_failed"] * 3
    retrieves = [path for method, path in fake.calls if method == "GET" and path.rsplit("/", 1)[-1].startswith("msgbatch_")]
    assert len(retrieves) == 3 * limit   # 영구 오류는 1회, 일시 오류는 연속 5회에서 멈춘다
    cancels = [path for _, path in fake.calls if path.endswith("/cancel")]
    assert len(cancels) == (0 if status == 404 else 3)   # 일시 오류로 포기한 묶음은 이중 과금을 막으려 취소를 시도한다
    if status == 500:
        assert sleeps[:5] == [1, 2, 4, 8, 16]   # 실패가 이어지면 조회 간격을 늘린다


def test_cancel_after_a_submission_does_not_submit_the_remaining_chunks():
    cancel = threading.Event()
    fake = FakeBatches(polls_before_end=1, on_create=lambda count: cancel.set() if count == 2 else None)
    client, cfg = judge(fake)
    batch = engine.evaluate_batch(CASES, AionaConfig())
    jev = jev_batch.evaluate_jev_batch_batched(batch, client, cfg, cancel_event=cancel, sleep=lambda s: None, custom_id_prefix="run1_gpt_")
    created = [path for method, path in fake.calls if method == "POST" and path.endswith("/messages/batches")]
    assert len(created) == 2 and [sorted(b["custom_ids"]) for b in jev["batches"]] == [["run1_gpt_0"], ["run1_gpt_1", "run1_gpt_2"]]
    assert any(path.endswith("msgbatch_2/cancel") for _, path in fake.calls)
    assert [r["status"] for r in jev["records"][3:]] == ["SKIPPED", "SKIPPED"]


def test_pause_turn_fallback_keeps_the_batch_request_usage():
    fake = FakeBatches(outcome=lambda cid, params: "pause_turn" if cid == "run1_gpt_0" else "succeeded")
    batch, jev, events, _ = run_batched(fake)
    record = jev["records"][0]
    assert record["batch"]["result"] == "pause_turn" and record["status"] == "SUCCESS"
    assert record["attempts"] == 2 and record["usage"]["input_tokens"] == 200 and record["usage"]["output_tokens"] == 100
    parts = record["verification"]["usage_requests"]
    assert len(parts) == 2 and parts[0]["dispatch"] == "batch" and "dispatch" not in parts[1]


def test_cancel_before_the_realtime_resend_still_keeps_the_batch_usage():
    fake = FakeBatches(outcome=lambda cid, params: "pause_turn")
    client, cfg = judge(fake)
    batch = engine.evaluate_batch(CASES, AionaConfig())
    cancel = threading.Event()

    def on_event(event):   # 실시간 재전송을 알리는 순간 중단 요청
        if event.get("type") == "batch_progress" and any(b.get("fallbacks") for b in event.get("batches") or []):
            cancel.set()
    jev = jev_batch.evaluate_jev_batch_batched(batch, client, cfg, custom_id_prefix="run1_gpt_", cancel_event=cancel, on_event=on_event,
                                               sleep=lambda s: None)
    record = jev["records"][0]
    assert record["status"] == "SKIPPED" and record["batch"]["result"] == "pause_turn" and not fake.realtime
    assert record["attempts"] == 1 and record["usage"]["input_tokens"] == 100
    assert [part["dispatch"] for part in record["verification"]["usage_requests"]] == ["batch"]


def test_submit_failure_falls_back_to_realtime_for_everything():
    fake = FakeBatches(create_status=500)
    batch, jev, events, _ = run_batched(fake, cases=CASES[:3])
    assert all(r["status"] == "SUCCESS" and r["batch"]["result"] == "submit_failed" and r["batch"]["id"] is None for r in jev["records"])
    assert len(fake.realtime) == 3 and jev["batches"] == []
    assert any("묶음 제출 실패" in e.get("note", "") for e in events)


def test_jev_after_code_routes_by_dispatch_setting(monkeypatch):
    fake = FakeBatches()
    client, cfg = judge(fake)
    batch = engine.evaluate_batch(CASES[:2], AionaConfig())
    sleeps = []
    jev = jev_after_code(batch, resolver=lambda **_: (client, cfg, None), dispatch_options={"custom_id_prefix": "r_", "sleep": sleeps.append})
    assert jev["dispatch"] == "batch" and jev["batches"][0]["custom_ids"] == {"r_0": 0} and not fake.realtime
    realtime_client, realtime_cfg = judge(fake, dispatch="realtime") if False else (None, None)
    plain = AnthropicSemanticConfig(model="claude-opus-5-5", web_search="disabled")
    plain_client = AnthropicSemanticClient(plain, "secret-test-key", transport=httpx.MockTransport(fake.handler))
    jev = jev_after_code(batch, resolver=lambda **_: (plain_client, plain, None))
    assert jev["dispatch"] == "realtime" and "batches" not in jev and len(fake.realtime) == 2
    assert jev_after_code(batch, resolver=lambda **_: (None, cfg, "키 없음"))["records"][0]["status"] == "NOT_CONFIGURED"


def test_batch_client_contract_and_error_handling():
    fake = FakeBatches()
    client = AnthropicBatchClient("https://api.anthropic.com/v1", "secret-test-key", transport=httpx.MockTransport(fake.handler))
    with pytest.raises(ValueError):
        client.create([])
    with pytest.raises(ValueError):
        client.create([{"custom_id": 1, "params": {}}])
    created = client.create([{"custom_id": "a", "params": {"model": "m"}}, {"custom_id": "b", "params": {"model": "m"}}])
    assert created["id"] == "msgbatch_1" and created["status"] == "in_progress" and created["counts"]["processing"] == 2 and created["results_url"] is None
    polled = client.retrieve("msgbatch_1")
    assert polled["status"] == "ended" and polled["counts"]["succeeded"] == 2 and polled["results_url"].endswith("/results")
    rows = list(client.results(polled["results_url"]))
    assert [r["custom_id"] for r in rows] == ["a", "b", None] and rows[0]["type"] == "succeeded" and rows[0]["message"]["model"] == "claude-opus-5-5"
    assert rows[2] == {"custom_id": None, "type": "invalid", "error": "결과 줄이 JSON 이 아님"}
    assert client.cancel("msgbatch_1")["status"] == "canceling"
    broken = AnthropicBatchClient("https://api.anthropic.com/v1", "k", transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})))
    with pytest.raises(BatchApiError) as info:
        broken.create([{"custom_id": "a", "params": {}}])
    assert info.value.http_status == 503 and "503" in str(info.value) and "k" != str(info.value)
    with pytest.raises(ValueError):
        AnthropicBatchClient("https://api.anthropic.com/v1", "")
    assert AnthropicBatchClient.summary({"id": "x", "processing_status": "weird"})["status"] == "unknown"


def test_batch_requests_carry_attachments_like_realtime():
    """묶음 요청도 실시간과 같은 입력 — 첨부파일(J8)을 평가자에게 보낸다 (Codex 리뷰: 빠지면 받지 않은 파일을 판정)."""
    fake = FakeBatches()
    case = dict(NORMAL, attachments=[{"name": "견적서.txt", "type": "txt", "content": "합계 1,250,000원"}])
    batch, jev, events, sleeps = run_batched(fake, cases=[case])
    params = fake.batches["msgbatch_1"]["requests"][0]["params"]
    text = json.dumps(params["messages"], ensure_ascii=False)
    assert "견적서.txt" in text and "합계 1,250,000원" in text and jev["records"][0]["status"] == "SUCCESS"
