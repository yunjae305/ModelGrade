import sys
from collections import Counter, defaultdict
src = open("boundary_exp.py", encoding="utf-8").read().split("def wilson_hi")[0]
sys.argv = ["x", "e4ce7a342251", r"C:\Users\winz\Desktop\quality-test-integrated\data\question_sets\qs-e1e3f5761ebf"]
exec(src)
from pathlib import Path
def jl2(d, n):
    f = HERE / "exp_eval" / d / n
    return {r["case_id"]: r for r in (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())} if f.exists() else {}
def dd(r, k):
    v = ((r or {}).get("decisions") or {}).get(k) or {} if (r or {}).get("status") == "SUCCESS" else {}
    return v
ALLQ = sorted({qid(c) for p in P for c in coll[p]}, key=lambda q: int(q[1:]))
for p in P:
    lu, jv, l2 = jsonl(f"luna-{p}.jsonl"), jsonl(f"jev-{p}.jsonl"), jl2("rules_v6again", f"luna-{p}.jsonl")
    st = defaultdict(lambda: {"fail": False, "hold": set()}); why = Counter()
    for cid, row in coll[p].items():
        q = qid(cid); r = lu.get(cid)
        if not r or r.get("status") != "SUCCESS":
            st[q]["hold"].add("평가 미완료"); continue
        for k, d in (r.get("decisions") or {}).items():
            dec = d.get("decision")
            if k in JEV:
                j = dd(jv.get(cid), k).get("decision")
                if j in ("PASS", "FAIL") and dec in ("PASS", "FAIL") and j != dec:
                    again = dd(l2.get(cid), k).get("decision")
                    if again is None:
                        st[q]["hold"].add("재판정 없음"); continue
                    if again != dec:
                        st[q]["hold"].add("심판 갈림"); continue
            if dec == "FAIL" and k == "J5" and not ((d.get("j5_outcome") or {}).get("label") or "").startswith(REAL):
                st[q]["hold"].add("J5 확인 실패"); continue
            if dec == "FAIL": st[q]["fail"] = True
            elif dec != "PASS" and d.get("status") not in ("NOT_APPLICABLE", None): st[q]["hold"].add("판정 불완전")
    for q, units in CODE[p].items():
        if any(v == "FAIL" for v in units.values()): st[q]["fail"] = True
    c = Counter()
    for q in ALLQ:
        s = st[q]
        if s["fail"]: c["FAIL"] += 1
        elif s["hold"]:
            c["보류"] += 1
            for h in s["hold"]: why[h] += 1
        else: c["PASS"] += 1
    print(p, f"PASS {c['PASS']} · FAIL {c['FAIL']} · 보류 {c['보류']} /300", "| 보류 사유(질문 수, 겹침 있음)", dict(why))
