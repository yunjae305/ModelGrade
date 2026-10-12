"""Two-selector comparison regression; isolated saved records and mocked judges only."""
import copy
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from playwright.sync_api import sync_playwright, expect
from test_same_question_versions import baseline
from test_comparison_workspace import wait
from webapp.server import Server, Handler


def assert_two_selectors(page):
    expect(page.locator('.version-selectors select:visible')).to_have_count(2)
    for side in ('baseline', 'target'):
        expect(page.locator(f'#version-{side}-model')).to_be_visible()
        expect(page.locator(f'#version-{side}-run')).to_be_hidden()
        expect(page.locator(f'#version-{side}-version')).to_be_hidden()


def main():
    Handler.log_message = lambda *args: None
    output = ROOT / 'results' / 'two-model-selection-ui-review'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='aiona-two-model-ui-') as directory:
        service, judge, sent, source = baseline(Path(directory))
        rows = [{**row, 'answer': '새 답변 ' + row['case_id']}
                for row in service.version_template(source['run_id'])['cases']]
        target = wait(service, service.start_version_run({
            'baseline_run_id': source['run_id'], 'cases': rows,
            'aiona_release': {'version': 'v2'}, 'name': '첫 번째 v2 평가',
        })['run_id'])
        service.manage_release({'version': 'v3', 'set_current': False})
        source_path = service.save_dir / (source['run_id'] + '.json')
        target_path = service.save_dir / (target['run_id'] + '.json')
        source_bytes, target_bytes = source_path.read_bytes(), target_path.read_bytes()
        calls = len(judge.calls)
        errors, posts = [], []
        server = Server(('127.0.0.1', 0), code_eval_service=service.evaluator,
                        comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: posts.append(request.url)
                        if request.method != 'GET' else None)

                def setup(route):
                    response = route.fetch()
                    info = response.json()
                    info['semantic_setup'] = {'can_evaluate': True, 'engine_label': '검증용 모의 평가자'}
                    route.fulfill(response=response, json=info)

                page.route('**/api/code-eval/info', setup)
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base + '/workspace.html#comparison')
                assert_two_selectors(page)
                expect(page.locator('#comparison-refresh')).to_be_enabled()
                expect(page.locator('#version-baseline-model')).to_have_value('')
                expect(page.locator('#version-target-model')).to_have_value('')

                # Each version with one saved evaluation is one direct model choice.
                expect(page.locator(f'#version-baseline-model option[value="{source["run_id"]}"]')).to_have_text('AIONA v1')
                expect(page.locator(f'#version-target-model option[value="{target["run_id"]}"]')).to_have_text('AIONA v2')
                page.locator('#version-baseline-model').select_option(source['run_id'])
                expect(page.locator('#version-baseline-run')).to_have_value(source['run_id'])
                expect(page.locator('#version-baseline-version')).to_have_value('v1')
                page.locator('#version-target-model').select_option(target['run_id'])
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'true')
                expect(page.locator('#version-target-run')).to_have_value(target['run_id'])
                expect(page.locator('#version-target-version')).to_have_value('v2')
                expect(page.locator('#version-summary-title')).to_have_text('AIONA v2의 통과 문항 수는 기준 버전과 같습니다.')
                expect(page.locator('[data-version-side="baseline"] h3')).to_have_text('AIONA v1')
                expect(page.locator('[data-version-side="target"] h3')).to_have_text('AIONA v2')
                expect(page.locator('#version-summary')).to_have_attribute('data-trend', 'unchanged')
                expect(page.locator('#version-delta .vc-delta-value')).to_have_text('0.0')
                page.screenshot(path=str(output / 'two-models-desktop.png'), full_page=True)

                # Navigating and reloading retain the actual evaluated records.
                page.locator('[data-view="versions"]').click()
                page.evaluate("AionaWorkspace.go('comparison')")
                expect(page.locator('#version-baseline-model')).to_have_value(source['run_id'])
                expect(page.locator('#version-target-model')).to_have_value(target['run_id'])
                page.reload()
                expect(page.locator('#version-baseline-model')).to_have_value(source['run_id'])
                expect(page.locator('#version-target-model')).to_have_value(target['run_id'])
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'true')

                # Registered models without evaluations remain actionable without inventing results.
                page.locator('#version-target-model').select_option('version:v3')
                expect(page.locator('#version-target-run')).to_have_value('')
                expect(page.locator('#version-target-version')).to_have_value('v3')
                expect(page.locator('#version-empty-target')).to_be_visible()
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'false')
                expect(page.locator('#version-add-answers')).to_be_enabled()
                page.locator('#version-add-answers').click()
                expect(page.locator('#version-answers-dialog')).to_be_visible()
                expect(page.locator('#version-answer-release')).to_have_value('v3')
                expect(page.locator('#version-answer-submit')).to_be_disabled()
                page.keyboard.press('Escape')
                page.reload()
                expect(page.locator('#version-target-model')).to_have_value('version:v3')
                expect(page.locator('#version-empty-target')).to_be_visible()

                # Multiple evaluations stay explicit within the same model selector.
                duplicate = copy.deepcopy(target)
                duplicate.update(run_id='abcdef123450', name='두 번째 v2 평가',
                                 created_at='2026-10-12T11:00:00+09:00')
                mismatch = copy.deepcopy(target)
                mismatch.update(run_id='abcdef123451', name='평가 조건이 다른 v2 기록',
                                created_at='2026-10-12T12:00:00+09:00')
                mismatch['evaluations']['aiona']['jev_eval']['version'] = 'different-conditions'
                service._runs[duplicate['run_id']] = duplicate
                service._runs[mismatch['run_id']] = mismatch
                page.locator('#version-target-model').select_option('')
                page.locator('#comparison-refresh').click()
                expect(page.locator('#comparison-refresh')).to_be_enabled()
                expect(page.locator('#version-target-model')).to_have_value('')
                expect(page.locator('#version-target-run')).to_have_value('')
                group = page.locator('#version-target-model optgroup[label^="AIONA v2"]')
                expect(group.locator('option:not([disabled])')).to_have_count(3)
                for run in (target, duplicate, mismatch):
                    option = group.locator(f'option[value="{run["run_id"]}"]')
                    expect(option).to_contain_text(run['name'])
                    assert '2026.' in option.text_content()
                page.locator('#version-target-model').select_option(duplicate['run_id'])
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'true')
                expect(page.locator('[data-version-side="target"]')).to_contain_text(duplicate['name'])
                page.locator('#version-target-model').select_option(mismatch['run_id'])
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'false')
                expect(page.locator('#version-summary-title')).to_have_text('전체 개선 폭을 아직 확정할 수 없습니다.')
                expect(page.locator('#version-summary')).to_have_attribute('data-trend', 'unresolved')
                expect(page.locator('#version-delta .vc-delta-value')).to_have_text('—')
                expect(page.locator('#version-reasons')).to_be_visible()
                expect(page.locator('#version-reasons')).to_contain_text('평가')

                # Session records from the former four-field UI still restore exact runs.
                page.evaluate('''selection => sessionStorage.setItem(
                    'aiona-version-comparison', JSON.stringify(selection))''',
                    {'baseline': source['run_id'], 'target': target['run_id'],
                     'baselineVersion': 'v1', 'targetVersion': 'v2'})
                page.reload()
                expect(page.locator('#version-baseline-model')).to_have_value(source['run_id'])
                expect(page.locator('#version-target-model')).to_have_value(target['run_id'])
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'true')
                page.evaluate('''selection => sessionStorage.setItem(
                    'aiona-version-comparison', JSON.stringify(selection))''',
                    {'baseline': source['run_id'], 'target': '', 'targetVersion': 'v2'})
                page.reload()
                expect(page.locator('#version-baseline-model')).to_have_value(source['run_id'])
                expect(page.locator('#version-target-model')).to_have_value('version:v2')
                expect(page.locator('#version-target-run')).to_have_value('')
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'false')
                page.evaluate('''selection => sessionStorage.setItem(
                    'aiona-version-comparison', JSON.stringify(selection))''',
                    {'baseline': source['run_id']})
                page.reload()
                expect(page.locator('#version-baseline-model')).to_have_value(source['run_id'])
                expect(page.locator('#version-target-model')).to_have_value('')
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'false')
                page.locator('#version-target-model').select_option(target['run_id'])
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'true')
                page.set_viewport_size({'width': 390, 'height': 844})
                assert_two_selectors(page)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'two-models-mobile.png'), full_page=True)

                # A removed saved selection must not silently become the only remaining run.
                del service._runs[duplicate['run_id']]
                del service._runs[mismatch['run_id']]
                page.evaluate('''selection => sessionStorage.setItem(
                    'aiona-version-comparison', JSON.stringify(selection))''',
                    {'baseline': source['run_id'], 'target': mismatch['run_id'],
                     'baselineVersion': 'v1', 'targetVersion': 'v2'})
                page.reload()
                expect(page.locator('#version-baseline-model')).to_have_value(source['run_id'])
                expect(page.locator('#version-target-model')).to_have_value('')
                expect(page.locator('#version-target-run')).to_have_value('')
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'false')
                assert len(judge.calls) == calls and not sent
                assert not posts, posts
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
        assert source_path.read_bytes() == source_bytes
        assert target_path.read_bytes() == target_bytes
    print('PASS: two visible model selectors, direct comparison, explicit multiple evaluations, '
          'unevaluated model guidance, condition mismatch protection, selection and legacy session '
          'restoration, desktop/mobile; no additional model calls or source changes.')


if __name__ == '__main__':
    main()
