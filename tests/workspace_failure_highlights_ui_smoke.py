"""Offline FAIL-location UI replay; no provider calls or operational writes."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
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
    saved = ROOT / 'data/comparison_runs/ad9bc5c843b1.json'
    original = saved.read_bytes()
    run = json.loads(original)
    output = ROOT / 'results/failure-highlights-review'
    output.mkdir(parents=True, exist_ok=True)
    errors, writes, external = [], [], []

    def forbidden(*args, **kwargs):
        raise AssertionError('Provider calls forbidden in saved-result highlight replay')

    with tempfile.TemporaryDirectory(prefix='failure-highlights-ui-') as temporary:
        path = Path(temporary)
        evaluator = CodeEvalService(save_dir=path / 'eval', jev_resolver=forbidden)
        service = ComparisonService(evaluator, save_dir=path / 'runs', client_factory=forbidden)
        service._runs[run['run_id']] = copy.deepcopy(run)
        live = copy.deepcopy(run)
        live.update(run_id='ffffffffffa1', status='EVALUATING', revision=1, blind={'enabled': False})
        for batch in live['evaluations'].values():
            batch['live'] = True
        service._runs[live['run_id']] = live
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')

                def read_only(route):
                    if route.request.method != 'GET':
                        writes.append(route.request.url)
                        route.abort()
                    elif not route.request.url.startswith(base + '/'):
                        external.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                context.route('**/*', read_only)
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.add_init_script('''
                    sessionStorage.setItem("aiona-comparison-run", "ad9bc5c843b1");
                    Object.defineProperty(navigator, 'clipboard', {configurable: true,
                      value: {writeText: async (text) => {window.copiedOriginal = text;}}});
                ''')
                page.goto(base + '/workspace.html#results')
                page.locator('[data-focus-key="result-claude-0"]').click()
                dialog = page.locator('#answer-dialog')
                expect(dialog).to_be_visible()
                view = page.locator('#original-answer [data-failure-view]')
                marks = view.locator('mark[data-failure-index]')
                pre = page.locator('#original-answer pre')

                def check_original(provider, index):
                    answer = run['datasets'][provider][index]['answer']
                    assert pre.text_content() == answer, (provider, index, 'Displayed original changed')
                    page.locator('#copy-answer').click()
                    assert page.evaluate('window.copiedOriginal') == answer

                expected_claims = [claim['claim'] for claim in run['evaluations']['claude']['jev_eval']['records'][0]
                                   ['decisions']['J5']['fact_check']['claims'] if claim['verdict'] == 'FAIL']
                assert len(expected_claims) == 2
                expect(marks).to_have_count(2)
                assert marks.all_text_contents() == expected_claims
                expect(view.locator('mark.verification')).to_have_count(0)
                expect(view.locator('[data-failure-position]')).to_have_text('1 / 2곳')
                check_original('claude', 0)

                # Summary links and keyboard navigation land on the exact marked
                # sentence; source/citation text must never become an answer mark.
                page.locator('#answer-evaluation [data-failure-jump]').first.click()
                expect(marks.nth(0)).to_be_focused()
                expect(marks.nth(0)).to_have_class(re.compile(r'is-current'))
                next_button = view.locator('[data-failure-next]')
                next_button.focus()
                next_button.press('Enter')
                expect(view.locator('[data-failure-position]')).to_have_text('2 / 2곳')
                expect(marks.nth(1)).to_be_focused()
                expect(marks.nth(1)).to_have_class(re.compile(r'is-current'))
                expect(view.locator('[data-failure-selection]')).to_contain_text('온도 차이')
                assert marks.nth(1).evaluate('''node => {
                    const mark = node.getBoundingClientRect(), modal = node.closest('dialog').getBoundingClientRect();
                    return mark.top >= modal.top && mark.bottom <= modal.bottom;
                }'''), 'Selected failure did not scroll into the visible dialog'
                view.locator('[data-failure-prev]').focus()
                view.locator('[data-failure-prev]').press('Enter')
                expect(view.locator('[data-failure-position]')).to_have_text('1 / 2곳')
                view.screenshot(path=str(output / 'claude-fail-locations-desktop.png'))

                toggle = view.locator('[data-failure-toggle]')
                toggle.click()
                expect(toggle).to_have_attribute('aria-pressed', 'false')
                expect(view).to_have_class(re.compile(r'highlights-hidden'))
                expect(marks).to_have_count(2)
                expect(view.locator('[data-failure-selection]')).to_be_hidden()
                assert marks.first.evaluate("node => getComputedStyle(node).backgroundColor") == 'rgba(0, 0, 0, 0)'
                check_original('claude', 0)
                toggle.click()
                expect(toggle).to_have_attribute('aria-pressed', 'true')

                # A source retrieval failure uses a distinct verification style
                # and only the claim indices recorded by the source policy.
                view.locator('[data-failure-next]').click()
                view.locator('[data-failure-toggle]').click()
                expect(view.locator('[data-failure-toggle]')).to_have_attribute('aria-pressed', 'false')
                page.locator('#answer-tab-aiona').click()
                policy = run['evaluations']['aiona']['jev_eval']['records'][0]['decisions']['J5']
                indexes = sorted({item['claim_index'] for item in policy['decision_policy']['failures']})
                policy_claims = [policy['fact_check']['claims'][index]['claim'] for index in indexes]
                expect(marks).to_have_count(2)
                assert marks.all_text_contents() == policy_claims
                expect(view.locator('mark.verification')).to_have_count(2)
                expect(view.locator('[data-failure-position]')).to_have_text('1 / 2곳')
                expect(view.locator('[data-failure-toggle]')).to_have_attribute('aria-pressed', 'true')
                check_original('aiona', 0)
                view.screenshot(path=str(output / 'aiona-source-verification-locations.png'))

                # A PASS model and a location-free count failure must not inherit
                # highlights from the previously selected model/question.
                page.locator('#answer-tab-gpt').click()
                expect(page.locator('#original-answer mark')).to_have_count(0)
                check_original('gpt', 0)
                page.locator('#answer-tab-aiona').click()
                page.locator('#answer-select').select_option('2')
                expect(page.locator('#answer-evaluation .answer-verdict')).to_contain_text('FAIL')
                expect(page.locator('#original-answer mark')).to_have_count(0)
                expect(view.locator('[data-failure-unlocated]')).to_be_visible()
                expect(view.locator('[data-failure-unlocated]')).to_contain_text('위치')
                check_original('aiona', 2)

                # The syntax diagnostic stores an exact original-answer line.
                # Highlight that line rather than the whole Python code block.
                page.locator('#answer-select').select_option('3')
                expect(marks).to_have_count(1)
                expected_line = run['datasets']['aiona'][3]['answer'].splitlines()[4]
                assert marks.first.text_content() == expected_line
                expect(view.locator('[data-failure-position]')).to_have_text('1 / 1곳')
                check_original('aiona', 3)
                view.screenshot(path=str(output / 'python-exact-line-location.png'))

                page.locator('#answer-select').select_option('0')
                page.locator('#answer-tab-claude').click()
                for width in (390, 320):
                    page.set_viewport_size({'width': width, 'height': 844})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert dialog.evaluate('node => node.scrollWidth <= node.clientWidth + 1')
                    page.locator('#answer-evaluation [data-failure-jump]').first.click()
                    expect(marks.first).to_be_focused()
                    view.locator('[data-failure-next]').focus()
                    view.locator('[data-failure-next]').press('Enter')
                    expect(view.locator('[data-failure-position]')).to_have_text('2 / 2곳')
                    check_original('claude', 0)
                    view.screenshot(path=str(output / f'claude-fail-locations-mobile-{width}.png'))

                # A completed batch replaces the rendered answer while the same
                # question stays open. Its summary link must target the new DOM,
                # not the old highlight component captured by a stale closure.
                page.set_viewport_size({'width': 1440, 'height': 1080})
                page.locator('[data-close="answer-dialog"]').first.click()
                with page.expect_response(lambda response: response.url.endswith(f'/{live["run_id"]}/result')):
                    page.evaluate('(id) => { void AionaWorkspace.openRun(id); }', live['run_id'])
                expect(page.locator('#final-summary')).to_be_hidden()
                page.locator('[data-focus-key="result-claude-0"]').click()
                expect(marks).to_have_count(2)
                old_mark = marks.first.element_handle()
                assert old_mark is not None
                with service._lock:
                    live.update(status='COMPLETED', revision=2)
                    for batch in live['evaluations'].values():
                        batch['live'] = False
                expect(page.locator('#run-status')).to_have_text('평가 완료')
                expect(dialog).to_be_visible()
                page.wait_for_function('node => !node.isConnected', arg=old_mark)
                page.locator('#answer-evaluation [data-failure-jump]').first.click()
                expect(marks.first).to_be_focused()
                assert marks.first.evaluate('node => node.isConnected')
                expect(view.locator('[data-failure-position]')).to_have_text('1 / 2곳')
                check_original('claude', 0)
                assert not errors and not writes and not external, (errors, writes, external)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    assert hashlib.sha256(saved.read_bytes()).digest() == hashlib.sha256(original).digest()
    summary = {'status': 'PASS', 'confirmed_failure_marks': 2, 'source_policy_marks': 2, 'syntax_line_marks': 1,
               'location_free_failure_marks': 0, 'original_text_and_copy_preserved': True,
               'navigation_toggle_reset_passed': True, 'desktop_and_mobile_widths': [1440, 390, 320],
               'live_completion_targets_current_highlight': True,
               'saved_unchanged': True, 'external_calls': 0, 'writes': 0}
    (output / 'ui-check.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
