"""Jobs continue across workspace menus and browser tabs; only mock models run."""
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright
from test_blind_comparison import CapturingJudge, adapter_fixture, make_service, until
from webapp.server import Server


def main():
    errors, writes, external = [], [], []
    collector_entered, collector_release = threading.Event(), threading.Event()
    judge = CapturingJudge(pause=True)
    adapter, sent, _, _ = adapter_fixture()

    def collect(request):
        collector_entered.set()
        assert collector_release.wait(30)
        return adapter(request)

    with tempfile.TemporaryDirectory(prefix='running-nav-ui-') as temporary:
        service = make_service(Path(temporary), judge, collect)
        cases = [{'id': 'navigation-fixture', 'question': '인사해 주세요.', 'answer': '안녕하세요.'}]
        rid = service.start({'cases': cases, 'providers': ['gemini', 'gpt', 'claude'],
                             'blind': True, 'auto_evaluate': True})['run_id']
        assert collector_entered.wait(3)
        server = Server(('127.0.0.1', 0), code_eval_service=service.evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 1080})
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))

                def read_only(route):
                    if route.request.method != 'GET':
                        writes.append(route.request.url)
                        route.abort()
                    elif not route.request.url.startswith(base + '/'):
                        external.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                page.route('**/*', read_only)
                page.add_init_script('sessionStorage.setItem("aiona-comparison-run", "' + rid + '")')
                page.goto(base + '/workspace.html#results')
                expect(page.locator('#progress-title')).to_have_text('응답 수집 중')
                page.locator('[data-view="criteria"]').click()
                expect(page.locator('#view-criteria')).to_be_visible()
                assert service.status(rid)['status'] == 'COLLECTING'
                collector_release.set()
                assert judge.entered.wait(5)
                until(lambda: service.status(rid)['done'] == 3)
                expect(page.locator('#view-criteria')).to_be_visible()
                assert service.status(rid)['status'] == 'EVALUATING'
                assert not service.status(rid).get('cancel_requested')
                page.locator('[data-view="results"]').click()
                expect(page.locator('#progress-title')).to_have_text('평가 중')
                assert page.evaluate('sessionStorage.getItem("aiona-comparison-run")') == rid
                page.locator('[data-view="history"]').click()
                expect(page.locator('#view-history')).to_be_visible()
                other_tab = page.context.new_page()
                other_tab.goto('about:blank')
                other_tab.bring_to_front()
                judge.release.set()
                until(lambda: service.status(rid)['status'] == 'COMPLETED')
                other_tab.close()
                page.bring_to_front()
                expect(page.locator('#view-history')).to_be_visible()
                assert not service.status(rid).get('cancel_requested')
                page.locator('[data-view="results"]').click()
                expect(page.locator('#live-semantic')).to_have_text('4 / 4개')
                expect(page.locator('#progress-title')).to_have_text('평가 완료')
                expect(page.locator('#final-summary')).to_have_attribute('data-ready', 'true')
                expect(page.locator('#result-models .result-metric strong')).to_have_text(['1'] * 4)
                assert page.evaluate('sessionStorage.getItem("aiona-comparison-run")') == rid
                assert len(judge.calls) == 4 and all(len(items) == 1 for items in sent.values())
                assert service.result(rid)['datasets']['aiona'][0]['answer'] == cases[0]['answer']
                assert not errors and not writes and not external, (errors, writes, external)
                browser.close()
            print('PASS: collection completed on criteria page; evaluation completed on history page while another browser tab was active; same run restored with 4/4 results; zero cancel/write/external browser requests; mock model calls only.')
        finally:
            collector_release.set()
            judge.release.set()
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
