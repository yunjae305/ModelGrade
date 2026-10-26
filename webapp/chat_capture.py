"""대화 화면 캡처 — 마지막 질문 말풍선부터 그 아래 답 자리까지만 찍는다 (사용자 10-26: 빈 출력 요구 질문에 복사 아이콘이 안 뜨면 그 대화 화면을
보고서에서 보이게, 답 영역만). 옆 채팅 목록 · 계정 표시는 찍지 않는다 — 결과 폴더가 공개 저장소에 올라간다.

칸은 질문 · 답 말풍선을 함께 품는 가장 가까운 조상(답이 없으면 화면 폭 절반 넘는 첫 조상)이고, 옆 목록(nav · aside 등)을 품은 칸까지는
올라가지 않으며 좌우 여백도 옆 목록 끝을 넘지 않는다. 대화 칸을 못 찾으면 찍지 않는다(화면 전체로 대신하지 않음).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

VIEW = (1100, 900)   # 찍는 동안 흉내 내는 화면 크기
SIDEBAR = "nav, aside, side-navigation-v2, bard-sidenav, mat-sidenav, [role='navigation']"
TURN_RECT_JS = """([userSel, answerSel, below, sidebar]) => {
  const us = userSel ? Array.from(document.querySelectorAll(userSel)) : [];
  const u = us[us.length - 1];
  if (!u) return null;
  u.scrollIntoView({block: 'start'});
  const after = (el) => !!(u.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING);
  const as = answerSel ? Array.from(document.querySelectorAll(answerSel)).filter(after) : [];
  const a = as[as.length - 1];
  const uw = u.getBoundingClientRect().width;
  // 답 말풍선이 있으면 질문 · 답을 함께 품는 가장 가까운 칸, 없으면 화면 폭 절반 넘는 칸까지(답은 왼쪽 · 질문은 오른쪽이라 질문 줄만 잡으면 답이 잘림)
  const wide = Math.max(uw * 1.15, 320, window.innerWidth * 0.5);
  let col = u;
  while (col.parentElement && col.tagName !== 'MAIN' && !col.parentElement.querySelector(sidebar)
         && (a ? !col.contains(a) : col.getBoundingClientRect().width < wide)) col = col.parentElement;
  if (col.querySelector(sidebar)) return null;
  const c = col.getBoundingClientRect(), ur = u.getBoundingClientRect();
  const top = Math.max(0, ur.top - 8);
  const bottom = Math.min(window.innerHeight, Math.max(ur.bottom, a ? a.getBoundingClientRect().bottom : 0) + below);
  // 아이콘 줄이 칸 밖으로 조금 나온다 — 좌우 여백을 주되 옆 목록의 오른쪽 끝은 넘지 않는다
  const sb = Array.from(document.querySelectorAll(sidebar)).map(e => e.getBoundingClientRect())
    .filter(r => r.width > 0 && r.height > 0 && r.right <= c.left + 1).reduce((m, r) => Math.max(m, r.right), 0);
  const left = Math.max(0, sb, c.left - 48), right = Math.min(window.innerWidth, c.right + 24);
  return {x: left, y: top, width: right - left, height: bottom - top};
}"""


def capture_last_turn(page: Any, user_selector: str, answer_selector: str, path: Path, below: int = 160) -> Optional[str]:
    """마지막 질문 말풍선 ~ 그 아래 답 자리(+below px)를 JPEG 로 — 저장한 경로, 못 찍으면 None."""
    if page is None or not user_selector:
        return None
    cdp = None
    try:
        # 동시 수집 때 창이 작게 놓여(10-26: 506×122) 화면 안만 찍혔다 — 찍는 동안만 화면 크기를 흉내 내고 되돌린다(창 크기는 그대로)
        try:
            cdp = page.context.new_cdp_session(page)
            cdp.send("Emulation.setDeviceMetricsOverride", {"width": VIEW[0], "height": VIEW[1], "deviceScaleFactor": 1, "mobile": False})
            page.wait_for_timeout(600)
        except Exception:
            cdp = None
        rect = page.evaluate(TURN_RECT_JS, [user_selector, answer_selector or "", below, SIDEBAR])
        if not rect or rect["width"] < 50 or rect["height"] < 20:
            return None
        page.wait_for_timeout(300)   # 말풍선으로 옮긴 뒤 그림이 자리 잡게
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), clip=rect, type="jpeg", quality=70)
        return str(path)
    except Exception:
        return None
    finally:
        if cdp is not None:
            try:
                cdp.send("Emulation.clearDeviceMetricsOverride")
                cdp.detach()
            except Exception:
                pass
