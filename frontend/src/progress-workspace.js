/* Mentor requirement status. The JSON file is the only source of item data. */
(() => {
  'use strict';
  const host = document.getElementById('progress-content');
  if (!host) return;
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
  function groupOf(item) {
    if (item.status.startsWith('완료')) return 'done';
    if (item.status === '부분완료') return 'partial';
    if (item.status === '승인대기' || item.status === '확인필요') return 'pending';
    return 'todo';
  }
  function field(parent, label, value) {
    const box = append(parent, 'div', 'progress-field');
    append(box, 'h3', '', label);
    append(box, 'p', '', value || '확인 필요');
    return box;
  }
  function itemCard(item) {
    const details = element('details', 'progress-item');
    details.id = 'requirement-' + item.id;
    const summary = append(details, 'summary', 'progress-item-summary');
    append(summary, 'span', 'progress-id', item.id);
    const heading = append(summary, 'span', 'progress-item-heading');
    append(heading, 'strong', '', item.title);
    append(heading, 'small', '', item.requirement);
    append(summary, 'span', 'progress-badge ' + groupOf(item), item.status);
    append(summary, 'span', 'progress-priority', item.priority);
    const body = append(details, 'div', 'progress-item-body');
    const metadata = append(body, 'div', 'progress-meta');
    append(metadata, 'span', '', '담당 · ' + item.owner);
    append(metadata, 'span', '', '검증 수준 · ' + item.level);
    append(metadata, 'span', '', '회의 근거 · ' + item.source);
    const grid = append(body, 'div', 'progress-fields');
    field(grid, '멘토 요구사항 · 작업 목적', item.requirement);
    field(grid, '실제 작업 결과 · 증거', item.evidence);
    field(grid, '남은 문제', item.gap);
    field(grid, '다음 산출물', item.output);
    const next = append(body, 'div', 'progress-next');
    append(next, 'h3', '', '다음 작업');
    const list = append(next, 'ol');
    for (const step of item.next_steps || []) append(list, 'li', '', step);
    field(body, '최종 완료 조건', item.acceptance);
    if (item.caution) field(body, '주의 사항', item.caution);
    if (Array.isArray(item.links) && item.links.length) {
      const links = append(body, 'div', 'progress-links');
      append(links, 'strong', '', '관련 근거');
      for (const link of item.links) {
        if (!/^https:\/\/github\.com\/yunjae305\/ModelGrade\//.test(link.url || '')) continue;
        const anchor = append(links, 'a', '', link.label || 'GitHub 근거');
        anchor.href = link.url;
        anchor.target = '_blank';
        anchor.rel = 'noopener noreferrer';
      }
    }
    return details;
  }
  function render(data) {
    const items = data.items;
    host.replaceChildren();
    const intro = append(host, 'div', 'progress-intro');
    const title = append(intro, 'div');
    append(title, 'span', 'progress-kicker', 'MEETING REQUIREMENTS · TRACKING');
    append(title, 'h2', '', '요구사항부터 검증까지, 담당별로 확인합니다.');
    append(title, 'p', '', '10월 12·13일 멘토 미팅의 요청과 현재 구현 근거를 연결했습니다. 실제 실행과 사람 검수는 따로 표시합니다.');
    const date = append(intro, 'div', 'progress-date');
    append(date, 'small', '', '마지막 업데이트');
    append(date, 'strong', '', data.as_of || '확인 필요');
    append(date, 'small', '', '최종 목표일 · ' + (data.due || '확인 필요'));

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
    const note = append(host, 'div', 'progress-note');
    append(note, 'strong', '', '검증 범위');
    append(note, 'p', '', data.disclaimer || '');
    if (data.review_note) append(note, 'p', '', data.review_note);

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

    const section = append(host, 'section', 'progress-list-section');
    append(section, 'h2', '', '요구사항 상세').id = 'progress-list-title';
    const controls = append(section, 'div', 'progress-controls');
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
    input.type = 'search'; input.placeholder = '제목·내용·근거 검색'; input.setAttribute('aria-label', '요구사항 검색');
    const resultCount = append(section, 'p', 'progress-result-count');
    const list = append(section, 'div', 'progress-list');
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

    function update() {
      ownerSelect.value = owner;
      statusSelect.value = status;
      for (const button of ownerButtons.querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.owner === owner));
      const found = items.filter(item =>
        (owner === 'all' || item.owner === owner) &&
        (status === 'all' || groupOf(item) === status) &&
        (!search || [item.id, item.title, item.requirement, item.evidence, item.gap, item.output, item.acceptance, ...(item.next_steps || [])]
          .some(value => String(value || '').toLocaleLowerCase('ko').includes(search))));
      list.replaceChildren(...found.map(itemCard));
      if (!found.length) append(list, 'p', 'progress-empty', '해당 조건의 요구사항이 없습니다.');
      resultCount.textContent = `전체 ${items.length}개 중 ${found.length}개 표시`;
    }
    update();
  }
  async function load() {
    const id = ++requestId;
    try {
      const response = await fetch('/data/requirements.json', {cache: 'no-store'});
      if (!response.ok) throw new Error('현황 JSON을 가져오지 못했습니다.');
      const data = await response.json();
      if (!Array.isArray(data.items) || data.items.some(item => !item.id || !item.owner || !item.status)) throw new Error('현황 JSON 형식이 올바르지 않습니다.');
      if (id === requestId) render(data);
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
