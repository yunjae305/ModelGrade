"""Gemini 데스크톱 앱(Google, Electron — %LOCALAPPDATA%\\Google\\Gemini\\app-<버전>\\Gemini.exe)을 켜고 그 창을 찾는다 — Gemini 앱 수집기(webapp/gemini_app_chat.py)가 쓴다.

    python tools/gemini_app.py --status     # 앱 창이 있는지, '가려져도 그리기' 옵션으로 켜졌는지
    python tools/gemini_app.py --launch     # 없으면 옵션을 붙여 켠다(로그인은 사람이 앱에서)
    python tools/gemini_app.py --relaunch   # 옵션 없이 켜져 있으면 닫고 옵션을 붙여 다시 켠다

- Claude 앱과 같이 Windows UI 자동화(UIA)로 창을 조작한다(2026-10-21: 사용자가 Gemini 를 웹에서 데스크톱 앱으로 바꿈). 디버깅 스위치는 쓰지 않는다.
- 앱 메인 프로세스는 경로(Google\\Gemini\\)로 고르고 --type= 자식(렌더러·GPU)은 뺀다.
- 앱은 '가려져도 계속 그리기' 옵션(FLAGS)으로 켠다 — Chromium 은 다른 창에 완전히 가려지거나 최소화된 창을 그리지 않아, 답이 화면(UIA)에 늦게
  나타나거나 아예 안 나타난다(2026-10-21 밤: 가려진 채 2시간 멈춤 · 짧은 질문에 160초). 옵션을 주면 Claude 앱이 화면을 다 덮어도 3초에 보였다.
  옵션 없이 켜져 있으면 relaunch() 가 앱을 닫고(로그인 · 채팅 기록은 그대로) 옵션을 붙여 다시 켠다.
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

APP_EXE = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Google" / "Gemini" / "Gemini.exe"   # 런처(옵션을 넘기지 않음)
WINDOW_CLASS = "Chrome_WidgetWin_1"
FLAGS = ["--disable-features=CalculateNativeWinOcclusion", "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding"]
_PS_PIDS = ("Get-CimInstance Win32_Process -Filter \"name='Gemini.exe'\" | Where-Object { $_.ExecutablePath -like '*Google\\Gemini\\*' -and "
            "$_.CommandLine -notlike '*--type=*' } | ForEach-Object { $_.ProcessId }")


def app_pids() -> List[int]:
    """앱 메인 프로세스(--type 자식 제외)의 PID — 보통 하나."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command", _PS_PIDS], capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
    return [int(x) for x in out.split() if x.strip().isdigit()]


def app_bin() -> Path:
    """실제 앱 실행 파일(app-<버전>\\Gemini.exe) — 맨 위 Gemini.exe 는 옵션을 넘기지 않는 런처라 버전 폴더의 것을 직접 켠다."""
    dirs = sorted(APP_EXE.parent.glob("app-*"), key=lambda d: [int(x) if x.isdigit() else 0 for x in d.name[4:].split(".")])
    return (dirs[-1] / "Gemini.exe") if dirs else APP_EXE


def flags_ok(pids: List[int]) -> bool:
    """메인 프로세스 명령줄에 FLAGS 가 모두 있는가."""
    if not pids:
        return False
    ps = f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pids[0]}\").CommandLine"
    cmd = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
    return all(f in cmd for f in FLAGS)


def main_window(pids: Optional[List[int]] = None) -> Any:
    """앱의 최상위 창(pywinauto UIA 래퍼) 또는 None. 창 제목은 'Gemini'."""
    from pywinauto import Desktop
    pids = app_pids() if pids is None else pids
    if not pids:
        return None
    for w in Desktop(backend="uia").windows(class_name=WINDOW_CLASS):
        try:
            if w.process_id() in pids and (w.window_text() or "").startswith("Gemini"):
                return w
        except Exception:
            continue
    return None


def status() -> Dict[str, Any]:
    pids = app_pids()
    w = main_window(pids) if pids else None
    return {"ok": w is not None, "pids": pids, "title": (w.window_text() if w is not None else None), "exe": str(app_bin()),
            "installed": app_bin().is_file(), "flags_ok": flags_ok(pids)}


def launch(wait_s: int = 90) -> Dict[str, Any]:
    """앱 창이 없으면 옵션을 붙여 켜고 창이 보일 때까지 기다린다. 이미 있으면 그대로(옵션 확인은 relaunch)."""
    s = status()
    if s["ok"]:
        return s
    if not app_bin().is_file():
        return {**s, "error": f"Gemini 앱이 없습니다: {app_bin()}"}
    subprocess.Popen([str(app_bin()), *FLAGS], close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(2)
        s = status()
        if s["ok"]:
            return s
    return {**s, "error": "앱 창이 제때 뜨지 않음"}


def relaunch(wait_s: int = 90) -> Dict[str, Any]:
    """옵션 없이 켜진 앱을 닫고 FLAGS 를 붙여 다시 켠다(정상 종료 요청 → 15초 안에 안 닫히면 강제). 이미 옵션이 있으면 그대로."""
    s = status()
    if s["ok"] and s["flags_ok"]:
        return s
    for pid in s["pids"]:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True)
    deadline = time.monotonic() + 15
    while app_pids() and time.monotonic() < deadline:
        time.sleep(1)
    for pid in app_pids():
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    time.sleep(2)
    return launch(wait_s)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--launch", action="store_true")
    ap.add_argument("--relaunch", action="store_true", help="옵션 없이 켜져 있으면 닫고 '가려져도 그리기' 옵션으로 다시 켠다")
    args = ap.parse_args(argv)
    print(json.dumps(relaunch() if args.relaunch else launch() if args.launch else status(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
