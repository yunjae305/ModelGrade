"""질문 세트 사전 점검 보고서 — 턴·질문 단위 집계, 대화 턴 점검, 답변 수집 상태, HTML 이스케이프 (모델 호출 없음)."""
import json

from tools.check_question_set import check_items, main as check_main
from tools.question_set_report import ROOT, analyze, main, render_html

LONGCTX = ROOT / "examples" / "AIONA_winz_longctx_J6_v1.json"


def test_turns_and_question_units_are_counted_separately():
    items = [{"case_id": f"C{t}", "conversation_id": "C", "turn": t, "question": f"{t}번째 일정을 정리해줘.", "type": "멀티턴 대화"}
             for t in (1, 2, 3)] + [{"case_id": "Q1", "question": "파이썬 함수를 작성해줘.", "type": "코딩·기술"}]
    analysis = analyze(check_items(items, pipeline="items"), items)
    assert (analysis["questions"], analysis["turns"]) == (2, 4)
    j6, code1 = analysis["criteria"]["J6"], analysis["criteria"]["CODE 1"]
    assert (j6["turns"], j6["units"], j6["units_conversations"], j6["turns_short"], j6["units_short"]) == (2, 1, 1, 28, 29)
    assert (code1["turns"], code1["units"], code1["units_singles"]) == (4, 2, 1)
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
    from tools.check_question_set import load_items
    items = load_items(LONGCTX)
    analysis = analyze(check_items(items, pipeline="items"), items)
    assert (analysis["questions"], analysis["conversations"], analysis["turns"], analysis["answered"]) == (24, 24, 231, 0)
    assert (analysis["criteria"]["J6"]["turns"], analysis["criteria"]["J6"]["units"]) == (207, 24)
    assert not any(r["missing"] or r["duplicated"] for r in analysis["conversation_rows"])
    assert analysis["status"]["code"] == "questions_only" and analysis["types"] is None
