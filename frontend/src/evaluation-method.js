/* 평가 방식 표시 — 저장된 실행이 기록한 의미 평가 방식만 읽는다. 현재 평가자 설정(SEMANTIC_PIPELINE)으로 과거 결과를 덮어 표시하지 않는다. */
((root) => {
  'use strict';
  const LABELS = {items: '항목별 의미 평가 (items)', legacy: '기존 의미 평가 (legacy)'};
  const known = (value) => typeof value === 'string' && Object.prototype.hasOwnProperty.call(LABELS, value);

  // 1순위: 평가 시작 때 기록한 evaluation_execution.pipeline · 2순위(그 기록이 없는 결과): 같은 실행의 평가 배치가 모두 같은 방식일 때 그 값
  function recordedPipeline(run) {
    const execution = run?.evaluation_execution?.pipeline;
    if (known(execution)) return execution;
    const values = new Set(Object.values(run?.evaluations || {}).map((batch) => batch?.jev_eval?.pipeline).filter(Boolean));
    const [only] = values;
    return values.size === 1 && known(only) ? only : null;
  }
  const label = (run) => '평가 방식: ' + (LABELS[recordedPipeline(run)] || '확인 불가');
  // 평가자 설정 화면용 — 다음 실행에 쓰일 현재 설정. 저장된 결과의 방식과 따로 표시한다.
  const setupLabel = (setup) => '현재 평가자 설정: ' + (known(setup?.pipeline) ? LABELS[setup.pipeline] : '확인 불가');

  const api = {recordedPipeline, label, setupLabel};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.AionaEvaluationMethod = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
