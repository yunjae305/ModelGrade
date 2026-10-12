/* Locate saved FAIL evidence in an unchanged answer. No HTML, verdict changes, or requests. */
((root) => {
  'use strict';
  const policyCodes = new Set(['SOURCE_ORIGINAL_UNAVAILABLE', 'EVALUATION_EVIDENCE_INSUFFICIENT']);
  const names = {J1: '의도 충족성', J2: '내용 적절성', J3: '논리 타당성', J4: '충분성', J5: '데이터 신뢰도', J6: '긴 대화 맥락 유지'};
  const list = value => Array.isArray(value) ? value : [];
  const text = value => typeof value === 'string' ? value : '';

  function mergeSpans(spans) {
    const result = [];
    for (const span of spans.sort((a, b) => a.start - b.start || a.end - b.end)) {
      const prior = result[result.length - 1];
      if (prior && span.start <= prior.end) prior.end = Math.max(prior.end, span.end);
      else result.push({start: span.start, end: span.end});
    }
    return result;
  }

  function exactMatches(answer, quote, start = 0, end = answer.length) {
    if (!quote || !quote.trim()) return [];
    const found = [];
    let from = start;
    while (from <= end - quote.length) {
      const position = answer.indexOf(quote, from);
      if (position < 0 || position + quote.length > end) break;
      found.push({start: position, end: position + quote.length});
      from = position + 1;
    }
    return found;
  }

  function whitespaceProjection(value) {
    let normalized = '', offset = 0;
    const starts = [], ends = [];
    for (const char of value) {
      const start = offset;
      offset += char.length;
      if (/\s/u.test(char)) {
        if (!normalized || normalized.endsWith(' ')) {
          if (normalized) ends[ends.length - 1] = offset;
          continue;
        }
        normalized += ' '; starts.push(start); ends.push(offset);
      } else {
        normalized += char;
        for (let unit = 0; unit < char.length; unit++) { starts.push(start); ends.push(offset); }
      }
    }
    return {value: normalized, starts, ends};
  }

  function claimMatches(answer, quote, projection) {
    if (!quote.trim()) return [];
    const needle = quote.trim().replace(/\s+/gu, ' ');
    return mergeSpans(exactMatches(projection.value, needle).map(span => ({
      start: projection.starts[span.start], end: projection.ends[span.end - 1]
    })).filter(span => Number.isInteger(span.start) && span.end > span.start));
  }

  function answerLines(answer) {
    const lines = [];
    let start = 0;
    for (let i = 0; i <= answer.length; i++) {
      if (i === answer.length || answer[i] === '\n' || answer[i] === '\r') {
        lines.push({start, end: i, value: answer.slice(start, i)});
        if (answer[i] === '\r' && answer[i + 1] === '\n') i++;
        start = i + 1;
      }
    }
    return lines;
  }

  function codeSpans(answer, codeCase, row, lines, codepointOffsets) {
    const spans = [];
    const aligned = typeof codeCase?.answer !== 'string' || codeCase.answer === answer;
    if (!aligned) return spans;
    if (aligned) for (const evidence of list(row.evidence)) {
      if (evidence?.target !== 'answer' || !Number.isInteger(evidence.start) || evidence.start < 0) continue;
      const end = Number.isInteger(evidence.end) && evidence.end > evidence.start ? evidence.end : evidence.start + 1;
      const from = codepointOffsets[evidence.start], to = codepointOffsets[end];
      if (!Number.isInteger(from) || !Number.isInteger(to) || to <= from) continue;
      const excerpt = text(evidence.excerpt);
      if (excerpt && answer.slice(from, to) !== excerpt) continue;
      spans.push({start: from, end: to});
    }
    // Explicit offsets identify a specific occurrence and take precedence over
    // repeated words in presentation snippets.
    if (spans.length) return mergeSpans(spans);
    for (const location of list(row.explain?.locations)) {
      if (!location || (location.target && location.target !== 'answer')) continue;
      if (/질문|기대한|원본에서|참조|출처/.test(text(location.label))) continue;
      if (location.kind === 'snippet') {
        const line = Number.isInteger(location.line) ? lines[location.line - 1] : null;
        if (Number.isInteger(location.line) && !line) continue;
        const quotes = list(location.segments).length ? list(location.segments).filter(part => part?.mark === true).map(part => text(part.text)) : [text(location.hit)];
        for (const quote of quotes) spans.push(...exactMatches(answer, quote, line?.start ?? 0, line?.end ?? answer.length));
      } else if (location.kind === 'code') {
        for (const marked of list(location.lines)) {
          if (marked?.mark !== true || !text(marked.text).trim()) continue;
          if (Number.isInteger(marked.answer_line)) {
            const line = lines[marked.answer_line - 1];
            if (line && line.value === marked.text) spans.push({start: line.start, end: line.end});
          } else spans.push(...exactMatches(answer, marked.text));
        }
      } else if (location.kind === 'line' && location.mark !== false && text(location.text).trim()) {
        const number = location.answer_line ?? location.line;
        if (Number.isInteger(number)) {
          const line = lines[number - 1];
          if (line && line.value === location.text) spans.push({start: line.start, end: line.end});
        } else spans.push(...exactMatches(answer, location.text));
      }
    }
    return mergeSpans(spans);
  }

  function genericQuotes(answer, decision) {
    const reason = text(decision.reason), exclusions = [];
    for (const claim of list(decision.fact_check?.claims)) {
      if (claim?.verdict === 'PASS') exclusions.push(text(claim.claim));
      for (const source of list(claim?.sources)) exclusions.push(text(source?.quote));
    }
    for (const check of list(decision.context_checks)) {
      exclusions.push(text(check?.instruction_quote));
      if (check?.decision === 'PASS') exclusions.push(text(check.answer_quote));
    }
    const spans = [];
    const quoted = /“([^”\n]+)”|‘([^’\n]+)’|「([^」\n]+)」|『([^』\n]+)』|"([^"\n]+)"|'([^'\n]+)'|`([^`\n]+)`/gu;
    for (const match of reason.matchAll(quoted)) {
      const quote = match.slice(1).find(value => value !== undefined).trim();
      // Generic reasons may quote short labels or an expected answer. Do not
      // turn those into guessed error locations just because the token occurs.
      if (Array.from(quote.replace(/\s/gu, '')).length < 6 || !/\p{L}/u.test(quote)) continue;
      if (exclusions.some(excluded => excluded && excluded.includes(quote))) continue;
      const prefix = reason.slice(Math.max(0, match.index - 65), match.index).split(/[.!?\n]/u).pop().toLowerCase();
      const lastEnd = word => prefix.lastIndexOf(word) < 0 ? -1 : prefix.lastIndexOf(word) + word.length;
      const referenced = Math.max(...['질문', '요청', '이전 대화', '앞선 대화', '앞 지시', '출처', '참조', '원문 자료',
        '정답', '기대', '요구한', '요구된', '올바른 값', '정확한 값', '올바른 답변', '정확한 답변',
        'history', 'question', 'source', 'expected', 'expected answer', 'expected response', 'correct answer'].map(lastEnd));
      const answered = Math.max(...['답변', '응답', 'answer', 'response'].map(lastEnd));
      if (referenced >= 0 && referenced >= answered) continue;
      spans.push(...exactMatches(answer, quote));
    }
    return mergeSpans(spans);
  }

  function segmentsFor(answer, findings) {
    if (!answer.length) return [];
    const events = new Map([[0, {add: [], remove: []}], [answer.length, {add: [], remove: []}]]);
    findings.forEach((finding, index) => finding.spans.forEach(span => {
      if (!events.has(span.start)) events.set(span.start, {add: [], remove: []});
      if (!events.has(span.end)) events.set(span.end, {add: [], remove: []});
      events.get(span.start).add.push(index); events.get(span.end).remove.push(index);
    }));
    const points = [...events.keys()].sort((a, b) => a - b), active = new Set(), segments = [];
    for (let i = 0; i < points.length - 1; i++) {
      const start = points[i], end = points[i + 1], event = events.get(start);
      event.remove.forEach(index => active.delete(index)); event.add.forEach(index => active.add(index));
      const findingIds = [...active].sort((a, b) => a - b).map(index => findings[index].id);
      const prior = segments[segments.length - 1];
      if (prior && prior.end === start && JSON.stringify(prior.findingIds) === JSON.stringify(findingIds)) prior.end = end;
      else segments.push({start, end, findingIds});
    }
    return segments;
  }

  function locateFailures(value, codeCase, semanticRecord) {
    const answer = text(value), findings = [], lines = answerLines(answer), projection = whitespaceProjection(answer);
    const aligned = typeof codeCase?.answer !== 'string' || codeCase.answer === answer;
    const codepointOffsets = [0];
    for (const char of answer) codepointOffsets.push(codepointOffsets[codepointOffsets.length - 1] + char.length);
    function add(finding, spans) {
      finding.spans = aligned ? mergeSpans(spans) : [];
      finding.locationNote = !aligned ? '평가한 답변과 현재 원문이 달라 위치를 표시할 수 없습니다.' : finding.spans.length ? '' : answer.length ? '답변 원문에서 특정 위치를 확인할 근거가 없습니다.' : '답변이 비어 있어 표시할 위치가 없습니다.';
      findings.push(finding);
    }
    list(codeCase?.results).forEach((row, index) => {
      if (!row || row.area !== 'common' || row.counted !== true || row.verdict !== 'FAIL') return;
      add({id: `code:${index}`, codeIndex: index, criterionId: row.subcategory || row.check_id || '',
        kind: 'code', title: row.title || row.explain?.headline || '공통 기준 미충족',
        reason: row.explain?.summary || row.reason || ''}, codeSpans(answer, codeCase, row, lines, codepointOffsets));
    });
    if (semanticRecord?.status === 'SUCCESS') {
      const decisions = semanticRecord.decisions || {}, requested = new Set(Array.isArray(semanticRecord.criteria) ? semanticRecord.criteria : Object.keys(decisions));
      const unrequested = new Set(list(semanticRecord.not_requested));
      for (const cid of Object.keys(decisions).sort()) {
        const decision = decisions[cid];
        if (!requested.has(cid) || unrequested.has(cid) || decision?.decision !== 'FAIL') continue;
        const policy = policyCodes.has(decision.reason_code), kind = policy ? 'verification' : 'semantic';
        const title = `${cid} · ${policy ? '검증 기준 미충족' : decision.name || names[cid] || '기준 미충족'}`;
        const base = {criterionId: cid, kind, title}, prefix = `semantic:${encodeURIComponent(cid)}`;
        const facts = decision.fact_check || decision.policy_original_assessment?.fact_check;
        let locatedEvidence = false;
        if (cid === 'J5') {
          const sourceIndices = new Set(list(decision.decision_policy?.failures).map(failure => failure?.claim_index).filter(Number.isInteger));
          list(facts?.claims).forEach((claim, index) => {
            if (!claim || (policy ? claim.verdict !== 'UNVERIFIED' || decision.reason_code === 'SOURCE_ORIGINAL_UNAVAILABLE' && !sourceIndices.has(index) : claim.verdict !== 'FAIL')) return;
            locatedEvidence = true;
            add({...base, id: `${prefix}:claim:${index}`, reason: policy ? decision.reason || claim.reason || '' : claim.reason || decision.reason || ''}, claimMatches(answer, text(claim.claim), projection));
          });
        } else if (cid === 'J6') {
          const checks = decision.context_checks || decision.policy_original_assessment?.context_checks;
          list(checks).forEach((check, index) => {
            if (!check || check.decision !== (policy ? 'UNVERIFIED' : 'FAIL')) return;
            locatedEvidence = true;
            add({...base, id: `${prefix}:context:${index}`, reason: policy ? decision.reason || check.reason || '' : check.reason || decision.reason || ''}, claimMatches(answer, text(check.answer_quote), projection));
          });
        }
        if (!locatedEvidence) add({...base, id: `${prefix}:decision`, reason: decision.reason || ''},
          policy || cid === 'J6' ? [] : genericQuotes(answer, decision));
      }
    }
    return {findings, segments: segmentsFor(answer, findings)};
  }

  const api = {locateFailures};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.AionaFailureLocations = api;
})(typeof window !== 'undefined' ? window : globalThis);
