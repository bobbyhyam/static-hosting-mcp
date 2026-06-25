"""FastMCP server for static-hosting-mcp.

U1 scaffolding: a discoverable stdio MCP server with zero tools and a
placeholder lifespan. U2 replaces the placeholder with the real config + GCS
client lifespan (``AppContext`` + a ``_ctx`` helper); U5/U6 register the six
artifact tools. Keeping the seam here means later units extend this module
rather than restructure it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[None]:
    """Placeholder lifespan.

    U2 constructs the GCS client + config here, runs the startup reachability
    check, and yields an ``AppContext``. For U1 the server simply starts with
    zero tools so ``uv run static-hosting-mcp`` is a discoverable, cleanly
    starting and stopping stdio server.
    """
    yield None


mcp = FastMCP("static-hosting-mcp", lifespan=app_lifespan)
