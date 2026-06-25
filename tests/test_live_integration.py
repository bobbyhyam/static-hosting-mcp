"""Live integration suite — the real publish -> grant -> revoke -> delete cycle (U7).

Every test here is tagged ``@pytest.mark.live`` (via ``pytestmark``) so the
default ``pytest`` run — which adds ``-m 'not live'`` in ``pyproject.toml`` —
deselects the whole module. ``pytest -m live`` selects it; the ``live_config``
fixture (``conftest.py``) then **skips** cleanly when the operator's ``.env`` is
not populated, so the live tier is never a red build without credentials (R18,
ASM8).

What the live tier proves that the credential-free unit tier cannot:

- The lifecycle runs **through the real MCP transport** — the server is spawned
  as a stdio subprocess (``uv run static-hosting-mcp``) and driven with an MCP
  :class:`ClientSession`, exercising FastMCP argument parsing, ``Context``
  injection, and result shaping end to end (R1-R9 through the tool surface),
  mirroring the reference ``test_tools.py`` (``stdio_client`` + ``uv run`` +
  ``ClientSession`` + a ``_parse_result`` helper).
- Restricted read is asserted **at the live ACL layer** — the reader entry is
  present on the object after grant and absent after revoke (AE2, AE3) — read
  back with a real :class:`GCSClient`, not the fake's in-memory set.
- A single ``grant_access`` with multiple emails persists **all** of them
  (R4/R5): a real reload->save round-trip is where a lost update would drop
  grantees, which the fake's in-memory set cannot catch.
- An **unauthenticated** HTTP GET of the object's XML/JSON API URL
  (``storage.googleapis.com/<bucket>/<key>``) returns **403**, proving the object
  is not public. The user-facing URL stays the authenticated
  ``storage.cloud.google.com`` form (KTD3); that endpoint **redirects** an
  anonymous request to a Google sign-in page (302 -> ``accounts.google.com``)
  rather than 401/403, so it is unsuitable for the deny assertion and is not
  fetched here. Confirming a *granted human* can open the authenticated URL in a
  browser stays a **manual** acceptance check (documented in U8), not automated.

Grant/revoke assertions require **real Google accounts**: GCS rejects an unknown
principal on a per-object ACL save with HTTP 400 (R16; the U3 client surfaces a
bare 400 as the actionable UBLA message), so a synthetic ``example.com`` address
cannot be granted. The reader(s) are therefore taken from ``GCS_TEST_GRANTEE``
(single) and ``GCS_TEST_GRANTEES`` (comma-separated, >=2); the grant-dependent
tests **skip** when those are unset so the suite stays green against any bucket
with just ``.env`` filled in, and exercises the live ACL layer when the operator
supplies real grantees. Each test deletes the object it created on teardown.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from static_hosting_mcp.config import Config
from static_hosting_mcp.gcs_client import GCSClient, ObjectNotFoundError

pytestmark = [pytest.mark.live]


# ---------------------------------------------------------------------------
# Grantee selection — real Google accounts, or a clean skip
# ---------------------------------------------------------------------------


def _single_grantee() -> str:
    """A real grantee for the ACL test, or skip (GCS rejects unknown principals)."""
    email = os.environ.get("GCS_TEST_GRANTEE", "").strip()
    if not email:
        pytest.skip(
            "set GCS_TEST_GRANTEE to a real Google account to exercise the live "
            "grant/revoke ACL path — GCS rejects an unknown principal with HTTP 400."
        )
    return email


def _multi_grantees() -> list[str]:
    """At least two real grantees for the lost-update guard, or skip."""
    emails = [e.strip() for e in os.environ.get("GCS_TEST_GRANTEES", "").split(",") if e.strip()]
    if len(emails) < 2:
        pytest.skip(
            "set GCS_TEST_GRANTEES to >=2 comma-separated real Google accounts to "
            "exercise the multi-grantee single-call persistence guard."
        )
    return emails


# ---------------------------------------------------------------------------
# MCP transport + HTTP helpers
# ---------------------------------------------------------------------------


def _real_client(config: Config) -> GCSClient:
    """A real GCSClient over the configured bucket, for ACL-layer read-back."""
    return GCSClient(config.bucket, key_path=config.key_path, project=config.project)


@asynccontextmanager
async def _mcp_session() -> AsyncIterator[ClientSession]:
    """Spawn the server as a stdio subprocess and yield an initialized session.

    Mirrors the reference ``test_tools.py``: ``uv run static-hosting-mcp`` is the
    console-script entry point, launched under ``stdio_client``. The child
    inherits this process's environment (``.env`` already loaded by
    ``conftest.py``) so its lifespan builds the real client against the same
    bucket.
    """
    params = StdioServerParameters(
        command="uv",
        args=["run", "static-hosting-mcp"],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _parse_result(result: Any) -> dict[str, Any]:
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
            return json.loads(text)
    raise AssertionError(f"no parseable dict in tool result: {result!r}")


async def _call(session: ClientSession, name: str, **arguments: Any) -> dict[str, Any]:
    """Call a tool by name and return its parsed ``dict`` result."""
    return _parse_result(await session.call_tool(name, arguments))


def _http_status(url: str) -> int:
    """GET *url* anonymously and return the HTTP status (no credentials sent)."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture
def cleanup(live_config: Config) -> Iterator[list[str]]:
    """Track object keys and delete them from the real bucket on teardown."""
    keys: list[str] = []
    yield keys
    real = _real_client(live_config)

    async def _purge() -> None:
        for key in keys:
            with suppress(ObjectNotFoundError):
                await real.delete(key)

    asyncio.run(_purge())


# ---------------------------------------------------------------------------
# Live tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_get_delete_through_mcp(live_config: Config, cleanup: list[str]) -> None:
    """publish -> get -> delete through the real MCP transport (R1-R3, R8, R9, AE5).

    Exercises the full stdio round-trip — argument parsing, ``Context`` injection,
    and result shaping — and confirms the object no longer resolves after delete.
    Needs no grantee, so it runs against any bucket with ``.env`` filled in.
    """
    async with _mcp_session() as session:
        published = await _call(
            session,
            "publish_artifact",
            title="U7 live lifecycle",
            content="<html><body><h1>live</h1></body></html>",
        )
        assert "isError" not in published, published
        key = published["key"]
        cleanup.append(key)
        assert published["url"] == f"https://storage.cloud.google.com/{live_config.bucket}/{key}"

        detail = await _call(session, "get_artifact", object_ref=key)
        assert detail["url"] == published["url"]
        assert detail["content_type"] == "text/html"
        assert detail["size"] > 0

        deleted = await _call(session, "delete_artifact", object_ref=key)
        assert deleted["deleted"] is True
        missing = await _call(session, "get_artifact", object_ref=key)
        assert missing["isError"] is True


@pytest.mark.asyncio
async def test_grant_then_revoke_at_acl_layer(live_config: Config, cleanup: list[str]) -> None:
    """grant -> revoke through MCP, asserted at the live ACL layer (AE2, AE3).

    The reader is present after grant and absent after revoke, checked both via
    ``get_artifact`` and a direct :class:`GCSClient` read. Skips without a real
    ``GCS_TEST_GRANTEE``.
    """
    reader = _single_grantee()
    real = _real_client(live_config)

    async with _mcp_session() as session:
        published = await _call(
            session,
            "publish_artifact",
            title="U7 live acl",
            content="<html><body>acl</body></html>",
        )
        key = published["key"]
        cleanup.append(key)

        granted = await _call(session, "grant_access", object_ref=key, emails=[reader])
        assert "isError" not in granted, granted
        assert all(g["ok"] for g in granted["grants"]), granted
        assert reader in (await _call(session, "get_artifact", object_ref=key))["grantees"]
        assert reader in await real.list_grantees(key)

        revoked = await _call(session, "revoke_access", object_ref=key, emails=[reader])
        assert all(r["ok"] for r in revoked["revocations"]), revoked
        assert reader not in (await _call(session, "get_artifact", object_ref=key))["grantees"]
        assert reader not in await real.list_grantees(key)

        await _call(session, "delete_artifact", object_ref=key)


@pytest.mark.asyncio
async def test_multi_grantee_single_call_persists_all(
    live_config: Config, cleanup: list[str]
) -> None:
    """A single ``grant_access`` with multiple emails persists all of them (R4/R5).

    Read back with a real :class:`GCSClient`: a lost update in the reload->save
    round-trip would drop one, which the fake's in-memory set can never
    reproduce. Skips without >=2 real ``GCS_TEST_GRANTEES``.
    """
    emails = _multi_grantees()
    real = _real_client(live_config)

    async with _mcp_session() as session:
        published = await _call(
            session,
            "publish_artifact",
            title="U7 multi grantee persistence",
            content="<html><body>multi</body></html>",
        )
        key = published["key"]
        cleanup.append(key)

        granted = await _call(session, "grant_access", object_ref=key, emails=emails)
        assert "isError" not in granted, granted
        assert all(g["ok"] for g in granted["grants"]), granted

    grantees = await real.list_grantees(key)
    for email in emails:
        assert email in grantees, f"{email} missing from {grantees} (lost update?)"


@pytest.mark.asyncio
async def test_unauthenticated_xml_api_get_is_denied_403(
    live_config: Config, cleanup: list[str]
) -> None:
    """An anonymous GET of the XML/JSON API URL returns 403 (object not public).

    The authenticated ``storage.cloud.google.com`` URL is deliberately *not*
    fetched: it 302-redirects an anonymous request to Google sign-in rather than
    returning 403, so it cannot prove the object is private. Needs no grantee.
    """
    real = _real_client(live_config)
    key = "2026/06/24/u7-live-deny-check.html"
    cleanup.append(key)
    await real.upload(key, b"<html><body>not public</body></html>", "text/html")

    # The user-facing URL stays the authenticated form (KTD3)...
    assert real.authenticated_url(key) == (
        f"https://storage.cloud.google.com/{live_config.bucket}/{key}"
    )
    # ...but the deny check uses the XML/JSON API endpoint, which 403s anonymously.
    api_url = f"https://storage.googleapis.com/{live_config.bucket}/{key}"
    assert _http_status(api_url) == 403
