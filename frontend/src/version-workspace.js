/* Saved AIONA releases. Viewing a comparison only reads persisted results. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const W = window.AionaWorkspace, {node, pill, number, percent, api} = W;
  const state = {history: null, before: null, after: null, comparison: null, rows: [], generation: 0, historyGeneration: 0, requestedRun: null, submitting: false, selection: null, detailGeneration: 0};
  try {
    const saved = JSON.parse(sessionStorage.getItem('aiona-version-comparison') || 'null');
    if (saved && ['baseline', 'target', 'baselineVersion', 'targetVersion', 'changeFilter'].every((key) => saved[key] === undefined || typeof saved[key] === 'string')) state.selection = saved;
  } catch (_) { /* Session storage is optional; no source text is stored here. */ }
  const labels = {improved: '개선', regressed: '악화', passMaintained: '통과 유지', failMaintained: '실패 유지', pending: '미완료', unverified: '조건 확인 필요', unmatched: '문항 불일치'};
  const kinds = {improved: 'success', regressed: 'danger', passMaintained: 'neutral', failMaintained: 'neutral', pending: 'warning', unverified: 'warning', unmatched: 'warning'};
  const releaseName = (run, fallback = '') => run?.aiona_release?.version ? 'AIONA ' + run.aiona_release.version : fallback || '버전 미등록';
  const time = (value) => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString('ko-KR') : value || '기록 없음';
  const signed = (value, suffix = '') => value === null || value === undefined ? '—' : `${value > 0 ? '+' : ''}${(value * 100).toFixed(1)}${suffix}`;
  function error(message) {
    for (const id of ['version-error', 'comparison-error']) {
      if ($(id)) { $(id).textContent = message || ''; $(id).hidden = !message; }
    }
  }
  function saveSelection(selection) {
    state.selection = selection;
    try { sessionStorage.setItem('aiona-version-comparison', JSON.stringify(selection)); } catch (_) { /* Keep selection in memory if storage is disabled. */ }
  }
  function rememberRun(runId, side) {
    if (typeof runId !== 'string' || !runId) return;
    saveSelection({...state.selection, [side]: runId});
  }
  const statusText = (run) => ({COMPLETED: '평가 종료', EVALUATING: '평가 중', COLLECTING: '수집 중', ERROR: '오류', READY: '평가 대기', CANCELLED: '취소'}[run?.status] || run?.status || '평가 기록 없음');
  const statusKind = (run) => ({COMPLETED: 'success', EVALUATING: 'purple', COLLECTING: 'purple', ERROR: 'danger', CANCELLED: 'warning'}[run?.status] || 'neutral');
  function conditionSummary(run) {
    if (window.AionaHistory?.conditionSummary) return window.AionaHistory.conditionSummary(run);
    if (typeof run?.evaluation_conditions_summary === 'string') return run.evaluation_conditions_summary;
    const fields = run?.evaluation_profile?.fields;
    return [fields?.judge?.requested_model, fields?.semantic_version && '기준 ' + fields.semantic_version,
      run?.evaluation_profile?.complete === false && '조건 기록 불완전'].filter(Boolean).join(' · ') || '평가 조건 미기록';
  }
  function completionNote(run) {
    const summary = run?.evaluation_summary;
    return summary && ((summary.incomplete || 0) + (summary.review_required || 0)) ?
      `미완료 ${number(summary.incomplete)} · 확인 필요 ${number(summary.review_required)}` : '';
  }
  function releases() {
    const items = [...(state.history?.releases || [])];
    for (const run of state.history?.runs || []) {
      if (run.aiona_release && !items.some((r) => r.version === run.aiona_release.version)) items.push(run.aiona_release);
    }
    return items;
  }
  const runsFor = (version) => (state.history?.runs || []).filter((run) => run.aiona_release?.version === version);
  function modelOptions() {
    return releases().map((release) => {
      const runs = runsFor(release.version), name = 'AIONA ' + release.version;
      if (!runs.length) return node('option', {value: 'version:' + release.version, text: name + ' · 평가 결과 없음'});
      if (runs.length === 1) return node('option', {value: runs[0].run_id, text: name});
      // Each option identifies one saved evaluation. Multiple records never mean an arbitrary latest result.
      return node('optgroup', {label: name + ' · 평가 기록 ' + number(runs.length) + '개'},
        node('option', {value: 'version:' + release.version, disabled: '', text: name + ' · 평가 기록을 선택하세요'}),
        ...runs.map((run) => node('option', {value: run.run_id,
          text: `${name} · ${run.name || run.run_id} · ${time(run.created_at)} · ${number(run.question_count)}문항 · ${statusText(run)}`})));
    });
  }
  function readModelSelection(side) {
    const value = $('version-' + side + '-model').value;
    const run = (state.history?.runs || []).find((item) => item.run_id === value);
    const version = run?.aiona_release?.version || (value.startsWith('version:') ? value.slice(8) : '');
    $('version-' + side + '-version').value = version;
    $('version-' + side + '-run').value = run?.run_id || '';
    const description = $('version-' + side + '-selection');
    description.textContent = run ? `${run.name || run.run_id} · ${time(run.created_at)} · ${number(run.question_count)}문항 · ${statusText(run)}${completionNote(run) ? ' · ' + completionNote(run) : ''}` :
      version && runsFor(version).length > 1 ? '이 모델의 평가 기록이 여러 개입니다. 위 목록에서 비교할 기록을 선택하세요.' :
      version ? '아직 저장된 평가 결과가 없습니다.' : '';
    description.hidden = !description.textContent;
    description.title = run ? conditionSummary(run) : '';
  }
  function populateSelectors() {
    const runs = state.history?.runs || [], saved = state.selection || {};
    for (const side of ['baseline', 'target']) {
      const explicitRun = runs.find((run) => run.run_id === (side === 'target' && state.requestedRun || saved[side]));
      const selectedVersion = explicitRun?.aiona_release?.version || saved[side + 'Version'] || '';
      const select = $('version-' + side + '-model');
      select.replaceChildren(node('option', {value: '', text: (side === 'baseline' ? '기준' : '비교') + ' 모델 선택'}), ...modelOptions());
      const available = runsFor(selectedVersion);
      const choice = explicitRun?.run_id || (!saved[side] && available.length === 1 ? available[0].run_id : selectedVersion ? 'version:' + selectedVersion : '');
      select.value = [...select.options].some((option) => option.value === choice) ? choice : '';
      readModelSelection(side);
    }
    state.requestedRun = null;
    if (saved.changeFilter) $('version-change-filter').value = saved.changeFilter;
  }
  async function refresh() {
    const generation = ++state.historyGeneration;
    $('version-refresh').disabled = true;
    if ($('comparison-refresh')) $('comparison-refresh').disabled = true;
    $('model-release-list').setAttribute('aria-busy', 'true'); error(null);
    if (!state.history) $('model-release-list').replaceChildren(node('tr', {}, node('td', {colspan: '7', text: '모델 버전과 평가 이력을 불러오고 있습니다.'})));
    try {
      const history = await api('/api/comparison/version-history');
      if (generation !== state.historyGeneration) return;
      state.history = history;
      renderReleases();
      window.dispatchEvent(new CustomEvent('aiona:model-version-changed', {detail: history}));
      populateSelectors();
      await compareSelected();
    } catch (err) {
      if (generation === state.historyGeneration) {
        error(err.status === 404 ? '버전 비교를 지원하는 서버가 필요합니다. 서버를 재시작해 주세요.' : err.message);
        if (!state.history) $('model-release-list').replaceChildren(node('tr', {}, node('td', {colspan: '7', text: '버전 목록을 읽지 못했습니다. 기록 새로고침으로 다시 시도하세요.'})));
      }
    } finally {
      if (generation === state.historyGeneration) {
        $('version-refresh').disabled = false; $('model-release-list').setAttribute('aria-busy', 'false');
        if ($('comparison-refresh')) $('comparison-refresh').disabled = false;
      }
    }
  }
  function renderReleases() {
    const history = state.history || {}, runs = history.runs || [];
    const items = releases(), current = history.current_release?.version;
    const latest = runsFor(current).slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
    if ($('model-default-version')) $('model-default-version').textContent = current ? 'AIONA ' + current : '선택 안 됨';
    if ($('model-release-count')) $('model-release-count').textContent = number((history.releases || []).length);
    if ($('model-latest-evaluation')) $('model-latest-evaluation').replaceChildren(node('strong', {text: statusText(latest)}), node('span', {class: 'field-help', text: latest ? time(latest.created_at) + (completionNote(latest) ? ' · ' + completionNote(latest) : '') : current ? '이 버전의 평가를 시작하면 표시됩니다.' : '기본 평가 버전을 선택하세요.'}));
    $('model-release-list').replaceChildren(...items.map((release) => {
      const active = release.version === history.current_release?.version;
      const registered = (history.releases || []).some((item) => item.version === release.version);
      const saved = runsFor(release.version).slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))), recent = saved[0];
      const actions = node('div', {class: 'release-actions'},
        node('button', {class: 'text-button', text: '상세 보기', onclick: () => openRelease(release)}),
        node('button', {class: 'text-button', text: '평가 이력', onclick: () => window.dispatchEvent(new CustomEvent('aiona:history-version', {detail: {version: release.version}}))}),
        node('button', {class: 'text-button', text: '비교 실행 선택', onclick: () => selectVersion(release.version)}));
      if (!registered) actions.append(node('button', {class: 'button secondary compact', text: '버전 등록', onclick: () => showRegistration(release, true)}));
      else if (!active) actions.append(node('button', {class: 'button secondary compact', text: '기본 평가 버전으로 선택', onclick: () => confirmDefault(release)}));
      return node('tr', {'data-model-version': release.version, 'data-current': String(active)},
        node('th', {scope: 'row'}, node('strong', {text: 'AIONA ' + release.version}), !registered ? node('small', {class: 'field-help', text: '실행에만 기록됨'}) : null),
        node('td', {}, node('span', {class: 'release-note', text: release.notes || '변경 설명 없음'})),
        node('td', {text: registered ? time(release.registered_at) : '등록 정보 없음'}), node('td', {}, active ? pill('기본 평가 버전', 'purple') : node('span', {text: '—'})),
        node('td', {text: number(saved.length) + '건'}), node('td', {}, pill(statusText(recent), statusKind(recent)), recent ? node('small', {class: 'field-help', text: time(recent.created_at)}) : null, completionNote(recent) ? node('small', {class: 'field-help', text: completionNote(recent)}) : null), node('td', {}, actions));
    }));
    if (!items.length) $('model-release-list').append(node('tr', {}, node('td', {colspan: '7', class: 'release-empty', text: '등록된 모델 버전이 없습니다. 새 버전 등록으로 시작하세요.'})));
  }
  function selectVersion(version) {
    const side = state.selection?.baseline ? 'target' : 'baseline';
    saveSelection({...state.selection, [side + 'Version']: version, [side]: ''});
    $('version-comparison-details').open = true; W.go('comparison');
  }
  function dialog(id, title) {
    const content = node('div', {class: 'version-detail-content'});
    const element = node('dialog', {id, class: 'modal version-detail-modal', 'aria-labelledby': id + '-title'},
      node('div', {class: 'modal-heading'}, node('h2', {id: id + '-title', text: title}), node('button', {type: 'button', class: 'icon-button', 'aria-label': '닫기', text: '×', onclick: () => element.close()})), content);
    document.body.append(element);
    let opener;
    element.addEventListener('close', () => {
      if (document.querySelector('dialog[open]')) return;
      const fallback = $('view-versions')?.hidden ? $('comparison-refresh') : $('version-register-next');
      const target = opener?.isConnected && opener.getClientRects().length ? opener : fallback;
      if (target?.getClientRects().length) target.focus({preventScroll: true});
    });
    return {element, content, show: () => { opener = document.activeElement; element.showModal(); }};
  }
  const releaseDetail = dialog('model-release-detail', '모델 버전 상세');
  async function openRelease(release) {
    const generation = ++state.detailGeneration, saved = runsFor(release.version);
    $('model-release-detail-title').textContent = 'AIONA ' + release.version;
    const activity = node('div', {'aria-live': 'polite', text: '관련 작업 이력을 불러오고 있습니다.'});
    const table = node('table', {class: 'data-table'}, node('thead', {}, node('tr', {}, ...['평가 실행', '평가 조건', '시각 · 문항', '상태', '작업'].map((text) => node('th', {text})))),
      node('tbody', {}, ...saved.map((run) => node('tr', {}, node('th', {scope: 'row', text: run.name || run.run_id}), node('td', {text: conditionSummary(run)}), node('td', {text: time(run.created_at) + ' · ' + number(run.question_count) + '문항'}), node('td', {}, pill(statusText(run), statusKind(run)), node('small', {class: 'field-help', text: completionNote(run)})), node('td', {}, node('button', {class: 'text-button', text: '상세 보기', onclick: () => { releaseDetail.element.close(); if (window.AionaHistory?.openRunDetail) window.AionaHistory.openRunDetail(run.run_id); else W.openRun(run.run_id); }}))))));
    releaseDetail.content.replaceChildren(
      node('p', {class: 'modal-description', text: release.notes || '변경 설명이 없습니다.'}),
      node('dl', {class: 'version-metadata'}, node('dt', {text: '등록 시각'}), node('dd', {text: time(release.registered_at)}), node('dt', {text: '기본 평가 버전'}), node('dd', {text: release.version === state.history?.current_release?.version ? '선택됨 · 이후 새 평가의 기본값' : '선택 안 됨'})),
      node('section', {}, node('h3', {text: '연결된 평가 실행 ' + number(saved.length) + '건'}), node('p', {class: 'field-help', text: '실행별 평가 조건을 확인하세요. 서로 다른 조건의 점수를 하나로 합치지 않습니다.'}), saved.length ? node('div', {class: 'table-scroll'}, table) : node('p', {class: 'field-help', text: '이 버전의 평가 실행이 없습니다.'})),
      node('section', {}, node('h3', {text: '관련 작업 이력'}), activity, node('button', {class: 'text-button', text: '작업 이력 전체 보기', onclick: () => { releaseDetail.element.close(); window.dispatchEvent(new CustomEvent('aiona:history-activity', {detail: {version: release.version}})); }})));
    releaseDetail.show();
    const loadActivity = async () => {
      activity.textContent = '관련 작업 이력을 불러오고 있습니다.';
      try {
        const response = await api('/api/comparison/activity-history?version=' + encodeURIComponent(release.version));
        if (generation !== state.detailGeneration) return;
        const types = {model_version_registered: '모델 버전 등록', default_version_changed: '기본 평가 버전 변경', evaluation_started: '평가 시작', evaluation_completed: '평가 종료', evaluation_failed: '평가 실패', evaluation_cancelled: '평가 취소', reevaluation_created: '재평가 실행 생성'};
        const events = response.events || [];
        activity.replaceChildren(events.length ? node('ul', {class: 'activity-list'}, ...events.slice(0, 10).map((event) => node('li', {},
          node('strong', {text: types[event.event_type] || event.event_type}), node('span', {class: 'field-help', text: time(event.timestamp) + ' · ' + (event.actor?.label || '미기록')}),
          event.reason ? node('p', {text: event.reason}) : null, event.error_summary ? node('p', {text: event.error_summary}) : null))) : node('p', {class: 'field-help', text: '저장된 작업 이력이 없습니다. 과거 작업자와 변경 사유는 추정하지 않습니다.'}));
      } catch (err) {
        if (generation === state.detailGeneration) activity.replaceChildren(node('p', {class: 'alert error', role: 'alert', text: err.message}), node('button', {class: 'button secondary', text: '작업 이력 다시 조회', onclick: loadActivity}));
      }
    };
    await loadActivity();
  }
  const defaultDialog = dialog('model-default-dialog', '기본 평가 버전 변경');
  function confirmDefault(release) {
    const previous = state.history?.current_release?.version || null;
    const reason = node('textarea', {id: 'model-default-reason', rows: '3', maxlength: '2000', required: '', placeholder: '기본 평가 버전을 변경하는 이유를 입력하세요.'});
    const failure = node('div', {class: 'alert error', role: 'alert', hidden: ''});
    const submit = node('button', {type: 'submit', id: 'model-default-confirm', class: 'button primary', text: '기본 평가 버전 변경'});
    const form = node('form', {}, node('p', {class: 'modal-description', text: '새 평가에 기본으로 기록할 모델 버전을 변경합니다. 저장된 평가 실행의 버전과 결과는 유지됩니다.'}),
      node('dl', {class: 'version-metadata'}, node('dt', {text: '변경 전'}), node('dd', {text: previous ? 'AIONA ' + previous : '선택 안 됨'}), node('dt', {text: '변경 후'}), node('dd', {text: 'AIONA ' + release.version})),
      node('label', {for: 'model-default-reason'}, '변경 사유', reason), failure,
      node('div', {class: 'modal-footer'}, node('button', {type: 'button', class: 'button secondary', text: '취소', onclick: () => defaultDialog.element.close()}), submit));
    form.addEventListener('submit', async (event) => {
      event.preventDefault(); if (submit.disabled) return;
      if (!reason.value.trim()) { reason.setCustomValidity('변경 사유를 입력하세요.'); reason.reportValidity(); return; }
      submit.disabled = true; submit.textContent = '저장 중…'; failure.hidden = true;
      try {
        await api('/api/comparison/model-versions', {action: 'activate', version: release.version, reason: reason.value.trim(), expected_current_version: previous});
        // Native close restores focus asynchronously. Finish that before replacing the row.
        if (defaultDialog.element.open) {
          const closed = new Promise((resolve) => defaultDialog.element.addEventListener('close', resolve, {once: true}));
          defaultDialog.element.close(); await closed;
        }
        await refresh();
        const updatedRow = [...$('model-release-list').querySelectorAll('[data-model-version]')].find((row) => row.dataset.modelVersion === release.version);
        if (!$('view-versions').hidden) (updatedRow?.querySelector('button') || $('version-register-next')).focus({preventScroll: true});
        window.dispatchEvent(new CustomEvent('aiona:history-changed'));
      } catch (err) { failure.textContent = err.message; failure.hidden = false; }
      finally { submit.disabled = false; submit.textContent = '기본 평가 버전 변경'; }
    });
    reason.addEventListener('input', () => reason.setCustomValidity(''));
    defaultDialog.content.replaceChildren(form); defaultDialog.show(); reason.focus();
  }
  async function compareSelected() {
    const generation = ++state.generation;
    const beforeId = $('version-baseline-run').value, afterId = $('version-target-run').value;
    saveSelection({...state.selection, baseline: beforeId, target: afterId, baselineVersion: $('version-baseline-version').value, targetVersion: $('version-target-version').value});
    state.before = null; state.after = null; state.comparison = null; state.rows = [];
    error(null); render();
    $('version-summary').setAttribute('aria-busy', 'true');
    $('version-summary-description').textContent = beforeId || afterId ? '저장된 원문과 평가 결과를 읽고 있습니다.' : '등록된 원문의 평가 실행을 선택해 주세요.';
    try {
      const [before, after] = await Promise.all([beforeId ? api(`/api/comparison/runs/${encodeURIComponent(beforeId)}/result`) : null, afterId ? api(`/api/comparison/runs/${encodeURIComponent(afterId)}/result`) : null]);
      if (generation !== state.generation) return;
      state.before = before; state.after = after;
      state.comparison = before && after ? window.AionaVersionComparison.compare(before, after) : null;
      state.statistics = null;
      render();
      if (before && after) {
        // 서버 계산 비교: 대화 단위 부호검정 p값·꼬리 표본·질문→기준→세부→항목 변화 (화면 집계는 바꾸지 않는다)
        try {
          const statistics = await api(`/api/comparison/version-comparison?baseline=${encodeURIComponent(before.run_id)}&target=${encodeURIComponent(after.run_id)}`);
          if (generation !== state.generation) return;
          state.statistics = statistics;
          renderStatistics();
        } catch (err) { if (generation === state.generation) { state.statistics = {error: err.message}; renderStatistics(); } }
      }
    } catch (err) { if (generation === state.generation) error(err.message); }
    finally { if (generation === state.generation) $('version-summary').setAttribute('aria-busy', 'false'); }
  }
  function modelSummary(run) {
    return run ? window.AionaVersionComparison.compare(run, null).baseline : null;
  }
  function scoreCard(run, summary, fallback, side) {
    const rate = summary?.ready ? summary.rate : null;
    const registered = state.history?.runs?.some((r) => side === 'baseline' || r.aiona_release?.version !== state.before?.aiona_release?.version);
    const notice = window.AionaResults.evidenceNotice(run);
    return node('article', {class: 'summary-score-card vc-score-card' + (side === 'target' ? ' is-target' : ''), 'data-version-side': side},
      node('div', {class: 'summary-score-heading'}, node('span', {class: 'summary-rank', text: side === 'baseline' ? '기준 버전' : '비교 버전'}), node('h3', {text: releaseName(run, fallback)})),
      node('p', {class: 'summary-model-id', text: run ? `${run.name || run.run_id} · ${number(summary.total)}문항 · ${time(run.created_at)}` : registered ? '실행 미선택' : '자료 미등록'}),
      node('div', {class: 'summary-score'}, node('strong', {text: rate === null || rate === undefined ? '—' : (rate * 100).toFixed(1)}), node('span', {text: rate === null || rate === undefined ? run ? '점수 미확정' : '평가 대기' : '/ 100점'})),
      node('p', {class: 'summary-score-counts', text: run ? `PASS ${number(summary.pass)} · FAIL ${number(summary.fail)} · 미완료 ${number(summary.pending)}` : registered ? '위 목록에서 비교할 실행을 선택해 주세요.' : '이 버전의 원문과 평가 결과가 필요합니다.'}),
      notice ? node('p', {class: 'field-help', 'data-evidence-notice': '', text: notice}) : null);
  }
  function render() {
    const c = state.comparison;
    const ready = state.before?.status === 'COMPLETED' && state.before?.aiona_release?.version;
    $('version-download-questions').disabled = !ready;
    $('version-add-answers').disabled = !ready;
    $('version-registration-status').textContent = ready ? `${releaseName(state.before)} · ${number(state.before.question_count)}개 질문을 그대로 사용합니다. 새 답변 평가는 새 실행을 만들고 평가자를 호출합니다.` : '평가가 종료된 기준 기록을 선택하면 동일 질문으로 평가를 추가할 수 있습니다.';
    const targetVersion = $('version-target-version').value;
    if ($('version-empty-target')) {
      $('version-empty-target').hidden = !targetVersion || runsFor(targetVersion).length > 0;
      $('version-empty-target').textContent = '이 버전의 평가 결과가 없습니다. 기준 실행과 동일한 질문으로 평가를 추가하세요.';
    }
    const cards = [scoreCard(state.before, c?.baseline || modelSummary(state.before), '기준 실행', 'baseline')];
    if (state.after) cards.push(scoreCard(state.after, c?.target || modelSummary(state.after), '비교 대상', 'target'));
    $('version-score-cards').replaceChildren(...cards);
    $('version-summary').dataset.ready = String(Boolean(c?.eligible));
    const trend = c?.eligible ? c.delta.rate > 0 ? 'improved' : c.delta.rate < 0 ? 'regressed' : 'unchanged' : 'unresolved';
    $('version-summary').dataset.trend = trend;
    $('version-comparison-status').textContent = c?.eligible ? '동일 조건 비교 완료' : c ? '전체 비교 미확정' : '비교 대상 대기';
    $('version-comparison-status').className = 'pill ' + (trend === 'improved' ? 'success' : trend === 'regressed' ? 'danger' : c && !c.eligible ? 'warning' : 'neutral');
    $('version-summary-title').textContent = c?.eligible ?
      trend === 'improved' ? `${releaseName(state.after)}에서 통과 문항이 ${number(c.delta.pass)}개 늘었습니다.` :
      trend === 'regressed' ? `${releaseName(state.after)}에서 통과 문항이 ${number(-c.delta.pass)}개 줄었습니다.` :
      `${releaseName(state.after)}의 통과 문항 수는 기준 버전과 같습니다.` : c ? '전체 개선 폭을 아직 확정할 수 없습니다.' : '비교할 두 모델을 선택하세요.';
    const verifiedCount = c?.rows?.filter((row) => row.verified).length || 0;
    $('version-summary-description').textContent = c?.eligible ? `${releaseName(state.before)} 대비 동일한 ${number(verifiedCount)}문항의 결과입니다. ${number(c.counts.improved)}문항이 FAIL → PASS, ${number(c.counts.regressed)}문항이 PASS → FAIL로 바뀌었습니다.` : c ? `전체 점수 차이는 미확정입니다. ${verifiedCount ? `아래 개선·악화는 같은 조건으로 판정된 공통 ${number(verifiedCount)}문항에 한정합니다.` : '문항 구성·평가 조건·완료 상태를 확인해 주세요.'}` : !state.before ? '기준 모델과 비교 모델을 하나씩 선택하세요. 저장된 평가 결과로 개선 폭과 근거를 확인합니다.' : '비교 모델을 선택하면 개선된 기준과 답변을 확인할 수 있습니다.';
    renderImpact(c, trend, verifiedCount);
    $('version-reasons').hidden = !c?.reasons?.length;
    $('version-reasons').replaceChildren(...(c?.reasons || []).map((reason) => node('p', {text: reason.label || reason.code})));
    if (c?.reasons?.some((r) => ['profile_mismatch', 'profile_unknown'].includes(r.code)) && state.before?.status === 'COMPLETED') {
      $('version-reasons').append(node('button', {class: 'button secondary', text: '기준 답변도 현재 설정으로 새 평가',
        disabled: state.submitting || W.isBusy() || W.canEvaluate?.() === false ? '' : null,
        onclick: (event) => reevaluate(state.before, event.currentTarget, 'baseline')}), node('p', {class: 'field-help', text: '새 실행을 만들고 평가자를 호출합니다. 원본 실행과 결과는 유지됩니다.'}));
    }
    renderProfiles();
    renderStatistics();
    $('version-change-panel').hidden = !c; $('version-criteria-panel').hidden = !c;
    if (!c) { $('version-rows').replaceChildren(); renderHighlights(); return; }
    const counts = c.counts || {};
    const countGroups = [['improved', '개선', counts.improved], ['regressed', '악화', counts.regressed], ['passMaintained', '통과 유지', counts.passMaintained], ['failMaintained', '실패 유지', counts.failMaintained], ['pending', '미완료', counts.pending], ['unverified', '조건 확인 필요', counts.unverified], ['unmatched', '문항 불일치', (counts.unmatchedBaseline || 0) + (counts.unmatchedTarget || 0) - (counts.changed || 0)]];
    $('version-change-counts').replaceChildren(...countGroups.map(([filter, label, count]) => node('button', {class: 'outcome-count ' + ({improved: 'clear', regressed: 'issue', pending: 'pending'}[filter] || ''), onclick: () => { $('version-change-filter').value = filter; renderRows(); }}, node('strong', {text: number(count)}), node('span', {text: label}))));
    const changedBefore = new Set((c.changed || []).map((row) => row.baselineIndex)), changedAfter = new Set((c.changed || []).map((row) => row.targetIndex));
    state.rows = [...(c.rows || []).map((row) => ({...row, label: row.changeLabel || labels[row.change]})),
      ...(c.changed || []).map((row) => ({...row, change: 'unmatched', label: '문항 변경 · ' + row.reason})),
      ...(c.unmatched?.baseline || []).filter((row) => !changedBefore.has(row.index)).map((row) => ({caseId: row.caseId, baselineIndex: row.index, targetIndex: null, change: 'unmatched', label: '기준 문항 · ' + (row.reason || '대응 문항 없음')})),
      ...(c.unmatched?.target || []).filter((row) => !changedAfter.has(row.index)).map((row) => ({caseId: row.caseId, baselineIndex: null, targetIndex: row.index, change: 'unmatched', label: '대상 문항 · ' + (row.reason || '대응 문항 없음')}))];
    renderRows(); renderCriteria(); renderHighlights();
  }
  function renderStatistics() {
    const box = $('version-statistics');
    if (!box) return;
    const s = state.statistics;
    box.hidden = !s || !state.before || !state.after;
    if (box.hidden) { box.replaceChildren(); return; }
    if (s.error) { box.replaceChildren(node('p', {class: 'field-help', text: '서버 비교를 불러오지 못했습니다: ' + s.error})); return; }
    const sign = s.sign_test || {}, tail = s.tail || {}, verdict = s.verdict || {}, conditions = s.conditions || {};
    const verdictLabel = {improved: '개선', regressed: '악화', uncertain: '우연과 구분되지 않음', not_comparable: '비교 조건 불일치'}[verdict.label] || '—';
    const children = [
      node('p', {class: 'vc-statistics-verdict', 'data-verdict': verdict.label || ''}, node('strong', {text: `통계 판정: ${verdictLabel}`}), ' · ', verdict.text || ''),
      node('p', {class: 'field-help', text: `대화 단위 부호검정 · 개선 ${number(sign.plus || 0)} · 악화 ${number(sign.minus || 0)} · 동점 ${number(sign.ties || 0)}` +
        (sign.incomplete ? ` · 미완료 ${number(sign.incomplete)}` : '') + ` · p값 ${sign.p_value === null || sign.p_value === undefined ? '—' : sign.p_value}` +
        ` · 꼬리(기준 실행 최악 ${number(tail.size || 0)}문항): 개선 ${number(tail.improved || 0)} · 악화 ${number(tail.regressed || 0)}${tail.worsened ? ' · 꼬리 악화' : ''}`})];
    if (Array.isArray(conditions.differences) && conditions.differences.length) {
      children.push(node('details', {class: 'version-profile-raw', open: ''}, node('summary', {text: `평가 조건 차이 ${number(conditions.differences.length)}개`}),
        node('ul', {class: 'finding-list'}, ...conditions.differences.map((d) => node('li', {}, node('strong', {text: d.label}), ` 기준 ${detailValue(d.baseline)} → 비교 ${detailValue(d.target)}`))),
        conditions.suggestion ? node('p', {class: 'field-help', text: conditions.suggestion}) : null));
    }
    const table = (s.criteria || []).filter((row) => row.level !== 'code' && (row.improved || row.regressed));
    if (table.length) {
      children.push(node('details', {class: 'version-profile-raw'}, node('summary', {text: `기준 → 세부 기준 → 항목 변화 ${number(table.length)}개`}),
        node('table', {class: 'version-criteria-table'}, node('thead', {}, node('tr', {}, ...['단위', 'ID', '개선', '악화', '유지'].map((h) => node('th', {text: h})))),
          node('tbody', {}, ...table.map((row) => node('tr', {}, node('td', {text: {criterion: '기준', sub: '세부 기준', item: '항목'}[row.level] || row.level}),
            node('td', {text: row.id}), node('td', {text: number(row.improved)}), node('td', {text: number(row.regressed)}), node('td', {text: number((row.pass_kept || 0) + (row.fail_kept || 0))})))))));
    }
    const regressions = s.regressions || [];
    if (regressions.length) {
      children.push(node('details', {class: 'version-profile-raw'}, node('summary', {text: `악화 질문 ${number(regressions.length)}개 (두 버전 답변과 근거)`}),
        ...regressions.slice(0, 20).map((r) => node('div', {class: 'jev-reason'}, node('p', {}, node('strong', {text: r.case_id}), ' ', r.question || ''),
          node('p', {class: 'field-help', text: '기준 답변: ' + (r.baseline_answer || '')}), node('p', {class: 'field-help', text: '비교 답변: ' + (r.target_answer || '')}),
          node('ul', {class: 'finding-list'}, ...(r.target_evidence || []).map((e) => node('li', {}, `${e.id} ${e.title || ''} · ${e.reason || ''}` +
            ((e.items || []).length ? ' · ' + e.items.map((it) => `${it.id} ${it.answer_quote ? '「' + it.answer_quote + '」' : ''}${it.unverifiable ? '(확인 불가)' : ''}`).join(', ') : ''))))))));
    }
    box.replaceChildren(...children);
  }
  function focusChanges(filter) {
    $('version-change-filter').value = filter;
    renderRows();
    scrollToPanel('version-change-panel');
    $('version-change-filter').focus({preventScroll: true});
  }
  function scrollToPanel(id) {
    $(id).scrollIntoView({behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start'});
  }
  function renderImpact(c, trend, verifiedCount) {
    const value = c?.eligible ? signed(c.delta.rate) : '—';
    if ($('version-delta')) $('version-delta').hidden = !c;
    $('version-delta')?.replaceChildren(
      node('span', {class: 'vc-delta-label', text: '질문 통과점수 변화'}),
      node('div', {}, node('strong', {class: 'vc-delta-value', text: value}), c?.eligible ? node('span', {class: 'vc-delta-unit', text: '점'}) : null),
      node('span', {class: 'vc-delta-context', text: c?.eligible ? trend === 'improved' ? '기준 버전보다 상승' : trend === 'regressed' ? '기준 버전보다 하락' : '기준 버전과 동일' : c ? '전체 비교 미확정' : '두 모델 선택 후 표시'}));
    const counts = c?.counts || {};
    const unresolved = (counts.pending || 0) + (counts.unverified || 0) + (counts.unmatchedBaseline || 0) + (counts.unmatchedTarget || 0) - (counts.changed || 0);
    const scoped = c && !c.eligible && verifiedCount > 0;
    const metrics = [['improved', scoped ? '공통 문항 내 개선' : '개선 문항', counts.improved, 'FAIL → PASS'],
      ['regressed', scoped ? '공통 문항 내 악화' : '악화 문항', counts.regressed, 'PASS → FAIL'],
      ['unresolved', '미확정 항목', unresolved, '미완료 · 조건 차이 · 문항 불일치']];
    const container = $('version-impact-counts');
    if (!container) return;
    container.hidden = !c;
    container.replaceChildren(...metrics.map(([filter, label, count, detail]) => node('button', {
      type: 'button', class: 'vc-impact-card is-' + filter, 'data-filter': filter,
      'aria-pressed': String($('version-change-filter').value === filter),
      disabled: !c || !count ? '' : null, onclick: () => focusChanges(filter)},
      node('span', {text: label}), node('strong', {text: number(count)}), node('small', {text: detail}))));
  }
  const excerpt = (value, limit = 160) => {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    return text.length > limit ? text.slice(0, limit - 1) + '…' : text;
  };
  function pairedFailureEvidence(row) {
    const oldBatch = state.before?.evaluations?.aiona, nextBatch = state.after?.evaluations?.aiona;
    const oldRecord = oldBatch?.jev_eval?.records?.[row.baselineIndex], nextRecord = nextBatch?.jev_eval?.records?.[row.targetIndex];
    const pairs = [];
    for (const [id, decision] of Object.entries(oldRecord?.decisions || {})) {
      if (oldRecord?.status !== 'SUCCESS' || !(oldRecord.criteria || []).includes(id) || decision.decision !== 'FAIL') continue;
      const target = nextRecord?.decisions?.[id];
      if (nextRecord?.status !== 'SUCCESS' || !(nextRecord.criteria || []).includes(id) || target?.decision !== 'PASS') continue;
      const name = oldBatch.jev_eval.criteria?.find((item) => item.id === id)?.name || window.AionaResults.comparisonCriteria.find((item) => item.id === id)?.name || id;
      pairs.push({id, name, before: decision.reason || 'FAIL로 저장되어 있으며 별도 사유는 없습니다.', after: target.reason || 'PASS로 저장되어 있으며 별도 사유는 없습니다.'});
    }
    const oldCode = window.AionaResults.resolvedCase(oldBatch?.cases?.[row.baselineIndex], oldRecord);
    const nextCode = window.AionaResults.resolvedCase(nextBatch?.cases?.[row.targetIndex], nextRecord);
    for (const fail of (oldCode?.results || []).filter((item) => item.area === 'common' && item.counted && item.verdict === 'FAIL')) {
      const id = fail.condition_id || fail.check_id;
      if (!id) continue;
      const pass = (nextCode?.results || []).find((item) => item.area === 'common' && item.counted && item.verdict === 'PASS' &&
        (item.condition_id || item.check_id) === id && String(item.category) === String(fail.category) &&
        JSON.stringify(item.required ?? null) === JSON.stringify(fail.required ?? null) && JSON.stringify(item.scope ?? null) === JSON.stringify(fail.scope ?? null));
      if (pass) pairs.push({id, name: fail.title || fail.check_id || id,
        before: fail.explain?.summary || fail.reason || '이 공통 조건을 충족하지 못했습니다.',
        after: pass.explain?.summary || pass.reason || '같은 공통 조건을 PASS로 판정했습니다.'});
    }
    return pairs.slice(0, 2);
  }
  function firstFailureReason(run, index) {
    const batch = run?.evaluations?.aiona, record = batch?.jev_eval?.records?.[index];
    const resolved = window.AionaResults.resolvedCase(batch?.cases?.[index], record);
    const failure = (resolved?.results || []).find((item) => item.area === 'common' && item.counted && item.verdict === 'FAIL');
    if (failure) return `${failure.title || failure.check_id || '공통 조건'} · ${failure.explain?.summary || failure.reason || 'FAIL로 저장되어 있습니다.'}`;
    if (record?.status !== 'SUCCESS') return '';
    const semantic = Object.entries(record.decisions || {}).find(([id, decision]) => (record.criteria || []).includes(id) && decision.decision === 'FAIL');
    return semantic ? `${semantic[0]} · ${semantic[1].reason || 'FAIL로 저장되어 있습니다.'}` : '';
  }
  function renderHighlights() {
    const c = state.comparison, criteriaSection = $('version-highlights'), answersSection = $('version-answer-highlights');
    if (!criteriaSection || !answersSection) return;
    criteriaSection.hidden = !c; answersSection.hidden = !c;
    $('version-criteria-highlights').replaceChildren(); $('version-improved-answers').replaceChildren();
    $('version-show-improved').hidden = true;
    if (!c) return;
    const comparable = (c.criteria || []).filter((criterion) => criterion.comparable && Number.isFinite(criterion.delta) && criterion.delta !== 0);
    const positive = comparable.filter((criterion) => criterion.delta > 0).sort((a, b) => b.delta - a.delta);
    const negative = comparable.filter((criterion) => criterion.delta < 0).sort((a, b) => a.delta - b.delta);
    const highlights = [...positive.slice(0, 2), ...negative.slice(0, 2)];
    $('version-highlight-description').textContent = highlights.length ? c.eligible ?
      '같은 문항·적용 조건에서 변화가 큰 평가 기준입니다.' : '동일하게 연결된 공통 문항의 확정된 기준 변화입니다. 전체 결과의 개선 폭은 아닙니다.' :
      c.eligible ? '확정된 기준별 통과율 변화가 없습니다. 전체 판정은 아래 표에서 확인하세요.' : '평가 조건과 미판정 항목을 확인한 뒤 기준별 변화가 표시됩니다.';
    $('version-criteria-highlights').replaceChildren(...highlights.map((criterion) => node('article', {
      class: 'vc-criterion-card is-' + (criterion.delta > 0 ? 'improved' : 'regressed'), 'data-criterion': criterion.id, 'data-change': criterion.delta > 0 ? 'improved' : 'regressed'},
      node('span', {class: 'vc-criterion-code', text: criterion.group === 'code' ? 'CODE ' + criterion.id : criterion.id}),
      node('h3', {text: criterion.name}), node('strong', {class: 'vc-criterion-delta', text: signed(criterion.delta, '%p')}),
      node('p', {class: 'vc-criterion-range', text: `${percent(criterion.baseline.rate)} → ${percent(criterion.target.rate)}`}),
      node('small', {text: `공통 ${number(criterion.matchedQuestions)}문항 · ${criterion.unit} ${number(criterion.target.targets)}개 판정`}),
      node('div', {class: 'vc-criterion-bars'}, ...[['이전', criterion.baseline.rate], ['이후', criterion.target.rate]].map(([label, rate]) => {
        const fill = node('span'); fill.style.width = `${rate * 100}%`;
        return node('div', {class: 'vc-criterion-bar'}, node('span', {text: label}), node('div', {class: 'vc-bar-track', 'aria-hidden': 'true'}, fill), node('span', {text: percent(rate)}));
      })))));
    if (!highlights.length) $('version-criteria-highlights').append(node('p', {class: 'vc-empty', text: c.eligible ? '기준별 통과율의 확정된 증감이 없습니다.' : '비교 가능한 기준이 확인되면 이전·이후 통과율을 표시합니다.'}));
    const improved = (c.rows || []).filter((row) => row.verified && row.change === 'improved');
    $('version-answer-highlight-description').textContent = improved.length ?
      `${c.eligible ? '' : '같은 조건으로 판정된 공통 문항 중 '}FAIL → PASS로 바뀐 ${number(improved.length)}문항${improved.length > 3 ? ' 중 3문항' : ''}입니다. 이전 실패 항목의 근거를 나란히 확인하세요.` :
      c.eligible ? '이번 비교에서 FAIL → PASS로 바뀐 문항은 없습니다.' : '동일 조건에서 개선이 확인된 문항이 아직 없습니다.';
    $('version-improved-answers').replaceChildren(...improved.slice(0, 3).map((row) => {
      const before = original(state.before, row.baselineIndex), after = original(state.after, row.targetIndex), pairs = pairedFailureEvidence(row);
      const fallback = firstFailureReason(state.before, row.baselineIndex);
      return node('article', {class: 'vc-answer-card', 'data-case-id': row.caseId, 'data-answer-changed': String(row.answerChanged)},
        node('div', {class: 'vc-answer-heading'}, node('span', {text: row.caseId || '문항'}), pill('FAIL → PASS', 'success')),
        node('h3', {class: 'vc-answer-question', text: excerpt(before?.question || after?.question, 150)}),
        !row.answerChanged ? node('p', {class: 'vc-answer-note', text: '답변 원문은 동일합니다. 저장된 판정이 FAIL → PASS로 달라졌습니다.'}) : null,
        node('div', {class: 'vc-answer-pair'}, ...[['before', state.before, before, 'FAIL'], ['after', state.after, after, 'PASS']].map(([side, run, source, verdict]) =>
          node('section', {class: 'vc-answer-' + side},
            node('span', {class: 'vc-answer-evidence-label', text: releaseName(run) + ' · ' + verdict}),
            row.answerChanged ? node('p', {class: 'vc-answer-excerpt', text: excerpt(source?.answer, 140)}) : null,
            ...(pairs.length ? pairs.map((pair) => node('p', {class: 'vc-answer-reason', 'data-evidence-criterion': pair.id},
              node('strong', {text: pair.name + ' · '}), excerpt(pair[side], 170))) : [node('p', {class: 'vc-answer-reason', text: side === 'before' ? excerpt(fallback || '공통 기준에서 FAIL로 판정되었습니다.', 180) : '이전 실패 항목에 대응하는 개별 PASS 근거가 저장되어 있지 않습니다. 상세 판정을 확인하세요.'})])))),
        node('button', {type: 'button', class: 'text-button vc-answer-link', text: '답변 원문·전체 근거 보기', onclick: () => openDetail(row)}));
    }));
    if (!improved.length) $('version-improved-answers').append(node('p', {class: 'vc-empty', text: c.eligible ? '확인된 개선 문항이 없습니다. 악화·유지 문항은 문항별 변화에서 확인하세요.' : '전체 비교를 확정하기 전에는 조건이 확인된 개선 문항만 표시합니다.'}));
    $('version-show-improved').hidden = !improved.length;
    $('version-show-improved').textContent = `개선 ${number(improved.length)}문항 모두 보기`;
  }
  function detailValue(value) { return value === null || value === undefined || value === '' ? '기록 없음' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value); }
  function renderProfiles() {
    $('version-profile-content').replaceChildren(...[state.before, state.after].map((run, index) => {
      const p = run?.evaluation_profile, fields = p?.fields;
      const box = node('section', {}, node('h3', {text: releaseName(run, index ? '대상 미선택' : '기준 미선택')}));
      if (!run) { box.append(node('p', {class: 'field-help', text: '실행을 선택하면 저장된 조건을 확인할 수 있습니다.'})); return box; }
      const release = run.aiona_release || {};
      const batch = run.evaluations?.aiona, semantic = batch?.jev_eval;
      const rows = [['실행', run.run_id], ['수집일', release.collected_at], ['메모', release.notes], ['평가 엔진', fields?.engine_versions?.engine || batch?.versions?.engine], ['의미 평가 버전', fields?.semantic_version || semantic?.version], ['의미 평가 기준', fields?.criteria_version || semantic?.criteria_version], ['평가자', fields?.judge?.requested_model || semantic?.client?.requested_model || semantic?.engine_label], ['추론 설정', fields?.judge?.reasoning_effort || semantic?.client?.reasoning_effort]];
      box.append(node('dl', {class: 'version-metadata'}, ...rows.flatMap(([label, value]) => [node('dt', {text: label}), node('dd', {text: detailValue(value)})])));
      box.append(p ? node('details', {class: 'version-profile-raw'}, node('summary', {text: '저장된 평가 조건 전체'}), node('pre', {text: detailValue(p)})) : node('p', {class: 'field-help', text: '평가 조건 기록이 없어 동일 조건 여부를 확정할 수 없습니다.'}));
      const unavailable = W.canEvaluate?.() === false;
      const action = node('button', {type: 'button', class: 'text-button', text: '이 원문을 현재 기준으로 새 평가', onclick: () => reevaluate(run, action, index ? 'target' : 'baseline')});
      action.disabled = !release.version || W.isBusy() || state.submitting || unavailable;
      box.append(action, node('p', {class: 'field-help', text: unavailable ? '평가자 설정을 확인해야 새 평가를 시작할 수 있습니다. 실행 준비에서 상태를 확인해 주세요.' : '평가자 호출 후 새 실행으로 저장하며, 기존 기록은 유지합니다.'}));
      return box;
    }));
  }
  async function reevaluate(run, button, side) {
    if (state.submitting || W.isBusy() || W.canEvaluate?.() === false) return;
    state.submitting = true; button.disabled = true; error(null);
    try {
      const next = await api('/api/comparison/version-runs', {source_run_id: run.run_id, aiona_release: run.aiona_release, name: run.name + ' · 현재 기준 새 평가'});
      rememberRun(next.run_id, side);
      await W.openRun(next.run_id);
    } catch (err) { error(err.message); }
    finally { state.submitting = false; renderProfiles(); }
  }
  function original(run, index) { return index === null || index === undefined ? null : run?.datasets?.aiona?.[index]; }
  function renderRows() {
    const filter = $('version-change-filter').value;
    $('version-impact-counts')?.querySelectorAll('[data-filter]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.filter === filter)));
    saveSelection({...state.selection, changeFilter: filter});
    const rows = state.rows.filter((r) => !filter || filter === 'maintained' && ['passMaintained', 'failMaintained'].includes(r.change) || filter === 'mismatch' && ['unverified', 'unmatched'].includes(r.change) || filter === 'unresolved' && ['pending', 'unverified', 'unmatched'].includes(r.change) || r.change === filter);
    $('version-row-count').textContent = `${number(rows.length)}개 표시`;
    $('version-rows').replaceChildren(...rows.map((row) => node('tr', {},
      node('td', {text: row.caseId || 'ID 없음'}),
      node('td', {}, node('button', {class: 'row-button', text: original(state.before, row.baselineIndex)?.question || original(state.after, row.targetIndex)?.question || '문항 원문 보기', onclick: () => openDetail(row)})),
      node('td', {}, pill(row.baselineState?.text || (row.baselineIndex === null ? '해당 없음' : '비교 제외'), row.baselineState?.outcome === 'issue' ? 'danger' : 'neutral')),
      node('td', {}, pill(row.targetState?.text || (row.targetIndex === null ? '해당 없음' : '비교 제외'), row.targetState?.outcome === 'issue' ? 'danger' : 'neutral')),
      node('td', {}, pill(row.label || labels[row.change], kinds[row.change])))));
    if (!rows.length) $('version-rows').append(node('tr', {}, node('td', {colspan: '5', text: '해당하는 문항이 없습니다.'})));
  }
  function renderCriteria() {
    $('version-criteria-body').replaceChildren(...(state.comparison?.criteria || []).map((criterion) => node('tr', {},
      node('th', {scope: 'row'}, node('strong', {text: criterion.name || criterion.id}), node('small', {text: `공통 ${number(criterion.matchedQuestions)}문항`})),
      ...[criterion.baseline, criterion.target].map((s) => node('td', {}, node('span', {class: 'criterion-rate', text: s?.pending || s?.unknown ? '미확정' : percent(s?.rate)}), node('span', {class: 'criterion-counts', text: `PASS ${number(s?.pass)} · FAIL ${number(s?.fail)} · 확인 필요 ${number((s?.pending || 0) + (s?.unknown || 0))}`}), node('span', {class: 'criterion-targets', text: `완료 판정 ${number((s?.pass || 0) + (s?.fail || 0))}개 기준 · 적용 ${number(s?.targets)}개${s?.excluded ? ` · 제외 ${number(s.excluded)}개` : ''}`}))),
      node('td', {}, node('span', {text: signed(criterion.delta, '%p')}), criterion.delta === null && criterion.reason ? node('span', {class: 'criterion-targets', text: criterion.reason}) : null))));
  }
  function reasons(run, index) {
    const batch = run?.evaluations?.aiona, record = batch?.jev_eval?.records?.[index];
    const c = window.AionaResults.resolvedCase(batch?.cases?.[index], record);
    const lines = (c?.results || []).filter((r) => r.area === 'common' && r.counted && r.verdict === 'FAIL').map((r) => `${r.title || r.check_id}: ${r.explain?.summary || r.reason || ''}`);
    for (const [id, decision] of Object.entries(record?.decisions || {})) {
      if (decision.decision === 'FAIL' || ['UNVERIFIED', 'INCOMPLETE'].includes(decision.status)) lines.push(`${({UNVERIFIED: '확인 필요 · ', INCOMPLETE: '판정 미완료 · '})[decision.status] || ''}${batch.jev_eval.criteria?.find((c) => c.id === id)?.name || id}: ${decision.reason || '판정 근거 확인 필요'}`);
    }
    if (record?.error) lines.push('평가 오류: ' + record.error);
    return lines;
  }
  function passReasons(run, index) {
    const batch = run?.evaluations?.aiona, record = batch?.jev_eval?.records?.[index], lines = [];
    for (const [id, decision] of Object.entries(record?.decisions || {})) {
      if (decision.decision === 'PASS') lines.push(`${batch.jev_eval.criteria?.find((c) => c.id === id)?.name || id}: ${decision.reason || 'PASS로 저장되어 있습니다. 별도 근거 문장은 없습니다.'}`);
    }
    if (record?.code_completion?.status === 'SUCCESS') {
      for (const resolution of record.code_completion.resolutions || []) {
        const result = resolution.result;
        if (result?.verdict === 'PASS') lines.push(`공통 조건 보완 · ${result.title || result.check_id || resolution.condition_id}: ${result.reason || result.explain?.summary || 'PASS로 저장되어 있습니다. 별도 근거 문장은 없습니다.'}`);
      }
    }
    return lines;
  }
  function openDetail(row) {
    $('version-answer-title').textContent = (row.caseId || '문항') + ' · 버전별 원문과 근거';
    $('version-answer-change').textContent = row.label || labels[row.change];
    $('version-answer-columns').replaceChildren(...[[state.before, row.baselineIndex, row.baselineState], [state.after, row.targetIndex, row.targetState]].map(([run, index, verdict]) => {
      const source = original(run, index), box = node('section', {}, node('h3', {text: releaseName(run)}));
      if (!source) { box.append(node('p', {class: 'field-help', text: '이 실행에는 대응되는 문항이 없습니다.'})); return box; }
      const record = run.evaluations?.aiona?.jev_eval?.records?.[index];
      const codeCase = window.AionaResults.resolvedCase(run.evaluations?.aiona?.cases?.[index], record);
      const highlighted = window.AionaAnswerHighlights.create(source.answer, codeCase, record, node);
      box.append(node('div', {class: 'original-question'}, node('span', {text: '질문 원문'}), node('pre', {text: source.question})),
        node('div', {class: 'answer-pane'}, highlighted.element),
        node('div', {class: 'answer-evaluation'}, pill(verdict?.text || '대응 문항 확인 필요', verdict?.outcome === 'issue' ? 'danger' : 'neutral'), node('p', {text: verdict?.detail || row.label || ''})));
      const findings = reasons(run, index);
      box.append(node('h3', {class: 'version-evidence-title', text: '판정 근거'}), findings.length ? node('ul', {class: 'finding-list'}, ...findings.map((text) => node('li', {}, node('p', {text})))) : node('p', {class: 'field-help', text: verdict?.outcome === 'clear' ? '적용된 공통 기준을 모두 통과했습니다.' : '저장된 FAIL 근거가 없습니다. 평가 완료 상태를 확인하세요.'}));
      const passes = passReasons(run, index);
      if (passes.length) box.append(node('details', {class: 'version-profile-raw version-pass-reasons'}, node('summary', {text: `통과 판정 근거 ${number(passes.length)}건`}), node('ul', {class: 'finding-list'}, ...passes.map((text) => node('li', {}, node('p', {text}))))));
      const evidence = window.AionaResults.semanticEvidence(run.evaluations?.aiona?.jev_eval?.records?.[index], node);
      if (evidence) box.append(evidence);
      if (record?.decisions?.J5?.status === 'NOT_APPLICABLE') box.append(node('p', {class: 'field-help', text: 'J5 해당 없음 · ' + (record.applicability?.not_applicable?.J5 || record.decisions.J5.reason || '검증할 사실 주장이 없습니다.')}));
      if (source.conversation_id !== undefined) {
        const history = (run.datasets.aiona || []).filter((r) => r && r.conversation_id === source.conversation_id && Number(r.turn) < Number(source.turn)).sort((a, b) => Number(a.turn) - Number(b.turn));
        box.append(node('details', {class: 'version-profile-raw'}, node('summary', {text: `같은 버전의 이전 대화 ${history.length}턴 원문`}), ...history.map((r) => node('pre', {text: `턴 ${r.turn}\n질문\n${r.question}\n답변\n${r.answer}`}))));
      }
      return box;
    }));
    $('version-answer-dialog').showModal();
  }
  for (const side of ['baseline', 'target']) {
    $('version-' + side + '-model').addEventListener('change', () => { readModelSelection(side); compareSelected(); });
  }
  $('version-change-filter').addEventListener('change', renderRows);
  $('version-show-improved')?.addEventListener('click', () => focusChanges('improved'));
  $('version-show-criteria')?.addEventListener('click', () => {
    scrollToPanel('version-criteria-panel');
    $('version-criteria-panel').focus({preventScroll: true});
  });
  $('version-refresh').addEventListener('click', refresh);
  $('comparison-refresh')?.addEventListener('click', refresh);
  let answerDraft = null, answerGeneration = 0, answerSubmitting = false;
  const answerError = (message) => { $('version-answer-error').textContent = message || ''; $('version-answer-error').hidden = !message; };
  function answerReady() {
    $('version-answer-submit').disabled = answerSubmitting || !answerDraft?.checked || !W.canEvaluate() || W.isBusy();
  }
  async function checkAnswers(cases) {
    const generation = ++answerGeneration;
    if (!answerDraft) return;
    answerDraft.cases = cases; answerDraft.checked = null; answerReady(); answerError(null);
    $('version-answer-check').textContent = '';
    if (!cases || !$('version-answer-release').value) return;
    try {
      const checked = await api('/api/comparison/version-check', {baseline_run_id: answerDraft.baselineId,
        aiona_release: {version: $('version-answer-release').value}, cases});
      if (generation !== answerGeneration || !answerDraft) return;
      answerDraft.checked = checked;
      $('version-answer-check').textContent = `동일 질문 ${number(checked.cases.length)}개 · 새 답변 확인 완료`;
    } catch (err) { if (generation === answerGeneration) answerError(err.message); }
    finally { if (generation === answerGeneration) answerReady(); }
  }
  $('version-download-questions').addEventListener('click', async () => {
    if (!state.before) return;
    try {
      const template = await api(`/api/comparison/runs/${state.before.run_id}/version-template`);
      const url = URL.createObjectURL(new Blob([JSON.stringify(template.cases, null, 2)], {type: 'application/json;charset=utf-8'}));
      const link = node('a', {href: url, download: `aiona-questions-${template.baseline_run_id}.json`});
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) { error(err.message); }
  });
  $('version-add-answers').addEventListener('click', async () => {
    const baseline = state.before;
    if (!baseline) return;
    $('version-add-answers').disabled = true;
    try {
      const template = await api(`/api/comparison/runs/${baseline.run_id}/version-template`);
      answerDraft = {baselineId: baseline.run_id, template: template.cases, cases: null, checked: null};
      const versions = (state.history?.releases || []).filter((r) => r.version !== baseline.aiona_release.version);
      $('version-answer-release').replaceChildren(...(versions.length ? versions.map((r) => node('option', {value: r.version, text: 'AIONA ' + r.version})) : [node('option', {value: '', text: '새 모델 버전을 먼저 등록하세요'})]));
      const targetVersion = $('version-target-version').value;
      if (versions.some((r) => r.version === targetVersion)) $('version-answer-release').value = targetVersion;
      else if (versions.some((r) => r.version === state.history.current_release?.version)) $('version-answer-release').value = state.history.current_release.version;
      $('version-answers-baseline').textContent = `${releaseName(baseline)} 기준 · 동일 질문 ${number(template.cases.length)}개`;
      const single = template.cases.length === 1;
      $('version-single-answer').hidden = !single; $('version-answer-file-label').hidden = single;
      $('version-fixed-question').textContent = single ? template.cases[0].question : '';
      $('version-answer-text').value = ''; $('version-answer-file').value = ''; $('version-answer-check').textContent = '';
      answerError(null); answerReady(); $('version-answers-dialog').showModal();
    } catch (err) { error(err.message); }
    finally { render(); }
  });
  $('version-answer-text').addEventListener('input', () => {
    if (!answerDraft) return;
    ++answerGeneration; answerDraft.checked = null;
    answerDraft.cases = [{...answerDraft.template[0], answer: $('version-answer-text').value}];
    answerError(null); $('version-answer-check').textContent = '';
    // The question and context are fixed by the template; the server rechecks on submission.
    if ($('version-answer-text').value.trim() && $('version-answer-release').value) {
      answerDraft.checked = {cases: answerDraft.cases, aiona_release: {version: $('version-answer-release').value}};
    }
    answerReady();
  });
  $('version-answer-release').addEventListener('change', () => checkAnswers(answerDraft?.cases));
  $('version-answer-file').addEventListener('change', async (event) => {
    const file = event.target.files[0]; if (!file || !answerDraft) return;
    const generation = ++answerGeneration;
    answerDraft.checked = null; answerDraft.cases = null; answerReady(); answerError(null);
    $('version-answer-check').textContent = '질문과 답변을 확인하고 있습니다.';
    try {
      if (file.size > 64 * 1024 * 1024) throw new Error('파일은 64MB 이하여야 합니다.');
      const bytes = new Uint8Array(await file.arrayBuffer());
      const encoding = bytes[0] === 255 && bytes[1] === 254 ? 'utf-16le' : bytes[0] === 254 && bytes[1] === 255 ? 'utf-16be' : 'utf-8';
      const input = new TextDecoder(encoding, {fatal: true}).decode(bytes);
      const parsed = await api('/api/comparison/dataset', {input_text: input, input_name: file.name});
      if (generation === answerGeneration) await checkAnswers(parsed.cases);
    } catch (err) { if (generation === answerGeneration) { $('version-answer-check').textContent = ''; answerError(err.message); } }
  });
  $('version-answers-dialog').addEventListener('close', () => { ++answerGeneration; answerDraft = null; });
  $('version-answers-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if ($('version-answer-submit').disabled || !answerDraft?.checked) return;
    answerSubmitting = true; answerReady(); answerError(null);
    const draft = answerDraft;
    try {
      const next = await api('/api/comparison/version-runs', {baseline_run_id: draft.baselineId,
        cases: draft.checked.cases, aiona_release: draft.checked.aiona_release,
        input_mode: draft.template.length === 1 ? 'manual' : 'file', name: `AIONA ${draft.checked.aiona_release.version} · 동일 질문 비교`});
      saveSelection({...state.selection, baseline: draft.baselineId, target: next.run_id});
      $('version-answers-dialog').close(); await W.openRun(next.run_id);
      if (location.hash === '#results' && W.currentRun()?.run_id === next.run_id && W.currentRun()?.status === 'COMPLETED') {
        $('version-comparison-details').open = true; W.go('comparison');
      }
    } catch (err) { answerError(err.message); }
    finally { answerSubmitting = false; answerReady(); }
  });
  function showRegistration(release = null, selectDefault = false) {
    $('release-version').value = release?.version || ''; $('release-notes').value = release?.notes || ''; $('release-error').hidden = true;
    $('release-set-current').checked = selectDefault;
    $('release-dialog').showModal(); $('release-version').focus();
  }
  $('version-register-next').addEventListener('click', () => showRegistration());
  $('release-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if ($('save-release').disabled) return;
    $('save-release').disabled = true; $('save-release').textContent = '등록 중…'; $('release-error').hidden = true;
    try {
      const release = {version: $('release-version').value.trim(), notes: $('release-notes').value.trim()};
      const setCurrent = $('release-set-current').checked;
      await api('/api/comparison/model-versions', {action: 'register', ...release, set_current: false});
      $('release-dialog').close(); await refresh();
      window.dispatchEvent(new CustomEvent('aiona:history-changed'));
      if (setCurrent) confirmDefault(release);
    } catch (err) { $('release-error').textContent = err.message; $('release-error').hidden = false; }
    finally { $('save-release').disabled = false; $('save-release').textContent = '버전 등록'; }
  });
  window.addEventListener('aiona:versions-open', refresh);
  window.addEventListener('aiona:comparison-open', refresh);
  window.addEventListener('aiona:version-select', (event) => {
    $('version-comparison-details').open = true;
    if (event.detail.baselineId) saveSelection({...state.selection, baseline: event.detail.baselineId, target: event.detail.runId});
    state.requestedRun = event.detail.runId; refresh();
  });
  window.addEventListener('aiona:version-created', (event) => rememberRun(event.detail?.runId, 'target'));
  window.addEventListener('aiona:semantic-setup', renderProfiles);
  render();
  if (['#versions', '#comparison'].includes(location.hash)) refresh();
})();
