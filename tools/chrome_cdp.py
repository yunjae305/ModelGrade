"""평소 Chrome 을 '수집용 프로필 + 원격 디버깅 포트' 로 띄운다 — 웹 채팅 수집기(ChatGPT·Gemini·Claude)가 이 Chrome 에 붙어 탭을 연다.

    python tools/chrome_cdp.py            # Chrome 을 띄우고 세 사이트 탭을 연다 (이미 떠 있으면 그대로 둠)
    python tools/chrome_cdp.py --status   # 붙을 수 있는지(포트 응답·열린 탭)만 본다

- 자동화 플래그(--no-sandbox·--enable-automation) 없이 평소 Chrome 그대로 실행한다. 로그인·봇 확인은 사용자가 그 창에서 직접 한다.
  프로그램은 자격 증명을 다루지 않고, 봇 확인을 우회하지 않는다.
- Chrome 136 이상은 기본 프로필에 디버깅 접속을 막으므로 전용 프로필 폴더(``data/browser_profiles/chrome-cdp``, 저장소에 올리지 않음)를 쓴다.
  로그인 상태는 그 폴더에 남아 다음 실행에도 유지된다.
- 수집 중에는 이 Chrome 창을 닫지 말고, 수집기가 여는 탭을 건드리지 않는다.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "data" / "browser_profiles" / "chrome-cdp"
PORT = 9222
CDP_URL = f"http://127.0.0.1:{PORT}"
CHROME_CANDIDATES = (Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                     Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"))
SITES = ("https://chatgpt.com/", "https://gemini.google.com/app", "https://claude.ai/new")


def chrome_path() -> Path:
    for path in CHROME_CANDIDATES:
        if path.is_file():
            return path
    raise FileNotFoundError("Chrome 을 찾지 못했습니다 (Program Files 아래 chrome.exe).")


def status(timeout: float = 2.0) -> dict:
    """포트가 응답하면 {"ok": True, "tabs": [...]} — 아니면 {"ok": False}."""
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=timeout) as resp:
            tabs = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "tabs": [{"title": t.get("title", "")[:60], "url": t.get("url", "")[:100]} for t in tabs if t.get("type") == "page"]}
    except Exception:
        return {"ok": False, "tabs": []}


def launch(open_sites: bool = True) -> dict:
    """떠 있지 않으면 띄운다. 돌려주는 값은 status()."""
    if status()["ok"]:
        return status()
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = [str(chrome_path()), f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check"]
    if open_sites:
        args += list(SITES)
    subprocess.Popen(args, close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    for _ in range(30):
        time.sleep(1)
        if status()["ok"]:
            break
    return status()


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    result = status() if "--status" in args else launch()
    if not result["ok"]:
        print(f"Chrome 디버깅 포트({PORT})에 붙지 못했습니다. 다른 Chrome 창이 이 프로필을 쓰고 있지 않은지 확인하세요.")
        return 1
    print(f"붙을 수 있음 — 열린 탭 {len(result['tabs'])}개")
    for tab in result["tabs"]:
        print(f"  - {tab['title']} | {tab['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
