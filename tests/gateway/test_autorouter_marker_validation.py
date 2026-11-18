"""Corrupt managed-session markers never degrade to never-managed (dispatch 142, P1).

The managed marker in the persistent session store is the authoritative record
that a session was adopted by AutoRouter. A present-but-malformed value must be
managed evidence — fail closed — never read as absence (never-managed keeps
stock semantics, which would launder revoked or unknown routing state).

Only a genuinely absent marker (key never written, in both the in-memory
conversation scope and the durable store) is never-managed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.autorouter_turn import (
    AutoRouterRefusal,
    MANAGED_METADATA_KEY,
    _read_managed_marker,
    _validate_managed_marker,
    pre_turn_route,
)

from tests.gateway.test_autorouter_managed_state import (
    consumer_session,
    _fake_runner,
)
from tests.gateway.test_autorouter_turn import (
    _STOCK_RUNTIME,
    _no_policy,
    _stub_runner_kwargs,
    _telegram,
)

ABSENT = None
CORRUPT_VALUES = [
    True,
    False,
    0,
    1,
    3.5,
    "",
    "   ",
    "corrupt!!",
    "autorouter_managed",
    [],
    {},
    {"mode": "bogus"},
    {"mode": 3},
    {"mode": None},
    {"since": "not-a-mode"},
]
VALID_MARKERS = [
    {"mode": "ordinary"},
    {"mode": "virtual"},
    {"mode": "ordinary", "since": 1794951250.0},
    {"mode": "VIRTUAL"},  # case-insensitive normalization
]


# ---------------------------------------------------------------------------
# Unit: marker normalization
# ---------------------------------------------------------------------------

def test_absent_marker_is_absent():
    assert _validate_managed_marker(ABSENT) is None


@pytest.mark.parametrize("value", CORRUPT_VALUES)
def test_present_but_invalid_marker_is_managed_not_absent(value):
    marker = _validate_managed_marker(value)
    assert marker is not None
    assert marker.get("malformed") is True
    assert marker["mode"] in ("ordinary", "virtual")


@pytest.mark.parametrize("value", VALID_MARKERS)
def test_valid_marker_is_not_malformed(value):
    marker = _validate_managed_marker(value)
    assert marker is not None
    assert "malformed" not in marker
    assert marker["mode"] in ("ordinary", "virtual")


# ---------------------------------------------------------------------------
# Unit: merge of the in-memory and persistent copies
# ---------------------------------------------------------------------------

def _runner_with_markers(in_memory, stored, *, peek_error=None, store_error=None):
    runner, states = _fake_runner(peek_error=peek_error, store_error=store_error)
    if stored is not _UNSET:
        runner.session_store.set_session_metadata("key", MANAGED_METADATA_KEY, stored)
    if in_memory is not _UNSET:
        states.setdefault(
            "key",
            SimpleNamespace(conversation=SimpleNamespace(model_override=None, autorouter_managed=in_memory)),
        ).conversation.autorouter_managed = in_memory
    return runner


_UNSET = object()


def test_both_copies_absent_is_never_managed():
    runner = _runner_with_markers(_UNSET, _UNSET)
    assert _read_managed_marker(runner, "key") is None


def test_stored_corrupt_in_memory_absent_refuses_closed():
    runner = _runner_with_markers(_UNSET, 1)
    marker = _read_managed_marker(runner, "key")
    assert marker is not None and marker.get("malformed") is True


def test_valid_in_memory_does_not_conceal_corrupt_persistent_state():
    """A valid in-memory marker never masks a corrupted authoritative store."""
    runner = _runner_with_markers({"mode": "virtual", "since": 1.0}, False)
    marker = _read_managed_marker(runner, "key")
    assert marker is not None and marker.get("malformed") is True


def test_corrupt_in_memory_does_not_conceal_valid_persistent_state():
    runner = _runner_with_markers(0, {"mode": "ordinary", "since": 1.0})
    marker = _read_managed_marker(runner, "key")
    assert marker is not None and marker.get("malformed") is True


def test_both_valid_marker_is_valid():
    runner = _runner_with_markers({"mode": "ordinary", "since": 1.0},
                                  {"mode": "ordinary", "since": 1.0})
    marker = _read_managed_marker(runner, "key")
    assert marker is not None and "malformed" not in marker


def test_unreadable_persistent_marker_refuses():
    runner = _runner_with_markers(_UNSET, _UNSET, store_error=RuntimeError("metadata table gone"))
    with pytest.raises(AutoRouterRefusal):
        _read_managed_marker(runner, "key")


def test_unreadable_session_state_refuses():
    runner = _runner_with_markers(_UNSET, _UNSET, peek_error=RuntimeError("state db locked"))
    with pytest.raises(AutoRouterRefusal):
        _read_managed_marker(runner, "key")


# ---------------------------------------------------------------------------
# Behaviour: corrupt persisted markers never yield stock routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", CORRUPT_VALUES)
def test_corrupt_store_marker_never_managed_fallthrough(consumer_session, monkeypatch, value):
    """With the consumer opting out (passthrough), every corrupt marker refuses —
    never widens to stock — and no provider resolution is issued."""
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    monkeypatch.setenv("FIXTURE_OUTCOME", "passthrough")
    runner, _states = _fake_runner()
    runner.session_store.set_session_metadata("key-corrupt", MANAGED_METADATA_KEY, value)
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key="key-corrupt",
                       user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert seen == []


@pytest.mark.parametrize("value", CORRUPT_VALUES)
def test_corrupt_in_memory_marker_never_managed_fallthrough(consumer_session, monkeypatch, value):
    """Corrupt in-memory marker with a disappeared consumer refuses — never stock."""
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, states = _fake_runner()
    states.setdefault("key-corrupt", SimpleNamespace(conversation=SimpleNamespace(
        model_override=None, autorouter_managed=value)))
    states["key-corrupt"].conversation.autorouter_managed = value
    monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda _kind: False)
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key="key-corrupt",
                       user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert seen == []


def test_corrupt_store_marker_with_consumer_recovery_routes_fresh_not_stock(consumer_session, monkeypatch):
    """141 semantics preserved for ALL corrupt values: a corrupt marker is managed
    evidence; a fresh authorized consumer route is applied (never stock, never the
    last-pinned route)."""
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    runner.session_store.set_session_metadata("key-fresh", MANAGED_METADATA_KEY, False)
    model, _kwargs, applied = pre_turn_route(
        runner=runner, source=_telegram(), session_key="key-fresh",
        user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert applied == "ordinary"
    assert model == "local-model-a"
    assert seen == [("locala", "local-model-a")]


def test_valid_in_memory_corrupt_store_passthrough_refuses(consumer_session, monkeypatch):
    """Valid in-memory copy + corrupted authoritative store + consumer opt-out:
    the turn refuses; the corrupt store is not concealed as never-managed."""
    _no_policy(monkeypatch)
    _stub_runner_kwargs(monkeypatch)
    monkeypatch.setenv("FIXTURE_OUTCOME", "passthrough")
    runner, states = _fake_runner()
    states.setdefault("key-conceal", SimpleNamespace(conversation=SimpleNamespace(
        model_override=None, autorouter_managed={"mode": "ordinary", "since": 1.0})))
    runner.session_store.set_session_metadata("key-conceal", MANAGED_METADATA_KEY, 7)
    with pytest.raises(AutoRouterRefusal):
        pre_turn_route(runner=runner, source=_telegram(), session_key="key-conceal",
                       user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))


def test_corrupt_store_marker_full_turn_issues_zero_provider_requests(consumer_session, monkeypatch):
    """End-to-end through the messaging turn runner: a corrupt managed marker
    with no authorized route fails the turn closed with zero agents built."""
    _no_policy(monkeypatch)
    _stub_runner_kwargs(monkeypatch)
    from tests.gateway.test_autorouter_turn import _e2e_runner, _e2e_turn, _HISTORY

    runner = _e2e_runner()
    runner.session_store.get_session_metadata = lambda key, k, default=None: (
        7 if k == MANAGED_METADATA_KEY else default)
    monkeypatch.setenv("FIXTURE_OUTCOME", "passthrough")
    result, rec = _e2e_turn(runner, "chat-corrupt", _HISTORY)
    assert result["failed"] is True
    assert result["failure_reason"] == "autorouter_refused"
    assert rec.built == []
