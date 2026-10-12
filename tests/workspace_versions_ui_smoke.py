"""Version UI on isolated saved-data copies; no provider or judge calls."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from playwright.sync_api import expect, sync_playwright
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server, Handler


def expect_normal_mode(page):
    for field in ('aiona-version', 'aiona-collected-at', 'aiona-release-notes'):
        expect(page.locator('#' + field)).to_be_hidden()
        expect(page.locator('#' + field)).to_be_disabled()
        expect(page.locator('#' + field).locator('xpath=ancestor::*[@data-version-field][1]')).to_be_hidden()
    expect(page.locator('#collect-button')).to_be_visible()
    expect(page.locator('#version-evaluate-button')).to_be_hidden()


def main():
    Handler.log_message = lambda *args: None
    saved = ROOT / 'backups/run-history/20261012-105105/comparison_runs/a3b5d89bc041.json'
    original = saved.read_bytes()
    baseline = json.loads(original)
    baseline['aiona_release'] = {'version': '10.1', 'collected_at': None, 'notes': 'UI fixture'}
    errors, posts = [], []
    output = ROOT / 'results/version_review'
    output.mkdir(exist_ok=True)
    def forbidden(*args, **kwargs):
        raise AssertionError('External model call forbidden during UI verification')
    with tempfile.TemporaryDirectory(prefix='aiona-versions-ui-') as directory:
        evaluator = CodeEvalService(save_dir=Path(directory) / 'eval', jev_resolver=forbidden)
        service = ComparisonService(evaluator, save_dir=Path(directory) / 'runs', client_factory=forbidden)
        service._runs[baseline['run_id']] = copy.deepcopy(baseline)
        service.version_registry.data = {'baseline_run_id': baseline['run_id']}
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.add_init_script("addEventListener('DOMContentLoaded', () => { document.getElementById('version-comparison-details').open = true; });")
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('request', lambda r: posts.append(r.url) if r.method != 'GET' else None)
                setup_available = True
                def mock_setup(route):
                    response = route.fetch()
                    info = response.json()
                    info['semantic_setup'] = {'can_evaluate': setup_available, 'engine': 'ui_fixture',
                                              'reason': 'UI fixture configuration unavailable'}
                    route.fulfill(response=response, json=info)
                page.route('**/api/code-eval/info', mock_setup)
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base + '/workspace.html#setup')
                expect_normal_mode(page)
                page.locator('.advanced-options > summary').click()
                expect_normal_mode(page)
                page.locator('.advanced-options > summary').click()
                page.goto(base + '/workspace.html#comparison')
                expect(page.locator('#view-comparison')).to_be_visible()
                expect(page.locator('#version-baseline-run')).to_have_value('')
                expect(page.locator('#version-target-run')).to_have_value('')
                page.locator('#version-baseline-model').select_option(baseline['run_id'])
                expect(page.locator('#version-baseline-run')).to_have_value(baseline['run_id'])
                expect(page.locator('#version-score-cards')).to_contain_text('96.4')
                expect(page.locator('#view-comparison')).to_contain_text('새 버전 답변')
                assert page.locator('#version-target-run').input_value() == ''
                expect(page.locator('[data-version-side="baseline"] [data-evidence-notice]')).to_contain_text('웹 검색을 사용하지 않은 당시 평가 결과')
                expect(page.locator('[data-version-side="baseline"] [data-evidence-notice]')).to_contain_text('근거가 저장되어 있지 않습니다')
                page.reload()
                expect(page.locator('#version-baseline-run')).to_have_value(baseline['run_id'])
                expect(page.locator('#version-summary')).to_have_attribute('aria-busy', 'false')
                page.screenshot(path=str(output / 'versions-10.2-pending-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.wait_for_function("""() => {
                    const nav = document.querySelector('.sidebar nav').getBoundingClientRect();
                    const tab = document.querySelector('[data-view="results"]').getBoundingClientRect();
                    return tab.left >= nav.left - 1 && tab.right <= nav.right + 1;
                }""")
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'versions-10.2-pending-mobile.png'), full_page=True)
                page.set_viewport_size({'width': 1440, 'height': 1080})

                # Synthetic changes exist only in this temporary service, labelled as fixtures.
                target = copy.deepcopy(baseline)
                target.update(run_id='fffffffffff1', name='UI 검증용 10.2 — 실제 모델 결과 아님',
                              aiona_release={'version': '10.2', 'collected_at': None, 'notes': 'UI fixture only'},
                              providers=['aiona'])
                target['datasets'] = {'aiona': target['datasets']['aiona']}
                target['evaluations'] = {'aiona': target['evaluations']['aiona']}
                target['evaluations']['aiona']['jev_eval']['records'][38]['decisions']['J3']['decision'] = 'PASS'
                target['evaluations']['aiona']['jev_eval']['records'][0]['decisions']['J1']['decision'] = 'FAIL'
                answer = 'UI 검증용 수정 답변\n<script>window.versionUnsafe = true</script>'
                target['datasets']['aiona'][38]['answer'] = answer
                target['evaluations']['aiona']['cases'][38]['answer'] = answer
                service._runs[target['run_id']] = target
                mismatch = copy.deepcopy(target)
                mismatch.update(run_id='fffffffffff2', name='UI 검증용 평가 조건 불일치')
                mismatch['evaluations']['aiona']['jev_eval']['version'] = 'fixture-different-version'
                service._runs[mismatch['run_id']] = mismatch
                pending = copy.deepcopy(target)
                pending.update(run_id='fffffffffff3', name='UI 검증용 미완료')
                pending['evaluations']['aiona']['jev_eval']['records'][0] = {
                    'status': 'API_ERROR', 'criteria': ['J1'], 'decisions': None, 'error': 'UI fixture'}
                service._runs[pending['run_id']] = pending

                page.locator('#comparison-refresh').click()
                expect(page.locator('#version-target-model option[value="fffffffffff1"]')).to_have_count(1)
                expect(page.locator('#version-target-model option[value="fffffffffff2"]')).to_have_count(1)
                expect(page.locator('#version-target-model option[value="fffffffffff3"]')).to_have_count(1)
                # Multiple saved results remain explicit options within the model choice.
                expect(page.locator('#version-target-run')).to_have_value('')
                page.locator('#version-target-model').select_option(target['run_id'])
                expect(page.locator('#version-target-run')).to_have_value(target['run_id'])
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'true')
                expect(page.locator('#version-score-cards')).to_contain_text('96.4')
                expect(page.locator('#version-rows tr')).to_have_count(165)
                page.screenshot(path=str(output / 'versions-comparison-fixture-desktop.png'), full_page=True)
                page.locator('#version-change-filter').select_option('improved')
                expect(page.locator('#version-rows tr')).to_have_count(1)
                expect(page.locator('#version-rows')).to_contain_text('Q039')
                page.locator('#version-rows button').first.click()
                expect(page.locator('#version-answer-dialog')).to_be_visible()
                expect(page.locator('#version-answer-columns')).to_contain_text(answer)
                assert page.locator('#version-answer-columns summary').filter(has_text='통과 판정 근거').count() > 0
                assert page.evaluate('window.versionUnsafe') is None
                page.screenshot(path=str(output / 'versions-answer-fixture-desktop.png'), full_page=True)
                page.keyboard.press('Escape')
                expect(page.locator('#version-answer-dialog')).to_be_hidden()
                page.locator('#version-change-filter').select_option('regressed')
                expect(page.locator('#version-rows')).to_contain_text('Q001')
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'versions-comparison-fixture-mobile.png'), full_page=True)
                page.set_viewport_size({'width': 1440, 'height': 1080})
                page.locator('#version-target-model').select_option(mismatch['run_id'])
                expect(page.locator('#version-reasons')).to_contain_text('평가')
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'false')
                page.locator('#comparison-refresh').click()
                expect(page.locator('#version-target-run')).to_have_value(mismatch['run_id'])
                expect(page.locator('#comparison-refresh')).to_be_enabled()
                expect(page.locator('#version-summary')).to_have_attribute('aria-busy', 'false')
                page.reload()
                expect(page.locator('#version-baseline-run')).to_have_value(baseline['run_id'])
                expect(page.locator('#version-target-run')).to_have_value(mismatch['run_id'])
                expect(page.locator('#version-reasons')).to_contain_text('평가')
                page.locator('#version-target-model').select_option(pending['run_id'])
                expect(page.locator('[data-version-side="target"] .summary-score strong')).to_have_text('—')
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'false')
                j1 = page.locator('#version-criteria-body tr').filter(has=page.get_by_text('의도 충족성', exact=True))
                expect(j1.locator('td').nth(1)).to_contain_text('확인 필요 1')
                expect(j1.locator('td').nth(1)).to_contain_text('완료 판정')
                expect(j1.locator('td').nth(2)).to_contain_text('—')
                page.locator('#version-target-model').select_option('')
                expect(page.locator('[data-version-side="target"]')).to_have_count(0)
                page.locator('#comparison-refresh').click()
                expect(page.locator('#version-target-run')).to_have_value('')
                expect(page.locator('#version-summary')).to_have_attribute('aria-busy', 'false')
                expect(page.locator('[data-version-side="target"]')).to_have_count(0)
                assert not posts
                page.evaluate("AionaWorkspace.prepareVersion('')")
                expect(page.locator('#view-setup')).to_be_visible()
                expect(page.locator('#aiona-version')).to_have_value('')
                page.locator('#aiona-version').fill('10.2')
                expect(page.locator('#aiona-version')).to_be_visible()
                expect(page.locator('#aiona-version')).to_be_enabled()
                expect(page.locator('#version-evaluate-button')).to_be_visible()
                expect(page.locator('#version-evaluate-button')).to_be_disabled()
                page.locator('[data-view="setup"]').click()
                expect_normal_mode(page)
                page.locator('.advanced-options > summary').click()
                expect_normal_mode(page)
                page.locator('.advanced-options > summary').click()
                page.evaluate("AionaWorkspace.go('comparison')")
                page.evaluate("AionaWorkspace.prepareVersion('')")
                expect(page.locator('#aiona-version')).to_have_value('')
                page.locator('#aiona-version').fill('10.2')
                expect(page.locator('#aiona-version')).to_be_visible()
                expect(page.locator('#aiona-version')).to_be_enabled()
                assert not posts and not errors, (posts, errors)
                upload = [{'case_id': 'UI-ONLY-1', 'question': '한 문장으로 답해 주세요.\r\n',
                           'answer': 'UI 흐름만 확인하는 모의 답변입니다.\r\n'}]
                page.locator('#dataset-file').set_input_files({
                    'name': 'ui-fixture-10.2.json', 'mimeType': 'application/json',
                    'buffer': json.dumps(upload, ensure_ascii=False).encode('utf-8')})
                page.locator('.advanced-options > summary').click()
                for field in ('aiona-collected-at', 'aiona-release-notes'):
                    expect(page.locator('#' + field)).to_be_visible()
                    expect(page.locator('#' + field)).to_be_enabled()
                page.locator('#aiona-collected-at').fill('2026-10-09')
                page.locator('#aiona-release-notes').fill('브라우저 검증용')
                expect(page.locator('#version-evaluate-button')).to_be_enabled()
                submitted = []
                def capture_submission(route):
                    submitted.append(route.request.post_data_json)
                    route.fulfill(status=202, content_type='application/json',
                                  body=json.dumps(service.status(target['run_id'])))
                page.route('**/api/comparison/version-runs', capture_submission)
                page.locator('#version-evaluate-button').click()
                expect(page.locator('#view-results')).to_be_visible()
                expect(page.locator('#result-subtitle')).to_contain_text(target['name'])
                assert len(submitted) == 1
                assert submitted[0]['aiona_release'] == {
                    'version': '10.2', 'collected_at': '2026-10-09', 'notes': '브라우저 검증용'}
                assert submitted[0]['cases'][0]['question'] == upload[0]['question']
                assert submitted[0]['cases'][0]['answer'] == upload[0]['answer']
                page.evaluate("AionaWorkspace.go('comparison')")
                expect(page.locator('#version-target-run')).to_have_value(target['run_id'])
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'true')
                page.unroute('**/api/comparison/version-runs', capture_submission)

                # Re-evaluating either side selects its new run, retaining the opposite side.
                reevaluations = {}
                for source, run_id in [(baseline, 'fffffffffff4'), (target, 'fffffffffff5')]:
                    updated = copy.deepcopy(source)
                    updated.update(run_id=run_id, source_run_id=source['run_id'], name='UI fixture re-evaluation')
                    if source is baseline:
                        updated['aiona_release'] = service.result(baseline['run_id'])['aiona_release']
                    service._runs[run_id] = updated
                    reevaluations[source['run_id']] = run_id
                reeval_submissions = []
                def capture_reevaluation(route):
                    payload = route.request.post_data_json
                    reeval_submissions.append(payload)
                    route.fulfill(status=202, content_type='application/json',
                                  body=json.dumps(service.status(reevaluations[payload['source_run_id']])))
                page.route('**/api/comparison/version-runs', capture_reevaluation)
                page.locator('#version-profile > summary').click()
                page.locator('#version-profile-content button').nth(0).click()
                expect(page.locator('#view-results')).to_be_visible()
                page.evaluate("AionaWorkspace.go('comparison')")
                expect(page.locator('#version-baseline-run')).to_have_value('fffffffffff4')
                expect(page.locator('#version-target-run')).to_have_value(target['run_id'])
                expect(page.locator('#version-summary')).to_have_attribute('aria-busy', 'false')
                page.locator('#version-profile-content button').nth(1).click()
                expect(page.locator('#view-results')).to_be_visible()
                page.evaluate("AionaWorkspace.go('comparison')")
                expect(page.locator('#version-baseline-run')).to_have_value('fffffffffff4')
                expect(page.locator('#version-target-run')).to_have_value('fffffffffff5')
                expect(page.locator('#version-summary')).to_have_attribute('aria-busy', 'false')
                assert [p['source_run_id'] for p in reeval_submissions] == [baseline['run_id'], target['run_id']]
                assert service._runs[baseline['run_id']] == baseline
                assert service._runs[target['run_id']] == target

                setup_available = False
                page.reload()
                expect(page.locator('#version-summary')).to_have_attribute('aria-busy', 'false')
                page.locator('#version-profile > summary').click()
                expect(page.locator('#version-profile-content button').nth(0)).to_be_disabled()
                expect(page.locator('#version-profile-content button').nth(1)).to_be_disabled()
                expect(page.locator('#version-profile-content')).to_contain_text('평가자 설정을 확인해야')
                expect(page.locator('#version-score-cards')).to_contain_text('96.4')
                # An explicitly created target is restored, but a baseline must still
                # be chosen by the user, including across a reload of the page.
                fresh = browser.new_page(viewport={'width': 1440, 'height': 1080})
                fresh.add_init_script("addEventListener('DOMContentLoaded', () => { document.getElementById('version-comparison-details').open = true; });")
                fresh.on('pageerror', lambda e: errors.append(str(e)))
                fresh.goto(base + '/workspace.html#setup')
                fresh.evaluate("""runId => window.dispatchEvent(new CustomEvent(
                    'aiona:version-created', {detail: {runId}}))""", target['run_id'])
                fresh.goto(base + '/workspace.html#comparison')
                expect(fresh.locator('#version-summary')).to_have_attribute('data-ready', 'false')
                fresh.reload()
                expect(fresh.locator('#version-baseline-run')).to_have_value('')
                expect(fresh.locator('#version-target-run')).to_have_value(target['run_id'])
                fresh.locator('#version-baseline-model').select_option(baseline['run_id'])
                expect(fresh.locator('#version-summary')).to_have_attribute('data-ready', 'true')
                fresh.evaluate("""runId => sessionStorage.setItem('aiona-version-comparison',
                    JSON.stringify({baseline: runId}))""", 'fffffffffff4')
                fresh.reload()
                expect(fresh.locator('#version-baseline-run')).to_have_value('fffffffffff4')
                expect(fresh.locator('#version-target-run')).to_have_value('')
                expect(fresh.locator('#version-summary')).to_have_attribute('data-ready', 'false')
                fresh.close()
                assert all(url.endswith(('/api/comparison/dataset', '/api/comparison/version-runs')) for url in posts)
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    assert hashlib.sha256(saved.read_bytes()).digest() == hashlib.sha256(original).digest()
    print('PASS: existing 10.1, pending 10.2, same-condition comparison, improved/regressed drilldown, '
          'literal answers, mismatched conditions, partial denominators, legacy evidence notice, '
          'registration and re-evaluation selections, unavailable evaluator, opt-in upload, desktop/mobile; '
          'no API/CLI generation, no saved data changes. Comparison screenshots are synthetic fixtures.')


if __name__ == '__main__':
    main()
