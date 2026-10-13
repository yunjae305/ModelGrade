"""저장된 실행의 CODE FAIL 을 현재 엔진으로 다시 검사한 차이 — 읽기 전용, 모델 호출 없음."""
import copy

from code_eval.core import ENGINE_VERSION
from webapp.code_recheck import code_recheck

ANSWER = "- 영업일: 20일 → 26일 = **26 ÷ 20 = 1.3배 (+30%)**"   # v16.2.x 는 '26 = 26 / 20 = 1.3' 로 읽어 FAIL


def stored_run(engine="aiona-code-eval-v16.2.5"):
    fail = {"check_id": "3-1.answer_equation:1", "area": "common", "counted": True, "verdict": "FAIL"}
    return {"status": "COMPLETED", "datasets": {"claude": [{"case_id": "Q1", "question": "계산해줘.", "answer": ANSWER}]},
            "evaluations": {"claude": {"versions": {"engine": engine},
                                       "cases": [{"case_status": "EVALUATED", "results": [fail]}], "jev_eval": {"records": [{}]}}}}


def test_old_engine_fail_that_current_engine_no_longer_produces_is_reported_without_changing_the_run():
    run = stored_run()
    before = copy.deepcopy(run)
    out = code_recheck(run)
    assert out["engine"] == ENGINE_VERSION
    entry = out["providers"]["claude"]
    assert entry["stored_engine"] == "aiona-code-eval-v16.2.5" and entry["same_engine"] is False
    assert entry["cases"] == {"0": {"cleared": ["3-1.answer_equation:1"], "added": []}}
    assert run == before   # 저장된 판정은 바꾸지 않는다


def test_same_engine_is_not_rechecked():
    entry = code_recheck(stored_run(ENGINE_VERSION))["providers"]["claude"]
    assert entry["same_engine"] is True and entry["cases"] == {}
