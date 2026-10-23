"""수집용 Chrome — 평소 Chrome 을 자동화 표시 없이 전용 프로필 + 디버깅 포트로 띄우고, 네 사이트 수집기가 포트로 붙어 각자 창을 쓴다.

    python tools/chrome_cdp.py            # 수집용 Chrome 을 띄우고 네 사이트(ChatGPT · Claude · Gemini · AIONA) 창을 연다 (떠 있으면 그대로)
    python tools/chrome_cdp.py --login    # 같은 것 + 로그인 안내 — 사이트 로그인은 사람이 그 창에서 한 번 한다(프로필에 남음)
    python tools/chrome_cdp.py --status   # 붙을 수 있는지(포트 응답 · 열린 탭)만 본다
    python tools/chrome_cdp.py --close    # 이 프로필로 떠 있는 Chrome 만 닫는다(평소 Chrome 은 건드리지 않음)

- 왜 평소 Chrome 인가(2026-10-23 시험): Playwright 가 띄운 Chrome 은 claude.ai 의 Cloudflare 확인을 60초 넘게 못 넘긴다. 평소 Chrome 을
  그대로 띄우고 포트로만 붙으면 8초 안에 통과하고, 붙은 뒤에도 다시 막히지 않는다. 입력은 포트(CDP)로 넣고 복사 아이콘 글은 페이지
  안에서 가로채므로 포커스 · 시스템 클립보드를 쓰지 않는다 → 수집 중에도 컴퓨터를 쓸 수 있고 Windows · macOS 같은 코드.
- 가려진 · 뒤에 있는 창도 그리도록 띄운다(--disable-backgrounding-occluded-windows 등) — 수집기가 창을 앞으로 끌어오지 않는다.
- 전용 프로필 폴더(``data/browser_profiles/web-chat``, 저장소에 올리지 않음)에 로그인 상태가 남는다. 로그인 · 봇 확인은 사용자가 창에서
  직접 한다 — 프로그램은 비밀번호를 다루지 않고 봇 확인을 우회하지 않는다. 수집 중에는 이 Chrome 을 닫지 않는다.
- Chrome 위치: .env 의 CHROME_PATH, 없으면 운영체제별 기본 설치 위치 · PATH.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PROFILE = ROOT / "data" / "browser_profiles" / "web-chat"
PORT = int(os.environ.get("CHROME_CDP_PORT") or 9222)
CDP_URL = f"http://127.0.0.1:{PORT}"
ATTACH_BLOCKED = "Browser context management is not supported"
#: 뒤 · 가려진 · 최소화된 창에서도 그리기 · 타이머가 멈추지 않게 — 수집기가 창을 앞으로 가져오지 않아도 메뉴 · 복사가 된다
RENDER_FLAGS = ("--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
                "--no-first-run", "--no-default-browser-check")
WINDOWS_CANDIDATES = (Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                      Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                      Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe")
MAC_CANDIDATES = (Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                  Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome")
LINUX_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def sites() -> tuple:
    """이 Chrome 에서 받는 네 사이트와 창 자리(webapp.cdp_chat_web.PLACE_RECTS) — 수집기마다 자기 창."""
    from webapp.aiona_web import AIONA_URL
    return (("https://chatgpt.com/", "right"), ("https://claude.ai/new", "left"),
            ("https://gemini.google.com/app", "top-right"), (f"{AIONA_URL}/chat", "bottom-right"))


def chrome_path() -> Path:
    """Chrome 실행 파일 — .env CHROME_PATH → 운영체제별 기본 위치 → PATH."""
    from code_eval.llm_client import env_value
    custom = env_value("CHROME_PATH")
    if custom and Path(custom).is_file():
        return Path(custom)
    system = platform.system()
    candidates = WINDOWS_CANDIDATES if system == "Windows" else MAC_CANDIDATES if system == "Darwin" else ()
    for path in candidates:
        if path.is_file():
            return path
    for name in (("chrome", "chrome.exe") if system == "Windows" else LINUX_NAMES + ("google-chrome",)):
        found = shutil.which(name)
        if found:
            return Path(found)
    raise FileNotFoundError("Chrome 을 찾지 못했습니다 — .env 에 CHROME_PATH=<chrome 실행 파일 경로> 를 넣으세요.")


def status(timeout: float = 2.0) -> dict:
    """포트가 응답하면 {"ok": True, "tabs": [...]} — 아니면 {"ok": False}."""
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=timeout) as resp:
            tabs = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "tabs": [{"title": t.get("title", "")[:60], "url": t.get("url", "")[:100]} for t in tabs if t.get("type") == "page"]}
    except Exception:
        return {"ok": False, "tabs": []}


def _spawn_detached(cmd: list) -> None:
    kwargs: dict = {"close_fds": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() == "Windows":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def profile_chrome_pids() -> list:
    """이 프로필로 떠 있는 Chrome 주 프로세스 — 포트 없이 떠 있으면 새로 띄워도 빈 탭만 열리므로(Chrome 단일 실행) 먼저 확인한다."""
    key = str(PROFILE)
    try:
        if platform.system() == "Windows":
            cmd = (f"Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | Where-Object {{ $_.CommandLine -like '*{key}*' -and $_.CommandLine -notlike '*--type=*' }}"
                   " | ForEach-Object { $_.ProcessId }")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, encoding="utf-8", timeout=60).stdout
        else:
            out = subprocess.run(["pgrep", "-f", f"user-data-dir={key}"], capture_output=True, text=True).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def launch(open_sites: bool = True) -> dict:
    """떠 있지 않으면 평소 Chrome 을 전용 프로필 · 포트로 띄운다(자동화 스위치 없음). 돌려주는 값은 status()."""
    if status()["ok"]:
        if open_sites:
            open_site_windows()
        return status()
    if profile_chrome_pids():
        raise RuntimeError(f"이 프로필의 Chrome 이 포트 없이 이미 떠 있습니다 — 그 창을 닫거나 python tools/chrome_cdp.py --close 뒤 다시 하세요 (프로필 {PROFILE})")
    PROFILE.mkdir(parents=True, exist_ok=True)
    _spawn_detached([str(chrome_path()), f"--user-data-dir={PROFILE}", f"--remote-debugging-port={PORT}", *RENDER_FLAGS, "about:blank"])
    for _ in range(45):
        time.sleep(1)
        if status()["ok"]:
            break
    if status()["ok"] and open_sites:
        open_site_windows()
    return status()


def open_site_windows() -> None:
    """네 사이트가 각자 창으로 열려 있게 한다(이미 열린 사이트는 그대로) — 창 자리는 수집기가 맞춘다."""
    from playwright.sync_api import sync_playwright
    from webapp.cdp_chat_web import cdp_place_window
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
        try:
            for url, place in sites():
                host = url.split("//", 1)[-1].split("/", 1)[0]
                context = browser.contexts[0]
                had = any(host in pg.url for pg in context.pages if not pg.is_closed())
                page = find_or_open(browser, host, url)
                if not had:
                    cdp_place_window(page, place)
            close_blank_pages(browser)
        finally:
            browser.close()   # 연결만 끊는다 — Chrome 은 그대로


def attach(playwright: Any, open_sites: bool = False) -> Any:
    """수집용 Chrome 에 Playwright 로 붙는다(없으면 띄움)."""
    if not status()["ok"]:
        launch(open_sites)
    if not status()["ok"]:
        raise RuntimeError(f"수집용 Chrome 을 띄우지 못했습니다 (포트 {PORT}) — python tools/chrome_cdp.py 로 띄워 보세요.")
    try:
        return playwright.chromium.connect_over_cdp(CDP_URL)
    except Exception as exc:
        if ATTACH_BLOCKED in str(exc):
            raise RuntimeError("이 Chrome 은 포트로 붙은 프로그램의 브라우저 명령을 막습니다 — python tools/chrome_cdp.py --close 로 닫고 다시 띄우세요.") from exc
        raise


def goto_tolerant(page: Any, url: str) -> None:
    """이동 — 사이트가 바로 다른 주소로 바꿔 'net::ERR_ABORTED' 가 나면(Gemini) 그대로 두고 로드만 기다린다."""
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:
        if "ERR_ABORTED" not in str(exc):
            raise
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass


def new_window_page(context: Any, url: str) -> Any:
    """같은 프로필에서 새 '창'(탭이 아니라)을 연다 — 수집기마다 자기 창을 쓰면 앞 탭을 서로 뺏지 않는다.
    빈 탭(about:blank)이 이미 있으면 새 창 대신 그것을 쓴다(빈 창 찌꺼기 방지, 2026-10-19)."""
    blank = next((pg for pg in context.pages if pg.url in ("", "about:blank") and not pg.is_closed()), None)
    if blank is not None:
        page = blank
    else:
        base = context.pages[0] if context.pages else context.new_page()
        try:
            with context.expect_page(timeout=10_000) as info:
                session = context.new_cdp_session(base)
                session.send("Target.createTarget", {"url": "about:blank", "newWindow": True})
                session.detach()
            page = info.value
        except Exception:
            page = context.new_page()
    if url and url != "about:blank":
        goto_tolerant(page, url)
    return page


def find_or_open(browser: Any, host: str, url: str) -> Any:
    """붙은 Chrome 에서 그 사이트 페이지를 찾고, 없으면 (빈 탭 또는) 새 창으로 연다."""
    context = browser.contexts[0]
    page = next((pg for pg in context.pages if host in pg.url and not pg.is_closed()), None)
    return page or new_window_page(context, url)


def close_blank_pages(browser: Any) -> int:
    """남은 빈 탭(about:blank)을 닫는다 — 마지막 하나는 남긴다(모두 닫히면 Chrome 이 끝난다)."""
    context = browser.contexts[0]
    blanks = [pg for pg in context.pages if pg.url in ("", "about:blank") and not pg.is_closed()]
    keep = 1 if len(blanks) == len(context.pages) else 0
    n = 0
    for pg in blanks[keep:]:
        try:
            pg.close()
            n += 1
        except Exception:
            pass
    return n


def close_profile_chrome() -> int:
    """수집용 프로필(PROFILE)로 떠 있는 Chrome 만 닫는다(사용자의 평소 Chrome 은 건드리지 않음)."""
    key = str(PROFILE)
    if platform.system() == "Windows":
        cmd = (f"Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | Where-Object {{ $_.CommandLine -like '*{key}*' -and $_.CommandLine -notlike '*--type=*' }}"
               " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, encoding="utf-8", timeout=60).stdout
        n = len([x for x in out.split() if x.strip().isdigit()])
    else:
        out = subprocess.run(["pgrep", "-f", f"user-data-dir={key}"], capture_output=True, text=True).stdout
        pids = [x for x in out.split() if x.strip().isdigit()]
        subprocess.run(["pkill", "-f", f"user-data-dir={key}"], capture_output=True)
        n = len(pids)
    if n:
        time.sleep(2)
    return n


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--close" in args:
        print(f"닫은 Chrome 프로세스: {close_profile_chrome()}개")
        return 0
    result = status() if "--status" in args else launch()
    if not result["ok"]:
        print(f"Chrome 디버깅 포트({PORT})에 붙지 못했습니다. 이 프로필을 쓰는 다른 Chrome 창이 떠 있으면 --close 로 닫고 다시 하세요.")
        return 1
    print(f"붙을 수 있음 — 열린 탭 {len(result['tabs'])}개")
    for tab in result["tabs"]:
        print(f"  - {tab['title']} | {tab['url']}")
    if "--login" in args:
        print("로그인: 뜬 Chrome 창에서 ChatGPT · Claude · Gemini 에 직접 로그인하세요(Chrome 에 구글 계정을 넣어 두면 'Google로 계속하기' 한 번).\n"
              "AIONA 는 .env 의 AIONA_LOGIN_ID · AIONA_LOGIN_PASSWORD 로 수집기가 로그인합니다. 로그인 상태는 프로필 폴더에 남습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
