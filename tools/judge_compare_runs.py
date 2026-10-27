# -*- coding: utf-8 -*-
"""같은 실행의 두 판정 폴더(전 · 후)를 비교한다 — 새 규칙으로 다시 판정한 결과가 무엇을 바꿨는지 (10-22 밤, 사용자: "복구 + 멀티턴 새 판정 · 전후 비교").

    python -X utf8 tools/judge_compare_runs.py compare --run 50ca3ccccace --before v4 --after v4-r2
→ data/judge_runs/<실행>/<after>/비교_<before>→<after>.md · .json
    python -X utf8 tools/judge_compare_runs.py publish --run 50ca3ccccace --before v4 --after v4-r2 --name "새 규칙 다시 판정"
→ 참고자료/결과/<지금 시각>/ — 질문 · 답변 JSON(머리에 result.rerun_of) · report.html 두 파일 (사용자 10-27). 비교.md/json · 판정 기록은 판정 폴더에.
  본 판정의 결과 폴더(처음 시각)는 그대로 두고 새 폴더로 — 전후를 함께 두기 위해(사용자 10-22).

세는 단위: 질문(모델별 /78), 기준별 FAIL 칸, 보류 칸(종류별), 멀티턴은 턴 단위 FAIL 수도(미팅 ⑤: 멀티턴 통과 0 점검).
같은 (모델, 질문)은 results.jsonl 의 마지막 줄만 본다.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ORDER = ("aiona", "aiona2", "gpt", "claude", "gemini")
KO = {"PASS": "통과", "FAIL": "FAIL", "HOLD": "보류"}


def last_lines(path: Path) -> Dict[Tuple[str, int], Dict[str, Any]]:
    out: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            x = json.loads(line)
            out[(x["provider"], int(x["question"]))] = x
    return out


def cells(x: Dict[str, Any]) -> Dict[Tuple[int, str], Dict[str, Any]]:
    """판정 줄 → {(턴, 항목): 항목 판정} (의미 항목만; CODE FAIL 은 code_fails)."""
    out = {}
    for tn, t in (x.get("turn_results") or {}).items():
        for iid, it in (t.get("items") or {}).items():
            if isinstance(it, dict):
                out[(int(tn), iid)] = it
    return out


def hold_kind(it: Dict[str, Any]) -> str:
    from tools import report_html as R
    return R.hold_kind(it)


def compare(before: Dict[Tuple[str, int], Dict[str, Any]], after: Dict[Tuple[str, int], Dict[str, Any]]) -> Dict[str, Any]:
    providers = [p for p in ORDER if any(k[0] == p for k in after)] + sorted({k[0] for k in after} - set(ORDER))
    verdicts = {p: {"전": Counter(), "후": Counter()} for p in providers}
    changed: List[Dict[str, Any]] = []
    fail_by_item = {"전": Counter(), "후": Counter()}
    hold_by_kind = {"전": Counter(), "후": Counter()}
    code_fail = {"전": Counter(), "후": Counter()}
    turn_fail = {p: {"전": [0, 0], "후": [0, 0]} for p in providers}   # 멀티턴 턴: [FAIL 턴, 전체 턴]
    turn_pass = {p: {"전": [0, 0], "후": [0, 0]} for p in providers}   # 멀티턴 턴: [FAIL · 보류 없는 턴, 전체 턴] (사용자 10-23: 턴 통과율 병기)
    codex_calls = Counter()
    for key in sorted(after, key=lambda k: (ORDER.index(k[0]) if k[0] in ORDER else 9, k[1])):
        p, q = key
        a, b = after[key], before.get(key)
        verdicts[p]["후"][a.get("verdict")] += 1
        if b:
            verdicts[p]["전"][b.get("verdict")] += 1
        ca, cb = cells(a), cells(b) if b else {}
        for side, cc in (("후", ca), ("전", cb)):
            for (tn, iid), it in cc.items():
                if it.get("decision") == "FAIL":
                    fail_by_item[side][iid] += 1
                elif it.get("decision") == "UNVERIFIED":
                    hold_by_kind[side][it.get("kind") or hold_kind(it)] += 1
        for side, x in (("후", a), ("전", b)):
            if x:
                for c in x.get("code_fails") or []:
                    code_fail[side][c.split(":")[0]] += 1
        if int(a.get("turns") or 1) > 1:
            for side, x, cc in (("후", a, ca), ("전", b, cb)):
                if not x:
                    continue
                turns = {tn for tn, _ in cc}
                bad = {tn for (tn, _), it in cc.items() if it.get("decision") == "FAIL"}
                notok = {tn for (tn, _), it in cc.items() if it.get("decision") in ("FAIL", "UNVERIFIED")}
                turn_fail[p][side][0] += len(bad)
                turn_fail[p][side][1] += len(turns)
                turn_pass[p][side][0] += len(turns - notok)
                turn_pass[p][side][1] += len(turns)
        cx = (a.get("calls") or {}).get("codex") or {}
        codex_calls["호출"] += int(cx.get("rounds") or 0)
        codex_calls["다시 물은 횟수"] += sum(int(v or 0) for v in (cx.get("turn_asks") or {}).values()) if isinstance(cx.get("turn_asks"), dict) else 0   # 턴마다 다시 물은 횟수(정수)
        if b and a.get("verdict") != b.get("verdict"):
            fa = sorted(f"T{tn}:{iid}" for (tn, iid), it in ca.items() if it.get("decision") == "FAIL") + list(a.get("code_fails") or [])
            fb = sorted(f"T{tn}:{iid}" for (tn, iid), it in cb.items() if it.get("decision") == "FAIL") + list(b.get("code_fails") or [])
            ha = sorted(f"T{tn}:{iid}" for (tn, iid), it in ca.items() if it.get("decision") == "UNVERIFIED")
            hb = sorted(f"T{tn}:{iid}" for (tn, iid), it in cb.items() if it.get("decision") == "UNVERIFIED")
            changed.append({"question": q, "provider": p, "전": b.get("verdict"), "후": a.get("verdict"),
                            "FAIL 사라짐": sorted(set(fb) - set(fa)), "FAIL 생김": sorted(set(fa) - set(fb)),
                            "보류 사라짐": sorted(set(hb) - set(ha)), "보류 생김": sorted(set(ha) - set(hb))})
    return {"providers": providers, "verdicts": verdicts, "changed": changed, "fail_by_item": fail_by_item, "hold_by_kind": hold_by_kind,
            "code_fail": code_fail, "turn_fail": turn_fail, "turn_pass": turn_pass, "codex": dict(codex_calls), "n_after": len(after), "n_before": len(before)}


def to_md(c: Dict[str, Any], run: str, before: str, after: str) -> str:
    L = [f"# 다시 판정 전후 비교 — 실행 {run} · 전 `{before}` → 후 `{after}`", "",
         f"- 질문 판정 줄: 전 {c['n_before']} · 후 {c['n_after']} (모델 × 질문, 마지막 줄만)", f"- 후 판정의 Codex 호출 {c['codex'].get('호출', 0)}번", "",
         "## 질문 판정 (모델별 /78)", "", "| 모델 | 통과 전 → 후 | FAIL 전 → 후 | 보류 전 → 후 |", "|---|---|---|---|"]
    for p in c["providers"]:
        v = c["verdicts"][p]
        L.append(f"| {p} | " + " | ".join(f"{v['전'][k]} → {v['후'][k]}" for k in ("PASS", "FAIL", "HOLD")) + " |")
    L += ["", f"## 판정이 바뀐 질문 — {len(c['changed'])}개", ""]
    if c["changed"]:
        L += ["| 질문 | 모델 | 전 → 후 | FAIL 사라짐 | FAIL 생김 | 보류 사라짐 | 보류 생김 |", "|---|---|---|---|---|---|---|"]
        for ch in c["changed"]:
            L.append(f"| Q{ch['question']} | {ch['provider']} | {KO.get(ch['전'], ch['전'])} → {KO.get(ch['후'], ch['후'])} | "
                     + " | ".join(", ".join(ch[k]) or "-" for k in ("FAIL 사라짐", "FAIL 생김", "보류 사라짐", "보류 생김")) + " |")
    items = sorted(set(c["fail_by_item"]["전"]) | set(c["fail_by_item"]["후"]), key=lambda i: -(abs(c["fail_by_item"]["후"][i] - c["fail_by_item"]["전"][i]) * 100 + c["fail_by_item"]["후"][i]))
    L += ["", "## 기준별 FAIL 칸 (의미 기준, 변화 큰 순)", "", "| 기준 | 전 | 후 | 변화 |", "|---|---|---|---|"]
    for i in items:
        b, a = c["fail_by_item"]["전"][i], c["fail_by_item"]["후"][i]
        L.append(f"| {i} | {b} | {a} | {a - b:+d} |")
    codes = sorted(set(c["code_fail"]["전"]) | set(c["code_fail"]["후"]))
    if codes:
        L += ["", "## CODE FAIL (질문 단위, 가족별)", "", "| 검사 | 전 | 후 |", "|---|---|---|"] + [f"| {k} | {c['code_fail']['전'][k]} | {c['code_fail']['후'][k]} |" for k in codes]
    kinds = sorted(set(c["hold_by_kind"]["전"]) | set(c["hold_by_kind"]["후"]))
    L += ["", "## 보류 칸 (종류별)", "", "| 종류 | 전 | 후 |", "|---|---|---|"] + [f"| {k} | {c['hold_by_kind']['전'][k]} | {c['hold_by_kind']['후'][k]} |" for k in kinds]
    L += ["", "## 멀티턴 — 턴 통과율(FAIL · 보류 없는 턴 / 판정한 턴, 사용자 10-23) · FAIL 턴 (미팅 ⑤)", "", "| 모델 | 턴 통과 전 | 턴 통과 후 | FAIL 턴 전 | FAIL 턴 후 |", "|---|---|---|---|---|"]
    for p in c["providers"]:
        t, u = c["turn_fail"][p], c["turn_pass"][p]
        pct = lambda a, b: f"{a}/{b} ({a / b * 100:.0f}%)" if b else "-"
        L.append(f"| {p} | {pct(*u['전'])} | {pct(*u['후'])} | {t['전'][0]}/{t['전'][1]} | {t['후'][0]}/{t['후'][1]} |")
    return "\n".join(L) + "\n"


def write_compare(run: str, before: str, after: str) -> Path:
    base = ROOT / "data" / "judge_runs" / run
    c = compare(last_lines(base / before / "results.jsonl"), last_lines(base / after / "results.jsonl"))
    out_md = base / after / f"비교_{before}→{after}.md"
    out_md.write_text(to_md(c, run, before, after), encoding="utf-8")
    out_md.with_suffix(".json").write_text(json.dumps(c, ensure_ascii=False, indent=1, default=list), encoding="utf-8")
    return out_md


def publish(run_id: str, before: str, after: str, name: str, note: str = "", rd: Optional[Path] = None) -> Path:
    """다시 판정 결과를 새 결과 폴더(참고자료/결과/<지금 시각>/)로 — 본 판정 폴더는 그대로. 결과 폴더에는 질문 · 답변 JSON 과 report.html 두 파일만
    (사용자 10-27). JSON 머리의 result.rerun_of 로 result_dirs.find(실행) 이 본 결과 폴더를 그대로 찾는다. 결과 보고서는 빌더가 실행 ID 로 찾는
    결과 JSON · 판정 기록을 이 다시 판정의 것으로 바꿔 끼워 만든다. rd 를 주면 그 폴더를 다시 만든다."""
    import subprocess
    from tools import result_dirs
    from tools.export_results_json import export
    from tools.modelgrade import RUNS, load_run
    from tools.report_html import write_report
    base = ROOT / "data" / "judge_runs" / run_id
    judge_dir = base / after
    rd = rd or result_dirs.RESULTS / result_dirs.stamp()
    rd.mkdir(parents=True, exist_ok=True)
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except OSError:
        head = ""
    run = load_run(run_id)
    meta = {"version": result_dirs.VERSION, "kind": "rerun", "rerun_of": run_id, "judge_out": after, "before_out": before, "started": rd.name,
            "name": name, "code_at": head, "note": note}
    write_report(judge_dir, run)                       # 판정 보고서 · 질문별 시간 표(판정 폴더에 — 결과 폴더에는 안 둠)
    js_path = export(run_id, "결과_판정결과", None, out_dir=rd, runs_dir=RUNS, judge_dir=judge_dir, result=meta)
    write_compare(run_id, before, after)               # 비교.md/json — 판정 폴더에
    build_report(run_id, rd, js_path, judge_dir)
    return rd


def build_report(run_id: str, rd: Path, js_path: Path, judge_dir: Path) -> Path:
    """다시 판정 폴더의 결과 보고서(report.html) — 빌더가 실행 ID 로 찾는 결과 JSON · 판정 보고서 · 판정 기록(results.jsonl 등)을
    이 다시 판정의 것으로 바꿔 끼워 만든다. 판정 기록을 바꿔 끼우지 않으면 시간 · 호출 · 토큰이 본 판정(첫 결과 폴더)의 숫자로 나온다(10-27)."""
    from tools import build_result_summary as brs
    from tools import result_dirs
    keep = (brs.RESULTS, brs.results_json, brs.judge_report_file, brs.result_file)
    orig_result_file = brs.result_file
    brs.RESULTS = result_dirs.RESULTS
    brs.results_json = lambda rid: js_path                                   # 이 폴더의 결과 JSON
    brs.judge_report_file = lambda rid: judge_dir / "report.html"            # 이 다시 판정의 판정 보고서
    brs.result_file = lambda rid, name, flat=False: (judge_dir / name) if (judge_dir / name).exists() else orig_result_file(rid, name, flat)   # 이 판정 기록
    try:
        brs.build(run_id, report_dir=rd)
    finally:
        brs.RESULTS, brs.results_json, brs.judge_report_file, brs.result_file = keep
    return rd / result_dirs.REPORT


def main(argv: List[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("compare", "publish"):
        sp = sub.add_parser(name)
        sp.add_argument("--run", required=True)
        sp.add_argument("--before", required=True, help="전 판정 폴더 이름 (data/judge_runs/<실행>/<이름>)")
        sp.add_argument("--after", required=True, help="후 판정 폴더 이름")
        if name == "publish":
            sp.add_argument("--name", required=True, help="결과 JSON 머리(result)에 적는 이름")
            sp.add_argument("--note", default="")
            sp.add_argument("--into", default="", help="새 폴더 대신 이 결과 폴더를 다시 만듦 (참고자료/결과/<시각>)")
    args = ap.parse_args(argv)
    if args.cmd == "compare":
        out_md = write_compare(args.run, args.before, args.after)
        print(out_md.read_text(encoding="utf-8"))
        print(f"저장: {out_md}")
        return 0
    rd = publish(args.run, args.before, args.after, args.name, args.note, Path(args.into) if args.into else None)
    print(f"결과 폴더: {rd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
