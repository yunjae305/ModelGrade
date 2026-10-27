"""평가 결과 폴더 (result-dirs-v2, 사용자 10-22 · 10-27) — 평가를 시작할 때마다 `참고자료/결과/<처음 시작한 시각>/` 하나를 만들고
그 안에는 두 파일만 둔다 (사용자 10-27: '질문, 답변이 합쳐진 json 파일 하나와 그것을 평가한 결과 report 하나'):

- `<실행>_결과_판정결과.json` — 질문 원문(턴마다 · 참조 자료) · 모델마다 받은 답과 수집 정보 · CODE 검사 · 판정 · 사람 판정 (tools/export_results_json)
- `report.html` — 그 JSON 을 평가한 결과 보고서 하나. 순위 · 분야별 · 기준별 · 시간 · FAIL 원인 · 보류 정하기 · 질문별 원문까지 한 파일
  (tools/build_result_summary --report)

판정 기록(results.jsonl · summary.json · human_decisions.json · 질문별 시간 표) · 판정 보고서 · 답변 수집 로그는 `data/judge_runs/<실행>/` 에 남는다
(data/ 는 git 에 안 올라감). 결과 폴더는 통째로 git 에 올린다.

- 폴더 이름: 이 PC 시각(한국) `YYYY-MM-DD_HH-MM-SS-mmm` (밀리초까지, 윈도우 폴더 이름에 쓸 수 없는 콜론 대신 '-').
- 멈춘 평가를 같은 명령으로 이어 돌리면 처음 시작한 시각 폴더를 계속 쓴다 — 실행 JSON 의 `result_dir` 에 폴더 이름을 적어 둔다.
- 실행 ID 로 폴더 찾기: 결과 JSON 이름(`<실행>_…판정결과.json`). 다시 판정 폴더는 JSON 머리의 `result.rerun_of` 로 가려 본 결과 폴더만 잡는다.
  옛 폴더(result-dirs-v1, 10-22 ~ 10-27)는 run.json 으로 찾는다.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "참고자료" / "결과"
VERSION = "result-dirs-v2"
REPORT = "report.html"             # 보고서 — 하나뿐 (사용자 10-27 '이게 메인 report.html 이야. 다른 report 는 없애')
JUDGE_REPORT = "판정보고서.html"     # 옛 결과 폴더(v1)와 판정 비교 실험 폴더의 판정 보고서 (tools/report_html) — 판정 폴더에서는 report.html
#: 판정 비교 실험 폴더(tools/judge_claude_compare)로 옮겨 두는 판정 기록 — {판정 폴더 이름: 결과 폴더 이름}
JUDGE_FILES = {"report.html": JUDGE_REPORT, "report.md": "판정보고서.md", "summary.json": "summary.json",
               "results.jsonl": "results.jsonl", "human_decisions.json": "human_decisions.json"}
HEAD = 4096   # 결과 JSON 머리 — 다시 판정 표시(result.rerun_of)는 앞쪽에 있다 (export_results_json)


def stamp(now: Optional[datetime] = None) -> str:
    """'2026-10-22_14-05-33-123' — 이 PC 시각, 밀리초까지."""
    now = now or datetime.now()
    return now.strftime("%Y-%m-%d_%H-%M-%S") + f"-{now.microsecond // 1000:03d}"


def _is_rerun(js: Path) -> bool:
    """다시 판정 폴더의 결과 JSON 인가 — 머리만 읽는다(파일은 수십 MB)."""
    try:
        with js.open(encoding="utf-8", errors="ignore") as f:
            return '"rerun_of"' in f.read(HEAD)
    except OSError:
        return False


def runs(base: Path = RESULTS) -> Iterator[Tuple[str, Path]]:
    """(실행 ID, 본 결과 폴더) — 최근 폴더부터. 다시 판정 · 판정 비교 폴더는 뺀다."""
    if not base.exists():
        return
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta = d / "run.json"   # 옛 폴더(v1) — run.json 의 run_id (다시 판정 · 비교 폴더는 run_id 대신 rerun_of · compare_of)
        if meta.exists():
            try:
                rid = json.loads(meta.read_text(encoding="utf-8")).get("run_id")
            except (OSError, ValueError):
                continue
            if rid:
                yield str(rid), d
            continue
        for js in sorted(d.glob("*_*판정결과.json")):
            if not _is_rerun(js):
                yield js.name.split("_", 1)[0], d
                break


def find(run_id: str, base: Path = RESULTS) -> Optional[Path]:
    """실행 ID 의 결과 폴더 — 없으면 None (판정 전이라 결과 JSON 이 아직 없는 폴더도 None)."""
    return next((d for rid, d in runs(base) if rid == run_id), None)


def ensure(run: Dict[str, Any], base: Path = RESULTS, now: Optional[datetime] = None) -> Path:
    """실행의 결과 폴더 — 실행에 `result_dir` 이 있으면 그 폴더(이어 돌리기), 없으면 지금 시각으로 새로 만들고 실행에 적는다(저장은 부르는 쪽)."""
    name = run.get("result_dir") or stamp(now)
    run["result_dir"] = name
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def publish(judge_dir: Path, dest: Path) -> list:
    """판정 폴더의 판정 보고서 · 판정 기록을 판정 비교 실험 폴더로 복사 — 복사한 파일 이름 목록. 평가 결과 폴더에는 쓰지 않는다(두 파일만)."""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for src, name in JUDGE_FILES.items():
        if (judge_dir / src).exists():
            shutil.copy2(judge_dir / src, dest / name)
            out.append(name)
    return out
