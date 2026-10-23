"""판정 토큰 기록 md — 판정 결과(results.jsonl)에서 질문마다 · 모델마다 · 심판마다 판정 토큰을 표로 (사용자 10-23: "사용된 토큰량 기록해둬" ·
"질문당 평가된 토큰 수도 기록되게" · "md파일로 보여줘").

판정 토큰만 센다(답변 수집 토큰 제외) — 심판이 돌려준 사용량 그대로, 비용은 넣지 않는다. 질문 · 모델마다 마지막 판정 줄을 쓰고,
저장된 출력을 다시 쓴 줄(--repair · --reuse-codex)은 그 판정에서 새로 쓴 토큰만 있어 '다시 씀'으로 표시한다.

    python -X utf8 tools/judge_token_report.py --results data/judge_runs/50ca3ccccace/v4-r3/results.jsonl --set 참고자료/질문세트_78 --out 토큰기록.md
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import judge_v4 as jv  # noqa: E402

MODELS = (("gpt", "GPT"), ("claude", "Claude"), ("gemini", "Gemini"), ("aiona", "AIONA"))
JUDGES = ("Jev", "Decisions", "Codex")
FIELDS = ("입력", "캐시_입력", "출력", "추론_출력", "호출")


def latest_lines(path: Path) -> Dict[Tuple[int, str], Dict[str, Any]]:
    """(질문, 모델) → 마지막 줄 — 판정 줄과 심판 없이 낸 줄(빈 출력 요구 등) 모두."""
    out: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            x = json.loads(line)
            out[(int(x["question"]), str(x["provider"]))] = x
    return out


def tokens_of(line: Dict[str, Any]) -> Dict[str, Any]:
    """판정 줄의 질문당 토큰 — tokens 칸(10-23 부터)이 없으면 calls 에서 다시 계산."""
    if line.get("status") != "judged":
        return {"합계": {f: 0 for f in FIELDS}, "다시_씀": False}
    return line.get("tokens") or jv.question_tokens_used(line.get("calls") or {})


def fmt(n: int) -> str:
    return f"{n:,}"


def build(results: Path, set_dir: Path, title: str = "") -> str:
    lines = latest_lines(results)
    qs = {int(q["id"]): q for q in json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))}
    qtype = {i: (q.get("type") or "단일 질문") for i, q in qs.items()}
    n_q = len(qs)
    by_judge: Dict[str, Counter] = defaultdict(Counter)
    by_model: Dict[str, Counter] = defaultdict(Counter)
    by_type: Dict[str, Counter] = defaultdict(Counter)
    verdicts: Dict[str, Counter] = defaultdict(Counter)
    reused = 0
    per_q: Dict[int, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    seconds: Dict[str, float] = defaultdict(float)
    for (q, p), line in lines.items():
        if q not in qs:
            continue
        verdicts[p][line.get("verdict") or "HOLD"] += 1
        tok = tokens_of(line)
        per_q[q][p] = tok
        reused += bool(tok.get("다시_씀"))
        seconds[p] += float(line.get("seconds") or 0)
        for judge in JUDGES:
            if judge in tok:
                by_judge[judge].update(tok[judge])
        by_model[p].update(tok["합계"])
        by_model[p]["질문"] += 1
        by_type[qtype[q]].update(tok["합계"])
        by_type[qtype[q]]["질문·모델"] += 1
    total = Counter()
    for c in by_model.values():
        total.update({f: c[f] for f in FIELDS})
    out: List[str] = [f"# 판정 토큰 기록{(' — ' + title) if title else ''}", ""]
    out += [f"- 판정 기록: `{results.relative_to(ROOT) if results.is_relative_to(ROOT) else results}` · 질문 세트: `{set_dir.relative_to(ROOT) if set_dir.is_relative_to(ROOT) else set_dir}` ({n_q}문항)",
            "- 판정 토큰만(답변 수집 토큰 제외) · 심판이 돌려준 사용량 그대로 · 비용은 넣지 않음. 캐시 입력은 입력 안에 든 값(입력과 따로 더하지 않음).",
            f"- 질문 · 모델마다 마지막 판정 줄 기준. 저장된 출력을 다시 쓴 줄 {reused}개" + (" — 그 줄은 이번에 새로 쓴 토큰만 들어 있음" if reused else " — 모두 이번 판정에서 새로 부른 토큰"), ""]
    out += ["## 판정 결과 (모델마다 질문 수 / " + str(n_q) + ")", "", "| 모델 | 통과 | FAIL | 보류 | 판정 시간 합 |", "|---|---:|---:|---:|---:|"]
    for p, name in MODELS:
        v = verdicts.get(p, Counter())
        out.append(f"| {name} | {v['PASS']} | {v['FAIL']} | {v['HOLD']} | {seconds[p] / 60:.0f}분 |")
    out += ["", "## 심판별 합계", "", "| 심판 | 호출 | 입력 | 캐시 입력 | 출력 | 추론 출력 |", "|---|---:|---:|---:|---:|---:|"]
    for judge in JUDGES:
        c = by_judge.get(judge)
        if c:
            out.append(f"| {judge} | {fmt(c['호출'])} | {fmt(c['입력'])} | {fmt(c['캐시_입력'])} | {fmt(c['출력'])} | {fmt(c['추론_출력'])} |")
    out.append(f"| **합계** | {fmt(total['호출'])} | {fmt(total['입력'])} | {fmt(total['캐시_입력'])} | {fmt(total['출력'])} | {fmt(total['추론_출력'])} |")
    out += ["", "## 모델별 합계", "", "| 모델 | 질문 | 입력 | 출력 | 질문당 평균 입력 | 질문당 평균 출력 |", "|---|---:|---:|---:|---:|---:|"]
    for p, name in MODELS:
        c = by_model.get(p, Counter())
        k = max(c["질문"], 1)
        out.append(f"| {name} | {c['질문']} | {fmt(c['입력'])} | {fmt(c['출력'])} | {fmt(round(c['입력'] / k))} | {fmt(round(c['출력'] / k))} |")
    out += ["", "## 질문 유형별 합계 (네 모델 합)", "", "| 유형 | 질문 × 모델 | 입력 | 출력 | 질문 × 모델당 평균 입력 |", "|---|---:|---:|---:|---:|"]
    for t, c in sorted(by_type.items(), key=lambda kv: -kv[1]["입력"]):
        out.append(f"| {t} | {c['질문·모델']} | {fmt(c['입력'])} | {fmt(c['출력'])} | {fmt(round(c['입력'] / max(c['질문·모델'], 1)))} |")
    out += ["", "## 질문당 토큰 (입력 / 출력 — 1차 심판 + Codex 합)", "",
            "`*` 저장된 출력을 다시 쓴 줄 · `-` 판정하지 않은 줄(빈 출력 요구 · 수집 실패 등 심판 없이 낸 판정)", "",
            "| 질문 | 유형 | 턴 | " + " | ".join(name for _, name in MODELS) + " | 네 모델 합 (입력) |", "|---:|---|---:|" + "---:|" * len(MODELS) + "---:|"]
    for q in sorted(qs):
        cells, qsum = [], 0
        for p, _ in MODELS:
            tok = per_q[q].get(p)
            line = lines.get((q, p))
            if tok is None or (line or {}).get("status") != "judged":
                cells.append("-")
                continue
            t = tok["합계"]
            qsum += t["입력"]
            cells.append(f"{fmt(t['입력'])} / {fmt(t['출력'])}" + ("*" if tok.get("다시_씀") else ""))
        out.append(f"| {q} | {qtype[q]} | {len(qs[q].get('turns') or [])} | " + " | ".join(cells) + f" | {fmt(qsum)} |")
    return "\n".join(out) + "\n"


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", required=True, help="판정 결과 results.jsonl")
    ap.add_argument("--set", required=True, help="질문 세트 폴더 (questions.json)")
    ap.add_argument("--out", required=True, help="쓸 md 파일")
    ap.add_argument("--title", default="")
    args = ap.parse_args(argv)
    md = build(Path(args.results).resolve(), Path(args.set).resolve(), args.title)
    Path(args.out).write_text(md, encoding="utf-8")
    print(f"저장: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
