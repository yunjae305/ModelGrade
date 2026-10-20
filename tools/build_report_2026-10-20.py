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
y += A_h
P.append(arrow(CC, y, CC, y + 24))
y += 26

B_y = y
parts, B_h, _ = module(y, "B", "질문 해석 — 코드, AI 없음", [
    ("기준 찾기", ["턴마다 채점할 항목과 근거 문장", "글자 수·형식 같은 CODE 조건"]),
    ("결과", ["판정상황표 — 답과 무관, 미리 고정"]),
], "code")
P += parts
y += B_h
f1_y = A_y + 24
parts, f1_h = filenode(f1_y, "질문 세트 파일", ["질문 326 · 간단 78", "판정상황표"])
P += parts
P += [file_read(f1_y + 22), file_write(B_y + 30, f1_y + f1_h - 14)]
P.append(arrow(CC, y, CC, y + 24))
y += 26

C_y = y
parts, C_h, spots = module(y, "C", "답변 수집 — 네 모델 동시", [
    ("앱", ["ChatGPT · Claude 데스크톱 앱"]),
    ("웹", ["Gemini · AIONA(개발 서버)"]),
    ("방법", ["화면을 자동 조작해 질문 → 복사 버튼 원문", "질문마다 저장 · 받은 질문은 건너뜀"]),
], "ai")
P += parts
parts, _ = aibox(C_y, "AI · 답하는 모델 4개", ["GPT-6 · Medium", "Claude Sonnet 5.5 · 중간", "Gemini 3.6 Flash · 보통",
                                             "AIONA", "로그인은 사람이 미리"])
P += parts
P.append(ai_link(spots[0][0] + spots[0][1] / 2))
y += C_h

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

D_y = y
parts, D_h, _ = module(y, "D", "CODE 평가 — 28가지 검사, 코드", [
    ("검사", ["형식·글자 수·계산 등 — 수집 직후 바로"]),
    ("결과", ["검사마다 PASS·FAIL 과 이유"]),
], "code")
P += parts
y += D_h
f2_y = C_y + 70
parts, f2_h = filenode(f2_y, "실행 기록", ["답변 원문", "CODE 결과"])
P += parts
P += [file_write(f2_y + 22), file_write(D_y + 30, f2_y + f2_h - 14)]

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

# E — 판정 입력 → Jev → (넘길 항목?) → Codex
E_y = y
inner = []
yb = E_y + 40
parts, h1 = block(yb, "입력", ["고정 프롬프트 + 질문·첨부·답변", "어느 모델 답인지는 숨김"])
inner += parts
yb += h1 + 6
jev_y = yb
parts, h2 = block(yb, "Jev", ["J1~J4 를 먼저 판정 (0.5~2초)", "FAIL·애매한 항목만 Codex 로"])
inner += parts
yb += h2
inner.append(arrow(CC, yb, CC, yb + 14))
yb += 14
dcy = yb + 30
inner += diamond(CC, dcy, 180, 60, ["Jev 가 FAIL", "또는 애매?"])
inner += [lab(CX0 + 20, dcy - 4, "예: 그 항목도"), lab(CX0 + 20, dcy + 13, "Codex 로"),
          lab(CC + 96, dcy - 4, "아니오: Codex 는"), lab(CC + 96, dcy + 13, "J5~J8 만")]
yb = dcy + 30
inner.append(arrow(CC, yb, CC, yb + 14))
yb += 16
codex_y = yb
parts, h3 = block(yb, "Codex", ["J5~J8 + 넘겨받은 항목을 확정", "이유·인용 첨부 · 사실은 웹검색"])
inner += parts
yb += h3 + 6
E_h = yb - E_y + 4
P += frame(E_y, E_h, "E", "의미 평가 — 심판 둘", "ai") + inner
parts, _ = aibox(jev_y - 4, "AI · 심판 1 — Jev", ["외부 API (TypeSafe)", "J1~J4 만"])
P += parts
parts, _ = aibox(codex_y - 4, "AI · 심판 2 — Codex CLI", ["GPT-6 Luna · 추론 low", "ChatGPT Pro 구독"])
P += parts
P += [ai_link(jev_y + h2 / 2), ai_link(codex_y + h3 / 2)]
parts, f3_h = filenode(E_y + 10, "고정 프롬프트", ["판정 프롬프트 v3", "Jev 질문 묶음"])
P += parts
P.append(file_read(E_y + 40))
f4_y = E_y + 22 + f3_h
parts, f4_h = filenode(f4_y, "판정 입력", ["질문마다 한 줄"])
P += parts
P.append(file_write(f4_y + 30))
y = E_y + E_h
P.append(arrow(CC, y, CC, y + 24))
y += 26

F_y = y
parts, F_h, _ = module(y, "F", "재판정·보류 — 규칙, 코드", [
    ("확정", ["Codex FAIL → FAIL", "Jev 애매 + Codex PASS → PASS"]),
    ("보류", ["Jev FAIL + Codex PASS (심판 갈림)", "확인 못 함 · 인용이 원문에 없음"]),
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
        ["A·B·D·F·G·H", "코드 (정해진 규칙)"],
        ["C 답변 수집", "답하는 모델 4개 — 앱·웹 자동 조작"],
        ["E 의미 평가", "심판 2개 — Jev(API) · Codex(구독)"],
        ["보류 정리", "사람"],
    ], "ai")
    + mini("판정 규칙", ["상황", "결과"], [
        ["답을 못 받음", FAIL],
        ["답 복사 실패", HOLD],
        ["빈 답 요구 질문", f"빈 답 {HOLD} · 글 {FAIL}"],
        ["Codex FAIL", FAIL],
        ["Jev FAIL + Codex PASS", HOLD],
        ["확인 못 함 · 인용 불일치", HOLD],
    ])
    + legend)

steps_table = table("단계별 요약", ["단계", "하는 일", "누가", "파일"], [
    [step("A", "질문 세트"), "고정된 질문 326개와 그중 78개. 고칠 때만 버전을 올림.", "고정 파일",
     files("questions.json", "질문세트_최소78/ids.json")],
    [step("B", "질문 해석"), "질문 글에서 채점할 항목·근거를 찾아 표로 고정.", "코드", files("build_judge_situations.py", "판정상황표_v2.json")],
    [step("C", "답변 수집"), "네 모델에 동시에 묻고 복사 버튼으로 원문을 받음. 질문마다 저장.", "앱·웹 자동 조작",
     files("modelgrade.py collect", "uia_chat_app.py", "cdp_chat_web.py")],
    [step("D", "CODE 평가"), "형식·글자 수·계산 등 28가지를 프로그램이 검사.", "코드", files("code_eval/engine.py")],
    [step("E", "의미 평가"), "Jev 가 J1~J4 를 먼저 보고, Codex 가 J5~J8 과 넘겨받은 항목을 이유·인용과 함께 확정.",
     "Jev(API) · Codex(구독)", files("judge_run.py", "judge_prompt_v3.txt")],
    [step("F", "재판정·보류"), "다시 묻지 않고 규칙으로 확정하거나 보류.", "코드", files("judge_split.py")],
    [step("G", "질문 단위 판정"), f"{FAIL} 하나라도 → {FAIL}, 아니면 보류 하나라도 → {HOLD}, 나머지 {PASS}.", "코드", files("judge_run.py")],
    [step("H", "결과·보류 추출"), "모델별 결과 · 질문별 이유 · 보류 목록을 명령 하나로.", "코드",
     files("modelgrade.py report", "export_results_json.py")],
], [13, 45, 18, 24])

# ---------- 02 10-19 → 지금 (체크리스트 t2) ----------
# 10-19 = 정밀 326 실행 8c61f117e661 의 기록(참고자료/결과/8c61f117e661_정밀326_2026-10-19.json — 턴마다 수집 방법·의미 판정),
#         10-19 발표, 변경내역 dual-judge-v1. 지금 = 10-20 저장소 코드와 간단 78 실행 ac6507c00276 의 기록.
# (단계, 이름, 10-19 띠(종류, 짧은 글), 지금 띠(종류, 짧은 글), 10-19 방식, 지금 방식, 바뀐 점(종류, 글))
SHIFT = [
    ("0", "질문 생성", ("api", "GPT-6 Luna API"), ("none", "안 함"),
     "GPT-6 Luna(API)로 생성 · k 맞춤", "안 함 — 세트 고정", ("none", "안 함")),
    ("A", "질문 세트", ("code", "326 v2.0"), ("code", "326 v2.2"),
     "326개(v2.0) · 간단 78", "같음 · 문서형 53개만 '본문 글로' 문구 추가(v2.2)", ("same", "거의 그대로")),
    ("B", "질문 해석", ("code", "평가 때 계산"), ("code", "미리 고정"),
     "평가할 때마다 코드가 계산", "판정상황표로 미리 고정", ("chg", "고정 파일로")),
    ("C", "답변 수집", ("api", "API 3 · 웹 1"), ("sub", "앱 2 · 웹 2"),
     "GPT·Claude·Gemini: API<br>AIONA: 웹(운영 서버)", "ChatGPT·Claude: 데스크톱 앱<br>Gemini·AIONA: 웹(개발 서버)", ("sub", "API → 앱·웹")),
    ("D", "CODE 평가", ("code", "28가지"), ("code", "28가지"),
     "28가지 검사", "같음", ("same", "그대로")),
    ("E", "의미 평가", ("api", "Luna·Jev API"), ("part", "Jev API · Codex 구독"),
     "Luna(API) J1~J8 + Jev(API) J1~J4 함께<br>턴마다 판정", "Jev(API) J1~J4 먼저 → Codex(구독) 확정<br>질문마다 판정", ("sub", "API → 구독")),
    ("F", "재판정·보류", ("api", "Luna 재호출"), ("code", "규칙만"),
     "갈리면 Luna 다시 호출 → 또 갈리면 보류", "다시 부르지 않고 규칙으로", ("chg", "재호출 없음")),
    ("G", "질문 단위 판정", ("code", "턴 → 질문"), ("code", "질문 단위"),
     "턴 판정을 모아 질문 1개로", "처음부터 질문 단위 (셈은 같음)", ("same", "셈은 그대로")),
    ("H", "결과·보류 추출", ("code", "화면·스크립트"), ("code", "명령 하나"),
     "웹 화면 + 스크립트 따로", "report 명령 하나", ("chg", "명령 하나로")),
    ("·", "실행 방식", None, None,
     "웹 화면(서버)", "파이썬 명령 · 20개씩 · 멈춰도 이어서", ("chg", "화면 → 명령")),
]


def chip(key, name, spec):
    kind, label = spec
    return f'<li class="sh-chip {kind}"><b>{E(key)} {E(name)}</b><small>{E(label)}</small></li>'


shift_strip = f"""
  <div class="shift" role="group" aria-label="단계별 10-19 방식과 지금 방식">
    <div class="shift-row"><span class="shift-k">10-19</span><ol class="sh-steps">{"".join(chip(k, n, b) for k, n, b, *_r in SHIFT if b)}</ol></div>
    <div class="shift-row"><span class="shift-k">지금</span><ol class="sh-steps">{"".join(chip(k, n, a) for k, n, _b, a, *_r in SHIFT if a)}</ol></div>
    <ul class="shift-key"><li><i class="api"></i>API 호출</li><li><i class="part"></i>일부 API</li><li><i class="sub"></i>앱·웹·구독 (사람 계정)</li>
      <li><i class="code"></i>코드</li><li><i class="none"></i>안 함</li></ul>
  </div>"""

glance_table = table("한눈에", ["항목", "10-19", "지금"], [
    ["API 를 쓰는 곳", "질문 생성 · 답변 수집(3모델) · 심판 2개", "<b>Jev 1개</b> (대체는 다음 과제)"],
    ["답변 수집", "API 3 · 웹 1", "앱 2 · 웹 2"],
    ["판정 단위", "턴마다 (모델당 487번)", "질문마다 (모델당 326번)"],
    ["판정 프롬프트", "질문마다 코드가 조립", "고정 파일 1개 (03장)"],
    ["실행", "웹 화면(서버)", "파이썬 명령 하나"],
], [22, 39, 39])

shift_table = table("단계별", ["단계", "10-19", "지금", "바뀐 점"], [
    [step(k, n), before, after, f'<span class="ch-tag t-{tk}">{E(tl)}</span>'] for k, n, _b, _a, before, after, (tk, tl) in SHIFT
], [16, 34, 34, 16])

# ---------- 03 판정 프롬프트는 고정 (체크리스트 t7) ----------
# 판정 때 쓰는 함수(tools/judge_inputs.py · judge_split.py)로 Q36 입력을 다시 만든다 — AI 호출 없음, 빌드 때마다 다시 계산.
RESULT_1019 = ROOT / "참고자료" / "결과" / "8c61f117e661_정밀326_2026-10-19.json"
EXAMPLE_Q36 = ROOT / "참고자료" / "프롬프트" / "예시_판정입력_Q36_AIONA_v3.txt"   # 10-19 09:23 다른 PC 에서 만든 예시 (실행 표식 224fae)
PROMPT_DOC = ROOT / "참고자료" / "프롬프트" / "판정_프롬프트_v3.md"
JEV_BUNDLE = ROOT / "참고자료" / "프롬프트" / "jev_questions_v1.json"


def sha16(text_):
    return hashlib.sha256(text_.encode("utf-8")).hexdigest()[:16]


def changed_lines(a, b):
    return [ln for ln in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=0)
            if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]


def prompt_demo(qid=36, mark="224fae", other_mark="7c19e2", flag="J2-3.a"):
    prompt = ji.PROMPT.read_text(encoding="utf-8")
    situ_path = ji.DEFAULT_SET / ji.SITUATIONS
    table_ = {r["case_id"]: r for r in json.loads(situ_path.read_text(encoding="utf-8"))["rows"]}
    rows = sorted((r for r in flatten(json.loads((ji.DEFAULT_SET / "questions.json").read_text(encoding="utf-8")))
                   if ji._question(r["case_id"]) == qid), key=ji._turn)
    if len(rows) != 1:
        raise SystemExit(f"Q{qid} 는 단일 턴 질문이어야 합니다 (턴 {len(rows)}개)")
    cid = rows[0]["case_id"]
    run = json.loads(RESULT_1019.read_text(encoding="utf-8"))

    def answers(provider):
        return {t["case_id"]: t["answer"] for t in run["providers"][provider]["turns"] if t["question"] == qid}

    situations = {cid: table_[cid]["case_situations"]}
    first = ji.question_input(mark, rows, situations, answers("aiona"))
    second = ji.question_input(mark, rows, situations, answers("aiona"))
    if other_mark in first:
        raise SystemExit(f"다른 표식 {other_mark} 이 입력 안에 있습니다 — 다른 표식으로 바꾸세요")
    other = ji.question_input(other_mark, rows, situations, answers("aiona"))
    gpt_in = ji.question_input(mark, rows, situations, answers("gpt"))
    a_lines, g_lines = first.splitlines(), gpt_in.splitlines()
    same_head = next((i for i, (x, y_) in enumerate(zip(a_lines, g_lines)) if x != y_), min(len(a_lines), len(g_lines)))
    answer_at = a_lines.index("[답변]") + 1
    plan = js.split(table_[cid]["items"])
    flag = flag if flag in plan["jev"] else plan["jev"][0]

    def codex_text(extra):   # judge_run.run_gpt 와 같은 모양 — 고정 프롬프트 + "[입력]" + 그 심판이 맡은 항목만 남긴 입력
        situ = {cid: ji.situations_for(table_[cid], plan["gpt"] + extra)}
        return prompt + "\n[입력]\n" + ji.question_input(mark, rows, situ, answers("aiona"))

    c0, c0b, c1 = codex_text([]), codex_text([]), codex_text([flag])
    doc = re.search(r"sha256 `([0-9a-f]{16})`", PROMPT_DOC.read_text(encoding="utf-8"))
    example = EXAMPLE_Q36.read_text(encoding="utf-8")
    return {
        "qid": qid, "mark": mark, "flag": flag,
        "prompt_sha": sha16(prompt), "prompt_chars": len(prompt), "doc_sha": doc.group(1) if doc else None,
        "first_sha": sha16(first), "second_sha": sha16(second), "input_chars": len(first),
        "example_same": example.rstrip("\n") == first,
        "mark_changed": len(changed_lines(first, other)) // 2,
        "mark_only": all(mark in ln or other_mark in ln for ln in changed_lines(first, other)),
        "same_head": same_head, "answer_at": answer_at,
        "no_model_names": not any(n in first or n in gpt_in for n in ("AIONA", "aiona", "GPT-6", "Claude", "Gemini")),
        "codex_sha": sha16(c0), "codex_sha2": sha16(c0b), "codex_chars": len(c0),
        "flag_changed": len(changed_lines(c0, c1)),
        "excerpt": "\n".join(a_lines[:answer_at] + ["(… 답변 원문 …)"] + a_lines[-2:]),
    }


D = prompt_demo()
if not (D["first_sha"] == D["second_sha"] and D["codex_sha"] == D["codex_sha2"] and D["example_same"]
        and D["prompt_sha"] == D["doc_sha"] and D["mark_only"] and D["same_head"] >= D["answer_at"] and D["no_model_names"]):
    print("경고: 프롬프트 고정 시연 결과가 예상과 다릅니다 —", {k: v for k, v in D.items() if k != "excerpt"})


def ok(cond, yes="같음", no="다름"):
    return f'<span class="r-pass">{yes}</span>' if cond else f'<span class="r-fail">{no}</span>'


def sha(*values):
    return f' <span class="sha">({" = ".join(values)})</span>'


pstack = f"""
  <div class="pstack" role="group" aria-label="심판에게 가는 글의 구성">
    <div class="ps prompt"><b>고정 판정 프롬프트 v3</b><span>파일 1개 · {D['prompt_chars']:,}자 — 모든 질문·모델·심판에 같은 글</span></div>
    <div class="ps-sep">[입력] — 아래를 코드가 정해진 순서로 붙임</div>
    <div class="ps mark"><b>실행 표식</b><span>실행마다 새로 고르는 6자리 경계 표시</span></div>
    <div class="ps fixed turn"><b>[이 턴의 상황]</b><span>판정상황표 그대로 — 질문만으로 정해짐</span></div>
    <div class="ps fixed turn"><b>[질문] · [첨부] · [참조 자료]</b><span>질문 세트 그대로</span></div>
    <div class="ps ans turn"><b>[답변]</b><span>수집한 원문 — 모델마다 다른 곳은 여기뿐 (모델 이름은 숨김)</span></div>
  </div>"""

test_table = table(f"Q{D['qid']} 로 직접 만들어 본 결과 — AI 호출 없음", ["확인한 것", "결과"], [
    ["프롬프트 파일이 10-19 이후 바뀌었나", ok(D["prompt_sha"] == D["doc_sha"], "안 바뀜", "바뀜") + " — 10-19 문서에 적은 지문(sha256)과 같음"
     + sha(D["prompt_sha"])],
    ["같은 질문·같은 답으로 두 번 만들면", ok(D["first_sha"] == D["second_sha"]) + sha(D["first_sha"], D["second_sha"])],
    ["10-19 다른 PC 에서 만든 예시와 비교하면", ok(D["example_same"], "글자까지 같음")],
    ["답만 바꾸면 (AIONA → GPT)", f"[답변] 앞 {D['same_head']}줄은 그대로 · 모델 이름 " + ok(D["no_model_names"], "없음", "있음")],
    ["표식만 바꾸면", f"표식이 든 {D['mark_changed']}줄만 바뀜" + ("" if D["mark_only"] else " — " + ok(False, no="표식 밖도 바뀜"))],
    ["Codex 가 받는 전체 글을 두 번", ok(D["codex_sha"] == D["codex_sha2"]) + f" — {D['codex_chars']:,}자" + sha(D["codex_sha"], D["codex_sha2"])],
    [f"Jev 가 항목 하나({D['flag']})를 넘기면", f"[이 턴의 상황] {D['flag_changed']}줄만 바뀜"],
], [42, 58])

vary_table = table("바뀔 수 있는 것은 이것뿐", ["무엇", "언제", "정하는 쪽"], [
    ["실행 표식", "실행마다", "코드 — 질문·답에 없는 6자리, 기록에 남김"],
    ["Codex 가 맡는 J1~J4 항목", "Jev 가 FAIL·애매로 넘길 때", "코드 — Jev 결과에 따라"],
    ["프롬프트 자체", "고쳐야 할 때만", "사람 — v3 를 덮어쓰지 않고 v4 새 파일"],
], [30, 30, 40])

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


# ---------- 06 k 9·12 FAIL 급증 원인 (체크리스트 n6) ----------
# 숫자는 tools/k_jump_analysis.py 가 조건 수 변형 실험(e4ce7a342251)과 지운 실험 세트(git 기록)로 센 것 — 여기서는 읽기만.
KJ = json.loads((ROOT / "참고자료" / "최소평가" / "k급증_원인_e4ce7a342251.json").read_text(encoding="utf-8"))
PK = {x["k"]: x for x in KJ["per_k"]}
JP = {j["k"]: j for j in KJ["jumps"]}
S326 = {x["k"]: x for x in KJ["set326"]}
KIND_LABEL = {"existing": "원래 있던 조건이 새로 깨짐", "added": "새로 붙은 조건에서", "j5": "J5 — 확인 못 한 사실을 FAIL 로 셈(엄격 규칙)"}


def k_chart_frame(kind, aria):
    width, height, left, right, top, bottom = 520, 290, 46, 506, 14, 246
    ks = sorted(PK)
    x = lambda k: left + (k - ks[0] + 0.5) / len(ks) * (right - left)
    y = lambda v: bottom - v * (bottom - top)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{E(aria)}">',
           f'<rect class="kband" x="{x(6) - (right - left) / len(ks) / 2:.1f}" y="{top}" width="{(right - left) / len(ks) * 5:.1f}" height="{bottom - top}"/>',
           f'<text class="axt" x="{x(8):.1f}" y="{top + 13}" text-anchor="middle">지금 k 6~10</text>']
    for f in (0, 0.25, 0.5, 0.75, 1):
        out.append(f'<line class="grid" x1="{left}" y1="{y(f):.1f}" x2="{right}" y2="{y(f):.1f}"/>'
                   f'<text class="tick" x="{left - 6}" y="{y(f) + 4:.1f}" text-anchor="end">{int(f * 100)}%</text>')
    for k in ks:
        out.append(f'<text class="tick" x="{x(k):.1f}" y="{bottom + 16}" text-anchor="middle">{k}</text>')
    out.append(f'<text class="axt" x="{right}" y="{height - 4}" text-anchor="end">질문당 조건 수 k</text>')
    if kind == "fail":
        for key, cls in (("fail_rate", "ink"), ("rate_without_j5_only", "blue")):
            pts = " ".join(f"{x(k):.1f},{y(PK[k][key]):.1f}" for k in ks)
            out.append(f'<polyline class="ln ln-{cls}" points="{pts}"/>')
            for k in ks:
                out.append(f'<circle class="pt-{cls}" cx="{x(k):.1f}" cy="{y(PK[k][key]):.1f}" r="3.5"><title>k {k} — '
                           f'{"기록된 FAIL" if cls == "ink" else "J5 하나로만 FAIL 뺀 것"} {PK[k][key] * 100:.0f}% (판정 {PK[k]["n"]}개)</title></circle>')
        for k in (9, 12):   # 급증 지점 — 기록된 FAIL 값
            out.append(f'<text class="val" x="{x(k):.1f}" y="{y(PK[k]["fail_rate"]) - 9:.1f}" text-anchor="middle">{k - 1}→{k}</text>')
    else:
        w = (right - left) / len(ks) * 0.62
        for k in ks:
            v = PK[k]["format3_share"]
            out.append(f'<rect class="bar" x="{x(k) - w / 2:.1f}" y="{y(v):.1f}" width="{w:.1f}" height="{bottom - y(v):.1f}" rx="2">'
                       f'<title>실험 k {k} — 형식 요구 3개↑ {v * 100:.0f}% (변형 {PK[k]["variants"]}개)</title></rect>')
            if k in S326 and S326[k]["questions"] >= 5:
                out.append(f'<rect class="dia" x="{x(k) - 4:.1f}" y="{y(S326[k]["format3_share"]) - 4:.1f}" width="8" height="8" transform="rotate(45 {x(k):.1f} {y(S326[k]["format3_share"]):.1f})">'
                           f'<title>지금 326 세트 k {k} — 형식 요구 3개↑ {S326[k]["format3_share"] * 100:.0f}% (질문 {S326[k]["questions"]}개)</title></rect>')
    out.append("</svg>")
    return "".join(out)


def pct0(v):
    return f"{v * 100:.0f}%"


j9, j12 = JP[9], JP[12]
jump_table = table("k 가 하나 늘 때 새로 생긴 FAIL — 어디서 나왔나", ["갈래", "8 → 9", "11 → 12"], [
    *[[KIND_LABEL[k], f'<span class="rnum">{j9["kinds"].get(k, 0)}건</span>', f'<span class="rnum">{j12["kinds"].get(k, 0)}건</span>'] for k in ("existing", "added", "j5")],
    ["합계 (PASS → FAIL 로 바뀐 판정)", f'<span class="rnum">{j9["new_fails"]}건 ({j9["pass_to_fail"]})</span>', f'<span class="rnum">{j12["new_fails"]}건 ({j12["pass_to_fail"]})</span>'],
], [50, 25, 25])
per_k_table = table("k 별 값 — 실험 판정(뼈대 18개 × 3모델) · 형식 요구 = 표·목록·제목·JSON 구조·코드블록",
                    ["k", "FAIL 비율", "J5 하나로만 FAIL 뺀 비율", "CODE 위반 있는 비율", "형식 요구 3개↑ (실험)", "형식 요구 3개↑ (지금 326)"],
                    [[f'<span class="rnum">{k}</span>', *[f'<span class="rnum">{pct0(PK[k][f])}</span>' for f in ("fail_rate", "rate_without_j5_only", "code_fail_rate", "format3_share")],
                      f'<span class="rnum">{pct0(S326[k]["format3_share"])} ({S326[k]["questions"]}개)</span>' if k in S326 else "—"] for k in sorted(PK)],
                    [8, 15, 19, 18, 18, 22])
combo = j12["json_plus_format"]
k_points = table("", ["무엇", "결과"], [
    ["급증 재현", f"FAIL 비율 k8 {pct0(PK[8]['fail_rate'])} → k9 {pct0(PK[9]['fail_rate'])}, k11 {pct0(PK[11]['fail_rate'])} → k12 <b>{pct0(PK[12]['fail_rate'])}</b>"],
    ["8 → 9", f"<b>평가 규칙 영향</b> — 실험은 '확인 못 한 J5 = FAIL'로 셈. J5 하나로만 FAIL 인 것을 빼면 {pct0(PK[8]['rate_without_j5_only'])} → {pct0(PK[9]['rate_without_j5_only'])} 로 "
              f"급증이 사라짐. 지금 규칙(확인 못 하면 보류)이면 생기지 않는 급증"],
    ["11 → 12", f"<b>실제 위반</b> — J5 를 빼도 {pct0(PK[11]['rate_without_j5_only'])} → {pct0(PK[12]['rate_without_j5_only'])}. 새 FAIL {j12['new_fails']}건 중 "
                f"{j12['kinds'].get('existing', 0)}건이 원래 있던 조건(목록·제목·표·문구·개수)이 깨진 것. 형식 요구 3개↑ 변형이 {pct0(PK[11]['format3_share'])} → {pct0(PK[12]['format3_share'])}"],
    ["충돌인가 누적인가", f"JSON 과 표·목록·제목을 함께 요구한 변형만 보면 FAIL {combo['with']['fail']}/{combo['with']['n']} — 나머지({combo['without']['fail']}/{combo['without']['n']})보다 높지 않음. "
                       "정면 충돌보다 <b>형식 요구가 한 답에 몰린 것</b>"],
    ["지금 326 세트", f"k 9~10 질문 {S326[9]['questions'] + S326[10]['questions']}개 중 형식 요구 3개↑ <b>{sum(round(S326[k]['format3_share'] * S326[k]['questions']) for k in (9, 10))}개</b> — 실험의 k12 같은 몰림이 없음"],
    ["결정 자료", "k 를 8 이하로 줄일 근거였던 8→9 급증은 평가 규칙 영향. k 를 10 넘게 쓸 때는 질문당 형식 요구 수를 함께 제한해야 함. "
                 "남은 확인: CODE 위반이 실제인지 답 원문(평가 PC 실행 e4ce7a342251)으로"],
], [18, 82])


# ---------- 장 ----------
SECTIONS = [
    ("s1", "전체 흐름도", "질문에서 결과까지 A~H", "전체 흐름도 — 질문에서 결과까지 A~H",
     "질문 326개를 네 모델에 묻고, 답을 코드와 심판 AI 로 채점해 질문마다 PASS·FAIL·보류를 냅니다.",
     f"""
  <figure class="fig poster">
    <span class="poster-sub">2026-10-20 저장소 기준</span>
    <div class="poster-grid">{flow}<aside class="pside" aria-label="흐름도 요약">{side_cards}</aside></div>
  </figure>
  {steps_table}
"""),
    ("s2", "바뀐 점", "10-19 → 지금, 단계마다", "10-19 → 지금 — 무엇이 바뀌었나",
     "10-19 실행 기록과 지금 코드를 단계별로 비교했습니다.",
     f"""{shift_strip}
  {glance_table}
  <p class="callout">참고 — 10-19 발표 01장에는 '답변 수집 — 모두 웹브라우저'로 적었지만, 실제로는 GPT·Claude·Gemini 답을 API 로 받았습니다(487턴 모두).</p>
  {shift_table}
  <p class="basis">출처: 10-19 실행 기록(참고자료/결과/8c61f117e661_정밀326_2026-10-19.json) · 2026-10-20 코드</p>
"""),
    ("s3", "프롬프트 고정", "같은 질문·답변이면 같은 글", "판정 프롬프트는 고정 — 같은 질문·답변이면 같은 글",
     "심판에게 보내는 글은 '고정 파일 + 코드가 붙인 입력'입니다. LLM 이 프롬프트를 만들지 않습니다.",
     f"""
  <div class="block"><h3>심판에게 가는 글</h3>{pstack}</div>
  {test_table}
  {vary_table}
  {details(f"Q{D['qid']} 입력 앞부분 보기", D['excerpt'], "[답변] 줄까지")}
  <p class="basis">이 장의 숫자는 보고서를 만들 때마다 판정과 같은 코드로 다시 계산합니다.</p>
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
    ("s6", "k 급증 원인", "8→9 · 11→12 에서 FAIL 이 뛰는 이유", "k 9·12 에서 FAIL 이 급증하는 이유",
     "조건 수만 바꾼 실험(뼈대 18개 × k 1~16 × GPT·Claude·Gemini)을 다시 뜯어, 급증이 평가 규칙 탓인지 실제 위반인지 나눴습니다.",
     f"""
  <figure class="fig rate-chart">
    <ul class="rate-key"><li><i class="kink"></i>기록된 FAIL</li><li><i class="kkd"></i>J5 하나로만 FAIL 인 것을 뺀 것</li>
      <li><i class="kbar"></i>형식 요구 3개↑ (실험)</li><li><i class="kdia"></i>지금 326 세트</li><li><i class="kband2"></i>지금 k 범위</li></ul>
    <div class="order-charts">
      <div><h3>k 별 FAIL 비율</h3>{k_chart_frame("fail", "질문당 조건 수 k 별 FAIL 비율 — 기록된 것과 J5 하나로만 FAIL 인 것을 뺀 것")}</div>
      <div><h3>형식 요구가 3개 이상인 질문 비율</h3>{k_chart_frame("format", "k 별 형식 요구 3개 이상 질문 비율 — 실험과 지금 326 세트")}</div>
    </div>
  </figure>
  {k_points}
  {jump_table}
  {per_k_table}
  <p class="basis">출처: 참고자료/최소평가/k급증_원인_e4ce7a342251.json (tools/k_jump_analysis.py — 판정 k_experiment_grid.json, 질문은 git 기록의 질문세트_실험300, 조건은 지금 적용 규칙). J5 는 FAIL 사유를 나눠 기록하지 않아 'J5 하나로만 FAIL' 을 뺀 값은 규칙 영향의 최대치입니다.</p>
"""),
]

total = len(SECTIONS)


def head(no, title, lead):
    return f'<div class="s-head"><span class="s-no">{no} / {total:02d}</span><h2>{title}</h2>{f"<p>{lead}</p>" if lead else ""}</div>'


glossary = table("", ["말", "뜻"], [
    ["질문 세트", "모델에 똑같이 던지는 고정 질문 326개 (간단평가는 그중 78개). 대화형 질문은 턴이 여러 개여도 1개로 셈"],
    ["CODE 평가", "프로그램이 형식·글자 수·계산 같은 28가지를 검사"],
    ["의미 평가", "심판 AI 가 내용을 44개 항목으로 판정 — J1~J8 묶음"],
    ["J1~J8", "의도 충족 · 내용 적절성 · 논리 · 충분성 · 사실(DATA) · 대화 맥락 · 긴 문서 활용 · 첨부 활용"],
    ["심판", "Jev(외부 API) — J1~J4 를 먼저 · Codex CLI(ChatGPT 구독) — 나머지를 확정"],
    ["PASS · FAIL · 보류", f"{PASS} 통과 · {FAIL} 실패 · {HOLD} 근거가 부족해 사람이 정함"],
    ["판정상황표", "질문마다 채점할 항목과 근거 문장을 코드가 미리 적어 둔 표"],
], [22, 78])

s0 = f"""
<section class="slide cover" id="s0">
  <div class="cover-top">
    <span class="date">2026-10-20 · 10-19 오후 미팅 후속 보고</span>
    <h1>ModelGrade 보고</h1>
    <p>AI 모델 4개(AIONA · GPT · Claude · Gemini)에 같은 질문을 던지고, 답을 자동으로 채점해 비교하는 도구입니다.</p>
  </div>
  <span class="kicker">오늘 이야기할 것</span>
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
<meta name="description" content="2026-10-20 — 10-19 오후 미팅 후속: 전체 흐름도 · 10-19 에서 바뀐 점 · 판정 프롬프트 고정 · 질문 수별 통과 비율 · 던지는 순서 · k 급증 원인">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+KR:wght@400;500;600;700&display=swap">
{css}
{extra_css}
</head>
<body>
{nav}
<main class="wrap">
{s0}
{sections}
</main>
{script}
</body>
</html>
"""
OUT.write_text(page, encoding="utf-8")
print("저장:", OUT, f"{len(page):,}자")
for w in WARN:
    print("글 넘침 경고:", w)
