"""긴 입력·멀티턴 스트레스 프로필 점검 — 턴별 입력 토큰·대화 누적 토큰을 재고 시험 프로필을 나눈다 (API 호출 없음).

    python tools/context_profile.py 질문세트.json                                 # o200k_base 근사 측정 (답변이 있으면 누적에 포함)
    python tools/context_profile.py --run data/comparison_runs/<id>.json          # 저장 실행: 근사 + 제공자가 집계한 실제 입력 토큰(모델별)
    python tools/context_profile.py 세트.json --model gpt=400000:32000            # 272K 경계 실험: 모델 최대 입력:출력 예약 (공식 문서 확인 값만)

프로필 — 평가 기준의 정의를 바꾸지 않는 시험용 구분이다:
- multi_turn_j6: 2턴 이상 대화 (J6 일반, 길이와 무관)
- stress_multi_turn (stress-10k-100k-v1, 멘토 제안 10-13): 턴 입력 평균 10K 토큰 이상이고 마지막 턴 누적 100K 토큰 이상인 대화.
  멀티턴의 정의가 아니라 따로 세는 스트레스 시험 집계 (--stress-turn·--stress-total 로 바꿀 수 있음)
- long_context_j7: 현재 판정 규칙 — 한 턴 입력(질문+참조, 이전 턴 제외) 8K 토큰 이상 (long_context.MIN_TOKENS)
- boundary_272k (실험): 입력 250K·272K·500K 근방. 최대 입력과 출력 예약을 아는 모델만 '실행 가능'으로, 모르면 '한도 미확인'
상태 — PASS/FAIL 이 아니다: tokenizer_unavailable(측정 불가) · over_model_limit(입력+출력 예약이 최대 입력 초과) · limit_unknown
토큰 수는 tiktoken o200k_base 근사다. 모델마다 토크나이저가 달라 실제 수는 저장 실행의 제공자 사용량(provider_input_tokens)으로 본다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval import long_context  # noqa: E402

PROFILE_VERSION = "context-profile-v1"
STRESS_PROFILE = "stress-10k-100k-v1"
BOUNDARY_POINTS = (250_000, 272_000, 500_000)


def _tokenizer_version() -> Optional[str]:
    try:
        import tiktoken
        return f"{long_context.TOKENIZER} (tiktoken {tiktoken.__version__})"
    except ImportError:
        return None


def provider_input_tokens(row: Dict[str, Any]) -> Optional[int]:
    """수집 응답에 제공자가 집계한 입력 토큰 (대화 턴은 앞 대화 이력이 포함된 실제 요청 크기). 없으면 None."""
    raw = ((row or {}).get("collection") or {}).get("raw") or {}
    usage = raw.get("usage") or {}
    if "promptTokenCount" in (raw.get("usageMetadata") or {}):
        return int(raw["usageMetadata"]["promptTokenCount"])
    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:   # Anthropic: 캐시 토큰은 따로 집계됨
        return int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
    return int(usage["input_tokens"]) if isinstance(usage.get("input_tokens"), int) else None


def _units(rows: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    units: List[List[Dict[str, Any]]] = []
    conversations: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows:
        cid = row.get("conversation_id")
        if cid in (None, ""):
            units.append([row])
        elif cid not in conversations:
            conversations[cid] = [row]
            units.append(conversations[cid])
        else:
            conversations[cid].append(row)
    for unit in units:
        unit.sort(key=lambda r: r.get("turn") or 0)
    return units


def profile(rows: List[Dict[str, Any]], *, stress_turn: int = 10_000, stress_total: int = 100_000,
            models: Optional[Dict[str, Dict[str, int]]] = None) -> Dict[str, Any]:
    """rows: [{question, answer?, reference?, conversation_id?, turn?, provider_tokens?: {model: n}}] → 프로필 보고."""
    models = models or {}
    measured, status = True, "measured"
    try:
        long_context.count_tokens("확인")
    except long_context.TokenizerUnavailable as exc:
        measured, status = False, f"tokenizer_unavailable: {exc}"
    out_units, max_input, provider_max = [], 0, {}
    counts = {"units": 0, "single": 0, "multi_turn_j6": 0, "stress_multi_turn": 0, "long_context_j7_turns": 0, "turns": 0}
    for unit in _units(rows):
        counts["units"] += 1
        counts["turns"] += len(unit)
        is_conversation = unit[0].get("conversation_id") not in (None, "")
        entry: Dict[str, Any] = {"id": unit[0].get("conversation_id") or unit[0].get("case_id"), "turns": len(unit)}
        if is_conversation and len(unit) >= 2:
            counts["multi_turn_j6"] += 1
        elif not is_conversation:
            counts["single"] += 1
        if measured:
            inputs = [long_context.count_tokens(long_context.input_text(r.get("question"), r.get("reference"))) for r in unit]
            cumulative, total = [], 0
            for r, n in zip(unit, inputs):
                total += n
                cumulative.append(total)   # 이번 턴 요청 = 앞 턴 질문·답변 + 이번 질문
                total += long_context.count_tokens(r.get("answer") or "")
            j7 = sum(n >= long_context.MIN_TOKENS for n in inputs)
            counts["long_context_j7_turns"] += j7
            stress = bool(is_conversation and len(unit) >= 2 and sum(inputs) / len(inputs) >= stress_turn and cumulative[-1] >= stress_total)
            counts["stress_multi_turn"] += stress
            max_input = max(max_input, max(cumulative))
            entry.update(turn_input_max=max(inputs), turn_input_mean=round(sum(inputs) / len(inputs)), last_turn_request=cumulative[-1],
                         j7_turns=j7, stress_multi_turn=stress, answers_counted=all(r.get("answer") for r in unit))
        for r in unit:
            for model, n in (r.get("provider_tokens") or {}).items():
                if isinstance(n, int):
                    provider_max[model] = max(provider_max.get(model, 0), n)
        out_units.append(entry)
    boundary = {}
    for point in BOUNDARY_POINTS:
        boundary[str(point)] = {name: ("limit_unknown" if not spec else "over_model_limit" if point + spec["output"] > spec["max_input"]
                                       else "runnable") for name, spec in models.items()} or {"_": "limit_unknown"}
    return {"version": PROFILE_VERSION, "status": status, "tokenizer": _tokenizer_version() if measured else None,
            "stress_profile": {"name": STRESS_PROFILE, "turn_mean_min": stress_turn, "last_turn_request_min": stress_total},
            "j7_rule": {"min_tokens": long_context.MIN_TOKENS, "policy": long_context.LONG_CONTEXT_POLICY_VERSION},
            "counts": counts, "max_request_tokens_estimate": max_input if measured else None,
            "provider_input_tokens_max": provider_max, "boundary_272k": boundary, "units": out_units}


def rows_from_run(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """저장 실행 → AIONA 구조의 질문 행 + 모델별 실제 입력 토큰 (답변은 GPT 수집 답변으로 근사 누적을 낸다)."""
    datasets = run.get("datasets") or {}
    base = datasets.get("aiona") or next(iter(datasets.values()), [])
    rows = []
    for i, row in enumerate(base):
        tokens = {}
        for provider, data in datasets.items():
            if provider != "aiona" and i < len(data):
                n = provider_input_tokens(data[i])
                if n is not None:
                    tokens[(run.get("models") or {}).get(provider) or provider] = n
        rows.append({**{k: row.get(k) for k in ("case_id", "question", "answer", "reference", "conversation_id", "turn")},
                     "provider_tokens": tokens})
    return rows


def _model_arg(value: str) -> Dict[str, Any]:
    name, _, spec = value.partition("=")
    max_input, _, output = spec.partition(":")
    if not name or not max_input.isdigit() or not output.isdigit():
        raise argparse.ArgumentTypeError("--model 이름=최대입력:출력예약 (정수)")
    return {"name": name, "max_input": int(max_input), "output": int(output)}


def render(report: Dict[str, Any]) -> str:
    c, s = report["counts"], report["stress_profile"]
    lines = [f"긴 입력·멀티턴 프로필 ({report['version']}, 토크나이저 {report['tokenizer'] or '없음'}, API 호출 없음)",
             f"- 상태: {report['status']}",
             f"- 독립 단위 {c['units']}개(단일 {c['single']} · 2턴 이상 대화 {c['multi_turn_j6']}) · 턴 {c['turns']}개",
             f"- J6 일반 멀티턴(2턴 이상): {c['multi_turn_j6']}개 대화",
             f"- 스트레스 멀티턴({s['name']}: 턴 평균 ≥ {s['turn_mean_min']:,} · 마지막 턴 요청 ≥ {s['last_turn_request_min']:,}): {c['stress_multi_turn']}개 대화",
             f"- J7 현재 규칙(한 턴 입력 ≥ {report['j7_rule']['min_tokens']:,}): {c['long_context_j7_turns']}개 턴",
             f"- 가장 큰 요청(근사): {report['max_request_tokens_estimate']}"]
    if report["provider_input_tokens_max"]:
        lines.append("- 제공자 집계 실제 입력 토큰 최대: " + ", ".join(f"{m} {n:,}" for m, n in report["provider_input_tokens_max"].items()))
    for point, row in report["boundary_272k"].items():
        lines.append(f"- 경계 실험 {int(point):,} 토큰: " + ", ".join(f"{m} {v}" for m, v in row.items()))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", type=Path, help="질문 세트 (tools/check_question_set.py 와 같은 형식)")
    parser.add_argument("--run", type=Path, help="저장된 비교 실행 — 모델별 실제 입력 토큰 포함")
    parser.add_argument("--model", action="append", type=_model_arg, default=[], help="이름=최대입력:출력예약 (확인된 값만)")
    parser.add_argument("--stress-turn", type=int, default=10_000)
    parser.add_argument("--stress-total", type=int, default=100_000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if bool(args.path) == bool(args.run):
        parser.error("질문 세트 경로 또는 --run 중 하나")
    if args.run:
        rows = rows_from_run(json.loads(args.run.read_text(encoding="utf-8-sig")))
    else:
        from tools.check_question_set import load_items
        rows = [r for r in load_items(args.path) if isinstance(r, dict)]
    report = profile(rows, stress_turn=args.stress_turn, stress_total=args.stress_total,
                     models={m["name"]: m for m in args.model})
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
