"""Deployment entry point regression; no network or paid provider calls."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from gateway.core import GatewayCore
from gateway.server import create_server


def test_default_factory_pairs_but_cannot_generate(tmp_path, monkeypatch):
    config = json.loads((Path(__file__).parents[1] / 'config.example.json').read_text())
    config_file, secret_file, ledger = (tmp_path / n for n in ('config.json', 'secret.bin', 'ledger.sqlite'))
    config_file.write_text(json.dumps(config))
    secret_file.write_bytes(b'x' * 32)
    secret_file.chmod(0o600)
    for key, value in {'ACM_CONFIG_FILE': config_file, 'ACM_SECRET_FILE': secret_file, 'ACM_LEDGER_FILE': ledger}.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv('ACM_PROVIDER_KEY_FILE', raising=False)
    core = GatewayCore(ledger, secret_file.read_bytes(), config)
    code = core.pairing_code()
    app = create_server()

    async def verify():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test') as client:
            paired = await client.post('/v1/pair', json={'code': code})
            assert paired.status_code == 200
            headers = {'Authorization': 'Bearer ' + paired.json()['access_token']}
            models = await client.get('/v1/models', headers=headers)
            assert models.json() == {'models': []}
            result = await client.post('/v1/generate', headers=headers, json={'approved_micro_usd': 100})
            assert result.status_code == 503
            assert result.json() == {'error': 'provider_unconfigured'}
            assert result.headers['cache-control'] == 'no-store'
            costs = (await client.get('/v1/costs', headers=headers)).json()
            assert costs['day_spent'] == 0 and costs['month_spent'] == 0 and costs['held'] == 0
    asyncio.run(verify())
    assert core.costs()['history'] == []


def test_factory_rejects_missing_secret_without_fallback(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text('{"models":{},"budgets":{}}')
    monkeypatch.setenv('ACM_CONFIG_FILE', str(config))
    monkeypatch.setenv('ACM_SECRET_FILE', str(tmp_path / 'missing-secret'))
    monkeypatch.setenv('ACM_LEDGER_FILE', str(tmp_path / 'ledger.sqlite'))
    with pytest.raises(FileNotFoundError):
        create_server()
    assert not (tmp_path / 'ledger.sqlite').exists()
