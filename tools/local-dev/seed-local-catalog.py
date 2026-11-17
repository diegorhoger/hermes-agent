#!/usr/bin/env python3
"""Seed the isolated AutoRouter store with verified-free evidence for local models only.

DEVELOPMENT-ONLY: this writes synthetic ``free`` price/capability evidence for
the three loopback routes of the isolated environment (local Ollama runtime +
two synthetic OpenAI-compatible endpoints). It authorizes NOTHING beyond this
machine's local development profile, and none of it is real account
entitlement evidence. Do not run it against any other home.

Usage: python3 tools/local-dev/seed-local-catalog.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEV_ROOT = Path(os.environ.get("HERMES_DEV_ROOT") or REPO.parent)
HOME = Path(os.environ.get("HERMES_DEV_HOME") or DEV_ROOT / "hermes-home")
AUTOROUTER = Path(os.environ.get("AUTOROUTER_SRC") or DEV_ROOT / "autorouter")
STORE = HOME / "plugin-data" / "autorouter"

if HOME.resolve() == (Path.home() / ".hermes").resolve():
    sys.exit("refusing: isolated home must not be ~/.hermes")

sys.path.insert(0, str(AUTOROUTER))
from autorouter_core.storage.state import StateStore

STORE.mkdir(parents=True, exist_ok=True)
store = StateStore(STORE)
now = time.time()


def seed(provider, model, context_length, tools):
    store.upsert_catalog(provider, model, context_length, {"tools": tools}, "free")
    cap = {
        "provider": provider,
        "model": model,
        "context_window": context_length,
        "tools": "supported" if tools is True else ("unsupported" if tools is False else "unknown"),
        "cost_tier": "free",
        "metadata_source": "local-isolated-dev-environment",
        "retrieved_at": now,
        "expires_at": now + 3650 * 24 * 3600,
    }
    store.upsert_capability(provider, model, json.dumps(cap),
                            metadata_source=cap["metadata_source"],
                            retrieved_at=now, expires_at=cap["expires_at"])


seed("ollama", "ornith-1.5:9b", 131072, True)
seed("locala", "local-model-a", 32768, True)
seed("localb", "local-model-b", 32768, True)

print("catalog:", store.get_catalog())
for provider, model in (("ollama", "ornith-1.5:9b"), ("locala", "local-model-a"), ("localb", "local-model-b")):
    row = store.get_capability(provider, model)
    print(provider, model, "capability row present:", bool(row))
