"""Provider durations must stop independently and survive completed/interrupted reloads."""
import json
import threading

import httpx
import pytest

from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from test_live_comparison_progress import until


def test_provider_timing_finishes_independently_and_is_saved(tmp_path):
    entered = {p: threading.Event() for p in ('gpt', 'claude')}
    release = {p: threading.Event() for p in entered}
    def handle(request):
        p = 'gpt' if request.url.host == 'api.openai.com' else 'claude'
        entered[p].set()
        assert release[p].wait(5)
        if p == 'gpt':
            return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'answer'}]}]})
        return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'answer'}]})
    evaluator = CodeEvalService(save_dir=tmp_path/'eval')
    service = ComparisonService(evaluator, save_dir=tmp_path/'runs', client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    for provider in entered:
        service.configure(provider, {'key': 'fixture', 'model': 'fixture'})
    rid = service.start({'cases': [{'question': 'q', 'answer': 'a'}], 'providers': list(entered)})['run_id']
    try:
        assert all(event.wait(3) for event in entered.values())
        live = service.status(rid)['progress']
        assert all(v['started_at'] and v['duration_ms'] is None for v in live.values())
        release['gpt'].set()
        until(lambda: service.status(rid)['progress']['gpt']['finished_at'] is not None)
        early = service.status(rid)
        assert early['status'] == 'COLLECTING'
        assert early['progress']['claude']['finished_at'] is None
        finished = early['progress']['gpt']
        assert finished['duration_ms'] >= 0 and finished['started_at'] <= finished['finished_at']
        snapshot = json.loads((tmp_path/'runs'/f'{rid}.json').read_text(encoding='utf-8'))
        assert snapshot['progress']['gpt'] == finished
        # Interrupted runs freeze the last saved elapsed time, without inventing an end time.
        snapshot['progress']['claude']['elapsed_ms'] = 1234
        ComparisonService._interrupted(snapshot)
        assert snapshot['progress']['claude']['timing_interrupted']
        assert snapshot['progress']['claude']['elapsed_ms'] == 1234
        assert snapshot['progress']['claude']['duration_ms'] is None
        assert snapshot['progress']['gpt'] == finished
    finally:
        for event in release.values():
            event.set()
    until(lambda: service.status(rid)['status'] == 'READY')
    # Wait for the worker's final persistence before reconstructing a new service.
    until(lambda: json.loads((tmp_path/'runs'/f'{rid}.json').read_text(encoding='utf-8'))['status'] == 'READY')
    recovered = ComparisonService(evaluator, save_dir=tmp_path/'runs').status(rid)
    assert recovered['progress']['gpt'] == finished
    assert recovered['progress']['claude']['duration_ms'] >= 0
    assert 'aiona' not in recovered['progress']


@pytest.mark.parametrize('cancel', [False, True])
def test_collection_failure_and_cancel_stop_timer(tmp_path, cancel):
    entered, release = threading.Event(), threading.Event()
    def handle(request):
        entered.set()
        assert release.wait(5)
        if not cancel:
            raise RuntimeError('unexpected fixture transport failure')
        return httpx.Response(200, json={'status': 'completed', 'output': []})
    service = ComparisonService(CodeEvalService(save_dir=tmp_path/'eval'), save_dir=tmp_path/'runs',
                                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    service.configure('gpt', {'key': 'fixture', 'model': 'fixture'})
    rid = service.start({'cases': [{'question': 'q', 'answer': 'a'}]*2, 'providers': ['gpt']})['run_id']
    try:
        assert entered.wait(3)
        if cancel:
            service.cancel(rid)
    finally:
        release.set()
    until(lambda: service.status(rid)['status'] == ('CANCELLED' if cancel else 'ERROR'))
    detail = service.status(rid)['progress']['gpt']
    assert detail['finished_at'] and detail['duration_ms'] >= 0
    assert detail['active'] is None


def test_every_collected_row_records_request_times_and_the_run_records_request_conditions(tmp_path):
    """지시서 10-13 D — 일반 비교 실행도 행마다 요청·수신 시각, 실행마다 호출 방식과 실제로 보낸 설정을 남긴다."""
    def handle(request):
        if request.url.host == 'api.openai.com':
            return httpx.Response(200, json={'status': 'completed', 'model': 'gpt-x-2026', 'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': 'answer'}]}]})
        return httpx.Response(529, json={'error': {'message': 'overloaded'}})
    service = ComparisonService(CodeEvalService(save_dir=tmp_path/'eval'), save_dir=tmp_path/'runs',
                                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    for provider in ('gpt', 'claude'):
        service.configure(provider, {'key': 'fixture', 'model': 'fixture-' + provider})
    rid = service.start({'cases': [{'question': 'q', 'answer': 'a'}], 'providers': ['gpt', 'claude'], 'auto_evaluate': False})['run_id']
    until(lambda: service.status(rid)['status'] in ('READY', 'ERROR'))
    saved = json.loads((tmp_path/'runs'/f'{rid}.json').read_text(encoding='utf-8'))
    gpt, claude = saved['datasets']['gpt'][0]['collection'], saved['datasets']['claude'][0]['collection']
    assert gpt['status'] == 'SUCCESS' and gpt['requested_at'] <= gpt['collected_at']
    assert claude['status'] == 'ERROR' and claude['requested_at'] and claude['collected_at']   # 실패한 수집도 시각을 남긴다
    request = saved['collection_execution']['request']
    assert request['access_mode'] == 'api' and request['parameters'] == {'max_output_tokens': saved['max_tokens']}
    assert (request['system_prompt'], request['tools'], request['sampling']) == (None, [], 'provider_default')


def test_conversation_rows_record_what_was_sent_without_the_text(tmp_path):
    """지시서 10-14 P0-2 — 모델별 입력 차이를 원문 없이 확인하도록 요청 구성(메시지 수·역할·글자 수·해시)을 행에 남긴다."""
    import hashlib
    answers = {'api.openai.com': '짧은 답', 'api.anthropic.com': '조금 더 긴 답변입니다'}
    def handle(request):
        text = answers[request.url.host]
        if request.url.host == 'api.openai.com':
            return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]})
        return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': text}]})
    service = ComparisonService(CodeEvalService(save_dir=tmp_path/'eval'), save_dir=tmp_path/'runs',
                                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    for provider in ('gpt', 'claude'):
        service.configure(provider, {'key': 'fixture', 'model': 'fixture-' + provider})
    cases = [{'conversation_id': 'C', 'turn': t, 'question': f'{t}번째 질문', 'answer': 'a'} for t in (1, 2)]
    rid = service.start({'cases': cases, 'providers': ['gpt', 'claude'], 'auto_evaluate': False})['run_id']
    until(lambda: service.status(rid)['status'] in ('READY', 'ERROR'))
    saved = json.loads((tmp_path/'runs'/f'{rid}.json').read_text(encoding='utf-8'))
    for provider, answer in (('gpt', '짧은 답'), ('claude', '조금 더 긴 답변입니다')):
        first, second = (row['collection']['request'] for row in saved['datasets'][provider])
        assert first == {'attachments_in_question': 0, 'attachments_sent': 0, 'messages': 1, 'user': 1, 'assistant': 0,
                         'chars': len('1번째 질문'), 'own_answer_chars': 0, 'sha256': first['sha256']}
        assert (second['messages'], second['user'], second['assistant'], second['own_answer_chars']) == (3, 2, 1, len(answer))
        sent = [{'role': 'user', 'content': '1번째 질문'}, {'role': 'assistant', 'content': answer}, {'role': 'user', 'content': '2번째 질문'}]
        assert second['sha256'] == hashlib.sha256(json.dumps(sent, ensure_ascii=False).encode('utf-8')).hexdigest()[:12]
        assert '1번째 질문' not in json.dumps(second, ensure_ascii=False)   # 원문은 남기지 않는다


def test_attachment_questions_record_that_api_models_did_not_receive_the_file(tmp_path):
    """3차 재현 — 첨부 질문(J8)에서 API 모델 요청에는 첨부가 가지 않는데 그 답변에도 J8 을 묻는다. 판정은 바꾸지 않고 사실을 행에 남긴다."""
    from tests.test_blind_comparison import CapturingJudge
    bodies = []
    def handle(request):
        bodies.append(request.content.decode('utf-8'))
        return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '합계는 모릅니다.'}]}]})
    judge = CapturingJudge()
    service = ComparisonService(CodeEvalService(save_dir=tmp_path/'eval', jev_resolver=lambda **_: (judge, judge.config, None)),
                                save_dir=tmp_path/'runs', client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    service.configure('gpt', {'key': 'fixture', 'model': 'fixture-gpt'})
    attachment = {'name': '견적서.txt', 'type': 'txt', 'content': '견적 합계 1,250,000원'}
    case = {'question': '첨부한 견적서의 합계를 알려줘.', 'answer': '합계는 1,250,000원입니다.', 'attachments': [attachment]}
    rid = service.start({'cases': [case], 'providers': ['gpt'], 'auto_evaluate': True})['run_id']
    until(lambda: service.status(rid)['status'] in ('COMPLETED', 'ERROR'))
    saved = json.loads((tmp_path/'runs'/f'{rid}.json').read_text(encoding='utf-8'))
    assert bodies and all('1,250,000' not in body and '견적서.txt' not in body for body in bodies)   # API 요청에는 첨부가 없다
    request = saved['datasets']['gpt'][0]['collection']['request']
    assert (request['attachments_in_question'], request['attachments_sent']) == (1, 0)
    asked = [call for call in judge.calls if call['state']['answer'] == '합계는 모릅니다.']
    assert asked and 'J8' in asked[0]['questions']                                                   # 그런데 J8 을 묻는다
