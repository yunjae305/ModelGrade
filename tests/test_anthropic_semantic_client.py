"""Claude judge transport contract, provenance and cancellation without paid requests."""
import asyncio
import copy
import json
import threading
import time

import httpx
import pytest

from code_eval.anthropic_semantic_client import (AnthropicSemanticClient, AnthropicSemanticConfig, FINAL_TOOL,
                                               _strict_schema, _text_submission, prompt_cache_setting)
from code_eval.jev_eval import jev_request, evaluate_jev_case


def entries(*ids):
    return {cid: {'choice':'PASS','confidence':.8,'reason':'자료와 일치합니다.'} for cid in ids}


def response(values=None, blocks=(), **extra):
    return {'model':'claude-sonnet-5-5','stop_reason':'tool_use', 'content':[
        *blocks, {'type':'tool_use','id':'final','name':FINAL_TOOL,'input':values or entries('J1')}],
        'usage':{'input_tokens':100,'output_tokens':50,'cache_creation_input_tokens':0,
                 'cache_read_input_tokens':0,'server_tool_use':{'web_search_requests':0},
                 'service_tier':'standard','inference_geo':'global'}, **extra}


def call(raw, *, ids=('J1',), state=None, config=None, status=200, fetcher=None):
    sent = []
    def handler(req):
        sent.append(req)
        return httpx.Response(status, json=raw)
    client = AnthropicSemanticClient(config or AnthropicSemanticConfig(), 'secret-test-key',
        transport=httpx.MockTransport(handler), source_fetcher=fetcher or (lambda url: {'status':'fetch_error'}))
    result = client.system_one(state or {'question':'질문','answer':'답변'}, jev_request('질문','답변',ids)['questions'])
    return result, sent


def test_request_preserves_blind_inputs_uses_medium_and_strict_final_tool():
    state = {'question':'  질문\r\n😀','answer':' 답변\r\n'+'가😀'*40000,
             'reference':' 기준 원문\r\n','history':[{'turn':1,'question':' 앞 질문 ','answer':' 앞 답변\r\n'}]}
    original = copy.deepcopy(state)
    result, sent = call(response(entries('J1','J5','J6')),ids=('J1','J5','J6'),state=state)
    body=json.loads(sent[0].content)
    assert result.status=='OK' and result.attempts==1 and state==original
    assert result.body['verification']['output_transport'] == 'strict_tool'
    assert str(sent[0].url)=='https://api.anthropic.com/v1/messages'
    assert sent[0].headers['x-api-key']=='secret-test-key' and sent[0].headers['anthropic-version']=='2023-06-01'
    assert body['model']=='claude-sonnet-5-5' and body['output_config']=={'effort':'medium'}
    assert body['thinking']=={'type':'adaptive'} and body['service_tier']=='standard_only'
    assert '지정된 JSON 형식으로만 답합니다.' not in body['system']
    assert '외부 도구를 사용하지 않습니다.' not in body['system']
    assert 'submit_evaluation 도구를 정확히 한 번' in body['system']
    prompt=''.join(block['text'] for block in body['messages'][0]['content'])
    assert json.loads(prompt.split('## 평가 대상 (데이터)\n',1)[1])==state
    assert len(body['tools'])==1 and body['tools'][0]['strict'] is True
    schema=body['tools'][0]['input_schema']
    assert schema['required']==['J1','J5','J6']
    assert 'minimum' not in schema['properties']['J1']['properties']['confidence']
    assert 'minimum: 0' in schema['properties']['J1']['properties']['confidence']['description']
    assert not {'metadata','context_management','temperature','output_format','betas'} & body.keys()
    assert 'secret-test-key' not in json.dumps(result.body)


def test_configured_opus_5_5_request_avoids_parameters_the_model_rejects():
    # Opus 5.5 returns 400 for disabled thinking, thinking budgets, sampling
    # parameters, forced tool choice and assistant prefill; effort must be explicit.
    raw = response(entries('J1'), model='claude-opus-5-5')
    result, sent = call(raw, config=AnthropicSemanticConfig(model='claude-opus-5-5', reasoning_effort='high'))
    body = json.loads(sent[0].content)
    assert result.status == 'OK' and result.body['model'] == 'claude-opus-5-5'
    assert result.body['answers']['J1']['choice'] == 'PASS'
    assert body['model'] == 'claude-opus-5-5' and body['output_config'] == {'effort': 'high'}
    assert body['thinking'] == {'type': 'adaptive'} and body['tool_choice'] == {'type': 'auto'}
    assert not {'temperature', 'top_p', 'top_k'} & body.keys()
    assert [message['role'] for message in body['messages']] == ['user']


def test_prompt_cache_marks_only_the_reusable_instructions_and_keeps_the_text():
    # F8: tools + system + static rules are cached; the timestamp and evaluation data follow the marker.
    bodies=[]
    for answer in ('첫 답변','둘째 답변'):
        _,sent=call(response(entries('J1','J5')),ids=('J1','J5'),state={'question':'질문','answer':answer})
        bodies.append(json.loads(sent[0].content))
    blocks=[b['messages'][0]['content'] for b in bodies]
    assert [[('cache_control' in block) for block in content] for content in blocks]==[[True,False]]*2
    assert blocks[0][0]==blocks[1][0] and blocks[0][0]['cache_control']=={'type':'ephemeral'}
    assert '실행 기준 시각' not in blocks[0][0]['text'] and '첫 답변' in blocks[0][1]['text']
    assert sum(json.dumps(b,ensure_ascii=False).count('cache_control') for b in bodies)==2
    _,sent=call(response(entries('J1')),config=AnthropicSemanticConfig(prompt_cache=False))
    body=json.loads(sent[0].content)
    assert isinstance(body['messages'][0]['content'],str) and 'cache_control' not in json.dumps(body)
    assert AnthropicSemanticConfig(prompt_cache=False).to_manifest()['prompt_cache_policy']=='disabled'
    assert AnthropicSemanticConfig().to_manifest()['prompt_cache_policy']=='static_prefix_ephemeral_5m_items_search_only'
    # legacy 는 검색이 없어도 기준 조합별로 질문끼리 앞부분을 나눠 쓰므로 캐시 표시를 유지한다
    _,sent=call(response(entries('J1')),config=AnthropicSemanticConfig(web_search='disabled'))
    assert [('cache_control' in block) for block in json.loads(sent[0].content)['messages'][0]['content']]==[True,False]
    assert prompt_cache_setting(None) is True and prompt_cache_setting(' OFF ') is False
    with pytest.raises(ValueError):
        prompt_cache_setting('sometimes')


def web_blocks(url='https://evidence.example/page'):
    return [{'type':'server_tool_use','id':'search1','name':'web_search','input':{'query':'fixture'}},
            {'type':'web_search_tool_result','tool_use_id':'search1','content':[
                {'type':'web_search_result','url':url,'title':'Official','encrypted_content':'opaque'}]},
            {'type':'server_tool_use','id':'fetch1','name':'web_fetch','input':{'url':url}},
            {'type':'web_fetch_tool_result','tool_use_id':'fetch1','content':{
                'type':'web_fetch_result','url':url,'content':{'type':'document','source':{'type':'text','data':'Alpha is blue.'}}}}]


def fact():
    return {'J5':{'choice':'PASS','confidence':.9,'reason':'공식 자료와 일치합니다.',
        'fact_check':{'basis':'web','claims':[{'claim':'Alpha is blue.','verdict':'PASS','reason':'공식 자료 확인',
            'sources':[{'url':'https://evidence.example/page','title':'Official','quote':'Alpha is blue.',
                       'authority':'primary','authority_reason':'제품 공식 규격 문서',
                       'temporal_relevance':'변경되지 않은 제품 정의'}]}]}}}


def test_web_tools_and_provider_results_then_independent_quote_check():
    fetched=[]
    def fetcher(url):
        fetched.append(url)
        return {'status':'ok','text':'Alpha is blue.','final_url':url,'content_type':'text/plain','checked_at':'2026-10-11T00:00:00Z'}
    result,sent=call(response(fact(),web_blocks()),ids=('J5',),state={'question':'색상?','answer':'Alpha is blue.'},fetcher=fetcher)
    body=json.loads(sent[0].content)
    assert [t['name'] for t in body['tools']]==['web_search','web_fetch',FINAL_TOOL]
    assert body['tools'][0]['max_uses']==4 and body['tools'][1]['max_uses']==8
    assert 'format' not in body['output_config']  # Search citations conflict with JSON output format.
    assert fetched==['https://evidence.example/page'] and result.body['verification']['search_performed']
    source=result.body['answers']['J5']['fact_check']['claims'][0]['sources'][0]
    assert source['quote_verified'] is True
    assert result.body['answers']['J5']['choice']=='PASS'


def test_fetched_source_with_different_text_cannot_verify_quote():
    result,_=call(response(fact(),web_blocks()),ids=('J5',),state={'question':'색상?','answer':'Alpha is blue.'},
                  fetcher=lambda url:{'status':'ok','text':'Alpha is red.','final_url':url,'content_type':'text/plain'})
    assert result.body['answers']['J5']['choice']=='UNVERIFIED'
    assert result.body['answers']['J5']['fact_check']['claims'][0]['sources'][0]['quote_verified'] is False


@pytest.mark.parametrize('blocks',[[],[{'type':'web_search_tool_result','tool_use_id':'forged','content':[
    {'type':'web_search_result','url':'https://evidence.example/page'}]}],
    [{'type':'server_tool_use','id':'search1','name':'web_search'},
     {'type':'web_search_tool_result','tool_use_id':'search1','content':{'type':'web_search_tool_result_error','error_code':'unavailable'}}]])
def test_unobserved_or_failed_search_never_accepts_model_authored_sources(blocks):
    result,_=call(response(fact(),blocks),ids=('J5',),state={'question':'색상?','answer':'Alpha is blue.'},
                  fetcher=lambda url:pytest.fail('unobserved URL must not be opened'))
    assert result.body['answers']['J5']['choice']=='UNVERIFIED'


def fetch_blocks(url, *, error=False):
    content = ({'type':'web_fetch_tool_error','error_code':'url_not_accessible'} if error else
               {'type':'web_fetch_result','url':url,'content':{'type':'document','source':{'type':'text','data':'Alpha is blue.'}}})
    return [{'type':'server_tool_use','id':'fetch1','name':'web_fetch','input':{'url':url}},
            {'type':'web_fetch_tool_result','tool_use_id':'fetch1','content':content}]


def test_page_opened_without_a_search_verifies_its_quote():
    # F1 (G4-01): the provider's own web_fetch result is retrieval provenance even without a search.
    fetched=[]
    def fetcher(url):
        fetched.append(url)
        return {'status':'ok','text':'Alpha is blue.','final_url':url,'content_type':'text/plain'}
    result,_=call(response(fact(),fetch_blocks('https://evidence.example/page')),ids=('J5',),
                  state={'question':'색상?','answer':'Alpha is blue.'},fetcher=fetcher)
    verification=result.body['verification']
    assert verification['search_performed'] is False and verification['fetched_urls']==['https://evidence.example/page']
    source=result.body['answers']['J5']['fact_check']['claims'][0]['sources'][0]
    assert fetched==['https://evidence.example/page'] and source['retrieval']=='web_fetch' and source['quote_verified'] is True
    assert result.body['answers']['J5']['choice']=='PASS'


@pytest.mark.parametrize('blocks',[fetch_blocks('https://other.example/page'),
                                   fetch_blocks('https://evidence.example/page', error=True)])
def test_failed_or_different_page_fetch_never_accepts_the_cited_url(blocks):
    result,_=call(response(fact(),blocks),ids=('J5',),state={'question':'색상?','answer':'Alpha is blue.'},
                  fetcher=lambda url:pytest.fail('unobserved URL must not be opened'))
    assert result.body['verification']['search_performed'] is False
    assert result.body['answers']['J5']['choice']=='UNVERIFIED'


PAGE = 'https://evidence.example/page'


def provider_fetch(text='Alpha is blue.', *, url=PAGE, requested=PAGE, source=None):
    return [{'type':'server_tool_use','id':'fetch1','name':'web_fetch','input':{'url':requested}},
            {'type':'web_fetch_tool_result','tool_use_id':'fetch1','content':{
                'type':'web_fetch_result','url':url,'retrieved_at':'2026-10-12T01:02:03Z',
                'content':{'type':'document','source':source or {'type':'text','media_type':'text/plain','data':text}}}}]


def test_blocked_source_is_checked_against_the_providers_own_fetch():
    # F5b: the server gets 403, the provider's web_fetch of the same URL returned the page text.
    result,_=call(response(fact(),provider_fetch()),ids=('J5',),state={'question':'색상?','answer':'Alpha is blue.'},
                  fetcher=lambda url:{'status':'http_error','detail':'http_403','text':''})
    source=result.body['answers']['J5']['fact_check']['claims'][0]['sources'][0]
    assert (source['quote_verified'], source['retrieval'], source['server_fetch_status']) == (True, 'provider_document', 'http_error')
    assert source['verified_at'] == '2026-10-12T01:02:03Z' and result.body['answers']['J5']['choice'] == 'PASS'
    verification=result.body['verification']
    assert verification['source_checks'][0]['status'] == 'http_error' and verification['source_checks'][0]['provider_document'] is True
    assert verification['provider_documents'] == [{'url':PAGE,'retrieved_at':'2026-10-12T01:02:03Z','chars':14}]


@pytest.mark.parametrize('server,blocks,retrieval', [
    ({'status':'blocked_address'}, provider_fetch(), 'web_fetch'),              # 공개 주소 검사 실패는 대신하지 않음
    ({'status':'redirect_host_changed'}, provider_fetch(), 'web_fetch'),        # 다른 발행처로 이동
    ({'status':'http_error'}, provider_fetch(requested='https://evidence.example/other'), 'web_fetch'),  # 요청·결과 URL 불일치
    ({'status':'http_error'}, provider_fetch(source={'type':'base64','media_type':'application/pdf','data':'JVBERi0='}), 'web_fetch'),
    ({'status':'http_error'}, provider_fetch('Alpha is red.'), 'provider_document'),  # 원문에 인용문이 없음
])
def test_provider_text_replaces_only_access_failures_with_a_linked_document_containing_the_quote(server, blocks, retrieval):
    result,_=call(response(fact(),blocks),ids=('J5',),state={'question':'색상?','answer':'Alpha is blue.'},
                  fetcher=lambda url:{**server,'text':''})
    source=result.body['answers']['J5']['fact_check']['claims'][0]['sources'][0]
    assert source['quote_verified'] is False and source['retrieval'] == retrieval
    assert result.body['answers']['J5']['choice'] == 'UNVERIFIED'


def test_blocked_source_with_a_provider_fetched_contradiction_is_a_factual_error():
    # D2-02: the server cannot read the page; the provider's fetched text contradicts the answer.
    answer = 'The tower opened in 1925.'
    values = {'J5':{'choice':'FAIL','confidence':.9,'reason':'개장 연도가 다릅니다.',
        'fact_check':{'basis':'web','claims':[{'claim':answer,'verdict':'FAIL','reason':'공식 자료는 1889년',
            'sources':[{'url':PAGE,'title':'Official','quote':'The tower opened in 1889.','authority':'primary',
                        'authority_reason':'운영 기관 공식 소개','temporal_relevance':'역사적 사실'}]}]}}}
    client = AnthropicSemanticClient(AnthropicSemanticConfig(), 'secret-test-key',
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response(values, provider_fetch('History: The tower opened in 1889.')))),
        source_fetcher=lambda url:{'status':'http_error','detail':'http_403','text':''})
    record = evaluate_jev_case('q', '탑은 언제 열었어?', answer, client, criteria=('J5',), fail_evidence_check=True)
    j5 = record['decisions']['J5']
    assert j5['decision'] == 'FAIL' and j5.get('reason_code') is None
    assert j5['j5_outcome']['code'] == 'contradicted' and j5['j5_outcome']['factual_error_confirmed'] is True


def test_pending_server_tools_cannot_be_confused_with_a_final_verdict():
    result,_=call(response(entries('J1'),[{'type':'server_tool_use','id':'pending','name':'web_search'}]))
    assert result.status=='INVALID_RESPONSE'


@pytest.mark.parametrize('stop',['max_tokens','refusal','end_turn','model_context_window_exceeded'])
def test_incomplete_stops_do_not_become_success_or_retry(stop):
    result,sent=call(response(stop_reason=stop))
    assert result.status=='INVALID_RESPONSE' and len(sent)==1


@pytest.mark.parametrize('status',[401,403,429,500,302])
def test_http_errors_do_not_retry_follow_redirect_or_expose_response(status):
    result,sent=call({'error':{'message':'secret-test-key private answer'}},status=status)
    assert result.status=='API_ERROR' and len(sent)==1
    assert 'secret-test-key' not in str(result) and 'private answer' not in str(result)


def test_pause_turn_continues_exact_blocks_and_accumulates_each_request_usage():
    paused=[{'type':'thinking','thinking':'opaque','signature':'unchanged'},*web_blocks()]
    sent=[]
    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200,json=response(content=paused,stop_reason='pause_turn') if len(sent)==1 else response())
    client=AnthropicSemanticClient(AnthropicSemanticConfig(),'secret',transport=httpx.MockTransport(handler))
    result=client.system_one({'question':'q','answer':'a'},jev_request('q','a',('J1',))['questions'])
    assert result.status=='OK' and result.attempts==2 and result.usage['input_tokens']==200
    assert sent[1]['messages'][1]=={'role':'assistant','content':paused}
    assert len(result.accounting['cost_parts'])==2 and result.accounting['unreported_attempts']==0
    assert result.accounting['cost_parts'][0]['usage']['service_tier']=='standard'


def test_pause_continuation_limit_and_usage_are_bounded():
    result,sent=call(response(content=[],stop_reason='pause_turn'),config=AnthropicSemanticConfig(max_continuations=1))
    assert result.status=='API_ERROR' and len(sent)==2 and result.attempts==2


def test_pause_continuation_reduces_per_request_tool_budget():
    sent=[]
    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200,json=response(content=web_blocks(),stop_reason='pause_turn') if len(sent)==1 else response(fact()))
    client=AnthropicSemanticClient(AnthropicSemanticConfig(),'secret',transport=httpx.MockTransport(handler),
                                    source_fetcher=lambda url:{'status':'fetch_error'})
    client.system_one({'question':'q','answer':'Alpha is blue.'},jev_request('q','a',('J5',))['questions'])
    assert [t.get('max_uses') for t in sent[1]['tools'][:2]]==[3,7]


def test_pause_after_budget_exhaustion_never_issues_more_requests():
    result,sent=call(response(content=web_blocks(),stop_reason='pause_turn'),ids=('J5',),
                     config=AnthropicSemanticConfig(max_tool_calls=1))
    assert result.status=='API_ERROR' and len(sent)==1 and '한도' in result.error


def test_live_http_can_be_cancelled_without_waiting_for_response():
    token=threading.Event()
    entered=threading.Event()
    cleaned=threading.Event()
    async def handler(req):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            cleaned.set()
        return httpx.Response(200,json=response())
    client=AnthropicSemanticClient(AnthropicSemanticConfig(),'secret',transport=httpx.MockTransport(handler))
    results=[]
    thread=threading.Thread(target=lambda:results.append(client.system_one(
        {'question':'q','answer':'a'},jev_request('q','a',('J1',))['questions'],cancel_event=token)))
    thread.start()
    assert entered.wait(2)
    started=time.monotonic()
    token.set()
    thread.join(3)
    assert not thread.is_alive() and time.monotonic()-started<3 and cleaned.is_set()
    assert results[0].status=='SKIPPED' and results[0].attempts==1
    assert results[0].accounting['cost_parts'][0]['usage']=={}


def test_pre_cancelled_call_never_opens_http():
    token=threading.Event();token.set()
    client=AnthropicSemanticClient(AnthropicSemanticConfig(),'secret',transport=httpx.MockTransport(lambda _:pytest.fail('no request')))
    result=client.system_one({}, {},cancel_event=token)
    assert result.status=='SKIPPED' and result.attempts==0 and result.accounting['cost_parts']==[]


def test_local_confidence_validation_remains_after_strict_schema_translation():
    client=AnthropicSemanticClient(AnthropicSemanticConfig(),'secret',transport=httpx.MockTransport(
        lambda req:httpx.Response(200,json=response({'J1':{'choice':'PASS','confidence':2,'reason':'bad'}}))))
    result=evaluate_jev_case('bad','q','a',client,criteria=('J1',))
    assert result['status']=='INVALID_RESPONSE'


def test_timing_separates_provider_wait_from_source_verification():
    async def handler(req):
        await asyncio.sleep(.03)
        return httpx.Response(200, json=response(fact(), web_blocks()))
    def fetcher(url):
        time.sleep(.02)
        return {'status': 'ok', 'text': 'Alpha is blue.', 'final_url': url, 'content_type': 'text/plain'}
    client = AnthropicSemanticClient(AnthropicSemanticConfig(), 'secret',
        transport=httpx.MockTransport(handler), source_fetcher=fetcher)
    result = evaluate_jev_case('timing', '색상?', 'Alpha is blue.', client, criteria=('J5',))
    timings = result['verification']['timings_ms']
    assert result['status'] == 'SUCCESS' and result['decisions']['J5']['decision'] == 'PASS'
    assert timings['api'] >= 25 and timings['source_verification'] >= 15
    assert timings['total'] >= timings['api'] + timings['source_verification'] - .2
    assert abs(timings['total'] - result['latency_ms']) <= 2


def test_failed_request_retains_elapsed_timing_without_fabricated_source_time():
    result, _ = call({'error': {'message': 'private'}}, status=429)
    timings = result.body['verification']['timings_ms']
    assert result.status == 'API_ERROR' and result.attempts == 1
    assert timings['api'] >= 0 and timings['source_verification'] == 0
    assert timings['total'] >= timings['api']


def test_completed_turn_without_search_blocks_records_zero_when_counter_is_omitted():
    raw = response(entries('J1', 'J5'))
    raw['usage'].pop('server_tool_use')
    result, _ = call(raw, ids=('J1', 'J5'))
    assert result.status == 'OK'
    assert result.accounting['cost_parts'][0]['web_search_calls'] == 0
    audit = result.body['verification']['usage_requests']
    assert audit[0]['usage']['inference_geo'] == 'global'
    assert audit[0]['service_tier'] == 'standard' and audit[0]['web_search_calls'] == 0
    assert audit == result.accounting['cost_parts'] and audit is not result.accounting['cost_parts']
    assert 'secret-test-key' not in json.dumps(audit)


@pytest.mark.parametrize('stop', ['tool_use', 'max_tokens'])
def test_search_without_a_billable_counter_remains_unknown(stop):
    raw = response(fact(), web_blocks(), stop_reason=stop)
    raw['usage'].pop('server_tool_use')
    result, _ = call(raw, ids=('J5',), state={'question': '색상?', 'answer': 'Alpha is blue.'})
    assert result.accounting['cost_parts'][0]['web_search_calls'] is None


def test_failed_finalization_does_not_infer_no_search_from_incomplete_blocks():
    raw = response(stop_reason='max_tokens')
    raw['usage'].pop('server_tool_use')
    result, _ = call(raw, ids=('J1', 'J5'))
    assert result.status == 'INVALID_RESPONSE'
    assert result.accounting['cost_parts'][0]['web_search_calls'] is None


def test_nullable_enums_use_type_correct_branches_for_messages_api():
    schema = {'type': ['string', 'null'], 'enum': ['input_consistency', 'external_fact', None]}
    result = _strict_schema(schema)
    assert result == {'anyOf': [{'type': 'string', 'enum': ['input_consistency', 'external_fact']},
                               {'type': 'null', 'enum': [None]}]}
    assert schema['type'] == ['string', 'null']
    assert _strict_schema({'type': ['integer', 'null']}) == {'anyOf': [{'type': 'integer'}, {'type': 'null'}]}


def text_response(text, blocks=(), **extra):
    return response(content=[*blocks, {'type': 'text', 'text': text}], stop_reason='end_turn', **extra)


def schema_fact():
    values = fact()
    claim = values['J5']['fact_check']['claims'][0]
    claim.update(scope='external_fact', scope_quote='')
    claim['sources'][0].update(origin='web', turn=None)
    return values


def nonfact():
    return {'J5': {'choice': 'NOT_APPLICABLE', 'confidence': 0.8, 'reason': '인사만 있습니다.',
                   'fact_check': {'basis': 'none', 'claims': []}}}


@pytest.mark.parametrize('form', ['plain', 'fenced', 'split'])
def test_completed_text_json_uses_original_schema_without_an_extra_request(form):
    values = entries('J1', 'J2')
    values['J1']['reason'] = '  원문\r\n```json {"literal":"\\n"} 😀  '
    encoded = json.dumps(values, ensure_ascii=False)
    if form == 'fenced':
        encoded = ' \r\n```json\r\n' + encoded + '\r\n```\r\n '
    raw = text_response(encoded)
    if form == 'split':
        midpoint = len(encoded) // 2
        raw['content'] = [{'type': 'thinking', 'thinking': 'not final output', 'signature': 'opaque'},
                          {'type': 'text', 'text': encoded[:midpoint]}, {'type': 'text', 'text': encoded[midpoint:]}]
    original = copy.deepcopy(raw)
    result, sent = call(raw, ids=('J1', 'J2'))
    assert result.status == 'OK' and result.attempts == len(sent) == 1 and raw == original
    assert result.body['answers']['J1']['reason'] == values['J1']['reason']
    assert result.body['verification']['output_transport'] == 'validated_text_json'
    assert result.body['verification']['provider_stop_reason'] == 'end_turn'
    assert len(result.body['verification']['usage_requests']) == 1
    assert AnthropicSemanticConfig().to_manifest()['output_mode'] == 'strict_submit_evaluation_tool_or_validated_text_json_v3'


@pytest.mark.parametrize('text', [
    '{}', '[]', 'null', '단순한 설명입니다.',
    '{"J1":{"choice":"PASS","confidence":0.8,"reason":"ok"}',
    '{"J1":{"choice":"PASS","confidence":NaN,"reason":"ok"}}',
    '{"J1":{"choice":"PASS","confidence":Infinity,"reason":"ok"}}',
    '{"J1":{"choice":"PASS","confidence":1e999,"reason":"ok"}}',
    '{"J1":{"choice":"PASS","confidence":0.8,"reason":"first","reason":"second"}}',
    '{"J1":{"choice":"PASS","confidence":0.8,"reason":"first"},"J1":{"choice":"FAIL","confidence":0.8,"reason":"second"}}',
    '설명: ' + json.dumps(entries('J1')),
    json.dumps(entries('J1')) + '\n추가 설명',
    json.dumps(entries('J1')) + json.dumps(entries('J1')),
    '```json\n' + json.dumps(entries('J1')),  # Incomplete fence.
    '```json\n' + json.dumps(entries('J1')) + '\n```\n설명',
    '```json\n' + json.dumps(entries('J1')) + '\n```\n```json\n{}\n```',
])
def test_text_fallback_rejects_malformed_or_ambiguous_json_without_retry(text):
    raw = text_response(text)
    raw['usage'].pop('server_tool_use')
    result, sent = call(raw)
    assert result.status == 'INVALID_RESPONSE' and len(sent) == result.attempts == 1
    assert 'output_transport' not in result.body['verification']


@pytest.mark.parametrize('mutation', [
    lambda v: v['J1'].pop('reason'),
    lambda v: v['J1'].update(confidence=True),
    lambda v: v['J1'].update(confidence='0.8'),
    lambda v: v['J1'].update(confidence=1.01),
    lambda v: v['J1'].update(confidence=-.01),
    lambda v: v['J1'].update(choice='NOT_APPLICABLE'),
    lambda v: v['J1'].update(reason=None),
    lambda v: v['J1'].update(model_original_assessment={'choice': 'PASS'}),
    lambda v: v.update(verification={'source_validation': 'provider_and_content'}),
    lambda v: v.update(J2=entries('J2')['J2']),
    lambda v: v.update(J1=[]),
])
def test_text_fallback_enforces_original_required_types_enum_bounds_and_no_extra_fields(mutation):
    values = entries('J1')
    mutation(values)
    result, sent = call(text_response(json.dumps(values)))
    assert result.status == 'INVALID_RESPONSE' and len(sent) == 1


@pytest.mark.parametrize('mutation', [
    lambda c: c.pop('scope'),
    lambda c: c.update(scope=None),
    lambda c: c.update(sources={}),
    lambda c: c['sources'][0].pop('origin'),
    lambda c: c['sources'][0].update(origin=None),
    lambda c: c['sources'][0].update(turn=True),
    lambda c: c['sources'][0].update(quote_verified=True),
    lambda c: c['sources'][0].update(verification_reason='verified by model'),
])
def test_text_fact_schema_rejects_missing_or_forged_evidence_fields_before_source_fetch(mutation):
    values = schema_fact()
    mutation(values['J5']['fact_check']['claims'][0])
    result, sent = call(text_response(json.dumps(values), web_blocks()), ids=('J5',),
                       fetcher=lambda _: pytest.fail('Invalid schema must never open a source'))
    assert result.status == 'INVALID_RESPONSE' and len(sent) == 1


@pytest.mark.parametrize('stop', ['max_tokens', 'refusal', 'tool_use', 'stop_sequence', 'model_context_window_exceeded'])
def test_only_end_turn_can_accept_text_json(stop):
    raw = text_response(json.dumps(nonfact()))
    raw['stop_reason'] = stop
    raw['usage'].pop('server_tool_use')
    result, sent = call(raw, ids=('J5',))
    assert result.status == 'INVALID_RESPONSE' and len(sent) == 1
    assert result.accounting['cost_parts'][0]['web_search_calls'] is None


@pytest.mark.parametrize('blocks', [
    [{'type': 'tool_use', 'id': 'other', 'name': 'other_tool', 'input': {}}],
    [{'type': 'tool_use', 'id': 'final', 'name': FINAL_TOOL, 'input': entries('J1')}],
    [{'type': 'server_tool_use', 'id': 'pending', 'name': 'web_search'}],
    [{'type': 'web_search_tool_result', 'tool_use_id': 'orphan', 'content': []}],
    [{'type': 'server_tool_use', 'id': 'x', 'name': 'web_search'},
     {'type': 'web_search_tool_result', 'tool_use_id': 'x', 'content': 'not a completed search result'}],
    [{'type': 'server_tool_use', 'id': 'x', 'name': 'web_search'},
     {'type': 'web_search_tool_result', 'tool_use_id': 'x', 'content': ['invalid result']}],
    [{'type': 'compaction', 'content': 'history removed'}],
    [{'type': 'future_unknown_block', 'content': 'unknown'}],
])
def test_text_fallback_never_hides_client_or_incomplete_server_tools(blocks):
    result, sent = call(text_response(json.dumps(entries('J1')), blocks))
    assert result.status == 'INVALID_RESPONSE' and len(sent) == 1


def test_validated_text_without_search_infers_zero_only_after_complete_schema_check():
    values = nonfact()
    raw = text_response(json.dumps(values))
    raw['usage'].pop('server_tool_use')
    result, sent = call(raw, ids=('J5',), state={'question': '안녕', 'answer': '안녕하세요.'})
    assert result.status == 'OK' and len(sent) == 1
    assert result.body['answers']['J5']['choice'] == 'NOT_APPLICABLE'
    assert result.accounting['cost_parts'][0]['web_search_calls'] == 0


def test_schema_invalid_end_turn_does_not_infer_zero_search_from_missing_provider_counter():
    values = nonfact()
    values['J5']['confidence'] = True
    raw = text_response(json.dumps(values))
    raw['usage'].pop('server_tool_use')
    result, sent = call(raw, ids=('J5',))
    assert result.status == 'INVALID_RESPONSE' and len(sent) == 1
    assert result.accounting['cost_parts'][0]['web_search_calls'] is None


@pytest.mark.parametrize('observed,matching', [(True, True), (True, False), (False, True)])
def test_validated_text_still_requires_provider_provenance_and_independent_quote_match(observed, matching):
    fetched = []
    def fetcher(url):
        fetched.append(url)
        return {'status': 'ok', 'text': 'Alpha is blue.' if matching else 'Alpha is red.',
                'final_url': url, 'content_type': 'text/plain'}
    raw = text_response(json.dumps(schema_fact()), web_blocks() if observed else [])
    raw['usage'].pop('server_tool_use')
    result, sent = call(raw, ids=('J5',), state={'question': '색상?', 'answer': 'Alpha is blue.'}, fetcher=fetcher)
    assert result.status == 'OK' and len(sent) == 1
    assert result.body['answers']['J5']['choice'] == ('PASS' if observed and matching else 'UNVERIFIED')
    assert fetched == (['https://evidence.example/page'] if observed else [])
    assert result.accounting['cost_parts'][0]['web_search_calls'] == (None if observed else 0)


@pytest.mark.parametrize('matching', [True, False])
def test_validated_text_still_checks_context_quotes_against_original_turns(matching):
    state = {'question': '계속해줘.', 'answer': '원래 계획을 유지합니다.',
             'history': [{'turn': 1, 'question': '원래 계획을 유지해줘.', 'answer': '알겠습니다.'}]}
    values = {'J6': {'choice': 'PASS', 'confidence': .8, 'reason': '지시 유지', 'context_checks': [
        {'turn': 1, 'instruction_quote': state['history'][0]['question'] if matching else '원문에 없는 지시',
         'answer_quote': state['answer'], 'decision': 'PASS', 'reason': '지시와 일치'}]}}
    original = copy.deepcopy(state)
    result, _ = call(text_response(json.dumps(values)), ids=('J6',), state=state)
    assert result.status == 'OK' and state == original
    assert result.body['answers']['J6']['choice'] == ('PASS' if matching else 'UNVERIFIED')


def test_validated_text_mixed_evidence_preserves_input_and_checks_external_claim_separately():
    values = schema_fact()
    fact_check = values['J5']['fact_check']
    fact_check['basis'] = 'mixed'
    scope = 'Summarize the following document:'
    fact_check['claims'].append({'claim': 'The group has 12 members.', 'verdict': 'PASS', 'reason': 'Matches the provided document',
        'scope': 'input_consistency', 'scope_quote': scope, 'sources': [
            {'origin': 'question', 'turn': None, 'url': '', 'title': 'Provided document',
             'quote': 'The group has 12 members.', 'authority': 'unestablished',
             'authority_reason': 'Only input consistency', 'temporal_relevance': 'Provided fictional setting'}]})
    state = {'question': scope + ' The group has 12 members.', 'answer': 'The group has 12 members. Alpha is blue.'}
    result, sent = call(text_response(json.dumps(values)), ids=('J5',), state=state,
                       fetcher=lambda _: pytest.fail('No observed external URL'))
    assert result.status == 'OK' and len(sent) == 1
    decision = result.body['answers']['J5']
    assert decision['choice'] == 'UNVERIFIED'
    assert [claim['verdict'] for claim in decision['fact_check']['claims']] == ['UNVERIFIED', 'PASS']


def test_text_fallback_after_completed_pause_reuses_provenance_and_accounts_each_request_once():
    sent = []
    def handler(req):
        sent.append(json.loads(req.content))
        if len(sent) == 1:
            raw = response(content=web_blocks(), stop_reason='pause_turn')
            raw['usage']['server_tool_use']['web_search_requests'] = 1
        else:
            raw = text_response(json.dumps(schema_fact()))
            raw['usage'].pop('server_tool_use')
        return httpx.Response(200, json=raw)
    client = AnthropicSemanticClient(AnthropicSemanticConfig(), 'fixture', transport=httpx.MockTransport(handler),
        source_fetcher=lambda url: {'status': 'ok', 'text': 'Alpha is blue.', 'final_url': url, 'content_type': 'text/plain'})
    result = client.system_one({'question': '색상?', 'answer': 'Alpha is blue.'}, jev_request('q', 'a', ('J5',))['questions'])
    assert result.status == 'OK' and len(sent) == result.attempts == 2
    assert result.body['answers']['J5']['choice'] == 'PASS'
    assert result.body['verification']['output_transport'] == 'validated_text_json'
    assert [part['web_search_calls'] for part in result.accounting['cost_parts']] == [1, 0]


def test_text_after_pause_cannot_hide_an_earlier_client_tool_submission():
    sent = []
    def handler(req):
        sent.append(req)
        return httpx.Response(200, json=response(stop_reason='pause_turn') if len(sent) == 1 else
                              text_response(json.dumps(entries('J1'))))
    client = AnthropicSemanticClient(AnthropicSemanticConfig(), 'fixture', transport=httpx.MockTransport(handler))
    result = client.system_one({'question': 'q', 'answer': 'a'}, jev_request('q', 'a', ('J1',))['questions'])
    assert result.status == 'INVALID_RESPONSE' and len(sent) == 2


def test_text_validator_rejects_future_schema_constraints_instead_of_silently_ignoring(monkeypatch):
    from code_eval import semantic_cli_common
    original = semantic_cli_common.output_schema
    def with_pattern(ids, **kwargs):
        schema = original(ids, **kwargs)
        schema['properties']['J1']['properties']['reason']['pattern'] = '^specific$'
        return schema
    monkeypatch.setattr(semantic_cli_common, 'output_schema', with_pattern)
    with pytest.raises(ValueError, match='Unsupported evaluation schema'):
        _text_submission([{'type': 'text', 'text': json.dumps(entries('J1'))}], ('J1',))


def test_text_schema_keeps_non_j_criteria_pass_fail_only():
    # C 보완 기준은 더 이상 요청하지 않는다. 임의 id 로 스키마 enum(PASS/FAIL) 검증만 남긴다.
    value = {'X1': {'choice': 'UNVERIFIED', 'confidence': .8, 'reason': 'unknown'}}
    with pytest.raises(ValueError, match='enum'):
        _text_submission([{'type': 'text', 'text': json.dumps(value)}], ('X1',))


@pytest.mark.parametrize('config,state,ids,mode', [
    (AnthropicSemanticConfig(), None, ('J1', 'J5'), 'live'),
    (AnthropicSemanticConfig(web_search='disabled'), None, ('J1', 'J5'), 'disabled'),
    (AnthropicSemanticConfig(), {'question': '질문', 'answer': '답변', 'reference': '자료'}, ('J1', 'J5'), 'reference'),
    (AnthropicSemanticConfig(), None, ('J1',), 'not_requested'),
])
def test_verification_records_whether_j5_search_could_run(config, state, ids, mode):
    # The final policy separates "search could not run" from "searched but found too little".
    values = {**entries(*ids)}
    if 'J5' in ids:
        values['J5'] = {'choice': 'NOT_APPLICABLE', 'confidence': .9, 'reason': '사실 없음',
                        'fact_check': {'basis': 'none', 'claims': []}}
    result, sent = call(response(values), ids=ids, state=state, config=config)
    assert result.body['verification']['search_mode'] == mode
    assert result.body['verification']['search_limits'] == {'searches': config.max_tool_calls, 'fetches': config.max_fetches}
    assert any(tool.get('name') == 'web_search' for tool in json.loads(sent[0].content)['tools']) is (mode == 'live')
