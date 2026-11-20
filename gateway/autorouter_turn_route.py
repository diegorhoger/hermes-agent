"""Gateway turn-time AutoRouter resolution (virtual pick + pre-inference routing).

Mirrors the fail-closed semantics of tui_gateway.agent_factory for the gateway/local
session turn path: explicit autorouter virtual picks resolve to a physical route per
turn/session binding, and ordinary sessions may be routed by a registered
``pre_inference_route`` consumer when no explicit model/provider override is in effect.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)


def request_route_authority(request_json: str | None) -> str:
    """Classify the route authority carried by a frozen session-create request.

    ``build_policy`` records the client's create fields verbatim in
    ``request_json``; a later session model switch stamps ``model_requested``
    there too. This is the only place that decides whether a session picked its
    own route, so it must stay independent of the profile the session follows:
    a followed profile can change underneath a still-authoritative create pick.

    Returns one of:
      * ``"explicit"`` — a model, non-auto provider, endpoint or recorded model
        switch is present; the user's route is authoritative.
      * ``"ordinary"`` — the request is readable and carries no route pick;
        ordinary-session pre-inference routing (Mode B) remains eligible.
      * ``"unavailable"`` — authority metadata is malformed or is not a JSON
        object. Callers must not treat this as grounds to rewrite the route.
    """
    text = request_json if isinstance(request_json, str) else ""
    text = text.strip() or "{}"
    try:
        import json

        req = json.loads(text)
    except Exception:
        return "unavailable"
    if not isinstance(req, dict):
        return "unavailable"

    model = req.get("model")
    provider = req.get("provider")
    model_requested = req.get("model_requested")
    base_url = req.get("base_url")
    explicit = bool(
        (isinstance(model, str) and model.strip())
        or (
            isinstance(provider, str)
            and provider.strip()
            and provider.strip().lower() != "auto"
        )
        or (isinstance(model_requested, str) and model_requested.strip())
        or (isinstance(base_url, str) and base_url.strip())
    )
    return "explicit" if explicit else "ordinary"


def has_explicit_request_route(request_json: str | None) -> bool:
    """True when a create request carries an authoritative route pick.

    Unreadable authority metadata fails closed: it is never an invitation for
    AutoRouter to rewrite a route whose provenance cannot be established.
    """
    return request_route_authority(request_json) != "ordinary"


def turn_has_explicit_route_authority(
    *,
    request_json: str | None,
    session_model_override: Any = None,
    one_turn_override: Any = None,
) -> bool:
    """Single owner of turn route authority precedence.

    A user-selected create request, a committed session model switch, or a
    one-turn override each make the route authoritative. Any of them prevents
    ordinary-session pre-inference routing from rewriting the model, provider
    or endpoint. Unreadable request metadata fails closed.
    """
    return bool(
        session_model_override
        or one_turn_override
        or has_explicit_request_route(request_json)
    )


def _bounded(value: Any, limit: int) -> str:
    text = str(value if value is not None else "")
    text = "".join(char for char in text if ord(char) >= 32 and ord(char) != 127)
    return text[:limit]


def _runtime_to_kwargs(runtime: dict[str, Any], *, fallback_provider: str | None = None) -> dict[str, Any]:
    """Convert a resolved runtime/provider record into gateway agent kwargs."""
    # The shared gateway converter is the only supported constructor boundary.
    # If it rejects malformed runtime data, do not silently pass through raw
    # provider fields or widen the credential/endpoint authority.
    from gateway.run import _runtime_agent_kwargs

    kwargs = _runtime_agent_kwargs(dict(runtime))
    provider = runtime.get("provider") or fallback_provider
    if provider:
        kwargs["provider"] = provider
        kwargs.setdefault("requested_provider", runtime.get("requested_provider") or provider)
    return kwargs


def resolve_autorouter_virtual_turn(
    *,
    model: str,
    runtime_kwargs: dict[str, Any],
    session_id: Optional[str],
    session_key: Optional[str],
    platform: str,
) -> tuple[str, dict[str, Any]] | None:
    """Resolve an explicit virtual AutoRouter pick to a physical turn route.

    Returns ``(model, runtime_kwargs)`` when the turn's effective route is a
    virtual autorouter pick, or ``None`` when it is not. Raises ``RuntimeError``
    before any provider request if the pick is not actionable or cannot be resolved.
    """
    try:
        from hermes_cli.autorouter_picker import (
            actionable,
            gate_reason,
            is_autorouter_pick,
            resolve_autorouter_physical,
        )
    except Exception as exc:
        provider = str(runtime_kwargs.get("provider") or runtime_kwargs.get("requested_provider") or "")
        if str(model or "").strip().lower() in {"auto-free", "autorouter/auto-free"} or provider.strip().lower() == "autorouter":
            raise RuntimeError(
                "AutoRouter selection refused before any provider request: host resolver unavailable"
            ) from exc
        return None

    provider = runtime_kwargs.get("provider") or runtime_kwargs.get("requested_provider")
    if not is_autorouter_pick(model, provider):
        return None
    if not actionable():
        try:
            why = gate_reason() or "unknown"
        except Exception:
            why = "unknown"
        raise RuntimeError(f"AutoRouter selection refused before any provider request: {why}")

    binding = session_id or session_key or ""
    if not binding:
        raise RuntimeError(
            "AutoRouter selection refused before any provider request: no stable session binding"
        )
    try:
        physical_runtime, receipt = resolve_autorouter_physical(session_id=binding, platform=platform or "gateway")
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    resolved_kwargs = _runtime_to_kwargs(dict(physical_runtime), fallback_provider=receipt.get("provider"))
    resolved_kwargs["provider"] = physical_runtime.get("provider") or receipt.get("provider")
    resolved_kwargs["requested_provider"] = (
        physical_runtime.get("requested_provider") or receipt.get("provider") or resolved_kwargs.get("requested_provider")
    )
    resolved_model = receipt.get("model") or physical_runtime.get("model") or model
    return str(resolved_model), resolved_kwargs


def apply_pre_inference_route_turn(
    *,
    model: str,
    runtime_kwargs: dict[str, Any],
    session_id: Optional[str],
    session_key: Optional[str],
    platform: str,
    has_explicit_model_or_provider: bool,
) -> tuple[str, dict[str, Any]]:
    """Apply ordinary-session AutoRouter pre-inference routing for gateway turns.

    Returns possibly rewritten ``(model, runtime_kwargs)``. Raises ``RuntimeError``
    before any provider request when a registered consumer refuses/fails/misbehaves.
    """
    if has_explicit_model_or_provider:
        return model, runtime_kwargs
    binding = session_id or session_key or ""
    if not binding:
        return model, runtime_kwargs

    try:
        from hermes_cli.middleware import (
            PRE_INFERENCE_ROUTE_MIDDLEWARE,
            apply_pre_inference_route_middleware,
        )
        from hermes_cli.plugins import has_middleware
    except Exception as exc:
        log.warning("pre_inference_route capability probe failed; refusing", exc_info=True)
        raise RuntimeError(
            "AutoRouter pre-inference route refused: capability probe failed"
        ) from exc

    try:
        registered = has_middleware(PRE_INFERENCE_ROUTE_MIDDLEWARE)
    except Exception as exc:
        log.warning("pre_inference_route capability probe failed; refusing", exc_info=True)
        raise RuntimeError(
            "AutoRouter pre-inference route refused: capability probe failed"
        ) from exc
    if not registered:
        return model, runtime_kwargs

    metadata = {
        "session_id": _bounded(binding, 256),
        "platform": _bounded(platform or "gateway", 64),
        "hermes_profile": _bounded(
            os.environ.get("HERMES_PROFILE") or os.environ.get("HERMES_ACTIVE_PROFILE") or "default",
            128,
        ),
        "requested_model": _bounded(model, 199),
        "requested_provider": _bounded(
            runtime_kwargs.get("requested_provider") or runtime_kwargs.get("provider"), 64
        ),
        "explicit_model": False,
    }
    try:
        route = apply_pre_inference_route_middleware(metadata, platform=platform or "gateway")
    except Exception as exc:
        log.warning("pre_inference_route middleware failed; refusing", exc_info=True)
        raise RuntimeError(f"AutoRouter pre-inference route refused: {exc}") from exc

    if route is None:
        raise RuntimeError(
            "AutoRouter pre-inference route refused: registered consumer returned no result"
        )
    if route.status == "passthrough":
        return model, runtime_kwargs
    if route.status == "refuse":
        raise RuntimeError(
            f"AutoRouter pre-inference route refused: {route.reason or 'no eligible free route'}"
        )
    if route.status == "route" and route.provider and route.model:
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider

            physical_runtime = resolve_runtime_provider(
                requested=str(route.provider).strip().lower(),
                target_model=str(route.model).strip(),
            )
            resolved_kwargs = _runtime_to_kwargs(dict(physical_runtime), fallback_provider=str(route.provider).strip().lower())
            resolved_kwargs["provider"] = physical_runtime.get("provider") or str(route.provider).strip().lower()
            resolved_kwargs["requested_provider"] = (
                physical_runtime.get("requested_provider") or str(route.provider).strip().lower()
            )
            return str(route.model).strip(), resolved_kwargs
        except Exception as exc:
            raise RuntimeError(
                f"AutoRouter pre-inference route refused: routed provider is not usable on this host: {exc}"
            ) from exc
    raise RuntimeError(
        "AutoRouter pre-inference route refused: consumer returned an unusable route"
    )


def resolve_autorouter_for_gateway_turn(
    *,
    model: str,
    runtime_kwargs: dict[str, Any],
    session_id: Optional[str],
    session_key: Optional[str],
    platform: str,
    has_explicit_model_or_provider: bool,
) -> tuple[str, dict[str, Any]]:
    """Apply virtual-pick resolution first, else ordinary-session pre-inference routing."""
    virtual = resolve_autorouter_virtual_turn(
        model=model,
        runtime_kwargs=runtime_kwargs,
        session_id=session_id,
        session_key=session_key,
        platform=platform,
    )
    if virtual is not None:
        return virtual
    return apply_pre_inference_route_turn(
        model=model,
        runtime_kwargs=runtime_kwargs,
        session_id=session_id,
        session_key=session_key,
        platform=platform,
        has_explicit_model_or_provider=has_explicit_model_or_provider,
    )
