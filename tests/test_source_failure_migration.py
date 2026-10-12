"""Offline one-run migration, exact backups, and failure rollback in temp dirs."""
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from code_eval.combined import combined_summary
from code_eval.engine import evaluate_batch
from code_eval.jev_criteria import CRITERIA
from code_eval.semantic_policy import DECISION_POLICY, SOURCE_ORIGINAL_FAILURE_POLICY_VERSION
from code_eval.semantic_sources import _REASONS
from tools import apply_source_failure_policy as migration
from webapp.activity_store import ActivityStore, event_record
from webapp.version_registry import evaluation_profile


RUN_ID = 'abcdef123456'
SOURCE_POLICY = {'j5_source_original_failure': SOURCE_ORIGINAL_FAILURE_POLICY_VERSION}


def judgment(case_id, kind='pass'):
    j5 = {'criterion_id': 'J5', 'name': 'DATA 신뢰도', 'decision': 'PASS',
          'confidence': .9, 'probabilities': {'PASS': 1.0, 'FAIL': 0.0}, 'reason': '일치함'}
    record = {'case_id': case_id, 'status': 'SUCCESS', 'criteria': ['J1', 'J5'],
              'review_required': kind != 'pass', 'decisions': {
                  'J1': {'criterion_id': 'J1', 'name': '의도 충족', 'decision': 'PASS', 'confidence': .9},
                  'J5': j5}, 'verification': {'source_validation': 'provider_and_content'}}
    if kind != 'pass':
        reason = _REASONS['http_error'] if kind == 'web' else '사용자 원문 일치 범위를 확인하지 못했습니다.'
        j5.update(decision=None, status='UNVERIFIED', confidence=0.0, probabilities={}, reason=reason)
        j5['fact_check'] = {'basis': 'web' if kind == 'web' else 'input', 'claims': [{
            'claim': 'fixture answer', 'verdict': 'UNVERIFIED', 'scope': 'external_fact' if kind == 'web' else 'input_consistency',
            'reason': reason, 'sources': [{'origin': 'web' if kind == 'web' else 'question',
                'url': 'https://example.org/fixture' if kind == 'web' else '',
                'quote': 'fixture quote', 'quote_verified': False, 'verification_reason': reason}]}]}
    return record


def make_run(root):
    run = {'run_id': RUN_ID, 'status': 'COMPLETED', 'revision': 25,
           'aiona_release': {'version': 'v1'}, 'datasets': {}, 'evaluations': {},
           '_activity_outbox': [event_record('evaluation_completed', key='fixture-terminal')],
           'input_document': {'original_text': 'exact original\r\n사용자 입력\n'}}
    modes = {'aiona': ['web', 'input'], 'gemini': ['web', 'pass'],
             'claude': ['pass', 'input'], 'gpt': ['pass', 'pass']}
    for serial, (provider, kinds) in enumerate(modes.items(), 1):
        cases = [{'case_id': f'Q{i + 1}', 'question': '간단하게 설명해줘.',
                  'answer': 'fixture answer\n원문\t preserved', 'reference': None} for i in range(2)]
        run['datasets'][provider] = copy.deepcopy(cases)
        batch = evaluate_batch(cases)
        batch['run']['run_id'] = f'{serial:012x}'
        semantic = {'records': [judgment(cases[i]['case_id'], kind) for i, kind in enumerate(kinds)],
                    'summary': {c.id: {'name': c.name} for c in CRITERIA},
                    'statuses': {'SUCCESS': 2}, 'targets': 2,
                    'version': 'old-semantic', 'criteria_version': 'old-criteria',
                    'applicability_version': 'old-applicability', 'history_policy': 'full_original_text',
                    'models': ['fixture-model'], 'conversations': {'unchanged': 'J6'},
                    'client': {'engine': 'openai_api', 'requested_model': 'fixture-model',
                               'max_tool_calls': 4, 'evidence_recovery_max_tool_calls': 6}}
        batch['jev_eval'] = semantic
        migration._refresh_summary(batch)
        semantic.pop('decision_policy')
        path = root / 'data' / 'code_eval_runs' / f"code-eval-{batch['run']['run_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Standalone files were originally saved before saved_to was assigned.
        path.write_text(json.dumps(batch, ensure_ascii=False), encoding='utf-8')
        batch['run']['saved_to'] = path.relative_to(root).as_posix()
        run['evaluations'][provider] = batch
    run['evaluation_profile'] = evaluation_profile(run)
    path = root / 'data' / 'comparison_runs' / f'{RUN_ID}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run, ensure_ascii=False), encoding='utf-8')
    ActivityStore(root / 'data' / 'aiona_versions.sqlite3').append(run['_activity_outbox'])
    return path, run


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_dry_run_writes_nothing_and_selects_only_source_failures(tmp_path):
    make_run(tmp_path)
    before = files(tmp_path)
    report = migration.migrate(tmp_path, RUN_ID)
    assert files(tmp_path) == before
    assert report['status'] == 'DRY_RUN' and report['external_calls'] == 0
    assert [(r['provider'], r['case_id']) for r in report['changed_records']] == [('aiona', 'Q1'), ('gemini', 'Q1')]
    assert report['after']['aiona'] == {'questions': 2, 'pass': 0, 'fail': 1, 'pending': 1}
    assert report['after']['gemini'] == {'questions': 2, 'pass': 1, 'fail': 1, 'pending': 0}
    assert len(report['files']) == 3 and report['running_server_refresh_required']


def test_apply_preserves_originals_refreshes_all_aggregates_and_is_idempotent(tmp_path):
    path, original = make_run(tmp_path)
    before_files = files(tmp_path)
    report = migration.migrate(tmp_path, RUN_ID, apply=True)
    current = json.loads(path.read_bytes())
    assert report['status'] == 'APPLIED'
    assert current['datasets'] == original['datasets']
    assert current['input_document'] == original['input_document']
    assert current['revision'] == original['revision'] + 1
    assert current['_activity_outbox'][:-1] == original['_activity_outbox']
    assert current['evaluation_policy_updates'][0]['original_evaluation_profile'] == original['evaluation_profile']
    assert current['evaluation_profile']['fields']['decision_policy'] == SOURCE_POLICY
    assert current['evaluation_profile']['fields']['judge'] == original['evaluation_profile']['fields']['judge']
    for provider, batch in current['evaluations'].items():
        old = original['evaluations'][provider]
        assert batch['cases'] == old['cases'] and batch['result_digest'] == old['result_digest']
        assert batch['jev_eval']['client'] == old['jev_eval']['client']
        assert batch['jev_eval']['conversations'] == old['jev_eval']['conversations']
        if provider in ('aiona', 'gemini'):
            assert batch['jev_eval']['decision_policy'] == SOURCE_POLICY
            assert batch['jev_eval']['records'][0]['decisions']['J5']['policy_original_assessment'] == old['jev_eval']['records'][0]['decisions']['J5']
            assert batch['jev_eval']['records'][1] == old['jev_eval']['records'][1]
            assert batch['jev_eval']['summary']['J5']['fail'] == 1
            assert batch['jev_eval']['completed'] == (1 if provider == 'aiona' else 2)
            assert batch['jev_eval']['review_required'] == (1 if provider == 'aiona' else 0)
            assert batch['jev_eval']['status'] == ('PARTIAL' if provider == 'aiona' else 'SUCCESS')
            assert batch['combined'] == combined_summary(batch)
            standalone = json.loads((tmp_path / batch['run']['saved_to']).read_bytes())
            assert migration._without_saved_to(standalone) == migration._without_saved_to(batch)
        else:
            assert batch == old
    for item in report['files']:
        backup = tmp_path / report['backup_directory'] / item['path']
        assert backup.read_bytes() == before_files[item['path']]
        assert migration._hash(backup.read_bytes()) == item['before_sha256']
        assert migration._hash((tmp_path / item['path']).read_bytes()) == item['after_sha256']
    events = ActivityStore(tmp_path / 'data' / 'aiona_versions.sqlite3').events()['events']
    assert len(events) == 2 and events[0]['event_type'] == 'evaluation_policy_applied'
    assert events[0]['actor']['type'] == 'local'
    snapshot = files(tmp_path)
    repeated = migration.migrate(tmp_path, RUN_ID, apply=True)
    assert repeated['status'] == 'NO_CHANGES' and files(tmp_path) == snapshot


@pytest.mark.parametrize('alteration', ['active', 'live_batch', 'linked_mismatch', 'linked_outside', 'missing_linked'])
def test_invalid_or_changed_inputs_never_write(tmp_path, alteration):
    path, run = make_run(tmp_path)
    batch = run['evaluations']['aiona']
    if alteration == 'active':
        run['status'] = 'EVALUATING'
    elif alteration == 'live_batch':
        batch['live'] = True
    elif alteration == 'linked_mismatch':
        linked = tmp_path / batch['run']['saved_to']
        value = json.loads(linked.read_bytes())
        value['cases'][0]['answer'] = 'changed answer'
        linked.write_text(json.dumps(value), encoding='utf-8')
    elif alteration == 'linked_outside':
        batch['run']['saved_to'] = '../unrelated.json'
    elif alteration == 'missing_linked':
        (tmp_path / batch['run']['saved_to']).unlink()
    path.write_text(json.dumps(run, ensure_ascii=False), encoding='utf-8')
    before = files(tmp_path)
    with pytest.raises(ValueError):
        migration.migrate(tmp_path, RUN_ID, apply=True)
    assert files(tmp_path) == before


@pytest.mark.parametrize('failure', ['file_replace', 'activity_insert'])
def test_failure_restores_exact_bytes_and_leaves_no_policy_event(tmp_path, monkeypatch, failure):
    path, _ = make_run(tmp_path)
    before = files(tmp_path)
    old_events = ActivityStore(tmp_path / 'data' / 'aiona_versions.sqlite3').events()
    if failure == 'file_replace':
        original_replace = migration._replace_file
        failed = False
        def fail_once(source, target):
            nonlocal failed
            if target == path and not failed:
                failed = True
                raise OSError('fixture replacement error')
            original_replace(source, target)
        monkeypatch.setattr(migration, '_replace_file', fail_once)
    else:
        def fail_insert(*args):
            raise sqlite3.OperationalError('fixture insert error')
        monkeypatch.setattr(ActivityStore, 'insert', fail_insert)
    with pytest.raises((OSError, sqlite3.Error)):
        migration.migrate(tmp_path, RUN_ID, apply=True)
    for name, body in before.items():
        # SQLite may update its header during a rolled-back transaction; its
        # committed registry/activity content is checked separately below.
        if name.endswith('.json'):
            assert (tmp_path / name).read_bytes() == body
    assert ActivityStore(tmp_path / 'data' / 'aiona_versions.sqlite3').events() == old_events
    audits = list((tmp_path / 'backups').rglob('audit.json'))
    assert len(audits) == 1 and json.loads(audits[0].read_bytes())['status'] == 'ROLLED_BACK'
    for name, body in before.items():
        if name.endswith('.json'):
            backup = audits[0].parent / name
            if backup.is_file():
                assert backup.read_bytes() == body
    assert not list(tmp_path.rglob('*.tmp'))
