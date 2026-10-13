"""API + 내장 도구 수집 — 질문·참조 자료·첨부파일을 함께 보내고 모델의 웹 검색 도구를 켜서 답변을 받는다 (2026-10-14 사용자 결정).

웹 서비스(ChatGPT·Claude·Gemini)는 검색·파일 읽기 같은 도구를 붙여 답하므로, 도구 없는 API 보다 이 방식이 실제 서비스 답변에
가깝다. 질문 세트 실행(질문 생성 → 수집 → 평가)에서 GPT·Claude·Gemini 답변을 이 방식으로 모은다.

- 첨부: 이미지(PNG)·PDF 는 파일 그대로 보낸다. TXT·DOCX 는 세 API 가 모두 원본을 받지 않으므로 파일 안의 글자를 붙여 보내고
  ``sent[].how`` 에 '글자로 보냄'을 남긴다. 보낸 첨부만 평가 데이터에 남긴다(받지 못한 파일로 J8 을 평가하지 않게).
- 참조 자료(J7): 질문 앞에 '[참조 자료]' 로 붙여 보낸다 — J7 입력 = 질문 + 참조 자료.
- 멀티턴: ``history`` = 앞 턴의 (질문 행, 그 모델의 답변). 앞 턴의 참조 자료·첨부도 그 턴 메시지에 다시 넣어 보낸다 — 같은 채팅을
  이어 가는 AIONA 와 입력 조건을 맞춘다 (Codex 리뷰: 글자만 남기면 다음 요청에 앞 턴 첨부가 사라짐).
- 응답은 업로드 화면과 같은 ``parse_api_response`` 로 읽어 구조화 출처(OpenAI annotations·Claude citations·Gemini grounding)를
  ``sources`` 로 남기고, 평가기에 넘길 ``answer_sources`` 도 만든다 (Codex 리뷰: 본문에 URL 이 없으면 출처가 사라지던 문제).
- 코드 실행 도구는 넣지 않았다(검색만). 키는 기록하지 않는다.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import httpx

from .ingestion.api_adapter import parse_api_response
from .ingestion.sources import evaluation_sources, normalize_sources

__all__ = ["AGENT_COLLECTION_VERSION", "ask_with_tools"]

AGENT_COLLECTION_VERSION = "api-tools-v2"   # v2: 앞 턴 첨부·참조 재전송, 구조화 출처 보존, 응답 모양 검증
_MIME = {"pdf": "application/pdf", "image": "image/png"}
_KIND = {"gpt": "openai_responses", "claude": "anthropic_messages", "gemini": "gemini_generate"}
#: 정상 종료 사유 — 이 사유로 끝난 빈 답변은 성공('아무것도 출력하지 마' 요청에 지시대로 빈 답을 낸 경우, 실제 수집에서 확인)
_COMPLETE = {"gpt": ("completed",), "claude": ("end_turn", "stop_sequence", "refusal"), "gemini": ("STOP",)}
#: 앞 턴 — (질문 글 또는 질문 행 {question, reference?, attachments?}, 그 모델의 답변)
Turn = Tuple[Union[str, Dict[str, Any]], str]


class _Failure(Exception):
    pass


def _file_bytes(attachment: Dict[str, Any], files_dir: Path) -> bytes:
    name = attachment.get("name")
    try:
        path = (files_dir / str(attachment.get("path") or "")).resolve()
        if not path.is_relative_to(files_dir.resolve()) or not path.is_file():
            raise _Failure(f"첨부파일이 없습니다: {name}")
        return path.read_bytes()
    except OSError as exc:   # 읽기 권한·잠김·경로 오류 — 예외 대신 수집 오류로 남긴다
        raise _Failure(f"첨부파일을 읽지 못했습니다: {name} ({type(exc).__name__})") from None


def _parts(question: str, reference: Any, attachments: List[Dict[str, Any]], files_dir: Path) -> Tuple[list, List[Dict[str, Any]]]:
    """(provider 중립 조각 목록, 보낸 첨부 기록). 조각: ('text', 글) | ('file', kind, mime, name, base64)."""
    parts: list = []
    sent: List[Dict[str, Any]] = []
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
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


def _turn_parts(entry: Turn, files_dir: Path) -> Tuple[list, str]:
    """앞 턴 → (그 턴에 보낸 조각, 답변). 질문 행이면 참조 자료·첨부까지 그대로 다시 만든다."""
    prior, answer = entry
    if isinstance(prior, dict):
        parts, _ = _parts(str(prior.get("question") or ""), prior.get("reference"), prior.get("attachments") or [], files_dir)
    else:
        parts = [("text", str(prior))]
    return parts, answer if isinstance(answer, str) else ""


def _openai_content(parts: list) -> list:
    content = []
    for p in parts:
        if p[0] == "text":
            content.append({"type": "input_text", "text": p[1]})
        elif p[1] == "image":
            content.append({"type": "input_image", "image_url": f"data:{p[2]};base64,{p[4]}"})
        else:
            content.append({"type": "input_file", "filename": p[3], "file_data": f"data:{p[2]};base64,{p[4]}"})
    return content


def _anthropic_content(parts: list) -> list:
    content = []
    for p in parts:
        if p[0] == "text":
            content.append({"type": "text", "text": p[1]})
        else:
            block = "image" if p[1] == "image" else "document"
            content.append({"type": block, "source": {"type": "base64", "media_type": p[2], "data": p[4]}})
    return content


def _gemini_parts(parts: list) -> list:
    return [{"text": p[1]} if p[0] == "text" else {"inline_data": {"mime_type": p[2], "data": p[4]}} for p in parts]


def _openai(config, turns, parts, max_tokens):
    messages = [m for prior, answer in turns
                for m in ({"role": "user", "content": _openai_content(prior)}, {"role": "assistant", "content": answer})]
    return ("https://api.openai.com/v1/responses", {"Authorization": f"Bearer {config['key']}"},
            {"model": config["model"], "input": messages + [{"role": "user", "content": _openai_content(parts)}],
             "tools": [{"type": "web_search"}], "max_output_tokens": max_tokens, "store": False})


def _anthropic(config, turns, parts, max_tokens):
    messages = [m for prior, answer in turns
                for m in ({"role": "user", "content": _anthropic_content(prior)}, {"role": "assistant", "content": answer})]
    return ("https://api.anthropic.com/v1/messages", {"x-api-key": config["key"], "anthropic-version": "2023-06-01"},
            {"model": config["model"], "max_tokens": max_tokens, "messages": messages + [{"role": "user", "content": _anthropic_content(parts)}],
             "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]})


def _gemini(config, turns, parts, max_tokens):
    contents = [c for prior, answer in turns
                for c in ({"role": "user", "parts": _gemini_parts(prior)}, {"role": "model", "parts": [{"text": answer}]})]
    return (f"https://generativelanguage.googleapis.com/v1beta/models/{config['model']}:generateContent", {"x-goog-api-key": config["key"]},
            {"contents": contents + [{"role": "user", "parts": _gemini_parts(parts)}], "tools": [{"google_search": {}}],
             "generationConfig": {"maxOutputTokens": max_tokens}})


def _text_blocks(provider: str, raw: Dict[str, Any]) -> List[Any]:
    """답변 본문이 될 블록의 text 값들 — 글자가 아닌 값이 하나라도 있으면 그 응답은 모양 오류다 (빈 글자 ""는 정상).
    본문을 담는 그릇(message.content·content·parts)이 목록이 아니어도 모양 오류(_Failure)다."""
    if provider == "gpt":
        output = raw.get("output") if isinstance(raw.get("output"), list) else []
        texts = []
        for m in output:
            if not (isinstance(m, dict) and m.get("type") == "message"):
                continue
            if not isinstance(m.get("content"), list):
                raise _Failure("응답 본문 모양이 틀립니다 (message.content).")
            texts += [b.get("text") if b.get("type") == "output_text" else b.get("refusal")
                      for b in m["content"] if isinstance(b, dict) and b.get("type") in ("output_text", "refusal")]
        return texts
    if provider == "claude":
        return [b.get("text") for b in raw.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"]
    candidates = raw.get("candidates") if isinstance(raw.get("candidates"), list) else []
    candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    content = candidate.get("content")
    if content is None:
        return []
    if not isinstance(content, dict) or (content.get("parts") is not None and not isinstance(content.get("parts"), list)):
        raise _Failure("응답 본문 모양이 틀립니다 (content.parts).")
    return [p.get("text") for p in content.get("parts") or [] if isinstance(p, dict) and "text" in p and not p.get("thought")]


def _read(provider: str, raw: Any) -> Dict[str, Any]:
    """응답 → parse_api_response 결과. 답변이 들어갈 자리조차 없는 응답(output null 등)과 글자가 아닌 본문은 빈 답변과 구분해
    실패로 둔다 (Codex 리뷰: 파서가 조용히 버린 본문이 빈 정상 답변으로 보이던 문제)."""
    if not isinstance(raw, dict):
        raise _Failure("응답이 JSON 객체가 아닙니다.")
    if provider == "gpt":
        output = raw.get("output") if isinstance(raw.get("output"), list) else []
        if not any(isinstance(m, dict) and m.get("type") == "message" for m in output):
            raise _Failure("응답에 message 출력이 없습니다.")
    elif provider == "claude":
        if not isinstance(raw.get("content"), list):
            raise _Failure("응답에 content 목록이 없습니다.")
    elif not (isinstance(raw.get("candidates"), list) and raw["candidates"]) and not isinstance(raw.get("promptFeedback"), dict):
        raise _Failure("응답에 candidates 가 없습니다.")   # promptFeedback 만 있으면 차단 응답 — 미완료로 남긴다
    try:   # 본문 검증과 읽기를 같은 범위에서 — 모양 오류는 어떤 것도 예외로 새지 않고 ERROR 가 된다 (Codex 리뷰)
        if any(not isinstance(text, str) for text in _text_blocks(provider, raw)):
            raise _Failure("응답 본문이 글자가 아닙니다.")
        return parse_api_response(raw, _KIND[provider])
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise _Failure(f"응답 모양을 읽지 못했습니다 ({type(exc).__name__})") from None


def ask_with_tools(provider: str, config: Dict[str, Any], question: str, attachments: Optional[List[Dict[str, Any]]],
                   files_dir: Path, client: httpx.Client, max_tokens: int = 8000, *, reference: Any = None,
                   history: Sequence[Turn] = ()) -> Dict[str, Any]:
    """질문 + 참조 자료 + 첨부 → {answer, status(SUCCESS|INCOMPLETE|ERROR), finish_reason, tools_used, usage, sources,
    answer_sources?, retrieved_sources?, environment, quality, sent, error, method}.

    예상할 수 있는 실패(첨부 없음·읽기 오류·네트워크·HTTP 오류·응답 모양)는 예외 대신 status=ERROR 로 돌려준다."""
    base = {"method": AGENT_COLLECTION_VERSION, "sent": []}
    try:
        turns = [_turn_parts(entry, files_dir) for entry in history]
        parts, sent = _parts(question, reference, attachments or [], files_dir)
        base["sent"] = sent
        url, headers, body = {"gpt": _openai, "claude": _anthropic, "gemini": _gemini}[provider](config, turns, parts, max_tokens)
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
        parsed = _read(provider, raw)
    except _Failure as exc:
        return {**base, "answer": "", "status": "ERROR", "error": str(exc)}
    answer, finish = parsed["answer"], parsed["finish_reason"]
    complete = parsed["status"] == "SUCCESS" or (not answer and finish in _COMPLETE[provider])
    sources, notes = normalize_sources(parsed["sources"], answer, "collection.sources")
    environment = parsed.get("environment") or {}
    out = {**base, "answer": answer, "status": "SUCCESS" if complete else "INCOMPLETE",
           "error": None if complete else f"응답 미완료 ({finish})", "finish_reason": finish,
           "tools_used": list(environment.get("tools_used") or []), "environment": environment,
           "usage": {k: v for k, v in (parsed.get("usage") or {}).items() if v is not None},
           "sources": sources, "quality": [*(parsed.get("quality") or []), *notes]}
    if parsed.get("retrieved_sources"):
        out["retrieved_sources"] = parsed["retrieved_sources"]
    judged = evaluation_sources(sources)
    if judged:   # 평가기(J5 citation_checks)가 대조할 출처
        out["answer_sources"] = judged
    return out
