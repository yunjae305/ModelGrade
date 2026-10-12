/* Regression checks for the production presentation rules; no API calls. */
'use strict';
const assert = require('node:assert/strict');
const {caseState, modelSummary, runSummary, finalSummary, questionOutcome, questionMatches, semanticEngineLabel, processingProgress} = require('../frontend/src/workspace-results.js');
assert.equal(semanticEngineLabel({engine:'anthropic_api'}), 'Claude API');
assert.equal(semanticEngineLabel({engine:'openai_api'}), 'OpenAI API');
assert.equal(semanticEngineLabel({engine:'anthropic_api',engine_label:'저장 당시 평가자'}), '저장 당시 평가자');
const source = (id = 'same') => ({case_id: id, question: 'question', answer: 'answer'});
const check = (verdict = 'PASS', status = 'EXECUTED') => ({area: 'common', counted: status === 'EXECUTED', verdict, status});
const code = (pass = 1, fail = 0, unjudged = 0) => ({case_status: 'EVALUATED', summary: {common: {pass, fail, unjudged}},
  results: [...Array.from({length: pass}, () => check()), ...Array.from({length: fail}, () => check('FAIL')),
    ...Array.from({length: unjudged}, () => check(null, 'UNSUPPORTED'))]});
const sem = (status = 'SUCCESS', decision = 'PASS', error = null) => ({status, criteria: ['J1'],
  decisions: status === 'SUCCESS' ? {J1: {decision}} : null, error});
function result(cases, records) {
  return {providers: ['aiona'], question_count: cases.length, datasets: {aiona: cases.map(() => source())},
    evaluations: {aiona: {cases, jev_eval: {records}}}};
}
let r = result([code(), code(1, 1), code(1, 0, 1)], [sem('API_ERROR', null, "You've hit your session limit"), sem('API_ERROR'), sem()]);
assert.equal(caseState(r, 'aiona', 0).outcome, 'pending');
assert.equal(caseState(r, 'aiona', 0).code.text, '통과');
assert.equal(caseState(r, 'aiona', 0).semantic.text, '호출 실패');
assert.match(caseState(r, 'aiona', 0).semantic.reason, /사용 한도/);
assert.equal(caseState(r, 'aiona', 1).outcome, 'issue');
assert.equal(caseState(r, 'aiona', 1).complete, true);  // FAIL settles the question; criteria remain visible separately
assert.equal(caseState(r, 'aiona', 1).criteriaComplete, false);
assert.equal(caseState(r, 'aiona', 2).outcome, 'pending');
assert.equal(caseState(r, 'aiona', 2).code.text, '일부 판정 불가');
assert.deepEqual(modelSummary(r, 'aiona', 3).counts, {issue: 1, clear: 0, pending: 2, waiting: 0, running: 0});
assert.equal(modelSummary(r, 'aiona', 3).criteriaComplete, 0);
assert.equal(questionMatches(r, ['aiona'], 1, 'issue'), true);
assert.equal(questionMatches(r, ['aiona'], 1, 'pending'), false);
assert.equal(runSummary(r).complete, 1);
assert.equal(runSummary(r).allComplete, false);

r = result([code(), code(), code(1, 1)], [sem(), sem('SUCCESS', 'FAIL'), sem()]);
assert.equal(caseState(r, 'aiona', 0).outcome, 'clear');
assert.equal(caseState(r, 'aiona', 1).outcome, 'issue');
assert.equal(caseState(r, 'aiona', 2).outcome, 'issue');
assert.equal(runSummary(r).allComplete, true); // completion and quality are independent
assert.equal(runSummary(r).complete, 3);
assert.deepEqual(runSummary(r).counts, {issue: 2, clear: 1, pending: 0, waiting: 0, running: 0});
assert.equal(questionOutcome(r, ['aiona'], 1), 'issue');
assert.equal(questionMatches(r, ['aiona'], 1, 'pending'), false);

// Final FAIL remains visible while a different criterion or model needs review.
const unverified = {status: 'SUCCESS', criteria: ['J1', 'J5'], decisions: {
  J1: {decision: 'FAIL'}, J5: {decision: null, status: 'UNVERIFIED', reason: '출처 확인 필요'}}};
const mixed = result([code(), code(1, 1)], [unverified, sem()]);
mixed.status = 'COMPLETED';
mixed.providers.push('gpt');
mixed.datasets.gpt = mixed.datasets.aiona.map((row) => ({...row, collection: {status: 'SUCCESS'}}));
mixed.evaluations.gpt = {cases: [code(), code()], jev_eval: {records: [sem(), {...unverified, decisions: {
  J1: {decision: 'PASS'}, J5: unverified.decisions.J5}}]}};
const mixedBefore = JSON.stringify(mixed);
for (const index of [0, 1]) {
  assert.equal(questionOutcome(mixed, mixed.providers, index), 'issue');
  assert.equal(questionMatches(mixed, mixed.providers, index, 'issue'), true);
  assert.equal(questionMatches(mixed, mixed.providers, index, 'pending'), false);
  assert.equal(questionMatches(mixed, mixed.providers, index, 'clear'), false);
}
assert.equal(questionMatches(mixed, ['aiona'], 1, 'pending'), false);
assert.equal(questionMatches(mixed, ['gpt'], 0, 'pending'), false);
assert.equal(questionMatches(mixed, ['gpt'], 1, 'pending'), true);
assert.equal(runSummary(mixed).counts.pending, 1);
assert.equal(finalSummary(mixed).incomplete, 1);
assert.equal(finalSummary(mixed).ready, false);
assert.ok(finalSummary(mixed).rows.every((row) => row.score === null));
assert.equal(JSON.stringify(mixed), mixedBefore);
const activeFail = result([code(1, 1)], [{status: 'RUNNING'}]);
activeFail.status = 'EVALUATING'; activeFail.evaluations.aiona.live = true;
assert.equal(questionMatches(activeFail, ['aiona'], 0, 'issue'), true);
assert.equal(questionMatches(activeFail, ['aiona'], 0, 'pending'), false);
activeFail.status = 'COMPLETED';
assert.equal(questionMatches(activeFail, ['aiona'], 0, 'pending'), false);
assert.equal(finalSummary(activeFail).ready, true);
assert.equal(finalSummary(activeFail).rows[0].score, 0);
assert.equal(caseState(activeFail, 'aiona', 0).criteriaComplete, false);

for (const status of ['NOT_CONFIGURED', 'TIMEOUT', 'INVALID_RESPONSE', 'SKIPPED', 'API_ERROR']) {
  assert.equal(caseState(result([code()], [sem(status)]), 'aiona', 0).outcome, 'pending', status);
}
for (const status of ['ERROR', 'CANCELLED']) {
  const interrupted = result([code(), code()], [sem(), null]);
  interrupted.status = status;
  interrupted.error = '서버 종료로 평가가 중단되었습니다.';
  interrupted.evaluations.aiona.live = true;
  assert.equal(caseState(interrupted, 'aiona', 0).complete, true);
  const pending = caseState(interrupted, 'aiona', 1);
  assert.equal(pending.outcome, 'pending');
  assert.equal(pending.semantic.text, '평가 중단');
  assert.equal(pending.semantic.rawReason, interrupted.error);
}
const partial = {status: 'SUCCESS', criteria: ['J1', 'J2'], decisions: {J1: {decision: 'PASS'}}};
assert.equal(caseState(result([code()], [partial]), 'aiona', 0).outcome, 'pending');
assert.equal(caseState(result([code()], [{...sem('SUCCESS', 'FAIL'), status: 'API_ERROR'}]), 'aiona', 0).outcome, 'pending');
assert.equal(caseState(result([code()], [{...sem(), decisions: {J1: {decision: 'PASS'}, J2: {decision: 'FAIL'}}}]), 'aiona', 0).outcome, 'clear');
assert.equal(caseState(result([code(0)], [sem()]), 'aiona', 0).outcome, 'pending');
assert.equal(caseState(result([code()], []), 'aiona', 0).outcome, 'pending');

r = result([code(), code()], [sem(), sem()]);
r.providers.push('gpt');
r.datasets.gpt = [{...source(), collection: {status: 'INCOMPLETE'}}, null];
r.evaluations.gpt = {cases: [{case_status: 'INPUT_ERROR'}, {case_status: 'INPUT_ERROR'}], jev_eval: {records: [sem('SKIPPED'), sem('SKIPPED')]}};
assert.equal(caseState(r, 'gpt', 0).outcome, 'pending');
assert.equal(caseState(r, 'gpt', 0).code.text, '평가 제외');
assert.equal(caseState(r, 'gpt', 1).outcome, 'pending');
assert.equal(questionOutcome(r, r.providers, 0), 'pending');
assert.equal(questionOutcome(r, ['aiona'], 0), 'clear');
assert.equal(runSummary(r).total, 4);
assert.equal(runSummary(r).complete, 2);
assert.equal(runSummary(r).allComplete, false);
const before = JSON.stringify(r);
runSummary(r);
assert.equal(JSON.stringify(r), before); // saved data and original text stay unchanged
delete r.evaluations;
assert.equal(caseState(r, 'aiona', 0).outcome, 'waiting');
assert.equal(modelSummary(r, 'aiona', 2).text, '평가 대기');
console.log('Result presentation regression checks passed.');

// Final score uses equal question weight, never the model's CODE condition count.
const ranking = result([code(1), code(1), code(1, 1)], [sem(), sem(), sem()]);
ranking.status = 'COMPLETED';
ranking.providers.push('gpt', 'claude');
for (const provider of ['gpt', 'claude']) {
  ranking.datasets[provider] = [1, 2, 3].map(() => ({...source(), collection: {status: 'SUCCESS'}}));
  ranking.evaluations[provider] = {cases: [code(30), code(1), code(1)], jev_eval: {records: [sem(), sem(), sem()]}};
}
const rankingBefore = JSON.stringify(ranking);
let overview = finalSummary(ranking);
assert.equal(overview.ready, true);
assert.deepEqual(overview.rows.map((r) => [r.provider, r.rank, r.score]), [['gpt', 1, 100], ['claude', 1, 100], ['aiona', 3, 2 / 3 * 100]]);
assert.equal(overview.leaders.length, 2);
assert.equal(overview.rows[0].tied, true);
assert.equal(JSON.stringify(ranking), rankingBefore);
ranking.status = 'EVALUATING';
assert.equal(finalSummary(ranking).ready, false);
assert.ok(finalSummary(ranking).rows.every((r) => r.score === null && r.rank === null));
ranking.status = 'COMPLETED';
ranking.evaluations.gpt.jev_eval.records[0] = sem('API_ERROR');
overview = finalSummary(ranking);
assert.equal(overview.incomplete, 1);
assert.equal(overview.ready, false);  // never silently shrink the shared denominator
assert.ok(overview.rows.every((r) => r.score === null));
ranking.evaluations.gpt.jev_eval.records[0] = sem();
ranking.datasets.gpt[0].question = 'different question';
assert.equal(finalSummary(ranking).comparable, false);
assert.equal(finalSummary(ranking).ready, false);
ranking.datasets.gpt[0].question = 'question';
ranking.datasets.gpt[0].conversation_id = 'different history';
assert.equal(finalSummary(ranking).ready, false);
delete ranking.datasets.gpt[0].conversation_id;
ranking.datasets.gpt.pop();
assert.equal(finalSummary(ranking).ready, false);
const solo = result([code()], [sem()]); solo.status = 'COMPLETED';
assert.equal(finalSummary(solo).ready, true);
assert.equal(finalSummary(solo).rows[0].rank, null);
assert.equal(finalSummary({...solo, question_count: 0}).ready, false);
console.log('Question score, exact ties, shared denominator, incomplete/mismatched inputs and single-model checks passed.');

r = result([code(), code(), code(), code()], [sem(), {status: 'RUNNING'}, null, sem('API_ERROR')]);
r.status = 'EVALUATING'; r.evaluations.aiona.live = true;
assert.equal(caseState(r, 'aiona', 0).outcome, 'clear');
assert.equal(caseState(r, 'aiona', 1).outcome, 'running');
assert.equal(caseState(r, 'aiona', 2).outcome, 'waiting');
assert.equal(caseState(r, 'aiona', 3).outcome, 'pending');
assert.equal(modelSummary(r, 'aiona', 4).semanticIncomplete, 1);
assert.equal(runSummary(r).complete, 1);
assert.deepEqual(runSummary(r).counts, {issue: 0, clear: 1, pending: 1, waiting: 1, running: 1});
assert.equal(modelSummary(r, 'aiona', 4).text, '평가 중');
r.status = 'ERROR'; r.evaluations.aiona.jev_eval.records[1] = null;
assert.equal(caseState(r, 'aiona', 1).outcome, 'pending');
assert.equal(modelSummary(r, 'aiona', 4).text, '평가 미완료');

// Per-criterion comparisons keep failures, missing judgments and N/A distinct.
const {criterionSummary, comparisonCriteria} = require('../frontend/src/workspace-results.js');
const j1 = comparisonCriteria.find((c) => c.id === 'J1');
const j6 = comparisonCriteria.find((c) => c.id === 'J6');
r = result([code(), code(), code(), code()], [sem(), sem('SUCCESS', 'FAIL'), sem('API_ERROR'), {status: 'SKIPPED', criteria: []}]);
let metric = criterionSummary(r, 'aiona', j1);
assert.deepEqual([metric.passed, metric.failed, metric.pending, metric.excluded, metric.rate], [1, 1, 1, 1, null]);
assert.equal(metric.targets, 3); // failed calls remain targets, never PASS or FAIL
assert.equal(criterionSummary(r, 'aiona', j6).rate, null);
assert.equal(criterionSummary(r, 'aiona', j6).targets, 0);
r.evaluations.aiona.jev_eval.records = [sem(), null, {status: 'RUNNING'}, sem('API_ERROR')];
metric = criterionSummary(r, 'aiona', j1);
assert.equal(metric.unknown, 2);
assert.equal(metric.pending, 1);
assert.equal(criterionSummary(r, 'gpt', j1).available, false);
r.evaluations.aiona.cases = [{case_status: 'EVALUATED', summary: {by_category: {'1': {pass: 2, fail: 1, unjudged: 1}}, specialized: {fail: 99}}},
  {case_status: 'INPUT_ERROR', summary: null}];
metric = criterionSummary(r, 'aiona', comparisonCriteria[0]);
assert.deepEqual([metric.passed, metric.failed, metric.pending, metric.excluded], [2, 1, 1, 1]);
assert.equal(metric.rate, null); // unfinished conditions cannot produce a settled pass rate
assert.deepEqual(metric.pendingReasons, {unverified: 0, errors: 0, missing: 0}); // CODE display is unchanged.

// Semantic pending reasons partition the existing total; no PASS/FAIL or N/A leakage.
const criterionRecords = [sem(), sem('SUCCESS', 'FAIL'),
  {...sem('SUCCESS', null), decisions: {J1: {decision: null, status: 'UNVERIFIED'}}},
  sem('API_ERROR'), {...sem('INVALID_RESPONSE'), decisions: {J1: {decision: null, status: 'UNVERIFIED'}}},
  {...sem(), decisions: {}}, {status: 'SUCCESS', criteria: [], decisions: {J1: {decision: null, status: 'NOT_APPLICABLE'}}},
  {status: 'SKIPPED', criteria: ['J1']}, null, {status: 'RUNNING', criteria: ['J1']}];
r = result(criterionRecords.map(() => code()), criterionRecords);
const criterionBefore = JSON.stringify(r);
metric = criterionSummary(r, 'aiona', j1);
assert.deepEqual(metric.pendingReasons, {unverified: 1, errors: 2, missing: 1});
assert.equal(Object.values(metric.pendingReasons).reduce((a, b) => a + b, 0), metric.pending);
assert.deepEqual([metric.pending, metric.targets, metric.passed, metric.failed, metric.rate, metric.unknown, metric.excluded],
  [4, 6, 1, 1, null, 2, 1]);
assert.equal(JSON.stringify(r), criterionBefore);
for (const status of ['API_ERROR', 'TIMEOUT', 'INVALID_RESPONSE', 'NOT_CONFIGURED', 'INTERRUPTED']) {
  metric = criterionSummary(result([code()], [sem(status)]), 'aiona', j1);
  assert.deepEqual(metric.pendingReasons, {unverified: 0, errors: 1, missing: 0});
}

// Processing totals must not become quality-completion totals at run end.
const held = {...sem(), review_required: true, decisions: {J1: {decision: null, status: 'UNVERIFIED'}}};
r = result(Array.from({length: 7}, () => code()), [sem(), held, sem('INVALID_RESPONSE'),
  {status: 'SKIPPED'}, null, {status: 'RUNNING'}, {status: 'INTERRUPTED'}]);
const originalProgressInput = JSON.stringify(r);
let processed = processingProgress(r);
assert.deepEqual(processed, {code_done: 7, code_total: 7, semantic_done: 4, semantic_total: 7,
  semantic_success: 1, semantic_unverified: 1, semantic_errors: 1, skipped: 1});
assert.equal(JSON.stringify(r), originalProgressInput); // saved input is immutable
delete r.evaluations.aiona.jev_eval.records[1].review_required;
assert.deepEqual(processingProgress(r), processed); // old results can omit the aggregate review flag
r.evaluations.aiona.jev_eval.records[1].review_required = true;
r.status = 'COMPLETED';
assert.deepEqual(processingProgress(r), processed); // missing work is never invented
assert.equal(runSummary(r).complete, 1);
r.evaluation_progress = {aiona: {code_done: 7, code_total: 7, semantic_done: 5, semantic_total: 7,
  semantic_success: 2, semantic_unverified: 1, semantic_errors: 1, skipped: 1}};
assert.equal(processingProgress(r).semantic_done, 5); // live snapshot may precede record materialization
assert.equal(processingProgress(r).semantic_success, 2);
for (const status of ['EVALUATING', 'COMPLETED', 'ERROR', 'CANCELLED']) {
  r.status = status;
  assert.equal(processingProgress(r).semantic_done, 5);
}
const previousGeneration = {...r, evaluation_progress: {aiona: {...r.evaluation_progress.aiona}}};
r.evaluation_progress.aiona.semantic_done = 0;
r.evaluation_progress.aiona.semantic_success = 0;
assert.equal(processingProgress(r).semantic_done, 0); // explicit new evaluation starts at zero
assert.equal(processingProgress(previousGeneration).semantic_done, 5);
r.providers.push('gpt');
r.evaluation_progress.gpt = {...previousGeneration.evaluation_progress.aiona, semantic_done: 3,
  semantic_success: 1, semantic_unverified: 1, semantic_errors: 1, skipped: 0};
assert.equal(processingProgress(r).semantic_total, 14);
assert.equal(processingProgress(r).semantic_done, 3);
assert.equal(processingProgress(r, 'gpt').semantic_done, 3);
console.log('Processing totals remain distinct from verdict completion, preserve terminal/cancelled counts and support legacy records.');

// Missing work never renders a partial 100%; the original rate returns only
// when all applicable targets have been judged. No unknown is converted to FAIL.
const {resolvedBatch, settledRate, semanticState} = require('../frontend/src/workspace-results.js');
assert.equal(settledRate(235, 0, 2), null);
assert.equal(settledRate(235, 0, 0), 1);
assert.equal(settledRate(235, 1, 0), 235 / 236);
assert.equal(settledRate(0, 0, 2), null);
assert.equal(resolvedBatch({cases: [code(235, 0, 2)]}).summary.common.pass_rate, null);
assert.equal(resolvedBatch({cases: [code(235)]}).summary.common.pass_rate, 1);
r = result([code(), code()], [sem()]);
metric = criterionSummary(r, 'aiona', j1);
assert.equal(metric.unknown, 1);
assert.equal(metric.rate, null);
r.evaluations.aiona.jev_eval.records.push(sem());
assert.equal(criterionSummary(r, 'aiona', j1).rate, 1);
const verbose = '정보 신뢰도 확인 필요: 인용문은 지정한 사용자 원문과 일치하지만, 현재 요청을 자료 요약·추출·변환 범위로 확인하지 못했습니다. '.repeat(10);
const rawHeld = {status: 'SUCCESS', criteria: ['J1', 'J5'], decisions: {
  J1: {decision: 'PASS'}, J5: {decision: null, status: 'UNVERIFIED', reason: verbose}}};
const concise = semanticState(rawHeld);
assert.equal(concise.reason, 'J5 · 자료 대조 범위 확인');
assert.equal(concise.rawReason, 'J5: ' + verbose);
assert.equal(rawHeld.decisions.J5.reason, verbose);
for (const status of ['EVALUATING', 'COMPLETED', 'ERROR', 'CANCELLED']) {
  r = result([code(), code(), code(1, 1), code(), code()], [sem(), rawHeld, rawHeld, null, {status: 'RUNNING'}]);
  r.status = status; r.evaluations.aiona.live = true;
  const summary = modelSummary(r, 'aiona', r.question_count);
  assert.equal(Object.values(summary.counts).reduce((sum, value) => sum + value, 0), r.question_count);
  assert.equal(summary.counts.issue, 1);
  assert.equal(caseState(r, 'aiona', 2).pending, false);
  assert.equal(caseState(r, 'aiona', 2).criteriaComplete, false);
}
// A cancelled request is an interruption, not an empty input; waiting rows during a run do not ask to start.
assert.equal(caseState(result([code()], [sem('SKIPPED', null, '평가 중단 요청으로 의미평가를 완료하지 못했습니다.')]), 'aiona', 0).semantic.text, '평가 중단');
assert.equal(caseState(result([code()], [sem('SKIPPED', null, 'CODE 입력 검증 결과 INPUT_ERROR — 평가할 질문·답변이 없음')]), 'aiona', 0).semantic.text, '평가 제외');
r = result([code()], [null]); delete r.evaluations; r.status = 'EVALUATING';
assert.doesNotMatch(caseState(r, 'aiona', 0).detail, /시작해 주세요/);
r.status = 'READY';
assert.match(caseState(r, 'aiona', 0).detail, /시작해 주세요/);
// CODE conditions still unresolved after a finished semantic call name the completion failure.
r = result([code(1, 0, 1), code(1, 0, 1)], [{...sem(), code_completion: {status: 'INVALID_RESPONSE', error: '공통 보완 판정의 근거가 없습니다'}}, sem()]);
assert.equal(caseState(r, 'aiona', 0).outcome, 'pending');
assert.equal(caseState(r, 'aiona', 0).code.pendingReason, '코드 보완 판정 실패');
assert.equal(caseState(r, 'aiona', 0).detail, '코드 보완 판정 실패 · 공통 보완 판정의 근거가 없습니다');
assert.equal(caseState(r, 'aiona', 1).code.pendingReason, '코드 보완 판정 기록 없음');
assert.equal(modelSummary(r, 'aiona', 2).codeIncomplete, 2);
assert.equal(modelSummary(r, 'aiona', 2).semanticIncomplete, 0);
console.log('Exclusive question outcomes, short reasons with intact originals and unsettled rates passed.');

// Case-level check errors (new engine) keep the question unsettled and name the cause; new records carry no completion wording.
r = result([code(1, 0, 0), code(1, 0, 0)], [sem(), sem()]);
r.evaluations.aiona.cases[0].errors = [{check_id: '3-1.check_error', kind: 'CHECK_ERROR', message: 'x'}];
assert.equal(caseState(r, 'aiona', 0).outcome, 'pending');
assert.equal(caseState(r, 'aiona', 0).code.text, '검사 실행 오류');
assert.equal(caseState(r, 'aiona', 0).code.pendingReason, '코드 검사 실행 오류');
assert.equal(caseState(r, 'aiona', 0).detail, '3-1.check_error · x');
assert.match(caseState(r, 'aiona', 0).code.reason, /검사 실행 오류 1건/);
assert.equal(caseState(r, 'aiona', 1).outcome, 'clear');
assert.equal(caseState(r, 'aiona', 1).code.pendingReason, '');
assert.doesNotMatch(caseState(r, 'aiona', 1).code.reason, /보완|미완료/);
assert.equal(modelSummary(r, 'aiona', 2).codeIncomplete, 1);
assert.equal(modelSummary(r, 'aiona', 2).codeErrors, 1);
const liveNew = result([code(3, 0, 0)], [{status: 'RUNNING'}]);
liveNew.status = 'EVALUATING'; liveNew.evaluations.aiona.live = true;
assert.equal(caseState(liveNew, 'aiona', 0).outcome, 'running');
assert.notEqual(caseState(liveNew, 'aiona', 0).code.text, '보완 평가 대기');
console.log('Case-level check errors and completion-free new records passed.');
