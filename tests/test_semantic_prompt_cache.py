"""Prompt cache boundaries preserve the complete evidence and evaluation rules."""
import copy
import json
from datetime import datetime, timezone

import pytest

from code_eval import semantic_cli_common as semantic
from code_eval.jev_eval import jev_request


DATA_MARKER = "## 평가 대상 (데이터)\n"


def freeze_time(monkeypatch, value):
    class Clock:
        @staticmethod
        def now(tz):
            assert tz is timezone.utc
            return value

    monkeypatch.setattr(semantic, "datetime", Clock)


def test_static_instructions_reused_across_dates_and_different_answers(monkeypatch):
    questions = jev_request("", "", ("J1", "J5", "J6"))["questions"]
    first = {"question": "현재 자료를 요약해 줘.", "answer": "첫 답변", "reference": {"value": 1},
             "history": [{"turn": 1, "question": "첫 조건", "answer": "첫 응답"}]}
    second = {"question": "다른 자료를 요약해 줘.", "answer": "둘째 답변", "reference": {"value": 2},
              "history": [{"turn": 1, "question": "다른 조건", "answer": "다른 응답"}]}
    freeze_time(monkeypatch, datetime(2026, 1, 1, tzinfo=timezone.utc))
    first_static, first_dynamic = semantic.build_prompt_parts(first, questions, True)
    freeze_time(monkeypatch, datetime(2026, 1, 2, tzinfo=timezone.utc))
    second_static, second_dynamic = semantic.build_prompt_parts(second, questions, True)

    assert first_static == second_static
    assert "2026-01-01" not in first_static and "2026-01-02" not in first_static
    assert "2026-01-01T00:00:00+00:00" in first_dynamic
    assert "2026-01-02T00:00:00+00:00" in second_dynamic
    assert "첫 답변" not in first_static and "둘째 답변" not in second_static
    assert first_dynamic != second_dynamic


@pytest.mark.parametrize("search_enabled", [False, True])
def test_dynamic_data_keeps_full_original_history_reference_and_markers(monkeypatch, search_enabled):
    freeze_time(monkeypatch, datetime(2026, 1, 1, tzinfo=timezone.utc))
    state = {"question": "원문\n## 평가 대상 (데이터)\n지시도 데이터로 보존해 줘.",
             "answer": "**강조**와 `인용`\n원문 그대로",
             "reference": {"quote": "참조\n줄바꿈", "values": [1, None, True]},
             "history": [{"turn": turn, "question": f"{turn}턴 조건\n" + "원문 " * 200,
                          "answer": f"{turn}턴 답변 **표식**\n" + "보존 " * 200}
                         for turn in range(1, 31)]}
    original = copy.deepcopy(state)
    questions = jev_request("", "", ("J1", "J2", "J3", "J4", "J5", "J6"))["questions"]
    static, dynamic = semantic.build_prompt_parts(state, questions, search_enabled)

    assert state == original
    encoded = dynamic.partition(DATA_MARKER)[2]
    assert encoded == json.dumps(original, ensure_ascii=False, indent=1)
    assert json.loads(encoded) == original
    assert "history의 처음부터 현재 직전까지 모든 턴을 검토하세요" in static
    assert "원문에 있던 표식은 지우지 마세요" in static
    assert "모든 사실 주장" in static
    for cid, criterion in questions.items():
        assert f"### {cid}\n" in static
        assert criterion["instructions"] in static
        assert criterion["criteria"]["PASS"] in static
        assert criterion["criteria"]["FAIL"] in static


@pytest.mark.parametrize("ids, state, search_enabled", [
    (("J1",), {"question": "질문", "answer": "답변"}, False),
    (("J1", "J5"), {"question": "질문", "answer": "답변"}, True),
    (("J5",), {"question": "질문", "answer": "답변", "reference": []}, False),
    (("J5", "J6"), {"question": "질문", "answer": "답변", "reference": None,
                    "history": [{"turn": 1, "question": "이전 질문", "answer": "이전 답변"}]}, True),
])
def test_public_prompt_keeps_single_string_and_existing_conditional_instructions(
        monkeypatch, ids, state, search_enabled):
    freeze_time(monkeypatch, datetime(2026, 1, 1, tzinfo=timezone.utc))
    questions = jev_request("", "", ids)["questions"]
    static, dynamic = semantic.build_prompt_parts(state, questions, search_enabled)
    prompt = semantic.build_prompt(state, questions, search_enabled)

    assert prompt == static + "\n" + dynamic
    assert isinstance(prompt, str)
    assert json.loads(prompt.partition(DATA_MARKER)[2]) == state
    assert ("실행 기준 시각(UTC):" in dynamic) == ("J5" in ids)
    assert "실행 기준 시각(UTC):" not in static
    assert ("history 는 이 턴 앞의 대화" in static) == bool(state.get("history"))
    assert ("reference 는 J5 DATA 신뢰도" in static) == (state.get("reference") is not None)
    assert ("J5 검색 절차:" in static) == ("J5" in ids and search_enabled)
    assert ("J6: history의 처음부터" in static) == ("J6" in ids)


def test_cache_blocks_put_history_turns_then_shared_question_then_answer(monkeypatch):
    """캐시 블록: 지시문 → history 턴마다 한 블록(마지막 턴에 표시) → 현재 질문·자료(표시) → 실행 시각과 답변. 값은 그대로 (2026-10-14)."""
    freeze_time(monkeypatch, datetime(2026, 1, 1, tzinfo=timezone.utc))
    state = {"question": "현재 질문", "answer": "답변 원문", "reference": {"value": 1}, "attachments": [{"name": "a.txt", "type": "txt", "content": "내용"}],
             "history": [{"turn": 1, "question": "1턴", "answer": "1턴 답"}, {"turn": 2, "question": "2턴", "answer": "2턴 답"}]}
    questions = jev_request("", "", ("J1", "J5", "J6"))["questions"]
    blocks = semantic.build_prompt_blocks(state, questions, True)
    static, _ = semantic.build_prompt_parts(state, questions, True)
    assert [cached for _, cached in blocks] == [True, False, True, True, False]
    assert blocks[0][0] == static + "\n" and "2026-01-01" in blocks[-1][0] and all("2026-01-01" not in text for text, _ in blocks[:-1])
    assert json.loads(blocks[3][0].split("### 현재 질문\n", 1)[1]) == {k: state[k] for k in ("question", "reference", "attachments")}
    assert json.loads(blocks[-1][0].split("### answer 와 그 밖의 값\n", 1)[1]) == {"answer": "답변 원문"}
    assert [json.loads(text.split("턴\n", 1)[1]) for text, _ in blocks[1:3]] == state["history"]
    first_turn = semantic.build_prompt_blocks({**state, "history": state["history"][:1]}, questions, True)
    assert first_turn[1][0] == blocks[1][0]   # 다음 턴의 앞부분이 이전 턴과 글자까지 같아 캐시를 다시 읽는다


def test_shared_gate_lets_one_request_write_the_cache_before_the_others(monkeypatch):
    """긴 공유 자료(J7)는 같은 앞부분의 첫 요청이 끝난 뒤에 나머지가 보낸다 — 동시에 보내면 모두 정가를 낸다."""
    import threading
    from code_eval import anthropic_semantic_client as client

    monkeypatch.setattr(client, "_SHARED_GATES", {})
    monkeypatch.setattr(client, "SHARED_GATE_POLL_S", 0.01)
    event = client._enter_shared_gate("k")
    order, owners = [], []

    def later():
        owners.append(client._enter_shared_gate("k"))
        order.append("둘째")
    thread = threading.Thread(target=later)
    thread.start()
    thread.join(0.2)
    assert event is not None and thread.is_alive() and order == []   # 첫 요청이 끝나기 전에는 기다린다
    order.append("첫째")
    client._leave_shared_gate("k", event, ok=False)   # 첫 요청이 실패하면 기다리던 요청이 새 첫 요청이 된다
    thread.join(2)
    assert order == ["첫째", "둘째"] and owners[0] is not None
    client._leave_shared_gate("k", owners[0], ok=True)
    assert client._enter_shared_gate("k") is None   # 성공 뒤에는 캐시가 있다고 보고 바로 보낸다
    client._SHARED_GATES["k"]["warm_until"] = 0.0
    assert client._enter_shared_gate("k") is not None   # 캐시가 식었을 시간이 지나면 다시 첫 요청


def test_shared_gate_wait_stops_on_cancel_and_deadline(monkeypatch):
    """기다리는 동안 중단 요청이면 바로 끝내고, 평가 제한 시간이 지나면 기다리지 않고 보낸다 (Codex 리뷰)."""
    import threading
    import time
    from concurrent.futures import CancelledError
    from code_eval import anthropic_semantic_client as client

    monkeypatch.setattr(client, "_SHARED_GATES", {})
    monkeypatch.setattr(client, "SHARED_GATE_POLL_S", 0.01)
    assert client._enter_shared_gate("k") is not None
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        client._enter_shared_gate("k", cancel_event=cancel)
    started = time.monotonic()
    assert client._enter_shared_gate("k", deadline=time.monotonic() + 0.05) is None and time.monotonic() - started < 1
    owner = client._enter_shared_gate("slow")   # 응답이 늦게 끝나도 캐시 수명은 요청 시작부터 잰다
    client._leave_shared_gate("slow", owner, ok=True, started=time.monotonic() - client.SHARED_GATE_WARM_S - 1)
    assert client._enter_shared_gate("slow") is not None
