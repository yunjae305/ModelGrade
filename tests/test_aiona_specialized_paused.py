"""Paused AIONA rules never run through any workspace evaluation entry point."""
import time

import pytest

from code_eval import engine
from code_eval.config import DEFAULT_CONFIG_PATH, load_aiona_config
from code_eval.jev_client import resolve_jev_client
from webapp.code_eval_service import CodeEvalService


@pytest.fixture
def evaluator(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Paused specialized rules must not be invoked')

    monkeypatch.setattr(engine, 'check_aiona', forbidden)
    return CodeEvalService(save_dir=tmp_path / 'evaluations', jev_resolver=lambda: resolve_jev_client(
        environment={}, env_path=tmp_path / '.no-env'))


def assert_paused(batch):
    assert batch['aiona_config']['enabled'] is False
    assert batch['aiona_config']['config_hash'].startswith('paused-')
    assert batch['summary']['specialized'] == {'pass': 0, 'fail': 0, 'unjudged': 0, 'pass_rate': None}
    checks = batch['cases'][0]['results']
    assert checks and all(check['area'] == 'common' for check in checks)


@pytest.mark.parametrize('flag', [None, False, True])
@pytest.mark.parametrize('background', [False, True])
def test_direct_and_background_requests_cannot_reenable_specialized(evaluator, flag, background):
    source = DEFAULT_CONFIG_PATH.read_bytes()
    payload = {'question': '인사해 주세요.', 'answer': '안녕하세요.',
               'displayed_text': '안녕하세요.', 'system_id': 'aiona'}
    if flag is not None:
        payload['aiona_specialized'] = flag
    if background:
        job_id = evaluator.start_job(payload)['job_id']
        deadline = time.monotonic() + 5
        while evaluator.job_status(job_id)['status'] == 'RUNNING' and time.monotonic() < deadline:
            time.sleep(.01)
        assert evaluator.job_status(job_id)['status'] == 'COMPLETED'
        batch = evaluator.job_result(job_id)
    else:
        batch = evaluator.evaluate(payload)
    assert_paused(batch)
    assert DEFAULT_CONFIG_PATH.read_bytes() == source


def test_reference_metadata_matches_the_paused_web_policy(evaluator):
    info = evaluator.info()
    assert info['aiona_config']['enabled'] is False
    assert info['aiona_specialized'] == {'status': 'PAUSED', 'included_in_score': False}
    assert {row['id'] for row in info['taxonomy']['specialized']} == {'AIONA-M', 'AIONA-F'}
    # Registry contents and the independent engine configuration are retained.
    configured = load_aiona_config()
    assert info['aiona_config']['config_hash'] == 'paused-' + configured.raw_hash
    assert info['aiona_config']['marker_patterns'] == len(configured.marker_patterns)
