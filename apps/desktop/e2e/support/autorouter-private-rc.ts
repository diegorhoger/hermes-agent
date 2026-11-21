import { execFileSync } from 'node:child_process'
import * as crypto from 'node:crypto'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

export const AUTOROUTER_PINNED_SHA = '8252022a309a8f4b887524174fe6351fbb225c15'
export const AUTOROUTER_ARCHIVE_BASENAME =
  'autorouter-pinned-8252022a309a8f4b887524174fe6351fbb225c15.tar.gz'
export const AUTOROUTER_ARCHIVE_SHA256 =
  '809bd0a721a6de90c54f98e7906f71df04f392495731b888553d4c32dfc46401'

const THIS_DIR = path.resolve(import.meta.dirname)
export const PREPARE_AUTOROUTER_HOME_SCRIPT = path.join(THIS_DIR, 'prepare_autorouter_home.py')
export const BUNDLED_PINNED_ARCHIVE = path.join(
  THIS_DIR,
  'fixtures',
  AUTOROUTER_ARCHIVE_BASENAME,
)

export function isPrivateRcAcceptanceMode(): boolean {
  const v = (process.env.AUTOROUTER_PRIVATE_RC || process.env.AUTOROUTER_PRIVATE_RC_ACCEPTANCE || '').trim().toLowerCase()
  return v === '1' || v === 'true' || v === 'yes'
}

function sha256File(filePath: string): string {
  const hash = crypto.createHash('sha256')
  hash.update(fs.readFileSync(filePath))
  return hash.digest('hex')
}

function assertFileExists(filePath: string, label: string): void {
  if (!fs.existsSync(filePath)) throw new Error(`${label} missing: ${filePath}`)
}

function verifyArchive(filePath: string): void {
  assertFileExists(filePath, 'Pinned AutoRouter archive')
  const actual = sha256File(filePath)
  if (actual !== AUTOROUTER_ARCHIVE_SHA256) {
    throw new Error(
      `Pinned AutoRouter archive SHA256 mismatch for ${filePath}: expected ${AUTOROUTER_ARCHIVE_SHA256}, got ${actual}`,
    )
  }
}

/** Defense in depth: never let a member path or symlink resolve outside the extraction root. */
export function assertArchivePathsSafe(filePath: string): void {
  const names: string = execFileSync('tar', ['-tzf', filePath], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
  for (const raw of names.split('\n')) {
    const entry = raw.trim()
    if (!entry) continue
    if (entry.startsWith('/') || /^[A-Za-z]:[\\/]/.test(entry)) {
      throw new Error(`Pinned archive contains an absolute path; refusing extraction: ${entry}`)
    }
    if (entry.split('/').some((part) => part === '..')) {
      throw new Error(`Pinned archive contains a parent traversal path; refusing extraction: ${entry}`)
    }
  }
  const verbose: string = execFileSync('tar', ['-tvzf', filePath], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
  for (const line of verbose.split('\n')) {
    if (line && line[0] === 'l') {
      throw new Error(`Pinned archive contains a symlink entry; refusing extraction: ${line.slice(0, 200)}`)
    }
  }
}

function looksLikePinnedAutorouterSource(dir: string): boolean {
  if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) return false
  return fs.existsSync(path.join(dir, 'plugin.yaml')) && fs.existsSync(path.join(dir, '__init__.py'))
}

function findExtractedSourceRoot(extractDir: string): string {
  if (looksLikePinnedAutorouterSource(extractDir)) return extractDir
  const entries = fs.readdirSync(extractDir, { withFileTypes: true }).filter((e) => e.isDirectory())
  for (const e of entries) {
    const p = path.join(extractDir, e.name)
    if (looksLikePinnedAutorouterSource(p)) return p
  }
  // fallback: single directory
  if (entries.length === 1) return path.join(extractDir, entries[0]!.name)
  throw new Error(`Could not locate pinned AutoRouter plugin source root in extracted archive: ${extractDir}`)
}

let cachedExtractedSource: string | null = null

/** Test seam: isolated acceptance checks must not inherit another run's extraction. */
export function resetPinnedAutorouterSourceCacheForTests(): void {
  cachedExtractedSource = null
}

export function resolvePinnedAutorouterSource(): string {
  if (cachedExtractedSource && fs.existsSync(cachedExtractedSource)) return cachedExtractedSource

  const sourceDirEnv = (process.env.AUTOROUTER_PINNED_SOURCE_DIR || '').trim()
  if (sourceDirEnv) {
    if (isPrivateRcAcceptanceMode()) {
      throw new Error('Private RC acceptance requires SHA256-verified pinned archive; AUTOROUTER_PINNED_SOURCE_DIR is not authenticated')
    }
    const resolved = path.resolve(sourceDirEnv)
    if (!looksLikePinnedAutorouterSource(resolved)) {
      throw new Error(
        `AUTOROUTER_PINNED_SOURCE_DIR does not look like pinned AutoRouter source (missing plugin.yaml/__init__.py): ${resolved}`,
      )
    }
    cachedExtractedSource = resolved
    return resolved
  }

  const archiveEnv = (process.env.AUTOROUTER_PINNED_ARCHIVE || '').trim()
  const archivePath = path.resolve(archiveEnv || BUNDLED_PINNED_ARCHIVE)
  verifyArchive(archivePath)
  assertArchivePathsSafe(archivePath)

  const extractBase = fs.mkdtempSync(path.join(os.tmpdir(), 'autorouter-pinned-src-'))
  execFileSync('tar', ['-xzf', archivePath, '-C', extractBase], { stdio: 'pipe' })
  const sourceRoot = findExtractedSourceRoot(extractBase)
  if (!looksLikePinnedAutorouterSource(sourceRoot)) {
    throw new Error(`Extracted pinned AutoRouter source is invalid: ${sourceRoot}`)
  }
  // Best-effort sanity: plugin.yaml should mention expected manifest name or be present; pinned SHA is enforced by archive hash + contract.
  cachedExtractedSource = sourceRoot
  return sourceRoot
}

export function resolveEvidenceDir(defaultBaseDir: string): string {
  const envDir = (process.env.AUTOROUTER_E2E_EVIDENCE_DIR || '').trim()
  const dir = path.resolve(envDir || path.join(defaultBaseDir, 'artifacts', 'autorouter-private-rc-e2e'))
  fs.mkdirSync(dir, { recursive: true })
  return dir
}

export interface PrivateRcInputs {
  prepareHomeScript: string
  pinnedAutorouterSourceDir: string
  evidenceDir: string
  pinnedSha: string
  archiveSha256: string
  privateRcMode: boolean
}

export function resolvePrivateRcInputs(defaultBaseDir: string): PrivateRcInputs {
  assertFileExists(PREPARE_AUTOROUTER_HOME_SCRIPT, 'prepare_autorouter_home.py')
  const pinnedAutorouterSourceDir = resolvePinnedAutorouterSource()
  const evidenceDir = resolveEvidenceDir(defaultBaseDir)
  return {
    prepareHomeScript: PREPARE_AUTOROUTER_HOME_SCRIPT,
    pinnedAutorouterSourceDir,
    evidenceDir,
    pinnedSha: AUTOROUTER_PINNED_SHA,
    archiveSha256: AUTOROUTER_ARCHIVE_SHA256,
    privateRcMode: isPrivateRcAcceptanceMode(),
  }
}

export function getMissingPrivateRcInputsReason(): string | null {
  try {
    if (!fs.existsSync(PREPARE_AUTOROUTER_HOME_SCRIPT)) return `prepare_autorouter_home.py missing: ${PREPARE_AUTOROUTER_HOME_SCRIPT}`
    const sourceDirEnv = (process.env.AUTOROUTER_PINNED_SOURCE_DIR || '').trim()
    if (sourceDirEnv) {
      if (isPrivateRcAcceptanceMode()) return 'AUTOROUTER_PINNED_SOURCE_DIR is not authenticated in private RC acceptance mode; use the pinned archive'
      const resolved = path.resolve(sourceDirEnv)
      if (!looksLikePinnedAutorouterSource(resolved)) return `AUTOROUTER_PINNED_SOURCE_DIR invalid: ${resolved}`
      return null
    }
    const archiveEnv = (process.env.AUTOROUTER_PINNED_ARCHIVE || '').trim()
    const archivePath = path.resolve(archiveEnv || BUNDLED_PINNED_ARCHIVE)
    if (!fs.existsSync(archivePath)) return `Pinned AutoRouter archive missing: ${archivePath}`
    const actual = sha256File(archivePath)
    if (actual !== AUTOROUTER_ARCHIVE_SHA256) return `Pinned AutoRouter archive SHA256 mismatch: ${archivePath}`
    return null
  } catch (e) {
    return e instanceof Error ? e.message : String(e)
  }
}
