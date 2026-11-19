"""api_server session model lock vs the AutoRouter managed marker (Disproof of issue #104).

A stale/corrupt ``autorouter_managed`` marker makes ``pre_turn_route`` refuse
every unpinned turn, before any provider request. On the messaging surface an
explicit physical ``/model <model> --session`` pick reconciles the marker at
the commit boundary. The api_server surface has its own session-pick
mechanism — ``POST /api/sessions/{session_id}/model`` (a Browser model lock,
the desktop's pick endpoint) — and an operator who picks a physical model there
must reconcile the same marker there, or the session is refused on every
unpinned turn forever.

These tests drive the REAL HTTP endpoint end to end against the canonical,
disposable session-authority machinery (``owner``/``api`` conftest fixtures over
a fresh profile ``state.db``), with sessions created through the supported host
binding (``admit_api_turn``) exactly as production turns enter:

- ``test_physical_lock_durably_clears_corrupt_marker_on_authoritative_route``
- ``test_virtual_autorouter_pick_keeps_the_marker``
- ``test_marker_clear_failure_is_reported_not_concealed``
- ``test_unpinned_turns_refuse_before_pick_with_zero_requests_and_recover_after``
- ``test_compression_continuation_resolves_to_owner_route``
- ``test_unrelated_header_key_does_not_clear_another_sessions_marker``
- ``test_cross_session_isolation``
- ``test_secondary_profile_session_is_not_visible_to_this_adapter``
- ``test_unavailable_authority_fails_closed``
- ``test_restart_restores_session_and_marker_stays_cleared``

The stale-marker refusal happens before any provider request; the recovery is
observed at the real turn boundary (``pre_turn_route``) on the authoritative
route, not on a fabricated key.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.session import SessionSource
from gateway.session_api_turn import admit_api_turn
from gateway.config import Platform


# ---------------------------------------------------------------------------
# Authority-backed helpers
# ---------------------------------------------------------------------------

def _bind_api_session(api, session_id: str) -> str:
    """Create the session through the supported host binding; return its route."""
    admit_api_turn(api, session_id=session_id, request_id=f"req-{session_id}",
                   user_message="hello", conversation_history=[])
    from gateway.session_authorities import active_authority
    authority = active_authority(api.gateway_runner)
    return authority.sessions[session_id].route


def _attach_turn_runner_state(owner) -> dict:
    """Give the authority's bare test runner the session-state accessors the turn
    boundary and the marker helpers use, with a disposable in-memory map."""
    states: dict[str, object] = {}

    def _peek(key):
        return states.get(key)

    def _state(key):
        return states.setdefault(
            key, SimpleNamespace(conversation=SimpleNamespace(model_override=None,
                                                              autorouter_managed=None)))

    owner.runner._peek_session_state = _peek
    owner.runner._session_state = _state
    owner.runner._evict_cached_agent = lambda _key: None
    return states


def _seed_corrupt_marker(owner, states, route: str) -> None:
    """The operator's situation: both copies of the managed marker are corrupt."""
    states[route] = SimpleNamespace(conversation=SimpleNamespace(model_override=None,
                                                                 autorouter_managed="corrupt!!"))
    owner.runner.session_store.set_session_metadata(route, "autorouter_managed", "corrupt!!")


def _make_app(api) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/model", api._handle_session_model_lock)
    return app


_PHYSICAL_BODY = {"model": "mockinfra-free-model", "provider": "mockinfra"}
_AUTH = {"Authorization": "Bearer " + "test-key"}


def _set_key(api):
    api._api_key = "test-key"


async def _post_model(api, session_id: str, body: dict, headers: dict | None = None):
    _set_key(api)
    app = _make_app(api)
    hdrs = dict(_AUTH)
    if headers:
        hdrs.update(headers)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(f"/api/sessions/{session_id}/model", json=body, headers=hdrs)
        payload = await resp.json()
        return resp.status, payload


def _turn_source(owner):
    return SessionSource(platform=Platform.API_SERVER, chat_id="unused",
                         chat_type="dm", user_id="api", profile=None)


def _turn(owner, route: str):
    from gateway.autorouter_turn import pre_turn_route
    return pre_turn_route(
        runner=owner.runner, source=_turn_source(owner), session_key=route,
        user_config={}, model="stock-model",
        runtime_kwargs={"provider": "stock-provider"})


@pytest.fixture
def consumer(monkeypatch):
    """A real pre_inference_route consumer plugin in the test home, with the route
    resolver stubbed to canned physical kwargs. Yields a recorder of invocations."""
    import hermes_yaml as yaml
    calls: list[dict] = []

    source = """
def register(ctx):
    def consume(**kwargs):
        return {'status': 'route', 'provider': 'locala', 'model': 'local-model-a',
                'reason': 'fixture route'}

    ctx.register_middleware('pre_inference_route', consume)
"""
    from pathlib import Path
    import os
    home = Path(os.environ["HERMES_HOME"])
    plugin_dir = home / "plugins" / "autorouter_fixture"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text("name: autorouter_fixture\nversion: 0.1.0\n")
    (plugin_dir / "__init__.py").write_text(source)
    cfg = yaml.safe_load((home / "config.yaml").read_text()) if (home / "config.yaml").exists() else {}
    cfg = dict(cfg or {})
    cfg["plugins"] = {**(cfg.get("plugins") or {}), "enabled": ["autorouter_fixture"]}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginManager
    manager = PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr("gateway.session_policy.policy_for_source", lambda runner, source: None)

    import hermes_cli.middleware as middleware
    original = middleware.apply_pre_inference_route_middleware

    def recorder(metadata, platform=None):
        calls.append(dict(metadata))
        return original(metadata, platform=platform)

    monkeypatch.setattr(middleware, "apply_pre_inference_route_middleware", recorder)

    seen = []

    def fake_resolve(provider, target_model=None):
        seen.append((provider, target_model))
        return {"provider": provider, "api_key": f"sk-dummy-{provider}",
                "base_url": f"http://127.0.0.1:9/{provider}", "api_mode": "chat_completions",
                "requested_provider": provider, "request_overrides": {}, "capabilities": {}}

    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", fake_resolve)
    return calls, seen


def test_physical_lock_durably_clears_corrupt_marker_on_authoritative_route(api, owner):
    """A physical pick through the api_server pick endpoint reconciles the marker
    belonging to the session's authoritative route — and only that marker."""
    states = _attach_turn_runner_state(owner)
    route = _bind_api_session(api, "sess-reconcile")
    _seed_corrupt_marker(owner, states, route)

    status, payload = asyncio.run(_post_model(api, "sess-reconcile", _PHYSICAL_BODY))

    assert status == 200, payload
    # The lock landed durably on the picked session's row...
    row = owner.persistence.sessions.get("sess-reconcile")
    model_config = row.model_config
    if isinstance(model_config, str):
        model_config = json.loads(model_config or "{}")
    assert model_config.get("browser_model_lock", {}).get("model") == "mockinfra-free-model"
    assert model_config.get("browser_model_lock", {}).get("confirmed") is True
    # ...and the corrupt marker is durably reconciled at the same commit boundary.
    assert owner.runner.session_store.get_session_metadata(route, "autorouter_managed") is None
    assert states[route].conversation.autorouter_managed is None


def test_virtual_autorouter_pick_keeps_the_marker(api, owner):
    """An AutoRouter virtual pick IS the managed binding: the marker stays by design."""
    states = _attach_turn_runner_state(owner)
    route = _bind_api_session(api, "sess-virtual")
    _seed_corrupt_marker(owner, states, route)

    status, payload = asyncio.run(
        _post_model(api, "sess-virtual", {"model": "auto-free", "provider": "autorouter"}))

    assert status == 200, payload
    assert owner.runner.session_store.get_session_metadata(route, "autorouter_managed") == "corrupt!!"
    assert states[route].conversation.autorouter_managed == "corrupt!!"


def test_marker_clear_failure_is_reported_not_concealed(api, owner, monkeypatch):
    """If the durable clear cannot land, the endpoint never reports a clean success."""
    states = _attach_turn_runner_state(owner)
    route = _bind_api_session(api, "sess-broken")
    _seed_corrupt_marker(owner, states, route)

    store = owner.runner.session_store
    original = store.set_session_metadata

    def boom(key, name, value):
        if name == "autorouter_managed" and value is None:
            raise RuntimeError("disk full")
        return original(key, name, value)

    monkeypatch.setattr(store, "set_session_metadata", boom)

    status, payload = asyncio.run(_post_model(api, "sess-broken", _PHYSICAL_BODY))

    assert status == 500
    assert payload["error"]["code"] == "managed_marker_reconciliation_failed"
    assert "marker" in payload["error"]["message"]
    # The physical lock itself landed (it is independent of reconciliation)...
    row = owner.persistence.sessions.get("sess-broken")
    model_config = row.model_config
    if isinstance(model_config, str):
        model_config = json.loads(model_config or "{}")
    assert model_config.get("browser_model_lock", {}).get("model") == "mockinfra-free-model"
    # ...and the marker is honestly reported as not reconciled.
    assert store.get_session_metadata(route, "autorouter_managed") == "corrupt!!"


def test_unpinned_turns_refuse_before_pick_with_zero_requests_and_recover_after(api, owner, consumer):
    """End-to-end recovery through the real binding and turn boundary.

    Before the pick: the turn boundary refuses BEFORE the consumer is consulted and
    before any route is resolved to a physical runtime — zero provider requests.
    After the pick: the boundary routes through the consumer exactly once.
    """
    calls, seen = consumer
    states = _attach_turn_runner_state(owner)
    route = _bind_api_session(api, "sess-recovery")
    _seed_corrupt_marker(owner, states, route)

    from gateway.autorouter_turn import AutoRouterRefusal
    with pytest.raises(AutoRouterRefusal):
        _turn(owner, route)
    assert calls == []  # the routing consumer was never consulted
    assert seen == []   # no provider route was resolved

    status, payload = asyncio.run(_post_model(api, "sess-recovery", _PHYSICAL_BODY))
    assert status == 200, payload

    model, kwargs, applied = _turn(owner, route)
    assert applied == "ordinary"
    assert model == "local-model-a"
    assert len(calls) == 1
    assert calls[0]["session_id"] == route[:256]
    assert calls[0]["platform"] == "api_server"
    assert calls[0]["requested_model"] == "stock-model"
    assert calls[0]["requested_provider"] == "stock-provider"
    assert calls[0]["explicit_model"] is False
    assert seen == [("locala", "local-model-a")]


def test_compression_continuation_resolves_to_owner_route(api, owner):
    """A compression continuation id still reconciles the OWNER route the turn uses."""
    from persistence.conversation.lineage import CompressionChild
    from persistence.conversation.messages import NewMessage

    states = _attach_turn_runner_state(owner)
    admit_api_turn(api, session_id="root", request_id="first",
                   user_message="hello", conversation_history=[])
    owner.persistence.lineage.publish_compression_child(
        "root", CompressionChild(session_id="tip", source="api_server"),
        [NewMessage.from_live({"role": "assistant", "content": "summary"})],
        require_compression_lease=False)
    owner.runner.session_store.advance_compression_session(owner.sessions["root"].route, "root", "tip")
    route = owner.sessions["root"].route
    _seed_corrupt_marker(owner, states, route)

    # The operator picks through the endpoint naming the continuation id...
    status, payload = asyncio.run(_post_model(api, "tip", _PHYSICAL_BODY))
    assert status == 200, payload
    # ...and the marker belonging to the compression root's authoritative route is cleared.
    assert owner.runner.session_store.get_session_metadata(route, "autorouter_managed") is None
    assert states[route].conversation.autorouter_managed is None


def test_unrelated_header_key_does_not_clear_another_sessions_marker(api, owner):
    """A client-supplied X-Hermes-Session-Key is not ownership proof: when the header
    names another live session's route, that session's marker survives this pick
    untouched. Client headers never widen the reconciliation boundary."""
    states = _attach_turn_runner_state(owner)
    route_a = _bind_api_session(api, "sess-header-target")
    route_b = _bind_api_session(api, "sess-header-other")  # another session, same store
    assert route_a != route_b
    _seed_corrupt_marker(owner, states, route_b)
    _seed_corrupt_marker(owner, states, route_a)

    # The client names B's route in the header — a live session entry with a corrupt marker.
    status, payload = asyncio.run(
        _post_model(api, "sess-header-target", _PHYSICAL_BODY,
                    headers={"X-Hermes-Session-Key": route_b}))

    assert status == 200, payload
    assert owner.runner.session_store.get_session_metadata(route_b,
                                                            "autorouter_managed") == "corrupt!!"
    assert states[route_b].conversation.autorouter_managed == "corrupt!!"
    assert owner.runner.session_store.get_session_metadata(route_a, "autorouter_managed") is None


def test_cross_session_isolation(api, owner):
    """Session B's marker is not reconciled by session A's physical pick."""
    states = _attach_turn_runner_state(owner)
    route_a = _bind_api_session(api, "sess-isolation-a")
    route_b = _bind_api_session(api, "sess-isolation-b")
    assert route_a != route_b
    _seed_corrupt_marker(owner, states, route_a)
    _seed_corrupt_marker(owner, states, route_b)

    status, payload = asyncio.run(_post_model(api, "sess-isolation-a", _PHYSICAL_BODY))

    assert status == 200, payload
    assert owner.runner.session_store.get_session_metadata(route_a, "autorouter_managed") is None
    assert owner.runner.session_store.get_session_metadata(route_b, "autorouter_managed") == "corrupt!!"
    assert states[route_b].conversation.autorouter_managed == "corrupt!!"


def test_secondary_profile_session_is_not_visible_to_this_adapter(api, owner, tmp_path, monkeypatch):
    """A session bound under a secondary profile's authority cannot be seen — let alone
    locked or reconciled — by this profile's adapter: the lookup fails closed and the
    secondary profile's marker is untouched."""
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from persistence_assembly import open_persistence, profile_scope

    # A second, independent profile (secondary home) with its own authority and DB.
    secondary_home = tmp_path / "secondary"
    secondary_home.mkdir()
    secondary_persistence = open_persistence(profile_scope(home=secondary_home))
    secondary_store = SessionStore(config=GatewayConfig(), sessions_dir=secondary_home / "sessions")
    secondary_store._db = secondary_persistence
    secondary_store._persistence_beside = lambda pinned: pinned
    secondary_runner = SimpleNamespace(_draining=False, session_store=secondary_store)
    secondary = SessionAuthority(secondary_runner, profile_id="secondary", instance_id="first",
                                 epoch=secondary_persistence.runtime.begin_epoch(instance_id="first"),
                                 persistence=secondary_persistence)
    secondary_runner.session_authority = secondary

    from gateway.platforms.api_server import APIServerAdapter
    from gateway.config import PlatformConfig
    secondary_adapter = APIServerAdapter(PlatformConfig(enabled=True))
    secondary_adapter.gateway_runner = secondary_runner
    secondary_adapter._session_db = secondary_persistence
    secondary_runner._adapter_for_source = lambda source: secondary_adapter
    monkeypatch.setenv("HERMES_HOME", str(secondary_home))
    secondary_states = _attach_turn_runner_state(secondary)
    route = _bind_api_session(secondary_adapter, "sess-secondary")
    _seed_corrupt_marker(secondary, secondary_states, route)
    secondary_persistence_marker = secondary_store.get_session_metadata(route, "autorouter_managed")

    # This profile's adapter, scoped to the launch home, is asked for the secondary's session.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    status, payload = asyncio.run(_post_model(api, "sess-secondary", _PHYSICAL_BODY))

    assert status in (404, 500), (status, payload)
    assert secondary_store.get_session_metadata(route, "autorouter_managed") == secondary_persistence_marker == "corrupt!!"
    secondary_persistence.close()


def test_unavailable_authority_fails_closed(api, owner, monkeypatch):
    """Missing authority must fail closed: the lock lands, the marker is not cleared,
    and the endpoint says so — never a silent 200."""
    states = _attach_turn_runner_state(owner)
    route = _bind_api_session(api, "sess-parked")
    _seed_corrupt_marker(owner, states, route)

    # The persisted API binding that authorizes this session to reconcile cannot be
    # resolved: the host binding refuses. Ownership is unproven -> fail closed.
    from persistence.core.errors import AuthorityRefusal
    import gateway.session_api as session_api

    def refusing_restore(authority, session_id):
        raise AuthorityRefusal("not_found")

    monkeypatch.setattr(session_api, "restore_api_session", refusing_restore)

    status, payload = asyncio.run(_post_model(api, "sess-parked", _PHYSICAL_BODY))

    assert status == 500
    assert payload["error"]["code"] == "managed_marker_reconciliation_failed"
    assert owner.runner.session_store.get_session_metadata(route, "autorouter_managed") == "corrupt!!"
    assert states[route].conversation.autorouter_managed == "corrupt!!"


def test_restart_restores_session_and_marker_stays_cleared(api, owner):
    """Persistence across a simulated restart: the durable receipt rebuilds the live
    entry and the reconciled marker stays gone."""
    states = _attach_turn_runner_state(owner)
    route = _bind_api_session(api, "sess-restart")
    _seed_corrupt_marker(owner, states, route)

    status, payload = asyncio.run(_post_model(api, "sess-restart", _PHYSICAL_BODY))
    assert status == 200, payload

    # Simulate a gateway restart: in-memory session state is rebuilt from durable state.
    owner.sessions.clear()
    states.clear()
    from gateway.session_api import restore_api_session
    ref = restore_api_session(owner, "sess-restart")
    assert owner.sessions["sess-restart"].route == route
    assert ref.session_id == "sess-restart"
    # The marker does not come back after restore, and the boundary routes the turn.
    assert owner.runner.session_store.get_session_metadata(route, "autorouter_managed") is None
    states = _attach_turn_runner_state(owner)
    from gateway.autorouter_turn import AutoRouterRefusal
    try:
        _turn(owner, route)
    except AutoRouterRefusal as exc:  # pragma: no cover - failure path for clarity
        pytest.fail(f"restored session refused again: {exc}")


def test_marker_absent_on_authoritative_route_is_not_an_error(api, owner):
    """A session with no managed marker anywhere reconciles cleanly (nothing to clear)."""
    _attach_turn_runner_state(owner)
    route = _bind_api_session(api, "sess-clean")
    status, payload = asyncio.run(_post_model(api, "sess-clean", _PHYSICAL_BODY))
    assert status == 200, payload
