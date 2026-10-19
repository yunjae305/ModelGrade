"""ModelGrade 진행 보고 2026-10-19 발표 HTML 생성 (v2 가독성판) — 10-15 자료의 스타일·목차·키 이동을 그대로 쓰고 장을 새로 짠다.
    python -X utf8 tools/build_deck_2026-10-19.py
장: 00 결론 · 01 흐름 · 02 질문생성 프롬프트 · 03 k 범위 · 04 기준 72 · 05 평가 프롬프트 · 06 결과 · 07 최소 78 vs 정밀 326 · 08 질문 수 근거 · 09 비용
"""
import html
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\winz\Desktop\quality-test-integrated")
sys.path.insert(0, str(ROOT))
from webapp import question_generation as qg  # noqa: E402

OLD = Path(r"C:\Users\winz\Downloads\modelgrade-presentation (1).html")   # 10-15 발표 자료 — 스타일·목차·키 이동을 가져온다
OUT = ROOT / "참고자료" / "발표" / "ModelGrade_진행보고_2026-10-19.html"
COPY = Path(r"C:\Users\winz\Downloads\modelgrade-presentation-2026-10-19.html")

old = OLD.read_text(encoding="utf-8")
css = old[old.index("<style>"):old.index("</style>") + len("</style>")]
E = html.escape

# ---------- 자료 ----------
prompt_doc = (ROOT / "참고자료/프롬프트/질문생성_프롬프트_v2.0.md").read_text(encoding="utf-8")
q36 = (ROOT / "참고자료/질문별평가프롬프트_최종326/Q36.md").read_text(encoding="utf-8")
q12 = (ROOT / "참고자료/질문별평가프롬프트_최종326/Q12.md").read_text(encoding="utf-8")
q12_t1 = q12[q12.index("# 1턴"):q12.index("# 2턴")]
q12_head = q12[:q12.index("# 1턴")]
gradient_svg = (ROOT / "참고자료/분석/2026-10-19-기울기/gradient.svg").read_text(encoding="utf-8")
gradient_svg = re.sub(r'<svg ([^>]*)width="760" height="420"', r'<svg \1viewBox="0 0 760 420" style="width:100%;height:auto;max-width:900px"', gradient_svg, count=1)
gradient_svg = gradient_svg.replace('<rect width="760" height="420" fill="white"/>', '<rect width="760" height="420" fill="white" rx="8"/>')
cat = qg.criteria_catalog()
kexp = json.load(open(ROOT / "참고자료/분석/2026-10-18-질문당기준수/boundary_exp_3models.json", encoding="utf-8"))["results"]["strict"]["by_k"]
CRIT_FAIL = json.load(open(ROOT / "참고자료/최소평가/criterion_fail_rates_326.json", encoding="utf-8"))
FBG = json.load(open(ROOT / "참고자료/최소평가/fail_by_group_min78_vs_full.json", encoding="utf-8"))
# k 별 갈림(AIONA 포함 네 모델) — tools/k_split_from_run.py 결과. 보류 반영본이 있으면 우선
_ks_gold = ROOT / "참고자료/최소평가/k별_갈림_8c61f117e661_보류반영.json"
_ks_base = ROOT / "참고자료/최소평가/k별_갈림_8c61f117e661.json"
KSPLIT = json.load(open(_ks_gold if _ks_gold.exists() else _ks_base, encoding="utf-8"))
ksplit_rows = "".join(f'<tr><th>{k}</th><td>{v["n"]}</td><td>{v["all_pass"] / v["n"] * 100:.0f}%</td><td class="d">{v["split"] / v["n"] * 100:.0f}%</td><td>{v["all_fail"] / v["n"] * 100:.0f}%</td></tr>'
                      for k, v in KSPLIT["by_k"].items())
ksplit_title = "AIONA 를 넣어도 같은가 — 326 결과로 확인" + (" (보류 정리 반영)" if KSPLIT["gold"] else "")
_ks_scope = "보류를 사람이 정리한 뒤" if KSPLIT["gold"] else "보류 없는"
_ks_excl = f", 보류 남은 {KSPLIT['excluded_hold']}개 제외" if KSPLIT["excluded_hold"] else ""
ksplit_lead = (f"위 실험에는 AIONA 가 없었습니다(API 세 모델). 326 정밀 평가(AIONA 포함 네 모델, {_ks_scope} 단일 질문 {KSPLIT['confirmed']}개{_ks_excl})로 "
               f"다시 세면 실험과 같은 모양입니다.")


def prompt_sections(doc):
    parts = re.split(r"\n(?=## )", doc)
    out = []
    for p in parts[1:]:
        title, _, body = p.partition("\n")
        body = re.sub(r"^```text\n|\n```\s*$", "", body.strip(), flags=re.S)
        out.append((title[3:].strip(), body))
    return out


sections = prompt_sections(prompt_doc)
k_rule_block = next(b for t, b in sections if t.startswith("④"))
k_rule_text = k_rule_block[k_rule_block.index("# 질문당 기준 수"):k_rule_block.index("# 고칠 질문")].strip()


def details(title, body, open_=False, note=""):
    return (f'<details class="src"{" open" if open_ else ""}><summary><span>{E(title)}{f" <span class=muted>— {E(note)}</span>" if note else ""}</span>'
            f'<span class="muted">{len(body):,}자</span></summary><pre>{E(body)}</pre></details>')


def q36_block():
    """Q36 평가 프롬프트의 핵심을 블록으로 — 질문 / CODE 조건 / 점검 항목(4개 + 수) / 답변 자리."""
    body = q36[q36.index("# 질문"):]
    body = body[body.index("[질문]"):]
    question = body[len("[질문]"):body.index("[CODE 조건]")].strip()
    code_part = body[body.index("[CODE 조건]"):body.index("[이 질문의 점검 항목]")]
    items_part = body[body.index("[이 질문의 점검 항목]"):body.index("[답변]")]
    code_lines = [ln[2:] for ln in code_part.split("\n") if ln.startswith("- ")]
    item_lines = [ln[2:] for ln in items_part.split("\n") if ln.startswith("- ")]

    def li(text):
        cid, _, rest = text.partition(" · ")
        return f"<li><code>{E(cid)}</code> {E(rest.split(' (적용:')[0])}</li>"
    return (f'<div class="exq-block"><span class="lab">질문</span><div class="q">{E(question)}</div>'
            f'<span class="lab">CODE 조건</span><div><ul>{"".join(li(x) for x in code_lines) or "<li>없음</li>"}</ul><p class="muted" style="font-size:.84rem;margin-top:4px">프로그램이 판정 — 심판에게는 참고만</p></div>'
            f'<span class="lab">점검 항목 {len(item_lines)}개</span><div><ul>{"".join(li(x) for x in item_lines[:4])}<li class="muted">… 나머지 {len(item_lines) - 4}개</li></ul></div>'
            f'<span class="lab">답변</span><div class="ans">여기에 모델 답변 원문이 들어갑니다 (어느 모델인지는 숨김)</div></div>')


def q36_excerpt():
    """Q36 평가 프롬프트의 핵심만 — 질문 · CODE 조건 · 점검 항목 4개 · 답변 자리."""
    body = q36[q36.index("# 질문"):]
    lines = body.split("\n")
    out, in_items, shown, total = [], False, 0, 0
    for ln in lines:
        if ln.startswith("[이 질문의 점검 항목]"):
            in_items = True; out.append(ln); continue
        if in_items and ln.startswith("- "):
            total += 1
            if shown < 4:
                out.append(ln); shown += 1
            continue
        if in_items and ln.strip() == "":
            in_items = False
            if total > shown:
                out.append(f"- … (이 질문의 점검 항목은 모두 {total}개)")
        out.append(ln)
    return "\n".join(out).strip()


# ---------- 공통 조각 ----------
def gist(*items):
    return '<ul class="gist">' + "".join(f"<li>{x}</li>" for x in items) + "</ul>"


def head(no, title, lead):
    return f'<div class="s-head"><span class="s-no">{no} / 09</span><h2>{title}</h2><p>{lead}</p></div>'


# ---------- 01 흐름 ----------
def box(x, y, w, h, n, title, lines, cls=""):
    t = [f'<rect class="fb {cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="10"/>',
         f'<text class="fn" x="{x + 14}" y="{y + 24}">{n}</text>', f'<text class="ft" x="{x + 40}" y="{y + 24}">{E(title)}</text>']
    for i, ln in enumerate(lines):
        t.append(f'<text class="fl" x="{x + 14}" y="{y + 48 + i * 19}">{E(ln)}</text>')
    return "\n".join(t)


def arrow(x1, y1, x2, y2):
    return f'<path class="fa" d="M{x1},{y1} L{x2},{y2}" marker-end="url(#ah)"/>'


def line(x1, y1, x2, y2):
    """화살촉 없는 연결선 — 갈라지고 모이는 자리에 쓴다."""
    return f'<path class="fa" d="M{x1},{y1} L{x2},{y2}"/>'


flow = f'''
<svg class="flow" viewBox="0 0 1040 385" role="img" aria-label="평가기 전체 흐름">
<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="fah"/></marker></defs>
{box(0, 10, 300, 92, "1", "질문 생성", ["GPT-6 Luna · 질문생성 프롬프트"])}
{arrow(300, 56, 348, 56)}
{box(350, 10, 340, 92, "2", "질문 세트 326", ["정밀 평가 326개 · 최소 평가 78개 (골라냄)"])}
{arrow(690, 56, 738, 56)}
{box(740, 10, 300, 92, "3", "답변 수집 — 모두 웹브라우저", ["AIONA · ChatGPT · Claude · Gemini (API 안 씀)"])}
{line(890, 102, 890, 119)}{line(150, 119, 890, 119)}
{arrow(150, 119, 150, 134)}{arrow(695, 119, 695, 134)}
{box(0, 136, 300, 110, "4a", "CODE 평가 — 28개 검사", ["프로그램이 형식·개수·계산을 검사"], "code")}
{box(350, 136, 690, 110, "4b", "의미 평가 — 44개 항목", ["GPT-6 Luna (J1~J8) ∥ Jev (J1~J4) 병행 — 갈리면 재판정, 또 다르면 보류"], "sem")}
{line(150, 246, 150, 262)}{line(695, 246, 695, 262)}{line(150, 262, 695, 262)}
{arrow(240, 262, 240, 278)}
{box(0, 280, 480, 92, "5", "질문 단위 판정", ["PASS · FAIL · 보류 — 대화 1개 = 질문 1개"])}
{arrow(480, 326, 558, 326)}
{box(560, 280, 480, 92, "6", "결과", ["모델별 N / 326 · 기준별 FAIL · 보류 목록(사람이 판정)"])}
</svg>'''

# ---------- 03 k 실험 차트 ----------
GRID = json.load(open(ROOT / "참고자료/최소평가/k_experiment_grid.json", encoding="utf-8"))


def grid_svg():
    """기본 질문(행) × k 1~16(열) — 칸 색 = 그 변형에 대한 모델들의 판정: 모두 PASS / 갈림 / 모두 FAIL."""
    bases = list(GRID["bases"].items())
    cw, ch, L, T = 48, 26, 230, 34
    W, H = L + 16 * cw + 20, T + len(bases) * ch + 40
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="기본 질문별 k 1~16 판정 격자">']
    out.append(f'<rect class="zr" x="{L}" y="{T - 4}" width="{5 * cw}" height="{len(bases) * ch + 8}" opacity="0.5"/>')
    out.append(f'<rect class="zg" x="{L + 5 * cw}" y="{T - 4}" width="{5 * cw}" height="{len(bases) * ch + 8}" opacity="0.6"/>')
    out.append(f'<rect class="zr" x="{L + 10 * cw}" y="{T - 4}" width="{6 * cw}" height="{len(bases) * ch + 8}" opacity="0.5"/>')
    for k in range(1, 17):
        out.append(f'<text class="tick" x="{L + (k - 0.5) * cw:.1f}" y="{T - 10}" text-anchor="middle">k={k}</text>')
    for i, (b, info) in enumerate(bases):
        y = T + i * ch
        label = (info.get("topic") or b)[:14]
        out.append(f'<text class="inl" x="{L - 8}" y="{y + ch - 8}" text-anchor="end">{E(label)}</text>')
        for k in range(1, 17):
            cell = info["by_k"].get(str(k))
            x = L + (k - 1) * cw
            if not cell:
                out.append(f'<rect x="{x + 2}" y="{y + 2}" width="{cw - 4}" height="{ch - 4}" rx="4" class="cell none"/>'); continue
            vs = [cell[m]["verdict"] for m in GRID["providers"]]
            cls = "pass" if all(v == "PASS" for v in vs) else "fail" if all(v == "FAIL" for v in vs) else "none" if None in vs else "disc"
            out.append(f'<rect x="{x + 2}" y="{y + 2}" width="{cw - 4}" height="{ch - 4}" rx="4" class="cell {cls}"><title>{E(b)} k={k}: ' +
                       E(" · ".join(f"{m} {cell[m]['verdict'] or '?'}" + (f"({', '.join(cell[m]['fails'][:3])})" if cell[m]["fails"] else "") for m in GRID["providers"])) + '</title></rect>')
    out.append(f'<text class="note" x="{L}" y="{H - 10}">칸 = 기본 질문 하나를 그 k 로 변형한 질문 1개. 빈 칸 = 그 k 변형이 없음. 칸에 마우스를 올리면 모델별 판정과 틀린 기준이 보입니다.</text>')
    out.append('</svg>')
    return "\n".join(out)


def k_chart(rows):
    W, H, L, T, bw, plot_h = 1000, 360, 60, 30, 50, 250
    out = [f'<svg class="chart kchart" viewBox="0 0 {W} {H}" role="img" aria-label="질문당 기준 수 k 별 세 모델 판정 분포">']
    out.append(f'<rect class="zr" x="{L}" y="{T}" width="{5 * 58}" height="{plot_h}"/>')
    out.append(f'<rect class="zg" x="{L + 5 * 58}" y="{T}" width="{5 * 58}" height="{plot_h}"/>')
    out.append(f'<rect class="zr" x="{L + 10 * 58}" y="{T}" width="{6 * 58}" height="{plot_h}"/>')
    out.append(f'<text class="zlab y" x="{L + 5 * 29}" y="{T - 10}" text-anchor="middle">k 1~5 · 너무 쉬움 (다 PASS 가 절반 넘음)</text>')
    out.append(f'<text class="zlab g" x="{L + 5 * 58 + 5 * 29}" y="{T - 10}" text-anchor="middle">k 6~10 · 갈림이 가장 큼 (채택)</text>')
    out.append(f'<text class="zlab r" x="{L + 10 * 58 + 6 * 29}" y="{T - 10}" text-anchor="middle">k 11~16 · 너무 어려움 (다 FAIL)</text>')
    for pct in (0, 25, 50, 75, 100):
        y = T + plot_h - plot_h * pct / 100
        out.append(f'<line class="grid" x1="{L}" y1="{y:.1f}" x2="{L + 16 * 58}" y2="{y:.1f}"/><text class="tick" x="{L - 8}" y="{y + 4:.1f}" text-anchor="end">{pct}%</text>')
    for i, r in enumerate(rows):
        x = L + i * 58 + 4
        ap, af = r["all_pass"], r["all_fail"]
        dc = max(0.0, 1 - ap - af)
        y0 = T + plot_h
        h_af, h_dc, h_ap = plot_h * af, plot_h * dc, plot_h * ap
        out.append(f'<rect class="kb fail" x="{x}" y="{y0 - h_af:.1f}" width="{bw}" height="{h_af:.1f}"/>')
        out.append(f'<rect class="kb disc" x="{x}" y="{y0 - h_af - h_dc:.1f}" width="{bw}" height="{h_dc:.1f}"/>')
        out.append(f'<rect class="kb pass" x="{x}" y="{y0 - h_af - h_dc - h_ap:.1f}" width="{bw}" height="{h_ap:.1f}"/>')
        if dc >= 0.18:
            out.append(f'<text class="kv" x="{x + bw / 2}" y="{y0 - h_af - h_dc / 2 + 4:.1f}" text-anchor="middle">{round(dc * 100)}%</text>')
        out.append(f'<text class="tick" x="{x + bw / 2}" y="{T + plot_h + 18}" text-anchor="middle">k={r["k"]}</text>')
        out.append(f'<text class="tick muted" x="{x + bw / 2}" y="{T + plot_h + 34}" text-anchor="middle">n={r["n"]}</text>')
    out.append(f'<text class="asub" x="{L}" y="{H - 8}">막대 = 그 k 의 질문 중 비율. 기본 질문 18개를 조건 수만 바꿔 k 1~16 으로 변형(255개) + 보충 · GPT·Claude·Gemini API 답 · Jev + GPT-6 Luna 평가 (2026-10-18)</text>')
    out.append('</svg>')
    return "\n".join(out)


def ksplit_chart():
    """326 정밀 평가 — AIONA 포함 네 모델, 단일 질문 k 6~10 의 '모두 PASS / 갈림 / 모두 FAIL' 비율 (보류 정리 반영본이 있으면 그것)."""
    rows = [(int(k), v) for k, v in KSPLIT["by_k"].items()]
    W, H, L, T, bw, plot_h, gap = 1000, 330, 60, 30, 70, 220, 110
    out = [f'<svg class="chart kchart" viewBox="0 0 {W} {H}" role="img" aria-label="326 평가에서 AIONA 포함 네 모델의 k 별 판정 분포">']
    out.append(f'<rect class="zg" x="{L}" y="{T}" width="{len(rows) * gap}" height="{plot_h}"/>')
    out.append(f'<text class="zlab g" x="{L + len(rows) * gap / 2}" y="{T - 10}" text-anchor="middle">k 6~10 · 326 정밀 평가 — AIONA 포함 네 모델{" (보류 정리 반영)" if KSPLIT["gold"] else " (보류 없는 질문만)"}</text>')
    for pct in (0, 25, 50, 75, 100):
        y = T + plot_h - plot_h * pct / 100
        out.append(f'<line class="grid" x1="{L}" y1="{y:.1f}" x2="{L + len(rows) * gap}" y2="{y:.1f}"/><text class="tick" x="{L - 8}" y="{y + 4:.1f}" text-anchor="end">{pct}%</text>')
    for i, (k, v) in enumerate(rows):
        n = v["n"]; x = L + i * gap + (gap - bw) / 2; y0 = T + plot_h
        parts = ((v["all_fail"] / n, "fail"), (v["split"] / n, "disc"), (v["all_pass"] / n, "pass"))
        yb = y0
        for frac, cls in parts:
            h = plot_h * frac
            out.append(f'<rect class="kb {cls}" x="{x:.1f}" y="{yb - h:.1f}" width="{bw}" height="{h:.1f}"/>')
            if cls == "disc" and frac >= 0.15:
                out.append(f'<text class="kv" x="{x + bw / 2:.1f}" y="{yb - h / 2 + 4:.1f}" text-anchor="middle">{frac * 100:.0f}%</text>')
            yb -= h
        out.append(f'<text class="tick" x="{x + bw / 2:.1f}" y="{T + plot_h + 18}" text-anchor="middle">k={k}</text>')
        out.append(f'<text class="tick muted" x="{x + bw / 2:.1f}" y="{T + plot_h + 34}" text-anchor="middle">n={n}</text>')
    xr = L + len(rows) * gap + 30
    out.append(f'<text class="atitle" x="{xr}" y="{T + 20}">갈림 비율 — 326(네 모델) vs 실험(세 모델)</text>')
    out.append(f'<text class="note" x="{xr}" y="{T + 40}">갈림 = 모델 판정이 서로 다른 질문의 비율</text>')
    exp = {r["k"]: r for r in kexp}
    for j, (k, v) in enumerate(rows):
        e = exp.get(k)
        if e:
            out.append(f'<text class="inl" x="{xr}" y="{T + 66 + j * 20}">k={k}: 네 모델 <tspan class="c1">{v["split"] / v["n"] * 100:.0f}%</tspan> (질문 {v["n"]}개) · 실험 {max(0, 1 - e["all_pass"] - e["all_fail"]) * 100:.0f}% ({e["n"]}개)</text>')
    excl = f", 보류 남은 {KSPLIT['excluded_hold']}개 제외" if KSPLIT["excluded_hold"] else ""
    out.append(f'<text class="asub" x="{L}" y="{H - 8}">단일 질문 {KSPLIT["confirmed"]}개 (긴 문서 제외{excl}). 326 세트는 k 6~10 으로만 만들어 그 밖의 k 는 없음.</text>')
    out.append('</svg>')
    return "\n".join(out)


k_table_rows = "".join(
    f'<tr class="{"pick" if 6 <= r["k"] <= 10 else ""}"><th scope="row">{r["k"]}</th><td>{r["n"]}</td><td>{round(r["all_pass"] * 100)}%</td>'
    f'<td class="d">{round(max(0, 1 - r["all_pass"] - r["all_fail"]) * 100)}%</td><td>{round(r["all_fail"] * 100)}%</td></tr>' for r in kexp)

# ---------- 04 기준 72개 ----------
CODE_CAT = {"1": "출력 무결성", "2": "형식·지시 준수", "3": "계산·자료 처리"}
ALWAYS_CODE = {"1-1.response_nonempty", "1-1.character_rules", "1-1.foreign_script"}
ANSWER_ONLY = {"1-2.fence_imbalance", "1-2.orphan_closing_tag", "1-3.footnote_link", "1-3.citation_link", "1-3.internal_link", "3-1.answer_equation", "3-2.declared_count", "J5-3.a"}
ALWAYS_SEM = {"J1-1.a", "J1-2.a", "J1-3.a", "J1-3.b", "J1-4.a", "J1-4.b", "J3-1.a"}
J_NAME = {"J1": "의도 충족성", "J2": "맥락 적합성", "J3": "논리 일관성", "J4": "완결성", "J5": "DATA 신뢰도", "J6": "멀티턴 맥락 유지", "J7": "롱컨텍스트", "J8": "첨부 해석"}


def tag(cid):
    if cid in ALWAYS_CODE or cid in ALWAYS_SEM:
        return '<span class="ctag always">항상</span>'
    if cid in ANSWER_ONLY:
        return '<span class="ctag ans">답변 따라</span>'
    return '<span class="ctag q">질문 조건</span>'


def short(text, n=64):
    text = text or ""
    return f'<span title="{E(text)}">{E(text[:n])}…</span>' if len(text) > n else E(text)


code_rows, last = [], None
for c in cat["code"]:
    grp = c["id"][0]
    if grp != last:
        code_rows.append(f'<tr class="grp"><th colspan="4">{grp}. {CODE_CAT[grp]}</th></tr>'); last = grp
    code_rows.append(f'<tr><th scope="row"><code>{E(c["id"])}</code></th><td>{E(c["name"])}</td><td class="ex">{short(c.get("example"), 70)}</td><td>{tag(c["id"])}</td></tr>')
sem_rows, last = [], None
for it in cat["items"]:
    grp = it["id"].split("-")[0]
    if grp != last:
        sem_rows.append(f'<tr class="grp"><th colspan="4">{grp}. {J_NAME.get(grp, "")}</th></tr>'); last = grp
    sem_rows.append(f'<tr><th scope="row"><code>{E(it["id"])}</code> <span class="muted">{E(it.get("sub") or "")}</span></th><td>{E(it.get("question") or "")}</td><td class="ex">{short(it.get("applies"), 60)}</td><td>{tag(it["id"])}</td></tr>')

# ---------- 06 결과 ----------
MODEL_NAME = {"gpt": "GPT-6 Luna", "aiona": "AIONA", "claude": "Claude Sonnet 5.5", "gemini": "Gemini 3.6 Flash"}
# 모델별 질문 판정 — 위 KSPLIT(k_split JSON, 보류 정리 반영본 우선)에서. 보류 정리 전 값: GPT 190·77·59 / AIONA 166·84·76 / Claude 131·112·83 / Gemini 120·124·82
RES = [(MODEL_NAME[m], m, KSPLIT["models"][m]["PASS"], KSPLIT["models"][m]["FAIL"], KSPLIT["models"][m]["HOLD"]) for m in ("gpt", "aiona", "claude", "gemini")]
RATE = {m: p / 326 * 100 for _, m, p, _, _ in RES}


def pf(p_, f, h):
    """PASS:FAIL 비율(확정 질문 중, %)과 보류 비율(전체 중, %) — 사용자 결정 10-19: 결과는 이 두 숫자로 보인다."""
    d = p_ + f
    return (p_ / d * 100 if d else 0.0, f / d * 100 if d else 0.0, h / (p_ + f + h) * 100 if p_ + f + h else 0.0)


PF = {m: pf(p_, f, h) for _, m, p_, f, h in RES}
RANK = sorted(PF, key=lambda m: -PF[m][0])
GOLD_APPLIED = bool(KSPLIT["gold"])
res_rows = "".join(
    f'<div class="fs-row"><span class="who"><i style="background:var(--m-{m})"></i>{E(n)}</span>'
    f'<div class="fs-track"><span class="p" style="width:{p / 326 * 100:.1f}%" title="PASS {p}"></span><span class="f2" style="width:{f / 326 * 100:.1f}%" title="FAIL {f}"></span><span class="h" style="width:{h / 326 * 100:.1f}%" title="보류 {h}"></span></div>'
    f'<span class="val"><b>PASS {PF[m][0]:.0f}</b> : FAIL {PF[m][1]:.0f} <span class="muted">({p}·{f}) · 보류 {PF[m][2]:.0f}% ({h})</span></span></div>' for n, m, p, f, h in RES)
CODE_FAIL = {"gpt": (55, [("문장 수", 11), ("자료 변환", 11), ("소제목", 7), ("JSON 구조", 7)]), "aiona": (73, [("문장 수", 16), ("자료 변환", 10), ("닫는 태그", 9), ("빈 응답", 7)]),
             "claude": (68, [("문장 수", 23), ("자료 변환", 12), ("글자 수", 8), ("질문 계산", 5)]), "gemini": (80, [("문장 수", 20), ("자료 변환", 13), ("소제목", 13), ("목록", 7)])}
SEM_FAIL = {"gpt": (29, [("J5 사실", 29), ("J6 멀티턴", 16), ("J4 완결", 5), ("J7 긴 문서", 3)]), "aiona": (27, [("J6 멀티턴", 20), ("J5 사실", 15), ("J2 맥락", 8), ("J1 의도", 5)]),
            "claude": (55, [("J5 사실", 52), ("J6 멀티턴", 27), ("J2 맥락", 14), ("J3 논리", 10)]), "gemini": (57, [("J5 사실", 79), ("J6 멀티턴", 78), ("J2 맥락", 37), ("J3 논리", 17)])}


def chips(items):
    return '<div class="chips">' + "".join(f'<span>{E(k)}<b>{v}</b></span>' for k, v in items) + '</div>'


fail_rows = "".join(f'<tr><th scope="row">{E(n)}</th><td class="num">{CODE_FAIL[m][0]}</td><td>{chips(CODE_FAIL[m][1])}</td><td class="num">{SEM_FAIL[m][0]}</td><td>{chips(SEM_FAIL[m][1])}</td></tr>' for n, m, *_ in RES)
HOLD = [("J5 심판이 옮겨 적은 문장이 답변과 안 맞음", (67, 18, 14, 7), "'우천 안내' 를 '날씨 안내' 로 적는 식 — 대조 규칙 보강 대상"),
        ("J5 웹 출처 원문을 못 열음", (60, 8, 5, 11), "검색은 했으나 원문·인용문 확인 실패"),
        ("J6 멀티턴 근거 부족", (26, 15, 7, 9), "앞 턴 조건을 지켰는지 확정 못 함"),
        ("J1~J4 두 심판이 갈림", ("3~16", "", "", ""), "재판정도 달라 보류 — 설계한 경우"),
        ("빈 출력 요구 질문", ("10", "", "", ""), "질문 10개 전체 보류 (AIONA 오류 화면 8 포함)")]
hold_rows = "".join(f'<tr><th scope="row">{E(a)}</th>' + "".join(f'<td class="num">{E(str(v))}</td>' for v in b) + f'<td>{E(c)}</td></tr>' for a, b, c in HOLD)
# 기준별 FAIL 률 — 4 모델 턴 단위 판정 합. 의미 기준은 J 묶음 단위로 기록됨
CF_TOP = [r for r in CRIT_FAIL if r["applied"] >= 20 and r["fail"] > 0][:12]
cf_rows = "".join(f'<tr><th scope="row"><code>{E(r["id"])}</code> {E(r["name"])}</th><td class="num">{r["applied"]:,}</td><td class="num"><b>{r["fail"]}</b> ({r["rate"] * 100:.0f}%)</td>'
                  f'<td class="exq">{" · ".join(f"{k} {v}" for k, v in r["by_model"].items() if v)}</td></tr>' for r in CF_TOP)
CF_ZERO = [r for r in CRIT_FAIL if r["fail"] == 0]
cf_zero_line = " · ".join(f'<code>{E(r["id"])}</code>({r["applied"]:,})' for r in CF_ZERO)

# ---------- 07 최소 78 vs 정밀 326 ----------
MIN78 = {m: (v["PASS"], v["FAIL"], v["HOLD"]) for m, v in (KSPLIT.get("min78") or {"gpt": {"PASS": 34, "FAIL": 23, "HOLD": 21}, "aiona": {"PASS": 31, "FAIL": 25, "HOLD": 22},
                                                                                   "claude": {"PASS": 29, "FAIL": 28, "HOLD": 21}, "gemini": {"PASS": 26, "FAIL": 29, "HOLD": 23}}).items()}
RATE78 = {m: MIN78[m][0] / 78 * 100 for m in MIN78}
PF78 = {m: pf(*MIN78[m]) for m in MIN78}
RANK78 = sorted(PF78, key=lambda m: -PF78[m][0])
GROUP_LABEL = {"CODE 1": "CODE 1 출력 무결성", "CODE 2": "CODE 2 형식·지시", "CODE 3": "CODE 3 계산·자료", "J1": "J1 의도 충족", "J2": "J2 맥락 적합", "J3": "J3 논리 일관",
               "J4": "J4 완결성", "J5": "J5 DATA 신뢰도", "J6": "J6 멀티턴 맥락", "J7": "J7 롱컨텍스트", "J8": "J8 첨부 해석"}


def cmp_chart():
    W, H, L, T, ph = 1000, 300, 90, 36, 200
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="정밀 326 과 최소 78 의 모델별 PASS·FAIL·보류 비율">']
    for pct in (0, 25, 50, 75, 100):
        y = T + ph - ph * pct / 100
        out.append(f'<line class="grid" x1="{L}" y1="{y:.1f}" x2="{W - 20}" y2="{y:.1f}"/><text class="tick" x="{L - 8}" y="{y + 4:.1f}" text-anchor="end">{pct}%</text>')
    slot = (W - 20 - L) / 4
    for i, (name, m, p_, f, h) in enumerate(RES):
        x0 = L + i * slot + 30
        for j, (lab, tot, (pp, ff, hh)) in enumerate((("정밀 326", 326, (p_, f, h)), ("최소 78", 78, MIN78[m]))):
            x = x0 + j * 90
            yb = T + ph
            for val, cls in ((hh, "h"), (ff, "f2"), (pp, "p")):
                hgt = ph * val / tot
                out.append(f'<rect class="cb {cls}" x="{x}" y="{yb - hgt:.1f}" width="70" height="{hgt:.1f}"/>')
                if val / tot >= 0.12:
                    out.append(f'<text class="kv" x="{x + 35}" y="{yb - hgt / 2 + 4:.1f}" text-anchor="middle">{val}</text>')
                yb -= hgt
            out.append(f'<text class="tick" x="{x + 35}" y="{T + ph + 16}" text-anchor="middle">{lab}</text>')
            a_, b_, c_ = pf(pp, ff, hh)
            out.append(f'<text class="inl" x="{x + 35}" y="{T - 8}" text-anchor="middle">{a_:.0f}:{b_:.0f} · 보류 {c_:.0f}%</text>')
        out.append(f'<text class="atitle" x="{x0 + 80}" y="{T + ph + 36}" text-anchor="middle">{E(name)}</text>')
    out.append(f'<text class="note" x="{L}" y="{H - 4}">막대 안 숫자 = 질문 수 · 위 숫자 = PASS:FAIL 비율(확정 질문 중) · 보류 비율. 최소 78 은 정밀 326 의 부분집합이라 같은 판정을 그대로 셈.</text>')
    out.append('</svg>')
    return "\n".join(out)


fbg_rows = []
for g in ("CODE 1", "CODE 2", "CODE 3", "J1", "J2", "J3", "J4", "J5", "J6", "J7", "J8"):
    row = FBG.get(g, {})
    cells = []
    for _, m, *_ in RES:
        full_n, sub_n = row.get(m, (0, 0))
        cells.append(f'<td class="num">{sub_n} <span class="muted">/ {full_n}</span></td>' if full_n else '<td class="num muted">—</td>')
    fbg_rows.append(f'<tr><th scope="row">{E(GROUP_LABEL[g])}</th>{"".join(cells)}</tr>')
fbg_rows = "".join(fbg_rows)
def pf_str(counts):
    return " · ".join(f"{pf(*counts[m])[0]:.0f}:{pf(*counts[m])[1]:.0f}" for m in ("aiona", "gpt", "claude", "gemini"))


MIN_CNT = {7: {"aiona": (4, 2, 1), "gpt": (2, 3, 2), "claude": (2, 2, 3), "gemini": (2, 3, 2)},
           21: {"aiona": (7, 9, 5), "gpt": (7, 10, 4), "claude": (6, 8, 7), "gemini": (6, 10, 5)},
           37: {"aiona": (16, 12, 9), "gpt": (14, 13, 10), "claude": (11, 14, 12), "gemini": (13, 15, 9)}}
FULL_CNT = {m: (p_, f, h) for _, m, p_, f, h in RES}
MIN = [("기준마다 1번↑", 7, "커버 점검", pf_str(MIN_CNT[7]), "aiona > claude > gemini > gpt", "다름", "24%"),
       ("기준마다 3번↑", 21, "", pf_str(MIN_CNT[21]), "aiona > gpt > claude > gemini", "다름", "49%"),
       ("기준마다 5번↑", 37, "", pf_str(MIN_CNT[37]), "aiona > gpt > gemini > claude", "다름", "61%"),
       ("기준마다 10번↑", 78, "최소 평가", pf_str(MIN78), " > ".join(RANK78), "같음" if RANK78 == RANK else "다름", "80%"),
       ("전체", 326, "정밀 평가", pf_str(FULL_CNT), " > ".join(RANK), "기준", "100%")]
min_rows = "".join(f'<tr{" class=pick" if b in (78, 326) else ""}><th scope="row">{E(a)}</th><td class="num">{b}</td><td class="exq">{E(c)}</td><td class="exq">{E(d)}</td><td class="exq">{E(e)}</td><td>{E(f)}</td><td class="num">{g}</td></tr>' for a, b, c, d, e, f, g in MIN)

# ---------- 08 질문 수 근거 ----------
FC = [(7, 1, 24.1, 3.9, 12.5), (21, 3, 49.4, 12.8, 29.9), (37, 5, 60.9, 18.3, 42.5), (78, 10, 80.5, 35.7, 64.5), (155, 20, 87.4, 62.1, 82.5), (326, 0, 100.0, 100.0, 100.0)]
BS = [(50, 52.3, 80.2, 89.5, 64.5), (100, 67.3, 89.8, 97.2, 72.1), (150, 77.1, 94.4, 98.8, 78.1), (250, 86.1, 98.4, 100.0, 84.5), (326, 88.5, 99.1, 100.0, 87.9), (500, 92.7, 99.9, 100.0, 91.6)]


def ss_chart():
    W, H = 1000, 340
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="질문 수에 따른 FAIL 포착률과 순위 일치 확률">']
    L, T, pw, ph = 50, 40, 400, 240
    xs = lambda n: L + pw * (n / 326)
    ys = lambda c: T + ph - ph * (c / 100)
    out.append(f'<text class="atitle" x="{L}" y="{T - 16}">① 전체 FAIL 조합(모델×기준 87개) 중 목록에서 보이는 비율</text>')
    for c in (25, 50, 75, 100):
        out.append(f'<line class="grid" x1="{L}" y1="{ys(c):.1f}" x2="{L + pw}" y2="{ys(c):.1f}"/><text class="tick" x="{L - 6}" y="{ys(c) + 4:.1f}" text-anchor="end">{c}%</text>')
    for n in (7, 78, 155, 326):
        out.append(f'<text class="tick" x="{xs(n):.1f}" y="{T + ph + 16}" text-anchor="middle">{n}</text>')
    for idx, cls in ((2, "ln cv20"), (4, "ln gpt")):
        pts = " ".join(f"{xs(r[0]):.1f},{ys(r[idx]):.1f}" for r in FC)
        out.append(f'<polyline class="{cls}" points="{pts}"/>')
    for r in FC[:-1]:
        out.append(f'<circle class="pt cv20" cx="{xs(r[0]):.1f}" cy="{ys(r[2]):.1f}" r="4"/><text class="inl" x="{xs(r[0]) + 6:.1f}" y="{ys(r[2]) - 6:.1f}">{r[1]}번↑ {r[0]}개 {r[2]:.0f}%</text>')
    out.append(f'<line class="guide" x1="{xs(78):.1f}" y1="{T}" x2="{xs(78):.1f}" y2="{T + ph}"/>')
    out.append(f'<text class="note" x="{L}" y="{H - 8}">진한 선 = 표준 탐욕 목록(기준마다 n번↑) · 초록 = 같은 수를 무작위로 뽑은 평균</text>')
    L2 = 540
    xs2 = lambda n: L2 + pw * (n / 500)
    ys2 = lambda p: T + ph - ph * (p / 100)
    out.append(f'<text class="atitle" x="{L2}" y="{T - 16}">② 새 세트 N개일 때 두 모델의 순서가 맞을 확률</text>')
    for p in (50, 75, 95, 100):
        out.append(f'<line class="grid{" strong" if p == 95 else ""}" x1="{L2}" y1="{ys2(p):.1f}" x2="{L2 + pw}" y2="{ys2(p):.1f}"/><text class="tick" x="{L2 - 6}" y="{ys2(p) + 4:.1f}" text-anchor="end">{p}%</text>')
    for n in (50, 100, 250, 326, 500):
        out.append(f'<text class="tick" x="{xs2(n):.1f}" y="{T + ph + 16}" text-anchor="middle">{n}</text>')
    for idx, cls, lab in ((3, "ln gpt", "AIONA > Claude (10.7pp)"), (2, "ln aiona", "GPT > AIONA (7.4pp)"), (4, "ln gemini", "Claude > Gemini (3.4pp)")):
        pts = " ".join(f"{xs2(r[0]):.1f},{ys2(r[idx]):.1f}" for r in BS)
        out.append(f'<polyline class="{cls}" points="{pts}"/>')
        out.append(f'<text class="inl" x="{xs2(BS[-1][0]) + 4:.1f}" y="{ys2(BS[-1][idx]) + 4:.1f}">{lab}</text>')
    out.append(f'<line class="guide" x1="{xs2(250):.1f}" y1="{T}" x2="{xs2(250):.1f}" y2="{T + ph}"/><text class="mk" x="{xs2(250) + 4:.1f}" y="{T + 14}">250: GPT>AIONA 98%</text>')
    out.append(f'<text class="note" x="{L2}" y="{H - 8}">같은 분포에서 N개를 새로 뽑는다고 보고 4,000번 반복. 점선 = 95%</text>')
    out.append('</svg>')
    return "\n".join(out)


fc_rows = "".join(f'<tr{" class=pick" if r[0] in (78, 326) else ""}><th scope="row">{r[0]}</th><td>{"정밀" if r[1] == 0 else f"{r[1]}번↑"}</td><td class="d">{r[2]:.0f}%</td><td>{r[3]:.0f}%</td><td>{r[4]:.0f}%</td></tr>' for r in FC)
bs_rows = "".join(f'<tr{" class=pick" if r[0] in (250, 326) else ""}><th scope="row">{r[0]}</th><td>{r[1]:.0f}%</td><td class="d">{r[2]:.0f}%</td><td>{r[3]:.0f}%</td><td>{r[4]:.0f}%</td></tr>' for r in BS)

# ---------- 09 비용 ----------
COST = [
    ("질문 생성", "GPT-6 Luna", "326 세트 생성 67회 호출", "18.6만 / 47.3만", "$0.26"),
    ("질문 생성", "GPT-6 Luna", "k 고치기(질문 하나씩, 3회차) · 10-18 실험 세트", "기록 일부 없음", "약 $1~2"),
    ("답변 수집", "GPT-6 Luna (API)", "487턴 + 재수집 (긴 문서 29만 토큰 × 20회 포함)", "1,108만 / 63만", "$1.0"),
    ("답변 수집", "Claude Sonnet 5.5 (API)", "487턴 + 재수집 — 긴 문서 10개 $19 · 대화 32개 $27 · 단일 $8", "1,953만 / 151만", "$54.2"),
    ("답변 수집", "Gemini 3.6 Flash (API)", "487턴 + 재수집 (추론 토큰 포함)", "714만 / 158만", "$11.3"),
    ("답변 수집", "AIONA (웹브라우저)", "487턴 — API 비용 없음", "—", "$0"),
    ("답변 수집", "GPT·Claude·Gemini", "10-18 통제 실험 300문항 (세 모델)", "348만 / 100만", "$8.2"),
    ("의미 평가", "GPT-6 Luna", "326 최종 평가 1,938판정 + 재판정·J5 보강 (호출 2,600회) · 웹 검색 2,207회($22)", "1억 200만 / 2,050만", "$44.0"),
    ("의미 평가", "GPT-6 Luna", "326 평가 중 취소·재평가로 버린 판정 (기록 없음, 추정)", "약 2,000만", "약 $13~16"),
    ("의미 평가", "GPT-6 Luna", "10-18 실험 평가 (기존 326 세트 1,275판정 · 통제 실험 1,849 · 판정 갈림 시험 458)", "7,470만 / 1,790만", "$23.9"),
    ("의미 평가", "Jev (J1~J4)", "326 최종 평가 1,821회 호출 (긴 입력 96턴은 한도 초과) + 10-18 실험", "1,270만 / 10만 (+실험)", "단가표 없음"),
]
cost_rows = "".join(f'<tr><th scope="row">{E(a)}</th><td>{E(b)}</td><td class="exq">{E(c)}</td><td class="num">{E(d)}</td><td class="num">{E(e)}</td></tr>' for a, b, c, d, e in COST)
COST_SUM = [("OpenAI (GPT-6 Luna)", "질문 생성 + GPT 답변 + 의미 평가 전부", "약 2억 4천만", "약 $86~90"),
            ("Anthropic (Claude Sonnet 5.5)", "답변 수집만", "약 2,300만", "약 $59"),
            ("Google (Gemini 3.6 Flash)", "답변 수집만", "약 1,100만", "약 $14"),
            ("합계 (10-18 ~ 10-19 이틀)", "", "약 2억 7천만", "약 $160")]
cost_sum_rows = "".join(f'<tr{" class=tot" if a.startswith("합계") else ""}><th scope="row">{E(a)}</th><td class="exq">{E(b)}</td><td class="num">{E(c)}</td><td class="num">{E(d)}</td></tr>' for a, b, c, d in COST_SUM)

# ---------- HTML ----------
extra_css = """
<style>
/* 2026-10-19 추가 */
:root { --m-claude: #7a4fb3; } :root[data-theme="dark"] { --m-claude: #b49be0; } @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --m-claude: #b49be0; } }
.s-head h2 { font-size: 1.75rem; }
.gist { margin: 0; padding: 14px 18px 14px 36px; background: var(--accent-soft); border-radius: 10px; display: flex; flex-direction: column; gap: 6px; font-size: 0.98rem; }
.gist li::marker { color: var(--accent); } .gist b { color: var(--ink); }
.flow { display: block; width: 100%; height: auto; font-family: var(--font-body); }
.flow .fb { fill: var(--surface); stroke: var(--line); stroke-width: 1.4; }
.flow .fb.code { stroke: var(--accent); stroke-width: 2; } .flow .fb.sem { stroke: var(--ok); stroke-width: 2; }
.flow .fn { fill: var(--accent); font-family: var(--font-num); font-weight: 700; font-size: 15px; }
.flow .ft { fill: var(--ink); font-weight: 700; font-size: 16px; } .flow .fl { fill: var(--muted); font-size: 13px; }
.flow .fa { stroke: var(--muted); stroke-width: 1.6; fill: none; } .flow .fah { fill: var(--muted); }
details.src { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 0; min-width: 0; }
details.src summary { cursor: pointer; padding: 12px 16px; font-weight: 600; font-size: 0.95rem; list-style: none; display: flex; justify-content: space-between; gap: 12px; }
details.src summary::after { content: "펼치기"; font-size: 0.8rem; color: var(--accent); font-weight: 600; white-space: nowrap; }
details.src[open] summary::after { content: "접기"; }
details.src > div { border-top: 1px solid var(--line); }
details.src pre { margin: 0; border-top: 1px solid var(--line); padding: 14px 16px; max-height: 560px; overflow: auto; font-family: var(--font-num); font-size: 0.78rem; line-height: 1.55; white-space: pre-wrap; word-break: break-word; color: var(--ink); background: var(--bg); border-radius: 0 0 10px 10px; }
pre.show { margin: 0; padding: 14px 16px; font-family: var(--font-num); font-size: 0.82rem; line-height: 1.6; white-space: pre-wrap; word-break: break-word; background: var(--surface); border: 1px solid var(--line); border-left: 4px solid var(--accent); border-radius: 10px; color: var(--ink); }
.steps4 { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.steps4 > div { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.steps4 .k { font-family: var(--font-num); font-size: 0.8rem; color: var(--accent); font-weight: 600; }
.steps4 b { font-size: 0.98rem; } .steps4 span { font-size: 0.86rem; color: var(--muted); }
svg .kb.pass { fill: var(--ok); opacity: 0.55; } svg .kb.disc { fill: var(--accent); } svg .kb.fail { fill: var(--risk); opacity: 0.6; }
svg .kv { fill: #fff; font-family: var(--font-num); font-size: 12px; font-weight: 700; }
svg .cb.p { fill: var(--ok); } svg .cb.f2 { fill: var(--risk); opacity: 0.75; } svg .cb.h { fill: var(--warn); opacity: 0.55; }
svg .cell.pass { fill: var(--ok); opacity: 0.7; } svg .cell.disc { fill: var(--accent); } svg .cell.fail { fill: var(--risk); opacity: 0.7; } svg .cell.none { fill: var(--line); opacity: 0.5; }
.ktab table { min-width: 480px; } .ktab tr.pick td, .ktab tr.pick th { background: var(--ok-soft); } .ktab td.d { font-weight: 700; color: var(--accent); }
.ctab table { min-width: 720px; font-size: 0.86rem; } .ctab td.ex { font-family: var(--font-body); color: var(--muted); white-space: normal; } .ctab td { white-space: normal; font-family: var(--font-body); }
.ctab th[scope="row"] { white-space: nowrap; } .ctab code { font-family: var(--font-num); font-size: 0.8rem; }
.ctag { font-size: 0.72rem; font-weight: 600; padding: 1px 8px; border-radius: 999px; white-space: nowrap; }
.ctag.q { color: var(--accent); background: var(--accent-soft); } .ctag.always { color: var(--ok); background: var(--ok-soft); } .ctag.ans { color: var(--warn); background: var(--warn-soft); }
.fs-track .p { background: var(--ok); } .fs-track .f2 { background: var(--risk); opacity: 0.75; } .fs-track .h { background: repeating-linear-gradient(135deg, var(--warn) 0 6px, var(--warn-soft) 6px 10px); }
.fs-row .val b { color: var(--ok); }
.rtab table { min-width: 640px; } .rtab td.exq { white-space: normal; font-family: var(--font-body); } .rtab td.num { color: var(--ink); }
.rtab tr.tot th, .rtab tr.tot td { background: var(--accent-soft); font-weight: 700; } .rtab tr.pick th, .rtab tr.pick td { background: var(--ok-soft); }
.rtab code { font-family: var(--font-num); font-size: 0.8rem; }
/* v3 가독성: 표는 본문체, 숫자만 고정폭 · 칩 · 용어 줄 · 예시 블록 */
.cmpt table, .ktab table, .rtab table { font-size: 0.95rem; }
.cmpt td, .rtab td, .ktab td { font-family: var(--font-body); white-space: normal; color: var(--ink); line-height: 1.5; }
.cmpt td.num, .rtab td.num, .ktab td, .ktab td.d, .cmpt td .n { font-family: var(--font-num); font-variant-numeric: tabular-nums; white-space: nowrap; }
.cmpt th, .cmpt td, .rtab th, .rtab td { padding: 12px 16px; vertical-align: top; }
.cmpt thead th { font-size: 0.84rem; }
.chips { display: flex; flex-wrap: wrap; gap: 5px; }
.chips span { font-size: 0.82rem; padding: 2px 9px; border-radius: 999px; background: var(--bg); border: 1px solid var(--line); white-space: nowrap; }
.chips span b { font-family: var(--font-num); color: var(--risk); margin-left: 4px; }
.terms { display: flex; flex-wrap: wrap; gap: 8px 14px; font-size: 0.9rem; color: var(--muted); padding: 12px 16px; border: 1px dashed var(--line); border-radius: 10px; }
.terms span b { color: var(--ink); margin-right: 4px; }
.gist { font-size: 1rem; line-height: 1.6; }
.agenda { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: 1fr 1fr; gap: 10px 24px; }
.agenda li { min-width: 0; }
.agenda a { display: grid; grid-template-columns: 40px 1fr; gap: 2px 10px; padding: 12px 14px; text-decoration: none; color: var(--ink); background: var(--surface); border: 1px solid var(--line); border-radius: 10px; }
.agenda a:hover { border-color: var(--accent); }
.agenda .n { font-family: var(--font-num); color: var(--accent); font-weight: 700; grid-row: span 2; font-size: 1.1rem; padding-top: 2px; }
.agenda strong { font-size: 1.05rem; }
.agenda .d { font-size: 0.88rem; color: var(--muted); line-height: 1.5; }
@media (max-width: 900px) { .agenda { grid-template-columns: 1fr; } }
.concl-cols ul { font-size: 0.95rem; gap: 6px; } .concl-cols li { line-height: 1.55; }
.exq-block { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 10px 14px; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; font-size: 0.95rem; }
.exq-block .lab { font-size: 0.8rem; font-weight: 700; color: var(--accent); padding-top: 3px; }
.exq-block .q { line-height: 1.7; }
.exq-block ul { margin: 0; padding-left: 1.1em; display: flex; flex-direction: column; gap: 5px; }
.exq-block li code { font-family: var(--font-num); font-size: 0.82rem; color: var(--accent); }
.exq-block .ans { color: var(--muted); font-style: normal; }
.card-body p { font-size: 0.95rem; line-height: 1.6; }
.note p, .defbox p { line-height: 1.6; }
@media (max-width: 640px) { .exq-block { grid-template-columns: 1fr; } }
/* 가로 스크롤 없이 다 보이게 (사용자 요청 10-19) — 최소 너비 제거, 표 칸은 줄바꿈, 차트는 폭에 맞춰 축소 */
.cmpt, .rtab, .ktab, .chart-scroll, .qtable, .effect { overflow-x: visible; }
.cmpt table, .ktab table, .rtab table, .ctab table, .ttable, .qtable table, .effect table { min-width: 0; width: 100%; table-layout: auto; }
svg.chart { min-width: 0; }
.cmpt th, .cmpt td { white-space: normal; }
.cmpt td.num, .ktab td { white-space: nowrap; }
.ctab th[scope="row"] { white-space: normal; }
.ctab table { font-size: 0.84rem; } .ctab th, .ctab td { padding: 8px 10px; }
.ktab th, .ktab td { padding: 8px 10px; }
.fs-row { grid-template-columns: 150px minmax(0, 1fr) 230px; } .fs-row .val { white-space: normal; }
.gradwrap { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 16px; display: flex; justify-content: center; }
.three { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.defbox { background: var(--surface); border: 1px solid var(--line); border-left: 4px solid var(--ok); border-radius: 10px; padding: 16px 20px; display: flex; flex-direction: column; gap: 8px; }
.defbox h3 { font-size: 1.05rem; } .defbox p { font-size: 0.95rem; color: var(--muted); } .defbox p b { color: var(--ink); }
.defbox.warn { border-left-color: var(--warn); }
table.mini { border-collapse: collapse; font-size: 0.86rem; width: 100%; } table.mini th, table.mini td { padding: 5px 10px; border-top: 1px solid var(--line); text-align: left; font-family: var(--font-num); }
table.mini thead th { font-family: var(--font-body); color: var(--muted); font-size: 0.78rem; border-top: 0; } table.mini td.d { color: var(--accent); font-weight: 700; }
@media (max-width: 900px) { .steps4, .three { grid-template-columns: 1fr; } }
</style>"""

TOC = [("s1", "01", "평가기 흐름", "질문 생성부터 결과까지 한 그림"), ("s2", "02", "질문생성 프롬프트", "구조와 원문 전체"),
       ("s3", "03", "k 범위 근거", "왜 6~10인가 — 통제 실험"), ("s4", "04", "평가 기준 72개", "CODE 28 + 의미 44"),
       ("s5", "05", "평가 프롬프트", "심판에게 무엇을 보내나"), ("s6", "06", "결과", "네 모델 326개 · 어디서 틀렸나"),
       ("s7", "07", "최소 78 vs 정밀 326", "같은 판정으로 비교"), ("s8", "08", "질문 수 근거", "왜 78 / 왜 326"), ("s9", "09", "비용·토큰", "단계 × 모델")]
nav = f"""
<nav class="topnav" aria-label="목차">
  <div class="topnav-in">
    <span class="brand">ModelGrade 진행 보고</span>
    <div class="toc-links" id="toc-links">
      <a href="#s0"><span class="n">00</span>목차</a>
      {"".join(f'<a href="#{i}"><span class="n">{n}</span>{t}</a>' for i, n, t, _ in TOC)}
    </div>
    <div class="pager"><button type="button" id="prev" aria-label="이전 장">←</button><button type="button" id="next" aria-label="다음 장">→</button></div>
  </div>
</nav>
"""

s0 = f"""
<section class="slide cover" id="s0">
  <div class="cover-top">
    <span class="date">2026-10-19 · 326개 질문 생성 → 네 모델 답변 수집 → 평가 완료</span>
    <h1>ModelGrade 진행 보고</h1>
    <p>AI 모델(AIONA · GPT · Claude · Gemini)에 같은 질문을 던지고, 받은 답변을 자동으로 채점해 비교하는 도구입니다. 이번에는 질문 생성부터 평가·비용까지 한 바퀴를 끝까지 돌렸습니다.</p>
  </div>
  <span class="kicker">오늘 이야기할 것</span>
  <ol class="agenda">
    <li><a href="#s1"><span class="n">01</span><strong>평가기는 어떻게 도는가</strong><span class="d">질문 생성 → 질문 세트(정밀 326 / 최소 78) → 답변 수집 → CODE + 의미 평가 → 질문 단위 판정 → 결과</span></a></li>
    <li><a href="#s2"><span class="n">02</span><strong>질문은 어떻게 만들었나</strong><span class="d">질문생성 프롬프트의 구조와 원문 전체</span></a></li>
    <li><a href="#s3"><span class="n">03</span><strong>질문당 기준 수 k 를 왜 6~10 으로 했나</strong><span class="d">조건 수만 바꾼 통제 실험 — 세 모델이 갈리는 구간, AIONA 포함 재확인</span></a></li>
    <li><a href="#s4"><span class="n">04</span><strong>평가 기준 72개는 무엇인가</strong><span class="d">CODE 28 (프로그램) + 의미 44 (심판 모델) — 어떤 질문에서 걸리나</span></a></li>
    <li><a href="#s5"><span class="n">05</span><strong>심판에게 무엇을 보내나</strong><span class="d">평가 프롬프트 예시 2개 — 단일 질문 · 10턴 대화</span></a></li>
    <li><a href="#s6"><span class="n">06</span><strong>326개를 평가한 결과</strong><span class="d">모델별 PASS·FAIL·보류 / 326, 어디서 틀렸나, 보류는 왜 생겼나, 기준별 FAIL 률</span></a></li>
    <li><a href="#s7"><span class="n">07</span><strong>최소 78 로 줄이면 무엇이 보이나</strong><span class="d">정밀 326 과 같은 판정으로 비교 — FAIL 포착 80%, 순위 같음</span></a></li>
    <li><a href="#s8"><span class="n">08</span><strong>질문 수는 왜 78 과 326 인가</strong><span class="d">FAIL 포착률 · 새 세트에서 순위가 맞을 확률 · 고르는 규칙을 바꾼 이유</span></a></li>
    <li><a href="#s9"><span class="n">09</span><strong>비용과 토큰</strong><span class="d">단계 × 모델 — 이틀 약 $160, 토큰 약 2억 7천만</span></a></li>
  </ol>
</section>
"""

s1 = f"""
<section class="slide" id="s1">
  {head("01", "평가기 흐름 — 생성 → 수집 → 평가 → 판정", "기준은 72개(CODE 28 + 의미 44)입니다. CODE 는 프로그램이, 의미는 두 심판(GPT-6 Luna · Jev)이 병행 판정합니다.")}
  <figure class="fig">{flow}</figure>
</section>
"""

gen_struct = [("①", "단일 질문", "40개씩 묶음", "주제·자료·조건을 설계해 JSON 으로 받음. 자료(표·JSON·공지문)는 질문 안에 직접 넣음"),
              ("②", "10턴 대화", "한 호출에 4개", "앞 턴 조건 유지·조건 변경·지시어·되돌리기 등 J6 항목이 고르게 걸리게 턴을 설계"),
              ("③", "긴 문서 질문", "문서 1개 + 질문 10개", "272K 토큰 이상 문서를 프로그램이 만들고, 문서 속 사실 문장으로만 답이 정해지는 질문"),
              ("④", "k 고치기", "범위 밖 질문을 하나씩", "원래 문단은 안 바꾸고 조건 문장만 더하거나 빼서 6~10 안으로. 코드가 다시 세어 검증")]
s2 = f"""
<section class="slide" id="s2">
  {head("02", "질문생성 프롬프트 — 규칙 한 벌, 네 단계", "한 번에 326개를 받을 수 없어 단계·묶음으로 나눕니다. 모든 단계가 같은 점검 목록 72개와 같은 k 규칙을 봅니다. 공개 질문 세트는 쓰지 않습니다.")}
  <div class="steps4">{"".join(f'<div><span class="k">{a} {b}</span><b>{E(c)}</b><span>{E(d)}</span></div>' for a, b, c, d in gen_struct)}</div>
  <div class="defbox"><h3>질문당 기준 수(k) 규칙 — 요점</h3>
    <p>① 단일 질문마다 <b>k 6~10</b>. k = 그 질문에 실제로 걸리는 변동 기준 수(항상 걸리는 10개·답변 따라 걸리는 8개는 안 셈). 코드가 다시 셉니다.</p>
    <p>② <b>범위 밖 단일 질문은 허용하지 않음</b>(상한 없음). 예외는 빈 출력 요구 질문뿐 — 조건을 붙이면 모순.</p>
    <p>③ 범위 안에서는 k 6·7·8·9·10 이 고르게. 기본 과제만으로 자동으로 붙는 기준(자료 값 옮기기 → J5-2.a 등)까지 세어 맞춥니다.</p></div>
  {details("k 규칙 원문 (모든 단계에 들어가는 문장)", k_rule_text)}
  <div class="block"><h3>프롬프트 원문 전체 (question-gen-v2.2-kvar)</h3>
    <p class="lead">펼치면 단계마다 코드가 실제로 보내는 글이 그대로 나옵니다. 점검 목록은 ①·④ 뒤에 붙는 기준 72개의 설명과 인식 규칙입니다.</p>
    {"".join(details(t, b) for t, b in sections)}</div>
</section>
"""

# B01 예시 — 실험 세트에서 같은 기본 질문의 k 2·8·12 변형 (조건 문장만 다름)
_exp_groups = json.load(open(ROOT / "참고자료/질문세트_실험300/questions.json", encoding="utf-8"))
_b01 = {int(x["k_actual"]): x["turns"][0]["question"] for x in _exp_groups if x.get("role") == "변형" and x.get("base_id") == "B01"}
_memo = _b01[2].split("\n\n", 1)[1].strip()
_ex = {k: _b01[k].split("\n\n", 1)[0].strip() for k in (2, 8, 12)}

s3 = f"""
<section class="slide" id="s3">
  {head("03", "k 범위 6~10 — 어떻게 시험했고, 왜 이렇게 정했나", "k = 질문 하나에 걸리는 기준 수. 조건 문장을 더할수록 k 가 올라갑니다.")}

  <div class="block"><h3>① 어떻게 시험했나</h3>
    <div class="steps4">
      <div><span class="k">1</span><b>기본 질문 18개</b><span>주제·자료가 다른 질문 18개를 고름</span></div>
      <div><span class="k">2</span><b>조건 수만 바꿔 k 1~16 변형</b><span>같은 질문에 조건 문장을 더하거나 빼서 k 마다 한 개씩 → 255개</span></div>
      <div><span class="k">3</span><b>세 모델에 던짐</b><span>GPT · Claude · Gemini (10-18). AIONA 는 지금 수집 중</span></div>
      <div><span class="k">4</span><b>질문마다 판정 분류</b><span>Jev + GPT-6 Luna 평가 → 모델들이 <b>모두 PASS</b> / <b>갈림</b> / <b>모두 FAIL</b></span></div>
    </div>
    <div class="exq-block">
      <span class="lab">같은 메모</span><div class="q muted">{E(_memo)}</div>
      <span class="lab">k = 2</span><div class="q">{E(_ex[2])}</div>
      <span class="lab">k = 8</span><div class="q">{E(_ex[8])}</div>
      <span class="lab">k = 12</span><div class="q">{E(_ex[12])}</div>
    </div>
  </div>

  <div class="block"><h3>② 결과 — k 별로 모델 판정이 어떻게 나뉘나</h3>
    <figure class="fig">
      <div class="fig-head"><h3>기본 질문 18개 × k 1~16 — 한 칸이 질문 하나</h3>
        <div class="legend"><span><i style="background:var(--ok);opacity:.7"></i>세 모델 모두 PASS</span><span><i style="background:var(--accent)"></i>갈림</span><span><i style="background:var(--risk);opacity:.7"></i>세 모델 모두 FAIL</span><span><i style="background:var(--line)"></i>변형 없음·판정 없음</span></div></div>
      <div class="chart-scroll">{grid_svg()}</div>
      <p class="muted">같은 질문인데 조건이 늘수록 왼쪽 초록(다 맞힘) → 가운데 파랑(갈림) → 오른쪽 빨강(다 틀림)으로 옮겨 갑니다. 아래 막대는 이 격자를 k 별로 세로로 합친 것입니다.</p>
    </figure>
    <figure class="fig">
      <div class="fig-head"><h3>k 별 질문 판정 분포 (GPT · Claude · Gemini)</h3>
        <div class="legend"><span><i style="background:var(--ok);opacity:.55"></i>모두 PASS</span><span><i style="background:var(--accent)"></i>갈림 — 모델마다 판정이 다름</span><span><i style="background:var(--risk);opacity:.6"></i>모두 FAIL</span></div></div>
      <div class="chart-scroll">{k_chart(kexp)}</div>
    </figure>
    {gist("<b>k 1~5</b>: 모두 PASS 가 50~80% — 다 맞혀서 모델을 가르지 못함",
          "<b>k 6~10</b>: 갈림이 33~50% 로 가장 높음 — 모델 차이가 드러남",
          "<b>k 12 이상</b>: 모두 FAIL 이 70% 넘음 — 다 틀려서 역시 가르지 못함")}
  </div>

  <div class="concl"><span class="concl-tag">③ 그래서</span>
    <h3>단일 질문은 모두 k 6~10 으로 만든다</h3>
    <div class="concl-cols">
      <div><b class="keep">근거</b><ul><li>갈리는 질문만 "어느 모델이 어디서 틀리나"를 보여 준다</li><li>6 은 갈림 33% 로 경계지만 자연스러운 질문을 만들 수 있어 하한, 11 부터 내려가 10 이 상한</li></ul></div>
      <div><b class="fix">적용</b><ul><li>범위 밖은 상한 없이 전부 고치거나 다시 만든다 (326 세트: 범위 밖 0)</li><li>예외는 빈 출력 요구 질문뿐 — 조건을 붙이면 모순</li></ul></div>
      <div><b class="wait">AIONA 확인</b><ul><li>326 평가(k 6~10)에 AIONA 를 넣어 세어도 갈림 36~56% 로 같은 모양</li><li>실험 300개를 AIONA 로 수집 중 → 끝나면 위 그래프를 네 모델로 교체</li></ul></div>
    </div></div>

  <details class="src"><summary><span>k 별 수치 표 · AIONA 포함 k 6~10 · 한계</span><span class="muted">펼쳐 보기</span></summary>
    <div style="padding:14px 16px;display:flex;flex-direction:column;gap:14px">
      <div class="cmpt ktab"><table><caption>실험 (세 모델) <span>갈림 = 세 모델의 판정이 같지 않은 질문 비율</span></caption>
        <thead><tr><th>k</th><th>질문 수</th><th>모두 PASS</th><th>갈림</th><th>모두 FAIL</th></tr></thead><tbody>{k_table_rows}</tbody></table></div>
      <div class="cmpt ktab"><table><caption>{E(ksplit_title)} <span>{E(ksplit_lead)}</span></caption>
        <thead><tr><th>k</th><th>질문</th><th>모두 PASS</th><th>갈림</th><th>모두 FAIL</th></tr></thead><tbody>{ksplit_rows}</tbody></table></div>
      <p class="muted">"다 PASS 면 기준이 헐렁한 것 아닌가" — 이 실험에서 다 PASS 는 같은 기준인데 조건이 적어서(질문이 쉬워서)입니다. 기준 자체가 느슨한지는 06장 끝 '기준별 FAIL 률'로 따로 봅니다. 한계: 기본 질문 18개·확인 못 한 사실(J5)을 FAIL 로 센 값이며, 보류로 세도 구간 모양은 같습니다.</p>
    </div></details>
</section>
"""

s4 = f"""
<section class="slide" id="s4">
  {head("04", "평가 기준 72개 — CODE 28 + 의미 44", "CODE 는 \"질문이 요구한 형식·개수·계산을 지켰나\"를 규칙으로 셉니다. 의미는 \"요청을 수행했나, 맥락에 맞나, 앞뒤가 맞나, 빠짐없나, 사실이 맞나, 대화·긴 문서·첨부를 제대로 다뤘나\"를 심판이 판정합니다.")}
  <div class="kpis3">
    <div><span class="k">질문 조건으로 걸리는 기준</span><b>64<small>개</small></b><span>질문에 요구가 있어야 적용 → "어떤 질문을 던질까"를 셀 때 쓰는 수</span></div>
    <div><span class="k">항상 적용</span><b>10<small>개</small></b><span>CODE 3(빈 응답·문자 규칙·외국 문자) + 의미 7(J1 전부·J3-1) — 모든 답변에</span></div>
    <div><span class="k">답변 따라 적용</span><b>8<small>개</small></b><span>각주·출처 링크·닫는 태그 등 7 + J5-3.a — 모델이 그렇게 답했을 때만</span></div>
  </div>
  <div class="block"><h3>CODE 평가 28개</h3>
    <div class="cmpt ctab"><table><thead><tr><th>검사</th><th>이름</th><th>예시 질문</th><th>적용</th></tr></thead><tbody>{"".join(code_rows)}</tbody></table></div></div>
  <div class="block"><h3>의미 평가 44개 (J1~J8)</h3>
    <div class="cmpt ctab"><table><thead><tr><th>항목</th><th>무엇을 보나</th><th>적용 조건</th><th>적용</th></tr></thead><tbody>{"".join(sem_rows)}</tbody></table></div></div>
</section>
"""

s5 = f"""
<section class="slide" id="s5">
  {head("05", "평가 프롬프트 — 질문 · 답변 · 그 질문의 항목만", "CODE 평가는 프롬프트가 없습니다(프로그램). 의미 평가는 질문마다 적용 항목을 미리 뽑아(질문별 해석 프롬프트) 그 항목만 묻습니다. 아래는 질문별 평가 프롬프트 326개 중 2개입니다.")}
  <div class="steps4">
    <div><span class="k">보내는 것</span><b>원문 그대로</b><span>질문·답변·앞 대화·참조 자료·첨부를 줄이지 않고 보냅니다. 어느 모델의 답인지는 숨깁니다.</span></div>
    <div><span class="k">묻는 것</span><b>그 질문의 항목만</b><span>항목마다 PASS / FAIL / 해당 없음 / 확인 못 함. FAIL 은 답변 문장을 따옴표로 인용해야 합니다.</span></div>
    <div><span class="k">멀티턴</span><b>1턴부터 순서대로</b><span>n턴 판정에 1~(n-1)턴 질문·답변을 대화 순서로 넣습니다.</span></div>
    <div><span class="k">두 심판</span><b>같은 입력</b><span>Luna 는 J1~J8, Jev 는 J1~J4. Jev 가 받을 수 없는 긴 입력(96턴)은 Luna 만 판정.</span></div>
  </div>
  <div class="block"><h3>예시 1 — 단일 질문 Q36 (사실 확인 + 형식 조건)</h3>
    <p class="lead">심판에게 가는 글의 핵심만 블록으로 보였습니다. 앞에 붙는 원칙·판정 방법·출력 형식은 326개가 모두 같아 접어 두었습니다.</p>
    {q36_block()}
    {details("Q36 평가 프롬프트 전문", q36, note="원칙 · 판정 방법 · 출력 형식 포함")}</div>
  <div class="block"><h3>예시 2 — 10턴 대화 Q12</h3>
    <p class="lead">턴마다 [질문]·[점검 항목]·[답변] 블록이 이어지고, 2턴부터 J6(앞 턴 조건 유지) 항목이 추가됩니다. 전체 49,688자 중 1턴만 펼쳐 볼 수 있습니다.</p>
    {details("Q12 — 공통 원칙 + 1턴", q12_head.strip() + "\\n\\n" + q12_t1.strip() + "\\n\\n# 2턴 … # 10턴 (같은 구조, J6 항목 추가)", note="10턴 대화")}</div>
</section>
"""

s6 = f"""
<section class="slide" id="s6">
  {head("06", "결과 — PASS : FAIL  " + " · ".join(f"{MODEL_NAME[m].split()[0]} {PF[m][0]:.0f}:{PF[m][1]:.0f}" for m in ("gpt", "aiona", "claude", "gemini")), "326개를 만들어 네 모델 답을 받고 끝까지 평가했습니다(실행 8c61f117e661). 질문 단위(대화 1개 = 1개). 결과는 <b>확정된 질문의 PASS : FAIL 비율</b>과 <b>보류 비율(326 중)</b> 두 숫자로 봅니다." + (" <b>보류 목록을 사람이 정리한 뒤의 숫자입니다.</b>" if GOLD_APPLIED else ""))}
  {gist("<b>PASS : FAIL</b> (확정 질문 중) — " + " · ".join(f"{MODEL_NAME[m].split()[0]} {PF[m][0]:.0f}:{PF[m][1]:.0f}" for m in ("gpt", "aiona", "claude", "gemini")) + ". <b>보류</b> " + " · ".join(f"{MODEL_NAME[m].split()[0]} {PF[m][2]:.0f}%" for m in ("gpt", "aiona", "claude", "gemini")) + " — 사람이 정하면 비율이 바뀝니다.",
        "FAIL 은 CODE 에서는 <b>문장 수·자료 변환</b>, 의미에서는 <b>J5 사실 확인·J6 멀티턴</b>에 몰려 있습니다.",
        "한 번도 FAIL 이 안 난 기준 8개는 깨진 출력을 잡는 안전망 검사 — 느슨해서가 아닙니다 (장 끝 표).")}
  <div class="three">
    <div class="card"><div class="card-head"><b>① 질문 생성</b><span>10-18 · GPT-6 Luna 67회 호출</span></div><div class="card-body">
      <p>326개 = 단일 284 · 대화 32(10턴 15 · 11턴 1 · 2턴 16) · 긴 문서 10 (487턴). 첨부 28개.</p>
      <p>생성 직후 k 범위 밖 175개 → 하나씩 고치기 3회차 + 손질 → <b>범위 밖 0</b>.</p></div></div>
    <div class="card"><div class="card-head"><b>② 답변 수집</b><span>10-18 밤 ~ 10-19 새벽</span></div><div class="card-body">
      <p>이번은 GPT·Claude·Gemini 를 API 로 487/487, <b>AIONA 웹브라우저 479/487</b>(빈 출력 요구 8개는 오류 화면 → 보류). <b>다음부터는 네 모델 모두 웹브라우저</b>로 받습니다(사용자 결정 10-19).</p>
      <p>Claude 출력 한도 16,000 으로 · 429 는 이어서 수집. AIONA 복사 버튼 문제를 고쳐 108턴 재복사.</p></div></div>
    <div class="card"><div class="card-head"><b>③ 평가</b><span>10-19 02:20 ~ 10:32</span></div><div class="card-body">
      <p>CODE 즉시. 의미 평가 1,938판정 + 재판정 — 판정 하나에 20초~15분(웹 검색·긴 대화).</p>
      <p>출력 한도에 걸린 21턴 + 시간 초과 2턴 → 한도 없음·6,000초로 재평가 → <b>판정 오류 0</b>.</p></div></div>
  </div>
  <div class="card"><div class="card-head"><b>모델별 PASS : FAIL 비율과 보류 비율</b><span>막대 = 326 중 PASS·FAIL·보류 질문 수 · 오른쪽 = 확정 질문(PASS+FAIL) 중 비율과 보류 비율 · CODE 28 + 의미 44 (Jev + GPT-6 Luna 병행)</span></div>
    <div class="card-body">{res_rows}
      <div class="fs-legend"><span><i style="background:var(--ok)"></i>PASS</span><span><i style="background:var(--risk);opacity:.75"></i>FAIL (기준 하나라도)</span><span><i style="background:repeating-linear-gradient(135deg,var(--warn) 0 6px,var(--warn-soft) 6px 10px)"></i>보류</span></div></div></div>
  <div class="block">
    <div class="cmpt rtab"><table><caption>어디서 틀렸나 <span>FAIL 이 하나라도 있는 질문 수와 많이 걸린 기준</span></caption>
      <thead><tr><th>모델</th><th>CODE FAIL<br>질문 수</th><th>많이 걸린 검사 (질문 수)</th><th>의미 FAIL<br>질문 수</th><th>많이 걸린 항목 (턴 수)</th></tr></thead><tbody>{fail_rows}</tbody></table></div>
    <div class="cmpt rtab"><table><caption>보류는 왜 생겼나 <span>턴 단위 항목 수</span></caption>
      <thead><tr><th>사유</th><th>AIONA</th><th>GPT</th><th>Claude</th><th>Gemini</th><th>설명</th></tr></thead><tbody>{hold_rows}</tbody></table></div>
  </div>
  <div class="cmpt rtab"><table><caption>기준별 FAIL 률 — 많이 틀린 기준 12개 <span>네 모델 턴 단위 판정 합. 의미 기준은 J 묶음 단위로 기록됨(항목 44개 단위 FAIL 률은 다음 평가에서)</span></caption>
    <thead><tr><th>기준</th><th>판정 수</th><th>FAIL (률)</th><th>모델별 FAIL</th></tr></thead><tbody>{cf_rows}</tbody></table>
    <div class="qnote"><b>한 번도 FAIL 이 안 난 기준 {len(CF_ZERO)}개</b> (괄호 = 판정 수): {cf_zero_line}. 이 중 6개는 깨진 문자·펜스 짝·JSON 구문·인용/각주 연결·등식처럼 <b>망가진 출력을 잡는 안전망</b>이라 0 이 자연스럽고, <code>1-2.code_fence_min</code>·<code>3-3.unique_values</code> 는 규칙을 확인해 보니 실제로 세고 있음(펜스 2개 이상, 중복 0개) — 요구가 쉬워 모델이 다 지킨 것. 느슨한 기준은 아니지만 다음 세트에서 더 어렵게 요구해 볼 대상.</div></div>
</section>
"""

s7 = f"""
<section class="slide" id="s7">
  {head("07", "최소 78 vs 정밀 326 — 같은 판정으로 비교", "최소 78 은 정밀 326 에서 골라낸 부분집합(세트 qs-c5d8a1624770)이라, 따로 수집·평가하지 않고 같은 판정을 그대로 셀 수 있습니다.")}
  {gist("고르는 규칙: <b>아직 덜 채운 기준을 가장 많이 채우는 질문부터</b>, 기준마다 10번 이상이 될 때까지 (집합 덮기 표준 탐욕법, 팀원 검산으로 교체).",
        "78개에서 전체 FAIL 조합 87개 중 <b>70개(80%)</b>가 보이고 " + ("<b>순위도 정밀과 같습니다</b>" if RANK78 == RANK else "<b>순위는 정밀과 다릅니다</b>") + ". PASS:FAIL 은 어려운 질문 위주라 정밀보다 FAIL 쪽으로 기웁니다.",
        "7개(기준마다 1번)는 기준이 다 걸리는지 점검하는 용도 — FAIL 은 24%만 보여 분석에는 부족합니다.")}
  <figure class="fig">
    <div class="fig-head"><h3>모델별 PASS · FAIL · 보류 — 정밀 326 과 최소 78</h3>
      <div class="legend"><span><i style="background:var(--ok)"></i>PASS</span><span><i style="background:var(--risk);opacity:.75"></i>FAIL</span><span><i style="background:repeating-linear-gradient(135deg,var(--warn) 0 6px,var(--warn-soft) 6px 10px)"></i>보류</span></div></div>
    <div class="chart-scroll">{cmp_chart()}</div>
  </figure>
  <div class="two">
    <div class="cmpt rtab"><table><caption>기준 묶음별 FAIL 질문 수 — 최소 78 안 / 정밀 전체 <span>최소 78 에서도 거의 모든 묶음에 FAIL 이 보여 "어디서 틀리나"를 같은 모양으로 읽을 수 있음</span></caption>
      <thead><tr><th>기준 묶음</th><th>GPT</th><th>AIONA</th><th>Claude</th><th>Gemini</th></tr></thead><tbody>{fbg_rows}</tbody></table></div>
    <div class="cmpt rtab"><table><caption>목록 크기별 비교 <span>PASS:FAIL 순서 AIONA · GPT · Claude · Gemini (%)</span></caption>
      <thead><tr><th>목록</th><th>질문 수</th><th>용도</th><th>PASS:FAIL (확정 중)</th><th>순위</th><th>정밀과</th><th>FAIL 조합</th></tr></thead><tbody>{min_rows}</tbody></table></div>
  </div>
  <figure class="fig"><div class="fig-head"><h3>기울기 — 같은 순서로 10개씩 늘릴 때 모델별 통과율</h3><span class="muted">78개에서 기준 64개 모두 10번 이상. 순위·통과율이 정밀과 같아지는 지점 <b>273개</b>.</span></div>
    <div class="gradwrap">{gradient_svg}</div></figure>
</section>
"""

s8 = f"""
<section class="slide" id="s8">
  {head("08", "질문 수 근거 — 왜 78, 왜 326", "평가가 끝난 기록만으로(AI 호출 없이) 셌습니다. 다음 세트는 내용이 달라지므로 특정 질문이 아니라 \"몇 개를 어떤 규칙으로\"가 답입니다.")}
  <div class="concl"><span class="concl-tag">결정</span>
    <h3>정밀 326 (순위·FAIL 전부) / 최소 78 (기준마다 10번↑, FAIL 조합 80%) / 커버 점검 7</h3>
    <div class="concl-cols">
      <div><b class="keep">왜 326 인가</b><ul><li>GPT 와 AIONA 는 통과율 차이가 <b>7.4pp</b>. 이 순서를 95% 확률로 맞히려면 <b>250개</b>가 필요합니다.</li><li>326 이면 99%. 기준마다 10번 이상이라 기준별 FAIL 도 셀 수 있습니다.</li><li>Claude·Gemini 는 차이 3.4pp — 326 으로도 못 가릅니다. "같은 수준"으로 보고합니다.</li></ul></div>
      <div><b class="fix">왜 78 인가</b><ul><li>7개·21개는 전체 FAIL 조합의 24~49%만 보입니다.</li><li><b>78개(기준마다 10번)에서 80%</b>. 155개로 늘려도 87% — 그 뒤는 늘어나는 폭이 작습니다.</li><li>78 은 이 세트의 결과값입니다. 새 세트에서는 75~85개쯤 — 규칙(10번↑)을 고정합니다.</li></ul></div>
      <div><b class="wait">왜 규칙을 바꿨나</b><ul><li>이전 규칙(기준 많은 순)은 이미 채운 기준까지 세어 10턴 대화만 연달아 골랐습니다 → 같은 목표에 110개.</li><li>바꾼 규칙(덜 채운 기준을 가장 많이 채우는 질문부터)은 세트 6개 × 목표 5개 모두에서 더 적었습니다 (팀원 검산).</li></ul></div>
    </div></div>
  <figure class="fig">
    <div class="fig-head"><h3>시험 결과</h3>
      <div class="legend"><span><i style="background:var(--k20)"></i>표준 탐욕 목록</span><span><i style="background:var(--m-gpt)"></i>무작위 평균 / AIONA>Claude</span><span><i style="background:var(--m-aiona)"></i>GPT>AIONA</span><span><i style="background:var(--m-gemini)"></i>Claude>Gemini</span></div></div>
    <div class="chart-scroll">{ss_chart()}</div>
  </figure>
  <div class="two">
    <div class="cmpt ktab"><table><caption>① 목록이 FAIL 을 얼마나 잡나 <span>(모델, 기준) FAIL 조합 87개 · FAIL 사건 586건 · 무작위는 같은 수를 300번 뽑은 평균</span></caption>
      <thead><tr><th>N</th><th>목표</th><th>FAIL 조합</th><th>FAIL 사건</th><th>무작위(조합)</th></tr></thead><tbody>{fc_rows}</tbody></table></div>
    <div class="cmpt ktab"><table><caption>② 새 세트 N개일 때 순서가 맞을 확률 <span>같은 분포에서 N개를 새로 뽑는 복원 추출 4,000번</span></caption>
      <thead><tr><th>N</th><th>전체 순위</th><th>GPT>AIONA</th><th>AIONA>Claude</th><th>Claude>Gemini</th></tr></thead><tbody>{bs_rows}</tbody></table></div>
  </div>
  <p class="muted">두 모델 통과율 차이 d 를 95% 로 가르는 데 필요한 수 ≈ (1.645·√(2p(1−p)) / d)² — GPT·AIONA 247개, AIONA·Claude 116개, Claude·Gemini 1,125개. 도구: tools/sample_size_from_run.py · tools/min_eval_list.py · tools/question_gradient.py (AI 호출 없음).</p>
</section>
"""

s9 = f"""
<section class="slide" id="s9">
  {head("09", "비용 — 이틀 약 $160", "실행 기록의 토큰 사용량에 우리 단가표(code_eval/cost_reporting.py)를 곱한 추정입니다. 실제 청구서와 조금 다를 수 있습니다. 토큰은 입력 / 출력(추론 포함).")}
  {gist("의미 평가(GPT-6 Luna)가 <b>약 $80</b> — 웹 검색 2,207회($22)와 앞 대화를 매번 다시 읽은 입력이 큼.",
        "Claude 답변 수집이 <b>$54</b> — 입력 단가가 Luna 의 20배이고 긴 문서를 52.5만 토큰으로 셈.",
        "줄일 곳: 답변 수집을 <b>네 모델 모두 웹브라우저</b>로(API 비용 $66 → 0, 사용자 결정) · 입력을 대화 순서로(캐시, 반영) · 웹 검색 상한 · Jev 동시 호출(반영).")}
  <div class="cmpt rtab"><table><caption>단계 × 모델 <span>326 세트 기준, 10-18 실험은 따로 표시</span></caption>
    <thead><tr><th>단계</th><th>모델</th><th>무엇</th><th>토큰 입력 / 출력</th><th>추정 비용</th></tr></thead><tbody>{cost_rows}</tbody></table></div>
  <div class="cmpt rtab"><table><caption>청구처별 합계</caption>
    <thead><tr><th>청구처</th><th>쓴 곳</th><th>토큰</th><th>추정 비용</th></tr></thead><tbody>{cost_sum_rows}</tbody></table></div>
</section>
"""

script = old[old.index("<script>"):old.index("</script>") + len("</script>")].replace(
    "var ids = ['s0', 's1', 's2', 's3', 's4', 's5', 's6', 's7'];", "var ids = ['s0', 's1', 's2', 's3', 's4', 's5', 's6', 's7', 's8', 's9'];")
page = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>ModelGrade 진행 보고</title>
<meta name="description" content="2026-10-19 — 326개 질문 생성, 네 모델 답변 수집, CODE + 의미 평가 결과와 비용">
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
{s1}
{s2}
{s3}
{s4}
{s5}
{s6}
{s7}
{s8}
{s9}
</main>
{script}
</body>
</html>
"""
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(page, encoding="utf-8")
shutil.copy2(OUT, COPY)
print("저장:", OUT, f"{len(page):,}자", "| 복사:", COPY)
