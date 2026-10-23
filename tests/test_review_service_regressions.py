"""실행 중인 평가 작업의 조회·내보내기를 보존하는 회귀 사례."""

import json
import threading
from types import SimpleNamespace

import pytest

from webapp import code_eval_service
from webapp.code_eval_service import CodeEvalService, JobCapacityError, MAX_JOBS


@pytest.fixture
def held_jobs(monkeypatch, tmp_path):
    workers = []

    class HeldThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            workers.append(self.target)

    # 서비스 작업만 보류하고 HTTP 서버 등의 실제 threading은 유지한다.
    monkeypatch.setattr(code_eval_service, "threading", SimpleNamespace(Lock=threading.Lock, Thread=HeldThread))
    return CodeEvalService(save_dir=tmp_path / "runs"), workers


def _start(service):
    return service.start_job({"question": "Evaluate this answer.", "answer": "Hello."})["job_id"]


def test_capacity_rejects_new_job_and_preserves_every_running_job(held_jobs):
    service, workers = held_jobs
    ids = [_start(service) for _ in range(MAX_JOBS)]

    with pytest.raises(JobCapacityError):
        _start(service)

    assert len(workers) == MAX_JOBS
    assert all(service.job_status(jid)["status"] == "RUNNING" for jid in ids)
    # 가장 오래된 작업도 완료 후 결과 조회 및 내보내기가 가능하다.
    workers[0]()
    assert service.job_result(ids[0])["summary"]["totals"]["evaluated"] == 1
    body, _, _ = service.export(ids[0], "json")
    assert json.loads(body)["cases"][0]["answer"] == "Hello."


def test_capacity_evicts_finished_job_even_when_older_jobs_are_running(held_jobs):
    service, workers = held_jobs
    ids = [_start(service) for _ in range(MAX_JOBS)]
    workers[-1]()
    assert service.job_status(ids[-1])["status"] == "COMPLETED"

    new_id = _start(service)

    assert service.job_status(new_id)["status"] == "RUNNING"
    assert all(service.job_status(jid)["status"] == "RUNNING" for jid in ids[:-1])
    with pytest.raises(FileNotFoundError):
        service.job_status(ids[-1])


