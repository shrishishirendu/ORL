// Award Engine service HTTP layer (node:http, no dependencies).
// Endpoints per docs/AWARD_ENGINE_CONTRACT.md (v1).

import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createEngine } from './engine.js'
import { validateCostMatrixRequest, validatePriceRosterRequest } from './validate.js'

const MAX_BODY_BYTES = 5 * 1024 * 1024
const MAX_CELLS = Number(process.env.ENGINE_MAX_CELLS || 20000)

function send(res, status, body) {
  const json = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(json),
    'cache-control': 'no-store',
  })
  res.end(json)
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        reject(Object.assign(new Error('too-large'), { status: 413 }))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || 'null'))
      } catch {
        reject(Object.assign(new Error('bad-json'), { status: 422 }))
      }
    })
    req.on('error', reject)
  })
}

export function createServer(engine) {
  const meta = {
    awards: engine.awards,
    jurisdictions: engine.jurisdictions,
    maxCells: MAX_CELLS,
  }
  const pinBlocked = () => !engine.commitMatches && !engine.allowUnpinned

  const routes = {
    'GET /engine/health': async () => {
      const body = engine.health()
      return [body.status === 'ok' ? 200 : 503, body]
    },
    'GET /engine/awards': async () => [200, engine.listAwards()],
    'POST /engine/cost-matrix': async (req) => priced(req, validateCostMatrixRequest, engine.costMatrix),
    'POST /engine/price-roster': async (req) => priced(req, validatePriceRosterRequest, engine.priceRoster),
  }

  async function priced(req, validate, handler) {
    if (pinBlocked()) {
      return [503, { error: 'engine_unpinned', details: [`award-intelligence is at ${engine.engineCommit || 'an unknown commit'}, pinned ${engine.pinnedCommit}. Set ALLOW_UNPINNED=1 to override.`] }]
    }
    let body
    try {
      body = await readJson(req)
    } catch (error) {
      if (error.status === 413) return [413, { error: 'too_large', details: [`Body exceeds ${MAX_BODY_BYTES} bytes.`] }]
      return [422, { error: 'validation', details: ['Request body is not valid JSON.'] }]
    }
    const errors = validate(body, meta)
    if (errors.length) return [422, { error: 'validation', details: errors }]
    return [200, await handler(body)]
  }

  return http.createServer(async (req, res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname
    const route = routes[`${req.method} ${pathname}`]
    if (!route) {
      const known = Object.keys(routes).some((key) => key.endsWith(` ${pathname}`))
      return send(res, known ? 405 : 404, { error: known ? 'method_not_allowed' : 'not_found' })
    }
    try {
      const [status, body] = await route(req)
      send(res, status, body)
    } catch (error) {
      console.error(error)
      send(res, 500, { error: 'internal', details: [String(error?.message || error)] })
    }
  })
}

export async function start({ port = Number(process.env.ENGINE_PORT || 8790), host = process.env.ENGINE_HOST || '127.0.0.1' } = {}) {
  const engine = await createEngine()
  const server = createServer(engine)
  await new Promise((resolve) => server.listen(port, host, resolve))
  const health = engine.health()
  console.log(`award engine service on http://${host}:${server.address().port} (engine ${engine.engineCommit || 'unknown'}, pinned ${engine.pinnedCommit}, status ${health.status}${health.unpinned ? ', UNPINNED' : ''})`)
  return { server, engine }
}

const norm = (value) => path.resolve(value).toLowerCase()
if (process.argv[1] && norm(fileURLToPath(import.meta.url)) === norm(process.argv[1])) {
  start().catch((error) => {
    console.error(error)
    process.exit(1)
  })
}
