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
  const fixed = (set) => set?.source === 'fixed';   // 고정 질문 세트(간단·정밀)
  const twoStep = (set) => imported(set) || fixed(set);   // '답변 수집' → '평가 진행' 두 단계로 진행하는 세트
  // 패널마다 상태 — 생성(gen)과 직접 넣기(input)가 같은 확인·평가 시작 화면을 쓴다
  const GEN = {status: 'question-set-status', error: 'question-set-error', body: 'question-set-body', current: null, runBox: 'question-set-run'};
  const INPUT = {status: 'question-input-status', error: 'question-input-error', body: 'question-input-body', current: null, runBox: 'question-input-run'};
  let timer = null;
  const previews = new Map();   // 세트 ID → 확인용 질문 목록 (한 번만 받아 온다)

  function showError(message, panel = GEN) {
    const box = $(panel.error);
    box.textContent = message || '';
    box.hidden = !message;
  }

  function stat(label, value) {
    return node('div', {}, node('dt', {text: label}), node('dd', {text: String(value ?? 0)}));
  }

  // 빠진 항목 + 난도 줄 — 기준 적용 표(CODE평가·LLM의미평가·합계)는 뺐다 (사용자 요청 2026-10-14)
  function coverageSummary(coverage, meta, showMissing = true) {
    const lines = [];
    if (coverage) {
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
      fixed(set) ? `고정 질문 · ${set.source_name || ''} · 출처 ` + Object.entries(set.fixed_sources || {}).map(([k, v]) => `${k} ${v}`).join(' · ')
      : imported(set) ? `직접 넣은 질문 · ${set.source_name || ''} · 모델 호출 없음`
        : `GPT ${set.model || ''} · 호출 ${set.calls || 0}회 · 입력 ${Number(usage.input_tokens || 0).toLocaleString('ko-KR')}토큰 · 출력 ${Number(usage.output_tokens || 0).toLocaleString('ko-KR')}토큰 · 보충 ${set.rounds || 0}회`,
    ].filter(Boolean);
    return node('details', {class: 'question-set-log'},
      node('summary', {text: (fixed(set) ? '세트 기록' : imported(set) ? '넣은 기록' : '생성 기록') + (warnings.length ? ` · 경고 ${warnings.length}건` : '')}),
      ...notes.map((text) => node('p', {text})),
      warnings.length ? node('ul', {class: 'question-set-warnings'}, warnings.slice(0, 20).map((w) => node('li', {text: w}))) : null);
  }

  // 질문 확인 — 질문마다 유형·난도·턴 원문·첨부 이름·긴 자료 글자 수·노리는 항목·함정. 평가 시작 전에 펼쳐 둔다
  const STATE = {making: '만드는 중', planned: '계획'};   // 생성 중 목록의 아직 다 만들지 않은 질문
  function questionRow(q) {
    const turns = q.turns || [];
    return node('li', {class: 'question-set-question'},
      node('div', {class: 'question-set-question-head'},
        q.id != null ? node('span', {class: 'question-set-qid', text: 'Q' + String(q.id).padStart(3, '0')}) : null,
        STATE[q.state] ? node('span', {class: 'pill ' + (q.state === 'making' ? 'warning' : 'neutral'), text: STATE[q.state]}) : null,
        node('strong', {text: q.topic || q.type || '질문'}),
        q.type && q.topic ? node('span', {class: 'question-set-tag', text: q.type}) : null,
        q.difficulty ? node('span', {class: 'question-set-tag', text: q.difficulty}) : null,
        turns.length > 1 ? node('span', {class: 'question-set-tag', text: `${turns.length}턴 대화`}) : null),
      node('ol', {class: 'question-set-turns'}, turns.map((t, i) => node('li', {},
        turns.length > 1 ? node('span', {class: 'question-set-turn-no', text: `T${i + 1}`}) : null,
        node('div', {class: 'question-set-turn-text'},
          node('p', {text: t.question}),
          (t.attachments || []).length ? node('p', {class: 'field-help', text: '첨부: ' + t.attachments.join(', ')}) : null,
          t.reference_chars ? node('p', {class: 'field-help', text: `긴 참조 자료 ${Number(t.reference_chars).toLocaleString('ko-KR')}자 함께 보냄`})
            : t.long_reference ? node('p', {class: 'field-help', text: '긴 참조 자료를 만들어 함께 보냄'}) : null)))),
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
      updateBusy(GEN);
      $('question-set-cancel').hidden = !running;
    }
    showError(set?.status === 'ERROR' ? '질문 세트를 만들지 못했습니다: ' + (set.error || '알 수 없는 오류') : '', panel);
    const body = $(panel.body);
    body.replaceChildren();
    if (!set) return;
    if (running) {
      body.append(node('p', {class: 'field-help', role: 'status', text: (set.message || '준비 중') + ' — 긴 자료가 있으면 몇 분 걸립니다.'}));
      const rows = set.preview || [];   // 만든 질문과 계획된 질문 (사용자 요청 2026-10-14: 어떤 질문이 만들어지는지 보이게)
      if (rows.length) {
        const made = rows.filter((r) => r.state === 'made').length;
        body.append(node('details', {class: 'question-set-review', open: ''},
          node('summary', {text: `만든 질문 ${made}개` + (rows.length > made ? ` · 계획된 질문 ${rows.length - made}개` : '')}),
          node('ol', {class: 'question-set-questions'}, rows.map(questionRow))));
      }
      return;
    }
    if (set.status !== 'COMPLETED') return;
    const s = set.summary || {};
    const meta = twoStep(set) ? null : `난도 쉬움 ${s.difficulty?.['쉬움'] ?? 0} · 보통 ${s.difficulty?.['보통'] ?? 0} · 어려움 ${s.difficulty?.['어려움'] ?? 0}`
      + (set.target_questions ? ` · 목표 질문 수 ${set.target_questions}` : '') + ((set.weak_units || []).length ? ` · 변별력 보강 ${set.weak_units.length}개 항목` : '');
    const details = !fixed(set);   // 고정 질문 세트는 요약 칸·질문 목록을 보이지 않고 수집·평가 진행만 (사용자 요청 2026-10-14)
    body.append(...[   // null 은 글자로 붙지 않게 뺀다
      details ? node('dl', {class: 'dataset-stats', 'aria-label': '질문 세트 요약'},
        stat('독립 질문·대화', s.questions), stat('단일 질문', s.single_questions), stat('멀티턴 대화', s.conversations),
        stat('턴', s.turns), stat('첨부파일', s.attachments), stat('긴 참조 자료', s.long_references)) : null,
      details ? coverageSummary(set.coverage, meta, !imported(set)) : null,   // 직접 넣은 질문은 빠진 항목 목록을 보이지 않는다
      details ? reviewBox(set) : null,
      set.run_error ? node('p', {class: 'question-set-missing', text: '답변 수집을 시작하지 못했습니다: ' + set.run_error}) : null,
      twoStep(set) ? runBox(panel) : null,
      details ? node('div', {class: 'question-set-footer'}, generationLog(set),
        node('div', {class: 'question-set-actions'},
          node('a', {class: 'button secondary compact', href: `/api/comparison/question-sets/${encodeURIComponent(set.id)}/download`, download: ''}, '질문 세트 JSON 내려받기'),
          twoStep(set) ? null
            : set.run_id
            ? node('button', {class: 'button primary compact', type: 'button', onclick: () => W.openRun(set.run_id)}, '답변 수집·평가 진행 보기')
            : node('button', {class: 'button primary compact', type: 'button', onclick: () => runSet(panel)}, '평가 시작'))) : null].filter(Boolean));
    if (twoStep(set)) pollRun(panel);
  }

  // 답변 수집 → 평가 진행 (질문 직접 넣기·고정 질문 세트) — 모델마다 받은 수 → 모두 받으면 '평가 진행' → 평가 중·결과 보기 (사용자 요청 2026-10-14)
  // 패널마다 상태(run·timer·failures·starting)를 따로 두고, 응답이 늦게 와도 그 세트를 보고 있을 때만 화면에 반영한다 (Codex)
  const STAGE = {WAITING: '대기', COLLECTING: '받는 중', COMPLETED: '완료', CANCELLED: '중단', ERROR: '오류'};
  function runBox(panel) {
    const box = node('div', {class: 'question-input-run', id: panel.runBox});
    fillRun(panel, box);
    return box;
  }

  function fillRun(panel, box = $(panel.runBox)) {
    if (!box) return;
    const set = panel.current;
    const run = panel.run && panel.run.run_id === set?.run_id ? panel.run : null;
    if (!set?.run_id) {
      box.replaceChildren(node('div', {class: 'question-set-actions'}, panel.starting
        ? node('button', {class: 'button primary compact', type: 'button', disabled: ''}, '답변 수집 시작 중…')
        : node('button', {class: 'button primary compact', type: 'button', onclick: () => startCollection(panel)}, set?.run_error ? '답변 수집 다시 시작' : '답변 수집')));
      return;
    }
    if (!run) { box.replaceChildren(node('p', {class: 'field-help', role: 'status', text: '답변 수집 상태를 불러오는 중'})); return; }
    const rows = (run.providers || []).map((p) => {
      const g = (run.progress || {})[p] || {};
      const failed = (g.failed || 0) + (g.incomplete || 0);
      return node('li', {}, node('strong', {text: W.providerName ? W.providerName(p) : p}),
        node('span', {text: ` ${run.models?.[p] || ''}`}),
        node('span', {class: 'pill ' + (g.stage === 'COMPLETED' ? 'success' : g.stage === 'COLLECTING' ? 'warning' : 'neutral'),
          text: `${STAGE[g.stage] || g.stage || '대기'} · ${g.done || 0}/${g.total || 0}` + (failed ? ` · 실패 ${failed}` : '')}));
    });
    let action;
    if (run.status === 'COLLECTING') action = node('button', {class: 'button primary compact', type: 'button', disabled: ''}, '답변 수집 중…');
    else if (run.status === 'READY') action = node('button', {class: 'button primary compact', type: 'button', onclick: () => evaluateRun(panel)}, '평가 진행');
    else if (run.status === 'EVALUATING') action = node('button', {class: 'button primary compact', type: 'button', onclick: () => W.openRun(run.run_id)},
      `평가 중 (${run.eval_done || 0}/${run.eval_total || 0}) · 진행 보기`);
    else if (run.status === 'COMPLETED') action = node('button', {class: 'button primary compact', type: 'button', onclick: () => W.openRun(run.run_id)}, '평가 결과 보기');
    else action = node('p', {class: 'question-set-missing', text: (run.status === 'CANCELLED' ? '수집이 중단됐습니다.' : '수집·평가 오류: ') + (run.error || '')});
    box.replaceChildren(
      node('p', {class: 'field-help', text: run.status === 'COLLECTING' ? '모델마다 동시에 답을 받는 중입니다. 모두 받으면 평가 진행 버튼이 켜집니다.'
        : run.status === 'READY' ? '모든 모델의 답변 수집이 끝났습니다. 평가 진행을 누르면 평가 모델이 채점합니다.' : '답변 수집'}),
      node('ul', {class: 'question-input-run-models'}, rows),
      node('div', {class: 'question-set-actions'}, action));
  }

  async function pollRun(panel) {
    clearTimeout(panel.timer);
    const id = panel.current?.run_id;
    if (!id) return;
    let run;
    try {
      run = await api(`/api/comparison/runs/${encodeURIComponent(id)}`);
    } catch (error) {   // 잠깐 끊겨도 같은 실행이면 점점 길게 다시 묻는다 (Codex) — 화면이 멈춘 채 남지 않게
      if (panel.current?.run_id !== id) return;
      showError('수집 상태를 불러오지 못했습니다(다시 시도 중): ' + error.message, panel);
      panel.failures = (panel.failures || 0) + 1;
      panel.timer = setTimeout(() => { if (panel.current?.run_id === id) pollRun(panel); }, Math.min(30000, 2000 * 2 ** panel.failures));
      return;
    }
    if (panel.current?.run_id !== id) return;
    if (panel.failures) { panel.failures = 0; showError('', panel); }
    panel.run = run;
    fillRun(panel);
    if (['COLLECTING', 'EVALUATING'].includes(run.status)) panel.timer = setTimeout(() => pollRun(panel), 2000);
  }

  // 패널의 바쁨 — 세트 만들기·질문 넣기·수집 시작 중이면 그 패널의 시작 버튼(간단·정밀 평가, 질문 넣기)을 끈다 (Codex: 한곳에서 켜고 끈다)
  function updateBusy(panel) {
    const busy = Boolean(panel.starting) || panel.current?.status === 'RUNNING';
    const ids = panel === GEN ? ['question-set-simple', 'question-set-precise'] : ['question-input-submit'];
    ids.forEach((id) => { if ($(id)) $(id).disabled = busy; });
  }

  // 수집 시작 — 실패했거나 아직 시작하지 않은 세트도 같은 세트로. 시작하는 동안은 그 패널의 다른 시작·새 세트를 막는다(유료 실행이 겹치지 않게, Codex).
  // owner: 세트를 만든 쪽(startFixed·submitQuestions)이 이미 패널을 잡고 있으면 그 표시를 이어받는다
  async function startCollection(panel, setId = panel.current?.id, owner = null) {
    if (!setId || (panel.starting && panel.starting !== owner)) return;
    panel.starting = setId;
    fillRun(panel); updateBusy(panel);
    try {
      const set = await api(`/api/comparison/question-sets/${encodeURIComponent(setId)}/run`, {});
      if (panel.current?.id === setId) render(set, panel);
    } catch (error) {
      if (panel.current?.id === setId) showError('답변 수집을 시작하지 못했습니다: ' + error.message, panel);
    } finally {
      if (panel.starting === setId) panel.starting = null;
      fillRun(panel); updateBusy(panel);
    }
  }

  async function evaluateRun(panel) {
    const id = panel.current?.run_id;
    if (!id) return;
    try {
      await api(`/api/comparison/runs/${encodeURIComponent(id)}/evaluate`, {});
      pollRun(panel);
    } catch (error) {
      showError('평가를 시작하지 못했습니다: ' + error.message, panel);
    }
  }

  // 고정 질문 세트(간단·정밀) — 버튼을 누르면 세트를 만들고 바로 답변 수집 (사용자 결정 2026-10-14)
  async function startFixed(kind) {
    if (GEN.starting) return;
    showError('', GEN);
    const owner = 'create:' + kind;   // 세트를 만드는 동안에도 패널을 잡아 예전 세트의 수집 버튼이 눌리지 않게 (Codex)
    GEN.starting = owner; fillRun(GEN); updateBusy(GEN);
    try {
      const set = await api('/api/comparison/question-sets/fixed', {kind});
      render(set, GEN);
      await startCollection(GEN, set.id, owner);
    } catch (error) {   // 켜져 있는 서버가 고정 세트 기능 전 코드면 404 — 무엇을 해야 하는지 알려 준다
      showError(error.status === 404 ? '서버가 고정 질문 기능을 넣기 전 코드로 켜져 있습니다. python run.py 를 다시 시작해 주세요.' : error.message, GEN);
    } finally {
      if (GEN.starting === owner) GEN.starting = null;
      fillRun(GEN); updateBusy(GEN);
    }
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
    if (INPUT.starting) return showError('답변 수집을 시작하는 중입니다. 끝난 뒤 다시 넣어 주세요.', INPUT);
    const owner = 'import';   // 질문을 넣는 동안에도 패널을 잡는다 (Codex)
    INPUT.starting = owner; fillRun(INPUT); updateBusy(INPUT);
    try {   // 질문을 넣고 바로 답변 수집 — 평가는 모두 받은 뒤 '평가 진행'으로 (사용자 요청 2026-10-14)
      const set = await api('/api/comparison/question-sets/import', payload);
      render(set, INPUT);
      await startCollection(INPUT, set.id, owner);
    }
    catch (error) { showError(error.message, INPUT); }
    finally {
      if (INPUT.starting === owner) INPUT.starting = null;
      fillRun(INPUT); updateBusy(INPUT);
    }
  }

  $('question-set-simple').addEventListener('click', () => startFixed('simple'));
  $('question-set-precise').addEventListener('click', () => startFixed('precise'));
  api('/api/comparison/question-sets/fixed').then((sets) => {   // 버튼에 질문 수
    for (const item of sets) {
      const button = $('question-set-' + item.kind);
      if (button) button.textContent = `${item.label} · ${item.questions}문항`;
    }
  }).catch(() => {});
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
  // 새 비교 = 새로 답변을 모아 평가하는 것 — 열 때마다 두 칸을 비운다. 지난 실행은 결과 비교·이력 관리에서 본다 (사용자 요청 2026-10-14).
  // 수집을 시작하는 중인 칸만은 그대로 둔다(시작 응답이 와야 실행이 잡힌다)
  function resetPanels() {
    for (const panel of [GEN, INPUT]) {
      if (panel.starting || !$(panel.body)) continue;
      clearTimeout(panel.timer);
      panel.current = null; panel.run = null; panel.failures = 0;
      showError('', panel);
      render(null, panel);
    }
    if ($('question-input-text') && !INPUT.starting) $('question-input-text').value = '';
  }
  window.addEventListener('aiona:setup-open', resetPanels);
  render(null);
  if ($('question-input-panel')) render(null, INPUT);
})();
