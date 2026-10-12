"""Startup configuration contracts; fake keys and HTTP only."""
import json
import pytest

import httpx

from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
from test_comparison_workspace import wait


def service(tmp_path, handler):
    return ComparisonService(CodeEvalService(save_dir=tmp_path / 'eval'),
                             save_dir=tmp_path / 'runs',
                             client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_env_models_reach_collection_without_leaking_credentials(tmp_path):
    env = tmp_path / '.env'
    env.write_text('GOOGLE_API_KEY=fake-google-secret\nGEMINI_MODEL=gemini-2.5-flash-lite\n'
                   'ANTHROPIC_API_KEY=fake-anthropic-secret\nANTHROPIC_MODEL=claude-haiku-4-5-20251001\n'
                   'OPENAI_API_KEY=fake-openai-file-secret\nOPENAI_MODEL=gpt-5-nano\n', encoding='utf-8')
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': ' original\r\nanswer '}]}
        ]})

    ce = service(tmp_path, handler)
    # Test/server objects are isolated until the executable explicitly loads ENV.
    assert not any(x['configured'] for x in ce.providers().values())
    ce.load_environment(environment={'OPENAI_API_KEY': 'fake-openai-process-secret',
                                     'OPENAI_MODEL': 'gpt-5-nano-2025-08-07'}, env_path=env)
    assert not sent  # Key loading itself never calls a provider.
    assert all(x['configured'] for x in ce.providers().values())
    assert ce.providers()['gpt']['model'] == 'gpt-5-nano-2025-08-07'
    assert ce.providers()['gemini']['model'] == 'gemini-2.5-flash-lite'
    assert ce.providers()['claude']['model'] == 'claude-haiku-4-5-20251001'
    run = ce.start({'providers': ['gpt'], 'cases': [{'question': ' Q\r\n', 'answer': ' A\n'}]})
    result = wait(ce, run['run_id'])
    assert len(sent) == 1
    body = json.loads(sent[0].content)
    assert sent[0].headers['Authorization'] == 'Bearer fake-openai-process-secret'
    assert body['model'] == 'gpt-5-nano-2025-08-07'
    assert body['input'] == [{'role': 'user', 'content': ' Q\r\n'}]
    assert result['datasets']['gpt'][0]['answer'] == ' original\r\nanswer '
    exposed = json.dumps(ce.providers()) + json.dumps(result) + ce.export(run['run_id']).decode('utf-8')
    assert 'fake-' not in exposed
    assert 'fake-' not in (tmp_path / 'runs' / (run['run_id'] + '.json')).read_text(encoding='utf-8')


def test_env_requires_both_values_and_uses_project_path(tmp_path, monkeypatch):
    env = tmp_path / '.env'
    env.write_text('GOOGLE_API_KEY=fake-key-only\nANTHROPIC_MODEL=model-only\n'
                   'OPENAI_API_KEY=fake-valid-pair\nOPENAI_MODEL=gpt-5-nano\n', encoding='utf-8')
    monkeypatch.setattr('webapp.comparison_service.ROOT', tmp_path)
    outside = tmp_path / 'unrelated-cwd'
    outside.mkdir()
    monkeypatch.chdir(outside)
    ce = service(tmp_path, lambda _: None)
    ce.load_environment(environment={})
    assert not ce.providers()['gemini']['configured']
    assert not ce.providers()['claude']['configured']
    assert ce.providers()['gpt']['configured']


def test_session_override_and_removal_do_not_rewrite_or_reload_env(tmp_path):
    env = tmp_path / '.env'
    original = b'OPENAI_API_KEY=fake-persisted-key\nOPENAI_MODEL=gpt-5-nano\n'
    env.write_bytes(original)
    ce = service(tmp_path, lambda _: None)
    ce.load_environment(environment={}, env_path=env)
    ce.configure('gpt', {'key': 'fake-session-key', 'model': 'session-model'})
    assert ce.providers()['gpt']['model'] == 'session-model'
    ce.configure('gpt', {'clear': True})
    assert not ce.providers()['gpt']['configured']
    assert not ce.providers()['gpt']['configured']
    assert env.read_bytes() == original
    restarted = service(tmp_path, lambda _: None)
    restarted.load_environment(environment={}, env_path=env)
    assert restarted.providers()['gpt']['model'] == 'gpt-5-nano'
    assert restarted.providers()['gpt']['configured']


def test_semantic_concurrency_defaults_to_twelve_and_loads_bounded_override(tmp_path):
    ce = service(tmp_path, lambda _: None)
    assert ce.semantic_workers == 12
    env = tmp_path / '.env'
    env.write_text('COMPARISON_SEMANTIC_WORKERS=4\n', encoding='utf-8')
    ce.load_environment(environment={}, env_path=env)
    assert ce.semantic_workers == 4
    ce.load_environment(environment={'COMPARISON_SEMANTIC_WORKERS': '12'}, env_path=env)
    assert ce.semantic_workers == 12


@pytest.mark.parametrize('value', ['0', '3', '17', '-4', '8.5', 'true', 'unlimited'])
def test_semantic_concurrency_rejects_invalid_environment_without_changing_settings(tmp_path, value):
    ce = service(tmp_path, lambda _: None)
    with pytest.raises(ValueError, match='COMPARISON_SEMANTIC_WORKERS'):
        ce.load_environment(environment={'COMPARISON_SEMANTIC_WORKERS': value}, env_path=tmp_path / 'missing')
    assert ce.semantic_workers == 12
    assert not any(p['configured'] for p in ce.providers().values())
