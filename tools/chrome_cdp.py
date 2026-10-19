"""수집용 Chrome(전용 프로필 + 원격 디버깅 포트) — Gemini·AIONA 웹 수집기가 이 Chrome 에 붙어 각자 창을 쓴다.

    python tools/chrome_cdp.py            # 수집용 Chrome 을 띄우고 Gemini·AIONA 창을 연다 (이미 떠 있으면 그대로 둠)
    python tools/chrome_cdp.py --login    # 로그인용: 평소 Chrome 그대로(자동화 없이) 같은 프로필로 띄운다 — Google 로그인은 여기서, 끝나면 창을 닫는다
    python tools/chrome_cdp.py --status   # 붙을 수 있는지(포트 응답·열린 탭)만 본다

- Chrome 155 부터 사람이 직접 띄운 Chrome 은 포트로 붙은 클라이언트에 브라우저 명령을 막는다("Browser context management is not supported", 2026-10-19).
  그래서 보조 프로세스(``--hold``)가 Playwright 로 Chrome 을 띄워 포트를 열어 두고(그 프로세스가 끝나면 Chrome 도 닫힘), 수집기들은 그 포트에 붙는다.
- 전용 프로필 폴더(``data/browser_profiles/chrome-cdp``, 저장소에 올리지 않음)에 로그인 상태가 남는다. 로그인·봇 확인은 사용자가 창에서 직접 한다.
- 수집 중에는 이 Chrome 창을 닫지 말고, 수집기가 여는 창을 건드리지 않는다.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PROFILE = ROOT / "data" / "browser_profiles" / "chrome-cdp"
PORT = 9222
CDP_URL = f"http://127.0.0.1:{PORT}"
CHROME_CANDIDATES = (Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                     Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"))
ATTACH_BLOCKED = "Browser context management is not supported"


def sites() -> tuple:
    """이 Chrome 에서 받는 두 사이트 — Gemini(오른쪽 위 창)·AIONA(오른쪽 아래 창). ChatGPT·Claude 는 데스크톱 앱으로 받는다."""
    from webapp.aiona_web import AIONA_URL
    return (("https://gemini.google.com/app", "top-right"), (f"{AIONA_URL}/chat", "bottom-right"))


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
    """떠 있지 않으면 보조 프로세스(--hold)로 띄운다. 돌려주는 값은 status()."""
    if status()["ok"]:
        return status()
    PROFILE.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--hold"] + ([] if open_sites else ["--no-sites"])
    subprocess.Popen(cmd, close_fds=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    for _ in range(45):
        time.sleep(1)
        if status()["ok"]:
            break
    return status()


def hold(open_sites: bool = True) -> int:
    """보조 프로세스 본체 — Playwright 로 수집용 프로필 Chrome 을 포트와 함께 띄우고, 창이 모두 닫힐 때까지 기다린다."""
    from playwright.sync_api import sync_playwright
    from webapp.cdp_chat_web import cdp_place_window
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=False, viewport=None,
                                                        ignore_default_args=["--enable-automation"], args=[f"--remote-debugging-port={PORT}"])
        if open_sites:
            for url, place in sites():
                page = new_window_page(context, url)   # 첫 사이트는 처음 빈 탭을 쓰고, 다음부터 새 창
                cdp_place_window(page, place)
        try:
            context.wait_for_event("close", timeout=0)
        except Exception:
            pass
    return 0


def launch_plain(urls: tuple) -> None:
    """로그인용 — 평소 Chrome 그대로(자동화·포트 없이) 같은 프로필로 띄운다. Google 로그인은 자동화 Chrome 을 거부할 수 있어 여기서 한다."""
    close_profile_chrome()
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = [str(chrome_path()), f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check", *urls]
    subprocess.Popen(args, close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def attach(playwright: Any, open_sites: bool = False) -> Any:
    """수집용 Chrome 에 Playwright 로 붙는다(없으면 띄움). 사람이 직접 띄운 Chrome(155+)이라 막히면 닫고 보조 프로세스로 다시 띄운다."""
    if not status()["ok"]:
        launch(open_sites)
    if not status()["ok"]:
        raise RuntimeError(f"수집용 Chrome 을 띄우지 못했습니다 (포트 {PORT}) — python tools/chrome_cdp.py 로 띄워 보세요.")
    try:
        return playwright.chromium.connect_over_cdp(CDP_URL)
    except Exception as exc:
        if ATTACH_BLOCKED not in str(exc):
            raise
        close_profile_chrome()
        launch(open_sites)
        return playwright.chromium.connect_over_cdp(CDP_URL)


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
    """수집용 프로필(PROFILE)로 떠 있는 Chrome 만 닫는다(사용자의 평소 Chrome 은 건드리지 않음) — Playwright 가 같은 프로필로 직접 띄우기 전에."""
    key = str(PROFILE)   # -like 에서 역슬래시는 그대로(이스케이프 아님)
    cmd = (f"Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | Where-Object {{ $_.CommandLine -like '*{key}*' -and $_.CommandLine -notlike '*--type=*' }}"
           " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, encoding="utf-8", timeout=60).stdout
    n = len([x for x in out.split() if x.strip().isdigit()])
    if n:
        time.sleep(2)
    return n


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--hold" in args:
        return hold(open_sites="--no-sites" not in args)
    if "--login" in args:
        urls = tuple(u for u, _ in sites())
        launch_plain(urls)
        print("로그인용 Chrome 을 띄웠습니다 — Gemini(bora3159 계정)·AIONA 에 로그인한 뒤 창을 닫아 주세요. 수집할 때는 python tools/chrome_cdp.py 로 다시 띄웁니다.")
        return 0
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
