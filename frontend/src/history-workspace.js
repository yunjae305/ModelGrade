/* Read-only run and activity history. Original inputs and judgments stay on the server. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const W = window.AionaWorkspace, {node, pill, number, api} = W;
  const host = $('view-history');
  if (!host) return;
  const storageKey = 'aiona-history-filters-v1';
  const emptyRunFilters = () => ({q: '', version: '', from: '', to: '', status: '', input: ''});
  const emptyActivityFilters = () => ({q: '', version: '', from: '', to: '', type: '', result: '', runId: ''});
  const state = {tab: 'runs', runs: [], events: [], versions: [], runFilters: emptyRunFilters(), activityFilters: emptyActivityFilters(),
    runPage: 0, activityPage: 0, runLoaded: false, activityLoaded: false, loading: false, runError: '', activityError: '', generation: 0, detailGeneration: 0};
  const statusLabels = {COLLECTING: '응답 수집 중', READY: '평가 대기', EVALUATING: '평가 중', COMPLETED: '평가 작업 종료', ERROR: '오류 확인 필요', CANCELLED: '실행 중단'};
  const eventLabels = {model_version_registered: '모델 버전 등록', default_version_changed: '기본 평가 버전 변경', evaluation_started: '평가 실행 시작',
    evaluation_completed: '평가 작업 종료', evaluation_failed: '평가 실패', evaluation_cancelled: '평가 취소', reevaluation_created: '재평가 실행 생성', evaluation_policy_applied: '평가 판정 정책 적용'};
  const resultLabels = {success: '성공', failure: '실패', cancelled: '취소'};
  const pageSize = 20;
  let returnFocus = null, restoreFocus = true;

  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
    if (saved) {
      state.tab = saved.tab === 'activities' ? 'activities' : 'runs';
      for (const key of ['runFilters', 'activityFilters']) for (const field of Object.keys(state[key])) {
        if (typeof saved[key]?.[field] === 'string') state[key][field] = saved[key][field];
      }
    }
  } catch (_) { /* Filters work in memory when session storage is unavailable. */ }

  function saveFilters() {
    try { sessionStorage.setItem(storageKey, JSON.stringify({tab: state.tab, runFilters: state.runFilters, activityFilters: state.activityFilters})); }
    catch (_) { /* Source text and result data are never stored here. */ }
  }
  function time(value) {
    return value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ko-KR') : '미기록';
  }
  function localDate(value) {
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}` : '';
  }
  function withinDates(value, filters) {
    const date = localDate(value);
    return (!filters.from || date && date >= filters.from) && (!filters.to || date && date <= filters.to);
  }
  const normalize = (value) => String(value || '').toLocaleLowerCase('ko-KR');
  const runVersion = (run) => run.aiona_release?.version || '';
  const eventVersion = (event) => event.model_version || '';
  const statusLabel = (run) => run.status === 'CANCELLED' ? (run.phase === 'EVALUATE' ? '평가 중단' : '수집 중단') : statusLabels[run.status] || run.status || '상태 미기록';
  const statusKind = (status) => ['ERROR', 'CANCELLED'].includes(status) ? 'warning' : ['COLLECTING', 'EVALUATING'].includes(status) ? 'purple' : 'neutral';
  const resultKind = (result) => result === 'failure' ? 'danger' : result === 'cancelled' ? 'warning' : result === 'success' ? 'success' : 'neutral';
  const inputLabel = (run) => run.input_mode === 'manual' ? '직접 입력' : run.input_mode === 'file' ? '파일 업로드' : '입력 방식 미기록';
  const valueText = (value) => value === null || value === undefined || value === '' ? '미기록' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
  const actorLabel = (event) => {
    const type = typeof event.actor === 'object' ? event.actor?.type : event.actor;
    return ['local', 'local_user'].includes(type) ? '로컬 사용자' : type === 'system' ? '시스템' : '미기록';
  };
  const derived = (event) => event.derived === true || event.source === 'legacy' || event.origin === 'derived' || event.provenance === 'derived';
  function conditionSummary(run) {
    if (run.evaluation_conditions_summary) return run.evaluation_conditions_summary;
    const profile = run.evaluation_profile, fields = profile?.fields;
    if (!fields) return '평가 조건 미기록';
    const parts = [fields.engine_versions?.engine, fields.criteria_version, fields.judge?.requested_model].filter(Boolean);
    return (parts.join(' · ') || '평가 조건 기록 있음') + (profile.complete === true ? '' : ' · 조건 확인 필요');
  }
  function completionSummary(run) {
    const summary = run.evaluation_summary;
    if (summary) {
      const parts = [`판정 ${number(summary.scored)} / ${number(summary.total ?? run.question_count)}`];
      if (summary.incomplete) parts.push(`미완료 ${number(summary.incomplete)}`);
      if (summary.review_required) parts.push(`보류·확인 ${number(summary.review_required)}`);
      return parts.join(' · ');
    }
    return run.status === 'COMPLETED' ? '종료 상태와 문항별 판정은 별도입니다. 상세에서 확인하세요.' : '';
  }
  function select(id, options, value, change) {
    const element = node('select', {id, onchange: (event) => change(event.target.value)},
      ...options.map(([v, text]) => node('option', {value: v, text})));
    element.value = value;
    return element;
  }
  function field(label, element, className = '') { return node('label', {class: className}, node('span', {text: label}), element); }
  function updateFilter(group, key, value) {
    state[group][key] = value;
    state[group === 'runFilters' ? 'runPage' : 'activityPage'] = 0;
    saveFilters(); renderLists();
  }
  function dateFilters(prefix, group) {
    return ['from', 'to'].map((key) => field(key === 'from' ? '시작일' : '종료일', node('input', {type: 'date', id: `${prefix}-${key}`, value: state[group][key],
      onchange: (event) => updateFilter(group, key, event.target.value)})));
  }
  function buildFilters() {
    const run = state.runFilters, activity = state.activityFilters;
    const versionOptions = [['', '전체 모델 버전'], ...state.versions.map((version) => [version, 'AIONA ' + version])];
    $('history-run-filters').replaceChildren(
      field('실행 검색', node('input', {id: 'history-search', type: 'search', value: run.q, placeholder: '실행명 또는 실행 ID', oninput: (event) => updateFilter('runFilters', 'q', event.target.value)}), 'history-filter-search'),
      field('모델 버전', select('history-version-filter', versionOptions, run.version, (value) => updateFilter('runFilters', 'version', value))),
      ...dateFilters('history-date', 'runFilters'),
      field('실행 상태', select('history-status-filter', [['', '전체 실행 상태'], ...Object.entries(statusLabels)], run.status, (value) => updateFilter('runFilters', 'status', value))),
      field('입력 방식', select('history-input-filter', [['', '전체 입력 방식'], ['file', '파일'], ['manual', '직접 입력']], run.input, (value) => updateFilter('runFilters', 'input', value))),
      node('button', {class: 'text-button', type: 'button', text: '필터 초기화', onclick: () => resetFilters('runs')}));
    $('history-activity-filters').replaceChildren(
      field('작업 검색', node('input', {id: 'activity-search', type: 'search', value: activity.q, placeholder: '이벤트·실행 ID, 작업, 사유', oninput: (event) => updateFilter('activityFilters', 'q', event.target.value)}), 'history-filter-search'),
      field('모델 버전', select('activity-version-filter', versionOptions, activity.version, (value) => updateFilter('activityFilters', 'version', value))),
      ...dateFilters('activity-date', 'activityFilters'),
      field('작업 유형', select('activity-type-filter', [['', '전체 작업 유형'], ...Object.entries(eventLabels)], activity.type, (value) => updateFilter('activityFilters', 'type', value))),
      field('결과', select('activity-result-filter', [['', '전체 결과'], ...Object.entries(resultLabels)], activity.result, (value) => updateFilter('activityFilters', 'result', value))),
      node('button', {class: 'text-button', type: 'button', text: '필터 초기화', onclick: () => resetFilters('activities')}));
  }
  function resetFilters(tab) {
    state[tab === 'runs' ? 'runFilters' : 'activityFilters'] = tab === 'runs' ? emptyRunFilters() : emptyActivityFilters();
    state[tab === 'runs' ? 'runPage' : 'activityPage'] = 0;
    saveFilters(); buildFilters(); renderLists();
    $(tab === 'runs' ? 'history-search' : 'activity-search').focus();
  }
  function tabButton(tab, label) {
    return node('button', {type: 'button', id: `history-tab-${tab}`, class: 'answer-tab', role: 'tab', 'aria-controls': `history-panel-${tab}`,
      onclick: () => setTab(tab), onkeydown: (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        setTab(event.key === 'Home' ? 'runs' : event.key === 'End' ? 'activities' : tab === 'runs' ? 'activities' : 'runs');
        $('history-tab-' + state.tab).focus();
      }}, label);
  }
  function table(headers, bodyId, className) {
    return node('div', {class: 'table-scroll', tabindex: '0', role: 'region', 'aria-label': (bodyId === 'history-body' ? '평가 실행' : '작업 이력') + ' 목록. 좁은 화면에서는 가로로 스크롤하세요.'}, node('table', {class: 'data-table ' + className},
      node('thead', {}, node('tr', {}, ...headers.map((label) => node('th', {scope: 'col', text: label})))), node('tbody', {id: bodyId})));
  }
  function panel(tab, title, filtersId, headers, bodyId, className) {
    return node('section', {id: 'history-panel-' + tab, class: 'panel history-list-panel', role: 'tabpanel', 'aria-labelledby': 'history-tab-' + tab},
      node('div', {class: 'panel-heading'}, node('div', {class: 'panel-title'}, node('h2', {text: title}), node('span', {id: 'history-count-' + tab, class: 'pill neutral', 'aria-live': 'polite'}))),
      node('div', {id: filtersId, class: 'matrix-filters history-filters'}),
      tab === 'runs' ? node('p', {class: 'history-list-note field-help', text: '같은 모델 버전이라도 평가 조건이 다를 수 있습니다. 실행별 조건과 문항 판정을 확인하세요.'}) :
        node('div', {id: 'activity-run-scope', class: 'history-filter-actions'}),
      node('div', {id: 'history-feedback-' + tab, class: 'history-feedback empty-state', role: 'status'}),
      table(headers, bodyId, className), node('div', {id: 'history-pagination-' + tab, class: 'table-pagination'}));
  }
  host.replaceChildren(
    node('div', {class: 'page-heading'}, node('div', {}, node('div', {class: 'eyebrow', text: 'HISTORY'}), node('h1', {id: 'history-title', text: '이력 관리'}),
      node('p', {text: '평가 실행과 작업 기록을 시간순으로 확인하세요.'})), node('button', {id: 'refresh-history', type: 'button', class: 'button secondary', text: '기록 새로고침', onclick: refresh})),
    node('div', {id: 'history-summary', class: 'history-summary-grid'}),
    node('div', {class: 'answer-tabs history-tabs', role: 'tablist', 'aria-label': '이력 유형'}, tabButton('runs', '평가 실행'), tabButton('activities', '작업 이력')),
    panel('runs', '평가 실행', 'history-run-filters', ['실행명', '모델 버전', '평가 조건', '문항 수', '실행 시각', '실행 상태', '작업'], 'history-body', 'history-run-table'),
    panel('activities', '작업 이력', 'history-activity-filters', ['작업 시각', '작업 유형', '작업 주체', '모델 버전 / 실행', '결과', '작업'], 'activity-body', 'history-activity-table'));

  const detail = node('dialog', {id: 'history-detail-dialog', class: 'modal history-detail-modal', 'aria-labelledby': 'history-detail-title'},
    node('div', {class: 'modal-heading'}, node('h2', {id: 'history-detail-title', tabindex: '-1', text: '기록 상세'}),
      node('button', {type: 'button', id: 'history-detail-close', class: 'button secondary compact', 'aria-label': '기록 상세 닫기', text: '닫기', onclick: () => detail.close()})),
    node('div', {id: 'history-detail-body', class: 'history-detail-body'}));
  document.body.append(detail);
  detail.addEventListener('close', () => {
    ++state.detailGeneration;
    if (restoreFocus) {
      const target = returnFocus?.isConnected && !returnFocus.closest('[hidden]') ? returnFocus : !host.hidden ? $('history-tab-' + state.tab) : null;
      target?.focus({preventScroll: true});
    }
    restoreFocus = true;
  });
  detail.addEventListener('click', (event) => {
    if (event.target !== detail) return;
    const bounds = detail.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) detail.close();
  });
  function beginDetail(title) {
    if (!detail.open) { returnFocus = document.activeElement; restoreFocus = true; }
    $('history-detail-title').textContent = title;
    $('history-detail-body').replaceChildren(node('p', {class: 'field-help', role: 'status', text: '저장된 기록을 불러오는 중입니다.'}));
    if (!detail.open) { detail.showModal(); $('history-detail-close').focus(); }
    return ++state.detailGeneration;
  }
  function leaveDetail(action) {
    restoreFocus = false;
    if (detail.open) detail.close();
    action();
  }
  function compareRun(runId) {
    leaveDetail(() => {
      W.go('comparison');
      window.dispatchEvent(new CustomEvent('aiona:version-select', {detail: {runId}}));
    });
  }
  function openResults(runId) { leaveDetail(() => W.openRun(runId)); }
  function setTab(tab) {
    state.tab = tab === 'activities' ? 'activities' : 'runs';
    for (const name of ['runs', 'activities']) {
      const active = state.tab === name, button = $('history-tab-' + name);
      button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
      $('history-panel-' + name).hidden = !active;
    }
    saveFilters();
  }
  function renderSummary() {
    const activeRuns = state.runs.filter((run) => ['COLLECTING', 'EVALUATING'].includes(run.status)).length;
    $('history-summary').replaceChildren(...[
      ['저장된 평가 실행', state.runError ? '조회 실패' : state.loading && !state.runLoaded ? '불러오는 중' : number(state.runs.length) + '건', '모델 버전별 평가 조건은 실행에 저장됩니다.'],
      ['진행 중인 실행', state.runError ? '확인 필요' : state.loading && !state.runLoaded ? '불러오는 중' : number(activeRuns) + '건', '응답 수집 중 또는 평가 중인 실행'],
      ['작업 이력', state.activityError ? '조회 실패' : state.loading && !state.activityLoaded ? '불러오는 중' : number(state.events.length) + '건', '버전 등록·기본 버전 변경·평가 작업 기록']
    ].map(([label, value, help]) => node('article', {class: 'history-summary-card'}, node('span', {class: 'field-help', text: label}), node('strong', {text: value}), node('p', {class: 'field-help', text: help}))));
  }
  function feedback(tab, count, allCount, errorMessage, filters) {
    const box = $('history-feedback-' + tab), invalidDates = filters.from && filters.to && filters.from > filters.to;
    box.replaceChildren();
    box.hidden = !state.loading && !errorMessage && !invalidDates && count > 0;
    box.setAttribute('role', errorMessage || invalidDates ? 'alert' : 'status');
    if (state.loading) box.append(node('p', {text: '기록을 불러오는 중입니다.'}));
    else if (errorMessage) box.append(node('h3', {text: '기록을 불러오지 못했습니다'}), node('p', {text: errorMessage}), node('button', {class: 'button secondary', text: '다시 시도', onclick: refresh}));
    else if (invalidDates) box.append(node('p', {text: '종료일은 시작일과 같거나 이후여야 합니다.'}));
    else if (!count) {
      box.append(node('h3', {text: allCount ? '조건에 맞는 기록이 없습니다' : tab === 'runs' ? '평가 실행이 없습니다' : '아직 작업 이력이 없습니다'}),
        node('p', {text: allCount ? '검색어나 필터를 바꿔 다시 확인하세요.' : tab === 'runs' ? '새 비교를 실행하면 원문과 평가 결과가 이곳에 저장됩니다.' : '새로운 버전 등록·기본 버전 변경·평가 작업부터 실제 시각과 주체가 기록됩니다.'}));
      if (allCount) box.append(node('button', {class: 'button secondary', text: '필터 초기화', onclick: () => resetFilters(tab)}));
      else if (tab === 'runs') box.append(node('button', {class: 'button primary', text: '새 비교 만들기', onclick: () => W.go('setup')}));
    }
    const tableBox = $(tab === 'runs' ? 'history-body' : 'activity-body').closest('.table-scroll');
    tableBox.hidden = state.loading || Boolean(errorMessage) || Boolean(invalidDates) || count === 0;
    $('history-pagination-' + tab).hidden = tableBox.hidden;
    $('history-panel-' + tab).setAttribute('aria-busy', String(state.loading));
  }
  function pagination(tab, total) {
    const key = tab === 'runs' ? 'runPage' : 'activityPage', pages = Math.max(1, Math.ceil(total / pageSize));
    state[key] = Math.min(state[key], pages - 1);
    const button = (label, direction, disabled) => node('button', {id: `history-page-${tab}-${direction}`, type: 'button', class: 'button secondary compact', text: label, disabled: disabled ? '' : null,
      onclick: () => {
        state[key] += direction; renderLists();
        const next = $(`history-page-${tab}-${direction}`);
        (next.disabled ? $(`history-page-${tab}-${-direction}`) : next).focus({preventScroll: true});
        $('history-panel-' + tab).scrollIntoView({block: 'start', behavior: 'instant'});
      }});
    $('history-pagination-' + tab).replaceChildren(node('span', {text: `${number(total)}건 중 ${number(total ? state[key] * pageSize + 1 : 0)}–${number(Math.min((state[key] + 1) * pageSize, total))}건`}),
      node('div', {}, button('이전', -1, !state[key]), node('span', {text: `${state[key] + 1} / ${pages}`}), button('다음', 1, state[key] + 1 >= pages)));
  }
  function runMatches(run) {
    const f = state.runFilters;
    return (!f.q || normalize((run.name || '') + ' ' + run.run_id).includes(normalize(f.q))) && (!f.version || runVersion(run) === f.version) &&
      (!f.status || run.status === f.status) && (!f.input || run.input_mode === f.input) && withinDates(run.created_at, f);
  }
  function activityMatches(event) {
    const f = state.activityFilters;
    const searchable = [event.event_id, event.run_id, event.source_run_id, eventVersion(event), eventLabels[event.event_type] || event.event_type, actorLabel(event), event.reason, event.error_summary].join(' ');
    return (!f.q || normalize(searchable).includes(normalize(f.q))) && (!f.version || eventVersion(event) === f.version) &&
      (!f.type || event.event_type === f.type) && (!f.result || event.result === f.result) && (!f.runId || event.run_id === f.runId) && withinDates(event.timestamp, f);
  }
  function renderLists() {
    const runs = state.runs.filter(runMatches), events = state.events.filter(activityMatches);
    $('history-count-runs').textContent = `${number(runs.length)}건`;
    $('history-count-activities').textContent = `${number(events.length)}건`;
    pagination('runs', runs.length); pagination('activities', events.length);
    $('history-body').replaceChildren(...runs.slice(state.runPage * pageSize, (state.runPage + 1) * pageSize).map((run) => node('tr', {'data-run-id': run.run_id},
      node('td', {}, node('button', {class: 'row-button', text: run.name || run.run_id, onclick: () => openRunDetail(run.run_id)}),
        node('span', {class: 'history-cell-secondary', text: inputLabel(run)})),
      node('td', {text: runVersion(run) ? 'AIONA ' + runVersion(run) : '모델 버전 미기록'}),
      node('td', {text: conditionSummary(run)}), node('td', {text: number(run.question_count)}), node('td', {text: time(run.created_at)}),
      node('td', {}, pill(statusLabel(run), statusKind(run.status)),
        completionSummary(run) ? node('span', {class: 'history-cell-secondary', text: completionSummary(run)}) : null,
        run.save_error || run.activity_save_error ? node('span', {class: 'history-cell-secondary', text: '기록 저장 확인 필요'}) : null),
      node('td', {}, node('div', {class: 'history-row-actions'},
        node('button', {class: 'text-button', text: '상세 보기', onclick: () => openRunDetail(run.run_id)}),
        node('button', {class: 'text-button', text: '비교 대상으로 선택', onclick: () => compareRun(run.run_id)}))))));
    $('activity-body').replaceChildren(...events.slice(state.activityPage * pageSize, (state.activityPage + 1) * pageSize).map((event) => node('tr', {
      'data-event-id': event.event_id, onclick: (click) => { if (!click.target.closest('button, a')) openEventDetail(event); }},
      node('td', {text: time(event.timestamp)}),
      node('td', {}, node('button', {class: 'row-button', text: eventLabels[event.event_type] || event.event_type || '작업 유형 미기록', onclick: () => openEventDetail(event)}),
        derived(event) ? node('span', {class: 'history-cell-secondary', text: '기존 기록에서 구성됨'}) : null),
      node('td', {text: actorLabel(event)}), node('td', {}, node('span', {text: eventVersion(event) ? 'AIONA ' + eventVersion(event) : '모델 버전 미기록'}),
        event.run_id ? node('span', {class: 'history-cell-secondary', text: event.run_id}) : null),
      node('td', {}, pill(resultLabels[event.result] || event.result || '미기록', resultKind(event.result))),
      node('td', {}, node('button', {class: 'text-button', text: '상세 보기', onclick: () => openEventDetail(event)})))));
    const scope = $('activity-run-scope'), scopeId = state.activityFilters.runId;
    scope.hidden = !scopeId;
    scope.replaceChildren(...(scopeId ? [node('span', {class: 'field-help', text: '연결된 실행: ' + scopeId}),
      node('button', {class: 'text-button', text: '실행 필터 해제', onclick: () => updateFilter('activityFilters', 'runId', '')})] : []));
    feedback('runs', runs.length, state.runs.length, state.runError, state.runFilters);
    feedback('activities', events.length, state.events.length, state.activityError, state.activityFilters);
  }
  async function refresh() {
    const generation = ++state.generation;
    state.loading = true; state.runError = ''; state.activityError = '';
    $('refresh-history').disabled = true; renderSummary(); renderLists();
    const [history, activities] = await Promise.allSettled([api('/api/comparison/version-history'), api('/api/comparison/activity-history')]);
    if (generation !== state.generation) return;
    if (history.status === 'fulfilled') {
      state.runs = (history.value.runs || []).slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
      state.versions = [...new Set([...(history.value.versions || []), ...(history.value.releases || []).map((release) => release.version), ...state.runs.map(runVersion)].filter(Boolean))];
      state.runLoaded = true;
    } else state.runError = history.reason.message || '평가 실행 조회에 실패했습니다.';
    if (activities.status === 'fulfilled') {
      state.events = (activities.value.events || []).slice().sort((a, b) => String(b.timestamp).localeCompare(String(a.timestamp)));
      state.activityLoaded = true;
    } else state.activityError = activities.reason.status === 404 ? '작업 이력을 지원하는 서버가 필요합니다. 서버를 재시작한 뒤 다시 시도하세요.' : activities.reason.message || '작업 이력 조회에 실패했습니다.';
    state.versions = [...new Set([...state.versions, ...state.events.map(eventVersion), state.runFilters.version, state.activityFilters.version].filter(Boolean))];
    state.loading = false; $('refresh-history').disabled = false;
    buildFilters(); renderSummary(); renderLists();
  }
  function metadata(rows) {
    return node('dl', {class: 'version-metadata history-metadata'}, ...rows.flatMap(([label, value]) => [node('dt', {text: label}), node('dd', {text: valueText(value)})]));
  }
  function section(title, ...content) { return node('section', {class: 'history-detail-section'}, node('h3', {text: title}), ...content); }
  function eventList(events, runId = '') {
    if (!events.length) return node('p', {class: 'field-help', text: '연결된 작업 이력이 없습니다. 과거 작업의 주체나 사유는 추정하지 않습니다.'});
    return node('ul', {class: 'history-related-list'}, ...events.map((event) => node('li', {},
      node('button', {class: 'row-button', text: (eventLabels[event.event_type] || event.event_type) + ' · ' + time(event.timestamp), onclick: () => openEventDetail(event, runId)}),
      node('span', {class: 'history-cell-secondary', text: actorLabel(event) + ' · ' + (resultLabels[event.result] || event.result || '미기록')}))));
  }
  function originalSection(run) {
    const rows = run.datasets?.aiona || [], content = node('div', {class: 'history-original-content'});
    const render = (index) => {
      const row = rows[index];
      content.replaceChildren(...(row ? [node('h4', {text: '질문 원문'}), node('pre', {text: row.question || ''}), node('h4', {text: 'AIONA 답변 원문'}), node('pre', {text: row.answer || ''})] : []));
    };
    render(0);
    const source = section('저장된 원문', rows.length ? field('문항 선택', select('history-source-question', rows.map((row, index) => [String(index), (row.case_id || String(index + 1)) + ' · ' + (row.question || '').slice(0, 90)]), '0', (value) => render(Number(value)))) : node('p', {class: 'field-help', text: '저장된 AIONA 원문이 없습니다.'}), content);
    if (typeof run.input_document?.original_text === 'string') source.append(node('details', {}, node('summary', {text: '업로드한 파일 원문'}), node('pre', {text: run.input_document.original_text})));
    return source;
  }
  async function openRunDetail(runId) {
    if (typeof runId !== 'string' || !runId) return;
    const generation = beginDetail('평가 실행 상세');
    $('history-detail-body').setAttribute('aria-busy', 'true');
    const [result, activities] = await Promise.allSettled([api('/api/comparison/runs/' + encodeURIComponent(runId) + '/result'), api('/api/comparison/activity-history?run_id=' + encodeURIComponent(runId))]);
    if (generation !== state.detailGeneration || !detail.open) return;
    const body = $('history-detail-body'); body.setAttribute('aria-busy', 'false');
    if (result.status === 'rejected') {
      body.replaceChildren(node('div', {class: 'alert error', role: 'alert', text: result.reason.message || '실행을 불러오지 못했습니다.'}),
        node('button', {class: 'button secondary', text: '다시 시도', onclick: () => openRunDetail(runId)})); return;
    }
    const run = result.value, summary = window.AionaResults?.modelSummary(run, 'aiona', run.question_count || run.datasets?.aiona?.length || 0);
    body.replaceChildren(
      section(run.name || runId, pill(statusLabel(run), statusKind(run.status)),
        run.save_error || run.activity_save_error ? node('p', {class: 'alert error', role: 'alert', text: '기록 저장에 문제가 있습니다. ' + (run.save_error || run.activity_save_error)}) : null,
        metadata([['실행 ID', run.run_id], ['모델 버전', runVersion(run) ? 'AIONA ' + runVersion(run) : null], ['실행 시각', time(run.created_at)],
          ['입력 방식', inputLabel(run)], ['문항 수', number(run.question_count)], ['모델 버전 변경 설명', run.aiona_release?.notes]]),
        node('div', {class: 'history-detail-actions'}, node('button', {class: 'button primary', text: '저장된 평가 결과 열기', onclick: () => openResults(runId)}),
          node('button', {class: 'button secondary', text: '비교 대상으로 선택', onclick: () => compareRun(runId)}))),
      section('문항 판정 상태', node('p', {text: run.evaluation_summary ? completionSummary(run) : summary ? `판정 ${number(summary.complete)} / ${number(summary.total)} · 통과 ${number(summary.counts.clear)} · 문제 발견 ${number(summary.counts.issue)} · 미완료 ${number(summary.counts.pending)}` : '저장된 결과에서 문항별 판정을 확인하세요.'}),
        node('p', {class: 'field-help', text: '평가 작업 종료는 모든 문항의 판정 완료를 뜻하지 않습니다. 보류·미완료 문항은 결과에서 별도로 확인할 수 있습니다.'})),
      section('평가 조건', node('p', {text: conditionSummary(run)}), node('p', {class: 'field-help', text: '이 실행에 저장된 평가 조건입니다. 같은 모델 버전의 다른 실행과 조건이 다를 수 있습니다.'}),
        metadata([['평가 엔진', run.evaluation_profile?.fields?.engine_versions?.engine], ['평가 기준', run.evaluation_profile?.fields?.criteria_version],
          ['의미 평가 조건', run.evaluation_profile?.fields?.semantic_version], ['평가자 모델', run.evaluation_profile?.fields?.judge?.requested_model]]),
        node('details', {}, node('summary', {text: '기술 정보와 저장된 평가 조건'}), node('pre', {text: valueText(run.evaluation_profile)}))),
      originalSection(run),
      section('관련 작업 이력', activities.status === 'fulfilled' ? eventList(activities.value.events || [], runId) :
        node('div', {class: 'alert error', role: 'alert', text: activities.reason.message || '관련 작업 이력을 불러오지 못했습니다.'}),
        node('button', {class: 'text-button', text: '작업 이력에서 보기', onclick: () => leaveDetail(() => openActivity({runId}))})));
    if (run.source_run_id || run.baseline_run_id) body.append(section('연결된 평가 실행',
      ...(run.source_run_id ? [node('p', {class: 'field-help', text: '이 실행은 원본을 보존한 별도의 재평가 실행입니다.'}), node('button', {class: 'text-button', text: '원본 실행 보기 · ' + run.source_run_id, onclick: () => openRunDetail(run.source_run_id)})] : []),
      ...(run.baseline_run_id ? [node('button', {class: 'text-button', text: '동일 질문의 기준 실행 보기 · ' + run.baseline_run_id, onclick: () => openRunDetail(run.baseline_run_id)})] : [])));
  }
  function openEventDetail(event, parentRunId = '') {
    beginDetail('작업 이력 상세');
    const body = $('history-detail-body'); body.setAttribute('aria-busy', 'false');
    body.replaceChildren(
      section(eventLabels[event.event_type] || event.event_type || '작업 기록', pill(resultLabels[event.result] || event.result || '미기록', resultKind(event.result)),
        derived(event) ? node('p', {class: 'alert warning', text: '기존 기록에서 구성됨 · 과거 작업 주체와 변경 사유는 확인된 내용만 표시합니다.'}) : null,
        metadata([['이벤트 ID', event.event_id], ['작업 시각', time(event.timestamp)], ['작업 주체', actorLabel(event)], ['모델 버전', eventVersion(event) ? 'AIONA ' + eventVersion(event) : null],
          ['실행 ID', event.run_id], ['변경 사유', event.reason], ['오류 요약', event.error_summary], ['관련 이벤트 ID', event.related_event_id], ['원본 실행 ID', event.source_run_id]])),
      section('변경 전·후', node('div', {class: 'history-change-grid'},
        node('div', {}, node('h4', {text: '변경 전'}), node('pre', {text: valueText(event.before)})),
        node('div', {}, node('h4', {text: '변경 후'}), node('pre', {text: valueText(event.after)})))),
      node('div', {class: 'history-detail-actions'},
        parentRunId ? node('button', {class: 'button secondary', text: '평가 실행 상세로 돌아가기', onclick: () => openRunDetail(parentRunId)}) : null,
        event.run_id ? node('button', {class: 'button primary', text: '연결된 실행 보기', onclick: () => openRunDetail(event.run_id)}) : null,
        event.source_run_id ? node('button', {class: 'button secondary', text: '원본 실행 보기', onclick: () => openRunDetail(event.source_run_id)}) : null));
    const related = state.events.find((item) => item.event_id === event.related_event_id);
    if (related) body.append(node('button', {class: 'text-button', text: '관련 이벤트 보기', onclick: () => openEventDetail(related, parentRunId)}));
  }
  function openActivity({version = '', runId = ''} = {}) {
    state.activityFilters = {...emptyActivityFilters(), version, runId}; state.activityPage = 0;
    if (version && !state.versions.includes(version)) state.versions.push(version);
    setTab('activities'); buildFilters(); renderLists(); W.go('history');
  }
  function openVersionHistory(version) {
    state.runFilters = {...emptyRunFilters(), version: typeof version === 'string' ? version : ''}; state.runPage = 0;
    if (version && !state.versions.includes(version)) state.versions.push(version);
    setTab('runs'); buildFilters(); renderLists(); W.go('history');
  }
  window.addEventListener('aiona:history-open', refresh);
  window.addEventListener('aiona:history-changed', () => { if (!host.hidden) refresh(); });
  window.addEventListener('aiona:history-version', (event) => openVersionHistory(event.detail?.version));
  window.addEventListener('aiona:history-activity', (event) => openActivity(event.detail || {}));
  window.AionaHistory = {refresh, openRunDetail, openActivity, conditionSummary};
  buildFilters(); setTab(state.tab); renderSummary(); renderLists();
  if (!host.hidden || location.hash === '#history') refresh();
})();
