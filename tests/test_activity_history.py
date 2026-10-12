"""Durable activity and model-version contracts; all runs use temporary data and fixture judges."""
import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from test_aiona_versions import CASES, make_service
from test_comparison_workspace import wait
from test_comparison_cancellation import until
from test_live_comparison_progress import StagedJudge
from webapp.activity_store import ActivityStore, event_record
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server
from webapp.version_registry import VersionRegistry


def test_registration_only_default_reason_and_restart(tmp_path):
    service, judge = make_service(tmp_path)
    initial = service.manage_release({'version': 'v1', 'set_current': False})
    assert initial['current_release'] is None
    assert [event['event_type'] for event in service.activity_history()['events']] == ['model_version_registered']
    service.manage_release({'action': 'activate', 'version': 'v1', 'reason': '첫 평가 기준'})
    service.manage_release({'version': 'v2', 'set_current': False, 'notes': '변경 설명', 'actor': 'admin'})
    assert service.version_history()['current_release']['version'] == 'v1'
    original = service.start({'cases': CASES})
    before = (service.save_dir / (original['run_id'] + '.json')).read_bytes()
    service.manage_release({'action': 'activate', 'version': 'v2', 'reason': '새 평가부터 사용', 'expected_current_version': 'v1'})
    event = service.activity_history({'event_type': 'default_version_changed'})['events'][0]
    assert event['before']['version'] == 'v1' and event['after']['version'] == 'v2'
    assert event['reason'] == '새 평가부터 사용' and event['actor'] == {'type': 'local', 'label': '로컬 사용자'}
    assert service.result(original['run_id'])['aiona_release']['version'] == 'v1'
    assert (service.save_dir / (original['run_id'] + '.json')).read_bytes() == before
    restarted = ComparisonService(service.evaluator, save_dir=service.save_dir, registry_path=tmp_path / 'registry.json')
    assert restarted.version_history()['current_release']['version'] == 'v2'
    assert restarted.activity_history() == service.activity_history() and not judge.calls
    with pytest.raises(ValueError, match='변경되었습니다'):
        service.manage_release({'action': 'activate', 'version': 'v1', 'expected_current_version': 'v1'})


def test_registry_event_failure_rolls_back_both_changes(tmp_path, monkeypatch):
    registry = VersionRegistry(tmp_path / 'versions.json')
    registry.manage({'version': 'v1', 'set_current': False})
    before_state, before_events = registry.releases(), registry.activity.events()
    def fail(*args):
        raise sqlite3.OperationalError('fixture disk failure')
    monkeypatch.setattr(ActivityStore, 'insert', fail)
    with pytest.raises(sqlite3.OperationalError):
        registry.manage({'version': 'v2', 'set_current': True})
    recovered = VersionRegistry(registry.path)
    assert recovered.releases() == before_state and recovered.current_release() is None
    assert recovered.activity.events() == before_events


def test_concurrent_registry_instances_and_event_replays_lose_no_records(tmp_path):
    path = tmp_path / 'versions.json'
    def register(index):
        VersionRegistry(path).manage({'version': f'v{index}', 'set_current': False})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(register, range(24)))
    registry = VersionRegistry(path)
    events = registry.activity.events()['events']
    assert len(registry.releases()) == len(events) == 24 and registry.current_release() is None
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: registry.activity.append(events), range(16)))
    assert len(registry.activity.events()['events']) == 24
    assert len({event['event_id'] for event in events}) == 24


def test_reevaluation_outbox_preserves_original_and_completion_is_idempotent(tmp_path):
    service, judge = make_service(tmp_path)
    first = wait(service, service.start_version_run({'cases': CASES, 'input_mode': 'manual', 'aiona_release': {'version': 'v1'}})['run_id'])
    path = service.save_dir / (first['run_id'] + '.json')
    original_bytes = path.read_bytes()
    second = wait(service, service.start_version_run({'source_run_id': first['run_id'], 'aiona_release': {'version': 'v1'}})['run_id'])
    assert second['input_mode'] == 'manual' and second['source_run_id'] == first['run_id']
    assert path.read_bytes() == original_bytes
    events = service.activity_history()['events']
    assert len(events) == 5
    assert [event['event_type'] for event in events].count('evaluation_completed') == 2
    reeval = next(event for event in events if event['event_type'] == 'reevaluation_created')
    assert reeval['source_run_id'] == first['run_id'] and reeval['run_id'] == second['run_id']
    assert all(event['actor']['type'] == 'system' for event in events if event['event_type'] == 'evaluation_completed')
    assert not any(key.startswith('_activity_') for key in second)
    count = len(judge.calls)
    for _ in range(3):
        service.version_history()
        assert service.activity_history()['events'] == events
    assert len(judge.calls) == count
    restarted = ComparisonService(service.evaluator, save_dir=service.save_dir, registry_path=tmp_path / 'registry.json')
    assert restarted.activity_history()['events'] == events
    assert path.read_bytes() == original_bytes


def test_cancellation_and_worker_finally_emit_only_one_terminal_event(tmp_path):
    judge = StagedJudge()
    evaluator = CodeEvalService(save_dir=tmp_path / 'eval', jev_resolver=lambda: (judge, judge.config, None))
    service = ComparisonService(evaluator, save_dir=tmp_path / 'runs')
    rid = service.start_version_run({'cases': [{'question': 'first', 'answer': 'original'}],
                                     'aiona_release': {'version': 'v1'}})['run_id']
    try:
        assert judge.entered['first'].wait(3)
        service.cancel(rid)
        service.cancel(rid)
        judge.release['first'].set()
        until(lambda: service.status(rid)['status'] == 'CANCELLED')
        until(lambda: not service._workers)
        events = service.activity_history({'run_id': rid})['events']
        terminal = [event for event in events if event['event_type'] == 'evaluation_cancelled']
        assert len(terminal) == 1 and terminal[0]['actor']['type'] == 'local'
        service.cancel(rid)
        assert service.activity_history({'run_id': rid})['events'] == events
    finally:
        judge.release['first'].set()
        service.shutdown(timeout=3)


def test_interrupted_new_run_is_a_system_failure_legacy_read_does_not_invent_events(tmp_path):
    service, judge = make_service(tmp_path)
    rid = service.start({'cases': CASES, 'aiona_release': {'version': 'v1'}})['run_id']
    with service._lock:
        run = service._run(rid)
        run['status'] = 'EVALUATING'
        service._save(run)
    restarted = ComparisonService(service.evaluator, save_dir=service.save_dir, registry_path=tmp_path / 'registry.json')
    assert restarted.status(rid)['status'] == 'ERROR'
    failed = restarted.activity_history({'event_type': 'evaluation_failed'})['events']
    assert len(failed) == 1 and failed[0]['actor']['type'] == 'system'
    assert '재시작' in failed[0]['error_summary']
    legacy = copy.deepcopy(run)
    legacy = {key: value for key, value in legacy.items() if not key.startswith('_activity_')}
    legacy.update(run_id='abcdef123456', status='COMPLETED')
    legacy_path = service.save_dir / 'abcdef123456.json'
    legacy_path.write_text(json.dumps(legacy), encoding='utf-8')
    before = legacy_path.read_bytes()
    assert restarted.activity_history({'run_id': 'abcdef123456'}) == {'events': [], 'total': 0}
    assert legacy_path.read_bytes() == before and not judge.calls


def test_worker_failure_and_retry_have_one_terminal_event_per_attempt(tmp_path):
    class FailingEvaluator(CodeEvalService):
        def evaluate(self, *args, **kwargs):
            raise RuntimeError('fixture failure, never copy credentials from exceptions')
    service = ComparisonService(FailingEvaluator(save_dir=tmp_path / 'eval'), save_dir=tmp_path / 'runs')
    rid = service.start_version_run({'cases': CASES, 'aiona_release': {'version': 'v1'}})['run_id']
    assert wait(service, rid)['status'] == 'ERROR'
    until(lambda: not service._workers)
    assert wait(service, service.evaluate(rid, {})['run_id'])['status'] == 'ERROR'
    events = service.activity_history({'run_id': rid})['events']
    assert len(events) == 4
    assert sum(event['event_type'] == 'evaluation_failed' for event in events) == 2


def test_failed_initial_audit_save_never_launches_models_and_can_recover(tmp_path, monkeypatch):
    service, judge = make_service(tmp_path)
    append = service.version_registry.activity.append
    def fail(*args):
        raise sqlite3.OperationalError('fixture disk failure')
    monkeypatch.setattr(service.version_registry.activity, 'append', fail)
    with pytest.raises(OSError, match='저장하지 못해'):
        service.start_version_run({'cases': CASES, 'aiona_release': {'version': 'v1'}})
    assert not judge.calls and not service._workers
    rid = next(iter(service._runs))
    assert service.status(rid).get('activity_save_error')
    monkeypatch.setattr(service.version_registry.activity, 'append', append)
    assert {event['event_type'] for event in service.activity_history()['events']} == {'evaluation_started', 'evaluation_failed'}
    assert not service.status(rid).get('activity_save_error')
    saved = json.loads((service.save_dir / (rid + '.json')).read_text(encoding='utf-8'))
    assert not saved.get('activity_save_error')
    restarted = ComparisonService(service.evaluator, save_dir=service.save_dir, registry_path=tmp_path / 'registry.json')
    assert not restarted.status(rid).get('activity_save_error')
    # Recover a previously persisted warning as well, without losing the outbox.
    saved['activity_save_error'] = 'earlier failed append'
    (service.save_dir / (rid + '.json')).write_text(json.dumps(saved), encoding='utf-8')
    restarted.activity_history()
    assert not restarted.status(rid).get('activity_save_error')
    recovered = ComparisonService(service.evaluator, save_dir=service.save_dir, registry_path=tmp_path / 'registry.json')
    assert not recovered.status(rid).get('activity_save_error')


def test_pass_summary_requires_all_code_and_semantic_verdicts():
    case = {'case_status': 'EVALUATED', 'results': [], 'summary': {'common': {'pass': 1, 'fail': 0, 'unjudged': 0}}}
    record = {'status': 'SUCCESS', 'criteria': ['J1'], 'decisions': {'J1': {'decision': 'PASS'}}}
    run = {'question_count': 1, 'status': 'COMPLETED', 'evaluations': {'aiona': {'cases': [case], 'jev_eval': {'records': [record]}}}}
    assert ComparisonService._evaluation_summary(run)['scored'] == 1
    record['decisions']['J1']['decision'] = None
    assert ComparisonService._evaluation_summary(run)['incomplete'] == 1
    record['decisions']['J1']['decision'] = 'PASS'
    case['summary']['common']['unjudged'] = 1
    assert ComparisonService._evaluation_summary(run)['incomplete'] == 1
    record.update(review_required=True)
    assert ComparisonService._evaluation_summary(run)['review_required'] == 1


@pytest.mark.parametrize('failure', ['code_summary', 'code_result', 'semantic'])
def test_failed_question_is_scored_once_while_unknown_criteria_remain_auditable(failure):
    case = {'case_status': 'EVALUATED', 'results': [],
            'summary': {'common': {'pass': 1, 'fail': 0, 'unjudged': 1}}}
    record = {'status': 'SUCCESS', 'criteria': ['J1', 'J5'], 'review_required': True,
              'decisions': {'J1': {'decision': 'PASS'},
                            'J5': {'decision': None, 'status': 'UNVERIFIED'}}}
    if failure == 'code_summary':
        case['summary']['common']['fail'] = 1
    elif failure == 'code_result':
        case['results'] = [{'area': 'common', 'counted': True, 'verdict': 'FAIL'}]
    else:
        record['decisions']['J1']['decision'] = 'FAIL'
    run = {'question_count': 1, 'evaluations': {'aiona': {'cases': [case], 'jev_eval': {'records': [record]}}}}
    original = copy.deepcopy(run)
    summary = ComparisonService._evaluation_summary(run)
    assert summary == {'total': 1, 'scored': 1, 'incomplete': 0, 'review_required': 0,
                       'criteria_incomplete': 1, 'criterion_review_required': 1}
    assert run == original


def test_code_failure_is_scored_before_semantic_response_arrives():
    case = {'case_status': 'EVALUATED', 'results': [],
            'summary': {'common': {'pass': 2, 'fail': 1, 'unjudged': 0}}}
    run = {'question_count': 1, 'evaluations': {'aiona': {'cases': [case]}}}
    assert ComparisonService._evaluation_summary(run) == {
        'total': 1, 'scored': 1, 'incomplete': 0, 'review_required': 0,
        'criteria_incomplete': 1, 'criterion_review_required': 0}


@pytest.mark.parametrize('record', [
    {'status': 'API_ERROR', 'criteria': ['J1'], 'decisions': {'J1': {'decision': 'FAIL'}}},
    {'status': 'RUNNING', 'criteria': ['J1'], 'decisions': {'J1': {'decision': 'FAIL'}}},
    {'status': 'SUCCESS', 'criteria': ['J1'], 'decisions': {
        'J1': {'decision': None, 'status': 'UNVERIFIED'}, 'J5': {'decision': 'FAIL'}}},
])
def test_unsuccessful_or_unrequested_semantic_fail_cannot_settle_question(record):
    case = {'case_status': 'EVALUATED', 'results': [],
            'summary': {'common': {'pass': 1, 'fail': 0, 'unjudged': 0}}}
    run = {'question_count': 1, 'evaluations': {'aiona': {'cases': [case], 'jev_eval': {'records': [record]}}}}
    summary = ComparisonService._evaluation_summary(run)
    assert summary['scored'] == 0
    assert summary['incomplete'] == 1


def test_unresolved_question_and_criterion_counts_use_different_units():
    case = {'case_status': 'EVALUATED', 'results': [],
            'summary': {'common': {'pass': 1, 'fail': 0, 'unjudged': 0}}}
    record = {'status': 'SUCCESS', 'criteria': ['J1', 'J5', 'J6'], 'review_required': True,
              'decisions': {'J1': {'decision': 'PASS'},
                            'J5': {'decision': None, 'status': 'UNVERIFIED'},
                            'J6': {'decision': None, 'status': 'UNVERIFIED'}}}
    run = {'question_count': 1, 'evaluations': {'aiona': {'cases': [case], 'jev_eval': {'records': [record]}}}}
    assert ComparisonService._evaluation_summary(run) == {
        'total': 1, 'scored': 0, 'incomplete': 1, 'review_required': 1,
        'criteria_incomplete': 1, 'criterion_review_required': 2}
    # Excluded input and specialized-only FAILs cannot settle a common outcome.
    case['results'] = [{'area': 'aiona_specialized', 'counted': True, 'verdict': 'FAIL'}]
    assert ComparisonService._evaluation_summary(run)['scored'] == 0
    case['case_status'] = 'INPUT_ERROR'
    record['decisions']['J1']['decision'] = 'FAIL'
    assert ComparisonService._evaluation_summary(run)['scored'] == 0


def test_filters_use_korean_date_boundaries_and_redact_credentials(tmp_path):
    service, judge = make_service(tmp_path)
    service.configure('gpt', {'key': 'private-provider-key-value', 'model': 'fixture-model'})
    service.manage_release({'version': 'v1', 'set_current': True,
                            'notes': 'private-provider-key-value', 'reason': 'api_key=sk-test12345678901234'})
    events = service.activity_history()['events']
    assert 'private-provider-key-value' not in str(events) and 'sk-test12345678901234' not in str(events)
    service.version_registry.activity.append([event_record('evaluation_started', key='seoul-boundary',
        timestamp='2026-10-11T15:01:00+00:00', model_version='boundary')])
    assert service.activity_history({'version': 'boundary', 'from': '2026-10-12', 'to': '2026-10-12'})['total'] == 1
    assert service.activity_history({'version': 'boundary', 'to': '2026-10-11'})['total'] == 0
    assert not judge.calls


def test_http_activity_query_is_read_only_and_errors_are_not_success(tmp_path):
    service, judge = make_service(tmp_path)
    service.manage_release({'version': 'v1', 'set_current': False})
    server = Server(('127.0.0.1', 0), code_eval_service=service.evaluator, comparison_service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base + '/api/comparison/activity-history?event_type=model_version_registered&version=v1') as response:
            assert json.load(response)['total'] == 1
        with pytest.raises(HTTPError) as exc:
            urlopen(base + '/api/comparison/activity-history?limit=invalid')
        assert exc.value.code == 400
        with pytest.raises(HTTPError) as exc:
            urlopen(Request(base + '/api/comparison/model-versions', data=json.dumps(
                {'version': 'v1', 'set_current': False}).encode(), headers={'Content-Type': 'application/json'}))
        assert exc.value.code == 400
        assert service.activity_history()['total'] == 1 and not judge.calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
