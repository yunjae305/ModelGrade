"""Transactional local registry and append-only activity records.

The legacy JSON is imported lazily on the first mutation and stays untouched.
SQLite serializes writers across threads/processes; event keys make recovery
from a saved run's outbox idempotent.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def clean_text(value, *, limit=2000, secrets=()):
    text = str(value or '')[:limit]
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 4:
            text = text.replace(secret, '[비공개]')
    text = re.sub(r'\b(?:sk|AIza)[-_A-Za-z0-9]{12,}\b', '[비공개]', text)
    text = re.sub(r'(?i)(bearer\s+)[\w.\-/+=]+', r'\1[비공개]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*)[^\s,;]+', r'\1[비공개]', text)
    return text


def event_record(event_type, *, key=None, timestamp=None, actor='local', model_version=None,
                 run_id=None, before=None, after=None, reason='', result='success',
                 error_summary=None, source_run_id=None, related_event_id=None):
    key = key or uuid.uuid4().hex
    return {
        'event_id': uuid.uuid5(uuid.NAMESPACE_URL, 'aiona-activity:' + key).hex,
        'timestamp': timestamp or datetime.now(timezone.utc).isoformat(),
        'event_type': event_type,
        'actor': {'type': actor, 'label': '로컬 사용자' if actor == 'local' else '시스템'},
        'model_version': model_version, 'run_id': run_id,
        'before': copy.deepcopy(before), 'after': copy.deepcopy(after),
        'reason': clean_text(reason), 'result': result,
        'error_summary': clean_text(error_summary, limit=500) if error_summary else None,
        'source_run_id': source_run_id, 'related_event_id': related_event_id,
    }


class ActivityStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS registry (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS activity (event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, data TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS activity_time ON activity(timestamp DESC)')
            with db:
                yield db
        finally:
            db.close()

    def registry(self, fallback):
        if not self.path.is_file():
            return copy.deepcopy(fallback)
        with self.connect() as db:
            row = db.execute('SELECT data FROM registry WHERE id=1').fetchone()
        return json.loads(row[0]) if row else copy.deepcopy(fallback)

    @staticmethod
    def insert(db, events):
        for event in events:
            db.execute('INSERT OR IGNORE INTO activity(event_id,timestamp,data) VALUES (?,?,?)',
                       (event['event_id'], event['timestamp'], json.dumps(event, ensure_ascii=False, allow_nan=False)))

    def append(self, events):
        if not events:
            return
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.insert(db, events)

    def events(self, filters=None):
        filters = filters or {}
        if not self.path.is_file():
            return {'events': [], 'total': 0}
        with self.connect() as db:
            events = [json.loads(row[0]) for row in db.execute('SELECT data FROM activity ORDER BY timestamp DESC, rowid DESC')]
        for query, field in (('version', 'model_version'), ('run_id', 'run_id'),
                             ('event_type', 'event_type'), ('result', 'result')):
            if filters.get(query):
                events = [event for event in events if event.get(field) == filters[query]
                          or query == 'version' and any(isinstance(event.get(side), dict)
                              and event[side].get('version') == filters[query] for side in ('before', 'after'))]
        for boundary in ('from', 'to'):
            if not filters.get(boundary):
                continue
            value = str(filters[boundary])
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone(timedelta(hours=9)))
                if boundary == 'to' and len(value) == 10:
                    parsed += timedelta(days=1)
            except ValueError:
                raise ValueError('작업 이력 기간은 날짜 또는 ISO 시각이어야 합니다.') from None
            if boundary == 'from':
                events = [event for event in events if datetime.fromisoformat(event['timestamp']) >= parsed]
            elif len(value) == 10:
                events = [event for event in events if datetime.fromisoformat(event['timestamp']) < parsed]
            else:
                events = [event for event in events if datetime.fromisoformat(event['timestamp']) <= parsed]
        query = str(filters.get('q') or '').strip().casefold()
        if query:
            events = [event for event in events if query in json.dumps(event, ensure_ascii=False).casefold()]
        total = len(events)
        if filters.get('limit'):
            try:
                limit = int(filters['limit'])
            except (ValueError, TypeError):
                raise ValueError('작업 이력 개수는 정수여야 합니다.') from None
            if not 1 <= limit <= 10000:
                raise ValueError('작업 이력 개수는 1~10,000 범위여야 합니다.')
            events = events[:limit]
        return {'events': events, 'total': total}
