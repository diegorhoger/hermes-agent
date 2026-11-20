"""Dispatch 160 local synthetic credential/quota + plugin lifecycle acceptance.

These scenarios exercise the EXACT pinned AutoRouter plugin source that the
private-RC Desktop package installs (SHA256-verified archive from the e2e
fixtures), loaded from an isolated extraction under a disposable temp dir.
No real provider, credential, network call, or live Hermes installation is
touched. Released-host / live-account criteria remain explicitly out of scope
of this local synthetic acceptance.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import pathlib
import sys
import tarfile
import time
import types

import pytest

ARCHIVE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "apps" / "desktop" / "e2e" / "support" / "fixtures"
    / "autorouter-pinned-cdaef496501eadccdde1ac250ef1a18d5b1fd402.tar.gz"
)
PKG = "_autorouter_rc160"


@pytest.fixture(scope="module")
def pinned(tmp_path_factory):
    extract = tmp_path_factory.mktemp("autorouter-rc160")
    with tarfile.open(ARCHIVE, "r:gz") as tf:
        tf.extractall(extract, filter="data")  # rejects absolute/../ / symlink escapes
    root = pathlib.Path(extract)

    spec = importlib.util.spec_from_file_location(
        PKG, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    pkg = importlib.util.module_from_spec(spec)
    pkg.__package__ = PKG
    sys.modules[PKG] = pkg
    spec.loader.exec_module(pkg)

    def mod(name):
        return importlib.import_module(f"{PKG}.autorouter_core.{name}")

    api = types.SimpleNamespace(
        root=root,
        fa=mod("evidence.free_access"),
        quota=mod("evidence.quota"),
        scope=mod("evidence.credential_scope"),
        scope_quota=mod("evidence.scope_quota"),
        elig=mod("eligibility.eligibility"),
        native=mod("integration.native_routing"),
        connections=mod("providers.connections"),
        state=mod("storage.state"),
    )
    # The disposable mock provider is a host-recognized provider inside the
    # packaged app; stand in for that host discovery in this isolated import.
    api.connections._known_ids_once = lambda: {"mockinfra", "nvidia", "openrouter"}
    return api


def _scope(api, account_id, *, identity="known", provider="openrouter", legacy=None):
    if legacy is not None:
        return api.scope.observed_scope(provider, legacy)
    return api.scope.observed_scope(provider, {
        "provider_id": provider,
        "credential_scope_id": account_id,
        "identity_status": identity,
    })


@pytest.fixture
def seeded_store(pinned, tmp_path):
    """Use the same seeding code as the packaged E2E harness (known-good)."""
    prep_spec = importlib.util.spec_from_file_location(
        "prep_rc160",
        pathlib.Path(__file__).resolve().parents[2]
        / "apps" / "desktop" / "e2e" / "support" / "prepare_autorouter_home.py",
    )
    prep = importlib.util.module_from_spec(prep_spec)
    prep_spec.loader.exec_module(prep)
    home = tmp_path / "home"
    home.mkdir()
    info = prep.seed_autorouter_store(home, pinned.root)
    return pathlib.Path(info["store_dir"])


SETTINGS = {
    "enabled": True,
    "enable_chat_router": True,
    "enable_native_routing": True,
    "providers": ["mockinfra"],
    "free_only": True,
    "policy": "auto",
    "min_context": 8192,
    "requires_tools": True,
    "model_preference": {
        "mode": "allowlist",
        "include": ["mockinfra/mockinfra-free-model"],
        "exclude": [],
    },
}
META = {
    "session_id": "s-160-credential",
    "platform": "gateway",
    "requested_model": "baseline-model",
    "requested_provider": "mockinfra",
}


def _write_allowance(api, store_dir, scope, *, remaining, state="healthy", observed=None):
    canonical = api.fa.canonical_scope(scope)
    observed = observed if observed is not None else time.time()
    evidence = api.fa.normalize_openrouter_quota(
        {"data": {"free_model_daily_requests": {
            "limit": 50, "remaining": remaining, "used": 50 - remaining,
        }}},
        canonical, observed,
    )
    snap = api.fa.FreeAccessSnapshot(
        "openrouter", canonical, quota_evidence=evidence, state=state,
        next_refresh_at=observed + 60,
    ).to_dict()
    api.fa._save(api.state.StateStore(store_dir), snap)
    return canonical


# --- credential / quota scope (issue #41, local synthetic) -----------------

def test_account_a_and_b_have_distinct_scopes(pinned):
    a = _scope(pinned, "a" * 32)
    b = _scope(pinned, "b" * 32)
    assert a and b and a != b
    assert pinned.fa.canonical_scope(a) != pinned.fa.canonical_scope(b)


def test_fresh_allowance_authorizes_under_strict_policy(pinned, tmp_path):
    store_dir = tmp_path / "store"; store_dir.mkdir()
    a = _scope(pinned, "a" * 32)
    _write_allowance(pinned, store_dir, a, remaining=50)
    snap = pinned.fa.read_snapshot(pinned.state.StateStore(store_dir), "openrouter", a)
    result = pinned.fa.evaluate_policy(
        snap, "openrouter", "mockinfra-free-model", a, time.time(),
        quota_policy="require_fresh_account_allowance",
    )
    assert result["allowed"] is True
    assert result["state"] == "healthy"


def test_exhausted_allowance_is_refused(pinned, tmp_path):
    store_dir = tmp_path / "store"; store_dir.mkdir()
    a = _scope(pinned, "a" * 32)
    _write_allowance(pinned, store_dir, a, remaining=0, state="exhausted")
    result = pinned.fa.evaluate_policy(
        pinned.fa.read_snapshot(pinned.state.StateStore(store_dir), "openrouter", a),
        "openrouter", "mockinfra-free-model", a, time.time(),
        quota_policy="require_fresh_account_allowance",
    )
    assert result["allowed"] is False
    assert result["state"] == "exhausted"


def test_missing_allowance_is_unknown_and_refused(pinned, tmp_path):
    store_dir = tmp_path / "store"; store_dir.mkdir()
    a = _scope(pinned, "a" * 32)
    snap = pinned.fa.read_snapshot(pinned.state.StateStore(store_dir), "openrouter", a)
    assert snap["quota_evidence"] == []
    result = pinned.fa.evaluate_policy(
        snap, "openrouter", "mockinfra-free-model", a, time.time(),
        quota_policy="require_fresh_account_allowance",
    )
    assert result["allowed"] is False
    assert result["state"] == "unknown"


def test_stale_allowance_is_unknown_and_refused(pinned, tmp_path):
    store_dir = tmp_path / "store"; store_dir.mkdir()
    a = _scope(pinned, "a" * 32)
    stale = time.time() - pinned.fa.TTL * 4
    _write_allowance(pinned, store_dir, a, remaining=50, observed=stale)
    snap = pinned.fa.read_snapshot(pinned.state.StateStore(store_dir), "openrouter", a)
    result = pinned.fa.evaluate_policy(
        snap, "openrouter", "mockinfra-free-model", a, time.time(),
        quota_policy="require_fresh_account_allowance",
    )
    assert result["allowed"] is False
    assert result["state"] in ("unknown", "unavailable")


def test_revoked_or_unknown_scope_cannot_establish_authority(pinned):
    assert _scope(pinned, "a" * 32, identity="revoked") == ""
    assert _scope(pinned, "a" * 32, identity="unknown") == ""
    # Provider-bound: metadata naming a different provider cannot establish scope.
    assert pinned.scope.observed_scope("openrouter", {
        "provider_id": "other", "credential_scope_id": "a" * 32, "identity_status": "known",
    }) == ""
    assert _scope(pinned, "", legacy="unknown") == ""
    snap = pinned.fa.read_snapshot(None, "openrouter", "")
    assert snap["state"] == "unknown"
    assert pinned.fa.evaluate_policy(
        snap, "openrouter", "m", "", time.time(), quota_policy="require_fresh_account_allowance"
    )["allowed"] is False


def test_no_credential_borrowing_between_accounts(pinned, tmp_path):
    store_dir = tmp_path / "store"; store_dir.mkdir()
    a = _scope(pinned, "a" * 32)
    b = _scope(pinned, "b" * 32)
    _write_allowance(pinned, store_dir, a, remaining=50)
    store = pinned.state.StateStore(store_dir)
    # Account B has no observation of its own.
    assert pinned.fa.read_snapshot(store, "openrouter", b)["quota_evidence"] == []
    # A's snapshot evaluated under B's scope is not authority for B.
    a_snap = pinned.fa.read_snapshot(store, "openrouter", a)
    assert pinned.fa.evaluate(a_snap, "openrouter", "m", b, time.time())["state"] == "unknown"


def test_http_429_does_not_infer_quota_exhaustion(pinned, tmp_path):
    store_dir = tmp_path / "store"; store_dir.mkdir()
    a = _scope(pinned, "a" * 32)
    _write_allowance(pinned, store_dir, a, remaining=50)  # fresh and healthy
    store = pinned.state.StateStore(store_dir)
    pinned.fa.invalidate_cached(store, "openrouter", a, 429)
    snap = pinned.fa.read_snapshot(store, "openrouter", a)
    # A rate limit marks the observation unavailable; it never synthesizes an
    # exhausted allowance or a hard billing stop.
    assert snap["state"] == "unavailable"
    assert snap["error_category"] == "rate_limited"
    result = pinned.fa.evaluate_policy(
        snap, "openrouter", "m", a, time.time(), quota_policy="require_fresh_account_allowance"
    )
    assert result["allowed"] is False
    assert result["state"] != "exhausted"
    assert result["hard_stop_guaranteed"] is False


def test_unusable_provider_and_unauthorized_paid_fallback_refused(pinned):
    # Paid and unknown-cost models are never eligible, even if a legacy
    # paid_authorized flag is present -- no unauthorized paid fallback.
    assert pinned.elig.cost_allowed("free") is True
    assert pinned.elig.cost_allowed("paid", paid_authorized=True) is False
    assert pinned.elig.cost_allowed("premium") is False
    assert pinned.elig.cost_allowed("unknown") is False
    assert pinned.elig.cost_allowed(None) is False
    reasons = pinned.elig.rejection_reasons(
        {"provider": "x", "model": "paid-model", "cost_tier": "paid",
         "context_length": 131072, "capabilities": {"tools": "supported"}},
        requires_tools=True, min_context=8192,
    )
    assert "cost_not_verified_free" in reasons


def test_strict_no_charge_policy_never_claims_signed_enforcement(pinned, tmp_path):
    store_dir = tmp_path / "store"; store_dir.mkdir()
    a = _scope(pinned, "a" * 32)
    _write_allowance(pinned, store_dir, a, remaining=50)
    snap = pinned.fa.read_snapshot(pinned.state.StateStore(store_dir), "openrouter", a)
    result = pinned.fa.evaluate_policy(
        snap, "openrouter", "m", a, time.time(), quota_policy="require_no_charge_guarantee"
    )
    assert result["allowed"] is False
    assert result["hard_stop_guaranteed"] is False
    assert result["verified_no_charge_execution"] is False


# --- plugin lifecycle (hot disable / re-enable / consumer ownership) -------

class _Ctx:
    def __init__(self):
        self.registered = []
        self.failing = False

    def register_middleware(self, kind, fn):
        if self.failing:
            raise RuntimeError("host registration unavailable")
        self.registered.append((kind, fn))


def test_load_and_enable_registers_consumer(pinned, monkeypatch):
    monkeypatch.setattr(
        pinned.native, "detect_host_native_routing",
        lambda: {"supported": True, "host_version": "rc160"},
    )
    ctx = _Ctx()
    report = pinned.native.register_if_supported(ctx, settings=SETTINGS)
    assert report["registered"] is True
    assert report["enabled"] is True
    assert len(ctx.registered) == 1
    assert pinned.native.is_enabled(SETTINGS) is True


def test_consumer_establishes_actionable_route(pinned, seeded_store):
    result = pinned.native.resolve_consumer_route(
        metadata=dict(META), store_dir=seeded_store, settings=SETTINGS,
    )
    assert result["status"] == "route"
    assert result["provider"] == "mockinfra"
    assert result["model"] == "mockinfra-free-model"


def test_hot_disable_refuses_and_makes_consumer_inactive(pinned, seeded_store, monkeypatch):
    monkeypatch.setattr(
        pinned.native, "detect_host_native_routing",
        lambda: {"supported": True, "host_version": "rc160"},
    )
    # A live session had an actionable route under the enabled settings.
    assert pinned.native.resolve_consumer_route(
        metadata=dict(META), store_dir=seeded_store, settings=SETTINGS,
    )["status"] == "route"

    # Plugin disabled mid-session: registration ownership is gone and any
    # further consumer attempt is a revocation-style refusal, not a passthrough.
    disabled = {**SETTINGS, "enabled": False}
    assert pinned.native.is_enabled(disabled) is False
    ctx = _Ctx()
    report = pinned.native.register_if_supported(ctx, settings=disabled)
    assert report["registered"] is False
    assert ctx.registered == []
    result = pinned.native.resolve_consumer_route(
        metadata=dict(META), store_dir=seeded_store, settings=disabled,
    )
    assert result["status"] == "refuse"
    assert "not enabled" in str(result.get("reason") or result.get("error"))


def test_virtual_selection_refused_when_consumer_unavailable(pinned, seeded_store):
    for settings in (
        {**SETTINGS, "enabled": False},
        {**SETTINGS, "providers": "not-a-list"},
        {**SETTINGS, "providers": []},
    ):
        result = pinned.native.resolve_consumer_route(
            metadata={**META, "selection_mode": "manual"},
            store_dir=seeded_store, settings=settings,
        )
        assert result["status"] == "refuse", settings


def test_re_enable_restores_only_with_appropriate_opt_in(pinned, seeded_store, monkeypatch):
    monkeypatch.setattr(
        pinned.native, "detect_host_native_routing",
        lambda: {"supported": True, "host_version": "rc160"},
    )
    disabled = {**SETTINGS, "enabled": False}
    assert pinned.native.resolve_consumer_route(
        metadata=dict(META), store_dir=seeded_store, settings=disabled,
    )["status"] == "refuse"

    # Re-enable + register; MODE B still needs its own native-routing opt-in.
    ctx = _Ctx()
    assert pinned.native.register_if_supported(ctx, settings=SETTINGS)["registered"] is True
    no_optin = {**SETTINGS, "enable_native_routing": False}
    assert pinned.native.resolve_consumer_route(
        metadata=dict(META), store_dir=seeded_store, settings=no_optin,
    )["status"] == "passthrough"
    # With the opt-in restored, routing resumes.
    assert pinned.native.resolve_consumer_route(
        metadata=dict(META), store_dir=seeded_store, settings=SETTINGS,
    )["status"] == "route"


def test_unreadable_settings_and_missing_binding_fail_closed(pinned, seeded_store):
    unreadable = pinned.native.resolve_consumer_route(
        metadata=dict(META), store_dir=seeded_store, settings="not-a-dict",
    )
    assert unreadable["status"] == "refuse"
    unbound = pinned.native.resolve_consumer_route(
        metadata={**META, "session_id": "   "}, store_dir=seeded_store, settings=SETTINGS,
    )
    assert unbound["status"] == "refuse"
