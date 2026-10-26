"""CODE 평가 단계의 실행 경계와 엔진 회귀 사례. 이 파일의 입력은 모두 테스트 픽스처다."""

from __future__ import annotations

import socket

import pytest

from code_eval import engine
from code_eval.__main__ import main as cli_main
from code_eval.config import load_aiona_config
from code_eval.model import Status


def test_code_phase_network_attempt_aborts_even_if_checker_catches_error(tmp_path, monkeypatch):
    def tries_network(_ctx):
        socket.create_connection(("127.0.0.1", 9), timeout=0.01)
        return []

    monkeypatch.setattr(engine, "_COMMON", (("1-1", tries_network),))
    src = tmp_path / "case.jsonl"
    src.write_text('{"case_id":"t","question":"설명해줘.","answer":"안녕"}', encoding="utf-8")
    out = tmp_path / "out"
    with pytest.raises(RuntimeError, match="차단한 네트워크 호출"):
        cli_main(["run", "--input", str(src), "--out", str(out)])
    assert not (out / "results.json").exists()


def test_checker_exception_is_recorded_and_later_cases_continue(monkeypatch):
    def broken(_ctx):
        raise RuntimeError("test checker failure")

    monkeypatch.setattr(engine, "_COMMON", (("1-1", broken),))
    cases = [{"case_id": "a", "question": "안내해줘.", "answer": "첫째."},
             {"case_id": "b", "question": "안내해줘.", "answer": "둘째."}]
    batch = engine.evaluate_batch(cases, load_aiona_config())
    assert batch["summary"]["totals"]["evaluated"] == 2
    assert batch["summary"]["totals"]["check_errors"] == 2
    assert [case["case_id"] for case in batch["cases"]] == ["a", "b"]
    for case in batch["cases"]:
        # 검사기 오류는 결과 행이 아니라 사례 errors 로 남고, 사례는 EVALUATED 로 유지된다.
        assert case["case_status"] == "EVALUATED"
        assert not [r for r in case["results"] if r["subcategory"] == "1-1"]
        assert case["errors"][0]["kind"] == "CHECK_ERROR" and case["errors"][0]["check_id"] == "1-1.check_error"
        assert "test checker failure" in case["errors"][0]["message"]
        assert case["summary"]["errors"] == 1


def test_explicit_one_table_request_is_extracted_without_row_floor():
    question = ("서버 스펙을 표 하나로만 정리해 주세요. "
                "열은 호스트명, CPU, 메모리, 디스크, 용도 5개입니다. 표만 출력해 주세요.")
    answer = "| 호스트명 | CPU | 메모리 | 디스크 | 용도 |\n|---|---|---|---|---|\n| a | b | c | d | e |"
    case = engine.evaluate_case({"question": question, "answer": answer})
    found = {r["check_id"]: r for r in case["results"]}
    assert found["2-2.table:required"]["verdict"] == "PASS"
    assert found["2-2.table:count"]["verdict"] == "PASS"
    assert found["2-2.table:columns"]["verdict"] == "PASS"
    assert found["2-2.table:column_count"]["verdict"] == "PASS"
    assert not any("row" in key for key in found)
    assert not any(r["verdict"] == "FAIL" for r in case["results"])


def test_missing_table_does_not_duplicate_presence_fail_as_count_fail():
    case = engine.evaluate_case({"question": "결과를 표 하나로만 정리해 주세요.", "answer": "결과입니다."})
    found = {r["check_id"]: r for r in case["results"]}
    assert found["2-2.table:required"]["verdict"] == "FAIL"
    assert "2-2.table:count" not in found  # 표 존재 FAIL 에 딸린 행은 만들지 않는다
    assert any(s["check_id"] == "2-2.table:count" for s in case["meta"]["skipped_conditions"])


@pytest.mark.parametrize("question,answer", [
    ("아래 상품 데이터를 JSON으로 변환해 주세요. 상품: 헤드셋 119000원 / 웹캠 55000원",
     '{"items": [{"name": "헤드셋", "price": 119000}]}'),
    ("아래 상담 기록을 표로 정리해 주세요. 원본 그대로 정확하게 옮겨 주세요.\n---\n상담 기록: 8월 1일 배송 문의",
     "| 날짜 | 내용 |\n|---|---|\n| 8월 1일 | 배송 문의 |"),
])
def test_unparsed_source_transformation_makes_no_condition(question, answer):
    # 'JSON으로 변환'(형식 변환)·구조화된 원본이 없는 '원본 그대로'는 변환 조건 자체를 만들지 않는다 — 행도 skipped 도 없음.
    case = engine.evaluate_case({"question": question, "answer": answer})
    assert not [r for r in case["results"] if r["check_id"].startswith("3-3.")]
    assert not [s for s in case["meta"]["skipped_conditions"] if s["check_id"].startswith("3-3.")]
    assert not [c for c in case["meta"]["question_conditions"] if c["kind"] in ("transform", "unique_values", "aggregate")]


@pytest.mark.parametrize("question,answer", [
    ("서울과 부산 인구를 표로 정리해줘.", "| 도시 | 인구 |\n|---|---|\n| 서울 | 935만 |\n| 부산 | 330만 |\n"),
    ("12자 이하로 답해줘.", "안녕하세요\n반갑습니다"),
])
def test_crlf_line_endings_measure_like_lf(question, answer):
    # 윈도우 줄바꿈(CRLF)의 \r 은 줄바꿈의 일부다. 표 인식과 글자 수가 LF 와 같아야 한다.
    def verdicts(text):
        case = engine.evaluate_case({"question": question, "answer": text})
        return [(r["check_id"], r["verdict"], r["reason"]) for r in case["results"]]
    crlf = answer.replace("\n", "\r\n")
    assert verdicts(crlf) == verdicts(answer)
    assert all(verdict != "FAIL" for _, verdict, _ in verdicts(crlf))
    assert engine.evaluate_case({"question": question, "answer": crlf})["answer"] == crlf


FENCE = "```"


@pytest.mark.parametrize("question,answer,check_id,measured", [
    # 같은 종류 형제 항목이 1칸 어긋나도 하위 항목이 아니다 (앞 항목 내용 시작 열보다 덜 들여씀).
    ("공부 방법을 불릿으로 정확히 3개 작성해줘.", "- 개념 정리\n - 문제 풀이\n- 오답 복습",
     "2-1.count:bullet_count", {"value": 3}),
    ("방법을 번호 목록으로 정확히 3개 작성해줘.", "1. 개념 정리\n 2. 문제 풀이\n3. 오답 복습",
     "2-1.count:list_count", {"value": 3}),
    ("공부 방법을 알려줘.", "다음 세 가지를 추천합니다.\n\n- 개념 정리\n - 문제 풀이\n- 오답 복습",
     "3-2.declared_count:1", {"count": 3}),
    # 목록 항목 안 펜스는 항목 내용 열에서 0~3칸이라 줄 기준 4칸 이상일 수 있다.
    ("설치 방법을 코드블록으로 보여줘.",
     "1. 준비\n   - 설치\n     " + FENCE + "bash\n     pip install x\n     " + FENCE + "\n2. 끝",
     "1-2.code_block:required", {"code_blocks": 1}),
    ("방법을 3줄로 설명해줘.",
     "설치합니다.\n실행합니다.\n\n- 명령\n    " + FENCE + "bash\n    pip install x\n    run x\n    " + FENCE,
     "2-1.count:line_count", {"value": 3}),
    # 들여쓴 펜스의 내용은 그 들여쓰기를 뺀 코드로 구문을 읽는다.
    ("파이썬 함수를 작성해줘.", "1. 함수 작성\n   " + FENCE + "python\n   def f():\n       return 1\n   " + FENCE,
     "1-2.python_syntax:block1", {"language_label": "python"}),
    # GFM 표는 양끝 파이프를 생략할 수 있고, 인용문 안에도 쓸 수 있다.
    ("과일과 가격을 표로 정리해줘. 데이터 행은 정확히 2개로 해줘.", "과일 | 가격\n---|---\n사과 | 1000\n배 | 2000",
     "2-2.table:table_data_rows", {"data_rows": 2}),
    ("과일과 가격을 표로 정리해줘.", "> | 과일 | 가격 |\n> |---|---|\n> | 사과 | 1000 |",
     "2-2.table:required", None),
])
def test_commonmark_structures_are_measured_as_rendered(question, answer, check_id, measured):
    case = engine.evaluate_case({"question": question, "answer": answer})
    row = next(r for r in case["results"] if r["check_id"] == check_id)
    assert row["verdict"] == "PASS"
    assert measured is None or measured.items() <= row["measured"].items()


@pytest.mark.parametrize("answer", [
    # 목록 밖 4칸 들여쓰기와, 빈 줄 뒤 본문으로 닫힌 목록 다음의 4칸 들여쓰기는 펜스가 아니다.
    "설치합니다.\n\n    " + FENCE + "\n    코드 아님\n    " + FENCE + "\n실행합니다.",
    "- 설치\n\n본문 문단입니다.\n\n    " + FENCE + "bash\n    pip\n    " + FENCE,
])
def test_indented_backticks_outside_list_items_stay_text(answer):
    case = engine.evaluate_case({"question": "방법을 코드블록으로 보여줘.", "answer": answer})
    row = next(r for r in case["results"] if r["check_id"] == "1-2.code_block:required")
    assert (row["verdict"], row["measured"]["code_blocks"]) == ("FAIL", 0)


def test_prose_pipe_above_a_rule_is_not_a_table():
    case = engine.evaluate_case({"question": "과일과 가격을 표로 정리해줘.", "answer": "사과 | 배 중에 고르세요.\n---\n끝입니다."})
    row = next(r for r in case["results"] if r["check_id"] == "2-2.table:required")
    assert row["verdict"] == "FAIL" and row["measured"]["tables"] == []


@pytest.mark.parametrize("question,expected", [
    # '배열' 안의 '열'은 표의 열이 아니다.
    ('다음 JSON 배열에서 "score"가 50 이상인 항목만 남겨줘.', set()),
    ("숫자 배열을 3개 만들어 표로 보여줘.", {"table_required"}),
    ("할 일을 표로 정리해줘. 열 이름은 '구분', '설명'이야.", {"table_required", "table_columns"}),
    ("표로 정리하고 열은 3개로 해줘.", {"table_required", "table_column_count"}),
])
def test_column_keyword_is_not_read_inside_another_word(question, expected):
    case = engine.evaluate_case({"question": question, "answer": "답변"})
    kinds = {c["kind"] for c in case["meta"]["question_conditions"]}
    assert {k for k in kinds if k.startswith("table_")} == expected


@pytest.mark.parametrize("answer,verdict", [
    ("개인정보   보호 정책을 따릅니다.", "PASS"),      # 연속 공백
    ("개인정보\n보호 정책을 따릅니다.", "PASS"),       # 줄바꿈
    ("개인정보\t보호 정책을 따릅니다.", "PASS"),       # 탭
    ("개인정보 보호 정책을 따릅니다.", "PASS"),   # NBSP
    ("개인정보 보호정책을 따릅니다.", "FAIL"),         # 띄어쓰기를 없앤 것은 다른 문구
    ("개인정보, 보호 정책을 따릅니다.", "FAIL"),       # 구두점
])
def test_required_phrase_allows_only_whitespace_differences(answer, verdict):
    case = engine.evaluate_case({"question": "답변에 '개인정보 보호 정책'을 반드시 포함해줘.", "answer": answer})
    [row] = [r for r in case["results"] if r["check_id"] == "2-1.literal:required_literal"]
    assert row["verdict"] == verdict
    assert ("whitespace_normalized_occurrences" in row["measured"]) is (verdict == "PASS")


@pytest.mark.parametrize("question", [
    "답변에 '개인정보 보호 정책'을 정확히 그대로 포함해줘.",
    "'개인정보 보호 정책'을 공백까지 그대로 포함해줘.",
    "답변에 '개인정보 보호 정책'이라는 문구를 한 글자도 바꾸지 말고 넣어줘.",
])
def test_exact_phrase_request_is_compared_character_for_character(question):
    case = engine.evaluate_case({"question": question, "answer": "개인정보   보호 정책"})
    [row] = [r for r in case["results"] if r["check_id"] == "2-1.literal:required_literal"]
    assert row["verdict"] == "FAIL" and row["required"] == {"literal": "개인정보 보호 정책", "exact": True}


def test_exact_count_does_not_make_the_phrase_exact_and_case_still_matters():
    case = engine.evaluate_case({"question": "정확히 3문장으로 쓰고 '개인정보 보호 정책'을 포함해줘.",
                                 "answer": "개인정보   보호 정책을 지킵니다. 둘. 셋."})
    assert [r["verdict"] for r in case["results"] if r["check_id"] == "2-1.literal:required_literal"] == ["PASS"]
    case = engine.evaluate_case({"question": "답변에 'Hello World'를 포함해줘.", "answer": "hello   world"})
    assert [r["verdict"] for r in case["results"] if r["check_id"] == "2-1.literal:required_literal"] == ["FAIL"]


@pytest.mark.parametrize("question,kinds", [
    ("3개 열로 된 표로 정리해줘.", {("table_required", None), ("table_column_count", 3)}),
    ("3열 표로 정리해줘.", {("table_required", None), ("table_column_count", 3)}),
    ("3행 표로 정리해줘.", {("table_required", None), ("table_rows_ambiguous", 3)}),
    ("공부 방법 항목 3개를 정리해줘.", {("list_count", 3)}),
    ("3개 열로 된 표에 항목 5개를 정리해줘.", {("table_column_count", 3), ("list_count", 5)}),
])
def test_table_columns_and_rows_are_not_read_as_item_counts(question, kinds):
    case = engine.evaluate_case({"question": question, "answer": "답변"})
    assert {(c["kind"], c["value"].get("n")) for c in case["meta"]["question_conditions"]} >= kinds
    assert not ("list_count", 3) in {(c["kind"], c["value"].get("n")) for c in case["meta"]["question_conditions"]} \
        or ("list_count", 3) in kinds


@pytest.mark.parametrize("answer,count", [
    ("> - 첫 번째 항목\n> - 두 번째 항목\n> - 세 번째 항목", 3),                      # 인용문 안 목록
    ("> " + FENCE + "\n> - 코드 속 줄\n> " + FENCE + "\n- 하나\n- 둘\n- 셋", 3),       # 인용문 안 코드 펜스
    (FENCE + "\n- 코드\n- 코드\n" + FENCE + "\n- 하나\n- 둘\n- 셋", 3),                 # 코드 블록 안 '-'
])
def test_blockquote_lists_count_but_code_inside_quotes_does_not(answer, count):
    case = engine.evaluate_case({"question": "공부 방법을 불릿으로 정확히 3개 작성해줘.", "answer": answer})
    [row] = [r for r in case["results"] if r["check_id"] == "2-1.count:bullet_count"]
    assert (row["verdict"], row["measured"]["value"]) == ("PASS", count)


# --- 공통 결과 행은 PASS/FAIL 뿐 (v16.3) ----------------------------------------------------------

def _ids(case, prefix):
    # 답변 구조 검사의 '검사 대상 없음' PASS 행(:none, v16.5.0)은 딸린 행 규칙과 무관하므로 뺀다
    return [(r["check_id"], r["verdict"]) for r in case["results"] if r["check_id"].startswith(prefix) and not r["check_id"].endswith(":none")]


def _skipped(case, prefix):
    return [s["check_id"] for s in case["meta"]["skipped_conditions"] if s["check_id"].startswith(prefix)]


def test_dependent_rows_are_not_created_after_leading_fail():
    fence = engine.evaluate_case({"question": "설치 방법을 코드블록으로 보여줘.", "answer": "pip install x"})
    assert _ids(fence, "1-2") == [("1-2.code_block:required", "FAIL")]  # code_fence_min 으로 두 번 FAIL 하지 않음
    label = engine.evaluate_case({"question": "예시를 파이썬 코드블록으로 보여줘.", "answer": "예시 없음"})
    assert _ids(label, "1-2") == [("1-2.code_block:required", "FAIL")]
    assert _skipped(label, "1-2.code_language_label")
    # 코드블록 요구 행이 없는데 언어 표지만 요구되고 블록이 0개면 FAIL 행 하나
    only_label = engine.evaluate_case({"question": "언어 표지는 python 으로 해줘.", "answer": "없음"})
    assert _ids(only_label, "1-2") == [("1-2.code_language_label", "FAIL")]
    assert "코드블록이 없음" in next(r for r in only_label["results"] if r["check_id"] == "1-2.code_language_label")["reason"]
    # 언어 표지가 없어도 코드블록으로 구분돼 있으면 통과, 다른 언어를 적으면 FAIL (사용자 결정 10-21)
    bare = engine.evaluate_case({"question": "결과는 json 코드블록 정확히 1개로 보여 주세요.", "answer": "```\n[1, 2]\n```"})
    assert ("1-2.code_language_label", "PASS") in _ids(bare, "1-2")
    wrong = engine.evaluate_case({"question": "결과는 json 코드블록 정확히 1개로 보여 주세요.", "answer": "```python\n[1, 2]\n```"})
    assert ("1-2.code_language_label", "FAIL") in _ids(wrong, "1-2")
    table = engine.evaluate_case({"question": "표로 정리해줘. 열은 '이름', '나이'야.", "answer": "없음"})
    assert _ids(table, "2-2") == [("2-2.table:required", "FAIL")]
    assert _skipped(table, "2-2.table:table_columns")
    js = engine.evaluate_case({"question": "이름과 나이를 JSON 객체로 줘. 키는 'name', 'age'이고 'age'는 정수여야 해.",
                               "answer": "없음"})
    assert _ids(js, "2-2") == [("2-2.json_structure:present", "FAIL")]
    assert set(_skipped(js, "2-2.json_structure")) == {"2-2.json_structure:json_required_keys", "2-2.json_structure:json_field_type"}
    for case in (fence, label, only_label, table, js):
        assert case["errors"] == [] and case["summary"]["common"]["unjudged"] == 0


def test_check_error_is_case_level_and_semantic_evaluation_still_runs(monkeypatch):
    from code_eval.checks_requirements import check_2_1
    from code_eval.jev_eval import evaluate_jev_batch
    from test_jev_eval import ScriptedJev

    def broken(_ctx):
        raise RuntimeError("test checker failure")

    monkeypatch.setattr(engine, "_COMMON", (("1-1", broken), ("2-1", check_2_1)))
    cases = [{"case_id": "a", "question": "공부 방법을 불릿으로 정확히 2개 작성해줘.", "answer": "- 하나\n- 둘"}]
    batch = engine.evaluate_batch(cases, load_aiona_config())
    case = batch["cases"][0]
    assert case["case_status"] == "EVALUATED"
    error = case["errors"][0]
    assert (error["check_id"], error["subcategory"], error["kind"]) == ("1-1.check_error", "1-1", "CHECK_ERROR")
    assert "RuntimeError" in error["message"] and "traceback" in error["details"]
    assert [r["verdict"] for r in case["results"] if r["check_id"] == "2-1.count:bullet_count"] == ["PASS"]
    assert not [r for r in case["results"] if r["subcategory"] == "1-1"]
    assert case["summary"]["errors"] == 1 and case["summary"]["common"]["unjudged"] == 0
    assert case["summary"]["overall"] == "검사한 범위 PASS · 실행 오류 있음"
    assert batch["summary"]["totals"]["check_errors"] == 1
    jev = evaluate_jev_batch(batch, ScriptedJev())
    assert jev["records"][0]["status"] == "SUCCESS"  # 실행 오류가 의미 평가를 막지 않는다
    assert batch["result_digest"] == engine.evaluate_batch(cases, load_aiona_config())["result_digest"]


def test_in_check_limit_exceeded_goes_to_case_errors():
    py = engine.evaluate_case({"question": "파이썬 코드를 작성해줘.", "answer": "```python\n" + "x=1\n" * 60_000 + "```"})
    assert py["case_status"] == "EVALUATED"
    assert not [r for r in py["results"] if r["check_id"].startswith("1-2.python_syntax")]
    assert [(e["check_id"], e["kind"]) for e in py["errors"]] == [("1-2.python_syntax:block1", "LIMIT_EXCEEDED")]
    js = engine.evaluate_case({"question": "결과를 JSON으로 줘.", "answer": "```json\n" + "[" * 50_000 + "]" * 50_000 + "\n```"})
    assert not [r for r in js["results"] if r["check_id"].startswith(("1-2.json_syntax", "2-2.json_structure"))]
    assert [(e["check_id"], e["kind"]) for e in js["errors"]] == [("1-2.json_syntax:block1", "LIMIT_EXCEEDED")]
    assert js["summary"]["errors"] == 1 and js["summary"]["common"]["unjudged"] == 0


def _cat(case, cat):
    b = case["summary"]["by_category"][cat]
    return (b["pass"], b["fail"], b["conditions"], b["rows"])


def test_answer_structure_checks_count_once_per_answer_so_every_model_gets_the_same_conditions():
    question = "정정된 재고를 다시 계산해줘."
    plain = engine.evaluate_case({"question": question, "answer": "노트 26권, 펜 34자루, 파일 5개입니다."})
    worked = engine.evaluate_case({"question": question, "answer": "- 20 - 4 = 16권\n- 16 + 10 = 26권\n- 30 - 8 = 22자루\n- 22 + 12 = 34자루"})
    wrong = engine.evaluate_case({"question": question, "answer": "- 20 - 4 = 16권\n- 16 + 10 = 27권"})
    for case in (plain, worked, wrong):
        assert case["errors"] == [] and case["summary"]["common"]["unjudged"] == 0
        # 같은 질문이면 답변이 무엇을 보여 줬든 분류별 조건 수가 같다 (항상 3 — 에러 케이스 1 은 대분류 0(10-26) + 답변 구조 9 + 질문이 만든 0)
        assert {k: v["conditions"] for k, v in case["summary"]["by_category"].items()} == {"0": 1, "1": 9, "2": 0, "3": 2}
    assert _cat(plain, "3") == (2, 0, 2, 2)      # 등식 없음 → '검사 대상 없음' PASS 1조건 + 선언 개수 1조건
    assert _cat(worked, "3") == (2, 0, 2, 5)     # 등식 4개가 모두 맞음 → 조건 1개 PASS, 행은 4개 그대로
    assert _cat(wrong, "3") == (1, 1, 2, 3)      # 등식 2개 중 1개 틀림 → 조건 1개 FAIL (등식 수만큼 세지 않음)
    assert [r["check_id"] for r in worked["results"] if r["check_id"].startswith("3-1.answer_equation")] == \
        [f"3-1.answer_equation:{n}" for n in (1, 2, 3, 4)]   # 대상별 행은 상세 판정에 남는다 (:none 행 없음)
    absent = next(r for r in plain["results"] if r["check_id"] == "3-1.answer_equation:none")
    assert absent["verdict"] == "PASS" and absent["counted"] and absent["details"]["absent"] is True
    assert "검사 대상 0개" in absent["reason"] and absent["explain"]["headline"] == "검사 대상 없음 — 위반 없음"
    assert absent["evidence"] == [] and absent["measured"] == {"targets": 0, "excluded": 0}
    # 1-2·1-3 의 7개도 같은 규칙 — 코드가 없는 답변은 대분류 1 에서 항상 검사 2(1-1) + 대상 없음 7 = 9 (에러 케이스는 대분류 0, 10-26)
    assert sorted(r["check_id"] for r in plain["results"] if r["check_id"].endswith(":none") and r["category"] == "1") == \
        ["1-2.fence_imbalance:none", "1-2.json_syntax:none", "1-2.orphan_closing_tag:none", "1-2.python_syntax:none",
         "1-3.citation_link:none", "1-3.footnote_link:none", "1-3.internal_link:none"]
    # 재현성: 같은 입력이면 같은 행
    assert engine.evaluate_case({"question": question, "answer": "노트 26권, 펜 34자루, 파일 5개입니다."})["results"] == plain["results"]


def test_python_blocks_count_once_per_answer_but_each_block_keeps_its_row():
    two = engine.evaluate_case({"question": "파이썬 코드를 보여줘.",
                                "answer": "```python\nprint(1)\n```\n\n```python\nfor i in range(3):\nprint(i)\n```"})
    one = engine.evaluate_case({"question": "파이썬 코드를 보여줘.", "answer": "```python\nprint(1)\n```"})
    assert _cat(two, "1") == (8, 1, 9, 10) and _cat(one, "1") == (9, 0, 9, 9)   # 에러 케이스는 대분류 0 (10-26)
    assert [(r["check_id"], r["verdict"]) for r in two["results"] if r["check_id"].startswith("1-2.python_syntax")] == \
        [("1-2.python_syntax:block1", "PASS"), ("1-2.python_syntax:block2", "FAIL")]
    assert not [r for r in two["results"] if r["check_id"] in ("1-2.python_syntax:none", "1-2.fence_imbalance:none")]


def test_excluded_only_unit_is_pass_without_a_confirmed_violation():
    shown = engine.evaluate_case({"question": "계산해줘.", "answer": "틀린 식: 2 + 2 = 5"})
    row = next(r for r in shown["results"] if r["check_id"] == "3-1.answer_equation:none")
    assert row["verdict"] == "PASS" and row["measured"]["excluded"] == 1 and "판정 제외 1건" in row["reason"]
    assert row["explain"]["headline"] == "판정 제외 대상만 있음 — 확인된 위반 없음"
    assert row["details"]["excluded_conditions"][0]["check_id"] == "3-1.answer_equation:skipped"
    assert _cat(shown, "3") == (2, 0, 2, 2)


def test_absent_unit_row_is_not_invented_after_an_execution_error(monkeypatch):
    py = engine.evaluate_case({"question": "파이썬 코드를 작성해줘.", "answer": "```python\n" + "x=1\n" * 60_000 + "```"})
    assert [e["kind"] for e in py["errors"]] == ["LIMIT_EXCEEDED"]
    assert not [r for r in py["results"] if r["check_id"].startswith("1-2.python_syntax")]   # :none 도 없음
    assert [r["check_id"] for r in py["results"] if r["check_id"] == "1-2.json_syntax:none"]  # 오류가 없는 단위는 그대로

    def broken(_ctx):
        raise RuntimeError("test checker failure")

    monkeypatch.setattr(engine, "_COMMON", (("1-2", broken), ("3-1", engine.check_3_1)))
    case = engine.evaluate_case({"question": "설명해줘.", "answer": "설명입니다."})
    assert case["errors"][0]["check_id"] == "1-2.check_error"
    assert not [r for r in case["results"] if r["subcategory"] == "1-2"]
    assert [r["check_id"] for r in case["results"] if r["subcategory"] == "3-1"] == ["3-1.answer_equation:none"]


def test_duplicate_common_condition_rows_are_dropped():
    from code_eval.core import result
    first = result("2-1.literal:forbidden_literal", "2-1", "금지 문자열", "question", Status.EXECUTED, "FAIL",
                   required="x", dedupe_key="forbid|x")
    second = result("2-1.literal:forbidden_literal", "2-1", "금지 문자열", "question", Status.EXECUTED, "FAIL",
                    required="x", dedupe_key="forbid|x")
    other = result("2-1.literal:forbidden_literal", "2-1", "금지 문자열", "question", Status.EXECUTED, "PASS",
                   required="y", dedupe_key="forbid|y")
    kept = engine._drop_duplicates([first, second, other])
    assert kept == [first, other]
    assert all(r.status == Status.EXECUTED for r in kept) and not any(r.status == Status.DUPLICATE for r in kept)


@pytest.mark.parametrize("answer, verdict", [
    ("상자 7개 × 상자마다 구슬 6개 = 7 × 6 = **42개**예요.", "PASS"),   # 말로 쓴 앞부분('… 구슬 6개')은 떼고 나머지만 재계산
    ("상자 7개 × 상자마다 구슬 6개 = 7 × 6 = 48개예요.", "FAIL"),
    ("13묶음 × 묶음당 9장 = 13 × 9\n= (10 × 9) + (3 × 9)\n= 90 + 27\n= **117장**", "PASS"),
])
def test_verbal_expression_before_an_equation_is_not_its_left_side(answer, verdict):
    """2026-10-14 실제 AIONA 답변 오판 — '7개 × 상자마다 구슬 6개 = …' 의 좌변을 '6' 하나로 읽어 FAIL 이 났다."""
    case = engine.evaluate_case({"question": "구슬은 총 몇 개야?", "answer": answer})
    rows = [r for r in case["results"] if str(r["check_id"]).startswith("3-1.answer_equation:") and r["check_id"] != "3-1.answer_equation:none"]
    assert [r["verdict"] for r in rows] == [verdict]


def test_a_question_back_to_the_user_is_not_a_declared_count():
    """2026-10-14 실제 GPT 답변 오판 — '선택지 세 가지를 안내해드릴까요?' 는 개수 선언이 아니다. 진짜 선언은 그대로 잰다."""
    asking = engine.evaluate_case({"question": "선택지 세 개를 안내해줘.",
                                   "answer": "- 어떤 주제의 선택지 세 가지를 안내해드릴까요?\n- 주제를 알려주시면 정리해드릴게요."})
    declared = engine.evaluate_case({"question": "팁을 알려줘", "answer": "다음 3가지 팁을 소개합니다.\n- 하나\n- 둘"})
    count = lambda case: [r["verdict"] for r in case["results"] if str(r["check_id"]).startswith("3-2.declared_count:") and r["check_id"] != "3-2.declared_count:none"]
    assert count(asking) == [] and count(declared) == ["FAIL"]


@pytest.mark.parametrize("question, op, n, exclude_ws", [
    ("감사 문구를 공백 포함 20자로 써줘.", "==", 20, False),          # 세는 방식까지 적은 'N자로' 는 정확히 N자
    ("공백 포함 30자 이내로 요약해줘.", "<=", 30, False),
    ("글자 수는 공백 포함 25자여야 해.", "==", 25, False),
    ("유물 포장 표기 문구를 정확히 20자(공백 포함)로 작성해", "==", 20, False),
    ("띄어쓰기를 빼고 30자 이내로 요약해", "<=", 30, True),
])
def test_char_count_with_a_whitespace_rule_is_recognised(question, op, n, exclude_ws):
    """2026-10-14 326세트 점검 — '공백 포함 N자' 의 '포함' 을 적용 대상 명사로 읽어 조건을 버리고 있었다."""
    from code_eval.extract import extract_conditions
    reqs = [r for r in extract_conditions(question).to_list() if r["kind"] == "char_count"]
    assert [(r["status"], r["value"]["op"], r["value"]["n"], r["value"]["exclude_whitespace"], r["value"]["scope"]) for r in reqs] \
        == [("ok", op, n, exclude_ws, "whole")]


def test_char_count_without_a_rule_or_operator_stays_unsupported():
    from code_eval.extract import extract_conditions
    reqs = [r for r in extract_conditions("20자로 써줘.").to_list() if r["kind"] == "char_count"]
    assert [r["status"] for r in reqs] == ["unsupported"]
