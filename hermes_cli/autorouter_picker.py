"""Virtual provider ``autorouter`` — ``AutoRouter · Auto (Free)`` (issue #116).

An opt-in, host-sanctioned routing *mode*, not a provider-served model. The
``auto-free`` selection never reaches a provider: client construction always
resolves an *eligible physical* provider/model through the registered
``pre_inference_route`` consumer (AutoRouter's resolver or a compatible
consumer) and refuses — before any provider request — when the consumer, the
host seam or an eligible route is missing.

Layer contract:

* picker row: injected only when (a) raw config says the operator enabled the
  AutoRouter plugin *and* its chat router, (b) the host supports the seam,
  and (c) THE AutoRouter plugin itself registered the ``pre_inference_route``
  middleware (ownership-checked via the plugin manager's registry ledger).
  Anything less means the row does not exist — never a decorative,
  non-actionable item.
* validation (switch pipeline): accepts ``auto-free`` under the same gate;
  never touches the consumer. Everything else is deterministically refused.
* runtime resolution for the switch credential step: a credential-free marker
  runtime (like MoA's) so the persisted pin never carries live material.
* live client construction / in-place switch / resume / provider resolution:
  the three call sites resolve the PHYSICAL route through the consumer with
  session binding, build on it, and refuse before any provider request when
  it cannot.

Only the canonical identifiers are recognized: the bare pair
``(auto-free, autorouter)`` and the wide ``autorouter/auto-free`` provider.
Explicit physical picks are never rewritten. The provider
``autorouter://local`` marker exists only inside this host. Fail closed
everywhere; an unsupported host surfaces a deterministic refusal.
"""

from __future__ import annotations

import os
from typing import Any, Optional

VIRTUAL_PROVIDER = "autorouter"
VIRTUAL_MODEL = "auto-free"
WIDE_VIRTUAL_MODEL = f"{VIRTUAL_PROVIDER}/{VIRTUAL_MODEL}"
DISPLAY_NAME = "AutoRouter"
PLACEHOLDER_KEY = "autorouter-virtual-provider"
MODELS = (VIRTUAL_MODEL,)

# Plugin identity this host pins for the AutoRouter consumer: the plugin must
# be LOADED and must OWN the middleware it registered.
AUTO_ROUTER_PLUGIN_ID = "autorouter"

def _bounded(value: Any, limit: int) -> str:
    text = str(value if value is not None else "")
    text = "".join(char for char in text if ord(char) >= 32 and ord(char) != 127)
    return text[:limit]


def _raw_settings() -> dict[str, Any]:
    try:
        from hermes_cli.config import read_raw_config

        raw = read_raw_config()
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    plugins = raw.get("plugins")
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    entry = entries.get("autorouter") if isinstance(entries, dict) else None
    settings = entry.get("settings") if isinstance(entry, dict) else None
    return settings if isinstance(settings, dict) else {}


def _profile_name() -> str:
    """Profile-scoped profile name (multiplexed gateway/cron/fork): never the launch profile."""
    try:
        from hermes_cli import profiles

        name = profiles.current_profile_name()
        if isinstance(name, str) and name:
            return _bounded(name, 128)
    except Exception:
        pass
    return "default"


def _consumer_registered() -> bool:
    """True iff the *AutoRouter* plugin owns a currently-registered consumer.

    ``has_middleware`` proves only that *some* middleware is registered; the
    ownership ledger is the host's own record of which plugin registered
    what, so an unrelated consumer never satisfies the gate. After unload the
    ledger no longer carries the registration and the gate fails closed.
    """
    try:
        from hermes_cli.middleware import (
            PRE_INFERENCE_ROUTE_MIDDLEWARE,
            apply_pre_inference_route_middleware,  # noqa: F401  (presence is the contract)
        )
        from hermes_cli.plugins import get_plugin_manager, has_middleware

        if not has_middleware(PRE_INFERENCE_ROUTE_MIDDLEWARE):
            return False
        manager = get_plugin_manager()
        ledger = getattr(manager, "_ownership_ledger", None)
        if not isinstance(ledger, dict):
            return False
        aliases = (
            AUTO_ROUTER_PLUGIN_ID,
            f"user/{AUTO_ROUTER_PLUGIN_ID}",
            f"bundled/{AUTO_ROUTER_PLUGIN_ID}",
            f"standalone/{AUTO_ROUTER_PLUGIN_ID}",
        )
        for key in aliases:
            entries = ledger.get(key)
            if not entries:
                continue
            for entry in entries:
                if (getattr(entry, "active", False)
                        and getattr(entry, "kind", None) == "middleware"
                        and getattr(entry, "key", None) == PRE_INFERENCE_ROUTE_MIDDLEWARE):
                    return True
        return False
    except Exception:
        return False


def gate_reason() -> Optional[str]:
    """Why the AutoRouter option is not actionable, or ``None`` when it is."""
    settings = _raw_settings()
    if settings.get("enabled") is not True:
        return "the AutoRouter plugin is not enabled"
    if settings.get("enable_chat_router") is not True:
        return "the AutoRouter chat router is not enabled"
    if not _consumer_registered():
        return "no AutoRouter consumer is registered on this host"
    return None


def actionable() -> bool:
    return gate_reason() is None


def picker_row(current_provider: str = "") -> Optional[dict[str, Any]]:
    """The ``AutoRouter`` picker row, or ``None`` when the mode is not actionable."""
    if not actionable():
        return None
    return {
        "slug": VIRTUAL_PROVIDER,
        "name": DISPLAY_NAME,
        "is_current": str(current_provider or "").strip().lower() == VIRTUAL_PROVIDER,
        "is_user_defined": False,
        "models": list(MODELS),
        "total_models": len(MODELS),
        "source": "virtual",
        "authenticated": True,
        "auth_type": "virtual",
        "warning": (
            "AutoRouter resolves an eligible verified-free model before every request; "
            "account allowance is never assumed."
        ),
    }


def is_autorouter_pick(model: Any, provider: Any) -> bool:
    """True exactly for the documented virtual picks; False for everything else.

    Accepts only (``auto-free``, ``autorouter``) and (``autorouter/auto-free``,
    ``autorouter`` / no provider). Arbitrary ``…/auto-free`` suffixes, missing
    slugs or mismatched providers are rejected, never interpreted.
    """
    p = str(provider or "").strip().lower()
    m = str(model or "").strip().lower()
    if p == VIRTUAL_PROVIDER and m == VIRTUAL_MODEL:
        return True
    if m == WIDE_VIRTUAL_MODEL and p in ("", VIRTUAL_PROVIDER):
        return True
    return False


def virtual_marker_runtime(requested_provider: Optional[str] = None) -> dict[str, Any]:
    """Credential-free marker runtime for the switch/persist step.

    Carries no usable endpoint and no credential material, so a persisted
    session pin never leaks live secrets. Client construction never sees it —
    the agent build re-resolves the physical route first.
    """
    return {
        "provider": VIRTUAL_PROVIDER,
        "api_mode": "chat_completions",
        "base_url": "autorouter://local",
        "api_key": PLACEHOLDER_KEY,
        "source": "autorouter-virtual-provider",
        "requested_provider": requested_provider or VIRTUAL_PROVIDER,
    }


def resolve_autorouter_physical(
    *, session_id: str, platform: str = "tui",
) -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve the virtual pick to a physical runtime; fail closed.

    ``session_id`` is required: the consumer's binding semantics only make
    sense bound to one session. Passing ``sid`` from the caller keeps binding
    aligned with the session that the user is actually in.

    Returns ``(runtime, receipt)`` where ``runtime`` is the physical provider's
    own resolved runtime and ``receipt`` carries no credential material.
    Raises ``RuntimeError`` unless the consumer produces a usable ``route``.
    """
    from hermes_cli.middleware import apply_pre_inference_route_middleware

    sid = _bounded(session_id, 256)
    if not sid:
        raise RuntimeError(
            "AutoRouter selection refused before any provider request: no stable session binding")
    platform_hint = _bounded(platform, 64) or "tui"
    metadata = {
        "session_id": sid,
        "platform": platform_hint,
        "hermes_profile": _profile_name(),
        "requested_model": VIRTUAL_MODEL,
        "requested_provider": VIRTUAL_PROVIDER,
        "explicit_model": True,
        "selection_mode": "manual",
    }
    try:
        route = apply_pre_inference_route_middleware(metadata, platform=platform_hint)
    except Exception as exc:
        raise RuntimeError(
            f"AutoRouter selection refused before any provider request: {exc}") from exc
    reason = str(getattr(route, "reason", "") or "no routing decision returned") if route is not None else "no routing decision returned"
    if (
        route is None
        or getattr(route, "status", None) != "route"
        or not getattr(route, "provider", None)
        or not getattr(route, "model", None)
    ):
        # passthrough is the only provable opt-out for ordinary sessions; an
        # explicit AutoRouter pick has none — it fails closed.
        raise RuntimeError(
            f"AutoRouter selection refused before any provider request: {reason.strip()}")
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested=route.provider.strip().lower(), target_model=route.model.strip())
    except Exception as exc:
        # The physical route itself would not resolve (revoked scope, missing
        # pool, provider down): never widen to an unapproved fallback — refuse.
        raise RuntimeError(
            f"AutoRouter selection resolved a provider that is not usable on this host: {exc}") from exc
    receipt = {"provider": route.provider.strip().lower(), "model": route.model.strip(),
               "session_id": sid}
    return runtime, receipt
