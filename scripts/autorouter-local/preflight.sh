#!/usr/bin/env bash
# Refuse to package an upstream-only branch as an AutoRouter-enabled Hermes installation.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
expected_branch="integration/autorouter-local-macos-20261121"
actual_branch="$(git branch --show-current)"
if [[ "$actual_branch" != "$expected_branch" ]]; then
  printf 'REFUSE: expected branch %s, got %s\n' "$expected_branch" "$actual_branch" >&2
  exit 2
fi
for path in \
  hermes_cli/autorouter_picker.py \
  tests/hermes_cli/test_autorouter_picker_host.py \
  tests/hermes_cli/test_pre_inference_route_host.py \
  agent/credential_scope.py \
  agent/quota_observation.py \
  agent/quota_fetch.py \
  tests/agent/test_quota_fetch_https_deadline.py \
  apps/desktop/src/app/shell/model-catalog-menu.test.tsx; do
  if [[ ! -f "$path" ]]; then
    printf 'REFUSE: integration component missing: %s\n' "$path" >&2
    exit 3
  fi
done
if ! grep -q 'PRE_INFERENCE_ROUTE_MIDDLEWARE' hermes_cli/middleware.py; then
  printf 'REFUSE: pre-inference seam not present\n' >&2
  exit 4
fi
if ! grep -q '_bind_deadline_to_socket' agent/quota_fetch.py; then
  printf 'REFUSE: reviewed HTTPS deadline repair not present\n' >&2
  exit 5
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  printf 'REFUSE: tracked worktree changes; use a clean integration commit\n' >&2
  exit 6
fi
printf 'PASS: integration source guard at %s\n' "$(git rev-parse HEAD)"
printf 'NOTE: this proves source presence, NOT native installation, authorization or live quota acceptance.\n'
