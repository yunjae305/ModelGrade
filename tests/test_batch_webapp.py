"""Message Batches 호출 방식의 webapp 연결 — 실행 조건 기록·묶음 ID 저장·진행 이벤트·중단 뒤 이어받기. Batches API 는 tests.test_jev_batch.FakeBatches 대역."""
import copy
import json

import httpx
import pytest

from code_eval.anthropic_semantic_client import AnthropicSemanticClient, AnthropicSemanticConfig
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from tests.test_blind_comparison import until
from tests.test_jev_batch import FakeBatches
from tests.test_jev_eval import NORMAL, MISSING


def batch_judge(fake, **overrides):
    settings = {"model": "claude-opus-5-5", "reasoning_effort": "high", "web_search": "disabled", "dispatch": "batch",
                "batch_questions": 2, "batch_poll_seconds": 1, **overrides}
    config = AnthropicSemanticConfig(**settings)
    return AnthropicSemanticClient(config, "secret-test-key", transport=httpx.MockTransport(fake.handler),
                                   source_fetcher=lambda url: {"status": "fetch_error"}), config


def make_service(tmp_path, judge, config, handler, *, fake):
    evaluator = CodeEvalService(save_dir=tmp_path / 'eval', jev_resolver=lambda: (judge, config, None))
    service = ComparisonService(evaluator, save_dir=tmp_path / 'runs',
                                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    service.configure('gpt', {'key': 'PRIVATE_KEY_gpt', 'model': 'PRIVATE_PROVIDER_MODEL_gpt'})
    return service


def gpt_handler(request):
    return httpx.Response(200, json={'status': 'completed', 'output': [
        {'type': 'message', 'content': [{'type': 'output_text', 'text': '- 레노버 아이디어패드 슬림 5: 가볍습니다.\n- 에이수스 비보북 14: 저렴합니다.\n- 맥북 에어: 오래 씁니다.'}]}]})


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    # 디스패처의 폴링 대기(time.sleep)를 없앤다 — 묶음 대역은 조회 즉시 끝난다
    from code_eval import jev_batch
    monkeypatch.setattr(jev_batch.time, "sleep", lambda s: None)


def test_batch_dispatch_run_records_batches_progress_and_results(tmp_path):
    fake = FakeBatches()
    judge, config = batch_judge(fake)
    service = make_service(tmp_path, judge, config, gpt_handler, fake=fake)
    rid = service.start({'cases': [{'question': NORMAL['question'], 'answer': NORMAL['answer']},
                                   {'question': MISSING['question'], 'answer': MISSING['answer']}],
                         'providers': ['gpt'], 'auto_evaluate': True})['run_id']
    until(lambda: service.status(rid)['status'] == 'COMPLETED', timeout=30)
    final = service.result(rid)
    execution = final['evaluation_execution']
    assert execution['dispatch'] == 'batch' and execution['pipeline'] == 'legacy' and execution['resumed'] is False
    assert set(execution['batches']) == {'aiona', 'gpt'}
    for provider in ('aiona', 'gpt'):
        saved = execution['batches'][provider]
        assert [sorted(b['custom_ids']) for b in saved] == [[f'{rid}:{provider}:0'], [f'{rid}:{provider}:1']]   # pilot 1 + 묶음 1
        assert all(b['status'] == 'ended' and b['id'].startswith('msgbatch_') for b in saved)
        jev = final['evaluations'][provider]['jev_eval']
        assert jev['dispatch'] == 'batch' and [r['status'] for r in jev['records']] == ['SUCCESS', 'SUCCESS']
        assert jev['records'][0]['batch']['id'] == saved[0]['id'] and jev['records'][0]['batch']['result'] == 'succeeded'
        progress = final['evaluation_progress'][provider]
        assert progress['stage'] == 'COMPLETED' and progress['batch']['done'] == 2 and progress['batch']['submitted'] == 2
        assert progress['batch']['batches'][0]['status'] == 'ended'
    assert not fake.realtime
    assert final['evaluation_profile']['fields']['judge']['dispatch'] == 'batch'
    events = service.event_stream(rid).read(after=0)
    batch_events = [e for e in events if e['type'] == 'batch_progress']
    assert batch_events and batch_events[-1]['done'] == 2 and 'params' not in json.dumps(batch_events)
    assert {e['model'] for e in batch_events} == {'AIONA', 'GPT'}
    saved_run = json.loads((tmp_path / 'runs' / f'{rid}.json').read_text(encoding='utf-8'))
    assert saved_run['evaluation_execution']['batches']['gpt'][0]['id'] == execution['batches']['gpt'][0]['id']


def test_resume_reuses_saved_batches_without_resubmitting(tmp_path):
    fake = FakeBatches()
    judge, config = batch_judge(fake)
    service = make_service(tmp_path, judge, config, gpt_handler, fake=fake)
    rid = service.start({'cases': [{'question': NORMAL['question'], 'answer': NORMAL['answer']},
                                   {'question': MISSING['question'], 'answer': MISSING['answer']}],
                         'providers': ['gpt'], 'auto_evaluate': True})['run_id']
    until(lambda: service.status(rid)['status'] == 'COMPLETED', timeout=30)
    saved = copy.deepcopy(service.result(rid)['evaluation_execution']['batches'])
    # 서버 재시작으로 중단된 실행처럼 만든다: 상태 ERROR, 묶음 기록은 남아 있음
    with service._lock:
        run = service._run(rid)
        run['status'], run['error'] = 'ERROR', '서버 재시작으로 실행이 중단되었습니다.'
        run['evaluations'] = {}
        service._save(run)
    creates_before = sum(1 for method, path in fake.calls if method == 'POST' and path.endswith('/messages/batches'))
    service.evaluate(rid, {'resume': True})
    until(lambda: service.status(rid)['status'] == 'COMPLETED', timeout=30)
    final = service.result(rid)
    creates_after = sum(1 for method, path in fake.calls if method == 'POST' and path.endswith('/messages/batches'))
    assert creates_after == creates_before   # 저장된 묶음은 다시 제출하지 않는다
    assert final['evaluation_execution']['resumed'] is True
    for provider in ('aiona', 'gpt'):
        batches = final['evaluation_execution']['batches'][provider]
        assert [b['id'] for b in batches] == [b['id'] for b in saved[provider]] and all(b.get('resumed') for b in batches)
        assert [r['status'] for r in final['evaluations'][provider]['jev_eval']['records']] == ['SUCCESS', 'SUCCESS']
        assert final['evaluations'][provider]['jev_eval']['records'][1]['batch']['id'] == saved[provider][1]['id']
    assert not fake.realtime
    # 이어받기 없이 다시 평가하면 새로 제출한다
    with service._lock:
        run = service._run(rid)
        run['status'] = 'CANCELLED'
        service._save(run)
    service.evaluate(rid, {})
    until(lambda: service.status(rid)['status'] == 'COMPLETED', timeout=30)
    assert sum(1 for method, path in fake.calls if method == 'POST' and path.endswith('/messages/batches')) == creates_after + 4
    assert service.result(rid)['evaluation_execution']['resumed'] is False


def test_realtime_judge_keeps_realtime_dispatch(tmp_path):
    fake = FakeBatches()
    judge, config = batch_judge(fake, dispatch='realtime')
    service = make_service(tmp_path, judge, config, gpt_handler, fake=fake)
    rid = service.start({'cases': [{'question': NORMAL['question'], 'answer': NORMAL['answer']}], 'providers': ['gpt'], 'auto_evaluate': True})['run_id']
    until(lambda: service.status(rid)['status'] == 'COMPLETED', timeout=30)
    final = service.result(rid)
    assert final['evaluation_execution']['dispatch'] == 'realtime' and final['evaluation_execution']['batches'] == {}
    assert len(fake.realtime) == 2 and 'batch' not in final['evaluations']['gpt']['jev_eval']['records'][0]
    assert final['evaluations']['gpt']['jev_eval']['dispatch'] == 'realtime'
