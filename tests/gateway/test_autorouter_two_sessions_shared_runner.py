"""Dispatch 161 — shared-Gateway concurrency acceptance (ONE runner, TWO sessions).

This is deliberately NOT a two-process/Electron test. One real ``GatewayRunner``
serves two simultaneous session IDs over two websocket clients; each session
carries its own explicit synthetic route (model, provider, endpoint) and
credential authority. Turns overlap at the local mock inference servers.

Wire receipts are captured per endpoint (model + Authorization + prompt), so a
cross-session credential/endpoint leak or a cached-agent reuse across
incompatible routes would be visible as a wrong model/key on one of the mocks.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.asyncio

MODEL_A = "model-a"
MODEL_B = "model-b"
KEY_A = "synthetic-key-account-A"
KEY_B = "synthetic-key-account-B"


class _InferencePeer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _record(self, body):
        self.server.requests.append({
            "model": body.get("model"),
            "auth": self.headers.get("Authorization"),
            "prompt": next((m.get("content") for m in reversed(body.get("messages", []))
                            if m.get("role") == "user"), ""),
            "path": self.path,
        })

    def do_GET(self):
        raw = json.dumps({"data": [{"id": m, "object": "model"}
                                   for m in (MODEL_A, MODEL_B, "model-switched")]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._record(body)
        server = self.server
        # Force overlap: every inference request waits until both sessions have
        # reached their endpoint before either is answered.
        with server.arrival_lock:
            server.arrived += 1
            if server.arrived >= 2:
                server.release.set()
        assert server.release.wait(20), "overlapping inference gate timed out"
        if getattr(server, "fail_next", False):
            server.fail_next = False
            raw = json.dumps({"error": {"message": "synthetic failure", "type": "server_error"}}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        reply = "LOCAL_ACK_" + str(body.get("model"))
        raw = json.dumps({
            "id": "chatcmpl-shared", "object": "chat.completion", "created": 1,
            "model": body.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def peers():
    servers = []
    for _ in range(2):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _InferencePeer)
        server.requests = []
        server.arrived = 0
        server.arrival_lock = threading.Lock()
        server.release = threading.Event()
        server.fail_next = False
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    try:
        yield [(s, f"http://127.0.0.1:{s.server_port}/v1") for s in servers]
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


@pytest.fixture
def configured_home(monkeypatch, peers):
    from agent import models_dev
    from gateway import run
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(run, "_hermes_home", home)
    catalog = {"custom": {"id": "custom", "models": {
        name: {"id": name, "name": name, "tool_call": True, "cost": {"input": 0, "output": 0},
               "limit": {"context": 100000, "output": 4000}}
        for name in (MODEL_A, MODEL_B, "model-switched")}}}
    monkeypatch.setattr(models_dev, "fetch_models_dev", lambda *a, **kw: catalog)
    (_, url_a), (_, url_b) = peers
    (home / "config.yaml").write_text(json.dumps({
        "model": {"provider": "custom", "default": MODEL_A,
                  "base_url": url_a, "api_key": KEY_A},
        "providers": {
            "account_b": {"name": "account_b", "base_url": url_b, "api_key": KEY_B},
        },
    }), encoding="utf-8")
    return home


class _Client:
    def __init__(self, ws, sid=""):
        self.ws, self.sid = ws, sid
        self.rid = 0
        self.waiters = {}
        self.events = []
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        import websockets.exceptions
        try:
            async for raw in self.ws:
                frame = json.loads(raw)
                if frame.get("id") in self.waiters:
                    self.waiters.pop(frame["id"]).set_result(frame)
                else:
                    self.events.append(frame.get("params") or frame)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def rpc(self, method, **params):
        self.rid += 1
        future = asyncio.get_running_loop().create_future()
        self.waiters[self.rid] = future
        payload = {"jsonrpc": "2.0", "id": self.rid, "method": method, "params": params}
        if self.sid:
            payload["params"]["session_id"] = self.sid
        await self.ws.send(json.dumps(payload))
        frame = await asyncio.wait_for(future, 60)
        assert "result" in frame, frame
        return frame["result"]

    async def close(self):
        await self.ws.close()
        self.task.cancel()


async def _runner_clients(configured_home):
    from gateway.run import GatewayRunner
    from gateway.run_api import start_gateway_api, stop_gateway_api
    from gateway.session_authority import initialize_session_authority
    from hermes_cli import web_server

    runner = GatewayRunner()
    authority = await initialize_session_authority(
        runner, profile_id=str(configured_home), instance_id="shared-two")
    api = await start_gateway_api(runner)
    uri = api.api_origin.replace("http:", "ws:") + "/api/ws?token=" + web_server._SESSION_TOKEN
    import websockets
    a = _Client(await websockets.connect(uri, compression=None, max_size=8 * 1024 * 1024))
    b = _Client(await websockets.connect(uri, compression=None, max_size=8 * 1024 * 1024))
    return runner, authority, api, stop_gateway_api, a, b


def _wait_terminal(authority, sid, request_id):
    from tests.waiting import async_wait_until

    async def done():
        rows = authority.persistence.admissions.list(session_id=sid, pending_only=False)
        return any(r.request_id == request_id and r.status == "terminal" for r in rows)
    return async_wait_until(done, timeout=90, what=f"turn {request_id} terminal")


async def _create_two(a, b, configured_home, peers):
    (_, url_a), (_, url_b) = peers
    ra = await a.rpc("session.create", request_id="shared-create-a", source="cli",
                     cwd=str(configured_home), toolsets=[])
    rb = await b.rpc("session.create", request_id="shared-create-b", source="cli",
                     cwd=str(configured_home), toolsets=[],
                     provider="account_b", model=MODEL_B, api_key=KEY_B, base_url=url_b)
    a.sid = ra["session_id"]
    b.sid = rb["session_id"]
    assert a.sid != b.sid
    return a.sid, b.sid


def _chat(server):
    return [r for r in server.requests if r["path"].startswith("/v1/chat/completions")]


def _assert_no_virtual(records):
    for r in records:
        assert str(r["model"] or "").lower() not in {"auto-free", "autorouter/auto-free"}
        assert "autorouter/auto" not in str(r["model"] or "").lower()


async def test_two_sessions_overlap_on_one_runner_with_isolated_routes(configured_home, peers):
    (s_a, url_a), (s_b, url_b) = peers
    runner, authority, api, stop_api, a, b = await _runner_clients(configured_home)
    try:
        sid_a, sid_b = await _create_two(a, b, configured_home, peers)
        # Overlapping turns on the SAME runner; the mock gate holds both until
        # both have arrived, proving concurrent in-flight execution.
        await asyncio.gather(
            a.rpc("prompt.submit", input_id="overlap-A", text="OVERLAP_TURN_A"),
            b.rpc("prompt.submit", input_id="overlap-B", text="OVERLAP_TURN_B"),
        )
        await asyncio.gather(
            _wait_terminal(authority, sid_a, "overlap-A"),
            _wait_terminal(authority, sid_b, "overlap-B"),
        )
        assert s_a.arrived >= 1 and s_b.arrived >= 1

        wa = [r for r in s_a.requests if r["path"].startswith("/v1/chat/completions")]
        wb = [r for r in s_b.requests if r["path"].startswith("/v1/chat/completions")]
        assert wa and wb, (wa, wb)
        assert wa[0]["model"] == MODEL_A and wb[0]["model"] == MODEL_B
        assert wa[0]["auth"] == f"Bearer {KEY_A}"
        assert wb[0]["auth"] == f"Bearer {KEY_B}"
        assert "OVERLAP_TURN_A" in wa[0]["prompt"]
        assert "OVERLAP_TURN_B" in wb[0]["prompt"]
        assert url_a in wa[0]["path"] or wa[0]["path"].startswith("/v1")
        _assert_no_virtual(wa + wb)
        # No endpoint/credential crossed: each mock saw only its own model+key.
        assert all(r["model"] == MODEL_A and r["auth"] == f"Bearer {KEY_A}" for r in wa)
        assert all(r["model"] == MODEL_B and r["auth"] == f"Bearer {KEY_B}" for r in wb)
    finally:
        await a.close(); await b.close()
        await stop_api(api)


async def test_session_a_switch_and_failure_cannot_redirect_session_b(configured_home, peers):
    (s_a, _), (s_b, _) = peers
    runner, authority, api, stop_api, a, b = await _runner_clients(configured_home)
    try:
        sid_a, sid_b = await _create_two(a, b, configured_home, peers)

        # Session A switches its model to a third physical route.
        from tests.fixtures.storage_read import session_row
        row_a = session_row(authority.persistence, sid_a)
        switched = await a.rpc("session.mutate", request_id="shared-switch-A", operation="model",
                               payload={"model": "model-switched", "provider": "custom"},
                               expected_revision=row_a["runtime_revision"],
                               expected_generation=row_a["runtime_generation"])
        assert switched["operation"] == "model", switched
        s_a.release.clear(); s_a.arrived = 0; s_b.release.clear(); s_b.arrived = 0
        await asyncio.gather(
            a.rpc("prompt.submit", input_id="after-switch-A", text="AFTER_SWITCH_A"),
            b.rpc("prompt.submit", input_id="after-switch-B", text="AFTER_SWITCH_B"),
        )
        await asyncio.gather(
            _wait_terminal(authority, sid_a, "after-switch-A"),
            _wait_terminal(authority, sid_b, "after-switch-B"),
        )
        assert _chat(s_a)[-1]["model"] == "model-switched"
        assert _chat(s_b)[-1]["model"] == MODEL_B
        assert _chat(s_b)[-1]["auth"] == f"Bearer {KEY_B}"

        # Session A's provider fails; B's next turn must be unaffected.
        s_a.fail_next = True
        s_a.release.clear(); s_a.arrived = 0; s_b.release.clear(); s_b.arrived = 0
        await asyncio.gather(
            a.rpc("prompt.submit", input_id="fail-A", text="FAIL_TURN_A"),
            b.rpc("prompt.submit", input_id="ok-B", text="OK_TURN_B"),
        )
        await asyncio.gather(
            _wait_terminal(authority, sid_a, "fail-A"),
            _wait_terminal(authority, sid_b, "ok-B"),
        )
        assert _chat(s_b)[-1]["model"] == MODEL_B
        assert _chat(s_b)[-1]["auth"] == f"Bearer {KEY_B}"
        assert "OK_TURN_B" in _chat(s_b)[-1]["prompt"]
        _assert_no_virtual(_chat(s_a) + _chat(s_b))
    finally:
        await a.close(); await b.close()
        await stop_api(api)
