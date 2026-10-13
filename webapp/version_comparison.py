"""AIONA 버전 비교 — 같은 질문 세트에 대한 두 실행(V1 → V2)의 품질 변화를 질문 → 기준 → 세부 기준 → 점검 항목·코드 조건까지 비교한다.

설계 '버전 비교': 비교 성립 조건(질문 세트 지문·코드 기준 버전·평가 방식·의미 기준 버전·평가 모델이 같아야 함. 호출 방식은 뺀다),
개선 = FAIL→PASS · 악화 = PASS→FAIL, 개선·악화 질문 수의 부호검정 p값(대화 단위), 가장 나쁜 질문들(꼬리)의 변화.
평균이 올라도 꼬리가 나빠지면 개선으로 보지 않는다. 판정 값은 저장된 결과를 그대로 읽고 어떤 설정도 소급하지 않는다.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Optional, Tuple

from .run_summary import question_outcomes, run_summary
from .version_registry import evaluation_profile

__all__ = ["COMPARISON_VERSION", "PROFILE_LABELS", "profile_differences", "sign_test", "compare_runs", "condition_changes"]

#: v2 — 전체 판정은 모든 문항이 연결·판정됐을 때만(부분 표본은 따로), 판정에 영향 있는 평가 설정은 모두 비교,
#: 펼친 항목은 '#n' 번호가 아니라 인용 원문으로 대응
COMPARISON_VERSION = "version-comparison-v2"
#: 비교 조건에 들어가는 평가 프로필 필드 (호출 방식·묶음 설정·API 주소는 판정에 영향이 없어 뺀다)
PROFILE_LABELS = {"engine_versions.engine": "코드 평가 엔진 버전", "engine_versions.checks": "코드 검사 버전", "aiona_config.config_hash": "AIONA 설정",
                  "pipeline": "의미 평가 방식", "semantic_version": "의미 평가 버전", "criteria_version": "의미 기준 버전",
                  "applicability_version": "적용 규칙 버전", "items_version": "점검 항목 버전", "quote_policy": "인용 검증 정책",
                  "recheck_policy": "재확인 정책", "history_policy": "대화 이력 정책", "decision_policy": "최종 판정 정책",
                  "judge.engine": "평가자 엔진", "judge.requested_model": "평가 모델", "judge.reasoning_effort": "추론 설정",
                  "judge.web_search": "웹 검색", "judge.max_tool_calls": "검색 횟수 한도", "judge.max_fetches": "원문 확인 한도",
                  "judge.fact_check_policy": "사실 검증 정책", "judge.context_check_policy": "대화 검증 정책", "judge.search_policy": "검색 정책",
                  "judge_models": "실제 평가 모델",
                  "judge.max_continuations": "검색 이어가기 한도", "judge.thinking": "추론 방식", "judge.output_mode": "출력 형식",
                  "judge.search_tool": "검색 도구 버전", "judge.fetch_tool": "원문 도구 버전", "judge.max_output_tokens": "출력 토큰 한도",
                  "judge.evidence_recovery_policy": "근거 보강 정책", "judge.evidence_recovery_rounds": "근거 보강 횟수",
                  "judge.evidence_recovery_timeout_s": "근거 보강 시간 한도", "judge.evidence_recovery_max_tool_calls": "근거 보강 검색 한도",
                  "judge.max_requests": "요청 수 한도", "judge.flags": "평가자 실행 옵션", "judge.disabled_features": "꺼 둔 평가자 기능",
                  "judge.temperature": "temperature", "judge.top_p": "top_p", "judge.seed": "seed", "judge.max_tokens": "출력 토큰 한도"}
#: 판정에 영향이 없는 호출 방식 설정만 뺀다 — 근거 보강·도구·출력 한도처럼 결과를 바꿀 수 있는 설정은 비교한다
_IGNORED_JUDGE = {"dispatch", "batch_questions", "batch_poll_seconds", "api_base"}
TAIL_SHARE = 0.1
TAIL_MIN = 5
SIGNIFICANCE = 0.05
CHANGE_LABELS = {"improved": "개선", "regressed": "악화", "pass_kept": "통과 유지", "fail_kept": "실패 유지", "pending": "미완료", "excluded": "제외"}


# --- 비교 성립 조건 -------------------------------------------------------------------------------------

def _flatten(fields: Dict[str, Any]) -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if key in ("judge", "engine_versions", "aiona_config") and isinstance(value, dict):
            for sub, inner in value.items():
                if key == "judge" and sub in _IGNORED_JUDGE:
                    continue
                if key == "aiona_config" and sub != "config_hash":
                    continue
                flat[f"{key}.{sub}"] = inner
        else:
            flat[key] = value
    defaults = {"pipeline": "legacy", "items_version": None, "quote_policy": None, "recheck_policy": None}
    for key, value in defaults.items():
        flat.setdefault(key, value)
    return flat


def profile_differences(baseline_profile: Optional[Dict[str, Any]], target_profile: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """두 평가 프로필의 차이 — [{field, label, baseline, target}] (호출 방식·묶음 설정은 비교하지 않는다)."""
    a = _flatten((baseline_profile or {}).get("fields") or {})
    b = _flatten((target_profile or {}).get("fields") or {})
    out = []
    for key in sorted(set(a) | set(b), key=lambda k: (list(PROFILE_LABELS).index(k) if k in PROFILE_LABELS else len(PROFILE_LABELS), k)):
        if a.get(key) != b.get(key):
            out.append({"field": key, "label": PROFILE_LABELS.get(key, key), "baseline": copy.deepcopy(a.get(key)), "target": copy.deepcopy(b.get(key))})
    return out


# --- 질문 대응 ---------------------------------------------------------------------------------------------

def _rows(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for index, source in enumerate((run.get("datasets") or {}).get("aiona") or []):
        if not isinstance(source, dict):
            rows.append({"index": index, "case_id": None})
            continue
        case_id = source.get("case_id") or source.get("id") or source.get("question_id")
        rows.append({"index": index, "case_id": str(case_id) if case_id not in (None, "") else None, "question": source.get("question"),
                     "reference": source.get("reference"), "conversation": source.get("conversation_id"), "turn": source.get("turn"),
                     "answer": source.get("answer")})
    return rows


def _match(baseline_run: Dict[str, Any], target_run: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """같은 문항 ID(양쪽에서 유일)이고 질문·참조·대화·턴이 같은 쌍만 — [{case_id, baseline_index, target_index, conversation}]."""
    a_rows, b_rows = _rows(baseline_run), _rows(target_run)

    def unique(rows):
        counts: Dict[str, int] = {}
        for row in rows:
            if row["case_id"]:
                counts[row["case_id"]] = counts.get(row["case_id"], 0) + 1
        return {row["case_id"]: row for row in rows if row["case_id"] and counts[row["case_id"]] == 1}
    a_by, b_by = unique(a_rows), unique(b_rows)
    pairs, unmatched = [], {"baseline": [], "target": []}
    for row in a_rows:
        other = b_by.get(row["case_id"]) if row["case_id"] in a_by else None
        if other is None:
            unmatched["baseline"].append({"index": row["index"], "case_id": row["case_id"],
                                          "reason": "대상 기록에 같은 문항 ID 가 없거나 ID 가 중복됩니다."})
            continue
        fields = [name for name, key in (("질문 원문", "question"), ("참조 데이터", "reference"), ("대화", "conversation"), ("턴", "turn"))
                  if row.get(key) != other.get(key)]
        if fields:
            reason = " · ".join(fields) + "이(가) 달라 별도 문항으로 취급합니다."
            unmatched["baseline"].append({"index": row["index"], "case_id": row["case_id"], "reason": reason})
            unmatched["target"].append({"index": other["index"], "case_id": other["case_id"], "reason": reason})
            continue
        pairs.append({"case_id": row["case_id"], "baseline_index": row["index"], "target_index": other["index"],
                      "conversation": row.get("conversation"), "turn": row.get("turn"), "question": row.get("question")})
    matched_targets = {p["target_index"] for p in pairs} | {u["index"] for u in unmatched["target"]}
    for row in b_rows:
        if row["index"] not in matched_targets:
            unmatched["target"].append({"index": row["index"], "case_id": row["case_id"], "reason": "기준 기록에 같은 문항 ID 가 없거나 ID 가 중복됩니다."})
    return pairs, unmatched


# --- 질문 아래 단위 비교 ------------------------------------------------------------------------------------

def _change(before: Optional[str], after: Optional[str]) -> str:
    if before in ("PASS", "FAIL") and after in ("PASS", "FAIL"):
        if before == "FAIL" and after == "PASS":
            return "improved"
        if before == "PASS" and after == "FAIL":
            return "regressed"
        return "pass_kept" if after == "PASS" else "fail_kept"
    if before in (None, "NA") and after in (None, "NA"):
        return "na"
    return "pending"


def _item_key(item: Dict[str, Any]) -> str:
    """항목 대응 키 — 펼친 요소(#n)는 모델이 붙인 번호 대신 인용 원문(질문, 답변에서 생긴 항목은 답변)과 턴으로 대응한다."""
    item_id = str(item.get("id"))
    base, _, element = item_id.partition("#")
    if not element:
        return item_id
    question = " ".join(str(item.get("question_quote") or "").split())
    answer = " ".join(str(item.get("answer_quote") or "").split())
    quote = (answer or question) if item.get("origin") == "answer" else (question or answer)   # J5-1.a 주장은 답변 인용으로
    if not quote:
        return item_id
    turn = item.get("turn")
    return f"{base} · {turn}턴 · {quote}" if turn is not None else f"{base} · {quote}"


def _semantic_units(record: Optional[Dict[str, Any]]) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, List[str]]]:
    """레코드 → (기준 판정, 세부 기준 판정, 항목 판정 묶음) — NOT_APPLICABLE 은 'NA', 판정 없음은 'PENDING'.

    항목은 대응 키마다 판정 목록이다. 같은 인용의 요소가 여럿이면 어느 것이 어느 것인지 알 수 없으므로 목록으로 두고,
    두 실행을 비교할 때(``_item_rows``) 같은 판정끼리 먼저 짝짓는다."""
    criteria: Dict[str, str] = {}
    subs: Dict[str, str] = {}
    grouped: Dict[str, List[str]] = {}
    record = record if isinstance(record, dict) else {}
    decisions = record.get("decisions") or {}
    for cid, decision in decisions.items():
        if not isinstance(decision, dict):
            continue
        verdict = decision.get("decision")
        criteria[cid] = verdict if verdict in ("PASS", "FAIL") else "NA" if decision.get("status") == "NOT_APPLICABLE" else "PENDING"
        for sid, sub in (decision.get("subs") or {}).items():
            if not isinstance(sub, dict):
                continue
            outcome = sub.get("decision")
            subs[sid] = outcome if outcome in ("PASS", "FAIL") else "NA" if outcome == "NOT_APPLICABLE" else "PENDING"
            for item in sub.get("items") or []:
                if not isinstance(item, dict):
                    continue
                grouped.setdefault(_item_key(item), []).append(
                    {"YES": "PASS", "NO": "FAIL", "NOT_APPLICABLE": "NA"}.get(item.get("decision"), "PENDING"))
    return criteria, subs, grouped


_DECISION_ORDER = {"FAIL": 0, "PASS": 1, "NA": 2, "PENDING": 3}


def _item_rows(a: Dict[str, List[str]], b: Dict[str, List[str]]) -> Dict[str, Dict[str, Any]]:
    """항목 판정 묶음 두 개 → 항목 비교 행. 같은 인용의 요소 묶음은 모델이 붙인 '#n' 과 무관하게 같은 판정끼리 먼저 짝짓고
    (번호만 바뀌면 변화 없음), 남은 판정을 FAIL·PASS·해당 없음·미완료 순으로 짝지어 개수가 바뀐 만큼만 개선·악화로 센다.
    묶음 행의 키는 '<키>  [n]' — 정규화한 인용에는 연속 공백이 없어 실제 인용 키와 겹치지 않는다."""
    rows: Dict[str, Dict[str, Any]] = {}
    for key in dict.fromkeys([*a, *b]):
        left, right = list(a.get(key) or []), list(b.get(key) or [])
        if len(left) <= 1 and len(right) <= 1:
            before, after = (left or [None])[0], (right or [None])[0]
            rows[key] = {"baseline": before, "target": after, "change": _change(before, after)}
            continue
        pairs: List[Tuple[Optional[str], Optional[str]]] = []
        for value in sorted(set(left) & set(right), key=lambda v: _DECISION_ORDER.get(v, 9)):
            for _ in range(min(left.count(value), right.count(value))):
                pairs.append((value, value))
                left.remove(value)
                right.remove(value)
        left.sort(key=lambda v: _DECISION_ORDER.get(v, 9))
        right.sort(key=lambda v: _DECISION_ORDER.get(v, 9))
        for n in range(max(len(left), len(right))):
            pairs.append((left[n] if n < len(left) else None, right[n] if n < len(right) else None))
        for n, (before, after) in enumerate(pairs, 1):
            rows[f"{key}  [{n}]"] = {"baseline": before, "target": after, "change": _change(before, after)}
    return rows


def _code_units(case: Optional[Dict[str, Any]], record: Optional[Dict[str, Any]]) -> Dict[str, str]:
    from code_eval.common_adjudication import resolved_case
    if not isinstance(case, dict) or case.get("case_status") != "EVALUATED":
        return {}
    try:
        case = resolved_case(case, record if isinstance(record, dict) else {})
    except (KeyError, TypeError, IndexError):
        pass
    out: Dict[str, str] = {}
    for row in case.get("results") or []:
        if row.get("area") != "common" or not row.get("counted"):
            continue
        key = f"{row.get('check_id') or row.get('subcategory')}:{row.get('condition_id') or ''}"
        out[key] = row.get("verdict") if row.get("verdict") in ("PASS", "FAIL") else "PENDING"
    return out


def _evidence(case: Optional[Dict[str, Any]], record: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """질문의 FAIL 근거 — CODE FAIL 행의 제목·사유, 의미 기준 FAIL 의 사유, items 의 '아니오' 항목 인용."""
    out: List[Dict[str, Any]] = []
    for row in (case or {}).get("results") or []:
        if row.get("area") == "common" and row.get("counted") and row.get("verdict") == "FAIL":
            out.append({"source": "CODE", "id": row.get("subcategory"), "title": row.get("title"),
                        "reason": (row.get("explain") or {}).get("headline") or row.get("reason") or ""})
    record = record if isinstance(record, dict) else {}
    for cid, decision in (record.get("decisions") or {}).items():
        if not isinstance(decision, dict) or decision.get("decision") != "FAIL":
            continue
        entry: Dict[str, Any] = {"source": "의미 평가", "id": cid, "title": decision.get("name"), "reason": decision.get("reason") or ""}
        quotes = []
        for sub in (decision.get("subs") or {}).values():
            for item in (sub or {}).get("items") or []:
                if isinstance(item, dict) and item.get("decision") == "NO":
                    quotes.append({"id": item.get("id"), "question_quote": item.get("question_quote"), "answer_quote": item.get("answer_quote"),
                                   "reason": item.get("reason"), "unverifiable": bool(item.get("unverifiable"))})
        if quotes:
            entry["items"] = quotes
        out.append(entry)
    return out


def _drilldown(a_case, a_record, b_case, b_record) -> Dict[str, Any]:
    a_c, a_s, a_i = _semantic_units(a_record)
    b_c, b_s, b_i = _semantic_units(b_record)
    a_code, b_code = _code_units(a_case, a_record), _code_units(b_case, b_record)

    def table(a: Dict[str, str], b: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
        keys = list(dict.fromkeys([*a, *b]))
        return {key: {"baseline": a.get(key), "target": b.get(key), "change": _change(a.get(key), b.get(key))} for key in keys}
    return {"criteria": table(a_c, b_c), "subs": table(a_s, b_s), "items": _item_rows(a_i, b_i), "code": table(a_code, b_code)}


# --- 통계 ------------------------------------------------------------------------------------------------------

def sign_test(plus: int, minus: int) -> Optional[float]:
    """양측 정확 부호검정 p값 (동점 제외). n=0 이면 None."""
    n = plus + minus
    if n <= 0:
        return None
    k = min(plus, minus)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return round(min(1.0, 2 * tail), 4)


def _units(pairs: List[Dict[str, Any]], a_out: List[Dict[str, Any]], b_out: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """대화 단위 — 같은 대화의 턴은 한 단위(통과 턴 수 비교), 단일 질문은 그 자체. 미완료가 섞인 단위는 동점 처리하지 않고 제외."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for pair in pairs:
        key = f"conv:{pair['conversation']}" if pair.get("conversation") not in (None, "") else f"q:{pair['case_id']}"
        groups.setdefault(key, []).append(pair)
    units = []
    for key, members in groups.items():
        outcomes_a = [a_out[p["baseline_index"]]["outcome"] for p in members]
        outcomes_b = [b_out[p["target_index"]]["outcome"] for p in members]
        complete = all(o in ("PASS", "FAIL") for o in outcomes_a + outcomes_b)
        score_a, score_b = outcomes_a.count("PASS"), outcomes_b.count("PASS")
        units.append({"unit": key, "size": len(members), "case_ids": [p["case_id"] for p in members], "complete": complete,
                      "baseline_pass": score_a, "target_pass": score_b,
                      "sign": None if not complete else "plus" if score_b > score_a else "minus" if score_b < score_a else "tie"})
    return units


def _badness(outcome: Dict[str, Any]) -> Tuple[int, int]:
    return (1 if outcome["outcome"] == "FAIL" else 0, len(outcome["code_fails"]) + len(outcome["semantic_fails"]))


# --- 비교 ------------------------------------------------------------------------------------------------------

def compare_runs(baseline_run: Dict[str, Any], target_run: Dict[str, Any]) -> Dict[str, Any]:
    """두 실행 기록(result() 모양) → 비교 결과 dict. 비교 조건이 맞지 않으면 eligible=False 와 차이 항목·'재평가로 맞추기' 안내를 준다."""
    a_batch = (baseline_run.get("evaluations") or {}).get("aiona") or {}
    b_batch = (target_run.get("evaluations") or {}).get("aiona") or {}
    a_total = baseline_run.get("question_count") or len((baseline_run.get("datasets") or {}).get("aiona") or [])
    b_total = target_run.get("question_count") or len((target_run.get("datasets") or {}).get("aiona") or [])
    a_out, b_out = question_outcomes(a_batch, a_total), question_outcomes(b_batch, b_total)
    a_profile, b_profile = evaluation_profile(baseline_run), evaluation_profile(target_run)
    differences = profile_differences(a_profile, b_profile)
    a_fp = baseline_run.get("dataset_fingerprint")
    b_fp = target_run.get("dataset_fingerprint")
    pairs, unmatched = _match(baseline_run, target_run)
    reasons: List[Dict[str, str]] = []
    if not baseline_run.get("run_id") or not target_run.get("run_id") or baseline_run.get("run_id") == target_run.get("run_id"):
        reasons.append({"code": "same_run", "label": "서로 다른 실행 기록을 선택해 주세요."})
    a_version = (baseline_run.get("aiona_release") or {}).get("version")
    b_version = (target_run.get("aiona_release") or {}).get("version")
    if not a_version or not b_version or a_version == b_version:
        reasons.append({"code": "same_or_unknown_version", "label": "서로 다른 AIONA 버전의 실행이어야 합니다."})
    if not (a_profile["complete"] and b_profile["complete"]):
        reasons.append({"code": "profile_unknown", "label": "평가 조건 기록이 불완전해 같은 조건인지 확인할 수 없습니다."})
    if differences:
        reasons.append({"code": "profile_mismatch", "label": "평가 조건이 다릅니다: " + " · ".join(d["label"] for d in differences)})
    same_set = bool(a_fp) and a_fp == b_fp
    if not same_set:
        reasons.append({"code": "question_set_mismatch", "label": "질문 세트 지문이 다릅니다. 동일하게 연결된 공통 문항만 비교합니다."})
    if not pairs:
        reasons.append({"code": "no_matched_questions", "label": "동일하게 연결된 문항이 없습니다."})
    if a_batch.get("live") or b_batch.get("live") or baseline_run.get("status") != "COMPLETED" or target_run.get("status") != "COMPLETED":
        reasons.append({"code": "incomplete_run", "label": "두 실행 모두 평가가 끝나야 합니다."})
    eligible = not reasons
    conditions = {"eligible": eligible, "reasons": reasons, "differences": differences, "same_question_set": same_set,
                  "suggestion": ("저장된 답변에 새 기준을 다시 돌려 조건을 맞추세요 (재평가로 맞추기: 기준 실행의 답변을 현재 설정으로 새 평가)."
                                 if differences else None)}
    # --- 질문 단위 ---
    rows = []
    counts = {key: 0 for key in CHANGE_LABELS}
    for pair in pairs:
        a, b = a_out[pair["baseline_index"]], b_out[pair["target_index"]]
        if a["outcome"] == "EXCLUDED" or b["outcome"] == "EXCLUDED":
            change = "excluded"
        else:
            change = _change(a["outcome"] if a["outcome"] in ("PASS", "FAIL") else None, b["outcome"] if b["outcome"] in ("PASS", "FAIL") else None)
            change = change if change in CHANGE_LABELS else "pending"
        counts[change] += 1
        rows.append({**pair, "baseline": a["outcome"], "target": b["outcome"], "change": change, "label": CHANGE_LABELS[change]})
    # --- 부호검정 (대화 단위) ---
    units = _units(pairs, a_out, b_out)
    plus = sum(1 for u in units if u["sign"] == "plus")
    minus = sum(1 for u in units if u["sign"] == "minus")
    ties = sum(1 for u in units if u["sign"] == "tie")
    incomplete_units = sum(1 for u in units if not u["complete"])
    p_value = sign_test(plus, minus)
    # --- 꼬리: 기준 실행에서 가장 나쁜 질문들 ---
    scored = [r for r in rows if r["change"] not in ("pending", "excluded")]
    size = min(len(scored), max(TAIL_MIN, math.ceil(len(scored) * TAIL_SHARE))) if scored else 0
    worst = sorted(scored, key=lambda r: _badness(a_out[r["baseline_index"]]), reverse=True)[:size]
    tail = {"size": size, "improved": sum(1 for r in worst if r["change"] == "improved"),
            "regressed": sum(1 for r in worst if r["change"] == "regressed"),
            "fail_kept": sum(1 for r in worst if r["change"] == "fail_kept"), "pass_kept": sum(1 for r in worst if r["change"] == "pass_kept"),
            "case_ids": [r["case_id"] for r in worst]}
    tail["worsened"] = tail["regressed"] > tail["improved"]
    # --- 전체 판정의 표본: 모든 문항이 연결되고 판정돼야 전체 버전의 개선·악화를 확정한다 ---
    coverage = {"matched": len(pairs), "unmatched_baseline": len(unmatched["baseline"]), "unmatched_target": len(unmatched["target"]),
                "pending": counts["pending"], "excluded": counts["excluded"], "incomplete_units": incomplete_units}
    coverage["complete"] = not (coverage["unmatched_baseline"] or coverage["unmatched_target"] or coverage["pending"]
                                or coverage["excluded"] or incomplete_units)
    # --- 판정 ---
    if not eligible:
        verdict = {"label": "not_comparable", "text": "비교 조건이 맞지 않아 개선·악화를 확정하지 않습니다."}
    elif not coverage["complete"]:
        verdict = {"label": "partial", "text": (f"연결되지 않거나 판정이 끝나지 않은 문항이 있어 전체 판정을 확정하지 않습니다 "
                   f"(미연결 {coverage['unmatched_baseline']}·{coverage['unmatched_target']}, 미완료 {coverage['pending']}, 제외 {coverage['excluded']}). "
                   f"판정이 끝난 공통 문항만: 개선 {plus}·악화 {minus} 단위, p={p_value if p_value is not None else '—'}")}
    elif p_value is not None and p_value < SIGNIFICANCE and plus > minus and not tail["worsened"]:
        verdict = {"label": "improved", "text": f"개선 — 개선 {plus}·악화 {minus} 단위, p={p_value} (꼬리 악화 없음)"}
    elif p_value is not None and p_value < SIGNIFICANCE and minus > plus:
        verdict = {"label": "regressed", "text": f"악화 — 개선 {plus}·악화 {minus} 단위, p={p_value}"}
    elif tail["worsened"] and plus > minus:
        verdict = {"label": "uncertain", "text": f"평균은 올랐지만 꼬리(가장 나쁜 {size}문항)가 나빠져 개선으로 보지 않습니다 (개선 {plus}·악화 {minus}, p={p_value})"}
    else:
        verdict = {"label": "uncertain", "text": f"우연과 구분되지 않음 — 개선 {plus}·악화 {minus} 단위, p={p_value if p_value is not None else '—'}"}
    # --- 악화·개선 질문 상세 ---
    a_cases, a_records = a_batch.get("cases") or [], (a_batch.get("jev_eval") or {}).get("records") or []
    b_cases, b_records = b_batch.get("cases") or [], (b_batch.get("jev_eval") or {}).get("records") or []
    a_sources, b_sources = (baseline_run.get("datasets") or {}).get("aiona") or [], (target_run.get("datasets") or {}).get("aiona") or []

    def detail(row: Dict[str, Any]) -> Dict[str, Any]:
        i, j = row["baseline_index"], row["target_index"]
        a_case = a_cases[i] if i < len(a_cases) else None
        b_case = b_cases[j] if j < len(b_cases) else None
        a_record = a_records[i] if i < len(a_records) else None
        b_record = b_records[j] if j < len(b_records) else None
        return {**row, "baseline_answer": (a_sources[i] or {}).get("answer") if i < len(a_sources) and isinstance(a_sources[i], dict) else None,
                "target_answer": (b_sources[j] or {}).get("answer") if j < len(b_sources) and isinstance(b_sources[j], dict) else None,
                "baseline_evidence": _evidence(a_case, a_record), "target_evidence": _evidence(b_case, b_record),
                "changes": _drilldown(a_case, a_record, b_case, b_record)}
    regressions = [detail(r) for r in rows if r["change"] == "regressed"]
    improvements = [detail(r) for r in rows if r["change"] == "improved"]
    # --- 기준별 변화 표 (연결된 문항만) ---
    criteria_table = _criteria_table(rows, a_cases, a_records, b_cases, b_records)
    return {"version": COMPARISON_VERSION, "baseline": run_summary(baseline_run), "target": run_summary(target_run),
            "conditions": conditions, "matched": len(pairs), "unmatched": unmatched, "counts": counts, "rows": rows,
            "sign_test": {"unit": "conversation", "plus": plus, "minus": minus, "ties": ties, "incomplete": incomplete_units,
                          "n": plus + minus, "p_value": p_value, "significance": SIGNIFICANCE},
            "tail": tail, "verdict": verdict, "coverage": coverage, "regressions": regressions, "improvements": improvements, "criteria": criteria_table}


def _criteria_table(rows, a_cases, a_records, b_cases, b_records) -> List[Dict[str, Any]]:
    """기준 → 세부 기준 → 항목(base id)·코드 조건의 개선·악화·유지 수 (연결된 문항 합산)."""
    buckets: Dict[str, Dict[str, Any]] = {}

    def bump(level: str, key: str, change: str, parent: Optional[str] = None) -> None:
        bucket = buckets.setdefault(f"{level}:{key}", {"level": level, "id": key, "parent": parent,
                                                      "improved": 0, "regressed": 0, "pass_kept": 0, "fail_kept": 0, "pending": 0, "na": 0})
        bucket[change if change in bucket else "pending"] += 1
    for row in rows:
        i, j = row["baseline_index"], row["target_index"]
        drill = _drilldown(a_cases[i] if i < len(a_cases) else None, a_records[i] if i < len(a_records) else None,
                           b_cases[j] if j < len(b_cases) else None, b_records[j] if j < len(b_records) else None)
        for cid, cell in drill["criteria"].items():
            bump("criterion", cid, cell["change"])
        for sid, cell in drill["subs"].items():
            bump("sub", sid, cell["change"], parent=sid.split("-")[0])
        for item_id, cell in drill["items"].items():
            base = item_id.split(" · ", 1)[0].split("#", 1)[0]   # 비교 키('J4-2.a · 인용')에서 기본 항목 ID 로 합산
            bump("item", base, cell["change"], parent=base.split(".")[0])
        for key, cell in drill["code"].items():
            bump("code", key, cell["change"], parent=key.split(".")[0])
    order = {"criterion": 0, "sub": 1, "item": 2, "code": 3}
    return sorted(buckets.values(), key=lambda b: (order[b["level"]], b["id"]))


# --- 조건 변경 이벤트 ----------------------------------------------------------------------------------------------

def condition_changes(previous: Optional[Dict[str, Any]], current: Dict[str, Any]) -> List[Dict[str, Any]]:
    """두 실행 요약(run_summary) 사이에 바뀐 요인 — [{event_type, label, before, after}] (기준·평가 모델·수집 모델).

    여러 요인이 함께 바뀌었으면 각 이벤트에 ``multi_factor=True`` 와 ``factors`` 를 붙여 원인을 단정하지 않는다.
    """
    if not isinstance(previous, dict):
        return []
    p, c = previous.get("profile") or {}, current.get("profile") or {}
    changes: List[Dict[str, Any]] = []
    criteria_keys = ("code_version", "semantic_version", "criteria_version", "applicability_version", "items_version", "pipeline")
    before = {k: p.get(k) for k in criteria_keys}
    after = {k: c.get(k) for k in criteria_keys}
    if before != after:
        changes.append({"event_type": "criteria_changed", "label": "기준 변경", "before": before, "after": after,
                        "fields": [k for k in criteria_keys if before[k] != after[k]]})
    judge_before = {**(p.get("judge") or {}), "judge_models": p.get("judge_models")}
    judge_after = {**(c.get("judge") or {}), "judge_models": c.get("judge_models")}
    if judge_before != judge_after:
        changes.append({"event_type": "judge_changed", "label": "평가 모델 변경", "before": judge_before, "after": judge_after,
                        "fields": [k for k in sorted(set(judge_before) | set(judge_after)) if judge_before.get(k) != judge_after.get(k)]})
    models_before = {k: v.get("model_id") for k, v in (previous.get("models") or {}).items() if k != "aiona"}
    models_after = {k: v.get("model_id") for k, v in (current.get("models") or {}).items() if k != "aiona"}
    shared = {k for k in models_before if k in models_after}
    if any(models_before[k] != models_after[k] for k in shared):
        changes.append({"event_type": "collection_models_changed", "label": "수집 모델 변경",
                        "before": {k: models_before[k] for k in shared}, "after": {k: models_after[k] for k in shared},
                        "fields": [k for k in sorted(shared) if models_before[k] != models_after[k]]})
    factors = [change["event_type"] for change in changes]
    for change in changes:
        change["multi_factor"] = len(factors) > 1
        change["factors"] = factors
    return changes
