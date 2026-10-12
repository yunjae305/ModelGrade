/* Pure AIONA run comparison. Saved inputs, answers and judgments are never rewritten. */
(function (root, factory) {
  'use strict';
  if (typeof module !== 'undefined' && module.exports) module.exports = factory(require('./workspace-results.js'));
  else root.AionaVersionComparison = factory(root.AionaResults);
})(typeof globalThis !== 'undefined' ? globalThis : this, function (results) {
  'use strict';

  const labels = {
    improved: '개선 · FAIL → PASS', regressed: '악화 · PASS → FAIL',
    passMaintained: 'PASS 유지', failMaintained: 'FAIL 유지', pending: '평가 미완료',
    unverified: '조건이 달라 변화 확정 불가'
  };
  const object = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  const text = (v) => typeof v === 'string' && v.trim().length > 0;
  const knownText = (v) => text(v) && !/^(unknown|not[-_ ]?recorded|미상|미기록)$/i.test(v.trim());
  const own = (v, k) => Object.prototype.hasOwnProperty.call(v, k);

  // Object key order is immaterial; array order and primitive types are preserved.
  function canonical(value) {
    if (value === null) return 'null';
    if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
    if (object(value)) return '{' + Object.keys(value).sort().map((k) => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
    return JSON.stringify(value);
  }

  function knownProfile(profile) {
    const f = profile?.fields;
    // Config fields such as forbidden_entries legitimately contain null. Unknown
    // required versions/model identities, however, must never compare as equal.
    const knownTree = (v) => typeof v === 'string' ? knownText(v) : typeof v === 'number' ? Number.isFinite(v) :
      typeof v === 'boolean' || (v !== null && typeof v === 'object' && Object.values(v).every(knownTree));
    // These two top-level judge options explicitly use null for unlimited.
    // Null anywhere else still means missing evidence of comparable settings.
    const knownJudge = object(f?.judge) && Object.entries(f.judge).every(([key, value]) =>
      (value === null && ['max_tool_calls', 'evidence_recovery_max_tool_calls'].includes(key)) || knownTree(value));
    return profile?.complete === true && object(f) && knownText(f.engine_versions?.engine) && knownTree(f.engine_versions) &&
      knownText(f.aiona_config?.config_hash) &&
      ['semantic_version', 'criteria_version', 'applicability_version', 'history_policy'].every((k) => knownText(f[k])) &&
      knownText(f.judge?.engine) && knownText(f.judge?.requested_model) && knownJudge &&
      Array.isArray(f.judge_models) && f.judge_models.length > 0 && f.judge_models.every(knownText);
  }

  function stableId(row) {
    if (!object(row)) return null;
    for (const key of ['case_id', 'id', 'question_id']) {
      const value = row[key];
      if (text(value)) return value;
      if (Number.isSafeInteger(value)) return String(value);
    }
    return null;
  }

  function conversation(row) {
    const cid = row?.conversation_id, turn = row?.turn;
    if (cid == null && turn == null) return {id: null, turn: null};
    const validId = text(cid) || Number.isSafeInteger(cid);
    const number = typeof turn === 'string' && /^\d+$/.test(turn.trim()) ? Number(turn) : turn;
    if (!validId || !Number.isSafeInteger(number) || number < 1) return null;
    return {id: String(cid).trim(), turn: number};
  }

  function identify(run) {
    const source = Array.isArray(run?.datasets?.aiona) ? run.datasets.aiona : [];
    const byId = new Map(), groups = new Map();
    const rows = source.map((row, index) => {
      const id = stableId(row), context = conversation(row);
      const info = {index, caseId: id, question: row?.question, reference: canonical(row?.reference ?? null),
        context, history: '', expectedContext: null, invalid: null};
      if (!id) info.invalid = {code: 'missing_id', reason: '안정적인 문항 ID가 없습니다.'};
      else if (!text(row?.question)) info.invalid = {code: 'invalid_question', reason: '질문 원문을 확인할 수 없습니다.'};
      else if (!context) info.invalid = {code: 'invalid_conversation', reason: '대화 ID와 턴 번호가 올바르지 않습니다.'};
      if (id) { if (!byId.has(id)) byId.set(id, []); byId.get(id).push(info); }
      if (context?.id !== null && context) {
        if (!groups.has(context.id)) groups.set(context.id, []);
        groups.get(context.id).push(info);
      }
      return info;
    });
    for (const entries of byId.values()) {
      if (entries.length > 1) entries.forEach((r) => { r.invalid = {code: 'duplicate_id', reason: '문항 ID가 중복되어 안전하게 연결할 수 없습니다.'}; });
    }
    for (const entries of groups.values()) {
      const ordered = [...entries].sort((a, b) => a.context.turn - b.context.turn);
      const valid = ordered.every((r, i) => r.context.turn === i + 1 && !r.invalid);
      const prior = [];
      let priorChars = 0;
      for (const row of ordered) {
        if (!valid && !row.invalid) row.invalid = {code: 'ambiguous_history', reason: '앞 대화의 턴이나 문항 ID가 누락·중복되어 연결할 수 없습니다.'};
        row.history = canonical(prior);
        row.expectedContext = {conversation_id: row.context.id, turn: row.context.turn,
          total_turns: ordered[ordered.length - 1].context.turn, history_turns: prior.length, history_chars: priorChars,
          history_complete: true, history_truncated: false, issues: []};
        // Previous answers are deliberately excluded: changing them is the
        // treatment being measured, whereas changing prompts changes the test.
        prior.push({id: row.caseId, question: row.question, reference: row.reference, turn: row.context.turn});
        const answer = source[row.index]?.answer;
        // Python stores Unicode code-point counts, not JavaScript UTF-16 length.
        priorChars += typeof row.question === 'string' && typeof answer === 'string' ?
          Array.from(row.question).length + Array.from(answer).length : NaN;
      }
    }
    return {rows, byId, source};
  }

  function differences(a, b) {
    const fields = [];
    if (a.question !== b.question) fields.push('question');
    if (a.reference !== b.reference) fields.push('reference');
    if (canonical(a.context) !== canonical(b.context)) fields.push('conversation');
    if (a.history !== b.history) fields.push('history');
    return fields;
  }

  function compactState(run, index) {
    const state = results.caseState(run, 'aiona', index);
    return {outcome: state.outcome, text: state.text, detail: state.detail, complete: state.complete};
  }

  function aligned(run, entry) {
    const c = run?.evaluations?.aiona?.cases?.[entry.index], source = run?.datasets?.aiona?.[entry.index];
    const record = run?.evaluations?.aiona?.jev_eval?.records?.[entry.index];
    if (!c || !source || c.case_status !== 'EVALUATED' || !record) return false;
    // EVALUATED records contain their original inputs. Missing originals cannot
    // prove alignment, and a semantic record's ID must agree before using its verdict.
    const ref = (v) => v == null || v === '' || Array.isArray(v) && !v.length || object(v) && !Object.keys(v).length ? null : v;
    const storedContext = c.conversation ? {id: c.conversation.id, turn: c.conversation.turn} : null;
    const expectedCodeContext = entry.context?.id === null ? null : entry.context;
    return own(c, 'case_id') && c.case_id === entry.caseId && record.case_id === entry.caseId &&
      typeof c.question === 'string' && typeof c.answer === 'string' &&
      c.question === source.question && c.answer === source.answer &&
      canonical(ref(c.reference)) === canonical(ref(source.reference)) &&
      canonical(storedContext) === canonical(expectedCodeContext) &&
      own(record, 'context') && canonical(record.context) === canonical(entry.expectedContext);
  }

  function summary(run, identified) {
    const states = identified.rows.map((r) => compactState(run, r.index));
    const total = states.length, batch = run?.evaluations?.aiona;
    const integrity = total > 0 && run?.question_count === total && batch?.cases?.length === total &&
      identified.rows.every((r) => aligned(run, r));
    const complete = states.filter((s) => s.complete).length;
    const passed = states.filter((s) => s.complete && s.outcome === 'clear').length;
    const failed = states.filter((s) => s.complete && s.outcome === 'issue').length;
    const ready = integrity && complete === total && !batch?.live;
    return {runId: text(run?.run_id) ? run.run_id : null, version: text(run?.aiona_release?.version) ? run.aiona_release.version : '버전 미기록',
      total, pass: passed, fail: failed, pending: total - complete, complete, integrity, ready,
      rate: ready ? passed / total : null};
  }

  function observedChange(a, b) {
    if (!a.complete || !b.complete) return 'pending';
    if (a.outcome === 'issue' && b.outcome === 'clear') return 'improved';
    if (a.outcome === 'clear' && b.outcome === 'issue') return 'regressed';
    return a.outcome === 'clear' ? 'passMaintained' : 'failMaintained';
  }

  function subset(run, indices) {
    const batch = run?.evaluations?.aiona;
    return {question_count: indices.length, evaluations: {aiona: batch ? {...batch,
      cases: indices.map((i) => batch.cases?.[i] || {case_status: 'INPUT_ERROR', results: [], summary: null}),
      jev_eval: {...batch.jev_eval, records: indices.map((i) => batch.jev_eval?.records?.[i])}} : undefined}};
  }

  function criterionTargets(run, index, criterion) {
    const batch = run?.evaluations?.aiona, record = batch?.jev_eval?.records?.[index];
    if (criterion.group !== 'code') return (record?.criteria || []).includes(criterion.id);
    const c = results.resolvedCase(batch?.cases?.[index], record);
    return (c?.results || []).filter((r) => r.area === 'common' && String(r.category) === criterion.id && r.counted)
      .map((r) => canonical({id: r.condition_id ?? r.check_id ?? null, required: r.required ?? null, scope: r.scope ?? null})).sort();
  }

  function criteriaComparison(baselineRun, targetRun, rows, conditionsMatch) {
    const base = subset(baselineRun, rows.map((r) => r.baselineIndex));
    const target = subset(targetRun, rows.map((r) => r.targetIndex));
    const metric = (run, criterion) => {
      const s = results.criterionSummary(run, 'aiona', criterion);
      return {targets: s.targets, pass: s.passed, fail: s.failed, pending: s.pending,
        unknown: s.unknown, excluded: s.excluded, rate: s.rate};
    };
    return results.comparisonCriteria.map((criterion) => {
      const a = metric(base, criterion), b = metric(target, criterion);
      const sameTargets = a.targets === b.targets && rows.every((r) =>
        canonical(criterionTargets(baselineRun, r.baselineIndex, criterion)) ===
        canonical(criterionTargets(targetRun, r.targetIndex, criterion)));
      const comparable = rows.length > 0 && conditionsMatch && rows.every((r) => r.verified) &&
        sameTargets && a.rate !== null && b.rate !== null;
      return {...criterion, baseline: a, target: b, matchedQuestions: rows.length, comparable,
        delta: comparable ? b.rate - a.rate : null,
        reason: !rows.length ? '동일하게 연결된 문항이 없습니다.' : !conditionsMatch ? '평가 조건이 같다고 확인되지 않았습니다.' :
          !rows.every((r) => r.verified) ? '공통 문항의 평가가 완료되지 않았습니다.' : !sameTargets ? '적용 대상이 달라 통과율 차이를 확정하지 않습니다.' :
            a.pending || a.unknown || b.pending || b.unknown ? '미판정 항목이 남아 통과율을 확정하지 않습니다.' :
              a.rate === null || b.rate === null ? '적용된 평가 대상이 없습니다.' : '동일하게 연결된 문항과 적용 대상을 비교합니다.'};
    });
  }

  /**
   * compare(baselineRun, targetRun) -> read-only display data.
   * Rates and delta.rate are ratios (multiply by 100 for percent/percentage points).
   * rows contain exact, unique-ID matches and only indices into the supplied runs.
   * changed pairs are also listed in unmatched on each side (counts overlap).
   * eligible/delta concern the entire test; verified rows can describe a shared
   * subset. Unknown/different profiles never produce verified improvements.
   */
  function compare(baselineRun, targetRun) {
    const old = identify(baselineRun), next = identify(targetRun);
    const baseline = summary(baselineRun, old), target = summary(targetRun, next);
    const profilesKnown = knownProfile(baselineRun?.evaluation_profile) && knownProfile(targetRun?.evaluation_profile);
    const profileMatched = profilesKnown && canonical(baselineRun.evaluation_profile.fields) === canonical(targetRun.evaluation_profile.fields);
    const distinct = Boolean(baseline.runId && target.runId && baseline.runId !== target.runId);
    const differentVersions = text(baselineRun?.aiona_release?.version) && text(targetRun?.aiona_release?.version) && baselineRun.aiona_release.version !== targetRun.aiona_release.version;
    const conditionsMatch = profileMatched && distinct && differentVersions;
    const rows = [], changed = [], unmatched = {baseline: [], target: []};
    const consumed = new Set();
    const entry = (r, code, reason) => ({index: r.index, caseId: r.caseId, code, reason});
    for (const a of old.rows) {
      if (a.invalid) { unmatched.baseline.push(entry(a, a.invalid.code, a.invalid.reason)); continue; }
      const candidates = next.byId.get(a.caseId) || [];
      if (candidates.length !== 1 || candidates[0].invalid) {
        unmatched.baseline.push(entry(a, candidates.length ? 'ambiguous_id' : 'unmatched_id',
          candidates.length ? '대상 기록의 문항 ID 또는 앞 대화가 모호합니다.' : '대상 기록에 같은 문항 ID가 없습니다. ID 변경은 자동으로 추정하지 않습니다.'));
        continue;
      }
      const b = candidates[0], fields = differences(a, b);
      consumed.add(b.index);
      if (fields.length) {
        const names = {question: '질문 원문', reference: '참조 데이터', conversation: '대화·턴', history: '앞 대화의 질문 경로'};
        const reason = fields.map((k) => names[k]).join(' · ') + '이 달라 별도 문항으로 취급합니다.';
        changed.push({caseId: a.caseId, baselineIndex: a.index, targetIndex: b.index, code: 'changed_question', reason, fields});
        unmatched.baseline.push(entry(a, 'changed_question', reason)); unmatched.target.push(entry(b, 'changed_question', reason));
        continue;
      }
      const baselineState = compactState(baselineRun, a.index), targetState = compactState(targetRun, b.index);
      const observed = observedChange(baselineState, targetState);
      const verified = conditionsMatch && baseline.integrity && target.integrity && baselineState.complete && targetState.complete &&
        aligned(baselineRun, a) && aligned(targetRun, b) && !baselineRun?.evaluations?.aiona?.live && !targetRun?.evaluations?.aiona?.live;
      const change = observed === 'pending' ? 'pending' : verified ? observed : 'unverified';
      rows.push({caseId: a.caseId, baselineIndex: a.index, targetIndex: b.index, baselineState, targetState,
        change, changeLabel: labels[change], observedChange: observed, verified,
        answerChanged: old.source[a.index]?.answer !== next.source[b.index]?.answer});
    }
    for (const b of next.rows) {
      if (consumed.has(b.index)) continue;
      const why = b.invalid || {code: 'unmatched_id', reason: '기준 기록에서 유일하고 동일한 문항 ID를 연결하지 못했습니다.'};
      unmatched.target.push(entry(b, why.code, why.reason));
    }
    const sameQuestions = rows.length > 0 && rows.length === old.rows.length && rows.length === next.rows.length;
    const reasons = [];
    const reason = (code, label) => reasons.push({code, label});
    if (!baseline.runId || !target.runId) reason('unknown_run', '실행 ID가 없어 서로 다른 실행인지 확인할 수 없습니다.');
    else if (!distinct) reason('same_run', '같은 실행 기록은 전후 변화로 비교하지 않습니다.');
    if (!differentVersions) reason('same_or_unknown_version', '서로 다른 AIONA 모델 버전을 선택해 주세요. 같은 버전의 반복 실행은 평가 이력에서 확인할 수 있습니다.');
    if (!profilesKnown) reason('profile_unknown', '평가 기준·설정·평가 모델 기록이 불완전해 같은 조건인지 확인할 수 없습니다.');
    else if (!profileMatched) reason('profile_mismatch', '평가 기준·설정 또는 평가 모델이 달라 개선·악화로 확정하지 않습니다.');
    if (!sameQuestions) reason('question_mismatch', '전체 문항 구성이 다릅니다. 동일하게 연결된 공통 문항만 별도로 확인합니다.');
    if (!baseline.integrity || !target.integrity) reason('result_mismatch', '입력 문항 수와 저장된 평가 결과의 연결을 확인할 수 없습니다.');
    if (!baseline.ready) reason('baseline_incomplete', '기준 기록의 AIONA 평가가 모두 완료되지 않았습니다.');
    if (!target.ready) reason('target_incomplete', '대상 기록의 AIONA 평가가 모두 완료되지 않았습니다.');
    const eligible = reasons.length === 0;
    const counts = {improved: 0, regressed: 0, passMaintained: 0, failMaintained: 0, pending: 0, unverified: 0,
      matched: rows.length, unmatchedBaseline: unmatched.baseline.length, unmatchedTarget: unmatched.target.length, changed: changed.length};
    rows.forEach((r) => { counts[r.change]++; });
    return {schema: 'aiona-version-comparison/v1', eligible, reasons, profileMatched, sameQuestions,
      scope: eligible ? 'full' : conditionsMatch && rows.length ? 'matched' : 'unverified', baseline, target,
      delta: eligible ? {pass: target.pass - baseline.pass, fail: target.fail - baseline.fail, rate: target.rate - baseline.rate} : null,
      counts, rows, changed, unmatched, criteria: criteriaComparison(baselineRun, targetRun, rows, conditionsMatch)};
  }

  return {compare};
});
