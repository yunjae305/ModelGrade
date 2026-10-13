"""설계안 v1.1 코드 평가 기준표 데이터(검사 28·세부 조건 194·PASS/FAIL 예시)가 엔진 목록·실행과 어긋나지 않는지 확인한다."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_eval.criteria_registry import CODE_UNITS, unit_of
from code_eval.engine import evaluate_case

DATA = Path(__file__).resolve().parents[1] / "code_eval" / "settings" / "code_check_conditions.json"
CONDITIONS = json.loads(DATA.read_text(encoding="utf-8"))
#: 설계안 예시가 현재 엔진(v16.3.0)에서 결과 행을 만들지 않는 검사 — 고유성 조건 추출 보강 전까지 기록만 한다
KNOWN_GAPS = {"3-3.unique_values"}


def test_counts_and_ids_match_the_registry():
    checks = CONDITIONS["checks"]
    assert len(checks) == 28 and sum(len(c["conditions"]) for c in checks) == 194
    assert [c["id"] for c in checks] == [unit for unit, _, _, _ in CODE_UNITS if not unit.startswith("AIONA")]
    assert len(CONDITIONS["categories"]) == 3 and sum(len(k["subs"]) for k in CONDITIONS["categories"]) == 8
    subs = {s["id"] for k in CONDITIONS["categories"] for s in k["subs"]}
    for check in checks:
        assert check["sub"] in subs and check["category"] == check["sub"].split("-")[0] and check["description"]
        assert check["conditions"] and all(c["title"] and c["text"] and c["source"] in ("question", "answer", "policy") for c in check["conditions"])
        assert set(check["examples"]) == {"PASS", "FAIL"} and all(ex["note"] and ex["question"] for ex in check["examples"].values())


@pytest.mark.parametrize("check_id,verdict", [(c["id"], v) for c in CONDITIONS["checks"] for v in ("PASS", "FAIL")])
def test_design_examples_reproduce_in_the_engine(check_id, verdict):
    example = next(c for c in CONDITIONS["checks"] if c["id"] == check_id)["examples"][verdict]
    case = evaluate_case({"question": example["question"], "answer": example["answer"]})
    assert case["errors"] == []
    verdicts = [r["verdict"] for r in case["results"] if r["counted"] and unit_of(r["check_id"]) == check_id]
    if check_id in KNOWN_GAPS:
        assert verdicts == [], f"{check_id} 가 이제 행을 만든다 — KNOWN_GAPS 에서 빼고 예시를 검증할 것"
        return
    assert verdicts and (verdict in verdicts if verdict == "FAIL" else all(v == "PASS" for v in verdicts)), (check_id, verdict, verdicts)
