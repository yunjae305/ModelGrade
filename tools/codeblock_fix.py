"""코드블록 보정 — 답 아래 복사 글의 코드블록 안을 그 코드블록의 코드 복사 버튼 글로 바꾼다 (사용자 10-21 밤: GPT · Claude · AIONA 모두).

    python -X utf8 tools/codeblock_fix.py --run a1642702fccc --provider gpt|claude|aiona [--only Q22_T1,…] [--cached] [--apply]

- 코드블록 밖 글은 답 아래 복사 글 그대로. ``` 뒤 언어가 비어 있으면 화면 코드블록 머리 글(Python · JSON …)을 LANGS 표로 바꿔 붙인다
  (프로그램 규칙 — ChatGPT 앱의 답 아래 복사는 언어를 지운다. Claude · AIONA 는 복사 글에 언어가 이미 있다).
- 안전장치: 받은 코드가 저장된 코드와 공백 · 줄바꿈을 빼고 같은 글자일 때만 바꾼다. 다르면 클립보드에 다른 글이 섞인 것으로 보고 그대로 둔다
  (10-21 GPT Q14: 동시에 돌린 Gemini 앱 복사 시험 글이 들어옴). 시스템 클립보드를 쓰는 GPT · Claude · Gemini 앱 작업은 동시에 돌리지 않는다.
- 화면: GPT = ChatGPT 앱(디버깅 포트 9334) · Claude = Claude 앱(UIA — 채팅 탭으로 바꾸고 마우스 · 시스템 클립보드를 쓴다, 끝나면 Code 탭으로)
  · AIONA = 수집 프로필 브라우저(창 없음, 페이지 안 복사 가로채기). Gemini 는 버전이 바뀌어 질문부터 다시 수집한다(이 도구 대상 아님).
- 메시지를 보내거나 설정을 바꾸지 않는다. 받은 코드블록은 data/judge_runs/<실행>/<모델>_codeblocks.json 에 남기고 --cached 면 다시 누르지 않는다.
- --apply 가 없으면 확인 파일(참고자료/결과/판정_<실행>/<모델>_답_보정_확인.md)만 쓴다. --apply 는 원래 글을 answer_before_codeblock_fix 에 남기고
  그 모델의 CODE 평가를 다시 한다(LLM Judge 는 다음 판정 때 새 글로).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.uia_chat_app import _clipboard_get, _clipboard_set  # noqa: E402

NAMES = {"gpt": "GPT", "claude": "Claude", "aiona": "AIONA"}
FENCE = re.compile(r"```([^\n`]*)\n(.*?)\n?```", re.S)
CODE_BLOCKS_JS = r"""(key) => {
  const t = document.querySelector(`[data-turn-key="${key}"]`); if (!t) return null;
  const ub = t.querySelector('[data-user-message-bubble]');
  const blocks = [...t.querySelectorAll('[data-markdown-copy="code-block"]')].filter(b => !(ub && ub.contains(b)));
  document.querySelectorAll('[data-mg-cb]').forEach(x => x.removeAttribute('data-mg-cb'));
  blocks.forEach((b, i) => b.setAttribute('data-mg-cb', String(i)));   // 답 안 코드블록에만 번호 — 질문 말풍선 속 코드블록은 제외
  return blocks.map(b => { const h = (b.innerText || '').split('\n').map(s => s.trim()).filter(Boolean); return h[0] || ''; });
}"""
MARK = "MG_CODECOPY_MARK"
CLAUDE_CODE_COPY = re.compile(r"^(클립보드에 복사|Copy to clipboard|코드 복사|Copy code)$")   # 답 안 코드블록 버튼(10-21 확인) — 답 아래 '메시지 작업'의 '복사'와 이름이 다르다

LANGS = {"python": "python", "json": "json", "javascript": "javascript", "js": "javascript", "typescript": "typescript", "bash": "bash",
         "sql": "sql", "html": "html", "css": "css", "java": "java", "c++": "cpp", "cpp": "cpp", "go": "go", "rust": "rust", "yaml": "yaml",
         "text": "text", "plaintext": "text", "markdown": "markdown", "csv": "csv"}


def capture_gpt(run: Dict[str, Any], targets: List[str], out: Dict[str, Tuple[List[str], List[str]]]) -> None:
    """GPT — ChatGPT 앱(9334). apply_bottom_copy.gpt_copies 와 같은 길로 채팅을 열고, 코드블록마다 마우스를 올려 '복사'를 실제로 누른다
    (앱의 코드블록 복사는 시스템 클립보드에 바로 쓰고 스크립트 클릭에는 반응하지 않는다, 10-21)."""
    from playwright.sync_api import sync_playwright
    from tools import gpt_app_recheck as g
    rows = {r["case_id"]: r for r in run["datasets"]["gpt"]}
    ts = [datetime.fromisoformat(r["collection"]["collected_at"]) for r in rows.values() if (r.get("collection") or {}).get("collected_at")]
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
        current = None
        try:
            for cid in targets:
                q = cid.split("_")[0]
                want = g.norm_q(rows[cid]["question"])
                conv = inside[int(q[1:])]   # 생성 순서 0 은 수집 전 다른 채팅, 1 부터 Q1…
                if conv["id"] != current:
                    page.evaluate(g.SIDEBAR_TOP_JS)
                    for _ in range(60):
                        if page.evaluate(g.CLICK_JS, conv["id"]):
                            break
                        page.evaluate(g.SCROLL_SIDEBAR_JS)
                        page.wait_for_timeout(200)
                    page.wait_for_timeout(2500)
                    current = conv["id"]
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
                loc = page.locator(f'[data-turn-key="{key}"]')
                loc.scroll_into_view_if_needed()
                page.wait_for_timeout(500)
                heads = page.evaluate(CODE_BLOCKS_JS, key) or []
                codes: List[str] = []
                for i in range(len(heads)):
                    got = None
                    blk = page.locator(f'[data-mg-cb="{i}"]').first
                    for _attempt in range(3):   # 마우스를 올리고 실제 클릭 — 스크립트 클릭은 반응하지 않음(10-21)
                        _clipboard_set(MARK)
                        blk.scroll_into_view_if_needed()
                        blk.hover()
                        page.wait_for_timeout(300)
                        blk.locator('button[aria-label="복사"]').first.click(force=True, timeout=5000)
                        page.wait_for_timeout(700)
                        text = _clipboard_get().replace("\r\n", "\n")
                        if text and text.strip() != MARK:
                            got = text
                            break
                    codes.append(got if got is not None else "")
                out[cid] = (codes, heads)
                print(f"  {cid}: 코드블록 {len(heads)}개 · 받음 {sum(1 for c in codes if c)}개 · 머리 {heads}", flush=True)
        finally:
            page.evaluate(g.UNHOOK_JS)


def capture_aiona(run: Dict[str, Any], targets: List[str], out: Dict[str, Tuple[List[str], List[str]]]) -> None:
    """AIONA — 저장된 채팅 주소를 열고 그 턴 답 칸의 코드블록(pre)마다 안의 '복사'를 누른다(페이지 안 가로채기 __mgCopied)."""
    from webapp.aiona_web import AionaChat
    rows = {r["case_id"]: r for r in run["datasets"]["aiona"]}
    by_url: Dict[str, List[str]] = defaultdict(list)
    for cid in targets:
        r = rows[cid]
        url = (r.get("collection") or {}).get("url") or next((x["collection"]["url"] for x in rows.values()   # 재시도로 받은 행은 주소가 비어 있다 — 같은 대화 다른 턴의 주소
                                                               if x.get("conversation_id") == r.get("conversation_id") and (x.get("collection") or {}).get("url")), None)
        if url:
            by_url[url].append(cid)
        else:
            print(f"  {cid}: 채팅 주소 없음", flush=True)
    with AionaChat(headless=True, allow_login=False, answer_timeout_s=60) as chat:
        for url, cids in by_url.items():
            try:
                chat.page.goto(url, wait_until="domcontentloaded")
                chat.page.wait_for_timeout(2500)
                chat._dismiss_popups()
                chat._answers().first.wait_for(timeout=20_000)
            except Exception as exc:
                print(f"  {cids}: 채팅 열기 실패 {type(exc).__name__}", flush=True)
                continue
            for cid in cids:
                idx = int(rows[cid].get("turn") or 1) - 1
                if chat._answers().count() <= idx:
                    print(f"  {cid}: 답 칸 없음", flush=True)
                    continue
                pres = chat._answers().nth(idx).locator("pre")
                codes: List[str] = []
                heads: List[str] = []
                for k in range(pres.count()):
                    pre = pres.nth(k)
                    lines = [s.strip() for s in (pre.inner_text() or "").split("\n") if s.strip()]
                    heads.append(lines[0] if lines else "")
                    btn = pre.locator("button[title='복사']").first
                    got = None
                    try:
                        chat.page.evaluate("() => { window.__mgCopied = []; }")
                        try:
                            btn.click(timeout=5000)
                        except Exception:
                            btn.dispatch_event("click")
                        chat.page.wait_for_function("() => (window.__mgCopied || []).length > 0", timeout=5000)
                        got = chat.page.evaluate("() => window.__mgCopied[window.__mgCopied.length - 1]")
                    except Exception:
                        pass
                    codes.append(got or "")
                out[cid] = (codes, heads)
                print(f"  {cid}: 코드블록 {len(heads)}개 · 받음 {sum(1 for c in codes if c)}개 · 머리 {heads}", flush=True)


def capture_claude(run: Dict[str, Any], targets: List[str], out: Dict[str, Tuple[List[str], List[str]]]) -> None:
    """Claude — 앱 '모두 보기' 목록에서 채팅 제목(claude-recheck.jsonl)으로 채팅을 열고, 질문이 맞는 말풍선 다음 답의 '클립보드에 복사'를 누른다."""
    from pywinauto import mouse
    from tools import claude_app
    from tools.claude_app_recheck import Recheck
    from tools.gpt_app_recheck import norm_q
    from webapp.uia_chat_app import ClaudeAppChat
    rows = {r["case_id"]: r for r in run["datasets"]["claude"]}
    recheck = ROOT / "data" / "judge_runs" / run["id"] / "claude-recheck.jsonl"
    titles: Dict[str, str] = {}
    if recheck.exists():
        for line in recheck.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                titles.setdefault(rec["case_id"], rec.get("title"))
    by_title: Dict[str, List[str]] = defaultdict(list)
    for cid in targets:
        if titles.get(cid):
            by_title[titles[cid]].append(cid)
        else:
            print(f"  {cid}: 채팅 제목 모름(claude-recheck.jsonl 에 없음)", flush=True)
    qn = {cid: norm_q(rows[cid].get("question") or "") for cid in targets}
    rc = Recheck(claude_app.main_window(claude_app.status()["pids"]))
    rc.tab("Claude")
    try:
        for title, cids in by_title.items():
            rc.open_list()
            link = None
            for _ in range(30):   # 목록은 내려야 더 불러온다
                link = next((l for l in rc.list_links() if (l.element_info.name or "") == title), None)
                if link is not None:
                    break
                rc.wheel(-10)
            if link is None:
                print(f"  {cids}: 목록에서 채팅 '{title}' 못 찾음", flush=True)
                continue
            ClaudeAppChat._press(link)
            time.sleep(3)
            total = max((x[1] for x in rc.groups()), default=0)

            def find_group(idx: int) -> Any:
                for _ in range(40):   # 긴 대화는 화면 근처만 그려진다 — 위 · 아래로 굴려 찾는다
                    cur = rc.groups()
                    hit = next((x[2] for x in cur if x[0] == idx), None)
                    if hit is not None:
                        return hit
                    lo = min((x[0] for x in cur), default=idx)
                    rc.wheel(10 if idx < lo else -10)
                return None

            want = None
            for idx in range(1, total + 1):
                g = find_group(idx)
                if g is None:
                    continue
                if rc.role(g) == "user":
                    u = norm_q(rc.group_text(g))
                    hits = [c for c in cids if qn[c] and (qn[c] in u or (len(qn[c]) > 60 and qn[c][:60] in u and qn[c][-40:] in u))]
                    want = max(hits, key=lambda c: len(qn[c])) if hits else None
                    continue
                if want is None or want in out:
                    continue
                rc.bring(g)
                r = g.element_info.rectangle
                try:
                    mouse.move(coords=((r.left + r.right) // 2, max(r.top + 20, min(r.bottom - 30, r.top + 200))))
                except Exception:
                    pass   # 화면이 잠기면 마우스를 못 옮긴다 — 코드블록 버튼은 UIA 로 누르므로 그대로 해 본다
                time.sleep(0.8)
                bar_ids = {tuple(b.element_info.runtime_id) for bar in rc.all("ToolBar") if (bar.element_info.name or "").startswith("메시지 작업")
                           for b in bar.descendants(control_type="Button")}
                codes: List[str] = []
                heads: List[str] = []
                for b in [b for b in g.descendants(control_type="Button")
                          if tuple(b.element_info.runtime_id) not in bar_ids and CLAUDE_CODE_COPY.match(b.element_info.name or "")]:
                    p, head = b.parent(), ""
                    for _ in range(3):   # 코드블록 머리 글(python 등) — 버튼과 같은 상자 안 첫 글
                        t = [x.element_info.name for x in p.descendants(control_type="Text") if (x.element_info.name or "").strip()]
                        if t:
                            head = t[0]
                            break
                        p = p.parent()
                    got = None
                    for _attempt in range(3):
                        _clipboard_set(MARK)
                        ClaudeAppChat._press(b)
                        for _ in range(15):
                            time.sleep(0.3)
                            text = _clipboard_get()
                            if text and text != MARK:
                                got = text.replace("\r\n", "\n")
                                break
                        if got is not None:
                            break
                    codes.append(got or "")
                    heads.append(head)
                out[want] = (codes, heads)
                print(f"  {want}: 코드블록 {len(heads)}개 · 받음 {sum(1 for c in codes if c)}개 · 머리 {heads}", flush=True)
                want = None
    finally:
        rc.tab("Code")


def _bare(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _lines(text: str) -> List[str]:
    return [l.rstrip() for l in (text or "").replace("\r\n", "\n").rstrip("\n").split("\n")]


def merge_codes(answer: str, codes: List[str], heads: List[str]) -> Tuple[Optional[str], str]:
    """저장된 답의 n번째 ``` 안을 n번째 코드로 — (새 답 또는 None, 설명). 코드가 같으면 그 코드블록은 손대지 않고(언어만 붙일 수 있음),
    공백 · 줄바꿈을 빼고 글자가 다른 코드블록이 하나라도 있으면 아무것도 바꾸지 않는다(안전장치)."""
    fences = list(FENCE.finditer(answer))
    if not fences:
        return None, "저장된 답에 코드블록 없음"
    if len(fences) != len(codes) or any(not c for c in codes):
        return None, f"코드블록 수가 다름(저장 {len(fences)} · 화면 {len(codes)}) 또는 못 받은 코드블록 — 그대로 둠"
    bad = [k + 1 for k, (m, c) in enumerate(zip(fences, codes)) if _bare(m.group(2)) != _bare(c)]
    if bad:
        return None, f"코드블록 {bad}번에서 받은 글이 저장된 코드와 글자가 다름(클립보드에 다른 글이 섞였을 수 있음) — 그대로 둠"
    eol = "\r\n" if "\r\n" in answer else "\n"
    parts, pos, n_code, n_lang = [], 0, 0, 0
    for m, code, head in zip(fences, codes, heads):
        lang0 = m.group(1).strip()
        lang = lang0 or LANGS.get((head or "").strip().lower(), "")
        parts.append(answer[pos:m.start()])
        if _lines(m.group(2)) == _lines(code):
            block = m.group(0) if lang == lang0 else "```" + lang + m.group(0)[3 + len(m.group(1)):]
        else:
            block = "```" + lang + eol + eol.join(_lines(code)) + eol + "```"
            n_code += 1
        n_lang += int(lang != lang0)
        parts.append(block)
        pos = m.end()
    parts.append(answer[pos:])
    new = "".join(parts)
    if new == answer:
        return None, f"코드블록 {len(fences)}개 모두 코드 복사 글과 같음"
    return new, f"코드블록 {len(fences)}개 중 코드 바꿈 {n_code} · 언어 표시 붙임 {n_lang}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--provider", required=True, choices=sorted(NAMES))
    ap.add_argument("--only", default="", help="쉼표로 나눈 case_id 만")
    ap.add_argument("--cached", action="store_true", help="앞서 받아 둔 <모델>_codeblocks.json 을 쓴다(화면을 다시 누르지 않음)")
    ap.add_argument("--resume", action="store_true", help="받아 둔 파일에 이미 있는 턴은 건너뛰고 남은 턴만 화면에서 받는다(멈춘 뒤 이어서)")
    ap.add_argument("--apply", action="store_true", help="실행 파일의 그 모델 답을 바꾸고 CODE 평가를 다시 한다")
    args = ap.parse_args(argv)
    from tools.modelgrade import code_evaluate, load_run, merge_provider_into_run
    prov, name = args.provider, NAMES[args.provider]
    run = load_run(args.run)
    run.setdefault("id", args.run)
    rows = [dict(r) for r in run["datasets"][prov]]
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    code_targets = [r["case_id"] for r in rows if "```" in (r.get("answer") or "") and (not only or r["case_id"] in only)]
    cache = ROOT / "data" / "judge_runs" / args.run / f"{prov}_codeblocks.json"
    if args.cached:
        captured = {k: tuple(v) for k, v in json.loads(cache.read_text(encoding="utf-8")).items() if k in code_targets}
        print(f"{name} 받아 둔 코드블록 {len(captured)}턴 — {cache}", flush=True)
    else:
        old = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
        todo = [c for c in code_targets if not (args.resume and c in old)]
        print(f"{name} 코드블록이 있는 턴 {len(code_targets)}개 — 화면에서 코드블록 복사 {len(todo)}개", flush=True)
        capture = {"gpt": capture_gpt, "claude": capture_claude, "aiona": capture_aiona}[prov]
        got: Dict[str, Tuple[List[str], List[str]]] = {}
        try:
            if todo:
                capture(run, todo, got)
        finally:   # 중간에 멈춰도 받은 만큼은 남긴다 (10-21 Claude: 화면 잠김으로 12턴에서 멈춤)
            old.update({k: list(v) for k, v in got.items()})
            cache.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
        captured = {k: tuple(v) for k, v in old.items() if k in code_targets}
    changed: List[Tuple[str, str]] = []
    kept: List[Tuple[str, str]] = []
    body: List[str] = []
    for r in rows:
        cid = r["case_id"]
        if cid not in code_targets:
            continue
        before = r.get("answer") or ""
        if cid not in captured:
            kept.append((cid, "화면에서 못 받음"))
            continue
        new, why = merge_codes(before, *captured[cid])
        if new is None:
            kept.append((cid, why))
            continue
        changed.append((cid, why))
        body += [f"## {cid} — 바꿈", "", f"- {why}", "", "**전**", "", "````", before[:4000], "````", "", "**후**", "", "````", new[:4000], "````", ""]
        if args.apply:
            r["answer_before_codeblock_fix"] = r.get("answer_before_codeblock_fix") or before
            r["answer"] = new
            r["collection"] = {**(r.get("collection") or {}), "codeblock_fix": {"at": datetime.now(timezone.utc).isoformat(), "note": why}}
    report = [f"# {name} 답 코드블록 보정 확인 — 실행 {args.run} ({datetime.now().strftime('%Y-%m-%d %H:%M')})", "",
              "답 아래 복사 글의 코드블록 안만 그 코드블록의 코드 복사 버튼 글로 바꿈(코드블록 밖 글은 그대로). 받은 코드가 저장된 코드와 공백 · 줄바꿈을 빼고 "
              "글자가 같을 때만 바꾼다. ``` 뒤 언어가 비어 있으면 화면 코드블록 머리 글을 붙인다.", "",
              f"- 코드블록 있는 턴 {len(code_targets)} · **바꿈 {len(changed)}** · 그대로 {len(kept)}", "",
              "| 턴 | 결과 |", "|---|---|"]
    report += [f"| {c} | 바꿈 — {w} |" for c, w in changed] + [f"| {c} | 그대로 — {w} |" for c, w in kept] + [""]
    out = ROOT / "참고자료" / "결과" / f"판정_{args.run}" / f"{name}_답_보정_확인.md"
    out.write_text("\n".join(report + body), encoding="utf-8")
    print(f"{name} 바뀌는 턴 {len(changed)}개 · 그대로 {len(kept)}개 — 확인 파일 {out}", flush=True)
    if args.apply and changed:
        merge_provider_into_run(args.run, prov, run["models"].get(prov) or prov, rows, code_evaluate(rows, prov), None, backup=True)
        print(f"실행 파일에 반영 · {name} CODE 평가 다시 함", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
