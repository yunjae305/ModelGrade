"""3단계 실사용 검증(2026-10-12)에서 찾은 판정 오류 F1~F4 의 회귀 테스트 (처음에는 xfail(strict) 재현).

실제 Claude 기록은 results/stage3-live-validation/ (Git 제외)에 있고, 여기서는 같은 문항으로
평가기 응답·원문을 고정해 서버 규칙만 재현한다. 원인과 수정: 참고자료/문서/3단계_실사용검증_2026-10-12.md
5장, 참고자료/3단계_최종안정화_2026-10-12.md.
"""
import importlib.util
from pathlib import Path

from code_eval.config import load_aiona_config
from code_eval.engine import evaluate_batch
from code_eval.semantic_evidence import _input_scope

spec = importlib.util.spec_from_file_location("live_stage3_validation", Path(__file__).with_name("live_stage3_validation.py"))
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
CASES = {case["id"]: case for case in harness.load_fixture()["cases"]}


def judged(case, tmp_path, behaviors=None, fetcher=None):
    client, config, _ = harness.mock_client([case], behaviors, fetcher=fetcher)
    batch, guard, timing = harness.run_pipeline([case], client, config, tmp_path, max_usd=1, max_calls=2)
    return harness.analyze([case], batch, guard, timing, mode="repro")["items"][0]["actual"]


def test_g4_01_fetch_only_evidence_from_the_cited_page_verifies_j5(tmp_path):
    # F1: 검색 없이 연 답변 출처 원문(공급자 web_fetch 결과)도 서버가 인용문을 확인하면 J5 근거로 인정한다.
    actual = judged(CASES["G4-01"], tmp_path, {"G4-01": {"search": "fetch_only"}})
    assert actual["citation_checks"][0]["verdict"] == "supported"
    assert actual["j5"]["outcome"] == "verified" and actual["verdict"] == "PASS"


def test_g4_02_digits_inside_the_cited_url_do_not_block_an_unrelated_verdict(tmp_path):
    # F2: 주장 문장의 링크 URL 경로 숫자('/3/')는 주장 수치가 아니므로 '무관' 판정을 막지 않는다.
    pages = {"https://docs.python.org/3/library/os.path.html": "os.path — Common pathname manipulations. Python 3 documentation",
             "https://docs.python.org/3/library/stdtypes.html": "This method modifies the sequence in place"}
    fetcher = lambda url: ({"status": "ok", "text": pages[url], "final_url": url, "content_type": "text/html"}
                           if url in pages else {"status": "http_error"})
    actual = judged(CASES["G4-02"], tmp_path, fetcher=fetcher)
    assert actual["citation_checks"][0]["verdict"] == "unrelated"
    assert actual["j5"]["outcome"] == "source_mismatch" and actual["verdict"] == "FAIL"


def test_g3_04_cancelled_quoted_ending_is_not_inherited():
    # F3: 따옴표 문구 끝맺음 지시의 해제('…로 끝내지 않아도 돼')는 다음 턴에 이어받지 않는다.
    cfg = load_aiona_config()
    cfg.enabled = False
    payloads, _ = harness.build_inputs([CASES["G3-04"]])
    case = evaluate_batch(payloads, cfg)["cases"][-1]
    assert [r["check_id"] for r in case["results"] if r.get("source") == "history_instruction"] == []


def test_g3_01_items_listed_in_the_request_are_task_inputs():
    # F4: '정리해 줘: 빨래, 청소'처럼 요청 문장에 바로 나열한 값은 입력 자료다 (현실 진위 인정은 아님).
    turn = CASES["G3-01"]["turns"][1]
    claim = {"claim": "빨래", "scope": "input_consistency", "scope_quote": turn["question"]}
    assert _input_scope(claim, {"question": turn["question"], "answer": turn["answer"]}, {"origin": "question"})
