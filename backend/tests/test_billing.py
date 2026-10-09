import asyncio
from datetime import datetime, timezone
import json

import httpx
import pytest

from gateway.api import create_app
from gateway.billing import OpenAIBilling
from test_gateway import core

NOW = int(datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp())
DAY = NOW - 43200


def page(start=None, amount=0, *, more=False, cursor=None):
    rows = [] if start is None else [{'start_time': start, 'end_time': start + 86400,
            'results': [{'object': 'organization.costs.result', 'amount': {'currency': 'usd', 'value': amount}}]}]
    return {'object': 'page', 'data': rows, 'has_more': more, 'next_page': cursor}


def test_missing_admin_key_has_no_balance_no_zero_no_upstream():
    async def verify():
        def unexpected(request):
            pytest.fail('인증 없이는 공급자를 호출하면 안 됨')
        billing = OpenAIBilling(client=httpx.AsyncClient(transport=httpx.MockTransport(unexpected)), clock=lambda: NOW)
        value = await billing.summary()
        assert value['status'] == 'not_configured' and value['balance'] is None
        assert 'day_usd' not in value and 'month_usd' not in value
        await billing.aclose()
    asyncio.run(verify())


def test_decimal_pagination_scoped_project_and_cache():
    async def verify():
        requests = []
        def upstream(request):
            requests.append(request)
            assert str(request.url).startswith('https://api.openai.com/v1/organization/costs?')
            assert request.headers['authorization'] == 'Bearer qa-admin-secret'
            assert request.url.params.get_list('project_ids') == ['proj_test']
            if len(requests) == 1:
                return httpx.Response(200, json=page(DAY - 86400, .1000004, more=True, cursor='page2'))
            assert request.url.params['page'] == 'page2'
            return httpx.Response(200, json=page(DAY, .1999996))
        billing = OpenAIBilling('qa-admin-secret', 'proj_test',
                client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)), clock=lambda: NOW)
        results = await asyncio.gather(billing.summary(), billing.summary(), billing.summary())
        for value in results:
            assert value['day_usd'] == '0.200000' and value['month_usd'] == '0.300000'
            assert value['scope'] == 'project' and value['balance'] is None
            assert 'qa-admin-secret' not in json.dumps(value)
        assert len(requests) == 2
        await billing.aclose()
    asyncio.run(verify())


@pytest.mark.parametrize('kind', ['auth', 'upstream', 'currency', 'invalid_amount', 'partial', 'duplicate'])
def test_bad_or_incomplete_costs_never_become_zero_balance(kind):
    async def verify():
        def upstream(request):
            if kind == 'auth':
                return httpx.Response(401, text='qa-admin-secret')
            if kind == 'upstream':
                return httpx.Response(429, text='qa-admin-secret')
            value = page(DAY, .1)
            if kind == 'currency':
                value['data'][0]['results'][0]['amount']['currency'] = 'eur'
            if kind == 'invalid_amount':
                value['data'][0]['results'][0]['amount']['value'] = 'NaN'
            if kind in ('partial', 'duplicate'):
                value['has_more'] = True
                value['next_page'] = None if kind == 'partial' else 'same'
            return httpx.Response(200, json=value)
        billing = OpenAIBilling('qa-admin-secret', client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)), clock=lambda: NOW)
        value = await billing.summary()
        assert value['status'] == ('unauthorized' if kind == 'auth' else 'unavailable')
        assert value['balance'] is None and 'month_usd' not in value
        assert 'qa-admin-secret' not in json.dumps(value)
        await billing.aclose()
    asyncio.run(verify())


def test_cache_expires_at_utc_day_boundary():
    async def verify():
        clock, calls = [DAY + 86399], []
        def upstream(request):
            calls.append(request)
            return httpx.Response(200, json=page())
        billing = OpenAIBilling('qa-admin-secret', client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)), clock=lambda: clock[0])
        assert (await billing.summary())['month_usd'] == '0.000000'
        clock[0] += 2
        assert (await billing.summary())['checked_at'] == clock[0]
        assert len(calls) == 2
        await billing.aclose()
    asyncio.run(verify())


def test_https_authenticated_read_only_route_and_cleanup(core):
    async def verify():
        billing = OpenAIBilling(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))), clock=lambda: NOW)
        before = core.costs()
        app = create_app(core, billing=billing)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test') as client:
                assert (await client.get('/v1/billing/openai')).status_code == 401
                token = (await client.post('/v1/pair', json={'code': core.pairing_code()})).json()
                headers = {'Authorization': 'Bearer ' + token['access_token']}
                assert (await client.get('http://test/v1/billing/openai', headers=headers)).status_code == 400
                response = await client.get('/v1/billing/openai', headers=headers)
                assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
                assert response.json()['status'] == 'not_configured'
                assert core.costs() == before
                await client.post('/v1/revoke', json={'refresh_token': token['refresh_token']})
                assert (await client.get('/v1/billing/openai', headers=headers)).status_code == 401
        assert billing._http.is_closed
    asyncio.run(verify())
