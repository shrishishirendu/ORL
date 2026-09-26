// Health/awards, determinism, validation (422) and unresolved-path tests.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { readGitHead } from '../src/engine.js'
import { createServer } from '../src/server.js'
import { CONTEXT, LEVEL, SHIFTS, WORKERS, clone, harness } from './helpers.js'

const matrixRequest = (overrides = {}) => ({
  award_code: 'MA000016', context: clone(CONTEXT), workers: clone(WORKERS), shifts: clone(SHIFTS), ...overrides,
})
const rosterRequest = (overrides = {}) => ({
  award_code: 'MA000016', context: clone(CONTEXT), period_start: '2026-10-12', period_end: '2026-10-18',
  workers: clone(WORKERS),
  assignments: [
    { worker_id: 1, shifts: [clone(SHIFTS[0]), clone(SHIFTS[1])] },
    { worker_id: 2, shifts: [clone(SHIFTS[2])] },
    { worker_id: 3, shifts: [clone(SHIFTS[3])] },
  ],
  ...overrides,
})

test('health reports the pinned commit', async () => {
  const { call } = await harness()
  const res = await call('GET', '/engine/health')
  assert.equal(res.status, 200)
  assert.equal(res.json.status, 'ok')
  assert.equal(res.json.commit_matches, true)
  assert.equal(res.json.engine_commit, res.json.pinned_commit)
  assert.equal(res.json.pinned_commit, fs.readFileSync(new URL('../award-intelligence.pin', import.meta.url), 'utf8').trim())
})

test('pin mismatch → health 503 degraded and pricing refused', async () => {
  const { engine } = await harness()
  const stub = {
    ...engine,
    commitMatches: false,
    allowUnpinned: false,
    health: () => ({ status: 'degraded', pinned_commit: engine.pinnedCommit, engine_commit: 'f'.repeat(40), commit_matches: false, unpinned: false }),
  }
  const server = createServer(stub)
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    const health = await fetch(`${base}/engine/health`)
    assert.equal(health.status, 503)
    assert.equal((await health.json()).status, 'degraded')
    const priced = await fetch(`${base}/engine/cost-matrix`, { method: 'POST', body: JSON.stringify(matrixRequest()) })
    assert.equal(priced.status, 503)
  } finally {
    server.close()
  }
})

test('readGitHead resolves loose and packed refs without git', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'engine-pin-'))
  fs.mkdirSync(path.join(dir, '.git/refs/heads'), { recursive: true })
  fs.writeFileSync(path.join(dir, '.git/HEAD'), 'ref: refs/heads/main\n')
  fs.writeFileSync(path.join(dir, '.git/packed-refs'), `# pack-refs\n${'a'.repeat(40)} refs/heads/main\n`)
  assert.equal(readGitHead(dir), 'a'.repeat(40))
  fs.writeFileSync(path.join(dir, '.git/refs/heads/main'), `${'b'.repeat(40)}\n`)
  assert.equal(readGitHead(dir), 'b'.repeat(40))
  assert.equal(readGitHead(path.join(dir, 'missing')), null)
})

test('awards lists MA000016 with its verified level keys', async () => {
  const { call } = await harness()
  const res = await call('GET', '/engine/awards')
  assert.equal(res.status, 200)
  const [award] = res.json.awards
  assert.equal(award.code, 'MA000016')
  assert.deepEqual(award.levels.map((level) => level.key), [1, 2, 3, 4, 5].map(LEVEL))
  assert.equal(award.levels[0].minimum_hourly, 28.42) // MA000016@2026-07-01, cl. 15.1 / Sch B
  assert.ok(award.instrument_versions.includes('MA000016@2026-07-01'))
})

test('determinism: the same request twice gives byte-identical JSON; input order does not matter', async () => {
  const { call } = await harness()
  const a = await call('POST', '/engine/cost-matrix', matrixRequest())
  const b = await call('POST', '/engine/cost-matrix', matrixRequest())
  assert.equal(a.text, b.text)
  const shuffled = await call('POST', '/engine/cost-matrix', matrixRequest({ workers: clone(WORKERS).reverse(), shifts: clone(SHIFTS).reverse() }))
  assert.equal(shuffled.text, a.text)
  const rows = a.json.rows.map((row) => [row.worker_id, row.shift_id])
  assert.deepEqual(rows, [...rows].sort((x, y) => x[0] - y[0] || x[1] - y[1]))

  const r1 = await call('POST', '/engine/price-roster', rosterRequest())
  const r2 = await call('POST', '/engine/price-roster', rosterRequest())
  assert.equal(r1.text, r2.text)
  const reordered = rosterRequest()
  reordered.assignments.reverse()
  reordered.assignments.at(-1).shifts.reverse()
  const r3 = await call('POST', '/engine/price-roster', reordered)
  assert.equal(r3.text, r1.text)
})

test('validation: malformed requests are 422 with details', async () => {
  const { call } = await harness()
  const cases = [
    ['unknown award', matrixRequest({ award_code: 'MA000100' }), /award_code must be one of/],
    ['unknown level key', matrixRequest({ workers: [{ ...clone(WORKERS[0]), classification_level: 'MA000016::securityofficerlevel9' }] }), /not a level key/],
    ['bad time', matrixRequest({ shifts: [{ ...clone(SHIFTS[0]), start_time: '25:00' }] }), /start_time must be a time/],
    ['bad date', matrixRequest({ shifts: [{ ...clone(SHIFTS[0]), date: '2026-02-30' }] }), /date must be a real date/],
    ['part_time without hours', matrixRequest({ workers: [{ ...clone(WORKERS[1]), ordinary_hours_per_week: null }] }), /part_time requires ordinary_hours_per_week/],
    ['unknown flag', matrixRequest({ shifts: [{ ...clone(SHIFTS[0]), flags: { free_lunch: true } }] }), /not a recognised engine shift flag/],
    ['bad employment type', matrixRequest({ workers: [{ ...clone(WORKERS[0]), employment_type: 'contractor' }] }), /employment_type must be one of/],
    ['unknown jurisdiction', matrixRequest({ context: { ...CONTEXT, jurisdiction: 'XX' } }), /context.jurisdiction must be one of/],
    ['colon in evidence text', matrixRequest({ context: { ...CONTEXT, legal_employer: 'Acme\nWork Type: x' } }), /must not contain line breaks/],
    ['duplicate shift id', matrixRequest({ shifts: [clone(SHIFTS[0]), clone(SHIFTS[0])] }), /duplicated/],
    ['pair to unknown shift', matrixRequest({ pairs: [[1, 999]] }), /shift_id 999 is not in shifts/],
    ['break longer than shift', matrixRequest({ shifts: [{ ...clone(SHIFTS[0]), break_minutes: 600 }] }), /break_minutes must be shorter/],
  ]
  for (const [name, body, pattern] of cases) {
    const res = await call('POST', '/engine/cost-matrix', body)
    assert.equal(res.status, 422, name)
    assert.equal(res.json.error, 'validation', name)
    assert.ok(res.json.details.some((detail) => pattern.test(detail)), `${name}: ${res.json.details}`)
  }
  const outside = await call('POST', '/engine/price-roster', rosterRequest({ period_end: '2026-10-17' }))
  assert.equal(outside.status, 422)
  assert.ok(outside.json.details.some((detail) => /outside the period/.test(detail)))
  const badJson = await call('POST', '/engine/price-roster', '{nope', { raw: true })
  assert.equal(badJson.status, 422)
  const notFound = await call('GET', '/engine/nothing')
  assert.equal(notFound.status, 404)
})

test('unresolved: missing employment type → engine issue, no $, never a guess', async () => {
  const { call } = await harness()
  const worker = { ...clone(WORKERS[0]), employment_type: null }
  const matrix = await call('POST', '/engine/cost-matrix', matrixRequest({ workers: [worker], pairs: [[1, 101]] }))
  assert.equal(matrix.status, 200, matrix.text)
  const [row] = matrix.json.rows
  assert.equal(row.status, 'unresolved')
  assert.equal(row.eligible, false)
  assert.equal(row.pay_cost, null)
  assert.ok(row.reasons.some((reason) => /employment type is required/.test(reason)), row.reasons.join('; '))

  const roster = await call('POST', '/engine/price-roster', rosterRequest({ workers: [worker, clone(WORKERS[1]), clone(WORKERS[2])] }))
  assert.equal(roster.status, 200, roster.text)
  assert.equal(roster.json.total_cost, null)
  assert.deepEqual(roster.json.unresolved_worker_ids, [1])
  const w1 = roster.json.workers.find((entry) => entry.worker_id === 1)
  assert.equal(w1.total_pay, null)
  assert.ok(w1.issues.length > 0)
  assert.equal(roster.json.workers.find((entry) => entry.worker_id === 2).status, 'resolved')
})

test('unresolved: overnight break with no break_start (engine will not guess where the break fell)', async () => {
  const { call } = await harness()
  const night = { ...clone(SHIFTS[1]) }
  delete night.break_start
  const res = await call('POST', '/engine/cost-matrix', matrixRequest({ workers: [clone(WORKERS[0])], shifts: [night] }))
  const [row] = res.json.rows
  assert.equal(row.status, 'unresolved')
  assert.equal(row.pay_cost, null)
  assert.ok(row.reasons.some((reason) => /Break start is required/.test(reason)), row.reasons.join('; '))
})

test('unresolved: worker with no classification level is priced by nobody', async () => {
  const { call } = await harness()
  const worker = { ...clone(WORKERS[0]), classification_level: null }
  const res = await call('POST', '/engine/cost-matrix', matrixRequest({ workers: [worker], pairs: [[1, 101]] }))
  const [row] = res.json.rows
  assert.equal(row.status, 'unresolved')
  assert.equal(row.pay_cost, null)
  assert.ok(row.reasons.some((reason) => /not a verified classification/.test(reason)), row.reasons.join('; '))
})

test('public holiday gap is flagged, not fixed: Christmas Day without the flag', async () => {
  const { call } = await harness()
  const xmas = { shift_id: 501, date: '2026-12-25', start_time: '08:00', end_time: '16:00', break_minutes: 30, site_code: 'STH-01' }
  const flagged = { ...xmas, shift_id: 502, flags: { public_holiday: true } }
  const res = await call('POST', '/engine/cost-matrix', matrixRequest({ workers: [clone(WORKERS[0])], shifts: [xmas, flagged] }))
  const [plain, withFlag] = res.json.rows
  assert.ok(plain.warnings.some((warning) => /Christmas Day/.test(warning)), plain.warnings.join('; '))
  assert.ok(withFlag.pay_cost > plain.pay_cost)
  assert.ok(!withFlag.warnings.some((warning) => /Christmas Day/.test(warning)))
})
