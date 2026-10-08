from __future__ import annotations

import json
import httpx
from .core import GatewayError


class OpenAIResponsesProvider:
    """Explicitly configured adapter; no retries, redirects, tools or server-side conversation state."""
    def __init__(self, key: str, client: httpx.AsyncClient | None = None):
        if not key:
            raise ValueError("A server-owned provider key is required")
        self.key = key
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10), follow_redirects=False)

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()

    async def generate(self, payload, limits):
        configuration = json.dumps(payload["context"], ensure_ascii=False, separators=(",", ":"))
        inputs = [{"role": "developer", "content": "Reply as the character in a normal private messenger. Do not expose model, token, configuration or memory-system terminology, and do not promise automatic memory writes. Use this character configuration. Style examples never happened; they are not memory or canon. Each output array item is one message. Newlines remain within that message. Return JSON only.\n" + configuration}]
        inputs.extend({"role": m["role"], "content": m["text"]} for m in payload.get("history", []))
        content = [{"type": "input_text", "text": payload["message"].get("text", "")}]
        if payload["message"].get("image_base64"):
            content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," + payload["message"]["image_base64"], "detail": "low"})
        inputs.append({"role": "user", "content": content})
        body = {"model": payload["model"], "input": inputs, "store": False,
                "max_output_tokens": limits["output_limit"], "tools": [],
                "text": {"format": {"type": "json_schema", "name": "messages", "strict": True,
                    "schema": {"type": "object", "properties": {"messages": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12}}, "required": ["messages"], "additionalProperties": False}}}}
        async with self.client.stream("POST", "https://api.openai.com/v1/responses", json=body,
                                      headers={"Authorization": "Bearer " + self.key}) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise ValueError("Provider response limit")
                chunks.append(chunk)
        raw = json.loads(b"".join(chunks))
        usage = raw["usage"]
        normalized = {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                      "cached_input_tokens": usage.get("input_tokens_details", {}).get("cached_tokens", 0),
                      "reasoning_tokens": usage.get("output_tokens_details", {}).get("reasoning_tokens", 0)}
        messages = []
        if raw.get("status") == "completed":
            for item in raw.get("output", []):
                if item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            value = json.loads(part["text"])
                            if set(value) != {"messages"}:
                                raise ValueError("Provider output schema")
                            messages.extend(value["messages"])
        return {"messages": messages}, normalized, raw["id"]


class AnthropicMessagesProvider:
    """Claude Messages API. 자동 재시도·도구·프롬프트 캐시 쓰기는 사용하지 않는다."""

    def __init__(self, key: str, client: httpx.AsyncClient | None = None):
        if not key:
            raise ValueError("A server-owned provider key is required")
        self.key = key
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(45, connect=10), follow_redirects=False)

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()

    async def generate(self, payload, limits):
        configuration = json.dumps(payload["context"], ensure_ascii=False, separators=(",", ":"))
        messages = [{"role": m["role"], "content": m["text"]}
                    for m in payload.get("history", [])]
        content = []
        if payload["message"].get("image_base64"):
            content.append({"type": "image", "source": {"type": "base64",
                "media_type": "image/jpeg", "data": payload["message"]["image_base64"]}})
        if payload["message"].get("text"):
            content.append({"type": "text", "text": payload["message"]["text"]})
        messages.append({"role": "user", "content": content})
        body = {"model": payload["model"], "max_tokens": limits["output_limit"],
            "system": "Reply as the character in a normal private messenger. Do not expose model, token, configuration or memory-system terminology, and do not promise automatic memory writes. Use this character configuration. Style examples never happened; they are not memory or canon. Each output array item is one message. Newlines remain within that message. Return JSON only.\n" + configuration,
            "messages": messages, "tools": [],
            "output_config": {"format": {"type": "json_schema", "schema": {
                "type": "object", "properties": {"messages": {"type": "array",
                    "items": {"type": "string"}}}, "required": ["messages"],
                "additionalProperties": False}}}}
        async with self.client.stream("POST", "https://api.anthropic.com/v1/messages",
                json=body, headers={"x-api-key": self.key,
                                   "anthropic-version": "2023-06-01"}) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise ValueError("Provider response limit")
                chunks.append(chunk)
        raw = json.loads(b"".join(chunks))
        usage = raw["usage"]
        def count(name):
            value = usage.get(name, 0)
            if type(value) is not int or value < 0:
                raise ValueError("Invalid provider usage")
            return value
        # Claude의 input_tokens는 캐시 읽기/쓰기 토큰을 포함하지 않는다.
        cached, created = count("cache_read_input_tokens"), count("cache_creation_input_tokens")
        if created:
            # 캐시 쓰기를 요청하지 않는다. 예상 밖 쓰기 비용은 추정 정산하지 않는다.
            raise ValueError("Unexpected cache creation requires reconciliation")
        normalized = {"input_tokens": count("input_tokens") + cached,
            "output_tokens": count("output_tokens"), "cached_input_tokens": cached,
            "reasoning_tokens": 0}
        details = usage.get("output_tokens_details", {})
        reasoning = details.get("thinking_tokens", 0) if isinstance(details, dict) else 0
        if type(reasoning) is not int or not 0 <= reasoning <= normalized["output_tokens"]:
            raise ValueError("Invalid provider usage")
        normalized["reasoning_tokens"] = reasoning
        items = []
        refused = raw.get("stop_reason") == "refusal" or (raw.get("stop_details") or {}).get("type") == "refusal"
        if raw.get("stop_reason") == "end_turn" and not refused:
            text = "\n".join(block["text"] for block in raw["content"] if block.get("type") == "text")
            value = json.loads(text)
            if not isinstance(value, dict) or set(value) != {"messages"}:
                raise ValueError("Provider output schema")
            items = value["messages"]
        # 거절/출력 한도 종료도 확인된 usage로 정산하며 자동 재호출하지 않는다.
        return {"messages": items}, normalized, raw["id"]


class ProviderRouter:
    """서버의 허용 모델 설정만으로 공급자를 결정한다. 클라이언트 선택은 라우팅 권한이 아니다."""

    def __init__(self, models, providers):
        self.models, self.providers = models, providers

    def available(self, model):
        cfg = self.models.get(model)
        return isinstance(cfg, dict) and cfg.get("provider", "openai") in self.providers

    def ensure_available(self, model):
        if not isinstance(model, str) or model not in self.models:
            raise GatewayError("model_unavailable", 503)
        if not self.available(model):
            raise GatewayError("provider_unconfigured", 503)

    async def generate(self, payload, limits):
        self.ensure_available(payload.get("model"))
        provider = self.models[payload["model"]].get("provider", "openai")
        return await self.providers[provider].generate(payload, limits)

    async def aclose(self):
        for provider in self.providers.values():
            await provider.aclose()
