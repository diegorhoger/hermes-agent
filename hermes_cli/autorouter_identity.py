"""Physical-execution identity checks for AutoRouter-validated routes.

Shared by the messaging gateway (``gateway/autorouter_turn.py``) and the TUI
gateway (``tui_gateway/autorouter_turn.py``). Given the route an authorized
consumer selected (provider + model) and the runtime the canonical resolver
built for it, prove that the runtime **is** that route before any provider
request is issued:

* the physical provider (``runtime["provider"]``) matches the decision —
  directly, or — for named custom providers — through the canonical
  ``custom`` adapter with an endpoint the selected entry owns;
* a ``requested_provider`` that merely *claims* the decision while carrying
  a different physical provider, or an endpoint owned by another provider,
  is refused (a claim is not an identity);
* the endpoint is not another known provider's canonical endpoint, and when
  the decision names a configured ``providers:``/``custom_providers:`` entry,
  it is that entry's own endpoint;
* a resolved model different from the decision is refused (no authorized
  canonical model mapping exists);
* every refusal happens before any client is built or request is issued, so
  no credential crosses to an unapproved endpoint.

Named custom providers resolve to the ``custom`` adapter by design (the
resolver reports ``provider="custom"`` with ``requested_provider=<name>`` and
the entry's owned endpoint); bare string equality is therefore not the test.
The tests are: provider-name agreement OR the canonical custom-adapter path,
plus endpoint ownership in both paths.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PhysicalIdentityError(ValueError):
    """The resolved runtime is not the validated route's physical identity."""


def _clean(value) -> str:
    return str(value or "").strip()


def _norm(value) -> str:
    return _clean(value).lower()


def _route_key(value) -> str:
    """An endpoint compared for identity (scheme/host fold case, path does not)."""
    from hermes_cli.route_identity import normalize_route_base_url

    return normalize_route_base_url(_clean(value))


def _load_config() -> dict:
    """The merged configuration, or a fail-closed refusal when unreadable."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    except Exception as exc:
        raise PhysicalIdentityError(
            "the configuration cannot be read, so endpoint ownership cannot be established") from exc
    return config if isinstance(config, dict) else {}


def _named_entry_url(provider: str, config: dict) -> str:
    """The configured endpoint of the ``providers:``/``custom_providers:`` entry named by
    *provider*, or ``""`` when it names no entry (a built-in, or an unknown name)."""
    try:
        from hermes_cli.runtime_provider_custom import _get_named_custom_provider

        entry = _get_named_custom_provider(provider, config=config, resolve_credentials=False)
    except Exception:
        # Identity cannot be established for a configured name: fail closed.
        raise PhysicalIdentityError(
            "the configured endpoint of the selected provider cannot be established")
    if not isinstance(entry, dict):
        return ""
    return _clean(entry.get("base_url") or entry.get("api") or entry.get("url") or "")


def _is_custom_class(resolved: str) -> bool:
    """True for the bare ``custom`` adapter and for aliases that resolve to it
    (ollama, vllm, llamacpp, …) — the adapters named custom providers travel through."""
    if resolved == "custom":
        return True
    try:
        from hermes_cli.runtime_provider import _resolves_to_custom

        return bool(_resolves_to_custom(resolved))
    except Exception:
        return False


def _known_provider(provider: str) -> bool:
    """True when *provider* is a registry-known provider (id or alias)."""
    try:
        from hermes_cli.providers import get_provider

        return get_provider(provider, allow_network=False) is not None
    except Exception:
        return False


def _ownership(provider: str, base_url: str, config: dict):
    """``provider_owns_route`` with a fail-closed unreadable-config boundary."""
    try:
        from hermes_cli.route_identity import provider_owns_route

        return provider_owns_route(provider, base_url, config)
    except PhysicalIdentityError:
        raise
    except Exception as exc:
        raise PhysicalIdentityError(
            "endpoint ownership cannot be established for the selected provider") from exc


def _known_owner_of(base_url: str) -> str:
    """The provider a models.dev-known endpoint belongs to, or "" when no known
    provider claims the host. Used to refuse an endpoint owned by an unrelated
    provider even when the selected name resolves nowhere."""
    try:
        from agent.model_metadata import _infer_provider_from_url

        return _norm(_infer_provider_from_url(base_url))
    except Exception:
        return ""


def _enforce_owned_endpoint(provider: str, base_url: str, config: dict, *, strict: bool) -> None:
    """Refuse an endpoint the selected provider does not own.

    ``strict`` is the named-custom path: the selected name resolves through the
    ``custom`` adapter, so the endpoint MUST be the entry's (or a registry-known
    provider's own). The direct path accepts a provider whose ownership is
    unknown (a user proxy) but refuses one provably owned by someone else.
    """
    entry_url = _named_entry_url(provider, config)
    if entry_url:
        if _route_key(entry_url) != _route_key(base_url):
            raise PhysicalIdentityError(
                "the resolved endpoint is not the configured endpoint of the selected provider")
        return
    inferred = _known_owner_of(base_url)
    if inferred and inferred != provider:
        from hermes_cli.providers import normalize_provider

        try:
            same = normalize_provider(inferred) == normalize_provider(provider)
        except Exception:
            same = False
        if not same:
            raise PhysicalIdentityError(
                f"the resolved endpoint belongs to unrelated provider {inferred!r}")
    owned = _ownership(provider, base_url, config)
    if owned is False:
        raise PhysicalIdentityError(
            "the resolved endpoint belongs to an unrelated provider")
    if strict and owned is not True:
        # Neither an entry, nor the registry, nor a claimed host explains this
        # substitution — an unexplained cross-provider substitution is refused.
        raise PhysicalIdentityError(
            "the resolved custom-provider route carries an endpoint that cannot be "
            "attributed to the selected provider")
    if strict and owned is True and not _known_provider(provider):
        raise PhysicalIdentityError(
            "the resolved custom-provider route carries an endpoint that cannot be "
            "attributed to the selected provider")


def validate_physical_runtime(*, provider: str, model: str, runtime, config=None) -> None:
    """Prove *runtime* is the physical route the decision authorizes.

    Raises :class:`PhysicalIdentityError` — before any client is built or
    request is issued — on any unexplained substitution. Returns ``None`` on
    success.
    """
    if not isinstance(runtime, dict):
        raise PhysicalIdentityError("the physical runtime did not resolve to a route")
    decision_provider = _norm(provider)
    decision_model = _clean(model)
    if not decision_provider:
        raise PhysicalIdentityError("no provider decision to validate the runtime against")
    resolved = _norm(runtime.get("provider"))
    requested = _norm(runtime.get("requested_provider"))
    base_url = _clean(runtime.get("base_url"))
    runtime_model = _clean(runtime.get("model"))
    if runtime_model and runtime_model != decision_model:
        # No authorized canonical model mapping exists: any drift is a refusal.
        raise PhysicalIdentityError(
            f"the resolved physical route (model {runtime_model}) disagrees with the routing decision")

    cfg = config if isinstance(config, dict) else _load_config()

    if resolved and resolved == decision_provider:
        # Direct resolution (built-in providers, registry aliases). The physical
        # identity is the resolved provider itself; the endpoint must not belong
        # to someone else's route.
        _enforce_owned_endpoint(decision_provider, base_url, cfg, strict=False)
        return
    if resolved and _is_custom_class(resolved) and requested == decision_provider:
        # Named custom provider through the canonical ``custom`` adapter: the
        # request names the entry, the wire carries the entry's owned endpoint.
        _enforce_owned_endpoint(decision_provider, base_url, cfg, strict=True)
        return
    raise PhysicalIdentityError(
        f"the resolved physical route ({resolved or 'unknown'}) disagrees with the routing decision")
