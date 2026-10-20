"""판정 결과 HTML (사용자 요청 2026-10-19 밤: "판정 결과는 html 로 저장") — `modelgrade report`·`start` 끝에 `data/judge_runs/<실행>/report.html` 로 쓴다.

내용: 모델별 요약(PASS·FAIL·보류·통과율·Jev→Codex 확정·GPT 대신 판정·평균 시간) · 모델별 답변 수집 시간 · 판정 시간 · 질문별 판정(FAIL 항목과 사유·인용, 보류 사유,
빈 출력 수집 상태) · 기준별 FAIL 수. 숫자 단위는 '질문 수 / 전체 질문 수'(모델 합산 없음, 사용자 규칙).
"""
from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

CSS = """
body{font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;margin:24px;color:#222;background:#fff}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}
.meta{color:#666;font-size:13px;margin-bottom:12px}
table{border-collapse:collapse;font-size:13px;margin:6px 0 12px}th,td{border:1px solid #ccc;padding:4px 8px;vertical-align:top;text-align:left}
th{background:#f3f3f3}td.num,th.num{text-align:right}
.PASS{color:#1a7f37;font-weight:600}.FAIL{color:#c0392b;font-weight:600}.HOLD{color:#b7791f;font-weight:600}
.q{max-width:420px;color:#444}.small{font-size:12px;color:#555}.reason{max-width:520px}
details summary{cursor:pointer;color:#333}
"""


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _hms(seconds: float) -> str:
    s = int(seconds or 0)
    return f"{s // 3600}시간 {s % 3600 // 60}분 {s % 60}초" if s >= 3600 else (f"{s // 60}분 {s % 60}초" if s >= 60 else f"{s}초")


def collection_time_rows(run: Dict[str, Any], providers: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """모델별 답변 수집 시간 — 받은 질문 수, 답변 시간 합(모델이 답하는 데 걸린 시간), 질문당 평균, 수집 묶음 벽시계 합."""
    providers = providers or list(run.get("providers") or [])
    out = []
    for p in providers:
        from webapp.run_summary import asks_empty_output
        rows = [r for r in (run.get("datasets") or {}).get(p) or [] if isinstance(r, dict)]
        by_q: Dict[int, List[str]] = {}
        empty_q: set = set()
        for r in rows:
            q = int(r["case_id"].split("_T")[0][1:])
            by_q.setdefault(q, []).append((r.get("collection") or {}).get("status") or "")
            if asks_empty_output(None, r.get("question")):
                empty_q.add(q)
        # 질문 단위: 모든 턴 받음 / 보류 / 오류·답 없음 — 보류·오류도 결과이므로 따로 센다 (사용자 10-20 밤).
        # 빈 출력 요구 질문은 복사 실패든 모델 오류('처리 중 오류가 생겨 멈췄습니다')든 판정이 보류이므로 수집 표에서도 보류로 센다.
        received = sum(1 for st in by_q.values() if all(x == "SUCCESS" for x in st))
        held = sum(1 for q, st in by_q.items() if not all(x == "SUCCESS" for x in st)
                   and (q in empty_q or all(x in ("SUCCESS", "COPY_FAILED") for x in st)))
        error = len(by_q) - received - held
        answer_s = sum((r.get("collection") or {}).get("latency_ms") or 0 for r in rows) / 1000
        wall = sum(e.get("seconds") or 0 for e in run.get("imports") or [] if e.get("provider") == p and e.get("method") == "collect-app-v1")
        out.append({"provider": p, "questions": received, "held": held, "errors": error, "total": len(by_q),
                    "answer_seconds": answer_s, "per_question": answer_s / received if received else 0, "wall_seconds": wall,
                    "label": (run.get("models") or {}).get(p) or p})
    return out


def judge_time_info(out_dir: Path) -> Dict[str, Any]:
    f = out_dir / "timing.jsonl"
    if not f.exists():
        return {}
    recs = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    judge = [r for r in recs if r.get("kind") == "judge"]
    total = sum(r.get("seconds") or 0 for r in judge)
    n = sum(r.get("judgments") or 0 for r in judge)
    return {"seconds": total, "judgments": n, "per_judgment": total / n if n else 0, "batches": len(judge)}


def _fail_items(line: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for turn, items in sorted((line.get("results") or {}).items(), key=lambda kv: int(kv[0])):
        for item, v in items.items():
            if v.get("decision") in ("FAIL", "UNVERIFIED"):
                e = (v.get("entries") or [{}])[0]
                out.append({"turn": turn, "item": item, "decision": v["decision"], "judge": v.get("judge"), "reason": e.get("reason") or "",
                            "quote": e.get("answer_quote") or "", "problems": v.get("problems") or [], "jev": v.get("jev")})
    return out


def write_report(out_dir: Path, run: Optional[Dict[str, Any]] = None) -> Path:
    out_dir = Path(out_dir)
    results = [json.loads(l) for l in (out_dir / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8")) if (out_dir / "summary.json").exists() else {}
    run = run or {}
    providers = list(run.get("providers") or sorted({r["provider"] for r in results}))
    questions_text: Dict[int, str] = {}
    for p in providers:
        for r in (run.get("datasets") or {}).get(p) or []:
            if isinstance(r, dict) and r.get("case_id", "").endswith("_T1"):
                questions_text.setdefault(int(r["case_id"].split("_T")[0][1:]), r.get("question") or "")
    total_q = run.get("question_count") or (summary.get("models") or {}).get(providers[0], {}).get("n") if providers else None
    parts = [f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>판정 결과 {_e(run.get('run_id') or out_dir.name)}</title><style>{CSS}</style></head><body>"]
    parts.append(f"<h1>판정 결과 — 실행 {_e(run.get('run_id') or out_dir.name)}</h1>")
    parts.append(f"<div class='meta'>{_e(run.get('name') or '')} · 질문 세트 {_e((run.get('question_set') or {}).get('set_id') or '')} · 만든 시각 {datetime.now():%Y-%m-%d %H:%M} · "
                 f"심판: Codex CLI {_e(((summary.get('judges') or {}).get('gpt') or {}).get('model'))}·{_e(((summary.get('judges') or {}).get('gpt') or {}).get('reasoning'))} + Jev {_e(((summary.get('judges') or {}).get('jev') or {}).get('model'))} · 기준 72개(CODE 28 + 의미 44)</div>")

    # 1. 모델별 요약
    parts.append("<h2>1. 모델별 결과 (질문 수 / 전체 질문 수)</h2><table><tr><th>모델</th><th class='num'>PASS</th><th class='num'>FAIL</th><th class='num'>보류</th><th class='num'>판정한 질문</th><th class='num'>통과율</th><th class='num'>Jev→Codex 확정 항목</th><th class='num'>GPT 대신 판정</th><th>평균 시간(초) Codex · Jev</th></tr>")
    by_provider: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_provider[r["provider"]].append(r)
    for p in providers:
        mine = by_provider.get(p) or []
        tally = Counter(r.get("verdict") for r in mine)
        m = (summary.get("models") or {}).get(p) or {}
        sec = m.get("seconds") or {}
        n = total_q or m.get("n") or len(mine)
        parts.append(f"<tr><td>{_e((run.get('models') or {}).get(p) or p)}</td><td class='num PASS'>{tally['PASS']}</td><td class='num FAIL'>{tally['FAIL']}</td><td class='num HOLD'>{tally['HOLD']}</td>"
                     f"<td class='num'>{len(mine)} / {n}</td><td class='num'>{(tally['PASS'] / n * 100) if n else 0:.1f}%</td><td class='num'>{m.get('jev_flagged', 0)}</td><td class='num'>{m.get('fallback', 0)}</td>"
                     f"<td>{(sec.get('gpt') or {}).get('mean', '-')} · {(sec.get('jev') or {}).get('mean', '-')}</td></tr>")
    parts.append("</table>")

    # 2. 시간
    parts.append("<h2>2. 걸린 시간</h2>")
    crow = collection_time_rows(run, providers) if run else []
    if crow:
        parts.append("<table><tr><th>모델</th><th class='num'>받음</th><th class='num'>보류</th><th class='num'>오류·답 없음</th><th class='num'>합계</th><th>답변 시간 합</th><th class='num'>질문당 평균</th><th>수집 묶음 벽시계 합</th></tr>")
        for c in crow:
            parts.append(f"<tr><td>{_e(c['label'])}</td><td class='num'>{c['questions']}</td><td class='num'>{c['held']}</td><td class='num'>{c['errors']}</td><td class='num'>{c['total']}</td><td>{_hms(c['answer_seconds'])}</td><td class='num'>{c['per_question']:.0f}초</td><td>{_hms(c['wall_seconds'])}</td></tr>")
        parts.append("</table>")
    jt = judge_time_info(out_dir)
    if jt:
        parts.append(f"<p>판정 시간: 총 <b>{_hms(jt['seconds'])}</b> · 판정 {jt['judgments']}건(질문×모델) · 건당 평균 {jt['per_judgment']:.0f}초 · 묶음 {jt['batches']}번</p>")

    # 3. 기준별 FAIL 수
    fail_counter: Dict[str, Counter] = defaultdict(Counter)
    for r in results:
        for f in _fail_items(r):
            if f["decision"] == "FAIL":
                fail_counter[f["item"]][r["provider"]] += 1
        for c in r.get("code_fails") or []:
            fail_counter[c][r["provider"]] += 1
    if fail_counter:
        parts.append("<h2>3. 기준별 FAIL 질문 수</h2><table><tr><th>기준</th>" + "".join(f"<th class='num'>{_e(p)}</th>" for p in providers) + "</tr>")
        for item in sorted(fail_counter):
            parts.append(f"<tr><td>{_e(item)}</td>" + "".join(f"<td class='num'>{fail_counter[item].get(p, 0)}</td>" for p in providers) + "</tr>")
        parts.append("</table>")

    # 4. 질문별
    parts.append("<h2>4. 질문별 판정</h2><table><tr><th>질문</th><th class='q'>질문 글(앞부분)</th><th>모델</th><th>판정</th><th class='reason'>FAIL · 보류 항목과 사유</th><th>비고</th></tr>")
    for r in sorted(results, key=lambda x: (int(x["question"]), providers.index(x["provider"]) if x["provider"] in providers else 99)):
        q = int(r["question"])
        cells = []
        for f in _fail_items(r):
            who = f["judge"] or ""
            jev = f.get("jev") or {}
            jev_note = f" (Jev {jev.get('decision')} {jev.get('probabilities', {}).get('FAIL', '')})" if jev else ""
            text = f"<b class='{'FAIL' if f['decision'] == 'FAIL' else 'HOLD'}'>T{f['turn']} {_e(f['item'])}</b> <span class='small'>[{_e(who)}{_e(jev_note)}]</span> {_e(f['reason'])}"
            if f["quote"]:
                text += f"<br><span class='small'>답 인용: {_e(f['quote'][:160])}</span>"
            if f["problems"]:
                text += f"<br><span class='small'>보류 사유: {_e('; '.join(f['problems'])[:240])}</span>"
            cells.append(text)
        for c in r.get("code_fails") or []:
            cells.append(f"<b class='FAIL'>CODE {_e(c)}</b>")
        note = r.get("collection_note") or r.get("reason") or ""
        verdict = r.get("verdict") or ""
        parts.append(f"<tr><td>Q{q}</td><td class='q'>{_e((questions_text.get(q) or '')[:90])}</td><td>{_e(r['provider'])}</td><td class='{_e(verdict)}'>{_e(verdict)}</td>"
                     f"<td class='reason'>{'<br>'.join(cells) if cells else ''}</td><td class='small'>{_e(note[:200])} {'· ' + str(r.get('seconds')) + '초' if r.get('seconds') else ''}</td></tr>")
    parts.append("</table></body></html>")
    path = out_dir / "report.html"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
