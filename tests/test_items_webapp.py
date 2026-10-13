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
    assert REGISTRY_VERSION == "criteria-registry-v4" and data["version"] == REGISTRY_VERSION
    assert data["semantic_items"] == describe()
    assert len(data["semantic_items"]["subs"]) == 25 and len(data["semantic_items"]["items"]) == 39
    assert data["semantic_items"]["version"] == SEMANTIC_ITEMS_VERSION and data["semantic_items"]["reason_codes"][0] == "FACT_FALSE_CONFIRMED"
    assert [c["id"] for c in data["criteria"] if c["layer"] == "LLM"] == ["J1", "J2", "J3", "J4", "J5", "J6"]   # 기준 목록은 그대로
    json.dumps(data, ensure_ascii=False, allow_nan=False)


# --- 모델 간 요소 합집합 (webapp/semantic_union) ------------------------------------------------------------

from tests.test_jev_eval_items import NORMAL, run_items  # noqa: E402  (NORMAL: "각각 이유를 한 줄씩" → J4-2.a 요구별)
from webapp import semantic_union  # noqa: E402


def element(item_id, question_quote, decision="YES", answer_quote="- 레노버 아이디어패드"):
    return {"id": item_id, "decision": decision, "question_quote": question_quote, "answer_quote": answer_quote, "reason": "r", "turn": None, "fact": None}


def test_plan_finds_elements_listed_by_one_evaluation_but_missing_in_another_without_naming_models():
    full = ScriptedItems([{"J4-2.a": [element("J4-2.a#1", "각각"), element("J4-2.a#2", "이유를 한 줄씩")]}])
    partial = ScriptedItems([{"J4-2.a": [element("J4-2.a#1", "각각")]}])
    code_a, aiona = run_items([NORMAL], full)
    batch_a = {**code_a, "jev_eval": aiona}
    code_b, gpt = run_items([NORMAL], partial)
    batch_b = {**code_b, "jev_eval": gpt}
    tasks = semantic_union.plan({"aiona": batch_a, "gpt": batch_b})
    assert tasks == [{"provider": "gpt", "index": 0, "missing": {"J4-2.a": [{"question_quote": "이유를 한 줄씩", "seen_in": ["평가 1"]}]}}]
    assert semantic_union.plan({"aiona": batch_a, "gpt": batch_b}, blind_labels={"aiona": "C", "gpt": "A"})[0]["missing"]["J4-2.a"][0]["seen_in"] == ["평가 C"]
    assert semantic_union.plan({"aiona": batch_a}) == []                                  # 한 평가뿐이면 대조 없음
    legacy = {"cases": batch_b["cases"], "jev_eval": {**gpt, "pipeline": "legacy"}}
    assert semantic_union.plan({"aiona": batch_a, "gpt": legacy}) == []                   # legacy 레코드는 제외
    assert "J5-1.a" not in semantic_union.UNION_BASES and not any(b.startswith("J6") for b in semantic_union.UNION_BASES)


def test_reconcile_rechecks_only_the_missing_elements_keeps_verification_and_reaggregates_the_batch():
    full = ScriptedItems([{"J4-2.a": [element("J4-2.a#1", "각각"), element("J4-2.a#2", "이유를 한 줄씩")]}])
    partial = ScriptedItems([{"J4-2.a": [element("J4-2.a#1", "각각")]}])
    code_a, aiona = run_items([NORMAL], full)
    code_b, gpt = run_items([NORMAL], partial)
    evaluations = {"aiona": {**code_a, "jev_eval": aiona}, "gpt": {**code_b, "jev_eval": gpt}}
    # 합집합 재확인 판정자: 요소 전부를 다시 나열하되 빠졌던 요소는 '아니오'(이유가 한 줄씩 없음)
    judge = ScriptedItems([{"J4-2.a": [element("J4-2.a#1", "각각"), element("J4-2.a#2", "이유를 한 줄씩", "NO", "")]}])
    seen = []
    summary = semantic_union.reconcile(evaluations, judge, semantic_contexts={"gpt": {"collected_at": "2026-10-02"}},
                                       on_record=lambda provider, index, record: seen.append((provider, index)))
    assert summary["status"] == "COMPLETED" and summary["questions"] == 1 and summary["rechecked"] == 1 and summary["updated"] == 1
    assert summary["tasks"] == [{"provider": "gpt", "index": 0, "bases": ["J4-2.a"], "status": "OK", "changed": True, "error": None}]
    assert seen == [("gpt", 0)] and len(judge.calls) == 1
    call = judge.calls[0]
    assert list(call["questions"]) == ["J4-2.a"] and call["state"]["collected_at"] == "2026-10-02" and call["state"]["question"] == NORMAL["question"]
    recheck = call["questions"]["J4-2.a"]["recheck"]
    assert recheck["reason"] == "missing_elements" and recheck["elements"] == [{"question_quote": "이유를 한 줄씩", "seen_in": ["평가 1"]}]
    record = evaluations["gpt"]["jev_eval"]["records"][0]
    j4 = record["decisions"]["J4"]
    assert j4["decision"] == "FAIL" and [it["id"] for it in j4["subs"]["J4-2"]["items"]] == ["J4-2.a#1", "J4-2.a#2"]
    assert all(it["union_round"] == 1 for it in j4["subs"]["J4-2"]["items"]) and j4["subs"]["J4-2"]["items"][1]["quote_check"]["question"]["ok"] is True
    assert record["union"]["status"] == "OK" and record["union"]["bases"]["J4-2.a"] == {
        "elements": [{"question_quote": "이유를 한 줄씩", "seen_in": ["평가 1"]}], "before": ["J4-2.a#1"], "after": ["J4-2.a#1", "J4-2.a#2"]}
    assert record["union"]["changed"] is True and record["verification"]["union_calls"][0]["items"] == ["J4-2.a"]
    assert record["decisions"]["J1"]["decision"] == "PASS" and record["criteria"] == ["J1", "J2", "J3", "J4"]   # 다른 기준은 그대로
    assert "union" not in evaluations["aiona"]["jev_eval"]["records"][0]
    gpt_eval = evaluations["gpt"]["jev_eval"]
    assert gpt_eval["summary"]["J4"]["fail"] == 1 and gpt_eval["summary"]["J4"]["subs"]["J4-2"]["fail"] == 1
    assert gpt_eval["union"] == {"version": semantic_union.UNION_VERSION, "rechecked": 1, "updated": 1}
    assert evaluations["aiona"]["jev_eval"]["union"] == {"version": semantic_union.UNION_VERSION, "rechecked": 0, "updated": 0}
    assert evaluations["gpt"]["combined"]["problems"][0]["items"][0]["id"] == "J4"
    # 호출 오류는 이전 판정을 유지하고 기록만 남긴다; client 없음은 NOT_CONFIGURED
    broken = ScriptedItems(status="TIMEOUT")
    code_c, again = run_items([NORMAL], partial)
    evaluations["gpt"] = {**code_c, "jev_eval": again}
    summary = semantic_union.reconcile(evaluations, broken)
    assert summary["errors"] == 1 and summary["rechecked"] == 0 and again["records"][0]["union"]["status"] == "TIMEOUT"
    assert again["records"][0]["decisions"]["J4"]["decision"] == "PASS"
    assert semantic_union.reconcile(evaluations, None)["status"] == "NOT_CONFIGURED"


def test_union_recheck_keeps_other_criteria_and_review_flags():
    # 앞 대화 이력을 확인할 수 없어 붙은 J6 확인 필요는 합집합 재확인(J4 만 다시 판정) 뒤에도 남는다
    full = ScriptedItems([{"J4-2.a": [element("J4-2.a#1", "각각"), element("J4-2.a#2", "이유를 한 줄씩")]}])
    partial = ScriptedItems([{"J4-2.a": [element("J4-2.a#1", "각각")]}])
    code_a, aiona = run_items([NORMAL], full)
    code_b, gpt = run_items([NORMAL], partial)
    record = gpt["records"][0]
    record["decisions"]["J6"] = {"criterion_id": "J6", "name": "긴 대화 맥락", "decision": None, "status": "UNVERIFIED",
                                 "reason": "앞 대화 이력을 확인할 수 없습니다", "context_checks": []}
    record["criteria"].append("J6")
    record["review_required"] = True
    judge = ScriptedItems([{"J4-2.a": [element("J4-2.a#1", "각각"), element("J4-2.a#2", "이유를 한 줄씩", "NO", "")]}])
    semantic_union.reconcile({"aiona": {**code_a, "jev_eval": aiona}, "gpt": {**code_b, "jev_eval": gpt}}, judge)
    assert record["decisions"]["J4"]["decision"] == "FAIL" and record["decisions"]["J6"]["status"] == "UNVERIFIED"
    assert "J6" in record["criteria"] and record["review_required"] is True


def test_union_matches_elements_by_question_position_not_model_numbers():
    # 재확인 응답이 빠졌던 요소에 기존 번호(#1)를 다시 쓰더라도, 기존 '각각' 요소의 아니오는 사라지지 않는다
    full = ScriptedItems([{"J4-2.a": [element("J4-2.a#1", "각각"), element("J4-2.a#2", "이유를 한 줄씩")]}])
    partial = ScriptedItems([{"J4-2.a": [element("J4-2.a#1", "각각", "NO", "")]}])
    code_a, aiona = run_items([NORMAL], full)
    code_b, gpt = run_items([NORMAL], partial)
    assert gpt["records"][0]["decisions"]["J4"]["decision"] == "FAIL"
    judge = ScriptedItems([{"J4-2.a": [element("J4-2.a#1", "이유를 한 줄씩")]}])
    semantic_union.reconcile({"aiona": {**code_a, "jev_eval": aiona}, "gpt": {**code_b, "jev_eval": gpt}}, judge)
    record = gpt["records"][0]
    items = record["decisions"]["J4"]["subs"]["J4-2"]["items"]
    assert [(it["id"], it["question_quote"], it["decision"]) for it in items] == [
        ("J4-2.a#1", "이유를 한 줄씩", "YES"), ("J4-2.a#2", "각각", "NO")]
    assert record["decisions"]["J4"]["decision"] == "FAIL" and record["union"]["bases"]["J4-2.a"]["after"] == ["J4-2.a#1", "J4-2.a#2"]


class ElementJudge(ScriptedItems):
    """답변에 따라 요소를 다르게 나열하는 대역 — AIONA 답변은 요소 2개, API 답변은 1개. 합집합 재확인(recheck 블록)에는 전부 답한다."""

    def __init__(self):
        super().__init__()
        self.union_calls = []

    def system_one(self, state, questions, *, cancel_event=None):
        if "J4-2.a" in questions and (questions["J4-2.a"].get("recheck") or {}).get("reason") == "missing_elements":
            self.union_calls.append(copy.deepcopy(questions["J4-2.a"]["recheck"]))
            self.rounds = [{"J4-2.a": [element("J4-2.a#1", "각각", answer_quote="답변 하나"), element("J4-2.a#2", "이유를 한 줄씩", "NO", "")]}]
        elif state["answer"] == "답변 하나":
            self.rounds = [{"J4-2.a": [element("J4-2.a#1", "각각", answer_quote="답변 하나")]}]
        else:
            self.rounds = [{"J4-2.a": [element("J4-2.a#1", "각각", answer_quote=state["answer"][:8]), element("J4-2.a#2", "이유를 한 줄씩", answer_quote=state["answer"][:8])]}]
        return super().system_one(state, questions, cancel_event=cancel_event)


def test_comparison_run_applies_the_union_pass_after_all_models_and_replays_updated_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(jev_eval, "resolve_semantic_pipeline", lambda *_, **__: "items")
    judge = ElementJudge()

    def handler(request):
        return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '답변 하나'}]}]})
    service = make_service(tmp_path, judge, handler)
    rid = service.start({'cases': [{'question': NORMAL['question'], 'answer': NORMAL['answer']}], 'providers': ['gpt'], 'blind': True, 'auto_evaluate': True})['run_id']
    until(lambda: service.status(rid)['status'] == 'COMPLETED')
    final = service.result(rid)
    union = final['semantic_union']
    assert union['status'] == 'COMPLETED' and union['rechecked'] == 1 and union['updated'] == 1 and union['tasks'][0]['provider'] == 'gpt'
    assert len(judge.union_calls) == 1 and judge.union_calls[0]['elements'][0]['question_quote'] == '이유를 한 줄씩'
    label = final['blind']['labels']['aiona']
    assert judge.union_calls[0]['elements'][0]['seen_in'] == [f'평가 {label}']   # 모델 이름이 아니라 블라인드 별칭
    gpt = final['evaluations']['gpt']
    assert gpt['jev_eval']['records'][0]['decisions']['J4']['decision'] == 'FAIL' and gpt['jev_eval']['union']['updated'] == 1
    assert gpt['jev_eval']['summary']['J4']['fail'] == 1
    assert any(it['id'] == 'J4' and it['source'] == '의미 평가' for it in gpt['combined']['problems'][0]['items'])   # CODE FAIL 도 함께 있는 답변
    assert final['evaluations']['aiona']['jev_eval']['records'][0]['decisions']['J4']['decision'] == 'PASS'
    rows = service.event_stream(rid).snapshot()['rows']
    gpt_row = next(row for row in rows.values() if row['model'] == 'GPT')   # 화면용 이벤트는 실제 모델명 (블라인드는 평가 요청에만)
    assert gpt_row['criteria']['J4']['result'] == 'FAIL' and gpt_row['status'] == 'DONE'
    verdicts = [c['result'] for c in gpt_row['criteria'].values()]   # 교체된 J4 행은 이전 PASS 를 빼고 FAIL 로 센다 (CODE FAIL 과 합산)
    assert gpt_row['fail'] == verdicts.count('FAIL') >= 1 and gpt_row['pass'] == verdicts.count('PASS')
    saved = json.loads((tmp_path / 'runs' / f'{rid}.json').read_text(encoding='utf-8'))
    assert saved['semantic_union']['rechecked'] == 1 and saved['evaluations']['gpt']['jev_eval']['records'][0]['union']['status'] == 'OK'
