"""aiona-chat-eval 프로그램 버전 — 시맨틱 버저닝 X.Y.Z (사용자 10-27 '버전은 이것을 따르자').

- X (Major): 하위 호환이 깨지는 변경 — 결과 폴더 · 결과 JSON 양식, 명령 · 옵션 이름, 기준 수(72)처럼 전 결과와 바로 견줄 수 없게 되는 것
- Y (Minor): 하위 호환을 지키며 기능이 늘거나 판정이 달라질 때 — 새 명령 · 옵션 · 보고서 장면, 새로 고를 수 있는 모델,
  판정 프롬프트 · 1차 항목 정의 · 확인 목록 다듬기(하네스2 자동 반영 포함)
- Z (Patch): 기능 추가 없이 버그 수정 · 문구 · 내부 정리만

올릴 때는 이 숫자와 CHANGELOG.md · README 배지를 같이 고치고 git 태그 vX.Y.Z 를 단다. 하네스2 자동 반영은 bump("minor") 로 스스로 올린다.
결과 JSON(program_version)과 보고서 머리에 이 숫자가 남는다.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import List

__version__ = "1.2.1"
NAME = "aiona-chat-eval"
ROOT = Path(__file__).resolve().parents[1]


def label() -> str:
    """'aiona-chat-eval 1.2.0'"""
    return f"{NAME} {__version__}"


def bump(part: str, notes: List[str], root: Path = ROOT) -> str:
    """버전을 올리고(major · minor · patch) CHANGELOG.md 맨 위에 그 버전 칸과 notes 를, README 배지도 같이 — 새 버전을 돌려준다."""
    vf = root / "tools" / "version.py"
    text = vf.read_text(encoding="utf-8")
    x, y, z = (int(n) for n in re.search(r'__version__ = "(\d+)\.(\d+)\.(\d+)"', text).groups())
    new = {"major": f"{x + 1}.0.0", "minor": f"{x}.{y + 1}.0", "patch": f"{x}.{y}.{z + 1}"}[part]
    vf.write_text(re.sub(r'__version__ = "[\d.]+"', f'__version__ = "{new}"', text, count=1), encoding="utf-8")
    cl = root / "CHANGELOG.md"
    if cl.exists():
        c = cl.read_text(encoding="utf-8")
        entry = f"## {new} — {date.today().isoformat()}\n\n" + "".join(f"- {n}\n" for n in notes) + "\n"
        i = c.find("\n## ")
        cl.write_text(c[:i + 1] + entry + c[i + 1:] if i >= 0 else c.rstrip("\n") + "\n\n" + entry, encoding="utf-8")
    rd = root / "README.md"
    if rd.exists():
        rd.write_text(re.sub(r"badge/version-[\d.]+-", f"badge/version-{new}-", rd.read_text(encoding="utf-8")).replace(
            f'alt="version {x}.{y}.{z}"', f'alt="version {new}"'), encoding="utf-8")
    return new
