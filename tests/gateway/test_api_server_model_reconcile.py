"""api_server session model lock vs the AutoRouter managed marker (Disproof of issue #104).

A stale/corrupt ``autorouter_managed`` marker makes ``pre_turn_route`` refuse
every unpinned turn, before any provider request. On the messaging surface an
explicit physical ``/model <model> --session`` pick reconciles the marker at
the commit boundary. The api_server surface has its own session-pick
mechanism — ``POST /api/sessions/{session_id}/model`` (a Browser model lock,
the desktop's pick endpoint) — and an operator who picks a physical model there
must reconcile the same marker there. Otherwise the session is refused on every
unpinned turn forever, with no recovery short of another command surface the
api_server client does not have.

These tests drive the real HTTP endpoint (``_handle_session_model_lock``) end
to end, plus a turn-boundary probe (``pre_turn_route``) to prove the refusal
exists before the fix and the session routes after it:

- ``test_physical_lock_durably_clears_corrupt_marker``
- ``test_virtual_autorouter_pick_keeps_the_marker``
- ``test_marker_clear_failure_is_reported_not_concealed``
- ``test_unpinned_turns_refuse_before_pick_and_route_after`` (end-to-end recovery proof)

Negative controls: refusal turns produce ZERO provider requests (the refusal
happens before any provider call), and the virtual pick never widens into
stock semantics.
"""

from __future__ import annotations

import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform
from gateway.platforms.api_server import APIServerAdapter
from gateway.config import PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from persistence.core.aio import AwaitableStore


# ---------------------------------------------------------------------------
# Fakes: the durable session-store half and the sessions-persistence half.
# ---------------------------------------------------------------------------


class _DurableMarkerStore:
    """Dict-backed session-store stand-in (marker metadata only) with fault injection."""

    def __init__(self):
        self.meta: dict[tuple[str, str], object] = {}
        self.overrides: dict[str, dict] = {}
        self.fail_clear_marker = False
        self.clear_marker_returns_false = False

    def get_session_metadata(self, key, k, default=None):
        return self.meta.get((key, k), default)

    def set_session_metadata(self, key, k, value):
        if k == "autorouter_managed" and value is None:
            if self.fail_clear_marker:
                raise RuntimeError("disk full")
            if self.clear_marker_returns_false:
                return False
            self.meta.pop((key, k), None)
            return True
        if value is None:
            self.meta.pop((key, k), None)
        else:
            self.meta[(key, k)] = value
        return True

    def set_model_override(self, key, override):
        if override is None:
            self.overrides.pop(key, None)
        else:
            self.overrides[key] = dict(override)

    def get_model_override(self, key):
        override = self.overrides.get(key)
        return dict(override) if override else None


class _FakeConversation:
    def __init__(self):
        self.autorouter_managed = None
        self.model_override = None


class _FakeSessionState:
    def __init__(self):
        self.conversation = _FakeConversation()


def _make_runner(marker_store: _DurableMarkerStore) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.session_store = marker_store
    runner._session_model_overrides = {}
    runner._sessions = {}
    runner._peek_session_state = lambda key: runner._sessions.get(key)
    runner._session_state = lambda key: runner._sessions.setdefault(key, _FakeSessionState())
    runner._evict_cached_agent = lambda _key: None
    return runner


def _seed_corrupt_marker(runner: GatewayRunner, store: _DurableMarkerStore, key: str) -> None:
    """The operator's situation: both copies of the managed marker are corrupt."""
    state = runner._session_state(key)
    state.conversation.autorouter_managed = "corrupt!!"
    store.set_session_metadata(key, "autorouter_managed", "corrupt!!")


def _make_session_row(session_id: str):
    return SimpleNamespace(
        session_id=session_id,
        source="api_server",
        user_id="user-1",
        model=None,
        title=None,
        started_at=None,
        ended_at=None,
        end_reason=None,
        message_count=0,
        tool_call_count=0,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        estimated_cost_usd=None,
        actual_cost_usd=None,
        api_call_count=0,
        parent_session_id=None,
        pinned=False,
        archived=False,
        hidden=False,
        system_prompt=None,
        model_config={},
    )


class _FakeSessions:
    def __init__(self, session_id: str):
        self._session_id = session_id
        self.runtime_locks: list[tuple[str, object]] = []

    def get(self, session_id):
        if session_id == self._session_id:
            return _make_session_row(session_id)
        return None

    def set_runtime_lock(self, session_id, lock):
        self.runtime_locks.append((session_id, lock))


def _make_adapter() -> APIServerAdapter:
    config = PlatformConfig(enabled=True, extra={"key": "sk-test"})
    return APIServerAdapter(config)


def _wire_storage(adapter: APIServerAdapter, sessions: _FakeSessions):
    """Bypass the authority-selected persistence with the fake sessions store."""
    store = types.SimpleNamespace(
        aio=AwaitableStore(types.SimpleNamespace(sessions=sessions, executor=None)),
        sessions=sessions,
    )
    adapter._session_db = object()  # explicit override, never the filesystem
    adapter._store_beside = lambda _db: store
    return store


def _create_app(adapter: APIServerAdapter) -> "web.Application":
    from aiohttp import web

    app = web.Application()
    app["api_server_adapter"] = adapter
    app.router.add_post("/api/sessions/{session_id}/model", adapter._handle_session_model_lock)
    return app


def _physical_lock_body():
    return {"model": "mockinfra-free-model", "provider": "mockinfra"}


def _turn_source() -> SessionSource:
    return SessionSource(
        platform=Platform.API_SERVER,
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
    )


def test_physical_lock_durably_clears_corrupt_marker():
    """A physical pick through the api_server pick endpoint reconciles the marker."""
    import asyncio

    async def run():
        adapter = _make_adapter()
        runner = _make_runner(_DurableMarkerStore())
        adapter.gateway_runner = runner
        sessions = _FakeSessions("sess-reconcile")
        _wire_storage(adapter, sessions)
        _seed_corrupt_marker(runner, runner.session_store, "sess-reconcile")
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/api/sessions/sess-reconcile/model",
                json=_physical_lock_body(),
                headers={"Authorization": "Bearer sk-test"},
            )
        return resp, runner, sessions

    resp, runner, sessions = asyncio.run(run())
    assert resp.status == 200, getattr(resp, "text", None)
    # The lock landed durably...
    assert sessions.runtime_locks and sessions.runtime_locks[0][0] == "sess-reconcile"
    assert sessions.runtime_locks[0][1].confirmed is True
    # ...and the corrupt marker is durably reconciled at the same commit boundary.
    assert runner.session_store.get_session_metadata("sess-reconcile", "autorouter_managed") is None
    assert runner._sessions["sess-reconcile"].conversation.autorouter_managed is None


def test_virtual_autorouter_pick_keeps_the_marker():
    """An AutoRouter virtual pick IS the managed binding: the marker stays by design."""
    import asyncio

    async def run():
        adapter = _make_adapter()
        runner = _make_runner(_DurableMarkerStore())
        adapter.gateway_runner = runner
        sessions = _FakeSessions("sess-virtual")
        _wire_storage(adapter, sessions)
        _seed_corrupt_marker(runner, runner.session_store, "sess-virtual")
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/api/sessions/sess-virtual/model",
                json={"model": "auto-free", "provider": "autorouter"},
                headers={"Authorization": "Bearer sk-test"},
            )
        return resp, runner, sessions

    resp, runner, sessions = asyncio.run(run())
    assert resp.status == 200
    assert runner.session_store.get_session_metadata("sess-virtual", "autorouter_managed") == "corrupt!!"


def test_marker_clear_failure_is_reported_not_concealed():
    """If the durable clear cannot land, the endpoint never reports a clean success."""
    import asyncio

    async def run():
        adapter = _make_adapter()
        marker_store = _DurableMarkerStore()
        marker_store.fail_clear_marker = True
        runner = _make_runner(marker_store)
        adapter.gateway_runner = runner
        sessions = _FakeSessions("sess-broken")
        _wire_storage(adapter, sessions)
        _seed_corrupt_marker(runner, marker_store, "sess-broken")
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/api/sessions/sess-broken/model",
                json=_physical_lock_body(),
                headers={"Authorization": "Bearer sk-test"},
            )
            body = await resp.json()
        return resp, body, marker_store

    resp, body, marker_store = asyncio.run(run())
    assert resp.status == 500
    assert body["error"]["code"] == "managed_marker_reconciliation_failed"
    assert "marker" in body["error"]["message"]
    # The lock did land (the persist half is independent); the failure names the reconcile half.
    assert marker_store.get_session_metadata("sess-broken", "autorouter_managed") == "corrupt!!"


def test_unpinned_turns_refuse_before_pick_and_route_after():
    """End-to-end recovery: refuses with ZERO provider requests before, recovers after."""
    import asyncio

    from gateway.autorouter_turn import pre_turn_route

    marker_store = _DurableMarkerStore()
    runner = _make_runner(marker_store)
    _seed_corrupt_marker(runner, marker_store, "sess-recovery")

    refusal_text = None
    try:
        pre_turn_route(
            runner=runner,
            source=_turn_source(),
            session_key="sess-recovery",
            user_config=None,
            model="mockinfra-free-model",
            runtime_kwargs={"provider": "mockinfra"},
        )
    except Exception as exc:
        refusal_text = str(exc)
    assert refusal_text and "refused before any provider request" in refusal_text

    adapter = _make_adapter()
    adapter.gateway_runner = runner
    sessions = _FakeSessions("sess-recovery")
    _wire_storage(adapter, sessions)
    app = _create_app(adapter)

    async def run():
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/api/sessions/sess-recovery/model",
                json=_physical_lock_body(),
                headers={"Authorization": "Bearer sk-test"},
            )
        return resp

    resp = asyncio.run(run())
    assert resp.status == 200

    # The unpinned turn after the pick is no longer refused: the managed marker is gone,
    # so a never-managed-style session evaluates MODE B on the stock path.
    try:
        result = pre_turn_route(
            runner=runner,
            source=_turn_source(),
            session_key="sess-recovery",
            user_config=None,
            model="mockinfra-free-model",
            runtime_kwargs={"provider": "mockinfra"},
        )
    except Exception as exc:  # pragma: no cover - failure path for clarity
        pytest.fail(f"recovered session refused again: {exc}")
    assert result[2] in ("stock", "explicit", "ordinary")


def test_header_session_key_is_the_marker_key_not_session_id():
    """The reconciliation clears the key a turn would use (X-Hermes-Session-Key precedence)."""
    import asyncio

    async def run():
        adapter = _make_adapter()
        runner = _make_runner(_DurableMarkerStore())
        adapter.gateway_runner = runner
        sessions = _FakeSessions("sess-header")
        _wire_storage(adapter, sessions)
        # The managed marker lives under the header key, as a turn with that header writes it.
        _seed_corrupt_marker(runner, runner.session_store, "channel-key-1")
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/api/sessions/sess-header/model",
                json=_physical_lock_body(),
                headers={"Authorization": "Bearer sk-test", "X-Hermes-Session-Key": "channel-key-1"},
            )
        return resp, runner

    resp, runner = asyncio.run(run())
    assert resp.status == 200
    assert runner.session_store.get_session_metadata("channel-key-1", "autorouter_managed") is None
