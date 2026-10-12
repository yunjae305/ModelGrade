"""Provided-material consistency stays distinct from independently sourced facts."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from code_eval import semantic_sources
from code_eval.anthropic_semantic_client import AnthropicSemanticClient, AnthropicSemanticConfig, FINAL_TOOL
from code_eval.codex_cli_client import CodexCliClient, CodexCliConfig
from code_eval.jev_eval import jev_request, parse_decisions
from code_eval.semantic_api_client import SemanticApiClient, SemanticApiConfig
from code_eval.semantic_cli_common import build_prompt, jev_answer, output_schema
from code_eval.semantic_evidence import finalize_answers

REQUEST = "다음 가상 공지만을 근거로 핵심 내용을 요약해줘."
NOTICE = "시민회관은 매주 토요일 오전 10시에 무료 독서 모임을 운영한다. 정원은 12명이며 사전 신청이 필요하다."
STATE = {"question": REQUEST + ' "' + NOTICE + '" 공지에 없는 정보는 덧붙이지 마.',
         "answer": "토요일 오전 10시에 무료 독서 모임을 운영합니다. 정원은 12명입니다."}
URL = "https://official.example/facts"
WEB_FACT = "Python lists are mutable."
QUESTIONS = jev_request("question", "answer", ("J5",))["questions"]


@pytest.fixture(autouse=True)
def forbid_real_fetch(monkeypatch):
    monkeypatch.setattr(semantic_sources, "fetch_public_source", lambda *a, **k: pytest.fail("No real source fetch"))


def input_claim(**changes):
    return {"claim": "정원은 12명입니다.", "verdict": "PASS", "reason": "제공 공지와 일치합니다.",
            "scope": "input_consistency", "scope_quote": REQUEST,
            "sources": [{"origin": "question", "turn": None, "url": "", "title": "제공 공지",
                         "quote": "정원은 12명이며 사전 신청이 필요하다.", "authority": "unestablished",
                         "authority_reason": "제공 자료 일치만 확인", "temporal_relevance": "주어진 가상 설정"}],
            **changes}


def web_claim():
    return {"claim": WEB_FACT, "verdict": "PASS", "reason": "공식 문서와 일치합니다.",
            "scope": "external_fact", "scope_quote": "",
            "sources": [{"origin": "web", "turn": None, "url": URL, "title": "Official documentation",
                         "quote": WEB_FACT, "authority": "primary", "authority_reason": "공식 언어 문서",
                         "temporal_relevance": "변하지 않는 언어 정의"}]}


def entries(claims=None, basis="input", choice="PASS"):
    return {"J5": {"choice": choice, "confidence": .8, "reason": "주장별 근거를 대조했습니다.",
                   "fact_check": {"basis": basis, "claims": claims if claims is not None else [input_claim()]}}}


def evaluate(values=None, state=None, *, searched=False, observed=(), fetcher=None):
    state = copy.deepcopy(STATE) if state is None else state
    values = entries() if values is None else values
    body = {"answers": {k: jev_answer(v) for k, v in values.items()},
            "verification": {"search_performed": searched, "source_urls": list(observed), "source_validation": "provider"}}
    before_body, before_state = copy.deepcopy(body), copy.deepcopy(state)
    checked = semantic_sources.verify_web_sources(body, fetcher=fetcher or (lambda *_: pytest.fail("No source required")))
    result = finalize_answers(checked, state, QUESTIONS)
    assert body == before_body and state == before_state
    return result


def decision(body):
    return body["answers"]["J5"]


def fetched(url, text=WEB_FACT, status="ok"):
    return {"status": status, "text": text, "final_url": url, "checked_at": "2026-10-11T00:00:00Z",
            "content_type": "text/plain"}


def test_fictional_notice_is_checked_against_question_without_fabricating_reference_or_search():
    result = evaluate()
    assert decision(result)["choice"] == "PASS"
    source = decision(result)["fact_check"]["claims"][0]["sources"][0]
    assert source["origin"] == "question" and source["quote_verified"] is True
    assert "자료 자체의 사실성은 검증하지 않습니다" in source["verification_reason"]
    assert result["verification"]["search_performed"] is False and "reference" not in STATE


@pytest.mark.parametrize("choice", ["FAIL", "UNVERIFIED"])
def test_input_scope_preserves_model_detected_mismatch_or_abstention(choice):
    result = evaluate(entries([input_claim(verdict=choice, reason="원문과의 대응을 확인하지 못했습니다.")], choice=choice))
    assert decision(result)["choice"] == choice


@pytest.mark.parametrize("changes", [
    {"scope": "external_fact"}, {"scope": None}, {"scope_quote": None},
    {"scope_quote": "원문에 없는 요약 요청"}, {"scope_quote": "정원은 12명이며 사전 신청이 필요하다."},
    {"claim": "답변에 없는 주장"},
])
def test_input_sources_cannot_verify_external_claims_or_invented_task_or_answer(changes):
    assert decision(evaluate(entries([input_claim(**changes)])))["choice"] == "UNVERIFIED"


@pytest.mark.parametrize("task_instruction", ["다음 자료가 사실인지 검증해줘.", "다음 자료가 사실인지 검증해 요약해줘.",
    "Verify whether the following claim is true and summarize it.", "다음 자료를 요약하고 진위를 확인해 줘.",
    "다음 자료를 요약하고 정확성을 확인해 줘.", "다음 자료를 요약하고 맞는지 확인해 줘.",
    "Summarize the following document and confirm its accuracy.",
    "Summarize the following document and check whether its claims are correct."])
def test_fact_check_requests_do_not_accept_their_own_premise_as_evidence(task_instruction):
    state = {**STATE, "question": task_instruction + NOTICE}
    assert decision(evaluate(entries([input_claim(scope_quote=task_instruction)]), state))["choice"] == "UNVERIFIED"


def test_scope_quote_cannot_hide_a_separate_request_to_verify_material_truth():
    state = {**STATE, "question": STATE["question"] + " 이 공지가 사실인지 검증해줘."}
    assert decision(evaluate(state=state))["choice"] == "UNVERIFIED"


def test_material_introduction_can_be_separate_from_exact_transformation_request():
    instruction = "담당자와 정원을 표로 정리해줘."
    state = {**STATE, "question": "다음은 가상의 공지야. " + NOTICE + " " + instruction}
    result = evaluate(entries([input_claim(scope_quote=instruction)]), state)
    assert decision(result)["choice"] == "PASS"
    assert decision(result)["fact_check"]["claims"][0]["sources"][0]["quote_verified"] is True


@pytest.mark.parametrize("case_id,question,answer,scope_quote,claim_text,source_quote", [
    ("claude:Q9_T1",
     "가상의 재고 기록이야. 연필은 12개에 개당 500원, 지우개는 7개에 개당 800원이 있어. "
     "이 정보를 name, quantity, unit_price 키를 가진 JSON 배열로 작성해줘. 수량과 가격은 문자열이 아니라 숫자로 넣어줘.",
     '```json\n[\n  {\n    "name": "연필",\n    "quantity": 12,\n    "unit_price": 500\n  },\n'
     '  {\n    "name": "지우개",\n    "quantity": 7,\n    "unit_price": 800\n  }\n]\n```\n\n'
     '`quantity`와 `unit_price`는 따옴표 없이 숫자로 넣었습니다.',
     "연필은 12개에 개당 500원, 지우개는 7개에 개당 800원이 있어. 이 정보를 name, quantity, unit_price 키를 가진 JSON 배열로 작성해줘.",
     '"name": "연필",\n    "quantity": 12,\n    "unit_price": 500', "연필은 12개에 개당 500원"),
    ("aiona:Q25_T1",
     '가상의 서점 이름은 "느린페이지"이고, 월요일은 쉬며 화요일부터 일요일까지 오전 11시부터 오후 8시까지 운영해. '
     "이 정보만 사용해서 온라인 소개 문구를 작성해줘. 위치나 판매 품목은 추가하지 마.",
     "느린페이지의 온라인 소개 문구를 작성했어요.\n\n### 소개 문구\n**느린페이지**\n"
     "월요일은 쉬어가고, 화요일부터 일요일까지 오전 11시부터 오후 8시까지 운영합니다.",
     "이 정보만 사용해서 온라인 소개 문구를 작성해줘",
     "월요일은 쉬어가고, 화요일부터 일요일까지 오전 11시부터 오후 8시까지 운영합니다.",
     "월요일은 쉬며 화요일부터 일요일까지 오전 11시부터 오후 8시까지 운영해"),
])
def test_confirmed_supplied_value_rewrites_keep_exact_case_text_immutable(
        case_id, question, answer, scope_quote, claim_text, source_quote):
    # Reproduce the decisive source/claim from 65d50fe3b23d without reading or
    # rewriting its historical result. This is an evidence-gate fixture, not a
    # fresh model judgment or permission to promote all historical input rows.
    state = {"question": question, "answer": answer}
    claim = input_claim(claim=claim_text, scope_quote=scope_quote)
    claim["sources"][0]["quote"] = source_quote
    stored_record = {"case_id": case_id, "status": "UNVERIFIED", "claim": copy.deepcopy(claim)}
    before = copy.deepcopy(stored_record)
    result = evaluate(entries([claim]), state)
    assert decision(result)["choice"] == "PASS"
    checked = decision(result)["fact_check"]["claims"][0]["sources"][0]
    assert checked["input_quote_verified"] is True and checked["input_scope_verified"] is True
    assert checked["quote_verified"] is True
    assert stored_record == before and stored_record["status"] == "UNVERIFIED"


@pytest.mark.parametrize("instruction", [
    "이 정보를 JSON 배열로 작성해줘.",
    "주어진 자료를 JSON 객체로 출력해줘.",
    "이 정보만 사용해서 온라인 소개 문구를 작성해줘.",
])
@pytest.mark.parametrize("violation", ["truth_request", "external_claim", "wrong_claim", "wrong_source", "missing_source"])
def test_explicit_rewrite_forms_preserve_truth_and_evidence_guards(instruction, violation):
    state = {**STATE, "question": "가상의 공지야. " + NOTICE + " " + instruction}
    claim = input_claim(scope_quote=instruction)
    if violation == "truth_request":
        state["question"] += " 그리고 이 공지가 사실인지 검증해줘."
    elif violation == "external_claim":
        claim["scope"] = "external_fact"
    elif violation == "wrong_claim":
        claim["claim"] = "정원은 20명입니다."
    elif violation == "wrong_source":
        claim["sources"][0]["quote"] = "정원은 20명이다."
    else:
        claim["sources"] = []
    assert decision(evaluate(entries([claim]), state))["choice"] == "UNVERIFIED"


@pytest.mark.parametrize("instruction", [
    "이 정보만 사용해서 새로운 이야기를 작성해줘.",
    "다음 조건을 만족하는 모임을 추천해줘.",
    "이 정보를 바탕으로 총인원을 계산해줘.",
    "이 정보를 바탕으로 가능한지 추론해줘.",
])
def test_task_inputs_remain_valid_evidence_outside_summarization(instruction):
    # The supplied capacity is an input, even when the requested result is a
    # calculation or recommendation. It does not verify a generated conclusion.
    state = {**STATE, "question": "가상의 공지야. " + NOTICE + " " + instruction}
    result = evaluate(entries([input_claim(scope_quote=instruction)]), state)
    assert decision(result)["choice"] == "PASS"
    assert decision(result)["fact_check"]["claims"][0]["sources"][0]["quote_verified"] is True


def listed_item_claim(question, scope_quote, claim="빨래", scope="input_consistency"):
    state = {"question": question, "answer": "| 순서 | 할 일 |\n|---|---|\n| 1 | 빨래 |\n| 2 | 청소 |"}
    item = input_claim(claim=claim, scope=scope, scope_quote=scope_quote if scope == "input_consistency" else "",
                       reason="사용자가 준 할 일과 일치합니다.")
    item["sources"][0]["quote"] = "빨래, 청소"
    return decision(evaluate(entries([item]), state))


@pytest.mark.parametrize("question,scope_quote", [
    # F4 (G3-01): 요청 문장에 바로 나열한 값도 제공 자료다
    ("화요일 할 일도 정리해 줘: 빨래, 청소.", "화요일 할 일도 정리해 줘: 빨래, 청소."),
    ("다음 항목을 분류해 줘: 빨래, 청소, 설거지.", "다음 항목을 분류해 줘"),
    ("할 일 목록: 빨래, 청소, 설거지. 순서대로 정리해줘.", "순서대로 정리해줘"),
])
def test_items_listed_in_the_request_are_task_inputs(question, scope_quote):
    j5 = listed_item_claim(question, scope_quote)
    assert j5["choice"] == "PASS"
    assert j5["fact_check"]["claims"][0]["sources"][0]["input_scope_verified"] is True


@pytest.mark.parametrize("question,scope_quote,scope", [
    ("할 일을 추천해 줘: 빨래, 청소.", "할 일을 추천해 줘", "input_consistency"),          # 변환이 아닌 추천
    ("이 목록이 맞는지 확인해서 정리해 줘: 빨래, 청소.", "정리해 줘", "input_consistency"),  # 진위 확인 요청
    ("화요일 할 일도 정리해 줘: 빨래, 청소.", "", "external_fact"),                      # 외부 사실 주장
])
def test_listed_items_do_not_prove_other_tasks_or_external_facts(question, scope_quote, scope):
    j5 = listed_item_claim(question, scope_quote, scope=scope)
    assert j5["choice"] == "UNVERIFIED"
    assert j5["fact_check"]["claims"][0]["verdict"] == "UNVERIFIED"


@pytest.mark.parametrize("instruction", [
    "현재 국내 모임의 정원을 JSON 배열로 작성해줘.",
    "전국 모임의 현재 정원을 표로 작성해줘.",
    "이 시설의 실제 운영 정원을 계산해줘.",
])
def test_input_cannot_prove_requested_real_world_data(instruction):
    state = {**STATE, "question": "가상의 공지야. " + NOTICE + " " + instruction}
    result = evaluate(entries([input_claim(scope_quote=instruction)]), state)
    assert decision(result)["choice"] == "UNVERIFIED"
    source = decision(result)["fact_check"]["claims"][0]["sources"][0]
    assert source["input_quote_verified"] is True and source["input_scope_verified"] is False
    assert source["quote_verified"] is False
    assert "사용자 원문 인용은 확인했지만" in source["verification_reason"]


@pytest.mark.parametrize("question,answer,scope_quote,source_quote", [
    ("가상의 온라인 강좌가 강사 사정으로 취소됐어. 처리에는 영업일 기준 3~5일이 걸려. 고객에게 보내는 취소 안내 이메일을 작성해줘.",
     "환불에는 영업일 기준 3~5일이 걸립니다.", "고객에게 보내는 취소 안내 이메일을 작성해줘.", "처리에는 영업일 기준 3~5일이 걸려."),
    ("친구 네 명이 식사했어. 음식값은 64,000원이야. 각 사람이 내야 할 금액을 계산해줘.",
     "음식값은 64,000원입니다.", "각 사람이 내야 할 금액을 계산해줘.", "음식값은 64,000원이야."),
    ("친구에게 빌린 책을 잊어버렸어. 내일 오후 6시에 학교 정문에서 돌려줄 수 있어. 변명 없이 사과하고 반환 일정을 제안하는 메시지를 써줘.",
     "내일 오후 6시에 학교 정문에서 돌려줄게.", "변명 없이 사과하고 반환 일정을 제안하는 메시지를 써줘.", "내일 오후 6시에 학교 정문에서 돌려줄 수 있어."),
    ("가상의 파일 업로드 화면은 PDF만 받고 파일 크기는 최대 10MB까지 허용해. 오류 안내 문구를 작성해줘.",
     "PDF는 최대 10MB까지 올릴 수 있어요.", "오류 안내 문구를 작성해줘.", "파일 크기는 최대 10MB까지 허용해."),
])
@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_supplied_situation_values_in_writing_and_calculation_keep_judge_verdict(
        question, answer, scope_quote, source_quote, verdict):
    claim = input_claim(claim=answer, scope_quote=scope_quote, verdict=verdict)
    claim["sources"][0]["quote"] = source_quote
    result = evaluate(entries([claim], choice=verdict), {"question": question, "answer": answer})
    assert decision(result)["choice"] == verdict
    assert decision(result)["fact_check"]["claims"][0]["sources"][0]["quote_verified"] is True


@pytest.mark.parametrize("instruction", [
    "현재 조건에 맞는 영화를 모두 골라줘.",
    "현재 확정된 실제 관람 계획을 JSON 객체 하나로 출력해줘.",
    "이번에는 계획만 세우고, 파일 이름은 아직 바뀌지 않은 것으로 유지해줘.",
    "최신 대응표에서 충돌이 생기는 항목과 충돌 없이 바꿀 수 있는 항목을 구분해줘.",
    "현재 선택한 영화의 상영시간을 다시 알려줘.",
    "기존 조건을 유지해 시간표를 고치고, 전체 시간 제한을 지키는지 확인해줘.",
    "선택한 두 편은 아직 바꾸지 말고, 현재 개별 영화 조건에 맞는 후보 목록을 갱신해줘.",
    "이 조건에서 가능한지 설명해줘.",
])
def test_simulated_history_followups_keep_exact_user_input_evidence(instruction):
    state, claim = history_material_case(instruction)
    result = evaluate(entries([claim]), state)
    assert decision(result)["choice"] == "PASS"
    assert decision(result)["fact_check"]["claims"][0]["sources"][0]["input_scope_verified"] is True


@pytest.mark.parametrize("violation", ["external", "truth", "unrelated", "assistant", "turn", "quote", "missing"])
def test_expanded_task_scope_does_not_relax_source_or_truth_checks(violation):
    instruction = "현재 조건에 맞는 영화를 모두 골라줘."
    state, claim = history_material_case(instruction)
    if violation == "external":
        claim["scope"] = "external_fact"
    elif violation == "truth":
        state["question"] += " 이 정보가 사실인지 검증해줘."
    elif violation == "unrelated":
        state["question"] = claim["scope_quote"] = "전국 독서 모임의 정원을 표로 정리해줘."
    elif violation == "assistant":
        state["history"][1].update(question="알려줘.", answer=NOTICE)
    elif violation == "turn":
        claim["sources"][0]["turn"] = 99
    elif violation == "quote":
        claim["sources"][0]["quote"] = "정원은 99명이다."
    else:
        claim["sources"] = []
    assert decision(evaluate(entries([claim]), state))["choice"] == "UNVERIFIED"


def test_source_quote_failure_is_distinct_and_model_cannot_forge_input_diagnostics():
    claim = input_claim()
    claim["sources"][0].update(quote="없는 인용문", input_quote_verified=True,
                                input_scope_verified=True, quote_verified=True)
    result = evaluate(entries([claim]))
    source = decision(result)["fact_check"]["claims"][0]["sources"][0]
    assert decision(result)["choice"] == "UNVERIFIED"
    assert source["input_quote_verified"] is False and source["input_scope_verified"] is True
    assert source["quote_verified"] is False
    assert "사용자 원문·턴" in source["verification_reason"]
    assert "원문과 일치하지만" not in source["verification_reason"]


@pytest.mark.parametrize("external_request", ["그리고 이 공지의 진위를 확인해 줘.", "실제 정원도 검색해줘.",
    "Also confirm the document's accuracy.", "Check whether the supplied claims are correct."])
def test_separate_material_scope_cannot_hide_external_verification_request(external_request):
    instruction = "정원을 표로 정리해줘."
    state = {**STATE, "question": "다음은 가상 공지야. " + NOTICE + instruction + external_request}
    assert decision(evaluate(entries([input_claim(scope_quote=instruction)]), state))["choice"] == "UNVERIFIED"


def test_separate_material_does_not_authorize_external_fact_or_assistant_evidence():
    instruction = "정원을 표로 정리해줘."
    state = {**STATE, "question": "다음은 가상 공지야. " + NOTICE + instruction,
             "history": [{"turn": 1, "question": "알려줘.", "answer": NOTICE}]}
    external = input_claim(scope="external_fact", scope_quote=instruction)
    assert decision(evaluate(entries([external]), state))["choice"] == "UNVERIFIED"
    assistant = input_claim(scope_quote=instruction)
    assistant["sources"][0].update(origin="history_user", turn=1)
    assert decision(evaluate(entries([assistant]), state))["choice"] == "UNVERIFIED"


def history_material_case(instruction="마지막으로 선택한 계획의 현재 정원을 한 표에 정리해줘."):
    state = {**STATE, "question": instruction, "history": [
        {"turn": 1, "question": "가상의 독서 모임 계획을 세울게.", "answer": "알겠습니다."},
        {"turn": 2, "question": NOTICE, "answer": "정원을 반영했습니다."},
    ]}
    claim = input_claim(scope_quote=instruction)
    claim["sources"][0].update(origin="history_user", turn=2)
    return state, claim


@pytest.mark.parametrize("instruction", [
    "마지막으로 선택한 계획의 현재 정원을 한 표에 정리해줘.",
    "지금까지 정해진 정원을 모아 표를 만들어줘.",
    "현재 계획의 정원만 모아서 표를 작성해줘.",
])
def test_retrospective_transformation_uses_material_introduction_in_user_history(instruction):
    state, claim = history_material_case(instruction)
    result = evaluate(entries([claim]), state)
    assert decision(result)["choice"] == "PASS"
    source = decision(result)["fact_check"]["claims"][0]["sources"][0]
    assert source["quote_verified"] is True and source["turn"] == 2
    assert result["verification"]["search_performed"] is False


@pytest.mark.parametrize("change", ["no_retrospection", "truth_request", "external_request",
    "assistant_material", "assistant_quote", "wrong_turn", "duplicate_turn", "wrong_quote",
    "question_origin", "external_scope", "missing_sources", "scope_from_history"])
def test_history_material_fallback_does_not_widen_source_or_fact_check_boundaries(change):
    state, claim = history_material_case()
    if change == "no_retrospection":
        state["question"] = claim["scope_quote"] = "전국 독서 모임의 정원을 표로 정리해줘."
    elif change == "truth_request":
        state["question"] += " 그 정원이 맞는지 확인해줘."
    elif change == "external_request":
        state["question"] += " 실제 운영 정원도 검색해줘."
    elif change == "assistant_material":
        state["history"][0].update(question="계획을 세울게.", answer="가상의 공지입니다.")
    elif change == "assistant_quote":
        state["history"][1].update(question="정원을 알려줘.", answer=NOTICE)
    elif change == "wrong_turn":
        claim["sources"][0]["turn"] = 3
    elif change == "duplicate_turn":
        state["history"].append(copy.deepcopy(state["history"][1]))
    elif change == "wrong_quote":
        claim["sources"][0]["quote"] = "정원은 20명입니다."
    elif change == "question_origin":
        claim["sources"][0].update(origin="question", turn=None)
    elif change == "external_scope":
        claim["scope"] = "external_fact"
    elif change == "missing_sources":
        claim["sources"] = []
    else:
        claim["scope_quote"] = state["history"][0]["question"]
    assert decision(evaluate(entries([claim]), state))["choice"] == "UNVERIFIED"


def test_fictional_plan_prompt_keeps_restatement_separate_from_user_value_evidence():
    prompt = build_prompt(STATE, QUESTIONS, True)
    assert "이전 assistant가 선택한 계획의 재진술" in prompt
    assert "사용자가 준 인원·예산·속성" in prompt
    assert "별도 외부 사실이나 사용자 제공값에 대한 검증 주장이 없는 경우에만" in prompt
    assert "sources가 비었거나 정확한 원문 인용을 확보하지 못했으면 PASS/FAIL을 제출하지 말고 UNVERIFIED" in prompt
    assert "이메일·메시지 작성" in prompt and "가상 후보 선택·계획·조건 변경" in prompt
    assert "가상 설정의 빈칸을 현실 웹검색으로 채우지 마세요" in prompt
    assert "실행 연도를 임의로 붙이지 말고" in prompt
    assert "실제 결제사의 정책을 검색하지 마세요" in prompt
    assert "실제 기관의 정책·과학적 설명 등 독립 외부 사실" in prompt


def test_context_prompt_selects_current_relevant_instructions_and_requires_pass_quotes():
    prompt = build_prompt(STATE, {"J6": {"instructions": "검토", "criteria": {"PASS": "유지", "FAIL": "누락"}}})
    assert "현재 답변에 적용되는 최신 유효 지시만" in prompt
    assert "반복 언급하지 않았다는 것만으로 망각으로 판단하지 마세요" in prompt
    assert "빈 answer_quote로 PASS를 제출하지 마세요" in prompt
    assert "현재 요청상 필수 내용이 누락된 FAIL만 빈 answer_quote" in prompt


@pytest.mark.parametrize("reference,search_enabled", [(None, True), ("Python list는 변경 가능합니다.", False), (None, False)])
def test_j5_prompt_requires_evaluator_to_check_unsourced_answers_without_changing_reference_policy(reference, search_enabled):
    state = {"question": "Python list는 변경 가능한가요?", "answer": "Python list는 변경 가능합니다."}
    if reference is not None:
        state["reference"] = reference
    original = copy.deepcopy(state)
    prompt = build_prompt(state, QUESTIONS, search_enabled)
    assert "답변 자체에 출처·링크·인용이 없다는 이유만으로 감점하거나 즉시 FAIL/UNVERIFIED로 판단하지 마세요" in prompt
    assert "reference가 없고 웹검색이 가능하면 평가자가 독립적으로 검색하고" in prompt
    assert "원문과 대조해 사실이면 PASS, 오류이면 FAIL" in prompt
    assert "검색·원문 대조를 시도한 뒤에도 참거짓을 확인하지 못한 주장만 UNVERIFIED" in prompt
    assert "reference가 있으면 그 자료에 있는 내용만 근거로 basis=reference" in prompt
    assert "자료가 다루지 않는 주장은 UNVERIFIED" in prompt
    assert "검색이 꺼져 있고 reference도 없으면 basis=none, J5=UNVERIFIED" in prompt
    assert ("reference 는 J5 DATA 신뢰도 판정의 기준 데이터" in prompt) is (reference is not None)
    assert ("J5 검색 절차:" in prompt) is search_enabled
    assert state == original


@pytest.mark.parametrize("source_change", [{"quote": "가짜 원문"}, {"turn": 1}, {"url": URL}])
def test_question_origin_requires_exact_quote_null_turn_and_no_external_url(source_change):
    claim = input_claim()
    claim["sources"][0].update(source_change, quote_verified=True, verification_reason="fabricated")
    result = evaluate(entries([claim]))
    assert decision(result)["choice"] == "UNVERIFIED"
    assert decision(result)["fact_check"]["claims"][0]["sources"][0]["quote_verified"] is False


@pytest.mark.parametrize("history_kind", ["user", "assistant_only", "wrong_turn", "duplicate"])
def test_history_input_evidence_uses_only_the_identified_user_turn(history_kind):
    request = "앞서 제공한 가상 공지를 요약해줘."
    prior = {"turn": 1, "question": NOTICE if history_kind != "assistant_only" else "공지 알려줘.", "answer": NOTICE}
    state = {**STATE, "question": request, "history": [prior] * (2 if history_kind == "duplicate" else 1)}
    claim = input_claim(scope_quote=request)
    claim["sources"][0].update(origin="history_user", turn=2 if history_kind == "wrong_turn" else 1)
    result = evaluate(entries([claim]), state)
    assert decision(result)["choice"] == ("PASS" if history_kind == "user" else "UNVERIFIED")


@pytest.mark.parametrize("available", [True, False])
def test_mixed_claims_keep_external_search_and_do_not_spend_source_slots_on_input(available):
    state = {**STATE, "answer": STATE["answer"] + " " + WEB_FACT}
    requests = []
    def fetch(url):
        requests.append(url)
        return fetched(url, status="ok" if available else "unsupported_content_type")
    result = evaluate(entries([input_claim(), web_claim()], "mixed"), state,
                      searched=True, observed=[URL], fetcher=fetch)
    assert requests == [URL]
    assert len(result["verification"]["source_checks"]) == 1
    assert decision(result)["choice"] == ("PASS" if available else "UNVERIFIED")
    claims = decision(result)["fact_check"]["claims"]
    assert claims[0]["verdict"] == "PASS"
    if not available:
        assert "HTML·일반 텍스트" in claims[1]["reason"]


def test_mixed_external_claim_without_observed_search_remains_unverified():
    state = {**STATE, "answer": STATE["answer"] + " " + WEB_FACT}
    result = evaluate(entries([input_claim(), web_claim()], "mixed"), state)
    assert decision(result)["fact_check"]["claims"][0]["verdict"] == "PASS"
    assert decision(result)["choice"] == "UNVERIFIED"


def test_prompt_and_schema_preserve_input_scope_and_cover_all_claims_with_bounded_sources():
    prompt = build_prompt(STATE, QUESTIONS, True)
    assert "최대 8개" in prompt and "HTML·일반 텍스트" in prompt
    assert "모든 사실 주장을 남기고" in prompt and "이전 assistant 답변을 독립 사실 근거로 쓰지" in prompt
    schema = output_schema(["J5"])["properties"]["J5"]["properties"]["fact_check"]["properties"]
    assert {"web", "reference", "input", "mixed", "none"} == set(schema["basis"]["enum"])
    claim = schema["claims"]["items"]
    assert {"scope", "scope_quote"} <= set(claim["required"])
    assert {"origin", "turn"} <= set(claim["properties"]["sources"]["items"]["required"])


def test_new_claim_schema_requires_explicit_nonnullable_scope_and_source_origin():
    schema = output_schema(["J5"])["properties"]["J5"]["properties"]["fact_check"]
    claim = schema["properties"]["claims"]["items"]
    source = claim["properties"]["sources"]["items"]
    for obj, key in [(claim, "scope"), (claim, "scope_quote"), (source, "origin")]:
        assert key in obj["required"]
        prop = obj["properties"][key]
        assert prop["type"] == "string"
        assert None not in prop.get("enum", [])
        assert prop.get("description")
    assert claim["properties"]["scope"]["enum"] == ["input_consistency", "external_fact"]
    assert source["properties"]["origin"]["enum"] == ["question", "reference", "history_user", "web"]
    assert source["properties"]["turn"]["type"] == ["integer", "null"]
    prompt = build_prompt(STATE, QUESTIONS, True)
    assert "basis=input이어도 각 claim에 scope=input_consistency" in prompt
    assert "scope_quote=null" not in prompt and "scope_quote는 빈 문자열" in prompt


def test_legacy_web_claim_without_new_scope_or_origin_keeps_provenance_validation():
    claim = web_claim()
    claim.pop("scope")
    claim.pop("scope_quote")
    claim["sources"][0].pop("origin")
    claim["sources"][0].pop("turn")
    state = {"question": "Are Python lists mutable?", "answer": WEB_FACT}
    result = evaluate(entries([claim], "web"), state, searched=True, observed=[URL], fetcher=fetched)
    assert decision(result)["choice"] == "PASS"
    result = evaluate(entries([claim], "web"), state)
    assert decision(result)["choice"] == "UNVERIFIED"


@pytest.mark.parametrize("original_choice", ["PASS", "UNVERIFIED"])
def test_missing_fact_claims_preserve_original_assessment_when_server_explains_gap(original_choice):
    values = entries([], "none", original_choice)
    values["J5"]["reason"] = "원래 모델의 설명"
    result = evaluate(values)
    assert decision(result)["choice"] == "UNVERIFIED"
    assert decision(result)["model_original_assessment"] == {"choice": original_choice, "reason": "원래 모델의 설명"}
    parsed = parse_decisions(result, ("J5",))
    assert parsed["J5"]["model_original_assessment"] == decision(result)["model_original_assessment"]


def test_abbreviated_context_quote_stays_unverified_and_preserves_original_model_assessment():
    state = {"question": "계속해줘.", "answer": "변경 계획만 작성합니다.",
             "history": [{"turn": 1, "question": "작업은 하지 말고 변경 계획만 작성해줘.", "answer": "알겠습니다."}]}
    value = {"choice": "PASS", "confidence": .8, "reason": "이전 지시 유지",
             "context_checks": [{"turn": 1, "instruction_quote": "작업은 하지 말고 ... 작성해줘.",
                                 "answer_quote": state["answer"], "decision": "PASS", "reason": "유지"}]}
    result = finalize_answers({"answers": {"J6": jev_answer(value)}}, state, {"J6": {}})
    entry = result["answers"]["J6"]
    assert entry["choice"] == "UNVERIFIED"
    assert "이전 턴" in entry["context_checks"][0]["reason"]
    assert entry["model_original_assessment"] == {"choice": "PASS", "reason": "이전 지시 유지"}
    assert parse_decisions(result, ("J6",))["J6"]["model_original_assessment"] == entry["model_original_assessment"]
    prompt = build_prompt(state, {"J6": {"instructions": "검토", "criteria": {"PASS": "유지", "FAIL": "누락"}}})
    assert "... 또는 …" in prompt and "원문에 있던 표식은 지우지" in prompt


def test_j5_prompt_requires_claims_even_when_search_fails_and_keeps_nonfactual_boundary():
    prompt = build_prompt(STATE, QUESTIONS, True)
    assert "검색 실패·원문 확인 실패·근거 부족이어도" in prompt
    assert "최소 한 번 실제 웹검색" in prompt and "NOT_APPLICABLE+claims=[]" in prompt
    assert "실제 가격·기관·날짜·과학적 원리" in prompt and "**None**" in prompt


@pytest.mark.parametrize("engine", ["codex", "openai", "anthropic"])
@pytest.mark.parametrize("basis", ["input", "mixed"])
def test_transports_share_input_and_mixed_finalization_without_real_calls(engine, basis, tmp_path):
    state = copy.deepcopy(STATE)
    claims = [input_claim()]
    if basis == "mixed":
        claims.append(web_claim())
        state["answer"] += " " + WEB_FACT
    values = entries(claims, basis)
    fetches = []
    def fetch(url):
        fetches.append(url)
        return fetched(url)
    if engine == "codex":
        def runner(cmd, **kwargs):
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text(json.dumps(values), encoding="utf-8")
            events = [{"type": "turn.completed"}]
            if basis == "mixed":
                events.insert(0, {"type": "item.completed", "item": {"type": "web_search", "results": [{"url": URL}]}})
            return SimpleNamespace(returncode=0, stdout="\n".join(map(json.dumps, events)), stderr="")
        client = CodexCliClient(CodexCliConfig(), runner, tmp_path, source_fetcher=fetch)
    else:
        def handler(request):
            if engine == "openai":
                events = [{"type": "web_search_call", "status": "completed", "action": {"type": "search", "sources": [{"url": URL}]}}] if basis == "mixed" else []
                raw = {"model": "fixture", "status": "completed", "output": [*events, {"type": "message", "content": [
                    {"type": "output_text", "text": json.dumps(values)}]}]}
            else:
                blocks = [{"type": "server_tool_use", "id": "search", "name": "web_search"},
                          {"type": "web_search_tool_result", "tool_use_id": "search", "content": [{"type": "web_search_result", "url": URL}]}] if basis == "mixed" else []
                raw = {"model": "claude-sonnet-5-5", "stop_reason": "tool_use", "content": [*blocks,
                       {"type": "tool_use", "id": "final", "name": FINAL_TOOL, "input": values}]}
            return httpx.Response(200, json=raw)
        cls, config = (SemanticApiClient, SemanticApiConfig()) if engine == "openai" else (AnthropicSemanticClient, AnthropicSemanticConfig())
        client = cls(config, "fixture-not-a-real-key", transport=httpx.MockTransport(handler), source_fetcher=fetch)
    call = client.system_one(state, QUESTIONS)
    assert call.status == "OK" and decision(call.body)["choice"] == "PASS"
    assert fetches == ([URL] if basis == "mixed" else [])
    assert decision(call.body)["fact_check"]["basis"] == basis
