"""Per-session managed-routing state in the messaging gateway (dispatch 141, P1/P2).

* never-managed sessions keep stock semantics, even with routing disabled;
* previously managed sessions whose consumer disappears, whose authorization is
  revoked, whose route evidence is unusable, or whose state cannot be read are
  refused before any provider request — never widened to the last route or the
  stock default;
* the managed marker survives a gateway restart (in-memory state is rebuilt,
  the session store persists the marker);
* explicit per-session physical picks stay authoritative and clear the marker;
* credentials and decisions never cross profile boundaries;
* refusal means zero requests reach the provider-resolution/build layer.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from gateway.autorouter_turn import (
    AutoRouterRefusal,
    pre_turn_route,
)

from tests.gateway.test_autorouter_turn import (
    _CONSUMER_PLUGIN,
    _STOCK_RUNTIME,
    _RecordingStore,
    _no_policy,
    _stub_runner_kwargs,
    _telegram,
    _write_plugin,
)


@pytest.fixture
def consumer_session(tmp_path, monkeypatch):
    """A scratch HERMES_HOME with a selectable pre_inference_route consumer."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    _write_plugin(home, "autorouter_fixture", _CONSUMER_PLUGIN)
    (home / "config.yaml").write_text("plugins:\n  enabled: [autorouter_fixture]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("FIXTURE_OUTCOME", "route")
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    return home


def _fake_runner(override=None, *, peek_error=None, store_error=None):
    """Runner fake with readable session state and a durable metadata store."""
    states = {}

    def _peek(_key):
        if peek_error is not None:
            raise peek_error
        return states.get(_key)

    def _state(_key):
        return states.setdefault(_key, SimpleNamespace(conversation=SimpleNamespace(
            model_override=override, autorouter_managed=None)))

    class _Store(_RecordingStore):
        def get_session_metadata(self, key, k, default=None):
            if store_error is not None:
                raise store_error
            return super().get_session_metadata(key, k, default)

    runner = SimpleNamespace(
        _peek_session_state=_peek,
        _session_state=_state,
        session_store=_Store(),
    )
    return runner, states


def _seed_managed(runner, states, key, mode="ordinary"):
    """What a real managed turn does: marker in memory AND in the store."""
    marker = {"mode": mode, "since": 1.0}
    states.setdefault(key, SimpleNamespace(conversation=SimpleNamespace(
        model_override=None, autorouter_managed=dict(marker))))
    runner.session_store.set_session_metadata(key, "autorouter_managed", dict(marker))


def _seed_managed_store_only(runner, key, mode="ordinary"):
    """Simulate a restart: only the durable store carries the marker."""
    runner.session_store.set_session_metadata(key, "autorouter_managed",
                                              {"mode": mode, "since": 1.0})


# ---------------------------------------------------------------------------
# Restart persistence
# ---------------------------------------------------------------------------

def test_managed_state_survives_restart_when_consumer_unavailable(consumer_session, monkeypatch):
    """A restarted gateway sees the marker from the store alone: with the
    consumer gone, the turn is refused — never widened to stock."""
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    _seed_managed_store_only(runner, "key-restart")
    # Simulate consumer unload: has_middleware now reports nothing.
    monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda _kind: False)
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key="key-restart",
                       user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert seen == []


def test_managed_state_survives_restart_and_reselects_eligible_route(consumer_session, monkeypatch):
    """Restart + consumer present + route evidence: routing re-engages."""
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    _seed_managed_store_only(runner, "key-restart-2")
    model, _kwargs, applied = pre_turn_route(
        runner=runner, source=_telegram(), session_key="key-restart-2",
        user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert applied == "ordinary"
    assert model == "local-model-a"
    assert seen == [("locala", "local-model-a")]


# ---------------------------------------------------------------------------
# Revocation, opt-out and ineligibility for previously managed sessions
# ---------------------------------------------------------------------------

def test_managed_session_consumer_unloaded_after_adoption(consumer_session, monkeypatch, tmp_path):
    """Adopted mid-life, then the consumer disappears: refused, no requests."""
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    model, kwargs, applied = pre_turn_route(
        runner=runner, source=_telegram(), session_key="key-unload",
        user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert applied == "ordinary"  # first (adopting) turn routed normally
    assert seen == [("locala", "local-model-a")]
    # The marker must now exist in the store.
    assert runner.session_store.get_session_metadata("key-unload", "autorouter_managed") is not None
    # Consumer unloads (plugin disabled / revoked mid-session).
    monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda _kind: False)
    seen.clear()
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key="key-unload",
                       user_config={}, model=model, runtime_kwargs=kwargs)
    assert seen == []


def test_managed_session_authorization_revoked(consumer_session, monkeypatch):
    """Consumer present but now refuses (authorization revoked / evidence
    ineligible): refused before any provider request."""
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    _seed_managed_store_only(runner, "key-revoked")
    monkeypatch.setenv("FIXTURE_OUTCOME", "refuse")
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key="key-revoked",
                       user_config={}, model="local-model-a", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert seen == []


def test_managed_session_passthrough_is_not_an_opt_out(consumer_session, monkeypatch):
    """Consumer opts out (operator disabled routing): a managed session refuses;
    a never-managed session keeps stock."""
    _no_policy(monkeypatch)
    _stub_runner_kwargs(monkeypatch)
    monkeypatch.setenv("FIXTURE_OUTCOME", "passthrough")
    runner, _states = _fake_runner()
    _seed_managed_store_only(runner, "key-pt-managed")
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key="key-pt-managed",
                       user_config={}, model="local-model-a", runtime_kwargs=dict(_STOCK_RUNTIME))
    # Never-managed control session: stock semantics preserved.
    runner2, _states2 = _fake_runner()
    model, kwargs, applied = pre_turn_route(
        runner=runner2, source=_telegram(), session_key="key-pt-fresh",
        user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert applied == "stock"
    assert (model, kwargs) == ("stock-model", _STOCK_RUNTIME)


def test_managed_session_evidence_ineligible_reselects_eligible_route(consumer_session, monkeypatch):
    """The pinned route becomes ineligible: the consumer re-selects an eligible
    route instead of failing or widening."""
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    _seed_managed_store_only(runner, "key-reselect")
    monkeypatch.setenv("FIXTURE_OUTCOME", "route_b")
    model, _kwargs, applied = pre_turn_route(
        runner=runner, source=_telegram(), session_key="key-reselect",
        user_config={}, model="local-model-a", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert applied == "ordinary"
    assert model == "local-model-b"
    assert seen == [("localb", "local-model-b")]


# ---------------------------------------------------------------------------
# Malformed / unreadable state
# ---------------------------------------------------------------------------

def test_malformed_managed_marker_routes_fail_closed(consumer_session, monkeypatch):
    """A malformed marker is managed evidence: a fresh consumer decision re-routes
    (applied ordinary — never stock), while passthrough/refusal refuse."""
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    runner.session_store.set_session_metadata("key-corrupt", "autorouter_managed", "corrupt!!")
    _model, _kwargs, applied = pre_turn_route(
        runner=runner, source=_telegram(), session_key="key-corrupt",
        user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert applied == "ordinary"  # consumer authorized a fresh route; NOT stock
    assert seen == [("locala", "local-model-a")]
    monkeypatch.setenv("FIXTURE_OUTCOME", "passthrough")
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key="key-corrupt",
                       user_config={}, model="local-model-a", runtime_kwargs=dict(_STOCK_RUNTIME))


def test_missing_session_key_refuses(consumer_session, monkeypatch):
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key="",
                       user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key=None,
                       user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert seen == []


def test_unreadable_session_state_refuses(monkeypatch):
    _no_policy(monkeypatch)
    runner, _states = _fake_runner(peek_error=RuntimeError("state db locked"))
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key="key-locked",
                       user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))


def test_unreadable_managed_marker_refuses(consumer_session, monkeypatch):
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner(store_error=RuntimeError("metadata table gone"))
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key="key-meta",
                       user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert seen == []


# ---------------------------------------------------------------------------
# Explicit picks stay authoritative; the marker clears
# ---------------------------------------------------------------------------

def test_explicit_physical_pick_stays_authoritative_and_clears_marker(consumer_session, monkeypatch):
    _no_policy(monkeypatch)
    _stub_runner_kwargs(monkeypatch)
    runner, states = _fake_runner()
    _seed_managed(runner, states, "key-explicit")
    states["key-explicit"].conversation.model_override = {"provider": "openrouter", "model": "paid/model-x"}
    _model, _kwargs, applied = pre_turn_route(
        runner=runner, source=_telegram(), session_key="key-explicit",
        user_config={}, model="paid/model-x", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert applied == "explicit"
    assert states["key-explicit"].conversation.autorouter_managed is None
    assert runner.session_store.get_session_metadata("key-explicit", "autorouter_managed") is None


# ---------------------------------------------------------------------------
# Profile isolation
# ---------------------------------------------------------------------------

def test_routing_decision_binds_to_the_bound_profile(consumer_session, monkeypatch):
    """Two concurrent profiles see their OWN profile in the consumer metadata;
    no decision (or profile authority) crosses the boundary."""
    _no_policy(monkeypatch)
    import hermes_cli.middleware as _mw

    captured = []
    real_apply = _mw.apply_pre_inference_route_middleware

    def recording_apply(metadata, platform=None):
        captured.append(dict(metadata))
        return real_apply(metadata, platform=platform)

    monkeypatch.setattr(_mw, "apply_pre_inference_route_middleware", recording_apply)
    monkeypatch.setenv("FIXTURE_OUTCOME", "route")
    _stub_runner_kwargs(monkeypatch)

    monkeypatch.setattr("hermes_cli.profiles.current_profile_name",
                        lambda default=None: "profile-a")
    runner_a, _sa = _fake_runner()
    pre_turn_route(runner=runner_a, source=_telegram(), session_key="key-pa",
                   user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    monkeypatch.setattr("hermes_cli.profiles.current_profile_name",
                        lambda default=None: "profile-b")
    runner_b, _sb = _fake_runner()
    pre_turn_route(runner=runner_b, source=_telegram(), session_key="key-pb",
                   user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert [c["hermes_profile"] for c in captured] == ["profile-a", "profile-b"]


def test_failed_scope_resolution_refuses_and_issues_no_request(consumer_session, monkeypatch):
    """A bound session whose profile scope cannot be established refuses: no
    launch-profile substitution, no request issued."""
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    _seed_managed_store_only(runner, "key-scope-fail")

    def boom(default=None):
        raise RuntimeError("bound profile scope is gone")

    monkeypatch.setattr("hermes_cli.profiles.current_profile_name", boom)
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key="key-scope-fail",
                       user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert seen == []


# ---------------------------------------------------------------------------
# Zero outbound inference on refusal (full-turn integration)
# ---------------------------------------------------------------------------

def test_refused_managed_turn_issues_zero_provider_requests(consumer_session, monkeypatch):
    _no_policy(monkeypatch)
    _stub_runner_kwargs(monkeypatch)
    from tests.gateway.test_autorouter_turn import _e2e_runner, _e2e_turn, _HISTORY

    runner = _e2e_runner()
    # Mark the session managed in the durable store; the fake policy layer is gone.
    runner.session_store.get_session_metadata = lambda key, k, default=None: (
        {"mode": "ordinary", "since": 1.0} if k == "autorouter_managed" else default)
    monkeypatch.setenv("FIXTURE_OUTCOME", "refuse")
    result, rec = _e2e_turn(runner, "chat-managed-refusal", [])
    assert result["failed"] is True
    assert result["failure_reason"] == "autorouter_refused"
    assert rec.built == []
