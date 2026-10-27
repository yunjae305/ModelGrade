"""ModelGrade CLI (modelgrade-cli-v1) — 수집 → 판정 → 결과를 명령 하나로. 어디서 멈춰도 이어 돌린다 (사용자 방향 2026-10-19 오후 미팅: 파이썬 CLI, 에이전트·구독, API 최소).

    python -X utf8 tools/modelgrade.py collect import --provider claude --csv 참고자료/질문세트_326_생성프롬프트v2/수집용/answers_claude.csv --run 8c61f117e661
        사람이 웹브라우저에서 받아 적은 답변 CSV(case_id · answer · chat_url · note)를 실행 JSON 의 그 모델 자리에 올리고 CODE 평가(28개)까지 끝낸다.
        --run 을 비우면 새 실행을 만든다(--new 이름). --label 은 결과 화면에 보일 모델 이름.
    python -X utf8 tools/modelgrade.py collect web --provider gpt --run 889e429f6d07 --only 36,30
        수집용 Chrome(tools/chrome_cdp.py — 평소 Chrome + 디버깅 포트, 로그인은 사람이 한 번)의 사이트 창에서 질문 묶음의 답을 받아 올리고 CODE 평가. 다시 부르면 이어서(--next 20).
    python -X utf8 tools/modelgrade.py judge --run 8c61f117e661 --only 36,30,46 --providers aiona,gpt [--out DIR]
        고정 프롬프트 v3 로 나눠서 판정(J1~J4 Jev · J5~J8 Codex CLI). 한 번에 질문 20개까지(사용자 규칙) — 넘기려면 --allow-large.
    python -X utf8 tools/modelgrade.py judge --run 8c61f117e661 --next 20 --providers aiona,gpt
        아직 안 한 질문 중 다음 20개를 자동으로 골라 판정(이어 돌리기). 결과 폴더 기본 data/judge_runs/<run>.
    python -X utf8 tools/modelgrade.py run --run 8c61f117e661 --providers aiona [--import-csv … --import-provider claude]
        올리기(선택) → 다음 묶음(20개) 판정 → 결과. 부를 때마다 한 묶음씩 — 다시 부르면 이어서.
    python -X utf8 tools/modelgrade.py report --dir data/judge_runs/pilot10b
        판정 결과 폴더의 모델별 PASS·FAIL·보류와 시간.
    python -X utf8 tools/modelgrade.py held --dir data/judge_runs/pilot10b
        보류 항목·오류를 <폴더>/held.csv 하나로 (사유별 집계를 함께 찍음, gold 열에 사람이 PASS/FAIL) — tools/export_held.py.

규칙: 답 못 받은 질문은 FAIL(분모 유지), 빈 출력 요구 질문은 보류. 비밀번호·키는 다루지 않는다.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

IMPORT_METHOD = "manual-web-csv-v1"
from tools.providers import family  # noqa: E402 — AIONA 두 번째 자리(aiona2)도 AIONA 로 (10-26)
RUNS = ROOT / "data" / "comparison_runs"
DEFAULT_SET = ROOT / "참고자료" / "질문세트_78"   # 10-20 미팅: 질문 78개 고정 (옛 326 세트는 --set 으로)
JUDGE_BATCH_MAX = 20   # 사용자 규칙 2026-10-19: 평가는 10~20개씩
LABELS = {"claude": "Claude 웹브라우저", "gemini": "Gemini 웹브라우저", "gpt": "ChatGPT 웹브라우저", "aiona": "AIONA 웹브라우저", "aiona2": "AIONA 웹브라우저"}   # collect import(사람이 웹에서 받은 CSV)
APP_LABELS = {"gpt": "ChatGPT 웹 (GPT-6 · Medium)", "claude": "Claude 웹 (Sonnet 5.5 · 노력 중간)",
              "gemini": "Gemini 웹 (3.8 Flash · 사고 보통)", "aiona": "AIONA 웹브라우저", "aiona2": "AIONA 웹브라우저"}   # aiona2 = AIONA 두 번째 자리(10-26)   # collect app(자동 수집: 데스크톱 앱 셋 + 웹 하나, Gemini 앱 10-21)
ALL_PROVIDERS = ["gpt", "claude", "gemini", "aiona"]
#: 시작할 때 고르는 앱 모델 — 앱 화면의 모델 목록 그대로 (사용자 10-22). 하나를 고르면 그 앱을 그 모델로 바꿔 받고 비교 상대는 AIONA 하나
#: (10-22 오전 미팅: 같은 모델끼리 비교 · 다른 앱은 받지 않음). 노력 · 사고 · 추론 수준은 지금 설정 그대로(노력 중간 · 사고 보통 · Medium).
APP_MODELS = {
    "claude": ["Opus 5.5", "Fable 5.1", "Sonnet 5.5", "Haiku 5.5", "Haiku 4.5", "Sonnet 5", "Opus 5", "Fable 5", "Opus 4.8", "Opus 4.7", "Opus 4.6", "Sonnet 4.6"],
    "gpt": ["GPT-6", "GPT-5.6 Sol"],
    "gemini": ["3.5 Flash-Lite", "3.8 Flash", "3.1 Pro", "4 Argon"],
}
APP_DEFAULT_MODEL = {"claude": "Sonnet 5.5", "gpt": "GPT-6", "gemini": "3.8 Flash"}
APP_NAMES = {"claude": "Claude 웹", "gpt": "ChatGPT 웹", "gemini": "Gemini 웹"}   # 10-23: 네 모델 모두 웹(수집용 Chrome)
APP_LEVEL = {"claude": "노력 중간", "gpt": "Medium", "gemini": "사고 보통"}
#: AIONA 모델 ↔ 앱 모델 짝 (사용자 10-22) — 왼쪽은 AIONA 모델 메뉴에 보이는 이름 그대로, 오른쪽은 그 AIONA 모델과 일치하는 앱 모델(앱 메뉴 이름 그대로).
#: 시작할 때는 이 표에 있는 앱 모델만 고를 수 있고, 고르면 앱은 오른쪽 모델로 · AIONA 는 왼쪽 모델로 바꿔 받는다.
#: 추론 수준은 AIONA 에서 바꿀 수 없어 앱만 지금 설정(노력 중간 · Medium · 사고 보통).
AIONA_PAIRS = [
    # (AIONA 모델,              앱,        앱 모델)
    ("Claude Opus 5.5",         "claude", "Opus 5.5"),
    ("Claude Fable 5.1",        "claude", "Fable 5.1"),
    ("Claude Sonnet 5.5",       "claude", "Sonnet 5.5"),
    ("Claude Haiku 5.5",        "claude", "Haiku 5.5"),
    ("Claude Haiku 4.5",        "claude", "Haiku 4.5"),
    ("Claude Sonnet 5",         "claude", "Sonnet 5"),
    ("Claude Opus 5",           "claude", "Opus 5"),
    ("Claude Fable 5",          "claude", "Fable 5"),
    ("Claude Opus 4.8",         "claude", "Opus 4.8"),
    ("Claude Opus 4.7",         "claude", "Opus 4.7"),
    ("Claude Opus 4.6",         "claude", "Opus 4.6"),
    ("Claude Sonnet 4.6",       "claude", "Sonnet 4.6"),
    ("GPT-6 Sol",               "gpt",    "GPT-6"),         # ChatGPT 앱 'GPT-6' = AIONA 'GPT-6 Sol' (사용자 10-22)
    ("GPT-5.6 Sol",             "gpt",    "GPT-5.6 Sol"),
    ("Gemini 3.5 Flash-Lite",   "gemini", "3.5 Flash-Lite"),
    ("Gemini 3.8 Flash",        "gemini", "3.8 Flash"),
    ("Gemini 3.1 Pro Preview",  "gemini", "3.1 Pro"),       # Gemini 앱 '3.1 Pro' = AIONA 'Gemini 3.1 Pro Preview' (사용자 10-22)
    ("Gemini 4 Argon",          "gemini", "4 Argon"),
]
AIONA_MODEL_FOR = {(app, model): aiona for aiona, app, model in AIONA_PAIRS}   # (앱, 앱 모델) → AIONA 모델


def aiona_model_for(provider: str, model: str) -> str:
    """고른 앱 모델과 짝인 AIONA 모델 (AIONA_PAIRS)."""
    name = AIONA_MODEL_FOR.get((provider, model))
    if not name:
        raise SystemExit(f"'{model}' 은 AIONA 모델 짝 표(AIONA_PAIRS)에 없어 고를 수 없습니다")
    return name


def app_label(provider: str, model: Optional[str] = None) -> str:
    """결과에 남길 모델 이름 — 'Claude 웹 (Opus 5.5 · 노력 중간)'. 모델을 안 고르면 지금 기본 설정(APP_LABELS 와 같은 글)."""
    if provider not in APP_MODELS:
        return f"{APP_LABELS.get(provider, provider)} ({model})" if model else APP_LABELS.get(provider, provider)   # AIONA 두 자리는 'AIONA 웹브라우저 (모델)'
    return f"{APP_NAMES[provider]} ({model or APP_DEFAULT_MODEL[provider]} · {APP_LEVEL[provider]})"


def model_menu() -> List[Tuple[str, str, str]]:
    """시작 메뉴의 모델 번호 — [(번호, 앱, 모델)]. 0 은 전체라 넣지 않는다. 순서: Claude · ChatGPT · Gemini 앱 화면 목록 순."""
    out, n = [], 1
    for provider in ("claude", "gpt", "gemini"):
        for model in APP_MODELS[provider]:
            if (provider, model) in AIONA_MODEL_FOR:   # AIONA 모델 짝 표에 있는 것만 (10-22)
                out.append((str(n), provider, model))
                n += 1
    return out


def parse_model(text: str) -> Optional[Tuple[str, str]]:
    """'0' · '전체' · '' → None(전체), 메뉴 번호, 'claude:Opus 5.5', 'Opus 5.5'(앱이 하나로 정해질 때) → (앱, 모델). 모르는 값은 SystemExit."""
    t = (text or "").strip()
    if t in ("", "0", "전체", "all"):
        return None
    for key, provider, model in model_menu():
        if t == key:
            return provider, model
    if ":" in t:
        provider, model = (x.strip() for x in t.split(":", 1))
        if (provider, model) in AIONA_MODEL_FOR:
            return provider, model
    hits = [(p, m) for _k, p, m in model_menu() if m.lower() == t.lower()]
    if len(hits) == 1:
        return hits[0]
    raise SystemExit(f"고를 수 없는 모델입니다: {text!r} — AIONA 모델 짝 표에 있는 앱 모델만, 메뉴 번호 · '앱:모델'(예 claude:Opus 5.5) 또는 0(전체)")


PLACE_BY_PROVIDER = {"claude": "left", "gpt": "right", "gemini": "top-right", "aiona": "bottom-right", "aiona2": "left"}   # 동시 수집 창 자리(webapp.cdp_chat_web.PLACE_RECTS)
SIMPLE_IDS_FILE = ROOT / "참고자료" / "질문세트_최소78" / "ids.json"
RESULTS_JSON_DIR = ROOT / "참고자료" / "결과"   # report 가 판정 결과 JSON 을 쓰는 곳 (git 에 남김, 10-20)   # 간단평가 = 정밀 326 중 이 번호들, 이 순서로


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
    cfg.enabled = family(provider) == "aiona"
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


META_KEYS = ("run_id", "name", "created_at", "question_set", "eval_kind", "run_kind", "providers", "question_count", "model_choice")


def run_meta(run: Dict[str, Any]) -> Dict[str, Any]:
    return {k: run.get(k) for k in META_KEYS}


def save_run(run: Dict[str, Any], backup: bool = True) -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    path = RUNS / f"{run['run_id']}.json"
    if backup and path.exists():
        shutil.copyfile(path, path.with_suffix(f".json.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    for attempt in range(20):   # Windows: 다른 프로세스가 실행 파일을 읽는 동안에는 바꿔치기가 거부된다 — 잠깐 기다렸다 다시 (10-26 밤 ChatGPT 수집기가 이것으로 멈춤)
        try:
            tmp.replace(path)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.5)
    (RUNS / f"{run['run_id']}.meta.json").write_text(json.dumps(run_meta(run), ensure_ascii=False), encoding="utf-8")   # 실행 찾기용 작은 색인
    return path


def run_metas() -> List[Dict[str, Any]]:
    """실행마다 작은 메타(.meta.json) — 없으면 한 번 큰 파일을 읽어 만들어 둔다(실행 JSON 은 수십 MB 라 매번 다 읽으면 20초, 2026-10-20)."""
    out = []
    for f in sorted(RUNS.glob("*.json")):
        if f.name.endswith(".meta.json"):
            continue
        meta_path = f.with_name(f.stem + ".meta.json")
        try:
            if meta_path.exists() and meta_path.stat().st_mtime >= f.stat().st_mtime:
                out.append(json.loads(meta_path.read_text(encoding="utf-8")))
                continue
            r = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(r, dict) and r.get("run_id"):
                meta = run_meta(r)
                meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                out.append(meta)
        except Exception:
            continue
    return out


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def new_run(name: str, set_dir: Path, groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    from webapp.question_generation import flatten
    return {"run_id": uuid.uuid4().hex[:12], "name": name, "created_at": _now(), "status": "COLLECT", "phase": "COLLECT", "providers": [],
            "models": {}, "datasets": {}, "evaluations": {}, "question_count": len(groups), "total": len(flatten(groups)),
            "question_set": {"set_id": json.loads((set_dir / "set.json").read_text(encoding="utf-8")).get("id") if (set_dir / "set.json").exists() else set_dir.name,
                             **({"dir": Path(os.path.relpath(Path(set_dir).resolve(), ROOT)).as_posix()} if _inside(Path(set_dir), ROOT) else {})},   # 보고서가 세트 폴더를 찾게 (10-26 저녁)
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


# --- collect web (수집용 Chrome 의 사이트 창에서 받기) --------------------------------------------------------------

def web_chat_class(provider: str):
    """모델 → 웹 수집기(모두 tools/chrome_cdp.py 의 수집용 Chrome 에 붙는다, 10-23 확정): gpt = chatgpt.com · claude = claude.ai · gemini = gemini.google.com
    (webapp.cdp_chat_web) · aiona = AIONA 웹(webapp.aiona_web, 같은 Chrome 에 붙음). 시험에서는 monkeypatch 로 가짜를 넣는다."""
    if provider == "gpt":
        from webapp.cdp_chat_web import ChatGptChat
        return ChatGptChat
    if provider == "claude":
        from webapp.cdp_chat_web import ClaudeChat
        return ClaudeChat
    if provider == "gemini":
        from webapp.cdp_chat_web import GeminiChat
        return GeminiChat
    if family(provider) == "aiona":   # aiona2(두 번째 AIONA 자리)도 AIONA 수집기 — 모델마다 탭을 따로 차지해 섞이지 않음
        return aiona_collector
    raise SystemExit(f"{provider} 는 자동 수집기가 없습니다 ({', '.join(APP_LABELS)} 만). 웹 수집 CSV 를 collect import 로 올리세요.")


def aiona_collector(*, debug_dir: Optional[Path] = None, place: Optional[str] = None, cancelled: Any = None, answer_timeout_s: int = 600,
                    model: Optional[str] = None, **_: Any):
    """AIONA 웹브라우저 수집기(webapp.aiona_web.AionaChat)를 다른 웹 수집기와 같은 인자로 — 수집용 Chrome 에 붙어(attach) 자기 창을 쓴다.
    로그인은 .env 의 AIONA_LOGIN_ID·PASSWORD 로 수집기가 하되, AIONA_AUTO_LOGIN=0 이면 안 한다(로그인이 풀렸으면 오류로 알림)."""
    from code_eval.llm_client import env_value
    from webapp.aiona_web import AionaChat
    return AionaChat(headless=False, allow_login=(env_value("AIONA_AUTO_LOGIN") or "1") != "0", answer_timeout_s=answer_timeout_s,
                     debug_dir=debug_dir, cancelled=cancelled, place=place, attach=True, model=model)


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
    """수집용 Chrome 의 사이트 창에서 질문 묶음의 답을 받아 실행 JSON 에 올리고 CODE 평가까지. --provider 를 쉼표로 여럿 주면 동시에(모델마다 자식 프로세스,
    창은 자리 고정). 대화는 채팅 하나에서 1턴부터 차례로, 단일·긴 문서는 새 채팅. 이미 답을 받은 질문(모든 턴 SUCCESS)은 건너뛴다."""
    providers = split_providers(args.provider, APP_LABELS)
    if len(providers) == 1:
        return collect_app_one(argparse.Namespace(**{**vars(args), "provider": providers[0]}))
    run_id = args.run
    if not run_id:   # 자식들이 같은 실행에 쓰도록 먼저 만든다
        set_dir = Path(args.set)
        groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
        run_id = save_run(new_run(args.new or f"웹 수집 {datetime.now():%Y-%m-%d}", set_dir, groups)).stem
        print(f"새 실행 {run_id}", flush=True)
    return run_collectors_parallel(providers, args, run_id)


PLACES = ("left", "right")


SHORT_NAMES = {"gpt": "ChatGPT", "claude": "Claude", "gemini": "Gemini"}
STATUS_KO = {"SUCCESS": "받음", "COPY_FAILED": "복사 못 함", "ERROR": "오류", "TIMEOUT": "시간 초과", "LIMIT": "사용 한도"}


def display_name(provider: str, model_label: Optional[str] = None) -> str:
    """터미널에 보일 모델 이름 — 'GPT' · 'Gemini' · 'AIONA' · 'AIONA · Claude Sonnet 5.5'(AIONA 에서 고른 모델)."""
    from tools.providers import short_label
    if family(provider) == "aiona":
        return short_label(model_label or LABELS.get(provider), provider)
    m = re.match(r"^.*?\((.+?) · ", model_label or "")   # 'ChatGPT 웹 (GPT-5.6 Sol · Medium)' — 기본이 아닌 모델을 골랐으면 이름에 (10-26 밤)
    picked = m.group(1) if m else None
    return SHORT_NAMES.get(provider, provider) + (f" · {picked}" if picked and picked != APP_DEFAULT_MODEL.get(provider) else "")


def brief_console() -> bool:
    """간단히 보일지 — start 가 MODELGRADE_CONSOLE=brief 로 켠다(자식 수집기 · 판정 도구도 같은 값을 물려받음)."""
    return os.environ.get("MODELGRADE_CONSOLE") == "brief"


def text_width(text: str) -> int:
    """터미널 칸 수 — 한글 같은 넓은 글자는 2칸."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def pad(text: Any, width: int, right: bool = False) -> str:
    gap = " " * max(0, width - text_width(str(text)))
    return gap + str(text) if right else str(text) + gap


def print_table(head: List[str], rows: List[List[Any]], indent: str = "  ") -> None:
    """한글 폭을 맞춘 표 — 첫 칸은 왼쪽, 나머지는 오른쪽 맞춤."""
    widths = [max(text_width(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    for r in [head] + rows:
        print(indent + "  ".join(pad(c, widths[i], right=i > 0) for i, c in enumerate(r)).rstrip(), flush=True)


def rel(path: Any) -> str:
    """저장소 안 경로는 짧게(저장소 기준) — 밖이면 그대로."""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


def brief_collect_line(line: str, state: Dict[str, Any]) -> Optional[str]:
    """수집기 한 줄을 간단히 — 싱글턴은 질문마다 한 줄('✔ Q11 [1/2] 352자 · 12초'), 멀티턴은 대화 줄 아래 턴 줄. 세부(CODE · 저장 경로)는 숨김(로그 파일엔 그대로)."""
    import re
    s = line.rstrip()
    m = re.match(r"^\S+ · .*: 이번 묶음 질문 (\d+)개 .*이미 받은 질문 (\d+)개", s)
    if m:
        return f"질문 {m.group(1)}개 받기" + (f" (이미 받은 {m.group(2)}개 건너뜀)" if m.group(2) != "0" else "")
    if s.startswith("받을 질문이 없습니다"):
        return "받을 질문 없음 — 모두 받음"
    m = re.match(r"^▶ Q(\d+) \((\d+)턴\) — 이번 묶음 (\d+)/(\d+)", s)
    if m:
        state["q"] = (m.group(1), int(m.group(2)), m.group(3), m.group(4))
        return None if m.group(2) == "1" else f"Q{m.group(1)} — {m.group(2)}턴 [{m.group(3)}/{m.group(4)}]"
    m = re.match(r"^\s+Q(\d+)_T(\d+): (\S+) (\d+)자 (\d+)초 \([^)]*\)(?: — (.*))?$", s)
    if m:
        q, t, st, chars, secs, err = m.groups()
        mark = "✔" if st == "SUCCESS" else f"✘ {STATUS_KO.get(st, st)}"
        tail = f" — {err[:80]}" if err and st != "SUCCESS" else ""
        cur = state.get("q")
        if cur and cur[0] == q and cur[1] == 1:
            return f"{mark} Q{q} [{cur[2]}/{cur[3]}] {chars}자 · {secs}초{tail}"
        return f"   {mark} {t}턴 {chars}자 · {secs}초{tail}"
    if s.startswith("CODE 평가 중"):
        return None
    m = re.match(r"^저장: .* 받은 질문 (\d+)/(\d+)", s)
    if m:
        return f"끝 — 받은 질문 {m.group(1)}/{m.group(2)}"
    return s.strip() or None


def board_collect_line(board: Any, provider: str, name: str, line: str, state: Dict[str, Any]) -> None:
    """수집기 한 줄로 진행 판 갱신 — 시작(전체 · 이미 받은 수) · 질문 시작 · 턴 끝(마지막 턴이나 실패면 한 질문 끝) · 저장. 실패 · 모르는 글은 판 위에 한 줄."""
    import re
    s = line.rstrip()
    m = re.match(r"^\S+ · .*: 이번 묶음 질문 (\d+)개 .*전체 (\d+)개 중 이미 받은 질문 (\d+)개", s)
    if m:
        board.update(provider, done=int(m.group(3)), total=int(m.group(2)), note="받는 중")
        return
    if s.startswith("받을 질문이 없습니다"):
        board.update(provider, done=board.totals.get(provider, 0), note="모두 받음")
        return
    m = re.match(r"^▶ Q(\d+) \((\d+)턴\)", s)
    if m:
        state["q"] = (m.group(1), int(m.group(2)))
        board.update(provider, note=f"Q{m.group(1)} 받는 중" + (f" (1/{m.group(2)}턴)" if m.group(2) != "1" else ""))
        return
    m = re.match(r"^\s+Q(\d+)_T(\d+): (\S+) (\d+)자 (\d+)초 \([^)]*\)(?: — (.*))?$", s)
    if m:
        q, t, st, _chars, _secs, err = m.groups()
        turns = (state.get("q") or (q, 1))[1]
        if st != "SUCCESS":
            state["fail"] = state.get("fail", 0) + 1
            board.log(f"  {name}  ✘ Q{q}{f' {t}턴' if turns > 1 else ''} {STATUS_KO.get(st, st)}" + (f" — {err[:80]}" if err else ""))
        if st != "SUCCESS" or int(t) >= turns:
            board.update(provider, add=1, note=f"Q{q} 끝" + (f" · 실패 {state['fail']}" if state.get("fail") else ""))
        else:
            board.update(provider, note=f"Q{q} 받는 중 ({int(t) + 1}/{turns}턴)")
        return
    if s.startswith("CODE 평가 중"):
        board.update(provider, note="CODE 검사 중")
        return
    if s.startswith("저장: "):
        board.update(provider, note="끝" + (f" · 실패 {state['fail']}" if state.get("fail") else ""))
        return
    if s.strip():
        board.log(f"  {name}  {s.strip()}")


def run_collectors_parallel(providers: List[str], args: argparse.Namespace, run_id: str) -> int:
    """모델마다 자식 프로세스(이 CLI 의 collect web --provider 하나)를 띄워 같이 돌리고 출력을 [모델] 머리말로 섞어 보여 준다 — 모두 같은 수집용 Chrome 에 붙는다."""
    import subprocess
    import threading
    procs = []
    for i, provider in enumerate(providers):
        cmd = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "collect", "web", "--provider", provider, "--run", run_id,
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
        if (getattr(args, "models", None) or {}).get(provider):   # 시작할 때 고른 앱 모델 (10-22)
            cmd += ["--model", args.models[provider]]
        procs.append((provider, subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT))))

    log_dir = ROOT / "data" / "judge_runs" / run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    names = {p: display_name(p, app_label(p, (getattr(args, "models", None) or {}).get(p))) for p in providers}
    width = max(text_width(n) for n in names.values())
    total = len([x for x in str(args.only or "").split(",") if x.strip()]) or int(args.next or 0)
    board = None
    if brief_console():   # 진행 판 — 모델마다 '받은 수 / 전체 (남은 n) · Q13 받는 중' (사용자 10-26 저녁)
        from tools.live_board import LiveBoard
        board = LiveBoard(names, {p: total for p in providers})
        board.start()

    def pump(provider: str, proc: Any) -> None:
        # 로그 파일에는 수집기 글 그대로 — 한 모델이 조용히 끝나도 나중에 원인을 볼 수 있게 (10-20 밤: AIONA 가 왜 빠졌는지 로그가 없었다).
        # 터미널은 간단히(start)면 진행 판, 아니면 수집기 글 그대로 — 머리는 보이는 이름 (10-26 저녁)
        state: Dict[str, Any] = {}
        with (log_dir / f"collect-{stamp}-{provider}.log").open("a", encoding="utf-8") as f:
            for line in proc.stdout:
                f.write(line)
                if board is not None:
                    board_collect_line(board, provider, names[provider], line, state)
                else:
                    print(f"  {pad(names[provider], width)}  {line.rstrip()}", flush=True)
            f.write(f"[종료 코드 {proc.wait()}]" + chr(10))

    threads = [threading.Thread(target=pump, args=(p, proc), daemon=True) for p, proc in procs]
    for t in threads:
        t.start()
    codes = {p: proc.wait() for p, proc in procs}
    for t in threads:
        t.join(timeout=5)
    if board is not None:
        board.close()
    bad = [p for p, c in codes.items() if c]
    if bad:
        print(f"실패한 수집기: {', '.join(bad)} (종료 코드 {codes})", flush=True)
        return 1
    if not brief_console():
        print(f"동시 수집 끝 — 실행 {run_id}", flush=True)
    return 0


def merge_provider_into_run(run_id: str, provider: str, label: str, merged: List[Dict[str, Any]], evaluation: Optional[Dict[str, Any]],
                            import_entry: Optional[Dict[str, Any]], backup: bool = True) -> Path:
    """실행 JSON 을 잠금 아래 다시 읽어 이 모델 자리만 바꿔 저장 — 다른 앱의 자식 프로세스가 같은 파일을 동시에 쓴다.
    evaluation·import_entry 가 None 이면 그 자리는 그대로(질문마다 중간 저장할 때)."""
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
        if evaluation is not None:
            run.setdefault("evaluations", {})[provider] = evaluation
        if import_entry is not None:
            run.setdefault("imports", []).append(import_entry)
        return save_run(run, backup=backup)
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


LIMIT_WAIT_S = 15 * 60        # 사용량 한도 오류 — 이만큼 기다렸다 같은 턴을 다시 (ChatGPT '한도에 도달' 로 34개 못 받음, 2026-10-20)
LIMIT_WAIT_MAX_S = 6 * 3600   # 이보다 오래 막혀 있으면 포기(ERROR 로 남겨 다음 실행에서 다시)
LIMIT_WORDS = ("사용량 한도로 막혀", "usage limit")   # 10-23: 수집기가 "사용량 한도로 막혀 있습니다" 라고 할 때만 기다린다(봇 확인 · 대화 글의 '한도' 는 아님)
COPY_FAILED = "COPY_FAILED"   # 답은 있었는데 복사해 오지 못함(재시도 뒤) — 판정은 보류 (사용자 규칙 2026-10-20)


def retry_text_capture(chat: Any, row: Dict[str, Any], res: Dict[str, Any]) -> Dict[str, Any]:
    """복사 버튼 대신 화면 글(text)로 받은 답: 복사만 다시 시도하고, 그래도 안 되면 COPY_FAILED(보류). 받은 화면 글은 note 로 남긴다."""
    print(f"  {row['case_id']}: 복사 버튼이 안 눌려 화면 글만 받음 — 복사 다시 시도", flush=True)
    again = getattr(chat, "recopy", None)
    text = (again() if callable(again) else "") or ""
    if text.strip():
        return {**res, "answer": text, "capture": "copy-button(재시도)", "retried": "복사 다시"}
    return {**res, "answer": "", "status": COPY_FAILED, "capture": "text",
            "error": f"복사 버튼이 안 눌려 화면 글({len(res.get('answer') or '')}자)만 받음 — 복사를 다시 시도했으나 실패, 보류(사용자 규칙 10-20)",
            "screen_text": (res.get("answer") or "")[:20000], "retried": "실패"}


def retry_empty_answer(chat: Any, row: Dict[str, Any]) -> Dict[str, Any]:
    """빈 출력 요구가 아닌 질문에 빈 답이 왔을 때(사용자 규칙 10-20): 복사만 다시 시도하고, 그래도 비면 빈 응답(에러 케이스, 사용자 10-26)으로 적는다."""
    print(f"  {row['case_id']}: 답이 비어 있음 — 복사 다시 시도", flush=True)
    again = getattr(chat, "recopy", None)
    text = (again() if callable(again) else "") or ""
    if text.strip():
        return {"answer": text, "status": "SUCCESS", "capture": "copy-button(재시도)", "error": None, "latency_ms": 0, "url": None, "sent": [],
                "model": getattr(chat, "model_seen", None), "method": getattr(chat, "METHOD", None), "retried": "복사 다시"}
    # 복사를 다시 해도 비면 모델이 빈 응답을 낸 것 — 에러 케이스(대분류 0, 사용자 10-26 "빈응답출력하는 사례도 에러케이스")
    return {"answer": "", "status": "SUCCESS", "capture": "empty", "latency_ms": 0, "url": None, "sent": [], "error": None,
            "model": getattr(chat, "model_seen", None), "method": getattr(chat, "METHOD", None), "retried": "복사 다시(여전히 빈 응답)",
            "note": "빈 응답 — 복사를 다시 해도 비어 있음 → 에러 케이스(사용자 10-26)"}


def collect_app_one(args: argparse.Namespace) -> int:
    import tempfile
    from webapp.question_generation import flatten
    set_dir = Path(args.set)
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    rows_all = flatten(groups)
    model = getattr(args, "model", None)
    if model and family(args.provider) != "aiona" and model not in APP_MODELS.get(args.provider, []):   # AIONA 는 메뉴 이름 그대로 받음
        raise SystemExit(f"{args.provider} 에는 모델 {model!r} 을 고를 수 없습니다 ({', '.join(APP_MODELS.get(args.provider, [])) or '고를 모델 없음'})")
    label = args.label or app_label(args.provider, model)
    run = load_run(args.run) if args.run else new_run(args.new or f"웹 수집 {datetime.now():%Y-%m-%d}", set_dir, groups)
    existing = {r.get("case_id"): r for r in (run.get("datasets") or {}).get(args.provider) or [] if isinstance(r, dict)}
    by_q: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows_all:
        by_q.setdefault(int(r["case_id"].split("_T")[0][1:]), []).append(r)
    done = {q for q, turns in by_q.items() if all((existing.get(t["case_id"]) or {}).get("collection", {}).get("status") in ("SUCCESS", "COPY_FAILED") for t in turns)}   # 복사 실패(보류)는 자동으로 다시 받지 않는다 — --only 로 지정하면 다시
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
    chat_cls = web_chat_class(args.provider)
    new_rows: Dict[str, Dict[str, Any]] = {}
    started_wall = time.monotonic()
    from webapp.run_summary import asks_empty_output

    def merged_rows() -> List[Dict[str, Any]]:
        """질문 세트 순서로 — 새 행이 있으면 바꾸고 없으면 전 행(또는 답 없음) 유지."""
        out = []
        for index, r in enumerate(rows_all):
            row = new_rows.get(r["case_id"]) or existing.get(r["case_id"])
            if row is None:
                row = {"case_id": r["case_id"], "question": r.get("question") or "", "answer": "", "system_id": args.provider, "source": label, "case_index": index,
                       "collection": {"status": "ERROR", "error": "아직 받지 않음", "method": "pending"}}
                for key in ("reference", "attachments", "conversation_id", "turn"):
                    if r.get(key) not in (None, "", []):
                        row[key] = r[key]
            out.append(row)
        return out

    backed_up = [False]

    def checkpoint() -> None:
        """질문 하나를 받을 때마다 실행 파일에 저장 — 중간에 멈춰도 받은 답이 남는다(10-20). 백업은 프로세스당 한 번만(파일이 수십 MB)."""
        if not args.run:
            return
        try:
            merge_provider_into_run(run["run_id"], args.provider, label, merged_rows(), None, None, backup=not backed_up[0])
            backed_up[0] = True
        except (SystemExit, OSError) as exc:   # 다음 턴에 다시 저장한다 — 중간 저장 실패로 수집기가 죽지 않게 (10-26 밤)
            print(f"  (중간 저장 실패: {type(exc).__name__}: {exc})", flush=True)
    from code_eval.llm_client import env_value
    answer_timeout = int(env_value("COLLECT_ANSWER_TIMEOUT") or 0)   # 답변 대기 제한(초), 기본 0 = 제한 없음 (사용자 지시 10-20: 제한 시간 해제)
    with chat_cls(debug_dir=ROOT / "data" / "judge_runs" / "collect-debug", place=getattr(args, "place", None), answer_timeout_s=answer_timeout,
                  **({"model": model} if model else {})) as chat,             tempfile.TemporaryDirectory(prefix="mg-att-") as tmpd:
        got = len(done)
        credit_out = [False]
        for i, q in enumerate(wanted, 1):
            turns = sorted(by_q[q], key=lambda r: int(r.get("turn") or 1))
            print(f"▶ Q{q} ({len(turns)}턴) — 이번 묶음 {i}/{len(wanted)} · 전체 {got + 1}/{total}", flush=True)
            q_started = time.monotonic()   # 질문마다 실제 걸린 시간 — 새 채팅 · 모델 맞추기 · 자료 붙여 넣기 · 답 기다림 · 복사까지 (사용자 10-26 밤)
            try:
                chat.new_chat()
            except Exception as exc:
                print(f"  Q{q}: 새 채팅 실패 — {exc}", flush=True)
                continue
            for r in turns:
                turn_started = q_started if r is turns[0] else time.monotonic()   # 1턴은 새 채팅을 연 때부터
                files = materialize_attachments(r, set_dir, Path(tmpd))
                res = chat.ask(r.get("question") or "", files=files, reference=r.get("reference"))
                waited = 0
                while res.get("status") != "SUCCESS" and any(w in (res.get("error") or "").lower() for w in LIMIT_WORDS) and waited < LIMIT_WAIT_MAX_S:
                    print(f"  {r['case_id']}: 사용량 한도로 막힘 — {LIMIT_WAIT_S // 60}분 기다렸다 다시 (누적 {waited // 60}분) · {res.get('error', '')[:80]}", flush=True)
                    time.sleep(LIMIT_WAIT_S)
                    waited += LIMIT_WAIT_S
                    res = chat.ask(r.get("question") or "", files=files, reference=r.get("reference"))
                if res.get("status") == "SUCCESS" and not (res.get("answer") or "").strip() and not asks_empty_output(None, r.get("question") or ""):
                    res = retry_empty_answer(chat, r)   # 사용자 규칙 10-20: 복사만 다시 시도 → 그래도 비면 빈 응답 = 에러 케이스 (10-26)
                elif res.get("status") == "SUCCESS" and res.get("capture") in ("text", "html"):   # html = AIONA 복사 버튼을 못 받아 화면 HTML 로 바꾼 글 (10-21 밤)
                    res = retry_text_capture(chat, r, res)   # 복사 버튼이 안 눌려 화면 글만 받음 → 복사 다시 → 안 되면 보류(마크다운이 빠진 글로 판정하지 않는다)
                elif res.get("status") == "ERROR" and asks_empty_output(None, r.get("question") or ""):
                    # 빈 출력 요구 질문에 모델이 오류('생각을 중단했습니다 · 처리 중 오류')를 냄 — 판정은 보류이고 다시 던져도 같으므로 보류(COPY_FAILED)로 적어
                    # 다음 실행에서 또 묻지 않는다 (10-20 밤: AIONA 가 같은 5개를 매번 다시 받음)
                    res = {**res, "status": COPY_FAILED, "capture": "none",
                           "error": f"빈 출력 요구 — 모델 오류로 답 없음 → 보류 (사용자 규칙 10-20) · {res.get('error') or ''}"[:300]}
                read_log = getattr(chat, "_turn_log", None)
                if not res.get("turn_log") and callable(read_log):
                    # 답이 비었거나 못 받음 — 그 순간 답 자리에 보이는 글을 남겨 결과 보고서 보류에 글 로그로 보인다 (사용자 10-26 저녁, 이미지 대신).
                    # 받은 답이 오류 · 거절 문구('언어 모델이라 할 수 없습니다' · '응답을 중지했습니다')여도 — 화면에만 뜬 오류 띠를 함께 남긴다 (10-26 밤)
                    from tools.collection_log import answer_problem
                    if not (res.get("answer") or "").strip():
                        res = {**res, "turn_log": read_log("no-answer")}
                    elif res.get("status") == "SUCCESS" and answer_problem(res.get("answer") or "", r.get("question") or ""):
                        res = {**res, "turn_log": read_log("error-like-answer")}
                col = {k: v for k, v in res.items() if k != "answer" and not (k == "turn_log" and v is None)}
                col["collected_at"] = _now()
                col["turn_seconds"] = round(time.monotonic() - turn_started, 1)   # 이 턴에 실제로 걸린 시간(답 기다린 시간 latency_ms 와 따로)
                row = {"case_id": r["case_id"], "question": r.get("question") or "", "answer": res.get("answer") or "" if res.get("status") == "SUCCESS" else "",
                       "system_id": args.provider, "source": label, "case_index": rows_all.index(r), "collection": col}
                for key in ("reference", "attachments", "conversation_id", "turn"):
                    if r.get(key) not in (None, "", []):
                        row[key] = r[key]
                new_rows[r["case_id"]] = row
                print(f"  {r['case_id']}: {res.get('status')} {len(res.get('answer') or '')}자 {res.get('latency_ms', 0) // 1000}초 ({res.get('capture')})" + (f" — {res.get('error')}" if res.get("error") else ""), flush=True)
                checkpoint()   # 턴마다 저장 — 10턴 대화 중간에 멈춰도 받은 턴이 남는다 (10-20 밤: AIONA 가 Q1 10턴을 받고도 저장 전에 끝나 모두 잃음)
                if res.get("status") == "LIMIT" and "월간 크레딧" in (res.get("error") or ""):
                    # 기다려도 풀리지 않는 한도(월간 크레딧) — 남은 질문을 보내 봐야 같은 문구라 이 모델 수집을 멈춘다. 충전 뒤 같은 명령이면 여기서부터 (10-27 새벽)
                    print(f"  {r['case_id']}: {res['error']} — 이 모델 수집을 멈춥니다", flush=True)
                    credit_out[0] = True
                    break
                if res.get("status") != "SUCCESS" and len(turns) > 1:
                    print(f"  Q{q}: 턴이 실패해 이 대화의 나머지 턴은 보내지 않음", flush=True)
                    break
            if credit_out[0]:
                break
            if all(new_rows.get(t["case_id"], {}).get("collection", {}).get("status") == "SUCCESS" for t in turns):
                got += 1
    merged = merged_rows()
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
        path = merge_provider_into_run(run["run_id"], args.provider, label, merged, evaluation, entry, backup=not backed_up[0])   # 동시 수집: 다른 앱이 쓴 자리는 건드리지 않는다
    ok = sum(1 for r in merged if r["collection"].get("status") == "SUCCESS")
    rows_by_q: Dict[int, List[Dict[str, Any]]] = {}
    for r in merged:
        rows_by_q.setdefault(int(r["case_id"].split("_T")[0][1:]), []).append(r)
    done_q = sum(1 for rs in rows_by_q.values() if all(r["collection"].get("status") == "SUCCESS" for r in rs))
    print(f"저장: {path} (실행 {run['run_id']}) | {args.provider} 받은 질문 {done_q}/{total} ({ok}/{len(merged)}턴)", flush=True)
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
    """웹 수집(선택, --collect gpt,claude: 수집용 Chrome 에서 다음 묶음 받기) → 올리기(선택) → 다음 묶음 판정 → 결과.
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
    """평가할 질문 번호 — 세트의 모든 질문을 세트에 적힌 순서대로(질문을 다시 검사·선별하지 않는다, 사용자 10-20).
    옛 326 세트에 'simple78' 을 주면 최소78 ids.json 의 번호만."""
    if kind == "simple78":
        ids = json.loads(SIMPLE_IDS_FILE.read_text(encoding="utf-8"))["ids"]
        have = {int(g["id"]) for g in groups}
        return [int(q) for q in ids if int(q) in have]
    return [int(g["id"]) for g in groups]


def find_run_for(set_dir: Path, kind: str, model_choice: Optional[str] = None) -> Optional[str]:
    """같은 질문 세트·같은 평가 종류·같은 모델 선택(model_choice — 예 'claude:Opus 5.5', 전체는 None)으로 쓰던 실행(가장 최근).
    고른 모델이 다르면 다른 실행 — 다른 모델로 받은 답과 섞이지 않게 (10-22). 정밀평가는 CLI 로 만든 실행(AIONA 웹 답을 올린 것)도 이어 쓴다."""
    sid = set_id_of(set_dir)
    cands = [m for m in run_metas() if (m.get("question_set") or {}).get("set_id") == sid and m.get("model_choice") == model_choice]
    tagged = [m for m in cands if m.get("eval_kind") == kind]
    if tagged:
        return max(tagged, key=lambda m: m.get("created_at") or "")["run_id"]
    if kind == "full326":
        cli = [m for m in cands if not m.get("eval_kind") and m.get("run_kind") == "manual-web-import"]
        if cli:
            run = load_run(max(cli, key=lambda m: m.get("created_at") or "")["run_id"])
            run["eval_kind"] = kind
            save_run(run)
            return run["run_id"]
    return None


def ensure_collection_chrome() -> None:
    """수집용 Chrome(tools/chrome_cdp.py)을 띄우고 네 사이트 창을 연다 — 떠 있으면 그대로. 못 띄우면 안내와 함께 끝낸다."""
    from tools import chrome_cdp
    try:
        chrome_cdp.launch(open_sites=True)
    except Exception as exc:
        raise SystemExit(f"수집용 Chrome 을 띄우지 못했습니다: {exc} — python tools/chrome_cdp.py 로 확인하세요")


def cmd_start(args: argparse.Namespace) -> int:
    """start — 터미널은 간단히(MODELGRADE_CONSOLE=brief, 자식 수집기 · 판정 도구가 물려받음), 끝나면 원래 값으로 (10-26 저녁)."""
    prev = os.environ.get("MODELGRADE_CONSOLE")
    os.environ["MODELGRADE_CONSOLE"] = prev or "brief"
    try:
        return _cmd_start(args)
    finally:
        if prev is None:
            os.environ.pop("MODELGRADE_CONSOLE", None)
        else:
            os.environ["MODELGRADE_CONSOLE"] = prev


LIST_CHOICES = {"1": "기존 질문 리스트", "2": "새 질문 리스트 만들기", "3": "기존 질문 리스트 · 답변 새로 받기"}


def ask_list_choice(n_questions: int) -> str:
    """처음 고르기 — 기존 질문 리스트 / 새 질문 리스트 / 기존 리스트로 답변 새로 받기 (사용자 10-26 저녁)."""
    print("어떤 질문 리스트로 평가할까요?")
    print(f"  1. 기존 질문 리스트 ({n_questions}개)")
    print("  2. 새 질문 리스트 만들기")
    print("  3. 기존 질문 리스트 · 답변 새로 받기 (AIONA 가 업데이트됐을 때 등)")
    while True:
        try:
            choice = input("번호 입력 후 Enter: ").strip()
        except EOFError:
            raise SystemExit("입력이 없어 그만둡니다.")
        if choice in LIST_CHOICES:
            return choice
        print("1 · 2 · 3 중 하나를 입력하세요.")


def ask_existing_set(default: Path, n_default: int) -> Path:
    """기존 질문 리스트가 여럿이면(새 질문 리스트로 만든 것) 고르기 — 없으면 기본 세트 그대로."""
    from tools import new_question_set
    made = [m for m in new_question_set.made_sets() if m["dir"].resolve() != default.resolve()]
    if not made:
        return default
    choices = [(default, f"{default.name} ({n_default}개 · 기본)")] + [
        (m["dir"], f"{m['dir'].name} ({m['total']}개 · {m['created_at'][:10]} 만듦)") for m in made]
    print("어느 질문 리스트로 할까요?")
    for i, (_d, label) in enumerate(choices, 1):
        print(f"  {i}. {label}")
    while True:
        try:
            pick = input("번호 입력 후 Enter: ").strip()
        except EOFError:
            raise SystemExit("입력이 없어 그만둡니다.")
        if pick.isdigit() and 1 <= int(pick) <= len(choices):
            return choices[int(pick) - 1][0]
        print(f"1 ~ {len(choices)} 중 하나를 입력하세요.")


def run_refine(run_id: str, set_dir: Path, items: int) -> None:
    """평가 끝에 하네스2(기준 다듬기 루프)를 묻지 않고 이어서 (사용자 10-27 '평가가 완료되면 하네스2도 자동연계되게해서 실행시켜') —
    사람 판정 · 보류 · 심판 갈림이 많은 LLM 기준을 골라 고칠 안을 시험하고, 채택된 안은 판정 프롬프트 등에 넣는다(tools/refine_loop auto).
    하네스2가 막혀도 평가 결과 · 보고서는 그대로."""
    print(f"\n기준 다듬기(하네스2)를 시작합니다 — 기준 {items}개까지, 끝나면 반영한 것과 사람이 볼 제안을 알립니다.", flush=True)
    from tools import refine_loop
    try:
        refine_loop.main(["auto", "--run", run_id, "--judge", "v4", "--set", str(set_dir), "--items", str(items)])
    except (SystemExit, Exception) as exc:
        print(f"  ⚠ 하네스2 멈춤: {type(exc).__name__}: {exc} — 평가 결과 · 보고서는 그대로", flush=True)
    else:
        print("기준 다듬기가 끝났습니다. 반영한 것은 다음 평가부터 쓰입니다.", flush=True)


def make_new_question_set(args: argparse.Namespace) -> Path:
    """새 질문 리스트 — 질문생성 프롬프트로 Claude Code CLI 가 질문 · 첨부 · 긴 문서를 만들고 우리 검사기를 통과할 때까지 고친다
    (tools/new_question_set.py, 사용자 10-26 밤). 만들다 멈춘 리스트가 있으면 이어서."""
    from tools import new_question_set
    return new_question_set.make(getattr(args, "size", None) or "full")


def answered_questions(run: Dict[str, Any], provider: str, groups: List[Dict[str, Any]]) -> set:
    """그 모델이 답을 다 받은 질문 — 모든 턴이 받음이거나 복사 실패(보류로 판정, 수집기가 자동으로 다시 받지 않는 것과 같은 기준)."""
    from webapp.question_generation import flatten
    rows = {r.get("case_id"): r for r in (run.get("datasets") or {}).get(provider) or [] if isinstance(r, dict)}
    by_q: Dict[int, List[str]] = {}
    for r in flatten(groups):
        by_q.setdefault(int(r["case_id"].split("_T")[0][1:]), []).append(r["case_id"])
    return {q for q, cids in by_q.items() if all(((rows.get(c) or {}).get("collection") or {}).get("status") in ("SUCCESS", "COPY_FAILED") for c in cids)}


def preflight_web_login(providers: List[str]) -> None:
    """수집 전에 사이트 로그인 확인 — 로그인 화면이 남은 창이 있으면 그 창에서 로그인할 때까지 멈춘다(수집 중간에 실패하지 않게, 10-26 저녁).
    AIONA 는 .env 아이디가 있으면 수집기가 로그인하므로 넘어간다. 터미널이 아니면 알리기만."""
    from code_eval.llm_client import env_value
    from tools import setup_wizard
    want = {"chatgpt.com": "gpt", "claude.ai": "claude", "gemini.google.com": "gemini", "aiona": "aiona"}
    aiona_auto = bool(env_value("AIONA_LOGIN_ID") and env_value("AIONA_LOGIN_PASSWORD"))
    for _attempt in range(3):
        try:
            state = setup_wizard.web_login_state()
        except Exception:
            return   # 확인 자체가 안 되면 수집기가 알아서 — 여기서 막지 않는다
        left = []
        for url, st in state.items():
            p = next((v for k, v in want.items() if k in url), None)
            if p is None or not any(family(x) == p for x in providers) or st != "login" or (p == "aiona" and aiona_auto):
                continue
            left.append(display_name(p))
        if not left:
            return
        print(f"  ⚠ 로그인이 풀린 사이트: {' · '.join(left)} — 열린 Chrome 창에서 로그인해 주세요", flush=True)
        if not setup_wizard.interactive():
            return
        try:
            input("  로그인을 마쳤으면 Enter … ")
        except EOFError:
            return


def _cmd_start(args: argparse.Namespace) -> int:
    """실행 흐름(사용자 10-26 저녁): 질문 리스트 고르기(기존 / 새로 만들기 / 기존 + 답변 새로 받기) → 답이 있으면 '답변 목록이 있습니다.', 없거나 빠진 것은
    수집(모델마다 진행 판) → '평가를 시작합니다.' → 평가(모델마다 진행 판) → '평가가 완료되었습니다.' 와 report.html 위치 → 묻지 않고 하네스2(기준 다듬기, 10-27).
    이미 받은 질문 · 판정한 질문은 건너뛰므로 같은 명령을 다시 부르면 이어서. 모델은 기본 네 개(AIONA · ChatGPT · Claude · Gemini) —
    --model(고른 모델 하나와 AIONA) · --aiona/--aiona2(AIONA 두 모델) 로 바꿀 수 있다."""
    set_dir = Path(args.set or DEFAULT_SET)
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    asked = not args.choice
    choice = args.choice or ask_list_choice(len(groups))
    if choice not in LIST_CHOICES:
        raise SystemExit(f"--choice 는 {' · '.join(LIST_CHOICES)} 중 하나입니다")
    if choice == "2":   # 새 질문 리스트 — 질문생성 프롬프트로 Claude Code CLI 가 질문 · 첨부를 만들고 검사(k · 적용 수 · 구성)를 통과한 뒤 그 세트로 같은 흐름
        set_dir = make_new_question_set(args)
        groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
        print("질문리스트가 완성되었습니다. 답변수집을 진행합니다.", flush=True)
    elif asked and not args.set:   # 프로그램이 만든 리스트가 있으면 어느 것으로 할지
        set_dir = ask_existing_set(set_dir, len(groups))
        groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    kind = "set78"
    if getattr(args, "aiona", None) and not getattr(args, "aiona2", None):
        # AIONA 모델만 고름 (사용자 10-26 밤) — 짝인 앱도 그 모델(추론은 웹 기본 Medium · 노력 중간)로 같이 받는다 = --model 앱:모델.
        # '자동 추천' 은 짝이 없어 네 모델 기본 평가
        pairs = {a: (app, m) for a, app, m in AIONA_PAIRS}
        if args.aiona != "자동 추천" and args.aiona not in pairs:
            raise SystemExit(f"AIONA 모델 이름이 아닙니다: {args.aiona!r} — '자동 추천' 또는 {', '.join(a for a, _p, _m in AIONA_PAIRS)}")
        if args.aiona != "자동 추천":
            args.model = "{}:{}".format(*pairs[args.aiona])
    if getattr(args, "aiona2", None):   # AIONA 두 모델 비교 (사용자 10-26) — 한 실행 안에 AIONA 자리 둘, 같은 질문을 동시에 받아 같은 기준으로 판정
        providers = ["aiona", "aiona2"]
        models = {"aiona": args.aiona or "자동 추천", "aiona2": args.aiona2}
        if models["aiona"] == models["aiona2"]:
            raise SystemExit("AIONA 두 자리에 같은 모델을 골랐습니다 — 서로 다른 모델을 고르세요(업데이트 전 · 후 비교는 aiona-chat-eval pair)")
        model_choice = f"aiona:{models['aiona']}|aiona2:{models['aiona2']}"
        pick = None
    elif args.model and parse_model(args.model):   # 고른 모델 하나와 AIONA (같은 이름의 모델로, 10-22) — '0' · '전체' 는 아래 네 모델 기본으로
        pick = parse_model(args.model)
        providers = [pick[0], "aiona"]
        models = {pick[0]: pick[1], "aiona": aiona_model_for(*pick)}
        model_choice = f"{pick[0]}:{pick[1]}"
    else:
        pick = None
        providers = split_providers(args.providers or ",".join(ALL_PROVIDERS), APP_LABELS)
        models, model_choice = {}, None
    qs = question_numbers(kind, groups)
    run_id = None if choice == "3" else (args.run or find_run_for(set_dir, kind, model_choice))   # 3: 답변 새로 받기 — 새 실행
    if args.run and choice != "3" and (load_run(args.run).get("model_choice") or None) != model_choice:
        raise SystemExit(f"실행 {args.run} 은 다른 모델 선택으로 받은 실행입니다 — 같은 모델로 고르거나 --run 을 빼고 새로 시작하세요")
    from tools import result_dirs
    if not run_id:
        title = f"평가 — {set_dir.name}" + (f" · {app_label(*pick)}" if pick else f" · AIONA {models['aiona']} vs {models['aiona2']}" if "aiona2" in providers else "")
        run = new_run(title, set_dir, groups)
        run["eval_kind"] = kind
        run["model_choice"] = model_choice
        result_dirs.ensure(run, RESULTS_JSON_DIR)   # 결과 폴더 = 참고자료/결과/<처음 시작한 시각, ms 까지> (사용자 10-22)
        run_id = save_run(run).stem
    else:
        run = load_run(run_id)
        had = run.get("result_dir")
        result_dirs.ensure(run, RESULTS_JSON_DIR)   # 이어 돌리면 처음 시각 폴더 그대로 (사용자 10-22)
        if not had:
            save_run(run)
    print(f"\n질문 {len(qs)}개 · 모델 {' · '.join(display_name(p, app_label(p, models.get(p))) for p in providers)}", flush=True)
    missing = {p: [q for q in qs if q not in answered_questions(run, p, groups)] for p in providers}
    n_missing = sum(len(v) for v in missing.values())
    if n_missing < len(qs) * len(providers):
        print("답변 목록이 있습니다." + (f" (빠진 답 {n_missing}개를 먼저 받습니다)" if n_missing else ""), flush=True)
    if n_missing:
        if choice != "2":
            print("답변수집을 진행합니다.", flush=True)
        try:
            ensure_collection_chrome()   # 자식 수집기들이 같은 Chrome 에 붙도록 먼저 한 번 띄운다(떠 있으면 그대로) — 시험에서는 monkeypatch
            preflight_web_login(providers)
        except SystemExit:
            raise
        except Exception as exc:
            print(f"  ⚠ 로그인 확인을 건너뜀({type(exc).__name__})", flush=True)
        ns = argparse.Namespace(provider=",".join(providers), run=run_id, new=None, only=",".join(str(q) for q in qs), next=JUDGE_BATCH_MAX,
                                set=str(set_dir), label=None, allow_large=True, place=None, skip_done=True,
                                models=models, model=models.get(providers[0]) if len(providers) == 1 else None)
        code = cmd_collect_app(ns)
        if code:
            return code
        print("답변수집이 완료되었습니다.", flush=True)
    out_dir = ROOT / "data" / "judge_runs" / run_id
    if args.judge == "n":   # 수집만 — 같은 명령으로 다시 부르면 받은 답은 건너뛰고 평가
        again = (f" --aiona \"{models['aiona']}\" --aiona2 \"{models['aiona2']}\"" if "aiona2" in providers   # AIONA 두 모델 실행은 같은 두 자리로 이어서
                 else f" --model \"{model_choice}\"" if model_choice else "")
        print(f"평가는 나중에: aiona-chat-eval start --set \"{rel(set_dir)}\" --choice 1{again}", flush=True)
        return 0
    print("평가를 시작합니다.", flush=True)
    # 판정 v4: 크기로 Jev 또는 Decisions(판정 프롬프트 3종) → FAIL · 애매만 Codex, 모델마다 한 줄로 동시에, 질문마다 저장(멈춰도 같은 명령으로 이어서)
    from tools import judge_v4
    code = judge_v4.main(["--run", run_id, "--set", str(set_dir), "--questions", ",".join(str(q) for q in qs), "--providers", ",".join(providers),
                          "--out", "v4"])
    if code:
        return code
    if (out_dir / "v4" / "results.jsonl").exists():   # 결과 폴더에 report.html · 결과 JSON (사용자 10-22)
        ns = argparse.Namespace(dir=str(out_dir / "v4"), run=run_id, from_start=True)
        code = cmd_report(ns)   # 보류는 묻지 않음 — 보고서 '보류 목록'에서 정함 (사용자 10-27 '나에게 묻지말고')
        if not code and getattr(args, "refine", "auto") != "off":
            run_refine(run_id, set_dir, getattr(args, "refine_items", 2))
        return code
    print("판정 결과(results.jsonl)가 없어 보고서는 만들지 않았습니다", flush=True)
    return 0


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}시간 {s % 3600 // 60}분 {s % 60}초" if s >= 3600 else (f"{s // 60}분 {s % 60}초" if s >= 60 else f"{s}초")


def print_collection_times(run_id: str, providers: Optional[List[str]] = None) -> None:
    """모델별 답변 수집 시간 — 받은 턴의 답변 시간 합(모델이 답하는 데 걸린 시간)과 수집 묶음의 벽시계 합. (사용자 요청 10-19)"""
    from tools.report_html import collection_time_rows
    try:
        run = load_run(run_id)
    except SystemExit:
        return
    rows = [[display_name(c["provider"], (run.get("models") or {}).get(c["provider"])), c["questions"], c["held"], c["errors"], c["total"],
             _hms(c["measured_seconds"] or c["wall_seconds"]), _hms(c["answer_seconds"]),
             f"{(c['measured_seconds'] / len(c['question_seconds'])) if c['question_seconds'] else c['per_question']:.0f}초"]
            for c in collection_time_rows(run, providers)]
    print_table(["모델", "받음", "보류", "오류", "질문", "걸린 시간", "답 기다린 시간", "질문당"], rows)
    print("  (보류 = 답을 복사해 오지 못함 · 빈 출력 요구에서 복사 버튼 없음 — 사람이 정함, 오류 = 모델이 답하지 못함)", flush=True)


def print_judge_times(out_dir: Path) -> None:
    from tools.report_html import judge_time_info
    jt = judge_time_info(out_dir)
    if jt:
        print(f"  판정 시간 {_hms(jt['seconds'])} · {jt['judgments']}건(질문 × 모델) · 건당 {jt['per_judgment']:.0f}초", flush=True)


def _engine_of(evaluation: Dict[str, Any]) -> str:
    return str(((evaluation or {}).get("versions") or {}).get("engine") or "?")


def _check_verdicts(evaluation: Dict[str, Any]) -> Dict[Tuple[str, str], str]:
    """{(case_id, check_id): 결과} — 세는(counted) 결과만."""
    out: Dict[Tuple[str, str], str] = {}
    for c in (evaluation or {}).get("cases") or []:
        if not isinstance(c, dict):
            continue
        for r in c.get("results") or []:
            if isinstance(r, dict) and r.get("counted") in (True, "True") and r.get("check_id"):
                out[(str(c.get("case_id")), str(r["check_id"]))] = str(r.get("verdict") or "")
    return out


def cmd_code_eval(args: argparse.Namespace) -> int:
    """실행 JSON 의 CODE 평가(28개 검사)를 지금 엔진으로 다시 계산한다 — 심판은 부르지 않는다. 엔진을 올린 뒤 옛 결과로 판정이 이어지지 않게
    (10-25 밤: 실행에 v16.6.0 결과가 남아 있어 v16.7.1 의 서비스 오류 문구 ④ 턴 3개가 에러 케이스로 안 잡힘). 판정 결과에 반영하려면 judge_v4 --recode."""
    run = load_run(args.run)
    providers = [p for p in (args.providers.split(",") if args.providers else run.get("providers") or []) if p]
    log: Dict[str, Any] = {"at": _now(), "providers": {}}
    changed_total = 0
    for p in providers:
        rows = (run.get("datasets") or {}).get(p) or []
        if not rows:
            print(f"{p}: 답변이 없어 건너뜀")
            continue
        before = (run.get("evaluations") or {}).get(p) or {}
        after = code_evaluate(rows, p)
        bv, av = _check_verdicts(before), _check_verdicts(after)
        diff = sorted((k, bv.get(k), av.get(k)) for k in set(bv) | set(av) if bv.get(k) != av.get(k))
        run.setdefault("evaluations", {})[p] = after
        log["providers"][p] = {"engine_before": _engine_of(before), "engine_after": _engine_of(after), "changed": len(diff)}
        changed_total += len(diff)
        print(f"{p}: 엔진 {_engine_of(before)} → {_engine_of(after)} · 바뀐 검사 결과 {len(diff)}개")
        for (cid, chk), b, a in diff[:args.show]:
            print(f"   {cid} {chk}: {b or '-'} → {a or '-'}")
    run.setdefault("code_reevals", []).append(log)
    path = save_run(run)   # 이전 파일은 .bak-<시각> 으로 남는다
    print(f"저장: {path} · 바뀐 검사 결과 {changed_total}개 — 판정에 반영: python tools/judge_v4.py --run {args.run} --out <판정 폴더> --recode")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from tools.report_html import load_results, resolve_run_id
    d = Path(args.dir)
    results = d / "results.jsonl"
    if not results.exists():
        raise SystemExit(f"결과가 없습니다: {results}")
    run_id = getattr(args, "run", None) or resolve_run_id(d)   # --dir 가 <실행>/v4 면 부모가 실행 ID (Codex 지적 10-20)
    rows = load_results(d)   # 같은 (모델, 질문) 은 마지막 줄만
    from_start = bool(getattr(args, "from_start", False))
    if not from_start:   # report 를 따로 부르면 수집 표 · 판정 시간도 (start 는 앞 단계에서 이미 보임)
        print(f"판정 폴더 {rel(d)} — 실행 {run_id} · (모델, 질문) {len(rows)}건", flush=True)
        print_collection_times(run_id)
        print_judge_times(d)
    from tools.report_html import write_report
    try:
        run = load_run(run_id)
    except SystemExit:
        run = None
    html_path = write_report(d, run)   # 사용자 요청 10-19: 판정 결과를 HTML 로 저장 (v2: 보류 원문·사람 판정 버튼, 10-20 밤)
    D = None   # 결과 보고서의 문항 단위 숫자 — 끝 블록에
    from tools import result_dirs
    rd = None
    if run is not None and run.get("result_dir"):   # 결과 폴더(참고자료/결과/<시작 시각>/) — 질문 · 답변 JSON 과 report.html 두 파일만 (사용자 10-27)
        rd = result_dirs.ensure(run, RESULTS_JSON_DIR)
    if run is not None and (RUNS / f"{run_id}.json").exists():   # 사용자 요청 10-20: 결과를 JSON 으로도 저장소에(참고자료/결과/)
        from tools.export_results_json import IDS78, export
        kind = run.get("eval_kind")
        label = {"simple78": "간단78", "full326": "정밀326"}.get(kind, "결과") + "_판정결과"
        only = json.loads(IDS78.read_text(encoding="utf-8"))["ids"] if kind == "simple78" else None
        try:
            export(run_id, label, only, out_dir=rd or RESULTS_JSON_DIR, runs_dir=RUNS, judge_dir=d)
        except Exception as exc:   # JSON 내보내기가 막혀도 HTML 은 이미 썼다 — 자동 보고서가 여기서 죽지 않게
            print(f"  ⚠ 결과 JSON 내보내기 실패: {exc}", flush=True)
            rd = None
        if rd is not None:   # 보고서 report.html — 상세 분석 장면 한 파일, 원문 · 보류 정하기까지 (사용자 10-22 · 10-27)
            from tools import build_result_summary as brs
            keep = brs.RESULTS
            brs.RESULTS = RESULTS_JSON_DIR
            try:
                _written, D = brs.build(run_id, report_dir=rd)
            except Exception as exc:   # 판정 보고서 · 결과 JSON 은 이미 썼다
                print(f"  ⚠ 결과 보고서 만들기 실패: {type(exc).__name__}: {exc}", flush=True)
            finally:
                brs.RESULTS = keep
    args.holds = print_result_block(D, rows, rd, html_path, from_start)   # start 가 보류를 지금 정할지 물을 때 씀
    return 0


def print_result_block(D: Optional[Dict[str, Any]], rows: List[Dict[str, Any]], rd: Optional[Path], html_path: Path, from_start: bool = False) -> Optional[int]:
    """③ 결과 — 질문 단위 순위(결과 보고서와 같은 숫자, 기준 v2 3절) 표와 보고서 위치. 결과 보고서를 못 만들었으면 판정 기록으로 센 질문 단위로.
    start 에서 부르면(from_start) '평가가 완료되었습니다.' 와 보고서 위치만."""
    from tools import result_dirs
    report = rd / result_dirs.REPORT if rd is not None and (rd / result_dirs.REPORT).exists() else html_path   # 못 만들었으면 판정 폴더의 판정 보고서
    if from_start:   # start 끝 — 완료 문구와 report.html 위치만, 보류가 있으면 한 줄 (사용자 10-26 저녁)
        holds = sum(D["qtally"][p]["HOLD"] for p in D["order"]) if D is not None else sum(r.get("verdict") == "HOLD" for r in rows)
        print("\n평가가 완료되었습니다.", flush=True)
        print(f"  보고서  {report.resolve()}", flush=True)   # 보고서 하나 — 결과와 보류 정하기 (사용자 10-27)
        if holds:
            print(f"  보류 질문은 보고서의 '보류' 에서 정합니다(바로 저장: aiona-chat-eval review)", flush=True)
        return holds
    if D is not None:
        print(f"\n③ 결과 — 순위(질문 단위, 보류는 사람이 정함)", flush=True)   # 기준 v2 3절 — 순위는 질문 단위 / 78 (사용자 10-27)
        body = [[i, display_name(p, ((D.get("d") or {}).get("models") or {}).get(p)), D["qtally"][p]["PASS"], D["qtally"][p]["FAIL"], D["qtally"][p]["HOLD"], D["nq"],
                 f"{D['tally'][p]['PASS']} / {D['n']}"] for i, p in enumerate(D["order"], 1)]
        print_table(["순위", "모델", "통과", "FAIL", "보류", "질문", "문항 통과"], body)
    else:
        print(f"\n③ 결과 — 질문 단위 (보류는 사람이 정함)", flush=True)
        by: Dict[str, Counter] = {}
        for r in rows:
            by.setdefault(r["provider"], Counter())[r.get("verdict")] += 1
        print_table(["모델", "통과", "FAIL", "보류", "질문"], [[display_name(p), c["PASS"], c["FAIL"], c["HOLD"], sum(c.values())] for p, c in by.items()])
    print(f"  보고서  {rel(report)} — 결과와 보류 정하기", flush=True)


def apply_human_decisions(d: Path, doc: Dict[str, Any]) -> Dict[str, Any]:
    """사람 판정 쌓기 — <판정 폴더>/human_decisions.json 에 (모델, 질문, 턴, 항목) 마다 나중 것이 이기게 합치고(원본 results.jsonl 은 그대로),
    에러 케이스 의심 · 코드 판정 확인 칸에 고른 값은 기록에 덧붙인다. held-apply 와 보류 바로 정하기 서버(tools/review_server)가 같이 쓴다.
    다른 실행의 판단은 SystemExit."""
    from tools.report_html import load_results, normalize_items, resolve_run_id
    run_id = resolve_run_id(d)
    if doc.get("run_id") and doc["run_id"] != run_id:
        raise SystemExit(f"다른 실행의 판단 파일입니다: 파일 {doc['run_id']} ≠ 폴더 {run_id}")
    lines = load_results(d)
    known = {(r["provider"], int(r["question"])): {(it["turn"], it["item"]) for it in normalize_items(r)} for r in lines}
    suspects = {(r["provider"], int(r["question"]), it["turn"], it["item"]): it for r in lines for it in normalize_items(r)
                if it.get("kind") == "에러 케이스 확인" and it.get("answer_text")}
    code_holds = {(r["provider"], int(r["question"]), it["turn"], it["item"]): it for r in lines for it in normalize_items(r)
                  if it.get("kind") == "코드 판정 확인"}
    target = d / "human_decisions.json"
    cur = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"run_id": run_id, "decisions": []}
    merged = {(x["provider"], int(x["question"]), int(x.get("turn") or 1), x["item"]): x for x in cur.get("decisions") or []}
    added = rejected = 0
    for x in doc.get("decisions") or []:
        try:
            key = (x.get("provider"), int(x.get("question") or 0), int(x.get("turn") or 1), x.get("item"))
        except (TypeError, ValueError):
            rejected += 1
            continue
        ok = key[:2] in known and (key[3] == "__question__" or (key[2], key[3]) in known[key[:2]]) and x.get("new") in ("PASS", "FAIL", "NOT_APPLICABLE", "UNVERIFIED", "ERROR_CASE")
        if not ok:
            rejected += 1
            continue
        merged[key] = {**x, "reviewer": x.get("reviewer") or doc.get("reviewer") or "", "at": x.get("at") or doc.get("exported_at") or _now()}
        added += 1
    cur = {"run_id": run_id, "policy": doc.get("policy") or cur.get("policy"), "updated_at": _now(), "decisions": list(merged.values())}
    target.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    from code_eval import error_cases   # 에러 케이스 의심에 사람이 고른 값은 기록에 쌓는다 — 다음 판정부터 같은 글은 기록대로 (사용자 10-26)
    labels = [{"text": suspects[k]["answer_text"], "label": v["new"], "kind": "", "provider": k[0], "run_id": run_id,
               "case_id": f"Q{k[1]}_T{k[2]}", "reviewer": v.get("reviewer") or "", "note": v.get("note") or "", "at": v.get("at") or _now()}
              for k, v in merged.items() if k in suspects and v.get("new") in error_cases.LABELS]
    from tools import human_records   # 코드 판정 확인 칸의 PASS · FAIL · 사유는 기준을 고치는 재료로 쌓는다 (사용자 10-26)
    picked = [{"check_id": k[3], "kind": code_holds[k].get("code_hold") or "", "label": v["new"], "note": v.get("note") or "", "provider": k[0],
               "run_id": run_id, "case_id": f"Q{k[1]}_T{k[2]}", "excerpt": code_holds[k].get("answer_quote") or "", "reason": code_holds[k].get("reason") or "",
               "reviewer": v.get("reviewer") or "", "at": v.get("at") or _now()}
              for k, v in merged.items() if k in code_holds and v.get("new") in ("PASS", "FAIL")]
    return {"run_id": run_id, "added": added, "rejected": rejected, "total": len(merged), "path": target,
            "error_labels": error_cases.add(labels) if labels else 0, "error_suspects": len(labels),
            "code_holds": human_records.add_code_holds(picked) if picked else 0, "code_holds_no_note": sum(1 for x in picked if not x["note"].strip())}


def cmd_held_apply(args: argparse.Namespace) -> int:
    """사람 판정(보고서 '보류 목록'의 '판단 내보내기' JSON) 반영 — <폴더>/human_decisions.json 에 쌓고(원본 results.jsonl 은 그대로) 보고서를 다시 만든다.
    같은 (모델, 질문, 턴, 항목) 은 나중 것이 이긴다. 다른 실행의 파일은 거부."""
    from code_eval import error_cases
    from tools import human_records
    from tools.report_html import write_report
    d = Path(args.dir)
    res = apply_human_decisions(d, json.loads(Path(args.file).read_text(encoding="utf-8-sig")))
    print(f"사람 판정 반영: {res['added']}건 (거부 {res['rejected']}건 — 없는 항목이거나 값이 이상함) → {res['path']} (모두 {res['total']}건)")
    print(f"에러 케이스 기록: {res['error_labels']}건 덧붙임 → {error_cases.labels_path()} (에러 케이스 의심 칸 {res['error_suspects']}건)")
    print(f"코드 판정 확인 기록: {res['code_holds']}건 덧붙임 → {human_records.code_hold_path()} (사유 없는 칸 {res['code_holds_no_note']}건)")
    try:
        run = load_run(res["run_id"])
    except SystemExit:
        run = None
    print(f"HTML 보고서 다시 만듦: {write_report(d, run)}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """보류 바로 정하기 — 결과 보고서를 이 PC 안에서 열고, 누른 판단을 바로 저장한 뒤 끝낼 때 보고서를 다시 만든다 (tools/review_server)."""
    from tools import review_server
    review_server.serve(args.run, open_browser=not args.no_open)
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    """업데이트 전 vs 후 (사용자 10-26) — 두 실행의 같은 모델(기본 AIONA) 답을 한 비교 실행의 두 자리(aiona · aiona2)로 묶는다.
    CODE 는 지금 엔진으로 다시 계산하고, --judge y 면 두 자리를 같은 기준 · 같은 판정 프롬프트로 판정해 결과 폴더를 만든다."""
    import copy
    from tools import result_dirs
    before, after = load_run(args.before), load_run(args.after)
    p = args.provider
    set_dir = Path(args.set)
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    rows = {k: {r["case_id"]: r for r in (run.get("datasets") or {}).get(p) or [] if isinstance(r, dict) and r.get("case_id")} for k, run in (("a", before), ("b", after))}
    from webapp.question_generation import flatten
    order = [r["case_id"] for r in flatten(groups)]
    common = [c for c in order if c in rows["a"] and c in rows["b"]]
    if not common:
        raise SystemExit(f"두 실행에 함께 있는 {p} 답이 없습니다 — 같은 질문 세트로 받은 실행인지 확인하세요")
    sets = {(before.get("question_set") or {}).get("set_id"), (after.get("question_set") or {}).get("set_id")}
    if len(sets) > 1:
        print(f"주의: 두 실행의 질문 세트가 다릅니다({' · '.join(map(str, sets))}) — 함께 있는 질문 {len({c.split('_T')[0] for c in common})}개만 비교합니다", flush=True)
    run = new_run(args.name or f"{p} 업데이트 전 vs 후 — {args.before} · {args.after}", set_dir, groups)
    run["eval_kind"] = "pair"
    run["pair"] = {"provider": p, "before": args.before, "after": args.after, "labels": [args.before_label, args.after_label]}
    judge_root = ROOT / "data" / "judge_runs"
    for slot, src, key, lab in (("aiona", before, "a", args.before_label), ("aiona2", after, "b", args.after_label)):
        label = f"{(src.get('models') or {}).get(p) or LABELS.get(p, p)} ({lab})"
        new_rows = []
        for c in common:
            r = copy.deepcopy(rows[key][c])
            r.update({"source": label, "system_id": slot})
            new_rows.append(r)
        run["datasets"][slot] = new_rows
        ev = copy.deepcopy((src.get("evaluations") or {}).get(p) or {})
        ev["cases"] = [cs for cs in ev.get("cases") or [] if cs.get("case_id") in set(common)]
        run["evaluations"][slot] = ev
        run["models"][slot] = label
        run["providers"].append(slot)
    result_dirs.ensure(run, RESULTS_JSON_DIR)
    run_id = save_run(run).stem
    print(f"비교 실행 {run_id} — {run['models']['aiona']} ↔ {run['models']['aiona2']} · 질문 {len({c.split('_T')[0] for c in common})}개 · 결과 폴더 {RESULTS_JSON_DIR / run['result_dir']}", flush=True)
    cmd_code_eval(argparse.Namespace(run=run_id, providers="aiona,aiona2", show=0))   # 두 자리의 CODE 를 같은 엔진으로
    if args.judge != "y":
        print(f"판정: python -X utf8 tools/judge_v4.py --run {run_id} --set \"{set_dir}\" --questions all --providers aiona,aiona2 --out v4  → aiona-chat-eval report --dir data/judge_runs/{run_id}/v4", flush=True)
        return 0
    from tools import judge_v4
    code = judge_v4.main(["--run", run_id, "--set", str(set_dir), "--questions", "all", "--providers", "aiona,aiona2", "--out", "v4"])
    if code:
        return code
    return cmd_report(argparse.Namespace(dir=str(judge_root / run_id / "v4"), run=run_id))


def cmd_held(args: argparse.Namespace) -> int:
    from tools.export_held import export, print_summary
    print_summary(*export(Path(args.dir), args.run, args.text))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="aiona-chat-eval", description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    from tools.version import label
    parser.add_argument("--version", action="version", version=label())   # 시맨틱 버저닝 (tools/version.py · CHANGELOG.md, 10-27)
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
    app = collect.add_parser("web", aliases=["app"], help="수집용 Chrome 의 사이트 창에서 질문 묶음의 답 받기 (+ CODE 평가): gpt=ChatGPT 웹 · claude=Claude 웹 · gemini=Gemini 웹 · aiona=AIONA 웹 — 쉼표로 여럿이면 동시에")
    app.add_argument("--provider", required=True, help="gpt, claude, gemini, aiona — 쉼표로 여럿이면 동시에 (예: gpt,claude,gemini,aiona)")
    app.add_argument("--run", help="기존 실행 id (비우면 새 실행)")
    app.add_argument("--new", help="새 실행 이름")
    app.add_argument("--only", help="질문 번호, 쉼표로")
    app.add_argument("--next", type=int, default=JUDGE_BATCH_MAX, help="아직 안 받은 질문 중 다음 N개 (기본 20)")
    app.add_argument("--set", default=str(DEFAULT_SET))
    app.add_argument("--label", help="결과에 보일 모델 이름 (기본: 'ChatGPT 웹 (GPT-6 · Medium)' · 'Claude 앱 (Sonnet 5.5 · 노력 중간)')")
    app.add_argument("--allow-large", action="store_true")
    app.add_argument("--place", choices=sorted(set(PLACE_BY_PROVIDER.values())), help="창 자리 (동시 수집 때 부모가 넣음)")
    app.add_argument("--skip-done", action="store_true", help="--only 목록에서 이미 받은 질문은 건너뛰기")
    app.add_argument("--model", help="모델 (claude · gpt · gemini 한 모델일 때, 예: 'Opus 5.5' · 'GPT-5.6 Sol' · '3.1 Pro') — 사이트의 모델 메뉴 이름 그대로")
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
    rn = sub.add_parser("run", help="웹 수집(선택) → 올리기(선택) → 다음 묶음 판정 → 결과. 부를 때마다 한 묶음, 다시 부르면 이어서")
    rn.add_argument("--run", required=True)
    rn.add_argument("--collect", help="먼저 수집용 Chrome 에서 다음 묶음의 답을 받을 모델: gpt, claude (쉼표로 여럿)")
    rn.add_argument("--providers")
    rn.add_argument("--next", type=int, default=JUDGE_BATCH_MAX)
    rn.add_argument("--out")
    rn.add_argument("--set", default=str(DEFAULT_SET))
    rn.add_argument("--workers", type=int, default=1)
    rn.add_argument("--import-csv", help="먼저 올릴 웹 답변 CSV")
    rn.add_argument("--import-provider", choices=["aiona", "gpt", "claude", "gemini"])
    rn.add_argument("--label")
    rn.set_defaults(func=cmd_run)
    ce = sub.add_parser("code-eval", help="실행 JSON 의 CODE 평가(28개 검사)를 지금 엔진으로 다시 계산 — 심판 호출 없음 (판정 반영은 judge_v4 --recode)")
    ce.add_argument("--run", required=True)
    ce.add_argument("--providers", help="모델 쉼표 목록 (기본: 실행의 모든 모델)")
    ce.add_argument("--show", type=int, default=20, help="바뀐 검사 결과를 몇 개까지 보여 줄지 (기본 20)")
    ce.set_defaults(func=cmd_code_eval)
    rp = sub.add_parser("report", help="판정 결과 요약 + HTML 보고서")
    rp.add_argument("--dir", required=True)
    rp.add_argument("--run", help="실행 ID (생략하면 --dir 의 폴더 이름이나 부모 이름에서 찾음)")
    rp.set_defaults(func=cmd_report)
    ha = sub.add_parser("held-apply", help="report.html 에서 내보낸 사람 판정 JSON 을 반영하고 보고서를 다시 만든다")
    ha.add_argument("--dir", required=True)
    ha.add_argument("--file", required=True, help="held_decisions_<실행>.json")
    ha.set_defaults(func=cmd_held_apply)
    hd = sub.add_parser("held", help="판정 결과의 보류·오류를 CSV 하나로 (<폴더>/held.csv — gold 열에 사람이 PASS/FAIL)")
    hd.add_argument("--dir", required=True)
    hd.add_argument("--run", help="실행 번호 — 답을 찾을 때 씀 (기본: 폴더 이름)")
    hd.add_argument("--text", choices=("excerpt", "none", "full"), default="excerpt", help="질문·답 원문 범위 (기본 앞부분)")
    hd.set_defaults(func=cmd_held)
    rv = sub.add_parser("review", help="보류 바로 정하기 — 보고서를 이 PC 안에서 열고, 누른 사람 판정을 바로 저장(끝내면 보고서 다시 만듦)")
    rv.add_argument("--run", help="실행 id (기본: 가장 최근에 판정한 실행)")
    rv.add_argument("--no-open", action="store_true", help="브라우저를 열지 않고 주소만")
    rv.set_defaults(func=cmd_review)
    st = sub.add_parser("start", help="질문 리스트 고르기 → 네 모델 동시 수집(진행 판) → 평가 → report.html (인자 없이 부르면 이것)")
    st.add_argument("--choice", choices=list(LIST_CHOICES), help="1 기존 질문 리스트 · 2 새 질문 리스트 만들기 · 3 기존 리스트로 답변 새로 받기 (안 주면 물어봄)")
    st.add_argument("--providers", help="받을 모델 (기본: gpt,claude,gemini,aiona)")
    st.add_argument("--model", help="평가할 모델 하나 — 메뉴 번호, '앱:모델'(예 claude:Opus 5.5) 또는 0(전체). 고르면 그 앱과 AIONA 만 받음")
    st.add_argument("--run", help="이어 쓸 실행 id (기본: 같은 세트·종류의 최근 실행, 없으면 새로)")
    st.add_argument("--set", help="질문 세트 폴더 (기본: 참고자료/질문세트_78 — 새로 만든 리스트가 있으면 물어봄)")
    st.add_argument("--size", choices=["full", "small"], default="full", help="새 질문 리스트(2번) 크기 — full 78개(기본) · small 흐름 시험용 5개")
    st.add_argument("--judge", choices=["y", "n"], default="y", help="수집 뒤 평가 — y(기본, 바로 평가) · n(수집만)")
    st.add_argument("--refine", choices=["auto", "off"], default="auto", help="평가 끝에 하네스2(기준 다듬기) — auto(기본, 바로 이어서) · off")
    st.add_argument("--refine-items", type=int, default=2, help="하네스2 가 다듬을 기준 수 (기본 2)")
    st.add_argument("--aiona", help="AIONA 모델(AIONA 메뉴 이름, 예 '자동 추천' · 'GPT-5.6 Sol') — 이것만 주면 짝인 앱도 그 모델로 함께 받음(= --model 앱:모델, '자동 추천' 은 네 모델 기본), --aiona2 와 함께면 두 모델 비교의 첫째 자리")
    st.add_argument("--aiona2", help="AIONA 두 모델 비교 — 둘째 자리 모델(예 'Claude Sonnet 5.5'). 주면 AIONA 두 모델만 동시에 받아 같은 기준으로 판정")
    st.set_defaults(func=cmd_start)
    pr = sub.add_parser("pair", help="업데이트 전 vs 후 — 두 실행의 같은 모델(기본 AIONA) 답을 한 비교 실행에 묶어 같은 기준으로 판정")
    pr.add_argument("--before", required=True, help="업데이트 전 실행 id")
    pr.add_argument("--after", required=True, help="업데이트 후 실행 id")
    pr.add_argument("--provider", default="aiona", choices=["aiona", "gpt", "claude", "gemini"])
    pr.add_argument("--before-label", default="업데이트 전")
    pr.add_argument("--after-label", default="업데이트 후")
    pr.add_argument("--name", help="비교 실행 이름")
    pr.add_argument("--set", default=str(DEFAULT_SET))
    pr.add_argument("--judge", choices=["y", "n"], default="n", help="묶은 뒤 바로 판정 · 결과 폴더까지(y)")
    pr.set_defaults(func=cmd_pair)
    dr = sub.add_parser("doctor", help="실행 환경 점검 — 파이썬 · 패키지 · .env · Codex CLI 로그인 · 1차 심판 키 · Chrome (값은 출력 안 함)")
    dr.set_defaults(func=lambda a: __import__("tools.doctor", fromlist=["main"]).main([]))
    lg = sub.add_parser("login", help="수집용 Chrome 을 띄우고 네 사이트(ChatGPT · Claude · Gemini · AIONA) 창을 연다 — 처음 한 번 직접 로그인, 로그인 확인까지")
    lg.set_defaults(func=lambda a: __import__("tools.setup_wizard", fromlist=["main"]).main(["--web"]))
    st = sub.add_parser("setup", help="처음 설정 한 번에 — .env 키(OpenAI · Jev · AIONA) · Codex · Claude CLI 로그인 · 웹 로그인 · 점검 (10-26 저녁)")
    st.add_argument("--keys", action="store_true", help=".env 키만 (있는 키도 바꿀지 물음)")
    st.add_argument("--cli", action="store_true", help="Codex · Claude CLI 로그인만")
    st.add_argument("--web", action="store_true", help="수집용 Chrome 웹 로그인만 (aiona-chat-eval login 과 같음)")
    st.set_defaults(func=lambda a: __import__("tools.setup_wizard", fromlist=["main"]).main([f"--{k}" for k in ("keys", "cli", "web") if getattr(a, k)]))
    if not argv and len(sys.argv) <= 1 or argv == []:
        argv = ["start"]
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:   # Ctrl+C — 긴 글 대신 한 줄. 받은 답 · 판정 · 만들던 질문 리스트는 저장돼 있어 같은 명령이면 이어서
        print(chr(10) + "멈췄습니다 — 같은 명령을 다시 실행하면 이어서 합니다.", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
