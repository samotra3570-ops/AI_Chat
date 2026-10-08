from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from .core import GatewayCore, GatewayError


def create_app(core: GatewayCore, provider=None):
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if provider is not None and hasattr(provider, 'aclose'):
                await provider.aclose()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(GatewayError)
    async def safe_error(request, error):
        return JSONResponse({"error": error.code}, status_code=error.status, headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.scheme != "https" and not (request.method == 'GET' and request.url.path == '/health'):
            return JSONResponse({"error": "https_required"}, status_code=400)
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 2 * 1024 * 1024:
                return JSONResponse({"error": "body_too_large"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def body(request, fields):
        try:
            value = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise GatewayError("invalid_payload")
        if not isinstance(value, dict) or set(value) != set(fields) or any(not isinstance(value[k], str) or not 1 <= len(value[k]) <= 256 for k in fields):
            raise GatewayError("invalid_payload")
        return value

    def auth(request):
        value = request.headers.get("authorization", "")
        if not value.startswith("Bearer ") or not 1 <= len(value[7:]) <= 256:
            raise GatewayError("unauthorized", 401)
        return core.authenticate(value[7:])

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    @app.post("/v1/pair")
    async def pair(request: Request):
        value = await body(request, ["code"])
        subject = request.client.host if request.client else "unknown"
        return await asyncio.to_thread(core.pair, value["code"], subject)

    @app.post("/v1/refresh")
    async def refresh(request: Request):
        value = await body(request, ["refresh_token"])
        return await asyncio.to_thread(core.refresh, value["refresh_token"])

    @app.post("/v1/revoke")
    async def revoke(request: Request):
        value = await body(request, ["refresh_token"])
        await asyncio.to_thread(core.revoke, value["refresh_token"])
        return {"revoked": True}

    @app.get("/v1/models")
    async def models(request: Request):
        auth(request)
        available = []
        for name, cfg in core.config.get('models', {}).items():
            if provider is None or (hasattr(provider, 'available') and not provider.available(name)):
                continue
            try:
                price = core._model(name, core.clock())
                reserved = core.charge(price, price['max_input_tokens'], price['max_output_tokens'])
                current = True
            except GatewayError:
                reserved, current = None, False
            available.append({'id': name, 'label': cfg.get('label', name),
                'provider': cfg.get('provider', 'openai'), 'vision': cfg.get('vision') is True,
                'max_input_tokens': cfg.get('max_input_tokens'), 'max_output_tokens': cfg.get('max_output_tokens'),
                'price_current': current, 'reserved_micro_usd': reserved,
                'input_price': cfg.get('input_price'), 'output_price': cfg.get('output_price')})
        return {'models': available}

    @app.get('/v1/settings')
    async def settings(request: Request):
        auth(request)
        return await asyncio.to_thread(core.settings)

    @app.post('/v1/settings')
    async def update_settings(request: Request):
        family = auth(request)
        try:
            payload = await request.json()
        except ValueError:
            raise GatewayError('invalid_settings')
        return await asyncio.to_thread(core.update_settings, payload, family)

    @app.get("/v1/costs")
    async def costs(request: Request):
        auth(request)
        return await asyncio.to_thread(core.costs)

    @app.post("/v1/quote")
    async def quote(request: Request):
        auth(request)
        try:
            payload = await request.json()
        except ValueError:
            raise GatewayError("invalid_payload")
        core.validate(payload)
        if provider is not None and hasattr(provider, 'ensure_available'):
            provider.ensure_available(payload['model'])
        return await asyncio.to_thread(core.quote, payload)

    @app.get("/v1/requests/{request_id}")
    async def lookup(request_id: str, request: Request):
        auth(request)
        return await asyncio.to_thread(core.lookup, request_id)

    @app.post("/v1/generate")
    async def generate(request: Request):
        family = auth(request)
        if provider is None:
            raise GatewayError("provider_unconfigured", 503)
        try:
            payload = await request.json()
        except ValueError:
            raise GatewayError("invalid_payload")
        if not isinstance(payload, dict) or type(payload.get("approved_micro_usd")) is not int:
            raise GatewayError("cost_confirmation_required")
        core.validate(payload)
        if hasattr(provider, 'ensure_available'):
            provider.ensure_available(payload['model'])
        started, receipt = await asyncio.to_thread(core.reserve, payload, family)
        if not started:
            return receipt
        try:
            result, usage, upstream_id = await provider.generate(payload, receipt)
            return await asyncio.to_thread(core.settle, payload["request_id"], result, usage, upstream_id)
        except Exception:
            await asyncio.to_thread(core.uncertain, payload["request_id"])
            return JSONResponse({"request_id": payload["request_id"], "status": "uncertain"}, status_code=202)

    return app
