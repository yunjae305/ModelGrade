"""Optional browser smoke test for upload-time auto collection: python tests/workspace_auto_collection_ui_smoke.py.

Requires Playwright and Chromium. Every provider (GPT·Claude·Gemini·FactChat) is an httpx MockTransport double and the
semantic judge is the fixture judge, so no paid or external calls are made. Checks: operator settings (FactChat key,
allow/price/limits) → one upload starts collection without a selection step → multi-turn order and own-history →
partial failure kept → reload and duplicate upload reuse the job → retry only failed rows → per-question comparison →
existing CODE + J1~J7 evaluation → saved result → advanced external import still reachable. Screenshots go to
results/ui-preview/auto-collection-*.png.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import httpx
from playwright.sync_api import expect, sync_playwright
from semantic_judge_fixture import Judge
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Handler, Server

CASES = [
    {"id": "S1", "question": "한국의 수도는 어디인가요? 한 문장으로 답해 주세요.", "answer": "한국의 수도는 서울입니다."},
    {"id": "S2", "question": "물의 끓는점을 섭씨로 알려 주세요.", "answer": "1기압에서 물은 섭씨 100도에서 끓습니다."},
    {"id": "C1-1", "conversation_id": "C1", "turn": 1, "question": "이 대화에서 기억할 숫자는 7입니다. 알겠다고만 답해 주세요.", "answer": "알겠습니다."},
    {"id": "C1-2", "conversation_id": "C1", "turn": 2, "question": "그 숫자에 3을 더하면 얼마인가요?", "answer": "10입니다."},
    {"id": "C1-3", "conversation_id": "C1", "turn": 3, "question": "방금 결과를 두 배로 하면요?", "answer": "20입니다."},
]
HOSTS = {"api.openai.com": "gpt", "api.anthropic.com": "claude", "generativelanguage.googleapis.com": "gemini", "api.factchat.ai": "factchat"}


class Providers:
    """제공자 대역 — 요청 본문을 기록하고, 질문마다 '{제공자} 답변 · {질문 앞부분}' 을 돌려준다. Gemini 는 S2 를 처음엔 실패."""

    def __init__(self):
        self.requests, self.fail_gemini_s2 = [], True
        self.lock = threading.Lock()

    def __call__(self, request):
        provider = HOSTS[request.url.host]
        body = json.loads(request.content)
        messages = body.get("input") or body.get("messages") or [
            {"role": "assistant" if c["role"] == "model" else "user", "content": c["parts"][0]["text"]} for c in body["contents"]]
        question = messages[-1]["content"]
        with self.lock:
            self.requests.append((provider, messages))
        time.sleep(0.25)
        if provider == "gemini" and question.startswith("물의 끓는점") and self.fail_gemini_s2:
            return httpx.Response(500, json={"error": "fixture"})
        text = f"{provider} 답변 · {question[:12]}"
        if provider == "gpt":
            raw = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                   "usage": {"input_tokens": 12, "output_tokens": 18}}
        elif provider == "claude":
            raw = {"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 12, "output_tokens": 18}}
        elif provider == "gemini":
            raw = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}],
                   "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 18}}
        else:
            raw = {"object": "chat.completion", "model": "claude-sonnet-5-20261001",
                   "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
                   "usage": {"prompt_tokens": 12, "completion_tokens": 18}}
        return httpx.Response(200, json=raw)

    def count(self, provider=None):
        with self.lock:
            return sum(1 for p, _ in self.requests if provider in (None, p))


def main():
    output = ROOT / 'results' / 'ui-preview'
    output.mkdir(parents=True, exist_ok=True)
    Handler.log_message = lambda *args: None
    providers, judge = Providers(), Judge()
    with tempfile.TemporaryDirectory(prefix='aiona-auto-ui-') as directory:
        tmp = Path(directory)
        evaluator = CodeEvalService(save_dir=tmp / 'eval', jev_resolver=lambda **_: (judge, judge.config, None))
        service = ComparisonService(evaluator, save_dir=tmp / 'runs', collection_workers=2,
                                    client_factory=lambda: httpx.Client(transport=httpx.MockTransport(providers)))
        for provider in ('gpt', 'claude', 'gemini'):   # .env 로 연결한 것과 같은 상태 — FactChat 은 화면에서 연결
            service.configure(provider, {'key': 'fixture-only', 'model': 'fixture-' + provider})
        server = Server(('127.0.0.1', 0), code_eval_service=evaluator, comparison_service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        dataset = tmp / 'aiona-qa.json'
        dataset.write_text(json.dumps(CASES, ensure_ascii=False), encoding='utf-8')
        errors, external = [], []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda err: errors.append(str(err)))

                def local_only(route):
                    if route.request.url.startswith(base):
                        route.continue_()
                    else:
                        external.append(route.request.url)
                        route.abort()
                page.route('**/*', local_only)

                def setup(route):   # 화면의 평가자 표시 — 실제 .env 대신 대역 평가자를 쓴다는 것을 적는다
                    try:
                        response = route.fetch()
                        info = response.json()
                    except Exception:
                        try:
                            route.abort()
                        except Exception:
                            pass
                        return
                    info['semantic_setup'] = {'can_evaluate': True, 'engine_label': 'Fixture Judge', 'model': 'fixture'}
                    route.fulfill(response=response, json=info)
                page.route('**/api/code-eval/info', setup)

                # 1. 관리 설정 — 연결 상태와 우수AI '연동 준비 중', FactChat 키 등록, 자동 수집 허용·단가·한도
                page.goto(base + '/workspace.html#connections')
                expect(page.locator('#connection-cards [data-target="oosoo"]')).to_contain_text('연동 준비 중')
                expect(page.locator('#connection-cards [data-target="gpt"]')).to_contain_text('자동 수집 꺼짐')
                page.locator('#connection-cards [data-target="factchat"]').get_by_role('button', name='API 키 등록').click()
                expect(page.locator('#base-url-field')).to_be_visible()
                page.locator('#api-key').fill('sk-fc-fixture-only')
                page.locator('#model-id').fill('claude-sonnet-5')
                page.locator('#save-key').click()
                expect(page.locator('#connection-cards [data-target="factchat"]')).to_contain_text('claude-sonnet-5')
                for provider in ('gpt', 'claude', 'gemini', 'factchat'):
                    page.locator('#allow-' + provider).check()
                    page.locator(f'#price-{provider}-input').fill('1')
                    page.locator(f'#price-{provider}-output').fill('2')
                page.locator('#limit-job-calls').fill('100')
                page.locator('#limit-job-cost').fill('5')
                page.locator('#limit-daily-calls').fill('1000')
                page.locator('#limit-daily-cost').fill('20')
                page.locator('#policy-max-tokens').select_option('4096')
                page.locator('#policy-save').click()
                expect(page.locator('#connection-cards [data-target="gpt"]')).to_contain_text('자동 수집')
                expect(page.locator('#policy-saved')).to_contain_text('마지막 저장')
                assert 'sk-fc-fixture-only' not in page.content()
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=str(output / 'auto-collection-settings.png'), full_page=True)
                assert providers.count() == 0

                # 2. 파일 한 개 업로드 → 선택·시작 버튼 없이 연결된 4개 AI 에 자동 요청
                page.locator('[data-view="setup"]').click()
                expect(page.locator('#collection-targets [data-target="gpt"]')).to_contain_text('자동 수집 준비')
                expect(page.locator('#collection-targets [data-target="oosoo"]')).to_contain_text('연동 준비 중')
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=str(output / 'auto-collection-empty.png'), full_page=True)
                page.locator('#dataset-file').set_input_files(str(dataset))
                expect(page.locator('#question-count')).to_contain_text('3')
                expect(page.locator('#single-count')).to_contain_text('2')
                expect(page.locator('#conversation-count')).to_contain_text('1')
                expect(page.locator('#turn-count')).to_contain_text('5')
                expect(page.locator('#answer-count')).to_contain_text('5')
                expect(page.locator('#collection-status')).to_contain_text('수집 중')
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=str(output / 'auto-collection-collecting.png'), full_page=True)
                expect(page.locator('#collection-status')).to_contain_text('일부 수집 실패', timeout=30000)
                for provider in ('gpt', 'claude', 'factchat'):
                    expect(page.locator(f'#collection-targets [data-target="{provider}"]')).to_contain_text('5 / 5턴 완료')
                expect(page.locator('#collection-targets [data-target="gemini"]')).to_contain_text('오류 1')
                expect(page.locator('#collection-targets [data-target="factchat"]')).to_contain_text('Anthropic 모델')
                assert providers.count('gemini') == 5 and providers.count() == 5 * 4   # S2 만 실패, 나머지 질문은 계속
                # 멀티턴: 턴 순서대로, 각 AI 자신의 앞 답변만 이력으로 (AIONA·다른 AI 답변 없음)
                for provider in ('gpt', 'claude', 'gemini', 'factchat'):
                    third = next(m for p, m in providers.requests if p == provider and m[-1]['content'].startswith('방금 결과'))
                    assert [m['role'] for m in third] == ['user', 'assistant', 'user', 'assistant', 'user'], third
                    assert all(m['content'].startswith(provider) for m in third if m['role'] == 'assistant'), third
                    assert 'AIONA' not in json.dumps(third, ensure_ascii=False) and '20입니다' not in json.dumps(third, ensure_ascii=False)

                # 3. 질문별 답변 비교 — 접힌 목록, 검색, 대화 펼치기, 아직 없는 답변 상태
                expect(page.locator('#compare-list .compare-item')).to_have_count(3)
                page.locator('#compare-search').fill('끓는점')
                expect(page.locator('#compare-list .compare-item')).to_have_count(1)
                page.locator('#compare-list .compare-item summary').first.click()
                expect(page.locator('#compare-list [data-provider="gemini"]')).to_contain_text('수집 실패')
                expect(page.locator('#compare-list [data-provider="gpt"]')).to_contain_text('gpt 답변 · 물의 끓는점')
                page.locator('#compare-search').fill('')
                page.locator('#compare-filter').select_option('multi')
                expect(page.locator('#compare-list .compare-item')).to_have_count(1)
                page.locator('#compare-list .compare-item summary').click()
                expect(page.locator('#compare-list .compare-turn')).to_have_count(3)
                expect(page.locator('#compare-list .compare-turn').nth(2).locator('[data-provider="aiona"]')).to_contain_text('20입니다.')
                expect(page.locator('#compare-list .compare-turn').nth(2).locator('[data-provider="claude"]')).to_contain_text('claude 답변 · 방금 결과')
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=str(output / 'auto-collection-compare.png'), full_page=True)
                page.locator('#compare-list .compare-turn').nth(1).locator('[data-provider="gpt"] .compare-detail').click()
                expect(page.locator('#answer-dialog')).to_be_visible()
                expect(page.locator('#original-answer')).to_contain_text('gpt 답변 · 그 숫자에')
                page.locator('#answer-dialog [data-close]').click()
                page.locator('#compare-filter').select_option('')

                # 4. 새로고침·같은 파일 다시 올리기 → 같은 작업, 새 요청 없음
                before = providers.count()
                run_id = service.history()[0]['run_id']
                page.reload()
                expect(page.locator('#file-name')).to_have_text('aiona-qa.json')
                expect(page.locator('#collection-status')).to_contain_text('일부 수집 실패')
                page.locator('#dataset-file').set_input_files(str(dataset))
                expect(page.locator('#collection-description')).to_contain_text('새 요청은 보내지 않았습니다')
                assert providers.count() == before and len(service.history()) == 1 and service.history()[0]['run_id'] == run_id

                # 5. 실패한 답변만 다시 수집
                providers.fail_gemini_s2 = False
                page.locator('#collection-targets [data-target="gemini"]').get_by_role('button', name='다시 수집').click()
                expect(page.locator('#collection-status')).to_contain_text('수집 완료', timeout=30000)
                expect(page.locator('#collection-targets [data-target="gemini"]')).to_contain_text('5 / 5턴 완료')
                assert providers.count() == before + 1 and providers.count('gpt') == 5
                expect(page.locator('#collection-budget')).to_contain_text('이번 작업 승인')
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=str(output / 'auto-collection-collected.png'), full_page=True)

                # 6. 기존 평가 연결 — CODE + J1~J7, 블라인드, 저장, FactChat 게이트웨이는 순위 없음
                expect(page.locator('#collect-button')).to_be_enabled()
                page.locator('#collect-button').click()
                expect(page.locator('#view-results')).to_be_visible()
                expect(page.locator('#final-summary')).to_be_visible(timeout=60000)
                expect(page.locator('#final-summary-title')).to_contain_text('실행 환경이 달라 순위를 매기지 않았습니다')
                expect(page.locator('#external-environment')).to_contain_text('응답 생성 경로')
                saved = service.result(run_id)
                assert saved['status'] == 'COMPLETED' and set(saved['evaluations']) == {'aiona', 'gpt', 'claude', 'gemini', 'factchat'}
                assert saved['blind']['enabled'] and saved['datasets']['aiona'][4]['answer'] == '20입니다.'
                sent = json.dumps([c['state'] for c in judge.calls], ensure_ascii=False)   # 답변 원문은 보내되 모델 ID·플랫폼명은 보내지 않는다
                assert judge.calls and 'fixture-gpt' not in sent and 'FactChat' not in sent and 'claude-sonnet-5' not in sent
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=str(output / 'auto-collection-results.png'), full_page=True)
                page.locator('[data-view="setup"]').click()
                expect(page.locator('#collect-button')).to_contain_text('평가 결과 보기')
                expect(page.locator('#collection-status')).to_contain_text('평가 완료')

                # 7. 기존 외부 파일 가져오기는 고급 기능으로 남는다
                expect(page.locator('#external-choose')).to_be_hidden()
                page.locator('#advanced-import > summary').click()
                expect(page.locator('#external-choose')).to_be_visible()

                mobile = browser.new_page(viewport={'width': 390, 'height': 900})
                mobile.on('pageerror', lambda err: errors.append(str(err)))
                mobile.route('**/*', local_only)
                mobile.goto(base + '/workspace.html#setup')
                mobile.evaluate(f"sessionStorage.setItem('aiona-collection-run', '{run_id}')")
                mobile.reload()
                expect(mobile.locator('#collection-status')).to_contain_text('평가 완료')
                assert mobile.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
                mobile.evaluate('window.scrollTo(0, 0)')
                mobile.screenshot(path=str(output / 'auto-collection-mobile.png'), full_page=True)
                browser.close()
            assert not errors, errors
            assert not external, external
        finally:
            server.shutdown()
            server.server_close()
    print('PASS: settings (FactChat key, allow/price/limits) → one upload → automatic collection for 4 AIs → multi-turn own history → '
          'partial failure kept → reload/duplicate upload reuse (no new requests) → retry failed only → comparison → '
          'CODE+J1~J7 evaluation saved, FactChat not ranked → external import under advanced. Mock providers and fixture judge only.')


if __name__ == '__main__':
    main()
