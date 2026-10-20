"""AIONA 웹 자동화 — winz.aiona.kr 에 로그인 상태를 유지하고(별도 Chrome 프로필) 새 채팅으로 질문을 보내 답변을 받는다.

사용자 결정(2026-10-14): AIONA 는 API 키가 없어 웹 브라우저 자동화로 답변을 수집한다 ("새 채팅 눌러서 대화 진행").
- 로그인: ``.env`` 의 AIONA_LOGIN_ID·AIONA_LOGIN_PASSWORD 는 사용자가 직접 넣고, 처음 로그인도 사용자가 실행한다
  (``python tools/web_chat_login.py aiona --auto``). '자동 로그인(30일 유지)'으로 로그인 상태를 ``data/browser_profiles/aiona`` 에
  남긴다. 로그인이 풀렸으면 ``allow_login`` 일 때만 같은 값으로 다시 로그인한다. 비밀번호는 로그·화면·결과 파일에 남기지 않는다.
- 화면 구조(2026-10-14 확인): 새 채팅 = ``/chat``, 입력칸 ``textarea[placeholder^=무엇이든]``, 전송 ``button[type=submit][title=전송]``
  (답하는 동안은 정지 버튼으로 바뀜), 내 질문 ``div.flex.justify-end``, 답변 ``div.flex.justify-start`` 안의 ``.prose``,
  파일 올리기 ``input[type=file]`` (이미지·PDF·DOCX·TXT 등, 이미지는 이름 글자 없이 ``img[alt=파일 이름]`` 미리보기만 보임),
  입력 상한 100,000자. 모델은 기본값 '자동 추천' 그대로 둔다.
- 긴 글을 보내면 의도 감지 안내('요약 뒤에 퀴즈까지 이어서 풀어볼까요?')가 떠서 고를 때까지 전송되지 않는다 — 질문을 바꾸지
  않는 '그냥 보내기'를 누르고 결과에 ``intent_prompt`` 로 남긴다 (2026-10-14 실제 수집에서 확인).
- AIONA 가 답을 못 내면 답변 칸 없이 '생각을 중단했습니다 · 처리 중 오류가 생겨 멈췄습니다'(``span[role=status]``)가 뜬다 —
  기다리지 않고 그 사유를 담아 수집 오류(ERROR)로 남긴다 (2026-10-14 실제 수집: '아무것도 출력하지 마.').
- 답변은 답변 아래 '복사' 버튼(``button[title=복사]``)이 클립보드에 넣는 원문으로 받는다 (2026-10-14 사용자 결정 — FactChat·우수AI 와
  같음). 복사 버튼을 못 쓰면 답변 본문(HTML)을 표·목록·코드 서식을 살려 Markdown 으로 옮긴다 (``ingestion.html_markdown``).
- 첨부 파일은 하나씩 올린다 — 여러 개를 한 번에 넣으면 첫 파일만 붙었다 (2026-10-14 Q296, PDF 2개).
- 봇 확인·추가 인증 화면이 나오면 멈추고 사람에게 넘긴다 — 우회하지 않는다.
- 같은 Chrome 프로필은 한 번에 한 브라우저만 연다(프로필 잠금 — 실행이 겹치면 뒤 실행이 기다린다). 로그인 실패는 고정 문구로만
  알린다 — Playwright 예외 문구(Call log)에는 입력값이 섞일 수 있다 (Codex 리뷰).
"""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from code_eval.llm_client import env_value

from .code_eval_service import ROOT
from .ingestion.html_markdown import html_to_markdown

__all__ = ["AIONA_URL", "PROFILE", "AIONA_WEB_VERSION", "AionaWebError", "credentials", "login", "AionaChat"]

AIONA_URL = (env_value("AIONA_URL") or "https://dev.aiona.kr").rstrip("/")   # 사용자 10-19 밤: dev.aiona.kr (.env AIONA_URL 로 바꿀 수 있음)
PROFILE = ROOT / "data" / "browser_profiles" / "aiona"
AIONA_WEB_VERSION = "aiona-web-v3"   # v3: 개인정보 확인 안내("마스킹하거나 그대로 전송")가 뜨면 '마스킹 후 전송' 을 누른다 (사용자 결정 2026-10-18)
REFERENCE_FILE_CHARS = 20_000   # 이보다 긴 참조 자료(J7)는 입력칸 대신 TXT 파일로 올린다
EMPTY_OUTPUT_WAIT_S = 15        # 빈 출력 요구 질문: 이 안에 복사 버튼이 안 뜨면 기다리지 않고 보류(COPY_FAILED) (사용자 규칙 2026-10-20 밤)
USER_MESSAGE = "div.space-y-4 > div.flex.justify-end"   # 내 질문 칸
_POPUPS = ("나중에", "계속 묻지 않기", "괜찮습니다.", "건너뛰기")   # 설문·저장 안내·첫 방문 안내


class AionaWebError(RuntimeError):
    pass


_PROFILE_LOCKS: Dict[str, threading.Lock] = {}
_PROFILE_LOCKS_GUARD = threading.Lock()


def _profile_lock(profile: Path) -> threading.Lock:
    """프로필마다 잠금 하나 — Chrome 은 같은 프로필을 두 브라우저가 동시에 열지 못한다."""
    key = str(profile.resolve())
    with _PROFILE_LOCKS_GUARD:
        return _PROFILE_LOCKS.setdefault(key, threading.Lock())


def credentials(env_path: Optional[Path] = None) -> tuple[str, str]:
    """``.env`` 의 로그인 정보. 없으면 오류 (값은 돌려주기만 하고 어디에도 쓰지 않는다)."""
    path = env_path or ROOT / ".env"
    user, password = env_value("AIONA_LOGIN_ID", None, path), env_value("AIONA_LOGIN_PASSWORD", None, path)
    if not user or not password:
        raise AionaWebError(".env 에 AIONA_LOGIN_ID·AIONA_LOGIN_PASSWORD 가 없습니다.")
    return user, password


def login_status(env_path: Optional[Path] = None, profile: Optional[Path] = None) -> Dict[str, Any]:
    """'연결된 모델' 화면용 — ``.env`` 에 로그인 정보가 있는지, 로그인 상태를 담은 브라우저 프로필이 있는지만 알린다.
    아이디·비밀번호 값은 돌려주지 않는다 (사용자 결정 2026-10-14: 로그인 정보는 화면이 아니라 .env 에만 넣는다)."""
    path = env_path or ROOT / ".env"
    has = bool(env_value("AIONA_LOGIN_ID", None, path)) and bool(env_value("AIONA_LOGIN_PASSWORD", None, path))
    return {"name": "AIONA", "url": AIONA_URL, "version": AIONA_WEB_VERSION, "method": "웹브라우저",
            "credentials": has, "credentials_source": ".env (AIONA_LOGIN_ID · AIONA_LOGIN_PASSWORD)",
            "profile_saved": ((profile or PROFILE) / "Default").is_dir()}


def login(page: Any, user: str, password: str, timeout_ms: int = 30_000) -> None:
    """로그인 화면을 채우고 '자동 로그인(30일 유지)'을 켠 뒤 로그인한다. 실패는 고정 문구의 오류로만 알린다(입력값을 담지 않음)."""
    try:
        page.goto(f"{AIONA_URL}/login")
        page.locator("input[type=email]").fill(user)
        page.locator("input[type=password]").fill(password)
        toggle = page.locator("xpath=//span[contains(normalize-space(.), '자동 로그인')]/preceding-sibling::button[1]")
        if toggle.count():
            toggle.first.click()
        page.locator("button[type=submit]").click()
    except Exception:
        raise AionaWebError("로그인 화면을 채우지 못했습니다 — 로그인 화면을 열지 못했거나 화면 구성이 바뀌었습니다.") from None
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=timeout_ms)
    except Exception:
        raise AionaWebError("로그인 화면을 벗어나지 못했습니다 — 아이디·비밀번호나 추가 인증 화면을 확인해 주세요.") from None


class AionaChat:
    """한 브라우저로 AIONA 에 차례로 질문한다. Playwright 동기 API 라 만든 스레드에서만 쓴다.

        with AionaChat() as chat:
            chat.new_chat()
            out = chat.ask("질문", files=[Path(...)])
    """

    def __init__(self, *, headless: bool = True, allow_login: bool = True, answer_timeout_s: int = 600, profile: Path = PROFILE,
                 debug_dir: Optional[Path] = None, cancelled: Optional[Callable[[], bool]] = None, place: Optional[str] = None,
                 attach: bool = False):
        self.headless, self.allow_login, self.answer_timeout_s, self.profile = headless, allow_login, answer_timeout_s, profile
        self.place = place   # 동시 수집 때 창 자리 (webapp.cdp_chat_web.PLACE_RECTS)
        self.attach = attach   # True 면 자기 프로필 대신 수집용 Chrome(tools/chrome_cdp.py, Gemini 와 같은 창 묶음)에 붙어 자기 창을 쓴다 (CLI 수집, 2026-10-19)
        self._browser = None
        self.debug_dir = debug_dir   # 실패·미완료 때 그 순간 화면을 남길 곳 (원인 확인용)
        self.cancelled = cancelled or (lambda: False)   # 프로필 잠금을 기다리는 동안·브라우저를 열기 전에 중단 요청을 본다
        self._playwright = self._context = self.page = None
        self._temp = tempfile.TemporaryDirectory(prefix="aiona-ref-")
        self._lock: Optional[threading.Lock] = None
        self._intent_as_is = False   # 이번 질문에서 의도 감지 안내의 '그냥 보내기'를 눌렀는지

    def __enter__(self) -> "AionaChat":
        if self.attach:
            return self._enter_attached()
        lock = _profile_lock(self.profile)
        while not lock.acquire(timeout=1.0):   # 같은 프로필을 쓰는 다른 실행이 끝날 때까지 기다리되, 중단 요청이면 그만둔다
            if self.cancelled():
                raise AionaWebError("중단 요청으로 AIONA 수집을 시작하지 않았습니다.")
        self._lock = lock
        try:
            if self.cancelled():
                raise AionaWebError("중단 요청으로 AIONA 수집을 시작하지 않았습니다.")
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self.profile.mkdir(parents=True, exist_ok=True)
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile), channel="chrome", headless=self.headless, viewport={"width": 1400, "height": 900})
            from .factchat_web import COPY_CAPTURE_JS
            self._context.add_init_script(COPY_CAPTURE_JS)   # 복사 버튼이 클립보드에 넣는 글을 페이지 안에서 받아 둔다
            self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
            if self.place:
                from .cdp_chat_web import cdp_place_window
                cdp_place_window(self.page, self.place)
            self.ensure_login()
        except BaseException:   # 실행·로그인 실패 때도 브라우저·임시 폴더·프로필 잠금을 정리한다 (__exit__ 는 불리지 않음)
            self._close()
            raise
        return self

    def _enter_attached(self) -> "AionaChat":
        """수집용 Chrome 에 붙어 AIONA 창을 쓴다 — 로그인 상태는 그 프로필(chrome-cdp)에 남는다."""
        from tools.chrome_cdp import attach, find_or_open
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        try:
            self._browser = attach(self._playwright)
            self.page = find_or_open(self._browser, AIONA_URL.split("//", 1)[-1], f"{AIONA_URL}/chat")
            from .factchat_web import COPY_CAPTURE_JS
            self.page.add_init_script(COPY_CAPTURE_JS)
            self.page.set_default_timeout(60_000)
            if self.place:
                from .cdp_chat_web import cdp_place_window
                cdp_place_window(self.page, self.place)
            self.ensure_login()
        except BaseException:
            self._close()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        self._close()

    def _close(self) -> None:
        """브라우저·Playwright·임시 폴더를 차례로 정리하고, 무엇이 실패하든 프로필 잠금은 반드시 푼다 (Codex 리뷰)."""
        try:
            for step in (lambda: self._browser is not None and self._browser.close(),   # 붙기 방식: 연결만 끊는다(Chrome 은 그대로)
                         lambda: self._context is not None and self._context.close(),
                         lambda: self._playwright is not None and self._playwright.stop(),
                         self._temp.cleanup):
                try:
                    step()
                except Exception:
                    pass
            self._context = self._playwright = self._browser = None
        finally:
            if self._lock is not None:
                self._lock.release()
                self._lock = None

    def ensure_login(self) -> None:
        self.page.goto(f"{AIONA_URL}/chat", wait_until="domcontentloaded")
        self.page.wait_for_timeout(1500)
        if "/login" not in self.page.url:
            return
        if not self.allow_login:
            raise AionaWebError("AIONA 로그인이 풀렸습니다. 'python tools/web_chat_login.py aiona --auto' 로 다시 로그인해 주세요.")
        login(self.page, *credentials())
        self.page.goto(f"{AIONA_URL}/chat", wait_until="domcontentloaded")

    def _snapshot(self, label: str) -> Optional[str]:
        """실패 순간의 화면 캡처 경로 (debug_dir 이 없으면 남기지 않음)."""
        if self.debug_dir is None:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.debug_dir / f"{time.strftime('%Y%m%dT%H%M%S')}-{label}.png"
            self.page.screenshot(path=str(path))
            return path.name
        except Exception:
            return None

    def _dismiss_popups(self) -> None:
        for label in _POPUPS:
            button = self.page.get_by_role("button", name=label, exact=True)
            try:
                if button.count() and button.first.is_visible():
                    button.first.click(timeout=1500)
            except Exception:
                pass

    def new_chat(self) -> None:
        self.page.goto(f"{AIONA_URL}/chat", wait_until="domcontentloaded")
        self.page.locator("textarea[placeholder^='무엇이든']").first.wait_for(timeout=30_000)
        self._dismiss_popups()

    def _answers(self) -> Any:
        return self.page.locator("div.space-y-4 > div.flex.justify-start")

    def _last_answer_html(self) -> str:
        answers = self._answers()
        if not answers.count():
            return ""
        return answers.last.evaluate("(el) => [...el.querySelectorAll('.prose')].map(p => p.innerHTML).join('<br>')")

    def recopy(self) -> str:
        """마지막 답의 복사 버튼을 한 번 더 — 빈 답이 왔을 때 재시도 (사용자 규칙 10-20)."""
        try:
            return self._copy_last_answer() or ""
        except Exception:
            return ""

    def _copy_button_ready(self, answers_before: int) -> bool:
        """답 말풍선이 생기고 그 안에 '복사' 버튼이 있는가 — 빈 출력 요구 질문의 보류 판단 (사용자 규칙 10-20 밤)."""
        try:
            answers = self._answers()
            return answers.count() > answers_before and answers.last.locator("button[title='복사']").count() > 0
        except Exception:
            return False

    def _copy_last_answer(self) -> Optional[str]:
        """마지막 답변 칸 안의 '복사' 버튼을 눌러 클립보드에 들어간 원문을 받는다. 버튼이 없거나 못 받으면 None —
        다른 답변의 버튼은 쓰지 않는다(답변 칸마다 자기 복사 버튼이 안에 있다, 2026-10-14 확인)."""
        try:
            buttons = self._answers().last.locator("button[title='복사']")
            # 코드블록마다 자기 '복사' 버튼이 있어 첫 번째를 누르면 코드만 복사된다(2026-10-18 사용자 지적) — 답변 맨 아래 도구 줄
            # (🔄·📋·👍·👎····)의 📋 를 쓴다: 뒤에서부터 보되 코드블록(pre) 안에 있는 버튼은 건너뛴다 (tools/aiona_recopy.py 와 같은 규칙)
            button = None
            for index in range(buttons.count() - 1, -1, -1):
                candidate = buttons.nth(index)
                if not candidate.evaluate("(el) => !!el.closest('pre')"):
                    button = candidate
                    break
            if button is None:
                return None
            self.page.evaluate("() => { window.__mgCopied = []; }")
            button.click(timeout=5000)
            self.page.wait_for_function("() => (window.__mgCopied || []).length > 0", timeout=5000)
            return self.page.evaluate("() => window.__mgCopied[window.__mgCopied.length - 1]")
        except Exception:
            return None

    def _upload(self, files: List[Path]) -> None:
        for f in files:   # 하나씩 — 여러 개를 한 번에 넣으면 첫 파일만 붙었다 (2026-10-14 Q296)
            self.page.locator("input[type=file]").first.set_input_files(str(f))
            # 올린 파일 칸이 보일 때까지 — 문서는 이름 글자, 이미지는 alt 가 파일 이름인 미리보기로만 보인다
            self.page.wait_for_function(
                "(name) => document.body.innerText.includes(name) || [...document.images].some(i => i.alt === name)",
                arg=f.name, timeout=60_000)
        self.page.wait_for_timeout(1500)

    def _stopped(self) -> List[str]:
        """AIONA 가 답을 내지 못하고 멈춘 표시 — 답변 칸 대신 '생각을 중단했습니다'(role=status)와 사유 문구가 뜬다
        (2026-10-14 실제 수집: '아무것도 출력하지 마.' 에 '처리 중 오류가 생겨 멈췄습니다'). 표시마다 사유 문구."""
        return self.page.evaluate("""() => [...document.querySelectorAll("div.space-y-4 span[role=status]")]
            .filter((s) => (s.innerText || '').includes('생각을 중단했습니다'))
            .map((s) => { const box = s.closest('button'); const all = box ? box.innerText : s.innerText;
                          return all.replace('생각을 중단했습니다', '').trim() || '생각을 중단했습니다'; })""")

    def _asked(self, asked_before: int) -> bool:
        return self.page.locator(USER_MESSAGE).count() > asked_before

    def _wait_sent(self, asked_before: int, timeout_s: float = 20.0) -> bool:
        """내 질문 칸이 하나 늘었는지 — 긴 글의 의도 감지 안내가 뜨면 '그냥 보내기'를, 개인정보 확인 안내가 뜨면 '마스킹 후 전송'을 누르고
        계속 기다린다. 중단 요청이면 더 누르지 않고 그만 기다린다."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and not self.cancelled():
            if self._asked(asked_before):
                return True
            if self._send_as_is():
                self._intent_as_is = True
            if self._mask_and_send():
                self._pii_masked = True
            self.page.wait_for_timeout(500)
        return self._asked(asked_before)   # 마지막 대기 사이에 올라갔을 수 있다 (Codex)

    def _mask_and_send(self) -> bool:
        """개인정보 확인 안내('메시지는 먼저 이동했지만 실제 전송은 보류했습니다 — 마스킹하거나 그대로 전송')에서 '마스킹 후 전송'을 누른다.
        이 안내는 내 질문 칸이 이미 올라간 뒤에도 뜨므로 답변 대기 중에도 확인한다 (사용자 결정 2026-10-18: 마스킹 후 전송)."""
        button = self.page.get_by_role("button", name="마스킹 후 전송", exact=True)
        try:
            if button.count() and button.first.is_visible():
                button.first.click(timeout=3000)
                return True
        except Exception:
            pass
        return False

    def _send_as_is(self) -> bool:
        """긴 글을 보내면 뜨는 의도 감지 안내('요약 뒤에 퀴즈까지…')에서 질문을 바꾸지 않는 '그냥 보내기'를 누른다."""
        button = self.page.get_by_role("button", name="그냥 보내기", exact=True)
        try:
            if button.count() and button.first.is_visible():
                button.first.click(timeout=3000)
                return True
        except Exception:
            pass
        return False

    def ask(self, question: str, files: Optional[List[Path]] = None, reference: Any = None) -> Dict[str, Any]:
        """현재 채팅에 질문을 보내고 답변이 끝날 때까지 기다린다 → {answer, status, error, url, sent, latency_ms, model}."""
        started = time.monotonic()
        files = list(files or [])
        sent = [{"name": f.name, "how": "파일 그대로"} for f in files]
        text = question
        if reference not in (None, "", {}, []):
            reference_text = reference if isinstance(reference, str) else str(reference)
            if len(reference_text) > REFERENCE_FILE_CHARS:   # 긴 참조 자료는 TXT 파일로 올린다
                path = Path(self._temp.name) / "참조자료.txt"
                path.write_text(reference_text, encoding="utf-8")
                files.append(path)
                sent.append({"name": path.name, "how": "참조 자료를 TXT 파일로 올림"})
            else:
                text = f"[참조 자료]\n{reference_text}\n\n{question}"
        before = self._answers().count()
        stopped_before = len(self._stopped())
        self._intent_as_is = False
        self._pii_masked = False
        self._dismiss_popups()
        try:
            if files:
                self._upload(files)
            box = self.page.locator("textarea[placeholder^='무엇이든']").first
            box.fill(text)
            send = self.page.locator("button[type=submit][title='전송']").first
            asked_before = self.page.locator(USER_MESSAGE).count()
            for _ in range(3):   # 전송을 눌러도 질문이 안 올라가는 때가 있다(2026-10-14 Q296, PDF 두 개) — 내 질문 칸이 생길 때까지 다시 누른다
                if self.cancelled():
                    raise AionaWebError("중단 요청으로 질문을 보내지 않음")
                # 전송 버튼이 켜질 때까지 — 앞 클릭이 늦게 올라가 정지 버튼으로 바뀌었으면 기다리지 않는다 (Codex)
                self.page.wait_for_function(
                    "([sel, n]) => { if (document.querySelectorAll(sel).length > n) return true;"
                    " const b = document.querySelector(\"button[type=submit][title='전송']\"); return !!b && !b.disabled; }",
                    arg=[USER_MESSAGE, asked_before], timeout=60_000)
                if self._asked(asked_before):
                    break
                if self.cancelled():   # 기다리는 동안 중단 요청이 왔으면 누르지 않는다 (Codex)
                    raise AionaWebError("중단 요청으로 질문을 보내지 않음")
                send.click()
                if self._wait_sent(asked_before):
                    break
            else:
                raise AionaWebError("전송 버튼을 세 번 눌러도 질문이 올라가지 않음")
        except Exception as exc:
            return {"answer": "", "status": "ERROR", "error": f"질문을 보내지 못했습니다: {type(exc).__name__}", "url": self.page.url,
                    "sent": sent, "latency_ms": round((time.monotonic() - started) * 1000), "method": AIONA_WEB_VERSION,
                    "debug_screenshot": self._snapshot("send-failed")}
        deadline = time.monotonic() + (self.answer_timeout_s or 10 ** 9)   # 0 = 제한 없음 — 답이 끝날 때까지 기다린다 (사용자 지시 10-20)
        last, stable, empty, as_is, masked = None, 0, 0, self._intent_as_is, self._pii_masked
        from webapp.run_summary import asks_empty_output
        asks_empty = asks_empty_output(None, question)   # 빈 출력 요구: 15초 안에 복사 버튼이 안 뜨면 바로 보류 (사용자 규칙 10-20 밤)
        empty_deadline = time.monotonic() + EMPTY_OUTPUT_WAIT_S
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(1500)
            self._dismiss_popups()
            if asks_empty and time.monotonic() > empty_deadline and not self._copy_button_ready(before):
                return {"answer": "", "status": "COPY_FAILED", "capture": "none", "url": self.page.url, "sent": sent,
                        "error": f"빈 출력 요구 — {EMPTY_OUTPUT_WAIT_S}초 안에 복사 버튼이 뜨지 않음(말풍선 없음·오류 등) → 보류 (사용자 규칙 10-20 밤)",
                        "latency_ms": round((time.monotonic() - started) * 1000), "model": "자동 추천", "method": AIONA_WEB_VERSION,
                        "debug_screenshot": self._snapshot("empty-output-no-copy"),
                        **({"intent_prompt": "그냥 보내기"} if as_is else {}), **({"pii_prompt": "마스킹 후 전송"} if masked else {})}
            as_is = self._send_as_is() or as_is
            masked = self._mask_and_send() or masked   # 개인정보 확인 안내는 질문 칸이 올라간 뒤 뜬다 — 전송 보류를 풀어야 답이 온다
            if self._answers().count() <= before:
                empty += 1   # 말풍선조차 안 생긴 채 시간이 간다 — 90초(60틱)면 빈 답으로 끝낸다 (무한 대기 방지 2026-10-20)
                if empty >= 60:
                    break
                stopped = self._stopped()
                if len(stopped) > stopped_before:   # AIONA 가 이 질문에서 멈췄다 — 기다리지 않고 AIONA 오류로 남긴다
                    return {"answer": "", "status": "ERROR", "error": f"AIONA 가 답하지 못했습니다: {stopped[-1][:200]}", "url": self.page.url,
                            "sent": sent, "latency_ms": round((time.monotonic() - started) * 1000), "model": "자동 추천",
                            "method": AIONA_WEB_VERSION, "debug_screenshot": self._snapshot("stopped"),
                            **({"intent_prompt": "그냥 보내기"} if as_is else {}), **({"pii_prompt": "마스킹 후 전송"} if masked else {})}
                continue
            idle = self.page.locator("button[type=submit][title='전송']").count() > 0   # 답하는 동안은 정지 버튼
            html = self._last_answer_html()
            stable = stable + 1 if html and html == last and idle else 0
            empty = empty + 1 if not html and idle else 0   # 빈 답변('아무것도 출력하지 마') — 말풍선은 생기고 본문이 없다
            last = html
            if stable >= 2 or empty >= 4:
                break
        converted = html_to_markdown(last or "")
        answer, capture = converted["markdown"], "html"
        done = (stable >= 2 and bool(answer.strip())) or (empty >= 4 and not answer.strip())
        copied = self._copy_last_answer() if done and answer.strip() else None
        if copied and copied.strip() and len(copied.strip()) < 0.6 * len(answer.strip()):
            # 복사 원문이 화면 글보다 훨씬 짧으면 일부(코드블록 하나 등)만 복사된 것 — 화면 글을 쓴다 (2026-10-18 사용자 지적)
            capture, copied = "html-partial-copy", None
        if copied and copied.strip():   # 복사 버튼 원문을 답으로 — HTML 변환 품질 경고는 원문에는 해당하지 않는다
            answer, capture, converted = copied, "copy-button", {"quality": []}
        return {"answer": answer, "status": "SUCCESS" if done else "INCOMPLETE", "capture": capture,
                **({} if done else {"debug_screenshot": self._snapshot("incomplete")}),
                **({"intent_prompt": "그냥 보내기"} if as_is else {}), **({"pii_prompt": "마스킹 후 전송"} if masked else {}),
                "error": None if done else "답변이 제한 시간 안에 끝나지 않았습니다.", "url": self.page.url, "sent": sent,
                "latency_ms": round((time.monotonic() - started) * 1000), "model": "자동 추천", "method": AIONA_WEB_VERSION,
                "conversion": [q for q in converted.get("quality") or [] if q.get("severity") != "info"]}
