"""문서 기준선이 코드와 어긋나지 않는지 — 버전 문자열·설정 키·구현 지도의 경로·변경 이력. 모델 호출 없음.

문장 그대로가 아니라 사실(버전 토큰·설정 값·파일 존재·커밋 존재)을 대조한다. 문구를 고쳐도 사실이 같으면 통과한다.
"""
import re
import subprocess
from pathlib import Path

import pytest

from code_eval.core import CHECK_VERSIONS, ENGINE_VERSION
from code_eval.criteria_registry import CHANGELOG, REGISTRY_VERSION
from code_eval.extract import EXTRACT_VERSION
from code_eval.jev_criteria import JEV_CRITERIA_VERSION
from code_eval.semantic_items import SEMANTIC_ITEMS_VERSION

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / '참고자료'


def read(name):
    return (DOCS / name).read_text(encoding='utf-8')


def env_values():
    env = (ROOT / '.env.example').read_text(encoding='utf-8')
    return dict(re.findall(r'^([A-Z0-9_]+)=(.*)$', env, re.M))


def test_measurement_rules_cite_the_current_engine_and_rule_versions():
    text = read('MEASUREMENT_v16.md')
    assert ENGINE_VERSION.rsplit('-', 1)[-1] in text                      # v16.3.0
    for check in ('2-1.literal', '3-1.answer_equation', '2-2.list', '3-3.transform', '3-3.unique_values', '3-3.aggregate'):
        token = CHECK_VERSIONS[check].split(' ', 1)[0]                      # 코드의 규칙 버전 토큰이 문서에 그대로
        assert f'`{token}`' in text, (check, token)
    assert f'`{EXTRACT_VERSION}`' in text
    assert '## 18.' in text and '## 19.' in text and '## 20.' in text
    # §1 실행 상태: 옛 공통 상태 셋은 '옛 결과' 로, AIONA 가 지금도 내는 INCOMPLETE·OUT_OF_SCOPE 는 현재 상태로 적혀 있어야 한다
    row = next(line for line in text.splitlines() if line.startswith('| 실행 상태 |'))
    assert all(f'`{s}`' in row for s in ('INCOMPLETE', 'OUT_OF_SCOPE', 'CONFIG_MISSING', 'DISPLAY_MISSING', 'DUPLICATE'))
    assert row.index('`UNSUPPORTED`') > row.index('`OUT_OF_SCOPE`') and '옛 결과' in row[row.index('`UNSUPPORTED`'):]


def test_service_guide_documents_current_settings_and_env_keys():
    text = read('SERVICE_UI.md')
    env = env_values()
    for key in ('SEMANTIC_PIPELINE', 'SEMANTIC_DISPATCH', 'SEMANTIC_ANTHROPIC_BATCH_QUESTIONS', 'SEMANTIC_ANTHROPIC_BATCH_POLL_SECONDS',
                'SEMANTIC_ANTHROPIC_MODEL', 'SEMANTIC_ANTHROPIC_EFFORT'):
        assert key in text and key in env, key
    assert env['SEMANTIC_PIPELINE'] == 'legacy' and env['SEMANTIC_DISPATCH'] == 'realtime' and env['SEMANTIC_ENGINE'] == 'anthropic_api'
    heading = next(line for line in text.splitlines() if line.startswith('### 현재 설정'))
    section = text[text.index(heading):]
    assert f"SEMANTIC_ANTHROPIC_MODEL={env['SEMANTIC_ANTHROPIC_MODEL']}" in section and f"SEMANTIC_ANTHROPIC_EFFORT={env['SEMANTIC_ANTHROPIC_EFFORT']}" in section
    # 낡은 '현재 설정' 문장이 남아 있으면 안 된다 (OpenAI·GPT CLI 는 대안 경로)
    for stale in ('현재 설정은 OpenAI API', '현재 연결은 OpenAI API', '현재 설정인 `openai_api`는'):
        assert stale not in text, stale
    assert '제거' in text.split('### 공통 미판정 조건의 PASS/FAIL 보완')[1][:400]   # C 보완 절은 제거 안내로 시작
    assert '/api/comparison/version-comparison' in text and 'criteria_changed' in text


def test_implementation_map_paths_exist():
    text = read('IMPLEMENTATION_MAP.md')
    tokens = set()
    for line in text.splitlines():
        if any(word in line for word in ('삭제', '미존재', '파일 없음')):
            continue   # 지운 파일·없는 로컬 자료를 설명하는 줄
        tokens.update(t for t in re.findall(r'`([A-Za-z0-9_./-]+\.(?:py|js|json|html|css))`', line)
                      if not t.startswith(('/', 'data/')))   # URL 경로·저장소에 없는 로컬 데이터는 제외
    by_name = {}
    for path in ROOT.rglob('*'):
        if path.is_file() and path.suffix in ('.py', '.js', '.json', '.html', '.css') and '.git' not in path.parts and 'node_modules' not in path.parts:
            by_name.setdefault(path.name, []).append(path)
    missing = sorted(t for t in tokens if not ((ROOT / t).exists() or ('/' not in t and t in by_name)))
    assert not missing, missing
    assert '## 10.' in text and '## 11.' in text and '## 12.' in text


def test_criteria_changelog_matches_registry_and_docs():
    registry_entries = [entry for entry in CHANGELOG if entry['scope'] == 'registry']
    assert registry_entries and registry_entries[-1]['version'] == REGISTRY_VERSION
    assert any(entry['version'] == SEMANTIC_ITEMS_VERSION for entry in CHANGELOG)
    assert any(entry['version'].endswith(ENGINE_VERSION.rsplit('-', 1)[-1]) for entry in CHANGELOG)
    assert all({'version', 'date', 'scope', 'changes'} <= set(entry) and entry['changes'] for entry in CHANGELOG)
    history = read('변경내역/2026-10-13-변경내역.md')
    assert REGISTRY_VERSION in history and JEV_CRITERIA_VERSION in history
    older = read('변경내역/2026-10-12-변경내역.md')
    assert '<a id="change-2026-10-12-26"></a>' in older and '| 26 |' in older
    literal = CHECK_VERSIONS['2-1.literal'].split(' ', 1)[0]
    assert literal in older and literal in read('평가기준_통합감사_2026-10-12.md')
    audit = read('평가기준_통합감사_2026-10-12.md')
    assert '## 9. 처리 결과' in audit and all(tag in audit for tag in ('D-1', 'D-2', 'G-1', 'G-2', 'G-3', 'G-4', 'G-5', 'G-6'))


def test_progress_document_commit_references_exist_in_git():
    progress = read('진행현황_2026-10-13.md')
    assert '## 3. 앞으로 할 것' in progress and '## 4. 정할 것' in progress
    table = progress.split('## 2.')[0]
    shas = re.findall(r'\| ([0-9a-f]{7}) \|', table)
    assert {'2f7ccd9', 'f1b3b62', '5fe032f', '52228cc', 'a1f1112'} <= set(shas)
    if not (ROOT / '.git').exists():
        pytest.skip('git checkout 이 아님')
    for sha in shas:
        probe = subprocess.run(['git', 'cat-file', '-e', f'{sha}^{{commit}}'], cwd=ROOT, capture_output=True)
        assert probe.returncode == 0, f'진행현황의 커밋 {sha} 가 git 에 없음'
