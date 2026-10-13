"""웹 화면에서 시작하는 items 평가 흐름 — 실제 서버·작업 공간 화면에 Mock items Judge 와 Mock 제공자만 끼운다 (유료 호출·외부 네트워크 0).

    python tests/workspace_items_ui_smoke.py      # 스크린샷: results/ui-preview/items-*.png

SEMANTIC_PIPELINE=items 는 이 프로세스의 환경변수로만 켠다 (.env·코드 기본값 legacy 는 그대로).
파일 업로드 → 자동 수집(Mock 제공자) → 평가 시작 → CODE → Mock items 의미 평가 → 집계·저장 → 결과 화면 →
질문별 항목 근거 → 새로고침 복원 → 이력의 평가 조건. 의미 판정 값은 tools/items_e2e_demo 의 Mock 대본이며 Claude 판정이 아니다.
"""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ['SEMANTIC_PIPELINE'] = 'items'   # 이 프로세스에서만 — .env 를 바꾸지 않는다

import httpx  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402
from code_eval.jev_client import resolve_semantic_pipeline  # noqa: E402
from test_blind_comparison import adapter_fixture  # noqa: E402
from tools.items_e2e_demo import PAGES, SCENARIOS, MockJudge, _no_network  # noqa: E402
from webapp.code_eval_service import CodeEvalService  # noqa: E402
from webapp.comparison_service import ComparisonService  # noqa: E402
from webapp.server import Handler, Server  # noqa: E402

LOOPBACK = {'127.0.0.1', '::1', 'localhost'}


class AionaScriptJudge(MockJudge):
    """AIONA 답변에만 시나리오 대본을 쓰고, Mock 제공자 답변에는 기본 응답(인용 붙은 YES·해당 없음)만 준다."""

    def __init__(self, scripts, pages, aiona_answers):
        super().__init__(scripts, pages)
        self.aiona_answers = aiona_answers

    def script_for(self, state):
        script = self.scripts.get(state.get('question'), {})
        if self.aiona_answers.get(state.get('question')) == state.get('answer'):
            return script
        return {'apply_model_items': script.get('apply_model_items', False)}


def guard_sockets():
    """루프백 밖으로 나가는 소켓 연결을 막고 시도를 기록한다 (Mock 제공자는 httpx MockTransport 라 소켓을 열지 않는다)."""
    blocked, original = [], socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in LOOPBACK:
            blocked.append(host)
            raise OSError(f'workspace_items_ui_smoke: 외부 연결 차단 {host}')
        return original(self, address)
    socket.socket.connect = connect
    return blocked


def main():
    assert resolve_semantic_pipeline() == 'items'
    blocked = guard_sockets()
    output = ROOT / 'results/ui-preview'
    output.mkdir(parents=True, exist_ok=True)
    Handler.log_message = lambda *args: None
    cases = [case for s in SCENARIOS for case in s['cases']]
    index = {case['case_id']: i for i, case in enumerate(cases)}
    judge = AionaScriptJudge({c['question']: sc for s in SCENARIOS for c, sc in zip(s['cases'], s['scripts'])}, PAGES,
                             {c['question']: c['answer'] for c in cases})
    with tempfile.TemporaryDirectory(prefix='aiona-items-ui-') as directory, _no_network():
        tmp = Path(directory)
        registry = tmp / 'versions.json'
        registry.write_text(json.dumps({'versions': ['v1'], 'current_release': {'version': 'v1'}}), encoding='utf-8')
        handler, sent, _, _ = adapter_fixture()
        evaluator = CodeEvalService(save_dir=tmp / 'eval', jev_resolver=lambda **_: (judge, judge.config, None))
        service = ComparisonService(evaluator, save_dir=tmp / 'runs', registry_path=registry,
                                    client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
        for provider in ('gpt', 'claude', 'gemini'):
            service.configure(provider, {'key': 'fixture-only', 'model': 'fixture-' + provider})
        # 운영자가 미리 정한 자동 수집 허용·한도 — 업로드하면 세 Mock 제공자에 자동으로 요청한다
        service.update_auto_policy({'allowed': {p: True for p in ('gpt', 'claude', 'gemini')},
                                    'prices': {p: {'input': 1, 'output': 2} for p in ('gpt', 'claude', 'gemini')},
                                    'limits': {'job_calls': 100, 'job_cost_usd': 5, 'daily_calls': 1000, 'daily_cost_usd': 20}})
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors, external = [], []
                page.on('pageerror', lambda err: errors.append(str(err)))

                def local_only(route):
                    if route.request.url.startswith(base):
                        route.continue_()
                    else:
                        external.append(route.request.url)
                        route.abort()
                page.route('**/*', local_only)

                def setup(route):   # 평가자 연결 표시 — 실제 평가자 설정(.env) 대신 Mock 을 쓴다는 것을 화면에도 적는다
                    try:
                        response = route.fetch()
                        info = response.json()
                    except Exception:   # 새로고침으로 끊긴 요청 — 화면 표시용 덮어쓰기라 건너뛴다
                        try:
                            route.abort()
                        except Exception:
                            pass
                        return
                    info['semantic_setup'] = {'can_evaluate': True, 'engine_label': 'Mock items Judge', 'model': 'mock-judge'}
                    route.fulfill(response=response, json=info)
                page.route('**/api/code-eval/info', setup)

                page.goto(base + '/workspace.html#setup')
                expect(page.locator('#setup-current-version')).to_have_text('AIONA v1')
                page.locator('#dataset-file').set_input_files({'name': 'items-scenarios.json', 'mimeType': 'application/json',
                                                               'buffer': json.dumps(cases, ensure_ascii=False).encode('utf-8')})
                expect(page.locator('#file-name')).to_have_text('items-scenarios.json')
                expect(page.locator('#collection-status')).to_contain_text('수집 완료', timeout=30000)   # 업로드만으로 자동 수집
                expect(page.locator('#collect-button')).to_be_enabled()
                page.locator('#collect-button').click()
                expect(page.locator('#final-summary')).to_be_visible(timeout=60000)
                expect(page.locator('#result-subtitle')).to_contain_text('질문·대화 8개 · 평가 대상 턴 10개')   # 대화 1개 = 1
                expect(page.locator('#result-subtitle')).to_contain_text('평가 방식: 항목별 의미 평가 (items)')
                page.locator('#view-results .page-heading').screenshot(path=str(output / 'items-result-method-label.png'))

                run = service.history()[0]
                saved = service.result(run['run_id'])
                assert saved['status'] == 'COMPLETED', saved.get('error')
                assert saved['evaluation_execution']['pipeline'] == 'items'
                aiona = saved['evaluations']['aiona']
                assert aiona['jev_eval']['pipeline'] == 'items' and all(
                    (r or {}).get('pipeline') == 'items' for i, r in enumerate(aiona['jev_eval']['records']) if i != index['F'])
                records = aiona['jev_eval']['records']
                assert records[index['F']]['status'] == 'TIMEOUT' and records[index['F']]['decisions'] is None
                assert records[index['C']]['decisions']['J4']['decision'] == 'FAIL'
                assert records[index['D-T3']]['decisions']['J6']['decision'] == 'FAIL'
                assert records[index['B']]['status'] == 'SUCCESS' and any(
                    r['verdict'] == 'FAIL' for r in aiona['cases'][index['B']]['results'] if r.get('area') == 'common')
                assert all(len(sent[p]) == len(cases) for p in ('gpt', 'claude', 'gemini'))   # Mock 제공자에 질문마다 한 번
                assert all('aiona' not in json.dumps(c['state'], ensure_ascii=False).lower() for c in judge.calls)   # 블라인드
                page.screenshot(path=str(output / 'items-results-desktop.png'), full_page=True)

                def open_case(case_id):
                    page.keyboard.press('Escape')
                    page.locator(f'[data-focus-key="result-aiona-{index[case_id]}"]').click()
                    expect(page.locator('#answer-dialog')).to_be_visible()
                    evaluation = page.locator('#answer-evaluation')
                    expect(evaluation).to_be_visible()
                    items = evaluation.locator('details.semantic-items-evidence')
                    if items.count():
                        items.locator('summary').click()
                    return evaluation

                ev = open_case('A')
                expect(ev).to_contain_text('세부 기준·점검 항목 판정')
                expect(ev).to_contain_text('J1-3.a · 해당 없음')
                ev = open_case('B')
                expect(ev.locator('.answer-checks')).to_contain_text('코드 검사')
                expect(ev).to_contain_text('세부 기준·점검 항목 판정')   # CODE FAIL 이어도 의미 평가 항목이 있다
                ev = open_case('C')
                expect(ev).to_contain_text('J4-2.a#1 · 아니오')
                expect(ev).to_contain_text('질문 인용: 각각 추천 이유도 붙여줘')
                expect(ev).to_contain_text('답변 인용: - 인사이드 아웃 2')
                expect(ev).to_contain_text('추천 이유 요구가 답변에 없음')
                page.locator('#answer-dialog').screenshot(path=str(output / 'items-case-c-j4.png'))
                ev = open_case('C2')
                expect(ev).to_contain_text('확인 불가(재확인 뒤 FAIL)')
                ev = open_case('D-T3')
                expect(ev).to_contain_text('J6-1.a#1 · 아니오')
                expect(ev).to_contain_text('질문 인용: 앞으로 존댓말로만 답해 주세요 (이전 1턴)')
                expect(ev).to_contain_text('질문 인용: 숙소 예산을 1박 15만 원으로 줄여서 (이전 2턴)')
                page.locator('#answer-dialog').screenshot(path=str(output / 'items-case-d-j6.png'))
                ev = open_case('E')
                expect(ev).to_contain_text('J5-1.a#1 · 아니오')
                ev = open_case('F')
                expect(ev).to_contain_text('평가 오류 원문 보기')
                expect(ev).not_to_contain_text('세부 기준·점검 항목 판정')   # 평가기 오류는 항목 판정이 없다 (해당 없음과 구분)
                page.locator('#answer-dialog').screenshot(path=str(output / 'items-case-f-error.png'))
                page.keyboard.press('Escape')

                os.environ['SEMANTIC_PIPELINE'] = 'legacy'   # 현재 설정을 바꿔도 저장된 실행의 방식이 그대로 보여야 한다
                page.reload()   # 새로고침 — sessionStorage 의 실행 ID 로 저장된 결과를 다시 연다
                expect(page.locator('#final-summary')).to_be_visible(timeout=15000)
                expect(page.locator('#result-subtitle')).to_contain_text('질문·대화 8개 · 평가 대상 턴 10개')
                expect(page.locator('#result-subtitle')).to_contain_text('평가 방식: 항목별 의미 평가 (items)')
                ev = open_case('C')
                expect(ev).to_contain_text('J4-2.a#1 · 아니오')
                page.keyboard.press('Escape')
                page.locator('[data-view="history"]').click()
                expect(page.locator('#history-body')).to_contain_text('항목별 평가')
                page.screenshot(path=str(output / 'items-history.png'), full_page=True)
                assert not errors, errors
                assert not external, external
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    assert not blocked, blocked
    reasons = [max([((q.get('recheck') or {}).get('reason') or ('recheck' if (q.get('recheck') or {}).get('round') else 'base'))
                    for q in c['questions'].values()] or ['base']) for c in judge.calls]
    kinds = {'base': reasons.count('base'), 'union': reasons.count('missing_elements')}
    kinds['recheck'] = len(reasons) - kinds['base'] - kinds['union']
    print(f'Mock Judge 호출 구성: 본 평가 {kinds["base"]} · 인용 재확인 {kinds["recheck"]} · 모델 간 요소 합집합 재확인 {kinds["union"]}')
    print(f'PASS: 웹 업로드 → 4개 모델 비교(Mock 제공자) → CODE → Mock items 의미 평가(pipeline=items) → 저장 → 결과 화면 항목 근거 → '
          f'새로고침 복원 → 이력 조건 표시. Mock Judge 호출 {len(judge.calls)}회, 외부 연결 0회.')


if __name__ == '__main__':
    main()
