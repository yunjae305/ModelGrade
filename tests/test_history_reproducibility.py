"""결과 이력 재현성 — 저장 데이터로 다시 낸 지문·해시·집계가 저장값과 같은지, 다르면 무엇이 다른지 (API 호출 없음)."""
import copy

from code_eval.combined import combined_summary
from code_eval.engine import evaluate_batch
from tools.history_reproducibility import check_run
from webapp.version_registry import evaluation_profile, fingerprint


def _run():
    cases = [{"case_id": "Q1", "question": "서울의 인구를 알려줘.", "answer": "약 930만 명입니다."}]
    batch = evaluate_batch(copy.deepcopy(cases))
    batch["jev_eval"] = {"version": "v", "models": ["m"], "summary": {"J1": {"name": "의도", "targets": 1, "pass": 1, "fail": 0}},
                         "records": [{"case_id": "Q1", "status": "SUCCESS", "criteria": ["J1"], "decisions": {"J1": {"criterion_id": "J1", "name": "의도 충족성", "decision": "PASS", "reason": "요청대로 답함"}}}]}
    batch["combined"] = combined_summary(batch)
    run = {"run_id": "r1", "created_at": "2026-10-14", "datasets": {"aiona": cases}, "evaluations": {"aiona": batch},
           "dataset_fingerprint": fingerprint(cases)}
    run["evaluation_profile"] = evaluation_profile(run)
    return run


def test_untouched_saved_run_reproduces_every_check():
    report = check_run(_run(), code_recheck=True)
    assert report["mismatches"] == 0 and {c["status"] for c in report["checks"].values()} == {"일치"}
    assert report["code_recheck"]["providers"]["aiona"]["changed_cases"] == 0


def test_edited_verdict_and_question_are_caught():
    run = _run()
    run["evaluations"]["aiona"]["jev_eval"]["records"][0]["decisions"]["J1"]["decision"] = "FAIL"   # 집계와 맞지 않게 판정만 바뀜
    run["datasets"]["aiona"][0]["question"] = "부산의 인구를 알려줘."                                 # 지문과 맞지 않는 질문
    report = check_run(run)
    assert report["checks"]["dataset_fingerprint"]["status"] == "불일치"
    summary = report["checks"]["aiona.jev_eval.summary"]
    assert summary["status"] == "불일치" and {"J1.pass", "J1.fail"} <= set(summary["changed"])
    assert report["checks"]["aiona.result_digest"]["status"] == "일치"                              # CODE 결과는 그대로
    assert report["mismatches"] >= 2
