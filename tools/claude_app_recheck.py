"""Claude(데스크톱 앱) 답 수집 확인 — 앱의 '최근 항목 → 모두 보기' 목록에서 채팅을 하나씩 열어, 답마다 답 아래 '메시지 작업' 도구 줄의 '복사'를
눌러(수집기와 같은 방식, 실제 클립보드) 나온 글을 저장된 답과 비교한다 (사용자 요청 2026-10-21). 복사 결과를 고치지 않는다.

    python -X utf8 tools/claude_app_recheck.py --run a1642702fccc [--start 0] [--limit N] [--max-chats 120]
    → data/judge_runs/<실행>/claude-recheck.jsonl (턴마다 바로 한 줄씩) · claude-recheck.json (요약)

- 앱 창을 채팅 탭으로 바꾸고 마우스를 말풍선 위로 옮기며 클립보드를 쓴다 — 도는 동안 PC 를 쓰지 않는다. 끝나면 Code 탭으로 되돌린다.
- 채팅의 '입력 내용' 글을 실행의 질문과 대조해 Q번호·턴을 정한다. 실행 뒤에 이어서 물은 메시지처럼 질문과 맞지 않는 턴은 건너뛴다.
- 메시지를 보내거나 설정을 바꾸지 않는다(누르는 것: 탭·'모두 보기'·채팅 링크·'뒤로'·답의 '복사').
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.gpt_app_recheck import norm_q  # noqa: E402
from webapp.uia_chat_app import ClaudeAppChat, _clipboard_get, _clipboard_set  # noqa: E402

GROUP = re.compile(r"^(\d+)개 메시지 중 (\d+)번째$")
SENTINEL = "__claude_recheck_sentinel__"


def _eol(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n")).strip()


class Recheck:
    def __init__(self, win: Any) -> None:
        self.win = win

    def all(self, kind: Optional[str] = None) -> List[Any]:
        return self.win.descendants(control_type=kind) if kind else self.win.descendants()

    def tab(self, prefix: str) -> None:
        for r in self.all("RadioButton"):
            if (r.element_info.name or "").startswith(prefix):
                if not r.is_selected():
                    r.select()
                    time.sleep(1.2)
                return

    def open_list(self) -> None:
        btn = next((e for e in self.all("Button") if (e.element_info.name or "") == "모두 보기"), None)
        if btn is None:
            raise RuntimeError("사이드바 '모두 보기' 를 찾지 못함")
        ClaudeAppChat._press(btn)
        time.sleep(2.5)

    def list_links(self) -> List[Any]:
        return [e for e in self.all("Hyperlink") if e.element_info.rectangle.left > 300 and (e.element_info.name or "") not in ("새 세션",)
                and not (e.element_info.name or "").startswith("앱 및 확장")]

    def link_url(self, link: Any) -> str:
        try:
            return str(link.legacy_properties().get("Value") or "")
        except Exception:
            return ""

    def wheel(self, dist: int) -> None:
        from pywinauto import mouse
        r = self.win.rectangle()
        mouse.scroll(coords=((r.left + r.right) // 2 + 150, (r.top + r.bottom) // 2), wheel_dist=dist)
        time.sleep(0.7)

    def groups(self) -> List[Any]:
        out = []
        for g in self.all("Group"):
            m = GROUP.match(g.element_info.name or "")
            if m:
                out.append((int(m.group(2)), int(m.group(1)), g))
        return sorted(out, key=lambda x: x[0])

    @staticmethod
    def group_text(g: Any) -> str:
        parts = []
        for d in g.descendants():
            i = d.element_info
            if i.name and i.control_type not in ("Button", "ToolBar"):
                parts.append(i.name)
        return "\n".join(parts)

    @staticmethod
    def role(g: Any) -> str:
        for d in g.descendants(control_type="Text"):
            n = d.element_info.name or ""
            if n.startswith("입력 내용: "):
                return "user"
            if n.startswith("Claude 응답: "):
                return "assistant"
        return "assistant"   # 답이 코드블록 하나뿐이면 'Claude 응답:' 머리글이 없다(Q11~16·Q20, 10-21) — 내 질문이 아니면 답

    def bring(self, g: Any) -> None:
        try:
            g.iface_scroll_item.ScrollIntoView()
        except Exception:
            pass
        time.sleep(0.4)

    def copy_answer(self, g: Any) -> Optional[str]:
        """답 말풍선 아래 '메시지 작업' 도구 줄의 '복사' — 말풍선을 화면에 끌어와 마우스를 올려 도구 줄을 띄운 뒤 누른다."""
        for attempt in range(3):
            self.bring(g)
            r = g.element_info.rectangle
            try:
                from pywinauto import mouse
                mouse.move(coords=((r.left + r.right) // 2, max(r.top + 20, min(r.bottom - 30, r.top + 200))))
            except Exception:
                pass
            time.sleep(1.0)
            r = g.element_info.rectangle
            bars = []
            for bar in self.all("ToolBar"):
                if not (bar.element_info.name or "").startswith("메시지 작업"):
                    continue
                names = [b.element_info.name or "" for b in bar.descendants(control_type="Button")]
                if any(re.match(ClaudeAppChat.COPY, n) for n in names) and not any(n.startswith(("편집", "Edit")) for n in names):
                    t = bar.element_info.rectangle.top
                    if r.top - 10 <= t <= r.bottom + 120:
                        bars.append(bar)
            if not bars:
                self.wheel(-3 if attempt == 1 else 3)
                continue
            btn = next(b for b in bars[0].descendants(control_type="Button") if re.match(ClaudeAppChat.COPY, b.element_info.name or ""))
            _clipboard_set(SENTINEL)
            ClaudeAppChat._press(btn)
            for _ in range(12):
                time.sleep(0.4)
                text = _clipboard_get()
                if text and text != SENTINEL:
                    return text
        return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--start", type=int, default=0, help="목록(최신순)에서 몇 번째 채팅부터")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--max-chats", type=int, default=130, help="목록에서 이만큼까지만 본다")
    ap.add_argument("--since", help="이 시각(UTC ISO) 뒤에 받은 행만 — 일부만 새로 받은 실행(10-22 멀티턴 다시 받기)")
    args = ap.parse_args(argv)
    from tools import claude_app
    from tools.modelgrade import load_run
    run = load_run(args.run)
    rows = {r["case_id"]: r for r in run["datasets"]["claude"] if isinstance(r, dict)
            and (not args.since or (r.get("collection") or {}).get("collected_at", "") >= args.since)}
    print(f"확인할 Claude 행 {len(rows)}개", flush=True)
    qnorm = {cid: norm_q(r.get("question") or "") for cid, r in rows.items()}
    # 앱은 질문의 마크다운을 그려 보여 준다 — 목록 '-' · 제목 '#' 등 기호를 빼고 앞부분으로 맞출 때 쓴다(10-22 멀티턴)
    md = lambda t: re.sub(r"[\s`*#|_>\-]+", "", (t or "").lower())  # noqa: E731
    qmd = {cid: md(r.get("question") or "") for cid, r in rows.items()}
    need = {cid for cid, r in rows.items() if (r.get("collection") or {}).get("status") == "SUCCESS"}
    out_dir = ROOT / "data" / "judge_runs" / args.run
    live = out_dir / "claude-recheck.jsonl"
    done_cids = set()
    if live.exists():
        done_cids = {json.loads(l)["case_id"] for l in live.read_text(encoding="utf-8").splitlines() if l.strip()}
    s = claude_app.status()
    rc = Recheck(claude_app.main_window(s["pids"]))
    checked: List[Dict[str, Any]] = []
    seen_urls: set = set()
    rc.tab("Claude")
    try:
        k = args.start
        n_opened = 0
        while k < args.max_chats and (args.limit is None or n_opened < args.limit) and not need <= done_cids:
            rc.open_list()
            links = rc.list_links()
            tries = 0
            while len(links) <= k and tries < 30:   # 목록은 내려야 더 불러온다
                rc.wheel(-10)
                links = rc.list_links()
                tries += 1
            if len(links) <= k:
                print(f"목록 끝 — {len(links)}개", flush=True)
                break
            link = links[k]
            title, url = link.element_info.name or "", rc.link_url(link)
            k += 1
            if url in seen_urls:
                continue
            seen_urls.add(url)
            ClaudeAppChat._press(link)
            time.sleep(2.5)
            n_opened += 1
            gs = rc.groups()
            total = max((x[1] for x in gs), default=0)

            def find_group(idx: int) -> Any:
                """idx 번째 말풍선 — 긴 대화는 화면 근처만 그려지므로 위·아래로 굴려 찾는다."""
                for _ in range(40):
                    cur = rc.groups()
                    hit = next((x[2] for x in cur if x[0] == idx), None)
                    if hit is not None:
                        return hit
                    lo = min((x[0] for x in cur), default=idx)
                    rc.wheel(10 if idx < lo else -10)
                return None

            def walk() -> Any:
                """맨 위로 올린 뒤 아래로 조금씩 내리며 말풍선을 번호 순서대로 내준다 — 말풍선이 아주 긴 대화(10-22 멀티턴 1만 7천 자 질문 ·
                수천 자 답)는 번호로 찾아 굴리는 find_group 이 대부분 못 찾았다."""
                for _ in range(300):
                    if any(x[0] == 1 for x in rc.groups()):
                        break
                    rc.wheel(15)
                nxt, still = 1, 0
                while nxt <= total and still < 40:
                    cur = {x[0]: x[2] for x in rc.groups()}
                    if nxt in cur:
                        still = 0
                        yield cur[nxt]
                        nxt += 1
                        continue
                    still += 1
                    rc.wheel(-4 if min(cur, default=nxt) <= nxt else 4)

            results = []
            last_user = None
            for g in walk():
                role = rc.role(g)
                if role == "user":
                    last_user = rc.group_text(g)
                    continue
                if role != "assistant" or last_user is None:
                    continue
                u, last_text = norm_q(last_user), last_user
                last_user = None
                cands = [cid for cid, q in qnorm.items() if q and (q in u or (len(q) > 60 and q[:60] in u and q[-40:] in u))]
                if not cands:   # 긴 질문은 앱이 '더 보기'로 접어 끝부분이 화면에 없다(10-22 멀티턴 1만 6천 자) — 앞 80자로(턴마다 자료 제목이 달라 하나로 정해질 때만)
                    um = md(last_text)
                    pre = [cid for cid, q in qmd.items() if len(q) > 80 and q[:80] in um]
                    cands = pre if len(pre) == 1 else []
                cid = max(cands, key=lambda c: len(qnorm[c])) if cands else None
                if not cid or cid in done_cids:
                    continue
                copied = rc.copy_answer(g)
                stored = rows[cid].get("answer") or ""
                same = copied is not None and _eol(copied) == _eol(stored)
                rec = {"case_id": cid, "title": title, "url": url, "recopy": "같음" if same else ("복사 실패" if copied is None else "다름"),
                       "similarity": None if same or copied is None else round(difflib.SequenceMatcher(None, _eol(copied), _eol(stored), autojunk=False).ratio(), 4),
                       "copied_chars": len(copied) if copied is not None else None, "stored_chars": len(stored)}
                if copied is not None and not same:
                    a, b = _eol(copied), _eol(stored)
                    j = next((x for x in range(min(len(a), len(b))) if a[x] != b[x]), min(len(a), len(b)))
                    rec["first_diff"] = {"at": j, "copied": a[max(0, j - 30):j + 60], "stored": b[max(0, j - 30):j + 60]}
                results.append(rec)
                done_cids.add(cid)
                with live.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            checked += results
            print(f"  {k:>3}. {title[:22]:<22} 말풍선 {total} · 실행 턴 {len(results)} · {dict(Counter(r['recopy'] for r in results))} · {sorted(r['case_id'] for r in results)[:3]}", flush=True)
    finally:
        rc.tab("Code")
    remaining = sorted(need - done_cids)
    summary = {"checked": len(checked), "recopy": dict(Counter(c["recopy"] for c in checked)),
               "different": [(c["case_id"], c["similarity"]) for c in checked if c["recopy"] == "다름"],
               "copy_failed": [c["case_id"] for c in checked if c["recopy"] == "복사 실패"], "not_checked": remaining}
    (out_dir / "claude-recheck.json").write_text(json.dumps({"summary": summary, "checked": checked}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n끝: 확인 {summary['checked']} · {summary['recopy']} · 다름 {summary['different'][:8]} · 복사 실패 {summary['copy_failed'][:8]} · 못 본 턴 {len(remaining)}개 {remaining[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
