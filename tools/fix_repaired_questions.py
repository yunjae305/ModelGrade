"""k 고치기가 손댄 질문 바로잡기 — 모델 호출 없이, 고치기 전 원문과 대조해 (1) 사라진 자료 블록(표·JSON·코드 펜스·'공지:'·'자료:' 줄)을 되살리고
(2) 되풀이 문장(비슷한 문장이 두 번 이상)을 지운다. 그 뒤 k 분포를 다시 센다.

    python tools/fix_repaired_questions.py <세트 id> [--apply]

- 대상: questions.json 에서 k_repaired 가 있는 단일 질문. 원문은 questions.before-k-repair.json.
- 되살린 자료는 원문의 위치(앞/뒤)를 따라 질문 앞이나 뒤에 붙인다. 바뀐 질문 id 를 <세트>/fixed_after_repair.json 에 남긴다
  (이 질문들은 모델 답을 다시 받아야 한다 — tools/null_run_rows.py 로 실행의 해당 행을 비운 뒤 '이어서 수집').
- 2026-10-18 실제 사례: Q288 에서 '공지: 대여한 온도계와 풍속계는 …' 자료 줄이 사라져 네 모델이 모두 원문을 요청함.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp import question_generation as qg  # noqa: E402

_WS = re.compile(r"\s+")
_DATA_LINE = re.compile(r"^\s*(\||```|[{\[]|(자료|공지|기록|메모|표|목록|데이터|원문|본문|문서|안내문|회의록|일정)\s*[:：])")
_SENT_SPLIT = re.compile(r"(?<=[.?!])\s+")


def compact(text: str) -> str:
    return _WS.sub("", text or "")


def data_blocks(text: str):
    """원문에서 자료 블록을 뽑는다 — 연속된 자료 줄(표 행·코드 펜스 안·JSON 줄·'자료:' 줄과 그 뒤 들여쓴/이어지는 줄)을 한 덩어리로."""
    lines = text.split("\n")
    blocks, cur, in_fence = [], [], False
    for line in lines:
        fence = line.strip().startswith("```")
        if fence:
            in_fence = not in_fence
            cur.append(line)
            if not in_fence:
                blocks.append("\n".join(cur)); cur = []
            continue
        if in_fence:
            cur.append(line); continue
        if _DATA_LINE.match(line) or (cur and line.strip() and not _SENT_SPLIT.split(line.strip())[0].endswith(("요.", "다.", "요?", "까?", "줘.", "주세요."))):
            cur.append(line)
        else:
            if cur:
                blocks.append("\n".join(cur)); cur = []
    if cur:
        blocks.append("\n".join(cur))
    return [b for b in blocks if len(compact(b)) >= 6]


def dedupe_sentences(text: str):
    """비슷한 문장(유사도 0.8 이상)이 다시 나오면 지운다 — 자료 블록(표·코드·콜론 줄)은 건드리지 않는다."""
    out_lines, removed = [], 0
    for line in text.split("\n"):
        if _DATA_LINE.match(line) or line.strip().startswith("```"):
            out_lines.append(line); continue
        sents = [s for s in _SENT_SPLIT.split(line) if s.strip()]
        kept = []
        for s in sents:
            if any(difflib.SequenceMatcher(None, s.strip(), t.strip()).ratio() >= 0.8 for t in kept + _seen(out_lines)):
                removed += 1; continue
            kept.append(s)
        out_lines.append(" ".join(kept))
    return "\n".join(out_lines), removed


def _seen(lines):
    return [s for l in lines if not _DATA_LINE.match(l) for s in _SENT_SPLIT.split(l) if s.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("set_id")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    folder = qg.STORE / args.set_id
    groups = json.loads((folder / "questions.json").read_text(encoding="utf-8"))
    before = {g["id"]: g for g in json.loads((folder / "questions.before-k-repair.json").read_text(encoding="utf-8"))}
    restored, deduped, changed = [], [], []
    fixed = copy.deepcopy(groups)   # 미리보기에서도 바뀐 글로 k 를 센다
    for g in fixed:
        if not g.get("k_repaired") or len(g["turns"]) != 1:
            continue
        original, current = before[g["id"]]["turns"][0]["question"], g["turns"][0]["question"]
        text = current
        missing = [b for b in data_blocks(original) if compact(b) not in compact(text)]
        if missing:
            # 원문에서 자료가 앞쪽(첫 절반)에 있었으면 앞에, 아니면 뒤에 붙인다
            front = [b for b in missing if original.find(b) < len(original) / 2]
            back = [b for b in missing if b not in front]
            if front:
                text = "\n\n".join(front) + "\n\n" + text
            if back:
                text = text.rstrip() + "\n\n" + "\n\n".join(back)
            restored.append((g["id"], [b[:40].replace("\n", " ") for b in missing]))
        text, removed = dedupe_sentences(text)
        if removed:
            deduped.append((g["id"], removed))
        if text != current:
            changed.append(g["id"])
            g["turns"][0]["question"] = text
            g["fixed_after_repair"] = True
    print(f"고친 질문 {sum(1 for g in groups if g.get('k_repaired'))} | 자료 되살림 {len(restored)} | 되풀이 제거 {len(deduped)} | 바뀐 질문 {len(changed)}")
    for r in restored:
        print("  자료:", r)
    for d in deduped:
        print("  되풀이:", d)
    groups = fixed
    report = qg.check(groups)
    ks = qg.k_var_summary(groups, report)
    print("k 분포(적용 후 기준):", {k: ks[k] for k in ("singles", "in_range", "out_of_range", "exempt", "by_k")}, "| 범위 밖:", ks["out_of_range_ids"])
    if args.apply and changed:
        (folder / "questions.json").write_text(json.dumps(groups, ensure_ascii=False, indent=1), encoding="utf-8")
        (folder / "fixed_after_repair.json").write_text(json.dumps({"changed": changed, "restored": [r[0] for r in restored],
                                                                   "deduped": [d[0] for d in deduped]}, ensure_ascii=False, indent=1), encoding="utf-8")
        spath = folder / "set.json"
        if spath.exists():
            job = json.loads(spath.read_text(encoding="utf-8"))
            job["summary"] = qg._summary(groups, report); job["coverage"] = report["coverage"]
            spath.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
        print("저장:", folder / "questions.json", "| 바뀐 질문 목록:", folder / "fixed_after_repair.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
