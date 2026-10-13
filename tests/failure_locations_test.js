/* Saved evidence only: no DOM, HTTP, model calls, or persisted-result changes. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {locateFailures} = require('../frontend/src/failure-locations.js');
const clone = value => JSON.parse(JSON.stringify(value));
const semantic = (decisions, extra = {}) => ({status: 'SUCCESS', criteria: Object.keys(decisions), decisions, ...extra});
const code = (extra = {}) => ({area: 'common', counted: true, verdict: 'FAIL', title: 'Fixture code criterion', reason: 'Fixture failure', ...extra});
const j5 = claims => semantic({J5: {decision: 'FAIL', name: '데이터 신뢰도', reason: 'The failed claims contradict the evidence.', fact_check: {claims}}});
const claim = (value, verdict = 'FAIL', extra = {}) => ({claim: value, verdict, reason: verdict + ' evidence', ...extra});
const quoted = (answer, result, kind) => result.findings.filter(finding => !kind || finding.kind === kind)
  .flatMap(finding => finding.spans.map(span => answer.slice(span.start, span.end)));
function verifyPartition(answer, result) {
  assert.equal(result.segments.map(segment => answer.slice(segment.start, segment.end)).join(''), answer);
  let end = 0;
  for (const segment of result.segments) {
    assert.equal(segment.start, end);
    assert.ok(segment.end > segment.start && segment.end <= answer.length);
    assert.ok(segment.findingIds.every(id => result.findings.some(finding => finding.id === id)));
    end = segment.end;
  }
  assert.equal(end, answer.length);
}

{
  const answer = 'Confirmed good. Wrong fact. Still unknown.';
  const record = j5([claim('Confirmed good.', 'PASS'), claim('Wrong fact.'), claim('Still unknown.', 'UNVERIFIED')]);
  const original = clone(record);
  const result = locateFailures(answer, {results: []}, record);
  assert.equal(result.findings.length, 1);
  assert.deepEqual(quoted(answer, result), ['Wrong fact.']);
  assert.equal(result.findings[0].id, 'semantic:J5:claim:1');
  assert.equal(result.findings[0].kind, 'semantic');
  assert.deepEqual(record, original);
  verifyPartition(answer, result);
}

{
  const answer = 'First unavailable. A provenance gap. Second unavailable.';
  const record = j5([claim('First unavailable.', 'UNVERIFIED'), claim('A provenance gap.', 'UNVERIFIED'), claim('Second unavailable.', 'UNVERIFIED')]);
  Object.assign(record.decisions.J5, {reason_code: 'SOURCE_ORIGINAL_UNAVAILABLE', reason: '출처 원문 확인 실패로 검증 기준 미충족',
    decision_policy: {failures: [{claim_index: 0}, {claim_index: 2}, {claim_index: 2}]}});
  const result = locateFailures(answer, null, record);
  assert.deepEqual(quoted(answer, result), ['First unavailable.', 'Second unavailable.']);
  assert.ok(result.findings.every(finding => finding.kind === 'verification' && finding.title.includes('검증 기준 미충족')));
  assert.equal(result.findings.length, 2);
  const generic = clone(record);
  generic.decisions.J5.reason_code = 'EVALUATION_EVIDENCE_INSUFFICIENT';
  assert.equal(locateFailures(answer, null, generic).findings.length, 3);
  delete record.decisions.J5.decision_policy;
  const noIndex = locateFailures(answer, null, record);
  assert.equal(noIndex.findings.length, 1);
  assert.deepEqual(noIndex.findings[0].spans, []);
  assert.ok(noIndex.findings[0].locationNote);
}

{
  const answer = '🙂\r\nAlpha  is\tblue.\nAlpha is blue.\n';
  const result = locateFailures(answer, null, j5([claim('Alpha is blue.')]));
  assert.deepEqual(quoted(answer, result), ['Alpha  is\tblue.', 'Alpha is blue.']);
  assert.equal(result.findings[0].spans[0].start, 4);
  verifyPartition(answer, result);
  assert.deepEqual(locateFailures('caf\u00e9', null, j5([claim('cafe\u0301')])).findings[0].spans, []);
}

{
  const answer = '🙂x BAD then BAD';
  const codeCase = {answer, results: [code({evidence: [{target: 'answer', start: 3, end: 6, excerpt: 'BAD'}],
    explain: {locations: [{kind: 'snippet', line: 1, hit: 'BAD'}]}})]};
  const result = locateFailures(answer, codeCase, null);
  assert.deepEqual(result.findings[0].spans, [{start: 4, end: 7}]);
  assert.equal(result.findings[0].codeIndex, 0);
  const mismatch = clone(codeCase);
  mismatch.answer = 'a different answer';
  assert.deepEqual(locateFailures(answer, mismatch, null).findings[0].spans, []);
  const badExcerpt = clone(codeCase);
  badExcerpt.results[0].evidence[0].excerpt = 'GOOD';
  badExcerpt.results[0].explain = {};
  assert.deepEqual(locateFailures(answer, badExcerpt, null).findings[0].spans, []);
  verifyPartition(answer, result);
}

{
  const answer = 'Python code:\r\ndef f():\r\n return (\r\nEnd';
  const codeCase = {answer, results: [code({explain: {locations: [{kind: 'code', lines: [
    {answer_line: 2, text: 'def f():', mark: false}, {answer_line: 3, text: ' return (', mark: true}
  ]}]}})]};
  const result = locateFailures(answer, codeCase, null);
  assert.deepEqual(quoted(answer, result), [' return (']);
  codeCase.results[0].explain.locations[0].lines[1].answer_line = 2;
  assert.deepEqual(locateFailures(answer, codeCase, null).findings[0].spans, []);
  verifyPartition(answer, result);
}

{
  const answer = 'Correct first sentence. Correct second sentence.';
  const result = locateFailures(answer, {answer, results: [code({evidence: [{target: 'question', start: 0, end: 7, excerpt: 'Correct'}],
    explain: {locations: [{kind: 'list', label: '기대한 결과 (원본에서 직접 계산)', items: ['Correct'], marks: [0]},
      {kind: 'list', label: '답변의 결과', items: ['Correct first sentence.', 'Correct second sentence.'], marks: []},
      {kind: 'snippet', target: 'question', line: 1, hit: 'Correct'}]}})]}, null);
  assert.equal(result.findings.length, 1);
  assert.deepEqual(quoted(answer, result), []);
  assert.ok(result.findings[0].locationNote);
}

{
  const answer = 'Matching history instruction. Wrong current answer. Good current answer.';
  const record = semantic({J6: {decision: 'FAIL', context_checks: [
    {decision: 'FAIL', instruction_quote: 'Matching history instruction.', answer_quote: 'Wrong current answer.', reason: 'Context contradiction'},
    {decision: 'PASS', answer_quote: 'Good current answer.'},
    {decision: 'UNVERIFIED', answer_quote: 'Matching history instruction.'}
  ]}});
  const result = locateFailures(answer, null, record);
  assert.deepEqual(quoted(answer, result), ['Wrong current answer.']);
  record.decisions.J6.context_checks[0].answer_quote = '';
  assert.deepEqual(locateFailures(answer, null, record).findings[0].spans, []);
}

{
  const answer = 'Source quotation repeated. A genuinely wrong phrase. Good fact repeated. QA.';
  const record = semantic({J3: {decision: 'FAIL', reason: '질문은 “Source quotation repeated.”를 요구했지만 답변의 “A genuinely wrong phrase.”는 모순입니다. “QA”도 있습니다.'}});
  assert.deepEqual(quoted(answer, locateFailures(answer, null, record)), ['A genuinely wrong phrase.']);
  const source = j5([{...claim('Good fact repeated.', 'PASS'), sources: [{quote: 'Source quotation repeated.'}]}]);
  source.decisions.J5.reason = '“Source quotation repeated.”와 “Good fact repeated.”를 확인했습니다.';
  assert.deepEqual(quoted(answer, locateFailures(answer, null, source)), []);
  const expected = '서울은 대한민국의 수도입니다.';
  for (const prefix of ['정답은 ', '기대 결과는 ', '질문에서 요구한 내용은 ', 'The expected answer is ']) {
    assert.deepEqual(quoted(expected, locateFailures(expected, {answer: expected}, semantic({J3: {
      decision: 'FAIL', reason: prefix + '‘서울은 대한민국의 수도입니다’이어야 합니다.'
    }}))), []);
  }
}

{
  const answer = 'abcdefghij';
  const record = j5([claim('abcdefgh'), claim('defghij')]);
  const codeCase = {answer, results: [code({evidence: [{target: 'answer', start: 3, end: 5, excerpt: 'de'}]}),
    code({verdict: 'PASS'}), code({counted: false}), code({area: 'specialized'})]};
  const result = locateFailures(answer, codeCase, record);
  assert.deepEqual(result.findings.map(finding => finding.id), ['code:0', 'semantic:J5:claim:0', 'semantic:J5:claim:1']);
  assert.deepEqual(result.segments.map(segment => [answer.slice(segment.start, segment.end), segment.findingIds.length]),
    [['abc', 1], ['de', 3], ['fgh', 2], ['ij', 1]]);
  verifyPartition(answer, result);
}

{
  const answer = '<script>alert("xss")</script> 🙂 wrong';
  const result = locateFailures(answer, null, j5([claim('<script>alert("xss")</script>')]));
  assert.deepEqual(quoted(answer, result), ['<script>alert("xss")</script>']);
  assert.equal(Object.keys(result).join(','), 'findings,segments');
  assert.equal(JSON.stringify(result).includes('<mark'), false);
  verifyPartition(answer, result);
  const empty = locateFailures('', {results: [code()]}, j5([claim('Absent original')]));
  assert.equal(empty.findings.length, 2);
  assert.deepEqual(empty.segments, []);
  assert.ok(empty.findings.every(finding => finding.spans.length === 0 && finding.locationNote));
}

{
  const answer = 'Failed statement repeats. Failed statement repeats.';
  assert.equal(locateFailures(answer, null, j5([claim('Failed statement repeats.')])).findings[0].spans.length, 2);
  const record = j5([claim('Failed statement repeats.')]);
  for (const status of ['RUNNING', 'API_ERROR', 'TIMEOUT', 'PENDING']) {
    assert.deepEqual(locateFailures(answer, null, {...record, status}).findings, []);
  }
  assert.deepEqual(locateFailures(answer, null, {...record, criteria: []}).findings, []);
  assert.deepEqual(locateFailures(answer, null, {...record, not_requested: ['J5']}).findings, []);
  const mismatch = locateFailures('😀 new', {answer: '😀 bad', results: [code({explain: {
    locations: [{kind: 'snippet', line: 1, hit: 'new'}]
  }})]}, j5([claim('😀 new')]));
  assert.ok(mismatch.findings.every(finding => finding.spans.length === 0 && finding.locationNote.includes('현재 원문이 달라')));
}

// Read-only verification of the reported saved case; no fixture is written back.
const actualPath = path.join(__dirname, '../data/comparison_runs/ad9bc5c843b1.json');
{
  // Policy FAILs keep the policy sentence and also expose the evaluator's concrete reason.
  const answer = 'Only claim.';
  const record = j5([claim('Only claim.', 'UNVERIFIED', {reason: '공식 문서에서 수치를 찾지 못함'})]);
  Object.assign(record.decisions.J5, {reason_code: 'EVALUATION_EVIDENCE_INSUFFICIENT', reason: '평가 근거 부족으로 검증 기준 미충족'});
  const located = locateFailures(answer, null, record).findings[0];
  assert.equal(located.reason, '평가 근거 부족으로 검증 기준 미충족 · 공식 문서에서 수치를 찾지 못함');
  const j2 = semantic({J2: {decision: 'FAIL', reason_code: 'EVALUATION_EVIDENCE_INSUFFICIENT', reason: '평가 근거 부족으로 검증 기준 미충족',
    policy_original_assessment: {decision: 'UNVERIFIED', reason: '기준 시점 불명확'}}});
  assert.equal(locateFailures(answer, null, j2).findings[0].reason, '평가 근거 부족으로 검증 기준 미충족 · 기준 시점 불명확');
}

if (fs.existsSync(actualPath)) {
  const bytes = fs.readFileSync(actualPath);
  const run = JSON.parse(bytes), batch = run.evaluations.claude;
  const index = batch.cases.findIndex(item => item.case_id === 'Q1_T1');
  assert.ok(index >= 0);
  const actualCase = batch.cases[index], record = batch.jev_eval.records[index];
  const result = locateFailures(actualCase.answer, actualCase, record);
  const confirmed = result.findings.filter(finding => finding.kind === 'semantic' && finding.criterionId === 'J5');
  assert.equal(confirmed.length, 2);
  assert.ok(confirmed.every(finding => finding.spans.length === 1));
  assert.deepEqual(quoted(actualCase.answer, {findings: confirmed}), record.decisions.J5.fact_check.claims.filter(item => item.verdict === 'FAIL').map(item => item.claim));
  assert.deepEqual(fs.readFileSync(actualPath), bytes);
  verifyPartition(actualCase.answer, result);
}
console.log('failure location tests passed');
{
  // A cited source that does not support its sentence is located at that sentence, labelled as a source mismatch.
  const answer = 'Alpha is blue [1]. Beta is 42.\n\n[1] https://cited.example/alpha';
  const record = semantic({J5: {decision: 'FAIL', name: '데이터 신뢰도', citation_mismatch: true,
    reason: '답변이 든 출처가 주장을 뒷받침하지 않음', fact_check: {basis: 'none', claims: []},
    citation_checks: [{url: 'https://cited.example/alpha', claim: 'Alpha is blue [1].', verdict: 'unrelated', reason: '야구 기사',
      note: '평가자 판단 — 서버는 출처 원문 수신만 확인했습니다.'}]}});
  const result = locateFailures(answer, null, record);
  assert.deepEqual(quoted(answer, result), ['Alpha is blue [1].']);
  assert.equal(result.findings[0].title, 'J5 · 출처 불일치');
  verifyPartition(answer, result);
  console.log('Source-mismatch findings are located at the cited sentence.');
}
