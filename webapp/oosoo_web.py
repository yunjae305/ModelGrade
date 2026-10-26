"""우수AI(app.oosoo.kr) 웹 자동화 — AIONA·FactChat 과 같은 방식으로 질문을 보내고 답을 받는다 (사용자 결정 2026-10-14).

- 로그인: ``.env`` 의 OOSOO_LOGIN_ID·OOSOO_LOGIN_PASSWORD 는 사용자가 직접 넣고, 처음 로그인도 사용자가 실행한다
  (``python tools/web_chat_login.py oosoo --auto --inspect``). 로그인 화면은 ``oosoo.kr/login`` 양식(이메일 ``#log``·비밀번호 ``#pwd``·
  자동로그인 ``#rememberme``·'로그인' 버튼 — 2026-10-14 확인). 로그인 상태는 ``data/browser_profiles/oosoo`` 에 남는다.
  비밀번호는 로그·화면·결과 파일에 남기지 않는다. 실패는 고정 문구로만 알린다.
- 채팅 화면(2026-10-14 점검): 로그인한 첫 화면(oosoo.kr)에서 '시작하기' → app.oosoo.kr/chat. 입력칸 textarea('무엇이든 물어보세요'),
  Enter 로 보냄, 질문 행 div.message.user-message-row, 답변 행 div.message[id^=message_] — 모델 이름 탭(.tab.active, 기본 GPT 5.4),
  본문 .stream-content, 답이 끝나면 .icon-menu 에 복사 버튼(data-action=copy). 첨부 #file-input. 답변은 복사 버튼 원문으로 받는다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from code_eval.llm_client import env_value

ROOT = Path(__file__).resolve().parent.parent
from .factchat_web import _INSPECT_JS

__all__ = ["OOSOO_URL", "OosooWebError", "credentials", "login", "inspect_chat",]

OOSOO_URL = "https://app.oosoo.kr/chat"
LOGIN_URL = "https://oosoo.kr/login/?redirect_to=https%3A%2F%2Fapp.oosoo.kr%2Fchat"
EDITOR = "textarea[placeholder^='무엇이든']"   # 채팅 입력칸 (2026-10-14 확인)


class OosooWebError(RuntimeError):
    pass


def credentials(env_path: Optional[Path] = None) -> tuple[str, str]:
    """``.env`` 의 로그인 정보. 없으면 오류 (값은 돌려주기만 하고 어디에도 쓰지 않는다)."""
    path = env_path or ROOT / ".env"
    user, password = env_value("OOSOO_LOGIN_ID", None, path), env_value("OOSOO_LOGIN_PASSWORD", None, path)
    if not user or not password:
        raise OosooWebError(".env 에 OOSOO_LOGIN_ID·OOSOO_LOGIN_PASSWORD 가 없습니다.")
    return user, password


APP_HOST = "app.oosoo.kr"


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def enter_app(page: Any, timeout_ms: int = 30_000) -> None:
    """로그인한 첫 화면(oosoo.kr)에서 '시작하기'를 눌러 채팅 앱(app.oosoo.kr/chat)으로 들어간다 — 앱 주소를 바로 열면
    로그인돼 있어도 첫 화면으로 돌아간다(2026-10-14 확인). 앱의 입력칸이 보일 때까지 기다린다."""
    if _host(page.url) != APP_HOST:
        page.goto("https://oosoo.kr/", wait_until="domcontentloaded")
        start = page.get_by_role("link", name="시작하기").first
        start.wait_for(timeout=timeout_ms)
        start.click()
        page.wait_for_url(lambda url: _host(url) == APP_HOST and "/login" not in urlsplit(url).path, timeout=timeout_ms)
    if "/chat" not in urlsplit(page.url).path:
        page.goto(OOSOO_URL, wait_until="domcontentloaded")
    page.locator(EDITOR).first.wait_for(timeout=timeout_ms)


def logged_in(page: Any) -> bool:
    """첫 화면에 '로그아웃'이 보이면 우수AI 계정에 로그인된 상태."""
    try:
        page.goto("https://oosoo.kr/", wait_until="domcontentloaded")
        return page.get_by_text("로그아웃").count() > 0
    except Exception:
        return False


def login(page: Any, user: str, password: str, timeout_ms: int = 45_000) -> None:
    """우수AI 로그인 양식을 채우고 '자동로그인'을 켠 뒤 로그인해 채팅 화면(app.oosoo.kr)으로 돌아올 때까지 기다린다."""
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        page.wait_for_selector("#log", timeout=timeout_ms)
        page.locator("#log").fill(user)
        page.locator("#pwd").fill(password)
        remember = page.locator("#rememberme")
        if remember.count() and not remember.first.is_checked():
            remember.first.check()
        page.locator("form input[type=submit], form button[type=submit]").first.click()
    except Exception:
        raise OosooWebError("로그인 화면을 채우지 못했습니다 — 로그인 화면을 열지 못했거나 화면 구성이 바뀌었습니다.") from None
    try:   # 로그인 화면을 벗어나고(주소의 호스트·경로로 판단 — 로그인 주소의 redirect_to 에 앱 주소가 들어 있다) 채팅 앱까지 들어간다
        page.wait_for_url(lambda url: not (_host(url) == "oosoo.kr" and urlsplit(url).path.startswith("/login")), timeout=timeout_ms)
        if not logged_in(page):
            raise OosooWebError("x")
        enter_app(page, timeout_ms)
    except Exception:
        raise OosooWebError("로그인 뒤 채팅 화면으로 넘어가지 않았습니다 — 아이디·비밀번호나 추가 인증 화면을 확인해 주세요.") from None


def inspect_chat(page: Any, out: Path, question: Optional[str] = None, wait_s: int = 40) -> Path:
    """로그인한 채팅 화면의 구조(입력칸·버튼·메시지 HTML)를 파일로 남긴다 — 수집 코드의 선택자를 정하는 데 쓴다.
    ``question`` 을 주면 그 질문을 한 번 보내고(키보드 입력 + Enter) 답이 나온 뒤의 구조도 남긴다."""
    enter_app(page)
    page.wait_for_timeout(3000)
    result: Dict[str, Any] = {"before": page.evaluate(_INSPECT_JS, None)}
    if question:
        box = page.locator(EDITOR).first
        box.click()
        page.keyboard.insert_text(question)
        page.wait_for_timeout(500)
        page.keyboard.press("Enter")
        page.wait_for_timeout(wait_s * 1000)
        result["after"] = page.evaluate(_INSPECT_JS, question)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


