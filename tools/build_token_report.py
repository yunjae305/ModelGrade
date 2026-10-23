# -*- coding: utf-8 -*-
"""판정 토큰 보고 — 결과 폴더의 results.jsonl(질문 · 모델마다 마지막 줄, tokens 칸)로 세 단계 표를 만든다 (사용자 10-23).
① 질문 × 답변 모델 × 심판(Jev · Decisions · Codex) 토큰 ② 질문마다 합계(네 모델) ③ 판정 전체 합계.
답변 수집(앱 · 웹) 토큰은 알 수 없어 넣지 않고, 비용도 넣지 않는다(토큰만). 캐시 입력은 입력 안에 든 값.

    python -X utf8 tools/build_token_report.py --dir 참고자료/결과/2026-10-23_12-41-05-662 [--set 참고자료/질문세트_78]
→ <dir>/판정토큰.html · 판정토큰.csv"""
from __future__ import annotations

import argparse
import csv
import html
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
JUDGES = ("Jev", "Decisions", "Codex")
COLS = ("입력", "캐시_입력", "출력", "추론_출력", "호출")
LABEL = {"aiona": "AIONA", "gpt": "GPT", "claude": "Claude", "gemini": "Gemini"}
PROV = ("aiona", "gpt", "claude", "gemini")


def last_lines(path: Path) -> Dict[Tuple[str, int], Dict[str, Any]]:
    out: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for l in path.read_text(encoding="utf-8").splitlines():
        if l.strip():
            r = json.loads(l)
            out[(r["provider"], int(r["question"]))] = r
    return out


def tokens_of(r: Dict[str, Any]) -> Dict[str, Counter]:
    """결과 줄의 tokens 칸 → {심판: Counter(입력 · 캐시_입력 · 출력 · 추론_출력 · 호출)} — 없으면 빈 dict(빈 출력 요구 · 답 없음)."""
    t = r.get("tokens")
    out: Dict[str, Counter] = {}
    if isinstance(t, dict):
        for j in JUDGES:
            u = t.get(j)
            if isinstance(u, dict) and any(isinstance(u.get(k), (int, float)) and u.get(k) for k in COLS):
                out[j] = Counter({k: int(u.get(k) or 0) for k in COLS})
    return out


def build(dir_: Path, set_dir: Path) -> Tuple[Path, Path]:
    lines = last_lines(dir_ / "results.jsonl")
    qs = {q["id"]: q for q in json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))}
    run = json.loads((dir_ / "run.json").read_text(encoding="utf-8")) if (dir_ / "run.json").exists() else {}
    detail: List[Dict[str, Any]] = []
    per_q: Dict[int, Counter] = defaultdict(Counter)
    per_q_judge: Dict[int, Dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    total: Dict[str, Counter] = defaultdict(Counter)
    no_token: Counter = Counter()
    for (p, q), r in sorted(lines.items(), key=lambda kv: (kv[0][1], PROV.index(kv[0][0]) if kv[0][0] in PROV else 9)):
        tj = tokens_of(r)
        if not tj:
            no_token[r.get("status") or r.get("verdict") or "?"] += 1
        for j in JUDGES:
            c = tj.get(j) or Counter()
            detail.append({"question": q, "type": qs.get(q, {}).get("type", ""), "model": LABEL.get(p, p), "judge": j, **{k: c.get(k, 0) for k in COLS},
                           "verdict": r.get("verdict"), "seconds": int(r.get("seconds") or 0)})
            for k in COLS:
                per_q[q][k] += c.get(k, 0); per_q_judge[q][j][k] += c.get(k, 0); total[j][k] += c.get(k, 0)
    grand = Counter()
    for j in JUDGES:
        grand.update(total[j])
    # CSV(①)
    csv_path = dir_ / "판정토큰.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["질문", "유형", "답변 모델", "심판", *COLS, "판정", "판정 초"])
        for d in detail:
            w.writerow([d["question"], d["type"], d["model"], d["judge"], *[d[k] for k in COLS], d["verdict"], d["seconds"]])
    # HTML
    def e(x: Any) -> str:
        return html.escape(str(x if x is not None else ""))

    def n(x: Any) -> str:
        return f"{int(x):,}" if isinstance(x, (int, float)) else e(x)

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
details summary{{cursor:pointer;color:var(--acc);font-size:13px;margin:6px 0}}
</style></head><body><main>
<h1>판정 토큰 — {e(dir_.name)}</h1>
<p class="mute small">심판이 호출마다 돌려준 실제 사용량(프롬프트 · 질문 · 자료 · 답변 · 확인 목록 · 다시 묻기 · 검색 결과 포함). 캐시 입력은 입력 안에 든 값. Decisions 는 선택형 API 라 출력 0. 답변 수집 토큰은 알 수 없어 없음 · 비용 없음(토큰만). {e(run.get("name") or "")}</p>''')
    # ③ 전체
    P.append('<h2>③ 판정 전체 합계</h2><div class="card"><div class="scroll"><table><tr><th class="l">심판</th>' + "".join(f"<th>{k.replace('_', ' ')}</th>" for k in COLS) + "</tr>")
    for j in JUDGES:
        P.append(f"<tr><td class='l'>{j}</td>" + "".join(f"<td>{n(total[j][k])}</td>" for k in COLS) + "</tr>")
    P.append(f"<tr><td class='l'><b>합계</b></td>" + "".join(f"<td><b>{n(grand[k])}</b></td>" for k in COLS) + "</tr></table></div>"
             f"<p class='small mute'>질문 × 모델 {len(lines)}칸 · 토큰 없는 칸 {sum(no_token.values())}({e(' · '.join(f'{k} {v}' for k, v in no_token.most_common()))} — 빈 출력 요구 · 답 없음은 심판을 부르지 않음)</p></div>")
    # ② 질문마다
    P.append('<h2>② 질문마다 합계(네 모델)</h2><div class="card"><div class="scroll"><table><tr><th class="l">질문</th><th class="l">유형</th><th>Jev 입력</th><th>Decisions 입력</th><th>Codex 입력</th><th>입력 합</th><th>출력 합</th><th>호출</th></tr>')
    for q in sorted(per_q):
        pj = per_q_judge[q]
        P.append(f"<tr><td class='l'>Q{q}</td><td class='l'>{e(qs.get(q, {}).get('type', ''))}</td><td>{n(pj['Jev']['입력'])}</td><td>{n(pj['Decisions']['입력'])}</td><td>{n(pj['Codex']['입력'])}</td><td><b>{n(per_q[q]['입력'])}</b></td><td>{n(per_q[q]['출력'] + per_q[q]['추론_출력'])}</td><td>{n(per_q[q]['호출'])}</td></tr>")
    P.append("</table></div></div>")
    # ① 상세
    P.append('<h2>① 질문 × 답변 모델 × 심판</h2><div class="card"><p class="small mute">심판은 어느 모델의 답인지 모르고 판정했고(블라인드), 이 표는 우리가 아는 모델 이름으로 다시 붙인 것. 같은 표를 <code>판정토큰.csv</code> 로도 둠.</p><div class="scroll"><table><tr><th class="l">질문</th><th class="l">모델</th><th class="l">심판</th>' + "".join(f"<th>{k.replace('_', ' ')}</th>" for k in COLS) + "<th class='l'>판정</th><th>초</th></tr>")
    for d in detail:
        if not d["호출"]:
            continue
        P.append(f"<tr><td class='l'>Q{d['question']}</td><td class='l'>{e(d['model'])}</td><td class='l'>{e(d['judge'])}</td>" + "".join(f"<td>{n(d[k])}</td>" for k in COLS) + f"<td class='l'>{e(d['verdict'])}</td><td>{n(d['seconds'])}</td></tr>")
    P.append("</table></div></div>")
    P.append(f"<p class='small mute'>만든 도구: tools/build_token_report.py · 자료: {e(dir_.name)}/results.jsonl</p></main></body></html>")
    out = dir_ / "판정토큰.html"
    out.write_text("\n".join(P), encoding="utf-8")
    return out, csv_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--set", default="참고자료/질문세트_78")
    a = ap.parse_args()
    out, csv_path = build(Path(a.dir), Path(a.set))
    print("저장:", out, "·", csv_path)


if __name__ == "__main__":
    main()
