/* 새 비교의 두 가지 질문 준비 (사용자 결정 2026-10-14)
   - 질문 생성 후 평가: '질문 생성' 버튼으로 GPT 가 우리 세부 기준(CODE 검사·의미 평가 항목)을 모두 평가할 수 있는 질문과 첨부파일을 만든다.
   - 질문 직접 넣기: 직접 입력하거나 질문 파일을 올린다(답변 없이 질문만, 모델 호출 없음).
   어느 쪽이든 만든 질문을 확인한 뒤 '평가 시작'을 눌러야 AIONA(웹브라우저)·GPT·Claude·Gemini(API) 답변 수집과 평가를 시작한다.
   화면에는 생성 선택 사항 없음 — 질문 수·변별력 보강은 API 인자. 서버: /api/comparison/question-sets */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const W = window.AionaWorkspace;
  if (!W || !$('question-set-panel')) return;
  const {api, node} = W;
  const STATUS = {RUNNING: ['만드는 중', 'warning'], COMPLETED: ['질문 확인', 'success'], ERROR: ['실패', 'danger'], CANCELLED: ['중단', 'neutral']};
  const imported = (set) => ['manual', 'file'].includes(set?.source);   // 직접 넣은 세트
  // 패널마다 상태 — 생성(gen)과 직접 넣기(input)가 같은 확인·평가 시작 화면을 쓴다
  const GEN = {status: 'question-set-status', error: 'question-set-error', body: 'question-set-body', current: null};
  const INPUT = {status: 'question-input-status', error: 'question-input-error', body: 'question-input-body', current: null};
  let timer = null, touched = false;   // touched: 사용자가 시작한 뒤에는 늦게 온 첫 목록 응답을 쓰지 않는다
  const previews = new Map();   // 세트 ID → 확인용 질문 목록 (한 번만 받아 온다)

  function showError(message, panel = GEN) {
    const box = $(panel.error);
    box.textContent = message || '';
    box.hidden = !message;
  }

  function stat(label, value) {
    return node('div', {}, node('dt', {text: label}), node('dd', {text: String(value ?? 0)}));
  }

  // 평가 항목 구조 표 — 단계별 개수 ⇒ 검사 항목 수 · 이 질문들로 적용되는 수 (사용자 요청 2026-10-14: 한 줄로 늘어뜨리지 말고 표로)
  // CODE평가·LLM의미평가를 열로 — '대분류 N개 > 중분류 N개 > 소분류 N개'(사용자 결정 2026-10-14), 소분류 합계, 이 질문들로 적용되는 소분류
  function scopeTable(t, codeHit, codeAll, itemHit, itemAll) {
    const steps = (...counts) => counts.flatMap(([label, n], i) => [i ? ' > ' : '', label + ' ', node('b', {text: String(n)}), '개']);
    const row = (label, cells, cls) => node('tr', cls ? {class: cls} : {}, node('th', {scope: 'row', text: label}), ...cells.map((c) => node('td', {}, ...[c].flat())));
    return node('div', {class: 'table-scroll'}, node('table', {class: 'question-set-scope'},
      node('thead', {}, node('tr', {}, ...['', 'CODE평가', 'LLM의미평가', '합계'].map((text) => node('th', {scope: 'col', text})))),
      node('tbody', {},
        row('분류', [steps(['대분류', t.code_categories], ['중분류', t.code_subcategories], ['소분류', t.code_checks]),
          steps(['대분류', t.semantic_criteria], ['중분류', t.semantic_subs], ['소분류', t.semantic_items]),
          steps(['소분류', t.code_checks + t.semantic_items])], 'question-set-scope-total'),
        row('이 질문들로 적용', [`소분류 ${codeHit} / ${codeAll}`, `소분류 ${itemHit} / ${itemAll}`, `${codeHit + itemHit} / ${codeAll + itemAll}`],
          'question-set-scope-hit'))));
  }

  // 평가 항목 구조 표 + 이 세트로 적용되는 항목 + 빠진 항목 (늘 보임)
  function coverageSummary(coverage, meta, showMissing = true) {
    const lines = [];
    if (coverage) {
      const t = coverage.taxonomy;
      const later = new Set(coverage.answer_dependent || []);
      const code = Object.entries(coverage.checks || {});   // CODE 소분류 = 검사 28 (답변 구조로 생기는 검사는 답변 뒤에 정해진다)
      const items = Object.entries(coverage.items || {});
      const codeHit = code.filter(([, n]) => n > 0).length, itemHit = items.filter(([, n]) => n > 0).length;
      if (t) lines.push(scopeTable(t, codeHit, code.length, itemHit, items.length));
      else lines.push(node('p', {text: `이 질문들로 적용되는 항목 — CODE ${codeHit}/${code.length}개 · 의미 점검 항목 ${itemHit}/${items.length}개`}));
      if (later.size) lines.push(node('p', {class: 'field-help', text: `답변 구조로 생기는 검사 ${later.size}개는 답변을 받은 뒤 확인합니다.`}));
      const missing = [...(coverage.missing_checks || []), ...(coverage.missing_items || [])];
      if (showMissing && missing.length) lines.push(node('p', {class: 'question-set-missing', text: '아직 적용 질문이 없는 항목: ' + missing.join(', ')}));
    }
    if (meta) lines.push(node('p', {class: 'field-help', text: meta}));
    return node('div', {class: 'question-set-summary'}, ...lines);
  }

  // 답변을 받은 뒤 확인하는 검사·GPT 사용량·경고 — 접어 둔다
  function generationLog(set) {
    const coverage = set.coverage || {}, usage = set.usage || {}, warnings = set.warnings || [];
    const notes = [
      (coverage.answer_dependent || []).length ? '답변 구조로만 생기는 검사는 답변을 받은 뒤 확인합니다: ' + coverage.answer_dependent.join(', ') : null,
      (coverage.answer_dependent_items || []).length ? '답변 문장으로 적용이 정해지는 항목도 답변을 받은 뒤 확인합니다: ' + coverage.answer_dependent_items.join(', ') : null,
      imported(set) ? `직접 넣은 질문 · ${set.source_name || ''} · 모델 호출 없음`
        : `GPT ${set.model || ''} · 호출 ${set.calls || 0}회 · 입력 ${Number(usage.input_tokens || 0).toLocaleString('ko-KR')}토큰 · 출력 ${Number(usage.output_tokens || 0).toLocaleString('ko-KR')}토큰 · 보충 ${set.rounds || 0}회`,
    ].filter(Boolean);
    return node('details', {class: 'question-set-log'},
      node('summary', {text: (imported(set) ? '넣은 기록' : '생성 기록') + (warnings.length ? ` · 경고 ${warnings.length}건` : '')}),
      ...notes.map((text) => node('p', {text})),
      warnings.length ? node('ul', {class: 'question-set-warnings'}, warnings.slice(0, 20).map((w) => node('li', {text: w}))) : null);
  }

  // 질문 확인 — 질문마다 유형·난도·턴 원문·첨부 이름·긴 자료 글자 수·노리는 항목·함정. 평가 시작 전에 펼쳐 둔다
  function questionRow(q) {
    const turns = q.turns || [];
    return node('li', {class: 'question-set-question'},
      node('div', {class: 'question-set-question-head'},
        node('span', {class: 'question-set-qid', text: 'Q' + String(q.id ?? '').padStart(3, '0')}),
        node('strong', {text: q.topic || q.type || '질문'}),
        q.type && q.topic ? node('span', {class: 'question-set-tag', text: q.type}) : null,
        q.difficulty ? node('span', {class: 'question-set-tag', text: q.difficulty}) : null,
        turns.length > 1 ? node('span', {class: 'question-set-tag', text: `${turns.length}턴 대화`}) : null),
      node('ol', {class: 'question-set-turns'}, turns.map((t, i) => node('li', {},
        turns.length > 1 ? node('span', {class: 'question-set-turn-no', text: `T${i + 1}`}) : null,
        node('div', {class: 'question-set-turn-text'},
          node('p', {text: t.question}),
          (t.attachments || []).length ? node('p', {class: 'field-help', text: '첨부: ' + t.attachments.join(', ')}) : null,
          t.reference_chars ? node('p', {class: 'field-help', text: `긴 참조 자료 ${Number(t.reference_chars).toLocaleString('ko-KR')}자 함께 보냄`}) : null)))),
      (q.target_items || []).length ? node('p', {class: 'question-set-items', text: '노리는 항목 ' + q.target_items.join(' · ')}) : null,
      q.trap ? node('p', {class: 'question-set-trap', text: '함정 ' + q.trap}) : null);
  }

  function reviewBox(set) {
    const box = node('details', {class: 'question-set-review', open: ''},
      node('summary', {text: set.run_id ? '질문 보기' : '질문 확인 — 확인한 뒤 평가 시작을 누르세요'}),
      node('p', {class: 'field-help', role: 'status', text: '질문 목록을 불러오는 중'}));
    const fill = (rows) => box.replaceChildren(box.firstChild, node('ol', {class: 'question-set-questions'}, rows.map(questionRow)));
    if (previews.has(set.id)) fill(previews.get(set.id));
    else api(`/api/comparison/question-sets/${encodeURIComponent(set.id)}/questions`)
      .then((rows) => { previews.set(set.id, rows); fill(rows); })
      .catch((error) => box.replaceChildren(box.firstChild, node('p', {class: 'question-set-missing', text: '질문 목록을 불러오지 못했습니다: ' + error.message})));
    return box;
  }

  function render(set, panel = GEN) {
    panel.current = set;
    const [label, tone] = set?.run_id ? ['수집·평가 시작됨', 'success'] : STATUS[set?.status] || ['대기', 'neutral'];
    const pill = $(panel.status);
    pill.textContent = label;
    pill.className = 'pill ' + tone;
    const running = set?.status === 'RUNNING';
    if (panel === GEN) {
      $('question-set-start').disabled = running;
      $('question-set-cancel').hidden = !running;
    }
    showError(set?.status === 'ERROR' ? '질문 세트를 만들지 못했습니다: ' + (set.error || '알 수 없는 오류') : '', panel);
    const body = $(panel.body);
    body.replaceChildren();
    if (!set) return;
    if (running) {
      body.append(node('p', {class: 'field-help', role: 'status', text: (set.message || '준비 중') + ' — 긴 자료가 있으면 몇 분 걸립니다.'}));
      return;
    }
    if (set.status !== 'COMPLETED') return;
    const s = set.summary || {};
    const meta = imported(set) ? null : `난도 쉬움 ${s.difficulty?.['쉬움'] ?? 0} · 보통 ${s.difficulty?.['보통'] ?? 0} · 어려움 ${s.difficulty?.['어려움'] ?? 0}`
      + (set.target_questions ? ` · 목표 질문 수 ${set.target_questions}` : '') + ((set.weak_units || []).length ? ` · 변별력 보강 ${set.weak_units.length}개 항목` : '');
    body.append(...[   // null 은 글자로 붙지 않게 뺀다
      node('dl', {class: 'dataset-stats', 'aria-label': '질문 세트 요약'},
        stat('독립 질문·대화', s.questions), stat('단일 질문', s.single_questions), stat('멀티턴 대화', s.conversations),
        stat('턴', s.turns), stat('첨부파일', s.attachments), stat('긴 참조 자료', s.long_references)),
      coverageSummary(set.coverage, meta, !imported(set)),   // 직접 넣은 질문은 빠진 항목 목록을 보이지 않는다
      reviewBox(set),
      set.run_error ? node('p', {class: 'question-set-missing', text: '답변 수집을 시작하지 못했습니다: ' + set.run_error}) : null,
      node('div', {class: 'question-set-footer'}, generationLog(set),
        node('div', {class: 'question-set-actions'},
          node('a', {class: 'button secondary compact', href: `/api/comparison/question-sets/${encodeURIComponent(set.id)}/download`, download: ''}, '질문 세트 JSON 내려받기'),
          set.run_id
            ? node('button', {class: 'button primary compact', type: 'button', onclick: () => W.openRun(set.run_id)}, '답변 수집·평가 진행 보기')
            : node('button', {class: 'button primary compact', type: 'button', onclick: () => runSet(panel)}, '평가 시작')))].filter(Boolean));
  }

  async function poll() {
    clearTimeout(timer);
    if (!GEN.current || GEN.current.status !== 'RUNNING') return;
    try {
      render(await api(`/api/comparison/question-sets/${encodeURIComponent(GEN.current.id)}`));
    } catch (error) {
      showError(error.message);
    }
    if (GEN.current?.status === 'RUNNING') timer = setTimeout(poll, 2000);
  }

  async function start() {   // 질문만 만든다 — 수집·평가는 사용자가 질문을 확인한 뒤 '평가 시작'으로
    if (!window.confirm('GPT로 질문을 만듭니다(계획 1회 + 긴 자료 수십 회 + 보충). 답변 수집·평가는 만든 질문을 확인한 뒤 평가 시작을 눌러야 시작합니다. 만들까요?')) return;
    showError('');
    touched = true;
    try {
      render(await api('/api/comparison/question-sets', {auto_run: false}));
      poll();
    } catch (error) {
      showError(error.message);
    }
  }

  async function runSet(panel) {
    const set = panel.current;
    if (!set) return;
    const s = set.summary || {};
    if (!window.confirm(`질문 ${s.questions ?? '?'}개(턴 ${s.turns ?? '?'}개)로 AIONA(웹브라우저)·GPT·Claude·Gemini(API) 답변 수집과 평가 모델 호출을 시작합니다. 유료 호출이 발생합니다. 시작할까요?`)) return;
    try { render(await api(`/api/comparison/question-sets/${encodeURIComponent(set.id)}/run`, {}), panel); }
    catch (error) { showError(error.message, panel); }
  }

  async function cancel() {
    if (!GEN.current) return;
    try { render(await api(`/api/comparison/question-sets/${encodeURIComponent(GEN.current.id)}/cancel`, {})); poll(); }
    catch (error) { showError(error.message); }
  }

  // 질문 직접 넣기 — 직접 입력('---' 줄로 여러 질문) 또는 질문 파일(JSON·JSONL·TXT·MD)
  function inputMode(file) {
    $('question-input-mode-text').setAttribute('aria-pressed', String(!file));
    $('question-input-mode-file').setAttribute('aria-pressed', String(file));
    $('question-input-text-panel').hidden = file;
    $('question-input-file-panel').hidden = !file;
  }

  async function submitQuestions() {
    const file = $('question-input-mode-file').getAttribute('aria-pressed') === 'true';
    showError('', INPUT);
    let payload;
    if (file) {
      const chosen = $('question-input-file').files[0];
      if (!chosen) return showError('질문 파일을 골라 주세요.', INPUT);
      if (chosen.size > 60 * 1024 * 1024) return showError('질문 파일은 60MB 이하여야 합니다.', INPUT);
      payload = {text: await chosen.text(), name: chosen.name, source: 'file'};
    } else {
      const text = $('question-input-text').value;
      if (!text.trim()) return showError('질문을 입력해 주세요.', INPUT);
      payload = {text, name: '직접 입력', source: 'manual'};
    }
    touched = true;
    const button = $('question-input-submit');
    button.disabled = true;
    try { render(await api('/api/comparison/question-sets/import', payload), INPUT); }
    catch (error) { showError(error.message, INPUT); }
    finally { button.disabled = false; }
  }

  $('question-set-start').addEventListener('click', start);
  $('question-set-cancel').addEventListener('click', cancel);
  if ($('question-input-panel')) {
    $('question-input-mode-text').addEventListener('click', () => inputMode(false));
    $('question-input-mode-file').addEventListener('click', () => inputMode(true));
    $('question-input-file').addEventListener('change', () => {
      const chosen = $('question-input-file').files[0];
      $('question-input-file-name').textContent = chosen ? chosen.name : 'JSON·JSONL·TXT·MD 파일';
    });
    $('question-input-submit').addEventListener('click', submitQuestions);
  }
  api('/api/comparison/question-sets').then((sets) => {
    if (touched) return;
    render(sets.find((s) => !imported(s)) || null);
    if ($('question-input-panel')) render(sets.find(imported) || null, INPUT);
    poll();
  }).catch(() => { if (!touched) render(null); });
})();
