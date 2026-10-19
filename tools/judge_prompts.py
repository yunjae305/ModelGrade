"""판정 프롬프트 채우기 (judge-prompt-fill-v1) — 고정 판정 프롬프트 v2 의 슬롯에 상황·질문·답변을 넣어 턴 × 모델마다 파일로 만든다 (AI 호출 없음).

2026-10-19 오후 미팅 결정: 판정 프롬프트는 고정하고, 질문마다는 데이터로 인덱싱한 것만 갈아 끼운다. 이 도구는 글을 새로 쓰지 않는다 —
고정 본문(``참고자료/프롬프트/judge_prompt_v2.txt``)의 슬롯 네 개만 채운다.

- ``{{CASE_SITUATIONS}}``: 질문 세트의 판정상황표_v2(턴마다 고정된 상황·근거·판정 항목)에서 그대로.
- ``{{QUESTION_BLOCK}}``: 질문 세트 원문 — 멀티턴이면 ``[앞 대화]``(1턴부터 바로 앞 턴까지 질문과 **같은 모델의** 답변) + ``[현재 질문]``,
  첨부가 있으면 ``[첨부파일]``(이름·종류·내용), 긴 문서면 ``[참조 자료]``(원문 그대로).
- ``{{ANSWER}}``: 수집 결과의 답변 원문. 어느 모델인지는 넣지 않는다(파일 경로·색인에만).
- ``{{RUN_MARK}}``: 이 실행의 무작위 표식(질문·답변 어디에도 없는 것으로).
- 만들지 않는 턴(프롬프트 상황표 3): 빈 출력 요구 질문(보류 — empty-output-hold-v2), 답을 받지 못한 턴(FAIL). 색인에 이유를 남긴다.

    python -X utf8 tools/judge_prompts.py data/comparison_runs/8c61f117e661.json
    python -X utf8 tools/judge_prompts.py RUN.json --providers aiona,gpt --only Q36_T1,Q11_T2 --out data/judge_prompts/test
    → <out>/<모델>/<case_id>.txt · <out>/index.jsonl (턴 × 모델 한 줄: 파일·상태·판정 항목) · <out>/manifest.json (버전·sha256·표식)
    기본 출력은 data/judge_prompts/<run_id>/ — 답변 원문이 들어 있어 git 에 올리지 않는다(data/ 는 .gitignore).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
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

VERSION = "judge-prompt-fill-v1"
TEMPLATE = ROOT / "참고자료" / "프롬프트" / "judge_prompt_v2.txt"
DEFAULT_SET = ROOT / "참고자료" / "질문세트_326"
SITUATIONS = "판정상황표_v2.json"
SLOT = re.compile(r"\{\{(RUN_MARK|CASE_SITUATIONS|QUESTION_BLOCK|ANSWER)\}\}")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _turn(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("turn") or 1)
    except (TypeError, ValueError):
        return 1


def collected(row: Optional[Dict[str, Any]]) -> bool:
    """답을 받았는가 — 빈 행·명시적 수집 실패만 '답 없음'(직접 올린 원문은 collection 이 없어도 받은 답). question_gradient 와 같은 규칙."""
    return isinstance(row, dict) and (row.get("collection") or {}).get("status", "SUCCESS") == "SUCCESS"


def question_block(row: Dict[str, Any], prior: List[Dict[str, Any]], answers: Dict[str, str]) -> str:
    """``{{QUESTION_BLOCK}}`` — 앞 대화(같은 모델의 답변) · 현재 질문 · 첨부 · 참조 자료 (판정 프롬프트 v2 문서의 조립 규칙)."""
    parts: List[str] = []
    if prior:
        lines = ["[앞 대화]"]
        for p in prior:
            n = _turn(p)
            answer = answers.get(p["case_id"])
            lines += [f"{n}턴 질문: {p.get('question') or ''}", f"{n}턴 답변: {answer if answer is not None else '(답변을 받지 못함)'}"]
        parts.append("\n".join(lines))
    parts.append("[현재 질문]\n" + (row.get("question") or ""))
    files = [a for a in row.get("attachments") or [] if isinstance(a, dict)]
    if files:
        parts.append("[첨부파일]\n" + "\n".join(f"- {a.get('name')} · {a.get('type')}\n{a.get('content') or '(내용 없음)'}" for a in files))
    if isinstance(row.get("reference"), str) and row["reference"].strip():
        parts.append("[참조 자료]\n" + row["reference"])
    return "\n".join(parts)


def fill(template: str, values: Dict[str, str]) -> str:
    """슬롯을 한 번에 바꾼다 — 넣은 글 안에 '{{ANSWER}}' 같은 글자가 있어도 다시 바뀌지 않게."""
    return SLOT.sub(lambda m: values[m.group(1)], template)


def run_mark(texts: Iterable[str]) -> str:
    joined = "\n".join(texts)
    for _ in range(100):
        mark = secrets.token_hex(3)
        if mark not in joined:
            return mark
    raise SystemExit("질문·답변에 없는 실행 표식을 만들지 못했습니다.")


def build(run: Dict[str, Any], set_dir: Path, out: Path, providers: Optional[List[str]] = None,
          only: Optional[set] = None, mark: Optional[str] = None) -> Dict[str, Any]:
    template = TEMPLATE.read_text(encoding="utf-8")
    situations_path = set_dir / SITUATIONS
    situations = {r["case_id"]: r for r in json.loads(situations_path.read_text(encoding="utf-8"))["rows"]}
    rows = flatten(json.loads((set_dir / "questions.json").read_text(encoding="utf-8")))
    by_conv: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("conversation_id") not in (None, ""):
            by_conv[r["conversation_id"]].append(r)
    providers = providers or list(run.get("providers") or (run.get("datasets") or {}))
    datasets = {p: {r.get("case_id"): r for r in (run.get("datasets") or {}).get(p) or [] if isinstance(r, dict)} for p in providers}
    answers = {p: {cid: r.get("answer") or "" for cid, r in rows_.items() if collected(r)} for p, rows_ in datasets.items()}
    texts = [r.get("question") or "" for r in rows] + [a for p in answers.values() for a in p.values()]
    mark = mark or run_mark(texts)
    if any(mark in t for t in texts):
        raise SystemExit(f"실행 표식 {mark} 이 질문·답변에 들어 있습니다 — 다른 표식으로 다시 실행하세요.")
    out.mkdir(parents=True, exist_ok=True)
    index, counts = [], Counter()
    for provider in providers:
        for row in rows:
            cid = row["case_id"]
            if only and cid not in only:
                continue
            entry = {"provider": provider, "case_id": cid, "turn": _turn(row), "items": situations[cid]["items"]}
            if asks_empty_output(None, row.get("question")):
                entry.update(status="held_empty_output", reason="빈 출력 요구 질문 — 보류(empty-output-hold-v2), 판정하지 않음")
            elif cid not in answers[provider]:
                entry.update(status="no_answer", reason="답을 받지 못함 — FAIL 로 셈, 판정하지 않음")
            else:
                prior = sorted((p for p in by_conv.get(row.get("conversation_id"), []) if _turn(p) < _turn(row)), key=_turn) \
                    if row.get("conversation_id") not in (None, "") else []
                text = fill(template, {"RUN_MARK": mark, "CASE_SITUATIONS": situations[cid]["case_situations"],
                                       "QUESTION_BLOCK": question_block(row, prior, answers[provider]), "ANSWER": answers[provider][cid]})
                path = out / provider / f"{cid}.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                entry.update(status="ready", file=str(path.relative_to(out)), chars=len(text), sha256=_sha(text))
            counts[(provider, entry["status"])] += 1
            index.append(entry)
    (out / "index.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in index), encoding="utf-8")
    manifest = {"version": VERSION, "prompt_version": "judge-prompt-v2", "template_sha256": _sha(template),
                "situations": {"file": SITUATIONS, "sha256": hashlib.sha256(situations_path.read_bytes()).hexdigest()[:16]},
                "set": set_dir.name, "run_id": run.get("run_id"), "run_mark": mark, "created_at": datetime.now(timezone.utc).isoformat(),
                "counts": {p: {s: n for (pp, s), n in sorted(counts.items()) if pp == p} for p in providers}}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path, help="수집 결과 실행 JSON (data/comparison_runs/<id>.json)")
    parser.add_argument("--set", type=Path, default=DEFAULT_SET, help="질문 세트 폴더 (questions.json·판정상황표_v2.json)")
    parser.add_argument("--providers", help="쉼표로 — 기본은 실행의 모든 모델")
    parser.add_argument("--only", help="쉼표로 case_id — 기본은 전체 턴")
    parser.add_argument("--out", type=Path, help="기본 data/judge_prompts/<run_id>")
    args = parser.parse_args(argv)
    run = json.loads(args.run.read_text(encoding="utf-8-sig"))
    out = args.out or ROOT / "data" / "judge_prompts" / str(run.get("run_id") or args.run.stem)
    manifest = build(run, args.set, out, args.providers.split(",") if args.providers else None,
                     set(args.only.split(",")) if args.only else None)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({**manifest, "out": str(out)}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
