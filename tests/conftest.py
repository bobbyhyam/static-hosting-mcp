"""Shared pytest fixtures and the two-tier test harness (U7).

This module wires the two test tiers the plan defines (R18):

- **Unit tier (default).** Credential-free fixtures backed by
  :class:`~tests.fakes.FakeGCSClient`: a ``fake_client``, the lifespan
  :class:`~static_hosting_mcp.server.AppContext` wired to it (``app_context``),
  and a FastMCP ``Context`` stand-in (``tool_context`` / ``make_tool_ctx``) so a
  tool can be called directly without the MCP transport or any credentials. The
  six tools only ever reach ``ctx.request_context.lifespan_context``, so a tiny
  duck-typed namespace is a faithful stand-in.
- **Live tier (``-m live``).** ``live_config`` reads the operator's real
  configuration from the environment and **skips** (never fails) when it is
  incomplete, so ``pytest -m live`` is a clean no-op without a ``.env``.

``.env`` is loaded at import so the live tier picks up ``GCS_BUCKET`` /
``GOOGLE_APPLICATION_CREDENTIALS`` / ``GCS_PROJECT_ID`` without the operator
exporting them by hand. The unit tier never reads credentials, so the load is a
harmless no-op there.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

# Load .env once at collection time so the live tier sees the operator's config.
# python-dotenv is a declared dev dependency; guard the import so a stripped-down
# environment still collects the (skipping) live tier instead of erroring.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dev dependency is normally present
    pass

from static_hosting_mcp.config import ENV_BUCKET, ENV_CREDENTIALS, Config
from static_hosting_mcp.server import AppContext

from .fakes import FakeGCSClient

# Default bucket name for the unit tier — not a real bucket, never contacted.
_UNIT_BUCKET = "my-bucket"
# A non-secret absolute placeholder; the unit tier never reads the key file.
_UNIT_KEY_PATH = "/abs/key.json"


def _make_app_context(client: FakeGCSClient) -> AppContext:
    """Build the lifespan AppContext a tool reads, wired to *client*."""
    return AppContext(
        client=client,
        config=Config(bucket=client.bucket_name, key_path=_UNIT_KEY_PATH),
    )


def _make_tool_ctx(client: FakeGCSClient) -> Any:
    """A minimal FastMCP ``Context`` stand-in over *client*'s AppContext.

    The tools reach the client only via ``ctx.request_context.lifespan_context``
    (``server._ctx``), so a nested namespace is enough and keeps the unit tier
    free of the MCP transport.
    """
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=_make_app_context(client))
    )


# ---------------------------------------------------------------------------
# Unit-tier fixtures (credential-free, FakeGCSClient)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_client() -> FakeGCSClient:
    """A fresh in-memory GCS stand-in for the unit tier (no credentials)."""
    return FakeGCSClient(_UNIT_BUCKET)


@pytest.fixture
def app_context(fake_client: FakeGCSClient) -> AppContext:
    """The lifespan ``AppContext`` wired to the unit-tier ``FakeGCSClient``."""
    return _make_app_context(fake_client)


@pytest.fixture
def tool_context(fake_client: FakeGCSClient) -> Any:
    """A FastMCP ``Context`` stand-in over the default unit-tier fake client."""
    return _make_tool_ctx(fake_client)


@pytest.fixture
def make_tool_ctx() -> Callable[[FakeGCSClient], Any]:
    """Factory returning a ``Context`` stand-in for a caller-supplied fake client.

    Tests that need a specially-configured fake (a different bucket name, or the
    ``ubla_on`` / ``fail_auth`` injectable failures) build their client and wrap
    it: ``ctx = make_tool_ctx(FakeGCSClient("b", ubla_on=True))``.
    """
    return _make_tool_ctx


# ---------------------------------------------------------------------------
# Live-tier fixtures (env-gated, real configuration)
# ---------------------------------------------------------------------------


@pytest.fixture
def live_config() -> Config:
    """Operator configuration for the live tier, or a clean skip when absent.

    ``GCS_BUCKET`` and ``GOOGLE_APPLICATION_CREDENTIALS`` are the minimum needed
    to reach a real bucket (``GCS_PROJECT_ID`` is optional). When either is
    missing the whole live test is **skipped**, not failed (ASM8): a default
    ``pytest`` run never reaches here, and ``pytest -m live`` without a populated
    ``.env`` is a no-op rather than a red build.
    """
    if not os.environ.get(ENV_BUCKET) or not os.environ.get(ENV_CREDENTIALS):
        pytest.skip(
            f"live tier needs {ENV_BUCKET} and {ENV_CREDENTIALS} set "
            "(copy .env.example to .env and fill it in); skipping."
        )
    return Config.from_env()
