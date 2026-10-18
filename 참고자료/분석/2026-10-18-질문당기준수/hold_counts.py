import os, sys
from collections import Counter
src = open("boundary_exp.py", encoding="utf-8").read().split("def wilson_hi")[0]
sys.argv = ["x", "e4ce7a342251", r"C:\Users\winz\Desktop\quality-test-integrated\data\question_sets\qs-e1e3f5761ebf"]
exec(src)
ALLQ = sorted({qid(c) for p in P for c in coll[p]}, key=lambda q: int(q[1:]))
for th in (2.0,):
    globals()["TH"] = th
    for j5 in ("hold",):
        line = []
        for p in P:
            V = verdicts(p, j5)
            c = Counter(V.get(q) for q in ALLQ)
            line.append(f"{p} PASS {c['PASS']} · FAIL {c['FAIL']} · 보류 {c[None]} /300")
        print(f"Jev 확정 {th} · {j5}:", " | ".join(line))
