"""수집 글 로그 — 답을 못 받았거나 빈 답인 턴의 수집 기록을 사람이 읽는 줄로 (사용자 10-26 저녁: 복사 아이콘이 안 떠 못 받은 답은 이미지 대신
글로 보여 준다). 결과 보고서(report.html) · 판정 폴더 판정 보고서의 보류 칸이 같은 줄을 쓴다."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

STATUS_KO = {"SUCCESS": "받음", "COPY_FAILED": "복사 못 함", "ERROR": "오류", "TIMEOUT": "시간 초과", "LIMIT": "사용 한도", "NO_RESPONSE": "응답 없음"}


def _clock(iso: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""


def answer_problem(answer: str, question: str = "") -> Optional[str]:
    """받은 답이 답 대신 오류 · 거절 문구인가 — '언어 모델이라 할 수 없습니다' · '응답을 중지했습니다' · '오류가 발생했습니다' 같은 것(CODE 0-1 의
    에러 케이스 의심과 같은 규칙). 아니면 None."""
    text = (answer or "").strip()
    if not text:
        return None
    from code_eval.checks_integrity import error_suspect
    hit = error_suspect(text, len("".join(text.split())), question or "")
    return f"답 대신 {hit[0]} — '{hit[1]}'" if hit else None


def problem(collection: Optional[Dict[str, Any]], answer: str = "", question: str = "") -> Optional[str]:
    """답을 정상으로 받지 못한 까닭 한 줄 (사용자 10-26 밤: 어떤 식으로 못 받았는지 보류 목록에서 보이게). 정상이면 None."""
    c = collection or {}
    st, err = c.get("status"), str(c.get("error") or "")
    if st == "COPY_FAILED":
        return "빈 출력 요구 질문에 모델이 오류를 내 답이 없음" if "빈 출력 요구" in err else "복사 아이콘이 없거나 눌리지 않아 답을 가져오지 못함"
    if st in ("TIMEOUT", "NO_RESPONSE"):
        return "기다려도 답이 오지 않음(시간 초과)"
    if st == "LIMIT":
        return "사이트 사용 한도에 걸려 답을 받지 못함"
    if st == "ERROR":
        return "사이트 · 모델 오류로 답을 받지 못함"
    if st not in (None, "SUCCESS"):
        return f"수집 실패({st})"
    if not (answer or "").strip():
        return "빈 응답 — 답 칸에 글이 없음"
    return answer_problem(answer, question)


def needs_log(collection: Optional[Dict[str, Any]], answer: str, question: str = "") -> bool:
    """글 로그를 보일 턴인가 — 답이 비었거나 받지 못했거나, 받은 답이 오류 · 거절 문구."""
    return problem(collection, answer, question) is not None


def log_lines(collection: Optional[Dict[str, Any]], answer: str = "", question: str = "") -> List[str]:
    """수집 기록(collection) → 줄 목록: 무엇이 문제였나 · 상태 · 오류 · 기다린 시간 · 다시 시도 · 답 자리에 보인 글. 기록이 없으면 빈 목록."""
    c = collection or {}
    if not c:
        return []
    at = _clock(c.get("collected_at"))
    st = c.get("status") or "?"
    why = problem(c, answer, question)
    out = ([f"문제: {why}"] if why else []) + [
        f"{at + ' ' if at else ''}수집 상태: {st} ({STATUS_KO.get(st, st)})" + (f" — {c['error']}" if c.get("error") else "")]
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
