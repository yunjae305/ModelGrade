"""v16 코드 평가 엔진 — 모든 질문·답변을 같은 검사 경로에 넣는다 (질문 유형 분류·배분 없음).

    질문 + 답변 → 입력 검증 → 구조 분석(공유) → 공통 검사 + 특화 검사 → 판정·측정값·근거 → 집계

CLI·웹은 이 모듈만 호출한다. 판정 코드를 복제하지 않는다.
"""

from __future__ import annotations

import json
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from .checks_aiona import check_aiona
from .checks_consistency import check_3_1, check_3_2, check_3_3
from .checks_integrity import check_1_1, check_1_2, check_1_3, foreign_script_observation
from .checks_requirements import check_2_1, check_2_2, table_observation
from .config import AionaConfig, load_aiona_config
from .explain import explain_result
from .io import validate_json_numbers
from .core import LIMITS, Ctx, nfc, result, versions
from .extract import persistent_conditions
from .model import CATEGORY_NAMES, FAIL, PASS, SPECIALIZED, SUBCATEGORIES, ConditionResult, Status, stable_hash

__all__ = ["evaluate_case", "evaluate_batch", "summarize_batch", "result_digest"]

#: 실행 순서. 2-2·3-3 은 1-2 의 JSON 파싱 결과를 공유하므로 1-2 뒤에 둔다.
_COMMON = (
    ("1-1", check_1_1), ("1-2", check_1_2), ("1-3", check_1_3),
    ("2-1", check_2_1), ("2-2", check_2_2),
    ("3-1", check_3_1), ("3-2", check_3_2), ("3-3", check_3_3),
)

_UNJUDGED = {Status.CONFIG_MISSING, Status.DISPLAY_MISSING, Status.INCOMPLETE, Status.UNSUPPORTED,
             Status.DEPENDENCY_FAILED, Status.CHECK_ERROR, Status.LIMIT_EXCEEDED}
_QUESTION_SOURCES = {"question"}


def _input_error(case_id: str, message: str, conversation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # 대화 정보가 읽히면 함께 남긴다 — 의미 평가 단계가 그 대화의 빈 턴(입력 오류)을 알 수 있게
    return {"case_id": case_id, "case_status": "INPUT_ERROR", "input_error": message,
            "results": [], "observations": [], "summary": None, "meta": {},
            **({"conversation": conversation} if conversation else {})}


def _conversation_of(payload: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """대화 입력 필드 ``conversation_id``·``turn``. turn 상한은 전체 앞 턴을 함께 받을 수 있는 배치 한도다."""
    cid, turn = payload.get("conversation_id"), payload.get("turn")
    if cid is None and turn is None:
        return None, None
    if cid is None or turn is None:
        return None, "conversation_id 와 turn(1 이상 정수)은 함께 있어야 함"
    if isinstance(cid, bool) or not isinstance(cid, (str, int)) or not str(cid).strip():
        return None, "conversation_id 는 비어 있지 않은 문자열이어야 함"
    max_turn = LIMITS["batch_cases"]
    turn_error = f"turn 은 1 이상 {max_turn:,} 이하의 정수여야 함 (한 번의 대화 입력 한도)"
    if isinstance(turn, str):
        if not turn.strip().isdecimal():
            return None, turn_error
        try:
            turn = int(turn.strip())
        except ValueError:  # 매우 긴 숫자 문자열도 배치 전체가 아니라 이 항목의 입력 오류다.
            return None, turn_error
    if isinstance(turn, bool) or not isinstance(turn, int) or not 1 <= turn <= max_turn:
        return None, turn_error
    return {"id": str(cid).strip(), "turn": turn}, None


def _reference_of(payload: Dict[str, Any]) -> tuple[Any, Optional[str]]:
    """참조 데이터 ``reference`` — 문자열 또는 JSON 객체·배열. 없거나 비어 있으면 (None, None)."""
    ref = payload.get("reference")
    if ref is None or ref == "" or ref == {} or ref == []:
        return None, None
    if isinstance(ref, (str, dict, list)):
        return ref, None
    return None, f"reference 는 문자열 또는 JSON 객체·배열이어야 함 ({type(ref).__name__})"


def _normalize_payload(payload: Any, index: int) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"입력 {index}: 객체가 아님 ({type(payload).__name__})")
    return payload


def _run_group(sub: str, fn: Callable, ctx: Ctx, *args) -> List[ConditionResult]:
    try:
        return list(fn(ctx, *args))
    except RecursionError:
        return [result(f"{sub}.check_error", sub, "검사 실행", "engine", Status.LIMIT_EXCEEDED,
                       reason="분석 깊이 한도 초과 — 품질 FAIL 이 아님")]
    except Exception as exc:  # 검사기 자체 결함은 답변 FAIL 로 바꾸지 않는다
        return [result(f"{sub}.check_error", sub, "검사 실행", "engine", Status.CHECK_ERROR,
                       reason=f"검사기 오류: {type(exc).__name__}: {exc}",
                       details={"traceback": traceback.format_exc(limit=3)})]


def _with_explanations(results: List[ConditionResult], ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    for r in results:
        d = r.to_dict()
        d["explain"] = explain_result(d, ctx)
        inherited = d["details"].get("inherited_from") if d["source"] == "history_instruction" else None
        if inherited:   # 현재 질문이 아니라 앞 턴의 지속 지시에서 온 조건임을 설명 첫머리에 밝힌다
            d["explain"]["summary"] = (f"{inherited['turn']}턴 사용자 지시(‘{inherited['instruction'][:60]}’)가 "
                                       f"이 턴에도 유지되는 조건입니다. " + (d["explain"].get("summary") or ""))
        out.append(d)
    return out


def _summarize(results: List[ConditionResult]) -> Dict[str, Any]:
    def block(rows: List[ConditionResult]) -> Dict[str, Any]:
        passed = sum(1 for r in rows if r.counted and r.verdict == PASS)
        failed = sum(1 for r in rows if r.counted and r.verdict == FAIL)
        unjudged = [r for r in rows if r.status in _UNJUDGED and r.verdict is None]
        statuses: Dict[str, int] = {}
        for r in rows:
            statuses[r.status] = statuses.get(r.status, 0) + 1
        decided = passed + failed
        return {"pass": passed, "fail": failed, "decided": decided,
                "pass_rate": (passed / decided) if decided else None,
                "unjudged": len(unjudged), "incomplete_with_fail": sum(1 for r in rows if r.status == Status.INCOMPLETE and r.verdict == FAIL),
                "statuses": statuses, "conditions": len(rows)}

    common = [r for r in results if r.area == "common"]
    special = [r for r in results if r.area != "common"]
    by_sub = {sub: block([r for r in common if r.subcategory == sub]) for sub in SUBCATEGORIES}
    by_cat = {cat: block([r for r in common if r.category == cat]) for cat in CATEGORY_NAMES}
    by_special = {sid: block([r for r in special if r.subcategory == sid]) for sid in SPECIALIZED}
    c = block(common)
    s = block(special)
    question_checked = any(r.source in _QUESTION_SOURCES and r.counted for r in common)
    if c["fail"]:
        overall = "FAIL 있음"
    elif c["decided"] == 0:
        overall = "판정한 조건 없음"
    elif c["unjudged"]:
        overall = "검사한 범위 PASS · 미완료 있음"
    else:
        overall = "검사한 범위 PASS"
    return {
        "common": c, "specialized": s,
        "by_category": by_cat, "by_subcategory": by_sub, "by_specialized": by_special,
        "check_level": "요구·계산·데이터 검사 포함" if question_checked else "기본 상태만 검사",
        "overall": overall,
        "note": "통과율 = PASS / (PASS + FAIL). 미검사·설정 없음·지원 밖은 분모에 넣지 않음. "
                "AIONA 특화 결과는 공통 통과율에 더하지 않음. 전체 내용 품질 판정이 아님.",
    }


def _prior_questions(items: List[Any]) -> Dict[int, Dict[str, Any]]:
    """대화 입력의 앞 턴 사용자 질문. 1턴부터 바로 앞 턴까지 한 번씩 모두 있을 때만 complete."""
    groups: Dict[str, List[tuple]] = {}
    for i, payload in enumerate(items):
        if isinstance(payload, dict):
            conversation, error = _conversation_of(payload)
            if conversation and not error:
                groups.setdefault(conversation["id"], []).append((conversation["turn"], i, payload.get("question")))
    out: Dict[int, Dict[str, Any]] = {}
    for members in groups.values():
        for turn, i, _ in members:
            if turn <= 1:
                continue
            earlier = [(t, q) for t, _, q in members if t < turn]
            complete = (sorted(t for t, _ in earlier) == list(range(1, turn))
                        and all(isinstance(q, str) and q.strip() for _, q in earlier))
            out[i] = {"prior": earlier if complete else [], "complete": complete}
    return out


def _mark_inherited(results: List[ConditionResult], inherited: List[Any]) -> None:
    """이전 턴 지시에서 온 결과 행을 표시한다. 판정·비교식은 바꾸지 않는다."""
    by_text = {req.text: req.origin for req in inherited}
    for r in results:
        origin = next((by_text.get(e.excerpt) for e in r.evidence if e.target == "history"), None)
        if origin:
            r.source = "history_instruction"
            r.title = f"{r.title} · {origin['turn']}턴 지시 유지"
            r.details = {**r.details, "inherited_from": dict(origin)}


def evaluate_case(payload: Dict[str, Any], cfg: Optional[AionaConfig] = None, index: int = 0,
                  on_event: Optional[Callable] = None, history: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    started = time.perf_counter()
    if not isinstance(payload, dict):
        return _input_error(f"case-{index + 1:05d}", "입력 항목이 JSON 객체가 아님")
    conversation, conversation_error = _conversation_of(payload)
    default_id = f"{conversation['id']}-T{conversation['turn']:02d}" if conversation else f"case-{index + 1:05d}"
    case_id = str(payload.get("case_id") or payload.get("id") or payload.get("question_id") or default_id)
    try:
        validate_json_numbers(payload)
    except ValueError as exc:
        return _input_error(case_id, str(exc), conversation)
    if "__parse_error__" in payload:
        return _input_error(case_id, str(payload["__parse_error__"]))
    question, answer = payload.get("question"), payload.get("answer")
    if "question" not in payload:
        return _input_error(case_id, "question 필드 누락", conversation)
    if not isinstance(question, str):
        return _input_error(case_id, f"question 이 문자열이 아님 ({type(question).__name__})", conversation)
    if not question.strip():
        return _input_error(case_id, "question 이 비어 있음", conversation)
    if "answer" not in payload:
        return _input_error(case_id, "answer 필드 누락 (빈 응답과 구분되는 입력 오류)", conversation)
    if not isinstance(answer, str):
        return _input_error(case_id, f"answer 가 문자열이 아님 ({type(answer).__name__})", conversation)
    displayed = payload.get("displayed_text")
    if displayed is not None and not isinstance(displayed, str):
        return _input_error(case_id, "displayed_text 는 문자열이어야 함", conversation)
    if conversation_error:
        return _input_error(case_id, conversation_error)
    reference, reference_error = _reference_of(payload)
    if reference_error:
        return _input_error(case_id, reference_error, conversation)
    reference_text = reference if isinstance(reference, str) else (
        json.dumps(reference, ensure_ascii=False) if reference is not None else "")
    for name, value, limit in (("question", question, LIMITS["question_chars"]),
                               ("answer", answer, LIMITS["answer_chars"]),
                               ("displayed_text", displayed or "", LIMITS["displayed_chars"]),
                               ("reference", reference_text, LIMITS["reference_chars"])):
        if len(value) > limit:
            return {"case_id": case_id, "case_status": "LIMIT_EXCEEDED",
                    "input_error": f"{name} 길이 {len(value)}자가 안전 한도 {limit}자를 넘음 — 품질 FAIL 아님",
                    "results": [], "observations": [], "summary": None, "meta": {},
                    **({"conversation": conversation} if conversation else {})}

    system_id = payload.get("system_id")
    cfg = cfg if cfg is not None else load_aiona_config()
    # 대화의 앞 턴 사용자 지시 중 '앞으로·계속' 같은 지속 형식·분량 조건을 이 턴에도 검사한다 (배치 입력만).
    inherited, inherited_audit = persistent_conditions(history["prior"], question) if history and history["complete"] else ([], [])
    if history and not history["complete"]:
        inherited_audit = [{"applied": False, "why": "앞 턴 질문이 누락·중복·입력 오류여서 이전 지시를 확인하지 않음"}]
    ctx = Ctx(case_id=case_id, question=question, answer=answer, displayed_text=displayed,
              system_id=str(system_id) if system_id is not None else None, aiona_config=cfg, inherited=inherited)

    results: List[ConditionResult] = []
    for sub, fn in _COMMON:
        if on_event:
            on_event({"type": "criterion_start", "index": index, "qid": case_id,
                      "code": sub, "name": SUBCATEGORIES[sub], "stage": "CODE"})
        group = _run_group(sub, fn, ctx)
        results.extend(group)
        if on_event:
            for row in group:
                if row.counted or row.status in _UNJUDGED:
                    on_event({"type": "code_result", "index": index, "qid": case_id, "row": row.to_dict()})
            on_event({"type": "criterion_end", "index": index, "qid": case_id, "code": sub, "stage": "CODE"})
    if cfg.enabled:
        results.extend(_run_group("AIONA-M", check_aiona, ctx, cfg, list(results)))
    if inherited:
        _mark_inherited(results, inherited)

    # 같은 대상·조건·측정 규칙의 중복 결과는 하나만 집계한다.
    seen: Dict[str, ConditionResult] = {}
    for r in results:
        if r.dedupe_key and r.status == Status.EXECUTED:
            first = seen.get(r.dedupe_key)
            if first is None:
                seen[r.dedupe_key] = r
            elif first is not r and first.area == r.area:
                r.status, r.verdict, r.duplicate_of = Status.DUPLICATE, None, first.condition_id
                r.reason = f"같은 조건 결과({first.condition_id})를 참조 — 중복 집계하지 않음"

    observations = []
    for fn in (foreign_script_observation, table_observation):
        try:
            observations.append(fn(ctx))
        except Exception as exc:
            observations.append({"type": "observation", "observation_id": fn.__name__,
                                 "status": Status.CHECK_ERROR, "error": f"{type(exc).__name__}: {exc}"})

    meta = {
        "system_id": ctx.system_id,
        "source": payload.get("source"),
        "source_version": payload.get("source_version"),
        "input_hash": stable_hash({"question": question, "answer": answer, "displayed_text": displayed}),
        "config_hash": cfg.raw_hash,
        "displayed_text_provided": displayed is not None,
        "question_conditions": ctx.conds.to_list(),
        "answer_structure": ctx.aa.structure_summary(),
        **({"inherited_conditions": inherited_audit} if inherited_audit else {}),
    }
    # 대화·참조 데이터 키는 입력에 있을 때만 넣는다 — 단일 질문·답변 입력의 결과 해시를 바꾸지 않기 위해
    return {
        "case_id": case_id,
        "case_status": "EVALUATED",
        "question": question,
        "answer": answer,
        "displayed_text": displayed,
        **({"conversation": conversation} if conversation else {}),
        **({"reference": reference} if reference is not None else {}),
        "results": _with_explanations(results, ctx),
        "observations": observations,
        "summary": _summarize(results),
        "meta": meta,
        "timing_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def summarize_batch(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    totals = {"cases": len(cases), "evaluated": 0, "input_errors": 0, "limit_exceeded": 0}
    common = {"pass": 0, "fail": 0, "unjudged": 0}
    special = {"pass": 0, "fail": 0, "unjudged": 0}
    by_sub = {s: {"pass": 0, "fail": 0, "unjudged": 0} for s in SUBCATEGORIES}
    by_special = {s: {"pass": 0, "fail": 0, "unjudged": 0} for s in SPECIALIZED}
    levels: Dict[str, int] = {}
    for c in cases:
        if c["case_status"] == "INPUT_ERROR":
            totals["input_errors"] += 1
            continue
        if c["case_status"] == "LIMIT_EXCEEDED":
            totals["limit_exceeded"] += 1
            continue
        totals["evaluated"] += 1
        s = c["summary"]
        levels[s["check_level"]] = levels.get(s["check_level"], 0) + 1
        for k in ("pass", "fail", "unjudged"):
            common[k] += s["common"][k]
            special[k] += s["specialized"][k]
            for sub in SUBCATEGORIES:
                by_sub[sub][k] += s["by_subcategory"][sub][k]
            for sid in SPECIALIZED:
                by_special[sid][k] += s["by_specialized"][sid][k]
    rate = lambda b: (b["pass"] / (b["pass"] + b["fail"])) if (b["pass"] + b["fail"]) else None
    for b in [common, special, *by_sub.values(), *by_special.values()]:
        b["pass_rate"] = rate(b)
    return {"totals": totals, "common": common, "specialized": special, "by_subcategory": by_sub,
            "by_specialized": by_special, "check_levels": levels}


def result_digest(cases: List[Dict[str, Any]]) -> str:
    """재현성 비교용 해시 — 실행 시각·실행 ID·소요 시간은 제외한다."""
    stable = [{k: v for k, v in c.items() if k not in ("timing_ms",)} for c in cases]
    return stable_hash(stable)


def evaluate_batch(payloads: Iterable[Any], cfg: Optional[AionaConfig] = None,
                   progress: Optional[Callable[[int, int], None]] = None,
                   total: Optional[int] = None, on_event: Optional[Callable] = None) -> Dict[str, Any]:
    cfg = cfg if cfg is not None else load_aiona_config()
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    cases = []
    items = list(payloads)
    if len(items) > LIMITS["batch_cases"]:
        raise ValueError(f"일괄 입력 {len(items)}건이 한 번 실행 한도 {LIMITS['batch_cases']}건을 넘음 — 파일을 나눠 실행")
    n = len(items)
    prior = _prior_questions(items)
    for i, payload in enumerate(items):
        if on_event:
            qid = str(payload.get("case_id") or payload.get("id") or payload.get("question_id") or f"case-{i + 1:05d}") if isinstance(payload, dict) else f"case-{i + 1:05d}"
            on_event({"type": "q_start", "index": i, "qid": qid})
        cases.append(evaluate_case(payload, cfg, i, on_event=on_event, history=prior.get(i)))
        if progress:
            progress(i + 1, n)
    ids: Dict[str, int] = {}
    for c in cases:
        ids[c["case_id"]] = ids.get(c["case_id"], 0) + 1
    duplicates = sorted(k for k, v in ids.items() if v > 1)
    return {
        "schema": "aiona-code-eval-result/v16",
        "versions": versions(),
        "aiona_config": cfg.summary(),
        "summary": summarize_batch(cases),
        "duplicate_case_ids": duplicates,
        "result_digest": result_digest(cases),
        "cases": cases,
        "run": {"run_id": uuid.uuid4().hex[:12], "started_at": started_at,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "note": "run 항목은 재현성 비교에서 제외"},
    }
