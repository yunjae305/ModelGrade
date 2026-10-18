"""병행 판정(dual-judge-v1) — 두 심판 합치기 규칙. 모델·웹 호출 없음."""
from types import SimpleNamespace

from code_eval.dual_judge import DualJudgeClient, DualJudgeConfig, HOLD_REASON
from code_eval.jev_client import JevCall, JevStatus
from code_eval.jev_eval import parse_decisions


def entry(choice, reason="사유"):
    return {"choice": choice, "confidence": .9, "probabilities": {"PASS": .9, "FAIL": .1} if choice == "PASS" else {"PASS": .1, "FAIL": .9},
            "reason": reason}


class FakeLuna:
    supports_cancellation = True
    supports_images = True

    def __init__(self, first, again=None, fail=False):
        self.first, self.again, self.fail, self.calls = first, again, fail, []

    def system_one(self, state, questions, *, cancel_event=None, images=None):
        self.calls.append((dict(questions), images))
        if self.fail:
            return JevCall(JevStatus.API_ERROR, error="luna down", attempts=1)
        answers = self.first if len(self.calls) == 1 else (self.again or {})
        body = {"model": "gpt-6-luna", "answers": {cid: entry(answers[cid]) for cid in questions}, "verification": {"search_performed": False}}
        return JevCall("OK", body=body, attempts=1, latency_ms=5, usage={"input_tokens": 10, "output_tokens": 2},
                       accounting={"model": "gpt-6-luna", "cost_parts": [{"model": "gpt-6-luna", "usage": {"input_tokens": 10, "output_tokens": 2},
                                                                           "web_search_calls": 0, "service_tier": "default", "inference_geo": None}]})


class FakeJev:
    def __init__(self, answers, error=None):
        self.answers, self.error, self.calls = answers, error, []

    def system_one(self, state, questions):
        self.calls.append(dict(questions))
        if self.error:
            raise RuntimeError(self.error)
        return JevCall("OK", body={"model": "jev-1.13.0", "answers": {cid: entry(self.answers[cid]) for cid in questions}},
                       attempts=1, usage={"input_tokens": 3, "output_tokens": 1}, accounting={"model": "jev-1.13.0"})


def client(luna, jev):
    config = DualJudgeConfig(SimpleNamespace(model="gpt-6-luna", engine="openai_api"), SimpleNamespace(model="jev-latest", engine="jev"))
    return DualJudgeClient(luna, jev, config)


QUESTIONS = {cid: {"type": "choice"} for cid in ("J1", "J2", "J4", "J5")}
STATE = {"question": "q", "answer": "a"}


def test_agreement_is_final_and_jev_only_sees_j1_to_j4():
    luna, jev = FakeLuna({"J1": "PASS", "J2": "FAIL", "J4": "PASS", "J5": "PASS"}), FakeJev({"J1": "PASS", "J2": "FAIL", "J4": "PASS"})
    call = client(luna, jev).system_one(STATE, QUESTIONS, images=[{"name": "x"}])
    assert call.status == "OK" and len(luna.calls) == 1 and jev.calls == [{"J1": {"type": "choice"}, "J2": {"type": "choice"}, "J4": {"type": "choice"}}]
    note = call.body["verification"]["dual_judge"]
    assert note["agreed"] == ["J1", "J2", "J4"] and note["disputed"] == [] and note["held"] == [] and note["recheck"] is None
    assert call.body["model"] == "gpt-6-luna + jev-1.13.0"
    assert luna.calls[0][1] == [{"name": "x"}]   # 그림은 Luna 에게만 간다
    decisions = parse_decisions(call.body, tuple(QUESTIONS))
    assert {cid: d["decision"] for cid, d in decisions.items()} == {"J1": "PASS", "J2": "FAIL", "J4": "PASS", "J5": "PASS"}
    # 사용량·비용 부분은 두 심판 합 — Jev 는 요청 하나로
    assert call.usage == {"input_tokens": 13, "output_tokens": 3}
    assert [p["model"] for p in call.accounting["cost_parts"]] == ["gpt-6-luna", "jev-latest"] or \
           [p["model"] for p in call.accounting["cost_parts"]] == ["gpt-6-luna", "jev-1.13.0"]


def test_dispute_confirmed_by_recheck_keeps_luna_decision():
    luna = FakeLuna({"J1": "PASS", "J2": "FAIL", "J4": "PASS", "J5": "PASS"}, again={"J2": "FAIL"})
    call = client(luna, FakeJev({"J1": "PASS", "J2": "PASS", "J4": "PASS"})).system_one(STATE, QUESTIONS)
    note = call.body["verification"]["dual_judge"]
    assert note["disputed"] == ["J2"] and note["confirmed"] == ["J2"] and note["held"] == []
    assert luna.calls[1][0] == {"J2": {"type": "choice"}}   # 갈린 기준만 다시 묻는다
    assert call.body["answers"]["J2"]["choice"] == "FAIL"
    assert call.body["answers"]["J2"]["dual_judge"] == {"jev": "PASS", "luna": "FAIL", "recheck": "FAIL", "outcome": "confirmed_by_recheck"}
    assert call.attempts == 3   # 관측한 요청별 사용량 3개(Luna·Jev·재판정), 미관측 0


def test_dispute_with_changed_recheck_is_held_not_overwritten():
    luna = FakeLuna({"J1": "PASS", "J2": "FAIL", "J4": "FAIL", "J5": "PASS"}, again={"J2": "PASS", "J4": "FAIL"})
    call = client(luna, FakeJev({"J1": "PASS", "J2": "PASS", "J4": "PASS"})).system_one(STATE, QUESTIONS)
    note = call.body["verification"]["dual_judge"]
    assert note["disputed"] == ["J2", "J4"] and note["held"] == ["J2"] and note["confirmed"] == ["J4"]
    held = call.body["answers"]["J2"]
    assert held["choice"] == "UNVERIFIED" and held["probabilities"] == {} and held["reason"].startswith(HOLD_REASON)
    assert "Jev PASS · GPT-6 Luna FAIL · 재판정 PASS" in held["reason"]
    assert held["dual_judge"]["outcome"] == "held" and held["dual_judge"]["luna_original"]["choice"] == "FAIL"
    decisions = parse_decisions(call.body, tuple(QUESTIONS))
    assert decisions["J2"]["status"] == "UNVERIFIED" and decisions["J2"]["decision"] is None
    assert decisions["J2"]["dual_judge"]["outcome"] == "held" and decisions["J2"]["dual_judge"]["luna_original"]["choice"] == "FAIL"
    assert decisions["J4"]["decision"] == "FAIL" and decisions["J4"]["dual_judge"]["outcome"] == "confirmed_by_recheck"


def test_jev_failure_falls_back_to_luna_and_is_recorded():
    luna = FakeLuna({"J1": "PASS", "J2": "FAIL", "J4": "PASS", "J5": "PASS"})
    call = client(luna, FakeJev({}, error="boom")).system_one(STATE, QUESTIONS)
    note = call.body["verification"]["dual_judge"]
    assert call.status == "OK" and note["jev"]["status"] == JevStatus.API_ERROR and "boom" in note["jev"]["error"]
    assert note["disputed"] == [] and note["held"] == [] and len(luna.calls) == 1
    assert call.body["model"] == "gpt-6-luna"   # Jev 판정이 없으면 모델 이름도 Luna 만
    assert parse_decisions(call.body, tuple(QUESTIONS))["J2"]["decision"] == "FAIL"


def test_luna_failure_returns_error_without_calling_jev():
    jev = FakeJev({"J1": "PASS"})
    call = client(FakeLuna({}, fail=True), jev).system_one(STATE, {"J1": {"type": "choice"}})
    assert call.status == JevStatus.API_ERROR and jev.calls == []
    assert call.body["verification"]["dual_judge"]["skipped"] == "luna_failed"


def test_items_pipeline_uses_luna_only():
    jev = FakeJev({"J1": "PASS"})
    items = {"J1-1.a": {"type": "item", "criterion": "J1"}}
    luna = FakeLuna({"J1-1.a": "PASS"})
    call = client(luna, jev).system_one(STATE, items)
    assert call.status == "OK" and jev.calls == [] and call.body["verification"]["dual_judge"]["skipped"] == "items_pipeline"
