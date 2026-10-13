"""멀티턴 판별(multi-turn-v2) — 10턴 이상이고 대화 전체 10K 토큰 이상인 대화만 J6. 모델·네트워크 호출 없음."""
import pytest

from code_eval import engine, long_context, multi_turn
from code_eval.config import AionaConfig
from code_eval.jev_applicability import jev_applicability, sub_applicability
from code_eval.jev_eval import evaluate_jev_batch

pytestmark = pytest.mark.multi_turn_rule   # conftest 가 끄는 길이 조건을 실제 값으로 둔다


class _OneTokenPerChar:
    def encode(self, text, disallowed_special=()):
        return list(text)


@pytest.fixture(autouse=True)
def per_char(monkeypatch):
    long_context.count_tokens.cache_clear()
    monkeypatch.setattr(long_context, "_encoder", lambda: _OneTokenPerChar())
    yield
    long_context.count_tokens.cache_clear()


def conversation(turns, question_chars=500, answer_chars=500, cid="C1", answer_sizes=None):
    """턴마다 질문 question_chars 자 + 답변 answer_chars 자 — 한 턴 토큰 = 질문 + 줄바꿈 + 답변 (글자 1개 = 토큰 1개)."""
    rows = []
    for index in range(1, turns + 1):
        prefix = f"{index}턴 질문 "
        answer = (answer_sizes or {}).get(index, answer_chars)
        rows.append({"case_id": f"{cid}-T{index}", "conversation_id": cid, "turn": index,
                     "question": prefix + "가" * (question_chars - len(prefix)), "answer": "나" * answer})
    return rows


def test_ten_turns_with_10k_total_make_a_multi_turn_conversation():
    ten = multi_turn.measure_conversation([(t, "가" * 500, "나" * 499) for t in range(1, 11)])   # 턴마다 1,000 → 전체 10,000
    assert (ten["multi_turn"], ten["turns"], ten["tokens"]) == (True, 10, 10_000)
    light = multi_turn.measure_conversation([(t, "가" * 400, "나" * 400) for t in range(1, 13)])   # 12턴이어도 전체 9,612
    assert (light["multi_turn"], light["tokens"]) == (False, 9_612)
    few_big = multi_turn.measure_conversation([(t, "가" * 6_000, "나" * 20_000) for t in range(1, 10)])
    assert (few_big["multi_turn"], few_big["turns"]) == (False, 9)   # 전체가 커도 10턴 미만이면 아님
    assert multi_turn.describe(light) == "12턴 · 대화 전체 9,612토큰 (멀티턴은 10턴 이상 · 대화 전체 10,000토큰 이상)"


def test_j6_applies_only_to_multi_turn_conversations():
    short = {"id": "C1", "turn": 3, "history_turns": 2, "issues": [],
             "multi_turn": multi_turn.measure_conversation([(t, "질문", "답변") for t in range(1, 13)])}
    result = jev_applicability("앞에서 정한 대로 써줘", conversation=short)
    assert "J6" not in result["criteria"] and result["not_applicable"]["J6"].startswith("멀티턴 대화가 아님 — 12턴 · 대화 전체")
    assert sub_applicability("앞에서 정한 대로 써줘", conversation=short)["J6-1"]["items"] == []
    long = {**short, "multi_turn": multi_turn.measure_conversation([(t, "가" * 600, "나" * 400) for t in range(1, 11)])}
    assert "J6" in jev_applicability("앞에서 정한 대로 써줘", conversation=long)["criteria"]
    unknown = {**short, "multi_turn": {"policy": multi_turn.MULTI_TURN_POLICY_VERSION, "error": "멀티턴 여부를 판별하지 못함 — x"}}
    result = jev_applicability("앞에서 정한 대로 써줘", conversation=unknown)
    assert "J6" not in result["not_applicable"] and result["undetermined"]["J6"] == "멀티턴 여부를 판별하지 못함 — x"
    assert sub_applicability("앞에서 정한 대로 써줘", conversation=unknown)["J6-1"]["undetermined"] is True


def test_each_model_is_measured_on_its_own_answers_and_short_conversations_are_not_applicable():
    long_model = conversation(11)                                                  # 11턴 · 전체 11,011 — 멀티턴
    short_model = conversation(11, answer_sizes={t: 100 for t in range(1, 12)})    # 같은 질문, 짧은 답변 — 전체 6,611
    for rows, expected in ((long_model, True), (short_model, False)):
        jev = evaluate_jev_batch(engine.evaluate_batch(rows, AionaConfig()), None, access_reason="키 없음")
        asked = ["J6" in record["criteria"] for record in jev["records"]]
        assert asked == ([False] + [True] * 10 if expected else [False] * 11)
        conv = jev["conversations"]
        if expected:
            assert conv["not_applicable"] == 0 and conv["items"][0]["multi_turn"]["turns"] == 11
        else:
            assert (conv["not_applicable"], conv["incomplete"], conv["items"][0]["result"]) == (1, 0, "NOT_APPLICABLE")
            assert conv["items"][0]["issues"][0].startswith("멀티턴 대화가 아님")
            assert jev["records"][5]["applicability"]["not_applicable"]["J6"].startswith("멀티턴 대화가 아님")


def test_missing_turns_keep_j6_unverified_only_when_the_conversation_could_still_be_multi_turn():
    rows = conversation(10, answer_chars=400)            # 턴마다 901 → 10턴 전체 9,010
    gap = [row for row in rows if row["turn"] != 2]     # 2턴이 빠짐 — 빠진 턴이 길었다면 전체 10K·10턴을 채울 수 있음
    jev = evaluate_jev_batch(engine.evaluate_batch(gap, AionaConfig()), None, access_reason="키 없음")
    assert jev["records"][2]["decisions"]["J6"]["status"] == "UNVERIFIED"
    tiny = [row for row in conversation(5, 100, 100) if row["turn"] != 2]   # 짧은 대화 — 빠진 턴을 더해도 10턴이 안 됨
    jev = evaluate_jev_batch(engine.evaluate_batch(tiny, AionaConfig()), None, access_reason="키 없음")
    assert all("J6" not in record["criteria"] for record in jev["records"])
    assert jev["conversations"]["items"][0]["result"] == "NOT_APPLICABLE"


def test_without_tokenizer_multi_turn_is_undetermined_and_j6_is_unverified(monkeypatch):
    def unavailable():
        raise long_context.TokenizerUnavailable("tiktoken o200k_base 를 불러오지 못함: ImportError")
    monkeypatch.setattr(long_context, "_encoder", unavailable)
    jev = evaluate_jev_batch(engine.evaluate_batch(conversation(3, 150, 149), AionaConfig()), None, access_reason="키 없음")
    second = jev["records"][1]
    assert second["decisions"]["J6"]["status"] == "UNVERIFIED" and second["decisions"]["J6"]["reason"].startswith("멀티턴 여부를 판별하지 못함")


def test_duplicate_or_invalid_turns_never_count_as_definite_turns():
    rows = conversation(9)
    duplicate = rows + [dict(rows[8], case_id="C1-T9b")]   # 9번 턴이 겹침 — 어느 쪽인지 몰라 확실한 턴에서 뺀다
    jev = evaluate_jev_batch(engine.evaluate_batch(duplicate, AionaConfig()), None, access_reason="키 없음")
    scale = jev["conversations"]["items"][0]["multi_turn"]
    assert (scale["turns"], scale["uncertain_turns"], scale["multi_turn"]) == (8, 1, False)   # 9턴까지만 가능 → 아님
    invalid = rows + [{**conversation(10)[9], "question": ""}]   # 10번 턴 입력 오류 — 그 턴이 있었다면 10턴·10K 를 채울 수 있음
    jev = evaluate_jev_batch(engine.evaluate_batch(invalid, AionaConfig()), None, access_reason="키 없음")
    scale = jev["conversations"]["items"][0]["multi_turn"]
    assert (scale["turns"], scale["uncertain_turns"], scale["multi_turn"]) == (9, 1, None)   # 모름
    second = jev["records"][1]   # 2턴 — 앞 이력은 완전하지만 대화 전체가 멀티턴인지 모르므로 확인 필요
    assert second["decisions"]["J6"]["status"] == "UNVERIFIED" and second["decisions"]["J6"]["reason"].startswith("멀티턴 여부를 판별하지 못함")
    assert jev["conversations"]["items"][0]["result"] == "INCOMPLETE"
