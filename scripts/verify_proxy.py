#!/usr/bin/env python3
"""Exercise the production Caddy template against isolated loopback upstreams."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def available_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def request(url):
    # Ignore workstation HTTP proxies; this fixture never leaves loopback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(url, timeout=2)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read()


class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/rejected":
            status, content_type, body = 409, "application/json", b'{"detail":"fixture conflict"}'
        else:
            status, content_type, body = 200, "text/html; charset=utf-8", b"<!doctype html>ready"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if self.path == "/download":
            self.send_header("Content-Disposition", 'attachment; filename="fixture.html"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def verify(caddy, template):
    proxy_port, upstream_port = available_port(), available_port()
    while upstream_port == proxy_port:
        upstream_port = available_port()
    config = template.read_text().replace("maimai.example.com", f"http://127.0.0.1:{proxy_port}")
    config = config.replace("127.0.0.1:8766", f"127.0.0.1:{upstream_port}")
    base = f"http://127.0.0.1:{proxy_port}"
    with tempfile.TemporaryDirectory(prefix="mai-proxy-check-") as directory:
        root = Path(directory)
        config_path = root / "Caddyfile"
        config_path.write_text("{\n\tadmin off\n\tauto_https off\n}\n" + config)
        environment = {
            **os.environ,
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
        }
        with (root / "caddy.log").open("w+") as log:
            process = subprocess.Popen(
                [caddy, "run", "--config", str(config_path), "--adapter", "caddyfile"],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
            )
            try:
                for _ in range(50):
                    if process.poll() is not None:
                        log.seek(0)
                        raise AssertionError("Caddy did not start: " + log.read())
                    try:
                        request(base)
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise AssertionError("Isolated Caddy listener did not become ready")

                for path in ("/?view=completion", "/api/venues/charts?name=fixture", "/api"):
                    status, headers, body = request(base + path)
                    assert status == 503, (path, status)
                    assert headers.get("Cache-Control") == "no-store", headers
                    assert headers.get("Content-Disposition") is None, headers
                    assert headers.get("Retry-After") == "5", headers
                    assert body, "A failed navigation must not become an empty download"
                    if path.startswith("/api"):
                        assert headers.get_content_type() == "application/json", headers
                        assert isinstance(json.loads(body).get("detail"), str), body
                    else:
                        assert headers.get_content_type() == "text/html", headers
                        assert body.startswith(b"<!doctype html>"), body

                with ThreadingHTTPServer(("127.0.0.1", upstream_port), Upstream) as upstream:
                    worker = threading.Thread(target=upstream.serve_forever, daemon=True)
                    worker.start()
                    try:
                        status, headers, body = request(base + "/?view=completion")
                        assert (status, body) == (200, b"<!doctype html>ready")
                        assert headers.get("Retry-After") is None, headers
                        status, _, body = request(base + "/api/rejected")
                        assert (status, json.loads(body)) == (
                            409,
                            {"detail": "fixture conflict"},
                        )
                        status, headers, _ = request(base + "/download")
                        assert status == 200
                        assert headers.get("Content-Disposition") == (
                            'attachment; filename="fixture.html"'
                        )
                    finally:
                        upstream.shutdown()
                        worker.join(timeout=5)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print(
        "Proxy check passed: typed outage responses, automatic recovery, upstream errors preserved"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caddy", default=shutil.which("caddy"))
    parser.add_argument("--config", type=Path, default=REPO / "deploy/Caddyfile.example")
    args = parser.parse_args()
    if not args.caddy:
        parser.error("Install Caddy or pass --caddy /path/to/caddy")
    verify(args.caddy, args.config)


if __name__ == "__main__":
    main()
