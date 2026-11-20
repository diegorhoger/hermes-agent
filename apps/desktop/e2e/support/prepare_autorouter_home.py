#!/usr/bin/env python3
"""Prepare an isolated HERMES_HOME for Desktop AutoRouter E2E against a localhost mock.

Installs (directory-install) the pinned AutoRouter plugin and a mockinfra
model-provider plugin, seeds AutoRouter's catalog/state with verified-free
evidence for mockinfra models, and writes Hermes config so the real
pre_inference_route consumer can route to the mock provider.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import time
from pathlib import Path


def _copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns(
            ".git", ".env*", "__pycache__", ".pytest_cache", "*.db", "*.sqlite*",
            "*.log", "state", "tests", ".mypy_cache", ".ruff_cache",
        ),
    )


def install_autorouter_plugin(home: Path, pinned_snapshot: Path) -> Path:
    plugin_dir = home / "plugins" / "autorouter"
    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    _copytree(pinned_snapshot, plugin_dir)
    origin = {
        "source": "local-pinned-snapshot",
        "pinned_sha": "cdaef496501eadccdde1ac250ef1a18d5b1fd402",
        "installed_at": time.time(),
    }
    (plugin_dir / ".hermes-install-origin.json").write_text(json.dumps(origin, indent=2), encoding="utf-8")
    return plugin_dir


def install_mockinfra_provider(home: Path, base_url: str) -> Path:
    provider_dir = home / "plugins" / "model-providers" / "mockinfra"
    provider_dir.mkdir(parents=True, exist_ok=True)
    (provider_dir / "plugin.yaml").write_text(
        "name: mockinfra-provider\nkind: model-provider\nversion: 1.0.0\n"
        "description: disposable localhost fixture for desktop E2E\nauthor: autorouter acceptance fixture\n",
        encoding="utf-8",
    )
    (provider_dir / "__init__.py").write_text(
        f"""from providers import register_provider
from providers.base import ProviderProfile

class MockInfraProfile(ProviderProfile):
    def fetch_models(self, **kwargs):
        return [
            "mockinfra-baseline-model",
            "mockinfra-free-model",
            "mockinfra-no-tools-model",
            "mockinfra-unknown-cost-model",
        ]

mockinfra = MockInfraProfile(
    name="mockinfra",
    aliases=("mockinfra-alias",),
    env_vars=("MOCKINFRA_API_KEY",),
    display_name="MockInfra",
    description="disposable localhost fixture for desktop E2E",
    fallback_models=("mockinfra-baseline-model", "mockinfra-free-model", "mockinfra-no-tools-model", "mockinfra-unknown-cost-model"),
    base_url="{base_url}",
    api_mode="chat_completions",
    auth_type="api_key",
    supports_model_listing=True,
    supports_health_check=False,
)
register_provider(mockinfra)
""",
        encoding="utf-8",
    )
    return provider_dir


def seed_autorouter_store(home: Path, plugin_dir: Path) -> dict:
    pkg_name = "_autorouter_seed_pkg"
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    assert spec and spec.loader
    pkg = importlib.util.module_from_spec(spec)
    pkg.__package__ = pkg_name
    sys.modules[pkg_name] = pkg
    spec.loader.exec_module(pkg)
    state_mod = importlib.import_module(f"{pkg_name}.autorouter_core.storage.state")

    store_dir = home / "plugin-data" / "autorouter"
    store_dir.mkdir(parents=True, exist_ok=True)
    store = state_mod.StateStore(store_dir)

    FRESH = 1_700_000_000.0
    EXPIRY = 1_900_000_000.0

    def seed(provider, model, ctx, tools, tier, catalog_tier=None):
        store.upsert_catalog(provider, model, ctx, {"tools": tools}, catalog_tier or tier)
        evidence = {
            "provider": provider,
            "model": model,
            "field_evidence": {
                field: {"metadata_source": "provider", "retrieved_at": FRESH, "expires_at": EXPIRY}
                for field in ("context_window", "tools", "cost_tier")
            },
            "context_window": ctx,
            "tools": tools,
            "cost_tier": tier,
            "source": "provider",
        }
        store.upsert_capability(provider, model, json.dumps(evidence), retrieved_at=FRESH, expires_at=EXPIRY)

    # baseline is a normal configured model (not autorouter-routed by default allowlist)
    seed("mockinfra", "mockinfra-baseline-model", 131072, "supported", "paid", catalog_tier="paid")
    seed("mockinfra", "mockinfra-free-model", 131072, "supported", "free")
    seed("mockinfra", "mockinfra-no-tools-model", 131072, "unsupported", "free")
    seed("mockinfra", "mockinfra-unknown-cost-model", 131072, "supported", "unknown")
    return {"store_dir": str(store_dir), "seeded": ["mockinfra/mockinfra-baseline-model", "mockinfra/mockinfra-free-model"]}


def write_config(home: Path, base_url: str, *, enable_chat_router: bool, enable_native_routing: bool,
                 default_model: str, default_provider: str) -> Path:
    path = home / "config.yaml"
    text = f"""model:
  provider: {default_provider}
  default: {default_model}
  base_url: {base_url}
  api_key: fixture-mockinfra-key-synthetic
  context_length: 131072
providers:
  mockinfra:
    base_url: {base_url}
    api_key: fixture-mockinfra-key-synthetic
    api_mode: chat_completions
plugins:
  enabled:
    - autorouter
  entries:
    autorouter:
      settings:
        enabled: true
        enable_chat_router: {'true' if enable_chat_router else 'false'}
        enable_native_routing: {'true' if enable_native_routing else 'false'}
        providers:
          - mockinfra
        free_only: true
        policy: auto
        min_context: 8192
        requires_tools: true
        model_preference:
          mode: allowlist
          include:
            - mockinfra/mockinfra-free-model
          exclude: []
"""
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--pinned-snapshot", required=True)
    ap.add_argument("--mock-url", required=True)
    ap.add_argument("--enable-chat-router", action="store_true")
    ap.add_argument("--enable-native-routing", action="store_true")
    ap.add_argument("--default-model", default="mockinfra-free-model")
    ap.add_argument("--default-provider", default="mockinfra")
    ap.add_argument("--seed-only", action="store_true")
    args = ap.parse_args()

    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    pinned = Path(args.pinned_snapshot).resolve()
    mock_url = args.mock_url.rstrip("/")
    base_url = mock_url if mock_url.endswith("/v1") else mock_url + "/v1"

    plugin_dir = home / "plugins" / "autorouter"
    if not args.seed_only:
        plugin_dir = install_autorouter_plugin(home, pinned)
        install_mockinfra_provider(home, base_url)

    seed_info = seed_autorouter_store(home, plugin_dir)
    cfg_path = write_config(home, base_url, enable_chat_router=args.enable_chat_router,
                            enable_native_routing=args.enable_native_routing,
                            default_model=args.default_model, default_provider=args.default_provider)
    (home / "models_dev_cache.json").write_text(json.dumps({"mockinfra": {"models": {
        "mockinfra-baseline-model": {"limit": {"context": 131072}, "tool_call": True, "cost": {"input": 1.0, "output": 1.0}},
        "mockinfra-free-model": {"limit": {"context": 131072}, "tool_call": True, "cost": {"input": 0.0, "output": 0.0}},
        "mockinfra-no-tools-model": {"limit": {"context": 131072}, "tool_call": False, "cost": {"input": 0.0, "output": 0.0}},
        "mockinfra-unknown-cost-model": {"limit": {"context": 131072}, "tool_call": True, "cost": {}},
    }}}), encoding="utf-8")

    print(json.dumps({
        "ok": True,
        "home": str(home),
        "plugin_dir": str(plugin_dir),
        "config": str(cfg_path),
        "seed": seed_info,
        "base_url": base_url,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
