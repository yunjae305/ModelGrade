"""우수AI(app.oosoo.kr) 웹 자동화 — AIONA·FactChat 과 같은 방식으로 질문을 보내고 답을 받는다 (사용자 결정 2026-10-14).

- 로그인: ``.env`` 의 OOSOO_LOGIN_ID·OOSOO_LOGIN_PASSWORD 는 사용자가 직접 넣고, 처음 로그인도 사용자가 실행한다
  (``python tools/web_chat_login.py oosoo --auto --inspect``). 로그인 화면은 ``oosoo.kr/login`` 양식(이메일 ``#log``·비밀번호 ``#pwd``·
  자동로그인 ``#rememberme``·'로그인' 버튼 — 2026-10-14 확인). 로그인 상태는 ``data/browser_profiles/oosoo`` 에 남는다.
  비밀번호는 로그·화면·결과 파일에 남기지 않는다. 실패는 고정 문구로만 알린다.
- 채팅 화면 구조(입력칸·답변·복사 버튼)는 첫 로그인 때 ``--inspect`` 로 남긴 구조를 보고 정한다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from code_eval.llm_client import env_value

from .code_eval_service import ROOT
from .factchat_web import _INSPECT_JS

__all__ = ["OOSOO_URL", "PROFILE", "OOSOO_WEB_VERSION", "OosooWebError", "credentials", "login", "login_status", "inspect_chat"]

OOSOO_URL = "https://app.oosoo.kr/chat"
LOGIN_URL = "https://oosoo.kr/login/?redirect_to=https%3A%2F%2Fapp.oosoo.kr%2Fchat"
PROFILE = ROOT / "data" / "browser_profiles" / "oosoo"
OOSOO_WEB_VERSION = "oosoo-web-v0"


class OosooWebError(RuntimeError):
    pass


def credentials(env_path: Optional[Path] = None) -> tuple[str, str]:
    """``.env`` 의 로그인 정보. 없으면 오류 (값은 돌려주기만 하고 어디에도 쓰지 않는다)."""
    path = env_path or ROOT / ".env"
    user, password = env_value("OOSOO_LOGIN_ID", None, path), env_value("OOSOO_LOGIN_PASSWORD", None, path)
    if not user or not password:
        raise OosooWebError(".env 에 OOSOO_LOGIN_ID·OOSOO_LOGIN_PASSWORD 가 없습니다.")
    return user, password


def login_status(env_path: Optional[Path] = None, profile: Optional[Path] = None) -> Dict[str, Any]:
    """'연결된 모델' 화면용 — 로그인 정보가 .env 에 있는지, 로그인 상태를 담은 프로필이 있는지만 (값은 돌려주지 않는다)."""
    path = env_path or ROOT / ".env"
    has = bool(env_value("OOSOO_LOGIN_ID", None, path)) and bool(env_value("OOSOO_LOGIN_PASSWORD", None, path))
    return {"name": "우수AI", "url": OOSOO_URL, "version": OOSOO_WEB_VERSION, "method": "웹페이지 자동화(브라우저)",
            "credentials": has, "credentials_source": ".env (OOSOO_LOGIN_ID · OOSOO_LOGIN_PASSWORD)",
            "profile_saved": ((profile or PROFILE) / "Default").is_dir()}


def _on_login(url: str) -> bool:
    return "oosoo.kr/login" in url or url.rstrip("/") in ("https://oosoo.kr", "https://www.oosoo.kr")


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
    try:
        page.wait_for_url(lambda url: "app.oosoo.kr" in url, timeout=timeout_ms)
    except Exception:
        raise OosooWebError("로그인 뒤 채팅 화면으로 넘어가지 않았습니다 — 아이디·비밀번호나 추가 인증 화면을 확인해 주세요.") from None


def inspect_chat(page: Any, out: Path, question: Optional[str] = None, wait_s: int = 40) -> Path:
    """로그인한 채팅 화면의 구조(입력칸·버튼·메시지 HTML)를 파일로 남긴다 — 수집 코드의 선택자를 정하는 데 쓴다.
    ``question`` 을 주면 그 질문을 한 번 보내고(키보드 입력 + Enter) 답이 나온 뒤의 구조도 남긴다."""
    page.goto(OOSOO_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    result: Dict[str, Any] = {"before": page.evaluate(_INSPECT_JS, None)}
    if question:
        box = page.locator("textarea, [contenteditable=true]").first
        box.click()
        page.keyboard.insert_text(question)
        page.wait_for_timeout(500)
        page.keyboard.press("Enter")
        page.wait_for_timeout(wait_s * 1000)
        result["after"] = page.evaluate(_INSPECT_JS, question)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return out
