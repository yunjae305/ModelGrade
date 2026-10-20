"""질문 수별 통과 비율 비교 (subset-pass-rates-v1) — 기준마다 20번 목표로 고른 198개(기준 많은 순)·155개(모자란 기준부터)가
정밀 326개와 같은 통과 비율을 내는지 본다 (10-19 오후 미팅 51:09~51:29 · 체크리스트 n5). AI 호출 없음, 결과 파일만 읽음.

    python -X utf8 tools/subset_pass_rates.py
    → 참고자료/최소평가/통과비율_목록별_8c61f117e661.json · .md

- 판정: 저장소의 326 결과 JSON(참고자료/결과/8c61f117e661_정밀326_2026-10-19.json)으로 질문마다 PASS·FAIL·보류를 다시 센다 —
  tools/question_gradient.verdicts_from_run 과 같은 규칙(답 못 받음 FAIL·빈 출력 요구 보류·CODE 나 의미 FAIL 하나면 FAIL·미완료 보류, 대화 1개 = 1개).
  README 의 326 숫자와 같은지 맞춰 본다(다르면 경고).
- 목록: 기준 많은 순(질문을 기준 수 많은 순으로 훑으며 아직 목표에 못 미친 기준을 하나라도 더하는 질문만 — v1) · 모자란 기준부터(표준 탐욕 —
  tools/question_gradient.order_by_k 순서의 앞부분, 10-19 발표 07장 표의 7·21·37·78·155 와 같은 계산). 목표 1·3·5·10·20번의 질문 수가
  저장된 비교(order_compare_kdesc_vs_greedy.json)와 같은지 맞춰 본다. 10번 목록은 간단 78(질문세트_최소78)과 같은 질문들.
  tools/min_eval_list.select 는 동점 처리가 달라 20번에서 156개(3개 다름) — 결과를 alternates 에 함께 남긴다.
- 무작위: 같은 개수를 326 에서 무작위로 2,000번 뽑았을 때 통과율의 95% 범위 — 고른 목록의 차이가 우연 범위 안인지 보는 기준.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from tools.min_eval_list import applied_by_question, select  # noqa: E402
from tools.question_gradient import order_by_k  # noqa: E402
from webapp import question_generation as qg  # noqa: E402
from webapp.run_summary import asks_empty_output, unit_verdict  # noqa: E402

VERSION = "subset-pass-rates-v1"
SET_DIR = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"
RESULTS = ROOT / "참고자료" / "결과" / "8c61f117e661_정밀326_2026-10-19.json"
OUT = ROOT / "참고자료" / "최소평가"
SAVED_SIZES = OUT / "order_compare_kdesc_vs_greedy.json"
#: README(참고자료/최소평가) 의 정밀 326 판정 — 다시 센 값이 이것과 같아야 같은 규칙
README_326 = {"gpt": (190, 77, 59), "aiona": (166, 84, 76), "claude": (131, 112, 83), "gemini": (120, 124, 82)}
LISTS = [("all", 0, "정밀 326"), ("kdesc", 20, "20번 · 기준 많은 순"), ("greedy", 20, "20번 · 모자란 기준부터"),
         ("kdesc", 10, "10번 · 기준 많은 순"), ("greedy", 10, "10번 · 모자란 기준부터")]
DRAWS = 2000


def select_kdesc(applied: Dict[int, set], need: int, universe: set) -> List[int]:
    """기준 많은 순(v1) — 기준 수 많은 순(같으면 번호순)으로 훑으며 아직 목표에 못 미친 기준을 하나라도 더하는 질문만 고른다."""
    available = {t: sum(1 for s in applied.values() if t in s) for t in universe}
    target = {t: min(need, available[t]) for t in universe}
    counts: Counter = Counter()
    chosen = []
    for q in sorted(applied, key=lambda q: (-len(applied[q]), q)):
        if all(counts[t] >= target[t] for t in universe):
            break
        if any(counts[t] < target[t] for t in applied[q] if t in universe):
            chosen.append(q)
            counts.update(applied[q])
    return chosen


def select_greedy(groups: List[Dict[str, Any]], report: Dict[str, Any], need: int, universe: set) -> List[int]:
    """모자란 기준부터(표준 탐욕) — question_gradient 순서에서 모든 기준이 목표(세트에 있는 만큼까지)에 닿을 때까지의 앞부분."""
    order, _, applied = order_by_k(groups, report, need)
    available = {t: sum(1 for s in applied.values() if t in s) for t in universe}
    target = {t: min(need, available[t]) for t in universe}
    counts: Counter = Counter()
    for i, q in enumerate(order, 1):
        counts.update(applied[q])
        if all(counts[t] >= target[t] for t in universe):
            return order[:i]
    return order


def turn_outcome(turn: Dict[str, Any]) -> str:
    """결과 JSON 의 턴 한 줄 → PASS|FAIL|PENDING — run_summary.question_outcome 과 같은 규칙(공통 CODE 검사 + 옛 의미 평가)."""
    code = turn.get("code") or []
    semantic = turn.get("semantic_legacy") or {}
    decisions = semantic.get("decisions") or {}
    criteria = semantic.get("criteria") if isinstance(semantic.get("criteria"), list) else list(decisions)
    code_fail = any(x.get("verdict") == "FAIL" for x in code)
    semantic_fail = any((decisions.get(c) or {}).get("decision") == "FAIL" for c in criteria)
    code_complete = bool(code) and all(x.get("verdict") in ("PASS", "FAIL") for x in code)
    semantic_complete = (semantic.get("status") == "SUCCESS" and bool(criteria)
                         and all((decisions.get(c) or {}).get("decision") in ("PASS", "FAIL") for c in criteria))
    if code_fail or semantic_fail:
        return "FAIL"
    return "PASS" if code_complete and semantic_complete else "PENDING"


def verdicts_from_results(data: Dict[str, Any], groups: List[Dict[str, Any]]) -> Dict[str, Dict[int, str]]:
    """{모델: {질문 번호: PASS|FAIL|HOLD}} — question_gradient.verdicts_from_run 과 같은 규칙."""
    text = {r["case_id"]: r.get("question") or "" for r in qg.flatten(groups)}
    out: Dict[str, Dict[int, str]] = {}
    for provider, section in (data.get("providers") or {}).items():
        per_q: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for turn in section.get("turns") or []:
            q = int(turn["question"])
            empty = asks_empty_output(None, text.get(turn["case_id"])) or any(
                str(x.get("check_id") or "").startswith("2-1.explicit_empty") for x in turn.get("code") or [])
            if empty:
                per_q[q].append({"outcome": "PENDING", "hold": "empty_output"})   # 수집 실패여도 보류 (empty-output-hold-v2, 10-19)
            elif turn.get("status") != "SUCCESS":
                per_q[q].append({"outcome": "FAIL"})   # 답을 받지 못함 — FAIL, 분모 유지
            else:
                per_q[q].append({"outcome": turn_outcome(turn)})
        out[provider] = {q: ("FAIL" if (k := unit_verdict(kinds)) in ("FAIL", "EXCLUDED") else "PASS" if k == "PASS" else "HOLD")
                         for q, kinds in per_q.items()}
    return out


def tally(verdicts: Dict[str, Dict[int, str]], ids: List[int]) -> Dict[str, Dict[str, Any]]:
    out = {}
    for provider, v in verdicts.items():
        c = Counter(v[q] for q in ids if q in v)
        n = sum(c.values())
        out[provider] = {"n": n, "pass": c["PASS"], "fail": c["FAIL"], "hold": c["HOLD"],
                         "rate": round(c["PASS"] / n, 4) if n else None,
                         "decided_rate": round(c["PASS"] / (c["PASS"] + c["FAIL"]), 4) if c["PASS"] + c["FAIL"] else None}
    return out


def ranking(rows: Dict[str, Dict[str, Any]]) -> List[str]:
    return sorted(rows, key=lambda p: (-(rows[p]["rate"] or 0), p))


def random_band(verdicts: Dict[str, Dict[int, str]], ids: List[int], n: int, ref_rank: List[str], draws: int = DRAWS, seed: int = 0) -> Dict[str, Any]:
    """326 에서 n 개를 무작위로 draws 번 — 모델별 통과율 2.5~97.5% 범위와 순위가 정밀과 같은 비율."""
    rng = random.Random(seed)
    rates: Dict[str, List[float]] = defaultdict(list)
    same = 0
    for _ in range(draws):
        pick = rng.sample(ids, n)
        rows = tally(verdicts, pick)
        for p, r in rows.items():
            rates[p].append(r["rate"])
        same += ranking(rows) == ref_rank
    band = {}
    for p, values in rates.items():
        values.sort()
        band[p] = {"low": round(values[int(0.025 * draws)], 4), "high": round(values[int(0.975 * draws) - 1], 4),
                   "mean": round(sum(values) / draws, 4)}
    return {"n": n, "draws": draws, "seed": seed, "rate_band": band, "same_ranking_share": round(same / draws, 4)}


def build(results_path: Path = RESULTS, set_dir: Path = SET_DIR) -> Dict[str, Any]:
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    report = qg.check(groups)
    applied = applied_by_question(groups, report)
    universe = set(qg.target_counts(report).keys())
    sizes = {str(need): {"k_desc_skip": len(select_kdesc(applied, need, universe)),
                         "greedy": len(select_greedy(groups, report, need, universe))} for need in (1, 3, 5, 10, 20)}
    saved = json.loads(SAVED_SIZES.read_text(encoding="utf-8")) if SAVED_SIZES.exists() else {}
    sizes_match = all(saved.get(k, {}).get(m) == v[m] for k, v in sizes.items() for m in ("k_desc_skip", "greedy")) if saved else None
    data = json.loads(results_path.read_text(encoding="utf-8"))
    verdicts = verdicts_from_results(data, groups)
    all_ids = sorted(g["id"] for g in groups)
    full = tally(verdicts, all_ids)
    full_match = all((full[p]["pass"], full[p]["fail"], full[p]["hold"]) == README_326[p] for p in README_326 if p in full)
    ref_rank = ranking(full)
    lists = []
    for kind, need, label in LISTS:
        ids = all_ids if kind == "all" else select_kdesc(applied, need, universe) if kind == "kdesc" else select_greedy(groups, report, need, universe)
        rows = tally(verdicts, ids)
        entry = {"key": f"{kind}{need or ''}", "kind": kind, "need": need, "label": label, "n": len(ids), "ids": ids, "models": rows,
                 "ranking": ranking(rows), "same_ranking": ranking(rows) == ref_rank,
                 "diff_pp": {p: round((rows[p]["rate"] - full[p]["rate"]) * 100, 1) for p in rows}}
        if kind != "all":
            entry["random"] = random_band(verdicts, all_ids, len(ids), ref_rank)
            entry["inside_random"] = {p: entry["random"]["rate_band"][p]["low"] <= rows[p]["rate"] <= entry["random"]["rate_band"][p]["high"] for p in rows}
        lists.append(entry)
    alt_ids = [c["id"] for c in select(groups, applied, 20, universe)["chosen"]]
    alt = {"label": "20번 · 모자란 기준부터 (min_eval_list 동점 처리)", "n": len(alt_ids), "ids": alt_ids, "models": tally(verdicts, alt_ids)}
    return {"version": VERSION, "run_id": data.get("run_id"), "results_file": str(results_path.relative_to(ROOT)).replace("\\", "/"),
            "rule": "질문 단위(대화 1개 = 1개) · 보류는 분모에 둠 · 통과율 = PASS / n · 확정 통과율 = PASS / (PASS + FAIL)",
            "checks": {"sizes": sizes, "sizes_match_saved": sizes_match, "full_326_match_readme": full_match},
            "reference_ranking": ref_rank, "lists": lists, "alternates": [alt]}


MODEL_NAMES = {"gpt": "GPT-6 Luna", "aiona": "AIONA", "claude": "Claude Sonnet 5.5", "gemini": "Gemini 3.6 Flash"}


def markdown(result: Dict[str, Any]) -> str:
    lines = [f"# 질문 수별 통과 비율 — 326 · 198 · 155 ({result['version']})", "",
             f"실행 `{result['run_id']}`(10-19 정밀 326, 네 모델) 판정을 그대로 다시 셈. {result['rule']}.",
             f"점검: 목록 크기가 저장된 비교와 {'같음' if result['checks']['sizes_match_saved'] else '**다름**'} · "
             f"326 판정이 README 와 {'같음' if result['checks']['full_326_match_readme'] else '**다름**'}.", "",
             "| 목록 | 질문 수 | 모델 | PASS · FAIL · 보류 | 통과율 | 326 과 차이 | 무작위 같은 수 95% 범위 | 확정 통과율 |",
             "|---|---|---|---|---|---|---|---|"]
    for entry in result["lists"]:
        for p in result["reference_ranking"]:
            r = entry["models"][p]
            band = (entry.get("random") or {}).get("rate_band", {}).get(p)
            lines.append(f"| {entry['label']} | {entry['n']} | {MODEL_NAMES.get(p, p)} | {r['pass']} · {r['fail']} · {r['hold']} | {r['rate']:.1%} | "
                         f"{entry['diff_pp'][p]:+.1f}pp | {f'{band['low']:.1%} ~ {band['high']:.1%}' if band else '—'} | {r['decided_rate']:.1%} |")
    lines += ["", "| 목록 | 질문 수 | 순위 | 정밀과 같은 순위 | 무작위 같은 수에서 순위가 같은 비율 |", "|---|---|---|---|---|"]
    for entry in result["lists"]:
        share = (entry.get("random") or {}).get("same_ranking_share")
        lines.append(f"| {entry['label']} | {entry['n']} | {' > '.join(MODEL_NAMES.get(p, p) for p in entry['ranking'])} | "
                     f"{'같음' if entry['same_ranking'] else '다름'} | {f'{share:.0%}' if share is not None else '—'} |")
    for alt in result.get("alternates") or []:
        lines += ["", f"참고: {alt['label']} 는 {alt['n']}개 — 통과율 " + " · ".join(
            f"{MODEL_NAMES.get(p, p)} {alt['models'][p]['rate']:.1%}" for p in result["reference_ranking"]) + "."]
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", type=Path, default=RESULTS, help="326 결과 JSON (참고자료/결과/…)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = build(args.results)
    stem = OUT / f"통과비율_목록별_{result['run_id']}"
    stem.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown(result), encoding="utf-8")
    checks = result["checks"]
    if not checks["sizes_match_saved"] or not checks["full_326_match_readme"]:
        print("경고: 재현 점검이 맞지 않음 —", json.dumps(checks, ensure_ascii=False))
    print(markdown(result))
    print("저장:", stem.with_suffix(".json"), "·", stem.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
