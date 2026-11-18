#!/bin/bash
# Verify the isolated local AutoRouter development environment (dispatch 140).
#
# Read-only. Checks the checkout, the isolated profile, the plugin registration,
# the synthetic providers' health, and (report-only) the Ollama runtime.
# Writes $RUNTIME/verify-<UTC>.json. Exits non-zero on any hard failure.
#
# Usage: tools/local-dev/verify-local.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEV_ROOT="${HERMES_DEV_ROOT:-$(dirname "$REPO")}"
HOME_DIR="${HERMES_DEV_HOME:-$DEV_ROOT/hermes-home}"
RUNTIME="${HERMES_DEV_RUNTIME:-$DEV_ROOT/runtime}"
LOGDIR="$RUNTIME/provider-logs"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
mkdir -p "$RUNTIME"

fails=()
checks=()
ok()   { checks+=("$1: ok"); say "  ok   $1"; }
bad()  { checks+=("$1: FAIL ($2)"); printf '  FAIL %s — %s\n' "$1" "$2" >&2; fails+=("$1"); }
warn() { checks+=("$1: warn ($2)"); printf '  warn %s — %s\n' "$1" "$2" >&2; }
say()  { printf '%s\n' "$*"; }

say "host checkout"
if out="$(git -C "$REPO" rev-parse HEAD 2>&1)"; then
  HERMES_SHA="$out"
  BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  ok "git checkout (branch=$BRANCH sha=$HERMES_SHA)"
else
  bad "git checkout" "$out"
  HERMES_SHA=unknown BRANCH=unknown
fi

if git -C "$REPO" diff --quiet && git -C "$REPO" diff --cached --quiet; then
  ok "working tree clean"
else
  warn "working tree dirty" "uncommitted changes present"
fi

[ -x "$REPO/.venv/bin/python" ] && ok "venv python" || bad "venv python" "$REPO/.venv/bin/python missing"

say "isolated profile"
if [ ! -f "$HOME_DIR/config.yaml" ]; then
  bad "profile" "$HOME_DIR/config.yaml missing (run setup-local-home.py)"
else
  case "$(cd "$HOME_DIR" && pwd)" in
    "$HOME"/.hermes* ) bad "profile isolation" "isolated home lives under ~/.hermes" ;;
    * ) ok "profile isolation ($HOME_DIR)" ;;
  esac
  if grep -q 'sk-[A-Za-z0-9-]*\(dummy\|not-a-secret\)' "$HOME_DIR/config.yaml"; then
    ok "loopback-only credentials"
  else
    warn "credentials" "config.yaml keys are not visibly dummy — inspect manually"
  fi
fi

say "autorouter plugin"
if HERMES_HOME="$HOME_DIR" PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" "$REPO/.venv/bin/python" - >/tmp/verify-plugin.out 2>/tmp/verify-plugin.err <<'PY'
from hermes_cli.plugins import get_plugin_manager, has_middleware
from hermes_cli.middleware import PRE_INFERENCE_ROUTE_MIDDLEWARE
m = get_plugin_manager()
m.discover_and_load()
assert has_middleware(PRE_INFERENCE_ROUTE_MIDDLEWARE), "pre_inference_route consumer not registered"
print("consumer-registered")
PY
then
  ok "pre_inference_route consumer registered"
else
  bad "plugin" "$(tail -1 /tmp/verify-plugin.err 2>/dev/null)"
fi

say "synthetic providers"
for pair in "locala:8401:local-model-a" "localb:8402:local-model-b"; do
  name="${pair%%:*}"; rest="${pair#*:}"; port="${rest%%:*}"; model="${rest##*:}"
  if curl -fsS -m 3 "http://127.0.0.1:${port}/v1/models" 2>/dev/null | grep -q "$model"; then
    ok "$name healthy (:$port)"
  else
    bad "$name" "no healthy /v1/models on :$port"
  fi
done

say "ollama (shared; report-only)"
ollama="unknown"
if curl -fsS -m 3 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  if curl -fsS -m 3 "$OLLAMA_URL/api/tags" | grep -q 'ornith-1.5:9b'; then
    ollama="ok (ornith-1.5:9b present)"; ok "ollama $ollama"
  else
    ollama="degraded (model missing)"; warn "ollama" "$ollama"
  fi
else
  ollama="unavailable"; warn "ollama" "$ollama — not restarting the shared service"
fi

python3 - "$RUNTIME" "$HERMES_SHA" "$BRANCH" "$ollama" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${#fails[@]}" <<'PY'
import json, sys, time
runtime, sha, branch, ollama, now, fail_count = sys.argv[1:7]
doc = {"verified_at": now, "epoch": time.time(), "hermes_sha": sha, "branch": branch,
       "ollama": ollama, "failed_checks": int(fail_count)}
open(f"{runtime}/verify-latest.json", "w", encoding="utf-8").write(json.dumps(doc, indent=2) + "\n")
PY

say ""
if [ "${#fails[@]}" -gt 0 ]; then
  say "verify-local: FAIL (${fails[*]})"
  exit 1
fi
say "verify-local: PASS"
