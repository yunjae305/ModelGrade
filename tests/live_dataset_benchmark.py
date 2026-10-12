"""Opt-in real browser upload -> collection -> evaluation timing on a running server.

Run with --live --input PATH. Uses the existing UI defaults and creates one new run.
Monetary costs remain in the server terminal; no retries or old-run modifications.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]

# Capture the first rendered terminal summary in the browser, independently of
# Python polling, result downloads and screenshots. Provisional scores are valid.
UI_OBSERVER = r"""(() => {
  const marker = window.__aionaBenchmark = {clicked: null, visible: null};
  let queued = false;
  const stamp = () => ({at: new Date().toISOString(), browser_ms: performance.now()});
  const ready = () => {
    const section = document.getElementById('final-summary');
    const title = document.getElementById('progress-title')?.textContent?.trim();
    return marker.clicked && section?.getClientRects().length && /^평가 (완료|미완료)$/.test(title || '');
  };
  const check = () => {
    if (marker.visible || queued || !ready()) return;
    queued = true;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      queued = false;
      if (!marker.visible && ready()) {
        const section = document.getElementById('final-summary'), rect = section.getBoundingClientRect();
        marker.visible = {...stamp(), ranking_ready: section.dataset.ready === 'true',
          progress_title: document.getElementById('progress-title').textContent.trim(),
          in_viewport: rect.bottom > 0 && rect.top < innerHeight};
      }
    }));
  };
  document.addEventListener('click', event => {
    if (event.target.closest?.('#collect-button')) { marker.clicked = stamp(); marker.visible = null; }
  }, true);
  new MutationObserver(check).observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
})();"""


def assert_no_runtime_cost(value):
    """Reject accounting metadata, without rejecting currency in original Q/A."""
    forbidden = {'costusd', 'estimatedcostusd', 'totalcostusd', 'totalusd', 'usdtotal',
                 'costledger', 'costledgers', 'llmcost', 'llmcosts', 'costreport', 'accounting'}
    markers = ('[LLM 비용]', '추정 합계: $', 'USD | 공개 단가 기준', 'RUNTIME_ONLY_MONEY_METADATA')
    if isinstance(value, dict):
        for key, item in value.items():
            if re.sub(r'[^a-z0-9]', '', str(key).lower()) in forbidden:
                raise AssertionError('Runtime cost field leaked: ' + str(key))
            assert_no_runtime_cost(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_runtime_cost(item)
    elif isinstance(value, str) and any(marker in value for marker in markers):
        raise AssertionError('Terminal-only cost report marker leaked.')


def check_cost_isolation(client, base, result, page):
    routes = [f'/api/comparison/runs/{result["run_id"]}',
              f'/api/comparison/runs/{result["run_id"]}/export']
    routes += [f'/api/comparison/runs/{result["run_id"]}/export/{provider}' for provider in result['providers']]
    assert_no_runtime_cost(result)
    for route in routes:
        response = client.get(base + route)
        response.raise_for_status()
        assert_no_runtime_cost(response.json())
    response = client.get(base + f'/api/comparison/runs/{result["run_id"]}/events')
    response.raise_for_status()
    for line in response.text.splitlines():
        if line.startswith('data:'):
            assert_no_runtime_cost(json.loads(line[5:]))
    source_routes = ['/workspace.html', '/src/workspace.js', '/src/workspace-results.js', '/src/code-eval.js']
    for route in source_routes:
        response = client.get(base + route)
        response.raise_for_status()
        assert_no_runtime_cost(response.text)
    assert_no_runtime_cost(page.locator('body').inner_text())
    saved = ROOT / 'data/comparison_runs' / (result['run_id'] + '.json')
    if saved.is_file():
        assert_no_runtime_cost(json.loads(saved.read_text(encoding='utf-8')))
    return {'passed': True, 'result_checked': True, 'export_routes_checked': routes,
            'sse_checked': True, 'frontend_sources_checked': source_routes, 'visible_ui_checked': True,
            'saved_json_checked': saved.is_file(),
            'scope': 'Runtime accounting fields and terminal report markers; original Q/A currency text is preserved.'}


def self_test():
    assert_no_runtime_cost({'answer': 'The price is USD 5, or $5.', 'usage': {'input_tokens': 100}})
    for leak in ({'usage': {'cost_usd': 1}}, {'accounting': {}}, '[LLM 비용] 실행 fixture', '추정 합계: $0.25'):
        try:
            assert_no_runtime_cost(leak)
        except AssertionError:
            continue
        raise AssertionError('The cost guard accepted runtime accounting data.')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.add_init_script(UI_OBSERVER)
        for ranking_ready in (False, True):
            page.goto('about:blank')
            page.evaluate('html => { document.body.innerHTML = html; }',
                          '<button id="collect-button">fixture</button><h2 id="progress-title">평가 중</h2>'
                          '<section id="final-summary" hidden>fixture result</section>')
            page.locator('#collect-button').click()
            assert page.evaluate('window.__aionaBenchmark.visible') is None
            page.evaluate('''ready => { setTimeout(() => {
                document.getElementById('progress-title').textContent = ready ? '평가 완료' : '평가 미완료';
                const summary = document.getElementById('final-summary');
                summary.dataset.ready = String(ready); summary.hidden = false;
            }, 75); }''', ranking_ready)
            page.wait_for_function('window.__aionaBenchmark.visible !== null')
            captured = page.evaluate('window.__aionaBenchmark')
            assert captured['visible']['ranking_ready'] is ranking_ready
            assert captured['visible']['browser_ms'] > captured['clicked']['browser_ms']
            page.wait_for_timeout(100)
            assert page.evaluate('window.__aionaBenchmark.visible') == captured['visible']
        browser.close()
    print('Benchmark self-test passed: first rendered result timestamp, provisional/final scores, cost isolation guard; no network/model calls.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--input', type=Path)
    parser.add_argument('--base', default='http://127.0.0.1:8080')
    parser.add_argument('--timeout', type=int, default=7200)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if not args.live or not args.input:
        parser.error('실제 모델을 호출하려면 --live와 --input을 지정하세요.')
    source = args.input.resolve(strict=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = ROOT / 'results' / 'dataset-benchmark' / stamp
    output.mkdir(parents=True, exist_ok=False)
    protected = [source, *list((ROOT / 'data/comparison_runs').glob('*.json'))]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    summary = {'status': 'NOT_STARTED', 'input_path': str(source),
               'input_sha256': hashes[str(source)], 'artifact_dir': str(output),
               'started_at': datetime.now(timezone.utc).isoformat(),
               'cost_location': 'server_terminal_only', 'snapshots': [], 'browser_errors': []}
    summary_path = output / 'summary.json'
    def save():
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    def get(client, route):
        response = client.get(args.base + route)
        response.raise_for_status()
        return response.json()
    started = time.monotonic()
    save()
    print(json.dumps({'artifact_dir': str(output)}, ensure_ascii=False), flush=True)
    try:
        with httpx.Client(timeout=30) as client, sync_playwright() as pw:
            get(client, '/api/health')
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1080})
            page.add_init_script(UI_OBSERVER)
            page.on('pageerror', lambda error: summary['browser_errors'].append(str(error)))
            page.goto(args.base + '/workspace.html')
            expect(page.locator('#model-selection-count')).to_have_text('4 / 4 연결')
            upload_start = time.monotonic()
            upload_browser_ms = page.evaluate('performance.now()')
            summary['upload_started_at'] = datetime.now(timezone.utc).isoformat()
            page.locator('#dataset-file').set_input_files(str(source))
            expect(page.locator('#collect-button')).to_be_enabled(timeout=30000)
            summary['upload_s'] = round(time.monotonic() - upload_start, 3)
            summary['question_count_text'] = page.locator('#question-count').inner_text()
            summary['max_tokens'] = page.locator('#max-tokens').input_value()
            page.locator('.advanced-options summary').click()
            page.locator('#run-name').fill('실측 · 단일28 + 멀티10/30 · Claude API medium · ' + stamp)
            page.screenshot(path=str(output / '01-uploaded.png'), full_page=True)
            run_start = time.monotonic()
            summary['run_click_requested_at'] = datetime.now(timezone.utc).isoformat()
            page.locator('#collect-button').click()
            expect(page.locator('#view-results')).to_be_visible()
            page.wait_for_function('sessionStorage.getItem("aiona-comparison-run")')
            run_id = page.evaluate('sessionStorage.getItem("aiona-comparison-run")')
            summary['run_clicked_at'] = page.evaluate('window.__aionaBenchmark.clicked.at')
            summary.update(run_id=run_id, status='RUNNING')
            save()
            print(json.dumps({'run_id': run_id, 'questions': summary['question_count_text'],
                              'max_tokens': summary['max_tokens']}, ensure_ascii=False), flush=True)
            seen, last_log = set(), 0
            while time.monotonic() - run_start < args.timeout:
                status = get(client, f'/api/comparison/runs/{run_id}')
                elapsed = time.monotonic() - run_start
                observed_at = datetime.now(timezone.utc).isoformat()
                phase = status['status']
                if phase not in seen:
                    seen.add(phase)
                    summary.setdefault('phase_first_seen_s', {})[phase] = round(elapsed, 3)
                    page.screenshot(path=str(output / f'phase-{phase.lower()}.png'), full_page=True)
                item = {'elapsed_s': round(elapsed, 3), 'status': phase, 'collected': status.get('done'),
                        'collection': status.get('progress'), 'evaluation': status.get('evaluation_progress')}
                summary['snapshots'].append(item)
                if time.monotonic() - last_log >= 30:
                    print(json.dumps({'elapsed_s': round(elapsed), 'status': phase,
                                      'collected': status.get('done'),
                                      'semantic_done': sum(p.get('semantic_done', 0) for p in (status.get('evaluation_progress') or {}).values())}), flush=True)
                    last_log = time.monotonic()
                    save()
                if phase in ('COMPLETED', 'ERROR', 'CANCELLED'):
                    summary['backend_finished_s'] = round((datetime.fromisoformat(observed_at) - datetime.fromisoformat(summary['run_clicked_at'].replace('Z', '+00:00'))).total_seconds(), 3)
                    summary['backend_terminal_observed_at'] = observed_at
                    summary['backend_timing_method'] = 'First terminal API response observed by polling every two seconds; not the exact server completion instant.'
                    break
                page.wait_for_timeout(2000)
            else:
                raise TimeoutError('The run did not finish within the benchmark timeout; no automatic retry was started.')
            result = get(client, f'/api/comparison/runs/{run_id}/result')
            (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            summary.update(status=result['status'], models=result.get('models'),
                           collection_progress=result.get('progress'), evaluation_progress=result.get('evaluation_progress'),
                           collection_completed_at=result.get('collection_completed_at'), eval_started_at=result.get('eval_started_at'),
                           collection_execution=result.get('collection_execution'), evaluation_execution=result.get('evaluation_execution'))
            if result['status'] == 'COMPLETED':
                # A finished run may legitimately contain UNVERIFIED verdicts.
                # The result view is complete even when ranking stays provisional.
                expect(page.locator('#progress-title')).to_have_text(re.compile(r'^평가 (?:완료|미완료)$'), timeout=30000)
                expect(page.locator('#final-summary')).to_be_visible(timeout=30000)
                page.wait_for_function('window.__aionaBenchmark.visible !== null', timeout=30000)
                timing = page.evaluate('window.__aionaBenchmark')
                summary['ui_result_visible_at'] = timing['visible']['at']
                summary['ui_result_ready_s'] = round((timing['visible']['browser_ms'] - timing['clicked']['browser_ms']) / 1000, 3)
                summary['ranking_ready'] = timing['visible']['ranking_ready']
                summary['ui_progress_title'] = timing['visible']['progress_title']
                summary['ui_summary_in_viewport'] = timing['visible']['in_viewport']
                summary['ui_timing_method'] = 'Browser MutationObserver followed by two animation frames; first rendered terminal summary, independent of API polling and screenshots.'
                summary['upload_to_result_s'] = round((timing['visible']['browser_ms'] - upload_browser_ms) / 1000, 3)
                summary['summary_title'] = page.locator('#final-summary-title').inner_text()
                summary['summary_description'] = page.locator('#final-summary-description').inner_text()
                save()
                page.screenshot(path=str(output / '04-final-results.png'), full_page=True)
                summary['cost_isolation'] = check_cost_isolation(client, args.base, result, page)
                assert not summary['browser_errors'], summary['browser_errors']
            browser.close()
    except BaseException as exc:
        summary['driver_error'] = {'type': type(exc).__name__, 'message': str(exc)}
        raise
    finally:
        summary['driver_total_s'] = round(time.monotonic() - started, 3)
        summary['originals_unchanged'] = all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in hashes.items())
        save()
        print(json.dumps({'status': summary['status'], 'summary_path': str(summary_path),
                          'ui_result_ready_s': summary.get('ui_result_ready_s'),
                          'originals_unchanged': summary['originals_unchanged']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
