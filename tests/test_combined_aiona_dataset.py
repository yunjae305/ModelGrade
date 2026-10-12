"""The merged dataset must preserve source Q/A and independent conversation histories."""
import json
from collections import Counter

import pytest

from code_eval.engine import evaluate_batch
from code_eval.jev_eval import conversation_contexts, jev_after_code
from webapp.code_eval_service import ROOT, CodeEvalService
from webapp.comparison_service import ComparisonService, DEFAULT_COMPARISON_DATASET, normalize_dataset

DATA = ROOT / 'data/code_eval'
SOURCE = DATA / 'AIONA_winz_멀티턴_1-3회차_통합.json'


@pytest.fixture(scope='module')
def merged():
    if not DEFAULT_COMPARISON_DATASET.is_file():
        pytest.skip('Optional private question dataset is not included in public checkouts')
    return json.loads(DEFAULT_COMPARISON_DATASET.read_text(encoding='utf-8'))


def test_original_q100_and_all_multiturn_qa_are_preserved(merged):
    original_path = DATA / 'AIONA_winz_set2_Q100_id_question_answer_v1.json'
    if not original_path.is_file() or not SOURCE.is_file():
        pytest.skip('Optional private source datasets are not included in public checkouts')
    original = json.loads(original_path.read_text(encoding='utf-8'))
    groups = json.loads(SOURCE.read_text(encoding='utf-8-sig'))
    assert len(merged) == 165
    assert merged[:100] == original
    assert [r['id'] for r in merged] == [f'Q{i:03d}' for i in range(1, 166)]
    offset = 100
    for ordinal, (group, rows) in enumerate(groups.items(), 1):
        appended = merged[offset:offset + len(rows)]
        assert [{k: r[k] for k in source} for r, source in zip(appended, rows)] == rows
        assert all(r['conversation_id'] == f'AIONA_MT_{ordinal:02d}' for r in appended)
        assert all(r['source'] == SOURCE.name and r['source_group'] == group for r in appended)
        assert [r['turn'] for r in appended] == list(range(1, len(rows) + 1))
        offset += len(rows)
    assert offset == len(merged)


def test_default_workspace_upload_accepts_all_165_cases(merged, tmp_path):
    sample = ComparisonService(CodeEvalService(save_dir=tmp_path / 'eval'), save_dir=tmp_path / 'compare').sample()
    assert sample['name'] == DEFAULT_COMPARISON_DATASET.name
    assert sample['cases'] == normalize_dataset({'cases': merged})
    assert Counter(r['conversation_id'] for r in sample['cases'] if 'conversation_id' in r) == {
        'AIONA_MT_01': 15, 'AIONA_MT_02': 20, 'AIONA_MT_03': 30}


def test_long_context_routes_62_later_turns_without_crossing_conversations(merged):
    batch = evaluate_batch(merged)
    assert all(c['case_status'] == 'EVALUATED' for c in batch['cases'])
    contexts = conversation_contexts(batch['cases'])
    assert len(contexts) == 65
    assert all(c['complete'] and not c['issues'] for c in contexts.values())
    semantic = jev_after_code(batch)  # pytest forces a resolver with no key; never calls a model
    assert semantic['summary']['J6']['targets'] == 62
    assert semantic['completed'] == 0
    for index, ctx in contexts.items():
        row = merged[index]
        prior = [(r['turn'], r['question'], r['answer']) for r in merged
                 if r.get('conversation_id') == row['conversation_id'] and r.get('turn', 0) < row['turn']]
        assert ctx['prior'] == prior
        record = semantic['records'][index]
        assert record['context']['history_turns'] == row['turn'] - 1
        assert ('J6' in record['criteria']) == (row['turn'] > 1)
        assert batch['cases'][index]['answer'] == row['answer']
    assert semantic['history_policy'] == 'full_original_text'
    for index, prior_count in [(114, 14), (134, 19), (164, 29)]:
        context = semantic['records'][index]['context']
        assert context['history_turns'] == prior_count
        assert context['history_complete'] is True
        assert context['history_truncated'] is False
