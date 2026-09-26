// Request validation for the Award Engine service (contract v1).
//
// Validation only rejects MALFORMED requests (HTTP 422). Anything the engine
// itself cannot price (missing employment type, missing roster cycle, …) is NOT
// a validation error: it is passed through so the engine can say so in its own
// words and the response carries status "unresolved".

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/
const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/
const EMPLOYMENT_TYPES = ['full_time', 'part_time', 'casual']
// Values that end up inside generated agreement text must be single-line and
// colon-free, otherwise they could smuggle "Key: value" pairs into the profile.
const UNSAFE_TEXT_RE = /[\r\n:]/

// ORL flag name → engine timesheet field (timesheetParser.js HEADER_ALIASES)
// and its type. Only engine-recognised shift facts are accepted.
export const SHIFT_FLAGS = Object.freeze({
  first_aid_nominated: ['firstAidNominated', 'boolean'],
  firearm_required: ['firearmRequired', 'boolean'],
  broken_shift: ['brokenShift', 'boolean'],
  broken_shift_top_up_hours: ['brokenShiftTopUpHours', 'number'],
  supervised_employees: ['supervisedEmployees', 'number'],
  relieving_officer: ['relievingOfficer', 'boolean'],
  aviation_security: ['aviationSecurity', 'boolean'],
  meal_allowance_eligible: ['mealAllowanceEligible', 'boolean'],
  motor_vehicle_km: ['motorVehicleKm', 'number'],
  motorcycle_km: ['motorcycleKm', 'number'],
  break_recorded: ['breakRecorded', 'boolean'],
  public_holiday: ['publicHoliday', 'boolean'],
  short_rest_directed: ['shortRestDirected', 'boolean'],
  minimum_engagement_applies: ['minimumEngagementApplies', 'boolean'],
  minimum_engagement_top_up_hours: ['minimumEngagementTopUpHours', 'number'],
  higher_duties_level: ['higherDutiesLevel', 'string'],
  higher_duties_hours: ['higherDutiesHours', 'number'],
  higher_duties_start: ['higherDutiesStart', 'time'],
  callback_type: ['callbackType', 'string'],
})

const isObject = (value) => value != null && typeof value === 'object' && !Array.isArray(value)
const isInt = (value) => Number.isInteger(value)
const isNullish = (value) => value === null || value === undefined

export function isRealDate(value) {
  if (typeof value !== 'string' || !DATE_RE.test(value)) return false
  const [y, m, d] = value.split('-').map(Number)
  const date = new Date(Date.UTC(y, m - 1, d))
  return date.getUTCFullYear() === y && date.getUTCMonth() === m - 1 && date.getUTCDate() === d
}

function checkTime(errors, value, path, { allow24 = false } = {}) {
  if (typeof value !== 'string' || !(TIME_RE.test(value) || (allow24 && value === '24:00'))) {
    errors.push(`${path} must be a time "HH:MM" (24h).`)
    return false
  }
  return true
}

function checkOptionalNumber(errors, value, path, { positive = false, integer = false, min } = {}) {
  if (isNullish(value)) return
  if (typeof value !== 'number' || !Number.isFinite(value)) return errors.push(`${path} must be a number or null.`)
  if (integer && !isInt(value)) return errors.push(`${path} must be an integer.`)
  if (positive && !(value > 0)) return errors.push(`${path} must be greater than 0.`)
  if (min != null && value < min) errors.push(`${path} must be ≥ ${min}.`)
}

function checkOptionalText(errors, value, path) {
  if (isNullish(value)) return
  if (typeof value !== 'string') return errors.push(`${path} must be a string or null.`)
  if (UNSAFE_TEXT_RE.test(value)) errors.push(`${path} must not contain line breaks or ":".`)
}

export function validateContext(errors, context, { jurisdictions }) {
  if (!isObject(context)) {
    errors.push('context must be an object.')
    return
  }
  if (typeof context.jurisdiction !== 'string' || !jurisdictions.includes(context.jurisdiction)) {
    errors.push(`context.jurisdiction must be one of ${jurisdictions.join(', ')}.`)
  }
  checkOptionalText(errors, context.legal_employer, 'context.legal_employer')
  checkOptionalText(errors, context.work_type, 'context.work_type')
}

export function validateWorker(errors, worker, path, { awardCode, levelKeys }) {
  if (!isObject(worker)) {
    errors.push(`${path} must be an object.`)
    return
  }
  if (!isInt(worker.worker_id)) errors.push(`${path}.worker_id must be an integer.`)
  if (!isNullish(worker.name) && typeof worker.name !== 'string') errors.push(`${path}.name must be a string.`)
  if (!isNullish(worker.award_code) && worker.award_code !== awardCode) {
    errors.push(`${path}.award_code "${worker.award_code}" does not match the request award_code "${awardCode}".`)
  }
  if (!isNullish(worker.classification_level) && !levelKeys.includes(worker.classification_level)) {
    errors.push(`${path}.classification_level "${worker.classification_level}" is not a level key of ${awardCode} (see GET /engine/awards).`)
  }
  if (!isNullish(worker.employment_type) && !EMPLOYMENT_TYPES.includes(worker.employment_type)) {
    errors.push(`${path}.employment_type must be one of ${EMPLOYMENT_TYPES.join(', ')} or null.`)
  }
  checkOptionalNumber(errors, worker.over_award_rate, `${path}.over_award_rate`, { positive: true })
  checkOptionalNumber(errors, worker.ordinary_hours_per_week, `${path}.ordinary_hours_per_week`, { positive: true })
  checkOptionalNumber(errors, worker.agreed_ordinary_hours_per_shift, `${path}.agreed_ordinary_hours_per_shift`, { positive: true })
  if (worker.employment_type === 'part_time') {
    if (!(worker.ordinary_hours_per_week > 0)) errors.push(`${path}: part_time requires ordinary_hours_per_week.`)
    if (!(worker.agreed_ordinary_hours_per_shift > 0)) errors.push(`${path}: part_time requires agreed_ordinary_hours_per_shift.`)
  }
  checkOptionalNumber(errors, worker.roster_cycle_weeks, `${path}.roster_cycle_weeks`, { integer: true, positive: true })
  if (!isNullish(worker.roster_cycle_start) && !isRealDate(worker.roster_cycle_start)) {
    errors.push(`${path}.roster_cycle_start must be a date "YYYY-MM-DD" or null.`)
  }
}

export function validateShift(errors, shift, path) {
  if (!isObject(shift)) {
    errors.push(`${path} must be an object.`)
    return
  }
  if (!isInt(shift.shift_id)) errors.push(`${path}.shift_id must be an integer.`)
  if (!isRealDate(shift.date)) errors.push(`${path}.date must be a real date "YYYY-MM-DD".`)
  checkTime(errors, shift.start_time, `${path}.start_time`)
  checkTime(errors, shift.end_time, `${path}.end_time`, { allow24: true })
  if (!isNullish(shift.break_start)) checkTime(errors, shift.break_start, `${path}.break_start`)
  const breakMinutes = isNullish(shift.break_minutes) ? 0 : shift.break_minutes
  if (!isInt(breakMinutes) || breakMinutes < 0) {
    errors.push(`${path}.break_minutes must be a non-negative integer.`)
  } else if (TIME_RE.test(shift.start_time || '') && (TIME_RE.test(shift.end_time || '') || shift.end_time === '24:00')) {
    if (breakMinutes >= spanMinutes(shift.start_time, shift.end_time)) {
      errors.push(`${path}.break_minutes must be shorter than the shift span.`)
    }
  }
  if (!isNullish(shift.site_code) && typeof shift.site_code !== 'string') errors.push(`${path}.site_code must be a string or null.`)
  if (!isNullish(shift.flags)) {
    if (!isObject(shift.flags)) {
      errors.push(`${path}.flags must be an object.`)
    } else {
      for (const [name, value] of Object.entries(shift.flags)) {
        const spec = SHIFT_FLAGS[name]
        if (!spec) {
          errors.push(`${path}.flags.${name} is not a recognised engine shift flag.`)
          continue
        }
        if (isNullish(value)) continue
        const [, type] = spec
        if (type === 'boolean' && typeof value !== 'boolean') errors.push(`${path}.flags.${name} must be a boolean.`)
        if (type === 'number' && (typeof value !== 'number' || !Number.isFinite(value) || value < 0)) errors.push(`${path}.flags.${name} must be a non-negative number.`)
        if (type === 'string' && typeof value !== 'string') errors.push(`${path}.flags.${name} must be a string.`)
        if (type === 'time') checkTime(errors, value, `${path}.flags.${name}`)
      }
    }
  }
}

/** Minutes from start to end; end ≤ start means the shift runs overnight. */
export function spanMinutes(start, end) {
  const toMin = (value) => Number(value.slice(0, 2)) * 60 + Number(value.slice(3, 5))
  const s = toMin(start)
  let e = toMin(end)
  if (e <= s) e += 1440
  return e - s
}

function validateAward(errors, body, awards) {
  if (typeof body.award_code !== 'string' || !awards[body.award_code]) {
    errors.push(`award_code must be one of: ${Object.keys(awards).sort().join(', ')}.`)
    return null
  }
  return awards[body.award_code]
}

function validateWorkers(errors, workers, award) {
  if (!Array.isArray(workers) || !workers.length) {
    errors.push('workers must be a non-empty array.')
    return new Set()
  }
  const ids = new Set()
  workers.forEach((worker, index) => {
    validateWorker(errors, worker, `workers[${index}]`, { awardCode: award.code, levelKeys: award.levelKeys })
    if (isInt(worker?.worker_id)) {
      if (ids.has(worker.worker_id)) errors.push(`workers[${index}].worker_id ${worker.worker_id} is duplicated.`)
      ids.add(worker.worker_id)
    }
  })
  return ids
}

/**
 * @param {object} body
 * @param {{ awards: Record<string, {code, levelKeys}>, jurisdictions: string[] }} meta
 * @returns {string[]} errors (empty = valid)
 */
export function validateCostMatrixRequest(body, meta) {
  const errors = []
  if (!isObject(body)) return ['Request body must be a JSON object.']
  const award = validateAward(errors, body, meta.awards)
  validateContext(errors, body.context, meta)
  const workerIds = award ? validateWorkers(errors, body.workers, award) : new Set()

  const shiftIds = new Set()
  if (!Array.isArray(body.shifts) || !body.shifts.length) {
    errors.push('shifts must be a non-empty array.')
  } else {
    body.shifts.forEach((shift, index) => {
      validateShift(errors, shift, `shifts[${index}]`)
      if (isInt(shift?.shift_id)) {
        if (shiftIds.has(shift.shift_id)) errors.push(`shifts[${index}].shift_id ${shift.shift_id} is duplicated.`)
        shiftIds.add(shift.shift_id)
      }
    })
  }

  if (!isNullish(body.baseline)) {
    if (!isObject(body.baseline)) {
      errors.push('baseline must be an object keyed by worker_id.')
    } else {
      for (const [key, shifts] of Object.entries(body.baseline)) {
        if (!/^-?\d+$/.test(key) || !workerIds.has(Number(key))) {
          errors.push(`baseline["${key}"] does not name a worker in workers.`)
        }
        if (!Array.isArray(shifts)) {
          errors.push(`baseline["${key}"] must be an array of shifts.`)
          continue
        }
        const seen = new Set()
        shifts.forEach((shift, index) => {
          validateShift(errors, shift, `baseline["${key}"][${index}]`)
          if (isInt(shift?.shift_id)) {
            if (seen.has(shift.shift_id)) errors.push(`baseline["${key}"][${index}].shift_id ${shift.shift_id} is duplicated.`)
            seen.add(shift.shift_id)
          }
        })
      }
    }
  }

  if (!isNullish(body.pairs)) {
    if (!Array.isArray(body.pairs)) {
      errors.push('pairs must be an array of [worker_id, shift_id].')
    } else {
      const seen = new Set()
      body.pairs.forEach((pair, index) => {
        if (!Array.isArray(pair) || pair.length !== 2 || !isInt(pair[0]) || !isInt(pair[1])) {
          errors.push(`pairs[${index}] must be [worker_id, shift_id] integers.`)
          return
        }
        if (!workerIds.has(pair[0])) errors.push(`pairs[${index}] worker_id ${pair[0]} is not in workers.`)
        if (!shiftIds.has(pair[1])) errors.push(`pairs[${index}] shift_id ${pair[1]} is not in shifts.`)
        const key = `${pair[0]}:${pair[1]}`
        if (seen.has(key)) errors.push(`pairs[${index}] is duplicated.`)
        seen.add(key)
      })
    }
  }

  const cells = Array.isArray(body.pairs) ? body.pairs.length : workerIds.size * shiftIds.size
  if (cells > meta.maxCells) errors.push(`Request asks for ${cells} cells; the limit is ${meta.maxCells}.`)
  return errors
}

export function validatePriceRosterRequest(body, meta) {
  const errors = []
  if (!isObject(body)) return ['Request body must be a JSON object.']
  const award = validateAward(errors, body, meta.awards)
  validateContext(errors, body.context, meta)
  const periodOk = isRealDate(body.period_start) && isRealDate(body.period_end)
  if (!isRealDate(body.period_start)) errors.push('period_start must be a real date "YYYY-MM-DD".')
  if (!isRealDate(body.period_end)) errors.push('period_end must be a real date "YYYY-MM-DD".')
  if (periodOk && body.period_end < body.period_start) errors.push('period_end must not precede period_start.')
  const workerIds = award ? validateWorkers(errors, body.workers, award) : new Set()

  if (!Array.isArray(body.assignments)) {
    errors.push('assignments must be an array.')
    return errors
  }
  const assigned = new Set()
  const shiftIds = new Set()
  body.assignments.forEach((assignment, index) => {
    const path = `assignments[${index}]`
    if (!isObject(assignment)) return errors.push(`${path} must be an object.`)
    if (!isInt(assignment.worker_id) || !workerIds.has(assignment.worker_id)) {
      errors.push(`${path}.worker_id must name a worker in workers.`)
    } else if (assigned.has(assignment.worker_id)) {
      errors.push(`${path}.worker_id ${assignment.worker_id} appears in more than one assignment.`)
    }
    assigned.add(assignment.worker_id)
    if (!Array.isArray(assignment.shifts)) return errors.push(`${path}.shifts must be an array.`)
    assignment.shifts.forEach((shift, shiftIndex) => {
      const shiftPath = `${path}.shifts[${shiftIndex}]`
      validateShift(errors, shift, shiftPath)
      if (isInt(shift?.shift_id)) {
        if (shiftIds.has(shift.shift_id)) errors.push(`${shiftPath}.shift_id ${shift.shift_id} is assigned more than once.`)
        shiftIds.add(shift.shift_id)
      }
      if (periodOk && isRealDate(shift?.date) && (shift.date < body.period_start || shift.date > body.period_end)) {
        errors.push(`${shiftPath}.date ${shift.date} is outside the period ${body.period_start}..${body.period_end}.`)
      }
    })
  })
  return errors
}
