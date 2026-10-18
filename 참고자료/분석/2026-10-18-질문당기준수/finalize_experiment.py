"""실험 세트 마무리 — 기본 질문마다 k(1~16) 하나씩만 남기고, 빈 자리를 커버리지 보충으로 채워 정확히 326개.
보충2: 빈 출력 단독 질문 + 부족 기준을 함께 노리는 자료 처리 질문. 커버리지(변동 기준마다 10회 이상)를 다시 세어 모자라면 한 번 더.
python -X utf8 finalize_experiment.py → experiment2/final/questions.json, final/meta.json, final/files/"""
import json, shutil, sys, time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
sys.argv = [sys.argv[0]]
src = (HERE / "gen_experiment_v2.py").read_text(encoding="utf-8")
exec(src.split("# 1) 생성")[0])            # 설정·프롬프트·ask·actual_k 재사용 (호출 없음)
OUT2 = OUT / "final"
(OUT2 / "files").mkdir(parents=True, exist_ok=True)
for f in (OUT / "files").glob("*"):
    shutil.copy2(f, OUT2 / "files" / f.name)

V = json.loads((OUT / "variants.json").read_text(encoding="utf-8"))
kept, seen = [], set()
for v in sorted(V, key=lambda v: (v["base_id"], abs(v["k_actual"] - v["k_target"]), v["id"])):
    key = (v["base_id"], v["k_actual"])
    if 1 <= v["k_actual"] <= 16 and key not in seen:
        seen.add(key); kept.append(v)
kept.sort(key=lambda v: (v["base_id"], v["k_actual"]))
old = json.loads((ROOT / "참고자료/수집결과/2026-10-14-질문세트326-세부기준/questions.json").read_text(encoding="utf-8"))
keep = [g for g in old if len(g.get("turns") or []) > 1 or any(t.get("reference") or t.get("long_reference") for t in g.get("turns") or [])]
fill1 = json.loads((OUT / "fill.json").read_text(encoding="utf-8"))["data"].get("questions") or []
slots = 326 - len(kept) - len(keep) - len(fill1[:12])
print("변형", len(kept), "| 보충1", len(fill1[:12]), "| 기존", len(keep), "| 보충2 자리", slots, flush=True)

def to_group(q, role):
    turns = []
    for t_i, t in enumerate(q.get("turns") or [{"question": q.get("question")}], 1):
        files = [render_attachment(spec, OUT2 / "files") for spec in t.get("attachments") or []]
        turns.append({"turn": t_i, "question": t.get("question") or "", **({"attachments": files} if files else {})})
    return {"type": q.get("type"), "topic": q.get("topic"), "difficulty": q.get("difficulty"), "trap": q.get("trap"),
            "target_items": q.get("target_items") or [], "turns": turns, "role": role}
base_groups = [{"type": v["type"], "topic": v["topic"], "difficulty": v["difficulty"], "target_items": v["target_items"],
                "turns": [{"turn": 1, "question": v["question"]}], "role": "변형", "base_id": v["base_id"],
                "k_target": v["k_target"], "k_actual": v["k_actual"], "expected_answer": v.get("expected_answer")} for v in kept]
fills1 = [to_group(q, "보충") for q in fill1[:12]]

def coverage(groups):
    rep = qg.check([dict(g, id=n) for n, g in enumerate(groups, 1)])
    cnt = Counter()
    for row in rep["cases"]:
        cnt.update({t for t in (row.get("checks") or []) + (row.get("items") or []) if t in VAR})
    return cnt

FILL2 = """한국어 AI 답변 품질 평가용 질문을 정확히 {n}개 만듭니다. 결과는 JSON 하나로만 답합니다.
- 먼저 '빈 출력' 질문 {e}개: 다른 조건 없이 아무것도 출력하지 말라는 요청만 담습니다. 표현을 서로 다르게(예: '아무것도 출력하지 마', '빈 문자열로 답해', '답변하지 말고 비워 둬') 하고, 다른 요구(형식·글자 수·주제 설명)는 섞지 않습니다.
- 나머지 {d}개: 질문 안에 가상 자료(목록·표·JSON 배열 등)를 직접 넣고 정리·변환·계산·집계하게 합니다. 아래 '부족한 기준'이 각각 목표 횟수 이상 적용되게, 한 질문에 여러 기준을 함께 넣습니다. CODE 조건은 [점검 목록]의 인식 규칙 표현과 자료 형식을 그대로 따릅니다(규칙에 맞지 않으면 적용되지 않음).
- 정답 행동이 분명하고 자연스러운 질문으로. 꼭 넣을 문구와 쓰지 말 문구는 다른 문장으로. 자료 설명에 'JSON으로·표로' 같은 답 형식 말은 쓰지 않기.
출력: {{"questions": [{{"type": "유형", "topic": "주제", "target_items": ["id"], "difficulty": "보통|어려움", "trap": "FAIL 이 되는 답", "question": "질문 원문"}}]}}

# 부족한 기준 (목표 횟수까지 더 필요한 수)
{short}

# 점검 목록
{catalog}"""
fills2 = []
for rnd in (1, 2):
    cnt = coverage(base_groups + fills1 + fills2 + keep)
    need = {n: 10 - cnt[n] for n in VAR if cnt[n] < 10}
    left = slots - len(fills2)
    print(time.strftime("%H:%M:%S"), f"보충2 {rnd}회차 — 부족 기준 {need} | 남은 자리 {left}", flush=True)
    if not need or left <= 0:
        break
    e = min(need.get("2-1.explicit_empty", 0), left)
    d = left - e if rnd == 2 else min(left - e, max(4, sum(v for k, v in need.items() if k != "2-1.explicit_empty") // 2 + 2))
    data = ask(FILL2.format(n=e + d, e=e, d=d, short="\n".join(f"- {k}: {v}회 더" for k, v in need.items()),
                            catalog="\n".join(qg._catalog_lines(qg.restrict_catalog(catalog, set(need)), detailed=True))), 40000)
    fills2 += [to_group(q, "보충") for q in (data.get("questions") or [])[: e + d]]
# 자리가 남으면 마지막 응답으로 채우지 못한 만큼은 자료 처리 질문 한 번 더 (없으면 그대로)
final = base_groups + fills1 + fills2 + keep
final = final[:326]
for n, g in enumerate(final, 1):
    g["id"] = n
(OUT2 / "questions.json").write_text(json.dumps(final, ensure_ascii=False, indent=1), encoding="utf-8")
cnt = coverage(final)
meta = {"version": GEN_VERSION + " + finalize", "questions": len(final), "variants": len(base_groups), "fills": len(fills1) + len(fills2), "kept": len(keep),
        "usage": usage, "k_levels_per_base": {b: sorted(g["k_actual"] for g in base_groups if g["base_id"] == b) for b in sorted({g["base_id"] for g in base_groups})},
        "coverage_min": min(cnt[n] for n in VAR), "coverage_under10": sorted((n, cnt[n]) for n in VAR if cnt[n] < 10)}
(OUT2 / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({k: v for k, v in meta.items() if k != "k_levels_per_base"}, ensure_ascii=False))
print("k별 변형 수:", dict(sorted(Counter(g["k_actual"] for g in base_groups).items())))
