"""Container health checks (skopaq/healthcheck.py)."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from skopaq import healthcheck


def test_fresh_heartbeat_is_healthy(tmp_path):
    beat = tmp_path / "hb"
    beat.touch()
    assert healthcheck.main(["heartbeat", str(beat), "180"]) == 0


def test_stale_heartbeat_is_unhealthy(tmp_path):
    beat = tmp_path / "hb"
    beat.touch()
    old = time.time() - 600
    os.utime(beat, (old, old))
    assert healthcheck.main(["heartbeat", str(beat), "180"]) == 1
    assert healthcheck.main(["heartbeat", str(beat)]) == 1  # default max age 180 s


def test_missing_heartbeat_is_unhealthy(tmp_path):
    assert healthcheck.main(["heartbeat", str(tmp_path / "missing")]) == 1


@pytest.mark.parametrize(
    "args", [[], ["nope"], ["heartbeat"], ["api", "extra"], ["heartbeat", "x", "soon"]]
)
def test_bad_arguments_are_a_usage_error(args):
    assert healthcheck.main(args) == 2


def _serve(status: int, body: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [(200, {"status": "ok"}, 0), (500, {"status": "ok"}, 1), (200, {"status": "down"}, 1)],
)
def test_api(monkeypatch, status, body, expected):
    server = _serve(status, body)
    try:
        monkeypatch.setenv("PORT", str(server.server_address[1]))
        assert healthcheck.main(["api"]) == expected
    finally:
        server.shutdown()
        server.server_close()


def test_api_closed_port_is_unhealthy(monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("PORT", str(port))
    assert healthcheck.main(["api"]) == 1


def test_api_ignores_http_proxy(monkeypatch):
    server = _serve(200, {"status": "ok"})
    try:
        monkeypatch.setenv("PORT", str(server.server_address[1]))
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # nothing listens there
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        assert healthcheck.main(["api"]) == 0
    finally:
        server.shutdown()
        server.server_close()
