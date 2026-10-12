"""근거 없는 J1~J4 FAIL: 형식 검증 → 해당 기준만 1회 재평가 → 그래도 없으면 판정 미완료 (모델 호출 없음)."""
from __future__ import annotations

import copy
from types import SimpleNamespace

from code_eval.combined import combined_summary
from code_eval.engine import evaluate_batch
from code_eval.jev_client import JevCall
from code_eval.jev_eval import evaluate_jev_batch, evaluate_jev_case
from code_eval.config import load_aiona_config
from code_eval.semantic_reasons import MISSING_EVIDENCE_CODE, fail_evidence

QUESTION = "재택근무의 장점과 단점을 비교해서 설명해줘."
ANSWER = "재택근무는 출퇴근 시간이 줄어듭니다. 교통비도 아낄 수 있습니다."


def entry(choice, reason=""):
    probs = {} if choice == "UNVERIFIED" else {"PASS": .9, "FAIL": .1} if choice == "PASS" else {"PASS": .1, "FAIL": .9}
    return {"choice": choice, "confidence": .8, "probabilities": probs, "reason": reason}


class SequenceJudge:
    """호출마다 준비한 응답을 순서대로 돌려주고, 물은 기준을 기록한다."""
    config = SimpleNamespace(engine="anthropic_api", to_manifest=lambda: {"engine": "anthropic_api"})

    def __init__(self, *responses):
        self.responses, self.asked = list(responses), []

    def system_one(self, state, questions):
        self.asked.append(sorted(questions))
        response = self.responses.pop(0)
        if isinstance(response, JevCall):
            return response
        return JevCall("OK", body={"model": "fixture", "answers": {cid: copy.deepcopy(response[cid]) for cid in questions}},
                       attempts=1, usage={"input_tokens": 10})


#: 배치 실행에서는 API 평가자에게 J5 사실 선별이 함께 붙는다.
NO_FACTS = {"choice": "NOT_APPLICABLE", "confidence": .9, "reason": "사실 주장 없음", "fact_check": {"basis": "none", "claims": []}}


def run(judge, criteria=("J1", "J4")):
    return evaluate_jev_case("q", QUESTION, ANSWER, judge, criteria=criteria, fail_evidence_check=True)


def test_quoted_fail_is_confirmed_without_reevaluation():
    judge = SequenceJudge({"J1": entry("PASS", "비교했다"), "J4": entry("FAIL", "'교통비도 아낄 수 있습니다'처럼 장점만 있고 단점이 없음")})
    record = run(judge)
    assert judge.asked == [["J1", "J4"]] and "reevaluation" not in record
    assert record["decisions"]["J4"]["decision"] == "FAIL"
    assert record["decisions"]["J4"]["fail_evidence"]["valid"] is True
    assert record["decisions"]["J4"]["fail_evidence"]["matched_quotes"][0]["found_in"] == "answer"


def test_fail_without_evidence_is_reevaluated_once_for_that_criterion_only():
    judge = SequenceJudge({"J1": entry("PASS", "비교했다"), "J4": entry("FAIL", "")},
                          {"J4": entry("FAIL", "단점 요청에 '출퇴근 시간이 줄어듭니다'만 답해 단점이 빠짐")})
    record = run(judge)
    assert judge.asked == [["J1", "J4"], ["J4"]]
    j4 = record["decisions"]["J4"]
    assert j4["decision"] == "FAIL" and j4["fail_evidence"]["valid"]
    assert j4["reevaluation"]["first"]["fail_evidence"]["problems"] == ["reason_missing"]
    assert j4["reevaluation"]["final"] == "FAIL"
    assert record["decisions"]["J1"]["decision"] == "PASS"
    assert record["reevaluation"]["criteria"] == ["J4"] and record["reevaluation"]["status"] == "SUCCESS"
    assert record["usage"]["input_tokens"] == 20 and record["attempts"] == 2


def test_reevaluated_pass_replaces_the_unsupported_fail():
    judge = SequenceJudge({"J4": entry("FAIL", "핵심이 빠짐")}, {"J4": entry("PASS", "장단점을 모두 다룸")})
    record = run(judge, ("J4",))
    assert record["decisions"]["J4"]["decision"] == "PASS"
    assert record["decisions"]["J4"]["reevaluation"]["first"]["fail_evidence"]["problems"] == ["quote_missing"]
    assert record["review_required"] is False


def test_still_unsupported_after_reevaluation_stays_incomplete_not_fail():
    judge = SequenceJudge({"J4": entry("FAIL", "")}, {"J4": entry("FAIL", "'없는 문장'을 근거로 듦")})
    record = run(judge, ("J4",))
    j4 = record["decisions"]["J4"]
    assert j4["decision"] is None and j4["status"] == "INCOMPLETE" and j4["reason_code"] == MISSING_EVIDENCE_CODE
    assert j4["reevaluation"]["retry"]["fail_evidence"]["problems"] == ["quote_not_in_evaluated_text"]
    assert "policy_original_assessment" not in j4   # the final evidence policy does not turn it into FAIL
    assert record["review_required"] is True and record["status"] == "SUCCESS"


def test_reevaluation_call_failure_is_recorded_as_incomplete():
    judge = SequenceJudge({"J4": entry("FAIL", "")}, JevCall("API_ERROR", error="HTTP 529"))
    record = run(judge, ("J4",))
    assert record["decisions"]["J4"]["status"] == "INCOMPLETE"
    assert "API_ERROR" in record["decisions"]["J4"]["reason"]
    assert record["reevaluation"]["status"] == "API_ERROR" and record["reevaluation"]["error"] == "HTTP 529"


def test_unverified_reevaluation_is_incomplete_and_other_valid_fail_settles_the_question():
    judge = SequenceJudge({"J1": entry("FAIL", ""), "J4": entry("FAIL", "'교통비도 아낄 수 있습니다'만 있고 단점이 없음"),
                           "J5": NO_FACTS},
                          {"J1": entry("UNVERIFIED", "판단 근거 부족")})
    cfg = load_aiona_config(); cfg.enabled = False
    batch = evaluate_batch([{"case_id": "q", "question": QUESTION, "answer": ANSWER}], cfg)
    batch["jev_eval"] = evaluate_jev_batch(batch, judge)
    record = batch["jev_eval"]["records"][0]
    assert judge.asked[1] == ["J1"]
    assert record["decisions"]["J1"]["status"] == "INCOMPLETE" and record["decisions"]["J4"]["decision"] == "FAIL"
    assert batch["jev_eval"]["summary"]["J1"]["incomplete"] == 1 and batch["jev_eval"]["reevaluated"] == 1
    summary = combined_summary(batch)
    assert summary["fail"] == 1   # the confirmed J4 FAIL is not invalidated


def test_only_unresolved_criterion_leaves_the_question_incomplete():
    judge = SequenceJudge({"J1": entry("PASS", "비교함"), "J4": entry("FAIL", ""), "J5": NO_FACTS}, {"J4": entry("FAIL", "")})
    cfg = load_aiona_config(); cfg.enabled = False
    batch = evaluate_batch([{"case_id": "q", "question": QUESTION, "answer": ANSWER}], cfg)
    batch["jev_eval"] = evaluate_jev_batch(batch, judge)
    summary = combined_summary(batch)
    assert (summary["pass"], summary["fail"], summary["pending"]) == (0, 0, 1)


def test_legacy_engine_without_reasons_is_not_reevaluated():
    judge = SequenceJudge({"J4": entry("FAIL", "")})
    record = evaluate_jev_case("q", QUESTION, ANSWER, judge, criteria=("J4",))
    assert judge.asked == [["J4"]] and record["decisions"]["J4"]["decision"] == "FAIL"


def test_evidence_check_is_formal_and_accepts_question_or_formatting_differences():
    state = {"question": QUESTION, "answer": "재택근무는 **출퇴근 시간이**  줄어듭니다."}
    assert fail_evidence({"reason": "'출퇴근 시간이 줄어듭니다'만 있음"}, state)["valid"]
    assert fail_evidence({"reason": "질문의 ‘장점과 단점을 비교’ 요구에 단점이 없음"}, state)["matched_quotes"][0]["found_in"] == "question"
    assert fail_evidence({"reason": "단점이 없음"}, state)["problems"] == ["quote_missing"]
    assert "의미적 타당성은 검증하지 않음" in fail_evidence({"reason": ""}, state)["scope"]
