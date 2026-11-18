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
  decision pins this turn's physical route; a never-managed session running
  ``passthrough`` is the provable opt-out and runs stock; anything else
  refuses the turn before any provider request.
* **Explicit physical pick** — never rewritten; the consumer does not run.
* **Policy (following) sessions** — honoured, or re-resolved per turn when
  the frozen selection is ``mode: autorouter``.

Managed routing state
---------------------
A session that AutoRouter has routed at least once is *managed*. The marker
lives in the conversation scope (``SessionState.conversation.autorouter_managed``)
and in the persistent session-store metadata (``autorouter_managed``), so it
survives a gateway restart. A session that was previously managed and whose
consumer disappears, whose authorization is revoked, or whose route evidence
becomes unusable is refused before any provider request — it is never widened
to the last pinned route or the stock default. Sessions that were never
managed keep stock semantics. Explicit per-session physical picks remain
authoritative regardless of the marker; an explicit pick clears it.

Refusals raise :class:`AutoRouterRefusal`, a ``RuntimeError`` the turn
runner converts into a failed turn with zero provider requests issued. No
virtual identifier (``autorouter`` / ``autorouter/auto-free``) ever reaches
an inference endpoint: the virtual pick is resolved to a physical route
before the agent is built every turn.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

VIRTUAL_PROVIDER = "autorouter"
MANAGED_METADATA_KEY = "autorouter_managed"
_MANAGED_MODES = ("ordinary", "virtual")


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
    """The session's model override, or ``None`` when there is none.

    Raises on a failed state lookup: an unreadable session must never be
    mistaken for an unpinned ordinary session.
    """
    peek = getattr(runner, "_peek_session_state", None)
    if not callable(peek):
        raise AutoRouterRefusal(
            "AutoRouter routing refused before any provider request: "
            "the session state store cannot be read")
    try:
        state = peek(session_key)
    except Exception as exc:
        raise AutoRouterRefusal(
            "AutoRouter routing refused before any provider request: "
            f"the session state cannot be read ({_scrub(exc)})") from exc
    conversation = getattr(state, "conversation", None) if state is not None else None
    if state is not None and conversation is None:
        raise AutoRouterRefusal(
            "AutoRouter routing refused before any provider request: "
            "the session state is unusable")
    return getattr(conversation, "model_override", None) if conversation is not None else None


def _is_virtual_pin(provider) -> bool:
    """The virtual AutoRouter provider is reserved to the consumer; no concrete model
    is served through it. ``provider == "autorouter"`` IS the virtual pin marker."""
    return str(provider or "").strip().lower() == VIRTUAL_PROVIDER


def _is_virtual_pick(override) -> bool:
    if not override:
        return False
    if _is_virtual_pin(override.get("provider")):
        return True
    try:
        from hermes_cli.autorouter_picker import is_autorouter_pick
        return bool(is_autorouter_pick(override.get("model"), override.get("provider")))
    except Exception:
        return False


def _profile_name() -> str | None:
    """The bound profile for this turn, or ``None`` when it cannot be established.

    Never falls back to the process-wide launch profile: under a multiplexed
    gateway the process environment belongs to the launch profile, and a
    routing decision (or the credentials it unlocks) may not cross profiles.
    """
    try:
        from hermes_cli import profiles

        name = profiles.current_profile_name()
        if isinstance(name, str) and name.strip():
            return name.strip()[:128]
    except Exception:
        return None
    return None


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

    profile = _profile_name()
    if profile is None:
        raise AutoRouterRefusal(
            "AutoRouter routing refused before any provider request: "
            "the bound profile for this session cannot be established")
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


def _validate_managed_marker(value) -> dict | None:
    """Normalize the persisted managed marker.

    Returns ``None`` only when the marker is genuinely *absent* (never written,
    key missing). Any other present value — booleans, numbers, strings of any
    kind (including empty), dicts without a known mode, and anything else — is
    managed evidence: a malformed marker that must fail closed, never a
    never-managed session.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        mode = str(value.get("mode") or "").strip().lower()
        if mode in _MANAGED_MODES:
            return {"mode": mode}
        return {"mode": "ordinary", "malformed": True}
    # Any other present value (bools, numbers, strings of any kind, lists,
    # foreign objects) is corrupt state: managed evidence, fail closed.
    return {"mode": "ordinary", "malformed": True}


def _read_managed_marker(runner, session_key: str):
    """In-memory conversation marker merged with the persistent store marker.

    Returns the normalized marker, or ``None`` only when BOTH copies are
    genuinely absent. Raises :class:`AutoRouterRefusal` when the persistent
    marker cannot be read — an unreadable managed state must never degrade
    into "never managed". A valid in-memory marker never conceals a corrupted
    authoritative persistent state (nor the reverse): a malformed copy anywhere
    fails closed as managed evidence.
    """
    state = None
    peek = getattr(runner, "_peek_session_state", None)
    if callable(peek):
        try:
            state = peek(session_key)
        except Exception as exc:
            raise AutoRouterRefusal(
                "AutoRouter routing refused before any provider request: "
                f"the session state cannot be read ({_scrub(exc)})") from exc
    in_memory = getattr(getattr(state, "conversation", None), "autorouter_managed", None) \
        if state is not None else None
    stored = None
    store = getattr(runner, "session_store", None)
    if store is not None:
        getter = getattr(store, "get_session_metadata", None)
        if callable(getter):
            try:
                stored = getter(session_key, MANAGED_METADATA_KEY)
            except Exception as exc:
                raise AutoRouterRefusal(
                    "AutoRouter routing refused before any provider request: "
                    f"the managed-routing state cannot be read ({_scrub(exc)})") from exc
    in_memory_marker = _validate_managed_marker(in_memory)
    stored_marker = _validate_managed_marker(stored)
    markers = [m for m in (in_memory_marker, stored_marker) if m is not None]
    if any(m.get("malformed") for m in markers):
        # Corruption anywhere is managed evidence. It must not be concealed by
        # a valid sibling copy (the persistent store is authoritative and the
        # in-memory copy may be its poisoned cache).
        return {"mode": "ordinary", "malformed": True}
    return markers[0] if markers else None


def _persist_managed_marker(runner, session_key: str, mode: str) -> bool:
    """Record that this session is AutoRouter-managed; True on success.

    When the marker cannot be persisted the caller cannot distinguish a
    restart from a never-managed session, so MODE B callers must fail closed.
    """
    mode = mode if mode in _MANAGED_MODES else "ordinary"
    marker = {"mode": mode, "since": time.time()}
    ok = True
    state_fn = getattr(runner, "_session_state", None)
    if callable(state_fn):
        try:
            conversation = state_fn(session_key).conversation
            conversation.autorouter_managed = marker
        except Exception:
            ok = False
    store = getattr(runner, "session_store", None)
    setter = getattr(store, "set_session_metadata", None) if store is not None else None
    if callable(setter):
        try:
            result = setter(session_key, MANAGED_METADATA_KEY, marker)
            if result is False:
                ok = False
        except Exception:
            ok = False
    else:
        ok = False
    return ok


def _clear_managed_marker(runner, session_key: str) -> None:
    """An explicit per-session physical pick is genuinely the operator's call:
    forget the managed marker (best effort)."""
    state_fn = getattr(runner, "_peek_session_state", None)
    if callable(state_fn):
        try:
            state = state_fn(session_key)
            if state is not None and getattr(state, "conversation", None) is not None:
                state.conversation.autorouter_managed = None
        except Exception:
            logger.debug("autorouter: failed to clear in-memory managed marker", exc_info=True)
    store = getattr(runner, "session_store", None)
    setter = getattr(store, "set_session_metadata", None) if store is not None else None
    if callable(setter):
        try:
            setter(session_key, MANAGED_METADATA_KEY, None)
        except Exception:
            logger.debug("autorouter: failed to clear persisted managed marker", exc_info=True)


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
        if mode == "autorouter":
            return "autorouter", frozen
        # A launch policy may also carry the virtual pin directly: the Desktop
        # freezes ``provider: autorouter`` with the stock model untouched and no
        # selection marker. That IS the MODE A pin regardless of the model id.
        try:
            if _is_virtual_pin(block.get("provider") if isinstance(block, dict) else None):
                return "autorouter", frozen
        except Exception:
            pass
        return (mode or "frozen"), frozen
    except Exception:
        logger.warning("autorouter: policy selection probe failed; leaving the frozen route alone", exc_info=True)
        return "frozen", None


def _ordinary_route(*, runner, source, session_key: str, user_config, model, runtime_kwargs,
                    managed: bool):
    """MODE B: evaluate the ordinary session at the turn boundary.

    Returns (model, runtime_kwargs, applied). ``applied`` is ``stock`` for the
    provable opt-out (passthrough or no consumer — never-managed sessions
    only), ``ordinary`` for a freshly resolved route. Raises
    :class:`AutoRouterRefusal` before any provider request otherwise.
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
        if managed:
            raise AutoRouterRefusal(
                "AutoRouter routing refused before any provider request: "
                "the session is AutoRouter-managed but the routing consumer is no "
                "longer registered on this host")
        return model, runtime_kwargs, "stock"

    profile = _profile_name()
    if profile is None:
        raise AutoRouterRefusal(
            "AutoRouter routing refused before any provider request: "
            "the bound profile for this session cannot be established")

    platform = session_platform(source)
    cfg = user_config if isinstance(user_config, dict) else {}
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    metadata = {
        "session_id": session_key[:256],
        "platform": platform[:64],
        "hermes_profile": profile,
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
        if managed:
            # A managed session has no provable opt-out: keeping the pinned
            # route or falling back to the stock default would launder
            # revoked authorization.
            raise AutoRouterRefusal(
                "AutoRouter routing refused before any provider request: "
                "the session is AutoRouter-managed and the consumer declined to "
                "route this turn (passthrough)")
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
    if not isinstance(physical, dict):
        raise AutoRouterRefusal(
            "AutoRouter routing refused before any provider request: the physical runtime did not resolve")
    try:
        from hermes_cli.autorouter_identity import PhysicalIdentityError, validate_physical_runtime

        validate_physical_runtime(provider=provider, model=routed_model, runtime=physical)
    except PhysicalIdentityError as exc:
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
        # The session cannot be located, so its managed state cannot be
        # established — stock inference would launder it. Refuse.
        raise AutoRouterRefusal(
            "AutoRouter routing refused before any provider request: no session key")

    # Explicit per-session selections first: physical picks untouched; an
    # explicit *virtual* pick was already re-resolved this turn inside
    # ``_resolve_session_agent_runtime`` (fail-closed) — leave it alone.
    override = _peek_override(runner, skey)
    if override:
        if _is_virtual_pick(override):
            _persist_managed_marker(runner, skey, "virtual")
            return model, runtime_kwargs, "virtual"
        # Genuine operator pick: explicit picks are authoritative. Forget any
        # stale managed marker (best effort).
        _clear_managed_marker(runner, skey)
        return model, runtime_kwargs, "explicit"

    # Following (launch-policy) sessions: the frozen route stands unless the
    # frozen selection is the AutoRouter virtual pick, which re-resolves per
    # turn through the same sanctioned consumer.
    mode, _frozen = _policy_selection_mode(runner, source)
    if mode == "autorouter":
        platform = session_platform(source)
        routed_model, routed_kwargs = _resolve_physical(session_key=skey, platform=platform)
        _persist_managed_marker(runner, skey, "virtual")
        logger.info("autorouter: policy selection re-resolved for session=%s", skey[:32])
        return routed_model, routed_kwargs, "virtual"
    if mode is not None:
        # A policy session with any other frozen selection: untouched.
        return model, runtime_kwargs, "explicit"

    # MODE B — ordinary sessions. Managed-ness decides whether the absence of
    # a provable route is stock semantics or a fail-closed refusal.
    managed = _read_managed_marker(runner, skey) is not None
    routed_model, routed_kwargs, applied = _ordinary_route(
        runner=runner, source=source, session_key=skey,
        user_config=user_config, model=model, runtime_kwargs=runtime_kwargs,
        managed=managed)
    if applied == "ordinary":
        if not _persist_managed_marker(runner, skey, "ordinary"):
            raise AutoRouterRefusal(
                "AutoRouter routing refused before any provider request: "
                "the managed-routing state cannot be recorded for this session")
    return routed_model, routed_kwargs, applied


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
