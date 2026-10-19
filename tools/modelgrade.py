"""ModelGrade CLI (modelgrade-cli-v1) — 수집 → 판정 → 결과를 명령 하나로. 어디서 멈춰도 이어 돌린다 (사용자 방향 2026-10-19 오후 미팅: 파이썬 CLI, 에이전트·구독, API 최소).

    python -X utf8 tools/modelgrade.py collect import --provider claude --csv 참고자료/질문세트_326_생성프롬프트v2/수집용/answers_claude.csv --run 8c61f117e661
        사람이 웹브라우저에서 받아 적은 답변 CSV(case_id · answer · chat_url · note)를 실행 JSON 의 그 모델 자리에 올리고 CODE 평가(28개)까지 끝낸다.
        --run 을 비우면 새 실행을 만든다(--new 이름). --label 은 결과 화면에 보일 모델 이름.
    python -X utf8 tools/modelgrade.py judge --run 8c61f117e661 --only 36,30,46 --providers aiona,gpt [--out DIR]
        고정 프롬프트 v3 로 나눠서 판정(J1~J4 Jev · J5~J8 Codex CLI). 한 번에 질문 20개까지(사용자 규칙) — 넘기려면 --allow-large.
    python -X utf8 tools/modelgrade.py report --dir data/judge_runs/pilot10b
        판정 결과 폴더의 모델별 PASS·FAIL·보류와 시간.

규칙: 답 못 받은 질문은 FAIL(분모 유지), 빈 출력 요구 질문은 보류. 비밀번호·키는 다루지 않는다.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERSION = "modelgrade-cli-v1"
IMPORT_METHOD = "manual-web-csv-v1"
RUNS = ROOT / "data" / "comparison_runs"
DEFAULT_SET = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"
JUDGE_BATCH_MAX = 20   # 사용자 규칙 2026-10-19: 평가는 10~20개씩
LABELS = {"claude": "Claude 웹브라우저", "gemini": "Gemini 웹브라우저", "gpt": "ChatGPT 웹브라우저", "aiona": "AIONA 웹브라우저"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- collect import -------------------------------------------------------------------------------------------------

def read_answers_csv(path: Path) -> Dict[str, Dict[str, str]]:
    """answers_<모델>.csv → {case_id: {answer, chat_url, note}}. 중복 case_id 는 오류."""
    out: Dict[str, Dict[str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "case_id" not in reader.fieldnames or "answer" not in reader.fieldnames:
            raise SystemExit(f"{path.name}: case_id · answer 열이 있어야 합니다 (열: {reader.fieldnames})")
        for row in reader:
            cid = (row.get("case_id") or "").strip()
            if not cid:
                continue
            if cid in out:
                raise SystemExit(f"{path.name}: case_id {cid} 가 두 번 있습니다")
            out[cid] = {"answer": row.get("answer") or "", "chat_url": (row.get("chat_url") or "").strip(), "note": (row.get("note") or "").strip()}
    return out


def collection_of(entry: Optional[Dict[str, str]], case_id: str) -> Dict[str, Any]:
    """CSV 한 줄 → 실행의 collection 기록. 답이 있으면 SUCCESS, note 에 '빈 답'이면 빈 답도 SUCCESS, 그 밖에 비어 있으면 ERROR(답 없음 → FAIL)."""
    if entry is None:
        return {"status": "ERROR", "error": "CSV 에 줄 없음", "capture": "manual-csv", "method": IMPORT_METHOD, "collected_at": _now()}
    answer, note = entry["answer"], entry["note"]
    if answer.strip() or "빈 답" in note:
        status, error = "SUCCESS", None
    elif "오류" in note:
        status, error = "ERROR", f"수집 오류 (note: {note})"
    else:
        status, error = "ERROR", "답이 비어 있음"
    return {"status": status, "error": error, "answer": answer, "capture": "manual-csv", "url": entry["chat_url"] or None, "note": note or None,
            "method": IMPORT_METHOD, "collected_at": _now()}


def build_rows(groups: List[Dict[str, Any]], answers: Dict[str, Dict[str, str]], provider: str, label: str) -> List[Dict[str, Any]]:
    from webapp.question_generation import flatten
    rows = []
    for index, r in enumerate(flatten(groups)):
        entry = answers.get(r["case_id"])
        col = collection_of(entry, r["case_id"])
        row = {"case_id": r["case_id"], "question": r.get("question") or "", "answer": (entry or {}).get("answer", "") if col["status"] == "SUCCESS" else "",
               "system_id": provider, "source": label, "case_index": index, "collection": col}
        for key in ("reference", "attachments", "conversation_id", "turn"):
            if r.get(key) not in (None, "", []):
                row[key] = r[key]
        rows.append(row)
    return rows


def code_evaluate(rows: List[Dict[str, Any]], provider: str) -> Dict[str, Any]:
    """답을 받은 행만 CODE 평가(28개 검사). AIONA 특화 검사는 AIONA 행에만."""
    from code_eval import engine
    from code_eval.config import load_aiona_config
    cfg = load_aiona_config()
    cfg.enabled = provider == "aiona"
    payloads = [{k: r[k] for k in ("case_id", "question", "answer", "reference", "attachments", "conversation_id", "turn") if r.get(k) not in (None, "")}
                for r in rows if r["collection"]["status"] == "SUCCESS"]
    for p in payloads:
        p.setdefault("answer", "")
    result = engine.evaluate_batch(payloads, cfg) if payloads else {"cases": [], "summary": {}}
    result["run"] = {"method": IMPORT_METHOD, "evaluated_at": _now(), "evaluated_cases": len(payloads), "skipped_no_answer": len(rows) - len(payloads)}
    return result


def load_run(run_id: str) -> Dict[str, Any]:
    path = RUNS / f"{run_id}.json"
    if not path.exists():
        raise SystemExit(f"실행 파일이 없습니다: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_run(run: Dict[str, Any]) -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    path = RUNS / f"{run['run_id']}.json"
    if path.exists():
        shutil.copyfile(path, path.with_suffix(f".json.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def new_run(name: str, set_dir: Path, groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    from webapp.question_generation import flatten
    return {"run_id": uuid.uuid4().hex[:12], "name": name, "created_at": _now(), "status": "COLLECT", "phase": "COLLECT", "providers": [],
            "models": {}, "datasets": {}, "evaluations": {}, "question_count": len(groups), "total": len(flatten(groups)),
            "question_set": {"set_id": json.loads((set_dir / "set.json").read_text(encoding="utf-8")).get("id") if (set_dir / "set.json").exists() else set_dir.name},
            "run_kind": "manual-web-import", "aiona_specialized": False, "blind_requested": True}


def cmd_collect_import(args: argparse.Namespace) -> int:
    set_dir = Path(args.set)
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    answers = read_answers_csv(Path(args.csv))
    label = args.label or LABELS.get(args.provider, args.provider)
    rows = build_rows(groups, answers, args.provider, label)
    known = {r["case_id"] for r in rows}
    extra = sorted(set(answers) - known)
    if extra:
        print(f"경고: 질문 세트에 없는 case_id {len(extra)}개는 무시합니다: {extra[:8]}")
    statuses = Counter(r["collection"]["status"] for r in rows)
    print(f"{args.provider} · {label}: 턴 {len(rows)}개 중 답 있음 {statuses.get('SUCCESS', 0)} · 답 없음 {statuses.get('ERROR', 0)} (CSV 줄 {len(answers)})")
    print("CODE 평가 중 …", flush=True)
    evaluation = code_evaluate(rows, args.provider)
    code_fail = sum(1 for c in evaluation.get("cases") or [] if any(x.get("verdict") == "FAIL" and x.get("counted") for x in c.get("results") or []))
    run = load_run(args.run) if args.run else new_run(args.new or f"웹 수집 {datetime.now():%Y-%m-%d}", set_dir, groups)
    if args.provider not in run["providers"]:
        run["providers"].append(args.provider)
    run.setdefault("models", {})[args.provider] = label
    run.setdefault("datasets", {})[args.provider] = rows
    run.setdefault("evaluations", {})[args.provider] = evaluation
    run.setdefault("imports", []).append({"provider": args.provider, "csv": str(Path(args.csv)), "at": _now(), "method": IMPORT_METHOD,
                                          "answered": statuses.get("SUCCESS", 0), "missing": statuses.get("ERROR", 0)})
    path = save_run(run)
    print(f"저장: {path} (실행 {run['run_id']}) | CODE FAIL 턴 {code_fail} / 평가 {evaluation['run']['evaluated_cases']} | 다음: judge --run {run['run_id']} --providers {args.provider} --only …")
    return 0


# --- judge · report -------------------------------------------------------------------------------------------------

def cmd_judge(args: argparse.Namespace) -> int:
    from tools import judge_run
    only = [x.strip() for x in (args.only or "").split(",") if x.strip()]
    if not only and not args.allow_large:
        raise SystemExit(f"--only 로 질문 번호를 {JUDGE_BATCH_MAX}개까지 지정하세요 (사용자 규칙: 평가는 10~20개씩). 전체를 돌리려면 --allow-large.")
    if len(only) > JUDGE_BATCH_MAX and not args.allow_large:
        raise SystemExit(f"한 번에 {JUDGE_BATCH_MAX}개까지입니다 ({len(only)}개 지정). 나눠서 돌리거나 --allow-large.")
    argv = [str(RUNS / f"{args.run}.json"), "--set", str(args.set)]
    if args.only:
        argv += ["--only", args.only]
    if args.providers:
        argv += ["--providers", args.providers]
    if args.out:
        argv += ["--out", str(args.out)]
    argv += ["--workers", str(args.workers)]
    return judge_run.main(argv)


def cmd_report(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    results = d / "results.jsonl"
    if not results.exists():
        raise SystemExit(f"결과가 없습니다: {results}")
    rows = [json.loads(l) for l in results.read_text(encoding="utf-8").splitlines() if l.strip()]
    by = {}
    for r in rows:
        b = by.setdefault(r["provider"], Counter())
        b[r.get("verdict")] += 1
        b["_sec"] += r.get("seconds") or 0
    print(f"{d} — 질문 {len({(r['provider'], r['question']) for r in rows})}건")
    print("| 모델 | PASS | FAIL | 보류 | 질문당 평균(초) |\n|---|---|---|---|---|")
    for p, b in by.items():
        n = b["PASS"] + b["FAIL"] + b["HOLD"]
        print(f"| {p} | {b['PASS']} | {b['FAIL']} | {b['HOLD']} | {b['_sec'] / max(1, n):.0f} |")
    if (d / "summary.md").exists():
        print((d / "summary.md").read_text(encoding="utf-8"))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="modelgrade", description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    collect = sub.add_parser("collect", help="답변 수집").add_subparsers(dest="sub", required=True)
    imp = collect.add_parser("import", help="웹브라우저에서 받아 적은 답변 CSV 올리기 (+ CODE 평가)")
    imp.add_argument("--provider", required=True, choices=["aiona", "gpt", "claude", "gemini"])
    imp.add_argument("--csv", required=True)
    imp.add_argument("--run", help="기존 실행 id (비우면 새 실행)")
    imp.add_argument("--new", help="새 실행 이름")
    imp.add_argument("--set", default=str(DEFAULT_SET))
    imp.add_argument("--label", help="결과에 보일 모델 이름 (기본: '<모델> 웹브라우저')")
    imp.set_defaults(func=cmd_collect_import)
    jd = sub.add_parser("judge", help="고정 프롬프트 v3 나눠서 판정 (한 번에 20개까지)")
    jd.add_argument("--run", required=True)
    jd.add_argument("--only", help="질문 번호, 쉼표로")
    jd.add_argument("--providers")
    jd.add_argument("--out", help="결과 폴더 (기본 data/judge_runs/<run>)")
    jd.add_argument("--set", default=str(DEFAULT_SET))
    jd.add_argument("--workers", type=int, default=1)
    jd.add_argument("--allow-large", action="store_true")
    jd.set_defaults(func=cmd_judge)
    rp = sub.add_parser("report", help="판정 결과 요약")
    rp.add_argument("--dir", required=True)
    rp.set_defaults(func=cmd_report)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
