"""Local comparison workspace: preserve prompts, answer text and provider response JSON.

Keys loaded from local environment or UI stay in server memory. Collection never retries a paid request
automatically. Failed, blocked and truncated responses remain explicit outcomes.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx

from code_eval.core import LIMITS
from code_eval.common_adjudication import resolved_case
from code_eval import jev_eval
from code_eval.engine import _conversation_of
from code_eval.io import validate_json_numbers
from code_eval.llm_client import env_value
from .code_eval_service import CodeEvalService, JobCapacityError, ROOT
from .blind_comparison import blind_plan
from .dataset_import import import_text
from .evaluation_events import EvaluationEvents, code_event, record_events
from . import semantic_union
from .version_registry import VersionRegistry, evaluation_profile, fingerprint, validate_release
from .activity_store import clean_text, event_record

PROVIDERS = {"gemini": "Gemini", "claude": "Claude", "gpt": "GPT"}
PROVIDER_ENV = {
    "gemini": ("GOOGLE_API_KEY", "GEMINI_MODEL"),
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"),
    "gpt": ("OPENAI_API_KEY", "OPENAI_MODEL"),
}
MAX_RUNS = 8
DEFAULT_COLLECTION_WORKERS = 3
MAX_COLLECTION_WORKERS = 4
DEFAULT_SEMANTIC_WORKERS = 12
MAX_SEMANTIC_WORKERS = 16
DEFAULT_COMPARISON_DATASET = ROOT / "data" / "code_eval" / "AIONA_winz_Q165_Q100_multiturn65_v1.json"
PUBLIC_COMPARISON_DATASET = ROOT / "examples" / "questions.sample.json"


class CostLedger:
    """API 비용은 계산하지 않는다. 실행이 끝났는지 표시하는 수명 주기 자리만 유지한다."""

    def record_collection(self, provider, configured_model, raw=None):
        pass

    def wrap_resolver(self, resolver):
        return resolver

    def finish(self, run_id, status):
        pass


def normalize_dataset(payload):
    if isinstance(payload, dict) and 'input_text' in payload:
        items, _ = import_text(payload['input_text'], payload.get('input_name') or '업로드 파일')
    else:
        items = CodeEvalService._cases(payload)
    if not 0 < len(items) <= LIMITS["batch_cases"]:
        raise ValueError(f"질문은 1~{LIMITS['batch_cases']:,}개여야 합니다.")
    cases, conversations = [], {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"{index + 1}번 항목은 JSON 객체여야 합니다.")
        validate_json_numbers(item)
        if item.get("__parse_error__"):
            raise ValueError(item["__parse_error__"])
        for field in ("question", "answer"):
            if not isinstance(item.get(field), str):
                raise ValueError(f"{index + 1}번 항목의 {field}는 문자열이어야 합니다.")
            if len(item[field]) > LIMITS[field + "_chars"]:
                raise ValueError(f"{index + 1}번 항목의 {field}가 입력 한도를 초과했습니다.")
        if not item["question"].strip():
            raise ValueError(f"{index + 1}번 질문이 비어 있습니다.")
        conversation, error = _conversation_of(item)
        if error:
            raise ValueError(f"{index + 1}번 항목: {error}")
        case = copy.deepcopy(item)
        case["case_id"] = str(item.get("case_id") or item.get("id") or item.get("question_id") or f"Q{index + 1:03d}")
        case["system_id"] = "aiona"
        if conversation:
            case["conversation_id"], case["turn"] = conversation["id"], conversation["turn"]
            conversations.setdefault(conversation["id"], []).append(conversation["turn"])
        cases.append(case)
    # Each model builds its own history. A missing/duplicate turn makes that
    # comparison ambiguous, so collection is blocked before making any calls.
    for cid, turns in conversations.items():
        if sorted(turns) != list(range(1, len(turns) + 1)):
            raise ValueError(f"대화 {cid}: turn이 1부터 연속되어야 하며 중복될 수 없습니다.")
    return cases


def import_dataset(payload):
    if isinstance(payload, dict) and 'input_text' in payload:
        rows, info = import_text(payload['input_text'], payload.get('input_name') or '업로드 파일')
        cases = normalize_dataset({'cases': rows})
    else:
        cases = normalize_dataset(payload)
        info = {'format': 'JSON', 'converted': False, 'notes': []}
    return {'cases': cases, 'import': info}


class ProviderError(Exception):
    pass


def request_answer(provider, config, messages, max_tokens, client, *, usage_observer=None):
    """No answer strip, newline replacement, Markdown rendering or JSON-mode prompt."""
    observed = False

    def record_usage(raw):
        nonlocal observed
        if observed:
            return
        observed = True
        if usage_observer is not None:
            try:
                usage_observer(raw)
            except Exception:
                pass  # Terminal cost reporting must never change a model result.

    key, model = config["key"], config["model"]
    if provider == "gpt":
        url = "https://api.openai.com/v1/responses"
        headers = {"Authorization": f"Bearer {key}"}
        body = {"model": model, "input": messages, "max_output_tokens": max_tokens, "store": False}
    elif provider == "claude":
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    else:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": key}
        body = {"contents": [{"role": "model" if m["role"] == "assistant" else "user",
                               "parts": [{"text": m["content"]}]} for m in messages],
                "generationConfig": {"maxOutputTokens": max_tokens}}
    try:
        try:
            response = client.post(url, headers=headers, json=body)
        except httpx.HTTPError:
            # Exception strings may include URLs or headers; don't expose them.
            raise ProviderError("네트워크 요청 실패 또는 시간 초과. 자동 재시도하지 않았습니다.") from None
        if response.status_code >= 400:
            reason = {400: "모델 ID·출력 토큰 한도 등 요청 설정을 확인해 주세요.",
                      401: "API 키를 확인해 주세요.", 403: "키 또는 모델 접근 권한을 확인해 주세요.",
                      404: "모델 ID를 확인해 주세요.", 429: "사용량·요청 한도를 확인해 주세요."}.get(
                          response.status_code, "모델 제공자에서 요청을 처리하지 못했습니다.")
            raise ProviderError(f"HTTP {response.status_code}: {reason}")
        try:
            raw = response.json()
            record_usage(raw)  # Keep usage even if answer parsing fails below.
            validate_json_numbers(raw)
            if provider == "gpt":
                blocks = [b for m in raw.get("output", []) if m.get("type") == "message"
                          for b in m.get("content", [])]
                texts = [b["text"] if b.get("type") == "output_text" else b["refusal"]
                         for b in blocks if b.get("type") in ("output_text", "refusal")]
                finish = raw.get("status", "unknown")
                complete = finish == "completed"
            elif provider == "claude":
                texts = [b["text"] for b in raw.get("content", []) if b.get("type") == "text"]
                finish = raw.get("stop_reason", "unknown")
                complete = finish in ("end_turn", "stop_sequence", "refusal")
            else:
                candidate = (raw.get("candidates") or [{}])[0]
                texts = [p["text"] for p in candidate.get("content", {}).get("parts", [])
                         if "text" in p and not p.get("thought")]
                finish = candidate.get("finishReason", "blocked")
                complete = finish == "STOP"
            if not all(isinstance(t, str) for t in texts):
                raise ValueError("text is not a string")
            answer = "".join(texts)
            return {"answer": answer, "raw": raw, "finish_reason": finish,
                    "status": "SUCCESS" if complete and texts else "INCOMPLETE",
                    "error": None if complete and texts else f"응답 미완료 ({finish}). 원본 응답은 보존했습니다."}
        except (ValueError, TypeError, AttributeError, KeyError):
            raise ProviderError("모델 응답 형식을 해석하지 못했습니다.") from None
    finally:
        record_usage(None)  # Network/HTTP/JSON failures still represent one request.


class ComparisonService:
    def __init__(self, evaluator, save_dir=None, client_factory=None, registry_path=None,
                 *, semantic_workers=DEFAULT_SEMANTIC_WORKERS, collection_workers=DEFAULT_COLLECTION_WORKERS):
        self.evaluator = evaluator
        self.save_dir = Path(save_dir) if save_dir is not None else ROOT / "data" / "comparison_runs"
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=180, follow_redirects=False))
        self._keys, self._runs = {}, {}
        self._streams = {}
        self._cost_ledgers = {}  # Runtime only: never add costs to serialized runs.
        self._cancel_events = {}
        self._workers = {}
        self._closing = False
        self.semantic_workers = self._semantic_worker_count(semantic_workers)
        self.collection_workers = self._collection_worker_count(collection_workers)
        self._lock = threading.RLock()
        default_registry = (self.save_dir.parent if save_dir is not None else ROOT / 'data') / 'aiona_versions.json'
        self.version_registry = VersionRegistry(Path(registry_path) if registry_path is not None else default_registry)

    def _launch_worker(self, run, token, target, args=()):
        """Register/start under one lock so shutdown cannot miss a new worker."""
        run_id = run["run_id"]
        def work():
            try:
                target(*args)
            finally:
                with self._lock:
                    threads = self._workers.get(run_id, set())
                    threads.discard(threading.current_thread())
                    if not threads:
                        self._workers.pop(run_id, None)
                        active_token = self._cancel_events.pop(run_id, None)
                        if active_token is not None and active_token.is_set():
                            self._mark_cancelled(run)
                            self._save(run)
                            self._finish_costs(run, self._cost_ledgers.get(run_id))
        work.__name__ = getattr(target, "__name__", "comparison_worker")
        with self._lock:
            if self._closing or token.is_set():
                token.set()
                self._mark_cancelled(run)
                self._save(run)
                self._finish_costs(run, self._cost_ledgers.get(run_id))
                if not self._workers.get(run_id):
                    self._cancel_events.pop(run_id, None)
                return
            thread = threading.Thread(target=work, daemon=True)
            self._workers.setdefault(run_id, set()).add(thread)
            try:
                thread.start()
            except Exception:
                self._workers[run_id].discard(thread)
                if not self._workers[run_id]:
                    self._workers.pop(run_id, None)
                raise

    def _mark_cancelled(self, run):
        run.update(status="CANCELLED", cancel_requested=True)
        self._clear_inflight(run)
        for progress in run.get("progress", {}).values():
            if progress.get("stage") != "COMPLETED":
                progress.update(stage="CANCELLED", active=None)
        for progress in run.get("evaluation_progress", {}).values():
            if progress.get("stage") != "COMPLETED":
                progress.update(stage="CANCELLED", active=[])
        stream = self._streams.get(run["run_id"])
        if stream and stream.state["status"] not in ("COMPLETED", "ERROR"):
            stream.publish({"type": "run_error", "reason": "평가 중단 요청. 완료된 원문과 결과는 보존합니다."})
        run["revision"] = run.get("revision", 0) + 1

    def shutdown(self, timeout=0):
        """Stop new work, preserve partial output, and optionally wait for workers."""
        deadline = time.monotonic() + max(0, timeout)
        with self._lock:
            self._closing = True
            for run_id, token in self._cancel_events.items():
                run = self._runs.get(run_id)
                if run is not None and (run["status"] in ("COLLECTING", "EVALUATING")
                                        or run["status"] == "READY" and run.get("auto_evaluate")
                                        or run.get("cancel_requested") and self._workers.get(run_id)):
                    already_stopped = token.is_set() and run["status"] == "CANCELLED"
                    token.set()
                    if not already_stopped:
                        run.setdefault('_activity_cancel_actor', 'system')
                        self._mark_cancelled(run)
                        self._save(run)
            threads = [thread for group in self._workers.values() for thread in group]
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if thread is not threading.current_thread():
                thread.join(remaining)
        with self._lock:
            drained = not any(thread.is_alive() for group in self._workers.values() for thread in group)
            for run_id, ledger in list(self._cost_ledgers.items()):
                run = self._runs.get(run_id)
                if run is not None and not self._workers.get(run_id):
                    self._finish_costs(run, ledger)
            return drained

    def finish_shutdown_costs(self):
        """Close cancelled runs' ledgers before the server exits with live calls."""
        with self._lock:
            unfinished = [(rid, ledger) for rid, ledger in self._cost_ledgers.items()
                          if self._runs.get(rid, {}).get("status") == "CANCELLED"]
            for run_id, ledger in unfinished:
                self._finish_costs(self._runs[run_id], ledger)

    @staticmethod
    def _collection_worker_count(value):
        if type(value) is not int or not 1 <= value <= MAX_COLLECTION_WORKERS:
            raise ValueError(f"COMPARISON_COLLECTION_WORKERS는 1~{MAX_COLLECTION_WORKERS} 사이의 정수여야 합니다.")
        return value

    @staticmethod
    def _collection_chains(cases):
        """Independent questions are single-item chains; dialogue turns stay ordered."""
        order = sorted(range(len(cases)), key=lambda i: (
            str(cases[i].get("conversation_id", f"~single-{i:08d}")), cases[i].get("turn", 1)))
        chains, conversations = [], {}
        for index in order:
            cid = cases[index].get("conversation_id")
            if cid is None:
                chains.append([index])
            else:
                if cid not in conversations:
                    conversations[cid] = []
                    chains.append(conversations[cid])
                conversations[cid].append(index)
        return chains

    @staticmethod
    def _semantic_worker_count(value):
        if type(value) is not int or not 4 <= value <= MAX_SEMANTIC_WORKERS:
            raise ValueError(f"COMPARISON_SEMANTIC_WORKERS는 4~{MAX_SEMANTIC_WORKERS} 사이의 정수여야 합니다.")
        return value

    def _finish_costs(self, run, ledger):
        """Called under _lock at a terminal transition, never on reads or saves."""
        if ledger is None or self._cost_ledgers.get(run['run_id']) is not ledger:
            return
        self._cost_ledgers.pop(run['run_id'], None)
        try:
            ledger.finish(run['run_id'], run['status'])
        except Exception:
            pass  # Reporting cannot prevent a completed result from being used.

    def load_environment(self, *, environment=None, env_path=None):
        """Explicit startup-only loading; never return or serialize credentials.

        Both key and model are required. UI overrides/removal remain effective
        for this process; polling must never silently reload a cleared key.
        """
        path = Path(env_path) if env_path is not None else ROOT / ".env"
        worker_value = env_value("COMPARISON_SEMANTIC_WORKERS", environment, path)
        workers = self.semantic_workers
        if worker_value is not None:
            if not re.fullmatch(r"[0-9]+", worker_value):
                raise ValueError("COMPARISON_SEMANTIC_WORKERS는 정수여야 합니다.")
            workers = self._semantic_worker_count(int(worker_value))
        collection_value = env_value("COMPARISON_COLLECTION_WORKERS", environment, path)
        collection_workers = self.collection_workers
        if collection_value is not None:
            if not re.fullmatch(r"[0-9]+", collection_value):
                raise ValueError("COMPARISON_COLLECTION_WORKERS는 정수여야 합니다.")
            collection_workers = self._collection_worker_count(int(collection_value))
        configs = {}
        for provider, (key_var, model_var) in PROVIDER_ENV.items():
            key = env_value(key_var, environment, path)
            model = env_value(model_var, environment, path)
            if key and model:
                configs[provider] = {"key": key, "model": model}
        for provider, config in configs.items():
            self.configure(provider, config)
        self.semantic_workers = workers
        self.collection_workers = collection_workers

    def providers(self):
        with self._lock:
            return {p: {"name": name, "configured": p in self._keys,
                        "model": self._keys.get(p, {}).get("model", "")} for p, name in PROVIDERS.items()}

    def configure(self, provider, payload):
        if provider not in PROVIDERS or not isinstance(payload, dict):
            raise ValueError("지원하지 않는 모델 제공자입니다.")
        with self._lock:
            if payload.get("clear") is True:
                self._keys.pop(provider, None)
            else:
                key, model = payload.get("key"), payload.get("model")
                if not isinstance(key, str) or not key.strip() or len(key) > 4096 or any(c.isspace() for c in key.strip()):
                    raise ValueError("API 키를 입력해 주세요.")
                if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", model):
                    raise ValueError("올바른 모델 ID를 입력해 주세요.")
                self._keys[provider] = {"key": key.strip(), "model": model}
        return self.providers()[provider]

    def sample(self):
        path = DEFAULT_COMPARISON_DATASET if DEFAULT_COMPARISON_DATASET.is_file() else PUBLIC_COMPARISON_DATASET
        return {"name": path.name, "cases": normalize_dataset({"input_text": path.read_text(encoding="utf-8")})}

    @staticmethod
    def info():
        return {"blind_version": "comparison-blind-v1", "aiona_versions": "v1", "auto_evaluate": True,
                "evaluation_policy_version": 4,
                "required_providers": list(PROVIDERS),
                "blind_after_collection": True,
                "parallel_collection": True, "parallel_evaluation": True, "evaluation_events": "sse-v1",
                "judge_input_fields": ["question", "answer", "reference", "history"]}

    def _run(self, run_id):
        run = self._runs.get(run_id)
        if run is None:
            path = self.save_dir / f"{run_id}.json"
            if not re.fullmatch(r"[a-f0-9]{12}", run_id) or not path.is_file():
                raise FileNotFoundError(run_id)
            run = json.loads(path.read_text(encoding="utf-8"))
            interrupted = run['status'] in ('COLLECTING', 'EVALUATING')
            self._interrupted(run)
            self._runs[run_id] = run
            if interrupted and run.get('_activity_tracking'):
                self._save(run)
        return run

    @staticmethod
    def _interrupted(run):
        if run["status"] in ("COLLECTING", "EVALUATING"):
            for progress in run.get("progress", {}).values():
                if progress.get("stage") == "COLLECTING":
                    progress.update(stage="ERROR", active=None, timing_interrupted=True)
            run["status"], run["error"] = "ERROR", "서버 재시작으로 실행이 중단되었습니다. 저장된 원문과 결과를 확인해 주세요."
            ComparisonService._clear_inflight(run)

    @staticmethod
    def _clear_inflight(run):
        for detail in run.get("evaluation_progress", {}).values():
            if detail["stage"] not in ("WAITING", "COMPLETED"):
                detail.update(stage="ERROR", active=[])
        for batch in run.get("evaluations", {}).values():
            if batch.get("live"):
                records = batch.get("jev_eval", {}).get("records", [])
                for index, record in enumerate(records):
                    if record and record.get("status") == "RUNNING":
                        records[index] = None

    def _save(self, run):
        try:
            # A successful replacement must not persist warnings from an earlier
            # failed attempt. The relevant warning is restored below on failure.
            run.pop('save_error', None)
            run.pop('activity_save_error', None)
            self._queue_terminal_activity(run)
            stream = self._streams.get(run["run_id"])
            if stream and stream.state["generation"]:
                run["evaluation_log"] = stream.snapshot()
            self.save_dir.mkdir(parents=True, exist_ok=True)
            target = self.save_dir / f"{run['run_id']}.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(run, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
            temporary.replace(target)
            run.pop("save_error", None)
        except (OSError, ValueError):
            run["save_error"] = "실행 기록 저장 실패. JSON을 직접 다운로드해 주세요."
            return False
        try:
            self.version_registry.activity.append(run.get('_activity_outbox', []))
            run.pop('activity_save_error', None)
        except (OSError, ValueError, sqlite3.Error):
            run['activity_save_error'] = '작업 이력 저장 실패. 저장된 실행 기록에서 다시 복구합니다.'
            return False
        return True

    def _queue_terminal_activity(self, run):
        if not run.get('_activity_tracking') or run['status'] not in ('COMPLETED', 'ERROR', 'CANCELLED'):
            return
        key = f"{run['run_id']}:{run.get('_activity_attempt', 1)}:terminal"
        event_type, result = {'COMPLETED': ('evaluation_completed', 'success'),
                             'ERROR': ('evaluation_failed', 'failure'),
                             'CANCELLED': ('evaluation_cancelled', 'cancelled')}[run['status']]
        event = event_record(event_type, key=key, actor=run.get('_activity_cancel_actor', 'system')
                             if run['status'] == 'CANCELLED' else 'system',
                             model_version=(run.get('aiona_release') or {}).get('version'),
                             run_id=run['run_id'], source_run_id=run.get('source_run_id'),
                             before={'status': 'EVALUATING' if run.get('eval_started_at') else 'COLLECTING'},
                             after={'status': run['status'], 'evaluation_summary': self._evaluation_summary(run)},
                             result=result, error_summary=clean_text(run.get('error'), secrets=[
                                 entry.get('key') for entry in self._keys.values()]),
                             related_event_id=run.get('_activity_started_event_id'))
        outbox = run.setdefault('_activity_outbox', [])
        if not any(item['event_id'] == event['event_id'] for item in outbox):
            run['finished_at'] = event['timestamp']
            outbox.append(event)

    @staticmethod
    def _evaluation_summary(run):
        """Question outcomes are exclusive; remaining criterion work is separate.

        A confirmed common/semantic FAIL settles a question even if another
        criterion has no verdict. ``review_required`` counts only unresolved
        questions, while ``criterion_review_required`` retains every explicit
        UNVERIFIED criterion (including those on already failed questions).
        """
        total = run.get('question_count', 0)
        batch = run.get('evaluations', {}).get('aiona') or {}
        semantic = batch.get('jev_eval') or {}
        records = semantic.get('records') or []
        cases = batch.get('cases') or []
        scored, review, criteria_complete, criterion_review = 0, 0, 0, 0
        for index in range(total):
            record = (records[index] if index < len(records) else None) or {}
            case = cases[index] if index < len(cases) else None
            if not case or case.get('case_status') != 'EVALUATED':
                continue
            decisions = record.get('decisions') or {}
            criteria = record.get('criteria') if isinstance(record.get('criteria'), list) else list(decisions)
            unverified = sum((decisions.get(cid) or {}).get('status') == 'UNVERIFIED'
                             and (decisions.get(cid) or {}).get('decision') not in ('PASS', 'FAIL')
                             for cid in criteria)
            criterion_review += unverified
            try:
                case = resolved_case(case, record)  # 옛 결과의 code_completion 투영 (새 결과는 그대로)
            except (KeyError, TypeError, IndexError):
                continue
            common = (case.get('summary') or {}).get('common') or {}
            # 검사 실행 오류(case errors)가 있는 질문은 CODE 완료로 세지 않는다.
            code_complete = (common.get('unjudged') == 0 and not case.get('errors')
                             and common.get('pass', 0) + common.get('fail', 0) > 0)
            semantic_complete = (record.get('status') == 'SUCCESS' and bool(criteria)
                                 and all((decisions.get(cid) or {}).get('decision') in ('PASS', 'FAIL') for cid in criteria))
            confirmed_fail = (common.get('fail', 0) > 0 or any(
                row.get('area') == 'common' and row.get('counted') and row.get('verdict') == 'FAIL'
                for row in case.get('results') or []) or record.get('status') == 'SUCCESS' and any(
                (decisions.get(cid) or {}).get('decision') == 'FAIL' for cid in criteria))
            complete = bool(code_complete and semantic_complete)
            settled = confirmed_fail or complete
            scored += settled
            criteria_complete += complete
            review += bool(not settled and (record.get('review_required') or unverified))
        return {'total': total, 'scored': scored, 'incomplete': max(0, total - scored), 'review_required': review,
                'criteria_incomplete': max(0, total - criteria_complete), 'criterion_review_required': criterion_review}

    @staticmethod
    def _conditions_summary(run):
        profile = evaluation_profile(run)
        fields, pieces = profile['fields'], []
        judge = fields.get('judge') or {}
        if fields.get('judge_models'):
            pieces.append('평가 모델 ' + ', '.join(fields['judge_models']))
        elif judge.get('requested_model'):
            pieces.append('평가 모델 ' + str(judge['requested_model']))
        if fields.get('criteria_version'):
            pieces.append('기준 ' + str(fields['criteria_version']))
        if judge.get('reasoning_effort'):
            pieces.append('추론 ' + str(judge['reasoning_effort']))
        if not profile['complete']:
            pieces.append('평가 조건 확인 필요')
        return ' · '.join(pieces)

    def _status(self, run):
        return {**{k: copy.deepcopy(v) for k, v in run.items()
                   if not k.startswith('_activity_') and k not in ("datasets", "evaluations", "config", "input_document", "evaluation_log")},
                **self.version_registry.metadata(run), 'evaluation_summary': self._evaluation_summary(run),
                'evaluation_conditions_summary': self._conditions_summary(run)}

    def event_stream(self, run_id):
        with self._lock:
            run = self._run(run_id)
            if run_id not in self._streams:
                saved = copy.deepcopy(run.get("evaluation_log"))
                if saved and run["status"] == "ERROR":
                    saved["status"] = "ERROR"
                if not saved and run["status"] in ("COMPLETED", "ERROR", "CANCELLED"):
                    saved = {"status": "COMPLETED" if run["status"] == "COMPLETED" else "ERROR"}
                self._streams[run_id] = EvaluationEvents(saved)
            return self._streams[run_id]

    def status(self, run_id):
        with self._lock:
            return self._status(self._run(run_id))

    def result(self, run_id):
        with self._lock:
            run = self._run(run_id)
            return {**{k: copy.deepcopy(v) for k, v in run.items() if not k.startswith('_activity_')},
                    **self.version_registry.metadata(run), 'evaluation_summary': self._evaluation_summary(run),
                    'evaluation_conditions_summary': self._conditions_summary(run)}

    def history(self, limit=30):
        with self._lock:
            stored = {}
            if self.save_dir.is_dir():
                paths = sorted(self.save_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                if limit is not None:
                    paths = paths[:limit]
                for path in paths:
                    try:
                        run = json.loads(path.read_text(encoding="utf-8"))
                        if run["run_id"] not in self._runs:
                            interrupted = run['status'] in ('COLLECTING', 'EVALUATING')
                            self._interrupted(run)
                            if interrupted and run.get('_activity_tracking'):
                                self._save(run)
                        stored[run["run_id"]] = self._status(run)
                    except (OSError, ValueError, KeyError):
                        continue
            stored.update({rid: self._status(r) for rid, r in self._runs.items()})
            runs = sorted(stored.values(), key=lambda r: r["created_at"], reverse=True)
            return runs if limit is None else runs[:limit]

    def version_history(self):
        with self._lock:
            return self.version_registry.version_history(self.history(limit=None))

    def manage_release(self, payload):
        with self._lock:
            safe_payload = dict(payload)
            secrets = [entry.get('key') for entry in self._keys.values()]
            for field in ('notes', 'reason'):
                if isinstance(safe_payload.get(field), str) and len(safe_payload[field]) <= 2000:
                    safe_payload[field] = clean_text(safe_payload[field], secrets=secrets)
            self.version_registry.manage(safe_payload)
            return self.version_history()

    def activity_history(self, filters=None):
        with self._lock:
            # Replay only outboxes produced by this feature. Never invent past actors/events.
            self.history(limit=None)
            if self.save_dir.is_dir():
                for path in self.save_dir.glob('*.json'):
                    try:
                        run = json.loads(path.read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        continue
                    self.version_registry.activity.append(run.get('_activity_outbox', []))
                    current = self._runs.get(run.get('run_id'), run)
                    if run.get('activity_save_error') or current.get('activity_save_error'):
                        # Only repair tracked warning metadata after replay; an
                        # ordinary history read never rewrites historical runs.
                        if not self._save(current):
                            raise OSError('작업 이력 복구 상태를 저장하지 못했습니다.')
            return self.version_registry.activity.events(filters)

    def version_template(self, run_id):
        source = self.result(run_id)
        if source['status'] != 'COMPLETED' or not source.get('aiona_release'):
            raise ValueError('평가가 종료된 AIONA 버전 기록을 기준으로 선택해 주세요.')
        cases = [{**{k: copy.deepcopy(row[k]) for k in ('case_id', 'question', 'reference', 'conversation_id', 'turn') if k in row},
                  'answer': ''} for row in source['datasets']['aiona']]
        ids = [row['case_id'] for row in cases]
        if len(ids) != len(set(ids)):
            raise ValueError('기준 기록의 문항 ID가 중복되어 같은 질문을 연결할 수 없습니다.')
        return {'baseline_run_id': run_id, 'aiona_release': source['aiona_release'], 'cases': cases}

    def prepare_version_comparison(self, payload):
        baseline_id = payload.get('baseline_run_id')
        if not isinstance(baseline_id, str):
            raise ValueError('기준 평가를 선택해 주세요.')
        template = self.version_template(baseline_id)
        release = validate_release(payload.get('aiona_release'), required=True)
        registered = next((r for r in self.version_registry.releases() if r['version'] == release['version']), None)
        if registered is None:
            raise ValueError('버전 관리에서 새 모델 버전을 먼저 등록해 주세요.')
        if release['version'] == template['aiona_release']['version']:
            raise ValueError('기준과 다른 모델 버전을 선택해 주세요.')
        cases = normalize_dataset(payload)
        by_id = {row['case_id']: row for row in cases}
        expected = template['cases']
        if len(by_id) != len(cases) or set(by_id) != {row['case_id'] for row in expected}:
            raise ValueError('기준 질문과 문항 ID·개수가 다릅니다. 내려받은 양식의 모든 문항을 유지해 주세요.')
        ordered = []
        for row in expected:
            candidate = by_id[row['case_id']]
            for key in ('question', 'reference', 'conversation_id', 'turn'):
                if json.dumps(candidate.get(key), sort_keys=True, ensure_ascii=False) != json.dumps(row.get(key), sort_keys=True, ensure_ascii=False):
                    raise ValueError(f"{row['case_id']}: 질문·참조자료·대화 순서가 기준과 다릅니다. 답변만 입력해 주세요.")
            if not candidate['answer'].strip():
                raise ValueError(f"{row['case_id']}: 새 버전의 답변을 입력해 주세요.")
            ordered.append(candidate)
        return {'baseline_run_id': baseline_id, 'aiona_release': validate_release(registered), 'cases': ordered}

    def start_version_run(self, payload):
        """A separate AIONA-only evaluation; never collect competitor responses."""
        release = validate_release(payload.get('aiona_release'), required=True)
        self.evaluator._config(payload)
        options = dict(payload)
        if payload.get('baseline_run_id') is not None:
            if payload.get('source_run_id') is not None:
                raise ValueError('기준 비교와 원문 재평가는 동시에 요청할 수 없습니다.')
            prepared = self.prepare_version_comparison(payload)
            options.update(prepared)
            options.pop('input_text', None)
            release = prepared['aiona_release']
        source_id = payload.get('source_run_id')
        if source_id is not None:
            if not isinstance(source_id, str):
                raise ValueError('원본 실행 ID가 올바르지 않습니다.')
            if any(k in payload for k in ('cases', 'input_text', 'input_document', 'question', 'answer')):
                raise ValueError('재평가는 원본 실행 ID만 사용하며 새 원문을 함께 보낼 수 없습니다.')
            source = self.result(source_id)
            source_release = source.get('aiona_release')
            if not source_release or release['version'] != source_release['version']:
                raise ValueError('재평가는 원본 AIONA 버전을 유지해야 합니다.')
            options['cases'] = source['datasets']['aiona']
            options['input_mode'] = source.get('input_mode', 'file')
            if source.get('input_document') is not None:
                options['input_document'] = source['input_document']
            release = source_release
        options.update(aiona_release=release, providers=[], blind=False, auto_evaluate=True)
        # start validates all input/configuration before creating or saving a run.
        return self.start(options)

    def start_comparison(self, payload):
        """Public workspace always collects all three providers before judging four models."""
        if payload.get('baseline_run_id') is not None:
            raise ValueError('동일 질문의 버전 비교는 버전 비교 등록 기능을 사용해 주세요.')
        if payload.get('source_run_id') is not None:
            raise ValueError('이전 실행 재평가는 버전 비교의 새 평가 기능을 사용해 주세요.')
        selected = payload.get("providers", list(PROVIDERS))
        if (not isinstance(selected, list) or len(selected) != len(PROVIDERS)
                or any(not isinstance(p, str) for p in selected) or set(selected) != set(PROVIDERS)):
            raise ValueError("새 비교는 Gemini·Claude·GPT 모두의 답변을 수집한 뒤 AIONA 포함 4개 모델을 평가합니다. 화면을 새로고침하고 4개 모델 비교를 시작해 주세요.")
        with self._lock:
            expected = payload.get('expected_model_version')
            current = self.version_registry.current_release()
            if expected is not None and expected != (current or {}).get('version'):
                raise ValueError('현재 모델 버전이 변경되었습니다. 새로고침 후 확인해 주세요.')
            missing = [name for p, name in PROVIDERS.items() if p not in self._keys]
            if missing:
                raise ValueError("4개 모델 비교에 필요한 연결이 없습니다: " + ", ".join(missing) + ". API 키와 모델 ID를 등록해 주세요.")
            return self.start({**payload, "providers": list(PROVIDERS), "blind": True, "auto_evaluate": True})

    def start(self, payload):
        release = validate_release(payload.get('aiona_release'))
        if release is None:
            release = self.version_registry.current_release()
        source_id = payload.get('source_run_id')
        if source_id is not None and (not isinstance(source_id, str) or not re.fullmatch(r'[a-f0-9]{12}', source_id)):
            raise ValueError('원본 실행 ID가 올바르지 않습니다.')
        cases = normalize_dataset(payload)
        input_mode = payload.get('input_mode', 'file')
        if input_mode not in ('file', 'manual'):
            raise ValueError('입력 방식이 올바르지 않습니다.')
        if input_mode == 'manual' and (len(cases) != 1 or not cases[0]['answer'].strip()):
            raise ValueError('직접 입력은 질문과 AIONA 답변 한 쌍이 필요합니다.')
        document = payload.get('input_document')
        if document is not None:
            if not isinstance(document, dict) or not isinstance(document.get('original_text'), str):
                raise ValueError('업로드 원문 기록 형식이 올바르지 않습니다.')
            document = {k: copy.deepcopy(document[k]) for k in ('original_text', 'source_name', 'format', 'notes') if k in document}
            validate_json_numbers(document)
        blind, auto_evaluate = payload.get("blind", False), payload.get("auto_evaluate", False)
        if not isinstance(blind, bool) or not isinstance(auto_evaluate, bool):
            raise ValueError("blind와 auto_evaluate는 true 또는 false여야 합니다.")
        evaluation_payload = {"aiona_config": copy.deepcopy(payload.get("aiona_config"))}
        if auto_evaluate:
            self.evaluator._config(evaluation_payload)  # reject bad settings before paid collection
        selected = payload.get("providers", [])
        if not isinstance(selected, list) or len(selected) > 3 or len(selected) != len(set(selected)) or any(p not in PROVIDERS for p in selected):
            raise ValueError("비교 모델 선택이 올바르지 않습니다.")
        max_tokens = payload.get("max_tokens", 8192)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 256 <= max_tokens <= 32768:
            raise ValueError("출력 토큰 한도는 256~32,768이어야 합니다.")
        with self._lock:
            if self._closing:
                raise ValueError("서버가 종료 중이므로 새 비교를 시작할 수 없습니다.")
            if any(p not in self._keys for p in selected):
                raise ValueError("선택한 모델의 API 키와 모델 ID를 먼저 등록해 주세요.")
            active = [r for r in self._runs.values() if r["status"] in ("COLLECTING", "EVALUATING")]
            if len(active) >= MAX_RUNS:
                raise JobCapacityError("실행 중인 비교가 많습니다. 완료 후 다시 시도해 주세요.")
            config = {p: dict(self._keys[p]) for p in selected}  # closure only; never serialize keys
            run_id = uuid.uuid4().hex[:12]
            run = {"run_id": run_id, "name": str(payload.get("name") or "새 비교")[:200],
                   "created_at": datetime.now(timezone.utc).isoformat(), "status": "COLLECTING" if selected else "READY",
                   "phase": "COLLECT", "total": len(cases) * len(selected), "done": 0,
                   "question_count": len(cases), "providers": ["aiona", *selected], "max_tokens": max_tokens,
                   "models": {"aiona": "업로드 원문", **{p: config[p]["model"] for p in selected}},
                   "progress": {p: {"done": 0, "success": 0, "incomplete": 0, "failed": 0, "total": len(cases),
                                    "stage": "WAITING", "active": None, "started_at": None,
                                    "finished_at": None, "elapsed_ms": 0, "duration_ms": None} for p in selected},
                   "datasets": {"aiona": cases, **{p: [None] * len(cases) for p in selected}},
                   "evaluations": {}, "cancel_requested": False, "error": None,
                   "auto_evaluate": auto_evaluate, "input_mode": input_mode, "aiona_specialized": False}
            if selected:
                chain_workers = min(self.collection_workers, len(self._collection_chains(cases)))
                run["collection_execution"] = {"collection_workers_per_model": chain_workers,
                    "collection_workers_total": chain_workers * len(selected),
                    "conversation_order": "sequential_original_turns"}
            run.update(aiona_release=release, dataset_fingerprint=fingerprint(cases),
                       source_fingerprint=fingerprint(cases, answers=True))
            if source_id is not None:
                run['source_run_id'] = source_id
            if payload.get('baseline_run_id') is not None:
                run['baseline_run_id'] = payload['baseline_run_id']
            if document is not None:
                run['input_document'] = document
            if blind:
                run["blind_requested"] = True
            started = event_record('evaluation_started', key=f'{run_id}:1:start',
                                   timestamp=run['created_at'], model_version=(release or {}).get('version'),
                                   run_id=run_id, after={'status': run['status'], 'input_mode': input_mode,
                                                       'question_count': len(cases)}, source_run_id=source_id)
            run.update(_activity_tracking=1, _activity_attempt=1, _activity_outbox=[started],
                       _activity_started_event_id=started['event_id'])
            if source_id:
                run['_activity_outbox'].append(event_record('reevaluation_created', key=f'{run_id}:reevaluation',
                    timestamp=run['created_at'], model_version=(release or {}).get('version'), run_id=run_id,
                    source_run_id=source_id, related_event_id=started['event_id'],
                    after={'run_id': run_id}, before={'run_id': source_id}))
            self._runs[run_id] = run
            self._cancel_events[run_id] = token = threading.Event()
            self._cost_ledgers[run_id] = ledger = CostLedger()
            if not self._save(run):
                self._cancel_events.pop(run_id, None)
                self._cost_ledgers.pop(run_id, None)
                run['status'], run['error'] = 'ERROR', '실행 기록 또는 작업 이력을 저장하지 못해 평가를 시작하지 않았습니다.'
                self._save(run)
                raise OSError(run['error'])
        if selected:
            try:
                self._launch_worker(run, token, self._collect, (run, cases, config, evaluation_payload, token))
            except Exception:
                with self._lock:
                    if token.is_set():
                        self._mark_cancelled(run)
                    else:
                        run["status"], run["error"] = "ERROR", "응답 수집 작업을 시작하지 못했습니다."
                    self._cancel_events.pop(run_id, None)
                    self._save(run)
                    self._finish_costs(run, ledger)
                raise ValueError(run["error"]) from None
        elif auto_evaluate:
            with self._lock:
                if self._closing or token.is_set():
                    token.set()
                    self._mark_cancelled(run)
                    self._save(run)
                    self._cancel_events.pop(run_id, None)
                    self._finish_costs(run, ledger)
                else:
                    self.evaluate(run_id, evaluation_payload)
        else:
            with self._lock:
                self._cancel_events.pop(run_id, None)
                self._finish_costs(run, ledger)
        return self.status(run_id)

    @staticmethod
    def _answer_case(case, provider, model, index, outcome):
        # AIONA display text must never be reused for other models.
        result = {k: copy.deepcopy(case[k]) for k in ("case_id", "question", "reference", "conversation_id", "turn") if k in case}
        result.update({"answer": outcome.get("answer", ""), "system_id": provider,
                       "source": model, "case_index": index, "collection": outcome})
        if outcome["status"] != "SUCCESS":
            # The downloaded JSON is also safe to feed directly into the
            # existing CLI/UI evaluator. Preserve partial text but exclude it.
            result["__parse_error__"] = "응답 수집 실패·중단 또는 미완료: " + outcome.get("error", "평가 제외")
        return result

    def _collect(self, run, cases, config, evaluation_payload=None, cancel_event=None):
        # Stable original indices remain in JSON even when turns are reordered
        # for requests. AIONA's answers are never passed as another model's history.
        chains = self._collection_chains(cases)
        workers = run.get("collection_execution", {}).get("collection_workers_per_model", 1)
        ledger = self._cost_ledgers.get(run['run_id'])
        token = cancel_event if cancel_event is not None else threading.Event()
        def collect_provider(provider, settings):
            started = time.monotonic()
            with self._lock:
                if token.is_set() or run["cancel_requested"]:
                    run["progress"][provider].update(stage="CANCELLED", active=None)
                    return
                run["progress"][provider].update(stage="COLLECTING", started_at=datetime.now(timezone.utc).isoformat())
                run["revision"] = run.get("revision", 0) + 1
                self._save(run)
            try:
                with self.client_factory() as client:
                    # Only this bounded number of chain workers is submitted.
                    # httpx.Client shares its connection pool safely across threads.
                    # Each worker owns a chain's history; no model or dialogue can
                    # observe another chain's answers even when calls overlap.
                    next_chain, active = [0], set()
                    queue_lock, stopped = threading.Lock(), threading.Event()
                    def collect_chains():
                        try:
                            while not token.is_set() and not stopped.is_set():
                                with queue_lock:
                                    if next_chain[0] >= len(chains):
                                        return
                                    chain = chains[next_chain[0]]
                                    next_chain[0] += 1
                                history, broken = [], False
                                for index in chain:
                                    with self._lock:
                                        if token.is_set() or stopped.is_set() or run["cancel_requested"]:
                                            return
                                        active.add(index)
                                        run["progress"][provider].update(stage="COLLECTING", active=min(active))
                                        run["revision"] = run.get("revision", 0) + 1
                                    case = cases[index]
                                    if broken:
                                        outcome = {"status": "ERROR", "error": "앞선 대화 응답이 없어 이 턴의 수집을 건너뛰었습니다."}
                                    else:
                                        messages = list(history) + [{"role": "user", "content": case["question"]}]
                                        if token.is_set() or stopped.is_set():
                                            return
                                        request_started = time.monotonic()
                                        try:
                                            outcome = request_answer(provider, settings, messages, run["max_tokens"], client,
                                                usage_observer=(lambda raw: ledger.record_collection(provider, settings['model'], raw)) if ledger else None)
                                        except ProviderError as exc:
                                            outcome = {"status": "ERROR", "error": str(exc)}
                                        outcome["latency_ms"] = round((time.monotonic() - request_started) * 1000, 1)
                                    if outcome["status"] == "SUCCESS":
                                        history = messages + [{"role": "assistant", "content": outcome["answer"]}]
                                    else:
                                        broken = True
                                    row = self._answer_case(case, provider, settings["model"], index, outcome)
                                    with self._lock:
                                        run["datasets"][provider][index] = row
                                        progress = run["progress"][provider]
                                        progress["done"] += 1
                                        active.discard(index)
                                        progress["active"] = min(active) if active and not token.is_set() else None
                                        progress["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
                                        progress[{"SUCCESS": "success", "INCOMPLETE": "incomplete", "ERROR": "failed"}[outcome["status"]]] += 1
                                        run["done"] += 1
                                        self._save(run)
                        except Exception:
                            stopped.set()
                            raise
                    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"collect-{provider}") as chains_pool:
                        futures = [chains_pool.submit(collect_chains) for _ in range(workers)]
                        for future in as_completed(futures):
                            future.result()
                with self._lock:
                    run["progress"][provider].update(stage="CANCELLED" if token.is_set() or run["cancel_requested"] else "COMPLETED", active=None)
            except Exception:
                with self._lock:
                    run["progress"][provider].update(stage="CANCELLED" if token.is_set() else "ERROR", active=None)
                raise
            finally:
                with self._lock:
                    elapsed = round((time.monotonic() - started) * 1000, 1)
                    run["progress"][provider].update(finished_at=datetime.now(timezone.utc).isoformat(),
                                                     elapsed_ms=elapsed, duration_ms=elapsed)
                    run["revision"] = run.get("revision", 0) + 1
                    self._save(run)
        try:
            with ThreadPoolExecutor(max_workers=len(config), thread_name_prefix="collect") as pool:
                futures = [pool.submit(collect_provider, provider, settings) for provider, settings in config.items()]
                for future in as_completed(futures):
                    future.result()
            with self._lock:
                run["status"] = "CANCELLED" if token.is_set() or run["cancel_requested"] else "READY"
                if token.is_set():
                    self._mark_cancelled(run)
                if not run["cancel_requested"]:
                    run["collection_completed_at"] = datetime.now(timezone.utc).isoformat()
                # Transition under the same lock so pollers cannot observe a
                # terminal READY state and stop before auto evaluation begins.
                if run.get("auto_evaluate") and not run["cancel_requested"]:
                    self.evaluate(run["run_id"], evaluation_payload or {})
                else:
                    self._finish_costs(run, ledger)
        except Exception:
            with self._lock:
                if run.get("phase") != "EVALUATE":
                    if token.is_set() or run["cancel_requested"]:
                        self._mark_cancelled(run)
                    else:
                        run["status"], run["error"] = "ERROR", "응답 수집을 완료하지 못했습니다. 수집된 원문은 보존했습니다."
                    self._finish_costs(run, ledger)
        finally:
            with self._lock:
                self._save(run)

    def cancel(self, run_id):
        with self._lock:
            run = self._run(run_id)
            if run["status"] in ("COLLECTING", "EVALUATING"):
                token = self._cancel_events.get(run_id)
                if token is not None:
                    token.set()
                # Keep the operation active until its in-flight response is
                # saved; terminal CANCELLED then means its worker has drained.
                run["cancel_requested"] = True
                run['_activity_cancel_actor'] = 'local'
                run["revision"] = run.get("revision", 0) + 1
                self._save(run)
            return self._status(run)

    def evaluate(self, run_id, payload):
        with self._lock:
            if self._closing:
                raise ValueError("서버가 종료 중이므로 평가를 시작할 수 없습니다.")
            run = self._run(run_id)
            if run["status"] not in ("READY", "CANCELLED", "ERROR"):
                raise ValueError("응답 수집을 마친 후 평가를 시작해 주세요.")
            if run_id in self._cost_ledgers and any(
                    thread is not threading.current_thread() for thread in self._workers.get(run_id, ())):
                raise ValueError("앞선 작업을 중단하는 중입니다. 종료 후 다시 평가해 주세요.")
            self.evaluator._config(payload)  # validate before changing the state
            # 의미 평가 방식은 실행 시작 시점에 한 번 확정한다 (잘못된 SEMANTIC_PIPELINE 값은 상태를 바꾸기 전에 거부)
            pipeline = jev_eval.resolve_semantic_pipeline()
            previous_status = run['status']
            ledger = self._cost_ledgers.get(run_id)
            if ledger is None:
                self._cost_ledgers[run_id] = ledger = CostLedger()
            started_at = datetime.now(timezone.utc).isoformat()
            if previous_status in ('ERROR', 'CANCELLED') or not run.get('_activity_tracking'):
                attempt = run.get('_activity_attempt', 0) + 1
                started = event_record('evaluation_started', key=f'{run_id}:{attempt}:start',
                    timestamp=started_at, model_version=(run.get('aiona_release') or {}).get('version'),
                    run_id=run_id, before={'status': previous_status}, after={'status': 'EVALUATING'},
                    source_run_id=run.get('source_run_id'))
                run.setdefault('_activity_outbox', []).append(started)
                run.update(_activity_tracking=1, _activity_attempt=attempt, _activity_started_event_id=started['event_id'])
                run.pop('finished_at', None)
                run.pop('_activity_cancel_actor', None)
            self._cancel_events[run_id] = token = threading.Event()
            # Histories contain already-collected responses, so semantic checks of
            # different turns are independent. Freeze equal concurrency per model.
            workers_per_provider = max(1, self.semantic_workers // len(run["providers"]))
            run["evaluation_execution"] = {
                "semantic_workers_per_model": workers_per_provider,
                "semantic_workers_total": workers_per_provider * len(run["providers"]),
                # 의미 평가 방식(legacy | items)과 호출 방식(realtime; Batch 는 3c 단계) — 저장 결과에서 읽는 실행 조건
                "pipeline": pipeline,
                "dispatch": "realtime",
            }
            # items 파이프라인의 J5-3.a 적용 판정·시점 있는 사실의 기준 — API 로 수집한 답변은 외부 도구 없이 생성(none),
            # AIONA 답변은 도구 사용 여부를 알 수 없음(unknown). 수집 날짜는 AIONA 는 등록한 수집일, API 는 수집 완료 시각의 날짜.
            collected = run.get("collection_completed_at")
            semantic_contexts = {
                provider: {"answer_tools": "unknown" if provider == "aiona" else "none",
                           "collected_at": ((run.get("aiona_release") or {}).get("collected_at") if provider == "aiona"
                                            else collected[:10] if isinstance(collected, str) and len(collected) >= 10 else None)}
                for provider in run["providers"]}
            if run.get("blind_requested") and not run.get("blind", {}).get("enabled"):
                run["blind"] = {**blind_plan(run["providers"]), "applied_at": started_at}
            run.update(status="EVALUATING", phase="EVALUATE", error=None, cancel_requested=False,
                       eval_done=0, eval_total=len(run["providers"]),
                       eval_started_at=started_at, revision=run.get("revision", 0) + 1,
                       evaluation_progress={p: {"stage": "WAITING", "code_done": 0,
                           "code_total": run["question_count"], "semantic_done": 0,
                           "semantic_total": run["question_count"], "semantic_success": 0,
                           "semantic_errors": 0, "semantic_unverified": 0, "skipped": 0, "active": [], "latest": None,
                           "updated_at": started_at} for p in run["providers"]})
            run["evaluations"] = {}
            stream = self.event_stream(run_id)
            stream.publish({"type": "run_start", "total": run["question_count"] * len(run["providers"])})
            if not self._save(run):
                run['status'], run['error'] = 'ERROR', '실행 기록 또는 작업 이력을 저장하지 못해 평가를 시작하지 않았습니다.'
                self._save(run)
                self._cancel_events.pop(run_id, None)
                self._finish_costs(run, ledger)
                raise OSError(run['error'])

        last_snapshot = [0.0]
        semantic_session = []
        semantic_lock = threading.Lock()
        resolver = ledger.wrap_resolver(self.evaluator._jev_resolver or jev_eval.resolve_jev_client)
        def shared_semantic_resolver():
            # Resolve once after the first CODE batch. All compared models use
            # the same judge/config even if .env is edited during a long run.
            with semantic_lock:
                if not semantic_session:
                    semantic_session.append(resolver())
                return semantic_session[0]
        def emit(provider, event):
            # Keep the stop check and publication atomic with shutdown's final
            # run_error, so a late criterion_start cannot restore active markers.
            with self._lock:
                if token.is_set():
                    return
                if event["type"] == "code_result":
                    event = code_event(event["row"], event["index"], event["qid"])
                label = run.get("blind", {}).get("labels", {}).get(provider) or ("AIONA" if provider == "aiona" else PROVIDERS[provider])
                stream.publish({**event, "model": label})
        def observe(provider, event):
            with self._lock:
                detail = run["evaluation_progress"][provider]
                detail["stage"] = "CANCELLED" if token.is_set() else event["phase"]
                detail["updated_at"] = datetime.now(timezone.utc).isoformat()
                run["revision"] += 1
                if event["phase"] == "CODE":
                    detail["code_done"] = event["done"]
                if "batch" in event:
                    batch = copy.deepcopy(event["batch"])
                    batch.update(live=True, jev_eval={"status": "RUNNING", "history_policy": "full_original_text", "criteria": event["criteria"],
                                                    "records": [None] * len(batch["cases"])})
                    run["evaluations"][provider] = batch
                    self._save(run)
                    last_snapshot[0] = time.monotonic()
                if "index" in event:
                    index = event["index"]
                    if event.get("started"):
                        if token.is_set():
                            return
                        detail["active"].append(index)
                        run["evaluations"][provider]["jev_eval"]["records"][index] = {"status": "RUNNING"}
                    else:
                        record = copy.deepcopy(event["record"])
                        run["evaluations"][provider]["jev_eval"]["records"][index] = record
                        if index in detail["active"]:
                            detail["active"].remove(index)
                        detail["semantic_done"] += 1
                        detail["semantic_unverified" if record.get("review_required") else
                               "semantic_success" if record["status"] == "SUCCESS" else
                               "skipped" if record["status"] == "SKIPPED" else "semantic_errors"] += 1
                        detail["latest"] = {"index": index, "case_id": record["case_id"],
                                            "status": record["status"], "updated_at": detail["updated_at"]}
                        # Persist real completed records for recovery. Live placeholders
                        # are explicitly marked and never offered as a final report.
                        if time.monotonic() - last_snapshot[0] >= 1:
                            self._save(run)
                            last_snapshot[0] = time.monotonic()
        def evaluate_provider(provider):
            source = run["datasets"][provider]
            cases = []
            for index, row in enumerate(source):
                if row is not None and (provider == "aiona" or row["collection"]["status"] == "SUCCESS"):
                    cases.append(row)
                else:
                    # Preserve indices and denominator. A network failure
                    # is INPUT_ERROR, never an empty-answer quality FAIL.
                    original = run["datasets"]["aiona"][index]
                    cases.append({**{k: original[k] for k in ("case_id", "conversation_id", "turn") if k in original},
                                  "__parse_error__": "응답 수집 실패·중단 또는 미완료로 평가에서 제외했습니다."})
            options = {"observer": lambda event, p=provider: observe(p, event),
                       "event_sink": lambda event, p=provider: emit(p, event),
                       "semantic_workers": workers_per_provider,
                       "cancel_event": token,
                       "pipeline": pipeline, "semantic_context": semantic_contexts[provider],
                       "jev_resolver": shared_semantic_resolver if run.get("blind", {}).get("enabled") else resolver}
            batch = self.evaluator.evaluate({"cases": cases, "aiona_config": payload.get("aiona_config"),
                                             "aiona_specialized": False}, **options)
            with self._lock:
                run["evaluations"][provider] = batch
                if provider == 'aiona':
                    run['evaluation_profile'] = evaluation_profile(run)
                run["eval_done"] += 1
                run["evaluation_progress"][provider].update(stage="CANCELLED" if token.is_set() else "COMPLETED", active=[])
                run["revision"] += 1
                self._save(run)
        def union_pass():
            # items 파이프라인: 네 평가가 모두 끝난 뒤 모델 간 요소 합집합 — 빠진 요소만 재확인하고 레코드·요약을 다시 집계한다.
            # 호출은 잠금 밖에서 사본으로 하고, 끝난 뒤 잠금 안에서 교체·저장·이벤트 재전송한다.
            with self._lock:
                if token.is_set() or run["cancel_requested"]:
                    return
                snapshot = copy.deepcopy(run["evaluations"])
                labels = (run.get("blind") or {}).get("labels")
            client = shared_semantic_resolver()[0]
            updated = []
            try:
                union = semantic_union.reconcile(snapshot, client, semantic_contexts=semantic_contexts, blind_labels=labels,
                                                 cancel_event=token, workers=workers_per_provider,
                                                 on_record=lambda provider, index, record: updated.append((provider, index, record)))
            except CancelledError:
                union = {"version": semantic_union.UNION_VERSION, "status": "CANCELLED"}
            with self._lock:
                if token.is_set() or run["cancel_requested"]:
                    run["semantic_union"] = {**union, "status": "CANCELLED"}
                    return
                run["evaluations"] = snapshot
                run["semantic_union"] = union
                run["evaluation_profile"] = evaluation_profile(run)
                for provider, index, record in updated:
                    for event in record_events(snapshot[provider]["cases"][index], record, index):
                        emit(provider, event)
                run["revision"] += 1
                self._save(run)

        def work():
            failed = False
            try:
                with ThreadPoolExecutor(max_workers=len(run["providers"]), thread_name_prefix="evaluate") as pool:
                    futures = [pool.submit(evaluate_provider, provider)
                               for provider in run.get("blind", {}).get("order", run["providers"])]
                    for future in as_completed(futures):
                        future.result()
                if pipeline == "items":
                    union_pass()
            except Exception:
                failed = True
            finally:
                with self._lock:
                    if token.is_set() or run["cancel_requested"]:
                        self._mark_cancelled(run)
                    elif failed:
                        run["status"], run["error"] = "ERROR", "평가를 완료하지 못했습니다. 완료된 결과와 원문은 보존했습니다."
                        self._clear_inflight(run)
                        stream.publish({"type": "run_error", "reason": run["error"]})
                    else:
                        run["status"] = "COMPLETED"
                        stream.publish({"type": "run_done"})
                    run["revision"] += 1
                    self._save(run)
                    self._finish_costs(run, ledger)
        try:
            self._launch_worker(run, token, work)
        except Exception:
            with self._lock:
                if token.is_set():
                    self._mark_cancelled(run)
                else:
                    run["status"], run["error"] = "ERROR", "평가 작업을 시작하지 못했습니다."
                    self._clear_inflight(run)
                    stream.publish({"type": "run_error", "reason": run["error"]})
                self._cancel_events.pop(run_id, None)
                self._save(run)
                self._finish_costs(run, ledger)
            raise ValueError(run["error"]) from None
        return self.status(run_id)

    def export(self, run_id, provider=None):
        run = self.result(run_id)
        if provider:
            if provider not in run["datasets"]:
                raise ValueError("이 실행에 해당 모델이 없습니다.")
            # Include all collection outcomes for round-trip transparency; the
            # download remains the same question/answer input JSON structure.
            value = [row if row is not None else self._answer_case(
                run["datasets"]["aiona"][index], provider, run["models"][provider], index,
                {"status": "NOT_COLLECTED", "error": "아직 수집되지 않은 질문입니다."})
                for index, row in enumerate(run["datasets"][provider])]
        else:
            value = run
        return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
