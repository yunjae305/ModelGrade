"""AIONA 웹 자동화 — winz.aiona.kr 에 로그인 상태를 유지하고(별도 Chrome 프로필) 새 채팅으로 질문을 보내 답변을 받는다.

사용자 결정(2026-10-14): AIONA 는 API 키가 없어 웹 브라우저 자동화로 답변을 수집한다 ("새 채팅 눌러서 대화 진행").
- 로그인: ``.env`` 의 AIONA_LOGIN_ID·AIONA_LOGIN_PASSWORD 는 사용자가 직접 넣고, 처음 로그인도 사용자가 실행한다
  (``python tools/web_chat_login.py aiona --auto``). '자동 로그인(30일 유지)'으로 로그인 상태를 ``data/browser_profiles/aiona`` 에
  남긴다. 로그인이 풀렸으면 ``allow_login`` 일 때만 같은 값으로 다시 로그인한다. 비밀번호는 로그·화면·결과 파일에 남기지 않는다.
- 화면 구조(2026-10-14 확인): 새 채팅 = ``/chat``, 입력칸 ``textarea[placeholder^=무엇이든]``, 전송 ``button[type=submit][title=전송]``
  (답하는 동안은 정지 버튼으로 바뀜), 내 질문 ``div.flex.justify-end``, 답변 ``div.flex.justify-start`` 안의 ``.prose``,
  파일 올리기 ``input[type=file]`` (이미지·PDF·DOCX·TXT 등). 모델은 기본값 '자동 추천' 그대로 둔다.
- 답변 본문(HTML)은 표·목록·코드 서식을 살려 Markdown 으로 옮긴다 (``ingestion.html_markdown``).
- 봇 확인·추가 인증 화면이 나오면 멈추고 사람에게 넘긴다 — 우회하지 않는다.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def credentials(env_path: Optional[Path] = None) -> tuple[str, str]:
    """``.env`` 의 로그인 정보. 없으면 오류 (값은 돌려주기만 하고 어디에도 쓰지 않는다)."""
    path = env_path or ROOT / ".env"
    user, password = env_value("AIONA_LOGIN_ID", None, path), env_value("AIONA_LOGIN_PASSWORD", None, path)
    if not user or not password:
        raise AionaWebError(".env 에 AIONA_LOGIN_ID·AIONA_LOGIN_PASSWORD 가 없습니다.")
    return user, password


def login(page: Any, user: str, password: str, timeout_ms: int = 30_000) -> None:
    """로그인 화면을 채우고 '자동 로그인(30일 유지)'을 켠 뒤 로그인한다. 로그인 화면을 벗어나지 못하면 오류."""
    page.goto(f"{AIONA_URL}/login")
    page.locator("input[type=email]").fill(user)
    page.locator("input[type=password]").fill(password)
    toggle = page.locator("xpath=//span[contains(normalize-space(.), '자동 로그인')]/preceding-sibling::button[1]")
    if toggle.count():
        toggle.first.click()
    page.locator("button[type=submit]").click()
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
                 debug_dir: Optional[Path] = None):
        self.headless, self.allow_login, self.answer_timeout_s, self.profile = headless, allow_login, answer_timeout_s, profile
        self.debug_dir = debug_dir   # 실패·미완료 때 그 순간 화면을 남길 곳 (원인 확인용)
        self._playwright = self._context = self.page = None
        self._temp = tempfile.TemporaryDirectory(prefix="aiona-ref-")

    def __enter__(self) -> "AionaChat":
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        self.profile.mkdir(parents=True, exist_ok=True)
        self._context = self._playwright.chromium.launch_persistent_context(
            str(self.profile), channel="chrome", headless=self.headless, viewport={"width": 1400, "height": 900})
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.ensure_login()
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
            self._temp.cleanup()

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
        for f in files:   # 올린 파일 칸이 보일 때까지
            self.page.get_by_text(f.name, exact=False).first.wait_for(timeout=60_000)
        self.page.wait_for_timeout(1500)

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
        last, stable, empty = None, 0, 0
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(1500)
            self._dismiss_popups()
            if self._answers().count() <= before:
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
                "error": None if done else "답변이 제한 시간 안에 끝나지 않았습니다.", "url": self.page.url, "sent": sent,
                "latency_ms": round((time.monotonic() - started) * 1000), "model": "자동 추천", "method": AIONA_WEB_VERSION,
                "conversion": [q for q in converted.get("quality") or [] if q.get("severity") != "info"]}
