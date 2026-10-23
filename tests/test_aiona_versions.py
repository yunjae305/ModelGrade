"""Release metadata and isolated AIONA version runs; no external model calls."""
import copy
import json

import pytest

from test_blind_comparison import CapturingJudge
from test_comparison_workspace import wait
from webapp.code_eval_service import CodeEvalService, ROOT
from webapp.comparison_service import ComparisonService, normalize_dataset
from webapp.version_registry import VersionRegistry, evaluation_profile, fingerprint, validate_release


CASES = [{'id': 'Q1', 'question': ' 1 + 1은?\n', 'answer': ' 2\n', 'reference': {'answer': '2'}}]


def make_service(tmp_path):
    judge = CapturingJudge()
    evaluator = CodeEvalService(save_dir=tmp_path / 'evaluations', jev_resolver=lambda: (judge, judge.config, None))
    service = ComparisonService(evaluator, save_dir=tmp_path / 'runs',
                                registry_path=tmp_path / 'registry.json',
                                client_factory=lambda: pytest.fail('version runs must not collect responses'))
    return service, judge


@pytest.mark.parametrize('release', [None, {}, {'version': 10.1}, {'version': ''},
    {'version': '10.2', 'collected_at': 0}, {'version': '10.2', 'collected_at': '2026-02-30'},
    {'version': '10.2', 'collected_at': '20261009'}, {'version': '10.2', 'notes': None}])
def test_invalid_release_rejected_without_saved_run_or_model_call(tmp_path, release):
    service, judge = make_service(tmp_path)
    with pytest.raises(ValueError):
        service.start_version_run({'cases': CASES, 'aiona_release': release})
    assert not service.history() and not judge.calls
    assert not service.save_dir.exists()


def test_release_strings_and_unknown_collection_date():
    assert validate_release({'version': '10.10'}) == {'version': '10.10', 'collected_at': None, 'notes': ''}
    assert validate_release({'version': '10.1'})['version'] != validate_release({'version': '10.10'})['version']


def test_new_version_run_saves_actual_conditions_and_does_not_collect(tmp_path):
    service, judge = make_service(tmp_path)
    before = copy.deepcopy(CASES)
    run = service.start_version_run({'cases': CASES, 'aiona_release': {'version': '10.10'},
                                     'input_document': {'original_text': ' raw\r\n', 'source_name': 'upload.txt'}})
    result = wait(service, run['run_id'])
    assert CASES == before
    assert result['providers'] == ['aiona'] and len(judge.calls) == 1
    assert result['aiona_release'] == {'version': '10.10', 'collected_at': None, 'notes': ''}
    assert result['datasets']['aiona'][0]['answer'] == CASES[0]['answer']
    assert result['input_document']['original_text'] == ' raw\r\n'
    profile = result['evaluation_profile']
    assert profile['complete'] and profile['fields']['judge_models'] == ['gpt-6.1-sol']
    assert profile['fields']['semantic_version'] == result['evaluations']['aiona']['jev_eval']['version']
    assert not {'executable', 'credentials_source', 'credentials_recorded', 'timeout_s'} & profile['fields']['judge'].keys()
    saved = json.loads((service.save_dir / (run['run_id'] + '.json')).read_text(encoding='utf-8'))
    assert saved['evaluation_profile'] == profile
    assert saved['source_fingerprint'] == fingerprint(result['datasets']['aiona'], answers=True)
    assert service.status(run['run_id'])['evaluation_profile'] == profile


def test_reevaluation_creates_new_run_preserving_source_bytes_and_release(tmp_path):
    service, judge = make_service(tmp_path)
    original = service.start_version_run({'cases': CASES, 'aiona_release': {'version': '10.1', 'notes': 'original'}})
    source = wait(service, original['run_id'])
    source_path = service.save_dir / (source['run_id'] + '.json')
    source_bytes = source_path.read_bytes()
    new = service.start_version_run({'source_run_id': source['run_id'], 'aiona_release': {'version': '10.1'}})
    result = wait(service, new['run_id'])
    assert result['run_id'] != source['run_id'] and result['source_run_id'] == source['run_id']
    assert result['datasets'] == source['datasets'] and result['aiona_release'] == source['aiona_release']
    assert source_path.read_bytes() == source_bytes and len(judge.calls) == 2
    for options in ({'aiona_release': {'version': '10.2'}}, {'cases': CASES, 'aiona_release': {'version': '10.1'}},
                    {'aiona_release': {'version': '10.1'}, 'aiona_config': []}):
        with pytest.raises(ValueError):
            service.start_version_run({'source_run_id': source['run_id'], **options})
    assert len(service.history()) == 2 and source_path.read_bytes() == source_bytes


def test_source_path_traversal_and_unknown_run_do_not_create_files(tmp_path):
    service, judge = make_service(tmp_path)
    for source_id in ('../registry', 'abcdef123456'):
        with pytest.raises(FileNotFoundError):
            service.start_version_run({'source_run_id': source_id, 'aiona_release': {'version': '10.1'}})
    assert not service.save_dir.exists() and not judge.calls


def test_existing_four_model_path_rejects_bad_metadata_before_collection(tmp_path):
    service, judge = make_service(tmp_path)
    for provider in ('gemini', 'claude', 'gpt'):
        service.configure(provider, {'key': 'fixture', 'model': 'fixture-model'})
    with pytest.raises(ValueError):
        service.start_comparison({'cases': CASES, 'aiona_release': {'version': 10.1}})
    assert not service.history() and not judge.calls


def test_model_release_is_independent_of_question_answer_file(tmp_path):
    release = {'version': 'v1', 'collected_at': None, 'notes': ''}
    (tmp_path / 'registry.json').write_text(json.dumps({'current_release': release}), encoding='utf-8')
    service, judge = make_service(tmp_path)
    first = service.start({'cases': CASES, 'name': 'first.json'})
    changed = [{'id': 'Q2', 'question': 'different question', 'answer': 'different answer'}]
    second = service.start({'cases': changed, 'name': 'second.json'})
    assert first['aiona_release'] == second['aiona_release'] == release
    explicit = service.start({'cases': CASES, 'aiona_release': {'version': 'v2'}})
    assert explicit['aiona_release']['version'] == 'v2'
    assert service.status(first['run_id'])['aiona_release']['version'] == 'v1'
    service.version_registry.data['current_release']['version'] = 'v3'
    assert service.status(second['run_id'])['aiona_release']['version'] == 'v1'
    assert not judge.calls


def test_registry_does_not_infer_release_from_source_identity(tmp_path):
    source = {'source_fingerprint': fingerprint(CASES, answers=True),
              'run_ids': ['fixture'], 'aiona_release': {'version': '10.1'}}
    path = tmp_path / 'registry.json'
    path.write_text(json.dumps({'sources': [source]}), encoding='utf-8')
    registry = VersionRegistry(path)
    run = {'run_id': 'fixture', 'datasets': {'aiona': CASES}}
    assert registry.release_for(run) is None
    run['aiona_release'] = {'version': 'v1'}
    assert registry.release_for(run) == {'version': 'v1'}
    run['datasets']['aiona'] = []
    assert registry.release_for(run) == {'version': 'v1'}


def test_fingerprints_separate_question_sets_and_exact_answer_snapshots():
    original = normalize_dataset({'cases': CASES})
    changed = copy.deepcopy(original)
    changed[0]['answer'] += ' updated'
    assert fingerprint(original) == fingerprint(changed)
    assert fingerprint(original, answers=True) != fingerprint(changed, answers=True)
    changed[0]['reference']['answer'] = 'different reference'
    assert fingerprint(original) != fingerprint(changed)


def test_actual_profile_ignores_runtime_paths_and_tracks_real_condition_changes():
    path = ROOT / 'backups/run-history/20261012-105105/comparison_runs/a3b5d89bc041.json'
    if not path.is_file():
        pytest.skip('Optional private saved-run fixture is not included in public checkouts')
    run = json.loads(path.read_text(encoding='utf-8'))
    profile = evaluation_profile(run)
    assert profile['complete']
    client = run['evaluations']['aiona']['jev_eval']['client']
    client.update(executable='different/path', timeout_s=999, credentials_source='private')
    assert evaluation_profile(run) == profile
    client['reasoning_effort'] = 'low'
    assert evaluation_profile(run) != profile
    del run['evaluations']['aiona']['jev_eval']['criteria_version']
    assert evaluation_profile(run)['complete'] is False


def test_version_history_includes_more_than_thirty_and_has_no_model_calls(tmp_path):
    service, judge = make_service(tmp_path)
    for index in range(32):
        service.start({'cases': CASES, 'aiona_release': {'version': '10.10' if index == 31 else '10.2'}})
    restarted = ComparisonService(service.evaluator, save_dir=service.save_dir, registry_path=tmp_path / 'registry.json')
    history = restarted.version_history()
    assert len(restarted.history()) == 30 and len(history['runs']) == 32
    assert history['versions'] == ['10.2', '10.10'] and not judge.calls


