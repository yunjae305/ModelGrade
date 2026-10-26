"""판정 결과 HTML 보고서 (v2, 2026-10-20 밤) — `modelgrade report --dir data/judge_runs/<실행>/<out>` 가 쓴다.

일반인이 봐도 바로 읽히게: 한눈에 보기(완전 통과 문항 수 / 177 — 멀티턴은 턴마다 1문항, 10-26 저녁) → 유형별 → 기준 72개별 → 걸린 시간·토큰 → 보류(사람이 정함: 질문 원문·답변 원문·
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
DEFAULT_SET = ROOT / "참고자료" / "질문세트_78"
LABEL = {"aiona": "AIONA", "aiona2": "AIONA 2", "gpt": "GPT", "claude": "Claude", "gemini": "Gemini"}
ORDER = ["aiona", "aiona2", "gpt", "claude", "gemini"]   # aiona2 = AIONA 두 번째 자리(두 모델 비교 · 업데이트 전후, 10-26)
KO = {"PASS": "통과", "FAIL": "FAIL", "HOLD": "보류", "UNVERIFIED": "보류", "NOT_APPLICABLE": "해당 없음", "ERROR_CASE": "에러 케이스"}
TYPE_ORDER = ["단일 질문", "10턴 멀티턴 대화", "롱컨텍스트", "첨부자료 확인·요약"]   # 10-21 오후 미팅: 싱글턴 · 멀티턴 · 롱컨텍스트 · 첨부


def display_type(t: str) -> str:
    """보고서에 묶어 보여 줄 유형 — 빈 출력 요구는 단일 질문에 합친다 (10-21 오후 미팅). 보류 규칙은 원래 유형으로."""
    return "단일 질문" if t == "명시적 빈 출력" else t
HOLD_KINDS = ("심판 출력 누락", "인용 불일치", "확인 못함", "심판 갈림", "사유 모순", "규칙 오적용", "질문 전체 보류", "기타")
#: 판정(judge_v4 의 안전장치)이 항목에 직접 붙이는 보류 종류 — 다시 짐작하지 않고 그대로 쓴다
#: (10-22 기준 점검: 반박 출처 없는 사실 FAIL 을 '확인 못함'으로 돌린 9칸이 보고서에서 '기타'로 셌던 것)
GUARD_KINDS = ("확인 못함", "사유 모순", "규칙 오적용", "에러 케이스 확인", "코드 판정 확인", "복사 실패")   # 복사 실패: 멀티턴에서 한 턴만 못 받음 — 그 턴만 보류(10-26 저녁)
CODE_HOLD_KIND = "코드 판정 확인"   # CODE 가 정하기 애매한 조건(이스케이프 불릿 · 불릿 묶음 여럿 · 자기 점검 금지어 · 서비스 꼬리글) — 사람이 PASS · FAIL + 사유 (10-26)
ERROR_SUSPECT_KIND = "에러 케이스 확인"   # CODE 0-1 이 보류한 에러 케이스 의심 턴 — 사람이 에러 케이스 · PASS · FAIL 을 고른다 (사용자 10-26)
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
.btns button.on[data-v=PASS]{background:var(--pass)}.btns button.on[data-v=FAIL]{background:var(--fail)}.btns button.on[data-v=NOT_APPLICABLE]{background:#777}.btns button.on[data-v=UNVERIFIED]{background:var(--hold)}.btns button.on[data-v=ERROR_CASE]{background:#7b3fb8}
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
        from code_eval.model import code_group   # CODE 검사 ID → 소분류 (10-26 에러 케이스 대분류)
        for c in cat.get("code") or []:
            out[c["id"]] = {"name": c.get("name") or c["id"], "group": "CODE " + code_group(c["id"])}
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


#: 문항 — 싱글턴 질문은 질문 하나가 1문항, 멀티턴은 턴마다 1문항(Q3-1 · Q3-2 …). 보고서 구분은 싱글턴 · 멀티턴 · 롱컨텍스트 셋뿐
#: (첨부 · 빈 출력 요구 질문은 싱글턴) — 사용자 10-26 저녁 · 10-26 미팅(대화 하나를 통째로 PASS · FAIL 하면 한 턴만 틀려도 대화 전체가 FAIL).
MULTI_TYPE = "10턴 멀티턴 대화"
UNIT_ORDER = ["싱글턴", "멀티턴", "롱컨텍스트"]


def unit_type(qtype: str) -> str:
    """질문 유형 → 문항 구분(싱글턴 · 멀티턴 · 롱컨텍스트)."""
    return "멀티턴" if qtype == MULTI_TYPE else "롱컨텍스트" if qtype == "롱컨텍스트" else "싱글턴"


def question_turns(set_dir: Path) -> Dict[int, int]:
    """질문마다 턴 수 — questions.json 의 turns."""
    f = Path(set_dir) / "questions.json"
    return {int(q["id"]): max(1, len(q.get("turns") or [])) for q in json.loads(f.read_text(encoding="utf-8"))} if f.exists() else {}


def units_of(qtypes: Dict[int, str], n_turns: Dict[int, int]) -> List[Tuple[int, int]]:
    """문항 목록 — 싱글턴 질문은 (q, 0), 멀티턴은 (q, 1) … (q, n)."""
    out: List[Tuple[int, int]] = []
    for q in sorted(qtypes):
        out += [(q, t) for t in range(1, n_turns.get(q, 1) + 1)] if qtypes[q] == MULTI_TYPE else [(q, 0)]
    return out


def unit_label(q: int, t: int) -> str:
    """문항 이름 — 'Q11' · 'Q3-2'."""
    return f"Q{q}-{t}" if t else f"Q{q}"


def turn_verdicts(line: Dict[str, Any], verdict: str, items: List[Dict[str, Any]], n_turns: int, code_fail_turns: set) -> Dict[int, str]:
    """멀티턴 문항(턴)마다 결론 — 질문과 같은 규칙(보류 우선 → FAIL → PASS)을 그 턴의 항목 · CODE FAIL 에만 쓴다.
    판정 전에 정한 줄(빈 출력 요구 · 답 없음 등) · 판정 실행 오류는 턴을 나눌 판정이 없어 모든 턴이 질문 결론."""
    if line.get("status") not in (None, "judged") or line.get("judge_error"):
        return {t: verdict for t in range(1, n_turns + 1)}
    out: Dict[int, str] = {}
    for t in range(1, n_turns + 1):
        decs = {i["decision"] for i in items if i["turn"] == t}
        out[t] = "HOLD" if "UNVERIFIED" in decs else "FAIL" if ("FAIL" in decs or t in code_fail_turns) else "PASS"
    return out


def code_fail_turns(code_fails: List[str], rows: List[Dict[str, Any]], n_turns: int) -> set:
    """질문의 CODE FAIL(code_fails)이 난 턴 — CODE 결과 행(turn · check_id · verdict)에서 찾고, 못 찾은 FAIL 은 모든 턴에(질문 결론과 어긋나지 않게)."""
    want = set(code_fails or [])
    turns = {r["turn"] for r in rows if r.get("verdict") == "FAIL" and r.get("check_id") in want}
    found = {r.get("check_id") for r in rows if r.get("verdict") == "FAIL"}
    return turns | (set(range(1, n_turns + 1)) if want - found else set())


def collection_log_html(lines: Optional[List[str]]) -> str:
    """수집 글 로그(tools.collection_log.log_lines) — 답을 못 받았거나 빈 답인 턴의 상태 · 오류 · 기다린 시간 · 답 자리에 보인 글 (사용자 10-26 저녁:
    이미지 대신 글로). 줄이 없으면 빈 글."""
    if not lines:
        return ""
    return ("<div class='small' style='margin-top:6px'><b>수집 로그</b></div>"
            f"<pre class='orig' style='background:#f6f6f6;white-space:pre-wrap'>{_e(chr(10).join(lines))}</pre>")


from tools.collection_log import log_lines, needs_log  # noqa: E402 — 수집 글 로그 (10-26 저녁)

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
                                                  "error": (r.get("collection") or {}).get("error") or "",
                                                  "log": (collection_log_html(log_lines(r.get("collection"))) if needs_log(r.get("collection"), r.get("answer") or "") else "")})
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
    if v.get("kind") in GUARD_KINDS:
        return v["kind"]
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
                        "kind": hold_kind(v) if v.get("decision") == "UNVERIFIED" else "", "answer_text": v.get("answer_text") or "", "code_hold": v.get("code_hold") or ""})
    return out


def effective(line: Dict[str, Any], items: List[Dict[str, Any]], human: Dict[Tuple[str, int, int, str], Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]], int]:
    """사람 판정을 반영한 질문 판정 — (판정, 반영된 항목들, 바뀐 항목 수). 규칙은 심판과 같다(사용자 10-21: 보류 우선):
    보류(확인 못 함 · 판정 실행 오류)가 하나라도 남으면 HOLD, 아니면 CODE FAIL 또는 항목 FAIL → FAIL, 그 밖 PASS."""
    whole = human.get((line["provider"], int(line["question"]), 1, "__question__"))
    if line.get("status") not in (None, "judged"):
        if whole and whole.get("new") in ("PASS", "FAIL", "ERROR_CASE"):
            return ("FAIL" if whole["new"] == "ERROR_CASE" else whole["new"]), items, 1   # 에러 케이스 = 결과물 없음 → FAIL (사용자 10-26)
        return line.get("verdict") or "HOLD", items, 0
    from code_eval import error_cases
    changed = 0
    new_items = []
    err_turns = set()   # 사람이 에러 케이스로 정한 턴 — J1-1.a 만 FAIL, 나머지 해당 없음 (판정 도구의 에러 케이스 턴과 같은 규칙)
    for it in items:
        h = human.get((line["provider"], int(line["question"]), it["turn"], it["item"]))
        if not h and it.get("kind") == ERROR_SUSPECT_KIND and it.get("answer_text"):   # 같은 글을 사람이 이미 정했으면 그대로 (에러 케이스 기록)
            lab = error_cases.lookup(it["answer_text"])
            h = {"new": lab["label"], "reviewer": lab.get("reviewer") or "", "note": "에러 케이스 기록의 같은 글", "from_labels": True} if lab else None
        if h and h.get("new") == "ERROR_CASE":
            err_turns.add(it["turn"])
            it = {**it, "decision": "FAIL", "human": h, "kind": "사람 판정 · 에러 케이스", "error_case": True}
            changed += 1
        elif h and h.get("new") in ("PASS", "FAIL", "NOT_APPLICABLE", "UNVERIFIED") and h["new"] != it["decision"]:
            it = {**it, "decision": h["new"], "human": h, "kind": "사람 판정" if h["new"] != "UNVERIFIED" else it["kind"]}
            changed += 1
        new_items.append(it)
    if err_turns:
        new_items = [it if it["turn"] not in err_turns or it.get("error_case") else
                     {**it, "decision": "FAIL" if it["item"] == "J1-1.a" else "NOT_APPLICABLE", "kind": "에러 케이스 턴"} for it in new_items]
    decs = {it["decision"] for it in new_items}
    verdict = "HOLD" if (line.get("judge_error") or "UNVERIFIED" in decs) else ("FAIL" if (line.get("code_fails") or "FAIL" in decs) else "PASS")
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


def judge_runs(out_dir: Path) -> List[Dict[str, Any]]:
    """timing.jsonl 의 판정 실행 기록 — 실행 순서대로 (다시 판정 표시 redo 포함)."""
    f = Path(out_dir) / "timing.jsonl"
    if not f.exists():
        return []
    recs = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in recs if str(r.get("kind") or "").startswith("judge")]


def judge_time_info(out_dir: Path) -> Dict[str, Any]:
    f = out_dir / "timing.jsonl"
    if not f.exists():
        return {}
    judge = [r for r in judge_runs(out_dir) if not r.get("redo")]   # 다시 판정(--redo)은 뺀다 — 처음부터 제대로 받았다면 없었을 실행 (사용자 10-21)
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
                     f"<div><b>턴 {t['turn']} 답변</b>{st}<pre class='orig'>{_e(t['answer']) or '<i>(빈 답)</i>'}</pre>{t.get('log') or ''}</div></div>")
    return "".join(parts) or "<p class='note'>원문 없음 (실행 파일을 못 읽음)</p>"


BUTTONS = (("PASS", "PASS"), ("FAIL", "FAIL"), ("NOT_APPLICABLE", "해당 없음"), ("UNVERIFIED", "계속 보류"))
#: 에러 케이스 의심 · 질문 전체 보류 — 사람이 에러 케이스 · PASS · FAIL 을 고른다(사용자 10-26). 에러 케이스 의심의 선택은 에러 케이스 기록에 쌓인다
ERROR_BUTTONS = (("ERROR_CASE", "에러 케이스"), ("PASS", "PASS"), ("FAIL", "FAIL"), ("UNVERIFIED", "계속 보류"))
CODE_BUTTONS = (("PASS", "PASS"), ("FAIL", "FAIL"), ("UNVERIFIED", "계속 보류"))


def _item_html(it: Dict[str, Any], names: Dict[str, Dict[str, str]], key_attrs: str = "", buttons: bool = False) -> str:
    nm = (names.get(it["item"]) or {}).get("name") or ("답 수집 — 다시 수집할 턴" if it["item"] == "collection.copy_failed" else "")
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
        choices = ERROR_BUTTONS if it.get("kind") in (ERROR_SUSPECT_KIND, "질문 전체 보류") else CODE_BUTTONS if it.get("kind") == CODE_HOLD_KIND else BUTTONS
        tip = "사유(기준 고칠 때 씀)" if it.get("kind") in (ERROR_SUSPECT_KIND, CODE_HOLD_KIND) else "메모(선택)"
        btn = "<div class='btns'>" + "".join(f"<button data-v='{v}'>{t}</button>" for v, t in choices) + f" <input class='note' placeholder='{tip}' size='36'></div>"
    return (f"<div class='item hold-item' {key_attrs}><span class='id'>턴 {it['turn']} · {_e(it['item'])}</span> <span class='small'>{_e(nm[:70])}</span> "
            f"<span class='{_e(it['decision'])}'>{_e(KO.get(it['decision'], it['decision']))}</span>{kind}{hum}<div class='why'>{_e(why)}</div>{q}{qq}{how}{btn}</div>")


def judge_process_info(out_dir: Path) -> Dict[str, Any]:
    """판정 실행 기록 — run.log 에서 Codex 호출·다시 묻기·429 대기(횟수·초)를, results 에서 판정 실행 오류를, summary 에서 벽시계를 센다."""
    out: Dict[str, Any] = {"codex_calls": 0, "reasks": 0, "waits": 0, "wait_seconds": 0, "judge_errors": 0, "wall_seconds": None}
    log = Path(out_dir) / "run.log"
    if log.exists():
        # 실행마다 '평가 끝' 줄로 끝난다 — 다시 판정(redo) 실행의 줄은 세지 않는다 (사용자 10-21). 실행 수가 기록과 안 맞으면 모두 센다.
        runs = judge_runs(Path(out_dir))
        segs: List[List[str]] = [[]]
        for l in log.read_text(encoding="utf-8", errors="ignore").splitlines():
            segs[-1].append(l)
            if l.startswith("평가 끝"):
                segs.append([])
        done = [s for s in segs if s and s[-1].startswith("평가 끝")]
        skip = {i for i, r in enumerate(runs) if r.get("redo")} if len(done) == len(runs) else set()
        keep = [l for i, s in enumerate(segs) if i not in skip for l in s]
        for l in keep:
            if "Codex 완료 — 입력" in l:
                out["codex_calls"] += 1
            elif "Codex 다시 묻기" in l:
                out["reasks"] += 1
            elif "429" in l and "기다렸다" in l:
                out["waits"] += 1
                m = re.search(r"(\d+)초 기다렸다", l)
                out["wait_seconds"] += int(m.group(1)) if m else 0
    out["judge_errors"] = sum(1 for r in load_results(Path(out_dir)) if r.get("judge_error"))
    jt = judge_time_info(Path(out_dir))   # 이 폴더의 판정을 모두 더한 실제 시간 — summary.json 은 마지막 실행(예: 몇 건 다시 판정)만 담는다
    out["wall_seconds"] = jt.get("seconds") or None
    out["runs"] = jt.get("batches")
    return out


def codex_reasoning(out_dir: Path) -> str:
    """이 결과의 Codex 추론 수준 — summary.json 의 codex.reasoning, 없으면 폴더 이름(…-medium 이면 medium), 그 밖은 low (10-21 전 결과)."""
    try:
        s = json.loads((Path(out_dir) / "summary.json").read_text(encoding="utf-8"))
        r = (s.get("codex") or {}).get("reasoning")
        if r:
            return str(r)
    except (OSError, ValueError):
        pass
    name = Path(out_dir).name
    for level in ("xhigh", "high", "medium"):
        if level in name:
            return level
    return "low"


def sibling_results(out_dir: Path) -> List[Tuple[str, Path]]:
    """같은 실행의 다른 판정 결과 폴더(추론 수준이 다른 것) — 전체 판정만(표본 폴더 제외)."""
    out_dir = Path(out_dir)
    sibs = []
    for d in sorted(out_dir.parent.iterdir()):
        if d.is_dir() and d != out_dir and d.name.startswith("v4") and not any(w in d.name for w in ("sample", "prev", "stopped")) and (d / "results.jsonl").exists():
            sibs.append((codex_reasoning(d), d))
    return sibs


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
        lines[(r["provider"], int(r["question"]))] = {"line": r, "items": items, "verdict": verdict, "orig_verdict": r.get("verdict"), "changed": changed,
                                                      "raw_items": normalize_items(r)}
    # 문항(싱글턴 질문 · 멀티턴 턴)마다 결론 — 숫자는 문항 수 / 177 (사용자 10-26 저녁)
    n_turns = question_turns(set_dir)
    units = units_of(qtypes, n_turns) if qtypes else [(q, 0) for q in sorted({int(r["question"]) for r in results})]
    total_u = len(units)
    uverdict: Dict[Tuple[str, int, int], str] = {}
    uverdict_before: Dict[Tuple[str, int, int], str] = {}
    for (p, q), L in lines.items():
        if qtypes.get(q) == MULTI_TYPE:
            ct = code_fail_turns(L["line"].get("code_fails") or [], code.get((p, q)) or [], n_turns.get(q, 1))
            for t, v in turn_verdicts(L["line"], L["verdict"], L["items"], n_turns.get(q, 1), ct).items():
                uverdict[(p, q, t)] = v
            for t, v in turn_verdicts(L["line"], L["orig_verdict"] or "HOLD", L["raw_items"], n_turns.get(q, 1), ct).items():
                uverdict_before[(p, q, t)] = v
        else:
            uverdict[(p, q, 0)], uverdict_before[(p, q, 0)] = L["verdict"], L["orig_verdict"]

    def utally(p: str, us: Optional[List[Tuple[int, int]]] = None, before: bool = False) -> Counter:
        src = uverdict_before if before else uverdict
        c: Counter = Counter()
        for q, t in (us if us is not None else units):
            c[src.get((p, q, t)) or "미평가"] += 1
        return c

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
    level = codex_reasoning(out_dir)
    calls = [((L["line"].get("calls") or {}).get("decisions") or {}) for L in lines.values()]
    jev_names = sorted({m for c in calls for m in ((c.get("models") or {}).get("jev") or [])})
    if any(c.get("first_judge_mode") == "stances" for c in calls):   # 사용자 10-26 저녁 — 크기로 한 심판, 판정 프롬프트 3종 가중 다수결
        first = (f"1차 Jev({', '.join(jev_names) or 'Jev'}) 또는 Decisions API(gpt-6-luna) — 질문 크기로 나눔(3만 2천 토큰 이하 · 첨부 없음이면 Jev), "
                 "판정 프롬프트 3종(비판적 · 친화적 · 유도적)으로 한 번씩 받아 가중 다수결")
    elif any(v == "jev+decisions" for c in calls for v in (c.get("first_judge") or {}).values()):   # 회의 흐름(10-26) — 실제로 쓴 1차 심판을 적는다
        first = (f"1차 Jev({', '.join(jev_names) or 'Jev'}) · Decisions API(gpt-6-luna) — 단일 질문은 둘을 교차, 멀티턴 · 첨부 · 롱컨텍스트는 Decisions")
    else:
        first = "1차 Decisions API(OpenAI gpt-6-luna) 전수 판정"
    judge_txt = f"{first} + FAIL · 애매한 것만 Codex CLI(gpt-6-luna, 추론 {level}, 웹 검색) 재확인 — 심판은 모델 이름을 모릅니다(블라인드)"

    P: List[str] = [f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                    f"<title>AIONA 채팅 품질 평가 결과</title><style>{CSS}</style></head><body data-run='{_e(run_id)}' data-policy='{POLICY}'><div class='wrap'>"]
    P.append(f"<h1>AIONA 채팅 품질 평가 결과</h1><div class='meta'>실행 {_e(run_id)} · 질문 {total_q}개 · 문항 {total_u}개(멀티턴은 턴마다) · 평가 기준 72개(에러 케이스 2 + CODE 26 + 의미 44) · 심판: {judge_txt} · 보고서 생성 {gen}"
             f"{'' if complete else ' · <b class=HOLD>평가 진행 중(일부 질문 미평가)</b>'}</div>")
    P.append("<p class='lead'>같은 질문 78개를 네 모델에 똑같이 던지고, 답변마다 72개 기준으로 검사했습니다. <b>완전 통과</b>는 그 질문에 적용되는 기준(첨부·멀티턴·형식 조건처럼 "
             "그 질문에 없는 기준은 '해당 없음'으로 제외) 가운데 FAIL 도 보류도 하나 없는 경우입니다. 숫자는 모두 <b>문항 수 / " + str(total_u) + "</b> 입니다 — 싱글턴 질문은 1문항, 10턴 대화는 턴마다 1문항(Q3-1 · Q3-2 …).</p>")

    # 1. 한눈에
    P.append("<h2>1. 한눈에 보기 — 완전 통과 문항 수</h2><div class='cards'>")
    for p in providers:
        t = utally(p)
        pct = t["PASS"] / max(1, total_u) * 100
        P.append(f"<div class='card {p}'><div class='name'>{_e(model_label[p])}</div><div class='big'>{t['PASS']} <small>/ {total_u}</small></div>"
                 f"<div class='bar'><i style='width:{pct:.1f}%'></i></div><div class='sub'>완전 통과 {pct:.1f}% · FAIL {t['FAIL']} · 보류 {t['HOLD']}"
                 f"{' · 미평가 ' + str(t['미평가']) if t['미평가'] else ''}</div></div>")
    P.append("</div>")
    if human:
        P.append("<table><tr><th>모델</th><th class='num'>사람 판정 전 통과</th><th class='num'>후 통과</th><th class='num'>전 보류</th><th class='num'>후 보류</th><th class='num'>바뀐 항목</th></tr>")
        for p in providers:
            b, a = utally(p, before=True), utally(p)
            P.append(f"<tr><td>{_e(model_label[p])}</td><td class='num'>{b['PASS']}</td><td class='num'>{a['PASS']}</td><td class='num'>{b['HOLD']}</td><td class='num'>{a['HOLD']}</td>"
                     f"<td class='num'>{sum(L['changed'] for k, L in lines.items() if k[0] == p)}</td></tr>")
        P.append("</table><p class='note'>사람 판정(human_decisions.json)을 반영한 뒤의 숫자입니다. 규칙은 심판과 같아서 보류가 하나라도 남으면 보류이고, 보류를 모두 정한 뒤 FAIL 이 하나라도 있으면 FAIL 입니다.</p>")

    # 기준별 어긴 질문 수 (여러 곳에서 씀)
    crit_fail: Dict[str, Counter] = defaultdict(Counter)
    crit_hold: Dict[str, Counter] = defaultdict(Counter)
    for (p, q), L in lines.items():
        for c in {i["item"] for i in L["items"] if i["decision"] == "FAIL"} | set(L["line"].get("code_fails") or []):
            crit_fail[c][p] += 1
        for c in {i["item"] for i in L["items"] if i["decision"] == "UNVERIFIED"}:
            crit_hold[c][p] += 1

    # AIONA 강점 — 같은 지표로 네 모델 비교, 사실일 때만
    if "aiona" in providers and len(providers) > 1 and "aiona2" not in providers:   # AIONA 끼리 비교(두 자리)에는 'AIONA 가 앞선 부분'이 맞지 않음
        wins: List[str] = []
        others = [p for p in providers if p != "aiona"]
        for ty in UNIT_ORDER:
            us = [(q, t) for q, t in units if unit_type(qtypes.get(q) or "") == ty]
            if not us or not all((p, q) in lines for p in providers for q, _t in us):
                continue
            a = utally("aiona", us)["PASS"]
            o = max(utally(p, us)["PASS"] for p in others)
            if a > o:
                wins.append(f"{ty}: AIONA {a}/{len(us)} 완전 통과 (다른 모델 최고 {o}/{len(us)}, 문항 단위)")
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
    P.append("<h2>2. 싱글턴 · 멀티턴 · 롱컨텍스트별 완전 통과</h2><table><tr><th>구분</th><th class='num'>문항 수</th>" + "".join(f"<th class='num'>{_e(model_label[p])}</th>" for p in providers) + "</tr>")
    for ty in UNIT_ORDER:
        us = [(q, t) for q, t in units if unit_type(qtypes.get(q) or "") == ty]
        if not us:
            continue
        best = max(utally(p, us)["PASS"] for p in providers)
        cells = []
        for p in providers:
            t = utally(p, us)
            extra = (f" <span class='small'>(보류 {t['HOLD']})</span>" if t["HOLD"] else "") + (f" <span class='small'>(미평가 {t['미평가']})</span>" if t["미평가"] else "")
            cells.append(f"<td class='num{' best' if t['PASS'] == best and best > 0 else ''}'>{t['PASS']} / {len(us)}{extra}</td>")
        P.append(f"<tr><td>{_e(ty)}</td><td class='num'>{len(us)}</td>{''.join(cells)}</tr>")
    P.append("</table><p class='note'>멀티턴은 턴마다 1문항입니다(그 턴의 항목 · CODE 로 보류 우선 → FAIL → 통과). '싱글턴'에는 첨부 자료 질문과 답을 내지 말라는 '명시적 빈 출력' 질문이 함께 들어 있습니다. 모델이 아무것도 안 내거나 복사할 답이 없으면 규칙상 보류로 두고 사람이 봅니다.</p>")

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
    disagree: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for (p, q), L in lines.items():
        for it in L["items"]:
            if "갈림, Codex 따름" in it["how"]:
                disagree[p].append({**it, "question": q})
    P.append("<table><tr><th>모델</th><th class='num'>보류 질문</th>" + "".join(f"<th class='num'>{k}</th>" for k in HOLD_KINDS[:5]) + "<th class='num'>FAIL 질문 안의 보류 항목</th><th class='num'>심판 갈림(Codex 따름)</th></tr>")
    for p in providers:
        P.append(f"<tr><td>{_e(model_label[p])}</td><td class='num'>{tally(p)['HOLD']}</td>" + "".join(f"<td class='num'>{kind_counter[p][k]}</td>" for k in HOLD_KINDS[:5]) + f"<td class='num'>{inside_fail[p]}</td><td class='num'>{len(disagree[p])}</td></tr>")
    P.append("</table><p class='note'>보류 종류 — <b>심판 출력 누락</b>: Codex 가 그 항목의 답을 안 냄(우리 쪽 문제, 다시 물으면 됨) · <b>인용 불일치</b>: 심판이 든 근거 문장이 원문에 없음 · "
             "<b>확인 못함</b>: 심판이 사실을 확인하지 못했다고 함 · <b>심판 갈림</b>: 두 심판이 확신을 갖고 반대로 판정 · <b>사유 모순</b>: 심판의 사유와 판정이 반대인 채 다시 물어도 남음(우리 쪽 문제). FAIL 질문 안의 보류 항목은 그 질문이 어차피 FAIL 이라 숫자를 바꾸지 않습니다.</p>")
    if any(disagree.values()):
        P.append("<details><summary>심판 갈림 — Decisions 는 FAIL, Codex 는 PASS·해당 없음이라 Codex 를 따른 항목 (숫자에는 보류로 들어가지 않음 · 확인용)</summary><table><tr><th>질문</th><th>모델</th><th>턴</th><th>항목</th><th>Codex 사유</th><th>심판</th></tr>")
        for p in providers:
            for it in sorted(disagree[p], key=lambda x: (x["question"], x["turn"])):
                P.append(f"<tr><td>Q{it['question']}</td><td>{_e(model_label[p])}</td><td>{it['turn']}</td><td>{_e(it['item'])}</td><td class='qtxt'>{_e(it['reason'][:160])}</td><td class='small'>{_e(it['how'])}</td></tr>")
        P.append("</table></details>")
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

    # 7-2. 수집·판정 과정 기록 — 수집 확인(collection-check.json)과 판정 실행 기록(run.log)
    P.append("<h2>수집·판정 과정 기록</h2>")
    cc_path = out_dir.parent / "collection-check.json"
    if cc_path.exists():
        cc = json.loads(cc_path.read_text(encoding="utf-8"))
        P.append(f"<h3>답변 수집 확인</h3><p class='note'>{_e(cc.get('rule'))} 확인 방법: {_e(cc.get('how'))}.</p>")
        P.append("<table><tr><th>모델</th><th class='num'>답이 있는 턴</th><th class='num'>다시 복사해 같음</th><th>답 아래 아이콘 글로 바꾼 턴</th><th>확인 방법</th></tr>")
        for p in providers:
            m = (cc.get("models") or {}).get(p) or {}
            rep = ", ".join(m.get("replaced") or []) or "-"
            why = f"<div class='small'>{_e(m.get('replaced_why'))}</div>" if m.get("replaced_why") else ""
            P.append(f"<tr><td>{_e(model_label[p])}</td><td class='num'>{m.get('answers', '-')}</td><td class='num'>{m.get('same', '-')}</td><td>{_e(rep)}{why}</td><td class='small'>{_e(m.get('tool'))}</td></tr>")
        P.append("</table>")
        if cc.get("chatgpt_copy_traits"):
            P.append("<p class='note'><b>ChatGPT 앱 복사 기능의 특성</b>(답 아래 복사 아이콘이 주는 글 그대로 평가함):</p><ul>" + "".join(f"<li class='small'>{_e(t)}</li>" for t in cc["chatgpt_copy_traits"]) + "</ul>")
        if cc.get("not_checked"):
            P.append(f"<p class='note'>{_e(cc['not_checked'])}</p>")
    jp = judge_process_info(out_dir)
    P.append("<h3>판정 실행</h3><table><tr><th>항목</th><th class='num'>값</th><th>설명</th></tr>"
             f"<tr><td>판정 벽시계</td><td class='num'>{_hms(jp['wall_seconds']) if jp['wall_seconds'] else '미측정'}</td><td class='small'>질문 78 × 모델 4 = 312건, 네 모델을 동시에 판정{' · 판정 실행 ' + str(jp['runs']) + '번을 더한 시간' if (jp.get('runs') or 0) > 1 else ''}</td></tr>"
             f"<tr><td>Codex 호출</td><td class='num'>{jp['codex_calls']:,}</td><td class='small'>Decisions 가 FAIL·애매로 걸러 낸 항목만 Codex 에 보낸 횟수(다시 묻기 포함)</td></tr>"
             f"<tr><td>다시 묻기</td><td class='num'>{jp['reasks']:,}</td><td class='small'>Codex 가 일부 항목에 답을 안 냈거나, 근거로 든 인용이 원문에 없거나, 사실 확인을 못 했을 때 그 항목만 다시 물음</td></tr>"
             f"<tr><td>429 대기</td><td class='num'>{jp['waits']:,}번 · {_hms(jp['wait_seconds'])}</td><td class='small'>(기다린 시간은 네 모델이 동시에 기다린 것을 모두 더한 값 — 벽시계보다 길 수 있음) 429 = Codex 서버가 '짧은 시간(약 1분)에 요청이 너무 많다'며 잠시 거절한 것. "
             "네 모델을 동시에 판정해 3~4만 토큰짜리 요청이 몰릴 때 생긴다. 바로 다시 보내면 또 거절되고, 포기하면 그 질문이 '판정 실행 오류'로 보류되므로 "
             "60초 → 120초 → 300초(그 뒤 600초마다) 기다렸다가 같은 요청을 다시 보내 끝까지 판정한다 — 잃는 것은 시간뿐.</td></tr>"
             f"<tr><td>판정 실행 오류</td><td class='num'>{jp['judge_errors']}</td><td class='small'>심판 호출이 끝내 실패해 보류로 둔 질문 수(0 이면 모든 질문이 정상 판정됨)</td></tr></table>")

    # 7-1. 추론 수준 비교 — 같은 답을 Codex 추론 수준만 바꿔 판정한 결과가 옆 폴더에 있으면
    sibs = sibling_results(out_dir)
    if sibs:
        P.append(f"<h2>추론 수준 비교 — 같은 답, 심판(Codex) 추론 수준만 다르게</h2><p class='note'>이 보고서는 추론 <b>{_e(level)}</b> 결과입니다. "
                 "아래는 같은 수집 답을 다른 추론 수준으로 판정한 결과와 나란히 둔 것입니다(완전 통과 / FAIL / 보류, 질문 수 / 78).</p>")
        head = "<tr><th>모델</th><th>" + _e(level) + "</th>" + "".join(f"<th>{_e(lv)}</th>" for lv, _ in sibs) + "<th class='num'>판정이 다른 질문</th></tr>"
        P.append("<table>" + head)
        others = []
        for lv, d in sibs:
            o = {}
            for r in load_results(d):
                v, _, _ = effective(r, normalize_items(r), load_human(d))
                o[(r["provider"], int(r["question"]))] = v
            others.append(o)
        for p in providers:
            mine = tally(p)
            cells = [f"<td>{mine['PASS']} / {mine['FAIL']} / {mine['HOLD']}" + (f" (미평가 {mine['미평가']})" if mine["미평가"] else "") + "</td>"]
            diff = 0
            for o in others:
                c = Counter(o.get((p, q), "미평가") for q in all_q)
                cells.append(f"<td>{c['PASS']} / {c['FAIL']} / {c['HOLD']}" + (f" (미평가 {c['미평가']})" if c["미평가"] else "") + "</td>")
                diff = max(diff, sum(1 for q in all_q if (p, q) in lines and (p, q) in o and lines[(p, q)]["verdict"] != o[(p, q)]))
            P.append(f"<tr><td>{_e(model_label[p])}</td>{''.join(cells)}<td class='num'>{diff}</td></tr>")
        P.append("</table>")

    # 8. 방법
    P.append("<h2>8. 어떻게 평가했나</h2><ul>"
             "<li><b>질문 78개</b>: 단일 질문 35 · 10턴 대화 11 · 첨부 자료 12 · 롱컨텍스트(27만 토큰 이상) 10 · 빈 출력 요구 10. 네 모델에 같은 질문을 같은 순서로 던지고 답을 복사해 저장했습니다.</li>"
             "<li><b>기준 72개</b>: 형식·개수·글자 수 같은 CODE 기준 28개는 프로그램이 검사하고, 요청 충실·지시 준수·논리·완결성·사실·멀티턴·롱컨텍스트·첨부 활용의 의미 기준 44개는 AI 심판이 턴마다 검사합니다.</li>"
             "<li><b>심판 둘</b>: Decisions API 가 항목 전부를 판정하고, FAIL 이거나 확신이 낮은 항목만 Codex 가 원문 인용과 함께 다시 봅니다. 다시 본 항목은 Codex 판정을 우선하되 두 심판이 확신을 갖고 갈리면 보류로 둡니다. 심판은 어느 모델의 답인지 모릅니다.</li>"
             "<li><b>완전 통과</b>: 확인 못 한 항목(보류)이 하나라도 있으면 그 질문은 보류(사람이 정함). 보류가 없고 CODE·의미 기준 중 하나라도 FAIL 이면 FAIL. 빈 출력 요구 질문은 빈 답·오류 모두 보류.</li>"
             "<li><b>답 수집</b>: 답은 각 앱·웹의 답변 아래 '복사' 버튼이 주는 글 그대로입니다(사용자가 복사해 붙여넣을 때 얻는 글). "
             "ChatGPT 앱의 복사는 코드블록 첫 줄의 언어 표기(```json → ```)를 지우므로, 질문이 'json·python 코드블록'을 지정한 문항에서 GPT 는 "
             "CODE 기준 '코드블록 언어 표시'(1-2)가 FAIL 로 나옵니다(앱 화면·앱 내부 원문에는 표기가 있음, 2026-10-21 확인).</li>"
             "<li><b>첨부 파일</b>: 두 심판 모두 원본을 이미지로 봅니다 — 사진은 그대로, pdf·docx 는 쪽마다 한 장(평가할 때만 그리고 남기지 않음), txt 는 글 그대로.</li>"
             "<li><b>심판의 흔들림</b>: 같은 답을 다시 판정하면 결과가 조금 달라질 수 있습니다. 표본 40건(질문 10개 × 4모델)을 추론 수준만 바꿔 다시 판정했을 때 "
             "질문 판정은 34건이 같고 6건이 바뀌었습니다(방향은 섞임). 모델 사이 차이가 몇 문항 이내면 이 흔들림 범위 안일 수 있습니다.</li>"
             f"<li>정책 버전 {POLICY} · 평가 프롬프트 참고자료/프롬프트/judge_prompt_v4.txt · 원본 결과 results.jsonl</li></ul>")
    P.append(f"</div><script>{JS}</script></body></html>")
    path = out_dir / "report.html"
    path.write_text("\n".join(P), encoding="utf-8")
    return path
