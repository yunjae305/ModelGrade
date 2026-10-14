"""업무 진행 현황 — 산출물이 가리키는 파일이 이 PC 에 있는지만 알려 준다 (파일 내용은 내보내지 않는다).

저장 실행 같은 비공개 원본(data/, 무시 목록)은 PC 마다 있거나 없다. 화면은 이 답으로 '자료 없음'과 '잘못된 경로'를 나눠 보인다.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List

PROGRESS_FILES_VERSION = "progress-files-v1"
ALLOWED_ROOTS = ("참고자료", "tools", "webapp", "code_eval", "tests", "frontend", "data", "examples", "README.md")
MAX_PATHS = 50
MAX_LENGTH = 300


def file_status(root: Path, value: Any) -> Dict[str, Any]:
    """저장소 기준 상대 경로 → {path, status: present|missing|invalid, size?}. 허용 폴더 밖·숨김 파일·상위 경로는 invalid."""
    text = str(value or "")
    parts = PurePosixPath(text).parts
    if (not text or len(text) > MAX_LENGTH or "\\" in text or ":" in text or text.startswith("/") or not parts
            or parts[0] not in ALLOWED_ROOTS or any(p in ("", ".", "..") or p.startswith(".") for p in parts)):
        return {"path": text[:MAX_LENGTH], "status": "invalid"}
    base = root.resolve()
    target = (base / text).resolve()
    if not target.is_relative_to(base):
        return {"path": text, "status": "invalid"}
    if target.is_file():
        return {"path": text, "status": "present", "size": target.stat().st_size}
    if target.is_dir():
        return {"path": text, "status": "present", "directory": True}
    return {"path": text, "status": "missing"}


def files_report(root: Path, values: Iterable[Any]) -> Dict[str, Any]:
    paths: List[Any] = list(values)
    if len(paths) > MAX_PATHS:
        raise ValueError(f"한 번에 {MAX_PATHS}개 경로까지 확인합니다.")
    return {"version": PROGRESS_FILES_VERSION, "files": [file_status(root, p) for p in paths]}
