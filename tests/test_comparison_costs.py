"""Runtime cost accounting follows real lifecycle boundaries; no paid requests."""
import copy
import json
import threading
import time

import httpx
import pytest

from code_eval.jev_client import JevCall, JevClientConfig
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService, ProviderError, request_answer
import webapp.comparison_service as comparison


def until(check):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('expected comparison state did not arrive')


class Judge:
    config = JevClientConfig(max_attempts=1)

    def __init__(self, paused=False):
        self.entered, self.release = threading.Event(), threading.Event()
        if not paused:
            self.release.set()

    def system_one(self, state, questions):
        self.entered.set()
        assert self.release.wait(5)
        return JevCall('OK', body={'model': 'fixture-judge', 'answers': {
            cid: {'choice': 'PASS', 'probabilities': {'PASS': .9, 'FAIL': .1},
                  'confidence': .9, 'reason': 'fixture'} for cid in questions}},
            usage={'input_tokens': 10, 'output_tokens': 2})


@pytest.fixture
def ledgers(monkeypatch):
    instances = []

    class Ledger:
        def __init__(self):
            self.collection, self.semantic, self.finishes = [], [], []
            self.lock = threading.Lock()
            self.private_marker = 'PRIVATE_RUNTIME_COST_LEDGER'
            instances.append(self)

        def record_collection(self, provider, model, raw=None):
            with self.lock:
                self.collection.append((provider, model, copy.deepcopy(raw)))

        def wrap_resolver(self, resolver):
            ledger = self

            def resolve():
                client, config, reason = resolver()
                if client is None:
                    return client, config, reason

                class Proxy:
                    def __getattr__(self, name):
                        return getattr(client, name)

                    def system_one(self, *args, **kwargs):
                        try:
                            return client.system_one(*args, **kwargs)
                        finally:
                            with ledger.lock:
                                ledger.semantic.append(True)

                return Proxy(), config, reason

            return resolve

        def finish(self, run_id, status):
            # Deliberately no dedup here: the service itself must not call twice.
            self.finishes.append((run_id, status))

    monkeypatch.setattr(comparison, 'CostLedger', Ledger)
    return instances


def response_raw(answer='answer', *, complete=True):
    return {'model': 'resolved-fixture-model', 'usage': {'input_tokens': 13, 'output_tokens': 3},
            'status': 'completed' if complete else 'incomplete',
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': answer}]}],
            'stop_reason': 'end_turn' if complete else 'max_tokens',
            'content': [{'type': 'text', 'text': answer}],
            'candidates': [{'finishReason': 'STOP' if complete else 'MAX_TOKENS',
                            'content': {'parts': [{'text': answer}]}}]}


def make_service(tmp_path, handler, judge=None):
    judge = judge or Judge()
    evaluator = CodeEvalService(save_dir=tmp_path / 'eval', jev_resolver=lambda: (judge, judge.config, None))
    service = ComparisonService(evaluator, save_dir=tmp_path / 'runs', registry_path=tmp_path / 'versions.json',
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    for provider in ('gpt', 'claude', 'gemini'):
        service.configure(provider, {'key': 'fixture-private-key', 'model': provider + '-configured'})
    return service


@pytest.mark.parametrize('provider', ['gpt', 'claude', 'gemini'])
@pytest.mark.parametrize('complete', [True, False])
def test_usage_observed_once_for_success_and_incomplete(provider, complete):
    raw, observed = response_raw(complete=complete), []
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw))) as client:
        result = request_answer(provider, {'key': 'fixture', 'model': 'configured'}, [], 1024, client,
                                usage_observer=observed.append)
    assert observed == [raw]
    assert result['status'] == ('SUCCESS' if complete else 'INCOMPLETE')
    assert result['raw'] == raw


@pytest.mark.parametrize('failure', ['answer', 'json', 'http', 'network'])
def test_usage_keeps_parsed_raw_or_reports_unknown_once(failure):
    raw, observed = response_raw(answer=123), []

    def handle(request):
        if failure == 'network':
            raise httpx.ReadTimeout('fixture timeout', request=request)
        if failure == 'http':
            return httpx.Response(429, json={'private_error': 'must not be observed'})
        if failure == 'json':
            return httpx.Response(200, text='not JSON')
        return httpx.Response(200, json=raw)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError):
            request_answer('gpt', {'key': 'fixture', 'model': 'configured'}, [], 1024, client,
                           usage_observer=observed.append)
    assert observed == [raw if failure == 'answer' else None]


def test_usage_observer_failure_does_not_change_answer():
    def broken(_):
        raise RuntimeError('reporter unavailable')

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response_raw()))) as client:
        result = request_answer('gpt', {'key': 'fixture', 'model': 'configured'}, [], 1024, client,
                                usage_observer=broken)
    assert result['status'] == 'SUCCESS' and result['answer'] == 'answer'


@pytest.mark.parametrize('blind', [False, True])
def test_auto_comparison_accounts_until_final_once_without_exposing_costs(tmp_path, ledgers, blind):
    judge = Judge(paused=True)
    service = make_service(tmp_path, lambda _: httpx.Response(200, json=response_raw()), judge)
    resolutions = []

    def resolve():
        resolutions.append(True)
        return judge, judge.config, None

    service.evaluator._jev_resolver = resolve
    rid = service.start({'cases': [{'question': 'q', 'answer': 'a'}],
                         'providers': ['gpt', 'claude', 'gemini'], 'blind': blind, 'auto_evaluate': True})['run_id']
    try:
        assert judge.entered.wait(3)
        assert len(ledgers) == 1
        ledger = ledgers[0]
        assert len(ledger.collection) == 3 and not ledger.finishes
        assert service.status(rid)['status'] == 'EVALUATING'
        judge.release.set()
        until(lambda: service.status(rid)['status'] == 'COMPLETED')
        assert ledger.finishes == [(rid, 'COMPLETED')]
        assert len(ledger.semantic) == 4
        assert len(resolutions) == (1 if blind else 4)
        assert not service._cost_ledgers
        outputs = [service.status(rid), service.result(rid), service.history(), service.event_stream(rid).snapshot(),
                   json.loads(service.export(rid))]
        outputs.extend(json.loads(p.read_text(encoding='utf-8')) for p in tmp_path.rglob('*.json'))
        text = json.dumps(outputs)
        assert 'PRIVATE_RUNTIME_COST_LEDGER' not in text and 'cost_usd' not in text and 'cost_ledger' not in text
        assert ledger.finishes == [(rid, 'COMPLETED')]
        restarted = make_service(tmp_path, lambda _: pytest.fail('history must not collect'))
        assert restarted.result(rid)['status'] == 'COMPLETED'
        assert len(ledgers) == 1 and ledger.finishes == [(rid, 'COMPLETED')]
    finally:
        judge.release.set()


def test_cancel_counts_inflight_but_not_unsent_requests_and_retry_starts_new_ledger(tmp_path, ledgers):
    entered, release = threading.Event(), threading.Event()

    def handle(_):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json=response_raw())

    service = make_service(tmp_path, handle)
    service.collection_workers = 1  # This test isolates unsent requests; parallel drain has separate coverage.
    rid = service.start({'cases': [{'question': 'q1', 'answer': 'a1'}, {'question': 'q2', 'answer': 'a2'}],
                         'providers': ['gpt'], 'auto_evaluate': True})['run_id']
    try:
        assert entered.wait(3)
        service.cancel(rid)
        assert not ledgers[0].finishes
        release.set()
        until(lambda: service.status(rid)['status'] == 'CANCELLED')
        assert len(ledgers[0].collection) == 1
        assert not ledgers[0].semantic
        assert ledgers[0].finishes == [(rid, 'CANCELLED')]
        service.evaluate(rid, {})
        until(lambda: service.status(rid)['status'] == 'COMPLETED')
        assert len(ledgers) == 2
        assert not ledgers[1].collection and len(ledgers[1].semantic) == 3
        assert ledgers[1].finishes == [(rid, 'COMPLETED')]
    finally:
        release.set()


def test_broken_conversation_skips_are_not_counted_as_requests(tmp_path, ledgers):
    service = make_service(tmp_path, lambda _: httpx.Response(429))
    cases = [{'question': f'q{turn}', 'answer': 'a', 'conversation_id': 'group', 'turn': turn} for turn in (1, 2)]
    rid = service.start({'cases': cases, 'providers': ['gpt']})['run_id']
    until(lambda: service.status(rid)['status'] == 'READY')
    assert ledgers[0].collection == [('gpt', 'gpt-configured', None)]
    assert ledgers[0].finishes == [(rid, 'READY')]
    assert service.status(rid)['progress']['gpt']['failed'] == 2


def test_evaluation_error_finishes_and_retry_does_not_rebill_collection(tmp_path, ledgers):
    service = make_service(tmp_path, lambda _: httpx.Response(200, json=response_raw()))
    resolver = service.evaluator._jev_resolver
    service.evaluator._jev_resolver = lambda: (_ for _ in ()).throw(RuntimeError('fixture resolver error'))
    rid = service.start({'cases': [{'question': 'q', 'answer': 'a'}], 'providers': ['gpt'],
                         'auto_evaluate': True, 'blind': True})['run_id']
    until(lambda: service.status(rid)['status'] == 'ERROR')
    assert len(ledgers) == 1 and len(ledgers[0].collection) == 1
    assert ledgers[0].finishes == [(rid, 'ERROR')]
    service.evaluator._jev_resolver = resolver
    service.evaluate(rid, {})
    until(lambda: service.status(rid)['status'] == 'COMPLETED')
    assert len(ledgers) == 2 and not ledgers[1].collection
    assert len(ledgers[1].semantic) == 2 and ledgers[1].finishes == [(rid, 'COMPLETED')]


@pytest.mark.parametrize('collection', [True, False])
def test_thread_start_failure_closes_ledger_once(tmp_path, ledgers, monkeypatch, collection):
    service = make_service(tmp_path, lambda _: pytest.fail('worker never started'))
    monkeypatch.setattr(comparison.threading.Thread, 'start', lambda _: (_ for _ in ()).throw(RuntimeError('no thread')))
    with pytest.raises(ValueError):
        service.start({'cases': [{'question': 'q', 'answer': 'a'}],
                       'providers': ['gpt'] if collection else [], 'auto_evaluate': True})
    run = service.history()[0]
    assert run['status'] == 'ERROR'
    assert len(ledgers) == 1 and ledgers[0].finishes == [(run['run_id'], 'ERROR')]
    assert not ledgers[0].collection and not ledgers[0].semantic and not service._cost_ledgers


def test_collection_worker_error_closes_ledger_without_imaginary_requests(tmp_path, ledgers):
    service = make_service(tmp_path, lambda _: pytest.fail('client never created'))
    service.client_factory = lambda: (_ for _ in ()).throw(RuntimeError('client setup failed'))
    rid = service.start({'cases': [{'question': 'q', 'answer': 'a'}], 'providers': ['gpt'], 'auto_evaluate': True})['run_id']
    until(lambda: service.status(rid)['status'] == 'ERROR')
    assert not ledgers[0].collection and not ledgers[0].semantic
    assert ledgers[0].finishes == [(rid, 'ERROR')]


def test_auto_evaluation_thread_start_error_does_not_finish_collection_twice(tmp_path, ledgers, monkeypatch):
    service = make_service(tmp_path, lambda _: httpx.Response(200, json=response_raw()))
    original_start = threading.Thread.start

    def start(thread):
        if getattr(getattr(thread, '_target', None), '__name__', None) == 'work':
            raise RuntimeError('evaluation thread cannot start')
        return original_start(thread)

    monkeypatch.setattr(comparison.threading.Thread, 'start', start)
    rid = service.start({'cases': [{'question': 'q', 'answer': 'a'}],
                         'providers': ['gpt'], 'auto_evaluate': True})['run_id']
    until(lambda: service.status(rid)['status'] == 'ERROR')
    assert len(ledgers) == 1 and len(ledgers[0].collection) == 1 and not ledgers[0].semantic
    assert ledgers[0].finishes == [(rid, 'ERROR')]
    assert not service._cost_ledgers


def test_version_reevaluation_starts_new_ledger_without_collection(tmp_path, ledgers):
    service = make_service(tmp_path, lambda _: pytest.fail('AIONA version evaluation never collects'))
    original_id = service.start_version_run({'cases': [{'question': 'q', 'answer': 'a'}],
                                             'aiona_release': {'version': '10.1'}})['run_id']
    until(lambda: service.status(original_id)['status'] == 'COMPLETED')
    new_id = service.start_version_run({'source_run_id': original_id,
                                       'aiona_release': {'version': '10.1'}})['run_id']
    until(lambda: service.status(new_id)['status'] == 'COMPLETED')
    assert new_id != original_id and len(ledgers) == 2
    assert all(not ledger.collection and len(ledger.semantic) == 1 for ledger in ledgers)
    assert [ledger.finishes for ledger in ledgers] == [[(original_id, 'COMPLETED')], [(new_id, 'COMPLETED')]]


def test_finish_failure_does_not_change_completion(tmp_path, ledgers, monkeypatch):
    service = make_service(tmp_path, lambda _: httpx.Response(200, json=response_raw()))
    monkeypatch.setattr(comparison.CostLedger, 'finish', lambda *args: (_ for _ in ()).throw(OSError('closed terminal')))
    rid = service.start({'cases': [{'question': 'q', 'answer': 'a'}], 'auto_evaluate': True})['run_id']
    until(lambda: service.status(rid)['status'] == 'COMPLETED')
    assert service.result(rid)['evaluations']['aiona']['jev_eval']['statuses'] == {'SUCCESS': 1}
    assert not service._cost_ledgers


def test_real_ledger_reports_four_models_once_and_never_serializes_money(tmp_path, capsys):
    from code_eval.semantic_api_client import SemanticApiConfig

    class ApiJudge(Judge):
        config = SemanticApiConfig(model='gpt-6.1-sol', web_search='disabled')

        def system_one(self, state, questions):
            call = super().system_one(state, questions)
            call.body['model'] = self.config.model
            call.usage = {'input_tokens': 1000, 'output_tokens': 100,
                          'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}
            call.accounting = {'model': self.config.model, 'usage': call.usage, 'web_search_calls': 0,
                               'private_marker': 'RUNTIME_ONLY_MONEY_METADATA'}
            return call

    models = {'gpt': 'gpt-6-luna', 'claude': 'claude-sonnet-5-5', 'gemini': 'gemini-3.8-flash'}

    def handle(request):
        provider = {'api.openai.com': 'gpt', 'api.anthropic.com': 'claude',
                    'generativelanguage.googleapis.com': 'gemini'}[request.url.host]
        raw = response_raw()
        raw['model'] = models[provider]
        raw['usage'] = {'input_tokens': 1000, 'output_tokens': 100,
                        'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}
        if provider == 'gemini':
            raw['modelVersion'] = raw.pop('model')
            raw['usageMetadata'] = {'promptTokenCount': 1000, 'candidatesTokenCount': 100}
            raw.pop('usage')
        return httpx.Response(200, json=raw)

    judge = ApiJudge(paused=True)
    service = make_service(tmp_path, handle, judge)
    for provider, model in models.items():
        service.configure(provider, {'key': 'fixture-private-key', 'model': model})
    rid = service.start_comparison({'cases': [{'question': 'fixture question', 'answer': 'fixture answer'}]})['run_id']
    try:
        assert judge.entered.wait(3)
        assert '[LLM 비용]' not in capsys.readouterr().out
        judge.release.set()
        until(lambda: service.status(rid)['status'] == 'COMPLETED')
        console = capsys.readouterr().out
        assert console.count('[LLM 비용]') == 1
        assert '수집 | gpt | gpt-6-luna | 추정액 $0.000150 | 호출 1건' in console
        assert '수집 | claude | claude-sonnet-5-5 | 추정액 $0.003000 | 호출 1건' in console
        assert '수집 | gemini | gemini-3.8-flash | 추정액 $0.001125 | 호출 1건' in console
        assert '평가 | openai_api | gpt-6.1-sol | 추정액 $0.012000 | 호출 4건' in console
        assert '추정 합계: $0.016275' in console
        assert '미확인' not in console and 'fixture-private-key' not in console and 'fixture question' not in console
        outputs = [service.status(rid), service.result(rid), service.history(), service.event_stream(rid).snapshot(),
                   json.loads(service.export(rid))]
        outputs.extend(json.loads(service.export(rid, provider)) for provider in ('aiona', *models))
        outputs.extend(json.loads(p.read_text(encoding='utf-8')) for p in tmp_path.rglob('*.json'))
        serialized = json.dumps(outputs, ensure_ascii=False)
        for marker in ('cost_usd', 'accounting', 'RUNTIME_ONLY_MONEY_METADATA', 'LLM 비용', 'USD', '0.016275'):
            assert marker not in serialized
        assert not capsys.readouterr().out
        assert not service._cost_ledgers
    finally:
        judge.release.set()
