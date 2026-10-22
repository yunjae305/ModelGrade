"""Gemini(데스크톱 앱) 답 수집 확인 — 앱 사이드바의 채팅을 하나씩 열어, 답마다 답 아래 '복사'(대답 복사)를 수집기와 똑같이 눌러
나온 글을 저장된 답과 비교한다 (사용자 규칙 10-21 '수집 확인 뒤에만 판정', 10-22). 복사 결과를 고치지 않는다.

    python -X utf8 tools/gemini_app_recheck.py --run 50ca3ccccace [--since 2026-10-21T14:28] [--max-chats 110]
    → data/judge_runs/<실행>/gemini-recheck.jsonl (턴마다 바로 한 줄씩) · gemini-recheck.json (요약)

- 대조 기준: 수집 때 답 아래 복사로 받은 글 그대로(collection.answer_bottom_copy → answer_before_table_fix → answer) — 코드블록 ·
  표 행 보정 전 글. 줄끝 · 끝 공백만 무시.
- 채팅의 질문 칸 글을 실행의 질문과 대조해 Q번호 · 턴을 정한다(앞 80자 — 멀티턴은 턴마다 자료 제목이 달라 하나로 정해진다).
  같은 질문이 여러 채팅에 있으면(실패해 다시 받은 경우) 사이드바에서 위(최근)에 있는 채팅 것만 본다.
- 창을 앞으로 가져오고 클립보드를 쓴다 — 도는 동안 PC 를 쓰지 않는다. 메시지를 보내거나 설정을 바꾸지 않는다
  (누르는 것: '기본 메뉴' · 채팅 링크 · 답의 '복사').
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
from webapp.gemini_app_chat import GeminiAppChat  # noqa: E402

QUERY_CLASS = "user-query-bubble-with-background"
FIXED_LINKS = {"새 채팅", "라이브러리", "이미지", "예약됨", "맞춤설정", "동영상", "새 노트북"}


def _eol(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n")).strip()


def stored_copy(row: Dict[str, Any]) -> str:
    c = row.get("collection") or {}
    return c.get("answer_bottom_copy") or c.get("answer_before_table_fix") or row.get("answer") or ""


class Recheck(GeminiAppChat):
    """수집기의 찾기 · 복사 함수를 그대로 쓴다(새 채팅 · 모델 고르기 · 보내기는 쓰지 않음)."""

    def __init__(self, win: Any) -> None:   # noqa: D401 — 수집기 __init__/__enter__ 는 앱을 다시 띄우므로 쓰지 않는다
        self.win = win
        self.debug_dir = None

    def chat_links(self) -> List[Any]:
        """사이드바 '채팅' 목록의 링크(위가 최근) — 고정 메뉴 · 노트북 링크는 뺀다."""
        # 사이드바 채팅 링크마다 옆에 '<제목> 옵션 더보기' 버튼이 있다 — 그 짝이 있는 링크만(10-22: 창 전체에서 찾으니 대화 아래
        # '개인 정보 보호 및 Gemini(새 창에서 열림)' 링크를 채팅으로 알고 눌렀다). 사이드바가 닫혀 있으면 빈 목록.
        titles = {re.sub(r"\s*옵션 더보기$", "", b.element_info.name or "").strip()
                  for b in self._all("Button") if (b.element_info.name or "").endswith("옵션 더보기")}
        return [h for h in self._all("Hyperlink") if (h.element_info.name or "").strip() in titles
                and (h.element_info.name or "").strip() not in FIXED_LINKS and "새 창" not in (h.element_info.name or "")]

    def ensure_sidebar(self) -> None:
        if self.chat_links():
            return
        opener = self._find("Button", self.SIDEBAR_OPEN)
        if opener is not None:
            self._press(opener)
            time.sleep(1.2)

    def blocks(self) -> List[Any]:
        """채팅 화면의 질문 칸 · 답 묶음을 화면 순서대로 [(종류, 요소)]."""
        out = []
        for g in self._all("Group"):
            head = (g.element_info.class_name or "").split(" ")[0]
            if head == QUERY_CLASS:
                out.append(("q", g))
            elif head == self.RESPONSE_CLASS:
                out.append(("a", g))
        return out

    @staticmethod
    def text_of(g: Any) -> str:
        try:
            return "\n".join((e.element_info.name or "") for e in g.descendants() if e.element_info.control_type != "Button")
        except Exception:
            return ""

    def load_all_turns(self, expected: Optional[int] = None) -> None:
        """긴 대화는 위로 올려야 앞 턴을 더 불러온다 — expected(대화의 턴 수)를 알면 그만큼 보일 때까지(최대 약 5분), 모르면
        질문 칸 수가 늘지 않을 때까지 맨 위로 굴린다. Gemini 는 앞 턴을 몇 초씩 걸려 하나씩 불러온다(10-22: 8 → 9 에 15초)."""
        from pywinauto import mouse
        r = self.win.rectangle()
        x, y = (r.left + r.right) // 2 + 150, (r.top + r.bottom) // 2
        last, still = -1, 0
        for _ in range(150):   # 앞 턴은 맨 위에 닿은 뒤 조금 있다 불러온다(10-22: 4번이면 일찍 멈춰 앞 턴 57개를 못 봄)
            n = sum(1 for k, _ in self.blocks() if k == "q")
            if expected and n >= expected:
                return
            still = still + 1 if n == last else 0
            if still >= (60 if expected else 10):
                return
            last = n
            try:
                self._front()
                mouse.scroll(coords=(x, y), wheel_dist=30)
            except Exception:
                pass
            time.sleep(2.0)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--since", help="이 시각(UTC ISO) 뒤에 받은 행만")
    ap.add_argument("--start", type=int, default=0, help="채팅 목록(위가 최근)에서 몇 번째부터")
    ap.add_argument("--limit", type=int, default=None, help="시험용 — 채팅 몇 개만")
    ap.add_argument("--max-chats", type=int, default=110)
    args = ap.parse_args(argv)
    from tools import gemini_app
    from tools.modelgrade import load_run
    run = load_run(args.run)
    rows = {r["case_id"]: r for r in run["datasets"]["gemini"] if isinstance(r, dict)
            and (not args.since or (r.get("collection") or {}).get("collected_at", "") >= args.since)}
    need = {cid for cid, r in rows.items() if (r.get("collection") or {}).get("status") == "SUCCESS"}
    qnorm = {cid: norm_q(r.get("question") or "") for cid, r in rows.items()}
    md = lambda t: re.sub(r"[\s`*#|_>\-•]+", "", (t or "").lower())  # noqa: E731
    qmd = {cid: md(r.get("question") or "") for cid, r in rows.items()}
    out_dir = ROOT / "data" / "judge_runs" / args.run
    out_dir.mkdir(parents=True, exist_ok=True)
    live = out_dir / "gemini-recheck.jsonl"
    done = set()
    if live.exists():
        done = {json.loads(l)["case_id"] for l in live.read_text(encoding="utf-8").splitlines() if l.strip()}
    print(f"확인할 Gemini 턴 {len(need)}개 · 이미 확인 {len(done & need)}개", flush=True)

    def match(text: str) -> Optional[str]:
        u, um = norm_q(text), md(text)
        cands = [cid for cid, q in qnorm.items() if q and (q in u or (len(q) > 80 and q[:80] in u))]
        if not cands:   # 화면이 마크다운 기호(목록 '-' · 제목 '#')를 그려 빼면 — 기호를 빼고 앞 80자로, 하나로 정해질 때만 (Claude 확인과 같음)
            pre = [cid for cid, q in qmd.items() if len(q) > 80 and q[:80] in um]
            cands = pre if len(pre) == 1 else []
        return max(cands, key=lambda c: len(qnorm[c])) if cands else None
    win = gemini_app.main_window()
    if win is None:
        raise SystemExit("Gemini 앱 창을 찾지 못했습니다 — 앱을 띄워 두세요.")
    rc = Recheck(win)
    checked: List[Dict[str, Any]] = []
    k, opened = args.start, 0
    while k < args.max_chats and (args.limit is None or opened < args.limit) and not need <= done:
        rc.ensure_sidebar()
        links = rc.chat_links()
        if len(links) <= k:
            print(f"목록 끝 — 채팅 {len(links)}개", flush=True)
            break
        link = links[k]
        title = link.element_info.name or ""
        k += 1
        rc._press(link)
        time.sleep(2.5)
        rc._close_sidebar()
        # 보이는 질문 칸 하나로 이 대화가 몇 번 질문인지 알아내 그 턴 수만큼 불러온다
        qn = None
        for kind, g in rc.blocks():
            if kind == "q":
                c = match(rc.text_of(g))
                if c:
                    qn = c.split("_T")[0]
                    break
        rc.load_all_turns(sum(1 for c in rows if c.split("_T")[0] == qn) if qn else None)
        results = []
        want = {c for c in need if qn and c.split("_T")[0] == qn}
        for _pass in range(40):   # 앞 턴을 복사하려고 올리면 아래 턴이 화면에서 지워진다 — 남은 턴이 있으면 조금 내리고 다시 읽는다(10-22 Q69)
            last_q = None
            for kind, g in rc.blocks():
                if kind == "q":
                    last_q = rc.text_of(g)
                    continue
                if last_q is None:
                    continue
                cid, last_q = match(last_q), None
                if not cid or cid in done or cid not in need:
                    continue
                bottom, _codes = rc._copy_buttons(g)
                if bottom is None:
                    rc._hover(g)
                    bottom, _codes = rc._copy_buttons(g)
                copied = rc._press_copy(bottom) if bottom is not None else None
                stored = stored_copy(rows[cid])
                same = copied is not None and _eol(copied) == _eol(stored)
                rec = {"case_id": cid, "title": title, "recopy": "같음" if same else ("복사 실패" if not copied else "다름"),
                       "similarity": None if same or not copied else round(difflib.SequenceMatcher(None, _eol(copied), _eol(stored), autojunk=False).ratio(), 4),
                       "copied_chars": len(copied) if copied else None, "stored_chars": len(stored)}
                if copied and not same:
                    a, b = _eol(copied), _eol(stored)
                    j = next((x for x in range(min(len(a), len(b))) if a[x] != b[x]), min(len(a), len(b)))
                    rec["first_diff"] = {"at": j, "copied": a[max(0, j - 30):j + 60], "stored": b[max(0, j - 30):j + 60]}
                results.append(rec)
                done.add(cid)
                with live.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if not want or want <= done:
                break
            try:   # 남은 턴이 있다 — 조금 내려 다음 턴들을 그리게
                from pywinauto import mouse
                r = rc.win.rectangle()
                rc._front()
                mouse.scroll(coords=((r.left + r.right) // 2 + 150, (r.top + r.bottom) // 2), wheel_dist=-12)
            except Exception:
                pass
            time.sleep(1.5)
        opened += 1
        checked += results
        print(f"  {k:>3}. {title[:24]:<24} 턴 {len(results)} · {dict(Counter(r['recopy'] for r in results))} · {sorted(r['case_id'] for r in results)[:3]}", flush=True)
    remaining = sorted(need - done)
    summary = {"checked": len(checked), "recopy": dict(Counter(c["recopy"] for c in checked)),
               "different": [(c["case_id"], c["similarity"]) for c in checked if c["recopy"] == "다름"],
               "copy_failed": [c["case_id"] for c in checked if c["recopy"] == "복사 실패"], "not_checked": remaining}
    (out_dir / "gemini-recheck.json").write_text(json.dumps({"summary": summary, "checked": checked}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n끝: 확인 {summary['checked']} · {summary['recopy']} · 다름 {summary['different'][:8]} · 복사 실패 {summary['copy_failed'][:8]} · 못 본 턴 {len(remaining)}개 {remaining[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
