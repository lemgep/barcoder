"""Container healthcheck: verifies the HTTP endpoint and database status."""

from __future__ import annotations

import http.client
import os
import sys


def main() -> int:
    port = os.environ.get("WEB_PORT", "8000")
    try:
        connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
        try:
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            body = response.read(4096).decode("utf-8", errors="replace")
        finally:
            connection.close()
    except (OSError, ValueError) as exc:
        print(f"Healthcheck failed: {exc}", file=sys.stderr)
        return 1

    if response.status == 200:
        return 0
    print(f"Healthcheck failed: HTTP {response.status}: {body}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
