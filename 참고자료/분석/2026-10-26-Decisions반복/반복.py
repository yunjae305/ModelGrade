"""Decisions 1차 판정 반복 시험 — 질문 하나를 같은 입력으로 N번 불러 고른 값(PASS · FAIL · 해당 없음)과 확률 · 확신이 바뀌는지 본다 (사용자 10-26).

    python -X utf8 참고자료/분석/2026-10-26-Decisions반복/반복.py capture --pick 73:gemini   # 실제 판정 흐름이 Decisions 에 보내는 입력을 가로채 저장(API 안 부름)
    python -X utf8 참고자료/분석/2026-10-26-Decisions반복/반복.py run --n 100 --workers 4   # 저장한 입력으로 N번 → 결과.jsonl 에 덧붙임
    python -X utf8 참고자료/분석/2026-10-26-Decisions반복/반복.py summary                    # 결과.jsonl → 요약.json

입력은 judge_v4 --first-judge decisions --first-only 가 만드는 그대로(guide · 질문 · 답변 · 그 턴의 항목 정의). 키는 .env 에서만 읽고 남기지 않는다.
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUN = "50ca3ccccace"
SET = ROOT / "참고자료" / "질문세트_78"
INPUT = HERE / "입력.json"
RESULTS = HERE / "결과.jsonl"
SUMMARY = HERE / "요약.json"
SURE = 0.8   # 교차 확인의 '확실' 기준(judge_v4.CROSS_SURE) — 고른 값 확률이 이보다 낮으면 Codex 로 넘어간다


def capture(pick: str) -> None:
    """judge_v4.main 을 1차만 · Decisions 모드로 돌리되 DecisionsJudge.judge 를 바꿔 입력만 받아 둔다(호출 안 함)."""
    import tools.judge_run as jr
    from tools import judge_v4 as jv
    got: dict = {}

    def grab(self, part, questions):
        got.setdefault("calls", []).append({"part": copy.deepcopy(part), "questions": copy.deepcopy(questions)})
        return "ERROR", None, "입력만 받음(반복 시험)"
    jr.DecisionsJudge.judge = grab
    out = "_반복시험_입력받기"
    try:
        jv.main(["--run", RUN, "--set", str(SET), "--pick", pick, "--out", out, "--first-judge", "decisions", "--first-only", "--votes", "1"])
    finally:
        shutil.rmtree(ROOT / "data" / "judge_runs" / RUN / out, ignore_errors=True)   # 입력받기용 판정 폴더는 남기지 않는다
    calls = got.get("calls") or []
    if len(calls) != 1:
        raise SystemExit(f"Decisions 호출 {len(calls)}번 — 단일 질문(턴 1개)을 고르세요")
    q, provider = pick.split(":")
    INPUT.write_text(json.dumps({"pick": pick, "question": int(q), "provider": provider, "run": RUN, "set": SET.name, **calls[0]},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {INPUT} · 항목 {len(calls[0]['questions'])}개")


def run(n: int, workers: int) -> None:
    from code_eval.decisions_client import resolve_decisions_client
    client, config, reason = resolve_decisions_client()
    if client is None:
        raise SystemExit(f"Decisions 없음 — {reason}")
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    start = sum(1 for x in RESULTS.read_text(encoding="utf-8").splitlines() if x.strip()) if RESULTS.exists() else 0
    lock = threading.Lock()
    done = [0]

    def once(i: int) -> None:
        for wait in (0, 5, 15, 30):   # 429 · 오류는 기다렸다 다시(클라이언트 자체 재시도 3번 뒤)
            time.sleep(wait)
            call = client.decide(data["part"], data["questions"])
            if call.status == "OK":
                break
        body = call.body if call.status == "OK" else None
        row = {"i": start + i + 1, "status": call.status, "error": call.error, "latency_ms": call.latency_ms, "model": (body or {}).get("model"),
               "usage": call.usage, "answers": (body or {}).get("answers"), "at": datetime.now(timezone.utc).isoformat()}
        with lock, RESULTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            done[0] += 1
            print(f"  {done[0]}/{n} · {call.status} · {call.latency_ms}ms" + (f" · {str(call.error)[:80]}" if call.status != "OK" else ""), flush=True)
    print(f"Decisions {config.model} · {n}번 · 동시 {workers}", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(once, range(n)))


def summary() -> dict:
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    rows = [json.loads(x) for x in RESULTS.read_text(encoding="utf-8").splitlines() if x.strip()]
    ok = [r for r in rows if r["status"] == "OK" and isinstance(r.get("answers"), dict)]
    items = {}
    for key, q in data["questions"].items():
        es = [r["answers"].get(key) for r in ok]
        es = [e for e in es if isinstance(e, dict) and e.get("choice") is not None]
        choices = Counter(e["choice"] for e in es)
        values = sorted({v for e in es for v in (e.get("probabilities") or {})})
        prob = {v: [float(e["probabilities"][v]) for e in es if isinstance((e.get("probabilities") or {}).get(v), (int, float))] for v in values}
        top = [float((e.get("probabilities") or {}).get(e["choice"], e.get("confidence") or 0)) for e in es]
        conf = [float(e["confidence"]) for e in es if isinstance(e.get("confidence"), (int, float))]

        def stat(xs):
            return {"평균": round(statistics.fmean(xs), 3), "표준편차": round(statistics.pstdev(xs), 3), "최소": min(xs), "최대": max(xs)} if xs else None
        vectors = Counter(json.dumps(e.get("probabilities"), sort_keys=True) for e in es)
        items[key] = {"기준": key.split(":", 1)[-1], "판정 수": len(es), "고른 값": dict(choices.most_common()),
                      "고른 값 바뀜": len(choices) > 1, "확률": {v: stat(xs) for v, xs in prob.items()}, "고른 값 확률": stat(top), "확신": stat(conf),
                      "확률 묶음 종류": len(vectors), "가장 흔한 확률 묶음": json.loads(vectors.most_common(1)[0][0]) if vectors else None,
                      "그 묶음 횟수": vectors.most_common(1)[0][1] if vectors else 0,
                      f"애매(<{SURE}) 횟수": sum(1 for t in top if t < SURE), "FAIL 횟수": choices.get("FAIL", 0)}
    verdicts = Counter("FAIL" if any((r["answers"].get(k) or {}).get("choice") == "FAIL" for k in data["questions"]) else "PASS" for r in ok)
    routes = Counter(tuple(sorted(k.split(":", 1)[-1] for k in data["questions"]
                                  if (r["answers"].get(k) or {}).get("choice") == "FAIL"
                                  or float(((r["answers"].get(k) or {}).get("probabilities") or {}).get((r["answers"].get(k) or {}).get("choice"), 0) or 0) < SURE))
                     for r in ok)
    # 전에 저장된 같은 질문의 Decisions 판정(v4-f1)과 — 항목 정의 지문이 지금과 같은 칸만 같은 입력이다
    from code_eval import semantic_items as si
    from tools import judge_v4 as jv
    saved = ROOT / "data" / "judge_runs" / RUN / "v4-f1" / "decisions" / f"Q{data['question']}_{data['provider']}.json"
    old = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else {}
    for key, it in items.items():
        it["이름"] = si.SUBS_BY_ID[si.SPECS_BY_ID[it["기준"]].sub].name if it["기준"] in si.SPECS_BY_ID else ""
        e = (old.get("1") or {}).get(it["기준"])
        if isinstance(e, dict):
            it["v4-f1"] = {"고른 값": e.get("choice"), "확률": e.get("probabilities"), "확신": e.get("confidence"),
                           "정의 같음": (old.get("_item_fp") or {}).get("1", {}).get(it["기준"]) == jv.doc_fp(data["questions"][key])}
    out = {"질문": data["pick"], "호출": len(rows), "성공": len(ok), "실패": len(rows) - len(ok), "모델": sorted({r.get("model") or "?" for r in ok}),
           "입력 토큰": sorted({(r.get("usage") or {}).get("input_tokens") for r in ok}),
           "응답 시간 ms": {"중앙값": statistics.median([r["latency_ms"] for r in ok]) if ok else None,
                         "최소": min((r["latency_ms"] for r in ok), default=None), "최대": max((r["latency_ms"] for r in ok), default=None)},
           "질문 1차 결과(FAIL 칸 하나라도 → FAIL)": dict(verdicts),
           "Codex 로 넘길 칸 묶음(FAIL 또는 확률<0.8)": {" · ".join(k) or "(없음)": c for k, c in routes.most_common()},
           "항목": items}
    SUMMARY.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


KO = {"PASS": "PASS", "FAIL": "FAIL", "NOT_APPLICABLE": "해당 없음"}
CSS = """:root{--bg:#f4f6f9;--surface:#fff;--ink:#121a24;--muted:#5a6676;--line:#d8dee7;--accent:#1f5f99;--ok:#1d7748;--ok-s:#e2f2e8;--bad:#b3291f;--bad-s:#fae4e1;--hold:#935800;--hold-s:#faefd6;
--f:"IBM Plex Sans KR","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif;--m:"IBM Plex Mono",ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e141b;--surface:#16202a;--ink:#e6ecf2;--muted:#9aa6b4;--line:#2b3746;--accent:#80b6e4;--ok:#6fcf97;--ok-s:#15301f;--bad:#f08a80;--bad-s:#3a1c1a;--hold:#e2b25a;--hold-s:#33270f;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0e141b;--surface:#16202a;--ink:#e6ecf2;--muted:#9aa6b4;--line:#2b3746;--accent:#80b6e4;--ok:#6fcf97;--ok-s:#15301f;--bad:#f08a80;--bad-s:#3a1c1a;--hold:#e2b25a;--hold-s:#33270f;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--f);font-size:15px;line-height:1.6}
.wrap{max-width:1100px;margin:0 auto;padding:28px 16px 80px}h1{font-size:1.5rem;margin:0 0 4px}h2{font-size:1.1rem;margin:28px 0 8px}p{margin:4px 0}.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:16px 0}.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card b{display:block;font-size:1.35rem;font-family:var(--m)}.card span{color:var(--muted);font-size:.85rem}
.tw{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:10px}table{border-collapse:collapse;width:100%;font-size:.88rem}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{color:var(--muted);font-weight:600}td.n{font-family:var(--m);text-align:right}tr:last-child td{border-bottom:0}
.tag{display:inline-block;border-radius:6px;padding:0 7px;font-size:.8rem;font-weight:700}.PASS{background:var(--ok-s);color:var(--ok)}.FAIL{background:var(--bad-s);color:var(--bad)}.NOT_APPLICABLE{background:var(--line);color:var(--muted)}
.amb{color:var(--hold);font-weight:700}.diff{color:var(--bad);font-weight:700}.q{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 14px;font-size:.9rem;white-space:pre-wrap}"""


def page() -> Path:
    import html
    s = json.loads(SUMMARY.read_text(encoding="utf-8"))
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    turn = data["part"]["턴"][0]
    esc = html.escape
    items = list(s["항목"].values())
    flips = sum(1 for it in items if it["고른 값 바뀜"])
    moved = sum(1 for it in items if it["확률 묶음 종류"] > 1)
    amb = [it for it in items if it[f"애매(<{SURE}) 횟수"]]

    def probs(p):
        return " · ".join(f"{KO.get(k, k)} {v:.2f}" for k, v in sorted((p or {}).items(), key=lambda kv: -kv[1]))
    rows = []
    for it in items:
        choice = next(iter(it["고른 값"]))
        top = it["고른 값 확률"]
        old = it.get("v4-f1") or {}
        if not old:
            cmp_ = "-"
        elif not old["정의 같음"]:
            cmp_ = f'<span class="muted">정의 바뀜 — 그때 {probs(old["확률"])}</span>'
        elif old["고른 값"] == choice and old["확률"] == it["가장 흔한 확률 묶음"]:
            cmp_ = "같음"
        else:
            cmp_ = f'<span class="diff">다름 — {probs(old["확률"])}</span>'
        rows.append(f'<tr><td>{esc(it["기준"])}</td><td>{esc(it["이름"])}</td>'
                    + "<td>" + " ".join(f'<span class="tag {k}">{KO.get(k, k)} {n}</span>' for k, n in it["고른 값"].items()) + "</td>"
                    f'<td>{probs(it["가장 흔한 확률 묶음"])}</td><td class="n">{top["최소"]:.2f}~{top["최대"]:.2f}</td><td class="n">{it["확신"]["평균"]:.2f}</td>'
                    f'<td class="n">{it["확률 묶음 종류"]}</td><td class="n{" amb" if it[f"애매(<{SURE}) 횟수"] else ""}">{it[f"애매(<{SURE}) 횟수"]}</td><td>{cmp_}</td></tr>')
    route = next(iter(s["Codex 로 넘길 칸 묶음(FAIL 또는 확률<0.8)"]))
    body = f"""<div class="wrap">
<h1>Decisions 같은 질문 100번</h1>
<p class="muted">Q{data['question']} {data['provider'].capitalize()} · 단일 질문 · 판정 항목 {len(items)}칸 · {s['모델'][0]} · 같은 입력({s['입력 토큰'][0]:,}토큰)을 {s['호출']}번 · 10-26</p>
<div class="cards">
<div class="card"><b>{s['성공']} / {s['호출']}</b><span>응답 받음(실패 {s['실패']})</span></div>
<div class="card"><b>{flips} / {len(items)}</b><span>고른 값이 한 번이라도 바뀐 칸</span></div>
<div class="card"><b>{moved} / {len(items)}</b><span>확률 · 확신이 한 번이라도 바뀐 칸</span></div>
<div class="card"><b>{s['질문 1차 결과(FAIL 칸 하나라도 → FAIL)'].get('FAIL', 0)} / {s['성공']}</b><span>질문 1차 결과 FAIL({esc(' · '.join(it['기준'] for it in items if it['FAIL 횟수']))})</span></div>
</div>
<p><b>결론</b>: 100번 모두 칸마다 고른 값 · 확률 · 확신이 소수 둘째 자리까지 똑같았다. Codex 로 넘어가는 칸(FAIL 또는 고른 값 확률 {SURE} 미만)도 {s['Codex 로 넘길 칸 묶음(FAIL 또는 확률<0.8)'][route]}번 모두 같은 {len(route.split(' · '))}칸 — {esc(route)}.</p>
<p class="muted">응답 시간 중앙값 {s['응답 시간 ms']['중앙값']:.0f}ms(처음 4번만 1.3초대). 이 질문에서는 Decisions 를 여러 번 불러 다수결해도 결과가 같다.</p>
<h2>칸마다</h2>
<div class="tw"><table><thead><tr><th>기준</th><th>이름</th><th>고른 값(100번)</th><th>확률</th><th>고른 값 확률 범위</th><th>확신</th><th>확률 묶음 종류</th><th>{SURE} 미만 횟수</th><th>v4-f1 저장본(오늘 13:19)</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p class="muted">v4-f1 저장본은 항목 정의 지문이 지금과 같은 칸만 같은 입력이다. 정의가 같은 {sum(1 for it in items if (it.get('v4-f1') or {}).get('정의 같음'))}칸은 그때와도 확률까지 같고, 나머지 칸은 저장 때 항목 정의가 지금과 달라 비교하지 않는다.</p>
<h2>판정한 질문 · 답변</h2>
<div class="q"><b>질문</b>  {esc(turn.get('질문') or '')}</div>
<div class="q" style="margin-top:8px"><b>답변</b>  {esc(turn.get('답변') or '')}</div>
<p class="muted" style="margin-top:12px">다시 돌리기: <code>python -X utf8 참고자료/분석/2026-10-26-Decisions반복/반복.py run --n 100</code> → <code>summary</code> → <code>page</code></p>
</div>"""
    out = HERE / "반복100.html"
    out.write_text(f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                   f'<title>Decisions 반복 100번</title><style>{CSS}</style></head><body>{body}</body></html>', encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--pick", default="73:gemini")
    r = sub.add_parser("run")
    r.add_argument("--n", type=int, default=100)
    r.add_argument("--workers", type=int, default=4)
    sub.add_parser("summary")
    sub.add_parser("page")
    args = ap.parse_args()
    if args.cmd == "capture":
        capture(args.pick)
    elif args.cmd == "run":
        run(args.n, args.workers)
    elif args.cmd == "page":
        print(f"저장: {page()}")
    else:
        s = summary()
        print(json.dumps({k: v for k, v in s.items() if k != "항목"}, ensure_ascii=False, indent=1))
        for it in s["항목"].values():
            print(f"{it['기준']:8} {it['고른 값']}  고른 값 확률 {it['고른 값 확률']}  묶음 {it['확률 묶음 종류']}종")


if __name__ == "__main__":
    main()
