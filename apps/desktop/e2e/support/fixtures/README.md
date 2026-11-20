# AutoRouter private RC E2E fixtures

This directory contains the offline pinned AutoRouter plugin archive required by
`apps/desktop/e2e/autorouter-private-rc.spec.ts` for private-RC acceptance runs.

- Pinned SHA: `cdaef496501eadccdde1ac250ef1a18d5b1fd402`
- Archive: `autorouter-pinned-cdaef496501eadccdde1ac250ef1a18d5b1fd402.tar.gz`
- Expected SHA256: `1579cab56da7032f37e6839eb86c94d158c0e01dc7d2caf49c10df156ec5485c`

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
