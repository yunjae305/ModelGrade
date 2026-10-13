"""첨부파일 입력 — 질문과 함께 모델에 보낸 파일(이미지·TXT·DOCX·PDF 등)의 이름·종류·내용 (J8 첨부파일 활용, 2026-10-13 추가).

입력 ``attachments`` 는 [{name, type, content, sha256?, size?, path?}] 이다.
- ``type``: image · txt · docx · pdf 등 파일 종류 (소문자 확장자 이름).
- ``content``: 파일에 실제로 든 내용 — 글자 파일은 원문, 이미지는 그림 속 글자·도표 값. 질문 생성 단계가 파일을 직접 만들기 때문에
  정답 내용을 그대로 안다. 평가 모델은 파일 대신 이 내용을 받아 답변이 첨부를 제대로 읽고 썼는지(J8) 판단한다.
- ``sha256``·``size``·``path``: 어떤 파일을 보냈는지 확인하는 기록 — 평가 모델에는 보내지 않는다.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

__all__ = ["TYPE_LABELS", "attachments_of", "judge_view", "attachment_text", "labels"]

#: 파일 종류 표시 이름 — 여기 없는 종류도 받되 확장자를 그대로 보여 준다
TYPE_LABELS = {"image": "이미지", "txt": "TXT", "docx": "DOCX", "pdf": "PDF"}
_FIELDS = ("name", "type", "content", "sha256", "size", "path")


def attachments_of(payload: Dict[str, Any]) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """입력의 ``attachments`` — 없거나 비면 (None, None), 모양이 틀리면 (None, 오류 문장)."""
    raw = payload.get("attachments")
    if raw is None or raw == []:
        return None, None
    if not isinstance(raw, list):
        return None, f"attachments 는 배열이어야 함 ({type(raw).__name__})"
    items: List[Dict[str, Any]] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return None, f"attachments {index}번: 객체가 아님 ({type(item).__name__})"
        name, kind, content = item.get("name"), item.get("type"), item.get("content")
        if not isinstance(name, str) or not name.strip():
            return None, f"attachments {index}번: name 은 비어 있지 않은 문자열이어야 함"
        if not isinstance(kind, str) or not kind.strip():
            return None, f"attachments {index}번: type 은 비어 있지 않은 문자열이어야 함"
        if content is not None and not isinstance(content, str):
            return None, f"attachments {index}번: content 는 문자열이어야 함 ({type(content).__name__})"
        items.append({key: item[key] for key in _FIELDS if key in item} | {"type": kind.strip().lower()})
    return items, None


def judge_view(attachments: Any) -> List[Dict[str, str]]:
    """평가 모델에 보낼 모양 — 이름·종류·내용만 (경로·지문·크기는 보내지 않는다)."""
    return [{"name": a["name"], "type": a["type"], "content": a.get("content") or ""}
            for a in attachments or [] if isinstance(a, dict) and a.get("name")]


def attachment_text(attachments: Any) -> str:
    """인용 대조용으로 이은 첨부 내용 — 파일마다 내용만 줄바꿈으로 잇는다."""
    return "\n".join(a.get("content") or "" for a in attachments or [] if isinstance(a, dict))


def labels(attachments: Any) -> List[str]:
    """'견적서.pdf (PDF)' 같은 표시 — 적용 근거·화면용."""
    return [f"{a['name']} ({TYPE_LABELS.get(a.get('type'), str(a.get('type') or '').upper())})"
            for a in attachments or [] if isinstance(a, dict) and a.get("name")]
