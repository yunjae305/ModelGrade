"""앱 수집기끼리 클립보드 · 키보드 · 마우스 · 창 앞으로 가져오기를 한 번에 하나만 쓰게 하는 프로세스 간 잠금.

10-21 밤: 네 모델을 동시에 받는 동안 GPT 앱이 복사한 답이 클립보드를 덮었고, 포커스를 못 잡은 Claude · Gemini 앱 수집기의
Ctrl+V 가 사용자 창에 그 글을 붙였다. 클립보드에 넣고 → 창을 앞으로 → 키를 보내고 → 확인하는 동안은 이 잠금을 쥔다.
Windows 이름 있는 뮤텍스라 프로세스가 죽으면 Windows 가 풀어 주고, 같은 스레드에서 겹쳐 잡아도 된다.
"""
from __future__ import annotations

import contextlib
from typing import Iterator

MUTEX_NAME = "Local\\ModelGradeAppInput"


class InputTurnTimeout(RuntimeError):
    pass


@contextlib.contextmanager
def input_turn(timeout_s: float = 600.0) -> Iterator[None]:
    import win32api
    import win32event
    handle = win32event.CreateMutex(None, False, MUTEX_NAME)
    try:
        got = win32event.WaitForSingleObject(handle, int(timeout_s * 1000))
        if got not in (win32event.WAIT_OBJECT_0, 0x80):   # 0x80 = WAIT_ABANDONED(잡고 있던 프로세스가 죽음) — 이제 우리 것
            raise InputTurnTimeout(f"입력 차례를 {timeout_s:.0f}초 안에 얻지 못했습니다(다른 수집기가 붙잡고 있음).")
        try:
            yield
        finally:
            win32event.ReleaseMutex(handle)
    finally:
        win32api.CloseHandle(handle)
