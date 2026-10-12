"""Verify polling against real workers paused between semantic results.

python tests/workspace_live_ui_smoke.py
No real providers or semantic engines are called.
"""
from pathlib import Path
import re
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright
from code_eval.codex_cli_client import CodexCliConfig
from test_live_comparison_progress import StagedJudge, live_service, until
from webapp.server import Server


def main():
    screenshots = ROOT / 'results' / 'ui-preview'
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='aiona-live-ui-') as temporary:
        judge = StagedJudge()
        judge.config = CodexCliConfig(executable='test-native-codex')
        comparison = live_service(Path(temporary), judge)
        source = [{'id': f'Q00{i+1}', 'question': q, 'answer': '  답변 원문\r\n'} for i, q in enumerate(judge.entered)]
        rid = comparison.start({'cases': source, 'name': '실시간 평가 UI 검증용 대역'})['run_id']
        server = Server(('127.0.0.1', 0), code_eval_service=comparison.evaluator, comparison_service=comparison)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(base + '/workspace.html')
                page.evaluate('(id) => sessionStorage.setItem("aiona-comparison-run", id)', rid)
                page.goto(base + '/workspace.html#results')
                page.reload()
                page.locator('#evaluate-button').click()
                assert all(event.wait(3) for event in judge.entered.values())
                expect(page.locator('#live-evaluation')).to_be_visible()
                expect(page.locator('#live-code')).to_have_text('3 / 3개')
                expect(page.locator('#live-semantic')).to_have_text('0 / 3개')
                expect(page.locator('#live-semantic-detail')).to_have_text('기준 판정 완료 0')
                expect(page.locator('.model-live-progress')).to_contain_text('확인 필요 0개')
                expect(page.locator('.result-model-card .metric-label')).to_contain_text('의미 평가 요청 처리')
                expect(page.locator('#live-active')).to_contain_text('Q001')
                expect(page.locator('#matrix-body .pill').first).to_have_text('평가 중')
                assert page.request.get(base + f'/api/comparison/runs/{rid}/evaluation/aiona/html').status == 404

                page.locator('#matrix-search').fill('Q003')
                page.locator('#matrix-body .result-cell').focus()
                judge.release['second'].set()
                expect(page.locator('#live-semantic')).to_have_text('1 / 3개', timeout=6000)
                expect(page.locator('#live-latest')).to_contain_text('Q002')
                expect(page.locator('#matrix-search')).to_have_value('Q003')
                expect(page.locator('#matrix-body .result-cell')).to_be_focused()
                page.locator('#clear-matrix-filter').click()
                expect(page.locator('#matrix-body tr').nth(1).locator('.pill')).to_have_text('FAIL · 기준 미충족')
                expect(page.locator('.outcome-count.issue strong')).to_have_text('1')
                expect(page.locator('.evaluation-warning').filter(has_text='미완료 사유')).to_have_count(0)
                expect(page.locator('#live-elapsed')).not_to_have_text('00:00', timeout=6000)
                page.evaluate('document.activeElement.blur();window.scrollTo(0,0)')
                page.screenshot(path=str(screenshots / 'workspace-live-progress-test.png'), full_page=True)

                for width in (320, 390, 768, 1024, 1440):
                    page.set_viewport_size({'width': width, 'height': 844})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                    if width == 390:
                        page.screenshot(path=str(screenshots / 'workspace-live-progress-mobile-test.png'), full_page=True)
                page.reload()
                expect(page.locator('#live-semantic')).to_have_text('1 / 3개')
                judge.release['third'].set()
                expect(page.locator('#live-semantic')).to_have_text('2 / 3개', timeout=6000)
                expect(page.locator('#live-semantic-detail')).to_contain_text('오류 1')
                expect(page.locator('#matrix-body tr').nth(2).locator('.pill')).to_have_text('평가 미완료')
                expect(page.locator('.evaluation-warning')).to_contain_text('미완료 사유 · 1개 질문')
                page.locator('#matrix-body tr').nth(2).locator('.result-cell').click()
                page.locator('#answer-evaluation summary').click()
                judge.release['first'].set()
                expect(page.locator('#progress-title')).to_have_text('평가 미완료', timeout=6000)
                expect(page.locator('#answer-evaluation details')).to_have_attribute('open', '')
                page.locator('#answer-dialog [data-close]').click()
                expect(page.locator('#live-evaluation')).to_be_visible()
                expect(page.locator('#live-code')).to_have_text('3 / 3개')
                expect(page.locator('#live-semantic')).to_have_text('3 / 3개')
                expect(page.locator('#live-semantic-detail')).to_contain_text('판정 완료 2')
                expect(page.locator('#run-progress')).to_have_attribute('value', '6')
                expect(page.locator('#run-progress')).to_have_attribute('max', '6')
                expect(page.locator('.result-model-card .result-metric strong')).to_have_text('3')
                expect(page.locator('.result-model-card .evaluation-metrics')).to_contain_text('2 / 3개 질문')
                frozen_elapsed = page.locator('#live-elapsed').inner_text()
                page.wait_for_timeout(1200)
                expect(page.locator('#live-elapsed')).to_have_text(frozen_elapsed)
                expect(page.locator('.outcome-count.issue strong')).to_have_text('1')
                expect(page.locator('.outcome-count.clear strong')).to_have_text('1')
                expect(page.locator('.outcome-count.pending strong')).to_have_text('1')
                expect(page.locator('.result-model-card .hint').filter(has_text='GPT CLI (Codex)')).to_contain_text('gpt-6.1-sol · 추론 high')
                assert page.request.get(base + f'/api/comparison/runs/{rid}/evaluation/aiona/html').status == 200
                assert comparison.result(rid)['datasets']['aiona'][0]['answer'] == source[0]['answer']
                assert not errors, errors
                browser.close()
            print('Live UI passed: real paused workers, 0/3 -> 1/3 -> 2/3 -> final, out-of-order failure, active IDs, elapsed time, focus/search/modal-detail retention, reload, 5 widths, Codex/model/high labels, partial-report guard and unchanged originals.')
        finally:
            for event in judge.release.values():
                event.set()
            until(lambda: comparison.status(rid)['status'] != 'EVALUATING')
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    main()
