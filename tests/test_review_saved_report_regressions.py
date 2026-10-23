"""Saved criteria remain historical, and failed automatic saves stay visible."""

from __future__ import annotations

import copy
import html
import json
from pathlib import Path

import pytest

from code_eval.combined import combined_summary
from code_eval.config import AionaConfig
from code_eval.engine import evaluate_batch
from code_eval.jev_eval import evaluate_jev_batch
from code_eval.report_html import _semantic_evidence, render_report

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def saved_legacy_batch():
    path = ROOT / 'results/set2_q100_claude_cli/results.json'
    if not path.is_file():
        pytest.skip('Optional private saved-report fixture is not included in public checkouts')
    return json.loads(path.read_text(encoding='utf-8'))


def _criteria_section(report):
    return report.split('<section aria-labelledby="jev-heading">', 1)[1].split('</section>', 1)[0]


@pytest.mark.parametrize('engine,label', [('anthropic_api', 'Claude API'), ('openai_api', 'OpenAI API')])
def test_api_evaluator_label_model_and_medium_reasoning_are_preserved(engine, label, saved_legacy_batch):
    batch = saved_legacy_batch
    semantic = batch['jev_eval']
    semantic.update(engine=engine, models=['claude-sonnet-5-5' if engine == 'anthropic_api' else 'offline-report-fixture'])
    semantic.pop('engine_label', None)
    semantic.setdefault('client', {}).update(requested_model=semantic['models'][0], reasoning_effort='medium')
    before = copy.deepcopy(batch)
    report = render_report(batch)
    assert f'이번 실행 · CODE + {label}' in report
    assert f'의미 평가는 {label}({semantic["models"][0]})로 판단했습니다.' in report
    assert '추론 수준은 medium입니다.' in report
    assert batch == before


def test_actual_v1_result_keeps_four_saved_definitions_when_rendered_again(saved_legacy_batch):
    batch = saved_legacy_batch
    before = copy.deepcopy(batch)
    report = render_report(batch)
    section = _criteria_section(report)
    assert 'Jev 의미 평가 기준 4개' in section and 'J1~J4 · Semantic Evaluation' in section
    assert 'jev-criteria-v1' in section
    assert '>J5</span>' not in section and '>J6</span>' not in section
    for criterion in batch["jev_eval"]["criteria"]:
        for field in ("question", "boundary", "example"):
            assert html.escape(criterion[field], quote=True) in section
    assert "적용 조건은 이 결과에 저장되지 않았습니다" in section
    assert "J5는 답변" not in section and "J6는 대화" not in section
    assert batch == before


def _batch():
    batch = evaluate_batch([{"case_id": "sample", "question": "Hello?", "answer": "Hello."}], AionaConfig())
    batch["jev_eval"] = evaluate_jev_batch(batch, None, access_reason="Offline test")
    batch["combined"] = combined_summary(batch)
    return batch


@pytest.mark.parametrize('checked,label', [(True, '확인됨'), (False, '확인하지 못함'), (None, '대조 기록 없음')])
def test_input_evidence_distinguishes_provided_material_from_external_fact(checked, label):
    quote = '<script>provided material</script>'
    record = {'decisions': {'J5': {'decision': 'PASS', 'fact_check': {'basis': 'input', 'claims': [{
        'claim': 'A summary matches the provided material', 'verdict': 'PASS', 'scope': 'input_consistency',
        'scope_quote': '<img> 자료만 요약해 주세요.', 'sources': [
            {'origin': origin, 'turn': 2 if origin == 'history_user' else None, 'quote': quote,
             'quote_verified': checked, 'verification_reason': '자료 자체의 사실성은 검증하지 않습니다.',
             'url': 'https://input.example/not-a-web-verification'}
            for origin in ('question', 'history_user', 'reference')]}]}}},
        'verification': {'search_performed': False, 'source_validation': 'provider'}}
    original = copy.deepcopy(record)
    report = _semantic_evidence(record)
    for origin in ('질문 원문', '이전 사용자 2턴', '참조 데이터'):
        assert '제공 자료 대조 · ' + origin in report
    assert '제공 자료 인용: ' + label in report
    assert '판정 범위: 제공 자료 일치 · 자료 자체의 사실성은 검증하지 않음' in report
    assert html.escape(quote) in report and '&lt;img&gt; 자료만 요약해 주세요.' in report
    assert '웹 검색 수행: 검색하지 않음' in report
    assert '출처 URL을 도구 기록과 대조' not in report and '<a ' not in report and '<script>' not in report
    assert record == original


def test_mixed_evidence_keeps_web_fact_and_input_consistency_scopes_separate():
    record = {'decisions': {'J5': {'decision': 'FAIL', 'fact_check': {'basis': 'mixed', 'claims': [
        {'claim': 'Input matches', 'verdict': 'PASS', 'scope': 'input_consistency', 'sources': [
            {'origin': 'question', 'quote': 'provided', 'quote_verified': True}]},
        {'claim': 'External fact is wrong', 'verdict': 'FAIL', 'scope': 'external_fact', 'sources': [
            {'origin': 'web', 'url': 'https://evidence.example/fact', 'quote': 'verified web text', 'quote_verified': True}]}]}}}}
    report = _semantic_evidence(record)
    assert '검증 기준: 제공 자료 일치와 외부 사실 확인 · 주장별 구분' in report
    assert '판정 범위: 외부 사실 확인' in report and '판정 범위: 제공 자료 일치' in report
    assert '제공 자료 인용: 확인됨' in report and '원문 인용: 확인됨' in report
    assert '<a href="https://evidence.example/fact"' in report


@pytest.mark.parametrize('criterion', ['J5', 'J6'])
def test_model_original_assessment_is_folded_and_distinct_from_final_verdict(criterion):
    record = {'decisions': {criterion: {'decision': None, 'status': 'UNVERIFIED', 'reason': '서버 검증 근거 부족',
        'model_original_assessment': {'choice': 'PASS', 'reason': '<script>모델 원래 설명</script>'}}}}
    original = copy.deepcopy(record)
    report = _semantic_evidence(record)
    assert '<details class="semantic-original-assessment">' in report
    assert '서버 근거 검증 전 모델 판정' in report and '모델 원판정: PASS' in report
    assert '서버 근거 검증 후 최종 판정: 확인 필요' in report
    assert '&lt;script&gt;모델 원래 설명&lt;/script&gt;' in report and '<script>' not in report
    assert '현재 결과와 점수에는 서버 근거 검증 후 최종 판정만 사용합니다.' in report
    assert record == original


def test_snapshot_names_descriptions_and_applicability_override_current_definitions():
    batch = _batch()
    snapshot = {"id": "J1", "name": "Saved name", "question": "Saved question <q>",
                "boundary": "Saved boundary", "example": "Saved example", "applies_when": "Saved applicability"}
    batch["jev_eval"]["criteria"] = [snapshot]
    batch["jev_eval"]["records"][0]["applicability"] = {}
    before = copy.deepcopy(batch)
    report = render_report(batch)
    section = _criteria_section(report)
    assert section.count('class="criterion-card jev"') == 1
    for value in snapshot.values():
        assert html.escape(value, quote=True) in section
    detail = report.split('id="case-sample"', 1)[1]
    assert 'J1 Saved name' in detail and 'Saved question &lt;q&gt;' in detail
    assert 'Saved name</span>' in report  # The area label also retains the saved name.
    assert batch == before


def test_missing_snapshot_uses_recorded_ids_and_labels_without_inventing_old_rules():
    batch = _batch()
    batch["jev_eval"].pop("criteria")
    batch["jev_eval"]["summary"] = {"J1": {"name": "Recorded only", "targets": 1, "pass": 0, "fail": 0}}
    report = render_report(batch)
    section = _criteria_section(report)
    assert "평가 당시 기준 정의가 저장되지 않은 결과" in section
    assert "현재 기준으로 대체하지 않습니다" in section
    assert "Recorded only" in section and "평가 당시 기준 설명이 저장되지 않았습니다" in section
    assert section.count('class="criterion-card jev"') == 1
    assert 'Saved applicability' not in section and '>J5</span>' not in section


def test_code_only_report_marks_current_definitions_as_reference_information():
    batch = _batch()
    batch.pop("jev_eval")
    assert "의미 평가 결과가 없어 현재 기준을 참고용으로 표시합니다" in _criteria_section(render_report(batch))


def test_html_save_failure_warning_is_visible_escaped_and_keeps_results():
    batch = _batch()
    batch["run"]["save_error"] = 'Permission denied <script>alert(1)</script>'
    report = render_report(batch)
    assert '<strong>자동 저장 실패</strong>' in report
    assert 'role="alert"' in report and "JSON을 직접 다운로드" in report
    assert 'Permission denied &lt;script&gt;alert(1)&lt;/script&gt;' in report
    assert '<script>' not in report and 'id="case-sample"' in report
    assert '평가 결과는 계속 확인할 수 있습니다' in report
    batch["run"].pop("save_error")
    assert '자동 저장 실패' not in render_report(batch)


