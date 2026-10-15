"""질문 300 세트 만들기 — 질문 세트 326(우리 생성) + IFEval-Ko + KoMT-Bench(공개) 748개 후보에서 300개를 고른다 (결정적, 모델 호출 없음).

    python -X utf8 tools/build_set300.py

고르는 규칙은 tools/build_fixed_sets.select 와 같다: 1) 72개 기준이 모두 한 번 이상 들어가게(공개 질문 먼저) 2) 300개까지
적게 들어간 기준을 담은 질문부터(우리 질문 점수는 0.5배라 비슷하면 공개 질문이 먼저). 설명은 참고자료/질문세트300_선정방법.md.
출력: examples/fixed_sets/set300/{questions.json, files/, selection.json}"""
import json, shutil, sys
from collections import Counter
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import build_fixed_sets as bfs
from tools.check_question_set import check_items, unit_of
from tools.min_question_count import all_targets, question_units
from webapp.question_generation import flatten, check, _summary

SET326 = ROOT / "참고자료/수집결과/2026-10-14-질문세트326-세부기준"
bfs.SET300 = SET326          # 우리 세트 첨부파일은 여기서 복사
OURS = "질문 세트 326"
SIZE = 300
OUT = ROOT / "examples/fixed_sets/set300"

groups = []
def add(turns, source, kind, pool="public"):
    groups.append({"type": kind, "topic": kind, "difficulty": "", "trap": "", "target_items": [],
                   "turns": [{"turn": i, **t} for i, t in enumerate(turns, 1)], "source": source, "pool": pool})
for r in json.loads((bfs.SOURCES / "ifeval_ko.json").read_text(encoding="utf-8")):
    add([{"question": r["prompt"]}], f"IFEval-Ko key {r['key']}", "지시 따르기")
for r in bfs._lines(bfs.SOURCES / "komt_question_ko.jsonl"):
    add([{"question": q} for q in r["turns"]], f"KoMT-Bench {r['question_id']}", r.get("category") or "")
for g in json.loads((SET326 / "questions.json").read_text(encoding="utf-8")):
    groups.append({**g, "source": f"{OURS} #{g['id']}", "pool": "ours"})
for i, g in enumerate(groups, 1):
    g["id"] = i

report = check_items(flatten(groups), pipeline="items")
order, targets, _ = question_units(report)
names = set(all_targets(report))
by_unit = {unit_of(row): int(str(row.get("case_id", "")).split("_T")[0].lstrip("Q")) for row in report["cases"]}
pools = {u: groups[by_unit[u] - 1]["pool"] for u in order}
best, count = bfs.select(order, targets, pools, names, SIZE, set())
if OUT.exists():
    shutil.rmtree(OUT)
(OUT / "files").mkdir(parents=True)
chosen = [bfs.materialize(groups[gid - 1], OUT / "files", {}, None, None) for gid in sorted({by_unit[u] for u in best})]
for new_id, g in enumerate(chosen, 1):
    g["id"] = new_id
(OUT / "questions.json").write_text(json.dumps(chosen, ensure_ascii=False, indent=1), encoding="utf-8")
set_report = check(chosen)
cov = set_report["coverage"]
sources = Counter(g["source"].split(" #")[0] if g["source"].startswith(OURS) else g["source"].split(" ")[0] for g in chosen)
# 72 기준(CODE 검사 28 + LLM 항목 44) 으로 적용 수 — 답변으로만 정해지는 8개는 '항상 검사' 로 따로
answer_only = set(cov["answer_dependent"]) | set(cov["answer_dependent_items"])
crit72 = {**cov["checks"], **cov["items"]}
targetable = {k: v for k, v in crit72.items() if k not in answer_only}
spread = sorted(targetable.values())
selection = {"version": "fixed-set-v2", "kind": "set300", "label": "질문 300 (326+공개 섞음)", "size": SIZE, "questions": len(chosen),
             "summary": _summary(chosen, set_report), "coverage": cov, "turns": sum(len(g["turns"]) for g in chosen),
             "sources": dict(sources), "criteria72": {"total": len(crit72), "answer_only": sorted(answer_only),
                                                      "covered": sum(1 for v in targetable.values() if v > 0), "targetable": len(targetable),
                                                      "min": spread[0], "median": spread[len(spread) // 2], "max": spread[-1],
                                                      "per_criterion": dict(sorted(crit72.items()))},
             "licenses": {s: bfs.LICENSES[s] for s in sources if s in bfs.LICENSES}, "candidates": len(groups)}
(OUT / "selection.json").write_text(json.dumps(selection, ensure_ascii=False, indent=1), encoding="utf-8")
print("후보", len(groups), "→ 고름", len(chosen), "턴", selection["turns"], "출처", dict(sources))
print("72 기준: 질문으로 검사하는", len(targetable), "중 덮음", selection["criteria72"]["covered"], "| 적용 수 최소/중앙/최대", spread[0], spread[len(spread)//2], spread[-1])
print("적게 적용된 기준(10 미만):", {k: v for k, v in sorted(targetable.items(), key=lambda x: x[1]) if v < 10})
print("요약:", {k: v for k, v in selection["summary"].items() if k in ("questions", "single_questions", "conversations", "turns", "attachments", "long_references", "difficulty")})
