"""Messaging-API coverage for explicit physical picks reconciling the managed marker.

Dispatch 145, item 1: a typed ``/model <physical-model> --session`` on a live messaging
chat must durably clear a corrupt AutoRouter managed marker at the authorized commit
boundary (``_record_model_switch``) — not only at the next turn boundary inside
``pre_turn_route``. These tests drive ``_handle_model_command`` end to end (event in,
reply out), with a durable store stand-in that survives "restarts" (fresh peek against
the same store) and can be told to fail.

Item 2 (fail-closed): a user-visible switch confirmation must never conceal an
unsuccessful durable state change — the reply carries a warning and the marker is
never silently dropped when the clear did not land.
"""

from __future__ import annotations

import types

import pytest

import hermes_yaml as yaml

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class _DurableStore:
    """Dict-backed session-store stand-in with durable semantics and fault injection."""

    def __init__(self, session_key: str):
        self.session_key = session_key
        self.meta: dict[tuple[str, str], object] = {}
        self.overrides: dict[str, dict] = {}
        self.fail_set_override = False
        self.fail_clear_marker = False
        self.clear_marker_returns_false = False

    # Session-key derivation used by the runner (Telegram recovery is a no-op for Feishu DMs).
    def _generate_session_key(self, source, key_source=None):
        return self.session_key

    def get_session_metadata(self, key, k, default=None):
        return self.meta.get((key, k), default)

    def set_session_metadata(self, key, k, value):
        if k == "autorouter_managed" and value is None:
            if self.fail_clear_marker:
                raise RuntimeError("disk full")
            if self.clear_marker_returns_false:
                return False
        if value is None:
            self.meta.pop((key, k), None)
        else:
            self.meta[(key, k)] = value
        return True

    def set_model_override(self, key, override):
        if self.fail_set_override:
            raise RuntimeError("write failure")
        if override is None:
            self.overrides.pop(key, None)
        else:
            self.overrides[key] = dict(override)

    def get_model_override(self, key):
        override = self.overrides.get(key)
        return dict(override) if override else None


def _make_runner(store: _DurableStore) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._session_db = None
    runner._evict_cached_agent = lambda _session_key: None
    runner.session_store = store
    return runner


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.FEISHU,
            chat_id="ou_test",
            chat_type="dm",
            user_id="user-1",
        ),
    )


def _session_key(runner: GatewayRunner, source: SessionSource) -> str:
    return runner._session_key_for_source(source)


def _seed_corrupt_marker(runner: GatewayRunner, store: _DurableStore, key: str) -> None:
    """What an operator finds: both copies of the marker are corrupt."""
    state = runner._session_state(key)
    state.conversation.autorouter_managed = "corrupt!!"
    store.set_session_metadata(key, "autorouter_managed", "corrupt!!")


def _switch_result(model: str, provider: str):
    from hermes_cli.model_switch import ModelSwitchResult

    return ModelSwitchResult(
        success=True,
        new_model=model,
        target_provider=provider,
        provider_changed=True,
        api_key="sk-test",
        base_url="http://127.0.0.1:9/v1",
        api_mode="chat_completions",
        provider_label=provider,
        is_global=False,
    )


@pytest.fixture
def typed_switch(monkeypatch, tmp_path):
    """Stub provider resolution and pricing probes; return a factory for /model argv handling."""
    import gateway.run as gateway_run

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "gpt-5.4", "provider": "openai-codex"}, "providers": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", dict)
    monkeypatch.setattr(
        "hermes_cli.model_selection_guards.combined_selection_warning",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "hermes_cli.model_selection_guards.selection_context_for_agent",
        lambda *a, **k: None,
    )

    async def _no_context_length(*_a, **_k):
        return None

    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length_async", _no_context_length
    )

    def install(switch_result):
        monkeypatch.setattr(
            "hermes_cli.model_switch.switch_model", lambda **kw: switch_result
        )
        return switch_result

    return install


def test_typed_session_pick_durably_clears_corrupt_marker(typed_switch, tmp_path):
    """`/model <physical> --session` with a corrupt managed marker: the reply confirms the
    switch, the override is persisted, and BOTH copies of the marker are durably gone."""
    store = _DurableStore("feishu:dm:ou_test:user-1")
    runner = _make_runner(store)
    source = _make_event("/model local-physical-a --session").source
    key = runner._session_key_for_source(source)
    _seed_corrupt_marker(runner, store, key)
    typed_switch(_switch_result("local-physical-a", "locala"))

    import asyncio

    reply = asyncio.run(runner._handle_model_command(_make_event("/model local-physical-a --session")))

    assert reply is not None
    assert "Warning" not in reply  # durable writes landed: no concealed failure
    assert "local-physical-a" in reply
    # Override durably persisted.
    assert store.get_model_override(key)["provider"] == "locala"
    assert store.get_model_override(key)["model"] == "local-physical-a"
    # Durable marker cleared — and still clear after a "restart" (fresh peek at the store).
    assert store.get_session_metadata(key, "autorouter_managed") is None
    assert runner._peek_session_state(key).conversation.autorouter_managed is None


def test_marker_clear_failure_is_reported_not_concealed(typed_switch, tmp_path):
    """The durable store refuses the marker clear: the reply warns, the override stays,
    and the corrupt marker is left in place (never silently dropped, never widened)."""
    store = _DurableStore("feishu:dm:ou_test:user-1")
    runner = _make_runner(store)
    source = _make_event("/model local-physical-a --session").source
    key = runner._session_key_for_source(source)
    _seed_corrupt_marker(runner, store, key)
    store.fail_clear_marker = True
    typed_switch(_switch_result("local-physical-a", "locala"))

    import asyncio

    reply = asyncio.run(runner._handle_model_command(_make_event("/model local-physical-a --session")))

    assert reply is not None
    assert "Warning" in reply
    assert "marker" in reply.lower()
    # The pick itself landed durably.
    assert store.get_model_override(key)["model"] == "local-physical-a"
    # The corrupt marker is still there, loudly: never concealed, never dropped.
    assert store.get_session_metadata(key, "autorouter_managed") == "corrupt!!"


def test_marker_clear_false_is_reported_not_concealed(typed_switch, tmp_path):
    """A store that reports the clear not-persisted (False, no exception) is a failure too."""
    store = _DurableStore("feishu:dm:ou_test:user-1")
    runner = _make_runner(store)
    source = _make_event("/model local-physical-a --session").source
    key = runner._session_key_for_source(source)
    _seed_corrupt_marker(runner, store, key)
    store.clear_marker_returns_false = True
    typed_switch(_switch_result("local-physical-a", "locala"))

    import asyncio

    reply = asyncio.run(runner._handle_model_command(_make_event("/model local-physical-a --session")))

    assert reply is not None
    assert "Warning" in reply
    assert store.get_session_metadata(key, "autorouter_managed") == "corrupt!!"


def test_write_through_failure_warns_and_keeps_marker(typed_switch, tmp_path):
    """The session write-through fails: the reply warns (never a clean success), and the
    corrupt managed marker is NOT cleared — a non-durable pick must not launder it away."""
    store = _DurableStore("feishu:dm:ou_test:user-1")
    runner = _make_runner(store)
    source = _make_event("/model local-physical-a --session").source
    key = runner._session_key_for_source(source)
    _seed_corrupt_marker(runner, store, key)
    store.fail_set_override = True
    typed_switch(_switch_result("local-physical-a", "locala"))

    import asyncio

    reply = asyncio.run(runner._handle_model_command(_make_event("/model local-physical-a --session")))

    assert reply is not None
    assert "Warning" in reply
    assert store.get_model_override(key) is None  # nothing durable landed
    assert store.get_session_metadata(key, "autorouter_managed") == "corrupt!!"


def test_virtual_pick_keeps_managed_marker(typed_switch, tmp_path):
    """An AutoRouter virtual pick is the managed binding itself: it persists and warns on
    failure, but never durably clears the marker at this boundary."""
    store = _DurableStore("feishu:dm:ou_test:user-1")
    runner = _make_runner(store)
    source = _make_event("/model autorouter/auto-free --session").source
    key = runner._session_key_for_source(source)
    _seed_corrupt_marker(runner, store, key)
    typed_switch(_switch_result("autorouter/auto-free", "autorouter"))

    import asyncio

    reply = asyncio.run(runner._handle_model_command(_make_event("/model autorouter/auto-free --session")))

    assert reply is not None
    assert "Warning" not in reply
    # Marker preserved — the virtual pick is the managed session re-committing, not an opt-out.
    assert store.get_session_metadata(key, "autorouter_managed") == "corrupt!!"


def test_one_turn_pick_keeps_marker_and_writes_nothing(typed_switch, tmp_path):
    """`--once` is deliberately not durable: no write-through, no marker clear, no warning —
    the pre-existing one-turn restore semantics are untouched."""
    store = _DurableStore("feishu:dm:ou_test:user-1")
    runner = _make_runner(store)
    source = _make_event("/model local-physical-a --once").source
    key = runner._session_key_for_source(source)
    _seed_corrupt_marker(runner, store, key)
    typed_switch(_switch_result("local-physical-a", "locala"))

    import asyncio

    reply = asyncio.run(runner._handle_model_command(_make_event("/model local-physical-a --once")))

    assert reply is not None
    assert "Warning" not in reply
    assert store.get_model_override(key) is None  # --once never persists
    assert store.get_session_metadata(key, "autorouter_managed") == "corrupt!!"


def test_global_pick_durably_clears_corrupt_marker(typed_switch, tmp_path, monkeypatch):
    """A `--global` pick commits to config.yaml: config write succeeds, the session override is
    dropped (config is the single authority), and the marker is durably cleared."""
    store = _DurableStore("feishu:dm:ou_test:user-1")
    runner = _make_runner(store)
    source = _make_event("/model local-physical-a --global").source
    key = runner._session_key_for_source(source)
    _seed_corrupt_marker(runner, store, key)
    typed_switch(_switch_result("local-physical-a", "locala"))
    written = []
    monkeypatch.setattr(
        "hermes_cli.model_switch.persist_model_selection",
        lambda result, config_path=None: written.append((result, config_path)),
    )

    import asyncio

    reply = asyncio.run(runner._handle_model_command(_make_event("/model local-physical-a --global")))

    assert reply is not None
    assert written, "--global must write the selection to config.yaml"
    assert "Warning" not in reply
    # Override dropped in favor of config.yaml, and the marker durably reconciled.
    assert store.get_model_override(key) is None
    assert store.get_session_metadata(key, "autorouter_managed") is None


def test_global_pick_config_failure_warns_and_keeps_marker(typed_switch, tmp_path, monkeypatch):
    """The config.yaml write fails: the reply warns; the marker clear still lands only if the
    session write-through succeeded (the pick is then durable in the routing index)."""
    store = _DurableStore("feishu:dm:ou_test:user-1")
    runner = _make_runner(store)
    source = _make_event("/model local-physical-a --global").source
    key = runner._session_key_for_source(source)
    _seed_corrupt_marker(runner, store, key)
    typed_switch(_switch_result("local-physical-a", "locala"))

    def _boom(_result, config_path=None):
        raise RuntimeError("read-only fs")

    monkeypatch.setattr("hermes_cli.model_switch.persist_model_selection", _boom)

    import asyncio

    reply = asyncio.run(runner._handle_model_command(_make_event("/model local-physical-a --global")))

    assert reply is not None
    assert "Warning" in reply
    # The session write-through succeeded, so the pick is durable and the marker is reconciled.
    assert store.get_model_override(key)["model"] == "local-physical-a"
    assert store.get_session_metadata(key, "autorouter_managed") is None
