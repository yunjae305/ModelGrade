"""aiona-chat-eval 프로그램 버전 — 시맨틱 버저닝 X.Y.Z (사용자 10-27 '버전은 이것을 따르자').

- X (Major): 하위 호환이 깨지는 변경 — 결과 폴더 · 결과 JSON 양식, 명령 · 옵션 이름, 기준 수(72)처럼 전 결과와 바로 견줄 수 없게 되는 것
- Y (Minor): 하위 호환을 지키며 기능이 늘거나 판정이 달라질 때 — 새 명령 · 옵션 · 보고서 장면, 새로 고를 수 있는 모델,
  판정 프롬프트 · 1차 항목 정의 · 확인 목록 다듬기(하네스2 자동 반영 포함)
- Z (Patch): 기능 추가 없이 버그 수정 · 문구 · 내부 정리만

올릴 때는 이 숫자와 CHANGELOG.md · README 배지 · README '새로운 기능'(최근 3개 버전)을 같이 고치고 git 태그 vX.Y.Z 를 단다 — bump() 가 모두 한다.
CHANGELOG 칸은 버전마다 신규 · 개선됨 · 수정됨으로 나눈다(사용자 10-27 'README 에 변경로그도 계속 작성해야해'). 하네스2 자동 반영은 bump("minor") 로 스스로 올린다.
결과 JSON(program_version)과 보고서 머리에 이 숫자가 남는다.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Sequence, Tuple, Union

__version__ = "2.1.1"
NAME = "aiona-chat-eval"
ROOT = Path(__file__).resolve().parents[1]


def label() -> str:
    """'aiona-chat-eval 1.2.0'"""
    return f"{NAME} {__version__}"


KINDS = ("신규", "개선됨", "수정됨")
README_VERSIONS = 3   # README '새로운 기능'에 보이는 최근 버전 수
LOG_START = "<!-- 새로운 기능: tools/version.py 가 CHANGELOG.md 에서 채움 — 여기는 손으로 고치지 않음 -->"
LOG_END = "<!-- /새로운 기능 -->"


def changelog_entries(text: str) -> List[Tuple[str, str, str]]:
    """CHANGELOG.md → [(버전, 날짜, 본문)] 위에서부터(최신 먼저)."""
    return [(m.group(1), m.group(2), m.group(3).strip()) for m in re.finditer(
        r"^## (\d+\.\d+\.\d+) — (\d{4}-\d{2}-\d{2})\n(.*?)(?=^## |\Z)", text, re.S | re.M)]


def readme_log(changelog_text: str, n: int = README_VERSIONS) -> str:
    """README '새로운 기능' 칸 — 최근 n 개 버전을 '2026년 10월 27일 · `1.4.0`' 머리와 신규 · 개선됨 · 수정됨 굵은 글씨로."""
    out = []
    for ver, day, body in changelog_entries(changelog_text)[:n]:
        y, m, d = (int(x) for x in day.split("-"))
        out.append(f"### {y}년 {m}월 {d}일 · `{ver}`\n\n" + re.sub(r"^### (.+)$", r"**\1**", body, flags=re.M))
    return "\n\n".join(out)


def sync_readme(root: Path = ROOT) -> bool:
    """README 의 '새로운 기능' 칸(LOG_START ~ LOG_END)을 CHANGELOG.md 최근 버전으로 다시 채운다. 칸이 없으면 그대로(False)."""
    rd, cl = root / "README.md", root / "CHANGELOG.md"
    if not (rd.exists() and cl.exists()):
        return False
    text = rd.read_text(encoding="utf-8")
    a, b = text.find(LOG_START), text.find(LOG_END)
    if a < 0 or b < a:
        return False
    body = readme_log(cl.read_text(encoding="utf-8"))
    rd.write_text(text[:a + len(LOG_START)] + "\n\n" + body + "\n\n" + text[b:], encoding="utf-8")
    return True


def bump(part: str, notes: Union[Sequence[str], Dict[str, Sequence[str]]], root: Path = ROOT) -> str:
    """버전을 올리고(major · minor · patch) CHANGELOG.md 맨 위에 그 버전 칸과 notes 를, README 배지 · '새로운 기능'도 같이 — 새 버전을 돌려준다.
    notes 는 {'신규' | '개선됨' | '수정됨': [줄, …]} 또는 줄 목록(수정 버전이면 '수정됨', 아니면 '신규'로)."""
    vf = root / "tools" / "version.py"
    text = vf.read_text(encoding="utf-8")
    x, y, z = (int(n) for n in re.search(r'__version__ = "(\d+)\.(\d+)\.(\d+)"', text).groups())
    new = {"major": f"{x + 1}.0.0", "minor": f"{x}.{y + 1}.0", "patch": f"{x}.{y}.{z + 1}"}[part]
    vf.write_text(re.sub(r'__version__ = "[\d.]+"', f'__version__ = "{new}"', text, count=1), encoding="utf-8")
    cl = root / "CHANGELOG.md"
    if cl.exists():
        c = cl.read_text(encoding="utf-8")
        groups = dict(notes) if isinstance(notes, dict) else {("수정됨" if part == "patch" else "신규"): list(notes)}
        bad = set(groups) - set(KINDS)
        if bad:
            raise ValueError(f"변경 종류는 {' · '.join(KINDS)} 중 하나 — {sorted(bad)}")
        entry = f"## {new} — {date.today().isoformat()}\n\n" + "".join(
            f"### {k}\n\n" + "".join(f"- {n}\n" for n in groups[k]) + "\n" for k in KINDS if groups.get(k))
        i = c.find("\n## ")
        cl.write_text(c[:i + 1] + entry + c[i + 1:] if i >= 0 else c.rstrip("\n") + "\n\n" + entry, encoding="utf-8")
    rd = root / "README.md"
    if rd.exists():
        rd.write_text(re.sub(r"badge/version-[\d.]+-", f"badge/version-{new}-", rd.read_text(encoding="utf-8")).replace(
            f'alt="version {x}.{y}.{z}"', f'alt="version {new}"'), encoding="utf-8")
        sync_readme(root)
    return new
