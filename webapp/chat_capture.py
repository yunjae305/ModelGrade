"""대화 글 로그 — 복사 아이콘이 안 떠 답을 못 받은 턴에서, 마지막 질문 말풍선 뒤 답 자리에 화면으로 보이는 글을 읽어 남긴다 (사용자 10-26 저녁:
이미지 캡처는 보고서 용량 부담이라 글 로그로). 결과 보고서 보류 칸에 보이고, 판정 원문으로는 쓰지 않는다(답은 복사 아이콘으로 받은 글만).

읽는 칸은 마지막 질문 말풍선을 품는 조상 가운데 옆 목록(nav · aside 등)을 품지 않는 가장 큰 칸이다 — 채팅 목록 · 계정 표시는 읽지 않는다
(결과 폴더가 공개 저장소에 올라간다). 대화 칸을 못 찾으면 읽지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

MAX_CHARS = 2000   # 답 자리 글은 이만큼만 — 무한 로딩 · 오류 안내를 알아볼 만큼
SIDEBAR = "nav, aside, side-navigation-v2, bard-sidenav, mat-sidenav, [role='navigation']"
TURN_TEXT_JS = """([userSel, sidebar]) => {
  const us = userSel ? Array.from(document.querySelectorAll(userSel)) : [];
  const u = us[us.length - 1];
  if (!u) return null;
  let col = u;
  while (col.parentElement && col.tagName !== 'MAIN' && !col.parentElement.querySelector(sidebar)) col = col.parentElement;
  if (col.querySelector(sidebar)) return null;
  // 질문 말풍선 뒤에 오는 칸들의 보이는 글(innerText) — 가까운 형제부터 바깥 형제 순서, 곧 화면 순서
  const parts = [];
  for (let node = u; node && node !== col; node = node.parentElement) {
    for (let s = node.nextElementSibling; s; s = s.nextElementSibling) {
      const t = (s.innerText || '').trim();
      if (t) parts.push(t);
    }
  }
  return {question: (u.innerText || '').trim(), after: parts.join('\\n')};
}"""


def read_last_turn(page: Any, user_selector: str, label: str = "", max_chars: int = MAX_CHARS) -> Optional[Dict[str, Any]]:
    """마지막 질문 말풍선과 그 뒤 답 자리에 보이는 글 — {label, read_at, question_tail, answer_area}, 못 읽으면 None."""
    if page is None or not user_selector:
        return None
    try:
        got = page.evaluate(TURN_TEXT_JS, [user_selector, SIDEBAR])
    except Exception:
        return None
    if not isinstance(got, dict):
        return None
    after = str(got.get("after") or "").strip()
    return {"label": label, "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "question_tail": str(got.get("question") or "")[-200:],
            "answer_area": after[:max_chars] + (" …(줄임)" if len(after) > max_chars else "")}
