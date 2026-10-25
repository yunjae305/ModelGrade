# -*- coding: utf-8 -*-
"""폴더 판정 (judge-folder-v1) — 질문마다 Codex 를 부르는 대신, 한 모델의 입력 파일을 폴더에 모아 두고 Codex 세션 하나가
폴더를 읽어 차례로 판정 · 결과 파일을 쓰게 한다 (10-23 미팅 6번: "파일로 썰어 놓고 한번 읽어 보다가 알아서 다 해라", 사용자 10-25 "구현해").

    python -X utf8 tools/judge_folder.py prepare --run 50ca3ccccace --from v4-r3 --out folder-pilot --provider aiona --questions 13,22,24
    python -X utf8 tools/judge_folder.py run     --run 50ca3ccccace --out folder-pilot --provider aiona
    python -X utf8 tools/judge_folder.py merge   --run 50ca3ccccace --from v4-r3 --out folder-pilot --provider aiona --set 참고자료/질문세트_78
    python -X utf8 tools/judge_folder.py compare --run 50ca3ccccace --from v4-r3 --out folder-pilot --provider aiona

- prepare: 기준 판정(--from)의 codex_io/ 에 남은 '그 질문에 Codex 가 받은 입력'(걸린 항목 포함)을 작업 폴더 `입력/Q{n}.json` 으로, 지시문 · 출력 형식(schema) ·
  작업 지시(작업_지시.md)를 함께 둔다 → 질문마다 보내던 것과 **같은 입력 · 같은 지시문**이라 차이는 호출 구조뿐.
- run: `codex exec` 한 번(작업 폴더, workspace-write 샌드박스 — 결과 파일을 써야 함, 셸 도구만 켬). 이벤트를 run.log 에, 사용량 · 시간을 usage.json 에.
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
    shutil.copy2(io / "판정_지시문.txt", w / "판정_지시문.txt")
    from tools import judge_v4
    (w / "schema.json").write_text(json.dumps(judge_v4.output_schema(), ensure_ascii=False, indent=1), encoding="utf-8")
    files, missing = [], []
    for q in qs:
        name = f"Q{q}_{a.provider}"
        inp = io / f"{name}_입력.json"
        fp = src / "codex" / f"{name}.json.fp"
        if not inp.exists():
            missing.append(f"Q{q}")
            continue
        shutil.copy2(inp, w / "입력" / f"Q{q}.json")
        if fp.exists():
            shutil.copy2(fp, w / "지문" / f"Q{q}.fp")
        files.append(f"Q{q}.json")
    files = q_sorted(files)
    (w / "작업_지시.md").write_text(task_text(a.provider, files), encoding="utf-8")
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
    cmd = [exe, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--sandbox", "workspace-write", "-C", str(work),
           "--color", "never", "--json", "--model", model, "-c", f"model_reasoning_effort={json.dumps(reasoning)}",
           "-c", 'approval_policy="never"', "-c", f"web_search={json.dumps('live' if live else 'disabled')}",
           "-c", "project_doc_max_bytes=0", "-c", "mcp_servers={}"]
    for feature in DISABLED_FEATURES:
        if feature in ("shell_tool", "unified_exec"):
            continue   # 폴더를 읽고 결과 파일을 쓰려면 셸이 필요
        cmd += ["--disable", feature]
    for feature in SEARCH_HOST_FEATURES:
        cmd += ["--enable" if live else "--disable", feature]
    cmd.append("-")
    return cmd


def cmd_run(a: argparse.Namespace) -> int:
    w = work_dir(a.run, a.out, a.provider)
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


def _last_rows(path: Path) -> Dict[int, Dict[str, Any]]:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return {int(r["question"]): r for r in rows}


def cmd_compare(a: argparse.Namespace) -> int:
    w = work_dir(a.run, a.out, a.provider)
    before = {q: r for q, r in _last_rows(RUNS / a.run / a.src / "results.jsonl").items() if r.get("provider") == a.provider}
    after = {q: r for q, r in _last_rows(RUNS / a.run / a.out / "results.jsonl").items() if r.get("provider") == a.provider}
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
             f"- 호출: 질문마다 {tok_before['호출']}번 → 폴더 1번(도구 호출 {usage.get('tool_calls', '?')}번 · 턴 {usage.get('turns', '?')})",
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
            p.add_argument("--model")
            p.add_argument("--reasoning")
            p.add_argument("--web-search", default="disabled", choices=["disabled", "live"])
        if name == "merge":
            p.add_argument("--set", default=str(ROOT / "참고자료" / "질문세트_78"))
    a = ap.parse_args(argv)
    return {"prepare": cmd_prepare, "run": cmd_run, "merge": cmd_merge, "compare": cmd_compare}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
