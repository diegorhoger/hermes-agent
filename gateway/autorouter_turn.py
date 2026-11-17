"""AutoRouter continuous per-turn route evaluation for the messaging gateway.

Mirrors ``tui_gateway/autorouter_turn.py`` for the messaging execution path
(Telegram/Slack/etc.), where a session may reuse a cached ``AIAgent`` across
turns. The route is re-resolved at a safe pre-turn boundary — before any
provider request of this turn is issued and before the agent is built or the
cache signature is computed — rather than at agent-construction time.

Modes
-----
* **MODE A — explicit AutoRouter virtual pick** (``autorouter/auto-free``).
  Resolved through the same sanctioned consumer the TUI/Desktop uses
  (``hermes_cli.autorouter_picker.resolve_autorouter_physical``), bound to
  the session. The persisted/pinned selection stays ``autorouter/auto-free``;
  only the physical route the agent talks to changes per turn. An explicit
  pick is never widened to a stock route on refusal or failure — the turn
  fails closed before any provider request.
* **MODE B — ordinary session** (no explicit per-session pick). Evaluated
  only when the host's ``pre_inference_route`` consumer is registered (i.e.
  the operator enabled AutoRouter for ordinary sessions). A ``route``
  decision pins this turn's physical route; ``passthrough`` is the provable
  opt-out and runs stock; anything else refuses the turn before any provider
  request.
* **Explicit physical pick** — never rewritten; the consumer does not run.
* **Policy (following) sessions** — honoured, or re-resolved per turn when
  the frozen selection is ``mode: autorouter``.

Refusals raise :class:`AutoRouterRefusal`, a ``RuntimeError`` the turn
runner converts into a failed turn with zero provider requests issued. No
virtual identifier (``autorouter`` / ``autorouter/auto-free``) ever reaches
an inference endpoint: the virtual pick is resolved to a physical route
before the agent is built every turn.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

VIRTUAL_PROVIDER = "autorouter"


class AutoRouterRefusal(RuntimeError):
    """The turn must not run: the route could not be resolved fail-closed."""


def session_platform(source) -> str:
    """Config-key platform for ``source`` (LOCAL→``cli``, else enum value)."""
    try:
        from gateway.run import _platform_config_key
        return _platform_config_key(source.platform) if source is not None else "cli"
    except Exception:
        try:
            return str(getattr(getattr(source, "platform", None), "value", "cli")) or "cli"
        except Exception:
            return "cli"


def _scrub(text: object, limit: int = 300) -> str:
    """Bound a consumer-authored string for logs and chat replies."""
    raw = str(text or "")
    raw = "".join(char for char in raw if ord(char) >= 32 and ord(char) != 127)
    lowered = raw.lower()
    for fragment in ("sk-", "sk_", "bearer ", "gho_", "ghp_", "github_pat_", "nvapi-", "hf_"):
        idx = lowered.find(fragment)
        while idx != -1:
            raw = raw[:idx] + "[redacted]" + raw[idx + len(fragment):]
            lowered = raw.lower()
            idx = lowered.find(fragment)
    return raw[:limit]


def _peek_override(runner, session_key: str):
    peek = getattr(runner, "_peek_session_state", None)
    if not callable(peek) or not session_key:
        return None
    try:
        state = peek(session_key)
    except Exception:
        return None
    conversation = getattr(state, "conversation", None) if state is not None else None
    return getattr(conversation, "model_override", None) if conversation is not None else None


def _is_virtual_pick(override) -> bool:
    try:
        from hermes_cli.autorouter_picker import is_autorouter_pick
        return bool(override) and is_autorouter_pick(
            override.get("model"), override.get("provider"))
    except Exception:
        return False


def _profile_name() -> str:
    try:
        from hermes_cli import profiles

        name = profiles.current_profile_name()
        if isinstance(name, str) and name.strip():
            return name.strip()[:128]
    except Exception:
        pass
    import os

    return (os.environ.get("HERMES_ACTIVE_PROFILE") or os.environ.get("HERMES_PROFILE") or "default").strip()[:128] or "default"


def _kwargs_from_runtime(runtime: dict, *, requested_provider: str) -> dict:
    """Gateway-shaped runtime kwargs for an already-resolved runtime.

    The route is resolved through Hermes' own resolver — never through raw
    credential reads — and this only reshapes the result into what the
    gateway turn machinery consumes.
    """
    from gateway.run import _runtime_agent_kwargs

    if not isinstance(runtime, dict):
        raise AutoRouterRefusal("AutoRouter route refused: the physical runtime did not resolve")
    kwargs = {
        **_runtime_agent_kwargs(runtime),
        "request_overrides": dict(runtime.get("request_overrides") or {}),
        "capabilities": dict(runtime.get("capabilities") or {}),
        "requested_provider": requested_provider,
    }
    if runtime.get("credential_pool") is not None:
        kwargs["credential_pool"] = runtime.get("credential_pool")
    if runtime.get("max_tokens") is not None:
        kwargs["max_tokens"] = runtime.get("max_tokens")
    return kwargs


def _resolve_physical(*, session_key: str, platform: str) -> tuple[str, dict]:
    """Resolve an explicit virtual pick to (model, runtime_kwargs); fail closed."""
    from hermes_cli.autorouter_picker import resolve_autorouter_physical

    try:
        runtime, receipt = resolve_autorouter_physical(session_id=session_key, platform=platform)
    except Exception as exc:
        logger.warning("autorouter: virtual pick refused; no provider request will be issued: %s",
                       _scrub(exc), exc_info=True)
        raise AutoRouterRefusal(
            f"AutoRouter routing refused before any provider request: {_scrub(exc)}") from exc
    model = str(receipt.get("model") or "").strip()
    provider = str(receipt.get("provider") or "").strip().lower()
    if not model or not provider:
        raise AutoRouterRefusal(
            "AutoRouter routing refused before any provider request: resolved route is incomplete")
    return model, _kwargs_from_runtime(runtime, requested_provider=VIRTUAL_PROVIDER)


def _policy_selection_mode(runner, source):
    """Return ``(mode, frozen)`` for a launch-policy session, or ``(None, None)`` when
    this session has no policy. ``mode`` is the frozen selection's mode (``autorouter``
    for a virtual pick) or ``frozen`` for any other policy selection — a policy session
    keeps its frozen route unless it is the AutoRouter virtual pick."""
    try:
        from gateway.session_policy import policy_for_source
        from gateway.session_authorities import active_authority

        policy = policy_for_source(runner, source)
        if policy is None:
            return None, None
        authority = active_authority(runner)
        frozen, _model = policy.effective(authority, secrets=False)
        block = (frozen or {}).get("model") if isinstance(frozen, dict) else None
        selection = block.get("selection") if isinstance(block, dict) else None
        mode = selection.get("mode") if isinstance(selection, dict) else None
        return (mode or "frozen"), frozen
    except Exception:
        logger.warning("autorouter: policy selection probe failed; leaving the frozen route alone", exc_info=True)
        return "frozen", None


def _ordinary_route(*, runner, source, session_key: str, user_config, model, runtime_kwargs):
    """MODE B: evaluate the ordinary session at the turn boundary.

    Returns (model, runtime_kwargs, applied). ``applied`` is ``stock`` for the
    provable opt-out (passthrough or no consumer), ``ordinary`` for a freshly
    resolved route. Raises :class:`AutoRouterRefusal` before any provider
    request on refusal.
    """
    try:
        from hermes_cli.middleware import (
            PRE_INFERENCE_ROUTE_MIDDLEWARE,
            apply_pre_inference_route_middleware,
        )
        from hermes_cli.plugins import has_middleware

        registered = has_middleware(PRE_INFERENCE_ROUTE_MIDDLEWARE)
    except Exception as exc:
        # Cannot even probe the routing capability: failing open would widen
        # into an unapproved stock route.
        raise AutoRouterRefusal(
            f"AutoRouter routing refused before any provider request: capability probe failed ({_scrub(exc)})"
        ) from exc
    if not registered:
        return model, runtime_kwargs, "stock"

    platform = session_platform(source)
    cfg = user_config if isinstance(user_config, dict) else {}
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    metadata = {
        "session_id": session_key[:256],
        "platform": platform[:64],
        "hermes_profile": _profile_name(),
        "requested_model": str(model_cfg.get("default") or model or "")[:199],
        "requested_provider": str(runtime_kwargs.get("provider") or model_cfg.get("provider") or "")[:64],
        "explicit_model": False,
    }
    try:
        route = apply_pre_inference_route_middleware(metadata, platform=platform)
    except AutoRouterRefusal:
        raise
    except Exception as exc:
        logger.warning("autorouter: middleware failed; refusing before any provider request", exc_info=True)
        raise AutoRouterRefusal(
            f"AutoRouter routing refused before any provider request: {_scrub(exc)}") from exc

    if route is None:
        raise AutoRouterRefusal(
            "AutoRouter routing refused before any provider request: "
            "the registered consumer returned no usable decision")
    status = getattr(route, "status", None)
    if status == "passthrough":
        # Provable opt-out: proceed exactly as if no consumer existed.
        return model, runtime_kwargs, "stock"
    if status != "route":
        reason = _scrub(getattr(route, "reason", "") or "no eligible route")
        raise AutoRouterRefusal(f"AutoRouter routing refused before any provider request: {reason}")
    provider = str(getattr(route, "provider", "") or "").strip().lower()
    routed_model = str(getattr(route, "model", "") or "").strip()
    if not provider or not routed_model:
        raise AutoRouterRefusal(
            "AutoRouter routing refused before any provider request: the consumer returned an unusable route")
    try:
        from gateway.run import _resolve_runtime_agent_kwargs_for_provider

        physical = _resolve_runtime_agent_kwargs_for_provider(provider, target_model=routed_model)
    except Exception as exc:
        logger.warning("autorouter: physical route %r could not be resolved; refusing", provider, exc_info=True)
        raise AutoRouterRefusal(
            f"AutoRouter routing refused before any provider request: {_scrub(exc)}") from exc
    runtime_kwargs = {**physical, "requested_provider": provider}
    logger.info("autorouter: turn boundary routed session=%s to provider=%s", session_key[:32], provider)
    return routed_model, runtime_kwargs, "ordinary"


def pre_turn_route(*, runner, source, session_key: str, user_config=None,
                   model: str, runtime_kwargs: dict):
    """Evaluate the AutoRouter route for this turn; fail closed on refusal.

    Returns ``(model, runtime_kwargs, applied)`` where ``applied`` is one of
    ``stock`` (consumer absent/opt-out — unchanged), ``explicit`` (explicit
    physical pick — untouched), ``virtual`` (explicit AutoRouter pick,
    re-resolved this turn), or ``ordinary`` (MODE B evaluation applied).
    Raises :class:`AutoRouterRefusal` before any provider request when the
    consumer refuses, fails or is unusable.
    """
    skey = str(session_key or "").strip()
    if not skey:
        return model, runtime_kwargs, "stock"

    # Explicit per-session selections first: physical picks untouched; an
    # explicit *virtual* pick was already re-resolved this turn inside
    # ``_resolve_session_agent_runtime`` (fail-closed) — leave it alone.
    override = _peek_override(runner, skey)
    if override:
        if _is_virtual_pick(override):
            return model, runtime_kwargs, "virtual"
        return model, runtime_kwargs, "explicit"

    # Following (launch-policy) sessions: the frozen route stands unless the
    # frozen selection is the AutoRouter virtual pick, which re-resolves per
    # turn through the same sanctioned consumer.
    mode, _frozen = _policy_selection_mode(runner, source)
    if mode == "autorouter":
        platform = session_platform(source)
        routed_model, routed_kwargs = _resolve_physical(session_key=skey, platform=platform)
        logger.info("autorouter: policy selection re-resolved for session=%s", skey[:32])
        return routed_model, routed_kwargs, "virtual"
    if mode is not None:
        # A policy session with any other frozen selection: untouched.
        return model, runtime_kwargs, "explicit"

    # MODE B — ordinary sessions.
    return _ordinary_route(
        runner=runner, source=source, session_key=skey,
        user_config=user_config, model=model, runtime_kwargs=runtime_kwargs)


def resolve_virtual_override(*, runner, source, session_key: str, override):
    """MODE A inside ``_resolve_session_agent_runtime``.

    An explicit AutoRouter virtual pick re-resolves to the session-consistent
    physical route on every turn through the sanctioned consumer; the frozen
    override is preserved for the UI/resume. Raises on refusal — the turn
    fails closed before any provider request is built (never widened to the
    stock route).
    """
    platform = session_platform(source)
    return _resolve_physical(session_key=str(session_key), platform=platform)
