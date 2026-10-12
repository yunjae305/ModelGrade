"""Four-model collection through production adapters; no external API calls."""
import copy
import json
import threading
import time

import httpx
import pytest

from code_eval.codex_cli_client import CodexCliConfig
from code_eval.jev_client import JevCall
from code_eval.semantic_cli_common import build_prompt
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService


def until(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(.01)
    raise AssertionError('comparison did not reach expected state')


class CapturingJudge:
    config = CodexCliConfig(executable='fixture-only-no-cli-launched')

    def __init__(self, pause=False):
        self.calls = []
        self.entered, self.release = threading.Event(), threading.Event()
        if not pause:
            self.release.set()

    def system_one(self, state, questions):
        self.calls.append(copy.deepcopy({'state': state, 'questions': questions}))
        self.entered.set()
        assert self.release.wait(15)
        return JevCall('OK', body={'model': 'gpt-6.1-sol', 'answers': {
            cid: {'choice': 'PASS', 'probabilities': {'PASS': .95, 'FAIL': .05},
                  'confidence': .9, 'reason': '대역 검증용 판정'} for cid in questions}})


def adapter_fixture():
    sent, answers, raw = {}, {}, {}
    def handle(req):
        provider = {'api.openai.com': 'gpt', 'api.anthropic.com': 'claude',
                    'generativelanguage.googleapis.com': 'gemini'}[req.url.host]
        body = json.loads(req.content)
        if provider == 'gemini':
            messages = [{'role': 'assistant' if item['role'] == 'model' else 'user',
                         'content': ''.join(part['text'] for part in item['parts'])} for item in body['contents']]
        else:
            messages = body.get('input') or body['messages']
        sent.setdefault(provider, []).append(messages)
        answer = '  원문 응답 ' + {'gpt': '하나', 'claude': '둘', 'gemini': '셋'}[provider] + ' ' + str(len(sent[provider])) + '\r\n```json\n{"value":"\\n"}\n```\n  '
        answers[(provider, messages[-1]['content'])] = answer
        if provider == 'gpt':
            value = {'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': answer[:9]}, {'type': 'output_text', 'text': answer[9:]}]}]}
        elif provider == 'claude':
            value = {'stop_reason': 'end_turn', 'content': [{'type': 'thinking', 'thinking': 'not answer'}, {'type': 'text', 'text': answer}]}
        else:
            value = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
                {'thought': True, 'text': 'not answer'}, {'text': answer}]}}]}
        value['model'] = 'PRIVATE_PROVIDER_MODEL_' + provider
        raw[(provider, messages[-1]['content'])] = value
        return httpx.Response(200, json=value)
    return handle, sent, answers, raw


def make_service(tmp_path, judge, handler):
    evaluator = CodeEvalService(save_dir=tmp_path / 'eval', jev_resolver=lambda: (judge, judge.config, None))
    service = ComparisonService(evaluator, save_dir=tmp_path / 'runs',
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    for provider in ('gemini', 'gpt', 'claude'):
        service.configure(provider, {'key': 'PRIVATE_KEY_' + provider, 'model': 'PRIVATE_PROVIDER_MODEL_' + provider})
    return service


def test_four_models_auto_evaluate_with_blind_identity_and_full_own_history(tmp_path):
    judge = CapturingJudge(pause=True)
    handler, sent, answers, raw = adapter_fixture()
    service = make_service(tmp_path, judge, handler)
    cases = [
        {'id': 'Q2', 'question': '  앞 답변을 설명해 줘.\r\n', 'answer': 'AIONA라는 원문 이름은 보존합니다.\r\n',
         'conversation_id': 'conversation', 'turn': 2},
        {'id': 'Q3', 'question': '원문 ' + '긴 질문\r\n' * 600, 'answer': '  긴 답변 ' + '본문\n' * 900 + ' ',
         'reference': {'fact': '동일 참조 원문'}, 'source': 'PRIVATE_UPLOAD_SOURCE'},
        {'id': 'Q1', 'question': '첫째 질문\n ', 'answer': '  첫째 원문 답변\r\n',
         'conversation_id': 'conversation', 'turn': 1}]
    try:
        rid = service.start({'cases': cases, 'providers': ['gemini', 'gpt', 'claude'], 'blind': True, 'auto_evaluate': True})['run_id']
        assert judge.entered.wait(5)
        live = service.result(rid)
        assert live['status'] == 'EVALUATING' and live['done'] == 9
        plan = copy.deepcopy(live['blind'])
        assert sorted(plan['order']) == ['aiona', 'claude', 'gemini', 'gpt']
        assert [plan['labels'][p] for p in plan['order']] == ['모델 A', '모델 B', '모델 C', '모델 D']
        assert not plan['provider_metadata_sent_to_judge']
        saved = json.loads((tmp_path / 'runs' / (rid + '.json')).read_text(encoding='utf-8'))
        assert saved['done'] == 9 and saved['blind'] == plan
        for provider in ('gemini', 'gpt', 'claude'):
            assert len(sent[provider]) == 3
            by_question = {m[-1]['content']: m for m in sent[provider]}
            assert by_question[cases[1]['question']] == [{'role': 'user', 'content': cases[1]['question']}]
            assert by_question[cases[0]['question']] == [
                {'role': 'user', 'content': cases[2]['question']},
                {'role': 'assistant', 'content': answers[provider, cases[2]['question']]},
                {'role': 'user', 'content': cases[0]['question']}]
            for index, case in enumerate(cases):
                row = live['datasets'][provider][index]
                assert row['question'] == case['question']
                assert row['answer'] == answers[provider, case['question']]
                assert row['collection']['raw'] == raw[provider, case['question']]
        judge.release.set()
        until(lambda: service.status(rid)['status'] == 'COMPLETED')
        final = service.result(rid)
        assert final['eval_done'] == 4 and len(judge.calls) == 12
        assert final['blind'] == plan
        assert all(b['jev_eval']['statuses'] == {'SUCCESS': 3} for b in final['evaluations'].values())
        for request in judge.calls:
            state = request['state']
            assert set(state) <= {'question', 'answer', 'reference', 'history'}
            prompt = build_prompt(state, request['questions'])
            assert 'PRIVATE_' not in prompt  # provider IDs, source metadata and keys stay out
            if state['question'] == cases[0]['question']:
                owner = next(p for p in final['providers'] if final['datasets'][p][0]['answer'] == state['answer'])
                assert state['history'] == [{'turn': 1, 'question': cases[2]['question'], 'answer': final['datasets'][owner][2]['answer']}]
            if state['question'] == cases[1]['question']:
                assert state['reference'] == cases[1]['reference']
        assert any(r['state']['answer'] == cases[1]['answer'] for r in judge.calls)
        assert any(r['state']['answer'] == cases[0]['answer'] for r in judge.calls)
        assert 'PRIVATE_KEY_' not in service.export(rid).decode()
        restarted = ComparisonService(service.evaluator, save_dir=tmp_path / 'runs')
        assert restarted.result(rid)['blind'] == plan
    finally:
        judge.release.set()


def test_blind_aliases_survive_failed_evaluation_and_manual_retry(tmp_path):
    judge = CapturingJudge()
    handler, sent, _, _ = adapter_fixture()
    service = make_service(tmp_path, judge, handler)
    def fail_resolver():
        raise ValueError('fixture evaluator failure')
    service.evaluator._jev_resolver = fail_resolver
    rid = service.start({'cases': [{'question': '질문', 'answer': '원문'}],
        'providers': ['gemini', 'gpt', 'claude'], 'blind': True, 'auto_evaluate': True})['run_id']
    until(lambda: service.status(rid)['status'] == 'ERROR')
    plan = service.status(rid)['blind']
    assert sum(map(len, sent.values())) == 3 and not judge.calls
    service.evaluator._jev_resolver = lambda: (judge, judge.config, None)
    service.evaluate(rid, {})
    until(lambda: service.status(rid)['status'] == 'COMPLETED')
    assert service.status(rid)['blind'] == plan
    assert sum(map(len, sent.values())) == 3 and len(judge.calls) == 4


def test_all_models_share_one_judge_configuration_for_a_blind_run(tmp_path):
    judge = CapturingJudge()
    handler, _, _, _ = adapter_fixture()
    service = make_service(tmp_path, judge, handler)
    resolutions = []
    def changing_resolver():
        resolutions.append(True)
        assert len(resolutions) == 1, 'judge settings must be frozen for the full comparison'
        return judge, judge.config, None
    service.evaluator._jev_resolver = changing_resolver
    rid = service.start({'cases': [{'question': 'q', 'answer': 'a'}], 'providers': ['gemini', 'gpt', 'claude'],
                         'blind': True, 'auto_evaluate': True})['run_id']
    until(lambda: service.status(rid)['status'] == 'COMPLETED')
    assert len(resolutions) == 1 and len(judge.calls) == 4
    manifests = [b['jev_eval']['client'] for b in service.result(rid)['evaluations'].values()]
    assert all(m == manifests[0] for m in manifests)


@pytest.mark.parametrize('workers,per_model', [(4, 1), (8, 2)])
def test_semantic_parallelism_preserves_blind_inputs_full_history_and_call_count(tmp_path, workers, per_model):
    judge = CapturingJudge(pause=True)
    handler, sent, _, _ = adapter_fixture()
    service = make_service(tmp_path, judge, handler)
    service.semantic_workers = workers
    cases = [{'id': f'T{i}', 'question': f'질문 {i}', 'answer': f'AIONA 원문 {i}',
              'conversation_id': 'C', 'turn': i} for i in range(1, 4)]
    rid = service.start({'cases': cases, 'providers': ['gemini', 'claude', 'gpt'],
                         'blind': True, 'auto_evaluate': True})['run_id']
    try:
        until(lambda: len(judge.calls) == workers)
        live = service.result(rid)
        assert live['evaluation_execution'] == {
            'semantic_workers_per_model': per_model, 'semantic_workers_total': workers}
        owners = {row['answer']: p for p, rows in live['datasets'].items() for row in rows}
        assert {p: sum(owners[c['state']['answer']] == p for c in judge.calls)
                for p in live['providers']} == {p: per_model for p in live['providers']}
        # Later turns can be judged before earlier judgments finish: their entire
        # history already exists from collection and must not use judge outputs.
        for request in judge.calls:
            state = request['state']
            assert set(state) <= {'question', 'answer', 'reference', 'history'}
            if state['question'] == '질문 2':
                prior = live['datasets'][owners[state['answer']]][0]
                assert state['history'] == [{'turn': 1, 'question': prior['question'], 'answer': prior['answer']}]
        # Configuration changes cannot alter an in-flight run's frozen limits.
        service.semantic_workers = 16
    finally:
        judge.release.set()
    until(lambda: service.status(rid)['status'] == 'COMPLETED')
    final = service.result(rid)
    assert len(judge.calls) == 12
    assert all(len(sent[p]) == 3 for p in ('gemini', 'claude', 'gpt'))
    assert final['evaluation_execution']['semantic_workers_total'] == workers
    assert all(batch['jev_eval']['statuses'] == {'SUCCESS': 3} for batch in final['evaluations'].values())


@pytest.mark.parametrize('options', [{'blind': 'true'}, {'auto_evaluate': 1}, {'auto_evaluate': True, 'aiona_config': []}])
def test_invalid_run_flags_rejected_before_requests(tmp_path, options):
    judge = CapturingJudge()
    service = make_service(tmp_path, judge, lambda _: pytest.fail('must not collect'))
    with pytest.raises(ValueError):
        service.start({'cases': [{'question': 'q', 'answer': 'a'}], 'providers': ['gpt'], **options})


def test_auto_collection_cancel_does_not_start_judge(tmp_path):
    judge = CapturingJudge()
    entered, release = threading.Event(), threading.Event()
    def handler(req):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '  reply\n'}]}]})
    service = make_service(tmp_path, judge, handler)
    rid = service.start({'cases': [{'question': 'q1', 'answer': 'a1'}, {'question': 'q2', 'answer': 'a2'}],
                         'providers': ['gpt'], 'blind': True, 'auto_evaluate': True})['run_id']
    try:
        assert entered.wait(3)
        service.cancel(rid)
    finally:
        release.set()
    until(lambda: service.status(rid)['status'] == 'CANCELLED')
    final = service.result(rid)
    assert not judge.calls and not final['evaluations']
    assert 'blind' not in final and 'collection_completed_at' not in final
    assert final['datasets']['gpt'][0]['answer'] == '  reply\n'
    assert final['datasets']['gpt'][1] is None


@pytest.mark.parametrize('selection', [[], ['gpt'], ['gpt', 'gemini'], ['gpt'] * 3,
                                      ['gpt', 'gemini', {}], None, 'all'])
def test_workspace_rejects_partial_comparison_without_calls_or_saved_run(tmp_path, selection):
    judge = CapturingJudge()
    handler, sent, _, _ = adapter_fixture()
    service = make_service(tmp_path, judge, handler)
    with pytest.raises(ValueError, match='4개 모델'):
        service.start_comparison({'cases': [{'question': 'q', 'answer': 'a'}], 'providers': selection})
    assert not sent and not judge.calls and not service.history()


@pytest.mark.parametrize('provider', ['gemini', 'claude', 'gpt'])
def test_workspace_requires_each_connection_before_any_collection(tmp_path, provider):
    judge = CapturingJudge()
    handler, sent, _, _ = adapter_fixture()
    service = make_service(tmp_path, judge, handler)
    service.configure(provider, {'clear': True})
    with pytest.raises(ValueError, match='연결이 없습니다'):
        service.start_comparison({'cases': [{'question': 'q', 'answer': 'a'}]})
    assert not sent and not judge.calls and not service.history()


def test_workspace_always_collects_three_then_blind_evaluates_four(tmp_path):
    judge = CapturingJudge()
    handler, sent, _, _ = adapter_fixture()
    service = make_service(tmp_path, judge, handler)
    rid = service.start_comparison({'cases': [{'question': '  원문 질문\r\n', 'answer': '  원문 답변\r\n'}],
                                    'blind': False, 'auto_evaluate': False})['run_id']
    until(lambda: service.status(rid)['status'] == 'COMPLETED')
    run = service.result(rid)
    assert sorted(run['providers']) == ['aiona', 'claude', 'gemini', 'gpt']
    assert run['blind']['enabled'] and run['auto_evaluate']
    assert all(len(sent[p]) == 1 for p in ('gemini', 'claude', 'gpt'))
    assert all(sent[p][0] == [{'role': 'user', 'content': '  원문 질문\r\n'}] for p in sent)
    assert len(judge.calls) == 4 and run['eval_done'] == 4


def test_blind_assignment_and_judge_wait_for_last_provider_response(tmp_path):
    judge = CapturingJudge(pause=True)
    handler, sent, _, _ = adapter_fixture()
    last_entered, release_last = threading.Event(), threading.Event()
    handler_lock = threading.Lock()
    def gated_handler(request):
        with handler_lock:
            response = handler(request)
            is_last = sum(map(len, sent.values())) == 3
        if is_last:
            last_entered.set()
            assert release_last.wait(10)
        return response
    service = make_service(tmp_path, judge, gated_handler)
    try:
        rid = service.start_comparison({'cases': [{'question': 'q', 'answer': 'a'}]})['run_id']
        assert last_entered.wait(5)
        # Entering the last HTTP handler does not mean the first two responses
        # have reached the service's progress update yet. Keep the last gated.
        until(lambda: service.status(rid)['done'] == 2)
        assert not release_last.is_set()
        collecting = service.result(rid)
        assert collecting['status'] == 'COLLECTING' and collecting['done'] == 2
        assert collecting['blind_requested'] and 'blind' not in collecting
        assert 'collection_completed_at' not in collecting
        assert not collecting['evaluations'] and not judge.calls
        with pytest.raises(ValueError, match='응답 수집을 마친 후'):
            service.evaluate(rid, {})
        release_last.set()
        assert judge.entered.wait(5)
        evaluating = service.result(rid)
        assert evaluating['done'] == 3 and evaluating['status'] == 'EVALUATING'
        assert evaluating['blind']['enabled']
        assert evaluating['collection_completed_at'] <= evaluating['blind']['applied_at'] == evaluating['eval_started_at']
        judge.release.set()
        until(lambda: service.status(rid)['status'] == 'COMPLETED')
        assert len(judge.calls) == 4
    finally:
        release_last.set()
        judge.release.set()
