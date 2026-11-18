# Dispatch 140 — isolated local AutoRouter development environment (operator runbook)

A fully isolated local development setup for the AutoRouter ↔ Hermes integration.
Everything runs on this machine; nothing here touches your everyday Hermes
install, your real provider credentials, or GitHub review/publication flows.

**Isolation contract**

- `HERMES_HOME` is its own disposable tree (absolute path passed explicitly).
- Providers are `locala` (`127.0.0.1:8401`) and `localb` (`127.0.0.1:8402`),
  enforced loopback-only synthetic OpenAI-compatible servers with zero
  credentials. `ollama` (`127.0.0.1:11434`) is reused **as-is**; this harness
  never restarts or reconfigures your shared Ollama service.
- The AutoRouter plugin is loaded from a *pinned local checkout*, not from any
  published artifact.
- GitHub pushes go only to `diegorhoger/hermes-agent` (branch
  `local/continuous-autorouting-140`) and `diegorhoger/autorouter` (same
  branch). No PRs are opened; no canonical branch is touched.

**Layout**

```
localdev-140/                          (persistent, user-owned)
  hermes-agent/                        Hermes checkout, branch local/continuous-autorouting-140
  autorouter/                          AutoRouter checkout, branch local/continuous-autorouting-140
  hermes-home/                         isolated HERMES_HOME (config + plugin-data + state.db)
  runtime/                             pidfiles, provider jsonl logs, status/verify reports
  runtime/provider-logs/               synthetic provider traffic + mode switches
  acceptance-scripts/                  preserved Dispatch-139 scripts
  acceptance-evidence/                 sanitized evidence of Dispatch 139/140 runs
```

**Start / verify / stop**

```bash
tools/local-dev/start-local.sh    # starts local providers + loads isolated profile
tools/local-dev/verify-local.sh   # read-only checks; exits non-zero on failure
tools/local-dev/stop-local.sh     # SIGTERM -> bounded wait -> SIGKILL (verified owners only)
```

`start-local.sh` refuses to bind a port held by an unrecognised process and
never restarts Ollama. `stop-local.sh` verifies PID + command line before
sending any signal and reports a foreign process instead of killing it.

**Talking to the isolated profile**

```bash
tools/local-dev/chat-local.sh "hello"           # ordinary (per-turn routed) session
tools/local-dev/chat-local.sh "hello again"     # resumes the same session
tools/local-dev/chat-local.sh "hello again" --new
```

Each turn re-resolves the physical route through the sanctioned
`pre_inference_route` consumer; the visible selection stays `Auto (Free)`.

**Desktop**

The Desktop application can be launched against the isolated profile exactly
like the normal `Apps > Hermes` entry with
`HERMES_HOME=<localdev-140>/hermes-home` set, or exercised headlessly via the
Playwright core spec shipped in `apps/desktop/e2e/core/` on this branch. See `apps/desktop/e2e/core/README.md` there and `apps/desktop/e2e/core/autorouter-continuous-routing.spec.ts` for the gated acceptance spec.

**Dispatch-140 Linux lane in CI**

`diegorhoger/hermes-agent` runs a secret-free, validation-only lane on
`local/continuous-autorouting-140` (turn-boundary, replay and gateway tests).
It publishes nothing. A sibling suite for the plugin itself used to live in
`diegorhoger/autorouter`, but this account has exhausted its Actions
minutes quota there, so the plugin suite runs locally (macOS) — 615 tests
passed, see `acceptance-evidence/`.

**Configuration of the isolated provider pool**

`hermes-home/config.yaml` carries the pool `providers: [ollama, locala, localb]`
with `model_preference` locked down to `ollama/ornith-1.5:9b`,
`locala/local-model-a`, `localb/local-model-b`. The three are fail-closed
loopback/chat routes; nobody burns paid API credit through this profile.

**Troubleshooting**

- Provider not serving → check `runtime/provider-logs/{locala,localb}.jsonl`
  and `provider-logs/*.mode`; `stop-local.sh && start-local.sh` is cheap.
- `Auto (Free)` does not route → run `verify-local.sh`; the plugin must report
  `pre_inference_route consumer registered`.
- Mid-turn routing changes are expected on ordinary sessions only. Sessions
  pinned to a specific physical model are sticky by design; the fail-closed
  refusal text is shown, never a fallback to a different provider.
