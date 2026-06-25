"""Async GCS wrapper, typed errors, and a startup reachability probe (U3).

This module owns the Google Cloud Storage SDK the way ``notion_client.py`` owns
httpx in the reference server: the wrapper is the *only* place that touches
``google.cloud.storage`` and ``google.api_core.exceptions``, and it surfaces a
small set of typed errors (:class:`StartupError`, :class:`ObjectNotFoundError`,
:class:`AuthError`, :class:`UBLAEnabledError`) so the tool layer (U5/U6) can map
SDK failures to curated error dicts with ``isinstance`` checks instead of
re-deriving HTTP status handling.

Design notes (from the approved plan, KTD3/KTD4/KTD7/KTD10):

- Every blocking SDK call runs through :func:`asyncio.to_thread` so the stdio
  event loop is never blocked. The public method surface is therefore async
  (except the two pure URL helpers).
- Multi-email ACL changes are a **single batched read-modify-write per object**
  (``reload`` -> apply every ``user-<email>`` mutation in memory -> ``save``
  once), never concurrent per-email saves on the same blob. Each ACL op also
  takes a **fresh** ``bucket.blob(key)`` handle rather than mutating a shared
  ``Blob`` across threads.
- There is **no dedicated UBLA exception type** in the SDK: a bucket with
  Uniform Bucket-Level Access enabled rejects an ACL ``save()`` with a generic
  ``BadRequest`` / HTTP 400. We treat *any* 400 from an ACL grant/save as
  UBLA-most-likely and raise :class:`UBLAEnabledError`; a message substring is
  used only to *strengthen* the wording, never to *gate* the mapping.
- ``authenticated_url`` and its inverse ``normalize_ref`` are co-located here so
  the ``storage.cloud.google.com`` prefix string lives in exactly one place
  (:data:`AUTHENTICATED_URL_PREFIX`); ``FakeGCSClient`` reuses the same module
  helpers so the real and fake clients cannot drift on URL shape.

:class:`GCSClientProtocol` declares the async method surface; both
:class:`GCSClient` (here) and ``FakeGCSClient`` (in ``tests/fakes.py``) implement
it, and a ``TYPE_CHECKING`` conformance assertion in each file makes ``mypy``
enforce signature/return-shape parity at type-check time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import google.auth.exceptions
import requests.exceptions
from google.api_core import exceptions as gexc
from google.cloud import storage

# Transport/retry/auth failures that mean "the storage backend is unreachable" but
# are NOT ``GoogleAPICallError`` subclasses, so the per-operation ``except
# gexc.GoogleAPICallError`` arm misses them: ``RetryError`` (the SDK exhausted its
# retries), ``requests`` transport errors (network/DNS/TLS), and ``google.auth``
# errors (token refresh / auth endpoint down). Mapping these to a typed
# :class:`GCSError` keeps the curated, never-crash error contract (R17/KTD11) in
# the canonical mid-session "dependency is down" case, not just at startup (RF4).
_DEPENDENCY_DOWN_ERRORS = (
    gexc.RetryError,
    requests.exceptions.RequestException,
    google.auth.exceptions.GoogleAuthError,
)

# The single source of truth for the user-facing authenticated-URL prefix
# (KTD3/KTD7). ``build_authenticated_url`` and ``normalize_object_ref`` are the
# only code that interpolates or strips it; the real and fake clients delegate
# here so the prefix lives in exactly one place.
AUTHENTICATED_URL_PREFIX = "https://storage.cloud.google.com/"

# ACL reader role and the entity prefix GCS uses for a Google-account grantee.
_READER_ROLE = "READER"
_USER_ENTITY_PREFIX = "user-"


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class GCSError(Exception):
    """Base class for every typed error surfaced by :class:`GCSClient`.

    The tool layer catches the specific subclasses below; catching
    :class:`GCSError` is the catch-all for "the storage wrapper failed in a way
    it understood".
    """


class StartupError(GCSError):
    """The startup reachability probe failed (bad bucket/credentials/role).

    Carries an actionable message naming the bucket so the lifespan (U2) can
    print it to stderr and exit instead of surfacing a raw SDK traceback.
    """


class ObjectNotFoundError(GCSError):
    """An operation targeted an object that does not exist (HTTP 404)."""

    def __init__(self, message: str, *, key: str | None = None) -> None:
        super().__init__(message)
        self.key = key


class AuthError(GCSError):
    """Authentication/permission failure (HTTP 401/403).

    Raised when the service account cannot perform the requested operation —
    e.g. it lacks ``roles/storage.objectAdmin`` on the bucket.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UBLAEnabledError(GCSError):
    """A per-object ACL grant/save failed with HTTP 400.

    Uniform Bucket-Level Access disables per-object ACLs and there is no
    dedicated SDK exception for it, so any 400 from an ACL save is mapped here
    with an actionable "disable UBLA" message naming the bucket.
    """

    def __init__(self, message: str, *, bucket: str | None = None) -> None:
        super().__init__(message)
        self.bucket = bucket


# ---------------------------------------------------------------------------
# Pure URL helpers (the only place the authenticated-URL prefix is interpolated)
# ---------------------------------------------------------------------------


def build_authenticated_url(bucket: str, key: str) -> str:
    """Return the permanent authenticated URL for ``key`` in ``bucket``."""
    return f"{AUTHENTICATED_URL_PREFIX}{bucket}/{key}"


def normalize_object_ref(bucket: str, ref: str) -> str:
    """Strip the authenticated-URL prefix from ``ref`` when present (KTD7).

    Accepts either a bare object key or a full
    ``https://storage.cloud.google.com/<bucket>/<key>`` URL and returns the
    object key. A reference that is already a bare key passes through unchanged.
    """
    prefix = f"{AUTHENTICATED_URL_PREFIX}{bucket}/"
    if ref.startswith(prefix):
        return ref[len(prefix) :]
    return ref


def reader_emails_from_acl(acl: Iterable[Any]) -> list[str]:
    """Extract the reader grantee emails from an iterable GCS ACL.

    Iterating a ``google.cloud.storage`` ACL yields ``{"entity", "role"}`` dicts;
    a Google-account reader is ``{"entity": "user-<email>", "role": "READER"}``.
    Returns the emails (without the ``user-`` prefix), unsorted.
    """
    emails: list[str] = []
    for entry in acl:
        if not isinstance(entry, dict):
            continue
        if entry.get("role") != _READER_ROLE:
            continue
        entity = str(entry.get("entity", ""))
        if entity.startswith(_USER_ENTITY_PREFIX):
            emails.append(entity[len(_USER_ENTITY_PREFIX) :])
    return emails


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class GCSClientProtocol(Protocol):
    """The async method surface every GCS client (real or fake) must provide.

    Declared as a ``typing.Protocol`` so ``mypy`` enforces that both
    :class:`GCSClient` and ``FakeGCSClient`` match the signatures *and* return
    shapes — not merely the method names — at type-check time.
    """

    def authenticated_url(self, key: str) -> str: ...

    def normalize_ref(self, ref: str) -> str: ...

    async def check_reachable(self) -> None: ...

    async def upload(self, key: str, data: bytes, content_type: str) -> None: ...

    async def grant_read(self, key: str, emails: Sequence[str]) -> list[dict[str, Any]]: ...

    async def revoke_read(self, key: str, emails: Sequence[str]) -> list[dict[str, Any]]: ...

    async def list_grantees(self, key: str) -> list[str]: ...

    async def list_objects(
        self, prefix: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]: ...

    async def get_metadata(self, key: str) -> dict[str, Any]: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...


# ---------------------------------------------------------------------------
# Real client
# ---------------------------------------------------------------------------


class GCSClient:
    """Async wrapper over ``google.cloud.storage`` for one bucket.

    Construct with a service-account key path (the portable default for a stdio
    server) or fall back to Application Default Credentials when no key path is
    given. A pre-built ``storage.Client`` may be injected via ``client`` — this
    is the documented seam used by the credential-free unit tests that exercise
    the real 400/401/403/404 -> typed-error mapping.
    """

    def __init__(
        self,
        bucket: str,
        *,
        key_path: str | None = None,
        project: str | None = None,
        client: storage.Client | None = None,
    ) -> None:
        self._bucket_name = bucket
        if client is not None:
            self._client = client
        elif key_path:
            self._client = storage.Client.from_service_account_json(key_path, project=project)
        else:
            # Documented ADC fallback (KTD5) when org policy blocks key creation.
            self._client = storage.Client(project=project)
        self._bucket = self._client.bucket(bucket)

    @property
    def bucket_name(self) -> str:
        """The attached bucket name (safe to surface; not a secret)."""
        return self._bucket_name

    # -- pure URL helpers (no I/O) -----------------------------------------

    def authenticated_url(self, key: str) -> str:
        return build_authenticated_url(self._bucket_name, key)

    def normalize_ref(self, ref: str) -> str:
        return normalize_object_ref(self._bucket_name, ref)

    # -- startup probe ------------------------------------------------------

    async def check_reachable(self) -> None:
        """List one object to prove credentials load and the bucket is reachable.

        Uses a 1-object list (which ``roles/storage.objectAdmin`` permits and
        which needs no ``storage.buckets.get``); raises :class:`StartupError`
        with an actionable message on any failure (R12).
        """

        def _do() -> None:
            try:
                iterator = self._client.list_blobs(self._bucket_name, max_results=1)
                for _ in iterator:
                    break
            except Exception as exc:  # noqa: BLE001 - fail-fast with one actionable line
                raise StartupError(self._startup_message(exc)) from exc

        await asyncio.to_thread(_do)

    # -- object operations --------------------------------------------------

    async def upload(self, key: str, data: bytes, content_type: str) -> None:
        """Upload ``data`` to ``key`` with the given content-type (R1)."""

        def _do() -> None:
            blob = self._bucket.blob(key)
            try:
                blob.upload_from_string(data, content_type=content_type)
            except gexc.GoogleAPICallError as exc:
                raise self._map_error(exc, key=key) from exc
            except _DEPENDENCY_DOWN_ERRORS as exc:
                raise GCSError(self._unreachable_message(exc)) from exc

        await asyncio.to_thread(_do)

    async def get_metadata(self, key: str) -> dict[str, Any]:
        """Return ``{key, size, content_type, created}`` for ``key`` (R8).

        Raises :class:`ObjectNotFoundError` when the object does not exist.
        """

        def _do() -> dict[str, Any]:
            blob = self._bucket.blob(key)
            try:
                blob.reload()
            except gexc.NotFound as exc:
                raise ObjectNotFoundError(self._not_found_message(key), key=key) from exc
            except gexc.GoogleAPICallError as exc:
                raise self._map_error(exc, key=key) from exc
            except _DEPENDENCY_DOWN_ERRORS as exc:
                raise GCSError(self._unreachable_message(exc)) from exc
            return self._metadata_dict(key, blob.size, blob.content_type, blob.time_created)

        return await asyncio.to_thread(_do)

    async def list_objects(
        self, prefix: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List up to ``limit`` objects, optionally under ``prefix`` (R7).

        Each entry is ``{key, size, content_type, created}``. ``list_blobs``
        returns object metadata inline, so no per-object reload is needed.
        """

        def _do() -> list[dict[str, Any]]:
            try:
                blobs = self._client.list_blobs(
                    self._bucket_name, prefix=prefix or None, max_results=limit
                )
                return [
                    self._metadata_dict(b.name, b.size, b.content_type, b.time_created)
                    for b in blobs
                ]
            except gexc.GoogleAPICallError as exc:
                raise self._map_error(exc) from exc
            except _DEPENDENCY_DOWN_ERRORS as exc:
                raise GCSError(self._unreachable_message(exc)) from exc

        return await asyncio.to_thread(_do)

    async def delete(self, key: str) -> None:
        """Delete ``key`` directly, mapping 404 to :class:`ObjectNotFoundError` (R9).

        No exists-precheck: the delete is issued straight away (avoiding a
        redundant round-trip and a TOCTOU window) and a not-found is translated
        to an actionable typed error.
        """

        def _do() -> None:
            try:
                self._bucket.delete_blob(key)
            except gexc.NotFound as exc:
                raise ObjectNotFoundError(self._not_found_message(key), key=key) from exc
            except gexc.GoogleAPICallError as exc:
                raise self._map_error(exc, key=key) from exc
            except _DEPENDENCY_DOWN_ERRORS as exc:
                raise GCSError(self._unreachable_message(exc)) from exc

        await asyncio.to_thread(_do)

    async def exists(self, key: str) -> bool:
        """Return whether ``key`` currently resolves to an object."""

        def _do() -> bool:
            try:
                return bool(self._bucket.blob(key).exists())
            except gexc.GoogleAPICallError as exc:
                raise self._map_error(exc, key=key) from exc
            except _DEPENDENCY_DOWN_ERRORS as exc:
                raise GCSError(self._unreachable_message(exc)) from exc

        return await asyncio.to_thread(_do)

    # -- ACL operations -----------------------------------------------------

    async def grant_read(self, key: str, emails: Sequence[str]) -> list[dict[str, Any]]:
        """Grant per-object read to every email in one read-modify-write (R4/R5).

        A single ``reload`` -> apply all ``user-<email>`` grants in memory ->
        ``save`` once. Idempotent (re-granting an existing reader is a no-op).
        Returns a per-email result list. Maps an HTTP 400 from the save to
        :class:`UBLAEnabledError` (UBLA-most-likely, AE6).
        """
        return await self._apply_acl(key, emails, grant=True)

    async def revoke_read(self, key: str, emails: Sequence[str]) -> list[dict[str, Any]]:
        """Revoke per-object read for every email in one read-modify-write (R5).

        Same single-save batching as :meth:`grant_read`; revoking a
        non-grantee is an idempotent no-op. Returns a per-email result list.
        """
        return await self._apply_acl(key, emails, grant=False)

    async def list_grantees(self, key: str) -> list[str]:
        """Return the current reader grantee emails for ``key``, sorted (R6)."""

        def _do() -> list[str]:
            blob = self._bucket.blob(key)
            try:
                blob.acl.reload()
            except gexc.NotFound as exc:
                raise ObjectNotFoundError(self._not_found_message(key), key=key) from exc
            except gexc.GoogleAPICallError as exc:
                raise self._map_error(exc, key=key) from exc
            except _DEPENDENCY_DOWN_ERRORS as exc:
                raise GCSError(self._unreachable_message(exc)) from exc
            return sorted(reader_emails_from_acl(blob.acl))

        return await asyncio.to_thread(_do)

    async def _apply_acl(
        self, key: str, emails: Sequence[str], *, grant: bool
    ) -> list[dict[str, Any]]:
        # ``str`` is itself a ``Sequence[str]`` (of characters); coerce a lone
        # email so a caller passing a bare string can't silently iterate chars.
        email_list = [emails] if isinstance(emails, str) else list(emails)

        def _do() -> list[dict[str, Any]]:
            blob = self._bucket.blob(key)  # fresh handle per call (KTD10)
            acl = blob.acl
            try:
                acl.reload()
                for email in email_list:
                    entity = acl.user(email)
                    if grant:
                        entity.grant_read()
                    else:
                        entity.revoke_read()
                acl.save()
            except gexc.GoogleAPICallError as exc:
                raise self._map_error(exc, key=key, acl=True) from exc
            except _DEPENDENCY_DOWN_ERRORS as exc:
                raise GCSError(self._unreachable_message(exc)) from exc
            verb = "granted" if grant else "revoked"
            results: list[dict[str, Any]] = [
                {"email": email, "ok": True, "status": verb} for email in email_list
            ]
            return results

        return await asyncio.to_thread(_do)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _metadata_dict(
        key: str, size: Any, content_type: Any, created: Any
    ) -> dict[str, Any]:
        return {
            "key": key,
            "size": size,
            "content_type": content_type,
            "created": created.isoformat() if created is not None else None,
        }

    def _map_error(
        self, exc: gexc.GoogleAPICallError, *, key: str | None = None, acl: bool = False
    ) -> GCSError:
        """Translate a Google API call error into a typed :class:`GCSError`.

        ``acl=True`` enables the "any 400 is UBLA-most-likely" mapping for ACL
        saves; outside an ACL context a 400 stays a generic error.
        """
        status = getattr(exc, "code", None)
        status = status if isinstance(status, int) else None
        if status == 404:
            return ObjectNotFoundError(self._not_found_message(key), key=key)
        if acl and status == 400:
            return UBLAEnabledError(self._ubla_message(exc), bucket=self._bucket_name)
        if status in (401, 403):
            return AuthError(self._auth_message(status, exc), status=status)
        return GCSError(
            f"GCS API error (HTTP {status}) on bucket '{self._bucket_name}': {exc}"
        )

    def _startup_message(self, exc: object) -> str:
        return (
            f"Could not reach GCS bucket '{self._bucket_name}': {exc}. "
            "Verify GCS_BUCKET names an existing bucket, GOOGLE_APPLICATION_CREDENTIALS "
            "points at a valid service-account key (or ADC is configured), and the "
            "service account holds roles/storage.objectAdmin on the bucket."
        )

    def _unreachable_message(self, exc: object) -> str:
        """Actionable message for a mid-session "dependency is down" failure (RF4).

        Used for the transport/retry/auth errors in
        :data:`_DEPENDENCY_DOWN_ERRORS` that are not ``GoogleAPICallError`` and so
        carry no HTTP status — the bucket is named so the curated tool error stays
        actionable just like the startup probe's.
        """
        return (
            f"Could not reach GCS bucket '{self._bucket_name}': {exc}. "
            "The storage backend may be unreachable (network/DNS/TLS), its request "
            "retries may be exhausted, or the service-account credentials could not "
            "be refreshed. Verify connectivity and that GOOGLE_APPLICATION_CREDENTIALS "
            "is still valid, then retry."
        )

    def _not_found_message(self, key: str | None) -> str:
        target = f"object '{key}'" if key else "the object"
        return (
            f"No such artifact: {target} was not found in bucket '{self._bucket_name}'. "
            "It may have been deleted, or the object reference (key or URL) is wrong."
        )

    def _auth_message(self, status: int, exc: object) -> str:
        return (
            f"Permission denied (HTTP {status}) on bucket '{self._bucket_name}': {exc}. "
            "Verify the service account holds roles/storage.objectAdmin on the bucket."
        )

    def _ubla_message(self, exc: object) -> str:
        detail = str(exc).strip()
        base = (
            f"Per-object ACL grant was rejected with HTTP 400 on bucket "
            f"'{self._bucket_name}'. This almost always means Uniform Bucket-Level "
            "Access (UBLA) is enabled, which disables per-object ACLs. Disable it with: "
            f"gcloud storage buckets update gs://{self._bucket_name} "
            "--no-uniform-bucket-level-access "
            f"(or: gsutil ubla set off gs://{self._bucket_name}), then retry."
        )
        lowered = detail.lower()
        if "uniform" in lowered or "ubla" in lowered:
            base = f"{base} (API said: {detail})"
        return base


if TYPE_CHECKING:
    # mypy-only conformance gate: if GCSClient ever drifts from the protocol's
    # signatures or return shapes, this assignment fails to type-check.
    def _assert_gcsclient_implements_protocol(client: GCSClient) -> GCSClientProtocol:
        return client
