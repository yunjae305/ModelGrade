"""Gemini(웹) 답 수집 확인 — 저장된 채팅 주소를 수집용 크롬(디버깅 포트 9222)의 새 탭으로 열어, 답마다 답 아래 도구 줄의 '대답 복사'(📋)를 눌러
나온 글을 저장된 답과 비교한다 (사용자 요청 2026-10-21). 코드블록의 '코드 복사' 버튼은 쓰지 않는다. 복사 결과를 고치지 않는다.

    python -X utf8 tools/gemini_recheck.py --run a1642702fccc [--limit N] [--start N]
    → data/judge_runs/<실행>/gemini-recheck.jsonl (대화마다 바로 한 줄씩) · gemini-recheck.json (요약)

- 클립보드는 가로채기만 하고 실제로 쓰지 않는다. 새 탭만 쓰고 끝나면 닫는다 — 메시지를 보내거나 설정을 바꾸지 않는다.
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOOK_JS = r"""(() => {
  if (window.__chkHooked) return; window.__chkHooked = true; window.__chk = [];
  const c = navigator.clipboard;
  if (c) {
    c.writeText = async (t) => { window.__chk.push(String(t)); };
    c.write = async (items) => { for (const it of items) if (it.types.includes('text/plain')) window.__chk.push(await (await it.getType('text/plain')).text()); };
  }
  const setData = DataTransfer.prototype.setData;
  DataTransfer.prototype.setData = function (type, value) { if (/text\/plain|^text$/i.test(type)) window.__chk.push(String(value)); return setData.call(this, type, value); };
})();"""
# 답 아래 도구 줄의 '대답 복사' — 화면 이름은 '복사'(툴팁 '대답 복사'), 코드블록 버튼은 '코드 복사'로 다르고 코드블록 안에 있다
BOTTOM_COPY_JS = r"""(i) => {
  const r = document.querySelectorAll('model-response')[i]; if (!r) return -1;
  const b = [...r.querySelectorAll('button')].filter(x => /^(복사|대답 복사|응답 복사|Copy|Copy response)$/.test((x.getAttribute('aria-label') || '').trim()))
    .filter(x => !x.closest('code-block, pre, .code-block'));
  if (!b.length) return 0;
  b[b.length - 1].click(); return b.length;
}"""


def _eol(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n")).strip()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--gap", type=int, default=20, help="채팅 사이 기다리는 초 (봇 확인 피하기)")
    args = ap.parse_args(argv)
    from playwright.sync_api import sync_playwright
    from tools.modelgrade import load_run
    run = load_run(args.run)
    rows = [r for r in run["datasets"]["gemini"] if isinstance(r, dict)]
    convs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        url = (r.get("collection") or {}).get("url")
        if url and str(url).startswith("http") and (r.get("collection") or {}).get("status") == "SUCCESS":
            convs[url].append(r)
    order = sorted(convs, key=lambda u: min(int(x["case_id"].split("_")[0][1:]) for x in convs[u]))
    order = order[args.start:args.start + args.limit] if args.limit else order[args.start:]
    out_dir = ROOT / "data" / "judge_runs" / args.run
    live = out_dir / "gemini-recheck.jsonl"
    checked: List[Dict[str, Any]] = []
    print(f"Gemini 대화 {len(order)}개 확인", flush=True)
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        page = b.contexts[0].new_page()
        page.add_init_script(HOOK_JS)
        try:
            for n, url in enumerate(order, 1):
                items = sorted(convs[url], key=lambda x: int(x.get("turn") or 1))
                if n > 1:
                    page.wait_for_timeout(args.gap * 1000)   # 채팅을 연달아 열면 Google 이 비정상 트래픽 확인을 띄운다(10-21) — 사이를 둔다
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
                if "google.com/sorry" in page.url or "sorry/index" in page.url:
                    print("Google 비정상 트래픽 확인 페이지가 떠서 멈춤 — 봇 확인은 풀지 않는다. 시간을 두고 --start 로 이어서", flush=True)
                    break
                for _ in range(20):   # 긴 대화는 앞 턴이 위로 올려야 불러와진다
                    if page.locator("model-response").count() >= max(int(x.get("turn") or 1) for x in items):
                        break
                    page.mouse.wheel(0, -4000)
                    page.wait_for_timeout(700)
                total = page.locator("model-response").count()
                for r in items:
                    i = int(r.get("turn") or 1) - 1
                    resp = page.locator("model-response").nth(i)
                    try:
                        resp.scroll_into_view_if_needed(timeout=4000)
                        resp.hover(timeout=4000)
                    except Exception:
                        pass
                    page.wait_for_timeout(400)
                    page.evaluate("() => { window.__chk = []; }")
                    nbtn = page.evaluate(BOTTOM_COPY_JS, i)
                    copied = None
                    for _ in range(8):
                        page.wait_for_timeout(300)
                        got = page.evaluate("() => window.__chk || []")
                        if got:
                            copied = got[-1]
                            break
                    stored = r.get("answer") or ""
                    same = copied is not None and _eol(copied) == _eol(stored)
                    rec = {"case_id": r["case_id"], "url": url, "responses_on_page": total, "bottom_buttons": nbtn,
                           "recopy": "같음" if same else ("복사 실패" if copied is None else "다름"),
                           "similarity": None if same or copied is None else round(difflib.SequenceMatcher(None, _eol(copied), _eol(stored), autojunk=False).ratio(), 4),
                           "copied_chars": len(copied) if copied is not None else None, "stored_chars": len(stored)}
                    if copied is not None and not same:
                        a, s = _eol(copied), _eol(stored)
                        k = next((j for j in range(min(len(a), len(s))) if a[j] != s[j]), min(len(a), len(s)))
                        rec["first_diff"] = {"at": k, "copied": a[max(0, k - 30):k + 60], "stored": s[max(0, k - 30):k + 60]}
                    checked.append(rec)
                    with live.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                mine = [c for c in checked if c["url"] == url]
                print(f"  {n:>3}. {items[0]['case_id'].split('_')[0]:<4} 턴 {len(items)} · 화면 답 {total} · {dict(Counter(c['recopy'] for c in mine))}", flush=True)
        finally:
            page.close()
    summary = {"conversations": len(order), "checked": len(checked), "recopy": dict(Counter(c["recopy"] for c in checked)),
               "different": [(c["case_id"], c["similarity"]) for c in checked if c["recopy"] == "다름"],
               "copy_failed": [c["case_id"] for c in checked if c["recopy"] == "복사 실패"],
               "bottom_button_not_one": [(c["case_id"], c["bottom_buttons"]) for c in checked if c["bottom_buttons"] != 1]}
    (out_dir / "gemini-recheck.json").write_text(json.dumps({"summary": summary, "checked": checked}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n끝: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
