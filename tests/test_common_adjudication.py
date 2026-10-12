"""Binary common-condition completion; all judge responses are fixtures, no paid calls."""
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from code_eval.common_adjudication import resolved_batch, targets_for
from code_eval.combined import combined_summary
from code_eval.config import AionaConfig
from code_eval.engine import evaluate_batch, result_digest
from code_eval.jev_client import JevCall, JevClientConfig
from code_eval.jev_eval import evaluate_jev_batch
from code_eval.report_html import render_report, _question_counts
from webapp.code_eval_service import CodeEvalService

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data/code_eval/AIONA_winz_Q165_Q100_multiturn65_v1.json'


class Judge:
    config = JevClientConfig()

    def __init__(self, decision='PASS', mode=None):
        self.calls = []
        self.decision, self.mode = decision, mode

    def system_one(self, state, questions):
        self.calls.append(copy.deepcopy({'state': state, 'questions': questions}))
        if self.mode == 'timeout':
            return JevCall('TIMEOUT', error='fixture timeout')
        if self.mode == 'exception':
            raise RuntimeError('fixture exception')
        if self.mode == 'bad_body':
            return JevCall('OK', body=['malformed'])
        answers = {}
        for cid in questions:
            decision = self.decision if cid.startswith('C') else 'PASS'
            answers[cid] = {'choice': decision, 'confidence': .9,
                            'probabilities': {'PASS': .95 if decision == 'PASS' else .05,
                                              'FAIL': .95 if decision == 'FAIL' else .05},
                            'reason': '보완 근거: 원문의 지정 범위를 확인한 대역 판정.'}
            if cid.startswith('C'):
                if self.mode == 'missing':
                    del answers[cid]
                elif self.mode == 'invalid':
                    answers[cid]['choice'] = 'HOLD'
                elif self.mode == 'no_reason':
                    del answers[cid]['reason']
            elif self.mode == 'bad_j':
                answers[cid]['choice'] = 'MAYBE'
        return JevCall('OK', body={'model': 'fixture-only', 'answers': answers}, attempts=1)


def sample(*indices):
    if not DATA.is_file():
        pytest.skip('Optional private question dataset is not included in public checkouts')
    rows = json.loads(DATA.read_text(encoding='utf-8'))
    if isinstance(rows, dict):
        rows = rows['cases']
    return [rows[i - 1] for i in indices]


def evaluate(cases, judge):
    batch = evaluate_batch(cases, AionaConfig())
    original = copy.deepcopy(batch)
    batch['jev_eval'] = evaluate_jev_batch(batch, judge)
    assert batch['cases'] == original['cases']
    assert batch['summary'] == original['summary']
    assert result_digest(batch['cases']) == batch['result_digest']
    return batch, resolved_batch(batch)


@pytest.mark.parametrize('decision', ['PASS', 'FAIL'])
def test_real_partial_scope_sort_yaml_get_binary_results_in_all_views(decision):
    judge = Judge(decision)
    cases = sample(9, 41, 91)
    batch, final = evaluate(cases, judge)
    assert len(judge.calls) == 3  # same JEV call, no extra inference calls
    assert batch['summary']['common']['unjudged'] >= 3
    assert final['summary']['common']['unjudged'] == 0
    for i, case in enumerate(batch['cases']):
        record = batch['jev_eval']['records'][i]
        targets = targets_for(case)
        assert targets
        for target in targets:
            new = final['cases'][i]['results'][target['index']]
            assert new['verdict'] == decision and new['evaluation_method'] == 'LLM_COMPLETION'
            assert new['code_original']['verdict'] is None
        for before, after in zip(case['results'], final['cases'][i]['results']):
            if before['verdict'] is not None or before['area'] != 'common':
                assert before == after
        assert record['code_completion']['status'] == 'SUCCESS'
    for call in judge.calls:
        source = next(row for row in cases if row['question'] == call['state']['question'])
        assert call['state'] == {'question': source['question'], 'answer': source['answer']}
        assert all(key.startswith(('J', 'C')) for key in call['questions'])
        supplement = {k: v for k, v in call['questions'].items() if k.startswith('C')}
        text = json.dumps(supplement)
        assert 'UNSUPPORTED' not in text and 'provider' not in text and 'measured' not in text
    summary = combined_summary(batch)
    assert summary['pending'] == 0
    assert (summary['pass'], summary['fail']) == ((3, 0) if decision == 'PASS' else (0, 3))
    html = render_report(batch)
    assert 'LLM 보완' in html and '보완 근거:' in html
    assert '코드 원본 결과와 해시' in html
    assert resolved_batch(final) == final  # projection is repeatable


@pytest.mark.parametrize('mode,status', [('timeout', 'TIMEOUT'), ('exception', 'API_ERROR'),
    ('bad_body', 'INVALID_RESPONSE'), ('missing', 'INVALID_RESPONSE'),
    ('invalid', 'INVALID_RESPONSE'), ('no_reason', 'INVALID_RESPONSE')])
def test_operational_failures_never_become_quality_pass_or_fail(mode, status):
    batch, final = evaluate(sample(9), Judge(mode=mode))
    record = batch['jev_eval']['records'][0]
    assert record['code_completion']['status'] == status
    assert record['code_completion']['error']
    assert final['cases'] == batch['cases']
    assert combined_summary(batch)['pending'] == 1
    assert combined_summary(batch)['pass'] == combined_summary(batch)['fail'] == 0
    assert '문항 모두 통과' not in render_report(batch)
    stats = _question_counts(final['cases'], 'common')
    assert stats['pass'] == 0 and stats['unjudged'] == 1
    if mode in ('missing', 'invalid', 'no_reason'):
        assert record['status'] == 'SUCCESS'  # valid J judgments retained independently


def test_code_completion_survives_invalid_semantic_decision():
    batch, final = evaluate(sample(9), Judge(mode='bad_j'))
    assert batch['jev_eval']['records'][0]['status'] == 'INVALID_RESPONSE'
    assert final['summary']['common']['unjudged'] == 0
    assert combined_summary(batch)['pending'] == 1


def test_specialized_rules_remain_outside_completion_and_common_total():
    batch, final = evaluate(sample(9), Judge())
    special = [r for r in final['cases'][0]['results'] if r['area'] != 'common']
    assert special and all(r['verdict'] is None for r in special)
    assert combined_summary(batch)['pass'] == 1
    # A specialized violation is still listed separately, never a common FAIL.
    special[0].update(verdict='FAIL', counted=True, status='EXECUTED')
    assert combined_summary(batch)['fail'] == 0


def test_full_history_and_reference_are_preserved_when_completing():
    earlier = '  앞 대화 원문\n' * 500
    later = {**sample(9)[0], 'conversation_id': 'hidden-conversation', 'turn': 2, 'reference': {'source': earlier}}
    judge = Judge()
    batch, final = evaluate([{'question': '첫 질문', 'answer': earlier, 'conversation_id': 'hidden-conversation', 'turn': 1}, later], judge)
    request = next(c for c in judge.calls if c['state']['question'] == later['question'])
    assert request['state']['history'] == [{'turn': 1, 'question': '첫 질문', 'answer': earlier}]
    assert request['state']['reference'] == later['reference']
    assert set(request['state']) == {'question', 'answer', 'history', 'reference'}
    assert final['summary']['common']['unjudged'] == 0


def test_service_progress_export_and_browser_share_completed_view(tmp_path):
    judge, events = Judge('FAIL'), []
    service = CodeEvalService(save_dir=tmp_path, jev_resolver=lambda: (judge, judge.config, None))
    batch = service.evaluate({'cases': sample(9, 41, 91)}, observer=events.append)
    completed = [e['record'] for e in events if 'record' in e]
    assert len(completed) == 3
    assert all(r['code_completion']['status'] == 'SUCCESS' for r in completed)
    saved = next(tmp_path.glob('code-eval-*.json'))
    saved_batch = json.loads(saved.read_text(encoding='utf-8'))
    assert saved_batch['combined']['fail'] == 3 and saved_batch['combined']['pending'] == 0
    assert result_digest(saved_batch['cases']) == saved_batch['result_digest']
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for production UI projection verification')
    script = r'''
const assert = require('node:assert/strict');
const fs = require('fs');
const api = require('./frontend/src/workspace-results.js');
const b = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const before = JSON.stringify(b), final = api.resolvedBatch(b);
assert.equal(final.summary.common.unjudged, 0);
assert.equal(final.summary.common.fail, 3);
const r = {status:'COMPLETED', providers:['aiona'], question_count:3,
  datasets:{aiona:b.cases}, evaluations:{aiona:b}};
assert.equal(api.runSummary(r).allComplete, true);
assert.deepEqual(api.runSummary(r).counts, {issue:3,clear:0,pending:0,waiting:0,running:0});
for (const c of api.comparisonCriteria.filter(c => c.group==='code'))
  assert.equal(api.criterionSummary(r, 'aiona', c).pending, 0);
assert.equal(api.criterionSummary(r, 'aiona', api.comparisonCriteria.at(-1)).targets, 0);
assert.equal(JSON.stringify(b), before);
assert.deepEqual(api.resolvedBatch(final), final);
'''
    result = subprocess.run([node, '-e', script, str(saved)], cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
