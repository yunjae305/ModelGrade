"""웹 채팅 자동화용 로그인 창 — AIONA(winz.aiona.kr) 또는 chatgpt.com·claude.ai·gemini.google.com 을 별도 Chrome 프로필로 열어
사용자가 직접 로그인한다.

수집 방식 비교 테스트(2026-10-14 사용자 요청: API 와 웹 브라우저 답변의 차이)에서 쓴다. 비밀번호는 프로그램이 다루지 않고,
로그인 상태(쿠키)는 ``data/browser_profiles/<이름>`` 에만 남는다(저장소에 올리지 않음). 창을 모두 닫으면 끝난다.
AIONA 는 '자동 로그인(30일간 로그인 유지)'을 켜 두면 수집이 그 로그인 상태를 계속 쓴다.
봇 확인 화면이 나오면 사용자가 직접 처리한다 — 프로그램은 우회하지 않는다.

    python tools/web_chat_login.py aiona      # AIONA (창에서 직접 로그인)
    python tools/web_chat_login.py aiona --auto   # AIONA (.env 의 AIONA_LOGIN_ID·AIONA_LOGIN_PASSWORD 로 로그인 — 사용자가 실행)
    python tools/web_chat_login.py factchat --auto --inspect   # FactChat(aichat.kku.ac.kr, .env 의 FACTCHAT_LOGIN_ID·PASSWORD)
                                                               # 로그인 후 채팅 화면 구조를 data/browser_profiles/factchat-inspect.json 에 남김
    python tools/web_chat_login.py web-chat   # ChatGPT·Claude·Gemini
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "data" / "browser_profiles"
SITES = {"aiona": ("https://winz.aiona.kr/login",), "factchat": ("https://aichat.kku.ac.kr/auth",),
         "web-chat": ("https://chatgpt.com/", "https://claude.ai/", "https://gemini.google.com/app")}


def main(argv=None) -> int:
    from playwright.sync_api import sync_playwright

    args = list(argv if argv is not None else sys.argv[1:])
    auto, inspect = "--auto" in args, "--inspect" in args
    args = [a for a in args if a not in ("--auto", "--inspect")]
    name = (args or ["aiona"])[0]
    if name not in SITES:
        raise SystemExit("사용: python tools/web_chat_login.py aiona|factchat|web-chat [--auto] [--inspect]")
    profile = PROFILES / name
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile), channel="chrome", headless=False, no_viewport=True)
        if auto and name == "factchat":
            sys.path.insert(0, str(ROOT))
            from webapp.factchat_web import FactChatWebError, credentials as fc_credentials, inspect_chat, login as fc_login
            page = context.pages[0] if context.pages else context.new_page()
            try:
                fc_login(page, *fc_credentials())
                print("FactChat 로그인 완료 — 로그인 상태를 저장했습니다.", flush=True)
                if inspect:   # 채팅 화면 구조 남기기 — 짧은 질문을 한 번 보내 답변 영역·복사 버튼도 확인
                    out = inspect_chat(page, PROFILES / "factchat-inspect.json", question="1 더하기 1은 얼마야? 숫자만 답해줘.")
                    print(f"채팅 화면 구조를 남겼습니다: {out}", flush=True)
                return 0
            except FactChatWebError as exc:
                print(f"FactChat 로그인 실패: {exc}", flush=True)
                return 1
            except Exception as exc:   # 원본 예외 문구는 찍지 않는다 — 브라우저 예외 문구(Call log)에 입력값이 섞일 수 있다
                print(f"FactChat 로그인·점검 실패: {type(exc).__name__}", flush=True)
                return 1
            finally:
                context.close()
        if auto and name == "aiona":
            sys.path.insert(0, str(ROOT))
            from webapp.aiona_web import AionaWebError, credentials, login
            page = context.pages[0] if context.pages else context.new_page()
            try:
                login(page, *credentials())
                print("AIONA 로그인 완료 — 로그인 상태를 저장했습니다.", flush=True)
                return 0
            except AionaWebError as exc:
                print(f"AIONA 로그인 실패: {exc}", flush=True)
                return 1
            except Exception as exc:   # 원본 예외 문구는 찍지 않는다 — 브라우저 예외 문구(Call log)에 입력값이 섞일 수 있다
                print(f"AIONA 로그인 실패: {type(exc).__name__}", flush=True)
                return 1
            finally:
                context.close()
        pages = list(context.pages)
        for index, url in enumerate(SITES[name]):
            page = pages[index] if index < len(pages) else context.new_page()
            page.goto(url)
        print("로그인한 뒤 브라우저 창을 닫아 주세요.", flush=True)
        try:
            context.wait_for_event("close", timeout=30 * 60 * 1000)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
