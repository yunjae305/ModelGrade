"""Claude Message Batches API 의 얇은 HTTP 클라이언트 — 제출·상태 조회·결과(JSONL) 받기·취소. 판정 로직 없음.

- ``POST /messages/batches`` {requests: [{custom_id, params}]} → {id, processing_status, request_counts, results_url, expires_at, …}
- ``GET /messages/batches/{id}`` → 같은 모양 (processing_status: in_progress | canceling | ended)
- ``GET results_url`` → JSONL. 줄마다 {custom_id, result: {type: succeeded|errored|canceled|expired, message?|error?}}
- ``POST /messages/batches/{id}/cancel`` → processing_status canceling
- 결과는 제출 순서와 무관하므로 반드시 custom_id 로 대응한다. 인증값·프롬프트는 기록에 남기지 않는다.
- 네트워크·HTTP 오류는 ``BatchApiError`` 로 올린다(재시도 없음). 디스패처(jev_batch)가 폴링 주기·만료 재전송·취소를 정한다.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional

import httpx

from .io import validate_json_numbers
from .llm_client import redact

__all__ = ["BatchApiError", "AnthropicBatchClient", "BATCH_STATUSES", "RESULT_TYPES"]

BATCH_STATUSES = ("in_progress", "canceling", "ended")
RESULT_TYPES = ("succeeded", "errored", "canceled", "expired")
_HEADERS_VERSION = "2023-06-01"


class BatchApiError(RuntimeError):
    """Batches API 호출 실패 — 메시지는 인증값을 가린 것이며 HTTP 상태를 담는다."""

    def __init__(self, message: str, *, http_status: Optional[int] = None):
        super().__init__(message)
        self.http_status = http_status


class AnthropicBatchClient:
    """``api_base`` 는 AnthropicSemanticConfig.api_base(…/v1). ``transport`` 는 테스트용 httpx.MockTransport."""

    def __init__(self, api_base: str, api_key: str, *, transport: Any = None, timeout: float = 60.0):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("ANTHROPIC_API_KEY가 없습니다.")
        self._base = api_base.rstrip("/") + "/"
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self._base, timeout=self._timeout, follow_redirects=False, transport=self._transport,
                            headers={"x-api-key": self._api_key, "anthropic-version": _HEADERS_VERSION})

    def _request(self, method: str, url: str, *, payload: Optional[Dict[str, Any]] = None) -> httpx.Response:
        try:
            with self._client() as client:
                return client.request(method, url, json=payload, headers={"Content-Type": "application/json"} if payload is not None else None)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise BatchApiError(f"Claude Batches API 연결 실패: {redact(f'{type(exc).__name__}: {exc}')}") from None

    @staticmethod
    def _json(response: httpx.Response, what: str) -> Dict[str, Any]:
        if not 200 <= response.status_code < 300:
            raise BatchApiError(f"Claude Batches API HTTP {response.status_code} ({what})", http_status=response.status_code)
        try:
            raw = response.json()
            validate_json_numbers(raw)
        except (ValueError, TypeError):
            raise BatchApiError(f"Claude Batches API 응답이 JSON 객체가 아님 ({what})", http_status=response.status_code) from None
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise BatchApiError(f"Claude Batches API 응답에 묶음 ID 가 없음 ({what})", http_status=response.status_code)
        return raw

    @staticmethod
    def summary(raw: Dict[str, Any]) -> Dict[str, Any]:
        """저장·화면용 묶음 상태 — {id, status, counts{processing, succeeded, errored, canceled, expired}, results_url, expires_at, created_at, ended_at}."""
        counts = raw.get("request_counts") if isinstance(raw.get("request_counts"), dict) else {}
        status = raw.get("processing_status")
        return {"id": raw.get("id"), "status": status if status in BATCH_STATUSES else "unknown",
                "counts": {key: int(counts.get(key) or 0) for key in ("processing", "succeeded", "errored", "canceled", "expired")},
                "results_url": raw.get("results_url") if isinstance(raw.get("results_url"), str) else None,
                "expires_at": raw.get("expires_at"), "created_at": raw.get("created_at"), "ended_at": raw.get("ended_at")}

    def create(self, requests: List[Dict[str, Any]]) -> Dict[str, Any]:
        """requests = [{"custom_id": str, "params": Messages 요청 본문}]. 반환은 summary()."""
        if not requests:
            raise ValueError("빈 묶음은 제출하지 않습니다.")
        for entry in requests:
            if not isinstance(entry, dict) or not isinstance(entry.get("custom_id"), str) or not isinstance(entry.get("params"), dict):
                raise ValueError("묶음 요청은 {custom_id, params} 여야 합니다.")
        payload = {"requests": [{"custom_id": e["custom_id"], "params": e["params"]} for e in requests]}
        validate_json_numbers(payload)
        return self.summary(self._json(self._request("POST", "messages/batches", payload=payload), "create"))

    def retrieve(self, batch_id: str) -> Dict[str, Any]:
        return self.summary(self._json(self._request("GET", f"messages/batches/{batch_id}"), "retrieve"))

    def cancel(self, batch_id: str) -> Dict[str, Any]:
        return self.summary(self._json(self._request("POST", f"messages/batches/{batch_id}/cancel", payload={}), "cancel"))

    def results(self, results_url: str) -> Iterator[Dict[str, Any]]:
        """결과 JSONL 을 줄 단위로 — {custom_id, type, message?, error?}. 형식이 깨진 줄은 type 'invalid' 로 넘긴다."""
        response = self._request("GET", results_url)
        if not 200 <= response.status_code < 300:
            raise BatchApiError(f"Claude Batches API HTTP {response.status_code} (results)", http_status=response.status_code)
        for line in response.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                validate_json_numbers(row)
            except (ValueError, TypeError):
                yield {"custom_id": None, "type": "invalid", "error": "결과 줄이 JSON 이 아님"}
                continue
            result = row.get("result") if isinstance(row, dict) else None
            custom_id = row.get("custom_id") if isinstance(row, dict) else None
            kind = result.get("type") if isinstance(result, dict) else None
            if not isinstance(custom_id, str) or kind not in RESULT_TYPES:
                yield {"custom_id": custom_id if isinstance(custom_id, str) else None, "type": "invalid", "error": "결과 줄 형식이 다름"}
                continue
            entry: Dict[str, Any] = {"custom_id": custom_id, "type": kind}
            if kind == "succeeded":
                entry["message"] = result.get("message") if isinstance(result.get("message"), dict) else None
            elif kind == "errored":
                error = result.get("error") if isinstance(result.get("error"), dict) else {}
                inner = error.get("error") if isinstance(error.get("error"), dict) else error
                entry["error"] = redact(str(inner.get("message") or inner.get("type") or "errored"))[:300]
            yield entry
