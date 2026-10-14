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
  function stageChips(parent, item, compact) {
    const strip = append(parent, compact ? 'span' : 'div', compact ? 'progress-stages compact' : 'progress-stages');
    if (compact) strip.setAttribute('aria-label', '검증 단계');
    for (const [key, label, short] of STAGES) {
      const stage = (item.stages || {})[key] || {state: 'none', note: '기록 없음'};
      const chip = append(strip, 'span', 'progress-stage ' + (stage.state || 'none'));
      append(chip, 'b', '', compact ? short : label);
      append(chip, 'span', '', STATES[stage.state] || '확인 필요');
      chip.title = (compact ? label + ' · ' : '') + (stage.note || '');
      if (!compact && stage.note) append(chip, 'small', '', stage.note);
    }
    return strip;
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
  function dataTable(parent, spec, caption) {
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
      body.replaceChildren(...shown.map(row => {
        const tr = element('tr');
        for (const column of columns) append(tr, 'td', column.numeric || column.percent ? 'num' : '', cellText(column, row[column.key]));
        return tr;
      }));
      if (!shown.length) append(append(body, 'tr'), 'td', 'progress-empty-cell', '조건에 맞는 행이 없습니다.').colSpan = columns.length || 1;
      count.textContent = `${rows.length}행 중 ${shown.length}행`;
    }
    for (const [, select] of selects) select.addEventListener('change', update);
    if (input) input.addEventListener('input', update);
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
    const width = 640, labelWidth = 104, right = 58, bar = series.length > 1 ? 11 : 14, gap = 2, pad = 10;
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
    categories.forEach((category, row) => {
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
    const rows = (chart.categories || []).map((category, row) => Object.fromEntries([['category', category]].concat(series.map((entry, i) => ['s' + i, (entry.values || [])[row]]))));
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
    if (content.table) dataTable(block(parent, '표'), content.table, artifact.title);
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
  function buildBody(item, body, artifacts, artifactState) {
    const metadata = append(body, 'div', 'progress-meta');
    append(metadata, 'span', '', '담당 · ' + item.owner);
    append(metadata, 'span', '', '우선순위 · ' + item.priority);
    append(metadata, 'span', '', '검증 수준 · ' + item.level);
    stageChips(body, item, false);
    if (item.status_change) {
      const note = append(body, 'div', 'progress-callout');
      append(note, 'strong', '', `상태 정정: ${item.status_change.from} → ${item.status_change.to}`);
      append(note, 'p', '', item.status_change.reason);
    }
    if (item.evidence_correction) {
      const note = append(body, 'div', 'progress-callout');
      append(note, 'strong', '', '이전 문서 근거 정정');
      append(note, 'p', '', item.evidence_correction);
    }

    const a = section(body, 'A', '멘토 요구사항');
    const meeting = item.meeting || {};
    const grid = append(a, 'div', 'progress-fields');
    field(grid, '실제 미팅에서 요청한 내용', meeting.request || item.requirement);
    field(grid, '요청 날짜 · 회의 시각(녹음 기준)', (meeting.sessions || []).map(s => `${s.date} · ${s.times.join(', ')}`).join(' / ') || item.source);
    field(grid, '이 작업의 목적', item.purpose + (meeting.purpose ? ` (멘토가 밝힌 이유: ${meeting.purpose})` : ''));
    field(grid, '완료 인정 조건', item.acceptance);
    if (meeting.notes || meeting.confidence) append(a, 'p', 'progress-muted', `전사 확인 · 신뢰도 ${meeting.confidence || '—'}${meeting.notes ? ' · ' + meeting.notes : ''} · ${meeting.basis || ''}`);

    const b = section(body, 'B', '실제 산출물', artifacts.length ? `${artifacts.length}개` : '');
    if (artifactState.error) append(b, 'p', 'progress-error-inline', '산출물 자료(artifacts.json)를 불러오지 못해 표시할 수 없습니다: ' + artifactState.error);
    else if (!artifacts.length) {
      const empty = append(b, 'div', 'progress-no-artifact');
      append(empty, 'strong', '', '실제 산출물 없음');
      append(empty, 'p', '', '아직 확인할 수 있는 결과(데이터·실행 기록)가 없습니다. 예시나 계획을 결과처럼 표시하지 않습니다. 필요한 작업은 아래 \'남은 일\'에 있습니다.');
    } else {
      const distinct = sources(artifacts);
      if (distinct.length > 1) append(b, 'p', 'progress-warning', '이 요구사항의 산출물은 서로 다른 자료에서 나왔습니다 — 같은 조건(실행·모델·기준 버전)으로 합치거나 비교하지 마세요: ' + distinct.join(' / '));
      const files = [];
      for (const artifact of artifacts) artifactCard(b, artifact, files);
      checkFiles(files);
    }

    const c = section(body, 'C', '결과 해석');
    const answers = item.interpretation || {};
    const qa = append(c, 'dl', 'progress-qa');
    for (const [key, question] of [['goal', '무엇을 확인하려고 했나?'], ['method', '어떤 방법으로 확인했나?'], ['result', '실제로 어떤 결과가 나왔나?'],
      ['meaning', '그 결과는 무엇을 뜻하나?'], ['gaps', '무엇이 아직 부족한가?']]) {
      append(qa, 'dt', '', question);
      append(qa, 'dd', '', answers[key] || '미확인');
    }

    evidenceRows(section(body, 'D', '검증 근거'), item, artifacts);

    const rest = section(body, '→', '남은 일');
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
  function itemCard(item, artifacts, artifactState) {
    const details = element('details', 'progress-item');
    details.id = 'requirement-' + item.id;
    const summary = append(details, 'summary', 'progress-item-summary');
    append(summary, 'span', 'progress-id', item.id);
    const heading = append(summary, 'span', 'progress-item-heading');
    append(heading, 'strong', '', item.title);
    append(heading, 'small', '', item.requirement);
    stageChips(heading, item, true);
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

  function render(data, artifactData) {
    const items = data.items;
    const artifactState = {error: artifactData.error || null, list: artifactData.list || [], skipped: artifactData.skipped || 0};
    const byRequirement = {};
    for (const artifact of artifactState.list) (byRequirement[artifact.requirement_id] ||= []).push(artifact);
    host.replaceChildren();
    const intro = append(host, 'div', 'progress-intro');
    const title = append(intro, 'div');
    append(title, 'span', 'progress-kicker', 'MEETING REQUIREMENTS · EVIDENCE');
    append(title, 'h2', '', '요구사항 → 실제 결과 → 검증 근거 → 남은 일을 순서대로 확인합니다.');
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
        document.getElementById('progress-list-title').scrollIntoView({block: 'start'});
      });
    }

    const listSection = append(host, 'section', 'progress-list-section');
    append(listSection, 'h2', '', '요구사항 상세').id = 'progress-list-title';
    append(listSection, 'p', 'progress-muted', '요구사항을 펼치면 A 멘토 요구사항 → B 실제 산출물 → C 결과 해석 → D 검증 근거 → 남은 일 순서로 보입니다.');
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
      return {list, skipped: data.artifacts.length - list.length};
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
