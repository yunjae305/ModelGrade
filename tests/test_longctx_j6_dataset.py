"""롱 컨텍스트(J6) 대화 세트의 형식·턴 순서·중복 ID·기대 적용·인용 일치·업로드 호환·J6 라우팅을 모델 호출 없이 검사한다.

세트 파일: examples/AIONA_winz_longctx_J6_v1.json (질문 행, AIONA 답변은 수집 전까지 빈 문자열)
기대 적용: examples/AIONA_winz_longctx_J6_v1.expect.json (턴별 applies·anchors, 테스트와 사람 검토만 읽음)
안내: 참고자료/LONGCTX_J6_DATASET.md
"""
import json
import re
from collections import Counter

import pytest

from code_eval.engine import evaluate_batch
from code_eval.jev_eval import conversation_contexts, jev_after_code
from webapp.code_eval_service import ROOT
from webapp.comparison_service import (DEFAULT_COMPARISON_DATASET, PUBLIC_COMPARISON_DATASET,
                                       import_dataset, normalize_dataset)

QUESTIONS = ROOT / 'examples' / 'AIONA_winz_longctx_J6_v1.json'
EXPECT = ROOT / 'examples' / 'AIONA_winz_longctx_J6_v1.expect.json'
# 기존 질문 세트와 ID 가 겹치지 않아야 한다 (비공개 파일이라 있을 때만 검사).
EXISTING_DATASETS = [DEFAULT_COMPARISON_DATASET,
                     ROOT / 'data' / 'code_eval' / 'AIONA_winz_단일28_멀티10-30_질문30_id_turns.json']

ROW_KEYS = {'id', 'conversation_id', 'turn', 'question', 'answer', 'system_id', 'source', 'source_group'}
ITEMS = ['J6-1.a', 'J6-2.a', 'J6-2.b', 'J6-2.c', 'J6-3.a', 'J6-3.b', 'J6-4.a', 'J6-4.b']
GROUPS = {'J6-1', 'J6-2', 'J6-3', 'J6-4'}
ID_PATTERN = re.compile(r'LC_J6[1-4]_\d{2}_T\d+')
CONVERSATION_PATTERN = re.compile(r'AIONA_LC_J6[1-4]_\d{2}')
# 설계 기준표의 PASS/FAIL 예시 소재는 평가 프롬프트에 들어가므로 세트에 쓰지 않는다.
DESIGN_EXAMPLE_KEYWORDS = ['헬스장 안내 데스크', '11월 7일', '몰티즈', '송년회 장기자랑', '반상회', '민호', '독서 모임', '이력서에 사진']
# 형식·분량 지시("앞으로 모든 답변은 세 문장으로")는 코드 평가 몫이라 J6-1.a 의 근거로 쓰지 않는다.
FORMAT_INSTRUCTION = re.compile(r'(앞으로|모든 답변|매 답변).*(문장|글자|줄|형식|표|JSON|마크다운|개 이내)')


@pytest.fixture(scope='module')
def rows():
    return json.loads(QUESTIONS.read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def expect():
    return json.loads(EXPECT.read_text(encoding='utf-8'))


def _conversations(rows):
    """입력 순서대로 {conversation_id: [행…]} — 대화가 파일 안에서 끊기면 같은 ID 가 두 번 시작된다."""
    grouped = {}
    for row in rows:
        grouped.setdefault(row['conversation_id'], []).append(row)
    return grouped


def _turn_items(expect):
    for cid, conversation in expect['conversations'].items():
        for turn, item in conversation['turns'].items():
            yield cid, int(turn), item


def test_rows_are_flat_conversation_rows_with_expected_keys(rows):
    assert isinstance(rows, list) and rows
    sample_keys = set()
    for row in json.loads(PUBLIC_COMPARISON_DATASET.read_text(encoding='utf-8')):
        sample_keys |= set(row)
    assert sample_keys <= ROW_KEYS  # 공개 예제의 행 키를 모두 포함 (형식 호환)
    for row in rows:
        assert set(row) == ROW_KEYS, row.get('id')
        assert row['system_id'] == 'aiona'
        assert row['source'] == QUESTIONS.name
        assert row['source_group'] in GROUPS
        assert isinstance(row['question'], str) and row['question'].strip()
        assert isinstance(row['answer'], str)  # 초안은 '' (AIONA 답변은 수집 후 채움)
        assert isinstance(row['turn'], int) and row['turn'] >= 1
        assert 'reference' not in row


def test_ids_follow_pattern_and_are_unique(rows):
    ids = [row['id'] for row in rows]
    assert len(set(ids)) == len(ids)
    for row in rows:
        assert ID_PATTERN.fullmatch(row['id']), row['id']
        assert CONVERSATION_PATTERN.fullmatch(row['conversation_id']), row['conversation_id']
        prefix = row['conversation_id'].removeprefix('AIONA_')
        assert row['id'] == f"{prefix}_T{row['turn']}"
        assert row['source_group'] == 'J6-' + row['conversation_id'][len('AIONA_LC_J6')]
    conversation_ids = set(row['conversation_id'] for row in rows)
    for path in EXISTING_DATASETS:
        if not path.is_file():
            continue  # 비공개 세트는 공개 체크아웃에 없다
        existing = normalize_dataset({'input_text': path.read_text(encoding='utf-8-sig'), 'input_name': path.name})
        assert not set(ids) & {case['case_id'] for case in existing}, path.name
        assert not conversation_ids & {case.get('conversation_id') for case in existing}, path.name


def test_turns_are_contiguous_and_within_5_to_12(rows):
    grouped = _conversations(rows)
    starts = []
    previous = None
    for row in rows:
        if row['conversation_id'] != previous:
            starts.append(row['conversation_id'])
            previous = row['conversation_id']
    assert len(starts) == len(set(starts)), '대화가 파일 안에서 끊겨 있음'
    for cid, members in grouped.items():
        turns = [row['turn'] for row in members]
        assert turns == list(range(1, len(turns) + 1)), cid
        assert 5 <= len(turns) <= 12, cid
    by_group = Counter(cid[len('AIONA_LC_'):len('AIONA_LC_J6x')] for cid in grouped)
    assert set(by_group) == {'J61', 'J62', 'J63', 'J64'}
    assert all(count >= 6 for count in by_group.values()), by_group
    assert len(grouped) >= 24
    twelve = Counter(cid[len('AIONA_LC_'):len('AIONA_LC_J6x')] for cid, members in grouped.items() if len(members) == 12)
    assert all(count <= 1 for count in twelve.values()), twelve  # 12턴 대화는 세부 기준당 최대 1개


def test_upload_path_accepts_file_unchanged(rows, expect):
    text = QUESTIONS.read_text(encoding='utf-8')
    imported = import_dataset({'input_text': text, 'input_name': QUESTIONS.name})
    assert imported['import']['converted'] is False
    cases = imported['cases']
    assert len(cases) == len(rows)
    for case, row in zip(cases, rows):
        assert case['case_id'] == row['id']
        assert (case['question'], case['answer'], case['conversation_id'], case['turn']) == (
            row['question'], row['answer'], row['conversation_id'], row['turn'])
    assert normalize_dataset({'cases': rows}) == cases  # ValueError 없이 같은 결과
    assert Counter(case['conversation_id'] for case in cases) == Counter(
        row['conversation_id'] for row in rows)
    assert set(Counter(case['conversation_id'] for case in cases)) == set(expect['conversations'])


def test_j6_routing_counts_every_later_turn_without_model_calls(rows):
    batch = evaluate_batch(rows)
    assert all(case['case_status'] == 'EVALUATED' for case in batch['cases'])
    contexts = conversation_contexts(batch['cases'])
    assert len(contexts) == len(rows)
    assert all(ctx['complete'] and not ctx['issues'] for ctx in contexts.values())
    semantic = jev_after_code(batch)  # conftest 가 키 없는 resolver 로 고정: 모델을 부르지 않는다
    later_turns = sum(1 for row in rows if row['turn'] > 1)
    assert semantic['summary']['J6']['targets'] == later_turns
    assert semantic['completed'] == 0
    assert semantic['history_policy'] == 'full_original_text'
    for index, ctx in contexts.items():
        row = rows[index]
        prior = [(r['turn'], r['question'], r['answer']) for r in rows
                 if r['conversation_id'] == row['conversation_id'] and r['turn'] < row['turn']]
        assert ctx['prior'] == prior
        record = semantic['records'][index]
        assert record['context']['history_turns'] == row['turn'] - 1
        assert ('J6' in record['criteria']) == (row['turn'] > 1)


def test_expect_file_matches_rows_and_quotes_are_verbatim(rows, expect):
    assert expect['dataset'] == QUESTIONS.name
    assert expect['items'] == ITEMS
    by_turn = {(row['conversation_id'], row['turn']): row for row in rows}
    later = {key for key in by_turn if key[1] >= 2}
    assert {(cid, turn) for cid, turn, _ in _turn_items(expect)} == later  # 1턴 항목 없음, 누락·초과 없음
    for cid, conversation in expect['conversations'].items():
        assert conversation['primary'] == 'J6-' + cid[len('AIONA_LC_J6')]
        assert conversation['topic'].strip()
    for cid, turn, item in _turn_items(expect):
        label = f'{cid} T{turn}'
        assert item['case_id'] == by_turn[(cid, turn)]['id'], label
        assert set(item) == {'case_id', 'applies', 'not_applicable', 'anchors', 'note'}, label
        applies = item['applies']
        assert len(set(applies)) == len(applies) and set(applies) <= set(ITEMS), label
        assert set(item['not_applicable']) <= set(ITEMS), label
        assert not set(applies) & set(item['not_applicable']), label
        assert set(item['anchors']) == set(applies), label  # applies 마다 anchor 가 있고, 밖의 항목에는 없다
        for key, anchors in item['anchors'].items():
            assert anchors, f'{label} {key}'
            for anchor in anchors:
                assert 1 <= anchor['turn'] <= turn, f'{label} {key}'
                assert anchor['quote'] in by_turn[(cid, anchor['turn'])]['question'], f'{label} {key}: {anchor["quote"]}'
        if 'J6-4.b' in applies:
            assert item['not_applicable'].get('J6-4.a'), label  # 반박 턴 → J6-4.a 해당 없음


def test_each_item_has_at_least_30_expected_applications(expect):
    counts = Counter(key for _, _, item in _turn_items(expect) for key in item['applies'])
    assert all(counts[key] >= 30 for key in ITEMS), dict(counts)
    for cid, conversation in expect['conversations'].items():
        primary = conversation['primary'] + '.'
        turns = conversation['turns'].values()
        assert sum(1 for item in turns if any(key.startswith(primary) for key in item['applies'])) >= 5, cid
        assert any(not item['applies'] for item in turns), f'{cid}: 중립 턴 없음'


def test_no_design_example_scenarios_and_no_format_instruction_anchors(rows, expect):
    for row in rows:
        for keyword in DESIGN_EXAMPLE_KEYWORDS:
            assert keyword not in row['question'], (row['id'], keyword)
    for cid, turn, item in _turn_items(expect):
        for anchor in item['anchors'].get('J6-1.a', []):
            assert not FORMAT_INSTRUCTION.search(anchor['quote']), (cid, turn, anchor['quote'])
    seen = {}
    for row in rows:
        other = seen.setdefault(row['question'], row['conversation_id'])
        assert other == row['conversation_id'], f"같은 질문이 두 대화에 있음: {row['id']}"
