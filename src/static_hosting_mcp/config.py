"""GCS configuration loaded and validated from the environment (U2).

``Config.from_env()`` reads the operator's environment into a frozen dataclass
and fails fast — with a single ``ValueError`` naming *every* missing required
variable — before the server constructs a GCS client. This mirrors the
``ultimate-brain-mcp`` reference ``UBConfig.from_env()`` missing-var aggregation
pattern.

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

        return cls(bucket=bucket, key_path=key_path, project=project)
