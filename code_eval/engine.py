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
from .core import ERROR_CHECK_ERROR, ERROR_LIMIT_EXCEEDED, LIMITS, Ctx, case_error, nfc, versions
from .model import CATEGORY_NAMES, FAIL, PASS, SPECIALIZED, SUBCATEGORIES, ConditionResult, Status, stable_hash

__all__ = ["evaluate_case", "evaluate_batch", "summarize_batch", "result_digest", "_drop_duplicates"]

#: 실행 순서. 2-2·3-3 은 1-2 의 JSON 파싱 결과를 공유하므로 1-2 뒤에 둔다.
_COMMON = (
    ("1-1", check_1_1), ("1-2", check_1_2), ("1-3", check_1_3),
    ("2-1", check_2_1), ("2-2", check_2_2),
    ("3-1", check_3_1), ("3-2", check_3_2), ("3-3", check_3_3),
)

#: legacy — v16.2.x 까지 저장된 결과의 미판정 상태값. 새 공통 결과 행은 EXECUTED(PASS/FAIL) 뿐이며
#: AIONA 특화 행(보류 중)과 옛 JSON 을 읽는 코드(common_adjudication 등)만 이 집합을 쓴다.
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
    """검사기 예외·재귀 한도는 결과 행이 아니라 사례 errors 로 남기고 이 소분류 행은 만들지 않는다."""
    try:
        return list(fn(ctx, *args))
    except RecursionError:
        case_error(ctx, f"{sub}.check_error", sub, ERROR_LIMIT_EXCEEDED, "분석 깊이 한도 초과 — 품질 FAIL 이 아님")
        return []
    except Exception as exc:  # 검사기 자체 결함은 답변 FAIL 로 바꾸지 않는다
        case_error(ctx, f"{sub}.check_error", sub, ERROR_CHECK_ERROR, f"검사기 오류: {type(exc).__name__}: {exc}",
                   traceback=traceback.format_exc(limit=3))
        return []


def _drop_duplicates(results: List[ConditionResult]) -> List[ConditionResult]:
    """같은 dedupe_key 의 두 번째 EXECUTED 행 처리 — 공통 행은 제외하고, AIONA 특화 행은 옛 동작대로 DUPLICATE 로 표시한다."""
    kept, seen = [], {}
    for r in results:
        if r.dedupe_key and r.status == Status.EXECUTED:
            first = seen.get((r.area, r.dedupe_key))
            if first is None:
                seen[(r.area, r.dedupe_key)] = r
            elif r.area == "common":
                continue
            else:
                r.status, r.verdict, r.duplicate_of = Status.DUPLICATE, None, first.condition_id
                r.reason = f"같은 조건 결과({first.condition_id})를 참조 — 중복 집계하지 않음"
        kept.append(r)
    return kept


def _with_explanations(results: List[ConditionResult], ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    for r in results:
        d = r.to_dict()
        d["explain"] = explain_result(d, ctx)
        out.append(d)
    return out


def _summarize(results: List[ConditionResult], errors: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    errors = errors or []

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
    elif c["unjudged"]:  # 옛 결과 투영용 — 새 공통 행에는 미판정 상태가 없다
        overall = "검사한 범위 PASS · 미완료 있음"
    elif errors:
        overall = "검사한 범위 PASS · 실행 오류 있음"
    else:
        overall = "검사한 범위 PASS"
    return {
        "common": c, "specialized": s,
        "by_category": by_cat, "by_subcategory": by_sub, "by_specialized": by_special,
        "errors": len(errors),
        "check_level": "요구·계산·데이터 검사 포함" if question_checked else "기본 상태만 검사",
        "overall": overall,
        "note": "통과율 = PASS / (PASS + FAIL). 판정할 수 없는 조건은 행을 만들지 않음(meta.skipped_conditions). "
                "검사기 오류·한도 초과는 errors 에 기록. AIONA 특화 결과는 공통 통과율에 더하지 않음. 전체 내용 품질 판정이 아님.",
    }


def evaluate_case(payload: Dict[str, Any], cfg: Optional[AionaConfig] = None, index: int = 0,
                  on_event: Optional[Callable] = None) -> Dict[str, Any]:
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
    ctx = Ctx(case_id=case_id, question=question, answer=answer, displayed_text=displayed,
              system_id=str(system_id) if system_id is not None else None, aiona_config=cfg)

    results: List[ConditionResult] = []
    for sub, fn in _COMMON:
        if on_event:
            on_event({"type": "criterion_start", "index": index, "qid": case_id,
                      "code": sub, "name": SUBCATEGORIES[sub], "stage": "CODE"})
        errors_before = len(ctx.errors)
        group = _run_group(sub, fn, ctx)
        results.extend(group)
        if on_event:
            for row in group:
                if row.counted:
                    on_event({"type": "code_result", "index": index, "qid": case_id, "row": row.to_dict()})
            # 검사 실행 오류는 결과 행이 아니지만 라이브 화면에는 미판정으로 보이게 보낸다
            for n, err in enumerate(ctx.errors[errors_before:], errors_before + 1):
                on_event({"type": "code_result", "index": index, "qid": case_id,
                          "row": {"check_id": err["check_id"], "condition_id": f"error-{n}", "subcategory": err["subcategory"],
                                  "title": "검사 실행 오류", "verdict": None, "reason": err["message"]}})
            on_event({"type": "criterion_end", "index": index, "qid": case_id, "code": sub, "stage": "CODE"})
    if cfg.enabled:
        results.extend(_run_group("AIONA-M", check_aiona, ctx, cfg, list(results)))

    # 같은 대상·조건·측정 규칙의 중복 공통 결과는 하나만 남긴다 (DUPLICATE 행을 만들지 않음).
    results = _drop_duplicates(results)

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
        # 판정하지 않은 조건의 사유. 결과 행이 아니며 집계·result_digest 에 들어가지 않는다.
        "skipped_conditions": list(ctx.skipped),
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
        "errors": list(ctx.errors),
        "summary": _summarize(results, ctx.errors),
        "meta": meta,
        "timing_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def summarize_batch(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    totals = {"cases": len(cases), "evaluated": 0, "input_errors": 0, "limit_exceeded": 0, "check_errors": 0}
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
        totals["check_errors"] += bool(c.get("errors"))
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
    """재현성 비교용 해시 — 실행 시각·실행 ID·소요 시간과 meta.skipped_conditions(판정 사유 메모)는 제외한다."""
    stable = []
    for c in cases:
        item = {k: v for k, v in c.items() if k not in ("timing_ms",)}
        if isinstance(item.get("meta"), dict) and "skipped_conditions" in item["meta"]:
            item["meta"] = {k: v for k, v in item["meta"].items() if k != "skipped_conditions"}
        stable.append(item)
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
    for i, payload in enumerate(items):
        if on_event:
            qid = str(payload.get("case_id") or payload.get("id") or payload.get("question_id") or f"case-{i + 1:05d}") if isinstance(payload, dict) else f"case-{i + 1:05d}"
            on_event({"type": "q_start", "index": i, "qid": qid})
        cases.append(evaluate_case(payload, cfg, i, on_event=on_event))
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
