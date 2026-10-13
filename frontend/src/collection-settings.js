/* 관리 설정 — 모델 연결(API 키·모델 ID)과 업로드 직후 자동 수집의 허용·단가·호출·비용 한도.
   키 값은 서버 세션에만 보내고 화면·브라우저 저장소·결과에 남기지 않는다. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  if (!window.AionaWorkspace || !$('connection-cards')) return;
  const W = window.AionaWorkspace, {node, pill, number, api} = W;
  const LIVE = ['gpt', 'claude', 'gemini', 'factchat'];
  const stateKinds = {ready: 'success', needs_connection: 'neutral', disabled: 'neutral', limits_missing: 'warning', unpriced: 'warning', unsupported: 'neutral'};
  const state = {status: null, modalProvider: null, loading: false};
  const usd = (value) => '$' + Number(value || 0).toLocaleString('en-US', {maximumFractionDigits: 4});

  function publish() {
    window.dispatchEvent(new CustomEvent('aiona:collection-status', {detail: state.status}));
  }
  async function load() {
    if (state.loading) return;
    state.loading = true;
    try { state.status = await api('/api/comparison/auto-collection'); render(); publish(); }
    catch (err) { $('policy-error').textContent = err.message; $('policy-error').hidden = false; }
    finally { state.loading = false; }
  }
  const target = (id) => state.status?.targets.find((t) => t.id === id);

  function renderCards() {
    $('connection-cards').replaceChildren(...(state.status?.targets || []).map((t) => {
      const unsupported = t.kind === 'unsupported';
      const top = node('div', {class: 'connection-top'}, W.avatar(t.id), node('div', {}, node('h2', {text: t.name}),
        node('p', {text: t.kind === 'gateway' ? t.platform + ' · 여러 제공자 모델 중계' : t.platform})),
        pill(t.label, stateKinds[t.state] || 'neutral'));
      const facts = node('dl', {class: 'connection-model'},
        node('dt', {text: unsupported ? '연동 방식' : '모델 ID'}),
        node('dd', {text: unsupported ? '공개 API·공식 내보내기 미확인' : t.model || '연결 후 입력'}));
      if (t.kind === 'gateway') facts.append(node('dt', {text: '게이트웨이 주소'}), node('dd', {text: t.base_url || '연결 시 선택'}));
      const card = node('article', {class: 'panel connection-card' + (unsupported ? ' is-unsupported' : ''), 'data-target': t.id}, top, facts);
      if (t.note) card.append(node('p', {class: 'connection-note', text: t.note}));
      else if (t.reason && t.state !== 'ready') card.append(node('p', {class: 'connection-note', text: t.reason}));
      const actions = node('div', {class: 'connection-actions'});
      if (!unsupported) {
        if (t.connected) actions.append(node('button', {type: 'button', class: 'button secondary', onclick: () => disconnect(t)}, '등록 해제'));
        actions.append(node('button', {type: 'button', class: 'button primary', onclick: () => openKey(t.id)}, t.connected ? '연결 설정' : 'API 키 등록', W.icon('link')));
      }
      card.append(actions);
      return card;
    }));
  }
  async function disconnect(t) {
    try {
      await api('/api/comparison/providers/' + t.id, {clear: true});
      await load(); W.announce(t.name + ' 키 등록을 해제했습니다.');
    } catch (err) { W.showError(err.message); }
  }

  function priceInput(id, side, value) {
    return node('input', {type: 'number', min: '0', step: '0.01', inputmode: 'decimal', id: `price-${id}-${side}`,
      'aria-label': `${target(id).name} ${side === 'input' ? '입력' : '출력'} 단가 (USD / 100만 토큰)`, value: value ?? '', placeholder: '예: 3'});
  }
  function renderPolicy() {
    const status = state.status, policy = status?.policy;
    if (!policy) return;
    $('policy-providers').replaceChildren(...LIVE.map((id) => {
      const t = target(id), price = policy.prices[id] || {};
      const toggle = node('input', {type: 'checkbox', id: 'allow-' + id, 'aria-label': t.name + ' 업로드 시 자동 수집 허용'});
      toggle.checked = Boolean(policy.allowed[id]);
      return node('tr', {}, node('th', {scope: 'row'}, W.avatar(id), node('span', {}, node('strong', {text: t.name}), node('small', {text: t.platform}))),
        node('td', {}, pill(t.connected ? '연결됨 · ' + t.model : '연결 필요', t.connected ? 'success' : 'neutral')),
        node('td', {}, node('label', {class: 'policy-toggle'}, toggle, node('span', {text: '허용'}))),
        node('td', {}, priceInput(id, 'input', price.input)), node('td', {}, priceInput(id, 'output', price.output)));
    }));
    const unsupported = (status.targets || []).filter((t) => t.kind === 'unsupported');
    unsupported.forEach((t) => $('policy-providers').append(node('tr', {class: 'policy-unsupported'},
      node('th', {scope: 'row'}, W.avatar(t.id), node('span', {}, node('strong', {text: t.name}), node('small', {text: t.platform}))),
      node('td', {colspan: '4'}, pill('연동 준비 중', 'neutral'), node('span', {class: 'field-help', text: ' ' + t.note})))));
    const limits = policy.limits;
    [['limit-job-calls', 'job_calls'], ['limit-job-cost', 'job_cost_usd'], ['limit-daily-calls', 'daily_calls'], ['limit-daily-cost', 'daily_cost_usd']]
      .forEach(([id, key]) => { if (document.activeElement !== $(id)) $(id).value = limits[key] ?? ''; });
    $('policy-max-tokens').value = String(policy.max_output_tokens);
    const today = status.today;
    $('policy-today').textContent = `오늘(${today.day}) 사용·예약 ${number(today.calls)}회 · ${usd(today.cost_usd)}` +
      (limits.daily_calls ? ` / 일일 한도 ${number(limits.daily_calls)}회 · ${usd(limits.daily_cost_usd)}` : '');
    $('policy-today').className = 'pill ' + (status.limits_ready ? 'neutral' : 'warning');
    $('policy-saved').textContent = policy.updated_at ? '마지막 저장 ' + new Date(policy.updated_at).toLocaleString('ko-KR') : '저장된 관리 설정이 없습니다. 기본값은 모든 제공자의 자동 수집이 꺼진 상태입니다.';
  }
  function render() { renderCards(); renderPolicy(); }

  function numberValue(id) {
    const raw = $(id).value.trim();
    return raw === '' ? null : Number(raw);
  }
  $('policy-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('policy-error').hidden = true; $('policy-save').disabled = true;
    const payload = {allowed: {}, prices: {}, max_output_tokens: Number($('policy-max-tokens').value),
      limits: {job_calls: numberValue('limit-job-calls'), job_cost_usd: numberValue('limit-job-cost'),
        daily_calls: numberValue('limit-daily-calls'), daily_cost_usd: numberValue('limit-daily-cost')}};
    for (const id of LIVE) {
      payload.allowed[id] = $('allow-' + id).checked;
      payload.prices[id] = {input: numberValue(`price-${id}-input`), output: numberValue(`price-${id}-output`)};
    }
    try {
      state.status = await api('/api/comparison/auto-collection/policy', payload);
      render(); publish(); W.announce('자동 수집 관리 설정을 저장했습니다.');
    } catch (err) { $('policy-error').textContent = err.message; $('policy-error').hidden = false; }
    finally { $('policy-save').disabled = false; }
  });

  function openKey(id) {
    const t = target(id);
    state.modalProvider = id; $('key-title').textContent = t.name + ' 연결';
    $('key-description').textContent = t.kind === 'gateway'
      ? 'FactChat은 여러 제공자의 모델을 중계하는 OpenAI 호환 API 게이트웨이입니다. 실제 답변 모델은 아래 모델 ID로 기록합니다.'
      : t.name + ' 답변 수집에 사용할 연결 정보입니다.';
    $('api-key').value = ''; $('api-key').type = 'password'; $('toggle-key').textContent = '표시'; $('toggle-key').setAttribute('aria-pressed', 'false');
    $('model-id').value = t.model || '';
    $('base-url-field').hidden = t.kind !== 'gateway';
    $('base-url').replaceChildren(...(state.status.factchat_base_urls || []).map((url) => node('option', {value: url, text: url.replace(/^https:\/\//, '')})));
    if (t.base_url) $('base-url').value = t.base_url;
    $('key-error').hidden = true; $('key-dialog').showModal();
  }
  $('toggle-key').addEventListener('click', () => {
    const visible = $('api-key').type === 'password'; $('api-key').type = visible ? 'text' : 'password';
    $('toggle-key').textContent = visible ? '숨기기' : '표시'; $('toggle-key').setAttribute('aria-pressed', String(visible));
  });
  function clearKeyField() {
    $('api-key').value = ''; $('api-key').type = 'password';
    $('toggle-key').textContent = '표시'; $('toggle-key').setAttribute('aria-pressed', 'false');
  }
  $('key-dialog').addEventListener('cancel', clearKeyField);
  $('key-dialog').addEventListener('close', clearKeyField);
  $('key-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const id = state.modalProvider, t = target(id);
    $('save-key').disabled = true; $('key-error').hidden = true;
    try {
      await api('/api/comparison/providers/' + id, {key: $('api-key').value, model: $('model-id').value.trim(),
        ...(t.kind === 'gateway' ? {base_url: $('base-url').value} : {})});
      $('api-key').value = ''; await load();
      $('key-dialog').close(); W.announce(t.name + ' API 키가 등록되었습니다.');
    } catch (err) { $('key-error').textContent = err.message; $('key-error').hidden = false; }
    finally { $('save-key').disabled = false; }
  });

  window.addEventListener('aiona:semantic-setup', load);
  window.addEventListener('aiona:connections-open', load);
  window.AionaCollectionSettings = {load, openKey, status: () => state.status};
})();
