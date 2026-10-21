"""Claude via the Anthropic API (pay per token with ANTHROPIC_API_KEY)."""
from __future__ import annotations

import asyncio
import json

import anthropic

from ..config import settings
from .base import DEFAULT_SYSTEM, LLMError, LLMProvider, LLMResult, build_user_message, parse_json_loose

# USD per 1M tokens (input, output) for cost estimates shown in the dashboard.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicAPIProvider(LLMProvider):
    name = "anthropic_api"

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(timeout=settings.llm_timeout_s, max_retries=2)
        self._sem = asyncio.Semaphore(settings.llm_max_concurrency)

    async def _call(self, model: str, system: str, user: str, schema: dict | None):
        output_config: dict = {}
        if settings.llm_effort:
            output_config["effort"] = settings.llm_effort
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        kwargs = dict(
            model=model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
        # Server-side refusal fallback (re-runs on another model if a safety classifier declines).
        if model.startswith(("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")):
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return await self._client.beta.messages.create(**kwargs)

    async def extract(self, content, prompt, schema=None, system=None, model=None) -> LLMResult:
        model = model or settings.llm_model
        system = system or DEFAULT_SYSTEM
        async with self._sem:
            try:
                try:
                    resp = await self._call(model, system, build_user_message(content, prompt, schema), schema)
                except anthropic.BadRequestError as e:
                    # Structured outputs need a strict schema (additionalProperties: false, etc.).
                    # If the user's schema isn't accepted, fall back to prompt-guided JSON.
                    if schema is None or "schema" not in str(e).lower():
                        raise
                    guided = prompt + "\n\nThe JSON must follow this JSON Schema:\n" + json.dumps(schema, indent=2)
                    resp = await self._call(model, system, build_user_message(content, guided, None), None)
            except anthropic.RateLimitError as e:
                raise LLMError("Anthropic API rate limit reached, try again shortly.") from e
            except anthropic.APIStatusError as e:
                raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise LLMError(f"Could not reach the Anthropic API: {e}") from e

        if resp.stop_reason == "refusal":
            raise LLMError("Claude declined to process this content.")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Output was too long and got cut off. Narrow the prompt or schema.")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = parse_json_loose(text)

        in_tok = (resp.usage.input_tokens or 0) + (getattr(resp.usage, "cache_read_input_tokens", 0) or 0)
        out_tok = resp.usage.output_tokens or 0
        price = PRICES.get(resp.model) or PRICES.get(model)
        cost = round(in_tok / 1e6 * price[0] + out_tok / 1e6 * price[1], 6) if price else None
        return LLMResult(
            data=data, provider=self.name, model=resp.model or model,
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost, raw_text=text,
        )
