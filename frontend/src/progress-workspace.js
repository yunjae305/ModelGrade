/* Mentor requirement status with real artifacts.
   requirements.json = 요구사항·상태·해석, artifacts.json = 실제 산출물(수치는 tools/progress_artifacts.py 가 실제 결과 파일에서 계산). */
(() => {
  'use strict';
  const host = document.getElementById('progress-content');
  if (!host) return;
  const GITHUB = /^https:\/\/github\.com\/yunjae305\/ModelGrade\//;
  const STAGES = [['implemented', '구현 완료', '구현'], ['auto_tested', '자동 테스트 확인', '테스트'], ['real_run', '실제 모델 실행', '실행'], ['accuracy_verified', '판정 정확도 검증', '정확도']];
  const STATES = {done: '확인', partial: '일부', none: '없음', na: '해당 없음'};
  const LEVELS = {none: '미확인', implemented: '구현 완료', auto_tested: '자동 테스트 확인', measured: '실제 데이터 계산', real_run: '실제 모델 실행', accuracy_verified: '판정 정확도 검증'};
  const MEASURED = new Set(['measured', 'real_run', 'accuracy_verified']);
  const TYPES = {prompt: '질문·프롬프트', data: '데이터', experiment: '실험', run: '실행 결과', code: '코드', research: '연구', policy: '정책'};
  const SERIES = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100'];   // dataviz 기본 범주 1~4 (흰 배경 검증, 노랑·청록은 직접 라벨·표 함께)
  const STATUS_COLORS = {SUCCESS: '#0ca30c', INCOMPLETE: '#fab219', ERROR: '#d03b3b'};
  const SVGNS = 'http://www.w3.org/2000/svg';
  const SET_ARTIFACT = 'W01-SET326';   // 실제 생성 질문 세트 산출물 — 질문 세트 326 (10-15, tools/progress_artifacts.py SET_ARTIFACT)
  let requestId = 0;
  let owner = 'all';
  let status = 'all';
  let search = '';

  function element(tag, className, value) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined) item.textContent = String(value);
    return item;
  }
  function append(parent, tag, className, value) {
    const item = element(tag, className, value);
    parent.append(item);
    return item;
  }
  function svg(parent, tag, attrs, value) {
    const item = document.createElementNS(SVGNS, tag);
    for (const [key, val] of Object.entries(attrs || {})) item.setAttribute(key, String(val));
    if (value !== undefined) item.textContent = String(value);
    parent.append(item);
    return item;
  }
  function fmt(value) {
    if (value === null || value === undefined || value === '') return '—';
    if (typeof value === 'number') return value.toLocaleString('ko-KR');
    if (typeof value === 'boolean') return value ? '예' : '아니요';
    return String(value);
  }
  function cellText(column, value) {
    if (column.percent) return value === null || value === undefined ? '—' : (value * 100).toFixed(1) + '%';
    return fmt(value);
  }
  function groupOf(item) {
    if (item.status.startsWith('완료')) return 'done';
    if (item.status === '부분완료') return 'partial';
    if (item.status === '승인대기' || item.status === '확인필요') return 'pending';
    return 'todo';
  }
  function field(parent, label, value) {
    const box = append(parent, 'div', 'progress-field');
    append(box, 'h4', '', label);
    append(box, 'p', '', value || '확인 필요');
    return box;
  }
  function section(parent, letter, title, note) {
    const box = append(parent, 'section', 'progress-section');
    const head = append(box, 'div', 'progress-section-head');
    append(head, 'span', 'progress-section-letter', letter);
    append(head, 'h3', '', title);
    if (note) append(head, 'small', '', note);
    return append(box, 'div', 'progress-section-body');
  }
  function list(parent, values, ordered) {
    const box = append(parent, ordered ? 'ol' : 'ul', 'progress-list-plain');
    for (const value of values || []) append(box, 'li', '', value);
    return box;
  }
  function link(parent, label, url) {
    if (!GITHUB.test(url || '')) return null;
    const anchor = append(parent, 'a', 'progress-link', label);
    anchor.href = url;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    return anchor;
  }
  // --- 표 · 다운로드 ------------------------------------------------------------------------
  function csvCell(value) {
    const text = String(value ?? '');
    return /[",\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  }
  function download(name, columns, rows) {
    const lines = [columns.map(column => csvCell(column.label)).join(',')]
      .concat(rows.map(row => columns.map(column => csvCell(cellText(column, row[column.key]))).join(',')));
    const blob = new Blob(['﻿' + lines.join('\r\n')], {type: 'text/csv;charset=utf-8'});
    const anchor = document.createElement('a');
    anchor.href = URL.createObjectURL(blob);
    anchor.download = name;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(anchor.href), 1000);
  }
  function dataTable(parent, spec, caption, pageSize = 0) {
    const columns = spec.columns || [];
    const rows = Array.isArray(spec.rows) ? spec.rows : [];
    const wrap = append(parent, 'div', 'progress-table-block');
    const tools = append(wrap, 'div', 'progress-table-tools');
    const selects = [];
    for (const key of spec.filters || []) {
      const column = columns.find(c => c.key === key);
      const select = append(tools, 'select');
      select.setAttribute('aria-label', (column?.label || key) + ' 필터');
      append(select, 'option', '', '전체 ' + (column?.label || key)).value = '';
      for (const value of [...new Set(rows.map(row => String(row[key] ?? '')))].sort((a, b) => a.localeCompare(b, 'ko', {numeric: true}))) {
        append(select, 'option', '', value || '(빈 값)').value = value;
      }
      selects.push([key, select]);
    }
    const input = rows.length > 8 ? append(tools, 'input') : null;
    if (input) { input.type = 'search'; input.placeholder = '표 안에서 찾기'; input.setAttribute('aria-label', (caption || '표') + ' 검색'); }
    const count = append(tools, 'span', 'progress-table-count');
    const paging = pageSize && rows.length > pageSize ? append(tools, 'span', 'progress-paging') : null;
    let page = 0;
    let previous, next;
    if (paging) {
      previous = append(tools, 'button', 'progress-mini-button', '이전'); previous.type = 'button';
      next = append(tools, 'button', 'progress-mini-button', '다음'); next.type = 'button';
    }
    let shown = rows;
    if (spec.download && rows.length) {
      const button = append(tools, 'button', 'progress-mini-button', 'CSV 다운로드');
      button.type = 'button';
      button.addEventListener('click', () => download(spec.download, columns, shown));
    }
    const scroll = append(wrap, 'div', 'progress-table-scroll');
    const table = append(scroll, 'table', 'progress-table');
    if (caption) append(table, 'caption', '', caption);
    const headRow = append(append(table, 'thead'), 'tr');
    for (const column of columns) append(headRow, 'th', column.numeric || column.percent ? 'num' : '', column.label).scope = 'col';
    const body = append(table, 'tbody');
    function update() {
      const term = (input?.value || '').trim().toLocaleLowerCase('ko');
      shown = rows.filter(row => selects.every(([key, select]) => !select.value || String(row[key] ?? '') === select.value) &&
        (!term || columns.some(column => String(row[column.key] ?? '').toLocaleLowerCase('ko').includes(term))));
      const pages = Math.max(1, Math.ceil(shown.length / (pageSize || shown.length || 1)));
      page = Math.min(page, pages - 1);
      const visible = paging ? shown.slice(page * pageSize, (page + 1) * pageSize) : shown;
      body.replaceChildren(...visible.map(row => {
        const tr = element('tr');
        for (const column of columns) append(tr, 'td', column.numeric || column.percent ? 'num' : '', cellText(column, row[column.key]));
        return tr;
      }));
      if (!shown.length) append(append(body, 'tr'), 'td', 'progress-empty-cell', '조건에 맞는 행이 없습니다.').colSpan = columns.length || 1;
      count.textContent = `${rows.length}행 중 ${shown.length}행`;
      if (paging) {
        paging.textContent = `${page + 1} / ${pages}쪽`;
        previous.disabled = page === 0;
        next.disabled = page >= pages - 1;
      }
    }
    for (const [, select] of selects) select.addEventListener('change', () => { page = 0; update(); });
    if (input) input.addEventListener('input', () => { page = 0; update(); });
    if (paging) {
      previous.addEventListener('click', () => { page--; update(); });
      next.addEventListener('click', () => { page++; update(); });
    }
    update();
    return wrap;
  }

  // --- 그래프 (SVG, 수치는 artifacts.json 그대로) ----------------------------------------------
  function niceMax(value) {
    if (!(value > 0)) return 1;
    const power = 10 ** Math.floor(Math.log10(value));
    for (const step of [1, 2, 2.5, 5, 10]) if (value <= step * power) return step * power;
    return 10 * power;
  }
  function barPath(x, y, width, height) {
    const r = Math.min(4, width, height / 2);
    if (width <= 0) return '';
    return `M${x},${y}h${width - r}a${r},${r} 0 0 1 ${r},${r}v${height - 2 * r}a${r},${r} 0 0 1 ${-r},${r}h${-(width - r)}z`;
  }
  function showTip(tip, box, event, lines) {
    tip.replaceChildren(...lines.map(line => element('div', '', line)));
    tip.hidden = false;
    const rect = box.getBoundingClientRect();
    const x = Math.min(Math.max(event.clientX - rect.left + 12, 4), Math.max(4, rect.width - tip.offsetWidth - 4)) + box.scrollLeft;
    tip.style.left = x + 'px';
    tip.style.top = Math.max(4, event.clientY - rect.top - tip.offsetHeight - 10) + 'px';
  }
  function legend(parent, entries) {
    const box = append(parent, 'div', 'progress-legend');
    for (const entry of entries) {
      const item = append(box, 'span', 'progress-legend-item');
      const swatch = append(item, 'i', entry.line ? 'line' : '');
      swatch.style.background = entry.color;
      append(item, 'span', '', entry.name);
    }
  }
  function barChart(box, chart, tip) {
    const series = chart.series || [];
    const categories = chart.categories || [];
    const width = 640, labelWidth = chart.label_width || 104, right = 58, bar = series.length > 1 ? 11 : 14, gap = 2, pad = 10;
    const band = series.length * bar + (series.length - 1) * gap + pad;
    const height = categories.length * band + 26;
    const values = series.flatMap(s => s.values || []).filter(v => typeof v === 'number');
    const max = chart.max || niceMax(Math.max(0, ...values));
    const plot = width - labelWidth - right;
    const steps = [5, 4, 2].find(n => Number.isInteger(max / n)) || 4;   // 눈금은 깔끔한 수로 (25 → 5씩)
    const root = svg(box, 'svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': chart.title, class: 'progress-svg'});
    for (let i = 0; i <= steps; i++) {
      const x = labelWidth + plot * i / steps;
      svg(root, 'line', {x1: x, x2: x, y1: 4, y2: height - 22, class: 'grid'});
      svg(root, 'text', {x, y: height - 8, 'text-anchor': 'middle', class: 'tick'}, fmt(Math.round(max * i / steps * 10) / 10) + (chart.unit === '%' ? '%' : ''));
    }
    categories.forEach((code, row) => {
      const category = (chart.category_labels || [])[row] || code;   // 'J6' 대신 'J6 · 멀티턴 맥락 유지'
      const top = 6 + row * band;
      svg(root, 'text', {x: labelWidth - 8, y: top + (band - pad) / 2 + 4, 'text-anchor': 'end', class: 'label'}, category);
      series.forEach((entry, index) => {
        const value = (entry.values || [])[row];
        const y = top + index * (bar + gap);
        const length = typeof value === 'number' ? Math.max(0, Math.min(1, value / max)) * plot : 0;
        if (length > 0) svg(root, 'path', {d: barPath(labelWidth, y, length, bar), fill: SERIES[index % SERIES.length], class: 'mark'});
        svg(root, 'text', {x: labelWidth + length + 5, y: y + bar / 2 + 4, class: 'value'}, typeof value === 'number' ? fmt(value) + (chart.unit === '%' ? '%' : '') : '—');
        const hit = svg(root, 'rect', {x: labelWidth, y: y - 1, width: plot + right - 4, height: bar + 2, class: 'hit'});
        hit.addEventListener('pointermove', event => showTip(tip, box, event, [`${category}`, `${entry.name}: ${typeof value === 'number' ? fmt(value) : '—'} ${chart.unit || ''}`.trim()]));
        hit.addEventListener('pointerleave', () => { tip.hidden = true; });
      });
    });
  }
  function stackedChart(box, chart, tip) {
    const series = chart.series || [];
    const categories = chart.categories || [];
    const width = 640, labelWidth = 74, right = 70, bar = 20, band = 34;
    const height = categories.length * band + 8;
    const totals = categories.map((_, row) => series.reduce((sum, entry) => sum + (Number((entry.values || [])[row]) || 0), 0));
    const max = Math.max(1, ...totals);
    const plot = width - labelWidth - right;
    const root = svg(box, 'svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': chart.title, class: 'progress-svg'});
    categories.forEach((category, row) => {
      const y = 6 + row * band;
      svg(root, 'text', {x: labelWidth - 8, y: y + bar / 2 + 4, 'text-anchor': 'end', class: 'label'}, category);
      let x = labelWidth;
      const parts = series.map(entry => ({entry, value: Number((entry.values || [])[row]) || 0})).filter(part => part.value > 0);
      parts.forEach((part, index) => {
        const full = part.value / max * plot;
        const length = Math.max(1, full - (index < parts.length - 1 ? 2 : 0));   // 조각 사이 2px 바탕색 틈
        const color = STATUS_COLORS[part.entry.state] || SERIES[index % SERIES.length];
        svg(root, 'rect', {x, y, width: length, height: bar, fill: color, rx: index === parts.length - 1 ? 3 : 0, class: 'mark'});
        const label = fmt(part.value);
        if (length > label.length * 7 + 12) svg(root, 'text', {x: x + length / 2, y: y + bar / 2 + 4, 'text-anchor': 'middle', class: 'inside'}, label);
        const hit = svg(root, 'rect', {x, y: y - 2, width: Math.max(length, 3), height: bar + 4, class: 'hit'});
        hit.addEventListener('pointermove', event => showTip(tip, box, event, [category, `${part.entry.name}: ${label} / ${fmt(totals[row])} ${chart.unit || ''}`.trim()]));
        hit.addEventListener('pointerleave', () => { tip.hidden = true; });
        x += full;
      });
      svg(root, 'text', {x: labelWidth + totals[row] / max * plot + 6, y: y + bar / 2 + 4, class: 'value'}, `${fmt(totals[row])} ${chart.unit || ''}`.trim());
    });
  }
  function lineChart(box, chart, tip) {
    const series = chart.series || [];
    const xs = (chart.categories || []).map(Number);
    const width = 640, height = 270, left = 40, right = 74, top = 14, bottom = 38;
    const values = series.flatMap(s => s.values || []).filter(v => typeof v === 'number');
    const max = chart.max || niceMax(Math.max(0, ...values));
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const sx = value => left + (maxX === minX ? 0.5 : (value - minX) / (maxX - minX)) * (width - left - right);
    const sy = value => top + (1 - value / max) * (height - top - bottom);
    const root = svg(box, 'svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': chart.title, class: 'progress-svg'});
    for (let i = 0; i <= 4; i++) {
      const value = max * i / 4;
      svg(root, 'line', {x1: left, x2: width - right, y1: sy(value), y2: sy(value), class: 'grid'});
      svg(root, 'text', {x: left - 6, y: sy(value) + 4, 'text-anchor': 'end', class: 'tick'}, fmt(Math.round(value)));
    }
    for (const x of xs) svg(root, 'text', {x: sx(x), y: height - bottom + 16, 'text-anchor': 'middle', class: 'tick'}, fmt(x));
    if (chart.x_label) svg(root, 'text', {x: (left + width - right) / 2, y: height - 4, 'text-anchor': 'middle', class: 'axis-label'}, chart.x_label);
    const crosshair = svg(root, 'line', {x1: 0, x2: 0, y1: top, y2: height - bottom, class: 'crosshair', visibility: 'hidden'});
    const ends = [];
    series.forEach((entry, index) => {
      const color = SERIES[index % SERIES.length];
      const points = xs.map((x, i) => [x, (entry.values || [])[i]]).filter(([, v]) => typeof v === 'number');
      svg(root, 'path', {d: points.map(([x, v], i) => `${i ? 'L' : 'M'}${sx(x)},${sy(v)}`).join(''), stroke: color, class: 'line'});
      for (const [x, v] of points) svg(root, 'circle', {cx: sx(x), cy: sy(v), r: 4, fill: color, class: 'dot'});
      if (points.length) ends.push({y: sy(points[points.length - 1][1]), x: sx(points[points.length - 1][0]), color, text: `${entry.name} ${fmt(points[points.length - 1][1])}`});
    });
    ends.sort((a, b) => a.y - b.y);
    for (let i = 1; i < ends.length; i++) ends[i].y = Math.max(ends[i].y, ends[i - 1].y + 14);
    for (const end of ends) {
      svg(root, 'line', {x1: end.x + 7, x2: end.x + 15, y1: end.y, y2: end.y, stroke: end.color, class: 'key'});
      svg(root, 'text', {x: end.x + 18, y: end.y + 4, class: 'value'}, end.text);
    }
    const hit = svg(root, 'rect', {x: left, y: top, width: width - left - right, height: height - top - bottom, class: 'hit'});
    hit.addEventListener('pointermove', event => {
      const rect = root.getBoundingClientRect();
      const px = (event.clientX - rect.left) / rect.width * width;
      let nearest = 0;
      xs.forEach((x, i) => { if (Math.abs(sx(x) - px) < Math.abs(sx(xs[nearest]) - px)) nearest = i; });
      crosshair.setAttribute('x1', sx(xs[nearest])); crosshair.setAttribute('x2', sx(xs[nearest])); crosshair.setAttribute('visibility', 'visible');
      showTip(tip, box, event, [`${chart.x_label || 'x'} ${fmt(xs[nearest])}`, ...series.map(entry => `${entry.name}: ${fmt((entry.values || [])[nearest])}`)]);
    });
    hit.addEventListener('pointerleave', () => { tip.hidden = true; crosshair.setAttribute('visibility', 'hidden'); });
  }
  function chartBlock(parent, chart) {
    const figure = append(parent, 'figure', 'progress-chart');
    append(figure, 'figcaption', '', chart.title);
    const series = chart.series || [];
    if (chart.kind === 'stacked') legend(figure, series.map(entry => ({name: entry.name, color: STATUS_COLORS[entry.state] || SERIES[0]})));
    else if (series.length > 1) legend(figure, series.map((entry, i) => ({name: entry.name, color: SERIES[i % SERIES.length], line: chart.kind === 'line'})));
    const box = append(figure, 'div', 'progress-chart-box');
    const tip = append(box, 'div', 'progress-tooltip');
    tip.hidden = true;
    if (chart.kind === 'line') lineChart(box, chart, tip);
    else if (chart.kind === 'stacked') stackedChart(box, chart, tip);
    else barChart(box, chart, tip);
    const details = append(figure, 'details', 'progress-chart-data');
    append(details, 'summary', '', '그래프 수치 표로 보기');
    const columns = [{key: 'category', label: chart.x_label || '항목'}].concat(series.map((entry, i) => ({key: 's' + i, label: entry.name, numeric: true})));
    const rows = (chart.categories || []).map((category, row) => Object.fromEntries([['category', (chart.category_labels || [])[row] || category]].concat(series.map((entry, i) => ['s' + i, (entry.values || [])[row]]))));
    dataTable(details, {columns, rows}, chart.title);
    return figure;
  }

  // --- 산출물 -------------------------------------------------------------------------------
  function stats(parent, values) {
    const box = append(parent, 'div', 'progress-stats');
    for (const stat of values || []) {
      const tile = append(box, 'div', 'progress-stat');
      append(tile, 'span', '', stat.label);
      append(tile, 'strong', '', fmt(stat.value));
      if (stat.note) append(tile, 'small', '', stat.note);
    }
  }
  function pairs(parent, values) {
    const box = append(parent, 'dl', 'progress-pairs');
    for (const pair of values || []) { append(box, 'dt', '', pair.label); append(box, 'dd', '', fmt(pair.value)); }
  }
  function block(parent, title) {
    const box = append(parent, 'div', 'progress-block');
    if (title) append(box, 'h5', '', title);
    return box;
  }
  function renderPrompt(parent, content) {
    pairs(parent, [{label: '버전', value: content.version}, {label: '생성 모델', value: content.model_setting}]);
    list(block(parent, '생성 단계 (코드 순서)'), content.stages, true);
    for (const prompt of content.prompts || []) {
      const details = append(parent, 'details', 'progress-prompt');
      append(details, 'summary', '', `프롬프트 원문 — ${prompt.name} · ${fmt(prompt.chars)}자 · sha256 ${prompt.sha256}`);
      append(details, 'p', 'progress-muted', '만드는 호출: ' + prompt.call);
      const actions = append(details, 'div', 'progress-prompt-actions');
      const copy = append(actions, 'button', 'progress-mini-button', '원문 복사'); copy.type = 'button';
      copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(prompt.text); copy.textContent = '복사됨'; }
        catch (_) { copy.textContent = '복사 실패 — 원문을 선택해 복사하세요'; }
      });
      const save = append(actions, 'button', 'progress-mini-button', '원문 다운로드'); save.type = 'button';
      save.addEventListener('click', () => {
        const url = URL.createObjectURL(new Blob([prompt.text], {type: 'text/plain;charset=utf-8'}));
        const anchor = document.createElement('a');
        anchor.href = url; anchor.download = `ModelGrade-${content.version}-${content.prompts.indexOf(prompt) + 1}.txt`;
        document.body.append(anchor); anchor.click(); anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      });
      append(details, 'pre', '', prompt.text);
    }
    if (content.settings) dataTable(block(parent, '생성 조건'), {columns: [{key: 'name', label: '조건'}, {key: 'value', label: '값', numeric: true}, {key: 'meaning', label: '뜻'}], rows: content.settings});
    if (content.output_fields) dataTable(block(parent, '출력 형식'), {columns: [{key: 'field', label: '필드'}, {key: 'meaning', label: '뜻'}], rows: content.output_fields});
    if (content.history) {
      const box = block(parent, '버전 기록');
      const items = append(box, 'ul', 'progress-list-plain');
      for (const entry of content.history) {
        const li = append(items, 'li');
        append(li, 'code', '', entry.version + ' · ' + entry.commit);
        append(li, 'span', '', ' ' + entry.summary + ' ');
        link(li, 'GitHub', entry.url);
      }
    }
  }
  function renderSamples(parent, samples) {
    const box = block(parent, `실제 생성 질문 예시 ${samples.length}개 (규칙으로 고른 원문, 긴 글은 앞부분만)`);
    const grid = append(box, 'div', 'progress-samples');
    for (const sample of samples) {
      const card = append(grid, 'article', 'progress-sample');
      append(card, 'strong', '', `Q${sample.id} · ${sample.why}`);
      append(card, 'small', '', `${sample.type} · ${sample.difficulty} · ${sample.turns_total}턴 · 노린 항목 ${sample.target_items.join(' ')}`);
      for (const turn of sample.turns || []) {
        const p = append(card, 'p', '', `T${turn.turn}: ${turn.question}`);
        if (turn.attachments) append(p, 'em', '', ' [첨부 ' + turn.attachments.join(', ') + ']');
        if (turn.reference_chars) append(p, 'em', '', ` [참조 자료 ${fmt(turn.reference_chars)}자]`);
      }
      if (sample.omitted_turns) append(card, 'small', '', `… 나머지 ${sample.omitted_turns}턴 생략`);
      append(card, 'p', 'progress-muted', 'FAIL 함정: ' + (sample.trap || '—'));
    }
  }
  function renderResearch(parent, content) {
    if (content.question) field(parent, '연구 질문', content.question);
    if (content.method) list(block(parent, '방법'), content.method, true);
    if (content.findings) list(block(parent, '발견'), content.findings);
    if (content.judgment) field(parent, '판단과 근거', content.judgment).classList.add('strong');
    if (content.next) list(block(parent, '추가 실험 조건'), content.next, true);
  }
  function renderPolicy(parent, content) {
    if (content.question) field(parent, '정책 질문', content.question);
    if (content.alternatives) dataTable(block(parent, '대안 비교'), {columns: [{key: 'name', label: '대안'}, {key: 'rule', label: '규칙'},
      {key: 'measured', label: '저장 실행 적용 대화'}, {key: 'pros', label: '장점'}, {key: 'cons', label: '단점'}], rows: content.alternatives});
    if (content.decision) {
      const box = block(parent, '결정 상태 · ' + content.decision.status);
      append(box, 'p', '', content.decision.text);
      if (content.decision.reason) append(box, 'p', 'progress-muted', '이유: ' + content.decision.reason);
      if (content.decision.open) append(box, 'p', 'progress-open', '남은 결정: ' + content.decision.open);
    }
  }
  function renderCode(parent, content) {
    if (content.features) list(block(parent, '구현 기능'), content.features);
    if (content.files) {
      const box = append(block(parent, '파일'), 'ul', 'progress-list-plain');
      for (const file of content.files) { const li = append(box, 'li'); append(li, 'code', '', file.path); if (file.url) { li.append(' '); link(li, 'GitHub', file.url); } }
    }
    if (content.tests) list(block(parent, '자동 테스트'), content.tests.map(test => `${test.path} — 테스트 ${fmt(test.count)}개`));
    if (content.commits) {
      const box = append(block(parent, '커밋'), 'ul', 'progress-list-plain');
      for (const commit of content.commits) { const li = append(box, 'li'); append(li, 'code', '', commit.hash); li.append(' ' + commit.summary + ' '); link(li, 'GitHub', commit.url); }
    }
  }
  function renderRun(parent, content) {
    if (content.run_id) pairs(parent, [{label: '실행 ID', value: content.run_id}]);
    if (content.inputs) { const box = block(parent, '입력'); pairs(box, content.inputs); }
    if (content.outputs) { const box = block(parent, '출력·상태'); pairs(box, content.outputs); }
    if (content.errors && content.errors.length) dataTable(block(parent, '실패 사유'), {columns: [{key: 'reason', label: '사유'}, {key: 'count', label: '턴', numeric: true}], rows: content.errors});
    if (content.log && content.log.length) dataTable(block(parent, '실행 기록 (앞부분 일부 — 실패·성공 예)'), {columns: [{key: 'case_id', label: '질문'}, {key: 'status', label: '상태'},
      {key: 'at', label: '시각'}, {key: 'retried', label: '다시 받음'}, {key: 'question', label: '질문 앞부분'}, {key: 'text', label: '답변·오류 앞부분'}], rows: content.log});
  }
  function renderContent(parent, artifact) {
    const content = artifact.content || {};
    if (content.stats) stats(parent, content.stats);
    if (artifact.type === 'prompt') renderPrompt(parent, content);
    if (artifact.type === 'research') renderResearch(parent, content);
    if (artifact.type === 'policy') renderPolicy(parent, content);
    if (artifact.type === 'code') renderCode(parent, content);
    if (artifact.type === 'run') renderRun(parent, content);
    for (const chart of content.charts || []) chartBlock(parent, chart);
    if (content.table) dataTable(block(parent, '표'), content.table, artifact.title, artifact.artifact_id === SET_ARTIFACT ? 25 : 0);
    if (content.minimum) dataTable(block(parent, '기준마다 k번을 채우는 가장 작은 질문 묶음 (탐욕 근사 — 실제 최소보다 클 수 있음)'), content.minimum);
    if (content.precision) dataTable(block(parent, '이론 기준 — 통과율 p 를 95% 신뢰구간 ±h 로 재는 데 필요한 기준별 적용 수'),
      {columns: [{key: 'p', label: '예상 통과율 p', numeric: true}, {key: 'half_width', label: '반폭 h', numeric: true}, {key: 'n', label: '필요 적용 수', numeric: true}], rows: content.precision});
    if (content.samples && content.samples.length) renderSamples(parent, content.samples);
    for (const example of content.examples || []) {
      const details = append(parent, 'details', 'progress-prompt');
      append(details, 'summary', '', example.label);
      append(details, 'pre', '', JSON.stringify(example.json, null, 2));
    }
    if (content.notes) list(block(parent, '참고'), content.notes);
  }
  function referenceRow(parent, reference, files) {
    const row = append(parent, 'li', 'progress-reference');
    append(row, 'span', '', reference.label);
    if (reference.path) append(row, 'code', '', reference.path);
    if (reference.revision) append(row, 'small', '', '커밋 ' + reference.revision);
    if (reference.url) link(row, 'GitHub', reference.url);
    const state = append(row, 'span', 'progress-file checking', '확인 중');
    files.push({path: reference.path, reference, state, row});
  }
  function applyFileStatus(entry, result) {
    const {state, reference, row} = entry;
    state.className = 'progress-file';
    if (!result) { state.textContent = '확인 불가'; state.classList.add('unknown'); return; }
    if (result.status === 'present') {
      state.textContent = reference.visibility === 'private' ? '이 PC 에 비공개 원본 있음' : '이 PC 에 있음';
      state.classList.add('present');
      const W = window.AionaWorkspace;
      if (reference.kind === 'saved_run' && reference.run_id && W?.openRun) {
        const button = append(row, 'button', 'progress-mini-button', '결과 화면에서 열기');
        button.type = 'button';
        button.addEventListener('click', () => W.openRun(reference.run_id));
      }
    } else if (result.status === 'missing') {
      state.textContent = reference.visibility === 'private' ? '자료 없음 — 이 PC 에 비공개 원본이 없어 공개 요약만 표시' : '자료 없음 — 파일을 찾지 못함';
      state.classList.add('missing');
    } else {
      state.textContent = '잘못된 경로';
      state.classList.add('invalid');
    }
  }
  async function checkFiles(entries) {
    const unique = [...new Set(entries.map(entry => entry.path).filter(Boolean))];
    let results = {};
    if (unique.length) {
      try {
        const response = await fetch('/api/progress/files?' + unique.map(path => 'path=' + encodeURIComponent(path)).join('&'), {cache: 'no-store'});
        if (response.ok) results = Object.fromEntries(((await response.json()).files || []).map(file => [file.path, file]));
      } catch (_) { results = {}; }
    }
    for (const entry of entries) applyFileStatus(entry, entry.path ? results[entry.path] : {status: 'invalid'});
  }
  function artifactCard(parent, artifact, files) {
    const card = append(parent, 'article', 'progress-artifact');
    card.id = 'artifact-' + artifact.artifact_id;
    const head = append(card, 'div', 'progress-artifact-head');
    append(head, 'span', 'progress-type ' + artifact.type, TYPES[artifact.type] || artifact.type);
    const level = artifact.verification_status?.level || 'none';
    append(head, 'span', 'progress-level ' + level, LEVELS[level] || level);
    if (!MEASURED.has(level)) append(head, 'span', 'progress-level unmeasured', '실측 미완료');
    if (artifact.data_source?.visibility === 'private') append(head, 'span', 'progress-level private', '비공개 원본 · 공개 요약');
    append(card, 'h4', '', artifact.title);
    append(card, 'p', 'progress-muted', artifact.description);
    append(card, 'p', 'progress-result', artifact.result_summary);
    if (artifact.verification_status?.note) append(card, 'p', 'progress-level-note', '검증 수준: ' + artifact.verification_status.note);
    renderContent(append(card, 'div', 'progress-artifact-body'), artifact);
    if ((artifact.limitations || []).length) list(block(card, '한계'), artifact.limitations);
    const meta = append(card, 'div', 'progress-artifact-meta');
    pairs(meta, [{label: '산출물 ID', value: artifact.artifact_id}, {label: '자료 출처', value: artifact.data_source?.label},
      {label: '결과 시각', value: artifact.created_at || '기록 없음'}, {label: '코드 커밋', value: artifact.code_revision}]);
    const refs = append(meta, 'ul', 'progress-references');
    for (const reference of artifact.file_or_result_reference || []) referenceRow(refs, reference, files);
    return card;
  }

  // --- 요구사항 카드 ------------------------------------------------------------------------
  function sources(artifacts) {
    return [...new Set(artifacts.map(a => a.data_source?.run_id ? `실행 ${a.data_source.run_id}` : a.data_source?.label).filter(Boolean))];
  }
  function versionsOf(artifacts) {
    const out = [];
    for (const a of artifacts) {
      const s = a.data_source || {};
      if (s.generator) out.push(`${a.artifact_id}: 질문 생성 ${s.generator} (${s.generator_model || '모델 기록 없음'})`);
      if (s.models) out.push(`${a.artifact_id}: 모델 ` + Object.entries(s.models).map(([k, v]) => `${k} ${v}`).join(', '));
      if (s.criteria_version) out.push(`${a.artifact_id}: 평가 기준 ${s.criteria_version} · 적용 ${s.applicability_version || '—'} · ${s.pipeline || ''}`.trim());
      if (a.content?.version && a.type === 'prompt') out.push(`${a.artifact_id}: ${a.content.version}`);
    }
    return [...new Set(out)];
  }
  function evidenceRows(parent, item, artifacts) {
    const v = item.verification || {};
    const runs = [...new Set((v.runs || []).concat(artifacts.map(a => a.data_source?.run_id ? `${a.artifact_id}: 실행 ${a.data_source.run_id}` : null).filter(Boolean)))];
    const rows = [
      ['관련 테스트', v.tests || []],
      ['결과 데이터·실행 ID', runs],
      ['사용 모델·평가 기준 버전', versionsOf(artifacts)],
      ['실행·결과 시각', artifacts.filter(a => a.created_at).map(a => `${a.artifact_id}: ${a.created_at}`)],
      ['코드 커밋', [...new Set(artifacts.map(a => `${a.artifact_id}: ${a.code_revision}`))]],
      ['실제 검수', v.human_review ? [v.human_review] : []],
      ['자료 출처', [item.meeting?.basis, ...artifacts.map(a => `${a.artifact_id}: ${a.data_source?.label || '—'}`)].filter(Boolean)]];
    const table = append(append(parent, 'div', 'progress-table-scroll'), 'table', 'progress-table progress-evidence');
    const body = append(table, 'tbody');
    for (const [label, values] of rows) {
      const tr = append(body, 'tr');
      append(tr, 'th', '', label).scope = 'row';
      const td = append(tr, 'td');
      if (!values.length) append(td, 'span', 'progress-unverified', '미확인');
      else list(td, values);
    }
  }
  function artifactOf(artifacts, id) { return artifacts.find(artifact => artifact.artifact_id === id); }
  function statOf(artifact, label) { return artifact?.content?.stats?.find(stat => stat.label === label)?.value; }
  function verificationLine(parent, item) {
    const line = append(parent, 'div', 'progress-verification-line');
    append(line, 'strong', '', '검증');
    for (const [key, label] of STAGES) {
      const stage = item.stages?.[key] || {state: 'none'};
      const span = append(line, 'span', 'progress-verification-step ' + (stage.state || 'none'), `${label} ${STATES[stage.state] || '미확인'}`);
      span.title = stage.note || '';
    }
    return line;
  }
  function summaryMetric(parent, label, value, note) {
    const metric = append(parent, 'div', 'progress-focus-metric');
    append(metric, 'span', '', label);
    append(metric, 'strong', '', fmt(value));
    if (note) append(metric, 'small', '', note);
  }
  function executiveSummary(parent, item, artifacts) {
    const box = append(parent, 'section', 'progress-executive');
    const top = append(box, 'div', 'progress-executive-top');
    append(top, 'span', 'progress-badge ' + groupOf(item), item.status);
    append(top, 'span', '', `담당 ${item.owner} · ${item.priority}`);
    if (item.role) append(box, 'p', 'progress-role', '이 화면의 역할 · ' + item.role);
    const prompt = artifactOf(artifacts, 'W01-PROMPT');
    const generated = artifactOf(artifacts, SET_ARTIFACT);
    const intent = artifactOf(artifacts, 'W01-INTENT');
    if (item.id === 'W01' && prompt && generated && intent) {
      const count = statOf(generated, '질문(대화)');
      const rate = statOf(intent, '적용률');
      const generatedVersion = versionOf(generated.data_source.generator);
      const promptVersion = versionOf(prompt.content.version);
      const topMiss = [...(intent.content.families || [])].sort((a, b) => b.missed - a.missed).find(row => row.missed > 0);
      append(box, 'h3', '', `평가 기준을 하나하나 시험할 질문을 GPT 로 만드는 방법(질문 생성기 ${promptVersion})을 정했습니다. 이전 판 ${generatedVersion}으로 실제 만든 질문 ${fmt(count)}개에서는 시험하려던 기준 중 ${rate}만 실제로 적용됐습니다.${topMiss ? ` 가장 많이 빗나간 기준은 ${topMiss.family}(${topMiss.name}) ${fmt(topMiss.missed)}건입니다.` : ''}`);
      const metrics = append(box, 'div', 'progress-focus-metrics');
      summaryMetric(metrics, '실제로 만든 질문', count, `이전 판 ${generatedVersion} · GPT 실제 생성`);
      summaryMetric(metrics, '시험하려던 기준이 실제 적용된 비율', rate, `${generatedVersion} · AI 정답률 아님`);
      summaryMetric(metrics, 'J6(긴 대화) 빼면', statOf(intent, 'J6 제외 적용률'), `${generatedVersion} · J6 제외 집계`);
      summaryMetric(metrics, `현재 ${promptVersion} 실제 생성`, '미실행', '성능 검증 대기 · 모의 테스트만');
      append(box, 'p', 'progress-source-line', `자료: ${generated.data_source.set || '질문 세트'} (${generatedVersion}, 실행 ${generated.data_source.run_id || '기록 없음'}, ${generated.created_at || '시각 기록 없음'}) · 현재 코드 ${promptVersion} 는 실제 생성 전`);
      const nav = append(box, 'nav', 'w01-nav');
      nav.setAttribute('aria-label', 'W01 설명 순서');
      W01_STEPS.forEach(([number, title], index) => {
        const button = append(nav, 'button', 'progress-mini-button', `${number} ${title}`);
        button.type = 'button';
        button.addEventListener('click', () => box.parentElement?.querySelector('#w01-step-' + (index + 1))?.scrollIntoView({block: 'start'}));
      });
    } else {
      append(box, 'h3', '', item.interpretation?.result || artifacts[0]?.result_summary || item.gap || '확인된 결과가 없습니다.');
      const primary = artifacts.find(a => a.content?.stats?.length || a.content?.outputs?.some(o => typeof o.value === 'number'));
      const values = (primary?.content?.stats || primary?.content?.outputs || []).filter(v => typeof v.value === 'number').slice(0, 3);
      if (values.length) {
        const metrics = append(box, 'div', 'progress-focus-metrics');
        for (const value of values) summaryMetric(metrics, value.label, value.value, primary?.title);
      }
      if (artifacts.length) {
        const source = artifacts[0];
        const historical = Boolean(source.data_source?.history);
        append(box, 'p', 'progress-source-line', `${historical ? '이전 실행 결과 · ' : ''}${source.data_source?.label || '자료 출처 확인 필요'} · 실행 ${source.data_source?.run_id || '기록 없음'} · 기준 시점 ${source.created_at || '기록 없음'}`);
      }
    }
    verificationLine(box, item);
    return box;
  }
  function intentBar(parent, intent, generatedVersion) {
    const applied = Number(statOf(intent, '실제 적용'));
    const missed = Number(statOf(intent, '적용 안 됨'));
    const pending = Number(statOf(intent, '답변 필요'));
    const target = Number(statOf(intent, '노린 항목(건)'));
    const judged = applied + missed;
    const main = append(parent, 'figure', 'progress-key-chart');
    append(main, 'figcaption', '', '생성기가 목표로 한 평가 항목이 실제로 얼마나 적용됐는가?');
    const bar = append(main, 'div', 'progress-intent-bar');
    bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', `판단 가능 ${judged}건 중 실제 적용 ${applied}건, 미적용 ${missed}건. 별도 판단 보류 ${pending}건.`);
    const yes = append(bar, 'span', 'applied'); yes.style.width = `${applied / judged * 100}%`;
    const no = append(bar, 'span', 'missed'); no.style.width = `${missed / judged * 100}%`;
    const legend = append(main, 'div', 'progress-intent-legend');
    append(legend, 'strong', 'applied', `실제 적용 ${fmt(applied)}건 · ${(applied / judged * 100).toFixed(1)}%`);
    append(legend, 'strong', 'missed', `미적용 ${fmt(missed)}건 · ${(missed / judged * 100).toFixed(1)}%`);
    append(main, 'p', 'progress-chart-interpretation', `${generatedVersion} 질문 세트에서 노린 ${fmt(target)}건 중 ${fmt(pending)}건은 답변이 있어야 판단할 수 있어 제외했습니다. 비율의 분모는 판단 가능 ${fmt(judged)}건입니다. 이는 평가 기준 적용 규칙의 일치율이며 사람 검수 정확도나 모델 평가 성능은 아닙니다.`);
    return main;
  }
  // --- W01 설명 화면: ① 왜 → ② 원리 → ③ 실제 질문 → ④ 잘 작동했나 → ⑤ 개선 (문장·수치는 artifacts.json 그대로) ------------
  const BASIS = {'근거 있음': 'evidence', '사용자 결정': 'decision', '멘토 요청': 'decision', '가정': 'assumption'};
  const W01_STEPS = [['①', '왜 질문을 만들었나요?'], ['②', '어떤 원리로 만들었나요?'], ['③', '실제로 어떤 질문을 만들었나요?'],
    ['④', '실제로 잘 작동했나요?'], ['⑤', '무엇을 개선해야 하나요?']];
  function versionOf(text) { return String(text || '').match(/v\d+(?:\.\d+)+/)?.[0] || '버전 미기록'; }
  function storySection(parent, index, lead) {
    const [number, title] = W01_STEPS[index];
    const box = append(parent, 'section', 'w01-step');
    box.id = 'w01-step-' + (index + 1);
    const head = append(box, 'div', 'w01-step-head');
    append(head, 'span', 'w01-step-number', number);
    append(head, 'h3', '', title);
    if (lead) append(box, 'p', 'w01-lead', lead);
    return box;
  }
  function more(parent, label) {
    const details = append(parent, 'details', 'w01-more');
    append(details, 'summary', '', label);
    return details;
  }
  function storyWhy(parent, design) {
    const why = design.content.why;
    const box = storySection(parent, 0, why.lead);
    const methods = append(box, 'div', 'w01-methods');
    for (const method of why.methods) {
      const card = append(methods, 'div', 'w01-method');
      append(card, 'strong', '', method.name);
      append(card, 'span', 'w01-method-who', method.who);
      pairs(card, [{label: '보는 것', value: method.checks}, {label: '장점', value: method.good}, {label: '한계', value: method.limit}]);
    }
    append(box, 'p', 'w01-note', why.together);
    list(box, [why.by_criterion, why.multi]).classList.add('w01-points');
    const glossary = design.content.glossary;
    append(box, 'h4', 'w01-subhead', `시험하는 평가 기준 ${glossary.length}개 — CODE 검사 ${glossary.filter(g => g.layer === 'CODE').length}개 · 의미 평가 ${glossary.filter(g => g.layer !== 'CODE').length}개`);
    const grid = append(box, 'div', 'w01-glossary');
    for (const term of glossary) {
      const card = append(grid, 'div', 'w01-term ' + (term.layer === 'CODE' ? 'code' : 'llm'));
      const head = append(card, 'div', 'w01-term-head');
      append(head, 'span', 'w01-term-id', term.id);
      append(head, 'strong', '', term.name);
      append(card, 'p', '', term.easy);
    }
    dataTable(more(box, '기준별 자세히 — 어떤 질문이면 적용되고, 어떤 답이 FAIL 인가'), {columns: [{key: 'id', label: '기준'}, {key: 'name', label: '이름'},
      {key: 'easy', label: '무엇을 확인하나'}, {key: 'applies', label: '어떤 질문이면 적용되나'}, {key: 'fail', label: '어떤 답이 FAIL 인가'},
      {key: 'official', label: '공식 정의(기준 목록)'}, {key: 'size', label: '세부 개수'}], rows: glossary}, '평가 기준 쉬운 설명');
    const numbers = design.content.numbers;
    const box2 = more(box, `화면에 나오는 기준 수 ${numbers.map(n => n.number).join(' · ')} 는 무엇인가요?`);
    append(box2, 'p', 'progress-muted', '세는 단계가 달라 숫자가 다릅니다. 위에서 아래로 갈수록 더 잘게 나눈 단위입니다.');
    const rows = append(box2, 'div', 'w01-numbers');
    for (const entry of numbers) {
      const row = append(rows, 'div', 'w01-number');
      append(row, 'strong', '', fmt(entry.number));
      const copy = append(row, 'div');
      append(copy, 'b', '', `${entry.what} · ${entry.level}`);
      append(copy, 'p', '', entry.meaning);
      append(copy, 'small', '', '나오는 곳: ' + entry.where);
    }
  }
  function storyHow(parent, design, quantity) {
    const box = storySection(parent, 1, '평가 기준 → 시험할 기준 나누기 → GPT 에 질문 요청 → 평가기 규칙으로 점검 → 부족한 기준 보충. 단계마다 왜 필요한지와 실제 결과를 함께 적었습니다.');
    const flow = append(box, 'ol', 'w01-flow');
    for (const step of design.content.flow) {
      const li = append(flow, 'li', 'w01-flow-step');
      append(li, 'span', 'w01-flow-number', step.step);
      const copy = append(li, 'div');
      append(copy, 'strong', '', step.title);
      append(copy, 'p', '', step.why);
      const result = append(copy, 'p', 'w01-flow-result');
      append(result, 'span', 'w01-tag ' + step.state, step.state === 'real' ? '실제 결과' : '코드 설정');
      result.append(' ' + step.result);
    }
    const batch = quantity.content.batch;
    const estimate = batch.estimates.find(entry => entry.questions === batch.value);
    const why = append(box, 'div', 'w01-callout');
    append(why, 'h4', '', `왜 한 번에 ${batch.value}개씩 나눠 만드나요?`);
    append(why, 'p', '', `GPT 는 한 번에 쓸 수 있는 답의 길이에 한도가 있어, 300개를 한 번에 요청하면 중간에 잘릴 수 있습니다. 그래서 한 번에 최대 ${batch.value}개씩 나눠 요청합니다.`);
    append(why, 'p', '', `${batch.value} 은 ${batch.introduced.at || '시각 기록 없음'} 커밋 ${batch.introduced.commit}(${batch.introduced.author})에서 정한 값입니다. 질문 1개 계획은 약 ${fmt(batch.per_question.mean)}토큰(추정)이라 ${batch.value}개는 약 ${fmt(estimate?.tokens)}토큰 — 한 번 호출의 출력 상한 ${fmt(batch.plan_tokens)}토큰의 ${Math.round((estimate?.share || 0) * 100)}% 입니다. 생성 모델 자체의 출력 한도는 ${batch.model_limit.split(' — ')[0]}.`);
    append(why, 'p', '', '실제로 생긴 문제는 길이보다 개수와 형식이었습니다:');
    list(why, batch.incidents);
    append(why, 'p', 'w01-verdict', `결론: ${batch.verdict}. ${batch.measured}.`);
    append(box, 'h4', 'w01-subhead', '헷갈리기 쉬운 질문 수 네 가지 — 서로 다른 숫자입니다');
    const four = append(box, 'div', 'w01-four');
    for (const entry of quantity.content.four_numbers) {
      const card = append(four, 'div', 'w01-four-card');
      append(card, 'span', 'w01-tag ' + (typeof entry.value === 'number' ? 'code' : 'open'), entry.kind);
      append(card, 'strong', '', typeof entry.value === 'number' ? `${fmt(entry.value)}개` : entry.value);
      append(card, 'b', '', entry.name);
      append(card, 'p', '', entry.what);
      append(card, 'small', '', '근거: ' + entry.basis);
      append(card, 'small', 'w01-verified', entry.verified);
    }
    const parts = design.content.prompt_parts;
    const checklist = design.content.prompt_sections.find(section => section.heading.startsWith('점검 목록'));
    append(box, 'h4', 'w01-subhead', `요청문(프롬프트)에 넣은 것 ${parts.length}가지와 이유`);
    append(box, 'p', 'progress-muted', `GPT 에 보내는 첫 요청문 ${fmt(design.content.prompt_chars)}자 중 ${fmt(checklist?.chars)}자(${Math.round((checklist?.chars || 0) / design.content.prompt_chars * 100)}%)는 평가 기준 정의를 그대로 옮긴 '점검 목록'입니다. 기준 목록이 바뀌면 요청문도 자동으로 바뀝니다. 표시: 근거 있음 = 실제 사례·결과가 있음 · 사용자 결정/멘토 요청 = 회의·결정 기록 · 가정 = 실험 근거 없음.`);
    const grid = append(box, 'div', 'w01-parts');
    for (const part of parts) {
      const card = append(grid, 'article', 'w01-part');
      const head = append(card, 'div', 'w01-part-head');
      append(head, 'strong', '', part.name);
      append(head, 'span', 'w01-basis ' + (BASIS[part.basis] || 'assumption'), part.basis);
      append(card, 'p', '', part.why);
      append(card, 'p', 'w01-quote', part.excerpt);
      append(card, 'small', '', part.evidence);
    }
    const builders = design.content.prompt_builders;
    dataTable(more(box, `요청문은 몇 종류인가요? — ${builders.length}종 (원문은 아래 '기술 자료 및 원본 보기')`), {columns: [{key: 'role', label: '역할'},
      {key: 'when', label: '언제 보내나'}, {key: 'shown', label: '원문 보관'}, {key: 'name', label: '코드 함수'}], rows: builders}, '질문 생성 요청문 종류');
  }
  function targetChips(parent, targets) {
    const box = append(parent, 'div', 'w01-targets');
    const merged = new Map();   // 같은 세부 기준 이름(J6 정보 기억 a·b·c)은 한 칩으로
    for (const target of targets || []) {
      const previous = merged.get(target.name);
      merged.set(target.name, {name: target.name, applied: previous ? previous.applied && target.applied : target.applied});
    }
    for (const target of merged.values()) {
      const chip = append(box, 'span', 'w01-chip ' + (target.applied ? 'applied' : 'missed'));
      append(chip, 'i', '', target.applied ? '적용' : '미적용');
      chip.append(' ' + target.name);
    }
  }
  function caseLine(parent, label, value, extra) {
    const row = append(parent, 'div', 'w01-line' + (extra ? ' ' + extra : ''));
    append(row, 'b', '', label);
    append(row, 'span', '', value || '기록 없음');
  }
  function caseHead(card, item) {
    const head = append(card, 'div', 'w01-case-head');
    append(head, 'span', 'w01-layer ' + (item.layer === 'CODE' ? 'code' : 'llm'), item.layer === 'CODE' ? 'CODE 검사' : '의미 평가');
    append(head, 'strong', '', item.title);
    append(card, 'small', 'w01-case-meta', `Q${item.id} · ${item.type || '유형 없음'} · 난도 ${item.difficulty || '—'}${item.turns_total > 1 ? ` · ${item.turns_total}턴 대화` : ''} · 이 질문에 실제 적용된 세부 기준 ${item.applied_total}개`);
  }
  function questionCase(grid, item) {
    const card = append(grid, 'article', 'w01-case');
    caseHead(card, item);
    const quote = append(card, 'blockquote', 'w01-question', item.question);
    if (item.question.length > 120) {
      quote.classList.add('clamp');
      const toggle = append(card, 'button', 'progress-mini-button w01-toggle', '질문 전체 보기');
      toggle.type = 'button';
      toggle.addEventListener('click', () => { toggle.textContent = quote.classList.toggle('clamp') ? '질문 전체 보기' : '접기'; });
    }
    if (item.attachments) append(card, 'small', 'w01-case-meta', '첨부: ' + item.attachments.join(', '));
    append(card, 'h5', '', '확인할 기준 (평가기 규칙으로 점검한 결과)');
    targetChips(card, item.targets);
    caseLine(card, '왜 만들었나', item.why);
    caseLine(card, 'FAIL 인 답', item.fail, 'fail');
    if (item.contrast) {
      const contrast = append(card, 'div', 'w01-contrast');
      append(contrast, 'strong', '', `같은 목표인데 평가기가 알아보지 못한 질문 — 계산을 노린 ${item.contrast.total}개 중 ${item.contrast.missed}개`);
      append(contrast, 'p', '', `Q${item.contrast.id} “${item.contrast.question}”`);
      append(contrast, 'small', '', '평가기는 정해진 문장 모양에서만 계산 요구를 읽습니다. 이런 질문에는 CODE 계산 검사가 켜지지 않습니다(④ CODE 3 미적용의 주요 원인).');
    }
  }
  function bubble(list, turn, label) {
    const li = append(list, 'li', 'w01-bubble' + (label ? ' marked' : ''));
    append(li, 'span', 'w01-turn', `${turn.turn}턴${label ? ' · ' + label : ''}`);
    append(li, 'p', '', turn.question);
    if (turn.pasted_chars) append(li, 'small', '', `질문 앞에 붙여 넣은 자료 ${fmt(turn.pasted_chars)}자는 생략했습니다.`);
  }
  function conversationCase(grid, item) {
    const card = append(grid, 'article', 'w01-case w01-case-wide');
    caseHead(card, item);
    caseLine(card, '대화 목적', `주제 '${item.topic}' — ${item.why}`);
    append(card, 'h5', '', '확인할 기준 (평가기 규칙으로 점검한 결과)');
    targetChips(card, item.targets);
    append(card, 'h5', '', `대표 턴 ${item.highlights.length}개 (전체 ${item.turns_total}턴)`);
    const chat = append(card, 'ol', 'w01-chat');
    for (const pick of item.highlights) bubble(chat, item.turns[pick.index], pick.label);
    caseLine(card, 'FAIL 인 답', item.fail, 'fail');
    const timeline = append(more(card, `전체 ${item.turns_total}턴 보기`), 'ol', 'w01-timeline');
    const labels = Object.fromEntries(item.highlights.map(pick => [pick.index, pick.label]));
    item.turns.forEach((turn, index) => bubble(timeline, turn, labels[index]));
  }
  function storyCases(parent, generated) {
    const cases = generated.content.cases || [];
    const box = storySection(parent, 2, `이전 판으로 실제 만든 질문 ${fmt(statOf(generated, '질문(대화)'))}개 중 대표 ${cases.length}개입니다. 질문은 원문 그대로이고, 기준 옆 '적용·미적용'은 평가기와 같은 규칙으로 점검한 결과입니다.`);
    const grid = append(box, 'div', 'w01-cases');
    for (const item of cases) (item.turns ? conversationCase : questionCase)(grid, item);
  }
  function legendItem(parent, kind, title, note) {
    const item = append(parent, 'div', 'w01-legend-item');
    append(item, 'i', kind);
    append(item, 'strong', '', title);
    if (note) append(item, 'small', '', note);
  }
  function segment(bar, kind, value, total) {
    const part = append(bar, 'span', kind);
    part.style.width = `${total ? value / total * 100 : 0}%`;
    return part;
  }
  function openRequirementCard(id) {
    const target = document.getElementById('requirement-' + id);
    if (target) { target.open = true; target.scrollIntoView({block: 'start'}); }
  }
  function storyWorked(parent, intent, quantity, prompt, generated) {
    const f = intent.content.funnel;
    const rate = statOf(intent, '적용률');
    const version = versionOf(generated.data_source.generator);
    const box = storySection(parent, 3, `이전 판(${version})으로 만든 질문 ${fmt(statOf(generated, '질문(대화)'))}개를 평가기와 같은 규칙으로 점검했습니다. AI 에게 답을 받아 채점한 결과가 아닙니다.`);
    const figure = append(box, 'figure', 'w01-figure');
    append(figure, 'figcaption', '', '생성한 질문이 의도한 평가 기준에 실제로 적용됐을까?');
    append(figure, 'p', 'progress-muted', `GPT 가 질문마다 '이 기준을 시험한다'고 적은 목표는 모두 ${fmt(f.intended)}건입니다. 그중 ${fmt(f.answer_dependent)}건은 AI 의 답을 받아야 적용 여부를 알 수 있어 따로 두고, 나머지 ${fmt(f.judged)}건을 확인했습니다.`);
    const bar = append(figure, 'div', 'w01-funnel');
    bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', `목표 ${f.intended}건 중 실제 적용 ${f.applied}건, 미적용 ${f.missed}건, 판단 보류 ${f.answer_dependent}건. 적용률은 ${f.applied} ÷ ${f.judged}.`);
    segment(bar, 'applied', f.applied, f.intended);
    segment(bar, 'missed', f.missed, f.intended);
    segment(bar, 'pending', f.answer_dependent, f.intended);
    const legendBox = append(figure, 'div', 'w01-legend');
    legendItem(legendBox, 'applied', `실제 적용 ${fmt(f.applied)}건`, `판단 가능 ${fmt(f.judged)}건 중 ${rate}`);
    legendItem(legendBox, 'missed', `미적용 ${fmt(f.missed)}건`, `판단 가능 ${fmt(f.judged)}건 중 ${(f.missed / f.judged * 100).toFixed(1)}%`);
    legendItem(legendBox, 'pending', `판단 보류 ${fmt(f.answer_dependent)}건`, '답을 받아야 알 수 있어 비율 계산에서 뺌');
    const meaning = append(figure, 'div', 'w01-not');
    append(meaning, 'strong', '', `${rate} 는 무엇을 뜻하나요?`);
    list(meaning, ['AI 의 정답률이 아닙니다 — 이 질문들의 답은 아직 채점하지 않았습니다.', '질문 품질 점수가 아닙니다 — 질문이 좋은지는 사람이 검수해야 합니다.',
      `'시험하려던 기준이 평가기에서 실제로 켜진 비율'입니다 — 실제 적용 ${fmt(f.applied)} ÷ 판단 가능 ${fmt(f.judged)}.`]);

    const j6 = intent.content.j6;
    const j6row = intent.content.families.find(row => row.family === 'J6');
    if (j6row?.missed) {   // J6 를 짧은 대화에 노려 빗나간 건이 있을 때만
      const cause = append(box, 'div', 'w01-j6');
      append(cause, 'h4', '', `가장 큰 원인 — J6 · ${j6row.name}: 미적용 ${fmt(f.missed)}건 중 ${fmt(j6row.missed)}건`);
      const j6bar = append(cause, 'div', 'w01-j6-bar');
      j6bar.setAttribute('role', 'img');
      j6bar.setAttribute('aria-label', `J6 를 노린 목표 ${j6.targets}건 — ` + j6.bins.map(bin => `${bin.label} ${bin.targets}건 중 적용 ${bin.applied}건`).join(', '));
      for (const bin of j6.bins) segment(j6bar, bin.targets && bin.applied === bin.targets ? 'applied' : 'missed', bin.targets, j6.targets);
      const j6legend = append(cause, 'div', 'w01-legend');
      for (const bin of j6.bins) legendItem(j6legend, bin.targets && bin.applied === bin.targets ? 'applied' : 'missed', `${bin.label} ${fmt(bin.conversations)}개`, `J6 목표 ${fmt(bin.targets)}건 중 적용 ${fmt(bin.applied)}건`);
      const compare = append(cause, 'div', 'w01-compare');
      const made = append(compare, 'div');
      append(made, 'b', '', `질문을 만든 쪽 — 생성기 ${version}`);
      append(made, 'p', '', j6.where);
      const judge = append(compare, 'div');
      append(judge, 'b', '', '평가하는 쪽 — 평가기의 J6 적용 조건');
      append(judge, 'p', '', `${j6.rule}. 짧은 대화는 J6 를 묻지 않습니다(해당 없음).`);
      append(cause, 'p', 'w01-verdict', `즉 AI 답변이 나빠서 생긴 문제가 아니라, 질문을 만든 조건과 평가하는 조건이 달라서 생긴 문제입니다. J6 를 빼고 계산하면 적용률은 ${statOf(intent, 'J6 제외 적용률')} 입니다.`);
    }

    append(box, 'h4', 'w01-subhead', '기준별로 보면 — 빗나간 기준과 주요 원인');
    const rows = intent.content.families.filter(row => row.missed > 0).sort((a, b) => b.missed - a.missed);
    const scroll = append(box, 'div', 'w01-mini-scroll');
    const table = append(scroll, 'table', 'w01-mini');
    append(table, 'caption', '', '기준별 시험하려던 건수·실제 적용·미적용·주요 원인');
    const head = append(append(table, 'thead'), 'tr');
    for (const [label, numeric] of [['기준', false], ['시험하려던 건수', true], ['실제 적용', true], ['미적용', true], ['주요 원인', false]]) append(head, 'th', numeric ? 'num' : '', label).scope = 'col';
    const body = append(table, 'tbody');
    for (const row of rows) {
      const tr = append(body, 'tr');
      const name = append(append(tr, 'td'), 'div', 'w01-mini-name');
      append(name, 'strong', '', `${row.family} · ${row.name}`);
      const mini = append(name, 'div', 'w01-mini-bar');
      mini.setAttribute('aria-hidden', 'true');
      segment(mini, 'applied', row.applied, row.applied + row.missed);
      segment(mini, 'missed', row.missed, row.applied + row.missed);
      for (const [key, label] of [['intended', '시험하려던'], ['applied', '적용'], ['missed', '미적용']]) append(tr, 'td', 'num', fmt(row[key])).dataset.label = label;
      append(tr, 'td', 'w01-mini-cause', row.cause);
    }
    const clean = intent.content.families.filter(row => row.missed === 0);
    append(box, 'p', 'progress-muted', `빗나간 건 없음: ${clean.map(row => `${row.family} ${row.name}(${row.applied}건)`).join(' · ')} — 시험하려던 건수가 적은 기준도 있어 100% 를 질문 품질로 읽지 않습니다. '시험하려던 건수'에는 답을 받아야 알 수 있는 판단 보류 건이 들어 있어 적용+미적용보다 클 수 있습니다.`);

    const growth = quantity.content.table.rows;
    const targets = quantity.content.growth_targets;
    const at50 = growth.find(row => row.questions === 50);
    const last = growth[growth.length - 1];
    const grow = append(box, 'div', 'w01-growth');
    append(grow, 'h4', 'w01-subhead', '질문을 많이 만들면 기준이 계속 채워질까요?');
    for (const row of growth.filter(r => [10, 50, 100, last.questions].includes(r.questions))) {
      const line = append(grow, 'div', 'w01-growth-row');
      append(line, 'strong', '', `${fmt(row.questions)}개`);
      const track = append(line, 'div', 'w01-growth-track');
      track.setAttribute('role', 'img');
      track.setAttribute('aria-label', `질문 ${row.questions}개로 기준 ${row.order_k1}/${targets}개가 한 번 이상 시험됨`);
      append(track, 'span').style.width = `${row.order_k1 / targets * 100}%`;
      append(line, 'span', '', `기준 ${row.order_k1}/${targets}개 시험 (무작위로 고르면 평균 ${row.random_k1})`);
    }
    append(grow, 'p', 'progress-chart-interpretation', `처음 50개로 기준 ${at50.order_k1}개가 한 번 이상 시험됐고, 나머지 ${fmt(last.questions - 50)}개가 더한 것은 ${last.order_k1 - at50.order_k1}개뿐입니다. 다섯 번 이상 시험된 기준은 ${fmt(last.questions)}개에서도 ${last.order_k5}개입니다. 그래서 '질문 수'보다 '기준마다 몇 번 시험되는지'를 목표로 만들어야 합니다(v1.4). 300개는 지난 실험에서 정한 수일 뿐 적정 수로 검증된 값이 아닙니다.`);
    const w03 = append(grow, 'button', 'progress-mini-button', '몇 개가 필요한지 — W03 질문 수 실험 보기');
    w03.type = 'button';
    w03.addEventListener('click', () => openRequirementCard('W03'));
    append(box, 'p', 'w01-pending', `현재 생성기 ${versionOf(prompt?.content?.version)}: 성능 검증 대기 — 구현과 모의 테스트만 확인했고 실제 생성 실험은 아직 없습니다. 위 수치는 모두 이전 판 ${version} 결과입니다.`);

    const raw = more(box, '자세한 데이터 보기 — 기준별 전체 수치 · J6 나눔 · 질문 수별 기준 증가');
    dataTable(raw, intent.content.table, intent.title);
    dataTable(raw, {columns: [{key: 'label', label: 'J6 를 노린 질문'}, {key: 'conversations', label: '질문·대화 수', numeric: true},
      {key: 'targets', label: 'J6 목표 건수', numeric: true}, {key: 'applied', label: '실제 적용', numeric: true}], rows: j6.bins}, 'J6 목표를 대화 길이로 나눈 표');
    dataTable(raw, quantity.content.table, quantity.title);
  }
  function storyImprove(parent, design) {
    const box = storySection(parent, 4, '아직 확인하지 못한 것과 다음에 확인할 방법입니다. 유료 호출이 필요한 확인은 승인 뒤에 합니다.');
    const items = append(box, 'ol', 'w01-improve');
    for (const entry of design.content.improve) {
      const li = append(items, 'li', 'w01-improve-item');
      append(li, 'strong', '', entry.title);
      append(li, 'p', '', entry.problem);
      append(li, 'p', 'w01-next', '다음 확인: ' + entry.next);
    }
  }
  function w01Story(parent, artifacts) {
    const design = artifactOf(artifacts, 'W01-DESIGN');
    const intent = artifactOf(artifacts, 'W01-INTENT');
    const generated = artifactOf(artifacts, SET_ARTIFACT);
    const quantity = artifactOf(artifacts, 'W01-QUANTITY');
    if (!design || !intent?.content?.funnel || !generated?.content?.cases || !quantity) return false;
    const story = append(parent, 'div', 'w01-story');
    storyWhy(story, design);
    storyHow(story, design, quantity);
    storyCases(story, generated);
    storyWorked(story, intent, quantity, artifactOf(artifacts, 'W01-PROMPT'), generated);
    storyImprove(story, design);
    return true;
  }
  function highlightedArtifacts(item, artifacts) {
    const preferred = item.id === 'W02' ? ['W02-MATRIX', 'W02-CRITERIA'] : item.id === 'W03' ? ['W03-COVERAGE', 'W03-STABILITY'] : [];
    if (preferred.length) return preferred.map(id => artifactOf(artifacts, id)).filter(artifact => artifact?.content?.charts?.length);
    return artifacts.filter(artifact => artifact.content?.charts?.length).slice(0, 1);
  }
  function keyResults(parent, item, artifacts) {
    const box = append(parent, 'section', 'progress-key-results');
    append(box, 'h3', '', '주요 결과와 시각화');
    const selected = highlightedArtifacts(item, artifacts);
    if (selected.length) {
      for (const artifact of selected) {
        chartBlock(box, artifact.content.charts[0]);
        const historical = Boolean(artifact.data_source?.history);
        append(box, 'p', 'progress-source-line', `${historical ? '이전 실행 결과 · ' : ''}${artifact.data_source?.label || '자료 출처 확인 필요'} · 실행 ${artifact.data_source?.run_id || '기록 없음'} · ${artifact.created_at || '시각 기록 없음'}`);
      }
      append(box, 'p', 'progress-chart-interpretation', item.interpretation?.meaning || selected[0].result_summary);
      if (item.interpretation?.gaps) append(box, 'p', 'progress-chart-limit', '남은 검증 · ' + item.interpretation.gaps);
    } else if (artifacts.length) {
      const summaries = append(box, 'div', 'progress-result-list');
      for (const artifact of artifacts.slice(0, 2)) {
        const row = append(summaries, 'div');
        append(row, 'strong', '', artifact.title);
        append(row, 'p', '', artifact.result_summary);
      }
      if (item.interpretation?.meaning) append(box, 'p', 'progress-chart-interpretation', item.interpretation.meaning);
    } else append(box, 'p', 'progress-no-artifact', '확인된 실제 산출물이 없습니다. 아래에서 필요한 증거와 다음 작업을 확인하세요.');
  }
  function representativeCases(parent, item, artifacts) {
    const sampleArtifact = artifacts.find(artifact => artifact.content?.samples?.length);
    const runArtifact = artifacts.find(artifact => artifact.content?.log?.length);
    if (!sampleArtifact && !runArtifact) return;
    const box = append(parent, 'section', 'progress-representatives');
    append(box, 'h3', '', '대표 실제 사례');
    if (sampleArtifact) {
      const samples = sampleArtifact.content.samples;
      const chosen = samples.slice(0, 5);
      renderSamples(box, chosen);
      if (samples.length > chosen.length) append(box, 'p', 'progress-muted', `대표 ${chosen.length}개를 먼저 표시했습니다. 나머지 사례와 전체 표는 '상세 산출물 전체 보기'에서 확인하세요.`);
    } else {
      const log = runArtifact.content.log.slice(0, 3);
      const cards = append(box, 'div', 'progress-representative-log');
      for (const row of log) {
        const card = append(cards, 'article', 'progress-sample');
        append(card, 'strong', '', `${row.case_id || '질문 ID 없음'} · ${row.status || '상태 미기록'}`);
        append(card, 'p', '', row.question || '질문 원문 없음');
        append(card, 'p', 'progress-muted', row.text || '답변·오류 기록 없음');
      }
      append(box, 'p', 'progress-muted', `실행 ${runArtifact.data_source?.run_id || 'ID 미기록'} · ${runArtifact.created_at || '시각 미기록'}의 공개 요약 일부입니다. 전체 기록은 상세 산출물에서 확인하세요.`);
    }
  }
  function management(parent, item, artifacts) {
    const metadata = append(parent, 'div', 'progress-meta');
    append(metadata, 'span', '', '담당 · ' + item.owner);
    append(metadata, 'span', '', '우선순위 · ' + item.priority);
    append(metadata, 'span', '', '검증 수준 · ' + item.level);
    if (item.status_change) {
      const note = append(parent, 'div', 'progress-callout');
      append(note, 'strong', '', `상태 정정: ${item.status_change.from} → ${item.status_change.to}`);
      append(note, 'p', '', item.status_change.reason);
    }
    if (item.evidence_correction) {
      const note = append(parent, 'div', 'progress-callout');
      append(note, 'strong', '', '이전 문서 근거 정정');
      append(note, 'p', '', item.evidence_correction);
    }
    const a = section(parent, 'A', '멘토 요구사항');
    const meeting = item.meeting || {};
    const grid = append(a, 'div', 'progress-fields');
    field(grid, '실제 미팅에서 요청한 내용', meeting.request || item.requirement);
    field(grid, '요청 날짜 · 회의 시각(녹음 기준)', (meeting.sessions || []).map(s => `${s.date} · ${s.times.join(', ')}`).join(' / ') || item.source);
    field(grid, '이 작업의 목적', item.purpose + (meeting.purpose ? ` (멘토가 밝힌 이유: ${meeting.purpose})` : ''));
    field(grid, '완료 인정 조건', item.acceptance);
    if (meeting.notes || meeting.confidence) append(a, 'p', 'progress-muted', `전사 확인 · 신뢰도 ${meeting.confidence || '—'}${meeting.notes ? ' · ' + meeting.notes : ''} · ${meeting.basis || ''}`);

    const c = section(parent, 'C', '결과 해석');
    const answers = item.interpretation || {};
    const qa = append(c, 'dl', 'progress-qa');
    for (const [key, question] of [['goal', '무엇을 확인하려고 했나?'], ['method', '어떤 방법으로 확인했나?'], ['result', '실제로 어떤 결과가 나왔나?'],
      ['meaning', '그 결과는 무엇을 뜻하나?'], ['gaps', '무엇이 아직 부족한가?']]) {
      append(qa, 'dt', '', question);
      append(qa, 'dd', '', answers[key] || '미확인');
    }
    evidenceRows(section(parent, 'D', '검증 근거'), item, artifacts);
    const rest = section(parent, '→', '남은 일');
    const restGrid = append(rest, 'div', 'progress-fields');
    field(restGrid, '현재 근거 요약', item.evidence);
    field(restGrid, '남은 문제', item.gap);
    field(restGrid, '다음 산출물', item.output);
    if (item.caution) field(restGrid, '주의 사항', item.caution);
    list(block(rest, '다음 작업'), item.next_steps, true);
    if (Array.isArray(item.links) && item.links.length) {
      const links = append(rest, 'div', 'progress-links');
      append(links, 'strong', '', '관련 근거');
      for (const entry of item.links) link(links, entry.label || 'GitHub 근거', entry.url);
    }
  }
  function detailedArtifacts(parent, item, artifacts, artifactState) {
    const b = section(parent, 'B', '실제 산출물', artifacts.length ? `${artifacts.length}개` : '');
    if (artifactState.error) append(b, 'p', 'progress-error-inline', '산출물 자료(artifacts.json)를 불러오지 못해 표시할 수 없습니다: ' + artifactState.error);
    else if (!artifacts.length) {
      const empty = append(b, 'div', 'progress-no-artifact');
      append(empty, 'strong', '', '실제 산출물 없음');
      append(empty, 'p', '', '아직 확인할 수 있는 결과(데이터·실행 기록)가 없습니다. 예시나 계획을 결과처럼 표시하지 않습니다. 필요한 작업은 아래 \'남은 일\'에 있습니다.');
    } else {
      const distinct = sources(artifacts);
      if (distinct.length > 1) append(b, 'p', 'progress-warning', '이 요구사항의 산출물은 서로 다른 자료에서 나왔습니다 — 같은 조건(실행·모델·기준 버전)으로 합치거나 비교하지 마세요: ' + distinct.join(' / '));
      const files = [];
      const highlighted = highlightedArtifacts(item, artifacts);
      for (const artifact of artifacts) {
        const card = artifactCard(b, artifact, files);
        if (highlighted.includes(artifact)) {
          card.querySelector('.progress-chart')?.remove();
        }
      }
      checkFiles(files);
    }
  }
  function buildBody(item, body, artifacts, artifactState) {
    executiveSummary(body, item, artifacts);
    const story = item.id === 'W01' && w01Story(body, artifacts);   // W01 은 ①~⑤ 설명 화면이 주요 결과·대표 사례를 대신한다
    if (!story) {
      keyResults(body, item, artifacts);
      representativeCases(body, item, artifacts);
    }
    const detailed = append(body, 'details', 'progress-disclosure progress-detailed');
    append(detailed, 'summary', '', story ? `기술 자료 및 원본 보기 · 프롬프트 원문 · 질문 ${fmt(statOf(artifactOf(artifacts, SET_ARTIFACT), '질문(대화)'))}개 전체 · JSON 형식 · 버전 기록 · 테스트 (산출물 ${artifacts.length}개)`
      : `상세 산출물 전체 보기 · ${artifacts.length}개`);
    let built = false;
    detailed.addEventListener('toggle', () => {
      if (!detailed.open || built) return;
      built = true;
      detailedArtifacts(detailed, item, artifacts, artifactState);
    });
    const evidence = append(body, 'details', 'progress-disclosure progress-management');
    append(evidence, 'summary', '', '검증 근거·멘토 요청·남은 일 보기');
    management(evidence, item, artifacts);
  }
  function itemCard(item, artifacts, artifactState) {
    const details = element('details', 'progress-item');
    details.id = 'requirement-' + item.id;
    const summary = append(details, 'summary', 'progress-item-summary');
    append(summary, 'span', 'progress-id', item.id);
    const heading = append(summary, 'span', 'progress-item-heading');
    append(heading, 'strong', '', item.title);
    append(heading, 'small', '', item.requirement);
    append(summary, 'span', 'progress-badge ' + groupOf(item), item.status);
    append(summary, 'span', 'progress-priority', artifacts.length ? `산출물 ${artifacts.length}` : '산출물 없음');
    const body = append(details, 'div', 'progress-item-body');
    let built = false;
    details.addEventListener('toggle', () => {
      if (!details.open || built) return;
      built = true;
      buildBody(item, body, artifacts, artifactState);
    });
    return details;
  }

  // --- 우영 담당 통합 요약 A·B·C — 수치·상태는 artifacts.json 의 overview(tools/progress_artifacts.py 가 계산) 그대로 ------------
  const STEP_STATES = {real: '실제 실행 완료', tested: '코드 구현·테스트 완료', partial: '부분 완료', pending: '미실행·검증 대기'};
  function scrollToId(id) {
    document.getElementById(id)?.scrollIntoView({block: 'start'});
  }
  function groupCharts(parent, group, byId) {
    const charts = group.charts || (group.chart ? [{kind: 'inline', chart: group.chart}] : []);
    if (!charts.length) return;
    const box = append(parent, 'div', 'progress-group-chart');
    const buttons = [];
    if (charts.length > 1) {
      const tabs = append(box, 'div', 'progress-chart-switch');
      tabs.setAttribute('role', 'group');
      tabs.setAttribute('aria-label', `${group.id} 그래프 선택`);
      charts.forEach((spec, index) => {
        const button = append(tabs, 'button', 'progress-mini-button', spec.label);
        button.type = 'button';
        button.addEventListener('click', () => show(index));
        buttons.push(button);
      });
    }
    const stage = append(box, 'div');
    function show(index) {
      const spec = charts[index];
      stage.replaceChildren();
      buttons.forEach((button, i) => button.setAttribute('aria-pressed', String(i === index)));
      if (spec.kind === 'inline') { chartBlock(stage, spec.chart); return; }
      const artifact = byId[spec.artifact_id];
      if (!artifact) { append(stage, 'p', 'progress-no-artifact', `그래프 자료(${spec.artifact_id})가 없습니다.`); return; }
      if (spec.kind === 'intent') { intentBar(stage, artifact, spec.version); return; }
      chartBlock(stage, artifact.content.charts[spec.chart || 0]);
      append(stage, 'p', 'progress-chart-interpretation', artifact.result_summary);
    }
    show(0);
  }
  function pipeline(parent, group) {
    const flow = append(parent, 'ol', 'progress-pipeline');
    flow.setAttribute('aria-label', '질문 생성부터 결과 비교까지 단계별 실제 상태');
    for (const step of group.steps || []) {
      const li = append(flow, 'li', 'progress-pipeline-step ' + step.state);
      append(li, 'span', 'progress-pipeline-state', STEP_STATES[step.state] || step.state);
      append(li, 'strong', '', step.name);
      append(li, 'small', '', step.detail);
    }
    if (group.saved_run) {
      const refs = append(parent, 'ul', 'progress-references');
      const files = [];
      referenceRow(refs, group.saved_run, files);
      checkFiles(files);
    }
  }
  function groupRequirements(parent, group, ctx) {
    const box = append(parent, 'div', 'progress-group-reqs');
    for (const id of group.requirements || []) {
      const item = ctx.items.find(entry => entry.id === id);
      if (!item) continue;
      const row = append(box, 'div', 'progress-group-req');
      append(row, 'span', 'progress-id', id);
      const copy = append(row, 'div');
      const top = append(copy, 'div', 'progress-group-req-title');
      append(top, 'strong', '', item.title);
      append(top, 'span', 'progress-badge ' + groupOf(item), item.status);
      verificationLine(copy, item);
      const button = append(row, 'button', 'progress-mini-button', `${id} 상세 보기`);
      button.type = 'button';
      button.addEventListener('click', () => ctx.openRequirement(id));
    }
  }
  function groupCard(parent, group, ctx) {
    const card = append(parent, 'article', 'progress-group-card');
    card.id = 'progress-group-' + group.id;
    const head = append(card, 'div', 'progress-group-head');
    append(head, 'span', 'progress-group-letter', group.id);
    const title = append(head, 'div');
    append(title, 'h3', '', group.title);
    append(title, 'small', '', (group.requirements || []).join(' · '));
    append(card, 'p', 'progress-group-finding', group.finding);
    if (group.scope) append(card, 'p', 'progress-source-line', group.scope);
    const metrics = append(card, 'div', 'progress-focus-metrics');
    for (const metric of group.metrics || []) summaryMetric(metrics, metric.label, metric.value, metric.note);
    if (group.steps) pipeline(card, group);
    else groupCharts(card, group, ctx.byId);
    if (group.missing?.length) {
      const missing = append(card, 'div', 'progress-missing');
      append(missing, 'strong', '', '실측 없음 · 미완료');
      for (const name of group.missing) append(missing, 'span', '', name);
    }
    if (group.table) {
      const details = append(card, 'details', 'progress-chart-data');
      append(details, 'summary', '', `서비스별 수집 기록 표 · ${group.table.rows.length}행 (실행 ID·시점·실패 원인·구현 상태)`);
      dataTable(details, group.table, group.title + ' 서비스별 기록');
    }
    const issue = append(card, 'p', 'progress-group-issue');
    append(issue, 'strong', '', '가장 중요한 미완료');
    issue.append(' ' + group.open_issue);
    groupRequirements(card, group, ctx);
    return card;
  }
  function ownerOverview(parent, overview, ctx) {
    const head = append(parent, 'div', 'progress-overview-head');
    append(head, 'span', 'progress-kicker', 'OWNER SUMMARY · ' + overview.owner);
    append(head, 'h2', '', overview.title);
    const reviewed = Number(overview.human_reviewed) || 0;
    append(head, 'p', '', `요구사항 ${overview.requirements.length}개를 질문 생성 → 답변 수집 → 평가 연동 세 묶음으로 모았습니다. 수치는 저장소의 실제 결과 파일에서 계산했습니다(데이터 기준 ${overview.data_as_of || '기록 없음'} · 커밋 ${overview.data_revision}). `
      + (reviewed ? `사람 검수 ${fmt(reviewed)}건.` : '사람 검수 0건이라 판정 정확도는 미측정입니다.'));
    const nav = append(parent, 'nav', 'progress-demo-nav');
    nav.setAttribute('aria-label', '3분 시연 순서');
    append(nav, 'strong', '', '3분 시연 순서');
    for (const [id, label] of [['progress-overview-strip', '① 전체 성과'], ['progress-group-A', '② A 질문 생성'], ['progress-group-B', '③ B 답변 수집'],
      ['progress-group-C', '④ C 평가 연동'], ['progress-group-plan', `⑤ ${ctx.due}까지 남은 일`]]) {
      const button = append(nav, 'button', 'progress-mini-button', label);
      button.type = 'button';
      button.addEventListener('click', () => scrollToId(id));
    }
    const strip = append(parent, 'div', 'progress-group-strip');
    strip.id = 'progress-overview-strip';
    for (const group of overview.groups || []) {
      const tile = append(strip, 'button', 'progress-group-tile');
      tile.type = 'button';
      append(tile, 'span', 'progress-group-letter', group.id);
      const copy = append(tile, 'span', 'progress-group-tile-copy');
      append(copy, 'strong', '', group.title);
      append(copy, 'small', '', group.headline);
      tile.addEventListener('click', () => scrollToId('progress-group-' + group.id));
    }
    for (const group of overview.groups || []) groupCard(parent, group, ctx);
    const plan = append(parent, 'section', 'progress-group-card progress-plan');
    plan.id = 'progress-group-plan';
    const planHead = append(plan, 'div', 'progress-group-head');
    append(planHead, 'span', 'progress-group-letter', '→');
    append(planHead, 'h3', '', `${ctx.due}까지 남은 작업과 필요한 승인`);
    const columns = append(plan, 'div', 'progress-fields');
    const left = append(columns, 'div', 'progress-field');
    append(left, 'h4', '', '남은 작업');
    const todo = append(left, 'ol', 'progress-list-plain');
    for (const entry of overview.plan?.remaining || []) { const li = append(todo, 'li'); append(li, 'code', '', entry.requirement); li.append(' ' + entry.text); }
    const right = append(columns, 'div', 'progress-field');
    append(right, 'h4', '', '승인이 필요한 일 (승인 전에는 실행하지 않음)');
    const asks = append(right, 'ul', 'progress-list-plain');
    for (const entry of overview.plan?.approvals || []) { const li = append(asks, 'li'); append(li, 'strong', '', entry.what); li.append(' — ' + entry.why); }
  }

  function render(data, artifactData) {
    const items = data.items;
    const artifactState = {error: artifactData.error || null, list: artifactData.list || [], skipped: artifactData.skipped || 0,
      overview: artifactData.overview || null};
    const byRequirement = {};
    for (const artifact of artifactState.list) (byRequirement[artifact.requirement_id] ||= []).push(artifact);
    host.replaceChildren();
    const intro = append(host, 'div', 'progress-intro');
    const title = append(intro, 'div');
    append(title, 'span', 'progress-kicker', 'MEETING REQUIREMENTS · EVIDENCE');
    append(title, 'h2', '', '핵심 결과부터 확인하고, 필요한 근거를 펼쳐 보세요.');
    append(title, 'p', '', '10월 12·13일 멘토 미팅의 요청마다 실제 산출물(표·그래프·프롬프트·실행 기록)을 붙였습니다. 수치는 저장소의 실제 결과 파일에서 계산했고, 구현·자동 테스트·실제 모델 실행·판정 정확도를 따로 표시합니다.');
    const date = append(intro, 'div', 'progress-date');
    append(date, 'small', '', '마지막 업데이트');
    append(date, 'strong', '', data.as_of || '확인 필요');
    append(date, 'small', '', '최종 목표일 · ' + (data.due || '확인 필요'));
    const refresh = append(date, 'button', 'progress-mini-button', '새로고침');
    refresh.type = 'button';
    refresh.addEventListener('click', load);

    const metrics = append(host, 'div', 'progress-metrics');
    const counts = [
      ['전체 요구사항', items.length],
      ['완료', items.filter(item => groupOf(item) === 'done').length],
      ['진행 중', items.filter(item => groupOf(item) === 'partial').length],
      ['미완료', items.filter(item => groupOf(item) === 'todo').length],
      ['확인·승인 대기', items.filter(item => groupOf(item) === 'pending').length],
      ['판정 정확도', Number(data.human_review_count) > 0 ? '검수 결과 확인' : '미측정']
    ];
    for (const [label, value] of counts) {
      const card = append(metrics, 'div', 'progress-metric');
      append(card, 'span', '', label);
      append(card, 'strong', '', value);
    }
    const stageMetrics = append(host, 'div', 'progress-stage-metrics');
    for (const [key, label] of STAGES) {
      const tally = state => items.filter(item => item.stages?.[key]?.state === state).length;
      const card = append(stageMetrics, 'div', 'progress-stage-metric');
      append(card, 'span', '', label);
      append(card, 'strong', '', `확인 ${tally('done')}`);
      append(card, 'small', '', `일부 ${tally('partial')} · 없음 ${tally('none')} · 해당 없음 ${tally('na')}`);
    }
    const artifactTile = append(stageMetrics, 'div', 'progress-stage-metric');
    append(artifactTile, 'span', '', '실제 산출물');
    append(artifactTile, 'strong', '', artifactState.error ? '불러오지 못함' : `${artifactState.list.length}개`);
    append(artifactTile, 'small', '', artifactState.error ? '' :
      `실측 ${artifactState.list.filter(a => MEASURED.has(a.verification_status?.level)).length} · 산출물 없는 요구사항 ${items.filter(item => !(byRequirement[item.id] || []).length).length}`);

    const note = append(host, 'div', 'progress-note');
    append(note, 'strong', '', '검증 범위');
    append(note, 'p', '', data.disclaimer || '');
    if (data.review_note) append(note, 'p', '', data.review_note);
    if (artifactState.error) append(note, 'p', 'progress-error-inline', '산출물 자료를 불러오지 못했습니다 — 요구사항만 표시합니다. (' + artifactState.error + ')');
    if (artifactState.skipped) append(note, 'p', 'progress-error-inline', `형식이 맞지 않는 산출물 ${artifactState.skipped}개는 표시하지 않았습니다.`);

    const owners = append(host, 'section', 'progress-owner-section');
    append(owners, 'h2', '', '담당별 현황');
    append(owners, 'p', '', '담당자를 선택하면 아래 요구사항 목록이 필터링됩니다.');
    const ownerButtons = append(owners, 'div', 'progress-owners');
    for (const name of ['우영', '윤재', '공동']) {
      const count = items.filter(item => item.owner === name).length;
      const button = append(ownerButtons, 'button', 'progress-owner', name + ' 담당 · ' + count + '개');
      button.type = 'button';
      button.dataset.owner = name;
      button.addEventListener('click', () => {
        owner = owner === name ? 'all' : name;
        update();
        scrollToId(overviewBox.hidden ? 'progress-list-title' : 'progress-owner-overview');
      });
    }
    const overviewBox = append(host, 'section', 'progress-owner-overview');
    overviewBox.id = 'progress-owner-overview';
    overviewBox.hidden = true;
    let overviewBuilt = false;
    function buildOverview() {
      overviewBuilt = true;
      if (!artifactState.overview) {
        append(overviewBox, 'p', 'progress-error-inline', '우영 통합 요약 자료(artifacts.json 의 overview)를 불러오지 못했습니다' + (artifactState.error ? ': ' + artifactState.error : '.'));
        return;
      }
      ownerOverview(overviewBox, artifactState.overview, {items, due: data.due || '최종 목표일',
        byId: Object.fromEntries(artifactState.list.map(artifact => [artifact.artifact_id, artifact])), openRequirement});
    }
    function openRequirement(id) {
      status = 'all'; search = ''; input.value = ''; update();
      const target = document.getElementById('requirement-' + id);
      if (target) { target.open = true; target.scrollIntoView({block: 'start'}); }
    }

    const listSection = append(host, 'section', 'progress-list-section');
    append(listSection, 'h2', '', '요구사항 상세').id = 'progress-list-title';
    append(listSection, 'p', 'progress-muted', '요구사항을 펼치면 핵심 성과, 주요 결과, 대표 사례를 먼저 보여줍니다. 전체 산출물과 검증·관리 정보는 아래에서 펼쳐 볼 수 있습니다.');
    const controls = append(listSection, 'div', 'progress-controls');
    const ownerSelect = append(controls, 'select');
    ownerSelect.setAttribute('aria-label', '담당자 필터');
    for (const [value, label] of [['all', '전체 담당자'], ['우영', '우영'], ['윤재', '윤재'], ['공동', '공동']]) {
      const option = append(ownerSelect, 'option', '', label); option.value = value;
    }
    const statusSelect = append(controls, 'select');
    statusSelect.setAttribute('aria-label', '상태 필터');
    for (const [value, label] of [['all', '전체 상태'], ['done', '완료'], ['partial', '진행 중'], ['todo', '미완료'], ['pending', '확인·승인 대기']]) {
      const option = append(statusSelect, 'option', '', label); option.value = value;
    }
    const input = append(controls, 'input');
    input.type = 'search'; input.placeholder = '제목·요청·산출물·근거 검색'; input.setAttribute('aria-label', '요구사항 검색');
    input.value = search;
    const resultCount = append(listSection, 'p', 'progress-result-count');
    const listBox = append(listSection, 'div', 'progress-list');
    ownerSelect.addEventListener('change', () => { owner = ownerSelect.value; update(); });
    statusSelect.addEventListener('change', () => { status = statusSelect.value; update(); });
    input.addEventListener('input', () => { search = input.value.trim().toLocaleLowerCase('ko'); update(); });

    const priority = append(host, 'section', 'progress-priority-section');
    append(priority, 'h2', '', '우선 작업 순서');
    append(priority, 'p', '', '미완료 P0 요구사항을 담당자별 ID 순으로 보여줍니다. 완료 조건과 남은 근거를 함께 확인하세요.');
    const priorityList = append(priority, 'div', 'progress-priority-list');
    for (const item of items.filter(item => item.priority === 'P0' && !item.status.startsWith('완료'))) {
      const row = append(priorityList, 'article', 'progress-priority-card');
      append(row, 'span', 'progress-id', item.id);
      const copy = append(row, 'div');
      append(copy, 'h3', '', item.title);
      append(copy, 'p', '', '우선 이유 · ' + (item.gap || '확인 필요'));
      append(copy, 'p', '', '완료 조건 · ' + (item.acceptance || '확인 필요'));
      const jump = append(row, 'button', '', '상세 보기');
      jump.type = 'button';
      jump.addEventListener('click', () => {
        owner = 'all'; status = 'all'; search = ''; input.value = ''; update();
        const target = document.getElementById('requirement-' + item.id);
        if (target) { target.open = true; target.scrollIntoView({block: 'start'}); }
      });
    }

    const haystack = item => [item.id, item.title, item.requirement, item.evidence, item.gap, item.output, item.acceptance, item.purpose,
      item.meeting?.request, ...Object.values(item.interpretation || {}), ...(item.next_steps || []),
      ...(byRequirement[item.id] || []).flatMap(a => [a.artifact_id, a.title, a.result_summary])];
    function update() {
      ownerSelect.value = owner;
      statusSelect.value = status;
      for (const button of ownerButtons.querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.owner === owner));
      overviewBox.hidden = owner !== '우영';   // 우영 담당을 고르면 A·B·C 통합 요약을 목록 위에
      if (!overviewBox.hidden && !overviewBuilt) buildOverview();
      const found = items.filter(item =>
        (owner === 'all' || item.owner === owner) &&
        (status === 'all' || groupOf(item) === status) &&
        (!search || haystack(item).some(value => String(value || '').toLocaleLowerCase('ko').includes(search))));
      listBox.replaceChildren(...found.map(item => itemCard(item, byRequirement[item.id] || [], artifactState)));
      if (!found.length) append(listBox, 'p', 'progress-empty', '해당 조건의 요구사항이 없습니다.');
      resultCount.textContent = `전체 ${items.length}개 중 ${found.length}개 표시`;
    }
    update();
  }
  function validArtifact(artifact) {
    return artifact && typeof artifact.artifact_id === 'string' && typeof artifact.requirement_id === 'string' &&
      typeof artifact.title === 'string' && Object.prototype.hasOwnProperty.call(TYPES, artifact.type);
  }
  async function loadArtifacts() {
    try {
      const response = await fetch('/data/artifacts.json', {cache: 'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!Array.isArray(data.artifacts)) throw new Error('artifacts 목록이 없습니다.');
      const list = data.artifacts.filter(validArtifact);
      return {list, skipped: data.artifacts.length - list.length, overview: data.overview && Array.isArray(data.overview.groups) ? data.overview : null};
    } catch (error) {
      return {error: error.message || '알 수 없는 오류'};
    }
  }
  async function load() {
    const id = ++requestId;
    try {
      const [response, artifactData] = await Promise.all([fetch('/data/requirements.json', {cache: 'no-store'}), loadArtifacts()]);
      if (!response.ok) throw new Error('현황 JSON을 가져오지 못했습니다.');
      const data = await response.json();
      if (!Array.isArray(data.items) || data.items.some(item => !item.id || !item.owner || !item.status)) throw new Error('현황 JSON 형식이 올바르지 않습니다.');
      if (id === requestId) render(data, artifactData);
    } catch (error) {
      if (id !== requestId) return;
      host.replaceChildren();
      append(host, 'p', 'progress-error', error.message);
      const retry = append(host, 'button', 'button secondary', '다시 불러오기');
      retry.type = 'button'; retry.addEventListener('click', load);
    }
  }
  window.addEventListener('aiona:progress-open', load);
  if (location.hash === '#progress') load();
})();
