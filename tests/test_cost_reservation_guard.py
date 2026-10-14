"""유료 호출 상한 — 호출마다 예약금을 잡아 '쓴 금액 + 진행 중 예약 + 새 예약 ≤ 상한' 일 때만 시작한다 (모델 호출 없음, 가짜 사용량)."""
import threading
from decimal import Decimal

from code_eval.jev_client import JevCall
from tests.live_stage3_validation import GuardedClient


class _Priced:
    """요청마다 정해진 비용이 나오도록 Opus 5.5 단가의 사용량을 돌려준다 (출력 토큰 $20/M)."""

    def __init__(self, costs):
        self.costs, self.calls = list(costs), 0

    def system_one(self, state, questions, *, cancel_event=None):
        usd = self.costs[self.calls]
        self.calls += 1
        usage = {"input_tokens": 0, "output_tokens": int(Decimal(str(usd)) * 1_000_000 / 20), "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0, "service_tier": "standard", "inference_geo": "global"}
        return JevCall("OK", body={"answers": {}}, attempts=1,
                       accounting={"cost_parts": [{"usage": usage, "web_search_calls": 0, "model": "claude-opus-5-5",
                                                   "service_tier": "standard", "inference_geo": "global"}]})


def _guard(tmp_path, client, **caps):
    index = {(f"q{i}", "a"): f"C{i}" for i in range(10)}
    return GuardedClient(client, type("Cfg", (), {"model": "claude-opus-5-5"})(), tmp_path, index, set(index.values()),
                         stop=threading.Event(), **caps)


def test_reservation_keeps_total_spend_under_the_cap(tmp_path):
    client = _Priced([0.9] * 10)
    guard = _guard(tmp_path, client, max_usd=3.0, max_calls=10, reserve_usd=1.0)
    statuses = [guard.system_one({"question": f"q{i}", "answer": "a"}, {"J1": {}}).status for i in range(10)]
    # 0.9 씩 쓰면: 0 → 0.9 → 1.8 까지는 1.0 예약이 들어가고(1.8+1.0 ≤ 3.0), 2.7 에서는 새 호출을 시작하지 않는다
    assert client.calls == 3 and statuses[:3] == ["OK"] * 3 and set(statuses[3:]) == {"SKIPPED"}
    assert guard.spent <= Decimal("3.0") and guard.stop.is_set()


def test_a_call_above_its_reservation_stops_new_calls_and_is_recorded(tmp_path):
    client = _Priced([1.5, 0.1])
    guard = _guard(tmp_path, client, max_usd=10.0, max_calls=10, reserve_usd=1.0)
    guard.system_one({"question": "q0", "answer": "a"}, {"J1": {}})
    assert guard.stop.is_set() and guard.rows[-1]["reservation_exceeded"] is True
    assert guard.system_one({"question": "q1", "answer": "a"}, {"J1": {}}).status == "SKIPPED" and client.calls == 1


def test_without_a_reservation_the_previous_behaviour_is_unchanged(tmp_path):
    client = _Priced([0.9] * 5)
    guard = _guard(tmp_path, client, max_usd=2.0, max_calls=10)
    for i in range(5):
        guard.system_one({"question": f"q{i}", "answer": "a"}, {"J1": {}})
    assert client.calls == 3   # 0 → 0.9 → 1.8 은 상한 미만이라 시작, 2.7 에서 멈춤 (진행 중 초과 가능 — 예약 없는 옛 방식)
