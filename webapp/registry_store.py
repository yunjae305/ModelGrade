"""모델 버전 목록 저장소 — SQLite 한 테이블(registry)에 현재 목록을 트랜잭션으로 저장한다.

레거시 JSON(aiona_versions.json)은 첫 변경 때 가져오고 그대로 둔다. SQLite 는 스레드·프로세스 사이의 쓰기를 직렬화한다.
예전 파일에 남아 있을 수 있는 activity 테이블(작업 이력, 2026-10-13 제거)은 읽지도 쓰지도 않는다.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
from contextlib import contextmanager
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


class RegistryStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS registry (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)')
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
