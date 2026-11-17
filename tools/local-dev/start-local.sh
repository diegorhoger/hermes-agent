#!/bin/bash
# Start the isolated local AutoRouter development environment (dispatch 140).
#
# Starts the two synthetic loopback providers (Deterministic, zero cost, zero
# credentials) with PID files, health-checks them, links the isolated profile,
# and reports (never modifies) the shared Ollama runtime. Refuses to start
# when a port is held by an unrecognised process.
#
# The shared Ollama service is NOT touched: no restart, no kickstart, no
# config change. If it is unhealthy, start continues with a DEGRADED verdict.
#
# Usage: tools/local-dev/start-local.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEV_ROOT="${HERMES_DEV_ROOT:-$(dirname "$REPO")}"
HOME_DIR="${HERMES_DEV_HOME:-$DEV_ROOT/hermes-home}"
RUNTIME="${HERMES_DEV_RUNTIME:-$DEV_ROOT/runtime}"
LOGDIR="$RUNTIME/provider-logs"
PIDDIR="$RUNTIME/pids"
mkdir -p "$LOGDIR" "$PIDDIR"

say()  { printf '%s\n' "$*"; }
die()  { printf 'start-local: ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf 'start-local: WARN: %s\n' "$*" >&2; }

PORT_A=8401
PORT_B=8402
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
SPAWN_PID_WAIT_S="${SPAWN_PID_WAIT_S:-10}"

[ -x "$REPO/.venv/bin/python" ] || die "venv python not found at $REPO/.venv/bin/python — build the checkout first"
[ -f "$REPO/pyproject.toml" ] || die "expected a Hermes checkout at $REPO"

branch="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
sha="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
say "host checkout: branch=$branch sha=$sha"
say "isolated home: $HOME_DIR"

# --- isolated profile must exist and be isolated -------------------------------
if [ ! -f "$HOME_DIR/config.yaml" ]; then
  say "generating isolated profile ..."
  python3 "$HERE/setup-local-home.py"
fi
case "$(cd "$HOME_DIR" && pwd)" in
  "$HOME"/.hermes* ) die "isolated home must not live under ~/.hermes" ;;
esac
if grep -q 'sk-' "$HOME_DIR/config.yaml" 2>/dev/null && ! grep -q 'dummy' "$HOME_DIR/config.yaml"; then
  die "config.yaml contains a non-dummy API key — refusing (this profile is loopback-only)"
fi

# --- ownership-aware port check ------------------------------------------------
pid_for_port() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1; }
cmdline_of()     { ps -p "$1" -o command= 2>/dev/null || true; }

check_port() {  # $1=port $2=name
  local port="$1" name="$2" pid_file="$PIDDIR/$2.pid" pid_port
  pid_port="$(pid_for_port "$port" || true)"
  if [ -z "$pid_port" ]; then
    return 0 # free
  fi
  if [ -f "$pid_file" ]; then
    local owner; owner="$(cat "$pid_file" 2>/dev/null || true)"
    if [ "$owner" = "$pid_port" ] && cmdline_of "$pid_port" | grep -q "synth_provider.py $name $port"; then
      say "port $port: reusing our $name (pid $pid_port)"
      return 0
    fi
  fi
  if cmdline_of "$pid_port" | grep -q "synth_provider.py $name $port"; then
    warn "port $port held by an unowned synth_provider.py (pid $pid_port) — adopting it into $pid_file"
    printf '%s\n' "$pid_port" > "$pid_file"
    return 0
  fi
  die "port $port is held by an unrecognised process (pid $pid_port): $(cmdline_of "$pid_port") — refusing to touch it"
}

check_port "$PORT_A" locala
check_port "$PORT_B" localb

# --- start synthetic providers ---------------------------------------------------
start_one() { # $1=name $2=port $3=model
  local name="$1" port="$2" model="$3" pid_file
  pid_file="$PIDDIR/$name.pid"
  if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    say "$name already running (pid $(cat "$pid_file"))"
    return 0
  fi
  local pid_port; pid_port="$(pid_for_port "$port" || true)"
  if [ -n "$pid_port" ]; then
    say "$name already serving on :$port (pid $pid_port)"
    return 0
  fi
  PROVIDER_LOG_DIR="$LOGDIR" nohup python3 "$HERE/synth_provider.py" "$name" "$port" "$model" \
      > "$LOGDIR/$name.stdout.log" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" > "$pid_file"
  say "started $name on :$port (pid $pid, model $model)"
}

start_one locala "$PORT_A" local-model-a
start_one localb "$PORT_B" local-model-b

# --- wait for health, bounded ----------------------------------------------------
deadline=$(( $(date +%s) + SPAWN_PID_WAIT_S ))
for spec in "locala:$PORT_A:http://127.0.0.1:8401/v1/models:local-model-a" "localb:$PORT_B:http://127.0.0.1:8402/v1/models:local-model-b"; do
  name="${spec%%:*}"; rest="${spec#*:}"; url="http://127.0.0.1:${rest%%:*}/v1/models"; want="${rest##*:}"
  ok=0
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS -m 2 "$url" 2>/dev/null | grep -q "$want"; then ok=1; break; fi
    sleep 0.2
  done
  [ "$ok" = 1 ] || die "$name failed health check on $url within ${SPAWN_PID_WAIT_S}s"
done
say "synthetic providers healthy"

# --- ollama: report only, never restart -------------------------------------------
ollama="unavailable"
if curl -fsS -m 3 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  if curl -fsS -m 3 "$OLLAMA_URL/api/tags" | grep -q 'ornith-1.5:9b'; then
    ollama="ok (ornith-1.5:9b present)"
  else
    ollama="degraded (service up; model ornith-1.5:9b NOT present)"
  fi
else
  ollama="unavailable (127.0.0.1:11434 not answering — not restarting the shared service)"
fi
say "ollama: $ollama"

# --- plugin load check --------------------------------------------------------------
HERMES_HOME="$HOME_DIR" "$REPO/.venv/bin/python" - <<'PY' || die "AutoRouter plugin failed to load in the isolated profile"
import sys
from hermes_cli.plugins import get_plugin_manager, has_middleware
from hermes_cli.middleware import PRE_INFERENCE_ROUTE_MIDDLEWARE
m = get_plugin_manager()
m.discover_and_load()
assert has_middleware(PRE_INFERENCE_ROUTE_MIDDLEWARE), "pre_inference_route consumer not registered"
print("plugin: pre_inference_route consumer registered")
PY

python3 - "$RUNTIME" "$branch" "$sha" "$ollama" <<'PY'
import json, sys, time
runtime, branch, sha, ollama = sys.argv[1:5]
doc = {"started_at": time.time(), "branch": branch, "hermes_sha": sha, "ollama": ollama,
       "providers": {"locala": 8401, "localb": 8402}}
open(f"{runtime}/status.json", "w", encoding="utf-8").write(json.dumps(doc, indent=2) + "\n")
PY

say "start-local: OK"
