# -*- coding: utf-8 -*-
"""폴더 판정 (judge-folder-v1) — 질문마다 Codex 를 부르는 대신, 한 모델의 입력 파일을 폴더에 모아 두고 Codex 세션 하나가
폴더를 읽어 차례로 판정 · 결과 파일을 쓰게 한다 (10-23 미팅 6번: "파일로 썰어 놓고 한번 읽어 보다가 알아서 다 해라", 사용자 10-25 "구현해").

    python -X utf8 tools/judge_folder.py prepare --run 50ca3ccccace --from v4-r3 --out folder-pilot --provider aiona --questions 13,22,24
    python -X utf8 tools/judge_folder.py run     --run 50ca3ccccace --out folder-pilot --provider aiona
    python -X utf8 tools/judge_folder.py merge   --run 50ca3ccccace --from v4-r3 --out folder-pilot --provider aiona --set 참고자료/질문세트_78
    python -X utf8 tools/judge_folder.py compare --run 50ca3ccccace --from v4-r3 --out folder-pilot --provider aiona

- prepare: 기준 판정(--from)의 codex_io/ 에 남은 '그 질문에 Codex 가 받은 입력'(걸린 항목 포함)을 작업 폴더 `입력/Q{n}.json` 으로, 지시문 · 출력 형식(schema) ·
  작업 지시(작업_지시.md)를 함께 둔다 → 질문마다 보내던 것과 **같은 입력 · 같은 지시문**이라 차이는 호출 구조뿐.
- run --mode batch(기본): 질문 N개(--batch-size, 기본 10)를 **한 표준 입력**에 담아 `codex exec` 한 번으로 판정(출력은 questions 배열) → 질문별 결과 파일로 나눔.
  호출 수가 질문 수 → 묶음 수로 준다. 멀티턴(큰 입력)은 1개씩.
- run --mode folder: Codex 세션 하나가 폴더를 읽고 결과 파일을 쓰는 방식 — Windows 의 Codex 샌드박스에서 쓰기가 막혀(10-25 다섯 번 시도: code-mode 호스트 ·
  approval_policy · sandbox_mode · BOM 까지 바꿔도 "읽기 전용") 지금은 쓰지 않는다. Mac · Linux 에서 다시 볼 것.
- merge: 결과 파일을 판정 v4 의 저장 Codex 출력 자리(<out>/codex/Q{n}_{모델}.json + 지문)로 옮기고 기준 판정의 1차 심판 저장물(decisions/ · inputs/)을 복사한 뒤
  judge_v4 --repair 로 합쳐 results.jsonl 을 만든다(심판을 새로 부르지 않음 — 빠진 항목만 Codex 에).
- compare: 기준 판정과 항목 판정 일치율 · Codex 판정 일치율 · 시간 · 토큰을 표로.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval.llm_client import env_value  # noqa: E402

VERSION = "judge-folder-v1"
RUNS = ROOT / "data" / "judge_runs"
WORK = "작업"


def q_sorted(names: List[str]) -> List[str]:
    return sorted(names, key=lambda s: int(re.search(r"\d+", s).group(0)))


def work_dir(run: str, out: str, provider: str) -> Path:
    return RUNS / run / out / WORK / provider


def task_text(provider: str, files: List[str]) -> str:
    lines = "\n".join(f"- 입력/{f} → 결과/{f}" for f in files)
    return f"""# 작업 지시 — 폴더 판정 ({provider}, 질문 {len(files)}개)

이 폴더가 작업 폴더입니다. 아래 순서대로 하세요.

0. 이 폴더는 쓰기가 허용된 작업 폴더입니다(workspace-write). 파일은 모두 UTF-8(BOM) 입니다 — PowerShell 이면 `Get-Content -Raw -Encoding UTF8` 로 읽고 `Set-Content -Encoding UTF8` 로 쓰세요. 쓰기가 거부되면 그 오류 문구를 그대로 표준 출력에 적고 멈추세요(짐작으로 포기하지 마세요).
1. `판정_지시문.txt` 를 처음부터 끝까지 읽으세요(길면 나눠서). 이것이 판정 규칙입니다. `schema.json` 은 결과 파일의 JSON 형식입니다.
2. `입력/` 의 파일을 아래 순서대로 하나씩 처리하세요. 파일 하나 = 질문 하나(지시문의 [입력] 형식 그대로). 긴 파일은 나눠서 끝까지 읽으세요.
3. 질문마다 지시문대로 판정하고, 결과를 **`결과/` 에 같은 이름으로** 쓰세요 — 내용은 `schema.json` 형식의 JSON 하나뿐(설명 글 없음).
   한 질문의 결과를 쓴 뒤에 다음 질문으로 넘어가세요. 쓰고 나서 `진행.log` 에 `Q번호 끝` 한 줄을 덧붙이세요.
4. 입력 파일 · 지시문은 고치지 마세요. 건너뛰는 질문이 없어야 합니다. 어떤 질문의 결과가 형식에 안 맞으면 그 파일만 다시 쓰세요.
5. 모두 끝나면 `완료.json` 에 {{"done": ["Q13.json", ...]}} 를 쓰세요. 표준 출력에는 마지막에 "끝" 한 줄만 적으세요.

처리할 파일(순서):
{lines}
"""


def cmd_prepare(a: argparse.Namespace) -> int:
    src = RUNS / a.run / a.src
    io = src / "codex_io"
    if not (io / "판정_지시문.txt").exists():
        raise SystemExit(f"기준 판정에 codex_io/판정_지시문.txt 가 없습니다: {io}")
    qs = [int(x) for x in a.questions.split(",") if x.strip()]
    w = work_dir(a.run, a.out, a.provider)
    for sub in ("입력", "결과", "지문"):
        (w / sub).mkdir(parents=True, exist_ok=True)
    (w / "판정_지시문.txt").write_text((io / "판정_지시문.txt").read_text(encoding="utf-8"), encoding="utf-8-sig")
    from tools import judge_v4
    (w / "schema.json").write_text(json.dumps(judge_v4.output_schema(), ensure_ascii=False, indent=1), encoding="utf-8-sig")
    files, missing = [], []
    for q in qs:
        name = f"Q{q}_{a.provider}"
        inp = io / f"{name}_입력.json"
        fp = src / "codex" / f"{name}.json.fp"
        if not inp.exists():
            missing.append(f"Q{q}")
            continue
        (w / "입력" / f"Q{q}.json").write_text(inp.read_text(encoding="utf-8"), encoding="utf-8-sig")
        if fp.exists():
            shutil.copy2(fp, w / "지문" / f"Q{q}.fp")
        files.append(f"Q{q}.json")
    files = q_sorted(files)
    (w / "작업_지시.md").write_text(task_text(a.provider, files), encoding="utf-8-sig")
    (w / "준비.json").write_text(json.dumps({"version": VERSION, "run": a.run, "from": a.src, "provider": a.provider, "questions": qs,
                                              "files": files, "missing": missing, "prepared_at": datetime.now(timezone.utc).isoformat()},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"준비: {w} — 입력 {len(files)}개" + (f", 기준 판정에 입력이 없는 질문 {missing}" if missing else ""))
    return 0


def _codex_cmd(work: Path, model: str, reasoning: str, web_search: str) -> List[str]:
    from code_eval.codex_cli_client import DISABLED_FEATURES, SEARCH_HOST_FEATURES, find_codex_executable
    exe = find_codex_executable(env_value("CODEX_CLI_PATH"))
    if not exe:
        raise SystemExit("Codex CLI 실행 파일 없음 — CODEX_CLI_PATH 확인")
    live = web_search == "live"
    cmd = [exe, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "-C", str(work),   # 샌드박스는 --approve-for-me 가 workspace-write 로 정함(둘을 같이 못 씀)
           "--color", "never", "--json", "--model", model, "-c", f"model_reasoning_effort={json.dumps(reasoning)}",
           "--approve-for-me", "-c", 'sandbox_mode="workspace-write"', "-c", f"web_search={json.dumps('live' if live else 'disabled')}",   # 셸 명령 승인: 자동 검토(approve-for-me) — approval_policy=never 는 Windows 에서 "blocked by policy" 로 명령을 모두 막음(10-25)
           "-c", "project_doc_max_bytes=0", "-c", "mcp_servers={}"]
    for feature in DISABLED_FEATURES:
        if feature in ("shell_tool", "unified_exec"):
            continue   # 폴더를 읽고 결과 파일을 쓰려면 셸이 필요
        cmd += ["--disable", feature]
    for feature in SEARCH_HOST_FEATURES:   # code_mode · code_mode_host — 이 Codex 판에서는 셸 · 파일 도구가 이 호스트를 거친다(끄면 "code-mode host is disabled" 로 파일을 못 읽음, 10-25 파일럿)
        cmd += ["--enable", feature]
    cmd.append("-")
    return cmd


BATCH_NOTE = """

# 묶음 입력 (이 실행에만 해당)
- 이번 [입력] 은 질문 하나가 아니라 **질문 여러 개**입니다: {"질문_수": N, "질문들": [<입력 형식의 JSON 하나>, ...]}.
- 질문마다 위 지시문대로 **독립적으로** 판정하세요(다른 질문의 내용 · 판정을 끌어오지 않음). 각 질문의 "판정할_항목" · "판정할_턴" 은 그 질문 것만 봅니다.
- 출력은 {"questions": [{"질문번호": <그 질문의 질문번호>, "turns": [...], "conclusion": {...}}, ...]} 하나 — 질문 수와 순서가 [입력] 과 같아야 하고,
  빠지는 질문이 없어야 합니다. 각 원소의 turns · conclusion 은 위 '출력 형식' 그대로입니다.
"""


def batch_schema(single: Dict[str, Any]) -> Dict[str, Any]:
    item = {"type": "object", "additionalProperties": False, "required": ["질문번호", "turns", "conclusion"],
            "properties": {"질문번호": {"type": "integer"}, **single["properties"]}}
    return {"type": "object", "additionalProperties": False, "required": ["questions"], "properties": {"questions": {"type": "array", "items": item}}}


def cmd_run_batch(a: argparse.Namespace, w: Path) -> int:
    from tools import judge_v4
    from tools.judge_run import judges_from_env
    gpt, _jev, notes = judges_from_env()
    if gpt is None:
        raise SystemExit("Codex CLI 를 쓸 수 없습니다: " + "; ".join(notes))
    if a.model:
        gpt.model = a.model
    if a.reasoning:
        gpt.reasoning = a.reasoning
    gpt.web_search = "live" if a.web_search == "live" else "disabled"
    gpt.io_dir = w / "codex_io"
    prompt = (w / "판정_지시문.txt").read_text(encoding="utf-8-sig") + BATCH_NOTE
    prep = json.loads((w / "준비.json").read_text(encoding="utf-8"))
    files = prep["files"]
    inputs = {f: json.loads((w / "입력" / f).read_text(encoding="utf-8-sig")) for f in files}
    schema = batch_schema(judge_v4.output_schema())
    size = max(1, int(a.batch_size))
    batches = [files[i:i + size] for i in range(0, len(files), size)]
    started = time.monotonic()
    usage_total: Dict[str, Any] = {}
    calls, written, missing, errors = 0, [], [], []
    (w / "결과").mkdir(exist_ok=True)
    for bi, group in enumerate(batches, 1):
        payload = {"질문_수": len(group), "질문들": [inputs[f] for f in group]}
        tag = f"batch{bi}-{a.provider}"
        print(f"▶ 묶음 {bi}/{len(batches)} — 질문 {len(group)}개 {[int(re.search(r'\d+', f).group(0)) for f in group]} · 입력 {len(json.dumps(payload, ensure_ascii=False)):,}자", flush=True)
        t0 = time.monotonic()
        status, body, error = gpt.judge(prompt, json.dumps(payload, ensure_ascii=False, indent=1), False, tag=tag, output_schema=schema)
        calls += 1
        for k, v in (getattr(gpt, "last_usage", None) or {}).items():
            if isinstance(v, (int, float)):
                usage_total[k] = usage_total.get(k, 0) + v
        if status != "OK" or not isinstance(body, dict):
            errors.append(f"묶음 {bi}: {status} {str(error)[:160]}")
            print(f"   ⚠ 묶음 {bi} 실패: {status} {str(error)[:160]}", flush=True)
            continue
        got = {}
        for r in body.get("questions") or []:
            if isinstance(r, dict) and isinstance(r.get("질문번호"), int):
                got[r["질문번호"]] = {"turns": r.get("turns") or [], "conclusion": r.get("conclusion")}
        for f in group:
            q = int(re.search(r"\d+", f).group(0))
            if q in got:
                (w / "결과" / f).write_text(json.dumps(got[q], ensure_ascii=False, indent=1), encoding="utf-8")
                written.append(f)
            else:
                missing.append(f)
        print(f"   ✅ 묶음 {bi} 끝 — 받은 질문 {len(got)}/{len(group)} · {time.monotonic() - t0:.0f}초 · 토큰 {getattr(gpt, 'last_usage', None)}", flush=True)
    seconds = round(time.monotonic() - started, 1)
    info = {"version": VERSION, "mode": "batch", "batch_size": size, "model": gpt.model, "reasoning": gpt.reasoning, "web_search": gpt.web_search,
            "returncode": 0 if not errors else 1, "seconds": seconds, "turns": calls, "tool_calls": 0, "calls": calls, "usage": usage_total,
            "results": q_sorted(written), "missing": q_sorted(missing), "errors": errors, "done": {"done": q_sorted(written)} if written and not missing else None,
            "stderr_tail": "", "finished_at": datetime.now(timezone.utc).isoformat()}
    (w / "usage.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"끝 — 호출 {calls}번 · {seconds:.0f}초 · 결과 {len(written)}개 · 빠짐 {missing} · 사용량 {usage_total}")
    return 0 if not errors and not missing else 1


def cmd_run(a: argparse.Namespace) -> int:
    w = work_dir(a.run, a.out, a.provider)
    if a.mode == "batch":
        return cmd_run_batch(a, w)
    task = (w / "작업_지시.md").read_text(encoding="utf-8")
    model = a.model or env_value("CODEX_CLI_MODEL") or "gpt-6-luna"
    reasoning = a.reasoning or env_value("CODEX_CLI_REASONING") or "low"
    cmd = _codex_cmd(w, model, reasoning, a.web_search)
    log = (w / "run.log").open("a", encoding="utf-8")
    log.write(f"[{datetime.now().isoformat(timespec='seconds')}] {VERSION} 시작 — 모델 {model} · 추론 {reasoning} · 웹 검색 {a.web_search}\n")
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            cwd=str(w), shell=False)
    err_buf: List[str] = []
    threading.Thread(target=lambda: err_buf.append(proc.stderr.read() if proc.stderr else ""), daemon=True).start()
    proc.stdin.write(task)
    proc.stdin.close()
    usage: Dict[str, Any] = {}
    turns = 0
    tool_calls = 0
    for line in proc.stdout:
        log.write(line)
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        item = ev.get("item") or {}
        if kind == "item.completed" and item.get("type") in ("command_execution", "function_call", "shell"):
            tool_calls += 1
        if kind == "turn.completed":
            turns += 1
            u = ev.get("usage") or {}
            for k, v in u.items():
                if isinstance(v, (int, float)):
                    usage[k] = usage.get(k, 0) + v
            print(f"   ✅ 턴 {turns} 끝 — 입력 {u.get('input_tokens', '?')} · 캐시 {u.get('cached_input_tokens', '?')} · 출력 {u.get('output_tokens', '?')} 토큰, {time.monotonic() - started:.0f}초", flush=True)
        elif kind == "item.completed" and item.get("type") == "agent_message":
            print(f"   💬 Codex: {(item.get('text') or '').strip()[:100]}", flush=True)
        elif kind in ("error", "turn.failed"):
            print(f"   ⚠ Codex: {str(ev.get('message') or ev.get('error') or ev)[:160]}", flush=True)
    proc.wait()
    seconds = round(time.monotonic() - started, 1)
    results = q_sorted([p.name for p in (w / "결과").glob("Q*.json")])
    done = json.loads((w / "완료.json").read_text(encoding="utf-8")) if (w / "완료.json").exists() else None
    info = {"version": VERSION, "model": model, "reasoning": reasoning, "web_search": a.web_search, "returncode": proc.returncode, "seconds": seconds,
            "turns": turns, "tool_calls": tool_calls, "usage": usage, "results": results, "done": done, "stderr_tail": "".join(err_buf)[-800:],
            "finished_at": datetime.now(timezone.utc).isoformat()}
    (w / "usage.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    log.write(f"[{datetime.now().isoformat(timespec='seconds')}] 끝 — 종료 코드 {proc.returncode}, {seconds}초, 결과 파일 {len(results)}개\n")
    log.close()
    print(f"끝 — 종료 코드 {proc.returncode} · {seconds:.0f}초 · 결과 파일 {len(results)}개 · 도구 호출 {tool_calls}번 · 사용량 {usage}")
    return 0 if proc.returncode == 0 else 1


def cmd_merge(a: argparse.Namespace) -> int:
    w = work_dir(a.run, a.out, a.provider)
    src = RUNS / a.run / a.src
    out = RUNS / a.run / a.out
    prep = json.loads((w / "준비.json").read_text(encoding="utf-8"))
    for sub in ("codex", "decisions", "inputs", "codex_io"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    if not (out / "codex_io" / "판정_지시문.txt").exists():
        shutil.copy2(w / "판정_지시문.txt", out / "codex_io" / "판정_지시문.txt")
    ok, bad = [], []
    for f in prep["files"]:
        q = int(re.search(r"\d+", f).group(0))
        name = f"Q{q}_{a.provider}"
        res = w / "결과" / f
        try:
            doc = json.loads(res.read_text(encoding="utf-8-sig"))
            assert isinstance(doc, dict) and "turns" in doc and "conclusion" in doc
        except Exception as exc:   # 결과 없음 · 형식 아님 → 이 질문은 judge_v4 가 Codex 에 다시 묻게 둔다
            bad.append(f"Q{q}: {type(exc).__name__}")
            continue
        (out / "codex" / f"{name}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        fp = w / "지문" / f"Q{q}.fp"
        if fp.exists():
            shutil.copy2(fp, out / "codex" / f"{name}.json.fp")
        shutil.copy2(res, out / "codex_io" / f"{name}_결과.json")
        shutil.copy2(w / "입력" / f, out / "codex_io" / f"{name}_입력.json")
        for sub in ("decisions", "inputs"):
            s = src / sub / f"{name}.json"
            if s.exists():
                shutil.copy2(s, out / sub / f"{name}.json")
            for extra in (src / sub).glob(f"{name}.json.*"):
                shutil.copy2(extra, out / sub / extra.name)
        ok.append(q)
    print(f"옮김: 결과 {len(ok)}개" + (f", 못 쓴 결과 {bad}" if bad else ""))
    if not ok:
        return 1
    from tools import judge_v4
    argv = ["--run", a.run, "--set", a.set, "--questions", ",".join(str(q) for q in ok), "--providers", a.provider, "--out", a.out, "--repair"]
    print("합치기: judge_v4 " + " ".join(argv), flush=True)
    return judge_v4.main(argv)


def _last_rows(path: Path, provider: str) -> Dict[int, Dict[str, Any]]:
    """그 모델의 질문 → 마지막 줄 (모델을 먼저 걸러야 다른 모델 줄이 덮어쓰지 않는다)."""
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return {int(r["question"]): r for r in rows if r.get("provider") == provider}


def cmd_compare(a: argparse.Namespace) -> int:
    w = work_dir(a.run, a.out, a.provider)
    before = _last_rows(RUNS / a.run / a.src / "results.jsonl", a.provider)
    after = _last_rows(RUNS / a.run / a.out / "results.jsonl", a.provider)
    common = sorted(set(before) & set(after))
    same_dec = same_codex = n_dec = n_codex = 0
    verdict_change: List[str] = []
    for q in common:
        if before[q].get("verdict") != after[q].get("verdict"):
            verdict_change.append(f"Q{q} {before[q].get('verdict')}→{after[q].get('verdict')}")
        ib = {(t, i): it for t, tr in (before[q].get("turn_results") or {}).items() for i, it in (tr.get("items") or {}).items()}
        ia = {(t, i): it for t, tr in (after[q].get("turn_results") or {}).items() for i, it in (tr.get("items") or {}).items()}
        for key in set(ib) & set(ia):
            n_dec += 1
            same_dec += ib[key].get("decision") == ia[key].get("decision")
            cb, ca = ib[key].get("codex"), ia[key].get("codex")
            if cb is not None and ca is not None:
                n_codex += 1
                same_codex += cb == ca
    usage = json.loads((w / "usage.json").read_text(encoding="utf-8")) if (w / "usage.json").exists() else {}
    tok_before = Counter()
    sec_before = 0.0
    for q in common:
        cx = ((before[q].get("tokens") or {}).get("Codex") or {})
        for k in ("입력", "캐시_입력", "출력", "호출"):
            tok_before[k] += int(cx.get(k) or 0)
        sec_before += float(((before[q].get("calls") or {}).get("codex") or {}).get("seconds") or 0)
    u = usage.get("usage") or {}
    lines = [f"# 폴더 판정 비교 — {a.provider}, 질문 {len(common)}개 (기준 {a.src} → 폴더 {a.out})", "",
             f"- 항목 최종 판정 일치: {same_dec}/{n_dec} ({same_dec / max(1, n_dec) * 100:.1f}%) · Codex 판정 일치: {same_codex}/{n_codex} ({same_codex / max(1, n_codex) * 100:.1f}%)",
             f"- 질문 판정 바뀜: {len(verdict_change)}개 {verdict_change}",
             f"- 호출: 질문마다 {tok_before['호출']}번 → {usage.get('mode', '폴더')} {usage.get('calls', 1)}번(묶음 크기 {usage.get('batch_size', '-')})",
             f"- 시간: 질문마다 Codex 합 {sec_before:.0f}초 → 폴더 {usage.get('seconds', '?')}초",
             f"- 토큰(Codex): 질문마다 입력 {tok_before['입력']:,} · 캐시 {tok_before['캐시_입력']:,} · 출력 {tok_before['출력']:,} → 폴더 입력 {u.get('input_tokens', '?')} · 캐시 {u.get('cached_input_tokens', '?')} · 출력 {u.get('output_tokens', '?')}",
             f"- 결과 파일 {len(usage.get('results') or [])}개 / 완료 표시 {bool(usage.get('done'))} / 종료 코드 {usage.get('returncode')}"]
    text = "\n".join(lines) + "\n"
    (w / "비교.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prepare", "run", "merge", "compare"):
        p = sub.add_parser(name)
        p.add_argument("--run", required=True)
        p.add_argument("--out", required=True, help="판정 폴더 이름 (data/judge_runs/<실행>/<이름>)")
        p.add_argument("--provider", required=True)
        if name in ("prepare", "merge", "compare"):
            p.add_argument("--from", dest="src", required=True, help="기준 판정 폴더 이름 — 입력 · 1차 심판 저장물을 가져온다")
        if name == "prepare":
            p.add_argument("--questions", required=True)
        if name == "run":
            p.add_argument("--mode", default="batch", choices=["batch", "folder"], help="batch = 질문 N개를 표준 입력 한 번에(기본) · folder = Codex 가 폴더를 읽고 씀(Windows 샌드박스에서 막힘)")
            p.add_argument("--batch-size", default=10, type=int)
            p.add_argument("--model")
            p.add_argument("--reasoning")
            p.add_argument("--web-search", default="disabled", choices=["disabled", "live"])
        if name == "merge":
            p.add_argument("--set", default=str(ROOT / "참고자료" / "질문세트_78"))
    a = ap.parse_args(argv)
    return {"prepare": cmd_prepare, "run": cmd_run, "merge": cmd_merge, "compare": cmd_compare}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
