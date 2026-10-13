"""롱컨텍스트 추가 탐침 — 개정 반영·상충 정보 판별·긴 거리 지시 유지를 만들고 채점한다 (모델 호출 없음)."""
from tools.long_context_probes import build_probe_case, estimate, grade_probe


def test_probe_document_places_facts_in_order_and_grades_each_failure_kind():
    case = build_probe_case(3_000)
    prompt, a, d = case["prompt"], case["answers"], case["distractors"]
    assert prompt.startswith("[요청] 마지막 질문 목록의 3번에 답할 때만")
    assert prompt.index(d[1]) < prompt.index(a[1])            # 옛 규정 코드가 개정 공지보다 앞
    assert prompt.index(d[2]) < prompt.index(a[2])            # 3월 값이 9월 정정보다 앞
    assert a[3] == "OK-" + d[3] and a[3] not in prompt          # 접두는 지시를 따라야만 나온다
    perfect = grade_probe(f"1) {a[1]}\n2) {a[2]}\n3) {a[3]}", case)
    assert perfect["score"] == 3 and perfect["misses"] == {}
    wrong = grade_probe(f"1) {d[1]}\n2) {d[2]}\n3) {d[3]}", case)
    assert wrong["score"] == 0
    assert wrong["misses"] == {1: "옛 값 · 상충 값을 고름", 2: "옛 값 · 상충 값을 고름", 3: "지시(접두) 빠짐"}
    assert build_probe_case(3_000)["sha256"] == case["sha256"]   # 같은 시드면 같은 문서


def test_cost_estimate_counts_claude_tokenizer_and_gpt_long_context_price():
    est = estimate((250_000, 272_000))
    rows = {(r["length"], r["model"]): r for r in est["rows"]}
    assert rows[(272_000, "claude")]["provider_input_estimate"] == 476_000
    # GPT: o200k 250K → 제공자 약 262.5K(기본 단가) · 272K → 약 285.6K 로 272K 를 넘어 입력 단가 2배
    per_token = lambda n: rows[(n, "gpt")]["usd"] / rows[(n, "gpt")]["provider_input_estimate"]
    assert per_token(272_000) > per_token(250_000) * 1.8
    assert est["calls"] == 6
