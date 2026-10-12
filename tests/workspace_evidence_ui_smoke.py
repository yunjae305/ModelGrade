"""Offline saved-data fixtures for web/context evidence; never call a provider."""
from __future__ import annotations

import copy
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from playwright.sync_api import expect, sync_playwright
from code_eval.combined import combined_summary
from code_eval.report_html import render_report
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=('openai_api', 'anthropic_api'), default='openai_api')
    engine = parser.parse_args().engine
    engine_label = 'Claude API' if engine == 'anthropic_api' else 'OpenAI API'
    model = 'claude-sonnet-5-5' if engine == 'anthropic_api' else 'offline-fixture-model'
    saved = ROOT / 'backups/run-history/20261012-105105/comparison_runs/a3b5d89bc041.json'
    original = saved.read_bytes()
    seed = json.loads(original)
    target = copy.deepcopy(seed)
    target.update(run_id='eeeeeeeeeee2', name='UI 근거 표시 검증용 — 실제 평가 아님', providers=['aiona'],
                  aiona_release={'version': '10.2', 'collected_at': None, 'notes': 'Offline synthetic UI fixture'})
    target['datasets'] = {'aiona': target['datasets']['aiona']}
    target['evaluations'] = {'aiona': target['evaluations']['aiona']}
    target.pop('blind', None)
    target.pop('blind_requested', None)
    target.pop('evaluation_log', None)
    batch = target['evaluations']['aiona']
    semantic = batch['jev_eval']
    semantic['engine'] = engine
    semantic.pop('engine_label', None)
    semantic['client'].update(engine=engine, requested_model=model, reasoning_effort='medium')
    semantic['models'] = [model]
    candidates = [i for i, row in enumerate(target['datasets']['aiona'])
                  if row.get('turn', 0) > 1 and not batch['cases'][i]['summary']['common']['fail']]
    web_index, unknown_index, na_index, reference_index, input_index, mixed_index = candidates[:6]
    web_id = target['datasets']['aiona'][web_index]['case_id']
    unknown_id = target['datasets']['aiona'][unknown_index]['case_id']
    na_id = target['datasets']['aiona'][na_index]['case_id']
    reference_id = target['datasets']['aiona'][reference_index]['case_id']
    input_id = target['datasets']['aiona'][input_index]['case_id']
    mixed_id = target['datasets']['aiona'][mixed_index]['case_id']
    claim = '검증용 주장 <script>window.evidenceUnsafe = true</script>'
    quote = '저장된 출처 인용 <img src=x onerror="window.evidenceUnsafe=true">'
    instruction = '이전 턴의 지시를 계속 유지해 주세요.'
    answer_quote = '현재 답변에서 유지된 내용입니다.'
    good_url = 'https://evidence.example/verified-fact'
    resolved_url = 'https://evidence.example/canonical-fact'
    authority = '공식 원문 작성 기관 <script>문자 그대로</script>'
    temporal = '질문의 기준 시점과 일치하는 게시일입니다.'
    failed_reason = '검증용: 요청한 인용문을 원문에서 찾지 못했습니다.'
    assessment_warning = '출처의 공신력 또는 기준 시점 적합성을 판단한 근거가 부족합니다.'
    for index in (web_index, unknown_index, na_index, reference_index, input_index, mixed_index):
        record = semantic['records'][index]
        for decision in record['decisions'].values():
            decision['decision'] = 'PASS'
            decision.pop('status', None)
        for cid in ('J5', 'J6'):
            if cid not in record['criteria']:
                record['criteria'].append(cid)
            record['decisions'][cid] = {'criterion_id': cid, 'name': cid, 'decision': 'PASS', 'confidence': 0.9,
                                        'reason': '오프라인 UI 검증용 판정 근거'}
        record['decisions']['J6']['context_checks'] = [{
            'turn': target['datasets']['aiona'][index]['turn'] - 1, 'instruction_quote': instruction,
            'answer_quote': answer_quote, 'decision': 'PASS', 'reason': '앞선 지시와 현재 답변을 함께 확인한 검증용 근거'}]
    record = semantic['records'][web_index]
    record['j5_basis'] = 'web'
    record['decisions']['J5']['fact_check'] = {'basis': 'web', 'claims': [{
        'claim': claim, 'verdict': 'PASS', 'reason': 'UI 검증용 출처 일치', 'sources': [
            {'url': good_url, 'title': '검증용 웹 출처', 'quote': quote, 'quote_verified': True,
             'verification_reason': '검증용: 인용문이 원문에 포함되어 있습니다.', 'verified_at': '2026-10-09T01:02:04Z',
             'resolved_url': resolved_url, 'authority': 'primary', 'authority_reason': authority, 'temporal_relevance': temporal},
            {'url': 'javascript:window.evidenceUnsafe=true', 'title': '실행 금지 출처 URL', 'quote': '<script>문자 그대로</script>',
             'quote_verified': False, 'verification_reason': failed_reason, 'resolved_url': 'javascript:window.evidenceUnsafe=true',
             'authority': 'unestablished', 'assessment_warning': assessment_warning},
            {'url': 'https://legacy.example/source', 'title': '과거 출처 — 원문 검증 기록 없음', 'quote': '당시 저장된 인용'}]}]}
    record['verification'] = {'search_performed': True, 'source_urls': [good_url, 'data:text/html,unsafe'],
                              'source_validation': 'provider_and_content', 'checked_at': '2026-10-09T01:02:03Z',
                              'source_checks': [{'url': good_url, 'status': 'ok', 'resolved_url': resolved_url,
                                                 'quote_count': 1, 'verified_quotes': 1, 'unverified_quotes': 0,
                                                 'checked_at': '2026-10-09T01:02:04Z'}]}
    unknown = semantic['records'][unknown_index]
    unknown['j5_basis'] = 'unverified'
    unknown['review_required'] = True
    unknown['decisions']['J5'].update(decision=None, status='UNVERIFIED', reason='검증용: 근거 부족으로 확인 필요',
                                      fact_check={'basis': 'none', 'claims': []},
                                      model_original_assessment={'choice': 'PASS', 'reason': '검증 전 원래 설명 <script>window.evidenceUnsafe=true</script>'})
    unknown['verification'] = {'search_performed': False, 'source_urls': [], 'source_validation': 'unavailable',
                               'checked_at': '2026-10-09T01:03:00Z'}
    no_fact = semantic['records'][na_index]
    no_fact['criteria'] = [cid for cid in no_fact['criteria'] if cid != 'J5']
    no_fact['decisions']['J5'].update(decision=None, status='NOT_APPLICABLE', reason='검증할 사실 주장이 없습니다.',
                                     fact_check={'basis': 'none', 'claims': []})
    no_fact.setdefault('applicability', {}).setdefault('not_applicable', {})['J5'] = '검증할 사실 주장이 없습니다.'
    no_fact['verification'] = {'search_performed': False, 'source_urls': [], 'checked_at': '2026-10-09T01:04:00Z'}
    reference_record = semantic['records'][reference_index]
    reference_record['j5_basis'] = 'reference'
    reference_record['decisions']['J5']['fact_check'] = {'basis': 'reference', 'claims': [{
        'claim': '참조 자료 표시 검증용 주장', 'verdict': 'PASS', 'reason': '제공된 참조 자료와의 일치만 평가한 검증용 근거',
        'sources': [{'url': '', 'title': '제공된 참조 자료', 'quote': '검증용 참조 자료 인용'}]}]}
    reference_record['verification'] = {'search_performed': False, 'source_urls': []}
    input_quote = '검증용 제공 자료 <script>window.evidenceUnsafe=true</script>'
    scope_quote = '다음 자료만 요약해 주세요. <img src=x onerror="window.evidenceUnsafe=true">'
    input_reason = '제공 자료의 원문에서 인용문을 확인했습니다. 자료 자체의 사실성은 검증하지 않습니다.'
    input_claim = {'claim': '제공 자료와 답변의 일치 검증용 주장', 'verdict': 'PASS',
                   'scope': 'input_consistency', 'scope_quote': scope_quote, 'reason': '제공된 자료 요약 검증',
                   'sources': [{'origin': origin, 'turn': 2 if origin == 'history_user' else None,
                                'url': 'https://input-only.example/must-not-link', 'title': '사용자가 제공한 원문',
                                'quote': input_quote, 'quote_verified': True, 'verification_reason': input_reason}
                               for origin in ('question', 'history_user', 'reference')]}
    input_record = semantic['records'][input_index]
    input_record['j5_basis'] = 'input'
    input_record['decisions']['J5']['fact_check'] = {'basis': 'input', 'claims': [input_claim]}
    input_record['verification'] = {'search_performed': False, 'source_urls': [], 'source_validation': 'provider'}
    mixed_record = semantic['records'][mixed_index]
    mixed_record['j5_basis'] = 'mixed'
    external_claim = copy.deepcopy(record['decisions']['J5']['fact_check']['claims'][0])
    external_claim.update(scope='external_fact', scope_quote=None)
    external_claim['sources'] = [{**external_claim['sources'][0], 'origin': 'web', 'turn': None}]
    mixed_record['decisions']['J5']['fact_check'] = {'basis': 'mixed', 'claims': [copy.deepcopy(input_claim), external_claim]}
    mixed_record['verification'] = copy.deepcopy(record['verification'])
    batch['combined'] = combined_summary(batch)
    before = copy.deepcopy(target)
    before.update(run_id='eeeeeeeeeee1', name='UI 기준 검증용 — 실제 평가 아님',
                  aiona_release={'version': '10.1', 'collected_at': None, 'notes': 'Offline synthetic UI fixture'})
    before_record = before['evaluations']['aiona']['jev_eval']['records'][unknown_index]
    before_record['decisions']['J5'].update(decision='PASS', status='EXECUTED')
    before_record.pop('review_required', None)
    output = ROOT / 'results/evidence_ui_review'
    output.mkdir(exist_ok=True)
    errors, writes, external = [], [], []

    def forbidden(*args, **kwargs):
        raise AssertionError('External generation must never run in UI verification')

    with tempfile.TemporaryDirectory(prefix='aiona-evidence-ui-') as directory:
        evaluator = CodeEvalService(save_dir=Path(directory) / 'eval', jev_resolver=forbidden)
        service = ComparisonService(evaluator, save_dir=Path(directory) / 'runs', client_factory=forbidden)
        service._runs[before['run_id']] = before
        service._runs[target['run_id']] = target
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('request', lambda r: writes.append(r.url) if r.method != 'GET' else None)
                base = f'http://127.0.0.1:{server.server_port}'

                def allow_local(route):
                    if route.request.url.startswith(base + '/'):
                        route.continue_()
                    else:
                        external.append(route.request.url)
                        route.abort()

                page.route('**/*', allow_local)
                def setup_fixture(route):
                    response = route.fetch()
                    body = response.json()
                    body['semantic_setup'] = {'engine': engine, 'engine_label': engine_label, 'model': model,
                        'reasoning_effort': 'medium', 'configuration_status': 'configured', 'can_evaluate': True,
                        'connection_status': 'not_checked', 'connection_message': '실제 연결은 확인하지 않은 UI 검증용 설정입니다.',
                        'search_status': 'configured', 'search_message': '웹 검색을 허용한 UI 검증용 설정입니다.'}
                    route.fulfill(response=response, json=body)

                page.route('**/api/code-eval/info', setup_fixture)
                page.add_init_script("sessionStorage.setItem('aiona-comparison-run', 'eeeeeeeeeee2')")
                page.goto(base + '/workspace.html#setup')
                expect(page.locator('#semantic-setup-note')).to_contain_text(engine_label)
                expect(page.locator('#semantic-setup-note')).to_contain_text(model + ' · 추론 medium')
                page.goto(base + '/workspace.html#results')
                page.reload()
                evaluator_info = page.locator('.result-model-card .hint').filter(has_text='의미 평가자')
                expect(evaluator_info).to_be_hidden()
                page.get_by_text('평가자 정보 보기', exact=True).click()
                expect(evaluator_info).to_be_visible()
                expect(evaluator_info).to_contain_text(engine_label + ' · ' + model + ' · 추론 medium')
                with page.expect_response(lambda response: response.url.endswith('/result')):
                    page.locator('#refresh-results').click()
                expect(evaluator_info).to_be_visible()
                page.locator('#matrix-search').fill(web_id)
                page.locator('#matrix-body .result-cell').first.click()
                detail = page.locator('#answer-evaluation')
                assert detail.locator('.semantic-fact-evidence').count(), (web_id, errors, detail.inner_text(), page.locator('#answer-id').inner_text())
                detail.locator('.semantic-fact-evidence > summary').click()
                detail.locator('.semantic-context-evidence > summary').click()
                detail.locator('.semantic-verification > summary').click()
                expect(detail).to_contain_text(claim)
                expect(detail).to_contain_text(quote)
                expect(detail).to_contain_text(instruction)
                expect(detail).to_contain_text(answer_quote)
                expect(detail).to_contain_text('웹 검색 수행: 예')
                expect(detail).to_contain_text('2026-10-09T01:02:03Z')
                expect(detail).to_contain_text('출처 URL을 도구 기록과 대조')
                expect(detail).to_contain_text('인용문 원문 대조를 시도함')
                expect(detail).to_contain_text('원문 인용: 확인됨')
                expect(detail).to_contain_text('원문 인용: 확인하지 못함')
                expect(detail).to_contain_text('원문 인용: 대조 기록 없음')
                expect(detail).to_contain_text(authority)
                expect(detail).to_contain_text('출처 분류: 공식·일차 자료')
                expect(detail).to_contain_text('출처 분류: 신뢰성 확인 불가')
                expect(detail).to_contain_text(assessment_warning)
                expect(detail).to_contain_text(temporal)
                expect(detail).to_contain_text(failed_reason)
                expect(detail).to_contain_text('2026-10-09T01:02:04Z')
                expect(detail.locator('a[href="' + good_url + '"]')).to_have_count(2)
                expect(detail.locator('a[href="' + resolved_url + '"]')).to_have_count(1)
                assert not detail.locator('a[href^="javascript:"],a[href^="data:"]').count()
                assert page.evaluate('window.evidenceUnsafe') is None
                page.locator('#answer-dialog').screenshot(path=str(output / 'workspace-evidence-fixture-desktop.png'))
                page.keyboard.press('Escape')
                page.locator('#matrix-search').fill(unknown_id)
                page.locator('#matrix-body .result-cell').first.click()
                expect(detail).to_contain_text('확인 필요')
                expect(detail).to_contain_text('근거 부족')
                original_detail = detail.locator('.semantic-original-assessment')
                expect(original_detail.locator('.jev-reason')).not_to_be_visible()
                original_detail.locator('summary').click()
                expect(original_detail).to_contain_text('모델 원판정: PASS')
                expect(original_detail).to_contain_text('서버 근거 검증 후 최종 판정: 확인 필요')
                expect(original_detail).to_contain_text('검증 전 원래 설명 <script>window.evidenceUnsafe=true</script>')
                expect(original_detail).to_contain_text('현재 결과와 점수에는 서버 근거 검증 후 최종 판정만 사용합니다.')
                status = page.evaluate("async ({id,index}) => {const r=await (await fetch('/api/comparison/runs/'+id+'/result')).json();const s=AionaResults.caseState(r,'aiona',index);return {outcome:s.outcome,complete:s.complete,fail:s.semantic.fail};}", {'id': target['run_id'], 'index': unknown_index})
                assert status == {'outcome': 'pending', 'complete': False, 'fail': 0}, status
                page.keyboard.press('Escape')
                page.locator('#matrix-search').fill(na_id)
                page.locator('#matrix-body .result-cell').first.click()
                expect(detail).to_contain_text('J5 해당 없음')
                assert not detail.locator('.semantic-fact-evidence').count()
                detail.locator('.semantic-verification > summary').click()
                expect(detail).to_contain_text('검색하지 않음')
                status = page.evaluate("async ({id,index}) => {const r=await (await fetch('/api/comparison/runs/'+id+'/result')).json();const s=AionaResults.caseState(r,'aiona',index);return {outcome:s.outcome,complete:s.complete,fail:s.semantic.fail};}", {'id': target['run_id'], 'index': na_index})
                assert status == {'outcome': 'clear', 'complete': True, 'fail': 0}, status
                page.keyboard.press('Escape')
                page.locator('#matrix-search').fill(reference_id)
                page.locator('#matrix-body .result-cell').first.click()
                detail.locator('.semantic-fact-evidence > summary').click()
                expect(detail).to_contain_text('제공된 참조 자료 인용 · 외부 출처 검증과 구분')
                expect(detail).not_to_contain_text('원문 인용: 확인됨')
                expect(detail).not_to_contain_text('원문 인용: 대조 기록 없음')
                page.keyboard.press('Escape')
                for case_id, basis in ((input_id, 'input'), (mixed_id, 'mixed')):
                    page.locator('#matrix-search').fill(case_id)
                    page.locator('#matrix-body .result-cell').first.click()
                    detail.locator('.semantic-fact-evidence > summary').click()
                    detail.locator('.semantic-verification > summary').click()
                    expect(detail).to_contain_text('판정 범위: 제공 자료 일치 · 자료 자체의 사실성은 검증하지 않음')
                    expect(detail).to_contain_text('제공 자료 대조 · 질문 원문')
                    expect(detail).to_contain_text('제공 자료 대조 · 이전 사용자 2턴')
                    expect(detail).to_contain_text('제공 자료 대조 · 참조 데이터')
                    expect(detail).to_contain_text('제공 자료 인용: 확인됨')
                    expect(detail).to_contain_text(input_quote)
                    expect(detail).to_contain_text(scope_quote)
                    expect(detail).to_contain_text(input_reason)
                    assert not detail.locator('a[href*="input-only.example"]').count()
                    if basis == 'input':
                        expect(detail).to_contain_text('웹 검색 수행: 검색하지 않음')
                        expect(detail).not_to_contain_text('출처 URL을 도구 기록과 대조')
                        expect(detail).not_to_contain_text('판정 범위: 외부 사실 확인')
                        page.locator('#answer-dialog').screenshot(path=str(output / 'workspace-input-evidence-desktop.png'))
                        page.set_viewport_size({'width': 390, 'height': 844})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        page.locator('#answer-dialog').screenshot(path=str(output / 'workspace-input-evidence-mobile.png'))
                        page.set_viewport_size({'width': 1440, 'height': 1080})
                    else:
                        expect(detail).to_contain_text('검증 기준: 제공 자료 일치와 외부 사실 확인 · 주장별 구분')
                        expect(detail).to_contain_text('판정 범위: 외부 사실 확인')
                        expect(detail).to_contain_text('원문 인용: 확인됨')
                        expect(detail.locator('a[href="' + good_url + '"]')).to_have_count(2)
                    assert page.evaluate('window.evidenceUnsafe') is None
                    page.keyboard.press('Escape')
                page.goto(base + '/workspace.html#comparison')
                page.locator('#version-baseline-model').select_option(before['run_id'])
                page.locator('#version-target-model').select_option(target['run_id'])
                page.locator('#version-change-filter').select_option('pending')
                expect(page.locator('#version-rows')).to_contain_text(unknown_id)
                expect(page.locator('#version-summary')).to_contain_text('미확정')
                page.locator('#version-rows button').first.click()
                expect(page.locator('#version-answer-columns')).to_contain_text('근거 부족')
                expect(page.locator('#version-answer-columns .semantic-context-evidence')).to_have_count(2)
                page.keyboard.press('Escape')
                page.locator('#version-change-filter').select_option('')
                page.locator('#version-rows tr').filter(has_text=web_id).get_by_role('button').click()
                for summary in page.locator('#version-answer-columns .semantic-fact-evidence > summary').all():
                    summary.click()
                expect(page.locator('#version-answer-columns')).to_contain_text('원문 인용: 확인됨')
                expect(page.locator('#version-answer-columns')).to_contain_text(failed_reason)
                page.keyboard.press('Escape')
                page.goto(base + f'/code-eval.html?comparison={target["run_id"]}&provider=aiona#results')
                expect(page.locator('#semantic-setup-title')).to_contain_text(engine_label + ' · ' + model + ' · 추론 medium')
                expect(page.locator('#jev-summary')).to_contain_text(engine_label + '로 판단한 의미 평가 결과')
                expect(page.locator('#jev-summary')).to_contain_text('추론 medium')
                page.locator('#case-search').fill(web_id)
                page.locator('#case-list button').first.click()
                code_detail = page.locator('#case-detail')
                code_detail.locator('.semantic-fact-evidence > summary').click()
                code_detail.locator('.semantic-context-evidence > summary').click()
                expect(code_detail).to_contain_text(engine_label)
                expect(code_detail).to_contain_text(claim)
                expect(code_detail).to_contain_text(instruction)
                expect(code_detail).to_contain_text('원문 인용: 확인됨')
                expect(code_detail).to_contain_text('원문 인용: 대조 기록 없음')
                expect(code_detail).to_contain_text(authority)
                page.locator('#case-search').fill(unknown_id)
                page.locator('#case-list button').first.click()
                expect(page.locator('#case-list')).to_contain_text('확인 필요')
                expect(code_detail).to_contain_text('확인 필요')
                code_detail.locator('.semantic-original-assessment > summary').click()
                expect(code_detail.locator('.semantic-original-assessment')).to_contain_text('서버 근거 검증 후 최종 판정: 확인 필요')
                assert 'NaN%' not in code_detail.inner_text()
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                code_detail.locator('.jev-box').screenshot(path=str(output / 'code-evidence-fixture-mobile.png'))
                page.locator('#case-search').fill(na_id)
                page.locator('#case-list button').first.click()
                expect(code_detail).to_contain_text('해당 없음 · J5')
                assert not code_detail.locator('.semantic-fact-evidence').count()
                report = render_report(batch)
                assert '<a href="javascript:' not in report and '<a href="data:' not in report
                assert '확인 필요' in report and engine_label in report and good_url in report
                assert f'의미 평가는 {engine_label}({model})로 판단했습니다.' in report
                assert '추론 수준은 medium입니다.' in report
                page.set_content(report)
                web_case = page.locator('details.case').filter(has=page.locator('summary > b', has_text=web_id)).first
                web_case.evaluate('(element) => { element.open = true; element.querySelectorAll("details").forEach(d => d.open = true); }')
                expect(web_case).to_contain_text(claim)
                expect(web_case).to_contain_text(quote)
                expect(web_case).to_contain_text(instruction)
                expect(web_case).to_contain_text('출처 URL을 도구 기록과 대조')
                expect(web_case).to_contain_text('인용문 원문 대조를 시도함')
                expect(web_case).to_contain_text('원문 인용: 확인됨')
                expect(web_case).to_contain_text('원문 인용: 확인하지 못함')
                expect(web_case).to_contain_text('원문 인용: 대조 기록 없음')
                expect(web_case).to_contain_text('출처 분류: 공식·일차 자료')
                expect(web_case).to_contain_text(authority)
                expect(web_case).to_contain_text(temporal)
                expect(web_case).to_contain_text(assessment_warning)
                expect(web_case).to_contain_text(failed_reason)
                expect(web_case.locator('a[href="' + resolved_url + '"]')).to_have_count(1)
                reference_case = page.locator('details.case').filter(has=page.locator('summary > b', has_text=reference_id)).first
                reference_case.evaluate('(element) => { element.open = true; element.querySelectorAll("details").forEach(d => d.open = true); }')
                expect(reference_case).to_contain_text('제공된 참조 자료 인용 · 외부 출처 검증과 구분')
                expect(reference_case).not_to_contain_text('원문 인용: 확인됨')
                expect(reference_case).not_to_contain_text('원문 인용: 대조 기록 없음')
                for case_id, basis in ((input_id, 'input'), (mixed_id, 'mixed')):
                    input_case = page.locator('details.case').filter(has=page.locator('summary > b', has_text=case_id)).first
                    input_case.evaluate('(element) => { element.open = true; element.querySelectorAll("details").forEach(d => d.open = true); }')
                    expect(input_case).to_contain_text('판정 범위: 제공 자료 일치 · 자료 자체의 사실성은 검증하지 않음')
                    expect(input_case).to_contain_text('제공 자료 대조 · 질문 원문')
                    expect(input_case).to_contain_text('제공 자료 대조 · 이전 사용자 2턴')
                    expect(input_case).to_contain_text('제공 자료 대조 · 참조 데이터')
                    expect(input_case).to_contain_text('제공 자료 인용: 확인됨')
                    expect(input_case).to_contain_text(input_quote)
                    expect(input_case).to_contain_text(scope_quote)
                    expect(input_case).to_contain_text(input_reason)
                    assert not input_case.locator('a[href*="input-only.example"]').count()
                    if basis == 'input':
                        expect(input_case).to_contain_text('웹 검색 수행: 검색하지 않음')
                        expect(input_case).not_to_contain_text('출처 URL을 도구 기록과 대조')
                        expect(input_case).not_to_contain_text('판정 범위: 외부 사실 확인')
                    else:
                        expect(input_case).to_contain_text('판정 범위: 외부 사실 확인')
                        expect(input_case).to_contain_text('검증 기준: 제공 자료 일치와 외부 사실 확인 · 주장별 구분')
                        expect(input_case.locator('a[href="' + good_url + '"]')).to_have_count(2)
                na_case = page.locator('details.case').filter(has=page.locator('summary > b', has_text=na_id)).first
                assert not na_case.locator('.semantic-fact-evidence').count()
                assert '해당 없음 · J5' in na_case.text_content()
                unknown_case = page.locator('details.case').filter(has=page.locator('summary > b', has_text=unknown_id)).first
                unknown_case.evaluate('(element) => { element.open = true; }')
                original_detail = unknown_case.locator('.semantic-original-assessment')
                expect(original_detail.locator('p').first).not_to_be_visible()
                original_detail.locator('summary').click()
                expect(original_detail).to_contain_text('모델 원판정: PASS')
                expect(original_detail).to_contain_text('서버 근거 검증 후 최종 판정: 확인 필요')
                assert not page.locator('a[href^="javascript:"],a[href^="data:"]').count()
                assert page.evaluate('window.evidenceUnsafe') is None
                assert not errors and not writes and not external, (errors, writes, external)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    assert hashlib.sha256(saved.read_bytes()).digest() == hashlib.sha256(original).digest()
    print(f'PASS ({engine}): workspace/version/detail/static-report engine/model/medium labels, web citations, literal quotes, J6 evidence, search provenance, '
          'input/mixed claim scopes and provided-data origins without false web links, UNVERIFIED pending (no PASS/FAIL), NOT_APPLICABLE complete without false warning, safe URLs, mobile. '
          'Synthetic offline fixtures; no provider calls or saved-data changes.')


if __name__ == '__main__':
    main()
