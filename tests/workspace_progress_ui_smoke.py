"""Mentor progress navigation, real artifacts, JSON refresh and failure states without paid model calls."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import sys
import tempfile
import threading

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Handler, Server


def group_of(status):
    if status.startswith('완료'):
        return 'done'
    if status == '부분완료':
        return 'partial'
    return 'pending' if status in ('승인대기', '확인필요') else 'todo'


def main():
    data_path = ROOT / 'frontend/data/requirements.json'
    artifacts_path = ROOT / 'frontend/data/artifacts.json'
    original_data, original_artifacts = data_path.read_bytes(), artifacts_path.read_bytes()
    data, artifacts = json.loads(original_data), json.loads(original_artifacts)
    items = data['items']
    by_requirement = {}
    for artifact in artifacts['artifacts']:
        by_requirement.setdefault(artifact['requirement_id'], []).append(artifact)
    by_id = {a['artifact_id']: a for a in artifacts['artifacts']}
    groups = [group_of(item['status']) for item in items]
    assert len(items) == 18
    output = ROOT / 'results/progress-workspace'
    output.mkdir(parents=True, exist_ok=True)
    errors, writes = [], []

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Progress browsing must not invoke a model')

    Handler.log_message = lambda *_args: None
    with tempfile.TemporaryDirectory(prefix='aiona-progress-') as directory:
        evaluator = CodeEvalService(save_dir=Path(directory) / 'eval', jev_resolver=forbidden)
        service = ComparisonService(evaluator, save_dir=Path(directory) / 'runs', client_factory=forbidden)
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 900}, accept_downloads=True)
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: writes.append(request.url) if request.method != 'GET' else None)
                url = f'http://127.0.0.1:{server.server_port}/workspace.html'
                page.goto(url + '#progress')
                expect(page.locator('#view-progress')).to_be_visible()
                expect(page.locator('.progress-item')).to_have_count(18)
                for index, (group, label) in enumerate([(None, len(items)), ('done', None), ('partial', None), ('todo', None), ('pending', None)]):
                    expect(page.locator('.progress-metric').nth(index)).to_contain_text(str(label if group is None else groups.count(group)))
                expect(page.locator('.progress-metric').nth(5)).to_contain_text('미측정')
                implemented = sum(item['stages']['implemented']['state'] == 'done' for item in items)
                expect(page.locator('.progress-stage-metric').nth(0)).to_contain_text(f'확인 {implemented}')
                expect(page.locator('.progress-stage-metric').nth(4)).to_contain_text(f"{len(artifacts['artifacts'])}개")

                # W01 — 담당자 요구사항 A~D 와 실제 산출물, 프롬프트 원문, 파일 확인
                w01 = page.locator('#requirement-W01')
                w01.locator('summary').click()
                generated = by_id['W01-SET326']   # 실제 생성 질문 세트 — 질문 세트 326 (10-15)
                questions = generated['content']['table']['rows']
                expect(w01.locator('.progress-executive h3')).to_contain_text('생성기 ' + by_id['W01-PROMPT']['content']['version'].removeprefix('question-gen-'))
                expect(w01.locator('.progress-executive h3')).to_contain_text(f'{len(questions)}개')
                expect(w01.locator('.progress-executive h3')).to_contain_text(
                    next(s['value'] for s in by_id['W01-INTENT']['content']['stats'] if s['label'] == '적용률'))
                expect(w01.locator('.progress-source-line').first).to_contain_text('질문 세트 326')
                expect(w01.locator('.progress-role')).to_contain_text('W02')
                expect(w01.locator('.progress-focus-metric')).to_have_count(4)
                expect(w01.locator('.progress-focus-metric').last).to_contain_text('미실행')
                expect(w01.locator('.progress-verification-step')).to_have_count(4)
                # ① 왜 → ② 원리 → ③ 실제 질문 → ④ 잘 작동했나 → ⑤ 개선 (수치·문장은 artifacts.json 그대로)
                expect(w01.locator('.w01-step h3')).to_have_text(['왜 질문을 만들었나요?', '어떤 원리로 만들었나요?', '실제로 어떤 질문을 만들었나요?',
                                                               '실제로 잘 작동했나요?', '무엇을 개선해야 하나요?'])
                design = by_id['W01-DESIGN']['content']
                expect(w01.locator('.w01-glossary .w01-term')).to_have_count(len(design['glossary']))
                expect(w01.locator('#w01-step-1')).to_contain_text(' · '.join(str(n['number']) for n in design['numbers']))
                expect(w01.locator('.w01-flow-step')).to_have_count(len(design['flow']))
                expect(w01.locator('.w01-part')).to_have_count(len(design['prompt_parts']))
                batch = by_id['W01-QUANTITY']['content']['batch']
                expect(w01.locator('.w01-callout')).to_contain_text(batch['introduced']['commit'])
                expect(w01.locator('.w01-callout')).to_contain_text('적정 개수는 미검증')
                expect(w01.locator('.w01-four-card')).to_have_count(4)
                intent = by_id['W01-INTENT']['content']
                applied = next(s['value'] for s in intent['stats'] if s['label'] == '실제 적용')
                missed = next(s['value'] for s in intent['stats'] if s['label'] == '적용 안 됨')
                pending = next(s['value'] for s in intent['stats'] if s['label'] == '답변 필요')
                funnel = intent['funnel']
                expect(w01.locator('.w01-funnel')).to_have_attribute('aria-label',
                    f"목표 {funnel['intended']}건 중 실제 적용 {applied}건, 미적용 {missed}건, 판단 보류 {pending}건. 적용률은 {applied} ÷ {applied + missed}.")
                expect(w01.locator('.w01-figure .w01-legend')).to_contain_text(f'판단 가능 {applied + missed}건 중 {applied / (applied + missed) * 100:.1f}%')
                j6 = next(row for row in intent['families'] if row['family'] == 'J6')
                if j6['missed']:   # J6 를 짧은 대화에 노려 빗나간 건이 있을 때만 J6 원인 단락
                    expect(w01.locator('.w01-j6')).to_contain_text('multi-turn-v2')
                    expect(w01.locator('.w01-j6 h4')).to_contain_text(f"{j6['missed']}건")
                else:
                    expect(w01.locator('.w01-j6')).to_have_count(0)
                expect(w01.locator('.w01-mini tbody tr')).to_have_count(sum(row['missed'] > 0 for row in intent['families']))
                top = max(intent['families'], key=lambda row: row['missed'])
                expect(w01.locator('.w01-mini tbody tr').first).to_contain_text(f"{top['family']} · {top['name']}")
                cases = generated['content']['cases']
                expect(w01.locator('.w01-case')).to_have_count(len(cases))
                conversation = next(case for case in cases if 'turns' in case)
                expect(w01.locator('.w01-chat .w01-bubble')).to_have_count(len(conversation['highlights']))
                w01.locator('.w01-case-wide summary').click()
                expect(w01.locator('.w01-timeline .w01-bubble')).to_have_count(conversation['turns_total'])
                single = w01.locator('.w01-case').first.bounding_box()['height']
                wide = w01.locator('.w01-case-wide').bounding_box()['height']
                assert single < 500 and single < wide / 2, (single, wide)   # 짧은 사례 카드가 긴 대화 카드 높이로 늘어나지 않는다
                w01.scroll_into_view_if_needed()
                page.screenshot(path=str(output / 'w01-after-desktop.png'), full_page=False)
                expect(w01.locator('.progress-detailed > summary')).to_contain_text('기술 자료 및 원본 보기')
                w01.locator('.progress-detailed > summary').click()
                expect(w01.locator('.progress-artifact')).to_have_count(len(by_requirement['W01']))
                expect(page.locator('#artifact-W01-INTENT .progress-result')).to_have_text(by_id['W01-INTENT']['result_summary'])
                prompt = by_id['W01-PROMPT']['content']['prompts'][0]
                page.locator('#artifact-W01-PROMPT .progress-prompt summary').first.click()
                expect(page.locator('#artifact-W01-PROMPT .progress-prompt pre').first).to_contain_text(prompt['text'][:60])
                page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
                page.locator('#artifact-W01-PROMPT .progress-prompt').first.get_by_role('button', name='원문 복사').click()
                expect(page.locator('#artifact-W01-PROMPT .progress-prompt').first).to_contain_text('복사됨')
                copied = page.evaluate('navigator.clipboard.readText()')
                assert copied.replace('\r\n', '\n') == prompt['text'].replace('\r\n', '\n'), (len(copied), len(prompt['text']), repr(copied[:80]), repr(prompt['text'][:80]))
                with page.expect_download() as prompt_download:
                    page.locator('#artifact-W01-PROMPT .progress-prompt').first.get_by_role('button', name='원문 다운로드').click()
                assert Path(prompt_download.value.path()).read_text(encoding='utf-8') == prompt['text']
                expect(page.locator('#artifact-W01-SET326 .progress-file').first).to_have_text('이 PC 에 있음')
                expect(page.locator('#artifact-W01-SET326 .progress-sample')).to_have_count(len(generated['content']['samples']))
                full_questions = page.locator('#artifact-W01-SET326 .progress-table-block').first
                pages = -(-len(questions) // 25)
                expect(full_questions.locator('tbody tr')).to_have_count(25)
                expect(full_questions.locator('.progress-paging')).to_have_text(f'1 / {pages}쪽')
                first_question = full_questions.locator('tbody tr').first.inner_text()
                full_questions.get_by_role('button', name='다음').click()
                expect(full_questions.locator('.progress-paging')).to_have_text(f'2 / {pages}쪽')
                assert full_questions.locator('tbody tr').first.inner_text() != first_question
                hard = [row for row in questions if row['difficulty'] == '어려움']
                empty = '아무것도 출력하지 마.'
                hard_empty = sum(any(empty in str(value) for value in row.values()) for row in hard)
                full_questions.get_by_label('난도 필터').select_option('어려움')
                expect(full_questions.locator('.progress-table-count')).to_have_text(f'{len(questions)}행 중 {len(hard)}행')
                full_questions.get_by_label(generated['title'] + ' 검색').fill(empty)
                expect(full_questions.locator('.progress-table-count')).to_have_text(f'{len(questions)}행 중 {hard_empty}행')
                full_questions.get_by_label('난도 필터').select_option('')
                expect(full_questions.locator('tbody')).to_contain_text(empty)
                full_questions.get_by_label(generated['title'] + ' 검색').fill('')
                full_questions.get_by_label('난도 필터').select_option('어려움')
                with page.expect_download() as questions_download:
                    full_questions.get_by_role('button', name='CSV 다운로드').click()
                assert len(Path(questions_download.value.path()).read_text(encoding='utf-8-sig').splitlines()) == len(hard) + 1
                w01.locator('.progress-management > summary').click()
                expect(w01.locator('.progress-section-letter')).to_have_text(['B', 'A', 'C', 'D', '→'])
                expect(w01).to_contain_text(data['items'][0]['acceptance'])
                expect(w01).to_contain_text(data['items'][0]['meeting']['request'])

                # W02 — 그래프 수치·표 필터·CSV 가 artifacts.json 과 같다
                page.locator('#requirement-W02 summary').click()
                expect(page.locator('#requirement-W02 .progress-key-results .progress-chart')).to_have_count(2)
                page.locator('#requirement-W02 .progress-detailed > summary').click()
                criteria = by_id['W02-CRITERIA']['content']['charts'][0]
                chart = page.locator('#requirement-W02 .progress-key-results .progress-chart').nth(1)
                expected = [f'{v:,}' for row in range(len(criteria['categories'])) for v in (s['values'][row] for s in criteria['series'])]
                expect(chart.locator('svg text.value')).to_have_count(len(expected))
                values = chart.locator('svg text.value').all_text_contents()
                assert values == expected, (values, expected)
                chart.locator('.progress-chart-data summary').click()
                first_row = chart.locator('.progress-chart-data tbody tr').first.locator('td').all_text_contents()
                assert first_row == [(criteria.get('category_labels') or criteria['categories'])[0]] + [f"{s['values'][0]:,}" for s in criteria['series']], first_row
                matrix = page.locator('#artifact-W02-MATRIX')
                rows = by_id['W02-MATRIX']['content']['table']['rows']
                matrix.get_by_label('구분 필터').select_option('의미 점검 항목')
                semantic = sum(r['group'] == '의미 점검 항목' for r in rows)
                expect(matrix.locator('.progress-table-count').last).to_have_text(f'{len(rows)}행 중 {semantic}행')
                with page.expect_download() as download_info:
                    matrix.get_by_role('button', name='CSV 다운로드').last.click()
                download = download_info.value
                assert download.suggested_filename == by_id['W02-MATRIX']['content']['table']['download']
                lines = Path(download.path()).read_text(encoding='utf-8-sig').strip().splitlines()
                assert len(lines) == semantic + 1 and lines[0].startswith('구분,상위 기준,ID')

                # W03 — 선 그래프 끝 라벨과 결론, 서로 다른 자료 경고
                page.locator('#requirement-W03 summary').click()
                expect(page.locator('#requirement-W03 .progress-key-results .progress-chart')).to_have_count(2)
                expect(page.locator('#requirement-W03 .progress-key-results')).to_contain_text('질문 수를 숫자로 정할 수 없다')
                page.locator('#requirement-W03 .progress-detailed > summary').click()
                line = by_id['W03-COVERAGE']['content']['charts'][0]
                last = line['series'][-1]
                expect(page.locator('#requirement-W03 .progress-key-results svg').first).to_contain_text(f"{last['name']} {last['values'][-1]}")
                expect(page.locator('#requirement-W03 .progress-warning')).to_contain_text('같은 조건')
                expect(page.locator('#artifact-W03-CONCLUSION')).to_contain_text(by_id['W03-CONCLUSION']['content']['judgment'][:30])

                for latest_id in ['W04', 'W08']:   # 질문 세트 326 수집 실행 (예전 질문 세트 300 의 재시도 전 이력 산출물은 없음 — 10-15)
                    latest = page.locator(f'#requirement-{latest_id}')
                    latest.locator('summary').click()
                    expect(latest.locator('.progress-executive .progress-source-line')).to_contain_text(by_id['W04-AIONA-RUN']['content']['run_id'])
                    expect(latest.locator('.progress-executive .progress-source-line')).not_to_contain_text('이전 실행 결과')
                expect(page.locator('#artifact-W04-AIONA-HISTORY')).to_have_count(0)

                # 우영 담당 통합 요약 A·B·C — 수치는 artifacts.json overview 그대로, 상세 보기로 W01~W09 연결
                overview = {g['id']: g for g in artifacts['overview']['groups']}
                page.get_by_role('button', name='우영 담당 · 9개').click()
                panel = page.locator('#progress-owner-overview')
                expect(panel).to_be_visible()
                expect(panel.locator('.progress-group-tile')).to_have_count(3)
                expect(panel.locator('.progress-group-card')).to_have_count(4)   # A·B·C + 남은 일
                expect(panel.locator('.progress-demo-nav button')).to_have_count(5)
                for gid, group in overview.items():
                    card = panel.locator(f'#progress-group-{gid}')
                    expect(card.locator('.progress-group-finding')).to_have_text(group['finding'])
                    expect(card.locator('.progress-focus-metric strong')).to_have_text(
                        [f"{m['value']:,}" if isinstance(m['value'], int) else str(m['value']) for m in group['metrics']])
                    expect(card.locator('.progress-group-issue')).to_contain_text(group['open_issue'])
                    expect(card.locator('.progress-group-req')).to_have_count(len(group['requirements']))
                a_card = panel.locator('#progress-group-A')
                expect(a_card.locator('.progress-intent-bar')).to_have_attribute('aria-label',
                    f'판단 가능 {applied + missed}건 중 실제 적용 {applied}건, 미적용 {missed}건. 별도 판단 보류 {pending}건.')
                panel.screenshot(path=str(output / 'woo-overview-desktop.png'))
                a_card.get_by_role('button', name='기준별 질문 커버리지').click()
                coverage = by_id['W02-MATRIX']['content']['charts'][0]['series'][0]['values']
                expect(a_card.locator('svg text.value')).to_have_text([f'{v:,}' for v in coverage])
                a_card.get_by_role('button', name='질문 수 연구').click()
                expect(a_card.locator('svg')).to_contain_text(f"{last['name']} {last['values'][-1]}")
                b_card = panel.locator('#progress-group-B')
                expect(b_card.locator('svg text.value')).to_have_text([f'{v:g}%' for v in overview['B']['chart']['series'][0]['values']])
                expect(b_card.locator('.progress-missing span')).to_have_count(len(overview['B']['missing']))
                b_card.locator('summary', has_text='서비스별 수집 기록 표').click()
                services = len(overview['B']['table']['rows'])
                expect(b_card.locator('.progress-table-count').last).to_have_text(f'{services}행 중 {services}행')
                expect(b_card.locator('.progress-table').last).not_to_contain_text('재시도 전')
                c_card = panel.locator('#progress-group-C')
                states = {'real': '실제 실행 완료', 'tested': '코드 구현·테스트 완료', 'partial': '부분 완료', 'pending': '미실행·검증 대기'}
                expect(c_card.locator('.progress-pipeline-state')).to_have_text([states[s['state']] for s in overview['C']['steps']])
                expect(c_card.locator('.progress-file')).to_contain_text('비공개 원본')
                panel.get_by_role('button', name='④ C 평가 연동').click()
                expect(c_card).to_be_in_viewport()
                expect(panel.locator('#progress-group-plan')).to_contain_text(data['due'])
                panel.get_by_role('button', name='W03 상세 보기').click()
                expect(page.locator('#requirement-W03')).to_have_attribute('open', '')
                expect(page.locator('#requirement-W03 .progress-key-results .progress-chart')).to_have_count(2)
                page.get_by_label('담당자 필터').select_option('윤재')
                expect(panel).to_be_hidden()
                page.get_by_label('담당자 필터').select_option('all')

                # W06 — 산출물 없음 안내, 근거 없는 칸은 미확인
                page.locator('#requirement-W06 summary').click()
                page.locator('#requirement-W06 .progress-detailed > summary').click()
                page.locator('#requirement-W06 .progress-management > summary').click()
                expect(page.locator('#requirement-W06 .progress-no-artifact').last).to_contain_text('실제 산출물 없음')
                expect(page.locator('#requirement-W06 .progress-unverified').first).to_have_text('미확인')
                page.screenshot(path=str(output / 'progress-desktop.png'), full_page=False)

                # 담당자·상태·검색 필터
                page.get_by_label('담당자 필터').select_option('우영')
                expect(page.locator('.progress-item')).to_have_count(9)
                page.get_by_label('담당자 필터').select_option('윤재')
                expect(page.locator('.progress-item')).to_have_count(7)
                page.get_by_label('담당자 필터').select_option('공동')
                expect(page.locator('.progress-item')).to_have_count(2)
                page.get_by_label('담당자 필터').select_option('all')
                page.get_by_label('상태 필터').select_option('pending')
                expect(page.locator('.progress-item')).to_have_count(groups.count('pending'))
                page.get_by_label('상태 필터').select_option('all')
                page.get_by_label('요구사항 검색').fill('게이트웨이')   # 산출물 제목까지 찾는다 (W07-FACTCHAT-API)
                expect(page.locator('#requirement-W07')).to_be_visible()
                page.get_by_label('요구사항 검색').fill('')
                page.reload()
                expect(page).to_have_url(url + '#progress')
                expect(page.locator('.progress-item')).to_have_count(18)
                page.locator('[data-view="results"]').click()
                expect(page.locator('#view-results')).to_be_visible()
                page.locator('[data-view="progress"]').click()
                expect(page.locator('.progress-item')).to_have_count(18)

                # 비공개 원본이 없는 PC · 잘못된 경로 · 형식이 틀린 산출물
                changed = json.loads(json.dumps(artifacts))
                changed['artifacts'].append({**by_id['W09-CONTRACT'], 'artifact_id': 'W06-BAD-PATH', 'requirement_id': 'W06',
                                             'file_or_result_reference': [{'label': '잘못된 경로', 'kind': 'repo_file', 'path': '../outside.txt'}]})
                changed['artifacts'].append({'artifact_id': 'BROKEN', 'requirement_id': 'W06', 'title': '형식 오류'})

                def files(route):
                    asked = parse_qs(urlparse(route.request.url).query).get('path', [])
                    route.fulfill(status=200, content_type='application/json', body=json.dumps({'files': [
                        {'path': p, 'status': 'missing' if p.startswith('data/') else 'invalid' if '..' in p else 'present'} for p in asked]}))
                page.route('**/data/artifacts.json', lambda route: route.fulfill(status=200, content_type='application/json; charset=utf-8',
                                                                                  body=json.dumps(changed, ensure_ascii=False)))
                page.route('**/api/progress/files*', files)
                page.get_by_role('button', name='새로고침').click()
                expect(page.locator('.progress-note')).to_contain_text('형식이 맞지 않는 산출물 1개')
                page.locator('#requirement-W03 summary').click()
                page.locator('#requirement-W03 .progress-detailed > summary').click()
                expect(page.locator('#artifact-W03-STABILITY .progress-file').first).to_contain_text('자료 없음')
                expect(page.locator('#artifact-W03-STABILITY')).to_contain_text(by_id['W03-STABILITY']['result_summary'][:20])
                page.locator('#requirement-W06 summary').click()
                page.locator('#requirement-W06 .progress-detailed > summary').click()
                expect(page.locator('#artifact-W06-BAD-PATH .progress-file')).to_have_text('잘못된 경로')
                page.unroute('**/api/progress/files*')
                page.unroute('**/data/artifacts.json')

                # 산출물 파일을 못 읽으면 요구사항은 그대로, 산출물 칸에 사유
                page.route('**/data/artifacts.json', lambda route: route.fulfill(status=404, body='{}'))
                page.get_by_role('button', name='새로고침').click()
                expect(page.locator('.progress-item')).to_have_count(18)
                expect(page.locator('.progress-note')).to_contain_text('산출물 자료를 불러오지 못했습니다')
                page.locator('#requirement-W01 summary').click()
                page.locator('#requirement-W01 .progress-detailed > summary').click()
                expect(page.locator('#requirement-W01 .progress-error-inline')).to_contain_text('HTTP 404')
                page.unroute('**/data/artifacts.json')

                # requirements.json 갱신이 다시 열 때 반영되고 원본 파일은 바뀌지 않는다
                edited = json.loads(json.dumps(data))
                edited['items'][0]['status'] = '완료(구현)'
                edited['as_of'] = '테스트용 갱신'
                page.route('**/data/requirements.json', lambda route: route.fulfill(
                    status=200, content_type='application/json; charset=utf-8', body=json.dumps(edited, ensure_ascii=False)))
                page.locator('[data-view="setup"]').click()
                page.locator('[data-view="progress"]').click()
                expect(page.locator('.progress-date')).to_contain_text('테스트용 갱신')
                expect(page.locator('.progress-metric').nth(1)).to_contain_text(str(groups.count('done') + 1))
                expect(page.locator('#requirement-W01')).to_contain_text('완료(구현)')
                page.unroute('**/data/requirements.json')
                page.locator('[data-view="setup"]').click()
                page.locator('[data-view="progress"]').click()
                expect(page.locator('.progress-metric').nth(1)).to_contain_text(str(groups.count('done')))
                assert data_path.read_bytes() == original_data and artifacts_path.read_bytes() == original_artifacts

                page.set_viewport_size({'width': 390, 'height': 844})
                page.locator('#requirement-W03 summary').click()
                expect(page.locator('#requirement-W03 .progress-key-results .progress-chart').first).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'progress-mobile.png'), full_page=False)
                mobile_w01 = page.locator('#requirement-W01')
                mobile_w01.locator('summary').first.click()
                mobile_w01.scroll_into_view_if_needed()
                expect(mobile_w01.locator('.w01-funnel')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'w01-after-mobile.png'), full_page=False)
                page.get_by_label('담당자 필터').select_option('우영')
                expect(page.locator('#progress-group-B svg')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#progress-owner-overview').screenshot(path=str(output / 'woo-overview-mobile.png'))
                assert not errors and not writes, (errors, writes)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    print(f"PASS: progress 18 items, {len(artifacts['artifacts'])} artifacts (A~D, prompt, charts = JSON, table filter, CSV), "
          'woo overview A·B·C (numbers = JSON, detail links, mobile), no-artifact notice, private missing / bad path / broken artifact / artifacts 404, filters, refresh, mobile, no model calls')


if __name__ == '__main__':
    main()
