"""질문 세트 무료 점검 도구 — 질문 단위·형식 오류·중복·기준별 적용 수 (모델 호출 없음)."""
import json

import pytest

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


def test_unjudged_question_conditions_items_rules_and_engine_input_errors():
    report = check_items([{"question": "3 + 4는 얼마야?"}], pipeline="legacy")
    assert report["criteria"]["CODE 3"]["applicable"] == 1          # 답변이 없어도 질문의 계산 조건은 적용
    plan = [{"question": f"가상의 모임 계획을 세울게. 정원은 {n}명이야."} for n in range(10, 12)]
    assert check_items(plan, pipeline="items")["criteria"]["J5"]["applicable"] == 2   # items 의 J5-2.a
    bad = check_items([{"question": "안녕?", "reference": 123}], pipeline="legacy")
    assert bad["errors"] and bad["criteria"]["J1"]["applicable"] == 0


def test_error_turns_are_left_out_of_conversation_history_like_the_evaluation():
    convo = [{"conversation_id": "C", "turn": 1, "question": "가상의 도시야.", "reference": 123},
             {"conversation_id": "C", "turn": 2, "question": "서울의 인구를 알려줘."}]
    report = check_items(convo, pipeline="items")
    assert report["errors"] and report["criteria"]["J6"]["applicable"] == 0   # 1턴 오류 — 이력이 불완전해 J6 없음
    assert report["criteria"]["J5"]["applicable"] == 1                         # 오류 턴의 '가상' 설정은 이력에 들어가지 않음


def test_official_judgement_counts_questions_and_conversations_and_turns_stay_as_reference(capsys):
    turns = [{"conversation_id": cid, "turn": t, "question": f"{cid} 대화 {t}번째 일정을 정리해줘."}
             for cid, n in (("A", 15), ("B", 16)) for t in range(1, n + 1)]
    report = check_items(turns, pipeline="items")
    code1, j6 = report["criteria"]["CODE 1"], report["criteria"]["J6"]
    assert (code1["applicable"], code1["applicable_turns"], code1["applicable_units"], code1["enough"], code1["short"]) == (31, 31, 2, False, 28)
    assert (j6["applicable"], j6["applicable_units"], j6["units_conversations"]) == (29, 2, 2)   # 같은 대화의 여러 턴은 1
    assert report["count_unit"] == "question_or_conversation" and report["cases"][1]["conversation_id"] == "A"
    singles = [{"case_id": "SAME", "question": f"{n}번 메모를 한 줄로 정리해줘."} for n in range(30)]   # 같은 ID 라도 항목은 30개
    assert check_items(singles, pipeline="items")["criteria"]["CODE 1"]["enough"] is True
    assert check_items(singles[:29], pipeline="items")["criteria"]["CODE 1"]["short"] == 1


def test_min_applicable_option_changes_only_the_target_count(tmp_path, capsys):
    singles = [{"question": f"{n}번 메모를 한 줄로 정리해줘."} for n in range(12)]
    assert check_items(singles, pipeline="items", minimum=10)["criteria"]["CODE 1"]["enough"] is True
    report = check_items(singles, pipeline="items", minimum=20)
    assert (report["min_applicable"], report["criteria"]["CODE 1"]["short"]) == (20, 8)
    path = tmp_path / "set.jsonl"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in singles), encoding="utf-8")
    assert main([str(path), "--json", "--pipeline", "items", "--min-applicable", "5"]) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["min_applicable"] == 5 and cli["criteria"]["CODE 1"]["enough"] is True
    assert check_items(singles, pipeline="items")["min_applicable"] == 30   # 기본은 잠정 운영 기준 30 그대로



def test_precheck_counts_j8_for_questions_with_attachments_like_the_evaluation():
    """3차 재현 — 사전 점검이 J8 적용 판정에 첨부를 넘기지 못해 첨부 질문도 J8 0개로 셌다."""
    attached = [{"question": "첨부한 견적서의 합계를 알려줘.", "attachments": [{"name": "견적서.txt", "type": "txt", "content": "합계 1,250,000원"}]},
                {"question": "서울의 인구를 알려줘."}]
    for pipeline in ("legacy", "items"):
        assert check_items(attached, pipeline=pipeline)["criteria"]["J8"]["applicable_units"] == 1


@pytest.mark.multi_turn_rule
def test_unanswered_conversation_counts_j6_by_turns_until_answers_arrive():
    """답변이 없는 10턴 대화는 대화 전체 10K 토큰을 아직 잴 수 없어 J6 적용으로 센다 — 답변이 있으면 실제 길이로 판정 (multi-turn-v2)."""
    turns = [{"question": f"{n}번째: 앞에서 정한 표 형식을 유지해서 다시 정리해 줘.", "answer": "", "conversation_id": "c1", "turn": n}
             for n in range(1, 11)]
    report = check_items(turns, "items")
    assert report["criteria"]["J6"]["applicable_units"] == 1 and report["multi_turn"]["c1"]["answers_pending"]
    answered = [dict(t, answer="네, 정리했습니다.") for t in turns]   # 답변을 받은 뒤에는 짧은 대화라 멀티턴이 아니다
    report = check_items(answered, "items")
    assert report["criteria"]["J6"]["applicable_units"] == 0 and report["multi_turn"]["c1"]["multi_turn"] is False
