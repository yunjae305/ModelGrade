"""답 아래 복사 아이콘 글로 바꾸기 — 확인(gpt_app_recheck·aiona_recopy)에서 저장된 답이 '답 아래 복사 아이콘' 글과 달랐던 턴만, 그 아이콘을 다시 눌러 받은 글로
바꾼다 (사용자 규칙 2026-10-21: 답변 아래 복사 아이콘으로 받은 글이 곧 답). 원래 글은 행의 answer_before_bottom_copy 에 남기고, 바꾼 모델의 CODE 평가를 다시 한다.

    python -X utf8 tools/apply_bottom_copy.py --run a1642702fccc --gpt Q7_T4,Q10_T6,Q38_T1 --aiona Q19_T1,Q54_T1,Q72_T1 [--dry]

- GPT: ChatGPT 앱(디버깅 포트)에서 그 채팅을 열고 그 턴의 답 아래 '복사'(코드블록·카드 안 버튼 제외, 화면 맨 아래 것)를 누른다 — 클립보드는 가로채기만.
- AIONA: 저장된 채팅 주소를 열고 그 턴 답변 맨 아래 📋 를 누른다(코드블록 버튼 제외).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def gpt_copies(run: dict, targets: List[str]) -> Dict[str, str]:
    from playwright.sync_api import sync_playwright
    from tools import gpt_app_recheck as g
    rows = {r["case_id"]: r for r in run["datasets"]["gpt"]}
    ts = [datetime.fromisoformat(r["collection"]["collected_at"]) for r in rows.values() if (r.get("collection") or {}).get("collected_at")]
    out: Dict[str, str] = {}
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp("http://127.0.0.1:9334")
        page = next(p for c in b.contexts for p in c.pages if p.url == "app://-/index.html")
        threads: Dict[str, dict] = {}
        for _ in range(40):
            for t in page.evaluate(g.THREADS_JS):
                threads.setdefault(t["id"], t)
            if page.evaluate(g.SCROLL_SIDEBAR_JS) <= 0:
                break
            page.wait_for_timeout(300)
        inside = sorted([t for t in threads.values() if t["created"] and min(ts) - timedelta(minutes=30) <= datetime.fromisoformat(t["created"].replace("Z", "+00:00")) <= max(ts) + timedelta(minutes=5)],
                        key=lambda x: x["created"])
        page.evaluate(g.HOOK_JS)
        try:
            for cid in targets:
                q = cid.split("_")[0]
                want = g.norm_q(rows[cid]["question"])
                conv = inside[int(q[1:])]   # 생성 순서 0 은 수집 전 다른 채팅, 1 부터 Q1…
                page.evaluate(g.SIDEBAR_TOP_JS)
                for _ in range(60):
                    if page.evaluate(g.CLICK_JS, conv["id"]):
                        break
                    page.evaluate(g.SCROLL_SIDEBAR_JS)
                    page.wait_for_timeout(200)
                page.wait_for_timeout(2500)
                key = None
                for direction in (-1, 1):
                    for _ in range(60):
                        key = next((tr["key"] for tr in page.evaluate(g.TURNS_JS) if want and (want in g.norm_q(tr["user"]) or want[:60] in g.norm_q(tr["user"]))), None)
                        if key or page.evaluate(g.SCROLL_CHAT_JS, direction) <= 0:
                            break
                        page.wait_for_timeout(400)
                    if key:
                        break
                if not key:
                    print(f"  {cid}: 턴 못 찾음", flush=True)
                    continue
                text = None
                for attempt in range(4):
                    loc = page.locator(f'[data-turn-key="{key}"]')
                    loc.scroll_into_view_if_needed()
                    loc.hover()
                    page.wait_for_timeout(600)
                    page.evaluate("() => { window.__chk = []; }")
                    n = page.evaluate(g.BOTTOM_COPY_JS, key)
                    page.wait_for_timeout(800)
                    got = [x for x in page.evaluate("() => window.__chk || []") if isinstance(x, str) and x.strip()]
                    if n >= 1 and got:
                        text = got[-1]
                        break
                if text:
                    out[cid] = text
                print(f"  {cid}: {'받음 ' + str(len(text)) + '자' if text else '못 받음'} (저장 {len(rows[cid].get('answer') or '')}자)", flush=True)
        finally:
            page.evaluate(g.UNHOOK_JS)
    return out


def aiona_copies(run: dict, targets: List[str]) -> Dict[str, str]:
    from tools.aiona_recopy import copy_answer
    from webapp.aiona_web import AionaChat
    rows = {r["case_id"]: r for r in run["datasets"]["aiona"]}
    out: Dict[str, str] = {}
    with AionaChat(headless=True, allow_login=False, answer_timeout_s=60) as chat:
        for cid in targets:
            r = rows[cid]
            url = (r.get("collection") or {}).get("url") or next((x["collection"]["url"] for x in rows.values()   # 재시도로 받은 행은 주소가 비어 있다 — 같은 대화 다른 턴의 주소
                                                                   if x.get("conversation_id") == r.get("conversation_id") and (x.get("collection") or {}).get("url")), None)
            if not url:
                print(f"  {cid}: 채팅 주소 없음", flush=True)
                continue
            chat.page.goto(url, wait_until="domcontentloaded")
            chat.page.wait_for_timeout(2500)
            chat._dismiss_popups()
            chat._answers().first.wait_for(timeout=20_000)
            text = copy_answer(chat, int(r.get("turn") or 1) - 1)
            if text and text.strip():
                out[cid] = text
            print(f"  {cid}: {'받음 ' + str(len(text)) + '자' if text else '못 받음'} (저장 {len(r.get('answer') or '')}자)", flush=True)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--gpt", default="")
    ap.add_argument("--aiona", default="")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args(argv)
    from tools.modelgrade import code_evaluate, load_run, merge_provider_into_run
    run = load_run(args.run)
    plan = {"gpt": [x for x in args.gpt.split(",") if x], "aiona": [x for x in args.aiona.split(",") if x]}
    now = datetime.now(timezone.utc).isoformat()
    for provider, targets in plan.items():
        if not targets:
            continue
        print(f"{provider}: {targets}", flush=True)
        got = gpt_copies(run, targets) if provider == "gpt" else aiona_copies(run, targets)
        if args.dry or not got:
            continue
        run = load_run(args.run)
        rows = [dict(r) for r in run["datasets"][provider]]
        for r in rows:
            if r["case_id"] in got:
                r["answer_before_bottom_copy"] = r.get("answer_before_bottom_copy") or r.get("answer")
                r["answer"] = got[r["case_id"]]
                r["collection"] = {**(r.get("collection") or {}), "capture": "copy-button-bottom",
                                   "bottom_copy_fix": {"at": now, "why": "저장된 답이 답 아래 복사 아이콘 글과 달라 그 아이콘 글로 바꿈 (사용자 규칙 10-21)"}}
        merge_provider_into_run(args.run, provider, run["models"].get(provider) or provider, rows, code_evaluate(rows, provider), None, backup=True)
        print(f"{provider}: {len(got)}턴 바꿈 · CODE 평가 다시", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
