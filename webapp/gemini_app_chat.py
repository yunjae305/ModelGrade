"""Gemini 데스크톱 앱 수집기 — Windows UI 자동화(UIA)로 앱 채팅 화면을 사람처럼 조작한다 (gemini-app-uia-v1, 2026-10-21: 사용자가 Gemini 를 웹에서 앱으로 바꿈).

앱 채팅은 gemini.google.com 을 그대로 띄운 것(Electron)이라 요소 이름이 웹 GeminiChat 과 같다. 조작 순서(한국어 UI, 2026-10-21 확인):
  '새 채팅'(Hyperlink) → 모드 버튼('모드 선택 도구 열기, 현재 Flash 보통 모드 사용 중') → 메뉴 '3.8 Flash' · '사고 수준' → '보통'
  → 첨부: 파일을 클립보드에 올리고(탐색기의 복사와 같음) 입력칸에 Ctrl+V → 칩 '파일 업로드됨'(지우기는 칩에 마우스를 올리면 뜨는 'close <이름>')
  → 입력칸(Edit 'Gemini 프롬프트 입력')에 값 설정 → '메시지 보내기'
  → 답 묶음(Group, class 'response-container …')이 늘고 글이 멈추면 그 묶음의 '복사'(대답 복사) → 클립보드 원문.
  → 코드블록: 같은 묶음 안 '코드 복사' 버튼을 차례로 눌러 받은 코드로 답 원문의 ``` 안을 바꾼다(tools/codeblock_fix.merge_codes — 공백·줄바꿈만 다를 때만).
    Gemini 는 대답 복사 글에 코드 줄바꿈·언어가 그대로 들어오므로 보통 바뀌는 것이 없지만, 네 모델을 같은 방식으로 맞춘다(사용자 10-21 밤).
수집기 인터페이스는 webapp/uia_chat_app.ClaudeAppChat 과 같다(new_chat · ask → {answer, status, …}) — tools/modelgrade.py collect app 이 그대로 쓴다.
메시지를 보내는 것 외에 설정을 바꾸지 않는다(모델·사고 수준은 매 채팅 확인). 시스템 클립보드를 쓰므로 도는 동안 다른 복사 작업을 하지 않는다.
"""
from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from webapp.cdp_chat_web import REFERENCE_FILE_CHARS
from webapp.input_turn import input_turn
from webapp.uia_chat_app import (ClaudeAppChat, CopyFailed, UiaChatError, _clipboard_get, _clipboard_set, _clipboard_set_files, _matches_screen,
                                 _squash, place_window)


class GeminiAppChat:
    NAME = "Gemini앱"
    MODEL_LABEL = "Gemini 3.8 Flash · 사고 수준 보통 (앱)"
    METHOD = "gemini-app-uia-v1"
    NEW_CHAT = r"^새 채팅$"                      # 사이드바 Hyperlink
    SIDEBAR_OPEN = r"^(기본 메뉴|Main menu)$"            # 좁은 창에서 닫힌 사이드바를 여는 버튼 — '새 채팅' 은 사이드바 안에만 있다
    SIDEBAR_CLOSE = r"^(사이드바 닫기|Close sidebar)$"   # 좁은 창(동시 수집 1/4 자리)에서는 사이드바가 채팅을 덮어 입력칸이 없다(10-21 밤)
    TEXTBOX = r"^Gemini 프롬프트 입력"
    SEND = r"^메시지 보내기"
    STOP = r"^(대답 생성 중지|응답 중지|생성 중지|중지|Stop)"   # 생성 중에는 보내기 자리에 '대답 생성 중지'(2026-10-21 확인)
    THINKING = r"(생각하는 중|생각 중|Thinking|답변을 생성)"   # 생성 중 화면 글
    MODE_BTN = r"^모드 선택 도구 열기"
    MODEL_ITEM = r"3\.8 Flash"                  # 메뉴 항목 '선택됨 3.8 Flash 무엇이든 도움을 받으세요' — 'Flash' 만 쓰면 '3.5 Flash-Lite' 가 먼저 걸린다
    LEVEL_ITEM = r"^사고 수준"
    LEVEL_PICK = r"보통"                         # 하위 '낮음 · 보통 · 높음'
    SELECTED = r"^선택됨"
    MODE_OK = r"Flash\s*보통"                    # 모드 버튼 이름에 보여야 하는 설정
    COPY = r"^(복사|대답 복사|응답 복사|Copy response)$"
    CODE_COPY = r"^(코드 복사|Copy code)$"
    CHIP_CLASS = "gem-attachment-tile"          # 첨부 칩 하나 = 이 class 의 Group 하나 (2026-10-21 밤 확인: docx · pdf · txt · png)
    CHIP_CLOSE = r"^(첨부파일 닫기|close .+)$"    # 칩 안 지우기 버튼 — 그림 '첨부파일 닫기', 문서 'close <파일 이름(확장자 뺌)>'(마우스를 올려야 생기기도 함)
    IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    RESPONSE_CLASS = "response-container"        # 답 하나를 감싸는 Group 의 class 머리
    CODE_BLOCK_CLASS = "code-block"
    COPY_SENTINEL = "__gemini_app_copy_sentinel__"
    EMPTY_OUTPUT_WAIT_S = 15   # 빈 출력 요구: 이 안에 답 묶음이 안 생기면 보류(COPY_FAILED) (사용자 규칙 10-20 밤)

    def __init__(self, *, headless: bool = False, debug_dir: Optional[Path] = None, cancelled: Optional[Callable[[], bool]] = None,
                 answer_timeout_s: int = 600, place: Optional[str] = None, model: Optional[str] = None) -> None:
        self.debug_dir, self.cancelled, self.answer_timeout_s, self.place = debug_dir, cancelled or (lambda: False), answer_timeout_s, place
        # 시작할 때 고른 모델 (10-22) — 메뉴 항목은 '3.8 Flash 무엇이든 …' 처럼 번호까지, 모드 버튼은 '현재 Flash 보통 모드' 처럼 이름만
        self.model_name = model or "3.8 Flash"
        self.MODEL_ITEM = re.escape(self.model_name)
        self.MODE_OK = re.escape(self.model_name.split(" ", 1)[-1]) + r"\s*보통"
        self.MODEL_LABEL = f"Gemini {self.model_name} · 사고 수준 보통 (앱)"
        self.win: Any = None
        self._temp = tempfile.TemporaryDirectory(prefix="gemini-app-ref-")
        self.model_seen: Optional[str] = None

    # --- 연결 ---------------------------------------------------------------------------------------
    def __enter__(self) -> "GeminiAppChat":
        from tools import gemini_app
        s = gemini_app.relaunch()   # 없으면 켜고, '가려져도 그리기' 옵션 없이 켜져 있으면 닫고 옵션을 붙여 다시 켠다 (10-21 밤)
        if s.get("ok") and not s.get("flags_ok"):
            print("   (Gemini 앱이 '가려져도 그리기' 옵션 없이 켜져 있음 — 다른 창에 가려지면 답이 늦게 보일 수 있음)", flush=True)
        if not s["ok"]:
            raise UiaChatError(f"Gemini 앱을 켜지 못했습니다 — 앱을 직접 켜고 로그인한 뒤 다시 시도하세요. {s.get('error') or ''}")
        self.win = gemini_app.main_window(s["pids"])
        if self.win is None:
            raise UiaChatError("Gemini 앱 창을 찾지 못했습니다.")
        if self.place:
            place_window(self.win, self.place)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self._temp.cleanup()
        except Exception:
            pass

    # --- UIA 도우미 ---------------------------------------------------------------------------------
    def _all(self, kind: Optional[str] = None) -> List[Any]:
        return self.win.descendants(control_type=kind) if kind else self.win.descendants()

    def _find(self, kind: str, pattern: str, *, last: bool = False) -> Any:
        hits = [e for e in self._all(kind) if re.match(pattern, e.element_info.name or "")]
        if not hits:
            return None
        return hits[-1] if last else hits[0]

    def _wait_for(self, kind: str, pattern: str, timeout_s: float, *, last: bool = False) -> Any:
        deadline = time.monotonic() + timeout_s
        while True:
            e = self._find(kind, pattern, last=last)
            if e is not None or time.monotonic() >= deadline:
                return e
            time.sleep(0.5)

    _press = staticmethod(ClaudeAppChat._press)

    def _front(self) -> None:
        try:
            self.win.set_focus()
        except Exception:
            pass

    def _is_foreground(self) -> bool:
        try:
            import win32gui
            return win32gui.GetForegroundWindow() == self.win.handle
        except Exception:
            return True

    def _ensure_front(self) -> None:
        """키 · 붙여넣기 · 클릭 바로 전 — 이 창이 맨 앞이 아니면 키를 보내지 않는다(다른 창, 특히 사용자 창에 들어간다, 10-21 밤)."""
        for _ in range(5):
            self._front()
            time.sleep(0.2)
            if self._is_foreground():
                return
            time.sleep(0.7)
        raise UiaChatError(f"{self.NAME} 창을 맨 앞으로 가져오지 못해 키를 보내지 않았습니다(다른 창이 앞에 있음).")

    def _unminimize(self) -> None:
        """최소화된 창은 화면을 그리지 않아 답이 끝난 것을 못 본다(10-21 밤: 2시간 멈춤) — 포커스는 뺏지 않고 창만 되살린다."""
        try:
            import win32gui
            if win32gui.IsIconic(self.win.handle):
                win32gui.ShowWindow(self.win.handle, 4)   # SW_SHOWNOACTIVATE
                print("   (Gemini 창이 최소화되어 있어 되살림)", flush=True)
        except Exception:
            pass

    def _escape(self) -> None:
        """열린 메뉴 닫기 — 이 창이 앞이면 Esc."""
        if not self._all("MenuItem"):
            return
        import win32api
        with input_turn():
            if not self._is_foreground():   # Esc 는 맨 앞 창으로 간다 — 이 창이 아니면 보내지 않는다(10-21 밤)
                return
            for _ in range(2):
                win32api.keybd_event(0x1B, 0, 0, 0)
                win32api.keybd_event(0x1B, 0, 2, 0)
                time.sleep(0.4)

    def _hover(self, elem: Any, dy: int = 0) -> None:
        """마우스를 요소 위로 — 화면이 잠겨 있으면 넘어간다(그 경우 UIA 로 누르는 것은 된다)."""
        try:
            from pywinauto import mouse
            r = elem.element_info.rectangle
            mouse.move(coords=((r.left + r.right) // 2, (r.top + r.bottom) // 2 + dy))
            time.sleep(0.6)
        except Exception:
            pass

    def _snapshot(self, tag: str) -> Optional[str]:
        if not self.debug_dir:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.debug_dir / f"{self.NAME}-{tag}-{int(time.time())}.png"
            self.win.capture_as_image().save(str(path))
            return str(path)
        except Exception:
            return None

    # --- 채팅 · 모델 --------------------------------------------------------------------------------
    def new_chat(self) -> None:
        self._front()
        self._escape()
        link = self._wait_for("Hyperlink", self.NEW_CHAT, 3)
        if link is None:   # 좁은 창: 사이드바가 닫혀 있으면 열고 찾는다
            opener = self._find("Button", self.SIDEBAR_OPEN)
            if opener is not None:
                self._press(opener)
                time.sleep(1.0)
            link = self._wait_for("Hyperlink", self.NEW_CHAT, 10)
        if link is None:
            raise UiaChatError("'새 채팅' 을 찾지 못했습니다.")
        self._press(link)
        time.sleep(2.0)
        self._close_sidebar()
        self._clear_leftover_attachments()
        self.model_seen = self._apply_model()

    def _menu_items(self, want: str = "", timeout_s: float = 5.0) -> List[Any]:
        """열린 메뉴의 항목 — 메뉴가 늦게 그려지므로 want 가 보일 때까지(최대 timeout_s) 기다린다."""
        deadline = time.monotonic() + timeout_s
        while True:
            items = self._all("MenuItem")
            if (items and (not want or any(re.search(want, m.element_info.name or "") for m in items))) or time.monotonic() >= deadline:
                return items
            time.sleep(0.4)

    def _apply_model(self) -> str:
        """매 채팅 모델 '3.8 Flash' · 사고 수준 '보통' 확인 — 아니면 고른다. 버튼 이름('현재 Flash 보통 모드')으로 마지막 확인."""
        btn = self._wait_for("Button", self.MODE_BTN, 10)
        if btn is None:
            raise UiaChatError("모드 선택 버튼을 찾지 못했습니다.")
        for want, path in ((self.MODEL_ITEM, [self.MODEL_ITEM]), (self.LEVEL_PICK, [self.LEVEL_ITEM, self.LEVEL_PICK])):
            for _attempt in range(3):   # 첫 누름에 메뉴가 안 열리는 때가 있다(2026-10-21) — 안 열리면 닫고 다시 누른다
                if any(re.search(path[0], m.element_info.name or "") for m in self._all("MenuItem")):
                    break
                self._front()
                self._press(self._find("Button", self.MODE_BTN) or btn)
                if any(re.search(path[0], m.element_info.name or "") for m in self._menu_items(path[0], 4.0)):
                    break
                self._escape()
            item = None
            for step in path:
                item = next((m for m in self._menu_items(step) if re.search(step, m.element_info.name or "")), None)
                if item is None:
                    break
                if step is not path[-1]:
                    try:
                        item.expand()
                    except Exception:
                        self._press(item)
                    time.sleep(0.8)
            if item is None:
                self._escape()
                raise UiaChatError(f"모드 메뉴에서 '{want}' 을 찾지 못했습니다.")
            if re.match(self.SELECTED, item.element_info.name or ""):
                self._escape()   # 이미 고른 설정
            else:
                self._press(item)
                time.sleep(1.0)
                self._escape()
            btn = self._wait_for("Button", self.MODE_BTN, 10) or btn
        name = btn.element_info.name or ""
        if not re.search(self.MODE_OK, name):
            raise UiaChatError(f"Gemini 모드 버튼이 '{name}' — '{self.model_name.split(' ', 1)[-1]} 보통' 이 아님")
        return self.MODEL_LABEL

    # --- 입력칸 · 첨부 -------------------------------------------------------------------------------
    def _close_sidebar(self) -> bool:
        """사이드바가 채팅 위를 덮고 있으면(좁은 창) '사이드바 닫기' 를 누른다 — 넓은 창에는 이 버튼이 없어 그대로."""
        btn = self._find("Button", self.SIDEBAR_CLOSE)
        if btn is None:
            return False
        self._press(btn)
        time.sleep(1.0)
        return True

    def _textbox(self) -> Any:
        box = self._wait_for("Edit", self.TEXTBOX, 3)
        if box is None and self._close_sidebar():
            box = self._wait_for("Edit", self.TEXTBOX, 10)
        if box is None:
            raise UiaChatError("입력칸을 찾지 못했습니다.")
        return box

    PLACEHOLDER = r"^(Gemini에게 물어보기|Ask Gemini)\s*$"   # 빈 입력칸은 안내 글을 값으로 돌려준다 (2026-10-21)

    def _box_text(self) -> str:
        box = self._find("Edit", self.TEXTBOX)
        try:
            value = (box.get_value() or "").strip() if box is not None else ""
        except Exception:
            return ""
        return "" if re.match(self.PLACEHOLDER, value) else value

    def _set_text(self, text: str) -> None:
        """입력칸에 글 — UIA 값 설정(2026-10-21 확인). 안 들어가면 클립보드 붙여넣기."""
        box = self._textbox()
        try:
            box.iface_value.SetValue(text)
            time.sleep(0.6)
        except Exception:
            pass
        if self._box_text()[:40] != text.strip()[:40]:
            from pywinauto.keyboard import send_keys
            with input_turn():   # 클립보드에 넣고 붙일 때까지 다른 수집기가 클립보드 · 창을 건드리지 못하게
                _clipboard_set(text)
                self._ensure_front()
                box.set_focus()
                if not self._is_foreground():
                    raise UiaChatError("입력칸에 포커스를 주는 사이 다른 창이 앞으로 와서 붙여넣지 않았습니다.")
                send_keys("^a{DEL}^v", pause=0.05)
                time.sleep(0.8)
        if self._box_text()[:40] != text.strip()[:40]:
            raise UiaChatError("입력칸에 질문이 들어가지 않았습니다.")

    def _chips(self) -> List[Any]:
        """입력칸의 첨부 칩 — '파일 업로드됨' 글은 화면 낭독기 알림이라 지운 뒤 '파일 삭제됨' 으로 남는 등 셀 수 없고,
        그림(png) 칩에는 그 글조차 없다(10-21 밤 Q18: 그림을 못 세어 남은 채 Q20 과 함께 보내짐) — 칩 상자로 센다."""
        return [g for g in self._all("Group") if self.CHIP_CLASS in (g.element_info.class_name or "")]

    def _attached_count(self) -> int:
        return len(self._chips())

    def _chip_kinds(self) -> Tuple[List[str], int]:
        """(문서 칩의 파일 이름들 — 확장자 뺀 것, 그림 칩 수)."""
        docs, images = [], 0
        for chip in self._chips():
            names = [(e.element_info.control_type, (e.element_info.name or "").strip()) for e in chip.descendants()]
            if any(t == "Image" and n == "attachment" for t, n in names):
                images += 1
                continue
            text = next((n for t, n in names if t == "Text" and n and n not in ("PDF", "TXT", "DOCX", "DOC", "HWP", "XLSX", "CSV", "MD")), "")
            docs.append(text)
        return sorted(docs), images

    def _attachments_match(self, files: List[Path]) -> Tuple[bool, str]:
        """입력칸의 첨부가 이 질문의 첨부와 정확히 같은가 — 문서는 파일 이름(확장자 뺌), 그림은 개수."""
        want_docs = sorted(f.stem for f in files if f.suffix.lower() not in self.IMAGE_EXT)
        want_images = sum(1 for f in files if f.suffix.lower() in self.IMAGE_EXT)
        deadline = time.monotonic() + 15   # 문서 칩은 붙인 직후 이름 글이 늦게 그려진다(10-21 밤) — 맞을 때까지 잠깐 기다린다
        while True:
            docs, images = self._chip_kinds()
            ok = docs == want_docs and images == want_images
            if ok or time.monotonic() >= deadline:
                break
            time.sleep(1.0)
        return ok, f"입력칸 첨부: 문서 {docs} · 그림 {images}개 / 질문의 첨부: 문서 {want_docs} · 그림 {want_images}개"

    def _clear_leftover_attachments(self) -> int:
        """입력칸에 남은 첨부(앞 질문이 실패해 남은 파일)를 지운다 — 칩마다 마우스를 올려 칩 안 지우기 버튼을 누른다."""
        removed = 0
        for _ in range(20):
            chips = self._chips()
            if not chips:
                break
            self._front()
            self._hover(chips[0])
            btn = next((e for e in chips[0].descendants(control_type="Button") if re.match(self.CHIP_CLOSE, e.element_info.name or "")), None)
            if btn is None:
                break
            self._press(btn)
            removed += 1
            time.sleep(1.0)
        return removed

    def _upload(self, files: List[Path]) -> None:
        """첨부 — 파일을 클립보드에 올리고 입력칸에 Ctrl+V. 칩 수가 늘 때까지 넉넉히 기다린다(큰 파일은 올리는 데 오래 걸림)."""
        from pywinauto.keyboard import send_keys
        before = self._attached_count()
        want = before + len(files)
        box = self._textbox()
        for _ in range(2):
            with input_turn():
                _clipboard_set_files(files)
                self._ensure_front()
                box.set_focus()
                time.sleep(0.4)
                if not self._is_foreground():   # 다른 창이 앞이면 Ctrl+V 가 그쪽에 들어간다 — 보내지 않고 다시
                    continue
                send_keys("^v", pause=0.05)
                time.sleep(1.0)   # 앱이 클립보드를 읽을 때까지 잠금을 쥔다
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if self._attached_count() >= want:
                    return
                time.sleep(1.0)
        raise UiaChatError(f"첨부 {len(files)}개가 입력칸에 붙지 않았습니다(붙은 수 {self._attached_count() - before}).")

    # --- 답 묶음 ---------------------------------------------------------------------------------------
    def _responses(self) -> List[Any]:
        """답 묶음 — class 의 첫 낱말이 정확히 'response-container'(안쪽의 'response-container-…' 상자는 빼기)."""
        return [g for g in self._all("Group") if (g.element_info.class_name or "").split(" ")[0] == self.RESPONSE_CLASS]

    def _response_text(self, group: Any) -> str:
        """답 묶음의 화면 글 — 표 칸 글은 'Text' 가 아니라 표 요소 이름으로만 잡혀(2026-10-21 Q19) 버튼을 뺀 모든 요소의 이름을 모은다."""
        try:
            return "\n".join((e.element_info.name or "") for e in group.descendants() if e.element_info.control_type != "Button")
        except Exception:
            return ""

    def _streaming(self) -> bool:
        if self._find("Button", self.STOP) is not None:
            return True
        return any(re.search(self.THINKING, e.element_info.name or "") for e in self._all("Text")[-30:])

    def _send_button(self) -> Any:
        return self._find("Button", self.SEND)

    def _wait_send_ready(self, timeout_s: float) -> Any:
        btn = self._wait_for("Button", self.SEND, timeout_s)
        if btn is None:
            raise UiaChatError("보내기 버튼이 켜지지 않았습니다(첨부가 아직 올라가는 중이거나 입력칸이 비어 있음).")
        return btn

    @staticmethod
    def _asks_empty(question: str) -> bool:
        from webapp.run_summary import asks_empty_output
        return asks_empty_output(None, question)

    def _last_rid(self) -> Optional[tuple]:
        groups = self._responses()
        return tuple(groups[-1].element_info.runtime_id) if groups else None

    def _new_response(self, before_rid: Optional[tuple]) -> Any:
        """보내기 전과 다른 마지막 답 묶음(새 답) 또는 None — 개수로 세면 긴 대화에서 앞 답이 트리에서 빠져 늘지 않는다(Q1 6턴, 2026-10-21),
        글로 비교하면 같은 거절 문장이 연달아 오면 못 가린다(Q1 2·3턴). 그래서 요소 자체(runtime id)로 본다."""
        groups = self._responses()
        if groups and tuple(groups[-1].element_info.runtime_id) != before_rid:
            return groups[-1]
        return None

    def _hold_if_no_response(self, before_rid: Optional[tuple], started: float, base: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + self.EMPTY_OUTPUT_WAIT_S
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                if self._new_response(before_rid) is not None:
                    return None
            except Exception:
                pass
        return {**base, "answer": "", "status": "COPY_FAILED", "capture": "none",
                "error": f"빈 출력 요구 — {self.EMPTY_OUTPUT_WAIT_S}초 안에 답 묶음(복사 버튼)이 뜨지 않음 → 보류 (사용자 규칙 10-20 밤)",
                "latency_ms": round((time.monotonic() - started) * 1000), "debug_screenshot": self._snapshot("empty-output-no-response")}

    # --- 복사 -------------------------------------------------------------------------------------------
    def _press_copy(self, btn: Any) -> str:
        with input_turn():   # 표시 글 → 복사 버튼 → 읽기 사이에 다른 수집기가 클립보드를 덮지 못하게
            _clipboard_set(self.COPY_SENTINEL)
            self._press(btn)
            for _ in range(12):
                time.sleep(0.4)
                text = _clipboard_get()
                if text and text != self.COPY_SENTINEL:
                    return text.replace("\r\n", "\n")
        return ""

    def _copy_buttons(self, group: Any) -> Tuple[Any, List[Any]]:
        """(답 묶음의 대답 복사 버튼, 코드블록 복사 버튼들) — 코드블록 안 버튼은 대답 복사가 아니다."""
        code, bottom = [], None
        for b in group.descendants(control_type="Button"):
            name = b.element_info.name or ""
            if re.match(self.CODE_COPY, name):
                code.append(b)
            elif re.match(self.COPY, name):
                bottom = b   # 마지막 것 — 도구 줄
        return bottom, code

    def _copy_last_answer(self) -> Tuple[str, List[str], List[str]]:
        """마지막 답 묶음의 '복사' → 원문, 그리고 코드블록마다 '코드 복사' → (코드들, 머리 글들). 복사한 글이 화면의 답과 맞는지 대조한다."""
        last_error = ""
        for round_ in range(6):
            groups = self._responses()
            if not groups:
                last_error = "답 묶음 없음"
                time.sleep(3)
                continue
            group = groups[-1]
            bottom, code_btns = self._copy_buttons(group)
            if bottom is None:   # 도구 줄이 안 그려져 있으면 마우스를 올려 띄운다
                self._front()
                self._hover(group)
                bottom, code_btns = self._copy_buttons(group)
            if bottom is None:
                last_error = "복사 버튼 없음"
                time.sleep(3)
                continue
            screen_raw = self._response_text(group)
            screen = _squash(screen_raw)
            text = self._press_copy(bottom)
            # 대조할 때만 빼는 것(저장하는 답은 복사 글 그대로, 2026-10-21 Q1 3턴):
            # 수식은 복사 글에 '$5 + 8 = 13$' · '\times' 로 오고 화면에는 그려진 글로만 있다 /
            # Gemini 가 계산하려고 실행한 코드와 출력은 복사 글에 코드블록으로 오지만 화면에는 '코드 표시'로 접혀 있다
            probe = re.sub(r"```.*?```", "", text or "", flags=re.S)
            if not probe.strip():   # 답이 코드블록뿐이면(Q11 JSON) ``` 표시 줄만 빼고 코드 본문으로 대조 — 화면 머리는 'JSON' 처럼 대문자라 소문자로 맞춘다
                # 실행한 코드 · 출력 블록이 앞에 붙어 오면(10-22 Q13: python · text · json 세 블록, 화면에는 json 만) 화면에 보이는 블록 하나로 대조
                blocks = re.findall(r"```[^\n]*\n(.*?)```", text or "", flags=re.S)
                probe = next((b for b in reversed(blocks) if _matches_screen(b.lower(), screen.lower())),
                             re.sub(r"^\s*```[^\n]*$", "", text or "", flags=re.M))
            probe = re.sub(r"\\[A-Za-z]+|\$", "", probe)
            # 문장부호뿐인 답('...' — 10-22 Q61 '아무 것도 답하지 마.')은 대조용으로 줄이면 빈 글이 되므로 화면 글에 그대로 있는지로 본다
            punct_only = bool((text or "").strip()) and not _squash(probe)
            if text and (_matches_screen(probe.lower(), screen.lower()) or (punct_only and text.strip() in (screen_raw or ""))):
                codes, heads = [], []
                for b in code_btns:
                    heads.append(self._code_head(b))
                    got = ""
                    for _ in range(3):
                        got = self._press_copy(b)
                        if got:
                            break
                    codes.append(got)
                return text, codes, heads
            last_error = "복사한 글이 화면의 답과 다름" if text else "클립보드에 글이 안 들어옴"
            last_dump = {"copied": text, "screen": screen_raw, "groups": [g.element_info.class_name for g in groups]}
            time.sleep(3)
        self._snapshot("copy-failed")
        if self.debug_dir and "last_dump" in locals():   # 원인을 바로 볼 수 있게 복사 글 · 화면 글을 남긴다
            try:
                import json
                self.debug_dir.mkdir(parents=True, exist_ok=True)
                (self.debug_dir / f"{self.NAME}-copy-failed-{int(time.time())}.json").write_text(json.dumps(last_dump, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception:
                pass
        raise CopyFailed(f"복사 실패({last_error}) — 6번 다시 시도함, 보류(사용자 규칙 10-20)")

    def _code_head(self, btn: Any) -> str:
        """코드블록 머리 글(Python · JSON …) — 버튼을 감싼 code-block Group 의 첫 글."""
        p = btn
        for _ in range(4):
            try:
                p = p.parent()
            except Exception:
                return ""
            if p is None:
                return ""
            if (p.element_info.class_name or "").startswith(self.CODE_BLOCK_CLASS):
                for t in p.descendants(control_type="Text"):
                    if (t.element_info.name or "").strip():
                        return t.element_info.name.strip()
                return ""
        return ""

    def recopy(self) -> str:
        try:
            text, codes, heads = self._copy_last_answer()
        except CopyFailed:
            return ""
        return self._merge(text, codes, heads)[0]

    @staticmethod
    def _merge(text: str, codes: List[str], heads: List[str]) -> Tuple[str, Optional[str]]:
        """대답 복사 글의 코드블록 안을 코드 복사 글로 — 바뀐 게 없으면 (원문, None)."""
        if not codes or "```" not in text:
            return text, None
        from tools.codeblock_fix import merge_codes
        new, why = merge_codes(text, codes, heads)
        return (new, why) if new is not None else (text, None)

    # --- 질문 → 답 -------------------------------------------------------------------------------------
    def ask(self, question: str, files: Optional[List[Path]] = None, reference: Any = None) -> Dict[str, Any]:
        """현재 채팅에 질문을 보내고 답이 끝날 때까지 기다린다 → {answer, status, error, url, sent, latency_ms, model, method, capture}."""
        started = time.monotonic()
        files = list(files or [])
        sent = [{"name": f.name, "how": "클립보드 붙여넣기"} for f in files]
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
        base = {"url": None, "sent": sent, "model": self.model_seen or self.MODEL_LABEL, "method": self.METHOD}
        before_rid = self._last_rid()   # 보내기 전 마지막 답 묶음 — 새 답은 이것과 다른 요소
        try:
            if self.cancelled():
                raise UiaChatError("중단 요청으로 질문을 보내지 않음")
            self._front()
            self._escape()
            if self._box_text():
                self._set_text("")
            left = self._clear_leftover_attachments()
            if left:
                print(f"   (입력칸에 남아 있던 첨부 {left}개 지움)", flush=True)
            if self._attached_count():
                raise UiaChatError(f"입력칸에 남은 첨부를 지우지 못해 보내지 않음 — {self._attachments_match([])[1]}")
            if files:
                self._upload(files)
            ok, why = self._attachments_match(files)   # 보내기 직전 — 첨부가 이 질문과 정확히 같아야 보낸다(남은 것 · 모자란 것 · 다른 것이면 멈춤)
            if not ok:
                raise UiaChatError(f"첨부가 질문과 달라 보내지 않음 — {why}")
            self._set_text(text)
            btn = self._wait_send_ready(300 if files else 30)
            self._press(btn)
            sent_ok = False
            send_deadline = time.monotonic() + 120
            tick = 0
            while time.monotonic() < send_deadline:
                time.sleep(0.7)
                tick += 1
                if self._streaming() or self._new_response(before_rid) is not None or not self._box_text():
                    sent_ok = True
                    break
                if tick == 12:   # 8초가 지나도 안 보내졌으면 보내기 버튼을 마우스로
                    b2 = self._send_button()
                    if b2 is not None:
                        try:
                            with input_turn():
                                self._ensure_front()
                                b2.click_input()
                        except Exception:
                            pass
                elif tick == 30:   # 20초 — 입력칸에서 Enter
                    from pywinauto.keyboard import send_keys
                    try:
                        with input_turn():
                            self._ensure_front()
                            self._textbox().set_focus()
                            if self._is_foreground():
                                send_keys("{ENTER}")
                    except Exception:
                        pass
            if not sent_ok:
                raise UiaChatError("전송 뒤에도 답변이 시작되지 않음")
        except Exception as exc:
            return {**base, "answer": "", "status": "ERROR",
                    "error": (str(exc)[:300] if isinstance(exc, UiaChatError) else f"질문을 보내지 못했습니다: {type(exc).__name__}: {str(exc)[:120]}"),
                    "latency_ms": round((time.monotonic() - started) * 1000), "debug_screenshot": self._snapshot("send-failed")}
        t_sent = time.monotonic()
        if self._asks_empty(question):   # 빈 출력 요구: 15초 안에 답 묶음이 안 생기면 바로 보류 (사용자 규칙 10-20 밤)
            held = self._hold_if_no_response(before_rid, started, base)
            if held is not None:
                return held
        deadline = time.monotonic() + (self.answer_timeout_s or 10 ** 9)   # 0 = 제한 없음
        last, stable, quiet = None, 0, 0
        while time.monotonic() < deadline:
            time.sleep(1.5)
            if self.cancelled():
                break
            self._unminimize()
            if self._streaming():   # 생각 중에는 답 묶음이 빈 채로 먼저 생긴다(••• 표시) — 정지 버튼이 사라질 때까지 기다린다 (2026-10-21)
                stable, quiet = 0, 0
                continue
            group = self._new_response(before_rid)
            current = self._response_text(group).strip() if group is not None else ""
            stable = stable + 1 if current == last else 0
            last = current
            if stable >= 2 and current:
                break
            quiet += 1
            if quiet >= 40:   # 정지 버튼 없이 60초째 글이 없음 — 빈 답(또는 답 없음)으로 끝난 것
                break
        done = self._new_response(before_rid) is not None and (bool(last) or quiet >= 40)
        if not done and quiet >= 40:
            return {**base, "answer": "", "status": "ERROR", "capture": "none", "error": "답 묶음이 생기지 않음(응답 없음)",
                    "latency_ms": round((time.monotonic() - started) * 1000), "debug_screenshot": self._snapshot("no-response")}
        t_done = time.monotonic()
        base["phases_s"] = {"send": round(t_sent - started, 1), "answer": round(t_done - t_sent, 1)}   # 보내기 · 답 기다림 (· 복사는 아래) — 느린 곳 찾기 (10-21)
        answer, capture, extra = "", "text", {}
        if done and not last:   # 답 묶음은 생겼는데 글이 없다 — 빈 답(복사 버튼도 없다)
            return {**base, "answer": "", "status": "SUCCESS", "capture": "empty", "error": None, "latency_ms": round((time.monotonic() - started) * 1000)}
        if done:
            self._front()
            try:
                bottom, codes, heads = self._copy_last_answer()
            except CopyFailed as exc:
                return {**base, "answer": "", "status": "COPY_FAILED", "capture": "none", "error": str(exc)[:300],
                        "latency_ms": round((time.monotonic() - started) * 1000)}
            except UiaChatError as exc:
                return {**base, "answer": "", "status": "ERROR", "error": str(exc)[:300], "latency_ms": round((time.monotonic() - started) * 1000)}
            base["phases_s"]["copy"] = round(time.monotonic() - t_done, 1)
            if bottom and bottom.strip():
                answer, capture = bottom, "copy-button"
                merged, why = self._merge(bottom, codes, heads)
                if why:
                    answer = merged
                    extra = {"answer_bottom_copy": bottom, "codeblock_fix": why, "code_blocks": len(codes)}
                elif codes:
                    extra = {"code_blocks": len(codes), "codeblock_fix": "코드 복사 글과 같음"}
                from webapp.copy_cleanup import join_split_table_rows
                fixed, rows = join_split_table_rows(answer)   # 대답 복사가 표 칸 뒤에 끼워 넣은 빈 줄로 쪼개진 표 행을 잇는다 (사용자 OK 10-21 밤)
                if rows:
                    extra = {**extra, "answer_before_table_fix": answer, "table_rows_joined": rows}
                    answer = fixed
            else:
                answer, capture = "", "empty"   # 답 묶음은 생겼는데 복사할 글이 없다 — 빈 답
        return {**base, **extra, "answer": answer if done else "", "status": "SUCCESS" if done else "INCOMPLETE", "capture": capture,
                "error": None if done else "답변이 제한 시간 안에 끝나지 않았습니다.",
                **({} if done else {"debug_screenshot": self._snapshot("incomplete")}),
                "latency_ms": round((time.monotonic() - started) * 1000)}
