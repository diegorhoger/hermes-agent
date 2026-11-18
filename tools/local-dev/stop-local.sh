#!/bin/bash
# Stop the isolated local AutoRouter development environment (dispatch 141).
#
# Only processes that pass FULL ownership verification (PID file marker +
# exact command line under THIS development root + recorded start identity
# + port-listener match) are signalled — SIGTERM, bounded wait, SIGKILL only
# for a verified owner that ignored SIGTERM. Verification is repeated
# immediately before every signal. Anything else is reported and left
# completely untouched. There is deliberately NO orphan sweep: a process
# holding a port is not ours to terminate merely because its command line
# resembles a synth provider.
#
# Usage: tools/local-dev/stop-local.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEV_ROOT="${HERMES_DEV_ROOT:-$(dirname "$REPO")}"
RUNTIME="${HERMES_DEV_RUNTIME:-$DEV_ROOT/runtime}"
PIDDIR="$RUNTIME/pids"
GRACE_S="${STOP_GRACE_S:-10}"

# shellcheck source=provider-ownership.sh
. "$HERE/provider-ownership.sh"

say()  { printf '%s\n' "$*"; }
warn() { printf 'stop-local: WARN: %s\n' "$*" >&2; }

rc=0
stopped=0

for pid_file in "$PIDDIR"/locala.pid "$PIDDIR"/localb.pid; do
	[ -f "$pid_file" ] || continue
	name="$(basename "$pid_file" .pid)"
	case "$name" in
		locala) port=8401 ;;
		localb) port=8402 ;;
		*)      warn "$pid_file: unknown provider name — leaving untouched"; rc=3; continue ;;
	esac
	model="$( [ "$name" = locala ] && echo local-model-a || echo local-model-b )"
	pid="$(read_verified_pid_file "$pid_file" "$name" "$port" "$model")" || {
		rc_read=$?
		if [ "$rc_read" = 2 ]; then
			# Stale or foreign: NEVER signal what the file names. Remove the file
			# only when its process is provably gone (a plain-number pid file from
			# an older writer is parsed the same way).
			stale_pid="$(sed -n -e 's/^pid //p' -e 's/^\([0-9][0-9]*\)$/\1/p' "$pid_file" 2>/dev/null | head -1 || true)"
			if [[ "${stale_pid:-}" =~ ^[0-9]+$ ]] && ! kill -0 "$stale_pid" 2>/dev/null; then
				rm -f "$pid_file"
				say "$name: stale pid file removed (pid ${stale_pid:-?} already gone — nothing signalled)"
				continue
			fi
			# Foreign, recycled or unverifiable process: leave it AND the file alone.
			warn "$name: pid file fails ownership verification — REFUSING to kill anything; left untouched"
			rc=3
			continue
		fi
		warn "$name: pid file unreadable — left untouched"
		rc=3
		continue
	}

	# Revalidate immediately before the first signal.
	token="$(sed -n 's/^started //p' "$pid_file" 2>/dev/null || true)"
	if ! revalidate_before_signal "$pid" "$name" "$port" "$model" "$token"; then
		warn "$name: pid $pid failed revalidation immediately before SIGTERM — REFUSING to signal it"
		rc=3
		continue
	fi
	kill -TERM "$pid"
	deadline=$(( $(date +%s) + GRACE_S ))
	while kill -0 "$pid" 2>/dev/null && [ "$(date +%s)" -lt "$deadline" ]; do sleep 0.2; done
	if kill -0 "$pid" 2>/dev/null; then
		# Revalidate again immediately before the final signal, against the ORIGINAL
		# start identity recorded in the pid file (not a fresh read).
		if revalidate_before_signal "$pid" "$name" "$port" "$model" "$token"; then
			warn "$name: verified pid $pid ignored SIGTERM for ${GRACE_S}s — SIGKILL (verified owner)"
			kill -KILL "$pid"
		else
			warn "$name: pid $pid changed identity mid-stop — REFUSING to SIGKILL"
			rc=3
			continue
		fi
	fi
	rm -f "$pid_file"
	say "$name: stopped (pid $pid)"
	stopped=$((stopped + 1))
done

# Report (never touch) anything still holding the provider ports.
for port in 8401 8402; do
  holder="$(port_listener_pid "$port" || true)"
  if [ -n "$holder" ]; then
    warn ":$port still busy after stop (pid $holder): $(ps_command_of "$holder") — left untouched"
    rc=3
  fi
done

say "stop-local: ${stopped} provider(s) stopped; rc=$rc"
exit "$rc"
