"""Guided version comparison on a temporary server with mocked judgments."""
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
    output = ROOT / 'results/ui-preview'
    with tempfile.TemporaryDirectory(prefix='same-questions-ui-') as directory:
        service, judge, sent, source = baseline(Path(directory))
        single = wait(service, service.start_version_run({'cases': [{'case_id': 'ONE', 'question': '  같은 단일 질문\n', 'answer': 'old'}],
                    'aiona_release': {'version': 'v1'}})['run_id'])
        service.version_registry.data['baseline_run_id'] = source['run_id']
        server = Server(('127.0.0.1', 0), code_eval_service=service.evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                def setup(route):
                    response = route.fetch(); info = response.json()
                    info['semantic_setup'] = {'can_evaluate': True, 'engine_label': '검증용 모의 평가자'}
                    route.fulfill(response=response, json=info)
                page.route('**/api/code-eval/info', setup)
                page.goto(f'http://127.0.0.1:{server.server_port}/workspace.html#comparison')
                expect(page.locator('#version-baseline-run')).to_have_value('')
                page.locator('#version-baseline-model').select_option(source['run_id'])
                expect(page.locator('#version-download-questions')).to_be_enabled()
                expect(page.locator('#version-baseline-run')).to_have_value(source['run_id'])
                with page.expect_download() as download:
                    page.locator('#version-download-questions').click()
                rows = json.loads(Path(download.value.path()).read_text(encoding='utf-8'))
                assert len(rows) == 3 and all(r['answer'] == '' for r in rows)
                assert 'old first' not in str(rows)
                page.screenshot(path=str(output / 'same-questions-flow.png'), full_page=True)
                page.locator('#version-add-answers').click()
                expect(page.locator('#version-answers-dialog')).to_be_visible()
                expect(page.locator('#version-answer-release')).to_have_value('v2')
                expect(page.locator('#version-answer-submit')).to_be_disabled()
                rows = [{**r, 'answer': '  new ' + r['case_id'] + '\n'} for r in rows]
                wrong = [dict(r) for r in rows]; wrong[0]['question'] = 'changed question'
                def upload(data):
                    page.locator('#version-answer-file').set_input_files({'name': 'new-answers.json', 'mimeType': 'application/json', 'buffer': json.dumps(data,ensure_ascii=False).encode()})
                calls = len(judge.calls)
                upload(wrong)
                expect(page.locator('#version-answer-error')).to_contain_text('기준과 다릅니다')
                expect(page.locator('#version-answer-submit')).to_be_disabled()
                assert len(judge.calls) == calls
                upload(list(reversed(rows)))
                expect(page.locator('#version-answer-check')).to_contain_text('동일 질문 3개')
                expect(page.locator('#version-answer-submit')).to_be_enabled()
                page.screenshot(path=str(output / 'same-questions-upload.png'), full_page=True)
                page.locator('#version-answer-submit').click()
                expect(page.locator('#version-answers-dialog')).not_to_be_visible()
                expect(page.locator('#view-comparison')).to_be_visible(timeout=15000)
                expect(page.locator('#version-summary')).to_have_attribute('aria-busy', 'false')
                new = service.history()[0]
                assert new['baseline_run_id'] == source['run_id']
                assert len(judge.calls) == calls + 3 and not sent
                page.evaluate("AionaWorkspace.go('comparison')")
                expect(page.locator('#version-baseline-run')).to_have_value(source['run_id'])
                expect(page.locator('#version-target-run')).to_have_value(new['run_id'])
                page.locator('#version-baseline-model').select_option(single['run_id'])
                expect(page.locator('#version-registration-status')).to_contain_text('1개 질문')
                page.locator('#version-add-answers').click()
                expect(page.locator('#version-answers-dialog')).to_be_visible()
                expect(page.locator('#version-answer-file-label')).to_be_hidden()
                assert page.locator('#version-fixed-question').text_content() == '  같은 단일 질문\n'
                expect(page.locator('#version-answer-submit')).to_be_disabled()
                page.locator('#version-answer-text').fill('  단일 새 답변\n ')
                expect(page.locator('#version-answer-submit')).to_be_enabled()
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#version-answers-dialog').screenshot(path=str(output / 'same-questions-single-mobile.png'))
                page.locator('#version-answer-submit').click()
                expect(page.locator('#version-answers-dialog')).not_to_be_visible()
                expect(page.locator('#view-comparison')).to_be_visible(timeout=15000)
                expect(page.locator('#version-summary')).to_have_attribute('aria-busy', 'false')
                result = service.result(service.history()[0]['run_id'])
                assert result['baseline_run_id'] == single['run_id']
                assert result['datasets']['aiona'][0]['question'] == '  같은 단일 질문\n'
                assert result['datasets']['aiona'][0]['answer'] == '  단일 새 답변\n '
                page.evaluate('(id) => AionaWorkspace.openRun(id)', result['run_id'])
                expect(page.locator('#final-summary-followup')).to_have_text('기준 버전과 비교')
                page.locator('#final-summary-followup').click()
                expect(page.locator('#version-baseline-run')).to_have_value(single['run_id'])
                expect(page.locator('#version-target-run')).to_have_value(result['run_id'])
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown(); server.server_close()
    print('PASS: question-only template, mismatch rejection before calls, reordered file alignment, new-version-only judgment, linked comparison, fixed single question with direct answer, mobile; mocked calls only.')


if __name__ == '__main__':
    main()
