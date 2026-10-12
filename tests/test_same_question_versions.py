"""Identical questions across model versions; all judgment calls are fixtures."""
import copy

import pytest

from test_model_lifecycle import make_service
from test_comparison_workspace import wait


def baseline(tmp_path):
    service, judge, sent = make_service(tmp_path)
    cases = [
        {'case_id': 'S1', 'question': '  독립 질문\n ', 'answer': 'old single', 'reference': {'value': 1}},
        {'case_id': 'M1', 'question': '기억해 줘\n', 'answer': 'old first', 'conversation_id': 'M', 'turn': 1},
        {'case_id': 'M2', 'question': '이전 내용은?', 'answer': 'old second', 'conversation_id': 'M', 'turn': 2},
    ]
    result = wait(service, service.start_version_run({'cases': cases, 'aiona_release': {'version': 'v1'}})['run_id'])
    service.manage_release({'version': 'v2', 'notes': 'Updated model'})
    return service, judge, sent, result


def test_template_excludes_old_answers_and_new_version_uses_own_history(tmp_path):
    service, judge, sent, source = baseline(tmp_path)
    before = (service.save_dir / (source['run_id'] + '.json')).read_bytes()
    template = service.version_template(source['run_id'])
    assert all(row['answer'] == '' for row in template['cases'])
    assert 'old first' not in str(template) and 'system_id' not in str(template)
    rows = [{**row, 'answer': f"  new {row['case_id']}\n "} for row in template['cases']]
    payload = {'baseline_run_id': source['run_id'], 'cases': list(reversed(rows)), 'aiona_release': {'version': 'v2'}}
    count = len(judge.calls)
    checked = service.prepare_version_comparison(payload)
    assert checked['cases'][0]['case_id'] == 'S1'
    assert len(judge.calls) == count and len(service.history()) == 1
    result = wait(service, service.start_version_run(payload)['run_id'])
    assert result['baseline_run_id'] == source['run_id']
    assert result['providers'] == ['aiona'] and result['aiona_release']['version'] == 'v2'
    assert [r['answer'] for r in result['datasets']['aiona']] == [r['answer'] for r in rows]
    last = next(call for call in judge.calls[count:] if call['state']['question'] == '이전 내용은?')
    assert last['state']['history'][0]['answer'] == '  new M1\n '
    assert not sent
    assert before == (service.save_dir / (source['run_id'] + '.json')).read_bytes()


@pytest.mark.parametrize('mutation', [
    lambda p: p['cases'][0].update(question='different'),
    lambda p: p['cases'][0].update(reference={'value': True}),
    lambda p: p['cases'][0].update(case_id='different'),
    lambda p: p['cases'][1].update(conversation_id='other'),
    lambda p: p['cases'][1].update(turn=2),
    lambda p: p['cases'].pop(),
    lambda p: p['cases'].append(copy.deepcopy(p['cases'][0])),
    lambda p: p['cases'][0].update(answer='  '),
    lambda p: p.update(aiona_release={'version': 'v1'}),
    lambda p: p.update(aiona_release={'version': 'not-registered'}),
    lambda p: p.update(source_run_id=p['baseline_run_id']),
])
def test_changed_or_incomplete_questions_rejected_before_evaluation(tmp_path, mutation):
    service, judge, sent, source = baseline(tmp_path)
    payload = {'baseline_run_id': source['run_id'], 'aiona_release': {'version': 'v2'},
               'cases': [{**r, 'answer': 'new answer'} for r in service.version_template(source['run_id'])['cases']]}
    mutation(payload)
    before = len(judge.calls)
    with pytest.raises(ValueError):
        service.start_version_run(payload)
    assert len(judge.calls) == before and len(service.history()) == 1 and not sent
