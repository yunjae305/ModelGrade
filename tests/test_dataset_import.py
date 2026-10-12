"""Source-preserving uploads; no providers or semantic models are called."""
import json
from collections import Counter
from pathlib import Path

import pytest

from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService, import_dataset, normalize_dataset

ROOT = Path(__file__).resolve().parent.parent


def uploaded(text, name='qa.md'):
    return import_dataset({'input_text': text, 'input_name': name})


def test_original_grouped_json_keeps_all_65_answers_and_separate_histories():
    path = ROOT / 'data/code_eval/AIONA_winz_멀티턴_1-3회차_통합.json'
    if not path.is_file():
        pytest.skip('Optional private question dataset is not included in public checkouts')
    text = path.read_text(encoding='utf-8')
    groups = json.loads(text)
    result = uploaded(text, path.name)
    assert result['import']['converted']
    assert result['import']['original_text'] == text
    assert len(result['cases']) == 65
    assert Counter(c['conversation_id'] for c in result['cases']) == {
        'AIONA_MT_01': 15, 'AIONA_MT_02': 20, 'AIONA_MT_03': 30}
    assert [(c['question'], c['answer'], c['turn']) for c in result['cases']] == [
        (c['question'], c['answer'], c['turn']) for group in groups.values() for c in group]
    assert sum(c['turn'] > 1 for c in result['cases']) == 62


def test_canonical_165_dataset_and_jsonl_remain_unchanged():
    path = ROOT / 'data/code_eval/AIONA_winz_Q165_Q100_multiturn65_v1.json'
    if not path.is_file():
        pytest.skip('Optional private question dataset is not included in public checkouts')
    text = path.read_text(encoding='utf-8')
    source = json.loads(text)
    result = uploaded(text, path.name)
    assert not result['import']['converted']
    assert [(c['question'], c['answer']) for c in result['cases']] == [(c['question'], c['answer']) for c in source]
    assert sum('conversation_id' not in c for c in result['cases']) == 100
    jsonl = '\r\n'.join(json.dumps(c, ensure_ascii=False) for c in source)
    assert uploaded(jsonl, 'qa.jsonl')['cases'] == result['cases']


@pytest.mark.parametrize('q,a', [('Q:', 'A:'), ('질문:', '답변:'), ('User:', 'Assistant:'),
                                ('Human:', 'AIONA:'), ('## 질문 1', '## 답변 1'),
                                ('**질문 1:**', '**답변 1:**'), ('1. Q:', '1. A:')])
def test_markers_preserve_full_unicode_crlf_and_code_blocks(q, a):
    question = ' 질문 원문🙂\r\n추가 조건\r\n'
    answer = '  답변 원문\r\n' + ('한글🙂' * 24000) + '\r\n```text\r\nQ: 코드 안 질문\r\nA: 코드 안 답변\r\n# 2회차\r\n```\r\n끝  '
    text = '\ufeff' + q + '\r\n' + question + a + '\r\n' + answer
    result = uploaded(text)
    row = result['cases'][0]
    assert row['question'] == question and row['answer'] == answer
    for field, (start, end) in row['import_span'].items():
        assert text[start:end] == row[field]
    assert result['import']['original_text'] == text
    assert 'conversation_id' not in row


def test_mixed_single_turn_and_rounds_convert_to_correct_groups():
    text = '# 자료 제목\n# 단일턴\nQ: 독립 1\nA: 답1\nQ: 독립2\nA: 답2\n'
    text += '# 1회차\nQ: 첫턴\nA: 이전 답변\nQ: 후속\nA: 후속 답변\n'
    text += '# 2회차\nQ: 새 대화\nA: 새 답변\n'
    result = uploaded(text)
    rows = result['cases']
    assert len(rows) == 5
    assert all('conversation_id' not in row for row in rows[:2])
    assert [(row['conversation_id'], row['turn']) for row in rows[2:]] == [
        ('AIONA_MT_01', 1), ('AIONA_MT_01', 2), ('AIONA_MT_02', 1)]
    assert result['import']['notes']
    assert result['import']['original_text'] == text
    for row in rows:
        for field, (start, end) in row['import_span'].items():
            assert row[field] == text[start:end]


@pytest.mark.parametrize('fields', [('질문', '답변'), ('Q', 'A'), ('prompt', 'response')])
def test_json_aliases_keep_values_exactly(fields):
    source = {fields[0]: '  Q\r\n', fields[1]: '  A\r\n```x```  '}
    result = uploaded(json.dumps([source]), 'export.txt')
    assert result['import']['converted']
    assert result['cases'][0]['question'] == source[fields[0]]
    assert result['cases'][0]['answer'] == source[fields[1]]


@pytest.mark.parametrize('text', [
    '구분 없는 일반 문서입니다.', 'Q: 질문만', 'A: 질문 없는 답변',
    'Q: 첫질문\nQ: 답변 없는 다음질문\nA: 답변',
    'Q: 질문\nA: 첫답변\nA: 중복 답변',
    'Q: 질문\nA: 답변\n```\n닫히지 않은 코드',
    '```\nQ: 본문이 아닌 코드 예시\nA: 예시\n```',
])
def test_ambiguous_text_is_not_silently_guessed(text):
    with pytest.raises(ValueError):
        uploaded(text)


@pytest.mark.parametrize('source', [
    {'Q': 'first', 'question': 'different', 'A': 'answer'},
    {'1회차': [{'question': 'q', 'answer': 'a', 'conversation_id': 'different'}]},
    {'단일턴': [{'question': 'q', 'answer': 'a', 'turn': 1}]},
    {'1회차': [{'question': 'q', 'answer': 'a', 'turn': 2}]},
    {'1회차': {'question': 'q', 'answer': 'a'}},
    {'Q': 'q', 'A': {'text': 'object is not a raw answer string'}},
])
def test_ambiguous_structured_values_are_not_coerced(source):
    with pytest.raises(ValueError):
        uploaded(json.dumps(source))


def test_invalid_json_does_not_fall_back_to_text_and_drop_content():
    with pytest.raises(ValueError, match='JSON'):
        uploaded('[{"question":"q","answer":"a"},\nBROKEN]', 'broken.json')
    with pytest.raises(ValueError):
        uploaded('{"question":"q","answer":NaN}', 'broken.json')


def test_markdown_json_fence_preserves_json_strings():
    source = {'question': '  Q\r\n', 'answer': '코드 ``` 원문  '}
    text = '```json\n' + json.dumps([source]) + '\n```\n'
    result = uploaded(text)
    assert result['import']['format'] == 'Markdown JSON'
    assert result['import']['original_text'] == text
    assert result['cases'][0]['question'] == source['question']
    assert result['cases'][0]['answer'] == source['answer']


def test_id_turns_preserves_text_source_ids_and_separate_histories():
    source = [
        {'id': 1, 'label': '단일 원본', 'turns': [
            {'turn': 1, 'question': '  질문\r\n🙂', 'answer': '\n```python\nx = 1\n```  '}]},
        {'id': 29, 'turns': [
            {'turn': 1, 'id': 29, 'question': '첫 질문', 'answer': ' 첫 답변\r\n'},
            {'turn': 2, 'question': '후속 질문\n', 'answer': '두 번째 답변  '}]},
        {'id': '30', 'turns': [
            {'turn': 1, 'Q': '다른 대화', 'A': '다른 답변'},
            {'turn': 2, 'question': '다른 후속', 'answer': '마지막 답변'}]},
    ]
    text = json.dumps(source, ensure_ascii=False)
    result = uploaded(text, 'arbitrary-export.json')
    rows = result['cases']
    assert result['import']['converted']
    assert result['import']['format'] == 'id/turns JSON'
    assert result['import']['original_text'] == text
    assert [(r['question'], r['answer']) for r in rows] == [
        (t.get('question', t.get('Q')), t.get('answer', t.get('A')))
        for group in source for t in group['turns']]
    assert [r['case_id'] for r in rows] == ['Q1_T1', 'Q29_T1', 'Q29_T2', 'Q30_T1', 'Q30_T2']
    assert 'conversation_id' not in rows[0] and 'turn' not in rows[0]
    assert [(r['conversation_id'], r['turn']) for r in rows[1:]] == [
        ('AIONA_ID_29', 1), ('AIONA_ID_29', 2), ('AIONA_ID_30', 1), ('AIONA_ID_30', 2)]
    assert [r['import_source'] for r in rows] == [
        {'group': {k: v for k, v in group.items() if k != 'turns'}, 'turn': t['turn']}
        for group in source for t in group['turns']]
    assert rows[1]['id'] == 29
    assert uploaded(json.dumps({'cases': source}), 'wrapper.json')['cases'] == rows
    assert uploaded(json.dumps(rows), 'exported.json')['cases'] == rows
    assert uploaded(text, 'another-name.json')['cases'] == rows


@pytest.mark.parametrize('source, message', [
    ([{'id': 1, 'turns': []}], '비어 있지 않은 배열'),
    ([{'id': 1, 'turns': {}}], '비어 있지 않은 배열'),
    ([{'id': True, 'turns': [{'turn': 1}]}], 'id는'),
    ([{'id': '', 'turns': [{'turn': 1}]}], 'id는'),
    ([{'id': 1.5, 'turns': [{'turn': 1}]}], 'id는'),
    ([{'turns': [{'turn': 1}]}], 'id와 turns'),
    ([{'id': 1, 'turns': [{'turn': 1}]}, {'id': ' 1 ', 'turns': [{'turn': 1}]}], 'id가 중복'),
    ([{'id': 1, 'turns': [None]}], '객체여야'),
    ([{'id': 1, 'turns': [{}]}], '순서대로'),
    ([{'id': 1, 'turns': [{'turn': True}]}], '순서대로'),
    ([{'id': 1, 'turns': [{'turn': '1'}]}], '순서대로'),
    ([{'id': 1, 'turns': [{'turn': 1.0}]}], '순서대로'),
    ([{'id': 1, 'turns': [{'turn': 1}, {'turn': 1}]}], '중복'),
    ([{'id': 1, 'turns': [{'turn': 1}, {'turn': 3}]}], '순서대로'),
    ([{'id': 1, 'turns': [{'turn': 2}, {'turn': 1}]}], '순서대로'),
    ([{'id': 1, 'turns': [{'turn': 1, 'id': 2}]}], '상위 id'),
    ([{'id': 1, 'turns': [{'turn': 1, 'case_id': 'Q2_T1'}]}], 'case_id'),
    ([{'id': 1, 'turns': [{'turn': 1, 'conversation_id': 'another'}]}], 'conversation_id'),
    ([{'id': 1, 'turns': [{'turn': 1, 'import_source': {}}]}], '예약 필드'),
    ([{'id': 1, 'turns': [{'turn': 1}]}, {'question': 'flat', 'answer': 'flat'}], '섞을 수 없습니다'),
])
def test_id_turns_rejects_ambiguous_or_invalid_structure(source, message):
    with pytest.raises(ValueError, match=message):
        uploaded(json.dumps(source), 'export.json')


@pytest.mark.parametrize('turn', [
    {'turn': 1, 'question': 'q'},
    {'turn': 1, 'question': 'q', 'answer': {'text': 'not raw text'}},
    {'turn': 1, 'question': '', 'answer': 'a'},
    {'turn': 1, 'question': 'q', 'Q': 'conflicting', 'answer': 'a'},
])
def test_id_turns_keeps_existing_question_answer_validation(turn):
    with pytest.raises(ValueError):
        uploaded(json.dumps([{'id': 1, 'turns': [turn]}]))


def test_flat_qa_with_turns_metadata_is_not_reinterpreted():
    source = {'id': 1, 'question': 'q', 'answer': 'a', 'turns': [1, 2]}
    result = uploaded(json.dumps([source]))
    assert not result['import']['converted']
    assert result['cases'][0]['turns'] == [1, 2]
    assert result['cases'][0]['case_id'] == '1'


def test_imported_original_saved_once_and_not_in_poll_status(tmp_path):
    text = '# 1회차\nQ: 첫 질문\nA: 원문 답변\nQ: 후속\nA: 후속 답변'
    result = uploaded(text)
    service = ComparisonService(CodeEvalService(save_dir=tmp_path / 'eval'), save_dir=tmp_path / 'runs')
    run = service.start({'cases': result['cases'], 'input_document': result['import']})
    assert 'input_document' not in run
    exported = json.loads(service.export(run['run_id']))
    assert exported['input_document']['original_text'] == text
    assert exported['datasets']['aiona'] == result['cases']
    assert normalize_dataset({'input_text': json.dumps({'cases': result['cases']})}) == result['cases']
