"""Always-on end-to-end tests: all six tools through the running stdio server.

This is the credential-free counterpart to ``test_live_integration.py``. It spawns
the server as a stdio subprocess **exactly the way ``.mcp.json`` launches it**
(``uv run`` + an MCP :class:`~mcp.ClientSession`) and drives every tool over the
genuine transport — so FastMCP argument parsing, ``Context`` injection, the
lifespan, and structured-result shaping are all exercised, not just the tool
coroutines (which the in-process unit tier in ``test_tools_unit.py`` already
covers). The one seam is the GCS leaf: the subprocess runs
``tests/stdio_fake_server.py``, which swaps :class:`GCSClient` for the in-memory
:class:`~tests.fakes.FakeGCSClient`, so the suite needs no bucket and no
credentials and is fully deterministic. It runs by default (no ``live`` marker)
and is the durable, CI-able form of the ``CLAUDE.md`` tmux live-test loop.

The real-bucket variant of this same round-trip lives in
``test_live_integration.py`` and is gated/skipped without ``.env`` (R18, ASM8).
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from .mcp_harness import call_tool, mcp_session

# uv is the project's standard runner (README "Development"; the same command
# .mcp.json uses). If it is somehow not on PATH, skip rather than error — these
# tests spawn `uv run` exactly like the live tier spawns the real server.
pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="`uv` not on PATH; the stdio E2E tier spawns the server with `uv run`.",
)

# Repo root = the directory holding pyproject.toml (this file is tests/<name>.py).
_REPO_ROOT = Path(__file__).resolve().parents[1]
# A deterministic, non-secret bucket name so returned URLs are predictable. It is
# never contacted — the subprocess swaps in the in-memory fake.
_FAKE_BUCKET = "static-hosting-e2e-bucket"
# The six tools the server must expose (R14-R16).
_ALL_TOOLS = {
    "publish_artifact",
    "grant_access",
    "revoke_access",
    "list_artifacts",
    "get_artifact",
    "delete_artifact",
}
# A published inline-HTML key is YYYY/MM/DD/<slug>-<6 alnum>.html (KTD6 / AE1).
_DATED_HTML_KEY_RE = re.compile(r"^\d{4}/\d{2}/\d{2}/e2e-lifecycle-smoke-[a-z0-9]{6}\.html$")


def _fake_server_params() -> StdioServerParameters:
    """Launch params for the fake-backed server, mirroring ``.mcp.json``'s shape.

    Starts from the current environment (so ``uv``/Python find PATH, HOME, the
    cache) but pins the GCS config to deterministic, non-secret placeholders and
    drops the host's ``.env`` source-path knobs, so the subprocess is independent
    of whatever ``.env`` the operator has. ``cwd``/``PYTHONPATH`` at the repo root
    make ``python -m tests.stdio_fake_server`` import the ``tests`` package.
    """
    env = dict(os.environ)
    env["GCS_BUCKET"] = _FAKE_BUCKET
    # Absolute (Config requires it) but never read — the fake reads no credential.
    env["GOOGLE_APPLICATION_CREDENTIALS"] = "/nonexistent/fake-e2e-key.json"
    env["PYTHONPATH"] = str(_REPO_ROOT)
    env.pop("ARTIFACT_SOURCE_ROOT", None)
    env.pop("ARTIFACT_MAX_BYTES", None)
    return StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "tests.stdio_fake_server"],
        env=env,
        cwd=str(_REPO_ROOT),
    )


@pytest.mark.asyncio
async def test_six_tool_lifecycle_through_stdio_with_fake_gcs() -> None:
    """publish -> get -> list -> grant -> revoke -> delete, all over the transport.

    A single session drives all six tools end to end and asserts the structured
    results FastMCP shaped and serialized back across stdio: the publish-time
    grant lands, get/list reflect the ACL, a second grant and a revoke mutate it,
    and after delete the object stops resolving.
    """
    body = "<html><body><h1>e2e</h1></body></html>"
    async with mcp_session(_fake_server_params()) as session:
        # -- publish_artifact (with a grant in the same call) ---------------
        published = await call_tool(
            session,
            "publish_artifact",
            title="E2E lifecycle smoke",
            content=body,
            grant_emails=["alice@example.com"],
        )
        assert "isError" not in published, published
        key = published["key"]
        assert _DATED_HTML_KEY_RE.match(key), key
        assert published["url"] == f"https://storage.cloud.google.com/{_FAKE_BUCKET}/{key}"
        assert published["content_type"] == "text/html"
        assert published["size"] == len(body.encode("utf-8"))
        assert published["grants"] == [{"email": "alice@example.com", "ok": True}]

        # -- get_artifact: the grantee from publish is present --------------
        detail = await call_tool(session, "get_artifact", object_ref=key)
        assert detail["url"] == published["url"]
        assert detail["content_type"] == "text/html"
        assert detail["size"] == published["size"]
        assert detail["grantees"] == ["alice@example.com"]

        # -- list_artifacts: the object appears with a grantee *count* ------
        listing = await call_tool(session, "list_artifacts")
        assert listing["total"] >= 1
        by_key = {item["key"]: item for item in listing["items"]}
        assert key in by_key, listing
        assert by_key[key]["grantee_count"] == 1
        assert by_key[key]["url"] == published["url"]
        # The date_prefix filter (derived from the key's date folder) still finds it.
        date_prefix = key.rsplit("/", 1)[0]
        filtered = await call_tool(session, "list_artifacts", date_prefix=date_prefix)
        assert key in {item["key"] for item in filtered["items"]}

        # -- grant_access: add a second reader ------------------------------
        granted = await call_tool(
            session, "grant_access", object_ref=key, emails=["bob@example.com"]
        )
        assert "isError" not in granted, granted
        assert all(g["ok"] for g in granted["grants"]), granted
        after_grant = await call_tool(session, "get_artifact", object_ref=key)
        assert after_grant["grantees"] == ["alice@example.com", "bob@example.com"]  # sorted

        # -- revoke_access: remove the first reader -------------------------
        revoked = await call_tool(
            session, "revoke_access", object_ref=key, emails=["alice@example.com"]
        )
        assert "isError" not in revoked, revoked
        assert all(r["ok"] for r in revoked["revocations"]), revoked
        after_revoke = await call_tool(session, "get_artifact", object_ref=key)
        assert after_revoke["grantees"] == ["bob@example.com"]

        # -- delete_artifact: the object stops resolving --------------------
        deleted = await call_tool(session, "delete_artifact", object_ref=key)
        assert deleted["deleted"] is True
        assert deleted["key"] == key
        missing = await call_tool(session, "get_artifact", object_ref=key)
        assert missing["isError"] is True


@pytest.mark.asyncio
async def test_all_six_tools_registered_and_errors_curated_over_stdio() -> None:
    """All six tools list over the transport, and tool errors stay curated dicts.

    Complements the in-process registration test (``test_tools_unit.py``) by going
    through the real session, and proves the ``{"isError": true, ...}`` envelope —
    not a raw MCP protocol error — survives the round-trip for both a validation
    refusal (a malformed ``date_prefix``) and a not-found lookup.
    """
    async with mcp_session(_fake_server_params()) as session:
        listed = await session.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        assert _ALL_TOOLS <= set(tools), sorted(tools)
        # R14 annotations survive the transport: read-only on the inspect tools,
        # destructive on the mutating-removal ones. Bind locals so the
        # ``ToolAnnotations | None`` narrows for the attribute reads.
        list_ann = tools["list_artifacts"].annotations
        get_ann = tools["get_artifact"].annotations
        delete_ann = tools["delete_artifact"].annotations
        revoke_ann = tools["revoke_access"].annotations
        assert list_ann is not None and list_ann.readOnlyHint is True
        assert get_ann is not None and get_ann.readOnlyHint is True
        assert delete_ann is not None and delete_ann.destructiveHint is True
        assert revoke_ann is not None and revoke_ann.destructiveHint is True

        # A malformed date_prefix is a curated refusal, not a transport error, and
        # must not fall through to listing the whole bucket.
        bad_filter = await call_tool(session, "list_artifacts", date_prefix="June-2026")
        assert bad_filter["isError"] is True
        assert "YYYY" in bad_filter["error"]
        assert "items" not in bad_filter

        # A not-found lookup echoes the caller's reference and names the next step.
        missing = await call_tool(session, "get_artifact", object_ref="2026/06/24/missing.html")
        assert missing["isError"] is True
        assert "2026/06/24/missing.html" in missing["error"]
