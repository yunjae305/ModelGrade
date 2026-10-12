"""Offline demo navigation/readiness regression checks. No model calls or saved-run writes."""
from __future__ import annotations

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
    saved = ROOT / 'backups/run-history/20261012-105105/comparison_runs/a3b5d89bc041.json'
    original = saved.read_bytes()
    seed = json.loads(original)
    ended = copy.deepcopy(seed)
    ended.update(run_id='ddddddddddd2', name='미완료 동선 검증용 — 실제 평가 아님', providers=['aiona'],
                 aiona_release={'version': '10.1', 'notes': 'Offline UI fixture'})
    ended['datasets'] = {'aiona': ended['datasets']['aiona']}
    ended['evaluations'] = {'aiona': ended['evaluations']['aiona']}
    for key in ('blind', 'blind_requested', 'evaluation_log'):
        ended.pop(key, None)
    ended['blind'] = {'enabled': True, 'order': ['aiona'], 'labels': {'aiona': '모델 A'}}
    pending = ended['evaluations']['aiona']['jev_eval']['records'][0]
    pending.update(status='SUCCESS', criteria=['J1'], decisions={
        'J1': {'decision': None, 'status': 'UNVERIFIED', 'reason': 'UI 검증용 근거 부족'}})
    live = copy.deepcopy(seed)
    live.update(run_id='ddddddddddd1', name='시연 동선 검증용 — 실제 평가 아님', status='EVALUATING',
                providers=['aiona', 'gpt'], eval_started_at='2026-10-09T01:00:00Z')
    for key in ('blind', 'blind_requested', 'evaluation_log'):
        live.pop(key, None)
    live['datasets'] = {p: live['datasets'][p] for p in live['providers']}
    live['evaluations'] = {p: live['evaluations'][p] for p in live['providers']}
    record = live['evaluations']['aiona']['jev_eval']['records'][0]
    record['criteria'] = ['J1']
    record['decisions'] = {'J1': {'decision': 'PASS', 'reason': '오프라인 검증용'},
                           'J5': {'decision': None, 'status': 'NOT_APPLICABLE', 'reason': '검증할 사실 없음'}}
    record['status'] = 'SUCCESS'
    progress = {'stage': 'JEV', 'code_done': 165, 'code_total': 165, 'semantic_done': 3,
                'semantic_total': 165, 'semantic_success': 1, 'semantic_errors': 0,
                'skipped': 0, 'active': [], 'latest': None}
    live['evaluation_progress'] = {'aiona': dict(progress, semantic_unverified=2, latest={
        'index': 0, 'case_id': record['case_id'], 'status': 'SUCCESS', 'updated_at': '2026-10-09T01:01:00Z'}),
        'gpt': dict(progress)}  # Older records legitimately omit semantic_unverified.
    setup = {'engine': 'openai_api', 'engine_label': 'OpenAI API', 'model': 'offline-ui-fixture',
             'configuration_status': 'missing', 'can_evaluate': False, 'connection_status': 'not_checked',
             'connection_message': '실제 연결·로그인·모델 접근은 평가 실행 시 확인합니다.',
             'search_status': 'configured', 'search_message': '웹 검색 허용 설정입니다. 실제 검색 연결은 확인하지 않았습니다.',
             'reason': '오프라인 검증용: 의미 평가 API 키가 없습니다.'}
    errors, paid_writes, external = [], [], []

    def forbidden(*args, **kwargs):
        raise AssertionError('Provider/evaluator calls are forbidden in demo flow checks')

    with tempfile.TemporaryDirectory(prefix='aiona-demo-flow-') as directory:
        path = Path(directory)
        evaluator = CodeEvalService(save_dir=path / 'eval', jev_resolver=forbidden)
        service = ComparisonService(evaluator, save_dir=path / 'runs', client_factory=forbidden)
        service._runs.update({seed['run_id']: seed, live['run_id']: live, ended['run_id']: ended})
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        info = evaluator.info()
        comparison_info = service.info()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.on('pageerror', lambda e: errors.append(str(e)))
                base = f'http://127.0.0.1:{server.server_port}'

                def route_request(route):
                    request = route.request
                    if not request.url.startswith(base + '/'):
                        external.append(request.url)
                        return route.abort()
                    if request.method != 'GET' and '/api/comparison/dataset' not in request.url:
                        paid_writes.append(request.url)
                        return route.fulfill(status=409, json={'error': 'No execution allowed in fixture'})
                    if request.url.endswith('/api/code-eval/info'):
                        result = dict(info)
                        if setup is None:
                            result.pop('semantic_setup', None)
                        else:
                            result['semantic_setup'] = setup
                        return route.fulfill(json=result)
                    if request.url.endswith('/api/comparison/info'):
                        return route.fulfill(json=comparison_info)
                    if request.url.endswith('/api/comparison/providers'):
                        return route.fulfill(json={p: {'configured': True, 'model': 'offline-ui-fixture'}
                                                  for p in ('aiona', 'gpt', 'claude', 'gemini')})
                    return route.continue_()

                page.route('**/*', route_request)
                page.goto(base + '/workspace.html')
                expect(page.locator('#semantic-setup-note')).to_contain_text('의미 평가 API 키가 없습니다')
                expect(page.locator('#sample-button')).to_have_text('예제 질문 불러오기')
                screenshots = ROOT / 'results' / 'ui-preview'
                screenshots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshots / 'workspace-demo-setup-desktop.png'))
                page.locator('#sample-button').click()
                expect(page.locator('#question-count')).to_contain_text('165')
                expect(page.locator('#collect-button')).to_be_disabled()
                expect(page.locator('#ready-detail')).to_contain_text('평가자 설정이 필요')

                # Saved results are available even when a new judge cannot run.
                page.locator('#saved-results-button').click()
                expect(page.locator('#view-history')).to_be_visible()
                assert page.evaluate("sessionStorage.getItem('aiona-workspace-view')") == 'history'
                page.locator('#history-body tr').filter(has_text=seed['name']).get_by_role('button', name='상세 보기', exact=True).click()
                expect(page.locator('#history-detail-dialog')).to_be_visible()
                page.locator('#history-detail-dialog').get_by_role('button', name='저장된 평가 결과 열기', exact=True).click()
                expect(page.locator('#final-summary')).to_be_visible()
                expect(page.locator('#final-summary')).to_contain_text('96.4')
                expect(page.locator('#blind-status')).to_have_text('평가 완료 · 실제 모델명 공개')
                expect(page.locator('#final-summary-evidence')).to_contain_text('저장 당시 결과')
                expect(page.locator('#final-summary-evidence')).to_contain_text('웹 검색을 사용하지 않은')
                assert page.evaluate("sessionStorage.getItem('aiona-workspace-view')") == 'results'

                page.locator('[data-view=setup]').click()
                setup.update(configuration_status='configured', can_evaluate=True, reason=None)
                page.locator('#refresh-semantic-setup').click()
                expect(page.locator('#semantic-setup-note')).to_contain_text('연결은 평가 시작 시 확인합니다.')
                expect(page.locator('#collect-button')).to_be_enabled()

                # A stale running server must never launch another evaluation
                # with the retired counting/evidence policy. Saved results stay
                # readable, and upgrading to policy 4 restores readiness.
                for policy in (None, 1, 2, 3):
                    if policy is None:
                        comparison_info.pop('evaluation_policy_version', None)
                    else:
                        comparison_info['evaluation_policy_version'] = policy
                    page.locator('#refresh-semantic-setup').click()
                    expect(page.locator('#global-error')).to_contain_text('서버를 재시작')
                    expect(page.locator('#collect-button')).to_be_disabled()
                    expect(page.locator('#evaluate-button')).to_be_disabled()
                    assert page.evaluate('AionaWorkspace.canEvaluate()') is False
                    page.evaluate('(id) => AionaWorkspace.openRun(id)', seed['run_id'])
                    expect(page.locator('#view-results')).to_be_visible()
                    expect(page.locator('#final-summary')).to_contain_text('96.4')
                    assert page.evaluate('AionaWorkspace.canEvaluate()') is False
                    page.locator('[data-view=setup]').click()
                    comparison_info['evaluation_policy_version'] = 4
                    page.locator('#refresh-semantic-setup').click()
                    expect(page.locator('#collect-button')).to_be_enabled()
                    expect(page.locator('#global-error')).to_be_hidden()
                    assert page.evaluate('AionaWorkspace.canEvaluate()') is True

                for width in (320, 390, 768, 1024, 1440):
                    page.set_viewport_size({'width': width, 'height': 900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                    if width == 390:
                        page.evaluate('window.scrollTo(0, 0)')
                        page.screenshot(path=str(screenshots / 'workspace-demo-setup-mobile.png'))
                expect(page.locator('.sidebar [data-view=connections]')).to_have_count(0)
                expect(page.locator('#semantic-setup-note')).to_contain_text('offline-ui-fixture')

                # New-version mode gives a distinct explanation and obeys readiness.
                page.evaluate("AionaWorkspace.prepareVersion('10.2')")
                expect(page.locator('#setup-description')).to_contain_text('저장된 이전 실행과 비교')
                page.locator('#dataset-file').set_input_files({'name': 'demo-fixture.json', 'mimeType': 'application/json',
                    'buffer': json.dumps([{'question': '오프라인 질문', 'answer': '오프라인 답변'}]).encode()})
                expect(page.locator('#version-evaluate-button')).to_be_enabled()
                comparison_info['evaluation_policy_version'] = 1
                page.locator('#refresh-semantic-setup').click()
                expect(page.locator('#version-evaluate-button')).to_be_disabled()
                expect(page.locator('#global-error')).to_contain_text('서버를 재시작')
                comparison_info['evaluation_policy_version'] = 4
                page.locator('#refresh-semantic-setup').click()
                expect(page.locator('#version-evaluate-button')).to_be_enabled()
                setup.update(configuration_status='missing', can_evaluate=False, reason='검증용 미설정')
                page.locator('#refresh-semantic-setup').click()
                expect(page.locator('#version-evaluate-button')).to_be_disabled()
                expect(page.locator('#semantic-setup-note')).to_contain_text('검증용 미설정')
                setup = None
                page.locator('#refresh-semantic-setup').click()
                expect(page.locator('#semantic-setup-note')).to_contain_text('이 서버는 평가자 설정 정보를 제공하지 않습니다')
                expect(page.locator('#version-evaluate-button')).to_be_enabled()

                # A J5 N/A result is complete; omitted legacy counter fields add zero.
                page.evaluate('(id) => sessionStorage.setItem("aiona-comparison-run", id)', live['run_id'])
                page.goto(base + '/workspace.html#results')
                page.reload()
                expect(page.locator('#live-latest')).to_contain_text('통과')
                expect(page.locator('#live-latest')).not_to_contain_text('판정 오류')
                expect(page.locator('#live-semantic-detail')).to_contain_text('확인 필요 2')
                page.evaluate('(id) => sessionStorage.setItem("aiona-comparison-run", id)', ended['run_id'])
                page.reload()
                expect(page.locator('#final-summary-description')).to_contain_text('1개 답변이 미완료입니다.')
                expect(page.locator('#final-summary-description')).to_contain_text('근거 부족·오류 원인을 확인하세요.')
                expect(page.locator('#final-summary-description')).not_to_contain_text('끝나면')
                expect(page.locator('#blind-status')).to_have_text('평가 작업 종료 · 실제 모델명 공개')
                expect(page.locator('#live-evaluation')).to_be_visible()
                expect(page.locator('#progress-title')).to_have_text('평가 미완료')
                page.locator('#final-summary-followup').click()
                expect(page.locator('#view-comparison')).to_be_visible()
                expect(page.locator('#version-target-run')).to_have_value(ended['run_id'])
                expect(page.locator('#version-summary')).to_have_attribute('aria-busy', 'false')
                expect(page.locator('#comparison-error')).to_be_hidden()
                assert not errors, errors
                assert not paid_writes, paid_writes
                assert not external, external
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    assert hashlib.sha256(saved.read_bytes()).digest() == hashlib.sha256(original).digest()
    print('Demo flow passed: missing/policy-1/policy-2/policy-3 server blocks new comparisons and version evaluations with restart guidance while saved results stay readable; policy-4 restores readiness; setup retry/legacy compatibility, version explanation, 5 layouts, J5 N/A latest result, mixed legacy counts; no execution/external calls or original changes.')


if __name__ == '__main__':
    main()
