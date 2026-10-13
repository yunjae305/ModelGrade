"""실행 요약 — 이력 관리용 한 실행의 조건·결과 요약 (설계 '이력 관리' 표의 '실행 요약').

날짜, 질문 세트 지문, 모델별 이름·모델 ID·AIONA 버전, 기준별·세부 기준별 통과율과 충족률, 평가 방식·호출 방식·평가 모델을
저장된 실행 기록에서만 읽는다(현재 설정을 소급하지 않는다). 판정 값은 바꾸지 않는다.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from code_eval.common_adjudication import resolved_case

from .version_registry import evaluation_profile, fingerprint

__all__ = ["RUN_SUMMARY_VERSION", "question_outcome", "question_outcomes", "run_summary"]

RUN_SUMMARY_VERSION = "run-summary-v1"
PROVIDER_NAMES = {"aiona": "AIONA", "gpt": "GPT", "claude": "Claude", "gemini": "Gemini"}


def question_outcome(case: Optional[Dict[str, Any]], record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """질문 하나의 최종 판정 — {"outcome": PASS|FAIL|PENDING|EXCLUDED, "code_fails": [check_id…], "semantic_fails": [cid…],
    "code_complete", "semantic_complete"}. 공통 CODE FAIL 이나 의미 기준 FAIL 이 하나라도 확정되면 FAIL, 모두 판정되고 FAIL 이 없으면 PASS."""
    if not isinstance(case, dict) or case.get("case_status") != "EVALUATED":
        return {"outcome": "EXCLUDED", "code_fails": [], "semantic_fails": [], "code_complete": False, "semantic_complete": False}
    record = record if isinstance(record, dict) else {}
    try:
        case = resolved_case(case, record)   # 옛 결과의 code_completion 투영 (새 결과는 그대로)
    except (KeyError, TypeError, IndexError):
        pass
    common = (case.get("summary") or {}).get("common") or {}
    code_fails = [row.get("check_id") or row.get("condition_id") for row in case.get("results") or []
                  if row.get("area") == "common" and row.get("counted") and row.get("verdict") == "FAIL"]
    code_complete = bool(common.get("unjudged") == 0 and not case.get("errors") and (common.get("pass", 0) + common.get("fail", 0)) > 0)
    decisions = record.get("decisions") or {}
    criteria = record.get("criteria") if isinstance(record.get("criteria"), list) else list(decisions)
    semantic_fails = [cid for cid in criteria if (decisions.get(cid) or {}).get("decision") == "FAIL"]
    semantic_complete = (record.get("status") == "SUCCESS" and bool(criteria)
                         and all((decisions.get(cid) or {}).get("decision") in ("PASS", "FAIL") for cid in criteria))
    if code_fails or semantic_fails or common.get("fail", 0) > 0:
        outcome = "FAIL"
    elif code_complete and semantic_complete:
        outcome = "PASS"
    else:
        outcome = "PENDING"
    return {"outcome": outcome, "code_fails": [c for c in code_fails if c], "semantic_fails": semantic_fails,
            "code_complete": code_complete, "semantic_complete": semantic_complete}


def question_outcomes(batch: Optional[Dict[str, Any]], total: Optional[int] = None) -> List[Dict[str, Any]]:
    """배치(provider 하나)의 질문별 최종 판정 목록 (index 순). 결과가 없는 index 는 PENDING."""
    batch = batch if isinstance(batch, dict) else {}
    cases = batch.get("cases") or []
    records = (batch.get("jev_eval") or {}).get("records") or []
    count = total if isinstance(total, int) and total > 0 else len(cases)
    out = []
    for index in range(count):
        case = cases[index] if index < len(cases) else None
        record = records[index] if index < len(records) else None
        if case is None:
            out.append({"outcome": "PENDING", "code_fails": [], "semantic_fails": [], "code_complete": False, "semantic_complete": False})
        else:
            out.append(question_outcome(case, record))
    return out


def _rate(passed: int, failed: int) -> Optional[float]:
    return round(passed / (passed + failed), 4) if passed + failed else None


def _criteria_rates(batch: Dict[str, Any]) -> Dict[str, Any]:
    """기준별·세부 기준별 통과율(저장된 jev_eval.summary)과 충족률, CODE 대분류별 통과율."""
    jev = batch.get("jev_eval") or {}
    criteria: Dict[str, Any] = {}
    for cid, part in (jev.get("summary") or {}).items():
        if not isinstance(part, dict):
            continue
        entry = {"name": part.get("name"), "targets": part.get("targets", 0), "pass": part.get("pass", 0), "fail": part.get("fail", 0),
                 "rate": _rate(part.get("pass", 0), part.get("fail", 0))}
        if isinstance(part.get("subs"), dict):
            entry["subs"] = {sid: {"name": sub.get("name"), "targets": sub.get("targets", 0), "pass": sub.get("pass", 0), "fail": sub.get("fail", 0),
                                   "rate": _rate(sub.get("pass", 0), sub.get("fail", 0))} for sid, sub in part["subs"].items() if isinstance(sub, dict)}
        if isinstance(part.get("fulfillment"), dict):
            entry["fulfillment"] = part["fulfillment"].get("rate")
        criteria[cid] = entry
    code: Dict[str, Dict[str, int]] = {}
    for case in batch.get("cases") or []:
        if not isinstance(case, dict) or case.get("case_status") != "EVALUATED":
            continue
        for row in case.get("results") or []:
            if row.get("area") != "common" or not row.get("counted") or row.get("verdict") not in ("PASS", "FAIL"):
                continue
            bucket = code.setdefault(str(row.get("category") or ""), {"pass": 0, "fail": 0})
            bucket["pass" if row["verdict"] == "PASS" else "fail"] += 1
    return {"criteria": criteria, "code": {cat: {**b, "rate": _rate(b["pass"], b["fail"])} for cat, b in code.items()},
            "fulfillment": copy.deepcopy((jev.get("fulfillment") or {}).get("rate")) if isinstance(jev.get("fulfillment"), dict) else None}


def run_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    """저장된 실행 기록 → 이력 요약 (judge 는 evaluation_profile 의 허용 키만, 인증값 없음)."""
    profile = evaluation_profile(run)
    fields = profile["fields"]
    judge = fields.get("judge") or {}
    release = run.get("aiona_release") or {}
    execution = run.get("evaluation_execution") or {}
    datasets = run.get("datasets") or {}
    models: Dict[str, Any] = {}
    for provider in run.get("providers") or list(datasets):
        info = (run.get("models") or {}).get(provider) if isinstance(run.get("models"), dict) else None
        model_id = info if isinstance(info, str) else (info or {}).get("model") if isinstance(info, dict) else None
        models[provider] = {"name": PROVIDER_NAMES.get(provider, provider),
                            "model_id": None if provider == "aiona" else model_id,   # AIONA 는 업로드 원문 — 모델 ID 가 아니라 버전으로 구분
                            "aiona_version": release.get("version") if provider == "aiona" else None}
    results: Dict[str, Any] = {}
    total = run.get("question_count") or len(datasets.get("aiona") or [])
    for provider, batch in (run.get("evaluations") or {}).items():
        if not isinstance(batch, dict):
            continue
        outcomes = question_outcomes(batch, total)
        counts = {key: sum(1 for o in outcomes if o["outcome"] == key) for key in ("PASS", "FAIL", "PENDING", "EXCLUDED")}
        results[provider] = {"questions": counts, "question_pass_rate": _rate(counts["PASS"], counts["FAIL"]), **_criteria_rates(batch)}
    return {"version": RUN_SUMMARY_VERSION, "run_id": run.get("run_id"), "name": run.get("name"), "status": run.get("status"),
            "created_at": run.get("created_at"), "finished_at": run.get("finished_at"),
            "question_set": {"fingerprint": run.get("dataset_fingerprint") or fingerprint(datasets.get("aiona") or []), "count": total},
            "aiona_release": copy.deepcopy(release) or None, "models": models,
            "profile": {"complete": profile["complete"], "pipeline": fields.get("pipeline"),
                        "dispatch": execution.get("dispatch") or judge.get("dispatch") or "realtime",
                        "code_version": (fields.get("engine_versions") or {}).get("engine"),
                        "semantic_version": fields.get("semantic_version"), "criteria_version": fields.get("criteria_version"),
                        "applicability_version": fields.get("applicability_version"), "items_version": fields.get("items_version"),
                        "judge": {k: judge.get(k) for k in ("engine", "requested_model", "reasoning_effort", "web_search") if k in judge},
                        "judge_models": list(fields.get("judge_models") or [])},
            "results": results}
