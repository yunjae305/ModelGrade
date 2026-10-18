"""판정 규칙 전후 비교 — 시험 묶음에서 Jev·Luna 일치율, 갈린 수, 대조군에서 새로 갈린 수.
python -X utf8 rules_compare.py <tag> [old_tag]   (old_tag 없으면 원래 실험 판정 e4ce7a342251)"""
import json, sys
from collections import Counter
from pathlib import Path
HERE = Path(__file__).parent
TAG = sys.argv[1]; OLD = sys.argv[2] if len(sys.argv) > 2 else None
TEST = json.loads((HERE / "rules_testset.json").read_text(encoding="utf-8"))
K = ("J1", "J2", "J3", "J4")
def jl(d, n):
    f = HERE / "exp_eval" / d / n
    return {r["case_id"]: r for r in (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())} if f.exists() else {}
def dec(r, k):
    if not r or r.get("status") != "SUCCESS": return None
    v = (r.get("decisions") or {}).get(k) or {}
    return v.get("decision") if v.get("decision") in ("PASS", "FAIL") else None
def table(d):
    out = {}
    for p, t in TEST.items():
        jv, lu = jl(d, f"jev-{p}.jsonl"), jl(d, f"luna-{p}.jsonl")
        for grp in ("conflict", "control"):
            for c in t[grp]:
                for k in K:
                    out[(p, c, k, grp)] = (dec(jv.get(c), k), dec(lu.get(c), k))
    return out
old, new = table(OLD or "e4ce7a342251"), table(f"rules_{TAG}")
for grp in ("conflict", "control"):
    s = Counter()
    for key, (oj, ol) in old.items():
        if key[3] != grp: continue
        nj, nl = new.get(key, (None, None))
        if None in (oj, ol, nj, nl): s["비교 못 함"] += 1; continue
        s["전 갈림"] += oj != ol; s["후 갈림"] += nj != nl; s["항목"] += 1
        s["Jev 바뀜"] += oj != nj; s["Luna 바뀜"] += ol != nl
        if oj != ol and nj == nl: s[f"합의→{nj}"] += 1
        if oj == ol and nj != nl: s["새로 갈림"] += 1
        if oj == ol and nj == nl and oj != nj: s[f"함께 {oj}→{nj}"] += 1
    print(grp, dict(s))
by = Counter()
for key, (nj, nl) in new.items():
    if nj and nl and nj != nl: by[(key[2], f"Jev {nj}/Luna {nl}")] += 1
print("남은 갈림", sorted(by.items()))
