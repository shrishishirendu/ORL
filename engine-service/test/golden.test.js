// Golden / contract tests: wrapper output == a direct award-intelligence call
// on hand-written engine inputs for the same facts.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { CONTEXT, SHIFTS, WORKERS, clone, directCache, directShift, harness } from './helpers.js'

const MON = '2026-10-12'
// Direct timesheet shifts, written by hand in timesheetParser.js shape.
const D = {
  101: (who) => directShift({ id: 101, ...who, date: '2026-10-12', day: 'Monday', week: MON, start: '07:00', finish: '15:00', hours: 7.5, location: 'STH-01' }),
  102: (who) => directShift({ id: 102, ...who, date: '2026-10-13', day: 'Tuesday', week: MON, start: '22:00', finish: '06:00', breakStart: '02:00', hours: 7.5, location: 'STH-01' }),
  103: (who) => directShift({ id: 103, ...who, date: '2026-10-17', day: 'Saturday', week: MON, start: '08:00', finish: '16:00', hours: 7.5, location: 'CBD-01', extra: { firstAidNominated: true } }),
  104: (who) => directShift({ id: 104, ...who, date: '2026-10-18', day: 'Sunday', week: MON, start: '08:00', finish: '16:00', hours: 7.5, location: 'CBD-01' }),
}
const WHO = {
  1: { employeeId: '1', employmentType: 'Full-time' },
  2: { employeeId: '2', employmentType: 'Part-time' },
  3: { employeeId: '3', employmentType: 'Casual' },
}
const NAMES = { 1: 'Sophie Chen', 2: 'Tom Anderson', 3: 'Priya Sharma' }
const identity = (id) => ({ employeeId: String(id), employeeName: NAMES[id], jobRole: '', employmentType: WHO[id].employmentType })

const ASSIGNMENTS = [
  { worker_id: 1, shifts: [SHIFTS[0], SHIFTS[1]] }, // weekday day + night across midnight
  { worker_id: 2, shifts: [SHIFTS[2]] },            // Saturday
  { worker_id: 3, shifts: [SHIFTS[3]] },            // Sunday
]

test('price-roster equals calculateTimesheetResults on hand-written inputs', async () => {
  const { call, mods, engine } = await harness()
  const res = await call('POST', '/engine/price-roster', {
    award_code: 'MA000016', context: CONTEXT, period_start: '2026-10-12', period_end: '2026-10-18',
    workers: clone(WORKERS), assignments: clone(ASSIGNMENTS),
  })
  assert.equal(res.status, 200, res.text)

  const cache = await directCache(mods, engine)
  const employees = [
    { ...identity(1), shifts: [D[101](WHO[1]), D[102](WHO[1])], totalHours: 15 },
    { ...identity(2), shifts: [D[103](WHO[2])], totalHours: 7.5 },
    { ...identity(3), shifts: [D[104](WHO[3])], totalHours: 7.5 },
  ]
  const shifts = employees.flatMap((employee) => employee.shifts)
  const direct = mods.calculateTimesheetResults(cache, { meta: {}, employees, shifts, totalHours: 30 }, { jurisdiction: 'NSW' })

  assert.equal(res.json.total_cost, direct.stats.totalCalculatedPay)
  assert.deepEqual(res.json.unresolved_worker_ids, [])
  assert.deepEqual(res.json.rate_validity, direct.rateValidity)
  assert.deepEqual(res.json.warnings, direct.warnings)
  direct.rows.forEach((row, index) => {
    const out = res.json.workers[index]
    assert.equal(out.worker_id, Number(row.employeeId))
    assert.equal(row.calculationStatus, 'resolved')
    assert.equal(out.status, 'resolved')
    assert.equal(out.total_pay, row.totalCalculatedPay)
    assert.equal(out.ordinary_pay, row.ordinaryPay)
    assert.deepEqual(out.items, row.extrasAllowances.items.map((item) => ({
      type: item.type, category: item.category, amount: item.amount, clause: item.clause || '', detail: item.detail || '',
    })))
    assert.deepEqual(out.release_blocking_gaps, [...new Set(row.releaseBlockingGaps)].sort())
    assert.deepEqual(out.instrument_versions, row.instrumentVersions)
  })
  // Sanity: every figure really is the engine's (e.g. Sunday casual 2.25× on L3 minimum,
  // Saturday part-time 1.5×, first-aid allowance present).
  assert.ok(res.json.workers[1].items.some((item) => item.type === 'First aid allowance'))
  assert.ok(res.json.workers[0].items.some((item) => item.type === 'Weekday night penalty'))
  assert.ok(res.json.workers[2].items.some((item) => item.type === 'Sunday penalty'))
})

test('cost-matrix pay_cost equals marginalCost().cost for every cell (empty baseline)', async () => {
  const { call, mods, engine } = await harness()
  const res = await call('POST', '/engine/cost-matrix', {
    award_code: 'MA000016', context: CONTEXT, workers: clone(WORKERS), shifts: clone(SHIFTS),
  })
  assert.equal(res.status, 200, res.text)
  assert.equal(res.json.rows.length, 12)

  const cache = await directCache(mods, engine)
  for (const row of res.json.rows) {
    const marginal = mods.marginalCost(cache, identity(row.worker_id), [], [D[row.shift_id](WHO[row.worker_id])])
    assert.equal(row.status, 'resolved', `${row.worker_id}/${row.shift_id}: ${row.reasons}`)
    assert.equal(row.eligible, true)
    assert.equal(row.pay_cost, marginal.cost, `cell ${row.worker_id}/${row.shift_id}`)
    assert.deepEqual(row.driving_items, marginal.drivingItems.map(({ type, amount, clause }) => ({ type, amount, clause })))
    assert.equal(row.pricing_method, 'marginalCost')
    assert.equal(row.min_hours, null)
    assert.equal(row.max_hours, null)
  }
})

// Five 7.6 h weekday shifts = 38 h, the MA000016 weekly ordinary-hours limit.
const BASE_WEEK = ['2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23'].map((date, index) => ({
  shift_id: 900 + index, date, start_time: '07:00', end_time: '14:36', break_minutes: 0, site_code: 'STH-01',
}))
const SAT = { shift_id: 201, date: '2026-10-24', start_time: '08:00', end_time: '16:00', break_minutes: 30, site_code: 'STH-01' }

test('baseline effect: a shift pushing the week into overtime costs more than on an empty week', async () => {
  const { call, mods, engine } = await harness()
  const request = (baseline) => ({
    award_code: 'MA000016', context: CONTEXT, workers: [clone(WORKERS[0])], shifts: [clone(SAT)], baseline,
  })
  const empty = await call('POST', '/engine/cost-matrix', request(undefined))
  const loaded = await call('POST', '/engine/cost-matrix', request({ 1: clone(BASE_WEEK) }))
  assert.equal(empty.status, 200, empty.text)
  assert.equal(loaded.status, 200, loaded.text)
  const [emptyRow] = empty.json.rows
  const [loadedRow] = loaded.json.rows
  assert.equal(loadedRow.status, 'resolved', loadedRow.reasons.join('; '))
  assert.ok(loadedRow.pay_cost > emptyRow.pay_cost, `${loadedRow.pay_cost} should exceed ${emptyRow.pay_cost}`)
  assert.ok(loadedRow.driving_items.some((item) => /overtime/i.test(item.type)))

  // Golden: Saturday sorts after the baseline, so the engine's marginalCost is used verbatim.
  const cache = await directCache(mods, engine)
  const week = '2026-10-19'
  const base = BASE_WEEK.map((shift, index) => directShift({
    id: shift.shift_id, ...WHO[1], date: shift.date, day: ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'][index],
    week, start: '07:00', finish: '14:36', breakMinutes: 0, hours: 7.6, location: 'STH-01',
  }))
  const cell = directShift({ id: 201, ...WHO[1], date: '2026-10-24', day: 'Saturday', week, start: '08:00', finish: '16:00', hours: 7.5, location: 'STH-01' })
  assert.equal(loadedRow.pricing_method, 'marginalCost')
  assert.equal(loadedRow.pay_cost, mods.marginalCost(cache, identity(1), base, [cell]).cost)
})

test('a cell earlier than baseline shifts is priced on chronological input (engine order quirk)', async () => {
  const { call, mods, engine } = await harness()
  // Baseline Tue–Sat (5 × 7.6 h); the cell is Monday of the same week.
  const baseline = ['2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23', '2026-10-24'].map((date, index) => ({
    shift_id: 910 + index, date, start_time: '07:00', end_time: '14:36', break_minutes: 0, site_code: 'STH-01',
  }))
  const monday = { shift_id: 301, date: '2026-10-19', start_time: '07:00', end_time: '14:36', break_minutes: 0, site_code: 'STH-01' }
  const res = await call('POST', '/engine/cost-matrix', {
    award_code: 'MA000016', context: CONTEXT, workers: [clone(WORKERS[0])], shifts: [monday], baseline: { 1: baseline },
  })
  assert.equal(res.status, 200, res.text)
  const [row] = res.json.rows
  assert.equal(row.pricing_method, 'chronological-difference')

  const cache = await directCache(mods, engine)
  const days = ['Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
  const base = baseline.map((shift, index) => directShift({ id: shift.shift_id, ...WHO[1], date: shift.date, day: days[index], week: '2026-10-19', start: '07:00', finish: '14:36', breakMinutes: 0, hours: 7.6, location: 'STH-01' }))
  const cell = directShift({ id: 301, ...WHO[1], date: '2026-10-19', day: 'Monday', week: '2026-10-19', start: '07:00', finish: '14:36', breakMinutes: 0, hours: 7.6, location: 'STH-01' })
  const chronological = mods.round2(mods.calcRow(cache, identity(1), [cell, ...base]).totalCalculatedPay - mods.calcRow(cache, identity(1), base).totalCalculatedPay)
  assert.equal(row.pay_cost, chronological)
  // Documented quirk: the engine's marginalCost appends the cover LAST, so the
  // weekly overtime lands on Monday instead of Saturday and the figure differs.
  const appended = mods.marginalCost(cache, identity(1), base, [cell]).cost
  assert.notEqual(appended, chronological)
})

test('a shift already in the baseline is priced against the rest of the week, not added twice', async () => {
  const { call } = await harness()
  const res = await call('POST', '/engine/cost-matrix', {
    award_code: 'MA000016', context: CONTEXT, workers: [clone(WORKERS[0])], shifts: [clone(SAT)],
    baseline: { 1: [...clone(BASE_WEEK), clone(SAT)] },
  })
  const withoutDup = await call('POST', '/engine/cost-matrix', {
    award_code: 'MA000016', context: CONTEXT, workers: [clone(WORKERS[0])], shifts: [clone(SAT)], baseline: { 1: clone(BASE_WEEK) },
  })
  assert.equal(res.json.rows[0].pay_cost, withoutDup.json.rows[0].pay_cost)
})

test('engine hard limit (work period > 14 h) makes the cell ineligible with the engine reason', async () => {
  const { call } = await harness()
  const long = { shift_id: 401, date: '2026-10-14', start_time: '06:00', end_time: '21:00', break_minutes: 30, break_start: '12:00', site_code: 'STH-01' }
  const res = await call('POST', '/engine/cost-matrix', {
    award_code: 'MA000016', context: CONTEXT, workers: [clone(WORKERS[0])], shifts: [long],
  })
  const [row] = res.json.rows
  assert.equal(row.status, 'resolved')
  assert.equal(row.eligible, false)
  assert.ok(typeof row.pay_cost === 'number')
  assert.match(row.reasons[0], /exceeds the 14 hour maximum/)
})
