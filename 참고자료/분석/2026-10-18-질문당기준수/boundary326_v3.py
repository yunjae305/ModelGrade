"""질문당 기준 수 범위 시험 v2 — Codex 검토(codex_out1.md) 반영.
- k_var = 질문만 보고 정해지는 적용 기준 중 '질문에 따라 바뀌는 것'만 (항상 적용 CODE 3개·의미 항목 7개 제외), 대화는 턴 평균
- 질문 판정: 수집 실패 → 불가 / 유효 FAIL 하나라도 → FAIL / FAIL 없이 미완료 → 미완료 / 모두 완료·통과 → PASS
- J5 세 방식: strict(그대로) · policy(확인된 사실 오류·출처 안 열림·불일치만 FAIL, 확인 실패는 보류=무시) · none(J5 제외)
- 범위 [a,b]: 범위 안 단일 질문으로 변별·전부 PASS·전부 FAIL(+ 95% 구간, 문서 공유 Q1~Q10 은 한 묶음 부트스트랩),
  커버리지를 채우는 데 필요한 범위 밖 질문 비율(t_needed). 결정 규칙: 전부 FAIL 상한 ≤ c1, 전부 PASS 상한 ≤ c2, n ≥ 30 →
  변별 하한 최대 → 3%p 이내면 폭 최소 → t_needed 최소.   → boundary326_v2.json"""
import json, math, random, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(r"C:\Users\winz\Desktop\quality-test-integrated")
sys.path.insert(0, str(ROOT))
from code_eval import criteria_registry as reg, semantic_items as si
from webapp.question_generation import check

HERE, E = Path(__file__).parent, Path(__file__).parent / "eval326"
D = ROOT / "참고자료/수집결과/2026-10-14-질문세트326-세부기준"
P, TH, JEV = ["aiona", "gpt", "gemini"], 0.8, ("J1", "J2", "J3", "J4")
qid = lambda cid: cid.split("_T")[0]
load = lambda p: json.loads(Path(p).read_text(encoding="utf-8"))
rng = random.Random(20261018)

# --- k_var ---
rep = check(load(D / "questions.json"))
cov = rep["coverage"]
names = list(cov["checks"]) + list(cov["items"])
ans_dep = set(cov["answer_dependent"]) | set(cov["answer_dependent_items"])
code_all = {u[0] for u in reg.CODE_UNITS if u[2] == "all"}
fixed = {it.id for it in si.ITEMS if it.always} | code_all
VAR = [n for n in names if n not in ans_dep and n not in fixed]
undetermined = len(rep.get("errors") or []) + len(rep.get("undetermined") or [])
if undetermined:
    raise SystemExit(f"판별 불가 {undetermined}건 — k 가 틀려지므로 멈춤 (토크나이저 캐시 확인)")
applied, turn_k = defaultdict(set), defaultdict(list)
for row in rep["cases"]:
    ts = {t for t in (row.get("checks") or []) + (row.get("items") or []) if t in VAR}
    applied[qid(row["case_id"])].update(ts)
    turn_k[qid(row["case_id"])].append(len(ts))
CONV = {q for q, v in turn_k.items() if len(v) > 1}
K = {q: round(statistics.mean(v)) for q, v in turn_k.items()}
SINGLE = sorted((q for q in K if q not in CONV), key=lambda x: int(x[1:]))
cluster = lambda q: "LONGDOC" if int(q[1:]) <= 10 else q   # Q1~Q10 은 같은 참조 문서
answers = {it["case_id"]: it["answers"] for it in load(D / "answers.json")["items"]}
coll_err = {p: {qid(c) for c, a in answers.items() if (a.get(p) or {}).get("status") != "SUCCESS"} for p in P}

# --- 판정 ---
Q_UNITS = {u[0] for u in reg.CODE_UNITS if u[2] in ("question", "question_or_answer")}
def jsonl(name):
    return {r["case_id"]: r for r in (json.loads(l) for l in (E / name).read_text(encoding="utf-8").splitlines() if l.strip())}
REAL = ("사실 오류", "출처 불일치")   # 출처 접근 불가는 평가 환경 원인이 섞여 보류 쪽(2차 검토)
CONFLICT = Counter()
def verdicts(p, mode, j5):
    lu, jv = jsonl(f"luna-{p}.jsonl"), jsonl(f"jev-{p}.jsonl")
    code = load(E / f"luna-{p}.json")["cases"]
    st = defaultdict(lambda: {"fail": False, "incomplete": False, "n": 0})
    for c in code:
        q = qid(c["case_id"])
        for r in c.get("results") or []:
            cid = r.get("check_id", "")
            if r.get("status") not in ("EXECUTED", "NOT_APPLICABLE", None):
                st[q]["incomplete"] = True
            if (cid.split(":")[0] in Q_UNITS or cid.split(":")[0] in code_all) and r.get("status") == "EXECUTED" and not cid.endswith(":none"):
                st[q]["n"] += 1
                st[q]["fail"] |= r.get("verdict") == "FAIL"
    for c in code:   # 의미 평가 기록이 없거나 비어 있으면 미완료
        rr = lu.get(c["case_id"])
        if c.get("answer") is not None and (not rr or (rr.get("status") == "SUCCESS" and not rr.get("decisions"))):
            st[qid(c["case_id"])]["incomplete"] = True
    for cid, r in lu.items():
        q = qid(cid)
        jd = (jv.get(cid) or {}).get("decisions") or {} if (jv.get(cid) or {}).get("status") == "SUCCESS" else {}
        crits = set((r.get("decisions") or {})) | (set(jd) & set(JEV) if mode == "parallel" else set())
        for k in crits:
            d = (r.get("decisions") or {}).get(k) or {}
            if k == "J5" and j5 == "none":
                continue
            j = jd.get(k)
            if mode == "parallel" and k in JEV and j and j.get("decision") == "PASS" and (j.get("confidence") or 0) >= TH:
                if d.get("decision") == "FAIL":
                    CONFLICT[(p, j5)] += 1
                st[q]["n"] += 1; continue
            dec = d.get("decision")
            if dec == "FAIL" and k == "J5" and j5 == "policy":
                lab = (d.get("j5_outcome") or {}).get("label") or ""
                if not lab.startswith(REAL):
                    continue          # 확인 실패 = 보류 → 이 질문 판정에 넣지 않음
            if dec == "FAIL":
                st[q]["fail"] = True; st[q]["n"] += 1
            elif dec == "PASS":
                st[q]["n"] += 1
            elif d.get("status") not in ("NOT_APPLICABLE", None) or r.get("status") != "SUCCESS":
                st[q]["incomplete"] = True
    out = {}
    for q in K:
        if q in coll_err[p]:
            out[q] = None
        else:
            s = st[q]
            out[q] = "FAIL" if s["fail"] else (None if s["incomplete"] else "PASS")
    return out

def wilson(x, n, z=1.6449):   # 단측 95%
    if n == 0:
        return (None, None)
    ph = x / n; den = 1 + z * z / n; c = ph + z * z / (2 * n); m = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return ((c - m) / den, (c + m) / den)

def metrics(qs, V):
    full = [q for q in qs if all(V[p].get(q) is not None for p in P)]
    vals = {q: [V[p][q] == "PASS" for p in P] for q in full}
    n = len(full)
    disc = sum(len(set(v)) > 1 for v in vals.values()); ap = sum(all(v) for v in vals.values()); af = sum(not any(v) for v in vals.values())
    # 묶음 부트스트랩 — 변별 하한
    groups = defaultdict(list)
    for q in full: groups[cluster(q)].append(q)
    keys = list(groups)
    rr = random.Random(20261018)
    bs = []
    for _ in range(400):
        pick = [q for g in (rr.choice(keys) for _ in keys) for q in groups[g]]
        if pick:
            bs.append(sum(len(set(vals[q])) > 1 for q in pick) / len(pick))
    bs.sort()
    return {"n": n, "disc": round(disc / n, 3) if n else None, "disc_lo": round(bs[int(0.05 * len(bs))], 3) if bs else None,
            "all_pass": round(ap / n, 3) if n else None, "all_pass_hi": round(wilson(ap, n)[1], 3) if n else None,
            "all_fail": round(af / n, 3) if n else None, "all_fail_hi": round(wilson(af, n)[1], 3) if n else None}

def t_needed(inside, m=10):
    """범위 안 질문만으로 단일 질문용 변동 기준(대화 전용 제외)을 각 m 회 채우지 못하면, 범위 밖 질문을 욕심 방식으로 더해 필요한 비율"""
    target = [n for n in VAR if any(n in applied[q] for q in SINGLE)]
    cnt = Counter(t for q in inside for t in applied[q])
    rest = [q for q in SINGLE if q not in set(inside)]
    added = 0
    global ADDED_SET
    ADDED_SET = list(inside)
    while True:
        short = {n: m - cnt[n] for n in target if cnt[n] < m}
        if not short or not rest:
            break
        best = max(rest, key=lambda q: sum(1 for t in applied[q] if t in short))
        if not any(t in short for t in applied[best]):
            break
        rest.remove(best); added += 1; cnt.update(applied[best]); ADDED_SET.append(best)
    unmet = sum(1 for n in target if cnt[n] < m)
    return (None if unmet else round(100 * added / len(SINGLE), 1)), unmet, ADDED_SET

RES = {}
for j5 in ("strict", "policy", "none"):
    V = {p: verdicts(p, "parallel", j5) for p in P}
    byk = [{"k": k, **metrics([q for q in SINGLE if K[q] == k], V)} for k in sorted({K[q] for q in SINGLE})]
    sweep = []
    for a in range(0, 14):
        for b in range(a, 14):
            inside = [q for q in SINGLE if a <= K[q] <= b]
            if len(inside) < 30:
                continue
            mm = metrics(inside, V)
            tn, unmet, final = t_needed(inside)
            fm = metrics(final, V)
            sweep.append({"a": a, "b": b, "outside_pct": round(100 * (len(SINGLE) - len(inside)) / len(SINGLE), 1),
                          "t_needed_pct": tn, "unmet_after": unmet, **mm, "final_with_fill": fm})
    RES[j5] = {"by_k": byk, "sweep": sweep, "overall": metrics(SINGLE, V)}

def choose(sweep, c1, c2, tie=0.03):
    ok = [s for s in sweep if s["n"] >= 30 and s["unmet_after"] == 0 and s["final_with_fill"]["all_fail_hi"] <= c1 and s["final_with_fill"]["all_pass_hi"] <= c2]
    if not ok:
        return None
    best = max(s["disc_lo"] for s in ok)
    near = [s for s in ok if s["disc_lo"] >= best - tie]
    return min(near, key=lambda s: (s["b"] - s["a"], s["t_needed_pct"], -s["n"]))
CHOICES = {}
for j5 in RES:
    for c1 in (0.25, 0.30, 0.35):
        for c2 in (0.40, 0.50, 0.60):
            CHOICES[f"{j5}|allfail≤{c1}|allpass≤{c2}"] = choose(RES[j5]["sweep"], c1, c2)
out = {"definition": {"var_criteria": len(VAR), "fixed_excluded": sorted(fixed), "undetermined_cases": undetermined,
                      "single": len(SINGLE), "conversations": len(CONV), "k_dist_single": dict(sorted(Counter(K[q] for q in SINGLE).items()))},
       "results": RES, "choices": CHOICES, "jev_luna_conflicts": {f"{p}|{j}": v for (p, j), v in CONFLICT.items()},
       "unmet_all_single": sorted((n, c) for n, c in Counter(t for q in SINGLE for t in applied[q]).items() if c < 10) + [(n, 0) for n in VAR if n not in {t for q in SINGLE for t in applied[q]}]}
(HERE / "boundary326_v3.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print("Jev PASS 가 Luna FAIL 을 덮은 수:", dict(CONFLICT))
print("단일 310개 전부로도 10회 미만인 기준:", out["unmet_all_single"])
print("변동 기준", len(VAR), "| 판별 불가 턴", undetermined, "| 단일 k_var 분포", out["definition"]["k_dist_single"])
for j5 in RES:
    o = RES[j5]["overall"]
    print(f"\n[J5 {j5}] 전체 단일 n={o['n']} 변별 {o['disc']} (하한 {o['disc_lo']}) 전부P {o['all_pass']} 전부F {o['all_fail']}")
    print("  k_var:", " ".join(f"{t['k']}:{t['n']}/{t['disc']}" for t in RES[j5]["by_k"] if t["n"]))
for k, s in CHOICES.items():
    print(k, "→", "해 없음" if not s else f"[{s['a']},{s['b']}] n={s['n']} 변별 {s['disc']}(하한 {s['disc_lo']}) 전P≤{s['all_pass_hi']} 전F≤{s['all_fail_hi']} 범위밖 {s['outside_pct']}% 필요 t {s['t_needed_pct']}%")
