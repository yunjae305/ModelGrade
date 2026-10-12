"""items 파이프라인의 webapp 실시간 반영 — 실행 조건 기록·의미 평가 문맥 전달·SSE 행·평가 프로필·기준 목록. 모델·네트워크 호출 없음.

평가자는 tests.test_jev_eval_items.ScriptedItems 대역, 답변 수집은 MockTransport 다. SEMANTIC_PIPELINE 은 conftest 가 legacy 로
고정하므로 items 는 monkeypatch 로 켠다 (comparison_service 는 jev_eval.resolve_semantic_pipeline 을 실행 시작 시점에 한 번 읽는다).
"""
import copy
import json

import httpx
import pytest

from code_eval import jev_eval
from code_eval.criteria_registry import REGISTRY_VERSION, registry
from code_eval.jev_client import JevClientConfig
from code_eval.semantic_items import SEMANTIC_ITEMS_VERSION, describe
from code_eval.semantic_quotes import ITEM_QUOTE_POLICY, RECHECK_POLICY
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.evaluation_events import EvaluationEvents, record_events
from webapp.version_registry import evaluation_profile
from tests.test_blind_comparison import until
from tests.test_jev_eval_items import ScriptedItems

AIONA_ANSWER = "카페인은 아데노신 수용체를 막습니다. 출처 https://official.example/caffeine 를 확인했습니다."
GPT_ANSWER = "반감기는 약 5시간입니다. 출처 https://official.example/half-life 를 확인했습니다."
QUESTION = "저녁에 커피 마시면 잠을 잘 못 자는 이유가 뭐야? 카페인이 몸에 얼마나 오래 남아?"


def make_service(tmp_path, judge, handler):
    evaluator = CodeEvalService(save_dir=tmp_path / 'eval', jev_resolver=lambda: (judge, judge.config, None))
    service = ComparisonService(evaluator, save_dir=tmp_path / 'runs',
                                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    service.configure('gpt', {'key': 'PRIVATE_KEY_gpt', 'model': 'PRIVATE_PROVIDER_MODEL_gpt'})
    return service


def gpt_handler(request):
    return httpx.Response(200, json={'status': 'completed', 'output': [
        {'type': 'message', 'content': [{'type': 'output_text', 'text': GPT_ANSWER}]}]})


def items_record(**extra):
    item = lambda item_id, decision="YES", **kw: {"id": item_id, "decision": decision, "question_quote": "q", "answer_quote": "a",
                                                  "reason": "r", "turn": None, "fact": None, "origin": "question", **kw}
    return {"case_id": "Q1", "status": "SUCCESS", "criteria": ["J1", "J2"], "pipeline": "items", "error": None, "review_required": False,
            "recheck": {"policy": RECHECK_POLICY, "rounds": 2, "calls": [], "unverifiable": ["J2-1.a"]},
            "items": {"rechecks": 2, "unverifiable": 1, "fulfillment": 0.75},
            "decisions": {
                "J1": {"criterion_id": "J1", "name": "의도 충족성", "decision": "PASS", "reason": "적용 항목 모두 예",
                       "subs": {"J1-1": {"sub_id": "J1-1", "name": "요청 작업 수행", "decision": "PASS", "items": [item("J1-1.a")]}}},
                "J2": {"criterion_id": "J2", "name": "요청 적합성", "decision": "FAIL", "reason": "J2-1.a: 확인 불가", "reason_code": "ITEM_UNVERIFIABLE",
                       "subs": {"J2-1": {"sub_id": "J2-1", "name": "대상 수준", "decision": "FAIL",
                                         "items": [item("J2-1.a", "NO", unverifiable=True, reason_code="ITEM_UNVERIFIABLE"),
                                                   item("J2-1.b", "NO", reason="수준 불일치")]}}}}, **extra}


def test_comparison_run_passes_pipeline_and_semantic_context_and_records_execution_conditions(tmp_path, monkeypatch):
    monkeypatch.setattr(jev_eval, "resolve_semantic_pipeline", lambda *_, **__: "items")
    judge = ScriptedItems()
    service = make_service(tmp_path, judge, gpt_handler)
    rid = service.start({'cases': [{'question': QUESTION, 'answer': AIONA_ANSWER}], 'providers': ['gpt'], 'auto_evaluate': True,
                         'aiona_release': {'version': 'v1', 'collected_at': '2026-10-01', 'notes': ''}})['run_id']
    until(lambda: service.status(rid)['status'] == 'COMPLETED')
    final = service.result(rid)
    assert final['evaluation_execution']['pipeline'] == 'items' and final['evaluation_execution']['dispatch'] == 'realtime'
    calls = {call['state']['answer']: call for call in judge.calls}
    assert set(calls) == {AIONA_ANSWER, GPT_ANSWER}
    aiona, gpt = calls[AIONA_ANSWER], calls[GPT_ANSWER]
    assert aiona['state']['collected_at'] == '2026-10-01'                                   # AIONA: 등록한 수집일
    assert gpt['state']['collected_at'] == final['collection_completed_at'][:10]              # API 수집: 수집 완료 날짜
    assert all(q['type'] == 'item' for q in aiona['questions'].values())
    assert aiona['questions']['J5-3.a']['answer_tools'] == 'unknown' and gpt['questions']['J5-3.a']['answer_tools'] == 'none'
    for provider in ('aiona', 'gpt'):
        batch = final['evaluations'][provider]
        assert batch['jev_eval']['pipeline'] == 'items' and batch['jev_eval']['items_version'] == SEMANTIC_ITEMS_VERSION
        record = batch['jev_eval']['records'][0]
        assert record['pipeline'] == 'items' and record['status'] == 'SUCCESS' and 'subs' in record['decisions']['J1']
        assert record['applicability']['subs']['J5-3']['answer_tools'] == ('unknown' if provider == 'aiona' else 'none')
    profile = final['evaluation_profile']
    assert profile['complete'] and profile['fields']['pipeline'] == 'items'
    assert profile['fields']['items_version'] == SEMANTIC_ITEMS_VERSION and profile['fields']['quote_policy'] == ITEM_QUOTE_POLICY
    assert profile['fields']['recheck_policy'] == RECHECK_POLICY
    rows = service.event_stream(rid).snapshot()['rows']
    assert rows and all(row['status'] == 'DONE' and row['rechecks'] == 0 and row['unverifiable'] == 0 for row in rows.values())
    saved = json.loads((tmp_path / 'runs' / f'{rid}.json').read_text(encoding='utf-8'))
    assert saved['evaluation_execution']['pipeline'] == 'items' and saved['evaluation_profile'] == profile


def test_legacy_run_records_legacy_pipeline_and_sends_no_context(tmp_path):
    from tests.test_blind_comparison import CapturingJudge
    judge = CapturingJudge()
    service = make_service(tmp_path, judge, gpt_handler)
    rid = service.start({'cases': [{'question': QUESTION, 'answer': AIONA_ANSWER}], 'providers': ['gpt'], 'auto_evaluate': True})['run_id']
    until(lambda: service.status(rid)['status'] == 'COMPLETED')
    final = service.result(rid)
    assert final['evaluation_execution']['pipeline'] == 'legacy' and final['evaluation_execution']['dispatch'] == 'realtime'
    assert all('collected_at' not in call['state'] for call in judge.calls)
    assert all(set(call['questions']) <= {'J1', 'J2', 'J3', 'J4', 'J5', 'J6'} for call in judge.calls)
    fields = final['evaluation_profile']['fields']
    assert fields['pipeline'] == 'legacy' and fields['items_version'] is None and fields['quote_policy'] is None and fields['recheck_policy'] is None
    assert 'rechecks' not in next(iter(service.event_stream(rid).snapshot()['rows'].values()))


def test_invalid_pipeline_setting_is_rejected_before_the_run_state_changes(tmp_path, monkeypatch):
    def broken(*_, **__):
        raise ValueError("알 수 없는 의미 평가 방식: 'fast' (legacy 또는 items)")
    monkeypatch.setattr(jev_eval, "resolve_semantic_pipeline", broken)
    service = make_service(tmp_path, ScriptedItems(), gpt_handler)
    rid = service.start({'cases': [{'question': QUESTION, 'answer': AIONA_ANSWER}], 'providers': ['gpt'], 'auto_evaluate': False})['run_id']
    until(lambda: service.status(rid)['status'] == 'READY')
    with pytest.raises(ValueError, match="legacy 또는 items"):
        service.evaluate(rid, {})
    assert service.status(rid)['status'] == 'READY' and 'evaluation_execution' not in service.result(rid)


def test_evaluation_profile_reads_missing_pipeline_as_legacy_and_items_fields_when_present():
    def run(jev):
        return {'evaluations': {'aiona': {'jev_eval': jev}}}
    assert evaluation_profile(run({'version': 'v', 'models': ['m']}))['fields']['pipeline'] == 'legacy'
    assert evaluation_profile({'evaluations': {}})['fields']['pipeline'] is None
    items = evaluation_profile(run({'version': 'v', 'pipeline': 'items', 'items_version': SEMANTIC_ITEMS_VERSION,
                                    'quote_policy': ITEM_QUOTE_POLICY, 'recheck_policy': RECHECK_POLICY}))['fields']
    assert (items['pipeline'], items['items_version'], items['quote_policy'], items['recheck_policy']) == (
        'items', SEMANTIC_ITEMS_VERSION, ITEM_QUOTE_POLICY, RECHECK_POLICY)
    legacy = evaluation_profile(run({'version': 'v', 'pipeline': 'legacy', 'items_version': None}))['fields']
    assert legacy['pipeline'] == 'legacy' and legacy['items_version'] is None


def test_record_events_carry_rechecks_and_unverifiable_counts_only_for_items_records():
    case = {"case_id": "Q1"}
    events = list(record_events(case, items_record(), 0))
    by_type = {}
    for event in events:
        by_type.setdefault(event["type"], []).append(event)
    j1, j2 = by_type["criterion_result"]
    assert "unverifiable" not in j1 and j2["unverifiable"] == 1 and j2["result"] == "FAIL" and j2["reason"] == "J2-1.a: 확인 불가"
    done = by_type["q_done"][0]
    assert done["rechecks"] == 2 and done["unverifiable"] == 1 and done["review_required"] is False
    hub = EvaluationEvents()
    hub.publish({"type": "run_start", "total": 1})
    for event in events:
        hub.publish({**event, "model": "aiona"})
    row = hub.snapshot()["rows"]["aiona:0"]
    assert row["rechecks"] == 2 and row["unverifiable"] == 1 and row["criteria"]["J2"]["unverifiable"] == 1 and "unverifiable" not in row["criteria"]["J1"]
    assert (row["pass"], row["fail"], row["unjudged"]) == (1, 1, 0)
    legacy = {"case_id": "Q1", "status": "SUCCESS", "criteria": ["J1"], "error": None,
              "decisions": {"J1": {"decision": "PASS", "reason": "r"}}}
    legacy_events = list(record_events(case, legacy, 0))
    assert "rechecks" not in legacy_events[-1] and "unverifiable" not in legacy_events[0]
    no_recheck = items_record(recheck=None)
    assert list(record_events(case, no_recheck, 0))[-1]["rechecks"] == 0


def test_registry_lists_semantic_items_from_the_single_definition():
    data = registry()
    assert REGISTRY_VERSION == "criteria-registry-v2" and data["version"] == REGISTRY_VERSION
    assert data["semantic_items"] == describe()
    assert len(data["semantic_items"]["subs"]) == 25 and len(data["semantic_items"]["items"]) == 39
    assert data["semantic_items"]["version"] == SEMANTIC_ITEMS_VERSION and data["semantic_items"]["reason_codes"][0] == "FACT_FALSE_CONFIRMED"
    assert [c["id"] for c in data["criteria"] if c["layer"] == "LLM"] == ["J1", "J2", "J3", "J4", "J5", "J6"]   # 기준 목록은 그대로
    json.dumps(data, ensure_ascii=False, allow_nan=False)
