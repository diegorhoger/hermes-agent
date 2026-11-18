#!/bin/bash
# Start the isolated local AutoRouter development environment (dispatch 141).
#
# Starts the two synthetic loopback providers (Deterministic, zero cost, zero
# credentials) with ownership-verified PID files, health-checks them, links
# the isolated profile, and reports (never modifies) the shared Ollama
# runtime. Refuses to start when a port is held by an unverified process;
# never signals a process it cannot verify.
#
# A provider is "ours" only when it passes full ownership verification
# (PID file marker + exact command line under THIS development root +
# recorded start identity + port-listener match). Unowned command lines that
# merely resemble a synth provider are never adopted.
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

# shellcheck source=provider-ownership.sh
. "$HERE/provider-ownership.sh"

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

# --- ownership-aware port checks ------------------------------------------------
# For each provider: verify the pid file (full ownership); verify the port;
# never adopt or signal anything we cannot verify.
handle_existing() { # $1=name $2=port $3=model; prints pid to reuse, or empty
  local name="$1" port="$2" model="$3" pid holder
  local pid_file="$PIDDIR/$name.pid"
  local rc=0
  if pid="$(read_verified_pid_file "$pid_file" "$name" "$port" "$model")"; then
    printf '%s\n' "$pid"
    return 0
  else
    rc=$?
  fi
  if [ "$rc" = 2 ]; then
    # The pid file is ours to manage but neither adopt nor signal what it
    # names: a matching live process that fails verification is foreign or
    # recycled. Inspect the port; only when it is FREE is the file stale.
    local holder; holder="$(port_listener_pid "$port" || true)"
    if [ -n "$holder" ]; then
      warn "$name: $pid_file fails ownership verification and :$port is held by pid $holder — refusing (foreign process left untouched)"
      return 3
    fi
    warn "$name: $pid_file fails ownership verification but :$port is free — removing the stale pid file (no process signalled)"
    rm -f "$pid_file"
  fi
  printf ''
  return 0
}

start_one() { # $1=name $2=port $3=model
  local name="$1" port="$2" model="$3" pid holder
  local pid_file="$PIDDIR/$name.pid"
  pid="$(handle_existing "$name" "$port" "$model")" || die "$name: cannot establish ownership of the existing state on :$port — refusing to start"
  if [ -n "$pid" ]; then
    if curl -fsS -m 2 "http://127.0.0.1:$port/v1/models" 2>/dev/null | grep -q "$model"; then
      say "port $port: reusing our verified $name (pid $pid)"
      return 0
    fi
    die "port $port: verified $name pid $pid is not answering a healthy /v1/models — leaving it alone; run stop-local.sh to stop it"
  fi
  holder="$(port_listener_pid "$port" || true)"
  if [ -n "$holder" ]; then
    die "port $port is held by an unverified process (pid $holder): $(ps_command_of "$holder") — refusing to touch it"
  fi
  # Spawn, record identity, and publish the pid file with O_EXCL semantics.
  PROVIDER_LOG_DIR="$LOGDIR" nohup python3 "$HERE/synth_provider.py" "$name" "$port" "$model" \
      > "$LOGDIR/$name.stdout.log" 2>&1 &
  pid=$!
  if ! write_pid_file "$pid_file" "$pid" "$name" "$port" "$model"; then
    # Another writer won or the process already died; never overwrite the winner.
    if kill -0 "$pid" 2>/dev/null; then kill -TERM "$pid" 2>/dev/null || true; fi
    die "failed to record verified ownership for $name (pid $pid) — refusing to continue (pid file race?)"
  fi
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
HERMES_HOME="$HOME_DIR" PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" "$REPO/.venv/bin/python" - <<'PY' || die "AutoRouter plugin failed to load in the isolated profile"
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
