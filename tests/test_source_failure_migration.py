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
from webapp.registry_store import RegistryStore
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
    # v3: 출처 원문 확인 실패는 FAIL 이 아니라 보류 — 질문은 FAIL 로 세지 않고 미결(pending)로 남는다
    assert report['after']['aiona'] == {'questions': 2, 'pass': 0, 'fail': 0, 'pending': 2}
    assert report['after']['gemini'] == {'questions': 2, 'pass': 1, 'fail': 0, 'pending': 1}
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
    update = current['evaluation_policy_updates'][0]
    assert update['update_id'] == report['update_id'] and update['actor'] == 'local' and update['policy_version'] == SOURCE_ORIGINAL_FAILURE_POLICY_VERSION
    assert not any(key.startswith('_activity_') for key in current)   # 작업 이력(시스템 로그)은 남기지 않는다
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
            held = batch['jev_eval']['records'][0]['decisions']['J5']
            assert held['decision'] is None and held['status'] == 'UNVERIFIED' and held['reason_code'] == 'SOURCE_ORIGINAL_UNAVAILABLE'
            # 보류는 unverified 로 세고 fail 로 세지 않는다 (aiona 는 Q2 의 입력 일치 미확인 보류가 하나 더 있다)
            assert batch['jev_eval']['summary']['J5']['fail'] == 0
            assert batch['jev_eval']['summary']['J5']['unverified'] == (2 if provider == 'aiona' else 1)
            assert batch['jev_eval']['completed'] == (0 if provider == 'aiona' else 1)
            assert batch['jev_eval']['review_required'] == (2 if provider == 'aiona' else 1)
            assert batch['jev_eval']['status'] == 'PARTIAL'
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


@pytest.mark.parametrize('failure', ['file_replace', 'registry_lock'])
def test_failure_restores_exact_bytes_and_leaves_no_policy_update(tmp_path, monkeypatch, failure):
    path, _ = make_run(tmp_path)
    before = files(tmp_path)
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
        def fail_lock(self):
            raise sqlite3.OperationalError('fixture lock error')
        monkeypatch.setattr(RegistryStore, 'connect', fail_lock)
    with pytest.raises((OSError, sqlite3.Error)):
        migration.migrate(tmp_path, RUN_ID, apply=True)
    for name, body in before.items():
        # SQLite may update its header during a rolled-back transaction; only the run files are compared.
        if name.endswith('.json'):
            assert (tmp_path / name).read_bytes() == body
    assert 'evaluation_policy_updates' not in json.loads(path.read_bytes())
    audits = list((tmp_path / 'backups').rglob('audit.json'))
    assert len(audits) == 1 and json.loads(audits[0].read_bytes())['status'] == 'ROLLED_BACK'
    for name, body in before.items():
        if name.endswith('.json'):
            backup = audits[0].parent / name
            if backup.is_file():
                assert backup.read_bytes() == body
    assert not list(tmp_path.rglob('*.tmp'))


def _rewrite(path, run, edit):
    """저장 실행과 연결된 평가 파일을 같은 내용으로 함께 고친다 (마이그레이션 입력 준비용)."""
    for batch in run['evaluations'].values():
        edit(batch)
        linked = path.parents[2] / batch['run']['saved_to']
        standalone = copy.deepcopy(batch)
        standalone['run'].pop('saved_to')   # 연결 파일은 saved_to 없이 저장된 그대로
        linked.write_text(json.dumps(standalone, ensure_ascii=False), encoding='utf-8')
    path.write_text(json.dumps(run, ensure_ascii=False), encoding='utf-8')


def test_reapplied_policy_recomputes_a_saved_j5_outcome_instead_of_leaving_it_stale(tmp_path):
    """지시서 10-14 P0-1 — 정책으로 J5 사유가 바뀌었는데 j5_outcome 이 정책 전 값으로 남던 문제 (v3: 보류 held → 출처 원문 확인 실패)."""
    from code_eval.semantic_policy import j5_outcome
    path, run = make_run(tmp_path)

    def stamp(batch):
        for record in batch['jev_eval']['records']:
            record['decisions']['J5']['j5_outcome'] = j5_outcome(record)   # 새 평가기가 저장하는 대로
    _rewrite(path, run, stamp)
    assert run['evaluations']['aiona']['jev_eval']['records'][0]['decisions']['J5']['j5_outcome']['code'] == 'held'
    migration.migrate(tmp_path, RUN_ID, apply=True)
    saved = json.loads(path.read_text(encoding='utf-8'))
    j5 = saved['evaluations']['aiona']['jev_eval']['records'][0]['decisions']['J5']
    assert j5['decision'] is None and j5['status'] == 'UNVERIFIED' and j5['reason_code'] == 'SOURCE_ORIGINAL_UNAVAILABLE'
    assert j5['j5_outcome']['code'] == 'source_unavailable' and j5['j5_outcome']['factual_error_confirmed'] is False
    assert j5['j5_outcome']['source_failure']['kind'] == 'access_failed'
    untouched = saved['evaluations']['gemini']['jev_eval']['records'][1]['decisions']['J5']
    assert untouched['j5_outcome']['code'] == 'verified'          # 바뀌지 않은 레코드는 그대로
    plain = json.loads(path.read_text(encoding='utf-8'))['evaluations']['claude']['jev_eval']['records'][0]['decisions']['J5']
    assert plain['j5_outcome']['code'] == 'verified'
    from tools.j5_audit import audit_consistency
    assert audit_consistency(saved)['violations'] == []   # 재적용 뒤 j5_outcome·사유 코드·상태가 서로 맞는다 (10-14 3차)


def test_refreshed_summary_counts_incomplete_like_the_evaluator(tmp_path):
    """검색이 꺼져 미완료가 된 J5 를 재집계가 세지 않아 summary.incomplete 가 비거나 옛 값으로 남던 문제."""
    path, run = make_run(tmp_path)
    scope = '사용자 원문 인용은 확인했지만, 요청에 주어진 자료·설정의 일치를 확인하는 주장인지 불명확합니다.'
    question, answer = '처음 배부한 내역을 정정할게. 노트는 4권을 배부했어.', '배부 4권'

    def search_off(batch):
        if batch is run['evaluations']['aiona']:
            record = batch['jev_eval']['records'][0]
            record['verification'] = {'search_mode': 'disabled', 'search_performed': False}
            record['decisions']['J5']['fact_check'] = {'basis': 'none', 'claims': [
                {'claim': 'fixture answer', 'verdict': 'UNVERIFIED', 'scope': 'external_fact', 'reason': '검색 없음', 'sources': []}]}
            # 도구는 PASS/FAIL 로 바뀐 기준이 있는 평가만 다시 집계·저장한다(v3 의 보류 전환은 세지 않음) —
            # Q2 를 입력 범위 재검증으로 PASS 가 되는 보류로 두어 aiona 평가의 재집계를 일으킨다
            batch['cases'][1].update(question=question, answer=answer)
            batch['jev_eval']['records'][1]['decisions']['J5'].update(
                reason=scope, model_original_assessment={'choice': 'PASS', 'reason': '사용자 제공값과 일치'},
                fact_check={'basis': 'input', 'claims': [{'claim': answer, 'verdict': 'UNVERIFIED', 'scope': 'input_consistency',
                    'scope_quote': question, 'reason': scope, 'sources': [{'url': '', 'origin': 'question', 'turn': None, 'quote': question,
                    'input_quote_verified': True, 'input_scope_verified': False, 'quote_verified': False, 'verification_reason': scope}]}]})
        migration._refresh_summary(batch)
        batch['jev_eval'].pop('decision_policy')
    _rewrite(path, run, search_off)
    report = migration.migrate(tmp_path, RUN_ID, apply=True, policy='final')
    saved = json.loads(path.read_text(encoding='utf-8'))
    semantic = saved['evaluations']['aiona']['jev_eval']
    assert semantic['records'][0]['decisions']['J5']['status'] == 'INCOMPLETE'
    assert semantic['records'][1]['decisions']['J5']['decision'] == 'PASS'
    assert semantic['summary']['J5']['incomplete'] == 1 and semantic['incomplete'] == 1
    assert report['status'] == 'APPLIED'
