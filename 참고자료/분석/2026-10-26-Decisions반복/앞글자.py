"""Decisions 앞 문장 바꾸기 시험 — 같은 질문(입력.json)의 J1-1.a 한 칸만, 입력 첫 문장만 100가지로 바꿔 불러 판정 · 확률이 바뀌는지 본다 (사용자 10-26).

    python -X utf8 참고자료/분석/2026-10-26-Decisions반복/앞글자.py run --workers 4   # 100가지 → 앞글자.jsonl (다시 쓰면 덮어씀)
    python -X utf8 참고자료/분석/2026-10-26-Decisions반복/앞글자.py page              # → 앞글자_요약.json · 앞글자100.html

첫 문장 '아래는 한국어 AI 답변 품질 평가 자료입니다.' 를 세 조각(4 × 5 × 5 = 100가지)으로 바꾸고, 그 뒤 글(안내 · 질문 · 답변 JSON)은 그대로 둔다.
0번이 지금 판정이 쓰는 원래 문장이다.
"""
from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from 반복 import CSS, INPUT, KO, RESULTS  # noqa: E402

ITEM = "T1:J1-1.a"
ORIGINAL = "아래는 한국어 AI 답변 품질 평가 자료입니다."
PARTS = (["아래는", "다음은", "아래 내용은", "이하는"],
         ["한국어 AI 답변", "한국어 AI 응답", "AI 답변", "한국어 인공지능 답변", "한국어 챗봇 답변"],
         ["품질 평가 자료입니다.", "품질을 평가하는 자료입니다.", "품질 평가용 자료입니다.", "평가 자료입니다.", "품질 검토 자료입니다."])
OUT = HERE / "앞글자.jsonl"
SUMMARY = HERE / "앞글자_요약.json"


def variants() -> list:
    out = [(idx, " ".join(words)) for idx, words in
           ((tuple(PARTS[k].index(w) for k, w in enumerate(ws)), ws) for ws in itertools.product(*PARTS))]
    assert out[0][1] == ORIGINAL and len(out) == 100
    return out


def run(workers: int) -> None:
    from code_eval import decisions_client as dc
    client, config, reason = dc.resolve_decisions_client()
    if client is None:
        raise SystemExit(f"Decisions 없음 — {reason}")
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    text = dc.state_to_input(data["part"])
    assert text.startswith(ORIGINAL)
    rest = text[len(ORIGINAL):]
    questions = {ITEM: data["questions"][ITEM]}
    local = threading.local()
    dc.state_to_input = lambda state: local.text   # 첨부가 없어 build_input 은 이 글을 그대로 보낸다 — 스레드마다 제 문장
    rows: dict = {}
    lock = threading.Lock()

    def once(job) -> None:
        i, (idx, lead) = job
        local.text = lead + rest
        for _ in range(4):   # 클라이언트 자체 재시도 3번 뒤에도 실패하면 다시
            call = client.decide(data["part"], questions)
            if call.status == "OK":
                break
        e = ((call.body or {}).get("answers") or {}).get(ITEM) if call.status == "OK" else None
        with lock:
            rows[i] = {"i": i, "parts": idx, "lead": lead, "status": call.status, "error": call.error, "latency_ms": call.latency_ms,
                       "input_tokens": (call.usage or {}).get("input_tokens"), "answer": e}
            print(f"  {len(rows)}/100 · {call.status} · {(e or {}).get('choice')} {(e or {}).get('probabilities')} · {lead}", flush=True)
    print(f"Decisions {config.model} · J1-1.a 만 · 앞 문장 100가지 · 동시 {workers}", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(once, enumerate(variants())))
    OUT.write_text("".join(json.dumps(rows[i], ensure_ascii=False) + "\n" for i in sorted(rows)), encoding="utf-8")


RECHECK = HERE / "앞글자_다시.json"


def recheck(picks: list, times: int = 2) -> None:
    """흔들림이 문장 탓인지 — 고른 문장을 같은 글로 times 번 더 불러 처음 값과 같은지 (→ 앞글자_다시.json)."""
    from code_eval import decisions_client as dc
    client, _, reason = dc.resolve_decisions_client()
    if client is None:
        raise SystemExit(f"Decisions 없음 — {reason}")
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    rest = dc.state_to_input(data["part"])[len(ORIGINAL):]
    rows = {r["i"]: r for r in map(json.loads, OUT.read_text(encoding="utf-8").splitlines()) if r}
    cur: dict = {}
    dc.state_to_input = lambda state: cur["text"]
    out = []
    for i in picks:
        cur["text"] = rows[i]["lead"] + rest
        for _ in range(times):
            e = ((client.decide(data["part"], {ITEM: data["questions"][ITEM]}).body or {}).get("answers") or {}).get(ITEM) or {}
            first = rows[i]["answer"] or {}
            out.append({"i": i, "lead": rows[i]["lead"], "처음": first.get("probabilities"), "다시": e.get("probabilities"),
                        "같음": e.get("probabilities") == first.get("probabilities") and e.get("confidence") == first.get("confidence")})
            print(i, rows[i]["lead"], "같음" if out[-1]["같음"] else f"다름 {first.get('probabilities')} → {e.get('probabilities')}", flush=True)
    RECHECK.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def summary() -> dict:
    rows = [json.loads(x) for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()]
    ok = [r for r in rows if r["status"] == "OK" and isinstance(r.get("answer"), dict) and r["answer"].get("choice")]
    vec = lambda e: json.dumps(e.get("probabilities"), sort_keys=True)
    groups = defaultdict(list)
    for r in ok:
        groups[(r["answer"]["choice"], vec(r["answer"]), r["answer"].get("confidence"))].append(r)
    pass_p = [float(r["answer"]["probabilities"].get("PASS", 0)) for r in ok]
    by_part = []
    for k, words in enumerate(PARTS):
        for j, w in enumerate(words):
            xs = [float(r["answer"]["probabilities"].get("PASS", 0)) for r in ok if r["parts"][k] == j]
            by_part.append({"자리": k + 1, "말": w, "원래 말": j == 0, "횟수": len(xs), "PASS 확률 평균": round(statistics.fmean(xs), 3) if xs else None,
                            "최소": min(xs) if xs else None, "최대": max(xs) if xs else None})
    base = None   # 같은 질문 15칸을 한꺼번에 물었던 100번(결과.jsonl)의 J1-1.a — 모두 같았으므로 첫 줄
    if RESULTS.exists():
        first = json.loads(RESULTS.read_text(encoding="utf-8").splitlines()[0])
        base = (first.get("answers") or {}).get(ITEM)
    orig = next((r for r in ok if r["i"] == 0), None)
    out = {"호출": len(rows), "성공": len(ok), "고른 값": dict(Counter(r["answer"]["choice"] for r in ok).most_common()),
           "확률 묶음 종류": len(groups),
           "PASS 확률": {"평균": round(statistics.fmean(pass_p), 3), "표준편차": round(statistics.pstdev(pass_p), 3), "최소": min(pass_p), "최대": max(pass_p)} if pass_p else None,
           "원래 문장(0번)": orig and orig["answer"], "15칸 함께 물은 100번의 J1-1.a": base,
           "확률 묶음": [{"고른 값": c, "확률": json.loads(v), "확신": conf, "횟수": len(rs), "문장": [r["lead"] for r in rs]}
                     for (c, v, conf), rs in sorted(groups.items(), key=lambda kv: -len(kv[1]))],
           "자리별": by_part, "입력 토큰": sorted({r.get("input_tokens") for r in ok})}
    SUMMARY.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def page() -> Path:
    import html
    esc = html.escape
    s = summary()

    def probs(p):
        return " · ".join(f"{KO.get(k, k)} {v:.2f}" for k, v in sorted((p or {}).items(), key=lambda kv: -kv[1]))
    pp = s["PASS 확률"]
    orig, base = s["원래 문장(0번)"] or {}, s["15칸 함께 물은 100번의 J1-1.a"] or {}
    again = json.loads(RECHECK.read_text(encoding="utf-8")) if RECHECK.exists() else []
    again_line = (f'<p>같은 문장을 다시 부르면: {sum(1 for a in again if a["같음"])} / {len(again)}번 처음과 확률 · 확신까지 같음'
                  f'({len({a["i"] for a in again})}가지 문장 × {len(again) // max(1, len({a["i"] for a in again}))}번) — 흔들림은 문장이 달라서 생긴 것이다.</p>') if again else ""
    groups = "".join(f'<tr><td><span class="tag {g["고른 값"]}">{KO.get(g["고른 값"])}</span></td><td>{probs(g["확률"])}</td><td class="n">{g["확신"]:.2f}</td>'
                     f'<td class="n">{g["횟수"]}</td><td class="wrap-cell">{esc(" / ".join(g["문장"][:3]))}{" …" if len(g["문장"]) > 3 else ""}</td></tr>' for g in s["확률 묶음"])
    parts = "".join(f'<tr><td class="n">{p["자리"]}</td><td>{esc(p["말"])}{" <span class=muted>(원래)</span>" if p["원래 말"] else ""}</td><td class="n">{p["횟수"]}</td>'
                    f'<td class="n">{p["PASS 확률 평균"]:.3f}</td><td class="n">{p["최소"]:.2f}~{p["최대"]:.2f}</td></tr>' for p in s["자리별"])
    rows = [json.loads(x) for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()]
    every = "".join(f'<tr><td class="n">{r["i"]}</td><td>{esc(r["lead"])}</td><td><span class="tag {(r["answer"] or {}).get("choice")}">{KO.get((r["answer"] or {}).get("choice"), r["status"])}</span></td>'
                    f'<td>{probs((r["answer"] or {}).get("probabilities"))}</td><td class="n">{(r["answer"] or {}).get("confidence")}</td></tr>' for r in rows)
    body = f"""<div class="wrap">
<h1>Decisions 앞 문장 100가지 · J1-1.a</h1>
<p class="muted">Q73 Gemini · J1-1.a(요청 작업 수행) 한 칸만 · {s['입력 토큰'][0]:,}~{s['입력 토큰'][-1]:,}토큰 · 입력 첫 문장만 4 × 5 × 5 = 100가지로 바꿈, 그 뒤 글은 그대로 · 10-26</p>
<div class="cards">
<div class="card"><b>{s['성공']} / {s['호출']}</b><span>응답 받음</span></div>
<div class="card"><b>{' · '.join(f'{KO.get(k)} {n}' for k, n in s['고른 값'].items())}</b><span>고른 값</span></div>
<div class="card"><b>{s['확률 묶음 종류']}종</b><span>확률 · 확신 묶음</span></div>
<div class="card"><b>{pp['최소']:.2f}~{pp['최대']:.2f}</b><span>PASS 확률 범위(평균 {pp['평균']:.3f})</span></div>
</div>
<p>원래 문장(0번): {probs(orig.get('probabilities'))} · 확신 {orig.get('confidence')} — 15칸을 함께 물은 100번의 J1-1.a: {probs(base.get('probabilities'))} · 확신 {base.get('confidence')}</p>
{again_line}
<p>고른 값은 100가지 모두 {KO.get(next(iter(s['고른 값'])))}, 고른 값 확률은 모두 0.8 이상이라 Codex 로 넘어가는 칸(애매)도 생기지 않았다. 바꾼 말 하나가 확률을 한쪽으로 미는 경향은 없다(말마다 평균 {min(p['PASS 확률 평균'] for p in s['자리별']):.3f}~{max(p['PASS 확률 평균'] for p in s['자리별']):.3f}).</p>
<h2>나온 확률 묶음</h2>
<div class="tw"><table><thead><tr><th>고른 값</th><th>확률</th><th>확신</th><th>횟수</th><th>그 문장(앞 3개)</th></tr></thead><tbody>{groups}</tbody></table></div>
<h2>바꾼 말마다</h2>
<p class="muted">첫 문장 = [1 아래는 …] [2 한국어 AI 답변 …] [3 품질 평가 자료입니다 …]. 말마다 그 말이 들어간 문장(20~25개)의 PASS 확률.</p>
<div class="tw"><table><thead><tr><th>자리</th><th>말</th><th>횟수</th><th>PASS 확률 평균</th><th>범위</th></tr></thead><tbody>{parts}</tbody></table></div>
<details style="margin-top:18px"><summary>100번 전부</summary><div class="tw" style="margin-top:8px"><table><thead><tr><th>#</th><th>첫 문장</th><th>고른 값</th><th>확률</th><th>확신</th></tr></thead><tbody>{every}</tbody></table></div></details>
<p class="muted" style="margin-top:12px">다시 돌리기: <code>python -X utf8 참고자료/분석/2026-10-26-Decisions반복/앞글자.py run</code> → <code>page</code></p>
</div>"""
    out = HERE / "앞글자100.html"
    out.write_text(f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                   f'<title>Decisions 앞 문장 100가지</title><style>{CSS}.wrap-cell{{white-space:normal;min-width:260px}}</style></head><body>{body}</body></html>',
                   encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--first", type=int, help="시험용 — 앞에서 N가지만 (파일은 쓰지 않음)")
    sub.add_parser("page")
    rc = sub.add_parser("recheck")
    rc.add_argument("--picks", default="0,1,25,50,77,98")
    args = ap.parse_args()
    if args.cmd == "recheck":
        recheck([int(x) for x in args.picks.split(",")])
    elif args.cmd == "run":
        if args.first:
            global variants
            full = variants
            variants = lambda: full()[:args.first]   # noqa: E731
            global OUT
            OUT = HERE / "_앞글자_시험.jsonl"
        run(args.workers)
    else:
        print(f"저장: {page()}")
        s = json.loads(SUMMARY.read_text(encoding="utf-8"))
        print(json.dumps({k: v for k, v in s.items() if k not in ("확률 묶음", "자리별")}, ensure_ascii=False))
        for g in s["확률 묶음"]:
            print(g["고른 값"], g["확률"], g["확신"], g["횟수"])


if __name__ == "__main__":
    main()
