"""API + 내장 도구 수집 — 질문·참조 자료·첨부파일을 함께 보내고 모델의 웹 검색 도구를 켜서 답변을 받는다 (2026-10-14 사용자 결정).

웹 서비스(ChatGPT·Claude·Gemini)는 검색·파일 읽기 같은 도구를 붙여 답하므로, 도구 없는 API 보다 이 방식이 실제 서비스 답변에
가깝다. 질문 세트 실행(질문 생성 → 수집 → 평가)에서 GPT·Claude·Gemini 답변을 이 방식으로 모은다.

- 첨부: 이미지(PNG)·PDF·TXT·DOCX 모두 원본 파일로 보낸다 (2026-10-14 사용자 요청, 세 API 에 작은 파일로 실제 확인).
  GPT 는 이미지 ``input_image``, 나머지 ``input_file``(파일 이름·MIME 그대로). Gemini 는 모두 ``inline_data``.
  Claude 는 이미지 ``image``, PDF ``document``(base64), TXT ``document``(text 원본·제목=파일 이름), DOCX 는 메시지 문서로 받지 않아
  Files API 에 올리고 ``container_upload`` + 코드 실행 도구로 원본을 열게 한다 — 요청이 끝나면 올린 파일을 지운다.
  ``sent[].how`` 에 보낸 방식을 남기고, 보낸 첨부만 평가 데이터에 남긴다(받지 못한 파일로 J8 을 평가하지 않게).
- 참조 자료(J7): 질문 앞에 '[참조 자료]' 로 붙여 보낸다 — J7 입력 = 질문 + 참조 자료.
- 멀티턴: ``history`` = 앞 턴의 (질문 행, 그 모델의 답변). 앞 턴의 참조 자료·첨부도 그 턴 메시지에 다시 넣어 보낸다 — 같은 채팅을
  이어 가는 AIONA 와 입력 조건을 맞춘다 (Codex 리뷰: 글자만 남기면 다음 요청에 앞 턴 첨부가 사라짐).
- 응답은 업로드 화면과 같은 ``parse_api_response`` 로 읽어 구조화 출처(OpenAI annotations·Claude citations·Gemini grounding)를
  ``sources`` 로 남기고, 평가기에 넘길 ``answer_sources`` 도 만든다 (Codex 리뷰: 본문에 URL 이 없으면 출처가 사라지던 문제).
- 코드 실행 도구는 Claude 의 DOCX 첨부가 있는 요청에만 넣는다(그 밖에는 검색만). Claude 가 도구 실행 중 멈추면(pause_turn)
  같은 응답을 이어 보내 최대 2회 이어 받는다. 키는 기록하지 않는다.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import httpx

from .ingestion.api_adapter import parse_api_response
from .ingestion.sources import evaluation_sources, normalize_sources

__all__ = ["AGENT_COLLECTION_VERSION", "ask_with_tools"]

#: v2: 앞 턴 첨부·참조 재전송, 구조화 출처 보존, 응답 모양 검증 · v3: TXT·DOCX 도 원본 파일로 (Claude DOCX 는 Files API + 코드 실행)
AGENT_COLLECTION_VERSION = "api-tools-v3"
_MIME = {"pdf": "application/pdf", "image": "image/png", "txt": "text/plain",
         "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
_ANTHROPIC = "https://api.anthropic.com/v1"
_CODE_EXECUTION = {"type": "code_execution_20260120", "name": "code_execution"}
MAX_CONTINUATIONS = 2   # Claude pause_turn 이어 받기 횟수
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
    """(provider 중립 조각 목록, 보낸 첨부 기록). 조각: ('text', 글) | ('file', kind, mime, name, base64, 원본 bytes).
    알 수 없는 종류의 첨부는 파일로 보낼 수 없어 글자로 붙이고 기록에 남긴다."""
    parts: list = []
    sent: List[Dict[str, Any]] = []
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        kind = a.get("type")
        record = {"name": a.get("name"), "type": kind, "sha256": a.get("sha256")}
        if kind in _MIME:
            data = _file_bytes(a, files_dir)
            parts.append(("file", kind, _MIME[kind], a.get("name"), base64.b64encode(data).decode(), data))
            record["how"] = "파일 그대로"
        else:
            parts.append(("text", f"[첨부파일: {a.get('name')}]\n{a.get('content') or ''}"))
            record["how"] = "글자로 보냄 (알 수 없는 종류)"
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


def _anthropic_content(parts: list, uploaded: Dict[int, str]) -> list:
    """Claude 메시지 조각. DOCX 는 미리 올린 파일 id(``uploaded``: 조각 id() → file_id)로 container_upload 한다."""
    content = []
    for p in parts:
        if p[0] == "text":
            content.append({"type": "text", "text": p[1]})
        elif p[1] == "image":
            content.append({"type": "image", "source": {"type": "base64", "media_type": p[2], "data": p[4]}})
        elif p[1] == "txt":   # 텍스트 문서 — 원본 글을 문서 블록으로 (제목 = 파일 이름)
            content.append({"type": "document", "source": {"type": "text", "media_type": "text/plain",
                                                           "data": p[5].decode("utf-8", errors="replace")}, "title": p[3]})
        elif p[1] == "docx":
            content.append({"type": "container_upload", "file_id": uploaded[id(p)]})
        else:
            content.append({"type": "document", "source": {"type": "base64", "media_type": p[2], "data": p[4]}})
    return content


def _gemini_parts(parts: list) -> list:
    return [{"text": p[1]} if p[0] == "text" else {"inline_data": {"mime_type": p[2], "data": p[4]}} for p in parts]


def _openai(config, turns, parts, max_tokens):
    messages = [m for prior, answer in turns
                for m in ({"role": "user", "content": _openai_content(prior)}, {"role": "assistant", "content": answer})]
    return ("https://api.openai.com/v1/responses", {"Authorization": f"Bearer {config['key']}"},
            {"model": config["model"], "input": messages + [{"role": "user", "content": _openai_content(parts)}],
             "tools": [{"type": "web_search"}], "max_output_tokens": max_tokens, "store": False})


def _anthropic(config, turns, parts, max_tokens, uploaded=None):
    uploaded = uploaded or {}
    messages = [m for prior, answer in turns
                for m in ({"role": "user", "content": _anthropic_content(prior, uploaded)}, {"role": "assistant", "content": answer})]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]
    if uploaded:   # DOCX 원본은 코드 실행 컨테이너에서 연다
        tools.append(dict(_CODE_EXECUTION))
    return (f"{_ANTHROPIC}/messages", {"x-api-key": config["key"], "anthropic-version": "2023-06-01"},
            {"model": config["model"], "max_tokens": max_tokens,
             "messages": messages + [{"role": "user", "content": _anthropic_content(parts, uploaded)}], "tools": tools})


def _upload_docx(client: httpx.Client, config: Dict[str, Any], all_parts: List[list], uploaded: Dict[int, str]) -> None:
    """Claude 요청에 들어갈 DOCX 조각(앞 턴 포함)을 Files API 에 올려 ``uploaded``({id(조각): file_id})에 채운다.

    ``uploaded`` 는 호출부가 가진다 — 도중에 실패해도 그때까지 올린 파일은 호출부의 정리(finally)가 지운다 (Codex 리뷰)."""
    headers = {"x-api-key": config["key"], "anthropic-version": "2023-06-01"}
    for parts in all_parts:
        for p in parts:
            if p[0] != "file" or p[1] != "docx":
                continue
            try:
                response = client.post(f"{_ANTHROPIC}/files", headers=headers, files={"file": (str(p[3]), p[5], p[2])})
            except httpx.HTTPError:
                raise _Failure("DOCX 첨부를 Claude Files API 에 올리지 못했습니다 (네트워크)") from None
            try:
                body = response.json() if response.status_code < 400 else None
            except ValueError:
                body = None
            file_id = body.get("id") if isinstance(body, dict) else None
            if not isinstance(file_id, str) or not file_id:
                raise _Failure(f"DOCX 첨부를 Claude Files API 에 올리지 못했습니다 (HTTP {response.status_code})")
            uploaded[id(p)] = file_id


def _delete_uploads(client: httpx.Client, config: Dict[str, Any], uploaded: Dict[int, str]) -> List[str]:
    """올린 파일을 지운다(실패하면 한 번 더) → 끝내 지우지 못한 file_id 목록. 수집 결과와 따로 기록해 나중에 정리할 수 있게 한다."""
    headers = {"x-api-key": config["key"], "anthropic-version": "2023-06-01"}
    failed = []
    for file_id in uploaded.values():
        for attempt in (1, 2):
            try:
                if client.delete(f"{_ANTHROPIC}/files/{file_id}", headers=headers).status_code < 400:
                    break
            except httpx.HTTPError:
                pass
            if attempt == 2:
                failed.append(file_id)
    return failed


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


_ERROR_ID = re.compile(r"[A-Za-z][A-Za-z_]{1,48}")


def _error_label(response: httpx.Response) -> str:
    """오류 응답 → ' (invalid_request_error · 크레딧·사용 한도 부족)' 같은 고정 설명. 제공자의 오류 문구는 기록하지 않는다 —
    입력(질문·첨부)이 되돌아올 수 있다 (Codex 리뷰). 오류 종류는 식별자 모양(영문·밑줄)일 때만 남긴다."""
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return ""
    ids = [v for key in ("type", "code", "status") if isinstance(v := error.get(key), str) and _ERROR_ID.fullmatch(v)]
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    quota = re.search(r"credit balance|billing|purchase credits|insufficient[_ ]quota|exceeded your current quota", message, re.I)
    parts = [*dict.fromkeys(ids), *(["크레딧·사용 한도 부족 — 제공자 콘솔에서 충전 필요"] if quota else [])]
    return f" ({' · '.join(parts)})" if parts else ""


def _post(client: httpx.Client, url: str, headers: Dict[str, str], body: Dict[str, Any]) -> Dict[str, Any]:
    """요청 1회 → 응답 JSON 객체. 네트워크·HTTP·JSON·객체 아님은 _Failure."""
    try:
        response = client.post(url, headers=headers, json=body)
    except httpx.HTTPError:
        raise _Failure("네트워크 요청 실패 또는 시간 초과") from None
    if response.status_code >= 400:
        raise _Failure(f"HTTP {response.status_code}{_error_label(response)}")
    try:
        raw = response.json()
    except ValueError:
        raise _Failure("응답 JSON 을 읽지 못했습니다.") from None
    if not isinstance(raw, dict):
        raise _Failure("응답이 JSON 객체가 아닙니다.")
    return raw


def _claude_blocks(raw: Dict[str, Any]) -> list:
    """Claude 응답의 content 목록 — 목록이 아니면 빈 답변으로 바꾸지 않고 _Failure (Codex 리뷰)."""
    if not isinstance(raw.get("content"), list):
        raise _Failure("응답에 content 목록이 없습니다.")
    return list(raw["content"])


def _add_usage(total: Dict[str, Any], usage: Any) -> None:
    """이어 받기 응답들의 토큰·서버 도구 사용량을 더한다."""
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
        elif key == "server_tool_use" and isinstance(value, dict):
            inner = total.setdefault("server_tool_use", {})
            for name, count in value.items():
                if isinstance(count, int) and not isinstance(count, bool):
                    inner[name] = inner.get(name, 0) + count


def ask_with_tools(provider: str, config: Dict[str, Any], question: str, attachments: Optional[List[Dict[str, Any]]],
                   files_dir: Path, client: httpx.Client, max_tokens: int = 8000, *, reference: Any = None,
                   history: Sequence[Turn] = ()) -> Dict[str, Any]:
    """질문 + 참조 자료 + 첨부 → {answer, status(SUCCESS|INCOMPLETE|ERROR), finish_reason, tools_used, usage, sources,
    answer_sources?, retrieved_sources?, environment, quality, sent, error, method}.

    예상할 수 있는 실패(첨부 없음·읽기 오류·네트워크·HTTP 오류·응답 모양)는 예외 대신 status=ERROR 로 돌려준다."""
    base: Dict[str, Any] = {"method": AGENT_COLLECTION_VERSION, "sent": []}
    uploaded: Dict[int, str] = {}
    try:
        turns = [_turn_parts(entry, files_dir) for entry in history]
        parts, sent = _parts(question, reference, attachments or [], files_dir)
        base["sent"] = sent
        if provider == "claude":
            _upload_docx(client, config, [prior for prior, _ in turns] + [parts], uploaded)
            for record in sent:
                if record.get("type") == "docx" and record.get("how") == "파일 그대로":
                    record["how"] = "파일 그대로 (Files API · 코드 실행 컨테이너)"
            url, headers, body = _anthropic(config, turns, parts, max_tokens, uploaded)
        else:
            url, headers, body = {"gpt": _openai, "gemini": _gemini}[provider](config, turns, parts, max_tokens)
        raw = _post(client, url, headers, body)
        if provider == "claude":   # 도구 실행 중 멈춤(pause_turn) — 같은 응답·같은 컨테이너로 이어 보내 받는다
            blocks, usage = _claude_blocks(raw), {}
            _add_usage(usage, raw.get("usage"))
            for _ in range(MAX_CONTINUATIONS):
                if raw.get("stop_reason") != "pause_turn":
                    break
                body = {**body, "messages": body["messages"] + [{"role": "assistant", "content": _claude_blocks(raw)}]}
                container = raw.get("container") if isinstance(raw.get("container"), dict) else {}
                if isinstance(container.get("id"), str) and container["id"]:   # 컨테이너를 이어 써야 DOCX·실행 상태가 남는다
                    body["container"] = container["id"]
                raw = _post(client, url, headers, body)
                blocks += _claude_blocks(raw)
                _add_usage(usage, raw.get("usage"))
            raw = {**raw, "content": blocks, "usage": usage}
        parsed = _read(provider, raw)
    except _Failure as exc:
        failure = str(exc)
    else:
        failure = None
    finally:
        if uploaded:   # 정리 실패는 답변 수집과 따로 남긴다 — file_id 로 나중에 지울 수 있다
            failed = _delete_uploads(client, config, uploaded)
            if failed:
                base["cleanup_failed"] = {"provider_files": failed, "note": "Claude Files API 에서 지우지 못한 첨부 — 콘솔·API 로 정리"}
    if failure is not None:
        return {**base, "answer": "", "status": "ERROR", "error": failure}
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
