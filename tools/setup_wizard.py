"""처음 설정 — `modelgrade setup` 한 번으로 .env 키 · Codex CLI · Claude CLI · 웹 네 사이트 로그인 · 환경 점검까지 (사용자 10-26 저녁:
"간단한 명령어들로 전부 설치가 되고 각종 ai 로그인들도 전부 되게"). Windows · macOS 같은 흐름. 키 · 비밀번호는 화면 · 로그에 쓰지 않는다.

    modelgrade setup            # 모두 — 처음 한 번
    modelgrade setup --keys     # .env 키만 (이미 있는 키도 바꿀지 물음)
    modelgrade setup --cli      # Codex · Claude CLI 로그인만
    modelgrade setup --web      # 수집용 Chrome 에서 ChatGPT · Claude · Gemini · AIONA 로그인만 (modelgrade login 과 같음)

터미널이 아니면(CI · 파이프) 묻지 않고 .env 만 만든 뒤 남은 일을 알려 준다.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV, EXAMPLE = ROOT / ".env", ROOT / ".env.example"

#: setup 이 묻는 키 — (이름, 숨겨 받기, 필수, 무엇에 쓰나, 어디서 받나). 나머지 설정은 .env.example 의 주석을 보고 직접 고친다
KEYS: Tuple[Tuple[str, bool, bool, str, str], ...] = (
    ("OPENAI_API_KEY", True, True, "OpenAI API 키 — 1차 심판(Decisions)과 질문 생성", "https://platform.openai.com/api-keys"),
    ("TYPESAFE_API_KEY", True, False, "Jev(TypeSafe) API 키 — 작은 질문의 1차 심판. 비우면 Decisions 가 모두 판정", "TypeSafe(Jev) 에서 받은 키"),
    ("AIONA_LOGIN_ID", False, False, "AIONA 아이디 — 로그인이 풀리면 수집기가 다시 로그인", "AIONA 계정"),
    ("AIONA_LOGIN_PASSWORD", True, False, "AIONA 비밀번호", "AIONA 계정"),
)

#: 웹 로그인 화면이 남았는지 — 주소가 로그인 · 계정 화면이거나, 눈에 보이는 단추 · 링크 글이 '로그인 · 가입'뿐인 것 (로그인하면 사라짐)
LOGIN_WALL_JS = r"""() => {
  const u = location.href.toLowerCase();
  if (/(\/login|\/signin|\/sign-in|\/auth\b|accounts\.google\.com|\/oauth)/.test(u)) return 'login';
  const words = /^(로그인|log in|login|sign in|sign up|sign up for free|회원가입|무료로 가입하기)$/i;
  const seen = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  return Array.from(document.querySelectorAll('a, button')).some(e => seen(e) && words.test((e.innerText || '').trim())) ? 'login' : 'ok';
}"""


def interactive() -> bool:
    """묻고 답할 수 있는 터미널인가 — CI · 파이프 · MODELGRADE_NONINTERACTIVE 면 아니다."""
    if os.environ.get("MODELGRADE_NONINTERACTIVE") or os.environ.get("CI"):
        return False
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def ask_yes(question: str, default: bool = True) -> bool:
    got = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not got else got in ("y", "yes", "ㅛ", "네", "예")


# ------------------------------------------------------------------ .env ------------------------------------------------------------------
def ensure_env() -> bool:
    """.env 가 없으면 .env.example 을 복사 — 새로 만들었으면 True."""
    if ENV.exists():
        return False
    shutil.copyfile(EXAMPLE, ENV)
    return True


def read_env(path: Optional[Path] = None) -> Dict[str, str]:
    """.env 의 값(주석 · 빈 줄 제외) — 값은 화면에 쓰지 않는다."""
    path = path or ENV
    out: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if line.strip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return out


def write_env(values: Dict[str, str], path: Optional[Path] = None) -> None:
    """키마다 첫 'KEY=' 줄(주석이 아닌)을 바꾸고, 없으면 끝에 붙인다 — 주석 · 순서 · 다른 키는 그대로."""
    path = path or ENV
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    for key, value in values.items():
        if re.search(r"[\r\n]", value):
            raise ValueError(f"{key} 값에 줄바꿈이 있습니다")
        hit = next((i for i, l in enumerate(lines) if not l.lstrip().startswith("#") and l.partition("=")[0].strip() == key and "=" in l), None)
        if hit is None:
            lines.append(f"{key}={value}")
        else:
            lines[hit] = f"{key}={value}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def setup_keys(ask_again: bool = False, reader: Callable[[str, bool], str] = None) -> List[str]:
    """KEYS 를 하나씩 묻는다 — 비어 있는 키만(ask_again 이면 있는 키도 바꿀지 물음). 빈 입력은 건너뜀. 돌려주는 값: 아직 비어 있는 필수 키."""
    reader = reader or (lambda prompt, secret: getpass.getpass(prompt) if secret else input(prompt))
    have = read_env()
    new: Dict[str, str] = {}
    for key, secret, required, what, where in KEYS:
        mark = "필수" if required else "선택"
        if have.get(key) and not (ask_again and ask_yes(f"  ✅ {key} 있음 — 바꿀까요?", default=False)):
            if not ask_again:
                print(f"  ✅ {key} 있음")
            continue
        print(f"  · {key} ({mark}) — {what}" + (f"\n    받는 곳: {where}" if where else ""))
        value = reader("    값 (" + ("입력이 화면에 보이지 않음, " if secret else "") + "비우면 건너뜀): ", secret).strip()
        if value:
            new[key] = value
    if new:
        write_env(new)
        print(f"  .env 에 {len(new)}개 저장 — " + ", ".join(new))
    now = read_env()
    return [k for k, _s, req, _w, _h in KEYS if req and not now.get(k)]


# ------------------------------------------------------------------ CLI 로그인 ------------------------------------------------------------------
def _run(cmd: List[str], capture: bool = True, timeout: Optional[int] = 30) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=capture, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "") if capture else ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, type(exc).__name__


def codex_logged_in(exe: str) -> bool:
    code, _ = _run([exe, "login", "status"])
    return code == 0


def claude_logged_in(exe: str) -> bool:
    """`claude auth status` 의 JSON loggedIn — 글이 JSON 이 아니면 종료 코드로."""
    code, out = _run([exe, "auth", "status"])
    try:
        return bool(json.loads(out[out.index("{"):]).get("loggedIn"))
    except (ValueError, AttributeError):
        return code == 0


def setup_cli(ask: bool = True) -> Dict[str, bool]:
    """Codex CLI(2차 심판, 필수) · Claude CLI(기준 다듬기 루프, 선택) — 설치 · 로그인 상태를 보고, 로그인이 안 됐으면 그 자리에서 로그인 명령을 띄운다."""
    from code_eval.claude_cli_client import find_claude_executable
    from code_eval.codex_cli_client import find_codex_executable
    from code_eval.llm_client import env_value
    out: Dict[str, bool] = {}
    for name, exe, check, login, install, required in (
            ("Codex CLI (2차 심판, 필수)", find_codex_executable(env_value("CODEX_CLI_PATH")), codex_logged_in, ["login"],
             "npm install -g @openai/codex", True),
            ("Claude CLI (기준 다듬기 루프, 선택)", find_claude_executable(env_value("CLAUDE_CLI_PATH")), claude_logged_in, ["auth", "login"],
             "npm install -g @anthropic-ai/claude-code", False)):
        if not exe:
            print(f"  {'❌' if required else '⚠️ '} {name}: 설치 안 됨 — 설치 스크립트를 다시 실행하거나: {install}")
            out[name] = False
            continue
        if check(exe):
            print(f"  ✅ {name}: 로그인 됨")
            out[name] = True
            continue
        print(f"  {'❌' if required else '⚠️ '} {name}: 로그인 필요")
        if ask and (required or ask_yes("    지금 로그인할까요?", default=False)) and (not required or ask_yes("    지금 로그인할까요? (브라우저가 열립니다)")):
            subprocess.run([exe, *login])   # 로그인 화면 · 안내는 그 CLI 가 터미널에 직접 띄운다
            out[name] = check(exe)
            print(f"    {'✅ 로그인 됨' if out[name] else '아직 로그인 안 됨 — 나중에: ' + Path(exe).stem + ' ' + ' '.join(login)}")
        else:
            out[name] = False
            print(f"    나중에: {Path(exe).stem} {' '.join(login)}")
    return out


# ------------------------------------------------------------------ 웹 로그인 ------------------------------------------------------------------
def web_login_state(timeout_ms: int = 8000) -> Dict[str, str]:
    """수집용 Chrome 의 사이트 창마다 로그인 화면이 남았는지 — {사이트 주소: 'ok' | 'login' | 'no-tab' | 'error'}."""
    from playwright.sync_api import sync_playwright
    from tools import chrome_cdp
    hosts = [url for url, _place in chrome_cdp.sites()]
    out: Dict[str, str] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(chrome_cdp.CDP_URL, timeout=timeout_ms)
        pages = [p for c in browser.contexts for p in c.pages]
        for url in hosts:
            host = re.sub(r"^https?://", "", url).split("/")[0]
            page = next((p for p in pages if host in (p.url or "")), None)
            if page is None:
                out[url] = "no-tab"
                continue
            try:
                out[url] = page.evaluate(LOGIN_WALL_JS)
            except Exception:
                out[url] = "error"
    return out


def setup_web(ask: bool = True) -> Dict[str, str]:
    """수집용 Chrome(전용 프로필 · 포트)을 띄워 네 사이트 창을 열고, 사람이 로그인한 뒤 창마다 로그인 화면이 남았는지 본다."""
    from code_eval.llm_client import env_value
    from tools import chrome_cdp
    try:
        st = chrome_cdp.launch()
    except FileNotFoundError as exc:
        print(f"  ❌ {exc}")
        return {}
    if not st.get("ok"):
        print(f"  ❌ 수집용 Chrome 에 붙지 못함(포트 {chrome_cdp.PORT}) — 이 프로필의 Chrome 이 떠 있으면: modelgrade login 전에 python tools/chrome_cdp.py --close")
        return {}
    aiona_auto = bool(env_value("AIONA_LOGIN_ID") and env_value("AIONA_LOGIN_PASSWORD"))
    print("  Chrome 창 네 개(ChatGPT · Claude · Gemini · AIONA)가 열렸습니다. 각 창에서 직접 로그인하세요 — 로그인은 이 프로필에 남아 다음부터는 안 해도 됩니다.\n"
          "  (Chrome 에 구글 계정을 넣어 두면 'Google 로 계속하기' 한 번이면 됩니다." + (" AIONA 는 .env 의 아이디로 수집기가 로그인합니다." if aiona_auto else "") + ")")
    if not ask:
        return {}
    for attempt in range(3):
        input("  로그인을 마쳤으면 Enter … ")
        try:
            state = web_login_state()
        except Exception as exc:
            print(f"  ⚠️  로그인 확인 실패({type(exc).__name__}) — 창에서 로그인됐는지 눈으로 확인하세요")
            return {}
        left = []
        for url, s in state.items():
            auto = "aiona" in url and aiona_auto
            ok = s == "ok" or (s == "login" and auto)
            label = {"ok": "로그인 됨으로 보임", "login": "로그인 화면이 보임" + (" — .env 아이디로 자동 로그인" if auto else ""),
                     "no-tab": "창이 없음 — 다시 열려면 modelgrade login", "error": "확인 못 함"}.get(s, s)
            print(f"   {'✅' if ok else '⚠️ '} {url} — {label}")
            if not ok and s != "error":
                left.append(url)
        if not left or not ask_yes("  로그인 화면이 남은 창이 있습니다. 로그인한 뒤 다시 확인할까요?"):
            return state
    return state


# ------------------------------------------------------------------ 전체 ------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="modelgrade setup", description="처음 설정 — .env 키 · Codex · Claude CLI · 웹 로그인 · 점검")
    ap.add_argument("--keys", action="store_true", help=".env 키만 (있는 키도 바꿀지 물음)")
    ap.add_argument("--cli", action="store_true", help="Codex · Claude CLI 로그인만")
    ap.add_argument("--web", action="store_true", help="수집용 Chrome 웹 로그인만")
    a = ap.parse_args(argv)
    only = a.keys or a.cli or a.web
    talk = interactive()
    if ensure_env():
        print(f"✅ .env 를 만들었습니다 (.env.example 복사 — {ENV})")
    if not talk:   # CI · 파이프 — 묻지 않고 남은 일만(login 은 예전처럼 창만 연다)
        if a.web and not (a.keys or a.cli):
            from tools import chrome_cdp
            return chrome_cdp.main(["--login"])
        print("터미널이 아니라 묻지 않습니다. 이어서 터미널에서: modelgrade setup")
        return 0
    if a.keys or not only:
        print("\n① .env 키 (값은 화면에 남지 않습니다 — 나중에 바꾸려면 modelgrade setup --keys 또는 .env 를 직접 고침)")
        missing = setup_keys(ask_again=a.keys)
        if missing:
            print(f"  ❌ 필수 키가 비어 있음: {', '.join(missing)}")
    if a.cli or not only:
        print("\n② CLI 로그인")
        setup_cli()
    if a.web or not only:
        print("\n③ 웹 로그인 (답변 수집용 Chrome)")
        if a.web or ask_yes("  지금 수집용 Chrome 을 띄워 로그인할까요?"):
            setup_web()
        else:
            print("  나중에: modelgrade login")
    if only:
        return 0
    print("\n④ 환경 점검")
    from tools import doctor
    return doctor.main([])


if __name__ == "__main__":
    raise SystemExit(main())
