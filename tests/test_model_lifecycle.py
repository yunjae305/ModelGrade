"""Model lifecycle and manual comparison contracts; all model calls are mocked."""
import copy
import json

import httpx
import pytest

from test_blind_comparison import CapturingJudge, adapter_fixture
from test_comparison_workspace import wait
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from webapp.version_registry import VersionRegistry


def make_service(tmp_path, judge=None):
    registry = tmp_path / 'versions.json'
    if not registry.exists():
        registry.write_text(json.dumps({'versions': ['v1'], 'current_release': {'version': 'v1'}}), encoding='utf-8')
    handler, sent, answers, raw = adapter_fixture()
    judge = judge or CapturingJudge()
    evaluator = CodeEvalService(save_dir=tmp_path / 'eval', jev_resolver=lambda: (judge, judge.config, None))
    service = ComparisonService(evaluator, save_dir=tmp_path / 'runs', registry_path=registry,
                                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    for provider in ('gpt', 'claude', 'gemini'):
        service.configure(provider, {'key': 'fixture-only', 'model': 'fixture-' + provider})
    return service, judge, sent


def test_registration_and_activation_persist_without_files_or_evaluation(tmp_path):
    service, judge, sent = make_service(tmp_path)
    h = service.manage_release({'version': 'v2', 'notes': '문맥 유지 개선'})
    assert h['current_release']['version'] == 'v2' and len(h['releases']) == 2
    assert h['releases'][0]['registered_at'] and h['releases'][0]['notes'] == '문맥 유지 개선'
    assert not h['runs'] and not judge.calls and not sent
    before = service.version_registry.path.read_bytes()
    with pytest.raises(ValueError, match='이미 등록'):
        service.manage_release({'version': 'v2'})
    assert service.version_registry.path.read_bytes() == before
    service.manage_release({'action': 'activate', 'version': 'v1'})
    assert VersionRegistry(service.version_registry.path).current_release()['version'] == 'v1'
    assert not judge.calls and not sent


def test_manual_collects_exact_question_three_times_then_judges_four_answers(tmp_path):
    service, judge, sent = make_service(tmp_path)
    original_call = judge.system_one
    def after_collection(state, questions):
        assert set(sent) == {'gpt', 'claude', 'gemini'}
        return original_call(state, questions)
    judge.system_one = after_collection
    case = {'case_id': 'Q1', 'question': '  질문 원문\n두 번째 줄  ', 'answer': '  AIONA 답변\n 그대로  '}
    snapshot = copy.deepcopy(case)
    run = service.start_comparison({'cases': [case], 'input_mode': 'manual', 'expected_model_version': 'v1'})
    result = wait(service, run['run_id'])
    assert result['status'] == 'COMPLETED' and len(judge.calls) == 4
    assert result['aiona_release']['version'] == 'v1' and result['input_mode'] == 'manual'
    assert result['datasets']['aiona'][0]['answer'] == case['answer'] and case == snapshot
    for provider in sent:
        assert sent[provider] == [[{'role': 'user', 'content': case['question']}]]
    assert all(set(call['state']) <= {'question', 'answer', 'reference', 'history'} for call in judge.calls)
    for batch in result['evaluations'].values():
        assert batch['aiona_config']['enabled'] is False
        assert all(not check['subcategory'].startswith('AIONA-')
                   for row in batch['cases'] for check in row['results'])
    service.manage_release({'version': 'v2'})
    assert service.result(run['run_id'])['aiona_release']['version'] == 'v1'
    restarted, _, _ = make_service(tmp_path)
    assert restarted.result(run['run_id'])['aiona_release']['version'] == 'v1'
    assert restarted.version_registry.current_release()['version'] == 'v2'


@pytest.mark.parametrize('payload', [
    {'input_mode': 'manual', 'cases': [{'question': 'q', 'answer': '  '}]},
    {'input_mode': 'manual', 'cases': [{'question': 'q', 'answer': 'a'}] * 2},
    {'input_mode': 'invalid', 'cases': [{'question': 'q', 'answer': 'a'}]},
    {'expected_model_version': 'v2', 'cases': [{'question': 'q', 'answer': 'a'}]},
])
def test_invalid_manual_or_stale_version_rejected_before_calls(tmp_path, payload):
    service, judge, sent = make_service(tmp_path)
    with pytest.raises(ValueError):
        service.start_comparison(payload)
    assert not service.history() and not judge.calls and not sent
