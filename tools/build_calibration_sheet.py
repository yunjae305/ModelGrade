# -*- coding: utf-8 -*-
"""사람 표시용 표 — 칸마다 질문 · 답변 원본과 AI 판정을 보여 주고, 사람이 그 칸의 결과를 PASS · FAIL · 에러 케이스 중에서 직접 고른다
(사용자 10-23 "표시용 표도 만들어줘" · 10-26 "질문/답변 원본이 보이게 · 평가결과가 무엇인지 · 왜 골랐는지 · 심판이 맞다 틀리다가 아닌
우리가 보았을 때 결과를 pass/fail/error case 로 선택").

칸 = (모델, 질문, 턴, 항목). 표시는 브라우저에 저장되고 '내보내기'로 JSON(표시결과_<이름>.json)을 받는다 — 사람 결과와 AI 판정을 견줘
기준마다 거짓 FAIL · 거짓 PASS 를 세고, 흔들리는 칸 · 기준 다듬기 루프의 정답으로 쓴다.

    python -X utf8 tools/build_calibration_sheet.py --run 50ca3ccccace --out v4-r2 [--fail 60 --pass 40 --seed 1]
→ 참고자료/분석/<오늘>-엄격함점검/표시용_표.html · 표본.json (판정 폴더 하나에서 FAIL · PASS 칸을 유형 · 기준별로 고루 뽑음)
    python -X utf8 tools/build_calibration_sheet.py --run 50ca3ccccace --pick-file 고를칸.json --dest <폴더>
→ 정해 둔 칸만 — 고를칸.json: [{"provider", "question", "turn", "item", "kind", "why", "results": [{"out", "label"}, ...]}]
  results 는 견줄 AI 판정 판(판정 폴더 · 이름) — 없으면 "out" 하나 (할일 9번, 10-26)
"""
from __future__ import annotations

import argparse
import html
import json
import random
import re
import sys
from collections import Counter
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KO = {"PASS": "PASS", "FAIL": "FAIL", "UNVERIFIED": "보류", "NOT_APPLICABLE": "해당 없음"}
TAG = {"PASS": "t-pass", "FAIL": "t-fail", "UNVERIFIED": "t-hold", "NOT_APPLICABLE": "t-na"}
#: 사람이 고르는 결과 — 에러 케이스 = 답이 오류 문구 · 빈 답 · 무응답 · '언어 모델이라 할 수 없습니다' 같은 거절이라 이 기준으로 볼 수 없음
MARKS = ("PASS", "FAIL", "에러 케이스")
PROVIDER = {"aiona": "AIONA", "claude": "Claude", "gemini": "Gemini", "gpt": "GPT"}
ALL_DECISIONS = ("PASS", "FAIL", "UNVERIFIED", "NOT_APPLICABLE")


def request_of(question: str) -> str:
    """붙인 자료를 뺀 요청 문장."""
    return str(question or "").split("[자료 끝]")[-1].strip()


def attachments_of(row: Dict[str, Any]) -> List[Tuple[str, str]]:
    out = []
    for a in row.get("attachments") or []:
        if isinstance(a, dict):
            out.append((str(a.get("name") or "첨부"), str(a.get("content") or "")))
    return out


@lru_cache(maxsize=4)
def _context(run_id: str, set_dir: str) -> Dict[str, Any]:
    """수집 답변 · 기준 이름 · 질문 · 턴별 확인 목록 — 판정 폴더가 여럿이어도 한 번만 읽는다."""
    from tools import report_html as R
    sd = Path(set_dir)
    run = json.loads((ROOT / "data" / "comparison_runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    plan_path = sd / "multiturn_v3" / "turn_checklists.json"
    return {"answers": {(p, r["case_id"]): r for p, rows in run["datasets"].items() for r in rows},
            "names": R.criteria_table(sd),
            "qs": {int(q["id"]): q for q in json.loads((sd / "questions.json").read_text(encoding="utf-8"))},
            "plan": json.loads(plan_path.read_text(encoding="utf-8")).get("questions", {}) if plan_path.exists() else {}}


def _row(ctx: Dict[str, Any], p: str, q: int, tn: int, multi: bool) -> Dict[str, Any]:
    a = ctx["answers"]
    return a.get((p, f"Q{q}_T{tn}")) or (a.get((p, f"Q{q}")) if not multi else None) or {}


def load_cells(run_id: str, out: str, set_dir: Path, keep: tuple = ("PASS", "FAIL")) -> List[Dict[str, Any]]:
    ctx = _context(run_id, str(set_dir))
    last: Dict[tuple, Dict[str, Any]] = {}
    path = ROOT / "data" / "judge_runs" / run_id / out / "results.jsonl"
    if not path.exists():
        return []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            x = json.loads(line)
            last[(x["provider"], int(x["question"]))] = x
    cells = []
    for (p, q), v in sorted(last.items()):
        if v.get("status") != "judged":
            continue
        multi = int(v.get("turns") or 1) > 1
        flow = (ctx["qs"].get(q) or {}).get("flow") or []
        for tn_s, t in (v.get("turn_results") or {}).items():
            tn = int(tn_s)
            row = _row(ctx, p, q, tn, multi)
            facts = ((ctx["plan"].get(str(q)) or {}).get("turns") or [{}] * 20)[tn - 1] if multi else {}
            prev = [(k, request_of(_row(ctx, p, q, k, True).get("question")), _row(ctx, p, q, k, True).get("answer") or "") for k in range(1, tn)] if multi else []
            for iid, it in (t.get("items") or {}).items():
                if it.get("decision") not in keep:
                    continue
                full = str((ctx["names"].get(iid) or {}).get("name") or "")
                name, _, desc = full.partition(" — ")
                cells.append({"type": "멀티턴" if multi else "싱글", "question": q, "provider": p, "turn": tn,
                              "flow": flow[tn - 1] if multi and tn - 1 < len(flow) else "", "situation": facts.get("situation") or "",
                              "item": iid, "name": name, "desc": desc, "decision": it["decision"], "how": it.get("how") or "",
                              "reason": it.get("reason") or "", "answer_quote": it.get("answer_quote") or "", "question_quote": it.get("question_quote") or "",
                              "codex": it.get("codex"), "question_text": str(row.get("question") or ""), "request": request_of(row.get("question")),
                              "attachments": attachments_of(row), "answer": row.get("answer") or "",
                              "facts": facts.get("checks") or [], "decided": facts.get("decided") or [], "prev": prev})
    return cells


def as_result(c: Dict[str, Any], label: str, out: str) -> Dict[str, Any]:
    return {"label": label, "out": out, "decision": c["decision"], "reason": c["reason"], "answer_quote": c["answer_quote"], "how": c["how"]}


def sample(cells: List[Dict[str, Any]], n_fail: int, n_pass: int, seed: int) -> List[Dict[str, Any]]:
    """층화 표본 — FAIL 은 멀티턴 3 : 싱글 1, 기준마다 상한(멀티턴 8 · 싱글 4)으로 고루; PASS 는 Codex 가 본 칸 반 · 1차 심판만 본 칸 반."""
    rnd = random.Random(seed)
    out: List[Dict[str, Any]] = []

    def pick(pool: List[Dict[str, Any]], n: int, cap: int) -> List[Dict[str, Any]]:
        rnd.shuffle(pool)
        by_item: Counter = Counter()
        got = []
        for c in pool:
            if by_item[c["item"]] >= cap:
                continue
            got.append(c); by_item[c["item"]] += 1
            if len(got) >= n:
                break
        if len(got) < n:   # 상한 때문에 모자라면 나머지는 그냥 채움
            rest = [c for c in pool if c not in got]
            got += rest[:n - len(got)]
        return got
    fails_m = [c for c in cells if c["decision"] == "FAIL" and c["type"] == "멀티턴"]
    fails_s = [c for c in cells if c["decision"] == "FAIL" and c["type"] == "싱글"]
    n_fs = min(len(fails_s), max(1, n_fail // 4)); n_fm = n_fail - n_fs
    out += pick(fails_m, n_fm, 8) + pick(fails_s, n_fs, 4)
    pass_cx = [c for c in cells if c["decision"] == "PASS" and c.get("codex")]
    pass_1 = [c for c in cells if c["decision"] == "PASS" and not c.get("codex")]
    half = n_pass // 2
    out += pick(pass_cx, min(half, len(pass_cx)), 5) + pick(pass_1, n_pass - min(half, len(pass_cx)), 5)
    rnd.shuffle(out)
    for i, c in enumerate(out, 1):
        c["no"] = i
    return out


# --------------------------------------------------------------------------------------------------------------------- 화면

def quote_parts(quote: str) -> List[str]:
    """AI 가 근거로 든 답변 인용을 찾을 조각으로 — '…' · 줄바꿈 · ' / ' 로 나누고 6글자 이상만."""
    parts = re.split(r"\s*(?:…|\.\.\.|\n| / )\s*", str(quote or ""))
    return [p.strip(" \"'“”‘’") for p in parts if len(p.strip(" \"'“”‘’")) >= 6]


#: 인용을 찾을 때 건너뛰는 글자 — 띄어쓰기 · 줄바꿈 · 마크다운 기호(AI 가 인용하며 빼거나 바꾸는 것)
_SKIP = r"[\s*|`#_>\-]*"


def _norm(s: str) -> str:
    return re.sub(_SKIP, "", s)


def find_span(text: str, part: str) -> Optional[Tuple[int, int]]:
    """답변 원문에서 인용 조각의 위치 — 그대로 없으면 띄어쓰기 · 마크다운 기호를 무시하고 찾는다."""
    i = text.find(part)
    if i >= 0:
        return i, i + len(part)
    n = _norm(part)
    if len(n) < 6:
        return None
    m = re.search(_SKIP.join(re.escape(ch) for ch in n), text)
    return (m.start(), m.end()) if m else None


def highlight(text: str, quotes: List[str]) -> Tuple[str, set]:
    """답변 원문에서 인용 조각을 모두 찾은 뒤 한 번에 <mark> 로 감싼다(겹치면 합침) — 돌려주는 값: (HTML, 찾은 조각)."""
    spans, found = [], set()
    for q in quotes:
        for part in quote_parts(q):
            sp = find_span(text, part)
            if sp:
                spans.append(sp)
                found.add(part)
    merged: List[List[int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out, pos = [], 0
    for s, e in merged:
        out.append(html.escape(text[pos:s]) + "<mark>" + html.escape(text[s:e]) + "</mark>")
        pos = e
    out.append(html.escape(text[pos:]))
    return "".join(out), found


CSS = """
:root { --bg:#f2f4f7; --surface:#fff; --ink:#111b26; --muted:#566272; --line:#d5dce5; --accent:#1e5d96; --accent-soft:#e3ecf6; --ok:#1d7748; --ok-soft:#e1f1e7;
  --risk:#b3291f; --risk-soft:#fae3e0; --warn:#965a00; --warn-soft:#faeed4; --na:#5b6573; --na-soft:#e9edf2; --hl:#fff1a8;
  --font-body:"IBM Plex Sans KR","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif; --font-num:"IBM Plex Mono",ui-monospace,Menlo,monospace; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#0e141b; --surface:#16202a; --ink:#e5ebf1; --muted:#98a4b3; --line:#2a3644; --accent:#7fb5e3; --accent-soft:#1a2a3a;
  --ok:#6fcf97; --ok-soft:#15301f; --risk:#f08a80; --risk-soft:#3a1c1a; --warn:#e2b25a; --warn-soft:#33270f; --na:#a7b1bd; --na-soft:#222d39; --hl:#5a4b10; color-scheme:dark; } }
:root[data-theme="dark"] { --bg:#0e141b; --surface:#16202a; --ink:#e5ebf1; --muted:#98a4b3; --line:#2a3644; --accent:#7fb5e3; --accent-soft:#1a2a3a;
  --ok:#6fcf97; --ok-soft:#15301f; --risk:#f08a80; --risk-soft:#3a1c1a; --warn:#e2b25a; --warn-soft:#33270f; --na:#a7b1bd; --na-soft:#222d39; --hl:#5a4b10; color-scheme:dark; }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--ink); font-family:var(--font-body); font-size:15px; line-height:1.6; }
.wrap { max-width:1280px; margin:0 auto; padding:24px 16px 80px; } h1 { font-size:1.55rem; margin:0 0 8px; }
.guide { background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:12px 16px; margin:0 0 12px; font-size:.93rem; }
.guide p { margin:4px 0; } .guide .legend span { margin-right:14px; white-space:nowrap; }
.bar { position:sticky; top:0; z-index:5; background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:10px 14px; display:flex; gap:10px 14px; flex-wrap:wrap; align-items:center; margin:0 0 16px; font-size:.92rem; }
.bar button, .bar label.file { font:inherit; border:1px solid var(--accent); background:var(--accent-soft); color:var(--accent); border-radius:8px; padding:5px 12px; cursor:pointer; font-weight:600; }
.bar input[type=text] { font:inherit; border:1px solid var(--line); border-radius:6px; padding:4px 8px; background:var(--surface); color:var(--ink); width:9em; }
.bar .cnt { font-family:var(--font-num); font-weight:600; } .bar .grow { flex:1; }
.cell { background:var(--surface); border:1px solid var(--line); border-left:5px solid var(--line); border-radius:14px; padding:14px 16px; margin:0 0 16px; }
.cell.done { border-left-color:var(--ok); } .cell.hidden { display:none; }
.head { display:flex; flex-wrap:wrap; gap:6px 12px; align-items:baseline; } .head .no { font-family:var(--font-num); font-weight:700; color:var(--accent); font-size:1.05rem; }
.head .where { font-weight:600; } .head .crit { font-weight:700; }
.desc { color:var(--muted); font-size:.9rem; margin:2px 0 8px; }
.why { background:var(--warn-soft); color:var(--ink); border-radius:8px; padding:6px 10px; font-size:.92rem; margin:0 0 8px; } .why b { color:var(--warn); margin-right:6px; }
.tag { display:inline-block; font-size:.78rem; font-weight:700; border-radius:6px; padding:1px 8px; vertical-align:1px; }
.t-fail { background:var(--risk-soft); color:var(--risk); } .t-pass { background:var(--ok-soft); color:var(--ok); } .t-hold { background:var(--warn-soft); color:var(--warn); }
.t-na { background:var(--na-soft); color:var(--na); } .t-type { background:var(--accent-soft); color:var(--accent); }
.results { display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:8px; margin:0 0 10px; }
.res { border:1px solid var(--line); border-radius:10px; padding:8px 10px; font-size:.9rem; } .res .lab { font-weight:700; margin-right:6px; }
.res .quote { color:var(--muted); margin-top:4px; font-size:.86rem; } .res .miss { color:var(--risk); font-weight:600; }
.panes { display:grid; grid-template-columns:1fr 1fr; gap:10px; } @media (max-width: 900px) { .panes { grid-template-columns:1fr; } }
.pane h3 { font-size:.85rem; color:var(--muted); margin:0 0 4px; font-weight:700; }
.pane .req { background:var(--accent-soft); border-radius:8px; padding:6px 10px; margin:0 0 6px; font-size:.92rem; }
pre.text { white-space:pre-wrap; word-break:break-word; font-family:var(--font-body); font-size:.88rem; background:var(--bg); border-radius:8px; padding:10px 12px; max-height:460px; overflow:auto; margin:0; }
pre.text.short { max-height:260px; } mark { background:var(--hl); color:inherit; border-radius:3px; padding:0 1px; }
details { margin:8px 0 0; } summary { cursor:pointer; color:var(--accent); font-size:.9rem; font-weight:600; }
ul.facts { margin:4px 0 0; padding-left:18px; font-size:.88rem; } .prev { margin:8px 0 0; } .prev b { font-size:.86rem; }
.mark { display:flex; flex-wrap:wrap; gap:8px 12px; align-items:center; margin-top:12px; padding-top:10px; border-top:1px dashed var(--line); }
.mark .q { font-weight:700; } .opt { border:1px solid var(--line); border-radius:8px; padding:5px 12px; cursor:pointer; font-weight:600; user-select:none; }
.opt input { margin-right:6px; } .opt.on.pass { background:var(--ok-soft); border-color:var(--ok); color:var(--ok); } .opt.on.fail { background:var(--risk-soft); border-color:var(--risk); color:var(--risk); }
.opt.on.err { background:var(--warn-soft); border-color:var(--warn); color:var(--warn); }
.mark .memo { font:inherit; border:1px solid var(--line); border-radius:6px; padding:6px 8px; background:var(--surface); color:var(--ink); flex:1 1 100%; min-height:2.4em; resize:vertical; overflow:hidden; }
"""

JS = r"""
const KEY = 'mark-' + document.body.dataset.sheet;
let saved = {};
try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { saved = {}; }
function store() { try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) {} }
// 파일에 넣어 둔 표시(preset)와 이 브라우저의 표시 가운데 더 나중에 고친 쪽을 쓴다
let preset = [];
try { preset = JSON.parse(document.getElementById('preset').textContent).rows || []; } catch (e) { preset = []; }
preset.forEach(r => { const s = saved[r.id]; if (r.at && (!s || (s.at || '') < r.at)) saved[r.id] = { mark: r.mark || undefined, memo: r.memo || '', at: r.at }; });
store();
const now = () => new Date().toISOString();
function fit(t) { t.style.height = 'auto'; if (t.scrollHeight) t.style.height = (t.scrollHeight + 2) + 'px'; }
const cells = [...document.querySelectorAll('.cell')];
const who = document.getElementById('who');
who.value = saved._who || '';
who.addEventListener('input', () => { saved._who = who.value; store(); });
const todo = document.getElementById('todo');
function paint(c) {
  const v = saved[c.dataset.id] || {};
  c.querySelectorAll('.opt').forEach(o => { const r = o.querySelector('input'); r.checked = v.mark === r.value; o.classList.toggle('on', r.checked); });
  c.classList.toggle('done', !!v.mark);
}
function refresh() {
  let n = 0;
  cells.forEach(c => { paint(c); const done = !!(saved[c.dataset.id] || {}).mark; if (done) n++; c.classList.toggle('hidden', todo.checked && done); });
  document.getElementById('cnt').textContent = n + ' / ' + cells.length + ' 칸 표시함';
  cells.forEach(c => fit(c.querySelector('.memo')));
}
cells.forEach(c => {
  const id = c.dataset.id;
  c.querySelectorAll('.opt input').forEach(r => r.addEventListener('change', () => { saved[id] = { ...(saved[id] || {}), mark: r.value, at: now() }; store(); refresh(); }));
  const memo = c.querySelector('.memo');
  memo.value = (saved[id] || {}).memo || '';
  memo.addEventListener('input', () => { saved[id] = { ...(saved[id] || {}), memo: memo.value, at: now() }; store(); fit(memo); });
});
todo.addEventListener('change', refresh);
window.addEventListener('resize', () => cells.forEach(c => fit(c.querySelector('.memo'))));
document.getElementById('export').addEventListener('click', () => {
  const rows = cells.map(c => ({ id: c.dataset.id, no: +c.dataset.no, question: +c.dataset.q, provider: c.dataset.p, turn: +c.dataset.t, item: c.dataset.item,
    kind: c.dataset.kind, ai: JSON.parse(c.dataset.ai), mark: (saved[c.dataset.id] || {}).mark || null, memo: (saved[c.dataset.id] || {}).memo || '' }));
  const name = (who.value || '이름없음').trim().replace(/[\\/:*?"<>|\s]+/g, '_');
  const blob = new Blob([JSON.stringify({ sheet: document.body.dataset.sheet, marker: who.value, exported: new Date().toISOString(), rows }, null, 1)], { type: 'application/json' });
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = '표시결과_' + name + '.json'; a.click();
});
document.getElementById('import').addEventListener('change', ev => {
  const f = ev.target.files[0]; if (!f) return;
  f.text().then(t => {
    const data = JSON.parse(t);
    (data.rows || []).forEach(r => { if (r.mark || r.memo) saved[r.id] = { mark: r.mark || undefined, memo: r.memo || '', at: now() }; });
    if (data.marker && !saved._who) { saved._who = data.marker; who.value = data.marker; }
    store(); cells.forEach(c => { c.querySelector('.memo').value = (saved[c.dataset.id] || {}).memo || ''; }); refresh();
  }).catch(() => alert('불러오지 못했습니다 — 내보내기로 받은 JSON 인지 확인해 주세요.'));
});
document.getElementById('clear').addEventListener('click', () => { if (confirm('이 표의 표시를 모두 지울까요?')) { saved = {}; store(); location.reload(); } });
refresh();
"""


def render_cell(c: Dict[str, Any]) -> str:
    e = html.escape
    cid = f"{c['provider']}-{c['question']}-{c['turn']}-{c['item']}"
    results = c.get("results") or [as_result(c, "AI 판정", c.get("out") or "")]
    answer_html, found = highlight(c["answer"], [r.get("answer_quote") or "" for r in results])
    res_html = []
    for r in results:
        parts = quote_parts(r.get("answer_quote") or "")
        miss = bool(parts) and not any(p in found for p in parts)
        quote = (f'<div class="quote">근거로 든 답변: “{e(str(r["answer_quote"])[:300])}”'
                 + (' <span class="miss">— 답변 원문에서 찾지 못함</span>' if miss else '') + '</div>') if r.get("answer_quote") else ""
        res_html.append(f'<div class="res"><span class="lab">{e(r["label"])}</span><span class="tag {TAG.get(r["decision"], "t-na")}">{e(KO.get(r["decision"], r["decision"]))}</span> '
                        f'{e(r.get("reason") or "") or "<i>사유 없음 — 1차 심판만 본 칸</i>"}{quote}</div>')
    where = f"Q{c['question']} · {PROVIDER.get(c['provider'], c['provider'])}" + (f" · {c['turn']}턴" if c["type"] == "멀티턴" else "")
    att = "".join(f"\n\n[첨부: {name}]\n{content}" for name, content in c.get("attachments") or [])
    q_text = c["question_text"] + att
    prev = ""
    if c.get("prev"):
        prev = "<details><summary>앞 턴 대화 보기 (" + str(len(c["prev"])) + "턴)</summary>" + "".join(
            f'<div class="prev"><b>{k}턴 요청</b> {e(req[:500])}{"…" if len(req) > 500 else ""}<pre class="text short">{e(ans)}</pre></div>' for k, req, ans in c["prev"]) + "</details>"
    facts = ""
    if c.get("facts") or c.get("decided"):
        facts = ("<details><summary>AI 심판이 받은 이 턴의 확인 목록</summary>" + (f"<div>{e(c['situation'])}</div>" if c.get("situation") else "")
                 + '<ul class="facts">' + "".join(f"<li>{e(f)}</li>" for f in c.get("facts") or []) + "".join(f"<li>정한 판정: {e(d)}</li>" for d in c.get("decided") or []) + "</ul></details>")
    ai = json.dumps([{"label": r["label"], "out": r.get("out"), "decision": r["decision"]} for r in results], ensure_ascii=False)
    opts = "".join(f'<label class="opt {cls}"><input type="radio" name="m-{e(cid)}" value="{e(v)}">{e(v)}</label>'
                   for v, cls in zip(MARKS, ("pass", "fail", "err")))
    return f'''<section class="cell" data-id="{e(cid)}" data-no="{c['no']}" data-q="{c['question']}" data-p="{e(c['provider'])}" data-t="{c['turn']}" data-item="{e(c['item'])}" data-kind="{e(c.get('kind') or '')}" data-ai="{e(ai)}">
<div class="head"><span class="no">#{c['no']}</span><span class="tag t-type">{e(c['type'])}</span><span class="where">{e(where)}</span><span class="crit">{e(c['item'])} {e(c['name'])}</span></div>
{('<div class="desc">이 기준이 보는 것: ' + e(c['desc']) + '</div>') if c.get('desc') else ''}
{('<div class="why"><b>왜 골랐나</b>' + e(c['why']) + '</div>') if c.get('why') else ''}
<div class="results">{''.join(res_html)}</div>
<div class="panes">
<div class="pane"><h3>질문 원본{' (이 턴)' if c['type'] == '멀티턴' else ''}</h3><div class="req"><b>요청</b> {e(c['request'][:600])}{'…' if len(c['request']) > 600 else ''}</div><pre class="text">{e(q_text)}</pre></div>
<div class="pane"><h3>답변 원본 <span style="font-weight:400">— 노란색은 AI 가 근거로 든 부분</span></h3><pre class="text">{answer_html or '<i>(빈 답)</i>'}</pre></div>
</div>
{prev}{facts}
<div class="mark"><span class="q">우리가 본 결과</span>{opts}<textarea class="memo" rows="2" placeholder="메모 — 왜 그렇게 봤는지(선택, 길어지면 칸이 늘어남)"></textarea></div>
</section>'''


def render(cells: List[Dict[str, Any]], sheet: str, run_id: str, out: str) -> str:
    e = html.escape
    kinds = Counter(c.get("kind") or KO.get(c["decision"], c["decision"]) for c in cells)
    summary = " · ".join(f"{k} {n}" for k, n in kinds.most_common())
    return f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>사람 표시용 표</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans+KR:wght@400;600;700&display=swap">
<style>{CSS}</style></head><body data-sheet="{e(sheet)}"><div class="wrap">
<h1>사람 표시용 표 — 칸 {len(cells)}개</h1>
<div class="guide">
<p>칸마다 <b>질문 원본과 답변 원본</b>을 읽고, 그 칸의 기준으로 보았을 때 결과를 직접 고릅니다. 위의 AI 판정은 참고용이고, AI 가 맞았는지를 고르는 것이 아닙니다.</p>
<p class="legend"><span><b>PASS</b> 기준을 지킴</span><span><b>FAIL</b> 기준을 어김</span><span><b>에러 케이스</b> 답이 오류 문구 · 빈 답 · 무응답 · '언어 모델이라 할 수 없습니다' 같은 거절이라 이 기준으로 볼 수 없음</span></p>
<p>이 파일은 폴더에서 더블클릭해 브라우저로 열면 됩니다(서버 필요 없음). 표시는 이 브라우저에 저장됩니다. 다 하면 <b>내보내기</b>로 받은 <code>표시결과_&lt;이름&gt;.json</code> 을 이 폴더에 올려 주세요. 나눠서 할 때는 칸 번호 범위를 정하고, 이어 할 때는 <b>불러오기</b>로 엽니다. 파일 안에 미리 넣어 둔 표시가 있으면 열 때 함께 보이고, 같은 칸은 더 나중에 고친 쪽이 남습니다.</p>
<p style="color:var(--muted)">고른 칸: {e(summary)} · 실행 {e(run_id)} · {e(out)}</p>
</div>
<div class="bar"><span>표시한 사람</span><input type="text" id="who" placeholder="이름"><span id="cnt" class="cnt"></span>
<label><input type="checkbox" id="todo"> 안 한 칸만 보기</label><span class="grow"></span>
<button id="export">내보내기(JSON)</button><label class="file">불러오기<input type="file" id="import" accept="application/json" hidden></label><button id="clear">표시 지우기</button></div>
{''.join(render_cell(c) for c in cells)}
</div><script id="preset" type="application/json">{{"rows": []}}</script><script>{JS}</script></body></html>
'''


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", default="v4")
    ap.add_argument("--set", default=str(ROOT / "참고자료" / "질문세트_78"))
    ap.add_argument("--fail", type=int, default=60)
    ap.add_argument("--pass", dest="n_pass", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--dest", default=None, help="저장 폴더(기본 참고자료/분석/<오늘>-엄격함점검)")
    ap.add_argument("--pick-file", help="정해 둔 칸 목록 JSON — [{provider, question, turn, item, kind, why, results: [{out, label}]}]")
    args = ap.parse_args(argv)
    if args.pick_file:
        picks = json.loads(Path(args.pick_file).read_text(encoding="utf-8"))
        by_out: Dict[str, Dict[tuple, Dict[str, Any]]] = {}
        picked: List[Dict[str, Any]] = []
        for pk in picks:
            key = (pk["provider"], int(pk["question"]), int(pk["turn"]), pk["item"])
            specs = pk.get("results") or [{"out": pk["out"], "label": "AI 판정"}]
            base, results = None, []
            for sp in specs:
                if sp["out"] not in by_out:
                    by_out[sp["out"]] = {(c["provider"], c["question"], c["turn"], c["item"]): c
                                         for c in load_cells(args.run, sp["out"], Path(args.set), keep=ALL_DECISIONS)}
                c = by_out[sp["out"]].get(key)
                if c is not None:
                    base = base or c
                    results.append(as_result(c, sp.get("label") or sp["out"], sp["out"]))
            if base is None:
                print(f"없는 칸: {pk}")
                continue
            picked.append({**base, "why": pk.get("why") or "", "kind": pk.get("kind") or "", "results": results, "no": len(picked) + 1})
        cells = picked
        args.out = "고른 칸"
    else:
        cells = load_cells(args.run, args.out, Path(args.set))
        picked = sample(cells, args.fail, args.n_pass, args.seed)
        for c in picked:
            c["results"] = [as_result(c, "AI 판정", args.out)]
    dest = Path(args.dest) if args.dest else ROOT / "참고자료" / "분석" / f"{date.today().isoformat()}-엄격함점검"
    dest.mkdir(parents=True, exist_ok=True)
    sheet = f"{args.run}-{args.out}-s{args.seed}"
    (dest / "표시용_표.html").write_text(render(picked, sheet, args.run, args.out), encoding="utf-8")
    drop = ("answer", "question_text", "attachments", "prev")
    (dest / "표본.json").write_text(json.dumps({"sheet": sheet, "run": args.run, "out": args.out, "seed": args.seed,
                                                   "pool": {f"{t} {d}": n for (t, d), n in sorted(Counter((c["type"], c["decision"]) for c in cells).items())},
                                                   "rows": [{k: v for k, v in c.items() if k not in drop} for c in picked]}, ensure_ascii=False, indent=1, default=list), encoding="utf-8")
    by = Counter(c.get("kind") or f"{c['type']} {c['decision']}" for c in picked)
    print(f"칸 {len(cells)}개 중 {len(picked)}개 — " + " · ".join(f"{k} {n}" for k, n in sorted(by.items())))
    print(f"저장: {dest / '표시용_표.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
