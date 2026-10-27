"""질문 개정 기록 (question-revisions-v1, 사용자 10-27 '질문생성프롬프트와 질문리스트도 틀린게 있지 않을지 … 수정가능하게') —
하네스2(또는 사람)가 질문 글을 고치면 <질문 세트>/question_revisions.json 에 남긴다. 그 뒤의 수집은 고친 시각보다 먼저 받은 답을 낡은 답으로 보고
그 질문만 다시 받으며(옛 답은 행의 previous 에), 판정은 답이 판정보다 새로우면 그 (모델, 질문)을 다시 판정한다(tools/judge_v4)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

FILE = "question_revisions.json"
VERSION = "question-revisions-v1"


def load(set_dir: Path) -> List[Dict[str, Any]]:
    f = Path(set_dir) / FILE
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("revisions") or []
    except (OSError, ValueError):
        return []


def revised_at(set_dir: Path) -> Dict[int, str]:
    """질문 → 마지막으로 고친 시각(UTC ISO) — 수집 기록의 collected_at 과 같은 꼴이라 글자로 견준다."""
    out: Dict[int, str] = {}
    for r in load(set_dir):
        q = int(r["question"])
        out[q] = max(out.get(q, ""), str(r.get("at") or ""))
    return out


def add(set_dir: Path, question: int, turns: Iterable[int], why: str, by: str = "하네스2", **extra: Any) -> Dict[str, Any]:
    f = Path(set_dir) / FILE
    doc = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"version": VERSION, "revisions": []}
    rec = {"question": int(question), "turns": sorted({int(t) for t in turns}), "at": datetime.now(timezone.utc).isoformat(), "by": by, "why": why, **extra}
    doc.setdefault("revisions", []).append(rec)
    f.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return rec


def is_stale(row: Dict[str, Any], rev: Dict[int, str]) -> bool:
    """받은 답이 그 질문을 고치기 전에 받은 것인가 — 받은 시각이 없는 옛 수집도 고친 질문이면 낡은 답."""
    try:
        q = int(str(row.get("case_id") or "").split("_T")[0][1:])
    except ValueError:
        return False
    at = rev.get(q)
    return bool(at) and str((row.get("collection") or {}).get("collected_at") or "") < at
