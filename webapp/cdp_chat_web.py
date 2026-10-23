"""ChatGPT · Gemini · Claude 웹 수집 — 수집용 Chrome(tools/chrome_cdp.py: 평소 Chrome + 전용 프로필 + 디버깅 포트)에 붙어 사이트마다 자기 창에서
질문을 보내고 답을 받는다. AIONA 는 webapp.aiona_web(attach=True)으로 같은 Chrome 에 붙는다 — 10-23 확정: 네 모델 모두 이 방식, 데스크톱 앱 수집기는 없앰.

- 로그인 · 봇 확인은 사용자가 그 Chrome 창에서 직접 한 번(프로필에 남음). 프로그램은 자격 증명을 다루지 않고 봇 확인을 우회하지 않는다(막히면 고정 문구로 오류).
- 입력은 포트(CDP)로 넣고 답은 복사 아이콘을 눌러 페이지 안에서 가로챈다(COPY_CAPTURE_JS) — 시스템 클립보드 · 포커스를 쓰지 않고 창을 앞으로 끌어오지
  않으므로 수집 중에도 사용자가 컴퓨터를 쓸 수 있다. 복사가 안 되면 답변 영역의 글을 쓴다(capture 에 표시).
- 새 채팅마다 모델 설정을 맞춘다 — ChatGPT: Chat 모드 · GPT-6 · 추론 Medium / Gemini: 3.8 Flash · 사고 수준 보통 / Claude: Sonnet 5.5 · 노력 중간
  (시작할 때 고른 모델이 있으면 그 모델, tools/modelgrade.APP_MODELS).
- 채팅 하나로 멀티턴(대화는 new_chat 뒤 같은 창에서 턴 순서대로), 첨부는 파일 입력에 그대로, 긴 참조 자료(J7)는 TXT 파일로 올린다(AIONA 와 같음).
- 화면 구조는 각 클래스 상수 — 바뀌면 상수만 고친다.
"""
from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .code_eval_service import ROOT
from .factchat_web import COPY_CAPTURE_JS

__all__ = ["CDP_WEB_VERSION", "CdpWebError", "cdp_login_status", "ChatGptChat", "GeminiChat", "ClaudeChat", "CDP_CHATS", "CDP_PROVIDERS"]

CDP_WEB_VERSION = "cdp-web-v4"   # v4(10-23): 평소 Chrome + 포트, 창 앞으로 끌어오지 않음, 네 모델 모두 이 방식
REFERENCE_FILE_CHARS = 20_000   # 이보다 긴 참조 자료는 입력칸 대신 TXT 파일로 올린다 (AIONA 와 같음)
#: 세 사이트가 같은 Chrome 을 쓰므로 메뉴 조작·전송은 한 번에 하나씩 — 탭 간 포커스 경쟁을 막는다
_CHROME_LOCK = threading.Lock()
#: 한도·봇 확인 문구 — 보이면 기다리지 않고 오류로 남긴다 (우회하지 않음)
_BOT_BLOCKED = ("보안 확인 수행 중", "Verify you are human", "사용자가 봇이 아닌지 확인", "Just a moment")   # 'Cloudflare' 낱말은 채팅 제목에도 나와 쓰지 않는다
_LIMIT_BLOCKED = ("너무 많은 요청", "Too many requests", "사용량 한도", "usage limit", "한도에 도달했", "메시지 한도", "limit reached", "plan limit")
_BLOCKED = _BOT_BLOCKED + _LIMIT_BLOCKED   # 10-23: '한도에 도달'(대화 글에 흔함) 대신 '한도에 도달했' — 말풍선 글은 _check_blocked 가 뺀다


# --- 창 배치 (동시 수집: 네 창이 서로 덮지 않게) -------------------------------------------------------------
#: 자리 → (x 비율, y 비율, 폭 비율, 높이 비율). 앱 창(Claude 왼쪽·ChatGPT 오른쪽)은 폭 60%(ChatGPT 앱은 물리 1,600px 보다 좁으면 모드 버튼을 숨긴다),
#: 웹 창(Gemini·AIONA)은 오른쪽 40% 를 위아래로 — Claude 창(마우스로 조작)과 겹치지 않는다.
PLACE_RECTS = {"left": (0.0, 0.0, 0.6, 1.0), "right": (0.4, 0.0, 0.6, 1.0), "top-right": (0.6, 0.0, 0.4, 0.5), "bottom-right": (0.6, 0.5, 0.4, 0.5)}


def place_rect(place: str, width: int, height: int) -> tuple:
    fx, fy, fw, fh = PLACE_RECTS[place]
    return int(width * fx), int(height * fy), int(width * fw), int(height * fh)


def cdp_place_window(page: Any, place: str) -> None:
    """CDP 로 브라우저 창(Chrome·Playwright Chromium)을 화면 자리로 옮긴다 — 좌표는 페이지가 보는 screen 크기(논리 픽셀) 기준. 실패해도 수집은 계속."""
    try:
        sw, sh = page.evaluate("() => [screen.availWidth, screen.availHeight]")
        x, y, w, h = place_rect(place, int(sw), int(sh))
        session = page.context.new_cdp_session(page)
        wid = session.send("Browser.getWindowForTarget")["windowId"]
        session.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        session.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"left": x, "top": y, "width": w, "height": h}})
        session.detach()
    except Exception:
        pass


class CdpWebError(RuntimeError):
    pass


def cdp_login_status() -> Dict[str, Any]:
    """'연결된 모델' 화면용 — 수집용 Chrome 이 떠 있는지(포트 응답)와 열린 사이트 탭. 자격 증명은 다루지 않는다."""
    from tools.chrome_cdp import PROFILE, status
    st = status()
    tabs = [t["url"] for t in st["tabs"]]
    return {"method": "웹브라우저 (수집용 Chrome, 로그인 상태)", "version": CDP_WEB_VERSION, "credentials": st["ok"],
            "credentials_source": "python tools/chrome_cdp.py 로 띄운 Chrome 에서 사용자가 직접 로그인",
            "profile_saved": (PROFILE / "Default").is_dir(),
            "tabs": {name: any(host in u for u in tabs) for name, host in (("chatgpt", "chatgpt.com"), ("gemini", "gemini.google.com"), ("claude", "claude.ai"))}}


class _CdpChat:
    """공통 — 연결·탭·전송·대기·복사. 사이트별 값은 하위 클래스 상수."""
    NAME = ""
    HOST = ""
    NEW_URL = ""
    MODEL_LABEL = ""          # 결과에 남길 모델 이름
    MODEL_NAME = ""           # 모델 메뉴에서 고르는 이름(시작할 때 바꿀 수 있음)
    LEVEL = ""                # 노력 · 추론 · 사고 수준 표기(결과 이름용)
    TEXTBOX_NAME = ""         # 입력칸 textbox 의 접근성 이름(앞부분)
    ANSWER_SELECTOR = ""      # 답변 말풍선
    USER_SELECTOR = ""        # 내 질문 말풍선 — 전송 확인용
    COPY_NAMES: tuple = ()    # 답변 복사 버튼 이름 후보 (마지막 것을 누른다)
    FILE_INPUT = ""           # 숨은 파일 input (없으면 메뉴 → 파일 선택 창)
    STOP_NAMES: tuple = ()    # 답하는 동안 보이는 정지 버튼 이름
    SEND_NAMES: tuple = ()    # 보내기 버튼 이름 — 파일 처리 중에는 꺼져 있다 (켜질 때까지 기다린다)
    REMOVE_FILE = None        # 입력칸에 남은 첨부를 지우는 버튼 이름 패턴 (지난 질문이 실패해 남은 파일)

    def __init__(self, *, headless: bool = False, debug_dir: Optional[Path] = None, cancelled: Optional[Callable[[], bool]] = None,
                 answer_timeout_s: int = 600, place: Optional[str] = None, model: Optional[str] = None) -> None:
        self.debug_dir, self.cancelled, self.answer_timeout_s, self.place = debug_dir, cancelled or (lambda: False), answer_timeout_s, place
        if model:   # 시작할 때 고른 모델 (10-22, 예 'Opus 5.5' · 'GPT-5.6 Sol' · '3.1 Pro') — 없으면 클래스 기본값
            self.MODEL_NAME = model
            self.MODEL_LABEL = f"{model} · {self.LEVEL} (웹)"
        self.page: Any = None
        self._playwright = self._browser = None
        self._temp = tempfile.TemporaryDirectory(prefix=f"{self.NAME}-ref-")
        self.model_seen: Optional[str] = None

    # --- 연결 ---------------------------------------------------------------------------------------
    def __enter__(self) -> "_CdpChat":
        from tools.chrome_cdp import attach, find_or_open
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        try:
            try:
                self._browser = attach(self._playwright)   # 수집용 Chrome(보조 프로세스가 띄움)에 붙는다 — 로그인은 그 프로필에 남아 있다
            except Exception as exc:
                raise CdpWebError(f"수집용 Chrome 에 붙지 못했습니다: {str(exc).splitlines()[0][:160]}")
            self.page = find_or_open(self._browser, self.HOST, self.NEW_URL or f"https://{self.HOST}/")   # 수집기마다 자기 창
            self.page.add_init_script(COPY_CAPTURE_JS)
            self.page.set_default_timeout(60_000)   # 시간 제한을 따로 주지 않은 동작도 1분이면 끝난다
            if self.place:
                cdp_place_window(self.page, self.place)
        except BaseException:
            self._close()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        self._close()

    def _close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()   # CDP 연결만 끊는다 — Chrome 창과 로그인 상태는 그대로
        except Exception:
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._browser = self._playwright = None
        self._temp.cleanup()

    def _eval(self, fn: str, timeout_ms: int = 20_000) -> Any:
        """시간 제한이 있는 page.evaluate — 탭이 응답하지 않으면 무한정 기다리지 않고 예외로 끝낸다 (Gemini 수집 스레드 멈춤 2026-10-15).
        ``fn`` 은 '() => 값' 형태. 값이 0·빈 문자열이어도 끝나도록 객체에 담아 기다린다(간격 폴링 — 뒤 탭에서도 돈다)."""
        handle = self.page.wait_for_function(f"() => ({{v: ({fn})()}})", timeout=timeout_ms, polling=250)
        return handle.json_value()["v"]

    def _snapshot(self, label: str) -> Optional[str]:
        if not self.debug_dir or self.page is None:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.debug_dir / f"{int(time.time())}-{label}.png"
            self.page.screenshot(path=str(path))
            return str(path)
        except Exception:
            return None

    # --- 채팅 ---------------------------------------------------------------------------------------
    def new_chat(self) -> None:
        with _CHROME_LOCK:
            self._page_front()   # 뒤에 있는 탭은 메뉴가 그려지지 않아 설정이 실패한다 (동시 실행 시험 2026-10-15)
            from tools.chrome_cdp import goto_tolerant
            goto_tolerant(self.page, self.NEW_URL)   # Gemini 는 /app 이 바로 다른 주소로 바뀌어 ERR_ABORTED 가 난다 (2026-10-19)
            self.page.wait_for_timeout(3000)
            self.page.evaluate(COPY_CAPTURE_JS)
            self._check_blocked()
            try:   # 입력칸·설정 버튼이 늦게 뜨는 화면이 있다 — 최대 20초
                self.page.get_by_role("textbox", name=re.compile(re.escape(self.TEXTBOX_NAME))).first.wait_for(state="visible", timeout=20_000)
            except Exception:
                pass
            self.page.wait_for_timeout(1500)
            try:
                self.model_seen = self._apply_model()
            except Exception as exc:
                self.model_seen = f"{self.MODEL_LABEL} (설정 확인 못 함: {type(exc).__name__}: {str(exc)[:80]})"
                self._snapshot("model-settings-failed")

    def _apply_model(self) -> str:   # 사이트별 — 설정을 맞추고 화면에 보이는 모델 표시를 돌려준다
        return self.MODEL_LABEL

    def _close_overlays(self) -> None:
        """입력칸을 덮은 사이드바·메뉴를 닫는다 — 좁은 창에서는 사이드바가 화면 위에 겹쳐 열린다."""
        self.page.keyboard.press("Escape")
        for name in ("사이드바 닫기", "기본 메뉴 닫기", "메뉴 닫기", "닫기", "Close sidebar"):
            btn = self.page.get_by_role("button", name=name, exact=True)
            try:
                if btn.count() and btn.first.is_visible():
                    btn.first.click(timeout=3000)
                    self.page.wait_for_timeout(600)
                    return
            except Exception:
                continue

    def _check_blocked(self) -> None:
        """한도·봇 확인 문구가 화면에 있는가 — 채팅 말풍선(질문·답) 글은 빼고 본다. 대화 내용에 '한도에 도달' 같은 말이 나오면 오탐(Gemini Q18, 2026-10-20)."""
        sel = ", ".join(x for x in (self.ANSWER_SELECTOR, self.USER_SELECTOR) if x)
        # 말풍선(질문 · 답) 안의 글자 노드는 건너뛰고 나머지 화면 글만 모은다 — innerText 를 split 으로 빼던 방식은 공백 차이로 못 뺐다(10-23 GPT Q22)
        js = """(sel) => { const skip = sel ? Array.from(document.querySelectorAll(sel)) : [];
            const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let t = ''; let n;
            while ((n = w.nextNode())) { if (skip.some(e => e.contains(n))) continue; const v = (n.nodeValue || '').trim(); if (v) t += v + ' '; if (t.length > 8000) break; }
            return t; }"""
        try:
            text = self.page.evaluate(js, sel) or ""
        except Exception:
            text = self._eval("() => document.body.innerText.slice(0, 4000)") or ""
        low = text.lower()
        hit = next((p for p in _LIMIT_BLOCKED if p.lower() in low), None)
        if hit:
            self._snapshot("blocked")
            raise CdpWebError(f"{self.NAME} 사용량 한도로 막혀 있습니다('{hit}') — 풀릴 때까지 기다립니다.")
        hit = next((p for p in _BOT_BLOCKED if p.lower() in low), None)
        if hit:
            self._snapshot("blocked")
            raise CdpWebError(f"{self.NAME} 봇 확인 화면입니다('{hit}') — Chrome 창에서 직접 확인을 마쳐 주세요(프로그램은 우회하지 않음).")

    def _answers(self) -> Any:
        return self.page.locator(self.ANSWER_SELECTOR)

    def _menu_pick(self, opener_role: str, opener_name: str, path: List[str], *, exact_last: bool = False) -> None:
        """메뉴를 열고(path 의 중간 항목은 하위 메뉴) 마지막 항목을 누른다."""
        opener = self.page.get_by_role(opener_role, name=opener_name).first
        opener.wait_for(state="visible", timeout=15_000)
        try:
            self._menu_walk(opener, path, exact_last)
        finally:   # 실패해도 열린 메뉴를 닫는다 — 열린 채 두면 입력칸을 가린다
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(300)

    def _menu_walk(self, opener: Any, path: List[str], exact_last: bool) -> None:
        opener.click(timeout=5000)
        self.page.wait_for_timeout(700)
        for i, name in enumerate(path):
            last = i == len(path) - 1
            item = None
            for role in ("menuitemradio", "menuitem", "menuitemcheckbox", "option"):
                cand = self.page.get_by_role(role, name=name, exact=exact_last and last)
                if cand.count():
                    item = cand.first
                    break
            if item is None:
                raise CdpWebError(f"{self.NAME} 메뉴 항목 '{name}' 이 없습니다")
            item.hover()
            self.page.wait_for_timeout(400)
            item.click(timeout=5000)
            self.page.wait_for_timeout(800)

    def _upload(self, files: List[Path]) -> None:
        if self.FILE_INPUT:
            self.page.locator(self.FILE_INPUT).first.set_input_files([str(f) for f in files])
        else:
            self._upload_via_menu(files)
        self.page.wait_for_timeout(2500 + 1500 * len(files))   # 업로드 처리 시간 — 미리보기가 붙을 때까지 (크면 더)

    def _upload_via_menu(self, files: List[Path]) -> None:
        raise CdpWebError(f"{self.NAME} 파일 업로드 경로가 없습니다")

    def _copy_last_answer(self) -> Optional[str]:
        """마지막 답변의 복사 버튼(이름 정확히 일치) — 먼저 그 답변 말풍선 안에서, 없으면 화면 전체의 마지막 것. 내 질문의 복사 버튼은 이름이 달라 섞이지 않는다."""
        before = self._eval("() => (window.__mgCopied || []).length")
        scopes = [self._answers().last, self.page]
        for scope in scopes:
            for name in self.COPY_NAMES:
                try:
                    btns = scope.get_by_role("button", name=name, exact=True)
                    if not btns.count():
                        continue
                    try:
                        btns.last.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    try:
                        scope.hover(timeout=1500) if scope is not self.page else None   # 말풍선에 마우스를 올려야 툴바가 그려지는 화면
                    except Exception:
                        pass
                    try:
                        btns.last.click(timeout=3000)
                    except Exception:
                        btns.last.dispatch_event("click")   # 다른 요소가 포인터를 가로채면 DOM 에 바로 클릭 (Gemini Q15 복사 실패, 2026-10-20)
                    self.page.wait_for_timeout(600)
                    copied = self._eval("() => window.__mgCopied || []")
                    if len(copied) > before and isinstance(copied[-1], str) and copied[-1].strip():
                        return copied[-1]
                except Exception:
                    continue
        return None

    def _page_front(self) -> None:
        """아무것도 하지 않는다 — 전에는 창을 OS 맨 앞으로 가져왔지만(앱 수집기 때문), 10-23 부터 수집 중에도 사용자가 컴퓨터를 쓸 수 있게
        창을 건드리지 않는다. 가려진 · 뒤에 있는 창도 그리도록 Chrome 을 띄우므로(tools/chrome_cdp.RENDER_FLAGS) 메뉴 · 복사가 그대로 된다."""
        return None

    def recopy(self) -> str:
        """마지막 답의 복사 버튼을 한 번 더 — 빈 답이 왔을 때 재시도 (사용자 규칙 10-20)."""
        try:
            with _CHROME_LOCK:
                self._page_front()
                return self._copy_last_answer() or ""
        except Exception:
            return ""

    def _last_answer_text(self) -> str:
        try:
            return self._answers().last.inner_text(timeout=2000)
        except Exception:
            return ""

    def _streaming(self) -> bool:
        btn = self.page.get_by_role("button", name=re.compile("|".join(re.escape(n) for n in self.STOP_NAMES)))
        try:
            return any(b.is_visible() for b in btn.all()[:4])
        except Exception:
            return False

    # --- 빈 출력 요구 질문 (사용자 규칙 2026-10-20 밤) ---------------------------------------------------------------
    EMPTY_OUTPUT_WAIT_S = 15   # 빈 출력을 요구한 질문은 이 안에 복사 버튼이 안 뜨면('출력 없음'·응답 중지·말풍선 없음) 기다리지 않고 보류(COPY_FAILED)

    @staticmethod
    def _asks_empty(question: str) -> bool:
        from .run_summary import asks_empty_output
        return asks_empty_output(None, question)

    def _copy_button_count(self, scope: Any = None) -> int:
        n = 0
        for name in self.COPY_NAMES:
            try:
                n += (scope if scope is not None else self.page).get_by_role("button", name=name, exact=True).count()
            except Exception:
                pass
        return n

    def _copy_button_ready(self, answers_before: int, copy_before: int) -> bool:
        """답 말풍선이 생기고 복사 버튼이 새로 떴는가 — 말풍선 안에 있거나, 화면 전체의 복사 버튼 수가 보내기 전보다 늘었거나."""
        try:
            answers = self._answers()
            if answers.count() <= answers_before:
                return False
            return self._copy_button_count(answers.last) > 0 or self._copy_button_count() > copy_before
        except Exception:
            return False

    def _hold_if_no_copy_button(self, answers_before: int, copy_before: int, started: float, base: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """빈 출력 요구 질문: EMPTY_OUTPUT_WAIT_S 초 동안 복사 버튼을 기다려 뜨면 None(평소대로 받음), 안 뜨면 보류 결과."""
        deadline = time.monotonic() + self.EMPTY_OUTPUT_WAIT_S
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(500)
            if self._copy_button_ready(answers_before, copy_before):
                return None
        return {**base, "answer": "", "status": "COPY_FAILED", "capture": "none", "url": self.page.url,
                "error": f"빈 출력 요구 — {self.EMPTY_OUTPUT_WAIT_S}초 안에 복사 버튼이 뜨지 않음(출력 없음·응답 중지 등) → 보류 (사용자 규칙 10-20 밤)",
                "latency_ms": round((time.monotonic() - started) * 1000), "debug_screenshot": self._snapshot("empty-output-no-copy")}

    def _box_text(self, box: Any) -> str:
        try:
            return (box.inner_text(timeout=2000) or "").strip()
        except Exception:
            return ""

    def _click_box(self, box: Any) -> None:
        try:
            box.click(timeout=5_000)
        except Exception:   # 창이 좁으면 사이드바가 입력칸을 덮는다(Gemini, 2026-10-15) — 닫고 한 번 더
            self._close_overlays()
            box.click(timeout=10_000)

    def _clear_composer(self, box: Any) -> None:
        """지난 질문이 보내지지 못하고 남긴 글·첨부를 지운다 — ChatGPT 는 보내지 않은 글을 새 채팅에도 남겨,
        다음 질문이 그 사이에 끼어 뒤섞였다 (긴 참조 자료 TXT 업로드 중 Enter 무시, 2026-10-15)."""
        if self.REMOVE_FILE is not None:
            try:   # 입력칸이 든 양식 안의 제거 버튼만 — 화면 다른 곳(파일 목록 등)의 같은 이름 버튼을 누르지 않게
                scope = box.locator("xpath=ancestor::form[1]")
                scope = scope if scope.count() else self.page
            except Exception:
                scope = self.page
            for _ in range(10):
                btn = scope.get_by_role("button", name=self.REMOVE_FILE)
                if not btn.count():
                    break
                try:
                    btn.first.click(timeout=3000)
                    self.page.wait_for_timeout(400)
                except Exception:
                    break
        if self._box_text(box):
            self._click_box(box)
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Delete")
            self.page.wait_for_timeout(300)
            if self._box_text(box):   # 포커스가 입력칸 밖이면 Ctrl+A 가 페이지를 골라 지워지지 않는다 — 섞인 질문을 보내지 않게 멈춘다
                raise CdpWebError("입력칸에 남은 글을 지우지 못함")

    def _send_button(self) -> Any:
        return self.page.get_by_role("button", name=re.compile("^(" + "|".join(re.escape(n) for n in self.SEND_NAMES) + ")$"))

    def _wait_send_ready(self, timeout_ms: int) -> None:
        """보내기 버튼이 켜질 때까지 — 큰 파일은 올린 뒤 처리하는 동안 버튼이 꺼져 있어 Enter 가 무시된다.
        버튼이 잠깐 안 보이는(다른 버튼으로 바뀐) 동안에도 바로 돌아가지 않고 기다린다 — 5초 넘게 계속 없을 때만 돌아간다."""
        if not self.SEND_NAMES:
            return
        btn = self._send_button()
        waited = missing = 0
        while waited < timeout_ms:
            try:
                if btn.count():
                    missing = 0
                    if any(b.is_enabled() for b in btn.all()[:3]):
                        return
                else:
                    missing += 1000
                    if missing > 5000:
                        return
            except Exception:
                return
            self.page.wait_for_timeout(1000)
            waited += 1000
        raise CdpWebError(f"{timeout_ms // 1000}초가 지나도 보내기 버튼이 켜지지 않음")

    def _press_send(self) -> None:
        """보내기 버튼(켜진 것)을 누른다 — 버튼 이름을 모르는 사이트나 버튼이 없으면 Enter."""
        if self.SEND_NAMES:
            btn = self._send_button()
            try:
                for b in btn.all()[:3]:
                    if b.is_visible() and b.is_enabled():
                        b.click(timeout=5_000)
                        return
            except Exception:
                pass
        self.page.keyboard.press("Enter")

    def _asked(self) -> int:
        return self.page.locator(self.USER_SELECTOR).count() if self.USER_SELECTOR else 0

    def ask(self, question: str, files: Optional[List[Path]] = None, reference: Any = None) -> Dict[str, Any]:
        """현재 채팅에 질문을 보내고 답변이 끝날 때까지 기다린다 → {answer, status, error, url, sent, latency_ms, model, method, capture}."""
        started = time.monotonic()
        files = list(files or [])
        sent = [{"name": f.name, "how": "파일 그대로"} for f in files]
        text = question
        if reference not in (None, "", {}, []):
            reference_text = reference if isinstance(reference, str) else str(reference)
            if len(reference_text) > REFERENCE_FILE_CHARS:
                path = Path(self._temp.name) / "참조자료.txt"
                path.write_text(reference_text, encoding="utf-8")
                files.append(path)
                sent.append({"name": path.name, "how": "참조 자료를 TXT 파일로 올림"})
            else:
                text = f"[참조 자료]\n{reference_text}\n\n{question}"
        base = {"url": None, "sent": sent, "model": self.model_seen or self.MODEL_LABEL, "method": CDP_WEB_VERSION}
        before = self._answers().count()
        asked_before = self._asked()
        text_before = self._last_answer_text()   # 긴 대화는 화면이 지난 턴을 치워(가상화) 말풍선 수가 안 늘 수 있다 — 글이 바뀌었는지도 본다
        copy_before = self._copy_button_count()   # 빈 출력 요구 질문의 복사 버튼 감지 기준
        try:
            with _CHROME_LOCK:
                if self.cancelled():
                    raise CdpWebError("중단 요청으로 질문을 보내지 않음")
                self._page_front()
                self._check_blocked()
                box = self.page.get_by_role("textbox", name=re.compile(re.escape(self.TEXTBOX_NAME))).first
                self._clear_composer(box)
                if files:
                    self._upload(files)
                self.page.keyboard.press("Escape")   # 남아 있는 메뉴·팝업 닫기
                self.page.wait_for_timeout(200)
                self._click_box(box)
                self.page.keyboard.insert_text(text)
                self.page.wait_for_timeout(600)
                self._wait_send_ready(300_000 if files else 30_000)
                self._press_send()
                # 질문이 올라갔는지 — 내 질문 말풍선이 늘거나, 답변 말풍선이 생기거나, 정지 버튼이 보이면 보낸 것.
                # 보내기 버튼을 아는 사이트(ChatGPT)만: 10초마다 입력칸에 글이 그대로면(전송이 무시됨) 다시 누른다 — 전체 2분 안에서.
                # 버튼을 모르는 사이트는 Enter 를 다시 누르지 않는다 — 감지가 늦을 때 같은 질문이 두 번 올라갈 수 있다(코드 리뷰)
                sent_ok = False
                send_deadline = time.monotonic() + 120
                tick = 0
                while time.monotonic() < send_deadline:
                    self.page.wait_for_timeout(500)
                    tick += 1
                    if self._asked() > asked_before or self._answers().count() > before or self._streaming():
                        sent_ok = True
                        break
                    if self.SEND_NAMES and tick % 20 == 0 and self._box_text(box):
                        left = int((send_deadline - time.monotonic()) * 1000)
                        if left > 1000:
                            self._wait_send_ready(min(left, 20_000))
                            self._press_send()
                if not sent_ok:
                    self._check_blocked()
                    raise CdpWebError("전송 뒤에도 답변이 시작되지 않음")
        except Exception as exc:
            return {**base, "answer": "", "status": "ERROR", "url": self.page.url,
                    "error": (str(exc)[:300] if isinstance(exc, CdpWebError) else f"질문을 보내지 못했습니다: {type(exc).__name__}"),
                    "latency_ms": round((time.monotonic() - started) * 1000), "debug_screenshot": self._snapshot("send-failed")}
        if self._asks_empty(question):   # 빈 출력 요구: 15초 안에 복사 버튼이 안 뜨면 바로 보류 (사용자 규칙 10-20 밤 — GPT 앱 '출력 없음' 109초 대기)
            held = self._hold_if_no_copy_button(before, copy_before, started, base)
            if held is not None:
                return held
        deadline = time.monotonic() + (self.answer_timeout_s or 10 ** 9)   # 0 = 제한 없음 — 답이 끝날 때까지 기다린다 (사용자 지시 10-20)
        last, stable = None, 0
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(1500)
            if self.cancelled():
                break
            if self._streaming():
                stable = 0
                continue
            current = self._last_answer_text()
            stable = stable + 1 if current == last else 0
            last = current
            if stable >= 2 and (self._answers().count() > before or (current and current != text_before)):
                break
            if stable >= 60:   # 정지 버튼 없이 90초째 아무 변화 없음 — 빈 답으로 끝난 것 (제한 시간을 없앤 뒤 무한 대기 사고 2026-10-20)
                break
        done = (stable >= 2 and (self._answers().count() > before or bool(last) and last != text_before)) or stable >= 60
        answer, capture = (last or ""), "text"
        if done:
            with _CHROME_LOCK:
                self._page_front()
                copied = self._copy_last_answer()
            if copied and copied.strip():
                answer, capture = copied, "copy-button"
            elif not answer.strip():
                # 답 말풍선은 생겼는데 글이 비었다 — '아무것도 출력하지 마' 를 지킨 빈 답이다 (Gemini Q44, 2026-10-15). 실패가 아니라 빈 답으로 남긴다
                answer, capture = "", "empty"
        if done:
            try:
                self._check_blocked()   # 한도 문구가 답 대신 왔는지
            except CdpWebError as exc:
                return {**base, "answer": "", "status": "ERROR", "error": str(exc)[:300], "url": self.page.url,
                        "latency_ms": round((time.monotonic() - started) * 1000)}
        return {**base, "answer": answer if done else "", "status": "SUCCESS" if done else "INCOMPLETE", "capture": capture, "url": self.page.url,
                "error": None if done else "답변이 제한 시간 안에 끝나지 않았습니다.",
                **({} if done else {"debug_screenshot": self._snapshot("incomplete")}),
                "latency_ms": round((time.monotonic() - started) * 1000)}


class ChatGptChat(_CdpChat):
    """chatgpt.com — 로그인(Plus) · Chat 모드 · 모델 GPT-6 · 추론 수준 Medium (사용자 결정 2026-10-15; 'GPT-6 Luna' 는 이 메뉴에 없음)."""
    NAME = "ChatGPT"
    HOST = "chatgpt.com"
    NEW_URL = "https://chatgpt.com/"
    MODEL_LABEL = "GPT-6 · Medium (웹)"
    MODEL_NAME = "GPT-6"
    LEVEL = "Medium"
    TEXTBOX_NAME = "ChatGPT"
    # 로그인 화면(2026-10-15): 답변 묶음 data-content-search-unit-key="…:assistant", 내 질문 data-user-message-bubble — 비로그인 화면 표식도 함께 둔다
    # 답 하나가 여러 덩어리(본문·'글쓰기' 카드 등)로 그려진다 — 덩어리가 아니라 턴 전체(data-turn-key: 내 질문 + 'ChatGPT 답변:' 제목 + 답)를 잡고
    # 글은 제목 뒤만 읽는다 (2026-10-15 Q32_T8: 마지막 덩어리인 카드만 복사돼 965자만 받음). 비로그인 화면 표식도 함께 둔다
    ANSWER_SELECTOR = ("[data-turn-key]:has(h4[data-conversation-role='assistant']), "
                       "[data-message-role='assistant'], [data-message-author-role='assistant']")
    ANSWER_HEADING = "ChatGPT 답변:"

    def _last_answer_text(self) -> str:
        text = super()._last_answer_text()
        return text.split(self.ANSWER_HEADING)[-1].strip() if self.ANSWER_HEADING in text else text
    USER_SELECTOR = "[data-user-message-bubble], [data-message-role='user'], [data-message-author-role='user']"
    COPY_NAMES = ("복사", "Copy")   # 답변 복사 버튼은 '복사', 내 질문 쪽은 '메시지 복사'

    def _copy_last_answer(self) -> Optional[str]:
        """답 전체 안의 '복사' 버튼을 모두 눌러 가장 긴 원문을 쓴다 — 카드 안 복사 버튼은 카드 내용만 복사한다.
        툴바가 늦게 그려지거나 화면 밖이면 못 누르므로 말풍선에 마우스를 올리고 3번까지 다시(간단평가 재수집에서 (text) 로 빠진 턴, 2026-10-20)."""
        for attempt in range(3):
            best = self._copy_last_answer_once()
            if best:
                return best
            try:
                self._answers().last.scroll_into_view_if_needed(timeout=2000)
                self._answers().last.hover(timeout=2000)
            except Exception:
                pass
            self.page.wait_for_timeout(1500)
        return None

    def _copy_last_answer_once(self) -> Optional[str]:
        last = self._answers().last
        best: Optional[str] = None
        try:
            buttons = last.get_by_role("button", name=re.compile(r"^(복사|Copy)$")).all()
            # 답 아래 도구 줄의 복사 아이콘만 — 코드블록(질문·답 속) 안의 '복사'와 내 질문 쪽 버튼은 뺀다 (사용자 지시 2026-10-21:
            # "코드블럭에도 복사버튼이 있고 아래에도 복사 아이콘이 있다, 아래에 있는 것을 눌러야 한다"). 예전에는 전부 눌러 가장 긴 글을 썼다.
            buttons = [b for b in buttons if b.evaluate(
                "(el) => !el.closest('[data-markdown-copy=\"code-block\"], pre') && !el.closest('[data-user-message-bubble]')")]
        except Exception:
            buttons = []
        for btn in buttons[-1:]:
            try:
                before = self._eval("() => (window.__mgCopied || []).length")
                try:
                    btn.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                try:
                    btn.click(timeout=3000)
                except Exception:
                    btn.dispatch_event("click")   # 상단 바 등이 포인터를 가로채면 클릭이 거부된다 — DOM 에 바로 클릭 이벤트 (짧은 답 복사 실패, 2026-10-20)
                self.page.wait_for_timeout(500)
                copied = self._eval("() => window.__mgCopied || []")
                if len(copied) > before and isinstance(copied[-1], str) and copied[-1].strip():
                    if best is None or len(copied[-1]) > len(best):
                        best = copied[-1]
            except Exception:
                continue
        visible = self._last_answer_text()
        if best and visible and len(best) < 0.6 * len(visible.strip()):   # 복사 원문이 화면 글보다 훨씬 짧으면 일부만 복사된 것
            return None
        if best:
            best = self._fix_code_blocks(best)
        return best or super()._copy_last_answer()

    CODE_WIDGET = "[data-code-block='true'], [data-markdown-copy='code-block']"
    CODE_COPY_NAMES = ("코드 복사", "Copy code")

    def _fix_code_blocks(self, text: str) -> str:
        """답 아래 복사 글(text)의 ``` 코드블록 안을, 같은 순서의 코드 위젯 '코드 복사' 글로 바꾼다 — chatgpt.com 의 답 복사는 코드 줄바꿈을 없앤다(10-23).
        위젯 수와 코드블록 수가 다르면 그대로 둔다. ``` 뒤 언어 표기는 복사 글 그대로(비어 있어도 안 붙임, 사용자 10-21)."""
        blocks = list(re.finditer(r"```([^\n]*)\n(.*?)```", text, flags=re.S))
        if not blocks:
            return text
        try:
            widgets = self._answers().last.locator(self.CODE_WIDGET)
            n = widgets.count()
        except Exception:
            return text
        if n != len(blocks):
            return text
        codes: List[str] = []
        for i in range(n):
            btn = widgets.nth(i).get_by_role("button", name=re.compile("|".join(re.escape(x) for x in self.CODE_COPY_NAMES))).first
            if not btn.count():
                return text
            try:
                before = self._eval("() => (window.__mgCopied || []).length")
                try:
                    btn.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                try:
                    btn.click(timeout=3000)
                except Exception:
                    btn.dispatch_event("click")
                self.page.wait_for_timeout(400)
                copied = self._eval("() => window.__mgCopied || []")
                if len(copied) <= before or not isinstance(copied[-1], str):
                    return text
                codes.append(copied[-1])
            except Exception:
                return text
        out, pos = [], 0
        for m, code in zip(blocks, codes):
            lang = m.group(1)
            out.append(text[pos:m.start()])
            out.append(f"```{lang}\n{code.rstrip(chr(10))}\n```")
            pos = m.end()
        out.append(text[pos:])
        return "".join(out)
    FILE_INPUT = "input[type=file][aria-label='파일 첨부']"
    STOP_NAMES = ("중지", "Stop")
    SEND_NAMES = ("보내기", "프롬프트 보내기")   # 입력칸 오른쪽 파란 버튼 — 큰 파일 처리 중에는 꺼져 있다
    REMOVE_FILE = re.compile(r"\.\w+ 제거$")      # 첨부 미리보기의 'OOO.txt 제거' 버튼
    MODEL_MENU = "ChatGPT 모델 선택"

    def _apply_model(self) -> str:
        chat = self.page.get_by_role("button", name="Chat", exact=True)
        if chat.count():
            try:
                chat.first.click(timeout=2000)
                self.page.wait_for_timeout(500)
            except Exception:
                pass
        self._pick_model_radio()
        power = self._pick_power(self.LEVEL)
        if power != self.LEVEL:
            raise CdpWebError(f"ChatGPT 추론 수준이 {self.LEVEL} 이 아닙니다({power}) — Chrome 창에서 {self.LEVEL} 으로 맞추세요")
        return f"{self.MODEL_NAME} · {power} (웹)"

    LEVELS = ("Instant", "Medium", "High", "Extra High", "Pro")   # 슬라이더 값 0~4 순서 (2026-10-23 chatgpt.com 확인)

    def _open_model_menu(self) -> None:
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(200)
        self.page.get_by_role("button", name=self.MODEL_MENU).first.click(timeout=5000)
        self.page.wait_for_timeout(900)

    def _close_menu(self) -> None:
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(400)

    def _pick_model_radio(self) -> None:
        """모델 메뉴(10-23 화면): 1단계에 추론 수준 항목(지금 값, 예 'High')과 모델 라디오(GPT-6 · GPT-5.6 Sol)가 함께 있다. 옛 화면('모델 선택' 하위 메뉴)도 본다."""
        self._open_model_menu()
        radio = self.page.locator("[role=menu] [role=menuitemradio]").filter(has_text=re.compile(rf"^{re.escape(self.MODEL_NAME)}$"))
        if radio.count():
            if radio.first.get_attribute("aria-checked") != "true":
                radio.first.click(timeout=3000)
                self.page.wait_for_timeout(600)
            self._close_menu()
            return
        self._close_menu()
        self._menu_pick("button", self.MODEL_MENU, ["모델 선택", self.MODEL_NAME], exact_last=True)

    def _shown_level(self) -> str:
        try:
            shown = " ".join(self.page.get_by_role("button", name=self.MODEL_MENU).first.inner_text(timeout=3000).split())
        except Exception:
            shown = ""
        return next((lv for lv in sorted(self.LEVELS, key=len, reverse=True) if lv in shown), "?")

    def _pick_power(self, want: str) -> str:
        """추론 수준 슬라이더: 메뉴를 열고 첫 항목(지금 수준)에 마우스를 올리면 role=slider 가 보인다 → 맨 왼쪽까지 내린 뒤 want 자리만큼 오른쪽으로.
        돌려주는 값은 메뉴를 닫은 뒤 버튼에 보이는 수준(열려 있을 때는 '추론 수준'이라고만 보인다)."""
        if self._shown_level() == want:
            return want
        idx = self.LEVELS.index(want) if want in self.LEVELS else 1
        for _attempt in range(2):
            try:
                self._open_model_menu()
                items = self.page.locator("[role=menu] [role=menuitem]")
                if items.count():
                    items.first.hover(timeout=3000)
                    self.page.wait_for_timeout(700)
                slider = self.page.locator("[role=slider]").first
                if not slider.count():
                    self._close_menu()
                    continue
                slider.focus()
                self.page.wait_for_timeout(200)
                for _ in range(len(self.LEVELS) + 1):
                    self.page.keyboard.press("ArrowLeft")
                    self.page.wait_for_timeout(120)
                for _ in range(idx):
                    self.page.keyboard.press("ArrowRight")
                    self.page.wait_for_timeout(150)
            except Exception:
                pass
            finally:
                self._close_menu()
            if self._shown_level() == want:
                break
        return self._shown_level()

    FILE_INPUT = "input[type=file][aria-label='파일 첨부']"
    STOP_NAMES = ("중지", "Stop")
    SEND_NAMES = ("보내기", "프롬프트 보내기")   # 입력칸 오른쪽 파란 버튼 — 큰 파일 처리 중에는 꺼져 있다
    REMOVE_FILE = re.compile(r"\.\w+ 제거$")      # 첨부 미리보기의 'OOO.txt 제거' 버튼
    MODEL_MENU = "ChatGPT 모델 선택"

    def _apply_model(self) -> str:
        chat = self.page.get_by_role("button", name="Chat", exact=True)
        if chat.count():
            try:
                chat.first.click(timeout=2000)
                self.page.wait_for_timeout(500)
            except Exception:
                pass
        self._pick_model_radio()
        power = self._pick_power(self.LEVEL)
        if power != self.LEVEL:
            raise CdpWebError(f"ChatGPT 추론 수준이 {self.LEVEL} 이 아닙니다({power}) — Chrome 창에서 {self.LEVEL} 으로 맞추세요")
        return f"{self.MODEL_NAME} · {power} (웹)"

    LEVELS = ("Light", "Medium", "High", "Extra High", "Max", "Ultra", "최소한", "없음")

    def _open_model_menu(self) -> None:
        self.page.keyboard.press("Escape")
        self.page.get_by_role("button", name=self.MODEL_MENU).first.click(timeout=5000)
        self.page.wait_for_timeout(700)

    def _pick_model_radio(self) -> None:
        """모델 메뉴(10-23 화면): 1단계에 추론 수준 항목(High 등)과 모델 라디오(GPT-6 · GPT-5.6 Sol)가 함께 있다. 옛 화면('모델 선택' 하위 메뉴)도 본다."""
        self._open_model_menu()
        radio = self.page.locator("[role=menu] [role=menuitemradio]").filter(has_text=re.compile(rf"^{re.escape(self.MODEL_NAME)}$"))
        if radio.count():
            if radio.first.get_attribute("aria-checked") != "true":
                radio.first.click(timeout=3000)
                self.page.wait_for_timeout(600)
            else:
                self.page.keyboard.press("Escape")
            return
        self.page.keyboard.press("Escape")
        self._menu_pick("button", self.MODEL_MENU, ["모델 선택", self.MODEL_NAME], exact_last=True)

    def _pick_power(self, want: str) -> str:
        """추론 수준: 메뉴 1단계의 수준 항목(지금 값, 예 'High')을 누르면 하위 메뉴에 수준 목록 → want 를 고른다. 돌려주는 값은 메뉴 버튼에 보이는 수준."""
        try:
            self._open_model_menu()
            level_item = self.page.locator("[role=menu] [role=menuitem]").filter(has_text=re.compile("|".join(re.escape(x) for x in self.LEVELS) + "|파워|추론")).first
            if level_item.count():
                level_item.click(timeout=3000)
                self.page.wait_for_timeout(800)
                target = self.page.locator("[role=menu] [role=menuitemradio], [role=menu] [role=menuitem], [role=listbox] [role=option]").filter(has_text=re.compile(rf"^{re.escape(want)}$"))
                if target.count():
                    target.first.click(timeout=3000)
                    self.page.wait_for_timeout(700)
        except Exception:
            pass
        finally:
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(300)
            self.page.keyboard.press("Escape")
        try:
            shown = " ".join(self.page.get_by_role("button", name=self.MODEL_MENU).first.inner_text(timeout=3000).split())
        except Exception:
            shown = ""
        return next((lv for lv in sorted(self.LEVELS, key=len, reverse=True) if lv in shown), "?")


class GeminiChat(_CdpChat):
    """gemini.google.com — 로그인(Plus) · 3.6 Flash · 사고 수준 보통 (사용자 결정 2026-10-18: 계정을 Plus 로 바꿔 메뉴가
    모델 직접 선택(3.5 Flash-Lite / 3.6 Flash / 3.1 Pro / 4 Argon) + 사고 수준(최저가·보통·최고가)으로 바뀜.
    이전 무료 계정 설정은 자동 모드 · 사고 수준 보통, 2026-10-15)."""
    NAME = "Gemini"
    HOST = "gemini.google.com"
    NEW_URL = "https://gemini.google.com/app"
    MODEL_LABEL = "Gemini 3.8 Flash · 사고 수준 보통 (웹)"
    MODEL_NAME = "3.8 Flash"   # 'Flash' 만 쓰면 '3.5 Flash-Lite' 가 먼저 걸린다 (메뉴 이름은 tools/modelgrade.APP_MODELS 와 같게, 10-23)
    LEVEL = "사고 보통"
    TEXTBOX_NAME = "Gemini 프롬프트 입력"
    ANSWER_SELECTOR = "model-response"
    USER_SELECTOR = "user-query"
    COPY_NAMES = ("대답 복사", "응답 복사", "복사", "Copy response")
    FILE_INPUT = ""
    STOP_NAMES = ("중지", "Stop")
    MODE_MENU = "모드 선택 도구 열기"

    def _apply_model(self) -> str:
        # 버튼에는 'Flash' 만 보여 버전·사고 수준을 확인할 수 없다 — 매번 메뉴에서 둘 다 고른다
        self._menu_pick("button", self.MODE_MENU, [self.MODEL_NAME])
        self._menu_pick("button", self.MODE_MENU, ["사고 수준", "보통"])
        btn = self.page.get_by_role("button", name=self.MODE_MENU).first
        if "Flash" not in f"{btn.inner_text(timeout=3000) or ''} {btn.get_attribute('aria-label') or ''}":
            raise CdpWebError("Gemini 모델 버튼에 'Flash' 가 보이지 않음")
        return self.MODEL_LABEL

    def _upload_via_menu(self, files: List[Path]) -> None:
        for f in files:
            with self.page.expect_file_chooser(timeout=10_000) as chooser:
                self.page.get_by_role("button", name="업로드 및 도구").first.click(timeout=5000)
                self.page.wait_for_timeout(800)
                item = self.page.get_by_role("menuitem", name=re.compile("파일|업로드"))
                if not item.count():   # 10-23 화면: '파일 · Drive · Google 포토' 둥근 버튼 — menuitem 이 아니라 글로 찾는다
                    item = self.page.locator("button, [role=button], [role=menuitem], [role=option]").filter(has_text=re.compile(r"^\s*파일\s*$|파일 업로드"))
                item.first.click(timeout=5000)
            chooser.value.set_files(str(f))
            self.page.wait_for_timeout(1500)

    def _copy_last_answer(self) -> Optional[str]:
        # 답변 복사 버튼은 마지막 model-response 안(또는 '옵션 더보기' 메뉴)에 있다 — 프롬프트 복사 버튼과 구분한다
        before = self._eval("() => (window.__mgCopied || []).length")
        last = self._answers().last
        try:
            btn = last.locator("[data-test-id='copy-button'], button[aria-label*='복사']").last
            if btn.count():
                btn.click(timeout=3000)
                self.page.wait_for_timeout(600)
                copied = self._eval("() => window.__mgCopied || []")
                if len(copied) > before and copied[-1].strip():
                    return copied[-1]
            more = last.get_by_role("button", name=re.compile("옵션 더보기|더보기"))
            if more.count():
                more.last.click(timeout=3000)
                self.page.wait_for_timeout(600)
                item = self.page.get_by_role("menuitem", name=re.compile("복사"))
                if item.count():
                    item.first.click(timeout=3000)
                    self.page.wait_for_timeout(600)
                    copied = self._eval("() => window.__mgCopied || []")
                    if len(copied) > before and copied[-1].strip():
                        return copied[-1]
                self.page.keyboard.press("Escape")
        except Exception:
            pass
        return None

    def _last_answer_text(self) -> str:
        """마지막 답변 본문. 짧은 거절 답('저는 텍스트 기반 AI입니다…')은 message-content 없이 그려져(2026-10-15 Q34) 답변 묶음 전체 글로 대신한다."""
        last = self._answers().last
        try:
            content = last.locator("message-content")
            if content.count():
                return content.first.inner_text(timeout=2000)
            return last.inner_text(timeout=2000)
        except Exception:
            return ""


class ClaudeChat(_CdpChat):
    """claude.ai — 로그인(유료) · 매 채팅 Sonnet 5.5 · 노력 중간 (사용자 결정 2026-10-15)."""
    NAME = "Claude"
    HOST = "claude.ai"
    NEW_URL = "https://claude.ai/new"
    MODEL_LABEL = "Claude Sonnet 5.5 · 노력 중간 (웹)"
    MODEL_NAME = "Sonnet 5.5"
    LEVEL = "노력 중간"
    TEXTBOX_NAME = "Claude에게 프롬프트"
    ANSWER_SELECTOR = "[data-testid='assistant-message']"
    USER_SELECTOR = "[data-testid='user-message']"
    COPY_NAMES = ("복사", "Copy")
    FILE_INPUT = "input[type=file][aria-label='파일 업로드']"
    STOP_NAMES = ("중지", "Stop")

    def _apply_model(self) -> str:
        opener = re.compile(r"^모델: ")
        current = " ".join(self.page.get_by_role("button", name=opener).first.inner_text(timeout=5000).split())
        if self.MODEL_NAME in current and "중간" in current:
            return f"{current} (웹)"
        if self.MODEL_NAME not in current:
            self._menu_pick("button", opener, [self.MODEL_NAME])
        self._menu_pick("button", opener, ["노력", "중간"])
        label = self.page.get_by_role("button", name=opener).first.inner_text(timeout=2000).strip().replace("\n", " ")
        return f"{label or self.MODEL_NAME + ' 중간'} (웹)"

    def _streaming(self) -> bool:
        if super()._streaming():
            return True
        last = self._answers().last
        try:
            return last.count() > 0 and last.get_attribute("data-is-streaming") == "true"
        except Exception:
            return False


CDP_CHATS = {"chatgpt_web": ChatGptChat, "gemini_web": GeminiChat, "claude_web": ClaudeChat}
CDP_PROVIDERS = tuple(CDP_CHATS)

