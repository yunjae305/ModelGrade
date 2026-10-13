/* 질문 세트 만들기 — 버튼 하나로 GPT 가 우리 세부 기준(CODE 검사·의미 평가 항목)을 모두 평가할 수 있는 질문과 첨부파일을 만든다
   (사용자 결정 2026-10-13). 서버: /api/comparison/question-sets */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const W = window.AionaWorkspace;
  if (!W || !$('question-set-panel')) return;
  const {api, node} = W;
  const STATUS = {RUNNING: ['만드는 중', 'warning'], COMPLETED: ['완료', 'success'], ERROR: ['실패', 'danger'], CANCELLED: ['중단', 'neutral']};
  let current = null, timer = null, touched = false;   // touched: 사용자가 시작·수집을 누른 뒤에는 늦게 온 첫 목록 응답을 쓰지 않는다

  function showError(message) {
    const box = $('question-set-error');
    box.textContent = message || '';
    box.hidden = !message;
  }

  function stat(label, value) {
    return node('div', {}, node('dt', {text: label}), node('dd', {text: String(value ?? 0)}));
  }

  function coverageBlock(coverage) {
    if (!coverage) return null;
    const checks = Object.entries(coverage.checks || {}), items = Object.entries(coverage.items || {});
    const counted = checks.filter(([id]) => !(coverage.answer_dependent || []).includes(id));
    const missing = [...(coverage.missing_checks || []), ...(coverage.missing_items || [])];
    return node('div', {class: 'question-set-coverage'},
      node('p', {text: `CODE 검사 ${counted.filter(([, n]) => n > 0).length}/${counted.length}개 · 의미 평가 항목 ${items.filter(([, n]) => n > 0).length}/${items.length}개에 적용 질문이 있습니다.`}),
      missing.length ? node('p', {class: 'field-help', text: '아직 적용 질문이 없는 항목: ' + missing.join(', ')}) : node('p', {class: 'field-help', text: '모든 검사·항목에 적용 질문이 있습니다.'}),
      (coverage.answer_dependent || []).length
        ? node('p', {class: 'field-help', text: '답변 구조로만 생기는 검사는 답변을 받은 뒤 확인합니다: ' + coverage.answer_dependent.join(', ')})
        : null,
      (coverage.answer_dependent_items || []).length
        ? node('p', {class: 'field-help', text: '답변 문장으로 적용이 정해지는 항목도 답변을 받은 뒤 확인합니다: ' + coverage.answer_dependent_items.join(', ')})
        : null);
  }

  function render(set) {
    current = set;
    const [label, tone] = STATUS[set?.status] || ['대기', 'neutral'];
    const pill = $('question-set-status');
    pill.textContent = label;
    pill.className = 'pill ' + tone;
    const running = set?.status === 'RUNNING';
    $('question-set-start').disabled = running;
    $('question-set-cancel').hidden = !running;
    showError(set?.status === 'ERROR' ? '질문 세트를 만들지 못했습니다: ' + (set.error || '알 수 없는 오류') : '');
    const body = $('question-set-body');
    body.replaceChildren();
    if (!set) return;
    if (running) {
      body.append(node('p', {class: 'field-help', role: 'status', text: (set.message || '준비 중') + ' — 긴 자료가 있으면 몇 분 걸립니다.'}));
      return;
    }
    if (set.status !== 'COMPLETED') return;
    const s = set.summary || {};
    const usage = set.usage || {};
    body.append(...[   // null 은 글자로 붙지 않게 뺀다
      node('dl', {class: 'dataset-stats', 'aria-label': '만든 질문 세트 요약'},
        stat('독립 질문·대화', s.questions), stat('단일 질문', s.single_questions), stat('멀티턴 대화', s.conversations),
        stat('턴', s.turns), stat('첨부파일', s.attachments), stat('긴 참조 자료', s.long_references)),
      node('p', {class: 'field-help', text: `목표 질문 수 ${set.target_questions ?? '자동(최소)'} · 난도 쉬움 ${s.difficulty?.['쉬움'] ?? 0} · 보통 ${s.difficulty?.['보통'] ?? 0} · 어려움 ${s.difficulty?.['어려움'] ?? 0}`
        + ((set.weak_units || []).length ? ` · 변별력 보강 ${set.weak_units.length}개 항목 (${set.weak_from_run})` : '')}),
      coverageBlock(set.coverage),
      node('p', {class: 'field-help', text: `GPT ${set.model || ''} · 호출 ${set.calls || 0}회 · 입력 ${Number(usage.input_tokens || 0).toLocaleString('ko-KR')}토큰 · 출력 ${Number(usage.output_tokens || 0).toLocaleString('ko-KR')}토큰 · 보충 ${set.rounds || 0}회`}),
      (set.warnings || []).length ? node('details', {class: 'question-set-warnings'}, node('summary', {text: `경고 ${set.warnings.length}건`}),
        node('ul', {}, set.warnings.slice(0, 20).map((w) => node('li', {text: w})))) : null,
      node('a', {class: 'button secondary compact', href: `/api/comparison/question-sets/${encodeURIComponent(set.id)}/download`, download: ''}, '질문 세트 JSON 내려받기'),
      set.run_id
        ? node('button', {class: 'button primary compact', type: 'button', onclick: () => W.openRun(set.run_id)}, '답변 수집·평가 진행 보기')
        : node('button', {class: 'button primary compact', type: 'button', onclick: runSet}, '이 질문 세트로 답변 수집·평가'),
      set.run_error ? node('p', {class: 'field-help', text: '답변 수집을 시작하지 못했습니다: ' + set.run_error}) : null].filter(Boolean));
  }

  async function poll() {
    clearTimeout(timer);
    if (!current || current.status !== 'RUNNING') return;
    try {
      render(await api(`/api/comparison/question-sets/${encodeURIComponent(current.id)}`));
    } catch (error) {
      showError(error.message);
    }
    if (current?.status === 'RUNNING') timer = setTimeout(poll, 2000);
  }

  async function start() {
    const chained = $('question-set-auto-run').checked;
    if (!window.confirm('GPT 호출이 발생합니다(질문 계획 1회 + 긴 자료 수십 회 + 보충 최대 2회)'
      + (chained ? '. 다 만든 뒤 AIONA(웹)·GPT·Claude·Gemini(API) 답변 수집과 평가까지 이어서 진행합니다' : '') + '. 시작할까요?')) return;
    showError('');
    touched = true;
    const count = $('question-set-count').value.trim();
    try {
      render(await api('/api/comparison/question-sets', {note: $('question-set-note').value, auto_run: $('question-set-auto-run').checked,
        target_questions: count ? Number(count) : null, weak_from_run: $('question-set-weak-run').value || null}));
      poll();
    } catch (error) {
      showError(error.message);
    }
  }

  async function runSet() {
    if (!current) return;
    if (!window.confirm('AIONA(웹)·GPT·Claude·Gemini(API) 답변 수집과 평가 모델 호출이 발생합니다. 시작할까요?')) return;
    try { render(await api(`/api/comparison/question-sets/${encodeURIComponent(current.id)}/run`, {})); }
    catch (error) { showError(error.message); }
  }

  async function cancel() {
    if (!current) return;
    try { render(await api(`/api/comparison/question-sets/${encodeURIComponent(current.id)}/cancel`, {})); poll(); }
    catch (error) { showError(error.message); }
  }

  async function loadWeakRuns() {   // 변별력 보강에 쓸 지난 질문 세트 실행(평가까지 끝난 것)
    try {
      const runs = (await api('/api/comparison/history')).filter((r) => r.run_kind === 'question_set' && r.status === 'COMPLETED');
      const select = $('question-set-weak-run');
      runs.forEach((r) => select.append(node('option', {value: r.run_id, text: `${r.name || r.run_id} · 질문 ${r.question_count ?? '?'}개 · ${(r.created_at || '').slice(0, 16).replace('T', ' ')}`})));
    } catch (error) { /* 목록이 없어도 생성은 할 수 있다 */ }
  }

  $('question-set-start').addEventListener('click', start);
  $('question-set-cancel').addEventListener('click', cancel);
  loadWeakRuns();
  api('/api/comparison/question-sets').then((sets) => { if (!touched) { render(sets[0] || null); poll(); } }).catch(() => { if (!touched) render(null); });
})();
