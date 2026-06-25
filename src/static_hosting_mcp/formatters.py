"""Pure formatting leaf for ``static-hosting-mcp``.

Object-key generation, content-type inference, curated tool-response shapes,
and structured error-message templates. Every function here is a **pure,
network-free** transform: the module imports only the standard library and, in
particular, imports **nothing** from ``gcs_client.py``.

The typed-client-error -> dict mapping deliberately lives in ``server.py`` (a
``_handle_api_error`` helper that ``isinstance``-matches the GCS client's
exception classes and then calls the shapers below). Keeping that mapping out
of this module is what lets ``formatters`` stay an import-pure leaf with no edge
back to the network layer (plan unit U4; KTD6 key shape, KTD11 structured
errors).
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from collections.abc import Iterable
from datetime import date

# ---------------------------------------------------------------------------
# Object-key generation (KTD6: YYYY/MM/DD/<slug>-<suffix>.<ext>)
# ---------------------------------------------------------------------------

#: Maximum slug length before the random suffix is appended. Long titles are
#: truncated to keep keys readable and well under object-name length limits.
SLUG_MAX_LENGTH = 60

#: Slug used when a title slugifies to the empty string (symbols-only / blank).
SLUG_FALLBACK = "artifact"

#: Length of the random collision-avoidance suffix.
SUFFIX_LENGTH = 6

#: Alphabet for the random suffix: lowercase alphanumerics keep keys clean and
#: case-unambiguous. 36**6 ~= 2.2e9 values makes a same-day, same-slug
#: collision effectively impossible in practice (revisited only if one is ever
#: observed -- see plan Non-Goals).
_SUFFIX_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

#: Content-type inferred for inline content whose title carries no usable file
#: extension (the agent typically publishes rendered HTML).
DEFAULT_INLINE_CONTENT_TYPE = "text/html"

#: Content-type returned when a source/title extension is present but unknown.
UNKNOWN_CONTENT_TYPE = "application/octet-stream"

#: File extension used in the object key when a content-type is unknown.
UNKNOWN_EXTENSION = "bin"

#: content-type -> file-extension, used when building the object key.
_CONTENT_TYPE_TO_EXT: dict[str, str] = {
    "text/html": "html",
    "text/markdown": "md",
    "text/plain": "txt",
    "text/css": "css",
    "text/csv": "csv",
    "application/json": "json",
    "application/pdf": "pdf",
    "application/xml": "xml",
    "application/javascript": "js",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    "image/webp": "webp",
}

#: file-extension -> content-type, used to infer a content-type from a source
#: path or title. Includes a few aliases (``htm``, ``jpeg``, ``markdown``).
_EXT_TO_CONTENT_TYPE: dict[str, str] = {
    "html": "text/html",
    "htm": "text/html",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "txt": "text/plain",
    "text": "text/plain",
    "css": "text/css",
    "csv": "text/csv",
    "json": "application/json",
    "pdf": "application/pdf",
    "xml": "application/xml",
    "js": "application/javascript",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "webp": "image/webp",
}

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_EXTENSION_RE = re.compile(r"\.([A-Za-z0-9]{1,8})$")


def slugify(title: str) -> str:
    """Turn a human title into a lowercase, ASCII, hyphen-separated slug.

    Unicode is ASCII-folded (``Café`` -> ``cafe``), every run of non-alphanumeric
    characters collapses to a single hyphen, leading/trailing hyphens are
    trimmed, and the result is capped at :data:`SLUG_MAX_LENGTH`. A title that
    reduces to nothing (blank or symbols only) falls back to
    :data:`SLUG_FALLBACK`.
    """
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = _NON_ALNUM_RE.sub("-", folded.lower()).strip("-")
    if len(slug) > SLUG_MAX_LENGTH:
        slug = slug[:SLUG_MAX_LENGTH].rstrip("-")
    return slug or SLUG_FALLBACK


def _random_suffix() -> str:
    """Return a fresh :data:`SUFFIX_LENGTH`-char ``secrets``-based suffix."""
    return "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(SUFFIX_LENGTH))


def _base_content_type(content_type: str) -> str:
    """Strip parameters/case from a content-type (``text/html; charset`` -> ``text/html``)."""
    return content_type.split(";", 1)[0].strip().lower()


def _extension_of(name: str) -> str | None:
    """Return the lowercase trailing file extension of *name*, or ``None``.

    Only a short trailing ``.<alnum>`` token counts, so a dotted title such as
    ``"v1.2 release plan"`` is correctly treated as having no extension.
    """
    match = _EXTENSION_RE.search(name.strip())
    return match.group(1).lower() if match else None


def extension_for(content_type: str) -> str:
    """Map a content-type to a file extension, defaulting to :data:`UNKNOWN_EXTENSION`."""
    return _CONTENT_TYPE_TO_EXT.get(_base_content_type(content_type), UNKNOWN_EXTENSION)


def infer_content_type(
    *,
    content_type: str | None = None,
    source_path: str | None = None,
    title: str | None = None,
) -> str:
    """Resolve the content-type for an artifact.

    Precedence: an explicit *content_type* override always wins; otherwise the
    extension of *source_path* (then *title*) is mapped via the inference table,
    with an unknown extension yielding :data:`UNKNOWN_CONTENT_TYPE`. Inline
    content with no usable extension defaults to
    :data:`DEFAULT_INLINE_CONTENT_TYPE`.
    """
    if content_type:
        return content_type
    for candidate in (source_path, title):
        if not candidate:
            continue
        ext = _extension_of(candidate)
        if ext is None:
            continue
        return _EXT_TO_CONTENT_TYPE.get(ext, UNKNOWN_CONTENT_TYPE)
    return DEFAULT_INLINE_CONTENT_TYPE


def generate_key(title: str, content_type: str, today: date) -> str:
    """Build a permanent, collision-proof object key for an artifact.

    The shape is ``YYYY/MM/DD/<slug>-<suffix>.<ext>`` where the date comes from
    the injected *today* (injected for deterministic tests), the slug from
    *title*, the suffix from :func:`_random_suffix`, and the extension from
    *content_type*. Two calls with identical arguments still differ in their
    suffix.
    """
    return f"{today:%Y/%m/%d}/{slugify(title)}-{_random_suffix()}.{extension_for(content_type)}"


# ---------------------------------------------------------------------------
# Curated tool-response shapes (KTD11: curated dict / list[dict] returns)
# ---------------------------------------------------------------------------


def publish_result(
    *,
    key: str,
    url: str,
    content_type: str,
    size: int,
    grants: list[dict] | None = None,
) -> dict:
    """Curated ``publish_artifact`` result: object key, authenticated URL,
    stored content-type, byte size, and the per-email ``grants`` outcomes
    (an empty list when no ``grant_emails`` were supplied)."""
    return {
        "key": key,
        "url": url,
        "content_type": content_type,
        "size": size,
        "grants": grants if grants is not None else [],
    }


def artifact_summary(
    *,
    key: str,
    url: str,
    created: str | None,
    size: int,
    grantee_count: int,
) -> dict:
    """Curated one-line summary for ``list_artifacts`` items.

    Reports the *count* of grantees (``grantee_count``), never the ACL itself,
    so a listing does not leak who an object is shared with.
    """
    return {
        "key": key,
        "url": url,
        "created": created,
        "size": size,
        "grantee_count": grantee_count,
    }


def artifact_detail(
    *,
    url: str,
    content_type: str,
    size: int,
    created: str | None,
    grantees: list[str],
) -> dict:
    """Curated ``get_artifact`` detail: URL, content-type, size, created date,
    and the current grantee emails (R8 / AE4)."""
    return {
        "url": url,
        "content_type": content_type,
        "size": size,
        "created": created,
        "grantees": list(grantees),
    }


def grant_results(outcomes: Iterable[tuple[str, bool, str | None]]) -> list[dict]:
    """Shape per-email grant/revoke *outcomes* into a curated list.

    Each outcome is an ``(email, ok, error)`` triple; the ``error`` key is
    included only for a failed entry, so a successful grant is a clean
    ``{"email": ..., "ok": True}``.
    """
    shaped: list[dict] = []
    for email, ok, error in outcomes:
        entry: dict = {"email": email, "ok": ok}
        if not ok and error:
            entry["error"] = error
        shaped.append(entry)
    return shaped


def list_result(items: list[dict], *, truncated: bool, hint: str) -> dict:
    """Envelope for ``list_artifacts``: the page of *items* plus ``total``,
    ``truncated``, and a ``hint``.

    ``total`` is computed as ``len(items)`` -- the count returned in *this*
    response, **not** a full-bucket count (a true prefix total would require
    exhausting every page; deferred per plan Non-Goals). Computing it here
    rather than accepting it as a parameter makes the ``total == len(items)``
    contract impossible to violate. ``truncated`` signals that more objects
    exist beyond the requested limit, and ``hint`` tells the caller to narrow
    the prefix or raise the limit.
    """
    return {
        "items": items,
        "total": len(items),
        "truncated": truncated,
        "hint": hint,
    }


# ---------------------------------------------------------------------------
# Structured errors and message templates (KTD11 / R17)
# ---------------------------------------------------------------------------


def error(message: str, *, hint: str | None = None) -> dict:
    """Build a structured, application-level error dict.

    The ``"isError": True`` key is an **application-level marker** (KTD11), not
    the MCP protocol ``isError`` flag -- a returned dict is always a successful
    ``CallToolResult`` under FastMCP. An optional ``hint`` carries the next
    step.
    """
    result: dict = {"isError": True, "error": message}
    if hint:
        result["hint"] = hint
    return result


def ubla_disabled_message(bucket: str) -> str:
    """Actionable message for a UBLA-on bucket: names the *bucket* and the exact
    command to disable Uniform Bucket-Level Access (KTD4 / AE6)."""
    return (
        f"Bucket {bucket!r} has Uniform Bucket-Level Access (UBLA) enabled, which "
        f"blocks the per-object ACL grant this server uses to share artifacts. "
        f"Disable it with: "
        f"gcloud storage buckets update gs://{bucket} --no-uniform-bucket-level-access"
    )


def not_found_message(reference: str) -> str:
    """Actionable not-found message naming the missing *reference* and the next
    step (R17 / AE5)."""
    return (
        f"No artifact found for {reference!r}. It may have been deleted, or the "
        f"key/URL is wrong -- call list_artifacts to see the current artifacts."
    )
