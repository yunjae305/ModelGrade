"""공개본 내보내기 — git 이 추적하는 파일 가운데 공개해도 되는 것만 새 폴더로 복사하고, 복사본을 다시 훑어 비밀 · 개인 경로가 없는지 확인한다.

    python tools/export_public.py --out ../public-export              # 기본 제외 목록으로 복사 + 점검
    python tools/export_public.py --out ../public-export --include-results   # 결과 폴더(수집 답변 원문)도 넣기 — 공개 전 점검 '결정 3' 뒤에만
    python tools/export_public.py --list                              # 복사하지 않고 넣을 파일 · 뺄 파일 수만

새 저장소(할일 14번)는 이 폴더에서 `git init` 해 만든다 — 이 내부 저장소의 이력(회의 메모 · 옛 실험 기록)은 옮기지 않는다.
제외 목록과 이유는 `참고자료/문서/공개_전_점검_2026-10-25.md` 의 '비밀·내부 정보 스캔' 절. 값(키 · 비밀번호)은 어디에도 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]

#: (패턴, 이유) — git 경로(슬래시) 기준 fnmatch. 공개 전 점검 문서의 표와 같은 목록.
EXCLUDE: Tuple[Tuple[str, str], ...] = (
    ("CLAUDE.md", "작업 도우미 지시(내부)"),
    ("참고자료/이어서진행_*.md", "할일 · 미팅 메모 · 녹취 경로"),
    ("참고자료/분석/회의록_*", "회의록 원문"),
    ("참고자료/분석/2026-10-18-질문당기준수/*", "옛 실험 원시 기록(사용자 경로 · 생성 답 속 가상 번호)"),
    ("참고자료/분석/2026-10-18-Codex검토-병행판정/*", "옛 검토 원시 기록(사용자 경로)"),
    ("참고자료/질문세트_326_생성프롬프트v2/답변/*", "수집한 모델 답변 원문(결정 3)"),
    (".runtime/*", "로컬 실행 도구 설치 기록"),
)
#: --include-results 가 없으면 뺀다 — 수집 답변 원문 · 수집 기록(공개 전 점검 결정 3)
RESULTS = (("참고자료/결과/*", "결과 폴더(수집 답변 원문 · 수집 기록, 결정 3)"),)

BINARY = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".docx", ".xlsx", ".pptx", ".zip", ".woff", ".woff2", ".ttf", ".otf", ".ico", ".webp", ".mp4"}
#: 복사본 점검 — 실제 키 모양 · 개인 키 · 사용자 절대 경로. 시험용 가짜 값(sk-semantic-… 처럼 짧고 낱말로 된 것)은 길이로 거른다.
CHECKS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("OpenAI · Anthropic 키 모양", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}")),
    ("Google 키 모양", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("GitHub 토큰 모양", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|github_pat_[A-Za-z0-9_]{60,}")),
    ("개인 키 블록", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY")),
    ("사용자 절대 경로", re.compile(r"[A-Za-z]:\\Users\\(?!<|Public\\|…)[^\\\s\"'`)\]]+|/Users/(?!<|Shared/)[^/\s\"'`)\]]+/")),
)


def tracked(root: Path = ROOT) -> List[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout.decode("utf-8")
    return [f for f in out.split("\0") if f]


def excluded_by(path: str, include_results: bool) -> Optional[str]:
    """뺄 파일이면 이유, 넣을 파일이면 None."""
    for pat, why in EXCLUDE + (() if include_results else RESULTS):
        if fnmatch.fnmatch(path, pat):
            return why
    return None


def plan(files: List[str], include_results: bool) -> Tuple[List[str], Dict[str, List[str]]]:
    keep, dropped = [], {}
    for f in files:
        why = excluded_by(f, include_results)
        if why:
            dropped.setdefault(why, []).append(f)
        else:
            keep.append(f)
    return keep, dropped


def scan(root: Path, files: List[str]) -> List[Dict[str, object]]:
    """복사본에서 비밀 · 개인 경로 모양을 찾는다 — (파일, 줄, 종류)만, 값은 남기지 않는다."""
    hits: List[Dict[str, object]] = []
    for f in files:
        p = root / f
        if p.suffix.lower() in BINARY or not p.is_file():
            continue
        text = p.read_bytes().decode("utf-8", errors="ignore")
        for n, line in enumerate(text.split("\n"), 1):
            for kind, pat in CHECKS:
                if pat.search(line):
                    hits.append({"file": f, "line": n, "kind": kind})
    return hits


def export(out: Path, include_results: bool = False, root: Path = ROOT, files: Optional[List[str]] = None) -> Dict[str, object]:
    files = tracked(root) if files is None else files
    keep, dropped = plan(files, include_results)
    out = out.resolve()
    if out == root.resolve() or root.resolve() in out.parents:
        raise SystemExit(f"내보낼 폴더는 이 저장소 밖이어야 합니다: {out}")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"내보낼 폴더가 비어 있지 않습니다: {out}")
    for f in keep:
        target = out / f
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / f, target)
    hits = scan(out, keep)
    report = {"kept": len(keep), "dropped": {why: len(v) for why, v in dropped.items()}, "include_results": include_results, "hits": hits}
    (out / "EXPORT_MANIFEST.json").write_text(json.dumps({**report, "files": keep}, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="내보낼 빈 폴더(이 저장소 밖)")
    ap.add_argument("--include-results", action="store_true", help="결과 폴더(수집 답변 원문)도 넣기 — 결정 3 뒤에만")
    ap.add_argument("--list", action="store_true", help="복사하지 않고 넣을 · 뺄 파일 수만")
    args = ap.parse_args(argv)
    if args.list or not args.out:
        keep, dropped = plan(tracked(), args.include_results)
        print(f"넣을 파일 {len(keep)}개 · 뺄 파일 {sum(len(v) for v in dropped.values())}개")
        for why, v in dropped.items():
            print(f"  - {why}: {len(v)}개")
        return 0
    rep = export(Path(args.out), args.include_results)
    print(f"복사 {rep['kept']}개 → {Path(args.out).resolve()} · 뺀 파일 {sum(rep['dropped'].values())}개")
    for why, n in rep["dropped"].items():
        print(f"  - {why}: {n}개")
    if rep["hits"]:
        print(f"⚠ 복사본에 비밀 · 개인 경로 모양 {len(rep['hits'])}곳 — EXPORT_MANIFEST.json 의 hits 를 보고 고친 뒤 다시 내보내세요")
        for h in rep["hits"][:20]:
            print(f"    {h['file']}:{h['line']} {h['kind']}")
        return 1
    print("점검: 비밀 · 개인 경로 모양 없음. 다음: 그 폴더에서 git init → LICENSE(결정 1) → 첫 커밋 → 새 저장소에 push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
