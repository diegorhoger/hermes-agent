"""MODE A / MODE B isolation at the TUI turn boundary (dispatch 141, P1).

The turn boundary must evaluate the session's OWN routing mode, exactly once
per decision, resolve the physical runtime through Hermes' canonical resolver,
and never accept a physical route that differs from the validated decision.

These tests drive the real ``tui_gateway.autorouter_turn.turn_boundary`` with
a real registered consumer plugin (scratch HERMES_HOME) and a synthetic agent
that records in-place route commits. The consumer appends one line per
invocation to ``CONSUMER_LOG`` so the tests can prove the consumer runs
exactly once per routing decision — never twice, never zero.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tui_gateway.autorouter_turn import turn_boundary

# One consumer, two gates:
#   * the MANUAL (MODE A, selection_mode=manual, explicit_model=True) outcome;
#   * the AUTOMATIC (MODE B, explicit_model=False) outcome.
# FIXTURE_MODE_A / FIXTURE_MODE_B select each independently, which lets the
# matrix cover MODE A only, MODE B only, both, and neither.
_CONSUMER_PLUGIN = """
import json
import os

def _outcome(mode):
    return {
        'route': {'status': 'route', 'provider': 'locala', 'model': 'local-model-a',
                  'reason': 'fixture route a'},
        'route_b': {'status': 'route', 'provider': 'localb', 'model': 'local-model-b',
                    'reason': 'fixture route b'},
        'refuse': {'status': 'refuse', 'reason': 'fixture refusal: no eligible free model'},
        'passthrough': {'status': 'passthrough', 'reason': 'fixture opt-out'},
        'malformed': {'status': 'route', 'provider': '', 'model': '', 'reason': 'nothing usable'},
        'providermismatch': {'status': 'route', 'provider': 'openrouter', 'model': 'paid/model-x',
                             'reason': 'cross-provider route'},
    }[os.environ[mode]]

def register(ctx):
    def consume(**kwargs):
        with open(os.environ['CONSUMER_LOG'], 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({k: kwargs.get(k) for k in sorted(kwargs)}) + '\\n')
        mode = 'FIXTURE_MODE_A' if kwargs.get('selection_mode') == 'manual' else 'FIXTURE_MODE_B'
        outcome = _outcome(mode)
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
def consumer_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    _write_plugin(home, "autorouter_fixture", _CONSUMER_PLUGIN)
    (home / "config.yaml").write_text("plugins:\n  enabled: [autorouter_fixture]\n")
    log = tmp_path / "consumer.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CONSUMER_LOG", str(log))
    monkeypatch.setenv("FIXTURE_MODE_A", "route")
    monkeypatch.setenv("FIXTURE_MODE_B", "route")
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    return home, log


def _unload_consumer(monkeypatch, tmp_path):
    """Simulate the consumer disappearing mid-session: a scratch home with no
    plugins, and a fresh manager that discovers nothing."""
    empty = tmp_path / "empty-home"
    empty.mkdir()
    (empty / "config.yaml").write_text("plugins:\n  enabled: []\n")
    monkeypatch.setenv("HERMES_HOME", str(empty))
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()  # fresh manager; discovery scans the empty home
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    return manager


def _agent(*, routed=True, virtual=False, provider="locala", model="local-model-a",
           base_url="http://127.0.0.1:9/locala", api_key="sk-canonical-not-the-consumers",
           api_mode="chat_completions"):
    calls = []

    class _Agent:
        pass

    agent = _Agent()
    agent._autorouter_routed = routed
    agent._autorouter_virtual = virtual
    agent.provider = provider
    agent.model = model
    agent.api_mode = api_mode
    # A real managed agent carries its full physical runtime; the turn
    # boundary revalidates it against the canonical resolver.
    agent.base_url = base_url
    agent.api_key = api_key

    def _switch_model(new_model, new_provider, api_key="", base_url="", api_mode="", capabilities=None):
        calls.append({"new_model": new_model, "new_provider": new_provider,
                      "api_key": api_key, "base_url": base_url, "api_mode": api_mode})
        agent.model = new_model
        agent.provider = new_provider
        # The real in-place switch rebuilds the client from the supplied runtime.
        if base_url:
            agent.base_url = base_url
        if api_key:
            agent.api_key = api_key
        if api_mode:
            agent.api_mode = api_mode

    agent.switch_model = _switch_model
    agent._switch_calls = calls
    return agent


def _session(key="sess-1"):
    return {"session_key": key, "source": None}


def _consumer_calls(log):
    lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _stub_canonical_resolver(monkeypatch, provider="locala", requested="locala",
                             model="local-model-a", api_key="sk-canonical-not-the-consumers"):
    seen = []

    def fake_resolve(*, requested=None, target_model=None, **_kwargs):
        seen.append({"requested": requested, "target_model": target_model})
        return {
            "provider": provider,
            "requested_provider": requested,
            "model": model,
            "api_key": api_key,
            "base_url": f"http://127.0.0.1:9/{requested}",
            "api_mode": "chat_completions",
            "request_overrides": {},
            "capabilities": {},
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fake_resolve)
    return seen


# ---------------------------------------------------------------------------
# Mode selection: each mode is evaluated exactly once, with its own semantics
# ---------------------------------------------------------------------------

def test_mode_a_evaluates_as_manual_selection(consumer_home, monkeypatch):
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_A", "route_b")
    _stub_canonical_resolver(monkeypatch, provider="localb", requested="localb", model="local-model-b")
    agent = _agent(virtual=True, provider="locala", model="local-model-a")
    changed = turn_boundary("sess-1", _session(), agent)
    assert changed is True
    calls = _consumer_calls(log)
    assert len(calls) == 1
    assert calls[0]["explicit_model"] is True
    assert calls[0]["selection_mode"] == "manual"
    assert calls[0]["requested_provider"] == "autorouter"
    assert calls[0]["requested_model"] == "auto-free"


def test_mode_b_evaluates_as_ordinary_automatic_routing(consumer_home, monkeypatch):
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "route_b")
    _stub_canonical_resolver(monkeypatch, provider="localb", requested="localb", model="local-model-b")
    agent = _agent(virtual=False, provider="locala", model="local-model-a")
    changed = turn_boundary("sess-1", _session(), agent)
    assert changed is True
    calls = _consumer_calls(log)
    assert len(calls) == 1
    assert calls[0]["explicit_model"] is False
    assert "selection_mode" not in calls[0] or calls[0]["selection_mode"] is None
    assert calls[0]["requested_provider"] == "locala"
    assert calls[0]["requested_model"] == "local-model-a"


def test_mode_a_only_matrix(consumer_home, monkeypatch):
    """MODE A enabled / MODE B disabled: virtual pick routes; the ordinary
    managed session is refused. The modes do not cross-contaminate."""
    _home, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_A", "route_b")
    monkeypatch.setenv("FIXTURE_MODE_B", "refuse")
    _stub_canonical_resolver(monkeypatch, provider="localb", requested="localb", model="local-model-b")
    virtual_agent = _agent(virtual=True, provider="locala", model="local-model-a")
    assert turn_boundary("sess-a", _session("sess-a"), virtual_agent) is True
    ordinary_agent = _agent(virtual=False, provider="locala", model="local-model-a")
    with pytest.raises(RuntimeError):
        turn_boundary("sess-b", _session("sess-b"), ordinary_agent)
    assert len(_consumer_calls(log)) == 2  # exactly one evaluation per decision


def test_mode_b_only_matrix(consumer_home, monkeypatch):
    """MODE A disabled / MODE B enabled: ordinary session routes; the virtual
    pick is refused. The modes do not cross-contaminate."""
    _home, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_A", "refuse")
    monkeypatch.setenv("FIXTURE_MODE_B", "route_b")
    _stub_canonical_resolver(monkeypatch, provider="localb", requested="localb", model="local-model-b")
    ordinary_agent = _agent(virtual=False, provider="locala", model="local-model-a")
    assert turn_boundary("sess-b", _session("sess-b"), ordinary_agent) is True
    virtual_agent = _agent(virtual=True, provider="locala", model="local-model-a")
    with pytest.raises(RuntimeError):
        turn_boundary("sess-a", _session("sess-a"), virtual_agent)
    assert len(_consumer_calls(log)) == 2


def test_both_modes_matrix(consumer_home, monkeypatch):
    """Both enabled: each mode evaluates with its own semantics, once each."""
    _home, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_A", "route_b")
    monkeypatch.setenv("FIXTURE_MODE_B", "route_b")
    _stub_canonical_resolver(monkeypatch, provider="localb", requested="localb", model="local-model-b")
    virtual_agent = _agent(virtual=True, provider="locala", model="local-model-a")
    ordinary_agent = _agent(virtual=False, provider="locala", model="local-model-a")
    assert turn_boundary("sess-a", _session("sess-a"), virtual_agent) is True
    assert turn_boundary("sess-b", _session("sess-b"), ordinary_agent) is True
    calls = _consumer_calls(log)
    assert len(calls) == 2
    manual = [c for c in calls if c.get("selection_mode") == "manual"]
    automatic = [c for c in calls if c.get("explicit_model") is False]
    assert len(manual) == 1 and len(automatic) == 1


def test_both_disabled_matrix(consumer_home, monkeypatch):
    """Both disabled: managed sessions of either mode fail closed."""
    _home, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_A", "refuse")
    monkeypatch.setenv("FIXTURE_MODE_B", "refuse")
    _stub_canonical_resolver(monkeypatch)
    for virtual in (True, False):
        agent = _agent(virtual=virtual, provider="locala", model="local-model-a")
        with pytest.raises(RuntimeError):
            turn_boundary("sess-x", _session("sess-x"), agent)
    assert len(_consumer_calls(log)) == 2


# ---------------------------------------------------------------------------
# Route outcomes: unchanged, changed, refusal, malformed, passthrough
# ---------------------------------------------------------------------------

def test_physical_model_unchanged_keeps_route_without_swapping(consumer_home, monkeypatch):
    """Same decision, healthy equivalent runtime: the client is kept — no churn,
    no second consumer call — but the physical runtime IS revalidated against
    the canonical resolver before the turn proceeds."""
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_A", "route")  # resolves to locala/local-model-a
    seen = _stub_canonical_resolver(monkeypatch)
    agent = _agent(virtual=True, provider="locala", model="local-model-a")
    assert turn_boundary("sess-1", _session(), agent) is False
    assert agent._switch_calls == []
    # The unchanged route is revalidated, not re-built and not trusted blindly.
    assert seen == [{"requested": "locala", "target_model": "local-model-a"}]
    assert len(_consumer_calls(log)) == 1


def test_physical_model_change_commits_new_route(consumer_home, monkeypatch):
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "route_b")  # route => localb/local-model-b
    _stub_canonical_resolver(monkeypatch, provider="localb", requested="localb", model="local-model-b")
    agent = _agent(virtual=False, provider="locala", model="local-model-a")
    assert turn_boundary("sess-1", _session(), agent) is True
    assert agent._switch_calls[-1]["new_provider"] == "localb"
    assert agent._switch_calls[-1]["new_model"] == "local-model-b"
    # Credentials come from the canonical resolver, never from the consumer payload.
    assert agent._switch_calls[-1]["api_key"] == "sk-canonical-not-the-consumers"
    assert len(_consumer_calls(log)) == 1


def test_consumer_refusal_fails_closed_without_resolving_or_swapping(consumer_home, monkeypatch):
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "refuse")
    seen = _stub_canonical_resolver(monkeypatch)
    agent = _agent(virtual=False, provider="locala", model="local-model-a")
    with pytest.raises(RuntimeError):
        turn_boundary("sess-1", _session(), agent)
    assert agent._switch_calls == []
    assert seen == []
    assert len(_consumer_calls(log)) == 1


def test_consumer_malformed_route_fails_closed(consumer_home, monkeypatch):
    _, _log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "malformed")
    seen = _stub_canonical_resolver(monkeypatch)
    agent = _agent(virtual=False, provider="locala", model="local-model-a")
    with pytest.raises(RuntimeError):
        turn_boundary("sess-1", _session(), agent)
    assert agent._switch_calls == []
    assert seen == []


def test_consumer_returns_route_provider_unresolvable_by_canonical_resolver(consumer_home, monkeypatch):
    """The resolver lands somewhere other than the validated decision: refused."""
    _, _log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "providermismatch")  # decision: openrouter/paid/model-x
    # The canonical resolver lands somewhere other than the validated decision:
    # refused, in place, before any swap.
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {"provider": "nous", "requested_provider": "nous",
                           "model": "paid/model-x", "api_key": "sk-nous",
                           "base_url": "http://127.0.0.1:9/nous", "api_mode": "chat_completions"})
    agent = _agent(virtual=False, provider="locala", model="local-model-a")
    with pytest.raises(RuntimeError):
        turn_boundary("sess-1", _session(), agent)
    assert agent._switch_calls == []


def test_consumer_passthrough_fails_closed_for_managed_session(consumer_home, monkeypatch):
    """A managed session has no opt-out: passthrough must not keep the pinned
    route or widen to stock."""
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "passthrough")
    _stub_canonical_resolver(monkeypatch)
    agent = _agent(virtual=False, provider="locala", model="local-model-a")
    with pytest.raises(RuntimeError):
        turn_boundary("sess-1", _session(), agent)
    assert agent._switch_calls == []
    assert len(_consumer_calls(log)) == 1


def test_mode_a_passthrough_fails_closed(consumer_home, monkeypatch):
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_A", "passthrough")
    _stub_canonical_resolver(monkeypatch)
    agent = _agent(virtual=True, provider="locala", model="local-model-a")
    with pytest.raises(RuntimeError):
        turn_boundary("sess-1", _session(), agent)
    assert len(_consumer_calls(log)) == 1


def test_consumer_unloaded_fails_closed(consumer_home, monkeypatch, tmp_path):
    """The consumer disappears mid-session (plugin disabled/revoked): refuse,
    never widen."""
    _unload_consumer(monkeypatch, tmp_path)
    agent = _agent(virtual=False, provider="locala", model="local-model-a")
    with pytest.raises(RuntimeError):
        turn_boundary("sess-1", _session(), agent)
    assert agent._switch_calls == []


def test_explicit_physical_pick_is_never_rewritten(consumer_home, monkeypatch):
    """An explicit fixed physical pick (no managed markers) is authoritative:
    the consumer does not run at all."""
    _, log = consumer_home
    _stub_canonical_resolver(monkeypatch)
    agent = _agent(routed=False, virtual=False, provider="openrouter", model="paid/model-x")
    assert turn_boundary("sess-1", _session(), agent) is False
    assert agent._switch_calls == []
    assert _consumer_calls(log) == []


def test_consumer_invocation_count_is_one_per_decision(consumer_home, monkeypatch):
    _, log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "route_b")
    _stub_canonical_resolver(monkeypatch, provider="localb", requested="localb", model="local-model-b")
    # Two turns in a row on the same session: one evaluation per boundary.
    agent = _agent(virtual=False, provider="locala", model="local-model-a")
    assert turn_boundary("sess-1", _session(), agent) is True
    assert turn_boundary("sess-1", _session(), agent) is False  # now unchanged
    assert len(_consumer_calls(log)) == 2


def test_profile_failure_refuses_without_invoking_consumer(consumer_home, monkeypatch):
    _, log = consumer_home
    monkeypatch.setattr("hermes_cli.profiles.current_profile_name",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("profile scope lost")))
    _stub_canonical_resolver(monkeypatch)
    agent = _agent(virtual=False, provider="locala", model="local-model-a")
    with pytest.raises(RuntimeError):
        turn_boundary("sess-1", _session(), agent)
    assert _consumer_calls(log) == []
    assert agent._switch_calls == []


def test_switch_failure_fails_closed(consumer_home, monkeypatch):
    _, _log = consumer_home
    monkeypatch.setenv("FIXTURE_MODE_B", "route_b")
    _stub_canonical_resolver(monkeypatch, provider="localb", requested="localb", model="local-model-b")
    agent = _agent(virtual=False, provider="locala", model="local-model-a")

    def boom(**_kwargs):
        raise ValueError("swap rolled back")

    agent.switch_model = boom
    with pytest.raises(RuntimeError):
        turn_boundary("sess-1", _session(), agent)


def test_build_seam_refuses_before_invoking_consumer_when_profile_lost(consumer_home, monkeypatch):
    """Agent build with an unresolvable bound profile fails closed before the
    consumer is even asked — never falls back to the launch profile."""
    from tui_gateway import agent_factory

    monkeypatch.setattr("hermes_cli.profiles.current_profile_name",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("scope lost")))
    called = []
    monkeypatch.setattr("hermes_cli.middleware.apply_pre_inference_route_middleware",
                        lambda *a, **k: called.append(1))
    with pytest.raises(RuntimeError):
        agent_factory._pre_inference_route_override(
            "sid", "sid", "key", None, None, {"model": {}}, None)
    assert called == []


def test_virtual_pick_resolution_refuses_before_consumer_when_profile_lost(monkeypatch):
    """MODE A resolution with an unresolvable bound profile fails closed before
    the consumer is asked — never substitutes the launch profile."""
    monkeypatch.setattr("hermes_cli.profiles.current_profile_name",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("scope lost")))
    called = []
    monkeypatch.setattr("hermes_cli.middleware.apply_pre_inference_route_middleware",
                        lambda *a, **k: called.append(1))
    import pytest

    with pytest.raises(RuntimeError):
        from hermes_cli.autorouter_picker import resolve_autorouter_physical

        resolve_autorouter_physical(session_id="sess", platform="tui")
    assert called == []
