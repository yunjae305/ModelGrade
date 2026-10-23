"""판정 토큰 기록 md — 판정 결과(results.jsonl)에서 질문마다 · 모델마다 · 심판마다 판정 토큰을 표로 (사용자 10-23: "사용된 토큰량 기록해둬" ·
"질문당 평가된 토큰 수도 기록되게" · "md파일로 보여줘").

판정 토큰만 센다(답변 수집 토큰 제외) — 심판이 돌려준 사용량 그대로, 비용은 넣지 않는다. 질문 · 모델마다 마지막 판정 줄을 쓰고,
저장된 출력을 다시 쓴 줄(--repair · --reuse-codex)은 그 판정에서 새로 쓴 토큰만 있어 '다시 씀'으로 표시한다.

    python -X utf8 tools/judge_token_report.py --results data/judge_runs/50ca3ccccace/v4-r3/results.jsonl --set 참고자료/질문세트_78 --out 토큰기록.md
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import judge_v4 as jv  # noqa: E402

MODELS = (("gpt", "GPT"), ("claude", "Claude"), ("gemini", "Gemini"), ("aiona", "AIONA"))
JUDGES = ("Jev", "Decisions", "Codex")
FIELDS = ("입력", "캐시_입력", "출력", "추론_출력", "호출")


def latest_lines(path: Path) -> Dict[Tuple[int, str], Dict[str, Any]]:
    """(질문, 모델) → 마지막 줄 — 판정 줄과 심판 없이 낸 줄(빈 출력 요구 등) 모두."""
    out: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            x = json.loads(line)
            out[(int(x["question"]), str(x["provider"]))] = x
    return out


def tokens_of(line: Dict[str, Any]) -> Dict[str, Any]:
    """판정 줄의 질문당 토큰 — tokens 칸(10-23 부터)이 없으면 calls 에서 다시 계산."""
    if line.get("status") != "judged":
        return {"합계": {f: 0 for f in FIELDS}, "다시_씀": False}
    return line.get("tokens") or jv.question_tokens_used(line.get("calls") or {})


def fmt(n: int) -> str:
    return f"{n:,}"


def build(results: Path, set_dir: Path, title: str = "") -> str:
    lines = latest_lines(results)
    qs = {int(q["id"]): q for q in json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))}
    qtype = {i: (q.get("type") or "단일 질문") for i, q in qs.items()}
    n_q = len(qs)
    by_judge: Dict[str, Counter] = defaultdict(Counter)
    by_model: Dict[str, Counter] = defaultdict(Counter)
    by_type: Dict[str, Counter] = defaultdict(Counter)
    verdicts: Dict[str, Counter] = defaultdict(Counter)
    reused = 0
    per_q: Dict[int, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    seconds: Dict[str, float] = defaultdict(float)
    for (q, p), line in lines.items():
        if q not in qs:
            continue
        verdicts[p][line.get("verdict") or "HOLD"] += 1
        tok = tokens_of(line)
        per_q[q][p] = tok
        reused += bool(tok.get("다시_씀"))
        seconds[p] += float(line.get("seconds") or 0)
        for judge in JUDGES:
            if judge in tok:
                by_judge[judge].update(tok[judge])
        by_model[p].update(tok["합계"])
        by_model[p]["질문"] += 1
        by_type[qtype[q]].update(tok["합계"])
        by_type[qtype[q]]["질문·모델"] += 1
    total = Counter()
    for c in by_model.values():
        total.update({f: c[f] for f in FIELDS})
    out: List[str] = [f"# 판정 토큰 기록{(' — ' + title) if title else ''}", ""]
    out += [f"- 판정 기록: `{results.relative_to(ROOT) if results.is_relative_to(ROOT) else results}` · 질문 세트: `{set_dir.relative_to(ROOT) if set_dir.is_relative_to(ROOT) else set_dir}` ({n_q}문항)",
            "- 판정 토큰만(답변 수집 토큰 제외) · 심판이 돌려준 사용량 그대로 · 비용은 넣지 않음. 캐시 입력은 입력 안에 든 값(입력과 따로 더하지 않음).",
            f"- 질문 · 모델마다 마지막 판정 줄 기준. 저장된 출력을 다시 쓴 줄 {reused}개" + (" — 그 줄은 이번에 새로 쓴 토큰만 들어 있음" if reused else " — 모두 이번 판정에서 새로 부른 토큰"), ""]
    out += ["## 판정 결과 (모델마다 질문 수 / " + str(n_q) + ")", "", "| 모델 | 통과 | FAIL | 보류 | 판정 시간 합 |", "|---|---:|---:|---:|---:|"]
    for p, name in MODELS:
        v = verdicts.get(p, Counter())
        out.append(f"| {name} | {v['PASS']} | {v['FAIL']} | {v['HOLD']} | {seconds[p] / 60:.0f}분 |")
    out += ["", "## 심판별 합계", "", "| 심판 | 호출 | 입력 | 캐시 입력 | 출력 | 추론 출력 |", "|---|---:|---:|---:|---:|---:|"]
    for judge in JUDGES:
        c = by_judge.get(judge)
        if c:
            out.append(f"| {judge} | {fmt(c['호출'])} | {fmt(c['입력'])} | {fmt(c['캐시_입력'])} | {fmt(c['출력'])} | {fmt(c['추론_출력'])} |")
    out.append(f"| **합계** | {fmt(total['호출'])} | {fmt(total['입력'])} | {fmt(total['캐시_입력'])} | {fmt(total['출력'])} | {fmt(total['추론_출력'])} |")
    out += ["", "## 모델별 합계", "", "| 모델 | 질문 | 입력 | 출력 | 질문당 평균 입력 | 질문당 평균 출력 |", "|---|---:|---:|---:|---:|---:|"]
    for p, name in MODELS:
        c = by_model.get(p, Counter())
        k = max(c["질문"], 1)
        out.append(f"| {name} | {c['질문']} | {fmt(c['입력'])} | {fmt(c['출력'])} | {fmt(round(c['입력'] / k))} | {fmt(round(c['출력'] / k))} |")
    out += ["", "## 질문 유형별 합계 (네 모델 합)", "", "| 유형 | 질문 × 모델 | 입력 | 출력 | 질문 × 모델당 평균 입력 |", "|---|---:|---:|---:|---:|"]
    for t, c in sorted(by_type.items(), key=lambda kv: -kv[1]["입력"]):
        out.append(f"| {t} | {c['질문·모델']} | {fmt(c['입력'])} | {fmt(c['출력'])} | {fmt(round(c['입력'] / max(c['질문·모델'], 1)))} |")
    out += ["", "## 질문당 토큰 (입력 / 출력 — 1차 심판 + Codex 합)", "",
            "`*` 저장된 출력을 다시 쓴 줄 · `-` 판정하지 않은 줄(빈 출력 요구 · 수집 실패 등 심판 없이 낸 판정)", "",
            "| 질문 | 유형 | 턴 | " + " | ".join(name for _, name in MODELS) + " | 네 모델 합 (입력) |", "|---:|---|---:|" + "---:|" * len(MODELS) + "---:|"]
    for q in sorted(qs):
        cells, qsum = [], 0
        for p, _ in MODELS:
            tok = per_q[q].get(p)
            line = lines.get((q, p))
            if tok is None or (line or {}).get("status") != "judged":
                cells.append("-")
                continue
            t = tok["합계"]
            qsum += t["입력"]
            cells.append(f"{fmt(t['입력'])} / {fmt(t['출력'])}" + ("*" if tok.get("다시_씀") else ""))
        out.append(f"| {q} | {qtype[q]} | {len(qs[q].get('turns') or [])} | " + " | ".join(cells) + f" | {fmt(qsum)} |")
    return "\n".join(out) + "\n"


def build_html_csv(results: Path, set_dir: Path, out_dir: Path) -> Tuple[Path, Path]:
    """세 단계 표(사용자 10-23: ① 질문 × 답변 모델 × 심판 ② 질문마다 합계 ③ 전체 합계) → <out_dir>/판정토큰.html · 판정토큰.csv.
    (tools/build_token_report.py 를 이 도구에 합침 — 10-23 코드 정리)"""
    import csv
    import html as _html
    lines = latest_lines(results)
    qs = {int(q["id"]): q for q in json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))}
    detail: List[Dict[str, Any]] = []
    per_q: Dict[int, Counter] = defaultdict(Counter)
    per_qj: Dict[int, Dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    total: Dict[str, Counter] = defaultdict(Counter)
    no_token: Counter = Counter()
    order = {p: i for i, (p, _) in enumerate(MODELS)}
    for (q, p), line in sorted(lines.items(), key=lambda kv: (kv[0][0], order.get(kv[0][1], 9))):
        if q not in qs:
            continue
        tok = tokens_of(line)
        judged = any(isinstance(tok.get(j), dict) and tok[j].get("호출") for j in JUDGES)
        if not judged:
            no_token[line.get("status") or line.get("verdict") or "?"] += 1
        for j in JUDGES:
            c = Counter({f: int((tok.get(j) or {}).get(f) or 0) for f in FIELDS})
            detail.append({"question": q, "type": qs[q].get("type") or "", "model": dict(MODELS).get(p, p), "judge": j, **c,
                           "verdict": line.get("verdict"), "seconds": int(line.get("seconds") or 0), "reused": bool(tok.get("다시_씀"))})
            for f in FIELDS:
                per_q[q][f] += c[f]; per_qj[q][j][f] += c[f]; total[j][f] += c[f]
    grand = Counter()
    for j in JUDGES:
        grand.update(total[j])
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "판정토큰.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["질문", "유형", "답변 모델", "심판", *FIELDS, "판정", "질문 판정 초(심판별 아님)", "저장 출력 다시 씀"])
        for d in detail:
            w.writerow([d["question"], d["type"], d["model"], d["judge"], *[d[k] for k in FIELDS], d["verdict"], d["seconds"], "예" if d["reused"] else ""])
    e = lambda x: _html.escape(str(x if x is not None else ""))
    n = lambda x: f"{int(x):,}" if isinstance(x, (int, float)) else e(x)
    P: List[str] = []
    P.append(f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>판정 토큰</title>
<style>
:root{{--bg:#fbfbf9;--fg:#1c1c1a;--mute:#6b6b66;--line:#e2e1dc;--card:#fff;--th:#f1f0ea;--acc:#2f5fa8}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#161614;--fg:#ececea;--mute:#a4a49e;--line:#33332f;--card:#1f1f1c;--th:#262623;--acc:#8ab4f8}}}}
:root[data-theme="dark"]{{--bg:#161614;--fg:#ececea;--mute:#a4a49e;--line:#33332f;--card:#1f1f1c;--th:#262623;--acc:#8ab4f8}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 "Pretendard","Noto Sans KR",system-ui,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:22px 16px 56px}} h1{{font-size:21px;margin:0 0 4px}} h2{{font-size:16px;margin:26px 0 8px}} .mute{{color:var(--mute)}} .small{{font-size:12.5px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border-bottom:1px solid var(--line);padding:5px 8px;text-align:right;white-space:nowrap}} th{{background:var(--th);position:sticky;top:0}}
th:first-child,td:first-child,th.l,td.l{{text-align:left}} .scroll{{overflow-x:auto;max-height:70vh;overflow-y:auto}}
</style></head><body><main>
<h1>판정 토큰 — {e(out_dir.name)}</h1>
<p class="mute small">심판이 호출마다 돌려준 실제 사용량(프롬프트 · 질문 · 자료 · 답변 · 확인 목록 · 다시 묻기 · 검색 결과 포함). 캐시 입력은 입력 안에 든 값. Decisions 는 선택형 API 라 출력 0. 답변 수집 토큰은 알 수 없어 없음 · 비용 없음(토큰만). 질문 · 모델마다 마지막 판정 줄 기준이며 '다시 씀' 줄은 저장된 출력을 다시 써서 이번에 새로 쓴 토큰만 들어 있음. 초는 그 질문 · 모델 판정 전체 시간(심판별 아님).</p>''')
    P.append('<h2>③ 판정 전체 합계</h2><div class="card"><div class="scroll"><table><tr><th class="l">심판</th>' + "".join(f"<th>{f.replace('_', ' ')}</th>" for f in FIELDS) + "</tr>")
    for j in JUDGES:
        P.append(f"<tr><td class='l'>{j}</td>" + "".join(f"<td>{n(total[j][f])}</td>" for f in FIELDS) + "</tr>")
    P.append("<tr><td class='l'><b>합계</b></td>" + "".join(f"<td><b>{n(grand[f])}</b></td>" for f in FIELDS) + "</tr></table></div>"
             f"<p class='small mute'>질문 × 모델 {sum(1 for (q, p) in lines if q in qs)}칸 · 토큰 없는 칸 {sum(no_token.values())}({e(' · '.join(f'{k} {v}' for k, v in no_token.most_common()))} — 빈 출력 요구 · 답 없음은 심판을 부르지 않음)</p></div>")
    P.append('<h2>② 질문마다 합계(네 모델)</h2><div class="card"><div class="scroll"><table><tr><th class="l">질문</th><th class="l">유형</th><th>Jev 입력</th><th>Decisions 입력</th><th>Codex 입력</th><th>입력 합</th><th>출력 합</th><th>추론 출력</th><th>호출</th></tr>')
    for q in sorted(per_q):
        pj = per_qj[q]
        P.append(f"<tr><td class='l'>Q{q}</td><td class='l'>{e(qs[q].get('type', ''))}</td><td>{n(pj['Jev']['입력'])}</td><td>{n(pj['Decisions']['입력'])}</td><td>{n(pj['Codex']['입력'])}</td><td><b>{n(per_q[q]['입력'])}</b></td><td>{n(per_q[q]['출력'])}</td><td>{n(per_q[q]['추론_출력'])}</td><td>{n(per_q[q]['호출'])}</td></tr>")
    P.append("</table></div></div>")
    P.append('<h2>① 질문 × 답변 모델 × 심판</h2><div class="card"><p class="small mute">심판은 어느 모델의 답인지 모르고 판정했고(블라인드), 이 표는 우리가 아는 모델 이름으로 다시 붙인 것. 같은 표를 <code>판정토큰.csv</code> 로도 둠.</p><div class="scroll"><table><tr><th class="l">질문</th><th class="l">모델</th><th class="l">심판</th>' + "".join(f"<th>{f.replace('_', ' ')}</th>" for f in FIELDS) + "<th class='l'>판정</th><th>질문 판정 초</th><th class='l'>다시 씀</th></tr>")
    for d in detail:
        if not d["호출"] and not d["reused"]:
            continue
        reuse = "예(이번에 새로 쓴 토큰만)" if d["reused"] else ""   # f-string 안에 같은 따옴표를 겹치지 않게(파이썬 3.10 · 3.11)
        P.append(f"<tr><td class='l'>Q{d['question']}</td><td class='l'>{e(d['model'])}</td><td class='l'>{e(d['judge'])}</td>" + "".join(f"<td>{n(d[f])}</td>" for f in FIELDS) + f"<td class='l'>{e(d['verdict'])}</td><td>{n(d['seconds'])}</td><td class='l'>{reuse}</td></tr>")
    P.append("</table></div></div>")
    P.append("<p class='small mute'>만든 도구: tools/judge_token_report.py --html-dir</p></main></body></html>")
    html_path = out_dir / "판정토큰.html"
    html_path.write_text("\n".join(P), encoding="utf-8")
    return html_path, csv_path


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", required=True, help="판정 결과 results.jsonl")
    ap.add_argument("--set", required=True, help="질문 세트 폴더 (questions.json)")
    ap.add_argument("--out", help="쓸 md 파일(토큰기록.md)")
    ap.add_argument("--html-dir", help="세 단계 표 판정토큰.html · 판정토큰.csv 를 쓸 폴더(보통 결과 폴더)")
    ap.add_argument("--title", default="")
    args = ap.parse_args(argv)
    if not args.out and not args.html_dir:
        ap.error("--out 또는 --html-dir 가운데 하나는 필요합니다")
    if args.out:
        md = build(Path(args.results).resolve(), Path(args.set).resolve(), args.title)
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"저장: {args.out}")
    if args.html_dir:
        h, c = build_html_csv(Path(args.results).resolve(), Path(args.set).resolve(), Path(args.html_dir).resolve())
        print(f"저장: {h} · {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
