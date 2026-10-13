"""Real evaluation workers expose partial results without changing final verdicts."""
import copy
import json
import threading
import time

from code_eval.engine import evaluate_batch
from code_eval.jev_client import JevCall, JevClientConfig, resolve_jev_client
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService


def until(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('expected live state did not arrive')


class StagedJudge:
    config = JevClientConfig(max_attempts=1)

    def __init__(self):
        self.entered = {q: threading.Event() for q in ('first', 'second', 'third')}
        self.release = {q: threading.Event() for q in self.entered}

    def system_one(self, state, questions):
        q = state['question']
        self.entered[q].set()
        assert self.release[q].wait(30)
        if q == 'third':
            return JevCall('API_ERROR', error='test quota error')
        fail = q == 'second'
        return JevCall('OK', body={'model': 'live-test', 'answers': {
            cid: {'choice': 'FAIL' if fail else 'PASS',
                  'probabilities': {'PASS': .1 if fail else .9, 'FAIL': .9 if fail else .1},
                  # FAIL reasons quote the evaluated question, as the judge contract requires.
                  'confidence': .8, 'reason': f"live test reason: '{q}'"} for cid in questions}})


def live_service(tmp_path, judge):
    evaluator = CodeEvalService(save_dir=tmp_path / 'eval', jev_resolver=lambda: (judge, judge.config, None))
    return ComparisonService(evaluator, save_dir=tmp_path / 'compare')


def test_live_results_arrive_out_of_order_without_collapsing_duplicate_ids(tmp_path):
    judge = StagedJudge()
    service = live_service(tmp_path, judge)
    cases = [{'id': 'same', 'question': q, 'answer': '  original\r\n'} for q in judge.entered]
    rid = service.start({'cases': cases})['run_id']
    try:
        service.evaluate(rid, {})
        assert all(event.wait(3) for event in judge.entered.values())
        initial = service.status(rid)
        detail = initial['evaluation_progress']['aiona']
        assert detail['code_done'] == 3
        assert detail['semantic_done'] == 0
        assert sorted(detail['active']) == [0, 1, 2]
        judge.release['second'].set()
        until(lambda: service.status(rid)['evaluation_progress']['aiona']['semantic_done'] == 1)
        partial = service.result(rid)
        assert partial['status'] == 'EVALUATING'
        assert partial['revision'] > initial['revision']
        batch = partial['evaluations']['aiona']
        assert batch['live'] is True
        assert batch['jev_eval']['history_policy'] == 'full_original_text'
        assert batch['jev_eval']['records'][1]['decisions']['J1']['decision'] == 'FAIL'
        assert batch['jev_eval']['records'][0]['status'] == 'RUNNING'
        cfg = service.evaluator._config({'aiona_specialized': False})
        assert batch['result_digest'] == evaluate_batch(partial['datasets']['aiona'], cfg)['result_digest']
        partial['evaluations']['aiona']['cases'][0]['answer'] = 'mutated copy'
        assert service.result(rid)['evaluations']['aiona']['cases'][0]['answer'] == cases[0]['answer']
        judge.release['third'].set()
        until(lambda: service.status(rid)['evaluation_progress']['aiona']['semantic_errors'] == 1)
        assert service.status(rid)['evaluation_progress']['aiona']['semantic_done'] == 2
        judge.release['first'].set()
        until(lambda: service.status(rid)['status'] == 'COMPLETED')
        final = service.result(rid)
        assert not final['evaluations']['aiona'].get('live')
        assert [r['status'] for r in final['evaluations']['aiona']['jev_eval']['records']] == ['SUCCESS', 'SUCCESS', 'API_ERROR']
        assert final['evaluation_progress']['aiona']['semantic_success'] == 2
        assert final['evaluation_progress']['aiona']['semantic_done'] == 3
        assert final['evaluation_progress']['aiona']['active'] == []
        assert [c['answer'] for c in final['evaluations']['aiona']['cases']] == [c['answer'] for c in cases]
    finally:
        for event in judge.release.values():
            event.set()
        until(lambda: service.status(rid)['status'] != 'EVALUATING')


def test_observer_receives_input_exclusion_and_offline_records_at_original_indices(tmp_path):
    events = []
    evaluator = CodeEvalService(save_dir=tmp_path / 'eval', jev_resolver=lambda: resolve_jev_client(environment={}, env_path=tmp_path / '.no-env'))
    cases = [{'question': 'first', 'answer': ' A\n'}, {'case_id': 'bad', '__parse_error__': 'collection failed'},
             {'question': 'second', 'answer': ' B\r\n'}]
    batch = evaluator.evaluate({'cases': cases}, observer=lambda e: events.append(copy.deepcopy(e)))
    records = {e['index']: e['record'] for e in events if 'record' in e}
    assert records.keys() == {0, 1, 2}
    assert [records[i]['status'] for i in range(3)] == ['NOT_CONFIGURED', 'SKIPPED', 'NOT_CONFIGURED']
    assert records[0] == batch['jev_eval']['records'][0]
    assert [e['done'] for e in events if e['phase'] == 'CODE'] == [0, 1, 2, 3]
    assert events[-1]['phase'] == 'SAVE'
    assert batch['result_digest'] == evaluate_batch(cases, evaluator._config({}))['result_digest']


def test_restart_retains_completed_partial_records_and_clears_inflight_markers(tmp_path):
    judge = StagedJudge()
    service = live_service(tmp_path, judge)
    rid = service.start({'cases': [{'question': q, 'answer': q} for q in judge.entered]})['run_id']
    try:
        service.evaluate(rid, {})
        assert all(event.wait(3) for event in judge.entered.values())
        judge.release['first'].set()
        until(lambda: service.status(rid)['evaluation_progress']['aiona']['semantic_done'] == 1)
        (tmp_path / 'compare' / f'{rid}.json').write_text(json.dumps(service.result(rid)), encoding='utf-8')
        recovered = ComparisonService(service.evaluator, save_dir=tmp_path / 'compare').result(rid)
        assert recovered['status'] == 'ERROR'
        assert recovered['evaluation_progress']['aiona']['stage'] == 'ERROR'
        assert recovered['evaluation_progress']['aiona']['active'] == []
        assert recovered['evaluations']['aiona']['jev_eval']['records'][0]['status'] == 'SUCCESS'
        assert recovered['evaluations']['aiona']['jev_eval']['records'][1:] == [None, None]
    finally:
        for event in judge.release.values():
            event.set()
        until(lambda: service.status(rid)['status'] != 'EVALUATING')


def test_fatal_worker_error_preserves_code_results_without_false_inflight_state(tmp_path):
    class FailingEvaluator(CodeEvalService):
        def evaluate(self, payload, observer=None, **kwargs):
            batch = evaluate_batch(payload['cases'])
            observer({'phase': 'CODE', 'done': 1, 'total': 1})
            observer({'phase': 'JEV', 'batch': batch, 'criteria': []})
            observer({'phase': 'JEV', 'index': 0, 'started': True})
            raise RuntimeError('test worker failure')
    service = ComparisonService(FailingEvaluator(save_dir=tmp_path / 'eval'), save_dir=tmp_path / 'compare')
    rid = service.start({'cases': [{'question': 'first', 'answer': 'original'}]})['run_id']
    service.evaluate(rid, {})
    until(lambda: service.status(rid)['status'] == 'ERROR')
    result = service.result(rid)
    assert result['evaluation_progress']['aiona']['stage'] == 'ERROR'
    assert result['evaluation_progress']['aiona']['active'] == []
    assert result['evaluations']['aiona']['cases'][0]['answer'] == 'original'
    assert result['evaluations']['aiona']['jev_eval']['records'] == [None]


def test_retry_clears_failure_message_while_preserving_uploaded_originals(tmp_path):
    judge = StagedJudge()
    def broken_resolver():
        raise ValueError('unsupported test engine')
    evaluator = CodeEvalService(save_dir=tmp_path / 'eval', jev_resolver=broken_resolver)
    service = ComparisonService(evaluator, save_dir=tmp_path / 'compare')
    cases = [{'question': 'first', 'answer': '  original\r\n'}]
    rid = service.start({'cases': cases})['run_id']
    service.evaluate(rid, {})
    until(lambda: service.status(rid)['status'] == 'ERROR')
    assert service.status(rid)['error']
    evaluator._jev_resolver = lambda: (judge, judge.config, None)
    try:
        service.evaluate(rid, {})
        assert judge.entered['first'].wait(3)
        assert service.status(rid)['status'] == 'EVALUATING'
        assert service.status(rid)['error'] is None
        assert service.result(rid)['datasets']['aiona'][0]['answer'] == cases[0]['answer']
        judge.release['first'].set()
        until(lambda: service.status(rid)['status'] == 'COMPLETED')
        assert service.status(rid)['error'] is None
        saved = json.loads((tmp_path / 'compare' / f'{rid}.json').read_text(encoding='utf-8'))
        assert saved['error'] is None
        assert saved['datasets']['aiona'][0]['answer'] == cases[0]['answer']
    finally:
        judge.release['first'].set()
        until(lambda: service.status(rid)['status'] != 'EVALUATING')
