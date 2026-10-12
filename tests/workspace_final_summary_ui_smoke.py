"""Desktop checks using saved results and explicit variants; no model calls."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from playwright.sync_api import expect, sync_playwright
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server


def main():
    saved = ROOT / 'data/comparison_runs/a3b5d89bc041.json'
    original = saved.read_bytes()
    result = json.loads(original)
    errors, writes = [], []
    with tempfile.TemporaryDirectory(prefix='final-summary-ui-') as directory:
        evaluator = CodeEvalService(save_dir=Path(directory)/'eval', jev_resolver=lambda: (_ for _ in ()).throw(AssertionError('No judge calls')))
        service = ComparisonService(evaluator, save_dir=Path(directory)/'runs')
        def fixture(value, suffix):
            run = copy.deepcopy(value)
            run['run_id'] = 'f' * 11 + suffix
            service._runs[run['run_id']] = run
            return run
        complete = fixture(result, '0')
        unfinished = fixture(result, '1')
        unfinished['evaluations']['gpt']['jev_eval']['records'][0] = {'status': 'API_ERROR', 'criteria': ['J1'], 'decisions': None, 'error': 'UI fixture'}
        mismatch = fixture(result, '2')
        mismatch['datasets']['gpt'][0]['question'] = 'Different fixture question'
        tie = fixture(result, '3')
        for provider in tie['providers']:
            tie['evaluations'][provider] = copy.deepcopy(result['evaluations']['aiona'])
        running = fixture(result, '4'); running['status'] = 'EVALUATING'
        solo = fixture(json.loads((ROOT/'data/comparison_runs/7722d46efcfc.json').read_text(encoding='utf-8')), '5')
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width':1440, 'height':1080})
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                page.on('request', lambda r: writes.append(r.url) if r.method != 'GET' else None)
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base+'/workspace.html')
                def open_run(run):
                    page.evaluate('(id) => sessionStorage.setItem("aiona-comparison-run", id)', run['run_id'])
                    page.goto(base+'/workspace.html#results')
                    page.reload()
                    expect(page.locator('#result-subtitle')).to_contain_text(run['name'])
                open_run(complete)
                expect(page.locator('#final-summary-title')).to_have_text('이번 평가의 최고점은 GPT, 98.2점입니다.')
                expect(page.locator('#final-summary-description')).to_contain_text('AIONA는 96.4점으로 2위')
                summary = page.locator('#final-summary')
                assert summary.locator('.summary-score-heading h3').all_text_contents() == ['GPT', 'AIONA', 'Claude', 'Gemini']
                assert summary.locator('.summary-score strong').all_text_contents() == ['98.2', '96.4', '86.7', '53.3']
                assert summary.locator('.summary-rank').all_text_contents() == ['1위','2위','3위','4위']
                for card, score in zip(summary.locator('.summary-score-card').all(), [162/165,159/165,143/165,88/165]):
                    track = card.locator('.summary-score-track').bounding_box()
                    fill = card.locator('.summary-score-track > span').bounding_box()
                    assert abs(fill['width'] / track['width'] - score) < .001
                assert all('/ 전체 165문항' in s for s in summary.locator('.summary-score-denominator').all_text_contents())
                assert page.locator('#final-summary').bounding_box()['y'] < page.locator('.run-progress').bounding_box()['y'] < page.locator('#result-models').bounding_box()['y']
                for width in (1280,1440,1680,1920):
                    page.set_viewport_size({'width':width,'height':1080})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                    boxes=[x.bounding_box() for x in summary.locator('.summary-score-card').all()]
                    assert max(b['y'] for b in boxes) - min(b['y'] for b in boxes) < 1
                    if width == 1440:
                        page.screenshot(path=str(ROOT/'results/ui-preview/workspace-final-summary-desktop.png'))
                        page.locator('#final-summary').screenshot(path=str(ROOT/'results/ui-preview/workspace-final-summary-card.png'))
                link=page.locator('[data-summary-provider="gemini"] .summary-score-link')
                link.focus(); page.keyboard.press('Enter')
                expect(page.locator('#matrix-model')).to_have_value('gemini')
                expect(page.locator('#matrix-filter')).to_have_value('issue')
                expect(page.locator('#matrix-caption')).to_contain_text('77')
                page.reload()
                expect(summary.locator('.summary-score strong')).to_have_text(['98.2','96.4','86.7','53.3'])
                open_run(unfinished)
                expect(page.locator('#final-summary-status')).to_have_text('최종 점수 미확정')
                expect(page.locator('#final-summary-description')).to_contain_text('1개 답변')
                assert summary.locator('.summary-score strong').all_text_contents() == ['—']*4
                assert summary.locator('.summary-rank').all_text_contents() == ['순위 미확정']*4
                open_run(mismatch)
                expect(page.locator('#final-summary-title')).to_contain_text('같은 문항으로 비교')
                open_run(tie)
                expect(page.locator('#final-summary-title')).to_have_text('모든 모델이 96.4점으로 동률입니다.')
                assert summary.locator('.summary-rank').all_text_contents() == ['공동 1위']*4
                open_run(solo)
                expect(page.locator('#final-summary-title')).to_have_text('AIONA의 질문 통과점수는 96.4점입니다.')
                expect(summary.locator('.summary-rank')).to_have_text('단독 결과')
                open_run(running)
                expect(page.locator('#progress-title')).to_have_text('평가 중')
                expect(page.locator('#final-summary')).to_be_hidden()
                assert not writes and not errors, (writes, errors)
                browser.close()
        finally:
            server.shutdown(); server.server_close()
    assert hashlib.sha256(saved.read_bytes()).digest() == hashlib.sha256(original).digest()
    print('PASS: actual Q165 ranking/scores, summary first, desktop 1280/1440/1680/1920, keyboard fail drilldown, reload, incomplete/mismatch/tie/solo/running; saved file unchanged; no model calls.')


if __name__ == '__main__':
    main()
