"""10-20 보고 자료 — 10-19 오후 미팅 체크리스트('내일 보고')를 장으로 쌓는다 (사용자 결정 2026-10-20: 10-19 발표와 같은 모양의 새 HTML).

    python -X utf8 tools/build_report_2026-10-20.py
    → 참고자료/발표/ModelGrade_보고_2026-10-20.html

처음 보는 사람이 바로 읽게 — 표지에 용어 표, 목록은 표로, 문장은 짧게 (사용자 요청 10-20).
스타일과 장 넘기기 스크립트는 10-19 발표 HTML 에서 그대로 읽어 온다. 10-19 발표는 고치지 않는다.
장: 01 전체 흐름도 A~H (체크리스트 t1) — 가운데 세로 흐름(단계·분기) · 왼쪽 파일 · 오른쪽 AI 호출 · 옆 표(누가 하나 · 판정 규칙 · 범례).
    02 10-19 → 지금 (t2) — 단계 띠 두 줄 + 한눈에 표 + 단계별 표. 10-19 는 실행 8c61f117e661 기록으로 확인한 것만.
    03 판정 프롬프트는 고정 (t7) — 심판에게 가는 글의 구성 + 같은 입력을 두 번 만들어 비교. 숫자는 빌드 때마다 AI 호출 없이 다시 계산
       (tools/judge_inputs.py 의 같은 함수로 Q36 입력을 만들고, 10-19 저장소 예시 파일·판정 프롬프트 문서의 sha256 과 맞춰 본다).
그림 속 글이 칸을 넘치거나 03장 비교가 예상과 다르면 빌드할 때 경고를 찍는다.
"""

from __future__ import annotations

import difflib
import hashlib
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from tools import judge_inputs as ji, judge_split as js  # noqa: E402
from webapp.question_generation import flatten  # noqa: E402

DECK = ROOT / "참고자료" / "발표" / "ModelGrade_진행보고_2026-10-19.html"
OUT = ROOT / "참고자료" / "발표" / "ModelGrade_보고_2026-10-20.html"

old = DECK.read_text(encoding="utf-8")
css = "\n".join(f"<style>{block}</style>" for block in re.findall(r"<style>(.*?)</style>", old, re.S))
nav_script = old[old.index("<script>"):old.index("</script>") + len("</script>")]
E = html.escape

extra_css = """<style>
:root { --file: #6a4bb0; --file-soft: #f0ebfa; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --file: #b7a2ee; --file-soft: #231d36; } }
:root[data-theme="dark"] { --file: #b7a2ee; --file-soft: #231d36; }
/* 01 흐름도: 가운데 세로 흐름(단계 A~H·분기) · 왼쪽 파일 · 오른쪽 AI 호출, 그 옆에 요약 표. 좁은 화면에서는 요약 표가 그림 아래로 */
.poster { gap: 14px; }
.poster-sub { align-self: center; border: 1px solid var(--line); background: var(--bg); border-radius: 999px; padding: 4px 14px; font-size: 0.9rem; color: var(--muted); text-align: center; }
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
.r-hold { color: var(--warn); font-weight: 700; }
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
/* 표: 넓은 화면은 칸 너비 고정, 좁은 화면은 가로 스크롤 없이(10-19 발표와 같은 방침) 줄마다 세로로 쌓고 칸 이름을 위에 붙인다 */
.cmpt.steps { min-width: 0; max-width: 100%; }
.steps table { table-layout: fixed; }
.steps td, .steps th { vertical-align: top; }
.steps th .l { font-family: var(--font-num); color: var(--accent); margin-right: 6px; }
.steps code { font-size: 0.86em; overflow-wrap: break-word; }
@media (max-width: 640px) {
  .steps thead, .steps colgroup { display: none; }
  .steps table, .steps caption, .steps tbody, .steps tr, .steps th, .steps td { display: block; width: auto; }
  .steps tbody tr { border-top: 1px solid var(--line); padding: 10px 0; }
  .steps tbody th, .steps tbody td { border: 0; padding-block: 2px; }
  .steps td[data-label]::before { content: attr(data-label); display: block; font-size: 0.76rem; font-weight: 600; color: var(--muted); }
}
.basis { color: var(--muted); font-size: 0.88rem; margin: 0; }
/* 02 단계 띠 두 줄(10-19 / 지금) — 색은 누가 하는지(API · 앱·웹·구독 · 코드 · 안 함) */
.shift { display: grid; gap: 10px; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 16px; min-width: 0; }
.shift-row { display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 10px; align-items: center; }
.shift-k { font-family: var(--font-num); font-weight: 600; font-size: 0.9rem; color: var(--muted); }
.sh-steps { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(9, minmax(0, 1fr)); gap: 6px; }
.sh-chip { border: 1.5px solid var(--line); border-radius: 8px; padding: 6px 8px; font-size: 0.82rem; line-height: 1.35; background: var(--bg); min-width: 0; overflow-wrap: anywhere; }
.sh-chip b { display: block; font-size: 0.86rem; }
.sh-chip small { display: block; color: var(--muted); font-size: 0.76rem; }
.sh-chip.api { border-color: var(--warn); background: var(--warn-soft); }
.sh-chip.part { border-color: var(--warn); border-style: dashed; }
.sh-chip.sub { border-color: var(--ok); background: var(--ok-soft); }
.sh-chip.code { border-color: var(--accent-weak); }
.sh-chip.none { border-style: dashed; color: var(--muted); background: transparent; }
.shift-key { list-style: none; margin: 4px 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 0.82rem; color: var(--muted); }
.shift-key i { display: inline-block; width: 14px; height: 10px; border-radius: 3px; margin-right: 5px; vertical-align: -1px; border: 1.5px solid var(--line); }
.shift-key .api { border-color: var(--warn); background: var(--warn-soft); }
.shift-key .part { border-color: var(--warn); border-style: dashed; }
.shift-key .sub { border-color: var(--ok); background: var(--ok-soft); }
.shift-key .code { border-color: var(--accent-weak); }
.shift-key .none { border-style: dashed; }
.ch-tag { display: inline-block; border-radius: 999px; padding: 1px 9px; font-size: 0.8rem; font-weight: 600; border: 1px solid var(--line); }
.ch-tag.t-same { color: var(--todo); background: var(--todo-soft); border-color: transparent; }
.ch-tag.t-sub { color: var(--ok); background: var(--ok-soft); border-color: transparent; }
.ch-tag.t-chg { color: var(--accent); background: var(--accent-soft); border-color: transparent; }
.ch-tag.t-none { color: var(--muted); border-style: dashed; }
.callout { margin: 0; border-left: 3px solid var(--warn); background: var(--warn-soft); border-radius: 0 8px 8px 0; padding: 10px 14px; font-size: 0.95rem; }
/* 03 심판에게 가는 글을 위에서 아래로 쌓은 모양 — 파랑 = 고정 파일, 초록 = 모델마다 다른 곳(답변), 점선 = 실행 표식 */
.pstack { display: grid; gap: 6px; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px; min-width: 0; }
.ps { border: 1.5px solid var(--line); border-radius: 8px; padding: 8px 12px; display: grid; gap: 2px; background: var(--bg); min-width: 0; overflow-wrap: anywhere; }
.ps b { font-size: 0.95rem; }
.ps span { color: var(--muted); font-size: 0.86rem; }
.ps.prompt { border-color: var(--accent); background: var(--accent-soft); }
.ps.fixed { border-color: var(--accent-weak); }
.ps.mark { border-style: dashed; }
.ps.ans { border-color: var(--ok); background: var(--ok-soft); }
.ps.turn { margin-left: 18px; }
.ps-sep { font-family: var(--font-num); font-size: 0.85rem; color: var(--muted); padding-left: 4px; }
.sha { font-family: var(--font-num); font-size: 0.86em; color: var(--muted); }
/* 04 통과 비율 점 그래프 — 파랑 198 · 주황 155 · 진한 눈금 326 · 회색 띠 = 같은 수 무작위 95% 범위 */
.rate-chart { --s198: #2a78d6; --s155: #eb6834; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) .rate-chart { --s198: #3987e5; --s155: #d95926; } }
:root[data-theme="dark"] .rate-chart { --s198: #3987e5; --s155: #d95926; }
.rate-chart svg { width: 100%; height: auto; display: block; }
.rate-chart text { font-family: var(--font-body); }
.rate-chart .grid { stroke: var(--line); stroke-width: 1; }
.rate-chart .tick { fill: var(--muted); font-size: 12px; font-family: var(--font-num); }
.rate-chart .mname { fill: var(--ink); font-size: 14px; font-weight: 600; }
.rate-chart .ref { stroke: var(--ink); stroke-width: 2.5; }
.rate-chart .reflab { fill: var(--muted); font-size: 11.5px; font-family: var(--font-num); }
.rate-chart .band { fill: var(--line); }
.rate-chart .d198 { fill: var(--s198); stroke: var(--surface); stroke-width: 2; }
.rate-chart .d155 { fill: var(--s155); stroke: var(--surface); stroke-width: 2; }
.rate-chart .val { fill: var(--ink); font-size: 12px; font-family: var(--font-num); stroke: var(--surface); stroke-width: 4px; paint-order: stroke; stroke-linejoin: round; }
.rate-chart .reflab { stroke: var(--surface); stroke-width: 4px; paint-order: stroke; stroke-linejoin: round; }
.rate-key { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 0.86rem; color: var(--muted); }
.rate-key i { display: inline-block; vertical-align: -1px; margin-right: 6px; }
.rate-key .k198 { width: 11px; height: 11px; border-radius: 50%; background: var(--s198); }
.rate-key .k155 { width: 11px; height: 11px; border-radius: 50%; background: var(--s155); }
.rate-key .kref { width: 3px; height: 13px; background: var(--ink); }
.rate-key .kband { width: 22px; height: 9px; border-radius: 5px; background: var(--line); }
/* 05 던지는 순서 선 그래프 — 파랑 기준 많은 순 · 주황 모자란 기준부터 · 회색 점선 번호순 (04장과 같은 색) */
.order-charts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
.order-charts > div { min-width: 0; }
.order-charts h3 { font-size: 1rem; margin: 0 0 6px; }
@media (max-width: 900px) { .order-charts { grid-template-columns: minmax(0, 1fr); } }
.rate-chart .ln { fill: none; stroke-width: 2; stroke-linejoin: round; }
.rate-chart .ln-kdesc { stroke: var(--s198); }
.rate-chart .ln-greedy { stroke: var(--s155); }
.rate-chart .ln-number { stroke: var(--muted); stroke-dasharray: 5 4; }
.rate-chart .guide { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 2 3; }
.rate-chart .pt-kdesc { fill: var(--s198); stroke: var(--surface); stroke-width: 2; }
.rate-chart .pt-greedy { fill: var(--s155); stroke: var(--surface); stroke-width: 2; }
.rate-chart .pt-number { fill: var(--muted); stroke: var(--surface); stroke-width: 2; }
.rate-chart .axt { fill: var(--muted); font-size: 12px; }
.rate-chart .hit { fill: transparent; }
.rate-chart .hit:hover { fill: var(--line); fill-opacity: 0.5; }
.rate-key .knum { width: 18px; height: 0; border-top: 2px dashed var(--muted); vertical-align: 3px; }
.rate-key .kkd { width: 18px; height: 0; border-top: 2px solid var(--s198); vertical-align: 3px; }
.rate-key .kgr { width: 18px; height: 0; border-top: 2px solid var(--s155); vertical-align: 3px; }
/* 06 k 급증 — 진한 선 = 기록된 FAIL, 파랑 = J5 하나로만 FAIL 뺀 것, 주황 막대 = 형식 요구 3개↑ (실험), 진한 마름모 = 지금 326 세트 */
.rate-chart .ln-ink { stroke: var(--ink); }
.rate-chart .ln-blue { stroke: var(--s198); }
.rate-chart .pt-ink { fill: var(--ink); stroke: var(--surface); stroke-width: 2; }
.rate-chart .pt-blue { fill: var(--s198); stroke: var(--surface); stroke-width: 2; }
.rate-chart .kband { fill: var(--accent-soft); }
.rate-chart .bar { fill: var(--s155); }
.rate-chart .dia { fill: var(--ink); stroke: var(--surface); stroke-width: 1.5; }
.rate-key .kink { width: 18px; height: 0; border-top: 2px solid var(--ink); vertical-align: 3px; }
.rate-key .kbar { width: 11px; height: 11px; border-radius: 2px; background: var(--s155); }
.rate-key .kdia { width: 9px; height: 9px; background: var(--ink); transform: rotate(45deg); }
.rate-key .kband2 { width: 18px; height: 11px; background: var(--accent-soft); border-radius: 2px; }
.rnum { font-family: var(--font-num); }
.rsub { display: block; color: var(--muted); font-size: 0.8rem; }
@media (max-width: 900px) { .sh-steps { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 480px) { .shift-row { grid-template-columns: minmax(0, 1fr); } .sh-steps { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>"""


# ---------- 그림 조각 ----------
WARN: list = []


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


# ---------- 01 흐름도 A~H ----------
P: list = []
y = 8
P += pill(CC, y, 340, 42, "시작 · python tools/modelgrade.py")
y += 42
P.append(arrow(CC, y, CC, y + 24))
y += 26

A_y = y
parts, A_h, _ = module(y, "A", "질문 세트 — 고정", [
    ("질문", ["정밀 326개 · 간단 78개(그중 골라냄)"]),
    ("원칙", ["새로 만들지 않음 · 고칠 땐 버전을 올림"]),
], "set")
P += parts
f1_y = A_y + 10
parts, f1_h = filenode(f1_y, "질문 세트 파일", ["질문 326 · 간단 78", "지금 v2.2"])
P += parts
P.append(file_read(A_y + 30, f1_y + 26))
y += A_h
P.append(arrow(CC, y, CC, y + 24))
y += 26

B_y = y
parts, B_h, spots = module(y, "B", "답변 수집 — 네 모델 동시", [
    ("앱", ["ChatGPT · Claude 데스크톱 앱"]),
    ("웹", ["Gemini · AIONA(개발 서버)"]),
    ("방법", ["화면을 자동 조작해 질문 → 복사 버튼 원문", "첨부는 붙여넣기 · 받은 질문은 건너뜀"]),
], "ai")
P += parts
parts, _ = aibox(B_y, "AI · 답하는 모델 4개", ["GPT-6 · Medium", "Claude Sonnet 5.5 · 중간", "Gemini 3.6 Flash · 보통",
                                             "AIONA", "로그인은 사람이 미리"])
P += parts
P.append(ai_link(spots[0][0] + spots[0][1] / 2))
y += B_h

# 분기 1 — 답을 받았나?
P.append(arrow(DX, y, DX, y + 22))
y += 22
cy1 = y + 30
P += diamond(DX, cy1, 190, 60, ["답을 받았나?"])
parts, _ = side(cy1 - 24, ["복사 실패 → 보류", "답 없음 → FAIL"])
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
parts, D_h, _ = module(y, "D", "CODE 평가 — 28가지 검사, 코드", [
    ("검사", ["형식·글자 수·계산 등 — 저장 직후 바로"]),
    ("결과", ["검사마다 PASS·FAIL 과 이유 (모델당 1~2초)"]),
], "code")
P += parts
y += D_h
f2_y = C_y + 6
parts, f2_h = filenode(f2_y, "질문·답변 JSON", ["실행 기록 · 결과 JSON", "CODE 결과도 함께"])
P += parts
P += [file_write(C_y + 30, f2_y + 22), file_write(D_y + 30, f2_y + f2_h - 12)]

# 판정 묶음 — 되돌아오는 자리
P.append(arrow(CC, y, CC, y + 24))
y += 26
R1_y = y
P += [rect(CC - 170, y, 340, 40, "pr", 8), text(CC, y + 25, "판정은 20개씩 묶어서", "prt", 14, 316, "middle")]
y += 40

# 분기 2 — 빈 답을 요구하는 질문인가?
P.append(arrow(DX, y, DX, y + 22))
y += 22
cy2 = y + 32
P += diamond(DX, cy2, 210, 64, ["빈 답을 요구하는", "질문인가?"])
parts, _ = side(cy2 - 33, ["심판 없이 정함", "빈 답 → 보류", "글을 씀 → FAIL"])
P += parts
P += [path(f"M{DX + 105},{cy2:g} L{SX - 2},{cy2:g}"), lab(DX + 112, cy2 - 7, "예")]
y = cy2 + 32
P += [arrow(DX, y, DX, y + 24), lab(DX + 7, y + 16, "아니오")]
y += 26

# E — 심판 둘이 같은 JSON 을 본다 (판정 v4: 상황을 심판이 찾는다)
E_y = y
parts, E_h, spots = module(y, "E", "의미 평가 — 심판 둘이 같은 JSON 을 봄", [
    ("입력", ["고정 프롬프트 v4 + 질문·답변 JSON", "판정할 항목은 안 줌 · 모델 이름 숨김"]),
    ("Codex", ["상황 찾기 → J1~J8 판정 → 결론", "이유·인용 · 빠진 턴은 다시 물음"]),
    ("Jev", ["J1~J4 를 함께 판정", "턴마다 한 번 · 1~4초"]),
], "ai")
P += parts
parts, _ = aibox(spots[1][0] - 1, "AI · 심판 1 — Codex CLI", ["GPT-6 Luna · low · 구독"])
P += parts
parts, _ = aibox(spots[2][0] - 1, "AI · 심판 2 — Jev", ["외부 API (TypeSafe)"])
P += parts
P += [ai_link(spots[1][0] + spots[1][1] / 2), ai_link(spots[2][0] + spots[2][1] / 2)]
parts, f3_h = filenode(E_y + 10, "고정 프롬프트", ["판정 프롬프트 v4", "Jev 질문 묶음 v4"])
P += parts
P.append(file_read(E_y + 52, E_y + 36))
f4_y = E_y + 22 + f3_h
parts, f4_h = filenode(f4_y, "질문·답변 JSON", ["C 에서 저장한 것", "질문 하나씩"])
P += parts
P.append(file_read(spots[0][0] + 30, f4_y + 26))
y = E_y + E_h
P.append(arrow(CC, y, CC, y + 24))
y += 26

F_y = y
parts, F_h, _ = module(y, "F", "합치기·보류 — 규칙, 코드", [
    ("확정", ["Codex FAIL → FAIL", "그 밖에는 Codex 판정대로"]),
    ("보류", ["Jev FAIL + Codex FAIL 아님 (갈림)", "인용이 원문에 없음 · 판정 없음"]),
], "code")
P += parts
y += F_h
P.append(arrow(CC, y, CC, y + 24))
y += 26

G_y = y
parts, G_h, _ = module(y, "G", "질문 단위 판정 — 코드", [
    ("판정", ["FAIL 하나라도 → FAIL", "아니면 보류 하나라도 → 보류", "나머지 → PASS"]),
    ("단위", ["대화는 턴 전체를 묶어 질문 1개"]),
], "code")
P += parts
# 분기 결과 칸 두 개는 오른쪽 통로로 모여 G 로 바로 간다
P += [path(f"M{CR},{cy1:g} L{LANE_R},{cy1:g} L{LANE_R},{G_y + 24} L{CR + 2},{G_y + 24}"),
      path(f"M{CR},{cy2:g} L{LANE_R},{cy2:g}", marker=None), lab(LANE_R + 6, G_y + 28, "바로 G 로")]
y += G_h
P.append(arrow(CC, y, CC, y + 24))
y += 26

H_y = y
parts, H_h, _ = module(y, "H", "결과·보류 추출 — 명령 하나", [
    ("명령", ["python tools/modelgrade.py report"]),
    ("결과", ["모델별 PASS·FAIL·보류 / 326", "질문별 이유 · 보류 목록"]),
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

# 분기 3 — 남은 질문이 있나? 예면 판정 묶음으로 되돌아간다
P.append(arrow(CC, y, CC, y + 22))
y += 22
cy3 = y + 30
P += diamond(CC, cy3, 200, 60, ["남은 질문이 있나?"])
P += [path(f"M{CC - 100},{cy3:g} L{LANE_L},{cy3:g} L{LANE_L},{R1_y + 20} L{CC - 172},{R1_y + 20}"),
      lab(CC - 106, cy3 - 7, "예 — 다음 20개", "end")]
y = cy3 + 30
P += [arrow(CC, y, CC, y + 24), lab(CC + 7, y + 16, "아니오")]
y += 26
P += pill(CC, y, 340, 42, "끝 · 보류는 사람이 정함")
y += 52

MARKERS = "".join(
    f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
    f'<path d="M0,0 L10,5 L0,10 z" class="{cls}"/></marker>' for mid, cls in (("pa", "pah"), ("paw", "pahw"), ("paf", "pahf")))
flow = (f'<svg class="pflow pflow-svg" viewBox="0 0 {W} {y:g}" role="img" aria-label="평가 흐름도 — 질문 세트에서 결과까지 A부터 H, '
        f'분기·AI 호출·파일 표시"><defs>{MARKERS}</defs>\n' + "\n".join(P) + "\n</svg>")


# ---------- 표 ----------
PASS, FAIL, HOLD = '<span class="r-pass">PASS</span>', '<span class="r-fail">FAIL</span>', '<span class="r-hold">보류</span>'


def table(caption, head, rows, widths):
    """칸은 HTML 그대로. 좁은 화면에서 줄이 세로로 쌓여도 칸 이름(data-label)이 보인다."""
    cols = "<colgroup>" + "".join(f'<col style="width:{w}%">' for w in widths) + "</colgroup>"
    thead = "<thead><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead>"
    body = "".join("<tr>" + f'<th scope="row">{r[0]}</th>' + "".join(f'<td data-label="{E(h)}">{c}</td>' for h, c in zip(head[1:], r[1:]))
                   + "</tr>" for r in rows)
    cap = f"<caption>{caption}</caption>" if caption else ""
    return f'<div class="cmpt steps"><table>{cap}{cols}{thead}<tbody>{body}</tbody></table></div>'


def mini(title, head, rows, cls=""):
    thead = "<thead><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead>"
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="pcard {cls}"><h4>{title}</h4><table class="ptab">{thead}<tbody>{body}</tbody></table></div>'


def step(letter, name):
    return f'<span class="l">{letter}</span>{name}'


def files(*names):
    """파일 이름은 '/' 뒤에서만 줄을 바꾼다 — 글자 단위로 끊기지 않게."""
    return "<br>".join("<code>" + E(n).replace("/", "/<wbr>") + "</code>" for n in names)


def details(title, body, note=""):
    return (f'<details class="src"><summary><span>{E(title)}{f" <span class=muted>— {E(note)}</span>" if note else ""}</span>'
            f'<span class="muted">{len(body):,}자</span></summary><pre>{E(body)}</pre></details>')


def legend_item(shape, label):
    return f'<li><svg class="pflow" width="40" height="22" viewBox="0 0 40 22" aria-hidden="true">{shape}</svg>{label}</li>'


legend = ('<div class="pcard"><h4>도형</h4><ul class="p-legend">'
          + legend_item('<rect class="pp" x="2" y="5" width="36" height="12" rx="6"/>', "시작·끝")
          + legend_item('<rect class="pm code" x="2" y="3" width="36" height="16" rx="3"/>', "코드가 하는 단계")
          + legend_item('<rect class="pm ai" x="2" y="3" width="36" height="16" rx="3"/>', "AI 가 끼는 단계")
          + legend_item('<polygon class="pd" points="20,2 38,11 20,20 2,11"/>', "갈림길")
          + legend_item('<rect class="pai" x="2" y="3" width="36" height="16" rx="3"/>', "AI 호출")
          + legend_item('<path class="pf" d="M4,2 H28 L36,10 V20 H4 Z"/>', "파일")
          + "</ul></div>")

side_cards = (
    mini("누가 하나", ["단계", "누가"], [
        ["A·C·D·F·G·H", "코드 (정해진 규칙)"],
        ["B 답변 수집", "답하는 모델 4개 — 앱·웹 자동 조작"],
        ["E 의미 평가", "심판 2개 — Codex(구독) · Jev(API)"],
        ["보류 정리", "사람"],
    ], "ai")
    + mini("판정 규칙", ["상황", "결과"], [
        ["답을 못 받음", FAIL],
        ["답 복사 실패", HOLD],
        ["빈 답 요구 질문", f"빈 답 {HOLD} · 글 {FAIL}"],
        ["Codex FAIL", FAIL],
        ["Jev FAIL + Codex FAIL 아님", HOLD],
        ["인용 불일치 · 판정 없음", HOLD],
    ])
    + legend)

steps_table = table("단계별 요약", ["단계", "하는 일", "누가", "파일"], [
    [step("A", "질문 세트"), "고정된 질문 326개와 그중 78개. 고칠 때만 버전을 올림.", "고정 파일",
     files("questions.json", "질문세트_최소78/ids.json")],
    [step("B", "답변 수집"), "네 모델에 동시에 묻고 복사 버튼으로 원문을 받음. 첨부는 붙여넣기.", "앱·웹 자동 조작",
     files("modelgrade.py collect", "uia_chat_app.py", "cdp_chat_web.py")],
    [step("C", "JSON 저장"), "질문마다 질문·첨부·답변 원문과 수집 상태·시간을 바로 저장. 멈춰도 이어서.", "코드",
     files("data/comparison_runs/<실행>.json", "참고자료/결과/<실행>_*.json")],
    [step("D", "CODE 평가"), "형식·글자 수·계산 등 28가지를 프로그램이 검사.", "코드", files("code_eval/engine.py")],
    [step("E", "의미 평가"), "고정 프롬프트 v4 + 질문·답변 JSON 을 Codex·Jev 에 함께 보냄. Codex 가 상황을 찾고 J1~J8 판정과 결론을, "
     "Jev 는 J1~J4 를 판정. 지금은 시험 단계(3건).", "Codex(구독) · Jev(API)", files("judge_v4.py", "judge_prompt_v4.txt")],
    [step("F", "합치기·보류"), "다시 묻지 않고 규칙으로 확정하거나 보류.", "코드", files("judge_v4.py · combine")],
    [step("G", "질문 단위 판정"), f"{FAIL} 하나라도 → {FAIL}, 아니면 보류 하나라도 → {HOLD}, 나머지 {PASS}.", "코드", files("judge_run.py")],
    [step("H", "결과·보류 추출"), "모델별 결과 · 질문별 이유 · 보류 목록을 명령 하나로.", "코드",
     files("modelgrade.py report", "export_results_json.py")],
], [13, 45, 18, 24])

# ---------- 02 바뀐 점 — 기존 → 현재 (사용자 10-20: 간략하게, 기존 → 현재만) ----------
# 기존 = 10-19 정밀 326 실행 8c61f117e661 의 기록(참고자료/결과/8c61f117e661_정밀326_2026-10-19.json), 현재 = 10-20 저장소 코드와 간단 78 실행 ac6507c00276.
# (항목, 바뀐 정도(chg·small·same·none), 기존, 현재)
CHANGES = [
    ("답변 수집 방법", "chg", "API 3 (GPT·Claude·Gemini) · 웹 1 (AIONA)", "앱 2 (ChatGPT·Claude 데스크톱) · 웹 2 (Gemini·AIONA)"),
    ("답을 받는 법", "chg", "API 응답 글 그대로", "화면의 복사 버튼으로 원문 — 화면 글은 읽지 않음"),
    ("질문 세트", "small", "326개 v2.0", "326개 v2.2 — 문서형 53개에 '채팅 본문 글로' 한 문장 (기준·78개 그대로)"),
    ("질문 생성", "none", "GPT-6 Luna API 로 생성", "안 함 — 세트 고정"),
    ("심판에게 주는 것", "chg", "질문마다 코드가 조립 (판정할 항목 목록 포함)", "고정 프롬프트 v4 + 질문·답변 JSON — 심판이 상황을 찾음 (03장)"),
    ("심판", "chg", "GPT-6 Luna API + Jev API", "Codex CLI (ChatGPT 구독) + Jev API"),
    ("판정 단위", "chg", "턴마다 (모델당 487번)", "질문마다 (모델당 326번)"),
    ("심판이 갈릴 때", "chg", "Luna 를 다시 불러 재판정 → 또 갈리면 보류", "다시 부르지 않고 보류"),
    ("빈 출력 요구 질문", "small", "답과 상관없이 보류", "복사할 답이 없으면 보류 · 글을 쓰면 FAIL"),
    ("실행", "chg", "웹 화면(서버)", "명령 하나 — 메뉴 1 간단 78 · 2 정밀 326"),
    ("CODE 평가", "same", "28가지 검사", "28가지 검사"),
]
CHANGE_TAG = {"chg": ("t-chg", "바뀜"), "small": ("t-sub", "조금"), "same": ("t-same", "그대로"), "none": ("t-none", "안 함")}


def change_rows():
    out = []
    for i, (name, kind, before, after) in enumerate(CHANGES):
        cls, tag = CHANGE_TAG[kind]
        out.append(f'<li class="chg-row{" first" if i == 0 else ""}"><span class="chg-name">{E(name)}</span>'
                   f'<span class="chg-old">{E(before)}</span><span class="chg-arrow" aria-hidden="true">→</span>'
                   f'<span class="chg-new">{E(after)}</span><span class="ch-tag {cls}">{tag}</span></li>')
    return "".join(out)


change_list = f"""
  <div class="chg-head" aria-hidden="true"><span>항목</span><span>기존 (10-19)</span><span></span><span>현재 (10-20)</span><span></span></div>
  <ol class="chg-list">{change_rows()}</ol>"""

# ---------- 03 평가 프롬프트 — 고정 프롬프트 v4 + 질문·답변 JSON ----------
# v4 는 심판이 질문을 읽고 상황(판정할 항목)을 직접 찾는다 (사용자 10-20). 숫자는 빌드 때마다 같은 함수로 다시 계산 — AI 호출 없음.
RESULT_1019 = ROOT / "참고자료" / "결과" / "8c61f117e661_정밀326_2026-10-19.json"
PROMPT_V4 = ROOT / "참고자료" / "프롬프트" / "judge_prompt_v4.txt"
JEV_V4 = ROOT / "참고자료" / "프롬프트" / "jev_questions_v4.json"
TIMES = json.loads((ROOT / "참고자료" / "결과" / "시간_요약_ac6507c00276.json").read_text(encoding="utf-8"))


def sha16(text_):
    return hashlib.sha256(text_.encode("utf-8")).hexdigest()[:16]


def prompt_parts(prompt):
    """프롬프트를 '# ' 제목으로 나눠 (제목, 글자 수)."""
    parts, title, buf = [], "머리말 — 할 일 세 가지", []
    for line in prompt.splitlines(keepends=True):
        if line.startswith("# "):
            parts.append((title, len("".join(buf))))
            title, buf = line[2:].strip(), []
        buf.append(line)
    parts.append((title, len("".join(buf))))
    return parts


def prompt_demo_v4(qid=143):
    from tools.judge_v4 import question_json
    prompt = PROMPT_V4.read_text(encoding="utf-8")
    rows = sorted((r for r in flatten(json.loads((ji.DEFAULT_SET / "questions.json").read_text(encoding="utf-8")))
                   if ji._question(r["case_id"]) == qid), key=ji._turn)
    run = json.loads(RESULT_1019.read_text(encoding="utf-8"))

    def answers(provider):
        return {t["case_id"]: t["answer"] for t in run["providers"][provider]["turns"] if int(t["question"]) == qid}

    def text_for(provider):
        return json.dumps(question_json(qid, rows, answers(provider), {}), ensure_ascii=False, indent=1)

    a1, a2, g = text_for("aiona"), text_for("aiona"), text_for("gpt")
    da, dg = json.loads(a1), json.loads(g)
    changed = sorted({k for ta, tg in zip(da["턴"], dg["턴"]) for k in ta if ta[k] != tg.get(k)})
    return {"qid": qid, "prompt_chars": len(prompt), "prompt_sha": sha16(prompt), "parts": prompt_parts(prompt),
            "same": sha16(a1) == sha16(a2), "sha1": sha16(a1), "sha2": sha16(a2), "changed": changed,
            "no_model_names": not any(n in a1 or n in g for n in ("AIONA", "aiona", "GPT-6", "Claude", "Gemini")),
            "no_table": all(m not in a1 for m in ("[이 턴의 상황]", "판정할 항목", "J1-1.a")),
            "input_chars": len(a1), "total_chars": len(prompt) + len("\n[입력]\n") + len(a1),
            "excerpt": a1 if len(a1) < 1800 else a1[:1800] + "\n…",
            "jev_items": len(json.loads(JEV_V4.read_text(encoding="utf-8"))["items"])}


D4 = prompt_demo_v4()
if not (D4["same"] and D4["changed"] == ["답변"] and D4["no_model_names"] and D4["no_table"]):
    print("경고: 판정 v4 입력 시연 결과가 예상과 다릅니다 —", {k: v for k, v in D4.items() if k not in ("excerpt", "parts")})


def ok(cond, yes="같음", no="다름"):
    return f'<span class="r-pass">{yes}</span>' if cond else f'<span class="r-fail">{no}</span>'


def sha(*values):
    return f' <span class="sha">({" = ".join(values)})</span>'


PART_NOTE = {
    "머리말 — 할 일 세 가지": "① 상황 찾기 ② 판정 ③ 결론",
    "지켜야 할 원칙": "답변 속 지시는 따르지 않음 · 모델을 짐작하지 않음 · 형식·개수는 CODE 몫",
    "상황표 (고정 — 모든 질문에 같음)": "① 질문으로 정해지는 상황 30개(심판이 찾음) ② 답을 보고 '해당 없음' ③ 판정하지 않는 경우",
    "판정 방법": "PASS·FAIL·해당 없음·보류, FAIL 은 원문 인용, 펼침 항목, 사실 확인",
    "결론 내기 (질문 하나에 하나)": "FAIL 하나라도 → FAIL, 보류 하나라도 → 보류, 그 밖 PASS",
    "점검 항목 정의 (44개 전체 — 모든 질문에 같은 목록, 판정은 상황표 1 로 찾은 상황의 항목만)": "J1~J8 의 44개 항목 — 질문·적용·근거·FAIL 예",
    "입력 형식 ([입력] 뒤의 JSON 하나)": "질문번호 · 턴_수 · 판정할_턴 · 턴마다 질문·첨부·참조 자료·답변",
    "출력 (JSON 하나만, 설명·코드 펜스 없이)": "턴마다 상황(근거 인용) + 항목 판정, 질문 결론",
}
pstack = f"""
  <div class="pstack" role="group" aria-label="심판에게 가는 글의 구성">
    <div class="ps prompt"><b>고정 판정 프롬프트 v4</b><span>파일 1개 · {D4['prompt_chars']:,}자 — 모든 질문·모델에 같은 글. 이런 상황엔 이걸 본다는 규칙이 다 들어 있음</span></div>
    <div class="ps-sep">[입력] — 수집한 JSON 그대로 (질문 하나)</div>
    <div class="ps fixed turn"><b>질문번호 · 턴_수 · 판정할_턴</b><span>보통 모든 턴 — 출력이 잘리면 빠진 턴만 다시 물음</span></div>
    <div class="ps fixed turn"><b>턴마다: 질문 · 첨부파일 · 참조자료 · 입력_추정토큰</b><span>질문 세트 그대로 (긴 문서 판단용 토큰 수만 코드가 셈)</span></div>
    <div class="ps ans turn"><b>턴마다: 답변</b><span>수집한 원문 — 모델마다 다른 곳은 여기뿐 (모델 이름은 넣지 않음)</span></div>
    <div class="ps-sep">판정할 항목 목록(판정상황표)은 넣지 않음 — 결과 비교에만 씀</div>
  </div>"""

parts_table = table("프롬프트 안에 든 것", ["부분", "무엇", "글자 수"], [
    [E(t.split(" (")[0]), E(PART_NOTE.get(t, "")), f'<span class="rnum">{n:,}</span>'] for t, n in D4["parts"]
], [30, 56, 14])

v3v4_table = table("v3 → v4 무엇이 바뀌었나", ["무엇", "v3 (10-19~10-20 오전)", "v4 (지금 시험)"], [
    ["판정할 항목 정하기", "코드가 질문 글로 미리 정해 [이 턴의 상황]으로 줌", "<b>심판이 질문을 읽고 상황표로 찾음</b> — 근거 문장을 인용"],
    ["입력 모양", "구분 줄(실행 표식)로 나눈 글", "질문·답변 JSON"],
    ["출력", "항목 판정", "상황 + 항목 판정 + 질문 결론(PASS·FAIL·보류)"],
    ["Jev", "Jev 먼저 → FAIL·애매한 항목만 Codex", f"같은 JSON 을 함께 봄 — J1~J4 {D4['jev_items']}문항, 턴마다 한 번"],
    ["갈릴 때", "Jev FAIL + Codex PASS → 보류", "Jev FAIL + Codex FAIL 아님 → 보류"],
], [22, 39, 39])

check_table = table(f"Q{D4['qid']} 로 직접 만들어 본 입력 — AI 호출 없음", ["확인한 것", "결과"], [
    ["같은 질문·같은 답으로 두 번 만들면", ok(D4["same"]) + sha(D4["sha1"], D4["sha2"])],
    ["답만 바꾸면 (AIONA → GPT)", "바뀌는 칸: " + ", ".join(D4["changed"]) + " · 모델 이름 " + ok(D4["no_model_names"], "없음", "있음")],
    ["입력에 판정할 항목 목록이 있나", ok(D4["no_table"], "없음", "있음")],
    ["Codex 가 받는 전체 글", f"{D4['total_chars']:,}자 = 프롬프트 {D4['prompt_chars']:,} + 입력 {D4['input_chars']:,}" + sha(D4["prompt_sha"])],
], [42, 58])

MODEL_NAMES_SHORT = {"gpt": "GPT", "aiona": "AIONA", "claude": "Claude", "gemini": "Gemini"}
V4ROWS = (TIMES.get("judge_v4") or {}).get("rows") or []
VERDICT_HTML = {"PASS": PASS, "FAIL": FAIL, "HOLD": HOLD}


def trial_name(r):
    turns = f" {r['turns']}턴" if r["turns"] > 1 else ""
    return f"Q{r['question']} · {E(MODEL_NAMES_SHORT.get(r['provider'], r['provider']))} · {E(r['kind'])}{turns}"


trial_table = table(f"판정 v4 시험 — {len(V4ROWS)}건 (10-20, 간단 78 실행의 답)", ["질문", "결과", "Codex 결론", "걸린 시간", "Jev·Codex 같은 판정"], [
    [trial_name(r),
     VERDICT_HTML.get(r["verdict"], E(r["verdict"])), VERDICT_HTML.get(r["codex_verdict"], E(str(r["codex_verdict"]))),
     f"Codex {r['codex_seconds']:.0f}초({r['codex_calls']}번) · Jev {r['jev_seconds']:.1f}초({r['jev_calls']}번)",
     f'<span class="rnum">{r["agree"][0]}/{r["agree"][1]}</span>' if r.get("agree") else "-"] for r in V4ROWS
], [26, 10, 14, 30, 20]) if V4ROWS else ""

trial_points = table("시험에서 알게 된 것", ["무엇", "어떻게 했나 · 남은 것"], [
    ["Codex(추론 low)가 10턴 질문을 한 번에 다 답하지 않음", "빠진 턴만 '판정할_턴' 으로 다시 물음 — Q17 은 3번 만에 10턴"],
    ["Jev 에 10턴 × 27문항을 한 번에 보내면 거절", "Jev 서버의 한도(max_tokens_exceeded, 요청 17.9만 자) — 턴마다 나눠 보냄"],
    ["심판이 상황을 잘못 고른 예", "첨부 없는 질문에 첨부 기준(J8) · 요약 요청 없는 질문에 요약 기준(J4-4) — 첨부·길이·턴 번호로 정해지는 조건은 프롬프트에 적고 코드가 다시 확인"],
    ["Jev 가 정상 답에도 '거절' FAIL", "대화 3턴부터 (사유 없음) — 심판 갈림 보류가 늘어남. 다음에 정할 것"],
], [36, 64])

# ---------- 04 질문 수별 통과 비율 (체크리스트 n5) ----------
# 숫자는 tools/subset_pass_rates.py 가 저장소의 326 결과 JSON 으로 다시 센 것(참고자료/최소평가/통과비율_목록별_8c61f117e661.json) — 여기서는 읽기만.
RATES = json.loads((ROOT / "참고자료" / "최소평가" / "통과비율_목록별_8c61f117e661.json").read_text(encoding="utf-8"))
R = {e["key"]: e for e in RATES["lists"]}
R326, R198, R155, R78 = R["all"], R["kdesc20"], R["greedy20"], R["greedy10"]
RANK = RATES["reference_ranking"]
MODEL_NAMES = {"gpt": "GPT-6 Luna", "aiona": "AIONA", "claude": "Claude Sonnet 5.5", "gemini": "Gemini 3.6 Flash"}


def pct(v):
    return f"{v * 100:.1f}%"


def rate_chart():
    """모델마다 한 줄 — 326 은 진한 눈금, 198·155 는 점, 뒤 회색 띠는 같은 수를 무작위로 뽑았을 때의 95% 범위."""
    left, right, top, row = 168, 880, 26, 76
    lo, hi = 0.25, 0.70
    x = lambda v: left + (v - lo) / (hi - lo) * (right - left)
    height = top + row * len(RANK) + 30
    out = [f'<svg viewBox="0 0 900 {height}" role="img" aria-label="모델별 통과율 — 정밀 326 과 198개·155개 목록 비교">']
    for t in (0.3, 0.4, 0.5, 0.6, 0.7):
        out.append(f'<line class="grid" x1="{x(t):.1f}" y1="{top - 8}" x2="{x(t):.1f}" y2="{height - 26}"/>'
                   f'<text class="tick" x="{x(t):.1f}" y="{height - 8}" text-anchor="middle">{int(t * 100)}%</text>')
    for i, p in enumerate(RANK):
        y0 = top + i * row
        out.append(f'<text class="mname" x="0" y="{y0 + 38}">{E(MODEL_NAMES.get(p, p))}</text>')
        for j, (entry, cls) in enumerate(((R198, "d198"), (R155, "d155"))):
            y = y0 + 22 + j * 22
            band = entry["random"]["rate_band"][p]
            v = entry["models"][p]["rate"]
            out.append(f'<rect class="band" x="{x(band["low"]):.1f}" y="{y - 5}" width="{x(band["high"]) - x(band["low"]):.1f}" height="10" rx="5">'
                       f'<title>{entry["n"]}개를 무작위로 뽑으면 95%가 {pct(band["low"])} ~ {pct(band["high"])}</title></rect>')
            out.append(f'<circle class="{cls}" cx="{x(v):.1f}" cy="{y}" r="6.5"><title>{E(MODEL_NAMES.get(p, p))} · {entry["n"]}개 — {pct(v)} '
                       f'(326 대비 {entry["diff_pp"][p]:+.1f}pp)</title></circle>')
            out.append(f'<text class="val" x="{x(v) - 12:.1f}" y="{y + 4}" text-anchor="end">{pct(v)}</text>')
        ref = R326["models"][p]["rate"]
        out.append(f'<line class="ref" x1="{x(ref):.1f}" y1="{y0 + 10}" x2="{x(ref):.1f}" y2="{y0 + 56}"><title>정밀 326 — {pct(ref)}</title></line>'
                   f'<text class="reflab" x="{x(ref) + 6:.1f}" y="{y0 + 10}">326 {pct(ref)}</text>')
    out.append("</svg>")
    return "".join(out)


def rate_cell(entry, p, base=True):
    r = entry["models"][p]
    diff = "" if not base else f' <span class="rsub">326 대비 {entry["diff_pp"][p]:+.1f}pp · 무작위 {pct(entry["random"]["rate_band"][p]["low"])}~{pct(entry["random"]["rate_band"][p]["high"])}</span>'
    return f'<b class="rnum">{pct(r["rate"])}</b>{diff}'


def counts_cell(entry, p):
    r = entry["models"][p]
    return f'<span class="rnum">{r["pass"]} · {r["fail"]} · {r["hold"]}</span>'


gap = {k: round((e["models"][RANK[0]]["rate"] - e["models"][RANK[-1]]["rate"]) * 100, 1) for k, e in (("326", R326), ("198", R198), ("155", R155))}
below = {k: [MODEL_NAMES[p] for p in RANK if not e["inside_random"][p] and e["diff_pp"][p] < 0] for k, e in (("198", R198), ("155", R155))}
diff_range = {k: (min(e["diff_pp"].values()), max(e["diff_pp"].values())) for k, e in (("198", R198), ("155", R155), ("78", R78))}

rate_table = table("통과율 — PASS / 질문 수 (보류는 분모에 둠)", ["모델", f"정밀 {R326['n']}", f"{R198['n']} · 기준 많은 순", f"{R155['n']} · 모자란 기준부터"],
                   [[E(MODEL_NAMES[p]), rate_cell(R326, p, False), rate_cell(R198, p), rate_cell(R155, p)] for p in RANK]
                   + [["순위", "기준", "같음" if R198["same_ranking"] else "<b>다름</b>", "같음" if R155["same_ranking"] else "<b>다름</b>"]],
                   [22, 18, 30, 30])
count_table = table("개수 — PASS · FAIL · 보류", ["모델", f"정밀 {R326['n']}", f"{R198['n']}", f"{R155['n']}"],
                    [[E(MODEL_NAMES[p]), counts_cell(R326, p), counts_cell(R198, p), counts_cell(R155, p)] for p in RANK], [22, 26, 26, 26])
rate_points = table("", ["무엇", "결과"], [
    ["순위", f"198·155 모두 326 과 <b>같음</b> ({' > '.join(MODEL_NAMES[p] for p in RANK)}). 같은 수를 무작위로 뽑아도 "
             f"순위가 같은 비율 {R198['random']['same_ranking_share']:.0%} · {R155['random']['same_ranking_share']:.0%}"],
    ["통과율", f"모두 326 보다 <b>낮음</b> — 198 은 {-diff_range['198'][1]:.1f}~{-diff_range['198'][0]:.1f}pp, "
              f"155 는 {-diff_range['155'][1]:.1f}~{-diff_range['155'][0]:.1f}pp 낮음. 기준이 많은(어려운) 질문을 먼저 골라서. "
              f"무작위 범위보다도 낮은 것(우연이 아님): 198 — {'·'.join(below['198']) or '없음'} / 155 — {'·'.join(below['155']) or '없음'}"],
    ["모델 간 차이", f"1위와 4위 차이 326 {gap['326']:.1f}pp → 198 {gap['198']:.1f}pp → 155 {gap['155']:.1f}pp 로 줄어듦"],
    ["그래서", "고른 목록으로는 <b>순위 비교는 되지만, 통과율 숫자를 326 과 그대로 비교하면 안 됨</b>. "
              f"지금 쓰는 간단 78 도 같은 경향(326 보다 {-diff_range['78'][1]:.1f}~{-diff_range['78'][0]:.1f}pp 낮음, 순위는 같음)"],
], [18, 82])


# ---------- 05 던지는 순서 세 가지 (체크리스트 n4) ----------
# 숫자는 tools/order_curves.py 가 저장소의 326 결과 JSON 으로 센 것(참고자료/최소평가/던지는순서_세가지_8c61f117e661.json) — 여기서는 읽기만.
ORD = json.loads((ROOT / "참고자료" / "최소평가" / "던지는순서_세가지_8c61f117e661.json").read_text(encoding="utf-8"))
ORD_KEYS = ["number", "kdesc", "greedy"]
ORD_NAMES = {k: ORD["orders"][k]["label"] for k in ORD_KEYS}
OT = ORD["totals"]


def order_chart(metric, total, as_pct, reach_key, aria):
    """가로 = 던진 질문 수, 세로 = metric. 세 순서를 선으로, 도달 지점(reach_key)에 점과 숫자."""
    width, height, left, right, top, bottom = 520, 290, 46, 506, 14, 246
    n_all = len(ORD["curves"]["number"][metric])
    x = lambda n: left + n / n_all * (right - left)
    y = lambda v: bottom - v / total * (bottom - top)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{E(aria)}">']
    for f in (0, 0.25, 0.5, 0.75, 1):
        label = f"{int(f * 100)}%" if as_pct else f"{round(f * total)}"
        out.append(f'<line class="grid" x1="{left}" y1="{y(f * total):.1f}" x2="{right}" y2="{y(f * total):.1f}"/>'
                   f'<text class="tick" x="{left - 6}" y="{y(f * total) + 4:.1f}" text-anchor="end">{label}</text>')
    for n in [t for t in (0, 50, 100, 150, 200, 250, 300) if n_all - t >= 40] + [n_all]:   # 끝 눈금과 붙는 눈금은 뺀다
        out.append(f'<text class="tick" x="{x(n):.1f}" y="{bottom + 16}" text-anchor="middle">{n}</text>')
    out.append(f'<text class="axt" x="{right}" y="{height - 4}" text-anchor="end">던진 질문 수</text>')
    if reach_key == "pairs_80":
        out.append(f'<line class="guide" x1="{left}" y1="{y(0.8 * total):.1f}" x2="{right}" y2="{y(0.8 * total):.1f}"/>'
                   f'<text class="reflab" x="{right}" y="{y(0.8 * total) - 5:.1f}" text-anchor="end">80%</text>')
    for k in ORD_KEYS:
        values = ORD["curves"][k][metric]
        pts = " ".join(f"{x(i + 1):.1f},{y(v):.1f}" for i, v in enumerate(values))
        out.append(f'<polyline class="ln ln-{k}" points="{left:.1f},{bottom:.1f} {pts}"/>')
    for j, k in enumerate(ORD_KEYS):   # 도달 지점 — 점 + 질문 수(겹치지 않게 위아래로 엇갈림)
        n = ORD["reach"][k][reach_key]
        if n is None:
            continue
        v = ORD["curves"][k][metric][n - 1]
        out.append(f'<line class="guide" x1="{x(n):.1f}" y1="{y(v):.1f}" x2="{x(n):.1f}" y2="{bottom}"/>'
                   f'<circle class="pt-{k}" cx="{x(n):.1f}" cy="{y(v):.1f}" r="5"/>'
                   f'<text class="val" x="{x(n) + (6 if n < n_all - 20 else -6):.1f}" y="{y(v) + 18 + j * 14:.1f}" '
                   f'text-anchor="{"start" if n < n_all - 20 else "end"}">{n}개</text>')
    step = 2
    for n in range(step, n_all + 1, step):   # 마우스를 올리면 그 질문 수의 세 값
        vals = " · ".join(f"{ORD_NAMES[k]} {ORD['curves'][k][metric][n - 1] / total * 100:.0f}%" if as_pct else
                          f"{ORD_NAMES[k]} {ORD['curves'][k][metric][n - 1]}" for k in ORD_KEYS)
        out.append(f'<rect class="hit" x="{x(n - step):.1f}" y="{top}" width="{x(n) - x(n - step):.2f}" height="{bottom - top}"><title>{n}개 — {E(vals)}</title></rect>')
    out.append("</svg>")
    return "".join(out)


def best_cells(row, key, as_pct, total):
    vals = {k: row[k][key] for k in ORD_KEYS}
    top_v = max(vals.values())
    fmt = (lambda v: f"{v / total * 100:.0f}%") if as_pct else (lambda v: f"{v}")
    return [f'<b class="rnum">{fmt(v)}</b>' if v == top_v and list(vals.values()).count(top_v) < 3 else f'<span class="rnum">{fmt(v)}</span>'
            for v in vals.values()]


reach_table = table("어디서 도달하나 — 질문 수 (적을수록 빠름)", ["도달", *ORD_NAMES.values()], [
    [name, *[f'<b class="rnum">{ORD["reach"][k][key]}개</b>' if ORD["reach"][k][key] == min(ORD["reach"][kk][key] for kk in ORD_KEYS)
             else f'<span class="rnum">{ORD["reach"][k][key]}개</span>' for k in ORD_KEYS]]
    for key, name in (("criteria_all", f"기준 {OT['criteria']}개 모두 10번 이상"), ("pairs_80", f"FAIL 종류 80% ({round(0.8 * OT['pairs'])}/{OT['pairs']})"),
                      ("pairs_all", f"FAIL 종류 전부 ({OT['pairs']})"), ("events_50", f"FAIL 사건 절반 ({OT['events'] // 2}/{OT['events']})"))
], [34, 22, 22, 22])
cover_table = table(f"질문 수별 값 — 기준 10번↑ (/{OT['criteria']}) · FAIL 종류 (/{OT['pairs']}) · FAIL 사건 (/{OT['events']})",
                    ["질문 수", *ORD_NAMES.values()],
                    [[f'<span class="rnum">{m["n"]}</span>',
                      *[f'<span class="rnum">{m[k]["covered10"]} · {m[k]["fail_pairs"]} · {m[k]["fail_events"]}</span>' for k in ORD_KEYS]]
                     for m in ORD["marks"]], [16, 28, 28, 28])
RCH = ORD["reach"]
m155 = next(m for m in ORD["marks"] if m["n"] == 155)
order_points = table("", ["무엇", "결과"], [
    ["기준을 고르게", f"기준 {OT['criteria']}개를 모두 10번 이상 평가하려면 모자란 기준부터 <b>{RCH['greedy']['criteria_all']}개</b> · 번호순 {RCH['number']['criteria_all']}개 · "
                   f"기준 많은 순 {RCH['kdesc']['criteria_all']}개(기준이 적은 질문에만 있는 기준이 맨 뒤로 밀려서)"],
    ["FAIL 종류", f"80%가 보이는 지점 — 모자란 기준부터 <b>{RCH['greedy']['pairs_80']}개</b> · 번호순 {RCH['number']['pairs_80']}개 · 기준 많은 순 {RCH['kdesc']['pairs_80']}개"],
    ["FAIL 개수", f"기준 많은 순이 중간 구간에서 가장 많이 잡음(155개에서 {m155['kdesc']['fail_events']}건 vs {m155['greedy']['fail_events']}·{m155['number']['fail_events']}건) — "
                  "어려운 질문을 먼저 던져서. 04장에서 통과율이 낮게 나온 것과 같은 이유"],
    ["326 에서", f"세 선이 같은 점(기준 {OT['criteria']}개 · FAIL 종류 {OT['pairs']}개 · 사건 {OT['events']}건) — 같은 326개를 순서만 바꾼 것"],
    ["그래서", "적은 질문으로 기준과 FAIL 종류를 넓게 보려면 <b>모자란 기준부터</b>(지금 간단 78 이 이 방식)"],
], [18, 82])


# ---------- 06 평가 횟수 k 와 질문 수 (사용자 10-20 미팅 준비) ----------
# 숫자는 tools/k_reach_curves.py 가 질문 세트로 센 것(참고자료/최소평가/평가횟수k_곡선_326.json) — 여기서는 읽기만.
from code_eval import semantic_items as si  # noqa: E402

KR = json.loads((ROOT / "참고자료" / "최소평가" / "평가횟수k_곡선_326.json").read_text(encoding="utf-8"))
K_ORDERS = ["number", "kdesc", "level"]
K_LINE = {"number": "number", "kdesc": "kdesc", "level": "greedy"}
K_NAMES = {k: KR["orders"][k]["label"] for k in KR["orders"]}
ITEM_NAME = {it.id: si.SUBS_BY_ID[it.sub].name for it in si.ITEMS}
ITEM_NAME["2-1.explicit_empty"] = "빈 출력 요구 (CODE)"
K_YMAX = 22


def k_chart(kind, targets, aria):
    """가로 = 던진 질문 수, 세로 = k(모든 기준이 최소 k번). 세 순서를 계단 선으로, k=10·20 에 닿는 지점에 점과 질문 수."""
    width, height, left, right, top, bottom = 520, 300, 46, 506, 18, 252
    n_all = KR["questions"]
    x = lambda n: left + n / n_all * (right - left)
    y = lambda v: bottom - min(v, K_YMAX) / K_YMAX * (bottom - top)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{E(aria)}">']
    for v in (0, 5, 10, 15, 20):
        out.append(f'<line class="grid" x1="{left}" y1="{y(v):.1f}" x2="{right}" y2="{y(v):.1f}"/>'
                   f'<text class="tick" x="{left - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v}</text>')
    for n in [t for t in (0, 50, 100, 150, 200, 250, 300) if n_all - t >= 40] + [n_all]:
        out.append(f'<text class="tick" x="{x(n):.1f}" y="{bottom + 16}" text-anchor="middle">{n}</text>')
    out.append(f'<text class="axt" x="{right}" y="{height - 4}" text-anchor="end">던진 질문 수</text>'
               f'<text class="axt" x="{left - 6}" y="{top - 6}" text-anchor="end">k</text>')
    for t in (10, 20):
        unreach = kind == "raw" and t > KR["ceiling_raw"]
        out.append(f'<line class="guide" x1="{left}" y1="{y(t):.1f}" x2="{right}" y2="{y(t):.1f}"/>'
                   f'<text class="reflab" x="{left + 6}" y="{y(t) - 5:.1f}">k={t}{" — 326개를 다 던져도 닿지 않음" if unreach else ""}</text>')
    for k in K_ORDERS:
        pts, prev = [f"{left:.1f},{bottom:.1f}"], 0
        for i, v in enumerate(KR["curves"][k][kind]):
            if v != prev:
                pts.append(f"{x(i + 1):.1f},{y(prev):.1f}")
            pts.append(f"{x(i + 1):.1f},{y(v):.1f}")
            prev = v
        out.append(f'<polyline class="ln ln-{K_LINE[k]}" points="{" ".join(pts)}"/>')
    for t in targets:
        for j, k in enumerate(K_ORDERS):
            n = KR["reach"][k][kind][str(t)]
            if n is None:
                continue
            near_end = n > n_all - 40
            out.append(f'<circle class="pt-{K_LINE[k]}" cx="{x(n):.1f}" cy="{y(t):.1f}" r="5"/>'
                       f'<text class="val" x="{x(n) + (-7 if near_end else 7):.1f}" y="{y(t) + 17 + j * 13:.1f}" '
                       f'text-anchor="{"end" if near_end else "start"}">{n}개</text>')
    step = 2
    for n in range(step, n_all + 1, step):
        vals = " · ".join(f"{K_NAMES[k]} k={KR['curves'][k][kind][n - 1]}" for k in K_ORDERS)
        out.append(f'<rect class="hit" x="{x(n - step):.1f}" y="{top}" width="{x(n) - x(n - step):.2f}" height="{bottom - top}"><title>{n}개 — {E(vals)}</title></rect>')
    out.append("</svg>")
    return "".join(out)


def reach_cell(k, kind, t):
    n = KR["reach"][k][kind][str(t)]
    best = min((KR["reach"][o][kind][str(t)] for o in K_ORDERS if KR["reach"][o][kind][str(t)]), default=None)
    if n is None:
        return '<span class="muted">닿지 않음</span>'
    return f'<b class="rnum">{n}개</b>' if n == best and k in K_ORDERS else f'<span class="rnum">{n}개</span>'


K_COLS = K_ORDERS + ["greedy10", "greedy20"]
k_reach_table = table("k 에 닿는 질문 수 — 적을수록 빠름", ["k", *[K_NAMES[k] for k in K_ORDERS], "참고: 간단 78 순서", "참고: 155 목록 순서"], [
    *[[f"모든 기준 {t}번 이상", *[reach_cell(k, "raw", t) for k in K_COLS]] for t in (1, 3, 5, 10, 20)],
    *[[f"기준마다 {t}번 (있는 만큼)", *[reach_cell(k, "cap", t) for k in K_COLS]] for t in (10, 20)],
], [22, 15, 15, 16, 16, 16])

rare_groups = {}
for r in KR["rare_criteria"]:
    rare_groups.setdefault(r["questions"], []).append(r["id"])
k_rare_table = table("326 안에 질문이 20개보다 적은 기준 — 천장을 정함", ["326 안 질문 수", "기준"], [
    [f'<span class="rnum">{n}개</span>', " · ".join(f"{E(i)} {E(ITEM_NAME.get(i, ''))}" for i in ids)] for n, ids in sorted(rare_groups.items())
], [18, 82])

KRR = KR["reach"]
k_points = table("", ["무엇", "결과"], [
    ["세로축 k", f"질문을 n개 던졌을 때 가장 적게 평가된 기준의 평가 횟수 — '모든 기준({KR['criteria']}개)이 최소 k번 평가됨'"],
    ["세 선이 만나나", f"<b>만납니다 — 질문을 다 던진 {KR['questions']}개에서</b>. 같은 질문을 순서만 바꾼 것이라 끝에서는 모든 기준의 평가 횟수가 같아집니다. 그보다 앞에서 세 선이 같은 값이 되는 곳은 없습니다"],
    ["만나는 높이", f"그대로 세면 <b>k = {KR['ceiling_raw']}</b> 이 천장 — 긴 문서 기준(J7-1·J7-2)과 빈 출력 요구가 326 안에 {KR['ceiling_raw']}개씩뿐이라 어떤 순서로도 그 위로 못 감"],
    ["k = 10", f"모자란 기준부터 <b>{KRR['level']['raw']['10']}개</b>에서 닿고 그 뒤로는 평평(천장) · 번호순 {KRR['number']['raw']['10']}개 · 기준 많은 순 {KRR['kdesc']['raw']['10']}개 "
               f"(기준 많은 순은 {KRR['kdesc']['raw']['1']}개째까지 k=0 — 기준이 적은 질문에만 있는 기준이 맨 뒤로 밀려서)"],
    ["k = 20", f"<b>모든 기준으로는 불가능</b> — 대화형 기준(J6) 16개, 계획 실행 가능성(J3-5.a) 19개. 326 안에 20개보다 적은 기준은 있는 질문을 다 쓰면 채운 것으로 세면(198·155 목록 방식) "
               f"모자란 기준부터 <b>{KRR['level']['cap']['20']}개</b> · 번호순 {KRR['number']['cap']['20']}개 · 기준 많은 순 {KRR['kdesc']['cap']['20']}개"],
    ["그래서", f"지금 세트로 '모든 기준 k번'은 10번이 최대이고 {KRR['level']['raw']['10']}~78개면 닿음(간단 78). 모든 기준을 20번 보려면 긴 문서·빈 출력 요구 질문을 각 10개 이상, 대화형을 4개 이상 더 넣어야 함"],
], [18, 82])

# ---------- 07 걸린 시간 — 답변 수집 · CODE · 의미 평가 ----------
# 숫자는 tools/time_summary.py 가 기록으로 센 것(참고자료/결과/시간_요약_ac6507c00276.json) — 여기서는 읽기만.
P_ORDER = ["gpt", "claude", "gemini", "aiona"]
KINDS = ("단일", "대화", "긴 문서")


def dur(sec):
    if sec is None:
        return "-"
    sec = float(sec)
    if sec < 90:
        return f"{sec:.0f}초"
    if sec < 3600:
        return f"{sec / 60:.1f}분"
    h = int(sec // 3600)
    m = round((sec - h * 3600) / 60)
    if m == 60:
        h, m = h + 1, 0
    return f"{h}시간 {m}분"


def collect_table(caption, block, n_total):
    models = block["models"]
    top = max(m["total_seconds"] for m in models.values()) or 1
    rows = []
    for p in P_ORDER:
        m = models[p]
        rows.append([E(MODEL_NAMES[p]) + f'<span class="rsub">{E(m.get("label") or "")}</span>',
                     f'<span class="rnum">{m["questions_done"]}/{n_total}</span>',
                     *[f'<span class="rnum">{dur(m["mean"].get(k))}</span>' for k in KINDS],
                     (f'Q{m["max"]["question"]} {E(m["max"]["kind"])}<br><span class="rnum">{dur(m["max"]["seconds"])}</span>' if m.get("max") else "-"),
                     f'<span class="tbar"><span class="rnum">{dur(m["total_seconds"])}</span><i style="width:{m["total_seconds"] / top * 100:.0f}%"></i></span>',
                     f'<span class="rnum">{m["chars_per_turn"]:,}자</span><span class="rsub">초당 {m["chars_per_second"]}자</span>'])
    return table(caption, ["모델", "모든 턴 받은 질문", "단일 평균", "대화 평균", "긴 문서 평균", "가장 오래 걸린 질문", "합계", "턴당 답 길이"],
                 rows, [17, 10, 9, 9, 9, 14, 16, 16])


C20, C19, EST = TIMES["collect_1020"]["models"], TIMES["collect_1019"]["models"], TIMES["estimate_78"]
SLOW = max(P_ORDER, key=lambda p: C20[p]["total_seconds"])
CODE_T = TIMES.get("code") or {}
code_lo = min((v["seconds"] for v in CODE_T.values()), default=0)
code_hi = max((v["seconds"] for v in CODE_T.values()), default=0)
per = EST["per_question"]
kind326 = {k: C19["gpt"]["count"].get(k, 0) for k in KINDS}
one326 = sum(per[k]["seconds"] * kind326[k] for k in per)
FAST = min(P_ORDER, key=lambda p: C20[p]["total_seconds"])
time_kpis = f"""
  <div class="kpis3">
    <div><span class="k">답변 수집 — 간단 78 (네 모델 동시)</span><b>{dur(C20[SLOW]['total_seconds'])}</b>
      <span>가장 오래 걸린 {E(MODEL_NAMES[SLOW])} · 답을 기다린 시간 합계 (가장 빠른 {E(MODEL_NAMES[FAST])} {dur(C20[FAST]['total_seconds'])})</span></div>
    <div><span class="k">CODE 평가 — 코드</span><b>{int(code_lo)}~{int(code_hi) + 1}초</b><span>모델 하나의 78개 답 — 수집 직후 바로</span></div>
    <div class="bad"><span class="k">의미 평가 — 78개 (추정)</span><b>{dur(EST['seconds_one_model'])}</b>
      <span>모델 하나 기준 · 네 모델을 동시에 돌리면 비슷 (Codex 사용량 한도에 걸리면 더)</span></div>
  </div>"""
V3J = TIMES.get("judge_v3") or {}
v3b = V3J.get("by_kind") or {}
judge_table = table("의미 평가 — 잰 시간과 어림", ["무엇", "시간"], [
    ["CODE 평가", f"모델 하나의 78개 답 {code_lo:.1f}~{code_hi:.1f}초 — AI 없음"],
    [f"10-20 오전 시험 (v3 흐름, {V3J.get('judgments', 0)}건)",
     " · ".join(f"{k} Codex 평균 {v3b[k]['codex']['mean']}초 (최대 {v3b[k]['codex']['max']}초) · Jev {v3b[k]['jev']['mean']}초" for k in KINDS if k in v3b) or "-"],
    [f"판정 v4 시험 ({len(V4ROWS)}건)", "<br>".join(f"{trial_name(r)} — Codex {r['codex_seconds']:.0f}초({r['codex_calls']}번) · Jev {r['jev_seconds']:.1f}초" for r in V4ROWS) or "-"],
    ["78개 어림 (모델 하나)", f"<b>{dur(EST['seconds_one_model'])}</b> = " + " + ".join(f"{k} {EST['counts_78'][k]}개 × {per[k]['seconds']}초" for k in per)
     + "<br><span class=\"rsub\">" + " · ".join(f"{k}: {E(per[k]['basis'])}" for k in per) + "</span>"],
    ["326개 어림 (모델 하나)", f"{dur(one326)} = " + " + ".join(f"{k} {kind326[k]}개 × {per[k]['seconds']}초" for k in per)],
    ["주의", "10턴 대화는 Codex(추론 low)가 한 번에 다 판정하지 못해 턴을 나눠 다시 물어야 함 — 대화형은 어림보다 더 걸릴 수 있음"],
], [26, 74])
time_points = table("", ["무엇", "결과"], [
    ["가장 느린 모델", f"{E(MODEL_NAMES[SLOW])} — 턴당 답이 길고({C20[SLOW]['chars_per_turn']:,}자) 글을 내는 속도가 가장 느림(초당 {C20[SLOW]['chars_per_second']}자). "
                  f"10턴 대화 하나에 평균 {dur(C20[SLOW]['mean'].get('대화'))}"],
    ["10-19 (API) 와 비교", "10-19 는 GPT·Claude·Gemini 를 API 로 받아 질문 하나가 훨씬 빨랐음(단일 5~11초). 지금은 사람이 쓰는 앱·웹 화면이라 생각·화면 표시 시간이 더해짐"],
    ["동시 수집", "네 모델을 동시에 받으므로 실제 걸린 시간은 가장 느린 모델 쪽에 가까움"],
], [18, 82])

# ---------- 08 보류 함께 보기 — 10-19 정밀 326 의 보류 300건 ----------
# 데이터는 tools/build_held_view.py 가 만든 참고자료/발표/보류_8c61f117e661.js (window.HELD_DATA) — 이 HTML 옆에 두고 <script src> 로 읽는다.
HELD_JS_FILE = "보류_8c61f117e661.js"
held_section = """
  <div class="hv" id="hv">
    <div class="hv-sum" id="hv-sum"></div>
    <div class="hv-filters" id="hv-filters" role="group" aria-label="걸러 보기"></div>
    <div class="hv-main">
      <div class="hv-listwrap">
        <div class="hv-count" id="hv-count"></div>
        <ol class="hv-list" id="hv-list" role="listbox" aria-label="보류 질문 목록" tabindex="0"></ol>
      </div>
      <article class="hv-detail" id="hv-detail" aria-live="polite"></article>
    </div>
  </div>
  <p class="basis">출처: 10-19 정밀 326 실행 8c61f117e661 — 질문 단위 보류는 04장과 같은 규칙, 보류 이유는 참고자료/보류목록/8c61f117e661-held.csv, 질문은 10-19 에 던진 v2.0 글, 답변은 참고자료/결과/8c61f117e661_정밀326_2026-10-19.json 원문 (tools/build_held_view.py).
  목록에서 ↑·↓ 로 옮겨 다닐 수 있습니다.</p>"""

EXTRA_CSS2 = """<style>
/* 02 바뀐 점 — 기존 → 현재 한 줄씩 */
.chg-head, .chg-row { display: grid; grid-template-columns: 150px minmax(0, 1fr) 26px minmax(0, 1.25fr) 64px; gap: 10px; align-items: center; }
.chg-head { font-size: 0.8rem; color: var(--muted); font-weight: 600; padding: 0 14px; }
.chg-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
.chg-row { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; font-size: 0.95rem; }
.chg-row.first { border: 2px solid var(--accent); background: var(--accent-soft); }
.chg-name { font-weight: 700; }
.chg-old { color: var(--muted); text-decoration: line-through; text-decoration-color: var(--line); text-decoration-thickness: 1px; }
.chg-arrow { color: var(--accent); font-weight: 700; text-align: center; }
.chg-new { font-weight: 600; }
.chg-row .ch-tag { justify-self: end; }
@media (max-width: 760px) {
  .chg-head { display: none; }
  .chg-row { grid-template-columns: minmax(0, 1fr) auto; }
  .chg-name { grid-column: 1; } .chg-row .ch-tag { grid-column: 2; grid-row: 1; }
  .chg-old, .chg-new { grid-column: 1 / -1; } .chg-arrow { display: none; }
  .chg-new::before { content: "→ "; color: var(--accent); }
}
/* 07 합계 막대 */
.tbar { display: grid; gap: 4px; }
.tbar i { display: block; height: 6px; border-radius: 3px; background: var(--warn); min-width: 2px; }
/* 08 보류 함께 보기 — 왼쪽 목록 · 가운데 질문·답변 원문 · 오른쪽 사유 (사용자 요구: 원문 + 오른쪽 사유, 비유 없이 구체적으로) */
.hv { display: grid; gap: 12px; width: min(1360px, calc(100vw - 48px)); position: relative; left: 50%; transform: translateX(-50%); min-width: 0; }
.hv-sum { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr); gap: 12px; }
.hv-panel { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; min-width: 0; }
.hv-panel h4 { margin: 0 0 8px; font-size: 0.95rem; }
.hv-panel h4 small { color: var(--muted); font-weight: 400; margin-left: 6px; }
.hv-mbar { display: grid; grid-template-columns: 140px minmax(0, 1fr) 82px; gap: 8px; align-items: center; font-size: 0.88rem; margin: 5px 0; }
.hv-mbar .track { background: var(--bg); border-radius: 5px; height: 10px; overflow: hidden; }
.hv-mbar .track i { display: block; height: 10px; background: var(--warn); border-radius: 5px; }
.hv-mbar .num { font-family: var(--font-num); text-align: right; color: var(--muted); }
.hv-why-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
.hv-why-list li { display: grid; grid-template-columns: 112px 54px minmax(0, 1fr); gap: 8px; align-items: baseline; font-size: 0.86rem; }
.hv-why-list .num { font-family: var(--font-num); color: var(--muted); }
.hv-filters { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.hv-fl { font-size: 0.8rem; color: var(--muted); font-weight: 600; margin-right: 4px; }
.hv-seg { display: inline-flex; flex-wrap: wrap; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.hv-seg button { border: 0; border-right: 1px solid var(--line); background: transparent; padding: 5px 10px; font: inherit; font-size: 0.84rem; color: var(--ink); cursor: pointer; }
.hv-seg button:last-child { border-right: 0; }
.hv-seg button[aria-pressed="true"] { background: var(--accent); color: var(--surface); }
.hv-search { font: inherit; font-size: 0.86rem; padding: 5px 8px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); color: var(--ink); width: 120px; }
.hv-main { display: grid; grid-template-columns: 250px minmax(0, 1fr); gap: 12px; align-items: start; }
.hv-listwrap { position: sticky; top: 64px; display: grid; gap: 6px; min-width: 0; }
.hv-count { font-size: 0.82rem; color: var(--muted); padding-left: 4px; }
.hv-list { list-style: none; margin: 0; padding: 6px; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; max-height: calc(100vh - 120px); overflow-y: auto; outline: none; }
.hv-list:focus-visible { box-shadow: 0 0 0 2px var(--accent); }
.hv-item { width: 100%; text-align: left; display: grid; grid-template-columns: 50px minmax(0, 1fr); gap: 1px 8px; padding: 7px 8px; border: 1px solid transparent; border-radius: 8px; background: transparent; color: var(--ink); font: inherit; cursor: pointer; }
.hv-item:hover { background: var(--bg); }
.hv-item[aria-selected="true"] { border-color: var(--accent); background: var(--accent-soft); }
.hv-item .n { font-family: var(--font-num); font-weight: 700; color: var(--accent); grid-row: span 2; font-size: 1rem; padding-top: 1px; }
.hv-item .m { font-weight: 600; font-size: 0.86rem; }
.hv-item .t { font-size: 0.76rem; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hv-detail { min-width: 0; display: grid; gap: 10px; }
.hv-dh { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 6px 12px; }
.hv-dh h3 { margin: 0; font-size: 1.35rem; }
.hv-dh h3 span { font-size: 0.95rem; color: var(--muted); font-weight: 500; margin-left: 8px; }
.hv-badge { border-radius: 999px; padding: 2px 12px; font-weight: 700; font-size: 0.86rem; color: var(--warn); background: var(--warn-soft); }
.hv-why { border-left: 4px solid var(--warn); background: var(--warn-soft); border-radius: 0 10px 10px 0; padding: 12px 16px; }
.hv-why b { display: block; font-size: 0.8rem; color: var(--warn); margin-bottom: 4px; }
.hv-why p { margin: 0; font-size: 1rem; line-height: 1.6; }
.hv-turns { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.hv-turns .hv-fl { margin-right: 2px; }
.hv-turns button { font: inherit; font-size: 0.84rem; border: 1px solid var(--line); background: var(--surface); color: var(--ink); border-radius: 8px; padding: 4px 10px; cursor: pointer; position: relative; }
.hv-turns button.held { border-color: var(--warn); }
.hv-turns button.held::after { content: ""; position: absolute; top: 3px; right: 3px; width: 6px; height: 6px; border-radius: 50%; background: var(--warn); }
.hv-turns button[aria-pressed="true"] { background: var(--accent); color: var(--surface); border-color: var(--accent); }
.hv-cols { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 12px; align-items: start; }
.hv-qa { display: grid; gap: 10px; min-width: 0; }
.hv-box { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; min-width: 0; }
.hv-box > h5 { margin: 0; padding: 9px 14px; border-bottom: 1px solid var(--line); font-size: 0.9rem; display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.hv-box > h5 small { color: var(--muted); font-weight: 400; }
.hv-text { padding: 12px 16px; font-size: 0.95rem; line-height: 1.7; overflow-wrap: anywhere; }
.hv-text.plain { white-space: pre-wrap; }
.hv-qbox .hv-text { background: var(--bg); border-radius: 0 0 10px 10px; max-height: 46vh; overflow-y: auto; }
.hv-abox .hv-text { max-height: 72vh; overflow-y: auto; }
.hv-empty { color: var(--muted); font-style: italic; }
.hv-extra { margin: 0 16px 12px; border: 1px dashed var(--line); border-radius: 8px; font-size: 0.86rem; }
.hv-extra summary { cursor: pointer; padding: 6px 10px; font-weight: 600; }
.hv-extra pre { margin: 0; padding: 8px 10px; white-space: pre-wrap; max-height: 260px; overflow-y: auto; border-top: 1px dashed var(--line); font-size: 0.84rem; }
.hv-raw { font: inherit; font-size: 0.78rem; border: 1px solid var(--line); background: var(--bg); color: var(--ink); border-radius: 6px; padding: 2px 8px; cursor: pointer; }
.md h3, .md h4, .md h5, .md h6 { margin: 14px 0 6px; font-size: 1rem; }
.md h3 { font-size: 1.08rem; }
.md p { margin: 0 0 10px; }
.md ul, .md ol { margin: 0 0 10px; padding-left: 22px; }
.md li { margin: 2px 0; }
.md li.sub { margin-left: 18px; list-style-type: circle; }
.md hr { border: 0; border-top: 1px solid var(--line); margin: 12px 0; }
.md-table { overflow-x: auto; margin: 0 0 10px; }
.md table { border-collapse: collapse; font-size: 0.88rem; }
.md th, .md td { border: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: top; }
.md th { background: var(--bg); }
.md pre { background: var(--bg); padding: 10px; border-radius: 6px; overflow-x: auto; font-size: 0.84rem; white-space: pre; }
.md code { font-family: var(--font-num); font-size: 0.88em; }
.md blockquote { margin: 0 0 10px; padding-left: 12px; border-left: 3px solid var(--line); color: var(--muted); }
.hv-side { position: sticky; top: 64px; display: grid; gap: 10px; max-height: calc(100vh - 80px); overflow-y: auto; min-width: 0; }
.hv-grp { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; display: grid; gap: 8px; }
.hv-grp-h { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.hv-grp-h b { font-size: 0.95rem; }
.hv-cause { border-radius: 999px; padding: 1px 10px; font-size: 0.78rem; font-weight: 700; white-space: nowrap; }
.c-held { color: var(--warn); background: var(--warn-soft); }
.c-source_unavailable { color: var(--accent); background: var(--accent-soft); }
.c-judge_disagreement { color: var(--risk); background: var(--risk-soft); }
.c-unverified, .c-incomplete, .c-pending { color: var(--todo); background: var(--todo-soft); }
.c-empty_output_hold { color: var(--ok); background: var(--ok-soft); }
.hv-plain { margin: 0; font-size: 0.9rem; line-height: 1.55; }
.hv-it { border-top: 1px dashed var(--line); padding-top: 8px; display: grid; gap: 6px; font-size: 0.88rem; min-width: 0; }
.hv-it-h b { font-size: 0.92rem; }
.hv-it-h span { display: block; color: var(--muted); font-size: 0.8rem; }
.hv-jr span { display: block; font-size: 0.76rem; color: var(--muted); font-weight: 600; }
.hv-jr p { margin: 2px 0 0; line-height: 1.5; overflow-wrap: anywhere; }
.hv-claims summary { cursor: pointer; font-size: 0.82rem; color: var(--accent); font-weight: 600; }
.hv-claims ul, .hv-src ul { list-style: none; padding: 0; margin: 6px 0 0; display: grid; gap: 4px; }
.hv-claims li { display: grid; grid-template-columns: 70px minmax(0, 1fr); gap: 6px; font-size: 0.84rem; overflow-wrap: anywhere; }
.tg { font-size: 0.72rem; font-weight: 700; border-radius: 4px; padding: 1px 4px; text-align: center; height: fit-content; }
.tg.ok { color: var(--ok); background: var(--ok-soft); }
.tg.bad { color: var(--risk); background: var(--risk-soft); }
.tg.unk { color: var(--warn); background: var(--warn-soft); }
.hv-src li { font-size: 0.82rem; overflow-wrap: anywhere; }
.hv-src em { font-style: normal; font-weight: 600; color: var(--risk); margin-right: 4px; }
.hv-none { color: var(--muted); font-size: 0.88rem; margin: 0; }
.hv-missing { padding: 16px; border: 1px dashed var(--warn); border-radius: 10px; color: var(--warn); }
@media (max-width: 1100px) { .hv-cols { grid-template-columns: minmax(0, 1fr); } .hv-side { position: static; max-height: none; } }
@media (max-width: 860px) {
  .hv { width: auto; left: 0; transform: none; }
  .hv-main, .hv-sum { grid-template-columns: minmax(0, 1fr); }
  .hv-listwrap { position: static; }
  .hv-list { max-height: 260px; }
  .hv-mbar { grid-template-columns: 110px minmax(0, 1fr) 70px; }
  .hv-why-list li { grid-template-columns: 100px 48px minmax(0, 1fr); }
}
</style>"""

HV_JS = r"""<script>
(function () {
  var root = document.getElementById('hv');
  if (!root) return;
  var D = window.HELD_DATA;
  if (!D) { root.innerHTML = '<p class="hv-missing">보류 데이터 파일(__FILE__)이 이 HTML 과 같은 폴더에 있어야 보입니다.</p>'; return; }
  var ORDER = ['gpt', 'aiona', 'claude', 'gemini'];
  var CAUSES = ['held', 'source_unavailable', 'judge_disagreement', 'unverified', 'incomplete', 'empty_output_hold', 'pending'];
  var TAG = { PASS: ['맞음', 'ok'], TRUE: ['맞음', 'ok'], FAIL: ['틀림', 'bad'], FALSE: ['틀림', 'bad'], UNVERIFIED: ['확인 못 함', 'unk'] };
  var CHOICE = { PASS: '통과', FAIL: '실패', UNVERIFIED: '확인 못 함', NOT_APPLICABLE: '해당 없음' };
  var items = D.items.slice().sort(function (a, b) { return a.q - b.q || ORDER.indexOf(a.model) - ORDER.indexOf(b.model); });
  var st = { model: 'all', cause: 'all', kind: 'all', q: '', sel: null, turn: null, raw: false };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function causeLabel(c) { return (D.causes[c] || {}).label || c; }

  /* 답변 마크다운 — 제목·굵게·목록·표·코드·인용만. 글은 먼저 이스케이프한다 */
  function inline(s) {
    s = s.replace(/\\([\\`*_{}\[\]()#+\-.!|>~])/g, function (m, c) { return '&#' + c.charCodeAt(0) + ';'; });
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, '$1<em>$2</em>');
    s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return s;
  }
  function cells(l) {
    l = l.trim();
    if (l.charAt(0) === '|') l = l.slice(1);
    if (l.charAt(l.length - 1) === '|') l = l.slice(0, -1);
    return l.split('|').map(function (c) { return inline(esc(c.trim())); });
  }
  var LI = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  function md(text) {
    var lines = String(text || '').replace(/\r\n?/g, '\n').split('\n'), out = [], i = 0;
    var sep = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
    while (i < lines.length) {
      var l = lines[i], m;
      if (/^\s*```/.test(l)) {
        var code = []; i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) { code.push(lines[i]); i++; }
        i++; out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>'); continue;
      }
      if (/^\s*$/.test(l)) { i++; continue; }
      if ((m = /^(#{1,6})\s+(.*)$/.exec(l))) { var lv = Math.min(6, m[1].length + 2); out.push('<h' + lv + '>' + inline(esc(m[2])) + '</h' + lv + '>'); i++; continue; }
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l)) { out.push('<hr>'); i++; continue; }
      if (/^\s*\|/.test(l) && i + 1 < lines.length && sep.test(lines[i + 1])) {
        var head = cells(l), body = []; i += 2;
        while (i < lines.length && /^\s*\|/.test(lines[i])) { body.push(cells(lines[i])); i++; }
        out.push('<div class="md-table"><table><thead><tr>' + head.map(function (c) { return '<th>' + c + '</th>'; }).join('') + '</tr></thead><tbody>'
          + body.map(function (r) { return '<tr>' + r.map(function (c) { return '<td>' + c + '</td>'; }).join('') + '</tr>'; }).join('') + '</tbody></table></div>');
        continue;
      }
      if (/^\s*>/.test(l)) {
        var q = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) { q.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
        out.push('<blockquote>' + md(q.join('\n')) + '</blockquote>'); continue;
      }
      if ((m = LI.exec(l))) {
        var ordered = /\d/.test(m[2]), lis = [], start = ordered ? parseInt(m[2], 10) : 1;
        while (i < lines.length) {
          var mm = LI.exec(lines[i]);
          if (!mm) {
            if (/^\s{2,}\S/.test(lines[i]) && lis.length) { lis[lis.length - 1].html += '<br>' + inline(esc(lines[i].trim())); i++; continue; }
            break;
          }
          if (/\d/.test(mm[2]) !== ordered && mm[1].length < 2) break;
          lis.push({ sub: mm[1].length >= 2, html: inline(esc(mm[3])) }); i++;
        }
        var tag = ordered ? 'ol' : 'ul';
        out.push('<' + tag + (ordered && start !== 1 ? ' start="' + start + '"' : '') + '>' + lis.map(function (x) { return '<li' + (x.sub ? ' class="sub"' : '') + '>' + x.html + '</li>'; }).join('') + '</' + tag + '>');
        continue;
      }
      var p = [];
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^\s*(```|#{1,6}\s|>)/.test(lines[i]) && !LI.test(lines[i])
             && !(/^\s*\|/.test(lines[i]) && i + 1 < lines.length && sep.test(lines[i + 1]))) { p.push(inline(esc(lines[i]))); i++; }
      if (p.length) out.push('<p>' + p.join('<br>') + '</p>'); else { out.push('<p>' + inline(esc(l)) + '</p>'); i++; }
    }
    return out.join('');
  }

  function filtered() {
    return items.filter(function (it) {
      return (st.model === 'all' || it.model === st.model) && (st.cause === 'all' || it.causes[st.cause])
        && (st.kind === 'all' || it.kind === st.kind) && (!st.q || String(it.q).indexOf(st.q) === 0);
    });
  }

  function summary() {
    var byModel = {}, byCause = {};
    items.forEach(function (it) { byModel[it.model] = (byModel[it.model] || 0) + 1; Object.keys(it.causes).forEach(function (c) { byCause[c] = (byCause[c] || 0) + 1; }); });
    var mx = Math.max.apply(null, ORDER.map(function (p) { return byModel[p] || 0; }));
    var left = '<div class="hv-panel"><h4>모델별 보류 <small>질문 ' + D.total_questions + '개 중 · 모두 ' + items.length + '건</small></h4>'
      + ORDER.map(function (p) {
        var n = byModel[p] || 0;
        return '<div class="hv-mbar"><span>' + esc(D.models[p]) + '</span><span class="track"><i style="width:' + (n / mx * 100).toFixed(0) + '%"></i></span><span class="num">' + n + ' / ' + D.total_questions + '</span></div>';
      }).join('') + '</div>';
    var right = '<div class="hv-panel"><h4>보류 사유 <small>그 사유가 하나라도 있는 질문 수 — 한 질문에 사유가 여럿일 수 있음</small></h4><ul class="hv-why-list">'
      + CAUSES.filter(function (c) { return byCause[c]; }).map(function (c) {
        return '<li><span class="hv-cause c-' + c + '">' + esc(causeLabel(c)) + '</span><span class="num">' + byCause[c] + '건</span><span>' + esc((D.causes[c] || {}).plain || '') + '</span></li>';
      }).join('') + '</ul></div>';
    document.getElementById('hv-sum').innerHTML = left + right;
  }

  function seg(name, label, opts) {
    return '<span><span class="hv-fl">' + label + '</span><span class="hv-seg">' + opts.map(function (o) {
      return '<button type="button" data-f="' + name + '" data-v="' + esc(o[0]) + '" aria-pressed="' + (st[name] === o[0]) + '">' + esc(o[1]) + '</button>';
    }).join('') + '</span></span>';
  }
  function filters() {
    var causes = CAUSES.filter(function (c) { return items.some(function (it) { return it.causes[c]; }); });
    document.getElementById('hv-filters').innerHTML =
      seg('model', '모델', [['all', '전체']].concat(ORDER.map(function (p) { return [p, D.models[p]]; })))
      + seg('cause', '사유', [['all', '전체']].concat(causes.map(function (c) { return [c, causeLabel(c)]; })))
      + seg('kind', '종류', [['all', '전체'], ['단일', '단일'], ['대화', '대화'], ['긴 문서', '긴 문서']])
      + '<label><span class="hv-fl">질문 번호</span><input class="hv-search" id="hv-q" inputmode="numeric" placeholder="예: 12" value="' + esc(st.q) + '"></label>';
  }

  function list() {
    var f = filtered();
    if (!f.some(function (it) { return it.id === st.sel; })) { st.sel = f.length ? f[0].id : null; st.turn = null; }
    document.getElementById('hv-count').textContent = '보류 ' + items.length + '건 중 ' + f.length + '건';
    document.getElementById('hv-list').innerHTML = f.map(function (it) {
      var why = Object.keys(it.causes).map(causeLabel).join(' · ');
      return '<li><button type="button" class="hv-item" role="option" data-id="' + it.id + '" aria-selected="' + (it.id === st.sel) + '">'
        + '<span class="n">Q' + it.q + '</span><span class="m">' + esc(D.models[it.model]) + '</span>'
        + '<span class="t">' + esc(it.kind) + (it.turns_total > 1 ? ' ' + it.turns_total + '턴' : '') + ' · ' + esc(why) + '</span></button></li>';
    }).join('') || '<li class="hv-none" style="padding:8px">조건에 맞는 보류가 없습니다.</li>';
    detail();
  }

  function holdCards(holds) {
    if (!holds.length) return '';
    var groups = {};
    holds.forEach(function (h) { (groups[h.cause] = groups[h.cause] || []).push(h); });
    return CAUSES.filter(function (c) { return groups[c]; }).map(function (c) {
      return '<section class="hv-grp"><div class="hv-grp-h"><b>' + groups[c].length + '개 항목</b><span class="hv-cause c-' + c + '">' + esc(causeLabel(c)) + '</span></div>'
        + '<p class="hv-plain">' + esc((D.causes[c] || {}).plain || '') + '</p>'
        + groups[c].map(function (h) {
          var claims = h.claims || [], unk = claims.filter(function (x) { return x.tag !== 'PASS' && x.tag !== 'TRUE'; }).length;
          return '<div class="hv-it"><div class="hv-it-h"><b>' + esc(h.criterion ? h.criterion + ' ' : '') + esc(h.criterion_name) + '</b>'
            + (h.criterion_q ? '<span>' + esc(h.criterion_q) + '</span>' : '') + '</div>'
            + (h.reason ? '<div class="hv-jr"><span>심판이 남긴 글</span><p>' + esc(h.reason) + '</p></div>' : '')
            + (h.original_reason ? '<div class="hv-jr"><span>심판의 처음 판단' + (h.original_choice ? ' — ' + esc(CHOICE[h.original_choice] || h.original_choice) : '') + '</span><p>' + esc(h.original_reason) + '</p></div>' : '')
            + (claims.length ? '<details class="hv-claims"' + (claims.length <= 6 ? ' open' : '') + '><summary>심판이 확인한 문장 ' + claims.length + '개 · 확인 못 함 ' + unk + '개</summary><ul>'
              + claims.map(function (x) { var t = TAG[x.tag] || [x.tag, 'unk']; return '<li><span class="tg ' + t[1] + '">' + esc(t[0]) + '</span><span>' + inline(esc(x.text)) + '</span></li>'; }).join('')
              + '</ul></details>' : '')
            + ((h.failed_sources || []).length ? '<div class="hv-src"><span class="hv-fl">확인하지 못한 출처</span><ul>' + h.failed_sources.map(function (s) {
              return '<li><em>' + esc(s.status) + '</em>' + (/^https?:/.test(s.url) ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer">' + esc(s.url) + '</a>' : esc(s.url)) + '</li>';
            }).join('') + '</ul></div>' : '')
            + '</div>';
        }).join('') + '</section>';
    }).join('');
  }

  function detail() {
    var box = document.getElementById('hv-detail');
    var it = items.filter(function (x) { return x.id === st.sel; })[0];
    if (!it) { box.innerHTML = '<p class="hv-none">왼쪽에서 질문을 고르세요.</p>'; return; }
    if (st.turn == null || st.turn > it.turns_total) st.turn = it.held_turns.length ? it.held_turns[0] : 1;
    var t = it.turns[st.turn - 1];
    var tabs = it.turns_total > 1 ? '<div class="hv-turns"><span class="hv-fl">턴</span>' + it.turns.map(function (x) {
      return '<button type="button" data-turn="' + x.n + '" class="' + (x.holds.length ? 'held' : '') + '" aria-pressed="' + (x.n === st.turn) + '"'
        + (x.holds.length ? ' title="보류 항목 ' + x.holds.length + '개"' : '') + '>' + x.n + '턴</button>';
    }).join('') + '<span class="hv-fl">· 점 = 보류 항목이 있는 턴</span></div>' : '';
    var att = (t.attachments || []).map(function (a) {
      return '<details class="hv-extra"><summary>첨부파일 — ' + esc(a.name) + ' · ' + esc(a.type) + '</summary><pre>' + esc(a.content || '(내용 없음)') + '</pre></details>';
    }).join('');
    var ref = t.ref_chars ? '<details class="hv-extra"><summary>참조 자료(긴 문서) ' + t.ref_chars.toLocaleString() + '자 — 앞부분만</summary><pre>' + esc(t.ref_head) + '\n…</pre></details>' : '';
    var ans = t.answer ? (st.raw ? '<div class="hv-text plain">' + esc(t.answer) + '</div>' : '<div class="hv-text md">' + md(t.answer) + '</div>')
      : '<div class="hv-text"><span class="hv-empty">답변 없음 (0자)' + (t.collected && t.collected !== 'SUCCESS' ? ' — 수집 상태 ' + esc(t.collected) : '') + '</span></div>';
    var side = t.holds.length ? holdCards(t.holds)
      : '<section class="hv-grp"><p class="hv-none">이 턴에는 보류 항목이 없습니다. 보류 항목이 있는 턴: ' + it.held_turns.map(function (n) { return n + '턴'; }).join(' · ') + '</p></section>';
    box.innerHTML = '<div class="hv-dh"><h3>Q' + it.q + ' · ' + esc(D.models[it.model]) + '<span>' + esc(it.kind) + (it.turns_total > 1 ? ' · ' + it.turns_total + '턴' : '') + '</span></h3><span class="hv-badge">질문 판정: 보류</span></div>'
      + '<div class="hv-why"><b>왜 보류인가</b><p>' + esc(it.headline) + '</p></div>' + tabs
      + '<div class="hv-cols"><div class="hv-qa">'
      + '<section class="hv-box hv-qbox"><h5>질문 원문 <small>' + (it.turns_total > 1 ? st.turn + '턴 · ' : '') + (t.question || '').length.toLocaleString() + '자</small></h5><div class="hv-text plain">' + esc(t.question) + '</div>' + att + ref + '</section>'
      + '<section class="hv-box hv-abox"><h5>답변 원문 <small>' + (t.answer || '').length.toLocaleString() + '자</small><button type="button" class="hv-raw" data-raw="1">' + (st.raw ? '읽기 좋게 보기' : '원문 글자 그대로 보기') + '</button></h5>' + ans + '</section>'
      + '</div><aside class="hv-side" aria-label="보류 사유"><h4 style="margin:0;font-size:.95rem">보류 사유 <small style="color:var(--muted);font-weight:400">' + (it.turns_total > 1 ? st.turn + '턴 · ' : '') + t.holds.length + '개 항목</small></h4>' + side + '</aside></div>';
  }

  root.addEventListener('click', function (e) {
    var b = e.target.closest('button');
    if (!b || !root.contains(b)) return;
    if (b.dataset.f) { st[b.dataset.f] = b.dataset.v; filters(); list(); return; }
    if (b.dataset.id) { st.sel = b.dataset.id; st.turn = null; list(); return; }
    if (b.dataset.turn) { st.turn = parseInt(b.dataset.turn, 10); detail(); return; }
    if (b.dataset.raw) { st.raw = !st.raw; detail(); }
  });
  root.addEventListener('input', function (e) {
    if (e.target.id === 'hv-q') { st.q = e.target.value.replace(/\D/g, ''); list(); }
  });
  document.getElementById('hv-list').addEventListener('keydown', function (e) {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    var f = filtered(), i = f.map(function (x) { return x.id; }).indexOf(st.sel);
    var j = Math.max(0, Math.min(f.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)));
    if (f[j]) { st.sel = f[j].id; st.turn = null; list(); var el = document.querySelector('.hv-item[aria-selected="true"]'); if (el) el.scrollIntoView({ block: 'nearest' }); }
    e.preventDefault(); e.stopPropagation();
  });
  summary(); filters(); list();
})();
</script>""".replace("__FILE__", HELD_JS_FILE)


# ---------- 장 ----------
SECTIONS = [
    ("s1", "전체 흐름도", "질문 → 답변 수집 → JSON → 평가", "전체 흐름도",
     "질문 세트를 네 모델에 던져 답을 받고, 질문·답변을 JSON 으로 저장한 뒤 코드(CODE 평가)와 심판 AI 둘(의미 평가)이 채점해 질문마다 PASS·FAIL·보류를 냅니다.",
     f"""
  <figure class="fig poster">
    <span class="poster-sub">2026-10-20 저장소 기준 · E 의미 평가는 판정 v4 (시험 중)</span>
    <div class="poster-grid">{flow}<aside class="pside" aria-label="흐름도 요약">{side_cards}</aside></div>
  </figure>
  {steps_table}
"""),
    ("s2", "바뀐 점", "답변 수집 방법 · 기존 → 현재", "바뀐 점 — 기존 → 현재",
     "10-19 실행(정밀 326)과 지금(10-20)을 비교했습니다. 답변 수집 방법이 바뀌었습니다 — API 대신 사람이 쓰는 앱·웹 화면에서 받습니다.",
     f"""{change_list}
  <p class="callout">참고 — 10-19 발표에는 '답변 수집 — 모두 웹브라우저'로 적었지만, 실제로는 GPT·Claude·Gemini 답을 API 로 받았습니다(487턴 모두). 지금은 네 모델 모두 화면에서 받습니다.</p>
  <p class="basis">출처: 10-19 실행 기록(참고자료/결과/8c61f117e661_정밀326_2026-10-19.json) · 2026-10-20 코드와 간단 78 실행 ac6507c00276</p>
"""),
    ("s3", "평가 프롬프트", "고정 프롬프트 v4 + 질문·답변 JSON", "평가 프롬프트 — 고정 프롬프트 v4 + 질문·답변 JSON",
     "심판에게는 모든 질문에 같은 프롬프트와, 수집한 질문·답변 JSON 만 보냅니다. 어떤 항목을 볼지는 프롬프트의 상황표를 보고 심판이 정하고, 결론까지 냅니다.",
     f"""
  <div class="block"><h3>심판에게 가는 글</h3>{pstack}</div>
  {parts_table}
  {v3v4_table}
  {check_table}
  {trial_table}
  {trial_points}
  {details(f"Q{D4['qid']} 입력 JSON 보기 (10-19 AIONA 답)", D4['excerpt'], "심판이 받는 [입력] 그대로")}
  <p class="basis">이 장의 숫자는 보고서를 만들 때마다 판정과 같은 코드(tools/judge_v4.question_json)로 다시 계산합니다 — AI 호출 없음. 프롬프트: 참고자료/프롬프트/judge_prompt_v4.txt (tools/build_judge_prompt_v4.py 가 v3 에서 만듦).</p>
"""),
    ("s4", "질문 수별 비율", f"{R198['n']}·{R155['n']}개 목록 vs 정밀 326", f"질문 수별 통과 비율 — {R198['n']}·{R155['n']}개 목록은 326 과 같은가",
     "기준마다 20번 평가되게 고른 두 목록이 정밀 326 과 같은 결과를 내는지, 10-19 실행 판정을 그대로 다시 세어 비교했습니다.",
     f"""
  <figure class="fig rate-chart">
    <ul class="rate-key"><li><i class="kref"></i>정밀 326</li><li><i class="k198"></i>{R198['n']}개 · 기준 많은 순</li>
      <li><i class="k155"></i>{R155['n']}개 · 모자란 기준부터</li><li><i class="kband"></i>같은 수를 무작위로 뽑았을 때 95% 범위</li></ul>
    {rate_chart()}
  </figure>
  {rate_points}
  {rate_table}
  {count_table}
  <p class="basis">출처: 참고자료/최소평가/통과비율_목록별_8c61f117e661.json (tools/subset_pass_rates.py — 326 판정·목록 크기를 10-19 기록과 맞춰 봄, 무작위 {R198['random']['draws']:,}번).</p>
"""),
    ("s5", "던지는 순서", "번호순 · 기준 많은 순 · 모자란 기준부터", "던지는 순서 세 가지 — 한 그래프로",
     "같은 326개 질문을 세 가지 순서로 하나씩 더해 갈 때, 기준과 FAIL 이 얼마나 빨리 채워지는지 비교했습니다.",
     f"""
  <figure class="fig rate-chart">
    <ul class="rate-key"><li><i class="knum"></i>번호순</li><li><i class="kkd"></i>기준 많은 순</li><li><i class="kgr"></i>모자란 기준부터</li>
      <li>● 점 = 도달 지점 (질문 수)</li></ul>
    <div class="order-charts">
      <div><h3>10번 이상 평가된 기준 수 (/{OT['criteria']})</h3>{order_chart("covered10", OT["criteria"], False, "criteria_all", "던진 질문 수별 10번 이상 평가된 기준 수 — 세 순서")}</div>
      <div><h3>보인 FAIL 종류 (모델 × 기준, /{OT['pairs']})</h3>{order_chart("fail_pairs", OT["pairs"], True, "pairs_80", "던진 질문 수별 보인 FAIL 종류 비율 — 세 순서")}</div>
    </div>
  </figure>
  {order_points}
  {reach_table}
  {cover_table}
  <p class="basis">출처: 참고자료/최소평가/던지는순서_세가지_8c61f117e661.json (tools/order_curves.py — FAIL 종류 {OT['pairs']}개·사건 {OT['events']}건이 10-19 기록과 같음). 그래프에 마우스를 올리면 그 질문 수의 값이 보입니다.</p>
"""),
    ("s6", "평가 횟수 k", "k 10·20 과 질문 수 · 세 선은 어디서 만나나", "평가 횟수 k 와 질문 수 — 세 선은 어디서 만나나",
     "모든 기준이 최소 k번 평가되려면 질문을 몇 개 던져야 하는지, 번호순·기준 많은 순·모자란 기준부터 세 순서로 비교했습니다. 미팅에서 나온 '세 선이 어느 지점에서 만나지 않을까'를 확인했습니다.",
     f"""
  <figure class="fig rate-chart">
    <ul class="rate-key"><li><i class="knum"></i>번호순</li><li><i class="kkd"></i>기준 많은 순</li><li><i class="kgr"></i>모자란 기준부터</li>
      <li>● 점 = k 에 닿는 질문 수</li></ul>
    <div class="order-charts">
      <div><h3>모든 기준 {KR['criteria']}개가 최소 k번</h3>{k_chart("raw", (10,), "던진 질문 수별 모든 기준의 최소 평가 횟수 — 세 순서")}</div>
      <div><h3>기준마다 k번 — 326 에 k개보다 적은 기준은 있는 만큼</h3>{k_chart("cap", (10, 20), "던진 질문 수별 기준마다 k번(있는 만큼) — 세 순서")}</div>
    </div>
  </figure>
  {k_points}
  {k_reach_table}
  {k_rare_table}
  <p class="basis">출처: 참고자료/최소평가/평가횟수k_곡선_326.json (tools/k_reach_curves.py — 질문 세트만 읽음, 기준은 05장과 같은 '질문으로 셀 수 있는 기준 {KR['criteria']}개'). 모자란 기준부터 = 그때까지 가장 적게 평가된 기준을 채우는 질문부터. 그래프에 마우스를 올리면 그 질문 수의 k 가 보입니다.</p>
"""),
    ("s7", "걸린 시간", "답변 수집 · CODE · 의미 평가", "걸린 시간 — 답변 수집과 평가",
     "간단 78 을 네 모델에서 받는 데 걸린 시간(10-20)과, CODE·의미 평가에 걸린 시간을 기록으로 셌습니다. 질문 하나 = 그 질문의 모든 턴을 합친 시간입니다.",
     f"""{time_kpis}
  {collect_table(f"답변 수집 — 간단 78 (10-20, 앱·웹) · 단일 {TIMES['kinds_78']['단일']} · 대화 {TIMES['kinds_78']['대화']} · 긴 문서 {TIMES['kinds_78']['긴 문서']}개", TIMES['collect_1020'], 78)}
  {time_points}
  {judge_table}
  {collect_table("비교 — 정밀 326 (10-19, GPT·Claude·Gemini 는 API · AIONA 는 웹)", TIMES['collect_1019'], 326)}
  <p class="basis">출처: 참고자료/결과/시간_요약_ac6507c00276.json (tools/time_summary.py — 수집은 실행 기록의 턴마다 걸린 시간, CODE 는 78개 답으로 직접 잼, 의미 평가는 판정 기록). 답을 기다린 시간만 셈 — 새 채팅 열기·모델 설정·첨부 붙여넣기 시간은 빠짐.</p>
"""),
    ("s8", "보류 함께 보기", "10-19 정밀 326 에서 보류된 300건", "보류 함께 보기 — 왜 보류로 두었나",
     "10-19 정밀 326 실행에서 질문 전체가 보류로 끝난 300건(네 모델)입니다. 질문·답변 원문과 함께, 왜 보류인지 쉬운 말로 붙였습니다. 보류 = 실패로 확정된 항목은 없지만 확인이 끝나지 않은 항목이 남아 사람이 정할 것.",
     held_section),
]

total = len(SECTIONS)


def head(no, title, lead):
    return f'<div class="s-head"><span class="s-no">{no} / {total:02d}</span><h2>{title}</h2>{f"<p>{lead}</p>" if lead else ""}</div>'


glossary = table("", ["말", "뜻"], [
    ["질문 세트", "모델에 똑같이 던지는 고정 질문 326개 (간단평가는 그중 78개). 대화형 질문은 턴이 여러 개여도 1개로 셈"],
    ["CODE 평가", "프로그램이 형식·글자 수·계산 같은 28가지를 검사"],
    ["의미 평가", "심판 AI 가 내용을 44개 항목으로 판정 — J1~J8 묶음"],
    ["J1~J8", "요청 충실 · 지시 준수 · 일관성·논리 · 완결성 · DATA 신뢰도 · 대화 맥락 유지 · 긴 문서 활용 · 첨부 활용"],
    ["심판", "Codex CLI(ChatGPT 구독) — 상황을 찾아 J1~J8 판정과 결론 · Jev(외부 API) — J1~J4 를 함께 판정"],
    ["PASS · FAIL · 보류", f"{PASS} 통과 · {FAIL} 실패 · {HOLD} 실패로 확정된 것은 없지만 확인이 끝나지 않아 사람이 정함"],
    ["k", "06장 — 모든 기준이 최소 몇 번 평가되었나"],
], [22, 78])

s0 = f"""
<section class="slide cover" id="s0">
  <div class="cover-top">
    <span class="date">2026-10-20 · 10-19 오후 미팅 후속 보고</span>
    <h1>ModelGrade 보고</h1>
    <p>AI 모델 4개(AIONA · GPT · Claude · Gemini)에 같은 질문을 던지고, 답을 자동으로 채점해 비교하는 도구입니다.</p>
  </div>
  <span class="kicker">목차</span>
  <ol class="agenda">
    {"".join(f'<li><a href="#{sid}"><span class="n">{i:02d}</span><strong>{E(short)}</strong><span class="d">{E(desc)}</span></a></li>' for i, (sid, short, desc, *_rest) in enumerate(SECTIONS, 1))}
  </ol>
  <span class="kicker">먼저 알아 둘 말</span>
  {glossary}
</section>
"""

sections = "".join(
    f'\n<section class="slide" id="{sid}">\n  {head(f"{i:02d}", E(title), E(lead))}\n{body}</section>\n'
    for i, (sid, _short, _desc, title, lead, body) in enumerate(SECTIONS, 1))

nav = f"""
<nav class="topnav" aria-label="목차">
  <div class="topnav-in">
    <span class="brand">ModelGrade 보고 10-20</span>
    <div class="toc-links" id="toc-links">
      <a href="#s0"><span class="n">00</span>목차</a>{"".join(f'<a href="#{sid}"><span class="n">{i:02d}</span>{E(short)}</a>' for i, (sid, short, *_rest) in enumerate(SECTIONS, 1))}
    </div>
    <div class="pager"><button type="button" id="prev" aria-label="이전 장">←</button><button type="button" id="next" aria-label="다음 장">→</button></div>
  </div>
</nav>
"""

ids = ", ".join(f"'{sid}'" for sid in ["s0", *[s[0] for s in SECTIONS]])
script = re.sub(r"var ids = \[[^\]]*\];", f"var ids = [{ids}];", nav_script, count=1)

page = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>ModelGrade 보고 10-20</title>
<meta name="description" content="2026-10-20 — 전체 흐름도 · 바뀐 점(답변 수집 방법) · 평가 프롬프트 v4 · 질문 수별 통과 비율 · 던지는 순서 · 평가 횟수 k · 걸린 시간 · 보류 함께 보기">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+KR:wght@400;500;600;700&display=swap">
{css}
{extra_css}
{EXTRA_CSS2}
</head>
<body>
{nav}
<main class="wrap">
{s0}
{sections}
</main>
{script}
<script src="{HELD_JS_FILE}"></script>
{HV_JS}
</body>
</html>
"""
OUT.write_text(page, encoding="utf-8")
print("저장:", OUT, f"{len(page):,}자")
for w in WARN:
    print("글 넘침 경고:", w)
