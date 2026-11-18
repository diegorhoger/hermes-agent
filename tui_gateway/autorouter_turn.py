"""AutoRouter continuous per-turn route re-evaluation for sessions it manages.

Only sessions whose agent was built through the AutoRouter pre-inference
seam (ordinary session, **MODE B**) or through the explicit virtual pick
(**MODE A**) carry the ``agent._autorouter_routed`` tag. Explicit physical
picks never carry it and are never rewritten.

Routing modes are evaluated **exactly once per decision**:

* **MODE B — ordinary managed session.** The registered ``pre_inference_route``
  consumer is asked once, with ``explicit_model: False``. A ``route`` decision
  pins this turn's physical route; ``passthrough``, a refusal, an exception or
  an unusable decision fails closed — a managed session is never widened into
  the previously pinned route or the stock default.
* **MODE A — explicit virtual pick.** The consumer is asked once, with
  ``explicit_model: True`` / ``selection_mode: "manual"``, exactly the call the
  build path uses; any non-``route`` outcome fails closed.

The validated decision is then resolved to a physical runtime through Hermes'
canonical resolver (``resolve_runtime_provider``) — never through a second
consumer call, and never through raw credential reads. The resolved route
must agree with the decision; anything else fails closed before any provider
request is issued.

At each new-turn boundary (before any inference request is made):

* re-resolve the physical route for the agent's own mode (once);
* keep the current route when it is still the validated decision;
* otherwise commit the newly resolved physical route in place through the
  agent's own ``switch_model`` (the same in-place swap the TUI ``/model``
  path uses), which rebuilds the provider client atomically and rolls
  back on failure;
* fail closed before any provider request when the consumer refuses, the
  physical runtime cannot be resolved, it disagrees with the decision, or
  the swap itself fails — an AutoRouter failure is never widened into a
  stock (potentially paid or unknown-cost) request, and never replayed once
  any output may have committed (nothing runs until after this gate).

An AutoRouter-managed session whose authorization or verified-free evidence
was revoked fails closed here: the consumer no longer grants the route, so
the turn is refused rather than kept on the last route or widened to stock.

The visible routing selection is untouched by this module: the session's
persisted selection (e.g. ``autorouter/auto-free``) stays what the user
picked; only the physical route the agent talks to changes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

VIRTUAL_PROVIDER = "autorouter"
VIRTUAL_MODEL = "auto-free"


def _session_id(sid: str, session: dict) -> str:
    return str(session.get("session_key") or sid or "").strip()


def _platform(session: dict) -> str:
    try:
        from hermes_cli.middleware import apply_pre_inference_route_middleware
        from tui_gateway.agent_factory import _resolve_agent_platform
        source = (session.get("source") or None)
        platform = _resolve_agent_platform(source) if source else None
        return platform or "tui"
    except Exception:
        return "tui"


def _profile_name() -> str | None:
    """The bound profile for this session, or ``None`` when it cannot be established.

    Never falls back to the process-wide launch profile: under a multiplexed
    gateway or a per-session profile scope the process environment belongs to
    the launch profile, and a routing decision (or its credentials) may not
    cross that boundary.
    """
    try:
        from hermes_cli.profiles import current_profile_name
        name = current_profile_name()
    except Exception:
        return None
    if isinstance(name, str) and name.strip():
        return name.strip()[:128]
    return None


def read_route(agent) -> tuple[str, str]:
    return (getattr(agent, "provider", "") or "").strip().lower(), getattr(agent, "model", "") or ""


def _refusal(reason: str) -> RuntimeError:
    return RuntimeError(f"AutoRouter continuous routing refused before any provider request: {reason}")


def _mode_metadata(agent, session_id: str, platform: str, profile: str) -> dict:
    if getattr(agent, "_autorouter_virtual", False):
        # MODE A — explicit virtual pick: manual selection semantics, the same
        # call shape the build path and the composer switch use.
        return {
            "session_id": session_id,
            "platform": platform,
            "hermes_profile": profile,
            "requested_model": VIRTUAL_MODEL,
            "requested_provider": VIRTUAL_PROVIDER,
            "explicit_model": True,
            "selection_mode": "manual",
        }
    # MODE B — ordinary managed session: automatic routing semantics.
    current_provider, current_model = read_route(agent)
    return {
        "session_id": session_id,
        "platform": platform,
        "hermes_profile": profile,
        "requested_model": current_model[:199],
        "requested_provider": current_provider[:64],
        "explicit_model": False,
    }


def turn_boundary(sid: str, session: dict, agent) -> bool:
    """Re-resolve the route; returns True when the live route was changed."""
    if not getattr(agent, "_autorouter_routed", False):
        # Never managed (explicit physical pick or routing never engaged):
        # stock semantics, the consumer is not involved.
        return False
    session_id = _session_id(sid, session)
    if not session_id:
        logger.warning("autorouter turn boundary: session has no stable id; refusing turn")
        raise _refusal("no stable session binding for this turn")
    from hermes_cli.middleware import PRE_INFERENCE_ROUTE_MIDDLEWARE, apply_pre_inference_route_middleware
    from hermes_cli.plugins import has_middleware
    if not has_middleware(PRE_INFERENCE_ROUTE_MIDDLEWARE):
        # The consumer unloaded (plugin disabled/revoked mid-session): fail
        # closed for an AutoRouter-managed session rather than widen into a
        # stock route.
        raise _refusal("the routing consumer is no longer registered on this host")
    platform = _platform(session)
    profile = _profile_name()
    if profile is None:
        # The routing decision may not be taken against the launch profile's
        # credentials or policy: fail closed.
        raise _refusal("the bound profile for this session cannot be established")
    metadata = _mode_metadata(agent, session_id, platform, profile)
    mode = "manual" if metadata.get("selection_mode") == "manual" else "automatic"
    try:
        route = apply_pre_inference_route_middleware(metadata, platform=platform)
    except Exception as exc:
        logger.warning("autorouter turn boundary: middleware failed; refusing turn (%s evaluation)",
                       mode, exc_info=True)
        raise _refusal(f"the consumer failed the {mode} evaluation: {exc}") from exc
    if route is None:
        # Registered consumer produced no usable decision: fail closed for a
        # managed session.
        raise _refusal("the consumer returned no decision")
    status = getattr(route, "status", None)
    if status == "passthrough":
        # A managed session has no provable opt-out: keeping the previously
        # pinned route would launder revoked (or never-granted) authorization,
        # and the stock default would widen into an unapproved route.
        raise _refusal(
            "the session is AutoRouter-managed and the consumer declined to route "
            "this turn (passthrough); refusing before any provider request")
    if status != "route":
        reason = str(getattr(route, "reason", "") or "no eligible route")
        raise _refusal(reason)
    provider = str(getattr(route, "provider", "") or "").strip().lower()
    model = str(getattr(route, "model", "") or "").strip()
    if not provider or not model:
        raise _refusal("the consumer returned an unusable route")
    current_provider, current_model = read_route(agent)
    if provider == current_provider and model == current_model:
        # Still the right route: keep it (no churn). The decision stands on its
        # own; no credentials are re-resolved and the consumer is not re-invoked.
        return False
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested=provider, target_model=model)
    except Exception as exc:
        logger.warning("autorouter turn boundary: physical runtime unresolvable; refusing turn",
                       exc_info=True)
        raise _refusal(f"the physical runtime for the validated route cannot be resolved: {exc}") from exc
    if not isinstance(runtime, dict):
        raise _refusal("the canonical resolver returned no runtime for the validated route")
    # The resolved runtime must agree with the validated decision — a fallback
    # rung that lands somewhere else (denied scope, renamed provider, dead
    # endpoint) must never be accepted as the route of record.
    resolved_provider = str(runtime.get("provider") or "").strip().lower()
    requested_provider = str(runtime.get("requested_provider") or "").strip().lower()
    if provider not in {resolved_provider, requested_provider}:
        raise _refusal(
            f"the resolved physical route ({resolved_provider or 'unknown'}) disagrees with "
            "the routing decision")
    runtime_model = str(runtime.get("model") or "").strip()
    if runtime_model and runtime_model != model:
        raise _refusal(
            f"the resolved physical route (model {runtime_model}) disagrees with "
            "the routing decision")
    logger.info("autorouter turn boundary: switching physical route %s/%s -> %s/%s",
                current_provider, current_model, provider, model)
    try:
        agent.switch_model(
            new_model=model, new_provider=provider,
            api_key=runtime.get("api_key") or "", base_url=runtime.get("base_url") or "",
            api_mode=runtime.get("api_mode") or "", capabilities=None)
    except Exception as exc:
        # switch_model rolls the agent back to the pre-switch snapshot on
        # failure; the turn fails closed with no provider request issued.
        logger.warning("autorouter turn boundary: in-place route commit failed; refusing turn",
                       exc_info=True)
        raise _refusal(f"the validated route could not be committed to the live agent: {exc}") from exc
    return True
