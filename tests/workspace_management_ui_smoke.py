"""Version/history browser regression on isolated storage and mocked evaluation only."""
import copy
import json
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


def main():
    Handler.log_message = lambda *args: None
    output = ROOT / 'results' / 'management-ui-review'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='aiona-management-ui-') as directory:
        service, judge, sent, source = baseline(Path(directory))
        rows = [{**r, 'answer': '새 답변 ' + r['case_id']} for r in service.version_template(source['run_id'])['cases']]
        target = wait(service, service.start_version_run({'baseline_run_id': source['run_id'], 'cases': rows,
                      'aiona_release': {'version': 'v2'}, 'name': '비교 검증 실행'})['run_id'])
        mismatch = copy.deepcopy(target)
        mismatch.update(run_id='abcdef123456', name='다른 평가 조건')
        mismatch['evaluations']['aiona']['jev_eval']['version'] = 'different-conditions'
        service._runs[mismatch['run_id']] = mismatch
        legacy_only = copy.deepcopy(target)
        legacy_only.update(run_id='abcdef123457', name='등록 정보 없는 과거 버전', aiona_release={'version': 'legacy-only', 'notes': '실행에만 기록됨'})
        service._runs[legacy_only['run_id']] = legacy_only
        source_path = service.save_dir / (source['run_id'] + '.json')
        original = source_path.read_bytes()
        calls = len(judge.calls)
        server = Server(('127.0.0.1', 0), code_eval_service=service.evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/workspace.html#versions')
                expect(page.locator('#model-default-version')).to_have_text('AIONA v2')
                expect(page.locator('#model-release-list tr')).to_have_count(3)
                expect(page.locator('#model-release-count')).to_have_text('2')
                legacy_row = page.locator('[data-model-version="legacy-only"]')
                expect(legacy_row).to_contain_text('실행에만 기록됨')
                legacy_row.get_by_role('button', name='버전 등록', exact=True).click()
                expect(page.locator('#release-version')).to_have_value('legacy-only')
                page.keyboard.press('Escape')
                page.locator('#version-register-next').click()
                expect(page.locator('#release-set-current')).not_to_be_checked()
                page.locator('#release-version').fill('v3')
                page.locator('#release-notes').fill('새 버전 검증 · <script>unsafe()</script>')
                page.locator('#save-release').click()
                expect(page.locator('#release-dialog')).to_be_hidden()
                expect(page.locator('#model-release-count')).to_have_text('3')
                expect(page.locator('#model-default-version')).to_have_text('AIONA v2')
                row = page.locator('[data-model-version="v3"]')
                row.get_by_role('button', name='기본 평가 버전으로 선택', exact=True).click()
                expect(page.locator('#model-default-dialog')).to_contain_text('AIONA v2')
                expect(page.locator('#model-default-dialog')).to_contain_text('AIONA v3')
                page.locator('#model-default-reason').fill('회귀 검증용 기본값 변경')
                page.locator('#model-default-confirm').click()
                expect(page.locator('#model-default-dialog')).to_be_hidden()
                expect(page.locator('#model-default-version')).to_have_text('AIONA v3')
                expect(row.get_by_role('button', name='상세 보기', exact=True)).to_be_focused()
                row.get_by_role('button', name='상세 보기', exact=True).click()
                expect(page.locator('#model-release-detail')).to_contain_text('<script>unsafe()</script>')
                expect(page.locator('#model-release-detail')).to_contain_text('회귀 검증용 기본값 변경')
                page.keyboard.press('Escape')
                expect(page.locator('#model-release-detail')).to_be_hidden()
                expect(row.get_by_role('button', name='상세 보기', exact=True)).to_be_focused()
                page.screenshot(path=str(output / 'versions-desktop.png'), full_page=True)

                # Two model choices resolve the exact saved results without extra version/run steps.
                page.evaluate("AionaWorkspace.go('comparison')")
                expect(page.locator('.version-selection select:visible')).to_have_count(2)
                expect(page.locator('#version-baseline-model')).to_be_visible()
                expect(page.locator('#version-target-model')).to_be_visible()
                expect(page.locator('#version-baseline-version')).to_be_hidden()
                expect(page.locator('#version-target-version')).to_be_hidden()
                expect(page.locator('#version-baseline-run')).to_be_hidden()
                expect(page.locator('#version-target-run')).to_be_hidden()
                expect(page.locator('#version-baseline-run')).to_have_value('')
                expect(page.locator('#version-target-run')).to_have_value('')
                page.locator('#version-baseline-model').select_option(source['run_id'])
                expect(page.locator('#version-baseline-run')).to_have_value(source['run_id'])
                expect(page.locator('#version-baseline-version')).to_have_value('v1')
                page.locator('#version-target-model').select_option('version:v3')
                expect(page.locator('#version-empty-target')).to_be_visible()
                expect(page.locator('#version-add-answers')).to_be_enabled()
                expect(page.locator('#version-target-run')).to_have_value('')
                page.locator('#version-target-model').select_option(target['run_id'])
                expect(page.locator('#version-target-version')).to_have_value('v2')
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'true')
                page.screenshot(path=str(output / 'comparison-desktop.png'), full_page=True)
                page.locator('#version-target-model').select_option(mismatch['run_id'])
                expect(page.locator('#version-summary')).to_have_attribute('data-ready', 'false')
                expect(page.locator('#version-summary')).to_contain_text('전체 개선 폭을 아직 확정할 수 없습니다.')
                page.reload()
                expect(page.locator('#version-target-run')).to_have_value(mismatch['run_id'])
                expect(page.locator('#version-target-model')).to_have_value(mismatch['run_id'])
                expect(page.locator('#version-reasons')).to_be_visible()

                page.locator('[data-view="history"]').click()
                expect(page.locator('#history-body tr')).to_have_count(4)
                page.locator('#history-search').fill(source['run_id'])
                expect(page.locator('#history-body tr')).to_have_count(1)
                page.locator('#history-body').get_by_role('button', name='상세 보기', exact=True).click()
                expect(page.locator('#history-detail-dialog')).to_contain_text('저장된 원문')
                expect(page.locator('#history-detail-dialog')).to_contain_text('평가 조건')
                expect(page.locator('#history-detail-dialog')).to_contain_text('관련 작업 이력')
                page.keyboard.press('Escape')
                page.locator('[data-view="versions"]').click()
                page.locator('[data-view="history"]').click()
                expect(page.locator('#history-search')).to_have_value(source['run_id'])
                page.locator('#history-search').fill('')
                page.screenshot(path=str(output / 'runs-desktop.png'), full_page=True)
                page.locator('#history-tab-activities').click()
                page.locator('#activity-type-filter').select_option('default_version_changed')
                page.locator('#activity-search').fill('회귀 검증용 기본값 변경')
                expect(page.locator('#activity-body tr')).to_have_count(1)
                page.locator('#activity-body').get_by_role('button', name='상세 보기', exact=True).click()
                expect(page.locator('#history-detail-dialog')).to_contain_text('로컬 사용자')
                expect(page.locator('#history-detail-dialog')).to_contain_text('변경 전')
                expect(page.locator('#history-detail-dialog')).to_contain_text('v2')
                expect(page.locator('#history-detail-dialog')).to_contain_text('v3')
                page.screenshot(path=str(output / 'activity-detail-desktop.png'), full_page=True)
                page.keyboard.press('Escape')
                page.screenshot(path=str(output / 'activities-desktop.png'), full_page=True)
                for view in ['versions', 'comparison', 'history']:
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.evaluate('(view) => AionaWorkspace.go(view)', view)
                    expect(page.locator('#view-' + view)).to_be_visible()
                    if view == 'history':
                        expect(page.locator('#history-panel-activities')).to_have_attribute('aria-busy', 'false')
                    else:
                        expect(page.locator('#version-summary')).to_have_attribute('aria-busy', 'false')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), view
                    page.screenshot(path=str(output / (view + '-mobile.png')), full_page=True)

                # Explicit API failure and truly empty state are different from filtered results.
                page.route('**/api/comparison/activity-history', lambda route: route.fulfill(status=503, json={'error': '검증용 조회 오류'}))
                page.locator('#refresh-history').click()
                expect(page.locator('#history-feedback-activities')).to_contain_text('검증용 조회 오류')
                page.screenshot(path=str(output / 'activity-error-mobile.png'), full_page=True)
                page.unroute('**/api/comparison/activity-history')
                page.route('**/api/comparison/activity-history', lambda route: route.fulfill(json={'events': [], 'total': 0}))
                page.locator('#refresh-history').click()
                expect(page.locator('#history-feedback-activities')).to_contain_text('아직 작업 이력이 없습니다')
                page.unroute('**/api/comparison/activity-history')
                page.set_viewport_size({'width': 1440, 'height': 1080})
                page.locator('[data-view="versions"]').click()
                page.locator('#version-register-next').click()
                page.locator('#release-version').fill('v4')
                page.locator('#release-set-current').check()
                page.locator('#save-release').click()
                expect(page.locator('#model-default-dialog')).to_be_visible()
                page.keyboard.press('Escape')
                expect(page.locator('#version-register-next')).to_be_focused()
                expect(page.locator('#model-default-version')).to_have_text('AIONA v3')
                page.route('**/api/comparison/model-versions', lambda route: route.fulfill(status=500, json={'error': '검증용 저장 실패'}))
                page.locator('#version-register-next').click()
                page.locator('#release-version').fill('save-failure')
                page.locator('#save-release').click()
                expect(page.locator('#release-error')).to_contain_text('검증용 저장 실패')
                expect(page.locator('#release-dialog')).to_be_visible()
                expect(page.locator('#save-release')).to_be_enabled()
                assert len(judge.calls) == calls and not sent
                assert source_path.read_bytes() == original
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    print('PASS: registration/default reason, persisted activity, exact saved comparison, filters, details, focus, empty/error/save failure, mobile; mocked evaluation only.')


if __name__ == '__main__':
    main()
