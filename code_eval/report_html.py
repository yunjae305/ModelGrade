"""단독 HTML 보고서. 스크립트 없음, 모든 원문은 이스케이프, 외부 리소스 로드 차단(CSP)."""

from __future__ import annotations

import html
import json
import math
import re
from collections import Counter
from typing import Any, Dict, List, Set, Tuple
from urllib.parse import urlsplit

from .combined import combined_summary
from .model import Status
from .jev_applicability import APPLIES_WHEN
from .jev_criteria import describe as describe_jev
from .llm_review import CRITERIA as LLM_REVIEW_CRITERIA, failed_case_queue
from .taxonomy import CATEGORIES, SPECIALIZED_ITEMS

__all__ = ["render_report"]

_COMMON_EXAMPLES = {
    "1-1": "응답이 비었거나, 설명 없이 깨진 글자나 한자·일본 문자가 갑자기 섞였는지",
    "1-2": "JSON 답변의 괄호가 빠져 정상적으로 읽히지 않는지",
    "1-3": "본문의 각주 번호가 실제 각주 정의와 연결되는지",
    "2-1": "불릿 3개를 요청했을 때 실제로 3개인지",
    "2-2": "요청한 표의 열 이름과 순서가 맞는지",
    "3-1": "2,000원짜리 3개의 총액을 6,000원으로 계산했는지",
    "3-2": "‘세 가지’라고 쓴 뒤 실제 항목도 세 개인지",
    "3-3": "원본 목록의 중복을 제거하면서 값과 순서를 지켰는지",
}

_LLM_EXAMPLES = {
    "task_completion": "질문에서 요구한 내용을 충족했는지",
    "relevance": "주제와 벗어난 내용이 아닌지",
    "sufficiency": "핵심 내용이 빠지지 않았는지",
    "data_interpretation": "데이터 해석이 올바른지",
    "code_fail_reason": "‘불릿 3개 부족’이라는 실패 사유가 실제 답변의 불릿 2개와 맞는지",
}

_SPECIAL_EXAMPLES = {
    "AIONA-M": "내부 처리용 태그가 고객에게 보이는 답변에 남아 있는지",
    "AIONA-F": "해당 질문에 금지된 표현이 답변에 그대로 나타나는지",
}

_SUBS = {sub["id"]: sub for category in CATEGORIES for sub in category["subs"]}

_JEV_STATUS_KO = {
    "SUCCESS": "완료", "PARTIAL": "일부 미완료", "NOT_CONFIGURED": "키 없음 · 미실행", "API_ERROR": "API 오류",
    "TIMEOUT": "시간 초과", "INVALID_RESPONSE": "응답 형식 오류", "SKIPPED": "입력 오류로 건너뜀", "NO_TARGET": "대상 없음",
}

_CSS = """
:root{--bg:#f4f6f8;--paper:#fff;--ink:#182638;--body:#38485b;--muted:#68788b;--line:#dce3eb;--blue:#2b56be;--blue-soft:#eef3ff;
--green:#23674d;--green-soft:#eff7f2;--red:#a03c45;--red-soft:#fcf0f1;--amber:#825719;--amber-soft:#fcf6e9;--violet:#6650a5;--violet-soft:#f2eefb;
--font:"Pretendard","Noto Sans CJK KR","Malgun Gothic","Apple SD Gothic Neo",sans-serif;--mono:Consolas,"Noto Sans CJK KR",monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--body);font:14px/1.7 var(--font);word-break:keep-all;overflow-wrap:anywhere}
main{max-width:1200px;margin:auto;padding:28px 16px}h1,h2,h3{color:var(--ink);letter-spacing:-.03em;margin:0}h1{font-size:28px}h2{font-size:20px;margin:28px 0 10px}
.meta{color:var(--muted);font-size:12px;margin-top:6px}.card{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:12.5px}th,td{border-bottom:1px solid var(--line);padding:7px 8px;text-align:left;vertical-align:top}
thead th{background:#eaf0f5;color:#576a80;font-size:11px}.wrap{overflow-x:auto}
.v{font:700 11px var(--mono);padding:2px 6px;border-radius:4px;border:1px solid currentColor;white-space:nowrap}
.PASS{color:var(--green);background:var(--green-soft)}.FAIL{color:var(--red);background:var(--red-soft)}.NONE{color:var(--muted);background:#eef1f4}
.st{font:11px var(--mono);color:var(--muted)}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f9fc;border:1px solid var(--line);border-radius:6px;padding:10px;font:12px/1.6 var(--mono);max-height:320px;overflow:auto;margin:6px 0}
details>summary{cursor:pointer;font-weight:700;color:var(--ink)}code{font:12px var(--mono);background:#edf1f5;padding:1px 4px;border-radius:3px}
.why{border-radius:8px;padding:8px 12px;margin:8px 0}.why.FAIL{background:var(--red-soft);border-left:4px solid var(--red)}.why.PASS{background:var(--green-soft)}.why.NONE{background:#f1f3f6}
.why p,.fix p,.vs p{margin:2px 0 0;color:var(--ink);font-weight:600}.vs{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0}.vs>div{border:1px solid var(--line);border-radius:7px;padding:6px 10px}
.fix{background:var(--blue-soft);border-radius:7px;padding:6px 10px;margin:8px 0}.res{border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin-top:8px;background:#fff}.res.FAIL{border-color:#e7b9bf}
.loc{border:1px solid var(--line);border-radius:7px;margin:6px 0;overflow:hidden}.loc-label{background:#f3f6f9;font-size:12px;font-weight:700;padding:4px 10px}
.loc-line{font:12.5px/1.8 var(--mono);padding:6px 10px;white-space:pre-wrap}mark{background:#ffd9de;color:#8f2e3b;font-weight:700}
.code-line{display:grid;grid-template-columns:36px 1fr auto;gap:8px;padding:0 10px;font:12px/1.7 var(--mono)}.code-line.mark{background:#fff0f2}.ln{color:#8291a3;text-align:right}.lt{white-space:pre-wrap}.ptr{color:var(--red);font-size:11px}
.loc-list{margin:0;padding:6px 10px 6px 30px;font-size:12.5px}.loc-list li.mark{background:#ffd9de;font-weight:700}.chip{font-size:11px;background:#e7edf6;border-radius:9px;padding:0 7px;margin-left:6px}
.tech summary{font-size:12px;color:var(--muted);cursor:pointer}.sec{display:block;margin-top:8px;font-size:12px;color:var(--muted)}
.q-full{font-weight:400;font-size:12.5px;color:var(--body);margin:4px 0 0 18px;white-space:pre-wrap}.q-cell{font-size:12px;min-width:200px;max-width:360px;white-space:pre-wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}.kpi{background:var(--paper);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.kpi b{display:block;font-size:22px;color:var(--ink)}.note{font-size:12px;color:var(--muted)}
.report-head{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;padding:12px 0 18px}.eyebrow{color:var(--blue);font-size:11px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.report-head h1{font-size:clamp(26px,3vw,38px);line-height:1.25;margin:5px 0}.report-head p{margin:0;color:var(--muted)}
.head-badge{flex:none;border-radius:999px;background:var(--blue-soft);color:var(--blue);font-weight:800;font-size:12px;padding:7px 12px}.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:32px 0 12px}.section-head h2{margin:0}.section-head p{margin:0;color:var(--muted);font-size:12px}.intro{margin:0 0 12px;color:var(--body)}
.category-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.category-card{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:19px;box-shadow:0 1px 2px #172b4808}.category-top{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.category-number{display:inline-grid;place-items:center;width:32px;height:32px;border-radius:10px;color:var(--blue);background:var(--blue-soft);font-weight:800}.category-card h3{font-size:18px;margin:12px 0 3px}.category-card>p{margin:0 0 15px;color:var(--muted);font-size:12px;min-height:40px}.mini-status{font-size:11px;font-weight:700;color:var(--muted);white-space:nowrap}.mini-status.is-fail{color:var(--red)}.criterion-list{list-style:none;padding:0;margin:0;border-top:1px solid var(--line)}.criterion-list li{padding:11px 0;border-bottom:1px solid var(--line)}.criterion-list li:last-child{border-bottom:0;padding-bottom:0}.criterion-title{display:flex;align-items:baseline;justify-content:space-between;gap:8px}.criterion-title strong{color:var(--ink);font-size:13px}.criterion-title span{font-size:11px;color:var(--muted);white-space:nowrap}.criterion-list p{margin:3px 0 0;color:var(--muted);font-size:11.5px;line-height:1.55}
.criteria-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.criterion-card{border:1px solid var(--line);border-radius:11px;background:var(--paper);padding:14px}.criterion-card .code{display:inline-block;font-weight:800;color:var(--blue);margin-bottom:7px}.criterion-card h3{font-size:14px;line-height:1.4}.criterion-card p{font-size:12px;color:var(--muted);margin:5px 0 0}
.criterion-example{margin-top:8px;padding:7px 9px;border-radius:7px;background:#f3f6fb;color:#45576e;font-size:11.5px;line-height:1.5}.criterion-example b{color:var(--blue);margin-right:5px}
.special-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.special-card{background:var(--paper);border:1px solid var(--line);border-radius:11px;padding:16px}.special-card h3{font-size:15px}.special-card p{font-size:12px;margin:6px 0;color:var(--body)}.special-card .criterion-title{margin-bottom:6px}.detail-note{margin:10px 0 0;color:var(--muted);font-size:12px}.config-details{margin-top:10px}.config-details summary{font-size:12px;color:var(--muted)}.config-details pre{max-height:180px}.outcome{border-radius:13px;background:#eaf0fa;padding:18px 20px;margin-top:12px;display:flex;align-items:center;justify-content:space-between;gap:12px}.outcome strong{display:block;font-size:20px;line-height:1.4;color:var(--ink)}.outcome p{font-size:12px;margin:4px 0 0;color:var(--body)}.outcome a{color:var(--blue);font-weight:700;white-space:nowrap}.kpi-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-top:12px}.kpi .kpi-label{display:block;color:var(--muted);font-size:12px}.kpi.is-fail b{color:var(--red)}.kpi b{font-variant-numeric:tabular-nums}.kpi .note{display:block;line-height:1.4}.section-anchor{scroll-margin-top:15px}.case>summary{list-style:none}.case>summary::-webkit-details-marker{display:none}.case>summary:before{content:'▸';display:inline-block;margin-right:8px;color:var(--blue)}.case[open]>summary:before{content:'▾'}a{color:var(--blue)}a:focus-visible,summary:focus-visible{outline:3px solid #7fa5ed;outline-offset:3px;border-radius:3px}
.result-breakdowns{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:12px;margin-top:20px}.result-breakdown{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:16px}.result-breakdown h3{font-size:16px;margin:0 0 10px}.result-breakdown table{font-variant-numeric:tabular-nums}.result-breakdown th:not(:first-child),.result-breakdown td:not(:first-child){text-align:right}.result-breakdown .group-row th{text-align:left;background:#f4f7fa;color:var(--ink);font-size:12px}.result-status{border-radius:8px;background:var(--amber-soft);color:var(--amber);padding:10px 12px;margin-top:12px;font-size:12px;font-weight:700}
.answer-view{background:#f7f9fc;border:1px solid var(--line);border-radius:6px;padding:4px 0;margin:6px 0;max-height:480px;overflow:auto}.answer-view .code-line.mark{background:#ffe3e7}.answer-view .ptr{max-width:240px}.answer-view .ptr a{color:var(--red);font-weight:700}
.judge{background:#f6f8fb;border:1px solid var(--line);border-radius:7px;padding:6px 10px;margin:8px 0}.judge p{margin:2px 0 0;color:var(--ink);font-weight:600}.judge p.note{color:var(--muted);font-weight:400}
.fail-no{display:inline-block;border-radius:9px;background:var(--red);color:#fff;font:700 11px var(--mono);padding:0 7px;margin-left:4px}
.jev-box{border:1px solid #dcd5ed;border-left:4px solid var(--violet);border-radius:8px;background:#fff;padding:8px 12px;margin-top:12px}.jev-box h3{color:var(--violet);font-size:14px}
.code-box{border:1px solid #d3deef;border-left:4px solid var(--blue);border-radius:8px;background:#fff;padding:8px 12px;margin-top:12px}.code-box h3{color:var(--blue);font-size:14px}.code-box h4{font-size:13px;margin:12px 0 4px}
.code-box table{margin-top:4px}.code-box td,.code-box th{border-bottom:1px solid #e6edf6}.box-count{font:700 12px var(--mono);color:var(--ink);margin-left:8px}
.sum-line{margin:14px 0 0;padding:7px 10px;border-radius:7px;background:#f4f7fa;font-size:12.5px;font-weight:700;color:var(--ink)}
.jev-box table{margin-top:4px}.jev-box td,.jev-box th{border-bottom:1px solid #eee9f7}.criterion-card.jev .code{color:var(--violet)}
.jev-chip{font:700 11px var(--mono);color:var(--violet);background:var(--violet-soft);border-radius:9px;padding:0 7px;margin-left:6px}
@media(max-width:900px){.result-breakdowns{grid-template-columns:1fr}}
@media(max-width:900px){.category-grid{grid-template-columns:1fr}.category-card>p{min-height:0}.criteria-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.kpi-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
.overall-card{display:flex;align-items:center;gap:26px;background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:20px 24px}
.overall-chart{position:relative;flex:none;width:150px;height:150px}.donut-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.donut-center b{font-size:30px;color:var(--ink);line-height:1.1}.donut-center span{font-size:11px;color:var(--muted)}
.overall-text strong{display:block;font-size:22px;line-height:1.4;color:var(--ink)}.legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin:10px 0 6px;font-size:13px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;vertical-align:-1px}.legend b{color:var(--ink);margin-left:2px}
.area-card{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:16px 20px;margin-top:12px}.area-card h3{font-size:16px;margin-bottom:8px}
.area-row{display:grid;grid-template-columns:78px minmax(120px,190px) minmax(0,1fr) 58px 130px;gap:12px;align-items:center;padding:7px 0;border-bottom:1px solid var(--line);font-size:13px}
.area-tag{display:inline-block;font-size:11px;font-weight:700;border-radius:9px;padding:1px 8px;text-align:center;white-space:nowrap}.area-tag.code{background:var(--blue-soft);color:var(--blue)}.area-tag.sem{background:var(--violet-soft);color:var(--violet)}
.area-name{font-weight:700;color:var(--ink)}.bar{height:12px;background:#edf1f5;border-radius:6px;overflow:hidden}.bar-fill{display:block;height:100%;border-radius:6px}.bar-fill.code{background:var(--blue)}.bar-fill.sem{background:var(--violet)}
.area-rate{font-weight:800;color:var(--ink);text-align:right;font-variant-numeric:tabular-nums}.area-rate.is-fail{color:var(--red)}.area-count{font-size:12px;color:var(--muted)}
.nowrap{white-space:nowrap}.crit-cell{min-width:170px}.reason-cell{min-width:220px}.where-cell{min-width:260px}
.crit-id{display:inline-block;font:700 11px var(--mono);border-radius:5px;padding:1px 6px}.crit-id.code{background:var(--blue-soft);color:var(--blue)}.crit-id.sem{background:var(--violet-soft);color:var(--violet)}
.where-label{font-size:11.5px;font-weight:700;color:var(--muted);margin-bottom:3px}.where-lines{border:1px solid var(--line);border-radius:6px;background:#f7f9fc;font:12px/1.65 var(--mono);padding:3px 0}
.wl{display:grid;grid-template-columns:30px minmax(0,1fr);gap:8px;padding:0 8px}.wl .ln{color:#8291a3;text-align:right}.wl .lt{white-space:pre-wrap;overflow-wrap:anywhere}.wl.mark{background:#fff0f2}
@media(max-width:700px){.overall-card{display:block}.overall-chart{margin:0 auto 12px}.area-row{grid-template-columns:70px minmax(0,1fr) 56px;gap:6px 10px}
.area-row .area-rate{grid-column:3;grid-row:1}.area-row .bar,.area-row .area-count{grid-column:1/-1}}
@media(max-width:600px){main{padding:18px 12px}.report-head{display:block}.head-badge{display:inline-block;margin-top:12px}.section-head{display:block}.section-head p{margin-top:3px}.criteria-grid,.special-grid{grid-template-columns:1fr}.kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.outcome{display:block}.outcome a{display:inline-block;margin-top:8px}.vs{grid-template-columns:1fr}table{min-width:560px}.answer-view .ptr{max-width:110px}}
"""


def _e(value: Any) -> str:
    if value is None:
        return "—"
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return html.escape(value, quote=True)


def _verdict_label(v: Any, status: str) -> str:
    if v:
        return str(v)
    if status in (Status.OUT_OF_SCOPE, Status.APPLICABILITY_EXCEPTION):
        return "해당 없음"
    if status == Status.DUPLICATE:
        return "중복 제외"
    return "판정 미완료"


def _verdict(v: Any, status: str) -> str:
    return f'<span class="v {v or "NONE"}">{_e(_verdict_label(v, status))}</span>'


def _rate(r: Any) -> str:
    return "—" if r is None else f"{r * 100:.1f}%"


def _case_keys(cases: List[Dict[str, Any]]) -> List[str]:
    """Keep existing unique anchors; give duplicate display IDs collision-free internal keys."""
    counts = Counter(c["case_id"] for c in cases)
    reserved = set(counts)
    keys = []
    for i, case in enumerate(cases):
        key = case["case_id"]
        if counts[key] > 1:
            key = f"entry-{i + 1}"
            while key in reserved:
                key += "-"
        reserved.add(key)
        keys.append(key)
    return keys


def _turn_index(cases: List[Dict[str, Any]], turn: Dict[str, Any], conversation_id: str) -> int | None:
    index = turn.get("case_index")
    if isinstance(index, int) and 0 <= index < len(cases) and cases[index]["case_id"] == turn["case_id"]:
        return index
    # Old saved runs have no internal index. Never guess between ambiguous duplicates.
    matches = [i for i, c in enumerate(cases) if c["case_id"] == turn["case_id"]
               and c.get("conversation") == {"id": conversation_id, "turn": turn["turn"]}]
    return matches[0] if len(matches) == 1 else None


def _case_link(label: Any, keys: List[str], index: int | None) -> str:
    return (f'<a href="#case-{_e(keys[index])}">{_e(label)}</a>'
            if index is not None and 0 <= index < len(keys) else _e(label))


def _loc_html(l: Dict[str, Any]) -> str:
    label = _e(l.get("label", "")) + (f' <span class="chip">{_e(l["tag"])}</span>' if l.get("tag") else "")
    if l["kind"] == "snippet" and l.get("segments"):
        body = "".join(f"<mark>{_e(sg['text'])}</mark>" if sg["mark"] else _e(sg["text"]) for sg in l["segments"])
        return f'<div class="loc"><div class="loc-label">{label}</div><div class="loc-line">{body}</div></div>'
    if l["kind"] == "snippet":
        return f'<div class="loc"><div class="loc-label">{label}</div><div class="loc-line">{_e(l["before"])}<mark>{_e(l["hit"])}</mark>{_e(l["after"])}</div></div>'
    if l["kind"] == "code":
        lines = "".join(f'<div class="code-line{" mark" if ln["mark"] else ""}"><span class="ln">{ln["n"]}</span>'
                        f'<span class="lt">{_e(ln["text"]) or "&nbsp;"}</span>'
                        f'{"<span class=ptr>◀ " + _e(l.get("pointer", "")) + "</span>" if ln["mark"] and l.get("pointer") else ""}</div>'
                        for ln in l["lines"])
        return f'<div class="loc"><div class="loc-label">{label}</div><div class="code-view">{lines}</div></div>'
    if l["kind"] == "list":
        items = "".join(f'<li{" class=mark" if i in l["marks"] else ""}>{_e(t)}</li>' for i, t in enumerate(l["items"]))
        more = f'<p class="note">외 {l["more"]}개</p>' if l.get("more") else ""
        return f'<div class="loc"><div class="loc-label">{label}</div><ol class="loc-list">{items}</ol>{more}</div>'
    return ""


Marks = Dict[int, List[Tuple[int, str, List[str]]]]


def _fail_marks(text: str, fails: List[Dict[str, Any]], target: str) -> Marks:
    """FAIL 번호별로 표시할 줄과 그 줄 안의 강조 글자. 설명의 위치를 쓰고, 없으면 근거 범위의 줄을 쓴다."""
    marks: Marks = {}
    for no, r in enumerate(fails, 1):
        found: Dict[int, List[str]] = {}
        for l in r["explain"]["locations"]:
            if l["kind"] == "code" and target == "answer":
                for ln in l["lines"]:
                    if ln["mark"] and ln.get("answer_line"):
                        found.setdefault(ln["answer_line"], [])
            elif l["kind"] == "snippet" and l.get("target", "answer") == target and l.get("line"):
                hits = [sg["text"] for sg in l.get("segments") or [] if sg["mark"]] or [l.get("hit") or ""]
                found.setdefault(l["line"], []).extend(h for h in hits if h)
        if not found:
            for e in r.get("evidence", []):
                if e["target"] == target and e.get("start") is not None:
                    first = text.count("\n", 0, e["start"]) + 1
                    last = text.count("\n", 0, max(e["start"], (e.get("end") or e["start"] + 1) - 1)) + 1
                    for n in range(first, last + 1):
                        found.setdefault(n, [])
        for n, hits in found.items():
            marks.setdefault(n, []).append((no, r["title"], hits))
    return marks


def _numbered(text: str, marks: Marks, case_id: str, anchored: Set[int]) -> str:
    """줄 번호를 붙인 원문. FAIL 줄은 강조하고 해당 FAIL 설명으로 가는 포인터를 단다."""
    rows = []
    for n, line in enumerate(text.split("\n"), 1):
        here = marks.get(n, [])
        spans = sorted({(i, i + len(h)) for _, _, hs in here for h in hs if (i := line.find(h)) >= 0})
        parts, pos = [], 0
        for s, e in spans:
            if s >= pos:
                parts += [_e(line[pos:s]), f"<mark>{_e(line[s:e])}</mark>"]
                pos = e
        parts.append(_e(line[pos:]))
        anchors = "".join(f'<span id="at-{_e(case_id)}-{no}"></span>' for no, _, _ in here if no not in anchored)
        anchored.update(no for no, _, _ in here)
        ptr = " · ".join(f'<a href="#fail-{_e(case_id)}-{no}">FAIL {no}</a> {_e(title)}' for no, title, _ in here)
        rows.append(f'<div class="code-line{" mark" if here else ""}"><span class="ln">{anchors}{n}</span>'
                    f'<span class="lt">{"".join(parts) or "&nbsp;"}</span>'
                    f'{"<span class=ptr>◀ " + ptr + "</span>" if ptr else ""}</div>')
    return f'<div class="answer-view">{"".join(rows)}</div>'


def _judge_html(r: Dict[str, Any]) -> str:
    if r.get("evaluation_method") == "LLM_COMPLETION":
        original = r.get("code_original") or {}
        return (f'<div class="judge"><b>판정한 곳</b><p>LLM 공통 조건 보완 · {_e(r["check_id"])}</p>'
                f'<p class="note">코드 원래 상태: {_e(original.get("status"))} · {_e(original.get("reason"))}</p></div>')
    sub = r["subcategory"]
    trace = _SUBS.get(sub, {}).get("trace") or ("code_eval/checks_aiona.py" if sub.startswith("AIONA") else "")
    return (f'<div class="judge"><b>판정한 곳</b><p>{_e(sub)} {_e(r.get("subcategory_name") or "")} › {_e(r["title"])} '
            f'<span class="st">{_e(r["check_id"])}</span></p><p class="note">판정식 <code>{_e(r["comparison"])}</code>'
            + (f' · 구현 {_e(trace)}' if trace else "") + '</p></div>')


def _loc_labels(r: Dict[str, Any]) -> str:
    labels = [("표시본 " if l.get("target") == "displayed_text" else "") + l["label"]
              for l in r["explain"]["locations"] if l.get("label")]
    return " · ".join(labels) or "위치 정보 없음"


def _result_html(r: Dict[str, Any], no: int = 0, case_id: str = "") -> str:
    x = r["explain"]
    why = ("왜 FAIL 인가요?" if r["verdict"] == "FAIL" else "결과" if r["verdict"] == "PASS"
           else "왜 적용하지 않았나요?" if r["status"] in (Status.OUT_OF_SCOPE, Status.APPLICABILITY_EXCEPTION)
           else "왜 중복 제외했나요?" if r["status"] == Status.DUPLICATE else "왜 판정하지 않았나요?")
    vs = (f'<div class="vs"><div><b>기대한 것</b><p>{_e(x["expected"] or "—")}</p></div>'
          f'<div class="{r["verdict"] or "NONE"}-box"><b>실제 답변</b><p>{_e(x["actual"] or "—")}</p></div></div>') if (x["expected"] or x["actual"]) else ""
    locs = "".join(_loc_html(l) for l in x["locations"])
    tech = (f'<details class="tech"><summary>기술 정보 (개발자용)</summary><p class="st">{_e(r["check_id"])} · {_e(r["status"])} · '
            f'{_e(r["source"])} · {_e(r["check_version"])}</p><p class="st">요구값 {_e(r["required"])} · 측정값 {_e(r["measured"])} · '
            f'비교식 {_e(r["comparison"])}</p></details>')
    anchor = f' id="fail-{_e(case_id)}-{no}"' if no else ""
    return (f'<details class="res {r["verdict"] or "NONE"}"{anchor}{" open" if r["verdict"] == "FAIL" else ""}><summary>{_verdict(r["verdict"], r["status"])}'
            + (f'<span class="fail-no">{no}</span>' if no else "")
            + f' <b>{_e(x["headline"])}</b> <span class="st">· {_e(r["subcategory"])} {_e(r.get("subcategory_name") or "")}</span></summary>'
            f'<p class="note">무엇을 검사했나: {_e(x["checked"])}</p>'
            + (f'<p class="note">질문에서 읽은 요구: “{_e(x["requirement"])}”</p>' if x.get("requirement") else "")
            + f'<div class="why {r["verdict"] or "NONE"}"><b>{why}</b><p>{_e(x["summary"])}</p></div>{vs}'
            + (f'<b class="sec">문제 위치</b>{locs}' if locs else "")
            + (_judge_html(r) if r["verdict"] == "FAIL" else "")
            + (f'<div class="fix"><b>고치려면</b><p>{_e(x["fix"])}</p></div>' if x.get("fix") else "")
            + (f'<p class="note">참고: {_e(x["note"])}</p>' if x.get("note") else "") + tech + '</details>')


def _jev_fails(record: Any) -> List[str]:
    return [cid for cid, d in ((record or {}).get("decisions") or {}).items() if d["decision"] == "FAIL"]


#: 적용 근거를 찾은 곳 — 질문(J1~J4) · 답변(J5) · 참조 데이터(J5) · 대화(J6)
_SOURCE_LABEL = {"question": "질문 근거", "answer": "답변 근거", "reference": "참조 데이터", "conversation": "대화"}
_J5_BASIS_KO = {"reference": "판정 기준: 참조 데이터", "web": "판정 기준: 웹 출처",
                "input": "판정 기준: 제공 자료 일치 (자료 자체의 사실성 검증 아님)",
                "mixed": "판정 기준: 제공 자료 일치와 외부 사실 확인 (주장별 구분)",
                "unverified": "판정 기준: 확인 필요", "engine": "판정 기준: 엔진 지식 (참조 데이터 없음)"}


def _safe_http_url(value: Any) -> str | None:
    if not isinstance(value, str) or not re.match(r"^https?://", value, re.I) or re.search(r"[\x00-\x20\x7f\\]", value):
        return None
    try:
        parts = urlsplit(value)
        return value if parts.scheme.lower() in ("http", "https") and parts.hostname and not parts.username and not parts.password else None
    except ValueError:
        return None


def _semantic_evidence(record: Dict[str, Any]) -> str:
    """Render only stored verification evidence; never infer sources or verdicts."""
    decisions = record.get("decisions") or {}
    j5, j6 = decisions.get("J5") or {}, decisions.get("J6") or {}
    verification = record.get("verification")
    sections = []

    def note(value):
        return f'<p class="note">{_e(value)}</p>'

    def verdict(value):
        return value if value in ("PASS", "FAIL") else "확인 필요"

    for cid, decision in decisions.items():
        prior = decision.get('policy_original_assessment')
        outcome = decision.get('decision')
        if not isinstance(prior, dict) or outcome not in ('PASS', 'FAIL'):
            continue
        source_failure = decision.get('reason_code') == 'SOURCE_ORIGINAL_UNAVAILABLE'
        marker = 'data-source-policy-detail' if source_failure else 'data-evidence-policy-detail'
        label = '출처 원문 확인 실패' if source_failure else '검증 기준 미충족' if outcome == 'FAIL' else '제공 자료 대조 완료'
        sections.append(f'<div class="why {_e(outcome)}" {marker}><strong>{_e(cid)} {_e(outcome)} · {label}</strong>'
                        + note(decision.get('reason') or '최종 검증 기준에 따라 판정했습니다.')
                        + (note('근거 검증 기준 미충족에 따른 FAIL입니다. 주장 자체의 오류를 확정한 것은 아닙니다.') if outcome == 'FAIL' else '') + '</div>')
        sections.append('<details class="semantic-policy-original"><summary>정책 적용 전 판정·사유</summary>'
                        + note('이전 판정: ' + verdict(prior.get('decision')))
                        + note(prior.get('reason') or '별도 사유 없음') + '</details>')

    def sources(items, show_check=False, basis=None):
        rows = []
        for item in items:
            source = {"url": item} if isinstance(item, str) else item if isinstance(item, dict) else {}
            input_source = source.get("origin") in ("question", "reference", "history_user")
            input_label = {"question": "질문 원문", "reference": "참조 데이터", "history_user": f'이전 사용자 {source.get("turn") if source.get("turn") is not None else "미기록"}턴'}.get(source.get("origin"))
            url = None if input_source else _safe_http_url(source.get("url"))
            title = _e("제공 자료 대조 · " + input_label if input_source else source.get("title") or source.get("url") or "출처 정보 없음")
            link = f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{title}</a>' if url else title
            quote = f'<p><q>{_e(source["quote"])}</q></p>' if source.get("quote") else ""
            details = note(source["title"]) if input_source and source.get("title") else ""
            if show_check:
                checked = source.get("quote_verified")
                if basis == "reference" and source.get("quote") and checked is not True and checked is not False:
                    details += note("제공된 참조 자료 인용 · 외부 출처 검증과 구분")
                else:
                    details += note(("제공 자료 인용: " if input_source else "원문 인용: ") + ("확인됨" if checked is True else "확인하지 못함" if checked is False else "대조 기록 없음"))
            for field, label in (("verification_reason", "원문 대조 사유"), ("verified_at", "원문 확인 시각")):
                if source.get(field):
                    details += note(label + ": " + str(source[field]))
            if source.get("authority"):
                authority = str(source["authority"])
                details += note("출처 분류: " + {"primary": "공식·일차 자료", "reliable_secondary": "신뢰 근거가 있는 이차 자료", "unestablished": "신뢰성 확인 불가"}.get(authority, authority))
            for field, label in (("authority_reason", "출처 신뢰성 근거"), ("temporal_relevance", "시점 적합성"), ("assessment_warning", "출처 확인 필요")):
                if source.get(field):
                    details += note(label + ": " + str(source[field]))
            if not input_source and source.get("resolved_url") and source["resolved_url"] != source.get("url"):
                resolved = _safe_http_url(source["resolved_url"])
                destination = _e(source["resolved_url"])
                final_link = f'<a href="{_e(resolved)}" target="_blank" rel="noopener noreferrer">{destination}</a>' if resolved else destination
                details += '<p>대조한 원문: ' + final_link + '</p>'
            rows.append(f'<li>{link}{quote}{details}</li>')
        return '<ul>' + ''.join(rows) + '</ul>'

    outcome = j5.get("j5_outcome") if isinstance(j5.get("j5_outcome"), dict) else None
    if outcome and outcome.get("code") not in (None, "not_applicable"):
        meaning = ("신뢰할 수 있는 근거와 충돌하는 사실 오류입니다." if outcome.get("factual_error_confirmed") else
                   "판정 미완료 — 사실 여부를 확인하지 못한 평가 상태이며 FAIL이 아닙니다." if j5.get("status") == "INCOMPLETE" else
                   "사실 오류를 확정한 FAIL이 아닙니다." if j5.get("decision") == "FAIL" else "근거로 확인된 판정입니다.")
        sections.append(f'<div class="semantic-j5-outcome"><strong>J5 판정 사유 · {_e(outcome.get("label") or outcome.get("code"))}</strong>'
                        f'{note(meaning)}</div>')
    for cid, decision in sorted((record.get("decisions") or {}).items()):
        again = decision.get("reevaluation") if isinstance(decision, dict) else None
        if not isinstance(decision, dict) or not (decision.get("status") == "INCOMPLETE" or again):
            continue
        title = (f"{cid} 판정 미완료" if decision.get("status") == "INCOMPLETE" else f"{cid} 재평가 후 {decision.get('decision')}")
        body = note(decision.get("reason") or "") if decision.get("status") == "INCOMPLETE" else ""
        if isinstance(again, dict):
            body += note("첫 판정: FAIL · " + str((again.get("first") or {}).get("reason") or "이유 없음"))
            retry = again.get("retry") or {}
            body += note("재평가 1회: " + str(retry.get("decision") or retry.get("status") or "응답 없음") + " → 최종 " + str(again.get("final")))
            body += note("근거 확인은 이유와 원문 인용의 형식 검증이며, 이유의 의미적 타당성까지 검증한 것은 아닙니다.")
        sections.append(f'<div class="semantic-reevaluation"><strong>{_e(title)}</strong>{body}</div>')
    citation_checks = j5.get("citation_checks") if isinstance(j5.get("citation_checks"), list) else []
    if citation_checks:
        labels = {"supported": "출처가 주장을 뒷받침", "contradicted": "출처가 주장과 모순", "unrelated": "출처가 주장과 무관",
                  "not_found": "출처에 해당 내용 없음", "inaccessible": "출처 원문 접근 불가", "unverified": "판정 확정 안 함",
                  "unchecked": "확인하지 않음"}
        body = note("답변이 직접 든 출처를 서버가 다시 받아 대조했습니다. 원문을 받지 못하거나 인용이 맞지 않으면 판정에 쓰지 않습니다.")
        for check in citation_checks:
            if not isinstance(check, dict):
                continue
            body += f'<p><strong>{_e(labels.get(check.get("verdict"), check.get("verdict")))} · </strong>{_e(check.get("url") or "")}</p>'
            for key, label in (("claim", "답변 구절: "), ("quote", "출처 원문: "), ("reason", "평가자 이유: "), ("note", "")):
                if check.get(key):
                    body += note(label + str(check[key]))
        sections.append(f'<details class="semantic-citation-evidence"><summary>J5 답변 출처 확인</summary>{body}</details>')
    if j5.get("status") != "NOT_APPLICABLE" and (j5.get("fact_check") or j5.get("status") in ("UNVERIFIED", "INCOMPLETE")):
        fact = j5.get("fact_check") or {}
        claims = fact.get("claims") if isinstance(fact.get("claims"), list) else []
        body = note({"web": "검증 기준: 웹 출처", "reference": "검증 기준: 참조 데이터",
                     "input": "검증 기준: 제공 자료 일치 · 자료 자체의 사실성은 검증하지 않음",
                     "mixed": "검증 기준: 제공 자료 일치와 외부 사실 확인 · 주장별 구분",
                     "none": "검증 기준: 확인 필요"}.get(fact.get("basis"), "검증 기준: 확인 필요"))
        if j5.get("status") == "UNVERIFIED":
            body += note("확인 필요 · " + (j5.get("reason") or "검증 근거가 충분하지 않습니다."))
        if not claims:
            body += note("확인 필요 · 저장된 검증 주장과 출처가 없습니다.")
        for claim in claims:
            body += f'<p><strong>{_e(verdict(claim.get("verdict")))} · </strong>{_e(claim.get("claim") or "검증 주장 미기록")}</p>'
            if claim.get("reason"):
                body += note(claim["reason"])
            if claim.get("scope") == "input_consistency":
                body += note("판정 범위: 제공 자료 일치 · 자료 자체의 사실성은 검증하지 않음")
            elif claim.get("scope") == "external_fact":
                body += note("판정 범위: 외부 사실 확인")
            if claim.get("scope_quote"):
                body += note("자료 대조 요청: " + str(claim["scope_quote"]))
            body += sources(claim["sources"], show_check=True, basis=fact.get("basis")) if isinstance(claim.get("sources"), list) and claim["sources"] else note(
                "참조 데이터에 근거한 검증입니다." if fact.get("basis") == "reference" else "확인 필요 · 저장된 검증 출처가 없습니다.")
        sections.append(f'<details class="semantic-fact-evidence"><summary>J5 사실 검증 근거</summary>{body}</details>')
    if j6.get("status") != "NOT_APPLICABLE" and (isinstance(j6.get("context_checks"), list) or j6.get("status") == "UNVERIFIED"):
        checks = j6.get("context_checks") if isinstance(j6.get("context_checks"), list) else []
        body = note("확인 필요 · " + (j6.get("reason") or "대화 맥락을 확정할 근거가 충분하지 않습니다.")) if j6.get("status") == "UNVERIFIED" else ""
        if not checks:
            body += note("확인 필요 · 저장된 이전 턴 인용 근거가 없습니다.")
        for check in checks:
            body += f'<p><strong>이전 {_e(check.get("turn", "미기록"))}턴 · {_e(verdict(check.get("decision")))}</strong></p>'
            body += note("이전 지시: " + (check.get("instruction_quote") or "인용 없음"))
            body += note("현재 답변: " + (check.get("answer_quote") or "인용 없음"))
            if check.get("reason"):
                body += note(check["reason"])
        sections.append(f'<details class="semantic-context-evidence"><summary>J6 이전 대화·현재 답변 근거</summary>{body}</details>')
    originals = [(cid, decisions[cid]) for cid in ("J5", "J6") if isinstance((decisions.get(cid) or {}).get("model_original_assessment"), dict)]
    if originals:
        body = note("현재 결과와 점수에는 서버 근거 검증 후 최종 판정만 사용합니다.")
        for cid, current in originals:
            original = current["model_original_assessment"]
            body += f'<p><strong>{_e(cid)}</strong></p>' + note("모델 원판정: " + verdict(original.get("choice")))
            if original.get("reason"):
                body += note("모델 원래 설명: " + str(original["reason"]))
            body += note("서버 근거 검증 후 최종 판정: " + ("해당 없음" if current.get("status") == "NOT_APPLICABLE" else verdict(current.get("decision"))))
            if current.get("reason"):
                body += note("최종 판정 사유: " + str(current["reason"]))
        sections.append(f'<details class="semantic-original-assessment"><summary>서버 근거 검증 전 모델 판정</summary>{body}</details>')
    if isinstance(verification, dict):
        body = ""
        if isinstance(verification.get("search_performed"), bool):
            body += note("웹 검색 수행: " + ("예" if verification["search_performed"] else "검색하지 않음"))
        if verification.get("checked_at"):
            body += note("검증 시각: " + str(verification["checked_at"]))
        if "source_validation" in verification:
            value = verification["source_validation"]
            label = (json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
            if value == "provider":
                label = "웹 검색을 수행하지 않음 · 출처 대조 결과는 주장별 근거 참조" if verification.get("search_performed") is False else "출처 URL을 도구 기록과 대조"
            elif value == "provider_and_content":
                label = "출처 URL을 도구 기록과 대조하고 인용문 원문 대조를 시도함"
            body += note("출처 검증: " + label)
        if isinstance(verification.get("source_urls"), list) and verification["source_urls"]:
            body += sources(verification["source_urls"])
        sections.append(f'<details class="semantic-verification"><summary>검색·검증 기록</summary>{body}</details>')
    return '<div class="semantic-evidence">' + ''.join(sections) + '</div>' if sections else ""


def _jev_case(record: Any, label: str, criteria: List[Dict[str, Any]]) -> str:
    """질문 하나의 의미 평가 상자. 코드 규칙으로 정한 적용 기준과 근거(질문·답변·참조 데이터·대화)·볼 것, 결과(또는 미실행), 해당 없음 기준."""
    if not record:
        return ""
    status = record.get("status")
    applicability = record.get("applicability") or {}
    evidence = applicability.get("evidence") or {}
    decisions = record.get("decisions") or {}
    asked = record.get("criteria") or [c["id"] for c in criteria]
    rows = []
    for c in (c for c in criteria if c["id"] in asked):
        d, ev = decisions.get(c["id"]), evidence.get(c["id"])
        if ev:
            source = _SOURCE_LABEL.get(ev.get("source") or "question", "근거")
            what = f'{_e(ev["look_for"])}<div class="note">{source}: ‘{_e(ev["excerpt"])}’ · {_e(" · ".join(ev["rules"]))}</div>'
        else:
            what = _e(c.get("question") or "평가 당시 기준 설명이 저장되지 않았습니다.")
        if c["id"] == "J5" and record.get("j5_basis"):
            what += f'<div class="note">{_e(_J5_BASIS_KO.get(record["j5_basis"], record["j5_basis"]))}</div>'
        if d:
            result = ('<span class="v NONE">확인 필요</span>' if d.get("status") == "UNVERIFIED" and d.get("decision") not in ("PASS", "FAIL") else
                      '<span class="v NONE">판정 미완료</span>' if d.get("status") == "INCOMPLETE" else _verdict(d.get("decision"), Status.EXECUTED))
            confidence = d.get("confidence")
            if d.get("decision") in ("PASS", "FAIL") and isinstance(confidence, (int, float)) and math.isfinite(confidence):
                result += f' <span class="st">판단 확신도 {confidence * 100:.0f}%</span>'
            if d.get("reason"):
                result += f'<div class="note">{_e(d["reason"])}</div>'
            elif d.get("decision") == "FAIL":
                result += '<div class="note">평가자가 FAIL 근거를 남기지 않았습니다.</div>'
        else:
            result = '<span class="st">미실행</span>'
        rows.append(f'<tr><th scope="row">{_e(c["id"])} {_e(c.get("name") or c["id"])}</th><td>{what}</td><td>{result}</td></tr>')
    if status == "SUCCESS":
        meta = " · ".join(x for x in (record.get("model"), f'{record["latency_ms"]} ms' if record.get("latency_ms") is not None else "") if x)
    else:
        meta = f'{label} 상태: {_JEV_STATUS_KO.get(status, status)} ({status})' + (
            f' · {record.get("error")}' if status != "NOT_CONFIGURED" and record.get("error") else "")
    names = {c["id"]: c.get("name") or c["id"] for c in criteria}
    skipped = "".join(f'<p class="note">해당 없음 · {_e(cid)} {_e(names.get(cid, cid))}: {_e(reason)}</p>'
                      for cid, reason in (applicability.get("not_applicable") or {}).items())
    context = record.get("context") or {}
    if context.get("history_turns"):
        cut = " · 긴 답변 일부 생략" if context.get("history_truncated") else ""
        skipped += f'<p class="note">대화 맥락: 앞 턴 {context["history_turns"]}개 함께 보냄{cut}</p>'
    if context.get("issues"):
        skipped += f'<p class="note">대화 이력 문제: {_e(", ".join(context["issues"]))}</p>'
    decided = [decisions[c["id"]]["decision"] for c in criteria if c["id"] in asked and c["id"] in decisions]
    missing = len(rows) - len(decided)
    unverified = sum(value not in ("PASS", "FAIL") for value in decided)
    counts = _counts(decided.count("PASS"), decided.count("FAIL"), (f" · 확인 필요 {unverified}" if unverified else "") + (f" · 미실행 {missing}" if missing else ""))
    return (f'<div class="jev-box"><h3>Semantic Evaluation — {_e(label)} {counts}</h3><div class="wrap"><table><thead><tr><th>평가할 기준</th>'
            f'<th>이 질문에서 볼 것</th><th>결과</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            f'{_semantic_evidence(record)}{skipped}<p class="st">{_e(meta)}</p></div>')


def _counts(pass_: int, fail: int, extra: str = "") -> str:
    return f'<span class="box-count">PASS {pass_} · FAIL {fail}{extra}</span>'


def _code_case(case: Dict[str, Any], fails: List[Dict[str, Any]], others: List[Dict[str, Any]], obs_html: str,
               anchor_key: str) -> str:
    """질문 하나의 CODE 평가 상자. PASS 를 포함한 모든 검사를 기준·검사·결과 표로 보여 주고, FAIL 상세와 검사별 설명을 잇는다."""
    cid = anchor_key
    fail_no = {id(r): no for no, r in enumerate(fails, 1)}
    rows, unjudged = [], {}
    for r in case["results"]:
        verdict = r["verdict"] if r["counted"] else None  # 중복 등 집계 밖 결과는 머리줄 수에 넣지 않는다
        if verdict is None:
            label = _verdict_label(None, r["status"])
            unjudged[label] = unjudged.get(label, 0) + 1
        crit = "AIONA 특화" if r["area"] == "aiona_specialized" else f'{r["subcategory"]} {r.get("subcategory_name") or ""}'
        no = fail_no.get(id(r))
        link = f' <a class="note" href="#fail-{_e(cid)}-{no}">아래 상세 ↓</a>' if no else ""
        rows.append(f'<tr><th scope="row">{_e(crit)}</th><td>{_e(r["title"])}<div class="note">{_e(r["explain"]["checked"])}</div></td>'
                    f'<td>{_verdict(verdict, r["status"])}{link}<div class="note">{_e(r["explain"]["summary"])}</div></td></tr>')
    n_pass = sum(1 for r in case["results"] if r["counted"] and r["verdict"] == "PASS")
    table = (f'<div class="wrap"><table><thead><tr><th>기준</th><th>검사</th><th>결과</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>' if rows else '<p class="note">이 질문에 적용된 CODE 검사가 없습니다.</p>')
    detail = ('<h4>CODE에서 발견된 문제</h4>' + "".join(_result_html(r, no, cid) for no, r in enumerate(fails, 1))) if fails else ""
    more = (f'<details><summary>나머지 검사 상세 설명 {len(others)}건</summary>{"".join(_result_html(r) for r in others)}</details>'
            if others else "")
    extra = "".join(f" · {label} {n}" for label, n in unjudged.items())
    return (f'<div class="code-box"><h3>CODE Evaluation {_counts(n_pass, len(fails), extra)}</h3>'
            f'{table}{detail}{more}{obs_html}</div>')


def _case(case: Dict[str, Any], jev_record: Any = None, jev_label: str = "Jev", anchor_key: str | None = None,
          criteria: List[Dict[str, Any]] | None = None) -> str:
    key = case["case_id"] if anchor_key is None else anchor_key
    if case.get("case_status") != "EVALUATED":
        return (f'<div class="card" id="case-{_e(key)}"><h3>{_e(case["case_id"])}</h3><p class="st">{_e(case.get("case_status"))} · '
                f'{_e(case.get("input_error"))}</p></div>')
    s = case["summary"]
    fails = [r for r in case["results"] if r["counted"] and r["verdict"] == "FAIL"]
    others = [r for r in case["results"] if not (r["counted"] and r["verdict"] == "FAIL")]
    obs = next((o for o in case["observations"] if o.get("observation_id") == "foreign_script_spans"), None)
    obs_html = ""
    if obs and obs.get("span_count"):
        spans = "".join(f'<li><code>{_e(x["span_text"])}</code> — 주변: {_e(x["surrounding_text"])}</li>'
                        for x in obs["script_spans"][:20])
        obs_html = (f'<p class="note">참고: 외국 문자 위치 {obs["span_count"]}곳 — 판정은 1-1 ‘외국 문자’ 결과를 따릅니다.</p>'
                    f'<ul class="note">{spans}</ul>')
    code_pass = s["common"]["pass"] + s["specialized"]["pass"]
    code_fail = s["common"]["fail"] + s["specialized"]["fail"]
    jev_decisions = [d["decision"] for d in ((jev_record or {}).get("decisions") or {}).values()]
    jev_pass, jev_fail = jev_decisions.count("PASS"), jev_decisions.count("FAIL")
    n_pass, n_fail = code_pass + jev_pass, code_fail + jev_fail
    semantic = ("" if not jev_record else f' + 의미 평가({_e(jev_label)}) '
                + (f'PASS {jev_pass} · FAIL {jev_fail}' if jev_decisions else "미실행"))
    sum_line = f'<p class="sum-line">합계 PASS {n_pass} · FAIL {n_fail} = CODE 평가 PASS {code_pass} · FAIL {code_fail}{semantic}</p>'
    errors = case.get("errors") or []
    if errors:
        listed = " / ".join(f'{_e(e.get("check_id") or "")} · {_e(e.get("kind") or "")} · {_e(e.get("message") or "")}' for e in errors)
        sum_line += f'<p class="detail-note"><strong>검사 실행 오류 {len(errors)}건:</strong> {listed} — 품질 FAIL 이 아니며 이 질문은 PASS 로 확정하지 않습니다.</p>'
    cid, anchored = case["case_id"], set()
    answer = _numbered(case["answer"], _fail_marks(case["answer"], fails, "answer"), key, anchored)
    shown = case.get("displayed_text")
    shown_html = ("<h3>표시본</h3>" + _numbered(shown, _fail_marks(shown, fails, "displayed_text"), key, anchored)
                  if shown is not None else "")
    jev_fails = _jev_fails(jev_record)
    jev_chip = f'<span class="jev-chip">{_e(jev_label)} FAIL {_e(" ".join(jev_fails))}</span>' if jev_fails else ""
    unverified = sum(d.get("status") == "UNVERIFIED" and d.get("decision") not in ("PASS", "FAIL") for d in ((jev_record or {}).get("decisions") or {}).values())
    if unverified:
        jev_chip += f'<span class="jev-chip">확인 필요 {unverified}건</span>'
    incomplete = sum(d.get("status") == "INCOMPLETE" for d in ((jev_record or {}).get("decisions") or {}).values())
    if incomplete:
        jev_chip += f'<span class="jev-chip">판정 미완료 {incomplete}건</span>'
    conv = case.get("conversation")
    conv_chip = f'<span class="chip">대화 {_e(conv["id"])} · {conv["turn"]}턴</span>' if conv else ""
    reference = case.get("reference")
    ref_html = (f'<h3>참조 데이터 (reference)</h3><pre>{_e(_reference_text(reference))}</pre>' if reference is not None else "")
    return f"""<details class="card case" id="case-{_e(key)}"{" open" if n_fail or jev_fails or unverified else ""}><summary><b>{_e(cid)}</b>{conv_chip} — PASS {n_pass} · FAIL {n_fail}{jev_chip}<div class="q-full">{_e(case["question"])}</div></summary>
<h3 style="margin-top:12px">질문</h3><pre>{_e(case["question"])}</pre>{ref_html}<h3>답변</h3>{answer}
{shown_html}
{sum_line}{_code_case(case, fails, others, obs_html, key)}{_jev_case(jev_record, jev_label, criteria or [])}</details>"""


def _category_overview() -> str:
    cards = []
    for category in CATEGORIES:
        rows = []
        for sub in category["subs"]:
            rows.append(f'<li><div class="criterion-title"><strong>{_e(sub["id"])} {_e(sub["name"])}</strong></div>'
                        f'<p>{_e(sub["target"])}</p>'
                        f'<div class="criterion-example"><b>예시</b>{_e(_COMMON_EXAMPLES[sub["id"]])}</div></li>')
        cards.append(f'<article class="category-card"><div class="category-top"><span class="category-number">{_e(category["id"])}</span></div>'
                     f'<h3>{_e(category["name"])}</h3><p>{_e(category["question"])}</p>'
                     f'<ul class="criterion-list">{"".join(rows)}</ul></article>')
    return '<div class="category-grid">' + ''.join(cards) + '</div>'


def _reference_text(reference: Any) -> str:
    return reference if isinstance(reference, str) else json.dumps(reference, ensure_ascii=False, indent=1)


def _report_criteria(batch: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """Historical judgments retain their recorded definitions; missing definitions stay unknown."""
    jev = batch.get("jev_eval")
    if not jev:
        criteria = [{**c, "applies_when": APPLIES_WHEN[c["id"]]} for c in describe_jev()["criteria"]]
        return criteria, "의미 평가 결과가 없어 현재 기준을 참고용으로 표시합니다. 이 실행에서 사용한 기준이라는 뜻은 아닙니다."
    snapshot = jev.get("criteria")
    if isinstance(snapshot, list) and snapshot:
        return snapshot, "이 실행에 저장된 평가 기준입니다. 질문별로 선택한 기준과 적용 근거는 아래 세부 결과에서 확인할 수 있습니다."
    # Older files may have judgments but no definitions. Recover only IDs and names
    # recorded in the results, never substitute today's rules for missing history.
    criteria_by_id = {cid: {"id": cid, "name": s.get("name") or cid}
                      for cid, s in (jev.get("summary") or {}).items()}
    for record in jev.get("records") or []:
        decisions = record.get("decisions") or {}
        for cid in list(record.get("criteria") or []) + list(decisions):
            criteria_by_id.setdefault(cid, {"id": cid, "name": (decisions.get(cid) or {}).get("name") or cid})
    return list(criteria_by_id.values()), ("평가 당시 기준 정의가 저장되지 않은 결과입니다. 기록에 있는 기준 ID와 판정만 표시하며, "
                                           "당시 설명·적용 조건은 확인할 수 없습니다. 현재 기준으로 대체하지 않습니다.")


def _jev_overview(criteria: List[Dict[str, Any]]) -> str:
    return (f'<div class="criteria-grid" aria-label="Jev 의미 평가 기준 {len(criteria)}개">'
            + ''.join(f'<article class="criterion-card jev"><span class="code">{_e(c["id"])}</span>'
                      f'<h3>{_e(c.get("name") or c["id"])}</h3>'
                      f'<p>{_e(c.get("question") or "평가 당시 기준 설명이 저장되지 않았습니다.")}</p>'
                      + (f'<p>{_e(c["boundary"])}</p>' if c.get("boundary") else "")
                      + f'<p><b>적용</b> {_e(c.get("applies_when") or "적용 조건은 이 결과에 저장되지 않았습니다. 질문별 적용 근거를 확인해 주세요.")}</p>'
                      + (f'<div class="criterion-example"><b>예시</b>{_e(c["example"])}</div>' if c.get("example") else "")
                      + '</article>' for c in criteria)
            + '</div>')


def _llm_overview() -> str:
    descriptions = (
        "질문에서 요청한 핵심 작업을 답변이 수행했는지 확인합니다.",
        "설명이 질문의 주제와 요청에 맞는지 확인합니다.",
        "요청을 이해하고 활용하기에 필요한 설명이 있는지 확인합니다.",
        "질문에 자료가 있을 때 해석이 그 자료와 일치하는지 확인합니다.",
        "코드 평가에서 FAIL이 난 기준마다 실패 사유가 적합한지 확인합니다.",
    )
    return ('<div class="criteria-grid" aria-label="FAIL 질문 LLM 재검토 기준 5개">'
            + ''.join(f'<article class="criterion-card"><span class="code">{n}</span>'
                      f'<h3>{_e(name)}</h3><p>{_e(description)}</p>'
                      f'<div class="criterion-example"><b>예시</b>{_e(_LLM_EXAMPLES[key])}</div></article>'
                      for n, ((key, name), description) in enumerate(zip(LLM_REVIEW_CRITERIA, descriptions), 1))
            + '</div>')


def _llm_result(batch: Dict[str, Any], keys: List[str]) -> str:
    queue = failed_case_queue(batch)
    if not queue:
        return '<div class="result-status">LLM 재검토 대상: 코드 FAIL 질문 0개</div>'
    review = batch.get("llm_review")
    if not review or review.get("source_digest") != batch.get("result_digest"):
        return (f'<div class="result-status">LLM 재검토 대상: 코드 FAIL 질문 {len(queue)}개 · '
                '아직 실행하지 않았습니다. 코드 판정과 별도로 결과를 기록합니다.</div>')
    if review.get("status") == "ACCESS_REQUIRED":
        return (f'<div class="result-status">LLM 재검토 대상: 코드 FAIL 질문 {len(queue)}개 · '
                '모델 연결 정보가 없어 미실행 상태입니다.</div>')
    rows = []
    labels = dict(LLM_REVIEW_CRITERIA)
    indices = [i for i, c in enumerate(batch["cases"]) if c.get("case_status") == "EVALUATED"
               and any(r.get("counted") and r.get("verdict") == "FAIL" for r in c.get("results", []))]
    for item, record, index in zip(queue, review.get("records", []), indices):
        link = _case_link(item["case_id"], keys, index)
        if record.get("status") != "COMPLETED":
            rows.append(f'<tr><td>{link}</td>'
                        f'<td>LLM 재검토</td><td>{_e(record.get("status"))}</td><td>판정 미완료</td></tr>')
            continue
        result = record["review"]
        for key, _ in LLM_REVIEW_CRITERIA[:4]:
            entry = result["criteria"][key]
            rows.append(f'<tr><td>{link}</td>'
                        f'<td>{_e(labels[key])}</td><td>{_e(entry["verdict"])}</td>'
                        f'<td>{_e(entry["reason"])}</td></tr>')
        failed_names = {r["condition_id"]: f'{r["subcategory"]} {r["title"]}' for r in item["failed_checks"]}
        for entry in result["failed_checks"]:
            rows.append(f'<tr><td>{link}</td>'
                        f'<td>{_e(labels["code_fail_reason"])} · {_e(failed_names[entry["condition_id"]])}</td>'
                        f'<td>{_e(entry["reason_valid"])}</td><td>{_e(entry["reason"])}</td></tr>')
    return (f'<div class="result-status">LLM 재검토 대상 {len(queue)}개 · 완료 '
            f'{review.get("completed_questions", 0)}개 · 코드 판정은 변경하지 않았습니다.</div>'
            f'<div class="card wrap"><table><thead><tr><th>질문</th><th>검토 항목</th><th>판정</th>'
            f'<th>근거</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def _special_overview() -> str:
    cards = []
    for item in SPECIALIZED_ITEMS:
        cards.append(f'<article class="special-card"><div class="criterion-title"><h3>{_e(item["name"])}</h3>'
                     f'</div><p>{_e(item["condition"])}</p>'
                     f'<div class="criterion-example"><b>예시</b>{_e(_SPECIAL_EXAMPLES[item["id"]])}</div></article>')
    return '<div class="special-grid">' + ''.join(cards) + '</div>'


_UNJUDGED_STATUSES = {
    Status.CONFIG_MISSING, Status.DISPLAY_MISSING, Status.INCOMPLETE,
    Status.UNSUPPORTED, Status.DEPENDENCY_FAILED, Status.CHECK_ERROR,
    Status.LIMIT_EXCEEDED,
}


def _question_counts(cases: List[Dict[str, Any]], area: str, subcategory: str | None = None) -> Dict[str, Any]:
    """Count each evaluated question once within the requested result scope."""
    counts = {"pass": 0, "fail": 0, "unjudged": 0, "excluded": 0}
    for case in cases:
        if case.get("case_status") != "EVALUATED":
            continue
        rows = [r for r in case["results"] if r["area"] == area
                and (subcategory is None or r["subcategory"] == subcategory)]
        if any(r["counted"] and r["verdict"] == "FAIL" for r in rows):
            counts["fail"] += 1
        elif any(r["verdict"] is None and r["status"] in _UNJUDGED_STATUSES for r in rows):
            counts["unjudged"] += 1
        elif area == "common" and case.get("errors"):
            counts["unjudged"] += 1  # 검사 실행 오류가 있는 질문은 PASS 로 확정하지 않는다
        elif any(r["counted"] and r["verdict"] == "PASS" for r in rows):
            counts["pass"] += 1
        elif subcategory is not None and any(r["status"] == Status.OUT_OF_SCOPE for r in rows):
            counts["excluded"] += 1
    decided = counts["pass"] + counts["fail"]
    counts["pass_rate"] = counts["pass"] / decided if decided else None
    return counts


def _result_breakdowns(batch: Dict[str, Any]) -> str:
    common_rows = []
    for category in CATEGORIES:
        common_rows.append(f'<tr class="group-row"><th colspan="5" scope="rowgroup">{_e(category["id"])} · {_e(category["name"])}</th></tr>')
        for sub in category["subs"]:
            stats = _question_counts(batch["cases"], "common", sub["id"])
            common_rows.append(f'<tr><th scope="row">{_e(sub["id"])} {_e(sub["name"])}</th>'
                               f'<td>{stats["pass"]}</td><td>{stats["fail"]}</td><td>{stats["unjudged"]}</td>'
                               f'<td>{_rate(stats["pass_rate"])}</td></tr>')
    special_rows = []
    for item in SPECIALIZED_ITEMS:
        stats = _question_counts(batch["cases"], "aiona_specialized", item["id"])
        special_rows.append(f'<tr><th scope="row">{_e(item["name"])}</th><td>{stats["pass"]}</td>'
                            f'<td>{stats["fail"]}</td><td>{stats["unjudged"]}</td><td>{stats["excluded"]}</td></tr>')
    return (f'<div class="result-breakdowns"><div class="result-breakdown wrap"><h3>공통 기준별 결과 · 질문 수</h3>'
            f'<table><thead><tr><th scope="col">기준</th><th scope="col">PASS</th><th scope="col">FAIL</th>'
            f'<th scope="col">미판정</th><th scope="col">통과율</th></tr></thead><tbody>{"".join(common_rows)}</tbody></table></div>'
            f'<div class="result-breakdown wrap"><h3>AIONA 특화 결과 · 질문 수</h3><table><thead><tr><th scope="col">기준</th>'
            f'<th scope="col">PASS</th><th scope="col">FAIL</th><th scope="col">미판정</th>'
            f'<th scope="col">적용 제외</th></tr></thead><tbody>{"".join(special_rows)}</tbody></table>'
             f'<p class="note">공통 통과율과 별도로 봅니다.</p></div></div>')


def _specialized_status_note(batch: Dict[str, Any]) -> str:
    missing = [r for case in batch["cases"] for r in case.get("results", [])
               if r.get("area") == "aiona_specialized" and r.get("status") == Status.CONFIG_MISSING]
    if not missing:
        return ""
    names = {r["subcategory"] for r in missing}
    requirements = []
    if "AIONA-M" in names:
        requirements.append("내부 마커 규칙")
    if "AIONA-F" in names:
        requirements.append("질문별 금지 표현 목록")
    return (f'<p class="detail-note"><strong>AIONA 특화 검사 미완료:</strong> '
            f'{len(missing)}건은 {"과 ".join(requirements)}이 없어 PASS/FAIL을 확정하지 못했습니다. '
            '공통 검사 결과와 별도로 확인해야 합니다.</p>')


def _donut(segments: List[Tuple[int, str]], size: int = 150, stroke: int = 20) -> str:
    """질문 수 비율 원그래프. 인라인 SVG 라 스크립트·외부 리소스가 필요 없다."""
    r = (size - stroke) / 2
    circ = 2 * math.pi * r
    total = sum(v for v, _ in segments) or 1
    mid = size / 2
    parts = [f'<circle cx="{mid}" cy="{mid}" r="{r:.2f}" fill="none" style="stroke:#e9eef3" stroke-width="{stroke}"/>']
    offset = 0.0
    for value, color in segments:
        if not value:
            continue
        length = circ * value / total
        parts.append(f'<circle cx="{mid}" cy="{mid}" r="{r:.2f}" fill="none" style="stroke:{color}" stroke-width="{stroke}" '
                     f'stroke-dasharray="{length:.2f} {circ - length:.2f}" stroke-dashoffset="{-offset:.2f}" '
                     f'transform="rotate(-90 {mid} {mid})"/>')
        offset += length
    return f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" aria-hidden="true">{"".join(parts)}</svg>'


def _where_html(where: Any) -> str:
    """답변의 틀린 부분 — 줄 번호와 해당 줄, 강조할 글자를 보여 준다. 위치가 없으면 그렇다고 적는다."""
    if not where:
        return '<span class="note">답변에서 위치를 특정하지 못했습니다 (이유 참고)</span>'
    rows = []
    for ln in where["lines"]:
        text, parts, pos = ln["text"], [], 0
        spans = sorted({(i, i + len(h)) for h in ln["hits"] if (i := text.find(h)) >= 0})
        for s, e in spans:
            if s >= pos:
                parts += [_e(text[pos:s]), f"<mark>{_e(text[s:e])}</mark>"]
                pos = e
        parts.append(_e(text[pos:]))
        rows.append(f'<div class="wl{" mark" if ln["mark"] else ""}"><span class="ln">{ln["n"]}</span>'
                    f'<span class="lt">{"".join(parts) or "&nbsp;"}</span></div>')
    return f'<div class="where-label">{_e(where["label"])}</div><div class="where-lines">{"".join(rows)}</div>'


def _overall_section(batch: Dict[str, Any], keys: List[str], criteria: List[Dict[str, Any]]) -> str:
    """종합 결과 — CODE 와 의미 평가를 질문 단위로 합친 결과를 평가 기준 다음에 보여 준다 (가중치 점수 없음)."""
    s = combined_summary(batch)
    names = {c["id"]: c.get("name") or c["id"] for c in criteria}
    for area in s["areas"]:
        if area["group"] != "CODE" and area["id"] in names:
            area["name"] = names[area["id"]]
    engine = _semantic_label(batch.get("jev_eval") or {}) if s["semantic_engine"] else None
    scope = f"CODE 평가 + 의미 평가({engine})" if engine else "CODE 평가"
    colors = {"pass": "var(--green)", "code": "var(--red)", "sem": "var(--violet)", "both": "#6b1f2a"}
    segments = [(s["pass"], colors["pass"]), (s["fail_code_only"], colors["code"]),
                (s["fail_semantic_only"], colors["sem"]), (s["fail_both"], colors["both"]), (s["pending"], "#9b7b35")]
    legend = "".join(f'<span><i style="background:{color}"></i>{label} <b>{value}</b></span>' for label, value, color in (
        ("통과", s["pass"], colors["pass"]), ("CODE에서 발견", s["fail_code_only"], colors["code"]),
        (f"의미 평가에서 발견", s["fail_semantic_only"], colors["sem"]), ("둘 다", s["fail_both"], colors["both"]),
        ("평가 미완료", s["pending"], "#9b7b35")))
    headline = (f'{s["questions"]}문항 중 PASS {s["pass"]} · FAIL {s["fail"]} · 평가 미완료 {s["pending"]}' if s["fail"] or s["pending"]
                else f'{s["questions"]}문항 모두 통과')
    notes = ['질문마다 CODE 평가나 의미 평가 중 하나라도 FAIL이면 FAIL 질문으로 셉니다. 가중치를 준 점수가 아닙니다.']
    if s["semantic_pending"]:
        why = _JEV_STATUS_KO.get(s["semantic_status"] or "", "결과 없음")
        whose = f'{engine} 의미 평가' if engine else "의미 평가"
        notes.append(f'{_e(whose)}를 하지 못한 {s["semantic_pending"]}문항({_e(why)})은 평가 미완료입니다. 확인된 FAIL은 유지하고 PASS로 처리하지 않습니다.')
    if any((r or {}).get("code_completion") for r in (batch.get("jev_eval") or {}).get("records") or []):
        notes.append("공통 조건은 코드 판정과 LLM 보완 판정을 함께 반영합니다. AIONA 내부 마커·금지 표현은 별도 표시하며, 코드 원본 결과와 해시는 JSON에 보존합니다.")
    if (batch.get("jev_eval") or {}).get("engine") in ("claude_cli", "codex_cli", "openai_api", "anthropic_api"):
        models = ", ".join((batch.get("jev_eval") or {}).get("models") or [])
        reasoning = ((batch.get("jev_eval") or {}).get("client") or {}).get("reasoning_effort")
        notes.append(f"의미 평가는 {_e(engine)}{f'({_e(models)})' if models else ''}로 판단했습니다. "
                     + (f"추론 수준은 {_e(reasoning)}입니다. " if reasoning else "")
                     + "판단 확신도는 모델이 스스로 적은 값이라 보정된 확률이 아닙니다.")
    if s["fail_semantic_only"] + s["fail_both"]:
        notes.append("판단 확신도는 의미 평가 엔진이 그 PASS/FAIL 판단을 얼마나 확신하는지(0~100%) 나타냅니다.")
    conversations = (batch.get("jev_eval") or {}).get("conversations") or {}
    if conversations.get("count"):
        notes.append(f'대화 입력은 턴마다 한 문항으로 셉니다(대화 {conversations["count"]}개). 대화 단위의 J6 결과는 아래 ‘대화 유지율’에 있습니다.')
    rate = "—" if s["rate"] is None else f'{s["rate"] * 100:.0f}%'
    rows = []
    for a in s["areas"]:
        if a["group"] != "CODE" and not a["targets"]:
            continue  # 이 실행에 적용된 질문이 없는 의미 평가 기준(예: 단일 질문·답변 입력의 J6)은 막대를 그리지 않는다
        tag = "code" if a["group"] == "CODE" else "sem"
        width = 0 if a["rate"] is None else a["rate"] * 100
        count = f'FAIL {a["fail"]} · 대상 {a["targets"]}' if a["pass"] + a["fail"] else (
            "미평가" if a["targets"] else "대상 없음")
        rows.append(f'<div class="area-row"><span class="area-tag {tag}">{_e(a["group"])}</span>'
                    f'<span class="area-name">{_e(a["id"] if a["group"] != "CODE" else "")} {_e(a["name"])}</span>'
                    f'<span class="bar"><span class="bar-fill {tag}" style="width:{width:.1f}%"></span></span>'
                    f'<span class="area-rate{" is-fail" if a["fail"] else ""}">{_rate(a["rate"])}</span>'
                    f'<span class="area-count">{count}</span></div>')
    problem_rows = []
    for p in s["problems"]:
        for it in p["items"]:
            tag = "code" if it["source"] == "CODE" else "sem"
            detail = it.get("detail") or it["source"]
            if it["confidence"] is not None:
                detail += f' · 판단 확신도 {it["confidence"] * 100:.0f}%'
            crit = (f'<span class="crit-id {tag}">{_e(it.get("id") or "")}</span> <b>{_e(it.get("name") or it["label"])}</b>'
                    f'<div class="note">{_e(detail)}</div>')
            problem_rows.append(f'<tr><td class="nowrap">{_case_link(p["case_id"], keys, p["case_index"])}</td>'
                                f'<td class="crit-cell">{crit}</td><td class="reason-cell">{_e(it["reason"])}</td>'
                                f'<td class="where-cell">{_where_html(it.get("where"))}</td></tr>')
    problems = (f'<div class="card wrap"><h3>FAIL 질문 {len(s["problems"])}개</h3><table><thead><tr><th>질문</th>'
                f'<th>FAIL 난 기준</th><th>이유</th><th>틀린 부분</th></tr></thead><tbody>{"".join(problem_rows)}</tbody></table></div>'
                if problem_rows else '<div class="card"><p>문제가 확인된 질문이 없습니다.</p></div>')
    return (f'<section id="overall" class="section-anchor" aria-labelledby="overall-heading"><div class="section-head">'
            f'<h2 id="overall-heading">종합 결과</h2><p>{_e(scope)} · 질문 단위</p></div>'
            f'<div class="overall-card"><div class="overall-chart">{_donut(segments)}<div class="donut-center"><b>{rate}</b>'
            f'<span>종합 통과율</span></div></div><div class="overall-text"><strong>{_e(headline)}</strong>'
            f'<div class="legend">{legend}</div>{"".join(f"<p class=note>{n}</p>" for n in notes)}</div></div>'
            f'<div class="area-card"><h3>영역별 통과율 · 질문 기준</h3>{"".join(rows)}'
            f'<p class="note">각 영역을 실제로 판정한 질문만 셉니다. CODE는 대분류별, 의미 평가는 기준별입니다.</p></div>'
            f'{_conversation_section(batch, keys)}{problems}</section>')


def _conversation_section(batch: Dict[str, Any], keys: List[str]) -> str:
    """대화 유지율 — 대화 입력(conversation_id·turn)이 있을 때만. 대화별 J6 결과·첫 FAIL 턴과 턴 구간별 유지율."""
    conv = (batch.get("jev_eval") or {}).get("conversations") or {}
    if not conv.get("count"):
        return ""
    rate = "—" if conv.get("rate") is None else f'{conv["rate"] * 100:.0f}%'
    bands = "".join(f'<tr><td>{_e(label)}턴</td><td>{b["targets"]}</td><td>{b["pass"]}</td>'
                    f'<td class="{"fail-num" if b["fail"] else ""}">{b["fail"]}</td><td>{_rate(b["rate"])}</td></tr>'
                    for label, b in conv["bands"].items() if b["targets"])
    items = []
    for it in conv["items"]:
        verdict = (f'<span class="v {it["result"]}">{it["result"]}</span>' if it["result"] in ("PASS", "FAIL")
                   else '<span class="v NONE">미완료</span>')
        first = "—"
        if it["first_fail_turn"] is not None:
            hit = next((t for t in it["turn_results"] if t["turn"] == it["first_fail_turn"]), None)
            first = _case_link(f'{it["first_fail_turn"]}턴', keys, _turn_index(batch["cases"], hit, it["id"])) if hit else f'{it["first_fail_turn"]}턴'
        strip = " ".join(f'<span class="v {t["decision"] or "NONE"}" title="{t["turn"]}턴 J6 {t["decision"] or "미실행"}">'
                         f'{_case_link(t["turn"], keys, _turn_index(batch["cases"], t, it["id"]))}</span>'
                         for t in it["turn_results"] if t["asked"])
        items.append(f'<tr><td class="nowrap">{_e(it["id"])}</td><td>{it["turns"]}</td><td>{it["j6_turns"]}</td><td>{verdict}</td>'
                     f'<td>{first}</td><td>{strip or "—"}</td><td class="note">{_e(", ".join(it["issues"])) if it["issues"] else "—"}</td></tr>')
    return (f'<div class="card wrap" id="conversations"><h3>대화 유지율 · J6 LONG CONTEXT</h3>'
            f'<p class="note">대화 {conv["count"]}개 · PASS {conv["pass"]} · FAIL {conv["fail"]} · 미완료 {conv["incomplete"]} · 유지율 {rate}. {_e(conv.get("note") or "")}</p>'
            f'<table><thead><tr><th>턴 구간</th><th>J6 평가 턴</th><th>PASS</th><th>FAIL</th><th>유지율</th></tr></thead><tbody>{bands}</tbody></table>'
            f'<table style="margin-top:10px"><thead><tr><th>대화</th><th>턴 수</th><th>J6 평가 턴</th><th>결과</th><th>첫 FAIL 턴</th>'
            f'<th>턴별 J6 (번호를 누르면 해당 턴으로)</th><th>비고</th></tr></thead><tbody>{"".join(items)}</tbody></table></div>')


def _semantic_label(semantic: Dict[str, Any]) -> str:
    return semantic.get("engine_label") or {"openai_api": "OpenAI API", "anthropic_api": "Claude API",
        "codex_cli": "GPT CLI", "claude_cli": "Claude CLI"}.get(semantic.get("engine"), "Jev")


def render_report(batch: Dict[str, Any], title: str = "AIONA 코드 평가 결과") -> str:
    from .common_adjudication import resolved_batch  # 옛 결과의 code_completion(LLM 보완) 투영 — 새 결과는 그대로
    batch = resolved_batch(batch)
    summ = batch["summary"]
    t = summ["totals"]
    common_questions = _question_counts(batch["cases"], "common")
    jev_records = (batch.get("jev_eval") or {}).get("records") or []
    jev = batch.get("jev_eval") or {}
    jev_label = _semantic_label(jev)
    criteria, criteria_note = _report_criteria(batch)
    criterion_ids = [c["id"] for c in criteria]
    criterion_label = (f'J1~J{len(criteria)}' if criterion_ids == [f'J{i + 1}' for i in range(len(criteria))] and criteria
                       else " · ".join(criterion_ids) or "저장된 기준 없음")
    criteria_version = (batch.get("jev_eval") or {}).get("criteria_version")
    version_note = f' · {_e(criteria_version)}' if criteria_version else ""
    keys = _case_keys(batch["cases"])
    cases = "".join(_case(c, jev_records[i] if i < len(jev_records) else None, jev_label, keys[i], criteria) for i, c in enumerate(batch["cases"]))
    save_warning = (f'<div class="result-status" role="alert"><strong>자동 저장 실패</strong> · {_e(batch["run"]["save_error"])}'
                    '<p>평가 결과는 계속 확인할 수 있습니다. 서버를 종료하기 전에 실행 화면에서 JSON을 직접 다운로드해 주세요. '
                    '이 HTML 보고서도 별도로 보관해 주세요.</p></div>' if batch["run"].get("save_error") else "")
    llm_section = ("" if not batch.get("llm_review") else
                   '<section aria-labelledby="llm-heading"><div class="section-head"><h2 id="llm-heading">LLM 재검토 (별도 실행)</h2>'
                   '<p>기존 5개 기준 · 기본 흐름 밖</p></div><p class="intro">코드 평가에서 FAIL이 나온 질문만 재검토한 별도 실행 결과입니다. '
                   'CODE·Jev 판정은 바꾸지 않습니다.</p>' + _llm_overview() + _llm_result(batch, keys) + '</section>')
    affected = sum(1 for case in batch["cases"] if any(r.get("verdict") == "FAIL" for r in case.get("results", [])))
    errored = sum(1 for case in batch["cases"] if case.get("errors"))
    outcome = (f'CODE 평가: {t["cases"]}개 입력 중 {affected}개에서 FAIL 발견' if affected
               else f'CODE 평가: {t["cases"]}개 입력에서 확인된 FAIL 없음') + (f' · 검사 실행 오류 {errored}개 질문' if errored else '')
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<title>{_e(title)}</title><style>{_CSS}</style></head><body><main>
<header class="report-head"><div><span class="eyebrow">AIONA · 평가 리포트</span><h1>{_e(title)}</h1>
<p>평가 기준을 먼저 보여 주고, 종합 결과와 세부 판정을 이어서 보여 줍니다.</p></div><span class="head-badge">이번 실행 · CODE + {_e(jev_label)}</span></header>
{save_warning}
<section aria-labelledby="common-heading" id="criteria" class="section-anchor"><div class="section-head"><h2 id="common-heading">01 · 공통 평가 기준</h2><p>3개 대분류 · 8개 세부 기준</p></div>
<p class="intro">각 항목에서 무엇을 확인하는지 설명합니다.</p>{_category_overview()}</section>
<section aria-labelledby="jev-heading"><div class="section-head"><h2 id="jev-heading">02 · Jev 의미 평가 기준</h2><p>{_e(criterion_label)} · Semantic Evaluation{version_note}</p></div>
<p class="intro">{_e(criteria_note)}</p>{_jev_overview(criteria)}</section>
<section aria-labelledby="special-heading"><div class="section-head"><h2 id="special-heading">03 · AIONA 특화 평가 기준</h2><p>AIONA 전용 2개 기준</p></div>
<p class="intro">AIONA 서비스에 적용하는 별도 검사입니다.</p>{_special_overview()}</section>
{_overall_section(batch, keys, criteria)}
 <section aria-labelledby="result-heading" id="results" class="section-anchor"><div class="section-head"><h2 id="result-heading">평가 결과 한눈에 보기</h2><p>CODE Evaluation · 코드 검사 결과</p></div>
 <p class="intro">해당 없음은 질문에 적용되지 않아 건너뛴 기준, 판정 미완료는 필요한 검사나 재검토의 결과를 확정하지 못한 상태입니다. 중복 제외는 같은 조건을 다른 항목에서 이미 센 경우입니다.</p>
<div class="outcome"><div><strong>{outcome}</strong><p>CODE로 확인된 조건의 결과입니다. 의미 평가까지 합친 결과는 바로 위 ‘종합 결과’에 있습니다.</p></div><a href="#overall">FAIL 질문 보기 ↑</a></div>
<div class="kpi-grid"><div class="kpi"><span class="kpi-label">입력 질문</span><b>{t['cases']}</b><span class="note">평가 {t['evaluated']} · 오류 {t['input_errors']} · 한도 초과 {t['limit_exceeded']} · 검사 오류 {t.get('check_errors', 0)}</span></div>
<div class="kpi"><span class="kpi-label">공통 PASS 질문</span><b>{common_questions['pass']}</b><span class="note">확인된 공통 조건에 FAIL 없는 질문</span></div>
<div class="kpi is-fail"><span class="kpi-label">공통 FAIL 질문</span><b>{common_questions['fail']}</b><span class="note">공통 조건에 FAIL 있는 질문</span></div>
<div class="kpi"><span class="kpi-label">공통 미판정 질문</span><b>{common_questions['unjudged']}</b><span class="note">확정된 공통 판정이 없는 질문</span></div>
<div class="kpi"><span class="kpi-label">공통 질문 통과율</span><b>{_rate(common_questions['pass_rate'])}</b><span class="note">PASS 질문 ÷ (PASS + FAIL 질문)</span></div></div>
 <p class="detail-note">질문마다 한 번만 셉니다. FAIL이 하나라도 있으면 FAIL, FAIL 없이 PASS가 있으면 PASS입니다. 미판정 검사만 있는 질문은 통과율에서 제외합니다. 이 비율은 답변 전체의 품질 점수가 아닙니다.</p>
 {_result_breakdowns(batch)}
 {_specialized_status_note(batch)}
<details class="config-details"><summary>특화 평가 설정 정보 (개발자용)</summary><pre>{_e(batch["aiona_config"])}</pre></details></section>
{llm_section}
<section id="case-results" aria-labelledby="case-heading"><div class="section-head"><h2 id="case-heading">질문별 평가 결과</h2><p>검사 내용 · 실패 이유 · 문제 위치</p></div>{cases}</section>
<p class="meta">엔진 {_e(batch["versions"]["engine"])} · 결과 해시 {_e(batch["result_digest"][:16])} · 실행 {_e(batch["run"]["started_at"])}</p>
</main></body></html>"""
