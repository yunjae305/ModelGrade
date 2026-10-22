"""평가가 끝나면 사용자 입력 없이 보고서까지 만든다 (사용자 요청 2026-10-20 밤).

    python tools/auto_report.py --run a1642702fccc --out v4 --set 참고자료/질문세트_78_생성프롬프트v3.3 [--poll 30] [--stale 30] [--no-repair]

끝 신호: data/judge_runs/<실행>/<out>/run.log 에 "평가 끝" 이 감시 시작 뒤에 새로 뜸(앞선 실행의 글은 세지 않음). 신호 없이 run.log 가 --stale 분
동안 안 바뀌고 판정 프로세스(judge_v4 / modelgrade start)도 없으면 있는 결과로 만든다.
순서: ① 보고서(HTML + 참고자료/결과 JSON) → ② 복구(--repair: 저장된 심판 출력으로 다시 합치고, Codex 가 답하지 않았거나 인용이 틀린 항목만 다시 묻기)
→ ③ 보고서 다시 → 참고자료/결과/판정_<실행>/ 에 복사 → 기본 브라우저로 열기. 기록은 같은 폴더의 auto-report.log, 끝나면 auto-report.done.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DONE_MARK = "평가 끝"
DEFAULT_SET = ROOT / "참고자료" / "질문세트_78_생성프롬프트v3.3"


def log(path: Path, text: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {text}"
    print(line, flush=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def done_count(run_log: Path) -> int:
    try:
        return run_log.read_text(encoding="utf-8", errors="ignore").count(DONE_MARK)
    except OSError:
        return 0


def stale_minutes(run_log: Path) -> float:
    try:
        return (time.time() - run_log.stat().st_mtime) / 60
    except OSError:
        return 0.0


def judge_running() -> bool:
    """판정 프로세스가 살아 있나 — 명령줄에 judge_v4 또는 'modelgrade.py start' 가 든 python (Windows PowerShell 로 조회).
    python 만 본다 — 조회하는 powershell 자신의 명령줄에도 'judge_v4' 가 들어 있어 늘 '판정 중' 으로 보였다(10-22)."""
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command",
                               "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*judge_v4*' -or $_.CommandLine -like '*modelgrade.py*start*' } | Select-Object -ExpandProperty ProcessId"],
                              capture_output=True, text=True, timeout=60)
        return bool(proc.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def run_step(cmd: list, logp: Path, label: str, log_file: Path) -> bool:
    log(logp, f"{label}: " + " ".join(str(c) for c in cmd[2:]))
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {label}\n")
        proc = subprocess.run(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    tail = log_file.read_text(encoding="utf-8", errors="ignore")[-1500:]
    log(logp, f"{label} exit {proc.returncode}\n{tail}")
    return proc.returncode == 0


def make_report(out_dir: Path, run_id: str, logp: Path) -> Path | None:
    ok = run_step([sys.executable, "-X", "utf8", str(ROOT / "tools" / "modelgrade.py"), "report", "--dir", str(out_dir), "--run", run_id],
                  logp, "보고서 만들기", out_dir / "auto-report.steps.log")
    html = out_dir / "report.html"
    return html if ok and html.exists() else None


def copy_out(out_dir: Path, run_id: str, logp: Path) -> None:
    from tools import result_dirs   # 결과 폴더(참고자료/결과/<시작 시각>/, 사용자 10-22) — 없는 옛 실행은 판정_<실행>/
    rd = result_dirs.find(run_id)
    if rd is not None:   # 결과 폴더는 modelgrade report 가 이미 채움 — 판정 기록만 다시 맞춤(판정 보고서는 판정보고서.html)
        log(logp, f"결과 폴더: {rd} — {', '.join(result_dirs.publish(out_dir, rd))}")
        return
    dest = ROOT / "참고자료" / "결과" / f"판정_{run_id}"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("report.html", "report.md", "summary.json", "results.jsonl", "human_decisions.json"):
        if (out_dir / name).exists():
            shutil.copy2(out_dir / name, dest / name)
    log(logp, f"복사: {dest}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", default="v4")
    ap.add_argument("--set", default=str(DEFAULT_SET), help="질문 세트 폴더 (복구 단계에 필요)")
    ap.add_argument("--providers", default="gemini,claude,gpt,aiona")
    ap.add_argument("--poll", type=int, default=30, help="확인 주기(초)")
    ap.add_argument("--stale", type=int, default=30, help="끝 신호 없이 로그가 이만큼(분) 멈추고 판정 프로세스도 없으면 있는 결과로 만든다")
    ap.add_argument("--no-repair", action="store_true", help="복구 단계(Codex 다시 묻기) 생략")
    ap.add_argument("--no-open", action="store_true", help="브라우저로 열지 않음")
    args = ap.parse_args(argv)
    out_dir = ROOT / "data" / "judge_runs" / args.run / args.out
    run_log, logp = out_dir / "run.log", out_dir / "auto-report.log"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = done_count(run_log)
    log(logp, f"감시 시작 — {run_log} 에 '{DONE_MARK}' 이 새로 뜨면(지금까지 {base}번) 보고서 → {'복구 → 보고서' if not args.no_repair else '(복구 없음)'}; "
              f"신호 없이 {args.stale}분 멈추고 판정 프로세스가 없어도 진행")
    while True:
        if done_count(run_log) > base:
            log(logp, "끝 신호 확인")
            break
        if run_log.exists() and stale_minutes(run_log) >= args.stale and not judge_running():
            log(logp, f"끝 신호 없이 {args.stale}분 넘게 멈췄고 판정 프로세스가 없음 — 있는 결과로 만든다")
            break
        time.sleep(args.poll)
    time.sleep(5)   # 마지막 줄이 디스크에 다 써지도록
    html = make_report(out_dir, args.run, logp)
    if html:
        copy_out(out_dir, args.run, logp)
    if not args.no_repair:
        # 보류의 대부분이 심판 출력 누락·인용 불일치(우리 쪽 문제)라 그 항목만 다시 묻는다 — Decisions 는 저장된 답, Codex 는 빠진 항목만 (Codex 검토 10-20)
        ok = run_step([sys.executable, "-X", "utf8", str(ROOT / "tools" / "judge_v4.py"), "--run", args.run, "--set", args.set, "--questions", "all",
                       "--providers", args.providers, "--out", args.out, "--repair"], logp, "복구(보류 항목 다시 묻기)", out_dir / "auto-report.steps.log")
        log(logp, "복구 끝" if ok else "복구 실패 — 첫 보고서 그대로")
        html2 = make_report(out_dir, args.run, logp)
        if html2:
            html = html2
            copy_out(out_dir, args.run, logp)
    (out_dir / "auto-report.done").write_text(f"{datetime.now().isoformat()} {html or '실패'}\n", encoding="utf-8")
    if html and not args.no_open:
        try:
            os.startfile(str(html))   # Windows 기본 브라우저
            log(logp, f"브라우저로 엶: {html}")
        except OSError as exc:
            log(logp, f"브라우저 열기 실패: {exc}")
    log(logp, "끝")
    return 0 if html else 1


if __name__ == "__main__":
    raise SystemExit(main())
