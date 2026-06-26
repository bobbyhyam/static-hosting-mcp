"""FastMCP server for static-hosting-mcp.

U2 replaces U1's placeholder lifespan with the real one: it loads :class:`Config`
from the environment, constructs the U3 :class:`GCSClient`, runs the startup
reachability probe, and yields an :class:`AppContext` that owns the client and
config for the process lifetime. Tools (U5/U6) read that context through
:func:`_ctx`; credentials and the key path stay in the lifespan and never reach
the tool surface (R11, KTD8).

The lifespan follows a standard FastMCP shape: an ``@asynccontextmanager``
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
from datetime import date
from inspect import isawaitable
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import ENV_MAX_BYTES, ENV_SOURCE_ROOT, Config
from .formatters import (
    artifact_detail,
    artifact_summary,
    error,
    generate_key,
    grant_results,
    infer_content_type,
    list_result,
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

    Both startup failure modes are converted into a clean, actionable stderr line
    plus a non-zero exit, so the operator never sees a raw anyio/``mcp.run()``
    traceback (R12 wiring): a :class:`ValueError` from :meth:`Config.from_env` (a
    missing required var, or a *relative* ``GOOGLE_APPLICATION_CREDENTIALS`` whose
    path shape is invalid) and a :class:`StartupError` from the reachability probe
    (U3, message already names the bucket). Catching the ``from_env`` error here —
    not only in ``main()`` — gives every entry point (``mcp dev``, the FastMCP CLI)
    the same fail-fast contract (RF6). On normal shutdown the ``finally`` tears the
    client down best-effort.
    """
    try:
        config = Config.from_env()
    except ValueError as exc:
        # A missing var or a bad credential *path shape* — one actionable line,
        # clean exit, no traceback (RF6), before any client is constructed.
        print(f"[static-hosting-mcp] {exc}", file=sys.stderr)
        sys.exit(1)
    client: GCSClientProtocol = GCSClient(
        config.bucket, key_path=config.key_path, project=config.project
    )
    try:
        await client.check_reachable()
    except StartupError as exc:
        # Mirror the missing-env pattern: one actionable line, clean exit.
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
    try:
        fetched = await app.client.list_objects(prefix=prefix, limit=limit + 1)
        truncated = len(fetched) > limit
        page = fetched[:limit]
        # Curated summaries report a grantee *count* (never the ACL itself, so a
        # listing cannot leak who an object is shared with). list_objects does not
        # carry ACLs, so read each object's grantees — concurrently, to keep the
        # listing responsive. return_exceptions=True so a single object deleted
        # between the list_objects snapshot and its ACL reload degrades to a null
        # grantee_count instead of collapsing the whole page (RF2 2b).
        grantee_lists = await asyncio.gather(
            *(app.client.list_grantees(obj["key"]) for obj in page),
            return_exceptions=True,
        )
    except GCSError as exc:
        # list_artifacts was the only GCS-touching tool whose client calls ran
        # unguarded; a listing-time GCSError (e.g. a 403 AuthError) now returns the
        # same curated isError dict as every sibling, not a raw protocol error
        # (RF2 2a).
        return _handle_api_error(exc)
    items = [
        artifact_summary(
            key=obj["key"],
            url=app.client.authenticated_url(obj["key"]),
            created=obj["created"],
            size=obj["size"],
            # A per-object list_grantees failure (the object vanished mid-listing)
            # surfaces as a captured exception here; report an unknown count rather
            # than sink the page.
            grantee_count=(None if isinstance(grantees, BaseException) else len(grantees)),
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


def _publish_grant_outcomes(
    classified: list[tuple[str, bool]], *, grant_error: str | None
) -> list[dict]:
    """Shape per-email grant outcomes for ``publish_artifact`` (RF5).

    A malformed email always carries its skip message. A *valid* email is a clean
    success unless the batched grant failed post-upload (``grant_error`` set), in
    which case it is marked failed with that message — so the returned result names
    exactly which grants did not land while still carrying the recoverable key/url.
    """

    def _triple(email: str, ok: bool) -> tuple[str, bool, str | None]:
        if not ok:
            return email, False, _malformed_email_message(email)
        if grant_error is not None:
            return email, False, grant_error
        return email, True, None

    return grant_results(_triple(email, ok) for email, ok in classified)


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
            return _handle_api_error(exc, reference=object_ref)
    outcomes = grant_results(
        (email, ok, None if ok else _malformed_email_message(email)) for email, ok in classified
    )
    return {"key": key, "url": client.authenticated_url(key), field: outcomes}


# ---------------------------------------------------------------------------
# Source-path safety (RF1, security S1 / P0)
# ---------------------------------------------------------------------------
#
# publish_artifact reads a caller-controlled local file, uploads it, and can share
# it with an external Google account in the same call. Unconfined, that is a
# read-any-local-file -> publish -> share-to-attacker primitive whose highest-value
# target is the service-account key the whole repr=False credential design hides.
# Every source_path read therefore passes _check_source_path first: canonicalize,
# refuse credential/secret shapes (regardless of root), require an operator-opted-in
# allow-list root (default-deny), and require a regular file (closes the FIFO /
# device event-loop-hang). The read itself is offloaded and size-capped by the
# caller.

# Suffixes and directory names that are credential/secret shapes, refused as a
# source_path regardless of ARTIFACT_SOURCE_ROOT.
_SECRET_PATH_SUFFIXES = (".pem", ".key")
_SECRET_DIR_NAMES = frozenset({"secrets"})


def _is_within(path: Path, root: Path) -> bool:
    """Whether *path* equals or lives under *root* (both already canonicalized)."""
    try:
        return path == root or path.is_relative_to(root)
    except (OSError, ValueError):
        return False


def _is_secret_path(resolved: Path, config: Config) -> bool:
    """Whether the canonicalized *resolved* path is a credential/secret shape.

    Refused independently of the allow-list root (so a secret inside the root is
    still refused): the configured service-account key, anything under ``~/.ssh`` /
    ``~/.config/gcloud`` / ``~/.gnupg``, any path with a ``secrets`` directory
    component, and ``*.pem`` / ``*.key`` files (RF1 / R11/R13).
    """
    if config.key_path:
        try:
            if resolved == Path(config.key_path).resolve():
                return True
        except (OSError, ValueError):  # pragma: no cover - resolve() of a configured path
            pass
    # Path.home() raises RuntimeError when the home dir cannot be determined
    # (HOME unset and no pwd entry for the uid -- a real distroless / scratch
    # container shape). This gate must never raise (R17/KTD11), so on that shape
    # skip only the home-dir secret checks; the secrets/ directory-component and
    # *.pem / *.key suffix checks below still run.
    try:
        home: Path | None = Path.home()
    except RuntimeError:
        home = None
    if home is not None:
        for protected in (home / ".ssh", home / ".config" / "gcloud", home / ".gnupg"):
            if _is_within(resolved, protected):
                return True
    if any(part in _SECRET_DIR_NAMES for part in resolved.parts):
        return True
    return resolved.suffix.lower() in _SECRET_PATH_SUFFIXES


def _check_source_path(source_path: str, config: Config) -> tuple[Path | None, dict | None]:
    """Validate a caller's *source_path* against the RF1 policy.

    Returns ``(resolved_path, None)`` when the file may be read, or
    ``(None, error_dict)`` with a structured refusal and **no** upload otherwise.
    Order matters: canonicalize -> refuse secret shapes (regardless of root) ->
    require a configured allow-list root and confinement (canonicalization closes
    symlink escapes) -> require a regular file (closes the ``/dev/zero`` / FIFO
    permanent event-loop stall, adversarial A6).
    """
    try:
        resolved = Path(source_path).resolve()
    except (OSError, ValueError) as exc:
        # OSError covers unresolvable paths; ValueError covers an embedded NUL
        # byte ("embedded null character in path"), which Pydantic does not strip
        # from a str. _check_source_path is documented to never raise (R17/KTD11);
        # publish_artifact calls it unguarded, so both must become a refusal here
        # (mirrors _is_within, which already catches (OSError, ValueError)).
        return None, error(f"Could not resolve source_path {source_path!r}: {exc}.")

    if _is_secret_path(resolved, config):
        return None, error(
            "Refusing to read source_path: it resolves to a credential or secret "
            "location. This server never uploads service-account keys, SSH keys, or "
            "files under a secrets directory. No object was created.",
            hint="Publish only non-secret artifacts, or pass inline `content` for text.",
        )

    root = config.artifact_source_root
    if root is None:
        return None, error(
            "source_path uploads are disabled: no allowed source directory is "
            f"configured. Set {ENV_SOURCE_ROOT} to an absolute directory to enable "
            "reading local files, or pass inline `content` instead. No object was "
            "created.",
            hint=f"Set {ENV_SOURCE_ROOT} to the directory your artifacts live in.",
        )
    if not _is_within(resolved, Path(root)):
        return None, error(
            f"source_path {source_path!r} resolves outside the allowed source "
            f"directory ({root}); it was not read and no object was created.",
            hint=f"Place the file under {ENV_SOURCE_ROOT} ({root}), or pass inline `content`.",
        )

    # is_file() can re-raise OSError on some platforms / CPython versions (EACCES
    # on a no-search parent directory under the root; EIO / ESTALE on a stale
    # network mount). On the project's CPython, is_file() delegates to
    # os.path.isfile and swallows these (returning False), but the gate is
    # documented to *never* raise (R17/KTD11) and publish_artifact consumes it
    # unguarded, so guard the syscall with a blanket OSError -> curated refusal so
    # the contract holds on every interpreter. The message echoes only the
    # caller-supplied source_path -- never exc itself, whose OSError.filename is
    # the *resolved* absolute path and would leak the real on-disk location
    # (RF12 / adversarial ADV4-1).
    try:
        is_regular_file = resolved.is_file()
    except OSError as exc:
        return None, error(
            f"Could not access source_path {source_path!r}: "
            f"{exc.strerror or exc.__class__.__name__}.",
            hint="Provide a path to an existing, readable regular file, or pass inline `content`.",
        )
    if not is_regular_file:
        return None, error(
            f"source_path {source_path!r} is not a regular file (it may be missing, a "
            "directory, or a special/device file); it was not read and no object was "
            "created.",
            hint="Provide a path to an existing regular file, or pass inline `content`.",
        )

    return resolved, None


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
                "Path to a local file to upload as-is. Disabled by default: the "
                "operator must set the ARTIFACT_SOURCE_ROOT environment variable "
                "to an allowed directory, and the file must resolve inside that "
                "root (credential- or secret-shaped paths are refused even then, "
                "and the file is size-capped by ARTIFACT_MAX_BYTES). Prefer inline "
                "`content`, which is the default and needs no operator setup. "
                "Provide this OR `content`, never both."
            ),
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

    Provide EITHER inline `content` (the default, no operator setup required) OR
    a local `source_path` (only when the operator has enabled it via
    `ARTIFACT_SOURCE_ROOT` — see that parameter) — exactly one — plus a `title`;
    optionally pre-share it by passing `grant_emails`. Use this to publish — and
    optionally share — in one step; to share an artifact that already exists, use
    `grant_access`. Returns a dict with the object `key`, the authenticated
    `url`, the stored `content_type`, the byte `size`, the per-email `grants`
    (empty when no `grant_emails` were given), and an optional `warning`. If a
    post-upload grant fails (e.g. on a UBLA-on bucket) the object is still
    uploaded and addressable: the result is a success-with-warning that carries
    the recoverable `key`/`url`, marks the failed emails in `grants`, and sets
    `warning` — retry the grant with `grant_access`; do NOT re-publish, which
    would mint a duplicate object.
    """
    app = _ctx(ctx)
    client = app.client
    config = app.config

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
        # RF1: confine the read to the operator's allow-list root, refuse secret
        # shapes, and require a regular file — all before any I/O on the bytes.
        # Offloaded off the stdio event loop (like the bytes read below) so the
        # gate's resolve() / is_file() syscalls cannot freeze the loop on a hung or
        # stale network mount under the root (RF12 / adversarial RR-1).
        resolved, src_error = await asyncio.to_thread(_check_source_path, source_path, config)
        if src_error is not None:
            return src_error
        assert resolved is not None
        # Size-cap from a stat *before* reading, so an oversized (or special) file
        # is refused without being pulled into memory (RF1 OOM guard).
        try:
            file_size = (await asyncio.to_thread(resolved.stat)).st_size
        except OSError as exc:
            # exc.strerror, not exc: str(exc) carries OSError.filename, the
            # resolved absolute path, which must not leak to the agent (RF12).
            return error(
                f"Could not read source_path {source_path!r}: "
                f"{exc.strerror or exc.__class__.__name__}.",
                hint="Check the file exists and is readable from the server's working directory.",
            )
        if file_size > config.artifact_max_bytes:
            return error(
                f"source_path {source_path!r} is {file_size} bytes, over the "
                f"{config.artifact_max_bytes}-byte limit; it was not read and no "
                "object was created.",
                hint=f"Raise {ENV_MAX_BYTES} or publish a smaller artifact.",
            )
        # Offload the blocking read so the stdio event loop is never blocked (RF1 /
        # performance #2), restoring this module's no-blocking-I/O invariant.
        try:
            data = await asyncio.to_thread(resolved.read_bytes)
        except OSError as exc:
            # exc.strerror, not exc: str(exc) carries OSError.filename, the
            # resolved absolute path, which must not leak to the agent (RF12).
            return error(
                f"Could not read source_path {source_path!r}: "
                f"{exc.strerror or exc.__class__.__name__}.",
                hint="Check the file exists and is readable from the server's working directory.",
            )

    if not data:
        return error(
            "Refusing to publish an empty (zero-byte) artifact. Supply non-empty "
            "`content` or a non-empty file. No object was created."
        )

    # Cap applies to inline content too (the source path is stat-capped above); a
    # uniform guard here also catches a file that grew between stat and read (RF1).
    if len(data) > config.artifact_max_bytes:
        return error(
            f"Artifact is {len(data)} bytes, over the {config.artifact_max_bytes}-byte "
            "limit; no object was created.",
            hint=f"Raise {ENV_MAX_BYTES} or publish a smaller artifact.",
        )

    resolved_type = infer_content_type(
        content_type=content_type, source_path=source_path, title=title
    )
    key = generate_key(title, resolved_type, date.today())

    try:
        await client.upload(key, data, resolved_type)
    except GCSError as exc:
        return _handle_api_error(exc, reference=client.authenticated_url(key))

    grants: list[dict] = []
    warning: str | None = None
    if grant_emails:
        valid, classified = _classify_emails(grant_emails)
        grant_error: str | None = None
        if valid:
            try:
                await client.grant_read(key, valid)
            except GCSError as exc:
                # RF5: the upload already succeeded, so do NOT discard the object's
                # key/url with a bare error — that strands a live object the agent
                # can't address and tempts a re-publish that mints a duplicate.
                # Mark the valid emails failed and return a recoverable
                # success-with-warning; the grant can be retried alone via
                # grant_access.
                grant_error = _handle_api_error(exc, reference=client.authenticated_url(key))[
                    "error"
                ]
                warning = (
                    f"The artifact was published, but granting read access failed: "
                    f"{grant_error} The object exists at the returned key/url — retry "
                    "the grant with grant_access; do not re-publish (that would create "
                    "a duplicate)."
                )
        grants = _publish_grant_outcomes(classified, grant_error=grant_error)

    return publish_result(
        key=key,
        url=client.authenticated_url(key),
        content_type=resolved_type,
        size=len(data),
        grants=grants,
        warning=warning,
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
