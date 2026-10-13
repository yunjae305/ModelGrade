"""질문 세트 사전 점검 보고서 — 30개 공식 판정(질문·대화 단위), 대화 턴 점검, 답변 수집 상태, HTML 이스케이프 (모델 호출 없음)."""
import json

from tools.check_question_set import check_items, load_items, main as check_main
from tools.question_set_report import ROOT, analyze, main, render_html

LONGCTX = ROOT / "examples" / "AIONA_winz_longctx_J6_v1.json"


def conversation(cid, turns):
    return [{"case_id": f"{cid}-T{t}", "conversation_id": cid, "turn": t, "question": f"{cid} 대화 {t}번째 일정을 정리해줘."}
            for t in range(1, turns + 1)]


def run(items):
    return analyze(check_items(items, pipeline="items"), items)


def test_ten_turn_conversation_with_j6_on_nine_turns_is_one_applicable_conversation():
    j6 = run(conversation("A", 10))["criteria"]["J6"]
    assert (j6["turns"], j6["units"], j6["units_conversations"], j6["enough"], j6["short"]) == (9, 1, 1, False, 29)


def test_two_conversations_each_applying_a_criterion_many_times_count_two():
    analysis = run(conversation("A", 4) + conversation("B", 5))
    assert (analysis["criteria"]["J6"]["turns"], analysis["criteria"]["J6"]["units"]) == (7, 2)
    assert (analysis["criteria"]["CODE 1"]["turns"], analysis["criteria"]["CODE 1"]["units"]) == (9, 2)


def test_29_distinct_questions_are_short_and_30_are_enough_while_the_cli_still_counts_turns():
    singles = [{"question": f"{n}번 메모를 한 줄로 정리해줘."} for n in range(29)]
    short = run(singles)["criteria"]["CODE 1"]
    assert (short["units"], short["enough"], short["short"]) == (29, False, 1)
    enough = run(singles + [{"question": "30번 메모를 한 줄로 정리해줘."}])["criteria"]["CODE 1"]
    assert (enough["units"], enough["enough"], enough["short"]) == (30, True, 0)
    turns_only = run(conversation("A", 15) + conversation("B", 16))   # 31턴이지만 대화 2개
    code1 = turns_only["criteria"]["CODE 1"]
    assert (code1["turns"], code1["cli_enough"], code1["units"], code1["enough"]) == (31, True, 2, False)
    assert "CODE 1" in turns_only["cli_differs"] and "CODE 1" in turns_only["short"]


def test_singles_and_conversations_mixed_and_context_attributes():
    items = [{"case_id": f"C{t}", "conversation_id": "C", "turn": t, "question": f"{t}번째 일정을 정리해줘.", "type": "멀티턴 대화"}
             for t in (1, 2, 3)] + [{"case_id": "Q1", "question": "파이썬 함수를 작성해줘.", "type": "코딩·기술"}]
    analysis = run(items)
    assert (analysis["questions"], analysis["turns"]) == (2, 4)
    j6, code1 = analysis["criteria"]["J6"], analysis["criteria"]["CODE 1"]
    assert (j6["turns"], j6["units"], j6["units_conversations"], j6["short"]) == (2, 1, 1, 29)
    assert (code1["turns"], code1["units"], code1["units_singles"], code1["units_conversations"]) == (4, 2, 1, 1)
    context = analysis["context"]
    assert (context["multi_turn"], context["single"]) == (1, 1)
    assert context["long_context"] == {"measured": False, "count": 0, "reference_bins": ["4K", "8K", "16K", "32K"]}   # 토큰 미측정 = 충족 아님
    multi = next(t for t in analysis["types"] if t["name"] == "멀티턴 대화")
    assert (multi["turns"], multi["turn_ratio"], multi["units"], multi["unit_ratio"]) == (3, 0.75, 1, 0.5)   # 턴 비율 ≠ 단위 비율


def test_conversation_turn_problems_and_answer_status():
    items = [{"conversation_id": "D", "turn": 1, "question": "가상의 모임 계획이야.", "answer": "네."},
             {"conversation_id": "D", "turn": 3, "question": "정원을 바꿔줘.", "answer": ""},
             {"conversation_id": "D", "turn": 3, "question": "다시 정리해줘.", "answer": ""}]
    analysis = analyze(check_items(items, pipeline="items"), items)
    row = analysis["conversation_rows"][0]
    assert (row["missing"], row["duplicated"], row["answered"]) == ([2], [3], 1)
    assert analysis["status"]["code"] == "blocked"
    no_answers = [{"question": "서울의 인구를 알려줘."}]
    assert analyze(check_items(no_answers, pipeline="items"), no_answers)["status"]["code"] == "questions_only"


def test_html_escapes_question_text_and_states_it_is_not_an_evaluation():
    attack = "<script>alert(1)</script> 표로 정리해줘."
    items = [{"case_id": "A", "question": attack}, {"case_id": '"><img src=x onerror=alert(1)>', "question": attack}]
    page = render_html(analyze(check_items(items, pipeline="items"), items), "<b>set</b>.json", generated_at="2026-10-13 00:00")
    assert "<script" not in page and "<img" not in page and "<b>set" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&lt;img src=x onerror=alert(1)&gt;" in page
    assert "default-src 'none'" in page and "실제 Claude 의미 평가(PASS·FAIL) 결과가 아닙니다" in page


def test_cli_renders_from_check_question_set_json(tmp_path, capsys):
    source = tmp_path / "set.json"
    source.write_text(json.dumps([{"question": "세 문장으로 요약해줘.", "answer": "요약입니다."}], ensure_ascii=False), encoding="utf-8")
    assert check_main([str(source), "--json", "--pipeline", "items"]) == 0
    check_json = tmp_path / "check.json"
    check_json.write_text(capsys.readouterr().out, encoding="utf-8")
    out = tmp_path / "report.html"
    assert main([str(source), "--check-json", str(check_json), "--out", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    assert "질문 세트 사전 점검" in page and "1/1" in page and "평가 실행 가능" in page


def test_longctx_example_counts_and_readiness():
    items = load_items(LONGCTX)
    analysis = run(items)
    assert (analysis["questions"], analysis["conversations"], analysis["turns"], analysis["answered"]) == (24, 24, 231, 0)
    j6 = analysis["criteria"]["J6"]
    assert (j6["turns"], j6["units"], j6["enough"], j6["short"], j6["cli_enough"]) == (207, 24, False, 6, True)
    assert all(row["units"] <= 24 and not row["enough"] for row in analysis["criteria"].values())   # 공식: 모든 기준 30개 미만
    assert analysis["criteria"]["CODE 3"]["short"] == 30 and analysis["context"]["multi_turn"] == 24
    assert not any(r["missing"] or r["duplicated"] for r in analysis["conversation_rows"])
    assert analysis["status"]["code"] == "questions_only" and analysis["types"] is None
