"""입력 파일 읽기와 결과 저장. 원문·기존 결과를 덮어쓰지 않는다."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Tuple

__all__ = ["parse_input_text", "load_input_file", "write_json", "write_jsonl", "validate_json_numbers"]


def validate_json_numbers(value: Any) -> None:
    """JSON 입력의 숫자는 유한해야 한다. 문자열 안의 NaN 표기는 그대로 둔다.

    json.loads가 받아들이는 NaN/Infinity 및 1e400의 float 오버플로를
    케이스 검증에서 거부한다. 반복자로 순회해 중첩 자료를 복사하지 않는다.
    """
    pending = [iter((value,))]
    seen = set()
    while pending:
        try:
            item = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("JSON 숫자는 유한한 값이어야 합니다 (NaN·Infinity·수치 범위 초과 입력 불가)")
        if isinstance(item, (dict, list, tuple)) and id(item) not in seen:
            seen.add(id(item))
            pending.append(iter(item.values() if isinstance(item, dict) else item))


def parse_input_text(text: str, name: str = "input") -> Tuple[List[Any], List[Dict[str, Any]]]:
    """JSON(객체·배열·{"cases": [...]}) 또는 JSONL 을 읽는다. (항목들, 줄 단위 파싱 오류) 를 돌려준다.

    JSONL 의 깨진 줄은 조용히 버리지 않고 INPUT_ERROR 항목으로 남긴다.
    """
    text = text.lstrip("﻿")
    stripped = text.strip()
    if not stripped:
        raise ValueError(f"{name}: 입력이 비어 있음")
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        value = None
    if value is not None:
        if isinstance(value, list):
            return value, []
        if isinstance(value, dict) and isinstance(value.get("cases"), list):
            return value["cases"], []
        if isinstance(value, dict):
            return [value], []
        raise ValueError(f"{name}: JSON 최상위는 객체·배열이어야 함")
    items: List[Any] = []
    errors: List[Dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError as exc:
            items.append({"__parse_error__": f"{name} {number}행 JSON 오류: {exc.msg}", "case_id": f"line-{number}"})
            errors.append({"line": number, "error": exc.msg})
    if not items:
        raise ValueError(f"{name}: 읽을 수 있는 항목이 없음")
    return items, errors


def load_input_file(path: Path) -> Tuple[List[Any], List[Dict[str, Any]]]:
    return parse_input_text(Path(path).read_text(encoding="utf-8"), str(path))


def _safe_target(path: Path, overwrite: bool) -> Path:
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} 가 이미 있음 — 기존 결과를 덮어쓰지 않음 (--overwrite 로 명시)")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, value: Any, overwrite: bool = False) -> Path:
    body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    target = _safe_target(path, overwrite)
    target.write_text(body, encoding="utf-8")
    return target


def write_jsonl(path: Path, cases: List[Dict[str, Any]], overwrite: bool = False) -> Path:
    validate_json_numbers(cases)
    target = _safe_target(path, overwrite)
    with target.open("w", encoding="utf-8", newline="\n") as fh:
        for case in cases:
            fh.write(json.dumps(case, ensure_ascii=False, allow_nan=False) + "\n")
    return target
