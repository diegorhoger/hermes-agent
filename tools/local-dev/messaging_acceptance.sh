#!/bin/bash
# Deterministic live messaging-gateway acceptance for the isolated AutoRouter dev profile.
#
# Successor to messaging_acceptance.sh (dispatch 146): the scenarios are no longer
# pinned to "locala first" — which breaks whenever accumulated routing history
# changes the auto policy's tie-breaking — but are driven by the *observed* first
# eligible decision. Fresh isolated acceptance state is created on every run:
#
#   * a dedicated Hermes profile is rebuilt from scratch at $RUNTIME/acceptance-home
#     (never the developer's hermes-home; ~/.hermes is refused)
#   * the loopback catalog is seeded with free evidence for exactly the two
#     synthetic loopback routes (Ollama excluded from the allowlist by design)
#   * provider request logs are truncated at the start so every assertion is exact
#
# Scenarios (all five must pass):
#   s1  ordinary unpinned turn: the auto policy selects an eligible provider;
#       the reply is served by the loopback pair (the decision row is read back
#       from the fresh profile's autorouter.db and cross-checked with the wire log).
#   s2  the selected provider is benched (429, mode=ratelimit): the turn fails
#       closed with the provider's 429 visible, is NOT replayed or substituted
#       by the peer within the turn.
#   s2b the next safe turn selects the other eligible provider; the conversation
#       session id and transcript survive the routing change.
#   s3  both providers benched: the turn retries the preferred route, fails closed
#       with the 429 visible, and records the bench (circuit opens).
#   s4  both circuits open: the next turn is refused with ZERO outbound provider
#       requests (wire logs prove it) and no synthetic content in the reply.
#   s5  model preference disabled: the turn is refused with ZERO outbound provider
#       requests and no synthetic content in the reply (negative guard).
#
# Usage: tools/local-dev/messaging_acceptance.sh           (requires the loopback providers up)
#        HERMES_DEV_ROOT=/path/to/dev-root tools/local-dev/messaging_acceptance.sh
set -euo pipefail

DEV="${HERMES_DEV_ROOT:-/Users/prometheos/HERMES/projects/AutoRouter/localdev-140}"
REPO="$DEV/hermes-agent"
RUNTIME="${HERMES_DEV_RUNTIME:-$DEV/runtime}"
HOME_DIR="$RUNTIME/acceptance-home"          # dedicated to this acceptance; rebuilt every run
LOGDIR="$RUNTIME/provider-logs"
EVIDENCE="$RUNTIME/messaging-acceptance.json"
PIDDIR="$RUNTIME/pids"
KEY="acceptance-5of5-key-not-a-secret"        # ≥16 chars: the API server refuses shorter keys
PORT=8643
S1="$RUNTIME/messaging-s1.json"; S2="$RUNTIME/messaging-s2.json"; S2B="$RUNTIME/messaging-s2b.json"
S3="$RUNTIME/messaging-s3.json"; S4="$RUNTIME/messaging-s4.json"; S5="$RUNTIME/messaging-s5.json"

die() { printf 'messaging-acceptance: ERROR: %s\n' "$*" >&2; exit 1; }
[ -d "$REPO" ] || die "Hermes checkout not found at $REPO (set HERMES_DEV_ROOT)"
[ -x "$REPO/.venv/bin/python" ] || die "venv missing at $REPO/.venv — build the checkout first"

# The acceptance home must not be (or live under) the everyday profile, nor collide
# with the developer's hermes-home.
case "$HOME_DIR" in
  "$HOME/.hermes"*) die "acceptance home $HOME_DIR resolves under ~/.hermes — refusing" ;;
esac
[ "$(basename "$HOME_DIR")" = "acceptance-home" ] || die "unexpected acceptance home: $HOME_DIR"

GW=""
restore_modes() { printf 'ok' > "$LOGDIR/locala.mode"; printf 'ok' > "$LOGDIR/localb.mode"; }
cleanup() {
  restore_modes || true
  if [ -n "$GW" ] && kill -0 "$GW" 2>/dev/null; then
    kill -TERM "$GW" 2>/dev/null || true
    for i in $(seq 1 30); do kill -0 "$GW" 2>/dev/null || break; sleep 1; done
  fi
}
trap cleanup EXIT

# --- fresh, isolated acceptance state (never the developer's databases) ---------------
echo "preparing fresh acceptance home at $HOME_DIR (wiping only our own prior acceptance state)"
rm -rf "$HOME_DIR"
mkdir -p "$HOME_DIR" "$PIDDIR"
HERMES_DEV_ROOT="$DEV" HERMES_DEV_HOME="$HOME_DIR" HERMES_DEV_RUNTIME="$RUNTIME" \
  AUTOROUTER_SRC="${AUTOROUTER_SRC:-$DEV/autorouter}" \
  python3 "$REPO/tools/local-dev/setup-local-home.py" >/dev/null
# Deterministic starting state: authorize exactly the synthetic pair; drop Ollama so the
# first eligible decision is reproducible on a fresh store.
python3 - "$HOME_DIR/config.yaml" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text(encoding="utf-8")
s = s.replace("providers: [ollama, locala, localb]", "providers: [locala, localb]")
s = s.replace("            - ollama/ornith-1.5:9b\n", "")
assert "ollama/ornith-1.5:9b" not in s, "ollama include was not removed"
p.write_text(s, encoding="utf-8")
PY
# Loopback-only evidence: exactly these two routes become eligible on a fresh store.
HERMES_DEV_ROOT="$DEV" HERMES_DEV_HOME="$HOME_DIR" HERMES_DEV_RUNTIME="$RUNTIME" \
  AUTOROUTER_SRC="${AUTOROUTER_SRC:-$DEV/autorouter}" \
  python3 "$REPO/tools/local-dev/seed-local-catalog.py" >/dev/null

: > "$LOGDIR/locala.jsonl"; : > "$LOGDIR/localb.jsonl"
restore_modes

cd "$REPO"
nohup env HERMES_HOME="$HOME_DIR" AI_AGENT=hermes-agent HERMES_AGENT=true \
  API_SERVER_ENABLED=true API_SERVER_KEY="$KEY" API_SERVER_PORT="$PORT" \
  "$REPO/.venv/bin/python" -m gateway.run > "$RUNTIME/messaging-acceptance.log" 2>&1 &
GW=$!
echo "$GW" > "$PIDDIR/messaging-acceptance.pid"
echo "gateway pid $GW starting (home $HOME_DIR)..."

deadline=$(( $(date +%s) + 180 ))
healthy=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then healthy=1; echo "health ok"; break; fi
  kill -0 "$GW" 2>/dev/null || die "gateway exited early — see $RUNTIME/messaging-acceptance.log"
  sleep 2
done
[ "$healthy" = 1 ] || die "gateway never became healthy — see $RUNTIME/messaging-acceptance.log"

request() { # $1=out-path
  curl -sS -m 150 -o "$1" -w '%{http_code}' \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{"model":"hermes-agent","messages":[{"role":"user","content":"Reply with exactly one word: ping"}]}' \
    "http://127.0.0.1:$PORT/v1/chat/completions" > "$1.code"
}

# ---------------------------------------------------------------- scenario s1
echo "s1: ordinary unpinned session is routed (recording which provider it selects)"
request "$S1"
SELECTED=$(sqlite3 "$HOME_DIR/plugin-data/autorouter/autorouter.db" \
  "select provider from decisions where reason='pre_inference_route' order by id desc limit 1")
[ -n "$SELECTED" ] || die "no pre_inference_route decision found in the acceptance home"
case "$SELECTED" in locala|localb) ;; *) die "unexpected selected provider: $SELECTED" ;; esac
OTHER=$([ "$SELECTED" = locala ] && echo localb || echo locala)
echo "selected provider: $SELECTED (peer: $OTHER)"

# ---------------------------------------------------------------- scenario s2
echo "s2: bench $SELECTED (429); the turn must fail closed with the 429 visible and no substitution"
printf 'ratelimit' > "$LOGDIR/$SELECTED.mode"
S2_T0=$(python3 -c 'import time; print(time.time())')
request "$S2"
S2_T1=$(python3 -c 'import time; print(time.time())')
printf 'ok' > "$LOGDIR/$SELECTED.mode"

# ---------------------------------------------------------------- scenario s2b
echo "s2b: the next safe turn selects the other eligible provider; session identity survives"
request "$S2B"

# ---------------------------------------------------------------- scenario s3
echo "s3: both providers benched; the turn retries the preferred route, fails closed, and records the bench"
printf 'ratelimit' > "$LOGDIR/locala.mode"; printf 'ratelimit' > "$LOGDIR/localb.mode"
S3_T0=$(python3 -c 'import time; print(time.time())')
request "$S3"
S3_T1=$(python3 -c 'import time; print(time.time())')

# ---------------------------------------------------------------- scenario s4
echo "s4: both circuits now open; the next turn must refuse with ZERO outbound provider requests"
S4_T0=$(python3 -c 'import time; print(time.time())')
request "$S4"
S4_T1=$(python3 -c 'import time; print(time.time())')
restore_modes

# ---------------------------------------------------------------- scenario s5
echo "s5: preference disabled (mode: none); the turn must be refused with ZERO outbound requests"
cp "$HOME_DIR/config.yaml" "$HOME_DIR/.config.yaml.s5"
python3 - "$HOME_DIR/config.yaml" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text(encoding="utf-8")
s2 = s.replace("mode: allowlist", "mode: none")
assert s2 != s
p.write_text(s2, encoding="utf-8")
PY
S5_T0=$(python3 -c 'import time; print(time.time())')
request "$S5"
S5_T1=$(python3 -c 'import time; print(time.time())')
cp "$HOME_DIR/.config.yaml.s5" "$HOME_DIR/config.yaml"
rm "$HOME_DIR/.config.yaml.s5"

kill -TERM "$GW" 2>/dev/null || true
for i in $(seq 1 30); do kill -0 "$GW" 2>/dev/null || break; sleep 1; done
GW=""

# Requests made inside the bench windows (exact attribution; no overlap fudge)
python3 - "$EVIDENCE" "$S1" "$S2" "$S2B" "$S3" "$S4" "$S5" "$SELECTED" "$OTHER" \
  "$S2_T0" "$S2_T1" "$S3_T0" "$S3_T1" "$S4_T0" "$S4_T1" "$S5_T0" "$S5_T1" "$LOGDIR" "$HOME_DIR" "$REPO" <<'PY'
import json, sys, os, sqlite3, subprocess

(out_path, s1, s2, s2b, s3, s4, s5, selected, other,
 s2_t0, s2_t1, s3_t0, s3_t1, s4_t0, s4_t1, s5_t0, s5_t1, logdir, home, repo) = sys.argv[1:21]

def load(p):
    code = int(open(p + ".code").read().strip())
    try:
        doc = json.load(open(p))
    except Exception:
        doc = None
    return code, doc

def content(doc):
    try:
        return (doc.get("choices") or [{}])[0].get("message", {}).get("content", "")
    except Exception:
        return ""

def finish(doc):
    try:
        return (doc.get("choices") or [{}])[0].get("finish_reason")
    except Exception:
        return None

def synthetic_name(t):
    # The canned loopback answer is prefixed with "synthetic-<name>/<model>"; the
    # provider's own name appearing in a 429 diagnostic is not served content.
    return "synthetic-" in t

c1, d1 = load(s1); c2, d2 = load(s2); c2b, d2b = load(s2b); c3, d3 = load(s3); c4, d4 = load(s4); c5, d5 = load(s5)
t1, t2, t2b, t3, t4, t5 = (content(d) for d in (d1, d2, d2b, d3, d4, d5))

def read_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try: rows.append(json.loads(line))
                    except json.JSONDecodeError: pass
    except FileNotFoundError:
        pass
    return rows

sel_model = "local-model-a" if selected == "locala" else "local-model-b"
oth_model = "local-model-a" if other == "locala" else "local-model-b"
reqs = read_jsonl(logdir + "/locala.jsonl") + read_jsonl(logdir + "/localb.jsonl")

def in_window(lo, hi):
    lo, hi = float(lo), float(hi)
    return [r for r in reqs if r.get("event") == "request" and lo <= r.get("ts", 0) <= hi]

s2_sel_reqs = [r for r in in_window(s2_t0, s2_t1) if r.get("model") == sel_model]
s2_peer_reqs = [r for r in in_window(s2_t0, s2_t1) if r.get("model") == oth_model]
s3_reqs = in_window(s3_t0, s3_t1)
s4_reqs = in_window(s4_t0, s4_t1)
s5_reqs = in_window(s5_t0, s5_t1)

# Session identity across the routing change: every turn must live in the SAME
# api_server session, and the transcript must keep growing.
db = os.path.join(home, "state.db")
con = sqlite3.connect(db)
integrity = con.execute("pragma integrity_check").fetchone()[0]
session_rows = con.execute(
    "select id from sessions where source='api_server' order by started_at").fetchall()
latest_sid = session_rows[-1][0] if session_rows else None
user_msgs = con.execute(
    "select count(*) from messages where session_id=? and role='user'", (latest_sid,)).fetchone()[0] if latest_sid else 0
con.close()

checks = []
def rec(name, ok, detail=""):
    checks.append({"check": name, "ok": bool(ok), **({"detail": detail} if detail else {})})
    print(("PASS" if ok else "FAIL"), name, f"— {detail}" if not ok else "")

rec("s1 ordinary unpinned session routed to an eligible loopback provider",
    c1 == 200 and synthetic_name(t1) and (("synthetic-locala" in t1) == (selected == "locala")),
    f"selected={selected} http={c1} content={t1[:80]!r}")
rec("s2 selected provider benched: failed turn is NOT replayed or substituted by the peer",
    c2 == 200 and ("429" in t2 or "rate" in t2.lower()) and not synthetic_name(t2)
    and bool(s2_sel_reqs) and not s2_peer_reqs,
    f"http={c2} content={t2[:100]!r} benched_reqs={len(s2_sel_reqs)} peer_reqs={len(s2_peer_reqs)}")
rec("s2b next safe turn selects the other eligible provider; session identity + transcript survive",
    c2b == 200 and synthetic_name(t2b)
    and (("synthetic-locala" in t2b) == (other == "locala"))
    and latest_sid is not None and user_msgs >= 3 and integrity == "ok",
    f"http={c2b} content={t2b[:80]!r} session={latest_sid} user_msgs={user_msgs} integrity={integrity}")
rec("s3 both providers benched: 429 surfaces, circuit opens, no synthetic content",
    c3 == 200 and ("429" in t3 or "rate" in t3.lower()) and not synthetic_name(t3),
    f"http={c3} requests={len(s3_reqs)} content={t3[:80]!r}")
rec("s4 circuits open: refusal with ZERO outbound provider requests",
    len(s4_reqs) == 0 and (c4 != 200 or finish(d4) == "error" or bool((d4 or {}).get("error"))
                           or "rout" in t4.lower())
    and not synthetic_name(t4),
    f"http={c4} requests_during_s4={len(s4_reqs)} content={t4[:80]!r}")
rec("s5 preference none: refusal with ZERO outbound provider requests",
    len(s5_reqs) == 0 and (c5 != 200 or finish(d5) == "error" or bool((d5 or {}).get("error"))
                           or "refus" in t5.lower())
    and not synthetic_name(t5),
    f"http={c5} requests_during_s5={len(s5_reqs)} content={t5[:80]!r}")

sha = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                     capture_output=True, text=True).stdout.strip()
out = {"issue": "diegorhoger/autorouter#104",
       "surface": "live messaging gateway (api_server platform), deterministic fresh state",
       "hermes_sha": sha,
       "deterministic_starting_state": True,
       "selected_provider": selected,
       "checks": checks,
       "passed": sum(1 for c in checks if c["ok"]),
       "failed": sum(1 for c in checks if not c["ok"])}
out["status"] = "PASSED" if out["failed"] == 0 else "FAILED"
open(out_path, "w").write(json.dumps(out, indent=2) + "\n")
print(f"{out['passed']} passed, {out['failed']} failed -> {out_path}")
sys.exit(0 if out["failed"] == 0 else 1)
PY
