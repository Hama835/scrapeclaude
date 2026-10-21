"""Deterministic stand-in for Claude, used by tests (LLM_PROVIDER=fake)."""
from __future__ import annotations

import re

from .base import LLMProvider, LLMResult


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def extract(self, content, prompt, schema=None, system=None, model=None) -> LLMResult:
        self.calls.append({"content": content, "prompt": prompt, "schema": schema})
        if schema and "action" in (schema.get("properties") or {}):
            # Agentic step planner: pick the first element whose text appears in the step.
            idx = 0
            for m in re.finditer(r"\[(\d+)\] <(\w+)[^>\n]*>[ \t]*([^\n]*)", content):
                if m.group(3).strip() and m.group(3).strip().lower() in prompt.lower():
                    idx = int(m.group(1))
                    break
            data = {"action": "click", "index": idx, "value": None, "reason": "fake"}
        else:
            headings = re.findall(r"^#+\s+(.+)$", content, re.MULTILINE)
            data = {"headings": headings, "chars": len(content)}
        return LLMResult(data=data, provider=self.name, model="fake", input_tokens=len(content) // 4)
