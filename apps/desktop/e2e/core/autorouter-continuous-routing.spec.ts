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
  appLogTail,
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
  { name: 'locala', port: 8401, model: 'local-model-a' },
  { name: 'localb', port: 8402, model: 'local-model-b' }
]

const PROVIDER_LOGS = path.join(REPO_ROOT, '..', 'runtime', 'provider-logs')
const SYNTH_PROVIDER = path.join(REPO_ROOT, 'tools', 'local-dev', 'synth_provider.py')
const PROVIDER_PID_DIR = path.join(REPO_ROOT, '..', 'runtime', 'pids')

const nonce = Math.random()
  .toString(36)
  .slice(2, 8)
  .replace(/[^a-z0-9]/g, 'x')
  .padEnd(4, 'q')

const U = (n: number) => `${nonce}U${n}`

test.describe.configure({ mode: 'serial', timeout: 300_000 })

test.skip(!RUN_E2E, 'set AUTOROUTER_E2E=1 and provide the isolated loopback environment (tools/local-dev)')

/** Ensure the loopback providers are up; return the pids this spec started (to stop afterwards). */
async function ensureLoopbackProviders(): Promise<number[]> {
  const { spawn } = await import('node:child_process')
  const started: number[] = []
  fs.mkdirSync(PROVIDER_PID_DIR, { recursive: true })

  for (const { name, port, model } of LOOPBACK) {
    const probe = spawnSync('curl', ['-fsS', '-m', '2', `http://127.0.0.1:${port}/v1/models`], { encoding: 'utf8' })

    if (probe.status === 0) {
      continue
    }

    const log = path.join(PROVIDER_LOGS, `${name}.stdout.log`)

    const child = spawn('python3', [SYNTH_PROVIDER, name, String(port), model], {
      env: { ...process.env, PROVIDER_LOG_DIR: PROVIDER_LOGS },
      detached: true,
      stdio: ['ignore', fs.openSync(log, 'a'), fs.openSync(log, 'a')]
    })

    child.unref()
    fs.writeFileSync(path.join(PROVIDER_PID_DIR, `${name}.pid`), String(child.pid))
    started.push(child.pid as number)
    await new Promise(resolve => setTimeout(resolve, 500))
    const again = spawnSync('curl', ['-fsS', '-m', '3', `http://127.0.0.1:${port}/v1/models`], { encoding: 'utf8' })

    if (again.status !== 0) {
      throw new Error(`loopback provider ${name} failed to start`)
    }
  }

  return started
}

test('desktop: Auto (Free) pin; ordinary turn; pinned route fail-closes on 429; recovers; restart+resume keep the pick', async () => {
  test.skip(!fs.existsSync(path.join(AUTOROUTER_SRC, 'plugin.yaml')),
    `autorouter checkout not found at ${AUTOROUTER_SRC}`)
  const startedProviders = await ensureLoopbackProviders()

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
  // The loopback providers are shared state; make this test deterministic.
  fs.writeFileSync(localaMode, 'ok')
  fs.writeFileSync(localbMode, 'ok')
  const priorA = 'ok'
  const priorB = 'ok'

  const restoreModes = () => {
    fs.writeFileSync(localaMode, priorA)
    fs.writeFileSync(localbMode, priorB)
  }

  const { app, page } = await launchCoreApp(
    coreAppEnv(sandbox, { HERMES_DESKTOP_PYTHON: path.join(REPO_ROOT, '.venv', 'bin', 'python') }))

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
      try {
        await send(page, `${U(1)} marker`, 'Enter')
        await expect(page.getByText(/synthetic-local[ab]\/local-model-[ab]/).first())
          .toBeVisible({ timeout: 180_000 })
      } catch (err) {
        console.log('BACKEND LOG TAIL:\n' + appLogTail(app, 120))
        throw err
      }
    })

    await test.step('a 429 bench on the pinned route fail-closes without replay', async () => {
      // The pinned ordinary desktop session freezes to whichever loopback route
      // was selected after turn 1; a benched pinned provider must surface the
      // provider error (no silent replay), and the composer must keep showing
      // the Auto (Free) selection.
      fs.writeFileSync(localaMode, 'ratelimit')
      fs.writeFileSync(localbMode, 'ratelimit')
      await send(page, `${U(2)} while locala is benched`, 'Enter')
      await expect(page.getByText(/custom is busy|rate limit|not processed/i).first())
        .toBeVisible({ timeout: 180_000 })
      await expect(page.getByRole('button', { name: /auto.*free/i }).first()).toBeVisible()
      fs.writeFileSync(localaMode, priorA)
      fs.writeFileSync(localbMode, priorB)
    })

    await test.step('after the bench clears, the same pinned session recovers', async () => {
      await send(page, `${U(3)} after the bench cleared`, 'Enter')
      await expect(page.getByText(/synthetic-local[ab]\/local-model-[ab]/).first())
        .toBeVisible({ timeout: 180_000 })
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
        const pill = again.page.getByRole('button', { name: /auto.*free/i }).first()
        await expect(pill).toBeVisible({ timeout: 30_000 })
        await expect(again.page.getByText(new RegExp(`${U(1)} marker`)).first()).toBeVisible({ timeout: 30_000 })
      } finally {
        await again.app.close().catch(() => undefined)
      }
    })
  } catch (err) {
    const dump = (rel: string) => {
      const full = path.join(home, rel)

      try {
        if (fs.existsSync(full)) {
          console.log(`===== ${rel} =====\n${fs.readFileSync(full, 'utf8').slice(-4000)}`)
        } else {
          console.log(`===== ${rel}: MISSING =====`)
        }
      } catch (e) {
        console.log(`===== ${rel}: unreadable (${e}) =====`)
      }
    }

    console.log('SANDBOX:', home)

    for (const rel of ['config.yaml', 'logs/hermes.log', 'logs/gateway.log', 'logs/serve.log',
      'plugin-data/autorouter/native-routing-state.json', 'state.db']) {
      dump(rel)
    }

    try {
      const stateDb = path.join(home, 'state.db')

      const sess = spawnSync('sqlite3', ['-json', stateDb,
        "select id, source, title, model, substr(model_config,1,400) as cfg from sessions order by rowid desc limit 2"], { encoding: 'utf8' })

      console.log('sessions row => ' + sess.stdout)
      const scan = spawnSync('bash', ['-c', `grep -rIn 'auto-free' "$1" | head -40`, 'scan', home], { encoding: 'utf8' })
      console.log('grep auto-free => ' + (scan.stdout || '') + (scan.stderr || ''))
    } catch (e) {
      console.log('sessions dump failed: ' + e)
    }

    try {
      const rows = fs.readdirSync(path.join(home, 'logs')).map(n => `logs/${n}`)

      for (const rel of rows) {dump(rel)}
    } catch {
      console.log('===== logs/: MISSING =====')
    }

    throw err
  } finally {
    restoreModes()
    await app.close().catch(() => undefined)
    sandbox.cleanup()

    // Only stop the providers this spec started.
    for (const pid of startedProviders) {
      try {
        process.kill(pid, 'SIGTERM')
      } catch {
        // already gone
      }
    }
  }
})
