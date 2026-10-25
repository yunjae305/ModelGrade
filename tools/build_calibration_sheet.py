# -*- coding: utf-8 -*-
"""심판이 너무 엄격한지 · 헐렁한지 재기 위한 표시용 표 (사용자 10-23: "표시용 표도 만들어줘").

판정 폴더의 FAIL 칸과 PASS 칸을 유형(싱글 · 멀티턴) · 기준별로 뽑아, 사람이 칸마다 '맞음 · 틀림 · 애매'와 원인(답이 틀림 · 프롬프트 · 기준 · 질문 · 수집)을
표시하는 HTML 을 만든다. 표시는 브라우저에 저장되고 '표시 내보내기'로 JSON 을 받는다 — 기준마다 거짓 FAIL · 거짓 PASS 비율을 내는 재료.

    python -X utf8 tools/build_calibration_sheet.py --run 50ca3ccccace --out v4-r2 [--fail 60 --pass 40 --seed 1]
→ 참고자료/분석/<오늘>-엄격함점검/표시용_표.html · 표본.json
    python -X utf8 tools/build_calibration_sheet.py --run 50ca3ccccace --pick-file 고를칸.json --dest <폴더>
→ 정해 둔 칸만(여러 판정 폴더 · 보류 칸 포함) — 고를칸.json: [{"out", "provider", "question", "turn", "item", "why"}] (할일 9번, 10-26)
"""
from __future__ import annotations

import argparse
import html
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KO = {"PASS": "통과", "FAIL": "FAIL", "UNVERIFIED": "보류", "NOT_APPLICABLE": "해당 없음"}
CAUSES = ("답이 틀림", "프롬프트", "기준", "질문", "수집")


def load_cells(run_id: str, out: str, set_dir: Path, keep: tuple = ("PASS", "FAIL")) -> List[Dict[str, Any]]:
    from tools import report_html as R
    run = json.loads((ROOT / "data" / "comparison_runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    answers = {(p, r["case_id"]): r for p, rows in run["datasets"].items() for r in rows}
    names = R.criteria_table(set_dir)
    qs = {q["id"]: q for q in json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))}
    plan_path = set_dir / "multiturn_v3" / "turn_checklists.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")).get("questions", {}) if plan_path.exists() else {}
    last: Dict[tuple, Dict[str, Any]] = {}
    for line in (ROOT / "data" / "judge_runs" / run_id / out / "results.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            x = json.loads(line)
            last[(x["provider"], int(x["question"]))] = x
    cells = []
    for (p, q), v in sorted(last.items()):
        if v.get("status") != "judged":
            continue
        multi = int(v.get("turns") or 1) > 1
        flow = qs.get(q, {}).get("flow") or []
        for tn, t in (v.get("turn_results") or {}).items():
            case_id = f"Q{q}_T{tn}" if multi else f"Q{q}"
            row = answers.get((p, case_id)) or answers.get((p, f"Q{q}_T{tn}")) or {}
            facts = ((plan.get(str(q)) or {}).get("turns") or [{}] * 20)[int(tn) - 1] if multi else {}
            for iid, it in (t.get("items") or {}).items():
                if it.get("decision") not in keep:
                    continue
                cells.append({"type": "멀티턴" if multi else "싱글", "question": q, "provider": p, "turn": int(tn), "flow": flow[int(tn) - 1] if multi and int(tn) - 1 < len(flow) else "",
                              "item": iid, "name": (names.get(iid) or {}).get("name") or "", "decision": it["decision"], "how": it.get("how") or "",
                              "reason": it.get("reason") or "", "answer_quote": it.get("answer_quote") or "", "question_quote": it.get("question_quote") or "",
                              "first": it.get("jev"), "first_prob": (it.get("decisions_prob") or {}).get("FAIL") if isinstance(it.get("decisions_prob"), dict) else None,
                              "codex": it.get("codex"), "request": str(row.get("question") or "").split("[자료 끝]")[-1].strip(),   # 붙인 자료를 뺀 요청 문장
                              "answer": row.get("answer") or "", "facts": facts.get("checks") or [], "situation": facts.get("situation") or ""})
    return cells


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


CSS = """
:root { --bg:#f2f4f7; --surface:#fff; --ink:#111b26; --muted:#566272; --line:#d5dce5; --accent:#1e5d96; --accent-soft:#e3ecf6; --ok:#1d7748; --ok-soft:#e1f1e7; --risk:#b3291f; --risk-soft:#fae3e0; --warn:#965a00; --warn-soft:#faeed4;
  --font-body:"IBM Plex Sans KR","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif; --font-num:"IBM Plex Mono",ui-monospace,Menlo,monospace; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#0e141b; --surface:#16202a; --ink:#e5ebf1; --muted:#98a4b3; --line:#2a3644; --accent:#7fb5e3; --accent-soft:#1a2a3a; --ok:#6fcf97; --ok-soft:#15301f; --risk:#f08a80; --risk-soft:#3a1c1a; --warn:#e2b25a; --warn-soft:#33270f; color-scheme:dark; } }
:root[data-theme="dark"] { --bg:#0e141b; --surface:#16202a; --ink:#e5ebf1; --muted:#98a4b3; --line:#2a3644; --accent:#7fb5e3; --accent-soft:#1a2a3a; --ok:#6fcf97; --ok-soft:#15301f; --risk:#f08a80; --risk-soft:#3a1c1a; --warn:#e2b25a; --warn-soft:#33270f; color-scheme:dark; }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--ink); font-family:var(--font-body); font-size:15px; line-height:1.55; }
.wrap { max-width:1180px; margin:0 auto; padding:26px 16px 80px; } h1 { font-size:1.6rem; margin:0 0 6px; } .lead { color:var(--muted); margin:0 0 12px; }
.bar { position:sticky; top:0; z-index:5; background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:10px 14px; display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:0 0 16px; font-size:.92rem; }
.bar button { font:inherit; border:1px solid var(--accent); background:var(--accent-soft); color:var(--accent); border-radius:8px; padding:6px 12px; cursor:pointer; font-weight:600; }
.bar .cnt { font-family:var(--font-num); color:var(--muted); }
.cell { background:var(--surface); border:1px solid var(--line); border-radius:14px; padding:14px 16px; margin:0 0 12px; }
.cell.done { border-color:var(--ok); }
.head { display:flex; flex-wrap:wrap; gap:8px 14px; align-items:baseline; margin-bottom:6px; }
.head .no { font-family:var(--font-num); font-weight:700; color:var(--accent); } .head .meta { color:var(--muted); font-size:.88rem; }
.tag { display:inline-block; font-size:.76rem; font-weight:700; border-radius:6px; padding:1px 8px; } .t-fail { background:var(--risk-soft); color:var(--risk); } .t-pass { background:var(--ok-soft); color:var(--ok); } .t-type { background:var(--accent-soft); color:var(--accent); }
.q { margin:4px 0; } .q b { color:var(--muted); font-weight:600; font-size:.85rem; margin-right:6px; }
.reason { background:var(--bg); border-radius:8px; padding:8px 10px; margin:6px 0; font-size:.92rem; } .reason .lab { color:var(--muted); font-size:.8rem; font-weight:600; }
details { margin:6px 0; } summary { cursor:pointer; color:var(--accent); font-size:.9rem; font-weight:600; } pre.ans { white-space:pre-wrap; word-break:break-word; font-family:var(--font-body); font-size:.9rem; background:var(--bg); border-radius:8px; padding:10px 12px; max-height:420px; overflow:auto; margin:6px 0 0; }
ul.facts { margin:4px 0 0; padding-left:18px; font-size:.88rem; color:var(--muted); }
.mark { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; margin-top:8px; padding-top:8px; border-top:1px dashed var(--line); font-size:.92rem; }
.mark label { cursor:pointer; } .mark select, .mark input[type=text] { font:inherit; border:1px solid var(--line); border-radius:6px; padding:4px 8px; background:var(--surface); color:var(--ink); } .mark input[type=text] { min-width:240px; flex:1; }
"""

JS = """
const KEY = 'calib-' + document.body.dataset.sheet;
const saved = JSON.parse(localStorage.getItem(KEY) || '{}');
function refresh() {
  let n = 0; document.querySelectorAll('.cell').forEach(c => { const id = c.dataset.id; const v = saved[id]; c.classList.toggle('done', !!(v && v.mark)); if (v && v.mark) n++; });
  document.getElementById('cnt').textContent = n + ' / ' + document.querySelectorAll('.cell').length + ' 표시함';
}
document.querySelectorAll('.cell').forEach(c => {
  const id = c.dataset.id; const v = saved[id] || {};
  c.querySelectorAll('input[type=radio]').forEach(r => { r.checked = v.mark === r.value; r.addEventListener('change', () => { saved[id] = {...(saved[id]||{}), mark: r.value}; localStorage.setItem(KEY, JSON.stringify(saved)); refresh(); }); });
  const sel = c.querySelector('select'); sel.value = v.cause || ''; sel.addEventListener('change', () => { saved[id] = {...(saved[id]||{}), cause: sel.value}; localStorage.setItem(KEY, JSON.stringify(saved)); });
  const memo = c.querySelector('input[type=text]'); memo.value = v.memo || ''; memo.addEventListener('input', () => { saved[id] = {...(saved[id]||{}), memo: memo.value}; localStorage.setItem(KEY, JSON.stringify(saved)); });
});
document.getElementById('export').addEventListener('click', () => {
  const rows = [...document.querySelectorAll('.cell')].map(c => ({ id: c.dataset.id, type: c.dataset.type, question: +c.dataset.q, provider: c.dataset.p, turn: +c.dataset.t, item: c.dataset.item, decision: c.dataset.dec, ...(saved[c.dataset.id] || {}) }));
  const blob = new Blob([JSON.stringify({ sheet: document.body.dataset.sheet, exported: new Date().toISOString(), rows }, null, 1)], { type: 'application/json' });
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = '표시결과_' + document.body.dataset.sheet + '.json'; a.click();
});
document.getElementById('clear').addEventListener('click', () => { if (confirm('이 표의 표시를 모두 지울까요?')) { localStorage.removeItem(KEY); location.reload(); } });
refresh();
"""


def render(cells: List[Dict[str, Any]], sheet: str, run_id: str, out: str) -> str:
    e = html.escape
    parts = []
    for c in cells:
        cid = f"{c['provider']}-{c['question']}-{c['turn']}-{c['item']}"
        facts = "".join(f"<li>{e(f)}</li>" for f in c["facts"][:8])
        parts.append(f'''<div class="cell" data-id="{e(cid)}" data-type="{e(c['type'])}" data-q="{c['question']}" data-p="{e(c['provider'])}" data-t="{c['turn']}" data-item="{e(c['item'])}" data-dec="{e(c['decision'])}">
<div class="head"><span class="no">#{c['no']}</span><span class="tag t-type">{e(c['type'])}</span><span class="meta">Q{c['question']} · {e(c['provider'])}{' · ' + str(c['turn']) + '턴(' + e(c['flow']) + ')' if c['type'] == '멀티턴' else ''}</span>
<span class="tag {'t-fail' if c['decision'] == 'FAIL' else 't-pass'}">{KO.get(c['decision'], c['decision'])}</span><b>{e(c['item'])} {e(c['name'])}</b><span class="meta">{e(c['how'])}</span></div>
{('<div class="q"><b>왜 골랐나</b>' + e(c['why']) + '</div>') if c.get('why') else ''}
<div class="q"><b>요청</b>{e(c['request'][:400])}{'…' if len(c['request']) > 400 else ''}</div>
{('<div class="q"><b>이 턴의 사실</b>' + e(c['situation']) + '<ul class="facts">' + facts + '</ul></div>') if c['facts'] else ''}
<div class="reason"><div class="lab">심판 사유</div>{e(c['reason']) or '<i>(1차 심판만 본 칸 — 사유 없음)</i>'}{('<div class="lab" style="margin-top:4px">답변 인용</div>' + e(c['answer_quote'])) if c['answer_quote'] else ''}</div>
<details><summary>답변 원문 보기</summary><pre class="ans">{e(c['answer'])}</pre></details>
<div class="mark"><span>심판이</span>
<label><input type="radio" name="m-{e(cid)}" value="맞음"> 맞음</label><label><input type="radio" name="m-{e(cid)}" value="틀림"> 틀림</label><label><input type="radio" name="m-{e(cid)}" value="애매"> 애매</label>
<select><option value="">원인(틀림 · 애매일 때)</option>{''.join(f'<option value="{e(x)}">{e(x)}</option>' for x in CAUSES)}</select>
<input type="text" placeholder="메모"></div></div>''')
    n_fail = sum(1 for c in cells if c["decision"] == "FAIL"); n_pass = len(cells) - n_fail
    return f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>심판 표시용 표</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans+KR:wght@400;600;700&display=swap">
<style>{CSS}</style></head><body data-sheet="{e(sheet)}"><div class="wrap">
<h1>심판 표시용 표 — 너무 엄격한지 · 헐렁한지</h1>
<p class="lead">실행 {e(run_id)} · 판정 폴더 {e(out)} 의 FAIL 칸 {n_fail}개 · 통과 칸 {n_pass}개(유형 · 기준별로 고루 뽑음). 칸마다 답변 원문과 심판 사유를 보고 <b>심판이 맞음 · 틀림 · 애매</b>를 고르고, 틀림 · 애매면 원인을 고릅니다. 표시는 이 브라우저에 저장되고, 다 하면 '표시 내보내기'로 JSON 을 받아 저장소에 넣어 주세요 — 기준마다 거짓 FAIL · 거짓 통과 비율을 냅니다.</p>
<div class="bar"><span id="cnt" class="cnt"></span><button id="export">표시 내보내기(JSON)</button><button id="clear">표시 지우기</button><span class="cnt">FAIL = 심판이 어겼다고 봄 · 통과 = 지켰다고 봄</span></div>
{''.join(parts)}
</div><script>{JS}</script></body></html>
'''


def main(argv: List[str] | None = None) -> int:
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
    ap.add_argument("--pick-file", help="정해 둔 칸 목록 JSON — [{out, provider, question, turn, item, why}] (여러 판정 폴더 · 보류 칸 포함)")
    args = ap.parse_args(argv)
    if args.pick_file:
        picks = json.loads(Path(args.pick_file).read_text(encoding="utf-8"))
        by_out: Dict[str, Dict[tuple, Dict[str, Any]]] = {}
        picked = []
        for pk in picks:
            if pk["out"] not in by_out:
                by_out[pk["out"]] = {(c["provider"], c["question"], c["turn"], c["item"]): c
                                     for c in load_cells(args.run, pk["out"], Path(args.set), keep=("PASS", "FAIL", "UNVERIFIED", "NOT_APPLICABLE"))}
            c = by_out[pk["out"]].get((pk["provider"], int(pk["question"]), int(pk["turn"]), pk["item"]))
            if c is None:
                print(f"없는 칸: {pk}")
                continue
            picked.append({**c, "why": pk.get("why") or "", "no": len(picked) + 1, "out": pk["out"]})
        cells = picked
        args.out = "고른 칸"
    else:
        cells = load_cells(args.run, args.out, Path(args.set))
        picked = sample(cells, args.fail, args.n_pass, args.seed)
    dest = Path(args.dest) if args.dest else ROOT / "참고자료" / "분석" / f"{date.today().isoformat()}-엄격함점검"
    dest.mkdir(parents=True, exist_ok=True)
    sheet = f"{args.run}-{args.out}-s{args.seed}"
    (dest / "표시용_표.html").write_text(render(picked, sheet, args.run, args.out), encoding="utf-8")
    (dest / "표본.json").write_text(json.dumps({"sheet": sheet, "run": args.run, "out": args.out, "seed": args.seed, "pool": {f"{t} {d}": n for (t, d), n in sorted(Counter((c["type"], c["decision"]) for c in cells).items())},
                                                   "rows": [{k: v for k, v in c.items() if k != "answer"} for c in picked]}, ensure_ascii=False, indent=1, default=dict), encoding="utf-8")
    by = Counter((c["type"], c["decision"]) for c in picked)
    print(f"칸 {len(cells)}개 중 {len(picked)}개 — " + " · ".join(f"{t} {d} {n}" for (t, d), n in sorted(by.items())))
    print(f"저장: {dest / '표시용_표.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
