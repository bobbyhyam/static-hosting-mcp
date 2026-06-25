"""FastMCP server for static-hosting-mcp.

U2 replaces U1's placeholder lifespan with the real one: it loads :class:`Config`
from the environment, constructs the U3 :class:`GCSClient`, runs the startup
reachability probe, and yields an :class:`AppContext` that owns the client and
config for the process lifetime. Tools (U5/U6) read that context through
:func:`_ctx`; credentials and the key path stay in the lifespan and never reach
the tool surface (R11, KTD8).

Following the ``ultimate-brain-mcp`` reference shape: an ``@asynccontextmanager``
``app_lifespan`` yielding a frozen-ish ``AppContext`` dataclass, and a small
``_ctx`` accessor the tool layer uses to reach it.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from inspect import isawaitable
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import Config
from .formatters import (
    artifact_detail,
    artifact_summary,
    error,
    list_result,
    not_found_message,
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
# U6 — inspect & lifecycle tools: list_artifacts, get_artifact, delete_artifact
# ---------------------------------------------------------------------------
#
# These three tools extend U2's lifespan module with the read/list/delete half
# of the artifact tool surface (U5 adds publish/grant/revoke to the same module;
# on integration the six tools coexist here). Every tool reaches the GCS client
# through :func:`_ctx`, maps the client's typed errors to a curated error dict
# via :func:`_handle_api_error`, and uses the U4 ``formatters`` shapers so each
# returned shape has a single definition (R6 read side, R7, R8, R9, R14-R17).

# YYYY, YYYY/MM, or YYYY/MM/DD — the date-folder prefixes ``list_artifacts``
# accepts. Object keys are ``YYYY/MM/DD/<slug>-<suffix>.<ext>`` (U4), so a date
# prefix scopes the listing to a whole day, month, or year folder.
_DATE_PREFIX_RE = re.compile(r"^\d{4}(/\d{2}(/\d{2})?)?$")


def _handle_api_error(exc: GCSError, *, reference: str | None = None) -> dict[str, Any]:
    """Map a typed :class:`GCSError` from the client layer to a curated error dict.

    This is the typed-error -> ``formatters.error`` mapping the plan deliberately
    places in ``server.py`` (U4): ``isinstance``-match U3's exception classes and
    then call the U4 shapers, which is what lets ``formatters`` stay a
    network-free leaf. ``reference`` is the caller's original object reference
    (key or URL); it is echoed back in the not-found message so the agent sees
    the input it passed. Shared by every GCS-touching tool (U5/U6).
    """
    if isinstance(exc, ObjectNotFoundError):
        ref = reference if reference is not None else (exc.key or "the artifact")
        return error(not_found_message(ref))
    if isinstance(exc, UBLAEnabledError):
        return error(ubla_disabled_message(exc.bucket or ""))
    if isinstance(exc, AuthError):
        # Separate "what happened" (the wrapper's bucket-named message) from the
        # "next step" hint, per R17's structured-and-actionable error contract.
        return error(
            str(exc),
            hint="Confirm the service account holds roles/storage.objectAdmin on the bucket.",
        )
    # Any other GCSError already carries an actionable, bucket-named message.
    return error(str(exc))


def _date_prefix_to_object_prefix(date_prefix: str | None) -> str | None:
    """Validate a ``date_prefix`` and fold it into an object-key prefix.

    ``None``/blank means "no filter". A well-formed ``YYYY`` / ``YYYY/MM`` /
    ``YYYY/MM/DD`` value is returned with a trailing ``/`` so it matches on the
    date-folder boundary (``"2026/06"`` lists June only, never a hypothetical
    ``"2026/061"`` sibling). A malformed value raises :class:`ValueError` so the
    tool can return a structured error instead of silently listing every object.
    """
    if date_prefix is None:
        return None
    cleaned = date_prefix.strip().strip("/")
    if not cleaned:
        return None
    if not _DATE_PREFIX_RE.match(cleaned):
        raise ValueError(
            f"date_prefix {date_prefix!r} is not a date folder. Use YYYY, YYYY/MM, "
            "or YYYY/MM/DD (for example '2026', '2026/06', or '2026/06/24')."
        )
    return f"{cleaned}/"


@mcp.tool(annotations=ToolAnnotations(title="List artifacts", readOnlyHint=True))
async def list_artifacts(
    date_prefix: Annotated[
        str | None,
        Field(
            description=(
                "Optional date-folder filter — YYYY, YYYY/MM, or YYYY/MM/DD "
                "(e.g. '2026', '2026/06', '2026/06/24'). Omit to list across all "
                "dates."
            ),
            examples=["2026", "2026/06", "2026/06/24"],
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=1000,
            description="Maximum number of artifacts to return (default 50).",
        ),
    ] = 50,
    ctx: Context = None,
) -> dict[str, Any]:
    """List published artifacts as curated summaries — use this to find many; use get_artifact for one.

    Returns an envelope: ``items`` (each with the object key, authenticated URL,
    created date, byte size, and grantee *count*), ``total`` (the number returned
    in this response), ``truncated`` (whether more artifacts match than were
    returned), and a ``hint``. Filter by ``date_prefix`` to scope to a day, month,
    or year. A malformed ``date_prefix`` returns a structured error rather than
    listing everything.
    """
    app = _ctx(ctx)
    try:
        prefix = _date_prefix_to_object_prefix(date_prefix)
    except ValueError as exc:
        return error(str(exc))
    # limit + 1 probe: fetching one extra object tells us whether more exist
    # than the caller asked for, with no second round-trip and no full-bucket
    # scan. ``total`` is therefore the size of *this* page (U4's list_result
    # computes it as len(items)), not a full-prefix count (deferred, plan
    # Non-Goals).
    fetched = await app.client.list_objects(prefix=prefix, limit=limit + 1)
    truncated = len(fetched) > limit
    page = fetched[:limit]
    # Curated summaries report a grantee *count* (never the ACL itself, so a
    # listing cannot leak who an object is shared with). list_objects does not
    # carry ACLs, so read each object's grantees — concurrently, to keep the
    # listing responsive.
    grantee_lists = await asyncio.gather(
        *(app.client.list_grantees(obj["key"]) for obj in page)
    )
    items = [
        artifact_summary(
            key=obj["key"],
            url=app.client.authenticated_url(obj["key"]),
            created=obj["created"],
            size=obj["size"],
            grantee_count=len(grantees),
        )
        for obj, grantees in zip(page, grantee_lists, strict=True)
    ]
    hint = (
        f"More than {limit} artifacts match; narrow date_prefix or raise limit to see more."
        if truncated
        else "All matching artifacts are shown."
    )
    return list_result(items, truncated=truncated, hint=hint)


@mcp.tool(annotations=ToolAnnotations(title="Get artifact", readOnlyHint=True))
async def get_artifact(
    object_ref: Annotated[
        str,
        Field(
            description=(
                "The artifact's object key (e.g. "
                "'2026/06/24/q2-tariff-deep-research-7f3a9c.html') or its full "
                "authenticated URL."
            ),
            examples=[
                "2026/06/24/q2-tariff-deep-research-7f3a9c.html",
                "https://storage.cloud.google.com/my-bucket/2026/06/24/report-ab12cd.html",
            ],
        ),
    ],
    ctx: Context = None,
) -> dict[str, Any]:
    """Get one artifact's metadata by key or URL — use this for one; use list_artifacts to find many.

    Returns the authenticated URL, content-type, byte size, created date, and the
    current grantee emails. Returns a structured not-found error if no artifact
    matches the reference.
    """
    app = _ctx(ctx)
    key = app.client.normalize_ref(object_ref)
    try:
        meta = await app.client.get_metadata(key)
        grantees = await app.client.list_grantees(key)
    except GCSError as exc:
        return _handle_api_error(exc, reference=object_ref)
    return artifact_detail(
        url=app.client.authenticated_url(key),
        content_type=meta["content_type"],
        size=meta["size"],
        created=meta["created"],
        grantees=grantees,
    )


@mcp.tool(annotations=ToolAnnotations(title="Delete artifact", destructiveHint=True))
async def delete_artifact(
    object_ref: Annotated[
        str,
        Field(
            description=(
                "The artifact's object key or full authenticated URL. The object "
                "is permanently deleted and its URL stops resolving."
            ),
            examples=[
                "2026/06/24/q2-tariff-deep-research-7f3a9c.html",
                "https://storage.cloud.google.com/my-bucket/2026/06/24/report-ab12cd.html",
            ],
        ),
    ],
    ctx: Context = None,
) -> dict[str, Any]:
    """Delete an artifact by key or URL; its authenticated URL stops resolving afterward.

    Permanently removes the object and returns a confirmation with the deleted
    key, or a structured not-found error if no artifact matches the reference.
    """
    app = _ctx(ctx)
    key = app.client.normalize_ref(object_ref)
    try:
        await app.client.delete(key)
    except GCSError as exc:
        return _handle_api_error(exc, reference=object_ref)
    return {
        "deleted": True,
        "key": key,
        "message": f"Deleted artifact {key!r}. Its authenticated URL no longer resolves.",
    }
