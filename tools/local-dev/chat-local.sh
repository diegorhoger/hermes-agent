#!/bin/bash
# One isolated chat turn against the local AutoRouter development environment.
#
# Spawns the TUI gateway (the same JSON-RPC wire the TUI/Desktop uses),
# submits one message, waits for message.complete, prints the reply JSON.
# The session id is kept in $HERMES_DEV_RUNTIME/current-session.json so a
# later invocation resumes the SAME conversation — including across a
# start/stop/restart cycle. Use --new to force a fresh session.
#
# Usage: tools/local-dev/chat-local.sh "your message" [--new] [--session ID]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEV_ROOT="${HERMES_DEV_ROOT:-$(dirname "$REPO")}"
HOME_DIR="${HERMES_DEV_HOME:-$DEV_ROOT/hermes-home}"
RUNTIME="${HERMES_DEV_RUNTIME:-$DEV_ROOT/runtime}"
WORK="$RUNTIME/work"
SESSION_FILE="$RUNTIME/current-session.json"
mkdir -p "$RUNTIME" "$WORK"

MSG=""
NEW=0
SESSION_ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --new) NEW=1 ;;
    --session) shift; SESSION_ID="${1:-}" ;;
    *) MSG="${MSG:+$MSG }$1" ;;
  esac
  shift
done
[ -n "$MSG" ] || { printf 'usage: chat-local.sh "message" [--new] [--session ID]\n' >&2; exit 2; }
[ -x "$REPO/.venv/bin/python" ] || { printf 'chat-local: venv python missing at %s\n' "$REPO/.venv/bin/python" >&2; exit 1; }
[ -f "$HOME_DIR/config.yaml" ] || { printf 'chat-local: isolated profile missing; run start-local.sh first\n' >&2; exit 1; }

HERMES_HOME="$HOME_DIR" REPO="$REPO" WORK="$WORK" SESSION_FILE="$SESSION_FILE" NEW="$NEW" SESSION_ID="$SESSION_ID" MSG="$MSG" "$REPO/.venv/bin/python" - <<'PY'
import json, os, subprocess, sys, threading, time

ROOT = os.environ["REPO"]
HOME = os.environ["HERMES_HOME"]
WORK = os.environ["WORK"]
SESSION_FILE = os.environ["SESSION_FILE"]
MSG = os.environ["MSG"]

env = dict(os.environ)
env["HERMES_HOME"] = HOME
env["TZ"] = "UTC"
env["PYTHONHASHSEED"] = "0"
env["HERMES_TUI_SLASH_TIMEOUT_S"] = "15"

proc = subprocess.Popen(
    [ROOT + "/.venv/bin/python", "-m", "tui_gateway.entry"],
    cwd=WORK, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=open(os.path.join(WORK, "..", "gateway-chat.stderr.log"), "ab", buffering=0))

frames, responses = [], {}
lock = threading.Lock()

def reader():
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except Exception:
            continue
        with lock:
            if "id" in frame and ("result" in frame or "error" in frame):
                responses[frame["id"]] = frame
            else:
                frames.append(frame)

threading.Thread(target=reader, daemon=True).start()

next_id = 0
def call(method, params=None, timeout=120):
    global next_id
    with lock:
        next_id += 1
        rid = next_id
    proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}) + "\n").encode())
    proc.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        with lock:
            if rid in responses:
                return responses.pop(rid)
        time.sleep(0.05)
    raise TimeoutError(f"timeout waiting for {method}")

def wait_complete(timeout=120):
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        deadline_slice = time.time() + 0.2
        with lock:
            batch, frames[:] = frames[:], []
        seen.extend(batch)
        for e in batch:
            if e.get("method") == "event" and (e.get("params") or {}).get("type") == "message.complete":
                return e, seen
            if e.get("method") == "event" and (e.get("params") or {}).get("type") == "error":
                raise RuntimeError("gateway error event: " + json.dumps((e.get("params") or {}).get("payload"), ensure_ascii=False)[:500])
        if time.time() < deadline_slice:
            time.sleep(0.05)
    raise TimeoutError(
        "timeout waiting for message.complete; last frames: " + json.dumps(seen[-6:], ensure_ascii=False)[:2000])

session_id = os.environ.get("SESSION_ID") or ""
if os.environ["NEW"] == "0" and not session_id and os.path.exists(SESSION_FILE):
    try:
        session_id = json.load(open(SESSION_FILE)).get("stored_session_id") or ""
    except Exception:
        session_id = ""

resumed = False
stored_session_id = ""
if session_id:
    r = call("session.resume", {"session_id": session_id})
    result = (r or {}).get("result") or {}
    if "error" in r or not result.get("session_id"):
        print(json.dumps({"resume_failed_for": session_id, "error": r.get("error") or r,
                          "detail": "resume failed; starting a fresh session"}))
        session_id = ""
    else:
        resumed = True
        stored_session_id = session_id
        session_id = result["session_id"]

if not session_id:
    r = call("session.create", {})
    result = (r or {}).get("result") or {}
    session_id = result.get("session_id")
    stored_session_id = result.get("stored_session_id") or ""
    if not session_id:
        print(json.dumps({"error": "session.create failed", "response": r}))
        proc.terminate(); sys.exit(1)

r = call("prompt.submit", {"session_id": session_id, "text": MSG})
if "error" in r:
    print(json.dumps({"error": r["error"], "session_id": session_id}))
    proc.terminate(); sys.exit(1)

complete, _seen = wait_complete(180)
payload = ((complete.get("params") or {}).get("payload")) or {}
result = {
    "session_id": session_id,
    "stored_session_id": stored_session_id or session_id,
    "resumed": resumed,
    "final_response": payload.get("text"),
    "usage": payload.get("usage"),
    "status": payload.get("status"),
}
json.dump({"session_id": session_id, "stored_session_id": stored_session_id or session_id}, open(SESSION_FILE, "w"))
print(json.dumps(result, indent=2, ensure_ascii=False))

proc.stdin.close()
proc.terminate()
try:
    proc.wait(5)
except Exception:
    proc.kill()
PY
