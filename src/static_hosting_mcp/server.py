"""FastMCP server for static-hosting-mcp.

U2 replaces U1's placeholder lifespan with the real one: it loads :class:`Config`
from the environment, constructs the U3 :class:`GCSClient`, runs the startup
reachability probe, and yields an :class:`AppContext` that owns the client and
config for the process lifetime. Tools read that context through :func:`_ctx`;
credentials and the key path stay in the lifespan and never reach the tool
surface (R11, KTD8).

U5 adds the three write tools — :func:`publish_artifact`, :func:`grant_access`,
and :func:`revoke_access` — plus the :func:`_handle_api_error` mapper that turns
U3's typed errors into the curated, application-level ``isError`` dicts U4
shapes (KTD11). Every tool returns a curated ``dict`` and never raises through to
the session. (The read/lifecycle tools — list/get/delete — are U6.)

Following the ``ultimate-brain-mcp`` reference shape: an ``@asynccontextmanager``
``app_lifespan`` yielding a frozen-ish ``AppContext`` dataclass, a small ``_ctx``
accessor the tool layer uses to reach it, and ``@mcp.tool`` ``async def`` tools
with ``ToolAnnotations`` and Pydantic ``Annotated``/``Field`` parameters.
"""

from __future__ import annotations

import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from inspect import isawaitable
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import Config
from .formatters import (
    error,
    generate_key,
    grant_results,
    infer_content_type,
    not_found_message,
    publish_result,
    ubla_disabled_message,
)
from .gcs_client import (
    AuthError,
    GCSClient,
    GCSClientProtocol,
    GCSError,
    ObjectNotFoundError,
    StartupError,
    UBLAEnabledError,
)


@dataclass
class AppContext:
    """Process-lifetime state owned by the lifespan and shared with every tool.

    ``client`` is typed as :class:`GCSClientProtocol` (not the concrete
    :class:`GCSClient`) so ``mypy`` verifies that whatever the lifespan builds —
    the real client in production or an injected fake in tests — conforms to the
    async method surface the tools call. Populated once before ``yield`` and
    read-only thereafter, so no locking is required.
    """

    client: GCSClientProtocol
    config: Config


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Build config + GCS client, reachability-check, and yield the AppContext.

    A :class:`StartupError` from the reachability probe (U3) is converted into a
    clean, actionable stderr line — the message already names the bucket — plus a
    non-zero exit, mirroring ``main()``'s missing-env fail-fast. This keeps the
    operator from seeing a raw anyio/``mcp.run()`` traceback when the bucket or
    credentials are wrong (R12 wiring). On normal shutdown the ``finally`` tears
    the client down best-effort.
    """
    config = Config.from_env()
    client: GCSClientProtocol = GCSClient(
        config.bucket, key_path=config.key_path, project=config.project
    )
    try:
        await client.check_reachable()
    except StartupError as exc:
        # Mirror main()'s missing-env pattern: one actionable line, clean exit.
        print(f"[static-hosting-mcp] {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        yield AppContext(client=client, config=config)
    finally:
        await _close_client(client)


async def _close_client(client: GCSClientProtocol) -> None:
    """Best-effort teardown of the GCS client at lifespan shutdown.

    :class:`GCSClientProtocol` declares no teardown method, and a stdio server's
    lifespan spans the whole process — so the underlying SDK connection pool is
    reclaimed at exit anyway. We still honor a ``close``/``aclose`` the concrete
    client may expose (sync or async), rather than reaching into another unit's
    private SDK handle, so the seam stays correct if U3 grows one later.
    """
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if closer is None:
        return
    result = closer()
    if isawaitable(result):
        await result


mcp = FastMCP("static-hosting-mcp", lifespan=app_lifespan)


def _ctx(ctx: Context | None) -> AppContext:
    """Return the lifespan :class:`AppContext` for a tool invocation.

    FastMCP injects ``ctx`` at call time, so it is never ``None`` in practice;
    the ``Context | None`` annotation only reflects the ``ctx: Context = None``
    sentinel default U5/U6 use on tool signatures (the type-based-injection seam
    the reference relies on).
    """
    assert ctx is not None
    return ctx.request_context.lifespan_context


# ---------------------------------------------------------------------------
# Tool-layer helpers (U5): client-side email validation and typed-error mapping
# ---------------------------------------------------------------------------

# Client-side email *format* check (KTD9): malformed addresses are rejected with
# a per-email result before any API call; well-formed ones are applied in a
# single batched ACL save. This is a format gate, not a deliverability check —
# whether the address is a real Google account is decided by GCS at grant time.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _classify_emails(emails: list[str]) -> tuple[list[str], list[tuple[str, bool]]]:
    """Split *emails* into ``(format_valid_subset, [(email, is_valid), ...])``.

    The valid subset is what gets sent to the GCS API in one batched call; the
    parallel ``(email, is_valid)`` list preserves the caller's order so the
    per-email result can report malformed entries alongside the granted ones.
    """
    valid: list[str] = []
    classified: list[tuple[str, bool]] = []
    for email in emails:
        ok = bool(_EMAIL_RE.match(email))
        classified.append((email, ok))
        if ok:
            valid.append(email)
    return valid, classified


def _malformed_email_message(email: str) -> str:
    """Per-email error text for a client-side-rejected (malformed) address."""
    return (
        f"{email!r} is not a valid email address and was skipped; no access was "
        "changed for it. Provide a Google-account email such as 'name@example.com'."
    )


def _handle_api_error(exc: GCSError, *, ref: str | None = None) -> dict:
    """Map a typed :class:`GCSError` to a curated, actionable error dict (KTD11).

    ``isinstance``-matches U3's exception classes and calls U4's message
    templates / :func:`error` shaper, so a storage failure becomes an
    application-level ``isError`` dict (a *successful* ``CallToolResult``) rather
    than an exception that crashes the session. ``ref`` is the caller-facing
    object reference, used to name the missing artifact in a not-found message.
    """
    if isinstance(exc, UBLAEnabledError):
        return error(ubla_disabled_message(exc.bucket or "the configured bucket"))
    if isinstance(exc, ObjectNotFoundError):
        return error(not_found_message(ref or exc.key or "the artifact"))
    if isinstance(exc, AuthError):
        # The client's message already names the bucket and the required role.
        return error(str(exc))
    # Catch-all for any other typed storage failure: surface its message rather
    # than letting it propagate (no tool raises through to the session).
    return error(str(exc))


async def _change_access(
    client: GCSClientProtocol,
    object_ref: str,
    emails: list[str],
    *,
    grant: bool,
    field: str,
) -> dict:
    """Shared implementation for :func:`grant_access` / :func:`revoke_access`.

    Normalizes the object reference (KTD7), validates each email's format
    client-side, applies every *valid* email in a **single** batched
    ``grant_read`` / ``revoke_read`` call (U3 — one reload→save per object, never
    concurrent per-email saves), and returns a curated dict whose *field* holds
    the per-email outcomes. Malformed emails are reported but never sent to the
    API; the artifact's URL is unchanged.
    """
    key = client.normalize_ref(object_ref)
    valid, classified = _classify_emails(emails)
    if valid:
        try:
            if grant:
                await client.grant_read(key, valid)
            else:
                await client.revoke_read(key, valid)
        except GCSError as exc:
            return _handle_api_error(exc, ref=object_ref)
    outcomes = grant_results(
        (email, ok, None if ok else _malformed_email_message(email)) for email, ok in classified
    )
    return {"key": key, "url": client.authenticated_url(key), field: outcomes}


# ---------------------------------------------------------------------------
# Write tools (U5): publish_artifact, grant_access, revoke_access
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Publish artifact",
        readOnlyHint=False,
        idempotentHint=False,
    )
)
async def publish_artifact(
    title: Annotated[
        str,
        Field(
            description=(
                "Human title for the artifact; used to derive the permanent object "
                "key (slugified, under a YYYY/MM/DD/ date prefix)."
            ),
            examples=["Q2 tariff deep research", "Launch plan v3"],
        ),
    ],
    content: Annotated[
        str | None,
        Field(
            description=(
                "Inline artifact body as text (e.g. rendered HTML or Markdown). "
                "Provide this OR `source_path`, never both."
            ),
            examples=["<html><body><h1>Report</h1></body></html>"],
        ),
    ] = None,
    source_path: Annotated[
        str | None,
        Field(
            description=(
                "Path to a local file to upload as-is (text or binary). Provide "
                "this OR `content`, never both."
            ),
            examples=["/home/user/report.pdf", "out/summary.md"],
        ),
    ] = None,
    content_type: Annotated[
        str | None,
        Field(
            description=(
                "Optional MIME type override. When omitted it is inferred from the "
                "source/title extension, defaulting to text/html for inline content."
            ),
            examples=["text/html", "application/pdf"],
        ),
    ] = None,
    grant_emails: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional Google-account emails to grant read access to as part of "
                "publishing. Malformed emails are reported per-email and skipped."
            ),
            examples=[["alice@example.com", "bob@example.com"]],
        ),
    ] = None,
    ctx: Context = None,
) -> dict:
    """Publish a single artifact to cloud storage and return its permanent URL.

    Provide EITHER inline `content` OR a local `source_path` (exactly one), plus
    a `title`; optionally pre-share it by passing `grant_emails`. Use this to
    publish — and optionally share — in one step; to share an artifact that
    already exists, use `grant_access`. Returns a dict with the object `key`, the
    authenticated `url`, the stored `content_type`, the byte `size`, and the
    per-email `grants` (empty when no `grant_emails` were given). On a UBLA-on
    bucket the object is uploaded but the grant returns an actionable error.
    """
    client = _ctx(ctx).client

    # Presence-based XOR (tests *which field was supplied*, not truthiness, so an
    # explicit empty string still counts as "content supplied" — AE7).
    has_content = content is not None
    has_source = source_path is not None
    if has_content and has_source:
        return error(
            "Provide either `content` or `source_path`, not both — they are "
            "mutually exclusive ways to supply the artifact body. No object was "
            "created.",
            hint="Pass inline `content` for text, or a `source_path` for a local file.",
        )
    if not has_content and not has_source:
        return error(
            "Provide exactly one of `content` or `source_path` — neither was "
            "supplied, so there is nothing to publish. No object was created.",
            hint="Pass inline `content` for text, or a `source_path` for a local file.",
        )

    if content is not None:
        data = content.encode("utf-8")
    else:
        # The XOR above guarantees source_path is set when content is None.
        assert source_path is not None
        try:
            data = Path(source_path).read_bytes()
        except OSError as exc:
            return error(
                f"Could not read source_path {source_path!r}: {exc}.",
                hint="Check the file exists and is readable from the server's working directory.",
            )

    if not data:
        return error(
            "Refusing to publish an empty (zero-byte) artifact. Supply non-empty "
            "`content` or a non-empty file. No object was created."
        )

    resolved_type = infer_content_type(
        content_type=content_type, source_path=source_path, title=title
    )
    key = generate_key(title, resolved_type, date.today())

    try:
        await client.upload(key, data, resolved_type)
    except GCSError as exc:
        return _handle_api_error(exc, ref=client.authenticated_url(key))

    grants: list[dict] = []
    if grant_emails:
        valid, classified = _classify_emails(grant_emails)
        if valid:
            try:
                await client.grant_read(key, valid)
            except GCSError as exc:
                return _handle_api_error(exc, ref=client.authenticated_url(key))
        grants = grant_results(
            (email, ok, None if ok else _malformed_email_message(email)) for email, ok in classified
        )

    return publish_result(
        key=key,
        url=client.authenticated_url(key),
        content_type=resolved_type,
        size=len(data),
        grants=grants,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Grant artifact access",
        readOnlyHint=False,
        idempotentHint=True,
    )
)
async def grant_access(
    object_ref: Annotated[
        str,
        Field(
            description=(
                "The artifact's object key or full authenticated URL (either form is accepted)."
            ),
            examples=[
                "2026/06/24/q2-tariff-deep-research-7f3a9c.html",
                "https://storage.cloud.google.com/my-bucket/2026/06/24/report-7f3a9c.html",
            ],
        ),
    ],
    emails: Annotated[
        list[str],
        Field(
            description=(
                "One or more Google-account emails to grant read access to. "
                "Malformed emails are reported per-email and skipped."
            ),
            examples=[["alice@example.com"]],
        ),
    ],
    ctx: Context = None,
) -> dict:
    """Grant read access to an already-published artifact for one or more emails.

    Identify the artifact by `object_ref` (its object key or full URL). Use this
    to share an artifact that already exists; to publish a new artifact (and
    optionally share it at the same time) use `publish_artifact`. Idempotent:
    re-granting an existing reader is a no-op. The artifact's URL is unchanged.
    Returns the `key`, `url`, and per-email `grants`.
    """
    client = _ctx(ctx).client
    return await _change_access(client, object_ref, emails, grant=True, field="grants")


@mcp.tool(
    annotations=ToolAnnotations(
        title="Revoke artifact access",
        readOnlyHint=False,
        idempotentHint=True,
        destructiveHint=True,
    )
)
async def revoke_access(
    object_ref: Annotated[
        str,
        Field(
            description=(
                "The artifact's object key or full authenticated URL (either form is accepted)."
            ),
            examples=[
                "2026/06/24/q2-tariff-deep-research-7f3a9c.html",
                "https://storage.cloud.google.com/my-bucket/2026/06/24/report-7f3a9c.html",
            ],
        ),
    ],
    emails: Annotated[
        list[str],
        Field(
            description=(
                "One or more Google-account emails to remove read access from. "
                "Malformed emails are reported per-email and skipped."
            ),
            examples=[["alice@example.com"]],
        ),
    ],
    ctx: Context = None,
) -> dict:
    """Revoke read access to a published artifact for one or more emails.

    Identify the artifact by `object_ref` (its object key or full URL). The
    artifact and its URL continue to exist; only the named accounts lose access.
    Idempotent: revoking an email that is not a grantee is a no-op. Returns the
    `key`, `url`, and per-email `revocations`.
    """
    client = _ctx(ctx).client
    return await _change_access(client, object_ref, emails, grant=False, field="revocations")
