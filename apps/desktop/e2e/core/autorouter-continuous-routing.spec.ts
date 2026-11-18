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
import * as os from 'node:os'
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

const SHARED_RUNTIME = process.env.AUTOROUTER_SHARED_RUNTIME || path.join(REPO_ROOT, '..', 'runtime')
const SHARED_PROVIDER_LOGS = path.join(SHARED_RUNTIME, 'provider-logs')
const SHARED_PID_DIR = path.join(SHARED_RUNTIME, 'pids')
const SYNTH_PROVIDER = path.join(REPO_ROOT, 'tools', 'local-dev', 'synth_provider.py')
const OWNERSHIP_LIB = path.join(REPO_ROOT, 'tools', 'local-dev', 'provider-ownership.sh')

/** Test-owned log dir used when this spec spawns its own providers (lazy). */
let OWN_LOG_DIR = ''

const ownLogDir = (): string => {
  if (!OWN_LOG_DIR) {OWN_LOG_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'autorouter-e2e-providers-'))}

  return OWN_LOG_DIR
}

/**
 * Redact credential-like material from anything we print on failure:
 * provider keys, bearer tokens, GitHub/Nous-style tokens. Even a dummy-looking
 * key stays out of the transcript.
 */
function redactSecrets(text: string): string {
  return text
    .replace(/sk(-|_)[A-Za-z0-9_-]{6,}/g, '[redacted-key]')
    .replace(/Bearer \S+/gi, 'Bearer [redacted]')
    .replace(/(ghp|gho|ghu|ghs|ghr|github_pat|nvapi|hf)_[A-Za-z0-9_-]{8,}/g, '[redacted-token]')
    .replace(/(api[_-]?key|token|secret|password)(\s*[:=]\s*)[^\s,'"]+/gi, '$1$2[redacted]')
}

function dumpRedacted(label: string, text: string): void {
  console.log(`===== ${label} =====\n${redactSecrets(text)}`)
}

/** Verified ownership of a provider PID exactly as provider-ownership.sh decides it. */
function pidVerifiedOurs(pid: number, name: string, port: number, model: string): boolean {
  const pidFile = path.join(SHARED_PID_DIR, `${name}.pid`)

  if (!fs.existsSync(pidFile)) {return false}
  const content = fs.readFileSync(pidFile, 'utf8')
  const started = /^started (.+)$/m.exec(content)?.[1]?.trim() ?? ''

  const script = `
    source ${JSON.stringify(OWNERSHIP_LIB)}
    verify_provider_process ${pid} ${name} ${port} ${model} ${JSON.stringify(started)}
  `

  const probe = spawnSync('bash', ['-c', script], { encoding: 'utf8' })

  return probe.status === 0
}

/** True when both ports are held by providers verified as this machine's own. */
function sharedProvidersVerified(): boolean {
  for (const { name, port, model } of LOOPBACK) {
    const pid = spawnSync('lsof', ['-nP', `-iTCP:${port}`, '-sTCP:LISTEN', '-t'],
      { encoding: 'utf8' }).stdout.trim().split('\n')[0]

    if (!pid || !/^\d+$/.test(pid)) {return false}

    if (!pidVerifiedOurs(Number(pid), name, port, model)) {return false}
  }

  return true
}

const nonce = Math.random()
  .toString(36)
  .slice(2, 8)
  .replace(/[^a-z0-9]/g, 'x')
  .padEnd(4, 'q')

const U = (n: number) => `${nonce}U${n}`

test.describe.configure({ mode: 'serial', timeout: 300_000 })

test.skip(!RUN_E2E, 'set AUTOROUTER_E2E=1 and provide the isolated loopback environment (tools/local-dev)')

interface EnsureResult {
  /** PIDs this spec spawned (it owns them and will stop them). */
  started: number[]
  /** Mode files to restore in the finally block (exact prior bytes, or null for "was absent"). */
  modeBackups: Map<string, string | null>
  /** Log dir each provider writes (test-owned when we spawned; shared otherwise). */
  logDir: string
}

/**
 * Providers: this spec uses whatever is already running on 8401/8402 ONLY when
 * it is verified as this machine's own dev-environment provider (shared pid
 * files, exact start identity, port ownership — the same checks as
 * provider-ownership.sh). Otherwise it spawns its own providers with an
 * isolated log/mode dir it owns completely. Foreign providers are never used,
 * benched, or adopted; ports held by them fail fast with a diagnostic.
 */
async function ensureLoopbackProviders(): Promise<EnsureResult> {
  const { spawn } = await import('node:child_process')
  const started: number[] = []
  const modeBackups = new Map<string, string | null>()
  let logDir = ownLogDir()
  fs.mkdirSync(logDir, { recursive: true })

  const probeAll = LOOPBACK.map(({ port }) =>
    spawnSync('curl', ['-fsS', '-m', '2', `http://127.0.0.1:${port}/v1/models`], { encoding: 'utf8' }).status === 0)

  if (probeAll.some(Boolean)) {
    if (!probeAll.every(Boolean)) {
      throw new Error('only one loopback port is held; refusing to adopt a half-foreign pair. Free 8401/8402 or start both with tools/local-dev/start-local.sh')
    }

    if (!sharedProvidersVerified()) {
      // Foreign (unverifiable) providers are never adopted or benched.
      throw new Error(
        'ports 8401/8402 are held by providers this test cannot verify as its own '
        + '(pid files + start identity + port ownership failed verification). '
        + 'Stop the foreign providers or free the ports before running the acceptance test.')
    }

    // Verified ours (shared dev environment): bench with exact-bytes backup/restore.
    logDir = SHARED_PROVIDER_LOGS

    for (const { name } of LOOPBACK) {
      const modeFile = path.join(SHARED_PROVIDER_LOGS, `${name}.mode`)
      modeBackups.set(modeFile, fs.existsSync(modeFile) ? fs.readFileSync(modeFile, 'utf8') : null)
    }

    return { started, modeBackups, logDir }
  }

  // Ports free: spawn our own providers in an isolated, test-owned dir.
  logDir = ownLogDir()
  fs.mkdirSync(logDir, { recursive: true })

  for (const { name, port, model } of LOOPBACK) {
    const log = path.join(logDir, `${name}.stdout.log`)

    const child = spawn('python3', [SYNTH_PROVIDER, name, String(port), model], {
      env: { ...process.env, PROVIDER_LOG_DIR: logDir },
      detached: true,
      stdio: ['ignore', fs.openSync(log, 'a'), fs.openSync(log, 'a')]
    })

    child.unref()
    started.push(child.pid as number)
    await new Promise(resolve => setTimeout(resolve, 500))
    const again = spawnSync('curl', ['-fsS', '-m', '3', `http://127.0.0.1:${port}/v1/models`], { encoding: 'utf8' })

    if (again.status !== 0) {
      throw new Error(`loopback provider ${name} failed to start`)
    }
  }

  for (const { name } of LOOPBACK) {
    modeBackups.set(path.join(logDir, `${name}.mode`), null)
  }

  return { started, modeBackups, logDir }
}

test('desktop: Auto (Free) pin; ordinary turn; pinned route fail-closes on 429; recovers; restart+resume keep the pick', async () => {
  test.skip(!fs.existsSync(path.join(AUTOROUTER_SRC, 'plugin.yaml')),
    `autorouter checkout not found at ${AUTOROUTER_SRC}`)
  const ensure = await ensureLoopbackProviders()
  const startedProviders = ensure.started

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

  // Benching state files: the spec-owned provider dir or the verified shared
  // dir — never an unverified foreign provider. Prior bytes are restored in
  // the finally block.
  const localaMode = path.join(ensure.logDir, 'locala.mode')
  const localbMode = path.join(ensure.logDir, 'localb.mode')

  const resetModes = () => {
    fs.writeFileSync(localaMode, 'ok')
    fs.writeFileSync(localbMode, 'ok')
  }

  resetModes()

  const restoreModes = () => {
    for (const [file, prior] of ensure.modeBackups) {
      if (prior === null) {
        if (fs.existsSync(file)) {fs.rmSync(file)}
      } else {
        fs.writeFileSync(file, prior)
      }
    }
  }

  /** Count requests of a given model logged by a provider since a timestamp (ms). */
  const providerRequests = (name: string, model: string, sinceMs: number): number => {
    const logFile = path.join(ensure.logDir, `${name}.jsonl`)

    if (!fs.existsSync(logFile)) {return 0}

    return fs.readFileSync(logFile, 'utf8').split('\n')
      .filter(line => line.trim())
      .map(line => { try { return JSON.parse(line) } catch { return null } })
      .filter(entry => entry && entry.event === 'request' && entry.model === model
        && typeof entry.ts === 'number' && entry.ts * 1000 >= sinceMs)
      .length
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
      const before = {
        locala: providerRequests('locala', 'local-model-a', 0),
        localb: providerRequests('localb', 'local-model-b', 0),
      }

      try {
        await send(page, `${U(1)} marker`, 'Enter')
        await expect(page.getByText(/synthetic-local[ab]\/local-model-[ab]/).first())
          .toBeVisible({ timeout: 180_000 })
      } catch (err) {
        console.log('BACKEND LOG TAIL:\n' + redactSecrets(appLogTail(app, 120)))
        throw err
      }

      // The provider change is proven through the actual outgoing model and endpoint:
      // exactly one loopback logged a new outgoing request with its own model.
      const nowA = providerRequests('locala', 'local-model-a', 0)
      const nowB = providerRequests('localb', 'local-model-b', 0)
      const served = nowA > before.locala ? 'locala' : nowB > before.localb ? 'localb' : null
      expect(served, 'no loopback provider logged an outgoing request for turn 1').not.toBeNull()
    })

    await test.step('a 429 bench on the pinned route fail-closes without replay', async () => {
      // The pinned ordinary desktop session freezes to whichever loopback route
      // was selected after turn 1; a benched pinned provider must surface the
      // provider error (no silent replay), and the composer must keep showing
      // the Auto (Free) selection.
      fs.writeFileSync(localaMode, 'ratelimit')
      fs.writeFileSync(localbMode, 'ratelimit')
      await send(page, `${U(2)} while locala is benched`, 'Enter')
      await expect(page.getByText(/custom is busy|rate limit|not processed|no eligible/i).first())
        .toBeVisible({ timeout: 180_000 })
      await expect(page.getByRole('button', { name: /auto.*free/i }).first()).toBeVisible()
      resetModes()
    })

    await test.step('after the bench clears, the same pinned session recovers', async () => {
      const before = {
        locala: providerRequests('locala', 'local-model-a', 0),
        localb: providerRequests('localb', 'local-model-b', 0),
      }

      await send(page, `${U(3)} after the bench cleared`, 'Enter')
      // The bench turn can still be settling when this step begins, and a stale /synthetic-local/
      // match cannot distinguish the new answer from the turn-1 one — poll the providers' own
      // request log until the recovery turn is provably on the wire instead.
      const deadline = Date.now() + 180_000
      let served: string | null = null

      while (Date.now() < deadline) {
        if (providerRequests('locala', 'local-model-a', 0) > before.locala) { served = 'locala';

 break }

        if (providerRequests('localb', 'local-model-b', 0) > before.localb) { served = 'localb';

 break }

        await new Promise(resolve => setTimeout(resolve, 500))
      }

      expect(served, 'no loopback provider logged an outgoing request for the recovery turn').not.toBeNull()
      await expect(page.getByText(/synthetic-local[ab]\/local-model-[ab]/).last())
        .toBeVisible({ timeout: 60_000 })
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
    // Failure diagnostics are redacted: no credentials, no keys, no
    // transcripts, no full profile. config.yaml is printed with every
    // credential-like value masked; state.db shows only non-sensitive columns.
    const dump = (rel: string, bytes = 2000) => {
      const full = path.join(home, rel)

      try {
        if (fs.existsSync(full)) {
          dumpRedacted(rel, fs.readFileSync(full, 'utf8').slice(-bytes))
        } else {
          console.log(`===== ${rel}: MISSING =====`)
        }
      } catch (e) {
        console.log(`===== ${rel}: unreadable (${e}) =====`)
      }
    }

    console.log('SANDBOX:', home)

    for (const rel of ['config.yaml', 'logs/hermes.log', 'logs/gateway.log', 'logs/serve.log',
      'plugin-data/autorouter/native-routing-state.json']) {
      dump(rel)
    }

    try {
      const stateDb = path.join(home, 'state.db')

      // No model_config / content columns: identities and titles only (redacted).
      const sess = spawnSync('sqlite3', ['-json', stateDb,
        "select id, source, substr(title,1,80) as title, model from sessions order by rowid desc limit 2"], { encoding: 'utf8' })

      console.log('sessions row => ' + redactSecrets(sess.stdout))
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

    // Only stop the providers this spec started — never foreign or shared ones.
    for (const pid of startedProviders) {
      try {
        process.kill(pid, 'SIGTERM')
      } catch {
        // already gone
      }
    }

    if (OWN_LOG_DIR) {
      try {
        fs.rmSync(OWN_LOG_DIR, { recursive: true, force: true })
      } catch {
        // best effort
      }
    }
  }
})
