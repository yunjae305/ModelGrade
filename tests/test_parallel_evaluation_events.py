"""Parallel provider workers and real SSE events, with no external API calls."""
import copy
import json
import threading
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest

from code_eval.engine import evaluate_batch
from code_eval.jev_client import JevCall, JevClientConfig
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.evaluation_events import EvaluationEvents
from webapp.server import Server
from test_live_comparison_progress import until


class ConcurrentJudge:
    config = JevClientConfig()

    def __init__(self):
        self.barrier = threading.Barrier(4, timeout=8)
        self.seen = set()
        self.calls = []
        self.lock = threading.Lock()

    def system_one(self, state, questions):
        model = state['answer'].split(':')[0]
        with self.lock:
            first = model not in self.seen
            self.seen.add(model)
            self.calls.append(copy.deepcopy(state))
        if first:
            self.barrier.wait()  # would deadlock with serial model evaluation
        return JevCall('OK', body={'model': 'fixture-only', 'answers': {
            cid: {'choice': 'PASS', 'confidence': .9, 'probabilities': {'PASS': .95, 'FAIL': .05},
                  'reason': '동시 실행 연결 검증용 대역 판정'} for cid in questions}})


def test_all_providers_collect_and_all_four_models_evaluate_concurrently(tmp_path):
    collection_barrier = threading.Barrier(3, timeout=8)
    calls = {p: [] for p in ('gpt', 'claude', 'gemini')}
    judge = ConcurrentJudge()
    resolution_count = []
    def resolver():
        resolution_count.append(1)
        return judge, judge.config, None
    def handle(request):
        provider = {'api.openai.com': 'gpt', 'api.anthropic.com': 'claude', 'generativelanguage.googleapis.com': 'gemini'}[request.url.host]
        body = json.loads(request.content)
        if provider == 'gemini':
            messages = [{'role': 'assistant' if c['role'] == 'model' else 'user', 'content': ''.join(p['text'] for p in c['parts'])} for c in body['contents']]
        else:
            messages = body.get('input') or body['messages']
        calls[provider].append(messages)
        if len(calls[provider]) == 1:
            collection_barrier.wait()  # proves overlap before any response is returned
        assert not judge.calls  # full collection barrier before blind evaluation
        answer = provider + ':' + messages[-1]['content']
        if provider == 'gpt':
            return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': answer}]}]})
        if provider == 'claude':
            return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': answer}]})
        return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': answer}]}}]})
    evaluator = CodeEvalService(save_dir=tmp_path/'eval', jev_resolver=resolver)
    service = ComparisonService(evaluator, save_dir=tmp_path/'runs', client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    for provider in calls:
        service.configure(provider, {'key': 'fixture-key', 'model': 'secret-model-' + provider})
    cases = [{'case_id': 'same', 'question': f'question-{t}', 'answer': f'aiona:answer-{t}', 'conversation_id': 'conversation', 'turn': t} for t in (1, 2, 3)]
    rid = service.start_comparison({'cases': cases})['run_id']
    until(lambda: service.status(rid)['status'] == 'COMPLETED', timeout=15)
    final = service.result(rid)
    assert judge.seen == {'aiona', 'gpt', 'claude', 'gemini'}
    assert len(resolution_count) == 1
    assert len(judge.calls) == 12
    for provider, requests in calls.items():
        assert [m[-1]['content'] for m in requests] == ['question-1', 'question-2', 'question-3']
        assert requests[2] == [{'role': 'user', 'content': 'question-1'}, {'role': 'assistant', 'content': provider + ':question-1'},
                               {'role': 'user', 'content': 'question-2'}, {'role': 'assistant', 'content': provider + ':question-2'},
                               {'role': 'user', 'content': 'question-3'}]
    log = final['evaluation_log']
    assert log['done'] == log['total'] == 12 and len(log['rows']) == 12
    assert log['unjudged'] == 0 and log['status'] == 'COMPLETED'
    assert {r['model'] for r in log['rows'].values()} == {'AIONA', 'GPT', 'Claude', 'Gemini'}   # 화면용 기록은 실제 모델명 (블라인드는 평가 요청에만)
    assert 'secret-model' not in json.dumps(log) and 'fixture-key' not in json.dumps(log)
    assert 'evaluation_log' not in service.status(rid)
    assert final['collection_completed_at'] <= final['blind']['applied_at']


def test_events_leave_code_digests_unchanged_and_no_supplemental_stage(tmp_path):
    from semantic_judge_fixture import Judge, sample
    judge, events = Judge(), []
    evaluator = CodeEvalService(save_dir=tmp_path, jev_resolver=lambda: (judge, judge.config, None))
    inputs = sample(9, 41, 91)
    batch = evaluator.evaluate({'cases': inputs}, event_sink=events.append)
    assert batch['result_digest'] == evaluate_batch(inputs, evaluator._config({}))['result_digest']
    hub = EvaluationEvents()
    hub.publish({'type': 'run_start', 'total': 3})
    from webapp.evaluation_events import code_event
    for e in events:
        if e['type'] == 'code_result':
            e = code_event(e['row'], e['index'], e['qid'])
        hub.publish({**e, 'model': '모델 A'})
    hub.publish({'type': 'run_done'})
    state = hub.snapshot()
    assert state['done'] == 3 and state['fail'] == 0 and state['unjudged'] == 0
    assert all(r['status'] == 'DONE' for r in state['rows'].values())
    # 새 엔진은 counted(PASS/FAIL) 행만 code_result 로 보내고 보완 단계·code_completion 을 만들지 않는다.
    assert not any(e['type'] == 'criterion_start' and e['stage'] == 'LLM 보완' for e in events)
    assert all(e['row']['counted'] for e in events if e['type'] == 'code_result')
    assert all('code_completion' not in e['record'] for e in events if 'record' in e)
    assert all('code_completion' not in r for r in batch['jev_eval']['records'])
    assert sum(r['pass'] + r['fail'] + r['unjudged'] for r in state['rows'].values()) == state['pass'] + state['fail']


def test_snapshot_replay_overflow_duplicates_retry_and_error():
    hub = EvaluationEvents(capacity=3)
    hub.publish({'type': 'run_start', 'total': 2})
    for i in range(2):
        base = {'model': '모델 A', 'index': i, 'qid': 'duplicate'}
        hub.publish({**base, 'type': 'q_start'})
        hub.publish({**base, 'type': 'criterion_result', 'key': 'J1', 'code': 'J1', 'name': '의도', 'result': 'PASS', 'reason': 'fixture', 'stage': 'LLM'})
        hub.publish({**base, 'type': 'q_done'})
    hub.publish({**base, 'type': 'q_done'})  # idempotent done counter
    assert hub.snapshot()['done'] == 2
    assert hub.read(0)[0]['type'] == 'snapshot'
    assert len(hub.read(hub.sequence - 1)) == 1
    copy_state = hub.snapshot(); copy_state['rows'].clear()
    assert len(hub.snapshot()['rows']) == 2
    before = hub.sequence
    hub.publish({'type': 'run_start', 'total': 1})
    hub.publish({**base, 'type': 'q_start'})
    hub.publish({'type': 'run_error', 'reason': 'fixture stop'})
    assert hub.snapshot()['done'] == 0
    assert list(hub.snapshot()['rows'].values())[0]['status'] == 'INTERRUPTED'
    assert hub.read(before)[0]['type'] == 'run_start'
    frames = list(hub.frames())
    assert len(frames) == 1 and b'"type": "snapshot"' in frames[0]


def test_real_sse_endpoint_replays_saved_final_snapshot(tmp_path):
    from semantic_judge_fixture import Judge
    judge = Judge()
    evaluator = CodeEvalService(save_dir=tmp_path/'eval', jev_resolver=lambda: (judge, judge.config, None))
    service = ComparisonService(evaluator, save_dir=tmp_path/'runs')
    rid = service.start({'cases': [{'question': 'hello', 'answer': 'world'}]})['run_id']
    service.evaluate(rid, {})
    until(lambda: service.status(rid)['status'] == 'COMPLETED')
    # New service process reconstructs the log without making a judge call.
    recovered = ComparisonService(evaluator, save_dir=tmp_path/'runs')
    server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=recovered)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f'http://127.0.0.1:{server.server_port}/api/comparison/runs/{rid}/events'
        response = httpx.get(url, timeout=5)
        assert response.status_code == 200 and response.headers['content-type'].startswith('text/event-stream')
        event = json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))
        assert event['type'] == 'snapshot' and event['done'] == 1 and event['status'] == 'COMPLETED'
        assert httpx.get(url, headers={'Last-Event-ID': 'bad'}).status_code == 400
        assert httpx.get(url, headers={'Origin': 'https://untrusted.example'}).status_code == 403
    finally:
        server.shutdown(); server.server_close()


def test_browser_event_state_and_display_queue_are_independent():
    root = Path(__file__).resolve().parent.parent
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the production UI state check')
    result = subprocess.run([node, str(root/'tests/evaluation_stream_test.js')], cwd=root,
                            capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
