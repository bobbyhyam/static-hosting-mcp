"""In-memory unit tests for the GCS client layer (U3) and the write tools (U5).

These are the credential-free tests. The U3 client-layer tests live under
``...Client...`` classes so the U3 gate ``pytest tests/test_tools_unit.py -k
client`` selects exactly that set; the U5 tool tests added below deliberately
avoid ``client`` in their class names so they stay out of that gate while still
running under the default ``pytest tests/test_tools_unit.py`` invocation.

Tiers exercised:

- ``TestGCSClientUnit`` drives the in-memory :class:`FakeGCSClient` to cover the
  upload/metadata/list/delete/ACL behavior and the UBLA + auth branches that
  the fake makes reachable without a live bucket.
- ``TestGCSClientRealErrorMapping`` constructs the real :class:`GCSClient` with
  an injected mock ``storage.Client`` and asserts the real
  400/401/403/404 -> typed-error mapping that the fake short-circuits (AE6 real
  path, plus the auth/not-found/startup branches).
- ``TestPublishArtifact`` / ``TestGrantAndRevokeAccess`` /
  ``TestToolRegistration`` (U5) call the write tools directly with an
  ``AppContext`` wired to the fake, exercising the publish XOR, the date-foldered
  key + authenticated URL, per-object grant/revoke, client-side email
  validation, and the UBLA / auth error mapping (AE1, AE2, AE3, AE6, AE7, R17).
"""

from __future__ import annotations

import re
from typing import Any, cast
from unittest import mock

import pytest
from google.api_core import exceptions as gexc
from mcp.server.fastmcp import Context

from static_hosting_mcp.config import Config
from static_hosting_mcp.gcs_client import (
    AuthError,
    GCSClient,
    GCSClientProtocol,
    ObjectNotFoundError,
    StartupError,
    UBLAEnabledError,
)
from static_hosting_mcp.server import (
    AppContext,
    grant_access,
    mcp,
    publish_artifact,
    revoke_access,
)

from .fakes import FakeGCSClient


def _ctx_for(client: FakeGCSClient) -> Context:
    """Build a Context whose lifespan ``AppContext`` carries the in-memory fake.

    The tools only ever reach ``ctx.request_context.lifespan_context.client``, so
    a tiny duck-typed stand-in is enough; it is cast to :class:`Context` to match
    the tool signatures (the real ``Context`` is injected by FastMCP at runtime).
    """

    class _FakeRequestContext:
        def __init__(self, app: AppContext) -> None:
            self.lifespan_context = app

    class _FakeContext:
        def __init__(self, app: AppContext) -> None:
            self.request_context = _FakeRequestContext(app)

    config = Config(bucket=client.bucket_name, key_path="/abs/key.json")
    return cast(Context, _FakeContext(AppContext(client=client, config=config)))


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
        clients: list[GCSClientProtocol] = [
            FakeGCSClient("my-bucket"),
            _real_client("my-bucket")[0],
        ]
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
# U5 write-tool tests: publish_artifact, grant_access, revoke_access
# ---------------------------------------------------------------------------

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
        # source_path to a local file -> uploaded with the inferred content-type.
        src = tmp_path / "notes.md"
        src.write_text("# Heading\n", encoding="utf-8")
        client = FakeGCSClient()
        result = await publish_artifact(
            title="My notes", source_path=str(src), ctx=_ctx_for(client)
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
    async def test_unreadable_source_path_is_structured_error(self) -> None:
        # A missing file surfaces a structured error, not a crash (R17 / KTD11).
        client = FakeGCSClient()
        result = await publish_artifact(
            title="t", source_path="/no/such/file/here.txt", ctx=_ctx_for(client)
        )
        assert result["isError"] is True
        assert "source_path" in result["error"]
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
    async def test_publish_on_ubla_bucket_returns_actionable_error(self) -> None:
        # Covers AE6: UBLA-on -> the grant returns an actionable error naming the
        # bucket + the disable command (the object itself was still uploaded).
        client = FakeGCSClient("ubla-bucket", ubla_on=True)
        result = await publish_artifact(
            title="t",
            content="<html></html>",
            grant_emails=["alice@example.com"],
            ctx=_ctx_for(client),
        )
        assert result["isError"] is True
        assert "ubla-bucket" in result["error"]
        assert "uniform bucket-level access" in result["error"].lower()
        assert "gcloud storage buckets update" in result["error"]
        assert len(client.objects) == 1  # upload happened; only the grant failed


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


class TestToolRegistration:
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
