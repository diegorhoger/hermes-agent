#!/bin/bash
# Stop the isolated local AutoRouter development environment (dispatch 140).
#
# Only processes that pass ownership verification (PID file + exact command
# line match for this environment's synth providers) are signalled. Bounded
# graceful termination: SIGTERM, bounded wait, SIGKILL only for verified
# owners that ignored SIGTERM. Anything else is reported and left alone.
#
# Usage: tools/local-dev/stop-local.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEV_ROOT="${HERMES_DEV_ROOT:-$(dirname "$REPO")}"
RUNTIME="${HERMES_DEV_RUNTIME:-$DEV_ROOT/runtime}"
PIDDIR="$RUNTIME/pids"
GRACE_S="${STOP_GRACE_S:-10}"

say()  { printf '%s\n' "$*"; }
warn() { printf 'stop-local: WARN: %s\n' "$*" >&2; }
cmdline_of() { ps -p "$1" -o command= 2>/dev/null || true; }

rc=0
stopped=0

for pid_file in "$PIDDIR"/locala.pid "$PIDDIR"/localb.pid; do
  [ -f "$pid_file" ] || continue
  name="$(basename "$pid_file" .pid)"
  port="$( [ "$name" = locala ] && echo 8401 || echo 8402 )"
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [ -z "$pid" ]; then
    warn "osc $name: empty pid file — removing"; rm -f "$pid_file"; continue
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    say "$name: pid $pid already gone — removing pid file"; rm -f "$pid_file"; continue
  fi
  # Ownership verification: the command line must be exactly this environment's
  # synth provider for this port. Anything else is left alone.
  cmd="$(cmdline_of "$pid")"
  if ! printf '%s' "$cmd" | grep -q "synth_provider.py $name $port"; then
    warn "$name: pid $pid is not this environment's synth provider ($cmd) — REFUSING to kill it"
    rc=3
    continue
  fi
  kill -TERM "$pid"
  deadline=$(( $(date +%s) + GRACE_S ))
  while kill -0 "$pid" 2>/dev/null && [ "$(date +%s)" -lt "$deadline" ]; do sleep 0.2; done
  if kill -0 "$pid" 2>/dev/null; then
    warn "$name: pid $pid ignored SIGTERM for ${GRACE_S}s — SIGKILL (verified owner)"
    kill -KILL "$pid"
  fi
  rm -f "$pid_file"
  say "$name: stopped (pid $pid)"
  stopped=$((stopped + 1))
done

# Orphan sweep: any synth_provider.py for THIS repo's ports whose parent is init
for port in 8401 8402; do
  name="$( [ "$port" = 8401 ] && echo locala || echo localb )"
  orphan="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)"
  if [ -n "$orphan" ]; then
    for pid in $orphan; do
      cmd="$(cmdline_of "$pid")"
      if printf '%s' "$cmd" | grep -q "synth_provider.py $name $port"; then
        warn "$name: orphaned verified instance on :$port (pid $pid) — terminating"
        kill -TERM "$pid" 2>/dev/null || true
        deadline=$(( $(date +%s) + GRACE_S ))
        while kill -0 "$pid" 2>/dev/null && [ "$(date +%s)" -lt "$deadline" ]; do sleep 0.2; done
        kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
        stopped=$((stopped + 1))
      else
        warn ":$port held by an unrecognised process (pid $pid): $cmd — leaving it alone"
        rc=3
      fi
    done
  fi
done

# Ports must be free afterwards
for port in 8401 8402; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | grep -q .; then
    warn ":$port still busy after stop"
    rc=3
  fi
done

say "stop-local: ${stopped} provider(s) stopped; rc=$rc"
exit "$rc"
