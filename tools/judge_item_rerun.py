"""한 항목만 지금 판정 프롬프트로 다시 판정 — Codex 와 Claude CLI 둘 다 (judge-item-rerun-v1, 사용자 10-22: "반복 칸만 새 프롬프트로 다시 돌려봐").

본 판정(v4)에서 1차 심판이 2차로 넘긴 그 항목 칸만 다시 본다. 1차 심판 결과는 저장된 것을 그 항목만 남겨 다시 쓰고(Jev · Decisions 다시 안 부름),
판정 흐름은 judge_v4.screen_one 그대로(첫 호출 · 빠진 턴 다시 묻기 · 인용 확인 · 상한 · 합치기) — 2차 심판만 Codex(judges_from_env) · Claude CLI(judge_claude_compare.ClaudeJudge).

    python -X utf8 tools/judge_item_rerun.py judge --questions 1,2,3        # 10~20문항씩
    python -X utf8 tools/judge_item_rerun.py compare                        # 전(v4 · v4-claude-sonnet) → 후 비교 → 비교.md · 비교.json

→ data/judge_runs/<실행>/<out>/codex/ · claude/ (각각 results.jsonl · decisions/ · codex/ · codex_io/ · run.log)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import judge_claude_compare as jc  # noqa: E402
from tools import judge_inputs as ji  # noqa: E402
from tools import judge_v4 as jv  # noqa: E402

VERSION = "judge-item-rerun-v1"
RUN, ITEM, OUT = "50ca3ccccace", "J1-4.b", "v4-j14b-rerun"
BEFORE = {"codex": "v4", "claude": "v4-claude-sonnet"}   # 고치기 전 판정
LABEL = {"gpt": "GPT", "claude": "Claude", "gemini": "Gemini", "aiona": "AIONA"}
LONG_TOKENS = 272_000


def latest(path: Path) -> Dict[Tuple[str, int], Dict[str, Any]]:
    return jc.latest_lines(path)


def item_of(line: Dict[str, Any], n: int, item: str) -> Dict[str, Any]:
    return (((line.get("turn_results") or {}).get(str(n)) or {}).get("items") or {}).get(item) or {}


def flagged_cells(run_id: str, item: str) -> Dict[Tuple[str, int], List[int]]:
    """본 판정에서 1차 심판이 그 항목을 2차로 넘긴 칸 — {(모델, 질문): [턴…]}."""
    out: Dict[Tuple[str, int], List[int]] = {}
    for k, l in latest(ROOT / "data" / "judge_runs" / run_id / BEFORE["codex"] / "results.jsonl").items():
        for n in (l.get("turn_results") or {}):
            if item_of(l, int(n), item).get("flagged"):
                out.setdefault(k, []).append(int(n))
    return {k: sorted(v) for k, v in out.items()}


def cmd_judge(args: argparse.Namespace) -> int:
    from tools.judge_run import judges_from_env
    run = json.loads((ROOT / "data" / "comparison_runs" / f"{args.run}.json").read_text(encoding="utf-8"))
    set_dir = jc.find_set_dir(run)
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    jv.QUESTION_TARGETS.clear()
    jv.QUESTION_TARGETS.update(jv.question_targets(groups))
    rows = jv.flatten(groups)
    prompt_file = Path(args.prompt) if args.prompt else jv.PROMPT_V4   # 기본은 지금 판정 프롬프트, --prompt 로 다른 판(예: 고치기 전)
    prompt = prompt_file.read_text(encoding="utf-8")
    fixed = json.loads(jv.JEV_V4.read_text(encoding="utf-8"))
    base = ROOT / "data" / "judge_runs" / args.run / BEFORE["codex"]
    root = ROOT / "data" / "judge_runs" / args.run / args.out
    cells = flagged_cells(args.run, args.item)
    qs = [int(x) for x in args.questions.split(",") if x.strip()]
    # 심판은 (심판, 모델)마다 따로 — Codex 실행기의 last_usage 가 한 칸이라 네 모델이 동시에 쓰면 토큰이 섞인다
    judges: Dict[Tuple[str, str], Any] = {}
    for p in LABEL:
        codex, _jev, notes = judges_from_env()
        if codex is None:
            raise SystemExit("Codex CLI 를 쓸 수 없습니다: " + "; ".join(notes))
        judges[("codex", p)] = codex
        judges[("claude", p)] = jc.ClaudeJudge(jc.find_claude(), calls_log=root / "claude" / "claude_calls.jsonl")
    for (name, _p), g in judges.items():
        d = root / name
        for sub in ("inputs", "codex", "jev", "decisions", "codex_io"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        g.files_dir, g.io_dir = set_dir / "files", d / "codex_io"
    codex = judges[("codex", "gpt")]
    jv._LOG["path"] = str(root / "run.log")
    jv.say = jv.log_say
    import tools.judge_run as jr
    jr.say = jv.log_say
    lock = threading.Lock()
    done = {name: {k for k, l in latest(root / name / "results.jsonl").items() if l.get("status") == "judged" and not l.get("judge_error")}
            for name in BEFORE}

    def work(name: str, provider: str) -> None:
        d = root / name
        data = {r.get("case_id"): r for r in (run.get("datasets") or {}).get(provider) or [] if isinstance(r, dict)}
        for q in qs:
            if (provider, q) not in cells or (provider, q) in done[name]:
                continue
            fname = f"Q{q}_{provider}.json"
            saved = json.loads((base / "decisions" / fname).read_text(encoding="utf-8"))
            only = {k: ({args.item: v.get(args.item)} if isinstance(v, dict) and args.item in v else {})
                    for k, v in saved.items() if str(k).isdigit()}   # 1차 결과에서 그 항목만 남김 → 2차로 그 항목만 넘어감
            (d / "decisions" / fname).write_text(json.dumps({**only, "_first_judge": saved.get("_first_judge") or {}}, ensure_ascii=False, indent=1),
                                                encoding="utf-8")
            turns = sorted([r for r in rows if ji._question(r["case_id"]) == q], key=ji._turn)
            answers = {t["case_id"]: (data.get(t["case_id"]) or {}).get("answer") or "" for t in turns}
            deliveries = {t["case_id"]: ji.delivery_note(data.get(t["case_id"])) for t in turns}
            try:
                line = {**jv.screen_one(q, provider, turns, answers, deliveries, [], prompt, fixed, judges[(name, provider)], None, d,
                                        reuse_codex=True, reuse_decisions=True), "status": "judged"}
            except Exception as exc:
                line = {"version": jv.VERSION, "provider": provider, "question": q, "status": "error", "verdict": "HOLD",
                        "reason": f"판정 실행 오류: {type(exc).__name__}: {str(exc)[:200]}"}
                jv.say(f"⚠ [{name}] Q{q} {provider}: {line['reason']}")
            with lock, (d / "results.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({**line, "rerun": {"version": VERSION, "judge2": name, "item": args.item},
                                    "at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")

    import subprocess
    try:   # 판정에 쓴 코드 · 프롬프트에 커밋 안 된 변경이 있으면 기록 (다른 세션이 고치는 중일 수 있음)
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "tools/judge_v4.py", "tools/judge_split.py", "참고자료/프롬프트/judge_prompt_v4.txt"],
                               cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8").stdout.splitlines()
        dirty = [x.strip() for x in dirty if x.strip()]
    except OSError:
        dirty = []
    t0 = time.monotonic()
    jv.say(f"{args.item} 다시 판정 — 질문 {len(qs)}개 · 심판 Codex({codex.model} · {codex.reasoning}) · Claude CLI({jc.MODEL} · {jc.EFFORT}) · 로그 {root / 'run.log'}")
    threads = [threading.Thread(target=work, args=(name, p), name=f"{name}-{p}") for name, p in judges]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with (root / "timing.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "item-rerun", "item": args.item, "questions": qs, "seconds": round(time.monotonic() - t0, 1), "uncommitted": dirty, "prompt": str(prompt_file),
                            "at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
    jv.say(f"묶음 끝 — 벽시계 {(time.monotonic() - t0) / 60:.0f}분")
    return 0


# ------------------------------------------------------------------ 비교 ------------------------------------------------------------------
def cell_kind(run: Dict[str, Any], groups: List[Dict[str, Any]], q: int, n: int, inputs: Path) -> str:
    g = next((x for x in groups if int(x["id"]) == q), {})
    if len(g.get("turns") or []) > 1:
        flow = g.get("flow") or []
        return "멀티턴 형식 수정 턴" if n - 1 < len(flow) and flow[n - 1] == "fmt" else "멀티턴 다른 턴"
    doc = next(iter(inputs.glob(f"Q{q}_*.json")), None)
    tokens = json.loads(doc.read_text(encoding="utf-8"))["턴"][0].get("입력_추정토큰") if doc else 0
    return "롱컨텍스트" if (tokens or 0) >= LONG_TOKENS else "단일"


def cmd_compare(args: argparse.Namespace) -> int:
    run = json.loads((ROOT / "data" / "comparison_runs" / f"{args.run}.json").read_text(encoding="utf-8"))
    groups = json.loads((jc.find_set_dir(run) / "questions.json").read_text(encoding="utf-8"))
    jr_ = ROOT / "data" / "judge_runs" / args.run
    root = jr_ / args.out
    cells = flagged_cells(args.run, args.item)
    if args.before_out:   # 같은 방식(그 항목만 묻기)으로 다른 프롬프트를 쓴 다시 판정과 비교
        before = {name: latest(jr_ / args.before_out / name / "results.jsonl") for name in BEFORE}
    else:
        before = {name: latest(jr_ / sub / "results.jsonl") for name, sub in BEFORE.items()}
    after = {name: latest(root / name / "results.jsonl") for name in BEFORE}
    rows = []
    for (p, q), ns in sorted(cells.items(), key=lambda kv: (kv[0][1], list(LABEL).index(kv[0][0]))):
        if not all((p, q) in after[name] for name in BEFORE):
            continue
        for n in ns:
            r = {"provider": p, "question": q, "turn": n, "kind": cell_kind(run, groups, q, n, jr_ / BEFORE["codex"] / "inputs")}
            for name in BEFORE:
                b, a = item_of(before[name].get((p, q)) or {}, n, args.item), item_of(after[name][(p, q)], n, args.item)
                r[f"{name}_before"], r[f"{name}_after"] = b.get("decision"), a.get("decision")
                r[f"{name}_reason"] = (a.get("reason") or "").replace("\n", " ")
            rows.append(r)
    kinds = ["멀티턴 형식 수정 턴", "멀티턴 다른 턴", "롱컨텍스트", "단일"]

    def tally(rs: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"칸": len(rs), **{f"{n}_{w}_FAIL": sum(1 for r in rs if r[f"{n}_{w}"] == "FAIL") for n in BEFORE for w in ("before", "after")},
                "갈림_before": sum(1 for r in rs if r["codex_before"] != r["claude_before"]),
                "갈림_after": sum(1 for r in rs if r["codex_after"] != r["claude_after"])}

    tot = tally(rows)
    per = {k: tally([r for r in rows if r["kind"] == k]) for k in kinds}
    # 시간 · 토큰 — 이번 다시 판정의 2차 심판 호출만
    cost: Dict[str, Counter] = {}
    for name in BEFORE:
        c = cost.setdefault(name, Counter())
        for l in after[name].values():
            cx = (l.get("calls") or {}).get("codex") or {}
            c["seconds"] += cx.get("seconds") or 0
            c["calls"] += cx.get("rounds") or 0
            for k, v in cx.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and "token" in k:
                    c[k] += v
    walls = [json.loads(x) for x in (root / "timing.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()] if (root / "timing.jsonl").exists() else []
    still = [r for r in rows if r["codex_after"] != r["claude_after"]]
    res = {"version": VERSION, "run": args.run, "item": args.item, "prompt": jv.PROMPT_V4.name, "cells": len(rows), "total": tot, "per_kind": per,
           "cost": {k: dict(v) for k, v in cost.items()}, "wall_seconds": round(sum(w.get("seconds") or 0 for w in walls), 1), "still_split": still}
    res["before"] = args.before_out or "본 판정(v4 · v4-claude-sonnet, 항목 전부를 한 번에)"
    name_suffix = f"_{args.before_out}" if args.before_out else ""
    (root / f"비교{name_suffix}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    L = [f"# {args.item} 다시 판정 — 고친 판정 프롬프트(요구된 구성 · 두괄식 예외) · 실행 {args.run}", "",
         f"- 대상: 본 판정에서 1차 심판이 2차로 넘긴 {args.item} 칸 {len(rows)}개 (1차 결과는 저장된 것 그대로, 2차만 다시)",
         f"- 전: {res['before']} · 후: {args.out} (프롬프트 {', '.join(sorted({Path(w.get('prompt') or '').name for w in walls if w.get('prompt')})) or 'judge_prompt_v4.txt'})",
         "- 2차 심판: Codex gpt-6-luna · low / Claude CLI claude-sonnet-5-5 · effort low",
         *([f"- 판정 코드: {args.note}"] if args.note else []),
         *([f"- 판정 때 커밋 안 된 파일: {', '.join(sorted({d for w in walls for d in (w.get('uncommitted') or [])}))}"]
           if any(w.get("uncommitted") for w in walls) else []), "",
         "| 칸 종류 | 칸 | Codex FAIL 전 → 후 | Claude FAIL 전 → 후 | 두 심판이 갈린 칸 전 → 후 |", "|---|---|---|---|---|"]
    for k, t in [*per.items(), ("합계", tot)]:
        L.append(f"| {k} | {t['칸']} | {t['codex_before_FAIL']} → {t['codex_after_FAIL']} | {t['claude_before_FAIL']} → {t['claude_after_FAIL']} | "
                 f"{t['갈림_before']} → {t['갈림_after']} |")
    L += ["", "판정 분포 (후): Codex " + str(dict(Counter(r["codex_after"] for r in rows))) + " · Claude " + str(dict(Counter(r["claude_after"] for r in rows))),
          "", "## 아직 갈린 칸", "", "| 칸 | 종류 | Codex (전→후) | Claude (전→후) | Codex 사유 | Claude 사유 |", "|---|---|---|---|---|---|"]
    for r in still:
        L.append(f"| Q{r['question']} {LABEL[r['provider']]} T{r['turn']} | {r['kind']} | {r['codex_before']}→{r['codex_after']} | {r['claude_before']}→{r['claude_after']} | "
                 f"{r['codex_reason'][:150]} | {r['claude_reason'][:150]} |")
    L += ["", "## 걸린 시간 · 토큰 (이번 다시 판정의 2차 심판)", "", f"- 벽시계 {res['wall_seconds'] / 60:.0f}분 (두 심판 · 네 모델 동시)"]
    for name, c in cost.items():
        L.append(f"- {name}: 호출 {c['calls']}번 · 판정 시간 합 {c['seconds'] / 60:.0f}분 · " + " · ".join(f"{k} {v:,}" for k, v in sorted(c.items()) if "token" in k))
    (root / f"비교{name_suffix}.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    return 0



# ------------------------------------------------------------------ 결과 폴더 ------------------------------------------------------------------
def md_to_html(md: str) -> str:
    """비교.md 를 화면으로 — 제목 · 목록 · 표 · 문단만 (이 도구가 쓰는 마크다운)."""
    import html as H
    out: List[str] = []
    rows: List[List[str]] = []

    def flush() -> None:
        if rows:
            head, body = rows[0], [r for r in rows[2:]]
            out.append('<div class="tw"><table><thead><tr>' + "".join(f"<th>{H.escape(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{H.escape(c)}</td>" for c in r) + "</tr>" for r in body) + "</tbody></table></div>")
            rows.clear()

    for line in md.splitlines():
        if line.startswith("|"):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
            continue
        flush()
        if line.startswith("## "):
            out.append(f"<h2>{H.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{H.escape(line[2:])}</h1>")
        elif line.startswith("- "):
            out.append(f"<p class=li>{H.escape(line[2:])}</p>")
        elif line.strip():
            out.append(f"<p>{H.escape(line)}</p>")
    flush()
    return "\n".join(out)


def cmd_publish(args: argparse.Namespace) -> int:
    """결과 폴더 = 참고자료/결과/<처음 시작 시각>/ (사용자 10-22 양식) — run.json · report.html · 비교 · 두 판정의 결과 줄.
    run.json 에는 run_id 대신 compare_of — result_dirs.find(실행) 이 본 결과 폴더를 그대로 찾게."""
    import shutil
    from tools import result_dirs
    jr_ = ROOT / "data" / "judge_runs" / args.run
    root = jr_ / args.out
    ptr = root / "result_dir.json"
    if ptr.exists():
        name = json.loads(ptr.read_text(encoding="utf-8"))["name"]
    else:
        name = result_dirs.stamp(datetime.fromtimestamp(root.stat().st_ctime))   # 첫 판정 명령이 판정 폴더를 만든 시각
        ptr.write_text(json.dumps({"name": name}, ensure_ascii=False), encoding="utf-8")
    rd = result_dirs.RESULTS / name
    rd.mkdir(parents=True, exist_ok=True)
    runs = {"새프롬프트": args.out, **({"옛프롬프트": args.before_out} if args.before_out else {})}
    walls = {k: [json.loads(x) for x in (jr_ / v / "timing.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()] for k, v in runs.items()}
    meta = {"version": VERSION, "kind": "item-rerun", "compare_of": args.run, "item": args.item, "started": name,
            "name": f"{args.item} 다시 판정 — 고친 판정 프롬프트 효과 확인 (사용자 10-22: '반복 칸만 새 프롬프트로 다시 돌려봐')",
            "judge2": {"codex": "gpt-6-luna · low", "claude": f"{jc.MODEL} · {jc.EFFORT}"},
            "runs": {k: {"judge_out": v, "prompt": sorted({Path(w.get("prompt") or "").name for w in walls[k]}),
                         "uncommitted": sorted({d for w in walls[k] for d in (w.get("uncommitted") or [])})} for k, v in runs.items()},
            "prompts": {"새프롬프트": "참고자료/프롬프트/judge_prompt_v4.txt @ ef10736", "옛프롬프트": "git show ef10736~1:참고자료/프롬프트/judge_prompt_v4.txt"},
            "note": args.note}
    (rd / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    for k, v in runs.items():
        for judge in BEFORE:
            shutil.copy2(jr_ / v / judge / "results.jsonl", rd / f"{k}_{judge}_results.jsonl")
    docs = [("비교_본판정→새프롬프트", root / "비교.md", root / "비교.json")]
    if args.before_out:
        docs.append(("비교_옛프롬프트→새프롬프트", root / f"비교_{args.before_out}.md", root / f"비교_{args.before_out}.json"))
    body = []
    for label, md, js_ in docs:
        shutil.copy2(md, rd / f"{label}.md")
        shutil.copy2(js_, rd / f"{label}.json")
        body.append(f'<section><div class="tag">{label}</div>{md_to_html(md.read_text(encoding="utf-8"))}</section>')
    if args.summary:
        body.insert(0, f'<section class="sum"><h1>요약</h1>{md_to_html(Path(args.summary).read_text(encoding="utf-8"))}</section>')
    css = """
:root { --bg:#fbfaf7; --fg:#1d1d1b; --mut:#6b6a66; --line:#e3e0d8; --card:#fff; --acc:#2f5bd3; }
@media (prefers-color-scheme: dark) { :root { --bg:#17171a; --fg:#ecebe6; --mut:#a3a19b; --line:#34343a; --card:#202024; --acc:#8fb0ff; } }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 "Noto Sans KR", system-ui, sans-serif; }
main { max-width:1150px; margin:0 auto; padding:28px 16px 60px; }
h1 { font-size:21px; margin:6px 0 8px; } h2 { font-size:17px; margin:24px 0 8px; }
section { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 18px; margin:18px 0; }
section.sum { border-color:var(--acc); }
.tag { color:var(--acc); font-size:12px; font-weight:600; }
p { margin:4px 0; } p.li { padding-left:14px; text-indent:-10px; } p.li::before { content:"· "; color:var(--mut); }
.tw { overflow-x:auto; } table { border-collapse:collapse; width:100%; margin:8px 0; }
th, td { border:1px solid var(--line); padding:6px 9px; text-align:left; vertical-align:top; font-size:13.5px; }
thead th { background:color-mix(in srgb, var(--acc) 8%, var(--card)); }
"""
    (rd / result_dirs.REPORT).write_text(
        '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{args.item} 다시 판정</title>\n<style>{css}</style></head><body><main>\n" + "\n".join(body) + "\n</main></body></html>\n", encoding="utf-8")
    print(f"결과 폴더: {rd}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("judge")
    c = sub.add_parser("compare")
    pb = sub.add_parser("publish", help="결과 폴더(참고자료/결과/<처음 시작 시각>/)에 모으기")
    pb.add_argument("--before-out", default="", help="같이 넣을 옛 프롬프트 다시 판정 폴더 이름")
    pb.add_argument("--note", default="")
    pb.add_argument("--summary", default="", help="report.html 맨 위에 넣을 요약 마크다운 파일")
    for x in (j, c, pb):
        x.add_argument("--run", default=RUN)
        x.add_argument("--item", default=ITEM)
        x.add_argument("--out", default=OUT)
    j.add_argument("--questions", required=True, help="질문 번호 쉼표 목록 — 한 번에 10~20개")
    c.add_argument("--note", default="", help="비교.md 에 남길 판정 코드 메모 (예: 다른 세션의 미커밋 변경으로 판정)")
    c.add_argument("--before-out", default="", help="'전'으로 쓸 다시 판정 폴더 이름 (없으면 본 판정 v4 · v4-claude-sonnet)")
    j.add_argument("--prompt", default="", help="판정 프롬프트 파일 (없으면 참고자료/프롬프트/judge_prompt_v4.txt)")
    a = ap.parse_args(argv)
    if a.cmd == "judge":
        n = len([x for x in a.questions.split(",") if x.strip()])
        if n > 20:
            raise SystemExit(f"한 번에 20문항까지 ({n}개)")
        return cmd_judge(a)
    if a.cmd == "publish":
        return cmd_publish(a)
    return cmd_compare(a)


if __name__ == "__main__":
    raise SystemExit(main())
