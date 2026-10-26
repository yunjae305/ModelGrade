"""수집 글 로그 — 답을 못 받았거나 빈 답인 턴의 수집 기록을 사람이 읽는 줄로 (사용자 10-26 저녁: 복사 아이콘이 안 떠 못 받은 답은 이미지 대신
글로 보여 준다). 결과 보고서 · 판정보고서의 보류 칸이 같은 줄을 쓴다."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

STATUS_KO = {"SUCCESS": "받음", "COPY_FAILED": "복사 못 함", "ERROR": "오류", "TIMEOUT": "시간 초과", "LIMIT": "사용 한도", "NO_RESPONSE": "응답 없음"}


def _clock(iso: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""


def needs_log(collection: Optional[Dict[str, Any]], answer: str) -> bool:
    """글 로그를 보일 턴인가 — 답이 비었거나 받지 못함."""
    c = collection or {}
    return not (answer or "").strip() or (c.get("status") not in (None, "SUCCESS"))


def log_lines(collection: Optional[Dict[str, Any]]) -> List[str]:
    """수집 기록(collection) → 줄 목록: 상태 · 오류 · 기다린 시간 · 다시 시도 · 답 자리에 보인 글. 기록이 없으면 빈 목록."""
    c = collection or {}
    if not c:
        return []
    at = _clock(c.get("collected_at"))
    st = c.get("status") or "?"
    out = [f"{at + ' ' if at else ''}수집 상태: {st} ({STATUS_KO.get(st, st)})" + (f" — {c['error']}" if c.get("error") else "")]
    if isinstance(c.get("latency_ms"), (int, float)) and c["latency_ms"]:
        out.append(f"기다린 시간: {int(c['latency_ms']) // 1000}초")
    for key, name in (("retried", "다시 시도"), ("note", "메모")):
        if c.get(key):
            out.append(f"{name}: {c[key]}")
    tl = c.get("turn_log") or {}
    if tl:
        when = _clock(tl.get("read_at"))
        area = (tl.get("answer_area") or "").strip()
        out.append(f"{when + ' ' if when else ''}답 자리에 보인 글: " + (area if area else "(아무 글도 없음)"))
    elif c.get("screenshot"):
        out.append("답 자리 글: 이 실행은 글 로그 전 수집이라 없음(대화 화면 이미지만 실행 폴더에 있음)")
    return out
