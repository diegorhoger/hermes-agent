#!/bin/bash
# provider-ownership.sh — verified process ownership for the isolated local
# AutoRouter development environment (dispatch 141).
#
# Sourced by start-local.sh and stop-local.sh. A process is considered this
# environment's synthetic provider ONLY when ALL of the following hold:
#
#   1. The PID is numeric and the process exists (not a zombie).
#   2. The process's command line is EXACTLY
#        python3 <abs path to this tools/local-dev>/synth_provider.py <name> <port> <model>
#      — same development root, same script path, same arguments. A process
#      whose command line merely *contains* a matching fragment is never
#      accepted, and neither is a synth_provider.py living elsewhere.
#   3. The process start identity recorded in the PID file matches the
#      process's CURRENT start identity (`ps -o lstart=`), so a PID file
#      naming a recycled PID is never accepted as a healthy owned provider.
#   4. The port is independently verified to be held by that same PID.
#
# Nothing is signalled for a process that fails verification.

PROVIDER_OWNERSHIP_VERSION=1
PID_MARKER="hermes-local-dev-provider v1"

# Self-locate this script's directory as the development root when the caller
# sources this file without defining HERE (e.g. from a Node harness).
if [ -z "${HERE:-}" ]; then
	HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

# A python interpreter name (python, python3, python3.14, ...) — the interpreter
# spelling may differ per platform (framework launcher vs bin/python3), so the
# identity that is pinned EXACTLY is the script path, the arguments, the
# development root and the start identity.
# interpreter names, any case: python, Python, python3, python3.14, ...
PYTHON_NAME_RE='^[Pp][Yy][Tt][Hh][Oo][Nn](3(\.[0-9]+)*)?$'

ps_command_of() {  # $1=pid — exact argv as ps shows it, or empty
	ps -p "$1" -o command= 2>/dev/null | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

ps_state_of() {  # $1=pid — e.g. S, R, Z; empty on failure
	ps -p "$1" -o state= 2>/dev/null | tr -d '[:space:]'
}

ps_lstart_of() {  # $1=pid — process start identity; empty on failure
	ps -p "$1" -o lstart= 2>/dev/null | sed -e 's/[[:space:]]*$//' | tr -s ' '
}

port_listener_pid() {  # $1=port — PID holding the LISTEN socket, or empty
	lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1
}

# verify_provider_process <pid> <name> <port> <model> [lstart-token]
#   Returns 0 iff every check passes. When a lstart token is supplied the
#   process start identity must equal it exactly (PID-reuse guard).
verify_provider_process() {
	local pid="$1" name="$2" port="$3" model="$4" token="${5:-}"
	[[ "$pid" =~ ^[0-9]+$ ]] || return 1
	kill -0 "$pid" 2>/dev/null || return 1
	case "$(ps_state_of "$pid")" in
		*Z* | Z*) return 1 ;;          # a zombie owns nothing and must never be signalled
	esac
	local cmd; cmd="$(ps_command_of "$pid")"
	[ -n "$cmd" ] || return 1
	# Parse argv: <interpreter> <abs-script> <name> <port> <model> — no extras,
	# no different script, no different root. The interpreter name must look like
	# python; the script must be THIS checkout's tools/local-dev/synth_provider.py.
	read -r a0 script arg_name arg_port arg_model a_extra <<< "$cmd"
	a0_base="$(basename "${a0:-}")"
	[[ "$a0_base" =~ $PYTHON_NAME_RE ]] || return 1
	[ "${script:-}" = "$HERE/synth_provider.py" ] || return 1
	[ "${arg_name:-}" = "$name" ] && [ "${arg_port:-}" = "$port" ] && [ "${arg_model:-}" = "$model" ] || return 1
	[ -z "${a_extra:-}" ] || return 1
	if [ -n "$token" ]; then
		local start; start="$(ps_lstart_of "$pid")"
		[ -n "$start" ] || return 1
		[ "$start" = "$token" ] || return 1
	fi
	[ "$(port_listener_pid "$port")" = "$pid" ] || return 1
	return 0
}

# read_verified_pid_file <file> <name> <port> <model>
#   On success prints the pid and returns 0.
#   Returns 2 when the file exists but is stale/foreign/unreadable — callers
#   must NOT signal anything it names.
#   Returns 1 when the file is absent.
read_verified_pid_file() {
	local file="$1" name="$2" port="$3" model="$4"
	[ -f "$file" ] || return 1
	local marker pid token got_name got_port got_model
	{
		IFS= read -r marker || true
		IFS=' ' read -r _pid_key pid || true
		IFS=' ' read -r _started_key token || true
		IFS=' ' read -r _name_key got_name || true
		IFS=' ' read -r _port_key got_port || true
		IFS=' ' read -r _model_key got_model || true
	} < "$file"
	if [ "$marker" != "$PID_MARKER" ] || [ "${_pid_key:-}" != pid ] || [ "${_started_key:-}" != started ] \
		|| [ "${_name_key:-}" != name ] || [ "${_port_key:-}" != port ] || [ "${_model_key:-}" != model ]; then
		return 2
	fi
	case "${pid:-}" in
		''|*[!0-9]*) return 2 ;;
	esac
	[ "$got_name" = "$name" ] && [ "$got_port" = "$port" ] && [ "$got_model" = "$model" ] || return 2
	verify_provider_process "$pid" "$name" "$port" "$model" "$token" || return 2
	printf '%s\n' "$pid"
	return 0
}

# write_pid_file <file> <pid> <name> <port> <model>
#   Controlled creation: fails instead of replacing an existing file
#   (callers ensure no verified file exists first). Records the pid AND the
#   process start identity used by the next verification.
write_pid_file() {
	local file="$1" pid="$2" name="$3" port="$4" model="$5" token
	token="$(ps_lstart_of "$pid")" || return 1
	[ -n "$token" ] || return 1
	( set -o noclobber
	  {
	    printf '%s\n' "$PID_MARKER"
	    printf 'pid %s\n' "$pid"
	    printf 'started %s\n' "$token"
	    printf 'name %s\n' "$name"
	    printf 'port %s\n' "$port"
	    printf 'model %s\n' "$model"
	  } > "$file" ) 2>/dev/null || return 1
}

# revalidate_before_signal <pid> <name> <port> <model> <lstart-token>
#   Final check immediately before a signal; identical to verify.
revalidate_before_signal() {
	verify_provider_process "$1" "$2" "$3" "$4" "$5"
}
