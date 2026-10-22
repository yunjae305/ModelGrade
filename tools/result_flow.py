"""결과 요약의 '전체 흐름도' 장면 — 10-20 보고 01장 흐름도(tools/build_report_2026-10-20.py)를 지금 판정 방식에 맞게 고친 것 (사용자 10-21).

바뀐 점: 의미 평가 → LLM Judge 심판 셋(사용자 10-21 저녁: 입력 32K 토큰 이하는 Jev · 넘으면 Decisions API · Decisions FAIL 만 Codex) ·
판정은 질문 하나씩 네 모델 동시(20개 묶음 없음) ·
빈 출력 요구는 모두 보류 · 질문 판정은 보류 우선 · 이름은 CODE 검사 · LLM Judge.
그림 조각(text · rect · module …)은 10-20 보고의 것을 그대로 옮겼다 — 10-20 보고는 그날 기록이라 손대지 않는다.

    from tools.result_flow import flow_scene, FLOW_CSS, WARN
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Dict, List

E = html.escape

WARN: List[str] = []   # 칸보다 긴 글 — 빌더가 끝에 찍는다

FLOW_CSS = """<style>
:root { --file: #6a4bb0; --file-soft: #f0ebfa; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --file: #b7a2ee; --file-soft: #231d36; } }
:root[data-theme="dark"] { --file: #b7a2ee; --file-soft: #231d36; }
/* 전체 흐름도: 가운데 세로 흐름(단계 A~H·분기) · 왼쪽 파일 · 오른쪽 AI 호출, 그 옆에 요약 표. 좁은 화면에서는 요약 표가 그림 아래로 */
.poster { display: grid; gap: 14px; margin: 0; }
.poster-sub { justify-self: center; border: 1px solid var(--line); background: var(--bg); border-radius: 999px; padding: 4px 14px; font-size: 0.9rem; color: var(--muted); text-align: center; }
.poster-grid { display: grid; grid-template-columns: minmax(0, 1fr) 270px; gap: 18px; align-items: start; }
.pflow-svg { width: 100%; height: auto; display: block; }
.pside { display: grid; gap: 12px; min-width: 0; }
.pcard { border: 1px solid var(--line); border-radius: 10px; background: var(--bg); padding: 12px 14px; font-size: 0.88rem; line-height: 1.5; min-width: 0; }
.pcard h4 { margin: 0 0 6px; font-size: 0.95rem; }
.pcard ul.p-legend { list-style: none; margin: 0; padding-left: 0; display: grid; gap: 3px; }
.p-legend li { display: flex; align-items: center; gap: 8px; }
.p-legend svg { flex: none; }
.ptab { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
.ptab th, .ptab td { text-align: left; padding: 4px 6px; border-top: 1px solid var(--line); vertical-align: top; }
.ptab thead th { border-top: 0; color: var(--muted); font-weight: 600; font-size: 0.78rem; }
.r-pass { color: var(--ok); font-weight: 700; }
.r-fail { color: var(--risk); font-weight: 700; }
.r-hold { color: var(--hold-text, var(--warn)); font-weight: 700; }
@media (max-width: 1000px) {
  .poster-grid { grid-template-columns: minmax(0, 1fr); }
  .pside { grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
}
.pflow text { font-family: var(--font-body); }
.pflow .pp { fill: var(--ink); }
.pflow .ppt { fill: var(--surface); font-weight: 600; font-size: 15px; }
.pflow .pm { fill: var(--surface); stroke: var(--line); stroke-width: 1.4; }
.pflow .pm.code { stroke: var(--accent); stroke-width: 1.8; }
.pflow .pm.ai { stroke: var(--ok); stroke-width: 1.8; }
.pflow .pm.set { stroke: var(--todo); stroke-width: 1.8; }
.pflow .pml { fill: var(--accent); font-family: var(--font-num); font-weight: 700; font-size: 16px; }
.pflow .pml.ai { fill: var(--ok); }
.pflow .pml.set { fill: var(--todo); }
.pflow .pmt { fill: var(--ink); font-weight: 700; font-size: 16px; }
.pflow .pb { fill: var(--bg); stroke: var(--line); }
.pflow .pbl { fill: var(--accent); font-weight: 600; font-size: 13px; }
.pflow .pbt { fill: var(--ink); font-size: 14px; }
.pflow .pd { fill: var(--surface); stroke: var(--muted); stroke-width: 1.4; }
.pflow .pdt { fill: var(--ink); font-weight: 600; font-size: 13.5px; }
.pflow .px { fill: var(--surface); stroke: var(--line); }
.pflow .pxt { fill: var(--ink); font-size: 13px; }
.pflow .pr { fill: var(--accent-soft); stroke: var(--accent-weak); }
.pflow .prt { fill: var(--ink); font-weight: 600; font-size: 14px; }
.pflow .pai { fill: var(--warn-soft); stroke: var(--warn); stroke-width: 1.4; stroke-dasharray: 6 4; }
.pflow .pait { fill: var(--warn); font-weight: 700; font-size: 14px; }
.pflow .paiT { fill: var(--ink); font-size: 13px; }
.pflow .pf { fill: var(--file-soft); stroke: var(--file); stroke-width: 1.3; }
.pflow .pft { fill: var(--file); font-weight: 700; font-size: 13.5px; }
.pflow .pfl { fill: var(--ink); font-size: 12px; }
.pflow .pc { stroke: var(--muted); stroke-width: 1.6; fill: none; }
.pflow .pc.ai { stroke: var(--warn); stroke-dasharray: 5 4; stroke-width: 1.4; }
.pflow .pc.file { stroke: var(--file); stroke-dasharray: 4 4; stroke-width: 1.3; }
.pflow .pah { fill: var(--muted); }
.pflow .pahw { fill: var(--warn); }
.pflow .pahf { fill: var(--file); }
.pflow .plab { fill: var(--muted); font-size: 12.5px; font-weight: 600; }
</style>"""


# ---------- 그림 조각 (10-20 보고와 같음) ----------
def text_width(s, size):
    """글 폭 어림 — 한글·화살표 1em, 가운뎃점 0.45em, 빈칸 0.3em, 영문 대문자 0.66em, 그 밖 0.56em."""
    w = 0.0
    for ch in s:
        if 0xAC00 <= ord(ch) <= 0xD7A3 or ch in "→—…":
            w += 1.0
        elif ch == "·":
            w += 0.45
        elif ch == " ":
            w += 0.3
        elif ch.isupper():
            w += 0.66
        else:
            w += 0.56
    return w * size


def text(x, y, s, cls, size, avail=None, anchor=None):
    if avail is not None and text_width(s, size) > avail:
        WARN.append(f"{s!r}: 약 {text_width(s, size):.0f}px > 칸 {avail:.0f}px")
    a = f' text-anchor="{anchor}"' if anchor else ""
    return f'<text class="{cls}" x="{x:g}" y="{y:g}"{a}>{E(s)}</text>'


def rect(x, y, w, h, cls, rx=8):
    return f'<rect class="{cls}" x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{rx}"/>'


def path(d, cls="", marker="pa", start=False):
    m = f' marker-end="url(#{marker})"' if marker else ""
    if start:
        m += f' marker-start="url(#{marker})"'
    return f'<path class="pc {cls}" d="{d}"{m}/>'


def arrow(x1, y1, x2, y2):
    return path(f"M{x1:g},{y1:g} L{x2:g},{y2:g}")


def lab(x, y, s, anchor=None):
    return text(x, y, s, "plab", 12.5, None, anchor)


# 칸 배치 — 왼쪽 통로(되돌아가기) · 파일 · 파일 연결선 자리 · 가운데 단계 · 오른쪽 통로(바로 G 로) · AI 호출
W = 914
LANE_L = 8
FX, FW = 24, 176
BUS = 212         # 파일 연결선이 꺾이는 세로 자리
CX0, CW = 228, 432
CC, CR = CX0 + CW // 2, CX0 + CW
LANE_R = 681
AX, AW = 702, 212
DX = 364          # 결과 칸이 오른쪽에 붙는 분기의 가운데
SX, SW = DX + 138, CR - DX - 138   # 분기 결과 칸
LABEL_W = 80      # 단계 안 작은 칸의 이름표 폭


def block(y, label, lines):
    h = 12 + 19 * len(lines)
    out = [rect(CX0 + 12, y, CW - 24, h, "pb", 6), text(CX0 + 24, y + 20, label, "pbl", 13, LABEL_W - 14)]
    out += [text(CX0 + 24 + LABEL_W, y + 20 + 19 * i, ln, "pbt", 14, CW - 48 - LABEL_W) for i, ln in enumerate(lines)]
    return out, h


def frame(y, h, letter, title, cls):
    return [rect(CX0, y, CW, h, f"pm {cls}", 10), text(CX0 + 16, y + 26, letter, f"pml {cls}", 16),
            text(CX0 + 42, y + 26, title, "pmt", 16, CW - 60)]


def module(y, letter, title, blocks, cls):
    """단계 상자 — 머리글 + 작은 칸들. (조각, 높이, 칸마다 (y, 높이))"""
    inner, spots, yb = [], [], y + 40
    for label, lines in blocks:
        parts, h = block(yb, label, lines)
        inner += parts
        spots.append((yb, h))
        yb += h + 6
    h = yb - y + 4
    return frame(y, h, letter, title, cls) + inner, h, spots


def pill(cx, y, w, h, s):
    return [rect(cx - w / 2, y, w, h, "pp", h // 2), text(cx, y + h / 2 + 5, s, "ppt", 15, w - 24, "middle")]


def diamond(cx, cy, w, h, lines):
    out = [f'<polygon class="pd" points="{cx:g},{cy - h / 2:g} {cx + w / 2:g},{cy:g} {cx:g},{cy + h / 2:g} {cx - w / 2:g},{cy:g}"/>']
    base = cy + 5 - (len(lines) - 1) * 9
    return out + [text(cx, base + 18 * i, ln, "pdt", 13.5, w * 0.62, "middle") for i, ln in enumerate(lines)]


def side(y, lines):
    """분기 결과 칸 — 오른쪽 통로로 이어져 G 로 바로 간다."""
    h = 12 + 18 * len(lines)
    return [rect(SX, y, SW, h, "px", 8)] + [text(SX + 12, y + 19 + 18 * i, ln, "pxt", 13, SW - 24) for i, ln in enumerate(lines)], h


def aibox(y, title, lines):
    h = 34 + 18 * len(lines)
    out = [rect(AX, y, AW, h, "pai", 10), text(AX + 12, y + 22, title, "pait", 14, AW - 24)]
    return out + [text(AX + 12, y + 42 + 18 * i, ln, "paiT", 13, AW - 24) for i, ln in enumerate(lines)], h


def filenode(y, title, lines):
    h = 32 + 17 * len(lines)
    x, w = FX, FW
    out = [f'<path class="pf" d="M{x},{y:g} H{x + w - 14} L{x + w},{y + 14:g} V{y + h:g} H{x} Z"/>',
           f'<path class="pf" d="M{x + w - 14},{y:g} V{y + 14:g} H{x + w}"/>',
           text(x + 10, y + 21, title, "pft", 13.5, w - 30)]
    return out + [text(x + 10, y + 39 + 17 * i, ln, "pfl", 12, w - 20) for i, ln in enumerate(lines)], h


def file_read(ym, yf=None):
    """파일 → 단계. 높이가 다르면 BUS 에서 꺾는다."""
    yf = ym if yf is None else yf
    return path(f"M{FX + FW},{yf:g} H{BUS} V{ym:g} H{CX0 - 2}", "file", "paf")


def file_write(ym, yf=None):
    """단계 → 파일. 높이가 다르면 BUS 에서 꺾는다."""
    yf = ym if yf is None else yf
    return path(f"M{CX0},{ym:g} H{BUS} V{yf:g} H{FX + FW + 2}", "file", "paf")


def ai_link(y):
    return path(f"M{CR + 2},{y:g} L{AX - 2},{y:g}", "ai", "paw", start=True)


# ---------- 흐름도 A~H — 지금 판정 방식 ----------
def flow_svg(n: int, set_ver: str) -> str:
    P: list = []
    y = 8
    P += pill(CC, y, 340, 42, "시작")   # 사용자 10-22: 명령어는 설명 안 함
    y += 42
    P.append(arrow(CC, y, CC, y + 24))
    y += 26

    A_y = y
    parts, A_h, _ = module(y, "A", "질문 세트 — 고정", [
        ("질문", [f"{n}개 — 기준마다 질문 10개 이상 (02장)"]),   # 사용자 10-22: 고정이라 '원칙' 줄 뺌 · 326 에서 고른 것이 아니라 수만 정하고 새로 만듦
    ], "set")
    P += parts
    f1_y = A_y + 10
    parts, f1_h = filenode(f1_y, "질문 세트 파일", [f"질문 {n}개", "questions.json", set_ver])
    P += parts
    P.append(file_read(A_y + 30, f1_y + 26))
    y += A_h
    P.append(arrow(CC, y, CC, y + 24))
    y += 26

    B_y = y
    parts, B_h, spots = module(y, "B", "답변 수집 — 네 모델 동시", [
        ("앱", ["ChatGPT · Claude · Gemini 데스크톱 앱"]),
        ("웹", ["AIONA(개발 서버)"]),
        ("방법", ["화면을 자동 조작해 질문 → 복사 버튼 원문", "첨부는 붙여넣기 · 받은 질문은 건너뜀"]),
    ], "ai")
    P += parts
    parts, _ = aibox(B_y, "AI · 답하는 모델 4개", ["GPT-6 · Medium", "Claude Sonnet 5.5 · 중간", "Gemini 3.8 Flash · 보통",
                                                 "AIONA · 자동 추천", "로그인은 사람이 미리"])
    P += parts
    P.append(ai_link(spots[0][0] + spots[0][1] / 2))
    y += B_h

    # 분기 1 — 답을 받았나?
    P.append(arrow(DX, y, DX, y + 22))
    y += 22
    cy1 = y + 30
    P += diamond(DX, cy1, 190, 60, ["답을 받았나?"])
    parts, _ = side(cy1 - 24, ["재시도 대상 → 보류", "빈 답 → 보류", "못 받음 → FAIL"])
    P += parts
    P += [path(f"M{DX + 95},{cy1:g} L{SX - 2},{cy1:g}"), lab(DX + 100, cy1 - 7, "아니오")]
    y = cy1 + 30
    P += [arrow(DX, y, DX, y + 24), lab(DX + 7, y + 16, "예")]
    y += 26

    C_y = y
    parts, C_h, _ = module(y, "C", "질문·답변 JSON 저장 — 코드", [
        ("저장", ["질문마다 바로 저장 · 멈춰도 이어서"]),
        ("내용", ["질문·첨부·답변 원문 · 수집 상태·시간"]),
    ], "code")
    P += parts
    y += C_h
    P.append(arrow(CC, y, CC, y + 24))
    y += 26

    D_y = y
    parts, D_h, _ = module(y, "D", "CODE 검사 — 28가지, 코드", [
        ("검사", ["형식·개수·글자 수·계산 — 저장 직후 바로"]),
        ("결과", ["검사마다 PASS·FAIL 과 이유"]),
    ], "code")
    P += parts
    y += D_h
    f2_y = C_y + 6
    parts, f2_h = filenode(f2_y, "질문·답변 JSON", ["실행 기록 · 결과 JSON", "CODE 검사 결과도 함께"])
    P += parts
    P += [file_write(C_y + 30, f2_y + 22), file_write(D_y + 30, f2_y + f2_h - 12)]

    # 판정 — 되돌아오는 자리 (20개 묶음 없음: 질문 하나씩, 네 모델 동시)
    P.append(arrow(CC, y, CC, y + 24))
    y += 26
    R1_y = y
    P += [rect(CC - 170, y, 340, 40, "pr", 8), text(CC, y + 25, "판정 — 질문 하나씩 · 네 모델 동시", "prt", 14, 316, "middle")]
    y += 40

    # 빈 출력 요구 분기는 그리지 않음 (10-22 미팅: 여러 예외 처리 중 하나라 흐름도에서는 노이즈 — 판정 규칙 표에는 남김)
    P.append(arrow(CC, y, CC, y + 24))
    y += 26

    # E — LLM Judge 심판 셋 (사용자 10-21 저녁): 짧은 입력(32K 토큰 이하)은 Jev · 긴 입력은 Decisions API · Decisions FAIL 만 Codex
    E_y = y
    parts, E_h, spots = module(y, "E", "LLM Judge — 44개, 심판 셋", [
        ("입력", ["판정 프롬프트 + 질문·답변 JSON", "모델 이름 숨김 (블라인드)"]),   # 사용자 10-22: 그냥 판정 프롬프트
        ("심판 1", ["3만 2천 토큰 이하 · 첨부 없음", "Jev 가 판정"]),
        ("심판 2", ["그 밖(긴 것 · 첨부)", "Decisions API 가 판정"]),
        ("심판 3", ["1차가 FAIL · 애매로 본 항목만", "Codex 가 다시 판정 · 원문 인용"]),
    ], "ai")
    P += parts
    for spot, title, lines in ((spots[1], "AI · 심판 1 — Jev", ["외부 API (TypeSafe)"]),
                               (spots[2], "AI · 심판 2 — Decisions", ["GPT-6 Luna · OpenAI API"]),
                               (spots[3], "AI · 심판 3 — Codex CLI", ["GPT-6 Luna · low · 구독"])):
        parts, _ = aibox(spot[0] - 1, title, lines)
        P += parts + [ai_link(spot[0] + spot[1] / 2)]
    parts, f3_h = filenode(E_y + 10, "판정 프롬프트", ["모든 질문에 같은 기준 글", "Jev 용은 질문 목록 꼴"])   # 사용자 10-22
    P += parts
    P.append(file_read(E_y + 52, E_y + 36))
    f4_y = E_y + 22 + f3_h
    parts, f4_h = filenode(f4_y, "질문·답변 JSON", ["C 에서 확인한 질문 · 답", "질문 하나씩 심판에게"])
    P += parts
    P.append(file_read(spots[0][0] + 30, f4_y + 26))
    y = E_y + E_h
    P.append(arrow(CC, y, CC, y + 24))
    y += 26

    F_y = y
    parts, F_h, _ = module(y, "F", "합치기·보류 — 규칙, 코드", [
        ("확정", ["Codex 가 본 항목 → Codex 판정대로", "나머지 → Jev · Decisions 판정대로"]),
        ("보류", ["Codex 확인 못 함 · 인용이 원문에 없음", "Codex 판정 없음 + 1차 FAIL"]),
    ], "code")
    P += parts
    y += F_h
    P.append(arrow(CC, y, CC, y + 24))
    y += 26

    G_y = y
    parts, G_h, _ = module(y, "G", "질문 단위 판정 — 코드", [
        ("판정", ["보류 하나라도 → 보류 (보류 우선)", "아니면 FAIL 하나라도 → FAIL", "모든 기준 통과 → 완전 통과"]),
    ], "code")
    P += parts
    # 분기 결과 칸(답을 못 받음 등)은 오른쪽 통로로 G 로 바로 간다
    P += [path(f"M{CR},{cy1:g} L{LANE_R},{cy1:g} L{LANE_R},{G_y + 24} L{CR + 2},{G_y + 24}"),
          lab(LANE_R + 6, G_y + 28, "바로 G 로")]
    y += G_h
    P.append(arrow(CC, y, CC, y + 24))
    y += 26

    H_y = y
    parts, H_h, _ = module(y, "H", "결과 추출", [   # 사용자 10-22: 이름 '결과 추출', 명령 줄 뺌
        ("결과", [f"모델별 완전 통과·FAIL·보류 / {n}", "보고서 · 결과 JSON · 이 결과 요약"]),
    ], "code")
    P += parts
    y += H_h
    f5_y = G_y + 10
    parts, f5_h = filenode(f5_y, "판정 결과", ["질문별 판정 · 시간", "report.html"])
    P += parts
    P += [file_write(F_y + 40, f5_y + 16), file_write(f5_y + 40), file_write(H_y + 26, f5_y + f5_h - 12)]
    f6_y = H_y + 70
    parts, f6_h = filenode(f6_y, "결과 JSON", ["저장소에 보관", "다른 PC 에서도 봄"])
    P += parts
    P.append(file_write(f6_y + 30))

    # 분기 3 — 남은 질문이 있나? 예면 판정 자리로 되돌아간다
    P.append(arrow(CC, y, CC, y + 22))
    y += 22
    cy3 = y + 30
    P += diamond(CC, cy3, 200, 60, ["남은 질문이 있나?"])
    P += [path(f"M{CC - 100},{cy3:g} L{LANE_L},{cy3:g} L{LANE_L},{R1_y + 20} L{CC - 172},{R1_y + 20}"),
          lab(CC - 106, cy3 - 7, "예 — 다음 질문", "end")]
    y = cy3 + 30
    P += [arrow(CC, y, CC, y + 24), lab(CC + 7, y + 16, "아니오")]
    y += 26
    P += pill(CC, y, 340, 42, "끝 · 보류는 사람이 정함")
    y += 52

    markers = "".join(
        f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" class="{cls}"/></marker>' for mid, cls in (("pa", "pah"), ("paw", "pahw"), ("paf", "pahf")))
    return (f'<svg class="pflow pflow-svg" viewBox="0 0 {W} {y:g}" role="img" aria-label="전체 흐름도 — 질문 세트에서 결과까지 A부터 H, '
            f'분기·AI 호출·파일 표시"><defs>{markers}</defs>\n' + "\n".join(P) + "\n</svg>")


# ---------- 옆 표 (단계별 요약 표는 뺌 — 흐름도 · 누가 하나와 겹치고 파일 경로는 개발용, 사용자 10-21 저녁) ----------
PASS, FAIL, HOLD = '<span class="r-pass">통과</span>', '<span class="r-fail">FAIL</span>', '<span class="r-hold">보류</span>'
HOLD_P = f"{HOLD} · 사람이 판단"   # 사용자 10-22: 보류 옆에 '사람이 판단'


def mini(title, head, rows, cls=""):
    thead = "<thead><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead>"
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="pcard {cls}"><h4>{title}</h4><table class="ptab">{thead}<tbody>{body}</tbody></table></div>'


def legend_item(shape, label):
    return f'<li><svg class="pflow" width="40" height="22" viewBox="0 0 40 22" aria-hidden="true">{shape}</svg>{label}</li>'


def side_cards() -> str:
    legend = ('<div class="pcard"><h4>도형</h4><ul class="p-legend">'
              + legend_item('<rect class="pp" x="2" y="5" width="36" height="12" rx="6"/>', "시작·끝")
              + legend_item('<rect class="pm code" x="2" y="3" width="36" height="16" rx="3"/>', "코드가 하는 단계")
              + legend_item('<rect class="pm ai" x="2" y="3" width="36" height="16" rx="3"/>', "AI 가 끼는 단계")
              + legend_item('<polygon class="pd" points="20,2 38,11 20,20 2,11"/>', "갈림길")
              + legend_item('<rect class="pai" x="2" y="3" width="36" height="16" rx="3"/>', "AI 호출")
              + legend_item('<path class="pf" d="M4,2 H28 L36,10 V20 H4 Z"/>', "파일")
              + "</ul></div>")
    return (mini("누가 하나", ["단계", "누가"], [
                ["A·C·D·F·G·H", "코드 (정해진 규칙)"],
                ["B 답변 수집", "답하는 모델 4개 — 앱·웹 자동 조작"],
                ["E LLM Judge", "심판 3개 — Jev(API) · Decisions(API) · Codex(구독)"],
                ["보류 정리", "사람"],
            ], "ai")
            + mini("판정 규칙", ["상황", "결과"], [
                ["CODE 검사 FAIL", FAIL],   # 사용자 10-22 '다시 확인' — judge_v4 규칙과 맞춤 · 보류는 모두 '사람이 판단'
                ["답을 못 받음 (모델 오류 · 끝나지 않음)", FAIL],
                ["재시도 대상(복사 실패) · 빈 답", HOLD_P],
                ["빈 출력 요구(글 · 빈 답 · 오류 모두)", HOLD_P],
                ["Codex FAIL", FAIL],
                ["1차 FAIL + Codex 통과", "Codex 의 판정을 따름"],
                ["Codex 판정 없음 + 1차 FAIL", HOLD_P],
                ["Codex 확인 못 함 (사실 확인 · 그 밖은 1차 확신 낮을 때)", HOLD_P],
                ["인용이 원문에 없음 (다시 물어도)", HOLD_P],
                ["질문에 하나라도 보류라면", HOLD_P],
            ])
            + legend)


def flow_scene(D: Dict[str, Any]) -> str:
    """장면 내용 — 흐름도 + 옆 표(누가 하나 · 판정 규칙 · 도형)."""
    n = D["n"]
    set_dir = Path(D["set_dir"])
    vers = re.findall(r"(생성프롬프트|멀티턴)(v[\d.]+)", set_dir.name)   # 질문세트_78_생성프롬프트v3.3_멀티턴v3 → v3.3 · 멀티턴 v3
    m = re.search(r"v[\d.]+$", set_dir.name)
    set_ver = (" · ".join(v if k == "생성프롬프트" else f"{k} {v}" for k, v in vers) if vers
               else m.group(0) if m else set_dir.name)
    # 받은 의견 E (10-21 밤): 이 그림은 다음 평가 구조(심판 셋) — 이번 결과가 다른 방식으로 판정됐으면 그 방식을 같이 적는다 (Gemini 표기는 페이지 전체 3.8, 사용자 10-21 밤)
    pipe = D["d"]["judgment"].get("pipeline") or ""
    now = re.search(r"\((.*)\)", pipe)
    sub = (f'질문 {n}개 · 모델 {len(D["order"])}개 · LLM Judge 심판 셋 — Jev · Decisions API · Codex CLI' if "Jev" in pipe else
           f'<b>다음 평가 구조</b> — 질문 {n}개 · 모델 {len(D["order"])}개 · LLM Judge 심판 셋(Jev · Decisions API · Codex CLI)<br>'
           f'이번 결과({html.escape(D["run_id"])})는 {html.escape(now.group(1) if now else pipe)}'
           + (' · Gemini 는 웹브라우저로 받음' if "웹" in D["d"]["models"].get("gemini", "") else ""))
    return (f'<figure class="poster"><span class="poster-sub">{sub}</span>'
            f'<div class="poster-grid">{flow_svg(n, set_ver)}<aside class="pside" aria-label="흐름도 요약">{side_cards()}</aside></div></figure>')
