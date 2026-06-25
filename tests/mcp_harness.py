"""Shared MCP stdio client harness for the end-to-end test tiers.

Two tiers drive the server *through the running MCP transport* rather than by
calling the tool coroutines directly: the always-on, credential-free E2E tier
(:mod:`tests.test_server_stdio`, backed by :class:`~tests.fakes.FakeGCSClient`)
and the live integration suite (:mod:`tests.test_live_integration`, against a
real bucket). Both spawn the server as a stdio subprocess exactly the way
``.mcp.json`` launches it (``uv run`` + an MCP :class:`ClientSession`), so the
session setup and the structured-result parsing live here once and the two tiers
cannot drift. This is the durable, CI-able form of the ``CLAUDE.md`` tmux
live-test loop.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@asynccontextmanager
async def mcp_session(params: StdioServerParameters) -> AsyncIterator[ClientSession]:
    """Spawn the server described by *params* over stdio and yield a live session.

    The subprocess is launched, an MCP :class:`ClientSession` is initialized over
    its stdio pipes, and the session is yielded for the duration of the ``async
    with`` block; ``stdio_client`` tears the child process down on exit.
    """
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def parse_tool_result(result: Any) -> dict[str, Any]:
    """Extract a tool's returned ``dict`` from an MCP ``CallToolResult``.

    FastMCP serializes a ``dict`` return into both ``structuredContent`` and a
    JSON ``TextContent`` block; some versions nest a non-model dict under a
    ``result`` key. Prefer the structured payload (unwrapping that nesting) and
    fall back to parsing the text block.
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            parsed: dict[str, Any] = json.loads(text)
            return parsed
    raise AssertionError(f"no parseable dict in tool result: {result!r}")


async def call_tool(session: ClientSession, name: str, **arguments: Any) -> dict[str, Any]:
    """Call a tool by name over the session and return its parsed ``dict`` result."""
    return parse_tool_result(await session.call_tool(name, arguments))
