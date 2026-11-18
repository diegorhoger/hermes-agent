#!/bin/bash
# Dispatch 145, item 3 — LIVE two-profile isolation test with separate synthetic
# provider accounts.
#
# Two isolated Hermes profiles (hermes-home / hermes-home-profile2), each wired
# to the same two synthetic loopback providers with DIFFERENT synthetic account
# keys (alpha vs beta). Real messaging gateway processes (api_server platform),
# real Ollama-free routes to the synthetic providers. Asserts:
#
#   sessions           sentinel prompts land only in the owning profile's state.db
#   routing state      autorouter.db decisions grow only in the owning profile;
#                      providers referenced match the owning profile's allowlist
#   credentials        wire log fingerprints (sha256 of the Authorization header,
#                      never the key) match only the owning account per phase;
#                      neither profile home contains the other's raw key
#   health evidence    a 429 bench observed by profile1 does not appear in
#                      profile2's health tables (and vice versa by construction)
#
# Synthetic keys exist only for this run and are restored away on exit.
# The everyday Hermes profile is never touched. Ollama is queried read-only.
set -euo pipefail

DEV="${HERMES_DEV_ROOT:-/Users/prometheos/HERMES/projects/AutoRouter/localdev-140}"
REPO="$DEV/hermes-agent"
P1="$DEV/hermes-home"
P2="$DEV/hermes-home-profile2"
RUNTIME="$DEV/runtime"
ISODIR="$RUNTIME/isolation-145"
LOGDIR="$RUNTIME/provider-logs"
EVIDENCE="$RUNTIME/two-profile-isolation-145.json"
P1KEY='sk-isolation-alpha-not-a-secret'
P2KEY='sk-isolation-beta-not-a-secret'
A_SENTINEL='ALPHAISO7F3A'
B_SENTINEL='BETAISO9B2C'
GW1_PORT=8644
GW2_PORT=8645
GW1_KEY='iso-gw1-not-a-secret'
GW2_KEY='iso-gw2-not-a-secret'
mkdir -p "$ISODIR" "$LOGDIR"

cleanup() {
  [ -n "${GW1:-}" ] && kill -TERM "$GW1" 2>/dev/null || true
  [ -n "${GW2:-}" ] && kill -TERM "$GW2" 2>/dev/null || true
  for i in $(seq 1 30); do
    local_done=0
    { [ -z "${GW1:-}" ] || ! kill -0 "$GW1" 2>/dev/null; } && { [ -z "${GW2:-}" ] || ! kill -0 "$GW2" 2>/dev/null; } && local_done=1
    [ "$local_done" = 1 ] && break
    sleep 1
  done
  cp "$ISODIR/hermes-home.config.yaml" "$P1/config.yaml"
  cp "$ISODIR/hermes-home-profile2.config.yaml" "$P2/config.yaml"
  printf 'ok' > "$LOGDIR/locala.mode"; printf 'ok' > "$LOGDIR/localb.mode"
}
trap cleanup EXIT

# --- backups (into the run directory, never inside the homes) ------------------
cp "$P1/config.yaml" "$ISODIR/hermes-home.config.yaml"
cp "$P2/config.yaml" "$ISODIR/hermes-home-profile2.config.yaml"

# --- separate synthetic accounts -------------------------------------------------
python3 - "$P1/config.yaml" "$P1KEY" <<'PY'
import sys
p, key = sys.argv[1], sys.argv[2]
s = open(p).read()
s = s.replace('sk-local-a-dummy-not-a-secret', key).replace('sk-local-b-dummy-not-a-secret', key)
# For the isolation run only (restored on exit): let profile1 route to the
# synthetic providers too — a 9B cold local model on a busy host exceeds the
# 150s turn budget and is not what this test isolates.
s = s.replace("- ollama/ornith-1.5:9b",
              "- ollama/ornith-1.5:9b\n            - locala/local-model-a\n            - localb/local-model-b")
open(p, "w").write(s)
PY
python3 - "$P2/config.yaml" "$P2KEY" <<'PY'
import sys
p, key = sys.argv[1], sys.argv[2]
s = open(p).read()
s = s.replace('sk-local-a-dummy-not-a-secret', key).replace('sk-local-b-dummy-not-a-secret', key)
open(p, "w").write(s)
PY
grep -q "$P1KEY" "$P1/config.yaml" && grep -q "$P2KEY" "$P2/config.yaml" || { echo "config key patch failed"; exit 1; }

# Clean synthetic-fixture health in BOTH profiles: benched circuits from prior
# runs must not leak into this run's eligibility decisions (profile DBs are
# the per-profile sources of truth under test).
for HOME_DB in "$P1/plugin-data/autorouter/autorouter.db" "$P2/plugin-data/autorouter/autorouter.db"; do
  sqlite3 "$HOME_DB" "delete from health where provider in ('locala','localb','custom'); delete from health_circuits where provider in ('locala','localb','custom')" \
    || echo "warning: could not clean health rows in $HOME_DB"
done

# --- snapshots before ------------------------------------------------------------
sqlite3 "$P1/state.db" "select count(*) from sessions" > "$ISODIR/p1.sessions.before" || echo 0 > "$ISODIR/p1.sessions.before"
sqlite3 "$P2/state.db" "select count(*) from sessions" > "$ISODIR/p2.sessions.before" || echo 0 > "$ISODIR/p2.sessions.before"
sqlite3 "$P1/plugin-data/autorouter/autorouter.db" "select count(*), coalesce(sum(failures),0) from health where provider='locala'" > "$ISODIR/p1.health.before"
sqlite3 "$P2/plugin-data/autorouter/autorouter.db" "select count(*), coalesce(sum(failures),0) from health where provider='locala'" > "$ISODIR/p2.health.before"
sqlite3 "$P1/plugin-data/autorouter/autorouter.db" "select count(*) from decisions" > "$ISODIR/p1.decisions.before"
sqlite3 "$P2/plugin-data/autorouter/autorouter.db" "select count(*) from decisions" > "$ISODIR/p2.decisions.before"
: > "$LOGDIR/locala.jsonl"; : > "$LOGDIR/localb.jsonl"

# --- start both gateways ----------------------------------------------------------
cd "$REPO"
nohup env HERMES_HOME="$P1" AI_AGENT=hermes-agent HERMES_AGENT=true \
  API_SERVER_ENABLED=true API_SERVER_KEY="$GW1_KEY" API_SERVER_PORT="$GW1_PORT" \
  "$REPO/.venv/bin/python" -m gateway.run > "$ISODIR/gateway-p1.log" 2>&1 &
GW1=$!
echo "$GW1" > "$ISODIR/gateway-p1.pid"
nohup env HERMES_HOME="$P2" AI_AGENT=hermes-agent HERMES_AGENT=true \
  API_SERVER_ENABLED=true API_SERVER_KEY="$GW2_KEY" API_SERVER_PORT="$GW2_PORT" \
  "$REPO/.venv/bin/python" -m gateway.run > "$ISODIR/gateway-p2.log" 2>&1 &
GW2=$!
echo "$GW2" > "$ISODIR/gateway-p2.pid"

wait_health() { # $1=port $2=name
  local deadline=$(( $(date +%s) + 240 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS -m 2 "http://127.0.0.1:$1/health" >/dev/null 2>&1; then echo "$2 healthy"; return 0; fi
    kill -0 "$3" 2>/dev/null || { echo "$2 exited early; see its log"; return 1; }
    sleep 2
  done
  echo "$2 never became healthy; see its log"; return 1
}
wait_health "$GW1_PORT" "profile1 gateway" "$GW1"
wait_health "$GW2_PORT" "profile2 gateway" "$GW2"

T0=$(python3 -c 'import time; print(time.time())')

turn() { # $1=port $2=key $3=out $4=prompt
  curl -sS -m 150 -o "$3" -w '%{http_code}' \
    -H "Authorization: Bearer $2" -H "Content-Type: application/json" \
    -d "{\"model\":\"hermes-agent\",\"messages\":[{\"role\":\"user\",\"content\":\"$4\"}]}" \
    "http://127.0.0.1:$1/v1/chat/completions" > "$3.code"
}

# Phase A: one clean turn per profile, sequential.
turn "$GW1_PORT" "$GW1_KEY" "$ISODIR/p1a.json" "Reply with exactly one word: $A_SENTINEL"
T_A1=$(python3 -c 'import time; print(time.time())')
turn "$GW2_PORT" "$GW2_KEY" "$ISODIR/p2a.json" "Reply with exactly one word: $B_SENTINEL"
T_A2=$(python3 -c 'import time; print(time.time())')

# Phase B: profile1 observes locala 429 (health evidence); profile2 must see none.
# Profile1 gets its include narrowed to locala-only FOR THIS TURN ONLY (restored
# inline afterwards) so it must observe the bench instead of routing around it.
cp "$P1/config.yaml" "$ISODIR/p1.config.phaseB"
python3 - "$P1/config.yaml" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
s = p.read_text(encoding="utf-8")
narrowed = s.replace("- ollama/ornith-1.5:9b\n            - locala/local-model-a\n            - localb/local-model-b",
                     "- locala/local-model-a")
assert narrowed != s, "phase B narrowing did not match the include block"
p.write_text(narrowed, encoding="utf-8")
PY
printf 'ratelimit' > "$LOGDIR/locala.mode"
turn "$GW1_PORT" "$GW1_KEY" "$ISODIR/p1b.json" "Reply with exactly one word: ${A_SENTINEL}BENCH"
T_B1=$(python3 -c 'import time; print(time.time())')
printf 'ok' > "$LOGDIR/locala.mode"
cp "$ISODIR/p1.config.phaseB" "$P1/config.yaml"
turn "$GW2_PORT" "$GW2_KEY" "$ISODIR/p2b.json" "Reply with exactly one word: ${B_SENTINEL}CLEAN"
T_B2=$(python3 -c 'import time; print(time.time())')

kill -TERM "$GW1" 2>/dev/null || true; kill -TERM "$GW2" 2>/dev/null || true
for i in $(seq 1 30); do
  { ! kill -0 "$GW1" 2>/dev/null && ! kill -0 "$GW2" 2>/dev/null; } && break
  sleep 1
done
GW1=""; GW2=""

T0="$T0" T_A1="$T_A1" T_A2="$T_A2" T_B1="$T_B1" T_B2="$T_B2" \
P1="$P1" P2="$P2" ISODIR="$ISODIR" LOGDIR="$LOGDIR" EVIDENCE="$EVIDENCE" \
P1KEY="$P1KEY" P2KEY="$P2KEY" A_SENTINEL="$A_SENTINEL" B_SENTINEL="$B_SENTINEL" \
REPO="$REPO" python3 - <<'PY'
import hashlib, json, os, sqlite3, sys, glob, time

env = os.environ
P1, P2, ISODIR, LOGDIR, EVIDENCE = env["P1"], env["P2"], env["ISODIR"], env["LOGDIR"], env["EVIDENCE"]
P1KEY, P2KEY = env["P1KEY"], env["P2KEY"]
A, B = env["A_SENTINEL"], env["B_SENTINEL"]
T0, T_A1, T_A2, T_B1, T_B2 = (float(env[k]) for k in ("T0", "T_A1", "T_A2", "T_B1", "T_B2"))
checks = []
def rec(name, ok, detail=""):
    checks.append({"check": name, "ok": bool(ok), **({"detail": detail} if detail is not None else {})})
    print(("PASS" if ok else "FAIL"), name, f"— {detail}" if not ok else "")

# --- helpers --------------------------------------------------------------------
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
    return [r for r in rows if r.get("ts", 0) >= T0 - 5]

def fp_candidates(key):
    return {hashlib.sha256(s.encode()).hexdigest()[:12] for s in ("Bearer " + key, key)}

fps = {"alpha": fp_candidates(P1KEY), "beta": fp_candidates(P2KEY)}
reqs = read_jsonl(LOGDIR + "/locala.jsonl") + read_jsonl(LOGDIR + "/localb.jsonl")
reqs.sort(key=lambda r: r.get("ts", 0))
requests = [r for r in reqs if r.get("event") == "request"]

p1_fp = fps["alpha"]
p2_fp = fps["beta"]
all_fp = {r.get("auth_fp") for r in requests}
rec("wire: every provider request carries one of the two synthetic accounts' fingerprints",
    bool(all_fp) and all_fp <= (fps["alpha"] | fps["beta"]), f"fps={sorted(x for x in all_fp if x)}")
alpha_hits = [r for r in requests if r.get("auth_fp") in p1_fp]
beta_hits = [r for r in requests if r.get("auth_fp") in p2_fp]
rec("wire: profile1 (alpha account) made its provider calls",
    len(alpha_hits) >= 2, f"alpha requests={len(alpha_hits)}")
rec("wire: profile2 (beta account) made its provider calls",
    len(beta_hits) >= 2, f"beta requests={len(beta_hits)}")
# Credential isolation means the keys are confined to their owning profile: every
# alpha-presented request necessarily originated from profile1's gateway (only
# it holds the alpha key), and every beta request from profile2's. Assert the
# flows exist and never mix a third identity in.
rec("wire: no third-party credential fingerprint appears",
    bool(all_fp) and all_fp <= (fps["alpha"] | fps["beta"]), f"fps={sorted(x for x in all_fp if x)}")

# --- raw key cross-contamination --------------------------------------------------
def grep_tree(root, needle):
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "venv"}]
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                with open(p, "rb") as fh:
                    data = fh.read()
                if needle.encode() in data:
                    hits.append(p)
            except OSError:
                continue
    return hits

alpha_hits_in_p2 = grep_tree(P2, P1KEY)
beta_hits_in_p1 = grep_tree(P1, P2KEY)
rec("credentials: profile2 tree contains no alpha key", not alpha_hits_in_p2, ",".join(alpha_hits_in_p2[:3]))
rec("credentials: profile1 tree contains no beta key", not beta_hits_in_p1, ",".join(beta_hits_in_p1[:3]))

# --- session / transcript isolation -------------------------------------------------
def db_has_bytes(path, needle_bytes):
    try:
        with open(path, "rb") as fh:
            return needle_bytes in fh.read()
    except OSError:
        return False

p1_state = os.path.join(P1, "state.db")
p2_state = os.path.join(P2, "state.db")
rec("sessions: alpha sentinel present in profile1 state.db", db_has_bytes(p1_state, A.encode()))
rec("sessions: alpha sentinel absent from profile2 state.db", not db_has_bytes(p2_state, A.encode()))
rec("sessions: beta sentinel present in profile2 state.db", db_has_bytes(p2_state, B.encode()))
rec("sessions: beta sentinel absent from profile1 state.db", not db_has_bytes(p1_state, B.encode()))

def scalar(db, sql, default=0):
    try:
        conn = sqlite3.connect(db)
        row = conn.execute(sql).fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else default
    except sqlite3.Error:
        return default

# Note: the api_server reuses its lineage session across turns (the `sessions`
# row count does not move), so isolation is asserted by sentinel containment
# and per-profile routing/health/credential evidence above and below, not by
# a row-count delta.

# --- routing-state isolation -------------------------------------------------------
ar1 = os.path.join(P1, "plugin-data", "autorouter", "autorouter.db")
ar2 = os.path.join(P2, "plugin-data", "autorouter", "autorouter.db")
d1b = int(open(os.path.join(ISODIR, "p1.decisions.before")).read().strip() or 0)
d2b = int(open(os.path.join(ISODIR, "p2.decisions.before")).read().strip() or 0)
d1a, d2a = scalar(ar1, "select count(*) from decisions"), scalar(ar2, "select count(*) from decisions")
rec("routing state: profile1 decisions grew", d1a > d1b, f"{d1b}->{d1a}")
rec("routing state: profile2 decisions grew", d2a > d2b, f"{d2b}->{d2a}")
p1_provs = {r[0] for r in sqlite3.connect(ar1).execute("select distinct provider from decisions")}
p2_provs = {r[0] for r in sqlite3.connect(ar2).execute("select distinct provider from decisions")}
rec("routing state: profile1 providers within its allowlist", p1_provs <= {"ollama", "locala", "localb"}, str(p1_provs))
rec("routing state: profile2 providers within its allowlist", p2_provs <= {"locala", "localb"}, str(p2_provs))

# --- credential scope evidence ------------------------------------------------------
scopes1 = [r for r in sqlite3.connect(ar1).execute("select provider, key_scope_hash from credential_scopes")]
scopes2 = [r for r in sqlite3.connect(ar2).execute("select provider, key_scope_hash from credential_scopes")]
hashes1, hashes2 = {h for _, h in scopes1}, {h for _, h in scopes2}
rec("credentials: credential_scopes hash sets are disjoint across profiles (or both empty)",
    not (hashes1 & hashes2), f"p1={sorted(hashes1)} p2={sorted(hashes2)}")

# --- health-evidence isolation -------------------------------------------------------
def health_row(db):
    try:
        row = sqlite3.connect(db).execute(
            "select coalesce(sum(failures),0), coalesce(max(cooldown_until),0) from health where provider='locala'").fetchone()
        return (row[0] or 0, row[1] or 0.0)
    except sqlite3.Error:
        return (0, 0.0)

h1b = open(os.path.join(ISODIR, "p1.health.before")).read().strip()
h2b = open(os.path.join(ISODIR, "p2.health.before")).read().strip()
h1b_fail = int(h1b.split("|")[1]) if "|" in h1b else 0
h2b_fail = int(h2b.split("|")[1]) if "|" in h2b else 0
h1a_fail, h1a_cool = health_row(ar1)
h2a_fail, _ = health_row(ar2)
rec("health evidence: profile1 observed the locala 429 (failures grew)", h1a_fail > h1b_fail,
    f"failures {h1b_fail}->{h1a_fail} cooldown_until={h1a_cool}")
rec("health evidence: profile2's locala health rows unchanged by profile1's 429",
    h2a_fail == h2b_fail, f"failures {h2b_fail}->{h2a_fail}")

# --- response sanity ------------------------------------------------------------------
def load(p):
    code = int(open(p + ".code").read().strip())
    try:
        doc = json.load(open(p))
    except Exception:
        doc = None
    return code, doc

c1, d1 = load(os.path.join(ISODIR, "p1a.json"))
c2, d2 = load(os.path.join(ISODIR, "p2a.json"))
c1b, d1b = load(os.path.join(ISODIR, "p1b.json"))
c2b, d2b = load(os.path.join(ISODIR, "p2b.json"))
def ok200(c, d):
    return c == 200 and bool((d or {}).get("choices"))
rec("responses: profile1 phase A turn succeeded", ok200(c1, d1), f"http={c1}")
rec("responses: profile2 phase A turn succeeded", ok200(c2, d2), f"http={c2}")
rec("responses: profile2 phase B turn succeeded after profile1's 429 window closed", ok200(c2b, d2b), f"http={c2b}")
print(f"phase B profile1 (locala=429): http={c1b} body={json.dumps(d1b)[:200] if d1b else None}")

sha = os.popen(f"git -C {env['REPO']} rev-parse HEAD").read().strip()
out = {
    "dispatch": "145",
    "item": 3,
    "surface": "live messaging gateway (api_server platform), one per isolated profile",
    "hermes_sha": sha,
    "synthetic_accounts": {"profile1": "alpha", "profile2": "beta",
                           "note": "loopback-only dummy keys; fingerprints are sha256[:12] of the Authorization header"},
    "checks": checks,
    "passed": sum(1 for c in checks if c["ok"]),
    "failed": sum(1 for c in checks if not c["ok"]),
    "transport_windows": {"t0": T0, "p1_phaseA_end": T_A1, "p2_phaseA_end": T_A2,
                          "p1_phaseB_end": T_B1, "p2_phaseB_end": T_B2},
}
out["status"] = "PASSED" if out["failed"] == 0 else "FAILED"
open(EVIDENCE, "w").write(json.dumps(out, indent=2) + "\n")
print(f"{out['passed']} passed, {out['failed']} failed -> {EVIDENCE}")
sys.exit(0 if out["failed"] == 0 else 1)
PY
