"""FastMCP server for static-hosting-mcp.

U2 replaces U1's placeholder lifespan with the real one: it loads :class:`Config`
from the environment, constructs the U3 :class:`GCSClient`, runs the startup
reachability probe, and yields an :class:`AppContext` that owns the client and
config for the process lifetime. Tools (U5/U6) read that context through
:func:`_ctx`; credentials and the key path stay in the lifespan and never reach
the tool surface (R11, KTD8).

Following the ``ultimate-brain-mcp`` reference shape: an ``@asynccontextmanager``
``app_lifespan`` yielding a frozen-ish ``AppContext`` dataclass, and a small
``_ctx`` accessor the tool layer uses to reach it.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from inspect import isawaitable

from mcp.server.fastmcp import Context, FastMCP

from .config import Config
from .gcs_client import GCSClient, GCSClientProtocol, StartupError


@dataclass
class AppContext:
    """Process-lifetime state owned by the lifespan and shared with every tool.

    ``client`` is typed as :class:`GCSClientProtocol` (not the concrete
    :class:`GCSClient`) so ``mypy`` verifies that whatever the lifespan builds —
    the real client in production or an injected fake in tests — conforms to the
    async method surface the tools call. Populated once before ``yield`` and
    read-only thereafter, so no locking is required.
    """

    client: GCSClientProtocol
    config: Config


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Build config + GCS client, reachability-check, and yield the AppContext.

    A :class:`StartupError` from the reachability probe (U3) is converted into a
    clean, actionable stderr line — the message already names the bucket — plus a
    non-zero exit, mirroring ``main()``'s missing-env fail-fast. This keeps the
    operator from seeing a raw anyio/``mcp.run()`` traceback when the bucket or
    credentials are wrong (R12 wiring). On normal shutdown the ``finally`` tears
    the client down best-effort.
    """
    config = Config.from_env()
    client: GCSClientProtocol = GCSClient(
        config.bucket, key_path=config.key_path, project=config.project
    )
    try:
        await client.check_reachable()
    except StartupError as exc:
        # Mirror main()'s missing-env pattern: one actionable line, clean exit.
        print(f"[static-hosting-mcp] {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        yield AppContext(client=client, config=config)
    finally:
        await _close_client(client)


async def _close_client(client: GCSClientProtocol) -> None:
    """Best-effort teardown of the GCS client at lifespan shutdown.

    :class:`GCSClientProtocol` declares no teardown method, and a stdio server's
    lifespan spans the whole process — so the underlying SDK connection pool is
    reclaimed at exit anyway. We still honor a ``close``/``aclose`` the concrete
    client may expose (sync or async), rather than reaching into another unit's
    private SDK handle, so the seam stays correct if U3 grows one later.
    """
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if closer is None:
        return
    result = closer()
    if isawaitable(result):
        await result


mcp = FastMCP("static-hosting-mcp", lifespan=app_lifespan)


def _ctx(ctx: Context | None) -> AppContext:
    """Return the lifespan :class:`AppContext` for a tool invocation.

    FastMCP injects ``ctx`` at call time, so it is never ``None`` in practice;
    the ``Context | None`` annotation only reflects the ``ctx: Context = None``
    sentinel default U5/U6 use on tool signatures (the type-based-injection seam
    the reference relies on).
    """
    assert ctx is not None
    return ctx.request_context.lifespan_context
