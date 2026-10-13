"""이력 관리 — 평가가 끝난 결과 비교마다 AIONA 결과를 한 줄씩 쌓고, 기준·평가 모델·AIONA 버전이 바뀔 때 결과가 어떻게 달라졌는지 추적한다.

설계안 v1.1 '이력 관리'를 AIONA 하나에 적용한다 (사용자 결정 10-13 — 수집 모델 GPT·Claude·Gemini 는 다루지 않는다).
버전 관리(``version_comparison``)는 AIONA 버전끼리 같은 질문으로 짝지어 비교하고, 이력 관리는 시간 흐름이다. 시스템 로그(작업 이력)와는 다르다.

- 한 줄(실행 요약) = 평가가 끝난 실행 하나의 AIONA 결과: 날짜, 질문 세트 지문, AIONA 버전, 기준별·세부 기준별 통과율과 충족률,
  평가 방식·호출 방식, 기준·평가 모델 조건.
- 비교 상대: 재평가 실행(같은 답변에 새 기준만 돌린 것)은 원본 실행, 그 밖에는 같은 질문 세트의 직전 기록. 질문 세트가 다르면
  통과율을 비교하지 않는다.
- 바뀐 요인: 기준 변경·평가 모델 변경(``version_comparison.condition_changes``)과 AIONA 버전 변경. 한 요인만 바뀐 비교만 그 요인의
  효과로 읽을 수 있고(``attributable``), 여러 요인이 함께 바뀌면 '여러 요인 변경'으로 표시하고 원인을 단정하지 않는다.
- 기준 변경에는 ``criteria_registry.CHANGELOG`` 에서 새 버전의 변경 항목·이유를 붙인다. 판정 값은 저장된 결과를 그대로 읽는다.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from code_eval.criteria_registry import CHANGELOG

from .run_summary import run_summary
from .version_comparison import condition_changes

__all__ = ["HISTORY_VERSION", "aiona_history"]

HISTORY_VERSION = "aiona-history-v1"
FACTOR_LABELS = {"criteria_changed": "기준 변경", "judge_changed": "평가 모델 변경", "aiona_version_changed": "AIONA 버전 변경"}


def _delta(before: Optional[float], after: Optional[float]) -> Optional[float]:
    return round(after - before, 4) if isinstance(before, (int, float)) and isinstance(after, (int, float)) else None


def _entry(run: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if run.get("status") != "COMPLETED" or not isinstance((run.get("evaluations") or {}).get("aiona"), dict):
        return None
    summary = run_summary(run)
    aiona = summary["results"].get("aiona")
    if not aiona:
        return None
    profile, questions = summary["profile"], aiona["questions"]
    criteria = {cid: {"name": part.get("name"), "rate": part.get("rate"), "pass": part.get("pass"), "fail": part.get("fail"),
                      "fulfillment": part.get("fulfillment"),
                      "subs": {sid: {"name": sub.get("name"), "rate": sub.get("rate"), "pass": sub.get("pass"), "fail": sub.get("fail")}
                               for sid, sub in (part.get("subs") or {}).items()}}
                for cid, part in (aiona.get("criteria") or {}).items()}
    return {
        "run_id": run.get("run_id"), "name": run.get("name"),
        "created_at": run.get("created_at"), "finished_at": run.get("finished_at"),
        "source_run_id": run.get("source_run_id"),
        "aiona_version": (summary.get("aiona_release") or {}).get("version"),
        "question_set": copy.deepcopy(summary["question_set"]),
        "conditions": {key: copy.deepcopy(profile.get(key)) for key in
                       ("code_version", "criteria_version", "semantic_version", "applicability_version", "items_version",
                        "pipeline", "dispatch", "judge", "judge_models")},
        "conditions_complete": bool(profile.get("complete")),
        "results": {"questions": dict(questions), "question_pass_rate": aiona["question_pass_rate"],
                    # 미완료·제외 질문이 있으면 통과율은 판정된 질문만의 값이라 변화 비교에 그대로 쓰지 않는다
                    "complete": not questions.get("PENDING") and not questions.get("EXCLUDED"),
                    "criteria": criteria, "fulfillment": aiona.get("fulfillment"), "code": copy.deepcopy(aiona.get("code") or {})},
        "_summary": summary,
    }


def _changelog(change: Dict[str, Any]) -> List[Dict[str, Any]]:
    """기준 변경의 새 버전에 해당하는 CHANGELOG 항목 (추가·삭제·문구 변경된 항목과 이유)."""
    after = change.get("after") or {}
    wanted = {value for key, value in after.items() if key != "code_version" and isinstance(value, str)}
    code = after.get("code_version")
    if isinstance(code, str) and code:
        wanted.add("engine " + code.rsplit("-", 1)[-1])
    before = change.get("before") or {}
    unchanged = {value for key, value in before.items() if isinstance(value, str) and after.get(key) == value}
    return [copy.deepcopy(entry) for entry in CHANGELOG if entry.get("version") in wanted - unchanged]


def _changes(previous: Dict[str, Any], current: Dict[str, Any]) -> List[Dict[str, Any]]:
    changes = [change for change in condition_changes(previous["_summary"], current["_summary"])
               if change["event_type"] != "collection_models_changed"]   # 이력 관리는 AIONA 만 — 수집 모델은 다루지 않는다
    if previous["aiona_version"] != current["aiona_version"]:
        changes.append({"event_type": "aiona_version_changed", "label": FACTOR_LABELS["aiona_version_changed"],
                        "before": previous["aiona_version"], "after": current["aiona_version"], "fields": ["aiona_version"]})
    factors = [change["event_type"] for change in changes]
    for change in changes:
        change["multi_factor"] = len(factors) > 1
        change["factors"] = factors
        if change["event_type"] == "criteria_changed":
            change["changelog"] = _changelog(change)
    return changes


def _deltas(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    criteria = {}
    for cid in dict.fromkeys([*before["criteria"], *after["criteria"]]):
        a, b = before["criteria"].get(cid) or {}, after["criteria"].get(cid) or {}
        criteria[cid] = {"rate": _delta(a.get("rate"), b.get("rate")), "fulfillment": _delta(a.get("fulfillment"), b.get("fulfillment")),
                         "subs": {sid: _delta(((a.get("subs") or {}).get(sid) or {}).get("rate"), ((b.get("subs") or {}).get(sid) or {}).get("rate"))
                                  for sid in dict.fromkeys([*(a.get("subs") or {}), *(b.get("subs") or {})])}}
    return {"question_pass_rate": _delta(before["question_pass_rate"], after["question_pass_rate"]), "criteria": criteria}


def aiona_history(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """저장된 실행 기록(result() 모양) 목록 → {version, entries(최신순), series(질문 세트별 추이, 오래된 순), changelog}."""
    entries = [entry for entry in (_entry(run) for run in runs if isinstance(run, dict)) if entry]
    entries.sort(key=lambda e: (e["finished_at"] or e["created_at"] or "", e["run_id"] or ""))
    by_id = {entry["run_id"]: entry for entry in entries}
    latest_by_set: Dict[str, Dict[str, Any]] = {}
    series: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        fingerprint = entry["question_set"].get("fingerprint") or ""
        source = by_id.get(entry["source_run_id"]) if entry["source_run_id"] else None
        reevaluation = bool(source) and (source["question_set"].get("fingerprint") or "") == fingerprint
        previous = source if reevaluation else latest_by_set.get(fingerprint)
        entry["previous_run_id"] = previous["run_id"] if previous else None
        entry["relation"] = "reevaluation" if reevaluation else "previous" if previous else None
        if previous:
            changes = _changes(previous, entry)
            factors = [change["event_type"] for change in changes]
            entry["changes"] = changes
            entry["factors"] = factors
            entry["multi_factor"] = len(factors) > 1
            # 설계: 한 번에 한 요인만 바뀐 실행끼리 비교 — 그때만 변화를 그 요인의 효과로 읽는다 (둘 다 모든 질문이 판정됐을 때)
            entry["attributable"] = len(factors) == 1 and previous["results"]["complete"] and entry["results"]["complete"]
            entry["deltas"] = _deltas(previous["results"], entry["results"])
        else:
            entry.update(changes=[], factors=[], multi_factor=False, attributable=False, deltas=None)
        latest_by_set[fingerprint] = entry
        group = series.setdefault(fingerprint, {"fingerprint": fingerprint or None, "count": entry["question_set"].get("count"), "points": []})
        group["points"].append({"run_id": entry["run_id"], "at": entry["finished_at"] or entry["created_at"],
                                "aiona_version": entry["aiona_version"], "question_pass_rate": entry["results"]["question_pass_rate"],
                                "criteria": {cid: part["rate"] for cid, part in entry["results"]["criteria"].items()},
                                "complete": entry["results"]["complete"], "markers": list(entry["factors"])})
    for entry in entries:
        entry.pop("_summary", None)
    return {"version": HISTORY_VERSION, "factor_labels": dict(FACTOR_LABELS), "entries": list(reversed(entries)),
            "series": list(series.values())}
