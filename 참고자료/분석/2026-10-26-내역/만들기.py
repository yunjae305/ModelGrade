"""20261026내역.html — Codex · Claude CLI 2차 심판의 호출 수 차이(세는 단위)와 판정이 갈린 질문 (사용자 10-26 할 일 2)."""
import html
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]   # 저장소 뿌리
R = ROOT / "data" / "judge_runs" / "50ca3ccccace"
OUT = ROOT / "참고자료" / "분석" / "2026-10-26-내역" / "20261026내역.html"
E = html.escape
LABEL = {"aiona": "AIONA", "claude": "Claude", "gemini": "Gemini", "gpt": "GPT"}
KO = {"PASS": "통과", "FAIL": "FAIL", "HOLD": "보류", "UNVERIFIED": "보류", "NOT_APPLICABLE": "해당 없음", None: "-"}
TAG = {"PASS": "ok", "FAIL": "bad", "HOLD": "hold", "UNVERIFIED": "hold", "NOT_APPLICABLE": "na"}
qs = {int(q["id"]): q for q in json.loads((ROOT / "참고자료" / "질문세트_78" / "questions.json").read_text(encoding="utf-8"))}


def req(q):
    t = (qs.get(q) or {}).get("turns") or [{}]
    s = str(t[0].get("question") or "").split("[자료 끝]")[-1].strip().replace("\n", " ")
    return s[:70] + ("…" if len(s) > 70 else "")


def kind(q):
    return "멀티턴" if len((qs.get(q) or {}).get("turns") or []) > 1 else "단일"


# 1. 호출 수 — 같은 단위로
calls = [json.loads(x) for x in (R / "v4-claude-sonnet" / "claude_calls.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
perq = Counter(c.get("tag") for c in calls)
def is_multi_tag(t):
    m = re.match(r"Q(\d+) ", t or "")
    return bool(m) and kind(int(m.group(1))) == "멀티턴"
cl = {"all": len(calls), "multi": sum(v for t, v in perq.items() if is_multi_tag(t)), "single": sum(v for t, v in perq.items() if not is_multi_tag(t)),
      "q": len(perq), "q_multi": sum(1 for t in perq if is_multi_tag(t))}
import sys
sys.path.insert(0, str(ROOT))
from tools.judge_claude_compare import first_codex_stats, latest_lines   # noqa: E402
_A, _B = latest_lines(R / "v4" / "results.jsonl"), latest_lines(R / "v4-claude-sonnet" / "results.jsonl")
_first = {k: c for k, c in first_codex_stats(R / "v4" / "results.jsonl").items() if k in set(_A) & set(_B)}
_n = lambda c: int(c.get("calls") if c.get("calls") is not None else (c.get("rounds") or 1))   # 그 판정의 첫 호출 + 다시 묻기 (팀원 414a339 와 같은 셈)
_multi = lambda k: kind(k[1]) == "멀티턴"
cxs = {"all": sum(_n(c) for c in _first.values()), "multi": sum(_n(c) for k, c in _first.items() if _multi(k)),
       "single": sum(_n(c) for k, c in _first.items() if not _multi(k)), "q": len(_first), "q_multi": sum(1 for k in _first if _multi(k))}
b10 = {}
for name in ("v4-b10", "v4-b10-claude"):
    L = {}
    for x in (R / name / "results.jsonl").read_text(encoding="utf-8").splitlines():
        if x.strip():
            l = json.loads(x)
            L[(l["provider"], int(l["question"]))] = l
    b10[name] = L
keys = set(b10["v4-b10"]) & set(b10["v4-b10-claude"])
b10_cx = sum(((b10["v4-b10"][k].get("calls") or {}).get("codex") or {}).get("calls") or 0 for k in keys)
b10_cl = sum(((b10["v4-b10-claude"][k].get("calls") or {}).get("codex") or {}).get("calls") or 0 for k in keys)

# 2. 판정이 갈린 질문
cmp22 = json.loads((R / "v4-claude-sonnet" / "비교.json").read_text(encoding="utf-8"))
cmp25 = json.loads((R / "v4-b10-claude" / "비교.json").read_text(encoding="utf-8"))
byq = defaultdict(list)
for d in cmp22["diffs"]:
    byq[d["question"]].append(d)
item_cnt = Counter(i[0].split(":")[-1] for d in cmp22["diffs"] for i in d["items"][:3])
dir_cnt = Counter((d["codex"], d["claude"]) for d in cmp22["diffs"])
stricter = sum(n for (a, b), n in dir_cnt.items() if b == "FAIL" and a != "FAIL")
looser = sum(n for (a, b), n in dir_cnt.items() if a == "FAIL" and b != "FAIL")


def tag(v):
    return f'<span class="t {TAG.get(v, "na")}">{E(KO.get(v, str(v)))}</span>'


def diff_rows(diffs):
    out = []
    for d in diffs:
        items = "".join(f'<li><b>{E(i[0].split(":")[-1])}</b>{(" · " + E(i[0].split(":")[0])) if ":" in i[0] else ""} {tag(i[1])}→{tag(i[2])} '
                        f'<span class="why">{E((i[3] or "")[:150])}{"…" if len(i[3] or "") > 150 else ""}</span></li>' for i in d["items"][:3])
        more = f'<li class="muted">외 {d["n_changed"] - 3}칸</li>' if d["n_changed"] > 3 else ""
        out.append(f'<tr><td class="n">Q{d["question"]}</td><td>{E(LABEL.get(d["provider"], d["provider"]))}</td><td>{tag(d["codex"])}</td><td>{tag(d["claude"])}</td>'
                   f'<td><ul class="items">{items}{more}</ul></td></tr>')
    return "".join(out)


hot = sorted(byq.items(), key=lambda x: (-len(x[1]), x[0]))
hot_rows = "".join(f'<tr><td class="n">Q{q}</td><td>{kind(q)}</td><td class="n">{len(ds)}</td><td>{E(" · ".join(LABEL[d["provider"]] for d in ds))}</td><td class="req">{E(req(q))}</td></tr>'
                   for q, ds in hot if len(ds) >= 2)
item_rows = "".join(f"<tr><td><b>{E(i)}</b></td><td class='n'>{n}</td></tr>" for i, n in item_cnt.most_common(10))
all_rows = "".join(f'<tr class="qhead"><td colspan="5">Q{q} · {kind(q)} — {E(req(q))}</td></tr>' + diff_rows(ds) for q, ds in sorted(byq.items()))

CSS = """
:root{--bg:#f4f6f9;--surface:#fff;--ink:#121a24;--muted:#5a6676;--line:#d8dee7;--accent:#1f5f99;--ok:#1d7748;--ok-s:#e2f2e8;--bad:#b3291f;--bad-s:#fae4e1;--hold:#935800;--hold-s:#faefd6;--na:#5b6573;--na-s:#eaeef3;
--f:"IBM Plex Sans KR","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif;--m:"IBM Plex Mono",ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e141b;--surface:#16202a;--ink:#e6ecf2;--muted:#9aa6b4;--line:#2b3746;--accent:#80b6e4;--ok:#6fcf97;--ok-s:#15301f;--bad:#f08a80;--bad-s:#3a1c1a;--hold:#e2b25a;--hold-s:#33270f;--na:#a7b1bd;--na-s:#222d39;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0e141b;--surface:#16202a;--ink:#e6ecf2;--muted:#9aa6b4;--line:#2b3746;--accent:#80b6e4;--ok:#6fcf97;--ok-s:#15301f;--bad:#f08a80;--bad-s:#3a1c1a;--hold:#e2b25a;--hold-s:#33270f;--na:#a7b1bd;--na-s:#222d39;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--f);font-size:15px;line-height:1.6}
.wrap{max-width:1100px;margin:0 auto;padding:28px 16px 80px}h1{font-size:1.55rem;margin:0 0 4px}h2{font-size:1.15rem;margin:30px 0 8px}
.lead{color:var(--muted);margin:0 0 16px}.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 12px}
.card ul{margin:0;padding-left:18px}.tw{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:.92rem}th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:.84rem}td.n{font-family:var(--m);white-space:nowrap}td.req{color:var(--muted)}
tr.qhead td{background:var(--bg);font-weight:600;font-size:.88rem}.t{display:inline-block;font-size:.76rem;font-weight:700;border-radius:6px;padding:0 7px}
.t.ok{background:var(--ok-s);color:var(--ok)}.t.bad{background:var(--bad-s);color:var(--bad)}.t.hold{background:var(--hold-s);color:var(--hold)}.t.na{background:var(--na-s);color:var(--na)}
ul.items{margin:0;padding-left:16px}ul.items li{margin:2px 0}.why{color:var(--muted)}.muted{color:var(--muted)}
.big{font-family:var(--m);font-size:1.3rem;font-weight:700}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
details summary{cursor:pointer;color:var(--accent);font-weight:600}
"""

page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>10-26 심판 비교 내역</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;700&family=IBM+Plex+Sans+KR:wght@400;600;700&display=swap"><style>{CSS}</style></head>
<body><div class="wrap">
<h1>10-26 내역 — Codex · Claude CLI 2차 심판</h1>
<p class="lead">같은 1차 판정 결과를 두고 2차 심판만 Codex(gpt-6-luna · low)와 Claude CLI(Sonnet 5.5 · low)로 바꿔 판정한 두 비교에서, 호출 수가 왜 달라 보였는지와 어떤 질문의 판정이 갈렸는지.</p>

<div class="card"><ul>
<li><b>호출 수는 세는 단위가 달랐다.</b> 보고서의 'Claude 687'은 CLI 실행 수, 'Codex 206'은 Codex 가 판정한 질문 수(처음 한 번만)였다. 같은 단위(CLI 실행 수)로 세면 Claude {cl['all']} · Codex {cxs['all']}번 — Claude 가 {cl['all'] / cxs['all']:.1f}배다.</li>
<li><b>호출의 대부분은 멀티턴이다.</b> 멀티턴 44개에 Claude {cl['multi']} · Codex {cxs['multi']}번 — 대화 하나에 첫 판정 뒤 빠진 턴 · 인용 불일치를 턴마다 다시 묻기 때문이다.</li>
<li><b>판정은 312칸 중 {cmp22['diff_cells']}칸에서 갈렸고, Claude 가 더 엄격했다.</b> Codex → Claude 로 FAIL 이 된 칸 {stricter} · FAIL 이 풀린 칸 {looser}. 기준 칸 단위로는 {cmp22['items_same'] / cmp22['items_total'] * 100:.1f}% 같았다.</li>
</ul></div>

<h2>1. 호출 수 — 무엇을 셌나</h2>
<div class="tw"><table><thead><tr><th>센 것</th><th>Claude CLI</th><th>Codex CLI</th><th>세는 법</th></tr></thead><tbody>
<tr><td>보고서에 적힌 수(10-22 비교)</td><td class="n">{cl['all']}</td><td class="n">206</td><td>Claude: <code>claude_calls.jsonl</code> 의 줄 = CLI 실행마다 한 줄(다시 묻기 포함). Codex: 'Codex 호출(처음)' = Codex 가 판정한 (모델, 질문) 수 — 질문마다 1, 다시 묻기는 안 셈</td></tr>
<tr><td><b>같은 단위: CLI 실행 수</b></td><td class="n"><b>{cl['all']}</b></td><td class="n"><b>{cxs['all']}</b></td><td>Codex 도 칸마다 그 판정의 첫 호출 + 다시 묻기를 셈(나중의 복구 · 다시 판정 몫은 뺌 — 팀원 정리 414a339 와 같은 셈)</td></tr>
<tr><td>└ 멀티턴 대화 {cl['q_multi']}개</td><td class="n">{cl['multi']}</td><td class="n">{cxs['multi']}</td><td>대화 하나에 평균 Claude {cl['multi'] / max(1, cl['q_multi']):.0f}번 · Codex {cxs['multi'] / max(1, cxs['q_multi']):.0f}번 — 첫 판정 뒤 빠진 턴 · 인용 불일치를 턴마다 다시 물음</td></tr>
<tr><td>└ 단일 질문</td><td class="n">{cl['single']} ({cl['q'] - cl['q_multi']}개)</td><td class="n">{cxs['single']} ({cxs['q'] - cxs['q_multi']}개)</td><td>질문 하나에 1~2번</td></tr>
<tr><td>10-25 묶음 비교(단일 {len(keys)}칸)</td><td class="n">{b10_cl}</td><td class="n">{b10_cx}</td><td>판정 줄의 실제 실행 수(<code>calls</code>) — Codex 는 10개씩 한 번에, Claude 는 질문마다</td></tr>
</tbody></table></div>
<p class="muted">Claude 실행 하나 안에서는 이미지 열기 · 웹 검색 같은 도구 사용 때문에 여러 차례(num_turns 2~5)를 주고받지만, 실행은 한 번으로 셌다. 웹 검색 0 · 실패 0.</p>

<h2>2. 판정이 갈린 질문</h2>
<div class="grid">
<div class="card"><div class="muted">10-22 전체 비교 (질문 78 × 4모델)</div><div class="big">{cmp22['diff_cells']} / {cmp22['cells']}칸</div><div class="muted">통과 여부까지 다른 칸 {cmp22['pass_flip_cells']} · 모델별 {' · '.join(f"{LABEL[p]} {n}" for p, n in cmp22['diff_per_model'].items())}</div></div>
<div class="card"><div class="muted">10-25 묶음 비교 (단일 질문)</div><div class="big">{cmp25['diff_cells']} / {cmp25['cells']}칸</div><div class="muted">기준 칸 {cmp25['items_same']} / {cmp25['items_total']} 같음</div></div>
</div>

<h3>여러 모델에서 갈린 질문</h3>
<div class="tw"><table><thead><tr><th>질문</th><th>유형</th><th>갈린 모델 수</th><th>모델</th><th>요청</th></tr></thead><tbody>{hot_rows}</tbody></table></div>

<h3>자주 갈린 기준 (갈린 질문에서 판정을 가른 칸)</h3>
<div class="tw"><table><thead><tr><th>기준</th><th>칸</th></tr></thead><tbody>{item_rows}</tbody></table></div>

<h3>갈린 칸 전부 — Codex → Claude</h3>
<details open><summary>10-22 전체 비교 {cmp22['diff_cells']}칸 (질문 순)</summary>
<div class="tw"><table><thead><tr><th>질문</th><th>모델</th><th>Codex</th><th>Claude</th><th>판정을 가른 기준 (Codex → Claude · 사유)</th></tr></thead><tbody>{all_rows}</tbody></table></div></details>
<details><summary>10-25 묶음 비교 {cmp25['diff_cells']}칸</summary>
<div class="tw"><table><thead><tr><th>질문</th><th>모델</th><th>Codex</th><th>Claude</th><th>판정을 가른 기준</th></tr></thead><tbody>{diff_rows(cmp25['diffs'])}</tbody></table></div></details>

<p class="muted" style="margin-top:24px">자료: <code>data/judge_runs/50ca3ccccace/</code> v4 ↔ v4-claude-sonnet(10-22) · v4-b10 ↔ v4-b10-claude(10-25)의 비교.json · claude_calls.jsonl · run.log. 만든 날 2026-10-26.</p>
</div></body></html>"""
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(page, encoding="utf-8")
print(OUT, len(page), "· 갈린 질문", len(byq), "· 여러 모델", sum(1 for _, ds in hot if len(ds) >= 2), "· 엄격", stricter, "느슨", looser)
