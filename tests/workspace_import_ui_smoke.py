"""Upload and optional source preview with mock collection; no real providers."""
import json
from pathlib import Path
import re
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
                # 질문 수는 단일 질문 1개 + 대화 1개(턴 수와 무관) — 942f959 의 업로드 요약 규칙, 턴 수는 파일 설명에 따로
                expect(page.locator('#question-count')).to_contain_text('2')
                expect(page.locator('#file-meta')).to_contain_text('Markdown · 3턴')
                expect(page.locator('#conversation-count')).to_contain_text('1')
                expect(page.locator('#import-tools, #import-description, #import-confirm, #import-confirm-label, #download-dataset')).to_have_count(0)
                expect(page.locator('#collect-button')).to_be_enabled()
                expect(page.locator('#view-source')).to_be_hidden()
                assert imported['import']['format'] == 'Markdown'
                assert imported['import']['original_text'] == source
                assert len(imported['cases']) == 3
                # 업로드는 자동 수집 작업을 만든다(0991560) — 연결된 AI 가 없어 AIONA 원문만 담고 외부 요청·평가는 없다
                runs = service.history()
                assert len(runs) == 1 and runs[0]['providers'] == ['aiona'], runs
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

                # 회차 묶음(id/turns) 형식 — 비공개 원본(AIONA_winz_멀티턴_1-3회차_통합.json, 3회차 65턴) 대신 같은 모양의 합성 파일 (4차)
                grouped_path = folder / 'AIONA_합성_멀티턴_3회차.json'
                grouped_path.write_text(json.dumps([{'id': f'{n}회차', 'turns': [
                    {'turn': t, 'question': f'{n}회차 {t}번째 합성 질문', 'answer': f'{n}회차 {t}번째 합성 답변'} for t in range(1, size + 1)]}
                    for n, size in ((1, 20), (2, 22), (3, 23))], ensure_ascii=False), encoding='utf-8')
                page.locator('#dataset-file').set_input_files(str(grouped_path))
                # 질문 수 = 대화 3개 (942f959: 대화 1개 = 질문 1개), 턴 65개는 파일 설명에
                expect(page.locator('#question-count')).to_contain_text('3')
                expect(page.locator('#file-meta')).to_contain_text('id/turns JSON · 65턴')
                expect(page.locator('#conversation-count')).to_contain_text('3')
                expect(page.locator('#collect-button')).to_be_enabled()
                assert all(run['providers'] == ['aiona'] for run in service.history())   # 업로드마다 AIONA 원문 작업만, 외부 요청 없음
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
                assert not sent and all(run['providers'] == ['aiona'] for run in service.history())   # 업로드 작업은 AIONA 원문만 (0991560)
                # 0991560 뒤로 수집은 업로드 때 자동 — 관리 설정에서 세 제공자를 허용하고 새 원문을 올리면 바로 수집하고, 버튼은 '평가 시작'
                service.update_auto_policy({'allowed': {p: True for p in ('gemini', 'claude', 'gpt')},
                                            'prices': {p: {'input': 1, 'output': 2} for p in ('gemini', 'claude', 'gpt')},
                                            'limits': {'job_calls': 50, 'job_cost_usd': 5, 'daily_calls': 100, 'daily_cost_usd': 10}})
                source = source.replace('독립 질문', '수집용 독립 질문')
                imported = upload(source, '대화 원문.md')
                expect(page.locator('#collect-button')).to_be_enabled(timeout=10000)
                page.locator('#collect-button').click()
                # a90b6be: 블라인드 상태는 고정 안내 — 끝남은 진행 제목으로 본다 (평가 모델이 없는 설정이라 '평가 미완료')
                expect(page.locator('#progress-title')).to_have_text(re.compile('평가 (완료|미완료)'), timeout=10000)
                expect(page.locator('#blind-status')).to_have_text('블라인드 평가 · 평가 모델에는 모델 이름 없이 보냅니다')
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
                # 결과 화면도 실행에 저장된 업로드 원문(input_document)을 연다 — sourceDocument() 가 결과면 result.input_document 를 읽는다
                expect(page.locator('#view-source')).to_be_visible()
                page.locator('#view-source').click()
                assert page.locator('#source-text').text_content() == source
                page.locator('#source-dialog [data-close]').click()
                page.keyboard.press('Escape')
                assert not errors, errors
                browser.close()
                print('Import UI passed: MD, UTF-16 TXT, grouped id/turns JSON 65 turns (synthetic fixture), upload-time auto collection after the policy allows providers, optional source within full Q/A preview, preserved source/code blocks in final export, safe HTML, replacement recovery, 4 widths and offline evaluation. No paid calls.')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    main()
