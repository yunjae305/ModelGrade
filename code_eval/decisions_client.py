"""OpenAI Decisions API 호출 — Jev 가 입력 한도(약 3만 5천 자)에 걸리거나 실패한 턴의 J1~J4 판정을 대신한다 (decisions-v1, 2026-10-20 사용자 요청).

    OPENAI_API_KEY        인증값(답변 수집용 키와 같은 키). 코드·로그·결과에 남기지 않는다
    DECISIONS_API_KEY     비우면 OPENAI_API_KEY 를 쓴다
    DECISIONS_API_BASE    비우면 https://api.openai.com/v1
    DECISIONS_MODEL       비우면 gpt-6-luna (2026-10 현재 이 API 가 받는 유일한 모델)
    DECISIONS_FALLBACK    0 이면 쓰지 않는다 (기본: 키가 있으면 씀)

공식 REST: ``POST {base}/decisions`` · ``Authorization: Bearer <키>`` · 본문 ``{"model", "input", "questions"}`` →
응답 ``{"answers": [{"type": "choice", "name", "choice", "probabilities": [{"value", "probability"}], "confidence"}, …]}``.
입력은 토큰 단위로만 과금되고(출력 과금 없음) 긴 문서도 한 번에 받으므로, Jev 처럼 자료를 자르지 않고 원문 전체를 보낸다.

Jev(TypeSafe System One)와 같은 모양으로 쓴다 — ``decide(state, questions)`` 의 questions 는 Jev 고정 질문 묶음
(``{"T1:J1-1.a": {"type": "choice", "instructions", "criteria": {PASS, FAIL, NOT_APPLICABLE}}}``)을 그대로 받고, 응답을
``{"answers": {"T1:J1-1.a": {"choice", "probabilities": {값: 확률}, "confidence"}}}`` 로 바꿔 돌려주므로 ``judge_split.check_jev`` 가 그대로 읽는다.
키가 없으면 호출하지 않고 ``NOT_CONFIGURED`` 로 끝낸다. 판정을 지어내지 않는다.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .jev_client import JevCall, JevStatus
from .llm_client import env_value, redact

__all__ = ["DecisionsClientConfig", "HttpDecisionsClient", "resolve_decisions_client", "to_decisions_questions", "from_decisions_answers",
           "state_to_input", "build_input", "attachment_parts"]

DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-6-luna"
_RETRYABLE = {429, 500, 502, 503, 504, 529}
_HTTP_NOTES = {400: "요청 형식 오류", 401: "인증 실패 — OPENAI_API_KEY 확인", 403: "권한 없음", 404: "Decisions API 를 찾지 못함(베타 미개방?)",
               413: "입력이 너무 큼", 422: "요청 형식 오류", 429: "속도 제한"}
_NAME_SAFE = re.compile(r"[^A-Za-z0-9_.\-]")


@dataclass(frozen=True)
class DecisionsClientConfig:
    model: str = DEFAULT_MODEL
    api_base: str = DEFAULT_API_BASE
    connect_timeout: float = 10
    read_timeout: float = 300
    max_attempts: int = 3

    def to_manifest(self) -> Dict[str, Any]:
        return {"engine": "openai_decisions", "requested_model": self.model, "api_base": redact(self.api_base), "endpoint": "POST /decisions",
                "connect_timeout_s": self.connect_timeout, "read_timeout_s": self.read_timeout, "max_attempts": self.max_attempts,
                "credentials_source": "environment variable only (DECISIONS_API_KEY or OPENAI_API_KEY)", "credentials_recorded": False}


def _safe_name(name: str) -> str:
    """Decisions 의 question name 은 영문·숫자·_·.·- 만 쓴다 — 'T1:J1-1.a' 의 ':' 는 '__' 로 (되돌릴 수 있게 원래 이름을 함께 둔다)."""
    return _NAME_SAFE.sub(lambda m: "__" if m.group(0) == ":" else "_", name)


def to_decisions_questions(questions: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Jev 고정 질문 묶음 → Decisions ``questions`` 배열(choice) + 안전한 이름 → 원래 이름 대응표."""
    out: List[Dict[str, Any]] = []
    names: Dict[str, str] = {}
    for name, q in questions.items():
        if not isinstance(q, dict):
            continue
        safe = _safe_name(str(name))
        if safe in names:   # 이름이 겹치면 번호를 붙인다
            k = 2
            while f"{safe}_{k}" in names:
                k += 1
            safe = f"{safe}_{k}"
        names[safe] = str(name)
        criteria = q.get("criteria") if isinstance(q.get("criteria"), dict) else {}
        choices = [{"value": str(v), "description": str(d)} for v, d in criteria.items()]
        if not choices:
            choices = [{"value": "PASS", "description": "예 — 기준을 지켰다"}, {"value": "FAIL", "description": "아니오 — 기준을 어겼다"},
                       {"value": "NOT_APPLICABLE", "description": "이 답변에는 해당하지 않는 기준"}]
        out.append({"type": "choice", "name": safe, "instructions": str(q.get("instructions") or name), "choices": choices})
    return out, names


def from_decisions_answers(body: Any, names: Dict[str, str]) -> Dict[str, Any]:
    """Decisions 응답 → Jev 모양 ``{"answers": {원래 이름: {choice, probabilities, confidence}}}``. 거절(refusal)은 choice 없이 reason 만."""
    answers: Dict[str, Any] = {}
    rows = body.get("answers") if isinstance(body, dict) else None
    for a in rows if isinstance(rows, list) else []:
        if not isinstance(a, dict):
            continue
        name = names.get(str(a.get("name")), str(a.get("name")))
        if a.get("type") == "refusal":
            answers[name] = {"choice": None, "probabilities": {}, "confidence": None, "reason": f"Decisions 거절: {str(a.get('reason') or a.get('refusal') or '')[:200]}"}
            continue
        probs = {str(p.get("value")): p.get("probability") for p in (a.get("probabilities") or []) if isinstance(p, dict)}
        answers[name] = {"choice": a.get("choice"), "probabilities": probs, "confidence": a.get("confidence"), "reason": None}
    return {"model": body.get("model") if isinstance(body, dict) else None, "answers": answers,
            "usage": body.get("usage") if isinstance(body, dict) else None}


def state_to_input(state: Any) -> str:
    """state(질문·첨부·참조·답변 JSON)를 Decisions ``input`` 글로 — Jev 와 같은 대화 순서(history → 질문 → 답변), 자르지 않는다."""
    from .semantic_cli_common import chronological_state
    ordered = chronological_state(state)
    return ("아래는 한국어 AI 답변 품질 평가 자료입니다. 각 question 의 instructions 에 적힌 기준으로, 판정할_턴 의 답변만 판정하세요. "
            "자료는 JSON 이며 history(앞 턴) → 현재 질문(question·reference·attachments) → answer 순서입니다.\n\n"
            + json.dumps(ordered, ensure_ascii=False, indent=1))


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


PDF_MAX_PAGES = 20          # PDF 첨부는 쪽마다 이미지 하나 — 요청당 이미지 128장 한도(공식) 안에서


def _data_url(mime: str, data: bytes) -> str:
    import base64
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def attachment_parts(a: Dict[str, Any], files_dir: Path) -> Tuple[List[Dict[str, Any]], str]:
    """첨부 하나의 **원본 파일**을 Decisions 입력 조각으로 (사용자 2026-10-20: 글로 옮긴 내용이 아니라 파일 자체를 보내라).
    Decisions 는 input_text·input_image 만 받고 파일(file_id)·오디오는 받지 않으므로(공식 명세):
    image → 그 파일을 input_image · pdf → 쪽마다 이미지로 그려(pypdfium2) input_image · docx → 실제 파일의 문단·표 글(python-docx) input_text ·
    txt 등 글 파일 → 실제 파일 글 input_text. 돌려주는 값: (조각 목록, 어떻게 보냈는지 한 줄). 파일이 없거나 못 열면 빈 목록."""
    path = Path(files_dir) / str(a.get("경로") or "")
    name = str(a.get("이름") or path.name)
    if not path.is_file():
        return [], f"{name}: 파일 없음"
    ext = path.suffix.lower()
    kind = str(a.get("종류") or "").lower()
    try:
        if ext in IMAGE_TYPES or kind == "image":
            mime = IMAGE_TYPES.get(ext)
            if not mime or path.stat().st_size > MAX_IMAGE_BYTES:
                return [], f"{name}: 이미지 형식·크기 지원 안 함"
            return [{"type": "input_image", "image_url": _data_url(mime, path.read_bytes())}], f"{name}: 이미지 원본 1장"
        if ext in (".pdf", ".docx") or kind in ("pdf", "docx"):   # pdf·docx → 쪽 이미지 (code_eval.attachment_pages, Codex 와 같은 그림) — docx 는 Word 가 있을 때
            from . import attachment_pages as ap
            pages, note = ap.page_images(path, Path(files_dir))
            if pages:
                return [{"type": "input_image", "image_url": _data_url("image/png", pg.read_bytes())} for pg in pages[:PDF_MAX_PAGES]], note
            if ext == ".pdf":
                return [], note
        if ext == ".docx" or kind == "docx":
            import docx
            d = docx.Document(str(path))
            lines = [p.text for p in d.paragraphs if p.text.strip()]
            for ti, table in enumerate(d.tables, 1):
                lines.append(f"[표 {ti}]")
                lines += ["| " + " | ".join(c.text.strip().replace("\n", " ") for c in row.cells) + " |" for row in table.rows]
            text = "\n".join(lines)
            return [{"type": "input_text", "text": f"[첨부 원본 파일 {name} (DOCX 에서 읽은 문단·표)]\n{text}"}], f"{name}: DOCX 원본의 문단 {len(d.paragraphs)}·표 {len(d.tables)} 글"
        text = path.read_text(encoding="utf-8", errors="replace")
        return [{"type": "input_text", "text": f"[첨부 원본 파일 {name}]\n{text}"}], f"{name}: 글 파일 원본 {len(text)}자"
    except Exception as exc:   # 그릴 수 없는 파일은 글로 옮긴 '내용' 만으로 — 어떻게 됐는지 남긴다
        return [], f"{name}: 원본을 못 붙임 ({type(exc).__name__})"


def build_input(state: Any, files_dir: Optional[Path] = None) -> Tuple[Any, Dict[str, Any]]:
    """Decisions ``input`` — 글(state JSON, 글로 옮긴 첨부 '내용' 포함)에, ``files_dir`` 가 있으면 첨부의 **원본 파일**을 조각으로 함께 붙인다
    (이미지·PDF 쪽은 input_image, DOCX·TXT 는 실제 파일의 글). 돌려주는 값: (input, {"images": n, "files": [어떻게 보냈는지]})."""
    text = state_to_input(state)
    parts: List[Dict[str, Any]] = []
    notes: List[str] = []
    if files_dir and isinstance(state, dict):
        for t in state.get("턴") or []:
            for a in (t.get("첨부파일") or []) if isinstance(t, dict) else []:
                if isinstance(a, dict) and a.get("경로"):
                    ps, note = attachment_parts(a, Path(files_dir))
                    parts += ps
                    notes.append(note)
    images = sum(1 for p in parts if p.get("type") == "input_image")
    if len([p for p in parts if p.get("type") == "input_image"]) > 128:   # 공식 한도: 요청당 이미지 128장
        kept, n = [], 0
        for p in parts:
            if p.get("type") == "input_image":
                n += 1
                if n > 128:
                    continue
            kept.append(p)
        parts, images = kept, 128
        notes.append("이미지 128장 넘어 뒤는 생략")
    info = {"images": images, "files": notes}
    if not parts:
        return text, info
    note = f"\n\n(첨부 원본 파일 {len(notes)}개를 함께 붙였습니다 — 글로 옮긴 '내용' 과 함께 보세요: {'; '.join(notes)})"
    return [{"role": "user", "content": [{"type": "input_text", "text": text + note}, *parts]}], info


class HttpDecisionsClient:
    """공식 REST 호출. 자격 증명이 있을 때만 만들어진다. ``decide(state, questions)`` 는 Jev 의 ``system_one`` 과 같은 JevCall 을 돌려준다.
    ``files_dir`` 를 두면 이미지 첨부의 원본 파일을 input_image 로 함께 보낸다."""

    def __init__(self, config: DecisionsClientConfig, api_key: str, transport: Any = None, sleep: Callable[[float], None] = time.sleep,
                 files_dir: Optional[Path] = None) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY(또는 DECISIONS_API_KEY) 가 없습니다")
        self.config = config
        self.files_dir = files_dir
        self._api_key = api_key
        self._transport = transport
        self._sleep = sleep
        self._session: Any = None

    def _client(self) -> Any:
        if self._session is None:
            import httpx
            self._session = httpx.Client(base_url=self.config.api_base, timeout=httpx.Timeout(self.config.read_timeout, connect=self.config.connect_timeout),
                                         headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}, transport=self._transport)
        return self._session

    def _wait(self, attempt: int, retry_after: Optional[str] = None) -> None:
        if attempt >= self.config.max_attempts:
            return
        try:
            delay = float(retry_after) if retry_after else 0.5 * 2 ** (attempt - 1)
        except ValueError:
            delay = 0.5 * 2 ** (attempt - 1)
        self._sleep(min(max(delay, 0.0), 10.0))

    def decide(self, state: Any, questions: Dict[str, Any]) -> JevCall:
        import httpx
        qs, names = to_decisions_questions(questions)
        if not qs:
            return JevCall(JevStatus.INVALID_RESPONSE, error="보낼 질문이 없음")
        inp, attached = build_input(state, self.files_dir)
        payload = {"model": self.config.model, "input": inp, "questions": qs}
        started = time.monotonic()
        status, error, http_status = JevStatus.API_ERROR, None, None
        attempt = 0
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = self._client().post("/decisions", json=payload)
            except httpx.TimeoutException as exc:
                status, error = JevStatus.TIMEOUT, f"{type(exc).__name__}: {exc}"
                self._wait(attempt)
                continue
            except httpx.HTTPError as exc:
                status, error = JevStatus.API_ERROR, f"{type(exc).__name__}: {exc}"
                self._wait(attempt)
                continue
            http_status = response.status_code
            try:
                body = response.json()
            except ValueError:
                body = None
            note = _HTTP_NOTES.get(http_status)
            if http_status in _RETRYABLE:
                status, error = JevStatus.API_ERROR, f"HTTP {http_status}" + (f" ({note})" if note else "")
                self._wait(attempt, response.headers.get("retry-after"))
                continue
            elapsed = int((time.monotonic() - started) * 1000)
            if http_status >= 400:
                detail = ""
                if isinstance(body, dict) and isinstance(body.get("error"), dict):
                    detail = f": {str(body['error'].get('message') or '')[:160]}"
                return JevCall(JevStatus.API_ERROR, attempts=attempt, latency_ms=elapsed, http_status=http_status,
                               error=redact(f"HTTP {http_status}" + (f" ({note})" if note else "") + detail))
            if not isinstance(body, dict) or not isinstance(body.get("answers"), list):
                return JevCall(JevStatus.INVALID_RESPONSE, attempts=attempt, latency_ms=elapsed, http_status=http_status,
                               error="응답에 answers 배열이 없음")
            return JevCall("OK", body={**from_decisions_answers(body, names), "attached": attached}, attempts=attempt, latency_ms=elapsed,
                           http_status=http_status, usage=body.get("usage") if isinstance(body.get("usage"), dict) else None)
        return JevCall(status, attempts=attempt, latency_ms=int((time.monotonic() - started) * 1000), http_status=http_status, error=redact(error))


def resolve_decisions_client(environment: Optional[Dict[str, str]] = None, env_path: Optional[Path] = None,
                             transport: Any = None) -> Tuple[Optional[HttpDecisionsClient], DecisionsClientConfig, Optional[str]]:
    """환경변수(또는 .env)로 client 를 만든다. 쓸 수 없으면 (None, 설정, 이유)."""
    def read(name: str) -> Optional[str]:
        return env_value(name, environment, env_path)

    config = DecisionsClientConfig(model=(read("DECISIONS_MODEL") or DEFAULT_MODEL).strip(), api_base=(read("DECISIONS_API_BASE") or DEFAULT_API_BASE).rstrip("/"),
                                   read_timeout=float(read("DECISIONS_TIMEOUT") or 300))
    if (read("DECISIONS_FALLBACK") or "1").strip() in ("0", "off", "no"):
        return None, config, "DECISIONS_FALLBACK=0 — Decisions 대체 심판 끔"
    key = (read("DECISIONS_API_KEY") or read("OPENAI_API_KEY") or "").strip()
    if not key:
        return None, config, "OPENAI_API_KEY(또는 DECISIONS_API_KEY) 없음 — Decisions 대체 심판 없음"
    return HttpDecisionsClient(config, key, transport=transport), config, None
