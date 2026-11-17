"""AutoRouter · Auto (Free) picker row and virtual provider (issue #116).

Covers the host half of the selection contract:

* inventory row appears only when actionable (raw-config opt-in + registered
  consumer); absent on unsupported hosts and unactionable configs — never a
  decorative clickable row;
* ``switch_model``/validation accept ``auto-free`` exactly under the same gate;
* explicit AutoRouter picks resolve to the PHYSICAL route via the consumer;
* refusal/malfunction => RuntimeError before any provider client exists;
* explicit physical picks remain authoritative.
"""
from __future__ import annotations

import os
import pytest

from hermes_cli import plugins
from hermes_cli import inventory as _inventory
from hermes_cli.autorouter_picker import (
    actionable, gate_reason, is_autorouter_pick, picker_row, virtual_marker_runtime,
)
from hermes_cli.middleware import PRE_INFERENCE_ROUTE_MIDDLEWARE
from hermes_cli.plugins import PluginManager

_CONSUMER = """
import os

_OUTCOMES = {
    'route': {'status': 'route', 'provider': 'nvidia', 'model': 'nvidia/nemotron-free',
              'reason': 'fixture route'},
    'refuse': {'status': 'refuse', 'reason': 'fixture refusal: no eligible free model'},
    'passthrough': {'status': 'passthrough', 'reason': 'native_routing_disabled'},
    'malformed': {'status': 'route', 'provider': 'NOT A PROVIDER!', 'model': 'x'},
    'raise': RuntimeError('consumer exploded'),
}

def register(ctx):
    def consume(**kwargs):
        outcome = _OUTCOMES[os.environ['FIXTURE_OUTCOME']]
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)

    ctx.register_middleware('pre_inference_route', consume)
"""


def _write_plugin(home, name, source):
    plugin_dir = home / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(f"name: {name}\nversion: 0.1.0\n")
    (plugin_dir / "__init__.py").write_text(source)


def _config(home, *, enabled, chat_router, native=False):
    settings = {
        "enabled": bool(enabled),
        "enable_chat_router": bool(chat_router),
        "enable_native_routing": bool(native),
    }
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [autorouter]\n"
        "  entries:\n    autorouter:\n      settings:\n"
        + "".join(f"        {k}: {str(v).lower()}\n" for k, v in settings.items())
    )


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_plugin(home, "autorouter", _CONSUMER)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("FIXTURE_OUTCOME", "route")
    monkeypatch.setenv("NVIDIA_API_KEY", "fixture-key")

    def _load(enabled, chat_router, native=False):
        _config(home, enabled=enabled, chat_router=chat_router, native=native)
        manager = PluginManager()
        manager.discover_and_load()
        monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
        return manager

    return _load


def _no_plugin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()
    return manager


# ── gate + row ────────────────────────────────────────────────────────────────

def test_row_absent_without_consumer(tmp_path, monkeypatch):
    _no_plugin(tmp_path, monkeypatch)
    assert picker_row() is None
    assert actionable() is False
    assert gate_reason() is not None


@pytest.mark.parametrize("enabled,chat_router,expect_reason_part", [
    (False, False, "plugin is not enabled"),
    (True, False, "chat router is not enabled"),
])
def test_row_absent_until_raw_config_opts_in(host, enabled, chat_router, expect_reason_part):
    host(enabled=enabled, chat_router=chat_router)
    assert picker_row() is None
    assert expect_reason_part in (gate_reason() or "")


def test_row_present_when_actionable(host):
    host(enabled=True, chat_router=True)
    row = picker_row("")
    assert row is not None
    assert row["slug"] == "autorouter"
    assert row["name"] == "AutoRouter"
    assert row["models"] == ["auto-free"]
    assert row["source"] == "virtual"
    assert row["total_models"] == 1


def test_row_is_current_only_for_the_virtual_pick(host):
    host(enabled=True, chat_router=True)
    assert picker_row("autorouter")["is_current"] is True
    assert picker_row("nvidia")["is_current"] is False


def test_plugin_disabled_never_shows_row_even_with_chat_router(host):
    host(enabled=False, chat_router=True)
    assert picker_row() is None


# ── virtual-pick detection + validation accept/reject ─────────────────────────

def test_is_autorouter_pick():
    assert is_autorouter_pick("auto-free", "autorouter")
    assert is_autorouter_pick("autorouter/auto-free", "")
    assert is_autorouter_pick("autorouter/auto-free", "nvidia") is False
    assert is_autorouter_pick("default", "moa") is False


def test_validation_accepts_auto_free_only_when_actionable(host):
    from hermes_cli.models_validate import validate_requested_model
    host(enabled=True, chat_router=True)
    accepted = validate_requested_model("auto-free", "autorouter")
    assert accepted["accepted"] is True
    rejected = validate_requested_model("auto-paid", "autorouter")
    assert rejected["accepted"] is False


def test_validation_refuses_selection_on_unsupported_host(host):
    from hermes_cli.models_validate import validate_requested_model
    host(enabled=True, chat_router=False)
    rejected = validate_requested_model("auto-free", "autorouter")
    assert rejected["accepted"] is False
    assert "not actionable" in rejected.get("message", "")


# ── shortcut / placeholder runtime ────────────────────────────────────────────

def test_marker_runtime_is_credential_free():
    marker = virtual_marker_runtime("autorouter")
    assert marker["provider"] == "autorouter"
    assert marker["api_key"] == "autorouter-virtual-provider"
    assert marker["base_url"] == "autorouter://local"
    from hermes_cli.runtime_provider import _resolve_requested_shortcuts
    shortcut = _resolve_requested_shortcuts("autorouter", None, None, "auto-free")
    assert shortcut["provider"] == "autorouter"
    assert shortcut["source"] == "autorouter-virtual-provider"


# ── resolution + fail-closed ─────────────────────────────────────────────────

def test_resolve_physical_route_excludes_virtual_marker(host, monkeypatch):
    from hermes_cli import autorouter_picker
    host(enabled=True, chat_router=True)
    runtime, receipt = autorouter_picker.resolve_autorouter_physical(session_id="sess-1", platform="tui")
    assert receipt == {"provider": "nvidia", "model": "nvidia/nemotron-free", "session_id": "sess-1"}
    # The virtual identifier never travels toward the provider runtime.
    assert runtime["model"] != "auto-free" if "model" in runtime else True
    assert receipt["provider"] == "nvidia"


def test_refused_route_refuses_before_network(host, monkeypatch):
    from hermes_cli import autorouter_picker
    host(enabled=True, chat_router=True)
    monkeypatch.setenv("FIXTURE_OUTCOME", "refuse")
    with pytest.raises(RuntimeError) as excinfo:
        autorouter_picker.resolve_autorouter_physical(session_id="sess-1", platform="tui")
    assert "before any provider request" in str(excinfo.value)


def test_malformed_route_refuses_before_network(host, monkeypatch):
    from hermes_cli import autorouter_picker
    host(enabled=True, chat_router=True)
    monkeypatch.setenv("FIXTURE_OUTCOME", "malformed")
    with pytest.raises(RuntimeError) as excinfo:
        autorouter_picker.resolve_autorouter_physical(session_id="sess-1", platform="tui")
    assert "before any provider request" in str(excinfo.value)


def test_raise_refuses_before_network(host, monkeypatch):
    from hermes_cli import autorouter_picker
    host(enabled=True, chat_router=True)
    monkeypatch.setenv("FIXTURE_OUTCOME", "raise")
    with pytest.raises(RuntimeError):
        autorouter_picker.resolve_autorouter_physical(session_id="sess-1", platform="tui")


# ── inventory explicit-only filter ────────────────────────────────────────────

def _ctx(host_home, provider="nvidia"):
    from hermes_cli.inventory import load_picker_context
    return load_picker_context()


def test_explicit_only_filter_keeps_autorouter_when_actionable(host):
    host(enabled=True, chat_router=True)
    ctx = _ctx(None)
    row = _inventory._filter_explicit_provider_rows(
        [picker_row()], ctx)
    assert any(r["slug"] == "autorouter" for r in row)


def test_explicit_only_filter_drops_autorouter_when_disabled(host):
    host(enabled=True, chat_router=False)
    ctx = _ctx(None)
    # Row cannot be built at all when disabled; direct filter check keeps semantics pinned.
    rows = [r for r in _inventory._filter_explicit_provider_rows(
        [{"slug": "autorouter", "name": "AutoRouter"}], ctx)]
    assert not any(r["slug"] == "autorouter" for r in rows)


# ── pick loading order (recommends virtual pick survives row surgery) ─────────

def test_autorouter_row_precedes_provider_catalog(host):
    host(enabled=True, chat_router=True)
    moa = _inventory._moa_provider_row
    from hermes_cli.inventory import build_models_payload
    row = build_models_payload(_ctx(None), explicit_only=True, non_blocking_catalogs=True)
    slugs = [r.get("slug") for r in row["providers"]]
    assert slugs[0] == "autorouter"
    assert "autorouter" in slugs


# ── _make_agent MODE A intercept ──────────────────────────────────────────────
def test_explicit_autorouter_pin_resolves_physical_route(host, monkeypatch):
    host(enabled=True, chat_router=True)
    monkeypatch.setenv("NVIDIA_API_KEY", "fixture-key")
    monkeypatch.setenv("FIXTURE_OUTCOME", "route")
    from tui_gateway import agent_factory
    override, provider_override = agent_factory._autorouter_selection_override(
        "rt", "sess-1", "key", {"model": "auto-free", "provider": "autorouter"}, None)
    assert override == {"model": "nvidia/nemotron-free", "provider": "nvidia"}
    assert provider_override is None


def test_explicit_autorouter_pin_refuses_before_build(host, monkeypatch):
    host(enabled=True, chat_router=True)
    monkeypatch.setenv("NVIDIA_API_KEY", "fixture-key")
    monkeypatch.setenv("FIXTURE_OUTCOME", "refuse")
    from tui_gateway import agent_factory
    with pytest.raises(RuntimeError):
        agent_factory._autorouter_selection_override(
            "rt", "sess-1", "key", {"model": "auto-free", "provider": "autorouter"}, None)


def test_malformed_pin_refuses_before_build(host, monkeypatch):
    host(enabled=True, chat_router=True)
    monkeypatch.setenv("NVIDIA_API_KEY", "fixture-key")
    monkeypatch.setenv("FIXTURE_OUTCOME", "malformed")
    from tui_gateway import agent_factory
    with pytest.raises(RuntimeError):
        agent_factory._autorouter_selection_override(
            "rt", "sess-1", "key", {"model": "auto-free", "provider": "autorouter"}, None)


def test_explicit_physical_pick_is_not_rewritten(host, monkeypatch):
    host(enabled=True, chat_router=True)
    monkeypatch.setenv("NVIDIA_API_KEY", "fixture-key")
    monkeypatch.setenv("FIXTURE_OUTCOME", "route")
    from tui_gateway import agent_factory
    override = agent_factory._autorouter_selection_override(
        "rt", "sess-2", "key", {"model": "nvidia/physical", "provider": "nvidia"}, None)
    assert override is None  # physical picks fall through untouched


def test_plain_virtual_pick_refuses_when_unresolvable(host, monkeypatch):
    host(enabled=True, chat_router=False)
    monkeypatch.setenv("NVIDIA_API_KEY", "fixture-key")
    monkeypatch.setenv("FIXTURE_OUTCOME", "route")
    from tui_gateway import agent_factory
    with pytest.raises(RuntimeError):
        agent_factory._autorouter_selection_override(
            "rt", "sess-3", "key", {"model": "auto-free", "provider": "autorouter"}, None)


def test_wide_virtual_pin_is_recognized(host):
    host(enabled=True, chat_router=True)
    monkeypatch = host  # reuse fixture signature: this test reaches through via monkeypatch below
    from tui_gateway import agent_factory
    from hermes_cli.autorouter_picker import is_autorouter_pick
    assert is_autorouter_pick("autorouter/auto-free", "autorouter")
    assert is_autorouter_pick("autorouter/auto-free", "")


def test_agent_init_guard_rejects_virtual_provider():
    # The virtual provider must never reach AIAgent construction.
    from agent.agent_init import init_agent

    class _Shim:
        model = ""
        session_cwd = None
        provider = ""
        requested_provider = ""
        reasoning_config = None
        background_review_callback = None
        skip_background_review = True
        log_prefix = ""

    with pytest.raises(RuntimeError):
        init_agent(_Shim(), model="auto-free", provider="autorouter")


# ── /model switch into AutoRouter commits the physical route live, pin virtual ──

def test_switch_into_autorouter_commits_physical_pin_stays_virtual(host, monkeypatch):
    host(enabled=True, chat_router=True)
    monkeypatch.setenv("NVIDIA_API_KEY", "fixture-key")
    monkeypatch.setenv("FIXTURE_OUTCOME", "route")
    import contextlib
    from unittest.mock import patch

    from tui_gateway import server

    class _FakeResult:
        success = True
        error_message = ""
        warning_message = ""
        new_model = "auto-free"
        target_provider = "autorouter"
        base_url = "autorouter://local"
        api_key = "autorouter-virtual-provider"
        api_mode = "chat_completions"

    class _FakeAgent:
        def __init__(self):
            self.model = "minimax/m3"
            self.provider = "minimax"
            self.base_url = ""
            self.api_key = ""

        def switch_model(self, **kwargs):
            self.model = kwargs["new_model"]
            self.provider = kwargs["new_provider"]
            self.base_url = kwargs["base_url"]
            self.api_key = kwargs["api_key"]
            self.api_mode = kwargs["api_mode"]

    session = {"agent": _FakeAgent(), "session_key": "k-1", "model_override": None}
    with (
        patch("hermes_cli.model_switch.switch_model", return_value=_FakeResult()),
        patch("tui_gateway.server._emit"),
        patch("tui_gateway.server._restart_slash_worker"),
        patch("tui_gateway.server._session_info", return_value={}),
        patch("tui_gateway.server._emit_session_info"),
        patch("tui_gateway.server._transcript_claim", lambda s: contextlib.nullcontext()),
        patch("tui_gateway.server._persist_live_session_runtime", side_effect=lambda s: None),
        patch("tui_gateway.server._persist_live_session_system_prompt"),
        patch("tui_gateway.server._append_model_switch_marker"),
    ):
        server._apply_model_switch("sid", session, "auto-free --provider autorouter",
                                   confirm_expensive_model=True)

    # Live agent runs the session-consistent physical route.
    assert session["agent"].provider == "nvidia"
    assert session["agent"].model == "nvidia/nemotron-free"
    assert session["agent"].api_key == "fixture-key"
    # Operator-visible receipt shows the picker selection, never the resolved route.
    assert session["model_override"]["provider"] == "autorouter"
    assert session["model_override"]["model"] == "auto-free"


def test_switch_into_autorouter_fails_closed_on_refusal(host, monkeypatch):
    host(enabled=True, chat_router=True)
    monkeypatch.setenv("NVIDIA_API_KEY", "fixture-key")
    monkeypatch.setenv("FIXTURE_OUTCOME", "refuse")
    import contextlib
    from unittest.mock import patch

    from tui_gateway import server

    class _FakeResult:
        success = True
        error_message = ""
        warning_message = ""
        new_model = "auto-free"
        target_provider = "autorouter"
        base_url = "autorouter://local"
        api_key = "autorouter-virtual-provider"
        api_mode = "chat_completions"

    class _FakeAgent:
        def __init__(self):
            self.model = "minimax/m3"
            self.provider = "minimax"
            self.base_url = ""
            self.api_key = ""

        def switch_model(self, **kwargs):
            self.model = kwargs["new_model"]
            self.provider = kwargs["new_provider"]

    session = {"agent": _FakeAgent(), "session_key": "k-1", "model_override": None}
    with (
        patch("hermes_cli.model_switch.switch_model", return_value=_FakeResult()),
        patch("tui_gateway.server._emit"),
        patch("tui_gateway.server._restart_slash_worker"),
        patch("tui_gateway.server._session_info", return_value={}),
        patch("tui_gateway.server._emit_session_info"),
        patch("tui_gateway.server._transcript_claim", lambda s: contextlib.nullcontext()),
        patch("tui_gateway.server._persist_live_session_runtime", side_effect=lambda s: None),
        patch("tui_gateway.server._persist_live_session_system_prompt"),
        patch("tui_gateway.server._append_model_switch_marker"),
    ):
        with pytest.raises(RuntimeError):
            server._apply_model_switch("sid", session, "auto-free --provider autorouter",
                                       confirm_expensive_model=True)
    # Unswitched: the agent stands unchanged, no override recorded.
    assert session["agent"].model == "minimax/m3"
    assert session["model_override"] is None


def test_session_info_reports_virtual_selection(host, monkeypatch, tmp_path):
    host(enabled=True, chat_router=True)
    import types
    from tui_gateway import server

    agent = types.SimpleNamespace(
        provider="nvidia", model="nvidia/nemotron-free",
        reasoning_config=None, service_tier=None, _cached_system_prompt="s",
    )
    session = {
        "model_override": {"provider": "autorouter", "model": "auto-free"},
        "session_key": "k-1",
    }
    info = server._session_info(agent, session)
    assert info["provider"] == "autorouter"
    assert info["model"] == "auto-free"


# ── Other plugins' consumers do not count as the AutoRouter consumer ─────────

def _write_fixture_plugin(home, name, source):
    target = home / "plugins" / name
    if target.exists():
        import shutil
        shutil.rmtree(target)
    _write_plugin(home, name, source)


def _load_host(home, monkeypatch):
    manager = PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    return manager


def test_foreign_plugin_consumer_does_not_unlock_picker(tmp_path, monkeypatch):
    # Plugin "other-routing" registers a pre_inference_route consumer; the
    # AutoRouter plugin (with its own consumer) is NOT installed. The row must
    # not render actionable even though *a* consumer exists.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "plugins").mkdir(exist_ok=True)
    _write_plugin(home, "other-routing", _CONSUMER)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [other-routing]\n"
        "  entries:\n    autorouter:\n      settings:\n"
        "        enabled: true\n        enable_chat_router: true\n        providers: ['nvidia']\n")
    _load_manager(home, monkeypatch)
    assert actionable() is False
    assert picker_row() is None
    reason = gate_reason() or ""
    assert "consumer" in reason


def _load_manager(home, monkeypatch):
    manager = PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    return manager


def test_unload_of_self_consumer_retires_actionable_state(host, monkeypatch):
    home = host(enabled=True, chat_router=True)
    """After the AutoRouter plugin's consumer registration is disposed, the
    picker must fall back to the absent state — never a stale actionable row."""
    assert picker_row() is not None
    manager = _load_host(home, monkeypatch)
    loaded = manager._plugins.get("autorouter")
    assert loaded is not None
    manager.unload("autorouter")
    assert actionable() is False
    assert picker_row() is None
