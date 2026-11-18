"""Physical-provider identity enforcement for AutoRouter-validated routes (dispatch 142, P1).

The canonical resolver must return the route the decision authorized — not a
route that *claims* the decision while carrying a different physical provider
or another provider's endpoint. These tests pin the accept/refuse matrix:

* selected ``locala``, resolved ``locala`` → accept;
* selected named custom provider, resolved through its valid ``custom`` adapter
  and owned endpoint → accept;
* selected ``locala``, physical provider ``nous`` with ``requested_provider=locala``
  → refuse;
* selected provider X, endpoint owned by unrelated provider Y → refuse;
* selected model X, resolver returns model Y → refuse;
* a configured entry carrying someone else's endpoint → refuse;
* unexplained cross-provider substitutions through the custom adapter → refuse;
* no credential crosses to an unapproved endpoint: every refusal happens inside
  the validator, before any client is built.
"""
from __future__ import annotations

import pytest

from hermes_cli.autorouter_identity import (
    PhysicalIdentityError,
    validate_physical_runtime,
)

NOUS_URL = "https://inference-api.nousresearch.com/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"


@pytest.fixture
def scratch_home(tmp_path, monkeypatch):
    """A scratch HERMES_HOME with locala/localb named custom entries."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "providers:\n"
        "  locala:\n"
        "    base_url: http://127.0.0.1:8401/v1\n"
        "    api_key: sk-local-a-dummy-not-a-secret\n"
        "  localb:\n"
        "    base_url: http://127.0.0.1:8402/v1\n"
        "    api_key: sk-local-b-dummy-not-a-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def empty_home(tmp_path, monkeypatch):
    """A scratch HERMES_HOME with no provider entries at all."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _rt(provider, *, requested=None, base_url="http://127.0.0.1:8401/v1", model=None, api_key="sk-x"):
    runtime = {
        "provider": provider,
        "requested_provider": provider if requested is None else requested,
        "base_url": base_url,
        "api_key": api_key,
        "api_mode": "chat_completions",
    }
    if model is not None:
        runtime["model"] = model
    return runtime


# ---------------------------------------------------------------------------
# Accept cases
# ---------------------------------------------------------------------------

def test_direct_resolution_accepts(scratch_home):
    validate_physical_runtime(provider="locala", model="local-model-a",
                              runtime=_rt("locala", base_url="http://127.0.0.1:8401/v1"))


def test_direct_resolution_accepts_unconfigured_provider(empty_home):
    # No entry, no registry claim: the resolver says locala and nothing refutes it.
    validate_physical_runtime(provider="locala", model="m",
                              runtime=_rt("locala"))


def test_named_custom_provider_through_custom_adapter_accepts(scratch_home):
    validate_physical_runtime(provider="locala", model="local-model-a",
                              runtime=_rt("custom", requested="locala",
                                          base_url="http://127.0.0.1:8401/v1"))


def test_named_custom_provider_trailing_slash_endpoint_accepts(scratch_home):
    validate_physical_runtime(provider="locala", model="local-model-a",
                              runtime=_rt("custom", requested="locala",
                                          base_url="http://127.0.0.1:8401/v1/"))


def test_builtin_provider_accepts_own_canonical_endpoint(empty_home):
    validate_physical_runtime(provider="nous", model="m",
                              runtime=_rt("nous", requested="nous", base_url=NOUS_URL))


# ---------------------------------------------------------------------------
# Refuse cases — physical identity
# ---------------------------------------------------------------------------

def test_physical_provider_mismatch_with_claimed_request_refuses(scratch_home):
    """Selected locala, physical provider nous, requested_provider=locala: refuse."""
    with pytest.raises(PhysicalIdentityError):
        validate_physical_runtime(provider="locala", model="local-model-a",
                                  runtime=_rt("nous", requested="locala", base_url=NOUS_URL))


def test_cross_provider_substitution_refuses(scratch_home):
    with pytest.raises(PhysicalIdentityError):
        validate_physical_runtime(provider="locala", model="local-model-a",
                                  runtime=_rt("openrouter", requested="locala", base_url=OPENROUTER_URL))


def test_custom_adapter_with_foreign_request_refuses(scratch_home):
    """The custom adapter carrying a different requested provider: refuse."""
    with pytest.raises(PhysicalIdentityError):
        validate_physical_runtime(provider="locala", model="local-model-a",
                                  runtime=_rt("custom", requested="nous", base_url=NOUS_URL))


def test_custom_adapter_with_unrelated_providers_endpoint_refuses(scratch_home):
    """Selected locala (entry 8401), endpoint belongs to localb (8402): refuse."""
    with pytest.raises(PhysicalIdentityError):
        validate_physical_runtime(provider="locala", model="local-model-a",
                                  runtime=_rt("custom", requested="locala",
                                              base_url="http://127.0.0.1:8402/v1"))


def test_direct_resolution_carrying_unrelated_providers_endpoint_refuses(scratch_home):
    """Selected locala, resolved locala, endpoint owned by localb: refuse."""
    with pytest.raises(PhysicalIdentityError):
        validate_physical_runtime(provider="locala", model="local-model-a",
                                  runtime=_rt("locala", requested="locala",
                                              base_url="http://127.0.0.1:8402/v1"))


def test_builtin_provider_carrying_foreign_canonical_endpoint_refuses(empty_home):
    """Selected nous, resolved nous, but the endpoint is OpenRouter's: refuse."""
    with pytest.raises(PhysicalIdentityError):
        validate_physical_runtime(provider="nous", model="m",
                                  runtime=_rt("nous", requested="nous", base_url=OPENROUTER_URL))


def test_unconfigured_selected_provider_carrying_known_providers_endpoint_refuses(empty_home):
    """Selected locala (nowhere configured), endpoint belongs to nous: refuse."""
    with pytest.raises(PhysicalIdentityError):
        validate_physical_runtime(provider="locala", model="m",
                                  runtime=_rt("locala", requested="locala", base_url=NOUS_URL))


def test_unexplained_custom_adapter_substitution_refuses(empty_home):
    """Selected name resolves nowhere, custom adapter, unknown host: refuse."""
    with pytest.raises(PhysicalIdentityError):
        validate_physical_runtime(provider="mystery", model="m",
                                  runtime=_rt("custom", requested="mystery",
                                              base_url="http://192.0.2.1:1/v1"))


def test_model_mismatch_refuses(scratch_home):
    with pytest.raises(PhysicalIdentityError):
        validate_physical_runtime(provider="locala", model="local-model-a",
                                  runtime=_rt("locala", model="paid/model-x"))


def test_model_mismatch_through_custom_adapter_refuses(scratch_home):
    with pytest.raises(PhysicalIdentityError):
        validate_physical_runtime(provider="locala", model="local-model-a",
                                  runtime=_rt("custom", requested="locala", model="local-model-b"))


def test_non_dict_runtime_refuses():
    with pytest.raises(PhysicalIdentityError):
        validate_physical_runtime(provider="locala", model="m", runtime=None)
    with pytest.raises(PhysicalIdentityError):
        validate_physical_runtime(provider="locala", model="m", runtime="locala")


def test_missing_decision_provider_refuses():
    with pytest.raises(PhysicalIdentityError):
        validate_physical_runtime(provider="", model="m", runtime=_rt("locala"))
