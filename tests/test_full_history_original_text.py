"""Verify the actual semantic transports receive complete, unmodified histories."""
import json
from types import SimpleNamespace

import httpx
import pytest

from code_eval.claude_cli_client import ClaudeCliClient, ClaudeCliConfig
from code_eval.engine import evaluate_batch
from code_eval.jev_client import HttpJevClient, JevClientConfig
from code_eval.jev_eval import evaluate_jev_batch
from webapp.comparison_service import DEFAULT_COMPARISON_DATASET


def pass_response(questions):
    return {'model': 'test-judge', 'answers': {
        cid: {'choice': 'PASS', 'confidence': 0.8, 'probabilities': {'PASS': 0.9, 'FAIL': 0.1}}
        for cid in questions}}


def long_cases():
    return [
        {'id': 'T1', 'conversation_id': 'C1', 'turn': 1,
         'question': '  첫 질문\r\n' + '원문질문' * 3000 + ' 끝  ',
         'answer': '  ```json\r\n{"value":"\\n"}\r\n```\n' + '가😀' * 35_000 + '\r\n마지막 원문  '},
        {'id': 'T2', 'conversation_id': 'C1', 'turn': 2, 'question': '  이어서 설명해줘.\r\n', 'answer': '답변 원문\r\n  '},
    ]


def test_q165_http_payload_preserves_every_original_history_and_conversation_boundary():
    if not DEFAULT_COMPARISON_DATASET.is_file():
        pytest.skip('Optional private question dataset is not included in public checkouts')
    cases = json.loads(DEFAULT_COMPARISON_DATASET.read_text(encoding='utf-8'))
    requests = []

    def handle(req):
        body = json.loads(req.content)
        requests.append(body)
        return httpx.Response(200, json=pass_response(body['questions']))

    client = HttpJevClient(JevClientConfig(max_attempts=1), 'test-key', transport=httpx.MockTransport(handle))
    try:
        result = evaluate_jev_batch(evaluate_batch(cases), client, workers=1)
    finally:
        client._client().close()
    assert len(requests) == 165 and result['completed'] == 165
    assert result['history_policy'] == 'full_original_text'
    for row, request, record in zip(cases, requests, result['records']):
        assert request['state']['question'] == row['question']
        assert request['state']['answer'] == row['answer']
        prior = [r for r in cases if 'conversation_id' in row and
                 r.get('conversation_id') == row['conversation_id'] and r.get('turn', 0) < row['turn']]
        expected = [{'turn': p['turn'], 'question': p['question'], 'answer': p['answer']} for p in prior]
        assert request['state'].get('history', []) == expected
        if record['context']:
            assert record['context']['history_truncated'] is False
            assert record['context']['history_chars'] == sum(len(p['question']) + len(p['answer']) for p in prior)


def test_claude_cli_stdin_carries_long_history_without_changing_whitespace_or_json_text(tmp_path):
    cases = long_cases()
    payloads = []

    def runner(cmd, **kwargs):
        payloads.append(json.loads(kwargs['input'].split('## 평가 대상 (데이터)\n', 1)[1]))
        ids = json.loads(cmd[cmd.index('--json-schema') + 1])['required']
        output = {'structured_output': {cid: {'choice': 'PASS', 'confidence': 0.8, 'reason': '테스트 근거'} for cid in ids}}
        if 'J5' in ids:
            output['structured_output']['J5'].update(choice='NOT_APPLICABLE',
                fact_check={'basis': 'none', 'claims': []}, reason='문자열 원문 보존 대역에는 검증할 사실 주장이 없음')
        if 'J6' in ids:
            state = payloads[-1]
            output['structured_output']['J6']['context_checks'] = [{
                'turn': state['history'][0]['turn'],
                'instruction_quote': state['history'][0]['question'].strip()[:32],
                'answer_quote': state['answer'].strip(), 'decision': 'PASS', 'reason': '원문 인용 연결 대역 검증'}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(output), stderr='')

    client = ClaudeCliClient(ClaudeCliConfig(executable='test-claude', max_attempts=1), runner=runner, workdir=tmp_path)
    result = evaluate_jev_batch(evaluate_batch(cases), client, workers=1)
    assert result['completed'] == 2
    assert payloads[1]['history'] == [{'turn': 1, 'question': cases[0]['question'], 'answer': cases[0]['answer']}]
    assert payloads[1]['question'] == cases[1]['question'] and payloads[1]['answer'] == cases[1]['answer']
    assert result['records'][1]['context']['history_chars'] > 60_000
    assert result['records'][1]['context']['history_truncated'] is False
    assert 'J6' in result['records'][1]['criteria']


def test_provider_rejecting_full_history_is_an_api_error_without_retrying_a_shortened_payload():
    cases = long_cases()
    requests = []

    def handle(req):
        body = json.loads(req.content)
        requests.append(body)
        if body['state'].get('history'):
            return httpx.Response(413, json={'error': 'test context limit'})
        return httpx.Response(200, json=pass_response(body['questions']))

    client = HttpJevClient(JevClientConfig(max_attempts=1), 'test-key', transport=httpx.MockTransport(handle))
    try:
        result = evaluate_jev_batch(evaluate_batch(cases), client, workers=1)
    finally:
        client._client().close()
    assert len(requests) == 2
    assert requests[1]['state']['history'][0]['answer'] == cases[0]['answer']
    record = result['records'][1]
    assert record['status'] == 'API_ERROR' and record['decisions'] is None
    assert 'J6' in record['criteria'] and record['context']['history_complete'] is True
    assert record['context']['history_truncated'] is False
    assert result['conversations']['items'][0]['result'] == 'INCOMPLETE'
