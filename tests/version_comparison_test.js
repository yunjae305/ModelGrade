/* Exercise saved-run comparison without HTTP, storage, CLI, or model calls. */
'use strict';
const assert = require('node:assert/strict');
const {compare} = require('../frontend/src/version-comparison.js');
const clone = (value) => JSON.parse(JSON.stringify(value));

const profile = () => ({complete: true, fields: {
  engine_versions: {engine: 'code-v1', checks: {'1-1': 'v1'}},
  aiona_config: {config_hash: 'config-1', forbidden_entries: null, errors: []},
  semantic_version: 'semantic-v1', criteria_version: 'criteria-v1', applicability_version: 'apply-v1',
  history_policy: 'full_original_text', judge: {engine: 'codex_cli', requested_model: 'judge-v1', reasoning_effort: 'high'},
  judge_models: ['judge-v1']
}});
const question = (id, extra = {}) => ({case_id: id, question: '질문 ' + id, answer: '답변 ' + id, ...extra});
function run(id, sources, decisions = []) {
  const cases = sources.map((s) => ({...clone(s), case_id: String(s.case_id ?? s.id ?? s.question_id), case_status: 'EVALUATED',
    ...(s.conversation_id ? {conversation: {id: s.conversation_id, turn: s.turn}} : {}),
    results: [{area: 'common', category: '1', condition_id: 'nonempty', counted: true, verdict: 'PASS'}],
    summary: {common: {pass: 1, fail: 0, unjudged: 0}, by_category: {'1': {pass: 1, fail: 0, unjudged: 0}}}}));
  const records = sources.map((s, i) => ({case_id: cases[i].case_id, context: null,
    status: 'SUCCESS', criteria: ['J1'], decisions: {J1: {decision: decisions[i] || 'PASS'}}}));
  const result = {run_id: id, status: 'COMPLETED', question_count: sources.length, providers: ['aiona'],
    aiona_release: {version: id, collected_at: '2026-10-09', notes: ''}, evaluation_profile: profile(),
    datasets: {aiona: clone(sources)}, evaluations: {aiona: {cases, jev_eval: {records}}}};
  refreshContexts(result);
  return result;
}
function refreshContexts(result) {
  result.datasets.aiona.forEach((s, index) => {
    if (!s.conversation_id) return;
    const group = result.datasets.aiona.filter((r) => r.conversation_id === s.conversation_id);
    const prior = group.filter((r) => r.turn < s.turn);
    result.evaluations.aiona.jev_eval.records[index].context = {
      conversation_id: s.conversation_id, turn: s.turn, total_turns: Math.max(...group.map((r) => r.turn)),
      history_turns: prior.length, history_chars: prior.reduce((n, r) => n + Array.from(r.question).length + Array.from(r.answer).length, 0),
      history_complete: true, history_truncated: false, issues: []
    };
  });
}
const base = () => run('old', ['a', 'b', 'c', 'd'].map((s) => question(s)), ['FAIL', 'PASS', 'PASS', 'FAIL']);
const next = () => run('new', ['a', 'b', 'c', 'd'].map((s) => question(s, {answer: '새 답변 ' + s})), ['PASS', 'FAIL', 'PASS', 'FAIL']);

// Equal test conditions across two different product versions.
let a = base(), b = next(), c = compare(a, b);
assert.equal(c.eligible, true);
assert.deepEqual(c.delta, {pass: 0, fail: 0, rate: 0});
assert.deepEqual([c.counts.improved, c.counts.regressed, c.counts.passMaintained, c.counts.failMaintained], [1, 1, 1, 1]);
assert.equal(c.baseline.rate, .5);
assert.equal(c.target.rate, .5);
assert.ok(c.rows.every((r) => r.verified && r.answerChanged));
assert.equal(c.rows[0].changeLabel, '개선 · FAIL → PASS');
assert.equal(c.criteria.find((x) => x.id === 'J1').baseline.targets, 4);
assert.equal(c.criteria.find((x) => x.id === 'J1').delta, 0);
assert.equal(c.criteria.find((x) => x.id === 'J6').delta, null); // N/A is not zero accuracy.

// Rates use equal question weight, not number of CODE conditions or competitors.
b.evaluations.aiona.cases[0].summary.common.pass = 99;
b.datasets.gpt = [null]; b.evaluations.gpt = {cases: []}; b.providers.push('gpt');
b.status = 'ERROR'; // A rival may be incomplete while the AIONA batch is final.
assert.equal(compare(a, b).eligible, true);
assert.equal(compare(a, b).target.rate, .5);
b.evaluations.aiona.jev_eval.records[1].decisions.J1.decision = 'PASS';
assert.equal(compare(a, b).delta.rate, .25);

// Stored object property order is irrelevant; legitimate config nulls are retained.
b = next(); b.evaluation_profile.fields = Object.fromEntries(Object.entries(b.evaluation_profile.fields).reverse());
assert.equal(compare(a, b).eligible, true);
const before = JSON.stringify([a, b]);
function freeze(value) { if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); } return value; }
freeze(a); freeze(b); compare(a, b);
assert.equal(JSON.stringify([a, b]), before);

// items 파이프라인 이전에 저장된 프로필(pipeline 없음)은 legacy 와 같은 조건이고, items 는 legacy 와 다른 조건이다.
{
  const legacy = run('L1', [question('Q1')]), saved = run('L2', [question('Q1')]);
  Object.assign(legacy.evaluation_profile.fields, {pipeline: 'legacy', items_version: null, quote_policy: null, recheck_policy: null});
  assert.equal('pipeline' in saved.evaluation_profile.fields, false);
  assert.equal(compare(saved, legacy).profileMatched, true);
  assert.equal(compare(legacy, saved).eligible, true);
  const items = run('I1', [question('Q1')]);
  Object.assign(items.evaluation_profile.fields, {pipeline: 'items', items_version: 'semantic-items-v1', quote_policy: 'q-v1', recheck_policy: 'r-v1'});
  assert.equal(compare(items, legacy).profileMatched, false);
  assert.ok(compare(items, legacy).reasons.some(reason => reason.code === 'profile_mismatch'));
  const unknownItems = run('I2', [question('Q1')]);
  Object.assign(unknownItems.evaluation_profile.fields, {pipeline: 'items', items_version: null, quote_policy: null, recheck_policy: null});
  assert.equal(compare(items, unknownItems).eligible, false);   // items 인데 정책 버전을 모르면 비교하지 않는다
  const bothItems = run('I3', [question('Q1')]);
  Object.assign(bothItems.evaluation_profile.fields, {pipeline: 'items', items_version: 'semantic-items-v1', quote_policy: 'q-v1', recheck_policy: 'r-v1'});
  assert.equal(compare(items, bothItems).profileMatched, true);
}

// Unlimited search limits are known settings, not absent judge metadata. The
// exception is exact and top-level; equality must still distinguish a limit.
for (const keys of [['max_tool_calls'], ['evidence_recovery_max_tool_calls'], ['max_tool_calls', 'evidence_recovery_max_tool_calls']]) {
  const baseline = base(), target = next();
  keys.forEach(key => { baseline.evaluation_profile.fields.judge[key] = null; target.evaluation_profile.fields.judge[key] = null; });
  const snapshot = JSON.stringify([baseline, target]);
  const unlimited = compare(baseline, target);
  assert.equal(unlimited.eligible, true);
  assert.equal(unlimited.profileMatched, true);
  assert.ok(unlimited.rows.every(row => row.verified));
  assert.equal(JSON.stringify([baseline, target]), snapshot);
  keys.forEach(key => {
    const limited = clone(target);
    limited.evaluation_profile.fields.judge[key] = 4;
    for (const pair of [[baseline, limited], [limited, baseline]]) {
      const mismatch = compare(...pair);
      assert.equal(mismatch.eligible, false);
      assert.equal(mismatch.profileMatched, false);
      assert.equal(mismatch.delta, null);
      assert.ok(mismatch.reasons.some(reason => reason.code === 'profile_mismatch'));
      assert.ok(!mismatch.reasons.some(reason => reason.code === 'profile_unknown'));
    }
  });
}
for (const mutate of [
  judge => { judge.engine = null; },
  judge => { judge.requested_model = null; },
  judge => { judge.reasoning_effort = null; },
  judge => { judge.max_output_tokens = null; },
  judge => { judge.options = {max_tool_calls: null}; },
  judge => { judge.options = {evidence_recovery_max_tool_calls: null}; },
  judge => { delete judge.engine; },
  judge => { delete judge.requested_model; },
]) {
  const baseline = base(), target = next();
  for (const item of [baseline, target]) {
    Object.assign(item.evaluation_profile.fields.judge, {max_tool_calls: null, evidence_recovery_max_tool_calls: null});
    mutate(item.evaluation_profile.fields.judge);
  }
  const unknown = compare(baseline, target);
  assert.equal(unknown.eligible, false);
  assert.equal(unknown.profileMatched, false);
  assert.ok(unknown.reasons.some(reason => reason.code === 'profile_unknown'));
}

// Reordered single questions must use stable IDs, not array offsets.
a = base(); b = next();
const order = [3, 1, 0, 2];
for (const key of ['cases']) b.evaluations.aiona[key] = order.map((i) => b.evaluations.aiona[key][i]);
b.evaluations.aiona.jev_eval.records = order.map((i) => b.evaluations.aiona.jev_eval.records[i]);
b.datasets.aiona = order.map((i) => b.datasets.aiona[i]);
c = compare(a, b);
assert.equal(c.eligible, true);
assert.equal(c.rows[0].targetIndex, 2);
assert.equal(c.counts.improved, 1);

// Same ID with changed reference/question is separated, never used in total delta.
a = base(); b = next();
b.datasets.aiona[0].reference = {source: '새 참조'};
c = compare(a, b);
assert.equal(c.eligible, false); assert.equal(c.delta, null);
assert.equal(c.rows.length, 3); assert.equal(c.counts.improved, 0);
assert.deepEqual(c.changed[0].fields, ['reference']);
assert.equal(c.unmatched.baseline.length, 1); assert.equal(c.unmatched.target.length, 1);
assert.equal(c.criteria.find((x) => x.id === 'J1').matchedQuestions, 3);
b.datasets.aiona[0].question = '바뀐 질문'; b.evaluations.aiona.cases[0].question = '바뀐 질문';
assert.ok(compare(a, b).changed[0].fields.includes('question'));

// Renamed and duplicated IDs are not guessed from matching question text.
a = base(); b = next(); b.datasets.aiona[0].case_id = 'renamed'; b.evaluations.aiona.cases[0].case_id = 'renamed';
c = compare(a, b); assert.equal(c.rows.length, 3); assert.equal(c.counts.improved, 0); assert.equal(c.changed.length, 0);
b = next(); b.datasets.aiona[0].case_id = 'b'; b.evaluations.aiona.cases[0].case_id = 'b';
c = compare(a, b); assert.equal(c.rows.length, 2); assert.equal(c.eligible, false);
assert.equal(c.unmatched.target.filter((r) => r.code === 'duplicate_id').length, 2);
assert.equal(compare(b, a).rows.length, 2);
b = next(); delete b.datasets.aiona[0].case_id;
assert.equal(compare(a, b).unmatched.target[0].code, 'missing_id');

// Legacy aliases are valid identities; identical text without any ID is not.
a = run('a', [{id: 'legacy', question: '같은 질문', answer: '답변'}]);
b = run('b', [{question_id: 'legacy', question: '같은 질문', answer: '답변'}]);
assert.equal(compare(a, b).eligible, true);

// Complete prior question paths matter even if the current question is unchanged.
const conversation = () => [1, 2, 3].map((turn) => question('t' + turn, {conversation_id: 'chat', turn}));
a = run('old', conversation(), ['PASS', 'FAIL', 'FAIL']);
b = run('new', conversation(), ['PASS', 'PASS', 'PASS']);
b.datasets.aiona.forEach((s) => { s.answer = '새 답변'; });
b.evaluations.aiona.cases.forEach((s) => { s.answer = '새 답변'; });
refreshContexts(b);
assert.equal(compare(a, b).counts.improved, 2); // Prior answers may differ.
for (const field of ['cases']) b.evaluations.aiona[field].reverse();
b.datasets.aiona.reverse(); b.evaluations.aiona.jev_eval.records.reverse();
assert.equal(compare(a, b).eligible, true); // Turn order, not stored array order.
b.datasets.aiona[2].question = '이전 지시 변경'; b.evaluations.aiona.cases[2].question = '이전 지시 변경';
c = compare(a, b);
assert.equal(c.rows.length, 0); assert.equal(c.changed.length, 3);
assert.deepEqual(c.changed.find((r) => r.caseId === 't2').fields, ['history']);
assert.equal(c.counts.improved, 0); assert.equal(c.delta, null);
b = run('new', conversation()); b.datasets.aiona[1].turn = 1;
assert.equal(compare(a, b).rows.length, 0); // Duplicate turns invalidate the history.

// An incomplete FAIL record cannot turn into a verified improvement.
a = base(); b = next(); a.evaluations.aiona.jev_eval.records[0].status = 'API_ERROR';
c = compare(a, b);
assert.equal(c.eligible, false); assert.equal(c.delta, null); assert.equal(c.baseline.rate, null);
assert.equal(c.counts.improved, 0); assert.equal(c.counts.pending, 1);
assert.equal(c.rows[0].baselineState.complete, false);
a = base(); a.evaluations.aiona.cases[0].summary.common.unjudged = 1;
c = compare(a, next());
assert.equal(c.counts.pending, 0); // A confirmed semantic FAIL settles the question.
assert.equal(c.counts.improved, 1);
assert.equal(c.rows[0].baselineState.complete, true);
a = base(); a.evaluations.aiona.live = true;
assert.equal(compare(a, next()).counts.improved, 0);
assert.equal(compare(a, next()).eligible, false);

// Unknown profiles (including identical unknown values) are not equivalence proof.
for (const mutate of [
  (r) => { delete r.evaluation_profile; },
  (r) => { r.evaluation_profile.complete = false; },
  (r) => { r.evaluation_profile.fields.criteria_version = null; },
  (r) => { r.evaluation_profile.fields.criteria_version = 'unknown'; },
  (r) => { r.evaluation_profile.fields.judge.requested_model = ''; },
  (r) => { r.evaluation_profile.fields.judge.reasoning_effort = null; },
  (r) => { r.evaluation_profile.fields.judge_models = []; },
]) {
  a = base(); b = next(); mutate(a); mutate(b); c = compare(a, b);
  assert.equal(c.eligible, false); assert.equal(c.profileMatched, false);
  assert.equal(c.counts.improved, 0); assert.equal(c.counts.regressed, 0);
  assert.ok(c.reasons.some((r) => r.code === 'profile_unknown'));
  assert.equal(c.rows[0].change, 'unverified'); assert.equal(c.rows[0].observedChange, 'improved');
  assert.ok(c.criteria.every((r) => r.delta === null));
}
for (const mutate of [
  (r) => { r.evaluation_profile.fields.engine_versions.checks['1-1'] = 'v2'; },
  (r) => { r.evaluation_profile.fields.judge.reasoning_effort = 'low'; },
  (r) => { r.evaluation_profile.fields.judge_models = ['different']; },
  (r) => { r.evaluation_profile.fields.aiona_config.forbidden_entries = 1; },
]) {
  a = base(); b = next(); mutate(b); c = compare(a, b);
  assert.equal(c.profileMatched, false); assert.equal(c.delta, null);
  assert.equal(c.counts.improved, 0); assert.equal(c.counts.regressed, 0);
  assert.ok(c.reasons.some((r) => r.code === 'profile_mismatch'));
}

// Same run, missing run ID, stale source/result alignment and empty inputs are guarded.
a = base(); b = next(); b.run_id = a.run_id;
assert.equal(compare(a, b).eligible, false);

// Semantic records belong to stable IDs, not merely the array slot they occupy.
a = run('old', [question('q1'), question('q2')], ['FAIL', 'PASS']);
b = clone(a); b.run_id = 'new'; b.evaluations.aiona.jev_eval.records.reverse();
c = compare(a, b);
assert.equal(c.eligible, false); assert.equal(c.target.integrity, false);
assert.equal(c.counts.improved, 0); assert.equal(c.counts.regressed, 0);
assert.ok(c.reasons.some((r) => r.code === 'result_mismatch'));

// Removing copied originals cannot make an unjudged source edit look evaluated.
for (const field of ['case_id', 'question', 'answer']) {
  a = base(); b = next(); delete b.evaluations.aiona.cases[0][field];
  if (field === 'answer') b.datasets.aiona[0].answer = 'NEVER EVALUATED';
  c = compare(a, b);
  assert.equal(c.eligible, false, field); assert.equal(c.target.integrity, false, field);
  assert.equal(c.counts.improved, 0, field); assert.equal(c.delta, null, field);
}

// A completed semantic verdict with incomplete, truncated or misassigned history
// cannot prove improvement under the full-original-history evaluation policy.
for (const mutate of [
  (r) => { r.evaluations.aiona.jev_eval.records[1].context.turn = 1; },
  (r) => { r.evaluations.aiona.jev_eval.records[1].context.conversation_id = 'other'; },
  (r) => { r.evaluations.aiona.jev_eval.records[1].context.total_turns = 100; },
  (r) => { r.evaluations.aiona.jev_eval.records[1].context.history_turns = 0; },
  (r) => { r.evaluations.aiona.jev_eval.records[1].context.history_chars = 0; },
  (r) => { r.evaluations.aiona.jev_eval.records[1].context.history_complete = false; },
  (r) => { r.evaluations.aiona.jev_eval.records[1].context.history_truncated = true; },
  (r) => { r.evaluations.aiona.jev_eval.records[1].context.issues = ['missing prior turn']; },
  (r) => { delete r.evaluations.aiona.jev_eval.records[1].context; },
  (r) => { delete r.evaluations.aiona.cases[1].conversation; },
]) {
  a = run('old', conversation(), ['PASS', 'FAIL', 'FAIL']);
  b = run('new', conversation(), ['PASS', 'PASS', 'PASS']); mutate(b);
  c = compare(a, b);
  assert.equal(c.eligible, false); assert.equal(c.target.integrity, false);
  assert.equal(c.counts.improved, 0); assert.equal(c.delta, null);
}
const emoji = conversation(); emoji[0].answer = '운동 🏃🏽‍♀️ 😊';
assert.equal(compare(run('old', emoji), run('new', emoji)).eligible, true);
assert.equal(compare(a, b).counts.improved, 0);
delete b.run_id; assert.equal(compare(a, b).eligible, false);
b = next(); b.datasets.aiona[0].answer = '평가되지 않은 편집';
c = compare(a, b); assert.equal(c.eligible, false); assert.equal(c.counts.improved, 0);
b = next(); b.question_count = 5;
assert.equal(compare(a, b).eligible, false);
assert.equal(compare({}, {}).eligible, false);
assert.equal(compare(base(), null).baseline.rate, .5); // History cards can read a standalone summary.
b = next(); delete b.evaluations;
assert.equal(compare(a, b).eligible, false);
b = next(); b.evaluations.aiona.cases = [];
assert.equal(compare(a, b).eligible, false);

// Per-criterion rates show actual target counts; different applicability cannot
// become a claimed rate change, even when both aggregate target counts match.
a = base(); b = next();
a.evaluations.aiona.jev_eval.records[0].criteria.push('J5');
a.evaluations.aiona.jev_eval.records[0].decisions.J5 = {decision: 'FAIL'};
b.evaluations.aiona.jev_eval.records[1].criteria.push('J5');
b.evaluations.aiona.jev_eval.records[1].decisions.J5 = {decision: 'PASS'};
c = compare(a, b);
const j5 = c.criteria.find((r) => r.id === 'J5');
assert.equal(c.eligible, true);
assert.equal(j5.baseline.targets, 1); assert.equal(j5.target.targets, 1);
assert.equal(j5.baseline.rate, 0); assert.equal(j5.target.rate, 1);
assert.equal(j5.comparable, false); assert.equal(j5.delta, null);
console.log('Version comparison checks passed: identities, context, completeness, profiles, scope, denominators, and immutability.');

// Repeated tests of one model version are history, not a version improvement.
{
  const before = base(), after = next();
  after.aiona_release.version = before.aiona_release.version;
  const repeated = compare(before, after);
  assert.equal(repeated.eligible, false);
  assert.equal(repeated.delta, null);
  assert(repeated.reasons.some((r) => r.code === 'same_or_unknown_version'));
  assert(repeated.rows.every((r) => !r.verified));
  delete after.aiona_release;
  assert.equal(compare(before, after).eligible, false);
}

// 점수 단위(사용자 결정 10-13) — 멀티턴 대화는 1문항이고 한 턴이라도 FAIL 이면 FAIL. Codex 재현: 단일 1 + 3턴 대화
{
  const conv = (answerPrefix) => [question('s1', {answer: answerPrefix + 's1'}),
    ...[1, 2, 3].map((turn) => question('c' + turn, {conversation_id: 'C', turn, answer: answerPrefix + 'c' + turn}))];
  const before = run('u1', conv('이전 '), ['PASS', 'FAIL', 'FAIL', 'FAIL']);
  const after = run('u2', conv('이후 '), ['FAIL', 'PASS', 'PASS', 'FAIL']);
  const units = compare(before, after);
  assert.equal(units.baseline.total, 2);
  assert.equal(units.baseline.rate, .5);   // 단일 PASS · 대화 FAIL
  assert.equal(units.target.rate, 0);      // 단일 FAIL · 대화(3턴 FAIL) FAIL — 턴 단위였다면 25% → 50% '개선'
}

// 검증 한계(원문 확인 실패·근거 부족)로만 생긴 PASS→FAIL 은 악화가 아니라 따로 (10-14 P1-1, 서버 version_comparison 과 같은 규칙)
{
  const before = run('L-old', [question('a'), question('b')]), after = run('L-new', [question('a'), question('b')]);
  const limited = after.evaluations.aiona.jev_eval.records[0];
  limited.criteria = ['J1', 'J5'];
  limited.decisions.J5 = {decision: 'FAIL', reason_code: 'SOURCE_ORIGINAL_UNAVAILABLE', j5_outcome: {code: 'source_unavailable', factual_error_confirmed: false}};
  after.evaluations.aiona.jev_eval.records[1].decisions.J1.decision = 'FAIL';   // 실제 FAIL
  const result = compare(before, after);
  assert.deepEqual(result.rows.map((r) => r.change), ['limitedRegressed', 'regressed']);
  assert.equal(result.rows[0].changeLabel, '검증 한계로 FAIL · 악화 아님');
  assert.deepEqual([result.counts.regressed, result.counts.limitedRegressed, result.counts.limitedImproved], [1, 1, 0]);
  assert.equal(compare(after, before).rows[0].change, 'limitedImproved');
  limited.decisions.J5.j5_outcome.factual_error_confirmed = true;   // 사실 오류로 확정된 FAIL 은 그대로 악화
  assert.equal(compare(before, after).rows[0].change, 'regressed');
  console.log('verification-limited changes are kept apart from regressions.');
}
