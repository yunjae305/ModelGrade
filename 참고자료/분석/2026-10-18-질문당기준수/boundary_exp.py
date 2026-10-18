"""통제 실험 범위 분석 — 기본 질문 18개 × 실제 k 1~16 변형(288) + 보충 12 + 기존 26.
- 판정: 수집 실패→불가 / 유효 FAIL→FAIL / 미완료→제외 / 완료·통과→PASS (병행: J1~J4 Jev 확신도 0.8 이상 PASS 는 확정, 나머지 Luna)
- J5 세 방식(strict·policy·none) — policy: 확인된 사실 오류·출처 불일치만 FAIL
- k 별(실제 k) 지표 + 기본 질문 단위 부트스트랩 / 범위 [a,b] 스윕: 범위 안 변형으로 최종 326 을 짤 때(보충 12 + 기존 26 포함)
  기준별 적용 ≥ m 을 채우는 데 필요한 범위 밖 변형 수 E(욕심 방식, 못 채우면 불가), 최종 배치의 전부 FAIL·PASS 상한, 변별 하한
- 결정 규칙: 커버리지 충족 · 전부 FAIL 상한 ≤ c1 · 전부 PASS 상한 ≤ c2 → 변별 하한 최대 → 3%p 이내 폭 최소 → E 최소
python -X utf8 boundary_exp.py <run_id> <set_dir>   → boundary_exp.json"""
import json, math, random, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(r"C:\Users\winz\Desktop\quality-test-integrated")
sys.path.insert(0, str(ROOT))
from code_eval import criteria_registry as reg, semantic_items as si
from webapp.question_generation import check, criteria_catalog

HERE = Path(__file__).parent
RUN, SET_DIR = sys.argv[1], Path(sys.argv[2])
EV = HERE / "exp_eval" / RUN
(EV / "x").parent.mkdir(parents=True, exist_ok=True)
load = lambda p: json.loads(Path(p).read_text(encoding="utf-8"))
run = load(ROOT / "data/comparison_runs" / f"{RUN}.json")
P = [p for p in run["providers"] if (EV / f"luna-{p}.jsonl").exists()]
TH, JEV = 0.8, ("J1", "J2", "J3", "J4")
qid = lambda cid: cid.split("_T")[0]

groups = load(SET_DIR / "questions.json")
variants = {f"Q{g['id']}": {"base_id": g["base_id"], "k_actual": g.get("k_actual")} for g in groups if g.get("role") == "변형"}
# 기존 326 세트의 긴 입력 10·대화 16 — 이번 수집에는 없고 커버리지에만 넣는다(번호는 K 와 겹치지 않게 'K' 접두)
_old = load(ROOT / "참고자료/수집결과/2026-10-14-질문세트326-세부기준/questions.json")
_keep = [g for g in _old if len(g.get("turns") or []) > 1 or any(t.get("reference") or t.get("long_reference") for t in g.get("turns") or [])]
rep = check(groups)
if rep.get("errors") or rep.get("undetermined"):
    raise SystemExit("판별 불가 — 멈춤")
cat = criteria_catalog()
code_all = {u[0] for u in reg.CODE_UNITS if u[2] == "all"}
always = {it.id for it in si.ITEMS if it.always}
ans_dep = {u[0] for u in reg.CODE_UNITS if u[2] == "answer"} | {"J5-3.a"}
VAR = {c["id"] for c in cat["code"] if c["id"] not in code_all | ans_dep} | {i["id"] for i in cat["items"] if i["id"] not in always | ans_dep}
applied, turn_k = defaultdict(set), defaultdict(list)
for row in rep["cases"]:
    ts = {t for t in (row.get("checks") or []) + (row.get("items") or []) if t in VAR}
    applied[qid(row["case_id"])].update(ts); turn_k[qid(row["case_id"])].append(len(ts))
K = {q: round(statistics.mean(v)) for q, v in turn_k.items()}
VQ = sorted((q for q in variants), key=lambda q: int(q[1:]))           # 변형 255
FILL = [f"Q{g['id']}" for g in groups if g.get("role") == "보충"]
_krep = check([dict(g, id=n) for n, g in enumerate(_keep, 1)])
for row in _krep["cases"]:
    applied["K" + row["case_id"].split("_T")[0][1:]].update(t for t in (row.get("checks") or []) + (row.get("items") or []) if t in VAR)
KEEP = sorted({"K" + r["case_id"].split("_T")[0][1:] for r in _krep["cases"]})
base = {q: variants[q]["base_id"] for q in VQ}

def jsonl(name):
    f = EV / name
    return {r["case_id"]: r for r in (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())} if f.exists() else {}
coll = {p: {r["case_id"]: r for r in run["datasets"][p] if r} for p in P}
REAL = ("사실 오류", "출처 불일치")
from code_eval import engine
from code_eval.config import load_aiona_config
Q_UNITS = {u[0] for u in reg.CODE_UNITS if u[2] in ("question", "question_or_answer")} | code_all
CFG = load_aiona_config(); CFG.enabled = False
CODE = {}
for p in P:
    cases = [{k: r[k] for k in ("case_id", "question", "answer", "reference", "conversation_id", "turn", "attachments") if r.get(k) not in (None, "")}
             for r in coll[p].values() if (r.get("collection") or {}).get("status") == "SUCCESS" or p == "aiona"]
    for c in cases:
        c.setdefault("answer", "")
    b = engine.evaluate_batch(cases, CFG)
    res = defaultdict(dict)
    for c in b["cases"]:
        for r in c.get("results") or []:
            cid = r.get("check_id", "")
            if cid.split(":")[0] in Q_UNITS and r.get("status") == "EXECUTED" and not cid.endswith(":none") and r.get("verdict") in ("PASS", "FAIL"):
                res[qid(c["case_id"])][cid] = "FAIL" if r["verdict"] == "FAIL" or res[qid(c["case_id"])].get(cid) == "FAIL" else "PASS"
    CODE[p] = res
def verdicts(p, j5):
    lu, jv = jsonl(f"luna-{p}.jsonl"), jsonl(f"jev-{p}.jsonl")
    st = defaultdict(lambda: {"fail": False, "incomplete": False, "seen": False})
    for cid, row in coll[p].items():
        q = qid(cid)
        if (row.get("collection") or {}).get("status") != "SUCCESS" and p != "aiona":
            st[q]["fail_collect"] = True
        r = lu.get(cid)
        if not r or r.get("status") not in ("SUCCESS",):
            st[q]["incomplete"] = True; continue
        st[q]["seen"] = True
        jd = (jv.get(cid) or {}).get("decisions") or {} if (jv.get(cid) or {}).get("status") == "SUCCESS" else {}
        for k, d in (r.get("decisions") or {}).items():
            if k == "J5" and j5 == "none":
                continue
            j = jd.get(k)
            if k in JEV and j and j.get("decision") == "PASS" and (j.get("confidence") or 0) >= TH:
                continue
            dec = d.get("decision")
            if dec == "FAIL" and k == "J5" and j5 == "policy" and not ((d.get("j5_outcome") or {}).get("label") or "").startswith(REAL):
                continue
            if dec == "FAIL":
                st[q]["fail"] = True
            elif dec != "PASS" and d.get("status") not in ("NOT_APPLICABLE", None):
                st[q]["incomplete"] = True
    # CODE — 질문 계기 검사 + 항상 검사(빈 답·깨진 글자·외국 문자). 대상 없음(:none)은 세지 않음
    for q, units in CODE[p].items():
        if any(v == "FAIL" for v in units.values()):
            st[q]["fail"] = True
    out = {}
    for q in set(K):
        s = st.get(q)
        if not s or s.get("fail_collect"):
            out[q] = None
        else:
            out[q] = "FAIL" if s["fail"] else (None if s["incomplete"] or not s["seen"] else "PASS")
    return out

def wilson_hi(x, n, z=1.6449):
    if not n: return None
    ph = x / n; den = 1 + z * z / n
    return (ph + z * z / (2 * n) + z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))) / den
def metrics(qs, V):
    full = [q for q in qs if all(V[p].get(q) is not None for p in P)]
    vals = {q: [V[p][q] == "PASS" for p in P] for q in full}
    n = len(full)
    if not n:
        return {"n": 0}
    groups_ = defaultdict(list)
    for q in full: groups_[base.get(q, q)].append(q)
    keys = list(groups_); rr = random.Random(7); bs = []
    for _ in range(500):
        pick = [q for g in (rr.choice(keys) for _ in keys) for q in groups_[g]]
        bs.append(sum(len(set(vals[q])) > 1 for q in pick) / len(pick))
    bs.sort()
    ap = sum(all(v) for v in vals.values()); af = sum(not any(v) for v in vals.values())
    return {"n": n, "disc": round(sum(len(set(v)) > 1 for v in vals.values()) / n, 3), "disc_lo": round(bs[int(0.05 * len(bs))], 3),
            "all_pass": round(ap / n, 3), "all_pass_hi": round(wilson_hi(ap, n), 3), "all_fail": round(af / n, 3), "all_fail_hi": round(wilson_hi(af, n), 3),
            "pass": {p: round(sum(V[p][q] == "PASS" for q in full) / n, 3) for p in P}}

FULL_CNT = Counter(t for q in VQ + FILL + KEEP for t in applied[q])   # 326개 전체로 가능한 최대 횟수 — 목표는 이를 넘지 않게
def fill_cover(inside, m):
    chosen = list(inside) + FILL + KEEP
    cnt = Counter(t for q in chosen for t in applied[q])
    rest = [q for q in VQ if q not in set(inside)]
    extra = []
    while True:
        short = {n for n in VAR if cnt[n] < min(m, FULL_CNT[n])}
        if not short: return extra, 0
        best = max(rest, key=lambda q: len(applied[q] & short), default=None)
        if best is None or not applied[best] & short: return None, len(short)
        rest.remove(best); extra.append(best); cnt.update(applied[best])

RES = {}
for j5 in ("strict", "policy", "none"):
    V = {p: verdicts(p, j5) for p in P}
    byk = [{"k": k, **metrics([q for q in VQ if K[q] == k], V)} for k in sorted({K[q] for q in VQ})]
    sweep = []
    for a in range(0, 17):
        for b in range(a, 17):
            inside = [q for q in VQ if a <= K[q] <= b]
            if len(inside) < 30: continue
            for m in (10, 20):
                extra, unmet = fill_cover(inside, m)
                final = inside + (extra or []) + FILL          # 지표용(기존 26 은 이번 수집에 없음 — 커버리지 계산에만 포함)
                sweep.append({"a": a, "b": b, "m": m, "inside": len(inside), "E": None if extra is None else len(extra), "unmet": unmet,
                              "inside_m": metrics(inside, V), "final_m": metrics(final, V)})
    RES[j5] = {"by_k": byk, "sweep": sweep}

def choose(sweep, m, c1, c2, tie=0.03):
    ok = [s for s in sweep if s["m"] == m and s["E"] is not None and s["final_m"].get("n", 0) >= 30
          and s["final_m"]["all_fail_hi"] <= c1 and s["final_m"]["all_pass_hi"] <= c2]
    if not ok: return None
    best = max(s["final_m"]["disc_lo"] for s in ok)
    near = [s for s in ok if s["final_m"]["disc_lo"] >= best - tie]
    return min(near, key=lambda s: (s["b"] - s["a"], s["E"]))
CH = {f"{j5}|m{m}|F≤{c1}|P≤{c2}": choose(RES[j5]["sweep"], m, c1, c2)
      for j5 in RES for m in (10, 20) for c1 in (0.25, 0.30, 0.35) for c2 in (0.40, 0.50, 0.60)}
out = {"providers": P, "k_dist_variants": dict(sorted(Counter(K[q] for q in VQ).items())), "results": RES, "choices": CH}
(HERE / "boundary_exp.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print("모델", P, "| 변형 실제 k 분포", out["k_dist_variants"])
for j5 in RES:
    print(f"[J5 {j5}] k별 변별:", " ".join(f"{t['k']}:{t.get('n',0)}/{t.get('disc','-')}" for t in RES[j5]["by_k"]))
for k, s in CH.items():
    if s: print(k, "→", f"[{s['a']},{s['b']}] E={s['E']} 최종 n={s['final_m']['n']} 변별 {s['final_m']['disc']}(하한 {s['final_m']['disc_lo']}) 전P {s['final_m']['all_pass']} 전F {s['final_m']['all_fail']}")
print("해 있는 조합", sum(1 for s in CH.values() if s), "/", len(CH))
