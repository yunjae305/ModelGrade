"""References and source ambiguity found in the follow-up implementation review."""

import pytest

from code_eval.engine import evaluate_case
from code_eval.model import Status
from code_eval.textparse import link_targets


def evaluate(answer, question="내용을 설명해줘."):
    return evaluate_case({"question": question, "answer": answer})


def row(case, check_id):
    return next(r for r in case["results"] if r["check_id"] == check_id)


def test_unknown_footnote_definition_makes_no_row_and_later_references_continue():
    answer = "본문[^x]\n\n[x]: 각주 설명\n\n# 개요\n[없는 대상](#missing)"
    case = evaluate(answer)
    assert not [r for r in case["results"] if r["check_id"] == "1-3.footnote_link"]
    assert any(s["check_id"] == "1-3.footnote_link" for s in case["meta"]["skipped_conditions"])
    assert row(case, "1-3.internal_link")["verdict"] == "FAIL"
    assert case["errors"] == []


@pytest.mark.parametrize("extra", [
    "\n추가 내용[^y]\n[^y]:",
    "\n추가 내용[^y]\n[^y]: 첫 정의\n[^y]: 중복 정의",
    "\n추가 내용[^y]",
])
def test_unknown_definition_does_not_hide_confirmed_footnote_failure(extra):
    result = row(evaluate("본문[^x]\n\n[x]: 각주 설명" + extra), "1-3.footnote_link")
    assert result["status"] == Status.EXECUTED and result["verdict"] == "FAIL"


@pytest.mark.parametrize("answer", [
    "각주 표기는 `[^x]`처럼 씁니다.",
    "각주 표기는 ``[^x]``처럼 씁니다.",
    "각주 표기는 `[각주1]`처럼 씁니다.",
    "인용 번호는 `[1]`처럼 씁니다.",
    "내부 링크는 `[본문](#missing)`처럼 씁니다.\n# 실제 제목",
    "```markdown\n본문[^x] [1] [링크](#missing)\n```",
])
def test_code_reference_examples_do_not_create_reference_checks(answer):
    assert not [r for r in evaluate(answer)["results"] if r["subcategory"] == "1-3"]


def test_literal_reference_example_does_not_mask_real_missing_footnote():
    answer = "각주 표기는 `[^x]`처럼 씁니다. 실제 내용[^y]"
    result = row(evaluate(answer), "1-3.footnote_link")
    assert result["verdict"] == "FAIL"
    assert result["measured"]["used"] == ["y"]
    assert result["evidence"][0]["start"] == answer.index("[^y]")


@pytest.mark.parametrize("example", [
    '```html\n<div id="target">예시</div>\n```',
    '`<div id="target">예시</div>`',
])
def test_code_anchor_is_not_a_target_for_a_real_link(example):
    answer = '[설명으로](#target)\n\n# 개요\n\n' + example
    result = row(evaluate(answer), "1-3.internal_link")
    assert result["verdict"] == "FAIL"
    assert result["measured"]["missing"] == ["target"]


def test_code_anchor_does_not_duplicate_real_target():
    answer = '[설명으로](#target)\n<div id="target">본문</div>\n```html\n<div id="target">예시</div>\n```'
    result = row(evaluate(answer), "1-3.internal_link")
    assert result["verdict"] == "PASS" and not result["measured"]["ambiguous"]


def test_fenced_anchor_is_also_excluded_by_target_parser():
    targets, duplicate = link_targets('# 개요\n```html\n<div id="target">예시</div>\n```')
    assert targets == {"개요": 1} and duplicate == []


def test_code_only_alternative_definition_does_not_hide_missing_footnote():
    result = row(evaluate("본문[^x]\n```markdown\n[x]: 설명 예시\n```"), "1-3.footnote_link")
    assert result["status"] == Status.EXECUTED and result["verdict"] == "FAIL"


@pytest.mark.parametrize("sources", [
    "```json\n[3, 1]\n```\n두 번째 배열: [4, 2]",
    "첫 번째 배열: [4, 2]\n```json\n[3, 1]\n```",
    "```json\n[3, 1]\n```\n```json\n[4, 2]\n```",
    "첫 번째 배열: [3, 1]\n두 번째 배열: [4, 2]",
])
def test_multiple_json_sources_cannot_be_silently_reduced_to_one(sources):
    question = "아래 두 배열을 오름차순으로 정렬해줘.\n" + sources
    case = evaluate("```json\n[1, 3]\n```", question)
    # 원본 후보가 둘이면 하나를 골라 PASS/FAIL 하지 않는다 — 변환 조건 자체를 만들지 않음(행·skipped 모두 없음).
    assert not [r for r in case["results"] if r["check_id"] == "3-3.transform"]
    assert not [s for s in case["meta"]["skipped_conditions"] if s["check_id"] == "3-3.transform"]
    assert not [c for c in case["meta"]["question_conditions"] if c["kind"] == "transform"]


@pytest.mark.parametrize("source", ["```json\n[3, 1]\n```", "원본: [3, 1]"])
def test_single_source_is_not_double_counted(source):
    result = row(evaluate("```json\n[1, 3]\n```", "아래 배열을 오름차순으로 정렬해줘.\n" + source),
                 "3-3.transform")
    assert result["verdict"] == "PASS"


def test_nested_arrays_inside_one_fenced_source_are_not_extra_sources():
    source = '[{"items": [3, 1]}, {"items": [4, 2]}]'
    question = "원본 데이터를 그대로 보존해줘.\n```json\n" + source + "\n```"
    result = row(evaluate("```json\n" + source + "\n```", question), "3-3.transform")
    assert result["verdict"] == "PASS"
