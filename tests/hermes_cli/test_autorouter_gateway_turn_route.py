"""Gateway turn-time AutoRouter resolution (Dispatch 158/158-R).

Focused regressions for `gateway.autorouter_turn_route`: explicit virtual
AutoRouter picks resolve to physical routes before agent construction, ordinary
pre-inference routing applies only without explicit model/provider authority, and
all refusal/misbehavior paths fail closed before any provider request.
"""
from __future__ import annotations

import types

import pytest

from gateway import autorouter_turn_route as mod


class _Route:
    def __init__(self, status, provider=None, model=None, reason=None):
        self.status = status
        self.provider = provider
        self.model = model
        self.reason = reason


def _physical_runtime(provider="mockinfra", model="mockinfra-free-model"):
    return {
        "provider": provider,
        "requested_provider": provider,
        "api_key": "synthetic-key",
        "base_url": "http://127.0.0.1:9999/v1",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
        "max_tokens": 1024,
        "capabilities": {"tools": True},
        "request_overrides": {},
        "model": model,
    }


def test_virtual_pick_not_detected_returns_none(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.autorouter_picker.is_autorouter_pick",
        lambda model, provider: False,
    )
    out = mod.resolve_autorouter_virtual_turn(
        model="mockinfra-free-model",
        runtime_kwargs={"provider": "mockinfra"},
        session_id="s1",
        session_key="k1",
        platform="gateway",
    )
    assert out is None


def test_virtual_pick_not_actionable_refuses(monkeypatch):
    monkeypatch.setattr("hermes_cli.autorouter_picker.is_autorouter_pick", lambda m, p: True)
    monkeypatch.setattr("hermes_cli.autorouter_picker.actionable", lambda: False)
    monkeypatch.setattr("hermes_cli.autorouter_picker.gate_reason", lambda: "chat router disabled")
    with pytest.raises(RuntimeError, match="chat router disabled"):
        mod.resolve_autorouter_virtual_turn(
            model="auto-free",
            runtime_kwargs={"provider": "autorouter"},
            session_id="s1",
            session_key="k1",
            platform="gateway",
        )


def test_virtual_pick_resolves_to_physical_route(monkeypatch):
    monkeypatch.setattr("hermes_cli.autorouter_picker.is_autorouter_pick", lambda m, p: True)
    monkeypatch.setattr("hermes_cli.autorouter_picker.actionable", lambda: True)

    def fake_resolve(*, session_id, platform):
        assert session_id == "s1"
        assert platform == "gateway"
        return _physical_runtime(), {"provider": "mockinfra", "model": "mockinfra-free-model", "session_id": "s1"}

    monkeypatch.setattr("hermes_cli.autorouter_picker.resolve_autorouter_physical", fake_resolve)

    model, kwargs = mod.resolve_autorouter_virtual_turn(
        model="auto-free",
        runtime_kwargs={"provider": "autorouter", "api_key": "autorouter-virtual-provider"},
        session_id="s1",
        session_key="k1",
        platform="gateway",
    )
    assert model == "mockinfra-free-model"
    assert kwargs["provider"] == "mockinfra"
    assert kwargs.get("api_key") != "autorouter-virtual-provider"
    assert "auto-free" not in str(kwargs)


def test_virtual_pick_resolver_failure_refuses(monkeypatch):
    monkeypatch.setattr("hermes_cli.autorouter_picker.is_autorouter_pick", lambda m, p: True)
    monkeypatch.setattr("hermes_cli.autorouter_picker.actionable", lambda: True)

    def boom(*, session_id, platform):
        raise RuntimeError("no eligible free route")

    monkeypatch.setattr("hermes_cli.autorouter_picker.resolve_autorouter_physical", boom)
    with pytest.raises(RuntimeError, match="no eligible free route"):
        mod.resolve_autorouter_virtual_turn(
            model="auto-free",
            runtime_kwargs={"provider": "autorouter"},
            session_id="s1",
            session_key="k1",
            platform="gateway",
        )


def test_virtual_pick_requires_stable_binding(monkeypatch):
    monkeypatch.setattr("hermes_cli.autorouter_picker.is_autorouter_pick", lambda m, p: True)
    monkeypatch.setattr("hermes_cli.autorouter_picker.actionable", lambda: True)
    with pytest.raises(RuntimeError, match="no stable session binding"):
        mod.resolve_autorouter_virtual_turn(
            model="auto-free",
            runtime_kwargs={"provider": "autorouter"},
            session_id=None,
            session_key=None,
            platform="gateway",
        )


def test_pre_inference_skips_when_explicit_authority():
    model, kwargs = mod.apply_pre_inference_route_turn(
        model="explicit-model",
        runtime_kwargs={"provider": "openai"},
        session_id="s1",
        session_key="k1",
        platform="gateway",
        has_explicit_model_or_provider=True,
    )
    assert model == "explicit-model"
    assert kwargs["provider"] == "openai"


def test_pre_inference_skips_without_registered_consumer(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda name: False)
    model, kwargs = mod.apply_pre_inference_route_turn(
        model="default-model",
        runtime_kwargs={"provider": "mockinfra"},
        session_id="s1",
        session_key="k1",
        platform="gateway",
        has_explicit_model_or_provider=False,
    )
    assert model == "default-model"
    assert kwargs["provider"] == "mockinfra"


def test_pre_inference_probe_failure_refuses_closed(monkeypatch):
    def boom(name):
        raise RuntimeError("plugin manager unavailable")

    monkeypatch.setattr("hermes_cli.plugins.has_middleware", boom)
    with pytest.raises(RuntimeError, match="capability probe failed"):
        mod.apply_pre_inference_route_turn(
            model="default-model",
            runtime_kwargs={"provider": "mockinfra"},
            session_id="s1",
            session_key="k1",
            platform="gateway",
            has_explicit_model_or_provider=False,
        )


def test_pre_inference_none_result_refuses(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda name: True)
    monkeypatch.setattr("hermes_cli.middleware.apply_pre_inference_route_middleware", lambda metadata, platform=None: None)
    with pytest.raises(RuntimeError, match="no result"):
        mod.apply_pre_inference_route_turn(
            model="default-model",
            runtime_kwargs={"provider": "mockinfra"},
            session_id="s1",
            session_key="k1",
            platform="gateway",
            has_explicit_model_or_provider=False,
        )


def test_pre_inference_refuse_status_refuses(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda name: True)
    monkeypatch.setattr(
        "hermes_cli.middleware.apply_pre_inference_route_middleware",
        lambda metadata, platform=None: _Route("refuse", reason="no eligible free route"),
    )
    with pytest.raises(RuntimeError, match="no eligible free route"):
        mod.apply_pre_inference_route_turn(
            model="default-model",
            runtime_kwargs={"provider": "mockinfra"},
            session_id="s1",
            session_key="k1",
            platform="gateway",
            has_explicit_model_or_provider=False,
        )


def test_pre_inference_passthrough_keeps_route(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda name: True)
    monkeypatch.setattr(
        "hermes_cli.middleware.apply_pre_inference_route_middleware",
        lambda metadata, platform=None: _Route("passthrough"),
    )
    model, kwargs = mod.apply_pre_inference_route_turn(
        model="default-model",
        runtime_kwargs={"provider": "mockinfra", "base_url": "http://127.0.0.1:1/v1"},
        session_id="s1",
        session_key="k1",
        platform="gateway",
        has_explicit_model_or_provider=False,
    )
    assert model == "default-model"
    assert kwargs["provider"] == "mockinfra"


def test_pre_inference_route_resolves_physical_runtime(monkeypatch):
    captured = {}

    monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda name: True)

    def fake_apply(metadata, platform=None):
        captured["metadata"] = metadata
        captured["platform"] = platform
        return _Route("route", provider="mockinfra", model="mockinfra-free-model", reason="ok")

    monkeypatch.setattr("hermes_cli.middleware.apply_pre_inference_route_middleware", fake_apply)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda requested, target_model: _physical_runtime(provider=requested, model=target_model),
    )

    model, kwargs = mod.apply_pre_inference_route_turn(
        model="default-model",
        runtime_kwargs={"provider": "mockinfra", "requested_provider": "mockinfra"},
        session_id="sess-123",
        session_key="k1",
        platform="gateway",
        has_explicit_model_or_provider=False,
    )
    assert model == "mockinfra-free-model"
    assert kwargs["provider"] == "mockinfra"
    assert captured["metadata"]["session_id"] == "sess-123"
    assert captured["metadata"]["explicit_model"] is False
    assert captured["platform"] == "gateway"


def test_pre_inference_route_unusable_runtime_refuses(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda name: True)
    monkeypatch.setattr(
        "hermes_cli.middleware.apply_pre_inference_route_middleware",
        lambda metadata, platform=None: _Route("route", provider="badprov", model="m"),
    )

    def boom(requested, target_model):
        raise RuntimeError("unknown provider")

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", boom)
    with pytest.raises(RuntimeError, match="not usable on this host"):
        mod.apply_pre_inference_route_turn(
            model="default-model",
            runtime_kwargs={"provider": "mockinfra"},
            session_id="s1",
            session_key="k1",
            platform="gateway",
            has_explicit_model_or_provider=False,
        )


def test_resolve_for_turn_prefers_virtual_pick_over_pre_inference(monkeypatch):
    called = {"pre": 0}

    def fake_virtual(**kwargs):
        return ("physical-model", {"provider": "nvidia"})

    def fake_pre(**kwargs):
        called["pre"] += 1
        return ("other", {"provider": "x"})

    monkeypatch.setattr(mod, "resolve_autorouter_virtual_turn", fake_virtual)
    monkeypatch.setattr(mod, "apply_pre_inference_route_turn", fake_pre)

    model, _kwargs = mod.resolve_autorouter_for_gateway_turn(
        model="auto-free",
        runtime_kwargs={"provider": "autorouter"},
        session_id="s1",
        session_key="k1",
        platform="gateway",
        has_explicit_model_or_provider=False,
    )
    assert model == "physical-model"
    assert called["pre"] == 0


def test_gateway_runtime_conversion_failure_never_falls_back_to_raw_fields(monkeypatch):
    """A failed canonical runtime conversion cannot be bypassed by raw kwargs."""
    monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda name: True)
    monkeypatch.setattr(
        "hermes_cli.middleware.apply_pre_inference_route_middleware",
        lambda metadata, platform=None: _Route(
            "route", provider="mockinfra", model="mockinfra-free-model",
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda requested, target_model: _physical_runtime(provider=requested, model=target_model),
    )

    def reject_runtime(runtime):
        raise ValueError("invalid canonical runtime conversion")

    monkeypatch.setattr("gateway.run._runtime_agent_kwargs", reject_runtime)
    with pytest.raises(RuntimeError, match="routed provider is not usable"):
        mod.apply_pre_inference_route_turn(
            model="default-model",
            runtime_kwargs={"provider": "mockinfra"},
            session_id="s1",
            session_key="k1",
            platform="gateway",
            has_explicit_model_or_provider=False,
        )


def test_virtual_selection_refuses_incomplete_physical_route(monkeypatch):
    """A malformed producer receipt cannot send 'auto-free' on the wire."""
    monkeypatch.setattr("hermes_cli.autorouter_picker.is_autorouter_pick",
                        lambda model, provider: model in {"auto-free", "autorouter/auto-free"} or provider == "autorouter")
    monkeypatch.setattr("hermes_cli.autorouter_picker.actionable", lambda: True)
    monkeypatch.setattr(
        "hermes_cli.autorouter_picker.resolve_autorouter_physical",
        lambda **kwargs: ({"provider": "mockinfra", "base_url": "http://127.0.0.1:9999/v1"}, {"provider": "mockinfra"}),
    )
    with pytest.raises(RuntimeError, match="incomplete physical route"):
        mod.resolve_autorouter_virtual_turn(
            model="auto-free", runtime_kwargs={"provider": "autorouter"},
            session_id="s1", session_key="k1", platform="gateway",
        )
