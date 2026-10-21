"""GPT(ChatGPT 앱) 답 수집 확인 — 앱에 남은 수집 채팅을 열어, 턴마다 수집기와 똑같이 답의 '복사' 버튼을 눌러 나온 글을 저장된 답과 비교한다
(사용자 요청 2026-10-21: "답변 복사를 제대로 한건지 확인"). 복사 결과를 고치거나 채우지 않는다 — 복사 버튼이 주는 글이 곧 답(사용자 규칙).

    python -X utf8 tools/gpt_app_recheck.py --run a1642702fccc     # 결과: data/judge_runs/<실행>/gpt-recheck.json

- 채팅 찾기: 사이드바 대화의 생성 시각이 이번 실행 GPT 수집 시간 안인 것 → 턴마다 '내가 한 말' 글을 실행의 질문과 대조해 Q번호·턴을 정한다.
- 비교: ① 지금 복사 버튼이 주는 글 = 저장된 답인가(줄끝·끝 공백만 무시) ② 화면에 보이는 답 글자가 저장된 답에 얼마나 들어 있는가
- 클립보드는 가로채기만 하고 실제로 쓰지 않는다. 앱에서는 채팅을 열고 복사 버튼만 누른다 — 메시지를 보내거나 설정을 바꾸지 않는다.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

THREADS_JS = r"""() => {
  const out = [];
  for (const el of document.querySelectorAll('[data-thread-title-trigger]')) {
    let f = el[Object.keys(el).find(k => k.startsWith('__reactFiber'))];
    for (let i = 0; i < 25 && f; i++, f = f.return) { const c = (f.memoizedProps || {}).conversation; if (c && c.id) { out.push({id: c.id, title: c.title || '', created: c.create_time || ''}); break; } }
  }
  return out;
}"""
SCROLL_SIDEBAR_JS = r"""() => {
  const t = document.querySelector('[data-thread-title-trigger]'); if (!t) return -1;
  let el = t.parentElement; while (el && !(el.scrollHeight > el.clientHeight + 20 && /(auto|scroll)/.test(getComputedStyle(el).overflowY))) el = el.parentElement;
  if (!el) return -1; const before = el.scrollTop; el.scrollTop += 700; return el.scrollTop - before;
}"""
SIDEBAR_TOP_JS = r"""() => {
  const t = document.querySelector('[data-thread-title-trigger]'); let el = t && t.parentElement;
  while (el && !(el.scrollHeight > el.clientHeight + 20)) el = el.parentElement; if (el) el.scrollTop = 0;
}"""
CLICK_JS = r"""(id) => {
  for (const el of document.querySelectorAll('[data-thread-title-trigger]')) {
    let f = el[Object.keys(el).find(k => k.startsWith('__reactFiber'))];
    for (let i = 0; i < 25 && f; i++, f = f.return) { const c = (f.memoizedProps || {}).conversation; if (c && c.id) { if (c.id === id) { el.scrollIntoView({block: 'center'}); el.click(); return true; } break; } }
  }
  return false;
}"""
TURNS_JS = r"""() => {
  const turns = [...document.querySelectorAll('[data-turn-key]')].filter(t => t.offsetParent !== null);
  return turns.map(t => {
    const ub = t.querySelector('[data-user-message-bubble], [data-message-role="user"], [data-message-author-role="user"]');
    const all = t.innerText || '';
    const at = all.indexOf('ChatGPT 답변:');
    const codes = [];
    for (const p of t.querySelectorAll('pre')) {
      if (ub && ub.contains(p)) continue;
      let f = p[Object.keys(p).find(k => k.startsWith('__reactFiber'))];
      for (let i = 0; i < 12 && f; i++, f = f.return) { const pr = f.memoizedProps || {}; if (typeof pr.code === 'string') { codes.push(pr.code); break; } }
    }
    return {key: t.getAttribute('data-turn-key'), user: ub ? ub.innerText : '', answer: at >= 0 ? all.slice(at + 'ChatGPT 답변:'.length) : '', codes};
  });
}"""
SCROLL_CHAT_JS = r"""(dir) => {
  const t = [...document.querySelectorAll('[data-turn-key]')].filter(x => x.offsetParent !== null)[0]; if (!t) return -1;
  let el = t.parentElement; while (el && !(el.scrollHeight > el.clientHeight + 20 && /(auto|scroll)/.test(getComputedStyle(el).overflowY))) el = el.parentElement;
  if (!el) return -1; const before = el.scrollTop; el.scrollTop = el.scrollTop + dir * el.clientHeight * 0.8; return Math.abs(el.scrollTop - before);
}"""
HOOK_JS = r"""() => {
  if (window.__chkHooked) { window.__chk = []; return; }
  window.__chkHooked = true; window.__chk = [];
  const clip = navigator.clipboard;
  window.__chkOrig = {w: clip.write, wt: clip.writeText};
  clip.writeText = async (t) => { window.__chk.push(String(t)); };
  clip.write = async (items) => { for (const it of items) if (it.types.includes('text/plain')) window.__chk.push(await (await it.getType('text/plain')).text()); };
}"""
UNHOOK_JS = r"""() => { if (window.__chkHooked) { const c = navigator.clipboard; c.write = window.__chkOrig.w; c.writeText = window.__chkOrig.wt; window.__chkHooked = false; } }"""
COPY_NAME = re.compile(r"^(복사|Copy)$")
# 답 아래 도구 줄의 복사 아이콘만 — 이름이 '복사'인 버튼 가운데 코드블록(질문·답 속) 안도, 내 질문 말풍선 쪽도 아닌 것 (사용자 지시 10-21:
# "코드블럭에도 복사버튼이 있고 아래에도 복사 아이콘이 있다, 아래에 있는 것을 눌러야 한다"). 턴마다 정확히 1개여야 누른다.
BOTTOM_COPY_JS = r"""(key) => {
  const t = document.querySelector(`[data-turn-key="${key}"]`); if (!t) return -1;
  const ub = t.querySelector('[data-user-message-bubble]');
  const b = [...t.querySelectorAll('button')].filter(x => /^(복사|Copy)$/.test((x.getAttribute('aria-label') || '').trim()))
    .filter(x => !x.closest('[data-markdown-copy="code-block"], pre') && !(ub && ub.contains(x)));
  if (!b.length) return 0;
  b[b.length - 1].click(); return b.length;   // 답 속 '글쓰기' 카드 등도 자기 복사 버튼이 있다 — 화면 순서상 마지막이 답 아래 도구 줄
}"""
_MD = re.compile(r"[\s`*#|_>\-]+")


def norm_q(text: str) -> str:
    """질문 대조용 — 공백·백틱 빼고, 코드블록 머리글(json·python 등)이 화면에 따로 찍히는 것을 무시하려고 그 낱말도 뺀다."""
    t = re.sub(r"[\s`]+", "", (text or "").lower())
    return re.sub(r"(json|python|javascript|text|csv|yaml|markdown)", "", t)


def coverage(visible: str, stored: str) -> float:
    """화면에 보이는 답 글자 가운데 저장된 답에 같은 순서로 들어 있는 비율 (공백·마크다운 기호·코드 머리글 무시)."""
    v = _MD.sub("", visible or "")
    for w in ("JSON", "Python", "python", "json", "복사", "text", "Text", "CSV", "csv"):
        v = v.replace(w, "")
    s = _MD.sub("", stored or "")
    if not v:
        return 1.0
    sm = difflib.SequenceMatcher(None, v, s, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(v)


def _eol(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n")).strip()


def collect(run: Dict[str, Any], limit: Optional[int] = None, start: int = 0, on_conversation: Any = None) -> List[Dict[str, Any]]:
    from playwright.sync_api import sync_playwright
    rows = [r for r in run["datasets"]["gpt"] if isinstance(r, dict)]
    times = [datetime.fromisoformat(r["collection"]["collected_at"]) for r in rows if (r.get("collection") or {}).get("collected_at")]
    lo, hi = min(times) - timedelta(minutes=30), max(times) + timedelta(minutes=5)
    qnorm = {r["case_id"]: norm_q(r.get("question") or "") for r in rows}
    found: List[Dict[str, Any]] = []
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp("http://127.0.0.1:9334")
        page = next(p for c in b.contexts for p in c.pages if p.url == "app://-/index.html")
        threads: Dict[str, Dict[str, str]] = {}
        stale = 0
        while stale < 4:
            before = len(threads)
            for t in page.evaluate(THREADS_JS):
                threads.setdefault(t["id"], t)
            moved = page.evaluate(SCROLL_SIDEBAR_JS)
            page.wait_for_timeout(500)
            stale = stale + 1 if (len(threads) == before or moved <= 0) else 0
        inside = [t for t in threads.values() if t["created"] and lo <= datetime.fromisoformat(t["created"].replace("Z", "+00:00")) <= hi]
        print(f"사이드바 대화 {len(threads)}개 · 이번 실행 GPT 수집 시간 안 {len(inside)}개", flush=True)
        try:
            for n, t in enumerate(sorted(inside, key=lambda x: x["created"])):
                if n < start:
                    continue
                if limit and n >= start + limit:
                    break
                ok = page.evaluate(CLICK_JS, t["id"])
                if not ok:   # 화면 밖이면 사이드바를 위로 감았다가 내려가며 찾는다
                    page.evaluate(SIDEBAR_TOP_JS)
                    for _ in range(60):
                        if page.evaluate(CLICK_JS, t["id"]):
                            ok = True
                            break
                        page.evaluate(SCROLL_SIDEBAR_JS)
                        page.wait_for_timeout(200)
                if not ok:
                    print(f"  · 못 엶: {t['title']}", flush=True)
                    continue
                page.wait_for_timeout(1800)
                seen: Dict[str, Dict[str, Any]] = {}
                order: List[str] = []
                copies: Dict[str, Optional[str]] = {}
                bottom_count: Dict[str, int] = {}
                attempts: Dict[str, int] = {}

                def recopy(key: str) -> Optional[str]:
                    """수집기(_copy_last_answer_once)와 같게 — 그 턴의 '복사' 버튼을 모두 눌러 가장 긴 글."""
                    turn = page.locator(f'[data-turn-key="{key}"]')
                    try:
                        turn.scroll_into_view_if_needed(timeout=3000)
                        turn.hover(timeout=3000)
                    except Exception:
                        pass
                    page.evaluate(HOOK_JS)
                    n = page.evaluate(BOTTOM_COPY_JS, key)
                    if n < 1:   # 도구 줄이 마우스를 올려야 그려질 수 있다 — 한 번 더
                        page.wait_for_timeout(600)
                        n = page.evaluate(BOTTOM_COPY_JS, key)
                    bottom_count[key] = n
                    if n < 1:
                        return None
                    page.wait_for_timeout(500)
                    got = [x for x in page.evaluate("() => window.__chk || []") if isinstance(x, str) and x.strip()]
                    return got[-1] if got else None

                def grab() -> None:
                    for tr in page.evaluate(TURNS_JS):
                        if not tr["key"]:
                            continue
                        if tr["key"] not in seen:
                            order.append(tr["key"])
                        if tr["key"] not in seen or len(tr["answer"]) > len(seen[tr["key"]]["answer"]):
                            seen[tr["key"]] = tr
                        if copies.get(tr["key"]) is None and tr["answer"].strip() and attempts.get(tr["key"], 0) < 3:   # 같은 답을 굴릴 때마다 끝없이 다시 시도하지 않게
                            attempts[tr["key"]] = attempts.get(tr["key"], 0) + 1
                            copies[tr["key"]] = recopy(tr["key"])

                # 긴 대화는 화면 근처 턴만 그려진다 — 위로 끝까지 올라가며 모으고, 다시 내려오며 모은다
                for direction in (-1, 1):
                    still = 0
                    for _ in range(120):
                        grab()
                        moved = page.evaluate(SCROLL_CHAT_JS, direction)
                        page.wait_for_timeout(450)
                        still = still + 1 if moved <= 0 else 0
                        if still >= 3:
                            break
                    grab()
                turns = [{**seen[k], "copied": copies.get(k), "bottom_buttons": bottom_count.get(k)} for k in order]
                for tr in turns:
                    u = norm_q(tr["user"])
                    cands = [cid for cid, q in qnorm.items() if q and (q in u or (len(q) > 60 and q[:60] in u and q[-40:] in u))]
                    tr["case_id"] = max(cands, key=lambda c: len(qnorm[c])) if cands else None
                found.append({"conversation": t["id"], "title": t["title"], "created": t["created"], "turns": turns})
                if on_conversation:
                    on_conversation(found[-1])
                print(f"  {n + 1:>3}. {t['title'][:24]:<24} 턴 {len(turns)} · 다시 복사 {sum(1 for x in turns if x['copied'])} → "
                      f"{sorted({x['case_id'] for x in turns if x['case_id']})[:3]}", flush=True)
        finally:
            page.evaluate(UNHOOK_JS)
    return found


def compare(run: Dict[str, Any], found: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = {r["case_id"]: r for r in run["datasets"]["gpt"] if isinstance(r, dict)}
    report: Dict[str, Any] = {"checked": [], "unmatched_turns": 0}
    for conv in found:
        for tr in conv["turns"]:
            cid = tr.get("case_id")
            if not cid or cid not in rows:
                report["unmatched_turns"] += 1
                continue
            stored = rows[cid].get("answer") or ""
            copied = tr.get("copied")
            same = copied is not None and _eol(copied) == _eol(stored)
            ratio = None if copied is None or same else round(difflib.SequenceMatcher(None, _eol(copied), _eol(stored), autojunk=False).ratio(), 4)
            first_diff = None
            if copied is not None and not same:
                a, b = _eol(copied), _eol(stored)
                i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
                first_diff = {"at": i, "copied": a[max(0, i - 30):i + 60], "stored": b[max(0, i - 30):i + 60]}
            fences = re.findall(r"```[^\n]*\n(.*?)\n?```", copied if copied is not None else stored, flags=re.S)
            raw_lines = [c.count("\n") + 1 for c in tr.get("codes") or []]
            got_lines = [c.count("\n") + 1 for c in fences]
            collapsed = [i + 1 for i, (a, b) in enumerate(zip(raw_lines, got_lines)) if a > 1 and b == 1]
            report["checked"].append({
                "app_code_lines": raw_lines, "copied_code_lines": got_lines, "code_collapsed_blocks": collapsed,
                "case_id": cid, "conversation": conv["conversation"], "status": (rows[cid].get("collection") or {}).get("status"),
                "recopy": "같음" if same else ("복사 실패" if copied is None else "다름"), "similarity": ratio, "first_diff": first_diff,
                "bottom_buttons": tr.get("bottom_buttons"),
                "coverage": round(coverage(tr["answer"], stored), 4), "visible_chars": len(tr["answer"]), "stored_chars": len(stored),
                "copied_chars": len(copied) if copied is not None else None})
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--limit", type=int, default=None, help="시험용 — 대화 몇 개만")
    ap.add_argument("--start", type=int, default=0, help="생성 순서로 몇 번째 대화부터(0부터) — 멈춘 뒤 이어서")
    args = ap.parse_args(argv)
    from tools.modelgrade import load_run
    run = load_run(args.run)
    live = ROOT / "data" / "judge_runs" / args.run / "gpt-recheck.jsonl"

    def save_one(conv: Dict[str, Any]) -> None:   # 대화마다 바로 비교해 한 줄씩 저장 — 중간에 멈춰도 남는다
        part = compare(run, [conv])
        with live.open("a", encoding="utf-8") as f:
            for c in part["checked"]:
                f.write(json.dumps({**c, "title": conv["title"]}, ensure_ascii=False) + "\n")

    found = collect(run, args.limit, args.start, save_one)
    report = compare(run, found)
    gpt_rows = [r for r in run["datasets"]["gpt"] if isinstance(r, dict)]
    matched = {c["case_id"] for c in report["checked"]}
    report["summary"] = {
        "conversations": len(found), "matched_turns": len(report["checked"]), "gpt_turns": len(gpt_rows),
        "recopy": dict(Counter(c["recopy"] for c in report["checked"])),
        "different": [(c["case_id"], c["similarity"]) for c in report["checked"] if c["recopy"] == "다름"],
        "bottom_button_not_one": [(c["case_id"], c["bottom_buttons"]) for c in report["checked"] if c.get("bottom_buttons") not in (1, None)],
        "code_collapsed": [c["case_id"] for c in report["checked"] if c.get("code_collapsed_blocks")],
        "not_found": sorted(r["case_id"] for r in gpt_rows if r["case_id"] not in matched and (r.get("collection") or {}).get("status") == "SUCCESS"),
        "low_coverage": [(c["case_id"], c["coverage"]) for c in report["checked"] if c["coverage"] < 0.97 and c["visible_chars"] > 20]}
    out = ROOT / "data" / "judge_runs" / args.run / "gpt-recheck.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    s = report["summary"]
    print(f"\n대화 {s['conversations']}개 · 맞춘 턴 {s['matched_turns']}/{s['gpt_turns']} · 다시 복사 {s['recopy']} · 다른 답 {s['different'][:10]}"
          f" · 답 아래 아이콘이 1개가 아닌 턴 {s['bottom_button_not_one'][:6]} · 복사에서 코드 줄바꿈이 사라진 턴 {s['code_collapsed']}"
          f" · 화면 글이 덜 들어간 답 {len(s['low_coverage'])}개 {s['low_coverage'][:8]} · 못 찾은 턴 {len(s['not_found'])}개 {s['not_found'][:10]} → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
