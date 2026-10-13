/* 결과 화면의 평가 방식 표시 규칙 — 저장된 실행 기록만 읽는다; no API calls. */
'use strict';
const assert = require('node:assert/strict');
const {recordedPipeline, label, setupLabel} = require('../frontend/src/evaluation-method.js');

assert.equal(label({evaluation_execution: {pipeline: 'items'}}), '평가 방식: 항목별 의미 평가 (items)');
assert.equal(label({evaluation_execution: {pipeline: 'legacy'}}), '평가 방식: 기존 의미 평가 (legacy)');
assert.equal(label({}), '평가 방식: 확인 불가');
assert.equal(label(null), '평가 방식: 확인 불가');
assert.equal(label({evaluation_execution: {pipeline: 'fast'}}), '평가 방식: 확인 불가');
assert.equal(label({evaluation_execution: {pipeline: 'toString'}}), '평가 방식: 확인 불가');   // 객체 기본 속성 이름은 방식이 아니다
// 실행 기록이 우선 — 평가 배치 값이 달라도 실행 기록을 쓴다
assert.equal(recordedPipeline({evaluation_execution: {pipeline: 'items'}, evaluations: {aiona: {jev_eval: {pipeline: 'legacy'}}}}), 'items');
// 실행 기록이 없는 결과는 같은 실행의 평가 배치가 모두 같을 때만
const batches = (...values) => ({evaluations: Object.fromEntries(values.map((v, i) => ['m' + i, {jev_eval: v ? {pipeline: v} : {}}]))});
assert.equal(recordedPipeline(batches('items', 'items')), 'items');
assert.equal(recordedPipeline(batches('legacy', 'items')), null);
assert.equal(recordedPipeline(batches(null, null)), null);
// 현재 설정은 따로 표시하고 저장된 결과에 섞지 않는다
assert.equal(setupLabel({pipeline: 'items'}), '현재 평가자 설정: 항목별 의미 평가 (items)');
assert.equal(setupLabel({}), '현재 평가자 설정: 확인 불가');
assert.equal(label({evaluation_execution: {}, semantic_setup: {pipeline: 'items'}}), '평가 방식: 확인 불가');
// 서버 계산값(evaluation_method)이 있으면 그 방식·기준 버전·실제 판정 수를 그대로 — 저장 JSON·평가 로그와 같은 값
const items = {evaluation_method: {pipeline: 'items', pipeline_source: 'evaluation_execution', criteria_version: 'jev-criteria-v4',
  items_version: 'semantic-items-v2', catalog: {criteria: 7, subs: 27, items: 41}, judged: {answers: 272, criteria: 1034, items: 5120}}};
assert.equal(label(items), '평가 방식: 항목별 의미 평가 (items) · 기준 jev-criteria-v4 · semantic-items-v2 (세부 27·점검 41) · '
  + '판정 답변 272건 · 기준 판정 1,034건 · 점검 항목 판정 5,120건');
const legacy = {evaluation_method: {pipeline: 'legacy', pipeline_source: 'evaluation_execution', criteria_version: 'jev-criteria-v4',
  items_version: null, catalog: {criteria: 7, subs: null, items: null}, judged: {answers: 272, criteria: 1034, items: null}}};
assert.equal(label(legacy), '평가 방식: 기존 의미 평가 (legacy) · 기준 jev-criteria-v4 · 판정 답변 272건 · 기준 판정 1,034건');
// 방식 기록이 생기기 전 실행 — 서버와 같게 legacy 로 보이되 추정임을 밝힌다 (예전엔 화면만 '확인 불가')
const old = {evaluation_method: {pipeline: 'legacy', pipeline_source: 'inferred', criteria_version: 'jev-criteria-v3', items_version: null,
  catalog: {criteria: 6, subs: null, items: null}, judged: {answers: 10, criteria: 30, items: null}}};
assert.equal(recordedPipeline(old), 'legacy');
assert.ok(label(old).startsWith('평가 방식: 기존 의미 평가 (legacy) · 방식 기록 없음 — items 도입 전 실행으로 추정 · 기준 jev-criteria-v3'));
// 서버 계산값이 실행 기록보다 우선 (같은 저장 JSON 에서 계산하므로 보통 같다)
assert.equal(recordedPipeline({...items, evaluation_execution: {pipeline: 'legacy'}}), 'items');
console.log('evaluation method label checks passed');
