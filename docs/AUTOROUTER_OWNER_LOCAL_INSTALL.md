# AutoRouter Hermes: owner-maintained macOS integration

**Owner repo:** https://github.com/diegorhoger/hermes-agent
**Target branch:** `integration/autorouter-local-macos-20261121`
**Source baseline:** upstream Hermes `242315ce571cde721e717a544cf8ae6c343315cf`.
**Status:** This branch starts from upstream and is **NOT installable as AutoRouter-enabled Hermes until the validated composite is pushed.** The `scripts/autorouter-local/preflight.sh` gate intentionally fails before that point.

## Publish the already-tested integration, not a reconstruction

On the owner's macOS coding-agent environment, inspect the disposable Dispatch 171 worktree at `/private/var/folders/2q/g9x952410m58_y3mmg59f2jw0000gn/T/opencode/ar-dispatch171/hermes-candidate` and verify its HEAD is `5e80fd394717658266bf8aba0c6960d101a022cb`. If it is unavailable, rebuild once from exact upstream main + PR #156277 (`96aa7df96427812a88ebc923ade584580caa9e3e`) + PR #156606 (`491708383cb65347368e0ee82408fa73b67d3fd9`) + the five picker commits ending at `f6441553fe1736cdb210723ceead9454c50179dc`. Do not import stale private-RC history.

Fetch this owner branch and **merge the runbook commit into the candidate** (the candidate shares its upstream baseline). Resolve only the expected runbook addition, run the preflight guard, then push the candidate plus runbook by **ordinary, non-force fast-forward** to this branch. If Git refuses, stop and report actual remote divergence; never rewrite remote history. Do not publish to `NousResearch/hermes-agent`.

Retain the confirmed upstream compatibility resolutions: keep `AuthorityRefusal` in `gateway/session_mutation_model.py`, preserve canonical `resume_route(...)` in `tui_gateway/agent_factory.py`, and keep both Desktop picker test blocks. Do not resurrect `RuntimeStoreError` or the obsolete session-route reader. Maintain the C1 profile-home scope and C2 real-HTTPS aggregate deadline fixes.

Pin the AutoRouter consumer to `diegorhoger/autorouter` SHA `8252022a309a8f4b887524174fe6351fbb225c15`. Do not install a mutable default-branch plugin or enable paid fallback.

## Safe Mac installation target

Build a **bundled, commit-specific Desktop variant**, not the official stable Hermes app. The repository already supports `HERMES_DESKTOP_VARIANT=bundled` and `HERMES_BUILD_COMMIT=<40-character commit SHA>`, yielding a distinct bundle ID and app/userData identity. Set `GITHUB_REPOSITORY=diegorhoger/hermes-agent`, do not set a release tag, and force publish mode `never`. Prefer the established tested private-RC packaging harness; use the repository's pinned package tooling and lockfiles.

Keep all AutoRouter profile, plugin and provider configuration in a **separate** user-owned `HERMES_HOME`, for example `$HOME/.hermes-autorouter-local`. First back up configuration; do not use or mutate the existing `$HOME/.hermes`. Do not run a global Hermes installer or change the default `hermes` launcher. The dev setup script has launcher-publication behavior in normal mode, so if using it, use only its `--runtime-only` path with isolated tool/home configuration. Do not overwrite `/Applications/Hermes.app` or the official Desktop installation. Stage the unique `.app` in `$HOME/Applications/` and verify the bundle ID before launch.

Install the SHA-pinned AutoRouter plugin into the isolated Hermes home through the supported plugin discovery mechanism, not monkeypatching or untracked global symlinks. Keep routing disabled until host capability and provider eligibility are verified. Run tests with synthetic accounts/local loopback first. Capture a record of the installed app path, bundle ID, source SHA, plugin SHA, Hermes home, command lines, test logs and rollback procedure.

## Acceptance gates before closing issues

- **#41:** verify sanctioned per-attempt credential scope and account A/B isolation. Real-account quota verification requires the owner's separate explicit permission to use real credentials and to make authenticated requests. Unknown/stale/revoked quota must remain UNKNOWN; never infer allowance from a 429.
- **#104:** demonstrate real newly created ordinary sessions with explicit opt-in, pre-client physical routing, explicit-pick authority, session isolation, fail-closed zero outbound, restart/resume, and opt-out on the installed fork.
- **#116:** demonstrate real native Desktop Auto (Free) picker selection, physical route before outbound, keyboard navigation, narrow viewport, focus/labels, and an actual VoiceOver or assistive-technology pass on the installed fork.

Offline tests and the Dispatch 171 2,055 reported passes are useful but do **not** prove a local installation or real-account/VoiceOver acceptance. Do not close issues without recorded evidence. If changing acceptance from *official Hermes release* to *owner-maintained installed fork*, document that owner-approved interpretation in each issue, without pretending an official NousResearch release exists.

## Ownership / prohibited actions

The owner authorized preparation of a local installation and commits to their own fork branch. This does **not** authorize public releases, paid inference, use of actual secrets, production replacement, credential rotation, modification of the existing Hermes installation, GitHub protection bypass, or upstream merges. Stop only at an actual missing privilege; work on other independent acceptance tasks first.

**Single handoff:** publish the verified candidate to this branch, build and install side by side on the owner's Mac, test the installed app, then report exact evidence and any real unmet acceptance. No endless status/review dispatches.
