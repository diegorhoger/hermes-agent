"""Same-route runtime revalidation at the TUI turn boundary (dispatch 142, P1/P2).

A consumer decision that still matches the attached route must NOT be treated
as proof the physical runtime is still authorized and equivalent: the turn
boundary re-resolves the runtime and revalidates it on EVERY turn.

These tests drive the real ``tui_gateway.autorouter_turn.turn_boundary`` with
a faithful simulation of the Hermes in-place swap (``switch_model`` rebuilds
the client atomically and rolls back on failure), and a real registered
consumer plugin. They prove:

* credential rotation on the same provider/model refreshes the client in place;
* runtime authorization revoked mid-session issues zero outbound requests
  with the old credential;
* an endpoint change rebuilds the client against the new endpoint;
* a healthy, physically equivalent runtime is kept without any client rebuild;
* the consumer is asked exactly once per turn regardless of revalidation;
* provider identity is enforced in the same gate (cross-provider substitution
  and foreign endpoints refuse before any request).
"""
from __future__ import annotations

import json

import pytest

from tui_gateway.autorouter_turn import turn_boundary

from tests.tui_gateway.test_autorouter_turn_boundary import (
    consumer_home,
    _agent,
    _session,
    _consumer_calls,
)

NOUS_URL = "https://inference-api.nousresearch.com/v1"


def _rotation_resolver(monkeypatch, *, base_url="http://127.0.0.1:9/locala", api_key="sk-rotated",
                       provider="locala", requested="locala", model="local-model-a", capabilities=None):
    """A resolver that starts healthy and can be flipped to any other state."""
    state = {"base_url": base_url, "api_key": api_key, "provider": provider,
             "requested": requested, "model": model, "capabilities": capabilities, "error": None}
    seen = []

    def fake_resolve(*, requested=None, target_model=None, **_kwargs):
        seen.append({"requested": requested, "target_model": target_model})
        if state["error"] is not None:
            raise state["error"]
        runtime = {
            "provider": state["provider"],
            "requested_provider": state["requested"],
            "model": state["model"],
            "api_key": state["api_key"],
            "base_url": state["base_url"],
            "api_mode": "chat_completions",
            "request_overrides": {},
            "capabilities": {} if capabilities is None else dict(capabilities),
        }
        return runtime

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fake_resolve)
    return state, seen


# ---------------------------------------------------------------------------
# Credential rotation / revocation on the same route
# ---------------------------------------------------------------------------

def test_same_route_credential_rotation_refreshes_client(consumer_home, monkeypatch):
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "route")  # decision: locala/local-model-a
    state, _seen = _rotation_resolver(monkeypatch, api_key="sk-first")
    agent = _agent(virtual=False, provider="locala", model="local-model-a",
                   api_key="sk-first", base_url="http://127.0.0.1:9/locala")
    assert turn_boundary("sess-1", _session(), agent) is False  # healthy start
    assert agent._switch_calls == []

    # A's credential is rotated while the provider/model stay identical.
    state["api_key"] = "sk-second"
    assert turn_boundary("sess-1", _session(), agent) is True  # rebuilt in place
    assert agent.api_key == "sk-second"
    assert agent._switch_calls[-1]["api_key"] == "sk-second"
    assert agent._switch_calls[-1]["new_provider"] == "locala"
    assert agent._switch_calls[-1]["new_model"] == "local-model-a"
    # The consumer still saw exactly one decision per turn.
    assert len(_consumer_calls(log)) == 2


def test_same_route_virtual_pick_credential_rotation_refreshes_client(consumer_home, monkeypatch):
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_A", "route")
    state, _seen = _rotation_resolver(monkeypatch, api_key="sk-first")
    agent = _agent(virtual=True, provider="locala", model="local-model-a",
                   api_key="sk-first", base_url="http://127.0.0.1:9/locala")
    assert turn_boundary("sess-1", _session(), agent) is False
    state["api_key"] = "sk-second"
    assert turn_boundary("sess-1", _session(), agent) is True
    assert agent.api_key == "sk-second"
    assert len(_consumer_calls(log)) == 2


def test_same_route_credential_revocation_refuses_without_outbound(consumer_home, monkeypatch):
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "route")
    state, _seen = _rotation_resolver(monkeypatch, api_key="sk-first")
    agent = _agent(virtual=False, provider="locala", model="local-model-a",
                   api_key="sk-first", base_url="http://127.0.0.1:9/locala")
    assert turn_boundary("sess-1", _session(), agent) is False

    # A's credential is revoked while the provider/model identifiers stay the same.
    state["error"] = PermissionError("credential revoked")
    with pytest.raises(RuntimeError):
        turn_boundary("sess-1", _session(), agent)
    # Zero outbound: the client was not rebuilt, the old credential is not used.
    assert agent._switch_calls == []
    assert agent.api_key == "sk-first"
    assert len(_consumer_calls(log)) == 2  # consumer decision happened; no request left


def test_same_route_endpoint_change_refreshes_client(consumer_home, monkeypatch):
    _, _log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "route")
    state, _seen = _rotation_resolver(monkeypatch, base_url="http://127.0.0.1:9/locala")
    agent = _agent(virtual=False, provider="locala", model="local-model-a",
                   api_key="sk-rotated", base_url="http://127.0.0.1:9/locala")
    assert turn_boundary("sess-1", _session(), agent) is False

    # A's endpoint is relocated while the provider/model stay identical.
    state["base_url"] = "http://127.0.0.1:9/relocated-locala"
    assert turn_boundary("sess-1", _session(), agent) is True
    assert agent.base_url == "http://127.0.0.1:9/relocated-locala"
    assert agent._switch_calls[-1]["base_url"] == "http://127.0.0.1:9/relocated-locala"


def test_same_route_healthy_equivalent_runtime_avoids_client_rebuild(consumer_home, monkeypatch):
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "route")
    _rotation_resolver(monkeypatch, api_key="sk-same")
    agent = _agent(virtual=False, provider="locala", model="local-model-a",
                   api_key="sk-same", base_url="http://127.0.0.1:9/locala")
    agent.runtime_capabilities = {"tools": True}
    assert turn_boundary("sess-1", _session(), agent) is False
    assert turn_boundary("sess-1", _session(), agent) is False
    assert agent._switch_calls == []
    assert len(_consumer_calls(log)) == 2


def test_same_route_capability_change_rebuilds_client(consumer_home, monkeypatch):
    _, _log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "route")
    _state, _seen = _rotation_resolver(monkeypatch, api_key="sk-same", capabilities={"tools": True})
    agent = _agent(virtual=False, provider="locala", model="local-model-a",
                   api_key="sk-same", base_url="http://127.0.0.1:9/locala")
    agent.runtime_capabilities = {"tools": False}  # capability map drifted
    assert turn_boundary("sess-1", _session(), agent) is True
    assert agent._switch_calls[-1]["new_provider"] == "locala"


# ---------------------------------------------------------------------------
# Custom-provider aliases (named custom provider through the custom adapter)
# ---------------------------------------------------------------------------

def _seed_locala_entry(home, *, base_url="http://127.0.0.1:8401/v1"):
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [autorouter_fixture]\n"
        "providers:\n"
        "  locala:\n"
        f"    base_url: {base_url}\n"
        "    api_key: sk-local-a-dummy-not-a-secret\n",
        encoding="utf-8",
    )


def test_custom_provider_alias_route_is_accepted(consumer_home, monkeypatch):
    """Decision locala resolves through the custom adapter to locala's owned
    endpoint: accepted — no rebuild, exactly one consumer call."""
    home, log = consumer_home
    _seed_locala_entry(home)
    # Rebind the discovery-cached config by rewriting it before the turn.
    monkeypatch.setenv("FIXTURE_MODE_B", "route")
    _state, _seen = _rotation_resolver(monkeypatch, provider="custom", requested="locala",
                                     base_url="http://127.0.0.1:8401/v1", api_key="sk-local-a-dummy-not-a-secret")
    agent = _agent(virtual=False, provider="locala", model="local-model-a",
                   api_key="sk-local-a-dummy-not-a-secret", base_url="http://127.0.0.1:8401/v1")
    assert turn_boundary("sess-1", _session(), agent) is False
    assert agent._switch_calls == []
    assert len(_consumer_calls(log)) == 1


def test_custom_provider_alias_with_foreign_endpoint_refuses(consumer_home, monkeypatch):
    """Decision locala; the resolver hands back locala's name but an endpoint
    belonging to localb's entry: refused, zero outbound."""
    home, log = consumer_home
    _seed_locala_entry(home)
    monkeypatch.setenv("FIXTURE_MODE_B", "route")
    _rotation_resolver(monkeypatch, provider="custom", requested="locala",
                       base_url="http://127.0.0.1:8402/v1", api_key="sk-local-b-dummy-not-a-secret")
    agent = _agent(virtual=False, provider="locala", model="local-model-a",
                   api_key="sk-local-a-dummy-not-a-secret", base_url="http://127.0.0.1:8401/v1")
    with pytest.raises(RuntimeError):
        turn_boundary("sess-1", _session(), agent)
    assert agent._switch_calls == []
    assert agent.api_key == "sk-local-a-dummy-not-a-secret"
    assert len(_consumer_calls(log)) == 1


def test_same_route_relocated_named_custom_endpoint_refreshes(consumer_home, monkeypatch):
    """The operator repoints locala's entry at a new port; the entry is rewritten
    between turns and the client rebuilds against the owned new endpoint."""
    home, log = consumer_home
    _seed_locala_entry(home, base_url="http://127.0.0.1:8401/v1")
    monkeypatch.setenv("FIXTURE_MODE_B", "route")
    state, _seen = _rotation_resolver(monkeypatch, provider="custom", requested="locala",
                                      base_url="http://127.0.0.1:8401/v1",
                                      api_key="sk-local-a-dummy-not-a-secret")
    agent = _agent(virtual=False, provider="locala", model="local-model-a",
                   api_key="sk-local-a-dummy-not-a-secret", base_url="http://127.0.0.1:8401/v1")
    assert turn_boundary("sess-1", _session(), agent) is False

    _seed_locala_entry(home, base_url="http://127.0.0.1:8499/v1")  # operator repoints the entry
    state["base_url"] = "http://127.0.0.1:8499/v1"                  # resolver now yields it
    assert turn_boundary("sess-1", _session(), agent) is True
    assert agent.base_url == "http://127.0.0.1:8499/v1"
    assert len(_consumer_calls(log)) == 2


# ---------------------------------------------------------------------------
# Provider identity enforced in the same gate
# ---------------------------------------------------------------------------

def test_claimed_request_physical_cross_provider_refuses(consumer_home, monkeypatch):
    """Decision locala; physical provider nous, requested_provider=locala: refuse."""
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "route")
    _rotation_resolver(monkeypatch, provider="nous", requested="locala", base_url=NOUS_URL)
    agent = _agent(virtual=False, provider="locala", model="local-model-a")
    with pytest.raises(RuntimeError):
        turn_boundary("sess-1", _session(), agent)
    assert agent._switch_calls == []
    assert len(_consumer_calls(log)) == 1


def test_different_model_from_resolver_refuses(consumer_home, monkeypatch):
    """Decision local-model-a; the resolver returns paid/model-x: refuse."""
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "route")
    _rotation_resolver(monkeypatch, model="paid/model-x")
    agent = _agent(virtual=False, provider="locala", model="local-model-a")
    with pytest.raises(RuntimeError):
        turn_boundary("sess-1", _session(), agent)
    assert agent._switch_calls == []
    assert len(_consumer_calls(log)) == 1


def test_one_consumer_call_per_decision_with_revalidation(consumer_home, monkeypatch):
    """Revalidation never re-invokes the consumer — one call per turn, always."""
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_A", "route")
    _rotation_resolver(monkeypatch, api_key="sk-first")
    agent = _agent(virtual=True, provider="locala", model="local-model-a",
                   api_key="sk-first", base_url="http://127.0.0.1:9/locala")
    for _ in range(3):
        turn_boundary("sess-1", _session(), agent)
    assert len(_consumer_calls(log)) == 3
