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
        token = CHECK_VERSIONS[check].split(' ', 1)[0]                      # 코드의 규칙 버전 토큰이 그 검사를 적은 같은 줄에
        assert re.search(rf'`{re.escape(check)}`[^\n]*`{re.escape(token)}`', text), (check, token)
    assert f'`{EXTRACT_VERSION}`' in text
    assert '## 18.' in text and '## 19.' in text and '## 20.' in text
    # §1 실행 상태: 옛 공통 상태 셋은 '옛 결과' 로, AIONA 가 지금도 내는 INCOMPLETE·OUT_OF_SCOPE 는 현재 상태로 적혀 있어야 한다
    row = next(line for line in text.splitlines() if line.startswith('| 실행 상태 |'))
    assert all(f'`{s}`' in row for s in ('INCOMPLETE', 'OUT_OF_SCOPE', 'CONFIG_MISSING', 'DISPLAY_MISSING', 'DUPLICATE'))
    assert row.index('`UNSUPPORTED`') > row.index('`OUT_OF_SCOPE`') and '옛 결과' in row[row.index('`UNSUPPORTED`'):]


def prose_after_section(text, heading):
    """heading 으로 시작하는 절(같은 수준의 다음 제목 전까지)을 뺀 나머지 뒷부분."""
    level = heading.split(' ', 1)[0]
    rest = text[text.index(heading) + len(heading):]
    match = re.search(rf'^{re.escape(level)} ', rest, re.MULTILINE)
    return rest[match.start():] if match else ''


def test_service_guide_documents_current_settings_and_env_keys():
    text = read('SERVICE_UI.md')
    env = env_values()
    for key in ('SEMANTIC_PIPELINE', 'SEMANTIC_DISPATCH', 'SEMANTIC_ANTHROPIC_BATCH_QUESTIONS', 'SEMANTIC_ANTHROPIC_BATCH_POLL_SECONDS',
                'SEMANTIC_ANTHROPIC_MODEL', 'SEMANTIC_ANTHROPIC_EFFORT'):
        assert key in text and key in env, key
    # Message Batches 호출 방식은 10-13 에 뺐다가 10-14 비용 결정으로 다시 넣음 — 예시 설정은 batch
    assert env['SEMANTIC_PIPELINE'] == 'legacy' and env['SEMANTIC_DISPATCH'] == 'batch' and env['SEMANTIC_ENGINE'] == 'anthropic_api'
    heading = next(line for line in text.splitlines() if line.startswith('### 현재 설정'))
    prose = text[text.index(heading):].split('```', 1)[0]                 # 예시 블록이 아니라 본문 문단에서
    assert f"SEMANTIC_ANTHROPIC_MODEL={env['SEMANTIC_ANTHROPIC_MODEL']}" in prose and f"SEMANTIC_ANTHROPIC_EFFORT={env['SEMANTIC_ANTHROPIC_EFFORT']}" in prose
    # 다른 절은 모델·effort 를 직접 적지 않고 현재 설정 절로 연결한다 (설정이 바뀌면 한 곳만 고친다)
    anchor = '#현재-설정'
    others = text[:text.index(heading)] + prose_after_section(text, heading)   # 현재 설정 절 자신은 값을 직접 적는다
    for line in others.splitlines():
        if '현재 평가자 설정은' in line or '현재 평가자는' in line:
            assert anchor in line, line[:80]
    # 낡은 '현재 설정' 문장이 남아 있으면 안 된다 (OpenAI·GPT CLI 는 대안 경로)
    for stale in ('현재 설정은 OpenAI API', '현재 연결은 OpenAI API', '현재 설정인 `openai_api`는'):
        assert stale not in text, stale
    body = text.split('### 공통 미판정 조건의 PASS/FAIL 보완')[1].split('\n', 1)[1]   # 제목 다음 본문
    assert body.lstrip().startswith('> **지금은 쓰지 않는 절차입니다.**') and '`code_completion` 을 만들지 않습니다' in body[:600]
    assert '/api/comparison/version-comparison' in text and '/api/comparison/aiona-history' in text and 'criteria_changed' in text


def test_implementation_map_paths_exist():
    text = read('IMPLEMENTATION_MAP.md')
    tokens = set()
    for line in text.splitlines():
        for cell in line.split('|'):   # 표의 칸 단위로 — '삭제'·'파일 없음' 을 설명하는 칸의 파일만 건너뛴다
            if any(word in cell for word in ('삭제', '미존재', '파일 없음')):
                continue
            tokens.update(t for t in re.findall(r'`([A-Za-z0-9_./-]+\.(?:py|js|json|html|css))`', cell)
                          if not t.startswith(('/', 'data/')))   # URL 경로·저장소에 없는 로컬 데이터는 제외
    by_name = {}
    for folder in ('code_eval', 'webapp', 'frontend/src', 'frontend', 'tests', 'examples'):   # 구현 지도가 가리키는 폴더만
        for path in (ROOT / folder).glob('*'):
            if path.is_file():
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
    latest = sorted((DOCS / '변경내역').glob('????-??-??-변경내역.md'))[-1].name   # 가장 최근 날짜의 변경내역
    history = read(f'변경내역/{latest}')
    assert REGISTRY_VERSION in history and JEV_CRITERIA_VERSION in history
    older = read('변경내역/2026-10-12-변경내역.md')
    assert '<a id="change-2026-10-12-26"></a>' in older and '| 26 |' in older
    audit = read('평가기준_통합감사_2026-10-12.md')
    # 10-12 역사 문서는 당시 규칙 버전(v16-1.1, 커밋 f523db2)을 그대로 두고 현재 버전은 MEASUREMENT 가 든다 — 현재 토큰과 묶지 않는다
    assert '2-1.literal v16-1.1' in audit and '규칙 v16-1.1' in older
    assert '## 9. 처리 결과' in audit and all(tag in audit for tag in ('D-1', 'D-2', 'G-1', 'G-2', 'G-3', 'G-4', 'G-5', 'G-6'))


def test_progress_document_commit_references_exist_in_git():
    progress = read('진행현황_2026-10-13.md')
    assert '## 3. 앞으로 할 것' in progress and '## 4. 정할 것' in progress
    table = progress.split('## 2.')[0]
    shas = re.findall(r'\| ([0-9a-f]{7}) \|', table)
    assert {'2f7ccd9', 'f1b3b62', '5fe032f', '52228cc', 'a1f1112'} <= set(shas)
    if not (ROOT / '.git').exists():
        pytest.skip('git checkout 이 아님')
    try:
        shallow = subprocess.run(['git', 'rev-parse', '--is-shallow-repository'], cwd=ROOT, capture_output=True, text=True)
    except OSError:
        pytest.skip('git 명령 없음')
    if shallow.returncode != 0 or shallow.stdout.strip() == 'true':
        pytest.skip('얕은 클론 — 과거 커밋을 확인할 수 없음')
    for sha in shas:
        probe = subprocess.run(['git', 'rev-parse', '--verify', '--quiet', f'{sha}^{{commit}}'], cwd=ROOT, capture_output=True, text=True)
        assert probe.returncode == 0 and probe.stdout.strip().startswith(sha), f'진행현황의 커밋 {sha} 가 git 에 없음'


def test_latest_design_header_counts_match_the_criteria_registry():
    """가장 최근 설계안 머리말의 기준 수가 코드의 기준 목록(evaluation_targets)과 같다 — 기준을 바꾸면 새 판 설계안도 맞춘다."""
    from code_eval.criteria_registry import evaluation_targets
    designs = sorted(DOCS.glob('평가체계_설계안_v*.html'), key=lambda p: tuple(int(x) for x in re.findall(r'v(\d+)\.(\d+)', p.name)[0]))
    text = designs[-1].read_text(encoding='utf-8')
    counts = evaluation_targets()['counts']
    code = re.search(r'코드 평가 <b>(\d+) 대분류 · (\d+) 소분류 · (\d+) 검사 · 세부 조건 \d+ · 질문 조건 (\d+)</b>', text)
    semantic = re.search(r'의미 평가 <b>(\d+) 기준 · (\d+) 세부 기준 · (\d+) 점검 항목</b>', text)
    assert code and semantic, designs[-1].name
    assert tuple(map(int, code.groups())) == (counts['code_categories'], counts['code_subcategories'], counts['code_checks'], counts['code_conditions'])
    assert tuple(map(int, semantic.groups())) == (counts['semantic_criteria'], counts['semantic_subs'], counts['semantic_items'])


def test_changelog_anchors_are_unique_and_every_index_row_points_to_one():
    """10-14 P1-5 — 변경내역 10-13 에 '#19' 앵커가 두 개라 목차 링크가 다른 항목을 가리키던 문제."""
    history = read('변경내역/2026-10-13-변경내역.md')
    anchors = re.findall(r'<a id="(change-2026-10-13-\d+)"></a>', history)
    assert len(anchors) == len(set(anchors))
    linked = re.findall(r'^\| \d+ \|.*\]\(#(change-2026-10-13-\d+)\)', history, flags=re.M)
    assert linked and set(linked) <= set(anchors) and set(anchors) <= set(linked)


def test_readme_states_the_current_semantic_item_counts():
    from code_eval.semantic_items import ITEMS, SUBS
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    assert f'세부 기준 {len(SUBS)}·점검 항목 {len(ITEMS)}' in readme


def test_handover_document_cites_the_current_versions():
    """10-14 4차 — 인수인계 문서의 버전 표가 코드 상수와 같다 (버전을 올리면 문서도 같이 고친다)."""
    from code_eval.attachment_images import JUDGE_IMAGE_POLICY_VERSION
    from code_eval.jev_applicability import JEV_APPLICABILITY_VERSION, SUB_APPLICABILITY_VERSION
    from code_eval.jev_eval import JEV_EVAL_VERSION
    from code_eval.long_context import LONG_CONTEXT_POLICY_VERSION
    from code_eval.multi_turn import MULTI_TURN_POLICY_VERSION
    from code_eval.semantic_items import ITEMS, SUBS
    from code_eval.semantic_policy import J5_OUTCOME_POLICY_VERSION
    from webapp.run_summary import COLLECTION_COMPARISON_VERSION, CRITERIA_VERIFICATION_VERSION, RUN_SUMMARY_VERSION
    from webapp.version_comparison import COMPARISON_VERSION
    text = read('인수인계_2026-10-14.md')
    for token in (ENGINE_VERSION, REGISTRY_VERSION, JEV_CRITERIA_VERSION, SEMANTIC_ITEMS_VERSION, JEV_APPLICABILITY_VERSION,
                  SUB_APPLICABILITY_VERSION, MULTI_TURN_POLICY_VERSION, LONG_CONTEXT_POLICY_VERSION, J5_OUTCOME_POLICY_VERSION,
                  JUDGE_IMAGE_POLICY_VERSION, JEV_EVAL_VERSION, RUN_SUMMARY_VERSION, COLLECTION_COMPARISON_VERSION,
                  CRITERIA_VERIFICATION_VERSION, COMPARISON_VERSION):
        assert f'`{token}`' in text, token
    assert f'({len(SUBS)}·{len(ITEMS)})' in text
