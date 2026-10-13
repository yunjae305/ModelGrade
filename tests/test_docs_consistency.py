"""문서 기준선이 코드와 어긋나지 않는지 — 버전 문자열·설정 키·구현 지도의 경로·변경 이력. 모델 호출 없음."""
import re
from pathlib import Path

from code_eval.core import CHECK_VERSIONS, ENGINE_VERSION
from code_eval.criteria_registry import CHANGELOG, REGISTRY_VERSION
from code_eval.extract import EXTRACT_VERSION
from code_eval.jev_criteria import JEV_CRITERIA_VERSION
from code_eval.semantic_items import SEMANTIC_ITEMS_VERSION

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / '참고자료'


def read(name):
    return (DOCS / name).read_text(encoding='utf-8')


def test_measurement_rules_cite_the_current_engine_and_rule_versions():
    text = read('MEASUREMENT_v16.md')
    assert ENGINE_VERSION.rsplit('-', 1)[-1] in text                      # v16.3.0
    for check, prefix in (('3-1.answer_equation', 'v16-1.4'), ('2-2.list', 'v16-1.3'), ('3-3.transform', 'v17-1.4')):
        assert CHECK_VERSIONS[check].startswith(prefix) and f'`{prefix}`' in text, check
    assert EXTRACT_VERSION in text
    assert '## 18.' in text and '## 19.' in text and '## 20.' in text
    assert 'UNSUPPORTED` · `DEPENDENCY_FAILED` · `APPLICABILITY_EXCEPTION` · `INCOMPLETE` 는 저장된 옛 결과' in text


def test_service_guide_documents_current_settings_and_env_keys():
    text = read('SERVICE_UI.md')
    env = (ROOT / '.env.example').read_text(encoding='utf-8')
    for key in ('SEMANTIC_PIPELINE', 'SEMANTIC_DISPATCH', 'SEMANTIC_ANTHROPIC_BATCH_QUESTIONS', 'SEMANTIC_ANTHROPIC_BATCH_POLL_SECONDS',
                'SEMANTIC_ANTHROPIC_MODEL', 'SEMANTIC_ANTHROPIC_EFFORT'):
        assert key in text and re.search(rf'^{key}=', env, re.M), key
    assert re.search(r'^SEMANTIC_PIPELINE=legacy$', env, re.M) and re.search(r'^SEMANTIC_DISPATCH=realtime$', env, re.M)
    assert '현재 설정 (2026-10-12): Claude API · Claude Opus 5.5' in text and 'SEMANTIC_ANTHROPIC_MODEL=claude-opus-5-5' in env
    assert '2026-10-13 엔진 v16.3.0 에서 제거' in text and '대안 경로 — 현재 설정은 Claude API' in text
    assert '/api/comparison/version-comparison' in text and 'criteria_changed' in text


def test_implementation_map_paths_exist():
    text = read('IMPLEMENTATION_MAP.md')
    paths = set(re.findall(r'`((?:code_eval|webapp|frontend/src|tests|examples)/[A-Za-z0-9_./-]+\.(?:py|js|json|html|css))`', text))
    missing = sorted(p for p in paths if not (ROOT / p).exists())
    assert not missing, missing
    assert '## 10.' in text and '## 11.' in text and '## 12.' in text


def test_criteria_changelog_matches_registry_and_docs():
    assert CHANGELOG[-1]['version'] == REGISTRY_VERSION
    assert any(entry['version'] == SEMANTIC_ITEMS_VERSION for entry in CHANGELOG)
    assert all({'version', 'date', 'scope', 'changes'} <= set(entry) and entry['changes'] for entry in CHANGELOG)
    history = read('변경내역/2026-10-13-변경내역.md')
    assert REGISTRY_VERSION in history and JEV_CRITERIA_VERSION.startswith('jev-criteria')
    older = read('변경내역/2026-10-12-변경내역.md')
    assert '<a id="change-2026-10-12-26"></a>' in older and '| 26 |' in older
    audit = read('평가기준_통합감사_2026-10-12.md')
    assert '## 9. 처리 결과' in audit and all(tag in audit for tag in ('D-1', 'D-2', 'G-1', 'G-2', 'G-3', 'G-4', 'G-5', 'G-6'))


def test_progress_document_tracks_the_changelog_entries():
    progress = read('진행현황_2026-10-13.md')
    for commit in ('2f7ccd9', 'f1b3b62', '5fe032f', '52228cc'):
        assert commit in progress, commit
    assert '## 3. 앞으로 할 것' in progress and '## 4. 정할 것' in progress
