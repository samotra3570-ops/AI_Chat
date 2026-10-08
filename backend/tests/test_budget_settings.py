import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from gateway.api import create_app
from gateway.core import GatewayCore, GatewayError
from test_gateway import core, payload, usage


def caps(request=5000, day=10000, month=20000):
    return {'request_micro_usd': request, 'day_micro_usd': day, 'month_micro_usd': month}


def test_settings_survive_restart_and_bootstrap_config_changes(core):
    saved = core.update_settings({'budgets': caps(6000, 12000, 30000), 'expected_revision': 0}, 'owner')
    restarted = GatewayCore(core.path, core.key, {'models': {}, 'budgets': caps(0, 0, 0)}, core.clock)
    assert restarted.settings() == saved
    assert restarted.costs()['budgets'] == saved['budgets']
    assert saved['revision'] == 1


def test_lowering_caps_keeps_uncertain_holds_and_spent_history(core):
    core.reserve(payload())
    core.settle('r1', {'messages': ['ok']}, usage(), 'upstream')
    core.reserve(payload('r2'))
    core.uncertain('r2')
    before = core.costs()
    core.update_settings({'budgets': caps(5000, 5000, 10000), 'expected_revision': 0}, 'owner')
    with pytest.raises(GatewayError, match='hard_cap'):
        core.reserve(payload('r3'))
    after = core.costs()
    for key in ['held', 'day_spent', 'month_spent', 'history']:
        assert after[key] == before[key]


def test_settings_revision_serializes_concurrent_devices(core):
    def save(value):
        try:
            return core.update_settings({'budgets': caps(value), 'expected_revision': 0}, str(value))
        except GatewayError as error:
            assert error.code == 'settings_conflict' and error.status == 409
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [5000, 6000]))
    winner = [result for result in results if result]
    assert len(winner) == 1 and core.settings() == winner[0]


@pytest.mark.parametrize('bad', [True, -1, 0.1, '0.1', 1000000000001, None])
def test_invalid_budget_never_changes_settings(core, bad):
    budgets = caps()
    budgets['day_micro_usd'] = bad
    with pytest.raises(GatewayError, match='invalid_settings'):
        core.update_settings({'budgets': budgets, 'expected_revision': 0}, 'owner')
    assert core.settings() == {'revision': 0, 'budgets': caps()}


def test_zero_blocks_generation_and_order_rejection_is_atomic(core):
    with pytest.raises(GatewayError, match='budget_order'):
        core.update_settings({'budgets': caps(10001, 10000, 20000), 'expected_revision': 0}, 'owner')
    assert core.settings()['revision'] == 0
    core.update_settings({'budgets': caps(0, 0, 0), 'expected_revision': 0}, 'owner')
    with pytest.raises(GatewayError, match='budget_unconfigured'):
        core.reserve(payload())
    assert core.costs()['history'] == []


def test_settings_routes_require_https_and_owner_session(core):
    app = create_app(core)
    async def verify():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test') as client:
            assert (await client.get('/v1/settings')).status_code == 401
            assert (await client.post('/v1/settings', json={})).status_code == 401
            paired = (await client.post('/v1/pair', json={'code': core.pairing_code()})).json()
            headers = {'Authorization': 'Bearer ' + paired['access_token']}
            assert (await client.get('http://test/v1/settings', headers=headers)).status_code == 400
            assert (await client.get('/v1/settings', headers=headers)).json()['revision'] == 0
            invalid = await client.post('/v1/settings', headers=headers, json={'models': {}, 'expected_revision': 0, 'budgets': caps()})
            assert invalid.status_code == 400  # No model prices or keys supplied by the app.
            saved = await client.post('/v1/settings', headers=headers, json={'expected_revision': 0, 'budgets': caps(1, 2, 3)})
            assert saved.status_code == 200 and saved.headers['cache-control'] == 'no-store'
            assert (await client.get('/v1/costs', headers=headers)).json()['budgets'] == caps(1, 2, 3)
            conflict = await client.post('/v1/settings', headers=headers, json={'expected_revision': 0, 'budgets': caps()})
            assert conflict.status_code == 409
            await client.post('/v1/revoke', json={'refresh_token': paired['refresh_token']})
            assert (await client.get('/v1/settings', headers=headers)).status_code == 401
    asyncio.run(verify())
