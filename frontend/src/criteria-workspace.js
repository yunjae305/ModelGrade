/* Read-only reference for the criteria used by the evaluation workspace. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const W = window.AionaWorkspace;
  if (!W || !$('criteria-reference-content')) return;
  const {api, node} = W;
  let taxonomy = null;
  let details = null;   // CODE 검사·의미 세부 기준·점검 항목 요약 (info.criteria_details) — 없으면 요약 카드만
  let loading = false;
  const names = {J5: '데이터 신뢰도', J6: '멀티턴 맥락 유지', J7: '롱컨텍스트 활용'};
  const specialized = {
    'AIONA-M': {
      applies: 'AIONA의 실제 표시 답변과 여섯 범주의 내부 마커 목록이 모두 준비된 경우',
      pass: '확인 대상인 내부 마커가 답변에 남아 있지 않음.',
      fail: '내부 자리표시자·태그·인용 잔재가 답변에 노출됨.',
    },
    'AIONA-F': {
      applies: '해당 질문의 금지 표현 목록과 AIONA의 실제 표시 답변이 준비된 경우',
      pass: '미리 정한 금지 표현이 답변에 없음.',
      fail: '미리 정한 금지 표현이 답변에 등장함. 부정문에 포함된 경우도 해당함.',
    },
  };

  function rules(pass, fail) {
    return node('dl', {class: 'criteria-rules'},
      node('div', {class: 'criteria-rule criteria-rule-pass'},
        node('dt', {text: 'PASS'}), node('dd', {text: pass})),
      node('div', {class: 'criteria-rule criteria-rule-fail'},
        node('dt', {text: 'FAIL'}), node('dd', {text: fail})));
  }
  function note(label, text) {
    return text ? node('p', {class: 'criteria-note'}, node('strong', {text: label + ' '}), text) : null;
  }
  function heading(id, name) {
    return node('div', {class: 'criteria-card-heading'},
      node('span', {class: 'criteria-code', text: id}), node('h3', {text: name}));
  }
  function codeCategory(category) {
    return node('section', {class: 'panel criteria-category', 'aria-labelledby': 'criteria-code-' + category.id},
      node('header', {class: 'criteria-category-heading'},
        node('span', {class: 'criteria-code', text: 'CODE ' + category.id}),
        node('div', {}, node('h3', {id: 'criteria-code-' + category.id, text: category.name}),
          node('p', {text: category.question}))),
      node('div', {class: 'criteria-category-items'}, ...(category.subs || []).map((criterion) =>
        node('article', {class: 'criteria-code-item', 'data-criterion': criterion.id},
          node('h4', {}, node('span', {class: 'criteria-subcode', text: criterion.id}), criterion.name),
          note('평가 대상', criterion.target), rules(criterion.pass, criterion.fail), codeChecks(criterion.id)))));
  }
  // 소분류의 실제 검사 목록 — 검사 이름 · 언제 적용되는지 한 줄씩
  function codeChecks(groupId) {
    const checks = (details?.code_checks || []).filter((check) => check.group === groupId);
    if (!checks.length) return null;
    return node('ul', {class: 'criteria-checks', 'aria-label': groupId + ' 검사 ' + checks.length + '개'},
      ...checks.map((check) => node('li', {}, node('span', {text: check.name}), node('small', {text: check.applies}))));
  }
  // 세부 기준 → 점검 항목 표 — 점검 질문 한 줄(경계 메모는 작게), 적용 조건, PASS·FAIL 예시 한 줄씩
  function semanticItems(criterionId) {
    const subs = (details?.semantic?.subs || []).filter((sub) => sub.criterion === criterionId);
    const items = details?.semantic?.items || [];
    if (!subs.length) return null;
    const cell = (cls, ...children) => node('div', {class: cls, role: 'cell'}, ...children);
    return node('div', {class: 'criteria-items', role: 'table', 'aria-label': criterionId + ' 세부 기준과 점검 항목'},
      node('div', {class: 'criteria-items-row criteria-items-head', role: 'row'},
        ...['세부 기준', '점검 항목 (예 = PASS)', '적용', '예시'].map((text) => node('div', {role: 'columnheader', text}))),
      ...subs.flatMap((sub) => items.filter((item) => item.sub === sub.id).map((item, index) =>
        node('div', {class: 'criteria-items-row' + (index === 0 ? ' criteria-items-first' : ''), role: 'row'},
          cell('criteria-items-sub', index === 0 ? node('strong', {text: sub.id}) : null, index === 0 ? ' ' + sub.name : ''),
          cell('criteria-items-question', node('span', {class: 'criteria-subcode', text: item.id}), ' ' + item.question,
            item.note ? node('small', {text: item.note}) : null),
          cell('criteria-items-applies', item.applies),
          cell('criteria-items-examples',
            item.pass ? node('p', {}, node('b', {class: 'criteria-ok', text: 'PASS'}), ' ' + item.pass) : null,
            item.fail ? node('p', {}, node('b', {class: 'criteria-ng', text: 'FAIL'}), ' ' + item.fail) : null)))));
  }
  function semanticCriterion(criterion) {
    const applies = criterion.id === 'J5' ?
      '모든 답변에서 사실 주장을 확인합니다. 사실 주장이 없는 답변은 해당 없음입니다.' : criterion.applies_when;
    // Keep the definition, while using the same plain-language status as the workspace.
    const fail = criterion.fail.replace('미판정(UNVERIFIED)', '판정 미완료');
    return node('article', {class: 'panel criteria-semantic-card', 'data-criterion': criterion.id},
      heading(criterion.id, names[criterion.id] || criterion.name),
      node('p', {class: 'criteria-question', text: criterion.question}),
      note('적용 조건', applies), rules(criterion.pass, fail), note('판정 범위', criterion.boundary), semanticItems(criterion.id));
  }
  function specializedCriterion(criterion) {
    const definition = specialized[criterion.id];
    if (!definition) return null;
    return node('article', {class: 'panel criteria-specialized-card', 'data-criterion': criterion.id, 'data-criterion-state': 'paused'},
      heading(criterion.id, criterion.name),
      node('p', {class: 'criteria-paused-status', text: '평가 대기 · 현재 미실시'}),
      node('p', {class: 'criteria-question', text: criterion.condition.replace(' (환각 카나리아)', '')}),
      note('적용 조건', definition.applies), rules(definition.pass, definition.fail));
  }
  function sectionHeading(id, title, description) {
    return node('header', {class: 'criteria-section-heading'}, node('h2', {id, tabindex: '-1', text: title}),
      node('p', {text: description}));
  }
  function jump(id) {
    const target = $(id);
    if (!target) return;
    target.focus({preventScroll: true});
    target.scrollIntoView({block: 'start', behavior: 'instant'});
  }
  function render() {
    const categories = taxonomy.categories;
    const criteria = taxonomy.jev.criteria;
    const extras = taxonomy.specialized.filter((item) => specialized[item.id]);
    const checks = details?.code_checks?.length, subs = details?.semantic?.subs?.length, items = details?.semantic?.items?.length;
    const index = [
      ['criteria-common-title', '공통 형식·수치 기준', categories.length + '개 분류 · ' + categories.reduce((sum, group) => sum + group.subs.length, 0) + '개 항목' +
        (checks ? ' · 검사 ' + checks + '개' : '')],
      ['criteria-semantic-title', '답변 내용 기준', criteria.length + '개 기준 · J1~J' + criteria.length + (subs ? ' · 세부 ' + subs + ' · 점검 ' + items : '')],
      ['criteria-specialized-title', 'AIONA 특화 기준', extras.length + '개 항목 · 평가 대기'],
    ];
    $('criteria-reference-content').replaceChildren(
      node('nav', {class: 'criteria-index', 'aria-label': '평가 기준 목차'}, ...index.map(([id, title, description]) =>
        node('button', {type: 'button', class: 'criteria-index-item', onclick: () => jump(id)},
          node('strong', {text: title}), node('span', {text: description}), node('span', {class: 'criteria-index-arrow', 'aria-hidden': 'true', text: '↓'})))),
      node('div', {class: 'criteria-legend'},
        node('p', {}, node('strong', {text: '판정의 의미'}), ' 적용되는 기준을 충족하면 PASS, 위반되거나 최종 검증 근거가 부족하면 FAIL입니다.'),
        node('p', {text: '원문 확인 실패를 포함한 보류 항목도 검증 후 PASS 또는 FAIL로 확정합니다. 적용할 조건이 없으면 해당 없음이며, 평가 호출 오류는 별도로 표시합니다.'})),
      node('section', {class: 'criteria-section', 'aria-labelledby': 'criteria-common-title'},
        sectionHeading('criteria-common-title', '공통 형식·수치 기준', '답변의 출력 상태, 질문에 명시된 조건, 직접 대조할 수 있는 값을 확인합니다.'),
        node('div', {class: 'criteria-code-groups'}, ...categories.map(codeCategory))),
      node('section', {class: 'criteria-section', 'aria-labelledby': 'criteria-semantic-title'},
        sectionHeading('criteria-semantic-title', '답변 내용 기준', '질문의 의도와 맥락에 맞게 답했는지, 논리와 사실에 문제가 없는지 확인합니다.'),
        node('div', {class: 'criteria-semantic-grid' + (details ? ' criteria-semantic-list' : '')}, ...criteria.map(semanticCriterion))),
      node('section', {class: 'criteria-section criteria-paused', 'aria-labelledby': 'criteria-specialized-title'},
        sectionHeading('criteria-specialized-title', 'AIONA 특화 기준', '평가 대기 중입니다. 현재 실행하지 않으며 점수와 평가 완료율에 포함하지 않습니다.'),
        node('details', {class: 'criteria-paused-definitions'},
          node('summary', {text: '대기 중인 ' + extras.length + '개 기준 보기'}),
          node('div', {class: 'criteria-semantic-grid'}, ...extras.map(specializedCriterion)))));
  }
  async function load() {
    if (taxonomy || loading) return;
    loading = true;
    const content = $('criteria-reference-content');
    content.setAttribute('aria-busy', 'true');
    $('criteria-reference-error').hidden = true;
    $('criteria-reference-retry').hidden = true;
    content.replaceChildren(node('p', {class: 'criteria-loading', text: '평가 기준을 불러오고 있습니다.'}));
    try {
      const info = await api('/api/code-eval/info');
      if (!Array.isArray(info.taxonomy?.categories) || !Array.isArray(info.taxonomy?.jev?.criteria) ||
          !Array.isArray(info.taxonomy?.specialized)) throw new Error('평가 기준을 불러오지 못했습니다. 다시 시도해 주세요.');
      taxonomy = info.taxonomy;
      details = info.criteria_details && Array.isArray(info.criteria_details.code_checks) ? info.criteria_details : null;
      render();
    } catch (_) {
      taxonomy = null;
      content.replaceChildren();
      $('criteria-reference-error').textContent = '평가 기준을 불러오지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해 주세요.';
      $('criteria-reference-error').hidden = false;
      $('criteria-reference-retry').hidden = false;
    } finally {
      loading = false;
      content.removeAttribute('aria-busy');
    }
  }
  $('criteria-reference-retry').addEventListener('click', load);
  window.addEventListener('aiona:criteria-open', load);
  if (location.hash === '#criteria' || !$('view-criteria').hidden) load();
})();
