"""Offline replay of current source failures; source files and provider APIs stay untouched."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright
from code_eval.semantic_policy import apply_final_decision_policy, apply_source_original_failure_policy
from tools.apply_source_failure_policy import _evaluation_state, _refresh_progress, _refresh_summary
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server


def main(*, source_only=False):
    saved = ROOT / 'data/comparison_runs/ad9bc5c843b1.json'
    original = saved.read_bytes()
    run = json.loads(original)
    original_data = copy.deepcopy(run['datasets'])
    original_cases = {provider: copy.deepcopy(batch['cases']) for provider, batch in run['evaluations'].items()}
    changed, restored = [], []
    for provider, batch in run['evaluations'].items():
        for index, record in enumerate(batch['jev_eval']['records']):
            if source_only:
                j5 = record.get('decisions', {}).get('J5') or {}
                if j5.get('reason_code') == 'INPUT_SCOPE_REVALIDATED':
                    # Rebuild only the historical source-failure scenario in
                    # this in-memory fixture, even after production migration.
                    record['decisions']['J5'] = copy.deepcopy(j5['policy_original_assessment'])
                    record['review_required'] = True
                apply_source_original_failure_policy(record)
            else:
                state = _evaluation_state(batch, index)
                apply_final_decision_policy(record, state=state)
                settled = copy.deepcopy(record)
                apply_final_decision_policy(record, state=state)
                assert record == settled, 'The final policy must be idempotent'
            current = record.get('decisions', {}).get('J5') or {}
            if current.get('reason_code') == 'SOURCE_ORIGINAL_UNAVAILABLE':
                changed.append((provider, index, copy.deepcopy(current['policy_original_assessment'])))
            if not source_only and (provider, record['case_id']) in {('claude', 'Q5_T5'), ('aiona', 'Q5_T7')}:
                assert current['decision'] == 'PASS', current
                assert all(claim['verdict'] == 'PASS' for claim in current['fact_check']['claims'])
                restored.append((provider, index, copy.deepcopy(current['policy_original_assessment'])))
        _refresh_summary(batch)
    _refresh_progress(run, '2026-10-12T05:00:00+00:00')
    assert {provider for provider, _, _ in changed} == {'gemini', 'aiona'}, changed
    assert run['datasets'] == original_data
    assert all(batch['cases'] == original_cases[provider] for provider, batch in run['evaluations'].items())
    for provider, index, prior in changed:
        current = run['evaluations'][provider]['jev_eval']['records'][index]['decisions']['J5']
        assert current['decision'] == 'FAIL' and current['reason_code'] == 'SOURCE_ORIGINAL_UNAVAILABLE'
        assert current['fact_check'] == prior['fact_check']
        assert current['policy_original_assessment'] == prior
        assert any(claim['verdict'] == 'UNVERIFIED' for claim in current['fact_check']['claims'])

    assert len(restored) == (0 if source_only else 2)
    output = ROOT / ('results/source-failure-policy-review' if source_only else 'results/binary-decision-policy-review')
    output.mkdir(parents=True, exist_ok=True)
    errors, writes, external = [], [], []
    expected = {'gemini': [2, 12, 0], 'aiona': [3, 11, 0], 'claude': [2, 12, 0], 'gpt': [0, 14, 0]}
    if source_only:
        expected.update(aiona=[3, 10, 1], claude=[2, 11, 1])

    # Exercise the real finalizer on an unresolved successful assessment; a
    # transport error remains a separate unfinished question.
    generic = copy.deepcopy(run)
    generic['run_id'] = 'eeeeeeeeeee1'
    generic_record = generic['evaluations']['gpt']['jev_eval']['records'][0]
    generic_record['decisions']['J1'].update(status='UNVERIFIED', decision=None, reason='오프라인 검증용: 필요한 근거 없음')
    generic_record['review_required'] = True
    apply_final_decision_policy(generic_record, state=_evaluation_state(generic['evaluations']['gpt'], 0))
    assert generic_record['decisions']['J1']['reason_code'] == 'EVALUATION_EVIDENCE_INSUFFICIENT'
    _refresh_summary(generic['evaluations']['gpt'])
    _refresh_progress(generic, '2026-10-12T05:00:00+00:00')
    failed_request = copy.deepcopy(run)
    failed_request['run_id'] = 'eeeeeeeeeee2'
    failed_record = failed_request['evaluations']['gpt']['jev_eval']['records'][0]
    failed_record.update(status='API_ERROR', decisions={}, review_required=False, error='오프라인 검증용: 연결 실패')
    apply_final_decision_policy(failed_record, state=_evaluation_state(failed_request['evaluations']['gpt'], 0))
    assert failed_record['status'] == 'API_ERROR' and failed_record['decisions'] == {}
    _refresh_summary(failed_request['evaluations']['gpt'])
    _refresh_progress(failed_request, '2026-10-12T05:00:00+00:00')

    def forbidden(*args, **kwargs):
        raise AssertionError('Provider calls forbidden in saved-result policy replay')

    with tempfile.TemporaryDirectory(prefix='source-failure-policy-ui-') as temporary:
        path = Path(temporary)
        evaluator = CodeEvalService(save_dir=path / 'eval', jev_resolver=forbidden)
        service = ComparisonService(evaluator, save_dir=path / 'runs', client_factory=forbidden)
        service._runs[run['run_id']] = copy.deepcopy(run)
        service._runs[generic['run_id']] = generic
        service._runs[failed_request['run_id']] = failed_request
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 1080})

                def read_only(route):
                    if route.request.method != 'GET':
                        writes.append(route.request.url)
                        route.abort()
                    elif not route.request.url.startswith(base + '/'):
                        external.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                context.route('**/*', read_only)
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.add_init_script('sessionStorage.setItem("aiona-comparison-run", "' + run['run_id'] + '")')
                page.goto(base + '/workspace.html#results')
                expect(page.locator('#live-semantic')).to_have_text('56 / 56개')
                if source_only:
                    expect(page.locator('#live-semantic-detail')).to_contain_text('기준 판정 완료 54')
                else:
                    expect(page.locator('#live-semantic-detail')).to_have_text('기준 판정 완료 56')
                for provider, counts in expected.items():
                    card = page.locator(f'.result-model-card[data-provider="{provider}"]')
                    expect(card).to_be_visible()
                    assert [int(value) for value in card.locator('.outcome-count strong').all_text_contents()] == (counts if counts[2] else counts[:2])
                    expect(card.locator('.outcome-counts')).to_have_class('outcome-counts' + ('' if counts[2] else ' final-outcomes'))
                    expect(card.locator('.outcome-count.pending')).to_have_count(1 if counts[2] else 0)
                    policy_note = card.locator('[data-source-policy-fail]')
                    if provider in {'gemini', 'aiona'}:
                        expect(policy_note).to_contain_text('원문 확인 실패 · FAIL 1개')
                    else:
                        expect(policy_note).to_have_count(0)
                    expect(card.locator('.evaluation-metrics')).to_contain_text(f'{14 - counts[2]} / 14개 질문')
                page.locator('#result-models').screenshot(path=str(output / 'model-cards-desktop.png'))

                for provider, index, prior in changed:
                    page.locator(f'[data-focus-key="result-{provider}-{index}"]').click()
                    expect(page.locator('#answer-dialog')).to_be_visible()
                    expect(page.locator('#answer-evaluation .answer-verdict')).to_contain_text('FAIL')
                    expect(page.locator('[data-source-policy-detail]')).to_contain_text('J5 FAIL · 출처 원문 확인 실패')
                    assert page.locator('#original-answer pre').text_content() == original_data[provider][index]['answer']
                    history = page.locator('#answer-evaluation .semantic-policy-original')
                    expect(history).not_to_have_attribute('open', '')
                    history.locator('summary').click()
                    expect(history).to_contain_text('이전 판정: 확인 필요')
                    expect(history).to_contain_text(prior['reason'])
                    facts = page.locator('#answer-evaluation .semantic-fact-evidence')
                    facts.locator('summary').click()
                    expect(facts).to_contain_text('확인 필요')
                    page.locator('#answer-evaluation').screenshot(path=str(output / f'{provider}-source-policy-detail.png'))
                    page.locator('[data-close="answer-dialog"]').first.click()

                    exported = context.request.get(base + f'/api/comparison/runs/{run["run_id"]}/evaluation/{provider}/html')
                    assert exported.status == 200
                    report = exported.text()
                    (output / f'{provider}-policy-report.html').write_text(report, encoding='utf-8')
                    report_page = context.new_page()
                    report_page.on('pageerror', lambda error: errors.append(str(error)))
                    report_page.set_content(report)
                    expect(report_page.locator('[data-source-policy-detail]')).to_have_count(1)
                    expect(report_page.locator('[data-source-policy-detail]')).to_contain_text('J5 FAIL · 출처 원문 확인 실패')
                    policy_case = report_page.locator('details.case').filter(has=report_page.locator('[data-source-policy-detail]'))
                    report_history = policy_case.locator('.semantic-policy-original')
                    report_history.locator('summary').click()
                    expect(report_history).to_contain_text(prior['reason'])
                    expect(policy_case.locator(':scope > summary')).to_contain_text('FAIL')
                    report_page.close()

                for provider, index, prior in restored:
                    # The seventh conversational turn may be on the next table
                    # page. The original-answer dialog offers every question.
                    page.locator(f'[data-focus-key="result-{provider}-0"]').click()
                    page.locator('#answer-select').select_option(str(index))
                    expect(page.locator('#answer-evaluation .answer-verdict')).to_contain_text('PASS')
                    expect(page.locator('[data-evidence-policy-detail]')).to_contain_text('J5 PASS · 제공 자료 대조 완료')
                    assert page.locator('#original-answer pre').text_content() == original_data[provider][index]['answer']
                    history = page.locator('#answer-evaluation .semantic-policy-original')
                    history.locator('summary').click()
                    expect(history).to_contain_text(prior['reason'])
                    page.locator('#answer-evaluation').screenshot(path=str(output / f'{provider}-input-policy-pass-detail.png'))
                    page.locator('[data-close="answer-dialog"]').first.click()
                    exported = context.request.get(base + f'/api/comparison/runs/{run["run_id"]}/evaluation/{provider}/html')
                    assert exported.status == 200
                    report_page = context.new_page()
                    report_page.set_content(exported.text())
                    expect(report_page.locator('[data-evidence-policy-detail]')).to_contain_text('J5 PASS · 제공 자료 대조 완료')
                    report_page.close()

                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('.result-model-card[data-provider="aiona"]').screenshot(path=str(output / 'model-card-mobile.png'))
                page.set_viewport_size({'width': 1440, 'height': 1080})
                page.evaluate('(id) => AionaWorkspace.openRun(id)', generic['run_id'])
                generic_card = page.locator('.result-model-card[data-provider="gpt"]')
                expect(generic_card.locator('[data-evidence-policy-fail]')).to_contain_text('검증 근거 부족 · FAIL 1개')
                assert [int(value) for value in generic_card.locator('.outcome-count strong').all_text_contents()] == [1, 13]
                page.locator('[data-focus-key="result-gpt-0"]').click()
                expect(page.locator('[data-evidence-policy-detail]')).to_contain_text('J1 FAIL · 검증 기준 미충족')
                page.locator('[data-close="answer-dialog"]').first.click()
                page.evaluate('(id) => AionaWorkspace.openRun(id)', failed_request['run_id'])
                error_card = page.locator('.result-model-card[data-provider="gpt"]')
                expect(error_card.locator('.outcome-count.pending strong')).to_have_text('1')
                assert [int(value) for value in error_card.locator('.outcome-count strong').all_text_contents()] == [0, 13, 1]
                expect(error_card.locator('[data-evidence-policy-fail]')).to_have_count(0)
                assert not errors and not writes and not external, (errors, writes, external)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    assert hashlib.sha256(saved.read_bytes()).digest() == hashlib.sha256(original).digest()
    summary = {'status': 'PASS', 'policy_mode': 'source_only' if source_only else 'final',
               'counts_fail_pass_pending': expected, 'source_policy_changes': len(changed),
               'input_scope_pass_restored': len(restored), 'saved_unchanged': True, 'answers_unchanged': True,
               'source_failure_claim_verdicts_retained': True, 'generic_unverified_becomes_policy_fail': True,
               'api_error_kept_unfinished': True,
               'html_reports_consistent': True, 'external_calls': 0, 'writes': 0}
    (output / 'ui-check.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main(source_only='--source-only' in sys.argv)
