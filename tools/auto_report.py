"""평가가 끝나면 사용자 입력 없이 보고서까지 만든다 (사용자 요청 2026-10-20 밤).

    python tools/auto_report.py --run a1642702fccc --out v4 [--poll 30] [--stale 30]

끝 신호: data/judge_runs/<실행>/<out>/run.log 에 "평가 끝". 신호 없이 run.log 가 --stale 분 동안 안 바뀌면(평가 프로세스가 죽은 경우)
있는 결과로 만든다. 보고서는 `python tools/modelgrade.py report <폴더>` (HTML + 참고자료/결과 JSON) 로 만들고, 보고서 묶음을
참고자료/결과/판정_<실행>/ 에 복사한 뒤 HTML 을 기본 브라우저로 연다. 기록은 같은 폴더의 auto-report.log, 끝나면 auto-report.done.
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


def log(path: Path, text: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {text}"
    print(line, flush=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def finished(run_log: Path) -> bool:
    try:
        return DONE_MARK in run_log.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def stale_minutes(run_log: Path) -> float:
    try:
        return (time.time() - run_log.stat().st_mtime) / 60
    except OSError:
        return 0.0


def make_report(out_dir: Path, run_id: str, logp: Path) -> Path | None:
    cmd = [sys.executable, "-X", "utf8", str(ROOT / "tools" / "modelgrade.py"), "report", "--dir", str(out_dir)]
    log(logp, "보고서 만들기: " + " ".join(cmd[3:]))
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    log(logp, proc.stdout[-4000:])
    if proc.returncode:
        log(logp, f"보고서 실패 (exit {proc.returncode}): {proc.stderr[-2000:]}")
        return None
    html = out_dir / "report.html"
    if not html.exists():
        log(logp, "report.html 이 없음")
        return None
    dest = ROOT / "참고자료" / "결과" / f"판정_{run_id}"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("report.html", "report.md", "summary.json", "results.jsonl"):
        if (out_dir / name).exists():
            shutil.copy2(out_dir / name, dest / name)
    log(logp, f"복사: {dest}")
    return html


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", default="v4")
    ap.add_argument("--poll", type=int, default=30, help="확인 주기(초)")
    ap.add_argument("--stale", type=int, default=30, help="끝 신호 없이 로그가 이만큼(분) 멈추면 있는 결과로 만든다")
    ap.add_argument("--no-open", action="store_true", help="브라우저로 열지 않음")
    args = ap.parse_args(argv)
    out_dir = ROOT / "data" / "judge_runs" / args.run / args.out
    run_log, logp = out_dir / "run.log", out_dir / "auto-report.log"
    out_dir.mkdir(parents=True, exist_ok=True)
    log(logp, f"감시 시작 — {run_log} 에 '{DONE_MARK}' 이 뜨거나 {args.stale}분 동안 멈추면 보고서를 만든다")
    while True:
        if finished(run_log):
            log(logp, "끝 신호 확인")
            break
        if run_log.exists() and stale_minutes(run_log) >= args.stale:
            log(logp, f"끝 신호 없이 {args.stale}분 넘게 멈춤 — 있는 결과로 만든다")
            break
        time.sleep(args.poll)
    time.sleep(5)   # 마지막 줄이 디스크에 다 써지도록
    html = make_report(out_dir, args.run, logp)
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
