# -*- coding: utf-8 -*-
"""1차 심판 투표(judge_v4.merge_votes · first_judge_votes, 10-25) — 다수결 · 동률 · 평균 확률 · 일부 실패."""
from tools import judge_v4 as jv


def _body(**choices):
    return {"answers": {k: v for k, v in choices.items()}, "usage": {"input_tokens": 10}}


def test_majority_and_average_probability():
    e = lambda c, pf, conf: {"choice": c, "probabilities": {"PASS": round(1 - pf, 2), "FAIL": pf}, "confidence": conf, "reason": f"{c} 사유"}
    bodies = [_body(a=e("FAIL", 0.8, 0.9)), _body(a=e("PASS", 0.2, 0.6)), _body(a=e("FAIL", 0.7, 0.8))]
    m = jv.merge_votes(bodies, ["a"])["a"]
    assert m["choice"] == "FAIL" and m["votes"] == ["FAIL", "PASS", "FAIL"] and m["vote_split"] is True
    assert abs(m["probabilities"]["FAIL"] - 0.567) < 0.01 and abs(m["confidence"] - 0.767) < 0.01 and m["reason"] == "FAIL 사유"


def test_tie_uses_highest_average_probability_and_missing_votes_are_skipped():
    e = lambda c, p: {"choice": c, "probabilities": p, "confidence": 0.5}
    bodies = [_body(a=e("PASS", {"PASS": 0.5, "FAIL": 0.3, "NOT_APPLICABLE": 0.2})), _body(a=e("FAIL", {"PASS": 0.2, "FAIL": 0.6, "NOT_APPLICABLE": 0.2})),
              _body(a=e("NOT_APPLICABLE", {"PASS": 0.3, "FAIL": 0.3, "NOT_APPLICABLE": 0.4}))]
    m = jv.merge_votes(bodies, ["a", "b"])
    assert m["a"]["choice"] == "FAIL" and m["a"]["vote_split"] is True   # 평균 확률 FAIL 0.4 가 가장 높음
    assert m["b"] is None


def test_first_judge_votes_calls_n_times_and_tolerates_failures(monkeypatch):
    calls = []

    def fake_first(part, questions, jev, decisions, use_jev):
        calls.append(1)
        if len(calls) == 2:
            return "ERROR", None, "429", "decisions"
        return "OK", {"answers": {"T1:J1-1.a": {"choice": "PASS", "probabilities": {"PASS": 0.9, "FAIL": 0.1}, "confidence": 0.9}}, "usage": {"input_tokens": 5}}, "", "decisions"
    monkeypatch.setattr(jv, "first_judge", fake_first)
    status, body, error, used, oks = jv.first_judge_votes({}, {"T1:J1-1.a": {}}, None, object(), use_jev=False, votes=3)
    assert status == "OK" and len(calls) == 3 and len(oks) == 2 and used == "decisions" and "429" in error
    assert body["answers"]["T1:J1-1.a"]["votes"] == ["PASS", "PASS"] and body["answers"]["T1:J1-1.a"]["vote_split"] is False
    status1, body1, _, _, oks1 = jv.first_judge_votes({}, {"T1:J1-1.a": {}}, None, object(), use_jev=False, votes=1)
    assert status1 == "OK" and len(oks1) == 1 and "votes" not in body1
