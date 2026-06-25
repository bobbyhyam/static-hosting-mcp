"""In-memory unit tests for the GCS client layer (U3).

These are the credential-free client-layer tests; later units add tool tests to
this same module. Every test here lives under a ``...Client...`` class so the
U3 gate ``pytest tests/test_tools_unit.py -k client`` selects exactly this set.

Two tiers are exercised:

- ``TestGCSClientUnit`` drives the in-memory :class:`FakeGCSClient` to cover the
  upload/metadata/list/delete/ACL behavior and the UBLA + auth branches that
  the fake makes reachable without a live bucket.
- ``TestGCSClientRealErrorMapping`` constructs the real :class:`GCSClient` with
  an injected mock ``storage.Client`` and asserts the real
  400/401/403/404 -> typed-error mapping that the fake short-circuits (AE6 real
  path, plus the auth/not-found/startup branches).

U6 adds the tool-layer tests below, all credential-free against the fake:
``TestArtifactInspectionTools`` drives ``list_artifacts`` / ``get_artifact`` /
``delete_artifact`` (AE4, AE5; R6 read side, R7-R9), ``TestToolRegistration``
checks the FastMCP registration, annotations, and parameter schemas (R14-R16),
and ``TestHandleApiError`` covers the typed-error -> curated-dict mapping (R17).
The full MCP round-trip (arg parsing + Context injection) is exercised by the
live suite (U7); here each tool is called directly with a stand-in Context.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

from static_hosting_mcp.config import Config
from static_hosting_mcp.gcs_client import (
    AuthError,
    GCSClient,
    GCSClientProtocol,
    GCSError,
    ObjectNotFoundError,
    StartupError,
    UBLAEnabledError,
)
from static_hosting_mcp.server import (
    AppContext,
    _handle_api_error,
    delete_artifact,
    get_artifact,
    list_artifacts,
    mcp,
)

from .fakes import FakeGCSClient


def _real_client(bucket: str = "test-bucket") -> tuple[GCSClient, mock.MagicMock]:
    """Build a real GCSClient over an injected mock storage.Client.

    Returns the client plus the mock so a test can configure the blob/ACL chain
    (e.g. make ``acl.save`` raise) and exercise the real SDK-error mapping with
    no credentials.
    """
    mc = mock.MagicMock()
    client = GCSClient(bucket, client=cast(Any, mc))
    return client, mc


class TestGCSClientUnit:
    """Behavioral tests against the in-memory FakeGCSClient."""

    # -- URL helpers (pure, shared real/fake source) ------------------------

    def test_authenticated_url_shape_real_and_fake(self) -> None:
        # Covers AE1: the exact storage.cloud.google.com/<bucket>/<key> form.
        key = "2026/06/24/q2-tariff-deep-research-7f3a9c.html"
        expected = f"https://storage.cloud.google.com/my-bucket/{key}"
        fake = FakeGCSClient("my-bucket")
        real, _ = _real_client("my-bucket")
        assert fake.authenticated_url(key) == expected
        assert real.authenticated_url(key) == expected

    def test_normalize_ref_roundtrip_real_and_fake(self) -> None:
        # Covers KTD7: normalize_ref is the inverse of authenticated_url and the
        # prefix string lives in exactly one place (shared by real + fake).
        key = "2026/06/24/x.html"
        clients: list[GCSClientProtocol] = [FakeGCSClient("my-bucket"), _real_client("my-bucket")[0]]
        for client in clients:
            url = client.authenticated_url(key)
            assert client.normalize_ref(url) == key  # full URL -> key
            assert client.normalize_ref(key) == key  # bare key passes through
            # A URL for a *different* bucket is not this bucket's object: left as-is.
            other = f"https://storage.cloud.google.com/other-bucket/{key}"
            assert client.normalize_ref(other) == other

    def test_client_implementations_satisfy_protocol_runtime(self) -> None:
        # Secondary, runtime guard; mypy is the authoritative signature check.
        assert isinstance(FakeGCSClient(), GCSClientProtocol)
        assert isinstance(_real_client()[0], GCSClientProtocol)

    # -- upload / metadata --------------------------------------------------

    @pytest.mark.asyncio
    async def test_client_upload_then_get_metadata(self) -> None:
        client = FakeGCSClient()
        body = b"<html>hello</html>"
        await client.upload("2026/06/24/x.html", body, "text/html")
        meta = await client.get_metadata("2026/06/24/x.html")
        assert meta["content_type"] == "text/html"
        assert meta["size"] == len(body)
        assert meta["size"] > 0
        assert meta["key"] == "2026/06/24/x.html"
        assert meta["created"]  # present and non-empty

    @pytest.mark.asyncio
    async def test_client_get_metadata_missing_raises(self) -> None:
        client = FakeGCSClient()
        with pytest.raises(ObjectNotFoundError):
            await client.get_metadata("nope/missing.html")

    # -- grant / revoke / list_grantees -------------------------------------

    @pytest.mark.asyncio
    async def test_client_grant_single_email_adds_reader(self) -> None:
        client = FakeGCSClient()
        await client.upload("k", b"x", "text/html")
        result = await client.grant_read("k", ["alice@example.com"])
        assert result == [{"email": "alice@example.com", "ok": True, "status": "granted"}]
        assert await client.list_grantees("k") == ["alice@example.com"]

    @pytest.mark.asyncio
    async def test_client_grant_three_emails_single_call_adds_all(self) -> None:
        # All three are added in one reload->save and a per-email ok is returned.
        client = FakeGCSClient()
        await client.upload("k", b"x", "text/html")
        emails = ["a@example.com", "b@example.com", "c@example.com"]
        result = await client.grant_read("k", emails)
        assert [r["email"] for r in result] == emails
        assert all(r["ok"] for r in result)
        assert await client.list_grantees("k") == sorted(emails)

    @pytest.mark.asyncio
    async def test_client_grant_is_idempotent(self) -> None:
        client = FakeGCSClient()
        await client.upload("k", b"x", "text/html")
        await client.grant_read("k", ["alice@example.com"])
        await client.grant_read("k", ["alice@example.com"])  # re-grant
        assert await client.list_grantees("k") == ["alice@example.com"]

    @pytest.mark.asyncio
    async def test_client_revoke_removes_and_nonmember_is_noop(self) -> None:
        client = FakeGCSClient()
        await client.upload("k", b"x", "text/html")
        await client.grant_read("k", ["alice@example.com", "bob@example.com"])
        await client.revoke_read("k", ["alice@example.com"])
        assert await client.list_grantees("k") == ["bob@example.com"]
        # Revoking a non-grantee is an idempotent no-op (still ok per email).
        result = await client.revoke_read("k", ["nobody@example.com"])
        assert result == [{"email": "nobody@example.com", "ok": True, "status": "revoked"}]
        assert await client.list_grantees("k") == ["bob@example.com"]

    # -- list_objects -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_client_list_objects_honors_prefix_and_limit(self) -> None:
        client = FakeGCSClient()
        keys = ["2026/06/24/a.html", "2026/06/24/b.html", "2026/06/25/c.html"]
        for key in keys:
            await client.upload(key, b"x", "text/html")
        assert {o["key"] for o in await client.list_objects()} == set(keys)
        june24 = await client.list_objects(prefix="2026/06/24/")
        assert {o["key"] for o in june24} == {"2026/06/24/a.html", "2026/06/24/b.html"}
        assert len(await client.list_objects(limit=1)) == 1

    # -- delete / exists ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_client_delete_removes_then_exists_false_and_missing_raises(self) -> None:
        client = FakeGCSClient()
        await client.upload("k", b"x", "text/html")
        assert await client.exists("k") is True
        await client.delete("k")
        assert await client.exists("k") is False
        with pytest.raises(ObjectNotFoundError):
            await client.delete("k")  # second delete -> not found

    # -- startup probe ------------------------------------------------------

    @pytest.mark.asyncio
    async def test_client_check_reachable_ok_and_failure(self) -> None:
        await FakeGCSClient(reachable=True).check_reachable()  # no raise
        with pytest.raises(StartupError):
            await FakeGCSClient(reachable=False).check_reachable()

    # -- UBLA + auth branches (fake stand-ins) ------------------------------

    @pytest.mark.asyncio
    async def test_client_ubla_on_grant_raises_ubla_error(self) -> None:
        # Covers AE6 at the client layer: the fake's ubla_on stands in for a 400.
        client = FakeGCSClient(ubla_on=True)
        await client.upload("k", b"x", "text/html")  # upload itself is unaffected
        with pytest.raises(UBLAEnabledError):
            await client.grant_read("k", ["alice@example.com"])

    @pytest.mark.asyncio
    async def test_client_fake_auth_failure_surfaces_auth_error(self) -> None:
        # Covers R17's auth/permission class at the client layer, credential-free.
        client = FakeGCSClient(fail_auth=True)
        with pytest.raises(AuthError):
            await client.upload("k", b"x", "text/html")
        with pytest.raises(AuthError):
            await client.grant_read("k", ["alice@example.com"])


class TestGCSClientRealErrorMapping:
    """The real GCSClient's SDK-error -> typed-error mapping, credential-free."""

    @pytest.mark.asyncio
    async def test_real_grant_read_maps_any_400_to_ubla(self) -> None:
        # Covers AE6 real path: any 400 from the ACL save is UBLA-most-likely,
        # and the mapping does NOT gate on the message text.
        client, mc = _real_client()
        mc.bucket.return_value.blob.return_value.acl.save.side_effect = gexc.BadRequest(
            "Bad Request"
        )
        with pytest.raises(UBLAEnabledError) as excinfo:
            await client.grant_read("k", ["alice@example.com"])
        assert "test-bucket" in str(excinfo.value)
        assert "API said" not in str(excinfo.value)  # not gated on message text

    @pytest.mark.asyncio
    async def test_real_grant_read_strengthens_wording_on_ubla_message(self) -> None:
        client, mc = _real_client()
        mc.bucket.return_value.blob.return_value.acl.save.side_effect = gexc.BadRequest(
            "Cannot insert legacy ACL for an object when uniform bucket-level access is enabled."
        )
        with pytest.raises(UBLAEnabledError) as excinfo:
            await client.grant_read("k", ["alice@example.com"])
        assert "API said" in str(excinfo.value)  # message only strengthens wording

    @pytest.mark.asyncio
    async def test_real_grant_read_maps_403_to_auth_error(self) -> None:
        client, mc = _real_client()
        mc.bucket.return_value.blob.return_value.acl.save.side_effect = gexc.Forbidden(
            "caller lacks storage.objects.update"
        )
        with pytest.raises(AuthError) as excinfo:
            await client.grant_read("k", ["alice@example.com"])
        assert excinfo.value.status == 403

    @pytest.mark.asyncio
    async def test_real_delete_maps_404_to_object_not_found(self) -> None:
        client, mc = _real_client()
        mc.bucket.return_value.delete_blob.side_effect = gexc.NotFound("No such object")
        with pytest.raises(ObjectNotFoundError):
            await client.delete("missing/object.html")

    @pytest.mark.asyncio
    async def test_real_check_reachable_maps_failure_to_startup_error(self) -> None:
        client, mc = _real_client()
        mc.list_blobs.side_effect = gexc.Forbidden("caller lacks storage.objects.list")
        with pytest.raises(StartupError):
            await client.check_reachable()


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


class TestToolRegistration:
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
