"""Read-only replay of the reported 68 x 4 run; no provider calls or saved writes."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server


def main():
    saved = ROOT / 'data/comparison_runs/7c13f8299015.json'
    original = saved.read_bytes()
    run = json.loads(original)
    errors, writes, external = [], [], []
    output = ROOT / 'results/outcome-consistency-review'
    output.mkdir(parents=True, exist_ok=True)

    def forbidden(*args, **kwargs):
        raise AssertionError('Provider calls forbidden in saved-result replay')

    with tempfile.TemporaryDirectory(prefix='outcome-ui-') as temporary:
        path = Path(temporary)
        evaluator = CodeEvalService(save_dir=path / 'eval', jev_resolver=forbidden)
        service = ComparisonService(evaluator, save_dir=path / 'runs', client_factory=forbidden)
        service._runs[run['run_id']] = copy.deepcopy(run)
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
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
                page.add_init_script('sessionStorage.setItem("aiona-comparison-run", "' + run['run_id'] + '")')
                page.goto(base + '/workspace.html#results')
                expect(page.locator('#live-semantic')).to_have_text('272 / 272개')
                expect(page.locator('#result-models')).not_to_contain_text('중복')
                observed = []
                for provider in run['providers']:
                    card = page.locator(f'.result-model-card[data-provider="{provider}"]')
                    counts = [int(value) for value in card.locator('.outcome-count strong').all_text_contents()]
                    assert sum(counts) == 68, (provider, counts)
                    metric = card.locator('.evaluation-metrics')
                    expect(metric).to_contain_text('질문 최종 판정')
                    unjudged = int(re.search(r'(\d+)개 미완료', metric.text_content()).group(1))
                    if unjudged:
                        expect(metric).to_contain_text('미확정')
                    warning = card.locator('.evaluation-warning').filter(has_text='미완료 사유')
                    for text in warning.locator(':scope > div > p').all_text_contents():
                        assert len(text) < 80, text
                    assert warning.locator(':scope > div > p').count() <= 3
                    expect(warning.locator('details')).not_to_have_attribute('open', '')
                    card.locator('.outcome-count.pending').click()
                    expect(page.locator('#matrix-caption')).to_contain_text(f'{counts[2]}개')
                    observed.append({'provider': provider, 'fail': counts[0], 'pass': counts[1], 'pending': counts[2]})
                for cell in page.locator('#criteria-body td').all():
                    if '미판정' in cell.text_content():
                        expect(cell.locator('.criterion-rate')).to_have_text('미확정')
                page.locator('#result-models').screenshot(path=str(output / 'model-cards-desktop.png'))
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('.result-model-card').first.screenshot(path=str(output / 'model-card-mobile.png'))
                assert not errors and not writes and not external, (errors, writes, external)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    assert hashlib.sha256(saved.read_bytes()).digest() == hashlib.sha256(original).digest()
    print(json.dumps({'status': 'PASS', 'question_counts': observed, 'saved_unchanged': True,
                      'external_calls': 0, 'writes': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
