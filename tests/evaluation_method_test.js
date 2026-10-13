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
console.log('evaluation method label checks passed');
