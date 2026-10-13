"""공통 묶음 → 기존 비교 실행 구조(run.datasets·providers·models).

시스템마다 같은 질문 순서의 행 목록을 만들어 기존 평가 경로(ComparisonService.evaluate)와 결과 화면이 그대로
읽게 한다. 답변이 없는 칸은 NOT_COLLECTED 행으로 채워 분모를 유지하고 품질 FAIL 로 세지 않는다.
실행 환경 차이는 비교 결과에 함께 남긴다 — 환경이 다르면 같은 조건의 성능 순위로 보지 않는다.
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Dict, List, Optional

from .schema import ACCESS_MODES, COLLECTION_METHODS
from .sources import evaluation_sources

#: 같은 날로 볼 수 있는 수집 시각 차이 (초). 넘으면 '수집 시점이 다름'으로 표시한다.
SAME_PERIOD_SECONDS = 24 * 3600
_ROW_COLLECTION_FIELDS = ("finish_reason", "platform_ref", "original", "normalization", "agent", "retrieved_sources", "raw",
                          "empty_answer_confirmed")


def dataset_row(question: Dict[str, Any], system: Dict[str, Any], response: Optional[Dict[str, Any]], index: int) -> Dict[str, Any]:
    row = {"case_id": question["case_id"], "question": question["question"]}
    if question.get("reference") is not None:
        row["reference"] = copy.deepcopy(question["reference"])
    if question.get("conversation_id") is not None:
        row.update(conversation_id=question["conversation_id"], turn=question["turn"])
    if response is None:
        response = {"answer": "", "status": "NOT_COLLECTED", "error": "이 시스템의 답변이 없습니다.", "collected_at": None,
                    "collection_method": None, "answer_format": "markdown", "environment": {}, "usage": {}, "sources": [],
                    "collection_quality": [], "response_key": None}
    collection = {"status": response["status"], "error": response.get("error"), "collected_at": response.get("collected_at"),
                  "collection_method": response.get("collection_method"),
                  "collection_method_label": COLLECTION_METHODS.get(response.get("collection_method")),
                  "access_mode": system["access_mode"], "answer_format": response.get("answer_format"),
                  "environment": copy.deepcopy(response.get("environment") or {}),
                  "usage": copy.deepcopy(response.get("usage") or {}), "sources": copy.deepcopy(response.get("sources") or []),
                  "quality": copy.deepcopy(response.get("collection_quality") or []), "response_key": response.get("response_key")}
    for key in _ROW_COLLECTION_FIELDS:
        if key in response:
            collection[key] = copy.deepcopy(response[key])
    row.update(answer=response.get("answer", ""), system_id=system["system_key"], source=system["label"], case_index=index,
               collection=collection)
    sources = evaluation_sources(response.get("sources") or [])
    if sources:
        row["answer_sources"] = sources
    if response["status"] != "SUCCESS":
        row["__parse_error__"] = "응답 수집 실패·중단 또는 미완료: " + (response.get("error") or "평가 제외")
    return row


def _time(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def system_summary(system: Dict[str, Any], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    collected = [r["collection"] for r in rows]
    times = sorted(t for t in (_time(c.get("collected_at")) for c in collected if c.get("status") != "NOT_COLLECTED") if t)
    statuses: Dict[str, int] = {}
    searched = {"true": 0, "false": 0, "unknown": 0}
    tools, methods, severities = [], [], {"info": 0, "warning": 0, "error": 0}
    tokens = {"input_tokens": 0, "output_tokens": 0}
    counted = {"input_tokens": False, "output_tokens": False}
    costs: Dict[str, float] = {}
    for c in collected:
        statuses[c["status"]] = statuses.get(c["status"], 0) + 1
        if c["status"] == "NOT_COLLECTED" and not c.get("collection_method"):
            continue
        used = (c.get("environment") or {}).get("web_search_used")
        searched["true" if used is True else "false" if used is False else "unknown"] += 1
        tools.extend((c.get("environment") or {}).get("tools_used") or [])
        if c.get("collection_method") and c["collection_method"] not in methods:
            methods.append(c["collection_method"])
        for q in c.get("quality") or []:
            severities[q["severity"]] = severities.get(q["severity"], 0) + 1
        for key in tokens:
            value = (c.get("usage") or {}).get(key)
            if isinstance(value, (int, float)):
                tokens[key] += value
                counted[key] = True
        cost = (c.get("usage") or {}).get("reported_cost")
        if isinstance(cost, dict):
            costs[cost["currency"]] = costs.get(cost["currency"], 0) + cost["amount"]
    return {**{k: copy.deepcopy(system[k]) for k in ("system_key", "label", "provider", "platform", "model", "model_version",
                                                       "access_mode", "settings", "environment")},
            "access_label": ACCESS_MODES[system["access_mode"]],
            "collection": {"methods": methods, "method_labels": [COLLECTION_METHODS[m] for m in methods],
                           "collected_from": times[0].isoformat() if times else None,
                           "collected_to": times[-1].isoformat() if times else None,
                           "collected_time_unknown": sum(1 for c in collected if c["status"] != "NOT_COLLECTED"
                                                         and not _time(c.get("collected_at"))),
                           "statuses": statuses, "web_search_used": searched, "tools_used": sorted(set(tools)),
                           "quality": severities,
                           "generation_usage": {k: (tokens[k] if counted[k] else None) for k in tokens},
                           "generation_reported_cost": costs or None}}


def environment_comparison(summaries: Dict[str, Dict[str, Any]], questions: List[Dict[str, Any]],
                           gaps: Dict[str, List[int]]) -> Dict[str, Any]:
    """비교 공정성 — 같은 질문·같은 대화 맥락·수집 시점·웹검색·도구·생성 경로가 같은지. 모르면 같다고 보지 않는다."""
    items = list(summaries.values())
    differences = []

    def differ(code: str, label: str, values: Dict[str, Any], unknown: bool) -> None:
        distinct = {repr(v) for v in values.values()}
        if len(distinct) > 1 or unknown:
            differences.append({"code": code, "label": label, "values": values,
                                "unknown": unknown, "detail": "확인되지 않은 값이 있음" if unknown and len(distinct) <= 1 else "시스템마다 다름"})

    if len(items) > 1:
        differ("access_mode", "응답 생성 경로(API·웹·에이전트·외부 플랫폼)",
               {s["system_key"]: s["access_label"] for s in items}, False)
        differ("web_search_available", "웹검색 허용 여부",
               {s["system_key"]: s["environment"]["web_search_available"] for s in items},
               any(s["environment"]["web_search_available"] is None for s in items))
        differ("tools_available", "사용 가능한 도구",
               {s["system_key"]: s["environment"]["tools_available"] for s in items},
               any(s["environment"]["tools_available"] is None for s in items))
        differ("system_instruction", "시스템 지시",
               {s["system_key"]: s["environment"]["system_instruction"].get("text") if s["environment"]["system_instruction"]["known"] else None
                for s in items}, any(not s["environment"]["system_instruction"]["known"] for s in items))
        used = {s["system_key"]: ("사용" if s["collection"]["web_search_used"]["true"] else
                                  "미사용" if s["collection"]["web_search_used"]["false"] and not s["collection"]["web_search_used"]["unknown"] else "확인 안 됨")
                for s in items}
        differ("web_search_used", "실제 웹검색 사용", used, any(v == "확인 안 됨" for v in used.values()))
        starts = [_time(s["collection"]["collected_from"]) for s in items]
        ends = [_time(s["collection"]["collected_to"]) for s in items]
        unknown_time = any(t is None for t in starts) or any(s["collection"]["collected_time_unknown"] for s in items)
        spread = (max(e for e in ends if e) - min(t for t in starts if t)).total_seconds() if any(starts) and any(ends) else None
        periods = {s["system_key"]: (s["collection"]["collected_from"], s["collection"]["collected_to"]) for s in items}
        if unknown_time or (spread is not None and spread > SAME_PERIOD_SECONDS):
            differences.append({"code": "collection_time", "label": "수집 시점", "values": periods, "unknown": unknown_time,
                                "detail": "수집 시각을 모르는 답변이 있음" if unknown_time else "수집 시점이 하루 이상 차이 남"})
        coverage = {s["system_key"]: s["collection"]["statuses"].get("SUCCESS", 0) for s in items}
        differ("question_coverage", "답변한 질문 수", coverage, False)
    if gaps:
        differences.append({"code": "conversation_gaps", "label": "대화 맥락", "values": copy.deepcopy(gaps), "unknown": False,
                            "detail": "앞 턴이 빠진 대화가 있어 해당 턴은 전체 대화 맥락으로 평가하지 않음"})
    same = None if len(items) < 2 else not any(d["code"] != "conversation_gaps" for d in differences)
    return {"same_conditions": same, "systems": len(items), "differences": differences,
            "same_questions": True, "question_count": len(questions),
            "note": ("시스템이 하나라 환경 비교 대상이 없습니다." if same is None else
                     "모든 확인 항목이 같습니다." if same else
                     "실행 환경이 다르거나 확인되지 않은 항목이 있어, 점수는 같은 조건의 성능 순위가 아닙니다.")}


def build_run_data(bundle: Dict[str, Any]) -> Dict[str, Any]:
    questions = bundle["questions"]
    slots = {(r["case_id"], r["system_key"]): r for r in bundle["responses"]}
    datasets, summaries = {}, {}
    for system in bundle["systems"]:
        rows = [dataset_row(q, system, slots.get((q["case_id"], system["system_key"])), i) for i, q in enumerate(questions)]
        datasets[system["system_key"]] = rows
        summaries[system["system_key"]] = system_summary(system, rows)
    return {"providers": [s["system_key"] for s in bundle["systems"]], "datasets": datasets,
            "models": {s["system_key"]: s["label"] for s in bundle["systems"]}, "systems": summaries,
            "environment_comparison": environment_comparison(summaries, questions, bundle["report"]["conversation_gaps"]),
            "questions": [{k: q[k] for k in ("case_id", "question_key", "question_type", "scenario", "dialogue", "dataset_version",
                                             "conversation_id", "turn") if q.get(k) is not None} | (
                {"aliases": q["aliases"]} if q.get("aliases") else {}) for q in questions]}
