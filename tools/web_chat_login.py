"""웹 채팅 자동화용 로그인 창 — AIONA · FactChat · 우수AI. (ChatGPT · Claude · Gemini 로그인은 수집용 Chrome 에서: python tools/chrome_cdp.py --login, 10-23)
별도 Chrome 프로필로 열어
사용자가 직접 로그인한다.

수집 방식 비교 테스트(2026-10-14 사용자 요청: API 와 웹 브라우저 답변의 차이)에서 쓴다. 비밀번호는 프로그램이 다루지 않고,
로그인 상태(쿠키)는 ``data/browser_profiles/<이름>`` 에만 남는다(저장소에 올리지 않음). 창을 모두 닫으면 끝난다.
AIONA 는 '자동 로그인(30일간 로그인 유지)'을 켜 두면 수집이 그 로그인 상태를 계속 쓴다.
봇 확인 화면이 나오면 사용자가 직접 처리한다 — 프로그램은 우회하지 않는다.

    python tools/web_chat_login.py aiona      # AIONA (창에서 직접 로그인)
    python tools/web_chat_login.py aiona --auto   # AIONA (.env 의 AIONA_LOGIN_ID·AIONA_LOGIN_PASSWORD 로 로그인 — 사용자가 실행)
    python tools/web_chat_login.py factchat --auto --inspect   # FactChat(aichat.kku.ac.kr, .env 의 FACTCHAT_LOGIN_ID·PASSWORD)
                                                               # 로그인 후 채팅 화면 구조를 data/browser_profiles/factchat-inspect.json 에 남김
    python tools/web_chat_login.py oosoo --auto --inspect      # 우수AI(app.oosoo.kr, .env 의 OOSOO_LOGIN_ID·PASSWORD) — 같은 방식
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "data" / "browser_profiles"
SITES = {"aiona": (None,),   # AIONA 는 webapp.aiona_web.AIONA_URL + /login (.env AIONA_URL, 기본 dev.aiona.kr)
         "factchat": ("https://aichat.kku.ac.kr/auth",), "oosoo": ("https://oosoo.kr/login/",)}


def main(argv=None) -> int:
    from playwright.sync_api import sync_playwright

    args = list(argv if argv is not None else sys.argv[1:])
    auto, inspect = "--auto" in args, "--inspect" in args
    args = [a for a in args if a not in ("--auto", "--inspect")]
    name = (args or ["aiona"])[0]
    if name == "aiona":   # AIONA 는 수집용 Chrome(tools/chrome_cdp.py, Gemini 와 같은 프로필)에서 — 수집기가 그 프로필에 붙는다 (2026-10-19)
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from tools.chrome_cdp import attach, find_or_open
        from webapp.aiona_web import AIONA_URL, AionaWebError, credentials, login
        with sync_playwright() as p:
            browser = attach(p)
            page = find_or_open(browser, AIONA_URL.split("//", 1)[-1], f"{AIONA_URL}/login")
            if not auto:
                page.goto(f"{AIONA_URL}/login")
                print("수집용 Chrome 의 AIONA 창에서 로그인해 주세요 (창은 그대로 두어도 됩니다).", flush=True)
                browser.close()
                return 0
            try:
                login(page, *credentials())
                print("AIONA 로그인 완료 — 수집용 Chrome 프로필에 저장했습니다.", flush=True)
                return 0
            except AionaWebError as exc:
                print(f"AIONA 로그인 실패: {exc}", flush=True)
                return 1
            except Exception as exc:   # 원본 예외 문구는 찍지 않는다 — 브라우저 예외 문구(Call log)에 입력값이 섞일 수 있다
                print(f"AIONA 로그인 실패: {type(exc).__name__}", flush=True)
                return 1
            finally:
                browser.close()
    if name not in SITES:
        raise SystemExit("사용: python tools/web_chat_login.py aiona|factchat|oosoo [--auto] [--inspect]")
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
        if auto and name == "oosoo":
            sys.path.insert(0, str(ROOT))
            from webapp.oosoo_web import OosooWebError, credentials as os_credentials, inspect_chat as os_inspect, login as os_login
            page = context.pages[0] if context.pages else context.new_page()
            try:
                os_login(page, *os_credentials())
                print("우수AI 로그인 완료 — 로그인 상태를 저장했습니다.", flush=True)
                if inspect:   # 채팅 화면 구조 남기기 — 짧은 질문을 한 번 보내 답변 영역·복사 버튼도 확인
                    out = os_inspect(page, PROFILES / "oosoo-inspect.json", question="1 더하기 1은 얼마야? 숫자만 답해줘.")
                    print(f"채팅 화면 구조를 남겼습니다: {out}", flush=True)
                return 0
            except OosooWebError as exc:
                print(f"우수AI 로그인 실패: {exc}", flush=True)
                return 1
            except Exception as exc:   # 원본 예외 문구는 찍지 않는다 — 브라우저 예외 문구(Call log)에 입력값이 섞일 수 있다
                print(f"우수AI 로그인·점검 실패: {type(exc).__name__}", flush=True)
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
