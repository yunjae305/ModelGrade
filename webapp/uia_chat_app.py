"""Claude 데스크톱 앱 수집기 — Windows UI 자동화(UIA, 화면 낭독기가 쓰는 접근성 API)로 앱 채팅 화면을 사람처럼 조작한다 (claude-app-uia-v1, 2026-10-19).

왜 UIA 인가: Claude 앱은 디버깅 스위치(--remote-debugging-port)가 있으면 시작을 거부해 ChatGPT 앱처럼 CDP 로 못 붙는다(tools/claude_app.py).
앱 채팅은 claude.ai 웹을 그대로 띄운 것이라(app.asar 가 https://claude.ai/ 를 연다) 요소 이름이 웹 ClaudeChat 과 같다.
조작 순서(요소 이름은 한국어 UI 기준, 2026-10-19 확인):
  채팅 탭(RadioButton 'Claude') → '새로 생성' → 모델 버튼 '모델: Sonnet 5.5 중간'(아니면 메뉴에서 'Sonnet 5.5' 라디오·'노력' → '중간')
  → 첨부: 파일을 클립보드에 올리고(탐색기의 복사와 같음) 입력칸에 Ctrl+V (사용자 확인 10-19) — 안 붙으면 '파일, 커넥터 등 추가' → '파일 업로드 또는 추가' → 열기 대화상자
  → 입력칸(Edit 'Claude에게 프롬프트…')에 값 설정(ValuePattern, 포커스 불필요) → '메시지 보내기'
  → 정지 버튼이 사라지고 글이 멈추면 마지막 '메시지 작업' 툴바의 '복사' → 클립보드 원문. 끝나도 모델은 되돌리지 않는다(사용자 10-19).
수집기 인터페이스는 webapp/cdp_chat_web.py 의 _CdpChat 과 같다(new_chat · ask → {answer, status, …}) — tools/modelgrade.py collect app 이 그대로 쓴다.
"""
from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from webapp.cdp_chat_web import REFERENCE_FILE_CHARS


class UiaChatError(RuntimeError):
    pass


class CopyFailed(UiaChatError):
    """답은 있는데 복사해 오지 못함 — 수집 상태 COPY_FAILED(판정 보류, 사용자 규칙 2026-10-20)."""


def file_dialog_text(files: List[Path]) -> str:
    """열기 대화상자의 파일 이름 칸에 넣을 글 — 하나면 경로 그대로, 여럿이면 "경로" "경로" (대화상자가 여러 파일을 받는다)."""
    paths = [str(Path(f).resolve()) for f in files]
    return paths[0] if len(paths) == 1 else " ".join(f'"{p}"' for p in paths)


def model_ok(label: str) -> bool:
    """모델 버튼 글이 사용자 결정(Sonnet 5.5 · 노력 중간, 2026-10-15)과 맞는가."""
    return "Sonnet 5.5" in (label or "") and "중간" in (label or "")


class ClaudeAppChat:
    NAME = "Claude앱"
    MODEL_LABEL = "Claude Sonnet 5.5 · 노력 중간 (앱)"
    METHOD = "claude-app-uia-v1"
    CHAT_TAB = r"^Claude"              # 상단 토글 RadioButton ('Code, 작업 중' 과 구분)
    NEW_CHAT = r"^새로 생성$"
    TEXTBOX = r"^Claude에게 프롬프트"
    SEND = r"^메시지 보내기"
    STOP = r"^(응답 중지|중지|Stop)"
    ATTACH = r"^파일, 커넥터 등 추가"
    ATTACH_ITEM = r"^파일 업로드 또는 추가"
    REMOVE = r"\.[A-Za-z0-9]{1,5} (삭제|제거)$|^Remove .+\.[A-Za-z0-9]{1,5}$"   # 입력칸 첨부 칩의 지우기 버튼('1792…_이름.txt 삭제')만 — 채팅 이름에 '제거' 가 든 사이드바 버튼을 누른 사고(2026-10-20)
    MODEL_BTN = r"^모델: "
    MODEL_RADIO = r"^Sonnet 5\.5"
    EFFORT_ITEM = r"^노력"
    MORE_MODELS = r"^더 많은 모델"      # 메뉴가 현재 모델 하나만 보일 때 나머지를 펴는 항목
    EFFORT_PICK = r"^중간"
    ACTION_BAR = r"^메시지 작업"
    MESSAGE_GROUP = r"^(\d+)개 메시지 중 (\d+)번째$"   # 말풍선 Group 이름 — 마지막 번째가 방금 받은 답
    A11Y_LABEL = r"^(Claude 응답|입력 내용): "          # 화면 낭독기용 머리글 Text — 답 본문이 아니다
    COPY = r"^(복사|Copy)$"
    DIALOG_TITLE = r"^(열기|Open)"

    def __init__(self, *, headless: bool = False, debug_dir: Optional[Path] = None, cancelled: Optional[Callable[[], bool]] = None,
                 answer_timeout_s: int = 600, place: Optional[str] = None) -> None:
        self.debug_dir, self.cancelled, self.answer_timeout_s, self.place = debug_dir, cancelled or (lambda: False), answer_timeout_s, place
        self.win: Any = None
        self._temp = tempfile.TemporaryDirectory(prefix="claude-app-ref-")
        self.model_seen: Optional[str] = None

    # --- 연결 ---------------------------------------------------------------------------------------
    def __enter__(self) -> "ClaudeAppChat":
        from tools import claude_app
        s = claude_app.launch()
        if not s["ok"]:
            raise UiaChatError("Claude 앱을 켜지 못했습니다 — Store 의 Claude 앱을 직접 켜고 로그인한 뒤 다시 시도하세요.")
        self.win = claude_app.main_window(s["pids"])
        if self.win is None:
            raise UiaChatError("Claude 앱 창을 찾지 못했습니다.")
        if self.place:
            place_window(self.win, self.place)
        self._select_chat_tab()
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

    @staticmethod
    def _press(elem: Any) -> None:
        """버튼 누르기 — Invoke 패턴이 없으면(메뉴 여는 버튼) 펼치기, 그것도 없으면 마우스 클릭."""
        for how in ("invoke", "expand", "select", "click_input"):
            try:
                getattr(elem, how)()
                return
            except Exception:
                continue
        raise UiaChatError(f"누르지 못함: {elem.element_info.name!r}")

    def _front(self) -> None:
        try:
            self.win.set_focus()
        except Exception:
            pass
        time.sleep(0.3)

    @staticmethod
    def _hover(elem: Any) -> None:
        from pywinauto import mouse
        r = elem.element_info.rectangle
        mouse.move(coords=((r.left + r.right) // 2, (r.top + r.bottom) // 2))
        time.sleep(1.2)

    def _is_foreground(self) -> bool:
        try:
            import win32gui
            return win32gui.GetForegroundWindow() == self.win.handle
        except Exception:
            return True

    def _escape(self) -> None:
        """열린 메뉴 닫기 — 키 대신 메뉴 버튼 접기(다른 앱 창이 앞에 있어도 안전). 그래도 남아 있고 이 창이 앞이면 Esc."""
        if not self._all("Menu"):
            return
        for pat in (self.MODEL_BTN, self.ATTACH):
            btn = self._find("Button", pat)
            if btn is not None:
                try:
                    btn.collapse()
                except Exception:
                    pass
        time.sleep(0.4)
        if self._all("Menu") and self._is_foreground():
            from pywinauto.keyboard import send_keys
            try:
                send_keys("{ESC}")
            except Exception:
                pass
            time.sleep(0.4)

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

    def _select_chat_tab(self) -> None:
        tab = self._find("RadioButton", self.CHAT_TAB)
        if tab is None:
            raise UiaChatError("앱 상단의 'Claude'(채팅) 탭을 찾지 못했습니다 — 창이 너무 작거나 로그인 전입니다.")
        if not tab.is_selected():
            tab.select()
            time.sleep(1.0)

    # --- 새 채팅 · 모델 -------------------------------------------------------------------------------
    def new_chat(self) -> None:
        self._front()
        self._select_chat_tab()
        self._escape()
        btn = self._wait_for("Button", self.NEW_CHAT, 10)
        if btn is None:
            raise UiaChatError("'새로 생성' 버튼을 찾지 못했습니다.")
        self._press(btn)
        time.sleep(1.5)
        if self._wait_for("Edit", self.TEXTBOX, 20) is None:
            raise UiaChatError("새 채팅 입력칸이 보이지 않습니다.")
        try:
            self.model_seen = self._apply_model()
        except Exception as exc:
            self.model_seen = f"{self.MODEL_LABEL} (설정 확인 못 함: {type(exc).__name__}: {str(exc)[:80]})"
            self._snapshot("model-settings-failed")

    def _model_label(self) -> str:
        btn = self._wait_for("Button", self.MODEL_BTN, 10)
        if btn is None:
            raise UiaChatError("모델 버튼('모델: …')을 찾지 못했습니다.")
        return (btn.element_info.name or "").replace("모델: ", "").strip()

    def _apply_model(self) -> str:
        """수집 설정(사용자 결정 10-15): Sonnet 5.5 · 노력 중간. 끝나도 되돌리지 않는다(사용자 10-19: "안 그래도 돼")."""
        label = self._model_label()
        if not model_ok(label):
            label = self._pick_model("Sonnet 5.5", "중간")
        if not model_ok(label):
            raise UiaChatError(f"모델을 Sonnet 5.5 · 노력 중간으로 맞추지 못했습니다 (지금 {label}) — 앱에서 직접 맞추세요")
        return f"Claude {label} (앱)"

    def _pick_model(self, model: str, effort: str) -> str:
        """모델 메뉴에서 모델 라디오('Sonnet 5.5 …')와 노력 단계('중간')를 고른다 — 고른 뒤 버튼 글로 확인하고, 안 바뀌면 방법을 바꿔 다시(최대 3번).
        목록에 없는 모델은 '더 많은 모델' 에 마우스를 올려 펼친다(클릭은 노력 하위 메뉴를 건드린다). 라디오는 UIA 선택이 먼저(마우스가 움직이면 펼친 목록이 닫힌다), 그다음 마우스 클릭."""
        label = self._model_label()
        if model and model not in label:
            pat = "^" + re.escape(model) + r"(\s|$)"
            for attempt in range(3):
                self._press(self._find("Button", self.MODEL_BTN))
                time.sleep(1.0)
                radio = self._wait_for("RadioButton", pat, 2)
                if radio is None:
                    more = self._find("MenuItem", self.MORE_MODELS)
                    if more is not None:
                        self._hover(more)
                        radio = self._wait_for("RadioButton", pat, 3)
                if radio is None:
                    self._escape()
                    raise UiaChatError(f"모델 메뉴에 {model} 이(가) 없습니다 (지금 {label})")
                self._choose(radio, attempt)
                time.sleep(1.0)
                self._escape()
                label = self._model_label()
                if model in label:
                    break
            if model not in label:
                raise UiaChatError(f"모델을 {model} 로 바꾸지 못했습니다 (지금 {label})")
        if effort and effort not in label:
            for attempt in range(3):
                self._press(self._find("Button", self.MODEL_BTN))
                time.sleep(1.0)
                item = self._wait_for("MenuItem", self.EFFORT_ITEM, 5)
                if item is None:
                    self._escape()
                    raise UiaChatError(f"모델 메뉴에 '노력' 항목이 없습니다 (지금 {label})")
                self._hover(item)   # 노력 항목에 올리면 단계(낮음·중간·높음·엑스트라·최대)가 펼쳐진다
                pick = None
                for kind in ("RadioButton", "MenuItem", "ListItem", "Button"):
                    pick = self._wait_for(kind, "^" + re.escape(effort), 2)
                    if pick is not None:
                        break
                if pick is None:
                    self._escape()
                    raise UiaChatError(f"노력 메뉴에 '{effort}' 이(가) 없습니다")
                self._choose(pick, attempt)
                time.sleep(1.0)
                self._escape()
                label = self._model_label()
                if effort in label:
                    break
            if effort not in label:
                raise UiaChatError(f"노력을 {effort} 으로 바꾸지 못했습니다 (지금 {label})")
        return label

    @staticmethod
    def _choose(elem: Any, attempt: int) -> None:
        """메뉴 항목 고르기 — 첫 시도는 UIA 선택/누르기(마우스 안 움직임), 다음부터는 마우스 클릭."""
        if attempt == 0:
            for how in ("select", "invoke"):
                try:
                    getattr(elem, how)()
                    return
                except Exception:
                    continue
        elem.click_input()

    # --- 입력칸 · 첨부 --------------------------------------------------------------------------------
    def _textbox(self) -> Any:
        box = self._wait_for("Edit", self.TEXTBOX, 10)
        if box is None:
            raise UiaChatError("입력칸을 찾지 못했습니다.")
        return box

    def _box_text(self) -> str:
        box = self._find("Edit", self.TEXTBOX)
        try:
            return (box.get_value() or "").strip() if box is not None else ""
        except Exception:
            return ""

    def _set_text(self, text: str) -> None:
        """입력칸에 글을 넣는다 — UIA 값 설정(포커스·키보드 없이, 2026-10-19 확인). 안 들어가면 클립보드 붙여넣기."""
        box = self._textbox()
        try:
            box.set_edit_text(text)
            time.sleep(0.6)
        except Exception:
            pass
        if self._box_text()[:40] != text.strip()[:40]:
            from pywinauto.keyboard import send_keys
            _clipboard_set(text)
            box.set_focus()
            send_keys("^a{DEL}^v", pause=0.05)
            time.sleep(0.8)
        if self._box_text()[:40] != text.strip()[:40]:
            raise UiaChatError("입력칸에 질문이 들어가지 않았습니다.")

    def _clear_leftover_attachments(self) -> int:
        """입력칸에 남은 첨부(앞 질문이 실패해 남은 파일)를 지운다 — 남아 있으면 다음 질문이 계속 막힌다(간단평가 1차 실행, 2026-10-20)."""
        removed = 0
        for _ in range(20):
            btn = next((e for e in self._all("Button") if re.search(self.REMOVE, e.element_info.name or "")), None)
            if btn is None:
                break
            self._press(btn)
            removed += 1
            time.sleep(0.5)
        return removed

    def _attached_count(self) -> int:
        return sum(1 for e in self._all("Button") if re.search(self.REMOVE, e.element_info.name or ""))

    def _wait_attached(self, want: int, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._attached_count() >= want:
                return True
            time.sleep(1.0)
        return self._attached_count() >= want

    def _upload(self, files: List[Path]) -> None:
        """첨부 — 파일을 클립보드에 올리고 입력칸에 Ctrl+V(사용자 확인 2026-10-19). 20초 안에 첨부 칩이 안 늘면 열기 대화상자로 다시.
        큰 파일(수십만 자)은 칩이 붙은 뒤에도 올리는 데 오래 걸리므로 보내기 버튼이 켜질 때까지는 ask 가 따로 기다린다."""
        from pywinauto.keyboard import send_keys
        before = self._attached_count()
        want = before + len(files)
        box = self._textbox()
        for _ in range(2):
            _clipboard_set_files(files)
            self._front()
            box.set_focus()
            time.sleep(0.4)
            if not self._is_foreground():   # 다른 앱 창이 앞이면 Ctrl+V 가 그쪽에 들어간다 — 보내지 않고 다시
                time.sleep(1.0)
                continue
            send_keys("^v")
            if self._wait_attached(want, 20):
                time.sleep(1.0)
                return
        self._snapshot("paste-attach-failed")
        self._upload_via_dialog(files, want)

    def _upload_via_dialog(self, files: List[Path], want: int) -> None:
        """예비: '파일, 커넥터 등 추가' → '파일 업로드 또는 추가' → 열기 대화상자(파일 이름 칸에 경로) → 입력칸의 첨부 지우기 버튼이 늘면 성공."""
        from pywinauto import Desktop
        self._press(self._wait_for("Button", self.ATTACH, 10) or self._fail("첨부 버튼을 찾지 못했습니다"))
        time.sleep(1.0)
        item = self._wait_for("MenuItem", self.ATTACH_ITEM, 5)
        if item is None:
            self._escape()
            raise UiaChatError("첨부 메뉴에 '파일 업로드 또는 추가' 가 없습니다.")
        self._press(item)
        dlg = Desktop(backend="win32").window(class_name="#32770", title_re=self.DIALOG_TITLE)
        try:
            dlg.wait("exists ready", timeout=20)
        except Exception:
            self._escape()
            raise UiaChatError("파일 열기 대화상자가 뜨지 않았습니다.")
        name_box = None
        for crit in ({"control_id": 1148, "class_name": "Edit"}, {"class_name": "Edit"}):
            try:
                name_box = dlg.child_window(**crit).wrapper_object()
                break
            except Exception:
                continue
        if name_box is None:
            dlg.close()
            raise UiaChatError("열기 대화상자의 파일 이름 칸을 찾지 못했습니다.")
        name_box.set_edit_text(file_dialog_text(files))
        time.sleep(0.3)
        try:
            dlg.child_window(control_id=1, class_name="Button").click()
        except Exception:
            dlg.type_keys("{ENTER}")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and dlg.exists():
            time.sleep(0.5)
        if dlg.exists():
            dlg.close()
            raise UiaChatError(f"열기 대화상자가 파일을 받지 않았습니다 ({', '.join(f.name for f in files)})")
        if not self._wait_attached(want, 120):   # 큰 파일(수십만 자)은 칩이 붙기까지 오래 걸린다
            raise UiaChatError(f"{self.NAME} 첨부를 입력칸에 붙이지 못했습니다 ({', '.join(f.name for f in files)}; 지우기 버튼 {self._attached_count()}/{want})")
        time.sleep(1.0)

    def _fail(self, msg: str) -> Any:
        raise UiaChatError(msg)

    # --- 보내기 · 기다리기 · 복사 ---------------------------------------------------------------------
    def _send_button(self) -> Any:
        return self._find("Button", self.SEND)

    def _wait_send_ready(self, timeout_s: float) -> Any:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            btn = self._send_button()
            if btn is not None and btn.element_info.enabled:
                return btn
            time.sleep(0.5)
        raise UiaChatError("보내기 버튼이 켜지지 않았습니다 (첨부 처리 중이거나 입력이 비었음).")

    def _streaming(self) -> bool:
        return self._find("Button", self.STOP) is not None

    def _bars(self) -> List[Any]:
        return [e for e in self._all("ToolBar") if re.match(self.ACTION_BAR, e.element_info.name or "")]

    def _answer_bars(self) -> List[Any]:
        """답변 말풍선의 '메시지 작업' 툴바 — 복사 버튼이 있고 '편집'(내 질문 쪽) 버튼이 없는 것."""
        out = []
        for bar in self._bars():
            names = [b.element_info.name or "" for b in bar.descendants(control_type="Button")]
            if any(re.match(self.COPY, n) for n in names) and not any(n.startswith("편집") or n.startswith("Edit") for n in names):
                out.append(bar)
        return out

    def _page_text(self) -> str:
        return "\n".join((e.element_info.name or "") for e in self._all("Text"))

    def _wait_group(self, timeout_s: float) -> Any:
        deadline = time.monotonic() + timeout_s
        while True:
            g = self._last_message_group()
            if g is not None or time.monotonic() >= deadline:
                return g
            time.sleep(0.5)
            self._select_chat_tab()

    def _last_message_group(self) -> Any:
        """마지막 말풍선 Group('N개 메시지 중 K번째' 의 K 최대)."""
        best, best_k = None, -1
        for g in self._all("Group"):
            m = re.match(self.MESSAGE_GROUP, g.element_info.name or "")
            if m and int(m.group(2)) > best_k:
                best, best_k = g, int(m.group(2))
        return best

    def _group_screen_text(self, group: Any) -> str:
        """말풍선 안의 글 전부 — 이름 있는 모든 요소(Text·ListItem·셀 등)와 편집 블록 값. 목록 항목은 Text 가 아니라 ListItem 이름에 들어 있다(2026-10-20)."""
        parts = []
        for d in group.descendants():
            i = d.element_info
            if i.name and i.control_type not in ("Button", "ToolBar"):
                parts.append(i.name)
            if i.control_type == "Edit":
                try:
                    parts.append(d.get_value() or "")
                except Exception:
                    pass
        return "\n".join(parts)

    def _draft_blocks(self, group: Any) -> List[str]:
        """답 안의 편집 블록(초안·문서 카드, Edit) 값들 — 복사 아이콘 글에는 이 블록이 안 들어간다(Q56, 2026-10-19). 수만 기록한다."""
        out = []
        for d in group.descendants(control_type="Edit"):
            try:
                v = (d.get_value() or "").strip()
            except Exception:
                v = ""
            if v:
                out.append(v)
        return out

    COPY_SENTINEL = "\u2063modelgrade-copy-wait\u2063"

    def _copy_last_answer(self) -> str:
        """마지막 답의 복사 아이콘 → 클립보드. 반드시 채팅 탭에서 — Code 탭이 앞에 있으면 그 탭의 글을 복사한다(Q3 사고 2026-10-19).
        긴 대화에서는 말풍선 이름('N개 메시지 중 K번째')이 잠시 안 잡히거나 글이 늦게 갱신되므로 3번까지 다시 본다(간단평가 재수집 2026-10-20).
        다른 앱(동시 수집)이 그 사이 클립보드를 쓸 수 있어, 표식을 넣어 두고 바뀐 글이 화면의 답과 맞는지 대조한다."""
        last_error = ""
        for round_ in range(6):
            self._select_chat_tab()
            if round_:
                self._close_artifact_panel()   # 아티팩트 패널이 열리면 채팅 말풍선·툴바가 트리에서 사라진다
                self._scroll_chat_bottom()     # 가상화된 긴 대화는 아래로 내려야 마지막 말풍선이 나타난다
            group = self._wait_group(5)
            if group is not None:
                top = group.element_info.rectangle.top
                bars = [b for b in self._answer_bars() if b.element_info.rectangle.top >= top - 10]
                screen = _squash(self._group_screen_text(group))
            else:
                docs = [d.element_info.name or "" for d in self._all("Document")]
                if any("Claude Code" in d for d in docs) and not any(d.endswith("- Claude") for d in docs):
                    last_error = "채팅 탭이 아님(Claude Code 화면)"
                    time.sleep(3)
                    continue
                bars = self._answer_bars()   # 말풍선 이름이 안 잡혀도 채팅 탭이면 마지막 답변 툴바가 마지막 답이다
                screen = _squash(self._page_text())
            if not bars and group is not None:   # 긴 대화에서 마지막 답의 툴바가 안 그려져 있으면 말풍선에 마우스를 올려 띄운다 (10턴째 실패, 2026-10-20)
                try:
                    self._front()
                    r = group.element_info.rectangle
                    from pywinauto import mouse
                    mouse.move(coords=((r.left + r.right) // 2, min(r.bottom - 40, r.top + 200)))
                    time.sleep(1.2)
                    top = group.element_info.rectangle.top
                    bars = [b for b in self._answer_bars() if b.element_info.rectangle.top >= top - 10]
                except Exception:
                    bars = []
            if not bars:
                last_error = "답변 툴바 없음"
                time.sleep(3)
                continue
            btn = next((b for b in bars[-1].descendants(control_type="Button") if re.match(self.COPY, b.element_info.name or "")), None)
            if btn is None:
                last_error = "복사 버튼 없음"
                time.sleep(3)
                continue
            _clipboard_set(self.COPY_SENTINEL)
            self._press(btn)
            text = ""
            for _ in range(10):
                time.sleep(0.5)
                text = _clipboard_get()
                if text and text != self.COPY_SENTINEL:
                    break
            if text and text != self.COPY_SENTINEL and _matches_screen(text, screen):
                return text
            last_error = "복사한 글이 화면의 답과 다름" if text and text != self.COPY_SENTINEL else "클립보드에 글이 안 들어옴"
            time.sleep(3)
        self._dump_copy_failure(last_error)
        raise CopyFailed(f"복사 실패({last_error}) — 6번 다시 시도함, 보류(사용자 규칙 10-20)")

    def _close_artifact_panel(self) -> bool:
        """아티팩트 패널(오른쪽, Document 'User-generated artifact content')이 열려 있으면 그 '닫기' 를 누른다."""
        docs = [(d.element_info.name or "") for d in self._all("Document")]
        if not any("artifact" in d.lower() for d in docs):
            return False
        closers = [b for b in self._all("Button") if (b.element_info.name or "") in ("닫기", "Close")]
        if not closers:
            return False
        btn = max(closers, key=lambda b: b.element_info.rectangle.left)   # 패널의 닫기는 오른쪽 끝
        try:
            self._press(btn)
            time.sleep(1.5)
            return True
        except Exception:
            return False

    def _scroll_chat_bottom(self) -> None:
        """채팅 영역을 끝까지 내린다 — 마우스 휠(이 창이 앞에 있을 때만)."""
        try:
            if not self._is_foreground():
                self._front()
            from pywinauto import mouse
            r = self.win.rectangle()
            x, y = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            mouse.scroll(coords=(x, y), wheel_dist=-40)
            time.sleep(0.8)
        except Exception:
            pass

    def _dump_copy_failure(self, why: str) -> None:
        """복사 실패 순간의 화면(png)과 말풍선·툴바·버튼 이름 목록(txt)을 남긴다 — 원인 확인용."""
        path = self._snapshot(f"copy-failed-{why[:10]}")
        if not self.debug_dir:
            return
        try:
            lines = [f"why: {why}", f"docs: {[d.element_info.name for d in self._all('Document')]}"]
            for e in self._all():
                i = e.element_info
                if i.control_type in ("Group", "ToolBar") and i.name and (re.match(self.MESSAGE_GROUP, i.name) or re.match(self.ACTION_BAR, i.name)):
                    lines.append(f"{i.control_type} {i.rectangle} {i.name[:50]}")
            lines += [f"Button {b.element_info.rectangle.top} {b.element_info.name[:40]}" for b in self._all("Button") if re.match(self.COPY, b.element_info.name or "")]
            (self.debug_dir / f"{self.NAME}-copy-failed-{int(time.time())}.txt").write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

    def ask(self, question: str, files: Optional[List[Path]] = None, reference: Any = None) -> Dict[str, Any]:
        """현재 채팅에 질문을 보내고 답변이 끝날 때까지 기다린다 → {answer, status, error, url, sent, latency_ms, model, method, capture}."""
        started = time.monotonic()
        files = list(files or [])
        sent = [{"name": f.name, "how": "클립보드 붙여넣기 (앱이 이름 앞에 숫자를 붙임)"} for f in files]
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
        before = len(self._answer_bars())
        text_before = self._page_text()
        try:
            if self.cancelled():
                raise UiaChatError("중단 요청으로 질문을 보내지 않음")
            self._front()
            self._select_chat_tab()
            self._escape()
            if self._box_text():
                self._textbox().set_edit_text("")
                time.sleep(0.4)
            left = self._clear_leftover_attachments()
            if left:
                print(f"   (입력칸에 남아 있던 첨부 {left}개 지움)", flush=True)
            if files:
                self._upload(files)
            self._set_text(text)
            btn = self._wait_send_ready(300 if files else 30)
            self._press(btn)
            sent_ok = False
            send_deadline = time.monotonic() + 120
            tick = 0
            while time.monotonic() < send_deadline:
                time.sleep(0.7)
                tick += 1
                if self._streaming() or len(self._answer_bars()) > before or not self._box_text():
                    sent_ok = True
                    break
                if tick == 12:   # 8초가 지나도 안 보내졌으면 보내기 버튼을 마우스로
                    b2 = self._send_button()
                    if b2 is not None:
                        try:
                            self._front()
                            b2.click_input()
                        except Exception:
                            pass
                elif tick == 30 and self._is_foreground():   # 20초 — 입력칸에서 Enter
                    from pywinauto.keyboard import send_keys
                    try:
                        self._textbox().set_focus()
                        send_keys("{ENTER}")
                    except Exception:
                        pass
            if not sent_ok:
                raise UiaChatError("전송 뒤에도 답변이 시작되지 않음")
        except Exception as exc:
            return {**base, "answer": "", "status": "ERROR",
                    "error": (str(exc)[:300] if isinstance(exc, UiaChatError) else f"질문을 보내지 못했습니다: {type(exc).__name__}: {str(exc)[:120]}"),
                    "latency_ms": round((time.monotonic() - started) * 1000), "debug_screenshot": self._snapshot("send-failed")}
        deadline = time.monotonic() + (self.answer_timeout_s or 10 ** 9)   # 0 = 제한 없음 — 답이 끝날 때까지 기다린다 (사용자 지시 10-20)
        last, stable = None, 0
        while time.monotonic() < deadline:
            time.sleep(1.5)
            if self.cancelled():
                break
            try:
                self._select_chat_tab()   # 기다리는 동안 사람이 Code 탭으로 옮기면 되돌린다 — 답 감지·복사가 그 탭에서 이뤄져야 한다
            except Exception:
                pass
            if self._streaming():
                stable = 0
                continue
            current = self._page_text()
            stable = stable + 1 if current == last else 0
            last = current
            if stable >= 2 and (len(self._answer_bars()) > before or (current and current != text_before)):
                break
            if stable >= 60:   # 정지 버튼 없이 90초째 아무 변화 없음 — 빈 답으로 끝난 것
                break
        done = (stable >= 2 and (len(self._answer_bars()) > before or bool(last) and last != text_before)) or stable >= 60
        answer, capture, drafts = "", "text", 0
        if done:
            self._front()
            group = self._last_message_group()
            drafts = len(self._draft_blocks(group)) if group is not None else 0   # 기록만 — 답은 복사 아이콘 글 그대로(사용자 결정 10-19)
            try:
                copied = self._copy_last_answer()
            except CopyFailed as exc:
                return {**base, "answer": "", "status": "COPY_FAILED", "capture": "none", "error": str(exc)[:300],
                        "latency_ms": round((time.monotonic() - started) * 1000), "draft_blocks": drafts}
            except UiaChatError as exc:
                return {**base, "answer": "", "status": "ERROR", "error": str(exc)[:300], "latency_ms": round((time.monotonic() - started) * 1000),
                        "draft_blocks": drafts}
            if copied and copied.strip():
                answer, capture = copied, "copy-button"
            else:
                answer, capture = "", "empty"   # 답 말풍선은 생겼는데 복사할 글이 없다 — 빈 답
        return {**base, "answer": answer if done else "", "status": "SUCCESS" if done else "INCOMPLETE", "capture": capture, "draft_blocks": drafts,
                "error": None if done else "답변이 제한 시간 안에 끝나지 않았습니다.",
                **({} if done else {"debug_screenshot": self._snapshot("incomplete")}),
                "latency_ms": round((time.monotonic() - started) * 1000)}


# --- 창 배치 · 대조 -----------------------------------------------------------------------------------

def _squash(text: str) -> str:
    """대조용 — 공백·마크다운 표시·숫자·구두점 제거(목록 번호 '1.'·표 테두리가 화면 글에는 없다, 2026-10-20)."""
    return re.sub(r"[\s#*_`>|\-\d.,:;()\[\]~·\"'“”‘’%+=/\\!?]+", "", text or "")


def _matches_screen(copied: str, screen: str) -> bool:
    """복사한 글이 화면의 답과 같은 글인가 — 줄마다 대조해 절반 이상(최소 한 줄)이 화면 글에 있으면 같은 글로 본다.
    화면 글에는 생각 요약 등이 사이에 끼어 있어 긴 조각 대조는 목록 답에서 어긋난다(2026-10-20)."""
    if not screen:
        return True
    lines = [_squash(l) for l in (copied or "").splitlines()]
    lines = [l for l in lines if len(l) >= 4]
    if not lines:
        return bool(_squash(copied)) and _squash(copied) in screen
    found = sum(1 for l in lines if (l if len(l) <= 40 else l[:40]) in screen)
    return found >= max(1, len(lines) // 2)


def place_window(win: Any, place: str) -> None:
    """앱 창(UIA 래퍼 또는 창 핸들)을 화면 자리(PLACE_RECTS, 물리 픽셀)로 놓는다 — 동시 수집 때 창들이 서로 덮지 않게.
    Claude 수집기는 조작할 때마다 자기 창을 앞으로 올리므로 마우스 클릭이 다른 창에 가지 않고, ChatGPT·웹 쪽은 CDP 라 창이 뒤에 있어도 된다."""
    import win32api
    import win32con
    import win32gui
    from webapp.cdp_chat_web import place_rect
    hwnd = win if isinstance(win, int) else win.handle
    x, y, w, h = place_rect(place, win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1))
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.MoveWindow(hwnd, x, y, w, h, True)
    except Exception:
        pass
    time.sleep(0.8)


# --- 클립보드 -----------------------------------------------------------------------------------------

def _clipboard_set(text: str) -> None:
    import win32clipboard
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                if text:
                    win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return
        except Exception:
            time.sleep(0.2)


def _clipboard_set_files(files: List[Path]) -> None:
    """탐색기에서 파일을 '복사' 한 것과 같은 클립보드(CF_HDROP) — DROPFILES 머리 20바이트 + 경로들(UTF-16, 빈 글로 끝)."""
    import struct
    import win32clipboard
    paths = [str(Path(f).resolve()) for f in files]
    data = struct.pack("IiiII", 20, 0, 0, 0, 1) + ("\0".join(paths) + "\0\0").encode("utf-16-le")
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, data)
            finally:
                win32clipboard.CloseClipboard()
            return
        except Exception:
            time.sleep(0.2)
    raise UiaChatError("클립보드에 파일을 올리지 못했습니다.")


def _clipboard_get() -> str:
    import win32clipboard
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or ""
                return ""
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.2)
    return ""
