"""보류·오류 추출 (judge-held-v1) — 판정 결과(data/judge_runs/<실행>/results.jsonl)의 보류 항목과 오류를 CSV 하나로 낸다
(10-19 오후 미팅: 보류·오류 데이터는 파이썬 CLI 로 뽑게 · 체크리스트 t6). AI 호출 없음, 결과 파일은 읽기만.

    python -X utf8 tools/modelgrade.py held --dir data/judge_runs/<실행> [--run <실행>] [--text excerpt|none|full]
    → <폴더>/held.csv — 한 줄 = 보류 항목 하나 또는 오류 하나. gold·reviewer_note 열은 비워 두고 사람이 PASS/FAIL 을 적는다
      (10-19 보류목록 CSV 와 같은 방식 — tools/export_review.py --held 는 옛 판정 형식(data/comparison_runs) 용).

- 보류: 항목 판정이 UNVERIFIED 인 것(심판 갈림 · 인용이 원문에 없음 · 판정 없음 · 선택지 밖 · Codex 가 확정 못 함 · 심판이 확인 못 함),
  질문 전체가 보류인 것(답 복사 실패 · 빈 출력 요구에 복사할 답 없음).
- 오류: 답을 못 받아 FAIL 로 센 질문(no_answer), 심판 호출 실패 — Codex 실패는 그 항목들, Jev 실패는 질문 한 줄(Codex 가 대신했는지 함께).
- 질문 글은 질문 세트에서, 답은 실행 파일(data/comparison_runs/<실행>.json — 없으면 result/<실행>_*.json)에서.
  기본은 앞부분만(질문 120자·답 200자, export_review 와 같은 자르기).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.export_review import _text  # noqa: E402
from webapp.question_generation import flatten  # noqa: E402

RUNS = ROOT / "data" / "comparison_runs"
RESULTS_DIR = ROOT / "result"
DEFAULT_SET = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"
COLUMNS = ("kind", "system", "question", "case_id", "turn", "item", "verdict", "decision", "cause", "cause_label", "judge", "jev",
           "reason", "answer_quote", "problems", "collection_note", "question_text", "answer", "text_scope", "gold", "reviewer_note")
CAUSES = {
    "judge_disagreement": "심판 갈림 — Jev FAIL · Codex PASS",
    "quote_mismatch": "인용이 원문에 없음",
    "missing": "판정 없음 — 심판이 그 항목을 내지 않음",
    "invalid": "선택지 밖 판정",
    "codex_unconfirmed": "Codex 가 확정하지 못함",
    "unverified": "심판이 확인 못 함",
    "copy_failed": "답 복사 실패",
    "empty_output": "빈 출력 요구 — 복사할 답 없음",
    "no_answer": "답 없음 — FAIL 로 셈",
    "call_error": "심판 호출 실패",
    "error_suspect": "에러 케이스 의심 — 사람이 에러 케이스 · PASS · FAIL 을 정함",
    "no_answer_hold": "답 없음 — 보류(수집 로그를 보고 사람이 정함)",
    "empty_reply_hold": "빈 응답 — 보류(수집 로그를 보고 사람이 정함)",
    "code_hold": "코드 판정 애매 — 사람이 PASS · FAIL 과 사유를 정함",
    "copy_failed_turn": "멀티턴 한 턴 복사 실패 — 그 턴만 보류, 다시 수집",
}
ERROR_CAUSES = {"no_answer", "call_error"}
QUESTION_STATUS = {"held_copy_failed": "copy_failed", "held_empty_output": "empty_output", "no_answer": "no_answer",
                   "held_no_answer": "no_answer_hold", "held_error_case": "empty_reply_hold"}


def cause_of(problems: List[str]) -> str:
    """항목 보류 사유(problems 글) → 원인 코드. 사유가 없으면 심판이 스스로 UNVERIFIED 를 낸 것."""
    text = " ".join(problems)
    for needle, cause in (("GPT CLI 실패", "call_error"), ("심판 갈림", "judge_disagreement"), ("원문에 없음", "quote_mismatch"),
                          ("판정 없음", "missing"), ("선택지", "invalid"), ("판정 값", "invalid"), ("확정하지 못함", "codex_unconfirmed")):
        if needle in text:
            return cause
    return "unverified"


def load_texts(set_dir: Path) -> Dict[str, str]:
    """case_id → 질문 글."""
    rows = flatten(json.loads((set_dir / "questions.json").read_text(encoding="utf-8")))
    return {r["case_id"]: r.get("question") or "" for r in rows}


def load_answers(run_id: Optional[str], runs_dir: Optional[Path] = None, results_dir: Optional[Path] = None) -> Dict[str, Dict[str, str]]:
    """모델 → case_id → 답. 실행 파일이 없는 PC 에서는 저장소의 결과 JSON 으로. 둘 다 없으면 빈 사전(답 칸만 비움)."""
    if not run_id:
        return {}
    runs_dir, results_dir = runs_dir or RUNS, results_dir or RESULTS_DIR
    run_path = runs_dir / f"{run_id}.json"
    if run_path.exists():
        run = json.loads(run_path.read_text(encoding="utf-8-sig"))
        return {p: {r.get("case_id"): r.get("answer") or "" for r in rows if isinstance(r, dict)}
                for p, rows in (run.get("datasets") or {}).items()}
    for path in sorted(results_dir.glob(f"{run_id}_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        return {p: {t.get("case_id"): t.get("answer") or "" for t in sec.get("turns") or []}
                for p, sec in (data.get("providers") or {}).items()}
    return {}


def _jev_note(v: Dict[str, Any]) -> str:
    jev = v.get("jev") or {}
    if not jev:
        return ""
    fail = (jev.get("probabilities") or {}).get("FAIL")
    return f"{jev.get('decision')}" + (f" (FAIL 확률 {fail})" if fail is not None else "")


def held_rows(line: Dict[str, Any], texts: Dict[str, str], answers: Dict[str, Dict[str, str]], text: str = "excerpt") -> List[Dict[str, Any]]:
    """결과 한 줄(모델 × 질문) → 보류·오류 줄들."""
    provider, qid = line.get("provider"), int(line.get("question"))
    mine = answers.get(provider) or {}
    base = {"system": provider, "question": qid, "verdict": line.get("verdict"), "text_scope": text, "gold": "", "reviewer_note": ""}

    def texts_of(case_id: str) -> Dict[str, str]:
        return {"question_text": _text(texts.get(case_id, ""), "question", text), "answer": _text(mine.get(case_id, ""), "answer", text)}

    status = line.get("status")
    if status in QUESTION_STATUS:   # 질문 전체가 보류(복사 실패·빈 출력) 또는 답 없음(FAIL) — 심판을 부르지 않은 질문
        cause = QUESTION_STATUS[status]
        first = f"Q{qid}_T1"
        return [{**base, **texts_of(first), "kind": "오류" if cause in ERROR_CAUSES else "보류", "case_id": "", "turn": "", "item": "",
                 "decision": line.get("verdict"), "cause": cause, "cause_label": CAUSES[cause], "reason": line.get("reason") or "",
                 "collection_note": line.get("collection_note") or ""}]
    out = []
    calls = line.get("calls") or {}
    jev_call = calls.get("jev") if isinstance(calls.get("jev"), dict) else None
    if jev_call and jev_call.get("status") != "OK":   # Jev 실패 — 항목 판정은 Codex 가 대신했을 수 있어 질문 한 줄로
        covered = "gpt_fallback" in calls
        out.append({**base, **texts_of(f"Q{qid}_T1"), "kind": "오류", "case_id": "", "turn": "", "item": "J1~J4", "decision": "",
                    "cause": "call_error", "cause_label": CAUSES["call_error"] + (" — Jev (Codex 가 J1~J4 대신)" if covered else " — Jev"),
                    "judge": "jev", "reason": str(jev_call.get("error") or jev_call.get("status")), "collection_note": ""})
    src = line.get("results") or {t: (b or {}).get("items") or {} for t, b in (line.get("turn_results") or {}).items()}   # 판정 v4 · v5(선별) 둘 다
    for turn, items in sorted(src.items(), key=lambda kv: int(kv[0])):
        case_id = f"Q{qid}_T{turn}"
        for item, v in items.items():
            if v.get("decision") != "UNVERIFIED":
                continue
            problems = [str(p) for p in v.get("problems") or []]
            cause = {"에러 케이스 확인": "error_suspect", "코드 판정 확인": "code_hold", "복사 실패": "copy_failed_turn"}.get(v.get("kind")) or cause_of(problems)
            entry = (v.get("entries") or [{}])[0]
            out.append({**base, **texts_of(case_id), "kind": "오류" if cause in ERROR_CAUSES else "보류", "case_id": case_id, "turn": int(turn),
                        "item": item, "decision": "UNVERIFIED", "cause": cause, "cause_label": CAUSES[cause], "judge": v.get("judge") or "",
                        "jev": _jev_note(v), "reason": entry.get("reason") or v.get("reason") or "", "answer_quote": entry.get("answer_quote") or "",
                        "problems": "; ".join(problems), "collection_note": ""})
    return out


def export(judge_dir: Path, run_id: Optional[str] = None, text: str = "excerpt", set_dir: Optional[Path] = None,
           runs_dir: Optional[Path] = None, results_dir: Optional[Path] = None) -> Tuple[Path, List[Dict[str, Any]], Dict[str, Counter]]:
    """judge_dir/results.jsonl → judge_dir/held.csv. (경로, 줄들, 모델별 원인 집계). 폴더를 비우면 기본 위치(질문 세트·실행·결과 JSON)."""
    results = judge_dir / "results.jsonl"
    if not results.exists():
        raise SystemExit(f"판정 결과가 없습니다: {results}")
    lines = [json.loads(ln) for ln in results.read_text(encoding="utf-8").splitlines() if ln.strip()]
    texts = load_texts(set_dir or DEFAULT_SET)
    answers = load_answers(run_id or judge_dir.name, runs_dir, results_dir)
    rows = [{c: r.get(c, "") for c in COLUMNS}   # 줄마다 같은 열 (질문 단위 줄은 항목 열이 빈칸)
            for line in sorted(lines, key=lambda x: (int(x["question"]), str(x["provider"]))) for r in held_rows(line, texts, answers, text)]
    summary: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        summary[r["system"]][r["cause"]] += 1
    path = judge_dir / "held.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:   # 엑셀에서 바로 열리게 BOM
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path, rows, summary


def print_summary(path: Path, rows: List[Dict[str, Any]], summary: Dict[str, Counter]) -> None:
    held = sum(1 for r in rows if r["kind"] == "보류")
    print(f"{path} — 보류 {held}줄 · 오류 {len(rows) - held}줄 (gold 열에 사람이 PASS/FAIL)")
    causes = [c for c in CAUSES if any(s[c] for s in summary.values())]
    if not causes:
        print("보류·오류 없음")
        return
    print("| 모델 | " + " | ".join(CAUSES[c] for c in causes) + " | 합 |")
    print("|---|" + "---|" * (len(causes) + 1))
    for system, counts in summary.items():
        print(f"| {system} | " + " | ".join(str(counts[c]) for c in causes) + f" | {sum(counts.values())} |")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dir", type=Path, help="판정 결과 폴더 (data/judge_runs/<실행>)")
    parser.add_argument("--run", help="실행 번호 — 답을 찾을 때 씀 (기본: 폴더 이름)")
    parser.add_argument("--text", choices=("excerpt", "none", "full"), default="excerpt", help="질문·답 원문 범위 (기본 앞부분)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print_summary(*export(args.dir, args.run, args.text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
