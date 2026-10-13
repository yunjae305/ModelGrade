"""기준별 적정 질문 수 검토 — 저장된 비교 실행(과 선택적 사람 정답)으로 적용 단위 5·10·20·30·50·100 의 안정성·일치율을 본다 (API 호출 없음).

    python tools/sample_size_study.py data/comparison_runs/<run>.json                    # 사람이 읽는 보고
    python tools/sample_size_study.py <run>.json --gold 정답.jsonl --json                # 사람 정답과 일치율까지

- 단위: 단일 질문 1개 = 1, 대화 1개 = 1 (``run_summary.question_units`` 와 같다). 기준이 단위의 어느 턴에든 적용되면 그 기준의 적용 단위.
- 단위 판정: 적용 턴 중 하나라도 FAIL 이면 FAIL, 모두 PASS 면 PASS. 미판정 턴이 있으면 그 단위는 표본에서 뺀다(분모에 넣지 않음).
- 같은 무작위 순서의 앞 n 개를 쓴다 — 5 ⊂ 10 ⊂ 20 … 처럼 같은 사례를 늘려 가며 비교한다.
- n 이 가용 단위보다 크면 '데이터 부족'으로 남기고 외삽하지 않는다.
- 정답 파일이 없으면 일치율은 계산하지 않는다 — 판정끼리의 안정성(통과율 구간 폭·시스템 순서 유지)만 낸다.
  정답 파일(JSONL·CSV): {"system": "gpt", "case_id": "Q1_T1", "criterion": "J5", "gold": "PASS"|"FAIL"|"UNVERIFIABLE"} —
  tools/export_review.py 가 만든 검수 파일에 gold 를 채운 것을 그대로 쓴다 (빈 gold 는 건너뜀).
- 이론 참고값(윌슨 구간·0건 실패 상한·두 시스템 차이 검출 표본 수)은 판정과 별개인 계산값이다. 적정 수는 이것 하나로 정하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval.common_adjudication import resolved_case  # noqa: E402
from code_eval.cost_reporting import estimate  # noqa: E402
from webapp.run_summary import question_units  # noqa: E402

STUDY_VERSION = "sample-size-study-v1"
SIZES = (5, 10, 20, 30, 50, 100)
CRITERIA = ("CODE 1", "CODE 2", "CODE 3", "J1", "J2", "J3", "J4", "J5", "J6", "J7")
GOLD_VALUES = ("PASS", "FAIL", "UNVERIFIABLE")
Z95, Z80 = 1.959964, 0.841621


def case_verdicts(batch: Dict[str, Any]) -> List[Dict[str, str]]:
    """배치(시스템 하나) → 턴마다 {기준: PASS|FAIL|UNJUDGED}. 적용되지 않은 기준은 키가 없다."""
    cases = batch.get("cases") or []
    records = (batch.get("jev_eval") or {}).get("records") or []
    out: List[Dict[str, str]] = []
    for index, case in enumerate(cases):
        row: Dict[str, str] = {}
        record = records[index] if index < len(records) and isinstance(records[index], dict) else {}
        if isinstance(case, dict) and case.get("case_status") == "EVALUATED":
            try:
                case = resolved_case(case, record)
            except (KeyError, TypeError, IndexError):
                pass
            for result in case.get("results") or []:
                if result.get("area") != "common" or not result.get("counted"):
                    continue
                cid = "CODE " + str(result.get("category") or "")
                if cid not in CRITERIA:
                    continue
                verdict = result.get("verdict")
                if verdict == "FAIL" or row.get(cid) == "FAIL":
                    row[cid] = "FAIL"
                elif verdict == "PASS" and row.get(cid) != "UNJUDGED":
                    row[cid] = "PASS"
                else:
                    row[cid] = "UNJUDGED"
        decisions = record.get("decisions") or {}
        criteria = record.get("criteria") if isinstance(record.get("criteria"), list) else list(decisions)
        for cid in criteria:
            decision = (decisions.get(cid) or {}).get("decision")
            row[cid] = decision if decision in ("PASS", "FAIL") else "UNJUDGED"
        out.append(row)
    return out


def unit_table(run: Dict[str, Any]) -> Tuple[List[Tuple[str, List[int]]], Dict[str, List[Dict[str, str]]], Dict[str, List[str]]]:
    """(단위 [(키, [index…])], 시스템별 턴 판정, 시스템별 case_id 목록)."""
    datasets = run.get("datasets") or {}
    structure = datasets.get("aiona") or next((rows for rows in datasets.values() if rows), [])
    total = run.get("question_count") or len(structure)
    units = []
    for indexes in question_units(structure, total):
        first = structure[indexes[0]] if indexes[0] < len(structure) and isinstance(structure[indexes[0]], dict) else {}
        key = f"대화:{first['conversation_id']}" if first.get("conversation_id") not in (None, "") else f"질문:{first.get('case_id') or indexes[0] + 1}"
        units.append((key, indexes))
    verdicts, case_ids = {}, {}
    for system, batch in (run.get("evaluations") or {}).items():
        if isinstance(batch, dict):
            verdicts[system] = case_verdicts(batch)
            case_ids[system] = [str((c or {}).get("case_id") or i + 1) for i, c in enumerate(batch.get("cases") or [])]
    return units, verdicts, case_ids


def unit_verdict(values: Iterable[str]) -> Optional[str]:
    """적용 턴 판정들 → 단위 판정. 하나라도 FAIL → FAIL · 미판정이 섞이면 None(표본 제외) · 모두 PASS → PASS · 적용 없음 → None."""
    values = list(values)
    if "FAIL" in values:
        return "FAIL"
    if not values or "UNJUDGED" in values:
        return None
    return "PASS"


def load_gold(path: Optional[Path]) -> Dict[Tuple[str, str, str], str]:
    """JSONL 또는 CSV(tools/export_review.py 의 검수 파일) — gold 를 비워 둔 행은 아직 검수하지 않은 것으로 건너뛴다."""
    gold: Dict[Tuple[str, str, str], str] = {}
    if path is None:
        return gold
    if path.suffix.lower() == ".csv":
        import csv
        with open(path, encoding="utf-8-sig", newline="") as handle:
            rows = list(enumerate(csv.DictReader(handle), 2))
    else:
        rows = [(n, json.loads(line)) for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1) if line.strip()]
    for number, row in rows:
        value = str(row.get("gold") or "").strip().upper()
        if not value:
            continue
        if value not in GOLD_VALUES or row.get("criterion") not in CRITERIA:
            raise ValueError(f"{path.name}:{number} — gold 는 {'/'.join(GOLD_VALUES)}, criterion 은 {'/'.join(CRITERIA)} 중 하나")
        gold[(str(row["system"]), str(row["case_id"]), row["criterion"])] = value
    return gold


def wilson_half_width(p: float, n: int) -> float:
    """윌슨 95% 구간의 절반 폭 — 관측 비율 p, 표본 n."""
    denom = 1 + Z95 ** 2 / n
    return Z95 * math.sqrt(p * (1 - p) / n + Z95 ** 2 / (4 * n * n)) / denom


def zero_fail_upper(n: int) -> float:
    """n 개 모두 PASS 일 때 실제 실패율의 95% 상한 (정확 이항, 대략 3/n)."""
    return 1 - 0.05 ** (1 / n)


def detect_n(p1: float, diff: float) -> Optional[int]:
    """두 시스템 통과율 차이 diff 를 독립 표본 비교로 검출하는 데 필요한 시스템당 단위 수 (α=.05 양측, 검정력 .8).
    같은 질문을 쓰는 짝지은 비교는 보통 이보다 적게 든다 — 보수적 근사."""
    p2 = p1 - diff
    if not (0 < p2 < 1) or not (0 < p1 <= 1):
        return None
    p1 = min(p1, 0.999)
    pbar = (p1 + p2) / 2
    n = (Z95 * math.sqrt(2 * pbar * (1 - pbar)) + Z80 * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2 / diff ** 2
    return math.ceil(n)


def _percentile(values: List[float], q: float) -> float:
    values = sorted(values)
    k = (len(values) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def judge_costs(run: Dict[str, Any]) -> Dict[str, Any]:
    """저장된 요청별 사용량으로 의미 평가 비용을 답변 하나 기준으로 낸다 (기록이 없는 요청은 미확인으로 센다)."""
    total, priced, unknown = 0.0, 0, 0
    for batch in (run.get("evaluations") or {}).values():
        semantic = (batch or {}).get("jev_eval") or {}
        engine = semantic.get("engine") or (semantic.get("client") or {}).get("engine")
        for record in semantic.get("records") or []:
            parts = ((record or {}).get("verification") or {}).get("usage_requests")
            if not isinstance(parts, list) or not parts:
                unknown += 1
                continue
            try:
                total += float(sum(estimate(engine, part.get("model") or record.get("model"), part.get("usage"),
                                            web_search_calls=part.get("web_search_calls"), service_tier=part.get("service_tier"),
                                            inference_geo=part.get("inference_geo"))[0] for part in parts))
                priced += 1
            except (ValueError, AttributeError):
                unknown += 1
    return {"answers_priced": priced, "answers_unknown": unknown, "usd_total": round(total, 4),
            "usd_per_answer": round(total / priced, 4) if priced else None}


def study(run: Dict[str, Any], gold: Optional[Dict[Tuple[str, str, str], str]] = None, sizes: Iterable[int] = SIZES,
          draws: int = 2000, seed: int = 0) -> Dict[str, Any]:
    gold = gold or {}
    units, verdicts, case_ids = unit_table(run)
    systems = sorted(verdicts)
    costs = judge_costs(run)
    turns_per_unit = {key: len(indexes) for key, indexes in units}
    report: Dict[str, Any] = {"version": STUDY_VERSION, "run_id": run.get("run_id"), "pipeline": (run.get("evaluation_execution") or {}).get("pipeline"),
                              "systems": systems, "units_total": len(units), "sizes": list(sizes), "draws": draws, "seed": seed,
                              "gold_labels": len(gold), "judge_cost": costs, "criteria": {}}
    for cid in CRITERIA:
        per_system: Dict[str, Dict[str, str]] = {}
        gold_units: Dict[str, Dict[str, str]] = {}
        for system in systems:
            rows = verdicts[system]
            per_system[system], gold_units[system] = {}, {}
            for key, indexes in units:
                applied = [rows[i][cid] for i in indexes if i < len(rows) and cid in rows[i]]
                verdict = unit_verdict(applied)
                if verdict is not None:
                    per_system[system][key] = verdict
                labels = [gold[(system, case_ids[system][i], cid)] for i in indexes
                          if i < len(case_ids[system]) and (system, case_ids[system][i], cid) in gold]
                if labels and verdict is not None:
                    gold_units[system][key] = "FAIL" if "FAIL" in labels else "UNVERIFIABLE" if "UNVERIFIABLE" in labels else "PASS"
        common = sorted(set.intersection(*(set(v) for v in per_system.values()))) if systems else []
        full = {s: sum(per_system[s][k] == "PASS" for k in common) / len(common) for s in systems} if common else {}
        pooled_fail = (sum(v == "FAIL" for s in systems for v in per_system[s].values())
                       / max(1, sum(len(per_system[s]) for s in systems)))
        entry: Dict[str, Any] = {
            "applicable_units": {s: len(per_system[s]) for s in systems},
            "applicable_units_singles": {s: sum(k.startswith("질문:") for k in per_system[s]) for s in systems},
            "applicable_units_conversations": {s: sum(k.startswith("대화:") for k in per_system[s]) for s in systems},
            "common_units": len(common), "full_pass_rate": {s: round(r, 4) for s, r in full.items()},
            "fails": {s: sum(v == "FAIL" for v in per_system[s].values()) for s in systems},
            "gold_units": {s: len(gold_units[s]) for s in systems}, "sizes": {}}
        pairs = [(a, b) for i, a in enumerate(systems) for b in systems[i + 1:] if full.get(a) != full.get(b)]
        rng = random.Random(f"{seed}:{cid}")
        orders = [rng.sample(common, len(common)) for _ in range(draws)] if common else []
        for n in sizes:
            if n > len(common):
                entry["sizes"][str(n)] = {"status": "insufficient", "available": len(common)}
                continue
            widths, agree_pairs, has_fail = [], [], []
            for order in orders:
                sample = order[:n]
                rates = {s: sum(per_system[s][k] == "PASS" for k in sample) / n for s in systems}
                widths.append(rates)
                if pairs:
                    agree_pairs.append(sum((rates[a] - rates[b]) * (full[a] - full[b]) > 0 for a, b in pairs) / len(pairs))
                has_fail.append(any(per_system[s][k] == "FAIL" for s in systems for k in sample))
            spread = {s: round(_percentile([w[s] for w in widths], .975) - _percentile([w[s] for w in widths], .025), 4) for s in systems}
            row: Dict[str, Any] = {"status": "ok", "pass_rate_95_width": spread, "max_width": max(spread.values()) if spread else None,
                                   "order_kept": round(sum(agree_pairs) / len(agree_pairs), 4) if agree_pairs else None,
                                   "sample_has_fail": round(sum(has_fail) / len(has_fail), 4),
                                   "theory": {"wilson_half_width": round(wilson_half_width(1 - pooled_fail, n), 4),
                                              "zero_fail_upper_95": round(zero_fail_upper(n), 4)},
                                   # 가용 단위의 절반을 넘으면 같은 단위가 대부분 겹쳐 변동 폭이 작게 나온다 (비복원 추출)
                                   "pool_dominated": n > len(common) / 2,
                                   # CODE 는 로컬 규칙이라 호출 비용이 없다. 의미 평가는 한 번 호출로 그 답변의 J 기준을 함께 판정 → 단위 비용
                                   "judge_cost_usd_per_system": (round(costs["usd_per_answer"] * n * (sum(turns_per_unit[k] for k in common) / len(common)), 2)
                                                                 if costs["usd_per_answer"] and not cid.startswith("CODE") else None)}
            if any(gold_units.values()):
                row["gold"] = _gold_rows(per_system, gold_units, orders, n, systems)
            entry["sizes"][str(n)] = row
        base = 1 - pooled_fail
        entry["detect_units_per_system"] = {f"{d:.2f}": detect_n(base, d) for d in (0.05, 0.10, 0.20)}
        report["criteria"][cid] = entry
    return report


def _gold_rows(per_system, gold_units, orders, n, systems) -> Dict[str, Any]:
    """정답이 붙은 단위만으로 일치율·오류 유형 — 같은 무작위 순서에서 정답 단위 앞 n 개."""
    agreement, false_fail, missed_fail, unverifiable_as_fail, used = [], 0, 0, 0, []
    for order in orders:
        hits = total = 0
        for s in systems:
            labeled = [k for k in order if k in gold_units[s]][:n]
            used.append(len(labeled))
            for k in labeled:
                judge, truth = per_system[s][k], gold_units[s][k]
                total += 1
                hits += judge == truth
                if order is orders[0]:
                    false_fail += judge == "FAIL" and truth == "PASS"
                    missed_fail += judge == "PASS" and truth == "FAIL"
                    unverifiable_as_fail += judge == "FAIL" and truth == "UNVERIFIABLE"
        if total:
            agreement.append(hits / total)
    if not agreement:
        return {"status": "no_overlap"}
    return {"agreement_mean": round(sum(agreement) / len(agreement), 4),
            "agreement_95": [round(_percentile(agreement, .025), 4), round(_percentile(agreement, .975), 4)],
            "labeled_units_per_system_max": max(used) if used else 0,
            "first_draw_errors": {"false_fail": false_fail, "missed_fail": missed_fail, "unverifiable_as_fail": unverifiable_as_fail}}


def render(report: Dict[str, Any]) -> str:
    cost = report["judge_cost"]
    lines = [f"기준별 적정 질문 수 검토 ({report['version']}, 실행 {report['run_id']}, 의미 평가 {report['pipeline']}, API 호출 없음)",
             f"- 시스템 {', '.join(report['systems'])} · 독립 단위 {report['units_total']}개 · 무작위 순서 {report['draws']}회(seed {report['seed']}) · 사람 정답 {report['gold_labels']}건",
             f"- 의미 평가 비용(저장 사용량): 답변 {cost['answers_priced']}건 ${cost['usd_total']} → 답변당 ${cost['usd_per_answer']}"
             + (f" · 사용량 미확인 {cost['answers_unknown']}건" if cost["answers_unknown"] else ""),
             "- 표 읽는 법: 폭 = 같은 시스템 통과율의 95% 변동 폭(작을수록 안정) · 순서 = 전체에서 차이 나던 시스템 쌍의 순서가 유지된 비율 · "
             "0건 상한 = n개 모두 PASS 여도 실제 실패율이 이만큼일 수 있음 · 심판 비용은 의미 기준만(한 번 호출로 그 답변의 J 기준을 함께 판정, CODE 는 무료)"]
    for cid, entry in report["criteria"].items():
        units = entry["applicable_units"]
        lines.append(f"\n[{cid}] 적용 단위(분모) " + ", ".join(f"{s} {units[s]}" for s in report["systems"])
                     + f" · 공통 {entry['common_units']} · FAIL " + ", ".join(f"{s} {entry['fails'][s]}" for s in report["systems"]))
        for n, row in entry["sizes"].items():
            if row["status"] != "ok":
                lines.append(f"    n={n:>3}: 데이터 부족 (공통 적용 단위 {row['available']}개) — 외삽하지 않음")
                continue
            order = "순위 차이 없음" if row["order_kept"] is None else f"순서 {row['order_kept']:.0%}"
            gold = row.get("gold")
            gold_text = (f" · 정답 일치 {gold['agreement_mean']:.0%} [{gold['agreement_95'][0]:.0%}~{gold['agreement_95'][1]:.0%}]"
                         if gold and gold.get("agreement_mean") is not None else "")
            pool = " (가용 단위 절반 초과 — 폭 과소추정)" if row["pool_dominated"] else ""
            lines.append(f"    n={n:>3}: 폭 최대 {row['max_width']:.0%}{pool} · {order} · 표본에 FAIL 포함 {row['sample_has_fail']:.0%} · "
                         f"0건 상한 {row['theory']['zero_fail_upper_95']:.0%}"
                         + (f" · 시스템당 심판 비용 ≈${row['judge_cost_usd_per_system']}" if row["judge_cost_usd_per_system"] is not None else "") + gold_text)
        detect = entry["detect_units_per_system"]
        lines.append("    차이 검출(독립 표본 근사, α .05·검정력 .8): " + ", ".join(f"{float(d):.0%}p → {v if v else '-'}개" for d, v in detect.items()))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--gold", type=Path, help="사람 정답 JSONL (system·case_id·criterion·gold)")
    parser.add_argument("--sizes", default=",".join(map(str, SIZES)), help="비교할 적용 단위 수 (쉼표)")
    parser.add_argument("--draws", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
    if any(n < 1 for n in sizes) or args.draws < 1:
        parser.error("--sizes·--draws 는 1 이상")
    run = json.loads(args.run.read_text(encoding="utf-8-sig"))
    report = study(run, load_gold(args.gold), sizes, args.draws, args.seed)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
