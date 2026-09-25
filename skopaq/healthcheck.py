"""Container health checks, one per service, without curl.

Usage::

    python -m skopaq.healthcheck api
        GET http://127.0.0.1:${PORT:-8000}/health (4 s timeout). Healthy if the
        status is 200 and the JSON body has "status": "ok".

    python -m skopaq.healthcheck heartbeat PATH [MAX_AGE_SECONDS=180]
        Healthy if PATH exists and was modified less than MAX_AGE seconds ago.
        The Telegram bot and the scheduler touch it (SKOPAQ_HEARTBEAT_FILE).

Exit codes: 0 healthy, 1 unhealthy (reason on stderr), 2 usage error.

Imports only the standard library (``skopaq/__init__.py`` is tiny), and the
HTTP client only for the ``api`` check, so a check starts fast even on a busy host.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

_USAGE = (
    "usage: python -m skopaq.healthcheck api\n"
    "       python -m skopaq.healthcheck heartbeat PATH [MAX_AGE_SECONDS]"
)


def check_api(port: int, timeout: float = 4.0) -> tuple[bool, str]:
    """GET /health on the local API."""
    # http.client, not urllib: it never goes through HTTP(S)_PROXY (an egress proxy set
    # in .env must not break a local check) and it imports less.
    import http.client

    url = f"http://127.0.0.1:{port}/health"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        status, body = resp.status, resp.read()
    except (OSError, http.client.HTTPException) as exc:  # refused, timeout, bad response
        return False, f"{url}: {exc}"
    finally:
        conn.close()
    if status != 200:
        return False, f"{url}: HTTP {status}"
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return False, f"{url}: response is not JSON"
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return False, f"{url}: status is not 'ok'"
    return True, f"{url}: ok"


def check_heartbeat(path: Path, max_age: float, now: Optional[float] = None) -> tuple[bool, str]:
    """The heartbeat file exists and is younger than *max_age* seconds."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False, f"{path}: no heartbeat file"
    age = (time.time() if now is None else now) - mtime
    if age > max_age:
        return False, f"{path}: last heartbeat {age:.0f}s ago (max {max_age:.0f}s)"
    return True, f"{path}: heartbeat {age:.0f}s ago"


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        if args[:1] == ["api"] and len(args) == 1:
            ok, detail = check_api(int(os.environ.get("PORT") or 8000))
        elif args[:1] == ["heartbeat"] and len(args) in (2, 3):
            max_age = float(args[2]) if len(args) == 3 else 180.0
            ok, detail = check_heartbeat(Path(args[1]), max_age)
        else:
            print(_USAGE, file=sys.stderr)
            return 2
    except ValueError as exc:  # a non-numeric PORT or MAX_AGE
        print(f"{exc}\n{_USAGE}", file=sys.stderr)
        return 2
    if not ok:
        print(f"unhealthy: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
