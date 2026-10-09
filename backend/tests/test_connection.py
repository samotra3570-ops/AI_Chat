"""재사용 연결·기존 인증·예산 보존 검증. 외부 모델 호출 없음."""
import asyncio
import hashlib
import json
import secrets

import httpx
import pytest

from gateway.api import create_app
from gateway.core import GatewayCore, GatewayError
from gateway.server import create_server
from test_gateway import core, payload, usage


def connection_code():
    return 'acm_connect_' + secrets.token_urlsafe(32)


def install(core, code):
    core.configure_connection_code(hashlib.sha256(code.encode()).hexdigest())


def test_reconnect_after_weeks_and_restart_keeps_owner_data(core):
    legacy = core.pair(core.pairing_code())
    core.update_settings({'expected_revision': 0, 'budgets': {
        'request_micro_usd': 5000, 'day_micro_usd': 11000, 'month_micro_usd': 25000}}, 'owner')
    core.reserve(payload())
    core.settle('r1', {'messages': ['이전 답장']}, usage(), 'receipt-before-connection')
    saved_settings, saved_request = core.settings(), core.lookup('r1')
    code = connection_code()
    install(core, code)
    first, second = core.pair(code, 'phone-one'), core.pair(code, 'phone-two')
    assert first['access_token'] != second['access_token']
    assert core.authenticate(first['access_token']) != core.authenticate(second['access_token'])
    assert core.authenticate(legacy['access_token'])
    assert core.settings() == saved_settings and core.lookup('r1') == saved_request
    core.test_clock[0] += 40 * 86400
    restarted = GatewayCore(core.path, core.key, core.config, core.clock)
    restored = restarted.pair(code, 'reinstalled-phone')
    assert restarted.authenticate(restored['access_token'])
    assert restarted.settings() == saved_settings and restarted.lookup('r1') == saved_request
    assert restarted.costs()['held'] == 0


def test_rotation_rejects_old_code_and_preserves_existing_sessions(core):
    old, new = connection_code(), connection_code()
    install(core, old)
    existing = core.pair(old)
    with core.connection() as db:
        initial = dict(db.execute('SELECT * FROM owner_connection').fetchone())
    install(core, old)
    with core.connection() as db:
        assert dict(db.execute('SELECT * FROM owner_connection').fetchone()) == initial
    install(core, new)
    with pytest.raises(GatewayError, match='invalid_pairing'):
        core.pair(old)
    assert core.authenticate(existing['access_token'])
    assert core.authenticate(core.pair(new)['access_token'])
    assert core.authenticate(core.pair(core.pairing_code())['access_token'])


def test_existing_ledger_without_connection_table_migrates(core):
    previous_session = core.pair(core.pairing_code())
    core.reserve(payload())
    core.uncertain('r1')
    before = core.costs()
    with core.connection(write=True) as db:
        db.execute('DROP TABLE owner_connection')
    reopened = GatewayCore(core.path, core.key, core.config, core.clock)
    code = connection_code()
    install(reopened, code)
    assert reopened.authenticate(reopened.pair(code)['access_token'])
    assert reopened.authenticate(previous_session['access_token'])
    assert reopened.costs() == before
    assert reopened.lookup('r1')['status'] == 'uncertain'


def test_reusable_sessions_share_budget_and_cannot_reset_holds(core):
    code = connection_code()
    install(core, code)
    for i in range(2):
        session = core.pair(code, f'phone-{i}')
        family = core.authenticate(session['access_token'])
        assert core.reserve(payload(f'r{i}'), family)[0]
    third = core.pair(code, 'third-phone')
    with pytest.raises(GatewayError, match='hard_cap'):
        core.reserve(payload('third'), core.authenticate(third['access_token']))
    assert core.costs()['held'] == 10000


def test_connection_code_is_not_a_request_bearer_or_stored_plaintext(core):
    code = connection_code()
    install(core, code)
    session = core.pair(code)
    with pytest.raises(GatewayError, match='unauthorized'):
        core.authenticate(code)
    with core.connection() as db:
        stored = json.dumps([dict(r) for r in db.execute('SELECT * FROM owner_connection')])
        assert code not in stored
        assert code not in json.dumps([dict(r) for r in db.execute('SELECT * FROM sessions')])
    core.test_clock[0] += 601
    with pytest.raises(GatewayError, match='unauthorized'):
        core.authenticate(session['access_token'])
    rotated = core.refresh(session['refresh_token'])
    with pytest.raises(GatewayError, match='unauthorized'):
        core.refresh(session['refresh_token'])
    with pytest.raises(GatewayError, match='unauthorized'):
        core.authenticate(rotated['access_token'])
    assert core.authenticate(core.pair(code)['access_token'])


def test_invalid_code_attempts_consume_ip_rate_slots(core):
    code = connection_code()
    install(core, code)
    for _ in range(5):
        with pytest.raises(GatewayError, match='invalid_pairing'):
            core.pair(connection_code(), 'same-ip')
    with pytest.raises(GatewayError, match='rate_limit'):
        core.pair(code, 'same-ip')
    core.test_clock[0] += 61
    assert core.authenticate(core.pair(code, 'same-ip')['access_token'])


def test_reusable_session_creation_has_global_owner_rate(core):
    code = connection_code()
    install(core, code)
    for i in range(20):
        core.pair(code, f'ip-{i}')
    with pytest.raises(GatewayError, match='rate_limit'):
        core.pair(code, 'twenty-first-ip')


@pytest.mark.parametrize('value', [None, '', 'not-a-digest', 'F' * 64])
def test_invalid_digest_does_not_replace_existing_code(core, value):
    code = connection_code()
    install(core, code)
    with pytest.raises(ValueError):
        core.configure_connection_code(value)
    assert core.authenticate(core.pair(code)['access_token'])


def test_factory_configures_digest_and_existing_api_accepts_reuse(tmp_path, monkeypatch):
    code = connection_code()
    digest = hashlib.sha256(code.encode()).hexdigest()
    config, key, ledger = [tmp_path / name for name in ['config.json', 'secret.bin', 'ledger.sqlite']]
    config.write_text('{"models":{},"budgets":{}}')
    key.write_bytes(b'x' * 32)
    for name in ['ACM_PROVIDER_KEY_FILE', 'ACM_OPENAI_KEY_FILE', 'ACM_ANTHROPIC_KEY_FILE', 'ACM_OPENAI_ADMIN_KEY_FILE', 'OPENAI_ADMIN_KEY']:
        monkeypatch.delenv(name, raising=False)
    for name, value in {'ACM_CONFIG_FILE': config, 'ACM_SECRET_FILE': key, 'ACM_LEDGER_FILE': ledger,
                        'ACM_CONNECTION_CODE_SHA256': digest}.items():
        monkeypatch.setenv(name, str(value))
    app = create_server()
    assert 'ACM_CONNECTION_CODE_SHA256' not in __import__('os').environ

    async def verify():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test') as client:
            assert (await client.get('/health')).json()['features']['reusable_connection'] == 1
            one = await client.post('/v1/pair', json={'code': code})
            two = await client.post('/v1/pair', json={'code': code})
            assert one.status_code == two.status_code == 200
            assert one.json()['access_token'] != two.json()['access_token']
            assert one.headers['cache-control'] == 'no-store'
            assert (await client.post('/v1/pair', json={'code': digest})).status_code == 401
            assert (await client.get('/v1/costs')).status_code == 401
            assert (await client.get('/v1/costs', headers={'Authorization': 'Bearer ' + code})).status_code == 401
            result = await client.get('/v1/costs', headers={'Authorization': 'Bearer ' + two.json()['access_token']})
            assert result.status_code == 200 and result.json()['history'] == []
            assert (await client.post('/v1/connection-code')).status_code == 404
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.post('/v1/pair', json={'code': code})).status_code == 400
    asyncio.run(verify())
    # 원문 환경 변수 없이 같은 영구 원장으로 재시작해도 복구 연결이 남는다.
    rebooted = create_server()
    async def reconnect():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rebooted), base_url='https://test') as client:
            assert (await client.post('/v1/pair', json={'code': code})).status_code == 200
    asyncio.run(reconnect())
