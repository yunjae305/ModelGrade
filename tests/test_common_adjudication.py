"""Legacy reader: saved v16.2.x ``code_completion`` records are still projected for display; nothing new is created.

All inputs are synthetic. The engine no longer produces unjudged common rows or C-criteria completions, so the
old-format record is built by hand exactly as the old engine saved it.
"""
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from code_eval.combined import combined_summary
from code_eval.common_adjudication import resolved_batch
from code_eval.config import AionaConfig
from code_eval.engine import evaluate_batch, result_digest, summarize_batch
from code_eval.report_html import render_report
from webapp.comparison_service import ComparisonService

ROOT = Path(__file__).resolve().parent.parent


def legacy_batch():
    """New batch whose first common row is rewritten to an old UNSUPPORTED row plus a saved SUCCESS completion."""
    batch = evaluate_batch([{'case_id': 'q1', 'question': '안내해줘.', 'answer': '안녕하세요.'}], AionaConfig())
    case = batch['cases'][0]
    resolved_summary = copy.deepcopy(case['summary'])  # the completion stored the fully judged summary
    index = next(i for i, r in enumerate(case['results']) if r['area'] == 'common' and r['verdict'] == 'PASS')
    projected = copy.deepcopy(case['results'][index])
    old = case['results'][index]
    old.update(status='UNSUPPORTED', verdict=None, counted=False, reason='옛 엔진: 코드로 확정할 수 없음')
    projected.update(evaluation_method='LLM_COMPLETION', confidence=.9, reason='보완 근거: 원문의 지정 범위를 확인함',
                     code_original={'status': 'UNSUPPORTED', 'verdict': None, 'reason': old['reason']})
    projected['explain'] = {**projected['explain'], 'headline': f"LLM 보완 · {projected['title']} · PASS",
                            'summary': projected['reason'], 'actual': '', 'fix': '', 'judge': None,
                            'note': '코드 미판정 조건을 원문으로 보완 판정했습니다.'}
    for block in (case['summary']['common'], case['summary']['by_subcategory'][old['subcategory']],
                  case['summary']['by_category'][old['category']]):
        block['pass'] -= 1
        block['decided'] -= 1
        block['unjudged'] += 1
        block['pass_rate'] = (block['pass'] / block['decided']) if block['decided'] else None
    batch['summary'] = summarize_batch(batch['cases'])
    batch['result_digest'] = result_digest(batch['cases'])
    target = {'id': 'C1', 'index': index, 'condition_id': old['condition_id'], 'check_id': old['check_id']}
    record = {'case_id': 'q1', 'status': 'SUCCESS', 'criteria': ['J1'], 'model': 'fixture-only', 'latency_ms': 1,
              'attempts': 1, 'error': None, 'usage': {}, 'j5_basis': None, 'context': None,
              'decisions': {'J1': {'criterion_id': 'J1', 'name': '의도 충족성', 'decision': 'PASS', 'confidence': .9,
                                   'probabilities': {'PASS': .95, 'FAIL': .05}}},
              'code_completion': {'version': 'common-adjudication-v1', 'status': 'SUCCESS', 'targets': [target],
                                  'decisions': {'C1': {'decision': 'PASS', 'reason': projected['reason'], 'confidence': .9}},
                                  'error': None, 'resolutions': [{**target, 'result': projected}],
                                  'summary': resolved_summary}}
    batch['jev_eval'] = {'status': 'SUCCESS', 'engine': 'jev', 'records': [record], 'summary': {}}
    return batch, index


def test_saved_code_completion_record_is_still_projected():
    batch, index = legacy_batch()
    before = copy.deepcopy(batch)
    assert batch['summary']['common']['unjudged'] == 1
    final = resolved_batch(batch)
    row = final['cases'][0]['results'][index]
    assert row['verdict'] == 'PASS' and row['evaluation_method'] == 'LLM_COMPLETION'
    assert row['code_original']['verdict'] is None
    assert final['summary']['common']['unjudged'] == 0 and final['cases'][0]['summary']['common']['unjudged'] == 0
    assert batch == before and result_digest(batch['cases']) == batch['result_digest']  # projection never rewrites
    assert resolved_batch(final) == final  # idempotent
    summary = combined_summary(batch)
    assert (summary['pass'], summary['fail'], summary['pending']) == (1, 0, 0)
    assert 'LLM 보완' in summary['note']
    html = render_report(batch)
    assert 'LLM 보완' in html and '코드 원본 결과와 해시' in html
    run = {'question_count': 1, 'status': 'COMPLETED', 'evaluations': {'aiona': batch}}
    assert ComparisonService._evaluation_summary(run)['scored'] == 1


def test_saved_code_completion_projection_in_browser_module(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for production UI projection verification')
    batch, _ = legacy_batch()
    saved = tmp_path / 'legacy.json'
    saved.write_text(json.dumps(batch, ensure_ascii=False), encoding='utf-8')
    script = r'''
const assert = require('node:assert/strict');
const fs = require('fs');
const api = require('./frontend/src/workspace-results.js');
const b = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const before = JSON.stringify(b);
assert.equal(api.resolvedBatch(b).summary.common.unjudged, 0);
const r = {status:'COMPLETED', providers:['aiona'], question_count:1, datasets:{aiona:b.cases}, evaluations:{aiona:b}};
const state = api.caseState(r, 'aiona', 0);
assert.equal(state.outcome, 'clear');
assert.equal(state.code.pendingReason, '');
assert.match(state.code.reason, /LLM 보완 판정 포함/);
assert.equal(JSON.stringify(b), before);
'''
    result = subprocess.run([node, '-e', script, str(saved)], cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
