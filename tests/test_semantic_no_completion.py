"""Semantic evaluation sends only J criteria and never records a code_completion; judge responses are fixtures."""
import copy

import pytest

from code_eval import engine
from code_eval.combined import combined_summary
from code_eval.config import AionaConfig
from code_eval.jev_eval import evaluate_jev_batch, evaluation_log
from code_eval.report_html import render_report
from semantic_judge_fixture import Judge, sample
from test_jev_eval import NORMAL, ScriptedJev

NO_CONFIG = AionaConfig()


def test_requests_never_carry_c_questions_or_record_code_completion():
    batch = engine.evaluate_batch([NORMAL], NO_CONFIG)
    # 옛 엔진이 남기던 미판정 공통 행이 있어도 C 질문은 만들지 않는다 — 저장된 옛 결과 모양의 사본으로 확인
    case = batch['cases'][0]
    index = next(i for i, r in enumerate(case['results']) if r['area'] == 'common')
    case['results'][index].update(status='UNSUPPORTED', verdict=None, counted=False)
    case['summary']['common']['unjudged'] = 1
    digest = engine.result_digest(batch['cases'])
    client, events = ScriptedJev(), []
    batch['jev_eval'] = evaluate_jev_batch(batch, client, on_event=events.append)
    assert client.requests and all(all(cid.startswith('J') for cid in r['questions']) for r in client.requests)
    assert all('code_completion' not in record for record in batch['jev_eval']['records'])
    assert not any(e['type'] == 'criterion_start' and e['stage'] == 'LLM 보완' for e in events)
    assert all(row['code_completion'] is None for row in evaluation_log(batch))  # 옛 로그 호환 키, 새 기록은 항상 None
    assert engine.result_digest(batch['cases']) == digest


@pytest.mark.parametrize('mode,status', [('timeout', 'TIMEOUT'), ('exception', 'API_ERROR'), ('bad_body', 'INVALID_RESPONSE')])
def test_operational_failures_leave_question_pending_without_completion(mode, status):
    batch = engine.evaluate_batch([NORMAL], NO_CONFIG)
    original = copy.deepcopy(batch['cases'])
    batch['jev_eval'] = evaluate_jev_batch(batch, Judge(mode=mode))
    record = batch['jev_eval']['records'][0]
    assert record['status'] == status and 'code_completion' not in record
    assert batch['cases'] == original
    summary = combined_summary(batch)
    assert summary['pending'] == 1 and summary['pass'] == summary['fail'] == 0
    assert '문항 모두 통과' not in render_report(batch)


def test_specialized_rules_remain_outside_common_total():
    batch = engine.evaluate_batch(sample(9), AionaConfig())
    batch['jev_eval'] = evaluate_jev_batch(batch, Judge())
    case = batch['cases'][0]
    assert case['errors'] == [] and case['summary']['common']['unjudged'] == 0
    special = [r for r in case['results'] if r['area'] != 'common']
    assert special and all(r['verdict'] is None for r in special)
    assert combined_summary(batch)['pass'] == 1
    # A specialized violation is still listed separately, never a common FAIL.
    special[0].update(verdict='FAIL', counted=True, status='EXECUTED')
    assert combined_summary(batch)['fail'] == 0
