"""사람 판정 기록 (human-records-v1) — 코드가 정하기 애매해 보류한 조건에 사람이 고른 PASS · FAIL 과 사유 (사용자 10-26).

`modelgrade held-apply` 가 report 보류 화면의 '코드 판정 확인' 칸 선택을 여기에 쌓는다. 같은 종류(불릿 표지 이스케이프 · 불릿 묶음 여럿 ·
자기 점검 문장의 금지어 · 서비스 꼬리글)의 판단과 사유가 모이면 기준 v2 5절 · CODE 규칙을 고치는 근거로 쓴다(`summary`).
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
VERSION = "human-records-v1"
DEFAULT_PATH = ROOT / "참고자료" / "분석" / "사람판정" / "코드확인_기록.json"


def code_hold_path() -> Path:
    """기록 파일 — MODELGRADE_CODE_HOLDS 로 바꿀 수 있다(시험용)."""
    return Path(os.environ.get("MODELGRADE_CODE_HOLDS") or DEFAULT_PATH)


def _read(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": VERSION, "records": []}


def add_code_holds(entries: Iterable[Dict[str, Any]]) -> int:
    """기록을 덧붙인다 — 같은 (실행, 모델, 턴, 검사)는 나중 것으로 바꾼다. 덧붙이거나 바꾼 수."""
    path = code_hold_path()
    doc = _read(path)
    rows = {(x.get("run_id"), x.get("provider"), x.get("case_id"), x.get("check_id")): x for x in doc.get("records") or []}
    changed = 0
    for e in entries:
        key = (e.get("run_id"), e.get("provider"), e.get("case_id"), e.get("check_id"))
        if rows.get(key, {}).get("label") == e.get("label") and rows.get(key, {}).get("note") == e.get("note"):
            continue
        rows[key] = e
        changed += 1
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": VERSION, "records": list(rows.values())}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return changed


def summary() -> List[Dict[str, Any]]:
    """종류별 PASS · FAIL 수와 사유 — 기준을 고칠 때 본다."""
    by: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"labels": Counter(), "notes": []})
    for x in _read(code_hold_path()).get("records") or []:
        k = x.get("kind") or x.get("check_id") or ""
        by[k]["labels"][x.get("label")] += 1
        if (x.get("note") or "").strip():
            by[k]["notes"].append(f"{x.get('label')}: {x['note'].strip()}")
    return [{"kind": k, "PASS": v["labels"]["PASS"], "FAIL": v["labels"]["FAIL"], "notes": v["notes"]} for k, v in sorted(by.items())]
