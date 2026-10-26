"""할일 9번 — 사람이 정해야 하는 칸 목록(고를칸.json) 만들기 (10-26).

칸마다 비교할 AI 판정 판(results)과 고른 까닭(why, 쉬운 말)을 적는다. 판정 폴더(data/judge_runs/, git 제외)가 있어야 다시 만들 수 있다.

    python -X utf8 참고자료/분석/2026-10-26-사람표시/고를칸_만들기.py
    python -X utf8 tools/build_calibration_sheet.py --run 50ca3ccccace --pick-file 참고자료/분석/2026-10-26-사람표시/고를칸.json --dest 참고자료/분석/2026-10-26-사람표시
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools import report_html as R  # noqa: E402

RUN = ROOT / "data" / "judge_runs" / "50ca3ccccace"
OUT = Path(__file__).with_name("고를칸.json")
KO = {"PASS": "PASS", "FAIL": "FAIL", "UNVERIFIED": "보류", "NOT_APPLICABLE": "해당 없음"}
HOLD = {"인용 불일치": "AI 가 근거로 든 문장이 답변 원문에 없어 판정을 정하지 못함",
        "사유 모순": "AI 의 사유와 판정이 서로 반대라 판정을 정하지 못함"}


def load(tag):
    A = {}
    p = RUN / tag / "results.jsonl"
    if not p.exists():
        return A
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            x = json.loads(line)
            A[(x["provider"], int(x["question"]))] = x
    return A


def cells(x):
    for tn, t in (x.get("turn_results") or {}).items():
        for i, it in (t.get("items") or {}).items():
            yield int(tn), i, it


picks, seen = [], set()


def add(kind, p, q, tn, i, why, results):
    k = (p, q, tn, i)
    if k in seen:
        return
    seen.add(k)
    picks.append({"out": results[-1]["out"], "provider": p, "question": q, "turn": tn, "item": i, "kind": kind, "why": why, "results": results})


# A. 연달아 두 번 판정했는데 FAIL ↔ PASS 가 뒤집힌 칸
for a, b in (("v4-r9-chk4", "v4-r9-chk5"), ("v4-r9-chk6", "v4-r9-chk7")):
    A, B = load(a), load(b)
    for k in set(A) & set(B):
        ia = {(tn, i): it for tn, i, it in cells(A[k])}
        for tn, i, it in cells(B[k]):
            d1, d2 = (ia.get((tn, i)) or {}).get("decision"), it.get("decision")
            if {d1, d2} == {"PASS", "FAIL"}:
                add("판마다 뒤집힘", k[0], k[1], tn, i, f"같은 질문을 연달아 두 번 판정했더니 {KO[d1]} → {KO[d2]} 로 바뀜",
                    [{"out": a, "label": "앞 판정"}, {"out": b, "label": "뒤 판정"}])
# B. 최근 판정의 보류 칸(빈 출력 요구 질문의 '질문 전체 보류'는 뺌)
for tag in ("v4-r9-chk3", "v4-r9-chk4", "v4-s2", "v4-s3", "v4-s4", "v4-s1"):
    for k, x in load(tag).items():
        for tn, i, it in cells(x):
            if it.get("decision") == "UNVERIFIED" and R.hold_kind(it) != "질문 전체 보류":
                add("보류", k[0], k[1], tn, i, HOLD.get(R.hold_kind(it), f"AI 가 판정을 정하지 못함({R.hold_kind(it)})"), [{"out": tag, "label": "AI 판정"}])
# C. 기준 다듬기 루프: 지금 규칙(base 1회차)과 고친 안(1회차)이 다른 칸
for item in ("J2-3.a", "J5-2.a", "J3-1.a", "J3-5.a"):
    d = sorted((ROOT / "data" / "refine" / "50ca3ccccace" / item).glob("*/"))[-1]
    base_tag = f"refine-{item}-{d.name}-base-r1"
    base = load(base_tag)
    for prop in ("P1", "P2", "P3"):
        tag = f"refine-{item}-{d.name}-{prop}-r1"
        P = load(tag)
        for k in set(base) & set(P):
            ib = {(tn, i): it for tn, i, it in cells(base[k]) if i == item}
            for tn, i, it in cells(P[k]):
                if i != item:
                    continue
                d1, d2 = (ib.get((tn, i)) or {}).get("decision"), it.get("decision")
                if d1 and d2 and d1 != d2 and "FAIL" in (d1, d2):
                    add("규칙 고친 안과 다름", k[0], k[1], tn, i, f"{item} 규칙을 고친 안({prop})으로 판정했더니 지금 규칙과 달라짐: {KO.get(d1, d1)} → {KO.get(d2, d2)}",
                        [{"out": base_tag, "label": "지금 규칙"}, {"out": tag, "label": f"고친 안 {prop}"}])
# D. 같은 1차 결과에서 2차 심판(Codex · Claude)이 갈린 칸
A, B = load("v4-b10"), load("v4-b10-claude")
for k in set(A) & set(B):
    ia = {(tn, i): it for tn, i, it in cells(A[k])}
    for tn, i, it in cells(B[k]):
        d1, d2 = (ia.get((tn, i)) or {}).get("decision"), it.get("decision")
        if {d1, d2} == {"PASS", "FAIL"}:
            add("두 AI 심판이 갈림", k[0], k[1], tn, i, f"같은 자료로 두 AI 심판이 다르게 판정함: Codex {KO[d1]} · Claude {KO[d2]}",
                [{"out": "v4-b10", "label": "Codex"}, {"out": "v4-b10-claude", "label": "Claude"}])
picks.sort(key=lambda x: (x["question"], x["provider"], x["turn"], x["item"]))   # 같은 답변을 보는 칸끼리 이어 읽게
OUT.write_text(json.dumps(picks, ensure_ascii=False, indent=1), encoding="utf-8")
print(len(picks), Counter(p["kind"] for p in picks))
