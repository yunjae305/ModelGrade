"""Message Batches 디스패처(jev_batch)·Batches HTTP 클라이언트 — MockTransport 로 Batches API 와 실시간 Messages 를 함께 흉내 낸다. 유료 호출 없음.

확인하는 것: pilot 1건 → 묶음 제출 → 폴링 → custom_id 대응 → 만료·오류·pause_turn 은 실시간 재전송 → 중단 시 취소,
저장된 묶음 이어받기, 제출 실패 시 전부 실시간, jev_after_code 의 dispatch 분기. 판정 규칙은 실시간과 같다(같은 _record_from_call).
"""
import copy
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

    def __init__(self, *, polls_before_end=1, outcome=None, create_status=200):
        self.polls_before_end, self.outcome, self.create_status = polls_before_end, outcome or (lambda cid, params: "succeeded"), create_status
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
                                               custom_id_prefix="run1:gpt:", **kw)
    return batch, jev, events, sleeps


def test_pilot_then_chunks_poll_and_map_results_by_custom_id():
    fake = FakeBatches(polls_before_end=2)
    batch, jev, events, sleeps = run_batched(fake)
    assert [b["custom_ids"] for b in jev["batches"]] == [{"run1:gpt:0": 0}, {"run1:gpt:1": 1, "run1:gpt:2": 2}, {"run1:gpt:3": 3, "run1:gpt:4": 4}]
    assert all(b["status"] == "ended" and b["fallbacks"] == {} for b in jev["batches"]) and jev["dispatch"] == "batch"
    assert jev["batch_dispatch"] == jev_batch.BATCH_DISPATCH_VERSION and jev["status"] == "SUCCESS" and jev["completed"] == 5
    assert jev["client"]["dispatch"] == "batch" and jev["client"]["batch_questions"] == 2
    records = jev["records"]
    assert [r["status"] for r in records] == ["SUCCESS"] * 5 and all(r["model"] == "claude-opus-5-5" for r in records)
    assert records[0]["batch"] == {"id": "msgbatch_1", "custom_id": "run1:gpt:0", "result": "succeeded"}
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
    outcomes = {"run1:gpt:1": "expired", "run1:gpt:2": "errored", "run1:gpt:3": "pause_turn", "run1:gpt:4": "omit"}
    fake = FakeBatches(outcome=lambda cid, params: outcomes.get(cid, "succeeded"))
    batch, jev, events, _ = run_batched(fake)
    records = jev["records"]
    assert all(r["status"] == "SUCCESS" for r in records) and len(fake.realtime) == 4
    assert records[0]["batch"]["result"] == "succeeded" and "fallback" not in records[0]["batch"]
    assert records[1]["batch"] == {"id": "msgbatch_2", "custom_id": "run1:gpt:1", "result": "expired", "fallback": jev_batch.FALLBACK_REASONS["expired"]}
    assert records[2]["batch"]["result"] == "errored" and records[3]["batch"]["result"] == "pause_turn" and records[4]["batch"]["result"] == "missing"
    assert records[3]["batch"]["fallback"].startswith("검색 이어가기") and records[4]["batch"]["fallback"].startswith("묶음 결과에 이 질문이 없어")
    assert jev["batches"][1]["fallbacks"] == {"run1:gpt:1": jev_batch.FALLBACK_REASONS["expired"], "run1:gpt:2": jev_batch.FALLBACK_REASONS["errored"]}
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
    saved = client.batch_client().create([{"custom_id": "run1:gpt:0", "params": client.build_request(first["state"], first["questions"])[0]}])
    fake.calls.clear()
    jev = jev_batch.evaluate_jev_batch_batched(batch, client, cfg, custom_id_prefix="run1:gpt:", sleep=lambda s: None,
                                               existing_batches=[{"id": saved["id"], "custom_ids": {"run1:gpt:0": 0, "run1:gpt:9": 9}}])
    assert jev["batches"][0]["id"] == saved["id"] and jev["batches"][0]["resumed"] is True and jev["batches"][0]["custom_ids"] == {"run1:gpt:0": 0}
    created = [path for method, path in fake.calls if method == "POST" and path.endswith("/messages/batches")]
    assert len(created) == 3   # 이어받은 1건 외 4건: pilot 1 + 묶음 2 (2·1)
    assert [sorted(b["custom_ids"]) for b in jev["batches"][1:]] == [["run1:gpt:1"], ["run1:gpt:2", "run1:gpt:3"], ["run1:gpt:4"]]
    assert all(r["status"] == "SUCCESS" for r in jev["records"]) and jev["records"][0]["batch"]["id"] == saved["id"]


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
    jev = jev_after_code(batch, resolver=lambda **_: (client, cfg, None), dispatch_options={"custom_id_prefix": "r:", "sleep": sleeps.append})
    assert jev["dispatch"] == "batch" and jev["batches"][0]["custom_ids"] == {"r:0": 0} and not fake.realtime
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
