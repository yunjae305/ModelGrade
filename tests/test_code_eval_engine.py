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
    assert [case["case_id"] for case in batch["cases"]] == ["a", "b"]
    assert all(case["results"][0]["status"] == Status.CHECK_ERROR and
               case["results"][0]["verdict"] is None for case in batch["cases"])


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
    assert found["2-2.table:count"]["status"] == Status.DEPENDENCY_FAILED
    assert found["2-2.table:count"]["verdict"] is None


@pytest.mark.parametrize("question,answer", [
    ("아래 상품 데이터를 JSON으로 변환해 주세요. 상품: 헤드셋 119000원 / 웹캠 55000원",
     '{"items": [{"name": "헤드셋", "price": 119000}]}'),
    ("아래 상담 기록을 표로 정리해 주세요. 원본 그대로 정확하게 옮겨 주세요.\n---\n상담 기록: 8월 1일 배송 문의",
     "| 날짜 | 내용 |\n|---|---|\n| 8월 1일 | 배송 문의 |"),
])
def test_unparsed_source_transformation_is_reported_without_quality_verdict(question, answer):
    case = engine.evaluate_case({"question": question, "answer": answer})
    rows = [r for r in case["results"] if r["check_id"] == "3-3.transform"]
    assert len(rows) == 1
    assert rows[0]["status"] == Status.UNSUPPORTED
    assert rows[0]["verdict"] is None and rows[0]["reason"]


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
