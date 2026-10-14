"""답변 수집 때 첨부(J8)를 API 모델에 함께 보낸다 — 첨부가 있는 질문에만 쓰고, 없으면 요청은 예전과 같다 (10-14 3차 7단계).

- 글자 첨부(txt·md·csv·json 등): ``content`` 를 질문 뒤에 '[첨부파일: 이름 (종류)]' 블록으로 붙인다 → mode "text".
- PDF·DOCX: ``content`` 는 파일에서 뽑은 글자다 → 같은 방식으로 붙이되 mode "text_extracted" (파일 자체를 보낸 것이 아님을 남긴다).
- 이미지: 허용된 폴더 안의 실제 파일(``path``)이 있을 때만 제공자의 이미지 입력으로 보낸다 → mode "image". 파일이 없으면 보내지 않고
  mode "not_sent" 와 사유를 남긴다 — 그림 속 글자(``content``)나 파일 이름으로 이미지 이해를 대신하지 않는다.
- 이미지 파일 확인(허용 폴더·sha256·파일 서명으로 읽은 형식·크기)과 제공자 형식은 ``code_eval.attachment_images`` 를 쓴다 —
  평가 모델에 보내는 그림과 같은 규칙이다 (10-14 4차). 형식이 확장자와 다르거나 서명이 없는 파일은 보내지 않는다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from code_eval import attachment_images

__all__ = ["IMAGE_TYPES", "MAX_IMAGE_BYTES", "prepare", "image_blocks", "sent_attachments"]

IMAGE_TYPES = attachment_images.IMAGE_TYPES
EXTRACTED_TYPES = {"pdf", "docx", "hwp", "hwpx", "pptx", "xlsx"}
MAX_IMAGE_BYTES = attachment_images.MAX_IMAGE_BYTES


def prepare(question: str, attachments: Any, roots: Sequence[Path]) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(첨부 글자를 붙인 질문, 보낼 이미지 [{name, media_type, data, sha256, size}], 전달 기록 [{name, type, mode, reason?}])."""
    text_blocks, images, delivery = [], [], []
    for item in attachments or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        kind = str(item.get("type") or "").lower()
        name = str(item["name"])
        if attachment_images.is_image(item):
            if not attachment_images.file_backed(item):
                delivery.append({"name": name, "type": kind, "mode": "not_sent",
                                 "reason": "이미지 파일(path) 없음 — 그림 속 글자로 대신하지 않음"})
                continue
            image, code = attachment_images.load(item, roots)
            if image is None:
                delivery.append({"name": name, "type": kind, "mode": "not_sent", "reason": attachment_images.issue_reason(code),
                                 "code": code})
                continue
            images.append(image)
            delivery.append({"name": name, "type": kind, "mode": "image", "bytes": image["size"], "sha256": image["sha256"],
                             "media_type": image["media_type"]})
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content:
            delivery.append({"name": name, "type": kind, "mode": "not_sent", "reason": "글자 내용(content) 없음"})
            continue
        text_blocks.append(f"[첨부파일: {name} ({kind.upper() or '파일'})]\n{content}")
        delivery.append({"name": name, "type": kind, "mode": "text_extracted" if kind in EXTRACTED_TYPES else "text"})
    text = question if not text_blocks else question + "\n\n" + "\n\n".join(text_blocks)
    return text, images, delivery


def sent_attachments(attachments: Any, delivery: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """실제로 보낸 첨부 [{name, sha256?, mode}] — 수집 결과의 ``sent`` (팀원 질문 세트 실행과 같은 모양). 받은 첨부만 J8 로 평가하도록
    ``ComparisonService._answer_case`` 가 이 목록에 있는 첨부만 그 답변의 평가 입력에 남긴다. 못 보낸(not_sent) 첨부는 넣지 않는다."""
    named = [item for item in attachments or [] if isinstance(item, dict) and item.get("name")]
    out = []
    for item, record in zip(named, delivery):
        if record.get("mode") == "not_sent":
            continue
        entry = {"name": str(item["name"]), "mode": record.get("mode")}
        sha = record.get("sha256") or item.get("sha256")
        if isinstance(sha, str) and sha:
            entry["sha256"] = sha
        out.append(entry)
    return out


def image_blocks(provider: str, text: str, images: List[Dict[str, Any]]) -> Any:
    """마지막 사용자 메시지의 내용 — 이미지가 없으면 글자 그대로(예전 요청과 같음), 있으면 제공자 형식의 여러 부분."""
    if not images:
        return text
    parts = attachment_images.blocks(provider if provider in ("gpt", "claude", "factchat") else "gemini", images)
    if provider == "gpt":   # OpenAI Responses
        return [{"type": "input_text", "text": text}] + parts
    if provider in ("claude", "factchat"):
        return [{"type": "text", "text": text}] + parts
    return {"gemini_parts": [{"text": text}] + parts}
