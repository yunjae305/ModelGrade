"""답변 수집 때 첨부(J8)를 API 모델에 함께 보낸다 — 첨부가 있는 질문에만 쓰고, 없으면 요청은 예전과 같다 (10-14 3차 7단계).

- 글자 첨부(txt·md·csv·json 등): ``content`` 를 질문 뒤에 '[첨부파일: 이름 (종류)]' 블록으로 붙인다 → mode "text".
- PDF·DOCX: ``content`` 는 파일에서 뽑은 글자다 → 같은 방식으로 붙이되 mode "text_extracted" (파일 자체를 보낸 것이 아님을 남긴다).
- 이미지: 허용된 폴더 안의 실제 파일(``path``)이 있을 때만 제공자의 이미지 입력으로 보낸다 → mode "image". 파일이 없으면 보내지 않고
  mode "not_sent" 와 사유를 남긴다 — 그림 속 글자(``content``)나 파일 이름으로 이미지 이해를 대신하지 않는다.
- 평가 모델에는 지금처럼 J8 의 ``judge_view``(이름·종류·내용)가 간다 — 이 모듈은 답변 수집 쪽만 다룬다.
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["IMAGE_TYPES", "MAX_IMAGE_BYTES", "prepare", "image_blocks"]

IMAGE_TYPES = {"image", "png", "jpg", "jpeg", "gif", "webp"}
EXTRACTED_TYPES = {"pdf", "docx", "hwp", "hwpx", "pptx", "xlsx"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # 제공자 이미지 한도보다 작게 — 넘으면 보내지 않고 기록


def _image_file(item: Dict[str, Any], roots: Sequence[Path]) -> Tuple[Optional[Path], Optional[str]]:
    raw = item.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return None, "이미지 파일(path) 없음 — 그림 속 글자로 대신하지 않음"
    path = Path(raw)
    candidates = [path] if path.is_absolute() else [root / path for root in roots]
    for candidate in candidates:
        resolved = candidate.resolve()
        if not any(resolved.is_relative_to(root.resolve()) for root in roots):
            continue
        if resolved.is_file():
            if resolved.stat().st_size > MAX_IMAGE_BYTES:
                return None, f"이미지가 {MAX_IMAGE_BYTES // (1024 * 1024)}MB 를 넘음"
            return resolved, None
    return None, "허용된 첨부 폴더에서 이미지 파일을 찾지 못함"


def prepare(question: str, attachments: Any, roots: Sequence[Path]) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(첨부 글자를 붙인 질문, 보낼 이미지 [{name, mime, data}], 전달 기록 [{name, type, mode, reason?}])."""
    text_blocks, images, delivery = [], [], []
    for item in attachments or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        kind = str(item.get("type") or "").lower()
        name = str(item["name"])
        if kind in IMAGE_TYPES or (mimetypes.guess_type(name)[0] or "").startswith("image/"):
            path, reason = _image_file(item, roots)
            if path is None:
                delivery.append({"name": name, "type": kind, "mode": "not_sent", "reason": reason})
                continue
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            images.append({"name": name, "mime": mime, "data": base64.b64encode(path.read_bytes()).decode("ascii")})
            delivery.append({"name": name, "type": kind, "mode": "image", "bytes": path.stat().st_size})
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content:
            delivery.append({"name": name, "type": kind, "mode": "not_sent", "reason": "글자 내용(content) 없음"})
            continue
        text_blocks.append(f"[첨부파일: {name} ({kind.upper() or '파일'})]\n{content}")
        delivery.append({"name": name, "type": kind, "mode": "text_extracted" if kind in EXTRACTED_TYPES else "text"})
    text = question if not text_blocks else question + "\n\n" + "\n\n".join(text_blocks)
    return text, images, delivery


def image_blocks(provider: str, text: str, images: List[Dict[str, Any]]) -> Any:
    """마지막 사용자 메시지의 내용 — 이미지가 없으면 글자 그대로(예전 요청과 같음), 있으면 제공자 형식의 여러 부분."""
    if not images:
        return text
    if provider == "gpt":   # OpenAI Responses
        return [{"type": "input_text", "text": text}] + [
            {"type": "input_image", "image_url": f"data:{i['mime']};base64,{i['data']}"} for i in images]
    if provider == "claude":
        return [{"type": "text", "text": text}] + [
            {"type": "image", "source": {"type": "base64", "media_type": i["mime"], "data": i["data"]}} for i in images]
    if provider == "factchat":   # OpenAI 호환 Chat Completions
        return [{"type": "text", "text": text}] + [
            {"type": "image_url", "image_url": {"url": f"data:{i['mime']};base64,{i['data']}"}} for i in images]
    return {"gemini_parts": [{"text": text}] + [{"inline_data": {"mime_type": i["mime"], "data": i["data"]}} for i in images]}
