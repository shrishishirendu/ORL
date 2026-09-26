// Award Engine service: loading the award-intelligence engine and mapping
// ORL's structured Worker/Shift JSON into the engine's own input shapes.
//
// This module never computes an award $ figure. Every amount it returns comes
// from award-intelligence's calculateTimesheetResults (directly, via the
// engine's calcRow wrapper, or via the engine's marginalCost). What lives here:
//   * loading the engine modules from AWARD_INTELLIGENCE_PATH (file URLs),
//   * reading the engine checkout's commit for the pin check,
//   * Worker → agreement-profile TEXT (fed to the engine's own
//     buildParsedCacheFromTexts + the bundled MA000016 award library),
//   * Shift → timesheet shift objects (the timesheetParser.js shape),
//   * mapping engine rows back to the contract's JSON.

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { SHIFT_FLAGS } from './validate.js'

const SERVICE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// Awards whose calculation path is fully supported by the engine (versioned,
// verified rule registry). Contract: only these are listed / accepted.
const SUPPORTED_AWARDS = Object.freeze({
  MA000016: { industry: 'security' },
})

const EMPLOYMENT_LABELS = Object.freeze({ full_time: 'Full-time', part_time: 'Part-time', casual: 'Casual' })
const DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
// Engine warning emitted by securityCalculator when a work period exceeds the
// award maximum (cl. 19.1). The contract maps shift-level hard limits to
// eligible=false; we detect the engine's own statement rather than re-derive it.
const HARD_LIMIT_WARNING = /hour work period exceeds the \d+(?:\.\d+)? hour maximum/

export function resolveAwardIntelligencePath(env = process.env) {
  return path.resolve(SERVICE_ROOT, env.AWARD_INTELLIGENCE_PATH || '../../award-intelligence')
}

export function readPinnedCommit(pinFile = path.join(SERVICE_ROOT, 'award-intelligence.pin')) {
  return fs.readFileSync(pinFile, 'utf8').trim()
}

/** HEAD commit of a git checkout, read from .git without invoking git. */
export function readGitHead(repoPath) {
  try {
    let gitDir = path.join(repoPath, '.git')
    if (fs.statSync(gitDir).isFile()) {
      const pointer = fs.readFileSync(gitDir, 'utf8').match(/^gitdir:\s*(.+)$/m)
      if (!pointer) return null
      gitDir = path.resolve(repoPath, pointer[1].trim())
    }
    const head = fs.readFileSync(path.join(gitDir, 'HEAD'), 'utf8').trim()
    if (/^[0-9a-f]{40}$/.test(head)) return head
    const ref = head.match(/^ref:\s*(.+)$/)?.[1]
    if (!ref) return null
    // Linked worktrees keep refs in the common dir.
    const commonDir = fs.existsSync(path.join(gitDir, 'commondir'))
      ? path.resolve(gitDir, fs.readFileSync(path.join(gitDir, 'commondir'), 'utf8').trim())
      : gitDir
    for (const dir of [gitDir, commonDir]) {
      const loose = path.join(dir, ref)
      if (fs.existsSync(loose)) return fs.readFileSync(loose, 'utf8').trim()
    }
    const packed = path.join(commonDir, 'packed-refs')
    if (fs.existsSync(packed)) {
      for (const line of fs.readFileSync(packed, 'utf8').split('\n')) {
        const [hash, name] = line.trim().split(' ')
        if (name === ref && /^[0-9a-f]{40}$/.test(hash)) return hash
      }
    }
    return null
  } catch {
    return null
  }
}

async function importEngineModules(aiPath) {
  const load = (relative) => import(pathToFileURL(path.join(aiPath, relative)).href)
  const [cacheBuilder, payCalculator, coverage, awardLibraryFs, registry, resolver, publicHolidays, rateValidity, timesheetDates, utils] = await Promise.all([
    load('src/domain/cacheBuilder.js'),
    load('src/domain/payCalculator.js'),
    load('src/engines/coverage.js'),
    load('server/awardLibraryFs.js'),
    load('src/domain/instruments/registry.js'),
    load('src/domain/instruments/resolver.js'),
    load('src/domain/publicHolidays.js'),
    load('src/domain/rateValidity.js'),
    load('src/domain/timesheetDates.js'),
    load('src/domain/utils.js'),
  ])
  return {
    buildParsedCacheFromTexts: cacheBuilder.buildParsedCacheFromTexts,
    calculateTimesheetResults: payCalculator.calculateTimesheetResults,
    marginalCost: coverage.marginalCost,
    calcRow: coverage.calcRow,
    loadAwardLibraryFs: awardLibraryFs.loadAwardLibraryFs,
    listInstrumentVersions: registry.listInstrumentVersions,
    classificationForVersion: resolver.classificationForVersion,
    JURISDICTIONS: publicHolidays.JURISDICTIONS,
    createCalendarSet: publicHolidays.createCalendarSet,
    resolvePublicHoliday: publicHolidays.resolvePublicHoliday,
    assessRates: rateValidity.assessRates,
    payPeriodFromTimesheet: rateValidity.payPeriodFromTimesheet,
    weekBucketFor: timesheetDates.weekBucketFor,
    round2: utils.round2,
  }
}

function buildAwardCatalog(mods, aiPath) {
  const catalog = {}
  for (const [code, { industry }] of Object.entries(SUPPORTED_AWARDS)) {
    const libraryEntries = mods.loadAwardLibraryFs(path.join(aiPath, 'src/domain/awardLibrary'), industry)
    const entry = libraryEntries.find((item) => item.parsedAward?.awardCode === code)
    const versions = [...mods.listInstrumentVersions(code)].sort((a, b) => a.effectiveFrom.localeCompare(b.effectiveFrom))
    if (!entry || !versions.length) continue
    const current = versions.find((version) => !version.effectiveTo) || versions.at(-1)
    const levels = []
    for (const level of entry.parsedAward.levels) {
      const perVersion = versions.map((version) => [version, mods.classificationForVersion(version, level.employeeLevel)])
      // Listed only if the verified registry resolves the level in EVERY version.
      if (perVersion.some(([, classification]) => !classification)) continue
      const currentClassification = mods.classificationForVersion(current, level.employeeLevel)
      levels.push({
        key: level.key,
        name: level.employeeLevel,
        minimum_hourly: currentClassification.minimumHourly,
        engine_classification_id: currentClassification.id,
        minimum_hourly_by_version: Object.fromEntries(perVersion.map(([version, classification]) => [version.versionId, classification.minimumHourly])),
      })
    }
    levels.sort((a, b) => a.key.localeCompare(b.key))
    catalog[code] = {
      code,
      name: current.title || entry.parsedAward.awardTitle,
      industry,
      levels,
      levelByKey: Object.fromEntries(levels.map((level) => [level.key, level])),
      levelKeys: levels.map((level) => level.key),
      instrument_versions: versions.map((version) => version.versionId),
      instrument_version_ranges: versions.map((version) => ({
        version: version.versionId,
        effective_from: version.effectiveFrom,
        effective_to: version.effectiveTo || null,
        reference: version.source?.reference || '',
      })),
      preloadedAwards: [entry],
    }
  }
  return catalog
}

// --- mapping: ORL → engine ---------------------------------------------------------

const clean = (value) => String(value ?? '').replace(/[\r\n:]+/g, ' ').trim()

export function engineEmployeeId(workerId) {
  return String(workerId)
}

/**
 * One agreement-profile block in the "Key: value" format that
 * agreementParser.parseAgreementDocument reads. Omitted keys stay omitted, so
 * the engine reports the gap itself instead of us defaulting it.
 */
export function agreementBlock(worker, context, award) {
  const lines = [
    `Employee: ${clean(worker.name) || `Worker ${worker.worker_id}`}`,
    `Employee ID: ${engineEmployeeId(worker.worker_id)}`,
  ]
  if (worker.award_code) lines.push(`Award Code: ${worker.award_code}`)
  if (worker.classification_level) lines.push(`Employee Level: ${award.levelByKey[worker.classification_level].name}`)
  if (worker.employment_type) lines.push(`Employment Type: ${EMPLOYMENT_LABELS[worker.employment_type]}`)
  if (worker.over_award_rate != null) lines.push(`Base Pay Rate: ${worker.over_award_rate}`)
  if (worker.ordinary_hours_per_week != null) lines.push(`Ordinary Hours Per Week: ${worker.ordinary_hours_per_week}`)
  if (worker.agreed_ordinary_hours_per_shift != null) lines.push(`Ordinary Hours Per Shift: ${worker.agreed_ordinary_hours_per_shift}`)
  if (worker.roster_cycle_weeks != null) lines.push(`Roster Cycle Weeks: ${worker.roster_cycle_weeks}`)
  if (worker.roster_cycle_start) lines.push(`Roster Cycle Start: ${worker.roster_cycle_start}`)
  if (context.legal_employer) lines.push(`Legal Employer: ${context.legal_employer}`)
  if (context.work_type) lines.push(`Work Type: ${context.work_type}`)
  // The engine's employee-master check (securityCalculator.js:326) wants the
  // source payroll system's state-scoped instrument code, e.g. MA000016-NSW.
  // ORL's Worker IS the employee master, so its award_code + the employer's
  // jurisdiction is that assignment. Outside NSW this yields e.g. MA000016-VIC
  // and the engine keeps flagging the gap, which is the honest outcome.
  if (worker.award_code && context.jurisdiction) lines.push(`Source Award Code: ${worker.award_code}-${context.jurisdiction}`)
  return lines.join('\n')
}

export function agreementText(workers, context, award) {
  return [...workers]
    .sort((a, b) => a.worker_id - b.worker_id)
    .map((worker) => agreementBlock(worker, context, award))
    .join('\n\n')
}

function dayName(dateKey) {
  const [y, m, d] = dateKey.split('-').map(Number)
  return DAY_NAMES[new Date(Date.UTC(y, m - 1, d)).getUTCDay()]
}

function spanMinutes(start, end) {
  const toMin = (value) => Number(value.slice(0, 2)) * 60 + Number(value.slice(3, 5))
  const s = toMin(start)
  let e = toMin(end)
  if (e <= s) e += 1440
  return e - s
}

/** Shift → the timesheetParser.js shift shape (date, dateKey, weekBucket, day, …). */
export function toEngineShift(mods, shift, worker) {
  const breakMinutes = shift.break_minutes || 0
  const engineShift = {
    employeeId: engineEmployeeId(worker.worker_id),
    employeeName: clean(worker.name) || `Worker ${worker.worker_id}`,
    jobRole: '',
    employmentType: EMPLOYMENT_LABELS[worker.employment_type] || '',
    date: shift.date,
    dateKey: shift.date,
    weekBucket: mods.weekBucketFor(shift.date),
    day: dayName(shift.date),
    start: shift.start_time,
    finish: shift.end_time,
    breakMinutes,
    breakStart: shift.break_start || '',
    // Paid (break-net) hours: the engine's segmenter treats `hours` as paid time.
    hours: mods.round2((spanMinutes(shift.start_time, shift.end_time) - breakMinutes) / 60),
    location: shift.site_code || '',
    // Free-text notes drive regex-triggered rules in the engine (e.g. "PH"),
    // so ORL never sends any; every rule input is an explicit flag instead.
    notes: '',
    sourceShiftId: String(shift.shift_id),
  }
  for (const [name, value] of Object.entries(shift.flags || {})) {
    if (value == null) continue
    engineShift[FLAG_FIELDS[name]] = value
  }
  return engineShift
}

const FLAG_FIELDS = Object.fromEntries(Object.entries(SHIFT_FLAGS).map(([name, [field]]) => [name, field]))

function identityFor(worker) {
  return {
    employeeId: engineEmployeeId(worker.worker_id),
    employeeName: clean(worker.name) || `Worker ${worker.worker_id}`,
    jobRole: '',
    employmentType: EMPLOYMENT_LABELS[worker.employment_type] || '',
  }
}

/** Chronological order: the engine allocates weekly overtime to the LAST
 *  segments in input order, so input order must be time order. */
const chronological = (a, b) => a.dateKey.localeCompare(b.dateKey) || a.start.localeCompare(b.start)
  || Number(a.sourceShiftId) - Number(b.sourceShiftId)

// --- mapping: engine → contract --------------------------------------------------

const mapItem = (item) => ({
  type: item.type,
  category: item.category,
  amount: item.amount,
  clause: item.clause || '',
  detail: item.detail || '',
})

const uniqueSorted = (values) => [...new Set(values.filter(Boolean))].sort()

// --- engine facade ---------------------------------------------------------------

export async function createEngine({ env = process.env } = {}) {
  const aiPath = resolveAwardIntelligencePath(env)
  const pinnedCommit = readPinnedCommit()
  const engineCommit = readGitHead(aiPath)
  const commitMatches = Boolean(engineCommit) && engineCommit === pinnedCommit
  const allowUnpinned = env.ALLOW_UNPINNED === '1'
  const mods = await importEngineModules(aiPath)
  const awards = buildAwardCatalog(mods, aiPath)

  async function buildCache(workers, context, award) {
    return mods.buildParsedCacheFromTexts(
      { agreementText: agreementText(workers, context, award) },
      {
        cacheFingerprint: 'orl-engine-service',
        preloadedAwards: award.preloadedAwards,
        industry: award.industry,
        sourceNames: { agreement: 'orl-workers' },
      },
    )
  }

  /** Engine-calendar check for the known MA000016 gap: the segment engine only
   *  applies public-holiday rates when a shift is flagged, never from the
   *  jurisdiction calendar. We flag (never adjust) segments it priced as
   *  ordinary days although the engine's own calendar says holiday. */
  function publicHolidayGapWarnings(row, jurisdiction, shiftIds = null) {
    if (row?.calculationStatus !== 'resolved') return []
    const calendars = mods.createCalendarSet(jurisdiction)
    const warnings = []
    for (const segment of row.segmentEvidence || []) {
      if (shiftIds && !shiftIds.has(segment.sourceShiftId)) continue
      if (segment.publicHoliday) continue
      const holiday = mods.resolvePublicHoliday({ dateKey: segment.dateKey }, calendars)
      if (holiday.isHoliday) {
        warnings.push(`${segment.dateKey} is ${holiday.name} in the engine's public holiday calendar, but the MA000016 segment engine priced it without public-holiday rates because the shift was not flagged public_holiday. The cost may be understated; set flags.public_holiday to apply them.`)
      }
    }
    return uniqueSorted(warnings)
  }

  const health = () => ({
    status: commitMatches || allowUnpinned ? 'ok' : 'degraded',
    pinned_commit: pinnedCommit,
    engine_commit: engineCommit,
    commit_matches: commitMatches,
    unpinned: !commitMatches && allowUnpinned,
  })

  function listAwards() {
    return {
      engine_commit: engineCommit,
      awards: Object.values(awards)
        .sort((a, b) => a.code.localeCompare(b.code))
        .map((award) => ({
          code: award.code,
          name: award.name,
          levels: award.levels,
          instrument_versions: award.instrument_versions,
          instrument_version_ranges: award.instrument_version_ranges,
        })),
    }
  }

  async function costMatrix(body) {
    const award = awards[body.award_code]
    const context = body.context
    const cache = await buildCache(body.workers, context, award)
    const workersById = new Map(body.workers.map((worker) => [worker.worker_id, worker]))
    const shiftsById = new Map(body.shifts.map((shift) => [shift.shift_id, shift]))

    const pairs = Array.isArray(body.pairs)
      ? body.pairs.map(([workerId, shiftId]) => ({ workerId, shiftId }))
      : [...workersById.keys()].flatMap((workerId) => [...shiftsById.keys()].map((shiftId) => ({ workerId, shiftId })))
    pairs.sort((a, b) => a.workerId - b.workerId || a.shiftId - b.shiftId)

    const baselineByWorker = new Map()
    const baselineFor = (worker) => {
      if (!baselineByWorker.has(worker.worker_id)) {
        const raw = body.baseline?.[String(worker.worker_id)] || []
        baselineByWorker.set(worker.worker_id, raw.map((shift) => toEngineShift(mods, shift, worker)).sort(chronological))
      }
      return baselineByWorker.get(worker.worker_id)
    }
    // Memoised baseline rows, keyed by worker + the shift excluded from it.
    const baselineRows = new Map()
    const baselineRow = (worker, identity, shifts, excludedShiftId) => {
      const key = `${worker.worker_id}|${excludedShiftId ?? ''}`
      if (!baselineRows.has(key)) baselineRows.set(key, shifts.length ? mods.calcRow(cache, identity, shifts) : null)
      return baselineRows.get(key)
    }

    const rows = pairs.map(({ workerId, shiftId }) => {
      const worker = workersById.get(workerId)
      const identity = identityFor(worker)
      const cell = toEngineShift(mods, shiftsById.get(shiftId), worker)
      const fullBaseline = baselineFor(worker)
      // A shift already in the baseline is priced against the REST of the week
      // (never added twice) — what a fixed-point re-solve needs.
      const inBaseline = fullBaseline.some((shift) => shift.sourceShiftId === cell.sourceShiftId)
      const base = inBaseline ? fullBaseline.filter((shift) => shift.sourceShiftId !== cell.sourceShiftId) : fullBaseline
      const union = [...base, cell].sort(chronological)
      const out = {
        worker_id: workerId,
        shift_id: shiftId,
        day: cell.dateKey,
        status: 'unresolved',
        eligible: false,
        pay_cost: null,
        min_hours: null,
        max_hours: null,
        reasons: [],
        driving_items: [],
        warnings: [],
        release_blocking_gaps: [],
        instrument_versions: [],
      }

      const withoutRow = baselineRow(worker, identity, base, inBaseline ? shiftId : null)
      if (withoutRow && withoutRow.calculationStatus !== 'resolved') {
        out.reasons = uniqueSorted((withoutRow.validationErrors || []).map((issue) => `baseline: ${issue}`))
        return out
      }
      const withRow = mods.calcRow(cache, identity, union)
      if (withRow.calculationStatus !== 'resolved') {
        out.reasons = uniqueSorted(withRow.validationErrors || [])
        return out
      }

      let pricing
      if (union.at(-1) === cell) {
        // The engine's marginalCost appends the cover shift LAST; when that is
        // also chronological order its answer is the exact one — use it as is.
        const marginal = mods.marginalCost(cache, identity, base, [cell])
        pricing = { cost: marginal.cost, items: marginal.drivingItems, method: 'marginalCost' }
      } else {
        // Cell falls before a baseline shift: marginalCost's appended order would
        // let the engine attribute weekly overtime to the wrong shift. Same
        // formula (calc(existing ∪ new) − calc(existing)), chronological input.
        pricing = {
          cost: mods.round2(withRow.totalCalculatedPay - (withoutRow?.totalCalculatedPay || 0)),
          items: itemDelta(mods, withRow, withoutRow),
          method: 'chronological-difference',
        }
      }

      const before = new Set(withoutRow?.complianceNotes || [])
      const newNotes = (withRow.complianceNotes || []).filter((note) => !before.has(note))
      const hardLimits = newNotes.filter((note) => HARD_LIMIT_WARNING.test(note))
      out.status = 'resolved'
      out.eligible = hardLimits.length === 0
      out.pay_cost = pricing.cost
      out.reasons = uniqueSorted(hardLimits)
      out.driving_items = pricing.items.map((item) => ({ type: item.type, amount: item.amount, clause: item.clause || '' }))
      out.warnings = uniqueSorted([
        ...newNotes.filter((note) => !HARD_LIMIT_WARNING.test(note)),
        ...publicHolidayGapWarnings(withRow, context.jurisdiction, new Set([cell.sourceShiftId])),
      ])
      out.release_blocking_gaps = uniqueSorted(withRow.releaseBlockingGaps || [])
      out.instrument_versions = uniqueSorted(withRow.instrumentVersions || [])
      out.pricing_method = pricing.method
      return out
    })

    const allShifts = [
      ...body.shifts.map((shift) => ({ dateKey: shift.date })),
      ...Object.values(body.baseline || {}).flat().map((shift) => ({ dateKey: shift.date })),
    ]
    return {
      engine_commit: engineCommit,
      instrument_versions: uniqueSorted(rows.flatMap((row) => row.instrument_versions)),
      rows,
      rate_validity: mods.assessRates([award.code], cache.rateSourcesByCode, mods.payPeriodFromTimesheet({ shifts: allShifts })),
    }
  }

  function buildTimesheet(body) {
    const workersById = new Map(body.workers.map((worker) => [worker.worker_id, worker]))
    const employees = [...body.assignments]
      .filter((assignment) => assignment.shifts.length > 0)
      .sort((a, b) => a.worker_id - b.worker_id)
      .map((assignment) => {
        const worker = workersById.get(assignment.worker_id)
        const shifts = assignment.shifts.map((shift) => toEngineShift(mods, shift, worker)).sort(chronological)
        return {
          ...identityFor(worker),
          shifts,
          totalHours: mods.round2(shifts.reduce((sum, shift) => sum + shift.hours, 0)),
        }
      })
    const shifts = employees.flatMap((employee) => employee.shifts)
    return {
      meta: { payPeriod: `${body.period_start} to ${body.period_end}`, business: body.context.legal_employer || '', generated: '' },
      employees,
      shifts,
      totalHours: mods.round2(shifts.reduce((sum, shift) => sum + shift.hours, 0)),
    }
  }

  async function priceRoster(body) {
    const award = awards[body.award_code]
    const cache = await buildCache(body.workers, body.context, award)
    const timesheet = buildTimesheet(body)
    const result = mods.calculateTimesheetResults(cache, timesheet, { jurisdiction: body.context.jurisdiction })

    const workers = result.rows.map((row, index) => {
      const workerId = Number(timesheet.employees[index].employeeId)
      const resolved = row.calculationStatus === 'resolved'
      return {
        worker_id: workerId,
        status: resolved ? 'resolved' : 'unresolved',
        total_pay: resolved ? row.totalCalculatedPay : null,
        ordinary_pay: resolved ? row.ordinaryPay : null,
        total_hours: row.totalHours,
        items: resolved ? (row.extrasAllowances?.items || []).map(mapItem) : [],
        issues: uniqueSorted(row.validationErrors || []),
        warnings: uniqueSorted([
          ...(row.complianceNotes || []),
          ...publicHolidayGapWarnings(row, body.context.jurisdiction),
        ]),
        release_blocking_gaps: uniqueSorted(row.releaseBlockingGaps || []),
        instrument_versions: uniqueSorted(row.instrumentVersions || []),
      }
    }).sort((a, b) => a.worker_id - b.worker_id)

    const unresolved = workers.filter((worker) => worker.status !== 'resolved').map((worker) => worker.worker_id)
    return {
      engine_commit: engineCommit,
      instrument_versions: uniqueSorted(workers.flatMap((worker) => worker.instrument_versions)),
      total_cost: unresolved.length ? null : result.stats.totalCalculatedPay,
      workers,
      unresolved_worker_ids: unresolved,
      warnings: [...result.warnings],
      public_holidays_applied: result.publicHolidaysApplied,
      rate_validity: result.rateValidity,
    }
  }

  return {
    aiPath,
    pinnedCommit,
    engineCommit,
    commitMatches,
    allowUnpinned,
    awards,
    jurisdictions: [...mods.JURISDICTIONS],
    modules: mods,
    health,
    listAwards,
    costMatrix,
    priceRoster,
    // exposed for golden tests
    buildCache,
    buildTimesheet,
  }
}

/** Per-type delta of two engine rows — the same evidence marginalCost's
 *  drivingItems carries (coverage.js itemDelta), for the chronological path. */
function itemDelta(mods, withRow, withoutRow) {
  const totals = new Map()
  const add = (type, amount, clause = '') => {
    if (!totals.has(type)) totals.set(type, { type, amount: 0, clause })
    totals.get(type).amount += amount
  }
  add('Ordinary time', (withRow.ordinaryPay || 0) - (withoutRow?.ordinaryPay || 0))
  for (const item of withRow.extrasAllowances?.items || []) add(item.type, Number(item.amount) || 0, item.clause)
  for (const item of withoutRow?.extrasAllowances?.items || []) add(item.type, -(Number(item.amount) || 0), item.clause)
  return [...totals.values()]
    .map((entry) => ({ ...entry, amount: mods.round2(entry.amount) }))
    .filter((entry) => Math.abs(entry.amount) >= 0.01)
    .sort((left, right) => right.amount - left.amount)
}
