"""Comparison stop/drain races using local judges and mocked provider HTTP."""

import json
import threading
import time

import httpx
import pytest

from code_eval.jev_client import JevCall, JevClientConfig
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService


def until(check, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        threading.Event().wait(0.01)
    raise AssertionError("Expected worker state did not arrive")


def cases(count=8):
    return [{"id": f"q{i}", "question": f"질문 {i}", "answer": f"답변 {i}"} for i in range(count)]


def ok(questions):
    return JevCall("OK", body={"model": "mock-judge", "answers": {
        cid: {"choice": "PASS", "confidence": 0.8, "probabilities": {"PASS": 0.9, "FAIL": 0.1}}
        for cid in questions}}, attempts=1)


def make_service(tmp_path, judge=None, handler=None):
    def resolver():
        if judge is None:
            raise AssertionError("A cancelled collection must never resolve a judge")
        return judge, judge.config, None
    service = ComparisonService(CodeEvalService(save_dir=tmp_path / "eval", jev_resolver=resolver),
                                save_dir=tmp_path / "runs", semantic_workers=4,
                                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    service.configure("gpt", {"key": "fixture", "model": "fixture-model"})
    return service


def test_shutdown_preserves_late_collection_response_without_autoevaluation_or_next_call(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    requests = []
    def handler(request):
        requests.append(request)
        entered.set()
        assert release.wait(4)
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "  수집 원문\r\n"}]}]})
    service = make_service(tmp_path, handler=handler)
    dialogue = [{**case, "conversation_id": "dialogue", "turn": i + 1}
                for i, case in enumerate(cases(2))]
    rid = service.start({"cases": dialogue, "providers": ["gpt"], "auto_evaluate": True})["run_id"]
    try:
        assert entered.wait(3)
        writes = []
        original_save = service._save
        def save(run):
            writes.append(run["status"])
            original_save(run)
        monkeypatch.setattr(service, "_save", save)
        assert service.shutdown(timeout=0) is False
        assert writes == ["CANCELLED"]
        for _ in range(4):
            assert service.shutdown(timeout=0) is False
        assert writes == ["CANCELLED"]  # Drain polling never rewrites the large snapshot.
        stopped = service.result(rid)
        assert stopped["status"] == "CANCELLED" and stopped["progress"]["gpt"]["active"] is None
        saved = json.loads((tmp_path / "runs" / f"{rid}.json").read_text(encoding="utf-8"))
        assert saved["status"] == "CANCELLED"
        with pytest.raises(ValueError, match="종료 중"):
            service.start({"cases": cases(1)})
        with pytest.raises(ValueError, match="종료 중"):
            service.evaluate(rid, {})
        release.set()
        assert service.shutdown(timeout=3) is True
        result = service.result(rid)
        assert len(requests) == 1 and not result["evaluations"]
        assert result["datasets"]["gpt"][0]["answer"] == "  수집 원문\r\n"
        assert result["datasets"]["gpt"][1] is None
        assert result["status"] == "CANCELLED" and not service._cost_ledgers
        assert len(writes) > 1  # The late real response and final worker state are saved.
        assert service.shutdown() is True
    finally:
        release.set()


def test_cancelled_evaluation_keeps_success_and_restarts_with_a_fresh_token(tmp_path):
    class Judge:
        config = JevClientConfig()
        supports_cancellation = True
        def __init__(self):
            self.calls, self.tokens = [], []
            self.waiting = threading.Event()
        def system_one(self, state, questions, *, cancel_event=None):
            self.calls.append(state)
            self.tokens.append(cancel_event)
            if state["question"] == "질문 0":
                return ok(questions)
            self.waiting.set()
            assert cancel_event.wait(4)
            return JevCall("API_ERROR", error="terminated", attempts=1)
    judge = Judge()
    service = make_service(tmp_path, judge)
    rid = service.start({"cases": cases(), "auto_evaluate": True, "blind": True})["run_id"]
    assert judge.waiting.wait(3)
    until(lambda: service.result(rid)["evaluation_progress"]["aiona"]["semantic_done"] >= 1)
    old_token = judge.tokens[0]
    service.cancel(rid)
    until(lambda: service.status(rid)["status"] == "CANCELLED")
    result = service.result(rid)
    records = result["evaluations"]["aiona"]["jev_eval"]["records"]
    assert old_token.is_set() and len(judge.calls) <= 5
    assert records[0]["status"] == "SUCCESS"
    assert all(record["status"] == "SKIPPED" for record in records[1:])
    assert not result["evaluation_progress"]["aiona"]["active"]
    assert result["evaluation_log"]["status"] == "ERROR"

    class RestartedJudge:
        config = JevClientConfig()
        supports_cancellation = True
        calls = 0
        def system_one(self, state, questions, *, cancel_event=None):
            assert cancel_event is not old_token and not cancel_event.is_set()
            self.calls += 1
            return ok(questions)
    restarted = RestartedJudge()
    service.evaluator._jev_resolver = lambda: (restarted, restarted.config, None)
    service.evaluate(rid, {})
    until(lambda: service.status(rid)["status"] == "COMPLETED")
    assert restarted.calls == 8 and service.status(rid)["cancel_requested"] is False
    assert service.shutdown(timeout=3) is True
    assert service.status(rid)["status"] == "COMPLETED"


def test_shutdown_between_acceptance_and_worker_start_does_not_launch_collection(tmp_path, monkeypatch):
    service = make_service(tmp_path, handler=lambda _: pytest.fail("No collection may start after closing"))
    accepted, release = threading.Event(), threading.Event()
    original = service._launch_worker
    def paused(*args, **kwargs):
        accepted.set()
        assert release.wait(4)
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "_launch_worker", paused)
    results = []
    thread = threading.Thread(target=lambda: results.append(service.start({"cases": cases(2), "providers": ["gpt"]})))
    thread.start()
    try:
        assert accepted.wait(3)
        assert service.shutdown() is True
        release.set()
        thread.join(3)
        assert not thread.is_alive() and results[0]["status"] == "CANCELLED"
        assert not service._workers and not service._cost_ledgers and not service._cancel_events
    finally:
        release.set()


def test_forced_shutdown_reports_partial_cost_limit_once_and_late_worker_cannot_complete(tmp_path, capsys):
    entered, release = threading.Event(), threading.Event()
    class Judge:
        config = JevClientConfig()
        def system_one(self, state, questions):
            entered.set()
            assert release.wait(4)  # Simulate a legacy API request that cannot be interrupted.
            return ok(questions)
    service = make_service(tmp_path, Judge())
    rid = service.start({"cases": cases(1), "auto_evaluate": True})["run_id"]
    try:
        assert entered.wait(3)
        assert service.shutdown() is False
        service.finish_shutdown_costs()
        first = capsys.readouterr().out
        service.finish_shutdown_costs()
        assert "진행 중인 요청의 사용량·비용은 미확인" in first
        assert not capsys.readouterr().out
        release.set()
        assert service.shutdown(timeout=3) is True
        result = service.result(rid)
        assert result["status"] == "CANCELLED"
        assert result["evaluations"]["aiona"]["jev_eval"]["records"][0]["status"] == "SUCCESS"
        assert not capsys.readouterr().out
    finally:
        release.set()


def test_code_service_shutdown_rejects_new_async_jobs(tmp_path):
    service = CodeEvalService(save_dir=tmp_path)
    assert service.is_idle()
    service.shutdown()
    with pytest.raises(ValueError, match="종료 중"):
        service.start_job({"cases": cases(1)})
    assert service.is_idle() and not service._jobs


def test_shutdown_cannot_be_followed_by_a_late_active_criterion_event(tmp_path, monkeypatch):
    class Judge:
        config = JevClientConfig()
        supports_cancellation = True
        def system_one(self, state, questions, *, cancel_event=None):
            assert cancel_event.wait(4)
            return JevCall("API_ERROR", error="stopped", attempts=1)
    service = make_service(tmp_path, Judge())
    rid = service.start({"cases": cases(1)})["run_id"]
    stream = service.event_stream(rid)
    entered, release, stop_entered, stopped = (threading.Event() for _ in range(4))
    original_publish = stream.publish
    def publish(event):
        if event["type"] == "criterion_start" and not entered.is_set():
            entered.set()
            assert release.wait(4)
        original_publish(event)
    monkeypatch.setattr(stream, "publish", publish)
    starter = threading.Thread(target=lambda: service.evaluate(rid, {}))
    def stop():
        stop_entered.set()
        service.shutdown()
        stopped.set()
    stopper = threading.Thread(target=stop)
    starter.start()
    try:
        assert entered.wait(3)
        stopper.start()
        assert stop_entered.wait(3)
        # Old code allows shutdown to complete here, then publishes the held
        # start event after run_error. Atomic publication makes shutdown wait.
        assert not stopped.wait(0.1)
        release.set()
        assert stopped.wait(3)
        starter.join(3)
        stopper.join(3)
        assert service.shutdown(timeout=3) is True
        result = service.result(rid)
        assert result["status"] == "CANCELLED"
        assert result["evaluation_log"]["status"] == "ERROR"
        assert all(row["status"] != "RUNNING" and not row.get("active")
                   for row in result["evaluation_log"]["rows"].values())
    finally:
        release.set()
        service.shutdown()
        starter.join(3)
        if stopper.ident is not None:
            stopper.join(3)
