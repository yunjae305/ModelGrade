"""고정 판정 항목 표 v1 (judge-items-v1) — 고정 질문 세트의 턴마다 "이 턴에서 판정할 의미 항목 번호"를 미리 적어 둔다 (AI 호출 없음).

- 줄 = 질문의 턴 하나 (단일 질문은 1줄, 10턴 대화는 10줄). 항목 = 코드 규칙(jev_applicability)이 질문 글을 읽고 정한 의미 항목 + J5-3.a(답변을 보고 생기는 유일한 항목 — 모든 턴에 넣고 심판이 적용 여부를 정함).
- 프롬프트에는 넣지 않는다(심판은 44개 전부에 답하고 적용 조건에 안 맞으면 NOT_APPLICABLE). 집계 때 이 표에 적힌 항목만 센다 — 분모가 어느 심판이든 같다. 다시 계산하지 않는다.
- 질문 세트가 바뀌면 이 표도 다시 만들고 v 를 올린다. 표의 sha256 을 결과 기록에 남긴다.

    python -X utf8 tools/build_judge_items_table.py [세트 id]   → 참고자료/질문세트_326_생성프롬프트v2/판정항목표_v1.csv · .json
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp import question_generation as qg  # noqa: E402
from code_eval import semantic_items as si  # noqa: E402

VERSION = "judge-items-v1"
ANSWER_ITEMS = ("J5-3.a",)   # 답변을 보고 생기는 항목 — 모든 턴에 넣음
OUT_DIR = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"


def main(set_id: str = "qs-b468b011e6c3") -> int:
    groups = json.loads((qg.STORE / set_id / "questions.json").read_text(encoding="utf-8"))
    report = qg.check(groups)
    kind = {g["id"]: ("긴 문서" if g["turns"][0].get("reference") else "대화" if len(g["turns"]) > 1 else "단일") for g in groups}
    topic = {g["id"]: g.get("topic") or "" for g in groups}
    order = [it.id for it in si.ITEMS]
    rows = []
    for c in report["cases"]:
        q, _, t = c["case_id"].partition("_T")
        qid = int(q[1:])
        items = sorted({x for x in (c.get("items") or []) if x.startswith("J")} | set(ANSWER_ITEMS), key=order.index)
        rows.append({"case_id": c["case_id"], "question": qid, "turn": int(t or 1), "kind": kind[qid], "topic": topic[qid],
                     "n_items": len(items), "items": items})
    rows.sort(key=lambda r: (r["question"], r["turn"]))
    out_json = OUT_DIR / "판정항목표_v1.json"
    out_csv = OUT_DIR / "판정항목표_v1.csv"
    payload = {"version": VERSION, "set_id": set_id, "prompt_version": "judge-prompt-v1", "answer_items_always": list(ANSWER_ITEMS),
               "items_total": len(order), "rows": rows}
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "질문", "턴", "종류", "주제", "판정 항목 수", "판정 항목 (의미 44개 중)"])
        for r in rows:
            w.writerow([r["case_id"], f"Q{r['question']}", r["turn"], r["kind"], r["topic"], r["n_items"], " ".join(r["items"])])
    digest = hashlib.sha256(out_json.read_bytes()).hexdigest()[:16]
    cnt = Counter(x for r in rows for x in r["items"])
    missing = [i for i in order if i not in cnt]
    sizes = Counter(r["n_items"] for r in rows)
    print(f"{VERSION} | 세트 {set_id} | 줄(턴) {len(rows)} · 질문 {len(groups)} | 항목 수 범위 {min(sizes)}~{max(sizes)} (중앙 {sorted(r['n_items'] for r in rows)[len(rows) // 2]})")
    print("한 번도 안 나오는 항목:", missing or "없음", "| 가장 적게 나오는 항목:", cnt.most_common()[-5:])
    print(f"sha256 {digest} | 저장: {out_csv.name}, {out_json.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:2]))
