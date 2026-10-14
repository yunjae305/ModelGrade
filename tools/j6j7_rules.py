"""J6·J7 규칙 비교 — 같은 저장 실행에 현재·이전·제안 규칙을 함께 적용해 적용 대화·턴 수, 모델 간 차이, 토큰 측정 기준을 비교한다.
API 호출 없음 · 공식 규칙(code_eval.multi_turn·long_context)은 읽기만 하고 바꾸지 않는다.

    python tools/j6j7_rules.py data/comparison_runs/<id>.json [--json]

J6 규칙
- current: 지금 코드 규칙 (multi-turn-v2: 10턴 이상 · 대화 전체 10K 토큰) — 그 모델이 실제로 주고받은 대화(같은 질문 + 그 모델 답변)로 잼
- two_plus: 2턴 이상 대화의 2번째 턴부터 (예전 규칙)
- stress_shared: 스트레스 구간을 '같은 사용자 시나리오'로 잼 — 질문 + 기준 답변(AIONA 업로드 답변, 없으면 질문만). 모든 모델이 같은 판정
민감도: 대화 전체 토큰 경계를 1,000·3,000·5,000 토큰으로 낮춰 같은 방식(자기 답변)으로 재면 모델마다 멀티턴 판정이 갈리는지
J7 토큰 기준 (o200k_base 근사, 제공자 집계는 따로)
- user_turn: 사용자 한 턴 입력(질문 + 참조) — 지금 J7 적용 기준
- conversation: 그 턴 요청 전체(앞 턴 질문 + 그 모델의 앞 답변 + 이번 질문) — 수집 요청과 같은 구성
- provider_collection: 제공자가 집계한 실제 수집 요청 입력 토큰 (시스템 프롬프트 없음)
- judge_request: 평가 모델의 첫 요청 입력 토큰 (평가 지시·기준 설명 포함, 저장 사용량)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval import long_context, multi_turn  # noqa: E402

RULES_VERSION = "j6j7-rules-v2"   # v2: J6 multi-turn-v2(턴 수 + 대화 전체 토큰) — v1 은 긴 턴 개수로 셌다
J7_THRESHOLDS = {"v1 8K": 8_000, "v2 250K (테스트 기간)": 250_000, "현재 272K": None}   # None = 코드의 MIN_TOKENS
SENSITIVITY = (1_000, 3_000, 5_000)   # 대화 전체 토큰 경계


def _conversations(rows: List[Dict[str, Any]]) -> Dict[Any, List[Dict[str, Any]]]:
    out: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get("conversation_id") not in (None, ""):
            out.setdefault(row["conversation_id"], []).append(row)
    for turns in out.values():
        turns.sort(key=lambda r: r.get("turn") or 0)
    return out


def _turn_sizes(turns: List[Dict[str, Any]], answers: Optional[List[str]] = None) -> List[int]:
    return [long_context.count_tokens(f"{t.get('question') or ''}\n{(answers[i] if answers is not None else t.get('answer')) or ''}")
            for i, t in enumerate(turns)]


def j6(run: Dict[str, Any], *, min_turns: Optional[int] = None, total_tokens: Optional[int] = None,
       sensitivity=SENSITIVITY) -> Dict[str, Any]:
    min_turns = min_turns or multi_turn.MIN_TURNS
    total_tokens = total_tokens or multi_turn.MIN_TOTAL_TOKENS
    datasets = run.get("datasets") or {}
    reference = _conversations(datasets.get("aiona") or next(iter(datasets.values()), []))
    systems = {s: _conversations(rows) for s, rows in datasets.items()}
    totals = {s: {cid: sum(_turn_sizes(turns)) for cid, turns in convs.items()} for s, convs in systems.items()}

    def multi(turns: List[Dict[str, Any]], tokens: int, boundary: int = total_tokens) -> bool:
        return len(turns) >= min_turns and tokens >= boundary
    out: Dict[str, Any] = {"policy": multi_turn.MULTI_TURN_POLICY_VERSION, "min_turns": min_turns, "total_tokens": total_tokens,
                           "conversations": len(reference), "rules": {}, "divergent_conversations": [], "sensitivity": {}}
    # 질문 + 기준 답변 (AIONA 업로드 답변 — 모든 모델에 같은 값)
    shared = {cid: multi(turns, sum(_turn_sizes(turns))) for cid, turns in reference.items()}
    for system, conversations in systems.items():
        own = {cid: multi(turns, totals[system][cid]) for cid, turns in conversations.items()}
        count = lambda flags: {"conversations": sum(1 for cid in conversations if flags(cid)),
                               "turns": sum(len(conversations[cid]) - 1 for cid in conversations if flags(cid))}
        out["rules"][system] = {"current": count(lambda cid: own[cid]), "two_plus": count(lambda cid: len(conversations[cid]) >= 2),
                                "stress_shared": count(lambda cid: shared.get(cid, False))}
    for cid in reference:
        decisions = {s: multi(convs[cid], totals[s][cid]) for s, convs in systems.items() if cid in convs}
        if len(set(decisions.values())) > 1:
            out["divergent_conversations"].append({"conversation": cid, "decisions": decisions})
    for threshold in sensitivity:
        row = {system: {str(cid): totals[system][cid] for cid in conversations} for system, conversations in systems.items()}
        chosen = {s: sorted(str(cid) for cid, turns in convs.items() if multi(turns, totals[s][cid], threshold)) for s, convs in systems.items()}
        out["sensitivity"][str(threshold)] = {"tokens_by_system": row, "multi_turn_by_system": chosen,
                                              "diverges": len({tuple(v) for v in chosen.values()}) > 1}
    return out


def _summary(values: List[int]) -> Dict[str, Any]:
    return {"n": len(values), "max": max(values) if values else None, "median": int(statistics.median(values)) if values else None}


def j7(run: Dict[str, Any]) -> Dict[str, Any]:
    thresholds = {name: (value or long_context.MIN_TOKENS) for name, value in J7_THRESHOLDS.items()}
    datasets = run.get("datasets") or {}
    bases: Dict[str, Dict[str, List[int]]] = {"user_turn": {}, "conversation": {}, "provider_collection": {}, "judge_request": {}}
    for system, rows in datasets.items():
        rows = [r for r in rows or [] if isinstance(r, dict)]
        bases["user_turn"][system] = [long_context.count_tokens(long_context.input_text(r.get("question"), r.get("reference"))) for r in rows]
        sizes, history = [], {}
        for r in sorted(rows, key=lambda r: (str(r.get("conversation_id") or r.get("case_id")), r.get("turn") or 0)):
            cid = r.get("conversation_id")
            prior = history.get(cid, "") if cid not in (None, "") else ""
            sizes.append(long_context.count_tokens(prior + (r.get("question") or "")))
            if cid not in (None, ""):
                history[cid] = prior + (r.get("question") or "") + "\n" + (r.get("answer") or "") + "\n"
        bases["conversation"][system] = sizes
        from tools.context_profile import provider_input_tokens
        bases["provider_collection"][system] = [n for r in rows if (n := provider_input_tokens(r)) is not None]
    for system, batch in (run.get("evaluations") or {}).items():
        first = []
        for record in ((batch or {}).get("jev_eval") or {}).get("records") or []:
            parts = ((record or {}).get("verification") or {}).get("usage_requests") or []
            if parts and isinstance(parts[0].get("usage"), dict):
                u = parts[0]["usage"]
                first.append(int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0))
        bases["judge_request"][system] = first
    table = {basis: {system: {**_summary(values), **{name: sum(v >= t for v in values) for name, t in thresholds.items()}}
                     for system, values in by_system.items() if values}
             for basis, by_system in bases.items()}
    return {"policy": long_context.LONG_CONTEXT_POLICY_VERSION, "thresholds": thresholds, "applies_on": "user_turn",
            "attachments_counted": False, "bases": table}


def render(report: Dict[str, Any]) -> str:
    j6r, j7r = report["j6"], report["j7"]
    lines = [f"# J6·J7 규칙 비교 ({RULES_VERSION}, 실행 {report['run_id']}, API 호출 없음 — 공식 규칙은 그대로)", "",
             f"## J6 ({j6r['policy']}: {j6r['min_turns']}턴 이상 · 대화 전체 질문+답변 ≥ {j6r['total_tokens']:,} 토큰) — 대화 {j6r['conversations']}개",
             "| 시스템 | 현재 규칙(자기 답변) | 2턴 이상 | 스트레스 구간(같은 시나리오) |", "|---|---|---|---|"]
    for system, rules in j6r["rules"].items():
        cell = lambda r: f"{r['conversations']} 대화 · {r['turns']} 턴"
        lines.append(f"| {system} | {cell(rules['current'])} | {cell(rules['two_plus'])} | {cell(rules['stress_shared'])} |")
    lines.append(f"\n현재 규칙에서 모델마다 판정이 갈린 대화: {len(j6r['divergent_conversations'])}개")
    for threshold, row in j6r["sensitivity"].items():
        lines.append(f"- 대화 전체 토큰 경계를 {int(threshold):,} 토큰으로 낮추면 멀티턴 대화: "
                     + ", ".join(f"{s} {len(v)}" for s, v in row["multi_turn_by_system"].items()) + (" — 모델마다 갈림" if row["diverges"] else ""))
    lines += ["", f"## J7 ({j7r['policy']}, 적용 기준은 사용자 한 턴 입력 · 첨부 내용은 길이에 들어가지 않음)",
              "| 토큰 기준 | 시스템 | 개수 | 최대 | 중앙값 | " + " | ".join(j7r["thresholds"]) + " |",
              "|---|---|---:|---:|---:|" + "---:|" * len(j7r["thresholds"])]
    for basis, by_system in j7r["bases"].items():
        for system, row in by_system.items():
            lines.append(f"| {basis} | {system} | {row['n']} | {row['max']:,} | {row['median']:,} | "
                         + " | ".join(str(row[name]) for name in j7r["thresholds"]) + " |")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    run = json.loads(args.run.read_text(encoding="utf-8-sig"))
    report = {"version": RULES_VERSION, "run_id": run.get("run_id"), "j6": j6(run), "j7": j7(run)}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
