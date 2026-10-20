"""k 9·12 에서 FAIL 이 급증하는 이유 (k-jump-v1) — 조건 수 변형 실험(k 1~16, 뼈대 18개 × GPT·Claude·Gemini)을 다시 뜯어본다
(10-19 오후 미팅 29:02~32:33 · 체크리스트 n6). AI 호출 없음.

    python -X utf8 tools/k_jump_analysis.py
    → 참고자료/최소평가/k급증_원인_e4ce7a342251.json · .md

- 판정: 참고자료/최소평가/k_experiment_grid.json (실행 e4ce7a342251, 'strict' — 확인 못 한 J5 = FAIL). 판정만 있고 답 원문은 평가 PC 에.
- 질문: 실험 세트 질문세트_실험300 은 10-19 에 저장소에서 지웠으므로 git 기록(지운 커밋의 바로 앞)에서 읽는다. 변형마다 base_id · k_actual.
- 조건: 지금 적용 규칙(webapp.question_generation.check)으로 변형마다 걸린 검사·항목을 구하고, k 가 하나 늘 때 새로 생긴 FAIL 을
  ① 새로 붙은 조건 ② 원래 있던 조건(앞 k 에서도 걸려 있었는데 새로 깨짐) ③ J5(엄격 규칙)로 나눈다.
- k 별로 FAIL 비율 · J5 하나로만 FAIL 인 것을 뺀 비율(지금 규칙이면 보류 — 엄격 규칙 영향의 최대치) · 형식 요구(표·목록·제목·JSON 구조·코드블록) 수를 센다.
- 지금 326 세트(질문세트_326_생성프롬프트v2)도 k 별 형식 요구 3개↑ 비율을 함께 센다 — 같은 위험이 실제 세트에 있는지.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp import question_generation as qg  # noqa: E402

VERSION = "k-jump-v1"
GRID = ROOT / "참고자료" / "최소평가" / "k_experiment_grid.json"
EXP_PATH = "참고자료/질문세트_실험300/questions.json"
SET326 = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2" / "questions.json"
OUT = ROOT / "참고자료" / "최소평가"
FORMAT = {"2-2.table", "2-2.list", "2-2.heading", "2-2.json_structure", "1-2.code_block"}
JUMPS = (9, 12)


def deleted_set() -> List[Dict[str, Any]]:
    """지운 실험 세트를 git 기록에서 — 지운 커밋의 바로 앞 판."""
    commit = subprocess.run(["git", "log", "--diff-filter=D", "--format=%h", "-1", "--", EXP_PATH], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.strip()
    if not commit:
        raise SystemExit(f"git 기록에서 {EXP_PATH} 를 지운 커밋을 찾지 못했습니다")
    raw = subprocess.run(["git", "show", f"{commit}^:{EXP_PATH}"], cwd=ROOT, capture_output=True, check=True).stdout
    return json.loads(raw.decode("utf-8"))


def labels(report: Dict[str, Any]) -> Dict[int, set]:
    """질문 → 걸린 조건(판정 기록과 같은 단위 — CODE 는 검사 이름, 의미는 J1~J8)."""
    out: Dict[int, set] = defaultdict(set)
    for row in report.get("cases") or []:
        q = int(row["case_id"].split("_T")[0][1:])
        for t in (row.get("checks") or []) + (row.get("items") or []):
            lab = t.split(":")[0]
            out[q].add(lab.split("-")[0] if lab.startswith("J") else lab)
    return out


def build() -> Dict[str, Any]:
    grid = json.loads(GRID.read_text(encoding="utf-8"))
    bases = grid["bases"]
    groups = deleted_set()
    applied = labels(qg.check(groups))
    variant = {(g["base_id"], g["k_actual"]): g["id"] for g in groups if g.get("base_id")}
    missing = [(b, k) for b, v in bases.items() for k in v["by_k"] if (b, int(k)) not in variant]
    per_k = []
    for k in sorted({int(k) for v in bases.values() for k in v["by_k"]}):
        verdicts = [(b, p, r) for b, v in bases.items() for p, r in (v["by_k"].get(str(k)) or {}).items()]
        n = sum(1 for _, _, r in verdicts if r.get("verdict") in ("PASS", "FAIL"))
        fail = sum(1 for _, _, r in verdicts if r.get("verdict") == "FAIL")
        j5_only = sum(1 for _, _, r in verdicts if r.get("verdict") == "FAIL" and set(r["fails"]) == {"J5"})
        code_fail = sum(1 for _, _, r in verdicts if r.get("verdict") == "FAIL" and any(not f.startswith("J") for f in r["fails"]))
        qs = [variant[(b, k)] for b in bases if (b, k) in variant]
        fmt = [len(applied[q] & FORMAT) for q in qs]
        per_k.append({"k": k, "n": n, "fail": fail, "fail_rate": round(fail / n, 4) if n else None,
                      "fail_without_j5_only": fail - j5_only, "rate_without_j5_only": round((fail - j5_only) / n, 4) if n else None,
                      "code_fail": code_fail, "code_fail_rate": round(code_fail / n, 4) if n else None,
                      "variants": len(qs), "format_mean": round(sum(fmt) / len(fmt), 2) if fmt else None,
                      "format3_share": round(sum(1 for x in fmt if x >= 3) / len(fmt), 4) if fmt else None})
    jumps = []
    for k in JUMPS:
        kinds: Counter = Counter()
        detail: Counter = Counter()
        flips = 0
        for b, v in bases.items():
            cur, prev = v["by_k"].get(str(k)), v["by_k"].get(str(k - 1))
            qc, qp = variant.get((b, k)), variant.get((b, k - 1))
            if not cur or not prev or qc is None or qp is None:
                continue
            added = applied[qc] - applied[qp]
            for p, r in cur.items():
                if p not in prev:
                    continue
                flips += r.get("verdict") == "FAIL" and prev[p].get("verdict") != "FAIL"
                for lab in set(r["fails"]) - set(prev[p]["fails"]):
                    kind = "j5" if lab == "J5" else "added" if lab in added else "existing" if lab in applied[qp] else "unmarked"
                    kinds[kind] += 1
                    detail[f"{kind}:{lab}"] += 1
        # 정면 충돌인가: JSON(구조·코드블록)과 표·목록·제목을 함께 요구한 변형만 따로 FAIL 비율을 본다
        combo = {b for b in bases if (b, k) in variant and applied[variant[(b, k)]] & {"2-2.json_structure", "1-2.code_block"}
                 and applied[variant[(b, k)]] & {"2-2.table", "2-2.list", "2-2.heading"}}

        def rate(selected):
            rs = [r for b in selected for r in (bases[b]["by_k"].get(str(k)) or {}).values() if r.get("verdict") in ("PASS", "FAIL")]
            return {"fail": sum(1 for r in rs if r["verdict"] == "FAIL"), "n": len(rs)}
        jumps.append({"k": k, "new_fails": sum(kinds.values()), "pass_to_fail": flips, "kinds": dict(kinds),
                      "top": [{"kind": key.split(":")[0], "label": key.split(":", 1)[1], "count": c} for key, c in detail.most_common(8)],
                      "json_plus_format": {"bases": len(combo), "with": rate(combo), "without": rate(set(bases) - combo)}})
    s326 = json.loads(SET326.read_text(encoding="utf-8"))
    rep326 = qg.check(s326)
    kmap = qg.k_var_by_group(s326, rep326)
    lab326 = labels(rep326)
    by_k326: Dict[int, List[int]] = defaultdict(list)
    for q, v in kmap.items():
        by_k326[v["k"]].append(len(lab326[q] & FORMAT))
    set326 = [{"k": k, "questions": len(v), "format3_share": round(sum(1 for x in v if x >= 3) / len(v), 4)} for k, v in sorted(by_k326.items())]
    return {"version": VERSION, "run": grid.get("run"), "rule": grid.get("rule"), "providers": grid.get("providers"), "bases": len(bases),
            "format_checks": sorted(FORMAT), "checks": {"all_grid_keys_matched": not missing}, "per_k": per_k, "jumps": jumps, "set326": set326}


KIND_NAMES = {"existing": "원래 있던 조건이 새로 깨짐", "added": "새로 붙은 조건", "j5": "J5 (엄격 규칙)", "unmarked": "적용 표시 없음"}


def markdown(r: Dict[str, Any]) -> str:
    lines = [f"# k 9·12 FAIL 급증 원인 ({r['version']})", "",
             f"실행 `{r['run']}` · 뼈대 {r['bases']}개 × {', '.join(r['providers'])} · 판정 규칙 {r['rule']}. 형식 요구 = {', '.join(r['format_checks'])}.", "",
             "| k | 판정 수 | FAIL 비율 | J5 하나로만 FAIL 뺀 비율 | CODE 위반 있는 비율 | 형식 요구 평균 | 형식 요구 3개↑ |", "|---|---|---|---|---|---|---|"]
    for x in r["per_k"]:
        lines.append(f"| {x['k']} | {x['n']} | {x['fail_rate']:.0%} | {x['rate_without_j5_only']:.0%} | {x['code_fail_rate']:.0%} | {x['format_mean']} | {x['format3_share']:.0%} |")
    for j in r["jumps"]:
        lines += ["", f"## k {j['k'] - 1} → {j['k']} — 새로 생긴 FAIL {j['new_fails']}건 (PASS→FAIL {j['pass_to_fail']}건)", "",
                  "| 갈래 | 건수 |", "|---|---|"] + [f"| {KIND_NAMES[k]} | {c} |" for k, c in sorted(j["kinds"].items(), key=lambda kv: -kv[1])]
        lines += ["", "많이 나온 것: " + " · ".join(f"{KIND_NAMES[t['kind']]} {t['label']} {t['count']}" for t in j["top"])]
        c = j["json_plus_format"]
        lines += ["", f"JSON 과 표·목록·제목을 함께 요구한 변형 {c['bases']}개의 FAIL {c['with']['fail']}/{c['with']['n']} · 나머지 {c['without']['fail']}/{c['without']['n']}"]
    lines += ["", "## 지금 326 세트 — k 별 형식 요구 3개↑ 비율", "", "| k | 질문 수 | 형식 요구 3개↑ |", "|---|---|---|"]
    lines += [f"| {x['k']} | {x['questions']} | {x['format3_share']:.0%} |" for x in r["set326"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = build()
    stem = OUT / f"k급증_원인_{result['run']}"
    stem.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown(result), encoding="utf-8")
    if not all(result["checks"].values()):
        print("경고: 실험 판정과 질문이 다 짝지어지지 않음 —", result["checks"])
    print(markdown(result))
    print("저장:", stem.with_suffix(".json"), "·", stem.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
