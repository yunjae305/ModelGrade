"""판정 실행 (judge-run-v1) — 나눠서 판정: J1~J4 는 Jev, J5~J8 은 GPT CLI(Codex, 구독 로그인). 질문 단위, 고정 프롬프트·고정 질문 묶음.

    python -X utf8 tools/judge_run.py data/comparison_runs/8c61f117e661.json --only 36,17 --providers aiona   # 시험
    python -X utf8 tools/judge_run.py data/comparison_runs/8c61f117e661.json                                   # 전체 326 × 모델
    옵션: --workers 2 · --out DIR(기본 data/judge_runs/<run_id>) · --set 참고자료/질문세트_326_생성프롬프트v2
    설정(.env): CODEX_CLI_PATH·CODEX_CLI_MODEL·CODEX_CLI_REASONING·CODEX_CLI_TIMEOUT·SEMANTIC_WEB_SEARCH (GPT) ·
               TYPESAFE_API_KEY·JEV_MODEL (Jev) · JEV_MAX_STATE_CHARS(기본 0 = 상한 없음 — 사용자 결정 10-19: GPT·Jev 모두 상한 없음. Jev 가 거부(HTTP 400)하면 GPT 가 J1~J4 도)
    CODEX_CLI_TIMEOUT 기본 0 = 제한 없음.

- GPT CLI 에는 고정 프롬프트 v3 + [입력](그 질문에서 GPT 가 맡은 항목만 [이 턴의 상황]에)을 한 번에 보낸다(``codex exec``, 출력 형식 고정).
- Jev 에는 고정 질문 묶음(jev_questions_v1)과 state(질문의 모든 턴)를 보낸다. 두 심판은 동시에 부른다.
- Jev 가 못 하면(입력 상한·오류·키 없음) 그 J1~J4 는 GPT CLI 가 같은 고정 프롬프트로 다시 판정하고 ``fallback`` 으로 남긴다.
- 결과: <out>/results.jsonl (모델 × 질문 한 줄 — 턴별 항목 판정·심판·시간·판정) · summary.json·summary.md (모델별 PASS·FAIL·보류 / 326, 시간).
  이미 있는 줄은 건너뛴다 — 구독 사용 한도로 멈춰도 같은 명령으로 이어서 돌린다. 답변 원문이 들어 있어 data/ 아래(git 제외)에 둔다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval.llm_client import env_value, redact  # noqa: E402
from tools import judge_inputs as ji  # noqa: E402
from tools import judge_split as js  # noqa: E402
from webapp.run_summary import question_outcome  # noqa: E402

VERSION = "judge-run-v1"
JEV_MAX_STATE_CHARS = 0   # 0 = 상한 없음 (사용자 결정 2026-10-19)
JEV_CONFIDENT = 0.7   # Jev 결정의 최대 확률이 이보다 낮으면 '애매' — Codex 가 다시 본다 (.env JEV_CONFIDENT)
JEV_TRIM_CHARS = 35_000   # Jev 입력 예산 — 넘으면 긴 자료(첨부·참조)만 줄인다(.env JEV_TRIM_CHARS, 0 = 안 줄임). 3.6만 자는 받고 5.2만 자는 400 (2026-10-19)


# --- 심판 호출 -------------------------------------------------------------------------------------------------------

_SAY_LOCK = threading.Lock()


SHOW_THINKING = True   # Codex 생각 요약 줄 — 여러 모델을 동시에 판정할 때는 끈다 (judge_v4 --show-thinking)


def say(text: str) -> None:
    """판정 진행 한 줄 — 질문 2개가 동시에 돌아도 줄이 섞이지 않게 잠금 아래 바로 찍는다."""
    with _SAY_LOCK:
        print(text, flush=True)


class CodexJudge:
    """``codex exec`` 로 고정 프롬프트 v3 + 입력을 보내고 JSON 결과를 받는다. 실패하면 한 번 다시.
    429(서버 거절 — 동시 요청이 많을 때의 속도 제한 또는 구독 사용량 한도)는 실패로 적지 않고 풀릴 때까지 기다렸다 다시 보낸다 — 60·120·300초, 그 뒤 600초마다, 횟수 제한 없음(사용자 결정 10-19)."""

    RATE_LIMIT_WAITS = (60, 120, 300)
    RATE_LIMIT_WAIT_MAX = 600

    name = "gpt"

    def __init__(self, executable: str, model: str, reasoning: str, timeout: float, web_search: str,
                 runner: Callable[..., Any] = subprocess.run) -> None:
        self.executable, self.model, self.reasoning, self.timeout, self.web_search = executable, model, reasoning, timeout, web_search
        self._run = runner
        self.io_dir: Optional[Path] = None   # 판정 폴더 codex_io/ — 입력 · 결과 파일을 남긴다 (judge_v4 가 정함, 10-21)

    #: 파일 방식에서 표준 입력으로 보내는 짧은 지시 (10-21 저녁 — 지시문 · 입력은 작업 폴더의 파일)
    FILE_INSTRUCTION = ("이 작업 폴더의 `판정_지시문.txt` 가 판정 지시문이고 `판정_입력.json` 이 판정할 [입력] 입니다. "
                        "두 파일을 처음부터 끝까지 모두 읽으세요 — 긴 파일은 나눠서 끝까지 읽고, 참조 자료에서 필요한 부분은 찾아 읽습니다. "
                        "두 파일은 UTF-8 입니다 — PowerShell 이면 `Get-Content -Encoding UTF8` 로 읽으세요(기본 인코딩이면 한글이 깨집니다). "
                        "그다음 지시문대로 [입력]을 판정하고, 지시문의 출력 형식(JSON)으로만 답하세요. 파일을 고치거나 새 파일을 만들지 마세요.")

    def _file_mode(self) -> bool:
        # 기본은 표준 입력으로 통째로(문서 전체가 한 번에 판정 맥락에) — 파일 방식은 Codex 판정 실행에서 도구가 막혀 파일을 못 읽었다(10-21 시험).
        # 입력 · 결과 파일은 어느 방식이든 codex_io/ 에 남긴다. CODEX_INPUT_MODE=file 이면 파일 방식.
        return os.environ.get("CODEX_INPUT_MODE", "stdin").strip().lower() == "file" and self._run is subprocess.run

    def manifest(self) -> Dict[str, Any]:
        return {"engine": "codex_cli", "model": self.model, "reasoning": self.reasoning, "web_search": self.web_search,
                "rate_limit_wait": "60·120·300s then 600s, unlimited"}

    def _keep(self, tag: str, kind: str, text: str, prompt: Optional[str] = None) -> None:
        """codex_io/ 에 입력 · 결과 파일을 남긴다 — 같은 질문 · 모델을 다시 물으면 덮어쓴다. 지시문은 모든 질문에 같아 한 번만."""
        if not self.io_dir:
            return
        try:
            self.io_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", tag or "codex").strip("_") or "codex"
            (self.io_dir / f"{safe}_{kind}").write_text(text, encoding="utf-8")
            if prompt is not None and not (self.io_dir / "판정_지시문.txt").exists():
                (self.io_dir / "판정_지시문.txt").write_text(prompt, encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def is_rate_limited(error: str) -> bool:
        text = (error or "").lower()
        return "429" in text or "too many requests" in text or "rate limit" in text or "usage limit" in text

    def _stream(self, cmd: List[str], text: str, cwd: str, tag: str, timeout: Optional[float] = None) -> Any:
        """``codex exec`` 를 띄우고 stdout 의 JSON 이벤트를 한 줄씩 받아 그때그때 터미널에 보인다(생각 요약·답 도착·완료). 결과는 subprocess.run 과 같은 모양."""
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", cwd=cwd, shell=False)
        err_buf: List[str] = []
        reader = threading.Thread(target=lambda: err_buf.append(proc.stderr.read() if proc.stderr else ""), daemon=True)
        reader.start()
        try:
            proc.stdin.write(text)
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        lines: List[str] = []
        started = last_seen = time.monotonic()
        killed: List[bool] = []
        timer = None
        limit = timeout or self.timeout
        if limit:   # stdout 을 끝까지 읽은 뒤에야 wait(timeout) 에 닿아 멈춘 Codex 를 못 끊던 것 (Codex 지적 10-20) — 시간이 다 되면 죽인다
            timer = threading.Timer(limit, lambda: (killed.append(True), proc.kill()))
            timer.daemon = True
            timer.start()
        for line in proc.stdout:
            lines.append(line)
            now = time.monotonic()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            item = event.get("item") or {}
            if kind == "item.completed" and item.get("type") == "reasoning" and SHOW_THINKING:
                say(f"      💭 {tag} Codex 생각: {(item.get('text') or '').strip().replace(chr(10), ' ')[:110]}")
            elif kind == "item.completed" and item.get("type") == "agent_message":
                say(f"      💬 {tag} Codex 답 도착 ({len(item.get('text') or ''):,}자, {now - started:.0f}초)")
            elif kind == "item.started" and item.get("type") == "web_search_call":
                say(f"      🌐 {tag} Codex 웹 검색 …")
            elif kind == "turn.completed":
                usage = event.get("usage") or {}
                self.last_usage = dict(usage)   # 판정 줄에 토큰을 남기려고 (보고서의 토큰 표)
                say(f"      ✅ {tag} Codex 완료 — 입력 {usage.get('input_tokens', '?')} · 출력 {usage.get('output_tokens', '?')} 토큰, {now - started:.0f}초")
            elif kind in ("error", "turn.failed"):
                say(f"      ⚠ {tag} Codex: {str(event.get('message') or event.get('error') or event)[:120]}")
            elif now - last_seen > 20 and SHOW_THINKING:
                say(f"      … {tag} Codex 작업 중 ({now - started:.0f}초)")
            last_seen = now
        if timer is not None:
            timer.cancel()
        if killed:
            reader.join(timeout=5)
            raise subprocess.TimeoutExpired(cmd, limit)
        try:
            proc.wait(timeout=limit or None)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        reader.join(timeout=5)
        return subprocess.CompletedProcess(cmd, proc.returncode, "".join(lines), "".join(err_buf))

    def judge(self, prompt: str, input_text: str, search: bool, tag: str = "",
              output_schema: Optional[Dict[str, Any]] = None, images: Optional[List[Any]] = None,
              workdir: Optional[Path] = None, timeout: Optional[float] = None, input_note: Optional[str] = None) -> Tuple[str, Any, str]:
        # output_schema: 판정 v4 처럼 출력 모양이 다를 때 · images: 이미지 첨부 원본 경로 — 전사 글만으로는 "이미지 자체를 확인할 수 없다"고 보류하던 것 (사용자 10-21)
        # workdir: 파일 방식으로 이 폴더에 입력 파일을 저장하고 Codex 를 거기서 띄운다(첫 호출 한 번 시험, 사용자 10-26) · timeout: 이 호출만의 시간 제한
        # input_note: workdir 과 함께 — 지시문은 표준 입력으로 통째로(규칙을 잘라 읽지 않게), 입력만 파일 · [입력] 자리에 이 읽기 안내를 둔다
        from code_eval.codex_cli_client import DISABLED_FEATURES, SEARCH_HOST_FEATURES, _error, _events
        live = search and self.web_search == "live"
        self.last_usage = {}
        last = ""
        waits = 0
        attempt = 0
        while attempt < 2:
            attempt += 1
            with tempfile.TemporaryDirectory(prefix="judge-run-") as folder:
                schema, output = Path(folder) / "schema.json", Path(folder) / "answer.json"
                schema.write_text(json.dumps(output_schema or js.gpt_output_schema(), ensure_ascii=False), encoding="utf-8")
                cmd = [self.executable, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                       "--color", "never", "--json", "--model", self.model, "-c", f"model_reasoning_effort={json.dumps(self.reasoning)}",
                       "-c", 'approval_policy="never"', "-c", f"web_search={json.dumps('live' if live else 'disabled')}",
                       "-c", "project_doc_max_bytes=0", "-c", "mcp_servers={}", "--output-schema", str(schema), "--output-last-message", str(output)]
                file_mode = self._file_mode() or (workdir is not None and self._run is subprocess.run)
                run_dir = Path(workdir).resolve() if (file_mode and workdir is not None) else Path(folder)
                for feature in DISABLED_FEATURES:
                    if file_mode and feature in ("shell_tool", "unified_exec"):
                        continue   # 파일 방식: 작업 폴더의 지시문 · 입력 파일을 읽으려면 셸이 필요 — 읽기 전용 모드라 고치거나 만들 수는 없다 (10-21)
                    cmd += ["--disable", feature]
                for feature in SEARCH_HOST_FEATURES:
                    if file_mode and not live and feature == "code_mode_host":
                        continue   # 파일 방식: 이 Codex 판(0.162)은 셸 명령을 code_mode_host 로 돌린다 — 끄면 "code-mode host is disabled" 로 파일을 못 읽는다 (10-26)
                    cmd += ["--enable" if live else "--disable", feature]
                if file_mode and os.name == "nt":
                    # 승인 없이(never) 읽기 전용 셸을 쓰려면 Windows 샌드박스가 있어야 한다 — 없으면 모든 셸 명령이 "blocked by policy" 로 거부됨 (10-26 시험)
                    cmd += ["-c", 'windows.sandbox="unelevated"']
                for img in images or []:
                    if Path(img).is_file():
                        cmd += ["-i", str(Path(img).resolve())]   # 절대 경로 — Codex 는 임시 폴더에서 돌아 상대 경로를 못 찾는다(조용히 무시)
                cmd.append("-")
                text = prompt + "\n[입력]\n" + input_text
                if not file_mode:
                    self._keep(tag, "입력.json", input_text, prompt)   # 통째로 넘기는 방식도 넘긴 입력을 파일로 남긴다 (10-21)
                if file_mode:   # 파일 방식 (10-21 저녁) — 지시문 · 입력을 작업 폴더(또는 workdir) 파일로, 표준 입력은 짧은 지시만
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "판정_입력.json").write_text(input_text, encoding="utf-8")
                    if workdir is not None and input_note:
                        text = prompt + "\n[입력]\n" + input_note
                    else:
                        (run_dir / "판정_지시문.txt").write_text(prompt, encoding="utf-8")
                        text = self.FILE_INSTRUCTION
                    self._keep(tag, "입력.json", input_text, prompt)
                try:   # 고정 프롬프트 뒤에 [입력] — 프롬프트 '입력 형식' 그대로
                    if self._run is subprocess.run:   # 실제 실행: 이벤트를 흘리며 받는다. 시험의 대역 실행기는 그대로 부른다
                        proc = self._stream(cmd, text, str(run_dir), tag, timeout)
                    else:
                        proc = self._run(cmd, input=prompt + "\n[입력]\n" + input_text, capture_output=True, text=True, encoding="utf-8",
                                         timeout=self.timeout or None, cwd=folder, shell=False)
                except subprocess.TimeoutExpired:
                    last = f"Codex CLI 가 {(timeout or self.timeout):.0f}초 안에 끝나지 않음"
                    continue
                except OSError as exc:
                    return "ERROR", None, redact(f"Codex CLI 실행 실패: {exc}")
                events = _events(proc.stdout or "")
                if proc.returncode or not any(e.get("type") == "turn.completed" for e in events):
                    last = _error(events, proc.stderr or "", proc.returncode)
                    if self.is_rate_limited(last):
                        pause = self.RATE_LIMIT_WAITS[waits] if waits < len(self.RATE_LIMIT_WAITS) else self.RATE_LIMIT_WAIT_MAX
                        waits += 1
                        say(f"   ⏳ Codex 429(요청이 몰려 서버가 잠시 거절) — {pause}초 기다렸다 다시 ({waits}번째)")
                        time.sleep(pause)
                        attempt -= 1   # 한도 대기는 시도 횟수에 세지 않는다
                    continue
                try:
                    result = json.loads(output.read_text(encoding="utf-8-sig"))
                    self._keep(tag, "결과.json", json.dumps(result, ensure_ascii=False, indent=1))
                    return "OK", result, ""
                except (OSError, ValueError):
                    last = "Codex CLI 결과 파일이 없거나 JSON 이 아님"
        return "ERROR", None, last


class JevJudge:
    """Jev(TypeSafe System One) — 고정 질문 묶음 + state."""

    name = "jev"

    def __init__(self, client: Any, max_chars: int = JEV_MAX_STATE_CHARS, trim_chars: int = JEV_TRIM_CHARS, confident: float = JEV_CONFIDENT) -> None:
        self.client, self.max_chars, self.trim_chars, self.confident = client, max_chars, trim_chars, confident

    def manifest(self) -> Dict[str, Any]:
        config = getattr(self.client, "config", None)
        return {"engine": "jev", "model": getattr(config, "model", None), "max_state_chars": self.max_chars, "trim_chars": self.trim_chars,
                "confident": self.confident}

    def judge(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Tuple[str, Any, str]:
        if self.max_chars and js.state_chars(state) > self.max_chars:
            return "TOO_LONG", None, f"입력 {js.state_chars(state):,}자 — Jev 입력 상한 {self.max_chars:,}자 초과"
        try:
            call = self.client.system_one(state, questions)
        except Exception as exc:   # Jev 오류는 이 질문의 Jev 판정 없음으로 — GPT 가 대신 판정
            return "ERROR", None, redact(f"{type(exc).__name__}: {exc}")
        if getattr(call, "status", None) != "OK":
            return "ERROR", None, f"{getattr(call, 'status', '?')}: {getattr(call, 'error', '')}"
        return "OK", call.body, ""


class DecisionsJudge:
    """OpenAI Decisions API — Jev 와 같은 고정 질문 묶음 + state 를 원문 그대로(자르지 않음). Jev 가 한도를 넘거나 실패한 턴의 J1~J4 를 대신 (decisions-v1, 2026-10-20)."""

    name = "decisions"

    def __init__(self, client: Any) -> None:
        self.client = client

    def manifest(self) -> Dict[str, Any]:
        config = getattr(self.client, "config", None)
        return {"engine": "openai_decisions", "model": getattr(config, "model", None), "role": "Jev 대체(한도 초과·실패 턴)"}

    def judge(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Tuple[str, Any, str]:
        try:
            call = self.client.decide(state, questions)
        except Exception as exc:
            return "ERROR", None, redact(f"{type(exc).__name__}: {exc}")
        if getattr(call, "status", None) != "OK":
            return "ERROR", None, f"{getattr(call, 'status', '?')}: {getattr(call, 'error', '')}"
        return "OK", call.body, ""


def decisions_from_env() -> Tuple[Optional[DecisionsJudge], Optional[str]]:
    """OPENAI_API_KEY(또는 DECISIONS_API_KEY)가 있으면 Decisions 대체 심판을 만든다 — 없으면 (None, 이유)."""
    from code_eval.decisions_client import resolve_decisions_client
    client, _, reason = resolve_decisions_client()
    return (DecisionsJudge(client) if client else None), reason


# --- 질문 하나 ------------------------------------------------------------------------------------------------------

def judge_question(entry: Dict[str, Any], turns: List[Dict[str, Any]], table: Dict[str, Dict[str, Any]], answers: Dict[str, str],
                   prompt: str, fixed: Dict[str, Any], mark: str, gpt: Optional[CodexJudge], jev: Optional[JevJudge],
                   case_outcomes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    started = time.monotonic()
    by_turn = {int(k): v for k, v in entry["items_by_turn"].items()}
    plan = {n: js.split(items) for n, items in by_turn.items()}
    jev_items = {n: p["jev"] for n, p in plan.items() if p["jev"]}
    gpt_items = {n: p["gpt"] for n, p in plan.items() if p["gpt"]}
    calls: Dict[str, Any] = {}
    results: Dict[int, Dict[str, Dict[str, Any]]] = {n: {} for n in by_turn}

    tag = f"Q{entry['question']} {entry['provider']}"

    def run_gpt(items: Dict[int, List[str]], label: str) -> None:
        if not items:
            return
        situations = {t["case_id"]: ji.situations_for(table[t["case_id"]], items.get(int(t.get("turn") or 1), [])) for t in turns}
        text = ji.question_input(mark, turns, situations, answers, entry.get("deliveries"))
        search = any("J5-1.a" in v for v in items.values()) and not any((t.get("reference") or "").strip() for t in turns)
        n_items = sum(len(v) for v in items.values())
        say(f"   🖥 {tag} Codex CLI 판정 중 — {'J1~J4 대신 ' if label != 'gpt' else ''}{n_items}항목 · 입력 {len(text):,}자 · 웹 검색 {'켬' if search else '끔'} …")
        t0 = time.monotonic()
        status, output, error = gpt.judge(prompt, text, search, tag) if gpt else ("ERROR", None, "GPT CLI 없음")
        calls[label] = {"status": status, "error": error, "seconds": round(time.monotonic() - t0, 1), "search": search,
                        "input_chars": len(text)}
        say(f"   🖥 {tag} Codex {status} {calls[label]['seconds']}초" + (f" — {str(error)[:100]}" if status != "OK" else ""))
        checked = js.check_gpt(output if status == "OK" else None, turns, answers, items)
        for n, part in checked.items():
            for item, value in part.items():
                if status != "OK":
                    value["problems"] = [f"GPT CLI 실패: {error}"]
                results[n][item] = {**value, **({"fallback": True} if label != "gpt" else {})}

    def run_jev() -> Optional[str]:
        if not jev_items:
            return None
        if jev is None:
            calls["jev"] = {"status": "ERROR", "error": "Jev 없음 (TYPESAFE_API_KEY)", "seconds": 0}
            return "ERROR"
        situations = {t["case_id"]: ji.situations_for(table[t["case_id"]], jev_items.get(int(t.get("turn") or 1), [])) for t in turns}
        state, questions = js.jev_request(turns, situations, answers, jev_items, fixed)
        raw_chars = js.state_chars(state)
        trimmed = js.trim_material(state, getattr(jev, "trim_chars", JEV_TRIM_CHARS))   # 긴 자료만 줄여 Jev 가 받게 (사용자 10-19: Jev 가 제대로 일하게)
        say(f"   🔎 {tag} Jev 판정 중 — J1~J4 {len(questions)}항목 · 입력 {js.state_chars(state):,}자" + (f"(자료 {trimmed:,}자 줄임)" if trimmed else "") + " …")
        t0 = time.monotonic()
        status, body, error = jev.judge(state, questions)
        calls["jev"] = {"status": status, "error": error, "seconds": round(time.monotonic() - t0, 1), "input_chars": js.state_chars(state),
                        "raw_chars": raw_chars, "trimmed_chars": trimmed}
        say(f"   🔎 {tag} Jev {status} {calls['jev']['seconds']}초" + (f" — {str(error)[:100]}" if status != "OK" else ""))
        if status == "OK":
            for n, part in js.check_jev(body, jev_items, fixed).items():
                results[n].update(part)
        return status

    # 흐름 v2(사용자 승인 2026-10-19): Jev 먼저(0.5~2초) → Jev 가 FAIL 이거나 확률이 애매한 J1~J4 항목을 골라 Codex 호출 하나(J5~J8 과 함께)에 넣어
    # 사유·인용과 함께 확정한다. Codex FAIL → FAIL, Jev FAIL·Codex PASS → 보류(심판 갈림), 애매·Codex PASS → PASS.
    jev_status = run_jev()
    confident = getattr(jev, "confident", JEV_CONFIDENT) if jev is not None else JEV_CONFIDENT
    flagged: Dict[int, List[str]] = {}
    first: Dict[int, Dict[str, Dict[str, Any]]] = {}
    # 자료를 줄여 Jev 에 보낸 질문은 Jev 가 원문 전부를 못 봤으므로 확신 PASS 도 전부 Codex(원문 그대로)가 다시 본다 (사용자 10-19)
    trimmed = bool((calls.get("jev") or {}).get("trimmed_chars"))
    if jev_items and jev_status == "OK":
        for n, items in jev_items.items():
            for item in items:
                v = results[n].get(item) or {}
                probs = v.get("probabilities") or {}
                conf = max(probs.values()) if probs else 1.0
                if trimmed or v.get("decision") == "FAIL" or conf < confident:
                    flagged.setdefault(n, []).append(item)
                    first.setdefault(n, {})[item] = dict(v)
    merged = {n: list(gpt_items.get(n, [])) + list(flagged.get(n, [])) for n in sorted(set(gpt_items) | set(flagged))}
    if flagged and trimmed:
        say(f"   ↪ {tag} Jev 는 줄인 자료로 봐서 J1~J4 전부 → Codex 가 원문으로 다시 확정: " + " ".join(f"T{n}:{i}" for n, items in flagged.items() for i in items))
    elif flagged:
        say(f"   ↪ {tag} Jev 가 걸러낸 항목(FAIL·애매) → Codex 확정: " + " ".join(f"T{n}:{i}" for n, items in flagged.items() for i in items))
    elif jev_items and jev_status == "OK":
        say(f"   ↪ {tag} Jev J1~J4 모두 확신 PASS → Codex 는 J5~J8 만")
    run_gpt(merged, "gpt")
    for n, items in flagged.items():
        for item in items:
            results[n][item] = js.combine_jev_gpt(first[n][item], results[n].get(item) or {})
    calls["jev_flagged"] = {str(n): items for n, items in flagged.items()}
    if flagged:
        calls["jev"]["flagged_reason"] = "trimmed" if trimmed else "fail_or_unsure"
    if jev_items and jev_status != "OK":   # Jev 가 못 한 J1~J4 는 GPT 가 같은 고정 프롬프트로
        say(f"   ↪ {tag} Jev 가 못 봐서 Codex 가 J1~J4 도 대신")
        run_gpt(jev_items, "gpt_fallback")
    for part in results.values():
        js.apply_overrides(part)
    semantic = js.semantic_verdict(results)
    code_fails = sorted({c for t in turns for c in (case_outcomes.get(t["case_id"]) or {}).get("code_fails") or []})
    verdict = "FAIL" if code_fails or semantic == "FAIL" else semantic
    fails = [f"T{n}:{i}" for n, part in sorted(results.items()) for i, v in part.items() if v.get("decision") == "FAIL"]
    holds = [f"T{n}:{i}" for n, part in sorted(results.items()) for i, v in part.items() if v.get("decision") == "UNVERIFIED"]
    say(f"■ {tag}: {verdict} · {round(time.monotonic() - started)}초" + (f" · CODE FAIL {' '.join(code_fails)}" if code_fails else "")
        + (f" · FAIL {' '.join(fails)}" if fails else "") + (f" · 보류 {' '.join(holds)}" if holds else ""))
    return {**{k: entry[k] for k in ("provider", "question", "turns")}, "status": "judged", "verdict": verdict,
            "semantic": semantic, "code_fails": code_fails, "calls": calls, "seconds": round(time.monotonic() - started, 1),
            "results": {str(n): items for n, items in sorted(results.items())}}


# --- 실행 전체 ------------------------------------------------------------------------------------------------------

def summarize(lines: List[Dict[str, Any]], total: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for provider in sorted({l["provider"] for l in lines}):
        mine = [l for l in lines if l["provider"] == provider]
        tally = Counter(l["verdict"] for l in mine)
        judged = [l for l in mine if l["status"] == "judged"]
        seconds = defaultdict(list)
        for l in judged:
            for name, call in (l.get("calls") or {}).items():
                seconds[name].append(call.get("seconds") or 0)
        out[provider] = {"n": total, "done": len(mine), "PASS": tally["PASS"], "FAIL": tally["FAIL"], "HOLD": tally["HOLD"],
                         "pass_rate": round(tally["PASS"] / total, 4) if total else None,
                         "fallback": sum(1 for l in judged if "gpt_fallback" in (l.get("calls") or {})),
                         "jev_flagged": sum(len(v) for l in judged for v in ((l.get("calls") or {}).get("jev_flagged") or {}).values()),
                         "seconds": {k: {"mean": round(sum(v) / len(v), 1), "total": round(sum(v), 1)} for k, v in seconds.items() if v}}
    return out


def run(run_data: Dict[str, Any], set_dir: Path, out: Path, gpt: Optional[CodexJudge], jev: Optional[JevJudge],
        providers: Optional[List[str]] = None, only: Optional[set] = None, workers: int = 2) -> Dict[str, Any]:
    prompt = ji.PROMPT.read_text(encoding="utf-8")
    fixed = js.load_jev_questions()
    table = {r["case_id"]: r for r in json.loads((set_dir / ji.SITUATIONS).read_text(encoding="utf-8"))["rows"]}
    inputs_dir = out / "inputs"
    manifest = ji.build(run_data, set_dir, inputs_dir, providers, only)   # 상태(보류·답 없음)·턴별 항목은 입력 도구와 같은 규칙
    providers = list(manifest["counts"])
    rows = ji.flatten(json.loads((set_dir / "questions.json").read_text(encoding="utf-8")))
    questions: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        questions[ji._question(row["case_id"])].append(row)
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.jsonl"
    done = {(d["provider"], d["question"]) for d in (json.loads(x) for x in results_path.read_text(encoding="utf-8").splitlines() if x.strip())} \
        if results_path.exists() else set()
    lock = threading.Lock()
    tasks = []
    for provider in providers:
        datasets = {r.get("case_id"): r for r in (run_data.get("datasets") or {}).get(provider) or [] if isinstance(r, dict)}
        answers = {cid: r.get("answer") or "" for cid, r in datasets.items() if ji.collected(r)}
        batch = (run_data.get("evaluations") or {}).get(provider) or {}
        cases = {c.get("case_id"): c for c in batch.get("cases") or [] if isinstance(c, dict)}
        outcomes = {cid: question_outcome(c, None) for cid, c in cases.items()}
        for line in (inputs_dir / f"{provider}.jsonl").read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if (provider, entry["question"]) in done:
                continue
            tasks.append((entry, answers, outcomes))

    def work(task) -> None:
        entry, answers, outcomes = task
        if entry["status"] in ("held_empty_output", "empty_output_pass", "empty_output_fail"):   # 빈 출력 요구: 심판 없이 수집 결과로 정한다 (사용자 규칙 10-19)
            verdict = {"empty_output_pass": "PASS", "empty_output_fail": "FAIL"}.get(entry["status"], "HOLD")
            line = {**{k: entry[k] for k in ("provider", "question", "turns")}, "status": entry["status"], "verdict": verdict,
                    "reason": entry.get("reason"), "collection_note": entry.get("collection_note")}
            say(f"■ Q{entry['question']} {entry['provider']}: {'보류' if verdict == 'HOLD' else verdict} (빈 출력 요구, 심판 없이) — {entry.get('collection_note')}")
        elif entry["status"] == "held_copy_failed":
            line = {**{k: entry[k] for k in ("provider", "question", "turns")}, "status": "held_copy_failed", "verdict": "HOLD",
                    "reason": entry.get("reason"), "collection_note": entry.get("collection_note")}
            say(f"■ Q{entry['question']} {entry['provider']}: 보류 (답을 복사해 오지 못함) — {entry.get('collection_note')}")
        elif entry["status"] == "no_answer":
            line = {**{k: entry[k] for k in ("provider", "question", "turns")}, "status": "no_answer", "verdict": "FAIL",
                    "reason": entry.get("reason"), "collection_note": entry.get("collection_note")}
            say(f"■ Q{entry['question']} {entry['provider']}: FAIL (답 없음) — {entry.get('reason')}")
        else:
            turns = sorted(questions[entry["question"]], key=ji._turn)
            line = judge_question(entry, turns, table, answers, prompt, fixed, manifest["run_mark"], gpt, jev, outcomes)
        with lock:
            with results_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")

    wall0 = time.monotonic()
    say(f"판정 시작 — {len(tasks)}건(질문×모델) · {min(max(1, workers), max(1, len(tasks)))}건씩 동시에 · 심판: "
        + (f"Codex CLI {gpt.model}·{gpt.reasoning}" if gpt else "Codex 없음") + " + " + (f"Jev {(jev.manifest() or {}).get('model')}" if jev else "Jev 없음"))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(work, tasks))
    wall = round(time.monotonic() - wall0, 1)
    if tasks:   # 판정 시간 기록 — 결과(report)에서 합산 (사용자 요청 10-19: 평가를 마치는 데 걸린 시간)
        with (out / "timing.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "judge", "at": datetime.now(timezone.utc).isoformat(), "judgments": len(tasks),
                                "questions": sorted({e["question"] for e, _, _ in tasks}), "providers": providers, "seconds": wall}, ensure_ascii=False) + "\n")
    lines = [json.loads(x) for x in results_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    total = len({e["question"] for e in lines}) if only else len(questions)
    summary = {"version": VERSION, "split": js.VERSION, "prompt": manifest["prompt"], "jev_questions": fixed["version"],
               "situations": manifest["situations"], "run_id": run_data.get("run_id"), "finished_at": datetime.now(timezone.utc).isoformat(),
               "judges": {"gpt": gpt.manifest() if gpt else None, "jev": jev.manifest() if jev else None},
               "models": summarize(lines, total), "wall_seconds": wall, "judgments": len(tasks)}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    md = ["| 모델 | PASS | FAIL | 보류 | 통과율 (PASS / n) | GPT 대신 판정 | Jev→Codex 확정 항목 | 평균 시간(초) GPT · Jev |", "|---|---|---|---|---|---|---|---|"]
    for p, m in summary["models"].items():
        sec = m["seconds"]
        md.append(f"| {p} | {m['PASS']} | {m['FAIL']} | {m['HOLD']} | {m['pass_rate']:.1%} ({m['PASS']}/{m['n']}) | {m['fallback']} | {m.get('jev_flagged', 0)} | "
                  f"{(sec.get('gpt') or {}).get('mean', '-')} · {(sec.get('jev') or {}).get('mean', '-')} |")
    md.append(f"\n이번 판정 {len(tasks)}건 · 벽시계 {wall:.0f}초")
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return summary


def judges_from_env() -> Tuple[Optional[CodexJudge], Optional[JevJudge], List[str]]:
    from code_eval.codex_cli_client import check_codex_login, find_codex_executable
    from code_eval.jev_client import resolve_jev_client
    notes = []
    executable = find_codex_executable(env_value("CODEX_CLI_PATH"))
    gpt = None
    if not executable:
        notes.append("GPT CLI(Codex) 실행 파일 없음 — CODEX_CLI_PATH 확인")
    elif (reason := check_codex_login(executable)):
        notes.append(reason)
    else:
        gpt = CodexJudge(executable, env_value("CODEX_CLI_MODEL") or "gpt-6.1-sol", env_value("CODEX_CLI_REASONING") or "high",
                         float(env_value("CODEX_CLI_TIMEOUT") or 0), env_value("SEMANTIC_WEB_SEARCH") or "live")
    client, _, reason = resolve_jev_client(engine="jev")
    if reason:
        notes.append(reason + " — J1~J4 는 GPT CLI 가 대신 판정")
    jev = JevJudge(client, int(env_value("JEV_MAX_STATE_CHARS") or JEV_MAX_STATE_CHARS),
                   int(env_value("JEV_TRIM_CHARS") if env_value("JEV_TRIM_CHARS") not in (None, "") else JEV_TRIM_CHARS),
                   float(env_value("JEV_CONFIDENT") or JEV_CONFIDENT)) if client else None
    return gpt, jev, notes


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path, help="수집 결과 실행 JSON (data/comparison_runs/<id>.json)")
    parser.add_argument("--set", type=Path, default=ji.DEFAULT_SET)
    parser.add_argument("--providers", help="쉼표로 — 기본은 실행의 모든 모델")
    parser.add_argument("--only", help="쉼표로 질문 번호 — 시험용")
    parser.add_argument("--workers", type=int, default=2, help="동시에 판정할 질문 수 (구독 사용 한도를 보며)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run_data = json.loads(args.run.read_text(encoding="utf-8-sig"))
    gpt, jev, notes = judges_from_env()
    for note in notes:
        print("알림:", note)
    if gpt is None:
        print("GPT CLI 없이는 판정할 수 없습니다.")
        return 1
    out = args.out or ROOT / "data" / "judge_runs" / str(run_data.get("run_id") or args.run.stem)
    summary = run(run_data, args.set, out, gpt, jev, args.providers.split(",") if args.providers else None,
                  {int(x) for x in args.only.split(",")} if args.only else None, args.workers)
    print((out / "summary.md").read_text(encoding="utf-8"))
    print(f"결과: {out / 'results.jsonl'} · {out / 'summary.json'}")
    return 0 if summary else 1


if __name__ == "__main__":
    raise SystemExit(main())
