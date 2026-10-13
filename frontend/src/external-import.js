/* 외부 AI 답변 가져오기 — 파일 변환·미리 확인·평가 시작. 원문은 서버로만 보내고 브라우저 저장소에 남기지 않는다. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const host = $('external-import');
  if (!host || !window.AionaWorkspace) return;
  const W = window.AionaWorkspace, {node, pill, number, api} = W;
  const names = {gemini: 'Gemini', claude: 'Claude', gpt: 'GPT'};
  const MAX_BYTES = 60 * 1024 * 1024;
  const state = {files: [], preview: null, busy: false, providers: {}, generation: 0, formatsLoaded: false};

  const envValue = (v) => v === true ? '허용' : v === false ? '없음' : v === null || v === undefined ? '확인 안 됨' :
    Array.isArray(v) ? (v.length ? v.join(', ') : '없음') : String(v);
  const time = (value) => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ko-KR', {dateStyle: 'short', timeStyle: 'short'}) : '';
  function showError(message) { $('external-error').textContent = message || ''; $('external-error').hidden = !message; }
  const selectedApi = () => [...host.querySelectorAll('[data-external-api]:checked')].map((input) => input.value);

  function payload(extra = {}) {
    const system = {};
    for (const [id, key] of [['external-system-key', 'system_key'], ['external-provider', 'provider'], ['external-platform', 'platform'],
      ['external-model', 'model'], ['external-access', 'access_mode']]) {
      const value = $(id).value.trim();
      if (value) system[key] = value;
    }
    const options = Object.keys(system).length ? {system} : {};
    const question = $('external-question').value.trim();
    if (question) options.question = question;
    return {files: state.files, options, api_providers: selectedApi(), ...extra};
  }

  function renderApiOptions() {
    const configured = Object.entries(state.providers).filter(([, value]) => value.configured);
    $('external-api-options').replaceChildren(...(configured.length ? [node('span', {class: 'field-help', text: '같은 질문을 API로도 수집:'}),
      ...configured.map(([key, value]) => node('label', {}, node('input', {type: 'checkbox', value: key, 'data-external-api': '',
        onchange: () => schedulePreview()}), `${names[key] || key} · ${value.model}`))] :
      [node('span', {class: 'field-help', text: 'API로 함께 수집하려면 모델 연결에서 키와 모델 ID를 등록하세요.'})]));
  }

  async function loadProviders() {
    try { state.providers = await api('/api/comparison/providers'); } catch (_) { state.providers = {}; }
    renderApiOptions();
  }

  function systemRow(system) {
    const c = system.collection || {}, env = system.environment || {}, used = c.web_search_used || {};
    const statuses = Object.entries(c.statuses || {}).map(([k, v]) => `${({SUCCESS: '수집', INCOMPLETE: '미완료', ERROR: '실패', NOT_COLLECTED: '없음'})[k] || k} ${number(v)}`).join(' · ');
    const period = time(c.collected_from) ? time(c.collected_from) + (c.collected_to && c.collected_to !== c.collected_from ? ' ~ ' + time(c.collected_to) : '') : '미기록';
    return node('tr', {}, node('th', {scope: 'row'}, node('strong', {text: system.label}), node('small', {text: [system.provider, system.model, system.model_version].filter((v) => v && v !== 'unknown').join(' · ') || '모델 미확인'})),
      node('td', {text: system.access_label}), node('td', {text: (c.method_labels || []).join(', ') || '미기록'}), node('td', {text: period}),
      node('td', {text: `${envValue(env.web_search_available)} / ${used.true ? '사용 ' + number(used.true) : used.false && !used.unknown ? '미사용' : '확인 안 됨'}`}),
      node('td', {text: c.tools_used?.length ? c.tools_used.join(', ') : envValue(env.tools_available)}),
      node('td', {text: statuses || '—'}),
      node('td', {}, c.quality?.error ? pill('오류 ' + number(c.quality.error), 'danger') : null, c.quality?.warning ? pill('주의 ' + number(c.quality.warning), 'warning') : null,
        !c.quality?.error && !c.quality?.warning ? pill('문제 없음', 'success') : null));
  }

  function render() {
    const preview = state.preview, box = $('external-preview');
    box.hidden = !preview;
    $('external-rerun-field').hidden = !preview?.duplicate_of;
    if (!preview) { renderReady(); return; }
    const systems = Object.values(preview.systems || {}), env = preview.environment_comparison || {};
    const answers = systems.reduce((n, s) => n + (s.collection?.statuses?.SUCCESS || 0), 0);
    const notes = [...(preview.import?.notes || []), ...(preview.import?.files || []).flatMap((f) => f.notes || [])];
    if (preview.report?.duplicate_responses_dropped) notes.push(`같은 답변이 두 번 들어온 항목 ${number(preview.report.duplicate_responses_dropped)}개는 하나만 남겼습니다.`);
    if (preview.relays_unresolved) notes.push(`플랫폼 중계 URL ${number(preview.relays_unresolved)}개는 원래 주소를 몰라 J5 원문 대조에서 제외합니다. '원래 주소 확인'을 켜면 평가 시작 때 확인합니다.`);
    box.replaceChildren(
      node('div', {class: 'dataset-stats external-stats'}, ...[['질문', preview.question_count], ['대화', preview.conversation_count], ['시스템', systems.length], ['평가할 답변', answers]]
        .map(([label, value]) => node('div', {}, node('span', {text: label}), node('strong', {text: number(value)})))),
      systems.length ? node('div', {class: 'table-scroll'}, node('table', {class: 'data-table external-systems'},
        node('thead', {}, node('tr', {}, ...['시스템', '실행 방식', '수집 경로', '수집 시각', '웹검색 허용 / 사용', '도구', '답변', '수집 품질'].map((t) => node('th', {scope: 'col', text: t})))),
        node('tbody', {}, ...systems.map(systemRow)))) : node('p', {class: 'field-help', text: '가져온 답변 없이 질문만 있습니다. API 수집 모델을 선택하세요.'}),
      env.systems > 1 ? node('div', {class: 'alert ' + (env.same_conditions === false ? 'warning' : 'info')},
        node('strong', {text: env.same_conditions === false ? '실행 환경이 다릅니다 — 결과는 같은 조건의 순위로 표시하지 않습니다.' : '확인한 실행 환경이 같습니다.'}),
        env.differences?.length ? node('ul', {}, ...env.differences.map((d) => node('li', {text: d.label + ' · ' + d.detail}))) : null) : null,
      preview.duplicate_of ? node('p', {class: 'alert warning', text: `같은 내용으로 평가한 실행(${preview.duplicate_of})이 이미 있습니다. 다시 평가하려면 '같은 내용 재실행'을 선택하세요.`}) : null,
      preview.api_collection ? node('p', {class: 'alert warning', text: `선택한 API 수집: 질문 ${number(preview.question_count)}개 × 모델 ${number(preview.api_collection.providers.length)}개 = 유료 호출 ${number(preview.api_collection.planned_calls)}회. 평가자 호출은 별도입니다.`}) : null,
      preview.api_blocked ? node('p', {class: 'alert error', text: preview.api_blocked}) : null,
      notes.length ? node('ul', {class: 'external-notes'}, ...notes.map((n) => node('li', {text: n}))) : null,
      node('details', {class: 'external-questions'}, node('summary', {text: `질문 미리보기 · 처음 ${number(preview.questions.length)}개`}),
        node('ol', {}, ...preview.questions.map((q) => node('li', {}, node('strong', {text: q.case_id + (q.conversation_id ? ` · ${q.conversation_id} ${q.turn}턴` : '') + ' '}), q.question)))));
    renderReady();
  }

  function renderReady() {
    const preview = state.preview, busy = state.busy || W.isBusy?.();
    const blocked = !preview || preview.api_blocked || (preview.duplicate_of && !$('external-rerun').checked && !selectedApi().length);
    $('external-preview-button').disabled = busy || !state.files.length;
    $('external-start').disabled = Boolean(busy || blocked || W.canEvaluate?.() === false);
    $('external-summary').textContent = !state.files.length ? '파일을 고르면 변환 결과를 먼저 확인합니다' : state.busy ? '확인하고 있습니다…' :
      preview ? `질문 ${number(preview.question_count)}개 · 시스템 ${number(Object.keys(preview.systems || {}).length + (preview.api_collection?.providers.length || 0))}개 준비됨` : '변환하지 못했습니다';
    $('external-detail').textContent = preview?.api_collection ? `평가 시작 시 API 유료 호출 ${number(preview.api_collection.planned_calls)}회와 평가자 호출이 발생합니다.` :
      preview ? '평가 시작 시 평가자(의미 평가) 호출이 발생합니다. 미리 확인은 호출하지 않습니다.' : '미리 확인은 모델·평가자를 호출하지 않습니다.';
  }

  let timer = null;
  function schedulePreview() { clearTimeout(timer); timer = setTimeout(previewNow, 250); renderReady(); }
  async function previewNow() {
    if (!state.files.length) return;
    const generation = ++state.generation;
    state.busy = true; showError(null); renderReady();
    try {
      const result = await api('/api/comparison/external/preview', payload());
      if (generation !== state.generation) return;
      state.preview = result;
    } catch (err) {
      if (generation !== state.generation) return;
      state.preview = null; showError(err.message);
    } finally {
      if (generation === state.generation) { state.busy = false; render(); }
    }
  }

  async function readFiles(list) {
    const files = [...list];
    const total = files.reduce((n, f) => n + f.size, 0);
    if (!files.length) return;
    if (total > MAX_BYTES) { showError('파일 합계가 60MB를 넘습니다. 나눠서 가져와 주세요.'); return; }
    state.files = await Promise.all(files.map(async (file) => ({name: file.name, text: await file.text()})));
    $('external-file-names').textContent = files.map((f) => f.name).join(', ');
    state.preview = null; $('external-rerun').checked = false;
    await previewNow();
  }

  async function start() {
    const preview = state.preview;
    if (!preview) return;
    if (preview.api_collection && !window.confirm(`API 유료 호출 ${number(preview.api_collection.planned_calls)}회가 발생합니다. 계속할까요?`)) return;
    state.busy = true; renderReady(); showError(null);
    try {
      const run = await api('/api/comparison/external/runs', payload({name: ($('run-name')?.value || '').trim() || '외부 수집 답변 평가',
        rerun: $('external-rerun').checked, resolve_relays: $('external-resolve').checked}));
      state.files = []; state.preview = null; $('external-files').value = '';
      $('external-file-names').textContent = '평가를 시작했습니다. 결과 비교에서 진행 상황을 확인하세요.';
      render();
      await W.openRun(run.run_id);
    } catch (err) { showError(err.message); }
    finally { state.busy = false; renderReady(); }
  }

  async function loadFormats() {
    if (state.formatsLoaded) return;
    try {
      const info = await api('/api/comparison/external/formats');
      state.formatsLoaded = true;
      $('external-formats').replaceChildren(
        node('p', {text: '확인할 수 없는 값(웹검색 허용 여부 등)은 추정하지 않고 "확인 안 됨"으로 둡니다. 원본은 함께 보관합니다.'}),
        node('ul', {}, ...info.formats.map((f) => node('li', {}, node('strong', {text: f.label}), f.schema ? node('code', {text: ' ' + f.schema}) : null))),
        node('p', {text: `플랫폼별 지원 현황 (${info.checked_at} 공식 문서 확인 기준)`}),
        node('div', {class: 'table-scroll'}, node('table', {class: 'data-table'}, node('thead', {}, node('tr', {}, ...['플랫폼', '지원 방식', '출처 정보', '참고'].map((t) => node('th', {scope: 'col', text: t})))),
          node('tbody', {}, ...info.platforms.map((p) => node('tr', {}, node('td', {text: p.name}), node('td', {text: p.status}), node('td', {text: p.sources}), node('td', {text: p.note})))))));
    } catch (err) { $('external-formats').replaceChildren(node('p', {text: '형식 목록을 불러오지 못했습니다. ' + err.message})); }
  }

  $('external-choose').addEventListener('click', () => $('external-files').click());
  $('external-files').addEventListener('change', (event) => readFiles(event.target.files));
  $('external-preview-button').addEventListener('click', previewNow);
  $('external-start').addEventListener('click', start);
  $('external-rerun').addEventListener('change', renderReady);
  $('external-format-guide').addEventListener('toggle', (event) => { if (event.currentTarget.open) loadFormats(); });
  for (const id of ['external-system-key', 'external-provider', 'external-platform', 'external-model', 'external-access', 'external-question']) {
    $(id).addEventListener('change', schedulePreview);
  }
  window.addEventListener('aiona:semantic-setup', () => { loadProviders(); renderReady(); });
  window.AionaExternalImport = {preview: previewNow, readFiles, state};
  loadProviders(); renderReady();
})();
