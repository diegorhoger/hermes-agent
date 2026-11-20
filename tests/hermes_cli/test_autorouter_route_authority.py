"""Real gateway regressions for explicit session-create route authority (Dispatch 160).

The gateway turn runner must not let Mode B (ordinary-session pre-inference
routing) silently replace a route the user selected. ``build_policy`` records
those selections verbatim in ``request_json`` -- including for sessions that
follow a profile -- so this module builds real session-create requests and
asserts the *effective* provider, model and endpoint after resolution.

Distinct baseline vs AutoRouter-selected IDs make any rewrite unambiguous on
the wire. Authority metadata that cannot be read fails closed.
"""
from __future__ import annotations

import json

import pytest

from gateway import autorouter_turn_route as mod
from gateway.session_policy import build_policy

BASELINE_MODEL = "mockinfra-baseline-model"
BASELINE_PROVIDER = "baselineprovider"
BASELINE_URL = "http://127.0.0.1:9101/v1"
ROUTED_MODEL = "mockinfra-free-model"
ROUTED_PROVIDER = "mockinfra"
ROUTED_URL = "http://127.0.0.1:9102/v1"


def _policy(tmp_path, **create_fields):
    """Build a real frozen session-create request via build_policy."""
    params = {"source": "cli", "cwd": str(tmp_path), "toolsets": [], **create_fields}
    return build_policy(params, {"model": {"default": BASELINE_MODEL}})


def _requests(policy):
    return json.loads(policy.request_json)


def _route_route_result():
    class _Route:
        status = "route"
        provider = ROUTED_PROVIDER
        model = ROUTED_MODEL
        reason = "free route"
    return _Route()


def _install_routing_mocks(monkeypatch):
    """A registered consumer that would rewrite to a different physical route."""
    monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda name: True)
    monkeypatch.setattr(
        "hermes_cli.middleware.apply_pre_inference_route_middleware",
        lambda metadata, platform=None: _route_route_result(),
    )

    def _runtime(requested, target_model):
        return {
            "provider": ROUTED_PROVIDER,
            "requested_provider": requested,
            "api_key": "synthetic-key",
            "base_url": ROUTED_URL,
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
            "credential_pool": None,
            "max_tokens": 1024,
            "capabilities": {"tools": True},
            "request_overrides": {},
            "model": target_model,
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime)


def _effective_route(policy, *, session_override=None, one_turn_override=None):
    has_explicit = mod.turn_has_explicit_route_authority(
        request_json=policy.request_json if policy is not None else None,
        session_model_override=session_override,
        one_turn_override=one_turn_override,
    )
    kwargs = {
        "provider": BASELINE_PROVIDER,
        "requested_provider": BASELINE_PROVIDER,
        "base_url": BASELINE_URL,
        "api_key": "synthetic-baseline-key",
        "model": BASELINE_MODEL,
    }
    return has_explicit, mod.resolve_autorouter_for_gateway_turn(
        model=BASELINE_MODEL,
        runtime_kwargs=dict(kwargs),
        session_id="sess-160",
        session_key="key-160",
        platform="gateway",
        has_explicit_model_or_provider=has_explicit,
    )


# --- classification of authority metadata ---------------------------------

def test_no_route_fields_is_ordinary(tmp_path):
    # An ordinary create records no model/provider/endpoint in request_json; the
    # configured default lives in config_json. Such sessions stay Mode B eligible.
    p = _policy(tmp_path)
    req = _requests(p)
    assert not ({"model", "provider", "base_url", "model_requested"} & set(req))
    assert mod.request_route_authority(p.request_json) == "ordinary"
    assert mod.request_route_authority(json.dumps({"source": "cli", "cwd": str(tmp_path)})) == "ordinary"


def test_provider_auto_alone_is_not_explicit(tmp_path):
    p = _policy(tmp_path, provider="auto")
    assert _requests(p)["provider"] == "auto"
    assert mod.request_route_authority(p.request_json) == "ordinary"


def test_explicit_model_is_authoritative(tmp_path):
    p = _policy(tmp_path, model="user-picked-model")
    assert mod.request_route_authority(p.request_json) == "explicit"


def test_explicit_provider_is_authoritative(tmp_path):
    p = _policy(tmp_path, provider="explicit-provider")
    assert mod.request_route_authority(p.request_json) == "explicit"


def test_explicit_endpoint_is_authoritative(tmp_path):
    p = _policy(tmp_path, base_url="http://127.0.0.1:9999/v1")
    assert mod.request_route_authority(p.request_json) == "explicit"


def test_recorded_model_switch_is_authoritative(tmp_path):
    p = _policy(tmp_path)
    stamped = json.dumps({**_requests(p), "model_requested": "switched-model", "route_derived": []}, sort_keys=True)
    assert mod.request_route_authority(stamped) == "explicit"


def test_followed_profile_create_pick_stays_authoritative(tmp_path):
    p = _policy(tmp_path, model="user-picked-model", follow_profile_config=True)
    assert _requests(p)["follow_profile_config"] is True
    assert mod.request_route_authority(p.request_json) == "explicit"


def test_profile_change_after_follow_keeps_pick_authoritative(tmp_path):
    """A followed profile changing its route must not rewrite the create pick."""
    p = _policy(tmp_path, model="user-picked-model", follow_profile_config=True)
    changed_config = {"model": {"default": "profile-changed-model", "provider": "other"}}
    # The live profile config changed; the frozen creation request did not.
    assert changed_config["model"]["default"] != _requests(p)["model"]
    assert mod.request_route_authority(p.request_json) == "explicit"


@pytest.mark.parametrize("source", ["cli", "tui", "gui", "acp", "oneshot"])
def test_api_and_request_surfaces_explicit_model_authoritative(tmp_path, source):
    if source not in {"cli", "tui", "gui", "acp", "oneshot"}:
        pytest.skip("surface not a create surface")
    p = build_policy(
        {"source": source, "cwd": str(tmp_path), "model": "api-picked-model", "toolsets": []},
        {"model": {"default": BASELINE_MODEL}},
    )
    assert mod.request_route_authority(p.request_json) == "explicit"


@pytest.mark.parametrize("bad", ["not json", "[1,2,3]", '"a string"', "42", "null"])
def test_unreadable_or_non_object_authority_fails_closed(bad):
    state = mod.request_route_authority(bad)
    assert state == "unavailable"
    assert mod.has_explicit_request_route(bad) is True


def test_missing_metadata_is_ordinary_not_unavailable():
    assert mod.request_route_authority("") == "ordinary"
    assert mod.request_route_authority(None) == "ordinary"


def test_session_and_one_turn_overrides_are_authoritative():
    assert mod.turn_has_explicit_route_authority(
        request_json=json.dumps({"source": "cli"}),
        session_model_override="session-switch-model",
    ) is True
    assert mod.turn_has_explicit_route_authority(
        request_json=json.dumps({"source": "cli"}),
        one_turn_override={"model": "one-turn-model"},
    ) is True
    assert mod.turn_has_explicit_route_authority(
        request_json=json.dumps({"source": "cli"}),
    ) is False


# --- effective route (provider / model / endpoint) -------------------------

def test_ordinary_session_is_rerouted_to_distinct_physical_route(tmp_path, monkeypatch):
    _install_routing_mocks(monkeypatch)
    p = build_policy({"source": "cli", "cwd": str(tmp_path), "toolsets": []},
                     {"model": {"default": BASELINE_MODEL}})
    has_explicit, (model, kwargs) = _effective_route(p)
    assert has_explicit is False
    assert model == ROUTED_MODEL
    assert kwargs["provider"] == ROUTED_PROVIDER
    assert kwargs["base_url"] == ROUTED_URL


@pytest.mark.parametrize("field,value", [
    ("model", "user-picked-model"),
    ("provider", "explicit-provider"),
    ("base_url", "http://127.0.0.1:9999/v1"),
])
def test_explicit_request_fields_preserve_baseline_route(tmp_path, monkeypatch, field, value):
    _install_routing_mocks(monkeypatch)
    p = _policy(tmp_path, **{field: value})
    has_explicit, (model, kwargs) = _effective_route(p)
    assert has_explicit is True
    assert model == BASELINE_MODEL
    assert kwargs["provider"] == BASELINE_PROVIDER
    assert kwargs["base_url"] == BASELINE_URL


def test_followed_profile_with_pick_preserves_baseline_after_profile_change(tmp_path, monkeypatch):
    _install_routing_mocks(monkeypatch)
    p = _policy(tmp_path, model="user-picked-model", follow_profile_config=True)
    # Simulate the followed profile's current config changing to a different route.
    live_config = {"model": {"default": "profile-changed-model", "provider": "other"}}
    assert live_config["model"]["default"] != _requests(p)["model"]
    has_explicit, (model, kwargs) = _effective_route(p)
    assert has_explicit is True
    assert model == BASELINE_MODEL
    assert kwargs["provider"] == BASELINE_PROVIDER
    assert kwargs["base_url"] == BASELINE_URL


def test_one_turn_override_preserves_baseline_route(tmp_path, monkeypatch):
    _install_routing_mocks(monkeypatch)
    p = build_policy({"source": "cli", "cwd": str(tmp_path), "toolsets": []},
                     {"model": {"default": BASELINE_MODEL}})
    has_explicit, (model, kwargs) = _effective_route(
        p, one_turn_override={"model": "one-turn-model", "provider": "one-turn-provider"})
    assert has_explicit is True
    assert model == BASELINE_MODEL
    assert kwargs["provider"] == BASELINE_PROVIDER


def test_session_model_switch_preserves_baseline_route(tmp_path, monkeypatch):
    _install_routing_mocks(monkeypatch)
    p = build_policy({"source": "cli", "cwd": str(tmp_path), "toolsets": []},
                     {"model": {"default": BASELINE_MODEL}})
    has_explicit, (model, kwargs) = _effective_route(p, session_override="session-switch-model")
    assert has_explicit is True
    assert model == BASELINE_MODEL
    assert kwargs["provider"] == BASELINE_PROVIDER


def test_malformed_authority_never_rewrites_route(tmp_path, monkeypatch):
    """Unreadable authority metadata must not authorize a route rewrite."""
    _install_routing_mocks(monkeypatch)
    p = build_policy({"source": "cli", "cwd": str(tmp_path), "toolsets": []},
                     {"model": {"default": BASELINE_MODEL}})
    import dataclasses
    bad = dataclasses.replace(p, request_json="{not valid json")
    has_explicit, (model, kwargs) = _effective_route(bad)
    assert has_explicit is True
    assert model == BASELINE_MODEL
    assert kwargs["provider"] == BASELINE_PROVIDER
    assert kwargs["base_url"] == BASELINE_URL


# --- API admission requested-route precedence -------------------------------

@pytest.mark.parametrize("settings", [
    {"requested_model": "api-explicit-model"},
    {"requested_provider": "api-explicit-provider"},
    {"route": {"provider": "api-provider", "model": "api-model"}},
    {"session_model": "api-session-model"},
    {"requested_runtime": {"provider": "api-provider"}},
    {"confirmed_runtime_lock": True},
])
def test_api_admission_route_authority_prevents_mode_b_override(tmp_path, monkeypatch, settings):
    """An API request route is separate from session.create request_json."""
    _install_routing_mocks(monkeypatch)
    p = _policy(tmp_path)
    has_explicit = mod.turn_has_explicit_route_authority(
        request_json=p.request_json, api_settings=settings,
    )
    assert has_explicit is True
    model, runtime = mod.resolve_autorouter_for_gateway_turn(
        model=BASELINE_MODEL,
        runtime_kwargs={"provider": BASELINE_PROVIDER, "base_url": BASELINE_URL},
        session_id="api-sess-160",
        session_key="api-key-160",
        platform="gateway",
        has_explicit_model_or_provider=has_explicit,
    )
    assert model == BASELINE_MODEL
    assert runtime["provider"] == BASELINE_PROVIDER
    assert runtime["base_url"] == BASELINE_URL


def test_api_admission_without_requested_route_remains_mode_b_eligible(tmp_path):
    p = _policy(tmp_path)
    settings = {
        "requested_model": None, "requested_provider": None,
        "route": None, "session_model": None, "requested_runtime": None,
        "confirmed_runtime_lock": False, "model_options": {},
    }
    assert mod.turn_has_explicit_route_authority(
        request_json=p.request_json, api_settings=settings,
    ) is False
    assert mod.api_turn_has_explicit_route_authority({"requested_provider": "auto"}) is False


@pytest.mark.parametrize("settings", [{"route": "unexpected"}, {"requested_model": 42}, "not-a-dict"])
def test_malformed_api_route_authority_is_not_rewritten(settings):
    assert mod.api_turn_has_explicit_route_authority(settings) is True
