/**
 * Dispatch 160 pinning-contract acceptance for the private RC harness.
 *
 * These are helper-level tests (vitest `electron` project): no Electron, no
 * app, no network. They prove the fail-closed inputs contract that the packaged
 * Playwright suite relies on, without ever contacting a third-party service.
 */
import { execFileSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  assertArchivePathsSafe,
  AUTOROUTER_ARCHIVE_BASENAME,
  AUTOROUTER_ARCHIVE_SHA256,
  AUTOROUTER_PINNED_SHA,
  BUNDLED_PINNED_ARCHIVE,
  getMissingPrivateRcInputsReason,
  isPrivateRcAcceptanceMode,
  resolvePinnedAutorouterSource,
  resolvePrivateRcInputs,
  resetPinnedAutorouterSourceCacheForTests,
} from './support/autorouter-private-rc'

const ENV_KEYS = [
  'AUTOROUTER_PRIVATE_RC',
  'AUTOROUTER_PRIVATE_RC_ACCEPTANCE',
  'AUTOROUTER_PINNED_SOURCE_DIR',
  'AUTOROUTER_PINNED_ARCHIVE',
  'AUTOROUTER_E2E_EVIDENCE_DIR',
] as const

const saved: Record<string, string | undefined> = {}
let tempDirs: string[] = []

beforeEach(() => {
  for (const key of ENV_KEYS) {
    saved[key] = process.env[key]
    delete process.env[key]
  }
  tempDirs = []
  resetPinnedAutorouterSourceCacheForTests()
})

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (saved[key] === undefined) delete process.env[key]
    else process.env[key] = saved[key]
  }
  resetPinnedAutorouterSourceCacheForTests()
  for (const dir of tempDirs) fs.rmSync(dir, { recursive: true, force: true })
})

function tmp(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'autorouter-pinning-test-'))
  tempDirs.push(dir)
  return dir
}

function writeSourceDir(withYaml: boolean, withInit: boolean): string {
  const dir = tmp()
  if (withYaml) fs.writeFileSync(path.join(dir, 'plugin.yaml'), 'name: autorouter\n')
  if (withInit) fs.writeFileSync(path.join(dir, '__init__.py'), '# pinned plugin\n')
  return dir
}

describe('private RC pinning contract', () => {
  it('accepts the SHA256-verified bundled archive', () => {
    expect(fs.existsSync(BUNDLED_PINNED_ARCHIVE)).toBe(true)
    expect(path.basename(BUNDLED_PINNED_ARCHIVE)).toBe(AUTOROUTER_ARCHIVE_BASENAME)
    expect(getMissingPrivateRcInputsReason()).toBeNull()
    expect(isPrivateRcAcceptanceMode()).toBe(false)
  })

  it('fails when the pinned archive is missing', () => {
    process.env.AUTOROUTER_PINNED_ARCHIVE = path.join(tmp(), 'absent.tar.gz')
    const reason = getMissingPrivateRcInputsReason()
    expect(reason).toBeTruthy()
    expect(reason).toMatch(/archive missing/i)
  })

  it('fails when the pinned archive is modified', () => {
    const dir = tmp()
    const archive = path.join(dir, AUTOROUTER_ARCHIVE_BASENAME)
    const bytes = fs.readFileSync(BUNDLED_PINNED_ARCHIVE)
    bytes[bytes.length - 1] = bytes[bytes.length - 1]! ^ 0xff
    fs.writeFileSync(archive, bytes)
    process.env.AUTOROUTER_PINNED_ARCHIVE = archive
    const reason = getMissingPrivateRcInputsReason()
    expect(reason).toBeTruthy()
    expect(reason).toMatch(/SHA256 mismatch/i)
    expect(() => resolvePinnedAutorouterSource()).toThrow(/SHA256 mismatch/i)
  })

  it('rejects an unauthenticated source-dir override in private acceptance mode', () => {
    process.env.AUTOROUTER_PRIVATE_RC = '1'
    process.env.AUTOROUTER_PINNED_SOURCE_DIR = writeSourceDir(true, true)
    expect(isPrivateRcAcceptanceMode()).toBe(true)
    expect(getMissingPrivateRcInputsReason()).toMatch(/not authenticated/i)
    expect(() => resolvePinnedAutorouterSource()).toThrow(/not authenticated/i)
  })

  it('requires BOTH plugin.yaml and __init__.py in a non-private source-dir override', () => {
    for (const [yaml, init] of [[true, false], [false, true], [false, false]] as const) {
      process.env.AUTOROUTER_PINNED_SOURCE_DIR = writeSourceDir(yaml, init)
      const reason = getMissingPrivateRcInputsReason()
      expect(reason, `yaml=${yaml} init=${init}`).toBeTruthy()
      expect(reason).toMatch(/invalid|missing plugin\.yaml/i)
      expect(() => resolvePinnedAutorouterSource()).toThrow(/missing plugin\.yaml|invalid/i)
      resetPinnedAutorouterSourceCacheForTests()
    }
  })

  it('accepts a complete source-dir override only outside private mode', () => {
    const dir = writeSourceDir(true, true)
    process.env.AUTOROUTER_PINNED_SOURCE_DIR = dir
    expect(getMissingPrivateRcInputsReason()).toBeNull()
    expect(resolvePinnedAutorouterSource()).toBe(path.resolve(dir))
  })

  it('extracts the verified archive into a source root with both required files', () => {
    const source = resolvePinnedAutorouterSource()
    expect(fs.existsSync(path.join(source, 'plugin.yaml'))).toBe(true)
    expect(fs.existsSync(path.join(source, '__init__.py'))).toBe(true)
    // The extraction root is disposable (under the OS temp dir), never the live checkout.
    expect(source.startsWith(os.tmpdir())).toBe(true)
  })

  it('documents setup that matches the actually accepted inputs', () => {
    const dir = tmp()
    process.env.AUTOROUTER_E2E_EVIDENCE_DIR = path.join(dir, 'evidence')
    const inputs = resolvePrivateRcInputs(path.resolve(__dirname, '../../..'))
    expect(inputs.pinnedSha).toBe(AUTOROUTER_PINNED_SHA)
    expect(inputs.archiveSha256).toBe(AUTOROUTER_ARCHIVE_SHA256)
    expect(fs.existsSync(inputs.prepareHomeScript)).toBe(true)
    expect(fs.existsSync(inputs.evidenceDir)).toBe(true)
  })
})

describe('pinned archive extraction cannot escape the extraction root', () => {
  const CRAFT = [
    'import tarfile, io, sys',
    'archive, kind, name = sys.argv[1:4]',
    'with tarfile.open(archive, "w:gz") as tf:',
    '    if kind == "file":',
    '        data = b"x"',
    '        info = tarfile.TarInfo(name); info.size = len(data)',
    '        tf.addfile(info, io.BytesIO(data))',
    '    else:',
    '        info = tarfile.TarInfo(name); info.type = tarfile.SYMTYPE; info.linkname = "/etc"',
    '        tf.addfile(info)',
  ].join('\n')

  function makeMaliciousTar(kind: 'file' | 'symlink', name: string): string {
    const archive = path.join(tmp(), 'malicious.tar.gz')
    execFileSync('python3', ['-c', CRAFT, archive, kind, name], { stdio: 'pipe' })
    return archive
  }

  it('rejects absolute member paths', () => {
    const archive = makeMaliciousTar('file', '/tmp/autorouter-absolute-escape-test')
    expect(() => assertArchivePathsSafe(archive)).toThrow(/absolute path/i)
  })

  it('rejects parent traversal member paths', () => {
    const archive = makeMaliciousTar('file', '../escaped-payload/x')
    expect(() => assertArchivePathsSafe(archive)).toThrow(/parent traversal/i)
  })

  it('rejects symlink members', () => {
    const archive = makeMaliciousTar('symlink', 'payload/link')
    expect(() => assertArchivePathsSafe(archive)).toThrow(/symlink/i)
  })

  it('accepts the genuine pinned archive (no unsafe members)', () => {
    expect(() => assertArchivePathsSafe(BUNDLED_PINNED_ARCHIVE)).not.toThrow()
  })
})
