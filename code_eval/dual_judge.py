"""병행 판정 — Jev(TypeSafe System One)와 GPT-6 Luna(OpenAI API) 두 심판으로 J1~J4 를 교차 확인한다 (사용자 결정 2026-10-18).

규칙
- 모든 기준은 GPT-6 Luna 가 판정한다(사유·J5 사실 확인·J6~J8 포함). Jev 는 J1~J4 만 함께 판정한다.
- J1~J4: 두 심판이 같으면 확정. 다르면 Luna 에 그 기준만 한 번 더 묻는다 — 재판정이 처음 Luna 판정과 같으면 확정,
  다르면 **보류**(UNVERIFIED — 사람이 PASS/FAIL 을 정한다). 어느 쪽도 다른 쪽을 덮어쓰지 않는다.
- Jev 가 답하지 못하면(한도 초과·오류·키 없음) Luna 판정만으로 확정하고 그 사실을 기록한다. items 방식은 Jev 가 받지 않으므로 Luna 만 쓴다.
- Jev 는 Luna 1차 판정과 동시에 부른다(2026-10-19). Luna 가 실패하면 Jev 판정은 버리고 사용량만 남긴다.
- 결과 body 는 Luna 의 것(answers·verification)에 ``verification.dual_judge`` 를 더한 것이다. 사용량·비용 부분은 두 심판 것을 합친다.

근거(통제 실험 300문항 × 3모델): J1~J4 에서 두 심판이 갈린 항목 184/2,366. 갈린 항목은 Luna 가 같은 규칙으로 다시 봐도 26% 가
바뀌는 경계 사례라, 규칙 문구를 고치는 것(갈림 184→171)보다 재판정 + 보류가 맞다. 이 방식의 보류는 모델마다 3~5/300 이다.
"""
from __future__ import annotations

import copy
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from .jev_client import JevCall, JevStatus, _sum_usage
from .llm_client import redact
from .semantic_cli_common import is_items

__all__ = ["DUAL_JUDGE_VERSION", "JEV_CRITERIA", "DualJudgeConfig", "DualJudgeClient"]

DUAL_JUDGE_VERSION = "dual-judge-v1"
#: Jev 가 함께 보는 기준 — J5~J8 은 출처 확인·긴 입력·그림이 필요해 Luna 만 본다
JEV_CRITERIA: Tuple[str, ...] = ("J1", "J2", "J3", "J4")
_BINARY = ("PASS", "FAIL")
HOLD_REASON = "심판 갈림 — 사람이 확인해 PASS/FAIL 을 정합니다."


@dataclass(frozen=True)
class DualJudgeConfig:
    """두 심판 설정을 묶은 것. 비용 집계는 호출마다 남긴 요청별 사용량(cost_parts)의 모델 이름으로 한다."""

    luna: Any
    jev: Any
    engine: str = field(default="dual", init=False)
    dispatch: str = field(default="realtime", init=False)   # 재판정이 필요해 Message Batches 는 쓰지 않는다

    @property
    def model(self) -> str:
        return f"{getattr(self.luna, 'model', '?')} + {getattr(self.jev, 'model', '?')}"

    def to_manifest(self) -> Dict[str, Any]:
        return {"engine": self.engine, "policy": DUAL_JUDGE_VERSION, "jev_criteria": list(JEV_CRITERIA), "jev_timing": "concurrent",
                "luna": self.luna.to_manifest() if callable(getattr(self.luna, "to_manifest", None)) else None,
                "jev": self.jev.to_manifest() if callable(getattr(self.jev, "to_manifest", None)) else None,
                "credentials_recorded": False}


def _answers(call: Optional[JevCall]) -> Dict[str, Any]:
    body = call.body if call is not None and isinstance(call.body, dict) else {}
    answers = body.get("answers")
    return answers if isinstance(answers, dict) else {}


def _choice(answers: Dict[str, Any], cid: str) -> Optional[str]:
    entry = answers.get(cid)
    return entry.get("choice") if isinstance(entry, dict) else None


def _usage(call: Optional[JevCall]) -> Dict[str, Any]:
    """호출의 누적 사용량 — 재시도까지 더한 accounting.usage 가 있으면 그것, 없으면 마지막 응답의 usage."""
    accounting = (call.accounting or {}) if call is not None else {}
    usage = accounting.get("usage")
    return copy.deepcopy(usage if isinstance(usage, dict) and usage else (call.usage or {}) if call is not None else {})


def _parts(call: Optional[JevCall], fallback_model: Any) -> list:
    """비용 집계용 요청별 사용량 — API 클라이언트는 cost_parts 를, Jev 는 누적 사용량 하나(미관측 시도 수 포함)를 낸다."""
    if call is None:
        return []
    accounting = call.accounting or {}
    parts = accounting.get("cost_parts")
    if isinstance(parts, list):
        return copy.deepcopy(parts)
    usage = _usage(call)
    if not usage:   # 사용량을 못 받은 호출(시간 초과·오류·시작 전 중단)은 part 없이 미관측 시도로만 센다 — 중복 집계 방지
        return []
    return [{"model": accounting.get("model") or fallback_model, "usage": usage,
             "web_search_calls": None, "service_tier": None, "inference_geo": None}]


def _unreported(call: JevCall, observed: int) -> int:
    """이 호출에서 사용량이 남지 않은 시도 수 — accounting 이 말하면 그 값, 아니면 시도 수에서 관측한 part 수를 뺀 것."""
    accounting = call.accounting or {}
    if isinstance(accounting.get("unreported_attempts"), int) and "cost_parts" not in accounting and observed:
        return max(0, int(accounting["unreported_attempts"]))
    return max(0, int(call.attempts or 0) - observed)


class DualJudgeClient:
    """``system_one`` 인터페이스로 두 심판을 돌리고 합친 JevCall 을 돌려준다. 그림 지원은 Luna 를 따른다."""

    supports_cancellation = True

    def __init__(self, luna: Any, jev: Any, config: DualJudgeConfig) -> None:
        self.luna, self.jev, self.config = luna, jev, config

    @property
    def supports_images(self) -> bool:
        return bool(getattr(self.luna, "supports_images", False))

    def _luna(self, state: Any, questions: Dict[str, Any], cancel_event: Any, images: Any) -> JevCall:
        kwargs: Dict[str, Any] = {}
        if cancel_event is not None and getattr(self.luna, "supports_cancellation", False):
            kwargs["cancel_event"] = cancel_event
        if images and self.supports_images:
            kwargs["images"] = images
        return self.luna.system_one(state, questions, **kwargs)

    def _jev(self, state: Any, questions: Dict[str, Any]) -> JevCall:
        try:
            return self.jev.system_one(state, questions)
        except Exception as exc:   # Jev 오류는 이 질문의 Jev 판정 없음으로만 남긴다 — Luna 판정은 그대로
            return JevCall(JevStatus.API_ERROR, error=redact(f"{type(exc).__name__}: {exc}"))

    @staticmethod
    def _cancelled(cancel_event) -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def system_one(self, state: Any, questions: Dict[str, Any], *, cancel_event=None, images=None) -> JevCall:
        started = time.monotonic()
        note: Dict[str, Any] = {"policy": DUAL_JUDGE_VERSION, "jev_criteria": [], "jev": None,
                                "agreed": [], "disputed": [], "recheck": None, "confirmed": [], "held": []}
        asked = [cid for cid in JEV_CRITERIA if cid in questions]
        jev_questions = {cid: questions[cid] for cid in asked} if asked and not is_items(questions) else None
        if jev_questions is None:
            first, jev_call = self._luna(state, questions, cancel_event, images), None
        else:
            # Jev 는 Luna 1차 판정과 동시에 부른다 — 둘은 서로의 결과를 쓰지 않으므로 판정은 같고 기다리는 시간만 준다 (사용자 결정 2026-10-19).
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="dual-jev") as pool:
                pending = pool.submit(self._jev, state, jev_questions)
                first = self._luna(state, questions, cancel_event, images)
                jev_call = pending.result()
        calls = [first] if jev_call is None else [first, jev_call]   # calls[1] 이 Jev — Luna 가 실패해도 이미 부른 Jev 사용량은 남긴다
        if first.status != "OK" or jev_call is None:
            note["skipped"] = ("luna_failed" if first.status != "OK" else "items_pipeline" if is_items(questions) else "no_jev_criteria")
            if jev_call is not None:
                note["jev_discarded"] = {"status": jev_call.status}   # Luna 실패 — Jev 판정은 쓰지 않는다
            return self._finish(first, calls, note, started)
        if self._cancelled(cancel_event):   # 중단 요청 뒤에는 어느 판정도 확정하지 않는다 — 사용량만 남긴다 (Codex 검토)
            return self._finish(self._skipped(first), calls, dict(note, skipped="cancelled"), started)
        note["jev_criteria"] = asked
        jev_answers = _answers(jev_call)
        note["jev"] = {"status": jev_call.status, "error": redact(jev_call.error) if jev_call.error else None,
                       "model": (jev_call.body or {}).get("model") if isinstance(jev_call.body, dict) else None,
                       "answers": {cid: {k: jev_answers[cid].get(k) for k in ("choice", "confidence")}
                                   for cid in asked if isinstance(jev_answers.get(cid), dict)}}
        luna_answers = _answers(first)
        if jev_call.status != "OK":
            return self._finish(first, calls, note, started)
        disputed = []
        for cid in asked:
            a, b = _choice(luna_answers, cid), _choice(jev_answers, cid)
            if a in _BINARY and b in _BINARY:
                (note["agreed"] if a == b else disputed).append(cid)
        note["disputed"] = disputed
        if not disputed:
            return self._finish(first, calls, note, started)
        again = self._luna(state, {cid: questions[cid] for cid in disputed}, cancel_event, images)
        calls.append(again)
        if again.status == JevStatus.SKIPPED or self._cancelled(cancel_event):   # 재판정 중 중단 — 갈린 기준을 Luna 판정으로 확정하지 않는다
            note["recheck"] = {"status": JevStatus.SKIPPED, "error": again.error, "answers": {}}
            return self._finish(self._skipped(first), calls, dict(note, skipped="cancelled"), started)
        again_answers = _answers(again)
        note["recheck"] = {"status": again.status, "error": redact(again.error) if again.error else None,
                           "answers": {cid: {k: again_answers[cid].get(k) for k in ("choice", "confidence")}
                                       for cid in disputed if isinstance(again_answers.get(cid), dict)}}
        body = copy.deepcopy(first.body)
        for cid in disputed:
            a, b, c = _choice(luna_answers, cid), _choice(jev_answers, cid), _choice(again_answers, cid)
            if again.status == "OK" and c == a:
                note["confirmed"].append(cid)
                body["answers"][cid]["dual_judge"] = {"jev": b, "luna": a, "recheck": c, "outcome": "confirmed_by_recheck"}
                continue
            original = copy.deepcopy(body["answers"][cid])
            reason = (f"{HOLD_REASON} Jev {b} · GPT-6 Luna {a} · 재판정 "
                      f"{c if again.status == 'OK' and c else '실패'}. 처음 사유: {original.get('reason') or '(없음)'}")
            body["answers"][cid] = {**{k: v for k, v in original.items() if k not in ("choice", "confidence", "probabilities", "reason")},
                                    "choice": "UNVERIFIED", "confidence": 0.0, "probabilities": {}, "reason": reason,
                                    "dual_judge": {"jev": b, "luna": a, "recheck": c if again.status == "OK" else None,
                                                   "outcome": "held", "luna_original": original}}
            note["held"].append(cid)
        merged = JevCall("OK", body=body, attempts=first.attempts, latency_ms=first.latency_ms, http_status=first.http_status,
                         usage=copy.deepcopy(first.usage), accounting=copy.deepcopy(first.accounting))
        return self._finish(merged, calls, note, started)

    @staticmethod
    def _skipped(first: JevCall) -> JevCall:
        """중단된 병행 판정 — 판정(answers)은 버리고 verification 만 남긴 SKIPPED. jev_eval 이 CANCEL 사유의 SKIPPED 레코드로 적는다."""
        verification = (first.body or {}).get("verification") if isinstance(first.body, dict) else None
        return JevCall(JevStatus.SKIPPED, body={"verification": copy.deepcopy(verification) if isinstance(verification, dict) else {}},
                       attempts=first.attempts, latency_ms=first.latency_ms, http_status=first.http_status,
                       error="평가 중단 요청으로 병행 판정을 종료했습니다.", usage=copy.deepcopy(first.usage), accounting=copy.deepcopy(first.accounting))

    def _finish(self, base: JevCall, calls: list, note: Dict[str, Any], started: float) -> JevCall:
        """합친 결과 — body 는 base(Luna)의 것, 사용량·비용 부분·시도 횟수는 모든 호출(Luna·Jev·재판정)의 합."""
        body = copy.deepcopy(base.body) if isinstance(base.body, dict) else {}
        body.setdefault("verification", {})["dual_judge"] = note
        luna_model = body.get("model") if isinstance(body.get("model"), str) else getattr(self.config.luna, "model", "?")
        if note.get("jev") and note["jev"].get("status") == "OK":
            body["model"] = f"{luna_model} + {note['jev'].get('model') or getattr(self.config.jev, 'model', 'jev')}"
        usage: Dict[str, Any] = {}
        parts: list = []
        unreported = 0
        unreported_by_model: Dict[str, int] = {}
        for position, call in enumerate(calls):
            _sum_usage(usage, _usage(call))
            model = self.config.jev.model if position == 1 else self.config.luna.model   # calls[1] 이 Jev
            mine = _parts(call, model)
            parts.extend(mine)
            missing = _unreported(call, len(mine))
            unreported += missing
            if missing:   # 심판별로 남긴다 — 장부가 Jev 의 미확인 요청을 Luna 행에 적지 않게 (Codex 검토)
                unreported_by_model[str((call.accounting or {}).get("model") or model)] = unreported_by_model.get(str((call.accounting or {}).get("model") or model), 0) + missing
        body["verification"]["usage_requests"] = copy.deepcopy(parts)
        # 실제 HTTP 시도 횟수는 여기 남긴다 — 아래 JevCall.attempts 는 비용 장부용(관측 part + 미관측 시도)이라 다르다
        note["attempts"] = {"luna": calls[0].attempts, "jev": calls[1].attempts if len(calls) > 1 else 0,
                            "recheck": calls[2].attempts if len(calls) > 2 else 0}
        accounting = {**copy.deepcopy(base.accounting or {}), "cost_parts": parts, "usage": usage,
                      "model": (base.accounting or {}).get("model") or luna_model, "unreported_attempts": unreported,
                      "unreported_by_model": unreported_by_model}
        # attempts = 관측한 요청별 사용량 수 + 미관측 시도 수 — Jev 의 재시도는 누적 사용량 하나(part)로 이미 들어 있어 ledger 가
        # 'attempts - len(parts)' 로 다시 미확인 요청을 세지 않게 한다 (Codex 검토)
        return JevCall(base.status, body=body, attempts=len(parts) + unreported,
                       latency_ms=int((time.monotonic() - started) * 1000), http_status=base.http_status,
                       error=base.error, usage=usage, accounting=accounting)
