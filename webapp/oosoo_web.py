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
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from code_eval.llm_client import env_value

from .code_eval_service import ROOT
from .factchat_web import _INSPECT_JS, COPY_CAPTURE_JS

__all__ = ["OOSOO_URL", "PROFILE", "OOSOO_WEB_VERSION", "OosooWebError", "credentials", "login", "login_status", "inspect_chat", "OosooChat"]

OOSOO_URL = "https://app.oosoo.kr/chat"
LOGIN_URL = "https://oosoo.kr/login/?redirect_to=https%3A%2F%2Fapp.oosoo.kr%2Fchat"
PROFILE = ROOT / "data" / "browser_profiles" / "oosoo"
OOSOO_WEB_VERSION = "oosoo-web-v0"
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


def login_status(env_path: Optional[Path] = None, profile: Optional[Path] = None) -> Dict[str, Any]:
    """'연결된 모델' 화면용 — 로그인 정보가 .env 에 있는지, 로그인 상태를 담은 프로필이 있는지만 (값은 돌려주지 않는다)."""
    path = env_path or ROOT / ".env"
    has = bool(env_value("OOSOO_LOGIN_ID", None, path)) and bool(env_value("OOSOO_LOGIN_PASSWORD", None, path))
    return {"name": "우수AI", "url": OOSOO_URL, "version": OOSOO_WEB_VERSION, "method": "웹브라우저",
            "credentials": has, "credentials_source": ".env (OOSOO_LOGIN_ID · OOSOO_LOGIN_PASSWORD)",
            "profile_saved": ((profile or PROFILE) / "Default").is_dir()}


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


# --- 질문·답변 ------------------------------------------------------------------------------------------
ANSWER = "#messages div.message[id^=message_]"
USER_MESSAGE = "#messages div.message.user-message-row"
COPY_BUTTON = ".icon-menu .icon-wrapper[data-action=copy]"
REFERENCE_FILE_CHARS = 20_000   # 이보다 긴 참조 자료(J7)는 입력칸 대신 TXT 파일로 올린다 (AIONA 와 같음)


class OosooChat:
    """한 브라우저로 우수AI 에 차례로 질문한다 (AionaChat·FactChatChat 과 같은 모양). Playwright 동기 API 라 만든 스레드에서만 쓴다."""

    def __init__(self, *, headless: bool = True, allow_login: bool = True, answer_timeout_s: int = 600, profile: Path = PROFILE,
                 debug_dir: Optional[Path] = None, cancelled: Optional[Any] = None):
        self.headless, self.allow_login, self.answer_timeout_s, self.profile = headless, allow_login, answer_timeout_s, profile
        self.debug_dir = debug_dir
        self.cancelled = cancelled or (lambda: False)
        self._playwright = self._context = self.page = None
        self._temp = tempfile.TemporaryDirectory(prefix="oosoo-ref-")
        self._lock = None

    def __enter__(self) -> "OosooChat":
        from .aiona_web import _profile_lock
        lock = _profile_lock(self.profile)
        while not lock.acquire(timeout=1.0):
            if self.cancelled():
                raise OosooWebError("중단 요청으로 우수AI 수집을 시작하지 않았습니다.")
        self._lock = lock
        try:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self.profile.mkdir(parents=True, exist_ok=True)
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile), channel="chrome", headless=self.headless, viewport={"width": 1400, "height": 900})
            self._context.add_init_script(COPY_CAPTURE_JS)
            self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self.ensure_login()
        except BaseException:
            self._close()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        self._close()

    def _close(self) -> None:
        try:
            for step in (lambda: self._context is not None and self._context.close(),
                         lambda: self._playwright is not None and self._playwright.stop(),
                         self._temp.cleanup):
                try:
                    step()
                except Exception:
                    pass
            self._context = self._playwright = None
        finally:
            if self._lock is not None:
                self._lock.release()
                self._lock = None

    def ensure_login(self) -> None:
        if not logged_in(self.page):
            if not self.allow_login:
                raise OosooWebError("우수AI 로그인이 풀렸습니다. 'python tools/web_chat_login.py oosoo --auto' 로 다시 로그인해 주세요.")
            login(self.page, *credentials())
        enter_app(self.page)

    def _snapshot(self, label: str) -> Optional[str]:
        if self.debug_dir is None:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.debug_dir / f"{time.strftime('%Y%m%dT%H%M%S')}-oosoo-{label}.png"
            self.page.screenshot(path=str(path))
            return path.name
        except Exception:
            return None

    def new_chat(self) -> None:
        enter_app(self.page)
        button = self.page.get_by_role("button", name="새로운 대화").first
        try:
            button.click(timeout=10_000)
        except Exception:   # 버튼을 못 찾으면 대화 지정 없는 채팅 주소로
            self.page.goto(OOSOO_URL, wait_until="domcontentloaded")
        self.page.locator(EDITOR).first.wait_for(timeout=30_000)
        self.page.wait_for_function("(sel) => document.querySelectorAll(sel).length === 0", arg=USER_MESSAGE, timeout=15_000)

    def _answers(self) -> Any:
        return self.page.locator(ANSWER)

    def _last_answer_html(self) -> str:
        answers = self._answers()
        if not answers.count():
            return ""
        return answers.last.evaluate(
            "(el) => { const c = el.querySelector('.tabCont.active .stream-content') || el.querySelector('.stream-content'); "
            "if (!c) return ''; const k = c.cloneNode(true); k.querySelectorAll('style').forEach(s => s.remove()); return k.innerHTML; }")

    def _ready(self) -> bool:
        """답이 끝나면 아이콘 메뉴(복사 버튼)가 보인다."""
        try:
            return bool(self._answers().last.evaluate(
                "(el) => { const m = el.querySelector('.icon-menu'); return !!m && getComputedStyle(m).display !== 'none'; }"))
        except Exception:
            return False

    def _copy_last_answer(self) -> Optional[str]:
        try:
            self.page.evaluate("() => { window.__mgCopied = []; }")
            self._answers().last.locator(COPY_BUTTON).first.click(timeout=5000, force=True)
            self.page.wait_for_function("() => (window.__mgCopied || []).length > 0", timeout=5000)
            return self.page.evaluate("() => window.__mgCopied[window.__mgCopied.length - 1]")
        except Exception:
            return None

    def _model(self) -> Optional[str]:
        try:
            return self._answers().last.evaluate("(el) => { const t = el.querySelector('.tabs .tab.active'); return t ? t.innerText.trim() : null; }")
        except Exception:
            return None

    def _upload(self, files: List[Path]) -> None:
        for f in files:   # 하나씩 올린다 — 여러 개를 한 번에 넣으면 첫 파일만 붙는 화면이 있다(AIONA 에서 확인)
            self.page.locator("#file-input").first.set_input_files(str(f))
            self.page.wait_for_function("(name) => document.body.innerText.includes(name)", arg=f.name, timeout=60_000)
        self.page.wait_for_timeout(1500)

    def ask(self, question: str, files: Optional[List[Path]] = None, reference: Any = None) -> Dict[str, Any]:
        """현재 채팅에 질문을 보내고 답이 끝날 때까지 기다린다 → {answer, status, error, url, sent, latency_ms, model, capture}."""
        from .ingestion.html_markdown import html_to_markdown
        started = time.monotonic()
        files = list(files or [])
        sent = [{"name": f.name, "how": "파일 그대로"} for f in files]
        text = question
        if reference not in (None, "", {}, []):
            reference_text = reference if isinstance(reference, str) else json.dumps(reference, ensure_ascii=False)
            if len(reference_text) > REFERENCE_FILE_CHARS:
                path = Path(self._temp.name) / "참조자료.txt"
                path.write_text(reference_text, encoding="utf-8")
                files.append(path)
                sent.append({"name": path.name, "how": "참조 자료를 TXT 파일로 올림"})
            else:
                text = "[참조 자료]\n" + reference_text + "\n\n" + question
        base = {"sent": sent, "method": OOSOO_WEB_VERSION}
        before = self._answers().count()
        try:
            if files:
                self._upload(files)
            sent_before = self.page.locator(USER_MESSAGE).count()
            editor = self.page.locator(EDITOR).first
            editor.click()
            editor.fill(text)   # 일반 textarea 라 채워 넣기로 충분하다
            self.page.keyboard.press("Enter")
            self.page.wait_for_function("([sel, n]) => document.querySelectorAll(sel).length > n", arg=[USER_MESSAGE, sent_before], timeout=30_000)
        except Exception as exc:
            return {**base, "answer": "", "status": "ERROR", "error": f"질문을 보내지 못했습니다: {type(exc).__name__}", "url": self.page.url,
                    "latency_ms": round((time.monotonic() - started) * 1000), "debug_screenshot": self._snapshot("send-failed")}
        deadline = time.monotonic() + self.answer_timeout_s
        last, stable = None, 0
        while time.monotonic() < deadline and not self.cancelled():
            self.page.wait_for_timeout(1500)
            if self._answers().count() <= before:
                continue
            ready = self._ready()
            html = self._last_answer_html()
            stable = stable + 1 if ready and html == last else 0
            last = html
            if stable >= 2:
                break
        done = stable >= 2
        copied = self._copy_last_answer() if done else None
        if copied is not None:
            answer, capture, quality = copied, "copy-button", []
        else:
            converted = html_to_markdown(last or "")
            answer, capture = converted["markdown"], "html"
            quality = [q for q in converted.get("quality") or [] if q.get("severity") != "info"]
        return {**base, "answer": answer, "status": "SUCCESS" if done else "INCOMPLETE", "capture": capture,
                "error": None if done else "답변이 제한 시간 안에 끝나지 않았습니다.", "url": self.page.url,
                "latency_ms": round((time.monotonic() - started) * 1000), "model": self._model() or "기본 모델",
                **({"conversion": quality} if quality else {}), **({} if done else {"debug_screenshot": self._snapshot("incomplete")})}
