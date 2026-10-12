"""Opt-in live upload/collection/evaluation rehearsal, never a quality benchmark.

--live: two synthetic reference turns, six collection/eight judge requests.
--live --web: one public Python fact, three collection/four web judge requests.
--replay ARTIFACT_DIR: validate saved results in a browser without model calls.
All artifacts use a separate results directory, never the customer's run store.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from playwright.sync_api import expect, sync_playwright
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server


CASES = [
    {'case_id': 'LIVE_DEMO_01', 'conversation_id': 'SYNTHETIC_DEMO', 'turn': 1,
     'question': '시연용 가상 상품의 기준 자료입니다: 모아컵의 색상은 파랑입니다. 앞으로 저를 도담님이라고 불러주세요. 이 자료만 근거로 모아컵의 색상을 한 문장으로 알려주세요.',
     'answer': '도담님, 모아컵의 색상은 파랑입니다.',
     'reference': '모아컵의 색상은 파랑입니다.'},
    {'case_id': 'LIVE_DEMO_02', 'conversation_id': 'SYNTHETIC_DEMO', 'turn': 2,
     'question': '앞서 정한 호칭과 상품 정보를 그대로 유지해서 한 문장으로 다시 안내해주세요.',
     'answer': '도담님, 모아컵의 색상은 빨강입니다.',
     'reference': '모아컵의 색상은 파랑입니다.'},
]

WEB_CASES = [
    {'case_id': 'LIVE_WEB_DEMO_01',
     'question': 'Python 3의 list 객체는 생성 후 같은 객체의 원소를 변경할 수 있는지, 가변성 관점에서 짧게 설명해 주세요.',
     'answer': 'Python 3의 list는 불변(immutable) 객체이므로 생성한 뒤 같은 객체의 원소를 변경할 수 없습니다.'},
]


def semantic_summary(result):
    summary = {}
    for provider, batch in result.get('evaluations', {}).items():
        semantic = batch.get('jev_eval') or {}
        records = semantic.get('records') if isinstance(semantic.get('records'), list) else []
        details = []
        for record in records:
            record = record if isinstance(record, dict) else {}
            j5 = (record.get('decisions') or {}).get('J5') or {}
            fact = j5.get('fact_check') or {}
            verification = record.get('verification') or {}
            claims = fact.get('claims') if isinstance(fact.get('claims'), list) else []
            sources = [source for claim in claims if isinstance(claim, dict)
                       for source in (claim.get('sources') or []) if isinstance(source, dict)]
            verified_claims = {verdict: sum(claim.get('verdict') == verdict and any(
                isinstance(source, dict) and source.get('quote_verified') is True
                for source in (claim.get('sources') or [])) for claim in claims if isinstance(claim, dict))
                for verdict in ('PASS', 'FAIL', 'UNVERIFIED')}
            details.append({'case_id': record.get('case_id'), 'status': record.get('status'),
                            'j5_decision': j5.get('decision'), 'j5_status': j5.get('status'),
                            'j5_reason': j5.get('reason'), 'fact_basis': fact.get('basis'),
                            'search_performed': verification.get('search_performed'),
                            'source_validation': verification.get('source_validation'),
                            'verified_quotes': sum(source.get('quote_verified') is True for source in sources),
                            'verified_claims_by_verdict': verified_claims,
                            'unverified_quotes': sum(source.get('quote_verified') is not True for source in sources)})
        summary[provider] = {'status': semantic.get('status'), 'completed': semantic.get('completed'),
                             'unverified': sum(bool(record.get('review_required')) for record in records if isinstance(record, dict)),
                             'records': details}
    return summary


def web_check_failures(result):
    """The fixed AIONA answer is false; generated answers need matching evidence."""
    failures = []
    semantic = semantic_summary(result)
    for provider in ('aiona', 'gemini', 'claude', 'gpt'):
        rows = semantic.get(provider, {}).get('records', [])
        if len(rows) != 1:
            failures.append(f'{provider}: expected one semantic record, got {len(rows)}')
            continue
        record = rows[0]
        if record['status'] != 'SUCCESS':
            failures.append(f'{provider}: semantic status {record["status"]!r}, expected SUCCESS')
        decision = record['j5_decision']
        if provider == 'aiona' and decision != 'FAIL':
            failures.append(f'{provider}: J5 {decision!r}, expected FAIL for the fixed false answer')
        elif decision not in ('PASS', 'FAIL'):
            failures.append(f'{provider}: J5 {decision!r}, expected a completed PASS/FAIL verdict')
        if record['fact_basis'] != 'web':
            failures.append(f'{provider}: missing web fact basis')
        if record['search_performed'] is not True:
            failures.append(f'{provider}: no recorded web search')
        if record['verified_quotes'] < 1:
            failures.append(f'{provider}: no quotation verified against a source page')
        if decision in ('PASS', 'FAIL') and record['verified_claims_by_verdict'][decision] < 1:
            failures.append(f'{provider}: no verified {decision} claim supporting final J5 {decision}')
    return failures


def replay(artifact_dir):
    """Read saved live artifacts only; a separate temporary server forbids execution."""
    output = Path(artifact_dir).resolve()
    output.relative_to((ROOT / 'results').resolve())
    result_path, original_summary_path = output / 'result.json', output / 'summary.json'
    result = json.loads(result_path.read_text(encoding='utf-8'))
    original_summary = json.loads(original_summary_path.read_text(encoding='utf-8'))
    protected = list((ROOT / 'data/comparison_runs').glob('*.json'))
    if (ROOT / 'data/aiona_versions.json').exists():
        protected.append(ROOT / 'data/aiona_versions.json')
    protected += [p for p in output.rglob('*') if p.is_file() and not p.name.startswith('ui-recheck')]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    errors, writes, external, execution_attempts = [], [], [], []
    summary = {'check_result': 'RUNNING', 'mode': 'offline_replay', 'run_id': result['run_id'],
               'result_path': str(result_path), 'original_summary_path': str(original_summary_path),
               'original_summary_check_result': original_summary.get('check_result'),
               'semantic': semantic_summary(result), 'source_details': {}, 'model_calls': 0}
    is_web = original_summary.get('mode') == 'web'

    def forbidden(*args, **kwargs):
        execution_attempts.append('model or evaluator execution attempted')
        raise AssertionError('Replay must not execute a provider or semantic evaluator')

    server = None
    try:
        assert result['status'] == 'COMPLETED', result['status']
        if is_web:
            summary['web_check_failures'] = web_check_failures(result)
            assert not summary['web_check_failures'], summary['web_check_failures']
        with tempfile.TemporaryDirectory(prefix='aiona-live-replay-') as directory:
            temporary = Path(directory)
            evaluator = CodeEvalService(root=ROOT, save_dir=temporary / 'eval', jev_resolver=forbidden)
            service = ComparisonService(evaluator, save_dir=temporary / 'runs',
                                        registry_path=temporary / 'versions.json', client_factory=forbidden)
            service._runs[result['run_id']] = copy.deepcopy(result)
            server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.on('pageerror', lambda error: errors.append(str(error)))
                base = f'http://127.0.0.1:{server.server_port}'

                def only_read_local(route):
                    request = route.request
                    if not request.url.startswith(base + '/'):
                        external.append(request.url)
                        return route.abort()
                    if request.method != 'GET':
                        writes.append(request.url)
                        return route.abort()
                    return route.continue_()

                page.route('**/*', only_read_local)
                page.add_init_script('sessionStorage.setItem("aiona-comparison-run", ' + json.dumps(result['run_id']) + ')')
                page.goto(base + '/workspace.html#results')
                expect(page.locator('#final-summary')).to_have_attribute('data-ready', 'true')
                expect(page.locator('#final-summary .summary-score-card')).to_have_count(len(result['providers']))
                expect(page.locator('#progress-title')).to_have_text('평가 완료')
                expect(page.locator('#live-evaluation')).to_be_visible()
                processed_total = result['question_count'] * len(result['providers'])
                expect(page.locator('#live-semantic')).to_have_text(f'{processed_total:,} / {processed_total:,}개')
                summary.update(final_ready=True, summary_title=page.locator('#final-summary-title').inner_text(),
                               summary_description=page.locator('#final-summary-description').inner_text(),
                               score_cards=page.locator('#final-summary .summary-score-card').all_inner_texts())
                page.screenshot(path=str(output / 'ui-recheck-final-desktop.png'), full_page=True)
                page.locator('#matrix-body .row-button').first.click()
                labels = {'aiona': 'AIONA', 'gemini': 'Gemini', 'claude': 'Claude', 'gpt': 'GPT'}
                for provider in result['providers']:
                    page.get_by_role('tab', name=labels[provider], exact=True).click()
                    assert page.locator('#original-answer pre').text_content() == result['datasets'][provider][0]['answer']
                    if is_web:
                        record = result['evaluations'][provider]['jev_eval']['records'][0]
                        j5 = record['decisions']['J5']
                        claim = next(claim for claim in j5['fact_check']['claims'] if claim['verdict'] == j5['decision'] and any(
                            source.get('quote_verified') is True for source in claim.get('sources', [])))
                        source = next(source for source in claim['sources'] if source.get('quote_verified') is True)
                        evidence = page.locator('#answer-evaluation .semantic-fact-evidence')
                        evidence.locator('summary').click()
                        expect(evidence).to_contain_text('검증 기준: 웹 출처')
                        expect(evidence).to_contain_text('원문 인용: 확인됨')
                        expect(evidence).to_contain_text(claim['claim'])
                        expect(evidence).to_contain_text(source['quote'])
                        expect(evidence.locator('a[href^="https://"], a[href^="http://"]').first).to_be_visible()
                        assert not evidence.locator('a[href^="javascript:"], a[href^="data:"]').count()
                        verification = page.locator('#answer-evaluation .semantic-verification')
                        verification.locator('summary').click()
                        expect(verification).to_contain_text('웹 검색 수행: 예')
                        summary['source_details'][provider] = {'visible': True, 'j5': j5['decision'],
                                                               'matched_claim': claim['claim'], 'verified_url': source['url']}
                        page.screenshot(path=str(output / f'ui-recheck-{provider}-source.png'))
                page.locator('#answer-dialog [data-close]').click()
                page.reload()
                expect(page.locator('#final-summary')).to_have_attribute('data-ready', 'true')
                expect(page.locator('#final-summary-title')).to_have_text(summary['summary_title'])
                expect(page.locator('#final-summary-description')).to_have_text(summary['summary_description'])
                assert page.locator('#final-summary .summary-score-card').all_inner_texts() == summary['score_cards']
                assert page.evaluate('sessionStorage.getItem("aiona-comparison-run")') == result['run_id']
                summary['reload_preserved'] = True
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'ui-recheck-final-mobile.png'), full_page=True)
                summary['mobile_no_overflow'] = True
                assert not errors and not writes and not external and not execution_attempts, (errors, writes, external, execution_attempts)
                browser.close()
            server.shutdown()
            server.server_close()
            server = None
        assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in hashes.items())
        summary['check_result'] = 'PASS'
    except BaseException as exc:
        summary.update(check_result='FAIL', failure={'type': type(exc).__name__, 'message': str(exc)})
        raise
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        summary.update(browser_errors=errors, write_requests=writes, external_requests=external, execution_attempts=execution_attempts)
        summary['originals_unchanged'] = all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in hashes.items())
        recheck_path = output / 'ui-recheck-summary.json'
        recheck_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'result': summary['check_result'], 'recheck_summary_path': str(recheck_path),
                          'model_calls': 0, 'originals_unchanged': summary['originals_unchanged']}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='실제 API/CLI 사용량이 발생하는 리허설 실행에 명시적으로 동의')
    parser.add_argument('--web', action='store_true', help='참조 자료 없는 Python list 1문항으로 웹 검색·원문 인용 검증까지 확인')
    parser.add_argument('--replay', metavar='ARTIFACT_DIR', help='저장된 리허설 결과만 별도 서버에서 다시 표시·검사, 실제 모델 호출 없음')
    args = parser.parse_args()
    if args.replay:
        if args.live or args.web:
            parser.error('--replay는 --live 또는 --web과 함께 사용할 수 없습니다.')
        return replay(args.replay)
    if not args.live:
        parser.error('실제 모델을 호출하려면 --live를 명시해야 합니다.')
    cases = WEB_CASES if args.web else CASES
    mode = 'web' if args.web else 'reference'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = ROOT / 'results' / 'live-demo-flow' / (stamp + '-web' if args.web else stamp)
    output.mkdir(parents=True, exist_ok=False)
    original_paths = list((ROOT / 'data/comparison_runs').glob('*.json'))
    if (ROOT / 'data/aiona_versions.json').exists():
        original_paths.append(ROOT / 'data/aiona_versions.json')
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in original_paths}
    input_path = output / ('synthetic-web-check.json' if args.web else 'synthetic-connection-check.json')
    input_path.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding='utf-8')
    evaluator = CodeEvalService(root=ROOT, save_dir=output / 'evaluations')
    service = ComparisonService(evaluator, save_dir=output / 'comparison', registry_path=output / 'versions.json')
    server, run_id = None, None
    errors, snapshots = [], []
    started = time.monotonic()
    result_path = output / 'result.json'
    summary = {'synthetic': True, 'mode': mode, 'check_result': 'RUNNING', 'status': 'NOT_STARTED',
               'artifact_dir': str(output), 'input_path': str(input_path), 'result_path': str(result_path),
               'result_available': False, 'saved_run_path': None,
               'live_collection_requests': len(cases) * 3, 'live_judge_requests': len(cases) * 4,
               'original_run_count': len(list((ROOT / 'data/comparison_runs').glob('*.json')))}

    def capture_result():
        if not run_id:
            return None
        result = service.result(run_id)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        summary.update(run_id=run_id, status=result.get('status'), result_available=True,
                       saved_run_path=str(output / 'comparison' / f'{run_id}.json'),
                       models=result.get('models'), semantic=semantic_summary(result),
                       collection={p: {'success': value.get('success'), 'failed': value.get('failed'),
                                       'incomplete': value.get('incomplete')}
                                   for p, value in result.get('progress', {}).items()})
        if args.web:
            summary['web_check_failures'] = web_check_failures(result)
        return result

    try:
        service.load_environment(env_path=ROOT / '.env')
        configured = service.providers()
        if not all(row['configured'] for row in configured.values()):
            raise RuntimeError('리허설 서버에 수집 모델 설정이 부족합니다. 실제 요청은 시작하지 않았습니다.')
        setup = evaluator.info().get('semantic_setup') or {}
        summary['semantic_setup'] = setup
        if setup.get('can_evaluate') is False:
            raise RuntimeError('의미 평가자 설정이 준비되지 않았습니다. 실제 요청은 시작하지 않았습니다.')
        if args.web and (setup.get('engine') != 'codex_cli' or setup.get('search_status') != 'configured'):
            raise RuntimeError('--web 리허설에는 웹 검색이 설정된 GPT CLI 평가자가 필요합니다. 실제 요청은 시작하지 않았습니다.')
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1080})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/workspace.html')
            expect(page.locator('#model-selection-count')).to_have_text('4 / 4 연결')
            page.locator('#dataset-file').set_input_files(str(input_path))
            expect(page.locator('#question-count')).to_contain_text(str(len(cases)))
            page.locator('#aiona-version').fill('연결검증용 합성 자료 — 실제 AIONA 성능 아님')
            page.locator('.advanced-options summary').click()
            page.locator('#run-name').fill('웹 검색 리허설 · Python list 합성 오답 · 실제 AIONA 성능 아님' if args.web else '실연결 리허설 · 합성 2턴 · 실제 AIONA 성능 아님')
            page.locator('#max-tokens').select_option('4096')
            expect(page.locator('#collect-button')).to_be_enabled()
            page.screenshot(path=str(output / '01-uploaded.png'), full_page=True)
            page.locator('#collect-button').click()
            expect(page.locator('#view-results')).to_be_visible()
            run_id = page.evaluate('sessionStorage.getItem("aiona-comparison-run")')
            if not run_id:
                raise RuntimeError('새 실행 ID를 받지 못했습니다.')
            print(json.dumps({'artifact_dir': str(output), 'run_id': run_id,
                              'synthetic': True, 'mode': mode, 'result_path': str(result_path),
                              'live_collection_requests': len(cases) * 3, 'live_judge_requests': len(cases) * 4}), flush=True)
            seen, last_log = set(), 0
            while time.monotonic() - started < 900:
                status = service.status(run_id)
                phase = status['status']
                if phase not in seen:
                    seen.add(phase)
                    snapshots.append(phase)
                    page.screenshot(path=str(output / f'phase-{phase.lower()}.png'), full_page=True)
                if time.monotonic() - last_log > 15:
                    print(json.dumps({'status': phase, 'collected': status.get('done'),
                                      'evaluated_models': status.get('eval_done', 0),
                                      'elapsed_s': round(time.monotonic() - started)}), flush=True)
                    last_log = time.monotonic()
                if phase in ('COMPLETED', 'ERROR', 'CANCELLED'):
                    break
                page.wait_for_timeout(500)
            result = capture_result()
            if result['status'] not in ('COMPLETED', 'ERROR', 'CANCELLED'):
                raise TimeoutError('900초 안에 리허설이 종료되지 않았습니다. 마지막 원문·검색·평가 기록은 result_path에서 확인하세요.')
            expect(page.locator('#final-summary')).to_be_visible(timeout=15000)
            page.screenshot(path=str(output / '04-final-results.png'), full_page=True)
            summary.update(summary_title=page.locator('#final-summary-title').inner_text(),
                           summary_description=page.locator('#final-summary-description').inner_text(),
                           final_ready=page.locator('#final-summary').get_attribute('data-ready'))
            page.locator('#matrix-body .row-button').first.click()
            expect(page.locator('#answer-dialog')).to_be_visible()
            page.get_by_role('tab', name='AIONA', exact=True).click()
            expect(page.locator('#original-answer pre')).to_have_text(cases[0]['answer'])
            if args.web:
                assert not summary['web_check_failures'], summary['web_check_failures']
                evidence = page.locator('#answer-evaluation .semantic-fact-evidence')
                expect(evidence).to_be_visible()
                evidence.locator('summary').click()
                expect(evidence).to_contain_text('검증 기준: 웹 출처')
                expect(evidence).to_contain_text('원문 인용: 확인됨')
                expect(evidence.locator('a[href^="https://"], a[href^="http://"]').first).to_be_visible()
                assert not evidence.locator('a[href^="javascript:"], a[href^="data:"]').count()
                summary['web_evidence_visible'] = True
            page.screenshot(path=str(output / '05-answer-detail.png'), full_page=True)
            page.locator('#answer-dialog [data-close]').click()
            page.reload()
            expect(page.locator('#final-summary')).to_be_visible()
            assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in hashes.items())
            assert result['status'] == 'COMPLETED', summary
            assert result.get('auto_evaluate') is True, summary
            assert all(row.get('success') == len(cases) for row in result['progress'].values()), summary
            assert summary['final_ready'] == 'true', summary
            assert not errors, errors
            summary['check_result'] = 'PASS'
            browser.close()
    except BaseException as exc:
        summary.update(check_result='FAIL', failure={'type': type(exc).__name__, 'message': str(exc)})
        raise
    finally:
        try:
            capture_result()
        except Exception as exc:
            summary['result_capture_error'] = str(exc)
        summary.update(elapsed_s=round(time.monotonic() - started), observed_states=snapshots, browser_errors=errors)
        try:
            summary['originals_unchanged'] = all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in hashes.items())
        except OSError:
            summary['originals_unchanged'] = False
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'result': summary['check_result'], 'summary_path': str(output / 'summary.json'),
                          'result_path': str(result_path), 'result_available': summary['result_available']}, ensure_ascii=False), flush=True)
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
