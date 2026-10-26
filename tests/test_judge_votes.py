# -*- coding: utf-8 -*-
"""1차 심판 투표(judge_v4.merge_votes · first_judge_votes, 10-25 · 판정 프롬프트 3종 stance_merge · stance_judge_votes, 10-26 저녁) — 다수결 · 동률 · 평균 확률 · 일부 실패."""
import json

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
    status, body, error, used, oks, attempts = jv.first_judge_votes({}, {"T1:J1-1.a": {}}, None, object(), use_jev=False, votes=3)
    assert status == "OK" and len(calls) == 3 and attempts == 3 and len(oks) == 2 and used == "decisions" and "429" in error
    assert [u for u, _ in oks] == ["decisions", "decisions"] and body["vote_judges"] == ["decisions", "decisions"]
    assert body["answers"]["T1:J1-1.a"]["votes"] == ["PASS", "PASS"] and body["answers"]["T1:J1-1.a"]["vote_split"] is False
    status1, body1, _, _, oks1, attempts1 = jv.first_judge_votes({}, {"T1:J1-1.a": {}}, None, object(), use_jev=False, votes=1)
    assert status1 == "OK" and len(oks1) == 1 and attempts1 == 1 and "votes" not in body1


def test_mixed_judges_keep_their_own_provenance(monkeypatch):
    seq = iter([("jev", "PASS"), ("decisions", "FAIL"), ("decisions", "FAIL")])

    def fake_first(part, questions, jev, decisions, use_jev):
        used, choice = next(seq)
        return "OK", {"answers": {"a": {"choice": choice, "probabilities": {"PASS": 0.5, "FAIL": 0.5}, "confidence": 0.5}}, "usage": {"input_tokens": 1}}, "", used
    monkeypatch.setattr(jv, "first_judge", fake_first)
    status, body, _, used, oks, attempts = jv.first_judge_votes({}, {"a": {}}, object(), object(), use_jev=True, votes=3)
    assert status == "OK" and used == "decisions" and attempts == 3 and [u for u, _ in oks] == ["jev", "decisions", "decisions"]
    assert body["answers"]["a"]["choice"] == "FAIL" and body["vote_judges"] == ["jev", "decisions", "decisions"]



STANCES = {"version": "t", "min_choice_prob": 0.6, "stances": [
    {"id": "critical", "name": "비판적", "text": "[비판적]", "weights": {"PASS": 1.2, "FAIL": 0.8, "NOT_APPLICABLE": 1.0}},
    {"id": "friendly", "name": "친화적", "text": "[친화적]", "weights": {"PASS": 0.8, "FAIL": 1.2, "NOT_APPLICABLE": 1.0}},
    {"id": "guided", "name": "유도적", "text": "[유도적]", "weights": {"PASS": 1.0, "FAIL": 1.0, "NOT_APPLICABLE": 1.0}}]}


def test_stance_merge_weights_each_answer_by_its_prompt_and_drops_unsure_votes():
    """판정 프롬프트 3종 (사용자 10-26 저녁) — 비판적의 PASS · 친화적의 FAIL 이 무겁고, 고른 값 확률 0.6 미만 표는 버린다."""
    e = lambda c, p: {"choice": c, "probabilities": {c: p, ("FAIL" if c == "PASS" else "PASS"): round(1 - p, 2)}, "confidence": p, "reason": f"{c} 사유"}
    st = STANCES["stances"]
    # 비판적 PASS(1.2) vs 친화적 FAIL(1.2) + 유도적 FAIL(1.0) → FAIL 2.2
    m = jv.stance_merge([(st[0], _body(a=e("PASS", 0.9))), (st[1], _body(a=e("FAIL", 0.8))), (st[2], _body(a=e("FAIL", 0.7)))], ["a"], STANCES)["a"]
    assert m["choice"] == "FAIL" and m["vote_score"] == {"PASS": 1.2, "FAIL": 2.2} and m["votes"] == ["PASS", "FAIL", "FAIL"] and m["vote_split"]
    assert [d["stance"] for d in m["vote_detail"]] == ["critical", "friendly", "guided"] and m["reason"] == "FAIL 사유"
    # 비판적 FAIL(0.8) vs 친화적 PASS(0.8) + 유도적 PASS 확률 0.5(버림) → 0.8 대 0.8 동률, 평균 확률이 높은 값
    m = jv.stance_merge([(st[0], _body(a=e("FAIL", 0.9))), (st[1], _body(a=e("PASS", 0.7))), (st[2], _body(a=e("PASS", 0.5)))], ["a"], STANCES)["a"]
    assert m["vote_score"] == {"FAIL": 0.8, "PASS": 0.8} and m["choice"] == "FAIL"   # 센 두 표의 평균 FAIL 0.6 > PASS 0.4
    assert [d["counted"] for d in m["vote_detail"]] == [True, True, False] and abs(m["confidence"] - 0.8) < 1e-9
    # 모두 0.6 미만이면 버리지 않고 모두 센다
    m = jv.stance_merge([(s_, _body(a=e("PASS", 0.55))) for s_ in st], ["a", "b"], STANCES)
    assert m["a"]["choice"] == "PASS" and all(d["counted"] for d in m["a"]["vote_detail"]) and m["b"] is None


def test_stance_judge_votes_sends_each_prompt_once_and_keeps_keys(monkeypatch):
    seen = []

    def fake_first(part, questions, jev, decisions, use_jev):
        seen.append((use_jev, questions["T1:J1-1.a"]["instructions"]))
        if len(seen) == 2:
            return "ERROR", None, "429", "decisions"
        return "OK", {"answers": {"T1:J1-1.a": {"choice": "PASS", "probabilities": {"PASS": 0.9, "FAIL": 0.1}, "confidence": 0.9}}, "usage": {"input_tokens": 5}}, "", "jev"
    monkeypatch.setattr(jv, "first_judge", fake_first)
    status, body, error, used, oks, attempts = jv.stance_judge_votes({}, {"T1:J1-1.a": {"instructions": "[J1-1.a] 정의"}}, object(), object(), True, cfg=STANCES)
    assert [i for _, i in seen] == ["[비판적]\n[J1-1.a] 정의", "[친화적]\n[J1-1.a] 정의", "[유도적]\n[J1-1.a] 정의"] and all(u for u, _ in seen)
    assert status == "OK" and attempts == 3 and len(oks) == 2 and used == "jev" and "친화적 ERROR 429" in error and body["stances"] == "t"
    assert body["answers"]["T1:J1-1.a"]["choice"] == "PASS" and [d["stance"] for d in body["answers"]["T1:J1-1.a"]["vote_detail"]] == ["critical", "guided"]


def test_stance_settings_file_has_three_prompts_with_weights():
    cfg = jv.first_stances(jv.STANCES_PATH)
    assert [s["name"] for s in cfg["stances"]] == ["비판적", "친화적", "유도적"] and cfg["min_choice_prob"] == 0.6
    assert all(set(s["weights"]) == {"PASS", "FAIL", "NOT_APPLICABLE"} and s["text"].startswith("[판정 태도") for s in cfg["stances"])

def _state(q, multi=False, images=None, plan=True):
    st = {"q": q, "provider": "gpt", "tag": f"Q{q} gpt", "flagged": {1: ["J2-3.a"]}, "tokens": {}, "counter": {"calls": 0},
          "status": None, "output": None, "error": "", "rounds": 0, "batch": None, "outside_seconds": 0.0,
          "codex_plan": ({"doc": {"질문번호": q, "턴": []}, "search": False, "images": images or [], "n_flagged": 1, "multi": multi} if plan else None)}

    def ask(doc):
        st["counter"]["calls"] += 1
        return "OK", {"turns": [{"turn": 1, "items": [{"id": "J2-3.a", "decision": "PASS"}]}], "conclusion": {"verdict": "PASS", "reason": "개별"}}, ""
    st["ask_codex"] = ask
    return st


def test_codex_batch_round_groups_plain_singles_and_leaves_multi_and_images_alone():
    calls = []

    class FakeGpt:
        last_usage = {"input_tokens": 100, "output_tokens": 10}

        def judge(self, prompt, text, search, tag="", output_schema=None, images=None):
            calls.append((json.loads(text)["질문_수"], search, "묶음 입력" in prompt))
            qs = [d["질문번호"] for d in json.loads(text)["질문들"]]
            return "OK", {"questions": [{"질문번호": q, "turns": [{"turn": 1, "items": []}], "conclusion": {"verdict": "PASS", "reason": "묶음"}} for q in qs[:-1]]}, ""   # 마지막 질문은 빠뜨림
    states = [_state(1), _state(2), _state(3), _state(4, multi=True), _state(5, images=["a.png"]), _state(6, plan=False)]
    jv.codex_batch_round(states, FakeGpt(), "PROMPT", size=10)
    assert calls == [(3, False, True)]                                   # 단일 3개만 한 번에, 멀티턴 · 첨부는 묶지 않음
    assert states[0]["counter"]["calls"] == 1 and states[0]["tokens"]["input_tokens"] == 100   # 묶음 호출 · 사용량은 대표 질문에
    assert states[1]["counter"]["calls"] == 0 and states[1]["output"]["conclusion"]["reason"] == "묶음"
    assert states[2]["counter"]["calls"] == 1 and states[2]["output"]["conclusion"]["reason"] == "개별"   # 묶음 답에 빠진 질문은 따로 물음
    assert states[3]["counter"]["calls"] == 1 and states[4]["counter"]["calls"] == 1 and states[5]["status"] is None
    assert all(st["batch"]["size"] == 3 for st in states[:3]) and states[0]["batch"]["lead"] and not states[1]["batch"]["lead"]


def test_first_call_alone_records_error_instead_of_raising():
    st = _state(7)
    def boom(doc):
        raise OSError("codex missing")
    st["ask_codex"] = boom
    jv.first_call_alone(st)
    assert st["status"] == "ERROR" and "OSError" in st["error"] and st["rounds"] == 1 and st["outside_seconds"] >= 0


def test_codex_batch_round_survives_batch_exception_and_attributes_time_to_lead():
    class Boom:
        last_usage = {}
        def judge(self, *a, **k):
            raise RuntimeError("down")
    states = [_state(1), _state(2)]
    for st in states:
        st["outside_seconds"] = 0.0
    jv.codex_batch_round(states, Boom(), "P", size=10)
    assert all(st["status"] == "OK" and st["output"]["conclusion"]["reason"] == "개별" for st in states)   # 묶음이 죽어도 질문마다 따로 받음
    assert states[0]["counter"]["calls"] == 2 and states[1]["counter"]["calls"] == 1   # 대표 질문: 묶음 1 + 개별 1


def test_batch_groups_split_by_count_and_by_chars_and_big_question_goes_alone():
    small = [_state(q) for q in (1, 2, 3)]
    big = _state(4)
    big["codex_plan"]["doc"] = {"질문번호": 4, "턴": [{"참조자료": "가" * 1000}]}   # 혼자서도 상한을 넘는 질문(롱컨텍스트)
    tail = _state(5)
    groups = jv.batch_groups(small + [big, tail], size=10, max_chars=500)
    assert [[st["q"] for st in g] for g in groups] == [[1, 2, 3], [4], [5]]   # 큰 질문은 혼자, 뒤 질문은 새 묶음
    assert all("chars" in st["codex_plan"] for st in small + [big, tail])
    by_count = jv.batch_groups([_state(q) for q in range(1, 6)], size=2, max_chars=10**6)
    assert [[st["q"] for st in g] for g in by_count] == [[1, 2], [3, 4], [5]]


def test_batch_chunks_keep_multi_turn_alone_and_group_singles():
    multi = {2, 5}
    got = jv.batch_chunks([1, 2, 3, 4, 5, 6, 7], lambda q: q in multi, size=3)
    assert got == [[1], [2], [3, 4], [5], [6, 7]]   # 멀티턴(2 · 5)은 혼자, 단일은 3개까지
    assert jv.batch_chunks([], lambda q: False, 10) == [] and jv.batch_chunks([9], lambda q: True, 10) == [[9]]
