/* Locate saved FAIL evidence in an unchanged answer. No HTML, verdict changes, or requests. */
((root) => {
  'use strict';
  const policyCodes = new Set(['SOURCE_ORIGINAL_UNAVAILABLE', 'EVALUATION_EVIDENCE_INSUFFICIENT']);
  const names = {J1: '의도 충족성', J2: '내용 적절성', J3: '논리 타당성', J4: '충분성', J5: '데이터 신뢰도', J6: '멀티턴 맥락 유지', J7: '롱컨텍스트 활용'};
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
    // 서버가 FAIL 근거를 원문과 대조한 기록(fail_evidence) — 인용이 실제로 있는 곳(answer·question·reference·history).
    // places_complete 가 없는 옛 기록은 첫 원문만 적었으므로 '답변에 없음'에만 쓰고 '답변에만 있음'으로는 쓰지 않는다.
    const complete = decision.fail_evidence?.places_complete === true, places = new Map();
    for (const match of list(decision.fail_evidence?.matched_quotes)) {
      const quote = text(match?.quote).trim();
      if (quote) places.set(quote, new Set([match.found_in, ...list(match.also_in)]));
    }
    const spans = [];
    const quoted = /“([^”\n]+)”|‘([^’\n]+)’|「([^」\n]+)」|『([^』\n]+)』|"([^"\n]+)"|'([^'\n]+)'|`([^`\n]+)`/gu;
    for (const match of reason.matchAll(quoted)) {
      const quote = match.slice(1).find(value => value !== undefined).trim();
      // Generic reasons may quote short labels or an expected answer. Do not
      // turn those into guessed error locations just because the token occurs.
      if (Array.from(quote.replace(/\s/gu, '')).length < 6 || !/\p{L}/u.test(quote)) continue;
      if (exclusions.some(excluded => excluded && excluded.includes(quote))) continue;
      const known = places.get(quote);
      if (known && !known.has('answer')) continue;   // 서버 대조로 답변에 없는 인용(입력·질문 인용) — 답변을 먼저 보므로 옛 기록도 맞다
      const prefix = reason.slice(Math.max(0, match.index - 65), match.index).split(/[.!?\n]/u).pop().toLowerCase();
      const lastEnd = word => prefix.lastIndexOf(word) < 0 ? -1 : prefix.lastIndexOf(word) + word.length;
      // 출처를 가리키는 말 — '입력에 없는'·'absent from the input' 처럼 부정하면 그 인용의 출처가 아니다
      const sourceEnd = (word, wholeWord = false) => {
        let end = -1;
        const pattern = new RegExp(wholeWord ? `(?<![a-z])${word}(?![a-z])` : word.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&'), 'gu');
        for (const found of prefix.matchAll(pattern)) {
          const after = prefix.slice(found.index + word.length), before = prefix.slice(0, found.index);
          if (/^\s*(?:에|에는|에도|엔)\s*(?:없|나오지 않|언급되지 않)/u.test(after) ||
            /(?:not (?:in|from)|absent from|missing from|not found in)(?: the)?\s*$/u.test(before)) continue;
          end = found.index + word.length;
        }
        return end;
      };
      // 기대·정답을 인용한 것 — 답변에 같은 문구가 있어도 오류 위치가 아니다 (서버 기록과 상관없이)
      const expected = Math.max(...['정답', '기대', '요구한', '요구된', '올바른 값', '정확한 값', '올바른 답변', '정확한 답변',
        'expected', 'expected answer', 'expected response', 'correct answer'].map(lastEnd));
      // '입력'·input — J7 이유가 긴 입력(질문·참조 자료)의 해당 부분을 인용할 때
      const sourced = Math.max(sourceEnd('input', true), ...['질문', '요청', '이전 대화', '앞선 대화', '앞 지시', '출처', '참조', '원문 자료', '입력',
        'history', 'question', 'source'].map(word => sourceEnd(word)));
      const answered = Math.max(...['답변', '응답', 'answer', 'response'].map(lastEnd));
      if (expected >= 0 && expected >= answered) continue;
      // 답변에만 있는 인용(서버가 모든 원문을 대조한 기록)은 출처를 가리키는 말과 상관없이 답변의 위치다
      if (!(complete && known && !['question', 'reference', 'history'].some(place => known.has(place))) &&
        sourced >= 0 && sourced >= answered) continue;
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

  // Policy FAILs show the policy sentence and the evaluator's concrete reason together.
  const joinReasons = (...parts) => [...new Set(parts.filter((part) => typeof part === 'string' && part.trim()))].join(' · ');

  function locateFailures(value, codeCase, semanticRecord) {
    const answer = text(value), findings = [], lines = answerLines(answer), projection = whitespaceProjection(answer);
    const aligned = typeof codeCase?.answer !== 'string' || codeCase.answer === answer;
    const codepointOffsets = [0];
    for (const char of answer) codepointOffsets.push(codepointOffsets[codepointOffsets.length - 1] + char.length);
    // 위치가 없는 FAIL 은 이유를 구분해 적는다 — 빠진 것을 지적한 FAIL(표 없음·문장 부족·J4 누락)은 고장이 아니라 원래 위치가 없다.
    function missingNote(finding) {
      if (finding.kind === 'code' && (finding.expected || finding.actual))
        return `요구: ${finding.expected || '—'} · 실제: ${finding.actual || '—'} — 답변 전체에 대한 조건이라 표시할 위치가 없습니다.`;
      if (finding.kind === 'verification') return '평가 쪽 검증 기준에 따른 판정이라 답변의 특정 위치를 가리키지 않습니다.';
      // 누락형으로 확인된 판정만 '빠진 내용' — J4 는 정의상 빠진 것만 보고, items 는 누락형 항목(답변 인용이 필요 없는 아니오, quote_check.answer.required=false)만
      if (finding.kind === 'semantic' && (finding.omission || finding.criterionId === 'J4' && !finding.itemId))
        return '답변에 빠진 내용을 지적한 판정이라 표시할 위치가 없습니다. 위 사유를 확인하세요.';
      return '저장된 판정 근거의 인용을 답변 원문에서 찾지 못해 위치를 표시하지 않습니다. 위 사유를 확인하세요.';
    }
    function add(finding, spans) {
      finding.spans = aligned ? mergeSpans(spans) : [];
      finding.locationNote = !aligned ? '평가한 답변과 현재 원문이 달라 위치를 표시할 수 없습니다.' : finding.spans.length ? '' : answer.length ? missingNote(finding) : '답변이 비어 있어 표시할 위치가 없습니다.';
      findings.push(finding);
    }
    list(codeCase?.results).forEach((row, index) => {
      if (!row || row.area !== 'common' || row.counted !== true || row.verdict !== 'FAIL') return;
      add({id: `code:${index}`, codeIndex: index, criterionId: row.subcategory || row.check_id || '',
        kind: 'code', title: row.title || row.explain?.headline || '공통 기준 미충족',
        reason: row.explain?.summary || row.reason || '', expected: text(row.explain?.expected), actual: text(row.explain?.actual)},
        codeSpans(answer, codeCase, row, lines, codepointOffsets));
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
        if (cid === 'J5' && decision.citation_mismatch) {
          // A cited source that does not support the sentence it is attached to — locate that sentence.
          list(decision.citation_checks).forEach((check, index) => {
            if (!check || !['contradicted', 'unrelated', 'not_found', 'inaccessible'].includes(check.verdict)) return;
            locatedEvidence = true;
            add({...base, title: `${cid} · ${check.verdict === 'inaccessible' ? '출처 접근 불가' : '출처 불일치'}`, id: `${prefix}:citation:${index}`,
              reason: joinReasons(`${text(check.url)} — ${check.note || ''}`, check.reason)}, claimMatches(answer, text(check.claim), projection));
          });
        }
        if (cid === 'J5') {
          const sourceIndices = new Set(list(decision.decision_policy?.failures).map(failure => failure?.claim_index).filter(Number.isInteger));
          list(facts?.claims).forEach((claim, index) => {
            if (!claim || (policy ? claim.verdict !== 'UNVERIFIED' || decision.reason_code === 'SOURCE_ORIGINAL_UNAVAILABLE' && !sourceIndices.has(index) : claim.verdict !== 'FAIL')) return;
            locatedEvidence = true;
            add({...base, id: `${prefix}:claim:${index}`, reason: policy ? joinReasons(decision.reason, claim.reason) : claim.reason || decision.reason || ''}, claimMatches(answer, text(claim.claim), projection));
          });
        } else if (cid === 'J6') {
          const checks = decision.context_checks || decision.policy_original_assessment?.context_checks;
          list(checks).forEach((check, index) => {
            if (!check || check.decision !== (policy ? 'UNVERIFIED' : 'FAIL')) return;
            locatedEvidence = true;
            add({...base, id: `${prefix}:context:${index}`, reason: policy ? joinReasons(decision.reason, check.reason) : check.reason || decision.reason || ''}, claimMatches(answer, text(check.answer_quote), projection));
          });
        }
        if (!locatedEvidence && decision.subs && typeof decision.subs === 'object') {
          // items 파이프라인: 아니오 항목마다 답변 인용으로 위치를 찾는다 (인용이 없으면 누락형)
          Object.values(decision.subs).flatMap(sub => list(sub?.items)).filter(item => item?.decision === 'NO').forEach((item, index) => {
            locatedEvidence = true;
            const quote = text(item.answer_quote);
            // 서버가 인용 검증 실패로 아니오를 매긴 항목(확인 불가)은 답변 위반이 아니라 검증 실패다 — 위치도 표시하지 않는다
            const unverifiable = Boolean(item.unverifiable) || item.reason_code === 'ITEM_UNVERIFIABLE';
            add({...base, kind: unverifiable ? 'verification' : 'semantic', id: `${prefix}:item:${index}`, itemId: text(item.id),
              title: `${cid} · ${text(item.id) || '점검 항목'}${unverifiable ? ' · 확인 불가' : ''}`,
              reason: item.reason || decision.reason || '', omission: !unverifiable && !quote && item.quote_check?.answer?.required === false},
              !unverifiable && quote ? claimMatches(answer, quote, projection) : []);
          });
        }
        if (!locatedEvidence) add({...base, id: `${prefix}:decision`,
          reason: policy ? joinReasons(decision.reason, decision.policy_original_assessment?.reason) : decision.reason || ''},
          policy || cid === 'J6' ? [] : genericQuotes(answer, decision));
      }
    }
    return {findings, segments: segmentsFor(answer, findings)};
  }

  const api = {locateFailures};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.AionaFailureLocations = api;
})(typeof window !== 'undefined' ? window : globalThis);
