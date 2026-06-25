import sys

from .config import Config
from .server import mcp


def main():
    # Fail fast on a bad environment BEFORE starting the transport. Delegating to
    # Config.from_env() collapses the previously-duplicated required-var list into
    # one source of truth and inherits its full validation — including the
    # absolute-path check on GOOGLE_APPLICATION_CREDENTIALS that the old
    # presence-only loop here silently skipped (RF6 / R12). The lifespan re-loads
    # the config; this early call only validates.
    try:
        Config.from_env()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    mcp.run()
