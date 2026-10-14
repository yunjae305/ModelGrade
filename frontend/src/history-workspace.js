/* 이력 관리 — 평가가 끝난 결과 비교마다 쌓이는 AIONA 결과 기록(/api/comparison/aiona-history). 원문과 판정은 서버에 그대로 둔다. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const W = window.AionaWorkspace, {node, pill, number, api} = W;
  const host = $('view-history');
  if (!host) return;
  const storageKey = 'aiona-history-filters-v2';
  const emptyFilters = () => ({q: '', version: '', from: '', to: '', set: ''});
  const state = {entries: [], series: [], labels: {}, filters: emptyFilters(), page: 0, loaded: false, loading: false, error: '',
    generation: 0, detailGeneration: 0};
  const criteriaNames = {J1: 'J1 의도 충족성', J2: 'J2 내용 적절성', J3: 'J3 논리 타당성', J4: 'J4 충분성', J5: 'J5 데이터 신뢰도', J6: 'J6 멀티턴 맥락 유지', J7: 'J7 롱컨텍스트 활용'};
  const pageSize = 20;
  let returnFocus = null, restoreFocus = true;

  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
    for (const field of Object.keys(state.filters)) if (typeof saved?.[field] === 'string') state.filters[field] = saved[field];
  } catch (_) { /* Filters work in memory when session storage is unavailable. */ }

  function saveFilters() {
    try { sessionStorage.setItem(storageKey, JSON.stringify(state.filters)); } catch (_) { /* Source text and results are never stored here. */ }
  }
  function time(value) { return value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ko-KR') : '미기록'; }
  function localDate(value) {
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}` : '';
  }
  const at = (entry) => entry.finished_at || entry.created_at;
  const normalize = (value) => String(value || '').toLocaleLowerCase('ko-KR');
  const percent = (rate) => typeof rate === 'number' ? (rate * 100).toFixed(1) + '%' : '—';
  const points = (delta) => typeof delta === 'number' ? (delta > 0 ? '+' : delta < 0 ? '−' : '±') + Math.abs(delta * 100).toFixed(1) + '%p' : '—';
  const deltaKind = (delta) => typeof delta !== 'number' || delta === 0 ? 'neutral' : delta > 0 ? 'success' : 'danger';
  // 질문 단위(대화 1개 = 1문항) — 턴 수가 다르면 함께 적는다. 예전 기록(units 없음)은 턴 수로 표시
  const setLabel = (set) => `${number(set?.units ?? set?.count)}문항${set?.units != null && set.units !== set.count ? `(${number(set?.count)}턴)` : ''} · ${String(set?.fingerprint || '지문 없음').slice(0, 8)}`;
  const valueText = (value) => value === null || value === undefined || value === '' ? '미기록' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
  function conditionText(conditions) {
    const c = conditions || {}, judge = c.judge || {};
    const parts = [c.code_version && String(c.code_version).replace('aiona-code-eval-', 'CODE '), c.criteria_version,
      judge.requested_model && judge.requested_model + (judge.reasoning_effort ? ' · ' + judge.reasoning_effort : ''),
      c.pipeline === 'items' ? '항목별 평가' : c.pipeline ? '기존 평가' : null, c.dispatch === 'batch' ? '묶음 호출' : null];
    return parts.filter(Boolean).join(' · ') || '평가 조건 미기록';
  }
  // 이전 화면(버전 관리)이 실행 기록으로 평가 조건 요약을 부를 때
  function conditionSummary(run) {
    if (run.evaluation_conditions_summary) return run.evaluation_conditions_summary;
    const fields = run.evaluation_profile?.fields;
    if (!fields) return '평가 조건 미기록';
    const parts = [fields.engine_versions?.engine, fields.criteria_version, fields.judge?.requested_model].filter(Boolean);
    return (parts.join(' · ') || '평가 조건 기록 있음') + (run.evaluation_profile.complete === true ? '' : ' · 조건 확인 필요');
  }
  function changeText(entry) {
    if (!entry.previous_run_id) return '첫 기록 (같은 질문 세트의 이전 기록 없음)';
    const labels = (entry.changes || []).map((change) => change.label);
    const base = entry.relation === 'reevaluation' ? '재평가(같은 답변) · ' : '';
    if (!labels.length) return base + '조건 변화 없음';
    return base + labels.join(' · ') + (entry.multi_factor ? ' — 여러 요인 변경, 원인을 단정하지 않음' : '');
  }
  function select(id, options, value, change) {
    const element = node('select', {id, onchange: (event) => change(event.target.value)}, ...options.map(([v, text]) => node('option', {value: v, text})));
    element.value = value;
    return element;
  }
  function field(label, element, className = '') { return node('label', {class: className}, node('span', {text: label}), element); }
  function updateFilter(key, value) { state.filters[key] = value; state.page = 0; saveFilters(); renderList(); }
  function buildFilters() {
    const f = state.filters;
    const versions = [...new Set(state.entries.map((e) => e.aiona_version).filter(Boolean))];
    if (f.version && !versions.includes(f.version)) versions.push(f.version);
    $('history-filters').replaceChildren(
      field('기록 검색', node('input', {id: 'history-search', type: 'search', value: f.q, placeholder: '실행명 또는 실행 ID', oninput: (event) => updateFilter('q', event.target.value)}), 'history-filter-search'),
      field('AIONA 버전', select('history-version-filter', [['', '전체 버전'], ...versions.map((v) => [v, 'AIONA ' + v])], f.version, (value) => updateFilter('version', value))),
      field('질문 세트', select('history-set-filter', [['', '전체 질문 세트'], ...state.series.map((s) => [s.fingerprint || '', setLabel(s)])], f.set, (value) => updateFilter('set', value))),
      ...['from', 'to'].map((key) => field(key === 'from' ? '시작일' : '종료일', node('input', {type: 'date', id: `history-date-${key}`, value: f[key], onchange: (event) => updateFilter(key, event.target.value)}))),
      node('button', {class: 'text-button', type: 'button', text: '필터 초기화', onclick: resetFilters}));
  }
  function resetFilters() { state.filters = emptyFilters(); state.page = 0; saveFilters(); buildFilters(); renderList(); $('history-search').focus(); }

  host.replaceChildren(
    node('div', {class: 'page-heading'}, node('div', {}, node('div', {class: 'eyebrow', text: 'AIONA 결과 이력'}), node('h1', {id: 'history-title', text: '이력 관리'}),
      node('p', {text: '평가가 끝난 결과 비교마다 AIONA 결과가 쌓입니다. 기준·평가 모델·AIONA 버전이 바뀔 때 결과가 어떻게 달라졌는지 같은 질문 세트의 직전 기록과 비교합니다.'})),
      node('button', {id: 'refresh-history', type: 'button', class: 'button secondary', text: '기록 새로고침', onclick: refresh})),
    node('div', {id: 'history-summary', class: 'history-summary-grid'}),
    node('section', {id: 'history-trend', class: 'panel history-trend-panel', hidden: ''}),
    node('section', {id: 'history-panel', class: 'panel history-list-panel'},
      node('div', {class: 'panel-heading'}, node('div', {class: 'panel-title'}, node('h2', {text: 'AIONA 결과 기록'}), node('span', {id: 'history-count', class: 'pill neutral', 'aria-live': 'polite'}))),
      node('div', {id: 'history-filters', class: 'matrix-filters history-filters'}),
      node('p', {class: 'history-list-note field-help', text: '통과율 변화는 같은 질문 세트끼리만 계산합니다. 한 요인만 바뀐 기록만 그 요인의 효과로 읽을 수 있고, 미완료 질문이 있으면 변화를 확정하지 않습니다.'}),
      node('div', {id: 'history-feedback', class: 'history-feedback empty-state', role: 'status'}),
      node('div', {class: 'table-scroll', tabindex: '0', role: 'region', 'aria-label': 'AIONA 결과 기록 목록. 좁은 화면에서는 가로로 스크롤하세요.'},
        node('table', {class: 'data-table history-run-table'}, node('thead', {}, node('tr', {}, ...['완료 시각', 'AIONA 버전', '질문 세트', '평가 조건', 'AIONA 질문 통과율', '직전 기록 대비', '작업'].map((label) => node('th', {scope: 'col', text: label})))),
          node('tbody', {id: 'history-body'}))),
      node('div', {id: 'history-pagination', class: 'table-pagination'})));

  const detail = node('dialog', {id: 'history-detail-dialog', class: 'modal history-detail-modal', 'aria-labelledby': 'history-detail-title'},
    node('div', {class: 'modal-heading'}, node('h2', {id: 'history-detail-title', tabindex: '-1', text: '기록 상세'}),
      node('button', {type: 'button', id: 'history-detail-close', class: 'button secondary compact', 'aria-label': '기록 상세 닫기', text: '닫기', onclick: () => detail.close()})),
    node('div', {id: 'history-detail-body', class: 'history-detail-body'}));
  document.body.append(detail);
  detail.addEventListener('close', () => {
    ++state.detailGeneration;
    if (restoreFocus) {
      const target = returnFocus?.isConnected && !returnFocus.closest('[hidden]') ? returnFocus : !host.hidden ? $('history-search') : null;
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
  function leaveDetail(action) { restoreFocus = false; if (detail.open) detail.close(); action(); }
  function compareRun(runId) {
    leaveDetail(() => { W.go('comparison'); window.dispatchEvent(new CustomEvent('aiona:version-select', {detail: {runId}})); });
  }
  function openResults(runId) { leaveDetail(() => W.openRun(runId)); }

  function renderSummary() {
    const latest = state.entries[0];
    const value = (text) => state.error ? '조회 실패' : state.loading && !state.loaded ? '불러오는 중' : text;
    $('history-summary').replaceChildren(...[
      ['쌓인 기록', value(number(state.entries.length) + '건'), '평가가 끝난 결과 비교마다 한 건'],
      ['질문 세트', value(number(state.series.length) + '개'), '통과율 변화는 같은 질문 세트끼리만 비교'],
      ['최근 기록', value(latest ? (latest.aiona_version ? 'AIONA ' + latest.aiona_version : '버전 미기록') : '없음'), latest ? time(at(latest)) + ' · 통과율 ' + percent(latest.results?.question_pass_rate) : '새 비교를 끝내면 기록됩니다.']
    ].map(([label, text, help]) => node('article', {class: 'history-summary-card'}, node('span', {class: 'field-help', text: label}), node('strong', {text}), node('p', {class: 'field-help', text: help}))));
  }
  function matches(entry) {
    const f = state.filters, date = localDate(at(entry));
    return (!f.q || normalize((entry.name || '') + ' ' + entry.run_id).includes(normalize(f.q))) && (!f.version || entry.aiona_version === f.version) &&
      (!f.set || (entry.question_set?.fingerprint || '') === f.set) && (!f.from || date && date >= f.from) && (!f.to || date && date <= f.to);
  }
  function feedback(count) {
    const box = $('history-feedback'), f = state.filters, invalidDates = f.from && f.to && f.from > f.to;
    box.replaceChildren();
    box.hidden = !state.loading && !state.error && !invalidDates && count > 0;
    box.setAttribute('role', state.error || invalidDates ? 'alert' : 'status');
    if (state.loading) box.append(node('p', {text: '기록을 불러오는 중입니다.'}));
    else if (state.error) box.append(node('h3', {text: '기록을 불러오지 못했습니다'}), node('p', {text: state.error}), node('button', {class: 'button secondary', text: '다시 시도', onclick: refresh}));
    else if (invalidDates) box.append(node('p', {text: '종료일은 시작일과 같거나 이후여야 합니다.'}));
    else if (!count) {
      box.append(node('h3', {text: state.entries.length ? '조건에 맞는 기록이 없습니다' : '아직 쌓인 기록이 없습니다'}),
        node('p', {text: state.entries.length ? '검색어나 필터를 바꿔 다시 확인하세요.' : 'AIONA 답변을 올려 결과 비교를 끝까지 평가하면 이곳에 한 건씩 쌓입니다.'}));
      box.append(state.entries.length ? node('button', {class: 'button secondary', text: '필터 초기화', onclick: resetFilters})
        : node('button', {class: 'button primary', text: '새 비교 만들기', onclick: () => W.go('setup')}));
    }
    const tableBox = $('history-body').closest('.table-scroll');
    tableBox.hidden = state.loading || Boolean(state.error) || Boolean(invalidDates) || count === 0;
    $('history-pagination').hidden = tableBox.hidden;
    $('history-panel').setAttribute('aria-busy', String(state.loading));
  }
  function pagination(total) {
    const pages = Math.max(1, Math.ceil(total / pageSize));
    state.page = Math.min(state.page, pages - 1);
    const button = (label, direction, disabled) => node('button', {id: `history-page-${direction}`, type: 'button', class: 'button secondary compact', text: label, disabled: disabled ? '' : null,
      onclick: () => {
        state.page += direction; renderList();
        const next = $(`history-page-${direction}`);
        (next.disabled ? $(`history-page-${-direction}`) : next).focus({preventScroll: true});
        $('history-panel').scrollIntoView({block: 'start', behavior: 'instant'});
      }});
    $('history-pagination').replaceChildren(node('span', {text: `${number(total)}건 중 ${number(total ? state.page * pageSize + 1 : 0)}–${number(Math.min((state.page + 1) * pageSize, total))}건`}),
      node('div', {}, button('이전', -1, !state.page), node('span', {text: `${state.page + 1} / ${pages}`}), button('다음', 1, state.page + 1 >= pages)));
  }
  function renderList() {
    const entries = state.entries.filter(matches);
    $('history-count').textContent = `${number(entries.length)}건`;
    pagination(entries.length);
    $('history-body').replaceChildren(...entries.slice(state.page * pageSize, (state.page + 1) * pageSize).map((entry) => {
      const delta = entry.deltas?.question_pass_rate, results = entry.results || {}, questions = results.questions || {};
      return node('tr', {'data-run-id': entry.run_id},
        node('td', {}, node('button', {class: 'row-button', text: time(at(entry)), onclick: () => openRunDetail(entry.run_id)}),
          node('span', {class: 'history-cell-secondary', text: entry.name || entry.run_id})),
        node('td', {text: entry.aiona_version ? 'AIONA ' + entry.aiona_version : '버전 미기록'}),
        node('td', {text: setLabel(entry.question_set)}),
        node('td', {text: conditionText(entry.conditions)}),
        node('td', {}, node('strong', {text: percent(results.question_pass_rate)}),
          node('span', {class: 'history-cell-secondary', text: `통과 ${number(questions.PASS)} · 실패 ${number(questions.FAIL)}` +
            (questions.PENDING ? ` · 미완료 ${number(questions.PENDING)}` : '') + (questions.EXCLUDED ? ` · 제외 ${number(questions.EXCLUDED)}` : '')})),
        node('td', {}, entry.deltas ? pill(points(delta), deltaKind(delta)) : null,
          node('span', {class: 'history-cell-secondary', text: changeText(entry)}),
          entry.deltas && (entry.changes || []).length === 1 && !entry.attributable
            ? node('span', {class: 'history-cell-secondary', text: '미완료 질문이 있어 변화를 확정하지 않음'}) : null),
        node('td', {}, node('div', {class: 'history-row-actions'},
          // 상세 보기 = 결과 비교의 실행 결과 화면으로 (사용자 요청 2026-10-14). 큰 실행은 결과를 받는 데 몇 초 걸려 누르는 즉시 '여는 중…'
          node('button', {class: 'text-button', text: '상세 보기', onclick: async (event) => {
            const button = event.currentTarget;
            button.disabled = true; button.textContent = '여는 중…';
            try { await W.openRun(entry.run_id); } finally { button.disabled = false; button.textContent = '상세 보기'; }
          }}),
          node('button', {class: 'text-button', text: '버전 비교 대상으로', onclick: () => compareRun(entry.run_id)}))));
    }));
    feedback(entries.length);
  }
  // 질문 세트별 AIONA 질문 통과율 추이 — 기록 순서대로 점을 잇고, 바뀐 요인이 있는 기록은 색, 미완료가 있는 기록은 빈 점
  const SVG = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs = {}, ...children) {
    const element = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
    element.append(...children);
    return element;
  }
  function trendChart(set) {
    const points = set.points || [], width = 320, height = 132, pad = 26;
    const x = (i) => points.length === 1 ? width / 2 : pad + i * (width - 2 * pad) / (points.length - 1);
    const y = (rate) => height - pad - (typeof rate === 'number' ? rate : 0) * (height - 2 * pad);
    const rated = points.map((p, i) => [p, i]).filter(([p]) => typeof p.question_pass_rate === 'number');
    const chart = svg('svg', {viewBox: `0 0 ${width} ${height}`, class: 'history-trend-chart', role: 'img',
      'aria-label': `${setLabel(set)} 질문 통과율 추이: ` + rated.map(([p]) => percent(p.question_pass_rate)).join(', ')},
      ...[0, .5, 1].map((r) => svg('line', {x1: pad, x2: width - pad, y1: y(r), y2: y(r), class: 'trend-grid'})),
      ...[0, .5, 1].map((r) => svg('text', {x: 2, y: y(r) + 4, class: 'trend-axis'}, `${r * 100}%`)));
    if (rated.length > 1) chart.append(svg('polyline', {class: 'trend-line', points: rated.map(([p, i]) => `${x(i)},${y(p.question_pass_rate)}`).join(' ')}));
    for (const [p, i] of rated) {
      const factors = (p.markers || []).map((m) => state.labels[m] || m);
      chart.append(svg('circle', {cx: x(i), cy: y(p.question_pass_rate), r: 5,
        class: 'trend-point' + (factors.length ? ' changed' : '') + (p.complete ? '' : ' incomplete')},
        svg('title', {}, `${time(p.at)} · ${p.aiona_version ? 'AIONA ' + p.aiona_version : '버전 미기록'} · 통과율 ${percent(p.question_pass_rate)}`
          + (p.relation === 'reevaluation' ? ' · 원본 실행 대비' + (factors.length ? ` 바뀐 요인: ${factors.join(', ')}` : ' 조건 같음')
            : i === 0 ? ' · 이 질문 세트의 첫 기록' : factors.length ? ` · 바뀐 요인: ${factors.join(', ')}` : ' · 직전 대비 조건 같음')
          + (p.complete ? '' : ' · 미완료 질문 있음(변화 확정 안 함)'))));
    }
    const settled = rated.filter(([p]) => p.complete === true), last = settled[settled.length - 1]?.[0], latest = points[points.length - 1];
    const recent = !latest ? '' : typeof latest.question_pass_rate === 'number'
      ? ` · 최근 ${percent(latest.question_pass_rate)}` + (latest.complete ? '' : ' (미완료 포함)'
        + (last && last !== latest ? ` · 최근 확정 ${percent(last.question_pass_rate)}` : ''))
      : ' · 최근 기록 미확정' + (last ? ` · 최근 확정 ${percent(last.question_pass_rate)}` : '');
    return node('article', {class: 'history-trend-card'},
      node('strong', {text: setLabel(set)}),
      node('span', {class: 'field-help', text: `기록 ${number(points.length)}건` + recent}),
      chart);
  }
  function renderTrend() {
    const box = $('history-trend'), series = state.series.filter((s) => (s.points || []).length);
    box.hidden = !series.length;
    box.replaceChildren(...(series.length ? [
      node('div', {class: 'panel-heading'}, node('div', {class: 'panel-title'}, node('h2', {text: 'AIONA 질문 통과율 추이'}))),
      node('p', {class: 'field-help', text: '질문 세트마다 따로 그립니다. 점에 마우스를 올리면 시각·버전·바뀐 요인을 봅니다. 색 점은 직전 기록과 조건이 바뀐 기록, 빈 점은 미완료 질문이 있어 변화를 확정하지 않은 기록입니다.'}),
      node('div', {class: 'history-trend-grid'}, ...series.map(trendChart))] : []));
  }
  async function refresh() {
    const generation = ++state.generation;
    state.loading = true; state.error = '';
    $('refresh-history').disabled = true; renderSummary(); renderList();
    try {
      const history = await api('/api/comparison/aiona-history');
      if (generation !== state.generation) return;
      state.entries = history.entries || []; state.series = history.series || []; state.labels = history.factor_labels || {};
      state.loaded = true;
    } catch (err) {
      if (generation !== state.generation) return;
      state.error = err.status === 404 ? '이력 관리를 지원하는 서버가 필요합니다. 서버를 재시작한 뒤 다시 시도하세요.' : err.message || 'AIONA 결과 기록 조회에 실패했습니다.';
    }
    state.loading = false; $('refresh-history').disabled = false;
    buildFilters(); renderSummary(); renderTrend(); renderList();
  }
  function metadata(rows) {
    return node('dl', {class: 'version-metadata history-metadata'}, ...rows.flatMap(([label, value]) => [node('dt', {text: label}), node('dd', {text: valueText(value)})]));
  }
  function section(title, ...content) { return node('section', {class: 'history-detail-section'}, node('h3', {text: title}), ...content); }
  function changeSection(entry) {
    if (!entry) return null;
    if (!entry.previous_run_id) return section('직전 기록 대비', node('p', {class: 'field-help', text: '같은 질문 세트의 이전 기록이 없어 비교하지 않습니다.'}));
    const before = state.entries.find((e) => e.run_id === entry.previous_run_id);
    const rows = Object.entries(entry.deltas?.criteria || {}).map(([cid, d]) => node('tr', {},
      node('th', {scope: 'row', text: criteriaNames[cid] || cid}),
      node('td', {text: percent(before?.results?.criteria?.[cid]?.rate)}), node('td', {text: percent(entry.results?.criteria?.[cid]?.rate)}),
      node('td', {}, pill(points(d.rate), deltaKind(d.rate)))));
    return section('직전 기록 대비',
      node('p', {text: changeText(entry)}),
      node('p', {class: 'field-help', text: entry.attributable ? '한 요인만 바뀌었고 두 기록 모두 모든 질문이 판정되어 변화를 그 요인의 효과로 볼 수 있습니다.' :
        entry.multi_factor ? '여러 요인이 함께 바뀌어 어느 요인 때문인지 단정하지 않습니다.' :
        (entry.changes || []).length ? '미완료 질문이 있어 변화를 확정하지 않습니다.' : '조건이 같은 기록끼리의 차이는 평가 모델의 흔들림일 수 있습니다.'}),
      ...(entry.changes || []).map((change) => node('details', {}, node('summary', {text: change.label + ((change.field_labels || change.fields || []).length ? ' · ' + (change.field_labels || change.fields).join(', ') : '')}),
        node('div', {class: 'history-change-grid'}, node('div', {}, node('h4', {text: '이전'}), node('pre', {text: valueText(change.before)})),
          node('div', {}, node('h4', {text: '이후'}), node('pre', {text: valueText(change.after)}))),
        ...(change.changelog || []).map((log) => node('div', {}, node('strong', {text: log.version + ' · ' + (log.date || '')}),
          node('ul', {}, ...(log.changes || []).map((text) => node('li', {text}))))))),
      node('div', {class: 'table-scroll'}, node('table', {class: 'data-table'}, node('thead', {}, node('tr', {}, ...['기준', '이전 통과율', '이번 통과율', '변화'].map((text) => node('th', {scope: 'col', text})))),
        node('tbody', {}, node('tr', {}, node('th', {scope: 'row', text: '질문 통과율'}), node('td', {text: percent(before?.results?.question_pass_rate)}),
          node('td', {text: percent(entry.results?.question_pass_rate)}), node('td', {}, pill(points(entry.deltas?.question_pass_rate), deltaKind(entry.deltas?.question_pass_rate)))), ...rows))),
      node('button', {class: 'text-button', text: '직전 기록 보기 · ' + entry.previous_run_id, onclick: () => openRunDetail(entry.previous_run_id)}));
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
    const generation = beginDetail('AIONA 결과 기록 상세');
    $('history-detail-body').setAttribute('aria-busy', 'true');
    let run;
    try { run = await api('/api/comparison/runs/' + encodeURIComponent(runId) + '/result'); }
    catch (err) {
      if (generation !== state.detailGeneration || !detail.open) return;
      $('history-detail-body').setAttribute('aria-busy', 'false');
      $('history-detail-body').replaceChildren(node('div', {class: 'alert error', role: 'alert', text: err.message || '실행을 불러오지 못했습니다.'}),
        node('button', {class: 'button secondary', text: '다시 시도', onclick: () => openRunDetail(runId)}));
      return;
    }
    if (generation !== state.detailGeneration || !detail.open) return;
    const body = $('history-detail-body'); body.setAttribute('aria-busy', 'false');
    const entry = state.entries.find((e) => e.run_id === runId), version = run.aiona_release?.version;
    body.replaceChildren(
      section(run.name || runId, pill(run.status === 'COMPLETED' ? '평가 작업 종료' : run.status, run.status === 'COMPLETED' ? 'neutral' : 'warning'),
        run.save_error ? node('p', {class: 'alert error', role: 'alert', text: '기록 저장에 문제가 있습니다. ' + run.save_error}) : null,
        metadata([['실행 ID', run.run_id], ['AIONA 버전', version ? 'AIONA ' + version : null], ['완료 시각', time(run.finished_at || run.created_at)],
          ['문항 수', number(run.question_count)], ['질문 세트', entry ? setLabel(entry.question_set) : null], ['AIONA 버전 변경 설명', run.aiona_release?.notes]]),
        node('div', {class: 'history-detail-actions'}, node('button', {class: 'button primary', text: '저장된 평가 결과 열기', onclick: () => openResults(runId)}),
          node('button', {class: 'button secondary', text: '버전 비교 대상으로', onclick: () => compareRun(runId)}))),
      entry ? section('AIONA 결과', node('p', {text: `질문 통과율 ${percent(entry.results?.question_pass_rate)} · 통과 ${number(entry.results?.questions?.PASS)} · 실패 ${number(entry.results?.questions?.FAIL)}` +
        (entry.results?.questions?.PENDING ? ` · 미완료 ${number(entry.results.questions.PENDING)}` : '')}),
        node('p', {class: 'field-help', text: Object.entries(entry.results?.criteria || {}).map(([cid, part]) => `${criteriaNames[cid] || cid} ${percent(part.rate)}`).join(' · ') || '기준별 통과율 없음'})) : null,
      changeSection(entry),
      section('평가 조건', node('p', {text: entry ? conditionText(entry.conditions) : conditionSummary(run)}),
        node('details', {}, node('summary', {text: '기술 정보와 저장된 평가 조건'}), node('pre', {text: valueText(run.evaluation_profile)}))),
      originalSection(run));
    if (run.source_run_id || run.baseline_run_id) body.append(section('연결된 평가 실행',
      ...(run.source_run_id ? [node('p', {class: 'field-help', text: '이 실행은 원본 답변에 새 기준을 다시 돌린 재평가 실행입니다.'}), node('button', {class: 'text-button', text: '원본 실행 보기 · ' + run.source_run_id, onclick: () => openRunDetail(run.source_run_id)})] : []),
      ...(run.baseline_run_id ? [node('button', {class: 'text-button', text: '동일 질문의 기준 실행 보기 · ' + run.baseline_run_id, onclick: () => openRunDetail(run.baseline_run_id)})] : [])));
  }
  function openVersionHistory(version) {
    state.filters = {...emptyFilters(), version: typeof version === 'string' ? version : ''}; state.page = 0;
    saveFilters(); buildFilters(); renderList(); W.go('history');
  }
  window.addEventListener('aiona:history-open', refresh);
  window.addEventListener('aiona:history-changed', () => { if (!host.hidden) refresh(); });
  window.addEventListener('aiona:history-version', (event) => openVersionHistory(event.detail?.version));
  window.AionaHistory = {refresh, openRunDetail, conditionSummary};
  buildFilters(); renderSummary(); renderList();
  if (!host.hidden || location.hash === '#history') refresh();
})();
