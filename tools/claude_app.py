"""Claude 데스크톱 앱(Store 패키지 Claude_…, Electron)을 켜고 그 창을 찾는다 — Claude 앱 수집기(webapp/uia_chat_app.py)가 쓴다.

    python tools/claude_app.py --status   # 앱 창이 있는지
    python tools/claude_app.py --launch   # 없으면 켠다(보통 실행, 로그인은 사람이 앱에서)

- 앱은 명령줄에 디버깅 스위치(--remote-debugging-port 등)가 있으면 "refusing to start" 를 내고 바로 끝난다(2026-10-19 확인).
  그래서 ChatGPT 앱처럼 CDP 로 붙지 못하고, Windows UI 자동화(UIA, 화면 낭독기가 쓰는 접근성 API)로 창을 조작한다. 우회하지 않는다.
- Claude Code CLI 도 이름이 claude.exe(…\\claude-code\\…)라, 앱 프로세스는 경로(WindowsApps\\Claude_)로만 고른다. 앱을 죽이지 않는다 —
  이 앱 안에서 Claude Code 세션이 돌고 있을 수 있다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_ALIAS = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Microsoft" / "WindowsApps" / "claude-desktop.exe"
WINDOW_CLASS = "Chrome_WidgetWin_1"
_PS_PIDS = ("Get-CimInstance Win32_Process -Filter \"name='claude.exe'\" | Where-Object { $_.ExecutablePath -like '*WindowsApps\\Claude_*' -and "
            "$_.CommandLine -notlike '*--type=*' } | ForEach-Object { $_.ProcessId }")


def app_pids() -> List[int]:
    """앱 메인 프로세스(렌더러·GPU 같은 --type 자식 제외)의 PID — 보통 하나."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command", _PS_PIDS], capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
    return [int(x) for x in out.split() if x.strip().isdigit()]


def main_window(pids: Optional[List[int]] = None) -> Any:
    """앱의 최상위 창(pywinauto UIA 래퍼) 또는 None. 창 제목은 'Claude' 로 시작한다."""
    from pywinauto import Desktop
    pids = app_pids() if pids is None else pids
    if not pids:
        return None
    for w in Desktop(backend="uia").windows(class_name=WINDOW_CLASS):
        try:
            if w.process_id() in pids and (w.window_text() or "").startswith("Claude"):
                return w
        except Exception:
            continue
    return None


def status() -> Dict[str, Any]:
    pids = app_pids()
    w = main_window(pids) if pids else None
    return {"ok": w is not None, "pids": pids, "title": (w.window_text() if w is not None else None)}


def launch(wait_s: int = 90) -> Dict[str, Any]:
    """앱 창이 없으면 켜고 창이 보일 때까지 기다린다. 이미 있으면 그대로."""
    s = status()
    if s["ok"]:
        return s
    if not APP_ALIAS.is_file():
        raise FileNotFoundError(f"Claude 앱 실행 별칭이 없습니다: {APP_ALIAS} (Store 에서 Claude 앱을 설치하세요)")
    subprocess.Popen([str(APP_ALIAS)], close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(1.5)
        s = status()
        if s["ok"]:
            return s
    return s


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    s = launch() if "--launch" in args else status()
    print(json.dumps(s, ensure_ascii=False))
    return 0 if s["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
