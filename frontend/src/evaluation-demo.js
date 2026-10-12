(() => {
  'use strict';
  const progress = new AionaEvalStream.EvalProgress(document.getElementById('demo-progress'));
  const criteria = [['1-1', '응답·문자 정상성'], ['1-2', '문서·코드 유효성'], ['2-1', '분량·표현 조건'], ['3-1', '계산 결과 일치'], ['J1', '의도 충족성']];
  let session = 0;
  document.getElementById('dark').addEventListener('change', (e) => { document.body.dataset.theme = e.target.checked ? 'dark' : 'light'; });
  document.getElementById('start').addEventListener('click', async () => {
    const current = ++session, slow = document.getElementById('speed').value === 'slow';
    progress.reset(); let id = 0;
    const send = (event) => progress.receive({...event, id: ++id, generation: String(current)});
    const pause = () => slow ? new Promise((resolve) => setTimeout(resolve, 430)) : Promise.resolve();
    const totals = {pass: 0, fail: 0, unjudged: 0, skipped: 0};
    send({type: 'run_start', total: 10});
    for (let index = 0; index < 10; index++) {
      if (current !== session) return;
      const base = {model: '모델 A', index, qid: 'Q' + String(index + 1).padStart(3, '0')};
      const n = index === 2 ? 0 : Math.floor(Math.random() * 6);
      send({...base, type: 'q_start'});
      const values = {pass: 0, fail: 0, unjudged: 0};
      for (let c = 0; c < n; c++) {
        const [code, name] = criteria[c];
        send({...base, type: 'criterion_start', code, name, stage: 'DEMO'});
        await pause(); if (current !== session) return;
        const result = ['PASS', 'FAIL', 'UNJUDGED'][Math.floor(Math.random() * 3)];
        values[result.toLowerCase()]++; totals[result.toLowerCase()]++;
        send({...base, type: 'criterion_result', key: code, code, name, result, reason: '데모 무작위 결과 · 실제 품질 판정 아님', stage: 'DEMO'});
      }
      if (!n) totals.skipped++;
      send({...base, type: n ? 'q_done' : 'q_skip', ...values, reason: n ? '' : '해당 기준 없음 (데모)'});
      await pause();
    }
    if (current === session) send({type: 'run_done', total: 10, done: 10, ...totals});
  });
})();
