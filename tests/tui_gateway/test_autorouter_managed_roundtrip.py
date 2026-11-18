"""Managed-routing state round-trips: persist at the row, restore at resume (dispatch 141).

Covers the TUI/Desktop persistence contract for AutoRouter-managed sessions:

* ``_runtime_model_config`` persists the managed marker and the routing mode
  (never a route) for managed agents;
* ``_stored_session_runtime_overrides`` restores it — an ordinary managed row
  resumes unpinned (consumer re-evaluates per turn), a virtual managed row
  resumes the virtual pick (fail-closed through the consumer);
* an explicit physical pick after management drops the stale markers;
* the deferred first-prompt build forwards the managed marker;
* an explicit ``/model`` switch to (and away from) the virtual pick sets and
  clears the markers.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

import tui_gateway.server as _server  # split-module functions rebound to server globals
_runtime_model_config = _server._runtime_model_config
_stored_session_runtime_overrides = _server._stored_session_runtime_overrides
_deferred_build_agent_kwargs = _server._deferred_build_agent_kwargs
_commit_agent_switch = _server._commit_agent_switch


def _agent(*, routed, virtual=False, provider="locala", model="local-model-a", requested=None):
    return SimpleNamespace(
        model=model, provider=provider,
        requested_provider=requested if requested is not None else provider,
        base_url="http://127.0.0.1:8401/v1", api_mode="chat_completions",
        reasoning_config=None, service_tier=None,
        _autorouter_routed=routed, _autorouter_virtual=virtual)


def test_ordinary_managed_row_round_trips_as_unpinned_managed():
    agent = _agent(routed=True, virtual=False)
    config = _runtime_model_config(agent, {"model": "ornith-1.5:9b", "_selection": {"provider": "locala", "model": "x"}})
    assert config["_autorouter_managed"] is True
    assert config["_autorouter_mode"] == "ordinary"
    for key in ("model", "provider", "base_url", "api_mode", "gateway_runtime", "_selection"):
        assert key not in config
    row = {"model": "ornith-1.5:9b", "billing_provider": "locala",
           "model_config": __import__("json").dumps(config)}
    overrides = _stored_session_runtime_overrides(row)
    assert overrides == {"autorouter_managed": True}
    assert "model_override" not in overrides and "provider_override" not in overrides


def test_virtual_managed_row_round_trips_as_virtual_pick():
    agent = _agent(routed=True, virtual=True, provider="localb", model="local-model-b", requested="autorouter")
    config = _runtime_model_config(agent, {})
    assert config["_autorouter_managed"] is True
    assert config["_autorouter_mode"] == "virtual"
    for key in ("model", "provider", "base_url", "api_mode", "gateway_runtime", "_selection"):
        assert key not in config
    row = {"model": "ornith-1.5:9b", "billing_provider": "localb",
           "model_config": __import__("json").dumps(config)}
    overrides = _stored_session_runtime_overrides(row)
    assert overrides["autorouter_managed"] is True
    assert overrides["provider_override"] == "autorouter"
    assert overrides["model_override"]["provider"] == "autorouter"
    assert overrides["model_override"]["model"] == "auto-free"
    # ... and the deferred first-prompt build keeps the pick + marker.
    session = {"resume_runtime_overrides": overrides, "resume_session_id": "sess-1"}
    kwargs = _deferred_build_agent_kwargs(session, session_db=None)
    assert kwargs["model_override"]["provider"] == "autorouter"
    assert kwargs["autorouter_managed"] is True


def test_deferred_build_forwards_ordinary_managed_marker():
    session = {"resume_runtime_overrides": {"autorouter_managed": True}, "resume_session_id": "sess-2"}
    kwargs = _deferred_build_agent_kwargs(session, session_db=None)
    assert kwargs["autorouter_managed"] is True


def test_unmanaged_row_drops_stale_managed_markers():
    """An explicit pick after management persists the pick and drops the
    managed marker — resume never re-adopts it."""
    agent = _agent(routed=False, provider="openrouter", model="paid/model-x", requested="openrouter")
    stale = {"_autorouter_managed": True, "_autorouter_mode": "virtual",
             "model": "stale", "provider": "stale", "_selection": {"provider": "autorouter", "model": "auto-free"}}
    config = _runtime_model_config(agent, stale)
    assert "_autorouter_managed" not in config
    assert "_autorouter_mode" not in config
    assert config["provider"] == "openrouter"


def test_model_switch_to_virtual_pick_sets_managed_markers(monkeypatch):
    monkeypatch.setattr(_server, "_transcript_claim", lambda session: contextlib.nullcontext())
    monkeypatch.setattr(_server, "_restart_slash_worker", lambda sid, session: None)
    monkeypatch.setattr(_server, "_persist_live_session_runtime", lambda session: None)
    monkeypatch.setattr(_server, "_persist_live_session_system_prompt", lambda session: None)
    monkeypatch.setattr(_server, "_append_model_switch_marker", lambda session, **kw: None)
    monkeypatch.setattr(_server, "_emit_session_info", lambda sid, session: None)
    monkeypatch.setattr("hermes_cli.autorouter_picker.is_autorouter_pick", lambda m, p: True)
    monkeypatch.setattr(
        "hermes_cli.autorouter_picker.resolve_autorouter_physical",
        lambda **kw: ({"api_key": "k", "base_url": "http://127.0.0.1:8401/v1", "api_mode": "chat_completions"},
                      {"provider": "locala", "model": "local-model-a"}))

    class _Agent:
        model = "paid/model-x"
        provider = "openrouter"
        _autorouter_routed = False
        _autorouter_virtual = False

        def switch_model(self, new_model, new_provider, **kwargs):
            self.model, self.provider = new_model, new_provider

    agent = _Agent()
    result = SimpleNamespace(new_model="auto-free", target_provider="autorouter",
                             api_key="", base_url="", api_mode="", runtime_capabilities=None)
    _commit_agent_switch("sid", {"session_key": "sess-1"}, agent, result, "paid/model-x", None)
    assert agent._autorouter_routed is True
    assert agent._autorouter_virtual is True


def test_model_switch_to_physical_pick_clears_managed_markers(monkeypatch):
    monkeypatch.setattr(_server, "_transcript_claim", lambda session: contextlib.nullcontext())
    monkeypatch.setattr(_server, "_restart_slash_worker", lambda sid, session: None)
    monkeypatch.setattr(_server, "_persist_live_session_runtime", lambda session: None)
    monkeypatch.setattr(_server, "_persist_live_session_system_prompt", lambda session: None)
    monkeypatch.setattr(_server, "_append_model_switch_marker", lambda session, **kw: None)
    monkeypatch.setattr(_server, "_emit_session_info", lambda sid, session: None)
    monkeypatch.setattr("hermes_cli.autorouter_picker.is_autorouter_pick", lambda m, p: False)

    class _Agent:
        model = "local-model-a"
        provider = "locala"
        _autorouter_routed = True
        _autorouter_virtual = True

        def switch_model(self, new_model, new_provider, **kwargs):
            self.model, self.provider = new_model, new_provider

    agent = _Agent()
    result = SimpleNamespace(new_model="paid/model-x", target_provider="openrouter",
                             api_key="k", base_url="http://x/v1", api_mode="chat_completions",
                             runtime_capabilities=None)
    _commit_agent_switch("sid", {"session_key": "sess-1"}, agent, result, "local-model-a", None)
    assert agent._autorouter_routed is False
    assert agent._autorouter_virtual is False
