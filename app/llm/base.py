from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: Any
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None  # API: estimated from usage. CLI: Claude Code's own estimate.
    raw_text: str = ""
    extra: dict = field(default_factory=dict)

    def usage(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd_estimate": self.cost_usd,
        }


DEFAULT_SYSTEM = (
    "You are a precise web data extraction engine. You receive the content of one or more web "
    "pages (as Markdown) and an instruction. Answer the instruction using only information "
    "present in the content. Never invent values: use null when something is not present. "
    "Keep URLs absolute exactly as they appear. Respond with JSON only."
)


def build_user_message(content: str, prompt: str, schema: dict | None) -> str:
    parts = [
        "<instruction>",
        prompt.strip(),
        "</instruction>",
    ]
    if schema is None:
        parts += [
            "",
            "Return a single JSON value (usually an object) that answers the instruction. "
            "Choose clear snake_case keys. Output JSON only, no prose, no code fences.",
        ]
    parts += ["", "<content>", content, "</content>"]
    return "\n".join(parts)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_loose(text: str) -> Any:
    """Parse JSON from a model reply that may include code fences or stray prose."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    stripped = _FENCE.sub("", text).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Last resort: first {...} or [...] block.
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = stripped.find(open_c), stripped.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"Model did not return valid JSON: {text[:300]}")


class LLMProvider:
    name = "base"

    async def extract(
        self,
        content: str,
        prompt: str,
        schema: dict | None = None,
        system: str | None = None,
        model: str | None = None,
    ) -> LLMResult:
        raise NotImplementedError
