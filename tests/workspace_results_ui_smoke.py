"""Check result meaning and navigation in Chromium without external model calls.

Q100 uses the original Q/A and real CODE verdicts; semantic responses are fixtures.
Run: python tests/workspace_results_ui_smoke.py
"""
from __future__ import annotations

import copy
import argparse
import hashlib
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright
from code_eval.engine import evaluate_batch
from code_eval.jev_client import resolve_jev_client
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server

LIMIT_ERROR = "claude 오류: You've hit your session limit · resets 1pm (Asia/Seoul)"


def semantic_record(status='SUCCESS', decision='PASS', error=None):
    return {'status': status, 'criteria': ['J1'], 'error': error,
            'decisions': {'J1': {'decision': decision, 'reason': 'UI 검증용 의미 판정 사유'}} if status == 'SUCCESS' else None}


def seed(comparison, cases, name, records, gpt=False):
    run_id = comparison.start({'cases': cases, 'providers': [], 'name': name})['run_id']
    run = comparison._runs[run_id]
    batch = evaluate_batch(run['datasets']['aiona'])
    batch['jev_eval'] = {'criteria': [{'id': 'J1', 'name': '질문의 요청 충족'}], 'records': records}
    run.update(status='COMPLETED', phase='EVALUATE', eval_done=1, eval_total=1)
    run['evaluations']['aiona'] = batch
    if gpt:
        run['providers'].append('gpt')
        run['models']['gpt'] = 'gpt-ui-fixture'
        run['datasets']['gpt'] = [{**c, 'collection': {'status': 'SUCCESS'}} for c in copy.deepcopy(run['datasets']['aiona'])]
        run['progress']['gpt'] = {'success': len(cases), 'done': len(cases), 'total': len(cases), 'failed': 0, 'incomplete': 0}
        run['evaluations']['gpt'] = copy.deepcopy(batch)
        run['evaluations']['gpt']['jev_eval']['records'] = [semantic_record('API_ERROR', error=LIMIT_ERROR) for _ in cases]
        run.update(eval_done=2, eval_total=2)
    comparison._save(run)
    return run_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--saved-run', type=Path, help='Optional read-only replay of the 65d50fe3b23d regression baseline')
    args = parser.parse_args()
    source_bytes = args.saved_run.read_bytes() if args.saved_run else None
    screenshots = ROOT / 'results' / 'ui-preview'
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='aiona-result-ui-') as temporary:
        path = Path(temporary)
        evaluator = CodeEvalService(save_dir=path / 'eval', jev_resolver=lambda **_: resolve_jev_client(environment={}, env_path=path / '.no-env'))
        comparison = ComparisonService(evaluator, save_dir=path / 'compare')
        # Q100 remains an independent benchmark; the workspace default is Q165.
        cases = json.loads((ROOT / 'data/code_eval/AIONA_winz_set2_Q100_id_question_answer_v1.json').read_text(encoding='utf-8'))
        q100 = seed(comparison, cases, 'UI 검증용 Q100 · 의미 호출 실패 대역', [semantic_record('API_ERROR', error=LIMIT_ERROR) for _ in cases])
        mixed_cases = [cases[0], cases[2], cases[0], cases[8]]  # duplicate IDs, known failure, unsupported condition
        mixed = seed(comparison, mixed_cases, 'UI 검증용 · 완료와 품질 분리', [semantic_record(), semantic_record(), semantic_record(decision='FAIL'), semantic_record()], gpt=True)
        complete = seed(comparison, [cases[0], cases[2]], 'UI 검증용 · 완료된 평가에 문제 있음', [semantic_record(), semantic_record()])
        unverified = semantic_record(decision='FAIL')
        unverified['criteria'].append('J5')
        unverified['decisions']['J5'] = {'decision': None, 'status': 'UNVERIFIED', 'reason': '검증용 출처 확인 필요'}
        overlap = seed(comparison, [cases[0], cases[0]], 'UI 검증용 · FAIL과 미완료 배타 집계', [unverified, semantic_record(decision='FAIL')], gpt=True)
        overlap_run = comparison._runs[overlap]
        overlap_run['evaluations']['gpt']['jev_eval']['records'] = [semantic_record(), {**copy.deepcopy(unverified), 'decisions': {
            'J1': semantic_record()['decisions']['J1'], 'J5': unverified['decisions']['J5']}}]
        comparison._save(overlap_run)
        reason_records = []
        for i in (0, 1, 2, 3, 4, 4):
            record = semantic_record()
            record['decisions']['J1'].update(decision=None, status='UNVERIFIED', reason=f'검증용 서로 다른 사유 {i}')
            reason_records.append(record)
        many_reasons = seed(comparison, [cases[0]] * len(reason_records), 'UI 검증용 · 보류 사유 접기', reason_records)
        criterion_records = [semantic_record(), semantic_record(decision='FAIL'), copy.deepcopy(reason_records[0]),
                             semantic_record('INVALID_RESPONSE', error='검증용 JSON 응답 오류'),
                             {**semantic_record(), 'decisions': {}}]
        criterion_reasons = seed(comparison, [cases[0]] * 5, 'UI 검증용 · 기준별 미판정 원인', criterion_records)
        if source_bytes is not None:
            saved_run = json.loads(source_bytes)
            assert saved_run['run_id'] == '65d50fe3b23d', 'This saved-result expectation is for the specified regression baseline only.'
            comparison._runs[saved_run['run_id']] = copy.deepcopy(saved_run)
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=comparison)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{server.server_port}/workspace.html')

                def open_run(run_id):
                    page.evaluate('(id) => sessionStorage.setItem("aiona-comparison-run", id)', run_id)
                    page.goto(f'http://127.0.0.1:{server.server_port}/workspace.html#results')
                    page.reload()
                    expect(page.locator('#result-content')).to_be_visible()

                open_run(q100)
                expect(page.locator('#progress-title')).to_have_text('평가 미완료')
                expect(page.locator('#run-progress')).to_have_attribute('value', '200')
                expect(page.locator('#run-progress')).to_have_attribute('max', '200')
                card = page.locator('.result-model-card[data-provider=aiona]')
                expect(card.locator('.outcome-count.issue strong')).to_have_text('2')
                expect(card.locator('.outcome-count.clear strong')).to_have_text('0')
                expect(card.locator('.outcome-count.pending strong')).to_have_text('98')
                expect(card).not_to_contain_text('중복될 수 있습니다')
                expect(card.locator('.evaluation-warning').filter(has_text='미완료 사유')).to_contain_text('사용 한도')
                expect(card.locator('.evaluation-metrics')).to_contain_text('미확정')
                expect(page.locator('#matrix-body .pill').first).to_have_text('평가 미완료')
                card.locator('.outcome-count.issue').click()
                expect(page.locator('#matrix-body tr')).to_have_count(2)
                expect(page.locator('#matrix-body tr').nth(0).locator('td').first).to_have_text('Q003')
                expect(page.locator('#matrix-body tr').nth(1).locator('td').first).to_have_text('Q087')
                page.locator('#matrix-body .result-cell').first.click()
                expect(page.locator('#answer-evaluation')).to_contain_text('남은 평가 있음')
                expect(page.locator('#answer-evaluation .finding-list li')).to_have_count(1)
                page.locator('#answer-evaluation summary').click()
                expect(page.locator('#answer-evaluation pre')).to_have_text(LIMIT_ERROR)
                assert page.locator('#original-answer pre').text_content() == cases[2]['answer']
                page.locator('#answer-dialog [data-close]').click()
                card.locator('.outcome-count.pending').click()
                expect(page.locator('#matrix-caption')).to_contain_text('98개')
                card.locator('.outcome-count.clear').click()
                expect(page.locator('#matrix-body')).to_contain_text('해당하는 질문이 없습니다')
                page.locator('#clear-matrix-filter').click()
                page.locator('#matrix-search').fill('Q009')
                page.locator('#matrix-body .result-cell').click()
                expect(page.locator('#answer-evaluation')).to_contain_text('일부 판정 불가')
                expect(page.locator('#answer-evaluation .finding-list')).to_contain_text('답변 일부')
                page.locator('#answer-dialog [data-close]').click()
                page.locator('#clear-matrix-filter').click()
                page.evaluate('document.activeElement.blur(); window.scrollTo(0, 0)')
                page.screenshot(path=str(screenshots / 'workspace-q100-incomplete-test.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert page.locator('.matrix-table').evaluate('(table) => table.scrollWidth <= table.clientWidth')
                page.screenshot(path=str(screenshots / 'workspace-q100-incomplete-mobile-test.png'), full_page=True)

                page.set_viewport_size({'width': 1440, 'height': 1080})
                open_run(mixed)
                expect(page.locator('#run-progress')).to_have_attribute('value', '16')
                expect(page.locator('#run-progress')).to_have_attribute('max', '16')
                expect(page.locator('#progress-title')).to_have_text('평가 미완료')
                aiona = page.locator('.result-model-card[data-provider=aiona]')
                expect(aiona.locator('.outcome-count.issue strong')).to_have_text('2')
                expect(aiona.locator('.outcome-count.clear strong')).to_have_text('1')
                expect(aiona.locator('.outcome-count.pending strong')).to_have_text('1')
                aiona.locator('.outcome-count.clear').click()
                expect(page.locator('#matrix-body tr')).to_have_count(1)
                page.locator('#matrix-body .result-cell').nth(1).click()
                expect(page.get_by_role('tab', name='GPT', exact=True)).to_have_attribute('aria-selected', 'true')
                expect(page.locator('#answer-evaluation')).to_contain_text('평가 미완료')
                page.get_by_role('tab', name='AIONA', exact=True).click()
                expect(page.locator('#answer-evaluation')).to_contain_text('확인된 문제 없음')
                page.locator('#answer-dialog [data-close]').click()
                aiona.locator('.outcome-count.issue').click()
                expect(page.locator('#matrix-body tr')).to_have_count(2)
                page.locator('#matrix-body tr').nth(1).locator('.result-cell').first.click()
                expect(page.locator('#answer-evaluation .finding-list')).to_contain_text('UI 검증용 의미 판정 사유')
                page.locator('#answer-dialog [data-close]').click()
                page.locator('#clear-matrix-filter').click()
                page.evaluate('document.activeElement.blur(); window.scrollTo(0, 0)')
                page.screenshot(path=str(screenshots / 'workspace-outcomes-test.png'), full_page=True)

                open_run(complete)
                expect(page.locator('#progress-title')).to_have_text('평가 완료')
                expect(page.locator('#run-progress')).to_have_attribute('value', '4')
                expect(page.locator('.outcome-count.issue strong')).to_have_text('1')
                expect(page.locator('.outcome-count.clear strong')).to_have_text('1')

                open_run(overlap)
                expect(page.locator('#progress-title')).to_have_text('평가 미완료')
                expect(page.locator('#final-summary')).to_contain_text('미확정')
                expect(page.locator('#final-summary')).to_contain_text('1개 답변이 미완료입니다.')
                expect(page.locator('#final-summary')).not_to_contain_text('두 항목에 포함')
                aiona = page.locator('.result-model-card[data-provider=aiona]')
                expect(aiona.locator('.outcome-count.issue strong')).to_have_text('2')
                expect(aiona.locator('.outcome-count.pending strong')).to_have_text('0')
                aiona.locator('.outcome-count.pending').click()
                expect(page.locator('#matrix-body')).to_contain_text('해당하는 질문이 없습니다')
                page.locator('#matrix-model').select_option('')
                expect(page.locator('#matrix-body')).to_contain_text('해당하는 질문이 없습니다')
                page.locator('#matrix-filter').select_option('issue')
                expect(page.locator('#matrix-body tr')).to_have_count(2)
                page.locator('#matrix-body .result-cell').first.click()
                expect(page.locator('#answer-evaluation')).to_contain_text('검증용 출처 확인 필요')
                expect(page.locator('#answer-evaluation')).to_contain_text('남은 평가 있음')
                page.locator('#answer-dialog [data-close]').click()
                page.locator('.result-model-card[data-provider=gpt] .outcome-count.pending').click()
                expect(page.locator('#matrix-body tr')).to_have_count(1)
                open_run(many_reasons)
                warning = page.locator('.result-model-card[data-provider=aiona] .evaluation-warning').first
                expect(warning.locator('strong')).to_have_text('미완료 사유 · 6개 질문')
                expect(warning.locator(':scope > div > p')).to_have_count(1)
                expect(warning.locator(':scope > div > p').first).to_have_text('J1 · 검증 근거 부족 (6개)')
                expect(warning.locator('details summary')).to_have_text('상세 사유 보기')
                expect(warning.locator('details p').first).not_to_be_visible()
                warning.locator('details summary').click()
                expect(warning.locator('details p').first).to_be_visible()
                expect(warning.locator('details')).to_contain_text('사유 2 (1개)')
                expect(warning.locator('details')).to_contain_text('사유 3 (1개)')
                with page.expect_response(lambda response: response.url.endswith('/result')):
                    page.locator('#refresh-results').click()
                expect(warning.locator('details p').first).to_be_visible()
                warning.locator('details summary').click()
                expect(warning.locator('details p').first).not_to_be_visible()
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                open_run(criterion_reasons)
                cell = page.locator('#criteria-body [data-criterion=J1] [data-provider=aiona]')
                expect(cell.locator('.criterion-rate')).to_have_text('미확정')
                expect(cell).to_contain_text('1 PASS · 1 FAIL')
                expect(cell).to_contain_text('질문 5개 대상 · 미판정 3')
                expect(cell).to_contain_text('근거 확인 필요 1 · 평가 오류 1 · 판정 누락 1')
                expect(page.locator('#criteria-body [data-criterion="1"]')).not_to_contain_text('판정 누락')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.set_viewport_size({'width': 1440, 'height': 1080})
                expect(cell).to_contain_text('근거 확인 필요 1 · 평가 오류 1 · 판정 누락 1')
                page.locator('#criteria-title').scroll_into_view_if_needed()
                page.screenshot(path=str(screenshots / 'workspace-criterion-pending-reasons-test.png'), full_page=True)
                page.locator('section[aria-labelledby="criteria-title"]').screenshot(path=str(screenshots / 'workspace-criterion-pending-reasons-table-test.png'))
                if source_bytes is not None:
                    open_run(saved_run['run_id'])
                    expected = [('gemini', 'J5', '미판정 31', '근거 확인 필요 30 · 평가 오류 1'),
                                ('gpt', 'J5', '미판정 27', '근거 확인 필요 26 · 평가 오류 1'),
                                ('gpt', 'J1', '미판정 1', '평가 오류 1'),
                                ('gemini', 'J1', '미판정 1', '평가 오류 1'),
                                ('aiona', 'J6', '미판정 5', '근거 확인 필요 5')]
                    observed = []
                    for provider, criterion, pending, reason in expected:
                        actual_cell = page.locator(f'#criteria-body [data-criterion={criterion}] [data-provider={provider}]')
                        expect(actual_cell).to_contain_text(pending)
                        expect(actual_cell).to_contain_text(reason)
                        observed.append({'provider': provider, 'criterion': criterion, 'text': actual_cell.inner_text()})
                    table = page.locator('section[aria-labelledby="criteria-title"]')
                    table.scroll_into_view_if_needed()
                    table.screenshot(path=str(screenshots / 'workspace-criterion-pending-reasons-actual-test.png'))
                    assert args.saved_run.read_bytes() == source_bytes
                    (screenshots / 'workspace-criterion-pending-reasons-actual-check.json').write_text(json.dumps({
                        'run_id': saved_run['run_id'], 'source_unchanged': True,
                        'sha256': hashlib.sha256(source_bytes).hexdigest(), 'observed': observed,
                        'scope': 'Read-only saved result copied into an isolated temporary server; no evaluation calls.'},
                        ensure_ascii=False, indent=2), encoding='utf-8')
                assert not errors, errors
                browser.close()
            print('Result UI passed: Q100 2 issues / 98 pending, exclusive FAIL/pending counts, preserved unfinished criterion details, unsettled rates, concise reasons with collapsed originals, modal selection and mobile layout.')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
            if source_bytes is not None:
                assert args.saved_run.read_bytes() == source_bytes


if __name__ == '__main__':
    main()
