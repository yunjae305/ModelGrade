"""Version improvement presentation with isolated, explicitly synthetic saved results.

No provider calls, no judge calls and no writes to the user's saved evaluations.
"""
from __future__ import annotations

import copy
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
from webapp.server import Handler, Server


def fixture(run_id, version, verdicts, *, changed_answers=False):
    questions = [
        '환불 처리 절차를 세 단계로 설명해 주세요.',
        '주어진 자료를 바탕으로 결론과 이유를 설명해 주세요.',
        '이번 회의의 핵심 내용을 한 문장으로 정리해 주세요.',
        '안내문에 필요한 날짜와 주의사항을 함께 써 주세요.',
    ]
    before_answers = [
        '환불을 요청하세요.',
        '매출이 늘었으므로 비용도 줄었습니다.',
        '회의에서는 다음 주 출시 일정을 확정했습니다.',
        '안내문입니다. 준비해 주세요.',
    ]
    after_answers = [
        '1. 주문 내역을 확인합니다.\n2. 환불 요청을 접수합니다.\n3. 처리 결과를 확인합니다.',
        '매출은 증가했습니다. 비용 변화는 자료에 없으므로 비용이 줄었다고 결론낼 수 없습니다.',
        before_answers[2], before_answers[3],
    ]
    answers = after_answers if changed_answers else before_answers
    sources = [dict(case_id=f'UI-Q{i + 1:02}', question=question, answer=answers[i])
               for i, question in enumerate(questions)]
    cases = [{**copy.deepcopy(source), 'case_status': 'EVALUATED', 'results': [{
        'area': 'common', 'category': '1', 'condition_id': 'nonempty',
        'check_id': '1-1', 'title': '빈 답변 검사', 'counted': True, 'verdict': 'PASS',
    }], 'summary': {'common': {'pass': 1, 'fail': 0, 'unjudged': 0},
                   'by_category': {'1': {'pass': 1, 'fail': 0, 'unjudged': 0}}}}
             for source in sources]
    reason_pairs = {
        'J1': {'PASS': '요청한 세 단계와 필요한 작업을 모두 제시했습니다.',
               'FAIL': '요청한 단계별 처리 절차가 빠졌습니다.'},
        'J3': {'PASS': '자료에서 확인할 수 있는 결론과 확인할 수 없는 결론을 구분했습니다.',
               'FAIL': '매출 증가를 근거로 비용 감소를 단정했습니다.'},
    }
    records = [{
        'case_id': source['case_id'], 'context': None, 'status': 'SUCCESS',
        'criteria': ['J1', 'J3'],
        'decisions': {cid: {'decision': value, 'reason': reason_pairs[cid][value]}
                      for cid, value in zip(('J1', 'J3'), verdicts[i])},
    } for i, source in enumerate(sources)]
    return {
        'run_id': run_id, 'name': f'UI 검증용 {version} · 실제 평가 결과 아님',
        'created_at': '2026-10-12T05:00:00Z', 'status': 'COMPLETED',
        'question_count': len(sources), 'providers': ['aiona'],
        'aiona_release': {'version': version, 'notes': 'Isolated synthetic fixture'},
        'datasets': {'aiona': sources}, 'evaluations': {'aiona': {
            'cases': cases,
            'versions': {'engine': 'ui-fixture-code-v1', 'checks': {'1-1': 'ui-fixture-v1'}},
            'aiona_config': {'config_hash': 'ui-fixture-paused', 'enabled': False},
            'jev_eval': {
                'records': records, 'version': 'ui-fixture-semantic-v1',
                'criteria_version': 'ui-fixture-criteria-v1',
                'applicability_version': 'ui-fixture-applicability-v1',
                'history_policy': 'full_original_text', 'models': ['ui-fixture-judge'],
                'client': {'engine': 'ui_fixture', 'requested_model': 'ui-fixture-judge',
                           'reasoning_effort': 'high'},
                'criteria': [{'id': 'J1', 'name': '의도 충족성'}, {'id': 'J3', 'name': '논리 타당성'}],
            },
        }},
    }


def main():
    Handler.log_message = lambda *args: None
    output = ROOT / 'results/version-improvement-review'
    output.mkdir(parents=True, exist_ok=True)
    verdicts = [('FAIL', 'PASS'), ('PASS', 'FAIL'), ('PASS', 'PASS'), ('FAIL', 'FAIL')]
    before = fixture('aaaaaaaaaa01', 'v1', verdicts)
    improved = fixture('aaaaaaaaaa02', 'v2', [('PASS', 'PASS')] * 3 + [('FAIL', 'FAIL')],
                       changed_answers=True)
    regressed = fixture('aaaaaaaaaa03', 'v2-regressed', [('FAIL', 'FAIL')] * 4)
    unchanged = fixture('aaaaaaaaaa04', 'v2-unchanged', verdicts)
    mixed = fixture('aaaaaaaaaa05', 'v2-mixed',
                    [('PASS', 'PASS'), ('PASS', 'FAIL'), ('PASS', 'FAIL'), ('FAIL', 'FAIL')],
                    changed_answers=True)
    incomplete = copy.deepcopy(improved)
    incomplete.update(run_id='aaaaaaaaaa06', aiona_release={'version': 'v2-incomplete'})
    incomplete['evaluations']['aiona']['jev_eval']['records'][0].update(
        status='API_ERROR', decisions=None, error='UI fixture: no evaluation response')
    mismatch = copy.deepcopy(improved)
    mismatch.update(run_id='aaaaaaaaaa07', aiona_release={'version': 'v2-mismatched'})
    mismatch['evaluations']['aiona']['jev_eval']['criteria_version'] = 'ui-fixture-different-criteria'
    literal = copy.deepcopy(improved)
    literal.update(run_id='aaaaaaaaaa08', aiona_release={'version': 'v2-literal'})
    literal_answer = '<script>window.versionUnsafe = true</script>\n' + literal['datasets']['aiona'][0]['answer']
    literal['datasets']['aiona'][0]['answer'] = literal_answer
    literal['evaluations']['aiona']['cases'][0]['answer'] = literal_answer
    same_answers = fixture('aaaaaaaaaa09', 'v2-same-answer', [('PASS', 'PASS')] * 3 + [('FAIL', 'FAIL')])
    fixtures = [before, improved, regressed, unchanged, mixed, incomplete, mismatch, literal, same_answers]
    originals = copy.deepcopy(fixtures)
    errors, posts, external = [], [], []

    def forbidden(*args, **kwargs):
        external.append('forbidden external evaluation')
        raise AssertionError('External model and judge calls are forbidden in this smoke test')

    with tempfile.TemporaryDirectory(prefix='aiona-version-improvement-ui-') as directory:
        temp = Path(directory)
        evaluator = CodeEvalService(save_dir=temp / 'eval', jev_resolver=forbidden)
        service = ComparisonService(evaluator, save_dir=temp / 'runs',
                                    registry_path=temp / 'versions.json', client_factory=forbidden)
        service._runs.update({run['run_id']: run for run in fixtures})
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: posts.append(request.url) if request.method != 'GET' else None)
                def setup(route):
                    response = route.fetch()
                    info = response.json()
                    info['semantic_setup'] = {'can_evaluate': False, 'reason': 'UI fixture: read-only review'}
                    route.fulfill(response=response, json=info)

                page.route('**/api/code-eval/info', setup)
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base + '/workspace.html#comparison')
                expect(page.locator('.version-selectors select:visible')).to_have_count(2)
                expect(page.locator('#version-delta .vc-delta-value')).to_have_text('—')
                expect(page.locator('#version-delta')).to_be_hidden()
                page.locator('#version-baseline-model').select_option(before['run_id'])
                expect(page.locator('#version-summary')).to_have_attribute('aria-busy', 'false')
                expect(page.locator('#version-highlights')).to_be_hidden()

                def select(run, trend, ready=True):
                    page.locator('#version-target-model').select_option(run['run_id'])
                    expect(page.locator('#version-summary')).to_have_attribute('aria-busy', 'false')
                    expect(page.locator('#version-summary')).to_have_attribute('data-ready', str(ready).lower())
                    expect(page.locator('#version-summary')).to_have_attribute('data-trend', trend)

                def impact(key, count):
                    expect(page.locator(f'#version-impact-counts [data-filter="{key}"] strong')).to_have_text(str(count))

                select(improved, 'improved')
                expect(page.locator('#version-delta .vc-delta-value')).to_have_text('+50.0')
                expect(page.locator('[data-version-side="baseline"] .summary-score strong')).to_have_text('25.0')
                expect(page.locator('[data-version-side="target"] .summary-score strong')).to_have_text('75.0')
                impact('improved', 2)
                impact('regressed', 0)
                for criterion in ('J1', 'J3'):
                    card = page.locator(f'#version-criteria-highlights [data-criterion="{criterion}"]')
                    expect(card).to_have_attribute('data-change', 'improved')
                    expect(card).to_contain_text('+25.0')
                expect(page.locator('#version-improved-answers [data-case-id]')).to_have_count(2)
                card = page.locator('#version-improved-answers [data-case-id="UI-Q01"]')
                expect(card).to_contain_text('요청한 단계별 처리 절차가 빠졌습니다.')
                expect(card).to_contain_text('요청한 세 단계와 필요한 작업을 모두 제시했습니다.')
                assert '자료에서 확인할 수 있는 결론' not in card.inner_text(), 'Unrelated PASS evidence must not explain J1 improvement'
                card.locator('.vc-answer-link').click()
                expect(page.locator('#version-answer-dialog')).to_be_visible()
                expect(page.locator('#version-answer-columns')).to_contain_text(before['datasets']['aiona'][0]['answer'])
                expect(page.locator('#version-answer-columns')).to_contain_text(improved['datasets']['aiona'][0]['answer'])
                page.screenshot(path=str(output / 'improved-answer-desktop.png'), full_page=True)
                page.keyboard.press('Escape')
                page.locator('#version-show-improved').click()
                expect(page.locator('#version-change-filter')).to_have_value('improved')
                expect(page.locator('#version-rows tr')).to_have_count(2)
                page.locator('#version-show-criteria').click()
                assert page.url.endswith('#comparison'), 'Criteria jump must preserve the comparison route'
                expect(page.locator('#version-criteria-panel')).to_be_visible()
                page.locator('#version-comparison-details > summary').click()
                expect(page.locator('#version-baseline-model')).to_be_hidden()
                expect(page.locator('#version-summary')).to_be_visible()
                expect(page.locator('#version-highlights')).to_be_visible()
                page.locator('#version-comparison-details > summary').click()
                page.locator('#version-change-filter').select_option('')
                page.evaluate('scrollTo(0, 0)')
                page.screenshot(path=str(output / 'improved-desktop.png'), full_page=True)
                page.locator('#version-summary').evaluate("element => element.scrollIntoView({behavior: 'instant', block: 'start'})")
                page.screenshot(path=str(output / 'improved-desktop-focus.png'), full_page=False)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'improved-mobile.png'), full_page=True)
                page.locator('#version-summary').evaluate("element => element.scrollIntoView({behavior: 'instant', block: 'start'})")
                page.screenshot(path=str(output / 'improved-mobile-focus.png'), full_page=False)
                page.locator('#version-answer-highlights').evaluate("element => element.scrollIntoView({behavior: 'instant', block: 'start'})")
                page.screenshot(path=str(output / 'improved-mobile-answer-focus.png'), full_page=False)
                page.set_viewport_size({'width': 1440, 'height': 1080})

                select(regressed, 'regressed')
                expect(page.locator('#version-delta .vc-delta-value')).to_have_text('-25.0')
                impact('improved', 0)
                impact('regressed', 1)
                expect(page.locator('#version-criteria-highlights [data-change="regressed"]')).to_have_count(2)
                expect(page.locator('#version-improved-answers [data-case-id]')).to_have_count(0)
                page.locator('#version-impact-counts [data-filter="regressed"]').click()
                expect(page.locator('#version-rows')).to_contain_text('UI-Q03')
                expect(page.locator('#version-rows tr')).to_have_count(1)
                page.evaluate('scrollTo(0, 0)')
                page.screenshot(path=str(output / 'regressed-desktop.png'), full_page=True)

                select(unchanged, 'unchanged')
                expect(page.locator('#version-delta .vc-delta-value')).to_have_text('0.0')
                impact('improved', 0)
                impact('regressed', 0)
                expect(page.locator('#version-criteria-highlights [data-criterion]')).to_have_count(0)
                expect(page.locator('#version-improved-answers [data-case-id]')).to_have_count(0)

                select(mixed, 'unchanged')
                expect(page.locator('#version-delta .vc-delta-value')).to_have_text('0.0')
                impact('improved', 1)
                impact('regressed', 1)
                expect(page.locator('#version-criteria-highlights [data-criterion="J1"]')).to_have_attribute('data-change', 'improved')
                expect(page.locator('#version-criteria-highlights [data-criterion="J3"]')).to_have_attribute('data-change', 'regressed')
                expect(page.locator('#version-improved-answers [data-case-id="UI-Q01"]')).to_have_count(1)
                page.evaluate('scrollTo(0, 0)')
                page.screenshot(path=str(output / 'mixed-desktop.png'), full_page=True)

                select(incomplete, 'unresolved', ready=False)
                expect(page.locator('#version-delta .vc-delta-value')).to_have_text('—')
                expect(page.locator('[data-version-side="target"] .summary-score strong')).to_have_text('—')
                expect(page.locator('#version-criteria-highlights [data-criterion]')).to_have_count(0)
                expect(page.locator('#version-improved-answers [data-case-id="UI-Q01"]')).to_have_count(0)
                page.evaluate('scrollTo(0, 0)')
                page.screenshot(path=str(output / 'incomplete-desktop.png'), full_page=True)

                select(mismatch, 'unresolved', ready=False)
                expect(page.locator('#version-delta .vc-delta-value')).to_have_text('—')
                expect(page.locator('#version-criteria-highlights [data-criterion]')).to_have_count(0)
                expect(page.locator('#version-improved-answers [data-case-id]')).to_have_count(0)
                impact('improved', 0)
                impact('regressed', 0)

                select(literal, 'improved')
                page.locator('#version-improved-answers [data-case-id="UI-Q01"] .vc-answer-link').click()
                expect(page.locator('#version-answer-columns')).to_contain_text(literal_answer)
                assert page.evaluate('window.versionUnsafe') is None
                page.keyboard.press('Escape')
                select(same_answers, 'improved')
                same_card = page.locator('#version-improved-answers [data-case-id="UI-Q01"]')
                expect(same_card).to_have_attribute('data-answer-changed', 'false')
                expect(same_card).to_contain_text('답변 원문은 동일합니다.')
                expect(same_card.locator('.vc-answer-excerpt')).to_have_count(0)
                page.locator('#version-target-model').select_option('')
                expect(page.locator('#version-summary')).to_have_attribute('aria-busy', 'false')
                expect(page.locator('#version-delta .vc-delta-value')).to_have_text('—')
                expect(page.locator('#version-delta')).to_be_hidden()
                expect(page.locator('#version-highlights')).to_be_hidden()
                expect(page.locator('[data-version-side="target"]')).to_have_count(0)
                assert not errors, errors
                assert not posts, posts
                assert not external, external
                assert fixtures == originals, 'Presentation must not mutate saved verdicts or original answers'
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    (output / 'summary.json').write_text(json.dumps({
        'fixture_only': True, 'status': 'PASS', 'external_calls': 0, 'browser_posts': 0,
        'source_changed': False, 'scenarios': [
            'improved', 'regressed', 'unchanged', 'mixed', 'incomplete', 'profile_mismatch',
            'empty_target', 'literal_answer', 'matching_criterion_evidence',
            'same_answer_different_judgment',
            'comparison_route_preserved', 'selection_collapsed', 'mobile_390', 'desktop_1440',
        ],
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('PASS: version uplift, criterion changes, matching evidence, regression, zero net change, '
          'incomplete/condition guards, dialog and filter actions, route and collapsed selection, '
          'desktop/mobile; isolated fixtures, no model calls, no saved-result changes.')


if __name__ == '__main__':
    main()
