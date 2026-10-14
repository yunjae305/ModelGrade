"""3분 시연 경로 — 비공개 파일 없이 처음부터 끝까지 (10-14 4차). 유료 호출·외부 네트워크 0.

    python tests/workspace_demo_path_ui_smoke.py      # 스크린샷: results/ui-preview/demo-path-*.png

tools/demo_run 으로 샘플·모의 실행을 임시 폴더에 만들고(실제 수집·평가 경로, Mock 제공자·Mock Judge), 그 폴더만 쓰는 서버에서:
1 서버·화면 연결 → 2 AIONA 결과 이력에서 저장 실행 열기 → 3 모델별 비교(4개) → 4 기준별 PASS·FAIL·검증 한계 → 5 질문 상세의 적용 기준과
판정 사유 → 6 입력 구성 주의 → 7 이력·기록 상세 → 8 새로고침 뒤 다시 열어 같은 값 → 9 화면 오류·외부 요청 0.
"""
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from playwright.sync_api import expect, sync_playwright  # noqa: E402
from tools.demo_run import RUN_NAME, build  # noqa: E402
from webapp.code_eval_service import CodeEvalService  # noqa: E402
from webapp.comparison_service import ComparisonService  # noqa: E402
from webapp.server import Handler, Server  # noqa: E402


def open_from_history(page):
    page.locator('[data-view="history"]').first.click()
    expect(page.locator('#history-body')).to_contain_text('상세 보기', timeout=15000)
    page.locator('#history-body').get_by_role('button', name='상세 보기').first.click()
    expect(page.locator('#history-detail-dialog')).to_be_visible()
    page.get_by_role('button', name='저장된 평가 결과 열기').click()
    expect(page.locator('#result-subtitle')).to_contain_text('평가 방식', timeout=20000)
    page.wait_for_timeout(800)


def snapshot(page):
    return {'subtitle': page.locator('#result-subtitle').inner_text(),
            'criteria': page.locator('.criterion-counts').all_inner_texts(),
            'cards': page.locator('.result-model-card').count()}


def main():
    output = ROOT / 'results/ui-preview'
    output.mkdir(parents=True, exist_ok=True)
    Handler.log_message = lambda *args: None
    with tempfile.TemporaryDirectory(prefix='modelgrade-demo-path-') as directory:
        folder = Path(directory)
        built = build(folder)
        evaluator = CodeEvalService(save_dir=folder / 'eval', jev_resolver=lambda **_: (None, None, '시연용 샘플 서버'))
        service = ComparisonService(evaluator, save_dir=folder / 'runs', registry_path=folder / 'versions.json')
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
                page.goto(base + '/workspace.html')
                expect(page.locator('#server-status')).to_contain_text('연결됨')                      # 1
                open_from_history(page)                                                              # 2
                first = snapshot(page)
                assert first['cards'] == 4, first                                                    # 3
                assert '평가 방식' in first['subtitle'] and 'items' in first['subtitle'], first['subtitle']
                assert any('검증 한계 1' in text for text in first['criteria']), first['criteria']      # 4
                expect(page.locator('[data-j5-breakdown]').first).to_contain_text('사실 오류 확정 1')
                verification = page.locator('#criteria-verification')                               # P1-7 기준별 검증 현황
                expect(verification).to_be_visible()
                expect(verification.locator('#verification-catalog')).to_contain_text('기준 8 · 세부 30 · 점검 항목 44')
                expect(verification.locator('[data-verification="J5"]')).to_contain_text('미측정')       # 사람 검수 0 → 0% 아님
                expect(verification.locator('[data-verification="J8"]')).to_contain_text('미측정')
                composition = page.locator('#input-composition')                                     # 6
                expect(composition).to_be_visible()
                expect(composition).to_contain_text('모델마다 입력 구성이 달랐습니다')
                page.screenshot(path=str(output / 'demo-path-results.png'), full_page=True)
                for case, search, needles in (('E', '카페인', ('J5 FAIL', '사실 오류')), ('G', '그림 속 자동차', ('J8 PASS', '첨부 그림 1개를 평가 모델에도 보냈습니다: demo-car.png (image/png'))):   # 5
                    page.locator('#matrix-search').fill(search)   # 표는 10행씩 — 질문 글자로 찾는다
                    cell = page.locator(f'[aria-label^="{case} AIONA"]').first
                    cell.scroll_into_view_if_needed()
                    cell.click()
                    dialog = page.locator('#answer-dialog')
                    expect(dialog).to_be_visible(timeout=10000)
                    page.wait_for_timeout(500)
                    text = dialog.inner_text()
                    assert all(needle in text for needle in needles), (case, text[:600])
                    table = page.locator('#answer-collection [data-collection-comparison]')        # P1-6 같은 질문의 수집 방식 비교 (평가 칸 밖)
                    expect(page.locator('#answer-evaluation [data-collection-comparison]')).to_have_count(0)
                    expect(table).to_contain_text('수집 방식 비교 — 같은 질문, 시스템 4개')
                    expect(table).to_contain_text('샘플 데이터')
                    table.locator('summary').click()
                    expect(table.locator('tbody tr')).to_have_count(4)
                    expect(table).to_contain_text('API 실시간 수집')
                    expect(table).to_contain_text('업로드 원문')
                    page.screenshot(path=str(output / f'demo-path-question-{case}.png'))
                    page.keyboard.press('Escape')
                page.locator('[data-view="history"]').first.click()                                 # 7
                expect(page.locator('#view-history')).to_contain_text('AIONA 결과 이력')
                expect(page.locator('#history-body')).to_contain_text('상세 보기')
                page.reload()                                                                         # 8
                expect(page.locator('#server-status')).to_contain_text('연결됨')
                open_from_history(page)
                again = snapshot(page)
                assert again == first, (first, again)
                assert errors == [] and external == [], (errors, external)                            # 9
                browser.close()
        finally:
            server.shutdown()
            service.shutdown()
    print(f"PASS: demo path on sample run {built['run_id']} ({RUN_NAME}) — history → results → 4 models → criteria with "
          f"verification-limited FAIL → question detail criteria and reasons → input composition → history → reload same values; "
          f"page errors 0, external requests 0, judge calls {built['judge_calls']} (mock), images sent to judge {built['images_seen']}.")


if __name__ == '__main__':
    main()
