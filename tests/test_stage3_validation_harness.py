"""3단계 실사용 검증 도구(tests/live_stage3_validation.py)의 모의 흐름 — 유료 호출 없음."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("live_stage3_validation", Path(__file__).with_name("live_stage3_validation.py"))
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def test_fixture_has_twenty_cases_four_per_group_with_prewritten_expectations():
    fixture = harness.load_fixture()
    groups = [case["group"] for case in fixture["cases"]]
    assert len(groups) == 20 and all(groups.count(group) == 4 for group in harness.GROUPS)
    payloads, targets = harness.build_inputs(fixture["cases"])
    assert len(targets) == 20 and len({(p["question"], p["answer"]) for p in payloads}) == len(payloads)


def test_mock_run_covers_flow_forced_failures_cap_and_resume(tmp_path):
    result = harness.run_mock(harness.load_fixture(), tmp_path)
    assert result["flow"] == dict.fromkeys(result["flow"], True)
    assert [s["scenario"] for s in result["scenarios"] if not s["ok"]] == []
    # 평가기 응답이 기대대로여도 결과가 달라지는 문항만 남는다 (CODE 결과 등) — 흐름 오류가 아님.
    assert result["ideal"]["matches"]["key_match"] + len(result["ideal_mismatches"]) == 20
