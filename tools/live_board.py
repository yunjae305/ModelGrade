"""진행 판 — 모델마다 한 줄, 그 자리에서 숫자만 바뀐다 (사용자 10-26 저녁: 수집 · 평가가 지금 몇 번째인지 보기 좋게).

      AIONA   :  12 / 78  (남은 66) · Q13 받는 중
      ChatGPT :  10 / 78  (남은 68) · Q11 받는 중

터미널이면 커서를 올려 판을 다시 그리고(Windows 는 VT 처리를 켬), 터미널이 아니면(로그 · CI) 바뀐 줄만 한 줄씩 찍는다.
판이 떠 있는 동안 다른 글(경고 등)은 log() 로 — 판을 지우고 글을 찍은 뒤 판을 다시 그린다.
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Dict, List, Optional, TextIO


def text_width(text: str) -> int:
    """터미널 칸 수 — 한글 같은 넓은 글자는 2칸."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def enable_ansi(stream: TextIO) -> bool:
    """커서 이동을 쓸 수 있는가 — 터미널이고, Windows 면 VT 처리를 켤 수 있을 때."""
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)   # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


class LiveBoard:
    """키(모델)마다 '보이는 이름 : 한 수 / 전체 (남은 n) · 메모' 한 줄."""

    def __init__(self, labels: Dict[str, str], totals: Dict[str, int], stream: Optional[TextIO] = None) -> None:
        self.keys: List[str] = list(labels)
        self.labels = labels
        self.totals = {k: int(totals.get(k) or 0) for k in self.keys}
        self.done: Dict[str, int] = {k: 0 for k in self.keys}
        self.note: Dict[str, str] = {k: "대기" for k in self.keys}
        self.stream = stream or sys.stdout
        self.ansi = enable_ansi(self.stream)
        self.width = max([text_width(v) for v in labels.values()] + [1])
        self.lock = threading.Lock()
        self.drawn = 0
        self.last: Dict[str, str] = {}

    def line(self, key: str) -> str:
        total, done = self.totals[key], min(self.done[key], self.totals[key]) if self.totals[key] else self.done[key]
        name = self.labels[key] + " " * (self.width - text_width(self.labels[key]))
        digits = len(str(total or done or 0))
        return (f"  {name} : {done:>{digits}} / {total}  (남은 {max(0, total - done)})" + (f" · {self.note[key]}" if self.note[key] else ""))

    def _fit(self, text: str) -> str:
        """터미널 폭에 맞게 자르기 — 줄이 접히면 커서를 올리는 수가 어긋나 판이 깨진다."""
        import shutil
        cols = shutil.get_terminal_size((100, 20)).columns - 2
        if text_width(text) <= cols:
            return text
        out, w = "", 0
        for ch in text:
            if w + text_width(ch) > cols - 1:
                break
            out += ch
            w += text_width(ch)
        return out + "…"

    def _draw(self) -> None:
        if self.ansi:
            out = (f"\x1b[{self.drawn}F" if self.drawn else "") + "".join(f"\x1b[2K{self._fit(self.line(k))}\n" for k in self.keys)
            self.stream.write(out)
            self.drawn = len(self.keys)
        else:   # 터미널이 아니면 바뀐 줄만
            for k in self.keys:
                text = self.line(k)
                if self.last.get(k) != text:
                    self.stream.write(text + "\n")
                    self.last[k] = text
        self.stream.flush()

    def update(self, key: str, done: Optional[int] = None, total: Optional[int] = None, note: Optional[str] = None, add: int = 0) -> None:
        """한 줄 바꾸기 — done(값으로) · add(더하기) · total · note."""
        if key not in self.done:
            return
        with self.lock:
            if total is not None:
                self.totals[key] = int(total)
            if done is not None:
                self.done[key] = int(done)
            self.done[key] += add
            if note is not None:
                self.note[key] = note
            self._draw()

    def log(self, text: str) -> None:
        """판 위에 글 한 줄 — 판을 지우고 글을 찍은 뒤 다시 그린다."""
        with self.lock:
            if self.ansi and self.drawn:
                self.stream.write(f"\x1b[{self.drawn}F" + "\x1b[2K\n" * self.drawn + f"\x1b[{self.drawn}F")
                self.drawn = 0
            self.stream.write(text.rstrip() + "\n")
            self._draw()

    def start(self) -> None:
        with self.lock:
            self._draw()

    def close(self) -> None:
        with self.lock:
            self.last.clear() if not self.ansi else None
            self._draw()
