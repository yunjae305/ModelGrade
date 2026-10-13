"""Optional browser smoke test for external answer import: python tests/workspace_external_import_ui_smoke.py.

Requires Playwright and Chromium. Imports a web transcript and an API response file, previews them, evaluates with
the real CODE engine and a fixture semantic judge (no paid calls), and checks the environment display.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from playwright.sync_api import expect, sync_playwright
from semantic_judge_fixture import Judge
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server

WEB = {"schema": "modelgrade-transcript/v1",
       "system": {"system_key": "chatgpt-web", "provider": "openai", "platform": "ChatGPT", "model": "gpt-5", "access_mode": "web"},
       "conversations": [{"id": "c1", "messages": [
           {"role": "user", "content": "서울 인구를 알려줘"},
           {"role": "assistant", "format": "html", "created_at": "2026-10-12T01:00:00Z",
            "content": "<p>서울 인구는 약 930만 명입니다.</p>",
            "sources": [{"url": "https://data.seoul.go.kr/p?utm_source=chatgpt.com", "claims": ["서울 인구는 약 930만 명입니다."]}]}]}]}
API = {"schema": "modelgrade-api-responses/v1",
       "system": {"system_key": "gpt-api-search", "platform": "OpenAI API", "label": "GPT API (검색)"},
       "items": [{"case_id": "Q1", "question": "서울 인구를 알려줘", "request": {"tools": [{"type": "web_search"}]},
                  "response": {"object": "response", "status": "completed", "model": "gpt-5", "created_at": 1760600000,
                               "output": [{"type": "web_search_call"}, {"type": "message", "content": [
                                   {"type": "output_text", "text": "서울 인구는 약 930만 명입니다."}]}],
                               "usage": {"input_tokens": 9, "output_tokens": 12}}}]}


def main():
    screenshots = ROOT / 'results' / 'ui-preview'
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='aiona-external-ui-') as temporary:
        path = Path(temporary)
        judge = Judge()
        evaluator = CodeEvalService(save_dir=path / 'eval', jev_resolver=lambda **_: (judge, judge.config, None))
        comparison = ComparisonService(evaluator, save_dir=path / 'compare')
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=comparison)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        web, api = path / 'chatgpt-web.json', path / 'gpt-api.json'
        web.write_text(json.dumps(WEB, ensure_ascii=False), encoding='utf-8')
        api.write_text(json.dumps(API, ensure_ascii=False), encoding='utf-8')
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{server.server_port}/workspace.html')
                expect(page.locator('#server-status')).to_contain_text('연결됨')
                expect(page.locator('#external-start')).to_be_disabled()
                page.locator('#external-files').set_input_files([str(web), str(api)])
                expect(page.locator('.external-systems tbody tr')).to_have_count(2)
                expect(page.locator('#external-preview')).to_contain_text('실행 환경이 다릅니다')
                expect(page.locator('#external-preview')).to_contain_text('응답 생성 경로')
                expect(page.locator('#external-start')).to_be_enabled()
                assert not judge.calls   # 미리 확인은 평가자를 부르지 않는다
                for width in (390, 1024):
                    page.set_viewport_size({'width': width, 'height': 900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                page.set_viewport_size({'width': 1440, 'height': 1080})
                page.locator('#external-import').screenshot(path=str(screenshots / 'external-import-preview.png'))
                page.locator('#external-start').click()
                expect(page.locator('#view-results')).to_be_visible()
                expect(page.locator('#final-summary')).to_be_visible(timeout=15000)
                expect(page.locator('#final-summary-title')).to_contain_text('실행 환경이 달라 순위를 매기지 않았습니다')
                expect(page.locator('#summary-ranking')).to_contain_text('환경 상이')
                expect(page.locator('#external-environment')).to_contain_text('실행 환경이 다릅니다')
                expect(page.locator('#external-environment')).to_contain_text('평가 엔진')
                expect(page.locator('.result-model-card')).to_have_count(2)
                expect(page.locator('.result-model-card').first).to_contain_text('웹검색 허용')
                expect(page.locator('.result-model-card').nth(1)).to_contain_text('API')
                expect(page.locator('#partial-comparison')).to_be_hidden()
                page.locator('.result-cell').first.click()
                expect(page.locator('#answer-dialog .collection-detail')).to_contain_text('수집 정보')
                page.locator('#answer-dialog .collection-detail summary').first.click()
                expect(page.locator('#answer-dialog .collection-detail')).to_contain_text('플랫폼 유입 표시 제거')
                page.keyboard.press('Escape')
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=str(screenshots / 'external-import-results.png'), full_page=True)
                # 이력 관리는 AIONA 결과 이력만 보여 준다 — 외부 수집 실행은 나오지 않는다
                page.locator('[data-view="history"]').click()
                expect(page.locator('#view-history')).to_be_visible()
                expect(page.locator('#view-history')).not_to_contain_text('GPT API (검색)')
                assert len(judge.calls) == 2, judge.calls
                page.wait_for_load_state('networkidle')
                browser.close()
        finally:
            server.shutdown()
        assert not errors, errors
    print('external import UI smoke passed')


if __name__ == '__main__':
    main()
