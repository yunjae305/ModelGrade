"""Dollar accounting uses real usage shapes, never live provider requests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from code_eval.cost_reporting import CostLedger, estimate
from code_eval.jev_client import JevCall


def test_openai_cache_writes_and_reasoning_are_not_double_counted():
    usage = {'input_tokens': 1_000, 'input_tokens_details': {'cached_tokens': 200, 'cache_write_tokens': 300},
             'output_tokens': 500, 'output_tokens_details': {'reasoning_tokens': 400}}
    # 500 ordinary, 200 cached, 300 written, 500 output plus two searches.
    assert estimate('openai_api', 'gpt-6-luna', usage, web_search_calls=2) == (Decimal('.0203395'), 1000, 500)


def test_openai_request_parts_ignore_duplicate_aggregate_and_keep_unknown_calls(capsys):
    config = SimpleNamespace(model='gpt-6.1-sol', engine='openai_api')
    usage = {'input_tokens': 1000, 'output_tokens': 100,
             'input_tokens_details': {'cached_tokens': 200, 'cache_write_tokens': 100},
             'output_tokens_details': {'reasoning_tokens': 75}}
    call = JevCall('SKIPPED', attempts=3, usage=usage, accounting={
        'usage': usage, 'reported_cost_usd': 99,
        'cost_parts': [
            {'model': config.model, 'usage': usage, 'web_search_calls': 2, 'service_tier': 'default'},
            {'model': config.model, 'usage': usage, 'web_search_calls': None, 'service_tier': 'default'},
            {'model': config.model, 'usage': {}, 'web_search_calls': None, 'service_tier': None}],
        'unreported_attempts': 0})
    ledger = CostLedger(); ledger._semantic(config, call); ledger.finish('parts', 'CANCELLED')
    output = capsys.readouterr().out
    assert '추정액 $0.022670 | 호출 3건 | 미확인 2건' in output
    assert '검색 사용량 미확인' in output and '서비스 등급 미기록' in output
    assert '$99' not in output


def test_openai_long_context_threshold_applies_per_request():
    usage = {'input_tokens': 272_000, 'output_tokens': 1000,
             'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}
    assert estimate('gpt', 'gpt-6.1-sol', usage)[0] == Decimal('.554')
    usage['input_tokens'] += 1
    assert estimate('gpt', 'gpt-6.1-sol', usage)[0] == Decimal('1.103004')


def test_claude_cached_input_is_separate_and_write_ttls_differ():
    usage = {'input_tokens': 1000, 'output_tokens': 500,
             'cache_read_input_tokens': 200, 'cache_creation_input_tokens': 300,
             'cache_creation': {'ephemeral_5m_input_tokens': 200, 'ephemeral_1h_input_tokens': 100},
             'output_tokens_details': {'thinking_tokens': 400}}
    assert estimate('claude', 'claude-sonnet-5-5', usage) == (Decimal('.00792'), 1500, 500)


def anthropic_usage(**changes):
    return {'input_tokens': 1000, 'output_tokens': 500, 'cache_read_input_tokens': 0,
            'cache_creation_input_tokens': 0, 'service_tier': 'standard', 'inference_geo': 'global', **changes}


def test_collection_claude_honors_explicit_usage_geography():
    assert estimate('claude', 'claude-sonnet-5-5', anthropic_usage(inference_geo='us'))[0] == Decimal('.0077')
    assert estimate('claude', 'claude-sonnet-5-5', anthropic_usage())[0] == Decimal('.007')


@pytest.mark.parametrize('changes', [{'service_tier': 'priority'}, {'service_tier': 'batch'},
                                      {'inference_geo': 'unknown'}, {'inference_geo': None}])
def test_collection_claude_does_not_guess_explicit_unknown_rate_metadata(changes):
    with pytest.raises(ValueError, match='단가 미확인'):
        estimate('claude', 'claude-sonnet-5-5', anthropic_usage(**changes))


def test_anthropic_semantic_caches_thinking_and_search_are_charged_once():
    usage = anthropic_usage(cache_read_input_tokens=200, cache_creation_input_tokens=300,
        cache_creation={'ephemeral_5m_input_tokens': 200, 'ephemeral_1h_input_tokens': 100},
        output_tokens_details={'thinking_tokens': 400},
        server_tool_use={'web_search_requests': 2, 'web_fetch_requests': 8})
    assert estimate('anthropic_api', 'claude-sonnet-5-5', usage, web_search_calls=2) == (Decimal('.02792'), 1500, 500)
    # Search failure/no search can still incur model tokens; fetch has no flat fee.
    assert estimate('anthropic_api', 'claude-sonnet-5-5', usage, web_search_calls=0)[0] == Decimal('.00792')


def test_anthropic_us_premium_applies_to_tokens_but_not_search_fees():
    usage = anthropic_usage(cache_read_input_tokens=200, cache_creation_input_tokens=300,
        cache_creation={'ephemeral_5m_input_tokens': 200, 'ephemeral_1h_input_tokens': 100}, inference_geo='us')
    assert estimate('anthropic_api', 'claude-sonnet-5-5', usage, web_search_calls=2)[0] == Decimal('.028712')


def test_anthropic_long_context_still_uses_standard_sonnet_rate():
    assert estimate('anthropic_api', 'claude-sonnet-5-5', anthropic_usage(input_tokens=900_000, output_tokens=1000),
                    web_search_calls=0)[0] == Decimal('1.81')


@pytest.mark.parametrize('changes', [
    {'cache_read_input_tokens': None}, {'cache_creation_input_tokens': None},
    {'cache_creation_input_tokens': 10},
    {'cache_creation_input_tokens': 10, 'cache_creation': {'ephemeral_5m_input_tokens': 10}},
    {'cache_creation_input_tokens': 10, 'cache_creation': {'ephemeral_5m_input_tokens': 8, 'ephemeral_1h_input_tokens': 3}},
    {'cache_creation_input_tokens': 10, 'cache_creation': False},
    {'cache_read_input_tokens': True}, {'inference_geo': None}, {'inference_geo': 'unknown'},
    {'service_tier': 'priority'}, {'service_tier': 'batch'},
    {'input_tokens': -1}, {'output_tokens': None},
])
def test_anthropic_unknown_or_inconsistent_billing_fields_do_not_get_a_guessed_total(changes):
    with pytest.raises(ValueError):
        estimate('anthropic_api', 'claude-sonnet-5-5', anthropic_usage(**changes), web_search_calls=0)


@pytest.mark.parametrize('field', ['cache_read_input_tokens', 'cache_creation_input_tokens', 'inference_geo'])
def test_anthropic_missing_billing_fields_stay_unknown(field):
    usage = anthropic_usage()
    usage.pop(field)
    with pytest.raises(ValueError):
        estimate('anthropic_api', 'claude-sonnet-5-5', usage, web_search_calls=0)


@pytest.mark.parametrize('searches', [None, -1, True, 1.5, '1'])
def test_anthropic_unknown_search_usage_never_becomes_zero(searches):
    with pytest.raises(ValueError):
        estimate('anthropic_api', 'claude-sonnet-5-5', anthropic_usage(), web_search_calls=searches)


def test_anthropic_actual_unknown_model_is_not_priced_as_configured_sonnet():
    with pytest.raises(ValueError, match='단가'):
        estimate('anthropic_api', 'claude-sonnet-5-5-unknown', anthropic_usage(), web_search_calls=0)


def test_anthropic_pause_turn_parts_are_priced_separately_without_aggregate_double_count(capsys):
    ledger = CostLedger()
    config = SimpleNamespace(model='claude-sonnet-5-5', engine='anthropic_api')
    parts = [
        {'model': config.model, 'usage': anthropic_usage(), 'web_search_calls': 2},
        {'model': config.model, 'usage': anthropic_usage(input_tokens=100, output_tokens=50, inference_geo='us'),
         'web_search_calls': 1},
    ]
    call = JevCall('OK', attempts=2, accounting={
        'cost_parts': parts, 'unreported_attempts': 0,
        'usage': anthropic_usage(input_tokens=1100, output_tokens=550), 'reported_cost_usd': 99,
        'model_usage': {config.model: {'reported_cost_usd': 99}}})
    ledger._semantic(config, call)
    ledger.finish('continued', 'COMPLETED')
    output = capsys.readouterr().out
    assert '평가 | anthropic_api | claude-sonnet-5-5' in output
    assert '호출 2건' in output and '추정 합계: $0.037770' in output
    assert '$99' not in output and '미확인' not in output


@pytest.mark.parametrize('unknown_part', [
    {'usage': {}, 'web_search_calls': None},
    {'usage': anthropic_usage(), 'web_search_calls': None},
    {'usage': anthropic_usage(service_tier='priority'), 'web_search_calls': 0},
    {'usage': anthropic_usage(inference_geo=None), 'web_search_calls': 0},
])
def test_anthropic_incomplete_continuation_only_includes_known_request_cost(capsys, unknown_part):
    ledger = CostLedger()
    config = SimpleNamespace(model='claude-sonnet-5-5', engine='anthropic_api')
    call = JevCall('TIMEOUT', attempts=2, accounting={'cost_parts': [
        {'model': config.model, 'usage': anthropic_usage(), 'web_search_calls': 2}, unknown_part],
        'unreported_attempts': 0})
    ledger._semantic(config, call)
    ledger.finish('continued', 'CANCELLED')
    output = capsys.readouterr().out
    assert '호출 2건' in output and '확인 가능한 부분 합계: $0.027000' in output
    assert '미확인 1건 제외' in output


def test_anthropic_missing_request_part_is_unknown_and_not_silently_omitted(capsys):
    ledger = CostLedger()
    config = SimpleNamespace(model='claude-sonnet-5-5', engine='anthropic_api')
    ledger._semantic(config, JevCall('TIMEOUT', attempts=2, accounting={'cost_parts': [
        {'usage': anthropic_usage(), 'web_search_calls': 0}], 'unreported_attempts': 1}))
    ledger.finish('missing', 'ERROR')
    output = capsys.readouterr().out
    assert '호출 2건' in output and '미확인 1건 제외' in output
    assert '확인 가능한 부분 합계: $0.007000' in output


def test_anthropic_pre_call_cancellation_is_not_reported_as_a_paid_request(capsys):
    ledger = CostLedger()
    ledger._semantic(SimpleNamespace(model='claude-sonnet-5-5', engine='anthropic_api'),
                     JevCall('SKIPPED', attempts=0, accounting={'cost_parts': [], 'unreported_attempts': 0}))
    ledger.finish('not-started', 'CANCELLED')
    assert '이번 작업의 LLM 호출 없음' in capsys.readouterr().out


def test_collection_claude_and_semantic_anthropic_have_distinct_cost_rows(capsys):
    ledger = CostLedger()
    ledger.record_collection('claude', 'claude-sonnet-5-5',
                             {'usage': {'input_tokens': 1000, 'output_tokens': 500}})
    ledger._semantic(SimpleNamespace(model='claude-sonnet-5-5', engine='anthropic_api'),
                     JevCall('OK', attempts=1, accounting={'cost_parts': [
                         {'usage': anthropic_usage(), 'web_search_calls': 2}], 'unreported_attempts': 0}))
    ledger.finish('two-phases', 'COMPLETED')
    output = capsys.readouterr().out
    assert '수집 | claude | claude-sonnet-5-5 | 추정액 $0.007000' in output
    assert '평가 | anthropic_api | claude-sonnet-5-5 | 추정액 $0.027000' in output
    assert '추정 합계: $0.034000' in output


def test_gemini_thinking_is_separate_and_intro_rate_expires():
    usage = {'promptTokenCount': 1000, 'cachedContentTokenCount': 200,
             'candidatesTokenCount': 100, 'thoughtsTokenCount': 400, 'totalTokenCount': 1500}
    assert estimate('gemini', 'gemini-3.8-flash', usage, today=date(2026, 12, 31)) == (Decimal('.00249'), 1000, 500)
    assert estimate('gemini', 'gemini-3.8-flash', usage, today=date(2027, 1, 1))[0] == Decimal('.00498')


def test_sonnet_55_long_context_has_no_surcharge():
    assert estimate('claude', 'claude-sonnet-5-5', {'input_tokens': 900_000, 'output_tokens': 1000})[0] == Decimal('1.81')


@pytest.mark.parametrize('usage', [{}, {'input_tokens': -1, 'output_tokens': 10},
    {'input_tokens': True, 'output_tokens': 10}, {'input_tokens': 10, 'output_tokens': None},
    {'input_tokens': 10, 'output_tokens': 5},
    {'input_tokens': 10, 'output_tokens': 5, 'input_tokens_details': {'cached_tokens': 11}}])
def test_missing_or_invalid_usage_never_becomes_zero_cost(usage):
    with pytest.raises(ValueError):
        estimate('gpt', 'gpt-6-luna', usage)


def test_unrecognized_model_and_tier_are_not_guessed():
    usage = {'input_tokens': 100, 'output_tokens': 10}
    with pytest.raises(ValueError, match='단가'):
        estimate('gpt', 'gpt-6-luna-unrecognized', usage)
    with pytest.raises(ValueError, match='등급'):
        estimate('gpt', 'gpt-6-luna', usage, service_tier='priority')


def test_ledger_is_thread_safe_one_shot_and_contains_no_source_text(capsys):
    ledger = CostLedger()
    raw = {'model': 'gpt-6-luna', 'usage': {'input_tokens': 1000, 'output_tokens': 500,
           'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}},
           'output': [{'text': 'private-question-answer-secret'}]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: ledger.record_collection('gpt', 'configured-alias', raw), range(100)))
    assert capsys.readouterr().out == ''
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: ledger.finish('abc123', 'COMPLETED'), range(4)))
    out = capsys.readouterr().out
    assert out.count('[LLM 비용]') == 1
    assert 'gpt-6-luna' in out and '호출 100건' in out and '$0.035000' in out
    assert 'private-question-answer-secret' not in out
    ledger.record_collection('gpt', 'gpt-6-luna', raw)
    ledger.finish('abc123', 'COMPLETED')
    assert capsys.readouterr().out == ''


def test_missing_usage_produces_partial_total(capsys):
    ledger = CostLedger()
    ledger.record_collection('gpt', 'gpt-6-luna', {'usage': {'input_tokens': 1000, 'output_tokens': 500,
                             'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}})
    ledger.record_collection('gpt', 'gpt-6-luna', None)
    ledger.finish('run', 'CANCELLED')
    out = capsys.readouterr().out
    assert '확인 가능한 부분 합계: $0.000350' in out and '미확인 1건' in out


def test_semantic_cli_does_not_treat_subscription_usage_as_free(capsys):
    ledger = CostLedger()
    config = SimpleNamespace(model='gpt-6.1-sol', engine='codex_cli')
    call = JevCall('OK', usage={'input_tokens': 1000, 'output_tokens': 100})
    original = SimpleNamespace(system_one=lambda **_: call, config=config)
    client, actual, reason = ledger.wrap_resolver(lambda: (original, config, None))()
    assert actual is config and reason is None and client.system_one(state={}, questions={}) is call
    ledger.finish('run', 'COMPLETED')
    out = capsys.readouterr().out
    assert 'CLI 달러 청구액 미제공' in out and '추정액 확인 불가' in out and '미확인 1건 제외' in out


def test_per_model_cli_costs_are_not_counted_again_in_session_total(capsys):
    ledger = CostLedger()
    config = SimpleNamespace(model='requested', engine='claude_cli')
    call = SimpleNamespace(accounting={'model_usage': {
        'model-one': {'reported_cost_usd': .02}, 'model-two': {'reported_cost_usd': .03}},
        'reported_cost_usd': .05}, usage={})
    ledger._semantic(config, call)
    ledger.finish('run', 'ERROR')
    out = capsys.readouterr().out
    assert 'model-one' in out and 'model-two' in out and '추정 합계: $0.050000' in out


def test_unreported_attempt_is_not_counted_twice(capsys):
    ledger = CostLedger()
    config = SimpleNamespace(model='gpt-6.1-sol', engine='codex_cli')
    ledger._semantic(config, SimpleNamespace(usage={}, accounting={'usage': {}, 'unreported_attempts': 1}))
    ledger.finish('run', 'ERROR')
    assert '호출 1건' in capsys.readouterr().out


def test_stdout_failure_never_changes_completed_work(monkeypatch):
    ledger = CostLedger()
    def broken(*args, **kwargs):
        raise BrokenPipeError()
    monkeypatch.setattr('builtins.print', broken)
    ledger.finish('run', 'COMPLETED')


def test_small_positive_amount_is_not_displayed_as_free(capsys):
    ledger = CostLedger()
    ledger.record_collection('gpt', 'gpt-6-luna', {'usage': {'input_tokens': 1, 'output_tokens': 0,
                             'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}})
    ledger.finish('run', 'COMPLETED')
    assert '<$0.000001' in capsys.readouterr().out
