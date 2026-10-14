"""최소 필요 질문 수 — 평가 항목마다 판정에 충분한 질문이 들어가는 가장 작은 질문 수를 찾는다 (모델 호출 없음).

평가 항목 = CODE 세부 조건(검사 안의 질문 조건 종류, '2-1.count:sentence_count')과 의미 평가 점검 항목 — 질문 생성기가 채우는 단위와 같다. 적용 여부는 ``tools/check_question_set.py`` 와 같은 코드 규칙으로
정한다(평가 때와 같은 엔진·적용 규칙). 질문 단위는 단일 질문 1개 = 1, 멀티턴 대화 1개 = 1.

1. 크기별 적용 수 — 세트를 앞에서부터 N개로 잘랐을 때 항목별 적용 질문 수와, k개 이상 적용된 항목 수.
   질문 생성기는 첫 묶음으로 모든 항목을 덮고 그 뒤 적용 질문이 적은 항목을 우선 노려 추가하므로, 앞에서 자른 세트도 의미가 있다.
2. 최소 부분집합 — 항목마다 적용 질문이 k개 이상(세트에 그만큼 없으면 있는 만큼)이 되도록 고른 가장 작은 질문 수(탐욕 다중 덮개).
   세트에 k개가 없는 항목은 '병목'으로 따로 적고, 세트 안 출현 비율로 k개를 채우는 데 필요한 질문 수를 추정한다.
3. 정밀도 기준 k — 통과율 p 를 95% 신뢰구간 반폭 h 로 재려면 항목마다 n = 1.96²·p(1-p)/h² 개가 필요하다. 이 n 을 k 로 쓴다.
4. (선택) 평가 결과(--run, 저장된 비교 실행 JSON)가 있으면 질문 단위로 다시 뽑아(부트스트랩) 크기별로 항목 통과율의 신뢰구간 반폭과
   모델 순위가 전체 세트와 같을 확률을 잰다 — 순위가 흔들리지 않는 가장 작은 크기가 실측 최소 질문 수다.

    python tools/min_question_count.py 질문세트.json
    python tools/min_question_count.py 질문세트.json --k 1,5,10,30,62,97 --sizes 30,50,100,150,200,300
    python tools/min_question_count.py 질문세트.json --run data/comparison_runs/<run>.json --json
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.check_question_set import ANSWER_DEPENDENT, ANSWER_DEPENDENT_ITEMS, check_items, load_items, unit_of  # noqa: E402

VERSION = "min-question-count-v2"   # v2: CODE 는 검사 단위가 아니라 세부 조건(검사:질문 조건 종류)으로 센다
Z95 = 1.959964
DEFAULT_K = (1, 5, 10, 30)
DEFAULT_SIZES = (30, 50, 100, 150, 200, 300)
#: 정밀도 표 — (예상 통과율 p, 신뢰구간 반폭 h)
PRECISION_GRID = ((0.5, 0.10), (0.8, 0.10), (0.9, 0.10), (0.5, 0.078), (0.8, 0.078), (0.5, 0.05), (0.8, 0.05))


def required_n(p: float, half_width: float) -> int:
    """통과율 p 를 95% 신뢰구간 반폭 ``half_width`` 로 재는 데 필요한 적용 질문 수 (정규 근사)."""
    return math.ceil(Z95 ** 2 * p * (1 - p) / half_width ** 2)


def question_units(report: Dict[str, Any]) -> Tuple[List[str], Dict[str, set], Dict[str, int]]:
    """check_items 보고 → (질문 단위 순서, 단위별 적용 항목 집합, 단위별 턴 수). 답변으로만 정해지는 검사·항목은 뺀다."""
    order: List[str] = []
    targets: Dict[str, set] = defaultdict(set)
    turns: Dict[str, int] = defaultdict(int)
    skip = set(ANSWER_DEPENDENT) | set(ANSWER_DEPENDENT_ITEMS)
    for row in report["cases"]:
        unit = unit_of(row)
        if unit not in targets:
            order.append(unit)
        turns[unit] += 1
        targets[unit].update(t for t in list(row.get("kinds") or []) + list(row.get("items") or []) if t not in skip)
    return order, dict(targets), dict(turns)


def all_targets(report: Dict[str, Any]) -> List[str]:
    """세어야 할 평가 항목 전체 (답변으로만 정해지는 것 제외) — 세트에 한 번도 안 나온 항목도 포함."""
    cov = report["coverage"]
    skip = set(ANSWER_DEPENDENT) | set(ANSWER_DEPENDENT_ITEMS)
    return [t for t in list(cov["kinds"]) + list(cov["items"]) if t not in skip]


def counts_for(units: Iterable[str], targets: Dict[str, set], names: Sequence[str]) -> Dict[str, int]:
    out = {t: 0 for t in names}
    for unit in units:
        for t in targets[unit]:
            if t in out:
                out[t] += 1
    return out


def prefix_table(order: List[str], targets: Dict[str, set], names: Sequence[str], sizes: Sequence[int],
                 ks: Sequence[int]) -> List[Dict[str, Any]]:
    """앞에서부터 N개 — 항목별 적용 수의 최솟값·중앙값과 k개 이상 적용된 항목 수."""
    rows = []
    for size in sorted({min(s, len(order)) for s in sizes if s > 0}):
        counts = counts_for(order[:size], targets, names)
        values = sorted(counts.values())
        rows.append({"questions": size, "min": values[0] if values else 0, "median": values[len(values) // 2] if values else 0,
                     "covered": {str(k): sum(1 for v in values if v >= k) for k in ks}, "targets": len(names),
                     "lowest": sorted(counts.items(), key=lambda kv: (kv[1], kv[0]))[:8]})
    return rows


def greedy_multicover(order: List[str], targets: Dict[str, set], names: Sequence[str], k: int,
                      turns: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """항목마다 min(k, 세트 안 적용 수)개를 채우는 작은 질문 부분집합 (탐욕: 남은 수요를 가장 많이 줄이는 질문, 같으면 턴 수 적은 것·앞 순서)."""
    available = counts_for(order, targets, names)
    demand = {t: min(k, available[t]) for t in names}
    chosen: List[str] = []
    left = set(order)
    position = {u: i for i, u in enumerate(order)}
    while any(v > 0 for v in demand.values()):
        best, best_key = None, None
        for unit in left:
            gain = sum(1 for t in targets[unit] if demand.get(t, 0) > 0)
            if gain == 0:
                continue
            key = (gain, -(turns or {}).get(unit, 1), -position[unit])
            if best_key is None or key > best_key:
                best, best_key = unit, key
        if best is None:
            break
        chosen.append(best)
        left.discard(best)
        for t in targets[best]:
            if demand.get(t, 0) > 0:
                demand[t] -= 1
    short = {t: available[t] for t in names if available[t] < k}
    pool = len(order)
    # 병목 항목 — 세트에서의 출현 비율이 그대로라면 k 개를 채우는 데 필요한 질문 수 (출현 0 이면 추정 불가)
    estimate = {t: (math.ceil(k * pool / n) if n else None) for t, n in short.items()}
    known = [v for v in estimate.values() if v is not None]
    return {"k": k, "questions": len(chosen), "turns": sum((turns or {}).get(u, 1) for u in chosen),
            "complete": not short, "short": dict(sorted(short.items(), key=lambda kv: kv[1])),
            "estimated_questions_for_short": max(known) if known else None,
            "never_applied": sorted(t for t, n in short.items() if n == 0), "chosen": chosen}


# --- 평가 결과가 있을 때: 크기별 부트스트랩 -------------------------------------------------------------

def unit_verdicts(run: Dict[str, Any]) -> Dict[str, Dict[str, Dict[str, str]]]:
    """저장된 비교 실행 → {모델: {질문 단위: {항목: PASS|FAIL}}}. CODE 검사는 결과 행(집계 대상)으로, 의미 평가는 기준 판정으로.

    같은 단위(대화의 여러 턴)에서 한 번이라도 FAIL 이면 FAIL."""
    out: Dict[str, Dict[str, Dict[str, str]]] = {}
    for provider, batch in (run.get("evaluations") or {}).items():
        if not isinstance(batch, dict):
            continue
        records = (batch.get("jev_eval") or {}).get("records") or []
        per_unit: Dict[str, Dict[str, str]] = defaultdict(dict)
        for index, case in enumerate(batch.get("cases") or []):
            if not isinstance(case, dict) or case.get("case_status") != "EVALUATED":
                continue
            conversation = case.get("conversation") or {}
            unit = f"대화:{conversation['id']}" if conversation.get("id") is not None else f"질문#{index + 1}"
            verdicts: Dict[str, str] = {}
            for row in case.get("results") or []:
                if row.get("area") != "common" or not row.get("counted") or row.get("verdict") not in ("PASS", "FAIL"):
                    continue
                check = str(row.get("check_id") or "").split(":")[0]
                if verdicts.get(check) != "FAIL":
                    verdicts[check] = row["verdict"]
            record = records[index] if index < len(records) and isinstance(records[index], dict) else {}
            for cid, decision in (record.get("decisions") or {}).items():
                if isinstance(decision, dict) and decision.get("decision") in ("PASS", "FAIL"):
                    verdicts[cid] = decision["decision"]
            for target, verdict in verdicts.items():
                if per_unit[unit].get(target) != "FAIL":
                    per_unit[unit][target] = verdict
        out[provider] = dict(per_unit)
    return out


def _rates(units: Sequence[str], verdicts: Dict[str, Dict[str, str]]) -> Dict[str, Tuple[int, int]]:
    tally: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    for unit in units:
        for target, verdict in (verdicts.get(unit) or {}).items():
            tally[target][0 if verdict == "PASS" else 1] += 1
    return {t: (p, f) for t, (p, f) in tally.items()}


def _ranking(units: Sequence[str], data: Dict[str, Dict[str, Dict[str, str]]]) -> Tuple[str, ...]:
    """모델 순위 — 모든 항목 판정을 합친 통과율(질문 단위 표본 안에서) 높은 순."""
    scores = []
    for provider, verdicts in data.items():
        passed = failed = 0
        for p, f in _rates(units, verdicts).values():
            passed, failed = passed + p, failed + f
        scores.append((-(passed / (passed + failed)) if passed + failed else 0.0, provider))
    return tuple(provider for _, provider in sorted(scores))


def bootstrap(data: Dict[str, Dict[str, Dict[str, str]]], sizes: Sequence[int], draws: int = 400, seed: int = 7) -> List[Dict[str, Any]]:
    """질문 단위를 크기별로 복원 추출 → 항목 통과율 95% 구간 반폭(모델·항목 중앙값·최댓값)과 전체 순위와 같을 확률."""
    units = sorted({u for verdicts in data.values() for u in verdicts})
    if not units or len(data) < 1:
        return []
    rng = random.Random(seed)
    full = _ranking(units, data)
    rows = []
    for size in sorted({s for s in sizes if s > 0}):
        same = 0
        rates: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        for _ in range(draws):
            sample = [rng.choice(units) for _ in range(size)]
            same += _ranking(sample, data) == full
            for provider, verdicts in data.items():
                for target, (p, f) in _rates(sample, verdicts).items():
                    rates[(provider, target)].append(p / (p + f))
        widths = []
        for values in rates.values():
            if len(values) >= draws // 2:   # 대부분의 표본에 나온 항목만 — 드문 항목은 구간이 정의되지 않는다
                values.sort()
                widths.append((values[int(0.975 * (len(values) - 1))] - values[int(0.025 * (len(values) - 1))]) / 2)
        widths.sort()
        rows.append({"questions": size, "rank_same": round(same / draws, 3), "models": list(full),
                     "half_width_median": round(widths[len(widths) // 2], 3) if widths else None,
                     "half_width_max": round(widths[-1], 3) if widths else None, "measured_targets": len(widths)})
    return rows


def analyze(items: List[Any], ks: Sequence[int] = DEFAULT_K, sizes: Sequence[int] = DEFAULT_SIZES,
            run: Optional[Dict[str, Any]] = None, pipeline: Optional[str] = "items") -> Dict[str, Any]:
    report = check_items(items, pipeline=pipeline)
    order, targets, turns = question_units(report)
    names = all_targets(report)
    available = counts_for(order, targets, names)
    covers = []
    for k in ks:
        result = greedy_multicover(order, targets, names, k, turns)
        result.pop("chosen")
        covers.append(result)
    out = {"version": VERSION, "questions": len(order), "turns": sum(turns.values()), "targets": len(names),
           "answer_dependent": list(ANSWER_DEPENDENT) + list(ANSWER_DEPENDENT_ITEMS),
           "available": dict(sorted(available.items(), key=lambda kv: (kv[1], kv[0]))),
           "prefix": prefix_table(order, targets, names, sizes, ks), "minimum": covers,
           "precision": [{"p": p, "half_width": h, "n": required_n(p, h)} for p, h in PRECISION_GRID]}
    if run is not None:
        out["bootstrap"] = bootstrap(unit_verdicts(run), sizes)
    return out


def render(result: Dict[str, Any]) -> str:
    lines = [f"최소 필요 질문 수 ({result['version']}) — 질문 {result['questions']}개(턴 {result['turns']}개) · 평가 항목 {result['targets']}개",
             "  답변으로만 적용이 정해지는 항목은 세지 않음: " + ", ".join(result["answer_dependent"]), "",
             "[크기별 적용 수 — 앞에서부터 N개]"]
    for row in result["prefix"]:
        covered = " · ".join(f"k≥{k}: {n}/{row['targets']}" for k, n in row["covered"].items())
        lowest = ", ".join(f"{t}={n}" for t, n in row["lowest"][:5])
        lines.append(f"  {row['questions']:>4}개  최소 {row['min']:>3} · 중앙 {row['median']:>3} · {covered}  (가장 적음: {lowest})")
    lines += ["", "[항목마다 k개 이상 적용되는 최소 질문 수 — 이 세트 안에서 고름]"]
    for row in result["minimum"]:
        line = f"  k={row['k']:>3}: {row['questions']:>4}개 (턴 {row['turns']})"
        if row["short"]:
            line += f" · 세트에 k개가 없는 항목 {len(row['short'])}개"
            if row["estimated_questions_for_short"]:
                line += f" → 같은 비율이면 약 {row['estimated_questions_for_short']}개 필요"
            if row["never_applied"]:
                never = row["never_applied"]
                line += f" · 한 번도 적용 안 됨 {len(never)}개: {', '.join(never[:8])}" + (" 외" if len(never) > 8 else "")
        lines.append(line)
    lines += ["", "[정밀도 기준 k — 통과율 p 를 95% 신뢰구간 ±h 로 재는 데 필요한 항목별 적용 질문 수]"]
    lines += [f"  p={row['p']:.1f} · ±{row['half_width'] * 100:.1f}%p → {row['n']}개" for row in result["precision"]]
    if result.get("bootstrap"):
        lines += ["", "[평가 결과 부트스트랩 — 크기별 항목 통과율 구간 반폭과 순위 안정]"]
        for row in result["bootstrap"]:
            lines.append(f"  {row['questions']:>4}개  순위 같음 {row['rank_same'] * 100:5.1f}% · 반폭 중앙 {row['half_width_median']} · 최대 {row['half_width_max']}"
                         f" (항목 {row['measured_targets']}개)")
    return "\n".join(lines)


def _ints(text: str) -> List[int]:
    return [int(x) for x in text.split(",") if x.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path", type=Path, help="질문 세트 (JSON·JSONL·MD — 업로드 화면과 같은 변환)")
    parser.add_argument("--k", default=",".join(map(str, DEFAULT_K)), help="항목별 필요 적용 수 목록 (쉼표)")
    parser.add_argument("--sizes", default=",".join(map(str, DEFAULT_SIZES)), help="앞에서 자를 크기 목록 (쉼표)")
    parser.add_argument("--run", type=Path, help="저장된 비교 실행 JSON — 있으면 부트스트랩")
    parser.add_argument("--json", action="store_true", help="JSON 으로 출력")
    args = parser.parse_args(argv)
    run = json.loads(args.run.read_text(encoding="utf-8")) if args.run else None
    result = analyze(load_items(args.path), _ints(args.k), _ints(args.sizes), run)
    print(json.dumps(result, ensure_ascii=False, indent=1) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
