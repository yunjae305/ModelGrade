"""복사 글 보정 — 앱 '복사' 버튼이 글로 바꾸며 끼워 넣은 것을 되돌린다 (사용자 10-21 밤).

Gemini 대답 복사 글은 화면에서 멀쩡한 표의 칸 뒤에 빈 줄을 끼워 넣는다:
    | **승인과 실제** | 승인 예산 상한 | 180,000원
    (빈 줄)
     | 예산 최고 상한선
    (빈 줄)
     |
마크다운은 빈 줄에서 표가 끝나므로 CODE 검사가 표를 '머리만 있고 0행'으로 읽는다. 표 안에서 닫히지 않은 행 뒤에
빈 줄 + '공백으로 시작하는 |' 줄이 오면 한 행으로 잇는다(구분선 바로 위 머리 줄도). 새 행('|' 로 바로 시작)이나 다른 글이 나오면 잇지 않는다.
"""
from __future__ import annotations

import re
from typing import List, Tuple

_DELIM = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_CONT = re.compile(r"^[ \t]+\|")      # 칸 뒤에 밀려난 이어지는 줄 — 새 행은 '|' 로 바로 시작한다
_LOOKAHEAD = 40                        # 한 행을 잇는 데 볼 줄 수 상한


def join_split_table_rows(text: str) -> Tuple[str, int]:
    """(고친 글, 이은 행 수). 고칠 것이 없으면 원문 그대로."""
    if not text or "|" not in text:
        return text, 0
    lines = text.split("\n")
    out: List[str] = []
    joined, in_table, i = 0, False, 0
    while i < len(lines):
        line = lines[i]
        s = line.rstrip()
        if _DELIM.match(s) and out and out[-1].lstrip().startswith("|"):
            in_table = True
            out.append(line)
            i += 1
            continue
        if s.startswith("|") and not s.endswith("|"):
            row, j, closed = s, i + 1, False
            while j < len(lines) and j - i <= _LOOKAHEAD:
                t = lines[j].rstrip()
                if not t.strip():
                    j += 1
                    continue
                if not _CONT.match(t):
                    break
                row = f"{row} {t.strip()}"
                j += 1
                if row.endswith("|"):
                    closed = True
                    break
            is_header = closed and j < len(lines) and bool(_DELIM.match(lines[j].rstrip()))   # 구분선 바로 위 머리 줄도 쪼개진다(Q36)
            if closed and (in_table or is_header):
                out.append(row)
                joined += 1
                i = j
                continue
        if in_table and not s.lstrip().startswith("|"):
            in_table = False
        out.append(line)
        i += 1
    return ("\n".join(out), joined) if joined else (text, 0)
