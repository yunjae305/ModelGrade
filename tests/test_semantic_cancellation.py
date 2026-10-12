"""Cancellation contracts with local judge doubles; no model/network calls."""

import threading
import time

from code_eval.engine import evaluate_batch, result_digest
from code_eval.jev_client import JevCall, JevClientConfig
from code_eval.jev_eval import evaluate_jev_batch, jev_after_code
from webapp.code_eval_service import CodeEvalService


def rows(count=6):
    return [{"id": f"t{i}", "conversation_id": "dialogue", "turn": i + 1,
             "question": f"  질문 {i}\r\n", "answer": f"  답변 {i}\r\n"} for i in range(count)]


def success(questions):
    return JevCall("OK", body={"model": "mock", "answers": {
        cid: {"choice": "PASS", "confidence": 0.8, "probabilities": {"PASS": 0.9, "FAIL": 0.1}}
        for cid in questions}}, attempts=1)


def test_cancel_before_resolution_skips_without_login_or_model_calls():
    token = threading.Event()
    token.set()
    starts = []
    def resolver():
        raise AssertionError("A cancelled batch must not resolve/login a judge")
    result = jev_after_code(evaluate_batch(rows()), resolver, cancel_event=token, on_start=starts.append)
    assert result["status"] == "PARTIAL" and not starts
    assert all(r["status"] == "SKIPPED" and r["attempts"] == 0 and r["decisions"] is None
               and r["error"].startswith("평가 중단") for r in result["records"])
    assert result["summary"]["J1"]["pending"] == 6


def test_parallel_cancel_preserves_completed_verdict_and_stops_unstarted_calls():
    token, second_entered = threading.Event(), threading.Event()
    batch = evaluate_batch(rows())
    digest = result_digest(batch["cases"])
    started, published = [], []

    class Judge:
        config = JevClientConfig()
        supports_cancellation = True
        calls = []
        def system_one(self, state, questions, *, cancel_event=None):
            assert cancel_event is token
            self.calls.append(state)
            if state["question"] == rows()[0]["question"]:
                assert second_entered.wait(3)
                token.set()
                return success(questions)  # Finished OK is retained even across stop.
            second_entered.set()
            assert token.wait(3)
            return JevCall("API_ERROR", error="terminated subprocess", attempts=1,
                           usage={"input_tokens": 7})

    judge = Judge()
    result = evaluate_jev_batch(batch, judge, workers=2, cancel_event=token,
                                on_start=started.append, on_record=lambda i, r: published.append(i))
    assert len(judge.calls) == 2 and sorted(started) == [0, 1]
    assert sorted(published) == list(range(6))
    assert result["records"][0]["status"] == "SUCCESS"
    assert result["records"][1]["usage"] == {"input_tokens": 7}
    assert all(r["status"] == "SKIPPED" and r["decisions"] is None for r in result["records"][1:])
    assert all(r["attempts"] == 0 for r in result["records"][2:])
    assert result["status"] == "PARTIAL" and result["conversations"]["incomplete"] == 1
    later = next(s for s in judge.calls if s["question"] == rows()[1]["question"])
    assert later["history"] == [{"turn": 1, "question": rows()[0]["question"], "answer": rows()[0]["answer"]}]
    assert result_digest(batch["cases"]) == digest


def test_legacy_judge_signature_works_and_stop_callback_prevents_next_request():
    token = threading.Event()
    class Judge:
        config = JevClientConfig()
        calls = 0
        def system_one(self, state, questions):
            self.calls += 1
            return success(questions)
    judge = Judge()
    result = evaluate_jev_batch(evaluate_batch(rows()), judge, workers=1, cancel_event=token,
                                on_record=lambda i, r: token.set() if i == 0 else None)
    assert judge.calls == 1
    assert [r["status"] for r in result["records"]] == ["SUCCESS"] + ["SKIPPED"] * 5


def test_service_shutdown_reaches_async_judge_preserves_partial_export_and_skips_extra_review(tmp_path):
    entered = threading.Event()
    class Judge:
        config = JevClientConfig()
        supports_cancellation = True
        calls = 0
        def system_one(self, state, questions, *, cancel_event=None):
            self.calls += 1
            entered.set()
            assert cancel_event.wait(3)
            return JevCall("API_ERROR", error="stopped", attempts=1)
    judge = Judge()
    def no_review():
        raise AssertionError("Cancellation must prevent supplemental model calls")
    service = CodeEvalService(save_dir=tmp_path / "saved", judge_resolver=no_review,
                              jev_resolver=lambda: (judge, judge.config, None))
    job = service.start_job({"cases": rows(8), "llm_review": True})
    assert entered.wait(3)
    service.shutdown()
    deadline = time.monotonic() + 3
    while service.job_status(job["job_id"])["status"] == "RUNNING" and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert service.job_status(job["job_id"])["status"] == "CANCELLED"
    result = service.job_result(job["job_id"])
    assert len(result["jev_eval"]["records"]) == 8 and judge.calls <= 4
    assert all(r["status"] == "SKIPPED" for r in result["jev_eval"]["records"])
    assert "llm_review" not in result and not service._evaluation_tokens
    assert service.export(job["job_id"], "json")[0]
    assert list((tmp_path / "saved").glob("*.json"))


def test_service_shutdown_before_work_does_not_resolve_judge(tmp_path):
    def resolver():
        raise AssertionError("No new judge resolution after shutdown")
    service = CodeEvalService(save_dir=tmp_path, jev_resolver=resolver)
    service.shutdown()
    result = service.evaluate({"cases": rows(2)})
    assert result["jev_eval"]["statuses"] == {"SKIPPED": 2}
    assert not service._evaluation_tokens
