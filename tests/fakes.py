"""In-memory fakes for credential-free tests (U3, shared by U5/U6/U7).

``FakeGCSClient`` mirrors the async method surface of
:class:`static_hosting_mcp.gcs_client.GCSClient` (and therefore
:class:`~static_hosting_mcp.gcs_client.GCSClientProtocol`) backed by plain
dicts, so the tool layer can be tested without a live bucket or any
credentials. Two injectable flags make the otherwise live-only branches
reachable in the unit tier:

- ``ubla_on`` makes every ACL change raise :class:`UBLAEnabledError` — the
  fake's stand-in for the HTTP 400 a UBLA-enabled bucket returns (AE6).
- ``fail_auth`` makes ACL and upload calls raise :class:`AuthError` — the
  stand-in for a 401/403 auth-permission failure (R17's auth error class).

The URL helpers delegate to the same module functions the real client uses, so
the real and fake clients cannot drift on URL shape.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from static_hosting_mcp.gcs_client import (
    AuthError,
    GCSClientProtocol,
    ObjectNotFoundError,
    StartupError,
    UBLAEnabledError,
    build_authenticated_url,
    normalize_object_ref,
)


class FakeGCSClient:
    """A dict-backed stand-in for :class:`GCSClient` with the same async surface."""

    def __init__(
        self,
        bucket: str = "fake-bucket",
        *,
        ubla_on: bool = False,
        fail_auth: bool = False,
        reachable: bool = True,
    ) -> None:
        self._bucket_name = bucket
        self.ubla_on = ubla_on
        self.fail_auth = fail_auth
        self.reachable = reachable
        # objects[key] = (data, content_type, created_iso, size)
        self.objects: dict[str, tuple[bytes, str, str, int]] = {}
        # acls[key] = set of reader emails
        self.acls: dict[str, set[str]] = {}

    @property
    def bucket_name(self) -> str:
        return self._bucket_name

    # -- pure URL helpers (delegate to the real module's single source) -----

    def authenticated_url(self, key: str) -> str:
        return build_authenticated_url(self._bucket_name, key)

    def normalize_ref(self, ref: str) -> str:
        return normalize_object_ref(self._bucket_name, ref)

    # -- startup probe ------------------------------------------------------

    async def check_reachable(self) -> None:
        await asyncio.sleep(0)
        if not self.reachable:
            raise StartupError(
                f"Could not reach fake bucket '{self._bucket_name}' (reachable=False)."
            )

    # -- object operations --------------------------------------------------

    async def upload(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.sleep(0)
        self._maybe_auth_fail()
        created = datetime.now(UTC).isoformat()
        self.objects[key] = (data, content_type, created, len(data))
        self.acls.setdefault(key, set())

    async def get_metadata(self, key: str) -> dict[str, Any]:
        await asyncio.sleep(0)
        if key not in self.objects:
            raise ObjectNotFoundError(self._not_found(key), key=key)
        _data, content_type, created, size = self.objects[key]
        return {"key": key, "size": size, "content_type": content_type, "created": created}

    async def list_objects(
        self, prefix: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        await asyncio.sleep(0)
        keys = sorted(self.objects)
        if prefix:
            keys = [k for k in keys if k.startswith(prefix)]
        out: list[dict[str, Any]] = []
        for key in keys[:limit]:
            _data, content_type, created, size = self.objects[key]
            out.append(
                {"key": key, "size": size, "content_type": content_type, "created": created}
            )
        return out

    async def delete(self, key: str) -> None:
        await asyncio.sleep(0)
        if key not in self.objects:
            raise ObjectNotFoundError(self._not_found(key), key=key)
        del self.objects[key]
        self.acls.pop(key, None)

    async def exists(self, key: str) -> bool:
        await asyncio.sleep(0)
        return key in self.objects

    # -- ACL operations -----------------------------------------------------

    async def grant_read(self, key: str, emails: Sequence[str]) -> list[dict[str, Any]]:
        return await self._apply_acl(key, emails, grant=True)

    async def revoke_read(self, key: str, emails: Sequence[str]) -> list[dict[str, Any]]:
        return await self._apply_acl(key, emails, grant=False)

    async def list_grantees(self, key: str) -> list[str]:
        await asyncio.sleep(0)
        return sorted(self.acls.get(key, set()))

    async def _apply_acl(
        self, key: str, emails: Sequence[str], *, grant: bool
    ) -> list[dict[str, Any]]:
        await asyncio.sleep(0)
        self._maybe_auth_fail()
        # Stand-in for a UBLA-on bucket: the ACL save is rejected (HTTP 400).
        if self.ubla_on:
            raise UBLAEnabledError(
                f"UBLA is enabled on fake bucket '{self._bucket_name}'; per-object ACLs "
                "are disabled.",
                bucket=self._bucket_name,
            )
        email_list = [emails] if isinstance(emails, str) else list(emails)
        readers = self.acls.setdefault(key, set())
        for email in email_list:
            if grant:
                readers.add(email)  # idempotent: a set keeps one entry per email
            else:
                readers.discard(email)  # idempotent: revoking a non-grantee is a no-op
        verb = "granted" if grant else "revoked"
        return [{"email": email, "ok": True, "status": verb} for email in email_list]

    # -- helpers ------------------------------------------------------------

    def _maybe_auth_fail(self) -> None:
        if self.fail_auth:
            raise AuthError(
                f"Permission denied (HTTP 403) on fake bucket '{self._bucket_name}'.",
                status=403,
            )

    def _not_found(self, key: str) -> str:
        return f"No such artifact: object '{key}' was not found in bucket '{self._bucket_name}'."


if TYPE_CHECKING:
    # mypy-only conformance gate: keeps FakeGCSClient and the real GCSClient from
    # drifting in signature or return shape relative to GCSClientProtocol.
    def _assert_fakegcsclient_implements_protocol(client: FakeGCSClient) -> GCSClientProtocol:
        return client
