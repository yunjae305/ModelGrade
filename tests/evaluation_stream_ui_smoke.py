"""Browser interaction checks for the event ticker. Local mock streams only."""
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright, expect
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.server import Server


def main():
    with tempfile.TemporaryDirectory(prefix='aiona-ticker-ui-') as folder:
        evaluator = CodeEvalService(save_dir=Path(folder)/'eval', jev_resolver=lambda: (None,None,'offline fixture'))
        service = ComparisonService(evaluator, save_dir=Path(folder)/'runs')
        server = Server(('127.0.0.1',0),code_eval_service=evaluator,comparison_service=service)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        errors=[]
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True)
                page=browser.new_page(viewport={'width':1280,'height':1000})
                page.on('pageerror',lambda e: errors.append(str(e)))
                page.add_init_script('Math.random = () => 0.5')
                page.goto(f'http://127.0.0.1:{server.server_port}/evaluation-demo.html')
                page.locator('#speed').select_option('fast'); page.locator('#start').click()
                expect(page.locator('.eval-counter')).to_have_text('완료')
                expect(page.locator('.eval-line')).to_contain_text('10개 답변 평가 종료')
                page.locator('.eval-ticker').click()
                expect(page.locator('.eval-log-row')).to_have_count(10)
                page.locator('.eval-log-row > summary').first.click()
                expect(page.locator('.eval-criterion').first).to_contain_text('FAIL')
                page.screenshot(path=str(ROOT/'results/ui-preview/evaluation-ticker-light-test.png'),full_page=True)
                page.locator('#dark').check()
                page.screenshot(path=str(ROOT/'results/ui-preview/evaluation-ticker-dark-test.png'),full_page=True)
                for width in (320,390,768,1440):
                    page.set_viewport_size({'width':width,'height':900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                page.set_viewport_size({'width':1280,'height':1000})
                # A controlled stream checks real counters independently of the rolling queue.
                page.evaluate('''() => {
                    const host=document.createElement('section'); document.querySelector('main').append(host);
                    window.fixture = new AionaEvalStream.EvalProgress(host); host.id='fixture'; window.seq=0;
                    window.send=e=>fixture.receive({...e,id:++seq});
                    send({type:'run_start',total:1500});
                    send({type:'criterion_start',model:'모델 A',index:0,qid:'Q001',code:'3-1',name:'계산',stage:'CODE'});
                }''')
                expect(page.locator('#fixture .eval-line').last).to_contain_text('3-1 계산 기준 평가 중')
                page.evaluate("send({type:'criterion_start',model:'모델 A',index:0,qid:'Q001',code:'J5',name:'신뢰도',stage:'LLM',together:true})")
                page.wait_for_timeout(100)
                expect(page.locator('#fixture .eval-line').last).to_contain_text('3-1 계산 기준 평가 중')
                waiting = '모델 A · Q001 · 의미 평가 응답 대기 · 여러 기준 함께 평가'
                expect(page.locator('#fixture .eval-line').last).to_have_text(waiting,timeout=2000)
                expect(page.locator('#fixture .eval-line').last).to_have_attribute('title',waiting)
                # Slow animation playback for the motion inspection, without delaying any evaluation.
                page.evaluate("document.querySelector('#fixture').getAnimations({subtree:true}).forEach(a=>a.playbackRate=.1)")
                page.screenshot(path=str(ROOT/'results/ui-preview/evaluation-ticker-motion-test.png'))
                # Backend snapshots predate together metadata; LLM and supplemental
                # LLM stages must recover the same honest waiting label.
                for stage in ('LLM', 'LLM 보완'):
                    page.evaluate('''stage => {
                        const rows=JSON.parse(JSON.stringify(fixture.state.data.rows));
                        rows['모델 A:0'].active={code:'J5',name:'신뢰도',stage};
                        send({type:'snapshot',status:'RUNNING',total:1500,rows});
                    }''',stage)
                    expect(page.locator('#fixture .eval-line')).to_have_text(waiting)
                    expect(page.locator('#fixture .eval-line')).to_have_attribute('title',waiting)
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.set_viewport_size({'width':1280,'height':1000})
                page.evaluate('''() => {
                    for(let i=0;i<1500;i++) {
                        const b={model:'모델 A',index:i,qid:'Q'+i};
                        send({...b,type:'criterion_result',key:'J1',code:'J1',name:'의도',result:'PASS',reason:'UI 대역',stage:'LLM'});
                        send({...b,type:'q_done',pass:1,fail:0,unjudged:0});
                    }
                }''')
                expect(page.locator('#fixture .eval-counter')).to_have_text('1500/1500')
                page.locator('#fixture .eval-ticker').click()
                expect(page.locator('#fixture .eval-log-row')).to_have_count(1500)
                page.locator('#fixture .eval-log-list').evaluate('(e)=>e.scrollTop=0')
                page.wait_for_timeout(60)
                page.evaluate("send({type:'q_skip',model:'모델 B',index:0,qid:'extra',reason:'UI 스크롤 검증'})")
                page.wait_for_timeout(100)
                assert page.locator('#fixture .eval-log-list').evaluate('(e)=>e.scrollTop') < 5
                page.evaluate("send({type:'run_done',total:1501,done:1501,pass:1500,fail:0,unjudged:0,skipped:1})")
                expect(page.locator('#fixture .eval-counter')).to_have_text('완료')
                expect(page.locator('#fixture .eval-line')).to_contain_text('1501개 답변 평가 종료')
                page.wait_for_timeout(450)
                expect(page.locator('#fixture .eval-line')).to_contain_text('1501개 답변 평가 종료')
                page.emulate_media(reduced_motion='reduce')
                page.evaluate("send({type:'run_start',total:1}); send({type:'criterion_start',model:'모델 A',index:0,qid:'Q001',code:'J5',name:'신뢰도',stage:'LLM',together:true})")
                expect(page.locator('#fixture .eval-line')).to_have_text(waiting)
                expect(page.locator('#fixture .eval-shimmer')).to_be_visible()
                assert page.locator('#fixture .eval-shimmer').evaluate('(e)=>getComputedStyle(e).animationName') == 'none'
                page.locator('#fixture .eval-ticker').focus()
                page.keyboard.press('Enter')
                expect(page.locator('#fixture .eval-ticker')).to_have_attribute('aria-expanded', 'false')
                assert not errors, errors
                browser.close()
            print('Ticker UI passed: truthful semantic waiting text and title, LLM/supplement snapshots, code criterion labels, fast demo, result expansion, 350ms minimum, final queue flush, 1500 rows, scroll pause, 4 widths, dark mode, reduced motion, keyboard and 10% animation inspection. No paid calls.')
        finally:
            server.shutdown(); server.server_close()


if __name__ == '__main__':
    main()
