"""Browser regression checks for upload races, recovery and full raw text.

Run: python tests/workspace_input_ui_smoke.py. Uses a temporary local server;
provider calls are forbidden by the test transport and no real keys are used.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from playwright.async_api import async_playwright, expect
from code_eval.jev_client import resolve_jev_client
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server


async def check(url):
    errors, writes = [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(permissions=['clipboard-read', 'clipboard-write'])
        page = await context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda request: writes.append(request.url) if request.method != 'GET' else None)
        await page.goto(url + '/workspace.html')
        await expect(page.locator('#server-status')).to_contain_text('연결됨')

        async def upload(value, name='input-ui-test.json'):
            await page.locator('#dataset-file').set_input_files({
                'name': name, 'mimeType': 'application/json',
                'buffer': json.dumps(value, ensure_ascii=False).encode('utf-8')})

        await upload([{'question': '질문만'}])
        await expect(page.locator('#global-error')).to_be_visible()
        await expect(page.locator('#collect-button')).to_be_disabled()
        await expect(page.locator('#sample-button')).to_be_enabled()
        await expect(page.locator('#dataset-panel')).to_have_attribute('aria-busy', 'false')

        # A delayed obsolete failure must not replace a newer successful file.
        entered, release = asyncio.Event(), asyncio.Event()

        async def delay(route):
            request = route.request.post_data_json
            if request['input_name'] == 'obsolete.json':
                entered.set()
                await release.wait()
                await route.fulfill(status=400, json={'error': 'obsolete upload error'})
            else:
                await route.continue_()

        await page.route('**/api/comparison/dataset', delay)
        await upload([{'question': '이전', 'answer': '이전 답'}], 'obsolete.json')
        await asyncio.wait_for(entered.wait(), timeout=5)
        await expect(page.locator('#dataset-loading')).to_be_visible()
        for button in ('sample-button', 'choose-file', 'replace-file', 'collect-button'):
            await expect(page.locator('#' + button)).to_be_disabled()

        original = '  긴 답변 원문\r\n' + '한글🙂' * 24000 + '\r\n끝의 공백도 보존  '
        cases = [
            {'id': 'T2', 'question': '  두 번째 질문\r\n', 'answer': original, 'conversation_id': 'out-of-order', 'turn': 2},
            {'id': 'T1', 'question': '첫 질문', 'answer': '첫 답변', 'conversation_id': 'out-of-order', 'turn': 1},
            {'id': 'single', 'question': '<script>window.injected=1</script>', 'answer': ''},
        ]
        await upload(cases, 'latest.json')
        await expect(page.locator('#file-name')).to_have_text('latest.json')
        await expect(page.locator('#collect-button')).to_be_enabled()

        await expect(page.locator('#dataset-loading')).not_to_be_visible()
        release.set()
        await expect(page.locator('#sample-button')).to_be_enabled()
        await expect(page.locator('#global-error')).not_to_be_visible()

        # Conversation navigation follows turn order, independent of file order.
        # 대화는 '질문별 답변 비교'에서 펼쳐 상세 보기로 연다 (그 대화로 묶인 원문 비교 창)
        await page.locator('#compare-filter').select_option('multi')
        await page.locator('#compare-list .compare-item summary').click()
        await page.locator('#compare-list .compare-turn').first.locator('[data-provider="aiona"] .compare-detail').click()
        await expect(page.locator('#answer-id')).to_contain_text('T1')
        await expect(page.locator('#answer-previous')).to_be_disabled()
        await page.locator('#answer-next').click()
        await expect(page.locator('#answer-id')).to_contain_text('T2')
        assert await page.locator('#original-question').text_content() == cases[0]['question']
        assert await page.locator('#original-answer pre').text_content() == original
        await page.locator('#copy-answer').click()
        assert await page.evaluate('navigator.clipboard.readText()') == original
        await page.locator('#answer-group').select_option('single')
        await expect(page.locator('#conversation-context')).not_to_be_visible()
        assert await page.locator('#original-question').text_content() == cases[2]['question']
        assert await page.evaluate('window.injected') is None
        await expect(page.locator('#answer-previous')).to_be_disabled()
        await expect(page.locator('#answer-next')).to_be_disabled()
        await page.keyboard.press('Escape')
        await page.locator('#compare-filter').select_option('')

        # Invalid replacement leaves the usable file and its raw data intact.
        await upload([{'question': '새 질문', 'answer': {'invalid': True}}])
        await expect(page.locator('#global-error')).to_contain_text('answer')
        await expect(page.locator('#file-name')).to_have_text('latest.json')
        await expect(page.locator('#collect-button')).to_be_enabled()

        # The actual attached id/turns file must be reviewable without evaluating it.
        dataset_path = ROOT / 'data/code_eval/AIONA_winz_단일28_멀티10-30_질문30_id_turns.json'
        dataset_bytes = dataset_path.read_bytes()
        dataset_text = dataset_bytes.decode('utf-8-sig')
        groups = json.loads(dataset_text)
        expected = [(group, turn) for group in groups for turn in group['turns']]
        assert len(groups) == 30 and len(expected) == 68
        assert [len(group['turns']) for group in groups] == [1] * 28 + [10, 30]
        async with page.expect_response(lambda response: response.url.endswith('/api/comparison/dataset')) as import_event:
            await page.locator('#dataset-file').set_input_files(str(dataset_path))
        imported = await (await import_event.value).json()
        await expect(page.locator('#file-name')).to_have_text(dataset_path.name)
        await expect(page.locator('#dataset-panel')).to_have_attribute('aria-busy', 'false')
        await expect(page.locator('#global-error')).not_to_be_visible()
        await expect(page.locator('#question-count')).to_have_text('30개')   # 단일 28 + 대화 2 (턴 수와 무관)
        await expect(page.locator('#answer-count')).to_have_text('68 / 68턴')   # AIONA 답변이 있는 턴 / 평가 대상 턴
        await expect(page.locator('#conversation-count')).to_have_text('2개 · 40턴')
        await expect(page.locator('#single-count')).to_have_text('28개')
        await expect(page.locator('#turn-count')).to_have_text('68턴')
        await page.locator('#compare-filter').select_option('multi')
        await expect(page.locator('#compare-list .compare-item')).to_have_count(2)
        await page.locator('#compare-filter').select_option('')
        await expect(page.locator('#import-tools, #import-description, #import-confirm, #import-confirm-label, #download-dataset')).to_have_count(0)
        await expect(page.locator('#collect-button')).to_be_enabled()
        await page.set_viewport_size({'width': 1440, 'height': 1080})
        await page.locator('#toast').wait_for(state='hidden')
        await page.evaluate('document.activeElement?.blur(); window.scrollTo(0, 0)')
        await page.screenshot(path=str(ROOT / 'results/ui-preview/workspace-upload-no-confirm-test.png'), full_page=True)

        await page.locator('#preview-all').click()
        await page.locator('#view-source').click()
        assert await page.locator('#source-text').text_content() == dataset_text
        await page.locator('#source-dialog [data-close]').click()
        await expect(page.locator('#answer-dialog')).to_be_visible()
        assert imported['import']['format'] == 'id/turns JSON'
        assert imported['import']['original_text'] == dataset_text
        assert len(imported['cases']) == 68
        for actual, (group, turn) in zip(imported['cases'], expected):
            assert actual['case_id'] == f"Q{group['id']}_T{turn['turn']}"
            assert actual['question'] == turn['question'] and actual['answer'] == turn['answer']
            assert actual['import_source']['group'] == {key: value for key, value in group.items() if key != 'turns'}
            assert actual['import_source']['turn'] == turn['turn']
            if len(group['turns']) == 1:
                assert 'conversation_id' not in actual and 'turn' not in actual
            else:
                assert actual['conversation_id'] == f"AIONA_ID_{group['id']}" and actual['turn'] == turn['turn']

        # Check every displayed question and answer, including original Markdown and whitespace.
        await expect(page.locator('#answer-select option')).to_have_count(68)
        for index, (_, turn) in enumerate(expected):
            await page.locator('#answer-select').select_option(str(index))
            assert await page.locator('#original-question').text_content() == turn['question']
            assert await page.locator('#original-answer pre').text_content() == turn['answer']
        await page.locator('#answer-group').select_option('single')
        await expect(page.locator('#answer-select option')).to_have_count(28)
        await expect(page.locator('#conversation-context')).not_to_be_visible()
        await page.keyboard.press('Escape')

        await page.locator('#compare-filter').select_option('multi')
        for shortcut, (group_id, count, start) in enumerate([(29, 10, 28), (30, 30, 38)]):
            item = page.locator('#compare-list .compare-item').nth(shortcut)
            if not await item.get_attribute('open'):
                await item.locator('summary').click()
            await item.locator('.compare-turn').first.locator('[data-provider="aiona"] .compare-detail').click()
            await expect(page.locator('#answer-group')).to_have_value(f'conversation:AIONA_ID_{group_id}')
            await expect(page.locator('#answer-select option')).to_have_count(count)
            await expect(page.locator('#answer-id')).to_contain_text(f'Q{group_id}_T1')
            await expect(page.locator('#answer-previous')).to_be_disabled()
            await expect(page.locator('#conversation-context')).to_contain_text(f'1 / {count}턴')
            await page.locator('#answer-next').click()
            await expect(page.locator('#answer-id')).to_contain_text(f'Q{group_id}_T2')
            await expect(page.locator('#conversation-context')).to_contain_text('이전 1개 턴')
            await page.locator('#answer-select').select_option(str(start + count - 1))
            await expect(page.locator('#answer-id')).to_contain_text(f'Q{group_id}_T{count}')
            await expect(page.locator('#answer-next')).to_be_disabled()
            await expect(page.locator('#conversation-context')).to_contain_text(f'이전 {count - 1}개 턴')
            assert await page.locator('#original-answer pre').text_content() == groups[group_id - 1]['turns'][-1]['answer']
            await page.keyboard.press('Escape')
        await expect(page.locator('#collect-button')).to_be_enabled()
        assert dataset_path.read_bytes() == dataset_bytes

        await page.locator('[data-view=setup]').click()
        for width in (320, 390, 768, 1024, 1440):
            await page.set_viewport_size({'width': width, 'height': 900})
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        await page.set_viewport_size({'width': 390, 'height': 844})
        await page.locator('[data-view=connections]').click()   # 키 등록은 연결·수집 설정 화면
        await page.locator('#connection-cards [data-target="gpt"]').get_by_role('button', name='연결 설정').click()
        await page.locator('#api-key').fill('disposable-browser-test-key')
        await page.locator('#toggle-key').click()
        await expect(page.locator('#api-key')).to_have_attribute('type', 'text')
        await page.keyboard.press('Escape')
        await expect(page.locator('#key-dialog')).not_to_be_visible()
        await expect(page.locator('#api-key')).to_have_value('')
        await expect(page.locator('#api-key')).to_have_attribute('type', 'password')
        assert not errors, errors
        # 업로드는 변환 요청과 자동 수집 작업 요청만 보낸다 — 자동 수집을 허용한 제공자가 없으므로 모델 호출은 없다
        assert writes and all(value.endswith(('/api/comparison/dataset', '/api/comparison/auto-collection/jobs')) for value in writes), writes
        await browser.close()


def main():
    def no_provider_calls(request):
        raise AssertionError('Input UI checks must not call model providers')

    with tempfile.TemporaryDirectory(prefix='aiona-input-ui-') as temporary:
        path = Path(temporary)
        evaluator = CodeEvalService(save_dir=path / 'eval', jev_resolver=lambda **_: resolve_jev_client(environment={}, env_path=path / '.no-env'))
        comparison = ComparisonService(evaluator, save_dir=path / 'compare', client_factory=lambda: httpx.Client(transport=httpx.MockTransport(no_provider_calls)))
        for provider in ('gemini', 'claude', 'gpt'):
            comparison.configure(provider, {'key': 'test-key', 'model': 'test-model'})
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=comparison)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            asyncio.run(check(f'http://127.0.0.1:{server.server_port}'))
            # 업로드마다 수집 작업 기록(작업 ID)은 생기지만, 허용된 제공자가 없어 AIONA 원문만 있고 요청·평가는 없다
            assert all(run['providers'] == ['aiona'] and run['status'] == 'READY' for run in comparison.history()), comparison.history()
            print('Input UI passed: invalid upload recovery, stale failure isolation, loading controls, unsorted turns, 72k+ original answer/copy, actual id/turns JSON with 68 exact Q/A, 28 singles and 10/30-turn navigation, immediate readiness without confirmation, optional source preview, safe text, 5 setup layouts and key clearing from model cards. No model calls; upload jobs keep only AIONA originals.')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    main()
