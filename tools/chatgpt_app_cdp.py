"""ChatGPT 데스크톱 앱(OpenAI.Codex 패키지, Electron)을 '원격 디버깅 포트' 로 띄운다 — 앱 수집기(webapp.cdp_chat_web.ChatGptAppChat)가 붙는다.

    python tools/chatgpt_app_cdp.py            # 포트가 안 열려 있으면 앱을 띄운다 (이미 떠 있는데 포트가 없으면 --restart 필요)
    python tools/chatgpt_app_cdp.py --restart  # 떠 있는 앱을 닫고 포트를 열어 다시 띄운다 (앱 창이 잠깐 닫힌다)
    python tools/chatgpt_app_cdp.py --status   # 붙을 수 있는지만 본다

- 2026-10-19 확인: 앱 메인 페이지(app://-/index.html)의 ChatGPT 모드 화면은 chatgpt.com 웹과 같은 구조(입력칸 'ChatGPT에게 물어보세요',
  말풍선 data-turn-key, 복사 버튼, 파일 input '파일 첨부', 모델 메뉴 'ChatGPT 모델 선택' → GPT-6 · Medium).
- 로그인은 사람이 앱에서. 프로그램은 자격 증명을 다루지 않는다. 포트는 `.env` CHATGPT_APP_CDP_PORT(기본 9334).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAIN_URL = "app://-/index.html"


def port() -> int:
    from code_eval.llm_client import env_value
    return int(env_value("CHATGPT_APP_CDP_PORT") or 9334)


def cdp_url() -> str:
    return f"http://127.0.0.1:{port()}"


def status(timeout: float = 2.0) -> dict:
    try:
        with urllib.request.urlopen(f"{cdp_url()}/json/list", timeout=timeout) as resp:
            tabs = json.loads(resp.read().decode("utf-8"))
        pages = [t for t in tabs if t.get("type") == "page"]
        return {"ok": any(t.get("url") == MAIN_URL for t in pages), "pages": [t.get("url", "")[:80] for t in pages]}
    except Exception:
        return {"ok": False, "pages": []}


def app_exe() -> Path:
    """Appx 패키지(OpenAI.Codex)의 app/ChatGPT.exe — PowerShell 로 설치 위치를 묻는다."""
    cmd = ["powershell", "-NoProfile", "-Command", "(Get-AppxPackage -Name 'OpenAI.Codex' | Select-Object -First 1).InstallLocation"]
    loc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30).stdout.strip()
    exe = Path(loc) / "app" / "ChatGPT.exe" if loc else None
    if not exe or not exe.is_file():
        raise FileNotFoundError("ChatGPT 앱(OpenAI.Codex 패키지)을 찾지 못했습니다 — Microsoft Store 에서 ChatGPT 를 설치하세요.")
    return exe


def app_pids() -> list:
    """앱 메인 프로세스(--type 자식 제외) PID."""
    cmd = ("Get-CimInstance Win32_Process -Filter \"name='ChatGPT.exe'\" | Where-Object { $_.CommandLine -notlike '*--type=*' } | ForEach-Object { $_.ProcessId }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
    return [int(x) for x in out.split() if x.strip().isdigit()]


def main_hwnd(visible_only: bool = True) -> int:
    """앱 메인 창(제목 있는 Chrome_WidgetWin_1) 핸들 — 보이는 것 우선, 없으면 0."""
    import win32gui
    import win32process
    pids = set(app_pids())
    found = []

    def cb(h, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(h)
            if pid in pids and win32gui.GetClassName(h) == "Chrome_WidgetWin_1" and win32gui.GetWindowText(h):
                found.append((win32gui.IsWindowVisible(h), h))
        except Exception:
            pass
    win32gui.EnumWindows(cb, None)
    vis = [h for v, h in found if v]
    if vis:
        return vis[0]
    return 0 if visible_only or not found else found[0][1]


def show_window(wait_s: int = 15) -> int:
    """앱이 창 없이(트레이) 떠 있으면 실행 파일을 한 번 더 실행한다 — 단일 인스턴스라 새 프로세스는 바로 끝나고 떠 있던 앱이 창을 보여 준다(2026-10-19 확인, 포트 유지)."""
    h = main_hwnd()
    if h:
        return h
    subprocess.Popen([str(app_exe())], close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(1.0)
        h = main_hwnd()
        if h:
            return h
    return 0


def running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ChatGPT.exe"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    return "ChatGPT.exe" in out


def launch(restart: bool = False) -> dict:
    if status()["ok"]:
        return status()
    if running():
        if not restart:
            raise RuntimeError("ChatGPT 앱이 포트 없이 떠 있습니다 — 앱을 닫거나 --restart 로 다시 띄우세요.")
        subprocess.run(["taskkill", "/IM", "ChatGPT.exe", "/F"], capture_output=True)
        time.sleep(2)
    subprocess.Popen([str(app_exe()), f"--remote-debugging-port={port()}"], close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    for _ in range(40):
        time.sleep(1)
        if status()["ok"]:
            break
    return status()


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--status" in args:
        s = status(); print(json.dumps(s, ensure_ascii=False)); return 0 if s["ok"] else 1
    s = launch(restart="--restart" in args)
    print(json.dumps(s, ensure_ascii=False))
    return 0 if s["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
