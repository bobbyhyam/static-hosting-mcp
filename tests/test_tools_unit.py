"""In-memory unit tests for the MCP tool layer (U5, U6).

Credential-free tool-layer tests against the in-memory :class:`FakeGCSClient`.
The GCS client-layer tests (``TestGCSClientUnit`` /
``TestGCSClientRealErrorMapping`` and the ``_real_client`` builder) were split
out into ``tests/test_gcs_client.py`` (code-review RF10) so this module stays
single-purpose and under the size threshold; ``_real_client`` is imported back
for the one tool test that drives the RF4 dependency-down mapping end-to-end.

``TestArtifactInspectionTools`` drives ``list_artifacts`` / ``get_artifact`` /
``delete_artifact`` (AE4, AE5; R6 read side, R7-R9); ``TestPublishArtifact`` and
``TestGrantAndRevokeAccess`` drive the write tools (R4, R5); the
``...ToolRegistration`` classes check the FastMCP registration, annotations, and
parameter schemas (R14-R16); and ``TestHandleApiError`` covers the typed-error
-> curated-dict mapping (R17). The full MCP round-trip (arg parsing + Context
injection) is exercised by the live suite (U7); here each tool is called
directly with a stand-in Context.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import requests.exceptions
from mcp.server.fastmcp import Context

from static_hosting_mcp.config import DEFAULT_MAX_ARTIFACT_BYTES, Config
from static_hosting_mcp.gcs_client import (
    AuthError,
    GCSError,
    ObjectNotFoundError,
    UBLAEnabledError,
)
from static_hosting_mcp.server import (
    AppContext,
    _handle_api_error,
    delete_artifact,
    get_artifact,
    grant_access,
    list_artifacts,
    mcp,
    publish_artifact,
    revoke_access,
)

from .fakes import FakeGCSClient
from .test_gcs_client import _real_client

# ---------------------------------------------------------------------------
# U6 tool-layer tests (list_artifacts, get_artifact, delete_artifact)
# ---------------------------------------------------------------------------
#
# The FastMCP ``@mcp.tool()`` decorator returns the original coroutine function,
# so each tool is invoked directly with a lightweight stand-in Context whose
# ``request_context.lifespan_context`` is the AppContext the tool reads via
# ``_ctx``. No credentials, no live bucket, no MCP transport.


def _tool_ctx(client: FakeGCSClient) -> Any:
    """A minimal stand-in for the FastMCP Context the U6 tools read via ``_ctx``."""
    app = AppContext(
        client=client, config=Config(bucket=client.bucket_name, key_path="/k.json")
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


async def _seed(client: FakeGCSClient, keys: list[str]) -> None:
    """Upload a small HTML object at each key so the listing tools have data."""
    for key in keys:
        await client.upload(key, b"<html>x</html>", "text/html")


class TestArtifactInspectionTools:
    """list_artifacts / get_artifact / delete_artifact against the in-memory fake."""

    # -- list_artifacts -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_list_returns_curated_summaries_with_grantee_count(self) -> None:
        client = FakeGCSClient("my-bucket")
        await _seed(client, ["2026/06/24/a.html"])
        await client.grant_read("2026/06/24/a.html", ["alice@example.com", "bob@example.com"])
        result = await list_artifacts(ctx=_tool_ctx(client))
        assert result["total"] == 1
        assert result["truncated"] is False
        assert result["hint"]
        item = result["items"][0]
        # Exactly the curated summary keys -- a grantee *count*, never the ACL.
        assert set(item) == {"key", "url", "created", "size", "grantee_count"}
        assert item["key"] == "2026/06/24/a.html"
        assert item["url"] == "https://storage.cloud.google.com/my-bucket/2026/06/24/a.html"
        assert item["grantee_count"] == 2
        assert "grantees" not in item

    @pytest.mark.asyncio
    async def test_list_truncation_and_total_is_page_count(self) -> None:
        # More than ``limit`` objects present -> ``truncated`` is True and
        # ``total`` is the count returned in *this* response (len(items)), not a
        # full-bucket count.
        client = FakeGCSClient()
        await _seed(client, [f"2026/06/24/f{i}.html" for i in range(5)])
        result = await list_artifacts(limit=2, ctx=_tool_ctx(client))
        assert len(result["items"]) == 2
        assert result["total"] == 2
        assert result["truncated"] is True
        assert "narrow date_prefix or raise limit" in result["hint"]

    @pytest.mark.asyncio
    async def test_list_not_truncated_when_count_equals_limit(self) -> None:
        # The limit + 1 probe must not false-positive when exactly ``limit``
        # objects exist (fetched == limit, not > limit).
        client = FakeGCSClient()
        await _seed(client, [f"2026/06/24/f{i}.html" for i in range(2)])
        result = await list_artifacts(limit=2, ctx=_tool_ctx(client))
        assert result["total"] == 2
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_list_date_prefix_filters_to_matching_keys(self) -> None:
        client = FakeGCSClient()
        await _seed(
            client, ["2026/06/24/a.html", "2026/06/25/b.html", "2026/07/01/c.html"]
        )
        ctx = _tool_ctx(client)
        june = await list_artifacts(date_prefix="2026/06", ctx=ctx)
        assert [i["key"] for i in june["items"]] == [
            "2026/06/24/a.html",
            "2026/06/25/b.html",
        ]
        day = await list_artifacts(date_prefix="2026/06/24", ctx=ctx)
        assert [i["key"] for i in day["items"]] == ["2026/06/24/a.html"]
        year = await list_artifacts(date_prefix="2026", ctx=ctx)
        assert len(year["items"]) == 3

    @pytest.mark.asyncio
    async def test_list_malformed_date_prefix_returns_structured_error(self) -> None:
        client = FakeGCSClient()
        await _seed(client, ["2026/06/24/a.html"])
        result = await list_artifacts(date_prefix="June-2026", ctx=_tool_ctx(client))
        assert result["isError"] is True
        assert "YYYY" in result["error"]
        # A bad filter must not fall through to listing the whole bucket.
        assert "items" not in result

    @pytest.mark.asyncio
    async def test_list_empty_bucket_is_empty_envelope(self) -> None:
        result = await list_artifacts(ctx=_tool_ctx(FakeGCSClient()))
        assert result["items"] == []
        assert result["total"] == 0
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_list_objects_failure_returns_curated_error(self) -> None:
        # RF2 (2a): a listing-time GCSError (e.g. a 403 AuthError) returns the
        # curated isError dict every sibling returns, not a raw FastMCP protocol
        # error, and does NOT fall through to a partial listing.
        client = FakeGCSClient("b", fail_auth=True)
        result = await list_artifacts(ctx=_tool_ctx(client))
        assert result["isError"] is True
        assert "Permission denied" in result["error"]
        assert "items" not in result

    @pytest.mark.asyncio
    async def test_list_one_grantee_failure_degrades_not_collapses_page(self) -> None:
        # RF2 (2b): an object deleted between the list_objects snapshot and its
        # list_grantees reload raises ObjectNotFoundError; return_exceptions=True
        # keeps the whole page, degrading only that object's grantee_count to None.
        client = FakeGCSClient("my-bucket")
        await _seed(client, ["2026/06/24/a.html", "2026/06/24/b.html"])
        await client.grant_read("2026/06/24/a.html", ["alice@example.com"])
        client.fail_list_grantees(
            "2026/06/24/b.html",
            ObjectNotFoundError("vanished mid-listing", key="2026/06/24/b.html"),
        )
        result = await list_artifacts(ctx=_tool_ctx(client))
        assert result["total"] == 2  # the page survived rather than collapsing
        by_key = {i["key"]: i for i in result["items"]}
        assert by_key["2026/06/24/a.html"]["grantee_count"] == 1
        assert by_key["2026/06/24/b.html"]["grantee_count"] is None

    # -- get_artifact -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_by_key_returns_detail_with_grantees(self) -> None:
        # Covers AE4: get returns url, content_type, size, created, grantees.
        client = FakeGCSClient("my-bucket")
        await client.upload("2026/06/24/a.html", b"<html>hi</html>", "text/html")
        await client.grant_read("2026/06/24/a.html", ["bob@example.com", "alice@example.com"])
        detail = await get_artifact("2026/06/24/a.html", ctx=_tool_ctx(client))
        assert set(detail) == {"url", "content_type", "size", "created", "grantees"}
        assert detail["url"] == "https://storage.cloud.google.com/my-bucket/2026/06/24/a.html"
        assert detail["content_type"] == "text/html"
        assert detail["size"] == len(b"<html>hi</html>")
        assert detail["created"]
        assert detail["grantees"] == ["alice@example.com", "bob@example.com"]  # sorted

    @pytest.mark.asyncio
    async def test_get_by_full_url_matches_get_by_key(self) -> None:
        # Covers KTD7: a full authenticated URL normalizes to the same key.
        client = FakeGCSClient("my-bucket")
        await client.upload("2026/06/24/a.html", b"x", "text/html")
        ctx = _tool_ctx(client)
        url = "https://storage.cloud.google.com/my-bucket/2026/06/24/a.html"
        assert await get_artifact(url, ctx=ctx) == await get_artifact(
            "2026/06/24/a.html", ctx=ctx
        )

    @pytest.mark.asyncio
    async def test_get_missing_returns_actionable_not_found_error(self) -> None:
        result = await get_artifact("2026/06/24/missing.html", ctx=_tool_ctx(FakeGCSClient()))
        assert result["isError"] is True
        assert "2026/06/24/missing.html" in result["error"]  # echoes the caller's ref
        assert "list_artifacts" in result["error"]  # states the next step

    @pytest.mark.asyncio
    async def test_get_artifact_dependency_down_returns_curated_iserror(self) -> None:
        # RF4 end-to-end: a non-GoogleAPICallError transport failure under a tool
        # still comes back as the curated isError dict every sibling returns, not a
        # raw FastMCP protocol error. Uses the real GCSClient over a mock so the
        # widened wrapper mapping is exercised (the fake never raises this).
        real, mc = _real_client("artifacts-bucket")
        mc.bucket.return_value.blob.return_value.reload.side_effect = (
            requests.exceptions.ConnectionError("Connection refused")
        )
        result = await get_artifact("2026/06/24/a.html", ctx=_tool_ctx(cast(Any, real)))
        assert result["isError"] is True
        assert "artifacts-bucket" in result["error"]

    # -- delete_artifact ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_delete_removes_then_get_returns_not_found(self) -> None:
        # Covers AE5: after delete the object no longer resolves.
        client = FakeGCSClient("my-bucket")
        await client.upload("2026/06/24/a.html", b"x", "text/html")
        ctx = _tool_ctx(client)
        deleted = await delete_artifact("2026/06/24/a.html", ctx=ctx)
        assert deleted["deleted"] is True
        assert deleted["key"] == "2026/06/24/a.html"
        assert await client.exists("2026/06/24/a.html") is False
        after = await get_artifact("2026/06/24/a.html", ctx=ctx)
        assert after["isError"] is True

    @pytest.mark.asyncio
    async def test_delete_by_full_url(self) -> None:
        client = FakeGCSClient("my-bucket")
        await client.upload("2026/06/24/a.html", b"x", "text/html")
        url = "https://storage.cloud.google.com/my-bucket/2026/06/24/a.html"
        result = await delete_artifact(url, ctx=_tool_ctx(client))
        assert result["deleted"] is True
        assert result["key"] == "2026/06/24/a.html"

    @pytest.mark.asyncio
    async def test_delete_missing_returns_actionable_not_found_error(self) -> None:
        result = await delete_artifact(
            "2026/06/24/missing.html", ctx=_tool_ctx(FakeGCSClient())
        )
        assert result["isError"] is True
        assert "2026/06/24/missing.html" in result["error"]


class TestReadToolRegistration:
    """FastMCP registration, annotations, and parameter schemas for U6 (R14-R16)."""

    @pytest.mark.asyncio
    async def test_u6_tools_registered_with_annotations(self) -> None:
        tools = {t.name: t for t in await mcp.list_tools()}
        for name in ("list_artifacts", "get_artifact", "delete_artifact"):
            assert name in tools
        # R14: readOnlyHint on the inspect tools, destructiveHint on delete.
        list_ann = tools["list_artifacts"].annotations
        get_ann = tools["get_artifact"].annotations
        delete_ann = tools["delete_artifact"].annotations
        assert list_ann is not None and list_ann.readOnlyHint is True
        assert get_ann is not None and get_ann.readOnlyHint is True
        assert delete_ann is not None and delete_ann.destructiveHint is True

    @pytest.mark.asyncio
    async def test_tool_parameter_schemas_and_sibling_disambiguation(self) -> None:
        tools = {t.name: t for t in await mcp.list_tools()}
        # R16: Pydantic-built parameter schemas; ``ctx`` is injected, not a param.
        assert set(tools["list_artifacts"].inputSchema["properties"]) == {
            "date_prefix",
            "limit",
        }
        assert set(tools["get_artifact"].inputSchema["properties"]) == {"object_ref"}
        assert set(tools["delete_artifact"].inputSchema["properties"]) == {"object_ref"}
        # R15: action-first, sibling-disambiguating descriptions.
        assert "get_artifact" in (tools["list_artifacts"].description or "")
        assert "list_artifacts" in (tools["get_artifact"].description or "")


class TestHandleApiError:
    """The shared typed-error -> curated-dict mapping in server.py (R17)."""

    def test_not_found_echoes_reference_and_next_step(self) -> None:
        result = _handle_api_error(
            ObjectNotFoundError("x", key="k.html"), reference="bad/ref.html"
        )
        assert result["isError"] is True
        assert "bad/ref.html" in result["error"]  # the caller's input, echoed back
        assert "list_artifacts" in result["error"]  # the next step

    def test_not_found_without_reference_falls_back_to_exc_key(self) -> None:
        result = _handle_api_error(ObjectNotFoundError("x", key="k.html"))
        assert "k.html" in result["error"]

    def test_ubla_names_bucket_and_disable_command(self) -> None:
        result = _handle_api_error(UBLAEnabledError("x", bucket="my-bucket"))
        assert result["isError"] is True
        assert "Uniform Bucket-Level Access" in result["error"]
        assert "gs://my-bucket" in result["error"]

    def test_auth_states_what_and_next_step(self) -> None:
        result = _handle_api_error(AuthError("Permission denied (HTTP 403)", status=403))
        assert result["isError"] is True
        assert "Permission denied" in result["error"]
        assert "objectAdmin" in (result.get("hint") or "")

    def test_generic_gcserror_surfaces_message(self) -> None:
        result = _handle_api_error(GCSError("GCS API error (HTTP 500)"))
        assert result["isError"] is True
        assert "HTTP 500" in result["error"]

# ---------------------------------------------------------------------------
# U5 write-tool helpers + tests (publish_artifact, grant_access, revoke_access)
# ---------------------------------------------------------------------------


def _ctx_for(
    client: FakeGCSClient,
    *,
    source_root: str | None = None,
    max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    key_path: str = "/abs/key.json",
) -> Context:
    """Build a Context whose lifespan ``AppContext`` carries the in-memory fake.

    The tools only ever reach ``ctx.request_context.lifespan_context``, so a tiny
    duck-typed stand-in is enough; it is cast to :class:`Context` to match the tool
    signatures (the real ``Context`` is injected by FastMCP at runtime). The
    ``source_root`` / ``max_bytes`` / ``key_path`` knobs drive publish_artifact's
    RF1 source-path confinement and size cap.
    """

    class _FakeRequestContext:
        def __init__(self, app: AppContext) -> None:
            self.lifespan_context = app

    class _FakeContext:
        def __init__(self, app: AppContext) -> None:
            self.request_context = _FakeRequestContext(app)

    config = Config(
        bucket=client.bucket_name,
        key_path=key_path,
        artifact_source_root=source_root,
        artifact_max_bytes=max_bytes,
    )
    return cast(Context, _FakeContext(AppContext(client=client, config=config)))


# A published inline-HTML key is YYYY/MM/DD/<slug>-<6 alnum>.html (KTD6 / AE1).
_DATED_HTML_KEY_RE = re.compile(r"^\d{4}/\d{2}/\d{2}/q2-tariff-deep-research-[a-z0-9]{6}\.html$")


class TestPublishArtifact:
    """publish_artifact: XOR validation, key/URL shape, grant-on-publish, errors."""

    @pytest.mark.asyncio
    async def test_both_content_and_source_path_is_error_no_upload(self) -> None:
        # Covers AE7: ambiguous input -> structured error, no object created.
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t",
            content="<html></html>",
            source_path="/tmp/whatever.html",
            ctx=_ctx_for(client),
        )
        assert result["isError"] is True
        assert "not both" in result["error"]
        assert client.objects == {}

    @pytest.mark.asyncio
    async def test_neither_content_nor_source_path_is_error(self) -> None:
        # Covers AE7: neither supplied -> structured error.
        client = FakeGCSClient()
        result = await publish_artifact(title="t", ctx=_ctx_for(client))
        assert result["isError"] is True
        assert "exactly one" in result["error"]
        assert client.objects == {}

    @pytest.mark.asyncio
    async def test_inline_html_stored_at_dated_key_with_authenticated_url(self) -> None:
        # Covers AE1: inline HTML + title -> dated .html key; URL is authenticated.
        client = FakeGCSClient("my-bucket")
        body = "<html><body>hi</body></html>"
        result = await publish_artifact(
            title="Q2 tariff deep research", content=body, ctx=_ctx_for(client)
        )
        key = result["key"]
        assert _DATED_HTML_KEY_RE.match(key)
        assert result["url"] == f"https://storage.cloud.google.com/my-bucket/{key}"
        assert result["content_type"] == "text/html"
        assert result["size"] == len(body.encode("utf-8"))
        assert result["grants"] == []
        assert key in client.objects  # the object was actually uploaded

    @pytest.mark.asyncio
    async def test_source_path_uploaded_with_inferred_content_type(self, tmp_path) -> None:
        # source_path under the configured allow-list root -> uploaded with the
        # inferred content-type (RF1: source_path requires ARTIFACT_SOURCE_ROOT).
        src = tmp_path / "notes.md"
        src.write_text("# Heading\n", encoding="utf-8")
        client = FakeGCSClient()
        result = await publish_artifact(
            title="My notes",
            source_path=str(src),
            ctx=_ctx_for(client, source_root=str(tmp_path)),
        )
        key = result["key"]
        assert key.endswith(".md")
        assert result["content_type"] == "text/markdown"
        data, content_type, _created, size = client.objects[key]
        assert data == b"# Heading\n"
        assert content_type == "text/markdown"
        assert size == len(b"# Heading\n")

    @pytest.mark.asyncio
    async def test_empty_content_is_rejected_before_upload(self) -> None:
        # Presence-based XOR passes (content supplied) but zero bytes is refused.
        client = FakeGCSClient()
        result = await publish_artifact(title="t", content="", ctx=_ctx_for(client))
        assert result["isError"] is True
        assert "empty" in result["error"].lower()
        assert client.objects == {}

    @pytest.mark.asyncio
    async def test_source_path_denied_when_no_root_configured(self) -> None:
        # RF1 default-deny: with no ARTIFACT_SOURCE_ROOT set, any source_path is
        # refused with a structured error and no upload (R17 / KTD11).
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t", source_path="/no/such/file/here.txt", ctx=_ctx_for(client)
        )
        assert result["isError"] is True
        assert "source_path" in result["error"]
        assert "ARTIFACT_SOURCE_ROOT" in result["error"]
        assert client.objects == {}

    @pytest.mark.asyncio
    async def test_missing_file_within_root_is_structured_error(self, tmp_path) -> None:
        # A missing (non-regular) file *inside* the allowed root surfaces a
        # structured error, not a crash (R17 / KTD11), and uploads nothing.
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t",
            source_path=str(tmp_path / "absent.txt"),
            ctx=_ctx_for(client, source_root=str(tmp_path)),
        )
        assert result["isError"] is True
        assert "not a regular file" in result["error"]
        assert client.objects == {}

    @pytest.mark.asyncio
    async def test_grant_emails_on_publish_adds_reader(self) -> None:
        # Covers AE2 (ACL layer): grant_emails on publish -> reader present, ok.
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t",
            content="<html></html>",
            grant_emails=["alice@example.com"],
            ctx=_ctx_for(client),
        )
        assert result["grants"] == [{"email": "alice@example.com", "ok": True}]
        assert await client.list_grantees(result["key"]) == ["alice@example.com"]

    @pytest.mark.asyncio
    async def test_publish_on_ubla_bucket_returns_recoverable_success_with_warning(
        self,
    ) -> None:
        # Covers AE6 + RF5: UBLA-on -> the upload succeeds and the grant fails, so
        # the result is a recoverable success-with-warning that still carries the
        # key/url (NOT a bare key-less error), with the valid email marked failed
        # and the actionable UBLA guidance in the warning. Exactly one object exists.
        client = FakeGCSClient("ubla-bucket", ubla_on=True)
        result = await publish_artifact(
            title="t",
            content="<html></html>",
            grant_emails=["alice@example.com"],
            ctx=_ctx_for(client),
        )
        # Not an error result: the object is live and addressable.
        assert "isError" not in result
        assert result["key"]
        assert result["url"].endswith(result["key"])
        assert result["size"] == len(b"<html></html>")
        # The grant failure is recorded per-email and surfaced as an actionable warning.
        grants = {g["email"]: g for g in result["grants"]}
        assert grants["alice@example.com"]["ok"] is False
        assert "error" in grants["alice@example.com"]
        assert "ubla-bucket" in result["warning"]
        assert "uniform bucket-level access" in result["warning"].lower()
        assert "gcloud storage buckets update" in result["warning"]
        assert "grant_access" in result["warning"]  # how to recover
        assert len(client.objects) == 1  # exactly one object, recoverable by key
        # The key is genuinely usable: grant_access on a non-UBLA view would target it.
        assert result["key"] in client.objects

    # -- RF1: source_path confinement, secret refusal, size cap -------------

    @pytest.mark.asyncio
    async def test_source_path_outside_root_refused_no_upload(self, tmp_path) -> None:
        # RF1: a real, readable file OUTSIDE the allowed root is refused, no upload.
        outside = tmp_path / "outside.txt"
        outside.write_text("data", encoding="utf-8")
        root = tmp_path / "allowed"
        root.mkdir()
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t",
            source_path=str(outside),
            ctx=_ctx_for(client, source_root=str(root)),
        )
        assert result["isError"] is True
        assert "outside the allowed source directory" in result["error"]
        assert client.objects == {}

    @pytest.mark.asyncio
    async def test_source_path_symlink_escaping_root_refused(self, tmp_path) -> None:
        # RF1: a symlink INSIDE the root pointing OUTSIDE it resolves to the real
        # out-of-root target and is refused — canonicalization closes symlink escapes.
        root = tmp_path / "allowed"
        root.mkdir()
        secret = tmp_path / "outside-secret.txt"
        secret.write_text("top secret", encoding="utf-8")
        link = root / "link.txt"
        link.symlink_to(secret)
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t",
            source_path=str(link),
            ctx=_ctx_for(client, source_root=str(root)),
        )
        assert result["isError"] is True
        assert "outside the allowed source directory" in result["error"]
        assert client.objects == {}

    @pytest.mark.asyncio
    async def test_credentials_file_refused_even_inside_root(self, tmp_path) -> None:
        # RF1: the configured service-account key is refused even when it sits INSIDE
        # the allowed root (the secret-shape check precedes the root check) — the
        # highest-value exfiltration target the control exists to protect.
        key_file = tmp_path / "gcs-sa-key.json"
        key_file.write_text('{"type":"service_account"}', encoding="utf-8")
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t",
            source_path=str(key_file),
            ctx=_ctx_for(client, source_root=str(tmp_path), key_path=str(key_file)),
        )
        assert result["isError"] is True
        assert "credential" in result["error"].lower()
        assert client.objects == {}

    @pytest.mark.asyncio
    async def test_secret_suffix_pem_refused_inside_root(self, tmp_path) -> None:
        # RF1: independent of the configured key, a *.pem under the root is refused
        # as a secret shape.
        pem = tmp_path / "private.pem"
        pem.write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t",
            source_path=str(pem),
            ctx=_ctx_for(client, source_root=str(tmp_path)),
        )
        assert result["isError"] is True
        assert client.objects == {}

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="mkfifo unavailable")
    @pytest.mark.asyncio
    async def test_fifo_source_path_refused_before_read(self, tmp_path) -> None:
        # RF1 / adversarial A6: a FIFO is refused by the is_file() gate BEFORE any
        # read, so a read that would block the event loop forever never happens.
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t",
            source_path=str(fifo),
            ctx=_ctx_for(client, source_root=str(tmp_path)),
        )
        assert result["isError"] is True
        assert "not a regular file" in result["error"]
        assert client.objects == {}

    @pytest.mark.asyncio
    async def test_source_path_over_max_bytes_refused_without_upload(self, tmp_path) -> None:
        # RF1: a file larger than the cap is refused from a stat (no read into
        # memory — the OOM guard) and nothing is uploaded.
        big = tmp_path / "big.txt"
        big.write_bytes(b"x" * 50)
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t",
            source_path=str(big),
            ctx=_ctx_for(client, source_root=str(tmp_path), max_bytes=10),
        )
        assert result["isError"] is True
        assert "over the" in result["error"]
        assert client.objects == {}

    @pytest.mark.asyncio
    async def test_inline_content_over_max_bytes_refused(self) -> None:
        # RF1: the size cap applies to inline content too.
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t", content="x" * 100, ctx=_ctx_for(client, max_bytes=10)
        )
        assert result["isError"] is True
        assert "over the" in result["error"]
        assert client.objects == {}

    # -- RF11: the source-path gate must honor its never-raise contract ------

    @pytest.mark.asyncio
    async def test_source_path_with_nul_byte_is_structured_error_no_upload(
        self, tmp_path
    ) -> None:
        # RF11 / C3: a source_path with an embedded NUL byte makes Path.resolve()
        # raise ValueError ("embedded null character in path"), which the gate's
        # original narrow `except OSError` missed -- so _check_source_path, which
        # is documented to NEVER raise (R17/KTD11) and is called unguarded by
        # publish_artifact, would propagate the ValueError. Pydantic does not strip
        # NUL from a str, so "a\x00b" reaches the tool. The widened
        # `except (OSError, ValueError)` must turn it into the same structured
        # refusal with no upload -- BOTH when source_path uploads are disabled
        # (no root: the crash happened before the default-deny) and when a root is
        # configured.
        for source_root in (None, str(tmp_path)):
            client = FakeGCSClient()
            result = await publish_artifact(
                title="t",
                source_path="a\x00b",
                ctx=_ctx_for(client, source_root=source_root),
            )
            assert result["isError"] is True, source_root
            assert "source_path" in result["error"]
            assert client.objects == {}, source_root

    @pytest.mark.asyncio
    async def test_source_path_when_home_undeterminable_does_not_raise(
        self, tmp_path, monkeypatch
    ) -> None:
        # RF11 / reliability-5: _is_secret_path calls Path.home(), which raises
        # RuntimeError when the home dir cannot be resolved (HOME unset and no pwd
        # entry for the uid -- a real distroless / scratch container shape). It runs
        # on every source_path attempt, before the default-deny, so an unguarded
        # call would crash the never-raise gate. The guard must skip only the
        # home-dir secret checks and let a normal file under the root still publish.
        def _no_home(*args: object, **kwargs: object) -> Path:
            raise RuntimeError("Could not determine home directory")

        monkeypatch.setattr(Path, "home", _no_home)
        src = tmp_path / "notes.md"
        src.write_text("# hi\n", encoding="utf-8")
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t",
            source_path=str(src),
            ctx=_ctx_for(client, source_root=str(tmp_path)),
        )
        assert "isError" not in result  # home-dir check skipped; file still published
        assert result["key"] in client.objects

    # -- RF12: the gate must stay total under EACCES and never leak the resolved path

    @pytest.mark.skipif(
        os.geteuid() == 0,
        reason="EACCES arm requires a non-root user (root bypasses the permission check)",
    )
    @pytest.mark.asyncio
    async def test_unreadable_source_path_is_refusal_without_leaking_resolved_path(
        self, tmp_path
    ) -> None:
        # RF12 / adversarial ADV4-1 + RR-1: _check_source_path is documented to
        # never raise and publish_artifact consumes it unguarded, so a syscall that
        # hits EACCES under the allow-list root must become a *curated* structured
        # refusal -- and that refusal must echo only the caller-supplied source_path,
        # never the *resolved* absolute path (OSError.filename), which would leak the
        # real on-disk location. Here an unreadable (mode 000) regular file passes
        # is_file()/stat() but raises in read_bytes(); before RF12 the catch
        # interpolated the raw OSError and leaked the resolved path.
        secret = tmp_path / "secret.html"
        secret.write_text("<h1>x</h1>", encoding="utf-8")
        resolved = str(secret.resolve())
        os.chmod(secret, 0o000)  # unreadable file; the parent dir stays searchable
        try:
            # A non-canonical source_path (redundant "/./") makes the resolved
            # absolute path a textually distinct substring, so a leak is detectable
            # even though the caller-supplied value is echoed back.
            supplied = f"{tmp_path}/./secret.html"
            client = FakeGCSClient()
            result = await publish_artifact(
                title="t",
                source_path=supplied,
                ctx=_ctx_for(client, source_root=str(tmp_path)),
            )
        finally:
            os.chmod(secret, 0o600)  # restore so tmp_path teardown can unlink it
        assert result["isError"] is True  # curated refusal, not a raised exception
        assert client.objects == {}  # nothing uploaded
        assert resolved not in result["error"]  # the resolved absolute path is not leaked
        assert resolved not in result.get("hint", "")

    @pytest.mark.asyncio
    async def test_source_path_when_is_file_raises_oserror_is_structured_refusal(
        self, tmp_path, monkeypatch
    ) -> None:
        # RF12 / adversarial ADV4-1: on CPython versions where Path.is_file()
        # re-raises a non-ignored OSError (EACCES on a no-search parent dir;
        # EIO / ESTALE on a stale mount) rather than returning False, the never-raise
        # gate would propagate it out of publish_artifact. The blanket OSError guard
        # must turn it into a curated refusal that does not echo the resolved
        # absolute path. Monkeypatched so the arm is exercised deterministically
        # regardless of the host interpreter's is_file() errno handling (mirrors the
        # RF11 Path.home monkeypatch above).
        src = tmp_path / "report.html"
        src.write_text("<h1>x</h1>", encoding="utf-8")
        resolved = str(src.resolve())

        def _raise_eacces(self: Path, *args: object, **kwargs: object) -> bool:
            raise PermissionError(13, "Permission denied", str(self))

        monkeypatch.setattr(Path, "is_file", _raise_eacces)
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t",
            source_path=f"{tmp_path}/./report.html",  # non-canonical: resolved != supplied
            ctx=_ctx_for(client, source_root=str(tmp_path)),
        )
        assert result["isError"] is True  # curated refusal, never a propagated exception
        assert client.objects == {}  # nothing uploaded
        assert resolved not in result["error"]  # PermissionError.filename not leaked

    @pytest.mark.asyncio
    async def test_inline_title_ending_in_version_token_is_html_not_bin(self) -> None:
        # RF3 at the tool level: a version-like title token must not mislabel inline
        # HTML as octet-stream / a .bin key (it would download instead of render).
        client = FakeGCSClient("my-bucket")
        result = await publish_artifact(
            title="Roadmap v1.0",
            content="<html><body>x</body></html>",
            ctx=_ctx_for(client),
        )
        assert result["content_type"] == "text/html"
        assert result["key"].endswith(".html")
        assert not result["key"].endswith(".bin")



class TestGrantAndRevokeAccess:
    """grant_access / revoke_access: idempotency, references, validation, errors."""

    @pytest.mark.asyncio
    async def test_grant_then_revoke_removes_reader(self) -> None:
        # Covers AE3: after revoke, the reader entry is gone (read back via ACL).
        client = FakeGCSClient()
        ctx = _ctx_for(client)
        key = (await publish_artifact(title="t", content="<p>x</p>", ctx=ctx))["key"]
        granted = await grant_access(key, ["alice@example.com", "bob@example.com"], ctx=ctx)
        assert all(g["ok"] for g in granted["grants"])
        assert await client.list_grantees(key) == ["alice@example.com", "bob@example.com"]
        revoked = await revoke_access(key, ["alice@example.com"], ctx=ctx)
        assert revoked["revocations"] == [{"email": "alice@example.com", "ok": True}]
        assert await client.list_grantees(key) == ["bob@example.com"]

    @pytest.mark.asyncio
    async def test_revoke_nongrantee_is_idempotent_noop(self) -> None:
        client = FakeGCSClient()
        ctx = _ctx_for(client)
        published = await publish_artifact(
            title="t", content="<p>x</p>", grant_emails=["bob@example.com"], ctx=ctx
        )
        key = published["key"]
        result = await revoke_access(key, ["nobody@example.com"], ctx=ctx)
        assert result["revocations"] == [{"email": "nobody@example.com", "ok": True}]
        assert await client.list_grantees(key) == ["bob@example.com"]

    @pytest.mark.asyncio
    async def test_regrant_existing_reader_is_single_entry(self) -> None:
        client = FakeGCSClient()
        ctx = _ctx_for(client)
        published = await publish_artifact(
            title="t", content="<p>x</p>", grant_emails=["alice@example.com"], ctx=ctx
        )
        key = published["key"]
        again = await grant_access(key, ["alice@example.com"], ctx=ctx)
        assert again["grants"] == [{"email": "alice@example.com", "ok": True}]
        assert await client.list_grantees(key) == ["alice@example.com"]

    @pytest.mark.asyncio
    async def test_malformed_email_skipped_valid_processed(self) -> None:
        # Malformed email -> per-email error, never sent to the API; the valid
        # email in the same call is still granted.
        client = FakeGCSClient()
        ctx = _ctx_for(client)
        key = (await publish_artifact(title="t", content="<p>x</p>", ctx=ctx))["key"]
        result = await grant_access(key, ["not-an-email", "alice@example.com"], ctx=ctx)
        grants = {g["email"]: g for g in result["grants"]}
        assert grants["alice@example.com"] == {"email": "alice@example.com", "ok": True}
        assert grants["not-an-email"]["ok"] is False
        assert "error" in grants["not-an-email"]
        assert await client.list_grantees(key) == ["alice@example.com"]

    @pytest.mark.asyncio
    async def test_grant_access_accepts_full_url_reference(self) -> None:
        # Covers KTD7: a full authenticated URL normalizes to the same object key.
        client = FakeGCSClient()
        ctx = _ctx_for(client)
        published = await publish_artifact(title="t", content="<p>x</p>", ctx=ctx)
        result = await grant_access(published["url"], ["alice@example.com"], ctx=ctx)
        assert result["key"] == published["key"]
        assert await client.list_grantees(published["key"]) == ["alice@example.com"]

    @pytest.mark.asyncio
    async def test_grant_access_on_ubla_bucket_returns_actionable_error(self) -> None:
        # Covers AE6 on the grant_access path.
        client = FakeGCSClient("ubla-bucket", ubla_on=True)
        await client.upload("2026/06/24/x.html", b"<p>x</p>", "text/html")
        result = await grant_access(
            "2026/06/24/x.html", ["alice@example.com"], ctx=_ctx_for(client)
        )
        assert result["isError"] is True
        assert "ubla-bucket" in result["error"]
        assert "gcloud storage buckets update" in result["error"]

    @pytest.mark.asyncio
    async def test_grant_access_auth_failure_returns_structured_error(self) -> None:
        # Covers R17's auth/permission class: a 401/403 from the grant maps to an
        # application-level isError dict via _handle_api_error, not a crash.
        client = FakeGCSClient("b", fail_auth=True)
        result = await grant_access(
            "2026/06/24/x.html", ["alice@example.com"], ctx=_ctx_for(client)
        )
        assert result["isError"] is True
        assert "Permission denied" in result["error"]



class TestWriteToolRegistration:
    """The three write tools register with the documented ToolAnnotations (R14)."""

    @pytest.mark.asyncio
    async def test_write_tools_present_with_annotations(self) -> None:
        tools = {t.name: t for t in await mcp.list_tools()}
        for name in ("publish_artifact", "grant_access", "revoke_access"):
            assert name in tools
            assert tools[name].description  # action-first description present (R15)

        publish_ann = tools["publish_artifact"].annotations
        grant_ann = tools["grant_access"].annotations
        revoke_ann = tools["revoke_access"].annotations
        assert publish_ann is not None
        assert grant_ann is not None
        assert revoke_ann is not None
        # publish: non-readonly, non-idempotent.
        assert publish_ann.readOnlyHint is False
        assert publish_ann.idempotentHint is False
        # grant: idempotent.
        assert grant_ann.idempotentHint is True
        # revoke: idempotent + destructive (R14).
        assert revoke_ann.idempotentHint is True
        assert revoke_ann.destructiveHint is True

    @pytest.mark.asyncio
    async def test_publish_parameter_schema_has_documented_fields(self) -> None:
        # R16: Pydantic Annotated/Field params surface in the tool input schema,
        # and the injected ctx is never exposed as a tool parameter.
        tools = {t.name: t for t in await mcp.list_tools()}
        props = tools["publish_artifact"].inputSchema["properties"]
        assert {"title", "content", "source_path", "content_type", "grant_emails"} <= set(props)
        assert "ctx" not in props


# ---------------------------------------------------------------------------
# U7 harness: the conftest two-tier fixtures drive the unit tier directly
# ---------------------------------------------------------------------------


class TestHarnessFixtures:
    """The shared conftest fixtures construct the AppContext with the fake and
    call tools directly (R18, unit tier). Class name avoids ``client`` so the U3
    ``-k client`` gate is unaffected."""

    def test_app_context_is_wired_to_the_fake(self, app_context: AppContext) -> None:
        assert isinstance(app_context.client, FakeGCSClient)
        assert app_context.config.bucket == "my-bucket"

    @pytest.mark.asyncio
    async def test_tool_context_fixture_calls_a_tool_directly(self, tool_context: Any) -> None:
        result = await list_artifacts(ctx=tool_context)
        assert result["total"] == 0
        assert result["items"] == []

    @pytest.mark.asyncio
    async def test_make_tool_ctx_builds_ctx_for_a_custom_fake(self, make_tool_ctx: Any) -> None:
        client = FakeGCSClient("custom-bucket")
        await client.upload("2026/06/24/a.html", b"<html>x</html>", "text/html")
        result = await list_artifacts(ctx=make_tool_ctx(client))
        assert result["total"] == 1
        assert result["items"][0]["url"].startswith(
            "https://storage.cloud.google.com/custom-bucket/"
        )
