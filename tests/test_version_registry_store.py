"""모델 버전 목록(VersionRegistry·RegistryStore) — 등록·기본 버전 변경과 사유, 재시작 유지, 쓰기 실패 롤백, 동시 등록. 모델 호출 없음."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from test_aiona_versions import CASES, make_service
from webapp.comparison_service import ComparisonService
from webapp.registry_store import RegistryStore
from webapp.version_registry import VersionRegistry


def test_registration_default_change_reason_is_kept_in_the_release_and_survives_restart(tmp_path):
    service, judge = make_service(tmp_path)
    initial = service.manage_release({'version': 'v1', 'set_current': False, 'reason': '첫 등록'})
    assert initial['current_release'] is None
    service.manage_release({'action': 'activate', 'version': 'v1', 'reason': '첫 평가 기준'})
    service.manage_release({'version': 'v2', 'set_current': False, 'notes': '변경 설명', 'actor': 'admin'})
    assert service.version_history()['current_release']['version'] == 'v1'
    original = service.start({'cases': CASES})
    before = (service.save_dir / (original['run_id'] + '.json')).read_bytes()
    service.manage_release({'action': 'activate', 'version': 'v2', 'reason': '새 평가부터 사용', 'expected_current_version': 'v1'})
    releases = {release['version']: release for release in service.version_history()['releases']}
    assert releases['v1']['registered_reason'] == '첫 등록' and 'registered_reason' not in releases['v2']
    assert [(c['from'], c['reason']) for c in releases['v1']['default_changes']] == [(None, '첫 평가 기준')]
    assert [(c['from'], c['reason']) for c in releases['v2']['default_changes']] == [('v1', '새 평가부터 사용')]
    assert service.result(original['run_id'])['aiona_release']['version'] == 'v1'
    assert (service.save_dir / (original['run_id'] + '.json')).read_bytes() == before
    restarted = ComparisonService(service.evaluator, save_dir=service.save_dir, registry_path=tmp_path / 'registry.json')
    assert restarted.version_history()['current_release']['version'] == 'v2'
    assert restarted.version_history()['releases'] == service.version_history()['releases'] and not judge.calls
    with pytest.raises(ValueError, match='변경되었습니다'):
        service.manage_release({'action': 'activate', 'version': 'v1', 'expected_current_version': 'v1'})
    assert not any(key.startswith('_activity_') for key in service.result(original['run_id']))   # 작업 이력은 남기지 않는다


def test_registry_write_failure_rolls_back(tmp_path, monkeypatch):
    registry = VersionRegistry(tmp_path / 'versions.json')
    registry.manage({'version': 'v1', 'set_current': False})
    before_state = registry.releases()
    original = RegistryStore.connect

    class FailingInsert:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            if sql.startswith('INSERT'):
                raise sqlite3.OperationalError('fixture disk failure')
            return self.db.execute(sql, *args)

    @contextmanager
    def failing(self):
        with original(self) as db:
            yield FailingInsert(db)
    monkeypatch.setattr(RegistryStore, 'connect', failing)
    with pytest.raises(sqlite3.OperationalError):
        registry.manage({'version': 'v2', 'set_current': True})
    monkeypatch.setattr(RegistryStore, 'connect', original)
    recovered = VersionRegistry(registry.path)
    assert recovered.releases() == before_state and recovered.current_release() is None


def test_concurrent_registry_instances_lose_no_records(tmp_path):
    path = tmp_path / 'versions.json'

    def register(index):
        VersionRegistry(path).manage({'version': f'v{index}', 'set_current': False})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(register, range(24)))
    registry = VersionRegistry(path)
    assert len({release['version'] for release in registry.releases()}) == 24 and registry.current_release() is None
