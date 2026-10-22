# -*- coding: utf-8 -*-
"""질문 78개를 팀 규칙의 던지는 순서(모자란 기준부터 — tools/min_eval_list.py v2 표준 탐욕법)로 재배치하고 id 를 1~78 로 다시 매긴다 (2026-10-20, 사용자 결정).

- 순서: 아직 10회를 못 채운 기준을 가장 많이 채우는 질문부터(같으면 기준 수 많은 것, 그다음 번호). 모든 기준이 10회에 닿은 뒤 남는 질문은 원래 번호순으로 뒤에 붙인다(78개 고정).
- 원래 번호(ChatGPT 생성 때 번호)는 질문마다 ``origin_id`` 로 남기고, 대응표를 ``던지는순서.json`` 에 쓴다. design.json·k_audit.json·attachment_manifest.json 등
  처음 생성 기록의 번호는 원래 번호다.
- 수집·판정 도구는 번호순으로 던지므로 이 재배치만으로 규칙이 지켜진다.

    python -X utf8 tools/check_question_set.py <세트>/questions.json --pipeline items --min-applicable 10 --json > precheck.json
    python -X utf8 <세트>/reorder_by_greedy.py <precheck.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SET_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        raise SystemExit("사전검사 JSON 경로를 주세요.")
    from tools.min_eval_list import applied_by_question, select
    raw = Path(args[0]).read_text(encoding="utf-8")
    report = json.loads(raw[raw.index("{"):])
    path = SET_DIR / "questions.json"
    groups = json.loads(path.read_text(encoding="utf-8"))
    for g in groups:
        g.setdefault("origin_id", g["id"])   # 두 번 돌려도 원래 번호는 처음 것
    # 사전검사는 현재 id 로 돼 있으므로 현재 id 기준으로 고른 뒤 origin_id 로 옮긴다
    applied = applied_by_question(groups, report)
    res = select(groups, applied, need=10)
    chosen = [c["id"] for c in res["chosen"]]
    rest = [g["id"] for g in groups if g["id"] not in set(chosen)]
    order = chosen + rest
    assert sorted(order) == sorted(g["id"] for g in groups), "질문이 빠지거나 겹침"
    by_id = {g["id"]: g for g in groups}
    new_groups = []
    mapping = []
    for new_id, cur_id in enumerate(order, 1):
        g = by_id[cur_id]
        info = next((c for c in res["chosen"] if c["id"] == cur_id), None)
        mapping.append({"id": new_id, "origin_id": g["origin_id"], "prev_id": cur_id, "kind": (info or {}).get("kind") or "",
                        "k": len(applied[cur_id]), "new_criteria": (info or {}).get("new", []), "fills_target": info is not None})
        g["id"] = new_id
        new_groups.append(g)
    path.write_text(json.dumps(new_groups, ensure_ascii=False, indent=1), encoding="utf-8")
    (SET_DIR / "던지는순서.json").write_text(json.dumps({
        "rule": "모자란 기준부터 — tools/min_eval_list.select v2 (need=10), 목표에 닿은 뒤 남는 질문은 원래 번호순으로 뒤에",
        "need": 10, "universe": res["universe"], "short": res["short"], "chosen": len(chosen), "rest": len(rest),
        "order": mapping}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"재배치: 고른 {len(chosen)}개 + 나머지 {len(rest)}개 → id 1~{len(order)} · 모자란 기준 {res['short'] or '없음'}")
    print("새 번호 → 원래 번호:", {m['id']: m['origin_id'] for m in mapping})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
