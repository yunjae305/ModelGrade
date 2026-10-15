"""통합 평가기준 목록이 실제 실행 경로와 어긋나지 않는지 확인한다 (모델 호출 없음)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from code_eval import criteria_registry as reg
from code_eval.core import CHECK_VERSIONS
from code_eval.engine import evaluate_case
from code_eval.jev_criteria import CRITERIA
from code_eval.model import SUBCATEGORIES
from code_eval.taxonomy import EXAMPLES

ROOT = Path(__file__).resolve().parents[1]


def test_every_check_unit_and_semantic_criterion_is_listed_once():
    ids = [c["id"] for c in reg.registry()["criteria"]]
    assert len(ids) == len(set(ids))
    assert {c["id"] for c in reg.registry()["criteria"] if c["layer"] == "CODE"} == set(CHECK_VERSIONS)
    assert [c["id"] for c in reg.registry()["criteria"] if c["layer"] == "LLM"] == [c.id for c in CRITERIA]
    assert {c["group"] for c in reg.registry()["criteria"] if c["category"] != "AIONA" and c["layer"] == "CODE"} == set(SUBCATEGORIES)


def test_check_ids_written_in_checkers_belong_to_a_listed_unit():
    # Every literal check id the checkers can emit must resolve to a registered unit.
    found = set()
    for path in (ROOT / "code_eval").glob("checks_*.py"):
        found |= set(re.findall(r"[\"'](?:f\")?((?:[123]-[123]|AIONA-[MF])\.[a-z_]+)", path.read_text(encoding="utf-8")))
    assert found
    assert found <= set(CHECK_VERSIONS)  # check_error 는 결과 행 check_id 가 아니다 (사례 errors 에만 남음)


QUESTIONS = [(e["question"], e["answer"]) for e in EXAMPLES] + [
    ("JSON 객체로 답해줘. 키는 \"name\", \"age\" 이고 \"age\"는 숫자야.", '{"name": "a", "age": 3}'),
    ("답변을 '결론:'으로 시작하고 '감사합니다'로 끝내줘. '죄송'이라는 말은 쓰지 마.", "결론: 됩니다. 감사합니다"),
    ("한글만 사용해서 200자 이하로 써줘.", "안녕하세요"),
    ("아무것도 출력하지 마.", ""),
    ("python 코드블록으로 1을 출력해줘.", "```python\nprint(1)\n```"),
    ("소제목을 붙여 설명해줘. 번호 목록으로 정리해줘.", "## 가\n1. 나"),
    ("공부 방법을 정확히 3개 추천해줘. 3문장, 2문단, 4줄, 10어절로 써줘.", "가"),
    ('다음 JSON 배열에서 중복 없이 "score" 합계를 구해줘.\n```json\n[{"score": 1}, {"score": 2}]\n```', "3"),
]


@pytest.mark.parametrize("question,answer", QUESTIONS)
def test_extracted_requirement_kinds_map_to_the_units_that_run(question, answer):
    case = evaluate_case({"question": question, "answer": answer})
    # 판정하지 않은 조건은 행이 없고 meta.skipped_conditions 에 check_id 만 남는다 — 그 단위도 실행된 것으로 본다.
    units = ({reg.unit_of(r["check_id"]) for r in case["results"]}
             | {reg.unit_of(s["check_id"]) for s in case["meta"]["skipped_conditions"]})
    assert case["errors"] == []
    by_kind = {}
    for unit, _, _, kinds in reg.CODE_UNITS:
        for kind in kinds:
            by_kind.setdefault(kind, set()).add(unit)
    kinds = {cond["kind"] for cond in case["meta"]["question_conditions"]}
    subsumed = {kind: unit for kind, when, unit, _ in reg.SUBSUMED_KINDS if when in kinds}
    for cond in case["meta"]["question_conditions"]:
        kind = cond["kind"]
        assert kind in by_kind or kind in reg.MODIFIER_KINDS, kind
        if kind in by_kind and cond.get("status") == "ok":
            assert by_kind[kind] & units or subsumed.get(kind) in units, (kind, sorted(units))


def test_relations_refer_to_listed_criteria():
    ids = {c["id"] for c in reg.registry()["criteria"]} | set(SUBCATEGORIES)
    for relation in reg.registry()["relations"]:
        assert {relation["a"], relation["b"]} <= ids
        assert relation["kind"] in ("same_cause", "boundary", "overlap", "gap")


def test_approved_gaps_are_now_covered_by_a_running_check():
    # 이전 턴 형식 지시(CODE 승계)와 답변 출처 지지(J5 citation_checks)는 더 이상 공백이 아니다.
    relations = reg.registry()["relations"]
    assert not [r for r in relations if r["kind"] == "gap"]
    assert {(r["a"], r["b"]) for r in relations if r["kind"] == "boundary"} >= {("2-1", "J6"), ("2-2", "J6"), ("1-3.citation_link", "J5")}


def test_evaluation_targets_are_pinned():
    """평가 기준을 한 곳에 고정 — 개수가 바뀌면 이 테스트와 설계안·변경내역을 함께 고친다 (사용자 결정 2026-10-14)."""
    targets = reg.evaluation_targets()
    assert targets["counts"] == {"code_categories": 3, "code_subcategories": 8, "code_checks": 28,
                                 "semantic_criteria": 8, "semantic_subs": 30, "semantic_items": 44, "total": 72}
    code = targets["code"]
    assert len(code["always_checks"]) + len(code["answer_checks"]) + len(code["question_checks"]) == 28 == len(set(code["checks"]))
    assert "conditions" not in code   # 질문 조건 종류는 기준이 아니다 — 단위는 검사 28 + 항목 44 하나 (사용자 결정 2026-10-15)
    assert targets["semantic"]["criteria"] == [f"J{i}" for i in range(1, 9)]
    assert reg.registry()["targets"] == targets


def test_generator_and_check_tools_use_the_registry_targets():
    from tools import check_question_set as cqs
    from webapp import question_generation as qg
    from tools import min_question_count as mq
    targets = reg.evaluation_targets()
    catalog = qg.criteria_catalog()
    assert [c["id"] for c in catalog["code"]] == targets["code"]["checks"]
    names = mq.all_targets(cqs.check_items([], pipeline="items"))
    skip = set(targets["code"]["answer_checks"]) | set(cqs.ANSWER_DEPENDENT_ITEMS)
    assert names == [t for t in targets["code"]["checks"] + targets["semantic"]["items"] if t not in skip] and len(names) == 64
    assert [i["id"] for i in catalog["items"]] == reg.evaluation_targets()["semantic"]["items"]
