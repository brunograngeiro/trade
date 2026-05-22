"""Small provider adapter for Analyst conversations."""

from __future__ import annotations

import httpx

from app.config import Settings


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def chat(self, provider: str, messages: list[dict], max_tokens: int = 900) -> dict:
        provider = provider.lower()
        if provider == "openai":
            return await self._chat_openai(messages, max_tokens=max_tokens)
        if provider in {"xai", "grok"}:
            return await self._chat_openai_compatible(
                base_url="https://api.x.ai/v1",
                api_key=self.settings.xai_api_key,
                model="grok-4.20-0309-non-reasoning",
                messages=messages,
                max_tokens=max_tokens,
            )
        if provider == "deepseek":
            return await self._chat_openai_compatible(
                base_url="https://api.deepseek.com",
                api_key=self.settings.deepseek_api_key,
                model="deepseek-chat",
                messages=messages,
                max_tokens=max_tokens,
            )
        raise LLMError(f"unsupported_provider:{provider}")

    async def _chat_openai(self, messages: list[dict], max_tokens: int) -> dict:
        return await self._chat_openai_compatible(
            base_url="https://api.openai.com/v1",
            api_key=self.settings.openai_api_key,
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=max_tokens,
        )

    async def _chat_openai_compatible(self, *, base_url: str, api_key: str, model: str,
                                      messages: list[dict], max_tokens: int) -> dict:
        if not api_key:
            raise LLMError("api_key_missing")
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "max_tokens": max_tokens},
            )
        if resp.status_code >= 400:
            raise LLMError(f"provider_error:{resp.status_code}:{resp.text[:240]}")
        payload = resp.json()
        content = payload.get("choices", [{}])[0].get("message", {}).get("content") or ""
        return {"provider": base_url, "model": model, "content": content, "raw": payload}
