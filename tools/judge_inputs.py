"""판정 입력 만들기 (judge-input-v1) — 고정 판정 프롬프트 v3 하나에 붙일 [입력]을 질문마다 만든다 (AI 호출 없음).

프롬프트는 ``참고자료/프롬프트/judge_prompt_v3.txt`` 한 파일로 고정이고(사용자 결정 2026-10-19 "프롬프트 수는 1개로 고정"), 이 도구는
프롬프트를 만들거나 고치지 않는다. 질문 1개(단일 질문 = 1턴, 대화 = 모든 턴)마다 입력 글 하나를 만든다 — 판정은 질문 단위(모델당 326번).

- 턴마다 [이 턴의 상황](판정상황표_v2 의 글 그대로) · [질문] · [첨부파일] · [참조 자료] · [답변](같은 모델의 그 턴 답변).
- 어느 모델의 답변인지는 입력 글에 넣지 않는다(파일 이름·줄 정보에만).
- 보내지 않는 질문(프롬프트 상황표 3): 빈 출력 요구 질문(보류 — empty-output-hold-v2), 답을 받지 못한 턴이 있는 질문(FAIL). 줄에 상태만 남긴다.

    python -X utf8 tools/judge_inputs.py data/comparison_runs/8c61f117e661.json [--providers aiona,gpt] [--only 36,11] [--out DIR]
    → <out>/<모델>.jsonl (질문 한 줄: question·status·turns·items_by_turn·input·sha256) · <out>/manifest.json (버전·sha256·표식·개수)
    기본 출력은 data/judge_inputs/<run_id>/ — 답변 원문이 들어 있어 git 에 올리지 않는다(data/ 는 .gitignore).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.question_generation import flatten  # noqa: E402
from webapp.run_summary import asks_empty_output  # noqa: E402

VERSION = "judge-input-v1"
PROMPT = ROOT / "참고자료" / "프롬프트" / "judge_prompt_v3.txt"
PROMPT_VERSION = "judge-prompt-v3"
DEFAULT_SET = ROOT / "참고자료" / "질문세트_326"
SITUATIONS = "판정상황표_v2.json"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _turn(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("turn") or 1)
    except (TypeError, ValueError):
        return 1


def _question(case_id: str) -> int:
    return int(case_id.split("_T")[0][1:])


def collected(row: Optional[Dict[str, Any]]) -> bool:
    """답을 받았는가 — 빈 행·명시적 수집 실패만 '답 없음'(직접 올린 원문은 collection 이 없어도 받은 답). question_gradient 와 같은 규칙."""
    return isinstance(row, dict) and (row.get("collection") or {}).get("status", "SUCCESS") == "SUCCESS"


def turn_block(mark: str, row: Dict[str, Any], situation: str, answer: str) -> str:
    parts = [f"<<{mark} {_turn(row)}턴 시작>>", "[이 턴의 상황]", situation, "[질문]", row.get("question") or ""]
    files = [a for a in row.get("attachments") or [] if isinstance(a, dict)]
    if files:
        parts += ["[첨부파일]", "\n".join(f"- {a.get('name')} · {a.get('type')}\n{a.get('content') or '(내용 없음)'}" for a in files)]
    if isinstance(row.get("reference"), str) and row["reference"].strip():
        parts += ["[참조 자료]", row["reference"]]
    parts += ["[답변]", answer, f"<<{mark} {_turn(row)}턴 끝>>"]
    return "\n".join(parts)


def question_input(mark: str, turns: List[Dict[str, Any]], situations: Dict[str, str], answers: Dict[str, str]) -> str:
    """고정 프롬프트 뒤에 붙일 [입력] — 프롬프트 '입력 형식' 그대로."""
    body = [turn_block(mark, row, situations[row["case_id"]], answers[row["case_id"]]) for row in turns]
    return "\n".join([f"실행 표식: {mark}", f"<<{mark} 질문 시작>>", *body, f"<<{mark} 질문 끝>>"])


def run_mark(texts: Iterable[str]) -> str:
    joined = "\n".join(texts)
    for _ in range(100):
        mark = secrets.token_hex(3)
        if mark not in joined:
            return mark
    raise SystemExit("질문·답변에 없는 실행 표식을 만들지 못했습니다.")


def build(run: Dict[str, Any], set_dir: Path, out: Path, providers: Optional[List[str]] = None,
          only: Optional[set] = None, mark: Optional[str] = None) -> Dict[str, Any]:
    prompt = PROMPT.read_text(encoding="utf-8")
    situations_path = set_dir / SITUATIONS
    table = {r["case_id"]: r for r in json.loads(situations_path.read_text(encoding="utf-8"))["rows"]}
    rows = flatten(json.loads((set_dir / "questions.json").read_text(encoding="utf-8")))
    questions: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        questions[_question(row["case_id"])].append(row)
    providers = providers or list(run.get("providers") or (run.get("datasets") or {}))
    datasets = {p: {r.get("case_id"): r for r in (run.get("datasets") or {}).get(p) or [] if isinstance(r, dict)} for p in providers}
    answers = {p: {cid: r.get("answer") or "" for cid, r in rows_.items() if collected(r)} for p, rows_ in datasets.items()}
    texts = [r.get("question") or "" for r in rows] + [a for p in answers.values() for a in p.values()]
    mark = mark or run_mark(texts)
    if any(mark in t for t in texts):
        raise SystemExit(f"실행 표식 {mark} 이 질문·답변에 들어 있습니다 — 다른 표식으로 다시 실행하세요.")
    out.mkdir(parents=True, exist_ok=True)
    counts: Counter = Counter()
    for provider in providers:
        lines = []
        for qid in sorted(questions):
            if only and qid not in only:
                continue
            turns = sorted(questions[qid], key=_turn)
            entry: Dict[str, Any] = {"question": qid, "provider": provider, "turns": len(turns),
                                     "items_by_turn": {str(_turn(t)): table[t["case_id"]]["items"] for t in turns}}
            missing = [t["case_id"] for t in turns if t["case_id"] not in answers[provider]]
            if any(asks_empty_output(None, t.get("question")) for t in turns):
                entry.update(status="held_empty_output", reason="빈 출력 요구 질문 — 보류(empty-output-hold-v2), 보내지 않음")
            elif missing:
                entry.update(status="no_answer", reason=f"답을 받지 못한 턴 {', '.join(missing)} — FAIL 로 셈, 보내지 않음")
            else:
                text = question_input(mark, turns, {t["case_id"]: table[t["case_id"]]["case_situations"] for t in turns}, answers[provider])
                entry.update(status="ready", input=text, chars=len(text), sha256=_sha(text))
            counts[(provider, entry["status"])] += 1
            lines.append(json.dumps(entry, ensure_ascii=False))
        (out / f"{provider}.jsonl").write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    manifest = {"version": VERSION, "prompt": {"version": PROMPT_VERSION, "file": PROMPT.name, "sha256": _sha(prompt)},
                "situations": {"file": SITUATIONS, "sha256": hashlib.sha256(situations_path.read_bytes()).hexdigest()[:16]},
                "set": set_dir.name, "run_id": run.get("run_id"), "run_mark": mark, "unit": "question",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "counts": {p: {s: n for (pp, s), n in sorted(counts.items()) if pp == p} for p in providers}}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path, help="수집 결과 실행 JSON (data/comparison_runs/<id>.json)")
    parser.add_argument("--set", type=Path, default=DEFAULT_SET, help="질문 세트 폴더 (questions.json·판정상황표_v2.json)")
    parser.add_argument("--providers", help="쉼표로 — 기본은 실행의 모든 모델")
    parser.add_argument("--only", help="쉼표로 질문 번호 — 기본은 전체 질문")
    parser.add_argument("--out", type=Path, help="기본 data/judge_inputs/<run_id>")
    args = parser.parse_args(argv)
    run = json.loads(args.run.read_text(encoding="utf-8-sig"))
    out = args.out or ROOT / "data" / "judge_inputs" / str(run.get("run_id") or args.run.stem)
    manifest = build(run, args.set, out, args.providers.split(",") if args.providers else None,
                     {int(x) for x in args.only.split(",")} if args.only else None)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({**manifest, "out": str(out)}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
