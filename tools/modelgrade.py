"""ModelGrade CLI (modelgrade-cli-v1) — 수집 → 판정 → 결과를 명령 하나로. 어디서 멈춰도 이어 돌린다 (사용자 방향 2026-10-19 오후 미팅: 파이썬 CLI, 에이전트·구독, API 최소).

    python -X utf8 tools/modelgrade.py collect import --provider claude --csv 참고자료/질문세트_326_생성프롬프트v2/수집용/answers_claude.csv --run 8c61f117e661
        사람이 웹브라우저에서 받아 적은 답변 CSV(case_id · answer · chat_url · note)를 실행 JSON 의 그 모델 자리에 올리고 CODE 평가(28개)까지 끝낸다.
        --run 을 비우면 새 실행을 만든다(--new 이름). --label 은 결과 화면에 보일 모델 이름.
    python -X utf8 tools/modelgrade.py collect app --provider gpt --run 889e429f6d07 --only 36,30
        ChatGPT 데스크톱 앱(tools/chatgpt_app_cdp.py 로 포트 열어 띄움, 로그인은 사람이)에서 질문 묶음의 답을 받아 올리고 CODE 평가. 다시 부르면 이어서(--next 20).
    python -X utf8 tools/modelgrade.py judge --run 8c61f117e661 --only 36,30,46 --providers aiona,gpt [--out DIR]
        고정 프롬프트 v3 로 나눠서 판정(J1~J4 Jev · J5~J8 Codex CLI). 한 번에 질문 20개까지(사용자 규칙) — 넘기려면 --allow-large.
    python -X utf8 tools/modelgrade.py judge --run 8c61f117e661 --next 20 --providers aiona,gpt
        아직 안 한 질문 중 다음 20개를 자동으로 골라 판정(이어 돌리기). 결과 폴더 기본 data/judge_runs/<run>.
    python -X utf8 tools/modelgrade.py run --run 8c61f117e661 --providers aiona [--import-csv … --import-provider claude]
        올리기(선택) → 다음 묶음(20개) 판정 → 결과. 부를 때마다 한 묶음씩 — 다시 부르면 이어서.
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
import time
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
LABELS = {"claude": "Claude 웹브라우저", "gemini": "Gemini 웹브라우저", "gpt": "ChatGPT 웹브라우저", "aiona": "AIONA 웹브라우저"}   # collect import(사람이 웹에서 받은 CSV)
APP_LABELS = {"gpt": "ChatGPT 앱 (GPT-6 · Medium)", "claude": "Claude 앱 (Sonnet 5.5 · 노력 중간)",
              "gemini": "Gemini 웹브라우저 (3.6 Flash · 사고 보통)", "aiona": "AIONA 웹브라우저"}   # collect app(자동 수집: 데스크톱 앱 둘 + 웹 둘)
ALL_PROVIDERS = ["gpt", "claude", "gemini", "aiona"]
PLACE_BY_PROVIDER = {"claude": "left", "gpt": "right", "gemini": "top-right", "aiona": "bottom-right"}   # 동시 수집 창 자리(webapp.cdp_chat_web.PLACE_RECTS)
SIMPLE_IDS_FILE = ROOT / "참고자료" / "질문세트_최소78" / "ids.json"   # 간단평가 = 정밀 326 중 이 번호들, 이 순서로
EVAL_KINDS = {"1": ("simple78", "간단평가 (78개 질문)"), "2": ("full326", "정밀평가 (326개 질문)")}


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


# --- collect app (데스크톱 앱에서 받기) -----------------------------------------------------------------------------

def app_chat_class(provider: str):
    """모델 → 데스크톱 앱 수집기: gpt = ChatGPT 앱(디버깅 포트, CDP), claude = Claude 앱(UIA 접근성 API, 2026-10-19). Gemini 는 앱이 없어 웹 CSV.
    시험에서는 monkeypatch 로 가짜를 넣는다."""
    if provider == "gpt":
        from webapp.cdp_chat_web import ChatGptAppChat
        return ChatGptAppChat
    if provider == "claude":
        from webapp.uia_chat_app import ClaudeAppChat
        return ClaudeAppChat
    if provider == "gemini":
        from webapp.cdp_chat_web import GeminiChat   # 데스크톱 앱이 없어 수집용 Chrome(CDP)의 gemini.google.com
        return GeminiChat
    if provider == "aiona":
        return aiona_collector
    raise SystemExit(f"{provider} 는 자동 수집기가 없습니다 ({', '.join(APP_LABELS)} 만). 웹 수집 CSV 를 collect import 로 올리세요.")


def aiona_collector(*, debug_dir: Optional[Path] = None, place: Optional[str] = None, cancelled: Any = None, answer_timeout_s: int = 600, **_: Any):
    """AIONA 웹브라우저 수집기(webapp.aiona_web.AionaChat, 별도 Chrome 프로필)를 앱 수집기와 같은 인자로 — 창 보이게.
    로그인은 .env 의 AIONA_LOGIN_ID·PASSWORD 로 수집기가 하되, AIONA_AUTO_LOGIN=0 이면 안 한다(로그인이 풀렸으면 오류로 알림)."""
    from code_eval.llm_client import env_value
    from webapp.aiona_web import AionaChat
    return AionaChat(headless=False, allow_login=(env_value("AIONA_AUTO_LOGIN") or "1") != "0", answer_timeout_s=answer_timeout_s,
                     debug_dir=debug_dir, cancelled=cancelled, place=place, attach=True)


def split_providers(text: str, allowed: Dict[str, str]) -> List[str]:
    out = [x.strip() for x in (text or "").split(",") if x.strip()]
    bad = [x for x in out if x not in allowed]
    if bad or not out:
        raise SystemExit(f"--provider 는 {', '.join(allowed)} 중에서 쉼표로: {text!r}")
    return out


def materialize_attachments(turn: Dict[str, Any], set_dir: Path, tmp: Path) -> List[Path]:
    """질문 세트의 첨부(파일 저장소의 해시 이름)를 원래 이름으로 임시 폴더에 만들어 올린다."""
    out = []
    for a in turn.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        src = set_dir / "files" / str(a.get("path") or "")
        dst = tmp / str(a.get("name") or src.name)
        if src.is_file():
            shutil.copyfile(src, dst)
        elif a.get("content"):
            dst.write_text(str(a["content"]), encoding="utf-8")
        else:
            continue
        out.append(dst)
    return out


def cmd_collect_app(args: argparse.Namespace) -> int:
    """데스크톱 앱에서 질문 묶음의 답을 받아 실행 JSON 에 올리고 CODE 평가까지. --provider gpt,claude 처럼 여러 앱이면 **동시에**(사용자 지시 10-19:
    앱마다 별도 프로세스, 창은 좌/우 반씩). 대화는 채팅 하나에서 1턴부터 차례로, 단일·긴 문서는 새 채팅. 이미 답을 받은 질문(모든 턴 SUCCESS)은 건너뛴다."""
    providers = split_providers(args.provider, APP_LABELS)
    if len(providers) == 1:
        return collect_app_one(argparse.Namespace(**{**vars(args), "provider": providers[0]}))
    run_id = args.run
    if not run_id:   # 자식들이 같은 실행에 쓰도록 먼저 만든다
        set_dir = Path(args.set)
        groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
        run_id = save_run(new_run(args.new or f"앱 수집 {datetime.now():%Y-%m-%d}", set_dir, groups)).stem
        print(f"새 실행 {run_id}", flush=True)
    return run_collectors_parallel(providers, args, run_id)


PLACES = ("left", "right")


def run_collectors_parallel(providers: List[str], args: argparse.Namespace, run_id: str) -> int:
    """앱마다 자식 프로세스(이 CLI 의 collect app --provider 하나)를 띄워 같이 돌리고 출력을 [모델] 머리말로 섞어 보여 준다."""
    import subprocess
    import threading
    procs = []
    for i, provider in enumerate(providers):
        cmd = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "collect", "app", "--provider", provider, "--run", run_id,
               "--set", str(args.set), "--place", PLACE_BY_PROVIDER.get(provider, PLACES[i % len(PLACES)])]
        if args.only:
            cmd += ["--only", args.only]
        else:
            cmd += ["--next", str(args.next)]
        if args.allow_large:
            cmd.append("--allow-large")
        if getattr(args, "skip_done", False):
            cmd.append("--skip-done")
        if getattr(args, "label", None):
            cmd += ["--label", args.label]
        procs.append((provider, subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT))))

    def pump(provider: str, proc: Any) -> None:
        for line in proc.stdout:
            print(f"[{provider}] {line.rstrip()}", flush=True)

    threads = [threading.Thread(target=pump, args=(p, proc), daemon=True) for p, proc in procs]
    for t in threads:
        t.start()
    codes = {p: proc.wait() for p, proc in procs}
    for t in threads:
        t.join(timeout=5)
    bad = [p for p, c in codes.items() if c]
    if bad:
        print(f"실패한 앱: {', '.join(bad)} (종료 코드 {codes})", flush=True)
        return 1
    print(f"동시 수집 끝 — 실행 {run_id} | 다음: judge --run {run_id} --next 20", flush=True)
    return 0


def merge_provider_into_run(run_id: str, provider: str, label: str, merged: List[Dict[str, Any]], evaluation: Dict[str, Any], import_entry: Dict[str, Any]) -> Path:
    """실행 JSON 을 잠금 아래 다시 읽어 이 모델 자리만 바꿔 저장 — 다른 앱의 자식 프로세스가 같은 파일을 동시에 쓴다."""
    import os
    lock = RUNS / f"{run_id}.lock"
    deadline = time.monotonic() + 120
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise SystemExit(f"실행 파일 잠금을 얻지 못했습니다: {lock} (남은 잠금 파일이면 지우세요)")
            time.sleep(0.3)
    try:
        run = load_run(run_id)
        if provider not in run["providers"]:
            run["providers"].append(provider)
        run.setdefault("models", {})[provider] = label
        run.setdefault("datasets", {})[provider] = merged
        run.setdefault("evaluations", {})[provider] = evaluation
        run.setdefault("imports", []).append(import_entry)
        return save_run(run)
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def collect_app_one(args: argparse.Namespace) -> int:
    import tempfile
    from webapp.question_generation import flatten
    set_dir = Path(args.set)
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    rows_all = flatten(groups)
    label = args.label or APP_LABELS.get(args.provider, args.provider)
    run = load_run(args.run) if args.run else new_run(args.new or f"앱 수집 {datetime.now():%Y-%m-%d}", set_dir, groups)
    existing = {r.get("case_id"): r for r in (run.get("datasets") or {}).get(args.provider) or [] if isinstance(r, dict)}
    by_q: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows_all:
        by_q.setdefault(int(r["case_id"].split("_T")[0][1:]), []).append(r)
    done = {q for q, turns in by_q.items() if all((existing.get(t["case_id"]) or {}).get("collection", {}).get("status") == "SUCCESS" for t in turns)}
    if args.only:
        wanted = [int(x) for x in args.only.split(",") if x.strip()]
        if getattr(args, "skip_done", False):   # start: 목록 전체를 주되 이미 받은 질문은 건너뛴다
            wanted = [q for q in wanted if q not in done]
    else:
        wanted = [q for q in sorted(by_q) if q not in done][:args.next]
    wanted = [q for q in wanted if q in by_q]
    if len(wanted) > JUDGE_BATCH_MAX and not args.allow_large:
        raise SystemExit(f"한 번에 {JUDGE_BATCH_MAX}개까지입니다 ({len(wanted)}개). 나눠서 돌리거나 --allow-large.")
    if not wanted:
        print("받을 질문이 없습니다 — 모두 답을 받았습니다."); return 0
    total = len(by_q)
    print(f"{args.provider} · {label}: 이번 묶음 질문 {len(wanted)}개 {wanted} · 전체 {total}개 중 이미 받은 질문 {len(done)}개", flush=True)
    chat_cls = app_chat_class(args.provider)
    new_rows: Dict[str, Dict[str, Any]] = {}
    started_wall = time.monotonic()
    with chat_cls(debug_dir=ROOT / "data" / "judge_runs" / "app-debug", place=getattr(args, "place", None)) as chat, tempfile.TemporaryDirectory(prefix="mg-att-") as tmpd:
        got = len(done)
        for i, q in enumerate(wanted, 1):
            turns = sorted(by_q[q], key=lambda r: int(r.get("turn") or 1))
            print(f"▶ Q{q} ({len(turns)}턴) — 이번 묶음 {i}/{len(wanted)} · 전체 {got + 1}/{total}", flush=True)
            try:
                chat.new_chat()
            except Exception as exc:
                print(f"  Q{q}: 새 채팅 실패 — {exc}", flush=True)
                continue
            for r in turns:
                files = materialize_attachments(r, set_dir, Path(tmpd))
                res = chat.ask(r.get("question") or "", files=files, reference=r.get("reference"))
                col = {k: v for k, v in res.items() if k != "answer"}
                col["collected_at"] = _now()
                row = {"case_id": r["case_id"], "question": r.get("question") or "", "answer": res.get("answer") or "" if res.get("status") == "SUCCESS" else "",
                       "system_id": args.provider, "source": label, "case_index": rows_all.index(r), "collection": col}
                for key in ("reference", "attachments", "conversation_id", "turn"):
                    if r.get(key) not in (None, "", []):
                        row[key] = r[key]
                new_rows[r["case_id"]] = row
                print(f"  {r['case_id']}: {res.get('status')} {len(res.get('answer') or '')}자 {res.get('latency_ms', 0) // 1000}초 ({res.get('capture')})" + (f" — {res.get('error')}" if res.get("error") else ""), flush=True)
                if res.get("status") != "SUCCESS" and len(turns) > 1:
                    print(f"  Q{q}: 턴이 실패해 이 대화의 나머지 턴은 보내지 않음", flush=True)
                    break
            if all(new_rows.get(t["case_id"], {}).get("collection", {}).get("status") == "SUCCESS" for t in turns):
                got += 1
    # 실행에 합치기 — 질문 세트 순서로, 새 행이 있으면 바꾸고 없으면 전 행(또는 답 없음) 유지
    merged = []
    for index, r in enumerate(rows_all):
        row = new_rows.get(r["case_id"]) or existing.get(r["case_id"])
        if row is None:
            row = {"case_id": r["case_id"], "question": r.get("question") or "", "answer": "", "system_id": args.provider, "source": label, "case_index": index,
                   "collection": {"status": "ERROR", "error": "아직 받지 않음", "method": "pending"}}
            for key in ("reference", "attachments", "conversation_id", "turn"):
                if r.get(key) not in (None, "", []):
                    row[key] = r[key]
        merged.append(row)
    print("CODE 평가 중 …", flush=True)
    evaluation = code_evaluate(merged, args.provider)
    entry = {"provider": args.provider, "method": "collect-app-v1", "at": _now(), "questions": wanted,
             "answered": sum(1 for r in new_rows.values() if r["collection"].get("status") == "SUCCESS"),
             "seconds": round(time.monotonic() - started_wall, 1)}   # 이 묶음 수집에 걸린 벽시계 시간 (사용자 요청 10-19)
    if not args.run:
        run["providers"].append(args.provider)
        run.setdefault("models", {})[args.provider] = label
        run.setdefault("datasets", {})[args.provider] = merged
        run.setdefault("evaluations", {})[args.provider] = evaluation
        run.setdefault("imports", []).append(entry)
        path = save_run(run)
    else:
        path = merge_provider_into_run(run["run_id"], args.provider, label, merged, evaluation, entry)   # 동시 수집: 다른 앱이 쓴 자리는 건드리지 않는다
    ok = sum(1 for r in merged if r["collection"].get("status") == "SUCCESS")
    rows_by_q: Dict[int, List[Dict[str, Any]]] = {}
    for r in merged:
        rows_by_q.setdefault(int(r["case_id"].split("_T")[0][1:]), []).append(r)
    done_q = sum(1 for rs in rows_by_q.values() if all(r["collection"].get("status") == "SUCCESS" for r in rs))
    print(f"저장: {path} (실행 {run['run_id']}) | {args.provider} 받은 질문 {done_q}/{total} ({ok}/{len(merged)}턴) | 다음: 같은 명령으로 이어서, 또는 judge --run {run['run_id']} --providers {args.provider} --next 20")
    return 0


# --- judge · report -------------------------------------------------------------------------------------------------

def judged_questions(out_dir: Path, providers: List[str]) -> Dict[str, set]:
    """결과 폴더의 results.jsonl 에서 모델별로 이미 판정한 질문 번호."""
    done: Dict[str, set] = {p: set() for p in providers}
    f = out_dir / "results.jsonl"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                done.setdefault(d["provider"], set()).add(int(d["question"]))
    return done


def next_questions(run: Dict[str, Any], providers: List[str], out_dir: Path, n: int) -> List[int]:
    """요청한 모델 전부가 아직 판정하지 않은 질문부터 번호순으로 n개."""
    done = judged_questions(out_dir, providers)
    total = run.get("question_count") or max(int(r["case_id"].split("_T")[0][1:]) for p in providers for r in run["datasets"].get(p) or [] if r)
    pending = [q for q in range(1, int(total) + 1) if any(q not in done.get(p, set()) for p in providers)]
    return pending[:n], len(pending)


def cmd_judge(args: argparse.Namespace) -> int:
    from tools import judge_run
    only = [x.strip() for x in (args.only or "").split(",") if x.strip()]
    if args.next:
        if only:
            raise SystemExit("--only 와 --next 는 같이 쓰지 않습니다.")
        if args.next > JUDGE_BATCH_MAX and not args.allow_large:
            raise SystemExit(f"--next 는 {JUDGE_BATCH_MAX}개까지입니다 (사용자 규칙: 평가는 10~20개씩).")
        run = load_run(args.run)
        providers = [x.strip() for x in (args.providers or ",".join(run["providers"])).split(",") if x.strip()]
        out_dir = Path(args.out) if args.out else ROOT / "data" / "judge_runs" / args.run
        picked, pending = next_questions(run, providers, out_dir, args.next)
        if not picked:
            print(f"남은 질문이 없습니다 — {', '.join(providers)} 모두 판정 완료 ({out_dir})")
            return 0
        print(f"다음 묶음: 질문 {len(picked)}개 {picked} (남은 질문 {pending}개 중) · 모델 {', '.join(providers)}", flush=True)
        only = [str(q) for q in picked]
        args.only = ",".join(only)
        args.providers = ",".join(providers)
    if not only and not args.allow_large:
        raise SystemExit(f"--only 로 질문 번호를 {JUDGE_BATCH_MAX}개까지 지정하거나 --next 20 을 쓰세요 (사용자 규칙: 평가는 10~20개씩). 전체를 돌리려면 --allow-large.")
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


def cmd_run(args: argparse.Namespace) -> int:
    """앱 수집(선택, --collect gpt,claude: 앱을 켜서 다음 묶음 받기) → 올리기(선택) → 다음 묶음 판정 → 결과.
    한 번 부를 때마다 한 묶음(기본 20개)만 — 다시 부르면 이어서."""
    if args.collect:
        code = cmd_collect_app(argparse.Namespace(provider=args.collect, run=args.run, new=None, only=None, next=args.next, set=args.set,
                                                  label=None, allow_large=False, place=None))
        if code:
            return code
    if args.import_csv:
        if not args.import_provider:
            raise SystemExit("--import-csv 에는 --import-provider 가 필요합니다.")
        ns = argparse.Namespace(provider=args.import_provider, csv=args.import_csv, run=args.run, new=None, set=args.set, label=args.label)
        cmd_collect_import(ns)
    out_dir = Path(args.out) if args.out else ROOT / "data" / "judge_runs" / args.run
    code = cmd_judge(argparse.Namespace(run=args.run, only=None, next=args.next, providers=args.providers, out=str(out_dir), set=args.set,
                                        workers=args.workers, allow_large=False))
    if code:
        return code
    if (out_dir / "results.jsonl").exists():
        cmd_report(argparse.Namespace(dir=str(out_dir)))
    return 0


# --- start (메뉴: 간단 78 / 정밀 326 → 네 모델 동시 수집 → 판정 20개씩 → 결과) ------------------------------------

def set_id_of(set_dir: Path) -> str:
    f = set_dir / "set.json"
    return (json.loads(f.read_text(encoding="utf-8")).get("id") if f.exists() else None) or set_dir.name


def question_numbers(kind: str, groups: List[Dict[str, Any]]) -> List[int]:
    """평가 종류의 질문 번호 — 간단평가는 최소78 ids.json 의 번호를 그 순서(던지는 순서 = 고른 순서)로, 정밀평가는 세트 전체를 번호순으로."""
    from webapp.question_generation import flatten
    nums = sorted({int(r["case_id"].split("_T")[0][1:]) for r in flatten(groups)})
    if kind == "simple78":
        ids = json.loads(SIMPLE_IDS_FILE.read_text(encoding="utf-8"))["ids"]
        have = set(nums)
        return [int(q) for q in ids if int(q) in have]
    return nums


def find_run_for(set_dir: Path, kind: str) -> Optional[str]:
    """같은 질문 세트·같은 평가 종류로 쓰던 실행(가장 최근). 정밀평가는 CLI 로 만든 실행(AIONA 웹 답을 올린 것)도 이어 쓴다."""
    sid = set_id_of(set_dir)
    cands = []
    for f in RUNS.glob("*.json"):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(r, dict) and (r.get("question_set") or {}).get("set_id") == sid:
            cands.append(r)
    tagged = [r for r in cands if r.get("eval_kind") == kind]
    if tagged:
        return max(tagged, key=lambda r: r.get("created_at") or "")["run_id"]
    if kind == "full326":
        cli = [r for r in cands if not r.get("eval_kind") and r.get("run_kind") == "manual-web-import"]
        if cli:
            r = max(cli, key=lambda r: r.get("created_at") or "")
            r["eval_kind"] = kind
            save_run(r)
            return r["run_id"]
    return None


def ask_choice() -> str:
    print("어떤 평가를 진행할까요?")
    for key, (_, title) in EVAL_KINDS.items():
        print(f"  {key}. {title}")
    while True:
        try:
            choice = input("번호 입력 후 Enter: ").strip()
        except EOFError:
            raise SystemExit("입력이 없어 그만둡니다.")
        if choice in EVAL_KINDS:
            return choice
        print("1 또는 2 를 입력하세요.")


def cmd_start(args: argparse.Namespace) -> int:
    """메뉴에서 평가 종류를 고르면 네 모델(ChatGPT 앱·Claude 앱·Gemini 웹·AIONA 웹)이 동시에 그 세트의 답을 받고, 원하면 판정을 20개씩 끝까지 돌린 뒤 결과를 보인다.
    이미 받은 질문·판정한 질문은 건너뛰므로 같은 명령을 다시 부르면 이어서."""
    set_dir = Path(args.set)
    choice = args.choice or ask_choice()
    kind, title = EVAL_KINDS[choice]
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    qs = question_numbers(kind, groups)
    providers = split_providers(args.providers, APP_LABELS)
    run_id = args.run or find_run_for(set_dir, kind)
    if not run_id:
        run = new_run(title, set_dir, groups)
        run["eval_kind"] = kind
        run_id = save_run(run).stem
        print(f"새 실행 {run_id}", flush=True)
    print(f"{title} — 질문 {len(qs)}개 · 모델 {', '.join(providers)} 동시 수집 · 실행 {run_id}", flush=True)
    ns = argparse.Namespace(provider=",".join(providers), run=run_id, new=None, only=",".join(str(q) for q in qs), next=JUDGE_BATCH_MAX,
                            set=str(set_dir), label=None, allow_large=True, place=None, skip_done=True)
    t_collect = time.monotonic()
    code = cmd_collect_app(ns)
    if code:
        return code
    print(f"수집 단계 끝 — 벽시계 {_hms(time.monotonic() - t_collect)}", flush=True)
    print_collection_times(run_id, providers)
    out_dir = ROOT / "data" / "judge_runs" / run_id
    go = args.judge
    if go == "ask":
        try:
            go = "y" if input("이어서 판정할까요? 20개씩 끝날 때까지 돌립니다 (y/N): ").strip().lower() == "y" else "n"
        except EOFError:
            go = "n"
    if go != "y":
        print(f"판정은 나중에: python -X utf8 tools/modelgrade.py start --choice {choice} --judge y  (수집은 건너뛰고 판정만 이어서)", flush=True)
        return 0
    t_judge = time.monotonic()
    while True:
        judged = judged_questions(out_dir, providers)
        pending = [q for q in qs if any(q not in judged.get(p, set()) for p in providers)]
        if not pending:
            break
        batch = pending[:JUDGE_BATCH_MAX]
        print(f"판정 {len(batch)}개 {batch} (남은 질문 {len(pending)}개)", flush=True)
        code = cmd_judge(argparse.Namespace(run=run_id, only=",".join(str(q) for q in batch), next=None, providers=",".join(providers),
                                            out=str(out_dir), set=str(set_dir), workers=1, allow_large=False))
        if code:
            return code
    print(f"판정 단계 끝 — 벽시계 {_hms(time.monotonic() - t_judge)}", flush=True)
    if (out_dir / "results.jsonl").exists():
        cmd_report(argparse.Namespace(dir=str(out_dir)))
    return 0


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}시간 {s % 3600 // 60}분 {s % 60}초" if s >= 3600 else (f"{s // 60}분 {s % 60}초" if s >= 60 else f"{s}초")


def print_collection_times(run_id: str, providers: Optional[List[str]] = None) -> None:
    """모델별 답변 수집 시간 — 받은 턴의 답변 시간 합(모델이 답하는 데 걸린 시간)과 수집 묶음의 벽시계 합. (사용자 요청 10-19)"""
    try:
        run = load_run(run_id)
    except SystemExit:
        return
    providers = providers or list(run.get("providers") or [])
    print("| 모델 | 받은 질문 | 답변 시간 합 | 질문당 평균 | 수집 묶음 벽시계 합 |\n|---|---|---|---|---|")
    for p in providers:
        rows = [r for r in (run.get("datasets") or {}).get(p) or [] if isinstance(r, dict)]
        ok = [r for r in rows if (r.get("collection") or {}).get("status") == "SUCCESS"]
        qs = {int(r["case_id"].split("_T")[0][1:]) for r in ok}
        answer_s = sum((r.get("collection") or {}).get("latency_ms") or 0 for r in rows) / 1000
        wall = sum(e.get("seconds") or 0 for e in run.get("imports") or [] if e.get("provider") == p and e.get("method") == "collect-app-v1")
        per_q = answer_s / len(qs) if qs else 0
        print(f"| {p} | {len(qs)} | {_hms(answer_s)} | {per_q:.0f}초 | {_hms(wall)} |")


def print_judge_times(out_dir: Path) -> None:
    f = out_dir / "timing.jsonl"
    if not f.exists():
        return
    recs = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    judge = [r for r in recs if r.get("kind") == "judge"]
    if judge:
        total = sum(r.get("seconds") or 0 for r in judge)
        n = sum(r.get("judgments") or 0 for r in judge)
        print(f"판정 시간: 총 {_hms(total)} · 판정 {n}건(질문×모델) · 건당 평균 {total / n:.0f}초" if n else f"판정 시간: 총 {_hms(total)}")


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
    print_collection_times(d.name)
    print_judge_times(d)
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
    app = collect.add_parser("app", help="앱·웹을 켜서 질문 묶음의 답 받기 (+ CODE 평가): gpt=ChatGPT 앱, claude=Claude 앱, gemini=수집용 Chrome, aiona=AIONA 웹. 같은 명령을 다시 부르면 이어서")
    app.add_argument("--provider", required=True, help="gpt, claude, gemini, aiona — 쉼표로 여럿이면 동시에 (예: gpt,claude,gemini,aiona)")
    app.add_argument("--run", help="기존 실행 id (비우면 새 실행)")
    app.add_argument("--new", help="새 실행 이름")
    app.add_argument("--only", help="질문 번호, 쉼표로")
    app.add_argument("--next", type=int, default=JUDGE_BATCH_MAX, help="아직 안 받은 질문 중 다음 N개 (기본 20)")
    app.add_argument("--set", default=str(DEFAULT_SET))
    app.add_argument("--label", help="결과에 보일 모델 이름 (기본: 'ChatGPT 앱 (GPT-6 · Medium)' · 'Claude 앱 (Sonnet 5.5 · 노력 중간)')")
    app.add_argument("--allow-large", action="store_true")
    app.add_argument("--place", choices=sorted(set(PLACE_BY_PROVIDER.values())), help="창 자리 (동시 수집 때 부모가 넣음)")
    app.add_argument("--skip-done", action="store_true", help="--only 목록에서 이미 받은 질문은 건너뛰기")
    app.set_defaults(func=cmd_collect_app)
    jd = sub.add_parser("judge", help="고정 프롬프트 v3 나눠서 판정 (한 번에 20개까지)")
    jd.add_argument("--run", required=True)
    jd.add_argument("--only", help="질문 번호, 쉼표로")
    jd.add_argument("--next", type=int, help="아직 안 한 질문 중 다음 N개 (20까지)")
    jd.add_argument("--providers")
    jd.add_argument("--out", help="결과 폴더 (기본 data/judge_runs/<run>)")
    jd.add_argument("--set", default=str(DEFAULT_SET))
    jd.add_argument("--workers", type=int, default=1)
    jd.add_argument("--allow-large", action="store_true")
    jd.set_defaults(func=cmd_judge)
    rn = sub.add_parser("run", help="앱 수집(선택) → 올리기(선택) → 다음 묶음 판정 → 결과. 부를 때마다 한 묶음, 다시 부르면 이어서")
    rn.add_argument("--run", required=True)
    rn.add_argument("--collect", help="먼저 데스크톱 앱을 켜서 다음 묶음의 답을 받을 모델: gpt, claude (쉼표로 여럿)")
    rn.add_argument("--providers")
    rn.add_argument("--next", type=int, default=JUDGE_BATCH_MAX)
    rn.add_argument("--out")
    rn.add_argument("--set", default=str(DEFAULT_SET))
    rn.add_argument("--workers", type=int, default=1)
    rn.add_argument("--import-csv", help="먼저 올릴 웹 답변 CSV")
    rn.add_argument("--import-provider", choices=["aiona", "gpt", "claude", "gemini"])
    rn.add_argument("--label")
    rn.set_defaults(func=cmd_run)
    rp = sub.add_parser("report", help="판정 결과 요약")
    rp.add_argument("--dir", required=True)
    rp.set_defaults(func=cmd_report)
    st = sub.add_parser("start", help="메뉴: 1 간단평가(78) / 2 정밀평가(326) → 네 모델 동시 수집 → 판정 20개씩 → 결과 (인자 없이 부르면 이것)")
    st.add_argument("--choice", choices=list(EVAL_KINDS), help="메뉴 번호를 미리 주기 (안 주면 물어봄)")
    st.add_argument("--providers", default=",".join(ALL_PROVIDERS))
    st.add_argument("--run", help="이어 쓸 실행 id (기본: 같은 세트·종류의 최근 실행, 없으면 새로)")
    st.add_argument("--set", default=str(DEFAULT_SET))
    st.add_argument("--judge", choices=["ask", "y", "n"], default="ask", help="수집 뒤 판정: ask(물어봄)·y·n")
    st.set_defaults(func=cmd_start)
    if not argv and len(sys.argv) <= 1 or argv == []:
        argv = ["start"]
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
