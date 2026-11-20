"""Dispatch 161 — live (in-process) AutoRouter consumer unload/reload acceptance.

The host's PluginManager owns middleware through a registration ledger, and
``PluginManager.unload(plugin)`` disposes that registration while the process
keeps running. This module proves the real consumer is removed and the
picker/turn paths fail closed after a live unload, then resumes after reload.

This is distinct from the packaged restart-time activation/opt-out test: no
process restart is used here. All inference is local/synthetic; the pinned
AutoRouter source is loaded from the SHA-verified offline archive.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]


def _archive_root() -> pathlib.Path:
    # The pinned, SHA-verified source is the same one the Desktop harness installs.
    import subprocess
    import tempfile

    archive = (
        REPO / "apps" / "desktop" / "e2e" / "support" / "fixtures"
        / "autorouter-pinned-cdaef496501eadccdde1ac250ef1a18d5b1fd402.tar.gz"
    )
    root = pathlib.Path(tempfile.mkdtemp(prefix="autorouter-live-unload-"))
    subprocess.run(["tar", "-xzf", str(archive), "-C", str(root)], check=True, capture_output=True)
    return root


@pytest.fixture
def live_manager(tmp_path, monkeypatch):
    import hermes_cli.plugins as plugins_mod
    from hermes_cli.plugins import PluginManager
    from hermes_cli.middleware import PRE_INFERENCE_ROUTE_MIDDLEWARE

    home = tmp_path / "hermes"
    plugin_dir = home / "plugins" / "autorouter"
    archive_root = _archive_root()
    shutil.copytree(
        archive_root, plugin_dir,
        ignore=shutil.ignore_patterns("tests", ".git", "__pycache__"),
    )

    # Reuse the harness's known-good seeding/provider installer.
    prep_spec = importlib.util.spec_from_file_location(
        "prep_live_unload",
        REPO / "apps" / "desktop" / "e2e" / "support" / "prepare_autorouter_home.py",
    )
    prep = importlib.util.module_from_spec(prep_spec)
    prep_spec.loader.exec_module(prep)
    prep.seed_autorouter_store(home, archive_root)
    prep.install_mockinfra_provider(home, "http://127.0.0.1:9999/v1")

    settings = {
        "enabled": True,
        "enable_chat_router": True,
        "enable_native_routing": True,
        "providers": ["mockinfra"],
        "free_only": True,
        "model_preference": {
            "mode": "allowlist",
            "include": ["mockinfra/mockinfra-free-model"],
            "exclude": [],
        },
        "min_context": 8192,
        "requires_tools": True,
    }
    config = {
        "plugins": {"enabled": ["autorouter"], "entries": {"autorouter": {"settings": settings}}},
        "model": {
            "provider": "mockinfra",
            "default": "mockinfra-baseline-model",
            "base_url": "http://127.0.0.1:9999/v1",
        },
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: home / "no-bundled")
    monkeypatch.setattr(PluginManager, "_scan_entry_points", lambda self: [])
    monkeypatch.setattr(plugins_mod, "_plugin_manager", None)

    manager = PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    yield manager, PRE_INFERENCE_ROUTE_MIDDLEWARE
    monkeypatch.setattr(plugins_mod, "_plugin_manager", None)


def _consumer(manager, kind):
    return manager._middleware[kind][0]


def _route_call(consumer, session_id="live-unload-1"):
    return consumer(
        session_id=session_id, platform="gateway",
        requested_model="baseline-model", requested_provider="mockinfra",
        hermes_profile="default", explicit_model=False,
    )


def test_live_unload_removes_registered_consumer_and_fails_closed(live_manager):
    """Live unload removes the real consumer; picker/turn guards fail closed."""
    import hermes_cli.autorouter_picker as picker
    from gateway.autorouter_turn_route import resolve_autorouter_virtual_turn

    manager, kind = live_manager
    assert manager.has_middleware(kind) is True
    assert picker.actionable() is True
    assert _route_call(_consumer(manager, kind))["status"] == "route"

    # Live, in-process unload (no process restart).
    assert manager.unload("autorouter") is True
    assert manager.has_middleware(kind) is False
    assert manager._ownership_ledger.get("autorouter") in (None, {})

    # No new Mode B route: there is no registered consumer to invoke.
    assert manager.invoke_middleware(kind, **{}) == []

    # Picker actionability updates with the registration.
    assert picker.actionable() is False
    assert picker.picker_row("mockinfra") is None
    assert "consumer" in (picker.gate_reason() or "")

    # Existing/virtual selections refuse safely before any outbound request.
    with pytest.raises(RuntimeError, match="no AutoRouter consumer is registered"):
        resolve_autorouter_virtual_turn(
            model="auto-free", runtime_kwargs={"provider": "autorouter"},
            session_id="s1", session_key="k1", platform="gateway",
        )


def test_live_reload_restores_authorization(live_manager):
    """After live unload, re-discovery re-registers the consumer and routing resumes."""
    manager, kind = live_manager
    consumer_before = _consumer(manager, kind)
    assert _route_call(consumer_before)["status"] == "route"
    assert manager.unload("autorouter") is True

    manager.discover_and_load(force=True)
    assert manager.has_middleware(kind) is True
    consumer_after = _consumer(manager, kind)
    assert consumer_after is not consumer_before
    result = _route_call(consumer_after, session_id="live-unload-2")
    assert result["status"] == "route"
    assert result["provider"] == "mockinfra"
    assert result["model"] == "mockinfra-free-model"
