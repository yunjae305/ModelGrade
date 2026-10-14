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
- 답변 본문(HTML)은 표·목록·코드 서식을 살려 Markdown 으로 옮긴다 (``ingestion.html_markdown``).
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

AIONA_URL = "https://winz.aiona.kr"
PROFILE = ROOT / "data" / "browser_profiles" / "aiona"
AIONA_WEB_VERSION = "aiona-web-v1"
REFERENCE_FILE_CHARS = 20_000   # 이보다 긴 참조 자료(J7)는 입력칸 대신 TXT 파일로 올린다
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
    return {"name": "AIONA", "url": AIONA_URL, "version": AIONA_WEB_VERSION, "method": "웹페이지 자동화(브라우저)",
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
                 debug_dir: Optional[Path] = None, cancelled: Optional[Callable[[], bool]] = None):
        self.headless, self.allow_login, self.answer_timeout_s, self.profile = headless, allow_login, answer_timeout_s, profile
        self.debug_dir = debug_dir   # 실패·미완료 때 그 순간 화면을 남길 곳 (원인 확인용)
        self.cancelled = cancelled or (lambda: False)   # 프로필 잠금을 기다리는 동안·브라우저를 열기 전에 중단 요청을 본다
        self._playwright = self._context = self.page = None
        self._temp = tempfile.TemporaryDirectory(prefix="aiona-ref-")
        self._lock: Optional[threading.Lock] = None

    def __enter__(self) -> "AionaChat":
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
            self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self.ensure_login()
        except BaseException:   # 실행·로그인 실패 때도 브라우저·임시 폴더·프로필 잠금을 정리한다 (__exit__ 는 불리지 않음)
            self._close()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        self._close()

    def _close(self) -> None:
        """브라우저·Playwright·임시 폴더를 차례로 정리하고, 무엇이 실패하든 프로필 잠금은 반드시 푼다 (Codex 리뷰)."""
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

    def _upload(self, files: List[Path]) -> None:
        self.page.locator("input[type=file]").first.set_input_files([str(f) for f in files])
        for f in files:   # 올린 파일 칸이 보일 때까지 — 문서는 이름 글자, 이미지는 alt 가 파일 이름인 미리보기로만 보인다
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
        self._dismiss_popups()
        try:
            if files:
                self._upload(files)
            box = self.page.locator("textarea[placeholder^='무엇이든']").first
            box.fill(text)
            send = self.page.locator("button[type=submit][title='전송']").first
            send.wait_for(timeout=30_000)
            self.page.wait_for_function("() => { const b = document.querySelector(\"button[type=submit][title='전송']\"); return b && !b.disabled; }",
                                        timeout=60_000)
            send.click()
        except Exception as exc:
            return {"answer": "", "status": "ERROR", "error": f"질문을 보내지 못했습니다: {type(exc).__name__}", "url": self.page.url,
                    "sent": sent, "latency_ms": round((time.monotonic() - started) * 1000), "method": AIONA_WEB_VERSION,
                    "debug_screenshot": self._snapshot("send-failed")}
        deadline = time.monotonic() + self.answer_timeout_s
        last, stable, empty, as_is = None, 0, 0, False
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(1500)
            self._dismiss_popups()
            as_is = self._send_as_is() or as_is
            if self._answers().count() <= before:
                stopped = self._stopped()
                if len(stopped) > stopped_before:   # AIONA 가 이 질문에서 멈췄다 — 기다리지 않고 AIONA 오류로 남긴다
                    return {"answer": "", "status": "ERROR", "error": f"AIONA 가 답하지 못했습니다: {stopped[-1][:200]}", "url": self.page.url,
                            "sent": sent, "latency_ms": round((time.monotonic() - started) * 1000), "model": "자동 추천",
                            "method": AIONA_WEB_VERSION, "debug_screenshot": self._snapshot("stopped"),
                            **({"intent_prompt": "그냥 보내기"} if as_is else {})}
                continue
            idle = self.page.locator("button[type=submit][title='전송']").count() > 0   # 답하는 동안은 정지 버튼
            html = self._last_answer_html()
            stable = stable + 1 if html and html == last and idle else 0
            empty = empty + 1 if not html and idle else 0   # 빈 답변('아무것도 출력하지 마') — 말풍선은 생기고 본문이 없다
            last = html
            if stable >= 2 or empty >= 4:
                break
        converted = html_to_markdown(last or "")
        answer = converted["markdown"]
        done = (stable >= 2 and bool(answer.strip())) or (empty >= 4 and not answer.strip())
        return {"answer": answer, "status": "SUCCESS" if done else "INCOMPLETE",
                **({} if done else {"debug_screenshot": self._snapshot("incomplete")}),
                **({"intent_prompt": "그냥 보내기"} if as_is else {}),
                "error": None if done else "답변이 제한 시간 안에 끝나지 않았습니다.", "url": self.page.url, "sent": sent,
                "latency_ms": round((time.monotonic() - started) * 1000), "model": "자동 추천", "method": AIONA_WEB_VERSION,
                "conversion": [q for q in converted.get("quality") or [] if q.get("severity") != "info"]}
