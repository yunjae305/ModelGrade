"""롱컨텍스트 추가 탐침 — 32K~500K 경계 시험(tools/long_context_threshold_test.py)이 다루지 않은 세 가지를 같은 방식으로 만든다.
API 호출 없음 — 문서를 만들고(같은 시드면 늘 같은 문서) 정답·채점·예상 비용만 낸다. 실제 호출은 승인 뒤 기존 도구의 실행 경로로.

    python tools/long_context_probes.py --dry-run                       # 길이별 문서 토큰 수·예상 비용 (호출 없음)

탐침 (문서 = 시드로 만든 한국어 업무 기록, 팀원 도구의 문장·코드 생성과 채점(번호 줄 정확 일치)을 그대로 쓴다):
1) 개정 반영 — 앞 15% 에 옛 규정 코드, 뒤 70% 에 '개정 — 이전 코드 폐기, 새 코드' → 현재 유효한 코드 (옛 코드는 오답)
2) 상충 정보 판별 — 30% 의 3월 보고 값과 80% 의 9월 정정 보고('3월 값은 집계 오류') → 정정된 값 (3월 값은 오답)
3) 긴 거리 지시 유지 — 문서 앞 사용자 요청에 '3번에 답할 때만 코드 앞에 OK- 를 붙인다', 사실은 문서 중간 50% → OK-코드
문서 안의 지시문은 넣지 않는다 — 문서 속 지시를 따르게 하는 것은 프롬프트 주입과 구분되지 않아, 지시는 사용자 요청에만 둔다.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.long_context_threshold_test import _assemble, _code, grade  # noqa: E402  (팀원 도구의 조립·채점 재사용)

PROBE_VERSION = "long-context-probes-v1"
LENGTHS = (32_000, 128_000, 272_000, 500_000)
#: 제공자 토큰 / o200k (한국어) — Claude 는 #21 실측 약 1.75배, 3차 실행 e3c60d3efd18 1.59~1.79배 · GPT·Gemini 약 1.05배
TOKEN_FACTOR = {"claude": 1.75, "gpt": 1.05, "gemini": 1.05}
#: (입력 $/M, 출력 $/M) — code_eval/cost_reporting.py 의 수집 모델 단가. GPT 는 272K 초과 입력이면 입력 2배·출력 1.5배
PRICES = {"claude": (2.0, 10.0), "gpt": (0.10, 0.50), "gemini": (0.75, 3.75)}
OUTPUT_TOKENS = 1024   # 경계 시험과 같은 출력 상한


def build_probe_case(target_tokens: int, seed: int = 20261014) -> Dict[str, Any]:
    rng = random.Random(f"probe:{seed}:{target_tokens}")
    used: set = set()
    old, new = _code(rng, "RG", used), _code(rng, "RG", used)
    march, september = _code(rng, "ST", used), _code(rng, "ST", used)
    contact = _code(rng, "EM", used)
    inserts = [
        (0.15, f"[규정 R-12] 출장 정산 승인 코드는 {old} 이다."),
        (0.30, f"[3월 재고 보고] 품목 A-31 의 재고 확인 코드는 {march} 로 보고되었다."),
        (0.50, f"[연락망] 물류팀 비상 연락 코드는 {contact} 이다."),
        (0.70, f"[공지] 규정 R-12 개정 — 이전 승인 코드는 폐기하고, 앞으로는 {new} 를 쓴다."),
        (0.80, f"[9월 정정 보고] 3월 보고의 A-31 재고 확인 코드는 집계 오류였다. 정정된 코드는 {september} 이다."),
    ]
    questions = ("\n\n질문 — 위 기록만 보고 번호마다 한 줄로 답하세요 (예: '1) XX-0000').\n"
                 "1) 규정 R-12 의 현재 유효한 출장 정산 승인 코드는?\n"
                 "2) 품목 A-31 의 정정된 재고 확인 코드는?\n"
                 "3) 물류팀 비상 연락 코드는?\n")
    case = _assemble(rng, target_tokens, seed, inserts, questions, {1: new, 2: september, 3: f"OK-{contact}"})
    preamble = "[요청] 마지막 질문 목록의 3번에 답할 때만 코드 앞에 'OK-' 를 붙여 주세요. 다른 번호에는 붙이지 마세요.\n\n"
    case["prompt"] = preamble + case["prompt"]
    case.update(version=PROBE_VERSION, distractors={1: old, 2: march, 3: contact},
                kinds={}, labels={1: "개정 반영", 2: "상충 정보 판별", 3: "긴 거리 지시 유지"})
    return case


def grade_probe(answer: str, case: Dict[str, Any]) -> Dict[str, Any]:
    """번호 줄마다 정답 하나뿐이면 정답 (팀원 grade). 오답이 정확히 옛 값·3월 값·접두 없는 코드면 그 실패 유형을 남긴다."""
    correct = grade(answer, case["answers"])
    misses = {}
    from tools.long_context_threshold_test import _CODE_TOKEN, _LINE
    lines: Dict[int, List[str]] = {}
    for line in (answer or "").splitlines():
        m = _LINE.match(line)
        if m:
            lines.setdefault(int(m.group(1)), []).append(m.group(2))
    for q, ok in correct.items():
        if ok:
            continue
        found = {t.replace(" ", "").rstrip("-") for t in _CODE_TOKEN.findall(" ".join(lines.get(q, [])).upper())}
        misses[q] = ("옛 값 · 상충 값을 고름" if found == {case["distractors"][q]} and q in (1, 2)
                     else "지시(접두) 빠짐" if q == 3 and found == {case["distractors"][q]} else "그 밖")
    return {"correct": {str(q): ok for q, ok in correct.items()}, "score": sum(correct.values()), "misses": misses}


def estimate(lengths=LENGTHS) -> Dict[str, Any]:
    rows, total = [], 0.0
    for length in lengths:
        for model, factor in TOKEN_FACTOR.items():
            inp = length * factor
            price_in, price_out = PRICES[model]
            if model == "gpt" and inp > 272_000:
                price_in, price_out = price_in * 2, price_out * 1.5
            usd = inp * price_in / 1e6 + OUTPUT_TOKENS * price_out / 1e6
            total += usd
            rows.append({"length": length, "model": model, "provider_input_estimate": int(inp), "usd": round(usd, 4)})
    return {"rows": rows, "usd_total": round(total, 3), "calls": len(rows),
            "note": "추정 — 제공자 실제 토큰 수로 다시 확인. 평가 모델(Opus) 호출 없음(코드 채점)"}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", required=True, help="문서만 만들고 길이·예상 비용 (호출 없음)")
    parser.add_argument("--lengths", default=",".join(map(str, LENGTHS)))
    args = parser.parse_args(argv)
    lengths = [int(x) for x in args.lengths.split(",") if x.strip()]
    cases = [{k: v for k, v in build_probe_case(n).items() if k in ("target_tokens", "tokens", "chars", "sha256")} for n in lengths]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"version": PROBE_VERSION, "cases": cases, "estimate": estimate(lengths)}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
