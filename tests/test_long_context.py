"""J7 롱컨텍스트 — 입력 길이 판별(long_context)·적용 판정·FAIL 근거·판정 기록. 모델·네트워크 호출 없음."""
import time

import pytest

from code_eval import engine, jev_eval, long_context, semantic_quotes as sq
from code_eval.config import AionaConfig
from code_eval.jev_eval import evaluate_jev_batch
from code_eval.jev_applicability import jev_applicability, sub_applicability
from code_eval.semantic_items import SPECS_BY_ID
from code_eval.semantic_reasons import fail_evidence

CLAUSE = "제11조 중도 해지: 임차인은 해지하려는 날의 30일 전까지 서면으로 통보한다."


class _OneTokenPerChar:
    """글자 하나 = 토큰 하나 — 경계(8,000)를 정확히 맞추려는 시험용 토크나이저."""
    def encode(self, text, disallowed_special=()):
        return list(text)


@pytest.fixture(autouse=True)
def fresh_counts():
    """토큰 수 캐시가 다른 토크나이저로 센 값을 넘기지 않게 시험마다 비운다."""
    long_context.count_tokens.cache_clear()
    yield
    long_context.count_tokens.cache_clear()


@pytest.fixture
def per_char(monkeypatch):
    monkeypatch.setattr(long_context, "_encoder", lambda: _OneTokenPerChar())


def document(chars):
    """앞뒤를 채운 문서 중간에 CLAUSE 를 둔다 — 전체 길이 chars 글자."""
    pad = (chars - len(CLAUSE)) // 2
    return "가" * pad + CLAUSE + "나" * (chars - len(CLAUSE) - pad)


def test_input_counts_question_and_reference_but_not_earlier_turns_and_tiers_follow_research_steps(per_char):
    assert long_context.input_text("질문", {"표": [1]}) == '질문\n{"표": [1]}' and long_context.input_text("질문", "") == "질문"
    assert [long_context.tier(n) for n in (250_000, 271_999, 272_000, 299_999, 300_000, 350_000, 400_000, 499_999, 500_000)] == [
        None, None, "272K~300K", "272K~300K", "300K~350K", "350K~400K", "400K~500K", "400K~500K", "500K 이상"]
    question = "이 계약서 기준으로 중도 해지는 언제까지 알려야 해?"
    below = long_context.measure(question, "가" * (271_999 - len(question) - 1))
    at = long_context.measure(question, "가" * (272_000 - len(question) - 1))
    assert (below["tokens"], below["long"], at["tokens"], at["long"], at["tier"]) == (271_999, False, 272_000, True, "272K~300K")
    assert at["min_tokens"] == 272_000 and at["policy"] == "long-context-v3-272k" and at["tokenizer"] == "tiktoken o200k_base"


def test_j7_applies_only_to_long_input_and_records_size(per_char):
    question = "계약서에서 해지 통보 기한을 알려줘."
    short = jev_applicability(question)
    assert "J7" not in short["criteria"] and short["not_applicable"]["J7"] == f"입력 {len(question)}토큰 — 272,000토큰 미만이라 롱컨텍스트가 아님"
    long = jev_applicability(question, reference=document(280_000))
    assert "J7" in long["criteria"] and "J7" not in long["not_applicable"]
    evidence = long["evidence"]["J7"]
    tokens = f"{280_000 + len(question) + 1:,}토큰"   # 질문 + 줄바꿈 + 참조 자료
    assert evidence["source"] == "reference" and evidence["matches"] == [tokens]
    assert evidence["long_context"]["tier"] == "272K~300K" and evidence["excerpt"] == f"입력 {tokens} · 272K~300K"
    subs = sub_applicability(question, reference=document(280_000))
    assert subs["J7-1"]["items"] == ["J7-1.a"] and subs["J7-2"]["items"] == ["J7-2.a"] and subs["J7-1"]["model_applies"] is True
    assert sub_applicability("짧은 질문")["J7-2"]["items"] == []


def test_without_tokenizer_j7_is_undetermined_and_counted_as_unverified_not_dropped(monkeypatch):
    def unavailable():
        raise long_context.TokenizerUnavailable("tiktoken o200k_base 를 불러오지 못함: ModuleNotFoundError")
    monkeypatch.setattr(long_context, "_encoder", unavailable)
    result = jev_applicability("질문", reference=document(40_000))
    assert "J7" not in result["criteria"] and "J7" not in result["not_applicable"]
    assert result["undetermined"]["J7"].startswith("롱컨텍스트를 판별하지 못함 — ")
    sub = sub_applicability("질문", reference=document(40_000))["J7-1"]
    assert sub["items"] == [] and sub["undetermined"] is True
    case = {"case_id": "doc", "question": "이 계약서에서 중도 해지 통보 기한은?", "answer": "30일 전입니다.", "reference": document(400)}
    batch = engine.evaluate_batch([case], AionaConfig())
    for pipeline in ("legacy", "items"):
        jev = evaluate_jev_batch(batch, None, access_reason="키 없음", pipeline=pipeline)
        record = jev["records"][0]
        assert "J7" in record["criteria"] and record["decisions"]["J7"]["status"] == "UNVERIFIED"
        assert record["review_required"] is True and "J7" in record["not_requested"]
        assert jev["summary"]["J7"]["targets"] == 1 and jev["summary"]["J7"]["pass"] == jev["summary"]["J7"]["fail"] == 0


def test_tokenizer_failure_is_remembered_then_retried_after_the_cooldown(monkeypatch):
    calls = []

    def load():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("offline")
        return _OneTokenPerChar()
    monkeypatch.setattr(long_context, "_STATE", {"encoder": None, "error": None, "failed_at": 0.0})
    monkeypatch.setattr(long_context, "_load", load)
    for text in ("가나다", "라마바"):   # 두 번째는 기억한 실패 — 다시 받지 않는다
        with pytest.raises(long_context.TokenizerUnavailable, match="ConnectionError"):
            long_context.count_tokens(text)
    assert calls == [1]
    long_context._STATE["failed_at"] -= long_context.RETRY_AFTER   # 기다리는 시간이 지나면 새로 시도
    assert long_context.count_tokens("사아자") == 3 and calls == [1, 1]


def test_a_stalled_vocabulary_download_is_cut_off_at_the_time_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(long_context, "_STATE", {"encoder": None, "error": None, "failed_at": 0.0})
    monkeypatch.setattr(long_context, "_cache_file", lambda: tmp_path / "vocab")   # 아직 받은 사전 없음
    monkeypatch.setattr(long_context, "_PREFETCH", "import time; time.sleep(30)")   # 멈춘 내려받기 대역
    monkeypatch.setattr(long_context, "LOAD_TIMEOUT", 1.0)
    started = time.monotonic()
    with pytest.raises(long_context.TokenizerUnavailable, match="TimeoutError"):
        long_context.count_tokens("가나다")
    assert time.monotonic() - started < 10


def test_parent_never_downloads_without_a_verified_cache(tmp_path, monkeypatch):
    import tiktoken
    monkeypatch.setattr(tiktoken, "get_encoding", lambda name: pytest.fail("확인된 캐시 없이 불러옴"))
    monkeypatch.setattr(long_context, "_STATE", {"encoder": None, "error": None, "failed_at": 0.0})
    monkeypatch.setattr(long_context, "_cache_file", lambda: tmp_path / "vocab")
    monkeypatch.setattr(long_context, "_PREFETCH", "pass")   # 받기는 성공했다고 끝나지만 캐시는 남기지 않음
    with pytest.raises(long_context.TokenizerUnavailable, match="사전 캐시를 쓰지 못함"):
        long_context.count_tokens("가나다")
    monkeypatch.undo()
    monkeypatch.setattr(long_context, "_STATE", {"encoder": None, "error": None, "failed_at": 0.0})
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "")
    with pytest.raises(long_context.TokenizerUnavailable, match="캐시가 꺼져 있음"):
        long_context.count_tokens("라마바")


def test_items_batch_measures_each_input_once_and_both_applicability_paths_share_it(per_char, monkeypatch):
    calls = []
    measure = long_context.measure
    monkeypatch.setattr(long_context, "measure", lambda *a: calls.append(1) or measure(*a))
    case = {"case_id": "doc", "question": "이 계약서에서 중도 해지 통보 기한은?", "answer": "30일 전입니다.", "reference": document(273_000)}
    jev = evaluate_jev_batch(engine.evaluate_batch([case], AionaConfig()), None, access_reason="키 없음", pipeline="items")
    assert calls == [1] and "J7" in jev["records"][0]["applicability"]["criteria"]
    assert jev["records"][0]["applicability"]["subs"]["J7-1"]["items"] == ["J7-1.a"]


def test_j7_fail_reason_may_quote_the_reference_and_records_where_the_quote_sits(per_char):
    reference = document(280_000)
    state = {"question": "중도 해지는 언제까지 알려야 해?", "answer": "만료 60일 전까지 알려야 합니다.", "reference": reference}
    decision = {"decision": "FAIL", "reason": "입력의 '해지하려는 날의 30일 전까지' 대신 '만료 60일 전까지'라고 답함"}
    check = fail_evidence(decision, state, "J7")
    assert check["valid"] and [q["found_in"] for q in check["matched_quotes"]] == ["reference", "answer"]
    assert check["policy"] == "j1-j4-j7-fail-evidence-v2"
    answer_only = fail_evidence({"decision": "FAIL", "reason": "답변이 '만료 60일 전까지'라고 함"}, state, "J7")
    assert not answer_only["valid"] and answer_only["problems"] == ["input_quote_missing"]
    made_up = fail_evidence({"decision": "FAIL", "reason": "입력의 '해지는 90일 전' 대신 '만료 60일 전까지'라고 답함"}, state, "J7")
    assert not made_up["valid"] and made_up["problems"] == ["input_quote_missing"]
    input_only = fail_evidence({"decision": "FAIL", "reason": "입력의 '해지하려는 날의 30일 전까지'를 놓침"}, state, "J7")
    assert input_only["problems"] == ["answer_quote_missing"]
    copied = {**state, "answer": "해지하려는 날의 30일 전까지가 아니라 만료 60일 전까지입니다."}
    both = fail_evidence({"decision": "FAIL", "reason": "답변이 입력 문구 '해지하려는 날의 30일 전까지'를 부정함"}, copied, "J7")
    assert both["valid"] and both["matched_quotes"][0]["also_in"] == ["reference"]   # 답변·입력 모두에 있는 인용
    turns = {**copied, "history": [{"turn": 1, "question": "해지하려는 날의 30일 전까지 맞지?", "answer": "네"}]}
    everywhere = fail_evidence({"decision": "FAIL", "reason": "답변이 입력 문구 '해지하려는 날의 30일 전까지'를 부정함"}, turns, "J7")
    assert everywhere["places_complete"] and everywhere["matched_quotes"][0]["also_in"] == ["reference", "history"]
    assert fail_evidence({"decision": "FAIL", "reason": "답변이 '만료 60일 전까지'라고 함"}, state)["valid"]   # J1~J4 는 인용 하나면 됨
    record = {"decisions": {"J7": decision}}
    applicability = jev_applicability(state["question"], reference=reference)
    jev_eval._attach_long_context(record, state, applicability)
    recorded = record["decisions"]["J7"]["long_context"]
    assert recorded["tokens"] == 280_000 + len(state["question"]) + 1 and recorded["tier"] == "272K~300K"
    assert [p["quote"] for p in recorded["quote_positions"]] == ["해지하려는 날의 30일 전까지"]
    assert 45 < recorded["quote_positions"][0]["position"] < 55   # 문서 한가운데


def test_j7_items_no_requires_a_quote_from_the_current_input():
    state = {"question": "중도 해지는 언제까지 알려야 해?", "answer": "만료 60일 전까지 알려야 합니다.", "reference": CLAUSE,
             "history": [{"turn": 1, "question": "계약서 요약해줘", "answer": "해지는 30일 전 통보"}]}
    qs = {"J7-1.a": SPECS_BY_ID["J7-1.a"].to_prompt_dict({})}
    item = {"id": "J7-1.a", "decision": "NO", "answer_quote": "만료 60일 전까지", "reason": "r"}
    _, failures = sq.verify_items({"items": [dict(item, question_quote="")]}, state, qs)
    assert [f["code"] for f in failures] == ["question_quote_required"]
    _, failures = sq.verify_items({"items": [dict(item, question_quote="해지는 30일 전 통보", turn=1)]}, state, qs)
    assert [f["code"] for f in failures] == ["question_quote_not_found"]   # 이전 턴은 J6 — J7 은 현재 입력에서 인용
    checked, failures = sq.verify_items({"items": [dict(item, question_quote="해지하려는 날의 30일 전까지")]}, state, qs)
    assert failures == [] and checked["items"][0]["quote_check"]["question"]["source"] == "reference"


def test_real_tokenizer_counts_korean_and_english_when_available():
    try:
        tokens = long_context.count_tokens("롱컨텍스트 판별 long context")
    except long_context.TokenizerUnavailable:
        pytest.skip("tiktoken o200k_base 를 불러올 수 없음")
    assert 3 <= tokens <= 15


def test_summary_counts_j7_pass_and_fail_per_input_length_tier():
    def record(tier, decision):
        return {"criteria": ["J1", "J7"], "decisions": {"J7": {"decision": decision}},
                "applicability": {"evidence": {"J7": {"long_context": {"tier": tier}}}}}
    final = [record("272K~300K", "PASS"), record("272K~300K", "FAIL"), record("300K~350K", "PASS"), record("500K 이상", None),
             {"criteria": ["J1"], "decisions": {}, "applicability": {}}]
    tiers = jev_eval._long_context_tiers(final)
    assert list(tiers) == ["272K~300K", "300K~350K", "350K~400K", "400K~500K", "500K 이상"]
    assert tiers["272K~300K"] == {"targets": 2, "pass": 1, "fail": 1, "rate": 0.5}
    assert tiers["500K 이상"] == {"targets": 1, "pass": 0, "fail": 0, "rate": None}


def test_cli_single_prints_an_undetermined_j7_without_crashing(tmp_path, monkeypatch):
    from code_eval import __main__ as cli
    from code_eval.jev_client import resolve_jev_client

    def unavailable():
        raise long_context.TokenizerUnavailable("tiktoken o200k_base 를 불러오지 못함: ModuleNotFoundError")
    output = []
    monkeypatch.setattr(long_context, "_encoder", unavailable)
    monkeypatch.setattr(jev_eval, "resolve_jev_client",
                        lambda **kw: resolve_jev_client(environment={}, env_path=tmp_path / "missing.env", **kw))
    monkeypatch.setattr(cli, "_print", output.append)
    assert cli.main(["single", "--question", "짧은 시를 써줘.", "--answer", "바람이 노래하네."]) == 0
    assert any("J7 롱컨텍스트 활용 UNVERIFIED / 롱컨텍스트를 판별하지 못함" in line for line in output)
