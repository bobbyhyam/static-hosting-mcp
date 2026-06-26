"""GCS configuration loaded and validated from the environment (U2).

``Config.from_env()`` reads the operator's environment into a frozen dataclass
and fails fast — with a single ``ValueError`` naming *every* missing required
variable — before the server constructs a GCS client. Aggregating all missing
variables into one error (rather than failing on the first) is a deliberate
fail-fast configuration pattern.

The key path is credential-adjacent: it is marked ``repr=False`` so a stray
``repr()``/log of the config never leaks it (R11, KTD8). The bucket name is not a
secret and legitimately appears in returned URLs, so it stays visible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Environment variable names — the single source of truth, reused by the
# fail-fast aggregation below and referenced by U8's operator docs.
ENV_BUCKET = "GCS_BUCKET"
ENV_CREDENTIALS = "GOOGLE_APPLICATION_CREDENTIALS"
ENV_PROJECT = "GCS_PROJECT_ID"
# Operator-controlled allow-list root for publish_artifact's source_path, and the
# upload size cap. Both gate the local-file read that the P0 review finding hardens
# (RF1): source_path is default-denied unless ARTIFACT_SOURCE_ROOT names a directory
# to confine reads to.
ENV_SOURCE_ROOT = "ARTIFACT_SOURCE_ROOT"
ENV_MAX_BYTES = "ARTIFACT_MAX_BYTES"

# Default maximum artifact size (inline content or source_path file): 100 MiB.
# Bounds the in-memory read so a caller-controlled path cannot drive the process
# to OOM; override with ARTIFACT_MAX_BYTES (RF1).
DEFAULT_MAX_ARTIFACT_BYTES = 100 * 1024 * 1024


def _parse_max_bytes(raw: str | None) -> int:
    """Parse ``ARTIFACT_MAX_BYTES`` into a positive int, or fall back to default.

    A malformed value is a fail-fast ``ValueError`` (caught at startup alongside the
    other config errors), not a silent fallback, so a typo cannot quietly disable the
    cap.
    """
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_ARTIFACT_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"{ENV_MAX_BYTES} must be a positive integer number of bytes, got {raw!r}."
        ) from None
    if value <= 0:
        raise ValueError(
            f"{ENV_MAX_BYTES} must be a positive integer number of bytes, got {value}."
        )
    return value


@dataclass(frozen=True)
class Config:
    """Validated GCS configuration held on the lifespan ``AppContext``.

    Frozen so the configuration is read-only for the process lifetime once the
    lifespan has built it. ``key_path`` carries ``repr=False`` so it is omitted
    from ``repr()`` (and therefore from accidental logs / error dumps); the tool
    surface never reads it (KTD8).
    """

    bucket: str
    # Credential-adjacent: absolute path to the service-account JSON key. Kept
    # out of repr()/logs so it cannot leak into tool output (R11).
    key_path: str = field(repr=False)
    # Optional GCP project override; ``None`` means it is derived from the key
    # or ADC at client-construction time (U3).
    project: str | None = None
    # Absolute, canonicalized directory that publish_artifact's source_path reads
    # are confined to. ``None`` (the default) means source_path is **denied** — the
    # operator must opt in by setting ARTIFACT_SOURCE_ROOT (RF1 / security S1).
    artifact_source_root: str | None = None
    # Upper bound on an uploaded artifact's byte size (inline or source_path).
    artifact_max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES

    @classmethod
    def from_env(cls) -> Config:
        """Load and validate config from the environment, failing fast.

        ``GCS_BUCKET`` and ``GOOGLE_APPLICATION_CREDENTIALS`` are required;
        ``GCS_PROJECT_ID`` is optional. Every missing required variable is named
        in one ``ValueError`` (fail fast, R11). A relative
        ``GOOGLE_APPLICATION_CREDENTIALS`` is rejected with an actionable
        message: a stdio server is launched from an unpredictable working
        directory, so only an absolute key path is reliable (ASM2, KTD5).
        """
        bucket = os.environ.get(ENV_BUCKET, "")
        key_path = os.environ.get(ENV_CREDENTIALS, "")
        # Treat an unset *or* empty project as "not provided" so it is derived
        # from the key/ADC downstream rather than passed through as "".
        project = os.environ.get(ENV_PROJECT) or None

        missing = []
        if not bucket:
            missing.append(ENV_BUCKET)
        if not key_path:
            missing.append(ENV_CREDENTIALS)
        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}"
            )

        if not os.path.isabs(key_path):
            raise ValueError(
                f"{ENV_CREDENTIALS} must be an absolute path to the "
                "service-account JSON key, but a relative path was given. A stdio "
                "server is launched from an unpredictable working directory, so set "
                f"{ENV_CREDENTIALS} to the file's absolute path (or use keyless ADC)."
            )

        # source_path allow-list root: optional, but if set it must be absolute
        # (same unpredictable-working-directory reasoning as the key path) and is
        # canonicalized so the publish-time confinement check compares realpaths.
        source_root = os.environ.get(ENV_SOURCE_ROOT) or None
        if source_root is not None:
            if not os.path.isabs(source_root):
                raise ValueError(
                    f"{ENV_SOURCE_ROOT} must be an absolute path to the directory "
                    "that source_path uploads are allowed to read from, but a "
                    "relative path was given."
                )
            source_root = os.path.realpath(source_root)

        return cls(
            bucket=bucket,
            key_path=key_path,
            project=project,
            artifact_source_root=source_root,
            artifact_max_bytes=_parse_max_bytes(os.environ.get(ENV_MAX_BYTES)),
        )
