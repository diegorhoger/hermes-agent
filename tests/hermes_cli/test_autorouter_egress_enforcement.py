"""Dispatch 165 — OS-enforced (not merely observed) outbound denial proof.

Uses macOS ``sandbox-exec`` (Seatbelt), a first-party per-process sandbox that
does NOT change system-wide network controls or require elevated privileges.
The profile allows loopback traffic and denies every other outbound connection.

This proves the *environment can enforce* the boundary. It does not claim that
the packaged app itself installs or relies on this sandbox in production.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

PROFILE = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
"""

PROBE = r"""
import json, os, socket, sys
def probe(host, port):
    s = socket.socket(); s.settimeout(1.5)
    try:
        s.connect((host, int(port)))
        return 'connected'
    except OSError as exc:
        return f'errno={exc.errno}'
port = os.environ['MOCK_PORT']
result = {'loopback': probe('127.0.0.1', port), 'nonloopback': probe('10.255.255.1', 9)}
print('RESULT ' + json.dumps(result))
"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        raw = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="sandbox-exec unavailable (non-macOS)")
def test_sandbox_denies_nonloopback_and_allows_loopback(tmp_path):
    profile = tmp_path / "deny-nonloopback.sb"
    profile.write_text(PROFILE, encoding="utf-8")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = dict(os.environ, MOCK_PORT=str(server.server_port))
        completed = subprocess.run(
            ["sandbox-exec", "-f", str(profile), sys.executable, "-c", PROBE],
            capture_output=True, text=True, timeout=30, env=env,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert completed.returncode == 0, completed.stderr
    line = next((l for l in completed.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, completed.stdout
    result = json.loads(line[len("RESULT "):])
    assert result["loopback"] == "connected", result
    # EPERM (errno 1) means the OS refused the connect, not that the host was
    # merely unreachable (which would be a timeout or ENETUNREACH).
    assert result["nonloopback"] == "errno=1", result
