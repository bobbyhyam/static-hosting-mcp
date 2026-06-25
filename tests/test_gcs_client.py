"""In-memory and injected-mock unit tests for the GCS client layer (U3).

These are the credential-free client-layer tests, split out of
``test_tools_unit.py`` (code-review RF10) so each module stays single-purpose
and under the size threshold. Every test here lives under a ``...Client...``
class, so ``pytest tests/test_gcs_client.py`` (or ``pytest -k client``) selects
exactly this set.

Two tiers are exercised:

- ``TestGCSClientUnit`` drives the in-memory :class:`FakeGCSClient` to cover the
  upload/metadata/list/delete/ACL behavior and the UBLA + auth branches that the
  fake makes reachable without a live bucket.
- ``TestGCSClientRealErrorMapping`` constructs the real :class:`GCSClient` with
  an injected mock ``storage.Client`` and asserts the real 400/401/403/404 ->
  typed-error mapping that the fake short-circuits (AE6 real path, plus the
  auth/not-found/startup branches).

``_real_client`` builds the real client over an injected mock; it is also
imported by ``test_tools_unit.py``, whose RF4 tool test drives the same
dependency-down mapping end-to-end.
"""

from __future__ import annotations

from typing import Any, cast
from unittest import mock

import pytest
import requests.exceptions
from google.api_core import exceptions as gexc

from static_hosting_mcp.gcs_client import (
    AuthError,
    GCSClient,
    GCSClientProtocol,
    GCSError,
    ObjectNotFoundError,
    StartupError,
    UBLAEnabledError,
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

    @pytest.mark.asyncio
    async def test_real_get_metadata_maps_connection_error_to_typed_gcserror(self) -> None:
        # RF4: a mid-session transport failure (requests.ConnectionError) is NOT a
        # GoogleAPICallError, so without the dependency-down arm it would escape the
        # wrapper uncaught. It must map to a typed, bucket-named GCSError.
        client, mc = _real_client("artifacts-bucket")
        mc.bucket.return_value.blob.return_value.reload.side_effect = (
            requests.exceptions.ConnectionError("Failed to establish a new connection")
        )
        with pytest.raises(GCSError) as excinfo:
            await client.get_metadata("2026/06/24/a.html")
        # Typed as the base GCSError (not a 404/auth/UBLA subclass) and actionable.
        assert type(excinfo.value) is GCSError
        assert "artifacts-bucket" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_real_grant_read_maps_auth_refresh_error_to_typed_gcserror(self) -> None:
        # RF4: a google.auth refresh failure on the ACL save (token endpoint down)
        # is likewise non-GoogleAPICallError and must surface as a typed GCSError.
        import google.auth.exceptions as gae

        client, mc = _real_client("artifacts-bucket")
        mc.bucket.return_value.blob.return_value.acl.save.side_effect = gae.RefreshError(
            "could not refresh access token"
        )
        with pytest.raises(GCSError) as excinfo:
            await client.grant_read("k", ["alice@example.com"])
        assert "artifacts-bucket" in str(excinfo.value)
