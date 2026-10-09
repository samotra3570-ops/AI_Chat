"""선톡 목적·비용·멱등성과 공급자 입력을 실제 유료 호출 없이 검증한다."""
import asyncio
import json
import httpx
import pytest
from test_gateway import core, payload, usage
from gateway.core import GatewayError
from gateway.api import create_app
from gateway.provider import OpenAIResponsesProvider, AnthropicMessagesProvider, PROACTIVE_INSTRUCTION


def proactive(request_id='proactive-1'):
    p = payload(request_id)
    p.update(purpose='proactive', message={'text': ''}, history=[{'role': 'user', 'text': '오늘 연습 잘 했어?'}, {'role': 'assistant', 'text': '응, 좀 쉬는 중'}])
    return p


def test_quote_is_free_and_explicitly_confirms_proactive_support(core):
    p = proactive()
    assert core.quote(p)['purpose'] == 'proactive'
    assert core.costs()['held'] == 0
    assert core.quote(payload()).get('purpose') is None
    with core.connection() as db:
        assert db.execute('SELECT count(*) FROM requests').fetchone()[0] == 0


@pytest.mark.parametrize('patch,code', [({'purpose': 'unknown'}, 'invalid_purpose'), ({'purpose': True}, 'invalid_purpose'),
    ({'message': {'text': '가짜 사용자 메시지'}}, 'invalid_proactive_message'),
    ({'message': {'image_base64': '/9j/AA=='}}, 'invalid_proactive_message'),
    ({'regenerate_of': 'r1'}, 'invalid_proactive_message'), ({'history': []}, 'proactive_history_required'),
    ({'history': [{'role': 'assistant', 'text': '이전 답변'}]}, 'proactive_history_required')])
def test_invalid_proactive_input_fails_before_reservation(core, patch, code):
    p = proactive(); p.update(patch)
    with pytest.raises(GatewayError, match=code): core.reserve(p)
    assert core.costs()['held'] == 0


def test_proactive_uses_same_hard_cap_and_confirmation(core):
    p = proactive(); p['approved_micro_usd'] = 4999
    with pytest.raises(GatewayError, match='cost_changed'): core.reserve(p)
    p['approved_micro_usd'] = 5000
    core.config['budgets']['request_micro_usd'] = 4999
    with pytest.raises(GatewayError, match='hard_cap'): core.reserve(p)
    assert core.costs()['held'] == 0


def test_purpose_is_hashed_and_completed_request_is_never_reserved_twice(core):
    p = proactive()
    assert core.reserve(p)[0]
    r = core.settle(p['request_id'], {'messages': ['잘 지내?']}, usage(), 'synthetic-upstream')
    assert not core.reserve(p)[0]
    assert core.lookup(p['request_id']) == r
    changed = payload(p['request_id'])
    with pytest.raises(GatewayError, match='idempotency_conflict'): core.reserve(changed)
    assert core.costs()['held'] == 0


def test_api_requires_explicit_cost_approval_and_lookup_only_does_not_generate(core):
    calls = []
    class Provider:
        async def generate(self, p, limits):
            calls.append(p)
            return {'messages': ['연습 끝났어. 뭐 해?']}, usage(), 'synthetic-api-upstream'
    async def run():
        app = create_app(core, Provider())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test') as client:
            paired = await client.post('/v1/pair', json={'code': core.pairing_code()})
            headers = {'authorization': 'Bearer ' + paired.json()['access_token']}
            p = proactive(); del p['approved_micro_usd']
            denied = await client.post('/v1/generate', json=p, headers=headers)
            assert denied.json()['error'] == 'cost_confirmation_required' and calls == []
            quoted = await client.post('/v1/quote', json=p, headers=headers)
            assert quoted.json()['purpose'] == 'proactive' and calls == []
            p['approved_micro_usd'] = quoted.json()['reserved_micro_usd']
            first = await client.post('/v1/generate', json=p, headers=headers)
            second = await client.post('/v1/generate', json=p, headers=headers)
            looked = await client.get('/v1/requests/' + p['request_id'], headers=headers)
            assert first.json() == second.json() == looked.json()
            assert len(calls) == 1 and first.json()['status'] == 'completed'
            assert (await client.get('/health')).json()['features']['proactive_generation'] == 1
    asyncio.run(run())


def test_openai_proactive_has_internal_developer_task_and_no_new_user_input():
    captured = []
    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'synthetic-openai', 'status': 'completed', 'usage': {'input_tokens': 100, 'output_tokens': 30},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({'messages': ['연습 끝났어', '뭐 해?']})}]}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result, actual, receipt = await OpenAIResponsesProvider('synthetic', client).generate(proactive(), {'output_limit': 500})
            assert result['messages'] == ['연습 끝났어', '뭐 해?'] and actual['output_tokens'] == 30
    asyncio.run(run())
    body = captured[0]
    assert body['input'][-1] == {'role': 'developer', 'content': PROACTIVE_INSTRUCTION}
    assert sum(m['role'] == 'user' for m in body['input']) == 1
    assert body['store'] is False and body['tools'] == []


def test_claude_proactive_task_is_explicitly_internal_and_not_conversation_history():
    captured = []
    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'synthetic-claude', 'stop_reason': 'end_turn',
            'usage': {'input_tokens': 100, 'output_tokens': 30},
            'content': [{'type': 'text', 'text': json.dumps({'messages': ['뭐 해?']})}]})
    async def run():
        p = proactive(); original = json.dumps(p['history'])
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result, actual, receipt = await AnthropicMessagesProvider('synthetic', client).generate(p, {'output_limit': 500})
            assert result['messages'] == ['뭐 해?'] and json.dumps(p['history']) == original
    asyncio.run(run())
    assert captured[0]['messages'][-1]['content'][0]['text'] == PROACTIVE_INSTRUCTION
    assert captured[0]['messages'][:-1] == [{'role': m['role'], 'content': m['text']} for m in proactive()['history']]
