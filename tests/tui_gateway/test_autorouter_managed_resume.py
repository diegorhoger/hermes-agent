"""AutoRouter-managed ordinary sessions must re-engage routing on resume.

Defect reproduced in the isolated development environment: an ordinary routed
session persisted the accounting pair (config-default model + billing-routed
provider), and resume read it as an explicit pick — the session then ran with
routing disabled for its whole resumed life and sent the stale model to the
routed endpoint (observed live: model=ornith-1.5:9b against locala:8401).

A managed session now persists only the ``_autorouter_managed`` marker; resume
returns no pick, so the host seam re-derives the route and turn-boundary
re-evaluation re-engages.
"""
from __future__ import annotations

import types


def _agent(**overrides):
    base = dict(model="local-model-a", provider="locala", base_url="http://127.0.0.1:8401/v1",
                api_mode="chat_completions", reasoning_config=None, service_tier=None)
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_runtime_model_config_managed_session_never_persists_a_route():
    from tui_gateway.server import _runtime_model_config

    agent = _agent(_autorouter_routed=True)
    config = _runtime_model_config(agent, {"model": "ornith-1.5:9b", "provider": "locala",
                                           "base_url": "http://127.0.0.1:8401/v1",
                                           "api_mode": "chat_completions",
                                           "gateway_runtime": {"provider": "locala", "model": "x"},
                                           "_selection": {"provider": "locala"}})
    assert config["_autorouter_managed"] is True
    for key in ("model", "provider", "base_url", "api_mode", "gateway_runtime", "_selection"):
        assert key not in config, f"managed session must not persist {key}"


def test_runtime_model_config_managed_round_trips_reasoning_and_tier():
    from tui_gateway.server import _runtime_model_config

    agent = _agent(reasoning_config={"enabled": False}, service_tier="fast", _autorouter_routed=True)
    config = _runtime_model_config(agent, {"model": "keep-me-not"})
    assert config["reasoning_config"] == {"enabled": False}
    assert config["service_tier"] == "fast"
    assert "model" not in config


def test_stored_session_runtime_overrides_managed_marker_opts_back_into_routing():
    from tui_gateway.server import _stored_session_runtime_overrides

    row = {"model": "ornith-1.5:9b", "billing_provider": "locala",
           "billing_base_url": "http://127.0.0.1:8401/v1",
           "model_config": '{"_autorouter_managed": true}'}
    # The managed marker survives resume so a lost consumer fails closed (no
    # route resurrected from billing accounting, no stock widening).
    assert _stored_session_runtime_overrides(row) == {"autorouter_managed": True}


def test_stored_session_runtime_overrides_managed_marker_wins_over_stale_pick_keys():
    from tui_gateway.server import _stored_session_runtime_overrides

    row = {"model": "ornith-1.5:9b", "billing_provider": "locala",
           "model_config": '{"_autorouter_managed": true, "model": "ornith-1.5:9b", "provider": "locala"}'}
    assert _stored_session_runtime_overrides(row) == {"autorouter_managed": True}
