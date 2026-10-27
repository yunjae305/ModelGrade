"""에러 케이스 기록 (error-case-labels-v1) — 에러 케이스로 의심된 답을 사람이 '에러 케이스 · PASS · FAIL' 로 정한 기록 (사용자 10-26).

의심 답(능력 부정 문구 · 서비스 오류 문구 · 짧은 답 + 오류 · 거절 말)은 기록에 없으면 보류로 가고, 사람이 보고서에서 고른 값이
`aiona-chat-eval held-apply` 로 여기 쌓인다. 다음 판정부터 같은 글(공백만 다른 글 포함)은 기록대로 정한다:
에러 케이스 → CODE 0-1 FAIL(에러 케이스), PASS · FAIL → 에러 케이스 아님(나머지 판정은 심판 몫).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

VERSION = "error-case-labels-v1"
LABELS = ("ERROR_CASE", "PASS", "FAIL")
DEFAULT_PATH = Path(__file__).resolve().parent / "settings" / "error_case_labels.json"


def labels_path() -> Path:
    """기록 파일 — MODELGRADE_ERROR_CASES 로 바꿀 수 있다(시험용)."""
    return Path(os.environ.get("MODELGRADE_ERROR_CASES") or DEFAULT_PATH)


def normalize(text: str) -> str:
    """같은 글인지 보는 열쇠 — 앞뒤 공백을 지우고 공백 · 줄바꿈 묶음을 한 칸으로."""
    return re.sub(r"\s+", " ", (text or "").strip())


def _read(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": VERSION, "labels": []}
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def _index(path_str: str, mtime: float) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for x in _read(Path(path_str)).get("labels") or []:
        if isinstance(x, dict) and x.get("label") in LABELS and x.get("text"):
            out[normalize(x["text"])] = x   # 같은 글은 나중 기록이 이긴다
    return out


def lookup(answer: str) -> Optional[Dict[str, Any]]:
    """사람이 정한 같은 글의 기록 — 없으면 None."""
    path = labels_path()
    if not path.exists():
        return None
    return _index(str(path), path.stat().st_mtime).get(normalize(answer))


def add(entries: Iterable[Dict[str, Any]]) -> int:
    """기록을 덧붙인다 — 같은 글 · 같은 값이 이미 있으면 건너뜀. 덧붙인 수."""
    path = labels_path()
    doc = _read(path)
    have = {(normalize(x.get("text") or ""), x.get("label")) for x in doc.get("labels") or []}
    added = 0
    for e in entries:
        text = normalize(e.get("text") or "")
        if not text or e.get("label") not in LABELS or (text, e["label"]) in have:
            continue
        doc.setdefault("labels", []).append({**e, "text": text, "at": e.get("at") or datetime.now().isoformat(timespec="seconds")})
        have.add((text, e["label"]))
        added += 1
    if added:
        doc["version"] = VERSION
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return added


def summary() -> List[Dict[str, Any]]:
    """기록 전체(같은 글은 마지막 것만) — 보고서 · 점검용."""
    path = labels_path()
    return list(_index(str(path), path.stat().st_mtime).values()) if path.exists() else []
