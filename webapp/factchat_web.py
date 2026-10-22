"""FactChat(건국대 글로컬 aichat.kku.ac.kr) 웹 자동화 — AIONA 와 같은 방식으로 질문을 보내고 답을 받는다 (사용자 결정 2026-10-14).

- 로그인: ``.env`` 의 FACTCHAT_LOGIN_ID·FACTCHAT_LOGIN_PASSWORD 는 사용자가 직접 넣고, 처음 로그인도 사용자가 실행한다
  (``python tools/web_chat_login.py factchat --auto``). 로그인 화면은 건국대 통합로그인(``auth.factchat.bot/kkug/idpw-login``,
  아이디 ``input[name=id]``·비밀번호 ``input[name=pw]``·'로그인' 버튼 — 2026-10-14 확인). 로그인 상태는
  ``data/browser_profiles/factchat`` 에 남고, 풀렸으면 ``allow_login`` 일 때만 같은 값으로 다시 로그인한다.
  비밀번호는 로그·화면·결과 파일에 남기지 않는다. 실패는 고정 문구로만 알린다(브라우저 예외 문구에 입력값이 섞일 수 있음).
- AIONA 와 프로필이 달라 두 브라우저를 동시에 띄울 수 있다(2026-10-14 이 PC 에서 헤드리스 두 개 동시 실행 확인).
- 채팅 화면 구조(입력칸·답변·복사 버튼)는 첫 로그인 때 ``--inspect`` 로 남긴 구조를 보고 정한다.
"""
from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from code_eval.llm_client import env_value

from .code_eval_service import ROOT

__all__ = ["FACTCHAT_URL", "PROFILE", "FACTCHAT_WEB_VERSION", "FactChatWebError", "credentials", "login", "login_status",
           "inspect_chat", "FactChatChat"]

FACTCHAT_URL = "https://aichat.kku.ac.kr"
AUTH_URL = f"{FACTCHAT_URL}/auth"
PROFILE = ROOT / "data" / "browser_profiles" / "factchat"
FACTCHAT_WEB_VERSION = "factchat-web-v0"
_AUTH_HOSTS = ("auth.factchat.bot", "/auth")


class FactChatWebError(RuntimeError):
    pass


def credentials(env_path: Optional[Path] = None) -> tuple[str, str]:
    """``.env`` 의 로그인 정보. 없으면 오류 (값은 돌려주기만 하고 어디에도 쓰지 않는다)."""
    path = env_path or ROOT / ".env"
    user, password = env_value("FACTCHAT_LOGIN_ID", None, path), env_value("FACTCHAT_LOGIN_PASSWORD", None, path)
    if not user or not password:
        raise FactChatWebError(".env 에 FACTCHAT_LOGIN_ID·FACTCHAT_LOGIN_PASSWORD 가 없습니다.")
    return user, password


def login_status(env_path: Optional[Path] = None, profile: Optional[Path] = None) -> Dict[str, Any]:
    """'연결된 모델' 화면용 — 로그인 정보가 .env 에 있는지, 로그인 상태를 담은 프로필이 있는지만 (값은 돌려주지 않는다)."""
    path = env_path or ROOT / ".env"
    has = bool(env_value("FACTCHAT_LOGIN_ID", None, path)) and bool(env_value("FACTCHAT_LOGIN_PASSWORD", None, path))
    return {"name": "FactChat", "url": FACTCHAT_URL, "version": FACTCHAT_WEB_VERSION, "method": "웹브라우저",
            "credentials": has, "credentials_source": ".env (FACTCHAT_LOGIN_ID · FACTCHAT_LOGIN_PASSWORD)",
            "profile_saved": ((profile or PROFILE) / "Default").is_dir()}


def _on_auth(url: str) -> bool:
    return any(part in url for part in _AUTH_HOSTS)


def login(page: Any, user: str, password: str, timeout_ms: int = 45_000) -> None:
    """FactChat → 건국대 통합로그인 화면을 채우고 로그인한 뒤 채팅 화면으로 돌아올 때까지 기다린다."""
    try:
        page.goto(AUTH_URL, wait_until="domcontentloaded")
        # 한 번 로그인한 뒤에는 통합로그인으로 바로 넘어가지 않고 '포털 시스템 로그인 (최근 사용)' 버튼이 먼저 뜬다 (2026-10-14 사용자 확인)
        portal = page.get_by_role("button", name="포털 시스템 로그인")
        try:
            portal.first.wait_for(timeout=8_000)
            portal.first.click()
        except Exception:
            pass   # 버튼 없이 바로 통합로그인 화면으로 넘어간 경우
        page.wait_for_selector("input[name=id]", timeout=timeout_ms)
        page.locator("input[name=id]").fill(user)
        page.locator("input[name=pw]").fill(password)
        page.locator("button[type=submit]").first.click()
    except Exception:
        raise FactChatWebError("로그인 화면을 채우지 못했습니다 — 로그인 화면을 열지 못했거나 화면 구성이 바뀌었습니다.") from None
    try:
        page.wait_for_url(lambda url: not _on_auth(url), timeout=timeout_ms)
    except Exception:
        raise FactChatWebError("로그인 화면을 벗어나지 못했습니다 — 아이디·비밀번호나 추가 인증 화면을 확인해 주세요.") from None


_INSPECT_JS = """(question) => {
  const pick = (e) => ({tag: e.tagName.toLowerCase(), id: e.id || null, cls: (e.className && e.className.baseVal === undefined ? e.className : '').toString().slice(0, 160),
    name: e.getAttribute('name'), type: e.getAttribute('type'), role: e.getAttribute('role'), placeholder: e.getAttribute('placeholder'),
    aria: e.getAttribute('aria-label'), title: e.getAttribute('title'), testid: e.getAttribute('data-testid'),
    text: (e.innerText || '').trim().slice(0, 80)});
  const all = (sel) => [...document.querySelectorAll(sel)].map(pick);
  const lastWithText = [...document.querySelectorAll('main *, body *')].filter(e => question && (e.innerText || '').includes(question)).pop();
  return {url: location.href, title: document.title,
    inputs: all('textarea, input, [contenteditable=true]'), buttons: all('button, [role=button]'),
    file_inputs: all('input[type=file]'),
    message_html: (document.querySelector('main') || document.body).outerHTML.slice(-60000),
    question_container: lastWithText ? lastWithText.outerHTML.slice(0, 4000) : null};
}"""


def inspect_chat(page: Any, out: Path, question: Optional[str] = None, wait_s: int = 40) -> Path:
    """로그인한 채팅 화면의 구조(입력칸·버튼·메시지 HTML)를 파일로 남긴다 — 수집 코드의 선택자를 정하는 데 쓴다.
    ``question`` 을 주면 그 질문을 한 번 보내고 답이 나온 뒤의 구조도 남긴다(답변 영역·복사 버튼 확인용)."""
    page.goto(FACTCHAT_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    result: Dict[str, Any] = {"before": page.evaluate(_INSPECT_JS, None)}
    if question:
        # 수집과 같은 입력 — 키보드 입력 + '메시지 보내기' 버튼 (fill + Enter 는 이 편집기에서 보내지지 않는 경우가 있다, Codex 리뷰)
        page.locator(EDITOR).first.click()
        page.keyboard.insert_text(question)
        page.locator(SEND_BUTTON).first.click(timeout=15_000)
        page.wait_for_selector(USER_MESSAGE, timeout=30_000)
        page.wait_for_timeout(wait_s * 1000)
        result["after"] = page.evaluate(_INSPECT_JS, question)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


# --- 질문·답변 (2026-10-14 첫 로그인 때 남긴 구조로 정함 — data/browser_profiles/factchat-inspect.json) -------------------
# 메시지: div.message-list-item[data-message-role=user|assistant], 답변: div.ai-message[data-testid=ai-message] 안 .ai-message-content,
# 답변 아래 버튼: Info · 복사(aria-label='복사') · 답변 인쇄 · 좋아요 · 싫어요, 입력칸: div.mi-editor-input[contenteditable] (Enter 로 보냄),
# 첨부: input[type=file], 전송: 글을 넣으면 생기는 '메시지 보내기' 버튼. 상단 에이전트 선택은 기본값('Super Agent') 그대로 둔다.
EDITOR = "div.mi-editor-input[contenteditable=true]"
ANSWER = "div.ai-message[data-testid=ai-message]"
COPY_BUTTON = "button[aria-label='복사']"
USER_MESSAGE = "div.message-list-item[data-message-role=user]"
SEND_BUTTON = "button[aria-label='메시지 보내기']"
REFERENCE_FILE_CHARS = 20_000   # 이보다 긴 참조 자료(J7)는 입력칸 대신 TXT 파일로 올린다 (AIONA 와 같음)

# 복사 버튼이 클립보드에 쓰는 글을 가로챈다 — 실제 클립보드는 쓰지 않아도 되고(헤드리스에서 거절돼도 됨), 이용자가 복사해 붙여
# 넣을 때 얻는 원문 그대로를 답변으로 받는다 (사용자 결정 2026-10-14: 복사 버튼 원문으로 수집)
COPY_CAPTURE_JS = r"""(() => {
  // 판 2(10-21 밤): 이미 판 1 이 걸린 열린 페이지(ChatGPT 앱)에도 다시 씌운다 — 판 1 은 실제 클립보드에도 썼다
  if (window.__mgCopyHooked === 2) return; window.__mgCopyHooked = 2; window.__mgCopied = window.__mgCopied || [];
  const keep = (t) => { if (typeof t === 'string') window.__mgCopied.push(t); };
  const clip = navigator.clipboard;
  if (clip) {
    const writeText = clip.writeText ? clip.writeText.bind(clip) : null;
    clip.writeText = (t) => { keep(String(t)); return Promise.resolve(); };   // 실제 클립보드에는 안 씀 — 동시 수집 중 다른 앱 수집기의 붙여넣기를 덮었다(10-21 밤)
    const write = clip.write ? clip.write.bind(clip) : null;
    clip.write = async (items) => {
      try { for (const item of items) if (item.types.includes('text/plain')) keep(await (await item.getType('text/plain')).text()); } catch (e) {}
      return undefined;   // 실제 클립보드에는 안 씀
    };
  }
  const setData = DataTransfer.prototype.__mgSetData || DataTransfer.prototype.setData;   // 판 1 이 감싼 것을 또 감싸지 않게 원래 것을 둔다
  DataTransfer.prototype.__mgSetData = setData;
  DataTransfer.prototype.setData = function (type, value) { if (/text\/plain|^text$/i.test(type)) keep(String(value)); return setData.call(this, type, value); };
})();"""


class FactChatChat:
    """한 브라우저로 FactChat 에 차례로 질문한다. Playwright 동기 API 라 만든 스레드에서만 쓴다 (AionaChat 과 같은 모양).

        with FactChatChat() as chat:
            chat.new_chat()
            out = chat.ask("질문", files=[Path(...)])
    """

    def __init__(self, *, headless: bool = True, allow_login: bool = True, answer_timeout_s: int = 600, profile: Path = PROFILE,
                 debug_dir: Optional[Path] = None, cancelled: Optional[Any] = None):
        self.headless, self.allow_login, self.answer_timeout_s, self.profile = headless, allow_login, answer_timeout_s, profile
        self.debug_dir = debug_dir
        self.cancelled = cancelled or (lambda: False)   # 프로필 잠금을 기다리는 동안·답을 기다리는 동안 중단 요청을 본다
        self._playwright = self._context = self.page = None
        self._temp = tempfile.TemporaryDirectory(prefix="factchat-ref-")
        self._lock = None
        self._home = FACTCHAT_URL

    def __enter__(self) -> "FactChatChat":
        from .aiona_web import _profile_lock
        lock = _profile_lock(self.profile)
        while not lock.acquire(timeout=1.0):   # 같은 프로필을 쓰는 다른 실행이 끝날 때까지 — 중단 요청이면 그만둔다
            if self.cancelled():
                raise FactChatWebError("중단 요청으로 FactChat 수집을 시작하지 않았습니다.")
        self._lock = lock
        try:
            if self.cancelled():   # 잠금을 바로 얻었어도 이미 중단 요청이면 브라우저를 열지 않는다 (Codex — 우수AI 와 같은 문제)
                raise FactChatWebError("중단 요청으로 FactChat 수집을 시작하지 않았습니다.")
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self.profile.mkdir(parents=True, exist_ok=True)
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile), channel="chrome", headless=self.headless, viewport={"width": 1400, "height": 900})
            self._context.add_init_script(COPY_CAPTURE_JS)
            self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self.ensure_login()
        except BaseException:   # 실행·로그인 실패 때도 브라우저·임시 폴더·프로필 잠금을 정리한다
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
        self.page.goto(FACTCHAT_URL, wait_until="domcontentloaded")
        try:
            self.page.wait_for_url(lambda url: "/dashboard/chat" in url or _on_auth(url), timeout=30_000)
        except Exception:
            pass
        if _on_auth(self.page.url):
            if not self.allow_login:
                raise FactChatWebError("FactChat 로그인이 풀렸습니다. 'python tools/web_chat_login.py factchat --auto' 로 다시 로그인해 주세요.")
            login(self.page, *credentials())
        self._settle()
        parts = self.page.url.split("/dashboard/chat/")
        if len(parts) == 2:   # 새 채팅 주소 — /dashboard/chat/<작업 공간 번호> (그 뒤 대화 ID 는 뺀다)
            self._home = parts[0] + "/dashboard/chat/" + parts[1].split("/")[0]

    def _snapshot(self, label: str) -> Optional[str]:
        if self.debug_dir is None:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.debug_dir / f"{time.strftime('%Y%m%dT%H%M%S')}-factchat-{label}.png"
            self.page.screenshot(path=str(path))
            return path.name
        except Exception:
            return None

    def _settle(self) -> None:
        """새 채팅 화면이 자리 잡을 때까지 — 처음 열면 /dashboard/chat 에서 /dashboard/chat/<작업 공간 번호> 로 한 번 더 이동한다.
        이동 전에 보내면 보낸 뒤 화면이 새 채팅으로 바뀌어 답을 놓친다 (2026-10-14 확인)."""
        try:
            self.page.wait_for_url(re.compile(r"/dashboard/chat/\d+"), timeout=15_000)
        except Exception:
            pass
        self.page.locator(EDITOR).first.wait_for(timeout=30_000)
        self.page.wait_for_timeout(1000)

    def new_chat(self) -> None:
        self.page.goto(self._home, wait_until="domcontentloaded")
        self._settle()

    def _answers(self) -> Any:
        return self.page.locator(ANSWER)

    def _last_answer_html(self) -> str:
        answers = self._answers()
        if not answers.count():
            return ""
        return answers.last.evaluate("(el) => { const c = el.querySelector('.ai-message-content'); return c ? c.innerHTML : ''; }")

    def _copy_last_answer(self) -> Optional[str]:
        """마지막 답변의 복사 버튼을 눌러 페이지가 클립보드에 쓰는 원문을 받는다. 못 받으면 None."""
        try:
            self.page.evaluate("() => { window.__mgCopied = []; }")
            self._answers().last.locator(COPY_BUTTON).first.click(timeout=5000, force=True)
            self.page.wait_for_function("() => (window.__mgCopied || []).length > 0", timeout=5000)
            return self.page.evaluate("() => window.__mgCopied[window.__mgCopied.length - 1]")
        except Exception:
            return None

    def _agent(self) -> Optional[str]:
        try:
            return self.page.evaluate("() => [...document.querySelectorAll('button')].map(b => (b.innerText || '').trim()).find(t => /Agent$/.test(t)) || null")
        except Exception:
            return None

    def _upload(self, files: List[Path]) -> None:
        self.page.locator("input[type=file]").first.set_input_files([str(f) for f in files])
        for f in files:   # 올린 파일 이름이 화면에 보일 때까지
            self.page.wait_for_function("(name) => document.body.innerText.includes(name)", arg=f.name, timeout=60_000)
        self.page.wait_for_timeout(1500)

    def ask(self, question: str, files: Optional[List[Path]] = None, reference: Any = None) -> Dict[str, Any]:
        """현재 채팅에 질문을 보내고 답이 끝날 때까지 기다린다 → {answer, status, error, url, sent, latency_ms, model, capture}.
        답변은 복사 버튼 원문(capture='copy-button'), 복사를 못 받으면 화면 HTML 변환(capture='html')."""
        from .ingestion.html_markdown import html_to_markdown
        started = time.monotonic()
        files = list(files or [])
        sent = [{"name": f.name, "how": "파일 그대로"} for f in files]
        text = question
        if reference not in (None, "", {}, []):
            reference_text = reference if isinstance(reference, str) else json.dumps(reference, ensure_ascii=False)
            if len(reference_text) > REFERENCE_FILE_CHARS:   # 긴 참조 자료는 TXT 파일로 올린다
                path = Path(self._temp.name) / "참조자료.txt"
                path.write_text(reference_text, encoding="utf-8")
                files.append(path)
                sent.append({"name": path.name, "how": "참조 자료를 TXT 파일로 올림"})
            else:
                text = f"[참조 자료]\n{reference_text}\n\n{question}"
        base = {"sent": sent, "method": FACTCHAT_WEB_VERSION}
        before = self._answers().count()
        try:
            if files:
                self._upload(files)
            # Lexical 편집기 — fill 로 넣으면 화면에만 보이고 편집기 상태에 들어가지 않아 Enter 가 보내지 않는다(2026-10-14 확인).
            # 키보드 입력으로 넣고 Enter 로 보낸 뒤, 내 메시지가 실제로 생겼는지 확인한다.
            sent_before = self.page.locator(USER_MESSAGE).count()
            editor = self.page.locator(EDITOR).first
            editor.click()
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Delete")
            self.page.keyboard.insert_text(text)
            send = self.page.locator(SEND_BUTTON).first   # 글이 들어가면 '메시지 보내기' 버튼이 생긴다 (Enter 로는 보내지지 않는 경우가 있음)
            send.wait_for(timeout=15_000)
            send.click()
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
            ready = self._answers().last.locator(COPY_BUTTON).count() > 0   # 답이 끝나면 복사 버튼이 생긴다
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
                "latency_ms": round((time.monotonic() - started) * 1000), "model": self._agent() or "기본 에이전트",
                **({"conversion": quality} if quality else {}), **({} if done else {"debug_screenshot": self._snapshot("incomplete")})}


def main(argv: Optional[List[str]] = None) -> int:
    """직접 시험 — 저장소 루트에서  python -m webapp.factchat_web "질문" [--show] [--file 경로 ...]
    로그인이 풀렸으면 .env 의 FACTCHAT_LOGIN_ID·PASSWORD 로 다시 로그인한다. 답변(복사 버튼 원문)과 상태를 출력한다."""
    import argparse
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="FactChat 웹에 질문 하나를 보내고 답변을 받는다")
    parser.add_argument("question")
    parser.add_argument("--show", action="store_true", help="브라우저 창을 보이게 연다")
    parser.add_argument("--file", nargs="*", default=[], help="첨부 파일 경로")
    args = parser.parse_args(argv)
    with FactChatChat(headless=not args.show) as chat:
        chat.new_chat()
        out = chat.ask(args.question, [Path(f) for f in args.file])
    print(json.dumps({key: out.get(key) for key in ("status", "capture", "model", "latency_ms", "error", "url")}, ensure_ascii=False))
    print(out.get("answer") or "")
    return 0 if out.get("status") == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
