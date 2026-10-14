"""Criteria-only workspace navigation, saved-result preservation; no model requests."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
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
    saved = ROOT / 'backups/run-history/20261012-105105/comparison_runs/a3b5d89bc041.json'
    original = saved.read_bytes()
    baseline = json.loads(original)
    baseline['aiona_release'] = {'version': '10.1', 'notes': 'Isolated navigation fixture'}
    errors, writes, criteria_reads = [], [], []
    output = ROOT / 'results/criteria-only-review'
    output.mkdir(parents=True, exist_ok=True)

    def forbidden(*args, **kwargs):
        raise AssertionError('Demo browsing must not invoke a model')

    with tempfile.TemporaryDirectory(prefix='aiona-reference-flow-') as directory:
        evaluator = CodeEvalService(save_dir=Path(directory) / 'eval', jev_resolver=forbidden)
        service = ComparisonService(evaluator, save_dir=Path(directory) / 'runs', client_factory=forbidden)
        service._runs[baseline['run_id']] = copy.deepcopy(baseline)
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: writes.append(request.url) if request.method != 'GET' else None)
                page.on('request', lambda request: criteria_reads.append(request.url) if request.url.endswith('/api/code-eval/info') else None)
                workspace_url = f'http://127.0.0.1:{server.server_port}/workspace.html'
                page.goto(workspace_url)
                page.get_by_role('button', name='저장된 결과 보기', exact=True).click()
                expect(page.locator('#view-history')).to_be_visible()
                page.locator('#history-body').get_by_role('button', name='상세 보기', exact=True).click()
                expect(page.locator('#history-detail-dialog')).to_be_visible()
                page.locator('#history-detail-dialog').get_by_role('button', name='저장된 평가 결과 열기', exact=True).click()
                expect(page.locator('#final-summary')).to_contain_text('96.4')
                # Failed read has a retry action and never leaves misleading partial definitions.
                criteria_endpoint = '**/api/code-eval/info'
                page.route(criteria_endpoint, lambda route: route.fulfill(
                    status=503, content_type='application/json', body='{"error":"isolated retry fixture"}'))
                page.get_by_role('link', name='평가 기준', exact=True).click()
                expect(page).to_have_url(workspace_url + '#criteria')
                expect(page.locator('#view-criteria')).to_be_visible()
                expect(page.locator('#criteria-reference-error')).to_be_visible()
                expect(page.locator('#criteria-reference-content')).to_be_empty()
                expect(page.locator('#criteria-reference-retry')).to_be_visible()
                page.unroute(criteria_endpoint)
                page.locator('#criteria-reference-retry').click()
                expect(page.locator('#criteria-reference-title')).to_have_text('평가 기준')
                expect(page.locator('.page-view:visible h1')).to_have_count(1)
                expect(page.get_by_role('link', name='평가 기준', exact=True)).to_have_attribute('aria-current', 'page')
                criteria = page.locator('#criteria-reference-content')
                expected_criteria = {
                    '1-1': '응답·문자 정상성', '1-2': '문서·코드 유효성', '1-3': '참조 연결 유효성',
                    '2-1': '분량·표현 조건', '2-2': '형식·구성 조건',
                    '3-1': '계산 결과 일치', '3-2': '선언 개수 일치', '3-3': '원본 보존·변환 일치',
                    'J1': '의도 충족성', 'J2': '내용 적절성', 'J3': '논리 타당성', 'J4': '충분성',
                    'J5': '데이터 신뢰도', 'J6': '긴 대화 맥락 유지',
                    'AIONA-M': '내부 마커 노출', 'AIONA-F': '금지 표현 출현',
                }
                expect(criteria.locator('[data-criterion]')).to_have_count(len(expected_criteria))
                for criterion_id, title in expected_criteria.items():
                    expect(criteria.locator(f'[data-criterion="{criterion_id}"]')).to_contain_text(title)
                expect(criteria.locator('[data-criterion]:visible')).to_have_count(14)
                expect(criteria.locator('[data-criterion-state="paused"]')).to_have_count(2)
                expect(criteria.locator('.criteria-paused')).to_contain_text('현재 실행하지 않으며 점수와 평가 완료율에 포함하지 않습니다.')
                criteria.locator('.criteria-paused-definitions summary').click()
                expect(criteria.locator('[data-criterion-state="paused"]:visible')).to_have_count(2)
                expect(criteria.locator('[data-criterion-state="paused"]:visible').first).to_contain_text('평가 대기 · 현재 미실시')
                criteria.locator('.criteria-paused-definitions summary').click()
                expect(page.locator('#criteria-reference-error')).to_be_hidden()
                expect(page.locator('#criteria-reference-retry')).to_be_hidden()
                for label, target in (
                    ('CODE평가', '#criteria-common-title'),
                    ('LLM의미평가', '#criteria-semantic-title'),
                    ('AIONA 특화 기준', '#criteria-specialized-title'),
                ):
                    criteria.get_by_role('button', name=re.compile(label)).click()
                    expect(page.locator(target)).to_be_focused()
                    expect(page).to_have_url(workspace_url + '#criteria')
                text = criteria.inner_text()
                for clutter in ('구현 위치', 'API 키', 'API키', '모델 연결 설정', '소스파일', '소스 파일', 'TYPESAFE_API_KEY'):
                    assert clutter not in text, clutter
                assert not re.search(r'code_eval[/\\]|\.py\b', text), text
                expect(criteria.locator('input, textarea, select')).to_have_count(0)
                expect(page.get_by_role('link', name='평가 상세 도구', exact=True)).to_have_count(0)
                cached_read_count = len(criteria_reads)
                page.locator('button[data-view="results"]').click()
                expect(page.locator('#view-results')).to_be_visible()
                expect(page.locator('button[data-view="results"]')).to_have_attribute('aria-current', 'page')
                expect(page.get_by_role('link', name='평가 기준', exact=True)).not_to_have_attribute('aria-current', 'page')
                expect(page.locator('#final-summary')).to_contain_text('96.4')

                page.locator('[data-view="versions"]').click()
                page.locator('[data-model-version="10.1"]').get_by_role('button', name='비교 실행 선택', exact=True).click()
                expect(page.locator('#view-comparison')).to_be_visible()
                expect(page.locator('#version-baseline-version')).to_have_value('10.1')
                expect(page.locator('#version-baseline-model')).to_have_value(baseline['run_id'])
                expect(page.locator('#version-baseline-run')).to_have_value(baseline['run_id'])
                expect(page.locator('#version-score-cards')).to_contain_text('96.4')
                page.get_by_role('link', name='평가 기준', exact=True).click()
                expect(page.locator('#view-criteria')).to_be_visible()
                expect(criteria.locator('[data-criterion]')).to_have_count(len(expected_criteria))
                assert len(criteria_reads) == cached_read_count, 'Revisiting cached criteria made another request'
                page.reload()
                expect(page).to_have_url(workspace_url + '#criteria')
                expect(page.locator('#view-criteria')).to_be_visible()
                expect(criteria.locator('[data-criterion]')).to_have_count(len(expected_criteria))
                expect(page.get_by_role('link', name='평가 기준', exact=True)).to_have_attribute('aria-current', 'page')
                page.screenshot(path=str(output / 'criteria-only-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                expect(page.locator('#criteria-reference-title')).to_be_visible()
                expect(page.get_by_role('link', name='평가 기준', exact=True)).to_be_visible()
                expect(page.get_by_role('link', name='평가 상세 도구', exact=True)).to_have_count(0)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'criteria-only-mobile.png'), full_page=True)
                page.screenshot(path=str(output / 'criteria-only-mobile-top.png'))
                page.locator('button[data-view="results"]').click()
                expect(page.locator('#view-results')).to_be_visible()
                expect(page.locator('#final-summary')).to_contain_text('96.4')
                page.evaluate("AionaWorkspace.go('comparison')")
                expect(page.locator('#view-comparison')).to_be_visible()
                expect(page.locator('button[data-view="results"]')).to_have_attribute('aria-current', 'page')
                expect(page.locator('#version-baseline-run')).to_have_value(baseline['run_id'])
                expect(page.locator('#version-score-cards')).to_contain_text('96.4')

                # The setup shortcut opens the same criteria-only page, including on mobile.
                page.locator('button[data-view="setup"]').click()
                page.get_by_role('link', name='기준 자세히', exact=True).click()
                expect(page).to_have_url(workspace_url + '#criteria')
                expect(page.locator('#view-criteria')).to_be_visible()
                expect(page.get_by_role('link', name='평가 기준', exact=True)).to_have_attribute('aria-current', 'page')
                expect(criteria.locator('[data-criterion]')).to_have_count(len(expected_criteria))
                assert not errors and not writes, (errors, writes)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    assert hashlib.sha256(saved.read_bytes()).digest() == hashlib.sha256(original).digest()
    print('PASS: saved results -> workspace criteria only -> preserved results/comparison, 8 CODE + 6 semantic + 2 paused specialized criteria, '
          'no operational clutter or detail-tool menu, retry, section jumps, cached revisit, direct-hash reload, desktop/mobile and setup shortcut; '
          'GET only, no model calls, original unchanged.')


if __name__ == '__main__':
    main()
