"""첨부 이미지 파일 확인 — 답변 수집과 의미 평가가 같은 규칙으로 '실제 그림'을 골라 보낸다 (10-14 4차, judge-attachment-images-v1).

이미지 첨부(``type`` 이 image 이거나 이름이 그림 확장자)마다 파일을 찾아 확인한다:
- 위치: ``path`` 가 허용 폴더 안에 있어야 한다. 질문 세트 생성 파일처럼 파일 이름만 있으면 허용 폴더 안에서 같은 이름을 찾고
  ``sha256`` 이 맞는 것만 쓴다.
- 지문: ``sha256`` 이 있으면 파일 내용의 sha256 과 같아야 한다 (다르면 IMAGE_HASH_MISMATCH).
- 형식: 파일 앞부분 서명(PNG·JPEG·GIF·WEBP)으로 실제 형식을 읽고, 이름의 확장자가 가리키는 형식과 같아야 한다
  (서명이 없거나 다르면 IMAGE_TYPE_INVALID). 보낼 때의 media_type 은 확장자가 아니라 서명으로 정한다.
- 크기: ``MAX_IMAGE_BYTES`` 이하.

파일을 가리키지 않는 이미지 항목(``path``·``sha256`` 이 모두 없음)은 '내용만 있는 첨부'다 — 팀원 설계(J8, 2026-10-14)대로
평가 모델은 ``content``(그림 속 글자·값)만 받고, 기록에 content_only 로 남긴다. 파일을 가리키는데 확인을 통과하지 못하면
그 그림이 있어야 하는 기준(``IMAGE_REQUIRED_CRITERIA``)은 묻지 않고 판정 미완료로 둔다 (jev_eval).
"""
from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["JUDGE_IMAGE_POLICY_VERSION", "IMAGE_REQUIRED_CRITERIA", "IMAGE_TYPES", "MAX_IMAGE_BYTES", "DEFAULT_ROOTS",
           "is_image", "file_backed", "sniff", "load", "prepare", "blocks", "issue_reason", "prompt_note", "labelled_blocks"]

JUDGE_IMAGE_POLICY_VERSION = "judge-attachment-images-v1"
#: 그림을 직접 봐야 판정할 수 있는 기준 — J8 첨부파일 활용(첨부 내용 반영·형식 읽기·접근 처리). 다른 기준은 글자로 평가한다.
IMAGE_REQUIRED_CRITERIA = ("J8",)
IMAGE_TYPES = {"image", "png", "jpg", "jpeg", "gif", "webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # 제공자 이미지 한도보다 작게 — 넘으면 보내지 않고 기록

ROOT = Path(__file__).resolve().parents[1]
#: 첨부 파일을 찾는 허용 폴더 — 업로드 첨부·질문 세트 생성 파일·예제
DEFAULT_ROOTS = (ROOT / "data" / "attachments", ROOT / "data" / "question_sets")   # 10-23: examples/ 삭제

_SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))

#: 확인 실패 코드 → 사유 (기록·화면 문장)
ISSUES = {
    "IMAGE_FILE_MISSING": "허용된 첨부 폴더에서 이미지 파일을 찾지 못함",
    "IMAGE_HASH_MISMATCH": "파일 내용의 sha256 이 첨부 기록과 다름",
    "IMAGE_TYPE_INVALID": "이미지 형식(파일 서명)을 확인할 수 없거나 확장자와 다름",
    "IMAGE_TOO_LARGE": f"이미지가 {MAX_IMAGE_BYTES // (1024 * 1024)}MB 를 넘음",
    "JUDGE_IMAGES_UNSUPPORTED": "평가 모델 연결이 이미지 입력을 지원하지 않음",
}


def issue_reason(code: str) -> str:
    return ISSUES.get(code, code)


def is_image(item: Any) -> bool:
    if not isinstance(item, dict) or not item.get("name"):
        return False
    kind = str(item.get("type") or "").lower()
    return kind in IMAGE_TYPES or (mimetypes.guess_type(str(item["name"]))[0] or "").startswith("image/")


def file_backed(item: Dict[str, Any]) -> bool:
    """파일을 가리키는 이미지 항목인가 — path 나 sha256 이 있으면 실제 파일을 보내야 한다."""
    return any(isinstance(item.get(key), str) and item[key].strip() for key in ("path", "sha256"))


def sniff(data: bytes) -> Optional[str]:
    """파일 앞부분 서명으로 읽은 실제 이미지 형식 — 모르는 형식이면 None."""
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _candidates(item: Dict[str, Any], roots: Sequence[Path]) -> List[Path]:
    raw = item.get("path") if isinstance(item.get("path"), str) and item["path"].strip() else None
    name = Path(raw).name if raw else str(item.get("name") or "")
    found: List[Path] = []
    if raw:
        path = Path(raw)
        found += [path] if path.is_absolute() else [root / path for root in roots]
        if path.is_absolute() or len(path.parts) > 1:
            return found
    # 파일 이름만 있는 경우(질문 세트 생성 파일) — 허용 폴더 안에서 같은 이름을 찾되, 뒤에서 sha256 으로 같은 파일인지 확인한다
    if name and isinstance(item.get("sha256"), str):
        found += [hit for root in roots if root.is_dir() for hit in root.rglob(name)]
    return found


def load(item: Dict[str, Any], roots: Sequence[Path]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(보낼 이미지 {name, media_type, data(base64), sha256, size, sha256_verified}, None) 또는 (None, 실패 코드)."""
    expected = item.get("sha256").strip().lower() if isinstance(item.get("sha256"), str) and item["sha256"].strip() else None
    resolved_roots = [root.resolve() for root in roots]
    failure = "IMAGE_FILE_MISSING"
    for candidate in _candidates(item, roots):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not any(resolved.is_relative_to(root) for root in resolved_roots) or not resolved.is_file():
            continue
        if resolved.stat().st_size > MAX_IMAGE_BYTES:
            failure = "IMAGE_TOO_LARGE"
            continue
        data = resolved.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if expected and digest != expected:
            failure = "IMAGE_HASH_MISMATCH"   # 같은 이름의 다른 파일일 수 있다 — 다른 후보를 더 본다
            continue
        actual = sniff(data)
        declared = mimetypes.guess_type(str(item.get("name") or resolved.name))[0]
        declared = "image/jpeg" if declared == "image/pjpeg" else declared
        if actual is None or (declared and declared.startswith("image/") and declared != actual):
            return None, "IMAGE_TYPE_INVALID"
        return {"name": str(item["name"]), "media_type": actual, "data": base64.b64encode(data).decode("ascii"),
                "sha256": digest, "size": len(data), "sha256_verified": expected is not None}, None
    return None, failure


def prepare(attachments: Any, roots: Optional[Sequence[Path]] = None) -> Optional[Dict[str, Any]]:
    """질문의 이미지 첨부 확인 결과 — 이미지 첨부가 없으면 None (요청·기록이 예전과 같다).

    {policy, images: [{index, name, media_type, data, sha256, size, sha256_verified}], issues: [{index, name, code, reason}],
     content_only: [{index, name}], required: 파일을 가리키는 이미지가 있는지}."""
    roots = list(DEFAULT_ROOTS if roots is None else roots)
    images, issues, content_only = [], [], []
    for index, item in enumerate(attachments or []):
        if not is_image(item):
            continue
        if not file_backed(item):
            content_only.append({"index": index, "name": str(item["name"])})
            continue
        image, code = load(item, roots)
        if image is None:
            issues.append({"index": index, "name": str(item["name"]), "code": code, "reason": issue_reason(code)})
        else:
            images.append({"index": index, **image})
    if not (images or issues or content_only):
        return None
    return {"policy": JUDGE_IMAGE_POLICY_VERSION, "images": images, "issues": issues, "content_only": content_only,
            "required": bool(images or issues)}


def prompt_note(state: Any) -> Optional[str]:
    """평가 요청 뒷부분(평가 대상 원문 쪽)에 붙이는 안내 — 이미지 첨부의 image_attached 가 적혀 있을 때만 (없으면 요청이 예전과 같다)."""
    attachments = state.get("attachments") if isinstance(state, dict) else None
    flags = [a.get("image_attached") for a in attachments or [] if isinstance(a, dict) and "image_attached" in a]
    if not flags:
        return None
    note = ("첨부 이미지: attachments 가운데 image_attached 가 true 인 항목은 실제 그림이 이 요청에 '[첨부 이미지 n: 이름]' 표시 바로 뒤에 "
            "함께 들어 있습니다. 그 그림을 직접 보고 판단하고, content(질문을 만들 때 적은 그림 설명)와 그림이 다르면 그림을 따릅니다.")
    if not all(flags):
        note += " image_attached 가 false 인 이미지는 그림을 받지 못한 것이므로 그 그림의 내용을 보았다고 가정하지 않습니다."
    return note


def labelled_blocks(provider: str, images: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """평가 요청용 — 그림마다 '[첨부 이미지 n: 이름]' 글자 표시 뒤에 그림 (평가 모델이 attachments 의 어느 항목인지 알게)."""
    parts: List[Dict[str, Any]] = []
    for number, image in enumerate(images, 1):
        parts.append({"type": "text", "text": f"[첨부 이미지 {number}: {image['name']}]"})
        parts += blocks(provider, [image])
    return parts


def blocks(provider: str, images: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """제공자 형식의 이미지 부분 — 질문 생성기의 API 요청(webapp.api_request)과 평가 요청(anthropic_semantic_client)이 같이 쓴다."""
    if provider == "gpt":   # OpenAI Responses
        return [{"type": "input_image", "image_url": f"data:{i['media_type']};base64,{i['data']}"} for i in images]
    if provider == "claude":   # Anthropic Messages
        return [{"type": "image", "source": {"type": "base64", "media_type": i["media_type"], "data": i["data"]}} for i in images]
    if provider == "factchat":   # OpenAI 호환 Chat Completions
        return [{"type": "image_url", "image_url": {"url": f"data:{i['media_type']};base64,{i['data']}"}} for i in images]
    if provider == "gemini":
        return [{"inline_data": {"mime_type": i["media_type"], "data": i["data"]}} for i in images]
    raise ValueError(f"이미지 형식을 모르는 제공자: {provider}")
