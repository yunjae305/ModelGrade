"""Jev · Decisions 3번 판정에서 표가 갈린 칸 — 질문 · 모델 · 턴 · 항목 · 세 표 · 답 앞부분 (발표용, 심판 호출 없음)."""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUN = "50ca3ccccace"
run = json.loads((ROOT / "data" / "comparison_runs" / f"{RUN}.json").read_text(encoding="utf-8"))
rows = {(p, r["case_id"]): r for p, rs in (run.get("datasets") or {}).items() for r in rs}
names = {}
try:
    from sys import path as _p
    _p.insert(0, str(ROOT))
    from code_eval.semantic_items import ITEMS
    names = {it.id: it.question for it in ITEMS}
except Exception:
    pass
out = {}
for judge in ("jev", "decisions"):
    split, total = [], 0
    for f in sorted((ROOT / "data" / "judge_runs" / RUN / f"first-{judge}" / "decisions").glob("Q*_*.json")):
        q, p = f.stem[1:].split("_")
        d = json.loads(f.read_text(encoding="utf-8"))
        for tn, items in d.items():
            if tn.startswith("_") or not isinstance(items, dict):
                continue
            for iid, e in items.items():
                if not isinstance(e, dict) or not e.get("votes"):
                    continue
                total += 1
                if e.get("vote_split"):
                    r = rows.get((p, f"Q{q}_T{tn}")) or {}
                    split.append({"question": int(q), "provider": p, "turn": int(tn), "item": iid, "votes": e["votes"], "choice": e.get("choice"),
                                  "item_question": names.get(iid, "")[:90], "request": str(r.get("question") or "").split("[자료 끝]")[-1].strip()[:160],
                                  "answer": str(r.get("answer") or "")[:260]})
    out[judge] = {"cells": total, "split": len(split), "by_item": Counter(x["item"] for x in split).most_common(),
                  "by_question": sorted(Counter((x["question"], x["provider"]) for x in split).items()), "cells_split": split}
    print(judge, "칸", total, "갈림", len(split))
(Path(__file__).parent / "갈림.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
