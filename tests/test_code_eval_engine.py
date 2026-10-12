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


# --- 공통 결과 행은 PASS/FAIL 뿐 (v16.3) ----------------------------------------------------------

def _ids(case, prefix):
    return [(r["check_id"], r["verdict"]) for r in case["results"] if r["check_id"].startswith(prefix)]


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
