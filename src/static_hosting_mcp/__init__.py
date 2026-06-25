import os
import sys

from .server import mcp


def main():
    required = [
        "GCS_BUCKET",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ]
    missing = [var for var in required if not os.environ.get(var)]
    if missing:
        print(
            f"Missing required environment variables: {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)
    mcp.run()
