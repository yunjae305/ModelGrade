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
                expect(w01.locator('.progress-section-letter')).to_have_text(['A', 'B', 'C', 'D', '→'])
                expect(w01.locator('.progress-artifact')).to_have_count(len(by_requirement['W01']))
                expect(w01).to_contain_text(data['items'][0]['acceptance'])
                expect(w01).to_contain_text(data['items'][0]['meeting']['request'])
                expect(page.locator('#artifact-W01-INTENT .progress-result')).to_have_text(by_id['W01-INTENT']['result_summary'])
                prompt = by_id['W01-PROMPT']['content']['prompts'][0]
                page.locator('#artifact-W01-PROMPT .progress-prompt summary').first.click()
                expect(page.locator('#artifact-W01-PROMPT .progress-prompt pre').first).to_contain_text(prompt['text'][:60])
                expect(page.locator('#artifact-W01-SET300 .progress-file').first).to_have_text('이 PC 에 있음')
                expect(page.locator('#artifact-W01-SET300 .progress-sample')).to_have_count(len(by_id['W01-SET300']['content']['samples']))

                # W02 — 그래프 수치·표 필터·CSV 가 artifacts.json 과 같다
                page.locator('#requirement-W02 summary').click()
                criteria = by_id['W02-CRITERIA']['content']['charts'][0]
                chart = page.locator('#artifact-W02-CRITERIA .progress-chart')
                values = [v for v in chart.locator('svg text.value').all_text_contents()]
                expected = [f'{v:,}' for row in range(len(criteria['categories'])) for v in (s['values'][row] for s in criteria['series'])]
                assert values == expected, (values, expected)
                chart.locator('.progress-chart-data summary').click()
                first_row = chart.locator('.progress-chart-data tbody tr').first.locator('td').all_text_contents()
                assert first_row == [criteria['categories'][0]] + [f"{s['values'][0]:,}" for s in criteria['series']], first_row
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
                line = by_id['W03-COVERAGE']['content']['charts'][0]
                last = line['series'][-1]
                expect(page.locator('#artifact-W03-COVERAGE svg')).to_contain_text(f"{last['name']} {last['values'][-1]}")
                expect(page.locator('#requirement-W03 .progress-warning')).to_contain_text('같은 조건')
                expect(page.locator('#artifact-W03-CONCLUSION')).to_contain_text(by_id['W03-CONCLUSION']['content']['judgment'][:30])

                # W06 — 산출물 없음 안내, 근거 없는 칸은 미확인
                page.locator('#requirement-W06 summary').click()
                expect(page.locator('#requirement-W06 .progress-no-artifact')).to_contain_text('실제 산출물 없음')
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
                expect(page.locator('#artifact-W03-STABILITY .progress-file').first).to_contain_text('자료 없음')
                expect(page.locator('#artifact-W03-STABILITY')).to_contain_text(by_id['W03-STABILITY']['result_summary'][:20])
                page.locator('#requirement-W06 summary').click()
                expect(page.locator('#artifact-W06-BAD-PATH .progress-file')).to_have_text('잘못된 경로')
                page.unroute('**/api/progress/files*')
                page.unroute('**/data/artifacts.json')

                # 산출물 파일을 못 읽으면 요구사항은 그대로, 산출물 칸에 사유
                page.route('**/data/artifacts.json', lambda route: route.fulfill(status=404, body='{}'))
                page.get_by_role('button', name='새로고침').click()
                expect(page.locator('.progress-item')).to_have_count(18)
                expect(page.locator('.progress-note')).to_contain_text('산출물 자료를 불러오지 못했습니다')
                page.locator('#requirement-W01 summary').click()
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
                expect(page.locator('#artifact-W03-COVERAGE .progress-chart')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'progress-mobile.png'), full_page=False)
                assert not errors and not writes, (errors, writes)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    print(f"PASS: progress 18 items, {len(artifacts['artifacts'])} artifacts (A~D, prompt, charts = JSON, table filter, CSV), "
          'no-artifact notice, private missing / bad path / broken artifact / artifacts 404, filters, refresh, mobile, no model calls')


if __name__ == '__main__':
    main()
