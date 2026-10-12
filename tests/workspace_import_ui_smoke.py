"""Upload and optional source preview with mock collection; no real providers."""
import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from playwright.sync_api import expect, sync_playwright
from test_blind_comparison import adapter_fixture
from code_eval.jev_client import resolve_jev_client
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server


def main():
    with tempfile.TemporaryDirectory(prefix='aiona-import-') as directory:
        folder = Path(directory)
        evaluator = CodeEvalService(save_dir=folder / 'eval', jev_resolver=lambda: resolve_jev_client(environment={}, env_path=folder / '.missing'))

        handler, sent, _, _ = adapter_fixture()
        service = ComparisonService(evaluator, save_dir=folder / 'runs',
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
        for provider in ('gemini', 'claude', 'gpt'):
            service.configure(provider, {'key': 'test-key', 'model': 'test-model'})
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/workspace.html')
                expect(page.locator('#server-status')).to_contain_text('연결됨')

                def upload(text, name, encoding='utf-8'):
                    with page.expect_response(lambda response: response.url.endswith('/api/comparison/dataset')) as event:
                        page.locator('#dataset-file').set_input_files({'name': name,
                            'mimeType': 'text/plain', 'buffer': text.encode(encoding)})
                    return event.value.json()

                source = '# AIONA 대화 원문\r\n# 단일턴\r\nQ: 독립 질문\r\nA: 독립 답변\r\n'
                source += '# 1회차\r\n## 질문 1\r\n  첫 질문🙂\r\n## 답변 1\r\n  **강조**\r\n```text\r\nQ: 코드 안 표시\r\n```\r\n'
                source += 'Q: 후속 질문\r\nA: <script>window.injected=1</script>\r\n마지막 공백  '
                imported = upload(source, '대화 원문.md')
                expect(page.locator('#question-count')).to_contain_text('3')
                expect(page.locator('#conversation-count')).to_contain_text('1')
                expect(page.locator('#import-tools, #import-description, #import-confirm, #import-confirm-label, #download-dataset')).to_have_count(0)
                expect(page.locator('#collect-button')).to_be_enabled()
                expect(page.locator('#view-source')).to_be_hidden()
                assert imported['import']['format'] == 'Markdown'
                assert imported['import']['original_text'] == source
                assert len(imported['cases']) == 3
                assert not service.history()
                page.locator('#preview-all').click()
                page.locator('#view-source').click()
                assert page.locator('#source-text').text_content() == source
                expect(page.locator('#source-dialog #import-notes')).to_contain_text('원문에 보존')
                assert page.evaluate('window.injected') is None
                page.locator('#source-dialog [data-close]').click()
                expect(page.locator('#answer-dialog')).to_be_visible()
                page.locator('#answer-next').click()
                assert page.locator('#original-question').text_content() == '  첫 질문🙂\r\n'
                page.locator('#answer-dialog [data-close]').click()
                expect(page.locator('#collect-button')).to_be_enabled()

                # A failed replacement keeps the previous usable dataset intact.
                upload('구분 없는 글입니다.', 'ambiguous.txt')
                expect(page.locator('#global-error')).to_contain_text('경계')
                expect(page.locator('#file-name')).to_have_text('대화 원문.md')
                expect(page.locator('#collect-button')).to_be_enabled()
                upload('질문: 새 질문\n답변: 새 답변', 'qa.txt', 'utf-16')
                expect(page.locator('#file-name')).to_have_text('qa.txt')
                expect(page.locator('#collect-button')).to_be_enabled()
                expect(page.locator('#global-error')).to_be_hidden()

                grouped_path = ROOT / 'data/code_eval/AIONA_winz_멀티턴_1-3회차_통합.json'
                page.locator('#dataset-file').set_input_files(str(grouped_path))
                expect(page.locator('#question-count')).to_contain_text('65')
                expect(page.locator('#conversation-count')).to_contain_text('3')
                expect(page.locator('#collect-button')).to_be_enabled()
                assert not service.history()
                for width in (320, 390, 768, 1440):
                    page.set_viewport_size({'width': width, 'height': 1080})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                page.locator('#toast').wait_for(state='hidden')
                page.locator('#dataset-panel').screenshot(path=str(ROOT / 'results/ui-preview/workspace-import-preview.png'))
                page.set_viewport_size({'width': 390, 'height': 1080})
                page.locator('#dataset-panel').screenshot(path=str(ROOT / 'results/ui-preview/workspace-import-mobile.png'))

                # Canonical and converted input both start immediately after upload.
                upload('[{"question":"q","answer":"a"}]', 'already.json')
                expect(page.locator('#collect-button')).to_be_enabled()
                page.locator('#preview-all').click()
                expect(page.locator('#view-source')).to_be_hidden()
                page.keyboard.press('Escape')
                upload(source, '대화 원문.md')
                expect(page.locator('#collect-button')).to_be_enabled()
                assert not sent and not service.history()
                page.locator('#collect-button').click()
                expect(page.locator('#blind-status')).to_have_text('평가 작업 종료 · 실제 모델명 공개', timeout=10000)
                rid = page.evaluate('sessionStorage.getItem("aiona-comparison-run")')
                assert service.result(rid)['input_document']['original_text'] == source
                assert len(service.result(rid)['datasets']['aiona']) == 3
                assert len(service.result(rid)['providers']) == 4
                assert all(len(sent[p]) == 3 for p in ('gemini', 'claude', 'gpt'))
                with page.expect_download() as event:
                    page.locator('#export-all').click()
                exported = json.loads(Path(event.value.path()).read_text(encoding='utf-8'))
                assert exported['input_document']['original_text'] == source
                assert exported['datasets']['aiona'] == imported['cases']
                page.locator('#matrix-body .row-button').first.click()
                expect(page.locator('#view-source')).to_be_hidden()
                page.keyboard.press('Escape')
                assert not errors, errors
                browser.close()
                print('Import UI passed: MD, UTF-16 TXT, grouped JSON 65 turns, immediate start without confirmation, optional source within full Q/A preview, preserved source/code blocks in final export, safe HTML, replacement recovery, 4 widths and offline evaluation. No paid calls.')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    main()
