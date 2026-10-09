"""GPT·Claude 라우팅, 비용 보존, 배포 상태 보존을 유료 호출 없이 검증한다."""
import asyncio
import json
from pathlib import Path
import time

import httpx
import pytest

from gateway.api import create_app
from gateway.core import GatewayCore, GatewayError
from gateway.deploy import configure_runtime, public_origin
from gateway.provider import AnthropicMessagesProvider, ProviderRouter
from gateway.server import create_server


@pytest.fixture(autouse=True)
def isolated_runtime_environment(monkeypatch):
    # 배포 준비 함수가 바꾸는 환경 변수를 다른 테스트에 남기지 않는다.
    for key in ['ACM_CONFIG_FILE', 'ACM_SECRET_FILE', 'ACM_LEDGER_FILE',
        'ACM_CONFIG_JSON', 'ACM_STATE_DIR', 'ACM_RUN_UID', 'ACM_PUBLIC_ORIGIN',
        'ACM_OPENAI_KEY_FILE', 'ACM_ANTHROPIC_KEY_FILE', 'ACM_PROVIDER_KEY_FILE',
        'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'HTTP_PROXY', 'HTTPS_PROXY',
        'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']:
        monkeypatch.setenv(key, '')
        monkeypatch.delenv(key, raising=False)


def payload(model='qa-claude', request_id='claude-1'):
    return {'request_id': request_id, 'model': model, 'approved_micro_usd': 17000,
        'room_id': 'room-jun', 'character_id': 'jun', 'context': {},
        'history': [{'role': 'assistant', 'text': '앞선 메시지'}],
        'message': {'text': '답장해 줘'}}


def response(stop='end_turn', **usage):
    return {'id': 'msg-test-1', 'type': 'message', 'stop_reason': stop, 'stop_details': None,
        'content': [{'type': 'text', 'text': json.dumps({'messages': ['하나', '둘', '한 말풍선\n줄바꿈']})}],
        'usage': {'input_tokens': 80, 'output_tokens': 30, 'cache_read_input_tokens': 20,
            'cache_creation_input_tokens': 0, 'output_tokens_details': {'thinking_tokens': 10}, **usage}}


def config():
    item = {'input_price': '1', 'cached_input_price': '.1', 'output_price': '2',
        'max_input_tokens': 16000, 'max_output_tokens': 500,
        'price_verified_at': time.time(), 'vision': False}
    return {'models': {'qa-gpt': {**item, 'provider': 'openai'},
        'qa-claude': {**item, 'provider': 'anthropic'}},
        'budgets': {'day_micro_usd': 18000, 'month_micro_usd': 18000, 'request_micro_usd': 17000}}


def test_claude_wire_schema_images_and_usage_preserve_items():
    captured = []
    def transport(request):
        captured.append(request)
        return httpx.Response(200, json=response())
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            provider = AnthropicMessagesProvider('synthetic-key', client)
            p = payload(); p['message']['image_base64'] = '/9j/AA=='
            result, usage, receipt = await provider.generate(p, {'output_limit': 500})
            assert result['messages'] == ['하나', '둘', '한 말풍선\n줄바꿈']
            assert usage == {'input_tokens': 100, 'output_tokens': 30, 'cached_input_tokens': 20, 'reasoning_tokens': 10}
            assert receipt == 'msg-test-1'
    asyncio.run(run())
    request = captured[0]; body = json.loads(request.content)
    assert str(request.url) == 'https://api.anthropic.com/v1/messages'
    assert request.headers['x-api-key'] == 'synthetic-key'
    assert request.headers['anthropic-version'] == '2023-06-01'
    assert 'authorization' not in request.headers
    assert body['system'] and body['messages'][0]['role'] == 'assistant'
    assert body['messages'][-1]['content'][0]['source']['media_type'] == 'image/jpeg'
    assert body['output_config']['format']['type'] == 'json_schema'
    assert body['max_tokens'] == 500 and body['tools'] == []
    assert 'cache_control' not in json.dumps(body)


@pytest.mark.parametrize('stop', ['refusal', 'max_tokens'])
def test_refusal_and_truncation_settle_known_usage_without_retry(stop):
    calls = []
    def transport(request):
        calls.append(request)
        value = response(stop); value['content'][0]['text'] = '불완전한 응답'
        return httpx.Response(200, json=value)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result, usage, receipt = await AnthropicMessagesProvider('test', client).generate(payload(), {'output_limit': 500})
            assert result == {'messages': []} and usage['input_tokens'] == 100
            assert receipt == 'msg-test-1'
    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize('usage', [{'cache_creation_input_tokens': 5}, {'input_tokens': -1}, {'output_tokens': True}])
def test_unknown_or_invalid_usage_is_not_guessed(usage):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response(**usage)))) as client:
            with pytest.raises(ValueError):
                await AnthropicMessagesProvider('test', client).generate(payload(), {'output_limit': 500})
    asyncio.run(run())


def test_routing_uses_config_not_model_name_and_never_falls_back():
    calls = []
    class Provider:
        def __init__(self, name): self.name = name
        async def generate(self, p, limits): calls.append(self.name); return self.name
    router = ProviderRouter({'gpt-looking-name': {'provider': 'anthropic'}, 'legacy': {},
        'missing': {'provider': 'unknown'}}, {'openai': Provider('gpt'), 'anthropic': Provider('claude')})
    async def run():
        assert await router.generate({'model': 'gpt-looking-name'}, {}) == 'claude'
        assert await router.generate({'model': 'legacy'}, {}) == 'gpt'
        with pytest.raises(GatewayError, match='provider_unconfigured'):
            await router.generate({'model': 'missing'}, {})
    asyncio.run(run())
    assert calls == ['claude', 'gpt']


def test_api_claude_idempotency_shared_cap_and_missing_key_no_hold(tmp_path):
    calls = []
    def transport(request): calls.append(request); return httpx.Response(200, json=response())
    core = GatewayCore(tmp_path/'ledger.sqlite', b'x'*32, config())
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as upstream:
            router = ProviderRouter(core.config['models'], {'anthropic': AnthropicMessagesProvider('test', upstream)})
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(core, router)), base_url='https://gateway.test') as client:
                session = (await client.post('/v1/pair', json={'code': core.pairing_code()})).json()
                headers = {'Authorization': 'Bearer '+session['access_token']}
                listed = (await client.get('/v1/models', headers=headers)).json()['models']
                assert [(m['id'],m['provider']) for m in listed] == [('qa-claude','anthropic')]
                assert (await client.post('/v1/quote', headers=headers, json=payload('qa-gpt'))).status_code == 503
                assert (await client.post('/v1/generate', headers=headers, json=payload('qa-gpt'))).status_code == 503
                assert core.costs()['held'] == 0 and calls == []
                p = payload()
                assert (await client.post('/v1/quote', headers=headers, json=p)).status_code == 200
                assert calls == []
                first = (await client.post('/v1/generate', headers=headers, json=p)).json()
                assert first['actual_micro_usd'] == 142 and first['status'] == 'completed'
                assert (await client.post('/v1/generate', headers=headers, json=p)).json() == first
                assert len(calls) == 1
                core.config['budgets']['day_micro_usd'] = 17000
                capped = await client.post('/v1/generate', headers=headers, json=payload(request_id='second'))
                assert capped.status_code == 402 and len(calls) == 1
                assert core.costs()['held'] == 0
    asyncio.run(run())


def test_api_unexpected_cache_cost_retains_hold_and_lookup_never_retries(tmp_path):
    calls = []
    def transport(request): calls.append(request); return httpx.Response(200, json=response(cache_creation_input_tokens=5))
    core = GatewayCore(tmp_path/'ledger.sqlite', b'x'*32, config())
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as upstream:
            router = ProviderRouter(core.config['models'], {'anthropic': AnthropicMessagesProvider('test', upstream)})
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(core, router)), base_url='https://gateway.test') as client:
                session = (await client.post('/v1/pair', json={'code': core.pairing_code()})).json()
                headers = {'Authorization': 'Bearer '+session['access_token']}
                assert (await client.post('/v1/generate', headers=headers, json=payload())).status_code == 202
                assert core.costs()['held'] == 17000
                assert (await client.get('/v1/requests/claude-1', headers=headers)).json()['status'] == 'uncertain'
                assert (await client.post('/v1/generate', headers=headers, json=payload())).json()['status'] == 'uncertain'
                assert len(calls) == 1 and core.costs()['held'] == 17000
    asyncio.run(run())


def test_runtime_preserves_secret_ledger_config_and_keeps_keys_private(tmp_path, monkeypatch):
    monkeypatch.setenv('ACM_STATE_DIR', str(tmp_path))
    monkeypatch.delenv('ACM_RUN_UID', raising=False)
    monkeypatch.setenv('ACM_CONFIG_JSON', json.dumps(config()))
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-openai')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'synthetic-claude')
    configure_runtime()
    secret = (tmp_path/'secret.bin').read_bytes()
    core = GatewayCore(tmp_path/'ledger.sqlite', secret, config())
    code = core.pairing_code()
    configure_runtime()
    assert (tmp_path/'secret.bin').read_bytes() == secret
    assert len(secret) == 32 and core.pair(code)['access_token']
    assert 'OPENAI_API_KEY' not in __import__('os').environ
    assert 'ANTHROPIC_API_KEY' not in __import__('os').environ
    assert (tmp_path/'openai.credential').stat().st_mode & 0o777 == 0o600
    assert (tmp_path/'anthropic.credential').stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path/'config.json').read_text())['models']['qa-claude']['provider'] == 'anthropic'
    app = create_server()
    async def health():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://private.test') as client:
            assert (await client.get('/health')).json() == {
                'status': 'ok', 'features': {'proactive_generation': 1},
            }
            assert (await client.get('/v1/models')).status_code == 400
        async with app.router.lifespan_context(app): pass
    asyncio.run(health())


def test_missing_original_secret_does_not_replace_existing_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv('ACM_STATE_DIR', str(tmp_path))
    (tmp_path/'ledger.sqlite').write_bytes(b'preserved')
    with pytest.raises(ValueError, match='original secret'): configure_runtime()
    assert (tmp_path/'ledger.sqlite').read_bytes() == b'preserved'
    assert not (tmp_path/'secret.bin').exists()


@pytest.mark.parametrize('origin', ['https://host.example/path', 'http://host.example', 'https://host.example?token=secret'])
def test_pair_origin_must_be_an_https_origin(monkeypatch, origin):
    monkeypatch.setenv('ACM_PUBLIC_ORIGIN', origin)
    with pytest.raises(ValueError): public_origin()
