"""Evidence protocol preservation and unresolved judgments; no model or web calls."""
import copy
from types import SimpleNamespace

import pytest

from code_eval.combined import combined_summary
from code_eval.engine import evaluate_batch
from code_eval.jev_client import JevCall
from code_eval.jev_eval import (CONTEXT_QUOTE_RETRY_NOTE, evaluate_jev_batch, evaluate_jev_case, evaluation_log,
                                 parse_decisions)
from code_eval.semantic_evidence import CONTEXT_CHECK_POLICY, finalize_answers
from webapp.evaluation_events import EvaluationEvents, record_events
from webapp.version_registry import evaluation_profile


def entry(choice='PASS', **extra):
    return {'choice': choice, 'confidence': .8,
            'probabilities': {} if choice in ('UNVERIFIED', 'NOT_APPLICABLE') else {'PASS': .9, 'FAIL': .1},
            'reason': 'fixture evidence', **extra}


class EvidenceJudge:
    config = SimpleNamespace(engine='openai_api', to_manifest=lambda: {
        'engine': 'openai_api', 'requested_model': 'fixture-judge', 'reasoning_effort': 'high',
        'api_base': 'https://api.example.test/v1', 'max_output_tokens': 4000, 'max_tool_calls': 4,
        'web_search': 'enabled', 'fact_check_policy': 'evidence-v1',
        'fact_check_scope': 'all_answers',
        'context_check_policy': 'history-quotes-v1', 'search_policy': 'J5_without_reference'})

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls = []

    def system_one(self, state, questions):
        self.calls.append(copy.deepcopy({'state': state, 'questions': questions}))
        answers = {cid: copy.deepcopy(self.overrides.get(cid,
            entry('NOT_APPLICABLE', fact_check={'basis': 'none', 'claims': []}) if cid == 'J5' else entry())) for cid in questions}
        return JevCall('OK', body={'model': 'fixture-judge', 'answers': answers,
            'verification': {'checked_at': '2026-10-09T00:00:00Z', 'web_search_calls': 0}}, attempts=1)


def evaluate(cases, judge):
    batch = evaluate_batch(cases)
    source = copy.deepcopy(batch)
    batch['jev_eval'] = evaluate_jev_batch(batch, judge, workers=1)
    assert batch['cases'] == source['cases'] and batch['result_digest'] == source['result_digest']
    return batch


def test_unverified_is_independent_of_valid_decisions_and_preserves_evidence():
    fact = {'basis': 'none', 'claims': [{'claim': 'unverified fact', 'verdict': 'UNVERIFIED',
                                      'reason': 'source unavailable', 'sources': []}]}
    checks = [{'turn': 1, 'instruction_quote': 'keep this', 'answer_quote': '',
               'decision': 'UNVERIFIED', 'reason': 'no matching evidence'}]
    body = {'answers': {'J1': entry('FAIL'), 'J5': entry('UNVERIFIED', fact_check=fact),
                        'J6': entry('UNVERIFIED', context_checks=checks)}}
    original = copy.deepcopy(body)
    decisions = parse_decisions(body, ('J1', 'J5', 'J6'))
    assert decisions['J1']['decision'] == 'FAIL'
    for cid in ('J5', 'J6'):
        assert decisions[cid]['decision'] is None and decisions[cid]['status'] == 'UNVERIFIED'
        assert decisions[cid]['confidence'] == .8 and decisions[cid]['probabilities'] == {}
    assert decisions['J5']['fact_check'] == fact and decisions['J6']['context_checks'] == checks
    decisions['J5']['fact_check']['claims'].clear()
    assert body == original


def test_unverified_cannot_resolve_common_code_conditions():
    with pytest.raises(ValueError, match='PASS/FAIL'):
        parse_decisions({'answers': {'C0001': entry('UNVERIFIED')}}, ('C0001',), {'C0001': 'code condition'})
    # The old binary wire format remains parseable.
    assert parse_decisions({'answers': {'J1': entry()}}, ('J1',))['J1']['decision'] == 'PASS'


@pytest.mark.parametrize('cid', ['J1', 'J6', 'C0001'])
def test_not_applicable_is_exclusive_to_j5_screening(cid):
    with pytest.raises(ValueError, match='PASS/FAIL'):
        parse_decisions({'answers': {cid: entry('NOT_APPLICABLE')}}, (cid,), {cid: 'fixture'})


@pytest.mark.parametrize('engine', ['codex_cli', 'claude_cli', 'openai_api'])
def test_every_llm_answer_screens_non_numeric_facts(engine):
    judge = EvidenceJudge({'J5': entry('UNVERIFIED', fact_check={'basis': 'none', 'claims': []})})
    judge.config = SimpleNamespace(engine=engine, to_manifest=lambda: {'engine': engine})
    batch = evaluate([{'case_id': 'q', 'question': "What is Australia's capital?",
                       'answer': 'The capital of Australia is Sydney.'}], judge)
    assert 'J5' in judge.calls[0]['questions']
    record = batch['jev_eval']['records'][0]
    assert record['applicability']['evidence']['J5']['status'] == 'SCREENING'
    assert record['applicability']['version'] == 'jev-applicability-v2.3-llm-screening'
    assert record['decisions']['J5']['decision'] == 'FAIL'
    assert record['decisions']['J5']['policy_original_assessment']['status'] == 'UNVERIFIED'
    assert combined_summary(batch)['pending'] == 0 and combined_summary(batch)['fail'] == 1


def test_no_factual_claims_are_excluded_from_all_quality_denominators():
    judge = EvidenceJudge()
    batch = evaluate([{'case_id': 'q', 'question': 'Say hello', 'answer': 'Hello!'}], judge)
    record = batch['jev_eval']['records'][0]
    assert record['criteria'] == ['J1']
    assert record['decisions']['J5']['status'] == 'NOT_APPLICABLE'
    assert record['j5_basis'] is None
    assert record['decisions']['J5']['fact_check'] == {'basis': 'none', 'claims': []}
    assert 'J5' not in record['applicability']['evidence'] and 'J5' in record['applicability']['not_applicable']
    assert record['applicability']['screening']['J5']['status'] == 'NOT_APPLICABLE'
    assert batch['jev_eval']['summary']['J5'] == {'name': 'DATA 신뢰도', 'targets': 0, 'pass': 0, 'fail': 0, 'pending': 0, 'unverified': 0, 'incomplete': 0}
    assert batch['jev_eval']['completed'] == 1 and not record['review_required']
    summary = combined_summary(batch)
    assert (summary['pass'], summary['fail'], summary['pending']) == (1, 0, 0)
    events = list(record_events(batch['cases'][0], record, 0))
    assert not any(e.get('key') == 'J5' for e in events)


def test_native_jev_keeps_its_original_applicability_rules():
    judge = EvidenceJudge()
    judge.config = SimpleNamespace(engine='jev', to_manifest=lambda: {'engine': 'jev'})
    batch = evaluate([{'case_id': 'q', 'question': 'Say hello', 'answer': 'Hello!'}], judge)
    assert 'J5' not in judge.calls[0]['questions']
    assert batch['jev_eval']['applicability_version'] == 'jev-applicability-v2.5-length-rules'


@pytest.mark.parametrize('basis', ['web', 'reference', 'none'])
def test_j5_actual_basis_and_verification_are_saved_without_guessing(basis):
    fact = {'basis': basis, 'claims': [{'claim': 'fact', 'verdict': 'PASS' if basis != 'none' else 'UNVERIFIED',
        'reason': 'fixture proof', 'sources': [{'url': 'https://example.test/source', 'title': 'Source', 'quote': 'fact'}]
        if basis == 'web' else []}]}
    judge = EvidenceJudge({'J5': entry('UNVERIFIED' if basis == 'none' else 'PASS', fact_check=fact)})
    record = evaluate_jev_case('q', 'question', 'answer', judge, criteria=('J1', 'J5'), reference={'fact': True})
    assert record['status'] == 'SUCCESS'
    assert record['j5_basis'] == ('unverified' if basis == 'none' else basis)
    assert record['decisions']['J5']['fact_check'] == fact
    assert record['verification']['web_search_calls'] == 0
    assert record['review_required'] is False
    if basis == 'none':
        assert record['decisions']['J5']['reason_code'] == 'EVALUATION_EVIDENCE_INSUFFICIENT'


def test_transport_failure_keeps_attempted_verification_metadata():
    verification = {'checked_at': '2026-10-09T00:00:00Z', 'web_search_calls': 1, 'status': 'TIMEOUT'}
    judge = SimpleNamespace(system_one=lambda **kw: JevCall('TIMEOUT', error='fixture timeout',
                                                          body={'verification': verification}))
    record = evaluate_jev_case('q', 'q', 'a', judge, criteria=('J1', 'J5'))
    assert record['status'] == 'TIMEOUT' and record['verification'] == verification
    assert record['j5_basis'] == 'unverified' and record['decisions'] is None


def test_unverified_j5_finishes_as_evidence_policy_fail_with_original_audit():
    judge = EvidenceJudge({'J5': entry('UNVERIFIED', fact_check={'basis': 'none', 'claims': []})})
    batch = evaluate([{'case_id': 'q', 'question': 'Explain this', 'answer': 'A response', 'reference': {'fact': 'value'}}], judge)
    jev = batch['jev_eval']
    assert jev['engine_label'] == 'OpenAI API' and jev['status'] == 'SUCCESS'
    assert jev['completed'] == 1 and jev['review_required'] == 0 and jev['unverified'] == 0
    assert jev['summary']['J5'] == {'name': 'DATA 신뢰도', 'targets': 1, 'pass': 0, 'fail': 1, 'unverified': 0, 'incomplete': 0, 'pending': 0}
    summary = combined_summary(batch)
    assert (summary['pass'], summary['fail'], summary['pending']) == (0, 1, 0)
    assert summary['semantic_unverified'] == summary['review_required'] == 0
    assert summary['review_items'] == []
    logged = evaluation_log(batch)[0]['jev']
    assert logged['decisions']['J5']['decision'] == 'FAIL' and not logged['review_required']
    assert logged['decisions']['J5']['policy_original_assessment']['status'] == 'UNVERIFIED'
    assert logged['decisions']['J5']['policy_original_assessment']['fact_check']['basis'] == 'none'
    assert logged['verification']['checked_at'] == '2026-10-09T00:00:00Z'


def test_existing_fail_evidence_survives_another_unverified_criterion():
    # A FAIL that quotes the evaluated answer is confirmed evidence (no re-evaluation needed).
    judge = EvidenceJudge({'J1': entry('FAIL', reason="'A response' does not explain it"),
                           'J5': entry('UNVERIFIED', fact_check={'basis': 'none', 'claims': []})})
    batch = evaluate([{'case_id': 'q', 'question': 'Explain this', 'answer': 'A response', 'reference': {'fact': 'value'}}], judge)
    summary = combined_summary(batch)
    assert summary['fail'] == 1 and summary['pass'] == 0 and summary['semantic_pending'] == 0
    assert summary['problems'][0]['items'][0]['id'] == 'J1'
    assert batch['jev_eval']['summary']['J5']['fail'] == 1
    assert summary['review_required'] == 0


def test_j6_unknown_keeps_full_history_and_finishes_as_evidence_policy_fail():
    long_question = ' preserve whitespace\r\n' + 'x' * 60_000 + '\n'
    cases = [{'case_id': 't1', 'conversation_id': 'c', 'turn': 1, 'question': long_question, 'answer': ' first answer\n'},
             {'case_id': 't2', 'conversation_id': 'c', 'turn': 2, 'question': 'Continue', 'answer': 'second answer'}]
    checks = [{'turn': 1, 'instruction_quote': 'preserve whitespace', 'answer_quote': '',
               'decision': 'UNVERIFIED', 'reason': 'could not verify'}]
    judge = EvidenceJudge({'J6': entry('UNVERIFIED', context_checks=checks)})
    batch = evaluate(cases, judge)
    assert len(judge.calls) == 2 and 'context_reevaluation' not in batch['jev_eval']['records'][1]   # 인용 대조 실패가 아님
    sent = judge.calls[1]['state']['history']
    assert sent == [{'turn': 1, 'question': long_question, 'answer': ' first answer\n'}]
    assert batch['jev_eval']['records'][1]['context']['history_chars'] > 60_000
    assert batch['jev_eval']['records'][1]['context']['history_truncated'] is False
    assert batch['jev_eval']['records'][1]['decisions']['J6']['context_checks'] == checks
    conv = batch['jev_eval']['conversations']
    assert conv['incomplete'] == 0 and conv['fail'] == 1 and conv['rate'] == 0
    assert conv['bands']['1~10']['unverified'] == 0 and conv['bands']['1~10']['fail'] == 1
    assert batch['jev_eval']['records'][1]['decisions']['J6']['policy_original_assessment']['context_checks'] == checks


#: 중간 점검 557a50d8baf1 Gemini Q29 7턴 — 평가 모델이 답변의 '$\rightarrow$' 를 '→' 로 바꿔 인용해 J6 가 '평가 근거 부족' FAIL 이 됐다
Q29_HISTORY = [
    {'turn': 5, 'question': '현재 가상 폴더에 report_3.txt와 report_4.txt가 새로 추가됐어. 나머지는 그대로야. '
                            '기존의 접두사 변경 규칙과 덮어쓰기 금지, 충돌 항목 건너뛰기 규칙을 적용한 대응표를 만들어줘.',
     'answer': '| `report_1.txt` | `final_1.txt` | **[충돌] 건너뛰기 (Skip)** |'},
    {'turn': 6, 'question': '변경 후 접두사를 final_ 대신 archive_로 바꿀게. 이미 final_로 시작하는 파일은 건드리지 마. '
                            '변경 전후 대응표를 다시 작성하되 아직 실행하지는 마.',
     'answer': '| `report_4.txt` | `archive_4.txt` | **[정상] 변경 가능 (Rename)** |'}]
Q29_QUESTION = '현재 폴더에 archive_3.txt라는 기존 파일이 하나 더 있다는 정보가 추가됐어. 지금도 계획만 세우는 단계야.'
Q29_ANSWER = ('| **충돌 항목** | `report_3.txt` | `archive_3.txt` | **[충돌] 건너뛰기 (Skip)** |\n'
              '| **정상 항목** | `report_1.txt` | `archive_1.txt` | **[정상] 변경 가능 (Rename)** |\n'
              '* **충돌이 생기는 항목 (건너뛰기 대상):**\n'
              '  * `report_3.txt` $\\rightarrow$ 기존 파일 보호를 위해 이름 변경을 진행하지 않고 유지합니다.\n'
              '* **충돌 없이 바꿀 수 있는 항목 (변경 예정 대상):**\n'
              '  * `report_4.txt` $\\rightarrow$ `archive_4.txt`\n\n'
              '*(현재 단계는 계획만 수립된 상태이며, 실제 파일 이름은 변경되지 않았습니다.)*')


def q29_checks(answer_quote):
    return [{'turn': 6, 'instruction_quote': '변경 후 접두사를 final_ 대신 archive_로 바꿀게.', 'answer_quote': answer_quote,
             'decision': 'PASS', 'reason': 'archive_ 접두사를 적용했습니다.'},
            {'turn': 6, 'instruction_quote': '이미 final_로 시작하는 파일은 건드리지 마.',
             'answer_quote': '| **정상 항목** | `report_1.txt` | `archive_1.txt` |', 'decision': 'PASS', 'reason': 'final_ 파일 제외'},
            {'turn': 5, 'instruction_quote': '충돌 항목 건너뛰기 규칙을 적용한 대응표를 만들어줘.',
             'answer_quote': '기존 파일 보호를 위해 이름 변경을 진행하지 않고 유지합니다.', 'decision': 'PASS', 'reason': '충돌 건너뛰기'},
            {'turn': 6, 'instruction_quote': '변경 전후 대응표를 다시 작성하되 아직 실행하지는 마.',
             'answer_quote': '현재 단계는 계획만 수립된 상태이며, 실제 파일 이름은 변경되지 않았습니다.', 'decision': 'PASS',
             'reason': '실행하지 않음'}]


class QuoteCheckingJudge:
    """실제 transport 처럼 J6 응답을 서버 인용 대조(finalize_answers)에 통과시키는 대역. 호출마다 준비한 context_checks 를 쓴다."""
    config = SimpleNamespace(engine='anthropic_api', to_manifest=lambda: {'engine': 'anthropic_api'})

    def __init__(self, *checks):
        self.checks, self.calls = list(checks), []

    def system_one(self, state, questions):
        self.calls.append(copy.deepcopy(questions))
        body = {'model': 'fixture-judge', 'answers': {'J6': entry('PASS', context_checks=self.checks.pop(0))}}
        return JevCall('OK', body=finalize_answers(body, state, questions), attempts=1, usage={'input_tokens': 10})


def run_q29(judge):
    return evaluate_jev_case('Q29_T7', Q29_QUESTION, Q29_ANSWER, judge, criteria=('J6',), history=Q29_HISTORY,
                             fail_evidence_check=True)


def test_q29_j6_misquote_is_asked_once_more_and_the_verbatim_retry_is_used():
    judge = QuoteCheckingJudge(q29_checks('`report_4.txt` → `archive_4.txt`'),
                               q29_checks('`report_4.txt` $\\rightarrow$ `archive_4.txt`'))
    record = run_q29(judge)
    assert len(judge.calls) == 2 and list(judge.calls[1]) == ['J6']
    assert CONTEXT_QUOTE_RETRY_NOTE in judge.calls[1]['J6']['instructions']
    assert CONTEXT_QUOTE_RETRY_NOTE not in judge.calls[0]['J6']['instructions']
    j6 = record['decisions']['J6']
    assert j6['decision'] == 'PASS' and 'reason_code' not in j6 and not record['review_required']
    again = record['context_reevaluation']
    assert (again['policy'], again['status'], again['adopted']) == (CONTEXT_CHECK_POLICY, 'SUCCESS', 'retry')
    assert again['first']['status'] == 'UNVERIFIED' and again['first']['context_checks'][0]['decision'] == 'UNVERIFIED'
    assert again['retry']['decision'] == 'PASS'
    assert record['attempts'] == 2 and record['usage']['input_tokens'] == 20
    logged = evaluation_log({'cases': [{'case_id': 'Q29_T7'}], 'jev_eval': {'records': [record]}})[0]['jev']
    assert logged['context_reevaluation']['adopted'] == 'retry'


def test_q29_j6_still_misquoted_after_one_retry_keeps_the_evidence_policy_fail():
    misquoted = q29_checks('`report_4.txt` → `archive_4.txt`')
    judge = QuoteCheckingJudge(misquoted, misquoted)
    record = run_q29(judge)
    assert len(judge.calls) == 2
    j6 = record['decisions']['J6']
    assert j6['decision'] == 'FAIL' and j6['reason_code'] == 'EVALUATION_EVIDENCE_INSUFFICIENT'
    assert j6['policy_original_assessment']['status'] == 'UNVERIFIED'
    again = record['context_reevaluation']
    assert again['adopted'] == 'first' and again['retry']['status'] == 'UNVERIFIED'


def test_missing_turn_is_not_inferred_or_asked_but_remains_an_unverified_target():
    cases = [{'case_id': 't1', 'conversation_id': 'c', 'turn': 1, 'question': 'Keep the original', 'answer': 'Original'},
             {'case_id': 't3', 'conversation_id': 'c', 'turn': 3, 'question': 'Continue', 'answer': 'Continuation'}]
    judge = EvidenceJudge()
    batch = evaluate(cases, judge)
    record = batch['jev_eval']['records'][1]
    assert 'J6' not in judge.calls[1]['questions']
    assert record['not_requested'] == ['J6'] and record['decisions']['J6']['status'] == 'UNVERIFIED'
    assert record['context']['issues'] == ['누락 2턴'] and not record['context']['history_complete']
    assert record['applicability']['evidence']['J6']['matches'] == ['누락 2턴']
    assert 'J6' not in record['applicability']['not_applicable']
    assert combined_summary(batch)['pending'] == 1


def test_events_count_final_policy_failure_and_keep_confirmed_fail_evidence():
    record = evaluate_jev_case('q', 'q', 'a', EvidenceJudge({'J1': entry('FAIL'), 'J5': entry('UNVERIFIED')}), criteria=('J1', 'J5'))
    stream = EvaluationEvents()
    stream.publish({'type': 'run_start', 'total': 1})
    for event in record_events({'case_id': 'q'}, record, 0):
        stream.publish({**event, 'model': 'aiona'})
    snapshot = stream.snapshot()
    row = snapshot['rows']['aiona:0']
    assert snapshot['fail'] == 2 and snapshot['unjudged'] == 0
    assert row['review_required'] is False
    assert row['criteria']['J5']['result'] == 'FAIL'
    assert row['criteria']['J5']['reason'] == '평가 근거 부족으로 검증 기준 미충족'
    assert 'verification_status' not in row['criteria']['J5']


def test_profile_tracks_evidence_policy_api_and_output_limits_but_not_runtime_secrets():
    batch = evaluate([{'case_id': 'q', 'question': 'Hello', 'answer': 'Hello'}], EvidenceJudge())
    run = {'evaluations': {'aiona': batch}}
    profile = evaluation_profile(run)
    assert profile['complete']
    judge = profile['fields']['judge']
    assert judge['fact_check_policy'] == 'evidence-v1' and judge['context_check_policy'] == 'history-quotes-v1'
    assert judge['fact_check_scope'] == 'all_answers'
    client = batch['jev_eval']['client']
    client.update(api_base='https://user:secret@api.example.test/v1?api_key=secret#key', checked_at='different', credentials='secret')
    assert evaluation_profile(run) == profile
    for field, value in [('max_output_tokens', 5000), ('max_tool_calls', 9), ('search_policy', 'disabled'),
                         ('fact_check_policy', 'evidence-v2'), ('fact_check_scope', 'numeric_only'),
                         ('reasoning_effort', 'low'), ('api_base', 'https://other.example.test/v1'),
                         ('search_host_features', ['code_mode', 'code_mode_host']),
                         ('search_host_policy', 'J5_without_reference_and_live')]:
        changed = copy.deepcopy(run)
        changed['evaluations']['aiona']['jev_eval']['client'][field] = value
        assert evaluation_profile(changed) != profile
