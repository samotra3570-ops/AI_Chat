import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pytest
from gateway.core import GatewayCore, GatewayError
from gateway.api import create_app
from gateway.provider import OpenAIResponsesProvider


@pytest.fixture
def core(tmp_path):
    now = [datetime(2026, 10, 8, 12, tzinfo=timezone.utc).timestamp()]
    config = {"models": {"test": {"input_price": "1", "cached_input_price": ".1", "output_price": "2",
        "max_input_tokens": 4000, "max_output_tokens": 500, "price_verified_at": now[0], "vision": False}},
        "budgets": {"day_micro_usd": 10000, "month_micro_usd": 20000, "request_micro_usd": 5000}}
    value = GatewayCore(tmp_path / "ledger.sqlite", b"k" * 32, config, lambda: now[0])
    value.test_clock = now
    return value


def payload(request_id="r1"):
    return {"request_id": request_id, "model": "test", "approved_micro_usd": 5000, "room_id": "room-jun", "character_id": "jun",
        "message": {"text": "뭐해"}, "context": {"styleExamples": [{"userExample": "뭐해",
        "characterBubbleSequence": ["집", "왜"], "isHistory": False, "isCanon": False, "isMemory": False, "isEvent": False}]}, "history": []}


def usage():
    return {"input_tokens": 100, "output_tokens": 30, "cached_input_tokens": 20, "reasoning_tokens": 10}


def test_single_use_pairing_and_refresh_reuse_revokes_family(core):
    code = core.pairing_code()
    session = core.pair(code)
    assert core.authenticate(session["access_token"])
    with pytest.raises(GatewayError): core.pair(code)
    rotated = core.refresh(session["refresh_token"])
    with pytest.raises(GatewayError): core.refresh(session["refresh_token"])
    with pytest.raises(GatewayError): core.authenticate(rotated["access_token"])


def test_historical_refresh_can_revoke_rotated_family(core):
    first = core.pair(core.pairing_code())
    rotated = core.refresh(first["refresh_token"])
    core.revoke(first["refresh_token"])
    with pytest.raises(GatewayError): core.authenticate(rotated["access_token"])


def test_expiry_pair_access_refresh(core):
    code = core.pairing_code()
    core.test_clock[0] += 601
    with pytest.raises(GatewayError): core.pair(code)
    session = core.pair(core.pairing_code())
    core.test_clock[0] += 601
    with pytest.raises(GatewayError): core.authenticate(session["access_token"])
    core.test_clock[0] += 604800
    with pytest.raises(GatewayError): core.refresh(session["refresh_token"])


def test_atomic_cap_under_parallel_reservations(core):
    def reserve(i):
        try: return core.reserve(payload(f"r{i}"))[0]
        except GatewayError as error: assert error.code == "hard_cap"; return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(8))) == 2
    assert core.costs()["held"] == 10000


def test_idempotency_and_conflicting_payload(core):
    assert core.reserve(payload())[0]
    assert not core.reserve(payload())[0]
    changed = payload(); changed["message"]["text"] = "다른 내용"
    with pytest.raises(GatewayError, match="idempotency_conflict"): core.reserve(changed)


def test_usage_settles_once_and_does_not_double_count_reasoning(core):
    core.reserve(payload())
    result = core.settle("r1", {"messages": ["응", "왜?", "응\n왜?"]}, usage(), "upstream-1")
    assert result["actual_micro_usd"] == 142
    assert result["messages"] == ["응", "왜?", "응\n왜?"]
    core.settle("r1", {"messages": ["changed"]}, usage(), "upstream-1")
    assert core.costs()["day_spent"] == 142


def test_uncertain_holds_survive_day_and_month_boundary(core):
    core.reserve(payload())
    core.uncertain("r1")
    core.test_clock[0] = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
    core.config["models"]["test"]["price_verified_at"] = core.test_clock[0]
    core.config["budgets"]["day_micro_usd"] = 5000
    with pytest.raises(GatewayError, match="hard_cap"): core.reserve(payload("r2"))
    assert core.lookup("r1")["status"] == "uncertain"
    with pytest.raises(GatewayError, match="billing_evidence_required"):
        core.settle("r1", {"messages": []}, usage(), "upstream")


def test_price_snapshot_survives_model_removed_for_reconciliation(core):
    core.reserve(payload()); core.uncertain("r1")
    core.config["models"].clear()
    result=core.settle("r1", {"messages": ["응"]}, usage(), "provider-verified", "invoice:verified-reference")
    assert result["actual_micro_usd"] == 142
    assert core.costs()["held"] == 0


@pytest.mark.parametrize("age",[-1,604801])
def test_future_or_stale_price_fail_closed(core,age):
    core.config["models"]["test"]["price_verified_at"] = core.clock()-age
    with pytest.raises(GatewayError, match="price_stale"): core.reserve(payload())


@pytest.mark.parametrize("field",["isHistory","isCanon","isMemory","isEvent"])
def test_example_isolation(core,field):
    p=payload();p["context"]["styleExamples"][0][field]=True
    with pytest.raises(GatewayError, match="example_isolation"): core.reserve(p)


@pytest.mark.parametrize("bad",[0,True,-1,1.1])
def test_unconfigured_or_non_integer_caps(core,bad):
    core.config["budgets"]["day_micro_usd"]=bad
    with pytest.raises(GatewayError): core.reserve(payload())


def test_result_encrypted_and_tokens_hash_only(core):
    session=core.pair(core.pairing_code())
    core.reserve(payload());core.settle("r1",{"messages":["private-sensitive-reply"]},usage(),"upstream")
    with core.connection() as db:
        assert session["access_token"] not in json.dumps([dict(r) for r in db.execute("SELECT * FROM sessions")])
        body=db.execute("SELECT result FROM requests").fetchone()[0]
        assert b"private-sensitive-reply" not in body
        row=dict(db.execute("SELECT * FROM requests").fetchone())
        assert "context" not in row and "message" not in row


def test_provider_adapter_preserves_message_items_and_actual_usage():
    captured=[]
    def transport(request):
        body=json.loads(request.content);captured.append(body)
        return httpx.Response(200,json={"id":"upstream-1","status":"completed","usage":{"input_tokens":100,"output_tokens":30,
          "input_tokens_details":{"cached_tokens":20},"output_tokens_details":{"reasoning_tokens":10}},
          "output":[{"type":"message","content":[{"type":"output_text","text":json.dumps({"messages":["응","왜?","응\n왜?"]})}]}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            adapter=OpenAIResponsesProvider("test-key",client)
            result,actual,receipt=await adapter.generate(payload(),{"output_limit":500})
            assert result["messages"] == ["응","왜?","응\n왜?"]
            assert actual==usage() and receipt=="upstream-1"
    asyncio.run(run())
    assert captured[0]["store"] is False and captured[0]["tools"] == []


def test_api_timeout_never_repeats_provider_and_requires_https(core):
    class Provider:
        calls=0
        async def generate(self,*args): self.calls+=1;raise TimeoutError()
    provider=Provider()
    async def run():
        app=create_app(core,provider)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://gateway.test") as client:
            paired=(await client.post('/v1/pair',json={'code':core.pairing_code()})).json()
            headers={'Authorization':'Bearer '+paired['access_token']}
            first=await client.post('/v1/generate',json=payload(),headers=headers)
            assert first.status_code==202 and first.json()['status']=='uncertain'
            repeated=await client.post('/v1/generate',json=payload(),headers=headers)
            assert repeated.json()['status']=='uncertain' and provider.calls==1
            lookup=await client.get('/v1/requests/r1',headers=headers)
            assert lookup.json()['status']=='uncertain'
            assert lookup.headers['cache-control']=='no-store'
            assert (await client.post('/v1/pair',json={'code':'private-user-text','raise_budget':100})).status_code==400
            assert (await client.get('/v1/costs')).status_code==401
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://gateway.test") as client:
            assert (await client.get('/v1/models')).status_code==400
    asyncio.run(run())


def test_cost_confirmation_cannot_be_exceeded(core):
    p=payload();p['approved_micro_usd']=4999
    with pytest.raises(GatewayError,match='cost_changed'):core.reserve(p)
    assert core.costs()['held']==0

def test_invalid_discount_price_fails_before_reservation(core):
    core.config['models']['test']['cached_input_price']='2'
    with pytest.raises(GatewayError,match='invalid_server_price'):core.reserve(payload())
    assert core.costs()['held']==0

def test_provider_over_limit_blocks_future_paid_work(core):
    core.reserve(payload())
    u=usage();u['input_tokens']=4001
    result=core.settle('r1',{'messages':['응']},u,'provider-limit')
    assert result['status']=='billing_limit_violation'
    with pytest.raises(GatewayError,match='billing_limit_violation'):core.reserve(payload('r2'))
