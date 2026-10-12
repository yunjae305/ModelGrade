"""Offline 68 x 4 progress replay: processed counts never become verdict counts.

Uses one temporary server and in-memory fixtures. No provider calls or port 8080.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright
from code_eval.engine import evaluate_batch
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server


def main():
    providers = ['aiona', 'gemini', 'claude', 'gpt']
    completed = [41, 41, 41, 40]  # 163 judged answers, independently of 272 processed.
    partial = [64, 64, 63, 63]
    started = datetime.now(timezone.utc) - timedelta(seconds=60)
    terminal_at = (started + timedelta(seconds=45)).isoformat()
    errors, writes, external = [], [], []

    def forbidden(*args, **kwargs):
        raise AssertionError('Model calls are forbidden in progress fixtures.')

    with tempfile.TemporaryDirectory(prefix='aiona-counts-ui-') as directory:
        path = Path(directory)
        evaluator = CodeEvalService(save_dir=path / 'eval', jev_resolver=forbidden)
        service = ComparisonService(evaluator, save_dir=path / 'runs', client_factory=forbidden)
        source = [{'case_id': f'fixture-{i + 1}', 'question': 'Give a brief greeting.', 'answer': 'Hello.'} for i in range(68)]
        rid = service.start({'cases': source, 'providers': [], 'name': '68×4 처리 수 UI 모의 검증'})['run_id']
        run = service._runs[rid]
        run.update(providers=providers, status='EVALUATING', phase='EVALUATE', eval_total=4, eval_done=0,
                   eval_started_at=started.isoformat(), revision=1)
        batches = {}
        for provider in providers:
            run['models'][provider] = 'offline-ui-fixture'
            if provider != 'aiona':
                run['datasets'][provider] = [{**copy.deepcopy(row), 'collection': {'status': 'SUCCESS'}} for row in run['datasets']['aiona']]
            batch = evaluate_batch(run['datasets'][provider])
            batch['jev_eval'] = {'criteria': [{'id': 'J1', 'name': '질문 요청 충족'}], 'records': []}
            batches[provider] = batch

        def stage(name, processed, *, terminal=False, omit_progress=False):
            with service._lock:
                run.update(status='COMPLETED' if terminal else 'EVALUATING', revision=run['revision'] + 1,
                           eval_done=4 if terminal else 0, evaluation_progress={})
                if terminal:
                    run['eval_finished_at'] = terminal_at
                for pindex, provider in enumerate(providers):
                    records = []
                    for index in range(68):
                        if index >= processed[pindex]:
                            records.append(None)
                        elif index < completed[pindex]:
                            records.append({'status': 'SUCCESS', 'criteria': ['J1'], 'decisions': {'J1': {'decision': 'PASS'}}})
                        else:
                            records.append({'status': 'SUCCESS', 'criteria': ['J1'], 'decisions': {
                                'J1': {'decision': None, 'status': 'UNVERIFIED', 'reason': '오프라인 검증용 근거 부족'}}})
                    batch = copy.deepcopy(batches[provider])
                    batch['live'] = not terminal
                    batch['jev_eval']['records'] = records
                    run['evaluations'][provider] = batch
                    run['evaluation_progress'][provider] = {'stage': name, 'code_done': 68, 'code_total': 68,
                        'semantic_done': processed[pindex], 'semantic_total': 68,
                        'semantic_success': min(processed[pindex], completed[pindex]),
                        'semantic_unverified': max(0, processed[pindex] - completed[pindex]),
                        'semantic_errors': 0, 'skipped': 0, 'active': [], 'updated_at': terminal_at}
                if omit_progress:
                    run.pop('evaluation_progress')

        stage('CODE', [0, 0, 0, 0])
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.on('pageerror', lambda error: errors.append(str(error)))

                def read_only(route):
                    request = route.request
                    if request.method != 'GET':
                        writes.append(request.url)
                        route.abort()
                    elif not request.url.startswith(base + '/'):
                        external.append(request.url)
                        route.abort()
                    else:
                        route.continue_()

                page.route('**/*', read_only)
                page.add_init_script('sessionStorage.setItem("aiona-comparison-run", "' + rid + '")')
                page.goto(base + '/workspace.html#results')
                expect(page.locator('#live-code')).to_have_text('272 / 272개')
                expect(page.locator('#live-semantic')).to_have_text('0 / 272개')
                for provider in providers:
                    expect(page.locator(f'.result-model-card[data-provider="{provider}"] .result-metric strong')).to_have_text('0')
                expect(page.locator('#run-progress')).to_have_attribute('value', '272')
                expect(page.locator('#run-progress')).to_have_attribute('max', '544')

                stage('JEV', partial)
                expect(page.locator('#live-semantic')).to_have_text('254 / 272개', timeout=7000)
                expect(page.locator('#run-progress')).to_have_attribute('value', '526')
                for provider, done in zip(providers, partial):
                    expect(page.locator(f'.result-model-card[data-provider="{provider}"] .result-metric strong')).to_have_text(str(done))

                stage('COMPLETED', [68] * 4, terminal=True)
                expect(page.locator('#progress-title')).to_have_text('평가 미완료', timeout=7000)
                expect(page.locator('#live-evaluation')).to_be_visible()
                expect(page.locator('#live-semantic')).to_have_text('272 / 272개')
                expect(page.locator('#live-semantic-detail')).to_contain_text('판정 완료 163')
                expect(page.locator('#run-progress')).to_have_attribute('value', '544')
                expect(page.locator('#run-progress')).to_have_attribute('max', '544')
                expect(page.locator('#final-summary')).to_have_attribute('data-ready', 'false')
                expect(page.locator('#progress-description')).to_contain_text('163')
                for provider, judged in zip(providers, completed):
                    card = page.locator(f'.result-model-card[data-provider="{provider}"]')
                    expect(card.locator('.result-metric strong')).to_have_text('68')
                    expect(card.locator('.evaluation-metrics')).to_contain_text(f'{judged} / 68개 질문')
                expect(page.locator('#live-elapsed')).to_have_text('00:45')
                page.wait_for_timeout(1200)
                expect(page.locator('#live-elapsed')).to_have_text('00:45')
                output = ROOT / 'results/ui-preview'
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / 'workspace-progress-counts-terminal.png'), full_page=True)

                # Historical snapshots without progress metadata derive processing
                # from stored records, never from the number of PASS/FAIL verdicts.
                stage('COMPLETED', [68] * 4, terminal=True, omit_progress=True)
                page.reload()
                expect(page.locator('#live-semantic')).to_have_text('272 / 272개')
                expect(page.locator('#live-semantic-detail')).to_contain_text('판정 완료 163')
                expect(page.locator('#run-progress')).to_have_attribute('value', '544')
                for provider in providers:
                    expect(page.locator(f'.result-model-card[data-provider="{provider}"] .result-metric strong')).to_have_text('68')
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'workspace-progress-counts-mobile.png'), full_page=True)
                assert not errors and not writes and not external, (errors, writes, external)
                browser.close()
            print('PASS: 68x4 CODE -> semantic 254/272 -> terminal 272/272; verdict count stays separate at 163; '
                  'run progress 544/544, model processing 68/68, frozen clock, saved-record fallback, mobile; no model calls.')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    main()
