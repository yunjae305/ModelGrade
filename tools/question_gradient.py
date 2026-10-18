"""기울기 그래프 — 질문을 **적용 기준 수(k_var)가 많은 순서**로 넣어 가며, 몇 번째 질문에서 기준 커버리지가 차고 모델별 통과율·순위가
안정되는지 본다 (사용자 생각 2026-10-18: 기준이 많은 질문부터 던지면 적은 질문으로도 평가가 끝난다). 모델·웹 호출 없음.

    python tools/question_gradient.py <run_id>                       # 서버가 평가까지 끝낸 실행 (run JSON 의 evaluations)
    python tools/question_gradient.py <run_id> --records-dir DIR     # 실험용: DIR/luna-<provider>.jsonl (+ jev-<provider>.jsonl) 로 판정
    옵션: --step 10 --min-apply 10 --tolerance 0.03 --out 참고자료/분석/<폴더>

- 단위: 단일 질문 1개 = 1, 대화 1개 = 1. k_var 는 질문 생성기와 같은 정의(``question_generation.k_var_by_group``, 변동 기준 54개).
- 순서: k_var 가 큰 질문부터. 같은 k 면 아직 적용 수가 ``--min-apply`` 에 못 미친 기준을 더 많이 채우는 질문부터(욕심 방식).
- 질문 판정(모델마다): 유효 FAIL(CODE·의미)이 하나라도 있으면 FAIL, 모두 판정되고 FAIL 없으면 PASS, 그 밖은 보류(분모에 둠).
  답을 못 받은 질문(수집 실패)은 모델 쪽 원인이므로 FAIL 로 센다(사용자 규칙). 통과율 = PASS / n.
- 안정 지점: 그 수 이상 어디서 잘라도 ① 모델 순위가 전체 세트와 같고 ② 모델마다 통과율이 전체 세트 값의 ±tolerance 안인 가장 작은 n.
- 결과: <out>/gradient.md(표·결론), gradient.svg(통과율·커버리지 선 그래프), gradient.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp import question_generation as qg  # noqa: E402
from webapp.run_summary import question_outcomes, unit_outcome  # noqa: E402

RUNS = ROOT / "data" / "comparison_runs"
SETS = ROOT / "data" / "question_sets"
JEV_CRITERIA = ("J1", "J2", "J3", "J4")
#: J5 FAIL 가운데 '확인 실패'라 보류로 두는 사유 코드 — semantic_policy(semantic-hold-unverified-v3)·run_summary.LIMIT_J5_CODES 와 같은 집합.
#: 확정 FAIL(사실 오류·출처 불일치·답변이 든 URL 접근 불가 등)은 그대로 FAIL
HOLD_J5_CODES = {"insufficient_evidence", "source_unavailable", "search_limit_reached", "held"}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _qid(case_id: str) -> int:
    return int(str(case_id).split("_T")[0][1:])


# --- 판정 ---------------------------------------------------------------------------------------------------------------

def verdicts_from_run(run: Dict[str, Any]) -> Dict[str, Dict[int, str]]:
    """서버가 평가한 실행 — {provider: {질문 id: PASS|FAIL|HOLD}}. run_summary 와 같은 질문 판정 규칙.
    행 번호 ↔ 질문은 실행의 질문 세트를 펼친 순서로 정한다(빈 행이 있어도 밀리지 않게, Codex 검토). 답을 받지 못한 질문(수집 실패·빈 행)은
    모든 모델에서 FAIL — 모델 쪽 원인은 FAIL, 분모 유지 (사용자 규칙). 보류(미완료)는 HOLD 로 분모에 둔다."""
    from webapp.question_generation import flatten
    set_id = (run.get("question_set") or {}).get("set_id")
    flat = flatten(_load(SETS / str(set_id) / "questions.json")) if set_id and (SETS / str(set_id) / "questions.json").exists() else None
    out: Dict[str, Dict[int, str]] = {}
    for provider in run["providers"]:
        batch = (run.get("evaluations") or {}).get(provider)
        rows = run["datasets"][provider]
        case_ids = [r["case_id"] for r in flat] if flat and len(flat) == len(rows) else [(r or {}).get("case_id") for r in rows]
        if any(c is None for c in case_ids):
            raise SystemExit(f"{provider}: 질문 세트 없이 빈 행의 질문 번호를 정할 수 없습니다.")
        outcomes = question_outcomes(batch, len(rows)) if batch else [{"outcome": "PENDING"}] * len(rows)
        per_q: Dict[int, List[str]] = defaultdict(list)
        for index, cid in enumerate(case_ids):
            row = rows[index]
            # 빈 행·명시적 수집 실패만 '답 없음' — 직접 올린 원문(collection 없음)은 수집된 답으로 본다 (Codex 검토)
            collected = row is not None and (row.get("collection") or {}).get("status", "SUCCESS") == "SUCCESS"
            per_q[_qid(cid)].append("FAIL" if not collected else outcomes[index]["outcome"])
        out[provider] = {q: ("FAIL" if (k := unit_outcome(kinds)) in ("FAIL", "EXCLUDED") else "PASS" if k == "PASS" else "HOLD")
                         for q, kinds in per_q.items()}
    return out


def verdicts_from_records(run: Dict[str, Any], records_dir: Path) -> Dict[str, Dict[int, str]]:
    """실험용 — jsonl 판정 파일(Luna 가 모든 기준, Jev 가 J1~J4)과 CODE 평가로 질문 판정. 병행 규칙: 둘이 갈리면 보류(재판정 없음),
    J5 확인 실패는 보류. 수집 실패는 FAIL."""
    from code_eval import engine
    from code_eval.config import load_aiona_config
    cfg = load_aiona_config(); cfg.enabled = False

    def jsonl(name: str) -> Dict[str, Dict[str, Any]]:
        path = records_dir / name
        if not path.exists():
            return {}
        return {r["case_id"]: r for r in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())}

    out: Dict[str, Dict[int, str]] = {}
    for provider in run["providers"]:
        luna, jev = jsonl(f"luna-{provider}.jsonl"), jsonl(f"jev-{provider}.jsonl")
        rows = [r for r in run["datasets"][provider] if r]
        cases = [{k: r[k] for k in ("case_id", "question", "answer", "reference", "conversation_id", "turn", "attachments") if r.get(k) not in (None, "")}
                 for r in rows if (r.get("collection") or {}).get("status") == "SUCCESS" or provider == "aiona"]
        for c in cases:
            c.setdefault("answer", "")
        code_fail: Dict[int, bool] = defaultdict(bool)
        for c in engine.evaluate_batch(cases, cfg)["cases"]:
            for r in c.get("results") or []:
                if r.get("status") == "EXECUTED" and r.get("verdict") == "FAIL" and not str(r.get("check_id", "")).endswith(":none"):
                    code_fail[_qid(c["case_id"])] = True
        state: Dict[int, Dict[str, bool]] = defaultdict(lambda: {"fail": False, "hold": False, "seen": False})
        for row in rows:
            q = _qid(row["case_id"])
            if (row.get("collection") or {}).get("status") != "SUCCESS" and provider != "aiona":
                state[q]["fail"] = True
                continue
            record = luna.get(row["case_id"])
            if not record or record.get("status") != "SUCCESS":
                state[q]["hold"] = True
                continue
            state[q]["seen"] = True
            jd = (jev.get(row["case_id"]) or {}).get("decisions") or {} if (jev.get(row["case_id"]) or {}).get("status") == "SUCCESS" else {}
            for cid, d in (record.get("decisions") or {}).items():
                decision = d.get("decision")
                j = (jd.get(cid) or {}).get("decision")
                if cid in JEV_CRITERIA and j in ("PASS", "FAIL") and decision in ("PASS", "FAIL") and j != decision:
                    state[q]["hold"] = True
                    continue
                if decision == "FAIL" and cid == "J5" and (d.get("j5_outcome") or {}).get("code") in HOLD_J5_CODES:
                    state[q]["hold"] = True
                    continue
                if decision == "FAIL":
                    state[q]["fail"] = True
                elif decision != "PASS" and d.get("status") not in ("NOT_APPLICABLE", None):
                    state[q]["hold"] = True
        verdict: Dict[int, str] = {}
        for q, s in state.items():
            if s["fail"] or code_fail[q]:
                verdict[q] = "FAIL"
            elif s["hold"] or not s["seen"]:
                verdict[q] = "HOLD"
            else:
                verdict[q] = "PASS"
        out[provider] = verdict
    return out


# --- 순서·기울기 ---------------------------------------------------------------------------------------------------------

def order_by_k(groups: List[Dict[str, Any]], report: Dict[str, Any], min_apply: int) -> Tuple[List[int], Dict[int, int], Dict[int, set]]:
    """k_var 가 큰 질문부터. 같은 k 면 아직 min_apply 에 못 미친 기준을 많이 채우는 질문부터. 반환 (질문 id 순서, k, 질문별 적용 기준 집합)."""
    kmap = qg.k_var_by_group(groups, report)
    countable = set(qg.target_counts(report).keys())   # 질문으로 셀 수 있는 기준 64
    applied: Dict[int, set] = defaultdict(set)
    for row in report.get("cases") or []:
        applied[_qid(row["case_id"])].update(t for t in (row.get("checks") or []) + (row.get("items") or []) if t in countable)
    remaining = {g["id"] for g in groups}
    counts: Counter = Counter()
    order: List[int] = []
    while remaining:
        best = max(remaining, key=lambda q: (kmap[q]["k"], sum(1 for t in applied[q] if counts[t] < min_apply), -q))
        remaining.remove(best)
        order.append(best)
        counts.update(applied[best])
    return order, {q: kmap[q]["k"] for q in kmap}, applied


def gradient(order: List[int], applied: Dict[int, set], verdicts: Dict[str, Dict[int, str]], total_criteria: int,
             step: int, min_apply: int, tolerance: float) -> Dict[str, Any]:
    providers = list(verdicts)
    n_all = len(order)
    rows = []
    for n in range(1, n_all + 1):   # 안정 지점·커버리지 도달은 모든 누적 수에서 본다 — step 은 표시 간격일 뿐 (Codex 검토)
        picked = order[:n]
        counts: Counter = Counter(t for q in picked for t in applied[q])
        models = {}
        for p in providers:
            tally = Counter(verdicts[p].get(q, "HOLD") for q in picked)
            models[p] = {"pass": tally["PASS"], "fail": tally["FAIL"], "hold": tally["HOLD"], "rate": round(tally["PASS"] / n, 4)}
        ranking = sorted(providers, key=lambda p: (-models[p]["rate"], p))
        rows.append({"n": n, "covered_1": sum(1 for t in counts.values() if t >= 1), "covered_min": sum(1 for t in counts.values() if t >= min_apply),
                     "models": models, "ranking": ranking})
    full = rows[-1]
    stable_n = None
    for i, row in enumerate(rows):
        later = rows[i:]
        if all(r["ranking"] == full["ranking"] for r in later) and \
           all(abs(r["models"][p]["rate"] - full["models"][p]["rate"]) <= tolerance for r in later for p in providers):
            stable_n = row["n"]
            break
    cover_1_n = next((r["n"] for r in rows if r["covered_1"] >= total_criteria), None)
    cover_min_n = next((r["n"] for r in rows if r["covered_min"] >= total_criteria), None)
    shown = [r for r in rows if r["n"] % step == 0 or r["n"] == n_all]
    return {"rows": shown, "full_ranking": full["ranking"], "stable_n": stable_n, "coverage_1_n": cover_1_n, "coverage_min_n": cover_min_n,
            "total_criteria": total_criteria, "min_apply": min_apply, "tolerance": tolerance, "n": n_all, "step": step}


# --- 출력 -----------------------------------------------------------------------------------------------------------------

def _svg(result: Dict[str, Any], labels: Dict[str, str]) -> str:
    rows = result["rows"]; providers = list(rows[0]["models"])
    width, height, left, top, right, bottom = 760, 420, 60, 30, 20, 60
    plot_w, plot_h = width - left - right, height - top - bottom
    n_max = rows[-1]["n"]
    x = lambda n: left + plot_w * n / n_max
    y = lambda v: top + plot_h * (1 - v)
    colors = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b"]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" font-family="sans-serif" font-size="12">',
             f'<rect width="{width}" height="{height}" fill="white"/>']
    for v in (0, .25, .5, .75, 1):
        parts.append(f'<line x1="{left}" y1="{y(v):.1f}" x2="{width - right}" y2="{y(v):.1f}" stroke="#ddd"/>'
                     f'<text x="{left - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{int(v * 100)}%</text>')
    for row in rows:
        parts.append(f'<text x="{x(row["n"]):.1f}" y="{height - bottom + 16}" text-anchor="middle" fill="#555">{row["n"]}</text>')
    total = result["total_criteria"]
    cov = " ".join(f'{x(r["n"]):.1f},{y(r["covered_min"] / total):.1f}' for r in rows)
    parts.append(f'<polyline points="{cov}" fill="none" stroke="#999" stroke-width="2" stroke-dasharray="6 4"/>')
    for i, p in enumerate(providers):
        pts = " ".join(f'{x(r["n"]):.1f},{y(r["models"][p]["rate"]):.1f}' for r in rows)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{colors[i % len(colors)]}" stroke-width="2.5"/>')
        parts.append(f'<text x="{left + 10 + i * 150}" y="{height - 14}" fill="{colors[i % len(colors)]}" font-weight="bold">{labels.get(p, p)}</text>')
    parts.append(f'<text x="{left + 10 + len(providers) * 150}" y="{height - 14}" fill="#999">기준 {result["min_apply"]}회 이상 커버리지(점선)</text>')
    if result["stable_n"]:
        parts.append(f'<line x1="{x(result["stable_n"]):.1f}" y1="{top}" x2="{x(result["stable_n"]):.1f}" y2="{top + plot_h}" stroke="#333" stroke-dasharray="3 3"/>'
                     f'<text x="{x(result["stable_n"]) + 4:.1f}" y="{top + 12}" fill="#333">안정 {result["stable_n"]}개</text>')
    parts.append(f'<text x="{width / 2}" y="{height - bottom + 36}" text-anchor="middle" fill="#333">질문 수 (적용 기준 수가 많은 순서로 넣음)</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def _markdown(result: Dict[str, Any], labels: Dict[str, str], run_id: str, kdist: Counter) -> str:
    rows = result["rows"]; providers = list(rows[0]["models"])
    head = "| 질문 수 | 기준 1회↑ | 기준 " + str(result["min_apply"]) + "회↑ | " + " | ".join(f"{labels.get(p, p)} PASS·FAIL·보류 (통과율)" for p in providers) + " | 순위 |"
    sep = "|" + "---|" * (3 + len(providers) + 1)
    lines = [f"# 기울기 그래프 — 실행 {run_id}", "",
             f"질문을 적용 기준 수(k_var)가 많은 순서로 넣었다. k 분포: {dict(sorted(kdist.items()))}. 기준 {result['total_criteria']}개(질문으로 셀 수 있는 것), "
             f"통과율 = PASS / 질문 수(보류·수집 실패 포함). 순위 안정 허용 오차 ±{int(result['tolerance'] * 100)}pp.", "",
             f"- 모든 기준이 1회 이상 적용되는 질문 수: **{result['coverage_1_n'] or '전체로도 미달'}**",
             f"- 모든 기준이 {result['min_apply']}회 이상 적용되는 질문 수: **{result['coverage_min_n'] or '전체로도 미달'}**",
             f"- 모델 순위·통과율이 안정되는 질문 수: **{result['stable_n'] or '전체 세트까지 흔들림'}** (전체 순위: {' > '.join(labels.get(p, p) for p in result['full_ranking'])})",
             "", "![기울기](gradient.svg)", "", head, sep]
    for r in rows:
        cells = [str(r["n"]), str(r["covered_1"]), str(r["covered_min"])]
        cells += [f"{m['pass']}·{m['fail']}·{m['hold']} ({m['rate']:.0%})" for m in (r["models"][p] for p in providers)]
        cells.append(" > ".join(labels.get(p, p)[:6] for p in r["ranking"]))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--records-dir", type=Path, help="실험용 판정 jsonl 폴더 (luna-<provider>.jsonl, jev-<provider>.jsonl)")
    ap.add_argument("--set-dir", type=Path, help="질문 세트 폴더 (기본: run 의 question_set.set_id)")
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--min-apply", type=int, default=10)
    ap.add_argument("--tolerance", type=float, default=0.03)
    ap.add_argument("--out", type=Path, help="결과 폴더 (기본: 참고자료/분석/<날짜>-기울기-<run_id>)")
    args = ap.parse_args(argv)
    run = _load(RUNS / f"{args.run_id}.json")
    set_dir = args.set_dir or SETS / str((run.get("question_set") or {}).get("set_id"))
    groups = _load(set_dir / "questions.json")
    report = qg.check(groups)
    if report.get("errors") or report.get("undetermined"):
        raise SystemExit("질문 세트 점검에서 판별 불가 — 멈춤")
    order, kmap, applied = order_by_k(groups, report, args.min_apply)
    verdicts = verdicts_from_records(run, args.records_dir) if args.records_dir else verdicts_from_run(run)
    total_criteria = len(qg.target_counts(report))
    result = gradient(order, applied, verdicts, total_criteria, args.step, args.min_apply, args.tolerance)
    result.update(run_id=args.run_id, order=order, k=kmap)
    labels = {p: ("AIONA" if p == "aiona" else str(run.get("models", {}).get(p) or p)) for p in verdicts}
    from datetime import date
    out = args.out or ROOT / "참고자료" / "분석" / f"{date.today().isoformat()}-기울기-{args.run_id}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "gradient.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=sorted), encoding="utf-8")
    (out / "gradient.svg").write_text(_svg(result, labels), encoding="utf-8")
    (out / "gradient.md").write_text(_markdown(result, labels, args.run_id, Counter(kmap.values())), encoding="utf-8")
    print(f"저장: {out}")
    print(f"기준 1회↑ 커버 {result['coverage_1_n']} · {args.min_apply}회↑ 커버 {result['coverage_min_n']} · 순위·통과율 안정 {result['stable_n']} / 전체 {result['n']}")
    for r in result["rows"]:
        print(r["n"], r["covered_1"], r["covered_min"], " | ".join(f"{labels[p][:10]} {r['models'][p]['rate']:.0%}" for p in verdicts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
