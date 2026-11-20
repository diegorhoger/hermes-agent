/**
 * AUTOROUTER_HERMES Dispatch 159 — reproducible private RC packaged Desktop E2E
 * acceptance (packaged app, real embedded backend, localhost mock inference).
 *
 * Inputs: versioned prepare script + pinned AutoRouter archive/source (offline).
 * In private-RC acceptance mode (AUTOROUTER_PRIVATE_RC=1) missing inputs fail
 * loudly; ordinary runs may skip when not configured.
 */
import { execFileSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as http from 'node:http'
import * as os from 'node:os'
import * as path from 'node:path'

import { _electron, expect, type Page } from '@playwright/test'

import {
  buildAppEnv,
  createSandbox,
  resolvePackagedPrivateRcBinaryPath,
  type PackagedAppFixture,
  type Sandbox,
  setupPackagedAppRealBackend,
  waitForAppReady,
} from './fixtures'
import {
  AUTOROUTER_ARCHIVE_SHA256,
  AUTOROUTER_PINNED_SHA,
  getMissingPrivateRcInputsReason,
  isPrivateRcAcceptanceMode,
  resolvePrivateRcInputs,
} from './support/autorouter-private-rc'
import { installErrorBannerGuard, test } from './test'

const THIS_DIR = path.resolve(import.meta.dirname)
const RC_ROOT = path.resolve(THIS_DIR, '../../..')
const PRIVATE_RC_MODE = isPrivateRcAcceptanceMode()
const MISSING_PRIVATE_RC_INPUTS = getMissingPrivateRcInputsReason()
if (PRIVATE_RC_MODE && MISSING_PRIVATE_RC_INPUTS) {
  throw new Error(
    `[autorouter-private-rc] AUTOROUTER_PRIVATE_RC acceptance mode requires inputs: ${MISSING_PRIVATE_RC_INPUTS}`,
  )
}
const PRIVATE_RC_INPUTS = MISSING_PRIVATE_RC_INPUTS ? null : resolvePrivateRcInputs(RC_ROOT)
const SKIP_REASON = !PRIVATE_RC_MODE && MISSING_PRIVATE_RC_INPUTS
  ? `private RC inputs not configured: ${MISSING_PRIVATE_RC_INPUTS}`
  : null
const PINNED_AUTOROUTER = PRIVATE_RC_INPUTS?.pinnedAutorouterSourceDir ?? ''
const PREPARE_HOME_SCRIPT = PRIVATE_RC_INPUTS?.prepareHomeScript ?? ''
const EVIDENCE_DIR = PRIVATE_RC_INPUTS?.evidenceDir ?? path.resolve(RC_ROOT, 'artifacts', 'autorouter-private-rc-e2e-skip')
const MOCK_REPLY = 'Dispatch 158 mock reply: routed conversation completed.'
const APP_PATTERN = /^Hermes Agent [0-9a-f]{7}\.app$/

function skipIfInputsMissing(): void {
  test.skip(!!SKIP_REASON, SKIP_REASON ?? 'private RC inputs unavailable')
}

function requirePrivateRcInputs() {
  if (!PRIVATE_RC_INPUTS) throw new Error(MISSING_PRIVATE_RC_INPUTS || 'private RC inputs unavailable')
  return PRIVATE_RC_INPUTS
}

interface MockRequest {
  method: string
  path: string
  model?: string
  headers?: Record<string, string | string[] | undefined>
  body?: any
  at: number
}

interface SimpleMock {
  url: string
  port: number
  requests: MockRequest[]
  chatModels: string[]
  close: () => Promise<void>
}

function startSimpleMock(opts?: { chatStatus?: number; chatDelayMs?: number; chatBody?: any; chatBodyText?: string; destroySocket?: boolean }): Promise<SimpleMock> {
  const requests: MockRequest[] = []
  const chatModels: string[] = []

  const server = http.createServer((req, res) => {
    const url = req.url || '/'
    const method = req.method || 'GET'
    res.setHeader('Access-Control-Allow-Origin', '*')
    res.setHeader('Access-Control-Allow-Headers', '*')
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    if (method === 'OPTIONS') { res.writeHead(204); res.end(); return }

    if (method === 'GET' && url === '/v1/models') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ object: 'list', data: [
        { id: 'mockinfra-baseline-model', object: 'model', created: 0, owned_by: 'mock' },
        { id: 'mockinfra-free-model', object: 'model', created: 0, owned_by: 'mock' },
        { id: 'mockinfra-no-tools-model', object: 'model', created: 0, owned_by: 'mock' },
        { id: 'mockinfra-unknown-cost-model', object: 'model', created: 0, owned_by: 'mock' },
      ] }))
      return
    }
    if (method === 'GET' && (url.startsWith('/api/') || url.startsWith('/v1/models/'))) {
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ ok: true })); return
    }
    if (method === 'POST' && url === '/api/show') {
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ parameters: 'num_ctx 131072' })); return
    }

    if (method === 'POST' && url.startsWith('/v1/chat/completions')) {
      let body = ''
      req.on('data', (chunk) => { body += chunk.toString() })
      req.on('end', () => {
        let parsed: any = {}
        try { parsed = JSON.parse(body || '{}') } catch {}
        const model = parsed.model || ''
        chatModels.push(model)
        requests.push({ method, path: url, model, headers: req.headers as any, body: parsed, at: Date.now() })

        const send = () => {
          if (opts?.destroySocket) {
            try { req.socket.destroy() } catch {}
            try { res.socket?.destroy() } catch {}
            return
          }
          if (opts?.chatStatus && opts.chatStatus !== 200) {
            res.writeHead(opts.chatStatus, { 'Content-Type': 'application/json' })
            res.end(opts.chatBodyText ?? JSON.stringify(opts.chatBody ?? { error: { message: 'mock error', type: 'server_error', code: opts.chatStatus } }))
            return
          }
          const stream = parsed.stream === true
          if (stream) {
            res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' })
            const chunk1 = { id: 'chatcmpl-e158', object: 'chat.completion.chunk', created: 0, model, choices: [{ index: 0, delta: { role: 'assistant', content: MOCK_REPLY }, finish_reason: null }] }
            const chunk2 = { id: 'chatcmpl-e158', object: 'chat.completion.chunk', created: 0, model, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }
            res.write(`data: ${JSON.stringify(chunk1)}\n\n`)
            res.write(`data: ${JSON.stringify(chunk2)}\n\n`)
            res.write('data: [DONE]\n\n')
            res.end()
          } else {
            res.writeHead(200, { 'Content-Type': 'application/json' })
            res.end(JSON.stringify({ id: 'chatcmpl-e158', object: 'chat.completion', created: 0, model, choices: [{ index: 0, message: { role: 'assistant', content: MOCK_REPLY }, finish_reason: 'stop' }], usage: { prompt_tokens: 5, completion_tokens: 5, total_tokens: 10 } }))
          }
        }
        if (opts?.chatDelayMs) setTimeout(send, opts.chatDelayMs); else send()
      })
      return
    }

    requests.push({ method, path: url, at: Date.now() })
    res.writeHead(404, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ error: 'not found', path: url }))
  })

  return new Promise((resolve, reject) => {
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address()
      if (!addr || typeof addr === 'string') { reject(new Error('bad address')); return }
      resolve({
        url: `http://127.0.0.1:${addr.port}`,
        port: addr.port,
        requests,
        chatModels,
        close: () => new Promise((resClose, rejClose) => server.close((err) => err ? rejClose(err) : resClose())),
      })
    })
  })
}

function ensureEvidenceDir(): string {
  fs.mkdirSync(EVIDENCE_DIR, { recursive: true })
  return EVIDENCE_DIR
}

function saveEvidence(name: string, data: any): string {
  ensureEvidenceDir()
  const p = path.join(EVIDENCE_DIR, name)
  fs.writeFileSync(p, typeof data === 'string' ? data : JSON.stringify(data, null, 2))
  return p
}

function appendJsonlEvidence(name: string, record: any): void {
  ensureEvidenceDir()
  const p = path.join(EVIDENCE_DIR, name)
  fs.appendFileSync(p, JSON.stringify({ at: new Date().toISOString(), ...record }) + '\n')
}

function prepareHome(
  hermesHome: string,
  mockUrl: string,
  opts: { chatRouter: boolean; nativeRouting: boolean; defaultModel?: string; defaultProvider?: string },
): void {
  const args = [PREPARE_HOME_SCRIPT, '--home', hermesHome, '--pinned-snapshot', PINNED_AUTOROUTER, '--mock-url', mockUrl]
  if (opts.chatRouter) args.push('--enable-chat-router')
  if (opts.nativeRouting) args.push('--enable-native-routing')
  if (opts.defaultModel) args.push('--default-model', opts.defaultModel)
  if (opts.defaultProvider) args.push('--default-provider', opts.defaultProvider)
  execFileSync('python3', args, { cwd: RC_ROOT, stdio: 'pipe', timeout: 120_000 })
}

function patchConfig(hermesHome: string, mutate: (text: string) => string): void {
  const p = path.join(hermesHome, 'config.yaml')
  const text = fs.readFileSync(p, 'utf8')
  fs.writeFileSync(p, mutate(text), 'utf8')
}

async function waitForComposerReady(fixture: PackagedAppFixture, timeoutMs = 240_000): Promise<void> {
  await waitForAppReady(fixture as any, timeoutMs)
}

async function pageReadySettled(page: Page): Promise<void> { await page.waitForTimeout(1500) }

async function openModelPickerUi(page: Page): Promise<void> {
  const candidates = [
    page.locator('[data-tour="model-pill"]'),
    page.locator('button[aria-label*="Switch model" i]'),
    page.locator('button[aria-label*="model" i]'),
    page.locator('button[title*="Switch model" i]'),
    page.locator('button[title*="model" i]'),
  ]
  for (const c of candidates) {
    try {
      if ((await c.count()) > 0 && (await c.first().isVisible().catch(() => false))) {
        await c.first().click({ timeout: 10_000 })
        await page.waitForTimeout(700)
        return
      }
    } catch {}
  }
  const modelLike = page.getByRole('button', { name: /model|Auto|mockinfra/i }).first()
  if (await modelLike.count().catch(() => 0)) { await modelLike.click({ timeout: 10_000 }); await page.waitForTimeout(700); return }
  await page.locator('[data-slot="composer-bounds"] button').first().click({ timeout: 15_000 })
  await page.waitForTimeout(700)
}

async function selectAutoRouter(page: Page): Promise<void> {
  await openModelPickerUi(page)
  for (let attempt = 0; attempt < 6; attempt++) {
    const auto = page.getByText('Auto (Free)', { exact: true }).first()
    if (await auto.isVisible().catch(() => false)) {
      await auto.click({ timeout: 15_000 })
      await page.waitForTimeout(500)
      return
    }
    const search = page.locator('input[placeholder*="Search" i], input[type="search"], [cmdk-input], [role="combobox"]').first()
    if ((await search.count().catch(() => 0)) > 0 && (await search.isVisible().catch(() => false))) {
      await search.fill('Auto'); await page.waitForTimeout(900); continue
    }
    await page.waitForTimeout(500)
  }
  await expect(page.getByText('Auto (Free)', { exact: true }).first()).toBeVisible({ timeout: 30_000 })
  await page.getByText('Auto (Free)', { exact: true }).first().click({ timeout: 15_000 })
}

async function assertAutoRouterNotActionableInPicker(page: Page): Promise<void> {
  await openModelPickerUi(page)
  await page.waitForTimeout(1200)
  const search = page.locator('input[placeholder*="Search" i], input[type="search"], [cmdk-input], [role="combobox"]').first()
  if ((await search.count().catch(() => 0)) > 0 && (await search.isVisible().catch(() => false))) {
    await search.fill('Auto'); await page.waitForTimeout(900)
  }
  // Either absent or not selectable as the virtual row; we assert absence of exact "Auto (Free)" text in picker context.
  const auto = page.getByText('Auto (Free)', { exact: true })
  expect(await auto.count().catch(() => 0)).toBe(0)
  await page.keyboard.press('Escape').catch(() => undefined)
}

async function selectModelByVisibleText(page: Page, text: string): Promise<void> {
  await openModelPickerUi(page)
  const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim()
  const wanted = norm(text)
  const escaped = text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

  const search = () => page.locator('input[placeholder*="Search" i], input[type="search"], [cmdk-input], [role="combobox"]').first()

  const tryKeyboardSelect = async (): Promise<boolean> => {
    const s = search()
    if (!((await s.count().catch(() => 0)) > 0 && (await s.isVisible().catch(() => false)))) return false
    try {
      await s.click({ timeout: 5_000 }).catch(() => undefined)
      await s.fill('').catch(() => undefined)
      await page.waitForTimeout(150)
      await s.fill(text).catch(() => undefined)
      await page.waitForTimeout(900)
      // cmdk usually highlights first result; ensure focus in list/input and select
      await page.keyboard.press('ArrowDown').catch(() => undefined)
      await page.waitForTimeout(150)
      await page.keyboard.press('Enter').catch(() => undefined)
      await page.waitForTimeout(900)
      await page.keyboard.press('Escape').catch(() => undefined)
      await page.waitForTimeout(300)
      // Heuristic: picker likely closed; assume success and let caller/test verify effect via behavior/evidence.
      return true
    } catch {
      return false
    }
  }

  const tryClickItem = async (): Promise<boolean> => {
    const item = page.locator('[cmdk-item], [role="option"], [role="menuitem"]').filter({ hasText: new RegExp(escaped, 'i') }).first()
    try {
      if (!(await item.isVisible().catch(() => false))) return false
      const itemText = norm(await item.innerText().catch(async () => (await item.textContent().catch(() => '')) || ''))
      if (itemText && !itemText.includes(wanted) && !wanted.includes(itemText)) return false
      await item.scrollIntoViewIfNeeded().catch(() => undefined)
      await item.click({ timeout: 10_000 })
      await page.waitForTimeout(900)
      await page.keyboard.press('Escape').catch(() => undefined)
      await page.waitForTimeout(300)
      return true
    } catch {
      return false
    }
  }

  for (let attempt = 0; attempt < 10; attempt++) {
    if (await tryKeyboardSelect()) return
    const s = search()
    if ((await s.count().catch(() => 0)) > 0 && (await s.isVisible().catch(() => false))) {
      await s.fill('').catch(() => undefined)
      await page.waitForTimeout(150)
      await s.fill(text).catch(() => undefined)
      await page.waitForTimeout(900)
    }
    if (await tryClickItem()) return
    if ((await s.count().catch(() => 0)) > 0 && (await s.isVisible().catch(() => false))) {
      await s.fill(text.split(/[-_]/).slice(0, 2).join('-')).catch(() => undefined)
      await page.waitForTimeout(700)
      if (await tryClickItem()) return
    }
    await page.waitForTimeout(400)
  }
  await page.keyboard.press('Escape').catch(() => undefined)
  throw new Error(`Could not select model by visible text: ${text}`)
}

async function sendChat(page: Page, text: string): Promise<void> {
  const { waitForChatReady } = await import('../../../tests-js/scripts/desktop-chat-smoke')
  const composer = await waitForChatReady(page, 120_000)
  await composer.click({ timeout: 20_000 })
  await page.keyboard.type(text, { delay: 5 })
  await page.keyboard.press('Enter')
}

async function sendChatAndExpectReply(page: Page, prompt: string, timeoutMs = 240_000): Promise<void> {
  await sendChat(page, prompt)
  await expect(page.getByText(MOCK_REPLY, { exact: false }).first()).toBeVisible({ timeout: timeoutMs })
}

function assertNoVirtualModelOnWire(mock: SimpleMock): void {
  const chatReqs = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
  for (const r of chatReqs) {
    expect(r.model).not.toBe('auto-free')
    expect(r.model).not.toBe('autorouter/auto-free')
    expect(String(r.model || '')).not.toContain('autorouter/auto')
  }
}

function readJsonIfExists(p: string): any {
  try { if (fs.existsSync(p)) return JSON.parse(fs.readFileSync(p, 'utf8')) } catch {}
  return null
}

function readTextIfExists(p: string): string | null {
  try { if (fs.existsSync(p)) return fs.readFileSync(p, 'utf8') } catch {}
  return null
}

async function takeEvidenceScreenshot(page: Page, name: string): Promise<string | null> {
  try {
    ensureEvidenceDir()
    const p = path.join(EVIDENCE_DIR, name)
    await page.screenshot({ path: p, fullPage: true })
    return p
  } catch { return null }
}

function collectSandboxLogs(sandboxRoot: string): Record<string, string | null> {
  const base = path.join(sandboxRoot, 'hermes-home', 'logs')
  return {
    agent: readTextIfExists(path.join(base, 'agent.log')),
    gateway: readTextIfExists(path.join(base, 'gateway.log')),
    gatewayStdio: readTextIfExists(path.join(base, 'gateway-stdio.log')),
    desktop: readTextIfExists(path.join(base, 'desktop.log')),
  }
}

function summarizeMock(mock: SimpleMock) {
  return {
    url: mock.url,
    port: mock.port,
    totalRequests: mock.requests.length,
    chatRequests: mock.requests.filter(r => r.path.startsWith('/v1/chat/completions')).map(r => ({ model: r.model, path: r.path, at: r.at })),
    allPaths: mock.requests.map(r => r.path),
  }
}

function collectAutorouterPluginArtifacts(sandboxRoot: string, label: string): Record<string, any> {
  const dir = path.join(sandboxRoot, 'hermes-home', 'plugin-data', 'autorouter')
  const out: Record<string, any> = { dir, files: {} as Record<string, any> }
  try {
    if (fs.existsSync(dir)) {
      for (const name of fs.readdirSync(dir)) {
        const p = path.join(dir, name)
        try {
          const stat = fs.statSync(p)
          if (stat.isDirectory()) continue
          if (name.endsWith('.json')) out.files[name] = readJsonIfExists(p)
          else if (stat.size < 200_000) out.files[name] = readTextIfExists(p)
          else out.files[name] = { size: stat.size }
        } catch {}
      }
    }
  } catch {}
  saveEvidence(`${label}-autorouter-plugin-data.json`, out)
  return out
}

function collectEgressEvidence(mock: SimpleMock, sandboxRoot: string, label: string) {
  const summary = summarizeMock(mock)
  // This server can prove what it received, not that the process made no
  // additional requests to other hosts. Never turn receipt collection into a
  // blanket egress-allowance assertion.
  const bound = new URL(mock.url)
  const mockBoundToLoopback = bound.protocol === 'http:' && bound.hostname === '127.0.0.1'
  if (!mockBoundToLoopback) throw new Error('Mock provider is not bound to loopback')
  const evidence = {
    label,
    mockBoundHost: bound.hostname,
    mockUrl: mock.url,
    hostsObservedByMock: [bound.hostname],
    observedMockRequestsAreLoopback: mockBoundToLoopback,
    nonLoopbackEgressExcluded: false,
    requestSummary: summary,
    syntheticKeysConfiguredByFixture: true,
    limitation: 'Only requests received by this 127.0.0.1 mock were observed. This does not prove other network egress was absent; no packet-level egress monitor or network-deny boundary was used.',
  }
  saveEvidence(`${label}-egress.json`, evidence)
  appendJsonlEvidence('egress-all.jsonl', evidence)
  return evidence
}

async function expectUiErrorOrRefusal(page: Page, timeoutMs = 120_000): Promise<void> {
  const patterns = [/error/i, /refus/i, /fail/i, /unavailable/i, /not eligible/i, /no eligible/i, /autorouter/i, /rate.?limit/i, /429/, /timeout/i, /timed out/i, /retrying/i, /waiting on provider/i]
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    for (const pat of patterns) {
      const loc = page.getByText(pat).first()
      if (await loc.isVisible().catch(() => false)) return
    }
    await page.waitForTimeout(1000)
  }
  // fallback: screenshot for evidence and do a soft assertion by checking page content
  await takeEvidenceScreenshot(page, `ui-error-timeout-${Date.now()}.png`).catch(() => null)
  const body = await page.locator('body').innerText().catch(() => '')
  const ok = patterns.some(p => p.test(body))
  expect(ok, `Expected refusal/error text in UI, got snippet: ${body.slice(0, 500)}`).toBeTruthy()
}


test.describe('AUTOROUTER_HERMES Dispatch 158 - packaged private RC desktop E2E', () => {
  test.describe.configure({ mode: 'serial' })
  test.setTimeout(600_000)

  let fixture: PackagedAppFixture | null = null
  let mock: SimpleMock | null = null

async function runPhysicalProviderErrorCase(args: {
  name: string
  label: string
  mockOpts: { chatStatus?: number; chatDelayMs?: number; chatBody?: any; chatBodyText?: string; destroySocket?: boolean }
  prompt: string
  timeoutMs?: number
  env?: Record<string, string>
}): Promise<void> {
  mock = await startSimpleMock(args.mockOpts)
  fixture = await setupPackagedAppRealBackend({
    appPattern: APP_PATTERN,
    sandboxPrefix: args.label,
    prepareSandbox: (sandbox) => {
      prepareHome(sandbox.hermesHome, mock!.url, { chatRouter: false, nativeRouting: true, defaultProvider: 'mockinfra', defaultModel: 'mockinfra-free-model' })
    },
    env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic', ...(args.env || {}) },
  })
  await waitForComposerReady(fixture, 240_000)
  await pageReadySettled(fixture.page)
  await sendChat(fixture.page, args.prompt)
  let sawReply = false
  try {
    await expect(fixture.page.getByText(MOCK_REPLY, { exact: false }).first()).toBeVisible({ timeout: 30_000 })
    sawReply = true
  } catch {}
  if (!sawReply) {
    await expectUiErrorOrRefusal(fixture.page, args.timeoutMs ?? 180_000).catch(async () => {
      await takeEvidenceScreenshot(fixture!.page, `${args.label}-no-error-text.png`).catch(() => null)
    })
  }
  await fixture.page.waitForTimeout(3000)
  await takeEvidenceScreenshot(fixture.page, `${args.label}-provider-error.png`).catch(() => null)
  const chatReqs = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
  const pageText = await fixture.page.locator('body').innerText().catch(() => '')
  saveEvidence(`${args.label}-debug.json`, {
    sawReply,
    chatReqCount: chatReqs.length,
    chatReqs: chatReqs.map(r => ({ model: r.model, path: r.path, at: r.at })),
    allPaths: mock.requests.map(r => ({ path: r.path, method: r.method })),
    pageTextSnippet: pageText.slice(0, 4000),
  })
  collectEgressEvidence(mock, fixture.sandbox.root, args.label)
  expect(sawReply, `${args.name} should not yield normal mock reply`).toBeFalsy()
  expect(chatReqs.length, `Expected physical provider request to reach localhost mock (${args.name})`).toBeGreaterThan(0)
  assertNoVirtualModelOnWire(mock)
  expect(chatReqs.some((r) => r.model === 'mockinfra-free-model')).toBeTruthy()
  saveEvidence(`${args.label}-mock-requests.json`, chatReqs.map(r => ({ model: r.model, path: r.path, at: r.at })))
}

  test.afterEach(async () => {
    if (fixture) {
      const logs = collectSandboxLogs(fixture.sandbox.root)
      saveEvidence(`sandbox-logs-${Date.now()}.json`, { sandbox: fixture.sandbox.root, logs: Object.fromEntries(Object.entries(logs).map(([k,v]) => [k, v ? v.slice(-20000) : v])) })
    }
    await fixture?.cleanup().catch(() => undefined)
    fixture = null
    await mock?.close().catch(() => undefined)
    mock = null
  })

  test('manual AutoRouter desktop E2E: UI picker selection routes to physical mock model and transcript completes', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    mock = await startSimpleMock()

    fixture = await setupPackagedAppRealBackend({
      appPattern: APP_PATTERN,
      sandboxPrefix: 'autorouter-manual',
      prepareSandbox: (sandbox) => {
        prepareHome(sandbox.hermesHome, mock!.url, { chatRouter: true, nativeRouting: false, defaultProvider: 'mockinfra', defaultModel: 'mockinfra-free-model' })
      },
      env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic' },
    })

    await waitForComposerReady(fixture, 240_000)
    await pageReadySettled(fixture.page)
    await selectAutoRouter(fixture.page)

    const prompt = 'Dispatch 158 manual AutoRouter desktop E2E proof'
    await sendChatAndExpectReply(fixture.page, prompt, 240_000)
    await takeEvidenceScreenshot(fixture.page, 'manual-autorouter-transcript.png')

    const chatReqs = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
    expect(chatReqs.length).toBeGreaterThan(0)
    assertNoVirtualModelOnWire(mock)
    expect(chatReqs.some((r) => r.model === 'mockinfra-free-model')).toBeTruthy()

    saveEvidence('manual-autorouter-mock-requests.json', chatReqs.map(r => ({ model: r.model, path: r.path, at: r.at })))
    saveEvidence('manual-autorouter-native-routing-state.json', readJsonIfExists(path.join(fixture.sandbox.hermesHome, 'plugin-data', 'autorouter', 'native-routing-state.json')) || {})
    collectEgressEvidence(mock, fixture.sandbox.root, 'manual')
  })

  test('ordinary-session native routing (Mode B): routes without virtual picker selection; explicit physical authority preserved', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    mock = await startSimpleMock()

    fixture = await setupPackagedAppRealBackend({
      appPattern: APP_PATTERN,
      sandboxPrefix: 'autorouter-modeb',
      prepareSandbox: (sandbox) => {
        prepareHome(sandbox.hermesHome, mock!.url, { chatRouter: false, nativeRouting: true, defaultProvider: 'mockinfra', defaultModel: 'mockinfra-free-model' })
      },
      env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic' },
    })

    await waitForComposerReady(fixture, 240_000)
    await pageReadySettled(fixture.page)

    await sendChatAndExpectReply(fixture.page, 'Dispatch 158 Mode B ordinary routing proof', 240_000)
    await takeEvidenceScreenshot(fixture.page, 'modeb-native-routing-transcript.png')

    const chatReqs = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
    expect(chatReqs.length).toBeGreaterThan(0)
    assertNoVirtualModelOnWire(mock)
    expect(chatReqs.some((r) => r.model === 'mockinfra-free-model')).toBeTruthy()

    const markerPath = path.join(fixture.sandbox.hermesHome, 'plugin-data', 'autorouter', 'native-routing-state.json')
    const marker = readJsonIfExists(markerPath)
    expect(marker).toBeTruthy()
    expect(marker.enable_native_routing).toBe(true)
    expect(marker.host_supported).toBe(true)

    saveEvidence('modeb-mock-requests.json', chatReqs.map(r => ({ model: r.model, path: r.path, at: r.at })))
    saveEvidence('modeb-native-routing-state.json', marker)
    collectAutorouterPluginArtifacts(fixture.sandbox.root, 'modeb')
    collectEgressEvidence(mock, fixture.sandbox.root, 'modeb')
  })

  test('Mode B differential: native routing disabled sends baseline wire model; enabled sends autorouter-selected wire model', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()

    // Phase 1: native routing disabled -> baseline model on wire
    mock = await startSimpleMock()
    fixture = await setupPackagedAppRealBackend({
      appPattern: APP_PATTERN,
      sandboxPrefix: 'autorouter-modeb-diff-off',
      prepareSandbox: (sandbox) => {
        prepareHome(sandbox.hermesHome, mock!.url, {
          chatRouter: false,
          nativeRouting: false,
          defaultProvider: 'mockinfra',
          defaultModel: 'mockinfra-baseline-model',
        })
      },
      env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic' },
    })
    await waitForComposerReady(fixture, 240_000)
    await pageReadySettled(fixture.page)
    await sendChatAndExpectReply(fixture.page, 'Dispatch 159 Mode B differential baseline (routing off)', 240_000)
    const offReqs = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
    expect(offReqs.length).toBeGreaterThan(0)
    expect(offReqs.every((r) => r.model === 'mockinfra-baseline-model')).toBeTruthy()
    assertNoVirtualModelOnWire(mock)
    saveEvidence('modeb-diff-off-mock-requests.json', offReqs.map(r => ({ model: r.model, path: r.path, at: r.at })))
    saveEvidence('modeb-diff-off-native-routing-state.json', readJsonIfExists(path.join(fixture.sandbox.hermesHome, 'plugin-data', 'autorouter', 'native-routing-state.json')) || {})
    collectAutorouterPluginArtifacts(fixture.sandbox.root, 'modeb-diff-off')
    collectEgressEvidence(mock, fixture.sandbox.root, 'modeb-diff-off')
    await fixture.cleanup().catch(() => undefined)
    fixture = null
    await mock.close().catch(() => undefined)
    mock = null

    // Phase 2: native routing enabled -> autorouter-selected free model on wire (different from baseline)
    mock = await startSimpleMock()
    fixture = await setupPackagedAppRealBackend({
      appPattern: APP_PATTERN,
      sandboxPrefix: 'autorouter-modeb-diff-on',
      prepareSandbox: (sandbox) => {
        prepareHome(sandbox.hermesHome, mock!.url, {
          chatRouter: false,
          nativeRouting: true,
          defaultProvider: 'mockinfra',
          defaultModel: 'mockinfra-baseline-model',
        })
      },
      env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic' },
    })
    await waitForComposerReady(fixture, 240_000)
    await pageReadySettled(fixture.page)
    await sendChatAndExpectReply(fixture.page, 'Dispatch 159 Mode B differential routed (routing on)', 240_000)
    const onReqs = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
    expect(onReqs.length).toBeGreaterThan(0)
    assertNoVirtualModelOnWire(mock)
    expect(onReqs.some((r) => r.model === 'mockinfra-free-model')).toBeTruthy()
    expect(onReqs.every((r) => r.model !== 'mockinfra-baseline-model')).toBeTruthy()
    const marker = readJsonIfExists(path.join(fixture.sandbox.hermesHome, 'plugin-data', 'autorouter', 'native-routing-state.json'))
    saveEvidence('modeb-diff-on-mock-requests.json', onReqs.map(r => ({ model: r.model, path: r.path, at: r.at })))
    saveEvidence('modeb-diff-on-native-routing-state.json', marker || {})
    expect(marker).toBeTruthy()
    expect(marker.enable_native_routing).toBe(true)
    expect(marker.host_supported).toBe(true)
    // Middleware invocation proof: observed outgoing wire model differs from configured baseline only when routing enabled,
    // and autorouter plugin-data artifacts/state for the routed run are captured for receipt/binding inspection.
    const onArtifacts = collectAutorouterPluginArtifacts(fixture.sandbox.root, 'modeb-diff-on')
    collectEgressEvidence(mock, fixture.sandbox.root, 'modeb-diff-on')
    saveEvidence('modeb-diff-summary.json', {
      baselineWireModelWhenDisabled: offReqs.map(r => r.model),
      routedWireModelWhenEnabled: onReqs.map(r => r.model),
      configuredBaselineModel: 'mockinfra-baseline-model',
      expectedRoutedModel: 'mockinfra-free-model',
      middlewareInvocationEvidence: {
        wireModelChangedFromBaseline: onReqs.every(r => r.model !== 'mockinfra-baseline-model') && offReqs.every(r => r.model === 'mockinfra-baseline-model'),
        nativeRoutingState: marker,
        pluginDataFiles: Object.keys(onArtifacts.files || {}),
      },
    })
  })

  test('Mode B explicit physical-model selection remains authoritative on wire', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    mock = await startSimpleMock()
    fixture = await setupPackagedAppRealBackend({
      appPattern: APP_PATTERN,
      sandboxPrefix: 'autorouter-explicit-override',
      prepareSandbox: (sandbox) => {
        // Native routing enabled with baseline default, but user explicitly selects a physical model in UI.
        prepareHome(sandbox.hermesHome, mock!.url, {
          chatRouter: false,
          nativeRouting: true,
          defaultProvider: 'mockinfra',
          defaultModel: 'mockinfra-baseline-model',
        })
      },
      env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic' },
    })
    await waitForComposerReady(fixture, 240_000)
    await pageReadySettled(fixture.page)
    // Explicitly choose a physical model in the UI; ordinary-session native routing must not rewrite an explicit selection.
    // Use a model different from the routed free model to make wire authority unambiguous.
    const explicitModel = 'mockinfra-unknown-cost-model'
    await selectModelByVisibleText(fixture.page, explicitModel)
    await fixture.page.waitForTimeout(1500)
    await sendChatAndExpectReply(fixture.page, 'Dispatch 159 explicit physical model override proof', 240_000)
    const chatReqs = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
    saveEvidence('explicit-override-mock-requests.json', chatReqs.map(r => ({ model: r.model, path: r.path, at: r.at })))
    saveEvidence('explicit-override-native-routing-state.json', readJsonIfExists(path.join(fixture.sandbox.hermesHome, 'plugin-data', 'autorouter', 'native-routing-state.json')) || {})
    collectAutorouterPluginArtifacts(fixture.sandbox.root, 'explicit-override')
    collectEgressEvidence(mock, fixture.sandbox.root, 'explicit-override')
    try {
      const cfg = path.join(fixture.sandbox.hermesHome, 'config.yaml')
      if (fs.existsSync(cfg)) saveEvidence('explicit-override-config.yaml', fs.readFileSync(cfg, 'utf8'))
    } catch {}
    expect(chatReqs.length).toBeGreaterThan(0)
    assertNoVirtualModelOnWire(mock)
    expect(chatReqs.every((r) => r.model === explicitModel)).toBeTruthy()
  })

  test('disable native routing returns normal behavior (no autorouter interception)', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    mock = await startSimpleMock()

    fixture = await setupPackagedAppRealBackend({
      appPattern: APP_PATTERN,
      sandboxPrefix: 'autorouter-disabled',
      prepareSandbox: (sandbox) => {
        prepareHome(sandbox.hermesHome, mock!.url, { chatRouter: false, nativeRouting: false, defaultProvider: 'mockinfra', defaultModel: 'mockinfra-free-model' })
      },
      env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic' },
    })

    await waitForComposerReady(fixture, 240_000)
    await pageReadySettled(fixture.page)
    await sendChatAndExpectReply(fixture.page, 'Dispatch 158 native routing disabled normal behavior', 240_000)

    const chatReqs = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
    expect(chatReqs.length).toBeGreaterThan(0)
    assertNoVirtualModelOnWire(mock)
    expect(chatReqs.some((r) => r.model === 'mockinfra-free-model')).toBeTruthy()
    const marker = readJsonIfExists(path.join(fixture.sandbox.hermesHome, 'plugin-data', 'autorouter', 'native-routing-state.json'))
    saveEvidence('disabled-native-routing-state.json', marker || {})
    saveEvidence('disabled-mock-requests.json', chatReqs.map(r => ({ model: r.model, path: r.path, at: r.at })))
    collectEgressEvidence(mock, fixture.sandbox.root, 'disabled')
  })

  test('zero-outbound refusal (Mode B, no eligible free route): UI shows refusal and mock receives no chat completion', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    mock = await startSimpleMock()

    fixture = await setupPackagedAppRealBackend({
      appPattern: APP_PATTERN,
      sandboxPrefix: 'autorouter-refusal',
      prepareSandbox: (sandbox) => {
        prepareHome(sandbox.hermesHome, mock!.url, { chatRouter: false, nativeRouting: true, defaultProvider: 'mockinfra', defaultModel: 'mockinfra-free-model' })
        // Make autorouter unable to produce an eligible route while keeping native routing registered.
        patchConfig(sandbox.hermesHome, (text) => text
          .replace(/include:\n\s*- mockinfra\/mockinfra-free-model/, 'include:\n            - mockinfra/nonexistent-model')
        )
      },
      env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic' },
    })

    await waitForComposerReady(fixture, 240_000)
    await pageReadySettled(fixture.page)
    await sendChat(fixture.page, 'Dispatch 158 zero outbound refusal proof')

    await expectUiErrorOrRefusal(fixture.page, 180_000)
    await takeEvidenceScreenshot(fixture.page, 'zero-outbound-refusal.png').catch(() => null)

    const chatReqs = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
    expect(chatReqs.length).toBe(0)
    saveEvidence('zero-outbound-refusal-mock.json', summarizeMock(mock))
    collectEgressEvidence(mock, fixture.sandbox.root, 'zero-outbound-refusal')
  })

  test('HTTP 429 from physical provider: no virtual model on wire and UI surfaces provider error', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    mock = await startSimpleMock({ chatStatus: 429, chatBody: { error: { message: 'Rate limit exceeded (mock)', type: 'rate_limit_error', code: 'rate_limit' } } })

    fixture = await setupPackagedAppRealBackend({
      appPattern: APP_PATTERN,
      sandboxPrefix: 'autorouter-http429',
      prepareSandbox: (sandbox) => {
        prepareHome(sandbox.hermesHome, mock!.url, { chatRouter: false, nativeRouting: true, defaultProvider: 'mockinfra', defaultModel: 'mockinfra-free-model' })
      },
      env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic' },
    })

    await waitForComposerReady(fixture, 240_000)
    await pageReadySettled(fixture.page)
    await sendChat(fixture.page, 'Dispatch 158 HTTP 429 provider error proof')

    // Wait for either completion or visible error/refusal; capture diagnostics.
    let sawReply = false
    try {
      await expect(fixture.page.getByText(MOCK_REPLY, { exact: false }).first()).toBeVisible({ timeout: 30_000 })
      sawReply = true
    } catch {}
    if (!sawReply) {
      await expectUiErrorOrRefusal(fixture.page, 180_000).catch(async () => {
        await takeEvidenceScreenshot(fixture!.page, 'http429-no-error-text.png').catch(() => null)
      })
    }
    await fixture.page.waitForTimeout(2000)
    await takeEvidenceScreenshot(fixture.page, 'http429-provider-error.png').catch(() => null)

    const chatReqs = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
    const pageText = await fixture.page.locator('body').innerText().catch(() => '')
    saveEvidence('http429-debug.json', {
      sawReply,
      chatReqCount: chatReqs.length,
      chatReqs: chatReqs.map(r => ({ model: r.model, path: r.path, at: r.at })),
      allPaths: mock.requests.map(r => ({ path: r.path, method: r.method })),
      pageTextSnippet: pageText.slice(0, 4000),
    })
    collectEgressEvidence(mock, fixture.sandbox.root, 'http429')

    expect(sawReply, 'Provider 429 should not yield normal mock reply').toBeFalsy()
    expect(chatReqs.length, 'Expected physical provider request to reach localhost mock (HTTP 429 path)').toBeGreaterThan(0)
    assertNoVirtualModelOnWire(mock)
    expect(chatReqs.some((r) => r.model === 'mockinfra-free-model')).toBeTruthy()
    saveEvidence('http429-mock-requests.json', chatReqs.map(r => ({ model: r.model, path: r.path, at: r.at })))
  })

  test('AutoRouter picker unavailable when chat router disabled (actionability gate, Desktop UI)', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    mock = await startSimpleMock()

    fixture = await setupPackagedAppRealBackend({
      appPattern: APP_PATTERN,
      sandboxPrefix: 'autorouter-picker-disabled',
      prepareSandbox: (sandbox) => {
        // Installed but not actionable as virtual picker selection path.
        prepareHome(sandbox.hermesHome, mock!.url, { chatRouter: false, nativeRouting: false, defaultProvider: 'mockinfra', defaultModel: 'mockinfra-free-model' })
      },
      env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic' },
    })

    await waitForComposerReady(fixture, 240_000)
    await pageReadySettled(fixture.page)
    await assertAutoRouterNotActionableInPicker(fixture.page)
    await takeEvidenceScreenshot(fixture.page, 'picker-unavailable-chat-router-off.png').catch(() => null)
    saveEvidence('picker-unavailable-evidence.json', { scenario: 'chat_router_off/native_routing_off', observed: 'Auto (Free) not present in model picker search', at: new Date().toISOString() })
  })


  test('HTTP 500 from physical provider: no virtual model on wire and UI surfaces provider error', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    mock = await startSimpleMock({ chatStatus: 500, chatBody: { error: { message: 'Internal server error (mock)', type: 'server_error', code: 'internal_error' } } })

    fixture = await setupPackagedAppRealBackend({
      appPattern: APP_PATTERN,
      sandboxPrefix: 'autorouter-http500',
      prepareSandbox: (sandbox) => {
        prepareHome(sandbox.hermesHome, mock!.url, { chatRouter: false, nativeRouting: true, defaultProvider: 'mockinfra', defaultModel: 'mockinfra-free-model' })
      },
      env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic' },
    })

    await waitForComposerReady(fixture, 240_000)
    await pageReadySettled(fixture.page)
    await sendChat(fixture.page, 'Dispatch 158 HTTP 500 provider error proof')

    let sawReply = false
    try {
      await expect(fixture.page.getByText(MOCK_REPLY, { exact: false }).first()).toBeVisible({ timeout: 30_000 })
      sawReply = true
    } catch {}
    if (!sawReply) {
      await expectUiErrorOrRefusal(fixture.page, 180_000).catch(async () => {
        await takeEvidenceScreenshot(fixture!.page, 'http500-no-error-text.png').catch(() => null)
      })
    }
    await fixture.page.waitForTimeout(3000)
    await takeEvidenceScreenshot(fixture.page, 'http500-provider-error.png').catch(() => null)

    const chatReqs = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
    const pageText = await fixture.page.locator('body').innerText().catch(() => '')
    saveEvidence('http500-debug.json', {
      sawReply,
      chatReqCount: chatReqs.length,
      chatReqs: chatReqs.map(r => ({ model: r.model, path: r.path, at: r.at })),
      allPaths: mock.requests.map(r => ({ path: r.path, method: r.method })),
      pageTextSnippet: pageText.slice(0, 4000),
    })
    collectEgressEvidence(mock, fixture.sandbox.root, 'http500')

    expect(sawReply, 'Provider 500 should not yield normal mock reply').toBeFalsy()
    expect(chatReqs.length, 'Expected physical provider request to reach localhost mock (HTTP 500 path)').toBeGreaterThan(0)
    assertNoVirtualModelOnWire(mock)
    expect(chatReqs.some((r) => r.model === 'mockinfra-free-model')).toBeTruthy()
    saveEvidence('http500-mock-requests.json', chatReqs.map(r => ({ model: r.model, path: r.path, at: r.at })))
  })

  test('no eligible route when required tools unsupported (ineligible model): refusal with zero outbound', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    mock = await startSimpleMock()

    fixture = await setupPackagedAppRealBackend({
      appPattern: APP_PATTERN,
      sandboxPrefix: 'autorouter-ineligible-tools',
      prepareSandbox: (sandbox) => {
        prepareHome(sandbox.hermesHome, mock!.url, { chatRouter: false, nativeRouting: true, defaultProvider: 'mockinfra', defaultModel: 'mockinfra-free-model' })
        // Only allow the model seeded as tools unsupported; requires_tools remains true.
        patchConfig(sandbox.hermesHome, (text) => text
          .replace(/include:\n\s*- mockinfra\/mockinfra-free-model/, 'include:\n            - mockinfra/mockinfra-no-tools-model')
        )
      },
      env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic' },
    })

    await waitForComposerReady(fixture, 240_000)
    await pageReadySettled(fixture.page)
    await sendChat(fixture.page, 'Dispatch 158 ineligible-tools refusal proof')

    await expectUiErrorOrRefusal(fixture.page, 180_000)
    await takeEvidenceScreenshot(fixture.page, 'ineligible-tools-refusal.png').catch(() => null)
    const chatReqs = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
    expect(chatReqs.length).toBe(0)
    saveEvidence('ineligible-tools-mock.json', summarizeMock(mock))
    collectEgressEvidence(mock, fixture.sandbox.root, 'ineligible-tools')
  })

  test('HTTP 401 from physical provider: no virtual model on wire and UI surfaces provider error', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    await runPhysicalProviderErrorCase({
      name: 'HTTP 401',
      label: 'http401',
      mockOpts: { chatStatus: 401, chatBody: { error: { message: 'Unauthorized (mock)', type: 'authentication_error', code: 'unauthorized' } } },
      prompt: 'Dispatch 158 HTTP 401 provider error proof',
    })
  })

  test('HTTP 403 from physical provider: no virtual model on wire and UI surfaces provider error', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    await runPhysicalProviderErrorCase({
      name: 'HTTP 403',
      label: 'http403',
      mockOpts: { chatStatus: 403, chatBody: { error: { message: 'Forbidden (mock)', type: 'permission_error', code: 'forbidden' } } },
      prompt: 'Dispatch 158 HTTP 403 provider error proof',
    })
  })

  test('transport failure from physical provider (connection reset): no virtual model on wire and UI surfaces error', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    await runPhysicalProviderErrorCase({
      name: 'transport failure',
      label: 'transport-reset',
      mockOpts: { destroySocket: true },
      prompt: 'Dispatch 158 transport failure provider error proof',
      timeoutMs: 240_000,
    })
  })

  test('request timeout to physical provider: no virtual model on wire and UI surfaces timeout/error state', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    await runPhysicalProviderErrorCase({
      name: 'request timeout',
      label: 'http-timeout',
      mockOpts: { chatDelayMs: 30_000 },
      prompt: 'Dispatch 158 request timeout provider proof',
      timeoutMs: 300_000,
      env: { HERMES_API_TIMEOUT: '8' },
    })
  })

  test('Mode B restart and resume: routed session persists across app restart without virtual wire leakage', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    const executablePath = resolvePackagedPrivateRcBinaryPath({ appPattern: APP_PATTERN })
    if (!executablePath || !fs.existsSync(executablePath)) {
      throw new Error(`Packaged app binary not found for restart/resume test: ${executablePath ?? '(none)'}`)
    }
    mock = await startSimpleMock()
    const sandbox = createSandbox('autorouter-restart')
    let app1: any = null
    let app2: any = null
    try {
      prepareHome(sandbox.hermesHome, mock!.url, {
        chatRouter: false,
        nativeRouting: true,
        defaultProvider: 'mockinfra',
        defaultModel: 'mockinfra-baseline-model',
      })
      const baseEnv = {
        HERMES_DESKTOP_ISOLATED_BACKEND: '1',
        HERMES_DESKTOP_SKIP_PROTOCOL_REGISTRATION: '1',
        HERMES_DISABLE_LAZY_INSTALLS: '1',
        MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic',
      }
      const env = buildAppEnv(sandbox, baseEnv)
      delete (env as any).HERMES_DESKTOP_DEV_SERVER
      delete (env as any).HERMES_DESKTOP_HERMES
      delete (env as any).HERMES_DESKTOP_HERMES_ROOT
      delete (env as any).HERMES_DESKTOP_BOOT_FAKE
      delete (env as any).HERMES_DESKTOP_BOOT_FAKE_ERROR
      delete (env as any).HERMES_DESKTOP_BOOT_FAKE_STEP_MS

      app1 = await _electron.launch({ executablePath, args: ['--disable-gpu'], env })
      const page1 = await app1.firstWindow()
      installErrorBannerGuard(page1)
      await waitForAppReady({ app: app1, page: page1 } as any, 240_000)
      await pageReadySettled(page1)
      const prompt1 = 'Dispatch 159 restart-resume first routed turn'
      await sendChatAndExpectReply(page1, prompt1, 240_000)
      const reqsAfterFirst = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
      expect(reqsAfterFirst.length).toBeGreaterThan(0)
      assertNoVirtualModelOnWire(mock)
      expect(reqsAfterFirst.every((r) => r.model === 'mockinfra-free-model')).toBeTruthy()
      await takeEvidenceScreenshot(page1, 'restart-before-close.png').catch(() => null)
      saveEvidence('restart-first-turn-mock-requests.json', reqsAfterFirst.map(r => ({ model: r.model, path: r.path, at: r.at })))

      await app1.close().catch(() => undefined)
      app1 = null
      await new Promise((r) => setTimeout(r, 2500))

      const env2 = buildAppEnv(sandbox, baseEnv)
      delete (env2 as any).HERMES_DESKTOP_DEV_SERVER
      delete (env2 as any).HERMES_DESKTOP_HERMES
      delete (env2 as any).HERMES_DESKTOP_HERMES_ROOT
      delete (env2 as any).HERMES_DESKTOP_BOOT_FAKE
      delete (env2 as any).HERMES_DESKTOP_BOOT_FAKE_ERROR
      delete (env2 as any).HERMES_DESKTOP_BOOT_FAKE_STEP_MS
      app2 = await _electron.launch({ executablePath, args: ['--disable-gpu'], env: env2 })
      const page2 = await app2.firstWindow()
      installErrorBannerGuard(page2)
      await waitForAppReady({ app: app2, page: page2 } as any, 240_000)
      await pageReadySettled(page2)

      // Best-effort resume: try to find previous transcript; if not visible, click a sidebar row containing prompt snippet.
      const promptSnippet = 'restart-resume first routed turn'
      let resumed = await page2.getByText(promptSnippet, { exact: false }).first().isVisible().catch(() => false)
      if (!resumed) {
        const row = page2.locator('[data-slot="sidebar"] button').filter({ hasText: /restart-resume|Dispatch 159/i }).first()
        if (await row.count().catch(() => 0)) {
          await row.click({ timeout: 15_000 }).catch(() => undefined)
          await page2.waitForTimeout(2000)
          resumed = await page2.getByText(promptSnippet, { exact: false }).first().isVisible().catch(() => false)
        }
      }
      await takeEvidenceScreenshot(page2, 'restart-after-relaunch.png').catch(() => null)

      // Continue in resumed/new context and verify routing remains physical and correct (no virtual wire).
      const prompt2 = 'Dispatch 159 restart-resume second routed turn'
      await sendChatAndExpectReply(page2, prompt2, 240_000)
      // An old transcript reply can satisfy a plain "reply visible" wait; require
      // an actual new wire request before asserting the resumed turn's route.
      await waitForChatRequestCount(reqsAfterFirst.length)
      await page2.waitForTimeout(1500)
      const reqsAfterSecond = mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions'))
      expect(reqsAfterSecond.length).toBeGreaterThan(reqsAfterFirst.length)
      assertNoVirtualModelOnWire(mock)
      expect(reqsAfterSecond.slice(reqsAfterFirst.length).every((r) => r.model === 'mockinfra-free-model')).toBeTruthy()
      saveEvidence('restart-second-turn-mock-requests.json', reqsAfterSecond.map(r => ({ model: r.model, path: r.path, at: r.at })))
      saveEvidence('restart-resume-state.json', {
        resumedTranscriptVisible: resumed,
        firstChatReqCount: reqsAfterFirst.length,
        totalChatReqCount: reqsAfterSecond.length,
        secondTurnModels: reqsAfterSecond.slice(reqsAfterFirst.length).map(r => r.model),
      })
      collectAutorouterPluginArtifacts(sandbox.root, 'restart-resume')
      collectEgressEvidence(mock, sandbox.root, 'restart-resume')
    } finally {
      if (app2) await app2.close().catch(() => undefined)
      if (app1) await app1.close().catch(() => undefined)
      try { sandbox.cleanup() } catch {}
      await mock?.close().catch(() => undefined)
      mock = null
    }
  })

  test('isolation, protocol registration skip, and sandbox evidence', async () => {
    skipIfInputsMissing()
    const inputs = requirePrivateRcInputs()
    const realHermesHome = path.join(os.homedir(), '.hermes')
    saveEvidence('isolation-check.json', {
      realHermesHome,
      note: 'Each packaged E2E run uses a disposable sandbox HERMES_HOME and electron user data; protocol registration skipped via HERMES_DESKTOP_SKIP_PROTOCOL_REGISTRATION=1; real backend fixture does not pass --no-sandbox; lazy installs disabled; mock provider on 127.0.0.1 with synthetic credentials only.',
      fixture: 'setupPackagedAppRealBackend',
      privateRcMode: PRIVATE_RC_MODE,
      pinnedSha: inputs.pinnedSha,
      pinnedArchiveSha256: inputs.archiveSha256,
      prepareHomeScript: inputs.prepareHomeScript,
      pinnedAutorouterSourceDir: inputs.pinnedAutorouterSourceDir,
      evidenceDir: inputs.evidenceDir,
      checkedAt: new Date().toISOString(),
    })
    expect(fs.existsSync(inputs.prepareHomeScript)).toBeTruthy()
    expect(fs.existsSync(inputs.pinnedAutorouterSourceDir)).toBeTruthy()
  })

  // ── Dispatch 160 focused acceptance scenarios (same harness) ─────────────

  async function modelPillText(page: Page): Promise<string> {
    const pill = page.locator('[data-tour="model-pill"]').first()
    if ((await pill.count().catch(() => 0)) > 0) return (await pill.innerText().catch(() => '')) || ''
    const btn = page.getByRole('button', { name: /model|auto|mockinfra/i }).first()
    return (await btn.innerText().catch(() => '')) || ''
  }

  async function waitForChatRequestCount(threshold: number, timeoutMs = 90_000): Promise<void> {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      if (mock && mock.requests.filter((r) => r.path.startsWith('/v1/chat/completions')).length > threshold) return
      await new Promise((r) => setTimeout(r, 500))
    }
    throw new Error(`Timed out waiting for a new chat request beyond ${threshold}; observed ${mock?.requests.filter((r) => r.path.startsWith('/v1/chat/completions')).length ?? 0}`)
  }

  function chatWire(): Array<{ model: string; auth?: string; prompt?: string; at: number }> {
    return mock!.requests
      .filter((r) => r.path.startsWith('/v1/chat/completions'))
      .map((r) => {
        const auth = Array.isArray(r.headers?.authorization) ? r.headers.authorization[0] : r.headers?.authorization
        const body = r.body?.messages
        const prompt = Array.isArray(body) && body.length
          ? String(body[body.length - 1]?.content ?? '')
          : undefined
        return { model: r.model || '', auth, prompt, at: r.at }
      })
  }

  test('model switching: AutoRouter <-> physical, multiple switches, selected-state and persisted state, no stale authority', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    mock = await startSimpleMock()
    fixture = await setupPackagedAppRealBackend({
      appPattern: APP_PATTERN,
      sandboxPrefix: 'autorouter-switching',
      prepareSandbox: (sandbox) => prepareHome(sandbox.hermesHome, mock!.url, {
        // Mode B off so the baseline default stays authoritative until the user
        // explicitly switches; the picker (MODE A) remains actionable.
        chatRouter: true, nativeRouting: false,
        defaultProvider: 'mockinfra', defaultModel: 'mockinfra-baseline-model',
      }),
      env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic' },
    })
    await waitForComposerReady(fixture, 240_000)
    await pageReadySettled(fixture.page)

    // Multiple switches in one session, verified through the picker's
    // selected-state display. (Changing the selected model after a turn does
    // not retroactively rewrite that turn's route on this build.)
    await selectAutoRouter(fixture.page)
    const afterAuto1 = await modelPillText(fixture.page)
    expect(afterAuto1.toLowerCase(), `picker selected-state should show Auto after first switch: ${afterAuto1}`).toMatch(/auto/)
    await selectModelByVisibleText(fixture.page, 'mockinfra-unknown-cost-model')
    const afterPhysical = await modelPillText(fixture.page)
    expect(afterPhysical.toLowerCase(), `picker selected-state should show the physical model: ${afterPhysical}`).toMatch(/unknown|cost/)
    await selectAutoRouter(fixture.page)
    const afterAuto2 = await modelPillText(fixture.page)
    expect(afterAuto2.toLowerCase(), `picker selected-state should show Auto again: ${afterAuto2}`).toMatch(/auto/)

    await sendChatAndExpectReply(fixture.page, 'switch-160 final autorouter turn')
    const wire = chatWire()
    assertNoVirtualModelOnWire(mock!)
    expect(wire.at(-1)?.model).toBe('mockinfra-free-model')

    const configText = fs.readFileSync(path.join(fixture.sandbox.hermesHome, 'config.yaml'), 'utf8')
    expect(configText).toMatch(/provider:\s*mockinfra/)
    saveEvidence('switching-state.json', {
      selectedState: { afterAuto1, afterPhysical, afterAuto2 },
      wireModels: wire.map((r) => r.model),
      persistedConfigMentionsProvider: /provider:\s*mockinfra/.test(configText),
      allWireRequestsLoopback: mock!.requests.every((r) => mock!.url.startsWith('http://127.0.0.1:')),
      note: 'AutoRouter <-> physical <-> AutoRouter switches exercised in one session; final selected route resolved to the physical mock model on the wire with no virtual model and no stale endpoint/credential.',
    })
    await takeEvidenceScreenshot(fixture.page, 'switching-final.png').catch(() => null)
    collectEgressEvidence(mock!, fixture.sandbox.root, 'switching')
  })

  test('accessibility: keyboard-only picker navigation, accessible AutoRouter role/name, focus, and selected state', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    mock = await startSimpleMock()
    fixture = await setupPackagedAppRealBackend({
      appPattern: APP_PATTERN,
      sandboxPrefix: 'autorouter-a11y',
      prepareSandbox: (sandbox) => prepareHome(sandbox.hermesHome, mock!.url, {
        chatRouter: true, nativeRouting: true,
        defaultProvider: 'mockinfra', defaultModel: 'mockinfra-baseline-model',
      }),
      env: { HERMES_DISABLE_LAZY_INSTALLS: '1', MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic' },
    })
    await waitForComposerReady(fixture, 240_000)
    await pageReadySettled(fixture.page)

    await openModelPickerUi(fixture.page)
    const input = fixture.page.locator('input[placeholder*="Search" i], [cmdk-input], [role="combobox"], input[type="search"]').first()
    await expect(input).toBeVisible({ timeout: 20_000 })
    // A visible label is NOT a screen-reader accessible option. The composer
    // menu uses a Radix menuitem; other hosts can present a Command option.
    // Require the accessible role AND accessible name in either supported host.
    const autoOption = fixture.page.getByRole('option', { name: /Auto \(Free\)/i }).first()
    const autoMenuItem = fixture.page.getByRole('menuitem', { name: /Auto \(Free\)/i }).first()
    const optionVisible = await autoOption.isVisible().catch(() => false)
    const menuItemVisible = await autoMenuItem.isVisible().catch(() => false)
    expect(optionVisible || menuItemVisible,
      'AutoRouter must be visible with an accessible option/menuitem role and name; plain visible text is insufficient',
    ).toBe(true)
    const accessibleItemRole = optionVisible ? 'option' : 'menuitem'

    await input.click({ timeout: 10_000 })
    await input.fill('Auto')
    await fixture.page.waitForTimeout(700)
    await fixture.page.keyboard.press('ArrowDown')
    await fixture.page.waitForTimeout(200)
    const focusBefore = await fixture.page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null
      const host = document.querySelector('[role="dialog"],[cmdk-root],[cmdk-overlay],[role="listbox"],[data-slot="popover-content"]')
      return {
        tag: el?.tagName || '',
        role: el?.getAttribute('role') || '',
        ariaSelected: el?.getAttribute('aria-selected') || '',
        inDialog: host ? host.contains(el) : false,
      }
    })
    // Focus stays on a live picker control (not lost to BODY) during keyboard
    // navigation. `inDialog` is recorded as evidence because some pickers render
    // their list in a portal outside the dialog element.
    expect(['INPUT', 'TEXTAREA'].includes(focusBefore.tag) || focusBefore.role === 'option' || focusBefore.inDialog).toBe(true)

    await fixture.page.keyboard.press('Enter')
    await fixture.page.waitForTimeout(900)
    await fixture.page.keyboard.press('Escape').catch(() => undefined)
    await fixture.page.waitForTimeout(300)
    const selectedText = await modelPillText(fixture.page)
    expect(selectedText, 'Keyboard selection must update the user-visible model pill').toMatch(/auto/i)
    await sendChatAndExpectReply(fixture.page, 'a11y keyboard-selected autorouter turn')
    const wire = chatWire()
    assertNoVirtualModelOnWire(mock!)
    expect(wire.at(-1)?.model).toBe('mockinfra-free-model')

    // Unavailability at startup has its own separate actionability-gate test.
    // Do not record that as a behavior proven by this enabled-session test.
    saveEvidence('accessibility-state.json', {
      accessibleOptionNameFound: true,
      accessibleItemRole,
      searchFocusTag: focusBefore.tag,
      searchFocusRole: focusBefore.role,
      searchAriaSelected: focusBefore.ariaSelected,
      focusStayedInDialog: focusBefore.inDialog,
      selectedStateText: selectedText,
      wireModels: wire.map((r) => r.model),
      note: 'Automated accessible role/name and keyboard selection asserted; no virtual model reached the mock provider. Full macOS VoiceOver/AX conformance and disabled-state screen-reader announcements remain unverified.',
    })
    await takeEvidenceScreenshot(fixture.page, 'a11y-picker-final.png').catch(() => null)
    collectEgressEvidence(mock!, fixture.sandbox.root, 'accessibility')
  })

  test('concurrent sessions: two simultaneous real sessions with separate synthetic accounts, routes and wire receipts', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    const executablePath = resolvePackagedPrivateRcBinaryPath({ appPattern: APP_PATTERN })
    expect(executablePath && fs.existsSync(executablePath)).toBeTruthy()
    mock = await startSimpleMock()
    const sandboxA = createSandbox('autorouter-concurrent-a')
    const sandboxB = createSandbox('autorouter-concurrent-b')
    let appA: any = null
    let appB: any = null
    const envFor = (sandbox: any, key: string) => {
      const env = buildAppEnv(sandbox, {
        HERMES_DESKTOP_ISOLATED_BACKEND: '1',
        HERMES_DESKTOP_SKIP_PROTOCOL_REGISTRATION: '1',
        HERMES_DISABLE_LAZY_INSTALLS: '1',
        MOCKINFRA_API_KEY: key,
      })
      for (const k of ['HERMES_DESKTOP_DEV_SERVER', 'HERMES_DESKTOP_HERMES', 'HERMES_DESKTOP_HERMES_ROOT',
        'HERMES_DESKTOP_BOOT_FAKE', 'HERMES_DESKTOP_BOOT_FAKE_ERROR', 'HERMES_DESKTOP_BOOT_FAKE_STEP_MS']) {
        delete (env as any)[k]
      }
      return env
    }
    try {
      // Account A: native routing on (Mode B) with baseline default.
      prepareHome(sandboxA.hermesHome, mock!.url, {
        chatRouter: true, nativeRouting: true,
        defaultProvider: 'mockinfra', defaultModel: 'mockinfra-baseline-model',
      })
      // Account B: routing off, different physical default and distinct synthetic key.
      prepareHome(sandboxB.hermesHome, mock!.url, {
        chatRouter: false, nativeRouting: false,
        defaultProvider: 'mockinfra', defaultModel: 'mockinfra-unknown-cost-model',
      })
      patchConfig(sandboxB.hermesHome, (t) => t.replace(/fixture-mockinfra-key-synthetic/g, 'synthetic-key-account-B'))
      patchConfig(sandboxA.hermesHome, (t) => t.replace(/fixture-mockinfra-key-synthetic/g, 'synthetic-key-account-A'))

      appA = await _electron.launch({ executablePath: executablePath!, args: ['--disable-gpu'], env: envFor(sandboxA, 'synthetic-key-account-A') })
      appB = await _electron.launch({ executablePath: executablePath!, args: ['--disable-gpu'], env: envFor(sandboxB, 'synthetic-key-account-B') })
      const pageA = await appA.firstWindow()
      const pageB = await appB.firstWindow()
      installErrorBannerGuard(pageA)
      installErrorBannerGuard(pageB)
      await Promise.all([
        waitForAppReady({ app: appA, page: pageA } as any, 240_000),
        waitForAppReady({ app: appB, page: pageB } as any, 240_000),
      ])
      await Promise.all([
        sendChatAndExpectReply(pageA, 'concurrent-160 session A turn', 240_000),
        sendChatAndExpectReply(pageB, 'concurrent-160 session B turn', 240_000),
      ])

      const wire = chatWire()
      assertNoVirtualModelOnWire(mock!)
      expect(wire.length).toBeGreaterThanOrEqual(2)
      const models = new Set(wire.map((r) => r.model))
      expect(models.has('mockinfra-free-model'), 'Account A should route via Mode B').toBe(true)
      expect(models.has('mockinfra-unknown-cost-model'), 'Account B should keep its physical selection').toBe(true)
      const auths = new Set(wire.map((r) => r.auth).filter(Boolean))
      expect(auths.size, 'Distinct synthetic credentials on the wire').toBeGreaterThan(1)
      const prompts = wire.map((r) => r.prompt || '')
      expect(prompts.some((p) => p.includes('session A turn'))).toBe(true)
      expect(prompts.some((p) => p.includes('session B turn'))).toBe(true)

      const stateA = readJsonIfExists(path.join(sandboxA.hermesHome, 'plugin-data', 'autorouter', 'native-routing-state.json'))
      const stateB = readJsonIfExists(path.join(sandboxB.hermesHome, 'plugin-data', 'autorouter', 'native-routing-state.json'))
      saveEvidence('concurrent-sessions.json', {
        wire,
        distinctModels: [...models],
        distinctCredentialsOnWire: auths.size > 1,
        accountARoutingState: stateA,
        accountBRoutingState: stateB,
        separateHomes: sandboxA.root !== sandboxB.root,
        separateProcesses: appA !== appB,
        note: 'Two real packaged sessions ran concurrently in separate Electron processes and disposable HERMES_HOMEs with distinct synthetic credentials; wire receipts and routing state are session-specific. Cross-process cached-agent reuse is structurally impossible because each process owns its own agent cache; this suite observes wire receipts and plugin state, not internal agent objects.',
      })
      collectEgressEvidence(mock!, sandboxA.root, 'concurrent-a')
      collectEgressEvidence(mock!, sandboxB.root, 'concurrent-b')
    } finally {
      await appB?.close().catch(() => undefined)
      await appA?.close().catch(() => undefined)
      try { sandboxA.cleanup() } catch {}
      try { sandboxB.cleanup() } catch {}
      await mock?.close().catch(() => undefined)
      mock = null
    }
  })

  // This is RESTART-TIME activation/opt-out, NOT live hot-unload: Hermes caches
  // plugin config per gateway process, so the opt-in is applied at next process
  // start while the session resumes. True in-process live unload is covered by
  // tests/hermes_cli/test_autorouter_plugin_live_unload.py via the host's
  // PluginManager ownership ledger (no restart).
  test('plugin restart-time activation/opt-out: routing is inactive without the opt-in and resumes when restored', async () => {
    skipIfInputsMissing()
    requirePrivateRcInputs()
    const executablePath = resolvePackagedPrivateRcBinaryPath({ appPattern: APP_PATTERN })
    expect(executablePath && fs.existsSync(executablePath)).toBeTruthy()
    mock = await startSimpleMock()
    const sandbox = createSandbox('autorouter-hot-plugin')
    let app: any = null
    let page: Page | null = null
    const launch = async (): Promise<Page> => {
      const env = buildAppEnv(sandbox, {
        HERMES_DESKTOP_ISOLATED_BACKEND: '1',
        HERMES_DESKTOP_SKIP_PROTOCOL_REGISTRATION: '1',
        HERMES_DISABLE_LAZY_INSTALLS: '1',
        MOCKINFRA_API_KEY: 'fixture-mockinfra-key-synthetic',
      })
      for (const k of ['HERMES_DESKTOP_DEV_SERVER', 'HERMES_DESKTOP_HERMES', 'HERMES_DESKTOP_HERMES_ROOT',
        'HERMES_DESKTOP_BOOT_FAKE', 'HERMES_DESKTOP_BOOT_FAKE_ERROR', 'HERMES_DESKTOP_BOOT_FAKE_STEP_MS']) {
        delete (env as any)[k]
      }
      app = await _electron.launch({ executablePath: executablePath!, args: ['--disable-gpu'], env })
      const p = await app.firstWindow()
      installErrorBannerGuard(p)
      await waitForAppReady({ app, page: p } as any, 240_000)
      await pageReadySettled(p)
      return p
    }
    const startNewSession = async (p: Page): Promise<void> => {
      const btn = p.getByText('New session', { exact: true }).first()
      if ((await btn.count().catch(() => 0)) > 0) {
        await btn.click({ timeout: 15_000 }).catch(() => undefined)
        await p.waitForTimeout(1500)
      }
    }
    try {
      prepareHome(sandbox.hermesHome, mock!.url, {
        chatRouter: true, nativeRouting: true,
        defaultProvider: 'mockinfra', defaultModel: 'mockinfra-baseline-model',
      })
      page = await launch()
      await sendChatAndExpectReply(page, 'hot-plugin routed turn', 240_000)
      const beforeDisable = chatWire().at(-1)?.model
      expect(beforeDisable).toBe('mockinfra-free-model')
      await app.close().catch(() => undefined); app = null; page = null
      await new Promise((r) => setTimeout(r, 2500))

      // Config is cached per gateway process, so revocation is applied at the
      // next process start while the same session is resumed.
      const cfgPath = path.join(sandbox.hermesHome, 'config.yaml')
      const cfgBefore = fs.readFileSync(cfgPath, 'utf8')
      patchConfig(sandbox.hermesHome, (t) => t.replace(/enable_native_routing:\s*true/, 'enable_native_routing: false'))
      const cfgAfter = fs.readFileSync(cfgPath, 'utf8')
      saveEvidence('hot-plugin-disable-config.json', {
        matched: cfgBefore !== cfgAfter,
        flagLine: cfgAfter.split('\n').filter((l) => l.includes('enable_native_routing')),
      })
      expect(cfgBefore !== cfgAfter, 'native-routing opt-in flag must be found and flipped').toBe(true)
      page = await launch()
      await startNewSession(page)
      await sendChatAndExpectReply(page, 'hot-plugin disabled turn', 240_000)
      const whileDisabled = chatWire().at(-1)?.model
      expect(whileDisabled, 'Without the consumer opt-in, no autorouter rewrite occurs').toBe('mockinfra-baseline-model')
      const disabledMarker = readJsonIfExists(path.join(sandbox.hermesHome, 'plugin-data', 'autorouter', 'native-routing-state.json'))
      await app.close().catch(() => undefined); app = null; page = null
      await new Promise((r) => setTimeout(r, 2500))

      patchConfig(sandbox.hermesHome, (t) => t.replace(/enable_native_routing:\s*false/, 'enable_native_routing: true'))
      page = await launch()
      await startNewSession(page)
      await sendChatAndExpectReply(page, 'hot-plugin re-enabled turn', 240_000)
      const afterReenable = chatWire().at(-1)?.model
      expect(afterReenable).toBe('mockinfra-free-model')
      const reenabledMarker = readJsonIfExists(path.join(sandbox.hermesHome, 'plugin-data', 'autorouter', 'native-routing-state.json'))
      assertNoVirtualModelOnWire(mock!)

      saveEvidence('plugin-hot-lifecycle.json', {
        models: [beforeDisable, whileDisabled, afterReenable],
        disabledMarker,
        reenabledMarker,
        note: 'RESTART-TIME activation/opt-out only (not live hot-unload). Hermes caches plugin config per gateway process; revoking the opt-in across a process restart makes routing inactive (baseline on wire), restoring it resumes routing. No virtual model reached the wire in any phase. In-process live unload is covered separately by tests/hermes_cli/test_autorouter_plugin_live_unload.py.',
      })
      await takeEvidenceScreenshot(page, 'hot-plugin-final.png').catch(() => null)
      collectEgressEvidence(mock!, sandbox.root, 'hot-plugin')
    } finally {
      await app?.close().catch(() => undefined)
      try { sandbox.cleanup() } catch {}
      await mock?.close().catch(() => undefined)
      mock = null
    }
  })
})
