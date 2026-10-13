"""질문 단위 평가 요약(ComparisonService._evaluation_summary) — 이력·결과 화면이 쓰는 판정·미완료 집계. 모델 호출 없음."""
import copy

import pytest

from webapp.comparison_service import ComparisonService


def test_pass_summary_requires_all_code_and_semantic_verdicts():
    case = {'case_status': 'EVALUATED', 'results': [], 'summary': {'common': {'pass': 1, 'fail': 0, 'unjudged': 0}}}
    record = {'status': 'SUCCESS', 'criteria': ['J1'], 'decisions': {'J1': {'decision': 'PASS'}}}
    run = {'question_count': 1, 'status': 'COMPLETED', 'evaluations': {'aiona': {'cases': [case], 'jev_eval': {'records': [record]}}}}
    assert ComparisonService._evaluation_summary(run)['scored'] == 1
    record['decisions']['J1']['decision'] = None
    assert ComparisonService._evaluation_summary(run)['incomplete'] == 1
    record['decisions']['J1']['decision'] = 'PASS'
    case['summary']['common']['unjudged'] = 1
    assert ComparisonService._evaluation_summary(run)['incomplete'] == 1
    record.update(review_required=True)
    assert ComparisonService._evaluation_summary(run)['review_required'] == 1


@pytest.mark.parametrize('failure', ['code_summary', 'code_result', 'semantic'])
def test_failed_question_is_scored_once_while_unknown_criteria_remain_auditable(failure):
    case = {'case_status': 'EVALUATED', 'results': [],
            'summary': {'common': {'pass': 1, 'fail': 0, 'unjudged': 1}}}
    record = {'status': 'SUCCESS', 'criteria': ['J1', 'J5'], 'review_required': True,
              'decisions': {'J1': {'decision': 'PASS'},
                            'J5': {'decision': None, 'status': 'UNVERIFIED'}}}
    if failure == 'code_summary':
        case['summary']['common']['fail'] = 1
    elif failure == 'code_result':
        case['results'] = [{'area': 'common', 'counted': True, 'verdict': 'FAIL'}]
    else:
        record['decisions']['J1']['decision'] = 'FAIL'
    run = {'question_count': 1, 'evaluations': {'aiona': {'cases': [case], 'jev_eval': {'records': [record]}}}}
    original = copy.deepcopy(run)
    summary = ComparisonService._evaluation_summary(run)
    assert summary == {'total': 1, 'scored': 1, 'incomplete': 0, 'review_required': 0,
                       'criteria_incomplete': 1, 'criterion_review_required': 1}
    assert run == original


def test_code_failure_is_scored_before_semantic_response_arrives():
    case = {'case_status': 'EVALUATED', 'results': [],
            'summary': {'common': {'pass': 2, 'fail': 1, 'unjudged': 0}}}
    run = {'question_count': 1, 'evaluations': {'aiona': {'cases': [case]}}}
    assert ComparisonService._evaluation_summary(run) == {
        'total': 1, 'scored': 1, 'incomplete': 0, 'review_required': 0,
        'criteria_incomplete': 1, 'criterion_review_required': 0}


@pytest.mark.parametrize('record', [
    {'status': 'API_ERROR', 'criteria': ['J1'], 'decisions': {'J1': {'decision': 'FAIL'}}},
    {'status': 'RUNNING', 'criteria': ['J1'], 'decisions': {'J1': {'decision': 'FAIL'}}},
    {'status': 'SUCCESS', 'criteria': ['J1'], 'decisions': {
        'J1': {'decision': None, 'status': 'UNVERIFIED'}, 'J5': {'decision': 'FAIL'}}},
])
def test_unsuccessful_or_unrequested_semantic_fail_cannot_settle_question(record):
    case = {'case_status': 'EVALUATED', 'results': [],
            'summary': {'common': {'pass': 1, 'fail': 0, 'unjudged': 0}}}
    run = {'question_count': 1, 'evaluations': {'aiona': {'cases': [case], 'jev_eval': {'records': [record]}}}}
    summary = ComparisonService._evaluation_summary(run)
    assert summary['scored'] == 0
    assert summary['incomplete'] == 1


def test_unresolved_question_and_criterion_counts_use_different_units():
    case = {'case_status': 'EVALUATED', 'results': [],
            'summary': {'common': {'pass': 1, 'fail': 0, 'unjudged': 0}}}
    record = {'status': 'SUCCESS', 'criteria': ['J1', 'J5', 'J6'], 'review_required': True,
              'decisions': {'J1': {'decision': 'PASS'},
                            'J5': {'decision': None, 'status': 'UNVERIFIED'},
                            'J6': {'decision': None, 'status': 'UNVERIFIED'}}}
    run = {'question_count': 1, 'evaluations': {'aiona': {'cases': [case], 'jev_eval': {'records': [record]}}}}
    assert ComparisonService._evaluation_summary(run) == {
        'total': 1, 'scored': 0, 'incomplete': 1, 'review_required': 1,
        'criteria_incomplete': 1, 'criterion_review_required': 2}
    # Excluded input and specialized-only FAILs cannot settle a common outcome.
    case['results'] = [{'area': 'aiona_specialized', 'counted': True, 'verdict': 'FAIL'}]
    assert ComparisonService._evaluation_summary(run)['scored'] == 0
    case['case_status'] = 'INPUT_ERROR'
    record['decisions']['J1']['decision'] = 'FAIL'
    assert ComparisonService._evaluation_summary(run)['scored'] == 0


def test_case_errors_keep_question_incomplete():
    case = {'case_status': 'EVALUATED', 'results': [], 'summary': {'common': {'pass': 1, 'fail': 0, 'unjudged': 0}},
            'errors': [{'check_id': '3-1.check_error', 'subcategory': '3-1', 'kind': 'CHECK_ERROR', 'message': 'x', 'details': {}}]}
    record = {'status': 'SUCCESS', 'criteria': ['J1'], 'decisions': {'J1': {'decision': 'PASS'}}}
    run = {'question_count': 1, 'status': 'COMPLETED', 'evaluations': {'aiona': {'cases': [case], 'jev_eval': {'records': [record]}}}}
    summary = ComparisonService._evaluation_summary(run)
    assert summary['incomplete'] == 1 and summary['criteria_incomplete'] == 1 and summary['scored'] == 0
    case['errors'] = []
    assert ComparisonService._evaluation_summary(run)['scored'] == 1
