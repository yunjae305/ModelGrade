"""Codex 자리에 Claude CLI 를 넣어 판정하고 Codex 판정과 비교 (judge-claude-compare-v1, 사용자 10-22 미팅 4번 확인:
"Codex 와 Claude CLI(Sonnet)로 판정해 보니 78개 중 3개 정도만 판정이 달랐다" → "CLAUDE CLI로 평가하는거는 지금 한번 해보라는거야").

같은 조건: 판정 프롬프트 v4 + 선별 뒷글(SCREEN_SUFFIX) · 같은 입력 JSON · 저장된 1차 심판(Jev · Decisions) 결과 그대로(API 다시 안 부름) →
걸린 항목도 같다 → 2차 심판만 Codex(gpt-6-luna · low) 대신 Claude CLI(Sonnet 5.5 · effort low) → 합치기 · 질문 판정은 judge_v4 그대로.
웹 검색은 Codex 와 같은 때(J5-1.a · J5-3.a 를 넘길 때)만 WebSearch · WebFetch 를 켜고, 첨부 이미지는 Read 도구로 원본을 열게 한다.

    python -X utf8 tools/judge_claude_compare.py judge --questions 1,2,3     # 10~20문항씩 (네 모델 동시)
    python -X utf8 tools/judge_claude_compare.py compare                     # Codex 판정(v4)과 비교 → 비교.md · 비교.json

→ data/judge_runs/<실행>/v4-claude-sonnet/ — results.jsonl · codex/(Claude 출력) · claude_calls.jsonl(호출마다 토큰 · 시간) · 비교.md
기존 v4 폴더 · 참고자료/결과 · 보고서는 읽기만 한다.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import judge_v4 as jv  # noqa: E402
from tools import judge_inputs as ji  # noqa: E402

VERSION = "judge-claude-compare-v1"
RUN = "50ca3ccccace"
SET_ID = "qs-2026-10-21-mt3"       # 질문 세트는 폴더 이름이 아니라 set.json 의 id 로 찾는다 (폴더 이름이 바뀌어도 — 10-22 질문세트_78 로 바꿈)


def find_set_dir(run: Dict[str, Any]) -> Path:
    """실행에 적힌 질문 세트(set_id)의 폴더 — 참고자료/질문세트_*/set.json 의 id 로 찾는다."""
    from tools import report_html
    sid = ((run or {}).get("question_set") or {}).get("set_id") or SET_ID
    d = report_html.set_dir_for({"question_set": {"set_id": sid}})
    sj = d / "set.json"
    if not sj.exists() or json.loads(sj.read_text(encoding="utf-8")).get("id") != sid:
        raise SystemExit(f"질문 세트 {sid} 의 폴더를 참고자료/질문세트_* 에서 찾지 못했습니다")
    return d
BASE_OUT = "v4"                    # Codex 판정 (비교 기준)
OUT = "v4-claude-sonnet"           # Claude CLI 판정
MODEL = "claude-sonnet-5-5"        # 'sonnet' 별칭은 Sonnet 5 로 잡혀 이름으로 지정 (10-22 확인)
EFFORT = "low"                     # Codex 와 같은 낮은 추론 (gpt-6-luna · low)
SYSTEM = "너는 AI 답변 품질 평가의 판정 심판이다. 사용자 메시지의 판정 지시문을 그대로 따르고, 지정된 JSON 형식으로만 답한다."


class ClaudeJudge:
    """judge_v4 가 Codex 에 쓰는 모양(judge · last_usage · files_dir · io_dir)을 그대로 갖춘 Claude CLI 심판.
    429 · 사용량 한도는 실패로 적지 않고 60 · 120 · 300초, 그 뒤 600초마다 기다렸다 다시 보낸다(Codex 와 같은 규칙)."""

    RATE_LIMIT_WAITS = (60, 120, 300)
    RATE_LIMIT_WAIT_MAX = 600
    name = "claude"

    def __init__(self, executable: str, model: str = MODEL, effort: str = EFFORT, timeout: float = 1800, calls_log: Optional[Path] = None) -> None:
        self.executable, self.model, self.reasoning, self.timeout = executable, model, effort, timeout
        self.files_dir: Optional[Path] = None
        self.io_dir: Optional[Path] = None
        self.calls_log = calls_log
        self._local = threading.local()
        self._lock = threading.Lock()

    @property
    def last_usage(self) -> Dict[str, Any]:   # 네 모델이 동시에 불러도 섞이지 않게 스레드마다
        return getattr(self._local, "usage", {})

    @staticmethod
    def is_rate_limited(text: str) -> bool:
        t = (text or "").lower()
        return any(k in t for k in ("429", "529", "rate limit", "rate_limit", "usage limit", "overloaded", "too many requests", "limit reached"))

    def _log(self, row: Dict[str, Any]) -> None:
        if self.calls_log:
            with self._lock, self.calls_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**row, "at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")

    def judge(self, prompt: str, input_text: str, search: bool, tag: str = "",
              output_schema: Optional[Dict[str, Any]] = None, images: Optional[List[Any]] = None) -> Tuple[str, Any, str]:
        self._local.usage = {}
        imgs = [str(Path(i).resolve()) for i in images or [] if Path(i).is_file()]
        tools = (["WebSearch", "WebFetch"] if search else []) + (["Read"] if imgs else [])
        cmd = [self.executable, "-p", "--output-format", "json", "--model", self.model, "--effort", self.reasoning,
               "--safe-mode", "--no-session-persistence", "--system-prompt", SYSTEM,
               "--json-schema", json.dumps(output_schema or {}, ensure_ascii=False), "--tools", ",".join(tools)]
        if tools:
            cmd += ["--allowedTools", *tools]
        if imgs:
            cmd += ["--add-dir", *sorted({str(Path(i).parent) for i in imgs})]
        text = prompt + "\n[입력]\n" + input_text
        if imgs:   # Codex 는 -i 로 원본 이미지를 받았다 — Claude CLI 는 Read 도구로 연다
            text += "\n\n[첨부 원본 이미지 — Read 도구로 열어 직접 확인하세요]\n" + "\n".join(f"- {p}" for p in imgs)
        last, waits, attempt = "", 0, 0
        while attempt < 2:
            attempt += 1
            t0 = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="claude-judge-") as folder:   # 프로젝트 밖 빈 폴더 — 설정 · 문서를 읽지 않게
                try:
                    proc = subprocess.run(cmd, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                          timeout=self.timeout or None, cwd=folder, shell=False)
                except subprocess.TimeoutExpired:
                    last = f"Claude CLI 가 {self.timeout:.0f}초 안에 끝나지 않음"
                    self._log({"tag": tag, "ok": False, "seconds": round(time.monotonic() - t0, 1), "error": last})
                    continue
                except OSError as exc:
                    return "ERROR", None, f"Claude CLI 실행 실패: {exc}"
            secs = round(time.monotonic() - t0, 1)
            try:
                out = json.loads(proc.stdout or "")
            except ValueError:
                out = None
            if not isinstance(out, dict) or out.get("is_error") or proc.returncode or not isinstance(out.get("structured_output"), dict):
                last = (str((out or {}).get("result") or (out or {}).get("subtype") or "") + " " + (proc.stderr or "")[-300:]).strip()[:400] \
                    or f"종료 코드 {proc.returncode}"
                if isinstance(out, dict) and out.get("api_error_status"):
                    last = f"api_error_status {out.get('api_error_status')} · {last}"
                self._log({"tag": tag, "ok": False, "seconds": secs, "error": last, **self._usage(out)})
                if self.is_rate_limited(last):
                    pause = self.RATE_LIMIT_WAITS[waits] if waits < len(self.RATE_LIMIT_WAITS) else self.RATE_LIMIT_WAIT_MAX
                    waits += 1
                    jv.say(f"   ⏳ {tag} Claude 한도 — {pause}초 기다렸다 다시 ({waits}번째)")
                    time.sleep(pause)
                    attempt -= 1   # 한도 대기는 시도 횟수에 세지 않는다
                continue
            u = self._usage(out)
            # 결과 줄(calls.codex)에 전체 토큰이 남게 — 입력은 캐시 포함 합(Codex 의 input_tokens 와 같은 뜻), 캐시 · 웹 검색은 칸을 나눠 (10-22 미팅 7번)
            self._local.usage = {"input_tokens": u["input_tokens"] + u["cache_read"] + u["cache_creation"], "output_tokens": u["output_tokens"],
                                 "uncached_input_tokens": u["input_tokens"], "cache_read_input_tokens": u["cache_read"],
                                 "cache_creation_input_tokens": u["cache_creation"], "web_search_requests": u["web_search"]}
            self._log({"tag": tag, "ok": True, "seconds": secs, "search": search, "images": len(imgs), **u})
            jv.say(f"      ✅ {tag} Claude 완료 — 입력 {u['input_tokens']:,} · 캐시 읽기 {u['cache_read']:,} · 캐시 쓰기 {u['cache_creation']:,} · 출력 {u['output_tokens']:,} 토큰, {secs:.0f}초")
            if self.io_dir:
                safe = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", tag or "claude").strip("_")
                (self.io_dir / f"{safe}_결과.json").write_text(json.dumps(out["structured_output"], ensure_ascii=False, indent=1), encoding="utf-8")
            return "OK", out["structured_output"], ""
        return "ERROR", None, last

    @staticmethod
    def _usage(out: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        u = (out or {}).get("usage") or {}
        mu = (out or {}).get("modelUsage") or {}
        return {"input_tokens": int(u.get("input_tokens") or 0), "output_tokens": int(u.get("output_tokens") or 0),
                "cache_read": int(u.get("cache_read_input_tokens") or 0), "cache_creation": int(u.get("cache_creation_input_tokens") or 0),
                "web_search": int((u.get("server_tool_use") or {}).get("web_search_requests") or 0),
                "turns": (out or {}).get("num_turns"), "models": sorted(mu), "cost_usd_reported": (out or {}).get("total_cost_usd")}


def find_claude() -> str:
    from code_eval.claude_cli_client import find_claude_executable
    exe = find_claude_executable()
    if not exe:
        raise SystemExit("claude 실행 파일을 찾지 못했습니다")
    return exe


def latest_lines(path: Path) -> Dict[Tuple[str, int], Dict[str, Any]]:
    latest: Dict[Tuple[str, int], Dict[str, Any]] = {}
    if path.exists():
        for x in path.read_text(encoding="utf-8").splitlines():
            if x.strip():
                l = json.loads(x)
                latest[(l["provider"], int(l["question"]))] = l
    return latest


def cmd_judge(args: argparse.Namespace) -> int:
    run = json.loads((ROOT / "data" / "comparison_runs" / f"{args.run}.json").read_text(encoding="utf-8"))
    set_dir = find_set_dir(run)
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    jv.QUESTION_TARGETS.clear()
    jv.QUESTION_TARGETS.update(jv.question_targets(groups))
    rows = jv.flatten(groups)
    prompt = jv.PROMPT_V4.read_text(encoding="utf-8")
    fixed = json.loads(jv.JEV_V4.read_text(encoding="utf-8"))
    base = ROOT / "data" / "judge_runs" / args.run / BASE_OUT
    out = ROOT / "data" / "judge_runs" / args.run / OUT
    for sub in ("inputs", "codex", "jev", "decisions", "codex_io"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    for f in (base / "decisions").glob("*.json"):   # 1차 심판 결과는 Codex 판정 때 것 그대로 (API 다시 안 부름)
        if not (out / "decisions" / f.name).exists():
            shutil.copy2(f, out / "decisions" / f.name)
    jv._LOG["path"] = str(out / "run.log")
    jv.say = jv.log_say
    gpt = ClaudeJudge(find_claude(), calls_log=out / "claude_calls.jsonl")
    gpt.files_dir, gpt.io_dir = set_dir / "files", out / "codex_io"
    base_lines = latest_lines(base / "results.jsonl")
    done = {k for k, l in latest_lines(out / "results.jsonl").items() if not l.get("judge_error") and l.get("status") != "error"}
    qs = [int(x) for x in args.questions.split(",") if x.strip()]
    providers = args.providers.split(",")
    lock = threading.Lock()

    def save(line: Dict[str, Any]) -> None:
        with lock, (out / "results.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({**line, "judge2": {"engine": "claude_cli", "model": gpt.model, "effort": gpt.reasoning},
                                "at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")

    def work(provider: str) -> None:
        data = {r.get("case_id"): r for r in (run.get("datasets") or {}).get(provider) or [] if isinstance(r, dict)}
        cases = {c.get("case_id"): c for c in ((run.get("evaluations") or {}).get(provider) or {}).get("cases") or [] if isinstance(c, dict)}
        for q in qs:
            if (provider, q) in done:
                continue
            b = base_lines.get((provider, q)) or {}
            if b.get("status") != "judged":   # 빈 출력 요구 등 심판 없이 정한 줄 — 비교할 2차 판정이 없어 그대로 옮긴다
                save({**b, "copied_from_base": True})
                continue
            turns = sorted([r for r in rows if ji._question(r["case_id"]) == q], key=ji._turn)
            answers = {t["case_id"]: (data.get(t["case_id"]) or {}).get("answer") or "" for t in turns}
            deliveries = {t["case_id"]: ji.delivery_note(data.get(t["case_id"])) for t in turns}
            code_fails = sorted({c for t in turns for c in jv.question_outcome(cases.get(t["case_id"]), None).get("code_fails") or []})
            try:   # reuse_codex: 이어 돌릴 때 저장된 Claude 출력을 다시 씀(빠진 항목만 다시 묻기)
                line = {**jv.screen_one(q, provider, turns, answers, deliveries, code_fails, prompt, fixed, gpt, None, out,
                                        reuse_codex=True, reuse_decisions=True), "status": "judged"}
            except Exception as exc:
                line = {"version": jv.VERSION, "provider": provider, "question": q, "turns": len(turns), "status": "error", "verdict": "HOLD",
                        "reason": f"판정 실행 오류: {type(exc).__name__}: {str(exc)[:200]}"}
                jv.say(f"⚠ Q{q} {provider}: {line['reason']}")
            save(line)

    jv.say(f"Claude CLI({gpt.model} · effort {gpt.reasoning}) 판정 — 질문 {len(qs)}개 × 모델 {len(providers)}개 · 로그 {out / 'run.log'}")
    t0 = time.monotonic()
    threads = [threading.Thread(target=work, args=(p,), name=f"claude-{p}") for p in providers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with (out / "timing.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "judge-claude", "questions": qs, "seconds": round(time.monotonic() - t0, 1),
                            "at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
    jv.say(f"묶음 끝 — 벽시계 {(time.monotonic() - t0) / 60:.0f}분")
    try:   # 묶음이 끝날 때마다 결과 폴더(참고자료/결과/<처음 시작 시각>/) 갱신 — 판정은 이미 저장됐으니 여기서 막혀도 다음 묶음은 그대로
        jv.say(f"결과 폴더 갱신: {publish(args.run, run)}")
    except Exception as exc:
        jv.say(f"⚠ 결과 폴더 갱신 실패: {type(exc).__name__}: {str(exc)[:200]}")
    return 0


# ------------------------------------------------------------------ 결과 폴더 ------------------------------------------------------------------
POINTER = "result_dir.json"   # 판정 폴더에 적어 두는 결과 폴더 이름 — 이어 돌려도 처음 시각 폴더 그대로


def ensure_result_dir(out: Path, run_id: str, base: Optional[Path] = None, started: Optional[datetime] = None) -> Path:
    """이 비교의 결과 폴더 = 참고자료/결과/<처음 시작 시각, ms 까지>/ (사용자 10-22 결과 폴더 양식).
    시각은 판정 폴더를 처음 만든 때(첫 판정 명령을 시작한 때). run.json 에는 run_id 대신 compare_of 를 적는다 —
    result_dirs.find(run_id) 가 본 결과 폴더 대신 이 비교 폴더를 잡지 않게(결과 요약 빌더 · modelgrade report 가 실행 ID 로 찾음)."""
    from tools import result_dirs
    base = base or result_dirs.RESULTS
    ptr = out / POINTER
    if ptr.exists():
        name = json.loads(ptr.read_text(encoding="utf-8"))["name"]
    else:
        name = result_dirs.stamp(started or datetime.fromtimestamp(out.stat().st_ctime))   # 윈도우 st_ctime = 만든 시각
        ptr.write_text(json.dumps({"name": name}, ensure_ascii=False), encoding="utf-8")
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    meta = {"version": VERSION, "kind": "judge-compare", "compare_of": run_id, "judge_out": OUT, "base_out": BASE_OUT, "started": name,
            "name": "Codex ↔ Claude CLI 판정 비교 (10-22 미팅 4번)", "question_set": {"set_id": SET_ID},
            "judge2": {"engine": "claude_cli", "model": MODEL, "effort": EFFORT}, "base_judge2": {"engine": "codex_cli", "model": "gpt-6-luna", "effort": "low"}}
    (d / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def publish(run_id: str, run: Optional[Dict[str, Any]] = None) -> Path:
    """결과 폴더에 모은다: run.json · results.jsonl · summary.json · 판정보고서.html/.md(tools/report_html — Codex 자리가 Claude CLI) ·
    비교.md/비교.json · report.html(비교 결과 화면)."""
    from tools import report_html, result_dirs
    out = ROOT / "data" / "judge_runs" / run_id / OUT
    rd = ensure_result_dir(out, run_id)
    lines = list(latest_lines(out / "results.jsonl").values())
    summary = {"version": jv.VERSION, "compare": VERSION, "run_id": run_id, "prompt": jv.PROMPT_V4.name,
               "judge2": {"engine": "claude_cli", "model": MODEL, "effort": EFFORT}, "finished_at": datetime.now(timezone.utc).isoformat(),
               "models": {p: {**{v: sum(1 for l in lines if l["provider"] == p and l.get("verdict") == v) for v in ("PASS", "FAIL", "HOLD")},
                              "n": sum(1 for l in lines if l["provider"] == p)} for p in sorted({l["provider"] for l in lines})}}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "report.md").write_text(jv.report_md([l for l in lines if l.get("status") == "judged"]), encoding="utf-8")
    if run is None:
        run = json.loads((ROOT / "data" / "comparison_runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    report_html.write_report(out, run, find_set_dir(run))   # 판정 보고서 — 결과 폴더에서는 판정보고서.html
    result_dirs.publish(out, rd)
    res, _md = compare_core(run_id)
    for f in ("비교.md", "비교.json"):
        shutil.copy2(out / f, rd / f)
    (rd / result_dirs.REPORT).write_text(render_html(res, rd.name), encoding="utf-8")
    return rd


# ------------------------------------------------------------------ 비교 ------------------------------------------------------------------
LABEL = {"gpt": "GPT", "claude": "Claude", "gemini": "Gemini", "aiona": "AIONA"}
KO = {"PASS": "완전 통과", "FAIL": "FAIL", "HOLD": "보류"}
IKO = {"PASS": "PASS", "FAIL": "FAIL", "NOT_APPLICABLE": "해당 없음", "UNVERIFIED": "보류", None: "-"}


def items_of(line: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {f"T{n}:{i}": v for n, t in ((line.get("turn_results") or {}).items()) for i, v in ((t or {}).get("items") or {}).items()}


def first_codex_stats(path: Path) -> Dict[Tuple[str, int], Dict[str, Any]]:
    """Codex 가 실제로 판정한 줄(저장 출력을 다시 쓴 줄 빼고)의 시간 · 토큰 — (모델, 질문)마다 처음 것."""
    got: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for x in path.read_text(encoding="utf-8").splitlines():
        if not x.strip():
            continue
        l = json.loads(x)
        c = (l.get("calls") or {}).get("codex") or {}
        if c.get("status") == "OK" and not c.get("reused") and (l["provider"], int(l["question"])) not in got:
            got[(l["provider"], int(l["question"]))] = c
    return got


def compare_core(run_id: str) -> Tuple[Dict[str, Any], List[str]]:
    """Codex 판정(v4)과 Claude CLI 판정의 최신 줄을 맞춰 본다 — 비교.json · 비교.md 를 판정 폴더에 쓰고 (결과, 마크다운 줄)을 돌려준다."""
    base_dir = ROOT / "data" / "judge_runs" / run_id / BASE_OUT
    out = ROOT / "data" / "judge_runs" / run_id / OUT
    A, B = latest_lines(base_dir / "results.jsonl"), latest_lines(out / "results.jsonl")
    keys = sorted(set(A) & set(B), key=lambda k: (list(LABEL).index(k[0]), k[1]))
    providers = [p for p in LABEL if any(k[0] == p for k in keys)]
    qs = sorted({k[1] for k in keys})
    diffs, item_pairs, flag_pairs = [], Counter(), Counter()
    for k in keys:
        a, b = A[k], B[k]
        ia, ib = items_of(a), items_of(b)
        for i in set(ia) & set(ib):
            item_pairs[(ia[i]["decision"], ib[i]["decision"])] += 1
            if ia[i].get("flagged"):
                flag_pairs[(ia[i]["decision"], ib[i]["decision"])] += 1
        if a.get("verdict") != b.get("verdict"):
            # 질문 판정을 가른 항목부터 — 보류가 끼면 보류(UNVERIFIED) 차이, 아니면 FAIL 차이
            first = "UNVERIFIED" if "HOLD" in (a.get("verdict"), b.get("verdict")) else "FAIL"
            changed = sorted(((i, ia[i]["decision"], ib[i]["decision"], (ib[i].get("reason") or ia[i].get("reason") or "").replace("\n", " "))
                              for i in set(ia) & set(ib)
                              if ia[i]["decision"] != ib[i]["decision"] and {"FAIL", "UNVERIFIED"} & {ia[i]["decision"], ib[i]["decision"]}),
                             key=lambda x: (first not in (x[1], x[2]), x[0]))
            cnt = {side: Counter(v["decision"] for v in it.values()) for side, it in (("codex", ia), ("claude", ib))}
            miss = [sum(len(v) for v in ((((x.get("calls") or {}).get("codex") or {}).get("missing_items")) or {}).values()) for x in (a, b)]
            diffs.append({"provider": k[0], "question": k[1], "codex": a.get("verdict"), "claude": b.get("verdict"),
                          "judge_error": b.get("judge_error"), "items": changed[:6], "n_changed": len(changed),
                          "fail": [cnt["codex"]["FAIL"], cnt["claude"]["FAIL"]], "hold": [cnt["codex"]["UNVERIFIED"], cnt["claude"]["UNVERIFIED"]],
                          "code_fails": b.get("code_fails") or [], "missing": miss})
    per = {p: sum(1 for d in diffs if d["provider"] == p) for p in providers}
    any_q = sorted({d["question"] for d in diffs})
    same_items = sum(v for (x, y), v in item_pairs.items() if x == y)
    same_flag = sum(v for (x, y), v in flag_pairs.items() if x == y)
    tally = {side: {p: Counter(L[k].get("verdict") for k in keys if k[0] == p) for p in providers} for side, L in (("codex", A), ("claude", B))}
    n_of = {p: sum(1 for k in keys if k[0] == p) for p in providers}
    calls = [json.loads(x) for x in (out / "claude_calls.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()] \
        if (out / "claude_calls.jsonl").exists() else []
    tok: Dict[str, Counter] = {}
    for c in calls:
        p = next((pp for pp in LABEL if re.search(rf"\b{pp}\b", c.get("tag") or "")), "?")
        t = tok.setdefault(p, Counter())
        t["calls"] += 1
        t["fail_calls"] += 0 if c.get("ok") else 1
        for f in ("input_tokens", "output_tokens", "cache_read", "cache_creation", "web_search", "seconds"):
            t[f] += c.get(f) or 0
    cx = first_codex_stats(base_dir / "results.jsonl")
    ctok = {p: Counter() for p in providers}
    for (p, q), c in cx.items():
        if p in ctok and q in qs:
            ctok[p]["calls"] += 1
            ctok[p]["seconds"] += c.get("seconds") or 0
            ctok[p]["input_tokens"] += c.get("input_tokens") or 0
            ctok[p]["output_tokens"] += c.get("output_tokens") or 0
    cl_secs = {p: sum(((B[(p, q)].get("calls") or {}).get("codex") or {}).get("seconds") or 0 for q in qs if (p, q) in B) for p in providers}
    flips = [d for d in diffs if "PASS" in (d["codex"], d["claude"])]   # 통과 여부가 다른 칸 (보류 ↔ FAIL 은 둘 다 통과 아님)
    res = {"version": VERSION, "run": run_id, "model": MODEL, "effort": EFFORT, "questions": len(qs), "providers": providers,
           "diff_per_model": per, "n_per_model": n_of, "cells": len(keys), "diff_cells": len(diffs),
           "pass_flip_per_model": {p: sum(1 for d in flips if d["provider"] == p) for p in providers}, "pass_flip_cells": len(flips),
           "pass_flip_questions_any_model": sorted({d["question"] for d in flips}), "diff_questions_any_model": any_q, "diffs": diffs,
           "items_total": sum(item_pairs.values()), "items_same": same_items,
           "flagged_total": sum(flag_pairs.values()), "flagged_same": same_flag,
           "item_pairs": {f"{x}→{y}": v for (x, y), v in item_pairs.most_common()}, "tally": {s: {p: dict(c) for p, c in t.items()} for s, t in tally.items()},
           "claude_tokens": {p: dict(c) for p, c in tok.items()}, "claude_seconds_per_model": cl_secs,
           "codex_first_calls": {p: dict(c) for p, c in ctok.items()}}
    (out / "비교.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    n = len(qs)
    L = [f"# Codex vs Claude CLI 판정 비교 — 실행 {run_id}",
         "",
         f"- 2차 심판: Codex gpt-6-luna · low ↔ Claude CLI {MODEL} · effort {EFFORT} (1차 결과 · 걸린 항목 · 프롬프트 같음)",
         f"- 질문 {n}개 × 모델 {len(providers)}개 = {len(keys)}칸",
         "",
         "## 질문 판정이 다른 수",
         "",
         "| 모델 | 다른 질문 | Codex (통과 · FAIL · 보류) | Claude CLI (통과 · FAIL · 보류) |",
         "|---|---|---|---|"]
    for p in providers:
        a, b = tally["codex"][p], tally["claude"][p]
        L.append(f"| {LABEL[p]} | {per[p]} / {n_of[p]} | {a['PASS']} · {a['FAIL']} · {a['HOLD']} | {b['PASS']} · {b['FAIL']} · {b['HOLD']} |")
    L += ["", f"- 한 모델이라도 다른 질문: **{len(any_q)} / {n}** ({', '.join(f'Q{q}' for q in any_q) or '없음'})",
          f"- 다른 칸(질문 × 모델): {len(diffs)} / {len(keys)} — 그중 통과 여부가 다른 칸 {len(flips)}"
          f" (한 모델이라도 통과 여부가 다른 질문 {len(res['pass_flip_questions_any_model'])} / {n}; 나머지는 보류 ↔ FAIL — 보류 우선 규칙 차이)",
          f"- 항목 단위 최종 판정 일치: {same_items:,} / {sum(item_pairs.values()):,} ({same_items / max(1, sum(item_pairs.values())):.1%})"
          f" · 2차 심판에 넘긴 항목만: {same_flag:,} / {sum(flag_pairs.values()):,} ({same_flag / max(1, sum(flag_pairs.values())):.1%})",
          "", "## 다른 질문", "",
          "| 질문 | 모델 | Codex | Claude CLI | FAIL 항목 (Codex · Claude) | 보류 항목 (Codex · Claude) | 갈린 기준 (Codex → Claude) · 사유 — 질문 판정을 가른 것부터 |",
          "|---|---|---|---|---|---|---|"]
    for d in diffs:
        why = "<br>".join(f"{i} {IKO.get(x, x)} → {IKO.get(y, y)} — {r[:110]}" for i, x, y, r in d["items"]) or (d.get("judge_error") or "")
        more = f"<br>… 갈린 항목 모두 {d['n_changed']}개" if d["n_changed"] > len(d["items"]) else ""
        code = f" (CODE FAIL {len(d['code_fails'])})" if d["code_fails"] else ""
        nomiss = f"<br>끝내 답하지 않은 항목 Codex {d['missing'][0]} · Claude {d['missing'][1]}" if any(d["missing"]) else ""
        L.append(f"| Q{d['question']} | {LABEL[d['provider']]} | {KO.get(d['codex'], d['codex'])} | {KO.get(d['claude'], d['claude'])}{code} | "
                 f"{d['fail'][0]} · {d['fail'][1]} | {d['hold'][0]} · {d['hold'][1]} | {why}{more}{nomiss} |")
    L += ["", "## 걸린 시간 · 토큰 (2차 심판)", "",
          "| 모델 | Claude 호출 | Claude 시간 | 입력 | 캐시 읽기 | 캐시 쓰기 | 출력 | 웹 검색 | Codex 호출(처음) | Codex 시간 | Codex 입력 | Codex 출력 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in providers:
        t, c = tok.get(p, Counter()), ctok.get(p, Counter())
        L.append(f"| {LABEL[p]} | {t['calls']} (실패 {t['fail_calls']}) | {cl_secs[p] / 60:.0f}분 | {t['input_tokens']:,} | {t['cache_read']:,} | {t['cache_creation']:,} | "
                 f"{t['output_tokens']:,} | {t['web_search']} | {c['calls']} | {c['seconds'] / 60:.0f}분 | {c['input_tokens']:,} | {c['output_tokens']:,} |")
    L += ["", "Codex 토큰 · 시간은 v4 결과 줄 중 저장 출력을 다시 쓰지 않은 처음 판정 줄의 값(다시 판정 · 복구 때 다시 물은 몫은 빠짐)."]
    (out / "비교.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    return res, L


def cmd_compare(args: argparse.Namespace) -> int:
    rd = publish(args.run)
    print((ROOT / "data" / "judge_runs" / args.run / OUT / "비교.md").read_text(encoding="utf-8"))
    print(f"결과 폴더: {rd}")
    return 0


def render_html(res: Dict[str, Any], folder: str) -> str:
    """비교 결과 화면 (결과 폴더의 report.html) — 숫자 · 다른 질문 · 시간 · 토큰."""
    import html

    def E(x: Any) -> str:
        return html.escape(str(x))

    def pct(a: float, b: float) -> str:
        return f"{a / max(1, b):.1%}"

    ps, n = res["providers"], res["questions"]
    cards = [("한 모델이라도 판정이 다른 질문", f"{len(res['diff_questions_any_model'])} / {n}"),
             ("판정이 다른 칸 (질문 × 모델)", f"{res['diff_cells']} / {res['cells']}"),
             ("통과 여부가 다른 칸", f"{res['pass_flip_cells']} / {res['cells']}"),
             ("항목 판정 일치 (2차 심판에 넘긴 항목)", pct(res["flagged_same"], res["flagged_total"]))]
    rows = "".join(
        f"<tr><th>{E(LABEL[p])}</th><td class=n>{res['diff_per_model'][p]} / {res['n_per_model'][p]}</td><td class=n>{res['pass_flip_per_model'][p]}</td>"
        + "".join(f"<td class=n>{res['tally'][s][p].get('PASS', 0)} · {res['tally'][s][p].get('FAIL', 0)} · {res['tally'][s][p].get('HOLD', 0)}</td>"
                  for s in ("codex", "claude")) + "</tr>" for p in ps)
    drows = "".join(
        f"<tr><th>Q{d['question']}</th><td>{E(LABEL[d['provider']])}</td><td>{E(KO.get(d['codex'], d['codex']))}</td>"
        f"<td>{E(KO.get(d['claude'], d['claude']))}</td><td class=n>{d['fail'][0]} · {d['fail'][1]}</td><td class=n>{d['hold'][0]} · {d['hold'][1]}</td><td><ul>"
        + "".join(f"<li><b>{E(i)}</b> {E(IKO.get(x, x))} → {E(IKO.get(y, y))} — {E(r[:140])}</li>" for i, x, y, r in d["items"])
        + (f"<li class=m>… 갈린 항목 모두 {d['n_changed']}개</li>" if d["n_changed"] > len(d["items"]) else "")
        + (f"<li class=m>{E(d['judge_error'])}</li>" if d.get("judge_error") else "")
        + (f"<li class=m>끝내 답하지 않은 항목 — Codex {d['missing'][0]} · Claude {d['missing'][1]}</li>" if any(d.get("missing") or []) else "")
        + "</ul></td></tr>" for d in res["diffs"])
    trows = ""
    for p in ps:
        t, c = res["claude_tokens"].get(p, {}), res["codex_first_calls"].get(p, {})
        trows += (f"<tr><th>{E(LABEL[p])}</th><td class=n>{t.get('calls', 0)}</td><td class=n>{res['claude_seconds_per_model'].get(p, 0) / 60:.0f}분</td>"
                  f"<td class=n>{t.get('input_tokens', 0) + t.get('cache_read', 0) + t.get('cache_creation', 0):,}</td><td class=n>{t.get('cache_read', 0):,}</td>"
                  f"<td class=n>{t.get('cache_creation', 0):,}</td><td class=n>{t.get('output_tokens', 0):,}</td>"
                  f"<td class=n>{c.get('calls', 0)}</td><td class=n>{c.get('seconds', 0) / 60:.0f}분</td>"
                  f"<td class=n>{c.get('input_tokens', 0):,}</td><td class=n>{c.get('output_tokens', 0):,}</td></tr>")
    css = """
:root { --bg:#fbfaf7; --fg:#1d1d1b; --mut:#6b6a66; --line:#e3e0d8; --card:#fff; --acc:#2f5bd3; }
@media (prefers-color-scheme: dark) { :root { --bg:#17171a; --fg:#ecebe6; --mut:#a3a19b; --line:#34343a; --card:#202024; --acc:#8fb0ff; } }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 "Noto Sans KR", system-ui, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:28px 16px 60px; }
h1 { font-size:24px; margin:0 0 4px; } h2 { font-size:18px; margin:32px 0 10px; }
p.sub, li.m { color:var(--mut); }
.cards { display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:12px; margin-top:18px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
.card b { display:block; font-size:26px; color:var(--acc); } .card span { color:var(--mut); font-size:13px; }
.tw { overflow-x:auto; } table { border-collapse:collapse; width:100%; background:var(--card); }
th, td { border:1px solid var(--line); padding:7px 10px; text-align:left; vertical-align:top; }
thead th { background:color-mix(in srgb, var(--acc) 8%, var(--card)); font-size:13px; }
td.n { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
ul { margin:0; padding-left:18px; }
"""
    card_html = "".join(f'<div class="card"><b>{E(v)}</b><span>{E(k)}</span></div>' for k, v in cards)
    return (
        '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>판정 심판 비교</title>\n<style>{css}</style></head><body><main>\n"
        "<h1>판정 심판 비교 — Codex ↔ Claude CLI</h1>\n"
        f'<p class="sub">실행 {E(res["run"])} · 질문 {n}개 × 모델 {len(ps)}개 · 2차 심판만 바꿈: Codex gpt-6-luna · low ↔ Claude CLI {E(res["model"])} · '
        f'effort {E(res["effort"])} (1차 심판 결과 · 걸린 항목 · 판정 프롬프트 · 합치기 규칙은 같음) · 결과 폴더 {E(folder)}</p>\n'
        f'<div class="cards">{card_html}</div>\n'
        '<h2>모델별</h2><div class="tw"><table><thead><tr><th>모델</th><th>판정이 다른 질문</th><th>통과 여부가 다른 질문</th>'
        f"<th>Codex (통과 · FAIL · 보류)</th><th>Claude CLI (통과 · FAIL · 보류)</th></tr></thead><tbody>{rows}</tbody></table></div>\n"
        '<p class="sub">보류 ↔ FAIL 차이는 둘 다 통과가 아닙니다 — 질문에 보류 항목이 하나라도 있으면 보류(보류 우선 규칙)라서 생기는 차이입니다.</p>\n'
        '<h2>판정이 다른 질문</h2><div class="tw"><table><thead><tr><th>질문</th><th>모델</th><th>Codex</th><th>Claude CLI</th>'
        "<th>FAIL 항목<br>Codex · Claude</th><th>보류 항목<br>Codex · Claude</th><th>갈린 기준 (Codex → Claude) · 사유 — 질문 판정을 가른 것부터</th></tr></thead>"
        f"<tbody>{drows or '<tr><td colspan=7>없음</td></tr>'}</tbody></table></div>\n"
        '<h2>걸린 시간 · 토큰 (2차 심판)</h2><div class="tw"><table><thead><tr><th>모델</th><th>Claude 호출</th><th>Claude 시간</th><th>입력(캐시 포함)</th>'
        "<th>캐시 읽기</th><th>캐시 쓰기</th><th>출력</th><th>Codex 호출(처음)</th><th>Codex 시간</th><th>Codex 입력</th><th>Codex 출력</th></tr></thead>"
        f"<tbody>{trows}</tbody></table></div>\n"
        '<p class="sub">Codex 토큰 · 시간은 저장 출력을 다시 쓰지 않은 처음 판정 줄의 값입니다(다시 판정 · 복구 때 다시 물은 몫은 빠짐). '
        f"항목 판정 일치 — 전체 {res['items_same']:,} / {res['items_total']:,} ({pct(res['items_same'], res['items_total'])}).</p>\n"
        "</main></body></html>\n")


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("judge", help="Claude CLI 로 판정 (10~20문항씩)")
    j.add_argument("--run", default=RUN)
    j.add_argument("--questions", required=True, help="질문 번호 쉼표 목록 — 한 번에 10~20개")
    j.add_argument("--providers", default="gpt,claude,gemini,aiona")
    c = sub.add_parser("compare", help="Codex 판정(v4)과 비교")
    c.add_argument("--run", default=RUN)
    a = ap.parse_args(argv)
    if a.cmd == "judge":
        n = len([x for x in a.questions.split(",") if x.strip()])
        if n > 20:
            raise SystemExit(f"한 번에 20문항까지 ({n}개) — 10~20개씩 나눠 돌리세요")
        return cmd_judge(a)
    return cmd_compare(a)


if __name__ == "__main__":
    raise SystemExit(main())
