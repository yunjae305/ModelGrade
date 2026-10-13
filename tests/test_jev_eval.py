"""CODE + Jev 하이브리드 평가. 이 파일의 입력은 모두 테스트 픽스처다.

Jev 는 실제로 호출하지 않는다. 공식 REST 형태는 httpx.MockTransport 로, 판단 결과는 정해 둔 응답을 돌려주는
대역으로 흉내 낸다 — 여기서 확인하는 것은 Jev 의 판단 품질이 아니라 AIONA 쪽 연결·분리·장애 처리다.
"""

from __future__ import annotations

import json

import httpx
import pytest

from code_eval import engine
from code_eval.config import AionaConfig
from code_eval.jev_client import HttpJevClient, JevCall, JevClientConfig, resolve_jev_client
from code_eval.jev_criteria import CODE_SCOPE_NOTE
from code_eval.jev_eval import evaluate_jev_batch, evaluation_log
from code_eval.report_html import render_report
from webapp.code_eval_service import CodeEvalService

NO_CONFIG = AionaConfig()
#: NORMAL 질문에 적용되는 기준 — 추천·이유·각각 표현과 답변의 가격 데이터("70만 원대")로 J1~J5 가 모두 적용된다
J_IDS = ("J1", "J2", "J3", "J4", "J5")

NORMAL = {"case_id": "normal",
          "question": "대학생이 쓸 노트북을 불릿으로 정확히 3개 추천하고 각각 이유를 한 줄씩 붙여줘.",
          "answer": "- 레노버 아이디어패드 슬림 5: 가볍고 배터리가 오래가 강의실 이동에 좋습니다.\n"
                    "- 에이수스 비보북 14: 70만 원대 가격에 문서 작업 성능이 충분합니다.\n"
                    "- 애플 맥북 에어 M2: 학생 할인을 받으면 오래 쓰기 좋은 선택입니다."}
FORMAT_FAIL = {"case_id": "format", "question": "공부 방법을 불릿으로 정확히 3개 작성해줘.",
               "answer": "- 개념을 정리하세요.\n- 문제를 풀어보세요."}
INAPPROPRIATE = {"case_id": "inappropriate",
                 "question": "대학생이 과제용으로 쓸 가성비 노트북 3개를 표로 추천해 줘. 열은 순서대로 모델, 가격, 추천 이유로 해 줘.",
                 "answer": "| 모델 | 가격 | 추천 이유 |\n|---|---|---|\n| 워크스테이션 A9000 | 890만 원 | 서버급 CPU 탑재 |\n"
                           "| 렌더팜 노트북 X | 760만 원 | 듀얼 GPU |\n| 크리에이터 프로 Max | 650만 원 | 8K 영상 편집 |"}
CONTRADICTION = {"case_id": "contradiction", "question": "SSD와 HDD 중 부팅 속도가 더 빠른 쪽을 이유와 함께 알려줘.",
                 "answer": "SSD는 움직이는 부품이 없어 데이터를 훨씬 빨리 읽습니다. 그래서 부팅 속도는 SSD가 HDD보다 빠릅니다. "
                           "결론적으로 부팅 속도를 원한다면 HDD가 더 빠르므로 HDD를 고르세요."}
MISSING = {"case_id": "missing", "question": "재택근무의 장점과 단점을 비교해서 설명해줘.",
           "answer": "재택근무는 출퇴근 시간이 줄어 개인 시간이 늘어납니다. 원하는 환경에서 일할 수 있어 집중이 잘 되고, "
                     "교통비와 식비도 아낄 수 있습니다."}
#: 3턴 대화 — 1턴에서 이름·반말 지시, 2턴은 참조 데이터 포함, 3턴 답변은 존댓말·다른 코스로 바뀜(J6 FAIL 사례)
CONV = [
    {"id": "C1-T01", "conversation_id": "C1", "turn": 1,
     "question": "앞으로 내 이름은 민수라고 부르고, 모든 답은 반말로 해줘. 첫 질문: 오늘 뭐 하면 좋을까?",
     "answer": "민수야, 오늘은 산책 어때? 날씨도 좋으니까 가볍게 걷다 오자."},
    {"id": "C1-T02", "conversation_id": "C1", "turn": 2, "question": "좋아. 그럼 산책 코스 하나 추천해줘.",
     "answer": "민수야, 집 근처 하천길 어때? 왕복 3km 정도라 한 시간이면 충분해.", "reference": {"course_length_km": 3}},
    {"id": "C1-T03", "conversation_id": "C1", "turn": 3, "question": "그 코스 이름이 뭐였지?",
     "answer": "고객님, 말씀하신 코스는 한강 공원 코스입니다. 왕복 10km 입니다."},
]


def jev_body(fails=(), confidence=0.9, ids=J_IDS):
    """공식 응답 형태(choice: choice·probabilities·confidence). 두 선택지의 confidence 는 2p-1. 물은 기준에만 답한다."""
    p = round((1 + confidence) / 2, 4)

    def answer(decision):
        probs = {"PASS": p, "FAIL": round(1 - p, 4)} if decision == "PASS" else {"PASS": round(1 - p, 4), "FAIL": p}
        return {"type": "choice", "choice": decision, "probabilities": probs, "confidence": confidence,
                "reason": "연결 검증용 대역의 판정 근거"}

    return {"model": "jev-1.13.0", "usage": {"input_tokens": 300, "output_tokens": 0},
            "answers": {cid: answer("FAIL" if cid in fails else "PASS") for cid in ids}}


class ScriptedJev:
    """질문별로 정해 둔 FAIL 기준을 Jev 응답 형태로 돌려주는 대역."""

    config = JevClientConfig()

    def __init__(self, fails_by_question=None):
        self.fails = fails_by_question or {}
        self.requests = []

    def system_one(self, state, questions):
        self.requests.append({"state": state, "questions": questions})
        return JevCall("OK", body=jev_body(self.fails.get(state["question"], ()), ids=tuple(questions)),
                       attempts=1, latency_ms=120)


def run_code_then_jev(cases, client):
    batch = engine.evaluate_batch(cases, NO_CONFIG)
    digest = batch["result_digest"]
    jev = evaluate_jev_batch(batch, client)
    assert engine.result_digest(batch["cases"]) == digest  # Jev 단계는 CODE 결과를 바꾸지 않는다
    return batch, jev


def decisions(record):
    return {cid: d["decision"] for cid, d in record["decisions"].items()}


def test_case1_normal_answer_code_pass_and_jev_all_pass_through_official_endpoint():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=jev_body(confidence=0.92, ids=tuple(json.loads(request.content)["questions"])))

    client = HttpJevClient(JevClientConfig(), "sk-test-key-000000", transport=httpx.MockTransport(handler))
    batch, jev = run_code_then_jev([NORMAL], client)

    assert batch["summary"]["common"]["fail"] == 0
    record = jev["records"][0]
    assert record["status"] == "SUCCESS" and record["model"] == "jev-1.13.0"
    assert decisions(record) == dict.fromkeys(J_IDS, "PASS")
    assert record["decisions"]["J1"]["confidence"] == 0.92
    assert jev["status"] == "SUCCESS" and jev["summary"]["J1"] == {"name": "의도 충족성", "targets": 1, "pass": 1, "fail": 0, "pending": 0, "unverified": 0, "incomplete": 0}
    # 질문 하나에 한 번만 호출하고, 그 안에 적용 기준(여기서는 J1~J5)을 choice(PASS/FAIL) 로 함께 보낸다
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST" and str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer sk-test-key-000000"
    body = json.loads(request.content)
    assert body["model"] == "jev-latest"
    assert body["state"] == {"question": NORMAL["question"], "answer": NORMAL["answer"]}
    assert set(body["questions"]) == set(J_IDS)  # J 기준만 보낸다 (C 보완 질문 없음)
    assert "code_completion" not in record
    assert all(q["type"] == "choice" and set(q["criteria"]) == {"PASS", "FAIL"} for q in body["questions"].values())


def test_case2_code_format_fail_still_gets_semantic_evaluation_without_code_results():
    client = ScriptedJev()
    batch, jev = run_code_then_jev([FORMAT_FAIL], client)

    assert [r["check_id"] for r in batch["cases"][0]["results"] if r["verdict"] == "FAIL"] == ["2-1.count:bullet_count"]
    assert jev["records"][0]["status"] == "SUCCESS" and decisions(jev["records"][0]) == {"J1": "PASS", "J4": "PASS"}
    # CODE 결과는 Jev 에 넘기지 않고, CODE 가 맡는 개수·형식 영역은 판단하지 말라고 지시한다
    assert len(client.requests) == 1 and set(client.requests[0]["state"]) == {"question", "answer"}
    questions = client.requests[0]["questions"]
    assert all(CODE_SCOPE_NOTE in q["instructions"] for q in questions.values())
    assert "개수만으로 FAIL 하지 않는다" in questions["J4"]["instructions"]


@pytest.mark.parametrize("case,failed", [
    (INAPPROPRIATE, "J2"),   # Case 3 — 형식은 완벽하지만 의미적으로 부적절
    (CONTRADICTION, "J3"),   # Case 4 — 논리 모순 (CODE 가 탐지하지 못함)
    (MISSING, "J4"),         # Case 5 — 핵심 설명 누락
], ids=["case3_inappropriate_J2", "case4_contradiction_J3", "case5_missing_J4"])
def test_case3_to_5_code_pass_but_jev_flags_semantic_problem(case, failed):
    client = ScriptedJev({case["question"]: {failed}})
    batch, jev = run_code_then_jev([case, NORMAL], client)

    assert batch["cases"][0]["summary"]["common"]["fail"] == 0
    assert [r["case_id"] for r in jev["records"]] == [case["case_id"], "normal"]
    asked = jev["records"][0]["criteria"]
    assert failed in asked
    assert decisions(jev["records"][0]) == {cid: "FAIL" if cid == failed else "PASS" for cid in asked}
    assert decisions(jev["records"][1]) == dict.fromkeys(J_IDS, "PASS")
    assert jev["summary"][failed]["fail"] == 1
    html = render_report({**batch, "jev_eval": jev})
    card = html.split(f'id="case-{case["case_id"]}"', 1)[1].split('id="case-normal"', 1)[0]
    assert f"Jev FAIL {failed}" in card and "Semantic Evaluation — Jev" in card and "판단 확신도 90%" in card


def _timeout(request):
    raise httpx.ReadTimeout("timed out", request=request)


@pytest.mark.parametrize("respond,status", [
    (lambda request: httpx.Response(500), "API_ERROR"),
    (_timeout, "TIMEOUT"),
    (lambda request: httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"J1": {"choice": "MAYBE"}}}),
     "INVALID_RESPONSE"),
], ids=["case6_api_error", "case6_timeout", "case6_invalid_response"])
def test_case6_jev_failure_keeps_code_result(respond, status):
    secret = "sk-secret-value-123456"
    client = HttpJevClient(JevClientConfig(), secret, transport=httpx.MockTransport(respond), sleep=lambda _s: None)
    batch, jev = run_code_then_jev([FORMAT_FAIL], client)

    assert batch["summary"]["common"]["fail"] == 1
    record = jev["records"][0]
    assert record["status"] == status and record["decisions"] is None and record["error"]
    assert jev["status"] == status and jev["completed"] == 0
    batch["jev_eval"] = jev
    assert secret not in json.dumps({"batch": batch, "log": evaluation_log(batch)}, ensure_ascii=False)


def test_case7_missing_api_key_keeps_code_evaluation(tmp_path):
    client, config, reason = resolve_jev_client(environment={}, env_path=tmp_path / ".env")
    assert client is None and config.model == "jev-latest" and "TYPESAFE_API_KEY" in reason
    assert resolve_jev_client(environment={"TYPESAFE_API_KEY": "sk-x"}, env_path=tmp_path / ".env")[0] is not None

    batch = CodeEvalService(save_dir=tmp_path / "runs").evaluate(
        {"cases": [FORMAT_FAIL, {"case_id": "broken", "question": "답변 필드가 없는 입력"}]})
    assert batch["summary"]["totals"]["evaluated"] == 1 and batch["summary"]["common"]["fail"] == 1
    jev = batch["jev_eval"]
    assert jev["status"] == "NOT_CONFIGURED" and "TYPESAFE_API_KEY" in jev["access_reason"]
    assert [(r["case_id"], r["status"]) for r in jev["records"]] == [("format", "NOT_CONFIGURED"), ("broken", "SKIPPED")]
    log = evaluation_log(batch)[0]
    assert log["code"]["fail_checks"] == ["2-1.count:bullet_count"] and log["code"]["duration_ms"] is not None
    assert log["jev"]["status"] == "NOT_CONFIGURED" and log["total_ms"] >= log["code"]["duration_ms"]


def test_applicability_is_decided_by_code_rules_from_request_sentences():
    from code_eval.jev_applicability import jev_applicability

    # J1 은 항상, J2~J4 는 요청 문장의 근거 문구가 있을 때만. 답변을 주지 않으면 J5 는 '해당 없음', J6 는 대화 입력이 아니라 적지 않는다
    a = jev_applicability(MISSING["question"])
    assert a["criteria"] == ["J1", "J4"] and a["evidence"]["J4"]["matches"] == ["비교"]
    assert set(a["not_applicable"]) == {"J2", "J3", "J5", "J7", "J8"} and a["evidence"]["J4"]["source"] == "question"
    # 상황 설명 문장의 '고객'은 대상이 아니고, 큰따옴표 안 인용문의 '이유'는 지시가 아니다
    assert jev_applicability("엑셀에 고객의 도시가 적혀 있어. 값이 '서울'인 셀 수를 세는 수식을 작성해줘.")["criteria"] == ["J1"]
    assert jev_applicability('“왜 늦었는지 이유를 말해”를 영어로 번역해줘.')["criteria"] == ["J1"]
    assert jev_applicability("문제를 초등학생도 이해할 수 있게 설명해줘.")["evidence"]["J2"]["rules"] == ["대상", "말투·수준"]

    batch = engine.evaluate_batch([MISSING, NORMAL], NO_CONFIG)
    # 키 없음: 적용 기준·근거·해당 없음은 보이고 결과는 '미실행'
    pending = evaluate_jev_batch(batch, None, access_reason="키 없음")
    card = render_report({**batch, "jev_eval": pending}).split('id="case-missing"', 1)[1].split('id="case-normal"', 1)[0]
    assert "질문 근거: ‘" in card and "비교 대상과 비교 기준을 모두 다뤘는지" in card and "미실행" in card
    assert "해당 없음 · J2 내용 적절성" in card and "해당 없음 · J3 논리 타당성" in card

    # 키 있음: 적용한 기준만 묻는다
    client = ScriptedJev()
    jev = evaluate_jev_batch(batch, client)
    asked = {r["state"]["question"]: {cid for cid in r["questions"] if cid.startswith("J")} for r in client.requests}
    assert asked == {MISSING["question"]: {"J1", "J4"}, NORMAL["question"]: set(J_IDS)}
    assert jev["records"][0]["criteria"] == ["J1", "J4"] and set(jev["records"][0]["decisions"]) == {"J1", "J4"}
    assert jev["summary"]["J4"]["targets"] == 2 and jev["summary"]["J2"]["targets"] == 1
    assert evaluation_log({**batch, "jev_eval": jev})[0]["jev"]["criteria"] == ["J1", "J4"]


def test_j5_applies_to_answer_data_or_reference_and_j6_to_conversation_turns():
    from code_eval.jev_applicability import jev_applicability

    q = "한국의 인구가 얼마인지 알려줘."
    # 답변의 수치·연도 데이터(코드 밖)가 있으면 J5 를 적용하고 근거 출처는 answer
    a = jev_applicability(q, "2023년 기준 약 5,155만 명이며, 수도권에 50% 이상이 살고 있습니다.")
    assert "J5" in a["criteria"] and "J5" not in a["not_applicable"]
    ev = a["evidence"]["J5"]
    assert ev["source"] == "answer" and ev["rules"] == ["수치·단위", "연도·날짜", "통계·출처"]
    assert ev["matches"][:3] == ["2023년", "5,155만 명", "50%"] and "5,155만 명" in ev["excerpt"]
    # 코드 펜스·인라인 코드 안의 숫자와 목록 번호는 데이터가 아니다 → J5 해당 없음
    b = jev_applicability(q, "```python\nprint(2023 + 100)\n```\n`5,155만 명`\n1. 첫째\n2. 둘째\n한국 인구는 통계적으로 늘고 있어요.")
    assert "J5" not in b["criteria"] and "데이터가 없음" in b["not_applicable"]["J5"]
    # 참조 데이터가 있으면 답변에 숫자가 없어도 J5 를 적용하고 출처는 reference
    c = jev_applicability(q, "정확한 값은 직접 확인해 주세요.", reference={"population": 51550000, "year": 2023})
    assert "J5" in c["criteria"] and c["evidence"]["J5"]["source"] == "reference"
    assert c["evidence"]["J5"]["rules"] == ["참조 데이터"] and "51550000" in c["evidence"]["J5"]["excerpt"]
    # J6: 대화의 첫 턴은 해당 없음, 2턴부터 앞 턴 대화가 있을 때 적용, 단일 턴 입력에는 적지 않음
    first = jev_applicability(q, "답", conversation={"id": "C1", "turn": 1, "history_turns": 0})
    assert "J6" not in first["criteria"] and first["not_applicable"]["J6"] == "대화의 첫 턴 — 앞 대화 없음"
    third = jev_applicability(q, "답", conversation={"id": "C1", "turn": 3, "history_turns": 2})
    assert third["criteria"][-1] == "J6" and third["evidence"]["J6"]["excerpt"] == "대화 C1 · 3턴 (앞 턴 2개)"
    broken = jev_applicability(q, "답", conversation={"id": "C1", "turn": 3, "history_turns": 0})
    assert "J6" not in broken["criteria"] and "앞 대화를 만들 수 없음" in broken["not_applicable"]["J6"]
    assert "J6" not in jev_applicability(q, "답")["not_applicable"]


def test_claude_cli_engine_stands_in_for_jev_with_same_criteria(tmp_path):
    import subprocess
    from types import SimpleNamespace
    from code_eval.claude_cli_client import ClaudeCliClient, ClaudeCliConfig

    calls = []

    def runner(cmd, **kw):
        calls.append((cmd, kw))
        schema = json.loads(cmd[cmd.index("--json-schema") + 1])
        out = {"is_error": False, "total_cost_usd": 0.02, "usage": {"input_tokens": 900, "output_tokens": 80},
               "modelUsage": {"claude-helper": {"outputTokens": 3}, "claude-judge": {"outputTokens": 80}},
               "structured_output": {cid: {"choice": "FAIL" if cid == "J4" else "PASS", "confidence": 0.8,
                                           "reason": f"{cid} 근거: '교통비와 식비도 아낄 수 있습니다' 외 단점이 없음"}
                                     for cid in schema["required"]}}
        if 'J5' in schema['required']:
            out['structured_output']['J5'].update(choice='UNVERIFIED', confidence=0.0,
                fact_check={'basis': 'none', 'claims': []}, reason='검색과 참조 자료가 없어 사실 확인 필요')
        return SimpleNamespace(returncode=0, stdout=json.dumps(out, ensure_ascii=False), stderr="")

    batch = engine.evaluate_batch([MISSING], NO_CONFIG)
    client = ClaudeCliClient(ClaudeCliConfig(executable="claude"), runner=runner, workdir=tmp_path)
    jev = evaluate_jev_batch(batch, client)

    cmd, kw = calls[0]
    assert {"-p", "--safe-mode", "--no-session-persistence"} <= set(cmd) and cmd[cmd.index("--tools") + 1] == ""
    assert MISSING["answer"] in kw["input"] and "의도 충족성" in kw["input"] and "논리 타당성" not in kw["input"]
    assert jev["engine"] == "claude_cli" and jev["engine_label"] == "Claude CLI"
    record = jev["records"][0]
    assert record["status"] == "SUCCESS" and record["model"] == "claude-judge" and record["criteria"] == ["J1", "J4", "J5"]
    assert decisions(record) == {"J1": "PASS", "J4": "FAIL", "J5": None} and record["decisions"]["J4"]["reason"].startswith("J4 근거")
    # Claude CLI has no search tool: unchecked external facts are an evaluation gap, not an answer FAIL.
    j5 = record['decisions']['J5']
    assert j5['status'] == 'INCOMPLETE' and j5['reason_code'] == 'J5_SEARCH_UNAVAILABLE' and record['review_required']
    assert j5['policy_original_assessment']['status'] == 'UNVERIFIED' and j5['j5_outcome']['code'] == 'search_unavailable'
    assert record["decisions"]["J4"]["probabilities"] == {"PASS": 0.1, "FAIL": 0.9}
    html = render_report({**batch, "jev_eval": jev})
    assert "Semantic Evaluation — Claude CLI" in html and "J4 근거" in html

    def broken(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    failed = ClaudeCliClient(ClaudeCliConfig(executable="claude", max_attempts=1), runner=broken, workdir=tmp_path)
    assert evaluate_jev_batch(batch, failed)["records"][0]["status"] == "TIMEOUT"
    missing_cli = resolve_jev_client(environment={"CLAUDE_CLI_PATH": str(tmp_path / "no-claude.exe")},
                                     env_path=tmp_path / ".env", engine="claude-cli")
    assert missing_cli[0] is None and "Claude CLI" in missing_cli[2]


def test_combined_summary_counts_a_question_once_across_code_and_semantic_fails():
    from code_eval.combined import combined_summary

    client = ScriptedJev({INAPPROPRIATE["question"]: {"J2"}, FORMAT_FAIL["question"]: {"J1"}})
    cases = [NORMAL, FORMAT_FAIL, INAPPROPRIATE, CONTRADICTION]
    batch, jev = run_code_then_jev(cases, client)
    batch["jev_eval"] = jev
    s = combined_summary(batch)

    # NORMAL·CONTRADICTION 통과, INAPPROPRIATE 는 의미 평가만, FORMAT_FAIL 은 CODE 와 의미 평가 둘 다
    assert (s["questions"], s["pass"], s["fail"]) == (4, 2, 2) and s["rate"] == 0.5
    assert (s["fail_code_only"], s["fail_semantic_only"], s["fail_both"]) == (0, 1, 1)
    assert [p["case_id"] for p in s["problems"]] == ["format", "inappropriate"]
    assert s["problems"][0]["sources"] == ["CODE", "의미 평가"]
    code_item = s["problems"][0]["items"][0]
    assert (code_item["id"], code_item["name"]) == ("2-1", "분량·표현 조건") and code_item["where"]["lines"]
    # 의미 평가의 틀린 부분은 근거가 인용한 글이 답변에 그대로 있을 때만 잡는다
    from code_eval.combined import _quoted_where
    assert _quoted_where("답변이 '개념을 정리하세요'만 제시했다", FORMAT_FAIL["answer"])["lines"][0]["n"] == 1
    assert _quoted_where("답변이 '답변에 없는 문장'을 썼다", FORMAT_FAIL["answer"]) is None
    areas = {a["id"]: a for a in s["areas"]}
    assert areas["2"]["fail"] == 1 and areas["J2"] == {**areas["J2"], "pass": 1, "fail": 1, "targets": 2}
    html = render_report(batch)
    assert html.index(">03 · AIONA 특화 평가 기준</h2>") < html.index(">종합 결과</h2>") < html.index(">평가 결과 한눈에 보기</h2>")
    assert "4문항 중 PASS 2 · FAIL 2 · 평가 미완료 0" in html and "FAIL 질문 2개" in html
    # 질문별 머리줄의 FAIL 수에 의미 평가 FAIL 도 들어간다 (CODE FAIL 0 + J2 FAIL 1)
    head = html[html.index('id="case-inappropriate"'):]
    assert head[:head.index('<div class="q-full">')].split(" · FAIL ")[1].startswith("1<")
    assert "FAIL 0 + 의미 평가(Jev) PASS" in head and 'Semantic Evaluation — Jev <span class="box-count">' in head


def test_conversation_turns_send_history_in_turn_order_and_ask_j6_from_second_turn():
    client = ScriptedJev({CONV[2]["question"]: {"J6"}})
    batch, jev = run_code_then_jev([CONV[2], NORMAL, CONV[0], CONV[1]], client)   # 입력 순서를 섞어도 turn 으로 묶는다

    assert [r["case_id"] for r in jev["records"]] == ["C1-T03", "normal", "C1-T01", "C1-T02"]
    by_q = {r["state"]["question"]: r for r in client.requests}
    assert set(by_q[NORMAL["question"]]["state"]) == {"question", "answer"}            # 단일 턴 입력의 요청은 그대로
    assert set(by_q[CONV[0]["question"]]["state"]) == {"question", "answer"}           # 첫 턴은 history 없음
    assert by_q[CONV[1]["question"]]["state"]["history"] == [{"turn": 1, "question": CONV[0]["question"], "answer": CONV[0]["answer"]}]
    assert by_q[CONV[1]["question"]]["state"]["reference"] == {"course_length_km": 3}
    assert [h["turn"] for h in by_q[CONV[2]["question"]]["state"]["history"]] == [1, 2]
    assert "reference" not in by_q[CONV[2]["question"]]["state"]["history"][1]        # history 항목은 turn·질문·답변만
    first, second, third = jev["records"][2], jev["records"][3], jev["records"][0]
    assert "J6" not in first["criteria"] and first["applicability"]["not_applicable"]["J6"] == "대화의 첫 턴 — 앞 대화 없음"
    assert "J6" in second["criteria"] and "J6" in third["criteria"]
    assert first["context"] == {"conversation_id": "C1", "turn": 1, "total_turns": 3, "history_turns": 0, "history_chars": 0,
                                "history_truncated": False, "history_complete": True, "issues": []}
    assert third["context"]["history_turns"] == 2 and jev["records"][1]["context"] is None
    # J5 기준: 참조 데이터가 있으면 reference, 없으면 engine, J5 를 묻지 않았으면 None
    assert (first["j5_basis"], second["j5_basis"], third["j5_basis"]) == (None, "reference", "engine")
    assert decisions(third)["J6"] == "FAIL" and decisions(second)["J6"] == "PASS"
    assert jev["summary"]["J6"] == {"name": "멀티턴 맥락 유지", "targets": 2, "pass": 1, "fail": 1, "pending": 0, "unverified": 0, "incomplete": 0}
    conv = jev["conversations"]
    assert (conv["count"], conv["pass"], conv["fail"], conv["incomplete"], conv["rate"]) == (1, 0, 1, 0, 0.0)
    item = conv["items"][0]
    assert (item["id"], item["turns"], item["cases"], item["j6_turns"], item["result"], item["first_fail_turn"]) == ("C1", 3, 3, 2, "FAIL", 3)
    assert [t["decision"] for t in item["turn_results"]] == [None, "PASS", "FAIL"] and item["issues"] == []
    assert conv["bands"]["1~10"] == {"targets": 2, "pass": 1, "fail": 1, "rate": 0.5, "pending": 0, "unverified": 0} and conv["bands"]["11~20"]["targets"] == 0
    log = evaluation_log({**batch, "jev_eval": jev})
    assert log[0]["conversation"] == {"id": "C1", "turn": 3} and log[0]["jev"]["history_turns"] == 2 and log[1]["conversation"] is None
    assert log[3]["jev"]["j5_basis"] == "reference"
    # 보고서: 대화 유지율 표, 대화 표기, 근거 출처 라벨, 참조 데이터, J5 판정 기준
    html = render_report({**batch, "jev_eval": jev})
    assert "대화 유지율 · J6 멀티턴 맥락 유지" in html and "첫 FAIL 턴" in html and html.count('<span class="chip">대화 C1 · ') == 3
    assert "대화 C1 · 3턴" in html and "답변 근거: ‘" in html and "참조 데이터: ‘" in html and "참조 데이터 (reference)" in html
    assert "판정 기준: 참조 데이터" in html and "판정 기준: 엔진 지식" in html
    assert "대화 맥락: 앞 턴 2개" in html and "대화의 첫 턴 — 앞 대화 없음" in html
    # 대화 입력이 없으면 대화 유지율 표가 없고, 적용 질문이 없는 J6 막대도 그리지 않는다
    plain = render_report({**engine.evaluate_batch([NORMAL], NO_CONFIG), "jev_eval": evaluate_jev_batch(engine.evaluate_batch([NORMAL], NO_CONFIG), None, access_reason="키 없음")})
    assert "대화 유지율" not in plain and "J6 멀티턴 맥락 유지</span>" not in plain and ">J5</span><h3>DATA 신뢰도</h3>" in plain


def test_incomplete_conversation_history_skips_j6_and_marks_conversation_incomplete():
    # 앞 턴이 입력 오류: J6 를 묻지 않고 history 도 보내지 않는다
    client = ScriptedJev()
    batch, jev = run_code_then_jev([{**CONV[0], "answer": None}, CONV[1]], client)
    rec = jev["records"][1]
    assert "J6" not in client.requests[0]['questions'] and rec['decisions']['J6']['status'] == 'UNVERIFIED'
    assert '1턴 INPUT_ERROR' in rec['applicability']['evidence']['J6']['matches']
    assert "history" not in client.requests[0]["state"]
    conv = jev["conversations"]
    assert conv["count"] == 1 and conv["incomplete"] == 1 and conv["items"][0]["result"] == "INCOMPLETE"
    assert "1턴 INPUT_ERROR" in conv["items"][0]["issues"]
    # 중간 턴 누락: 있는 앞 턴은 맥락으로 보내지만 J6 는 묻지 않는다
    client = ScriptedJev()
    batch, jev = run_code_then_jev([CONV[0], CONV[2]], client)
    rec = jev["records"][1]
    assert "J6" not in client.requests[1]['questions'] and rec['decisions']['J6']['status'] == 'UNVERIFIED'
    assert '누락 2턴' in rec['applicability']['evidence']['J6']['matches']
    assert [h["turn"] for h in client.requests[1]["state"]["history"]] == [1] and rec["context"]["history_complete"] is False
    assert jev["conversations"]["items"][0]["result"] == "INCOMPLETE" and "누락 2턴" in jev["conversations"]["items"][0]["issues"]
    # 같은 turn 중복: 어느 쪽이 진짜인지 모르므로 그 턴부터 J6 를 묻지 않는다
    batch, jev = run_code_then_jev([CONV[0], CONV[1], {**CONV[1], "id": "C1-T02b"}], ScriptedJev())
    assert all(r['decisions']['J6']['status'] == 'UNVERIFIED' and 'J6' in r['not_requested'] for r in jev['records'][1:])
    assert jev["conversations"]["items"][0]["result"] == "INCOMPLETE" and "중복 2턴" in jev["conversations"]["items"][0]["issues"]
    # 대화 입력이 없으면 집계는 비어 있다
    assert run_code_then_jev([NORMAL], ScriptedJev())[1]["conversations"]["count"] == 0


def test_long_history_preserves_all_turns_and_original_answers():
    from code_eval.jev_eval import build_history

    prior = [(t, f"{t}번째 질문", "가" * 3000) for t in range(1, 41)]
    history, truncated = build_history(prior)
    assert not truncated
    assert history == [{"turn": t, "question": q, "answer": a} for t, q, a in prior]
    assert sum(len(h["question"]) + len(h["answer"]) for h in history) > 60_000
    assert build_history([(1, "질문", "짧은 답")]) == ([{"turn": 1, "question": "질문", "answer": "짧은 답"}], False)


def test_claude_cli_prompt_carries_history_and_reference_and_schema_has_j5_j6(tmp_path):
    from types import SimpleNamespace
    from code_eval.claude_cli_client import ClaudeCliClient, ClaudeCliConfig

    calls = []

    def runner(cmd, **kw):
        calls.append((cmd, kw))
        schema = json.loads(cmd[cmd.index("--json-schema") + 1])
        out = {"is_error": False, "usage": {}, "modelUsage": {"claude-judge": {"outputTokens": 1}},
               "structured_output": {cid: {"choice": "PASS", "confidence": 0.7, "reason": "근거"} for cid in schema["required"]}}
        state = json.loads(kw['input'].split('## 평가 대상 (데이터)\n', 1)[1])
        if 'J5' in schema['required']:
            if state.get('reference'):
                out['structured_output']['J5']['fact_check'] = {'basis': 'reference', 'claims': [{
                    'claim': '왕복 3km', 'verdict': 'PASS', 'reason': '참조 거리와 일치하는 대역 검증',
                    'sources': [{'url': '', 'title': '참조 자료', 'quote': '"course_length_km": 3'}]}]}
            else:
                out['structured_output']['J5'].update(choice='NOT_APPLICABLE', fact_check={'basis': 'none', 'claims': []},
                    reason='산책을 권유하는 대역 응답에는 확인할 사실 주장이 없음')
        if 'J6' in schema['required']:
            out['structured_output']['J6']['context_checks'] = [{
                'turn': 1, 'instruction_quote': state['history'][0]['question'],
                'answer_quote': state['answer'], 'decision': 'PASS', 'reason': '이름과 반말 지시 유지 대역 검증'}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(out, ensure_ascii=False), stderr="")

    batch = engine.evaluate_batch([CONV[0], CONV[1]], NO_CONFIG)
    jev = evaluate_jev_batch(batch, ClaudeCliClient(ClaudeCliConfig(executable="claude"), runner=runner, workdir=tmp_path))
    assert jev["status"] == "SUCCESS" and jev["records"][1]["criteria"][-2:] == ["J5", "J6"]
    cmd, kw = next((cmd, kw) for cmd, kw in calls
                   if json.loads(kw["input"].split("## 평가 대상 (데이터)\n", 1)[1]).get("history"))
    required = json.loads(cmd[cmd.index("--json-schema") + 1])["required"]
    assert {"J5", "J6"} <= set(required)
    assert CONV[0]["answer"] in kw["input"] and "history 는 이 턴 앞의 대화" in kw["input"] and "reference 는 J5" in kw["input"]
    assert "DATA 신뢰도" in kw["input"] and "멀티턴 맥락 유지" in kw["input"]
