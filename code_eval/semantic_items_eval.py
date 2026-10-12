"""items 파이프라인의 집계 — 응답 items 파싱 → '판정하는 곳' 규칙(OVERRIDES) → 세부 기준·기준 집계 → decisions[cid] + 통계.

순수 함수, 모델 호출 없음. 설계 '판정·집계':
- 세부 기준은 적용 항목(NOT_APPLICABLE 제외)이 모두 YES 면 PASS, 하나라도 NO 면 FAIL, 적용 항목이 없으면 NOT_APPLICABLE.
- 기준은 세부 기준이 모두 PASS 면 PASS, 하나라도 FAIL 이면 FAIL, 모두 NOT_APPLICABLE 이면 NOT_APPLICABLE(decision None·status).
- decisions[cid] 는 legacy 최상위 키(criterion_id·name·decision·confidence·probabilities·reason·status?·reason_code?)를 그대로 두고
  ``subs`` 를 덧붙인다. J5 는 ``semantic_facts.legacy_projection`` 으로 fact_check 호환 투영을 더한다.
- 충족률 = 예 항목 수 ÷ 적용 항목 수 (origin='question' 항목만 분모 — 답변에서 생긴 항목(J5-1.a·J3-1.a)은 세부 기준 PASS/FAIL 로만 반영).
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

from .jev_criteria import CRITERIA
from .semantic_items import DECISIONS, ITEM_UNVERIFIABLE, OVERRIDES, REASON_CODE_PRIORITY, SPECS_BY_ID, SUBS_BY_ID
from .semantic_quotes import _split_id

__all__ = ["parse_item_answers", "apply_overrides", "sub_decision", "criterion_decision", "aggregate_items", "fulfillment"]

_NAMES = {c.id: c.name for c in CRITERIA}
_ORDER = [c.id for c in CRITERIA]
_ITEM_ORDER = {item_id: n for n, item_id in enumerate(SPECS_BY_ID)}
#: OVERRIDES 의 선행 조건 설명 (reason 문구용)
_OVERRIDE_WHY = {"NO": "아니오", "APPLIED": "적용"}


def _element_number(element: Optional[str]) -> int:
    try:
        return int(element) if element is not None else 0
    except ValueError:
        return 0


def _sort_key(item: Dict[str, Any]) -> Tuple[int, int, str]:
    base, element = _split_id(item.get("id"))
    return _ITEM_ORDER.get(base, len(_ITEM_ORDER)), _element_number(element), str(item.get("id"))


def parse_item_answers(items: Any, questions: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """검증을 마친 body["items"] 를 {criterion_id: [item…]} 로 묶는다 (기준표 순서).

    보내지 않은 항목 ID 나 잘못된 decision 은 ValueError. 보낸 항목 중 응답에 없는 ID 는 ``problems=["missing_answer"]`` 인 자리표시
    항목(decision NO · unverifiable)으로 채운다 — 판정에 '모른다' 값은 없다. 각 item 에 ``item_id``(base)·``element``(#n)·``origin``
    (항목 계약의 question|answer — 충족률 분모 판정에 쓴다) 를 더한다.
    """
    if not isinstance(items, list):
        raise ValueError("items 배열 없음")
    criteria = {cid for cid in (spec.get("criterion") for spec in questions.values() if isinstance(spec, dict)) if cid}
    grouped: Dict[str, List[Dict[str, Any]]] = {cid: [] for cid in _ORDER if cid in criteria}
    answered: set = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("item 이 객체가 아님")
        base, element = _split_id(raw.get("id"))
        spec = questions.get(base) if base else None
        if not isinstance(spec, dict):
            raise ValueError(f"보내지 않은 항목에 답함: {raw.get('id')!r}")
        cid = spec.get("criterion") or (SPECS_BY_ID[base].criterion if base in SPECS_BY_ID else None)
        if cid not in grouped:
            raise ValueError(f"보내지 않은 기준의 항목: {raw.get('id')!r}")
        if raw.get("decision") not in DECISIONS:
            raise ValueError(f"{raw.get('id')} decision 이 YES/NO/NOT_APPLICABLE 이 아님: {raw.get('decision')!r}")
        item = copy.deepcopy(raw)
        item["item_id"] = base
        item["element"] = element
        # origin 은 서버 소유(verify_items 가 적지만, 재확인 뒤 만들어진 자리표시 항목에는 없다) — 항목 계약의 값으로 확정한다
        item["origin"] = spec.get("origin") or (SPECS_BY_ID[base].origin if base in SPECS_BY_ID else "question")
        grouped[cid].append(item)
        answered.add(base)
    for base, spec in questions.items():
        if base in answered or not isinstance(spec, dict):
            continue
        cid = spec.get("criterion")
        if cid not in grouped:
            continue
        grouped[cid].append({"id": base, "item_id": base, "element": None, "decision": "NO", "question_quote": "", "answer_quote": "",
                             "reason": "응답에 이 항목의 판정이 없음 (확인 불가로 FAIL)", "turn": None, "fact": None,
                             "origin": spec.get("origin") or "question", "problems": ["missing_answer"],
                             "unverifiable": True, "reason_code": ITEM_UNVERIFIABLE, "model_original": None})
    for cid in grouped:
        grouped[cid].sort(key=_sort_key)
    return grouped


def _items_of(items_by_cid: Dict[str, List[Dict[str, Any]]], trigger: str) -> List[Dict[str, Any]]:
    """선행 항목(또는 세부 기준) ID 에 해당하는 항목들."""
    found: List[Dict[str, Any]] = []
    for items in items_by_cid.values():
        for item in items:
            base = item.get("item_id") or _split_id(item.get("id"))[0]
            if base == trigger or (trigger in SUBS_BY_ID and base in SPECS_BY_ID and SPECS_BY_ID[base].sub == trigger):
                found.append(item)
    return found


def apply_overrides(items_by_cid: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """'판정하는 곳' 표의 서버 집계 규칙을 제자리에 적용한다 — 반환은 적용된 규칙 목록 [{trigger, when, targets}].

    선행 항목이 NO(요소 중 하나라도) 또는 APPLIED(NOT_APPLICABLE 이 아닌 요소 존재)면 후속 항목들을 NOT_APPLICABLE 로 덮고
    reason 에 '서버 집계 규칙: …' 을 남긴다. 원래 판정은 ``model_decision`` 에 보존한다 (모든 항목에 기록).
    """
    for items in items_by_cid.values():
        for item in items:
            item.setdefault("model_decision", item.get("decision"))
    applied: List[Dict[str, Any]] = []
    for trigger, when, targets in OVERRIDES:
        sources = _items_of(items_by_cid, trigger)
        if when == "NO":
            fired = any(it.get("decision") == "NO" for it in sources)
        else:
            fired = any(it.get("decision") in ("YES", "NO") for it in sources)
        if not fired:
            continue
        changed: List[str] = []
        for target in targets:
            for item in _items_of(items_by_cid, target):
                if item.get("decision") == "NOT_APPLICABLE":
                    continue
                original = item.get("reason") or ""
                item["decision"] = "NOT_APPLICABLE"
                item["override"] = trigger
                item["reason"] = (f"서버 집계 규칙: {trigger} {_OVERRIDE_WHY[when]} → {target} 해당 없음"
                                  + (f" (원래 판정 {item.get('model_decision')}: {original})" if original else ""))
                changed.append(item["id"])
        if changed:
            applied.append({"trigger": trigger, "when": when, "targets": changed})
    return applied


def sub_decision(items: List[Dict[str, Any]]) -> str:
    """적용 항목(NOT_APPLICABLE 제외) 없음 → NOT_APPLICABLE · 하나라도 NO → FAIL · 모두 YES → PASS."""
    applied = [it for it in items if it.get("decision") != "NOT_APPLICABLE"]
    if not applied:
        return "NOT_APPLICABLE"
    return "FAIL" if any(it.get("decision") == "NO" for it in applied) else "PASS"


def criterion_decision(subs: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """(decision, status) — 모두 NOT_APPLICABLE → (None, 'NOT_APPLICABLE'); 하나라도 FAIL → ('FAIL', None); 나머지 → ('PASS', None)."""
    values = [sub.get("decision") if isinstance(sub, dict) else sub for sub in subs.values()]
    values = [v for v in values if v]
    if not values or all(v == "NOT_APPLICABLE" for v in values):
        return None, "NOT_APPLICABLE"
    return ("FAIL" if any(v == "FAIL" for v in values) else "PASS"), None


def _reason_code(items: List[Dict[str, Any]]) -> Optional[str]:
    codes = {it.get("reason_code") for it in items if it.get("decision") == "NO" and it.get("reason_code")}
    return next((code for code in REASON_CODE_PRIORITY if code in codes), None)


def _summary_reason(items: List[Dict[str, Any]], decision: Optional[str]) -> str:
    if decision is None:
        return "적용 항목 없음"
    failed = [it for it in items if it.get("decision") == "NO"]
    if not failed:
        return "적용 항목 모두 예"
    parts = [f"{it.get('id')}: {(it.get('reason') or '아니오').strip()}" for it in failed[:3]]
    if len(failed) > 3:
        parts.append(f"외 {len(failed) - 3}개")
    return " · ".join(parts)


def aggregate_items(items_by_cid: Dict[str, List[Dict[str, Any]]], questions: Dict[str, Any], *,
                    applicability: Optional[Dict[str, Any]] = None, rechecks: Optional[Dict[str, Any]] = None,
                    not_sent: Optional[List[Dict[str, Any]]] = None) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """(decisions, stats). ``not_sent`` 는 transport 가 보내지 않은 항목 [{id, reason}] — 그 세부 기준은 NOT_APPLICABLE·not_run 으로 남긴다."""
    from .semantic_facts import legacy_projection   # 지연 import — semantic_facts 가 이 모듈의 상수를 쓰지 않지만 순환을 피한다
    not_run: Dict[str, Dict[str, Any]] = {}
    for entry in not_sent or []:
        base = entry.get("id") if isinstance(entry, dict) else None
        if base in SPECS_BY_ID:
            not_run.setdefault(SPECS_BY_ID[base].sub, {"items": [], "reason": entry.get("reason")})["items"].append(base)
    criteria = [cid for cid in _ORDER if cid in items_by_cid or any(SUBS_BY_ID[s].criterion == cid for s in not_run)]
    decisions: Dict[str, Dict[str, Any]] = {}
    stats = {"applied": 0, "yes": 0, "no": 0, "not_applicable": 0, "question_items": 0, "answer_items": 0,
             "question_yes": 0, "fulfillment": None, "rechecks": int((rechecks or {}).get("rounds") or 0), "unverifiable": 0,
             "overrides": []}
    for cid in criteria:
        items = sorted(items_by_cid.get(cid, []), key=_sort_key)
        subs: Dict[str, Dict[str, Any]] = {}
        for item in items:
            base = item.get("item_id") or _split_id(item.get("id"))[0]
            sid = SPECS_BY_ID[base].sub if base in SPECS_BY_ID else (questions.get(base) or {}).get("sub") or base
            subs.setdefault(sid, {"sub_id": sid, "name": SUBS_BY_ID[sid].name if sid in SUBS_BY_ID else sid, "decision": None, "items": []})
            subs[sid]["items"].append(copy.deepcopy(item))
            stats["applied" if item.get("decision") != "NOT_APPLICABLE" else "not_applicable"] += 1
            if item.get("decision") == "YES":
                stats["yes"] += 1
            elif item.get("decision") == "NO":
                stats["no"] += 1
            if item.get("decision") != "NOT_APPLICABLE":
                if (item.get("origin") or "question") == "answer":
                    stats["answer_items"] += 1
                else:
                    stats["question_items"] += 1
                    stats["question_yes"] += item.get("decision") == "YES"
            stats["unverifiable"] += bool(item.get("unverifiable"))
        for sid, entry in not_run.items():
            if SUBS_BY_ID[sid].criterion == cid and sid not in subs:
                subs[sid] = {"sub_id": sid, "name": SUBS_BY_ID[sid].name, "decision": "NOT_APPLICABLE", "items": [],
                             "not_run": {"items": list(entry["items"]), "reason": entry.get("reason")}}
        for sid, sub in subs.items():
            if sub.get("decision") is None:
                sub["decision"] = sub_decision(sub["items"])
        ordered = {sid: subs[sid] for sid in SUBS_BY_ID if sid in subs}
        ordered.update({sid: sub for sid, sub in subs.items() if sid not in ordered})
        decision, status = criterion_decision(ordered)
        entry: Dict[str, Any] = {"criterion_id": cid, "name": _NAMES.get(cid, cid), "decision": decision, "confidence": None,
                                 "probabilities": {}, "reason": _summary_reason(items, decision)}
        if status:
            entry["status"] = status
        code = _reason_code(items)
        if code:
            entry["reason_code"] = code
        if cid == "J5":
            projected = legacy_projection(items, {sid: sub["decision"] for sid, sub in ordered.items()})
            entry["fact_check"] = projected.get("fact_check")
            if projected.get("reason_code"):
                entry["reason_code"] = projected["reason_code"]
            if projected.get("decision") is None:
                entry.update(decision=None, status="NOT_APPLICABLE")
        entry["subs"] = ordered
        decisions[cid] = entry
    if stats["question_items"]:
        stats["fulfillment"] = round(stats["question_yes"] / stats["question_items"], 4)
    if isinstance(applicability, dict) and applicability.get("version"):
        stats["applicability_version"] = applicability["version"]
    return decisions, stats


def fulfillment(records: List[Any]) -> Dict[str, Any]:
    """배치 충족률 — 질문 기준(origin='question') 적용 항목만 분모. {applied, yes, rate, by_criterion{cid: {applied, yes, rate}}, by_sub}."""
    total = {"applied": 0, "yes": 0}
    by_criterion: Dict[str, Dict[str, int]] = {}
    by_sub: Dict[str, Dict[str, int]] = {}
    for record in records:
        if not isinstance(record, dict) or record.get("pipeline") != "items":
            continue
        for cid, decision in (record.get("decisions") or {}).items():
            for sid, sub in ((decision or {}).get("subs") or {}).items():
                for item in sub.get("items") or []:
                    if item.get("decision") == "NOT_APPLICABLE" or (item.get("origin") or "question") == "answer":
                        continue
                    for bucket in (total, by_criterion.setdefault(cid, {"applied": 0, "yes": 0}),
                                   by_sub.setdefault(sid, {"applied": 0, "yes": 0})):
                        bucket["applied"] += 1
                        bucket["yes"] += item.get("decision") == "YES"

    def finish(bucket: Dict[str, Any]) -> Dict[str, Any]:
        return {**bucket, "rate": round(bucket["yes"] / bucket["applied"], 4) if bucket["applied"] else None}

    return {**finish(total), "by_criterion": {cid: finish(b) for cid, b in by_criterion.items()},
            "by_sub": {sid: finish(b) for sid, b in by_sub.items()}}
