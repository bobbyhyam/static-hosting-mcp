"""Credential-free stdio entry point: the real MCP server, GCS leaf faked.

Spawned as a subprocess by :mod:`tests.test_server_stdio` exactly the way
``.mcp.json`` launches the real server (``uv run`` + the stdio transport + an MCP
:class:`~mcp.ClientSession`), but with the network-touching
:class:`~static_hosting_mcp.gcs_client.GCSClient` the lifespan constructs replaced
by the in-memory :class:`~tests.fakes.FakeGCSClient`. That single seam keeps
everything else real — the lifespan, all six registered tools, FastMCP argument
parsing, ``Context`` injection, and structured-result shaping run unchanged — so
the end-to-end tool surface is exercised over the genuine MCP transport with no
credentials and no bucket. It is the durable, CI-able form of the ``CLAUDE.md``
tmux live-test loop.

The process speaks the MCP protocol on stdout, so this module must never print
there; ``uv``'s own progress output goes to stderr. Run it via
``python -m tests.stdio_fake_server`` from the repo root (what the test does).
"""

from __future__ import annotations

from unittest.mock import patch

import static_hosting_mcp.server as server
from static_hosting_mcp.gcs_client import GCSClientProtocol
from tests.fakes import FakeGCSClient


def _fake_gcs_client(
    bucket: str,
    *,
    key_path: str | None = None,
    project: str | None = None,
    client: object | None = None,
) -> GCSClientProtocol:
    """Drop-in for ``GCSClient(...)`` that ignores the credential args.

    The lifespan calls ``GCSClient(config.bucket, key_path=..., project=...)``;
    this mirrors that signature so the swap is transparent, and returns an
    in-memory fake bound to the same bucket name (so returned URLs stay
    deterministic) with no credential ever read.
    """
    return FakeGCSClient(bucket)


def main() -> None:
    """Swap in the fake GCS client, then run the real server over stdio."""
    # The lifespan resolves ``GCSClient`` from this module's globals at call time,
    # so patching it here (for the whole ``mcp.run()`` lifetime) makes every
    # connection build a fake-backed AppContext. ``patch.object`` keeps the rebind
    # a deliberate, self-restoring test seam rather than a typed reassignment to a
    # class symbol.
    with patch.object(server, "GCSClient", _fake_gcs_client):
        server.mcp.run()


if __name__ == "__main__":
    main()
