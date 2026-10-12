"""Isolated browser flow with local mocked providers/judge; no paid calls."""
import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from playwright.sync_api import expect, sync_playwright
from test_model_lifecycle import make_service
from webapp.server import Server, Handler


def main():
    output = ROOT / 'results/ui-preview'
    output.mkdir(exist_ok=True)
    Handler.log_message = lambda *args: None
    with tempfile.TemporaryDirectory(prefix='aiona-manual-ui-') as directory:
        service, judge, sent = make_service(Path(directory))
        server = Server(('127.0.0.1', 0), code_eval_service=service.evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                errors = []
                page.on('pageerror', lambda err: errors.append(str(err)))
                def setup(route):
                    response = route.fetch()
                    info = response.json()
                    info['semantic_setup'] = {'can_evaluate': True, 'engine_label': '모의 평가자', 'model': 'fixture-only'}
                    route.fulfill(response=response, json=info)
                page.route('**/api/code-eval/info', setup)
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base + '/workspace.html#setup')
                expect(page.locator('#setup-current-version')).to_have_text('AIONA v1')
                expect(page.locator('#collect-button')).to_be_disabled()
                expect(page.locator('#aiona-config')).to_have_count(0)
                raw = [{'case_id': 'FILE1', 'question': '파일 질문\n ', 'answer': '파일 답변\n '}]
                page.locator('#dataset-file').set_input_files({'name': 'fixture.json', 'mimeType': 'application/json', 'buffer': json.dumps(raw).encode()})
                expect(page.locator('#file-name')).to_have_text('fixture.json')
                page.locator('#input-mode-manual').click()
                expect(page.locator('#file-input-panel')).to_be_hidden()
                expect(page.locator('#collect-button')).to_be_disabled()
                question, answer = '  질문 원문\n두 번째 줄  ', '  AIONA 답변\n 그대로  '
                page.locator('#manual-question').fill(question)
                expect(page.locator('#collect-button')).to_be_disabled()
                page.locator('#manual-answer').fill('  ')
                expect(page.locator('#collect-button')).to_be_disabled()
                page.locator('#manual-answer').fill(answer)
                expect(page.locator('#collect-button')).to_be_enabled()
                page.locator('#input-mode-file').click()
                expect(page.locator('#file-name')).to_have_text('fixture.json')
                page.locator('#input-mode-manual').click()
                expect(page.locator('#manual-question')).to_have_value(question)
                expect(page.locator('#manual-answer')).to_have_value(answer)
                page.screenshot(path=str(output / 'manual-input-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'manual-input-mobile.png'), full_page=True)
                page.set_viewport_size({'width': 1440, 'height': 1080})
                page.locator('#collect-button').click()
                expect(page.locator('#final-summary')).to_be_visible(timeout=15000)
                manual = service.history()[0]
                assert manual['status'] == 'COMPLETED'
                saved = service.result(manual['run_id'])
                assert saved['input_mode'] == 'manual' and saved['aiona_release']['version'] == 'v1'
                assert saved['datasets']['aiona'][0]['question'] == question
                assert saved['datasets']['aiona'][0]['answer'] == answer
                assert len(judge.calls) == 4 and all(len(calls) == 1 for calls in sent.values())
                page.locator('[data-view="setup"]').click()
                page.locator('#input-mode-file').click()
                expect(page.locator('#collect-button')).to_be_enabled()
                page.locator('#collect-button').click()
                expect(page.locator('#final-summary')).to_be_visible(timeout=15000)
                assert len(judge.calls) == 8 and len(service.history()) == 2
                page.locator('[data-view="versions"]').click()
                expect(page.locator('[data-model-version="v1"] td').nth(3)).to_have_text('2건')
                expect(page.locator('#view-comparison')).to_be_hidden()
                page.locator('#version-register-next').click()
                expect(page.locator('#release-dialog')).to_be_visible()
                expect(page.locator('#release-set-current')).not_to_be_checked()
                page.locator('#release-version').fill('v2')
                page.locator('#release-notes').fill('문맥 유지 개선 <script>표시는 텍스트</script>')
                page.locator('#release-set-current').check()
                page.locator('#save-release').click()
                expect(page.locator('#release-dialog')).not_to_be_visible()
                expect(page.locator('#model-default-dialog')).to_be_visible()
                expect(page.locator('#model-default-dialog')).to_contain_text('AIONA v1')
                expect(page.locator('#model-default-dialog')).to_contain_text('AIONA v2')
                assert service.version_registry.current_release()['version'] == 'v1'
                page.locator('#model-default-reason').fill('문맥 유지 개선 평가를 위한 기본값 변경')
                page.locator('#model-default-confirm').click()
                expect(page.locator('#model-default-dialog')).not_to_be_visible()
                expect(page.locator('[data-model-version="v2"]')).to_have_attribute('data-current', 'true')
                expect(page.locator('[data-model-version="v2"] td').nth(3)).to_have_text('0건')
                assert len(judge.calls) == 8 and len(service.history()) == 2
                page.reload()
                expect(page.locator('[data-model-version="v2"]')).to_have_attribute('data-current', 'true')
                page.screenshot(path=str(output / 'model-versions-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'model-versions-mobile.png'), full_page=True)
                page.set_viewport_size({'width': 1440, 'height': 1080})
                page.locator('#version-register-next').click()
                page.locator('#release-version').fill('v2')
                page.locator('#save-release').click()
                expect(page.locator('#release-error')).to_contain_text('이미 등록')
                page.locator('[data-close="release-dialog"]').first.click()
                page.locator('[data-model-version="v1"]').get_by_role('button', name='평가 이력', exact=True).click()
                expect(page.locator('#history-version-filter')).to_have_value('v1')
                expect(page.locator('#history-body tr')).to_have_count(2)
                expect(page.locator('#history-body')).to_contain_text('직접 입력')
                expect(page.locator('#history-body')).to_contain_text('파일 업로드')
                detail_trigger = page.locator(f'[data-run-id="{manual["run_id"]}"] .row-button')
                detail_trigger.click()
                expect(page.locator('#history-detail-dialog')).to_be_visible()
                expect(page.locator('#history-source-question')).to_have_count(1)
                assert page.locator('.history-original-content pre').nth(0).text_content() == question
                assert page.locator('.history-original-content pre').nth(1).text_content() == answer
                page.keyboard.press('Escape')
                expect(page.locator('#history-detail-dialog')).not_to_be_visible()
                expect(detail_trigger).to_be_focused()
                detail_trigger.click()
                page.locator('#history-detail-dialog').get_by_role('button', name='저장된 평가 결과 열기', exact=True).click()
                expect(page.locator('#view-results')).to_be_visible()
                page.locator('[data-view="history"]').click()
                expect(page.locator('#history-version-filter')).to_have_value('v1')
                page.locator('#history-version-filter').select_option('v2')
                expect(page.locator('#history-body tr')).to_have_count(0)
                expect(page.locator('#history-feedback-runs')).to_be_visible()
                page.locator('[data-view="setup"]').click()
                expect(page.locator('#setup-current-version')).to_have_text('AIONA v2')
                page.locator('[data-view="versions"]').click()
                page.locator('[data-model-version="v1"]').get_by_role('button', name='기본 평가 버전으로 선택', exact=True).click()
                page.locator('#model-default-reason').fill('이전 모델의 추가 평가')
                page.locator('#model-default-confirm').click()
                expect(page.locator('[data-model-version="v1"]')).to_have_attribute('data-current', 'true')
                assert all(r['aiona_release']['version'] == 'v1' for r in service.history())
                activities = page.request.get(base + '/api/comparison/activity-history').json()['events']
                changes = [event for event in activities if event['event_type'] == 'default_version_changed']
                assert len(changes) == 2 and {event['reason'] for event in changes} == {'문맥 유지 개선 평가를 위한 기본값 변경', '이전 모델의 추가 평가'}
                assert service.result(manual['run_id'])['datasets']['aiona'] == saved['datasets']['aiona']
                assert len(judge.calls) == 8 and len(service.history()) == 2
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    print('PASS: manual/file evaluation, exact source, explicit default selection with reasons, saved-run reopening, history filters, dialog focus return, activity persistence, desktop/mobile; mocked models only.')


if __name__ == '__main__':
    main()
