"""종합 결과 — CODE 평가와 의미 평가(Jev 또는 시험 엔진)를 질문 단위로 합쳐 보여 주기 위한 집계.

- 공통 CODE(저장된 옛 보완 판정 포함)나 의미 평가 중 하나라도 FAIL 이면 'FAIL 질문'.
  모두 판정한 질문만 PASS이며, 나머지는 pending. 검사 실행 오류(case["errors"])가 있는 질문은 PASS 로 확정하지 않는다.
  AIONA 특화는 별도 집계한다.
- 가중치 점수를 만들지 않는다. 질문 수와 통과 비율만 쓴다.
- 영역별 통과율은 그 영역을 실제로 판정한 질문만 분모로 쓴다 (CODE 대분류 3개 + AIONA 특화 + J1~J7).
- CODE 판정·결과 해시와 의미 평가 기록은 바꾸지 않는다. 보고서·화면은 이 결과만 읽는다.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

from .jev_criteria import CRITERIA
from .model import CATEGORY_NAMES
from .common_adjudication import resolved_batch  # 옛 결과의 code_completion 투영 (읽기 전용)

__all__ = ["combined_summary"]

#: 근거 문장에서 따옴표로 인용한 부분 (‘…’ '…' “…” "…" 「…」)
_QUOTED = re.compile(r"[‘'“\"「]([^‘’'“”\"「」\n]{2,80})[’'”\"」]")


def _rate(passed: int, failed: int) -> Optional[float]:
    return passed / (passed + failed) if passed + failed else None


def _code_fails(case: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [r for r in case.get("results", []) if r.get("area") == "common" and r.get("counted") and r.get("verdict") == "FAIL"]


def _code_where(r: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """CODE FAIL 의 위치 — 판정 설명(explain)의 첫 위치를 답변 줄 번호와 강조할 글자로 정리한다."""
    for loc in (r.get("explain") or {}).get("locations") or []:
        if loc.get("target", "answer") != "answer":
            continue
        if loc["kind"] == "code":
            marked = [i for i, ln in enumerate(loc["lines"]) if ln["mark"]]
            if not marked:
                continue
            around = loc["lines"][max(0, marked[0] - 1):marked[-1] + 2]
            return {"label": loc["label"], "lines": [{"n": ln.get("answer_line") or ln["n"], "text": ln["text"],
                                                      "mark": ln["mark"], "hits": []} for ln in around]}
        if loc["kind"] == "snippet" and loc.get("line"):
            if loc.get("segments"):
                text = "".join(sg["text"] for sg in loc["segments"])
                hits = [sg["text"] for sg in loc["segments"] if sg["mark"]]
            else:
                text, hits = loc.get("before", "") + loc.get("hit", "") + loc.get("after", ""), [loc.get("hit", "")]
            return {"label": f'답변 {loc["line"]}번째 줄', "lines": [{"n": loc["line"], "text": text, "mark": True,
                                                                  "hits": [h for h in hits if h]}]}
        if loc["kind"] == "list" and loc.get("items"):
            marks = set(loc.get("marks") or [])
            return {"label": loc["label"], "lines": [{"n": i + 1, "text": t, "mark": i in marks, "hits": []}
                                                     for i, t in enumerate(loc["items"][:6])]}
    return None


def _quoted_where(reason: str, answer: str) -> Optional[Dict[str, Any]]:
    """의미 평가 근거가 따옴표로 인용한 부분 중 답변에 글자 그대로 있는 것만 위치로 쓴다 (지어내지 않음)."""
    lines = answer.split("\n")
    found: Dict[int, List[str]] = {}
    for quote in _QUOTED.findall(reason or ""):
        quote = quote.strip()
        for n, line in enumerate(lines, 1):
            if quote and quote in line:
                found.setdefault(n, []).append(quote)
                break
    if not found:
        return None
    rows = [{"n": n, "text": lines[n - 1], "mark": True, "hits": hits} for n, hits in sorted(found.items())[:2]]
    return {"label": "답변 " + ", ".join(f'{row["n"]}번째' for row in rows) + " 줄 (근거가 인용한 부분)", "lines": rows}


def combined_summary(batch: Dict[str, Any]) -> Dict[str, Any]:
    batch = resolved_batch(batch)
    jev = batch.get("jev_eval") or {}
    records = jev.get("records") or []
    cases = batch["cases"]
    evaluated = [(i, c) for i, c in enumerate(cases) if c.get("case_status") == "EVALUATED"]

    problems, passed = [], 0
    review_items = []
    unverified_count = 0
    code_only = semantic_only = both = pending = undecided = 0
    for i, case in evaluated:
        record = records[i] if i < len(records) else None
        decisions = (record or {}).get("decisions") or {}
        unverified = [d for d in decisions.values() if d.get('status') in ('UNVERIFIED', 'INCOMPLETE')]
        if unverified:
            unverified_count += len(unverified)
            review_items.append({'case_id': case['case_id'], 'case_index': i, 'question': case.get('question') or '',
                                 'criteria': copy.deepcopy(unverified)})
        semantic_complete = bool(record and record.get("status") == "SUCCESS" and record.get("criteria")
                                 and all(decisions.get(cid, {}).get("decision") in ("PASS", "FAIL") for cid in record["criteria"]))
        if not semantic_complete:
            pending += 1
        code_fail = _code_fails(case)
        sem_fail = [d for d in decisions.values() if d.get("decision") == "FAIL"]
        if not code_fail and not sem_fail:
            common = (case.get("summary") or {}).get("common") or {}
            if (semantic_complete and not common.get("unjudged") and not case.get("errors")
                    and common.get("decided", common.get("pass", 0))):
                passed += 1
            else:
                undecided += 1
            continue
        code_only += bool(code_fail and not sem_fail)
        semantic_only += bool(sem_fail and not code_fail)
        both += bool(code_fail and sem_fail)
        items = [{"source": "CODE", "id": r["subcategory"], "name": r.get("subcategory_name") or "",
                  "detail": f'{CATEGORY_NAMES.get(r.get("category") or "", "AIONA 특화")} · {r["title"].split(" (")[0]}',
                  "label": f'{r["subcategory"]} {r.get("subcategory_name") or ""} › {r["title"]}',
                  "reason": r.get("reason") if r.get("evaluation_method") == "LLM_COMPLETION" else
                            (r.get("explain") or {}).get("headline") or r.get("reason") or "",
                  "confidence": r.get("confidence"),
                  "where": _quoted_where(r.get("reason") or "", case.get("answer") or "")
                           if r.get("evaluation_method") == "LLM_COMPLETION" else _code_where(r)}
                 for r in code_fail]
        # Jev 는 근거 문장을 주지 않으므로, 없으면 이 질문에서 확인한 것(적용 판정의 '볼 것')을 보여 준다
        looks = ((record or {}).get("applicability") or {}).get("evidence") or {}
        items += [{"source": "의미 평가", "id": d["criterion_id"], "name": d["name"], "detail": "의미 평가",
                   "label": f'{d["criterion_id"]} {d["name"]}',
                   "reason": d.get("reason") or (f'확인한 것: {looks[d["criterion_id"]]["look_for"]}'
                                                 if d["criterion_id"] in looks else ""),
                   "confidence": d.get("confidence"), "where": _quoted_where(d.get("reason") or "", case.get("answer") or ""),
                   **{k: copy.deepcopy(d[k]) for k in ('fact_check', 'context_checks', 'subs', 'reason_code') if k in d}}
                  for d in sem_fail]
        problems.append({"case_id": case["case_id"], "case_index": i, "question": case.get("question") or "",
                         "sources": sorted({it["source"] for it in items}, key=["CODE", "의미 평가"].index), "items": items})

    areas: List[Dict[str, Any]] = []
    for cat, name in CATEGORY_NAMES.items():
        p = f = 0
        for _, case in evaluated:
            rows = [r for r in case.get("results", []) if r.get("area") == "common" and r.get("category") == cat and r.get("counted")]
            # 이 대분류의 검사기가 실행 오류로 죽은 질문은 남은 PASS 행만으로 PASS 로 세지 않는다
            errored = {str(e.get("subcategory", "")).split("-")[0] for e in case.get("errors") or []}
            if any(r["verdict"] == "FAIL" for r in rows):
                f += 1
            elif (rows and cat not in errored
                  and not (case.get("summary") or {}).get("by_category", {}).get(cat, {}).get("unjudged")):
                p += 1
        areas.append({"group": "CODE", "id": cat, "name": name, "targets": p + f, "pass": p, "fail": f, "rate": _rate(p, f)})
    p = f = 0
    for _, case in evaluated:
        rows = [r for r in case.get("results", []) if r.get("area") == "aiona_specialized" and r.get("counted")]
        if any(r["verdict"] == "FAIL" for r in rows):
            f += 1
        elif rows:
            p += 1
    if p + f:
        areas.append({"group": "CODE", "id": "AIONA", "name": "AIONA 특화", "targets": p + f, "pass": p, "fail": f, "rate": _rate(p, f)})
    for c in CRITERIA:
        s = (jev.get("summary") or {}).get(c.id) or {}
        p, f = s.get("pass", 0), s.get("fail", 0)
        areas.append({"group": "의미 평가", "id": c.id, "name": c.name, "targets": s.get("targets", p + f),
                      "pass": p, "fail": f, "pending": s.get('pending', max(0, s.get('targets', p + f) - p - f)),
                      "unverified": s.get('unverified', 0), "rate": _rate(p, f)})

    total = len(evaluated)
    return {
        "questions": total,
        "excluded": len(cases) - total,
        "pass": passed,
        "fail": len(problems),
        "pending": undecided,
        "rate": _rate(passed, len(problems)),
        "fail_code_only": code_only,
        "fail_semantic_only": semantic_only,
        "fail_both": both,
        "semantic_engine": (jev.get("engine_label") or {
            "anthropic_api": "Claude API", "openai_api": "OpenAI API", "codex_cli": "GPT CLI (Codex)",
            "claude_cli": "Claude CLI"}.get(jev.get("engine"), "Jev")) if jev else None,
        "semantic_status": jev.get("status") if jev else None,
        "semantic_pending": pending,
        "semantic_unverified": unverified_count,
        "review_required": len(review_items),
        "review_items": review_items,
        "areas": areas,
        "problems": problems,
        "note": "질문마다 CODE 나 의미 평가 중 하나라도 FAIL 이면 FAIL 질문. 가중치 점수 없음. "
                "판정할 수 없는 공통 조건은 행이 없음. 검사 실행 오류가 있는 질문은 PASS 로 집계하지 않음"
                + ("(옛 결과의 LLM 보완 기록은 그대로 반영)" if any((r or {}).get("code_completion") for r in records) else "")
                + ". 남은 평가 오류는 PASS로 집계하지 않음. AIONA 특화는 별도 집계.",
    }
