"""Optional browser smoke test and screenshots: python tests/workspace_ui_smoke.py.

Requires the Playwright Python package and its Chromium browser. All provider
responses are test doubles and the semantic evaluator is explicitly offline.
"""
from __future__ import annotations
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from playwright.sync_api import sync_playwright, expect
from code_eval.jev_client import resolve_jev_client
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server


def main():
    screenshots = ROOT / 'results' / 'ui-preview'
    screenshots.mkdir(parents=True, exist_ok=True)
    requests = []
    def answer(req):
        requests.append(req)
        text = '  테스트 답변\n```json\n{"value":"\\n"}\n```\n  '
        return httpx.Response(200, json={
            'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}],
            'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': text}],
            'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': text}]}}]})
    with tempfile.TemporaryDirectory(prefix='aiona-ui-') as temporary:
        path = Path(temporary)
        evaluator = CodeEvalService(save_dir=path / 'eval', jev_resolver=lambda **_: resolve_jev_client(environment={}, env_path=path / '.no-env'))
        comparison = ComparisonService(evaluator, save_dir=path / 'compare', client_factory=lambda: httpx.Client(transport=httpx.MockTransport(answer)))
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=comparison)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080}, device_scale_factor=1)
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{server.server_port}/workspace.html')
                expect(page.locator('#server-status')).to_contain_text('연결됨')
                expect(page.locator('#collect-button')).to_be_disabled()
                page.screenshot(path=str(screenshots / 'workspace-empty.png'), full_page=True)
                page.locator('#sample-button').click()
                expect(page.locator('#question-count')).to_contain_text('165')
                expect(page.locator('#conversation-count')).to_contain_text('3')
                expect(page.locator('#file-meta')).to_contain_text('멀티턴 65개')
                expect(page.locator('#preview-body tr')).to_have_count(4)
                expect(page.locator('.conversation-shortcut')).to_have_count(3)
                page.locator('.conversation-shortcut').nth(2).click()
                expect(page.locator('#answer-select option')).to_have_count(30)
                expect(page.locator('#answer-id')).to_contain_text('Q136 · 3회차 · 1턴')
                expect(page.locator('#answer-previous')).to_be_disabled()
                page.locator('#answer-next').click()
                expect(page.locator('#answer-id')).to_contain_text('Q137')
                expect(page.locator('#conversation-context')).to_contain_text('이전 1개 턴')
                page.locator('#answer-select').select_option('164')
                expect(page.locator('#answer-next')).to_be_disabled()
                expect(page.locator('#conversation-context')).to_contain_text('이전 29개 턴')
                page.locator('#answer-group').select_option('conversation:AIONA_MT_01')
                expect(page.locator('#answer-select option')).to_have_count(15)
                expect(page.locator('#answer-id')).to_contain_text('Q101')
                page.locator('#answer-group').select_option('single')
                expect(page.locator('#answer-select option')).to_have_count(100)
                expect(page.locator('#conversation-context')).not_to_be_visible()
                page.keyboard.press('Escape')
                expect(page.locator('#answer-dialog')).not_to_be_visible()
                assert not requests
                page.locator('#toast').wait_for(state='hidden')
                page.evaluate('document.activeElement.blur(); window.scrollTo(0, 0)')
                page.screenshot(path=str(screenshots / 'workspace-desktop.png'), full_page=True)
                page.locator('#preview-all').click()
                expect(page.locator('#answer-select option')).to_have_count(165)
                page.locator('#answer-next').click()
                expect(page.locator('#answer-id')).to_contain_text('Q002')
                page.locator('#answer-select').select_option('99')
                expect(page.locator('#answer-id')).to_contain_text('Q100')
                page.locator('#answer-select').select_option('100')
                expect(page.locator('#answer-id')).to_contain_text('Q101 · 1회차 · 1턴')
                page.locator('#answer-select').select_option('164')
                expect(page.locator('#answer-id')).to_contain_text('Q165 · 3회차 · 30턴')
                page.locator('#answer-dialog [data-close]').click()

                # Every responsive layout stays within the viewport; actions do
                # not cover the preceding dataset or run settings.
                for width in (320, 390, 768, 1024, 1440):
                    page.set_viewport_size({'width': width, 'height': 900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                    assert page.locator('.action-bar').bounding_box()['y'] >= page.locator('.advanced-options').bounding_box()['y'] + page.locator('.advanced-options').bounding_box()['height']

                page.set_viewport_size({'width': 390, 'height': 844})
                page.evaluate('document.activeElement.blur(); window.scrollTo(0, 0)')
                page.screenshot(path=str(screenshots / 'workspace-mobile.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.set_viewport_size({'width': 1440, 'height': 1080})
                page.locator('#setup-models .model-row').nth(3).get_by_role('button', name='연결하기').click()
                expect(page.locator('#key-dialog')).to_be_visible()
                page.locator('#api-key').fill('ui-test-key')
                page.locator('#model-id').fill('gpt-test')
                page.screenshot(path=str(screenshots / 'workspace-key-dialog.png'), full_page=True)
                page.locator('#save-key').click()
                expect(page.locator('#key-dialog')).not_to_be_visible()
                assert not requests
                assert page.locator('#api-key').input_value() == ''
                storage = page.evaluate('JSON.stringify({...localStorage,...sessionStorage})')
                assert 'ui-test-key' not in storage
                for index, provider in ((1, 'gemini'), (2, 'claude')):
                    expect(page.locator('#collect-button')).to_be_disabled()
                    page.locator('#setup-models .model-row').nth(index).get_by_role('button', name='연결하기').click()
                    page.locator('#api-key').fill('ui-test-key')
                    page.locator('#model-id').fill(provider + '-test')
                    page.locator('#save-key').click()
                    expect(page.locator('#key-dialog')).not_to_be_visible()

                fixture = [{'id': 'same', 'question': '  첫 질문\r\n', 'answer': '  첫 답변\n'},
                           {'id': 'same', 'question': '둘째 질문', 'answer': '<script>alert(1)</script>\n  '}]
                page.locator('#dataset-file').set_input_files({'name': 'UI 검증용.json', 'mimeType': 'application/json', 'buffer': json.dumps(fixture, ensure_ascii=False).encode()})
                expect(page.locator('#question-count')).to_contain_text('2')
                page.locator('.advanced-options summary').click()
                page.locator('#run-name').fill('UI 검증용 비교')
                page.locator('#collect-button').click()
                expect(page.locator('#progress-title')).to_have_text('평가 미완료', timeout=15000)
                expect(page.locator('#blind-banner')).to_be_visible()
                assert set(page.locator('.result-card-heading strong').all_text_contents()) == {'AIONA', 'Gemini', 'Claude', 'GPT'}
                expect(page.locator('#reveal-models')).to_be_hidden()
                expect(page.locator('#matrix-body tr')).to_have_count(2)
                assert len(requests) == 6
                sent = json.loads(next(r for r in requests if r.url.host == 'api.openai.com').content)['input'][0]['content']
                assert sent == fixture[0]['question']
                page.locator('#matrix-body .row-button').nth(1).click()
                page.get_by_role('tab', name='AIONA', exact=True).click()
                expect(page.locator('#original-question')).to_have_text('둘째 질문')
                assert page.locator('#original-answer pre').text_content() == fixture[1]['answer']
                assert page.locator('#original-answer script').count() == 0
                page.get_by_role('tab', name='GPT', exact=True).click()
                assert page.locator('#original-answer pre').text_content() == '  테스트 답변\n```json\n{"value":"\\n"}\n```\n  '
                page.locator('#answer-previous').click()
                expect(page.get_by_role('tab', name='GPT', exact=True)).to_have_attribute('aria-selected', 'true')
                page.get_by_role('tab', name='GPT', exact=True).focus()
                tabs = page.locator('#answer-tabs [role=tab]').all_text_contents()
                previous = tabs[(tabs.index('GPT') - 1) % len(tabs)]
                page.keyboard.press('ArrowLeft')
                expect(page.get_by_role('tab', name=previous, exact=True)).to_be_focused()
                page.get_by_role('tab', name='AIONA', exact=True).click()
                assert page.locator('#original-answer pre').text_content() == fixture[0]['answer']
                page.keyboard.press('End')
                expect(page.locator('#answer-tabs [role=tab]').last).to_be_focused()
                page.locator('#answer-dialog [data-close]').click()
                expect(page.locator('#evaluate-button')).to_be_hidden()
                expect(page.locator('#progress-title')).to_have_text('평가 미완료', timeout=15000)
                expect(page.locator('#run-progress')).to_have_attribute('value', '16')
                expect(page.locator('#run-progress')).to_have_attribute('max', '16')
                expect(page.locator('#matrix-body .pill').first).to_have_text('평가 미완료')  # offline semantics remain pending
                expect(page.locator('#matrix-body .check-status').first).to_contain_text('의미: 연결 필요')
                page.locator('#matrix-body .result-cell').first.click()
                expect(page.locator('#answer-evaluation')).to_contain_text('의미 평가 서비스가 연결되지 않아')
                page.locator('#answer-dialog [data-close]').click()
                page.screenshot(path=str(screenshots / 'workspace-test-results.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(screenshots / 'workspace-test-results-mobile.png'), full_page=True)
                page.set_viewport_size({'width': 1440, 'height': 1080})
                with page.expect_download() as info:
                    page.locator('#export-all').click()
                download = info.value
                data = json.loads(Path(download.path()).read_text(encoding='utf-8'))
                assert 'ui-test-key' not in json.dumps(data)
                assert data['datasets']['aiona'][1]['answer'] == fixture[1]['answer']
                rid = data['run_id']
                detail = browser.new_page()
                detail.on('pageerror', lambda e: errors.append(str(e)))
                detail.goto(f'http://127.0.0.1:{server.server_port}/code-eval.html?comparison={rid}&provider=gpt#results')
                expect(detail.locator('#result-body')).to_be_visible()
                assert '/evaluation/gpt/html' in detail.locator('#export-html').get_attribute('href')
                page.locator('[data-view=history]').click()
                expect(page.locator('#history-body tr')).to_have_count(1)
                for width in (320, 390, 768, 1024, 1440):
                    page.set_viewport_size({'width': width, 'height': 900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                    if width <= 700:
                        assert page.locator('#history-panel-runs .table-scroll').evaluate('(box) => { box.scrollLeft = 100; return box.scrollWidth > box.clientWidth && box.scrollLeft > 0; }')
                page.locator('#history-body').get_by_role('button', name='상세 보기', exact=True).click()
                expect(page.locator('#history-detail-dialog')).to_be_visible()
                page.locator('#history-detail-dialog').get_by_role('button', name='저장된 평가 결과 열기', exact=True).click()
                expect(page.locator('#view-results')).to_be_visible()
                expect(page.locator('#progress-title')).to_have_text('평가 미완료')
                page.reload()
                expect(page.locator('#progress-title')).to_have_text('평가 미완료')
                assert not errors, errors
                browser.close()
            print('UI smoke passed: 5 viewport widths, Q165 conversation navigation, keyboard/model tabs, keys, exact prompts/answers, collection, offline evaluation, exports, history and detail links.')
            print(f'Screenshots: {screenshots}')
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == '__main__':
    main()
