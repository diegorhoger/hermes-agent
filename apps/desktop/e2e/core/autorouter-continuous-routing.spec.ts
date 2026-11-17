/**
 * AUTOROUTER — dispatch 140 acceptance (local development only).
 *
 * Drives the real Electron Desktop against the isolated AutoRouter
 * development profile on this machine: a Desktop-attached Hermes gateway
 * whose virtual `Auto (Free)` selection re-resolves per turn through the
 * sanctioned pre_inference_route consumer. Proven here:
 *   1. the composer model pill shows the virtual `Auto (Free)` row;
 *   2. picking it keeps the selection visible and routes each turn to an
 *      eligible verified-free loopback route (locala/local-model-a or
 *      localb/local-model-b — synthetic, zero cost);
 *   3. a controlled eligibility change (locala benched to HTTP 429) makes the
 *      next turn select another eligible route;
 *   4. the conversation survives close + relaunch with its context intact.
 *
 * Gated on AUTOROUTER_E2E=1: needs (a) the loopback providers up
 * (tools/local-dev/start-local.sh), (b) the sibling autorouter checkout
 * (AUTOROUTER_SRC or <repo>/../autorouter), and (c) a display. Never runs in
 * upstream CI.
 */

import { spawnSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as path from 'node:path'

import { expect, test } from '@playwright/test'

import {
  coreAppEnv,
  createCoreSandbox,
  currentSessionId,
  launchCoreApp,
  REPO_ROOT,
  send,
  waitForInteractive
} from './harness'

const RUN_E2E = process.env.AUTOROUTER_E2E === '1'
const AUTOROUTER_SRC = process.env.AUTOROUTER_SRC || path.join(REPO_ROOT, '..', 'autorouter')
const LOOPBACK = [
  { name: 'locala', url: 'http://127.0.0.1:8401' },
  { name: 'localb', url: 'http://127.0.0.1:8402' }
]
const PROVIDER_LOGS = path.join(REPO_ROOT, '..', 'runtime', 'provider-logs')

const nonce = Math.random()
  .toString(36)
  .slice(2, 8)
  .replace(/[^a-z0-9]/g, 'x')
  .padEnd(4, 'q')
const U = (n: number) => `${nonce}U${n}`

test.describe.configure({ mode: 'serial', timeout: 300_000 })

test.skip(!RUN_E2E, 'set AUTOROUTER_E2E=1 and provide the isolated loopback environment (tools/local-dev)')

test('desktop: Auto (Free) routes ordinary turns and failover survives a 429 flip', async () => {
  test.skip(!fs.existsSync(path.join(AUTOROUTER_SRC, 'plugin.yaml')),
    `autorouter checkout not found at ${AUTOROUTER_SRC}`)
  const health = LOOPBACK.map(({ name, url }) =>
    [name, spawnSync('curl', ['-fsS', '-m', '2', `${url}/v1/models`], { encoding: 'utf8' })] as const)
  for (const [name, probe] of health) {
    test.skip(probe.status !== 0, `loopback provider ${name} not healthy — run tools/local-dev/start-local.sh`)
  }

  const sandbox = createCoreSandbox('autorouter')
  const home = sandbox.hermesHome
  const pluginsDir = path.join(home, 'plugins')
  fs.mkdirSync(pluginsDir, { recursive: true })
  fs.symlinkSync(AUTOROUTER_SRC, path.join(pluginsDir, 'autorouter'), 'dir')

  // The isolated development profile: loopback providers only, no real credentials.
  fs.writeFileSync(path.join(home, 'config.yaml'), `model:
  provider: ollama
  default: ornith-1.5:9b
  base_url: http://127.0.0.1:11434/v1
providers:
  ollama:
    base_url: http://127.0.0.1:11434/v1
    api_key: sk-ollama-local-dummy-not-a-secret
  locala:
    base_url: http://127.0.0.1:8401/v1
    api_key: sk-local-a-dummy-not-a-secret
  localb:
    base_url: http://127.0.0.1:8402/v1
    api_key: sk-local-b-dummy-not-a-secret
plugins:
  enabled: [autorouter]
  entries:
    autorouter:
      settings:
        enabled: true
        enable_chat_router: true
        enable_native_routing: true
        free_only: true
        providers: [locala, localb]
        requires_tools: false
        min_context: 2048
        cooldown_seconds: 2
        policy: auto
        model_preference:
          mode: allowlist
          include:
            - locala/local-model-a
            - localb/local-model-b
onboarding:
  run: false
nous:
  guest: false
`)

  // Seed the AutoRouter store with verified-free evidence for the two
  // synthetic loopback routes only (development evidence, never entitlement).
  const seed = spawnSync(
    process.env.HERMES_E2E_PYTHON || 'python3',
    [path.join(REPO_ROOT, 'tools', 'local-dev', 'seed-local-catalog.py')],
    {
      encoding: 'utf8',
      env: {
        ...process.env,
        HERMES_DEV_ROOT: path.dirname(home),
        HERMES_DEV_HOME: home,
        AUTOROUTER_SRC
      }
    }
  )
  expect(seed.status, `seed-local-catalog failed: ${seed.stdout}\n${seed.stderr}`).toBe(0)

  // Benching state files: isolate 429 flips to this test's runtime dir.
  const localaMode = path.join(PROVIDER_LOGS, 'locala.mode')
  const localbMode = path.join(PROVIDER_LOGS, 'localb.mode')
  const priorA = fs.existsSync(localaMode) ? fs.readFileSync(localaMode, 'utf8') : 'ok'
  const priorB = fs.existsSync(localbMode) ? fs.readFileSync(localbMode, 'utf8') : 'ok'
  const restoreModes = () => {
    fs.writeFileSync(localaMode, priorA)
    fs.writeFileSync(localbMode, priorB)
  }

  const readMode = (name: 'locala' | 'localb') =>
    (fs.existsSync(path.join(PROVIDER_LOGS, `${name}.mode`))
      ? fs.readFileSync(path.join(PROVIDER_LOGS, `${name}.mode`), 'utf8').trim()
      : 'ok')

  const { app, page } = await launchCoreApp(coreAppEnv(sandbox))
  try {
    await waitForInteractive(app, page)

    await test.step('composer model pill opens and Auto (Free) is listed', async () => {
      const pill = page.locator('[data-tour="model-pill"]').first()
      await expect(pill).toBeVisible({ timeout: 60_000 })
      await pill.click()
      await expect(page.getByText('Auto (Free)', { exact: false }).first()).toBeVisible({ timeout: 15_000 })
      await page.getByText('Auto (Free)', { exact: false }).first().click()
      // The pill now reflects the virtual selection, not a physical pick.
      await expect(pill).toContainText(/Auto/i, { timeout: 15_000 })
    })

    await test.step('first turn is routed and answered by a loopback route', async () => {
      await send(page, `${U(1)} marker`, 'Enter')
      await expect(page.getByText(/synthetic-local[ab]\/local-model-[ab]/).first())
        .toBeVisible({ timeout: 180_000 })
    })

    await test.step('a 429 flip on locala makes the next turn select another route', async () => {
      test.skip(readMode('locala') !== 'ok', 'locala not healthy enough for the failover leg')
      fs.writeFileSync(localaMode, 'ratelimit')
      await send(page, `${U(2)} after the flip`, 'Enter')
      await expect(page.getByText(/synthetic-localb\/local-model-b/).first())
        .toBeVisible({ timeout: 180_000 })
      fs.writeFileSync(localaMode, priorA)
    })

    await test.step('close and relaunch: the session and selection survive', async () => {
      const session = await currentSessionId(page)
      expect(session).not.toBe('')
      await app.close()
      const again = await launchCoreApp(coreAppEnv(sandbox))
      try {
        await waitForInteractive(again.app, again.page)
        await again.page.reload()
        await waitForInteractive(again.app, again.page)
        // The pinned virtual selection survives the relaunch on the same session.
        const pill = again.page.locator('[data-tour="model-pill"]').first()
        await expect(pill).toContainText(/Auto/i, { timeout: 30_000 })
        await expect(again.page.getByText(new RegExp(`${U(1)} marker`)).first()).toBeVisible({ timeout: 30_000 })
      } finally {
        await again.app.close().catch(() => undefined)
      }
    })
  } finally {
    restoreModes()
    await app.close().catch(() => undefined)
    sandbox.cleanup()
  }
})
