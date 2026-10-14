"""Mentor progress navigation and JSON refresh without paid model calls."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Handler, Server


def main():
    data_path = ROOT / 'frontend/data/requirements.json'
    original_data = data_path.read_bytes()
    data = json.loads(original_data)
    assert len(data['items']) == 18
    output = ROOT / 'results/progress-workspace'
    output.mkdir(parents=True, exist_ok=True)
    errors, writes = [], []

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Progress browsing must not invoke a model')

    Handler.log_message = lambda *_args: None
    with tempfile.TemporaryDirectory(prefix='aiona-progress-') as directory:
        evaluator = CodeEvalService(save_dir=Path(directory) / 'eval', jev_resolver=forbidden)
        service = ComparisonService(evaluator, save_dir=Path(directory) / 'runs', client_factory=forbidden)
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 900})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: writes.append(request.url) if request.method != 'GET' else None)
                url = f'http://127.0.0.1:{server.server_port}/workspace.html'
                page.goto(url + '#progress')
                expect(page.locator('#view-progress')).to_be_visible()
                expect(page.locator('.progress-item')).to_have_count(18)
                expect(page.locator('.progress-metric').nth(0)).to_contain_text('18')
                expect(page.locator('.progress-metric').nth(1)).to_contain_text('3')
                expect(page.locator('.progress-metric').nth(2)).to_contain_text('9')
                expect(page.locator('.progress-metric').nth(3)).to_contain_text('4')
                expect(page.locator('.progress-metric').nth(4)).to_contain_text('2')
                expect(page.locator('.progress-metric').nth(5)).to_contain_text('미측정')
                page.locator('#requirement-W01 summary').click()
                expect(page.locator('#requirement-W01')).to_contain_text(data['items'][0]['acceptance'])
                page.get_by_label('담당자 필터').select_option('우영')
                expect(page.locator('.progress-item')).to_have_count(9)
                page.get_by_label('담당자 필터').select_option('윤재')
                expect(page.locator('.progress-item')).to_have_count(7)
                page.get_by_label('담당자 필터').select_option('공동')
                expect(page.locator('.progress-item')).to_have_count(2)
                page.get_by_label('담당자 필터').select_option('all')
                page.get_by_label('상태 필터').select_option('pending')
                expect(page.locator('.progress-item')).to_have_count(2)
                page.get_by_label('상태 필터').select_option('all')
                page.get_by_label('요구사항 검색').fill('FactChat')
                expect(page.locator('.progress-item')).to_have_count(1)
                expect(page.locator('#requirement-W07')).to_be_visible()
                page.get_by_label('요구사항 검색').fill('')
                page.screenshot(path=str(output / 'progress-desktop.png'), full_page=True)
                page.reload()
                expect(page).to_have_url(url + '#progress')
                expect(page.locator('.progress-item')).to_have_count(18)
                page.locator('[data-view="results"]').click()
                expect(page.locator('#view-results')).to_be_visible()
                page.locator('[data-view="setup"]').click()
                expect(page.locator('#view-setup')).to_be_visible()
                page.locator('[data-view="progress"]').click()
                expect(page.locator('.progress-item')).to_have_count(18)

                changed = json.loads(json.dumps(data))
                changed['items'][0]['status'] = '완료(구현)'
                changed['as_of'] = '테스트용 갱신'
                route_pattern = '**/data/requirements.json'
                page.route(route_pattern, lambda route: route.fulfill(
                    status=200, content_type='application/json; charset=utf-8',
                    body=json.dumps(changed, ensure_ascii=False)))
                page.locator('[data-view="setup"]').click()
                page.locator('[data-view="progress"]').click()
                expect(page.locator('.progress-date')).to_contain_text('테스트용 갱신')
                expect(page.locator('.progress-metric').nth(1)).to_contain_text('4')
                expect(page.locator('#requirement-W01')).to_contain_text('완료(구현)')
                page.unroute(route_pattern)
                page.locator('[data-view="setup"]').click()
                page.locator('[data-view="progress"]').click()
                expect(page.locator('.progress-metric').nth(1)).to_contain_text('3')
                assert data_path.read_bytes() == original_data

                page.set_viewport_size({'width': 390, 'height': 844})
                expect(page.locator('#view-progress')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'progress-mobile.png'), full_page=True)
                assert not errors and not writes, (errors, writes)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    print('PASS: progress navigation, 18 items, owner/status/search filters, direct URL, JSON refresh, mobile layout, no model calls')


if __name__ == '__main__':
    main()
