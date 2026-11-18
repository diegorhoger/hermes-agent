"""AutoRouter continuous per-turn routing in the messaging gateway execution path.

The messaging gateway (Telegram/Slack/etc.) historically resolved the route at
agent construction time only, and an explicit ``autorouter/auto-free`` pick
that failed physical resolution silently widened to the stock route. These
tests pin the repaired contract:

* routing is evaluated at a safe pre-turn boundary — before any provider
  request of the turn, before agent construction;
* explicit physical picks are never rewritten and the consumer never runs for
  them;
* an explicit AutoRouter virtual pick re-resolves to the session-consistent
  physical route every turn; refusal fails closed with zero provider
  requests;
* ordinary sessions route per turn only when the operator enabled the
  consumer; consumer refusal fails the turn closed; the provable
  ``passthrough`` opt-out runs stock;
* a route change between turns rebuilds the agent on the new physical route
  with conversation history intact;
* sessions stay isolated: routing decisions bind to the session id the
  consumer sees.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from gateway.autorouter_turn import (
    AutoRouterRefusal,
    pre_turn_route,
    resolve_virtual_override,
)
from gateway.config import Platform

from tests.gateway.test_local_viewer_events_submit import _gateway_runner, _RecordingAgent

_STOCK_RUNTIME = {"provider": "custom", "api_key": "k1", "base_url": "http://stock.invalid"}


# --------------------------------------------------------------------------
# Fixture consumer plugin (same shape as tests/hermes_cli/test_pre_inference_route_host.py)
# --------------------------------------------------------------------------

_CONSUMER_PLUGIN = """
import os

_OUTCOMES = {
    'route': {'status': 'route', 'provider': 'locala', 'model': 'local-model-a',
              'reason': 'fixture route'},
    'route_b': {'status': 'route', 'provider': 'localb', 'model': 'local-model-b',
                'reason': 'fixture route b'},
    'refuse': {'status': 'refuse', 'reason': 'fixture refusal: no eligible free model'},
    'passthrough': {'status': 'passthrough', 'reason': 'native_routing_disabled'},
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


def _stub_runner_kwargs(monkeypatch):
    """The route resolver (credentials through Hermes) returns canned physical kwargs."""
    seen = []

    def fake_resolve(provider, target_model=None):
        seen.append((provider, target_model))
        return {
            "provider": provider,
            "api_key": f"sk-dummy-{provider}",
            "base_url": f"http://127.0.0.1:9/{provider}",
            "api_mode": "chat_completions",
            "requested_provider": provider,
            "request_overrides": {},
            "capabilities": {},
        }

    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", fake_resolve)
    return seen


def _no_policy(monkeypatch):
    monkeypatch.setattr("gateway.session_policy.policy_for_source", lambda runner, source: None)


def _telegram():
    from gateway.session import SessionSource

    return SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1", user_id="u",
                         user_name="n", chat_name="n", chat_type="dm", thread_id=None)


class _RecordingStore:
    """Minimal session-store stand-in: in-memory metadata + overrides, durable reads."""

    def __init__(self):
        self.meta = {}

    def get_session_metadata(self, key, k, default=None):
        return self.meta.get((key, k), default)

    def set_session_metadata(self, key, k, value):
        if value is None:
            self.meta.pop((key, k), None)
        else:
            self.meta[(key, k)] = value
        return True


def _fake_runner(override=None, *, peek_error=None):
    """A runner fake with readable session state + a durable metadata store."""
    states = {}

    def _peek(_key):
        if peek_error is not None:
            raise peek_error
        return states.get(_key)

    def _state(_key):
        return states.setdefault(_key, SimpleNamespace(conversation=SimpleNamespace(
            model_override=override, autorouter_managed=None)))

    runner = SimpleNamespace(
        _peek_session_state=_peek,
        _session_state=_state,
        session_store=_RecordingStore(),
    )
    return runner, states


def _peek_state(override):
    state = SimpleNamespace(conversation=SimpleNamespace(model_override=override, autorouter_managed=None))
    return lambda _key: state


# --------------------------------------------------------------------------
# Unit: ordinary (MODE B) evaluation
# --------------------------------------------------------------------------

def test_ordinary_session_routes_through_consumer(consumer_session, monkeypatch):
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    model, kwargs, applied = pre_turn_route(
        runner=runner, source=_telegram(), session_key="key-1",
        user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert applied == "ordinary"
    assert model == "local-model-a"
    assert kwargs["provider"] == "locala"
    assert kwargs["requested_provider"] == "locala"
    assert seen == [("locala", "local-model-a")]


def test_ordinary_passthrough_is_byte_identical_stock(consumer_session, monkeypatch):
    _no_policy(monkeypatch)
    monkeypatch.setenv("FIXTURE_OUTCOME", "passthrough")
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    model, kwargs, applied = pre_turn_route(
        runner=runner, source=_telegram(), session_key="key-2",
        user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert applied == "stock"
    assert model == "stock-model"
    assert kwargs == _STOCK_RUNTIME
    assert seen == []


def test_ordinary_refusal_fails_closed_without_provider_resolution(consumer_session, monkeypatch):
    _no_policy(monkeypatch)
    monkeypatch.setenv("FIXTURE_OUTCOME", "refuse")
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    with pytest.raises(AutoRouterRefusal) as excinfo:
        pre_turn_route(runner=runner, source=_telegram(), session_key="key-3",
                       user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert "no eligible free model" in str(excinfo.value)
    assert seen == []


def test_ordinary_consumer_exception_fails_closed(consumer_session, monkeypatch):
    _no_policy(monkeypatch)
    monkeypatch.setenv("FIXTURE_OUTCOME", "raise")
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key="key-4",
                       user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert seen == []


def test_ordinary_no_consumer_registered_is_byte_identical_stock(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    model, kwargs, applied = pre_turn_route(
        runner=runner, source=_telegram(), session_key="key-5",
        user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert applied == "stock"
    assert (model, kwargs) == ("stock-model", _STOCK_RUNTIME)
    assert seen == []


def test_explicit_physical_pick_is_never_rewritten(consumer_session, monkeypatch):
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    override = {"provider": "openrouter", "model": "paid/model-x"}
    runner = SimpleNamespace(_peek_session_state=_peek_state(override))
    model, kwargs, applied = pre_turn_route(
        runner=runner, source=_telegram(), session_key="key-6",
        user_config={}, model="paid/model-x", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert applied == "explicit"
    assert model == "paid/model-x"
    assert kwargs == _STOCK_RUNTIME
    assert seen == []


def test_explicit_virtual_pick_already_resolved_upstream_is_left_alone(consumer_session, monkeypatch):
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    # What _resolve_session_agent_runtime returns for a virtual override (MODE A):
    physical = {"provider": "localb", "api_key": "sk-dummy-localb", "base_url": "http://127.0.0.1:9/localb",
                "api_mode": "chat_completions", "requested_provider": "autorouter"}
    runner = SimpleNamespace(
        _peek_session_state=_peek_state({"provider": "autorouter", "model": "auto-free"}))
    model, kwargs, applied = pre_turn_route(
        runner=runner, source=_telegram(), session_key="key-7",
        user_config={}, model="local-model-b", runtime_kwargs=dict(physical))
    assert applied == "virtual"
    assert model == "local-model-b"
    assert kwargs == physical
    assert seen == []


# --------------------------------------------------------------------------
# Unit: MODE A virtual pick resolution (this is what the classic
# _resolve_session_agent_runtime branch calls)
# --------------------------------------------------------------------------

def test_virtual_pick_resolves_physical_route(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.autorouter_picker.resolve_autorouter_physical",
        lambda **kw: ({"provider": "locala", "api_key": "sk-a", "base_url": "http://127.0.0.1:8401/v1",
                       "api_mode": "chat_completions", "request_overrides": {}, "capabilities": {}},
                      {"provider": "locala", "model": "local-model-a"}))
    model, kwargs = resolve_virtual_override(
        runner=SimpleNamespace(), source=_telegram(), session_key="sess-8", override=None)
    assert model == "local-model-a"
    assert kwargs["provider"] == "locala"
    # The user-visible selection stays the virtual pick; the physical route is what runs.
    assert kwargs["requested_provider"] == "autorouter"


def test_virtual_pick_refusal_fails_closed(monkeypatch):
    def boom(**_kw):
        raise RuntimeError("AutoRouter selection refused before any provider request: no model is enabled")

    monkeypatch.setattr("hermes_cli.autorouter_picker.resolve_autorouter_physical", boom)
    with pytest.raises(AutoRouterRefusal) as excinfo:
        resolve_virtual_override(
            runner=SimpleNamespace(), source=_telegram(), session_key="sess-9", override=None)
    assert "no model is enabled" in str(excinfo.value)


# --------------------------------------------------------------------------
# Unit: policy (following) sessions
# --------------------------------------------------------------------------

def test_policy_session_with_autorouter_selection_re_resolves(monkeypatch):
    policy = SimpleNamespace(
        effective=lambda authority, secrets=False: (
            {"model": {"selection": {"mode": "autorouter", "model": "auto-free"}}}, "auto-free"))
    monkeypatch.setattr("gateway.session_policy.policy_for_source", lambda runner, source: policy)
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda runner: object())
    monkeypatch.setattr(
        "hermes_cli.autorouter_picker.resolve_autorouter_physical",
        lambda **kw: ({"provider": "localb", "api_key": "sk-b", "base_url": "http://127.0.0.1:8402/v1",
                       "api_mode": "chat_completions", "request_overrides": {}, "capabilities": {}},
                      {"provider": "localb", "model": "local-model-b"}))
    runner, _states = _fake_runner()
    model, kwargs, applied = pre_turn_route(
        runner=runner, source=_telegram(), session_key="sess-10",
        user_config={}, model="frozen-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert applied == "virtual"
    assert model == "local-model-b"
    assert kwargs["provider"] == "localb"


def test_policy_session_with_plain_selection_is_untouched(monkeypatch):
    policy = SimpleNamespace(
        effective=lambda authority, secrets=False: (
            {"model": {"provider": "custom"}}, "frozen-model"))
    monkeypatch.setattr("gateway.session_policy.policy_for_source", lambda runner, source: policy)
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda runner: object())
    runner, _states = _fake_runner()
    model, kwargs, applied = pre_turn_route(
        runner=runner, source=_telegram(), session_key="sess-11",
        user_config={}, model="frozen-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert applied == "explicit"
    assert (model, kwargs) == ("frozen-model", _STOCK_RUNTIME)


# --------------------------------------------------------------------------
# E2E: full turn through TurnRunner.run_sync
# --------------------------------------------------------------------------

_HISTORY = [
    {"role": "user", "content": "my favourite marker is zinnia"},
    {"role": "assistant", "content": "noted: zinnia"},
]


def _e2e_runner():
    runner = _gateway_runner()
    runner._peek_session_state.return_value = None
    runner._resolve_session_agent_runtime.return_value = (
        "stock-model", {"provider": "custom", "api_key": "k1", "base_url": "http://stock.invalid"})
    runner._resolve_turn_agent_config.side_effect = lambda _msg, model, rt, **_kw: {
        "model": model,
        "runtime": {k: v for k, v in rt.items() if k != "request_overrides"},
        "signature": hashlib.sha256(
            f"{model}|{rt.get('provider')}|{rt.get('base_url')}|{rt.get('api_key')}".encode()).hexdigest()[:16],
    }
    runner._agent_config_signature.side_effect = lambda model, runtime, *_a, **_kw: hashlib.sha256(
        f"{model}|{runtime.get('provider')}|{runtime.get('base_url')}|{runtime.get('api_key')}".encode()).hexdigest()[:16]
    runner._apply_fallback_chain_to_agent.return_value = None
    return runner


def _e2e_turn(runner, chat_id, history, admission_id="adm-1"):
    from gateway.run_turn_runner import TurnRunner
    from gateway.session import SessionSource
    from gateway.turn_context import TurnContext

    class _Recording(_RecordingAgent):
        instances = []

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            type(self).instances.append(self)

    _Recording.built = []
    _Recording.instances = []

    ctx = TurnContext(
        source=SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, user_id="u"),
        message="ping", history=list(history), session_id=chat_id, session_key=f"key-{chat_id}",
        user_config={}, AIAgent=_Recording, resolve_display_setting=lambda *_a: False,
        _run_still_current=lambda: True, _hooks_ref=SimpleNamespace(loaded_hooks=False))
    result = TurnRunner(runner, ctx).run_sync()
    return result, _Recording


def test_full_turn_evaluates_route_and_switches_physical_provider_between_turns(
        consumer_session, monkeypatch):
    """Turn 1 routes to locala; the consumer's binding cools it; turn 2 uses localb —
    same session key, context intact, both provider requests provably issued."""
    _no_policy(monkeypatch)
    _stub_runner_kwargs(monkeypatch)
    runner = _e2e_runner()
    monkeypatch.setenv("FIXTURE_OUTCOME", "route")
    first, rec1 = _e2e_turn(runner, "chat-a", _HISTORY)
    assert first["failed"] is not True
    assert rec1.built[-1]["provider"] == "locala"
    assert rec1.built[-1]["model"] == "local-model-a"
    assert rec1.built[-1]["session_id"] == "chat-a"

    monkeypatch.setenv("FIXTURE_OUTCOME", "route_b")
    second, rec2 = _e2e_turn(runner, "chat-a", _HISTORY, admission_id="adm-2")
    assert second["failed"] is not True
    assert rec2.built[-1]["provider"] == "localb"
    assert rec2.built[-1]["model"] == "local-model-b"
    # Context is preserved across the physical switch: each turn re-sees the full history.
    histories = [call[1]["conversation_history"] for rec in (rec1, rec2) for call in rec.instances[-1].calls]
    assert len(histories) == 2
    for passed in histories:
        assert passed[:2] == _HISTORY


def test_full_turn_refusal_constructs_no_agent_and_no_provider_request(
        consumer_session, monkeypatch):
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner = _e2e_runner()
    monkeypatch.setenv("FIXTURE_OUTCOME", "refuse")
    result, rec = _e2e_turn(runner, "chat-b", [])
    assert result["failed"] is True
    assert result["failure_reason"] == "autorouter_refused"
    assert rec.built == []
    assert seen == []
