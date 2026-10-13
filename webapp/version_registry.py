"""Additive AIONA release metadata; legacy source and result files stay untouched."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .activity_store import ActivityStore, clean_text, event_record


def validate_release(value, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, dict):
        raise ValueError('AIONA 버전 정보를 입력해 주세요.')
    version = value.get('version')
    if (not isinstance(version, str) or not version.strip() or len(version) > 80
            or any(ord(c) < 32 for c in version)):
        raise ValueError('AIONA 버전은 1~80자의 문자열이어야 합니다.')
    collected = value.get('collected_at')
    if collected == '':
        collected = None
    if collected is not None:
        if not isinstance(collected, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', collected):
            raise ValueError('응답 수집일은 YYYY-MM-DD 형식으로 입력해 주세요.')
        try:
            date.fromisoformat(collected)
        except ValueError:
            raise ValueError('응답 수집일이 올바르지 않습니다.') from None
    notes = value.get('notes', '')
    if not isinstance(notes, str) or len(notes) > 2000:
        raise ValueError('버전 설명은 2,000자 이하의 문자열이어야 합니다.')
    return {'version': version.strip(), 'collected_at': collected, 'notes': notes}


def fingerprint(cases, *, answers=False):
    """Canonical fields, exact text, input order; metadata aliases do not alter identity."""
    rows = []
    for case in cases:
        if not isinstance(case, dict):
            rows.append(None)
            continue
        row = {k: case.get(k) for k in ('question', 'conversation_id', 'turn', 'reference')}
        if answers:
            row.update(answer=case.get('answer'), displayed_text=case.get('displayed_text'))
        rows.append(row)
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def evaluation_profile(run):
    """Describe actual saved AIONA evaluation, never today's configuration.

    외부 수집 실행처럼 AIONA 가 없는 실행은 첫 번째 평가 대상의 평가 조건을 쓴다 (모든 대상이 같은 평가기로 평가됨).
    """
    evaluations = run.get('evaluations', {})
    batch = evaluations.get('aiona') or (evaluations.get((run.get('providers') or [None])[0]) or {}
                                         if 'aiona' not in (run.get('providers') or ['aiona']) else {})
    semantic = batch.get('jev_eval') or {}
    client = semantic.get('client') or {}
    allowed = ('engine', 'requested_model', 'reasoning_effort', 'flags', 'disabled_features',
               'web_search', 'project_doc_max_bytes', 'temperature', 'top_p', 'seed', 'max_tokens',
               'max_output_tokens', 'max_tool_calls', 'fact_check_policy', 'fact_check_scope', 'context_check_policy', 'search_policy',
               'search_host_features', 'search_host_policy', 'max_fetches', 'max_continuations',
               'evidence_recovery_policy', 'evidence_recovery_rounds', 'evidence_recovery_timeout_s',
               'evidence_recovery_max_tool_calls', 'max_requests',
               'thinking', 'output_mode', 'search_tool', 'fetch_tool')
    judge = {k: copy.deepcopy(client[k]) for k in allowed if k in client} if isinstance(client, dict) else {}
    if isinstance(client, dict) and isinstance(client.get('api_base'), str):
        try:
            url = urlsplit(client['api_base'])
            if url.scheme in ('http', 'https') and url.hostname:
                host = '[' + url.hostname + ']' if ':' in url.hostname else url.hostname
                judge['api_base'] = urlunsplit((url.scheme, host + (f':{url.port}' if url.port else ''), url.path.rstrip('/'), '', ''))
        except ValueError:
            pass
    fields = {
        'engine_versions': copy.deepcopy(batch.get('versions')),
        'aiona_config': copy.deepcopy(batch.get('aiona_config')),
        'semantic_version': semantic.get('version'),
        'criteria_version': semantic.get('criteria_version'),
        'applicability_version': semantic.get('applicability_version'),
        'history_policy': semantic.get('history_policy'),
        'decision_policy': copy.deepcopy(semantic.get('decision_policy')),
        'judge': judge or None,
        'judge_models': sorted(set(semantic.get('models') or [])),
    }
    complete = bool(not batch.get('live') and fields['engine_versions'] and fields['engine_versions'].get('engine')
                    and fields['aiona_config'] and fields['aiona_config'].get('config_hash')
                    and all(fields[k] for k in ('semantic_version', 'criteria_version', 'applicability_version',
                                               'history_policy', 'judge_models'))
                    and judge.get('engine') and judge.get('requested_model'))
    return {'complete': complete, 'fields': fields}


class VersionRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        self.activity = ActivityStore(path.with_suffix('.sqlite3'))

    def _refresh(self):
        self.data = self.activity.registry(self.data)

    def release_for(self, run):
        return copy.deepcopy(run.get('aiona_release'))

    def current_release(self):
        self._refresh()
        return validate_release(copy.deepcopy(self.data.get('current_release')))

    def releases(self):
        self._refresh()
        releases = copy.deepcopy(self.data.get('releases', []))
        current = self.current_release()
        if current and not any(r['version'] == current['version'] for r in releases):
            releases.insert(0, current)
        return releases

    def manage(self, payload):
        action = payload.get('action', 'register')
        if action not in ('register', 'activate'):
            raise ValueError('지원하지 않는 버전 관리 작업입니다.')
        release = validate_release({'version': payload.get('version'), 'notes': payload.get('notes', '')}, required=True)
        release['notes'] = clean_text(release['notes'])
        set_current = payload.get('set_current', True)  # Preserve legacy API clients; the new UI sends false.
        if type(set_current) is not bool:
            raise ValueError('기본 평가 버전 선택 옵션은 true 또는 false여야 합니다.')
        reason = payload.get('reason', '')
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError('변경 사유는 2,000자 이하의 문자열이어야 합니다.')
        reason = clean_text(reason.strip())
        with self.activity.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM registry WHERE id=1').fetchone()
            data = json.loads(row[0]) if row else copy.deepcopy(self.data)
            current = validate_release(data.get('current_release'))
            if ('expected_current_version' in payload
                    and payload['expected_current_version'] != (current or {}).get('version')):
                raise ValueError('기본 평가 버전이 변경되었습니다. 새로고침 후 다시 확인해 주세요.')
            releases = copy.deepcopy(data.get('releases', []))
            if current and not any(item['version'] == current['version'] for item in releases):
                releases.insert(0, current)
            existing = next((r for r in releases if r['version'] == release['version']), None)
            events = []
            if action == 'register':
                if existing:
                    raise ValueError('이미 등록된 모델 버전입니다.')
                release['registered_at'] = datetime.now(timezone.utc).isoformat()
                releases.insert(0, release)
                registered = event_record('model_version_registered', model_version=release['version'],
                                          after=release, reason=reason, timestamp=release['registered_at'])
                events.append(registered)
            else:
                if existing is None:
                    raise ValueError('등록된 모델 버전을 선택해 주세요.')
                release = existing
            change_default = action == 'activate' or set_current
            if change_default and (current or {}).get('version') != release['version']:
                events.append(event_record('default_version_changed', model_version=release['version'],
                              before={'version': current['version']} if current else None,
                              after={'version': release['version']}, reason=reason,
                              related_event_id=events[0]['event_id'] if events else None))
            updated = {**data, 'releases': releases, 'versions': list(dict.fromkeys(
                [*data.get('versions', []), *[r['version'] for r in releases]])),
                       'current_release': validate_release(release) if change_default else current}
            db.execute('INSERT OR REPLACE INTO registry(id,data) VALUES(1,?)',
                       (json.dumps(updated, ensure_ascii=False, allow_nan=False),))
            self.activity.insert(db, events)
        self.data = updated
        return self.current_release()

    def metadata(self, run):
        cases = run.get('datasets', {}).get('aiona', [])
        return {'aiona_release': self.release_for(run), 'evaluation_profile': evaluation_profile(run),
                'dataset_fingerprint': run.get('dataset_fingerprint') or fingerprint(cases),
                'source_fingerprint': run.get('source_fingerprint') or fingerprint(cases, answers=True)}

    def version_history(self, runs):
        self._refresh()
        versions = set(self.data.get('versions', []))
        versions.update(r['aiona_release']['version'] for r in runs if r.get('aiona_release'))
        def order(value):
            return [(0, int(p)) if p.isdecimal() else (1, p) for p in re.split(r'(\d+)', value)]
        return {'versions': sorted(versions, key=order), 'baseline_run_id': self.data.get('baseline_run_id'),
                'current_release': self.current_release(), 'releases': self.releases(), 'runs': runs}
