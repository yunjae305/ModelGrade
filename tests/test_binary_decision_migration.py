"""최종 판정 정책 마이그레이션(final) — 입력 범위 재검증으로 증명된 PASS 만 되살리고, 나머지 보류는 보류로 남기며 변경을 기록한다."""
import copy
import json
import sqlite3

import pytest

from code_eval.engine import evaluate_case
from code_eval.semantic_policy import DECISION_POLICY, FINAL_DECISION_POLICY_VERSION
from tools import apply_source_failure_policy as migration
from test_source_failure_migration import RUN_ID, files, make_run


SCOPE_REASON = '사용자 원문 인용은 확인했지만, 요청에 주어진 자료·설정의 일치를 확인하는 주장인지 불명확합니다.'


def binary_fixture(root, *, broken_quote=False):
    path, _ = make_run(root)
    # The previous explicit source rule was already applied, like the user's run.
    migration.migrate(root, RUN_ID, apply=True)
    run = json.loads(path.read_bytes())
    correction = ('처음 배부한 내역을 정정할게. 노트는 6권이 아니라 실제로는 4권을 배부했어. '
                  '새 물품이 들어온 것이 아니라 기존 기록의 오류를 고치는 거야. 현재 재고를 다시 계산해줘.')
    rename = ('품목 이름 ‘파일’을 ‘서류철’로 바꿔줘. 새로운 품목을 추가하는 게 아니라 기존 품목의 이름만 바꾸는 거야. '
              '최신 이름, 수량, 단가를 정리해줘.')
    for provider, question, answer, quote in [
        ('claude', correction, '노트 배부 4권으로 정정했습니다.', '노트는 6권이 아니라 실제로는 4권을 배부했어.'),
        ('aiona', rename, '서류철은 기존 파일 재고를 그대로 이어받았으며, 새 품목을 추가하지 않았어요.', rename),
    ]:
        batch = run['evaluations'][provider]
        payload = {'case_id': 'Q2', 'question': question, 'answer': answer}
        batch['cases'][1] = evaluate_case(payload, index=1)
        run['datasets'][provider][1] = payload
        j5 = batch['jev_eval']['records'][1]['decisions']['J5']
        source = {'url': '', 'origin': 'question', 'turn': None,
                  'quote': quote + ('invalid' if broken_quote and provider == 'claude' else ''),
                  'input_quote_verified': True, 'input_scope_verified': False,
                  'quote_verified': False, 'verification_reason': SCOPE_REASON}
        j5.update(reason=SCOPE_REASON, model_original_assessment={'choice': 'PASS', 'reason': '사용자 제공값과 일치'},
                  fact_check={'basis': 'input', 'claims': [{'claim': answer, 'verdict': 'UNVERIFIED',
                      'scope': 'input_consistency', 'scope_quote': question,
                      'reason': SCOPE_REASON, 'sources': [source]}]})
        standalone = copy.deepcopy(batch)
        standalone['run'].pop('saved_to', None)
        (root / batch['run']['saved_to']).write_text(json.dumps(standalone, ensure_ascii=False), encoding='utf8')
    run['evaluation_log'] = {'generation': 'original-generation', 'unjudged': 4, 'rows': {'old': 'original event'}}
    run['evaluation_progress'] = {provider: {'stage': 'COMPLETED', 'code_done': 2, 'code_total': 2,
        'semantic_done': 2, 'semantic_total': 2, 'semantic_success': 1,
        'semantic_unverified': 1, 'semantic_errors': 0, 'skipped': 0,
        'latest': {'index': 1, 'status': 'SUCCESS', 'updated_at': 'original-time'}, 'updated_at': 'original-time'}
        for provider in run['evaluations']}
    path.write_text(json.dumps(run, ensure_ascii=False), encoding='utf8')
    return path, run


def test_final_dry_run_restores_two_original_input_passes_without_writes(tmp_path):
    _, original = binary_fixture(tmp_path)
    before = files(tmp_path)
    report = migration.migrate(tmp_path, RUN_ID, policy='final')
    assert report['policy_version'] == FINAL_DECISION_POLICY_VERSION
    assert report['status'] == 'DRY_RUN' and files(tmp_path) == before
    assert [(r['provider'], r['after'], r['reason_code']) for r in report['changed_records']] == [
        ('aiona', 'PASS', 'INPUT_SCOPE_REVALIDATED'), ('claude', 'PASS', 'INPUT_SCOPE_REVALIDATED')]
    # 출처 원문 확인 실패 보류(aiona·gemini Q1)는 v3 에서 FAIL 로 확정하지 않으므로 미결로 남는다
    assert {provider: counts['pending'] for provider, counts in report['after'].items()} == {'aiona': 1, 'gemini': 1, 'claude': 0, 'gpt': 0}
    assert all(counts['fail'] == 0 for counts in report['after'].values())
    assert report['before']['gemini'] == report['after']['gemini']


def test_final_apply_keeps_prior_hold_and_source_audit_progress_is_current_and_idempotent(tmp_path):
    path, original = binary_fixture(tmp_path)
    # An unrelated historical run is never rewritten.
    historical = path.with_name('111111111111.json')
    historical.write_text('{"preserve": "exact older run"}', encoding='utf8')
    old_history = historical.read_bytes()
    report = migration.migrate(tmp_path, RUN_ID, policy='final', apply=True)
    assert report['status'] == 'APPLIED'
    current = json.loads(path.read_bytes())
    assert historical.read_bytes() == old_history
    assert current['datasets'] == original['datasets']
    assert current['input_document'] == original['input_document']
    assert current['evaluation_log'] == original['evaluation_log']
    assert current['evaluation_policy_updates'][:-1] == original['evaluation_policy_updates']
    assert current['evaluation_policy_updates'][-1]['original_evaluation_progress'] == original['evaluation_progress']
    assert current['evaluation_profile']['fields']['decision_policy'] == DECISION_POLICY
    for provider, batch in current['evaluations'].items():
        old = original['evaluations'][provider]
        assert batch['cases'] == old['cases'] and batch['result_digest'] == old['result_digest']
        assert batch['jev_eval']['client'] == old['jev_eval']['client']
        assert batch['jev_eval']['records'][0] == old['jev_eval']['records'][0]
        detail = current['evaluation_progress'][provider]
        # Q1 의 출처 원문 확인 실패 보류(aiona·gemini)는 그대로 확인 필요로 남는다
        held_q1 = provider in ('aiona', 'gemini')
        assert detail['semantic_success'] == (1 if held_q1 else 2) and detail['semantic_unverified'] == (1 if held_q1 else 0)
        assert detail['semantic_done'] == 2 and detail['updated_at'] == 'original-time'
        assert detail['latest'] == original['evaluation_progress'][provider]['latest']
        if provider in ('aiona', 'claude'):
            j5 = batch['jev_eval']['records'][1]['decisions']['J5']
            assert j5['decision'] == 'PASS'
            assert j5['policy_original_assessment'] == old['jev_eval']['records'][1]['decisions']['J5']
            assert j5['fact_check']['claims'][0]['sources'][0]['quote_verified'] is True
            assert batch['jev_eval']['status'] == ('PARTIAL' if held_q1 else 'SUCCESS')
            assert batch['jev_eval']['unverified'] == (1 if held_q1 else 0)
        else:
            assert batch == old
    assert len(current['evaluation_policy_updates']) == 2 and current['evaluation_policy_updates'][-1]['update_id'] == report['update_id']
    snapshot = files(tmp_path)
    assert migration.migrate(tmp_path, RUN_ID, policy='final', apply=True)['status'] == 'NO_CHANGES'
    assert files(tmp_path) == snapshot


def test_changed_quote_cannot_be_restored_to_pass_and_stays_held(tmp_path):
    path, original = binary_fixture(tmp_path, broken_quote=True)
    report = migration.migrate(tmp_path, RUN_ID, policy='final', apply=True)
    # 인용이 원문과 다르면 PASS 로 되살리지 않는다 — v3 에서는 FAIL 도 아니라 보류(HELD)로 정리되고, 그 정리도 변경 목록에 올라
    # 연결된 평가 파일까지 같이 바뀐다 (보류 전환을 빠뜨리면 비교 실행 파일과 평가 파일이 어긋남)
    assert [(item['provider'], item['after']) for item in report['changed_records']] == [('aiona', 'PASS'), ('claude', 'HELD')]
    j5 = json.loads(path.read_bytes())['evaluations']['claude']['jev_eval']['records'][1]['decisions']['J5']
    assert j5['decision'] is None and j5['status'] == 'UNVERIFIED' and j5['reason_code'] == 'EVALUATION_EVIDENCE_INSUFFICIENT'
    assert j5['policy_original_assessment'] == original['evaluations']['claude']['jev_eval']['records'][1]['decisions']['J5']
    assert j5['decision_policy']['factual_error_confirmed'] is False and j5['decision_policy']['outcome_basis'] == 'held_for_review'


def test_final_replacement_failure_rolls_back_already_policy_updated_files(tmp_path, monkeypatch):
    path, _ = binary_fixture(tmp_path)
    before = files(tmp_path)
    original_replace, replaced = migration._replace_file, []
    def fail_after_first(source, target):
        if replaced:   # 첫 파일은 바뀐 뒤, 두 번째 교체에서 실패 — 이미 바꾼 파일도 원래 바이트로 돌아와야 한다
            raise OSError('fixture replacement failure')
        original_replace(source, target)
        replaced.append(target)
    monkeypatch.setattr(migration, '_replace_file', fail_after_first)
    with pytest.raises(OSError):
        migration.migrate(tmp_path, RUN_ID, policy='final', apply=True)
    monkeypatch.setattr(migration, '_replace_file', original_replace)
    assert replaced and path.read_bytes() == before[path.relative_to(tmp_path).as_posix()]
    for provider in ('aiona', 'claude'):
        linked = json.loads(path.read_bytes())['evaluations'][provider]['run']['saved_to']
        assert (tmp_path / linked).read_bytes() == before[linked]


def test_evaluation_state_preserves_original_full_history_and_reference(tmp_path):
    _, run = binary_fixture(tmp_path)
    batch = run['evaluations']['aiona']
    for i, case in enumerate(batch['cases']):
        case['conversation'] = {'id': 'fixture', 'turn': i + 1}
    batch['cases'][0].update(question='원문\r\n' + 'many original words ' * 500,
                             answer='이전 답변\t그대로')
    batch['cases'][1]['reference'] = {'provided': '원본'}
    state = migration._evaluation_state(batch, 1)
    assert state['history'] == [{'turn': 1, 'question': batch['cases'][0]['question'], 'answer': batch['cases'][0]['answer']}]
    assert state['question'] == batch['cases'][1]['question']
    assert state['answer'] == batch['cases'][1]['answer']
    assert state['reference'] == batch['cases'][1]['reference']
