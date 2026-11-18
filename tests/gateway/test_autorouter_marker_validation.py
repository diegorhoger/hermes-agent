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
    _clear_managed_marker,
    _persist_managed_marker,
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
    with pytest.raises(AutoRouterRefusal, match="marker is corrupt"):
        _read_managed_marker(runner, "key")


def test_valid_in_memory_does_not_conceal_corrupt_persistent_state():
    """A valid in-memory marker never masks a corrupted authoritative store."""
    runner = _runner_with_markers({"mode": "virtual", "since": 1.0}, False)
    with pytest.raises(AutoRouterRefusal, match="marker is corrupt"):
        _read_managed_marker(runner, "key")


def test_corrupt_in_memory_does_not_conceal_valid_persistent_state():
    runner = _runner_with_markers(0, {"mode": "ordinary", "since": 1.0})
    with pytest.raises(AutoRouterRefusal, match="marker is corrupt"):
        _read_managed_marker(runner, "key")


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


def test_corrupt_store_marker_refuses_even_when_consumer_offers_fresh_route(consumer_session, monkeypatch):
    """A fresh candidate cannot overwrite corrupt managed authority and reach inference."""
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    runner, _states = _fake_runner()
    runner.session_store.set_session_metadata("key-fresh", MANAGED_METADATA_KEY, False)
    with pytest.raises(AutoRouterRefusal, match="marker is corrupt"):
        pre_turn_route(
            runner=runner, source=_telegram(), session_key="key-fresh",
            user_config={}, model="stock-model", runtime_kwargs=dict(_STOCK_RUNTIME))
    assert seen == []


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


def test_corrupt_store_marker_full_turn_with_eligible_consumer_issues_zero_requests(consumer_session, monkeypatch):
    """Even when the consumer offers a good route, corrupt authority stops execution."""
    _no_policy(monkeypatch)
    seen = _stub_runner_kwargs(monkeypatch)
    from tests.gateway.test_autorouter_turn import _e2e_runner, _e2e_turn, _HISTORY

    runner = _e2e_runner()
    runner.session_store.get_session_metadata = lambda key, k, default=None: (
        False if k == MANAGED_METADATA_KEY else default)
    result, rec = _e2e_turn(runner, "chat-corrupt-eligible", _HISTORY)
    assert result["failed"] is True
    assert result["failure_reason"] == "autorouter_refused"
    assert rec.built == []
    assert seen == []


# ---------------------------------------------------------------------------
# Unit: conflicting in-memory / persistent markers refuse until reconciled
# ---------------------------------------------------------------------------

def test_conflicting_modes_refuse_closed():
    """Two individually valid markers that disagree on mode are irreconcilable:
    the turn must refuse before any provider request, in both directions."""
    for in_memory, stored in (({"mode": "ordinary", "since": 1.0}, {"mode": "virtual", "since": 1.0}),
                              ({"mode": "virtual", "since": 1.0}, {"mode": "ordinary", "since": 1.0})):
        runner = _runner_with_markers(in_memory, stored)
        with pytest.raises(AutoRouterRefusal):
            _read_managed_marker(runner, "key")


def test_conflict_reconciled_by_authorized_persist():
    """A fresh authorized persist (explicit opt-in) writes both copies
    consistently; the reconciled session then resolves normally."""
    runner, states = _fake_runner()
    store = runner.session_store
    store.set_session_metadata("key", MANAGED_METADATA_KEY, {"mode": "virtual", "since": 1.0})
    states.setdefault("key", SimpleNamespace(
        conversation=SimpleNamespace(model_override=None, autorouter_managed={"mode": "ordinary", "since": 1.0})))
    with pytest.raises(AutoRouterRefusal):
        _read_managed_marker(runner, "key")
    # Authorized reconciliation: an explicit opt-in re-persists a consistent pair.
    assert _persist_managed_marker(runner, "key", "virtual") is True
    marker = _read_managed_marker(runner, "key")
    assert marker is not None and marker["mode"] == "virtual" and "malformed" not in marker


def test_conflict_reconciled_by_authorized_clear():
    """A genuine explicit physical pick clears both copies; the reconciled
    session is never-managed (stock) again."""
    runner, states = _fake_runner()
    store = runner.session_store
    store.set_session_metadata("key", MANAGED_METADATA_KEY, {"mode": "virtual", "since": 1.0})
    states.setdefault("key", SimpleNamespace(
        conversation=SimpleNamespace(model_override=None, autorouter_managed={"mode": "ordinary", "since": 1.0})))
    _clear_managed_marker(runner, "key")
    assert _read_managed_marker(runner, "key") is None


def test_conflict_refuses_full_turn_with_zero_provider_requests(consumer_session, monkeypatch):
    """End-to-end through the messaging turn runner: conflicting markers fail
    the turn closed with zero agents built."""
    _no_policy(monkeypatch)
    _stub_runner_kwargs(monkeypatch)
    from tests.gateway.test_autorouter_turn import _e2e_runner, _e2e_turn, _HISTORY

    runner = _e2e_runner()
    runner.session_store.get_session_metadata = lambda key, k, default=None: (
        {"mode": "virtual", "since": 1.0} if k == MANAGED_METADATA_KEY else default)

    # In-memory copy disagrees with the persisted copy.
    class _State:
        conversation = SimpleNamespace(model_override=None,
                                       autorouter_managed={"mode": "ordinary", "since": 1.0})

    runner._peek_session_state = lambda key: _State()
    result, rec = _e2e_turn(runner, "chat-conflict", _HISTORY)
    assert result["failed"] is True
    assert result["failure_reason"] == "autorouter_refused"
    assert rec.built == []
