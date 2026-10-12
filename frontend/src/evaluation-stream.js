/* Actual events own the counters; a separate, disposable queue owns only motion. */
((root) => {
  'use strict';
  const rowKey = (e) => `${e.model}:${e.index}`;
  const empty = () => ({total: 0, done: 0, pass: 0, fail: 0, unjudged: 0, skipped: 0, status: 'WAITING', rows: {}});
  class EventState {
    constructor() { this.data = empty(); this.lastId = -1; }
    apply(e) {
      if (e.type === 'snapshot') { this.data = {...empty(), ...e}; this.lastId = e.id ?? -1; return true; }
      if (e.id !== undefined && e.id <= this.lastId) return false;
      this.lastId = e.id ?? this.lastId;
      if (e.type === 'run_start') this.data = {...empty(), total: e.total, generation: e.generation, status: 'RUNNING'};
      const d = this.data;
      if (e.model !== undefined && e.index !== undefined) {
        const r = d.rows[rowKey(e)] ||= {model: e.model, index: e.index, qid: e.qid, criteria: {}, status: 'RUNNING', pass: 0, fail: 0, unjudged: 0};
        r.qid = e.qid;
        if (e.type === 'criterion_start') r.active = {code: e.code, name: e.name, stage: e.stage, together: e.together};
        if (e.type === 'criterion_end' && r.active?.code === e.code) r.active = null;
        if (e.type === 'criterion_result') {
          const old = r.criteria[e.key];
          if (old) { const field = old.result.toLowerCase(); r[field]--; d[field]--; }
          r.criteria[e.key] = e;
          const field = e.result.toLowerCase(); r[field]++; d[field]++;
        }
        if (['q_done', 'q_skip'].includes(e.type)) {
          if (r.status === 'RUNNING') { d.done++; if (e.type === 'q_skip') d.skipped++; }
          r.status = e.type === 'q_skip' ? 'SKIPPED' : 'DONE'; r.reason = e.reason || e.error; r.finished_id = e.id; r.active = null;
          for (const key of ['rechecks', 'unverifiable']) if (e[key] !== undefined) r[key] = e[key];
        }
      }
      if (['run_done', 'run_error'].includes(e.type)) {
        for (const key of ['total', 'done', 'pass', 'fail', 'unjudged', 'skipped']) if (e[key] !== undefined) d[key] = e[key];
        d.status = e.type === 'run_done' ? 'COMPLETED' : 'ERROR';
        if (e.type === 'run_error') for (const row of Object.values(d.rows)) {
          if (row.status === 'RUNNING') Object.assign(row, {status: 'INTERRUPTED', active: null, reason: e.reason});
        }
      }
      return true;
    }
  }
  class DisplayQueue {
    constructor() { this.items = []; this.fast = false; }
    add(e) {
      if (['run_done', 'run_error', 'snapshot', 'run_start'].includes(e.type)) { this.items = []; this.fast = false; return; }
      if (!['criterion_start', 'q_done', 'q_skip'].includes(e.type)) return;
      this.items.push(e);
      if (this.items.length > 50) { this.items = []; this.fast = true; }
      else if (this.items.length > 10) this.items = this.items.filter((item) => ['q_done', 'q_skip'].includes(item.type));
    }
    take() { return this.items.shift(); }
  }

  function element(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  let instance = 0;
  class EvalProgress {
    constructor(host) {
      this.host = host; this.state = new EventState(); this.queue = new DisplayQueue(); this.modelLabels = {};
      this.timer = null; this.frame = null; this.lastShown = 0; this.nodes = new Map(); this.dirty = new Set(); this.autoScroll = true;
      this.reduced = root.matchMedia?.('(prefers-reduced-motion: reduce)');
      host.classList.add('eval-progress'); host.setAttribute('role', 'group'); host.setAttribute('aria-label', '평가 진행');
      this.card = element('button', 'eval-ticker'); this.card.type = 'button';
      this.card.setAttribute('aria-expanded', 'false');
      const icon = element('span', 'eval-clipboard'); icon.setAttribute('aria-hidden', 'true');
      icon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M9 5H6a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-3"/><rect x="9" y="2" width="6" height="5" rx="1"/><path d="m8 14 3 3 5-6"/></svg>';
      this.viewport = element('span', 'eval-rolling'); this.viewport.setAttribute('role', 'region');
      this.viewport.setAttribute('aria-roledescription', '순환 목록'); this.viewport.setAttribute('aria-label', '문항 평가 진행');
      this.counter = element('span', 'eval-counter', '0/0');
      this.card.append(icon, element('span', 'eval-label', '평가'), this.viewport, this.counter);
      this.track = element('progress', 'eval-track'); this.track.max = 1; this.track.value = 0;
      this.track.setAttribute('aria-label', '모델별 문항 처리 진행률');
      this.connection = element('span', 'eval-connection'); this.connection.hidden = true;
      this.log = element('details', 'eval-log'); this.log.id = 'eval-log-' + (++instance);
      this.card.setAttribute('aria-controls', this.log.id);
      this.summary = element('summary'); this.summary.append(element('span', 'eval-chevron', '›'), element('strong', '', '평가 기록'));
      this.latest = element('span', 'eval-log-latest', '첫 문항 결과를 기다리고 있습니다.'); this.summary.append(this.latest);
      this.list = element('div', 'eval-log-list'); this.list.setAttribute('role', 'list'); this.list.tabIndex = 0;
      this.list.setAttribute('aria-label', '모델별 문항 평가 기록');
      this.log.append(this.summary, this.list); host.append(this.card, this.track, this.connection, this.log);
      this.card.addEventListener('click', () => { this.log.open = !this.log.open; });
      this.log.addEventListener('toggle', () => { this.card.setAttribute('aria-expanded', String(this.log.open)); if (this.log.open) this.renderLog(); });
      this.list.addEventListener('scroll', () => { this.autoScroll = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < 30; }, {passive: true});
      this.showText('평가 시작을 기다리고 있습니다.', false);
    }
    modelLabel(model) { return this.modelLabels[model] || model; }
    criterionText(event) {
      return `${this.modelLabel(event.model)} · ${event.qid} · ` + (event.together ?
        '의미 평가 응답 대기 · 여러 기준 함께 평가' : `${event.code} ${event.name} 기준 평가 중…`);
    }
    setModelLabels(labels) {
      if (JSON.stringify(labels) === JSON.stringify(this.modelLabels)) return;
      this.modelLabels = labels;
      this.dirty = new Set(Object.keys(this.state.data.rows));
      this.nodes.forEach((item) => { item.signature = ''; });
      const rows = Object.values(this.state.data.rows).filter((row) => row.status !== 'RUNNING').sort((a, b) => (a.finished_id || Infinity) - (b.finished_id || Infinity));
      if (rows.length) this.latest.textContent = this.rowText(rows.at(-1));
      this.renderLog();
    }
    connect(url, identity) {
      if (this.identity === identity) {
        this.host.hidden = this.state.data.total === 0;
        if (this.source || ['COMPLETED', 'ERROR'].includes(this.state.data.status)) return;
      }
      this.disconnect(); this.identity = identity; this.reset();
      this.source = new EventSource(url);
      this.source.onmessage = (message) => {
        this.connection.hidden = true;
        try { this.receive(JSON.parse(message.data)); } catch { this.connection.textContent = '진행 정보를 확인하고 있습니다.'; this.connection.hidden = false; }
      };
      this.source.onerror = () => {
        if (!['COMPLETED', 'ERROR'].includes(this.state.data.status)) {
          this.connection.textContent = '진행 연결 복구 중 · 서버 상태를 확인할 수 없습니다.'; this.connection.hidden = false;
        }
      };
    }
    disconnect() { this.source?.close(); this.source = null; }
    reset() {
      clearTimeout(this.timer); this.timer = null; this.queue = new DisplayQueue(); this.state = new EventState();
      this.nodes.clear(); this.dirty.clear(); this.list.replaceChildren(); this.autoScroll = true; this.lastShown = 0;
      this.latest.textContent = '첫 문항 결과를 기다리고 있습니다.';
    }
    receive(e) {
      if (!this.state.apply(e)) return;
      if (e.type === 'run_start' || e.type === 'snapshot') {
        this.nodes.clear(); this.list.replaceChildren(); this.autoScroll = true;
        this.dirty = new Set(Object.keys(this.state.data.rows));
        clearTimeout(this.timer); this.timer = null;
      }
      if (e.model !== undefined && e.index !== undefined) this.dirty.add(rowKey(e));
      this.queue.add(e);
      const d = this.state.data;
      this.host.hidden = d.total === 0;
      this.host.dataset.terminal = String(['COMPLETED', 'ERROR'].includes(d.status));
      // Updated synchronously, never from the rolling queue.
      this.counter.textContent = d.status === 'COMPLETED' ? '완료' : `${d.done}/${d.total}`;
      this.counter.title = '모델별 문항 수를 합산한 실제 처리 수';
      this.track.max = Math.max(1, d.total); this.track.value = d.done;
      if (['COMPLETED', 'ERROR'].includes(d.status)) {
        clearTimeout(this.timer); this.timer = null; this.queue.items = [];
        this.showText(`${d.total}개 답변 ${d.status === 'COMPLETED' ? '평가 종료' : '평가 중단'} · PASS ${d.pass} · FAIL ${d.fail} · 미판정 ${d.unjudged}${d.skipped ? ' · 제외 ' + d.skipped : ''}`, false);
        this.disconnect();
      } else if (this.queue.fast) {
        clearTimeout(this.timer); this.timer = null; this.queue.items = [];
        this.showText(`최신 결과 반영 중 · ${d.done}/${d.total}개 답변 처리`, false);
      } else if (e.type === 'snapshot') {
        const active = Object.values(d.rows).find((r) => r.status === 'RUNNING' && r.active);
        if (active) {
          const live = {...active, ...active.active, type: 'criterion_start', together: active.active.together ?? active.active.stage !== 'CODE'};
          this.showText(this.criterionText(live), false, live);
        } else this.showText(`평가 진행 중 · ${d.done}/${d.total}개 답변 처리`, false);
      }
      else if (!this.timer) this.drain();
      if (['q_done', 'q_skip'].includes(e.type)) this.latest.textContent = this.rowText(d.rows[rowKey(e)]);
      if (!this.frame && ['q_done', 'q_skip', 'run_start', 'snapshot', 'run_error'].includes(e.type))
        this.frame = requestAnimationFrame(() => { this.frame = null; this.renderLog(); });
    }
    drain() {
      if (!this.queue.items.length) return;
      const wait = Math.max(0, 350 - (performance.now() - this.lastShown));
      this.timer = setTimeout(() => {
        this.timer = null; const e = this.queue.take(); if (!e) return;
        let text = `${this.modelLabel(e.model)} · ${e.qid}`;
        if (e.type === 'criterion_start') text = this.criterionText(e);
        else if (e.type === 'q_skip') text += ' 평가 제외 · ' + (e.reason || '해당 기준 없음');
        else text += ` ${e.unjudged ? '처리 완료' : '평가 완료'} · PASS ${e.pass} · FAIL ${e.fail}${e.unjudged ? ' · 미판정 ' + e.unjudged : ''}`;
        this.showText(text, true, e); this.lastShown = performance.now(); this.drain();
      }, wait);
    }
    showText(text, animate, event) {
      const old = this.viewport.lastElementChild;
      const line = element('span', 'eval-line', text); line.title = text;
      if (event?.type === 'criterion_start') {
        line.replaceChildren(element('strong', '', `${this.modelLabel(event.model)} · ${event.qid}`),
          ...(event.together ? [document.createTextNode(' · 의미 평가 '), element('span', 'eval-shimmer', '응답 대기'),
            document.createTextNode(' · 여러 기준 함께 평가')] :
            [document.createTextNode(` · ${event.code} ${event.name} 기준 `), element('span', 'eval-shimmer', '평가 중…')]));
      }
      if (event?.type === 'q_done') {
        line.replaceChildren(element('span', 'eval-check', '✓ '), element('strong', '', `${this.modelLabel(event.model)} · ${event.qid}`),
          document.createTextNode(` ${event.unjudged ? '처리 완료' : '평가 완료'} · PASS ${event.pass} · `),
          element('span', event.fail ? 'eval-fail' : 'eval-pass', `FAIL ${event.fail}`),
          document.createTextNode((event.unjudged ? ' · 미판정 ' + event.unjudged : '') + this.itemsText(event)));
      }
      if (event?.type === 'q_skip') line.classList.add('is-skipped');
      if (animate && old && !this.reduced?.matches) {
        this.viewport.replaceChildren(old, line); old.classList.add('is-exiting'); line.classList.add('is-entering');
        setTimeout(() => old.remove(), 320);
      } else this.viewport.replaceChildren(line);
    }
    // items 파이프라인만 보내는 값: 재확인 회차(인용 검증 실패 항목을 다시 물은 횟수)와 확인 불가(3회 뒤에도 확인하지 못해 FAIL 한 항목 수)
    itemsText(value) {
      return `${value?.rechecks ? ` · 재확인 ${value.rechecks}회` : ''}${value?.unverifiable ? ` · 확인 불가 ${value.unverifiable}` : ''}`;
    }
    rowText(row) {
      return `${this.modelLabel(row.model)} · ${row.qid}  ${row.status === 'SKIPPED' ? '평가 제외 · ' + row.reason : `${row.status === 'INTERRUPTED' ? '평가 중단 · ' : ''}PASS ${row.pass} · FAIL ${row.fail} · 미판정 ${row.unjudged}${this.itemsText(row)}`}`;
    }
    renderLog() {
      const rows = Object.entries(this.state.data.rows).filter(([, r]) => r.status !== 'RUNNING').sort((a, b) => (a[1].finished_id || Infinity) - (b[1].finished_id || Infinity));
      if (rows.length && !this.latest.textContent.includes(' · ')) this.latest.textContent = this.rowText(rows.at(-1)[1]);
      if (!this.log.open) return;
      const follow = this.autoScroll;
      for (const [key, row] of rows) {
        let item = this.nodes.get(key);
        if (item && !this.dirty.has(key)) continue;
        const signature = JSON.stringify(row);
        if (item?.signature === signature) continue;
        if (!item) {
          const details = element('details', 'eval-log-row'); details.setAttribute('role', 'listitem');
          const summary = element('summary'); const children = element('div', 'eval-criteria-log');
          details.append(summary, children);
          item = {details, summary, children, signature: ''}; this.nodes.set(key, item); this.list.append(details);
          details.addEventListener('toggle', () => { if (details.open) this.renderCriteria(item, row); });
        }
        item.signature = signature;
        item.summary.textContent = this.rowText(row);
        item.details.dataset.outcome = row.fail ? 'fail' : row.pass && !row.unjudged ? 'pass' : 'unknown';
        if (item.details.open) this.renderCriteria(item, row);
      }
      this.dirty.clear();
      if (follow) this.list.scrollTop = this.list.scrollHeight;
    }
    renderCriteria(item, row) {
      item.children.replaceChildren(...Object.values(row.criteria).map((c) => {
        const line = element('div', 'eval-criterion');
        line.append(element('strong', '', c.code), element('span', 'eval-verdict ' + c.result.toLowerCase(), c.result),
          element('span', '', `${c.name} · ${c.stage} · ${c.reason}${c.unverifiable ? ` · 확인 불가 ${c.unverifiable}` : ''}`));
        return line;
      }));
      if (row.reason) item.children.append(element('p', '', row.reason));
    }
  }
  const api = {EventState, DisplayQueue, EvalProgress};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.AionaEvalStream = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
