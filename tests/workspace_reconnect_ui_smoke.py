"""Interrupted connections recover through GET only. No real models or saved runs."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright
from code_eval.engine import evaluate_batch
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server


def main():
    def forbidden(*args, **kwargs):
        raise AssertionError('This regression must never call a provider or evaluator')

    with tempfile.TemporaryDirectory(prefix='aiona-reconnect-ui-') as directory:
        folder = Path(directory)
        evaluator = CodeEvalService(save_dir=folder / 'eval', jev_resolver=forbidden)
        service = ComparisonService(evaluator, save_dir=folder / 'runs', client_factory=forbidden)
        cases = [{'case_id': 'RECONNECT1', 'question': '연결 상태를 확인합니다.', 'answer': '모의 응답 원문입니다.'}]
        rid = service.start({'cases': cases, 'name': '재접속 검증용 모의 실행'})['run_id']
        other = service.start({'cases': cases, 'name': '다른 실행 검증용'})['run_id']
        run = service._runs[rid]
        batch = evaluate_batch(cases)
        batch.update(live=True, jev_eval={'status': 'RUNNING', 'criteria': [], 'records': [None]})

        def running():
            run.update(status='EVALUATING', phase='EVALUATE', error=None, evaluations={'aiona': batch},
                revision=run.get('revision', 0) + 1, eval_done=0, eval_total=1,
                eval_started_at=datetime.now(timezone.utc).isoformat(),
                evaluation_progress={'aiona': {'stage': 'JEV', 'code_done': 1, 'code_total': 1,
                    'semantic_done': 0, 'semantic_total': 1, 'semantic_success': 0, 'semantic_errors': 0,
                    'semantic_unverified': 0, 'skipped': 0, 'active': [0], 'latest': None}})
            stream = service.event_stream(rid)
            stream.publish({'type': 'run_start', 'total': 1})
            stream.publish({'type': 'criterion_start', 'model': 'AIONA', 'index': 0,
                'qid': 'RECONNECT1', 'code': 'J5', 'name': '신뢰도', 'stage': 'LLM', 'together': True})

        running()
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        connection = {'offline': False, 'status_requests': [], 'failed_requests': []}
        errors, writes = [], []

        def run_route(route):
            path = route.request.url.split('?')[0]
            is_status = path.endswith('/' + rid)
            if is_status:
                connection['status_requests'].append(time.monotonic())
            if connection['offline'] and (is_status or path.endswith('/' + rid + '/result')):
                if is_status:
                    connection['failed_requests'].append(time.monotonic())
                if connection['offline'] == 'http503':
                    route.fulfill(status=503, json={'error': '오프라인 재시작 검증용 응답'})
                else:
                    route.abort('connectionrefused')
            else:
                route.continue_()

        def setup_route(route):
            response = route.fetch()
            body = response.json()
            body['semantic_setup'] = {'engine': 'offline_fixture', 'engine_label': '오프라인 검증용',
                'configuration_status': 'configured', 'can_evaluate': True,
                'connection_status': 'not_checked', 'search_status': 'not_checked'}
            route.fulfill(response=response, json=body)

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('request', lambda r: writes.append(r.url) if r.method != 'GET' else None)
                page.route('**/api/comparison/runs/' + rid + '**', run_route)
                page.route('**/api/code-eval/info', setup_route)
                page.route('**/offline-events', lambda route: route.abort('connectionrefused'))
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base + '/workspace.html')
                expect(page.locator('#server-status')).to_contain_text('로컬 서버 연결됨')
                page.evaluate('(rid) => sessionStorage.setItem("aiona-comparison-run", rid)', rid)
                page.goto(base + '/workspace.html#results'); page.reload()
                expect(page.locator('#progress-title')).to_have_text('평가 중')

                connection['offline'] = True
                expect(page.locator('#progress-title')).to_have_text('서버 연결 확인 중')
                expect(page.locator('#server-status')).to_contain_text('서버 연결 확인')
                expect(page.locator('#global-error')).to_contain_text('자동으로 다시 연결')
                expect(page.locator('#live-evaluation .live-heading')).to_contain_text('서버에 다시 연결하고 있습니다.')
                frozen = page.locator('#live-elapsed').inner_text()
                page.wait_for_timeout(1200)
                expect(page.locator('#live-elapsed')).to_have_text(frozen)
                # The increasing intervals cap at five seconds; all are GETs.
                deadline = time.monotonic() + 16
                while len(connection['failed_requests']) < 5 and time.monotonic() < deadline:
                    page.wait_for_timeout(100)
                assert len(connection['failed_requests']) >= 5, connection
                intervals = [b - a for a, b in zip(connection['failed_requests'], connection['failed_requests'][1:])]
                for actual, expected in zip(intervals[:4], (1, 2, 4, 5)):
                    assert expected - .2 <= actual <= expected + 1.5, intervals
                screenshots = ROOT / 'results/ui-preview'
                screenshots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshots / 'workspace-reconnect-offline-test.png'), full_page=True)

                run.update(status='CANCELLED', error='서버 종료로 평가가 중단되었습니다.', revision=run['revision'] + 1)
                service._clear_inflight(run)
                service.event_stream(rid).publish({'type': 'run_error', 'reason': run['error']})
                connection['offline'] = False
                expect(page.locator('#progress-title')).to_have_text('평가 중단', timeout=7000)
                expect(page.locator('#server-status')).to_contain_text('로컬 서버 연결됨')
                expect(page.locator('#global-error')).to_be_hidden()
                expect(page.locator('#live-evaluation')).to_be_visible()
                ended_elapsed = page.locator('#live-elapsed').inner_text()
                page.wait_for_timeout(1200)
                expect(page.locator('#live-elapsed')).to_have_text(ended_elapsed)
                expect(page.locator('#evaluate-button')).to_be_enabled()
                expect(page.locator('#progress-description')).to_contain_text('저장된 답변 전체를 다시 평가')
                page.locator('#matrix-body .result-cell').first.click()
                expect(page.locator('#answer-evaluation')).to_contain_text('평가 중단')
                page.locator('#answer-dialog [data-close]').click()
                page.locator('[data-view="history"]').click()
                expect(page.locator('#history-body').get_by_text('평가 중단', exact=True)).to_be_visible()
                connection['offline'] = True
                page.locator('#history-body').get_by_role('button', name='재접속 검증용 모의 실행', exact=True).click()
                expect(page.locator('#global-error')).to_contain_text('자동으로 다시 연결')
                connection['offline'] = False
                expect(page.locator('#view-results')).to_be_visible(timeout=4000)
                expect(page.locator('#global-error')).to_be_hidden()
                page.screenshot(path=str(screenshots / 'workspace-reconnect-recovered-test.png'), full_page=True)

                # Manual refresh invalidates old retry loops; it never multiplies polling.
                running()
                page.evaluate('(rid) => { void AionaWorkspace.openRun(rid); }', rid)
                expect(page.locator('#progress-title')).to_have_text('평가 중')
                connection['offline'] = 'http503'
                expect(page.locator('#progress-title')).to_have_text('서버 연결 확인 중')
                for _ in range(3):
                    page.locator('#refresh-results').click()
                    page.wait_for_timeout(100)
                connection['offline'] = False
                expect(page.locator('#progress-title')).to_have_text('평가 중', timeout=4000)
                expect(page.locator('#global-error')).to_be_hidden()
                expect(page.locator('#live-evaluation .live-heading')).to_contain_text('실시간 진행')
                expect(page.locator('#live-evaluation .live-heading > span:last-child')).to_be_hidden()
                start_count = len(connection['status_requests'])
                page.wait_for_timeout(2250)
                assert 1 <= len(connection['status_requests']) - start_count <= 3
                page.evaluate('(rid) => { void AionaWorkspace.openRun(rid); }', other)
                expect(page.locator('#result-subtitle')).to_contain_text('다른 실행 검증용')
                start_count = len(connection['status_requests'])
                page.wait_for_timeout(1300)
                assert len(connection['status_requests']) == start_count

                # An interrupted initial result load also reconnects without a reload.
                connection['offline'] = True
                page.evaluate('(rid) => sessionStorage.setItem("aiona-comparison-run", rid)', rid)
                page.reload()
                expect(page.locator('#global-error')).to_contain_text('자동으로 다시 연결')
                run.update(status='ERROR', error='서버 재시작으로 평가가 중단되었습니다.', revision=run['revision'] + 1)
                service._clear_inflight(run)
                service.event_stream(rid).publish({'type': 'run_error', 'reason': run['error']})
                connection['offline'] = False
                expect(page.locator('#progress-title')).to_have_text('오류 확인 필요', timeout=7000)
                expect(page.locator('#global-error')).to_be_hidden()

                # SSE disconnects cannot promise that the server kept evaluating.
                page.evaluate('''() => {
                    const host=document.createElement('section');host.id='disconnect-fixture';document.body.append(host);
                    const fixture=new AionaEvalStream.EvalProgress(host);fixture.connect('/offline-events','offline');
                    fixture.source.onerror();fixture.disconnect();
                }''')
                expect(page.locator('#disconnect-fixture .eval-connection')).to_have_text('진행 연결 복구 중 · 서버 상태를 확인할 수 없습니다.')
                assert not writes, writes
                assert not errors, errors
                browser.close()
            print('Reconnect UI passed: GET-only 1/2/4/5s backoff, frozen disconnected clock, automatic CANCELLED/ERROR recovery, history/retry wording, three manual refreshes without duplicate loops, switching runs, initial-load recovery and truthful SSE status. No model calls or paid POSTs.')
        finally:
            server.shutdown(); server.server_close()


if __name__ == '__main__':
    main()
