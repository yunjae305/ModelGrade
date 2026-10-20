"""판정 결과 HTML 보고서 (v2, 2026-10-20 밤) — `modelgrade report --dir data/judge_runs/<실행>/<out>` 와 자동 보고서(tools/auto_report.py)가 쓴다.

일반인이 봐도 바로 읽히게: 한눈에 보기(완전 통과 질문 수 / 78) → 유형별 → 기준 72개별 → 걸린 시간·토큰 → 보류(사람이 정함: 질문 원문·답변 원문·
보류 사유 + PASS/FAIL 버튼·내보내기) → FAIL 상세(원문 + 사유) → 질문별 표 → 평가 방법. 숫자 단위는 '질문 수 / 전체 질문 수'(모델 합산 없음).
결과 구조는 판정 v4(results)·v5 선별(turn_results) 둘 다 읽는다. 사람 판정(<out>/human_decisions.json)이 있으면 반영한 뒤의 수치로 보이고 전후를 같이 적는다.
"""
from __future__ import annotations

import html
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "data" / "comparison_runs"
DEFAULT_SET = ROOT / "참고자료" / "질문세트_78_생성프롬프트v3.3"
LABEL = {"aiona": "AIONA", "gpt": "GPT", "claude": "Claude", "gemini": "Gemini"}
ORDER = ["aiona", "gpt", "claude", "gemini"]
KO = {"PASS": "통과", "FAIL": "FAIL", "HOLD": "보류", "UNVERIFIED": "보류", "NOT_APPLICABLE": "해당 없음"}
TYPE_ORDER = ["단일 질문", "10턴 멀티턴 대화", "첨부자료 확인·요약", "롱컨텍스트", "명시적 빈 출력"]
HOLD_KINDS = ("심판 출력 누락", "인용 불일치", "확인 못함", "심판 갈림", "질문 전체 보류", "기타")
POLICY = "judge-v5-screen-1"

CSS = """
:root{--ink:#1d1d1f;--mute:#6e6e73;--line:#e5e5ea;--bg:#f5f5f7;--aiona:#0a66ff;--pass:#1a7f37;--fail:#c0392b;--hold:#b7791f}
*{box-sizing:border-box}body{font-family:'Pretendard','Malgun Gothic','Apple SD Gothic Neo',sans-serif;margin:0;color:var(--ink);background:var(--bg);line-height:1.5}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}h1{font-size:26px;margin:0 0 6px}h2{font-size:19px;margin:40px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--line)}
h3{font-size:15px;margin:18px 0 6px}.meta{color:var(--mute);font-size:13px}.lead{font-size:15px;color:#333;margin:6px 0 14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:14px 0}
.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px 18px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.card.aiona{border:2px solid var(--aiona)}.card .name{font-weight:700;font-size:15px}.card .big{font-size:34px;font-weight:800;margin:4px 0 2px}.card .big small{font-size:15px;color:var(--mute);font-weight:500}
.card .sub{font-size:13px;color:var(--mute)}.bar{height:8px;background:#eee;border-radius:4px;overflow:hidden;margin:8px 0 4px}.bar i{display:block;height:100%;background:var(--pass)}
.card.aiona .bar i{background:var(--aiona)}.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;color:#fff}
.b-PASS{background:var(--pass)}.b-FAIL{background:var(--fail)}.b-HOLD{background:var(--hold)}.b-NA{background:#999}
table{border-collapse:collapse;font-size:13px;margin:8px 0 14px;background:#fff;width:100%}th,td{border:1px solid var(--line);padding:6px 9px;vertical-align:top;text-align:left}
th{background:#fafafa;font-weight:600}td.num,th.num{text-align:right;white-space:nowrap}tr.grp td{background:#f0f4ff;font-weight:700}
.PASS{color:var(--pass);font-weight:700}.FAIL{color:var(--fail);font-weight:700}.HOLD{color:var(--hold);font-weight:700}.best{background:#eaf3ff}
.strength{background:#eef5ff;border:1px solid #cfe0ff;border-radius:12px;padding:12px 16px;margin:10px 0}.strength ul{margin:6px 0 0 18px}
.cta{display:inline-block;background:var(--aiona);color:#fff;padding:10px 18px;border-radius:10px;text-decoration:none;font-weight:700;margin-top:8px}
details{background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin:8px 0}summary{cursor:pointer;font-weight:600}
details.q{border-left:4px solid var(--hold)}details.f{border-left:4px solid var(--fail)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:900px){.two{grid-template-columns:1fr}}
pre.orig{white-space:pre-wrap;word-break:break-word;background:#fafafa;border:1px solid var(--line);border-radius:8px;padding:10px;font-size:12.5px;max-height:420px;overflow:auto;font-family:inherit}
.item{border-top:1px dashed var(--line);padding:8px 0}.item .id{font-weight:700}.item .why{color:#333}.item .quote{color:var(--mute);font-size:12.5px}.kind{font-size:12px;color:#fff;background:#888;padding:0 6px;border-radius:8px;margin-left:6px}
.btns button{border:1px solid #bbb;background:#fff;border-radius:8px;padding:3px 10px;margin:4px 4px 0 0;cursor:pointer;font-size:12.5px}.btns button.on{color:#fff;border-color:transparent}
.btns button.on[data-v=PASS]{background:var(--pass)}.btns button.on[data-v=FAIL]{background:var(--fail)}.btns button.on[data-v=NOT_APPLICABLE]{background:#777}.btns button.on[data-v=UNVERIFIED]{background:var(--hold)}
.toolbar{position:sticky;top:0;background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:10px 0;z-index:5;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.toolbar input{padding:5px 8px;border:1px solid #bbb;border-radius:6px}.toolbar button{padding:6px 12px;border-radius:8px;border:1px solid var(--aiona);background:var(--aiona);color:#fff;cursor:pointer;font-weight:600}
.note{font-size:12.5px;color:var(--mute)}.small{font-size:12px;color:var(--mute)}.qtxt{color:#444}
"""

JS = """
(function(){
  const RUN=document.body.dataset.run, KEY='mg-held-'+RUN;
  let store={}; try{store=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){store={}}
  function save(){try{localStorage.setItem(KEY,JSON.stringify(store))}catch(e){}}
  function paint(){document.querySelectorAll('.hold-item').forEach(el=>{const d=store[el.dataset.key]||{};el.querySelectorAll('.btns button').forEach(b=>b.classList.toggle('on',b.dataset.v===d.v));const n=el.querySelector('input.note');if(n&&d.note!=null&&n.value!==d.note)n.value=d.note});
    const n=Object.values(store).filter(d=>d.v).length;const c=document.getElementById('decided');if(c)c.textContent=n}
  document.querySelectorAll('.hold-item .btns button').forEach(b=>b.addEventListener('click',()=>{const el=b.closest('.hold-item');const k=el.dataset.key;store[k]=Object.assign(store[k]||{},{v:b.dataset.v});save();paint()}));
  document.querySelectorAll('.hold-item input.note').forEach(i=>i.addEventListener('change',()=>{const k=i.closest('.hold-item').dataset.key;store[k]=Object.assign(store[k]||{},{note:i.value});save()}));
  document.querySelectorAll('.allbtn').forEach(b=>b.addEventListener('click',()=>{b.closest('details').querySelectorAll('.hold-item').forEach(el=>{store[el.dataset.key]=Object.assign(store[el.dataset.key]||{},{v:b.dataset.v})});save();paint()}));
  const ex=document.getElementById('export');if(ex)ex.addEventListener('click',()=>{const who=(document.getElementById('reviewer')||{}).value||'';const out=[];
    document.querySelectorAll('.hold-item').forEach(el=>{const d=store[el.dataset.key];if(!d||!d.v)return;out.push({provider:el.dataset.provider,question:+el.dataset.question,turn:+el.dataset.turn,item:el.dataset.item,old:el.dataset.old,new:d.v,note:d.note||'',reviewer:who,at:new Date().toISOString()})});
    const doc={run_id:RUN,policy:document.body.dataset.policy,exported_at:new Date().toISOString(),reviewer:who,decisions:out};
    const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(doc,null,1)],{type:'application/json'}));a.download='held_decisions_'+RUN+'.json';a.click()});
  const cl=document.getElementById('clear');if(cl)cl.addEventListener('click',()=>{if(confirm('이 브라우저에 저장된 판단을 모두 지울까요?')){store={};save();paint()}});
  paint();
})();
"""


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _hms(seconds: float) -> str:
    s = int(seconds or 0)
    return f"{s // 3600}시간 {s % 3600 // 60}분 {s % 60}초" if s >= 3600 else (f"{s // 60}분 {s % 60}초" if s >= 60 else f"{s}초")


def _qno(case_id: str) -> int:
    return int(case_id.split("_T")[0][1:])


def _tno(case_id: str) -> int:
    return int(case_id.split("_T")[1]) if "_T" in case_id else 1


# ---------------------------------------------------------------- 읽기 --------------------------------------------------------------
def resolve_run_id(out_dir: Path) -> str:
    """--dir 가 <실행>/v4 처럼 한 단계 안이면 부모 이름이 실행 ID (Codex 지적 10-20: 'v4' 를 실행 ID 로 읽던 문제)."""
    for cand in (out_dir.name, out_dir.parent.name):
        if (RUNS / f"{cand}.json").exists() or (RUNS / f"{cand}.meta.json").exists():
            return cand
    return out_dir.parent.name if out_dir.name in ("v4", "v3", "v5") else out_dir.name


def load_results(out_dir: Path) -> List[Dict[str, Any]]:
    """results.jsonl — 같은 (모델, 질문) 이 여러 줄이면 마지막 줄만 (다시 판정한 것)."""
    seen: Dict[Tuple[str, int], Dict[str, Any]] = {}
    f = out_dir / "results.jsonl"
    if not f.exists():
        return []
    for l in f.read_text(encoding="utf-8").splitlines():
        if l.strip():
            r = json.loads(l)
            seen[(r["provider"], int(r["question"]))] = r
    return list(seen.values())


def load_human(out_dir: Path) -> Dict[Tuple[str, int, int, str], Dict[str, Any]]:
    f = out_dir / "human_decisions.json"
    if not f.exists():
        return {}
    doc = json.loads(f.read_text(encoding="utf-8"))
    out = {}
    for d in doc.get("decisions") or []:
        out[(d["provider"], int(d["question"]), int(d.get("turn") or 1), d["item"])] = d
    return out


def set_dir_for(run: Dict[str, Any]) -> Path:
    sid = ((run or {}).get("question_set") or {}).get("set_id")
    for d in (ROOT / "참고자료").glob("질문세트_*"):
        sj = d / "set.json"
        try:
            if sid and sj.exists() and json.loads(sj.read_text(encoding="utf-8")).get("id") == sid:
                return d
        except (OSError, ValueError):
            continue
    return DEFAULT_SET


def criteria_table(set_dir: Path) -> Dict[str, Dict[str, str]]:
    """기준 72개 — {id: {name, group}} (CODE 28 은 catalog.code, 의미 44 는 catalog.items)."""
    out: Dict[str, Dict[str, str]] = {}
    f = set_dir / "criteria_catalog.json"
    if f.exists():
        cat = json.loads(f.read_text(encoding="utf-8")).get("catalog") or {}
        for c in cat.get("code") or []:
            out[c["id"]] = {"name": c.get("name") or c["id"], "group": "CODE " + c["id"].split(".")[0]}
        for it in cat.get("items") or []:
            out[it["id"]] = {"name": f"{it.get('sub') or ''} — {(it.get('question') or '')[:60]}", "group": it["id"].split("-")[0]}
    return out


def question_types(set_dir: Path) -> Dict[int, str]:
    f = set_dir / "questions.json"
    out: Dict[int, str] = {}
    if f.exists():
        for q in json.loads(f.read_text(encoding="utf-8")):
            out[int(q["id"])] = q.get("type") or "단일 질문"
    return out


def originals(run: Dict[str, Any]) -> Dict[Tuple[str, int], List[Dict[str, Any]]]:
    """(모델, 질문) → 턴마다 {turn, question, answer, attachments, status}."""
    out: Dict[Tuple[str, int], List[Dict[str, Any]]] = defaultdict(list)
    for p, rows in ((run or {}).get("datasets") or {}).items():
        for r in rows or []:
            if not isinstance(r, dict) or not r.get("case_id"):
                continue
            out[(p, _qno(r["case_id"]))].append({"turn": _tno(r["case_id"]), "question": r.get("question") or "", "answer": r.get("answer") or "",
                                                  "attachments": [a.get("name") for a in r.get("attachments") or [] if isinstance(a, dict)],
                                                  "reference_chars": len(r.get("reference") or ""), "status": (r.get("collection") or {}).get("status") or "",
                                                  "error": (r.get("collection") or {}).get("error") or ""})
    for k in out:
        out[k].sort(key=lambda t: t["turn"])
    return out


def code_detail(run: Dict[str, Any]) -> Dict[Tuple[str, int], List[Dict[str, Any]]]:
    """CODE 평가 결과 — (모델, 질문) → counted 조건들 {turn, check_id, verdict, reason}."""
    out: Dict[Tuple[str, int], List[Dict[str, Any]]] = defaultdict(list)
    for p, ev in ((run or {}).get("evaluations") or {}).items():
        for case in (ev or {}).get("cases") or []:
            cid = case.get("case_id") or ""
            if not cid:
                continue
            for r in case.get("results") or []:
                if r.get("counted") not in (True, "True"):
                    continue
                ex = r.get("explain")
                out[(p, _qno(cid))].append({"turn": _tno(cid), "check_id": r.get("check_id"), "verdict": r.get("verdict"),
                                            "reason": (ex.get("headline") if isinstance(ex, dict) else None) or r.get("reason") or ""})
    return out


def hold_kind(v: Dict[str, Any]) -> str:
    how, probs, entries = v.get("how") or "", v.get("problems") or [], v.get("entries") or []
    if "심판 갈림" in how:
        return "심판 갈림"
    if not entries or any("판정 없음" in p for p in probs):
        return "심판 출력 누락"
    if any("원문에 없음" in p or "선택지" in p for p in probs):
        return "인용 불일치"
    if v.get("codex") == "UNVERIFIED" or "보류" in how:
        return "확인 못함"
    return "기타"


def normalize_items(line: Dict[str, Any]) -> List[Dict[str, Any]]:
    """판정 v4(results[turn][item] + entries) 와 v5(turn_results[turn].items[item]) 를 같은 모양으로."""
    out: List[Dict[str, Any]] = []
    src = line.get("turn_results") or line.get("results") or {}
    for turn, block in sorted(src.items(), key=lambda kv: int(kv[0])):
        items = block.get("items") if isinstance(block, dict) and "items" in block else block
        for item, v in (items or {}).items():
            if not isinstance(v, dict):
                continue
            entries = v.get("entries") or []
            e = next((x for x in entries if x.get("decision") == v.get("decision")), entries[0] if entries else {})
            how = v.get("how") or ""
            if not how and isinstance(v.get("jev"), dict):   # 판정 v4 줄: Jev 판정과 FAIL 확률
                jv = v["jev"]
                how = f"{v.get('judge') or ''} · Jev {jv.get('decision')} {(jv.get('probabilities') or {}).get('FAIL', '')}".strip(" ·")
            out.append({"turn": int(turn), "item": item, "decision": v.get("decision"), "how": how, "reason": v.get("reason") or e.get("reason") or "",
                        "notes": v.get("notes") or [],
                        "answer_quote": v.get("answer_quote") or e.get("answer_quote") or "", "question_quote": v.get("question_quote") or e.get("question_quote") or "",
                        "problems": v.get("problems") or [], "codex": v.get("codex"), "second": v.get("jev"), "flagged": v.get("flagged") or "",
                        "kind": hold_kind(v) if v.get("decision") == "UNVERIFIED" else ""})
    return out


def effective(line: Dict[str, Any], items: List[Dict[str, Any]], human: Dict[Tuple[str, int, int, str], Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]], int]:
    """사람 판정을 반영한 질문 판정 — (판정, 반영된 항목들, 바뀐 항목 수). 규칙은 심판과 같다: CODE FAIL 또는 항목 FAIL → FAIL, 보류 남으면 HOLD."""
    whole = human.get((line["provider"], int(line["question"]), 1, "__question__"))
    if line.get("status") not in (None, "judged"):
        if whole and whole.get("new") in ("PASS", "FAIL"):
            return whole["new"], items, 1
        return line.get("verdict") or "HOLD", items, 0
    changed = 0
    new_items = []
    for it in items:
        h = human.get((line["provider"], int(line["question"]), it["turn"], it["item"]))
        if h and h.get("new") in ("PASS", "FAIL", "NOT_APPLICABLE", "UNVERIFIED") and h["new"] != it["decision"]:
            it = {**it, "decision": h["new"], "human": h, "kind": "사람 판정" if h["new"] != "UNVERIFIED" else it["kind"]}
            changed += 1
        new_items.append(it)
    decs = {it["decision"] for it in new_items}
    verdict = "FAIL" if (line.get("code_fails") or "FAIL" in decs) else ("HOLD" if "UNVERIFIED" in decs else "PASS")
    return verdict, new_items, changed


def codex_usage_from_log(out_dir: Path) -> Dict[Tuple[int, str], Dict[str, float]]:
    """run.log 의 'Q1 gpt Codex 완료 — 입력 N · 출력 M 토큰, S초' 줄을 모아 (질문, 모델)별 호출 수·토큰·초. 429 대기 횟수는 (0, '_all')."""
    out: Dict[Tuple[int, str], Dict[str, float]] = defaultdict(lambda: {"calls": 0, "in": 0, "out": 0, "sec": 0, "waits": 0})
    f = out_dir / "run.log"
    if not f.exists():
        return out
    pat = re.compile(r"Q(\d+) (\w+) Codex 완료 — 입력 (\d+) · 출력 (\d+) 토큰, (\d+)초")
    for l in f.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = pat.search(l)
        if m:
            k = (int(m.group(1)), m.group(2))
            out[k]["calls"] += 1
            out[k]["in"] += int(m.group(3))
            out[k]["out"] += int(m.group(4))
            out[k]["sec"] += int(m.group(5))
        elif "429" in l and "기다렸다" in l:
            out[(0, "_all")]["waits"] += 1
    return out


def collection_time_rows(run: Dict[str, Any], providers: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """모델별 답변 수집 — 받은 질문 수·보류·오류, 답변 시간 합, 질문당 평균, 수집 묶음 벽시계 합 (modelgrade 의 수집 끝 표와 같은 셈)."""
    from webapp.run_summary import asks_empty_output
    providers = providers or list(run.get("providers") or [])
    out = []
    for p in providers:
        rows = [r for r in (run.get("datasets") or {}).get(p) or [] if isinstance(r, dict)]
        by_q: Dict[int, List[str]] = {}
        empty_q: set = set()
        for r in rows:
            q = _qno(r["case_id"])
            by_q.setdefault(q, []).append((r.get("collection") or {}).get("status") or "")
            if asks_empty_output(None, r.get("question")):
                empty_q.add(q)
        received = sum(1 for st in by_q.values() if all(x == "SUCCESS" for x in st))
        held = sum(1 for q, st in by_q.items() if not all(x == "SUCCESS" for x in st)
                   and (q in empty_q or all(x in ("SUCCESS", "COPY_FAILED") for x in st)))
        error = len(by_q) - received - held
        answer_s = sum((r.get("collection") or {}).get("latency_ms") or 0 for r in rows) / 1000
        wall = sum(e.get("seconds") or 0 for e in run.get("imports") or [] if e.get("provider") == p and e.get("method") == "collect-app-v1")
        chars = sum(len(r.get("answer") or "") for r in rows)
        out.append({"provider": p, "questions": received, "held": held, "errors": error, "total": len(by_q), "answer_seconds": answer_s,
                    "per_question": answer_s / received if received else 0, "wall_seconds": wall, "answer_chars": chars,
                    "label": (run.get("models") or {}).get(p) or LABEL.get(p, p)})
    return out


def judge_time_info(out_dir: Path) -> Dict[str, Any]:
    f = out_dir / "timing.jsonl"
    if not f.exists():
        return {}
    recs = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    judge = [r for r in recs if str(r.get("kind") or "").startswith("judge")]
    total = sum(r.get("seconds") or 0 for r in judge)
    n = sum(r.get("judgments") or 0 for r in judge)
    return {"seconds": total, "judgments": n, "per_judgment": total / n if n else 0, "batches": len(judge)}


# ---------------------------------------------------------------- 보고서 -------------------------------------------------------------
def _orig_block(turns: List[Dict[str, Any]], only: Optional[set] = None) -> str:
    parts = []
    for t in turns:
        if only and t["turn"] not in only:
            continue
        att = f" <span class='small'>첨부: {_e(', '.join(t['attachments']))}</span>" if t["attachments"] else ""
        ref = f" <span class='small'>참조 자료 {t['reference_chars']:,}자(생략)</span>" if t["reference_chars"] else ""
        st = "" if t["status"] == "SUCCESS" else f" <span class='small FAIL'>수집 상태 {_e(t['status'])} {_e(t['error'][:120])}</span>"
        parts.append(f"<div class='two'><div><b>턴 {t['turn']} 질문</b>{att}{ref}<pre class='orig'>{_e(t['question'])}</pre></div>"
                     f"<div><b>턴 {t['turn']} 답변</b>{st}<pre class='orig'>{_e(t['answer']) or '<i>(빈 답)</i>'}</pre></div></div>")
    return "".join(parts) or "<p class='note'>원문 없음 (실행 파일을 못 읽음)</p>"


BUTTONS = (("PASS", "PASS"), ("FAIL", "FAIL"), ("NOT_APPLICABLE", "해당 없음"), ("UNVERIFIED", "계속 보류"))


def _item_html(it: Dict[str, Any], names: Dict[str, Dict[str, str]], key_attrs: str = "", buttons: bool = False) -> str:
    nm = (names.get(it["item"]) or {}).get("name") or ""
    kind = f"<span class='kind'>{_e(it['kind'])}</span>" if it.get("kind") else ""
    human = it.get("human")
    hum = f" <span class='badge b-{_e(human['new']) if human['new'] in ('PASS', 'FAIL') else 'NA'}'>사람 판정 {_e(KO.get(human['new'], human['new']))}</span>" if human else ""
    why = it["reason"] or (("; ".join(it["problems"]))[:200] if it["problems"] else it["how"])
    q = f"<div class='quote'>답 인용: {_e(it['answer_quote'][:220])}</div>" if it["answer_quote"] else ""
    qq = f"<div class='quote'>질문 인용: {_e(it['question_quote'][:160])}</div>" if it["question_quote"] else ""
    extra = (" · " + _e("; ".join(it["problems"])[:160])) if it["problems"] and it["reason"] else ""
    extra += (" · 표시: " + _e("; ".join(it.get("notes") or [])[:160])) if it.get("notes") else ""
    how = f"<div class='small'>심판: {_e(it['how'])}{extra}</div>" if (it["how"] or extra) else ""
    btn = ""
    if buttons:
        btn = "<div class='btns'>" + "".join(f"<button data-v='{v}'>{t}</button>" for v, t in BUTTONS) + " <input class='note' placeholder='메모(선택)' size='28'></div>"
    return (f"<div class='item hold-item' {key_attrs}><span class='id'>턴 {it['turn']} · {_e(it['item'])}</span> <span class='small'>{_e(nm[:70])}</span> "
            f"<span class='{_e(it['decision'])}'>{_e(KO.get(it['decision'], it['decision']))}</span>{kind}{hum}<div class='why'>{_e(why)}</div>{q}{qq}{how}{btn}</div>")


def _link(anchor: str, inner: str) -> str:
    return f"<a href='{anchor}' style='text-decoration:none'>{inner}</a>"


def write_report(out_dir: Path, run: Optional[Dict[str, Any]] = None, set_dir: Optional[Path] = None) -> Path:
    out_dir = Path(out_dir)
    run = run or {}
    run_id = run.get("run_id") or resolve_run_id(out_dir)
    results = load_results(out_dir)
    human = load_human(out_dir)
    set_dir = set_dir or set_dir_for(run)
    names = criteria_table(set_dir)
    qtypes = question_types(set_dir)
    orig = originals(run)
    code = code_detail(run)
    usage = codex_usage_from_log(out_dir)
    providers = ([p for p in ORDER if p in (run.get("providers") or [])] or [p for p in ORDER if any(r["provider"] == p for r in results)]
                 or sorted({r["provider"] for r in results}))
    total_q = int(run.get("question_count") or (len(qtypes) if qtypes else 0) or 78)
    all_q = sorted(qtypes) if qtypes else sorted({int(r["question"]) for r in results})
    model_label = {p: (run.get("models") or {}).get(p) or LABEL.get(p, p) for p in providers}

    # 모델×질문 — 반영된 판정과 항목
    lines: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for r in results:
        verdict, items, changed = effective(r, normalize_items(r), human)
        lines[(r["provider"], int(r["question"]))] = {"line": r, "items": items, "verdict": verdict, "orig_verdict": r.get("verdict"), "changed": changed}

    def tally(p: str, qs: Optional[List[int]] = None, before: bool = False) -> Counter:
        c: Counter = Counter()
        for q in (qs if qs is not None else all_q):
            L = lines.get((p, q))
            c[(L["orig_verdict"] if before else L["verdict"]) if L else "미평가"] += 1
        return c

    def fail_count(p: str, q: int) -> int:
        L = lines.get((p, q))
        return (len({i["item"] for i in L["items"] if i["decision"] == "FAIL"}) + len(L["line"].get("code_fails") or [])) if L else 0

    def sort_key(L: Dict[str, Any]) -> Tuple[int, int]:
        p = L["line"]["provider"]
        return int(L["line"]["question"]), providers.index(p) if p in providers else 9

    judged_n = {p: sum(1 for q in all_q if (p, q) in lines) for p in providers}
    complete = all(judged_n[p] == len(all_q) for p in providers)
    cta = os.environ.get("AIONA_SIGNUP_URL") or "https://aiona.kr"
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    judge_txt = "Decisions API(OpenAI gpt-6-luna) 전수 판정 + Codex CLI(gpt-6-luna, low, 웹 검색) 재확인 — 심판은 모델 이름을 모릅니다(블라인드)"

    P: List[str] = [f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                    f"<title>AIONA 채팅 품질 평가 결과</title><style>{CSS}</style></head><body data-run='{_e(run_id)}' data-policy='{POLICY}'><div class='wrap'>"]
    P.append(f"<h1>AIONA 채팅 품질 평가 결과</h1><div class='meta'>실행 {_e(run_id)} · 질문 {total_q}개 · 평가 기준 72개(CODE 28 + 의미 44) · 심판: {judge_txt} · 보고서 생성 {gen}"
             f"{'' if complete else ' · <b class=HOLD>평가 진행 중(일부 질문 미평가)</b>'}</div>")
    P.append("<p class='lead'>같은 질문 78개를 네 모델에 똑같이 던지고, 답변마다 72개 기준으로 검사했습니다. <b>완전 통과</b>는 그 질문의 72개 기준을 하나도 어기지 않은 경우입니다. "
             "10턴 대화는 열 번의 답 모두가 검사 대상이라 기준이 엄격합니다. 숫자는 모두 <b>질문 수 / 78</b> 입니다.</p>")

    # 1. 한눈에
    P.append("<h2>1. 한눈에 보기 — 완전 통과 질문 수</h2><div class='cards'>")
    for p in providers:
        t = tally(p)
        pct = t["PASS"] / total_q * 100
        P.append(f"<div class='card {p}'><div class='name'>{_e(model_label[p])}</div><div class='big'>{t['PASS']} <small>/ {total_q}</small></div>"
                 f"<div class='bar'><i style='width:{pct:.1f}%'></i></div><div class='sub'>완전 통과 {pct:.1f}% · FAIL {t['FAIL']} · 보류 {t['HOLD']}"
                 f"{' · 미평가 ' + str(t['미평가']) if t['미평가'] else ''}</div></div>")
    P.append("</div>")
    if human:
        P.append("<table><tr><th>모델</th><th class='num'>사람 판정 전 통과</th><th class='num'>후 통과</th><th class='num'>전 보류</th><th class='num'>후 보류</th><th class='num'>바뀐 항목</th></tr>")
        for p in providers:
            b, a = tally(p, before=True), tally(p)
            P.append(f"<tr><td>{_e(model_label[p])}</td><td class='num'>{b['PASS']}</td><td class='num'>{a['PASS']}</td><td class='num'>{b['HOLD']}</td><td class='num'>{a['HOLD']}</td>"
                     f"<td class='num'>{sum(L['changed'] for k, L in lines.items() if k[0] == p)}</td></tr>")
        P.append("</table><p class='note'>사람 판정(human_decisions.json)을 반영한 뒤의 숫자입니다. 규칙은 심판과 같아서 보류 하나를 PASS 로 바꿔도 다른 FAIL 이 남으면 그 질문은 FAIL 입니다.</p>")

    # 기준별 어긴 질문 수 (여러 곳에서 씀)
    crit_fail: Dict[str, Counter] = defaultdict(Counter)
    crit_hold: Dict[str, Counter] = defaultdict(Counter)
    for (p, q), L in lines.items():
        for c in {i["item"] for i in L["items"] if i["decision"] == "FAIL"} | set(L["line"].get("code_fails") or []):
            crit_fail[c][p] += 1
        for c in {i["item"] for i in L["items"] if i["decision"] == "UNVERIFIED"}:
            crit_hold[c][p] += 1

    # AIONA 강점 — 같은 지표로 네 모델 비교, 사실일 때만
    if "aiona" in providers and len(providers) > 1:
        wins: List[str] = []
        others = [p for p in providers if p != "aiona"]
        for ty in TYPE_ORDER:
            qs = [q for q in all_q if qtypes.get(q) == ty]
            if not qs or not all((p, q) in lines for p in providers for q in qs):
                continue
            a = tally("aiona", qs)["PASS"]
            o = max(tally(p, qs)["PASS"] for p in others)
            if a > o:
                wins.append(f"{ty}: AIONA {a}/{len(qs)} 완전 통과 (다른 모델 최고 {o}/{len(qs)})")
        avg = {p: (sum(fail_count(p, q) for q in all_q if (p, q) in lines) / judged_n[p]) if judged_n[p] else None for p in providers}
        if complete and avg["aiona"] is not None and all(avg[p] is not None and avg["aiona"] < avg[p] for p in others):
            wins.append(f"질문당 어긴 기준 수 평균이 가장 적음: AIONA {avg['aiona']:.1f}개 (다른 모델 {min(avg[p] for p in others):.1f}~{max(avg[p] for p in others):.1f}개)")
        if complete:
            better = sorted([(min(crit_fail[c][p] for p in others) - crit_fail[c]["aiona"], c) for c in crit_fail
                             if crit_fail[c]["aiona"] < min(crit_fail[c][p] for p in others)], reverse=True)[:5]
            for margin, c in better:
                wins.append(f"기준 {c} ({(names.get(c) or {}).get('name', '')[:30]}): AIONA 가 어긴 질문 {crit_fail[c]['aiona']}개, 다른 모델은 최소 {crit_fail[c]['aiona'] + margin}개")
        body = ("<ul>" + "".join(f"<li>{_e(w)}</li>" for w in wins) + "</ul>") if wins else "<p class='note'>아직 앞선 항목이 확인되지 않았습니다.</p>"
        P.append("<div class='strength'><b>AIONA 가 앞선 부분</b> <span class='small'>(같은 질문·같은 기준으로 네 모델을 비교해 AIONA 가 가장 좋은 항목만 적습니다"
                 f"{'' if complete else ' · 평가가 끝나면 채워집니다'})</span>{body}<a class='cta' href='{_e(cta)}' target='_blank' rel='noopener'>AIONA 직접 써 보기 →</a></div>")

    # 2. 유형별
    P.append("<h2>2. 질문 유형별 완전 통과</h2><table><tr><th>유형</th><th class='num'>질문 수</th>" + "".join(f"<th class='num'>{_e(model_label[p])}</th>" for p in providers) + "</tr>")
    for ty in TYPE_ORDER + sorted({t for t in qtypes.values() if t not in TYPE_ORDER}):
        qs = [q for q in all_q if qtypes.get(q) == ty]
        if not qs:
            continue
        best = max(tally(p, qs)["PASS"] for p in providers)
        cells = []
        for p in providers:
            t = tally(p, qs)
            extra = (f" <span class='small'>(보류 {t['HOLD']})</span>" if t["HOLD"] else "") + (f" <span class='small'>(미평가 {t['미평가']})</span>" if t["미평가"] else "")
            cells.append(f"<td class='num{' best' if t['PASS'] == best and best > 0 else ''}'>{t['PASS']} / {len(qs)}{extra}</td>")
        P.append(f"<tr><td>{_e(ty)}</td><td class='num'>{len(qs)}</td>{''.join(cells)}</tr>")
    P.append("</table><p class='note'>'명시적 빈 출력'은 답을 내지 말라는 질문입니다. 모델이 아무것도 안 내거나 오류를 내면 규칙상 보류로 두고 사람이 봅니다.</p>")

    # 3. 기준별
    P.append("<h2>3. 기준 72개별 — 어긴 질문 수</h2><p class='note'>질문 하나에서 같은 기준을 여러 턴에서 어겨도 1로 셉니다. 괄호는 보류 질문 수. 숫자가 작을수록 좋습니다.</p>")
    P.append("<details><summary>기준 72개 표 펼치기</summary><table><tr><th>기준</th><th>이름</th>" + "".join(f"<th class='num'>{_e(model_label[p])}</th>" for p in providers) + "</tr>")
    ids = sorted(set(names) | set(crit_fail) | set(crit_hold), key=lambda c: (0 if c[0].isdigit() else 1, c))
    last_grp = None
    for c in ids:
        grp = (names.get(c) or {}).get("group") or ("CODE" if c[0].isdigit() else c.split("-")[0])
        if grp != last_grp:
            P.append(f"<tr class='grp'><td colspan='{2 + len(providers)}'>{_e(grp)}</td></tr>")
            last_grp = grp
        mn = min(crit_fail[c][p] for p in providers)
        mx = max(crit_fail[c][p] for p in providers)
        P.append(f"<tr><td>{_e(c)}</td><td class='qtxt'>{_e((names.get(c) or {}).get('name', '')[:60])}</td>" + "".join(
            f"<td class='num{' best' if crit_fail[c][p] == mn and mx > mn else ''}'>{crit_fail[c][p]}" + (f" <span class='small'>({crit_hold[c][p]})</span>" if crit_hold[c][p] else "") + "</td>"
            for p in providers) + "</tr>")
    P.append("</table></details>")

    # 4. 시간·토큰
    P.append("<h2>4. 걸린 시간과 토큰</h2>")
    crow = collection_time_rows(run, providers) if run.get("datasets") else []
    if crow:
        P.append("<h3>답변 수집</h3><table><tr><th>모델</th><th class='num'>받음</th><th class='num'>보류</th><th class='num'>오류·답 없음</th><th class='num'>합계</th><th class='num'>답변 시간 합</th><th class='num'>질문당 평균</th><th class='num'>답변 글자 수</th><th class='num'>수집 벽시계</th></tr>")
        for c in crow:
            P.append(f"<tr><td>{_e(c['label'])}</td><td class='num'>{c['questions']}</td><td class='num'>{c['held']}</td><td class='num'>{c['errors']}</td><td class='num'>{c['total']}</td>"
                     f"<td class='num'>{_hms(c['answer_seconds'])}</td><td class='num'>{c['per_question']:.0f}초</td><td class='num'>{c['answer_chars']:,}</td><td class='num'>{_hms(c['wall_seconds']) if c['wall_seconds'] else '미측정'}</td></tr>")
        P.append("</table>")
    P.append("<h3>판정</h3><table><tr><th>모델</th><th class='num'>판정한 질문</th><th class='num'>판정 시간 합</th><th class='num'>질문당 평균</th><th class='num'>Decisions 입력 토큰</th><th class='num'>Codex 호출</th><th class='num'>Codex 입력 토큰</th><th class='num'>Codex 출력 토큰</th></tr>")
    for p in providers:
        mine = [L for (pp, q), L in lines.items() if pp == p]
        if not mine:
            P.append(f"<tr><td>{_e(model_label[p])}</td><td class='num'>0</td><td colspan='6' class='small'>아직 없음</td></tr>")
            continue
        sec = sum(L["line"].get("seconds") or 0 for L in mine)
        dtok = sum(int((((L["line"].get("calls") or {}).get("decisions") or {}).get("input_tokens") or 0)) for L in mine)
        cu = [usage[(q, p)] for (pp, q) in lines if pp == p and (q, p) in usage]
        P.append(f"<tr><td>{_e(model_label[p])}</td><td class='num'>{len(mine)}</td><td class='num'>{_hms(sec)}</td><td class='num'>{sec / len(mine):.0f}초</td>"
                 f"<td class='num'>{dtok:,}</td><td class='num'>{sum(u['calls'] for u in cu)}</td><td class='num'>{sum(u['in'] for u in cu):,}</td><td class='num'>{sum(u['out'] for u in cu):,}</td></tr>")
    waits = usage[(0, "_all")]["waits"] if (0, "_all") in usage else 0
    jt = judge_time_info(out_dir)
    P.append("</table>" + (f"<p>판정 시간(벽시계): 총 <b>{_hms(jt['seconds'])}</b> · 판정 {jt['judgments']}건(질문×모델) · 건당 평균 {jt['per_judgment']:.0f}초</p>" if jt else ""))
    P.append(f"<p class='note'>판정 시간은 질문×모델 하나를 끝내는 데 걸린 시간의 합(네 모델이 동시에 돌아 실제 벽시계는 이보다 짧음). Codex 토큰은 run.log 에서 모은 값"
             f"{' · 429 대기 ' + str(waits) + '회' if waits else ''}. 토큰 수가 없는 칸은 미측정.</p>")

    # 5. 보류
    hold_lines = sorted([L for L in lines.values() if L["verdict"] == "HOLD"], key=sort_key)
    kind_counter: Dict[str, Counter] = defaultdict(Counter)
    inside_fail: Counter = Counter()
    for (p, q), L in lines.items():
        for it in L["items"]:
            if it["decision"] == "UNVERIFIED":
                kind_counter[p][it["kind"] or "기타"] += 1
                if L["verdict"] == "FAIL":
                    inside_fail[p] += 1
    P.append(f"<h2>5. 보류 — 사람이 정하는 것</h2><p class='lead'>심판이 확정하지 못한 질문 {len(hold_lines)}건입니다. 질문 원문과 답변 원문, 보류 사유를 보고 항목마다 PASS/FAIL 을 눌러 주세요. "
             "판단은 이 브라우저에 저장되고, '판단 내보내기'로 JSON 을 받아 <code>python tools/modelgrade.py held-apply --dir &lt;결과 폴더&gt; --file &lt;받은 JSON&gt;</code> 로 반영하면 보고서가 다시 만들어집니다.</p>")
    P.append("<table><tr><th>모델</th><th class='num'>보류 질문</th>" + "".join(f"<th class='num'>{k}</th>" for k in HOLD_KINDS[:4]) + "<th class='num'>FAIL 질문 안의 보류 항목</th></tr>")
    for p in providers:
        P.append(f"<tr><td>{_e(model_label[p])}</td><td class='num'>{tally(p)['HOLD']}</td>" + "".join(f"<td class='num'>{kind_counter[p][k]}</td>" for k in HOLD_KINDS[:4]) + f"<td class='num'>{inside_fail[p]}</td></tr>")
    P.append("</table><p class='note'>보류 종류 — <b>심판 출력 누락</b>: Codex 가 그 항목의 답을 안 냄(우리 쪽 문제, 다시 물으면 됨) · <b>인용 불일치</b>: 심판이 든 근거 문장이 원문에 없음 · "
             "<b>확인 못함</b>: 심판이 사실을 확인하지 못했다고 함 · <b>심판 갈림</b>: 두 심판이 확신을 갖고 반대로 판정. FAIL 질문 안의 보류 항목은 그 질문이 어차피 FAIL 이라 숫자를 바꾸지 않습니다.</p>")
    if hold_lines:
        P.append("<div class='toolbar'>검토자 <input id='reviewer' placeholder='이름'> <span class='small'>판단한 항목 <b id='decided'>0</b>개</span> <button id='export'>판단 내보내기(JSON)</button> "
                 "<button id='clear' style='background:#fff;color:#333;border-color:#bbb'>저장된 판단 지우기</button></div>")
    for L in hold_lines:
        r, p, q = L["line"], L["line"]["provider"], int(L["line"]["question"])
        held = [it for it in L["items"] if it["decision"] == "UNVERIFIED"]
        turns_needed = {it["turn"] for it in held} or None
        P.append(f"<details class='q' id='hold-{p}-{q}'><summary>Q{q} · {_e(model_label[p])} · {_e(qtypes.get(q, ''))} · <span class='HOLD'>보류</span> 항목 {len(held)}개</summary>")
        if r.get("status") not in (None, "judged"):
            P.append(f"<p><b>질문 전체 보류</b> — {_e(r.get('reason') or r.get('collection_note') or '')}</p>")
            P.append(_item_html({"turn": 1, "item": "질문 전체", "decision": "UNVERIFIED", "how": r.get("reason") or "", "reason": r.get("collection_note") or r.get("reason") or "",
                                 "answer_quote": "", "question_quote": "", "problems": [], "kind": "질문 전체 보류"}, names,
                                f"data-key='{p}|{q}|1|__question__' data-provider='{p}' data-question='{q}' data-turn='1' data-item='__question__' data-old='UNVERIFIED'", buttons=True))
        else:
            P.append("<div class='small'>한꺼번에: <span class='btns'><button class='allbtn' data-v='PASS'>이 질문의 보류 전부 PASS</button><button class='allbtn' data-v='FAIL'>전부 FAIL</button></span></div>")
            for it in held:
                P.append(_item_html(it, names, f"data-key='{p}|{q}|{it['turn']}|{it['item']}' data-provider='{p}' data-question='{q}' data-turn='{it['turn']}' data-item='{_e(it['item'])}' data-old='{_e(it['decision'])}'", buttons=True))
        P.append(f"<h3>원문</h3>{_orig_block(orig.get((p, q)) or [], turns_needed)}</details>")

    # 6. FAIL 상세
    fail_lines = sorted([L for L in lines.values() if L["verdict"] == "FAIL"], key=sort_key)
    P.append(f"<h2>6. FAIL 상세 — 어떤 기준을 어떻게 어겼나 ({len(fail_lines)}건)</h2><p class='note'>어긴 기준과 심판이 든 근거(원문 인용), 그 아래 질문·답변 원문입니다. 펼쳐서 확인하세요.</p>")
    for L in fail_lines:
        r, p, q = L["line"], L["line"]["provider"], int(L["line"]["question"])
        fails = [it for it in L["items"] if it["decision"] == "FAIL"]
        code_f = [c for c in (code.get((p, q)) or []) if c["verdict"] == "FAIL"]
        holds_in = [it for it in L["items"] if it["decision"] == "UNVERIFIED"]
        pre = f" · {_e(r.get('reason'))}" if r.get("status") not in (None, "judged") else ""
        P.append(f"<details class='f' id='fail-{p}-{q}'><summary>Q{q} · {_e(model_label[p])} · {_e(qtypes.get(q, ''))} · <span class='FAIL'>FAIL</span> 어긴 기준 {len({i['item'] for i in fails}) + len({c['check_id'] for c in code_f})}개"
                 f"{' · 보류 항목 ' + str(len(holds_in)) + '개' if holds_in else ''}{pre}</summary>")
        for c in code_f:
            P.append(f"<div class='item'><span class='id'>턴 {c['turn']} · CODE {_e(c['check_id'])}</span> <span class='small'>{_e((names.get(c['check_id']) or {}).get('name', ''))}</span> <span class='FAIL'>FAIL</span><div class='why'>{_e(c['reason'])}</div></div>")
        for it in fails:
            P.append(_item_html(it, names))
        if holds_in:
            P.append(f"<details><summary>보류 항목 {len(holds_in)}개 (이 질문은 FAIL 이라 숫자에 영향 없음)</summary>" + "".join(_item_html(it, names) for it in holds_in) + "</details>")
        P.append(f"<h3>원문</h3>{_orig_block(orig.get((p, q)) or [], ({it['turn'] for it in fails} | {c['turn'] for c in code_f}) or None)}</details>")

    # 7. 질문별 표
    P.append("<h2>7. 질문별 판정표</h2><table><tr><th>질문</th><th>유형</th><th class='qtxt'>질문 글(앞부분)</th>" + "".join(f"<th>{_e(model_label[p])}</th>" for p in providers) + "</tr>")
    for q in all_q:
        qt = next((t["question"] for p in providers for t in (orig.get((p, q)) or []) if t["turn"] == 1), "")
        cells = []
        for p in providers:
            L = lines.get((p, q))
            if not L:
                cells.append("<td class='small'>미평가</td>")
                continue
            v = L["verdict"]
            note = L["line"].get("collection_note") or (L["line"].get("reason") if L["line"].get("status") not in (None, "judged") else "") or ""
            txt = (f"<span class='badge b-{v}'>{KO.get(v, v)}</span>" + (f" <span class='small'>{fail_count(p, q)}개 어김</span>" if v == "FAIL" else "")
                   + (f"<div class='small'>{_e(note[:120])}</div>" if note else ""))
            cells.append(f"<td>{_link(f'#fail-{p}-{q}', txt) if v == 'FAIL' else (_link(f'#hold-{p}-{q}', txt) if v == 'HOLD' else txt)}</td>")
        P.append(f"<tr><td>Q{q}</td><td class='small'>{_e(qtypes.get(q, ''))}</td><td class='qtxt small'>{_e(qt[:80])}</td>{''.join(cells)}</tr>")
    P.append("</table>")

    # 8. 방법
    P.append("<h2>8. 어떻게 평가했나</h2><ul>"
             "<li><b>질문 78개</b>: 단일 질문 35 · 10턴 대화 11 · 첨부 자료 12 · 롱컨텍스트(27만 토큰 이상) 10 · 빈 출력 요구 10. 네 모델에 같은 질문을 같은 순서로 던지고 답을 복사해 저장했습니다.</li>"
             "<li><b>기준 72개</b>: 형식·개수·글자 수 같은 CODE 기준 28개는 프로그램이 검사하고, 요청 충실·지시 준수·논리·완결성·사실·멀티턴·롱컨텍스트·첨부 활용의 의미 기준 44개는 AI 심판이 턴마다 검사합니다.</li>"
             "<li><b>심판 둘</b>: Decisions API 가 항목 전부를 판정하고, FAIL 이거나 확신이 낮은 항목만 Codex 가 원문 인용과 함께 다시 봅니다. 다시 본 항목은 Codex 판정을 우선하되 두 심판이 확신을 갖고 갈리면 보류로 둡니다. 심판은 어느 모델의 답인지 모릅니다.</li>"
             "<li><b>완전 통과</b>: CODE·의미 기준 중 하나라도 FAIL 이면 그 질문은 FAIL. FAIL 은 없고 보류만 있으면 보류(사람이 정함). 빈 출력 요구 질문은 빈 답·오류 모두 보류.</li>"
             f"<li>정책 버전 {POLICY} · 평가 프롬프트 참고자료/프롬프트/judge_prompt_v4.txt · 원본 결과 results.jsonl</li></ul>")
    P.append(f"</div><script>{JS}</script></body></html>")
    path = out_dir / "report.html"
    path.write_text("\n".join(P), encoding="utf-8")
    return path
