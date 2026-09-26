import path from 'node:path'
import { createEngine } from '../src/engine.js'
import { createServer } from '../src/server.js'

let shared
/** One engine + server per test process (engine import is the slow part). */
export async function harness() {
  if (!shared) {
    shared = (async () => {
      const engine = await createEngine()
      const server = createServer(engine)
      await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
      server.unref()
      const base = `http://127.0.0.1:${server.address().port}`
      const call = async (method, path, body, { raw } = {}) => {
        const res = await fetch(base + path, {
          method,
          headers: body === undefined ? {} : { 'content-type': 'application/json' },
          body: body === undefined ? undefined : (raw ? body : JSON.stringify(body)),
        })
        const text = await res.text()
        return { status: res.status, text, json: JSON.parse(text) }
      }
      return { engine, server, call, mods: engine.modules }
    })()
  }
  return shared
}

export const CONTEXT = Object.freeze({ jurisdiction: 'NSW', legal_employer: 'ORL Demo Security Pty Ltd', work_type: 'static guarding' })
export const LEVEL = (n) => `MA000016::securityofficerlevel${n}`

// 3 workers: full-time L1, part-time L2 with agreed hours, casual L3 over-award.
export const WORKERS = Object.freeze([
  { worker_id: 1, name: 'Sophie Chen', award_code: 'MA000016', classification_level: LEVEL(1), employment_type: 'full_time', over_award_rate: null, ordinary_hours_per_week: null, agreed_ordinary_hours_per_shift: null, roster_cycle_weeks: 1, roster_cycle_start: null },
  { worker_id: 2, name: 'Tom Anderson', award_code: 'MA000016', classification_level: LEVEL(2), employment_type: 'part_time', over_award_rate: null, ordinary_hours_per_week: 20, agreed_ordinary_hours_per_shift: 7.5, roster_cycle_weeks: 1, roster_cycle_start: null },
  { worker_id: 3, name: 'Priya Sharma', award_code: 'MA000016', classification_level: LEVEL(3), employment_type: 'casual', over_award_rate: 31.5, ordinary_hours_per_week: null, agreed_ordinary_hours_per_shift: null, roster_cycle_weeks: 1, roster_cycle_start: null },
])

// Week of Mon 2026-10-12 (no national public holiday).
export const SHIFTS = Object.freeze([
  { shift_id: 101, date: '2026-10-12', start_time: '07:00', end_time: '15:00', break_minutes: 30, site_code: 'STH-01' }, // weekday day
  { shift_id: 102, date: '2026-10-13', start_time: '22:00', end_time: '06:00', break_minutes: 30, break_start: '02:00', site_code: 'STH-01' }, // night, crosses midnight
  { shift_id: 103, date: '2026-10-17', start_time: '08:00', end_time: '16:00', break_minutes: 30, site_code: 'CBD-01', flags: { first_aid_nominated: true } }, // Saturday
  { shift_id: 104, date: '2026-10-18', start_time: '08:00', end_time: '16:00', break_minutes: 30, site_code: 'CBD-01' }, // Sunday
])

export const clone = (value) => JSON.parse(JSON.stringify(value))

// --- Independent, hand-written engine inputs for golden tests ------------------
// Written directly in the engine's own formats (agreementParser "Key: value"
// text, timesheetParser shift objects) — NOT produced by the wrapper's mapper.

export const DIRECT_AGREEMENT_TEXT = `Employee: Sophie Chen
Employee ID: 1
Award Code: MA000016
Employee Level: Security Officer Level 1
Employment Type: Full-time
Roster Cycle Weeks: 1
Legal Employer: ORL Demo Security Pty Ltd
Work Type: static guarding
Source Award Code: MA000016-NSW

Employee: Tom Anderson
Employee ID: 2
Award Code: MA000016
Employee Level: Security Officer Level 2
Employment Type: Part-time
Ordinary Hours Per Week: 20
Ordinary Hours Per Shift: 7.5
Roster Cycle Weeks: 1
Legal Employer: ORL Demo Security Pty Ltd
Work Type: static guarding
Source Award Code: MA000016-NSW

Employee: Priya Sharma
Employee ID: 3
Award Code: MA000016
Employee Level: Security Officer Level 3
Employment Type: Casual
Base Pay Rate: 31.5
Roster Cycle Weeks: 1
Legal Employer: ORL Demo Security Pty Ltd
Work Type: static guarding
Source Award Code: MA000016-NSW`

export function directShift({ id, employeeId, employmentType, date, day, week, start, finish, breakMinutes = 30, breakStart = '', hours, location, extra = {} }) {
  return {
    employeeId, employeeName: '', jobRole: '', employmentType,
    date, dateKey: date, weekBucket: week, day, start, finish,
    breakMinutes, breakStart, hours, location, notes: '', sourceShiftId: String(id), ...extra,
  }
}

export async function directCache(mods, engine, text = DIRECT_AGREEMENT_TEXT) {
  const preloadedAwards = mods.loadAwardLibraryFs(path.join(engine.aiPath, 'src/domain/awardLibrary'), 'security')
  return mods.buildParsedCacheFromTexts({ agreementText: text }, {
    cacheFingerprint: 'golden', preloadedAwards, industry: 'security',
  })
}
