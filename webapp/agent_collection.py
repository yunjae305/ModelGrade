"""API + 내장 도구 수집 — 질문·참조 자료·첨부파일을 함께 보내고 모델의 웹 검색 도구를 켜서 답변을 받는다 (2026-10-14 사용자 결정).

웹 서비스(ChatGPT·Claude·Gemini)는 검색·파일 읽기 같은 도구를 붙여 답하므로, 도구 없는 API 보다 이 방식이 실제 서비스 답변에
가깝다. 질문 세트 실행(질문 생성 → 수집 → 평가)에서 GPT·Claude·Gemini 답변을 이 방식으로 모은다.

- 첨부: 이미지(PNG)·PDF 는 파일 그대로 보낸다. TXT·DOCX 는 세 API 가 모두 원본을 받지 않으므로 파일 안의 글자를 붙여 보내고
  ``sent[].how`` 에 '글자로 보냄'을 남긴다. 보낸 첨부만 평가 데이터에 남긴다(받지 못한 파일로 J8 을 평가하지 않게).
- 참조 자료(J7): 질문 앞에 '[참조 자료]' 로 붙여 보낸다 — J7 입력 = 질문 + 참조 자료.
- 멀티턴: ``history`` = 앞 턴의 (질문, 그 모델의 답변) — 첨부는 그 턴에만 보낸다.
- 코드 실행 도구는 넣지 않았다(검색만). 키는 기록하지 않는다.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

__all__ = ["AGENT_COLLECTION_VERSION", "ask_with_tools"]

AGENT_COLLECTION_VERSION = "api-tools-v1"
_MIME = {"pdf": "application/pdf", "image": "image/png"}


class _Failure(Exception):
    pass


def _file_bytes(attachment: Dict[str, Any], files_dir: Path) -> bytes:
    path = (files_dir / str(attachment.get("path") or "")).resolve()
    if not path.is_relative_to(files_dir.resolve()) or not path.is_file():
        raise _Failure(f"첨부파일이 없습니다: {attachment.get('name')}")
    return path.read_bytes()


def _parts(question: str, reference: Any, attachments: List[Dict[str, Any]], files_dir: Path) -> Tuple[list, List[Dict[str, Any]]]:
    """(provider 중립 조각 목록, 보낸 첨부 기록). 조각: ('text', 글) | ('file', kind, mime, name, base64)."""
    parts: list = []
    sent: List[Dict[str, Any]] = []
    for a in attachments or []:
        kind = a.get("type")
        record = {"name": a.get("name"), "type": kind, "sha256": a.get("sha256")}
        if kind in _MIME:
            parts.append(("file", kind, _MIME[kind], a.get("name"), base64.b64encode(_file_bytes(a, files_dir)).decode()))
            record["how"] = "파일 그대로"
        else:
            parts.append(("text", f"[첨부파일: {a.get('name')}]\n{a.get('content') or ''}"))
            record["how"] = "글자로 보냄"
        sent.append(record)
    text = question
    if reference not in (None, "", {}, []):
        text = f"[참조 자료]\n{reference if isinstance(reference, str) else str(reference)}\n\n{question}"
    parts.append(("text", text))
    return parts, sent


def _openai(config, history, parts, max_tokens):
    content = []
    for p in parts:
        if p[0] == "text":
            content.append({"type": "input_text", "text": p[1]})
        elif p[1] == "image":
            content.append({"type": "input_image", "image_url": f"data:{p[2]};base64,{p[4]}"})
        else:
            content.append({"type": "input_file", "filename": p[3], "file_data": f"data:{p[2]};base64,{p[4]}"})
    messages = [m for q, a in history for m in ({"role": "user", "content": q}, {"role": "assistant", "content": a})]
    return ("https://api.openai.com/v1/responses", {"Authorization": f"Bearer {config['key']}"},
            {"model": config["model"], "input": messages + [{"role": "user", "content": content}], "tools": [{"type": "web_search"}],
             "max_output_tokens": max_tokens, "store": False})


def _anthropic(config, history, parts, max_tokens):
    content = []
    for p in parts:
        if p[0] == "text":
            content.append({"type": "text", "text": p[1]})
        else:
            block = "image" if p[1] == "image" else "document"
            content.append({"type": block, "source": {"type": "base64", "media_type": p[2], "data": p[4]}})
    messages = [m for q, a in history for m in ({"role": "user", "content": q}, {"role": "assistant", "content": a})]
    return ("https://api.anthropic.com/v1/messages", {"x-api-key": config["key"], "anthropic-version": "2023-06-01"},
            {"model": config["model"], "max_tokens": max_tokens, "messages": messages + [{"role": "user", "content": content}],
             "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]})


def _gemini(config, history, parts, max_tokens):
    out = [{"text": p[1]} if p[0] == "text" else {"inline_data": {"mime_type": p[2], "data": p[4]}} for p in parts]
    contents = [c for q, a in history for c in ({"role": "user", "parts": [{"text": q}]}, {"role": "model", "parts": [{"text": a}]})]
    return (f"https://generativelanguage.googleapis.com/v1beta/models/{config['model']}:generateContent", {"x-goog-api-key": config["key"]},
            {"contents": contents + [{"role": "user", "parts": out}], "tools": [{"google_search": {}}],
             "generationConfig": {"maxOutputTokens": max_tokens}})


def _read(provider: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """응답 → {answer, finish_reason, complete, tools_used, usage}. 거절 본문도 답변으로 남긴다(J1 평가 대상)."""
    if not isinstance(raw, dict):
        raise _Failure("응답이 JSON 객체가 아닙니다.")
    if provider == "gpt":
        output = raw.get("output") if isinstance(raw.get("output"), list) else []
        blocks = [b for m in output if isinstance(m, dict) and m.get("type") == "message"
                  for b in (m.get("content") if isinstance(m.get("content"), list) else []) if isinstance(b, dict)]
        texts = [b.get("text") if b.get("type") == "output_text" else b.get("refusal") for b in blocks if b.get("type") in ("output_text", "refusal")]
        finish = raw.get("status", "unknown")
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        return {"answer": "".join(t for t in texts if isinstance(t, str)), "finish_reason": finish, "complete": finish == "completed",
                "tools_used": [m.get("type") for m in output if isinstance(m, dict) and m.get("type") not in ("message", "reasoning")],
                "usage": {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}}
    if provider == "claude":
        blocks = [b for b in raw.get("content") or [] if isinstance(b, dict)]
        finish = raw.get("stop_reason", "unknown")
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        return {"answer": "".join(b.get("text") or "" for b in blocks if b.get("type") == "text"), "finish_reason": finish,
                "complete": finish in ("end_turn", "stop_sequence", "refusal"),
                "tools_used": [b.get("type") for b in blocks if b.get("type") != "text"],
                "usage": {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}}
    candidates = raw.get("candidates") if isinstance(raw.get("candidates"), list) else []
    candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    finish = candidate.get("finishReason", "blocked")
    usage = raw.get("usageMetadata") if isinstance(raw.get("usageMetadata"), dict) else {}
    grounding = candidate.get("groundingMetadata") if isinstance(candidate.get("groundingMetadata"), dict) else {}
    parts = (candidate.get("content") or {}).get("parts") if isinstance(candidate.get("content"), dict) else []
    return {"answer": "".join(p.get("text") or "" for p in parts or [] if isinstance(p, dict) and "text" in p and not p.get("thought")),
            "finish_reason": finish, "complete": finish == "STOP",
            "tools_used": ["google_search"] if grounding.get("webSearchQueries") else [],
            "usage": {"input_tokens": usage.get("promptTokenCount"), "output_tokens": usage.get("candidatesTokenCount")}}


def ask_with_tools(provider: str, config: Dict[str, Any], question: str, attachments: Optional[List[Dict[str, Any]]],
                   files_dir: Path, client: httpx.Client, max_tokens: int = 8000, *, reference: Any = None,
                   history: Sequence[Tuple[str, str]] = ()) -> Dict[str, Any]:
    """질문 + 참조 자료 + 첨부 → {answer, status(SUCCESS|INCOMPLETE|ERROR), finish_reason, tools_used, usage, sent, error, method}.

    예상할 수 있는 실패(첨부 없음·네트워크·HTTP 오류·응답 모양)는 예외 대신 status=ERROR 로 돌려준다."""
    base = {"method": AGENT_COLLECTION_VERSION, "sent": []}
    try:
        parts, sent = _parts(question, reference, attachments or [], files_dir)
        base["sent"] = sent
        url, headers, body = {"gpt": _openai, "claude": _anthropic, "gemini": _gemini}[provider](config, list(history), parts, max_tokens)
        try:
            response = client.post(url, headers=headers, json=body)
        except httpx.HTTPError:
            raise _Failure("네트워크 요청 실패 또는 시간 초과") from None
        if response.status_code >= 400:
            detail = ""
            try:
                error = response.json().get("error")
                detail = str(error.get("message") if isinstance(error, dict) else error or "")[:300]
            except (ValueError, AttributeError):
                pass
            raise _Failure(f"HTTP {response.status_code} {detail}".strip())
        try:
            raw = response.json()
        except ValueError:
            raise _Failure("응답 JSON 을 읽지 못했습니다.") from None
        out = _read(provider, raw)
    except _Failure as exc:
        return {**base, "answer": "", "status": "ERROR", "error": str(exc)}
    ok = out.pop("complete") and bool(out["answer"].strip())
    return {**base, **out, "status": "SUCCESS" if ok else "INCOMPLETE", "error": None if ok else f"응답 미완료 ({out['finish_reason']})"}
