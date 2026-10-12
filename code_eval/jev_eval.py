"""Jev 의미 평가 단계 — CODE 평가와 별개로 모든 질문·답변에 J1~J6 중 정해진 기준을 판단한다.

    CODE 결과(batch) → 평가된 질문마다 Jev 1회 호출(평가할 기준 동시) → batch["jev_eval"]

- CODE PASS·FAIL 과 관계없이 모든 질문을 평가한다. 확정된 CODE 판정은 넘기지 않는다.
  (v16.2.x 까지 있던 공통 미판정 보완 C 기준·code_completion 은 없다. 옛 기록은 common_adjudication 이 읽기만 한다.)
- CODE 판정·결과 해시는 바꾸지 않는다. CODE 와 Jev 를 합친 단일 점수를 만들지 않는다.
- Jev 장애·키 없음은 이 단계의 상태로만 남고 CODE 결과 반환을 막지 않는다.
- 질문마다 어떤 J 를 물을지는 ``jev_applicability`` 가 코드 규칙으로 정한다 (LLM 없음).
  J1 은 모든 질문, J2~J4 는 질문 요청 문장에 근거 문구가 있을 때만, J5 는 답변에 데이터가 있거나 참조 데이터가 있을 때,
  J6 는 대화 입력의 2번째 턴부터. 근거 문구·'볼 것'은 화면 표시용이며 Jev 에 보내지 않는다.
- 대화 입력(case["conversation"] = {"id", "turn"})은 같은 대화의 앞 턴을 turn 순으로 묶은 history 를 state 에 함께 보낸다.
  앞 턴이 하나라도 빠졌거나 입력 오류·중복이면 J6 는 묻지 않고 그 대화는 미완료(INCOMPLETE)로 집계한다 — 판정을 지어내지 않는다.
  history 의 질문·답변은 공백·줄바꿈을 포함한 원문 전체를 보낸다. 길이에 따른 축약·생략은 하지 않는다.
  참조 데이터(case["reference"])는 있을 때만 state 에 넣는다.
"""

from __future__ import annotations

import copy
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from threading import Event
from typing import Any, Callable, Dict, List, Optional, Tuple

from .jev_applicability import APPLIES_WHEN, JEV_APPLICABILITY_VERSION, jev_applicability
from .jev_client import JevClient, JevClientConfig, JevStatus, resolve_jev_client
from .jev_criteria import CRITERIA, DECISIONS, JEV_CRITERIA_VERSION, describe, jev_questions
from .llm_client import redact
from .semantic_policy import DECISION_POLICY, apply_final_decision_policy

__all__ = ["JEV_EVAL_VERSION", "jev_request", "parse_decisions", "evaluate_jev_case", "evaluate_jev_batch",
           "jev_after_code", "evaluation_log", "build_history", "conversation_contexts", "conversations_summary"]

JEV_EVAL_VERSION = "jev-semantic-eval-v2.5"
DEFAULT_WORKERS = 4
ENGINE_LABELS = {"jev": "Jev", "claude_cli": "Claude CLI", "codex_cli": "GPT CLI (Codex)",
                 "openai_api": "OpenAI API", "anthropic_api": "Claude API"}
FACT_SCREENING_ENGINES = {'codex_cli', 'claude_cli', 'openai_api', 'anthropic_api'}
FACT_SCREENING_VERSION = 'jev-applicability-v2.3-llm-screening'
CANCEL_REASON = "평가 중단 요청으로 의미평가를 완료하지 못했습니다."

#: 대화 길이 구간 — 턴 번호로 묶어 J6 유지율을 본다
TURN_BANDS: Tuple[Tuple[str, int, Optional[int]], ...] = (("1~10", 1, 10), ("11~20", 11, 20), ("21~30", 21, 30), ("31+", 31, None))

ALL_IDS = tuple(c.id for c in CRITERIA)


def jev_request(question: str, answer: str, criteria: Tuple[str, ...] = ALL_IDS, reference: Any = None,
                history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Jev 에 보내는 내용. 질문·답변(과 있을 때만 reference·history)만 담고 평가할 기준만 묻는다 (CODE 결과·표시본 없음)."""
    state: Dict[str, Any] = {"question": question, "answer": answer}
    if reference is not None:
        state["reference"] = reference
    if history:
        state["history"] = history
    questions = {cid: q for cid, q in jev_questions().items() if cid in criteria}
    return {"state": state, "questions": questions}


def _probability(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError(f"{where} 값이 0~1 숫자가 아님")
    return float(value)


def parse_decisions(body: Dict[str, Any], criteria: Tuple[str, ...] = ALL_IDS,
                    names: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, Any]]:
    """Jev choice 응답에서 물은 기준의 결정만 AIONA 내부 형태로 바꾼다. 형식이 다르면 ValueError."""
    answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(answers, dict):
        raise ValueError("answers 객체 없음")
    decisions: Dict[str, Dict[str, Any]] = {}
    names = names if names is not None else {c.id: c.name for c in CRITERIA}
    for cid in criteria:
        entry = answers.get(cid)
        if not isinstance(entry, dict):
            raise ValueError(f"{cid} 응답 없음")
        decision = entry.get("choice")
        if (decision == 'UNVERIFIED' and cid in ALL_IDS) or (decision == 'NOT_APPLICABLE' and cid == 'J5'):
            confidence = entry.get('confidence')
            try:
                confidence = round(_probability(confidence, f'{cid} confidence'), 4)
            except ValueError:
                confidence = None
            decisions[cid] = {'criterion_id': cid, 'name': names[cid], 'decision': None,
                              'status': decision, 'confidence': confidence, 'probabilities': {}}
        else:
            # J 기준은 UNVERIFIED/NOT_APPLICABLE 이 아니면 PASS/FAIL 이어야 한다.
            if decision not in ('PASS', 'FAIL'):
                raise ValueError(f"{cid} choice 가 PASS/FAIL 이 아님: {decision!r}")
            probs = entry.get("probabilities")
            if not isinstance(probs, dict) or {'PASS', 'FAIL'} - set(probs):
                raise ValueError(f"{cid} probabilities 에 PASS/FAIL 이 없음")
            decisions[cid] = {
                "criterion_id": cid,
                "name": names[cid],
                "decision": decision,
                "confidence": round(_probability(entry.get("confidence"), f"{cid} confidence"), 4),
                "probabilities": {k: round(_probability(probs[k], f"{cid} probabilities.{k}"), 4) for k in ('PASS', 'FAIL')},
            }
        if isinstance(entry.get("reason"), str) and entry["reason"].strip():  # Claude CLI 시험 엔진만 근거를 준다
            decisions[cid]["reason"] = entry["reason"].strip()
        if isinstance(entry.get('fact_check'), dict):
            decisions[cid]['fact_check'] = copy.deepcopy(entry['fact_check'])
        if isinstance(entry.get('context_checks'), list):
            decisions[cid]['context_checks'] = copy.deepcopy(entry['context_checks'])
        if isinstance(entry.get('model_original_assessment'), dict):
            original = entry['model_original_assessment']
            decisions[cid]['model_original_assessment'] = {
                key: original.get(key) if isinstance(original.get(key), str) else None for key in ('choice', 'reason')}
    return decisions


def _record(case_id: str, status: str, criteria: Tuple[str, ...] = (), **kw: Any) -> Dict[str, Any]:
    record = {"case_id": case_id, "status": status, "criteria": list(criteria), "decisions": None, "model": None,
              "latency_ms": None, "attempts": 0, "error": None, "usage": {}, "j5_basis": None, "context": None}
    record.update(kw)
    return record


def evaluate_jev_case(case_id: str, question: str, answer: str, client: Optional[JevClient],
                      access_reason: Optional[str] = None, criteria: Tuple[str, ...] = ALL_IDS,
                      reference: Any = None, history: Optional[List[Dict[str, Any]]] = None,
                      cancel_event: Optional[Event] = None) -> Dict[str, Any]:
    # Legacy native Jev keeps its historical knowledge/reference contract. New
    # CLI/API transports finalize explicit evidence before this parser runs.
    basis = {"j5_basis": ("reference" if reference is not None else "engine") if "J5" in criteria else None}
    def finish(status, **kw):
        return _record(case_id, status, criteria, **kw)
    if cancel_event is not None and cancel_event.is_set():
        return finish(JevStatus.SKIPPED, error=CANCEL_REASON, **basis)
    if client is None:
        return finish(JevStatus.NOT_CONFIGURED, error=access_reason, **basis)
    started = time.monotonic()
    try:
        request = jev_request(question, answer, criteria, reference, history)
        if cancel_event is not None and getattr(client, "supports_cancellation", False):
            request["cancel_event"] = cancel_event
        if cancel_event is not None and cancel_event.is_set():
            return finish(JevStatus.SKIPPED, error=CANCEL_REASON, **basis)
        call = client.system_one(**request)
    except Exception as exc:  # 대역·SDK 의 예외도 이 질문의 API 오류로만 남긴다
        if cancel_event is not None and cancel_event.is_set():
            return finish(JevStatus.SKIPPED, error=CANCEL_REASON,
                          latency_ms=int((time.monotonic() - started) * 1000), **basis)
        return finish(JevStatus.API_ERROR, error=redact(f"{type(exc).__name__}: {exc}"),
                       latency_ms=int((time.monotonic() - started) * 1000), **basis)
    common = {"latency_ms": call.latency_ms, "attempts": call.attempts, "usage": call.usage or {}, **basis}
    if isinstance(call.body, dict) and isinstance(call.body.get('verification'), dict):
        common['verification'] = copy.deepcopy(call.body['verification'])
        if 'J5' in criteria:
            common['j5_basis'] = 'unverified'
    if call.status != "OK":
        if cancel_event is not None and cancel_event.is_set():
            return finish(JevStatus.SKIPPED, error=CANCEL_REASON, **common)
        return finish(call.status, error=redact(call.error), **common)
    model = call.body.get("model") if isinstance(call.body, dict) else None
    common["model"] = model if isinstance(model, str) else None
    try:
        decisions = parse_decisions(call.body or {}, criteria)
    except (ValueError, TypeError, KeyError) as exc:
        record = _record(case_id, JevStatus.INVALID_RESPONSE, criteria, error=str(exc), **common)
    else:
        record = _record(case_id, JevStatus.SUCCESS, criteria, decisions=decisions, **common)
        record['criteria'] = [cid for cid in record['criteria'] if decisions[cid].get('status') != 'NOT_APPLICABLE']
        record['review_required'] = any(d.get('status') == 'UNVERIFIED' for d in decisions.values())
        if 'J5' in criteria:
            fact_check = decisions['J5'].get('fact_check')
            if decisions['J5'].get('status') == 'NOT_APPLICABLE':
                record['j5_basis'] = None
            elif fact_check is not None:
                actual_basis = fact_check.get('basis')
                record['j5_basis'] = actual_basis if actual_basis in ('web', 'reference', 'input', 'mixed') else 'unverified'
            elif decisions['J5'].get('status') == 'UNVERIFIED':
                record['j5_basis'] = 'unverified'
    # The transport has already completed any evidence recovery. Apply the
    # explicit final-outcome policy only now, before summaries/events are built.
    apply_final_decision_policy(record, state=request["state"])
    return record


def _overall(records: List[Dict[str, Any]]) -> str:
    if any(r["status"] == JevStatus.SKIPPED and r.get("error") == CANCEL_REASON for r in records):
        return "PARTIAL"
    kinds = {r["status"] for r in records if r["status"] != JevStatus.SKIPPED}
    if not kinds:
        return "NO_TARGET"
    if any(r.get('review_required') for r in records):
        return 'PARTIAL'
    return kinds.pop() if len(kinds) == 1 else "PARTIAL"


# --- 대화(다회차) 입력 -----------------------------------------------------------------

def _history_chars(items: List[Dict[str, Any]]) -> int:
    return sum(len(it["question"]) + len(it["answer"]) for it in items)


def build_history(prior: List[Tuple[int, str, str]]) -> Tuple[List[Dict[str, Any]], bool]:
    """앞 턴을 새 레코드로 묶고 질문·답변 문자열은 원문 그대로 보존한다.

    반환 형태 (history, truncated)는 과거 결과와 호환되도록 유지한다.
    이력 길이에 따라 답변·질문·턴을 줄이지 않으므로 truncated는 항상 False다.
    """
    return [{"turn": turn, "question": question, "answer": answer}
            for turn, question, answer in prior], False


def conversation_contexts(cases: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """대화 입력 case 마다 앞 턴 정보 — {case 인덱스: {"id", "turn", "total_turns", "prior": [(turn, 질문, 답변)…], "complete", "issues"}}.

    turn 번호만 믿고 입력 순서는 보지 않는다. 앞 턴 누락·입력 오류·같은 turn 중복은 issues 에 남기고 complete=False 로 둔다.
    대화 입력이 아닌 case 는 포함하지 않는다.
    """
    groups: Dict[str, List[Tuple[int, int]]] = {}
    for i, case in enumerate(cases):
        conv = case.get("conversation")
        if conv:
            groups.setdefault(conv["id"], []).append((conv["turn"], i))
    contexts: Dict[int, Dict[str, Any]] = {}
    for cid, members in groups.items():
        members.sort()
        counts: Dict[int, int] = {}
        for turn, _ in members:
            counts[turn] = counts.get(turn, 0) + 1
        turns = sorted(counts)
        duplicates = sorted(t for t, n in counts.items() if n > 1)
        for turn, i in members:
            prior: List[Tuple[int, str, str]] = []
            issues: List[str] = []
            for earlier, j in members:
                if earlier >= turn:
                    break
                if earlier in duplicates:
                    continue  # 어느 쪽이 진짜인지 알 수 없어 history 에 넣지 않는다 (아래 '중복'으로 기록)
                if cases[j].get("case_status") == "EVALUATED":
                    prior.append((earlier, cases[j]["question"], cases[j]["answer"]))
                else:
                    issues.append(f"{earlier}턴 {cases[j].get('case_status')}")
            # 실제로 주어진 턴 사이의 빈 구간만 기록한다. 거대한 턴 번호만큼 할당하지 않는다.
            missing = []
            expected = 1
            for earlier in turns:
                if earlier >= turn:
                    break
                if expected < earlier:
                    missing.append((expected, earlier - 1))
                expected = earlier + 1
            if expected < turn:
                missing.append((expected, turn - 1))
            if missing:
                issues.append("누락 " + "·".join(f"{lo}턴" if lo == hi else f"{lo}~{hi}턴" for lo, hi in missing))
            dup_before = [t for t in duplicates if t <= turn]
            if dup_before:
                issues.append("중복 " + "·".join(f"{t}턴" for t in dup_before))
            contexts[i] = {"id": cid, "turn": turn, "total_turns": members[-1][0], "prior": prior,
                           "complete": not issues, "issues": issues}
    return contexts


def _context_of(ctx: Optional[Dict[str, Any]], history: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    if not ctx:
        return None
    history = history or []
    return {"conversation_id": ctx["id"], "turn": ctx["turn"], "total_turns": ctx["total_turns"],
            "history_turns": len(history), "history_chars": _history_chars(history),
            "history_truncated": bool(ctx.get("truncated")), "history_complete": ctx["complete"], "issues": ctx["issues"]}


def _band(turn: int) -> str:
    for label, low, high in TURN_BANDS:
        if turn >= low and (high is None or turn <= high):
            return label
    return TURN_BANDS[-1][0]


def _rate(passed: int, failed: int) -> Optional[float]:
    return round(passed / (passed + failed), 4) if passed + failed else None


def conversations_summary(cases: List[Dict[str, Any]], records: List[Dict[str, Any]],
                          contexts: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """대화별 J6 결과와 턴 구간별 유지율. 대화 입력이 없으면 count 0."""
    by_id: Dict[str, List[Tuple[int, int]]] = {}
    for i, ctx in contexts.items():
        by_id.setdefault(ctx["id"], []).append((ctx["turn"], i))
    bands = {label: {"targets": 0, "pass": 0, "fail": 0, "unverified": 0, "pending": 0} for label, _, _ in TURN_BANDS}
    items: List[Dict[str, Any]] = []
    for cid, members in by_id.items():
        members.sort()
        turn_results, fail_turns, issues = [], [], []
        asked = decided = 0
        complete = True
        for turn, i in members:
            record = records[i] or {}
            case = cases[i]
            if case.get("case_status") != "EVALUATED":
                issues.append(f"{turn}턴 {case.get('case_status')}")
                complete = False
            complete = complete and contexts[i]["complete"]
            for issue in contexts[i]["issues"]:
                if issue not in issues:
                    issues.append(issue)
            was_asked = "J6" in (record.get("criteria") or [])
            d = (record.get("decisions") or {}).get("J6")
            decision = d["decision"] if d else None
            turn_results.append({"turn": turn, "case_id": case["case_id"], "case_index": i,
                                 "asked": was_asked, "decision": decision,
                                 "confidence": d["confidence"] if d else None,
                                 "status": d.get('status') if d else None,
                                 "context_checks": copy.deepcopy(d.get('context_checks', [])) if d else []})
            if was_asked:
                asked += 1
                bands[_band(turn)]["targets"] += 1
            if was_asked and decision not in ('PASS', 'FAIL'):
                bands[_band(turn)]['pending'] += 1
                if d and d.get('status') == 'UNVERIFIED':
                    bands[_band(turn)]['unverified'] += 1
            if decision in ('PASS', 'FAIL'):
                decided += 1
                bands[_band(turn)]["pass" if decision == "PASS" else "fail"] += 1
                if decision == "FAIL":
                    fail_turns.append(turn)
        if not asked:
            issues.append("J6 를 물은 턴 없음")
        if fail_turns:
            result = "FAIL"
        elif asked and decided == asked and complete:
            result = "PASS"
        else:
            result = "INCOMPLETE"
        items.append({"id": cid, "turns": members[-1][0], "cases": len(members), "j6_turns": asked, "result": result,
                      "first_fail_turn": fail_turns[0] if fail_turns else None, "fail_turns": fail_turns,
                      "issues": issues, "turn_results": turn_results})
    for band in bands.values():
        band["rate"] = _rate(band["pass"], band["fail"])
    n = {k: sum(1 for it in items if it["result"] == k) for k in ("PASS", "FAIL", "INCOMPLETE")}
    return {"count": len(items), "pass": n["PASS"], "fail": n["FAIL"], "incomplete": n["INCOMPLETE"],
            "rate": _rate(n["PASS"], n["FAIL"]), "bands": bands, "items": items,
            "note": "대화는 J6 를 물은 모든 턴이 PASS 일 때 PASS, 하나라도 FAIL 이면 FAIL. "
                    "앞 턴 누락·입력 오류·중복이나 미실행 턴이 있으면 미완료(INCOMPLETE)로 두고 판정하지 않음."}


# --- 배치 -------------------------------------------------------------------------------

def evaluate_jev_batch(batch: Dict[str, Any], client: Optional[JevClient],
                       config: Optional[JevClientConfig] = None, access_reason: Optional[str] = None,
                       workers: int = DEFAULT_WORKERS,
                       progress: Optional[Callable[[int, int], None]] = None,
                       on_record: Optional[Callable[[int, Dict[str, Any]], None]] = None,
                       on_start: Optional[Callable[[int], None]] = None,
                       on_event: Optional[Callable] = None,
                       cancel_event: Optional[Event] = None) -> Dict[str, Any]:
    """질문마다 한 번 호출한다. 기록 순서는 batch["cases"] 와 같다 (같은 case_id 가 있어도 순서로 대응).

    대화 입력은 호출 전에 앞 턴 history 를 미리 조립해 두므로 동시 호출 중에 공유 상태를 바꾸지 않는다.
    """
    started = time.perf_counter()
    config = config if config is not None else getattr(client, 'config', None)
    engine = getattr(config, 'engine', 'jev')
    fact_screening = engine in FACT_SCREENING_ENGINES
    applicability_version = FACT_SCREENING_VERSION if fact_screening else JEV_APPLICABILITY_VERSION
    cases = batch["cases"]
    records: List[Optional[Dict[str, Any]]] = [None] * len(cases)
    targets: List[int] = []
    applies: Dict[int, Dict[str, Any]] = {}
    contexts = conversation_contexts(cases)
    histories: Dict[int, List[Dict[str, Any]]] = {}
    for i, case in enumerate(cases):
        ctx = contexts.get(i)
        if case.get("case_status") != "EVALUATED":
            records[i] = _record(case["case_id"], JevStatus.SKIPPED,
                                 error=f"CODE 입력 검증 결과 {case.get('case_status')} — 평가할 질문·답변이 없음")
            continue
        targets.append(i)
        conversation = None
        if ctx:
            history, truncated = build_history(ctx["prior"])
            histories[i] = history
            ctx["truncated"] = truncated
            conversation = {"id": ctx["id"], "turn": ctx["turn"], "issues": ctx["issues"],
                            "history_turns": len(history) if ctx["complete"] else 0}
        applies[i] = jev_applicability(case["question"], case["answer"], reference=case.get("reference"),
                                       conversation=conversation)
        if fact_screening:
            applies[i]['version'] = applicability_version
            applies[i]['not_applicable'].pop('J5', None)
            applies[i]['evidence']['J5'] = {'source': 'answer', 'status': 'SCREENING',
                'rules': ['모든 답변의 사실 주장 확인'], 'matches': [],
                'excerpt': case['answer'][:120], 'look_for': '수치·명칭·설명을 포함한 사실 주장 여부와 검증 근거 확인'}
            applies[i]['criteria'] = [cid for cid in ALL_IDS if cid in applies[i]['evidence']]

    def run(i: int) -> Dict[str, Any]:
        case = cases[i]
        if cancel_event is not None and cancel_event.is_set():
            return _record(case["case_id"], JevStatus.SKIPPED, tuple(applies[i]["criteria"]), error=CANCEL_REASON)
        if on_start:
            on_start(i)
        if on_event:
            for criterion in CRITERIA:
                if criterion.id in applies[i]["criteria"]:
                    on_event({"type": "criterion_start", "index": i, "qid": case["case_id"],
                              "code": criterion.id, "name": criterion.name, "stage": "LLM", "together": True})
        return evaluate_jev_case(case["case_id"], case["question"], case["answer"], client, access_reason,
                                 tuple(applies[i]["criteria"]), reference=case.get("reference"),
                                 history=histories.get(i) or None, cancel_event=cancel_event)

    def publish(i: int, record: Dict[str, Any]) -> None:
        record["applicability"] = copy.deepcopy(applies.get(i))
        record["context"] = _context_of(contexts.get(i), histories.get(i))
        applicability = record['applicability']
        for cid, decision in (record.get('decisions') or {}).items():
            if decision.get('status') == 'NOT_APPLICABLE' and applicability:
                applicability['criteria'] = [k for k in applicability['criteria'] if k != cid]
                applicability['evidence'].pop(cid, None)
                applicability.setdefault('not_applicable', {})[cid] = decision.get('reason') or '검증할 사실 주장이 없습니다.'
                applicability.setdefault('screening', {})[cid] = copy.deepcopy(decision)
        ctx = contexts.get(i)
        if (record.get('error') != CANCEL_REASON and cases[i].get('case_status') == 'EVALUATED'
                and ctx and ctx['turn'] > 1 and not ctx['complete']):
            # Never ask the model to reconstruct missing history. Preserve other
            # valid decisions, but make this explicit unknown part of completion.
            reason = '앞 대화 이력을 확인할 수 없습니다: ' + ' · '.join(ctx['issues'])
            if 'J6' not in record['criteria']:
                record['criteria'].append('J6')
            record.setdefault('not_requested', []).append('J6')
            if record.get('decisions') is None:
                record['decisions'] = {}
            record['decisions']['J6'] = {'criterion_id': 'J6', 'name': next(c.name for c in CRITERIA if c.id == 'J6'),
                'decision': None, 'status': 'UNVERIFIED', 'confidence': None, 'probabilities': {},
                'reason': reason, 'context_checks': []}
            record['review_required'] = True
            applicability = record['applicability']
            applicability['criteria'] = list(record['criteria'])
            applicability.setdefault('not_applicable', {}).pop('J6', None)
            applicability.setdefault('evidence', {})['J6'] = {'source': 'conversation', 'status': 'UNVERIFIED',
                'rules': ['대화 이력 검증'], 'matches': list(ctx['issues']), 'excerpt': reason,
                'look_for': '앞 턴 누락·중복·입력 오류로 긴 대화 문맥을 확인할 수 없음'}
        records[i] = record
        if on_record:
            on_record(i, record)

    for i, record in enumerate(records):
        if record is not None:
            publish(i, record)
    if client is None or len(targets) <= 1:
        for done, i in enumerate(targets, 1):
            publish(i, run(i))
            if progress:
                progress(done, len(targets))
    else:
        limit = max(1, min(workers, len(targets)))
        # Keep only one worker window queued, so stopping a long batch does not
        # leave hundreds of external calls waiting in the executor.
        with ThreadPoolExecutor(max_workers=limit) as pool:
            futures = {}
            next_target = done = 0
            while futures or next_target < len(targets):
                cancelled = cancel_event is not None and cancel_event.is_set()
                if cancelled:
                    for future, i in list(futures.items()):
                        if future.cancel():
                            publish(i, _record(cases[i]["case_id"], JevStatus.SKIPPED,
                                               tuple(applies[i]["criteria"]), error=CANCEL_REASON))
                            del futures[future]
                            done += 1
                            if progress:
                                progress(done, len(targets))
                    while next_target < len(targets):
                        i = targets[next_target]
                        next_target += 1
                        publish(i, _record(cases[i]["case_id"], JevStatus.SKIPPED,
                                           tuple(applies[i]["criteria"]), error=CANCEL_REASON))
                        done += 1
                        if progress:
                            progress(done, len(targets))
                else:
                    while len(futures) < limit and next_target < len(targets):
                        i = targets[next_target]
                        next_target += 1
                        futures[pool.submit(run, i)] = i
                if not futures:
                    continue
                ready, _ = wait(futures, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in ready:
                    publish(futures.pop(future), future.result())
                    done += 1
                    if progress:
                        progress(done, len(targets))

    final: List[Dict[str, Any]] = [r for r in records if r is not None]
    statuses: Dict[str, int] = {}
    for r in final:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    # targets = 이 기준을 적용한 질문(턴) 수 (jev_applicability 결과)
    summary = {c.id: {"name": c.name, "targets": 0, "pass": 0, "fail": 0, "unverified": 0, "pending": 0} for c in CRITERIA}
    for r in final:
        for cid in r["criteria"]:
            summary[cid]["targets"] += 1
        for cid, d in (r["decisions"] or {}).items():
            if d.get('decision') in ('PASS', 'FAIL'):
                summary[cid]["pass" if d["decision"] == "PASS" else "fail"] += 1
            elif d.get('status') == 'UNVERIFIED':
                summary[cid]['unverified'] += 1
    for part in summary.values():
        part['pending'] = part['targets'] - part['pass'] - part['fail']
    return {
        "version": JEV_EVAL_VERSION,
        "engine": engine,
        "engine_label": ENGINE_LABELS.get(engine, engine),
        "history_policy": "full_original_text",
        "criteria_version": JEV_CRITERIA_VERSION,
        "decision_policy": copy.deepcopy(DECISION_POLICY),
        "criteria": [{**c, "applies_when": '모든 답변에서 사실 주장 여부를 먼저 확인하며, 사실이 없는 답변은 해당 없음' if fact_screening and c['id'] == 'J5'
                      else APPLIES_WHEN[c["id"]]} for c in describe()["criteria"]],
        "source_digest": batch.get("result_digest"),
        "client": config.to_manifest() if config is not None else None,
        "status": _overall(final),
        "access_reason": access_reason if client is None else None,
        "targets": len(targets),
        "completed": sum(r['status'] == JevStatus.SUCCESS and not r.get('review_required') for r in final),
        "review_required": sum(bool(r.get('review_required')) for r in final),
        "unverified": sum(s['unverified'] for s in summary.values()),
        "statuses": statuses,
        "models": sorted({r["model"] for r in final if r["model"]}),
        "applicability_version": applicability_version,
        "summary": summary,
        "conversations": conversations_summary(cases, final, contexts),
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "records": final,
    }


def jev_after_code(batch: Dict[str, Any], resolver: Optional[Callable] = None,
                   progress: Optional[Callable[[int, int], None]] = None,
                   engine: Optional[str] = None,
                   on_record: Optional[Callable[[int, Dict[str, Any]], None]] = None,
                   on_start: Optional[Callable[[int], None]] = None,
                   workers: int = DEFAULT_WORKERS, on_event: Optional[Callable] = None,
                   cancel_event: Optional[Event] = None) -> Dict[str, Any]:
    """CODE 평가 직후 실행한다. 키가 없으면 호출 없이 NOT_CONFIGURED 로 기록한다.

    ``engine`` 을 주면 SEMANTIC_ENGINE 설정 대신 그 엔진(jev | claude_cli | codex_cli | openai_api | anthropic_api)을 쓴다.
    """
    if cancel_event is not None and cancel_event.is_set():
        client, config, reason = None, None, CANCEL_REASON
    else:
        client, config, reason = (resolver or resolve_jev_client)(**({"engine": engine} if engine else {}))
    return evaluate_jev_batch(batch, client, config, reason, progress=progress,
                              on_record=on_record, on_start=on_start, workers=workers, on_event=on_event,
                              cancel_event=cancel_event)


def evaluation_log(batch: Dict[str, Any]) -> List[Dict[str, Any]]:
    """운영·분석용 질문별 기록. 인증값은 담지 않는다 (오류 문구도 가린 값만 쓴다)."""
    jev = batch.get("jev_eval") or {}
    records = jev.get("records") or []
    rows = []
    for i, case in enumerate(batch["cases"]):
        record = records[i] if i < len(records) else None
        summary = case.get("summary") or {}
        code_ms = case.get("timing_ms")
        jev_ms = record.get("latency_ms") if record else None
        rows.append({
            "run_id": batch.get("run", {}).get("run_id"),
            "case_id": case["case_id"],
            "conversation": case.get("conversation"),
            "code": {
                "case_status": case.get("case_status"),
                "overall": summary.get("overall"),
                "pass": (summary.get("common") or {}).get("pass"),
                "fail": (summary.get("common") or {}).get("fail"),
                "unjudged": (summary.get("common") or {}).get("unjudged"),
                "specialized_fail": (summary.get("specialized") or {}).get("fail"),
                "fail_checks": [r["check_id"] for r in case.get("results", [])
                                if r.get("counted") and r.get("verdict") == "FAIL"],
                "duration_ms": code_ms,
            },
            "jev": None if record is None else {
                "status": record["status"],
                "criteria": record.get("criteria") or [],
                "decisions": {cid: {k: copy.deepcopy(d[k]) for k in ('decision', 'confidence', 'probabilities', 'status',
                                                                    'reason', 'fact_check', 'context_checks',
                                                                    'reason_code', 'decision_policy',
                                                                    'policy_original_assessment', 'model_original_assessment') if k in d}
                              for cid, d in (record["decisions"] or {}).items()},
                "decision_policy": copy.deepcopy(record.get('decision_policy')),
                "j5_basis": record.get("j5_basis"),
                "verification": copy.deepcopy(record.get('verification')),
                "review_required": bool(record.get('review_required')),
                "history_turns": (record.get("context") or {}).get("history_turns"),
                "model": record["model"],
                "duration_ms": jev_ms,
                "attempts": record["attempts"],
                "error": redact(record["error"]),
            },
            # 옛 기록 호환 — 새 레코드에는 code_completion 이 없어 항상 None
            "code_completion": None if record is None else record.get("code_completion"),
            "total_ms": round((code_ms or 0) + (jev_ms or 0), 2),
        })
    return rows
