/* 새 비교 — AIONA Q/A 업로드 직후 자동 수집, AI별 수집 상태·재시도·중단, 질문별 답변 비교, 평가 시작.
   자동 요청은 관리 설정에서 허용·한도를 정한 제공자에만 서버가 보낸다. 같은 업로드·새로고침은 기존 작업을 다시 연다.
   원문은 모두 textContent 로 넣는다. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  if (!window.AionaWorkspace || !$('collection-panel')) return;
  const W = window.AionaWorkspace, {node, pill, number, api} = W;
  const SESSION_KEY = 'aiona-collection-run';
  const PAGE_SIZE = 10;
  const LIVE = ['gpt', 'claude', 'gemini', 'factchat'];
  const state = {status: null, runId: null, starting: false, queued: null, notice: '', jobs: new Map(), search: '', filter: '', page: 0,
    open: new Set(), items: new Map(), listKey: ''};
  const usd = (value) => '$' + Number(value || 0).toLocaleString('en-US', {maximumFractionDigits: 4});

  // --- 현재 작업 -----------------------------------------------------------------------------
  const inConversation = (c) => c.conversation_id !== undefined && c.conversation_id !== null;
  // 내용 지문(32비트 FNV-1a) — 질문·답변·대화 위치가 하나라도 바뀌면 다른 데이터로 본다 (같은 길이로 바꾼 글도)
  function fingerprint(text) {
    let hash = 0x811c9dc5;
    for (let i = 0; i < text.length; i++) { hash ^= text.charCodeAt(i); hash = Math.imul(hash, 0x01000193) >>> 0; }
    return hash.toString(36);
  }
  const datasetKey = (cases) => cases.length + ':' + fingerprint(JSON.stringify(cases.map((c) =>
    [c.case_id, c.question, c.answer, c.conversation_id ?? null, c.turn ?? null])));
  // 화면에 묶인 작업의 전체 결과 (답변 원문 포함). 다른 실행을 보고 있으면 null.
  function job() {
    const result = W.currentResult(), run = W.currentRun();
    if (!state.runId) return null;
    if (result?.run_id === state.runId) return run?.run_id === state.runId ? {...result, ...run, datasets: result.datasets} : result;
    return null;
  }
  const collecting = (run) => run?.status === 'COLLECTING';
  const evaluated = (run) => Boolean(run && (run.phase === 'EVALUATE' || Object.keys(run.evaluations || {}).length));
  const canExtend = (run) => Boolean(run && !collecting(run) && !evaluated(run) && ['READY', 'CANCELLED', 'ERROR'].includes(run.status));
  const target = (id) => state.status?.targets?.find((t) => t.id === id);
  const providerLabel = (id, run) => id === 'aiona' ? 'AIONA' : target(id)?.name || run?.models?.[id] || id;
  function remember(runId) {
    try { if (runId) sessionStorage.setItem(SESSION_KEY, runId); else sessionStorage.removeItem(SESSION_KEY); } catch (_) { /* storage may be disabled */ }
  }
  function showError(message) { $('collection-error').textContent = message || ''; $('collection-error').hidden = !message; }

  async function loadStatus() {
    try { state.status = await api('/api/comparison/auto-collection'); }
    catch (_) { /* 연결 정보가 없으면 각 AI 상태를 '확인 중'으로 둔다 */ }
    render();
  }

  async function startJob({rerun = false} = {}) {
    const dataset = W.dataset();
    if (!dataset.cases.length || W.versionMode()) return;
    // 앞 요청이 끝나지 않았으면 기다렸다가 그때의 최신 데이터로 다시 시작한다 (새 업로드를 버리지 않음)
    // 대기열은 그때의 데이터 내용·입력 방식에 묶는다 — 끝났을 때 바뀌어 있으면(직접 입력 초안 등) 보내지 않는다.
    // 재수집 표시는 같은 데이터를 다시 요청할 때만 이어 간다 (다른 데이터로 옮겨 붙어 유료 재수집이 되지 않게)
    if (state.starting) {
      const entry = {rerun, key: datasetKey(dataset.cases), mode: dataset.inputMode};
      const same = state.queued && state.queued.key === entry.key && state.queued.mode === entry.mode;
      state.queued = same ? {...entry, rerun: rerun || state.queued.rerun} : entry;
      return;
    }
    const key = datasetKey(dataset.cases);
    state.starting = true; showError(null); render();
    try {
      const response = await api('/api/comparison/auto-collection/jobs', {cases: dataset.cases, name: $('run-name').value.trim() || dataset.name,
        input_name: dataset.name, input_mode: dataset.inputMode, rerun,
        input_document: dataset.importInfo?.converted ? dataset.importInfo : undefined,
        expected_model_version: W.currentRelease()?.version});
      const run = response.run;
      state.jobs.set(key, run.run_id);   // 그 데이터의 작업으로 기억만 하고
      if (datasetKey(W.dataset().cases) !== key) return;   // 그 사이 다른 데이터로 바뀌었으면 화면에 붙이지 않는다
      state.runId = run.run_id; remember(run.run_id);
      const skipped = Object.entries(response.skipped || {}).map(([id, reason]) => `${providerLabel(id)}: ${reason}`);
      state.notice = response.reused ? '같은 파일로 만든 수집 작업을 다시 열었습니다. 새 요청은 보내지 않았습니다.' :
        run.providers.length > 1 ? `${run.providers.filter((p) => p !== 'aiona').map((p) => providerLabel(p, run)).join(' · ')}에 같은 질문을 보내고 있습니다.` +
          (skipped.length ? ' 한도 때문에 시작하지 않은 AI: ' + skipped.join(' / ') : '') :
        skipped.length ? '한도 안에서 시작할 수 있는 AI가 없습니다. ' + skipped.join(' / ') : '자동 수집할 수 있는 AI가 없습니다. 관리 설정에서 연결과 허용 여부를 확인하세요.';
      W.announce(response.reused ? '이미 수집한 작업을 열었습니다.' : '자동 수집을 시작했습니다.');
      W.openRun(run.run_id, {navigate: false});
    } catch (err) { if (datasetKey(W.dataset().cases) === key) showError(err.message); }
    finally {
      state.starting = false;
      const queued = state.queued;
      state.queued = null;
      render();
      const now = W.dataset();
      if (queued && datasetKey(now.cases) === queued.key && now.inputMode === queued.mode) startJob({rerun: queued.rerun});
    }
  }

  async function collectMore(providers) {
    const run = job();
    if (!run || !canExtend(run)) return;
    showError(null);
    try {
      const response = await api(`/api/comparison/runs/${run.run_id}/collect`, {providers});
      const skipped = Object.entries(response.skipped || {}).map(([id, reason]) => `${providerLabel(id)}: ${reason}`);
      state.notice = '실패하거나 받지 못한 답변만 다시 요청합니다. 이미 받은 답변은 그대로 둡니다.' + (skipped.length ? ' 시작하지 않음 — ' + skipped.join(' / ') : '');
      W.openRun(run.run_id, {navigate: false});
    } catch (err) { showError(err.message); }
  }

  // --- AI별 수집 상태 --------------------------------------------------------------------------
  function bar(done, total, kind) {
    const value = node('progress', {max: String(Math.max(1, total)), value: String(done), 'aria-hidden': 'true'});
    value.className = kind;
    return value;
  }
  function action(label, onclick, kind = 'text-button') {
    return node('button', {type: 'button', class: kind, onclick}, label);
  }
  function firstAnswer(run, id) {
    const rows = run.datasets?.[id] || [];
    const index = rows.findIndex((row) => row && (id === 'aiona' || row.collection?.status === 'SUCCESS'));
    return index < 0 ? 0 : index;
  }
  function aionaRow(run, dataset) {
    const cases = run?.datasets?.aiona || dataset.cases, answered = cases.filter((c) => c.answer.trim()).length;
    const release = W.currentRelease();
    return node('li', {class: 'collection-target', 'data-target': 'aiona'}, W.avatar('aiona'),
      node('div', {class: 'target-name'}, node('strong', {text: 'AIONA 원본 답변'}),
        node('small', {text: run?.aiona_release?.version ? 'AIONA ' + run.aiona_release.version + ' · 업로드 원문' : release ? 'AIONA ' + release.version + ' · 업로드 원문' : '업로드 원문 · 다시 생성하지 않음'})),
      cases.length ? pill(answered === cases.length ? '준비됨' : `답변 없음 ${number(cases.length - answered)}턴`, answered === cases.length ? 'success' : 'warning') : pill('파일 대기', 'neutral'),
      node('div', {class: 'target-progress'}, bar(answered, cases.length || 1, 'success'), node('span', {text: cases.length ? `${number(answered)} / ${number(cases.length)}턴` : '—'})),
      node('div', {class: 'target-actions'}, cases.length ? action('답변 보기', () => W.openAnswer(firstAnswer(run || {datasets: {aiona: cases}}, 'aiona'), run ? 'result' : 'dataset', 'aiona')) : null));
  }
  const stageLabels = {completed: ['수집 완료', 'success'], partial: ['일부 실패', 'warning'], failed: ['수집 실패', 'danger'],
    cancelled: ['중단됨', 'warning'], collecting: ['수집 대기', 'neutral']};
  function inRunRow(t, run) {
    const id = t.id, rows = run.datasets?.[id] || [], progress = run.progress?.[id] || {};
    const total = rows.length || run.question_count, success = rows.filter((row) => row?.collection?.status === 'SUCCESS').length;
    const failed = rows.filter((row) => row && row.collection?.status !== 'SUCCESS').length;
    const entry = run.auto_collection?.providers?.[id] || {};
    const active = collecting(run) && progress.stage === 'COLLECTING';
    const [label, kind] = active ? ['수집 중', 'purple'] : collecting(run) && progress.stage === 'WAITING' ? ['수집 대기', 'neutral'] :
      stageLabels[entry.state] || (success === total ? stageLabels.completed : stageLabels.partial);
    const model = id === 'factchat' ? `${entry.model || t.model} · ${({openai: 'OpenAI', anthropic: 'Anthropic', google: 'Google', xai: 'xAI', perplexity: 'Perplexity', meta: 'Meta'})[entry.vendor] || '제공자 미확인'} 모델` +
      (entry.response_models?.length && entry.response_models.join() !== entry.model ? ` · 응답 모델 ${entry.response_models.join(', ')}` : '') : run.models?.[id] || t.model;
    const actions = node('div', {class: 'target-actions'});
    if (success) actions.append(action('답변 보기', () => W.openAnswer(firstAnswer(run, id), 'result', id)));
    if ((failed || success < total) && canExtend(run) && t.state === 'ready') actions.append(action('다시 수집', () => collectMore([id]), 'button secondary compact'));
    const item = node('li', {class: 'collection-target', 'data-target': id}, W.avatar(id),
      node('div', {class: 'target-name'}, node('strong', {text: t.name}), node('small', {text: (id === 'factchat' ? 'FactChat API Gateway · ' : '') + model})),
      pill(label, kind),
      node('div', {class: 'target-progress'}, bar(progress.done ?? success + failed, total, failed ? 'warning' : 'success'),
        node('span', {text: `${number(success)} / ${number(total)}턴 완료` + (failed ? ` · 오류 ${number(failed)}` : '')})),
      actions);
    if (entry.reason && !active && (failed || entry.state !== 'completed')) item.append(node('p', {class: 'target-reason', text: entry.reason}));
    return item;
  }
  function outsideRow(t, run, dataset) {
    const id = t.id, entry = run?.auto_collection?.providers?.[id];
    let label = t.label, kind = 'neutral', reason = t.reason || t.note;
    const actions = node('div', {class: 'target-actions'});
    if (t.state === 'ready') {
      if (!run) { label = dataset.cases.length ? '대기' : '자동 수집 준비'; kind = 'success'; reason = '파일을 올리면 이 AI에 자동으로 질문을 보냅니다.'; }
      else if (entry?.state === 'over_limit') { label = '한도 초과'; kind = 'warning'; reason = entry.reason; }
      else { label = '수집 안 됨'; reason = '이 작업을 만든 뒤 연결·허용되었습니다. 필요하면 수집을 시작하세요.'; }
      if (run && canExtend(run)) actions.append(action('수집 시작', () => collectMore([id]), 'button secondary compact'));
    } else if (t.state === 'unsupported') {
      reason = t.note;
    } else {
      kind = t.state === 'needs_connection' ? 'neutral' : 'warning';
      actions.append(action(t.state === 'needs_connection' ? '연결하기' : '관리 설정', () => W.go('connections')));
    }
    return node('li', {class: 'collection-target is-idle', 'data-target': id}, W.avatar(id),
      node('div', {class: 'target-name'}, node('strong', {text: t.name}), node('small', {text: t.kind === 'unsupported' ? t.platform : t.connected ? (t.kind === 'gateway' ? 'FactChat API Gateway · ' : '') + t.model : t.platform})),
      pill(label, kind), node('div', {class: 'target-progress'}, node('span', {class: 'target-reason-inline', text: reason || ''})), actions);
  }
  function renderTargets(run, dataset) {
    const targets = state.status?.targets || LIVE.map((id) => ({id, name: id, label: '확인 중', state: 'checking', kind: 'api'}));
    $('collection-targets').replaceChildren(aionaRow(run, dataset),
      ...targets.map((t) => run?.providers?.includes(t.id) ? inRunRow(t, run) : outsideRow(t, run, dataset)));
  }
  function renderHeader(run, dataset) {
    const status = $('collection-status');
    const answers = run ? run.providers.filter((p) => p !== 'aiona') : [];
    const failures = answers.some((p) => (run.datasets?.[p] || []).some((row) => !row || row.collection?.status !== 'SUCCESS'));
    const [text, kind] = !dataset.cases.length ? ['파일 대기', 'neutral'] : state.starting ? ['작업 준비 중', 'purple'] :
      !run ? [W.versionMode() ? '버전 평가' : dataset.inputMode === 'manual' ? '수집 대기' : '작업 확인 중', 'neutral'] :
      collecting(run) ? [`수집 중 ${number(run.done)} / ${number(run.total)}`, 'purple'] :
      evaluated(run) ? [run.status === 'COMPLETED' ? '평가 완료' : run.status === 'EVALUATING' ? '평가 중' : '평가 중단', run.status === 'COMPLETED' ? 'success' : 'purple'] :
      !answers.length ? ['수집한 AI 없음', 'warning'] : run.status === 'CANCELLED' ? ['수집 중단', 'warning'] : failures ? ['일부 수집 실패', 'warning'] : ['수집 완료', 'success'];
    status.textContent = text; status.className = 'pill ' + kind;
    $('collection-cancel').hidden = !collecting(run); $('collection-cancel').disabled = Boolean(run?.cancel_requested);
    $('collection-cancel').textContent = run?.cancel_requested ? '중단 대기 중' : '수집 중단';
    $('collection-rerun').hidden = !run || collecting(run) || !(state.status?.targets || []).some((t) => t.state === 'ready');
    $('collection-description').textContent = state.notice && run ? state.notice :
      dataset.inputMode === 'manual' && !run ? '직접 입력한 질문은 “이 질문으로 답변 수집”을 누르면 연결된 AI에 보냅니다.' :
      '파일을 올리면 관리 설정에서 허용한 AI가 같은 질문에 답변합니다. 멀티턴은 턴 순서대로, 각 AI 자신의 앞 답변만 이어 씁니다.';
  }
  function renderBudget(run) {
    const status = state.status, limits = status?.policy?.limits;
    const budget = run?.auto_collection?.budget;
    const parts = [];
    if (budget && (budget.authorized_calls || budget.spent_calls)) {
      parts.push(`이번 작업 승인 ${number(budget.authorized_calls)}회 · 비용 상한 ${usd(budget.authorized_cost_usd)} · 사용 ${number(budget.spent_calls)}회 · ${usd(budget.spent_cost_usd)}` +
        (budget.estimated_calls ? ` (사용량이 없는 ${number(budget.estimated_calls)}회는 상한으로 계산)` : ''));
    }
    if (status) {
      parts.push(status.limits_ready ? `한도 작업당 ${number(limits.job_calls)}회·${usd(limits.job_cost_usd)} · 일일 ${number(limits.daily_calls)}회·${usd(limits.daily_cost_usd)} (오늘 사용·예약 ${number(status.today.calls)}회·${usd(status.today.cost_usd)})` :
        '관리 설정에서 호출·비용 한도를 정하기 전에는 자동 수집을 시작하지 않습니다.');
    }
    $('collection-budget').textContent = parts.join(' · ');
    $('collection-budget').hidden = !parts.length;
  }

  // --- 질문별 답변 비교 --------------------------------------------------------------------------
  function units(cases) {
    const list = [], byConversation = new Map();
    cases.forEach((c, index) => {
      if (!inConversation(c)) { list.push({key: 'case:' + index, label: c.case_id, indices: [index]}); return; }
      if (!byConversation.has(c.conversation_id)) {
        const unit = {key: 'conversation:' + c.conversation_id, label: c.source_group || c.conversation_id, indices: [], conversation: true};
        byConversation.set(c.conversation_id, unit); list.push(unit);
      }
      byConversation.get(c.conversation_id).indices.push(index);
    });
    for (const unit of list) unit.indices.sort((a, b) => (cases[a].turn || 0) - (cases[b].turn || 0));
    return list;
  }
  function cellState(run, provider, index) {
    if (provider === 'aiona') return {kind: 'success', text: 'AIONA 원문'};
    const row = run?.datasets?.[provider]?.[index];
    if (!row) return collecting(run) ? {kind: 'purple', text: '수집 대기', pending: true} : {kind: 'neutral', text: '수집되지 않음', pending: true};
    const status = row.collection?.status;
    return status === 'SUCCESS' ? {kind: 'success', text: '수집 완료'} :
      status === 'INCOMPLETE' ? {kind: 'warning', text: '응답 미완료', error: true} : {kind: 'danger', text: '수집 실패', error: true};
  }
  function compareSource() {
    const run = job(), dataset = W.dataset();
    return run ? {run, cases: run.datasets?.aiona || [], providers: run.providers} : {run: null, cases: dataset.cases, providers: ['aiona']};
  }
  function itemFingerprint(unit, source) {
    const open = state.open.has(unit.key);
    return JSON.stringify([open, source.run?.run_id || '', source.providers, unit.indices.map((index) => source.providers.map((p) => {
      if (p === 'aiona') return fingerprint((source.cases[index]?.question || '') + '\u0001' + (source.cases[index]?.answer || ''));
      const row = source.run?.datasets?.[p]?.[index];
      return open ? [row?.collection?.status || null, row?.answer?.length ?? null] : row?.collection?.status || null;
    }))]);
  }
  function answerCard(source, provider, index) {
    const c = source.cases[index], cell = cellState(source.run, provider, index);
    const row = provider === 'aiona' ? c : source.run?.datasets?.[provider]?.[index];
    const body = node('div', {class: 'compare-answer-body'});
    if (row && (provider === 'aiona' || row.collection?.status === 'SUCCESS' || row.answer)) body.append(node('div', {class: 'answer-text', text: row.answer || '(빈 답변)'}));
    if (row?.collection?.error) body.append(node('p', {class: 'compare-error', text: row.collection.error}));
    if (!row) body.append(node('p', {class: 'empty-response', text: cell.text === '수집 대기' ? '이 AI의 답변을 기다리고 있습니다.' : '이 AI의 답변이 아직 없습니다.'}));
    return node('article', {class: 'compare-answer', 'data-provider': provider},
      node('header', {}, W.avatar(provider), node('strong', {text: providerLabel(provider, source.run)}), pill(cell.text, cell.kind)),
      body,
      row ? node('button', {type: 'button', class: 'text-button compare-detail', onclick: () => W.openAnswer(index, source.run ? 'result' : 'dataset', provider,
        inConversation(c) ? 'conversation:' + c.conversation_id : '')}, '상세 보기', W.icon('arrow')) : null);
  }
  function chips(unit, source) {
    return node('span', {class: 'compare-chips'}, ...source.providers.filter((p) => p !== 'aiona').map((p) => {
      const states = unit.indices.map((index) => cellState(source.run, p, index));
      const kind = states.some((s) => s.error) ? 'danger' : states.some((s) => s.pending) ? 'neutral' : 'success';
      const text = providerLabel(p, source.run) + ' · ' + (kind === 'success' ? '모두 수집' : kind === 'danger' ? '오류 있음' : '수집 전');
      return node('span', {class: 'compare-chip ' + kind, title: text, 'aria-label': text}, W.avatar(p));
    }));
  }
  function body(unit, source) {
    const box = node('div', {class: 'compare-body'});
    for (const index of unit.indices) {
      const c = source.cases[index];
      const answers = node('div', {class: 'compare-answers'}, ...source.providers.map((p) => answerCard(source, p, index)));
      answers.style.setProperty('--compare-columns', String(Math.min(source.providers.length, 3)));
      box.append(node('div', {class: 'compare-turn'},
        node('div', {class: 'compare-turn-question'}, node('span', {text: unit.conversation ? `${c.turn}턴 · ${c.case_id}` : c.case_id}), node('pre', {text: c.question})),
        answers));
    }
    return box;
  }
  // 요약 줄은 그대로 두고 상태 칩과 펼친 본문만 바꾼다 — 수집이 진행돼도 포커스와 펼침이 유지된다
  function updateItem(details, unit, source) {
    const first = source.cases[unit.indices[0]];   // 요약 줄의 질문도 현재 원문으로 (직접 입력을 고친 경우)
    details.querySelector('.compare-id').textContent = unit.conversation ? unit.label : first.case_id;
    details.querySelector('.compare-question').textContent = first.question;
    details.querySelector('.compare-chips').replaceWith(chips(unit, source));
    const current = details.querySelector(':scope > .compare-body');
    if (details.open) { if (current) current.replaceWith(body(unit, source)); else details.append(body(unit, source)); }
    else if (current) current.remove();
    state.items.set(unit.key, {fingerprint: itemFingerprint(unit, source), node: details});
  }
  function itemNode(unit, source) {
    const first = source.cases[unit.indices[0]];
    const details = node('details', {class: 'compare-item', 'data-unit': unit.key},
      node('summary', {}, node('span', {class: 'compare-id', text: unit.conversation ? unit.label : first.case_id}),
        node('span', {class: 'compare-question', text: first.question}),
        unit.conversation ? pill(`멀티턴 ${number(unit.indices.length)}턴`, 'purple') : null, node('span', {class: 'compare-chips'}), W.icon('chevron')));
    details.open = state.open.has(unit.key);
    details.addEventListener('toggle', () => {
      if (details.open) state.open.add(unit.key); else state.open.delete(unit.key);
      updateItem(details, unit, compareSource());
    });
    updateItem(details, unit, source);
    return details;
  }
  function matches(unit, source) {
    const query = state.search.trim().toLocaleLowerCase();
    if (query && !unit.indices.some((i) => (source.cases[i].case_id + ' ' + source.cases[i].question).toLocaleLowerCase().includes(query)) &&
        !unit.label.toLocaleLowerCase().includes(query)) return false;
    if (state.filter === 'multi') return Boolean(unit.conversation);
    if (state.filter === 'pending' || state.filter === 'error') {
      return source.providers.some((p) => p !== 'aiona' && unit.indices.some((i) => cellState(source.run, p, i)[state.filter === 'pending' ? 'pending' : 'error']));
    }
    return true;
  }
  function renderCompare() {
    const source = compareSource(), all = units(source.cases), list = all.filter((unit) => matches(unit, source));
    const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
    state.page = Math.min(state.page, pages - 1);
    const visible = list.slice(state.page * PAGE_SIZE, state.page * PAGE_SIZE + PAGE_SIZE);
    const listKey = JSON.stringify([source.run?.run_id || '', visible.map((unit) => unit.key)]);
    if (listKey !== state.listKey) { state.items.clear(); state.listKey = listKey; }
    const nodes = visible.map((unit) => {
      const cached = state.items.get(unit.key);
      if (!cached) return itemNode(unit, source);
      if (cached.fingerprint !== itemFingerprint(unit, source)) updateItem(cached.node, unit, source);
      return cached.node;
    });
    const host = $('compare-list');
    if (nodes.length !== host.children.length || nodes.some((n, i) => host.children[i] !== n)) {
      host.replaceChildren(...nodes);
      if (!nodes.length) host.append(node('p', {class: 'compare-empty', text: source.cases.length ? '조건에 맞는 질문이 없습니다.' : 'AIONA Q/A 파일을 올리면 질문 목록이 여기에 나타납니다.'}));
    }
    $('compare-count').textContent = `질문·대화 ${number(all.length)}개`;
    $('compare-caption').textContent = list.length ? `${number(list.length)}개 중 ${number(state.page * PAGE_SIZE + 1)}–${number(Math.min(list.length, (state.page + 1) * PAGE_SIZE))} 표시` : '';
    $('compare-page').textContent = `${state.page + 1} / ${pages}`;
    $('compare-previous').disabled = state.page === 0; $('compare-next').disabled = state.page + 1 >= pages;
    $('preview-all').disabled = !source.cases.length;
  }

  // --- 하단 실행 막대 (workspace.renderReady 가 부른다) -------------------------------------------
  function renderAction() {
    if (W.versionMode()) return;   // 버전 평가는 workspace.renderReady 가 그린다 (자동 수집 없음)
    const dataset = W.dataset(), run = job(), button = $('collect-button');
    const unitsCount = units(dataset.cases).length;
    let label = '평가 시작', disabled = true, summary, detail;
    if (!dataset.cases.length) {
      summary = dataset.inputMode === 'manual' ? '질문과 AIONA 답변을 입력하세요' : 'AIONA 서비스의 Q/A 파일을 업로드해 주세요';
      detail = dataset.loading ? '파일을 확인하고 있습니다.' : '올리면 연결된 AI의 답변 수집이 자동으로 시작됩니다.';
    } else {
      summary = `질문·대화 ${number(unitsCount)}개 · 평가 대상 턴 ${number(dataset.cases.length)}개`;
      if (!run) detail = state.starting ? '자동 수집 작업을 준비하고 있습니다.' : dataset.inputMode === 'manual' ? '“이 질문으로 답변 수집”을 누르면 연결된 AI에 요청합니다.' : '수집 작업 상태를 확인하고 있습니다.';
      else if (collecting(run)) detail = '답변 수집이 끝나면 평가를 시작할 수 있습니다. 수집 중에도 아래에서 받은 답변을 확인할 수 있습니다.';
      else if (evaluated(run)) { label = '평가 결과 보기'; disabled = false; detail = '이 작업의 평가 결과를 결과 비교에서 확인합니다.'; }
      else {
        const notice = W.evaluatorNotice();
        disabled = Boolean(notice) || W.isBusy() || !W.isReady();
        detail = notice || `AIONA 포함 ${number(run.providers.length)}개 답변을 기존 코드 검사와 의미 평가(J1~J8)로 평가합니다. 평가 모델에는 모델 이름을 가리고 보내며, 평가자 호출이 발생합니다.`;
      }
    }
    button.replaceChildren(label, W.icon('arrow'));
    button.dataset.mode = label === '평가 결과 보기' ? 'results' : 'evaluate';
    button.disabled = disabled;
    $('ready-summary').textContent = summary; $('ready-detail').textContent = detail;
  }

  function render() {
    const dataset = W.dataset(), run = job();
    renderHeader(run, dataset); renderTargets(run, dataset); renderBudget(run); renderCompare(); renderAction();
    $('manual-collect').disabled = dataset.inputMode !== 'manual' || !dataset.cases.length || state.starting || Boolean(run);
  }

  // --- 이벤트 ---------------------------------------------------------------------------------
  window.addEventListener('aiona:dataset-changed', (event) => {
    const reason = event.detail?.reason, dataset = W.dataset();
    if (['upload', 'restore', 'manual', 'version-reset'].includes(reason)) { state.page = 0; state.open.clear(); }
    if (W.versionMode() || !dataset.cases.length) state.runId = null;
    else state.runId = state.jobs.get(datasetKey(dataset.cases)) || (reason === 'restore' ? state.runId : null);
    if (reason === 'upload' || reason === 'manual') state.notice = '';
    // 업로드는 늘 서버에 작업을 요청한다 — 같은 업로드면 서버가 기존 작업을 돌려주고 새 요청을 만들지 않는다
    if (reason === 'upload' && !W.versionMode()) startJob();
    // 입력 방식을 바꿔 다른 데이터로 돌아오면 그 데이터의 수집 작업을 다시 현재 실행으로 연다 (새 요청 없음)
    else if (state.runId && W.currentRun()?.run_id !== state.runId) W.openRun(state.runId, {navigate: false});
    render();
  });
  window.addEventListener('aiona:run-rendered', render);
  window.addEventListener('aiona:collection-status', (event) => { state.status = event.detail; render(); });
  window.addEventListener('aiona:semantic-setup', () => { loadStatus(); });
  window.addEventListener('aiona:setup-open', () => {
    loadStatus();
    // 다른 실행을 보고 돌아오면 이 화면의 수집 작업을 다시 현재 실행으로 연다 (새 요청 없음)
    if (state.runId && W.currentRun()?.run_id !== state.runId) W.openRun(state.runId, {navigate: false});
  });
  $('collect-button').addEventListener('click', () => {
    if ($('collect-button').dataset.mode === 'results') W.go('results');
    else if (job()) W.startEvaluation();
  });
  $('manual-collect').addEventListener('click', () => startJob());
  $('collection-cancel').addEventListener('click', async () => {
    const run = job();
    if (!run) return;
    $('collection-cancel').disabled = true;
    try { await api(`/api/comparison/runs/${run.run_id}/cancel`, {}); W.openRun(run.run_id, {navigate: false}); }
    catch (err) { showError(err.message); }
  });
  $('collection-rerun').addEventListener('click', () => {
    const count = W.dataset().cases.length, ready = (state.status?.targets || []).filter((t) => t.state === 'ready');
    if (!window.confirm(`같은 질문 ${number(count)}턴을 ${ready.map((t) => t.name).join(' · ')}에 다시 보내 새 실행으로 수집합니다. ` +
      '관리 설정 한도 안에서만 시작하며 유료 호출이 발생할 수 있습니다. 계속할까요?')) return;
    startJob({rerun: true});
  });
  $('compare-search').addEventListener('input', (event) => { state.search = event.target.value; state.page = 0; renderCompare(); });
  $('compare-filter').addEventListener('change', (event) => { state.filter = event.target.value; state.page = 0; renderCompare(); });
  $('compare-previous').addEventListener('click', () => { state.page--; renderCompare(); });
  $('compare-next').addEventListener('click', () => { state.page++; renderCompare(); });
  $('preview-all').addEventListener('click', () => W.openAnswer(0, job() ? 'result' : 'dataset', 'aiona'));

  // 새로고침: 이 탭에서 연 수집 작업을 다시 붙인다. 작업을 다시 시작하지 않는다.
  async function restore() {
    let runId = null;
    try { runId = sessionStorage.getItem(SESSION_KEY); } catch (_) { /* private browsing */ }
    if (!runId) return;
    try {
      const run = await api(`/api/comparison/runs/${runId}/result`);
      if (run.run_kind !== 'auto_collection') { remember(null); return; }
      state.runId = runId;
      state.jobs.set(datasetKey(run.datasets.aiona), runId);
      W.restoreDataset(run);
      if (!W.currentRun() || W.currentRun().run_id !== runId) W.openRun(runId, {navigate: false});
    } catch (err) { if (err.status === 404) remember(null); }
    render();
  }
  window.AionaAutoCollection = {renderAction, render, startJob, state};
  render();
  restore();
})();
