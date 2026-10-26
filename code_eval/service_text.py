"""서비스가 답 끝에 붙이는 글 — 답변 모델이 쓴 글이 아니다 (AIONA 의 '데이터 제공: …' 줄).

판정 도구(tools/judge_v4.py, J5-3.a 출처 주장)와 CODE(2-1 허용 문자)가 같은 정의를 쓴다.
"""
from __future__ import annotations

import re
from typing import List, Tuple

#: 서비스가 실제로 붙이는 footer 항목(50ca3ccccace AIONA 답 14개에서 본 것 전부) — 항목 전체가 이 가운데 하나여야 한다('MET Norway, 달빛…' 처럼
#: 아는 제공처 뒤에 모르는 것이 붙거나 자료 제목이 다르면 답변 모델이 지어낸 것일 수 있어 그대로 판정, Codex 검토 10-22 3번)
SERVICE_FOOTER_ENTRY = (r"(?:MET Norway \(Weather data from MET Norway \(CC BY 4\.0\)\)"
                        r"|한국천문연구원 \(공공데이터포털 특일 정보\(공휴일\)\)"
                        r"|한국관광공사 \(TourAPI 4\.0 관광정보\))")
SERVICE_FOOTER = re.compile(r"^데이터 ?제공\s*[:：]\s*" + SERVICE_FOOTER_ENTRY + r"(?:\s*·\s*" + SERVICE_FOOTER_ENTRY + r")*\s*$")


def footer_spans(text: str) -> List[Tuple[int, int]]:
    """답 안에서 서비스 footer 줄의 (시작, 끝) — 줄 전체가 footer 형식일 때만."""
    out, pos = [], 0
    for line in (text or "").splitlines(keepends=True):
        if SERVICE_FOOTER.match(line.strip()):
            out.append((pos, pos + len(line)))
        pos += len(line)
    return out
