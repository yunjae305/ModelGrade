"""실행 환경 점검 — 다른 PC 에서 처음 돌리기 전에 무엇이 빠졌는지 한 번에 본다 (값 · 키는 출력하지 않는다).

    ./modelgrade.sh doctor        (Linux · Mac)
    .\\modelgrade doctor          (Windows PowerShell)

필수(✅ 가 아니면 종료 코드 1): 파이썬 3.10+ · 파이썬 패키지 · .env · Codex CLI 설치와 로그인 · 1차 심판 키(Decisions 또는 Jev) · Chrome.
선택(⚠ 이어도 됨): 수집용 Chrome 이 떠 있는지 · Claude CLI(기준 다듬기 루프) · 한글 글꼴(질문 세트 만들기) · docx → pdf(Word 또는 LibreOffice) · AIONA 로그인 정보.
"""
from __future__ import annotations

import importlib
import platform
import shutil
import sys
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PACKAGES = (("httpx", "httpx"), ("tiktoken", "tiktoken"), ("docx", "python-docx"), ("fpdf", "fpdf2"), ("PIL", "Pillow"),
            ("pypdfium2", "pypdfium2"), ("playwright", "playwright"))


class Check(NamedTuple):
    name: str
    ok: Optional[bool]   # True 됨 · False 안 됨 · None 확인만(정보)
    detail: str
    required: bool
    fix: str = ""


def _try(fn: Callable[[], Check], name: str, required: bool) -> Check:
    try:
        return fn()
    except Exception as exc:   # 점검 자체가 실패해도 다른 점검은 계속
        return Check(name, False, f"점검 실패: {type(exc).__name__}(설정값이 섞일 수 있어 내용은 출력하지 않음)", required)   # Codex 리뷰 10-25 밤


def check_python() -> Check:
    ok = sys.version_info >= (3, 10)
    return Check("파이썬 3.10 이상", ok, platform.python_version(), True, "python.org 에서 3.12 이상을 설치하세요")


def check_packages() -> Check:
    missing = []
    for mod, pkg in PACKAGES:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(pkg)
    return Check("파이썬 패키지", not missing, "모두 있음" if not missing else "없음: " + ", ".join(missing), True,
                 "설치 스크립트를 다시 실행하거나: python -m pip install -r requirements.txt")


def check_env() -> Check:
    ok = (ROOT / ".env").is_file()
    return Check(".env 파일", ok, "있음" if ok else "없음", True, ".env.example 을 .env 로 복사하고 키를 채우세요")


def check_codex() -> Check:
    from code_eval.codex_cli_client import check_codex_login, find_codex_executable
    from code_eval.llm_client import env_value
    exe = find_codex_executable(env_value("CODEX_CLI_PATH"))
    if not exe:
        return Check("Codex CLI (2차 심판)", False, "설치 안 됨", True, "Node.js 를 설치한 뒤: npm install -g @openai/codex → codex login")
    reason = check_codex_login(exe)
    return Check("Codex CLI (2차 심판)", reason is None, "설치 · 로그인 됨" if reason is None else "로그인 필요", True, "codex login")


def check_first_judge() -> Check:
    from code_eval.decisions_client import resolve_decisions_client
    from code_eval.jev_client import resolve_jev_client
    dec, _, _ = resolve_decisions_client()
    jev, _, _ = resolve_jev_client(engine="jev")
    have = [n for n, c in (("Decisions", dec), ("Jev", jev)) if c is not None]
    return Check("1차 심판 키", bool(have), " · ".join(have) + " 설정됨" if have else "없음", True,
                 ".env 에 OPENAI_API_KEY(Decisions) 또는 TYPESAFE_API_KEY(Jev) 를 넣으세요")


def check_chrome() -> Check:
    from tools.chrome_cdp import chrome_path
    try:
        path = chrome_path()
    except FileNotFoundError:
        return Check("Google Chrome (답변 수집)", False, "못 찾음", True, "Chrome 을 설치하거나 .env 에 CHROME_PATH=<실행 파일> 을 넣으세요")
    return Check("Google Chrome (답변 수집)", True, path.name, True)


def check_collect_chrome() -> Check:
    from tools.chrome_cdp import PORT, status
    st = status(timeout=1.0)
    return Check("수집용 Chrome 창", None, f"떠 있음 · 탭 {len(st['tabs'])}개" if st["ok"] else f"안 떠 있음(포트 {PORT}) — 수집을 시작하면 띄움", False,
                 "처음 한 번: modelgrade login 으로 네 사이트에 로그인")


def check_claude() -> Check:
    from code_eval.claude_cli_client import find_claude_executable
    from code_eval.llm_client import env_value
    exe = find_claude_executable(env_value("CLAUDE_CLI_PATH"))
    return Check("Claude CLI (기준 다듬기 루프 · 심판 비교, 선택)", bool(exe), "설치됨" if exe else "없음", False,
                 "npm install -g @anthropic-ai/claude-code → claude 로그인")


def check_font() -> Check:
    from webapp.attachment_files import font_path
    try:
        return Check("한글 글꼴 (질문 세트 만들기의 첨부 PDF, 선택)", True, Path(font_path()).name, False)
    except Exception:
        hint = "sudo apt install fonts-nanum" if platform.system() == "Linux" else ".env 또는 환경 변수 QUESTION_FONT=<.ttf 경로>"
        return Check("한글 글꼴 (질문 세트 만들기의 첨부 PDF, 선택)", False, "못 찾음", False, hint)


def check_docx_pdf() -> Check:
    from code_eval.attachment_pages import _soffice
    word = False
    if platform.system() == "Windows":   # pywin32 가 있어도 Word 가 없으면 못 씀 — Word 의 COM 등록을 본다(Word 를 띄우지 않음, Codex 리뷰 10-25 밤)
        try:
            importlib.import_module("win32com.client")
            import winreg
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Word.Application"))
            word = True
        except Exception:
            word = False
    office = _soffice()
    how = "Word" if word else ("LibreOffice" if office else "")
    return Check("docx 첨부를 쪽 이미지로 (선택)", bool(how), how or "없음 — docx 첨부는 글로만 심판에게 감", False,
                 "LibreOffice 설치(soffice) 또는 Windows 에서 pip install pywin32 + Word")


def check_aiona_login() -> Check:
    from code_eval.llm_client import env_value
    ok = bool(env_value("AIONA_LOGIN_ID")) and bool(env_value("AIONA_LOGIN_PASSWORD"))
    return Check("AIONA 로그인 정보 (선택)", ok, "있음" if ok else "없음 — AIONA 는 Chrome 창에서 직접 로그인", False)


CHECKS = ((check_python, "파이썬", True), (check_packages, "패키지", True), (check_env, ".env", True), (check_codex, "Codex CLI", True),
          (check_first_judge, "1차 심판", True), (check_chrome, "Chrome", True), (check_collect_chrome, "수집용 Chrome", False),
          (check_claude, "Claude CLI", False), (check_font, "한글 글꼴", False), (check_docx_pdf, "docx", False), (check_aiona_login, "AIONA", False))


def run_checks() -> List[Check]:
    """점검마다 따로 — 패키지가 없어 한 점검이 실패해도('점검 실패: ModuleNotFoundError') 나머지는 계속."""
    return [_try(fn, name, required) for fn, name, required in CHECKS]


def main(argv: Optional[List[str]] = None) -> int:
    checks = run_checks()
    mark = {True: "✅", False: "❌", None: "ℹ️ "}
    for c in checks:
        sign = mark[c.ok] if c.ok is not False or c.required else "⚠️ "
        print(f"{sign} {c.name}: {c.detail}" + (f"\n     → {c.fix}" if c.ok is False and c.fix else ""))
    bad = [c for c in checks if c.required and c.ok is not True]
    print()
    run = r".\modelgrade" if platform.system() == "Windows" else "./modelgrade.sh"
    print(f"필수 항목 모두 준비됨 — 다음: {run} login(처음 한 번) → {run} start" if not bad else f"필수 항목 {len(bad)}개가 남았습니다 (위의 → 를 따라 하세요)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
