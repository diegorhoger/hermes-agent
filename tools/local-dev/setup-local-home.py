#!/usr/bin/env python3
"""Generate the isolated local-development Hermes profile for the AutoRouter harness.

Creates ``$HERMES_DEV_ROOT/hermes-home`` (default: the checkout's sibling
``hermes-home`` directory) with loopback-only providers and the AutoRouter
plugin symlinked from the sibling ``autorouter`` checkout. Never touches the
operator's live ``~/.hermes`` profile. Contains zero real credentials: every
API key is a local dummy, and no real account, quota or token store exists in
this profile.

Usage:
    python3 tools/local-dev/setup-local-home.py [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEV_ROOT = Path(os.environ.get("HERMES_DEV_ROOT") or REPO.parent)
HOME = Path(os.environ.get("HERMES_DEV_HOME") or DEV_ROOT / "hermes-home")
AUTOROUTER = Path(os.environ.get("AUTOROUTER_SRC") or DEV_ROOT / "autorouter")
RUNTIME = Path(os.environ.get("HERMES_DEV_RUNTIME") or DEV_ROOT / "runtime")

CONFIG = """\
model:
  provider: ollama
  default: ornith-1.5:9b
  base_url: http://127.0.0.1:11434/v1
providers:
  ollama:
    base_url: http://127.0.0.1:11434/v1
    api_key: sk-ollama-local-dummy-not-a-secret
  locala:
    base_url: http://127.0.0.1:8401/v1
    api_key: sk-local-a-dummy-not-a-secret
  localb:
    base_url: http://127.0.0.1:8402/v1
    api_key: sk-local-b-dummy-not-a-secret
plugins:
  enabled: [autorouter]
  entries:
    autorouter:
      settings:
        enabled: true
        enable_chat_router: true
        enable_native_routing: true
        free_only: true
        providers: [ollama, locala, localb]
        requires_tools: false
        min_context: 2048
        cooldown_seconds: 2
        policy: auto
        model_preference:
          mode: allowlist
          include:
            - ollama/ornith-1.5:9b
            - locala/local-model-a
            - localb/local-model-b
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="regenerate config.yaml and the plugin link (keeps sessions/state)")
    args = parser.parse_args()

    if HOME.resolve() == (Path.home() / ".hermes").resolve():
        print("refusing: the isolated home must not be ~/.hermes", file=sys.stderr)
        return 2
    HOME.mkdir(parents=True, exist_ok=True)
    (RUNTIME / "provider-logs").mkdir(parents=True, exist_ok=True)

    config_path = HOME / "config.yaml"
    if config_path.exists() and not args.force:
        print(f"config.yaml already exists at {config_path} (use --force to regenerate)")
    else:
        config_path.write_text(CONFIG, encoding="utf-8")
        print(f"wrote {config_path}")

    if not (AUTOROUTER / "plugin.yaml").exists():
        print(f"AutoRouter checkout not found at {AUTOROUTER}", file=sys.stderr)
        return 2

    plugins_dir = HOME / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    link = plugins_dir / "autorouter"
    if link.is_symlink() or link.exists():
        if link.is_symlink() and link.resolve() == AUTOROUTER.resolve():
            print(f"plugin link already points at {AUTOROUTER}")
        elif args.force:
            if link.is_symlink() or link.is_file():
                link.unlink()
            else:
                shutil.rmtree(link)
            link.symlink_to(AUTOROUTER.resolve())
            print(f"relinked plugin -> {AUTOROUTER}")
        else:
            print(f"plugin dir exists at {link} and does not point at {AUTOROUTER}; "
                  "refusing to overwrite (use --force)", file=sys.stderr)
            return 2
    else:
        link.symlink_to(AUTOROUTER.resolve())
        print(f"linked plugin -> {AUTOROUTER}")

    meta = {
        "hermes_home": str(HOME),
        "autorouter_src": str(AUTOROUTER),
        "autorouter_sha": _git_sha(AUTOROUTER),
        "hermes_sha": _git_sha(REPO),
        "note": "isolated development profile; loopback providers only; no real credentials",
    }
    (RUNTIME / "profile.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {RUNTIME / 'profile.json'}")
    return 0


def _git_sha(path: Path) -> str:
    import subprocess

    try:
        return subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
