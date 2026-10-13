"""Local comparison workspace: preserve prompts, answer text and provider response JSON.

Keys loaded from local environment or UI stay in server memory. Collection never retries a paid request
automatically. Failed, blocked and truncated responses remain explicit outcomes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx

from code_eval.core import LIMITS
from code_eval.common_adjudication import resolved_case
from code_eval import attachment_images, jev_eval
from code_eval.engine import _conversation_of
from code_eval.io import validate_json_numbers
from code_eval.llm_client import env_value
from .code_eval_service import CodeEvalService, JobCapacityError, ROOT
from .blind_comparison import blind_plan
from .dataset_import import import_text
from .evaluation_events import EvaluationEvents, code_event, record_events
from . import semantic_union
from . import attachment_delivery
from . import auto_collection as auto
from .version_registry import VersionRegistry, evaluation_profile, fingerprint, validate_release
from .aiona_history import aiona_history
from .version_comparison import compare_runs
from .registry_store import clean_text
from .run_summary import collection_comparison, evaluation_method, input_composition
from .ingestion import SCHEMA as COLLECTION_SCHEMA, import_files
from .ingestion.api_adapter import LIVE_PROVIDERS, parse_api_response, response_fields
from .ingestion.runs import build_run_data, dataset_row, environment_comparison, system_summary
from .ingestion.schema import response_key
from .ingestion.sources import normalize_sources

PROVIDERS = {"gemini": "Gemini", "claude": "Claude", "gpt": "GPT"}


def _request_shape(messages, attachments=0, delivery=None):
    """이번 요청에 실제로 보낸 메시지 구성 — 원문 없이 수·역할·글자 수·sha256 앞 12자 (모델별 입력 차이 확인용).
    attachments_in_question 은 질문에 딸린 첨부(J8) 수, attachments_sent 는 이 요청에 실제로 실은 수 — request_answer 는 텍스트만
    보내던 때는 늘 0 이었다. 지금은 attachment_delivery 가 글자 첨부는 질문에 붙이고 이미지는 실제 파일이 있을 때만 보낸다 —
    attachment_delivery 에 첨부마다 보낸 방식(text·text_extracted·image·not_sent)과 사유를 남긴다 (10-14 3차)."""
    delivery = list(delivery or [])
    return {"attachments_in_question": attachments, "attachments_sent": sum(1 for d in delivery if d.get("mode") != "not_sent"),
            **({"attachment_delivery": delivery} if delivery else {}), "messages": len(messages), "user": sum(m["role"] == "user" for m in messages),
            "assistant": sum(m["role"] == "assistant" for m in messages), "chars": sum(len(m["content"]) for m in messages),
            "own_answer_chars": sum(len(m["content"]) for m in messages if m["role"] == "assistant"),
            "sha256": hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]}


def collection_request(max_tokens):
    """수집 요청 조건 기록 — request_answer 가 실제로 보내는 값만 (모델·대화 이력·최대 출력). 재현용, 판정에는 쓰지 않는다."""
    return {"access_mode": "api", "parameters": {"max_output_tokens": max_tokens}, "system_prompt": None, "tools": [],
            "sampling": "provider_default", "history": "own_previous_answers",
            "note": "시스템 프롬프트·도구·온도 없이 질문(과 같은 대화의 앞 턴, 그 모델의 이전 답변)만 보냄 · OpenAI store=false"}
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


def _external_semantic_context(system):
    """외부 수집 시스템의 의미 평가 문맥 — 웹검색·도구가 모두 꺼진 것으로 확인된 경우만 도구 없이 생성(none),
    수집 날짜는 수집 시각을 아는 답변이 모두 같은 날일 때만 쓴다(추정하지 않는다)."""
    env = system.get("environment") or {}
    collection = system.get("collection") or {}
    first, last = collection.get("collected_from"), collection.get("collected_to")
    same_day = (isinstance(first, str) and isinstance(last, str) and first[:10] == last[:10]
                and not collection.get("collected_time_unknown"))
    return {"answer_tools": "none" if env.get("web_search_available") is False and env.get("tools_available") == [] else "unknown",
            "collected_at": first[:10] if same_day else None}


class ProviderError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status  # HTTP 상태 코드 (네트워크 실패·형식 오류는 None)


def request_answer(provider, config, messages, max_tokens, client, *, usage_observer=None, images=None):
    """No answer strip, newline replacement, Markdown rendering or JSON-mode prompt.
    ``images`` (첨부 이미지, attachment_delivery.prepare) 가 있으면 마지막 사용자 메시지에 제공자 형식으로 함께 싣는다 — 없으면 예전 요청 그대로."""
    if images:
        last = messages[-1]
        messages = messages[:-1] + [{"role": last["role"], "content": attachment_delivery.image_blocks(provider, last["content"], images)}]
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
    elif provider == "factchat":
        # OpenAI 호환 Chat Completions 게이트웨이 — 시스템 프롬프트·도구 없이 같은 메시지만 보낸다.
        url = config["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    else:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": key}
        body = {"contents": [{"role": "model" if m["role"] == "assistant" else "user",
                               "parts": m["content"]["gemini_parts"] if isinstance(m["content"], dict) else [{"text": m["content"]}]}
                              for m in messages],
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
            raise ProviderError(f"HTTP {response.status_code}: {reason}", status=response.status_code)
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
            elif provider == "factchat":
                choice = (raw.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                content = message.get("content")
                if isinstance(content, list):
                    texts = [part["text"] for part in content if part.get("type") == "text"]
                else:
                    texts = [text for text in (content, message.get("refusal")) if isinstance(text, str) and text][:1]
                finish = choice.get("finish_reason") or "unknown"
                complete = finish == "stop"
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
        # 첨부 이미지 파일을 읽어도 되는 폴더 (10-14 3차) — 이 밖의 경로는 읽지 않는다. 의미 평가도 같은 폴더에서 같은 그림을 찾는다 (4차)
        self.attachment_roots = list(attachment_images.DEFAULT_ROOTS)
        self.evaluator = evaluator
        self.save_dir = Path(save_dir) if save_dir is not None else ROOT / "data" / "comparison_runs"
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=180, follow_redirects=False))
        self._keys, self._runs = {}, {}
        self._streams = {}
        self._code_recheck_cache = {}   # (run_id, 엔진, 완료 시각) → 현재 엔진 CODE 재검사 차이
        self._cost_ledgers = {}  # Runtime only: never add costs to serialized runs.
        self._cancel_events = {}
        self._workers = {}
        self._closing = False
        self.semantic_workers = self._semantic_worker_count(semantic_workers)
        self.collection_workers = self._collection_worker_count(collection_workers)
        self._lock = threading.RLock()
        self.relay_fetcher = None  # 중계 URL 확인 함수 (테스트 대역). None 이면 공개 주소 검사를 하는 기본 확인기.
        default_registry = (self.save_dir.parent if save_dir is not None else ROOT / 'data') / 'aiona_versions.json'
        self.version_registry = VersionRegistry(Path(registry_path) if registry_path is not None else default_registry)
        # 자동 수집 관리 설정(키 없음)과 작업 색인 — 같은 업로드는 같은 작업으로 돌려준다 (서버 시작 후 처음 필요할 때 저장 기록에서 만든다)
        self.auto_policy = auto.PolicyStore((self.save_dir.parent if save_dir is not None else ROOT / 'data') / 'auto_collection_policy.json')
        self._auto_jobs = None   # job_key → 가장 최근 run_id
        self._auto_days = {}     # run_id → 일일 한도에 묶인 날짜

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
        key, model = env_value("FACTCHAT_API_KEY", environment, path), env_value("FACTCHAT_MODEL", environment, path)
        if key and model:
            configs["factchat"] = {"key": key, "model": model, "base_url": env_value("FACTCHAT_BASE_URL", environment, path) or None}
        for provider, config in configs.items():
            self.configure(provider, config)
        self.semantic_workers = workers
        self.collection_workers = collection_workers

    def provider_config(self, provider):
        """내부용 — 질문 세트 생성기가 같은 연결 설정(키·모델)을 쓴다. 화면·기록으로 내보내지 않는다."""
        with self._lock:
            config = self._keys.get(provider)
            return dict(config) if config else None

    def providers(self):
        with self._lock:
            return {p: {"name": name, "configured": p in self._keys,
                        "model": self._keys.get(p, {}).get("model", "")} for p, name in PROVIDERS.items()}

    def configure(self, provider, payload):
        if (provider not in PROVIDERS and provider != "factchat") or not isinstance(payload, dict):
            raise ValueError("지원하지 않는 모델 제공자입니다.")
        with self._lock:
            if payload.get("clear") is True:
                self._keys.pop(provider, None)
            else:
                key, model = payload.get("key"), payload.get("model")
                if not isinstance(key, str) or not key.strip() or len(key) > 4096 or any(c.isspace() for c in key.strip()):
                    raise ValueError("API 키를 입력해 주세요.")
                if provider == "factchat":
                    # 게이트웨이 모델 ID 는 'google/gemma-…' 처럼 제공자 경로를 포함할 수 있다
                    if not auto.valid_gateway_model(model):
                        raise ValueError("올바른 모델 ID를 입력해 주세요.")
                    self._keys[provider] = {"key": key.strip(), "model": model, "base_url": auto.validate_base_url(payload.get("base_url"))}
                else:
                    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", model):
                        raise ValueError("올바른 모델 ID를 입력해 주세요.")
                    self._keys[provider] = {"key": key.strip(), "model": model}
            if provider == "factchat":
                config = self._keys.get(provider, {})
                return {"name": "FactChat", "configured": provider in self._keys, "model": config.get("model", ""),
                        "base_url": config.get("base_url")}
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
                "judge_input_fields": ["question", "answer", "reference", "history"],
                "external_import": COLLECTION_SCHEMA, "auto_collection": "v1"}

    def _run(self, run_id):
        run = self._runs.get(run_id)
        if run is None:
            path = self.save_dir / f"{run_id}.json"
            if not re.fullmatch(r"[a-f0-9]{12}", run_id) or not path.is_file():
                raise FileNotFoundError(run_id)
            run = json.loads(path.read_text(encoding="utf-8"))
            self._load_with_reservations(run)
            self._runs[run_id] = run
        return run

    @staticmethod
    def _interrupted(run):
        if run["status"] in ("COLLECTING", "EVALUATING"):
            for progress in run.get("progress", {}).values():
                if progress.get("stage") == "COLLECTING":
                    progress.update(stage="ERROR", active=None, timing_interrupted=True)
            run["status"], run["error"] = "ERROR", "서버 재시작으로 실행이 중단되었습니다. 저장된 원문과 결과를 확인해 주세요."
            ComparisonService._clear_inflight(run)
            ComparisonService._finalize_auto_collection(run)   # 반복해도 같다 — 남은 예약을 정산한 뒤 한 번 더 (_load_with_reservations)

    # --- 자동 수집 요청 예약 기록 — 요청을 보내기 전에 디스크에 남겨, 재시작해도 이미 청구됐을 수 있는 요청을 사용량으로 센다 ---
    @staticmethod
    def _reservations_path(save_dir, run_id):
        return Path(save_dir) / "auto-reservations" / f"{run_id}.json"   # 실행 JSON 과 다른 폴더 (실행 목록에 섞이지 않게)

    def _write_reservations(self, run_id, entries):
        """보낸 뒤 정산이 실행 기록에 저장되지 않은 요청 {요청 번호: {"cost_usd", "day"}} 을 기록한다 (비면 지움).
        실패하면 False — 그 요청은 보내지 않는다."""
        path = self._reservations_path(self.save_dir, run_id)
        try:
            if not entries:
                path.unlink(missing_ok=True)
                return True
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"requests": entries}), encoding="utf-8")
            temporary.replace(path)
            return True
        except OSError:
            return False

    def _clear_reservations(self, run_id):
        try:
            self._reservations_path(self.save_dir, run_id).unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _recover_reservations(run, save_dir):
        """정산이 실행 기록에 저장되지 않은 요청 — 제공자가 이미 청구했을 수 있으므로 예약한 상한 비용으로 사용량에 더한다.

        실행 상태와 상관없이(중단·취소·오류) 기록이 있으면 본다. 이미 정산한 번호(settled_requests)는 건너뛰어 여러 번 불러도
        두 번 세지 않는다. 더한 것이 있으면 True — 호출한 쪽이 실행을 저장한 뒤 기록을 지운다."""
        budget = (run.get("auto_collection") or {}).get("budget")
        if not isinstance(budget, dict):
            return False
        try:
            entries = json.loads(ComparisonService._reservations_path(save_dir, run["run_id"]).read_text(encoding="utf-8"))["requests"]
        except (OSError, ValueError, TypeError, KeyError):
            return False
        settled = budget.setdefault("settled_requests", [])
        recovered = {"calls": 0, "cost_usd": 0.0}
        for rid, entry in (entries.items() if isinstance(entries, dict) else []):
            if rid in settled or not isinstance(entry, dict):
                continue
            cost = float(entry.get("cost_usd") or 0.0)
            auto.settle(budget, entry.get("provider"), cost, entry.get("day") or auto.today(), estimated=True)
            settled.append(rid)
            recovered["calls"] += 1
            recovered["cost_usd"] += cost
        if recovered["calls"]:
            previous = budget.get("recovered_reservations") or {"calls": 0, "cost_usd": 0.0}
            budget["recovered_reservations"] = {"calls": previous["calls"] + recovered["calls"],
                                                "cost_usd": round(previous["cost_usd"] + recovered["cost_usd"], 8)}
        return True

    def _load_with_reservations(self, run):
        """디스크에서 읽은 실행 — 재시작으로 끊긴 실행 정리, 남은 예약 정산. 바뀌었으면 저장하고, 저장한 뒤에만 예약 기록을 지운다."""
        interrupted = run['status'] in ('COLLECTING', 'EVALUATING')
        self._interrupted(run)
        recovered = self._recover_reservations(run, self.save_dir)
        if interrupted:
            self._finalize_auto_collection(run)
        if (interrupted or recovered) and self._save(run) and recovered:
            self._clear_reservations(run["run_id"])
        if recovered and run.get("run_kind") == "auto_collection" and self._auto_jobs is not None:
            self._remember_auto(run)   # 복구한 날짜로 일일 색인을 다시 맞춘다 (색인 전이면 나중에 저장된 기록에서 읽음)

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
            if run.get('status') in ('COMPLETED', 'ERROR', 'CANCELLED') and not run.get('finished_at'):
                run['finished_at'] = datetime.now(timezone.utc).isoformat()   # 이력 관리의 완료 시각
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
        return True

    @staticmethod
    def _evaluation_summary(run):
        """Question outcomes are exclusive; remaining criterion work is separate.

        A confirmed common/semantic FAIL settles a question even if another
        criterion has no verdict. ``review_required`` counts only unresolved
        questions, while ``criterion_review_required`` retains every explicit
        UNVERIFIED or INCOMPLETE criterion (including those on already failed questions).
        """
        per_model = run.get('question_count', 0)
        # 기존 비교는 AIONA 답변만 이력 요약에 센다. AIONA 가 없는 외부 수집 실행은 모든 시스템의 답변을 센다.
        providers = ['aiona'] if 'aiona' in run.get('providers', ['aiona']) else list(run.get('providers') or [])
        total = per_model * len(providers)
        scored, review, criteria_complete, criterion_review = 0, 0, 0, 0
        for provider, index in ((p, i) for p in providers for i in range(per_model)):
            batch = run.get('evaluations', {}).get(provider) or {}
            records = (batch.get('jev_eval') or {}).get('records') or []
            cases = batch.get('cases') or []
            record = (records[index] if index < len(records) else None) or {}
            case = cases[index] if index < len(cases) else None
            if not case or case.get('case_status') != 'EVALUATED':
                continue
            decisions = record.get('decisions') or {}
            criteria = record.get('criteria') if isinstance(record.get('criteria'), list) else list(decisions)
            unverified = sum((decisions.get(cid) or {}).get('status') in ('UNVERIFIED', 'INCOMPLETE')
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
                   if not k.startswith('_activity_') and k not in ("datasets", "evaluations", "config", "input_document", "evaluation_log",
                                                                   "questions")},
                **self.version_registry.metadata(run), 'evaluation_summary': self._evaluation_summary(run),
                'evaluation_conditions_summary': self._conditions_summary(run), 'evaluation_method': evaluation_method(run),
                'input_composition': input_composition(run)}

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
                    'evaluation_conditions_summary': self._conditions_summary(run), 'evaluation_method': evaluation_method(run),
                    'input_composition': input_composition(run), 'collection_comparison': collection_comparison(run)}

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
                            self._load_with_reservations(run)
                        stored[run["run_id"]] = self._status(run)
                    except (OSError, ValueError, KeyError):
                        continue
            stored.update({rid: self._status(r) for rid, r in self._runs.items()})
            runs = sorted(stored.values(), key=lambda r: r["created_at"], reverse=True)
            return runs if limit is None else runs[:limit]

    def version_history(self):
        with self._lock:
            return self.version_registry.version_history(self.history(limit=None))

    def version_comparison(self, baseline_id, target_id):
        """두 실행의 버전 비교(서버 계산) — 질문 → 기준 → 세부 기준 → 항목·코드 조건, 대화 단위 부호검정, 꼬리 표본."""
        if not baseline_id or not target_id:
            raise ValueError("기준 실행과 비교 실행의 ID 가 필요합니다.")
        with self._lock:
            return compare_runs(self.result(baseline_id), self.result(target_id))

    def code_recheck(self, run_id):
        """저장된 실행의 CODE FAIL 을 현재 엔진으로 다시 검사한 차이 (읽기 전용, 모델 호출 없음). 실행·엔진별로 한 번만 계산한다."""
        from code_eval.core import ENGINE_VERSION
        from .code_recheck import code_recheck
        run = self.result(run_id)
        if run.get('status') != 'COMPLETED':
            raise ValueError('평가가 끝난 실행만 다시 검사할 수 있습니다.')
        key = (run_id, ENGINE_VERSION, run.get('finished_at'))
        if key not in self._code_recheck_cache:
            self._code_recheck_cache[key] = code_recheck(run)
        return self._code_recheck_cache[key]

    def aiona_history(self):
        """이력 관리 — 평가가 끝난 실행마다 AIONA 결과 한 줄, 같은 질문 세트의 직전 기록 대비 조건 변화·통과율 변화 (aiona_history)."""
        with self._lock:
            runs = []
            for status in self.history(limit=None):
                if status.get('status') != 'COMPLETED':
                    continue
                try:
                    runs.append(self.result(status['run_id']))
                except (FileNotFoundError, ValueError, OSError):
                    continue
            return aiona_history(runs)

    def manage_release(self, payload):
        with self._lock:
            safe_payload = dict(payload)
            secrets = [entry.get('key') for entry in self._keys.values()]
            for field in ('notes', 'reason'):
                if isinstance(safe_payload.get(field), str) and len(safe_payload[field]) <= 2000:
                    safe_payload[field] = clean_text(safe_payload[field], secrets=secrets)
            self.version_registry.manage(safe_payload)
            return self.version_history()

    def version_template(self, run_id):
        source = self.result(run_id)
        if source['status'] != 'COMPLETED' or not source.get('aiona_release'):
            raise ValueError('평가가 종료된 AIONA 버전 기록을 기준으로 선택해 주세요.')
        cases = [{**{k: copy.deepcopy(row[k]) for k in ('case_id', 'question', 'reference', 'attachments', 'conversation_id', 'turn') if k in row},
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
            for key in ('question', 'reference', 'attachments', 'conversation_id', 'turn'):
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

    # --- 외부 수집 답변 (4단계) ---------------------------------------------------------------
    @staticmethod
    def _question_row(run, index):
        """평가에서 제외할 행의 질문 식별자. 기존 비교는 AIONA 원문, 외부 수집 실행은 질문 목록에서 읽는다."""
        return run["datasets"]["aiona"][index] if "aiona" in run["datasets"] else run["questions"][index]

    def _external_bundle(self, payload, *, resolve=False):
        files = payload.get("files")
        if files is None and isinstance(payload.get("input_text"), str):
            files = [{"name": payload.get("input_name") or "업로드 파일", "text": payload["input_text"]}]
        if files is None and isinstance(payload.get("manual"), dict):
            files = [{"name": "직접 입력", "manual": payload["manual"]}]
        options = payload.get("options", {})
        if not isinstance(options, dict):
            raise ValueError("가져오기 설정(options)은 객체여야 합니다.")
        # API 로 새로 수집할 때는 답변 없이 질문 목록만 가져와도 된다.
        return import_files(files, options, resolve=resolve, fetcher=self.relay_fetcher,
                            questions_only=bool(payload.get("api_providers")))

    def _api_selection(self, payload, question_count):
        selected = payload.get("api_providers", [])
        if (not isinstance(selected, list) or len(selected) != len(set(selected))
                or any(p not in PROVIDERS for p in selected)):
            raise ValueError("API로 함께 수집할 모델 선택이 올바르지 않습니다.")
        missing = [PROVIDERS[p] for p in selected if p not in self._keys]
        if missing:
            raise ValueError("API 키와 모델 ID가 연결되지 않은 모델: " + ", ".join(missing))
        return selected, {"providers": selected, "planned_calls": question_count * len(selected),
                          "models": {p: self._keys[p]["model"] for p in selected}}

    def _find_external_duplicate(self, fingerprint):
        """같은 내용(시스템·질문·답변)으로 이미 만든 외부 수집 평가. 재실행은 사용자가 명시할 때만 새로 만든다."""
        candidates = {rid: run for rid, run in self._runs.items()}
        if self.save_dir.is_dir():
            for path in self.save_dir.glob("*.json"):
                if path.stem in candidates:
                    continue
                try:
                    run = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(run, dict) and run.get("run_kind") == "external_import":
                    candidates[run.get("run_id")] = run
        matches = sorted((run for run in candidates.values() if run.get("run_kind") == "external_import"
                          and (run.get("external_import") or {}).get("fingerprint") == fingerprint
                          and run.get("status") != "ERROR"), key=lambda r: r.get("created_at") or "")
        return matches[0]["run_id"] if matches else None

    def preview_external(self, payload):
        """외부 수집 파일을 변환·검증만 한다. 모델·평가자·네트워크 호출 없음."""
        bundle = self._external_bundle(payload)
        data = build_run_data(bundle)
        _, plan = self._api_selection(payload, len(bundle["questions"]))
        with self._lock:
            duplicate = self._find_external_duplicate(bundle["import_fingerprint"])
        questions = bundle["questions"]
        relays = sum(1 for r in bundle["responses"] for s in r["sources"] if s.get("skip_reason") == "relay_unresolved")
        return {"schema": COLLECTION_SCHEMA, "fingerprint": bundle["import_fingerprint"], "duplicate_of": duplicate,
                "question_count": len(questions),
                "conversation_count": len({q["conversation_id"] for q in questions if q["conversation_id"] is not None}),
                "systems": data["systems"], "environment_comparison": data["environment_comparison"],
                "import": bundle["import"], "report": bundle["report"], "relays_unresolved": relays,
                "api_collection": plan if plan["providers"] else None,
                "api_blocked": ("앞 턴이 빠진 대화가 있어 API 수집으로 같은 대화 맥락을 재현할 수 없습니다."
                                if plan["providers"] and bundle["report"]["conversation_gaps"] else None),
                "questions": [{k: q[k] for k in ("case_id", "conversation_id", "turn") if q.get(k) is not None}
                              | {"question": q["question"][:300]} for q in questions[:20]]}

    def start_external(self, payload):
        """외부 수집 답변을 기존 CODE·J1~J8 평가기로 평가한다. 선택하면 같은 질문을 연결된 API 로도 수집한다."""
        rerun = payload.get("rerun", False)
        resolve = payload.get("resolve_relays", False)
        if not isinstance(rerun, bool) or not isinstance(resolve, bool):
            raise ValueError("rerun 과 resolve_relays 는 true 또는 false 여야 합니다.")
        max_tokens = payload.get("max_tokens", 8192)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 256 <= max_tokens <= 32768:
            raise ValueError("출력 토큰 한도는 256~32,768이어야 합니다.")
        evaluation_payload = {"aiona_config": copy.deepcopy(payload.get("aiona_config"))}
        self.evaluator._config(evaluation_payload)  # 잘못된 설정은 실행을 만들기 전에 거절
        bundle = self._external_bundle(payload, resolve=resolve)
        selected, plan = self._api_selection(payload, len(bundle["questions"]))
        if selected and bundle["report"]["conversation_gaps"]:
            raise ValueError("앞 턴이 빠진 대화가 있어 API 수집으로 같은 대화 맥락을 재현할 수 없습니다. "
                             "API 수집 없이 가져온 답변만 평가하거나, 빠진 턴을 채운 파일로 다시 가져와 주세요.")
        if not selected and not bundle["responses"]:
            raise ValueError("평가할 답변이 없습니다.")
        data = build_run_data(bundle)
        questions = [{k: copy.deepcopy(q[k]) for k in ("case_id", "question", "reference", "attachments", "conversation_id", "turn")
                      if q.get(k) is not None} for q in bundle["questions"]]
        config, api_systems = {}, {}
        for provider in selected:
            key = f"{provider}-api"
            if key in data["datasets"]:
                raise ValueError(f"가져온 데이터에 이미 '{key}' 시스템이 있어 API 수집 결과와 구분할 수 없습니다.")
            kind, vendor, platform = LIVE_PROVIDERS[provider]
            model = self._keys[provider]["model"]
            api_systems[key] = {"system_key": key, "label": f"{PROVIDERS[provider]} API · {model}", "provider": vendor,
                                "platform": platform, "model": model, "model_version": None, "access_mode": "api",
                                "settings": {"max_output_tokens": max_tokens},
                                "environment": {"web_search_available": False, "tools_available": [],
                                                "system_instruction": {"known": True, "note": "시스템 프롬프트를 보내지 않음"},
                                                "notes": ["이 서버의 기존 수집 경로로 질문(과 같은 대화의 앞 턴)만 보냈습니다."]}}
            config[key] = {**self._keys[provider], "provider": provider}
        with self._lock:
            if self._closing:
                raise ValueError("서버가 종료 중이므로 새 평가를 시작할 수 없습니다.")
            duplicate = self._find_external_duplicate(bundle["import_fingerprint"])
            if duplicate and not rerun and not selected:
                raise ValueError(f"같은 내용의 외부 수집 평가가 이미 있습니다 (실행 {duplicate}). "
                                 "같은 답변을 다시 평가하려면 '재실행'을 선택해 별도 실행으로 남기세요.")
            active = [r for r in self._runs.values() if r["status"] in ("COLLECTING", "EVALUATING")]
            if len(active) >= MAX_RUNS:
                raise JobCapacityError("실행 중인 비교가 많습니다. 완료 후 다시 시도해 주세요.")
            run_id = uuid.uuid4().hex[:12]
            created = datetime.now(timezone.utc).isoformat()
            n = len(questions)
            datasets = {**data["datasets"], **{key: [None] * n for key in api_systems}}
            run = {"run_id": run_id, "name": str(payload.get("name") or "외부 수집 답변 평가")[:200], "created_at": created,
                   "status": "COLLECTING" if selected else "READY", "phase": "COLLECT", "total": n * len(selected), "done": 0,
                   "question_count": n, "providers": [*data["providers"], *api_systems], "max_tokens": max_tokens,
                   "models": {**data["models"], **{k: s["label"] for k, s in api_systems.items()}},
                   "progress": {key: {"done": 0, "success": 0, "incomplete": 0, "failed": 0, "total": n, "stage": "WAITING",
                                      "active": None, "started_at": None, "finished_at": None, "elapsed_ms": 0,
                                      "duration_ms": None} for key in api_systems},
                   "datasets": datasets, "evaluations": {}, "cancel_requested": False, "error": None,
                   "auto_evaluate": True, "input_mode": "external_import", "aiona_specialized": False,
                   "run_kind": "external_import", "questions": questions,
                   "systems": data["systems"], "environment_comparison": data["environment_comparison"],
                   "external_import": {"schema": COLLECTION_SCHEMA, "fingerprint": bundle["import_fingerprint"],
                                       "import": bundle["import"], "report": bundle["report"],
                                       "question_meta": data["questions"],
                                       "api_systems": api_systems, "api_collection": plan if selected else None,
                                       "api_providers": {f"{p}-api": p for p in selected},
                                       **({"rerun_of": duplicate} if duplicate and rerun else
                                          {"same_import_as": duplicate} if duplicate else {})},
                   "aiona_release": None, "dataset_fingerprint": fingerprint(questions),
                   "source_fingerprint": bundle["import_fingerprint"]}
            if selected:
                chain_workers = min(self.collection_workers, len(self._collection_chains(questions)))
                run["collection_execution"] = {"collection_workers_per_model": chain_workers,
                                               "collection_workers_total": chain_workers * len(selected),
                                               "conversation_order": "sequential_original_turns",
                                               "request": collection_request(max_tokens)}
            self._runs[run_id] = run
            self._cancel_events[run_id] = token = threading.Event()
            self._cost_ledgers[run_id] = ledger = CostLedger()
            if not self._save(run):
                self._cancel_events.pop(run_id, None)
                self._cost_ledgers.pop(run_id, None)
                run['status'], run['error'] = 'ERROR', '실행 기록을 저장하지 못해 평가를 시작하지 않았습니다.'
                self._save(run)
                raise OSError(run['error'])
            if not selected:
                self.evaluate(run_id, evaluation_payload)
                return self.status(run_id)
        try:
            self._launch_worker(run, token, self._collect, (run, questions, config, evaluation_payload, token))
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
        return self.status(run_id)

    def _finalize_external_collection(self, run):
        """이 서버가 API 로 수집한 행을 공통 답변 형식으로 바꾼다 (원본 응답의 사용량·출처·도구 정보 포함). 반복 호출해도 같다."""
        if run.get("run_kind") != "external_import":
            return
        try:
            external = run.get("external_import") or {}
            for key, system in (external.get("api_systems") or {}).items():
                rows = run["datasets"][key]
                kind = LIVE_PROVIDERS[external["api_providers"][key]][0]
                for index, question in enumerate(run["questions"]):
                    row = rows[index]
                    if row is not None and (row.get("collection") or {}).get("collection_method") == "api_live":
                        continue
                    outcome = (row or {}).get("collection") or {"status": "NOT_COLLECTED", "error": "수집되지 않은 질문입니다."}
                    answer = (row or {}).get("answer", "")
                    response = {"answer": answer, "status": outcome.get("status", "ERROR"), "error": outcome.get("error"),
                                "collected_at": outcome.get("collected_at"), "collection_method": "api_live",
                                # 요청 시각·요청 구성도 외부 가져오기 행 형식으로 옮길 때 남긴다 (실패한 수집 포함, 10-14 P1-2)
                                **{k: outcome[k] for k in ("requested_at", "request") if k in outcome},
                                "answer_format": "markdown", "environment": {"web_search_used": False, "tools_used": []},
                                "usage": {}, "sources": [], "collection_quality": []}
                    if isinstance(outcome.get("raw"), dict):
                        try:
                            fields, notes = response_fields(parse_api_response(outcome["raw"], kind), answer=answer)
                            sources, source_notes = normalize_sources(fields["sources"], answer, "sources")
                            response.update(usage=fields["usage"], sources=sources, finish_reason=fields["finish_reason"],
                                            collection_quality=notes + source_notes, raw=outcome["raw"])
                        except ValueError:
                            response["raw"] = outcome["raw"]
                    if isinstance(outcome.get("latency_ms"), (int, float)):
                        response["usage"]["latency_ms"] = outcome["latency_ms"]
                    meta = next(q for q in run["external_import"]["question_meta"] if q["case_id"] == question["case_id"])
                    response["response_key"] = response_key({**response, "system_key": key}, meta["question_key"])
                    rows[index] = dataset_row(question, system, response, index)
                run["systems"][key] = system_summary(system, rows)
            gaps = (run["external_import"].get("report") or {}).get("conversation_gaps") or {}
            run["environment_comparison"] = environment_comparison(run["systems"], run["questions"], gaps)
        except Exception:
            run["save_error"] = "API 수집 결과를 공통 형식으로 정리하지 못했습니다. 수집 원문은 보존했습니다."

    # --- 업로드 직후 자동 수집 ------------------------------------------------------------------
    @staticmethod
    def _auto_job_key(cases, input_mode):
        """같은 업로드(질문·대화 구성·AIONA 답변이 모두 같음)는 같은 작업 — 새로고침·중복 업로드가 유료 요청을 다시 만들지 않는다."""
        encoded = json.dumps(["auto-collection-v1", input_mode, fingerprint(cases, answers=True)], ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _progress_entry(total):
        return {"done": 0, "success": 0, "incomplete": 0, "failed": 0, "total": total, "stage": "WAITING", "active": None,
                "started_at": None, "finished_at": None, "elapsed_ms": 0, "duration_ms": None}

    @staticmethod
    def _model_label(provider, config):
        # 게이트웨이는 플랫폼과 실제 모델을 함께 적는다 — 'FactChat' 만으로 모델을 나타내지 않는다
        return f"FactChat · {config['model']}" if provider == "factchat" else config["model"]

    def _remember_auto(self, run):
        info = run.get("auto_collection") or {}
        key, run_id = info.get("job_key"), run.get("run_id")
        if not key or not run_id:
            return
        current = self._auto_jobs.get(key)
        if current is None or (current[0] or "") <= (run.get("created_at") or ""):
            self._auto_jobs[key] = (run.get("created_at"), run_id)
        budget = info.get("budget") or {}
        # 마지막으로 비용을 쓴 날 (자정을 넘긴 작업 포함) — 정산 전 예약 기록의 날짜도 (재시작 직후 아직 복구 전일 수 있음)
        days = [auto.latest_day(budget)] if budget else []
        days += self._reservation_days(run_id)
        self._auto_days[run_id] = max(days) if days else None

    def _reservation_days(self, run_id):
        try:
            entries = json.loads(self._reservations_path(self.save_dir, run_id).read_text(encoding="utf-8"))["requests"]
            return [entry["day"] for entry in entries.values() if isinstance(entry, dict) and isinstance(entry.get("day"), str)]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return []

    def _ensure_auto_index(self):
        """잠금 안에서 호출. 저장된 자동 수집 기록은 서버 시작 후 처음 한 번만 읽는다."""
        if self._auto_jobs is not None:
            return
        self._auto_jobs, seen = {}, set()
        for run in list(self._runs.values()):
            if run.get("run_kind") == "auto_collection":
                self._remember_auto(run)
                seen.add(run["run_id"])
        if self.save_dir.is_dir():
            for path in self.save_dir.glob("*.json"):
                if path.stem in seen:
                    continue
                try:
                    run = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(run, dict) and run.get("run_kind") == "auto_collection":
                    self._remember_auto(run)

    def _find_auto_job(self, job_key):
        self._ensure_auto_index()
        entry = self._auto_jobs.get(job_key)
        if entry is None:
            return None
        try:
            return self._run(entry[1])
        except (FileNotFoundError, ValueError, OSError):
            self._auto_jobs.pop(job_key, None)
            return None

    def _today_used(self):
        """오늘 일일 한도에 묶인 호출 수·비용 — 날짜별로 실제 쓴 양(by_day)에, 진행 중인 작업의 남은 승인량을 더한다.

        자정을 넘겨 계속 도는 작업도 오늘 쓴 양과 남은 승인량이 오늘 한도에 묶인다. by_day 가 없는 예전 기록은 시작한 날에 모두 묶는다."""
        self._ensure_auto_index()
        day, calls, cost = auto.today(), 0, 0.0
        for run_id, budget_day in list(self._auto_days.items()):
            live = self._runs.get(run_id)
            active = bool(live) and live["status"] == "COLLECTING"
            if budget_day != day and not active:
                continue   # 오늘 비용을 쓰지 않았고 진행 중도 아닌 작업 (색인은 마지막으로 비용을 쓴 날)
            try:
                run = self._run(run_id)
            except (FileNotFoundError, ValueError, OSError):
                self._auto_days.pop(run_id, None)
                continue
            budget, active = run["auto_collection"]["budget"], run["status"] == "COLLECTING"
            used_calls, used_cost = auto.used_on(budget, day, active, budget.get("day") == day)
            calls, cost = calls + used_calls, cost + used_cost
        return calls, cost

    def auto_collection_status(self):
        """연결·자동 수집 허용·한도·오늘 사용량 (키 값은 돌려주지 않는다)."""
        policy = self.auto_policy.load()
        with self._lock:
            targets = []
            for provider, target in auto.TARGETS.items():
                state, label, reason = auto.target_state(provider, policy, self._keys)
                config = self._keys.get(provider, {})
                targets.append({"id": provider, "name": target["name"], "kind": target["kind"], "platform": target["platform"],
                                "vendor": target["vendor_label"], "note": target.get("note"),
                                "connected": provider in self._keys, "model": config.get("model", ""),
                                "base_url": config.get("base_url"), "allowed": bool(policy["allowed"].get(provider)),
                                "priced": provider in auto.LIVE_TARGETS and auto.priced(policy, provider),
                                "state": state, "label": label, "reason": reason})
            calls, cost = self._today_used()
        return {"version": "v1", "targets": targets, "policy": policy, "limits_ready": auto.limits_ready(policy),
                "today": {"day": auto.today(), "calls": calls, "cost_usd": round(cost, 6)},
                "factchat_base_urls": list(auto.FACTCHAT_BASE_URLS)}

    def update_auto_policy(self, payload):
        with self._lock:
            policy = auto.validate_policy(payload, self.auto_policy.load())
            self.auto_policy.save(policy)
        return self.auto_collection_status()

    @staticmethod
    def _auto_environment(run):
        """FactChat 게이트웨이 답변은 직접 API 호출과 실행 조건이 같다고 확인되지 않는다 — 순위를 강제로 매기지 않게 표시한다."""
        if "factchat" not in run["providers"]:
            run.pop("environment_comparison", None)
            return
        values = {p: "API 게이트웨이 (FactChat)" if p == "factchat" else "제공자 API 직접 호출"
                  for p in run["providers"] if p != "aiona"}
        run["environment_comparison"] = {
            "same_conditions": False, "systems": len(run["providers"]), "same_questions": True,
            "question_count": run["question_count"],
            "differences": [{"code": "access_mode", "label": "응답 생성 경로", "values": values, "unknown": True,
                             "detail": "FactChat 게이트웨이의 시스템 지시·도구·웹검색 조건을 확인하지 못함"}],
            "note": "FactChat 게이트웨이를 거친 답변은 직접 API 호출과 실행 조건(시스템 지시·도구·웹검색)이 같다고 확인되지 않아, "
                    "점수를 같은 조건의 성능 순위로 보지 않습니다."}

    def _auto_provider_entry(self, provider, config, plan):
        entry = {"state": "collecting", "label": "자동 수집", "reason": None, "model": config["model"],
                 "platform": auto.TARGETS[provider]["platform"], "access_mode": "api",
                 "planned_calls": plan["calls"], "planned_cost_usd": round(plan["cost_usd"], 6)}
        if provider == "factchat":
            entry.update(base_url=config["base_url"], vendor=auto.gateway_vendor(config["model"]), response_models=[])
        else:
            entry["vendor"] = auto.TARGETS[provider]["vendor"]
        return entry

    def _auto_guard(self, run, plans, providers):
        """요청 직전 승인 범위 확인(동시 요청은 예약으로 계산)과 응답 뒤 정산. 설정·권한 오류는 그 제공자의 남은 요청을 멈춘다."""
        reserves = {p: plans[p]["reserves"] for p in providers}
        prices = run["auto_collection"]["prices"]
        pending = {"calls": 0, "cost": 0.0}
        halted = {}
        lock, service = self._lock, self
        ledger, request_ids = {}, {}   # 정산이 실행 기록에 저장되기 전까지 디스크 기록에 남기는 요청 · (제공자, 행) → 요청 번호

        class Guard:
            @staticmethod
            def before(provider, index):
                with lock:
                    if provider in halted:
                        return halted[provider]
                    budget, reserve = run["auto_collection"]["budget"], reserves[provider][index]
                    if (budget["spent_calls"] + pending["calls"] + 1 > budget["authorized_calls"]
                            or budget["spent_cost_usd"] + pending["cost"] + reserve > budget["authorized_cost_usd"] + 1e-9):
                        return "승인된 호출·비용 범위를 넘을 수 있어 요청하지 않았습니다. 관리 설정의 한도를 확인한 뒤 다시 수집해 주세요."
                    rid = uuid.uuid4().hex   # 실행·재시작과 상관없이 겹치지 않는 요청 번호 (복구한 번호를 재시도가 다시 쓰지 않게)
                    ledger[rid] = {"cost_usd": reserve, "day": auto.today(), "provider": provider}
                    if not service._write_reservations(run["run_id"], ledger):
                        del ledger[rid]
                        return "요청 예약을 기록하지 못해 요청하지 않았습니다 (재시작 시 비용을 셀 수 없음). 저장 공간을 확인한 뒤 다시 수집해 주세요."
                    request_ids[(provider, index)] = rid
                    pending["calls"] += 1
                    pending["cost"] += reserve
                    return None

            @staticmethod
            def after(provider, index, outcome, status):
                with lock:
                    reserve = reserves[provider][index]
                    pending["calls"] -= 1
                    pending["cost"] -= reserve
                    usage = auto.usage_tokens(provider, outcome.get("raw"))
                    cost = auto.cost_usd(prices[provider], *usage) if usage else reserve
                    budget = run["auto_collection"]["budget"]
                    auto.settle(budget, provider, cost, auto.today(), estimated=usage is None)
                    rid = request_ids.pop((provider, index), None)
                    if rid:
                        budget.setdefault("settled_requests", []).append(rid)   # 실행 기록과 함께 저장 — 복구에서 두 번 세지 않음
                    service._auto_days[run["run_id"]] = auto.today()   # 일일 한도 색인: 마지막으로 비용을 쓴 날
                    outcome["estimated_cost_usd"] = {"amount": round(cost, 8), "basis": "usage" if usage else "upper_bound"}
                    raw = outcome.get("raw")
                    if provider == "factchat" and isinstance(raw, dict) and isinstance(raw.get("model"), str):
                        seen = run["auto_collection"]["providers"]["factchat"].setdefault("response_models", [])
                        if raw["model"] not in seen:
                            seen.append(raw["model"])
                    if status in auto.HALTING_STATUS and provider not in halted:
                        halted[provider] = (f"앞선 요청이 HTTP {status}({auto.HALTING_STATUS[status]} 확인 필요)로 실패해 이 제공자의 "
                                            "남은 요청을 멈췄습니다. 설정을 확인한 뒤 다시 수집해 주세요.")

            @staticmethod
            def saved():
                """실행 기록을 저장한 뒤 — 정산이 저장된 요청만 예약 기록에서 뺀다 (저장 전에 빼면 그 사이 꺼질 때 비용이 빠짐)."""
                with lock:
                    settled = set(run["auto_collection"]["budget"].get("settled_requests") or ())
                    done = [rid for rid in ledger if rid in settled]
                    if done:
                        for rid in done:
                            del ledger[rid]
                        service._write_reservations(run["run_id"], ledger)
        return Guard

    @staticmethod
    def _finalize_auto_collection(run):
        """제공자별 수집 결과 요약 (반복 호출해도 같다). 수집 원문·다른 제공자의 결과는 바꾸지 않는다."""
        info = run.get("auto_collection")
        if run.get("run_kind") != "auto_collection" or not info:
            return
        for provider in run["providers"]:
            if provider == "aiona":
                continue
            rows, entry = run["datasets"][provider], info["providers"].setdefault(provider, {})
            success = sum(1 for row in rows if row and (row.get("collection") or {}).get("status") == "SUCCESS")
            stage = (run.get("progress", {}).get(provider) or {}).get("stage")
            if stage in ("COLLECTING", "WAITING") and run["status"] == "COLLECTING":
                continue
            state, label = (("cancelled", "중단됨") if stage == "CANCELLED" and success < len(rows) else
                            ("completed", "수집 완료") if success == len(rows) else
                            ("partial", "일부 실패") if success else ("failed", "수집 실패"))
            failure = next(((row.get("collection") or {}).get("error") for row in rows
                            if row and (row.get("collection") or {}).get("status") != "SUCCESS"
                            and (row.get("collection") or {}).get("error")), None)
            entry.update(state=state, label=label, collected=success, total=len(rows),
                         reason=failure if state in ("partial", "failed") else None)
            if provider == "factchat":
                vendors = {auto.gateway_vendor(m) for m in entry.get("response_models") or [entry.get("model")]}
                entry["vendor"] = vendors.pop() if len(vendors) == 1 else "mixed"

    def _launch_collection(self, run, token, ledger, cases, config, guard):
        try:
            self._launch_worker(run, token, self._collect, (run, cases, config, None, token, guard, True))
        except Exception:
            with self._lock:
                if token.is_set():
                    self._mark_cancelled(run)
                else:
                    run["status"], run["error"] = "ERROR", "응답 수집 작업을 시작하지 못했습니다."
                self._cancel_events.pop(run["run_id"], None)
                self._finalize_auto_collection(run)
                self._save(run)
                self._finish_costs(run, ledger)
            raise ValueError(run["error"] or "응답 수집 작업을 시작하지 못했습니다.") from None

    def _register_auto_run(self, run, cases, config, plans, admitted):
        """잠금 안에서 호출 — 저장에 실패하면 시작하지 않는다. (token, ledger, guard) 또는 수집할 제공자가 없으면 None."""
        run_id = run["run_id"]
        self._cancel_events[run_id] = token = threading.Event()
        self._cost_ledgers[run_id] = ledger = CostLedger()
        if not self._save(run):
            self._cancel_events.pop(run_id, None)
            self._cost_ledgers.pop(run_id, None)
            run['status'], run['error'] = 'ERROR', '실행 기록을 저장하지 못해 수집을 시작하지 않았습니다.'
            self._save(run)
            raise OSError(run['error'])
        if not admitted:
            self._cancel_events.pop(run_id, None)
            self._finish_costs(run, ledger)
            return None
        return token, ledger, self._auto_guard(run, plans, admitted)

    def start_auto_collection(self, payload):
        """업로드한 AIONA Q/A 로 자동 수집 작업을 만든다. 관리 설정에서 허용·한도 안에 드는 연결된 제공자만 요청한다.

        같은 업로드는 기존 작업을 그대로 돌려주고 새 요청을 만들지 않는다 (새로 받으려면 rerun=true 를 명시).
        평가는 시작하지 않는다 — 수집이 끝나면 사용자가 평가를 시작한다.
        """
        rerun = payload.get("rerun", False)
        if not isinstance(rerun, bool):
            raise ValueError("rerun 은 true 또는 false 여야 합니다.")
        if payload.get("baseline_run_id") is not None or payload.get("source_run_id") is not None:
            raise ValueError("버전 비교·재평가는 버전 관리 기능을 사용해 주세요.")
        cases = normalize_dataset(payload)
        input_mode = payload.get("input_mode", "file")
        if input_mode not in ("file", "manual"):
            raise ValueError("입력 방식이 올바르지 않습니다.")
        if input_mode == "manual" and (len(cases) != 1 or not cases[0]["answer"].strip()):
            raise ValueError("직접 입력은 질문과 AIONA 답변 한 쌍이 필요합니다.")
        document = payload.get("input_document")
        if document is not None:
            if not isinstance(document, dict) or not isinstance(document.get("original_text"), str):
                raise ValueError("업로드 원문 기록 형식이 올바르지 않습니다.")
            document = {k: copy.deepcopy(document[k]) for k in ("original_text", "source_name", "format", "notes") if k in document}
            validate_json_numbers(document)
        policy = self.auto_policy.load()
        max_tokens = policy["max_output_tokens"]
        job_key = self._auto_job_key(cases, input_mode)
        with self._lock:
            if self._closing:
                raise ValueError("서버가 종료 중이므로 새 수집을 시작할 수 없습니다.")
            current = self.version_registry.current_release()
            expected = payload.get("expected_model_version")
            if expected is not None and expected != (current or {}).get("version"):
                raise ValueError("현재 모델 버전이 변경되었습니다. 새로고침 후 확인해 주세요.")
            previous = self._find_auto_job(job_key)
            if previous is not None and not rerun:
                return {"run": self._status(previous), "reused": True, "skipped": {}}
            active = [r for r in self._runs.values() if r["status"] in ("COLLECTING", "EVALUATING")]
            if len(active) >= MAX_RUNS:
                raise JobCapacityError("실행 중인 비교가 많습니다. 완료 후 다시 시도해 주세요.")
            states = {p: auto.target_state(p, policy, self._keys) for p in auto.TARGETS}
            eligible = [p for p in auto.LIVE_TARGETS if states[p][0] == "ready"]
            chains, n = self._collection_chains(cases), len(cases)
            plans = {p: auto.plan_provider(cases, chains, range(n), max_tokens, policy["prices"][p]) for p in eligible}
            budget = auto.empty_budget(auto.today())
            admitted, skipped = auto.admit(eligible, plans, policy, budget, self._today_used())
            config = {p: {**self._keys[p], "provider": p} for p in admitted}
            budget.update(authorized_calls=sum(plans[p]["calls"] for p in admitted),
                          authorized_cost_usd=sum(plans[p]["cost_usd"] for p in admitted),
                          providers={p: {"planned_calls": plans[p]["calls"], "planned_cost_usd": plans[p]["cost_usd"],
                                         "spent_calls": 0, "spent_cost_usd": 0.0} for p in admitted})
            providers_info = {}
            for provider in auto.TARGETS:
                state, label, reason = states[provider]
                providers_info[provider] = (self._auto_provider_entry(provider, config[provider], plans[provider]) if provider in admitted
                                            else {"state": "over_limit", "label": "한도 초과", "reason": skipped[provider]}
                                            if provider in skipped else {"state": state, "label": label, "reason": reason})
            run_id = uuid.uuid4().hex[:12]
            run = {"run_id": run_id, "name": str(payload.get("name") or "새 비교")[:200],
                   "created_at": datetime.now(timezone.utc).isoformat(), "status": "COLLECTING" if admitted else "READY",
                   "phase": "COLLECT", "total": n * len(admitted), "done": 0, "question_count": n,
                   "providers": ["aiona", *admitted], "max_tokens": max_tokens,
                   "models": {"aiona": "업로드 원문", **{p: self._model_label(p, config[p]) for p in admitted}},
                   "progress": {p: self._progress_entry(n) for p in admitted},
                   "datasets": {"aiona": cases, **{p: [None] * n for p in admitted}},
                   "evaluations": {}, "cancel_requested": False, "error": None,
                   # 평가는 수집이 끝난 뒤 사용자가 시작한다. 평가 모델에는 모델명을 가린다 (기존 4개 모델 비교와 같음).
                   "auto_evaluate": False, "blind_requested": True, "input_mode": input_mode, "aiona_specialized": False,
                   "run_kind": "auto_collection", "aiona_release": current,
                   "dataset_fingerprint": fingerprint(cases), "source_fingerprint": fingerprint(cases, answers=True),
                   "auto_collection": {"version": 1, "job_key": job_key,
                                       "source_name": str(payload.get("input_name") or payload.get("name") or "")[:200] or None,
                                       "max_output_tokens": max_tokens, "limits": copy.deepcopy(policy["limits"]),
                                       "prices": {p: copy.deepcopy(policy["prices"][p]) for p in admitted},
                                       "providers": providers_info, "budget": budget,
                                       **({"rerun_of": previous["run_id"]} if previous is not None else {})}}
            if admitted:
                chain_workers = min(self.collection_workers, len(chains))
                run["collection_execution"] = {"collection_workers_per_model": chain_workers,
                                               "collection_workers_total": chain_workers * len(admitted),
                                               "conversation_order": "sequential_original_turns",
                                               "request": collection_request(run["max_tokens"])}
            if document is not None:
                run["input_document"] = document
            self._auto_environment(run)
            self._runs[run_id] = run
            self._remember_auto(run)
            started = self._register_auto_run(run, cases, config, plans, admitted)
            if started is None:
                return {"run": self._status(run), "reused": False, "skipped": skipped}
        token, ledger, guard = started
        self._launch_collection(run, token, ledger, cases, config, guard)
        return {"run": self.status(run_id), "reused": False, "skipped": skipped}

    def collect_more(self, run_id, payload):
        """평가 시작 전 자동 수집 작업에서 실패·미수집 답변만 다시 요청하거나, 나중에 연결한 제공자를 더한다 (명시적 요청만).

        이미 받은 답변은 다시 요청하지 않고, 같은 대화의 뒤 턴은 그 답변을 이력으로 이어 쓴다.
        """
        providers = payload.get("providers")
        if (not isinstance(providers, list) or not providers or len(providers) != len(set(providers))
                or any(p not in auto.LIVE_TARGETS for p in providers)):
            raise ValueError("다시 수집할 제공자 선택이 올바르지 않습니다.")
        policy = self.auto_policy.load()
        with self._lock:
            if self._closing:
                raise ValueError("서버가 종료 중이므로 새 수집을 시작할 수 없습니다.")
            run = self._run(run_id)
            if run.get("run_kind") != "auto_collection":
                raise ValueError("자동 수집 작업에서만 다시 수집할 수 있습니다.")
            if run.get("phase") != "COLLECT" or run.get("evaluations"):
                raise ValueError("평가를 시작한 실행에는 답변을 더하거나 다시 수집할 수 없습니다. '새로 수집'으로 별도 실행을 만들어 주세요.")
            if run["status"] not in ("READY", "CANCELLED", "ERROR") or self._workers.get(run_id):
                raise ValueError("수집이 진행 중입니다. 끝난 뒤 다시 시도해 주세요.")
            active = [r for r in self._runs.values() if r["status"] in ("COLLECTING", "EVALUATING")]
            if len(active) >= MAX_RUNS:   # 새로 시작할 때와 같은 한도 — 재수집으로 동시 실행이 늘지 않게
                raise JobCapacityError("실행 중인 비교가 많습니다. 완료 후 다시 시도해 주세요.")
            info, cases = run["auto_collection"], run["datasets"]["aiona"]
            n, max_tokens = len(cases), run["max_tokens"]
            for provider in providers:
                state, label, reason = auto.target_state(provider, policy, self._keys)
                if state != "ready":
                    raise ValueError(f"{auto.TARGETS[provider]['name']}: {label} — {reason}")
                previous = info["providers"].get(provider) or {}
                if provider in run["providers"] and (previous.get("model") != self._keys[provider]["model"]
                                                     or previous.get("base_url") != self._keys[provider].get("base_url")):
                    raise ValueError(f"{auto.TARGETS[provider]['name']}의 모델 설정이 이 작업과 달라 같은 실행에 이어 수집할 수 없습니다. "
                                     "'새로 수집'으로 별도 실행을 만들어 주세요.")
            wanted = {p: [i for i, row in enumerate(run["datasets"].get(p) or [None] * n)
                          if row is None or (row.get("collection") or {}).get("status") != "SUCCESS"] for p in providers}
            wanted = {p: indices for p, indices in wanted.items() if indices}
            if not wanted:
                raise ValueError("다시 수집할 답변이 없습니다. 선택한 제공자의 답변이 모두 수집되어 있습니다.")
            chains = self._collection_chains(cases)
            plans = {p: auto.plan_provider(cases, chains, indices, max_tokens, policy["prices"][p]) for p, indices in wanted.items()}
            budget = info["budget"]
            if budget["day"] != auto.today():
                # 이어서 받는 날의 일일 한도에 이 작업의 사용량 전체를 묶는다 (보수적으로 계산)
                budget["day"] = self._auto_days[run_id] = auto.today()
            admitted, skipped = auto.admit(list(wanted), plans, policy, budget, self._today_used())
            if not admitted:
                raise ValueError("관리 설정 한도 안에서 시작할 수 없습니다. " + " ".join(skipped.values()))
            config = {p: {**self._keys[p], "provider": p} for p in admitted}
            info["prices"].update({p: copy.deepcopy(policy["prices"][p]) for p in admitted})
            for provider in admitted:
                plan = plans[provider]
                if provider not in run["providers"]:
                    run["providers"].append(provider)
                    run["datasets"][provider] = [None] * n
                    run["models"][provider] = self._model_label(provider, config[provider])
                    info["providers"][provider] = self._auto_provider_entry(provider, config[provider], plan)
                else:
                    entry = info["providers"][provider]
                    entry.update(state="collecting", label="다시 수집", reason=None,
                                 planned_calls=entry.get("planned_calls", 0) + plan["calls"],
                                 planned_cost_usd=round(entry.get("planned_cost_usd", 0) + plan["cost_usd"], 6))
                kept = n - len(wanted[provider])
                run["progress"][provider] = {**self._progress_entry(n), "done": kept, "success": kept}
                spent = budget["providers"].setdefault(provider, {"planned_calls": 0, "planned_cost_usd": 0.0,
                                                                  "spent_calls": 0, "spent_cost_usd": 0.0})
                spent["planned_calls"] += plan["calls"]
                spent["planned_cost_usd"] += plan["cost_usd"]
            budget["authorized_calls"] = budget["spent_calls"] + sum(plans[p]["calls"] for p in admitted)
            budget["authorized_cost_usd"] = budget["spent_cost_usd"] + sum(plans[p]["cost_usd"] for p in admitted)
            chain_workers = min(self.collection_workers, len(chains))
            run.update(status="COLLECTING", total=sum(len(wanted[p]) for p in admitted), done=0, error=None, cancel_requested=False,
                       revision=run.get("revision", 0) + 1,
                       collection_execution={"collection_workers_per_model": chain_workers,
                                             "collection_workers_total": chain_workers * len(admitted),
                                             "conversation_order": "sequential_original_turns"})
            self._auto_environment(run)
            token, ledger, guard = self._register_auto_run(run, cases, config, plans, admitted)
        self._launch_collection(run, token, ledger, cases, config, guard)
        return {"run": self.status(run_id), "skipped": skipped}

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
                    "conversation_order": "sequential_original_turns", "request": collection_request(run["max_tokens"])}
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
            self._runs[run_id] = run
            self._cancel_events[run_id] = token = threading.Event()
            self._cost_ledgers[run_id] = ledger = CostLedger()
            if not self._save(run):
                self._cancel_events.pop(run_id, None)
                self._cost_ledgers.pop(run_id, None)
                run['status'], run['error'] = 'ERROR', '실행 기록을 저장하지 못해 평가를 시작하지 않았습니다.'
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
        result = {k: copy.deepcopy(case[k]) for k in ("case_id", "question", "reference", "attachments", "conversation_id", "turn") if k in case}
        result.update({"answer": outcome.get("answer", ""), "system_id": provider,
                       "source": model, "case_index": index, "collection": outcome})
        if outcome["status"] != "SUCCESS":
            # The downloaded JSON is also safe to feed directly into the
            # existing CLI/UI evaluator. Preserve partial text but exclude it.
            result["__parse_error__"] = "응답 수집 실패·중단 또는 미완료: " + outcome.get("error", "평가 제외")
        return result

    def _collect(self, run, cases, config, evaluation_payload=None, cancel_event=None, guard=None, skip_success=False):
        # Stable original indices remain in JSON even when turns are reordered
        # for requests. AIONA's answers are never passed as another model's history.
        # guard (자동 수집): 요청 직전 승인 범위 확인·응답 뒤 정산. skip_success: 이미 받은 답변은 다시 요청하지 않고 이력으로 쓴다.
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
                                    kept = run["datasets"][provider][index] if skip_success else None
                                    if not broken and kept is not None and (kept.get("collection") or {}).get("status") == "SUCCESS":
                                        history = history + [{"role": "user", "content": cases[index]["question"]},
                                                             {"role": "assistant", "content": kept["answer"]}]
                                        continue
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
                                        question, images, delivery = attachment_delivery.prepare(
                                            case["question"], case.get("attachments"), self.attachment_roots)
                                        messages = list(history) + [{"role": "user", "content": question}]
                                        if token.is_set() or stopped.is_set():
                                            return
                                        blocked = guard.before(provider, index) if guard else None
                                        if blocked:
                                            outcome = {"status": "ERROR", "error": blocked, "not_requested": True}
                                        else:
                                            request_started, status_code = time.monotonic(), None
                                            requested_at = datetime.now(timezone.utc).isoformat()
                                            try:
                                                outcome = request_answer(settings.get("provider", provider), settings, messages, run["max_tokens"], client,
                                                    usage_observer=(lambda raw: ledger.record_collection(provider, settings['model'], raw)) if ledger else None,
                                                    images=images)
                                            except ProviderError as exc:
                                                outcome = {"status": "ERROR", "error": str(exc)}
                                                status_code = exc.status
                                            outcome["latency_ms"] = round((time.monotonic() - request_started) * 1000, 1)
                                            # 요청·수신 시각 — 실패한 수집도 남긴다 (재현 기록, 판정에는 쓰지 않음)
                                            outcome["requested_at"] = requested_at
                                            outcome["collected_at"] = datetime.now(timezone.utc).isoformat()
                                            outcome["request"] = _request_shape(messages, len(case.get("attachments") or []), delivery)
                                            if guard:
                                                guard.after(provider, index, outcome, status_code)
                                        if run.get("run_kind") in ("external_import", "auto_collection") and "collected_at" not in outcome:
                                            outcome["collected_at"] = datetime.now(timezone.utc).isoformat()   # 요청하지 않은 행(한도 차단)
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
                                        if self._save(run) and guard:
                                            guard.saved()
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
                self._finalize_external_collection(run)
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
                self._finalize_external_collection(run)
                if run.get("phase") != "EVALUATE":
                    if token.is_set() or run["cancel_requested"]:
                        self._mark_cancelled(run)
                    else:
                        run["status"], run["error"] = "ERROR", "응답 수집을 완료하지 못했습니다. 수집된 원문은 보존했습니다."
                    self._finish_costs(run, ledger)
        finally:
            with self._lock:
                self._finalize_auto_collection(run)
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
            run.pop('finished_at', None)   # 다시 평가하면 완료 시각은 새 평가가 끝날 때 기록한다
            self._cancel_events[run_id] = token = threading.Event()
            # Histories contain already-collected responses, so semantic checks of
            # different turns are independent. Freeze equal concurrency per model.
            workers_per_provider = max(1, self.semantic_workers // len(run["providers"]))
            run["evaluation_execution"] = {
                "semantic_workers_per_model": workers_per_provider,
                "semantic_workers_total": workers_per_provider * len(run["providers"]),
                # 의미 평가 방식(legacy | items)과 호출 방식 — 호출은 실시간만(Message Batches 는 2026-10-13 제거)
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
            if run.get("run_kind") == "external_import":
                semantic_contexts = {provider: _external_semantic_context(run["systems"][provider]) for provider in run["providers"]}
            for semantic_context in semantic_contexts.values():   # 평가 모델에 보낼 첨부 그림도 답변 수집과 같은 폴더에서 찾는다
                semantic_context["attachment_roots"] = [str(root) for root in self.attachment_roots]
            if run.get("run_kind") == "auto_collection" and "factchat" in semantic_contexts:
                # 게이트웨이가 모델에 도구·검색을 붙였는지 확인할 수 없다 — '도구 없이 생성'으로 단정하지 않는다
                semantic_contexts["factchat"]["answer_tools"] = "unknown"
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
            # 평가 로그에도 방식·기준 버전을 남긴다 — 시작 때는 쓸 버전(계획), 끝날 때 저장 결과에서 센 실제 판정 수로 바꾼다
            from code_eval.semantic_items import ITEMS, SEMANTIC_ITEMS_VERSION, SUBS
            planned = {"pipeline": pipeline, "pipeline_source": "evaluation_execution", "criteria_version": jev_eval.JEV_CRITERIA_VERSION,
                       "items_version": SEMANTIC_ITEMS_VERSION if pipeline == "items" else None,
                       "catalog": {"criteria": len(jev_eval.CRITERIA), "subs": len(SUBS) if pipeline == "items" else None,
                                   "items": len(ITEMS) if pipeline == "items" else None}, "judged": None}
            stream.publish({"type": "run_start", "total": run["question_count"] * len(run["providers"]), "method": planned})
            if not self._save(run):
                run['status'], run['error'] = 'ERROR', '실행 기록을 저장하지 못해 평가를 시작하지 않았습니다.'
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
                # 화면용 이벤트는 실제 모델명 — 블라인드는 평가 모델에 보내는 요청에만 적용한다
                label = "AIONA" if provider == "aiona" else PROVIDERS.get(provider) or run["models"].get(provider, provider)
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
                    original = row if row is not None else self._question_row(run, index)
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
                        stream.publish({"type": "run_error", "reason": run["error"], "method": evaluation_method(run)})
                    else:
                        run["status"] = "COMPLETED"
                        stream.publish({"type": "run_done", "method": evaluation_method(run)})
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
