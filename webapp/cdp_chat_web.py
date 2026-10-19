"""ChatGPT·Gemini·Claude 웹 수집 — 수집용 Chrome(tools/chrome_cdp.py, 디버깅 포트 9222)에 붙어 각 사이트 탭에서 질문을 보내고 답을 받는다.

사용자 결정 2026-10-15: 세 모델 모두 로그인 상태의 웹 화면으로 수집(답변 수집은 API 대신 웹브라우저). 로그인·봇 확인은 사용자가 그 Chrome 창에서
직접 하고, 프로그램은 자격 증명을 다루지 않으며 봇 확인을 우회하지 않는다(막히면 고정 문구로 오류를 남긴다).
새 채팅마다 모델 설정을 맞춘다 — ChatGPT: Chat 모드·GPT-6 Luna·파워 Medium / Gemini: 3.6 Flash·사고 수준 보통(Plus 계정, 10-18) / Claude: Sonnet 5.5·노력 중간.

- 채팅 하나로 멀티턴(대화는 new_chat 뒤 같은 탭에서 턴 순서대로), 첨부는 파일 입력에 그대로, 긴 참조 자료(J7)는 TXT 파일로 올린다(AIONA 와 같음).
- 답변은 사이트의 복사 버튼 원문(클립보드 가로채기)으로 받고, 복사가 안 되면 답변 영역의 글을 쓴다(capture 에 표시).
- 화면 구조(2026-10-15 수집용 Chrome 에서 확인): 아래 각 클래스 상수. 바뀌면 상수만 고친다.
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

CDP_WEB_VERSION = "cdp-web-v3"
REFERENCE_FILE_CHARS = 20_000   # 이보다 긴 참조 자료는 입력칸 대신 TXT 파일로 올린다 (AIONA 와 같음)
#: 세 사이트가 같은 Chrome 을 쓰므로 메뉴 조작·전송은 한 번에 하나씩 — 탭 간 포커스 경쟁을 막는다
_CHROME_LOCK = threading.Lock()
#: 한도·봇 확인 문구 — 보이면 기다리지 않고 오류로 남긴다 (우회하지 않음)
_BLOCKED = ("보안 확인 수행 중", "Verify you are human", "사용자가 봇이 아닌지 확인", "Just a moment", "너무 많은 요청", "Too many requests",
            "사용량 한도", "usage limit", "한도에 도달", "메시지 한도", "limit reached")   # 'Cloudflare' 낱말은 채팅 제목에도 나와 쓰지 않는다


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
    TEXTBOX_NAME = ""         # 입력칸 textbox 의 접근성 이름(앞부분)
    ANSWER_SELECTOR = ""      # 답변 말풍선
    USER_SELECTOR = ""        # 내 질문 말풍선 — 전송 확인용
    COPY_NAMES: tuple = ()    # 답변 복사 버튼 이름 후보 (마지막 것을 누른다)
    FILE_INPUT = ""           # 숨은 파일 input (없으면 메뉴 → 파일 선택 창)
    STOP_NAMES: tuple = ()    # 답하는 동안 보이는 정지 버튼 이름
    SEND_NAMES: tuple = ()    # 보내기 버튼 이름 — 파일 처리 중에는 꺼져 있다 (켜질 때까지 기다린다)
    REMOVE_FILE = None        # 입력칸에 남은 첨부를 지우는 버튼 이름 패턴 (지난 질문이 실패해 남은 파일)

    def __init__(self, *, headless: bool = False, debug_dir: Optional[Path] = None, cancelled: Optional[Callable[[], bool]] = None,
                 answer_timeout_s: int = 600, place: Optional[str] = None) -> None:
        self.debug_dir, self.cancelled, self.answer_timeout_s, self.place = debug_dir, cancelled or (lambda: False), answer_timeout_s, place
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
            self.page.bring_to_front()   # 뒤에 있는 탭은 메뉴가 그려지지 않아 설정이 실패한다 (동시 실행 시험 2026-10-15)
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
        text = self._eval("() => document.body.innerText.slice(0, 4000)") or ""
        hit = next((p for p in _BLOCKED if p.lower() in text.lower()), None)
        if hit:
            self._snapshot("blocked")
            raise CdpWebError(f"{self.NAME} 화면이 막혀 있습니다(한도 또는 봇 확인: '{hit}') — Chrome 창에서 직접 확인하세요.")

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
                    btns.last.scroll_into_view_if_needed(timeout=2000)
                    btns.last.click(timeout=3000)
                    self.page.wait_for_timeout(600)
                    copied = self._eval("() => window.__mgCopied || []")
                    if len(copied) > before and isinstance(copied[-1], str) and copied[-1].strip():
                        return copied[-1]
                except Exception:
                    continue
        return None

    def recopy(self) -> str:
        """마지막 답의 복사 버튼을 한 번 더 — 빈 답이 왔을 때 재시도 (사용자 규칙 10-20)."""
        try:
            with _CHROME_LOCK:
                self.page.bring_to_front()
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
        try:
            with _CHROME_LOCK:
                if self.cancelled():
                    raise CdpWebError("중단 요청으로 질문을 보내지 않음")
                self.page.bring_to_front()
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
        done = stable >= 2 and (self._answers().count() > before or bool(last) and last != text_before)
        answer, capture = (last or ""), "text"
        if done:
            with _CHROME_LOCK:
                self.page.bring_to_front()
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
        except Exception:
            buttons = []
        for btn in buttons:
            try:
                before = self._eval("() => (window.__mgCopied || []).length")
                btn.scroll_into_view_if_needed(timeout=2000)
                btn.click(timeout=3000)
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
        return best or super()._copy_last_answer()
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
        self._menu_pick("button", self.MODEL_MENU, ["모델 선택", self.MODEL_NAME], exact_last=True)
        # 추론 수준(파워)은 슬라이더 — 표시 값만 읽어 확인한다 (기본 Medium, 사용자 결정)
        power = "?"
        try:
            self.page.get_by_role("button", name=self.MODEL_MENU).first.click(timeout=5000)
            self.page.wait_for_timeout(700)
            self.page.get_by_role("menuitem", name="파워").first.click(timeout=5000)
            self.page.wait_for_timeout(900)
            text = self._eval("() => document.body.innerText")
            power = next((lv for lv in ("Medium", "High", "Light", "Max", "Ultra", "Extra High", "최소한", "없음") if lv in text), "?")
        except Exception:
            pass
        finally:
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(300)
            self.page.keyboard.press("Escape")
        if power != "Medium":
            raise CdpWebError(f"ChatGPT 추론 수준이 Medium 이 아닙니다({power}) — Chrome 창에서 Medium 으로 맞추세요")
        return f"{self.MODEL_NAME} · {power} (웹)"


class GeminiChat(_CdpChat):
    """gemini.google.com — 로그인(Plus) · 3.6 Flash · 사고 수준 보통 (사용자 결정 2026-10-18: 계정을 Plus 로 바꿔 메뉴가
    모델 직접 선택(3.5 Flash-Lite / 3.6 Flash / 3.1 Pro / 4 Argon) + 사고 수준(최저가·보통·최고가)으로 바뀜.
    이전 무료 계정 설정은 자동 모드 · 사고 수준 보통, 2026-10-15)."""
    NAME = "Gemini"
    HOST = "gemini.google.com"
    NEW_URL = "https://gemini.google.com/app"
    MODEL_LABEL = "Gemini 3.6 Flash · 사고 수준 보통 (웹)"
    MODEL_NAME = "3.6 Flash"   # 'Flash' 만 쓰면 '3.5 Flash-Lite' 가 먼저 걸린다
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
                self.page.wait_for_timeout(600)
                item = self.page.get_by_role("menuitem", name=re.compile("파일|업로드"))
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
    TEXTBOX_NAME = "Claude에게 프롬프트"
    ANSWER_SELECTOR = "[data-testid='assistant-message']"
    USER_SELECTOR = "[data-testid='user-message']"
    COPY_NAMES = ("복사", "Copy")
    FILE_INPUT = "input[type=file][aria-label='파일 업로드']"
    STOP_NAMES = ("중지", "Stop")

    def _apply_model(self) -> str:
        opener = re.compile(r"^모델: ")
        current = " ".join(self.page.get_by_role("button", name=opener).first.inner_text(timeout=5000).split())
        if "Sonnet 5.5" in current and "중간" in current:
            return f"{current} (웹)"
        if "Sonnet 5.5" not in current:
            self._menu_pick("button", opener, ["Sonnet 5.5"])
        self._menu_pick("button", opener, ["노력", "중간"])
        label = self.page.get_by_role("button", name=opener).first.inner_text(timeout=2000).strip().replace("\n", " ")
        return f"{label or 'Sonnet 5.5 중간'} (웹)"

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


class ChatGptAppChat(ChatGptChat):
    """ChatGPT 데스크톱 앱(OpenAI.Codex 패키지, Electron) — tools/chatgpt_app_cdp.py 로 포트를 열어 띄운 앱의 메인 페이지(app://-/index.html)에 붙는다.
    ChatGPT 모드 화면은 chatgpt.com 웹과 같은 구조라(2026-10-19 확인) 웹 수집기를 그대로 쓰고, 다른 점만 바꾼다:
    붙는 포트·페이지, 새 채팅은 주소 이동 대신 '새 채팅' 버튼, 모드가 Codex 면 ChatGPT 로 바꿈(보이는 '모드 전환' 버튼 → '만들고, 배우고, 탐색하세요'),
    모델 메뉴는 하위 메뉴 없이 'GPT-6' 바로, 추론 수준은 입력칸 옆 표시(Medium)로 확인. 사용자 결정 10-19: 모델 GPT-6 · 추론 Medium 유지."""
    NAME = "ChatGPT앱"
    HOST = "app://-/index.html"
    NEW_URL = ""
    MODEL_LABEL = "GPT-6 · Medium (앱)"
    TEXTBOX_NAME = "ChatGPT에게 물어보세요"
    MODE_ITEM_CHATGPT = "만들고, 배우고, 탐색하세요"
    METHOD = "chatgpt-app-v1"

    def __enter__(self) -> "ChatGptAppChat":
        from tools.chatgpt_app_cdp import cdp_url, launch, status
        if not status()["ok"]:
            try:
                launch(restart=True)   # 포트 없이 떠 있으면 닫고 포트를 열어 다시 켠다, 안 떠 있으면 켠다 (로그인은 앱에 남아 있다)
            except Exception as exc:
                raise CdpWebError(f"ChatGPT 앱을 켜지 못했습니다: {str(exc)[:120]}")
        if not status()["ok"]:
            raise CdpWebError("ChatGPT 앱이 디버깅 포트로 떠 있지 않습니다 — python tools/chatgpt_app_cdp.py --restart 로 띄우고 앱에서 로그인하세요.")
        from tools.chatgpt_app_cdp import show_window
        show_window()   # 트레이에만 떠 있으면 창을 보이게 (사용자가 수집 과정을 본다)
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(cdp_url())
            context = self._browser.contexts[0]
            self.page = next((pg for pg in context.pages if pg.url == self.HOST and not pg.is_closed()), None)
            if self.page is None:
                raise CdpWebError("ChatGPT 앱 메인 페이지(app://-/index.html)를 찾지 못했습니다.")
            self.page.add_init_script(COPY_CAPTURE_JS)
            self.page.set_default_timeout(60_000)
            if self.place:
                self._place_window()
        except BaseException:
            self._close()
            raise
        return self

    def _place_window(self) -> None:
        """동시 수집: 앱 창을 화면 반쪽에 놓고, 그동안은 창을 앞으로 가져오지 않는다(Claude 앱 창의 키 입력을 뺏지 않게). 페이지는 하나라 탭 전환이 필요 없다."""
        try:
            from tools.chatgpt_app_cdp import show_window
            from webapp.uia_chat_app import place_window
            hwnd = show_window()
            if hwnd:
                place_window(hwnd, self.place)
            self.page.bring_to_front = lambda: None
        except Exception as exc:
            self._snapshot(f"place-failed-{type(exc).__name__}")

    # --- 모드 ---
    def _mode_button(self) -> Optional[Dict[str, Any]]:
        """보이는 '모드 전환' 버튼(같은 이름의 숨은 버튼이 하나 더 있다) — {x, y, mode}."""
        return self.page.evaluate("""() => { const bs=[...document.querySelectorAll('button')].filter(b => (b.getAttribute('aria-label')||'').startsWith('모드 전환'))
            .map(b => ({r: b.getBoundingClientRect(), aria: b.getAttribute('aria-label')})).filter(o => o.r.width > 0);
            return bs.length ? {x: bs[0].r.x + bs[0].r.width - 8, y: bs[0].r.y + bs[0].r.height / 2, mode: bs[0].aria.replace('모드 전환, 현재 모드: ', '')} : null; }""")

    def ensure_chatgpt_mode(self) -> str:
        btn = self._mode_button()
        if btn is None:
            raise CdpWebError("ChatGPT 앱의 모드 전환 버튼을 찾지 못했습니다 — 앱 창이 너무 작거나 화면이 다릅니다.")
        if btn["mode"] == "ChatGPT":
            return "ChatGPT"
        self.page.mouse.click(btn["x"], btn["y"])
        self.page.wait_for_timeout(800)
        item = self.page.evaluate("""(h) => { const e=[...document.querySelectorAll('*')].find(e => e.children.length===0 && (e.textContent||'').includes(h));
            if (!e) return null; const r=e.getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2}; }""", self.MODE_ITEM_CHATGPT)
        if item is None:
            self.page.keyboard.press("Escape")
            raise CdpWebError("모드 메뉴에서 'ChatGPT' 항목을 찾지 못했습니다.")
        self.page.mouse.click(item["x"], item["y"])
        self.page.wait_for_timeout(2500)
        btn = self._mode_button()
        if not btn or btn["mode"] != "ChatGPT":
            raise CdpWebError("ChatGPT 모드로 바꾸지 못했습니다 — 앱 왼쪽 위에서 직접 ChatGPT 로 바꾸세요.")
        return "ChatGPT"

    # --- 새 채팅 · 모델 ---
    def new_chat(self) -> None:
        with _CHROME_LOCK:
            self.page.bring_to_front()
            self.ensure_chatgpt_mode()
            btn = self.page.get_by_role("button", name="새 채팅", exact=True)
            clicked = False
            for i in range(btn.count()):
                try:
                    if btn.nth(i).is_visible():
                        btn.nth(i).click(timeout=5000)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                raise CdpWebError("ChatGPT 앱에서 '새 채팅' 버튼을 찾지 못했습니다.")
            self.page.wait_for_timeout(2000)
            self.page.evaluate(COPY_CAPTURE_JS)
            self._check_blocked()
            self.page.get_by_role("textbox", name=re.compile(re.escape(self.TEXTBOX_NAME))).first.wait_for(state="visible", timeout=20_000)
            self.page.wait_for_timeout(800)
            try:
                self.model_seen = self._apply_model()
            except Exception as exc:
                self.model_seen = f"{self.MODEL_LABEL} (설정 확인 못 함: {type(exc).__name__}: {str(exc)[:80]})"
                self._snapshot("model-settings-failed")

    def _apply_model(self) -> str:
        """앱의 모델 메뉴('ChatGPT 모델 선택')를 열면 모델 라디오(GPT-6 · GPT-5.6 Sol)와 '파워' 슬라이더가 바로 보인다.
        Playwright 역할 검색이 이 메뉴를 못 보므로 DOM 을 직접 읽는다(2026-10-19). GPT-6 가 선택돼 있으면 두고, 아니면 누른다.
        추론 수준은 메뉴 글의 단계 표시(Medium)로 확인 — 사용자 결정 10-19: GPT-6 · Medium 유지."""
        FIND = """(name) => { const rs=[...document.querySelectorAll('[role=menuitemradio]')].filter(e => e.getBoundingClientRect().width > 0)
            .map(e => ({t: (e.innerText||'').trim().split('\\n')[0], checked: (e.getAttribute('aria-checked')||'') === 'true'}));
            const hit = rs.findIndex(r => r.t === name || r.t.startsWith(name + ' '));
            return {all: rs.map(r => r.t), hit, checked: hit >= 0 ? rs[hit].checked : null}; }"""
        CLICK = """(name) => { const e=[...document.querySelectorAll('[role=menuitemradio]')].filter(e => e.getBoundingClientRect().width > 0)
            .find(e => { const t=(e.innerText||'').trim().split('\\n')[0]; return t === name || t.startsWith(name + ' '); }); if (!e) return false; e.click(); return true; }"""
        opener = self.page.get_by_role("button", name=self.MODEL_MENU).first
        opener.wait_for(state="visible", timeout=15_000)
        power = "?"
        try:
            opener.click(timeout=5000)
            self.page.wait_for_timeout(900)
            found = self.page.evaluate(FIND, self.MODEL_NAME)
            if found["hit"] < 0:
                raise CdpWebError(f"{self.NAME} 모델 메뉴에 '{self.MODEL_NAME}' 이 없습니다 (항목: {found['all']})")
            if not found["checked"]:
                self.page.evaluate(CLICK, self.MODEL_NAME)
                self.page.wait_for_timeout(800)
                opener.click(timeout=5000)
                self.page.wait_for_timeout(900)
                found = self.page.evaluate(FIND, self.MODEL_NAME)
                if not found["checked"]:
                    raise CdpWebError(f"{self.NAME} 모델을 {self.MODEL_NAME} 로 바꾸지 못했습니다")
            text = self._eval("() => document.body.innerText")
            power = next((lv for lv in ("Extra High", "Medium", "High", "Light", "Max", "Ultra", "최소한", "없음") if lv in text), "?")
        finally:
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(300)
        if power != "Medium":
            raise CdpWebError(f"ChatGPT 추론 수준이 Medium 이 아닙니다({power}) — 앱 입력칸 옆에서 Medium 으로 맞추세요")
        return f"{self.MODEL_NAME} · {power} (앱)"

    IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

    def _attached_count(self) -> int:
        """입력칸에 붙은 첨부 수 — 'OOO.ext 제거' 버튼 수(이미지는 이름이 글로 안 보여 이 버튼으로 센다)."""
        return self._eval("""() => [...document.querySelectorAll('button')].filter(b => /\\.\\w+ 제거$/.test((b.getAttribute('aria-label')||b.innerText||'').trim())).length""")

    def _upload(self, files: List[Path]) -> None:
        """앱 화면에는 지난 채팅의 첨부 input 이 여러 개 남아 있고 입력칸에 연결된 것은 마지막 것(2026-10-19 확인: 15개 중 마지막만 붙음).
        이미지는 '사진 첨부'(image/*) input, 그 밖은 '파일 첨부' input — 마지막 것부터 거꾸로 넣어 보고 '제거' 버튼이 늘면 성공."""
        images = [f for f in files if f.suffix.lower() in self.IMAGE_EXT]
        others = [f for f in files if f.suffix.lower() not in self.IMAGE_EXT]
        before = self._attached_count()
        want = before
        for batch, labels in ((others, ("파일 첨부",)), (images, ("사진 첨부", "사진 또는 동영상 첨부", "파일 첨부"))):
            if not batch:
                continue
            want += len(batch)
            done = False
            for label in labels:
                inputs = self.page.locator(f"input[type=file][aria-label='{label}']")
                for i in range(inputs.count() - 1, -1, -1):
                    try:
                        inputs.nth(i).set_input_files([str(f) for f in batch])
                    except Exception:
                        continue
                    for _ in range(12):   # 미리보기가 붙을 때까지 (큰 파일은 더)
                        self.page.wait_for_timeout(1000)
                        if self._attached_count() >= want:
                            break
                    if self._attached_count() >= want:
                        done = True
                        break
                if done:
                    break
            if not done:
                raise CdpWebError(f"{self.NAME} 첨부를 입력칸에 붙이지 못했습니다 ({', '.join(f.name for f in batch)})")
        self.page.wait_for_timeout(1500)

    def ask(self, question: str, files: Optional[List[Path]] = None, reference: Any = None) -> Dict[str, Any]:
        out = super().ask(question, files, reference)
        out["method"] = self.METHOD
        return out
