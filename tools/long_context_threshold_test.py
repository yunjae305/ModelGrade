"""롱컨텍스트 경계 테스트 — 몇 K 토큰부터 J7(롱컨텍스트 활용)을 적용할지 정하려고, 길이별 긴 문서에서 정보를 찾는 정답률을 잰다.

사용자 결정(2026-10-13): 후보 경계는 272K 토큰. 250K~500K 를 실제로 시험해 정한다. 이 도구가 문서를 만들고(같은 시드면 늘 같은
문서), 연결된 비교 모델(Gemini·Claude·GPT)에 보내 코드로 채점한다 — 평가 모델(Opus) 호출 없음.

설계 (RULER·Lost in the Middle 방식):
- 길이: 32K(짧은 기준) + 250K·272K·300K·350K·400K·500K 토큰 (tiktoken o200k_base, ``code_eval.long_context`` 와 같은 셈).
- 문서: 한국어 업무 기록 문장을 시드로 만들어 채우고, 그 안에 정답이 정해진 사실을 넣는다.
  1·2·3번 — 문서 앞(10%)·중간(50%)·뒤(90%)의 사실 하나 찾기, 4번 — 두 곳(25%·75%)에 흩어진 사실 잇기(헷갈리게 하는 비슷한
  사실 여러 개 포함).
- 질문 4개를 문서 뒤에 한 번에 묻고 '1) … 4) …' 형식으로 답하게 한다. 정답 코드가 답의 해당 번호 줄에 있으면 정답.
- 판단: 32K 대비 정답률이 떨어지기 시작하는 길이 — J7 경계 후보.

    python tools/long_context_threshold_test.py --dry-run          # 문서만 만들고 길이 확인 (API 호출 없음)
    python tools/long_context_threshold_test.py --providers gemini claude gpt   # 실제 호출 (유료, 길이 7개 × 모델 수)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from code_eval import long_context  # noqa: E402
from code_eval.llm_client import env_value  # noqa: E402

LENGTHS = (32_000, 250_000, 272_000, 300_000, 350_000, 400_000, 500_000)
POSITIONS = {1: 0.10, 2: 0.50, 3: 0.90}
HOP_POSITIONS = (0.25, 0.75)
TEST_VERSION = "long-context-threshold-test-v1"

_DEPTS = ["재무팀", "인사팀", "물류팀", "고객지원팀", "품질관리팀", "구매팀", "법무팀", "연구개발팀", "영업1팀", "영업2팀", "시설관리팀", "교육팀"]
_TOPICS = ["분기 예산 집행 현황", "신규 채용 일정", "창고 재고 정리", "고객 문의 처리 시간", "협력사 납품 일정", "사내 교육 과정 개편",
           "보안 점검 결과", "회의실 예약 규칙", "출장비 정산 절차", "장비 교체 계획", "업무 매뉴얼 개정", "연말 행사 준비"]
_ACTIONS = ["다음 회의까지 초안을 정리하기로 했다", "담당자를 정해 진행 상황을 매주 공유하기로 했다", "추가 자료를 받은 뒤 다시 논의하기로 했다",
            "기존 방식을 유지하되 일부 절차를 줄이기로 했다", "관련 부서와 협의해 일정을 다시 잡기로 했다", "시범 운영 결과를 보고 확대 여부를 정하기로 했다"]
_NOTES = ["참석자 대부분이 일정이 빠듯하다는 의견을 냈다", "지난달보다 처리 건수가 조금 늘었다", "외부 업체 견적을 두 곳에서 더 받기로 했다",
          "현장 직원의 의견을 설문으로 모으기로 했다", "비용 절감 효과는 아직 크지 않은 것으로 보인다", "문서 양식을 하나로 통일하자는 제안이 나왔다"]
_NAMES = ["김도윤", "이서연", "박지호", "최하은", "정민준", "강지우", "조은서", "윤태양", "장서윤", "임하준", "한지민", "오세린"]


def _filler_sentence(rng: random.Random) -> str:
    return (f"{rng.randint(1, 12)}월 {rng.randint(1, 28)}일 {rng.choice(_DEPTS)} 회의에서는 {rng.choice(_TOPICS)}을(를) 다뤘다. "
            f"{rng.choice(_NAMES)} 담당이 현황을 설명했고, {rng.choice(_NOTES)}. 결론적으로 {rng.choice(_ACTIONS)}.")


def _code(rng: random.Random, prefix: str) -> str:
    return f"{prefix}-{rng.randint(1000, 9999)}"


def build_case(target_tokens: int, seed: int = 20261013) -> Dict[str, Any]:
    """길이 target_tokens 근처의 문서·질문·정답. 같은 (길이, 시드)면 늘 같은 내용."""
    rng = random.Random(f"{seed}:{target_tokens}")
    answers = {1: _code(rng, "KX"), 2: _code(rng, "CR"), 3: _code(rng, "HD"), 4: _code(rng, "BG")}
    hop_name, hop_branch = "윤서진", "북극성"
    distractors = [(n, b, _code(rng, "BG")) for n, b in (("한도경", "남십자성"), ("서지안", "오리온"), ("문하율", "카시오페아"))]
    needles = {
        1: f"[특별 기록] 프로젝트 '은하수-7'의 최종 승인 코드는 {answers[1]}로 확정되었다.",
        2: f"[특별 기록] 물류센터 '청람'의 비상 대응 코드는 {answers[2]}이다.",
        3: f"[특별 기록] '해오름 계약'의 해지 확인 코드는 {answers[3]}로 정했다.",
    }
    hop_a = (f"[인사 기록] {hop_name} 책임자는 '{hop_branch}' 지점 소속이다. "
             + " ".join(f"{n} 책임자는 '{b}' 지점 소속이다." for n, b, _ in distractors))
    hop_b = (f"[예산 기록] {hop_name} 책임자가 관리하는 지점의 예산 코드는 {answers[4]}이다. "
             + " ".join(f"{n} 책임자가 관리하는 지점의 예산 코드는 {c}이다." for n, _, c in distractors))
    questions = ("\n\n위 기록만 보고 다음 네 질문에 답하세요. 각 답은 해당 코드만 '1) 코드' 형식으로 한 줄씩 쓰세요.\n"
                 "1) 프로젝트 '은하수-7'의 최종 승인 코드는?\n2) 물류센터 '청람'의 비상 대응 코드는?\n"
                 "3) '해오름 계약'의 해지 확인 코드는?\n4) '북극성' 지점의 예산 코드는?")
    inserts = sorted([(POSITIONS[q], needles[q]) for q in (1, 2, 3)] + [(HOP_POSITIONS[0], hop_a), (HOP_POSITIONS[1], hop_b)])
    fixed = long_context.count_tokens("\n".join(text for _, text in inserts) + questions)
    budget = target_tokens - fixed
    sentences, tokens = [], 0
    while tokens < budget:   # 문장마다 세어 목표 길이에 맞춘다 (합은 이어 붙인 뒤와 거의 같다)
        sentence = _filler_sentence(rng)
        tokens += long_context.count_tokens(sentence + "\n")
        sentences.append(sentence)
    body: List[str] = []
    cursor = 0
    for position, text in inserts:
        cut = int(len(sentences) * position)
        body.extend(sentences[cursor:cut])
        body.append(text)
        cursor = cut
    body.extend(sentences[cursor:])
    prompt = "다음은 회사 업무 기록 모음입니다.\n\n" + "\n".join(body) + questions
    return {"target_tokens": target_tokens, "seed": seed, "prompt": prompt, "answers": answers,
            "tokens": long_context.count_tokens(prompt), "chars": len(prompt),
            "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}


_LINE = re.compile(r"^\s*\**\s*([1-4])\s*[\)\.:번]\s*(.*)$")


def grade(answer: str, answers: Dict[int, str]) -> Dict[int, bool]:
    """번호 줄마다 정답 코드가 있는가. 번호 줄을 못 찾으면 답 전체에서 찾는다 (코드는 문항마다 달라 섞이지 않음)."""
    lines: Dict[int, str] = {}
    for line in (answer or "").splitlines():
        match = _LINE.match(line)
        if match:
            lines.setdefault(int(match.group(1)), match.group(2))
    norm = lambda text: re.sub(r"\s+", "", text or "").upper()
    return {q: norm(code) in norm(lines.get(q, answer if not lines else "")) for q, code in answers.items()}


def _configs() -> Dict[str, Dict[str, str]]:
    from webapp.comparison_service import PROVIDER_ENV
    configs = {}
    for provider, (key_var, model_var) in PROVIDER_ENV.items():
        key, model = env_value(key_var, None, ROOT / ".env"), env_value(model_var, None, ROOT / ".env")
        if key and model:
            configs[provider] = {"key": key, "model": model}
    return configs


def _usage(provider: str, raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    if provider == "gemini":
        meta = raw.get("usageMetadata") or {}
        return {"input_tokens": meta.get("promptTokenCount"), "output_tokens": meta.get("candidatesTokenCount")}
    usage = raw.get("usage") or {}
    return {"input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
            "output_tokens": usage.get("output_tokens", usage.get("completion_tokens"))}


def run(providers: List[str], out: Path, max_tokens: int = 1024) -> Dict[str, Any]:
    import httpx
    from webapp.comparison_service import ProviderError, request_answer
    configs = _configs()
    missing = [p for p in providers if p not in configs]
    if missing:
        raise SystemExit("연결 설정(.env 키·모델)이 없는 모델: " + ", ".join(missing))
    cases = [build_case(length) for length in LENGTHS]
    results: List[Dict[str, Any]] = []

    def ask(provider: str) -> List[Dict[str, Any]]:
        rows = []
        with httpx.Client(timeout=900, follow_redirects=False) as client:
            for case in cases:   # 모델마다 짧은 길이부터 차례로
                started = time.monotonic()
                row = {"provider": provider, "model": configs[provider]["model"], "target_tokens": case["target_tokens"],
                       "tokens": case["tokens"], "sha256": case["sha256"]}
                try:
                    outcome = request_answer(provider, configs[provider], [{"role": "user", "content": case["prompt"]}],
                                             max_tokens, client)
                    row.update(status=outcome["status"], answer=outcome["answer"], finish=outcome.get("finish_reason"),
                               usage=_usage(provider, outcome.get("raw")), error=outcome.get("error"))
                except ProviderError as exc:
                    row.update(status="ERROR", answer="", error=str(exc), http_status=exc.status, usage={})
                row["latency_s"] = round(time.monotonic() - started, 1)
                row["correct"] = {str(q): ok for q, ok in grade(row["answer"], case["answers"]).items()}
                row["score"] = sum(row["correct"].values())
                rows.append(row)
                print(f"{provider} {case['target_tokens']:>7,} → {row['status']} {row['score']}/4 ({row['latency_s']}s)", flush=True)
        return rows

    with ThreadPoolExecutor(max_workers=len(providers)) as pool:   # 모델끼리는 동시에, 한 모델 안에서는 차례로
        for rows in pool.map(ask, providers):
            results.extend(rows)
    report = {"version": TEST_VERSION, "created_at": datetime.now(timezone.utc).isoformat(), "tokenizer": long_context.TOKENIZER,
              "lengths": list(LENGTHS), "positions": {"1": "앞 10%", "2": "중간 50%", "3": "뒤 90%", "4": "두 곳(25%·75%) 연결"},
              "cases": [{k: v for k, v in c.items() if k != "prompt"} for c in cases], "results": results}
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "summary.md").write_text(summary(report), encoding="utf-8")
    return report


def summary(report: Dict[str, Any]) -> str:
    providers = sorted({r["provider"] for r in report["results"]})
    lines = [f"# 롱컨텍스트 경계 테스트 ({report['version']})", "",
             "정답 수(4문항 중) — 1 앞 10% · 2 중간 50% · 3 뒤 90% · 4 두 곳 연결. ✓ 정답, ✗ 오답, ERR 호출 실패", "",
             "| 길이(목표) | 실제 토큰 | " + " | ".join(providers) + " |", "|---|---|" + "---|" * len(providers)]
    for length in report["lengths"]:
        rows = {r["provider"]: r for r in report["results"] if r["target_tokens"] == length}
        tokens = next((r["tokens"] for r in rows.values()), None)
        cells = []
        for p in providers:
            r = rows.get(p)
            if not r:
                cells.append("—")
            elif r["status"] == "ERROR":
                cells.append(f"ERR ({r.get('http_status') or ''})")
            else:
                marks = "".join("✓" if r["correct"][q] else "✗" for q in ("1", "2", "3", "4"))
                cells.append(f"{r['score']}/4 {marks}")
        lines.append(f"| {length // 1000}K | {tokens:,} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--providers", nargs="+", default=["gemini", "claude", "gpt"])
    parser.add_argument("--dry-run", action="store_true", help="문서만 만들고 길이를 확인 (API 호출 없음)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.dry_run:
        for length in LENGTHS:
            case = build_case(length)
            print(f"{length:>7,} 목표 → {case['tokens']:>7,} 토큰 · {case['chars']:>9,} 자 · 정답 {case['answers']}")
        return 0
    out = args.out or ROOT / "results" / ("long-context-threshold-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    report = run(args.providers, out)
    print(summary(report))
    print(f"저장: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
