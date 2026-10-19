"""k 범위 실험 격자 — 기본 질문마다 k 1~16 변형에 대한 모델별 질문 판정(PASS/FAIL)을 격자로 만든다 (AI 호출 없음).
실험 분석(참고자료/분석/2026-10-18-질문당기준수/boundary_exp.py)과 같은 strict 규칙: CODE 질문 검사 FAIL 또는 의미 FAIL(확인 못 한 J5 포함)이면 FAIL,
Jev 가 확신(0.8↑) PASS 인 J1~J4 는 Luna FAIL 을 덮음. 모델 답은 실행 e4ce7a342251(data/), 판정 원본은 저장소 exp_eval/ 의 jsonl.

    python -X utf8 tools/k_experiment_grid.py            # → 참고자료/최소평가/k_experiment_grid.json (발표 03장이 읽음)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval import engine, semantic_items as si  # noqa: E402
from code_eval import criteria_registry as reg  # noqa: E402
from code_eval.config import load_aiona_config  # noqa: E402

RUN = "e4ce7a342251"
EV = ROOT / "참고자료/분석/2026-10-18-질문당기준수/exp_eval" / RUN
SET = ROOT / "참고자료/질문세트_실험300/questions.json"
OUT = ROOT / "참고자료/최소평가/k_experiment_grid.json"
TH, JEV = 0.8, ("J1", "J2", "J3", "J4")
qid = lambda cid: cid.split("_T")[0]


def jsonl(name):
    f = EV / name
    return {r["case_id"]: r for r in (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())} if f.exists() else {}


def main() -> int:
    run = json.loads((ROOT / "data/comparison_runs" / f"{RUN}.json").read_text(encoding="utf-8"))
    groups = json.loads(SET.read_text(encoding="utf-8"))
    variants = {f"Q{g['id']}": (g["base_id"], int(g["k_actual"]), g.get("topic")) for g in groups if g.get("role") == "변형"}
    providers = [p for p in run["providers"] if (EV / f"luna-{p}.jsonl").exists()]
    code_all = {u[0] for u in reg.CODE_UNITS if u[2] == "all"}
    q_units = {u[0] for u in reg.CODE_UNITS if u[2] in ("question", "question_or_answer")} | code_all
    cfg = load_aiona_config(); cfg.enabled = False
    grid: dict = defaultdict(lambda: defaultdict(dict))
    for p in providers:
        coll = {r["case_id"]: r for r in run["datasets"][p] if r}
        cases = [{k: r[k] for k in ("case_id", "question", "answer", "reference", "conversation_id", "turn", "attachments") if r.get(k) not in (None, "")}
                 for r in coll.values() if (r.get("collection") or {}).get("status") == "SUCCESS" and qid(r["case_id"]) in variants]
        for c in cases:
            c.setdefault("answer", "")
        code_fail: dict = defaultdict(list)
        for c in engine.evaluate_batch(cases, cfg)["cases"]:
            for r in c.get("results") or []:
                cid = r.get("check_id", "")
                if cid.split(":")[0] in q_units and r.get("status") == "EXECUTED" and not cid.endswith(":none") and r.get("verdict") == "FAIL":
                    code_fail[qid(c["case_id"])].append(cid.split(":")[0])
        lu, jv = jsonl(f"luna-{p}.jsonl"), jsonl(f"jev-{p}.jsonl")
        for cid, row in coll.items():
            q = qid(cid)
            if q not in variants:
                continue
            base, k, topic = variants[q]
            if (row.get("collection") or {}).get("status") != "SUCCESS":
                grid[base][k][p] = {"verdict": None, "fails": ["수집 실패"]}; continue
            r = lu.get(cid)
            if not r or r.get("status") != "SUCCESS":
                grid[base][k][p] = {"verdict": None, "fails": ["판정 없음"]}; continue
            jd = (jv.get(cid) or {}).get("decisions") or {} if (jv.get(cid) or {}).get("status") == "SUCCESS" else {}
            fails = sorted(set(code_fail.get(q, [])))
            incomplete = False
            for kk, d in (r.get("decisions") or {}).items():
                j = jd.get(kk)
                if kk in JEV and j and j.get("decision") == "PASS" and (j.get("confidence") or 0) >= TH:
                    continue
                dec = d.get("decision")
                if dec == "FAIL":
                    fails.append(kk)
                elif dec != "PASS" and d.get("status") not in ("NOT_APPLICABLE", None):
                    incomplete = True
            grid[base][k][p] = {"verdict": "FAIL" if fails else (None if incomplete else "PASS"), "fails": fails}
    out = {"run": RUN, "providers": providers, "rule": "strict (확인 못 한 J5 = FAIL)",
           "bases": {b: {"topic": next((t for q, (bb, kk, t) in variants.items() if bb == b), ""),
                         "by_k": {str(k): grid[b][k] for k in sorted(grid[b])}} for b in sorted(grid)}}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    # 요약 출력
    for b in sorted(grid):
        line = []
        for k in sorted(grid[b]):
            vs = [grid[b][k][p]["verdict"] for p in providers]
            line.append("P" if all(v == "PASS" for v in vs) else "F" if all(v == "FAIL" for v in vs) else "?" if None in vs else "x")
        print(b, "".join(line))
    print("P=모두 PASS x=갈림 F=모두 FAIL ?=판정 없음 (k=1..16)")
    print("저장:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
