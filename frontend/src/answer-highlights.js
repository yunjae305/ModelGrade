/* Present stored FAIL locations without changing a single character of the answer. */
(() => {
  'use strict';
  let sequence = 0;
  function create(answer, codeCase, semanticRecord, make) {
    const locations = window.AionaFailureLocations.locateFailures(answer, codeCase, semanticRecord);
    const root = make('div', {class: 'answer-highlight-view', 'data-failure-view': ''});
    const pre = make('pre', {class: 'answer-highlight-text', 'data-failure-answer': '', tabindex: '0', 'aria-label': '답변 원문'});
    if (!locations.findings.length) {
      pre.textContent = answer;
      root.append(pre);
      return {element: root, locations, focus: () => {}};
    }
    const byId = new Map(locations.findings.map((finding) => [finding.id, finding]));
    const marked = locations.segments.filter((segment) => segment.findingIds.length);
    const selectionId = 'answer-failure-selection-' + (++sequence);
    const selection = make('div', {class: 'answer-highlight-selection', id: selectionId, 'data-failure-selection': '', 'aria-live': 'polite'});
    const counter = make('span', {class: 'failure-position', 'data-failure-position': '', 'aria-live': 'polite'});
    const previous = make('button', {type: 'button', class: 'button secondary compact', 'data-failure-prev': '', text: '이전 위치', onclick: () => select(active - 1, true)});
    const next = make('button', {type: 'button', class: 'button secondary compact', 'data-failure-next': '', text: '다음 위치', onclick: () => select(active + 1, true)});
    const toggle = make('button', {type: 'button', class: 'button secondary compact', 'data-failure-toggle': '', 'aria-pressed': 'true', text: '강조 표시', onclick: () => {
      const enabled = toggle.getAttribute('aria-pressed') !== 'true';
      toggle.setAttribute('aria-pressed', String(enabled));
      root.classList.toggle('highlights-hidden', !enabled);
      selection.hidden = !enabled;
    }});
    const hasVerifiedFailure = locations.findings.some((finding) => finding.kind !== 'verification' && finding.spans.length);
    const hasVerificationFailure = locations.findings.some((finding) => finding.kind === 'verification' && finding.spans.length);
    const legend = make('div', {class: 'answer-highlight-legend'},
      hasVerifiedFailure ? make('span', {class: 'failure-legend-error', text: '빨강 · 기준 위반 구간'}) : null,
      hasVerificationFailure ? make('span', {class: 'failure-legend-verification', text: '주황 · 검증 실패 관련 구간'}) : null);
    const toolbar = make('div', {class: 'answer-highlight-toolbar', 'data-failure-toolbar': ''},
      make('div', {class: 'answer-highlight-heading'}, make('strong', {text: marked.length ? `FAIL 위치 ${marked.length}곳` : 'FAIL 판정 범위'}), legend));
    if (marked.length) toolbar.append(make('div', {class: 'answer-highlight-controls'}, previous, counter, next, toggle));
    root.append(toolbar);
    const marks = [];
    for (const segment of locations.segments) {
      const text = answer.slice(segment.start, segment.end);
      if (!segment.findingIds.length) { pre.append(document.createTextNode(text)); continue; }
      const index = marks.length, findings = segment.findingIds.map((id) => byId.get(id)).filter(Boolean);
      const verificationOnly = findings.every((finding) => finding.kind === 'verification');
      const mark = make('mark', {class: 'answer-fail-mark' + (verificationOnly ? ' verification' : ''),
        'data-failure-index': String(index), tabindex: '0', role: 'button', 'aria-details': selectionId,
        'aria-label': `${index + 1}번 FAIL 구간 · ${findings.map((finding) => finding.title).join(' · ')} · ${text}`,
        title: findings.map((finding) => finding.title).join(' · '), text,
        onclick: () => select(index, false), onkeydown: (event) => {
          if (!['Enter', ' '].includes(event.key)) return;
          event.preventDefault(); select(index, false);
        }});
      marks.push(mark); pre.append(mark);
    }
    let active = 0;
    function select(index, moveFocus) {
      if (!marks.length) return;
      active = Math.max(0, Math.min(marks.length - 1, index));
      counter.textContent = `${active + 1} / ${marks.length}곳`;
      previous.disabled = active === 0; next.disabled = active === marks.length - 1;
      marks.forEach((mark, i) => {
        mark.classList.toggle('is-current', i === active);
        if (i === active) mark.setAttribute('aria-current', 'location'); else mark.removeAttribute('aria-current');
      });
      selection.replaceChildren(...marked[active].findingIds.map((id) => {
        const finding = byId.get(id);
        return make('div', {class: finding.kind === 'verification' ? 'verification' : ''},
          make('strong', {text: finding.title}), make('p', {text: finding.reason || '이 구간이 FAIL 판정 근거에 포함됩니다.'}),
          finding.kind === 'verification' ? make('p', {class: 'field-help', text: '검증 기준 미충족으로 FAIL입니다. 사실 오류가 확인된 구간과 구분합니다.'}) : null);
      }));
      if (moveFocus) {
        root.classList.remove('highlights-hidden'); toggle.setAttribute('aria-pressed', 'true'); selection.hidden = false;
        marks[active].scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
        marks[active].focus({preventScroll: true});
      }
    }
    if (marks.length) { root.append(selection); select(0, false); }
    const unlocated = locations.findings.filter((finding) => !finding.spans.length);
    if (unlocated.length) root.append(make('div', {class: 'answer-highlight-unlocated', 'data-failure-unlocated': ''},
      ...unlocated.map((finding) => make('div', {}, make('strong', {text: finding.title}),
        make('p', {text: finding.locationNote || '특정 문장 위치 없음 · 저장된 근거에서 답변 구간을 찾지 못했습니다.'})))));
    root.append(pre);
    if (marks.length) requestAnimationFrame(() => {
      if (!pre.isConnected || !pre.clientHeight) return;
      const target = marks[0].getBoundingClientRect(), bounds = pre.getBoundingClientRect();
      pre.scrollTop += target.top - bounds.top - Math.max(20, (pre.clientHeight - target.height) / 2);
    });
    return {element: root, locations, focus: (index = 0) => select(index, true)};
  }
  window.AionaAnswerHighlights = {create};
})();
