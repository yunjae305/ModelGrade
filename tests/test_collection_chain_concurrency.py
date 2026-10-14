"""Independent collection chains overlap; dialogue history and paid-call count do not change."""
from collections import Counter, defaultdict
import copy
import hashlib
import json
from pathlib import Path
import threading
import time

import httpx
import pytest

from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService, normalize_dataset


MODELS = {'claude': 'claude-sonnet-5-5', 'gpt': 'gpt-6-luna', 'gemini': 'gemini-3.8-flash'}
DATA = Path(__file__).resolve().parents[1] / 'data' / 'code_eval' / 'AIONA_winz_단일28_멀티10-30_질문30_id_turns.json'


def until(check, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.005)
    raise AssertionError('Collection did not reach the expected state')


def decode(request):
    payload = json.loads(request.content)
    if request.url.host == 'api.anthropic.com':
        return 'claude', payload['messages']
    if request.url.host == 'api.openai.com':
        return 'gpt', payload['input']
    return 'gemini', [{'role': 'assistant' if item['role'] == 'model' else 'user',
                       'content': ''.join(part['text'] for part in item['parts'])} for item in payload['contents']]


def answer(provider, messages):
    digest = hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()[:16]
    return f'  {provider}: {digest}\r\n```json\r\n{{"literal":"\\n"}}\r\n``` 😀  '


def raw_response(provider, text):
    common = {'model': MODELS[provider], 'usage': {'input_tokens': 100, 'output_tokens': 50,
        'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
        'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
        'service_tier': 'standard', 'inference_geo': 'global'}}
    if provider == 'claude':
        return {**common, 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': text}]}
    if provider == 'gpt':
        return {**common, 'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]}
    return {'modelVersion': MODELS[provider], 'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 50},
            'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': text}]}}]}


def make_service(tmp_path, handler, workers=3):
    evaluator = CodeEvalService(save_dir=tmp_path / 'eval', jev_resolver=lambda: pytest.fail('collection must not call a judge'))
    service = ComparisonService(evaluator, save_dir=tmp_path / 'runs', collection_workers=workers,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    for provider, model in MODELS.items():
        service.configure(provider, {'key': 'fixture-only', 'model': model})
    return service


def finish(service, rid):
    until(lambda: service.status(rid)['status'] not in ('COLLECTING', 'EVALUATING'))
    until(lambda: not service._workers.get(rid))
    return service.result(rid)


def assert_history_and_text(result, captured):
    originals = result['datasets']['aiona']
    expected = []
    for provider in result['providers'][1:]:
        rows = result['datasets'][provider]
        for index, original in enumerate(originals):
            row = rows[index]
            assert row['case_index'] == index and row['case_id'] == original['case_id']
            assert row['question'] == original['question']
            prior = sorted([j for j, source in enumerate(originals)
                if original.get('conversation_id') is not None and source.get('conversation_id') == original['conversation_id']
                and source['turn'] < original['turn']], key=lambda j: originals[j]['turn'])
            messages = []
            for j in prior:
                messages += [{'role': 'user', 'content': originals[j]['question']},
                             {'role': 'assistant', 'content': rows[j]['answer']}]
            messages.append({'role': 'user', 'content': original['question']})
            assert row['answer'] == answer(provider, messages)
            assert row['collection']['raw'] == raw_response(provider, row['answer'])
            assert row['collection']['latency_ms'] >= 0
            expected.append((provider, json.dumps(messages, ensure_ascii=False)))
    assert Counter(captured) == Counter(expected)


def test_actual_68_questions_keep_204_calls_original_text_indices_and_model_specific_history(tmp_path, capsys):
    if not DATA.is_file():
        pytest.skip('Optional private question dataset is not included in public checkouts')
    original_bytes = DATA.read_bytes()
    cases = normalize_dataset({'input_text': original_bytes.decode('utf-8')})
    originals = copy.deepcopy(cases)
    captured, active, peaks = [], defaultdict(int), defaultdict(int)
    lock = threading.Lock()
    first_wave = {provider: threading.Barrier(3) for provider in MODELS}
    first_counts = defaultdict(int)
    def handle(request):
        provider, messages = decode(request)
        with lock:
            captured.append((provider, json.dumps(messages, ensure_ascii=False)))
            active[provider] += 1
            peaks[provider] = max(peaks[provider], active[provider])
            first_counts[provider] += 1
            first = first_counts[provider] <= 3
        try:
            if first:
                first_wave[provider].wait(5)
            time.sleep(.001)
            return httpx.Response(200, json=raw_response(provider, answer(provider, messages)))
        finally:
            with lock:
                active[provider] -= 1
    service = make_service(tmp_path, handle)
    rid = service.start({'cases': cases, 'providers': list(MODELS)})['run_id']
    result = finish(service, rid)
    assert result['status'] == 'READY' and result['done'] == result['total'] == 204
    assert len(cases) == 68 and cases == originals and DATA.read_bytes() == original_bytes
    assert peaks == {provider: 3 for provider in MODELS}
    execution = result['collection_execution']   # 수집 재현성 기록(request, 7f0f49f)이 더해졌다 — 동시 실행 조건만 그대로인지 본다
    assert {key: execution[key] for key in ('collection_workers_per_model', 'collection_workers_total', 'conversation_order')} == {
        'collection_workers_per_model': 3, 'collection_workers_total': 9, 'conversation_order': 'sequential_original_turns'}
    assert execution['request']['history'] == 'own_previous_answers'
    assert all(p['success'] == 68 and p['done'] == 68 and p['active'] is None for p in result['progress'].values())
    assert_history_and_text(result, captured)
    saved = json.loads((tmp_path / 'runs' / f'{rid}.json').read_text(encoding='utf-8'))
    assert saved['datasets'] == result['datasets']
    assert not result['evaluations'] and not service._cost_ledgers
    output = capsys.readouterr().out
    assert '[LLM 비용]' not in output
    assert '미확인' not in output and 'fixture-only' not in output


def small_cases():
    # Reversed input positions ensure the scheduler uses turns, not array order.
    return [{'id': f'{cid}{turn}', 'question': f' {cid}{turn}\r\n', 'answer': f'AIONA {cid}{turn}',
             'conversation_id': cid, 'turn': turn} for cid in ('A', 'B') for turn in (3, 1, 2)] + [
             {'id': f'S{i}', 'question': f' S{i}\r\n', 'answer': f'AIONA S{i}'} for i in range(4)]


def test_failed_turn_breaks_only_its_own_chain_without_extra_request(tmp_path, capsys):
    captured = []
    def handle(request):
        provider, messages = decode(request)
        captured.append(messages)
        if messages[-1]['content'] == ' A2\r\n':
            return httpx.Response(429)
        return httpx.Response(200, json=raw_response(provider, answer(provider, messages)))
    service = make_service(tmp_path, handle)
    rid = service.start({'cases': small_cases(), 'providers': ['gpt']})['run_id']
    result = finish(service, rid)
    rows = result['datasets']['gpt']
    assert len(captured) == 9  # Failed A2 is charged/unknown; A3 is never requested.
    assert not any(m[-1]['content'] == ' A3\r\n' for m in captured)
    assert rows[0]['collection']['status'] == rows[2]['collection']['status'] == 'ERROR'
    assert 'latency_ms' not in rows[0]['collection'] and rows[2]['collection']['latency_ms'] >= 0
    assert all(row['collection']['status'] == 'SUCCESS' for row in rows[3:])
    assert result['progress']['gpt']['done'] == 10 and result['progress']['gpt']['failed'] == 2
    assert '[LLM 비용]' not in capsys.readouterr().out


@pytest.mark.parametrize('shutdown', [False, True])
def test_parallel_cancellation_preserves_all_inflight_answers_and_starts_no_more_chains(tmp_path, capsys, shutdown):
    all_entered, release = threading.Event(), threading.Event()
    captured, lock = [], threading.Lock()
    def handle(request):
        provider, messages = decode(request)
        with lock:
            captured.append(messages)
            if len(captured) == 3:
                all_entered.set()
        assert release.wait(5)
        return httpx.Response(200, json=raw_response(provider, answer(provider, messages)))
    service = make_service(tmp_path, handle)
    rid = service.start({'cases': small_cases(), 'providers': ['claude'], 'auto_evaluate': True})['run_id']
    try:
        assert all_entered.wait(3)
        active = service.status(rid)['progress']['claude']['active']
        assert type(active) is int  # Existing UI still receives one representative index.
        if shutdown:
            assert service.shutdown(timeout=0) is False
        else:
            service.cancel(rid)
        release.set()
        result = finish(service, rid)
        assert result['status'] == 'CANCELLED' and len(captured) == 3
        rows = result['datasets']['claude']
        assert [i for i, row in enumerate(rows) if row] == [1, 4, 6]
        for row in filter(None, rows):
            assert row['answer'] == answer('claude', [{'role': 'user', 'content': row['question']}])
        assert not result['evaluations'] and result['done'] == 3 and not service._cost_ledgers
        assert result['progress']['claude']['active'] is None
        assert '[LLM 비용]' not in capsys.readouterr().out
    finally:
        release.set()


@pytest.mark.parametrize('value', [0, 5, -1, True, 2.5, '3'])
def test_collection_worker_setting_rejects_invalid_values_before_starting(tmp_path, value):
    with pytest.raises(ValueError, match='COMPARISON_COLLECTION_WORKERS'):
        make_service(tmp_path, lambda _: pytest.fail('no request'), workers=value)


def test_collection_worker_env_validates_before_configuring_providers(tmp_path):
    service = ComparisonService(CodeEvalService(save_dir=tmp_path / 'eval'), save_dir=tmp_path / 'runs')
    assert service.collection_workers == 3
    missing = tmp_path / 'absent.env'
    for value in ('0', '5', '-1', '1.5', 'not-a-number'):
        with pytest.raises(ValueError, match='COMPARISON_COLLECTION_WORKERS'):
            service.load_environment(environment={'COMPARISON_COLLECTION_WORKERS': value,
                'OPENAI_API_KEY': 'fixture', 'OPENAI_MODEL': MODELS['gpt']}, env_path=missing)
        assert not service.providers()['gpt']['configured'] and service.collection_workers == 3
    service.load_environment(environment={'COMPARISON_COLLECTION_WORKERS': '1'}, env_path=missing)
    assert service.collection_workers == 1


def test_one_dialogue_never_runs_its_turns_in_parallel(tmp_path):
    active, peak, lock = [0], [0], threading.Lock()
    def handle(request):
        provider, messages = decode(request)
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            time.sleep(.01)
            return httpx.Response(200, json=raw_response(provider, answer(provider, messages)))
        finally:
            with lock:
                active[0] -= 1
    service = make_service(tmp_path, handle, workers=4)
    rid = service.start({'cases': small_cases()[:3], 'providers': ['claude']})['run_id']
    result = finish(service, rid)
    assert result['status'] == 'READY' and peak[0] == 1
    assert result['collection_execution']['collection_workers_per_model'] == 1
