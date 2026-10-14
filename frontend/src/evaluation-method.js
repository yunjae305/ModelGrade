/* 평가 방식 표시 — 저장된 실행이 기록한 의미 평가 방식만 읽는다. 현재 평가자 설정(SEMANTIC_PIPELINE)으로 과거 결과를 덮어 표시하지 않는다. */
((root) => {
  'use strict';
  const LABELS = {items: '항목별 의미 평가 (items)', legacy: '기존 의미 평가 (legacy)'};
  const known = (value) => typeof value === 'string' && Object.prototype.hasOwnProperty.call(LABELS, value);

  // 0순위: 서버가 저장된 실행에서 계산한 evaluation_method (평가 로그·검증 도구와 같은 계산 — 방식 기록이 생기기 전 실행은 legacy 추정)
  // 1순위: 평가 시작 때 기록한 evaluation_execution.pipeline · 2순위(그 기록이 없는 결과): 같은 실행의 평가 배치가 모두 같은 방식일 때 그 값
  function recordedPipeline(run) {
    if (known(run?.evaluation_method?.pipeline)) return run.evaluation_method.pipeline;
    const execution = run?.evaluation_execution?.pipeline;
    if (known(execution)) return execution;
    const values = new Set(Object.values(run?.evaluations || {}).map((batch) => batch?.jev_eval?.pipeline).filter(Boolean));
    const [only] = values;
    return values.size === 1 && known(only) ? only : null;
  }
  const count = (n) => Number(n).toLocaleString('ko-KR');
  // 서버 계산값이 있을 때만 붙이는 상세 — 기준 버전 · items 판 세부 기준·점검 항목 수 · 실제 판정 수 (저장 JSON·평가 로그와 같은 값)
  function details(run) {
    const m = run?.evaluation_method;
    if (!m || !known(m.pipeline)) return '';
    const parts = [];
    if (m.pipeline_source === 'inferred') parts.push('방식 기록 없음 — items 도입 전 실행으로 추정');
    if (m.criteria_version) parts.push('기준 ' + m.criteria_version);
    if (m.pipeline === 'items' && m.items_version) {
      parts.push(m.items_version + (m.catalog?.subs ? ` (세부 ${m.catalog.subs}·점검 ${m.catalog.items})` : ''));
    }
    const j = m.judged;
    if (j && Number.isInteger(j.answers)) {
      parts.push(`판정 답변 ${count(j.answers)}건 · 기준 판정 ${count(j.criteria)}건` + (Number.isInteger(j.items) ? ` · 점검 항목 판정 ${count(j.items)}건` : ''));
    }
    return parts.length ? ' · ' + parts.join(' · ') : '';
  }
  const label = (run) => '평가 방식: ' + (LABELS[recordedPipeline(run)] || '확인 불가') + details(run);
  // 평가자 설정 화면용 — 다음 실행에 쓰일 현재 설정. 저장된 결과의 방식과 따로 표시한다.
  const setupLabel = (setup) => '현재 평가자 설정: ' + (known(setup?.pipeline) ? LABELS[setup.pipeline] : '확인 불가');

  const api = {recordedPipeline, label, details, setupLabel};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.AionaEvaluationMethod = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
