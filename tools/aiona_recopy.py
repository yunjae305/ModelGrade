"""AIONA 답 다시 복사 — 저장된 채팅 URL 을 다시 열어 답변 맨 아래 복사 버튼(코드블록 복사 버튼 아님)으로 원문을 다시 받는다 (모델 호출 없음).

    python tools/aiona_recopy.py <run_id> [--apply] [--only Q27_T1,Q33_T1]

- 대상: 실행의 AIONA 행 중 capture 가 copy-button 이고 url 이 있는 것. 같은 채팅(대화)은 한 번만 열고 턴 번호에 맞는 답변 칸을 복사한다.
- 교체 규칙: 새 원문이 기존 답과 다르고, 더 길고, 기존 답의 글자(공백 제외)를 모두 담고 있을 때만 교체(capture → copy-button-recopied).
  그 밖(같음·짧음·실패)은 그대로 두고 로그에 남긴다. --apply 가 없으면 바꾸지 않고 결과만 보여 준다.
- 서버가 멈춘 상태에서 돌린다(실행 파일을 직접 쓰고, AIONA 브라우저 프로필을 수집과 같이 쓰면 안 된다). 바꾸기 전 원본은 <run>.before-recopy.json.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.aiona_web import AionaChat  # noqa: E402

RUNS = ROOT / "data" / "comparison_runs"
_WS = re.compile(r"\s+")
_MARKS = re.compile(r"[\s>*#`|_~]+")   # '-' 는 지우지 않는다 — '-123' 과 '123' 이 같아지면 안 된다 (Codex 검토)   # 마크다운 기호 — 카드 내용이 전체 답변에서는 인용 블록(> **…**)으로 들어간다 (2026-10-18)


def _compact(text: str) -> str:
    return _WS.sub("", text or "")


def _bare(text: str) -> str:
    """공백과 마크다운 기호를 뺀 글자 — 카드 복사본(기호 없음)과 전체 답변 안의 같은 내용(인용·굵게)을 맞춘다."""
    return _MARKS.sub("", text or "")


def _contains(new: str, old: str) -> bool:
    """새 원문이 기존 답을 담는가 — 글자 그대로, 기호를 뺀 글자로, 또는 기존 답 문장의 80% 이상이 기호를 뺀 새 원문에 있을 때."""
    if set(re.findall(r"-?\d+(?:[.,]\d+)*", old)) - set(re.findall(r"-?\d+(?:[.,]\d+)*", new)):
        return False   # 기존 답의 수치(부호 포함)가 하나라도 없으면 다른 답 — 다른 검사보다 먼저 (Codex 검토)
    if _compact(old) in _compact(new) or _bare(old) in _bare(new):
        return True
    sents = [x for x in re.split(r"(?<=[.?!다요])\s+|\n+", old) if _bare(x)]   # 짧은 결론(예/아니요)도 포함 (Codex 검토)
    return bool(sents) and all(_bare(x) in _bare(new) for x in sents)


def copy_answer(chat: AionaChat, index: int):
    """index 번째(0부터) 답변 칸의 맨 아래 복사 버튼 — 코드블록(pre) 안 버튼은 건너뛴다. 못 받으면 None."""
    answers = chat._answers()
    if answers.count() <= index:
        return None
    buttons = answers.nth(index).locator("button[title='복사']")
    for i in range(buttons.count() - 1, -1, -1):
        candidate = buttons.nth(i)
        try:
            if candidate.evaluate("(el) => !!el.closest('pre')"):
                continue
            chat.page.evaluate("() => { window.__mgCopied = []; }")
            candidate.scroll_into_view_if_needed(timeout=3000)
            candidate.click(timeout=5000)
            chat.page.wait_for_function("() => (window.__mgCopied || []).length > 0", timeout=5000)
            return chat.page.evaluate("() => window.__mgCopied[window.__mgCopied.length - 1]")
        except Exception:
            continue
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="", help="쉼표로 나눈 case_id 만")
    ap.add_argument("--show", action="store_true", help="브라우저 창을 보이게")
    args = ap.parse_args(argv)
    path = RUNS / f"{args.run_id}.json"
    run = json.loads(path.read_text(encoding="utf-8"))
    if args.apply and (run.get("evaluations") or run.get("status") in ("EVALUATING", "COMPLETED")):
        # 답을 바꾸면 그 답의 이전 판정이 남는다 — 평가 전 실행에만 쓴다(평가 뒤라면 바꾼 행만 tools/reeval_failed.py 로 다시 평가) (Codex 검토)
        raise SystemExit("이 실행은 이미 평가가 있습니다 — 답을 바꾸면 이전 판정과 어긋나 --apply 를 하지 않습니다. 평가 전 실행에 쓰세요.")
    rows = run["datasets"].get("aiona") or []
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    targets = [(i, r) for i, r in enumerate(rows)
               if r and r.get("collection", {}).get("status") == "SUCCESS" and r["collection"].get("capture") in ("copy-button", "copy-button-recopied")
               and r["collection"].get("url") and (not only or r["case_id"] in only)]
    by_url = defaultdict(list)
    for i, r in targets:
        by_url[r["collection"]["url"]].append((i, r))
    print(f"대상 {len(targets)}턴 · 채팅 {len(by_url)}개", flush=True)
    changed, same, failed, shorter = [], [], [], []
    started = time.time()
    with AionaChat(headless=not args.show, allow_login=False, answer_timeout_s=60) as chat:
        for n, (url, items) in enumerate(by_url.items(), 1):
            try:
                chat.page.goto(url, wait_until="domcontentloaded")
                chat.page.wait_for_timeout(2500)
                chat._dismiss_popups()
                chat._answers().first.wait_for(timeout=20_000)
            except Exception as exc:
                failed += [(r["case_id"], f"열기 실패 {type(exc).__name__}") for _, r in items]
                continue
            for i, r in items:
                turn = int(r.get("turn") or 1)
                new = copy_answer(chat, turn - 1)
                old = r.get("answer") or ""
                if new is None or not new.strip():
                    failed.append((r["case_id"], "복사 실패")); continue
                if _compact(new) == _compact(old):
                    same.append(r["case_id"]); continue
                if len(_compact(new)) > len(_compact(old)) and _contains(new, old):
                    changed.append((r["case_id"], len(old), len(new)))
                    if args.apply:
                        r["answer"] = new
                        r["collection"]["capture"] = "copy-button-recopied"
                        r["collection"]["recopied_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                else:
                    shorter.append((r["case_id"], len(old), len(new)))
            if n % 20 == 0:
                print(f"  {n}/{len(by_url)} 채팅 · 교체 {len(changed)} · 같음 {len(same)} · 실패 {len(failed)} · {int(time.time() - started)}초", flush=True)
    print(f"끝: 교체 {len(changed)} · 같음 {len(same)} · 새 원문이 더 짧거나 다름 {len(shorter)} · 실패 {len(failed)}")
    for c in changed[:40]:
        print("  교체", c)
    for c in shorter[:20]:
        print("  보류(다름)", c)
    for c in failed[:20]:
        print("  실패", c)
    if args.apply and changed:
        backup_dir = ROOT / "data" / "comparison_runs_backup"   # 실행 폴더 밖 — 서버가 실행 폴더의 *.json 을 실행으로 읽는다 (Codex 검토)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{args.run_id}.before-recopy.json"
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        path.write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
        print("저장:", path, "| 원본:", backup.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
