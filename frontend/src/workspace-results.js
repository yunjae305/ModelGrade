/* Shared presentation rules. Reading a saved result never changes its verdicts. */
((root) => {
  'use strict';
  const outcomeLabels = {issue: 'FAIL · 기준 미충족', clear: 'PASS · 기준 충족', pending: '평가 미완료', waiting: '평가 대기', running: '평가 중'};
  const outcomeKinds = {issue: 'danger', clear: 'success', pending: 'warning', waiting: 'neutral', running: 'purple'};
  const count = (value) => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;

  // Unfinished conditions must not look like a settled 100% pass rate. Keep
  // the PASS/(PASS+FAIL) denominator unchanged once every target is decided.
  function settledRate(passed, failed, pending = 0, unknown = 0) {
    return !count(pending) && !count(unknown) && count(passed) + count(failed) ?
      count(passed) / (count(passed) + count(failed)) : null;
  }

  function conciseReason(id, reason) {
    const text = String(reason || '');
    const label = /(?:도구|호출|검색).*?(?:한도|제한)/.test(text) ? '검색 한도 도달' :
      /(?:연도|기준일|시점).*?(?:없|지정하지|명시하지|확정할 수 없)/.test(text) ? '기준 시점 불명확' :
      /(?:요약·추출·변환|대조 요청|변환 요청|요청.*범위).*?(?:확인|명시|없)/.test(text) ? '자료 대조 범위 확인' :
      /403|404|차단|(?:출처|원문|본문).*?(?:읽지|열지|접근|접속)|서버.*?응답/.test(text) ? '출처 원문 확인 실패' :
      /인용.*?(?:찾지|없|불일치|일치하지)/.test(text) ? '인용 근거 확인 필요' :
      id === 'J6' ? '이전 대화 근거 부족' : '검증 근거 부족';
    return `${id} · ${label}`;
  }

  // Criterion-level "판정 미완료" (no PASS/FAIL): an unsupported FAIL after one re-evaluation or J5 without search.
  function incompleteReason(id, decision) {
    return `${id} · ` + (({JUDGE_FAIL_EVIDENCE_MISSING: '근거 없는 FAIL 재평가 후 미완료', J5_SEARCH_UNAVAILABLE: '검색 불가로 미완료',
      J5_SEARCH_ERROR: '검색 실행 오류로 미완료'})[decision?.reason_code] || '판정 미완료');
  }
  const j5OutcomePending = new Set(['search_unavailable', 'search_error', 'incomplete']);
  const citationLabels = {supported: '출처가 주장을 뒷받침', contradicted: '출처가 주장과 모순', unrelated: '출처가 주장과 무관',
    not_found: '출처에 해당 내용 없음', inaccessible: '출처 원문 접근 불가', unverified: '판정 확정 안 함', unchecked: '확인하지 않음'};
  const searchModes = {live: '웹 검색 사용', disabled: '웹 검색 꺼짐(설정)', unsupported: '검색 도구가 없는 평가자',
    reference: '참조 자료로만 판정(검색 안 함)', not_requested: 'J5 미요청'};

  function resolvedCase(c, record) {
    const completion = record?.code_completion;
    if (completion?.status !== 'SUCCESS' || !completion.resolutions?.length) return c;
    const results = [...(c?.results || [])];
    for (const r of completion.resolutions) {
      const original = results[r.index];
      if (!original || original.condition_id !== r.condition_id || original.area !== 'common' ||
          original.verdict !== null && original.evaluation_method !== 'LLM_COMPLETION') return c;
      results[r.index] = r.result;
    }
    return {...c, results, summary: completion.summary};
  }

  function resolvedBatch(batch) {
    if (!batch) return batch;
    const cases = (batch.cases || []).map((c, i) => resolvedCase(c, batch.jev_eval?.records?.[i]));
    function totals(parts) {
      const total = {pass: 0, fail: 0, unjudged: 0};
      parts.forEach((s) => Object.keys(total).forEach((k) => { total[k] += count(s?.[k]); }));
      return {...total, pass_rate: settledRate(total.pass, total.fail, total.unjudged)};
    }
    return {...batch, cases, summary: {...batch.summary,
      common: totals(cases.map((c) => c.summary?.common)),
      by_subcategory: Object.fromEntries(Object.keys(batch.summary?.by_subcategory || {}).map((id) =>
        [id, totals(cases.map((c) => c.summary?.by_subcategory?.[id]))]))}};
  }

  function semanticState(record, fallbackReason) {
    const decisions = Object.entries(record?.decisions || {});
    const criteria = record?.criteria || decisions.map(([id]) => id);
    const fail = record?.status === 'SUCCESS' ? decisions.filter(([id, d]) => criteria.includes(id) && d.decision === 'FAIL').length : 0;
    const unverified = decisions.filter(([id, d]) => criteria.includes(id) && d?.status === 'UNVERIFIED' && !['PASS', 'FAIL'].includes(d.decision));
    const incomplete = decisions.filter(([id, d]) => criteria.includes(id) && d?.status === 'INCOMPLETE');
    const complete = record?.status === 'SUCCESS' && criteria.length > 0 &&
      criteria.every((id) => ['PASS', 'FAIL'].includes(record.decisions?.[id]?.decision));
    let rawReason = record?.error || fallbackReason || '';
    let text = '평가 대기', reason = '아직 의미 평가 결과가 없습니다.';
    if (record?.status === 'RUNNING') {
      text = '평가 중'; reason = '의미 평가 응답을 기다리고 있습니다.';
    } else if (complete) {
      text = fail ? `문제 ${fail}개` : '통과';
      reason = `적용된 의미 기준 ${criteria.length}개를 모두 판정했습니다.`;
    } else if (unverified.length || incomplete.length) {
      text = incomplete.length ? '판정 미완료' : '확인 필요';
      rawReason = [...unverified, ...incomplete].map(([id, d]) => `${id}: ${d.reason || '검증 근거가 충분하지 않습니다.'}`).join('\n');
      reason = [...new Set([...unverified.map(([id, d]) => conciseReason(id, d.reason)),
        ...incomplete.map(([id, d]) => incompleteReason(id, d))])].join(' · ');
    } else if (record?.status === 'NOT_CONFIGURED') {
      text = '연결 필요'; reason = '의미 평가 서비스가 연결되지 않아 내용을 평가하지 못했습니다.';
    } else if (record?.status === 'API_ERROR') {
      text = '호출 실패';
      reason = /session limit|usage limit|quota|rate.?limit|429|한도|속도 제한/i.test(rawReason) ?
        '의미 평가 서비스의 사용 한도를 초과해 평가하지 못했습니다.' : '의미 평가 서비스 호출에 실패했습니다.';
    } else if (record?.status === 'TIMEOUT') {
      text = '시간 초과'; reason = '제한 시간 안에 의미 평가 결과를 받지 못했습니다.';
    } else if (record?.status === 'INVALID_RESPONSE' || record?.status === 'SUCCESS') {
      text = '판정 오류'; reason = '의미 평가 응답에 필요한 판정이 없어 평가를 완료하지 못했습니다.';
    } else if (record?.status === 'SKIPPED' && /^평가 중단 요청/.test(String(record.error || ''))) {
      text = '평가 중단'; reason = '평가 중단 요청으로 이 질문의 의미 평가를 마치지 못했습니다.';
    } else if (record?.status === 'SKIPPED') {
      text = '평가 제외'; reason = '평가할 질문·답변이 없어 의미 평가에서 제외했습니다.';
    } else if (record?.status === 'INTERRUPTED') {
      text = '평가 중단'; reason = '작업이 중단되어 이 질문의 의미 평가를 마치지 못했습니다.';
    } else if (record?.status) {
      text = '평가 미완료'; reason = '의미 평가를 완료하지 못했습니다.';
    }
    return {text, reason, rawReason, fail, complete, unverified: unverified.length, incomplete: incomplete.length,
      waiting: !record, inProgress: record?.status === 'RUNNING',
      kind: record?.status === 'RUNNING' ? 'purple' : fail ? 'danger' : complete ? 'success' : 'warning'};
  }

  function safeHttpUrl(value) {
    if (typeof value !== 'string' || !/^https?:\/\//i.test(value) || /[\u0000-\u0020\u007f\\]/.test(value)) return null;
    try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && url.hostname && !url.username && !url.password ? url.href : null; }
    catch (_) { return null; }
  }
  const semanticEngineLabel = (value) => value?.engine_label || ({openai_api: 'OpenAI API', anthropic_api: 'Claude API', codex_cli: 'GPT CLI', claude_cli: 'Claude CLI'})[value?.engine] || 'Jev';
  function semanticEvidence(record, make) {
    if (!record) return null;
    const decisions = record.decisions || {}, j5 = decisions.J5, j6 = decisions.J6, verification = record.verification;
    const hasFact = j5?.status !== 'NOT_APPLICABLE' && (j5?.fact_check || j5?.status === 'UNVERIFIED' || j5?.status === 'INCOMPLETE');
    const unresolvedDecisions = Object.entries(decisions).filter(([, d]) => d?.status === 'INCOMPLETE' || d?.reevaluation);
    const citations = Array.isArray(j5?.citation_checks) ? j5.citation_checks : [];
    const hasContext = j6?.status !== 'NOT_APPLICABLE' && (Array.isArray(j6?.context_checks) || j6?.status === 'UNVERIFIED');
    const originals = ['J5', 'J6'].filter((id) => decisions[id]?.model_original_assessment && typeof decisions[id].model_original_assessment === 'object');
    const policyDecisions = Object.entries(decisions).filter(([, value]) => value?.policy_original_assessment && ['PASS', 'FAIL'].includes(value.decision));
    if (!hasFact && !hasContext && !verification && !originals.length && !policyDecisions.length &&
      !unresolvedDecisions.length && !j5?.j5_outcome) return null;
    const box = make('div', {class: 'semantic-evidence'}), note = (text) => make('p', {class: 'hint field-help', text});
    const verdict = (value) => ['PASS', 'FAIL'].includes(value) ? value : '확인 필요';
    const display = (value) => typeof value === 'object' ? JSON.stringify(value) : String(value);
    policyDecisions.forEach(([id, decision]) => {
      const sourceFailure = decision.reason_code === 'SOURCE_ORIGINAL_UNAVAILABLE';
      const failed = decision.decision === 'FAIL', prior = decision.policy_original_assessment;
      box.append(make('div', {class: failed ? 'semantic-policy-failure' : 'semantic-policy-pass',
        [sourceFailure ? 'data-source-policy-detail' : 'data-evidence-policy-detail']: ''},
        make('strong', {text: `${id} ${decision.decision} · ` + (sourceFailure ? '출처 원문 확인 실패' : failed ? '검증 기준 미충족' : '제공 자료 대조 완료')}),
        note(decision.reason || '최종 검증 기준에 따라 판정했습니다.'),
        failed ? note(prior.reason ? '구체 사유: ' + prior.reason : '평가자가 남긴 구체 사유가 없습니다.') : null,
        failed ? note('근거 검증 기준 미충족에 따른 FAIL입니다. 주장 자체의 오류를 확정한 것은 아닙니다.') : null));
      box.append(make('details', {class: 'semantic-policy-original'},
        make('summary', {text: '정책 적용 전 판정·사유'}),
        note('이전 판정: ' + verdict(prior.decision)), note(prior.reason || '별도 사유 없음'),
        note('적용 정책: ' + (decision.decision_policy?.version || '최종 PASS/FAIL 판정'))));
    });
    const outcome = j5?.j5_outcome;
    if (outcome?.code && outcome.code !== 'not_applicable') {
      box.append(make('div', {class: j5OutcomePending.has(outcome.code) ? 'semantic-policy-pending' :
        j5?.decision === 'FAIL' ? 'semantic-policy-failure' : 'semantic-policy-pass', 'data-j5-outcome': outcome.code}, make('strong', {text: 'J5 판정 사유 · ' + (outcome.label || outcome.code)}),
        note(outcome.factual_error_confirmed ? '신뢰할 수 있는 근거와 충돌하는 사실 오류입니다.' :
          j5OutcomePending.has(outcome.code) ? '판정 미완료 — 사실 여부를 확인하지 못한 평가 상태이며 FAIL이 아닙니다.' :
          j5?.decision === 'FAIL' ? '사실 오류를 확정한 FAIL이 아닙니다.' : '근거로 확인된 판정입니다.'),
        outcome.search?.mode ? note('검색 상태: ' + (searchModes[outcome.search.mode] || outcome.search.mode) +
          (outcome.search.limit_reached ? ' · 검색 한도 도달' : '') +
          (outcome.search.errors?.length ? ' · 검색 오류 ' + outcome.search.errors.join(', ') : '')) : null));
    }
    unresolvedDecisions.forEach(([id, decision]) => {
      const again = decision.reevaluation;
      box.append(make('div', {class: decision.status === 'INCOMPLETE' ? 'semantic-policy-pending' :
        decision.decision === 'FAIL' ? 'semantic-policy-failure' : 'semantic-policy-pass',
        'data-incomplete-detail': decision.status === 'INCOMPLETE' ? '' : null},
        make('strong', {text: decision.status === 'INCOMPLETE' ? incompleteReason(id, decision) :
          `${id} 재평가 후 ${decision.decision}`}),
        decision.status === 'INCOMPLETE' ? note(decision.reason || '판정을 완료하지 못했습니다.') : null,
        again ? note('첫 판정: FAIL · ' + (again.first?.reason || '이유 없음') + ' — 근거 확인: ' +
          ((again.first?.fail_evidence?.problems || []).map((p) => ({reason_missing: '이유 없음', quote_missing: '원문 인용 없음',
            quote_not_in_evaluated_text: '인용이 원문에 없음'})[p] || p).join(', ') || '확인 안 됨')) : null,
        again ? note('재평가 1회: ' + (again.retry ? (again.retry.decision || again.retry.status || '판정 없음') +
          (again.retry.reason ? ' · ' + again.retry.reason : '') : '응답 없음') + ' → 최종 ' + (again.final || '미완료')) : null,
        again ? note('근거 확인은 이유와 원문 인용의 형식 검증이며, 이유의 의미적 타당성까지 검증한 것은 아닙니다.') : null));
    });
    if (citations.length) {
      box.append(make('details', {class: 'semantic-citation-evidence', open: j5?.citation_mismatch ? '' : null},
        make('summary', {text: 'J5 답변 출처 확인'}),
        note('답변이 직접 든 출처를 서버가 다시 받아 대조했습니다. 원문을 받지 못하거나 인용이 맞지 않으면 판정에 쓰지 않습니다.'),
        ...citations.map((check) => {
          const url = safeHttpUrl(check.url);
          return make('div', {class: 'jev-reason'}, make('p', {}, make('strong', {text: (citationLabels[check.verdict] || check.verdict) + ' · '}),
            url ? make('a', {href: url, target: '_blank', rel: 'noopener noreferrer', class: 'text-button', text: check.url}) : String(check.url || '')),
            check.claim ? note('답변 구절: ' + check.claim) : null,
            check.quote ? make('p', {}, make('q', {text: check.quote})) : null,
            check.reason ? note('평가자 이유: ' + check.reason) : null, check.note ? note(check.note) : null);
        })));
    }
    const sourceNode = (source, showCheck = false, basis = null) => {
      const value = typeof source === 'string' ? {url: source} : source || {};
      const inputSource = ['question', 'reference', 'history_user'].includes(value.origin);
      const inputLabel = ({question: '질문 원문', reference: '참조 데이터', history_user: `이전 사용자 ${value.turn ?? '미기록'}턴`})[value.origin];
      const url = inputSource ? null : safeHttpUrl(value.url), resolved = inputSource ? null : safeHttpUrl(value.resolved_url);
      const quoteCheck = basis === 'reference' && value.quote && typeof value.quote_verified !== 'boolean' ?
        '제공된 참조 자료 인용 · 외부 출처 검증과 구분' : (inputSource ? '제공 자료 인용: ' : '원문 인용: ') + (value.quote_verified === true ? '확인됨' : value.quote_verified === false ? '확인하지 못함' : '대조 기록 없음');
      return make('li', {}, url ? make('a', {href: url, target: '_blank', rel: 'noopener noreferrer', class: 'text-button', text: value.title || value.url}) : make('span', {text: inputSource ? '제공 자료 대조 · ' + inputLabel : value.title || value.url || '출처 정보 없음'}),
        inputSource && value.title ? note(value.title) : null,
        value.quote ? make('p', {}, make('q', {text: value.quote})) : null,
        showCheck ? note(quoteCheck) : null,
        value.verification_reason ? note('원문 대조 사유: ' + value.verification_reason) : null,
        value.verified_at ? note('원문 확인 시각: ' + value.verified_at) : null,
        value.authority ? note('출처 분류: ' + (({primary: '공식·일차 자료', reliable_secondary: '신뢰 근거가 있는 이차 자료', unestablished: '신뢰성 확인 불가'})[value.authority] || String(value.authority))) : null,
        value.authority_reason ? note('출처 신뢰성 근거: ' + value.authority_reason) : null,
        value.temporal_relevance ? note('시점 적합성: ' + value.temporal_relevance) : null,
        value.assessment_warning ? note('출처 확인 필요: ' + value.assessment_warning) : null,
        !inputSource && value.resolved_url && value.resolved_url !== value.url ? make('p', {}, '대조한 원문: ', resolved ? make('a', {href: resolved, target: '_blank', rel: 'noopener noreferrer', class: 'text-button', text: value.resolved_url}) : make('span', {text: value.resolved_url})) : null);
    };
    if (hasFact) {
      const fact = j5.fact_check || {}, claims = Array.isArray(fact.claims) ? fact.claims : [];
      const section = make('details', {class: 'semantic-fact-evidence'}, make('summary', {text: 'J5 사실 검증 근거'}), note(({web: '검증 기준: 웹 출처', reference: '검증 기준: 참조 데이터', input: '검증 기준: 제공 자료 일치 · 자료 자체의 사실성은 검증하지 않음', mixed: '검증 기준: 제공 자료 일치와 외부 사실 확인 · 주장별 구분', none: '검증 기준: 확인 필요'})[fact.basis] || '검증 기준: 확인 필요'));
      if (j5.status === 'UNVERIFIED') section.append(note('확인 필요 · ' + (j5.reason || '검증 근거가 충분하지 않습니다.')));
      if (!claims.length) section.append(note('확인 필요 · 저장된 검증 주장과 출처가 없습니다.'));
      claims.forEach((claim) => section.append(make('div', {class: 'jev-reason'}, make('p', {}, make('strong', {text: verdict(claim.verdict) + ' · '}), String(claim.claim || '검증 주장 미기록')), claim.reason ? note(claim.reason) : null,
        claim.scope === 'input_consistency' ? note('판정 범위: 제공 자료 일치 · 자료 자체의 사실성은 검증하지 않음') : claim.scope === 'external_fact' ? note('판정 범위: 외부 사실 확인') : null,
        claim.scope_quote ? note('자료 대조 요청: ' + claim.scope_quote) : null,
        Array.isArray(claim.sources) && claim.sources.length ? make('ul', {class: 'finding-list'}, ...claim.sources.map((source) => sourceNode(source, true, fact.basis))) : note(fact.basis === 'reference' ? '참조 데이터에 근거한 검증입니다.' : '확인 필요 · 저장된 검증 출처가 없습니다.'))));
      box.append(section);
    }
    if (hasContext) {
      const checks = Array.isArray(j6.context_checks) ? j6.context_checks : [];
      const section = make('details', {class: 'semantic-context-evidence'}, make('summary', {text: 'J6 이전 대화·현재 답변 근거'}));
      if (j6.status === 'UNVERIFIED') section.append(note('확인 필요 · ' + (j6.reason || '대화 맥락을 확정할 근거가 충분하지 않습니다.')));
      if (!checks.length) section.append(note('확인 필요 · 저장된 이전 턴 인용 근거가 없습니다.'));
      checks.forEach((check) => section.append(make('div', {class: 'jev-reason'}, make('p', {}, make('strong', {text: `이전 ${check.turn ?? '미기록'}턴 · ${verdict(check.decision)}`})),
        note('이전 지시: ' + (check.instruction_quote || '인용 없음')), note('현재 답변: ' + (check.answer_quote || '인용 없음')), check.reason ? note(check.reason) : null)));
      box.append(section);
    }
    if (originals.length) {
      const section = make('details', {class: 'semantic-original-assessment'}, make('summary', {text: '서버 근거 검증 전 모델 판정'}),
        note('현재 결과와 점수에는 서버 근거 검증 후 최종 판정만 사용합니다.'));
      originals.forEach((id) => {
        const current = decisions[id], original = current.model_original_assessment;
        section.append(make('div', {class: 'jev-reason'}, make('strong', {text: id}),
          note('모델 원판정: ' + verdict(original.choice)), original.reason ? note('모델 원래 설명: ' + original.reason) : null,
          note('서버 근거 검증 후 최종 판정: ' + (current.status === 'NOT_APPLICABLE' ? '해당 없음' : verdict(current.decision))),
          current.reason ? note('최종 판정 사유: ' + current.reason) : null));
      });
      box.append(section);
    }
    if (verification && typeof verification === 'object') {
      const section = make('details', {class: 'semantic-verification'}, make('summary', {text: '검색·검증 기록'}));
      if (verification.search_mode) section.append(note('검색 상태: ' + (searchModes[verification.search_mode] || verification.search_mode)));
      if (typeof verification.search_performed === 'boolean') section.append(note('웹 검색 수행: ' + (verification.search_performed ? '예' : '검색하지 않음')));
      if (verification.checked_at) section.append(note('검증 시각: ' + verification.checked_at));
      if (verification.source_validation !== undefined) section.append(note('출처 검증: ' + (({provider: verification.search_performed === false ? '웹 검색을 수행하지 않음 · 출처 대조 결과는 주장별 근거 참조' : '출처 URL을 도구 기록과 대조', provider_and_content: '출처 URL을 도구 기록과 대조하고 인용문 원문 대조를 시도함'})[verification.source_validation] || display(verification.source_validation))));
      if (Array.isArray(verification.source_urls) && verification.source_urls.length) section.append(make('ul', {class: 'finding-list'}, ...verification.source_urls.map((source) => sourceNode(source))));
      box.append(section);
    }
    return box;
  }

  function caseState(result, provider, index) {
    const batch = result?.evaluations?.[provider], source = result?.datasets?.[provider]?.[index];
    const record = batch?.jev_eval?.records?.[index];
    const c = resolvedCase(batch?.cases?.[index], record);
    const checks = (c?.results || []).filter((r) => r.area === 'common');
    const fails = checks.filter((r) => r.counted && r.verdict === 'FAIL');
    const summary = c?.summary?.common || {};
    const unjudged = count(summary.unjudged);
    const code = {pass: count(summary.pass), fail: Math.max(fails.length, count(summary.fail)), unjudged,
      checks, fails, complete: false, text: '평가 대기', kind: 'neutral', reason: '', pendingReason: '', pendingDetail: ''};
    const semantic = semanticState(!record && batch?.live && ['ERROR', 'CANCELLED'].includes(result.status) ?
      {status: 'INTERRUPTED', error: result.error} : record, batch?.jev_eval?.access_reason);
    let outcome = 'waiting', detail = result?.status === 'EVALUATING' ? '평가를 진행하고 있습니다. 이 모델의 코드 검사 결과가 아직 저장되지 않았습니다.' :
      '답변 원문이 준비되어 있습니다. 평가를 시작해 주세요.';
    if (!source || (provider !== 'aiona' && source.collection?.status !== 'SUCCESS')) {
      outcome = batch ? 'pending' : 'waiting';
      detail = !source || source.collection?.status === 'NOT_COLLECTED' ? '답변이 수집되지 않았습니다.' :
        source.collection?.status === 'INCOMPLETE' ? '답변 수집이 완료되지 않아 평가에서 제외했습니다.' : '답변 수집에 실패해 평가에서 제외했습니다.';
      code.text = batch ? '평가 제외' : '평가 대기'; code.reason = detail;
      semantic.text = batch ? '평가 제외' : '평가 대기'; semantic.reason = detail; semantic.complete = false;
    } else if (batch && c?.case_status !== 'EVALUATED') {
      outcome = 'pending'; detail = '입력 상태를 확인해야 하므로 평가에서 제외했습니다.';
      code.text = '입력 확인 필요'; code.kind = 'warning'; code.reason = c?.input_error || detail;
    } else if (batch) {
      code.complete = unjudged === 0 && code.pass + code.fail > 0;
      code.text = code.fail ? `문제 ${code.fail}개` : unjudged ? '일부 판정 불가' : code.complete ? '통과' : '판정 없음';
      code.kind = code.fail ? 'danger' : code.complete ? 'success' : 'warning';
      code.reason = `조건 PASS ${code.pass}개 · FAIL ${code.fail}개 · ${unjudged}개 미완료`;
      if (record?.code_completion?.status === 'SUCCESS') code.reason += ' · LLM 보완 판정 포함';
      else if (record?.code_completion?.error) code.reason += ' · 보완 평가 오류: ' + record.code_completion.error;
      // A finished semantic call can still leave CODE conditions unresolved; name that cause.
      if (unjudged && semantic.complete) {
        const completion = record?.code_completion;
        code.pendingReason = completion?.error ? '코드 보완 판정 실패' :
          completion?.status === 'SUCCESS' ? '코드 보완 판정을 조건에 연결하지 못함' : '코드 보완 판정 기록 없음';
        code.pendingDetail = code.pendingReason + (completion?.error ? ' · ' + completion.error : '');
      }
      if (code.fail || semantic.fail) {
        outcome = 'issue'; detail = `코드 검사 ${code.fail}개 · 의미 평가 ${semantic.fail}개 문제 발견`;
        if (record?.decisions?.J5?.decision === 'FAIL' && record.decisions.J5.reason_code === 'SOURCE_ORIGINAL_UNAVAILABLE') detail += ' · J5 출처 원문 확인 실패';
        if (Object.values(record?.decisions || {}).some((d) => d.decision === 'FAIL' && d.reason_code === 'EVALUATION_EVIDENCE_INSUFFICIENT')) detail += ' · 검증 근거 부족';
        if (!code.complete || !semantic.complete) detail += ' · 남은 평가 있음';
      } else if (code.complete && semantic.complete) {
        outcome = 'clear'; detail = '적용된 코드 검사와 의미 기준에서 문제를 발견하지 못했습니다.';
      } else if (batch.live && result.status === 'EVALUATING' && (semantic.waiting || semantic.inProgress)) {
        outcome = semantic.inProgress ? 'running' : 'waiting'; detail = semantic.reason;
        if (unjudged) { code.text = '보완 평가 대기'; code.reason = '코드 미판정 조건을 LLM이 보완 판정합니다.'; }
      } else {
        outcome = 'pending'; detail = !semantic.complete ? semantic.reason : code.pendingDetail || '일부 코드 조건을 판정하지 못했습니다.';
      }
    }
    const criteriaComplete = Boolean(source && c?.case_status === 'EVALUATED' && code.complete && semantic.complete);
    // One confirmed failure settles the question's outcome. Unfinished
    // criteria remain visible in the detail, never as a second question count.
    const complete = outcome === 'issue' || outcome === 'clear';
    return {outcome, text: outcomeLabels[outcome], kind: outcomeKinds[outcome], detail, code, semantic,
      complete, criteriaComplete, pending: outcome === 'pending'};
  }

  function modelSummary(result, provider, total) {
    const rows = Array.from({length: total}, (_, i) => caseState(result, provider, i));
    const counts = {issue: 0, clear: 0, pending: 0, waiting: 0, running: 0};
    rows.forEach((r) => { counts[r.outcome]++; });
    const evaluated = Boolean(result?.evaluations?.[provider]);
    return {rows, counts, evaluated, total,
      complete: rows.filter((r) => r.complete).length,
      criteriaComplete: rows.filter((r) => r.criteriaComplete).length,
      semanticComplete: rows.filter((r) => r.semantic.complete).length,
      semanticIncomplete: rows.filter((r) => r.pending && !r.semantic.complete && r.code.checks.length &&
        (!result?.evaluations?.[provider]?.live || !r.semantic.waiting && !r.semantic.inProgress)).length,
      codeIncomplete: rows.filter((r) => r.pending && r.semantic.complete && r.code.pendingReason).length,
      text: result?.status === 'EVALUATING' ? '평가 중' : !evaluated ? '평가 대기' : rows.every((r) => r.complete) ? '평가 완료' : '평가 미완료',
      kind: result?.status === 'EVALUATING' ? 'purple' : !evaluated ? 'neutral' : rows.every((r) => r.complete) ? 'neutral' : 'warning'};
  }

  function runSummary(result) {
    const providers = result?.providers || [], totalPerModel = count(result?.question_count);
    const models = providers.map((p) => modelSummary(result, p, totalPerModel));
    const totals = {issue: 0, clear: 0, pending: 0, waiting: 0, running: 0};
    models.forEach((m) => Object.keys(totals).forEach((k) => { totals[k] += m.counts[k]; }));
    const total = totalPerModel * providers.length, complete = models.reduce((n, m) => n + m.complete, 0);
    return {models, counts: totals, total, complete, allComplete: total > 0 && complete === total};
  }

  // Work processed and verdicts established are independent quantities. Keep
  // these counters in the same units when a run becomes terminal. Old records
  // without progress metadata are reconstructed only from saved work, never
  // from COMPLETED status or the number of passing answers.
  function processingProgress(result, provider) {
    const providers = provider === undefined ? result?.providers || [] : [provider];
    const keys = ['code_done', 'code_total', 'semantic_done', 'semantic_total',
      'semantic_success', 'semantic_unverified', 'semantic_errors', 'skipped'];
    const total = Object.fromEntries(keys.map((key) => [key, 0]));
    for (const p of providers) {
      const detail = result?.evaluation_progress?.[p], batch = result?.evaluations?.[p];
      const rows = (batch?.jev_eval?.records || []).filter((record) => record?.status &&
        !['RUNNING', 'WAITING', 'PENDING', 'INTERRUPTED'].includes(record.status));
      const fallback = {code_done: (batch?.cases || []).filter(Boolean).length,
        code_total: count(result?.question_count), semantic_done: rows.length,
        semantic_total: count(result?.question_count), semantic_success: 0,
        semantic_unverified: 0, semantic_errors: 0, skipped: 0};
      rows.forEach((record) => {
        const verdict = semanticState(record);
        fallback[record.review_required || verdict.unverified ? 'semantic_unverified' : verdict.complete ?
          'semantic_success' : record.status === 'SKIPPED' ? 'skipped' : 'semantic_errors']++;
      });
      for (const key of keys) {
        total[key] += typeof detail?.[key] === 'number' && Number.isFinite(detail[key]) && detail[key] >= 0 ?
          detail[key] : fallback[key];
      }
    }
    return total;
  }

  function questionOutcome(result, providers, index) {
    const outcomes = providers.map((p) => caseState(result, p, index).outcome);
    return ['issue', 'pending', 'running', 'waiting', 'clear'].find((o) => outcomes.includes(o)) || 'waiting';
  }

  function questionMatches(result, providers, index, outcome) {
    if (!outcome) return true;
    return questionOutcome(result, providers, index) === outcome;
  }

  function finalSummary(result) {
    const total = count(result?.question_count), providers = result?.providers || [];
    const models = providers.map((provider) => ({provider, ...modelSummary(result, provider, total)}));
    const baseline = result?.datasets?.[providers.includes('aiona') ? 'aiona' : providers[0]] || [];
    const comparable = total > 0 && baseline.length === total && providers.every((provider) => {
      const rows = result?.datasets?.[provider] || [];
      return rows.length === total && rows.every((row, i) => row && typeof row.question === 'string' &&
        row.question === baseline[i]?.question && row.conversation_id === baseline[i]?.conversation_id && row.turn === baseline[i]?.turn);
    });
    const complete = models.length > 0 && models.every((m) => m.complete === total &&
      result?.evaluations?.[m.provider]?.cases?.length === total);
    const ready = result?.status === 'COMPLETED' && comparable && complete;
    const ordered = ready ? [...models].sort((a, b) => b.counts.clear - a.counts.clear) : models;
    const rows = ordered.map((model) => ({...model,
      score: ready ? model.counts.clear / total * 100 : null,
      rank: ready && models.length > 1 ? ordered.findIndex((m) => m.counts.clear === model.counts.clear) + 1 : null,
      tied: ready && ordered.filter((m) => m.counts.clear === model.counts.clear).length > 1,
      incomplete: Math.max(0, total - model.complete)}));
    return {ready, comparable, complete, total, rows,
      leaders: ready ? rows.filter((m) => m.counts.clear === rows[0].counts.clear) : [],
      incomplete: rows.reduce((n, m) => n + m.incomplete, 0)};
  }

  const comparisonCriteria = [
    {id: '1', group: 'code', name: '출력 무결성', unit: '조건'},
    {id: '2', group: 'code', name: '요구 조건 준수', unit: '조건'},
    {id: '3', group: 'code', name: '수치·데이터 정합성', unit: '조건'},
    {id: 'J1', group: 'semantic', name: '의도 충족성', unit: '질문'},
    {id: 'J2', group: 'semantic', name: '내용 적절성', unit: '질문'},
    {id: 'J3', group: 'semantic', name: '논리 타당성', unit: '질문'},
    {id: 'J4', group: 'semantic', name: '충분성', unit: '질문'},
    {id: 'J5', group: 'semantic', name: '데이터 신뢰도', unit: '질문'},
    {id: 'J6', group: 'semantic', name: '긴 대화 맥락 유지', unit: '질문'}
  ];

  function criterionSummary(result, provider, criterion) {
    const batch = result?.evaluations?.[provider];
    let passed = 0, failed = 0, pending = 0, unknown = 0, excluded = 0;
    const pendingReasons = {unverified: 0, incomplete: 0, errors: 0, missing: 0};
    if (criterion.group === 'code') {
      for (const [index, raw] of (batch?.cases || []).entries()) {
        const c = resolvedCase(raw, batch?.jev_eval?.records?.[index]);
        if (c.case_status !== 'EVALUATED') { excluded++; continue; }
        const part = c.summary?.by_category?.[criterion.id] || {};
        passed += count(part.pass); failed += count(part.fail); pending += count(part.unjudged);
      }
      if (batch) unknown += Math.max(0, count(result.question_count) - (batch.cases || []).length);
    } else {
      const records = batch?.jev_eval?.records || [];
      for (const record of records) {
        if (!record || record.status === 'RUNNING') { unknown++; continue; }
        if (record.status === 'SKIPPED') { excluded++; continue; }
        if (!(record.criteria || []).includes(criterion.id)) continue;
        const decision = record.status === 'SUCCESS' ? record.decisions?.[criterion.id]?.decision : null;
        if (decision === 'PASS') passed++;
        else if (decision === 'FAIL') failed++;
        else {
          pending++;
          if (record.status && record.status !== 'SUCCESS') pendingReasons.errors++;
          else if (record.decisions?.[criterion.id]?.status === 'UNVERIFIED') pendingReasons.unverified++;
          else if (record.decisions?.[criterion.id]?.status === 'INCOMPLETE') pendingReasons.incomplete++;
          else pendingReasons.missing++;
        }
      }
      if (batch) unknown += Math.max(0, count(result.question_count) - records.length);
    }
    const decided = passed + failed;
    return {available: Boolean(batch), passed, failed, pending, pendingReasons, unknown, excluded,
      targets: decided + pending, decided, rate: settledRate(passed, failed, pending, unknown)};
  }

  function evidenceNotice(run, provider = 'aiona') {
    const semantic = run?.evaluations?.[provider]?.jev_eval;
    if (!semantic) return '';
    const judge = run.evaluation_profile?.fields?.judge || semantic.client || {};
    const notices = [];
    if (judge.web_search === 'disabled' || judge.web_search === false) notices.push('웹 검색을 사용하지 않은 당시 평가 결과입니다.');
    const records = (semantic.records || []).filter(Boolean);
    const hasEvidence = records.some((record) => record.verification || record.decisions?.J5?.fact_check || record.decisions?.J6?.context_checks);
    if (records.length && !hasEvidence) notices.push('사실 검증 출처·대화 인용 근거가 저장되어 있지 않습니다. 점수와 판정은 저장 당시 결과입니다.');
    return notices.join(' ');
  }
  const api = {incompleteReason, caseState, semanticState, modelSummary, runSummary, finalSummary, questionOutcome, questionMatches, comparisonCriteria, criterionSummary,
    resolvedCase, resolvedBatch, safeHttpUrl, semanticEvidence, semanticEngineLabel, evidenceNotice, processingProgress, settledRate, conciseReason};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.AionaResults = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
