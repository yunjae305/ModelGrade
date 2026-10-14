/* AIONA comparison workspace. All source text is inserted with textContent. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const names = {aiona: 'AIONA', gemini: 'Gemini', claude: 'Claude', gpt: 'GPT'};
  const collectionProviders = ['gemini', 'claude', 'gpt'];
  const marks = {aiona: 'A', gemini: '✦', claude: '✳', gpt: 'GPT'};
  const labels = {setup: '새 비교', connections: '연결된 모델', results: '결과 비교', comparison: '결과 비교', history: '이력 관리', versions: '버전 관리', criteria: '평가 기준', progress: '업무 진행 현황'};
  const pageTitles = {...labels, comparison: '버전별 결과 비교'};
  const statusLabels = {COLLECTING: '응답 수집 중', READY: '평가 대기', EVALUATING: '평가 중', COMPLETED: '평가 작업 종료', CANCELLED: '수집 중단', ERROR: '오류 확인 필요'};
  const results = window.AionaResults;
  const state = {cases: [], fileName: '', importInfo: null, providers: {}, run: null,
    result: null, page: 0, search: '', filter: '', filterModel: '', config: null, busy: false, pollVersion: 0, comparisonReady: false, connectionLost: false, connectionError: null,
    uploadVersion: 0, datasetLoading: false, semanticSetup: null, settingsLoading: false, evaluationPolicyReady: null, versionReady: false, versionMode: false, modalProvider: null, modalCase: null, modalSource: null, modalGroup: '', modalModel: 'aiona', modalEvaluation: null};
  let toastTimer;
  let restoringRun = false;
  Object.assign(state, {inputMode: 'file', fileDraft: null, manualRunName: '', currentRelease: null});
  const expandedReasonGroups = new Set();
  const expandedEvaluatorInfo = new Set();
  const evalProgress = new AionaEvalStream.EvalProgress($('evaluation-ticker'));
  let eventStreaming = false;

  function node(tag, attrs = {}, ...children) {
    const n = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined) continue;
      if (key === 'text') n.textContent = value;
      else if (key === 'class') n.className = value;
      else if (key.startsWith('on')) n.addEventListener(key.slice(2), value);
      else n.setAttribute(key, value);
    }
    for (const child of children.flat()) if (child !== null && child !== undefined) n.append(child instanceof Node ? child : document.createTextNode(String(child)));
    return n;
  }
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon'); svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-' + name); svg.append(use); return svg;
  }
  // 모델 로고 (frontend/assets). 블라인드 중에는 resultAvatar 가 A·B·C·D 표시를 대신 그려 로고로 정체가 드러나지 않는다.
  const logos = {aiona: '/assets/aiona-logo.png', gpt: '/assets/gpt-logo.svg', claude: '/assets/claude-logo.svg', gemini: '/assets/gemini-logo.webp',
    factchat: '/assets/factchat-logo.svg', oosoo: '/assets/oosoo-logo.svg'};
  // 외부 수집 실행은 시스템 키가 자유롭다. 기존 4개 모델 키가 아니면 저장된 시스템 정보로 이름·표시를 만든다.
  const systemOf = (p) => state.run?.systems?.[p];
  const external = () => state.run?.run_kind === 'external_import';
  const autoRun = () => state.run?.run_kind === 'auto_collection';
  const providerName = (p) => names[p] || systemOf(p)?.label || state.run?.models?.[p] || p;
  const baseRows = () => state.result?.datasets?.aiona || state.result?.datasets?.[state.run?.providers?.[0]] || [];
  const avatar = (p) => logos[p] ? node('span', {class: 'model-avatar has-logo ' + p, 'aria-hidden': 'true'}, node('img', {src: logos[p], alt: ''}))
    : marks[p] ? node('span', {class: 'model-avatar ' + p, 'aria-hidden': 'true', text: marks[p]})
    : node('span', {class: 'model-avatar external-system', 'aria-hidden': 'true', text: (systemOf(p)?.platform || providerName(p)).replace(/^unknown$/, '?').slice(0, 1).toUpperCase()});
  const pill = (text, kind = 'neutral') => node('span', {class: 'pill ' + kind, text});
  const number = (n) => Number(n || 0).toLocaleString('ko-KR');
  const percent = (rate) => rate === null || rate === undefined ? '—' : (rate * 100).toFixed(1) + '%';
  const blind = () => Boolean(state.run?.blind?.enabled);
  // 블라인드는 평가 모델에 보내는 내부 처리에만 쓴다 — 화면은 처음부터 실제 모델명·로고·순서로 보여 준다
  const resultProviders = () => state.run.providers;
  const modelName = (p) => providerName(p);
  const known = (value) => value && value !== 'unknown' ? value : null;
  const modelId = (run, p) => p === 'aiona' && run.aiona_release?.version ? 'AIONA ' + run.aiona_release.version :
    run.systems?.[p] ? [run.systems[p].access_label, known(run.systems[p].provider), known(run.systems[p].model) || '모델 미확인', run.systems[p].model_version].filter(Boolean).join(' · ') :
    run.models?.[p] || names[p];
  const statusLabel = (run) => run.status === 'CANCELLED' && run.phase === 'EVALUATE' ? '평가 중단' : statusLabels[run.status] || run.status;
  const resultAvatar = avatar;
  function announce(message) {
    $('toast').querySelector('span').textContent = message; $('toast').hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4000);
  }
  function error(message) { $('global-error').textContent = message || ''; $('global-error').hidden = !message; }
  function serverConnection(connected) {
    state.connectionLost = !connected;
    $('server-status').classList.toggle('offline', !connected); $('server-status').classList.toggle('online', connected);
    $('server-status').replaceChildren(node('span', {class: 'online-dot'}), connected ? '로컬 서버 연결됨' : '서버 연결 확인');
  }
  function connectionRestored() {
    serverConnection(true);
    if (state.connectionError && $('global-error').textContent === state.connectionError) error(null);
    state.connectionError = null;
  }
  const retryableConnection = (err) => err.connection || err.status >= 500;
  function showConnectionError() {
    serverConnection(false);
    state.connectionError = '서버 상태를 확인할 수 없습니다. 자동으로 다시 연결합니다. 서버를 종료했다면 다시 실행해 주세요.';
    error(state.connectionError); renderResult(); renderReady();
  }
  async function api(path, body) {
    let response;
    try { response = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}); }
    catch (_) { const failure = new Error('로컬 서버와 연결할 수 없습니다. 서버 실행 상태를 확인해 주세요.'); failure.connection = true; throw failure; }
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('서버 응답을 읽을 수 없습니다. 서버 연결을 확인해 주세요.'); }
    if (!response.ok) {
      const failure = new Error(data.error || '요청 처리에 실패했습니다.');
      failure.status = response.status; throw failure;
    }
    return data;
  }
  function revealActiveNavigation() {
    const selected = document.querySelector('.sidebar .nav-item.active'), nav = selected?.closest('nav');
    if (!nav || nav.scrollWidth <= nav.clientWidth) return;
    const outer = nav.getBoundingClientRect(), item = selected.getBoundingClientRect();
    if (item.left < outer.left) nav.scrollLeft += item.left - outer.left;
    else if (item.right > outer.right) nav.scrollLeft += item.right - outer.right;
  }
  window.addEventListener('resize', revealActiveNavigation);
  function go(view) {
    if (!labels[view]) view = 'setup';
    document.querySelectorAll('.page-view').forEach((n) => { n.hidden = n.id !== 'view-' + view; });
    document.querySelectorAll('[data-view]').forEach((n) => {
      const active = n.dataset.view === (view === 'comparison' ? 'results' : view); n.classList.toggle('active', active);
      if (active) n.setAttribute('aria-current', 'page'); else n.removeAttribute('aria-current');
    });
    $('breadcrumb-current').textContent = labels[view];
    $('topbar-title').textContent = pageTitles[view];
    $('engine-label').hidden = view === 'criteria' || view === 'progress';
    try { sessionStorage.setItem('aiona-workspace-view', view); } catch (_) { /* session storage can be disabled */ }
    revealActiveNavigation();
    if (location.hash !== '#' + view) history.replaceState(null, '', '#' + view);
    window.scrollTo({top: 0, behavior: 'instant'});
    $('main').focus({preventScroll: true});
    if (view === 'history') window.dispatchEvent(new CustomEvent('aiona:history-open'));
    if (view === 'versions') window.dispatchEvent(new CustomEvent('aiona:versions-open'));
    if (view === 'comparison') window.dispatchEvent(new CustomEvent('aiona:comparison-open'));
    if (view === 'criteria') window.dispatchEvent(new CustomEvent('aiona:criteria-open'));
    if (view === 'progress') window.dispatchEvent(new CustomEvent('aiona:progress-open'));
    if (view === 'results') restoreSavedRun();
    if (view === 'setup') window.dispatchEvent(new CustomEvent('aiona:setup-open'));
    if (view === 'connections') window.dispatchEvent(new CustomEvent('aiona:connections-open'));
  }
  document.querySelectorAll('[data-view]').forEach((n) => n.addEventListener('click', (event) => { event.preventDefault(); if (n.dataset.view === 'setup') setVersionMode(false); go(n.dataset.view); }));
  document.querySelectorAll('[data-go]').forEach((n) => n.addEventListener('click', (event) => { event.preventDefault(); go(n.dataset.go); }));
  window.addEventListener('hashchange', () => go(location.hash.slice(1)));

  function active() { return state.busy || (state.run && ['COLLECTING', 'EVALUATING'].includes(state.run.status)); }
  const canEvaluate = () => !state.connectionLost && !state.settingsLoading && state.evaluationPolicyReady === true && state.semanticSetup?.can_evaluate !== false;
  function renderSemanticSetup() {
    const setup = state.semanticSetup;
    document.querySelectorAll('[data-semantic-setup]').forEach((box) => {
      box.replaceChildren(node('strong', {text: state.settingsLoading ? '평가자 설정 확인 중' : setup ?
        `평가자 · ${setup.engine_label || setup.engine || '의미 평가자'}${setup.model ? ' · ' + setup.model : ''}${setup.reasoning_effort ? ' · 추론 ' + setup.reasoning_effort : ''}` : '평가자 설정 정보 없음'}));
      if (state.settingsLoading) return;
      if (state.evaluationPolicyReady === false) {
        box.append(node('p', {text: '서버 재시작이 필요합니다. 새 평가를 시작하려면 서버를 종료하고 start.bat을 다시 실행해 주세요.'}));
        return;
      }
      if (!setup) {
        box.append(node('p', {text: '이 서버는 평가자 설정 정보를 제공하지 않습니다. 새 평가를 시작하기 전에 서버 설정을 확인하세요. 저장된 결과는 바로 볼 수 있습니다.'}));
        return;
      }
      if (setup.can_evaluate === false) box.append(node('p', {text: (setup.reason || '의미 평가자 설정이 필요합니다.') + ' 새 실행은 설정 후 시작할 수 있습니다. 서버 설정을 수정했다면 서버를 재시작하고 다시 확인하세요.'}));
      else box.append(node('p', {text: '연결은 평가 시작 시 확인합니다.'}));
      if (setup.search_message && setup.search_status !== 'configured') box.append(node('p', {text: setup.search_message}));
    });
    document.querySelectorAll('[data-refresh-setup]').forEach((button) => { button.disabled = state.settingsLoading; });
  }
  // 평가를 시작할 수 없는 이유 (평가자 설정·서버 버전) — 새 비교와 버전 평가가 같은 문구를 쓴다
  const evaluatorNotice = () => state.settingsLoading ? '평가자 설정을 확인하고 있습니다.' : state.evaluationPolicyReady === false ? '서버 재시작 후 새 평가를 시작할 수 있습니다. 저장된 결과는 계속 확인할 수 있습니다.' : '의미 평가자 설정이 필요합니다. 위 안내에서 원인을 확인하고 설정을 다시 확인해 주세요.';
  function renderReady() {
    const count = state.cases.length;
    $('version-evaluate-button').disabled = !state.versionReady || !canEvaluate() || !state.versionMode || !$('aiona-version').value.trim() || !count || active() || state.datasetLoading;
    if (!state.versionMode) {
      // 새 비교: 업로드 직후 자동 수집, 수집이 끝나면 평가 시작 (auto-collection.js 가 그린다)
      window.AionaAutoCollection?.renderAction();
      return;
    }
    $('collect-button').disabled = true;
    $('ready-summary').textContent = count ? `AIONA ${$('aiona-version').value.trim() || '버전 미입력'} · ${number(count)}개 Q/A 평가` : '이 버전에서 생성된 AIONA Q/A 원문을 등록해 주세요';
    $('ready-detail').textContent = state.datasetLoading ? '파일을 확인하고 있습니다.' : active() ? '진행 중인 평가를 먼저 완료해 주세요.' : !state.versionReady ? '버전 평가를 지원하는 서버에 연결해야 합니다.' : 'AIONA 답변을 현재 평가 기준으로 평가합니다. 완료된 실행은 버전 비교에서 선택할 수 있습니다.';
    if (!active() && !state.datasetLoading && !canEvaluate()) $('ready-detail').textContent = evaluatorNotice();
  }
  function setVersionMode(enabled) {
    state.versionMode = enabled;
    document.querySelectorAll('[data-version-field]').forEach((field) => {
      field.hidden = !enabled;
      field.querySelectorAll('input').forEach((input) => { input.disabled = !enabled; });
    });
    $('version-mode-note').hidden = !enabled;
    $('version-evaluate-button').hidden = !enabled;
    $('collect-button').hidden = enabled;
    // 버전 평가는 AIONA 원문만 평가한다 — 다른 AI 답변을 자동 수집하지 않는다
    $('collection-panel').hidden = enabled; $('advanced-import').hidden = enabled;
    // 새 비교는 '질문 생성 후 평가'와 '질문 직접 넣기' 두 가지 (사용자 결정 2026-10-14) — 예전 Q/A 파일 업로드 흐름은 버전 평가에서만 보인다
    $('legacy-flow').hidden = !enabled;
    $('question-set-panel').hidden = enabled; $('question-input-panel').hidden = enabled;
    $('setup-title').textContent = enabled ? 'AIONA 버전 원문 등록' : 'AIONA 모델 평가 워크스페이스';
    $('setup-description').textContent = enabled ? '해당 버전에서 받은 AIONA 원문을 평가하고, 저장된 이전 실행과 비교하세요.' : 'LLM으로 질문을 만들거나 질문을 직접 넣으면 AIONA(웹페이지 자동화)·GPT·Claude·Gemini가 답하고, 같은 기준으로 평가합니다.';
    renderReady();
    window.dispatchEvent(new CustomEvent('aiona:dataset-changed', {detail: {reason: 'mode'}}));
  }
  function releaseMetadata() {
    if (!state.versionMode) return undefined;
    const version = $('aiona-version').value.trim();
    return version ? {version, collected_at: $('aiona-collected-at').value || undefined, notes: $('aiona-release-notes').value.trim() || undefined} : undefined;
  }
  $('aiona-version').addEventListener('input', renderReady);
  $('version-mode-exit').addEventListener('click', () => setVersionMode(false));
  // 모델 연결 카드·키 등록 대화상자·자동 수집 관리 설정은 collection-settings.js 가 그린다
  async function loadProviders() {
    state.providers = await api('/api/comparison/providers');
  }
  document.querySelectorAll('[data-close]').forEach((n) => n.addEventListener('click', () => $(n.dataset.close).close()));

  // conversation_id 가 없거나 null 이면 단일 질문 (업로드 API 도 null 을 단일 질문으로 받는다)
  const inConversation = (c) => c.conversation_id !== undefined && c.conversation_id !== null;
  // 질문 단위 — 단일 질문은 1개, 같은 conversation_id 의 턴들은 묶어서 1개
  function questionUnits(cases) {
    const units = [], byConversation = new Map();
    for (const c of cases) {
      if (!inConversation(c)) { units.push([c]); continue; }
      if (!byConversation.has(c.conversation_id)) { byConversation.set(c.conversation_id, []); units.push(byConversation.get(c.conversation_id)); }
      byConversation.get(c.conversation_id).push(c);
    }
    return units;
  }
  // reason: upload(파일·예제 업로드 → 자동 수집 시작) | restore(저장된 수집 작업 다시 열기 — 새 요청 없음)
  function setDataset(cases, name, importInfo = null, reason = 'upload') {
    if (state.inputMode !== 'file') setInputMode('file');
    state.cases = cases; state.fileName = name; state.importInfo = importInfo;
    error(null);
    $('dropzone').hidden = true; $('dataset-content').hidden = false;
    $('file-name').textContent = name;
    // 독립 질문 = 단일 질문 1개 + 멀티턴 대화(2턴 이상) 1개 — 대화의 턴은 평가 대상 턴으로 따로 센다
    const units = questionUnits(cases), conversations = units.filter((unit) => inConversation(unit[0]));
    const conversationTurns = conversations.reduce((n, unit) => n + unit.length, 0);
    const answered = cases.filter((c) => c.answer.trim()).length;
    $('file-meta').textContent = `AIONA Q/A · ${state.importInfo?.format || (name.match(/\.(\w+)$/)?.[1] || '').toUpperCase() || '파일'} · ${number(cases.length)}턴`;
    $('question-count').replaceChildren(number(units.length), node('small', {text: '개'}));
    $('single-count').replaceChildren(number(units.length - conversations.length), node('small', {text: '개'}));
    $('conversation-count').replaceChildren(number(conversations.length), node('small', {text: conversations.length ? `개 · ${number(conversationTurns)}턴` : '개'}));
    $('turn-count').replaceChildren(number(cases.length), node('small', {text: '턴'}));
    $('answer-count').replaceChildren(number(answered), node('small', {text: ` / ${number(cases.length)}턴`}));
    $('answer-ready').textContent = answered === cases.length ? '모두 준비됨' : `답변 없음 ${number(cases.length - answered)}턴`;
    $('answer-ready').className = answered === cases.length ? 'ready' : 'missing';
    if (!$('run-name').value) $('run-name').value = name.replace(/\.(?:jsonl?|md|markdown|txt)$/i, '') + ' 비교';
    renderReady();
    if (reason === 'upload') announce(number(cases.length) + '개 AIONA Q/A를 가져왔습니다.');
    window.dispatchEvent(new CustomEvent('aiona:dataset-changed', {detail: {reason}}));
  }
  function datasetLoading(loading, version) {
    if (version !== state.uploadVersion) return;
    state.datasetLoading = loading;
    ['choose-file', 'replace-file', 'sample-button'].forEach((id) => { $(id).disabled = loading; });
    $('dataset-panel').setAttribute('aria-busy', String(loading));
    $('dataset-loading').hidden = !loading; renderReady();
  }
  async function upload(file) {
    if (!file) return;
    const version = ++state.uploadVersion;
    error(null); datasetLoading(true, version);
    try {
      if (file.size > 64 * 1024 * 1024) throw new Error('파일은 64MB 이하여야 합니다.');
      const buffer = new Uint8Array(await file.arrayBuffer());
      const encoding = buffer[0] === 255 && buffer[1] === 254 ? 'utf-16le' : buffer[0] === 254 && buffer[1] === 255 ? 'utf-16be' : 'utf-8';
      let text;
      try { text = new TextDecoder(encoding, {fatal: true}).decode(buffer); }
      catch (_) { throw new Error('파일의 문자 인코딩을 읽을 수 없습니다. 원문을 UTF-8로 저장한 뒤 다시 올려 주세요.'); }
      const data = await api('/api/comparison/dataset', {input_text: text, input_name: file.name});
      if (version === state.uploadVersion) setDataset(data.cases, file.name, data.import);
    } catch (err) { if (version === state.uploadVersion) error(err.message); }
    finally { if (version === state.uploadVersion) { datasetLoading(false, version); $('dataset-file').value = ''; } }
  }
  $('choose-file').addEventListener('click', () => $('dataset-file').click());
  $('replace-file').addEventListener('click', () => $('dataset-file').click());
  $('dataset-file').addEventListener('change', (e) => upload(e.target.files[0]));
  ['dragenter', 'dragover'].forEach((event) => $('dropzone').addEventListener(event, (e) => { e.preventDefault(); $('dropzone').classList.add('dragging'); }));
  ['dragleave', 'drop'].forEach((event) => $('dropzone').addEventListener(event, (e) => {
    e.preventDefault(); $('dropzone').classList.remove('dragging'); if (event === 'drop') upload(e.dataTransfer.files[0]);
  }));
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());
  $('sample-button').addEventListener('click', async () => {
    const version = ++state.uploadVersion; error(null); datasetLoading(true, version);
    try { const data = await api('/api/comparison/sample'); if (version === state.uploadVersion) setDataset(data.cases, data.name); }
    catch (err) { if (version === state.uploadVersion) error(err.message); } finally { datasetLoading(false, version); }
  });
  function download(value, name) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: 'application/json;charset=utf-8'}));
    const a = node('a', {href: url, download: name}); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const sourceDocument = () => state.modalSource === 'result' ? state.result?.input_document : state.importInfo;
  // 같은 질문의 시스템별 수집 방식·모델·토큰·시각·상태·평가·판정 (서버 collection-comparison-v1, 10-14 4차). 기록이 없으면 '기록 없음'
  function collectionDetails(question, sample) {
    const outcomeText = {PASS: 'PASS', FAIL: 'FAIL', PENDING: '미완료', EXCLUDED: '평가 제외'};
    const value = (v) => v == null ? '-' : number(v);
    const tokens = (e) => e.input_tokens == null && e.output_tokens == null ? '기록 없음' : `${value(e.input_tokens)} · ${value(e.output_tokens)}`;
    const when = (e) => [e.requested_at, e.collected_at].filter(Boolean).map(shortTime).join(' → ') || '기록 없음';
    const collected = (e) => e.status === 'UPLOADED' ? '업로드' : e.status === 'SUCCESS' ? '수집 완료' : e.status + (e.error ? ' · ' + e.error : '');
    const rows = question.systems.map((e) => node('tr', {'data-system': e.system},
      node('th', {scope: 'row', text: modelName(e.system)}), node('td', {text: e.method}),
      node('td', {text: [e.model, e.response_model && e.response_model !== e.model ? '응답 ' + e.response_model : null].filter(Boolean).join(' · ') || '기록 없음'}),
      node('td', {text: tokens(e)}), node('td', {text: number(e.answer_chars) + '자'}), node('td', {text: when(e)}), node('td', {text: collected(e)}),
      node('td', {text: e.evaluation ? (e.evaluation === 'SUCCESS' ? '평가함' : e.evaluation) + ` · 기준 ${number(e.criteria)}개` : '평가 안 함'}),
      node('td', {}, pill(outcomeText[e.outcome] || e.outcome, e.outcome === 'PASS' ? 'success' : e.outcome === 'FAIL' ? 'danger' : 'warning'))));
    return node('details', {class: 'version-profile-raw', 'data-collection-comparison': ''},
      node('summary', {text: `수집 방식 비교 — 같은 질문, 시스템 ${number(question.systems.length)}개` + (question.outcomes_differ ? ' · 판정이 다름' : '') +
        (sample ? ' · 샘플 데이터' : '')}),
      node('div', {class: 'table-scroll'}, node('table', {class: 'data-table'},
        node('thead', {}, node('tr', {}, ...['시스템', '수집 방식', '모델 (설정 · 응답)', '입력 · 출력 토큰', '답변', '요청 → 수신', '수집 상태', '평가', '판정'].map((text) => node('th', {scope: 'col', text})))),
        node('tbody', {}, ...rows))));
  }
  $('view-source').addEventListener('click', () => {
    $('import-notes').replaceChildren(...(sourceDocument()?.notes || []).map((text) => node('p', {text})));
    $('source-text').textContent = sourceDocument()?.original_text || '';
    $('source-dialog').showModal();
  });
  $('template-download').addEventListener('click', () => download([
    {id: 'Q001', question: '1 + 1은 얼마야?', answer: '1 + 1 = 2입니다.'},
    {id: 'Q002', question: '인사말 한 문장으로 답해줘.', answer: '안녕하세요, 반갑습니다.'}
  ], 'aiona-dataset-example.json'));
  function syncManual() {
    if (state.inputMode !== 'manual') return;
    const question = $('manual-question').value, answer = $('manual-answer').value;
    state.cases = question.trim() && answer.trim() ? [{case_id: 'Q1', question, answer}] : [];
    state.fileName = '직접 입력 비교'; state.importInfo = null;
    renderReady();
    window.dispatchEvent(new CustomEvent('aiona:dataset-changed', {detail: {reason: 'manual'}}));
  }
  function setInputMode(mode) {
    if (mode === state.inputMode) return;
    if (state.inputMode === 'file') state.fileDraft = {cases: state.cases, name: state.fileName, info: state.importInfo, runName: $('run-name').value};
    else state.manualRunName = $('run-name').value;
    ++state.uploadVersion; state.datasetLoading = false;
    $('dataset-loading').hidden = true; $('dataset-panel').setAttribute('aria-busy', 'false');
    ['choose-file', 'replace-file', 'sample-button'].forEach((id) => { $(id).disabled = false; });
    state.inputMode = mode;
    $('file-input-panel').hidden = mode !== 'file'; $('manual-input-panel').hidden = mode !== 'manual';
    $('sample-button').hidden = mode !== 'file';
    ['file', 'manual'].forEach((value) => $('input-mode-' + value).setAttribute('aria-pressed', String(mode === value)));
    if (mode === 'manual') { $('run-name').value = state.manualRunName; syncManual(); }
    else {
      state.cases = state.fileDraft?.cases || []; state.fileName = state.fileDraft?.name || ''; state.importInfo = state.fileDraft?.info || null;
      $('run-name').value = state.fileDraft?.runName || ''; renderReady();
      window.dispatchEvent(new CustomEvent('aiona:dataset-changed', {detail: {reason: 'mode'}}));
    }
    error(null);
  }
  ['file', 'manual'].forEach((mode) => $('input-mode-' + mode).addEventListener('click', () => setInputMode(mode)));
  ['manual-question', 'manual-answer'].forEach((id) => $(id).addEventListener('input', syncManual));
  function updateCurrentRelease(history) {
    state.currentRelease = history.current_release || null;
    $('setup-current-version').textContent = state.currentRelease ? 'AIONA ' + state.currentRelease.version : '모델 버전 등록';
    renderReady();
    window.dispatchEvent(new CustomEvent('aiona:dataset-changed', {detail: {reason: 'release'}}));
  }
  window.addEventListener('aiona:model-version-changed', (event) => updateCurrentRelease(event.detail));

  // 새 비교의 평가 시작 — 자동 수집이 끝난 현재 실행(답변 원문 포함)을 기존 CODE·J1~J8 평가기로 평가한다
  async function startEvaluation() {
    if (!state.run || !canEvaluate() || active() || !['READY', 'CANCELLED', 'ERROR'].includes(state.run.status)) return;
    state.busy = true; renderReady(); error(null);
    try {
      state.run = await api(`/api/comparison/runs/${state.run.run_id}/evaluate`, {aiona_config: state.config});
      rememberRun(); resetFilters(); go('results'); renderResult();
      poll();
    } catch (err) { error(err.message); }
    finally { state.busy = false; renderReady(); }
  }
  function rememberRun() {
    try { sessionStorage.setItem('aiona-comparison-run', state.run.run_id); } catch (_) { /* session storage can be disabled */ }
  }
  $('version-evaluate-button').addEventListener('click', async () => {
    if ($('version-evaluate-button').disabled) return;
    state.busy = true; renderReady(); error(null);
    try {
      const run = await api('/api/comparison/version-runs', {cases: state.cases, name: $('run-name').value || `AIONA ${$('aiona-version').value} 평가`,
        aiona_release: releaseMetadata(), aiona_config: state.config, input_document: state.importInfo?.converted ? state.importInfo : undefined});
      window.dispatchEvent(new CustomEvent('aiona:version-created', {detail: {runId: run.run_id}}));
      state.result = null; state.run = run; resetFilters(); rememberRun(); go('results'); renderResult(); await poll();
    } catch (err) { error(err.message); }
    finally { state.busy = false; renderReady(); }
  });
  async function evaluate(options = {}) {
    if (!state.run || !canEvaluate()) return;
    $('evaluate-button').disabled = true; error(null);
    try { state.run = await api(`/api/comparison/runs/${state.run.run_id}/evaluate`, {aiona_config: state.config, ...options}); renderResult(); $('main').focus({preventScroll: true}); await poll(); }
    catch (err) { error(err.message); }
    finally { $('evaluate-button').disabled = !canEvaluate(); }
  }
  $('evaluate-button').addEventListener('click', () => evaluate());
  $('cancel-button').addEventListener('click', async () => {
    if (!state.run) return; $('cancel-button').disabled = true;
    try { state.run = await api(`/api/comparison/runs/${state.run.run_id}/cancel`, {}); renderResult(); $('main').focus({preventScroll: true}); }
    catch (err) { error(err.message); } finally { $('cancel-button').disabled = false; }
  });
  async function poll(id = state.run?.run_id, openOnRecovery = false) {
    if (!id) return;
    const version = ++state.pollVersion;
    let failures = 0;
    while (version === state.pollVersion) {
      try {
        const run = await api('/api/comparison/runs/' + id);
        if (version !== state.pollVersion) return;
        const changed = failures > 0 || state.connectionLost || !state.result || state.result.run_id !== id || state.result.revision !== run.revision ||
          state.result.done !== run.done || state.result.status !== run.status;
        if (changed || !['COLLECTING', 'EVALUATING'].includes(run.status)) {
          const result = await api(`/api/comparison/runs/${id}/result`);
          if (version !== state.pollVersion) return;
          state.result = result; state.run = result; connectionRestored(); failures = 0;
          if (openOnRecovery) { go('results'); openOnRecovery = false; }
          rememberRun(); renderResult(); renderReady();
        } else { state.run = run; connectionRestored(); failures = 0; updateLiveClock(); }
        if (!['COLLECTING', 'EVALUATING'].includes(state.run.status)) {
          if (state.run.status === 'READY') announce('응답 수집이 끝났습니다. 원문을 확인하고 평가를 시작하세요.');
          if (state.run.status === 'COMPLETED') announce(results.runSummary(state.result).allComplete ? '평가가 완료되었습니다. 결과를 확인하세요.' : '평가 작업이 끝났습니다. 완료하지 못한 항목을 확인해 주세요.');
          return;
        }
      } catch (err) {
        if (version !== state.pollVersion) return;
        if (!retryableConnection(err)) { connectionRestored(); error(err.message); return; }
        failures++; showConnectionError();
      }
      await new Promise((resolve) => setTimeout(resolve, failures ? Math.min(5000, 1000 * 2 ** Math.min(failures - 1, 3)) : 1000));
    }
  }
  async function restoreSavedRun() {
    if (state.run || state.busy || restoringRun) return;
    let id;
    try { id = sessionStorage.getItem('aiona-comparison-run'); } catch (_) { /* private browsing */ }
    if (!id) return;
    restoringRun = true;
    try { await openRun(id, {navigate: false}); }
    finally { restoringRun = false; }
  }
  // 저장 당시 엔진과 현재 엔진의 CODE FAIL 차이(읽기 전용) — 옛 엔진의 오독 FAIL 을 상세에서 알린다. 실패해도 화면은 그대로.
  function loadCodeRecheck(result) {
    state.codeRecheck = null;
    if (result?.status !== 'COMPLETED') return;
    const id = result.run_id;
    api(`/api/comparison/runs/${id}/code-recheck`).then((recheck) => {
      if (state.run?.run_id !== id) return;
      state.codeRecheck = {...recheck, run_id: id};
      renderResult();
      if ($('answer-dialog').open && state.modalSource === 'result') {
        state.modalAnswerRender = null; state.modalEvaluation = null;   // 결과가 늦게 와도 열린 상세를 다시 그린다
        renderAnswer();
      }
    }).catch(() => {});
  }
  async function openRun(id, {navigate = true} = {}) {
    const version = ++state.pollVersion;
    try {
      const result = await api(`/api/comparison/runs/${id}/result`);
      if (version !== state.pollVersion) return;
      state.result = result; state.run = result; connectionRestored(); resetFilters();
      rememberRun(); if (navigate) go('results'); renderResult(); renderReady();
      loadCodeRecheck(result);
      if (['COLLECTING', 'EVALUATING'].includes(result.status)) await poll();
    } catch (err) {
      if (version !== state.pollVersion) return;
      if (err.status === 404) {
        state.run = null; state.result = null; evalProgress.disconnect();
        $('evaluation-ticker').hidden = true;
        if ($('answer-dialog').open && state.modalSource === 'result') $('answer-dialog').close();
        try { sessionStorage.removeItem('aiona-comparison-run'); } catch (_) { /* storage may be unavailable */ }
        connectionRestored(); error(null); resetFilters(); renderResult(); renderReady();
        announce('실행 기록이 없어 선택을 해제했습니다.');
      } else if (retryableConnection(err)) { showConnectionError(); await poll(id, navigate); }
      else error(err.message);
    }
  }
  $('refresh-results').addEventListener('click', () => { if (state.run) openRun(state.run.run_id); });
  $('reuse-dataset').addEventListener('click', () => {
    if (active() || !state.result?.datasets?.aiona?.length) return;
    const document = state.result.input_document;
    setVersionMode(false);
    $('run-name').value = state.run.name + ' · 새 비교';
    setDataset(state.result.datasets.aiona, document?.source_name || state.run.name,
      document ? {...document, converted: true} : null);
    $('legacy-flow').hidden = false;   // 저장된 AIONA 원문으로 다시 비교 — 예전 업로드 흐름을 그대로 보인다
    go('setup');
  });
  function common(batch) { return AionaResults.resolvedBatch(batch)?.summary?.common || {}; }
  function resetFilters() {
    state.page = 0; state.search = ''; state.filter = ''; state.filterModel = '';
    $('matrix-search').value = ''; $('matrix-filter').value = ''; $('matrix-model').value = '';
  }
  function renderResult() {
    const run = state.run;
    const focusKey = document.activeElement?.dataset.focusKey;
    renderRecentResult();
    $('result-empty').hidden = Boolean(run); $('result-content').hidden = !run;
    $('export-all').hidden = !run; $('refresh-results').hidden = !run;
    if (!run) return;
    // 예전 실행의 실시간 기록은 '모델 A~D' 별칭으로 남아 있을 수 있어 실제 이름으로 바꿔 보여 준다
    evalProgress.setModelLabels(blind() ? Object.fromEntries(run.providers.map((p) => [run.blind.labels[p], providerName(p)])) : {});
    if (eventStreaming && (run.phase === 'EVALUATE' || run.evaluation_log)) {
      evalProgress.connect(`/api/comparison/runs/${run.run_id}/events`, `${run.run_id}:${run.eval_started_at || ''}`);
    } else { evalProgress.disconnect(); $('evaluation-ticker').hidden = true; }
    const isVersionRun = run.providers.length === 1 && run.aiona_release?.version;
    // 자동 수집 실행은 연결·허용된 제공자만 포함하므로 4개 미만이어도 정상이다
    const partial = !isVersionRun && !external() && !autoRun() && (run.providers.length !== 4 || collectionProviders.some((p) => !run.providers.includes(p)));
    renderEnvironment();
    // 입력 구성 주의 — 모델마다 자기 답변 이력·첨부 미전달 등으로 입력이 달랐으면 점수 비교 전에 알린다 (서버 input_composition)
    const notes = (state.result || run).input_composition?.notes || [];
    $('input-composition').hidden = !notes.length;
    $('input-composition-notes').replaceChildren(...notes.map((n) => node('li', {'data-note': n.code, text: n.text})));
    $('partial-comparison').hidden = !partial;
    $('partial-comparison-description').textContent = `이 기록은 ${run.providers.length}개 모델만 포함한 실행입니다. Gemini·Claude·GPT 전체 응답을 수집하는 4개 모델 비교는 별도로 시작해야 합니다. 원문은 그대로 다시 사용할 수 있습니다.`;
    $('reuse-dataset').disabled = Boolean(active() || !state.result?.datasets?.aiona?.length);
    const providers = resultProviders();
    // 평가 방식은 저장된 실행 기록만 — 현재 설정으로 덮어 표시하지 않는다 (평가를 시작한 실행에만 붙인다)
    const method = run.evaluation_execution || Object.keys(state.result?.evaluations || {}).length ? ` · ${window.AionaEvaluationMethod.label(state.result || run)}` : '';
    // 질문 수는 질문 단위(단일 질문 1개·대화 1개 = 1), 턴 수는 따로 — 저장된 결과를 받기 전이거나 대화가 없으면 기존 표시
    const units = state.result ? results.questionUnits(state.result).length : null;
    const size = units !== null && units !== run.question_count ? `질문·대화 ${number(units)}개 · 평가 대상 턴 ${number(run.question_count)}개` : `${number(run.question_count)}개 질문`;
    $('result-subtitle').textContent = `${run.name} · ${size} · ${run.providers.length}개 모델${method}`;
    $('export-all').href = `/api/comparison/runs/${run.run_id}/export`;
    const collecting = run.status === 'COLLECTING', evaluating = run.status === 'EVALUATING';
    $('results-title').textContent = collecting ? (external() ? 'API 답변을 수집하고 있어요.' : autoRun() ? '연결된 AI의 답변을 수집하고 있어요.' : '세 모델의 답변을 수집하고 있어요.') : '평가 결과, 한눈에.';
    const finished = run.status === 'COMPLETED', summary = results.runSummary(state.result);
    renderFinalSummary(); renderOverview(providers);
    const disconnected = state.connectionLost && (collecting || evaluating);
    const statusText = disconnected ? '서버 연결 확인 중' : finished && state.result ? summary.allComplete ? '평가 완료' : '평가 미완료' : statusLabel(run);
    $('progress-eyebrow').textContent = run.phase === 'EVALUATE' || evaluating || run.status === 'COMPLETED' ? '품질 평가' : '응답 수집';
    $('progress-title').textContent = statusText;
    $('progress-description').textContent = disconnected ? '서버에 다시 연결하고 있습니다. 마지막 확인 상태를 표시합니다.' : collecting ? (run.cancel_requested ? '현재 요청의 응답을 보존한 후 수집을 중단합니다.' : external() ? '답변 수집을 마치면 가져온 답변과 함께 자동으로 평가합니다.' : autoRun() ? '수집을 마치면 새 비교 화면에서 질문별 답변을 확인하고 평가를 시작하세요.' : '답변 수집을 마치면 자동으로 블라인드 평가합니다.') :
      evaluating ? (isVersionRun ? '등록한 AIONA 원문의 ' : '') + '코드 검사와 의미 평가를 진행합니다.' : finished && state.result ?
        `답변 ${number(summary.total)}개 중 ${number(summary.complete)}개 평가 완료 · 문제 발견 ${number(summary.counts.issue)}개 · 미완료 ${number(summary.counts.pending)}개` : run.phase === 'EVALUATE' && ['ERROR', 'CANCELLED'].includes(run.status) ?
          '평가가 중단되었습니다. 저장된 원문과 완료된 결과를 확인하세요. 전체 평가 시작을 누르면 저장된 답변 전체를 다시 평가하며 평가자 호출이 발생합니다.' : '수집한 답변을 확인한 후 동일한 평가기로 평가하세요.';
    $('cancel-button').hidden = !collecting; $('cancel-button').disabled = Boolean(run.cancel_requested);
    $('cancel-button').textContent = run.cancel_requested ? '중단 대기 중' : '수집 중단';
    $('evaluate-button').hidden = !['READY', 'CANCELLED', 'ERROR'].includes(run.status);
    $('evaluate-button').disabled = !canEvaluate();
    $('run-status').className = 'pill ' + (disconnected ? 'warning' : finished ? summary.allComplete ? 'neutral' : 'warning' : run.status === 'ERROR' || run.status === 'CANCELLED' ? 'warning' : 'purple');
    $('run-status').textContent = statusText;
    const evaluationPhase = run.phase === 'EVALUATE' || evaluating || finished;
    const processed = results.processingProgress({...state.result, ...run});
    const done = evaluationPhase ? processed.code_done + processed.semantic_done : run.done;
    const total = evaluationPhase ? processed.code_total + processed.semantic_total : run.total;
    $('run-progress').max = Math.max(1, total || 1);
    $('run-progress').value = done || (!evaluationPhase && !run.total ? 1 : 0);
    $('run-progress').setAttribute('aria-label', evaluationPhase ? '코드 및 의미 검사 단계 처리 수' : '현재 작업 처리 수');
    $('progress-detail').replaceChildren(node('span', {text: evaluationPhase ? `${number(done)} / ${number(total)}개 검사 단계 처리 · 보류·오류·제외 포함` : `${number(run.done)} / ${number(run.total)} API 응답 처리`}),
      node('span', {text: (external() ? '질문 ' : 'AIONA 원문 ') + number(run.question_count) + '개 포함'}));
    renderLiveProgress();
    const warnings = [run.error, run.save_error, ...Object.values(state.result?.evaluations || {}).map((b) => b.run?.save_error)].filter(Boolean);
    $('result-warning').hidden = !warnings.length; $('result-warning').textContent = warnings.join(' ');
    renderBlindStatus();
    $('result-models').dataset.count = providers.length;
    $('result-models').replaceChildren(...providers.map((p) => modelResultCard(p)));
    updateLiveClock();
    renderCriteriaComparison(providers);
    renderCriteriaVerification(state.result?.criteria_verification);
    if ($('matrix-model').options.length !== providers.length + 1 ||
        [...$('matrix-model').options].slice(1).some((option, i) => option.value !== providers[i] || option.text !== modelName(providers[i]))) {
      $('matrix-model').replaceChildren(node('option', {value: '', text: '전체 모델'}), ...providers.map((p) => node('option', {value: p, text: modelName(p)})));
    }
    $('matrix-model').value = state.filterModel;
    renderMatrix(); renderReady();
    if (focusKey) document.querySelectorAll('[data-focus-key]').forEach((element) => {
      if (element.dataset.focusKey === focusKey) element.focus({preventScroll: true});
    });
    if ($('answer-dialog').open && state.modalSource === 'result') renderAnswer();
    window.dispatchEvent(new CustomEvent('aiona:run-rendered'));
  }
  function renderFinalSummary() {
    const run = state.result, visible = Boolean(run && ['COMPLETED', 'ERROR', 'CANCELLED'].includes(run.status));
    $('final-summary').hidden = !visible; $('result-details-heading').hidden = !visible;
    if (!visible) return;
    const summary = results.finalSummary(run), {rows, leaders, total, ready} = summary;
    const score = (value) => value.toFixed(1);
    $('final-summary').dataset.ready = String(ready);
    $('final-summary-status').className = 'pill ' + (ready ? 'success' : 'warning');
    $('final-summary-status').textContent = ready ? `${rows.length}개 모델 · ${number(total)}문항 평가 완료` : '최종 점수 미확정';
    if (ready && !summary.rankable) {
      const labels = (run.environment_comparison?.differences || []).filter((d) => d.code !== 'conversation_gaps').map((d) => d.label);
      $('final-summary-title').textContent = '실행 환경이 달라 순위를 매기지 않았습니다.';
      $('final-summary-description').textContent = `같은 ${number(total)}문항의 시스템별 질문 통과점수입니다. 다른 조건: ${labels.join(', ')}. 같은 조건의 성능 순위로 보지 마세요.`;
    } else if (ready) {
      const top = leaders[0], leaderNames = leaders.map((m) => modelName(m.provider)).join(' · ');
      $('final-summary-title').textContent = rows.length === 1 ? `${leaderNames}의 질문 통과점수는 ${score(top.score)}점입니다.` :
        leaders.length === rows.length ? `모든 모델이 ${score(top.score)}점으로 동률입니다.` :
        leaders.length > 1 ? `${leaderNames}, ${score(top.score)}점으로 공동 1위입니다.` :
        `이번 평가의 최고점은 ${leaderNames}, ${score(top.score)}점입니다.`;
      const baseline = rows.find((m) => m.provider === 'aiona');
      let description = `동일한 ${number(total)}문항에서 ${leaderNames}${leaders.length > 1 ? ' 모두' : '는'} ${number(top.counts.clear)}문항을 통과했습니다.`;
      if (rows.length === 1) description = `${number(total)}문항 중 PASS ${number(top.counts.clear)}개 · FAIL ${number(top.counts.issue)}개입니다.`;
      else if (baseline && baseline.rank !== 1) description += ` AIONA는 ${score(baseline.score)}점으로 ${baseline.tied ? '공동 ' : ''}${baseline.rank}위이며, 최고점과 ${number(top.counts.clear - baseline.counts.clear)}문항(${score(top.score - baseline.score)}점) 차이입니다.`;
      else if (leaders.length === 1 && rows[1]) description += ` 다음 모델과 ${number(top.counts.clear - rows[1].counts.clear)}문항(${score(top.score - rows[1].score)}점) 차이입니다.`;
      $('final-summary-description').textContent = description;
    } else {
      $('final-summary-title').textContent = summary.incomplete || summary.comparable ? '아직 최종 비교 결과를 확정할 수 없습니다.' : '같은 문항으로 비교할 수 있는지 확인이 필요합니다.';
      $('final-summary-description').textContent = summary.incomplete ? `${number(summary.incomplete)}개 답변이 미완료입니다. 아래에서 근거 부족·오류 원인을 확인하세요.` : summary.comparable ? '실행이 중단되었습니다. 아래에서 상태를 확인하세요.' :
        '모델별 질문·대화 구성이 다르거나 입력이 누락되어 점수와 순위를 확정할 수 없습니다.';
    }
    const notices = [...new Set(rows.map((model) => results.evidenceNotice(run, model.provider)).filter(Boolean))];
    $('final-summary-evidence').hidden = !notices.length;
    $('final-summary-evidence').textContent = notices.join(' ');
    $('final-summary-followup').hidden = !run.baseline_run_id && (!summary.incomplete || !run.aiona_release?.version);
    $('final-summary-followup').textContent = run.baseline_run_id ? '기준 버전과 비교' : '버전 비교에서 AIONA 재평가 준비';
    $('summary-ranking').style.setProperty('--summary-columns', String(Math.max(1, rows.length)));
    $('summary-ranking').replaceChildren(...rows.map((model) => {
      const p = model.provider;
      const rank = !ready ? '순위 미확정' : rows.length === 1 ? '단독 결과' : !summary.rankable ? '환경 상이 · 순위 없음' : `${model.tied ? '공동 ' : ''}${model.rank}위`;
      const scoreFill = node('span');
      scoreFill.style.width = `${ready ? model.score : 0}%`;
      const item = node('li', {class: 'summary-score-card' + (model.rank === 1 ? ' is-leading' : ''), 'data-summary-provider': p},
        node('div', {class: 'summary-score-heading'}, node('span', {class: 'summary-rank', text: rank}), resultAvatar(p), node('h3', {text: modelName(p)})),
        node('p', {class: 'summary-model-id', text: modelId(run, p)}),
        node('div', {class: 'summary-score'}, node('strong', {text: ready ? score(model.score) : '—'}), node('span', {text: ready ? '/ 100점' : '점수 미확정'})),
        node('div', {class: 'summary-score-track', 'aria-hidden': 'true'}, scoreFill),
        node('p', {class: 'summary-score-counts', text: `PASS ${number(model.counts.clear)} · FAIL ${number(model.counts.issue)}${model.incomplete ? ' · 미완료 ' + number(model.incomplete) : ''}`}),
        node('p', {class: 'summary-score-denominator', text: ready ? `통과 ${number(model.counts.clear)} / 전체 ${number(total)}문항` : `평가 완료 ${number(model.complete)} / ${number(total)}문항`}));
      item.append(node('button', {type: 'button', class: 'text-button summary-score-link',
        onclick: () => ready ? filterQuestions(p, model.counts.issue ? 'issue' : 'clear') : $('result-models').scrollIntoView({behavior: 'instant', block: 'start'})},
        ready ? model.counts.issue ? `FAIL ${number(model.counts.issue)}문항 확인` : '통과한 문항 확인' : '평가 상태 확인', icon('arrow')));
      return item;
    }));
  }
  // Presentation only: every figure is read from the shared result summaries above.
  function renderOverview(providers) {
    const result = state.result;
    $('result-kpis').hidden = !result;
    $('model-benchmark').hidden = !result || !Object.keys(result.evaluations || {}).length;
    if (!result) return;
    const totals = results.runSummary(result), final = results.finalSummary(result);
    const waiting = totals.counts.waiting + totals.counts.running, single = final.rows.length === 1, top = final.leaders[0];
    const kpi = (kind, iconName, label, value, help) => node('article', {class: 'kpi-card ' + kind},
      node('span', {class: 'kpi-icon', 'aria-hidden': 'true'}, icon(iconName)), node('span', {class: 'kpi-label', text: label}),
      node('strong', {class: 'kpi-value', text: value}), node('span', {class: 'kpi-help', text: help}));
    $('result-kpis').replaceChildren(
      kpi('progress', 'check', '판정 완료 답변', `${number(totals.complete)} / ${number(totals.total)}`,
        totals.counts.pending || waiting ? `미완료 ${number(totals.counts.pending)} · 평가 전·진행 중 ${number(waiting)}` : '모든 답변 판정 완료'),
      kpi('pass', 'shield', 'PASS 답변', number(totals.counts.clear), '기준 충족 · 확인된 문제 없음'),
      kpi('fail', 'alert', 'FAIL 답변', number(totals.counts.issue), '기준 미충족 · 문제 발견'),
      kpi('leader', 'trophy', single ? '질문 통과점수' : '최고 질문 통과점수', final.ready && !final.rankable ? '환경 상이' : final.ready ? top.score.toFixed(1) + '점' : '확정 전',
        final.ready && !final.rankable ? '실행 환경이 달라 시스템별 점수만 표시' : final.ready ? !single && final.leaders.length === final.rows.length ? '모든 모델 동률' :
          final.leaders.map((m) => modelName(m.provider)).join(' · ') + (single ? '' : final.leaders.length > 1 ? ' 공동 1위' : ' 1위') :
          final.incomplete ? `미완료 ${number(final.incomplete)}개 답변 남음` : final.comparable ? '실행 완료 후 확정' : '문항 구성 확인 필요'));
    const byProvider = Object.fromEntries(final.rows.map((m) => [m.provider, m]));
    $('benchmark-list').replaceChildren(...(final.ready ? final.rows.map((m) => m.provider) : providers).map((p) => {
      const m = byProvider[p], c = m.counts;
      const parts = [['pass', 'PASS', c.clear], ['fail', 'FAIL', c.issue], ['pending', '미완료', c.pending], ['waiting', '평가 전·진행 중', c.waiting + c.running]];
      const counts = parts.map(([, label, n]) => `${label} ${number(n)}`).join(' · ');
      const bar = node('div', {class: 'benchmark-bar', role: 'img', 'aria-label': `${modelName(p)} · 전체 ${number(m.total)}문항 중 ${counts}`});
      parts.filter(([, , n]) => n > 0).forEach(([kind, label, n]) => {
        const segment = node('span', {class: 'benchmark-segment ' + kind, title: `${label} ${number(n)}문항`});
        segment.style.flexGrow = String(n); bar.append(segment);
      });
      const rank = !final.ready ? '점수 미확정' : single ? '질문 통과점수' : !final.rankable ? '순위 없음 · 환경 상이' : `${m.tied ? '공동 ' : ''}${m.rank}위`;
      return node('li', {class: 'benchmark-row' + (final.ready && !single && m.rank === 1 ? ' is-leading' : '')},
        node('div', {class: 'benchmark-model'}, resultAvatar(p), node('div', {}, node('strong', {text: modelName(p)}),
          node('span', {text: modelId(state.run, p)}))),
        node('div', {class: 'benchmark-track'}, bar, node('p', {class: 'benchmark-counts', text: counts})),
        node('div', {class: 'benchmark-score'}, node('strong', {text: final.ready ? m.score.toFixed(1) : '—'}), node('span', {text: rank})));
    }));
  }
  function renderRecentResult() {
    const run = state.run, body = $('recent-result-body');
    if (!run) {
      let saved = null;
      try { saved = sessionStorage.getItem('aiona-comparison-run'); } catch (_) { /* storage may be unavailable */ }
      body.replaceChildren(node('div', {class: 'recent-empty'}, node('span', {class: 'empty-icon', 'aria-hidden': 'true'}, icon('chart')),
        node('div', {}, node('strong', {text: saved ? '최근 실행 결과를 불러올 수 있습니다' : '이 세션에서 연 평가 결과가 없습니다'}),
          node('p', {text: saved ? '결과 비교를 열면 저장된 최근 실행을 불러옵니다.' : '평가를 시작하거나 이력 관리에서 저장된 결과를 열어 보세요.'}))));
      return;
    }
    const final = state.result ? results.finalSummary(state.result) : null, ready = Boolean(final?.ready);
    const running = ['COLLECTING', 'EVALUATING'].includes(run.status);
    const preview = node('button', {type: 'button', class: 'recent-preview', onclick: () => go('results')},
      node('span', {class: 'recent-head'}, node('span', {}, node('strong', {text: run.name}), node('span', {class: 'recent-meta', text: `${number(run.question_count)}개 질문 · ${run.providers.length}개 모델`})),
        pill(ready ? '평가 완료' : statusLabel(run), ready ? 'success' : running ? 'purple' : 'warning')));
    if (ready) {
      preview.append(node('span', {class: 'recent-scores'}, ...final.rows.map((m) => {
        const fill = node('span'); fill.style.width = `${m.score}%`;
        return node('span', {class: 'recent-score' + (final.rows.length > 1 && m.rank === 1 ? ' is-leading' : '')}, resultAvatar(m.provider),
          node('span', {class: 'recent-score-name', text: modelName(m.provider)}), node('span', {class: 'summary-score-track', 'aria-hidden': 'true'}, fill),
          node('strong', {text: m.score.toFixed(1) + '점'}));
      })));
    } else {
      preview.append(node('span', {class: 'recent-note', text: running ? '진행 중입니다. 결과 비교 화면에서 실시간 진행을 확인하세요.' :
        final?.incomplete ? `미완료 ${number(final.incomplete)}개 답변이 있어 점수를 확정하지 않았습니다.` : '최종 점수가 확정되지 않았습니다. 결과 비교에서 상태를 확인하세요.'}));
    }
    preview.append(node('span', {class: 'recent-link'}, '결과 비교에서 자세히 보기', icon('arrow')));
    body.replaceChildren(preview);
  }
  $('final-summary-followup').addEventListener('click', () => {
    const runId = state.run?.run_id;
    go('comparison');
    window.dispatchEvent(new CustomEvent('aiona:version-select', {detail: {runId, baselineId: state.run?.baseline_run_id}}));
  });
  function renderBlindStatus() {
    $('blind-banner').hidden = !blind();
    if (!blind()) return;
    $('blind-status').textContent = '블라인드 평가 · 평가 모델에는 모델 이름 없이 보냅니다';
  }
  // 기준별 검증 현황 (서버 criteria-verification-v1, 10-14 4차) — 사람 검수가 없으면 정확도는 0% 가 아니라 '미측정'
  function renderCriteriaVerification(report) {
    const panel = $('criteria-verification');
    panel.hidden = !report;
    if (!report) return;
    const c = report.catalog;
    $('verification-catalog').textContent = `기준 ${number(c.criteria)} · 세부 ${number(c.subs)} · 점검 항목 ${number(c.items)} · CODE 검사 ${number(c.code_checks)}`;
    $('verification-head').replaceChildren(node('tr', {}, ...['평가 기준', '적용 답변', '적용 질문', '독립 대화', '실제 판정', 'PASS · FAIL', '검증 미완료', '사람 검수', '검수 정답 일치', '정확도']
      .map((text) => node('th', {scope: 'col', text}))));
    $('verification-body').replaceChildren(...report.criteria.map((r) => node('tr', {'data-verification': r.id},
      node('th', {scope: 'row'}, node('span', {class: 'criterion-code', text: r.id}), node('strong', {text: r.name})),
      node('td', {text: number(r.applied_answers)}), node('td', {text: number(r.applied_units)}), node('td', {text: number(r.conversations)}),
      node('td', {text: number(r.judged)}),
      node('td', {text: `${number(r.pass)} · ${number(r.fail)}` + (r.limited_fail ? ` (검증 한계 ${number(r.limited_fail)})` : '')}),
      node('td', {text: number(r.unresolved)}), node('td', {text: number(r.human_reviewed) + '건'}),
      node('td', {text: r.accuracy == null ? '-' : number(r.human_agree) + '건'}),
      node('td', {}, r.accuracy == null ? pill('미측정', 'neutral') : node('strong', {text: percent(r.accuracy)})))));
    $('verification-footnote').textContent = '정확도는 사람 검수 정답(검수자 합의)과 AI 판정이 같은 비율입니다. 사람 검수가 없는 기준은 0%가 아니라 미측정입니다.' +
      (report.not_applied.length ? ` 이 실행에서 적용되지 않은 기준: ${report.not_applied.join('·')}.` : '') +
      (report.not_human_verified.length ? ` 사람 검수로 확인하지 않은 기준: ${report.not_human_verified.join('·')}.` : '');
  }
  function renderCriteriaComparison(providers) {
    $('criteria-head').replaceChildren(node('tr', {}, node('th', {scope: 'col', text: '평가 기준'}),
      ...providers.map((p) => node('th', {scope: 'col'}, resultAvatar(p), node('strong', {text: modelName(p)})))));
    $('criteria-body').replaceChildren(...results.comparisonCriteria.map((criterion) => {
      const row = node('tr', {'data-criterion': criterion.id, class: criterion.id === 'J1' ? 'semantic-divider' : ''},
        node('th', {scope: 'row'}, node('span', {class: 'criterion-code', text: criterion.group === 'code' ? 'CODE ' + criterion.id : criterion.id}),
          node('strong', {text: criterion.name}), node('small', {text: criterion.group === 'code' ? '코드 검사 · 조건 수' : 'LLM 평가 · 질문 수'})));
      for (const p of providers) {
        const metric = results.criterionSummary(state.result, p, criterion);
        const cell = node('td', {'data-provider': p});
        if (!metric.available || !metric.targets) {
          cell.append(node('span', {class: 'criterion-empty', text: !metric.available ? '평가 대기' : metric.unknown ? '판정 대기' : metric.excluded ? '적용 대상 없음 · 제외 있음' : '해당 없음'}));
        } else {
          cell.append(node('strong', {class: 'criterion-rate ' + (metric.pending || metric.unknown ? 'has-pending' : metric.failed ? 'has-fail' : 'all-pass'), text: metric.pending || metric.unknown ? '미확정' : percent(metric.rate)}),
            node('span', {class: 'criterion-counts', text: `${number(metric.passed)} PASS · ${number(metric.failed)} FAIL` +
              (metric.limitedFails ? ` (검증 한계 ${number(metric.limitedFails)})` : '')}),
            node('span', {class: 'criterion-targets', text: `${criterion.unit} ${number(metric.targets)}개 대상${metric.pending ? ' · 미판정 ' + number(metric.pending) : ''}${metric.unknown ? ' · 집계 중' : ''}${metric.excluded ? ' · 제외 답변 ' + number(metric.excluded) + '개' : ''}`}));
          if (criterion.group === 'semantic' && metric.pending) {
            const reasons = [['unverified', '근거 확인 필요'], ['incomplete', '판정 미완료'], ['errors', '평가 오류'], ['missing', '판정 누락']]
              .filter(([key]) => metric.pendingReasons[key]).map(([key, label]) => `${label} ${number(metric.pendingReasons[key])}`);
            cell.append(node('span', {class: 'criterion-targets', text: reasons.join(' · ')}));
          }
        }
        row.append(cell);
      }
      return row;
    }));
  }
  const evaluationStageLabels = {WAITING: '평가 대기', CODE: '코드 검사 중', JEV: '의미 평가 중', SAVE: '결과 저장 중', COMPLETED: '평가 작업 종료', ERROR: '평가 중단'};
  const shortTime = (value) => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ko-KR', {dateStyle: 'short', timeStyle: 'short'}) : '';
  function collectionTime(p) {
    if (p === 'aiona') return '업로드 원문 · 수집 없음';
    const imported = systemOf(p)?.collection;
    if (imported && !state.run?.progress?.[p]) {
      const from = shortTime(imported.collected_from), to = shortTime(imported.collected_to);
      return from ? (from === to ? from : from + ' ~ ' + to) + (imported.collected_time_unknown ? ` · 미기록 ${number(imported.collected_time_unknown)}개` : '') : '수집 시각 미기록';
    }
    const progress = state.run?.progress?.[p];
    if (!progress || !('started_at' in progress)) return '시간 기록 없음';
    if (!progress.started_at) return '수집 대기';
    let ms = progress.duration_ms;
    if (ms === null || ms === undefined) {
      ms = state.run.status === 'COLLECTING' && progress.stage === 'COLLECTING'
        ? Math.max(progress.elapsed_ms || 0, Date.now() - Date.parse(progress.started_at)) : progress.elapsed_ms;
    }
    if (!Number.isFinite(ms)) return '시간 기록 없음';
    const seconds = Math.max(0, Math.floor(ms / 1000));
    const time = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
    return (progress.timing_interrupted ? '마지막 기록 ' : '') + time;
  }
  function updateLiveClock() {
    if (state.connectionLost) return;
    document.querySelectorAll('[data-collection-time]').forEach((item) => {
      item.textContent = collectionTime(item.dataset.collectionTime);
    });
    if ($('live-evaluation').hidden) return;
    if (!Number.isFinite(Date.parse(state.run?.eval_started_at))) { $('live-elapsed').textContent = '시간 기록 없음'; return; }
    const savedEnds = Object.values(state.run.evaluation_progress || {}).map((d) => Date.parse(d.updated_at)).filter(Number.isFinite);
    const end = state.run.status === 'EVALUATING' ? Date.now() :
      Date.parse(state.run.eval_finished_at) || (savedEnds.length ? Math.max(...savedEnds) : NaN);
    if (!Number.isFinite(end)) { $('live-elapsed').textContent = '시간 기록 없음'; return; }
    const seconds = Math.max(0, Math.floor((end - Date.parse(state.run.eval_started_at)) / 1000));
    $('live-elapsed').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  }
  function renderLiveProgress() {
    const run = state.run, progress = run.evaluation_progress || {};
    const evaluating = run.status === 'EVALUATING';
    $('live-evaluation').hidden = !(run.phase === 'EVALUATE' || evaluating || run.status === 'COMPLETED');
    if ($('live-evaluation').hidden) return;
    const heading = $('live-evaluation').querySelector('.live-heading > span:first-child');
    heading.textContent = evaluating ? '실시간 진행' : '처리 결과';
    heading.className = 'pill ' + (evaluating ? 'purple' : 'neutral');
    const connectionNote = $('live-evaluation').querySelector('.live-heading > span:last-child');
    connectionNote.hidden = !evaluating || !state.connectionLost;
    connectionNote.textContent = connectionNote.hidden ? '' : '서버에 다시 연결하고 있습니다.';
    const processed = results.processingProgress({...state.result, ...run}), sum = (key) => processed[key];
    $('live-code').textContent = `${number(sum('code_done'))} / ${number(sum('code_total'))}개`;
    $('live-semantic').textContent = `${number(sum('semantic_done'))} / ${number(sum('semantic_total'))}개`;
    $('live-semantic-detail').textContent = `기준 판정 완료 ${number(sum('semantic_success'))}` +
      (sum('semantic_unverified') ? ` · 확인 필요 ${number(sum('semantic_unverified'))}` : '') +
      (sum('semantic_errors') ? ` · 오류 ${number(sum('semantic_errors'))}` : '') +
      (sum('skipped') ? ` · 제외 ${number(sum('skipped'))}` : '');
    const active = Object.entries(progress).filter(([, d]) => !['WAITING', 'COMPLETED', 'ERROR'].includes(d.stage));
    $('live-active').textContent = !evaluating ? run.status === 'COMPLETED' ? '평가 요청 처리가 끝났습니다. 보류·오류의 원인은 아래 결과에서 확인하세요.' : '평가가 중단되었습니다. 마지막으로 저장된 처리 수를 표시합니다.' : state.connectionLost ? '서버 연결이 끊겨 마지막으로 받은 진행 상태를 표시합니다.' : active.map(([p, d]) => `${modelName(p)} · ${evaluationStageLabels[d.stage]}${d.active.length ? ' · ' + d.active.map((i) => baseRows()[i]?.case_id || `질문 ${i + 1}`).join(', ') + ' 응답 대기' : ''}`).join(' / ') || '다음 모델의 평가를 준비하고 있습니다.';
    $('live-active').hidden = !evaluating;
    const latest = Object.entries(progress).filter(([, d]) => d.latest).sort((a, b) => Date.parse(b[1].latest.updated_at) - Date.parse(a[1].latest.updated_at))[0];
    const latestRecord = latest && state.result?.evaluations?.[latest[0]]?.jev_eval?.records?.[latest[1].latest.index];
    const message = latest ? `최근 처리: ${modelName(latest[0])} · ${latest[1].latest.case_id} · ${results.semanticState(latestRecord || {status: latest[1].latest.status}).text}` : '첫 결과를 기다리고 있습니다.';
    $('live-latest').hidden = !evaluating;
    if ($('live-latest').textContent !== message) $('live-latest').textContent = message;
    updateLiveClock();
  }
  const envValue = (v) => v === true ? '허용' : v === false ? '없음' : v === null || v === undefined ? '확인 안 됨' :
    Array.isArray(v) ? (v.length ? v.join(', ') : '없음') : String(v);
  function differenceText(d) {
    if (d.code === 'conversation_gaps') return Object.entries(d.values).map(([cid, turns]) => `${cid} ${turns.join('·')}턴 누락`).join(' / ');
    if (d.code === 'collection_time') return Object.entries(d.values).map(([k, [from, to]]) => `${providerName(k)} ${shortTime(from) || '미기록'}${to && to !== from ? ' ~ ' + shortTime(to) : ''}`).join(' / ');
    return Object.entries(d.values).map(([k, v]) => `${providerName(k)} ${envValue(v)}`).join(' / ');
  }
  // 외부 수집 실행의 수집 환경 비교와 평가 엔진·정책 버전. 환경이 다르면 같은 조건의 순위로 보지 않는다.
  function renderEnvironment() {
    const box = $('external-environment'), comparison = state.run?.environment_comparison;
    box.hidden = !(external() || autoRun()) || !comparison;
    if (box.hidden) return;
    const fields = state.result?.evaluation_profile?.fields || {}, policy = fields.decision_policy || {};
    const versions = [fields.engine_versions?.engine && '평가 엔진 ' + fields.engine_versions.engine,
      fields.criteria_version && '의미 기준 ' + fields.criteria_version, fields.judge?.requested_model && '평가 Judge ' + fields.judge.requested_model,
      policy.j5_source_original_failure && 'J5 출처 정책 ' + policy.j5_source_original_failure].filter(Boolean);
    const same = comparison.same_conditions;
    box.className = 'alert external-environment ' + (same === false ? 'warning' : 'info');
    box.replaceChildren(node('strong', {text: same === null ? '외부 수집 답변 · 시스템 1개' : same ? '수집 환경 비교 · 확인한 조건이 같습니다' : '수집 환경 비교 · 실행 환경이 다릅니다'}),
      node('p', {text: comparison.note}),
      comparison.differences.length ? node('ul', {}, ...comparison.differences.map((d) => node('li', {text: `${d.label}: ${differenceText(d)}`}))) : null,
      versions.length ? node('p', {class: 'summary-scope', text: versions.join(' · ')}) : null);
  }
  function systemEnvironment(system) {
    const env = system.environment || {}, c = system.collection || {}, used = c.web_search_used || {};
    const usedText = used.true ? `사용 ${number(used.true)}개` + (used.unknown ? ` · 확인 안 됨 ${number(used.unknown)}개` : '') :
      used.false && !used.unknown ? '사용 안 함' : '확인 안 됨';
    return node('dl', {class: 'system-environment'}, ...[['실행 방식', system.access_label], ['수집 경로', (c.method_labels || []).join(', ') || '미기록'],
      ['웹검색 허용', envValue(env.web_search_available)], ['웹검색 사용', usedText],
      ['도구', c.tools_used?.length ? c.tools_used.join(', ') : env.tools_available === null || env.tools_available === undefined ? '확인 안 됨' : '사용 기록 없음'],
      ['수집 품질', c.quality?.warning || c.quality?.error ? `주의 ${number(c.quality.warning || 0)} · 오류 ${number(c.quality.error || 0)}` : '문제 기록 없음']]
      .flatMap(([k, v]) => [node('dt', {text: k}), node('dd', {text: v})]));
  }
  const urlStatusLabels = {original: '원래 URL', tracking_removed: '플랫폼 유입 표시 제거', relay_unwrapped: '중계 URL에 담긴 원래 URL',
    relay_resolved: '중계 URL 이동 주소 확인', relay_unresolved: '중계 URL · 원래 주소 미확인(원문 대조 제외)', invalid: '올바르지 않은 URL', missing: 'URL 없음'};
  function collectionDetail(c) {
    const env = c.environment || {}, usage = c.usage || {};
    const rows = [['수집 방식', c.collection_method_label || c.collection_method || '미기록'],
      ['수집 시각', Number.isFinite(Date.parse(c.collected_at)) ? new Date(c.collected_at).toLocaleString('ko-KR') : '미기록'],
      ['답변 형식', ({markdown: 'Markdown', text: '텍스트', html_converted: 'HTML → Markdown 변환'})[c.answer_format] || '미기록'],
      ['웹검색 사용', envValue(env.web_search_used)], ['사용 도구', env.tools_used ? env.tools_used.join(', ') || '없음' : '확인 안 됨'],
      ['생성 토큰', usage.input_tokens !== undefined || usage.output_tokens !== undefined ? `입력 ${usage.input_tokens ?? '—'} · 출력 ${usage.output_tokens ?? '—'}` : '제공 안 됨'],
      ...(usage.reported_cost ? [['제공자가 밝힌 비용', `${usage.reported_cost.amount} ${usage.reported_cost.currency}`]] : [])];
    const box = node('details', {class: 'collection-detail'}, node('summary', {text: `수집 정보 · 출처 ${number((c.sources || []).length)}개`}),
      node('dl', {class: 'system-environment'}, ...rows.flatMap(([k, v]) => [node('dt', {text: k}), node('dd', {text: String(v)})])));
    if (c.sources?.length) box.append(node('ol', {class: 'collection-sources'}, ...c.sources.map((source) => {
      const url = results.safeHttpUrl(source.url || source.display_url);
      return node('li', {}, url ? node('a', {href: url, target: '_blank', rel: 'noopener noreferrer', text: source.title || source.url || source.display_url}) :
        node('span', {text: source.title || source.display_url || 'URL 없음'}),
        node('small', {text: ' · ' + (urlStatusLabels[source.url_status] || '')}),
        source.relay_url && source.url_status !== 'relay_unresolved' ? node('small', {text: ' · 중계 URL ' + source.relay_url}) : null,
        source.claims?.length ? node('p', {class: 'field-help', text: '연결된 답변 구절: ' + source.claims.join(' / ')}) : null);
    })));
    if (c.quality?.length) box.append(node('ul', {class: 'collection-quality'}, ...c.quality.map((q) =>
      node('li', {text: `[${({info: '참고', warning: '주의', error: '오류'})[q.severity] || q.severity}] ${q.message}`}))));
    if (c.agent) box.append(node('p', {class: 'field-help', text: `에이전트 실행 ${c.agent.status} · 도구 호출 ${number(c.agent.tool_calls)}회 · 실패 ${number(c.agent.tool_errors)}회. 도구 사용 과정은 평가 기준이 아니라 근거 정보로만 보관합니다.`}));
    if (c.original?.content) box.append(node('details', {}, node('summary', {text: `원본 보기 (${c.original.format})`}), node('pre', {class: 'collection-original', text: c.original.content})));
    return box;
  }
  function modelResultCard(p) {
    const run = state.run, batch = state.result?.evaluations?.[p], c = common(batch);
    const summary = results.modelSummary(state.result, p, run.question_count);
    const live = run.status === 'EVALUATING' && run.evaluation_progress?.[p];
    const hasEvaluation = run.phase === 'EVALUATE' || run.status === 'EVALUATING' || Boolean(batch);
    const processed = results.processingProgress({...state.result, ...run}, p);
    const progress = run.progress[p], imported = Boolean(systemOf(p) && !progress);
    const available = p === 'aiona' ? run.question_count : imported ? systemOf(p).collection.statuses?.SUCCESS || 0 : progress?.success || 0;
    const isCollecting = run.status === 'COLLECTING' && p !== 'aiona' && progress?.stage === 'COLLECTING';
    const collectionLabel = run.status === 'COLLECTING' ? p === 'aiona' || imported ? '원문 준비됨' :
      progress?.done === progress?.total ? '수집 종료' : isCollecting ? '답변 수집 중' : '수집 대기' : null;
    const metric = hasEvaluation ? processed.semantic_done : available;
    const card = node('article', {class: 'panel result-model-card', 'data-provider': p},
      node('div', {class: 'result-card-heading'}, resultAvatar(p), node('strong', {text: modelName(p)}),
        pill(collectionLabel || (live ? evaluationStageLabels[live.stage] : summary.text),
          collectionLabel ? isCollecting ? 'purple' : 'neutral' : live ? 'purple' : summary.kind)),
      node('div', {class: 'result-model-id', text: modelId(run, p)}),
      node('div', {class: 'result-metric'}, node('strong', {text: number(metric)}),
        node('span', {text: '/ ' + number(run.question_count) + '개'})),
      node('div', {class: 'metric-label', text: hasEvaluation ? (summary.complete === run.question_count ? '모든 질문 최종 판정 완료' : '의미 평가 요청 처리') : '수집된 답변 · 평가 전'}));
    const track = node('progress', {max: String(Math.max(1, run.question_count)), value: String(metric), 'aria-label': modelName(p) + (hasEvaluation ? ' 의미 평가 처리 수' : ' 답변 수집 수')});
    card.append(node('div', {class: 'metric-track'}, track));
    if (systemOf(p)) card.append(systemEnvironment(systemOf(p)));
    if (live) card.append(node('div', {class: 'model-live-progress'},
      node('p', {text: `코드 ${number(live.code_done)} / ${number(live.code_total)}개 처리 · 의미 ${number(live.semantic_done)} / ${number(live.semantic_total)}개 처리`}),
      node('p', {text: `의미 판정 완료 ${number(live.semantic_success)}개 · 확인 필요 ${number(live.semantic_unverified)}개 · 오류 ${number(live.semantic_errors)}개 · 제외 ${number(live.skipped)}개`})));
    if (batch) {
      const finalOnly = !batch.live && summary.complete === run.question_count && !summary.counts.pending;
      const outcomes = finalOnly ? {issue: 'FAIL · 기준 미충족', clear: 'PASS · 기준 충족'} :
        {issue: 'FAIL · 기준 미충족', clear: 'PASS · 기준 충족', pending: '평가 미완료'};
      card.append(node('div', {class: 'outcome-counts' + (finalOnly ? ' final-outcomes' : '')}, ...Object.entries(outcomes).map(([outcome, label]) => node('button', {
        class: 'outcome-count ' + outcome, 'data-focus-key': p + '-' + outcome, 'aria-label': modelName(p) + ' ' + label + ' ' + summary.counts[outcome] + '개 질문 보기',
        onclick: () => filterQuestions(p, outcome)}, node('strong', {text: number(summary.counts[outcome])}), node('span', {text: label})))));
      if (live) card.append(node('div', {class: 'live-question-counts'},
        ...['running', 'waiting'].map((outcome) => node('button', {class: 'text-button',
          'data-focus-key': p + '-' + outcome, onclick: () => filterQuestions(p, outcome)},
          (outcome === 'running' ? '평가 중 ' : '평가 대기 ') + number(summary.counts[outcome]) + '개', icon('arrow')))));
      const composition = (state.result || run).input_composition?.systems?.[p];
      if (composition) card.append(node('p', {class: 'hint', 'data-input-composition': '', text: composition.source === 'API 수집' ?
        `수집 입력 ${number(composition.input_tokens)} · 출력 ${number(composition.output_tokens)} 토큰(제공자 집계) · 가장 큰 요청 ${number(composition.max_request_input || 0)} · ` +
        `턴 ${number(composition.turns)} · 판정한 기준 ${number(composition.judged_criteria)}건` + (composition.attachments_not_sent ? ` · 첨부 미전달 ${number(composition.attachments_not_sent)}` : '') :
        `업로드 원문 · 턴 ${number(composition.turns)} · 판정한 기준 ${number(composition.judged_criteria)}건`}));
      card.append(node('div', {class: 'evaluation-metrics'},
        node('div', {}, node('span', {text: '공통 코드 통과율'}), node('strong', {text: c.unjudged || batch.cases?.length !== run.question_count ? '미확정' : percent(c.pass_rate)})),
        node('p', {text: `조건 PASS ${number(c.pass)}개 · FAIL ${number(c.fail)}개 · ${number(c.unjudged)}개 미완료` +
          (summary.codeErrors ? ` · 검사 실행 오류 ${number(summary.codeErrors)}개 질문` : '')}),
        node('div', {}, node('span', {text: '질문 최종 판정'}), node('strong', {text: `${number(summary.complete)} / ${number(run.question_count)}개 질문`}))));
      const semantic = batch.jev_eval;
      const sourcePolicyFailures = (semantic?.records || []).filter((record) => record?.status === 'SUCCESS' && record.criteria?.includes('J5') &&
        record.decisions?.J5?.decision === 'FAIL' && record.decisions.J5.reason_code === 'SOURCE_ORIGINAL_UNAVAILABLE').length;
      if (sourcePolicyFailures) card.append(node('div', {class: 'evaluation-warning evaluation-policy-fail', 'data-source-policy-fail': ''}, icon('shield'),
        node('div', {}, node('strong', {text: `원문 확인 실패 · FAIL ${number(sourcePolicyFailures)}개`}),
          node('p', {text: 'J5 신뢰도 검증 기준 미충족'}),
          node('button', {type: 'button', class: 'text-button', onclick: () => filterQuestions(p, 'issue'), text: 'FAIL 문항 보기'}))));
      const evidencePolicyFailures = (semantic?.records || []).filter((record) => record?.status === 'SUCCESS' &&
        (record.criteria || []).some((id) => record.decisions?.[id]?.decision === 'FAIL' &&
          record.decisions[id].reason_code === 'EVALUATION_EVIDENCE_INSUFFICIENT')).length;
      if (evidencePolicyFailures) card.append(node('div', {class: 'evaluation-warning evaluation-policy-fail', 'data-evidence-policy-fail': ''}, icon('shield'),
        node('div', {}, node('strong', {text: `검증 근거 부족 · FAIL ${number(evidencePolicyFailures)}개`}),
          node('p', {text: '최종 검증에서 평가 기준 미충족'}),
          node('button', {type: 'button', class: 'text-button', onclick: () => filterQuestions(p, 'issue'), text: 'FAIL 문항 보기'}))));
      // J5 FAIL 사유 구성 — 사실 오류 확정과 검증 한계(원문·근거·검색·출처 접근)를 나눠 센다 (저장 판정은 그대로)
      const j5 = results.j5Breakdown(semantic?.records);
      if (j5.fail) card.append(node('div', {class: 'evaluation-warning' + (j5.confirmed ? '' : ' evaluation-policy-fail'), 'data-j5-breakdown': ''}, icon('shield'),
        node('div', {}, node('strong', {text: `J5 FAIL ${number(j5.fail)}개 · 사실 오류 확정 ${number(j5.confirmed)}개`}),
          ...[[true, '사실 오류 확정'], [false, '검증 한계(사실 오류 확정 아님)']].map(([confirmed, title]) => {
            const rows = Object.values(j5.codes).filter((c) => c.confirmed === confirmed).sort((a, b) => b.count - a.count);
            return rows.length ? node('p', {text: `${title}: ` + rows.map((c) => `${c.short} ${number(c.count)}`).join(' · ')}) : null;
          }).filter(Boolean),
          j5.unclassified ? node('p', {text: `사유 기록 없음 ${number(j5.unclassified)}개 (이전 결과)`}) : null)));
      const evaluatorKey = run.run_id + ':' + p;
      if (semantic?.engine_label || ['openai_api', 'anthropic_api'].includes(semantic?.engine)) card.append(
        node('details', {class: 'evaluation-error-detail', open: expandedEvaluatorInfo.has(evaluatorKey) ? '' : null,
          ontoggle: (event) => {
            if (!event.currentTarget.isConnected) return;
            if (event.currentTarget.open) expandedEvaluatorInfo.add(evaluatorKey); else expandedEvaluatorInfo.delete(evaluatorKey);
          }}, node('summary', {'data-focus-key': p + '-evaluator', text: '평가자 정보 보기'}),
          node('p', {class: 'hint', text: '의미 평가자 · ' + results.semanticEngineLabel(semantic)
            + (semantic.client?.requested_model ? ' · ' + semantic.client.requested_model : '')
            + (semantic.client?.reasoning_effort ? ' · 추론 ' + semantic.client.reasoning_effort : '')})));
      const reasons = new Map(), originals = new Map();
      summary.rows.filter((r) => r.pending && r.code.checks.length && (r.semantic.complete ? r.code.pendingReason :
        !batch.live || !r.semantic.waiting && !r.semantic.inProgress)).forEach((r) => {
        const reason = r.semantic.complete ? r.code.pendingReason : r.semantic.reason;
        reasons.set(reason, (reasons.get(reason) || 0) + 1);
        const full = r.semantic.complete ? r.code.pendingDetail : r.semantic.rawReason || r.semantic.reason;
        originals.set(full, (originals.get(full) || 0) + 1);
      });
      if (reasons.size) {
        const entries = [...reasons].sort((a, b) => b[1] - a[1]);
        const reasonNode = ([reason, n]) => node('p', {text: reason + ' (' + number(n) + '개)'});
        const content = node('div', {}, node('strong', {text: '미완료 사유 · ' + number(summary.semanticIncomplete + summary.codeIncomplete) + '개 질문'}),
          node('small', {class: 'hint', text: '답변이 틀렸다는 뜻이 아니라, 평가가 PASS·FAIL을 정할 근거를 확보하지 못한 질문입니다.'}),
          ...entries.slice(0, 3).map(reasonNode));
        if (originals.size) {
          const key = run.run_id + ':' + p;
          content.append(node('details', {class: 'evaluation-error-detail', open: expandedReasonGroups.has(key) ? '' : null,
            ontoggle: (event) => {
              if (!event.currentTarget.isConnected) return;
              if (event.currentTarget.open) expandedReasonGroups.add(key); else expandedReasonGroups.delete(key);
            }}, node('summary', {'data-focus-key': p + '-remaining-reasons', text: '상세 사유 보기' + (entries.length > 3 ? ' · 외 ' + number(entries.length - 3) + '개 원인' : '')}),
            ...[...originals].sort((a, b) => b[1] - a[1]).map(reasonNode)));
        }
        card.append(node('div', {class: 'evaluation-warning'}, icon('clock'), content));
      }
      const recheck = state.codeRecheck?.run_id === run.run_id ? state.codeRecheck.providers?.[p] : null;
      const stale = Object.values(recheck?.cases || {}).filter((entry) => entry.cleared?.length);
      if (stale.length) card.append(node('div', {class: 'evaluation-warning recheck-summary'}, icon('clock'), node('div', {},
        node('strong', {text: `옛 엔진 FAIL · 질문 ${number(stale.length)}개`}),
        node('p', {text: `저장 당시 엔진(${String(recheck.stored_engine || '').replace('aiona-code-eval-', '') || '기록 없음'})의 CODE FAIL `
          + `${number(stale.reduce((n, entry) => n + entry.cleared.length, 0))}개는 현재 엔진(${state.codeRecheck.engine.replace('aiona-code-eval-', '')})에서 나오지 않습니다. `
          + '저장 판정은 그대로이며 원문 재평가로 갱신됩니다.'}))));
      if (c.unjudged) card.append(node('div', {class: 'evaluation-warning'}, icon('clock'), node('div', {},
        node('strong', {text: '미판정 코드 조건 ' + number(c.unjudged) + '개'}),
        node('p', {text: '저장 당시 엔진의 미판정 조건입니다. 질문 상세에서 원인을 확인하세요.'}))));
      if (recheck?.pasted_without_end?.length) card.append(node('div', {class: 'evaluation-warning'}, icon('clock'), node('div', {},
        node('strong', {text: `붙여 넣은 자료 끝 표시 없음 · 질문 ${number(recheck.pasted_without_end.length)}개`}),
        node('p', {text: '생성기 v1 로 만든 질문이라 자료 구간([자료 끝] 표시)을 가리지 못해, 자료 속 문장이 요구 조건·진위 요청으로 읽힐 수 있습니다. '
          + '화면의 원문 재평가는 저장된 질문을 그대로 쓰므로 해결되지 않습니다 — 변환된 질문 세트(끝 표시 추가)로 답변을 다시 수집하는 새 실행이 필요합니다.'}))));
    } else card.append(node('div', {class: 'result-card-stats'}, node('span', {text: `답변 수집 오류·미완료 ${number((progress?.failed || 0) + (progress?.incomplete || 0))}개`})));
    const bottom = node('div', {class: 'result-card-bottom'});
    if (batch && !batch.live) bottom.append(node('a', {href: `/code-eval.html?comparison=${run.run_id}&provider=${p}#results`, class: 'text-button', target: '_blank', rel: 'noopener noreferrer'}, '상세 보고서', icon('arrow')));
    else if (live) bottom.append(pill('결과 도착 시 바로 반영', 'purple'));
    else bottom.append(pill(collectionLabel || (p === 'aiona' || imported ? '원문 준비됨' : progress?.done === progress?.total ? '수집 종료' : '수집 대기'), 'neutral'));
    bottom.append(node('a', {href: `/api/comparison/runs/${run.run_id}/export/${p}`, class: 'text-button'}, '원문 JSON', icon('download')));
    card.append(node('div', {class: 'collection-timing'}, node('span', {}, icon('clock'), imported ? '답변 수집 시각' : '답변 수집 시간'),
      node('strong', {'data-collection-time': p, text: collectionTime(p)})));
    card.append(bottom); return card;
  }
  function renderMatrix() {
    const data = baseRows(), providers = resultProviders();
    const scope = state.filterModel ? [state.filterModel] : providers;
    $('matrix-count').textContent = number(state.run.question_count) + '개';
    $('matrix-head').replaceChildren(node('tr', {}, node('th', {scope: 'col', text: 'ID'}), node('th', {scope: 'col', text: '질문 원문'}),
      ...providers.map((p) => node('th', {scope: 'col', text: modelName(p)}))));
    const query = state.search.toLocaleLowerCase(), filtered = data.map((c, index) => ({c, index})).filter(({c, index}) =>
      (!query || (c.case_id + ' ' + c.question).toLocaleLowerCase().includes(query)) &&
      results.questionMatches(state.result, scope, index, state.filter));
    const pages = Math.max(1, Math.ceil(filtered.length / 10)); state.page = Math.min(state.page, pages - 1);
    const rows = filtered.slice(state.page * 10, state.page * 10 + 10).map(({c, index}) => node('tr', {},
      node('td', {text: c.case_id}), node('td', {}, node('button', {class: 'row-button', 'data-focus-key': 'question-' + index, text: c.question, 'aria-label': c.case_id + ' 응답 비교 열기', onclick: () => openAnswer(index, 'result')})),
      ...providers.map((p) => {
        const status = results.caseState(state.result, p, index);
        // 칸에 마우스를 올리면 판정 이유(미완료면 미완료 사유)를 바로 보인다 — 상세를 열지 않아도 왜 그런지 알 수 있게
        return node('td', {'data-model': modelName(p)}, node('button', {class: 'result-cell', 'data-focus-key': 'result-' + p + '-' + index, 'aria-label': c.case_id + ' ' + modelName(p) + ' ' + status.text + ' 상세 보기', title: status.detail || '', onclick: () => openAnswer(index, 'result', p)},
          pill(status.text, status.kind), node('span', {class: 'check-status', text: '코드: ' + status.code.text + ' · 의미: ' + status.semantic.text})));
      })));
    if (!rows.length) rows.push(node('tr', {}, node('td', {colspan: providers.length + 2, text: state.result ? '선택한 조건에 해당하는 질문이 없습니다.' : '수집을 마치면 질문별 원문을 확인할 수 있습니다.'})));
    $('matrix-body').replaceChildren(...rows);
    $('matrix-caption').textContent = data.length ? `${number(filtered.length)}개 중 ${filtered.length ? state.page * 10 + 1 : 0}–${Math.min((state.page + 1) * 10, filtered.length)} 표시` : '응답 수집 중';
    $('page-number').textContent = `${state.page + 1} / ${pages}`;
    $('previous-page').disabled = state.page === 0; $('next-page').disabled = state.page + 1 >= pages;
  }
  $('matrix-search').addEventListener('input', (e) => { state.search = e.target.value; state.page = 0; renderMatrix(); });
  $('matrix-filter').addEventListener('change', (e) => { state.filter = e.target.value; state.page = 0; renderMatrix(); });
  $('matrix-model').addEventListener('change', (e) => { state.filterModel = e.target.value; state.page = 0; renderMatrix(); });
  $('clear-matrix-filter').addEventListener('click', () => { resetFilters(); renderMatrix(); });
  function filterQuestions(provider, outcome) {
    state.filterModel = provider; state.filter = outcome; state.page = 0;
    $('matrix-model').value = provider; $('matrix-filter').value = outcome; renderMatrix();
    $('question-results').scrollIntoView({block: 'start', behavior: 'instant'}); $('matrix-filter').focus({preventScroll: true});
  }
  $('previous-page').addEventListener('click', () => { state.page--; renderMatrix(); });
  $('next-page').addEventListener('click', () => { state.page++; renderMatrix(); });
  const answerCases = (source) => source === 'dataset' ? state.cases : baseRows();
  function conversationGroups(cases) {
    const groups = new Map();
    cases.forEach((c, i) => {
      if (!inConversation(c)) return;
      const key = 'conversation:' + c.conversation_id;
      if (!groups.has(key)) groups.set(key, {key, label: c.source_group || c.conversation_id, indices: []});
      groups.get(key).indices.push(i);
    });
    for (const group of groups.values()) group.indices.sort((a, b) => cases[a].turn - cases[b].turn);
    return groups;
  }
  function answerIndices() {
    return [...$('answer-select').options].map((option) => Number(option.value));
  }
  function answerSelection(cases) {
    const groups = conversationGroups(cases);
    const singles = cases.map((c, i) => inConversation(c) ? null : i).filter((i) => i !== null);
    $('answer-group').replaceChildren(node('option', {value: '', text: '전체 Q/A · ' + number(cases.length) + '개'}),
      groups.size && singles.length ? node('option', {value: 'single', text: '단건 질문 · ' + number(singles.length) + '개'}) : null,
      ...[...groups.values()].map((group) => node('option', {value: group.key, text: group.label + ' · ' + number(group.indices.length) + '턴'})));
    $('answer-group').value = state.modalGroup;
    const indices = state.modalGroup === 'single' ? singles : groups.get(state.modalGroup)?.indices || cases.map((_, i) => i);
    $('answer-select').replaceChildren(...indices.map((i) => node('option', {value: String(i), text: cases[i].case_id + ' · ' + cases[i].question.slice(0, 90)})));
  }
  $('answer-select').addEventListener('change', (e) => openAnswer(Number(e.target.value), state.modalSource, state.modalModel));
  $('answer-group').addEventListener('change', (e) => {
    state.modalGroup = e.target.value; answerSelection(answerCases(state.modalSource));
    const indices = answerIndices();
    openAnswer(indices.includes(state.modalCase) ? state.modalCase : indices[0], state.modalSource, state.modalModel);
  });
  function adjacentAnswer(direction) {
    const indices = answerIndices(), position = indices.indexOf(state.modalCase);
    openAnswer(indices[position + direction], state.modalSource, state.modalModel);
  }
  $('answer-previous').addEventListener('click', () => adjacentAnswer(-1));
  $('answer-next').addEventListener('click', () => adjacentAnswer(1));
  function openAnswer(index, source, provider = null, group = '') {
    provider = provider || (source === 'result' ? resultProviders()[0] : 'aiona');
    const cases = answerCases(source);
    if (!Number.isInteger(index) || index < 0 || index >= cases.length) return;
    if (state.modalSource !== source || !$('answer-dialog').open) {
      state.modalGroup = group; answerSelection(cases);
      state.modalAnswerRender = null; state.modalEvaluation = null;
    }
    state.modalCase = index; state.modalSource = source; state.modalModel = provider;
    $('view-source').hidden = !sourceDocument()?.original_text;
    const c = cases[index];
    const indices = answerIndices(), position = indices.indexOf(index);
    $('answer-select').value = String(index); $('answer-previous').disabled = position <= 0; $('answer-next').disabled = position + 1 >= indices.length;
    $('answer-position').textContent = `${number(position + 1)} / ${number(indices.length)}개 Q/A`;
    $('answer-id').textContent = c.case_id + (c.conversation_id ? ' · ' + (c.source_group || c.conversation_id) + ' · ' + c.turn + '턴' : '') + ' · 원문 응답';
    $('original-question').textContent = c.question;
    $('conversation-context').hidden = !inConversation(c);
    if (inConversation(c)) {
      const group = conversationGroups(cases).get('conversation:' + c.conversation_id);
      $('conversation-context').replaceChildren(icon('clock'), node('div', {},
        node('strong', {text: (c.source_group || c.conversation_id) + ' · ' + c.turn + ' / ' + group.indices.length + '턴'}),
        node('p', {text: c.turn > 1 ? `이전 ${number(c.turn - 1)}개 턴의 질문·답변 전문을 사용합니다. ` + (source === 'result' && external() ? '각 시스템은 원래 대화에 남은 자기 답변을 이력으로 사용합니다. 빠진 턴이 있으면 멀티턴 맥락 유지(J6)를 판정하지 않습니다.' : '비교 모델은 각자 받은 답변으로 대화를 이어갑니다.') : '대화의 첫 질문입니다. 이후 턴은 이전 질문·답변 전문을 이어서 사용합니다.'})));
    }
    const providers = source === 'dataset' ? ['aiona'] : resultProviders();
    $('answer-tabs').replaceChildren(...providers.map((p) => node('button', {class: 'answer-tab', role: 'tab', id: 'answer-tab-' + p,
      'aria-controls': 'original-answer', 'aria-selected': String(p === provider), tabindex: p === provider ? '0' : '-1',
      onclick: () => { state.modalModel = p; renderAnswer(); }, onkeydown: (e) => {
        if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(e.key)) return;
        e.preventDefault(); const current = providers.indexOf(state.modalModel);
        const next = e.key === 'Home' ? 0 : e.key === 'End' ? providers.length - 1 : (current + (e.key === 'ArrowRight' ? 1 : -1) + providers.length) % providers.length;
        state.modalModel = providers[next]; renderAnswer(); $('answer-tab-' + providers[next]).focus();
      }}, source === 'dataset' ? names[p] : modelName(p))));
    renderAnswer(); if (!$('answer-dialog').open) $('answer-dialog').showModal();
  }
  function renderAnswer() {
    const p = state.modalModel, index = state.modalCase;
    const row = state.modalSource === 'dataset' ? state.cases[index] : state.result.datasets[p]?.[index];
    $('answer-tabs').querySelectorAll('[role=tab]').forEach((n) => {
      const selected = n.id === 'answer-tab-' + p; n.setAttribute('aria-selected', String(selected)); n.tabIndex = selected ? 0 : -1;
    });
    $('original-answer').setAttribute('aria-labelledby', 'answer-tab-' + p);
    const batch = state.modalSource === 'result' && state.result.evaluations[p];
    const record = batch?.jev_eval?.records?.[index], codeCase = results.resolvedCase(batch?.cases?.[index], record);
    const fingerprint = JSON.stringify([state.modalSource, state.run?.run_id, p, index, row, codeCase, record, batch?.live, state.run?.status]);
    if (state.modalAnswerRender === fingerprint) return;
    state.modalAnswerRender = fingerprint;
    const highlighted = row && window.AionaAnswerHighlights.create(row.answer, codeCase, record, node);
    renderAnswerEvaluation(p, index, highlighted);
    const box = $('original-answer'); box.replaceChildren();
    $('copy-answer').disabled = !row || (p !== 'aiona' && row.collection?.status === 'ERROR');
    if (!row) { box.append(node('p', {class: 'empty-response', text: '이 질문의 응답이 아직 수집되지 않았습니다.'})); return; }
    const meta = node('div', {class: 'answer-meta'}, node('span', {text: (state.modalSource === 'dataset' ? names[p] : modelName(p)) + ' · ' + number(row.answer.length) + '자 · 공백과 줄바꿈 포함'}));
    if (batch && !batch.live) meta.append(node('a', {href: `/code-eval.html?comparison=${state.run.run_id}&provider=${p}#results`, target: '_blank', rel: 'noopener noreferrer', text: '상세 판정 열기 ↗'}));
    box.append(meta);
    if (row.collection?.error) box.append(node('p', {class: 'alert warning', text: row.collection.error}));
    if (state.modalSource === 'result' && systemOf(p) && row.collection) box.append(collectionDetail(row.collection));
    box.append(highlighted.element);
  }
  function renderAnswerEvaluation(p, index, highlighted) {
    const box = $('answer-evaluation');
    const batch = state.modalSource === 'result' && state.result.evaluations[p];
    const record = batch?.jev_eval?.records?.[index], c = batch?.cases?.[index];
    const fingerprint = JSON.stringify([state.modalAnswerRender, state.run?.run_id, p, index, state.modalSource, c?.case_status, c?.summary?.common, record,
      state.codeRecheck?.run_id === state.run?.run_id ? state.codeRecheck?.providers?.[p]?.cases?.[index] || null : 'pending',
      !record || record.status === 'RUNNING' ? state.run?.status : null]);
    if (fingerprint === state.modalEvaluation) return;
    const context = JSON.stringify([state.run?.run_id, state.modalSource, p, index]);
    const detailKey = (detail) => detail.className + ':' + detail.querySelector(':scope > summary')?.textContent;
    const expanded = new Set(state.modalEvaluationContext === context ? [...box.querySelectorAll('details[open]')].map(detailKey) : []);
    // 수집 방식 비교는 평가 칸 밖의 별도 영역 — 평가 칸(#answer-evaluation)의 구조는 그대로 둔다
    const holder = $('answer-collection');
    const keepOpen = state.modalEvaluationContext === context && Boolean(holder.querySelector('details[open]'));
    holder.hidden = true; holder.replaceChildren();
    state.modalEvaluationContext = context;
    state.modalEvaluation = fingerprint; box.replaceChildren();
    box.hidden = !batch;
    if (!batch) return;
    const status = results.caseState(state.result, p, index);
    const imageNote = results.attachmentImageNote(record);
    const compared = state.modalSource === 'result' ? state.result?.collection_comparison : null;
    box.append(node('div', {class: 'answer-verdict'}, pill(status.text, status.kind), node('p', {text: status.detail})),
      node('div', {class: 'answer-checks'}, ...[['코드 검사', status.code], ['의미 평가', status.semantic]].map(([label, check]) =>
        node('div', {}, node('strong', {text: label}), pill(check.text, check.kind), node('p', {text: check.reason}),
          label === '의미 평가' && imageNote ? node('p', {class: 'field-help', 'data-attachment-images': '', text: imageNote}) : null))));
    if (compared?.questions?.[index]) {
      const details = collectionDetails(compared.questions[index], compared.sample);
      details.open = keepOpen;
      holder.append(details); holder.hidden = false;
    }
    const findings = node('ul', {class: 'finding-list'});
    const recheck = state.modalSource === 'result' && state.codeRecheck?.run_id === state.run?.run_id ? state.codeRecheck.providers?.[p] : null;
    const cleared = new Set(recheck?.cases?.[index]?.cleared || []);
    if ((recheck?.pasted_without_end || []).includes(String(index))) {
      box.append(node('p', {class: 'recheck-note', text: '이 질문의 붙여 넣은 자료에 끝 표시([자료 끝])가 없어 자료 속 문장이 요구 조건·진위 요청으로 읽혔을 수 있습니다. '
        + '원문 재평가로는 해결되지 않고, 변환된 질문 세트로 답변을 다시 수집하는 새 실행이 필요합니다.'}));
    }
    for (const r of status.code.checks.filter((r) => (r.counted && r.verdict === 'FAIL') || !['PASS', 'FAIL'].includes(r.verdict) && r.status !== 'NOT_APPLICABLE' && r.status !== 'DUPLICATE')) {
      findings.append(node('li', {}, node('strong', {text: r.explain?.headline || r.title || r.check_id}),
        node('p', {text: r.explain?.summary || r.reason || '이 조건을 판정하지 못했습니다.'}),
        r.source === 'history_instruction' ? node('p', {text: '이전 턴 지시에서 이어진 조건: ' + (r.explain?.requirement || r.title)}) : null,
        r.explain?.expected ? node('p', {text: '요구 조건: ' + r.explain.expected}) : null,
        r.explain?.actual ? node('p', {text: '실제 답변: ' + r.explain.actual}) : null,
        r.verdict === 'FAIL' && cleared.has(String(r.check_id)) ? node('p', {class: 'recheck-note',
          text: `현재 엔진(${state.codeRecheck.engine.replace('aiona-code-eval-', '')})으로 다시 검사하면 이 FAIL은 나오지 않습니다. `
            + `저장 당시 엔진(${String(recheck.stored_engine || '').replace('aiona-code-eval-', '') || '기록 없음'})이 답변을 다르게 읽은 결과입니다. `
            + '저장된 판정은 그대로이며, 원문 재평가(새 실행)에서 새 판정이 적용됩니다.'}) : null));
    }
    for (const [id, decision] of Object.entries(record?.decisions || {}).filter(([, d]) => d.decision === 'FAIL' || ['UNVERIFIED', 'INCOMPLETE'].includes(d.status))) {
      const criterion = batch.jev_eval.criteria?.find((c) => c.id === id);
      const outcome = id === 'J5' && decision.j5_outcome?.label ? ` (${results.j5Detail(decision.j5_outcome, decision.decision).title})` : '';
      findings.append(node('li', {}, node('strong', {text: ({UNVERIFIED: '확인 필요 · ', INCOMPLETE: '판정 미완료 · '}[decision.status] || '의미 평가 · ') + (criterion?.name || id) + outcome}),
        node('p', {text: decision.status === 'UNVERIFIED' ? results.conciseReason(id, decision.reason) :
          decision.status === 'INCOMPLETE' ? results.incompleteReason(id, decision) : decision.reason || '평가자가 FAIL 근거를 남기지 않았습니다.'})));
    }
    const locationCount = highlighted?.locations.segments.filter((segment) => segment.findingIds.length).length || 0;
    if (locationCount) {
      box.append(node('button', {type: 'button', class: 'button secondary failure-jump', 'data-failure-jump': '',
        onclick: () => highlighted.focus(0), text: `답변에서 FAIL 위치 ${number(locationCount)}곳 보기 ↓`}));
      if (findings.children.length) box.append(node('details', {class: 'evaluation-error-detail'}, node('summary', {text: 'FAIL 판정 근거 전체 보기'}), findings));
    } else if (findings.children.length) box.append(findings);
    const evidence = results.semanticEvidence(record, node);
    if (evidence) box.append(evidence);
    if (record?.decisions?.J5?.status === 'NOT_APPLICABLE') box.append(node('p', {class: 'hint', text: 'J5 해당 없음 · ' + (record.applicability?.not_applicable?.J5 || record.decisions.J5.reason || '검증할 사실 주장이 없습니다.')}));
    if (!status.semantic.complete && status.semantic.rawReason) box.append(node('details', {class: 'evaluation-error-detail'},
      node('summary', {text: status.semantic.unverified || status.semantic.incomplete ? '미판정 사유 원문 보기' : '평가 오류 원문 보기'}), node('pre', {text: status.semantic.rawReason})));
    box.querySelectorAll('details').forEach((detail) => { if (expanded.has(detailKey(detail))) detail.open = true; });
  }
  $('copy-answer').addEventListener('click', async () => {
    const row = state.modalSource === 'dataset' ? state.cases[state.modalCase] : state.result.datasets[state.modalModel]?.[state.modalCase];
    if (!row) return;
    try { await navigator.clipboard.writeText(row.answer); announce('답변 원문을 복사했습니다.'); }
    catch (_) { announce('복사 권한이 없습니다. 답변 원문을 직접 선택해 복사해 주세요.'); }
  });
  async function loadSetup() {
    if (state.settingsLoading) return;
    state.settingsLoading = true; renderSemanticSetup(); renderReady();
    window.dispatchEvent(new CustomEvent('aiona:semantic-setup'));
    const checks = await Promise.allSettled([loadProviders(), api('/api/code-eval/info'), api('/api/comparison/info'), api('/api/comparison/version-history')]);
    state.semanticSetup = checks[1].status === 'fulfilled' ? checks[1].value.semantic_setup || null : null;
    state.evaluationPolicyReady = checks[2].status === 'fulfilled' ? checks[2].value.evaluation_policy_version === 4 : null;
    state.comparisonReady = false; state.versionReady = false;
    const failure = checks.find((r) => r.status === 'rejected');
    if (failure) {
      serverConnection(false);
      const oldServer = checks[0].status === 'fulfilled' && checks[1].status === 'fulfilled' && checks[2].status === 'rejected' && checks[2].reason.status === 404;
      error(oldServer ? '실행 중인 서버가 이전 버전입니다. 서버를 재시작한 뒤 새로고침해 주세요.' :
        '로컬 서버에 연결하지 못했습니다. start.bat으로 실행해 주세요. ' + failure.reason.message);
    } else {
      updateCurrentRelease(checks[3].value);
      connectionRestored(); error(null);
      $('engine-label').textContent = checks[1].value.versions.engine + ' · 원문 보존';
      state.comparisonReady = checks[2].value.blind_version === 'comparison-blind-v1' && checks[2].value.auto_evaluate === true &&
        checks[2].value.blind_after_collection === true &&
        checks[2].value.parallel_collection === true && checks[2].value.parallel_evaluation === true &&
        collectionProviders.every((p) => checks[2].value.required_providers?.includes(p)) && checks[2].value.auto_collection === 'v1';
      eventStreaming = checks[2].value.evaluation_events === 'sse-v1';
      state.versionReady = checks[2].value.aiona_versions === 'v1';
      if (!state.evaluationPolicyReady || !state.comparisonReady) error('실행 중인 서버가 이전 버전입니다. 수정된 평가를 적용하려면 서버를 재시작한 뒤 새로고침해 주세요. 저장된 결과는 계속 확인할 수 있습니다.');
    }
    state.settingsLoading = false; renderSemanticSetup(); renderReady();
    $('evaluate-button').disabled = !canEvaluate();
    window.dispatchEvent(new CustomEvent('aiona:semantic-setup'));
  }
  document.querySelectorAll('[data-refresh-setup]').forEach((button) => button.addEventListener('click', loadSetup));
  async function initialize() {
    renderResult(); go(location.hash.slice(1) || 'setup');
    await loadSetup();
  }
  setInterval(updateLiveClock, 1000);
  window.AionaWorkspace = {api, node, pill, percent, number, go, openRun, canEvaluate, icon, avatar, announce, openAnswer, startEvaluation, renderReady,
    currentRun: () => state.run,
    currentResult: () => state.result,
    isBusy: active,
    isReady: () => state.comparisonReady,
    evaluatorNotice: () => canEvaluate() ? null : evaluatorNotice(),
    versionMode: () => state.versionMode,
    currentRelease: () => state.currentRelease,
    dataset: () => ({cases: state.cases, name: state.fileName, importInfo: state.importInfo, inputMode: state.inputMode, loading: state.datasetLoading}),
    showError: error,
    // 저장된 자동 수집 작업을 다시 열 때 — 새 요청 없이 그 실행의 AIONA 원문을 평가 데이터로 보여 준다
    restoreDataset(run) {
      if (state.versionMode || state.cases.length || !run?.datasets?.aiona?.length) return false;
      const document = run.input_document;
      setDataset(run.datasets.aiona, run.auto_collection?.source_name || document?.source_name || run.name,
        document ? {...document, converted: true} : null, 'restore');
      return true;
    },
    prepareVersion(version) {
      setInputMode('file'); state.fileDraft = null;
      ++state.uploadVersion; state.datasetLoading = false;
      state.cases = []; state.fileName = ''; state.importInfo = null;
      $('aiona-version').value = version || ''; $('aiona-collected-at').value = ''; $('aiona-release-notes').value = ''; $('run-name').value = '';
      $('dataset-content').hidden = true; $('dropzone').hidden = false;
      $('dataset-loading').hidden = true;
      ['choose-file', 'replace-file', 'sample-button'].forEach((id) => { $(id).disabled = false; });
      $('dataset-panel').setAttribute('aria-busy', 'false');
      setVersionMode(true); go('setup'); $('aiona-version').focus();
      window.dispatchEvent(new CustomEvent('aiona:dataset-changed', {detail: {reason: 'version-reset'}}));
    }};
  initialize();
})();
