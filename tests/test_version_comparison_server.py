"""서버 버전 비교(webapp/version_comparison)·실행 요약(run_summary)·조건 변경 이벤트 — 대역 평가자, 모델 호출 없음."""
import copy

import pytest

from code_eval.jev_client import JevCall
from webapp import version_comparison as vc
from webapp.run_summary import question_outcomes, run_summary
from webapp.version_registry import evaluation_profile
from tests.test_aiona_versions import make_service
from tests.test_blind_comparison import CapturingJudge
from tests.test_comparison_workspace import wait

QUESTIONS = [{'id': 'Q1', 'question': '1 + 1은?', 'answer': '2'},
             {'id': 'Q2', 'question': '사과를 한 문장으로 설명해 줘.', 'answer': '사과는 과일입니다.'},
             {'id': 'Q3', 'question': '바다를 한 문장으로 설명해 줘.', 'answer': '바다는 넓습니다.'},
             {'id': 'C1-T1', 'conversation_id': 'C1', 'turn': 1, 'question': '내 이름은 민수야. 기억해 줘.', 'answer': '네, 민수님.'},
             {'id': 'C1-T2', 'conversation_id': 'C1', 'turn': 2, 'question': '내 이름이 뭐였지?', 'answer': '민수님입니다.'}]


class AnswerJudge(CapturingJudge):
    """답변에 '나쁜' 이 있으면 J1 FAIL — 버전마다 다른 답변으로 개선·악화를 만든다."""

    def system_one(self, state, questions):
        self.calls.append(copy.deepcopy({'state': state, 'questions': questions}))
        fails = {'J1'} if '나쁜' in state['answer'] else set()
        return JevCall('OK', body={'model': 'gpt-6.1-sol', 'answers': {
            cid: {'choice': 'FAIL' if cid in fails else 'PASS', 'probabilities': {'PASS': .05, 'FAIL': .95} if cid in fails else {'PASS': .95, 'FAIL': .05},
                  'confidence': .9, 'reason': f'{cid} 대역 사유'} for cid in questions}})


def version_run(service, version, answers):
    cases = [{**copy.deepcopy(q), 'answer': answers.get(q['id'], q['answer'])} for q in QUESTIONS]
    run = service.start_version_run({'cases': cases, 'aiona_release': {'version': version}})
    return wait(service, run['run_id'])


def test_sign_test_profile_differences_and_condition_changes():
    assert vc.sign_test(0, 0) is None and vc.sign_test(3, 3) == 1.0 and vc.sign_test(5, 0) == 0.0625 and vc.sign_test(7, 0) == 0.0156
    assert vc.sign_test(0, 7) == 0.0156 and vc.sign_test(8, 1) == 0.0391
    a = {'fields': {'semantic_version': 'v', 'judge': {'requested_model': 'm', 'dispatch': 'realtime', 'batch_questions': 20}}}
    b = {'fields': {'semantic_version': 'v', 'pipeline': 'legacy', 'items_version': None, 'judge': {'requested_model': 'm', 'dispatch': 'batch'}}}
    assert vc.profile_differences(a, b) == []                                           # pipeline 누락 = legacy, 호출 방식은 비교하지 않음
    c = {'fields': {'semantic_version': 'v2', 'pipeline': 'items', 'judge': {'requested_model': 'n'}}}
    diffs = vc.profile_differences(a, c)
    assert [(d['field'], d['label'], d['baseline'], d['target']) for d in diffs] == [
        ('pipeline', '의미 평가 방식', 'legacy', 'items'), ('semantic_version', '의미 평가 버전', 'v', 'v2'), ('judge.requested_model', '평가 모델', 'm', 'n')]
    previous = {'profile': {'code_version': 'e1', 'criteria_version': 'c1', 'judge': {'requested_model': 'm'}, 'judge_models': ['m']},
                'models': {'gpt': {'model_id': 'g1'}, 'aiona': {'model_id': None}}}
    current = {'profile': {'code_version': 'e1', 'criteria_version': 'c2', 'judge': {'requested_model': 'n'}, 'judge_models': ['n']},
               'models': {'gpt': {'model_id': 'g1'}, 'claude': {'model_id': 'c'}}}
    changes = vc.condition_changes(previous, current)
    assert [c['event_type'] for c in changes] == ['criteria_changed', 'judge_changed']
    assert changes[0]['fields'] == ['criteria_version'] and changes[0]['multi_factor'] is True and changes[1]['factors'] == ['criteria_changed', 'judge_changed']
    assert vc.condition_changes(None, current) == []
    only_models = vc.condition_changes({**previous, 'profile': current['profile'], 'models': {'gpt': {'model_id': 'g0'}}}, current)
    assert [c['event_type'] for c in only_models] == ['collection_models_changed'] and only_models[0]['multi_factor'] is False
    assert only_models[0]['before'] == {'gpt': 'g0'} and only_models[0]['after'] == {'gpt': 'g1'}


def test_compare_runs_drills_down_and_uses_conversation_units(tmp_path):
    service, judge = make_service(tmp_path)
    judge.__class__ = AnswerJudge
    base = version_run(service, '10.1', {'Q3': '나쁜 답'})
    target = version_run(service, '10.2', {'Q2': '나쁜 답', 'C1-T2': '나쁜 답'})
    result = service.version_comparison(base['run_id'], target['run_id'])
    assert result['version'] == vc.COMPARISON_VERSION and result['conditions']['eligible'] is True and result['conditions']['differences'] == []
    assert result['matched'] == 5 and result['unmatched'] == {'baseline': [], 'target': []}
    assert result['counts'] == {'improved': 1, 'regressed': 2, 'pass_kept': 2, 'fail_kept': 0, 'pending': 0, 'excluded': 0}
    by_id = {row['case_id']: row for row in result['rows']}
    assert by_id['Q3']['change'] == 'improved' and by_id['Q2']['change'] == 'regressed' and by_id['C1-T2']['change'] == 'regressed'
    assert by_id['C1-T1']['change'] == 'pass_kept' and by_id['Q1']['label'] == '통과 유지'
    sign = result['sign_test']
    assert sign == {'unit': 'conversation', 'plus': 1, 'minus': 2, 'ties': 1, 'incomplete': 0, 'n': 3, 'p_value': 1.0, 'significance': 0.05}
    assert result['tail']['size'] == 5 and result['tail']['case_ids'][0] == 'Q3' and result['tail']['improved'] == 1 and result['tail']['regressed'] == 2
    assert result['verdict']['label'] == 'uncertain'
    regression = next(r for r in result['regressions'] if r['case_id'] == 'Q2')
    assert regression['baseline_answer'] == '사과는 과일입니다.' and regression['target_answer'] == '나쁜 답'
    assert regression['target_evidence'][0]['id'] == 'J1' and regression['target_evidence'][0]['reason'] == 'J1 대역 사유' and regression['baseline_evidence'] == []
    assert regression['changes']['criteria']['J1'] == {'baseline': 'PASS', 'target': 'FAIL', 'change': 'regressed'}
    assert [r['case_id'] for r in result['improvements']] == ['Q3']
    j1 = next(row for row in result['criteria'] if row['level'] == 'criterion' and row['id'] == 'J1')
    assert j1['improved'] == 1 and j1['regressed'] == 2 and j1['pass_kept'] == 2
    assert any(row['level'] == 'code' for row in result['criteria'])
    assert result['baseline']['aiona_release']['version'] == '10.1' and result['target']['results']['aiona']['questions'] == {'PASS': 3, 'FAIL': 2, 'PENDING': 0, 'EXCLUDED': 0}
    # 같은 실행끼리·조건이 다르면 비교 불가 사유와 '재평가로 맞추기' 안내
    same = service.version_comparison(base['run_id'], base['run_id'])
    assert same['conditions']['eligible'] is False and [r['code'] for r in same['conditions']['reasons']] == ['same_run', 'same_or_unknown_version']
    changed = copy.deepcopy(target)
    changed['evaluations']['aiona']['jev_eval']['criteria_version'] = 'jev-criteria-v9'
    mismatch = vc.compare_runs(base, changed)
    assert mismatch['conditions']['eligible'] is False and mismatch['conditions']['differences'][0]['label'] == '의미 기준 버전'
    assert mismatch['conditions']['suggestion'].startswith('저장된 답변에 새 기준을 다시 돌려') and mismatch['verdict']['label'] == 'not_comparable'
    with pytest.raises(ValueError):
        service.version_comparison(base['run_id'], None)


def test_run_summary_and_condition_change_events_in_activity_history(tmp_path):
    service, judge = make_service(tmp_path)
    judge.__class__ = AnswerJudge
    first = version_run(service, '10.1', {})
    summary = run_summary(first)
    assert summary['run_id'] == first['run_id'] and summary['question_set']['count'] == 5 and summary['question_set']['fingerprint'] == first['dataset_fingerprint']
    assert summary['models']['aiona'] == {'name': 'AIONA', 'model_id': None, 'aiona_version': '10.1'}
    assert summary['profile']['pipeline'] == 'legacy' and summary['profile']['dispatch'] == 'realtime' and summary['profile']['judge']['requested_model'] == 'gpt-6.1-sol'
    assert summary['results']['aiona']['question_pass_rate'] == 1.0 and summary['results']['aiona']['criteria']['J1']['rate'] == 1.0
    assert [o['outcome'] for o in question_outcomes(first['evaluations']['aiona'])] == ['PASS'] * 5
    assert first['condition_changes'] == {'previous_run_id': None, 'changes': [], 'summary': {k: summary[k] for k in ('profile', 'models', 'question_set')}}
    # 평가 모델이 바뀐 다음 실행: judge_changed 이벤트가 작업 이력에 남는다
    judge.config = copy.deepcopy(judge.config)
    object.__setattr__(judge.config, 'model', 'gpt-7-fixture')
    second = version_run(service, '10.2', {})
    changes = second['condition_changes']
    assert changes['previous_run_id'] == first['run_id'] and [c['event_type'] for c in changes['changes']] == ['judge_changed']
    assert changes['changes'][0]['fields'] == ['requested_model'] and changes['changes'][0]['multi_factor'] is False
    events = service.activity_history({'event_type': 'judge_changed'})['events']
    assert len(events) == 1 and events[0]['run_id'] == second['run_id'] and events[0]['source_run_id'] == first['run_id']
    assert events[0]['before']['requested_model'] == 'gpt-6.1-sol' and events[0]['after']['requested_model'] == 'gpt-7-fixture'
    assert '평가 모델 변경' in events[0]['reason'] and '여러 요인' not in events[0]['reason']
    assert evaluation_profile(second)['fields']['judge']['requested_model'] == 'gpt-7-fixture'
