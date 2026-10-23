"""Comparison workflow contracts. Provider HTTP is always mocked; no paid calls."""
import json
import threading
import time

import httpx
import pytest
from code_eval.engine import evaluate_batch

from webapp.code_eval_service import CodeEvalService
from webapp import comparison_service
from webapp.comparison_service import ComparisonService, ProviderError, normalize_dataset, request_answer


def wait(service, rid):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        run = service.status(rid)
        if run['status'] not in ('COLLECTING', 'EVALUATING'):
            return service.result(rid)
        time.sleep(.01)
    raise AssertionError('worker did not complete')


def service(tmp_path, handler):
    return ComparisonService(CodeEvalService(save_dir=tmp_path / 'eval'), save_dir=tmp_path / 'compare',
                             client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_existing_local_sample_takes_precedence_and_preserves_original_text(tmp_path, monkeypatch):
    path = tmp_path / 'private-sample.json'
    source = [{'id': 'LOCAL_1', 'question': '  원문 질문\r\n🙂', 'answer': '  원문 답변\r\n  '}]
    original = json.dumps(source, ensure_ascii=False).encode('utf-8')
    path.write_bytes(original)
    monkeypatch.setattr(comparison_service, 'DEFAULT_COMPARISON_DATASET', path)
    monkeypatch.setattr(comparison_service, 'PUBLIC_COMPARISON_DATASET', tmp_path / 'unused-public.json')
    result = service(tmp_path, lambda _: pytest.fail('sample must not call providers')).sample()
    assert result == {'name': path.name, 'cases': normalize_dataset({'cases': source})}
    assert path.read_bytes() == original


@pytest.mark.parametrize('provider,raw', [
    ('gpt', {'status': 'completed', 'output': [{'type': 'reasoning'}, {'type': 'message', 'content': [
        {'type': 'output_text', 'text': '  ```json\n'}, {'type': 'output_text', 'text': '{"a":"\\n"}\n```\n  '}]}]}),
    ('claude', {'stop_reason': 'end_turn', 'content': [{'type': 'thinking', 'thinking': 'hidden'},
        {'type': 'text', 'text': '  ```json\n'}, {'type': 'text', 'text': '{"a":"\\n"}\n```\n  '}]}),
    ('gemini', {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
        {'text': 'hidden', 'thought': True}, {'text': '  ```json\n'}, {'text': '{"a":"\\n"}\n```\n  '}]}}]})
])
def test_provider_adapter_preserves_text_and_raw_response(provider, raw):
    requests = []
    def handle(req):
        requests.append(req)
        return httpx.Response(200, json=raw)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = request_answer(provider, {'key': 'secret-key', 'model': 'test-model'},
                                [{'role': 'user', 'content': '  원문 질문\r\n'}], 8192, client)
    assert result['answer'] == '  ```json\n{"a":"\\n"}\n```\n  '
    assert result['raw'] == raw
    assert result['status'] == 'SUCCESS'
    body = json.loads(requests[0].content)
    sent = body.get('input') or body.get('messages') or body.get('contents')
    assert (sent[0].get('content') or sent[0]['parts'][0]['text']) == '  원문 질문\r\n'
    assert 'secret-key' not in str(requests[0].url)


@pytest.mark.parametrize('provider,raw', [
    ('gpt', {'status': 'incomplete', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'partial\n'}]}]}),
    ('claude', {'stop_reason': 'max_tokens', 'content': [{'type': 'text', 'text': 'partial\n'}]}),
    ('gemini', {'candidates': [{'finishReason': 'MAX_TOKENS', 'content': {'parts': [{'text': 'partial\n'}]}}]})
])
def test_truncated_answers_are_preserved_and_marked_incomplete(provider, raw):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw))) as client:
        result = request_answer(provider, {'key': 'key', 'model': 'model'}, [], 4096, client)
    assert result['status'] == 'INCOMPLETE'
    assert result['answer'] == 'partial\n'


def test_api_error_does_not_leak_keys_or_provider_error_body():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401, json={'error': 'secret-key'}))) as client:
        with pytest.raises(ProviderError) as exc:
            request_answer('gpt', {'key': 'secret-key', 'model': 'model'}, [], 1024, client)
    assert '401' in str(exc.value)
    assert 'secret-key' not in str(exc.value)


@pytest.mark.parametrize('cases', [[], [{'question': 'x'}], [{'question': ' ', 'answer': 'x'}],
    [{'question': 'x', 'answer': 2}], [{'question': 'x', 'answer': 'a', 'reference': {'n': float('inf')}}],
    [{'question': 'x', 'answer': 'a', 'conversation_id': 'c', 'turn': 2}],
    [{'question': 'x', 'answer': 'a', 'conversation_id': 'c', 'turn': 1}] * 2])
def test_bad_datasets_are_rejected_before_collection(cases):
    with pytest.raises(ValueError):
        normalize_dataset({'cases': cases})


def test_conversation_uses_each_models_own_history_and_preserves_input_indices(tmp_path):
    sent = []
    def handle(req):
        messages = json.loads(req.content)['messages']; sent.append(messages)
        return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': f' own-{len(sent)}\n'}]})
    ce = service(tmp_path, handle)
    ce.configure('claude', {'key': 'secret', 'model': 'claude-test'})
    cases = [{'id': 'duplicate', 'question': 'second', 'answer': 'AIONA second', 'displayed_text': 'displayed', 'conversation_id': 'c', 'turn': 2},
             {'id': 'duplicate', 'question': ' first\n', 'answer': 'AIONA first', 'conversation_id': 'c', 'turn': 1}]
    run = ce.start({'cases': cases, 'providers': ['claude']})
    result = wait(ce, run['run_id'])
    assert sent[0] == [{'role': 'user', 'content': ' first\n'}]
    assert sent[1] == [{'role': 'user', 'content': ' first\n'}, {'role': 'assistant', 'content': ' own-1\n'}, {'role': 'user', 'content': 'second'}]
    assert [c['answer'] for c in result['datasets']['claude']] == [' own-2\n', ' own-1\n']
    assert [c['case_index'] for c in result['datasets']['claude']] == [0, 1]
    assert 'displayed_text' not in result['datasets']['claude'][0]
    assert 'secret' not in ce.export(run['run_id']).decode()
    assert 'key' not in ce.providers()['claude']
    assert 'secret' not in next((tmp_path / 'compare').glob('*.json')).read_text(encoding='utf-8')


def test_failed_requests_are_excluded_not_scored_as_empty_answers(tmp_path):
    calls = []
    def handle(req):
        calls.append(req)
        if json.loads(req.content)['input'][-1]['content'] == '1 + 1은?':
            return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '1 + 1 = 2'}]}]})
        return httpx.Response(429)
    ce = service(tmp_path, handle); ce.configure('gpt', {'key': 'secret', 'model': 'test'})
    run = ce.start({'providers': ['gpt'], 'cases': [{'id': 'same', 'question': '1 + 1은?', 'answer': '2'}, {'id': 'same', 'question': '2 + 2는?', 'answer': '4'}]})
    wait(ce, run['run_id']); ce.evaluate(run['run_id'], {})
    result = wait(ce, run['run_id'])
    assert result['status'] == 'COMPLETED'
    assert len(calls) == 2  # no automatic paid retry
    batch = result['evaluations']['gpt']
    assert [c['case_status'] for c in batch['cases']] == ['EVALUATED', 'INPUT_ERROR']
    assert batch['summary']['totals']['input_errors'] == 1
    assert batch['cases'][0]['answer'] == '1 + 1 = 2'
    assert len(json.loads(ce.export(run['run_id'], 'gpt'))) == 2
    downloaded = evaluate_batch(json.loads(ce.export(run['run_id'], 'gpt')))
    assert downloaded['cases'][1]['case_status'] == 'INPUT_ERROR'
    assert result['evaluations']['aiona']['cases'][1]['answer'] == '4'


def test_cancel_preserves_inflight_answer_and_does_not_send_next_question(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def handle(req):
        calls.append(req); entered.set(); assert release.wait(3)
        return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': ' answer\n '}]})
    ce = service(tmp_path, handle); ce.configure('claude', {'key': 'secret', 'model': 'test'})
    run = ce.start({'providers': ['claude'], 'cases': [
        {'question': 'one', 'answer': 'one', 'conversation_id': 'dialogue', 'turn': 1},
        {'question': 'two', 'answer': 'two', 'conversation_id': 'dialogue', 'turn': 2}]})
    assert entered.wait(3); ce.cancel(run['run_id']); release.set()
    result = wait(ce, run['run_id'])
    assert result['status'] == 'CANCELLED'
    assert len(calls) == 1
    assert result['datasets']['claude'][0]['answer'] == ' answer\n '
    assert result['datasets']['claude'][1] is None
    downloaded = json.loads(ce.export(run['run_id'], 'claude'))
    assert len(downloaded) == 2
    assert downloaded[1]['question'] == 'two'
    assert evaluate_batch(downloaded)['cases'][1]['case_status'] == 'INPUT_ERROR'


def test_saved_history_survives_restart_and_interrupted_runs_are_reported(tmp_path):
    ce = service(tmp_path, lambda _: pytest.fail('no collection expected'))
    run = ce.start({'cases': [{'question': ' Q\n', 'answer': ' A\n'}]})
    replacement = service(tmp_path, lambda _: pytest.fail('no collection expected'))
    assert replacement.history()[0]['run_id'] == run['run_id']
    assert json.loads(replacement.export(run['run_id'], 'aiona'))[0]['answer'] == ' A\n'
    assert not replacement.providers()['gpt']['configured']
    path = tmp_path / 'compare' / (run['run_id'] + '.json')
    saved = json.loads(path.read_text(encoding='utf-8')); saved['status'] = 'COLLECTING'
    path.write_text(json.dumps(saved), encoding='utf-8')
    restarted = service(tmp_path, lambda _: pytest.fail('no request on restart'))
    assert restarted.history()[0]['status'] == 'ERROR'
    assert restarted.result(run['run_id'])['status'] == 'ERROR'


