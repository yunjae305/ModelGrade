"""평가 결과 폴더 (result-dirs-v1, 사용자 10-22) — 평가를 시작할 때마다 `참고자료/결과/<처음 시작한 시각>/` 하나를 만들고
그 안에 결과 파일을 모은다: report.html(결과 보고서 — 결과 요약의 상세 분석 장면, 원문은 원문/) · 판정보고서.html(모든 판정 기록 ·
보류를 사람이 정하는 화면) · <실행>_결과_판정결과.json · results.jsonl · summary.json · 판정보고서.md · human_decisions.json ·
수집기록/(답변 수집 로그 · 다시 받기 기록). 결과 폴더는 통째로 git 에 올린다 (사용자 10-22 — 로그도 .gitignore 예외).

- 폴더 이름: 이 PC 시각(한국) `YYYY-MM-DD_HH-MM-SS-mmm` (밀리초까지, 윈도우 폴더 이름에 쓸 수 없는 콜론 대신 '-').
- 멈춘 평가를 같은 명령으로 이어 돌리면 처음 시작한 시각 폴더를 계속 쓴다 — 실행 JSON 의 `result_dir` 에 폴더 이름을 적어 둔다.
- 폴더마다 run.json(실행 ID · 시작 시각 · 질문 세트 · 모델 선택)으로 실행과 잇는다 — 결과 요약 등은 실행 ID 로 폴더를 찾는다.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "참고자료" / "결과"
VERSION = "result-dirs-v1"
REPORT = "report.html"             # 결과 보고서 (tools/build_result_summary --report)
JUDGE_REPORT = "판정보고서.html"     # 판정 보고서 (tools/report_html) — 판정 폴더에서는 report.html
#: 판정 폴더(data/judge_runs/<실행>/<out>)에서 결과 폴더로 옮겨 두는 파일 — {판정 폴더 이름: 결과 폴더 이름}
JUDGE_FILES = {"report.html": JUDGE_REPORT, "report.md": "판정보고서.md", "summary.json": "summary.json",
               "results.jsonl": "results.jsonl", "human_decisions.json": "human_decisions.json"}


COLLECT_DIR = "수집기록"   # 답변 수집 기록 (data/judge_runs/<실행>/ 에서 옮김 — data/ 는 git 에 안 올라감)
COLLECT_PATTERNS = ("collect*.log", "*recheck*.json", "*recheck*.jsonl")   # 모델마다 터미널에 보인 그대로 · 다시 받기(재확인) 기록


def stamp(now: Optional[datetime] = None) -> str:
    """'2026-10-22_14-05-33-123' — 이 PC 시각, 밀리초까지."""
    now = now or datetime.now()
    return now.strftime("%Y-%m-%d_%H-%M-%S") + f"-{now.microsecond // 1000:03d}"


def find(run_id: str, base: Path = RESULTS) -> Optional[Path]:
    """실행 ID 의 결과 폴더(run.json 으로 찾음) — 없으면 None."""
    if not base.exists():
        return None
    for d in sorted(base.iterdir(), reverse=True):
        meta = d / "run.json"
        if d.is_dir() and meta.exists():
            try:
                if json.loads(meta.read_text(encoding="utf-8")).get("run_id") == run_id:
                    return d
            except (OSError, ValueError):
                continue
    return None


def ensure(run: Dict[str, Any], base: Path = RESULTS, now: Optional[datetime] = None) -> Path:
    """실행의 결과 폴더 — 실행에 `result_dir` 이 있으면 그 폴더(이어 돌리기), 없으면 지금 시각으로 새로 만들고 실행에 적는다(저장은 부르는 쪽).
    run.json 은 늘 다시 쓴다."""
    name = run.get("result_dir") or stamp(now)
    run["result_dir"] = name
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    meta = {"version": VERSION, "run_id": run.get("run_id"), "started": name, "name": run.get("name"),
            "question_set": run.get("question_set"), "eval_kind": run.get("eval_kind"), "model_choice": run.get("model_choice")}
    (d / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def publish_collection(log_dir: Path, dest: Path) -> list:
    """답변 수집 기록을 결과 폴더 수집기록/ 으로 복사 — 빈 로그는 뺀다. 복사한 파일 이름 목록."""
    files = sorted({f for pat in COLLECT_PATTERNS for f in log_dir.glob(pat) if f.is_file() and f.stat().st_size})
    if files:
        (dest / COLLECT_DIR).mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, dest / COLLECT_DIR / f.name)
    screens = log_dir / "화면"   # 옛 수집(10-26 오후)의 대화 화면 — 지금 수집은 글 로그라 새로 생기지 않는다(사용자 10-26 저녁)
    if screens.is_dir() and any(screens.rglob("*.jpg")):
        shutil.copytree(screens, dest / COLLECT_DIR / "화면", dirs_exist_ok=True)
        files += sorted(screens.rglob("*.jpg"))
    return [f.name for f in files]


def publish(judge_dir: Path, dest: Path) -> list:
    """판정 폴더의 보고서 · 판정 기록을 결과 폴더로 복사 — 복사한 파일 이름 목록."""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for src, name in JUDGE_FILES.items():
        if (judge_dir / src).exists():
            shutil.copy2(judge_dir / src, dest / name)
            out.append(name)
    return out
