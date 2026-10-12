(() => {
'use strict';
// 모든 입력·결과 문자열은 textContent 로만 넣는다 (답변 속 HTML·스크립트를 실행하지 않음).
const $ = (id) => document.getElementById(id);
const state = { failSub: null, info: null, batch: null, jobId: null, exportBase: null, filter: 'all', search: '', selected: null, override: null, batchText: null, batchName: '' };
const SUB_NAMES = { '1-1': '응답·문자 정상성', '1-2': '문서·코드 유효성', '1-3': '참조 연결 유효성', '2-1': '분량·표현 조건', '2-2': '형식·구성 조건', '3-1': '계산 결과 일치', '3-2': '선언 개수 일치', '3-3': '원본 보존·변환 일치' };
const CAT_NAMES = { '1': '출력 무결성', '2': '요구 조건 준수', '3': '수치·데이터 정합성' };
const SPECIAL_NAMES = { 'AIONA-M': '내부 마커 노출', 'AIONA-F': '금지 표현 출현' };
const STATUS_KO = { EXECUTED: '검사 완료', CONFIG_MISSING: '판정 안 함 · 설정 없음', DISPLAY_MISSING: '판정 안 함 · 표시본 없음', INCOMPLETE: '일부만 검사', UNSUPPORTED: '판정 안 함 · 확정 불가', DEPENDENCY_FAILED: '판정 안 함 · 앞선 검사 실패', CHECK_ERROR: '검사 오류', LIMIT_EXCEEDED: '입력 너무 큼', OUT_OF_SCOPE: '적용 대상 아님', DUPLICATE: '중복(한 번만 셈)', APPLICABILITY_EXCEPTION: '적용 제외' };
const semanticLabel = (value) => window.AionaResults.semanticEngineLabel(value);
const returnLink = $('workspace-return');
if (returnLink) {
  try {
    const view = sessionStorage.getItem('aiona-workspace-view');
    if (['setup', 'connections', 'results', 'comparison', 'history', 'versions', 'criteria'].includes(view)) returnLink.href = '/workspace.html#' + view;
  } catch (_) { /* The plain workspace link works without browser storage. */ }
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}
const fmt = (v) => v === null || v === undefined ? '—' : (typeof v === 'string' ? v : JSON.stringify(v, null, 1));
const rate = (r) => r === null || r === undefined ? '—' : (r * 100).toFixed(1) + '%';
const llmStatus = { COMPLETED: '검토 완료', NO_CODE_FAIL: '대상 없음', ACCESS_REQUIRED: '모델 연결 필요', PARTIAL: '일부 미완료' };
const JEV_STATUS = { SUCCESS: '완료', PARTIAL: '일부 미완료', NOT_CONFIGURED: '연결 설정 필요 · 미실행', API_ERROR: 'API 오류', TIMEOUT: '시간 초과',
  INVALID_RESPONSE: '응답 형식 오류', SKIPPED: '입력 오류로 건너뜀', NO_TARGET: '대상 없음' };
// Jev 기록은 batch.cases 와 같은 순서로 저장된다 (같은 case_id 가 있어도 순서로 대응).
function jevRecord(c) { const j = state.batch.jev_eval; return j && j.records ? j.records[state.batch.cases.indexOf(c)] || null : null; }
// 표시 ID는 중복될 수 있다. 탐색은 입력 배열의 위치를 사용한다.
function caseIndex(ref, conversationId) {
  const cases = state.batch.cases;
  if (Number.isInteger(ref.case_index) && cases[ref.case_index] && cases[ref.case_index].case_id === ref.case_id) return ref.case_index;
  // 예전 저장 결과에는 case_index가 없다. 유일하게 대응할 때만 연결한다.
  const matches = cases.map((c, i) => ({ c, i })).filter(({ c }) => c.case_id === ref.case_id
    && (ref.question === undefined || c.question === ref.question)
    && (conversationId === undefined || (c.conversation && c.conversation.id === conversationId && c.conversation.turn === ref.turn)));
  return matches.length === 1 ? matches[0].i : -1;
}
const jevFails = (rec) => rec && rec.decisions ? Object.values(rec.decisions).filter((d) => d.decision === 'FAIL').map((d) => d.criterion_id) : [];
const jevUnverified = (rec) => Object.values(rec?.decisions || {}).filter((d) => ['UNVERIFIED', 'INCOMPLETE'].includes(d.status) && !['PASS', 'FAIL'].includes(d.decision));
const verdictLabel = (v, status) => v || (['OUT_OF_SCOPE', 'APPLICABILITY_EXCEPTION'].includes(status)
  ? '해당 없음' : status === 'UNVERIFIED' ? '확인 필요' : status === 'DUPLICATE' ? '중복 제외' : '판정 미완료');
const badge = (v, status) => el('span', { class: 'v ' + (v || 'NONE'), text: verdictLabel(v, status) });

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `요청 실패 (${res.status})`);
  return body;
}

// ---- 탭 -------------------------------------------------------------------------
function revealActiveTab() {
  const strip = document.querySelector('.tab-strip');
  const activeTab = strip?.querySelector('[aria-selected="true"]');
  if (!activeTab || strip.scrollWidth <= strip.clientWidth) return;
  const outer = strip.getBoundingClientRect(), item = activeTab.getBoundingClientRect();
  if (item.left < outer.left) strip.scrollLeft += item.left - outer.left;
  else if (item.right > outer.right) strip.scrollLeft += item.right - outer.right;
}
window.addEventListener('resize', revealActiveTab);
function showTab(name) {
  document.querySelectorAll('.nav-tab').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === name)));
  document.querySelectorAll('.view').forEach((v) => { v.hidden = v.id !== 'view-' + name; });
  try { history.replaceState(null, '', '#' + name); } catch (_) { /* noop */ }
  revealActiveTab();
}
document.querySelectorAll('.nav-tab').forEach((t) => t.addEventListener('click', () => showTab(t.dataset.tab)));

// ---- 기준 열람 (엔진 정의에서 생성) ------------------------------------------------
function renderCriteria(info) {
  const tax = info.taxonomy;
  $('engine-version').textContent = info.versions.engine;
  const grid = $('taxonomy-grid'); grid.replaceChildren();
  tax.categories.forEach((cat) => {
    grid.append(el('article', { class: 'tax-card c' + cat.id },
      el('div', { class: 'tax-num', text: '0' + cat.id }), el('h2', { text: cat.name }),
      el('p', { class: 'hint', text: cat.question }),
      ...cat.subs.map((s) => el('div', { class: 'tax-sub' }, el('strong', {}, el('span', { class: 'row-id', text: s.id }), s.name), el('span', { text: s.target })))));
  });
  $('principles').replaceChildren(...tax.principles.map((p) => el('li', { text: p })));
  const methods = $('methods-body'); methods.replaceChildren();
  tax.categories.forEach((cat) => {
    methods.append(el('h2', { class: 'mt', text: `0${cat.id} ${cat.name}` }));
    cat.subs.forEach((s) => methods.append(el('article', { class: 'card method-card' },
      el('h3', {}, el('span', { class: 'row-id', text: s.id }), s.name),
      el('dl', {}, ...[['검사 대상', s.target], ['코드 판정 방법', s.method], ['PASS', s.pass], ['FAIL', s.fail], ['판정 근거', s.basis], ['구현 위치', s.trace]]
        .flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })])))));
  });
  if (tax.jev) {
    methods.append(el('h2', { class: 'mt jev-head', text: `의미 평가 (J1~J${tax.jev.criteria.length})` }), el('p', { class: 'hint', text: tax.jev.code_scope_note }));
    tax.jev.criteria.forEach((c) => methods.append(el('article', { class: 'card method-card jev-card' },
      el('h3', {}, el('span', { class: 'row-id jev', text: c.id }), `${c.name} (${c.name_en})`),
      el('dl', {}, ...[['판단 질문', c.question], ['적용 조건', c.applies_when || ''], ['PASS', c.pass], ['FAIL', c.fail], ['CODE와의 경계', c.boundary], ['예시', c.example]]
        .flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })])))));
  }
  const pt = $('policy-table');
  pt.replaceChildren(el('thead', {}, el('tr', {}, ...['평가 항목', '위반 조건 · 원문', '코드 비교 방식', '필요한 입력'].map((h) => el('th', { text: h })))),
    el('tbody', {}, ...tax.specialized.map((s) => el('tr', {}, el('th', { text: s.name }), el('td', { text: s.condition + ` (MD ${s.md_line}행)` }), el('td', { text: s.method }), el('td', { text: s.requires })))));
  renderConfig(info.aiona_config);
  const md = tax.md;
  $('md-title').textContent = `${md.source} · 채택 조건·검사 전제 발췌` + (md.sha256 ? ` (sha256 ${md.sha256.slice(0, 12)}…)` : '');
  $('md-lines').replaceChildren(...md.lines.map((l) => el('div', { class: 'md-line' + (l.retired ? ' retired' : '') }, el('span', { class: 'n', text: l.line }), el('span', { text: l.text }))));
  $('md-note').textContent = md.retired_note + ' 화면 표시명 변경: 169행 ‘고아 닫는 태그’ → ‘짝이 없는 닫는 태그’.';
  $('refs').replaceChildren(...tax.references.map((r) => el('li', {}, el('a', { href: r.url, target: '_blank', rel: 'noopener noreferrer', text: r.name }), ' — ', r.scope)));
  const sel = $('example-select');
  tax.examples.forEach((ex, i) => sel.append(el('option', { value: String(i), text: ex.label })));
}

function renderConfig(c) {
  const rows = [
    ['적용 system_id', c.applies_to_system_ids.join(', ')],
    ['내부 마커 목록 경로', c.marker_source || '없음'],
    ['등록 마커 패턴 수', String(c.marker_patterns)],
    ['미등록 마커 범주', c.marker_categories_missing.join(' · ') || '없음'],
    ['질문별 금지 표현 경로', c.forbidden_source || '없음'],
    ['금지 표현 항목 수', c.forbidden_entries === null ? '목록 없음' : String(c.forbidden_entries)],
    ['설정 오류·누락', c.errors.join(' / ') || '없음'],
  ];
  $('policy-config').replaceChildren(...rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
  $('config-status').textContent = (state.override ? '업로드 설정 사용 중. ' : '') +
    `기본 설정: 마커 패턴 ${c.marker_patterns}건, 금지 표현 ${c.forbidden_entries === null ? '목록 없음' : c.forbidden_entries + '건'}. 설정이 없으면 특화 항목은 ‘판정 미완료 · 설정 없음’으로 남습니다.`;
}

// ---- 실행 ------------------------------------------------------------------------
function setError(msg) { const e = $('run-error'); e.hidden = !msg; e.textContent = msg || ''; }
function setProgress(done, total, label) {
  $('progress').hidden = false;
  $('progress-label').textContent = label || `진행 ${done} / ${total}`;
  $('progress-fill').style.width = total ? `${Math.round((done / total) * 100)}%` : '0%';
}

async function runJob(body) {
  setError('');
  if (state.override) body.aiona_config = state.override;
  $('run-single').disabled = true; $('run-batch').disabled = true;
  try {
    const job = await api('/api/code-eval/jobs', { method: 'POST', body: JSON.stringify(body) });
    state.jobId = job.job_id;
    state.exportBase = null;
    setProgress(job.done, job.total);
    let status = job;
    while (status.status === 'RUNNING') {
      await new Promise((r) => setTimeout(r, 250));
      status = await api('/api/code-eval/jobs/' + job.job_id);
      setProgress(status.done, status.total, status.phase === 'JEV'
        ? `CODE 검사 완료 · 의미 평가 중 ${status.done} / ${status.total}` : null);
    }
    if (status.status !== 'COMPLETED') throw new Error(status.error || '평가 실패');
    setProgress(status.done, status.total, `완료 ${status.done} / ${status.total}`);
    state.batch = await api(`/api/code-eval/jobs/${job.job_id}/result`);
    state.selected = null;
    renderResults();
    showTab('results');
  } catch (err) {
    setError(err.message);
  } finally {
    $('run-single').disabled = false; $('run-batch').disabled = !state.batchText;
  }
}

$('run-single').addEventListener('click', () => {
  const body = { question: $('q').value, answer: $('a').value };
  const cid = $('case-id').value.trim(); const sid = $('system-id').value.trim(); const disp = $('displayed').value;
  const ref = $('reference').value.trim();
  if (cid) body.case_id = cid;
  if (sid) body.system_id = sid;
  if (disp) body.displayed_text = disp;
  if (ref) { // JSON 으로 읽히면 객체·배열로, 아니면 문자열 그대로 (J5 DATA 신뢰도의 기준 데이터)
    try { body.reference = /^[\[{]/.test(ref) ? JSON.parse(ref) : ref; } catch (_) { body.reference = ref; }
  }
  runJob({ cases: [body] });
});
$('example-select').addEventListener('change', (e) => {
  const ex = state.info && state.info.taxonomy.examples[Number(e.target.value)];
  if (!ex) return;
  $('q').value = ex.question; $('a').value = ex.answer;
});
$('batch-file').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  state.batchText = null; $('run-batch').disabled = true;
  if (!file) { $('batch-info').textContent = '선택된 파일 없음'; return; }
  state.batchText = await file.text(); state.batchName = file.name;
  const lines = state.batchText.split(/\r?\n/).filter((l) => l.trim()).length;
  $('batch-info').textContent = `${file.name} · ${(file.size / 1024).toFixed(1)} KB · 비어 있지 않은 줄 ${lines}개 (항목 수는 서버가 파싱해 확인)`;
  $('run-batch').disabled = false;
});
$('run-batch').addEventListener('click', () => { if (state.batchText) runJob({ input_text: state.batchText, input_name: state.batchName }); });
$('config-file').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try { state.override = JSON.parse(await file.text()); setError(''); }
  catch (err) { state.override = null; setError('설정 파일 JSON 오류: ' + err.message); }
  renderConfig(state.info.aiona_config);
});
$('config-clear').addEventListener('click', () => { state.override = null; $('config-file').value = ''; renderConfig(state.info.aiona_config); });

// ---- 결과 ------------------------------------------------------------------------
const SUB_DESC = { '1-1': '답변이 비었거나 깨진 글자가 있는지', '1-2': '코드블록·JSON·파이썬 문법, ``` 와 $$ 짝', '1-3': '각주·인용 번호·문서 안 링크 연결',
  '2-1': '개수·글자 수·넣을 단어·뺄 단어', '2-2': '표·목록·제목·JSON 형식', '3-1': '계산이 맞는지', '3-2': '“다음 세 가지” 같은 개수 말과 실제 항목 수', '3-3': '원본 자료의 정렬·필터 결과' };
const COND_KO = { bullet_count: '불릿 개수', sentence_count: '문장 수', paragraph_count: '문단 수', line_count: '줄 수', word_count: '어절 수', list_count: '항목 개수',
  char_count: '글자 수', required_literal: '꼭 넣을 말', forbidden_literal: '쓰면 안 되는 말', starts_with: '시작 문구', ends_with: '끝 문구', charset: '허용 문자',
  explicit_empty: '빈 출력 요청', table_required: '표 요청', table_forbidden: '표 금지', table_columns: '표 열 이름', table_column_count: '표 열 수', table_header: '표 헤더',
  table_data_rows: '표 데이터 행 수', table_total_rows: '표 전체 행 수', table_rows_ambiguous: '표 행 수(헤더 포함 여부 불명)', list_required: '목록 형식 요청', list_forbidden: '목록 금지',
  heading_required: '제목 요청', heading_count: '제목 수', json_required: 'JSON 요청', json_required_keys: 'JSON 필수 키', json_field_type: 'JSON 값 종류',
  code_block_required: '코드블록 요청', code_block_count: '코드블록 개수', code_block_forbidden: '코드블록 금지', code_language_label: '코드 언어 표시',
  python_code_requested: '파이썬 코드 요청', code_requested: '코드 요청', error_example_requested: '오류 예시 요청', calculation: '계산', transform: '원본 자료 변환',
  unique_values: '중복 금지', aggregate: '집계', markdown_forbidden: '마크다운 금지' };
function kpi(label, value, small, cls) { return el('div', { class: 'kpi' + (cls ? ' ' + cls : '') }, label, el('b', { text: String(value) }), small ? el('small', { text: small }) : null); }
const subName = (r) => r.area === 'common' ? `${r.subcategory} ${SUB_NAMES[r.subcategory]}` : `AIONA · ${SPECIAL_NAMES[r.subcategory]}`;

// 화면과 결과를 같은 서버가 만들므로 기대 엔진 버전은 서버 info 에서 받는다 (예전 저장 결과만 '예전 버전'으로 표시)
let EXPECTED_ENGINE = null;
function fallbackExplain(r) {
  return { headline: r.title + (r.verdict === 'FAIL' ? ' — 문제 있음' : r.verdict === 'PASS' ? ' — 통과' : ' — 판정 안 함'),
    summary: r.reason || '', checked: '', requirement: '', expected: r.required == null ? '' : fmt(r.required),
    actual: r.measured == null ? '' : fmt(r.measured), locations: [], fix: '', note: '이 결과는 예전 서버가 만들어 쉬운 설명이 없습니다. 서버를 다시 켠 뒤 다시 평가하면 자세한 설명이 나옵니다.' };
}
function prepareBatch(b) {
  let missing = 0;
  (b.cases || []).forEach((c) => (c.results || []).forEach((r) => { if (!r.explain) { r.explain = fallbackExplain(r); missing += 1; } }));
  return missing;
}
function staleBanner(text) {
  const box = $('stale-banner');
  box.hidden = !text; box.textContent = text || '';
}
const RESTART_HELP = '실행 창에서 Ctrl+C 로 서버를 끈 뒤 py -3 run.py 로 다시 켜고, 이 페이지를 새로고침(Ctrl+F5)해 주세요.';

function allFails() {
  const out = [];
  state.batch.cases.forEach((c, caseIndex) => (c.results || []).forEach((r) => { if (r.counted && r.verdict === 'FAIL') out.push({ c, r, caseIndex }); }));
  return out;
}

function renderResults() {
  if (globalThis.AionaResults) state.batch = globalThis.AionaResults.resolvedBatch(state.batch);
  const b = state.batch; const s = b.summary;
  const missing = prepareBatch(b);
  staleBanner(missing || b.versions.engine !== EXPECTED_ENGINE
    ? `⚠ 지금 실행 중인 서버가 예전 버전(${b.versions.engine})이라 FAIL 이유를 자세히 보여 줄 수 없습니다. ${RESTART_HELP}` : '');
  $('result-body').hidden = false; $('exports').hidden = false;
  $('result-lead').textContent = `엔진 ${b.versions.engine} · 실행 ${b.run.run_id} · ${b.run.duration_ms} ms · 결과 해시 ${b.result_digest.slice(0, 16)}` + (b.run.saved_to ? ` · 저장 ${b.run.saved_to}` : '');
  const exportBase = state.exportBase || `/api/code-eval/jobs/${state.jobId}/export`;
  ['json', 'jsonl', 'html'].forEach((f) => { $('export-' + f).href = `${exportBase}/${f}`; });
  if (b.run.save_error) {
    $('result-lead').append(el('br'), el('span', { class: 'error', role: 'alert' },
      el('strong', { text: '자동 저장 실패. ' }), '평가 결과는 계속 확인할 수 있습니다. 서버를 종료하기 전에 ',
      el('a', { href: `${exportBase}/json`, download: true, text: 'JSON을 직접 다운로드' }),
      `해 주세요. 사유: ${b.run.save_error}`));
  }
  $('kpis').replaceChildren(
    kpi('입력', s.totals.cases, `평가 ${s.totals.evaluated} · 입력 오류 ${s.totals.input_errors} · 한도 초과 ${s.totals.limit_exceeded}`),
    kpi('통과 (PASS)', s.common.pass, '검사해서 문제가 없던 조건', 'ok'),
    kpi('문제 (FAIL)', s.common.fail, `문제가 확인된 조건 · 케이스 ${new Set(allFails().filter((x) => x.r.area === 'common').map((x) => x.caseIndex)).size}건`, 'bad'),
    kpi('평가 미완료', s.common.unjudged, '아직 보완 판정을 받지 못한 공통 조건 · 오류 사유는 상세 결과에서 확인'),
    kpi('통과율', rate(s.common.pass_rate), 'PASS ÷ (PASS + FAIL)'),
    kpi('AIONA 특화', `${s.specialized.pass} / ${s.specialized.fail}`, `PASS / FAIL · 판정 안 함 ${s.specialized.unjudged}`),
    ...(b.llm_review ? [kpi('LLM 재검토', b.llm_review.queued_questions,
      `완료 ${b.llm_review.completed_questions} · ${llmStatus[b.llm_review.status] || b.llm_review.status}`)] : []),
    ...(b.jev_eval ? [kpi(`${semanticLabel(b.jev_eval)} 의미 평가`, JEV_STATUS[b.jev_eval.status] || b.jev_eval.status,
      `완료 ${b.jev_eval.completed} / ${b.jev_eval.targets} · 의미 평가 FAIL 있는 질문 ${b.jev_eval.records.filter((r) => jevFails(r).length).length}건`, 'jev')] : []));
  const head = el('thead', {}, el('tr', {}, ...['소분류', '무엇을 보나', 'PASS', 'FAIL', '판정 안 함', '통과율'].map((h) => el('th', { text: h }))));
  const body = el('tbody');
  let lastCat = '';
  Object.entries(s.by_subcategory).forEach(([sub, v]) => {
    const cat = sub[0];
    if (cat !== lastCat) { body.append(el('tr', { class: 'cat-row' }, el('td', { colspan: '6', text: `${cat}. ${CAT_NAMES[cat]}` }))); lastCat = cat; }
    const tr = el('tr', { class: v.fail ? 'has-fail clickable' : '', title: v.fail ? '누르면 이 소분류의 FAIL 만 봅니다' : null },
      el('td', { text: `${sub} ${SUB_NAMES[sub]}` }), el('td', { class: 'desc', text: SUB_DESC[sub] }), el('td', { text: v.pass }),
      el('td', { class: v.fail ? 'fail-num' : '', text: v.fail }), el('td', { text: v.unjudged }), el('td', { text: rate(v.pass_rate) }));
    if (v.fail) tr.addEventListener('click', () => { state.failSub = sub; renderFailOverview(); $('fail-overview').scrollIntoView({ block: 'start' }); });
    body.append(tr);
  });
  $('sub-table').replaceChildren(head, body);
  $('special-table').replaceChildren(el('thead', {}, el('tr', {}, ...['항목', 'PASS', 'FAIL', '판정 안 함', '통과율'].map((h) => el('th', { text: h })))),
    el('tbody', {}, ...Object.entries(s.by_specialized).map(([k, v]) => el('tr', {}, el('td', { text: SPECIAL_NAMES[k] }), el('td', { text: v.pass }), el('td', { text: v.fail }), el('td', { text: v.unjudged }), el('td', { text: rate(v.pass_rate) })))));
  renderCombined(b.combined);
  renderJevSummary(b);
  state.failSub = null;
  renderFailOverview();
  renderCaseList();
  const firstFail = allFails()[0];
  const first = firstFail ? firstFail.c : filteredCases()[0];
  if (first) selectCase(b.cases.indexOf(first));
}

// 답변의 틀린 부분 — 줄 번호와 해당 줄, 강조할 글자 (서버 combined 의 where)
function whereBlock(where) {
  if (!where) return el('span', { class: 'hint', text: '답변에서 위치를 특정하지 못했습니다 (이유 참고)' });
  return el('div', {}, el('div', { class: 'where-label', text: where.label }), el('div', { class: 'where-lines' }, ...where.lines.map((ln) => {
    const hit = ln.hits.find((h) => ln.text.includes(h));
    const i = hit ? ln.text.indexOf(hit) : -1;
    const body = i >= 0 ? [ln.text.slice(0, i), el('mark', { text: hit }), ln.text.slice(i + hit.length)] : [ln.text || ' '];
    return el('div', { class: 'wl' + (ln.mark ? ' mark' : '') }, el('span', { class: 'ln', text: String(ln.n) }), el('span', { class: 'lt' }, ...body));
  })));
}

// 종합 결과 — 서버가 계산한 질문 단위 합산(CODE 또는 의미 평가 FAIL = FAIL 질문, 가중치 점수 없음)
function renderCombined(s) {
  const sized = (node, width) => { node.style.width = width; return node; }; // CSP: style 속성 대신 CSSOM
  const box = $('combined'); box.replaceChildren(); box.hidden = !s;
  if (!s) return;
  const pct = (r) => (r === null || r === undefined ? '—' : `${Math.round(r * 100)}%`);
  const parts = [['PASS', s.pass, 'pass'], ['CODE·보완에서 발견', s.fail_code_only, 'code'], ['의미 평가에서 발견', s.fail_semantic_only, 'sem'], ['둘 다', s.fail_both, 'both'], ['평가 미완료', s.pending || 0, 'pending']];
  const total = s.questions || 1;
  box.append(el('div', { class: 'combined-head' },
    el('div', { class: 'combined-rate' }, el('b', { text: pct(s.rate) }), el('span', { text: '종합 통과율' })),
    el('div', {}, el('h2', { text: s.fail || s.pending ? `${s.questions}문항 중 PASS ${s.pass} · FAIL ${s.fail} · 평가 미완료 ${s.pending || 0}` : `${s.questions}문항 모두 통과` }),
      el('div', { class: 'stack' }, ...parts.filter(([, n]) => n).map(([label, n, cls]) => sized(el('span', { class: 'stack-' + cls, title: `${label} ${n}` }), `${(n / total) * 100}%`))),
      el('div', { class: 'stack-legend' }, ...parts.map(([label, n, cls]) => el('span', {}, el('i', { class: 'stack-' + cls }), `${label} `, el('b', { text: String(n) })))),
      el('p', { class: 'hint', text: `CODE 평가${s.semantic_engine ? ` + 의미 평가(${s.semantic_engine})` : ''} · 질문마다 하나라도 FAIL이면 FAIL 질문으로 셉니다(가중치 점수 아님).`
        + (s.semantic_pending ? ` 의미 평가를 하지 못한 ${s.semantic_pending}문항은 미완료로 표시하며 PASS로 처리하지 않습니다.` : '') }))));
  // 이 실행에 적용된 질문이 없는 의미 평가 기준(예: 단일 질문·답변 입력의 J6)은 막대를 그리지 않는다
  box.append(el('h3', { class: 'mt', text: '영역별 통과율 · 질문 기준' }), ...s.areas.filter((a) => a.group === 'CODE' || a.targets).map((a) => {
    const tag = a.group === 'CODE' ? 'code' : 'sem';
    return el('div', { class: 'area-row' }, el('span', { class: 'area-tag ' + tag, text: a.group }),
      el('span', { class: 'area-name', text: `${a.group === 'CODE' ? '' : a.id + ' '}${a.name}` }),
      el('span', { class: 'bar' }, sized(el('span', { class: 'bar-fill ' + tag }), `${a.rate === null ? 0 : a.rate * 100}%`)),
      el('span', { class: 'area-rate' + (a.fail ? ' is-fail' : ''), text: a.rate === null ? '—' : `${(a.rate * 100).toFixed(1)}%` }),
      el('span', { class: 'area-count', text: a.pass + a.fail ? `FAIL ${a.fail} · 대상 ${a.targets}` : (a.targets ? '미평가' : '대상 없음') }));
  }));
  if (s.problems.length) {
    box.append(el('h3', { class: 'mt', text: `FAIL 질문 ${s.problems.length}개` }), el('div', { class: 'table-wrap' }, el('table', { class: 'fo-table' },
      el('thead', {}, el('tr', {}, ...['질문', 'FAIL 난 기준', '이유', '틀린 부분'].map((h) => el('th', { text: h })))),
      el('tbody', {}, ...s.problems.flatMap((p) => p.items.map((it) => {
        const tag = it.source === 'CODE' ? 'code' : 'sem';
        const detail = (it.detail || it.source) + (it.confidence === null ? '' : ` · 판단 확신도 ${Math.round(it.confidence * 100)}%`);
        const tr = el('tr', { class: 'clickable' }, el('td', { class: 'cid', text: p.case_id }),
          el('td', { class: 'crit-cell' }, el('span', { class: 'crit-id ' + tag, text: it.id || '' }), ' ', el('strong', { text: it.name || it.label }),
            el('div', { class: 'hint', text: detail })),
          el('td', { text: it.reason }), el('td', { class: 'where-cell' }, whereBlock(it.where)));
        tr.addEventListener('click', () => openResult(caseIndex(p)));
        return tr;
      }))))));
  }
}

function renderJevSummary(b) {
  const box = $('jev-summary'); box.replaceChildren();
  const j = b.jev_eval;
  box.append(el('h2', { text: `Semantic Evaluation — ${semanticLabel(j)}` }),
    el('p', { class: 'hint', text: `${j && j.criteria && j.criteria.length ? `이 실행에 저장된 ${j.criteria.length}개 기준의` : '저장된'} 의미 평가 결과입니다. CODE 판정·통과율과 합치지 않고 따로 봅니다. 수치는 질문(턴) 수입니다.` }));
  if (j && ['claude_cli', 'codex_cli', 'openai_api', 'anthropic_api'].includes(j.engine)) box.append(el('p', { class: 'hint', text: `${semanticLabel(j)}로 판단한 의미 평가 결과입니다. 기준과 질문별 평가 기준은 같고, 판단 확신도는 모델이 스스로 적은 값이라 보정된 확률이 아닙니다.` }));
  if (!j) { box.append(el('p', { class: 'hint', text: '의미 평가 결과가 없습니다.' })); return; }
  box.append(el('p', { class: 'jev-status', text: `상태 ${JEV_STATUS[j.status] || j.status} (${j.status}) · 대상 ${j.targets} · 완료 ${j.completed}`
    + (j.models.length ? ` · 모델 ${j.models.join(', ')}` : '')
    + (j.client?.reasoning_effort ? ` · 추론 ${j.client.reasoning_effort}` : '') + ` · ${j.duration_ms} ms` }));
  if (j.access_reason) box.append(el('p', { class: 'hint', text: j.access_reason }));
  box.append(el('p', { class: 'hint', text: j.criteria && j.criteria.length
    ? '이 실행에 저장된 기준 설명을 표시합니다. 케이스를 고르면 당시 선택한 기준과 적용 근거가 보입니다.'
    : '평가 당시 기준 정의가 저장되지 않은 결과입니다. 기록에 있는 기준과 판정만 확인할 수 있습니다.' }));
  const asks = Object.fromEntries((j.criteria || []).map((c) => [c.id, c.question]));
  box.append(el('div', { class: 'table-wrap' }, el('table', {},
    el('thead', {}, el('tr', {}, ...['기준', '무엇을 보나', '평가 대상 질문', 'PASS', 'FAIL', '미평가'].map((h) => el('th', { text: h })))),
    el('tbody', {}, ...Object.entries(j.summary).map(([id, v]) => { const asked = v.targets ?? j.targets; return el('tr', {}, el('td', { text: `${id} ${v.name}` }), el('td', { class: 'desc', text: asks[id] || '' }),
      el('td', { text: asked }), el('td', { text: v.pass }), el('td', { class: v.fail ? 'fail-num' : '', text: v.fail }), el('td', { text: asked - v.pass - v.fail })); })))));
  renderConversations(box, j.conversations);
}

// 대화 유지율(J6) — 대화 입력(conversation_id·turn)이 있을 때만. 예전 결과 파일에는 conversations 가 없을 수 있다.
function renderConversations(box, conv) {
  if (!conv || !conv.count) return;
  const pct = (r) => (r === null || r === undefined ? '—' : `${Math.round(r * 100)}%`);
  box.append(el('h3', { class: 'mt', text: '대화 유지율 · J6 LONG CONTEXT' }),
    el('p', { class: 'hint', text: `대화 ${conv.count}개 · PASS ${conv.pass} · FAIL ${conv.fail} · 미완료 ${conv.incomplete} · 유지율 ${pct(conv.rate)}. ${conv.note || ''}` }));
  const bands = Object.entries(conv.bands || {}).filter(([, b]) => b.targets);
  if (bands.length) {
    box.append(el('div', { class: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {}, ...['턴 구간', 'J6 평가 턴', 'PASS', 'FAIL', '유지율'].map((h) => el('th', { text: h })))),
      el('tbody', {}, ...bands.map(([label, b]) => el('tr', {}, el('td', { text: `${label}턴` }), el('td', { text: b.targets }), el('td', { text: b.pass }),
        el('td', { class: b.fail ? 'fail-num' : '', text: b.fail }), el('td', { text: pct(b.rate) })))))));
  }
  box.append(el('div', { class: 'table-wrap' }, el('table', {},
    el('thead', {}, el('tr', {}, ...['대화', '턴 수', 'J6 평가 턴', '결과', '첫 FAIL 턴', '턴별 J6', '비고'].map((h) => el('th', { text: h })))),
    el('tbody', {}, ...conv.items.map((it) => {
      const first = it.turn_results.find((t) => t.turn === it.first_fail_turn);
      const strip = el('span', {}, ...it.turn_results.filter((t) => t.asked).map((t) => {
        const chip = el('button', { type: 'button', class: 'link-btn', title: `${t.turn}턴 J6 ${t.decision || '미실행'}`, onclick: () => openResult(caseIndex(t, it.id)) },
          badge(t.decision, t.decision ? 'EXECUTED' : 'INCOMPLETE'), ` ${t.turn} `);
        return chip;
      }));
      const tr = el('tr', {}, el('td', { class: 'cid', text: it.id }), el('td', { text: it.turns }), el('td', { text: it.j6_turns }),
        el('td', {}, it.result === 'INCOMPLETE' ? el('span', { class: 'st', text: '미완료' }) : badge(it.result, 'EXECUTED')),
        el('td', {}, first ? el('button', { type: 'button', class: 'link-btn', onclick: () => openResult(caseIndex(first, it.id)) }, `${it.first_fail_turn}턴`) : '—'),
        el('td', {}, strip), el('td', { class: 'desc', text: it.issues.length ? it.issues.join(', ') : '—' }));
      return tr;
    })))));
}

// 적용 근거를 찾은 곳 — 질문(J1~J4) · 답변(J5) · 참조 데이터(J5) · 대화(J6)
const SOURCE_LABEL = { question: '질문 근거', answer: '답변 근거', reference: '참조 데이터', conversation: '대화' };
const J5_BASIS = { reference: '판정 기준: 참조 데이터', web: '판정 기준: 웹 출처', unverified: '판정 기준: 확인 필요', engine: '판정 기준: 엔진 지식 (참조 데이터 없음)' };

// 코드 규칙으로 정한 적용 기준과 근거(질문·답변·참조 데이터·대화)·볼 것, 결과(또는 미실행), 해당 없음 기준.
function jevBlock(rec) {
  const box = el('div', { class: 'jev-box' }, el('div', { class: 'jev-title', text: `Semantic Evaluation — ${semanticLabel(state.batch.jev_eval)}` }));
  if (!rec) { box.append(el('p', { class: 'hint', text: '의미 평가 결과가 없습니다.' })); return box; }
  const app = rec.applicability || {}; const evidence = app.evidence || {}; const decisions = rec.decisions || {};
  const all = state.batch.jev_eval.criteria || [];
  const asked = rec.criteria && rec.criteria.length ? rec.criteria : all.map((c) => c.id);
  const rows = all.filter((c) => asked.includes(c.id)).map((c) => {
    const d = decisions[c.id];
    return el('tr', { class: d && d.decision === 'FAIL' ? 'jev-fail' : '' }, el('th', { text: `${c.id} ${c.name}` }),
      el('td', {}, evidence[c.id] ? evidence[c.id].look_for : c.question,
        evidence[c.id] ? el('div', { class: 'jev-reason', text: `${SOURCE_LABEL[evidence[c.id].source || 'question'] || '근거'}: ‘${evidence[c.id].excerpt}’ · ${evidence[c.id].rules.join(' · ')}` }) : null,
        c.id === 'J5' && rec.j5_basis ? el('div', { class: 'jev-reason', text: J5_BASIS[rec.j5_basis] || rec.j5_basis }) : null),
      el('td', {}, d ? badge(d.decision, d.status || 'EXECUTED') : el('span', { class: 'st', text: '미실행' }),
        d && ['PASS', 'FAIL'].includes(d.decision) && Number.isFinite(d.confidence) ? el('span', { class: 'jev-conf', text: ` 판단 확신도 ${Math.round(d.confidence * 100)}%` }) : null,
        d && d.reason ? el('div', { class: 'jev-reason', text: d.reason }) :
          d && d.decision === 'FAIL' ? el('div', { class: 'jev-reason', text: '평가자가 FAIL 근거를 남기지 않았습니다.' }) : null));
  });
  box.append(el('div', { class: 'table-wrap' }, el('table', { class: 'jev-table' },
    el('thead', {}, el('tr', {}, ...['평가할 기준', '이 질문에서 볼 것', '결과'].map((h) => el('th', { text: h })))), el('tbody', {}, ...rows))));
  const verification = window.AionaResults.semanticEvidence(rec, el);
  if (verification) box.append(verification);
  Object.entries(app.not_applicable || {}).forEach(([cid, reason]) => {
    const c = all.find((x) => x.id === cid);
    box.append(el('p', { class: 'hint', text: `해당 없음 · ${cid} ${c ? c.name : ''}: ${reason}` }));
  });
  const ctx = rec.context;
  if (ctx && ctx.history_turns) box.append(el('p', { class: 'hint', text: `대화 맥락: 앞 턴 ${ctx.history_turns}개 함께 보냄${ctx.history_truncated ? ' · 긴 답변 일부 생략' : ''}` }));
  if (ctx && ctx.issues && ctx.issues.length) box.append(el('p', { class: 'hint', text: `대화 이력 문제: ${ctx.issues.join(', ')}` }));
  box.append(el('p', { class: 'hint', text: rec.status === 'SUCCESS'
    ? [rec.model, rec.latency_ms != null ? `${rec.latency_ms} ms` : ''].filter(Boolean).join(' · ')
    : `${semanticLabel(state.batch.jev_eval)} 상태: ${JEV_STATUS[rec.status] || rec.status} (${rec.status})${rec.status !== 'NOT_CONFIGURED' && rec.error ? ' · ' + rec.error : ''}` }));
  return box;
}

function renderFailOverview() {
  const box = $('fail-overview'); box.replaceChildren();
  let fails = allFails();
  const total = fails.length;
  if (state.failSub) fails = fails.filter((x) => x.r.subcategory === state.failSub);
  box.append(el('div', { class: 'fo-head' }, el('h2', { text: `FAIL 한눈에 보기 (${fails.length}건)` }),
    state.failSub ? el('button', { type: 'button', class: 'chip-btn', onclick: () => { state.failSub = null; renderFailOverview(); } }, `${state.failSub} ${SUB_NAMES[state.failSub] || ''} 만 보는 중 ✕`) : null));
  if (!total) { box.append(el('p', { class: 'ok-msg', text: '문제가 확인된 조건이 없습니다. (판정하지 않은 항목은 각 케이스에서 사유를 볼 수 있습니다)' })); return; }
  box.append(el('p', { class: 'hint', text: '행을 누르면 해당 케이스로 이동해 문제 위치와 고치는 방법을 펼쳐 보여 줍니다.' }));
  const tbody = el('tbody');
  fails.slice(0, 300).forEach(({ c, r, caseIndex }) => {
    const tr = el('tr', { class: 'clickable' }, el('td', { class: 'cid', text: c.case_id }), el('td', { text: subName(r) }),
      el('td', {}, el('strong', { text: r.explain.headline }), el('div', { class: 'fo-sum', text: r.explain.summary })),
      el('td', { class: 'fo-q', text: c.question || '' }));
    tr.addEventListener('click', () => openResult(caseIndex, r.condition_id));
    tbody.append(tr);
  });
  box.append(el('div', { class: 'table-wrap' }, el('table', { class: 'fo-table' },
    el('thead', {}, el('tr', {}, ...['케이스', '항목', '무엇이 문제인가', '질문'].map((h) => el('th', { text: h })))), tbody)));
  if (fails.length > 300) box.append(el('p', { class: 'hint', text: `외 ${fails.length - 300}건 — 소분류 표에서 좁혀 보세요.` }));
}

function openResult(index, condId) {
  if (!Number.isInteger(index) || !state.batch.cases[index]) return;
  state.filter = 'all'; state.search = ''; $('case-search').value = '';
  document.querySelectorAll('#case-filters .filter').forEach((x) => x.setAttribute('aria-pressed', String(x.dataset.filter === 'all')));
  selectCase(index, condId);
}

function filteredCases() {
  const q = state.search.toLowerCase();
  return state.batch.cases.filter((c) => {
    if (state.filter === 'error' && c.case_status === 'EVALUATED') return false;
    if (state.filter === 'fail' && !(c.summary && (c.summary.common.fail || c.summary.specialized.fail)) && !jevFails(jevRecord(c)).length) return false;
    if (state.filter === 'unjudged' && !(c.summary && (c.summary.common.unjudged || c.summary.specialized.unjudged)) && !jevUnverified(jevRecord(c)).length) return false;
    if (q && !(c.case_id.toLowerCase().includes(q) || (c.question || '').toLowerCase().includes(q))) return false;
    return true;
  });
}

function renderCaseList() {
  const list = $('case-list'); list.replaceChildren();
  const cases = filteredCases();
  cases.slice(0, 500).forEach((c) => {
    const index = state.batch.cases.indexOf(c);
    const s = c.summary;
    const jf = jevFails(jevRecord(c));
    const unverified = jevUnverified(jevRecord(c)).length;
    const fails = (s ? s.common.fail + s.specialized.fail : 0) + jf.length; // CODE FAIL + 의미 평가 FAIL
    const tag = c.case_status !== 'EVALUATED' ? c.case_status : (fails ? `FAIL ${fails}건` : unverified ? `확인 필요 ${unverified}건` : 'FAIL 없음');
    list.append(el('li', {}, el('button', { type: 'button', 'aria-current': String(state.selected === index), onclick: () => selectCase(index) },
      el('span', { class: 'cid', text: c.case_id }), ' ', el('span', { class: fails ? 'tag bad' : 'tag', text: tag }),
      jf.length ? el('span', { class: 'tag jev', text: ` · ${semanticLabel(state.batch.jev_eval)} FAIL ${jf.join(' ')}` }) : null,
      fails && unverified ? el('span', {class: 'tag', text: ` · 확인 필요 ${unverified}건`}) : null, el('span', { class: 'cq', text: c.question || c.input_error || '' }))));
  });
  if (cases.length > 500) list.append(el('li', { class: 'hint', text: `외 ${cases.length - 500}건 — 검색으로 좁혀 주세요.` }));
  if (!cases.length) list.append(el('li', { class: 'hint', text: '조건에 맞는 케이스 없음' }));
}

function highlighted(text, ranges) {
  const pre = el('div', { class: 'pre' });
  const valid = ranges.filter((r) => Number.isInteger(r[0]) && Number.isInteger(r[1]) && r[1] > r[0]).sort((a, b) => a[0] - b[0]);
  let pos = 0;
  valid.forEach(([s, e]) => { if (s < pos) return; pre.append(text.slice(pos, s)); pre.append(el('mark', { text: text.slice(s, e) })); pos = e; });
  pre.append(text.slice(pos));
  return pre;
}

function renderLoc(l) {
  const label = el('div', { class: 'loc-label' }, l.label + (l.target === 'displayed_text' ? ' (표시본)' : ''), l.tag ? el('span', { class: 'chip', text: l.tag }) : null);
  if (l.kind === 'snippet' && l.segments) return el('div', { class: 'loc' }, label, el('div', { class: 'loc-line' }, ...l.segments.map((sg) => sg.mark ? el('mark', { text: sg.text }) : sg.text)));
  if (l.kind === 'snippet') return el('div', { class: 'loc' }, label, el('div', { class: 'loc-line' }, l.before, el('mark', { text: l.hit }), l.after));
  if (l.kind === 'code') {
    return el('div', { class: 'loc' }, label, el('div', { class: 'code-view' }, ...l.lines.map((ln) => el('div', { class: 'code-line' + (ln.mark ? ' mark' : '') },
      el('span', { class: 'ln', text: ln.n }), el('span', { class: 'lt', text: ln.text || ' ' }), ln.mark && l.pointer ? el('span', { class: 'ptr', text: '◀ ' + l.pointer }) : null))));
  }
  if (l.kind === 'list') {
    return el('div', { class: 'loc' }, label, el('ol', { class: 'loc-list' }, ...l.items.map((t, i) => el('li', { class: l.marks.includes(i) ? 'mark' : '', text: t }))),
      l.more ? el('p', { class: 'hint', text: `외 ${l.more}개` }) : null);
  }
  return null;
}

function resultDetail(r) {
  const x = r.explain;
  const rows = [['조건 ID', `${r.check_id} (${r.condition_id})`], ['분류', `${r.subcategory} ${r.subcategory_name || ''} · ${r.area === 'common' ? '공통' : 'AIONA 특화'}`],
    ['출처', `${r.source}${r.policy_version ? ' · ' + r.policy_version : ''}`], ['적용 서비스', r.applies_to], ['측정 범위', r.scope], ['상태', `${r.status} (${STATUS_KO[r.status] || ''})`],
    ['판정', verdictLabel(r.verdict, r.status) + (r.verdict ? '' : ' (null)')], ['요구값', fmt(r.required)], ['측정값', fmt(r.measured)], ['비교식', r.comparison || '—'], ['엔진 사유', r.reason],
    ['근거 위치(문자 번호)', r.evidence.map((e) => `${e.target} [${e.start ?? '?'}:${e.end ?? '?'}] ${e.excerpt}${e.note ? ' (' + e.note + ')' : ''}`).join('\n') || '—'],
    ['규칙 버전', r.check_version]];
  if (r.duplicate_of) rows.push(['중복 참조', r.duplicate_of]);
  const whyTitle = r.verdict === 'FAIL' ? '왜 FAIL 인가요?' : r.verdict === 'PASS' ? '결과'
    : ['OUT_OF_SCOPE', 'APPLICABILITY_EXCEPTION'].includes(r.status) ? '왜 적용하지 않았나요?'
    : r.status === 'DUPLICATE' ? '왜 중복 제외했나요?' : '왜 판정하지 않았나요?';
  const sec = (title, body, cls) => el('div', { class: 'sec' + (cls ? ' ' + cls : '') }, el('div', { class: 'sec-title', text: title }), el('p', { text: body }));
  return el('div', { class: 'res-detail' },
    sec('무엇을 검사했나', x.checked),
    x.requirement ? sec('질문에서 읽은 요구', `“${x.requirement}”`) : null,
    el('div', { class: 'why ' + (r.verdict || 'NONE') }, el('div', { class: 'sec-title', text: whyTitle }), el('p', { text: x.summary })),
    (x.expected || x.actual) ? el('div', { class: 'vs' },
      el('div', { class: 'vs-card' }, el('div', { class: 'sec-title', text: '기대한 것' }), el('p', { text: x.expected || '—' })),
      el('div', { class: 'vs-card actual ' + (r.verdict || 'NONE') }, el('div', { class: 'sec-title', text: '실제 답변' }), el('p', { text: x.actual || '—' }))) : null,
    x.locations.length ? el('div', { class: 'locs' }, el('div', { class: 'sec-title', text: '문제 위치' }), ...x.locations.map(renderLoc)) : null,
    x.fix ? sec('고치려면', x.fix, 'fix') : null,
    x.note ? el('p', { class: 'hint', text: '참고: ' + x.note }) : null,
    el('details', { class: 'tech' }, el('summary', { text: '기술 정보 (개발자용)' }), el('dl', {}, ...rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]))));
}

function selectCase(index, openCond) {
  state.selected = index;
  renderCaseList();
  const c = state.batch.cases[index];
  const box = $('case-detail'); box.replaceChildren();
  if (!c) return;
  box.append(el('h2', {}, c.case_id, c.conversation ? el('span', { class: 'chip', text: `대화 ${c.conversation.id} · ${c.conversation.turn}턴` }) : null));
  if (c.case_status !== 'EVALUATED') { box.append(el('p', { class: 'error', text: `${c.case_status}: ${c.input_error}` })); return; }
  const s = c.summary;
  const fails = c.results.filter((r) => r.counted && r.verdict === 'FAIL');
  box.append(el('p', { class: 'hint', text: `CODE 평가 — 문제 ${s.common.fail + s.specialized.fail}건 · 통과 ${s.common.pass + s.specialized.pass}건 · 판정 안 함 ${s.common.unjudged + s.specialized.unjudged}건 · ${s.check_level}` }));
  const rowById = {};
  if (fails.length) {
    box.append(el('div', { class: 'problems' }, el('div', { class: 'sec-title', text: `CODE 평가에서 발견된 문제 ${fails.length}건` }),
      el('ol', {}, ...fails.map((r) => el('li', {}, el('button', { type: 'button', class: 'link-btn', onclick: () => rowById[r.condition_id] && openRow(rowById[r.condition_id], r) },
        el('span', { class: 'chip', text: subName(r) }), ' ', el('strong', { text: r.explain.headline }), el('div', { class: 'fo-sum', text: r.explain.summary })))))));
  } else {
    box.append(el('p', { class: 'ok-msg', text: 'CODE 평가에서 문제가 확인된 조건이 없습니다.' }));
  }
  box.append(jevBlock(jevRecord(c)));
  box.append(el('h3', { text: '질문' }), highlighted(c.question, []));
  if (c.reference !== undefined && c.reference !== null) {
    box.append(el('h3', { text: '참조 데이터 (reference) — J5 기준' }),
      highlighted(typeof c.reference === 'string' ? c.reference : JSON.stringify(c.reference, null, 1), []));
  }
  box.append(el('h3', { text: '답변' }));
  let aBox = highlighted(c.answer, []);
  box.append(aBox);
  if (c.displayed_text !== null && c.displayed_text !== undefined) box.append(el('h3', { text: '표시본 (displayed_text)' }), highlighted(c.displayed_text, []));
  const conds = c.meta.question_conditions;
  box.append(el('details', {}, el('summary', { text: `질문에서 읽은 요구 ${conds.length}개` }),
    el('ul', { class: 'hint' }, ...conds.map((q) => el('li', { text: `${COND_KO[q.kind] || q.kind}: “${q.evidence.excerpt}”${q.status === 'ok' ? '' : ' — 판정 안 함: ' + q.reason}` })))));

  function openRow(btn, r) {
    box.querySelectorAll('.res-row').forEach((b) => b.setAttribute('aria-expanded', 'false'));
    box.querySelectorAll('.res-detail').forEach((d) => d.remove());
    btn.setAttribute('aria-expanded', 'true');
    btn.after(resultDetail(r));
    const ranges = r.evidence.filter((e) => e.target === 'answer').map((e) => [e.start, e.end]);
    const fresh = highlighted(c.answer, ranges); aBox.replaceWith(fresh); aBox = fresh;
    btn.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }

  const groups = {};
  c.results.forEach((r) => { (groups[r.subcategory] = groups[r.subcategory] || []).push(r); });
  const order = [...Object.keys(SUB_NAMES), ...Object.keys(SPECIAL_NAMES)];
  box.append(el('h3', { class: 'mt', text: '전체 검사 결과 (누르면 자세히)' }));
  order.forEach((key) => {
    const rows = groups[key];
    const name = SUB_NAMES[key] || SPECIAL_NAMES[key];
    const g = el('div', { class: 'group' }, el('div', { class: 'group-title', text: `${key.startsWith('AIONA') ? 'AIONA 특화 · ' : key + ' '}${name}` }));
    if (!rows) { g.append(el('p', { class: 'hint', text: '이 질문·답변에는 해당하는 조건이 없음' })); box.append(g); return; }
    rows.forEach((r) => { try {
      const btn = el('button', { type: 'button', class: 'res-row' + (r.verdict === 'FAIL' ? ' is-fail' : ''), 'aria-expanded': 'false' },
        el('span', {}, badge(r.verdict, r.status)),
        el('span', {}, el('strong', { class: 'hl', text: r.explain.headline }), el('span', { class: 'reason', text: r.verdict === 'PASS' ? r.explain.checked : r.explain.summary })),
        el('span', { class: 'st', text: STATUS_KO[r.status] || r.status }));
      rowById[r.condition_id] = btn;
      btn.addEventListener('click', () => {
        if (btn.getAttribute('aria-expanded') === 'true') {
          btn.setAttribute('aria-expanded', 'false'); box.querySelectorAll('.res-detail').forEach((d) => d.remove());
          const fresh = highlighted(c.answer, []); aBox.replaceWith(fresh); aBox = fresh; return;
        }
        openRow(btn, r);
      });
      g.append(btn);
    } catch (err) { g.append(el('p', { class: 'error', text: `이 결과를 표시하지 못했습니다 (${r.check_id}): ${err.message}` })); } });
    box.append(g);
  });
  c.observations.forEach((o) => {
    if (o.observation_id === 'foreign_script_spans' && o.span_count) {
      const obs = el('div', { class: 'obs' }, el('h3', { text: '참고: 외국 문자 위치 — 판정은 1-1 외국 문자 결과' }),
        el('p', { text: `${Object.entries(o.script_counts).filter(([, n]) => n).map(([k, n]) => `${({ Han: '한자', Hiragana: '히라가나', Katakana: '가타카나' }[k] || k)} ${n}자`).join(' · ')}가 ${o.span_count}곳에 있습니다. 질문 주제·주변 설명·괄호 병기 없이 나온 구간은 1-1 외국 문자 검사에서 FAIL입니다.` }));
      obs.append(el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, ...['문자', '종류', '주변 문장', '구조'].map((h) => el('th', { text: h })))),
        el('tbody', {}, ...o.script_spans.slice(0, 50).map((x) => el('tr', {}, el('td', { text: x.span_text }),
          el('td', { text: Object.keys(x.scripts).map((k) => ({ Han: '한자', Hiragana: '히라가나', Katakana: '가타카나' }[k] || k)).join('·') }),
          el('td', { text: x.surrounding_text }),
          el('td', { text: [x.structure && x.structure.in_quotes ? '따옴표 안' : '', x.structure && x.structure.in_parentheses ? '괄호 안' : '', x.structure && x.structure.in_blockquote ? '인용문' : '', x.same_text_in_question ? '질문에도 있음' : ''].filter(Boolean).join(', ') || '—' })))))));
      box.append(obs);
    } else if (o.observation_id === 'table_rows' && o.tables.length) {
      box.append(el('div', { class: 'obs' }, el('h3', { text: '참고: 표 행 수 (판정 아님)' }),
        el('p', { text: o.tables.map((t, i) => `표 ${i + 1}: 헤더 ${t.header_rows}행 · 데이터 ${t.data_rows}행 · 전체 ${t.total_rows}행`).join(' / ') })));
    }
  });
  if (openCond && rowById[openCond]) {
    const r = c.results.find((x) => x.condition_id === openCond);
    openRow(rowById[openCond], r);
  }
}

document.querySelectorAll('#case-filters .filter').forEach((b) => b.addEventListener('click', () => {
  state.filter = b.dataset.filter;
  document.querySelectorAll('#case-filters .filter').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
  const first = filteredCases()[0];
  if (first) selectCase(state.batch.cases.indexOf(first)); else renderCaseList();
}));
$('case-search').addEventListener('input', (e) => { state.search = e.target.value; renderCaseList(); });

// ---- 시작 ------------------------------------------------------------------------
function renderSemanticSetup(info) {
  const setup = info.semantic_setup;
  $('semantic-setup-title').textContent = setup ? `의미평가 · ${setup.engine_label || '평가자'}${setup.model ? ' · ' + setup.model : ''}${setup.reasoning_effort ? ' · 추론 ' + setup.reasoning_effort : ''}` : '의미평가 설정 정보 없음';
  $('semantic-setup-description').textContent = setup
    ? [setup.reason, setup.search_message, setup.connection_message,
       setup.can_evaluate === false ? '연결 설정 전에는 코드 검사 결과만 제공되며 의미평가는 미실행으로 남습니다.' : ''].filter(Boolean).join(' ')
    : '서버에서 설정 정보를 제공하지 않습니다. 새 평가 전에 서버 설정을 확인해 주세요.';
  $('run-single').textContent = setup?.can_evaluate === false ? '코드 검사만 실행' : '평가 실행';
  $('run-batch').textContent = setup?.can_evaluate === false ? '일괄 코드 검사만 실행' : '일괄 평가 실행';
}
$('semantic-setup-refresh').addEventListener('click', async () => {
  $('semantic-setup-refresh').disabled = true;
  try { state.info = await api('/api/code-eval/info'); renderSemanticSetup(state.info); setError(''); }
  catch (_) { $('semantic-setup-description').textContent = '설정을 불러오지 못했습니다. 서버 연결을 확인하고 다시 시도해 주세요.'; }
  finally { $('semantic-setup-refresh').disabled = false; }
});
api('/api/code-eval/info').then(async (info) => {
  state.info = info; renderCriteria(info); renderSemanticSetup(info);
  EXPECTED_ENGINE = info.versions.engine;
  const params = new URLSearchParams(location.search);
  const comparison = params.get('comparison'), provider = params.get('provider');
  if (comparison && provider) {
    const base = `/api/comparison/runs/${encodeURIComponent(comparison)}/evaluation/${encodeURIComponent(provider)}`;
    state.batch = await api(base); state.exportBase = base; state.selected = null;
    renderResults(); showTab('results');
  }
})
  .catch((err) => setError('서버 정보를 불러오지 못했습니다: ' + err.message + ' (py -3 run.py 로 실행했는지 확인)'));
const initial = (location.hash || '#run').slice(1);
showTab(['run', 'results', 'overview', 'methods', 'policy', 'sources'].includes(initial) ? initial : 'run');
})();
