import assert from 'node:assert/strict'

import { test } from 'vitest'

import { registerDeepLinkProtocol } from './deep-link-protocol'

function run(env: NodeJS.ProcessEnv, defaultApp = true, isolatedCommitBuild = false) {
  const calls: unknown[][] = []
  const log: string[] = []
  registerDeepLinkProtocol({
    app: { setAsDefaultProtocolClient: (...args: unknown[]) => { calls.push(args);

 return true } },
    protocol: 'hermes',
    env,
    isolatedCommitBuild,
    defaultApp,
    argv: ['/electron', '.'],
    execPath: '/electron',
    resolve: entry => `/checkout/${entry}`,
    log: line => log.push(line)
  })

  return { calls, log }
}

test('the E2E/dev guard leaves the OS hermes:// handler untouched; normal launches still register', () => {
  const skipped = run({ HERMES_DESKTOP_SKIP_PROTOCOL_REGISTRATION: '1' })
  assert.deepEqual(skipped.calls, [])
  assert.match(skipped.log.join('\n'), /registration skipped/)

  assert.deepEqual(run({}).calls, [['hermes', '/electron', ['/checkout/.']]])
  assert.deepEqual(run({}, false).calls, [['hermes']])
})


test('an isolated commit build never claims the production hermes:// handler, including when packaged', () => {
  for (const defaultApp of [false, true]) {
    const privateRun = run({}, defaultApp, true)
    assert.deepEqual(privateRun.calls, [])
    assert.match(privateRun.log.join('\n'), /isolated commit build/)
  }

  // The explicit test harness guard still wins and remains observable.
  const explicitlySkipped = run({ HERMES_DESKTOP_SKIP_PROTOCOL_REGISTRATION: '1' }, false, true)
  assert.deepEqual(explicitlySkipped.calls, [])
  assert.match(explicitlySkipped.log.join('\n'), /HERMES_DESKTOP_SKIP_PROTOCOL_REGISTRATION/)

  // A stable/canary-like noncommit product retains its historical behavior.
  assert.deepEqual(run({}, false, false).calls, [['hermes']])
})
