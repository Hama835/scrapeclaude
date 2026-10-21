"""Claude via the official Claude Code CLI, authenticated with your own Claude subscription.

Auth on a server: run `claude setup-token` on a machine with a browser, then put the token in
CLAUDE_CODE_OAUTH_TOKEN on the server. ANTHROPIC_API_KEY is removed from the child process
environment so the CLI never silently switches to API billing.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from ..config import settings
from .base import DEFAULT_SYSTEM, LLMError, LLMProvider, LLMResult, build_user_message, parse_json_loose


class ClaudeCLIProvider(LLMProvider):
    name = "claude_cli"

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(settings.llm_max_concurrency)
        # Empty working dir so the CLI doesn't pick up any project CLAUDE.md or settings.
        self._workdir = tempfile.mkdtemp(prefix="scrapeclaude-cli-")

    def _env(self) -> dict:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        env.setdefault("DISABLE_AUTOUPDATER", "1")
        env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        return env

    async def extract(self, content, prompt, schema=None, system=None, model=None) -> LLMResult:
        model = model or settings.llm_model
        cmd = [
            settings.claude_cli_path,
            "-p",
            "Follow the <instruction> using the page content provided on stdin.",
            "--output-format", "json",
            "--model", model,
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--max-turns", "4",
            "--system-prompt", system or DEFAULT_SYSTEM,
        ]
        if settings.llm_effort:
            cmd += ["--effort", settings.llm_effort]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema)]
        stdin = build_user_message(content, prompt, schema).encode()

        async with self._sem:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self._workdir,
                    env=self._env(),
                )
            except FileNotFoundError as e:
                raise LLMError(
                    f"Claude CLI not found at '{settings.claude_cli_path}'. "
                    "Install it with: npm install -g @anthropic-ai/claude-code"
                ) from e
            try:
                out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=settings.llm_timeout_s)
            except asyncio.TimeoutError as e:
                proc.kill()
                raise LLMError(f"Claude CLI timed out after {settings.llm_timeout_s}s") from e

        try:
            payload = json.loads(out.decode() or "{}")
        except json.JSONDecodeError as e:
            raise LLMError(f"Claude CLI returned non-JSON output: {out[:300]!r} {err[:300]!r}") from e

        if proc.returncode != 0 or payload.get("is_error"):
            msg = payload.get("result") or err.decode()[:500] or f"exit code {proc.returncode}"
            raise LLMError(f"Claude CLI error: {msg}")

        if schema is not None:
            if "structured_output" not in payload:
                raise LLMError("Claude CLI finished without output matching the schema.")
            data = payload["structured_output"]
        else:
            data = parse_json_loose(payload.get("result", ""))

        usage = payload.get("usage") or {}
        return LLMResult(
            data=data,
            provider=self.name,
            model=model,
            input_tokens=int(usage.get("input_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cost_usd=payload.get("total_cost_usd"),
            raw_text=payload.get("result", ""),
            extra={"note": "Billed to your Claude subscription limits, not per token."},
        )
