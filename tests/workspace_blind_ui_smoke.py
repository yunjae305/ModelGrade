"""Actual local HTTP/UI path with four-model test doubles; never calls real APIs."""
import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from playwright.sync_api import expect, sync_playwright
from test_blind_comparison import CapturingJudge, adapter_fixture, until
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server


class VariedFixtureJudge(CapturingJudge):
    def system_one(self, state, questions):
        result = super().system_one(state, questions)
        for criterion, entry in result.body['answers'].items():
            if (criterion == 'J3' and '셋' in state['answer']) or (criterion == 'J5' and '둘' in state['answer']):
                entry.update(choice='FAIL', probabilities={'PASS': .05, 'FAIL': .95}, reason='브라우저 검증용 대역 FAIL · 실제 품질 판정 아님')
        return result


def expect_upload_only(page):
    for field in ('aiona-version', 'aiona-collected-at', 'aiona-release-notes'):
        expect(page.locator('#' + field)).to_be_hidden()
        expect(page.locator('#' + field)).to_be_disabled()
        expect(page.locator('#' + field).locator('xpath=ancestor::*[@data-version-field][1]')).to_be_hidden()
    expect(page.locator('#collect-button')).to_be_visible()
    expect(page.locator('#version-evaluate-button')).to_be_hidden()


def main():
    desktop_only = '--desktop-only' in sys.argv
    screenshots = ROOT / 'results/ui-preview'
    screenshots.mkdir(parents=True, exist_ok=True)
    errors = []
    comparison_posts = []
    with tempfile.TemporaryDirectory(prefix='aiona-four-blind-') as directory:
        folder = Path(directory)
        judge = VariedFixtureJudge(pause=True)
        handler, sent, answers, _ = adapter_fixture()
        collection_entered = threading.Event()
        collection_release = {p: threading.Event() for p in ('gpt', 'claude', 'gemini')}
        def gated_handler(request):
            provider = {'api.openai.com': 'gpt', 'api.anthropic.com': 'claude', 'generativelanguage.googleapis.com': 'gemini'}[request.url.host]
            collection_entered.set()
            assert collection_release[provider].wait(60)
            return handler(request)
        evaluator = CodeEvalService(save_dir=folder / 'eval', jev_resolver=lambda: (judge, judge.config, None))
        service = ComparisonService(evaluator, save_dir=folder / 'runs',
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(gated_handler)))
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        full = service.sample()['cases']
        cases = [full[i] for i in (0, 2, 8, 10, 18, 24, 38, 54, 61, 86, 100, 101)]
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('request', lambda request: comparison_posts.append(request.post_data_json)
                        if request.method == 'POST' and request.url.endswith('/api/comparison/runs') else None)
                page.goto(base + '/workspace.html')
                expect(page.locator('#server-status')).to_contain_text('연결됨')
                expect_upload_only(page)
                page.screenshot(path=str(screenshots / 'workspace-upload-empty-test.png'), full_page=True)
                page.locator('.advanced-options > summary').click()
                expect_upload_only(page)
                page.locator('.advanced-options > summary').click()
                page.locator('#sample-button').click()
                expect(page.locator('#question-count')).to_contain_text('165')
                expect(page.locator('#aiona-version')).to_have_value('')
                expect_upload_only(page)
                # Returning from explicit version registration must not attach its
                # leftover metadata to a normal four-model comparison.
                page.locator('[data-view="versions"]').click()
                page.locator('#version-register-next').click()
                expect(page.locator('#aiona-version')).to_be_visible()
                expect(page.locator('#aiona-version')).to_be_enabled()
                expect(page.locator('#aiona-version')).to_have_value('10.2')
                page.locator('.advanced-options > summary').click()
                page.locator('#aiona-collected-at').fill('2026-10-11')
                page.locator('#aiona-release-notes').fill('전용 버전 모드의 메모 — 일반 비교 전송 금지')
                page.locator('#version-mode-exit').click()
                expect_upload_only(page)
                page.locator('.advanced-options > summary').click()
                page.locator('#dataset-file').set_input_files({'name': 'test.json', 'mimeType': 'application/json',
                    'buffer': json.dumps(cases, ensure_ascii=False).encode('utf-8')})
                expect(page.locator('#question-count')).to_contain_text('12')
                expect(page.locator('#aiona-only')).to_have_count(0)
                expect(page.locator('#setup-models input[type=checkbox]')).to_have_count(0)
                for index, provider in ((1, 'gemini'), (2, 'claude'), (3, 'gpt')):
                    expect(page.locator('#collect-button')).to_be_disabled()
                    page.locator('#setup-models .model-row').nth(index).get_by_role('button', name='연결하기').click()
                    page.locator('#api-key').fill('PRIVATE_KEY_' + provider)
                    page.locator('#model-id').fill('PRIVATE_PROVIDER_MODEL_' + provider)
                    page.locator('#save-key').click()
                    expect(page.locator('#key-dialog')).not_to_be_visible()
                assert not sent and not judge.calls  # registration never consumes a request
                page.reload()
                expect(page.locator('#model-selection-count')).to_have_text('4 / 4 연결')
                expect_upload_only(page)
                expect(page.locator('#setup-models input[type=checkbox]')).to_have_count(0)
                page.locator('[data-view="versions"]').click()
                page.locator('#version-register-next').click()
                page.locator('.advanced-options > summary').click()
                page.locator('#aiona-version').fill('10.2')
                page.locator('#aiona-collected-at').fill('2026-10-11')
                page.locator('#aiona-release-notes').fill('일반 비교 전송에서 제외할 전용 메타데이터')
                page.locator('#version-mode-exit').click()
                expect_upload_only(page)
                page.locator('.advanced-options > summary').click()
                page.locator('#dataset-file').set_input_files({'name': '브라우저 검증용 대역.json', 'mimeType': 'application/json',
                    'buffer': json.dumps(cases, ensure_ascii=False).encode('utf-8')})
                expect(page.locator('#question-count')).to_contain_text('12')
                expect(page.locator('#ready-detail')).to_contain_text('답변 36개 수집')
                expect(page.locator('#collect-button')).to_be_enabled()
                expect_upload_only(page)
                page.screenshot(path=str(screenshots / 'workspace-upload-only-test.png'), full_page=True)
                page.locator('.action-bar').screenshot(path=str(screenshots / 'workspace-required-four-start.png'))
                if not desktop_only:
                    page.set_viewport_size({'width': 390, 'height': 844})
                    button_box = page.locator('#collect-button').bounding_box()
                    action_box = page.locator('.action-buttons').bounding_box()
                    assert abs(button_box['width'] - action_box['width']) < 1
                    page.locator('.action-bar').screenshot(path=str(screenshots / 'workspace-required-four-start-mobile.png'))
                page.set_viewport_size({'width': 1440, 'height': 1080})
                page.locator('#collect-button').click()
                assert collection_entered.wait(5)
                assert len(comparison_posts) == 1 and 'aiona_release' not in comparison_posts[0]
                expect(page.locator('#progress-title')).to_have_text('응답 수집 중')
                expect(page.locator('#progress-description')).to_contain_text('수집이 끝나면')
                expect(page.locator('#blind-banner')).to_be_hidden()
                assert page.locator('.result-card-heading strong').all_text_contents() == ['AIONA', 'Gemini', 'Claude', 'GPT']
                expect(page.locator('.result-card-heading .pill')).to_have_text(['원문 준비됨', '답변 수집 중', '답변 수집 중', '답변 수집 중'])
                rid = page.evaluate('sessionStorage.getItem("aiona-comparison-run")')
                assert not service.result(rid)['evaluations'] and not judge.calls
                assert 'blind' not in service.result(rid)
                expect(page.locator('[data-collection-time="aiona"]')).to_have_text('업로드 원문 · 수집 없음')
                expect(page.locator('[data-collection-time="gpt"]')).not_to_have_text('00:00')
                collection_release['gemini'].set()
                until(lambda: service.status(rid)['progress']['gemini']['stage'] == 'COMPLETED')
                expect(page.locator('[data-provider="gemini"] .result-card-heading .pill')).to_have_text('수집 종료')
                frozen = page.locator('[data-collection-time="gemini"]').text_content()
                ticking = page.locator('[data-collection-time="gpt"]').text_content()
                expect(page.locator('[data-collection-time="gpt"]')).not_to_have_text(ticking)
                assert page.locator('[data-collection-time="gemini"]').text_content() == frozen
                page.reload()
                expect(page.locator('[data-collection-time="gemini"]')).to_have_text(frozen)
                page.locator('#result-models').screenshot(path=str(screenshots / 'workspace-collection-before-blind-test.png'))
                page.locator('#matrix-body .row-button').first.click()
                assert page.locator('#answer-tabs [role=tab]').all_text_contents() == ['AIONA', 'Gemini', 'Claude', 'GPT']
                for event in collection_release.values():
                    event.set()
                assert judge.entered.wait(5)
                expect(page.locator('#progress-title')).to_have_text('평가 중', timeout=10000)
                expect(page.locator('#answer-tabs [role=tab]')).to_have_text(['모델 A', '모델 B', '모델 C', '모델 D'])
                page.locator('#answer-dialog [data-close]').click()
                expect(page.locator('#reveal-models')).to_be_disabled()
                expect(page.locator('#evaluation-ticker')).to_be_visible()
                expect(page.locator('#evaluation-ticker .eval-rolling')).to_contain_text('의미 평가 응답 대기 · 여러 기준 함께 평가', timeout=10000)
                assert 'PRIVATE_PROVIDER_MODEL_' not in page.locator('#evaluation-ticker').inner_text()
                assert page.locator('.result-card-heading strong').all_text_contents() == ['모델 A', '모델 B', '모델 C', '모델 D']
                assert 'PRIVATE_PROVIDER_MODEL_' not in page.locator('#result-content').inner_text()
                assert page.locator('#criteria-head th').all_text_contents()[1:] == ['A모델 A', 'B모델 B', 'C모델 C', 'D모델 D']
                expect(page.locator('#criteria-body tr')).to_have_count(9)
                page.screenshot(path=str(screenshots / 'workspace-four-blind-live-test.png'), full_page=True)
                rid = page.evaluate('sessionStorage.getItem("aiona-comparison-run")')
                live = service.result(rid)
                assert live['done'] == 36 and live['auto_evaluate']
                aliases = live['blind']['labels']
                order = live['blind']['order']
                assert sorted(order) == ['aiona', 'claude', 'gemini', 'gpt']
                # Closing/reopening the page is read-only; the backend owns auto evaluation.
                page.reload()
                expect(page.locator('#progress-title')).to_have_text('평가 중')
                expect(page.locator('#evaluation-ticker .eval-rolling')).to_contain_text('의미 평가 응답 대기 · 여러 기준 함께 평가')
                assert service.status(rid)['blind'] == live['blind']
                judge.release.set()
                until(lambda: service.status(rid)['status'] == 'COMPLETED')
                expect(page.locator('#blind-status')).to_have_text('평가 완료 · 실제 모델명 공개', timeout=10000)
                expect(page.locator('#final-summary')).to_have_attribute('data-ready', 'true')
                expect(page.locator('#final-summary .summary-score-card')).to_have_count(4)
                expect(page.locator('#progress-title')).to_have_text('평가 완료')
                expect(page.locator('#live-evaluation')).to_be_visible()
                expect(page.locator('#live-semantic')).to_have_text('48 / 48개')
                expect(page.locator('#reveal-models')).to_be_hidden()
                expect(page.locator('#result-warning')).to_be_hidden()
                assert len(judge.calls) == 48
                expect(page.locator('#evaluation-ticker .eval-counter')).to_have_text('완료')
                expect(page.locator('#evaluation-ticker .eval-rolling')).to_contain_text('48개 답변 평가 종료')
                page.locator('#evaluation-ticker .eval-ticker').click()
                expect(page.locator('#evaluation-ticker .eval-log-row')).to_have_count(48)
                assert '모델 A' not in page.locator('#evaluation-ticker').inner_text()
                assert 'AIONA' in page.locator('#evaluation-ticker').inner_text()
                page.locator('#evaluation-ticker .eval-log-row > summary').first.click()
                expect(page.locator('#evaluation-ticker .eval-criteria-log .eval-criterion').first).to_be_visible()
                page.locator('#evaluation-ticker .eval-ticker').click()
                assert all(len(requests) == 12 for requests in sent.values())
                actual_names = [{'aiona': 'AIONA', 'gemini': 'Gemini', 'claude': 'Claude', 'gpt': 'GPT'}[p] for p in order]
                assert page.locator('.result-card-heading strong').all_text_contents() == actual_names
                assert page.locator('[data-collection-time="gemini"]').text_content() == frozen
                # Four desktop cards align in a single row; metrics have matching table rows.
                boxes = [c.bounding_box() for c in page.locator('.result-model-card').all()]
                assert max(b['y'] for b in boxes) - min(b['y'] for b in boxes) < 1
                assert all(page.locator('#criteria-body tr').nth(i).locator('td').count() == 4 for i in range(9))
                page.locator('#matrix-body .row-button').first.click()
                assert page.locator('#answer-tabs [role=tab]').all_text_contents() == actual_names
                assert page.locator('#original-answer pre').text_content() == service.result(rid)['datasets'][order[0]][0]['answer']
                page.get_by_role('tab', name='AIONA', exact=True).click()
                assert page.locator('#original-answer pre').text_content() == cases[0]['answer']
                page.locator('#answer-dialog [data-close]').click()
                page.locator('#matrix-search').fill('Q101')
                expect(page.locator('#matrix-body tr')).to_have_count(1)
                assert 'PRIVATE_PROVIDER_MODEL_gpt' in page.locator('#result-models').inner_text()
                assert page.locator('#matrix-search').input_value() == 'Q101'
                assert page.locator('#matrix-head th').all_text_contents()[2:] == [{'aiona': 'AIONA', 'gemini': 'Gemini', 'claude': 'Claude', 'gpt': 'GPT'}[p] for p in order]
                assert len(judge.calls) == 48
                page.locator('#clear-matrix-filter').click()
                page.locator('#toast').wait_for(state='hidden')
                for width in ((1280, 1440, 1680, 1920) if desktop_only else (320, 390, 768, 1024, 1440, 1680)):
                    page.set_viewport_size({'width': width, 'height': 1080})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'overflow at {width}'
                    assert page.locator('#criteria-body tr').count() == 9
                    page.evaluate('window.scrollTo(0,0)')
                    if width in (390, 1440, 1680):
                        page.screenshot(path=str(screenshots / f'workspace-four-blind-{width}-test.png'), full_page=True)
                    if width == 1680:
                        page.locator('#result-models').screenshot(path=str(screenshots / 'workspace-four-blind-cards-test.png'))
                        page.locator('#view-results .criteria-panel').screenshot(path=str(screenshots / 'workspace-four-blind-criteria-test.png'))
                with page.expect_download() as info:
                    page.locator('#export-all').click()
                archived = json.loads(Path(info.value.path()).read_text(encoding='utf-8'))
                assert archived['blind'] == live['blind']
                assert 'PRIVATE_KEY_' not in json.dumps(archived)
                for p in archived['providers']:
                    expected = [c['answer'] for c in cases] if p == 'aiona' else [answers[p, c['question']] for c in cases]
                    assert [c['answer'] for c in archived['datasets'][p]] == expected
                page.reload()
                expect(page.locator('#blind-status')).to_have_text('평가 완료 · 실제 모델명 공개')
                assert page.locator('.result-card-heading strong').all_text_contents() == actual_names
                assert len(judge.calls) == 48
                # Older partial records remain readable and can supply the original file for a new full run.
                legacy = service.start({'cases': cases, 'name': '기존 단독 실행', 'blind': True})
                page.evaluate('(id) => sessionStorage.setItem("aiona-comparison-run", id)', legacy['run_id'])
                page.reload()
                expect(page.locator('#partial-comparison')).to_be_visible()
                expect(page.locator('#partial-comparison-description')).to_contain_text('1개 모델')
                page.locator('#reuse-dataset').click()
                expect(page.locator('#question-count')).to_contain_text('12')
                expect(page.locator('#collect-button')).to_be_enabled()
                expect_upload_only(page)
                expect(page.locator('#aiona-version')).to_have_value('')
                expect(page.locator('#aiona-release-notes')).to_have_value('')
                assert len(service.history()) == 2 and len(judge.calls) == 48
                assert service.result(legacy['run_id'])['providers'] == ['aiona']
                # A running older backend with an existing /info route also requires restart.
                page.route('**/api/comparison/info', lambda route: route.fulfill(status=200,
                    content_type='application/json', body=json.dumps({'blind_version': 'comparison-blind-v1', 'auto_evaluate': True})))
                page.goto(base + '/workspace.html#setup')
                page.reload()
                expect(page.locator('#global-error')).to_contain_text('서버를 재시작')
                page.locator('#dataset-file').set_input_files({'name': 'test.json', 'mimeType': 'application/json',
                    'buffer': json.dumps(cases, ensure_ascii=False).encode('utf-8')})
                expect(page.locator('#question-count')).to_contain_text('12')
                expect(page.locator('#collect-button')).to_be_disabled()
                page.unroute('**/api/comparison/info')
                page.route('**/api/comparison/info', lambda route: route.fulfill(status=404,
                    content_type='application/json', body=json.dumps({'error': 'API를 찾을 수 없습니다.'})))
                page.goto(base + '/workspace.html')
                expect(page.locator('#global-error')).to_contain_text('이전 버전')
                page.locator('#dataset-file').set_input_files({'name': 'test.json', 'mimeType': 'application/json',
                    'buffer': json.dumps(cases, ensure_ascii=False).encode('utf-8')})
                expect(page.locator('#question-count')).to_contain_text('12')
                expect(page.locator('#collect-button')).to_be_disabled()
                expect(page.locator('#aiona-only')).to_have_count(0)
                assert len(judge.calls) == 48 and all(len(requests) == 12 for requests in sent.values())
                assert not errors, errors
                browser.close()
                print('Four-model UI passed: independent collection clocks, completed timer freeze and reload, blind judging, automatic final names, 48 mock judgments, 9 criterion rows, raw JSON preservation. Desktop-only: ' + str(desktop_only) + '. No real APIs called.')
        finally:
            for event in collection_release.values():
                event.set()
            judge.release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    main()
