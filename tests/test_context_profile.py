"""긴 입력·멀티턴 프로필 — 일반 멀티턴과 스트레스 멀티턴을 따로 세고, J7 8K 규칙과 272K 경계 실험을 나누며, 측정 불가는 상태로 남긴다."""
import pytest

from code_eval import long_context
from tools.context_profile import main, profile, provider_input_tokens


def _long(tokens):
    text = "회의록 항목 번호와 담당자 이름을 기록한다. "
    unit = long_context.count_tokens(text)
    return text * (tokens // unit + 1)


@pytest.mark.multi_turn_rule   # J6 길이 조건을 실제 값(10K 토큰 턴 10개)으로
def test_stress_conversation_is_counted_apart_from_ordinary_multi_turn():
    big = _long(10_500)
    stress = [{"conversation_id": "S", "turn": t, "question": big, "answer": "확인했습니다."} for t in range(1, 11)]
    small = [{"conversation_id": "M", "turn": t, "question": "일정을 정리해줘.", "answer": "네."} for t in (1, 2)]
    huge = {"case_id": "L", "question": _long(long_context.MIN_TOKENS + 1_000)}
    report = profile(stress + small + [{"case_id": "Q", "question": "안녕?"}, huge])
    counts = report["counts"]
    assert (counts["units"], counts["single"], counts["conversations_2plus"]) == (4, 2, 2)
    assert (counts["j6_multi_turn"], counts["stress_multi_turn"]) == (1, 1)   # 2턴짜리 짧은 대화는 2턴 이상이지만 현재 J6 규칙·스트레스 모두 아님
    assert counts["long_context_j7_turns"] == 1 and sum(report["j7_rule"]["tiers"].values()) == 1   # 현재 J7 하한(코드 값) 그대로
    s = next(u for u in report["units"] if u["id"] == "S")
    assert s["stress_multi_turn"] and s["last_turn_request"] > 100_000 and s["turn_input_mean"] >= 10_000
    assert next(u for u in report["units"] if u["id"] == "M")["stress_multi_turn"] is False   # 2턴 대화도 J6 멀티턴이지만 스트레스는 아님
    assert report["tokenizer"].startswith("tiktoken o200k_base (tiktoken ")


def test_boundary_experiment_needs_known_model_limits_and_tokenizer_failure_is_a_state(monkeypatch, capsys, tmp_path):
    report = profile([{"question": "짧은 질문"}], models={"m": {"max_input": 400_000, "output": 32_000}})
    assert report["boundary_272k"] == {"250000": {"m": "runnable"}, "272000": {"m": "runnable"}, "500000": {"m": "over_model_limit"}}
    assert profile([{"question": "짧은 질문"}])["boundary_272k"]["272000"] == {"_": "limit_unknown"}

    def broken(_text):
        raise long_context.TokenizerUnavailable("사전 없음")
    monkeypatch.setattr(long_context, "count_tokens", broken)
    failed = profile([{"conversation_id": "C", "turn": t, "question": "q"} for t in (1, 2)])
    assert failed["status"].startswith("tokenizer_unavailable") and failed["max_request_tokens_estimate"] is None
    assert failed["counts"]["long_context_j7_turns"] == 0 and "turn_input_max" not in failed["units"][0]   # 0개 '적용 없음'이 아니라 측정 안 함
    assert failed["units"][0]["j6"].startswith("멀티턴 여부를 판별하지 못함")
    with pytest.raises(SystemExit):
        main(["--model", "gpt=abc"])


def test_provider_reported_input_tokens_per_api_shape():
    assert provider_input_tokens({"collection": {"raw": {"usage": {"input_tokens": 256}}}}) == 256
    assert provider_input_tokens({"collection": {"raw": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 90,
                                                                   "cache_creation_input_tokens": 5}}}}) == 105
    assert provider_input_tokens({"collection": {"raw": {"usageMetadata": {"promptTokenCount": 306}}}}) == 306
    assert provider_input_tokens({"collection": {}}) is None
