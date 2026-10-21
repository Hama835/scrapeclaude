from __future__ import annotations

import os
import shutil

from ..config import settings
from .base import LLMError, LLMProvider, LLMResult

_provider: LLMProvider | None = None


def resolve_provider_name() -> str:
    name = settings.llm_provider.strip().lower()
    if name == "auto":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic_api"
        return "claude_cli"
    return name


def get_llm() -> LLMProvider:
    global _provider
    if _provider is None:
        name = resolve_provider_name()
        if name == "claude_cli":
            from .claude_cli import ClaudeCLIProvider

            _provider = ClaudeCLIProvider()
        elif name == "anthropic_api":
            from .anthropic_api import AnthropicAPIProvider

            _provider = AnthropicAPIProvider()
        elif name == "fake":  # used by the test suite
            from .fake import FakeProvider

            _provider = FakeProvider()
        else:
            raise LLMError(f"Unknown LLM_PROVIDER '{name}'")
    return _provider


def set_llm(provider: LLMProvider | None) -> None:
    global _provider
    _provider = provider


def provider_status() -> dict:
    name = resolve_provider_name()
    info = {"provider": name, "model": settings.llm_model, "effort": settings.llm_effort}
    if name == "claude_cli":
        info["cli_found"] = bool(shutil.which(settings.claude_cli_path))
        info["auth"] = "CLAUDE_CODE_OAUTH_TOKEN" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "local login"
    elif name == "anthropic_api":
        info["api_key_set"] = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return info


__all__ = ["LLMError", "LLMProvider", "LLMResult", "get_llm", "set_llm", "provider_status"]
