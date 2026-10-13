"""서버 버전 비교(webapp/version_comparison)·실행 요약(run_summary)·AIONA 이력(aiona_history) — 대역 평가자, 모델 호출 없음."""
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
                  'confidence': .9, 'reason': f"{cid} 대역 사유: '{state['answer']}'" if cid in fails else f'{cid} 대역 사유'}
            for cid in questions}})   # FAIL 은 답변 원문을 인용 — 근거 없는 FAIL 재평가(D-1) 대상이 아니다


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
    assert regression['target_evidence'][0]['id'] == 'J1' and regression['target_evidence'][0]['reason'] == "J1 대역 사유: '나쁜 답'" and regression['baseline_evidence'] == []
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


def test_run_summary_and_aiona_history_track_one_factor_at_a_time(tmp_path):
    service, judge = make_service(tmp_path)
    judge.__class__ = AnswerJudge
    first = version_run(service, '10.1', {})
    summary = run_summary(first)
    assert summary['run_id'] == first['run_id'] and summary['question_set']['count'] == 5 and summary['question_set']['fingerprint'] == first['dataset_fingerprint']
    assert summary['models']['aiona'] == {'name': 'AIONA', 'model_id': None, 'aiona_version': '10.1'}
    assert summary['profile']['pipeline'] == 'legacy' and summary['profile']['dispatch'] == 'realtime' and summary['profile']['judge']['requested_model'] == 'gpt-6.1-sol'
    assert summary['results']['aiona']['question_pass_rate'] == 1.0 and summary['results']['aiona']['criteria']['J1']['rate'] == 1.0
    assert [o['outcome'] for o in question_outcomes(first['evaluations']['aiona'])] == ['PASS'] * 5
    assert first['finished_at'] and 'condition_changes' not in first and not any(k.startswith('_activity_') for k in first)
    # 같은 AIONA 버전에 평가 모델만 바꾼 실행 → 한 요인(평가 모델 변경)만 바뀐 비교
    judge.config = copy.deepcopy(judge.config)
    object.__setattr__(judge.config, 'model', 'gpt-7-fixture')
    second = version_run(service, '10.1', {})
    assert evaluation_profile(second)['fields']['judge']['requested_model'] == 'gpt-7-fixture'
    # AIONA 버전만 바꾼 실행 (답변 하나가 나빠짐) → AIONA 버전 변경 한 요인, 질문 통과율 -0.2
    third = version_run(service, '10.2', {'Q2': '나쁜 답'})
    # AIONA 버전과 평가 모델이 함께 바뀐 실행 → 여러 요인 변경, 원인 단정 안 함
    object.__setattr__(judge.config, 'model', 'gpt-6.1-sol')
    fourth = version_run(service, '10.3', {})
    # 같은 버전에 답변만 다른 실행 → AIONA 답변 변경 한 요인 / 답변과 평가 모델이 함께 바뀐 실행 → 여러 요인 (평가 모델 효과로 읽지 않음)
    fifth = version_run(service, '10.3', {'Q3': '나쁜 답'})
    object.__setattr__(judge.config, 'model', 'gpt-7-fixture')
    sixth = version_run(service, '10.3', {'Q1': '나쁜 답'})
    history = service.aiona_history()
    assert history['version'] == 'aiona-history-v1' and [e['run_id'] for e in history['entries']] == [
        sixth['run_id'], fifth['run_id'], fourth['run_id'], third['run_id'], second['run_id'], first['run_id']]   # 최신순
    answers_only, answers_and_judge = (next(e for e in history['entries'] if e['run_id'] == r['run_id']) for r in (fifth, sixth))
    assert answers_only['factors'] == ['answers_changed'] and answers_only['attributable'] is True
    assert answers_and_judge['factors'] == ['judge_changed', 'answers_changed'] and answers_and_judge['attributable'] is False
    by_id = {e['run_id']: e for e in history['entries']}
    start, judge_only, version_only, both = (by_id[r['run_id']] for r in (first, second, third, fourth))
    assert start['previous_run_id'] is None and start['changes'] == [] and start['deltas'] is None
    assert start['aiona_version'] == '10.1' and start['results']['question_pass_rate'] == 1.0 and start['results']['complete'] is True
    assert start['question_set'] == summary['question_set'] and start['conditions']['dispatch'] == 'realtime'
    assert judge_only['previous_run_id'] == first['run_id'] and judge_only['factors'] == ['judge_changed']
    assert judge_only['changes'][0]['fields'] == ['judge.requested_model'] and judge_only['changes'][0]['field_labels'] == ['평가 모델']
    assert judge_only['attributable'] is True and judge_only['multi_factor'] is False
    assert version_only['factors'] == ['aiona_version_changed'] and version_only['changes'][0]['before'] == '10.1'
    assert version_only['attributable'] is True and version_only['deltas']['question_pass_rate'] == -0.2
    assert version_only['deltas']['criteria']['J1']['rate'] == -0.2 and version_only['results']['criteria']['J1']['fail'] == 1
    assert both['factors'] == ['judge_changed', 'aiona_version_changed'] and both['multi_factor'] is True and both['attributable'] is False
    assert all(change['multi_factor'] for change in both['changes'])
    assert not any('collection_models_changed' in e['factors'] for e in history['entries'])   # 이력 관리는 AIONA 만
    (series,) = history['series']
    assert series['fingerprint'] == first['dataset_fingerprint'] and [p['run_id'] for p in series['points']] == [
        first['run_id'], second['run_id'], third['run_id'], fourth['run_id'], fifth['run_id'], sixth['run_id']]
    assert [p['markers'] for p in series['points']] == [[], ['judge_changed'], ['aiona_version_changed'], ['judge_changed', 'aiona_version_changed'],
                                                         ['answers_changed'], ['judge_changed', 'answers_changed']]
    assert history['factor_labels']['aiona_version_changed'] == 'AIONA 버전 변경'


def test_overall_verdict_needs_every_question_linked_and_judged(tmp_path):
    service, judge = make_service(tmp_path)
    judge.__class__ = AnswerJudge
    base = version_run(service, '10.1', {'Q1': '나쁜 답', 'Q2': '나쁜 답', 'Q3': '나쁜 답'})
    target = version_run(service, '10.2', {})
    full = vc.compare_runs(base, target)
    assert full['coverage']['complete'] is True and full['verdict']['label'] != 'partial'
    # 판정이 끝나지 않은 문항이 하나라도 있으면 전체 판정을 확정하지 않는다 (부분 표본 통계는 따로 보여 줌)
    unjudged = copy.deepcopy(target)
    unjudged['evaluations']['aiona']['jev_eval']['records'][3]['status'] = 'API_ERROR'
    partial = vc.compare_runs(base, unjudged)
    assert partial['coverage']['pending'] >= 1 and partial['coverage']['complete'] is False
    assert partial['verdict']['label'] == 'partial' and '판정이 끝난 공통 문항만' in partial['verdict']['text']
    # 문항 ID 를 바꿔 연결에서 빠진 문항이 있어도 같은 결론을 내지 않는다
    renamed = copy.deepcopy(target)
    for key in ('id', 'case_id'):
        if key in renamed['datasets']['aiona'][0]:
            renamed['datasets']['aiona'][0][key] = 'Q1-renamed'
    assert vc.compare_runs(base, renamed)['verdict']['label'] in ('partial', 'not_comparable')


def test_profile_differences_ignore_only_dispatch_settings():
    a = {'fields': {'judge': {'requested_model': 'm', 'evidence_recovery_rounds': 0, 'dispatch': 'realtime', 'api_base': 'https://a'}}}
    b = {'fields': {'judge': {'requested_model': 'm', 'evidence_recovery_rounds': 1, 'dispatch': 'batch', 'batch_questions': 20,
                              'api_base': 'https://b'}}}
    assert [(d['field'], d['label']) for d in vc.profile_differences(a, b)] == [('judge.evidence_recovery_rounds', '근거 보강 횟수')]


def test_expanded_items_are_matched_by_quote_not_model_numbers():
    def record(*items):
        return {'decisions': {'J4': {'decision': 'FAIL', 'subs': {'J4-2': {'decision': 'FAIL', 'items': [
            {'id': item_id, 'decision': decision, 'question_quote': quote} for item_id, quote, decision in items]}}}}}
    before = record(('J4-2.a#1', '각각', 'YES'), ('J4-2.a#2', '이유를 한 줄씩', 'NO'))
    after = record(('J4-2.a#1', '이유를  한 줄씩', 'NO'), ('J4-2.a#2', '각각', 'YES'))   # 나열 순서만 다름 (공백 차이 포함)
    items = vc._drilldown(None, before, None, after)['items']
    assert {key: row['change'] for key, row in items.items()} == {'J4-2.a · 각각': 'pass_kept', 'J4-2.a · 이유를 한 줄씩': 'fail_kept'}
    # 답변에서 생긴 주장(J5-1.a)은 답변 인용으로 대응하고, 같은 인용이 둘이면 덮어쓰지 않는다
    claims = {'decisions': {'J5': {'decision': 'FAIL', 'subs': {'J5-1': {'decision': 'FAIL', 'items': [
        {'id': 'J5-1.a#1', 'origin': 'answer', 'decision': 'YES', 'question_quote': '인구', 'answer_quote': '서울 인구는 933만'},
        {'id': 'J5-1.a#2', 'origin': 'answer', 'decision': 'NO', 'question_quote': '인구', 'answer_quote': '부산 인구는 900만'},
        {'id': 'J4-2.a#1', 'decision': 'YES', 'question_quote': '각각'}, {'id': 'J4-2.a#2', 'decision': 'NO', 'question_quote': '각각'}]}}}}}
    assert vc._semantic_units(claims)[2] == {'J5-1.a · 서울 인구는 933만': 'PASS', 'J5-1.a · 부산 인구는 900만': 'FAIL',
                                             'J4-2.a · 각각 [1]': 'FAIL', 'J4-2.a · 각각 [2]': 'PASS'}
    # 같은 인용의 두 요소가 번호만 바뀌면 변화 없음, 판정 개수가 바뀐 만큼만 개선·악화
    swapped = copy.deepcopy(claims)
    j4 = swapped['decisions']['J5']['subs']['J5-1']['items']
    j4[2]['decision'], j4[3]['decision'] = 'NO', 'YES'
    assert {k: r['change'] for k, r in vc._drilldown(None, claims, None, swapped)['items'].items() if k.startswith('J4')} == {
        'J4-2.a · 각각 [1]': 'fail_kept', 'J4-2.a · 각각 [2]': 'pass_kept'}
    j4[2]['decision'] = 'YES'
    assert {k: r['change'] for k, r in vc._drilldown(None, claims, None, swapped)['items'].items() if k.startswith('J4')} == {
        'J4-2.a · 각각 [1]': 'improved', 'J4-2.a · 각각 [2]': 'pass_kept'}


def test_run_summary_code_rates_include_saved_completion_like_the_question_verdict():
    from code_eval.common_adjudication import resolved_batch
    from tests.test_common_adjudication import legacy_batch
    from webapp import run_summary as module
    batch, index = legacy_batch()
    category = str(batch['cases'][0]['results'][index]['category'])
    resolved = resolved_batch(batch)['cases'][0]['results']
    expected = sum(1 for r in resolved if r.get('area') == 'common' and r.get('counted') and r.get('verdict') == 'PASS'
                   and str(r.get('category')) == category)
    assert module._criteria_rates(batch)['code'][category]['pass'] == expected
    raw = sum(1 for r in batch['cases'][0]['results'] if r.get('area') == 'common' and r.get('counted') and r.get('verdict') == 'PASS'
              and str(r.get('category')) == category)
    assert expected == raw + 1   # 보완 판정으로 확정된 행이 요약에도 들어간다
