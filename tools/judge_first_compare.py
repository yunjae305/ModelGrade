"""1차 심판 비교 — 같은 칸을 Jev · Decisions 가 각각 3번씩 판정한 결과 (judge-first-compare-v1, 사용자 10-26
"평가모델 jev, decision api 도 각각 3번씩 평가 돌려서 결과내고").

1차만 돌리기 (Codex 안 부름 · 20문항씩 · 네 모델 동시):
    python -X utf8 tools/judge_v4.py --run 50ca3ccccace --set 참고자료/질문세트_78 --questions 1,...,20 --out first-jev --first-judge jev --first-only --votes 3
    python -X utf8 tools/judge_v4.py --run 50ca3ccccace --set 참고자료/질문세트_78 --questions 1,...,20 --out first-decisions --first-judge decisions --first-only --votes 3
비교:
    python -X utf8 tools/judge_first_compare.py [--pipe-jev pipe-jev --pipe-decisions pipe-decisions] [--dest <폴더>]

→ 비교.md · 비교.json — ① 받은 입력(심판이 판정한 칸 · 못 받은 입력) ② 3표 일치율 ③ 두 심판의 다수결 일치(소분류별)
④ Codex 로 넘기는 칸 비율 ⑤ 최종 판정에서 Codex 가 본 칸과의 일치 · 넘기지 않았을 FAIL ⑥ 시간 · 호출
⑦ (--pipe-*) 1차 심판만 바꾼 두 판정(같은 Codex 흐름)의 질문 판정 · 칸 판정 차이.
판정 폴더는 읽기만 한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import judge_v4 as jv  # noqa: E402

VERSION = "judge-first-compare-v1"
RUN = "50ca3ccccace"
#: 지금까지의 최종 판정(Codex 확정) — 단일 질문은 v4-s1~s4, 멀티턴은 v4-r8 위에 그 뒤 확인 판정(같은 (모델, 질문)은 마지막 줄)
FINAL_DIRS = ("v4-r8", "v4-r8-fix", "v4-r8-fix2", "v4-r8-fix3", "v4-r9-chk", "v4-r9-chk2", "v4-r9-chk3", "v4-r9-chk4", "v4-r9-chk5",
              "v4-r9-chk6", "v4-r9-chk7", "v4-s1", "v4-s2", "v4-s3", "v4-s4")
CHOICES = ("PASS", "FAIL", "NOT_APPLICABLE")
KO = {"PASS": "PASS", "FAIL": "FAIL", "NOT_APPLICABLE": "해당 없음", "UNVERIFIED": "보류", None: "-"}
Cell = Tuple[str, int, int, str]   # (모델, 질문, 턴, 항목)


def lines(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def first_cells(folder: Path) -> Dict[Cell, Optional[Dict[str, Any]]]:
    """판정 폴더 decisions/<질문>_<모델>.json → 칸마다 1차 답(choice · votes · probabilities · confidence), 답이 없으면 None."""
    out: Dict[Cell, Optional[Dict[str, Any]]] = {}
    for f in sorted((folder / "decisions").glob("Q*_*.json")):
        q_s, provider = f.stem[1:].split("_", 1)
        data = json.loads(f.read_text(encoding="utf-8"))
        for t, items in data.items():
            if not str(t).isdigit() or not isinstance(items, dict):
                continue
            for i, e in items.items():
                out[(provider, int(q_s), int(t), i)] = e if isinstance(e, dict) and e.get("choice") in CHOICES else None
    return out


def final_cells(run_dir: Path, dirs: Iterable[str] = FINAL_DIRS) -> Tuple[Dict[Cell, Dict[str, Any]], Dict[Tuple[str, int], Dict[str, Any]]]:
    """최종 판정 — 폴더들의 판정 줄 중 (모델, 질문)마다 가장 늦은 줄. 돌려주는 값: (칸 → 판정 칸, (모델, 질문) → 줄)."""
    latest: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for d in dirs:
        for l in lines(run_dir / d / "results.jsonl"):
            if l.get("status") != "judged":
                continue
            k = (l["provider"], int(l["question"]))
            if k not in latest or str(l.get("at") or "") >= str(latest[k].get("at") or ""):
                latest[k] = l
    cells: Dict[Cell, Dict[str, Any]] = {}
    for (p, q), l in latest.items():
        for t, tr in (l.get("turn_results") or {}).items():
            for i, it in (tr.get("items") or {}).items():
                if isinstance(it, dict):
                    cells[(p, q, int(t), i)] = it
    return cells, latest


def sub_of(item: str) -> str:
    return item.rsplit(".", 1)[0]


SUB_NAME = {s.id: s.name for s in jv.si.SUBS}


def kappa(pairs: List[Tuple[str, str]]) -> Optional[float]:
    """코언 카파 — 두 심판이 우연히 맞을 비율을 뺀 일치도."""
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[c] * cb[c] for c in set(ca) | set(cb)) / (n * n)
    return round((po - pe) / (1 - pe), 3) if pe < 1 else None


def pct(a: int, b: int) -> str:
    return f"{a / b * 100:.1f}%" if b else "-"


def question_kind(tokens: Optional[int], turns: int) -> str:
    if turns > 1:
        return "멀티턴"
    if tokens is not None and tokens > 200_000:
        return "롱컨텍스트"
    if tokens is not None and tokens > jv.JEV_MAX_TOKENS:
        return "긴 단일"
    return "작은 단일"


def judge_stats(name: str, cells: Dict[Cell, Optional[Dict[str, Any]]], kinds: Dict[Tuple[str, int], str]) -> Dict[str, Any]:
    judged = {k: e for k, e in cells.items() if e is not None}
    by_kind: Dict[str, Counter] = defaultdict(Counter)
    for k, e in cells.items():
        by_kind[kinds.get((k[0], k[1]), "?")]["판정" if e is not None else "답 없음"] += 1
    unanimous = sum(1 for e in judged.values() if len(set(e.get("votes") or [e["choice"]])) == 1)
    votes = sum(len(e.get("votes") or []) for e in judged.values())
    minority = sum(sum(1 for v in (e.get("votes") or []) if v != e["choice"]) for e in judged.values())
    flagged = sum(1 for k, e in judged.items() if jv.screen_flag(k[3], e)[0])
    return {"judge": name, "cells": len(cells), "judged": len(judged), "by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
            "unanimous": unanimous, "votes": votes, "minority_votes": minority,
            "choices": dict(Counter(e["choice"] for e in judged.values())), "flagged": flagged}


def compare(jev: Dict[Cell, Optional[Dict[str, Any]]], dec: Dict[Cell, Optional[Dict[str, Any]]],
            final: Dict[Cell, Dict[str, Any]]) -> Dict[str, Any]:
    shared = sorted(k for k in set(jev) & set(dec) if jev[k] is not None and dec[k] is not None)
    pairs = [(jev[k]["choice"], dec[k]["choice"]) for k in shared]
    confusion = Counter(pairs)
    per_sub: Dict[str, Counter] = defaultdict(Counter)
    for k in shared:
        c = per_sub[sub_of(k[3])]
        c["n"] += 1
        c["same"] += jev[k]["choice"] == dec[k]["choice"]
        c["jev_fail"] += jev[k]["choice"] == "FAIL"
        c["dec_fail"] += dec[k]["choice"] == "FAIL"
        c["jev_flag"] += jv.screen_flag(k[3], jev[k])[0]
        c["dec_flag"] += jv.screen_flag(k[3], dec[k])[0]
    # 최종 판정에서 Codex 가 본 칸(codex 가 있음)과 비교 — 그 칸의 최종 판정(PASS · FAIL · 해당 없음)을 기준으로
    ref = {k: it for k, it in final.items() if it.get("codex") is not None and it.get("decision") in CHOICES}
    vs: Dict[str, Dict[str, Any]] = {}
    for name, cells in (("Jev", jev), ("Decisions", dec)):
        both = [k for k in shared if k in ref]
        fails = [k for k in both if ref[k]["decision"] == "FAIL"]
        missed = [k for k in fails if not jv.screen_flag(k[3], cells[k])[0]]
        vs[name] = {"cells": len(both), "same": sum(1 for k in both if cells[k]["choice"] == ref[k]["decision"]),
                    "final_fail": len(fails), "missed_fail": len(missed), "missed_examples": [list(k) for k in missed[:12]],
                    "pass_flagged": sum(1 for k in both if ref[k]["decision"] != "FAIL" and jv.screen_flag(k[3], cells[k])[0])}
    disagree = [k for k in shared if jev[k]["choice"] != dec[k]["choice"]]
    return {"shared": len(shared), "same": sum(a == b for a, b in pairs), "kappa": kappa(pairs),
            "confusion": {f"{a}|{b}": n for (a, b), n in sorted(confusion.items())},
            "per_sub": {s: dict(c) for s, c in sorted(per_sub.items())}, "vs_codex": vs,
            "disagree_examples": [{"cell": list(k), "jev": jev[k]["choice"], "jev_votes": jev[k].get("votes"),
                                   "decisions": dec[k]["choice"], "decisions_votes": dec[k].get("votes"),
                                   "final": (final.get(k) or {}).get("decision"), "codex": (final.get(k) or {}).get("codex")} for k in disagree[:40]]}


def time_calls(folder: Path) -> Dict[str, Any]:
    ls = [l for l in lines(folder / "results.jsonl") if l.get("status") == "first_only"]
    calls = [(l.get("calls") or {}).get("decisions") or {} for l in ls]
    usage: Counter = Counter()
    for c in calls:
        for u in (c.get("usage") or {}).values():
            for k, v in (u or {}).items():
                if isinstance(v, (int, float)):
                    usage[k] += v
    return {"questions": len(ls), "seconds": round(sum(c.get("seconds") or 0 for c in calls), 1), "calls": sum(c.get("calls") or 0 for c in calls),
            "failed_calls": sum(c.get("attempts_failed") or 0 for c in calls), "usage": dict(usage)}


def pipe_compare(run_dir: Path, a: str, b: str) -> Optional[Dict[str, Any]]:
    """1차 심판만 바꾼 두 판정 폴더(같은 Codex 흐름)의 질문 판정 · 칸 판정 차이."""
    la = {(l["provider"], int(l["question"])): l for l in lines(run_dir / a / "results.jsonl") if l.get("status") == "judged"}
    lb = {(l["provider"], int(l["question"])): l for l in lines(run_dir / b / "results.jsonl") if l.get("status") == "judged"}
    keys = sorted(set(la) & set(lb))
    if not keys:
        return None
    verdict = Counter((la[k].get("verdict"), lb[k].get("verdict")) for k in keys)
    cell_pairs: Counter = Counter()
    diff: List[Dict[str, Any]] = []
    codex_cells = {a: 0, b: 0}
    for k in keys:
        ta, tb = la[k].get("turn_results") or {}, lb[k].get("turn_results") or {}
        for t in set(ta) & set(tb):
            ia, ib = ta[t].get("items") or {}, tb[t].get("items") or {}
            for i in set(ia) & set(ib):
                da, db = ia[i].get("decision"), ib[i].get("decision")
                cell_pairs[(da, db)] += 1
                codex_cells[a] += ia[i].get("codex") is not None
                codex_cells[b] += ib[i].get("codex") is not None
                if da != db and len(diff) < 60:
                    diff.append({"cell": [k[0], k[1], int(t), i], a: da, b: db, f"{a}_how": ia[i].get("how"), f"{b}_how": ib[i].get("how")})
    calls = {x: sum(((l.get("calls") or {}).get("codex") or {}).get("calls") or 0 for l in L.values()) for x, L in ((a, la), (b, lb))}
    return {"questions": len(keys), "verdict": {f"{x}|{y}": n for (x, y), n in sorted(verdict.items(), key=str)},
            "verdict_same": sum(n for (x, y), n in verdict.items() if x == y), "cells": sum(cell_pairs.values()),
            "cells_same": sum(n for (x, y), n in cell_pairs.items() if x == y), "cell_pairs": {f"{x}|{y}": n for (x, y), n in sorted(cell_pairs.items(), key=str)},
            "codex_cells": codex_cells, "codex_calls": calls, "diff_examples": diff}


def write_md(r: Dict[str, Any]) -> str:
    J, D, C = r["jev"], r["decisions"], r["compare"]
    md = [f"# 1차 심판 비교 — Jev · Decisions 각 3표 ({r['date']}, {VERSION})", "",
          "같은 질문 78개 × 네 모델의 모든 턴을 Jev 와 Decisions 가 각각 3번씩 따로 판정했습니다(Codex 는 부르지 않음). 칸 = (모델, 질문, 턴, 항목).", "",
          "## 받은 입력 · 3표 일치", "", "| | Jev | Decisions |", "|---|---|---|",
          f"| 판정한 칸 / 판정할 칸 | {J['judged']:,} / {J['cells']:,} | {D['judged']:,} / {D['cells']:,} |"]
    kinds = sorted(set(J["by_kind"]) | set(D["by_kind"]))
    for k in kinds:
        a, b = J["by_kind"].get(k, {}), D["by_kind"].get(k, {})
        md.append(f"| └ {k} | {a.get('판정', 0):,} / {a.get('판정', 0) + a.get('답 없음', 0):,} | {b.get('판정', 0):,} / {b.get('판정', 0) + b.get('답 없음', 0):,} |")
    md += [f"| 3표가 모두 같은 칸 | {pct(J['unanimous'], J['judged'])} | {pct(D['unanimous'], D['judged'])} |",
           f"| 다수결과 다른 표 | {pct(J['minority_votes'], J['votes'])} | {pct(D['minority_votes'], D['votes'])} |",
           f"| FAIL 로 본 칸 | {pct(J['choices'].get('FAIL', 0), J['judged'])} | {pct(D['choices'].get('FAIL', 0), D['judged'])} |",
           f"| Codex 로 넘길 칸(선별 규칙) | {pct(J['flagged'], J['judged'])} | {pct(D['flagged'], D['judged'])} |",
           f"| 1차 시간(질문 합) · 호출 | {r['time']['jev']['seconds']:,.0f}초 · {r['time']['jev']['calls']:,}번 | {r['time']['decisions']['seconds']:,.0f}초 · {r['time']['decisions']['calls']:,}번 |", ""]
    md += ["## 두 심판이 모두 판정한 칸", "",
           f"- 칸 {C['shared']:,}개 중 다수결이 같은 칸 {pct(C['same'], C['shared'])} · 코언 카파 {C['kappa']}",
           "- 갈린 칸(Jev → Decisions): " + " · ".join(f"{KO[a.split('|')[0]]}→{KO[a.split('|')[1]]} {n}" for a, n in C["confusion"].items() if a.split("|")[0] != a.split("|")[1]), "",
           "| 소분류 | 칸 | 같음 | Jev FAIL | Decisions FAIL | Jev 넘김 | Decisions 넘김 |", "|---|---|---|---|---|---|---|"]
    for s, c in sorted(C["per_sub"].items(), key=lambda x: x[1]["same"] / max(1, x[1]["n"])):
        md.append(f"| {s} {SUB_NAME.get(s, '')} | {c['n']} | {pct(c['same'], c['n'])} | {c['jev_fail']} | {c['dec_fail']} | {c['jev_flag']} | {c['dec_flag']} |")
    md += ["", "## Codex 가 본 칸과 비교 (지금까지의 최종 판정)", "",
           "최종 판정에서 Codex 가 직접 본 칸만 기준으로 씁니다. 그 칸들은 당시 1차 심판이 넘긴 칸이라, 작은 질문(Jev 가 맡음)에서는 Jev 쪽으로 기울어 있습니다.", "",
           "| | Jev | Decisions |", "|---|---|---|"]
    V = C["vs_codex"]
    md += [f"| 기준 칸 | {V['Jev']['cells']} | {V['Decisions']['cells']} |",
           f"| 최종 판정과 같음 | {pct(V['Jev']['same'], V['Jev']['cells'])} | {pct(V['Decisions']['same'], V['Decisions']['cells'])} |",
           f"| 최종 FAIL 중 Codex 로 넘기지 않았을 칸 | {V['Jev']['missed_fail']} / {V['Jev']['final_fail']} | {V['Decisions']['missed_fail']} / {V['Decisions']['final_fail']} |",
           f"| 최종 FAIL 아닌데 넘길 칸 | {V['Jev']['pass_flagged']} | {V['Decisions']['pass_flagged']} |", ""]
    if r.get("pipe"):
        P = r["pipe"]
        md += ["## 1차 심판만 바꾼 두 판정 (같은 Codex 흐름)", "",
               f"- 질문 {P['questions']}개: 질문 판정이 같음 {pct(P['verdict_same'], P['questions'])} — " + " · ".join(f"{k} {n}" for k, n in P["verdict"].items()),
               f"- 칸 {P['cells']:,}개: 칸 판정이 같음 {pct(P['cells_same'], P['cells'])} · Codex 가 본 칸 " + " · ".join(f"{k} {n}" for k, n in P["codex_cells"].items())
               + " · Codex 호출 " + " · ".join(f"{k} {n}" for k, n in P["codex_calls"].items()), ""]
    md += ["## 읽는 법", "",
           "- 3표 일치: 같은 칸을 세 번 따로 물었을 때 세 답이 모두 같은 비율 — 높을수록 심판이 흔들리지 않습니다.",
           "- 넘길 칸: 1차 답이 FAIL · FAIL 확률 0.3 이상 · 확신 0.7 미만이면 Codex 가 다시 봅니다(지금 선별 규칙). 넘기지 않은 칸은 1차 답이 그대로 최종이 됩니다.",
           "- 넘기지 않았을 FAIL: Codex 가 FAIL 로 확정한 칸인데 그 심판을 1차로 썼다면 Codex 에 가지 않았을 칸 — 놓친 FAIL 입니다.", ""]
    return "\n".join(md)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", default=RUN)
    ap.add_argument("--jev", default="first-jev")
    ap.add_argument("--decisions", default="first-decisions")
    ap.add_argument("--pipe-jev", help="1차 Jev 로 Codex 까지 돌린 판정 폴더 (선택)")
    ap.add_argument("--pipe-decisions", help="1차 Decisions 로 Codex 까지 돌린 판정 폴더 (선택)")
    ap.add_argument("--dest", default=str(ROOT / "참고자료" / "분석" / f"{date.today().isoformat()}-1차심판비교"))
    args = ap.parse_args(argv)
    run_dir = ROOT / "data" / "judge_runs" / args.run
    jev, dec = first_cells(run_dir / args.jev), first_cells(run_dir / args.decisions)
    final, _ = final_cells(run_dir)
    kinds: Dict[Tuple[str, int], str] = {}
    for folder in (args.decisions, args.jev):
        for l in lines(run_dir / folder / "results.jsonl"):
            if l.get("status") == "first_only":
                tok = ((l.get("calls") or {}).get("decisions") or {}).get("question_tokens")
                kinds.setdefault((l["provider"], int(l["question"])), question_kind(tok, int(l.get("turns") or 1)))
    r = {"version": VERSION, "date": date.today().isoformat(), "run": args.run, "folders": {"jev": args.jev, "decisions": args.decisions},
         "jev": judge_stats("Jev", jev, kinds), "decisions": judge_stats("Decisions", dec, kinds), "compare": compare(jev, dec, final),
         "time": {"jev": time_calls(run_dir / args.jev), "decisions": time_calls(run_dir / args.decisions)}}
    if args.pipe_jev and args.pipe_decisions:
        r["pipe"] = pipe_compare(run_dir, args.pipe_jev, args.pipe_decisions)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "비교.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    (dest / "비교.md").write_text(write_md(r), encoding="utf-8")
    print(f"→ {dest / '비교.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
