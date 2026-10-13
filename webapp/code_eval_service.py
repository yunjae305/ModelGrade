"""v16 코드 평가 웹 서비스 — CLI 와 같은 ``code_eval.engine`` 과 Jev 단계를 호출한다.

일괄 평가는 백그라운드 작업으로 돌리고 엔진의 실제 진행 수(done/total)를 돌려준다.
기존 LLM 재검토는 요청에 ``"llm_review": true`` 를 줄 때만 실행한다 (기본 흐름은 CODE + Jev).
"""

from __future__ import annotations

import json
import threading
import uuid
from threading import Event
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from code_eval.combined import combined_summary
from code_eval.config import ConfigError, load_aiona_config
from code_eval.core import versions
from code_eval.engine import evaluate_batch
from code_eval.io import parse_input_text, validate_json_numbers
from code_eval.jev_eval import jev_after_code
from code_eval.jev_criteria import describe as describe_jev
from code_eval.llm_client import resolve_client
from code_eval.llm_review import review_after_code
from code_eval.report_html import render_report
from code_eval.semantic_setup import semantic_setup
from code_eval.taxonomy import taxonomy

ROOT = Path(__file__).resolve().parent.parent
MAX_JOBS = 8


class JobCapacityError(ValueError):
    """모든 보관 슬롯에서 평가가 실행 중이므로 새 작업을 받을 수 없음."""


class CodeEvalService:
    def __init__(self, root: Path = ROOT, save_dir: Optional[Path] = None,
                 judge_resolver: Callable = resolve_client, jev_resolver: Optional[Callable] = None) -> None:
        self.root = root
        self.save_dir = save_dir if save_dir is not None else root / "data" / "code_eval_runs"
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()
        self._judge_resolver = judge_resolver
        self._jev_resolver = jev_resolver
        self._evaluation_tokens = {}
        self._shutting_down = False

    def shutdown(self) -> None:
        """Signal active and not-yet-started evaluations without deleting results."""
        with self._lock:
            self._shutting_down = True
            for token in self._evaluation_tokens:
                token.set()

    def is_idle(self) -> bool:
        with self._lock:
            return not self._evaluation_tokens

    def info(self) -> Dict[str, Any]:
        return {"versions": versions(), "aiona_config": self._config({}).summary(), "taxonomy": taxonomy(),
                "aiona_specialized": {"status": "PAUSED", "included_in_score": False},
                "semantic_setup": semantic_setup(env_path=self.root / ".env", injected=self._jev_resolver is not None)}

    # --- 입력 -----------------------------------------------------------------------
    @staticmethod
    def _cases(payload: Dict[str, Any]) -> List[Any]:
        if not isinstance(payload, dict):
            raise ValueError("요청 본문은 JSON 객체여야 합니다")
        if "input_text" in payload:
            if not isinstance(payload["input_text"], str):
                raise ValueError("input_text 는 문자열이어야 합니다")
            items, _ = parse_input_text(payload["input_text"], payload.get("input_name") or "업로드 파일")
            return items
        if isinstance(payload.get("cases"), list):
            return payload["cases"]
        if "question" in payload or "answer" in payload:
            return [{k: payload[k] for k in ("question", "answer", "case_id", "id", "system_id", "displayed_text",
                                             "source", "source_version", "reference", "conversation_id", "turn")
                     if k in payload}]
        raise ValueError("cases, input_text 또는 question/answer 가 필요합니다")

    @staticmethod
    def _config(payload: Dict[str, Any]):
        override = payload.get("aiona_config")
        if override is not None and not isinstance(override, dict):
            raise ValueError("AIONA 설정은 JSON 객체여야 합니다.")
        try:
            validate_json_numbers(override)
            cfg = load_aiona_config(override=override if override else None)
            # The workspace currently pauses both specialized criteria for every
            # entry point, including direct and background evaluation requests.
            # Keep the registry intact, but do not allow a stale client flag to
            # re-enable checks or create CONFIG_MISSING results in new runs.
            cfg.enabled = False
            cfg.raw_hash = 'paused-' + cfg.raw_hash
            return cfg
        except ConfigError as exc:
            raise ValueError(f"AIONA 설정 오류: {exc}") from exc

    # --- 동기 평가 (단건·소량) -----------------------------------------------------------
    def evaluate(self, payload: Dict[str, Any], observer: Optional[Callable] = None,
                 jev_resolver: Optional[Callable] = None, semantic_workers: int = 4,
                 event_sink: Optional[Callable] = None,
                 cancel_event: Optional[Event] = None, *, pipeline: Optional[str] = None,
                 semantic_context: Optional[Dict[str, Any]] = None,
                 dispatch_options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """``pipeline``(legacy|items, 없으면 SEMANTIC_PIPELINE 설정)과 ``semantic_context``({answer_tools, collected_at}, items 에서만
        쓴다)는 comparison_service 가 실행 시작 시점에 확정해 넘긴다 — 실행 도중 설정이 바뀌어도 한 실행 안에서는 같은 방식을 쓴다."""
        token = cancel_event if cancel_event is not None else Event()
        with self._lock:
            if self._shutting_down:
                token.set()
            self._evaluation_tokens[token] = self._evaluation_tokens.get(token, 0) + 1
        try:
            return self._evaluate(payload, observer, jev_resolver, semantic_workers, event_sink, token,
                                  pipeline=pipeline, semantic_context=semantic_context, dispatch_options=dispatch_options)
        finally:
            with self._lock:
                remaining = self._evaluation_tokens[token] - 1
                if remaining:
                    self._evaluation_tokens[token] = remaining
                else:
                    self._evaluation_tokens.pop(token)

    def _evaluate(self, payload, observer, jev_resolver, semantic_workers, event_sink, cancel_event, *,
                  pipeline=None, semantic_context=None, dispatch_options=None):
        cases = self._cases(payload)
        cfg = self._config(payload)
        if observer:
            observer({"phase": "CODE", "done": 0, "total": len(cases)})
        def code_progress(done, total):
            observer({"phase": "CODE", "done": done, "total": total})
        batch = evaluate_batch(cases, cfg, progress=code_progress if observer else None, on_event=event_sink)
        if observer:
            observer({"phase": "JEV", "batch": batch, "criteria": describe_jev()["criteria"]})
        def record(index, value):
            if observer:
                observer({"phase": "JEV", "index": index, "record": value})
            if event_sink:
                from .evaluation_events import record_events
                for event in record_events(batch["cases"][index], value, index):
                    event_sink(event)
        def started(index):
            observer({"phase": "JEV", "index": index, "started": True})
        batch["jev_eval"] = jev_after_code(batch, jev_resolver or self._jev_resolver,
                                          on_record=record if observer or event_sink else None,
                                          on_start=started if observer else None,
                                          workers=semantic_workers, on_event=event_sink, cancel_event=cancel_event,
                                          pipeline=pipeline, context=semantic_context, dispatch_options=dispatch_options)
        batch["combined"] = combined_summary(batch)
        if payload.get("llm_review", False) and not cancel_event.is_set():
            batch["llm_review"] = review_after_code(batch, self._judge_resolver)
        if observer:
            observer({"phase": "SAVE"})
        self._save(batch)
        return batch

    # --- 비동기 작업 --------------------------------------------------------------------
    def start_job(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        cases = self._cases(payload)
        cfg = self._config(payload)
        job_id = uuid.uuid4().hex[:12]
        job = {"job_id": job_id, "status": "RUNNING", "phase": "CODE", "done": 0,
               "total": len(cases), "error": None, "batch": None}
        with self._lock:
            if self._shutting_down:
                raise ValueError("서버가 종료 중이므로 새 평가 작업을 시작할 수 없습니다.")
            if len(self._order) >= MAX_JOBS:
                finished = next((jid for jid in self._order
                                 if self._jobs[jid]["status"] in ("COMPLETED", "ERROR", "CANCELLED")), None)
                if finished is None:
                    raise JobCapacityError(f"평가 작업 {MAX_JOBS}개가 실행 중입니다. 작업이 끝난 뒤 다시 시도해 주세요.")
                self._order.remove(finished)
                self._jobs.pop(finished)
            self._jobs[job_id] = job
            self._order.append(job_id)

        def work() -> None:
            try:
                token = Event()
                def observe(event):
                    phase = event["phase"]
                    if phase == "CODE":
                        job.update(phase=phase, done=event["done"], total=event["total"])
                    elif phase == "JEV":
                        if "batch" in event:
                            job.update(phase=phase, done=0, total=len(cases))
                        elif "record" in event:
                            job["done"] += 1
                    elif phase == "SAVE":
                        job["phase"] = phase
                batch = self.evaluate(payload, observer=observe, cancel_event=token)
                job["batch"] = batch
                job["status"] = "CANCELLED" if token.is_set() else "COMPLETED"
            except Exception as exc:  # 실행 오류는 작업 상태로 알린다
                job["status"] = "ERROR"
                job["error"] = f"{type(exc).__name__}: {exc}"

        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            with self._lock:
                self._jobs.pop(job_id, None)
                self._order.remove(job_id)
            raise
        # 즉시 끝난 작업이 다른 요청에 의해 정리돼도 접수 응답은 반환한다.
        return self._job_status(job)

    def _job(self, job_id: str) -> Dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            raise FileNotFoundError(job_id)
        return job

    def job_status(self, job_id: str) -> Dict[str, Any]:
        return self._job_status(self._job(job_id))

    @staticmethod
    def _job_status(job: Dict[str, Any]) -> Dict[str, Any]:
        return {k: job[k] for k in ("job_id", "status", "phase", "done", "total", "error")}

    def job_result(self, job_id: str) -> Dict[str, Any]:
        job = self._job(job_id)
        if job["status"] not in ("COMPLETED", "CANCELLED"):
            raise ValueError(f"작업이 아직 끝나지 않았습니다 ({job['status']})")
        return job["batch"]

    def export(self, job_id: str, fmt: str):
        batch = self.job_result(job_id)
        if fmt == "json":
            return json.dumps(batch, ensure_ascii=False, indent=2, allow_nan=False).encode(), "application/json", "results.json"
        if fmt == "jsonl":
            body = "".join(json.dumps(c, ensure_ascii=False, allow_nan=False) + "\n" for c in batch["cases"])
            return body.encode(), "application/x-ndjson", "results.jsonl"
        if fmt == "html":
            return render_report(batch).encode(), "text/html", "report.html"
        raise ValueError("지원하지 않는 형식")

    def _save(self, batch: Dict[str, Any]) -> None:
        try:
            self.save_dir.mkdir(parents=True, exist_ok=True)
            target = self.save_dir / f"code-eval-{batch['run']['run_id']}.json"
            if not target.exists():
                target.write_text(json.dumps(batch, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            batch["run"]["saved_to"] = target.relative_to(self.root).as_posix() if target.is_relative_to(self.root) else str(target)
        except (OSError, ValueError) as exc:
            batch["run"]["save_error"] = str(exc)
