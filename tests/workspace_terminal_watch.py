"""Read-only terminal UI watcher. GET only; never starts or retries model work.

Polls an existing run, then opens a fresh browser page and records the first
observed terminal result view. A completed run may have unconfirmed scores.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def now():
    return datetime.now(timezone.utc).isoformat()


def elapsed_since(earlier, later):
    return round((datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds(), 3) if earlier else None


def watch(base, run_id, output, *, interval=30, timeout=7200, benchmark_summary=None, sample=True, observation_kind='live_watch'):
    output.mkdir(parents=True, exist_ok=True)
    report = {'run_id': run_id, 'watch_started_at': now(), 'base': base, 'poll_interval_s': interval, 'observation_kind': observation_kind,
              'status': 'WATCHING', 'observations': [], 'browser_errors': [], 'blocked_requests': []}
    summary_path = output / 'ui-watch-summary.json'
    protected = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in (ROOT / 'data/comparison_runs').glob('*.json') if path.stem != run_id}

    def save():
        summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def open_page(browser):
        page = browser.new_page(viewport={'width': 1440, 'height': 1080})
        page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))

        def read_only(route):
            request = route.request
            if request.method == 'GET' and request.url.startswith(base + '/'):
                route.continue_()
            else:
                report['blocked_requests'].append({'method': request.method, 'url': request.url})
                route.abort()

        page.route('**/*', read_only)
        page.add_init_script('sessionStorage.setItem("aiona-comparison-run", ' + json.dumps(run_id) + ')')
        page.goto(base + '/workspace.html#results')
        expect(page.locator('#result-content')).to_be_visible(timeout=30000)
        return page

    started = time.monotonic()
    save()
    try:
        with httpx.Client(timeout=30) as client, sync_playwright() as pw:
            if sample:
                browser = pw.chromium.launch(headless=True)
                page = open_page(browser)
                report['sample'] = {'observed_at': now(), 'progress_title': page.locator('#progress-title').inner_text(),
                                    'description': page.locator('#progress-description').inner_text()}
                page.screenshot(path=str(output / '01-current-results.png'), full_page=True)
                browser.close()
            while time.monotonic() - started < timeout:
                response = client.get(base + f'/api/comparison/runs/{run_id}')
                response.raise_for_status()
                status = response.json()
                observation = {'observed_at': now(), 'status': status['status'],
                    'semantic_done': sum(p.get('semantic_done', 0) for p in (status.get('evaluation_progress') or {}).values()),
                    'semantic_unverified': sum(p.get('semantic_unverified', 0) for p in (status.get('evaluation_progress') or {}).values()),
                    'semantic_errors': sum(p.get('semantic_errors', 0) for p in (status.get('evaluation_progress') or {}).values())}
                report['observations'].append(observation)
                save()
                print(json.dumps(observation, ensure_ascii=False), flush=True)
                if status['status'] in ('COMPLETED', 'ERROR', 'CANCELLED'):
                    report['api_terminal_observed_at'] = observation['observed_at']
                    break
                time.sleep(interval)
            else:
                raise TimeoutError('The existing run did not reach a terminal state within the watcher timeout.')
            response = client.get(base + f'/api/comparison/runs/{run_id}/result')
            response.raise_for_status()
            result = response.json()
            report['run_created_at'] = result.get('created_at')
            report['terminal_status'] = result['status']
            if benchmark_summary:
                benchmark = json.loads(benchmark_summary.read_text(encoding='utf-8'))
                report['run_clicked_at'] = benchmark.get('run_clicked_at')
                report['benchmark_summary_path'] = str(benchmark_summary)
            browser = pw.chromium.launch(headless=True)
            page = open_page(browser)
            if result['status'] == 'COMPLETED':
                expect(page.locator('#final-summary')).to_be_visible(timeout=30000)
                report['ui_result_visible_at'] = now()
                summary = page.evaluate('''r => {
                    const final = AionaResults.finalSummary(r), run = AionaResults.runSummary(r);
                    return {ready:final.ready, incomplete:final.incomplete, all_complete:run.allComplete,
                        models:r.providers.map(provider => {
                            const m=AionaResults.modelSummary(r,provider,r.question_count);
                            return {provider,counts:m.counts,complete:m.complete,issue_pending:m.issuePending,
                                overlapping_cases:m.rows.map((s,i)=>s.outcome==='issue'&&s.pending?r.datasets[provider][i].case_id:null).filter(Boolean),
                                pending_filter_count:Array.from({length:r.question_count},(_,i)=>i).filter(i=>AionaResults.questionMatches(r,[provider],i,'pending')).length};
                        })};
                }''', result)
                expect(page.locator('#progress-title')).to_have_text('평가 완료' if summary['all_complete'] else '평가 미완료')
                expect(page.locator('#final-summary')).to_have_attribute('data-ready', str(summary['ready']).lower())
                assert all(m['counts']['pending'] == m['pending_filter_count'] for m in summary['models'])
                report['computed_summary'] = summary
                report['ranking_ready'] = summary['ready']
                report['summary_title'] = page.locator('#final-summary-title').inner_text()
                report['summary_description'] = page.locator('#final-summary-description').inner_text()
                if not summary['ready']:
                    expect(page.locator('#final-summary-description')).to_contain_text('작업은 종료')
                for model in summary['models']:
                    card = page.locator('.result-model-card[data-provider="' + model['provider'] + '"]')
                    for outcome in ('issue', 'pending'):
                        card.locator('.outcome-count.' + outcome).click()
                        expect(page.locator('#matrix-caption')).to_have_text(re.compile('^' + str(model['counts'][outcome]) + '개 중'))
                    for case_id in model['overlapping_cases']:
                        page.locator('#matrix-search').fill(case_id)
                        for outcome in ('issue', 'pending'):
                            page.locator('#matrix-filter').select_option(outcome)
                            expect(page.locator('#matrix-caption')).to_have_text(re.compile(r'^1개 중'))
                        page.locator('#matrix-search').fill('')
                    details = card.locator('.evaluation-warning details')
                    if details.count():
                        expect(details.first.locator('p').first).not_to_be_visible()
                        details.first.locator('summary').click()
                        expect(details.first.locator('p').first).to_be_visible()
                        details.first.locator('summary').click()
                report['ui_filters_checked'] = True
                page.locator('#clear-matrix-filter').click()
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=str(output / '02-terminal-results.png'), full_page=True)
                pending = next(((p, i) for p in result['providers'] for i, rec in enumerate(result.get('evaluations', {}).get(p, {}).get('jev_eval', {}).get('records', []))
                                if rec and any(d.get('status') == 'UNVERIFIED' for d in (rec.get('decisions') or {}).values())), None)
                if pending:
                    provider, index = pending
                    case_id = result['datasets'][provider][index]['case_id']
                    page.locator('#matrix-search').fill(case_id)
                    model_name = {'aiona': 'AIONA', 'gpt': 'GPT', 'claude': 'Claude', 'gemini': 'Gemini'}[provider]
                    page.locator('#matrix-body td[data-model="' + model_name + '"] .result-cell').first.click()
                    expect(page.locator('#answer-evaluation')).to_contain_text('확인 필요')
                    report['pending_example'] = {'provider': provider, 'case_id': case_id,
                                                 'detail': page.locator('#answer-evaluation').inner_text()}
                    page.locator('#answer-dialog').screenshot(path=str(output / '03-pending-detail.png'))
                    page.keyboard.press('Escape')
                page.reload()
                expect(page.locator('#final-summary')).to_be_visible(timeout=30000)
                expect(page.locator('#final-summary')).to_have_attribute('data-ready', str(summary['ready']).lower())
                assert page.evaluate('sessionStorage.getItem("aiona-comparison-run")') == run_id
                report['reload_preserved'] = True
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / '04-terminal-mobile.png'), full_page=True)
            report['progress_title'] = page.locator('#progress-title').inner_text()
            report['progress_description'] = page.locator('#progress-description').inner_text()
            report['status'] = 'PASS' if result['status'] == 'COMPLETED' else 'TERMINAL_' + result['status']
            browser.close()
            assert not report['browser_errors'] and not report['blocked_requests'], report
    except BaseException as exc:
        report['status'] = 'CHECK_FAILED'
        report['watcher_error'] = {'type': type(exc).__name__, 'message': str(exc)}
        raise
    finally:
        report['watch_finished_at'] = now()
        report['watch_duration_s'] = round(time.monotonic() - started, 3)
        if report.get('ui_result_visible_at'):
            if observation_kind == 'live_watch':
                report['click_to_ui_visible_s'] = elapsed_since(report.get('run_clicked_at'), report['ui_result_visible_at'])
                report['created_to_ui_visible_s'] = elapsed_since(report.get('run_created_at'), report['ui_result_visible_at'])
            report['terminal_observation_to_ui_visible_s'] = elapsed_since(report['api_terminal_observed_at'], report['ui_result_visible_at'])
        report['prior_saved_runs_unchanged'] = all(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in protected.items())
        save()
        print(json.dumps({'status': report['status'], 'summary_path': str(summary_path),
                          'ui_result_visible_at': report.get('ui_result_visible_at')}, ensure_ascii=False), flush=True)
    return report


def replay(saved_result, output):
    from webapp.code_eval_service import CodeEvalService
    from webapp.comparison_service import ComparisonService
    from webapp.server import Server

    original = saved_result.read_bytes()
    run = json.loads(original)
    if run.get('status') not in ('COMPLETED', 'ERROR', 'CANCELLED'):
        raise ValueError('Replay requires a saved terminal run.')

    def forbidden(*args, **kwargs):
        raise AssertionError('Model calls are forbidden during saved-result replay.')

    with tempfile.TemporaryDirectory(prefix='aiona-terminal-replay-') as directory:
        path = Path(directory)
        evaluator = CodeEvalService(save_dir=path / 'eval', jev_resolver=forbidden)
        comparison = ComparisonService(evaluator, save_dir=path / 'runs', client_factory=forbidden)
        comparison._runs[run['run_id']] = run
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=comparison)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            report = watch(f'http://127.0.0.1:{server.server_port}', run['run_id'], output,
                           timeout=30, sample=False, observation_kind='saved_result_replay')
            assert hashlib.sha256(saved_result.read_bytes()).digest() == hashlib.sha256(original).digest()
            report.update(saved_result_path=str(saved_result), saved_result_sha256=hashlib.sha256(original).hexdigest(), saved_result_unchanged=True)
            (output / 'ui-watch-summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


def self_test():
    from code_eval.engine import evaluate_batch
    from webapp.code_eval_service import CodeEvalService
    from webapp.comparison_service import ComparisonService
    from webapp.server import Server

    def forbidden(*args, **kwargs):
        raise AssertionError('Model calls are forbidden in a read-only watcher test.')

    with tempfile.TemporaryDirectory(prefix='aiona-terminal-watch-') as directory:
        path = Path(directory)
        evaluator = CodeEvalService(save_dir=path / 'eval', jev_resolver=forbidden)
        comparison = ComparisonService(evaluator, save_dir=path / 'runs', client_factory=forbidden)
        run_id = comparison.start({'cases': [{'case_id': 'watch-fixture', 'question': 'Hello?', 'answer': 'Hello.'}], 'providers': []})['run_id']
        run = comparison._runs[run_id]
        batch = evaluate_batch(run['datasets']['aiona'])
        batch['jev_eval'] = {'records': [{'status': 'SUCCESS', 'criteria': ['J1', 'J5'], 'decisions': {
            'J1': {'decision': 'FAIL', 'reason': 'Known fixture failure'},
            'J5': {'decision': None, 'status': 'UNVERIFIED', 'reason': 'Offline fixture: insufficient evidence'}}}]}
        run.update(status='COMPLETED', phase='EVALUATE', eval_done=1, eval_total=1)
        run['evaluations']['aiona'] = batch
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=comparison)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            report = watch(f'http://127.0.0.1:{server.server_port}', run_id, path / 'watch', interval=0.01, timeout=30, sample=False)
            assert report['status'] == 'PASS' and report['ranking_ready'] is False
            assert report['progress_title'] == '평가 미완료' and report['reload_preserved']
            assert report['computed_summary']['models'][0]['counts']['pending'] == 1
            print('Watcher self-test passed: terminal success with FAIL + UNVERIFIED, fresh/reloaded UI, mobile, GET only.')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--base', default='http://127.0.0.1:8080')
    parser.add_argument('--run-id')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--saved-result', type=Path, help='Replay a terminal JSON in an isolated local server; no existing server or model calls.')
    parser.add_argument('--benchmark-summary', type=Path)
    parser.add_argument('--timeout', type=int, default=7200)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.saved_result:
        if not args.output:
            parser.error('--output is required for saved-result replay.')
        replay(args.saved_result.resolve(strict=True), args.output)
        return
    if not args.run_id or not args.output:
        parser.error('--run-id and --output are required for an existing-run watch.')
    watch(args.base.rstrip('/'), args.run_id, args.output, timeout=args.timeout, benchmark_summary=args.benchmark_summary)


if __name__ == '__main__':
    main()
