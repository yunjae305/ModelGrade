"""CLI — 웹 화면과 같은 엔진을 호출한다. CODE 평가 뒤 Jev 의미 평가(J1~J8)를 이어서 기록한다.

기존 LLM 재검토는 기본 흐름에서 빠졌고 별도 명령으로만 실행한다:
    py -3 -m code_eval.llm_review --results results/set2_q100/results.json --out results/set2_q100_llm

    py -3 -m code_eval run --input data/code_eval/AIONA_winz_set2_Q100_id_question_answer_v1.json --out results/set2_q100 --overwrite
    py -3 -m code_eval single --question "5에서 8을 빼면?" --answer "결과는 -3입니다."
    py -3 -m code_eval info
    py -3 -m code_eval criteria   # CODE 검사 단위·J1~J8 통합 목록 (판정에 쓰지 않음)
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from .config import load_aiona_config
from .core import versions
from .engine import evaluate_batch
from .io import load_input_file, write_json, write_jsonl
from .combined import combined_summary
from .jev_eval import evaluation_log, jev_after_code
from .report_html import render_report


def _print(text: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print(text, flush=True)


def _cfg(args):
    override = None
    if getattr(args, "aiona_override", None):
        override = json.loads(Path(args.aiona_override).read_text(encoding="utf-8"))
    return load_aiona_config(Path(args.aiona_config) if args.aiona_config else None, override)


@contextmanager
def _code_phase_guard():
    """Block network calls during CODE evaluation, including attempts swallowed by a checker."""
    attempts = []

    def deny(*_args, **_kwargs):
        attempts.append("network connection")
        raise RuntimeError("CODE 평가 중 네트워크·LLM 호출 금지")

    try:
        with patch.object(socket.socket, "connect", deny), \
             patch.object(socket.socket, "connect_ex", deny), \
             patch.object(socket, "create_connection", deny):
            yield
    finally:
        if attempts:
            raise RuntimeError(f"CODE 평가 중 차단한 네트워크 호출 {len(attempts)}건")


def _summary_text(batch) -> str:
    s = batch["summary"]
    rate = s["common"]["pass_rate"]
    rate_text = "—" if rate is None else f"{rate * 100:.1f}%"
    lines = [f"입력 {s['totals']['cases']}건 · 평가 {s['totals']['evaluated']} · 입력 오류 {s['totals']['input_errors']}"
             f" · 한도 초과 {s['totals']['limit_exceeded']}",
             f"공통 PASS {s['common']['pass']} · FAIL {s['common']['fail']} · 미판정 {s['common']['unjudged']}"
             f" · 통과율 {rate_text}"]
    for sub, b in s["by_subcategory"].items():
        lines.append(f"  {sub}: PASS {b['pass']} / FAIL {b['fail']} / 미판정 {b['unjudged']}")
    lines.append(f"AIONA 특화 PASS {s['specialized']['pass']} · FAIL {s['specialized']['fail']} · 미판정 {s['specialized']['unjudged']}")
    lines.append(f"결과 해시 {batch['result_digest'][:16]} · 소요 {batch['run']['duration_ms']} ms")
    return "\n".join(lines)


def _jev_text(batch) -> str:
    jev = batch["jev_eval"]
    parts = " · ".join(f"{cid} {v['name']} 대상 {v['targets']} · PASS {v['pass']} / FAIL {v['fail']}"
                       for cid, v in jev["summary"].items())
    conv = jev.get("conversations") or {}
    conv_line = (f"  대화 {conv['count']}개 · J6 유지 PASS {conv['pass']} / FAIL {conv['fail']} / 미완료 {conv['incomplete']}"
                 if conv.get("count") else "")
    return "\n".join(line for line in (
        f"{jev.get('engine_label', 'Jev')} 의미 평가 상태 {jev['status']} · 대상 {jev['targets']} · 완료 {jev['completed']}"
        + (f" · 모델 {', '.join(jev['models'])}" if jev["models"] else ""),
        f"  {parts}", conv_line, f"  {jev['access_reason']}" if jev.get("access_reason") else "") if line)


def _prepare_output(out: Path, overwrite: bool) -> None:
    """평가·모델 호출 전에 결과 경로 충돌을 확인한다. 쓰기 시점에도 다시 검사한다."""
    targets = [out / name for name in ("results.json", "results.jsonl", "eval_log.jsonl", "report.html")]
    for path in targets:
        if path.exists():
            if not overwrite:
                raise FileExistsError(f"기존 결과 파일을 덮어쓰지 않음: {path} (--overwrite 로 명시)")
            if not path.is_file():
                raise IsADirectoryError(f"결과 경로가 파일이 아님: {path}")
    out.mkdir(parents=True, exist_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="code_eval", description="AIONA 코드 평가기 v16 (CODE 검사 + Jev 의미 평가)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("run", "single"):
        p = sub.add_parser(name)
        p.add_argument("--aiona-config", help="AIONA 특화 설정 파일 (기본: code_eval/settings/aiona_specialized.json)")
        p.add_argument("--aiona-override", help="특화 설정 덮어쓰기 JSON (marker_registry / forbidden_expressions)")
        p.add_argument("--semantic-engine", choices=("auto", "openai-api", "anthropic-api", "jev", "claude-cli", "codex-cli", "gpt-cli"),
                       help="의미 평가 엔진 (기본: .env 의 SEMANTIC_ENGINE, 없으면 jev). codex-cli/gpt-cli 는 Codex CLI 로그인 사용")
        p.add_argument("--semantic-pipeline", choices=("legacy", "items"),
                       help="의미 평가 방식 (기본: .env 의 SEMANTIC_PIPELINE, 없으면 legacy). items 는 세부 기준·점검 항목에 예/아니오+인용")
    run = sub.choices["run"]
    run.add_argument("--input", required=True,
                     help="JSON 또는 JSONL (question, answer 필수 · 선택: reference, 대화 입력은 conversation_id + turn)")
    run.add_argument("--out", required=True, help="결과 폴더 (results.json · results.jsonl · eval_log.jsonl · report.html)")
    run.add_argument("--overwrite", action="store_true", help="기존 결과 파일 덮어쓰기 허용")
    single = sub.choices["single"]
    single.add_argument("--question", required=True)
    single.add_argument("--answer", required=True)
    single.add_argument("--system-id")
    single.add_argument("--displayed-text")
    single.add_argument("--case-id")
    single.add_argument("--reference", help="J5 DATA 신뢰도의 기준이 되는 참조 데이터 (문자열)")
    single.add_argument("--json", action="store_true", help="전체 결과 JSON 출력")
    sub.add_parser("info")
    sub.add_parser("criteria")
    args = parser.parse_args(argv)

    if args.cmd == "info":
        _print(json.dumps({"versions": versions(), "aiona_config": load_aiona_config().summary()},
                          ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "criteria":
        from .criteria_registry import registry
        _print(json.dumps(registry(), ensure_ascii=False, indent=2))
        return 0
    cfg = _cfg(args)
    if args.cmd == "single":
        payload = {"question": args.question, "answer": args.answer}
        for key, value in (("system_id", args.system_id), ("displayed_text", args.displayed_text),
                           ("case_id", args.case_id), ("reference", args.reference)):
            if value is not None:
                payload[key] = value
        with _code_phase_guard():
            batch = evaluate_batch([payload], cfg)
        batch["jev_eval"] = jev_after_code(batch, engine=args.semantic_engine, pipeline=args.semantic_pipeline)
        batch["combined"] = combined_summary(batch)
        if args.json:
            _print(json.dumps(batch, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        case = batch["cases"][0]
        for r in case["results"]:
            x = r["explain"]
            _print(f"[{r['verdict'] or '판정 안 함'}] {x['headline']}  ({r['subcategory']})")
            if r["verdict"] != "PASS":
                _print(f"    왜: {x['summary']}")
                if x.get("expected") or x.get("actual"):
                    _print(f"    기대: {x.get('expected') or '—'}  /  실제: {x.get('actual') or '—'}")
                for loc in x["locations"][:3]:
                    if loc["kind"] == "snippet" and loc.get("segments"):
                        line = "".join(f"[{sg['text']}]" if sg["mark"] else sg["text"] for sg in loc["segments"])
                        _print(f"    위치: {loc['label']}  {line}")
                    elif loc["kind"] == "snippet":
                        _print(f"    위치: {loc['label']}  {loc['before']}[{loc['hit']}]{loc['after']}")
                    elif loc["kind"] == "code":
                        for ln in loc["lines"]:
                            _print(f"    {'▶' if ln['mark'] else ' '} {ln['n']:>3} | {ln['text']}")
                    elif loc["kind"] == "list":
                        _print(f"    {loc['label']}: " + " / ".join(loc["items"][:8]))
                if x.get("fix"):
                    _print(f"    고치려면: {x['fix']}")
        _print(_summary_text(batch))
        for d in (batch["jev_eval"]["records"][0]["decisions"] or {}).values():
            confidence = d.get("confidence")
            detail = (f"confidence {confidence * 100:.0f}%" if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                      else d.get("reason") or "")
            _print(f"[{batch['jev_eval']['engine_label']}] {d['criterion_id']} {d['name']} {d['decision'] or d.get('status')} / {detail}")
        _print(_jev_text(batch))
        return 0

    out = Path(args.out)
    _prepare_output(out, args.overwrite)
    items, parse_errors = load_input_file(Path(args.input))

    def progress(done, total):
        if done == total or done % 250 == 0:
            print(f"\r진행 {done}/{total}", end="", file=sys.stderr, flush=True)

    with _code_phase_guard():
        batch = evaluate_batch(items, cfg, progress=progress)
    print(file=sys.stderr)
    batch["jev_eval"] = jev_after_code(batch, engine=args.semantic_engine, pipeline=args.semantic_pipeline)
    batch["combined"] = combined_summary(batch)
    batch["input"] = {"path": str(args.input), "items": len(items), "jsonl_parse_errors": parse_errors}
    _prepare_output(out, args.overwrite)
    with _code_phase_guard():
        write_json(out / "results.json", batch, args.overwrite)
        write_jsonl(out / "results.jsonl", batch["cases"], args.overwrite)
        write_jsonl(out / "eval_log.jsonl", evaluation_log(batch), args.overwrite)
        (out / "report.html").write_text(render_report(batch), encoding="utf-8")
    _print(_summary_text(batch))
    _print(_jev_text(batch))
    _print(f"저장: {out / 'results.json'} · {out / 'results.jsonl'} · {out / 'eval_log.jsonl'} · {out / 'report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
