"""AutoRouter continuous per-turn route re-evaluation for sessions it manages.

Only sessions whose agent was built through the AutoRouter pre-inference
seam (ordinary session, MODE B) or through the explicit virtual pick
(MODE A) carry the ``agent._autorouter_routed`` tag. Explicit physical
picks never carry it and are never rewritten.

At each new-turn boundary (before any inference request is made):

* re-resolve the physical route through the same sanctioned consumer the
  agent build used (``pre_inference_route`` middleware);
* keep the current route when it is still eligible;
* otherwise commit the newly resolved physical route in place through the
  agent's own ``switch_model`` (the same in-place swap the TUI ``/model``
  path uses), which rebuilds the provider client atomically and rolls
  back on failure;
* fail closed before any provider request when the consumer refuses,
  the physical runtime cannot be resolved, or the swap itself fails —
  an AutoRouter failure is never widened into a stock (potentially paid
  or unknown-cost) request, and never replayed once any output may
  have committed (nothing runs until after this gate).

The visible routing selection is untouched by this module: the session's
persisted selection (e.g. ``autorouter/auto-free``) stays what the user
picked; only the physical route the agent talks to changes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _session_id(sid: str, session: dict) -> str:
    return str(session.get("session_key") or sid or "").strip()


def _platform(session: dict) -> str:
    try:
        from hermes_cli.middleware import apply_pre_inference_route_middleware  # noqa: F401
        from tui_gateway.agent_factory import _resolve_agent_platform
        source = (session.get("source") or None)
        platform = _resolve_agent_platform(source) if source else None
        return platform or "tui"
    except Exception:
        return "tui"


def _profile_name() -> str:
    try:
        from hermes_cli.profiles import current_profile_name
        name = current_profile_name()
        if isinstance(name, str) and name.strip():
            return name.strip()
    except Exception:
        pass
    import os
    return (os.environ.get("HERMES_ACTIVE_PROFILE") or os.environ.get("HERMES_PROFILE") or "default").strip() or "default"


def read_route(agent) -> tuple[str, str]:
    return (getattr(agent, "provider", "") or "").strip().lower(), getattr(agent, "model", "") or ""


def turn_boundary(sid: str, session: dict, agent) -> bool:
    """Re-resolve the route; returns True when the live route was changed."""
    if not getattr(agent, "_autorouter_routed", False):
        return False
    session_id = _session_id(sid, session)
    if not session_id:
        logger.warning("autorouter turn boundary: session has no stable id; refusing turn")
        raise RuntimeError(
            "AutoRouter continuous routing refused: no stable session binding for this turn")
    from hermes_cli.middleware import PRE_INFERENCE_ROUTE_MIDDLEWARE, apply_pre_inference_route_middleware
    from hermes_cli.plugins import has_middleware
    if not has_middleware(PRE_INFERENCE_ROUTE_MIDDLEWARE):
        # The consumer unloaded (plugin disabled/revoked mid-session): fail
        # closed for an AutoRouter-managed session rather than widen into a
        # stock route.
        raise RuntimeError(
            "AutoRouter continuous routing refused: the routing consumer is no longer "
            "registered on this host")
    platform = _platform(session)
    metadata = {
        "session_id": session_id,
        "platform": platform,
        "hermes_profile": _profile_name(),
        "requested_model": (getattr(agent, "model", "") or "")[:199],
        "requested_provider": (getattr(agent, "provider", "") or "")[:64],
        "explicit_model": False,
    }
    try:
        route = apply_pre_inference_route_middleware(metadata, platform=platform)
    except Exception as exc:
        logger.warning("autorouter turn boundary: middleware failed; refusing turn", exc_info=True)
        raise RuntimeError(
            f"AutoRouter continuous routing refused before any provider request: {exc}") from exc
    if route is None:
        # Registered consumer produced no usable decision: fail closed for a
        # managed session.
        raise RuntimeError(
            "AutoRouter continuous routing refused before any provider request: "
            "the consumer returned no decision")
    if getattr(route, "status", None) == "passthrough":
        # Provable opt-out (e.g. the operator disabled native routing):
        # proceed with the currently pinned route, no new decision.
        logger.info("autorouter turn boundary: consumer passthrough; keeping pinned route")
        return False
    if getattr(route, "status", None) != "route":
        reason = str(getattr(route, "reason", "") or "no eligible route")
        raise RuntimeError(
            f"AutoRouter continuous routing refused before any provider request: {reason}")
    provider = str(getattr(route, "provider", "") or "").strip().lower()
    model = str(getattr(route, "model", "") or "").strip()
    current_provider, current_model = read_route(agent)
    if provider == current_provider and model == current_model:
        # Still the right route: keep it (no churn).
        return False
    try:
        from hermes_cli.autorouter_picker import resolve_autorouter_physical

        runtime, receipt = resolve_autorouter_physical(session_id=session_id, platform=platform)
    except Exception as exc:
        logger.warning("autorouter turn boundary: physical runtime unresolvable; refusing turn",
                       exc_info=True)
        raise RuntimeError(
            f"AutoRouter continuous routing refused before any provider request: {exc}") from exc
    if (str(receipt.get("provider", "")).lower() != provider or str(receipt.get("model", "")) != model):
        # The receipt must match the middleware decision; never apply a different route.
        raise RuntimeError(
            "AutoRouter continuous routing refused before any provider request: "
            "the resolved physical route disagrees with the routing decision")
    logger.info("autorouter turn boundary: switching physical route %s/%s -> %s/%s",
                current_provider, current_model, provider, model)
    agent.switch_model(
        new_model=model, new_provider=provider,
        api_key=runtime.get("api_key") or "", base_url=runtime.get("base_url") or "",
        api_mode=runtime.get("api_mode") or "", capabilities=None)
    return True
