# AutoRouter private RC E2E fixtures

This directory contains the offline pinned AutoRouter plugin archive required by
`apps/desktop/e2e/autorouter-private-rc.spec.ts` for private-RC acceptance runs.

- Pinned SHA: `8252022a309a8f4b887524174fe6351fbb225c15` (AutoRouter PR #132 merged: fail-closed host capability detection)
- Archive: `autorouter-pinned-8252022a309a8f4b887524174fe6351fbb225c15.tar.gz`
- Expected SHA256: `809bd0a721a6de90c54f98e7906f71df04f392495731b888553d4c32dfc46401`

The harness verifies the archive hash before extracting it into a disposable
temporary directory. Do not replace this archive without updating the expected
hash and pinned SHA in `apps/desktop/e2e/support/autorouter-private-rc.ts`.

Private-RC acceptance mode:
- Set `AUTOROUTER_PRIVATE_RC=1` (or `AUTOROUTER_PRIVATE_RC_ACCEPTANCE=1`) to
  require these inputs. Missing/invalid inputs fail loudly instead of skipping.
- Without private-RC mode, tests may skip when inputs are unavailable so normal
  desktop test jobs remain runnable.
- Optional overrides:
  - `AUTOROUTER_PINNED_ARCHIVE=/absolute/path/to/archive.tar.gz`
  - `AUTOROUTER_PINNED_SOURCE_DIR=/absolute/path/to/extracted-pinned-source`
  - `AUTOROUTER_E2E_EVIDENCE_DIR=/absolute/path/to/disposable-evidence-dir`
