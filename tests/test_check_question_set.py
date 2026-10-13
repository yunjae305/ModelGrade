"""질문 세트 무료 점검 도구 — 질문 단위·형식 오류·중복·기준별 적용 수 (모델 호출 없음)."""
import json

from tools.check_question_set import check_items, load_items, main


def items():
    return [
        {"case_id": "Q1", "question": "안내문을 세 문장으로 작성하고 표로 정리해줘."},
        {"case_id": "Q2", "question": "  안내문을 세 문장으로   작성하고 표로 정리해줘. "},            # 공백만 다른 중복
        {"case_id": "C1", "conversation_id": "C", "turn": 1, "question": "가상의 모임 계획을 세울게. 정원은 12명이야."},
        {"case_id": "C2", "conversation_id": "C", "turn": 2, "question": "정원을 15명으로 바꿔서 다시 정리해줘."},
        {"case_id": "BAD", "question": ""},
        {"case_id": "D2", "conversation_id": "D", "turn": 2, "question": "이어서 정리해줘."},          # 1턴 없는 대화
    ]


def test_counts_units_errors_duplicates_and_applicable_criteria():
    report = check_items(items())
    assert (report["questions"], report["single_questions"], report["conversations"], report["conversation_turns"]) == (4, 2, 2, 3)
    messages = [e["message"] for e in report["errors"]]
    assert any("question" in m for m in messages) and any("대화 D" in m for m in messages)
    assert [d["case_id"] for d in report["duplicates"]] == ["Q2"]
    criteria = report["criteria"]
    assert criteria["CODE 1"]["applicable"] == 5 and criteria["CODE 2"]["applicable"] >= 2   # 세 문장·표 요구
    assert criteria["J6"]["applicable"] >= 1 and "J6" in report["short_criteria"]               # 2턴부터, 30개 미만이면 부족


def test_cli_reads_jsonl_and_reports_type_distribution(tmp_path, capsys):
    path = tmp_path / "set.jsonl"
    path.write_text("\n".join(json.dumps({"question": q, "type": t}, ensure_ascii=False)
                              for q, t in [("서울의 인구를 알려줘.", "정보 검색·사실 확인"), ("파이썬 함수를 작성해줘.", "코딩·기술")]), encoding="utf-8")
    assert len(load_items(path)) == 2
    assert main([str(path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["types"]["코딩·기술"] == {"count": 1, "ratio": 0.5, "target": 0.15}
