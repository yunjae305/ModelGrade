"""J6·J7 규칙 비교 — 자기 답변으로 재는 J6 는 모델마다 갈릴 수 있고, 같은 시나리오(질문 + 기준 답변)로 재면 같다.
J7 은 토큰 기준(사용자 한 턴·대화 누적·제공자 집계·평가자 요청)을 나눠 센다 (모델 호출 없음)."""
import pytest

from code_eval import long_context
from tools.j6j7_rules import j6, j7


def _long(n):
    text = "회의록 항목 번호와 담당자 이름을 기록한다. "
    return text * (n // long_context.count_tokens(text) + 1)


def _run():
    def rows(system, answer):
        return [{"case_id": f"C_T{t}", "conversation_id": "C", "turn": t, "question": "이어서 정리해줘.", "answer": answer,
                 "collection": {"raw": {"usage": {"input_tokens": 100 * t}}}} for t in range(1, 4)]
    return {"run_id": "r", "datasets": {"aiona": rows("aiona", "짧게."), "claude": rows("claude", _long(400)), "gpt": rows("gpt", "짧게.")},
            "evaluations": {"claude": {"jev_eval": {"records": [{"verification": {"usage_requests": [
                {"usage": {"input_tokens": 15_000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}]}}]}}}}


@pytest.mark.multi_turn_rule
def test_own_answer_measure_diverges_while_shared_scenario_does_not():
    report = j6(_run(), min_turns=3, total_tokens=900)   # 3턴 · 대화 전체 900 토큰으로 낮춰 같은 계산
    rules = report["rules"]
    assert rules["claude"]["current"] == {"conversations": 1, "turns": 2}     # 긴 답변을 쓴 모델만 멀티턴
    assert rules["gpt"]["current"] == {"conversations": 0, "turns": 0}
    assert all(r["two_plus"] == {"conversations": 1, "turns": 2} for r in rules.values())
    assert {r["stress_shared"]["conversations"] for r in rules.values()} == {0}   # 같은 시나리오 기준 — 모두 같은 판정
    assert report["divergent_conversations"] == [{"conversation": "C", "decisions": {"aiona": False, "claude": True, "gpt": False}}]


def test_j7_counts_each_token_basis_separately():
    report = j7(_run())
    bases = report["bases"]
    assert report["applies_on"] == "user_turn" and report["attachments_counted"] is False
    assert bases["user_turn"]["claude"]["max"] < 20                                   # 사용자 질문은 짧다
    assert bases["conversation"]["claude"]["max"] > bases["conversation"]["gpt"]["max"]  # 누적 요청은 자기 답변 길이만큼 커진다
    assert bases["provider_collection"]["gpt"]["max"] == 300
    assert bases["judge_request"]["claude"] == {"n": 1, "max": 15_000, "median": 15_000, "v1 8K": 1,
                                                "v2 250K (테스트 기간)": 0, "현재 272K": 0}
