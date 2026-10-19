"""판정 상황표 v2 (judge-situations-v2) — 고정 판정 프롬프트 v2 의 ``{{CASE_SITUATIONS}}`` 에 들어갈 글을 턴마다 미리 정해 둔다 (AI 호출 없음).

- 줄 = 질문의 턴 하나. 상황 = 코드 규칙(``sub_applicability``)이 질문 글(앞 대화·첨부·참조 포함)에서 찾은 세부 기준과 그 근거 문장.
  판정할 항목 = 그 상황의 항목 + J5-3.a(답변을 보고 생기는 유일한 항목 — 모든 턴에 넣고 적용 여부는 프롬프트 상황표 2).
  이 표의 판정 항목이 집계 분모다(판정항목표_v1 을 대신함). v1 과 다른 턴은 ``items_table.changed`` 에 남긴다 — 적용 규칙
  적용 규칙 sub-applicability-v6(사실 확인 요청)로 Q36_T1 에 J5-1.a 가 더해진 것뿐이어야 한다.
- 답변과 무관하게 질문만으로 정해지므로 모델·심판·실행이 달라도 같은 턴이면 글자 하나 다르지 않다. 질문 세트가 바뀌면 다시 만들고 v 를 올린다.

    python -X utf8 tools/build_judge_situations.py [질문 세트 폴더]   (기본: 참고자료/질문세트_326 — 서버 원본 data/question_sets/qs-b468b011e6c3 와 같은 사본)
    → 참고자료/질문세트_326/판정상황표_v2.json · .csv
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval import semantic_items as si  # noqa: E402
from webapp import question_generation as qg  # noqa: E402

VERSION = "judge-situations-v2"
PROMPT_VERSION = "judge-prompt-v2"
ANSWER_ITEMS = ("J5-3.a",)   # 답변을 보고 생기는 항목 — 모든 턴에 넣음 (판정항목표_v1 과 같은 규칙)
DEFAULT_SET = ROOT / "참고자료" / "질문세트_326"
ITEMS_V1 = "판정항목표_v1.json"
GENERIC_RULES = {"요청", "답변"}   # 모든 질문·답변에 걸리는 규칙 — 근거 문장을 적지 않는다
EXCERPT_CHARS = 200


def _basis(sub_id: str, found: Dict[str, Any]) -> str:
    if sub_id in ("J5-3",):
        return "답변을 보고 정함(상황표 2)"
    rules = [r for r in found.get("rules") or [] if r]
    if found.get("model_applies") or "평가 모델 판정" in rules:
        head = "적용 여부는 상황표 2"
        rules = [r for r in rules if r != "평가 모델 판정"]
    else:
        head = ""
    if not rules or set(rules) <= GENERIC_RULES:
        return head or ("모든 답변" if sub_id == "J3-1" else "모든 질문")
    matches = [m for m in found.get("matches") or [] if m]
    if "reference" in matches:   # 참조 자료의 값 — 자료 첫머리를 근거로 옮기지 않는다
        return (f"{head} · " if head else "") + "근거: [참조 자료]의 값"
    # 대화 id 는 수집 출처 이름(AIONA_ID_…)을 담을 수 있다 — 심판에게 모델을 짐작할 단서를 주지 않게 뺀다
    excerpt = re.sub(r"대화 \S+ · ", "대화 ", " ".join(str(found.get("excerpt") or "").split()))
    if len(excerpt) > EXCERPT_CHARS:
        excerpt = excerpt[:EXCERPT_CHARS] + "…"
    text = f"근거: \"{excerpt}\"" if excerpt else "근거: " + "·".join(rules)
    if matches:
        text += f" (찾은 말: {', '.join(matches[:4])})"
    return f"{head} · {text}" if head else text


def render(items: List[str], situations: List[Dict[str, Any]]) -> str:
    """``{{CASE_SITUATIONS}}`` 에 그대로 넣는 글."""
    lines = [f"판정할 항목 ({len(items)}개): {' '.join(items)}", "상황 (프로그램이 질문 글로 정함 — 상황표 1):"]
    lines += [f"- {s['sub']} {s['name']} → {' '.join(s['items'])} · {s['basis']}" for s in situations]
    return "\n".join(lines)


def build(set_dir: Path) -> Dict[str, Any]:
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    report = qg.check(groups)
    kind = {g["id"]: ("긴 문서" if g["turns"][0].get("reference") else "대화" if len(g["turns"]) > 1 else "단일") for g in groups}
    order = [it.id for it in si.ITEMS]
    subs = {s.id: s for s in si.SUBS}
    rows = []
    for case in report["cases"]:
        q, _, t = case["case_id"].partition("_T")
        items = sorted({x for x in case.get("items") or [] if x.startswith("J")} | set(ANSWER_ITEMS), key=order.index)
        found = case.get("situations") or {}
        situations = []
        for sub in si.SUBS:
            mine = [i for i in items if i.startswith(sub.id + ".")]
            if mine:
                situations.append({"sub": sub.id, "name": subs[sub.id].name, "items": mine, "basis": _basis(sub.id, found.get(sub.id) or {})})
        rows.append({"case_id": case["case_id"], "question": int(q[1:]), "turn": int(t or 1), "kind": kind[int(q[1:])],
                     "items": items, "situations": situations, "case_situations": render(items, situations)})
    rows.sort(key=lambda r: (r["question"], r["turn"]))
    return {"version": VERSION, "prompt_version": PROMPT_VERSION, "set": set_dir.name, "answer_items_always": list(ANSWER_ITEMS), "rows": rows}


def main(argv: List[str]) -> int:
    set_dir = Path(argv[0]) if argv else DEFAULT_SET
    payload = build(set_dir)
    v1 = set_dir / ITEMS_V1
    if v1.is_file():   # 판정항목표_v1 과 다른 턴을 남긴다 — 적용 규칙을 바꾼 결과만이어야 한다
        old = {r["case_id"]: r["items"] for r in json.loads(v1.read_text(encoding="utf-8"))["rows"]}
        if set(old) != {r["case_id"] for r in payload["rows"]}:
            raise SystemExit(f"판정항목표_v1 과 턴 목록이 다릅니다 (줄 {len(old)} vs {len(payload['rows'])})")
        changed = [{"case_id": r["case_id"], "added": [i for i in r["items"] if i not in old[r["case_id"]]],
                    "removed": [i for i in old[r["case_id"]] if i not in r["items"]]} for r in payload["rows"] if old[r["case_id"]] != r["items"]]
        payload["items_table"] = {"version": "judge-items-v1", "sha256": hashlib.sha256(v1.read_bytes()).hexdigest()[:16], "changed": changed}
    out_json, out_csv = set_dir / "판정상황표_v2.json", set_dir / "판정상황표_v2.csv"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "질문", "턴", "종류", "판정 항목 수", "상황 (세부 기준)", "CASE_SITUATIONS 글"])
        for r in payload["rows"]:
            w.writerow([r["case_id"], f"Q{r['question']}", r["turn"], r["kind"], len(r["items"]), " ".join(s["sub"] for s in r["situations"]),
                        r["case_situations"]])
    digest = hashlib.sha256(out_json.read_bytes()).hexdigest()[:16]
    changed = (payload.get("items_table") or {}).get("changed")
    print(f"{VERSION} | 줄(턴) {len(payload['rows'])} | 판정항목표_v1 과 다른 턴: {changed} | sha256 {digest} | 저장: {out_json.name}, {out_csv.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
