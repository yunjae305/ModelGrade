"""롱컨텍스트 경계 테스트 도구 — 문서 생성·채점 (API 호출 없음)."""
import pytest

from code_eval import long_context
from tools.long_context_threshold_test import build_case, grade

try:
    long_context.count_tokens("확인")
except long_context.TokenizerUnavailable:   # 토크나이저가 없으면 길이를 맞출 수 없다
    pytest.skip("tiktoken o200k_base 를 불러올 수 없음", allow_module_level=True)


def test_document_hits_the_target_length_and_places_facts_at_fixed_depths():
    case = build_case(32_000)
    assert abs(case["tokens"] - 32_000) < 300 and build_case(32_000)["sha256"] == case["sha256"]   # 같은 시드면 같은 문서
    prompt = case["prompt"]
    depth = lambda key: prompt.find(key) / len(prompt)
    assert abs(depth("은하수-7") - 0.10) < 0.02 and abs(depth("'청람'") - 0.50) < 0.02 and abs(depth("해오름") - 0.90) < 0.02
    assert abs(depth("윤서진 책임자는") - 0.25) < 0.02 and abs(depth("윤서진 책임자가 관리") - 0.75) < 0.02
    assert all(prompt.count(code) == 1 for code in case["answers"].values())


def test_grading_reads_numbered_lines_and_falls_back_to_the_whole_answer():
    answers = {1: "KX-1111", 2: "CR-2222", 3: "HD-3333", 4: "BG-4444"}
    assert grade("1) KX-1111\n2) CR-2222\n3) 모름\n4) BG-9999", answers) == {1: True, 2: True, 3: False, 4: False}
    assert grade("**1)** KX-1111\n2. CR-2222\n3: HD-3333\n4번 BG-4444", answers) == {1: True, 2: True, 3: True, 4: True}
    assert grade("KX-1111, CR-2222, HD-3333, BG-4444", answers) == {1: True, 2: True, 3: True, 4: True}
    assert grade("1) CR-2222\n2) KX-1111", answers)[1] is False   # 번호 줄이 있으면 그 줄에서만


def test_hard_document_hides_unmarked_facts_with_one_resolvable_answer_each():
    import re
    from tools.long_context_threshold_test import build_hard_case
    case = build_hard_case(32_000)
    prompt, answers = case["prompt"], case["answers"]
    assert abs(case["tokens"] - 32_000) < 300 and "[특별 기록]" not in prompt and case["kinds"] == {5: "count"}
    assert "부산" not in prompt.split("위 기록만 보고")[0] and "제주" not in prompt.split("위 기록만 보고")[0]   # 질문의 도시는 문서에 없음
    team = re.search(r"모래시계 사업은 (\S+)이 맡는다", prompt).group(1)
    leader = re.search(team + r"의 팀장은 (\S+)이다", prompt).group(1)
    assert re.search(leader + r" 팀장이 승인한 예산 코드는 (\S+)이다", prompt).group(1) == answers[4]   # 3단계 연결은 하나로만 이어짐
    assert prompt.count("정전이 일어나") == int(answers[5])
    assert grade("1) a\n2) b\n3) c\n4) d\n5) 다섯 번", answers, case["kinds"])[5] is (answers[5] == "5")
