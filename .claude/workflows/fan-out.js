// fan-out — independent legs in parallel, one compact row back per leg.
// The leg contract lives in .claude/agents/workflow-leg.md, spawned by agentType. The four saved
// templates (one-leg, fan-out, find-verify, chain) share the args, the leg helper and the return
// shape; a script cannot import, so each repeats them.

export const meta = {
  name: 'fan-out',
  description: 'Independent workflow-leg legs in parallel; returns one compact row per leg',
  whenToUse: 'Two or more independent jobs that cut wall clock when run at once; each brief stands alone.',
  phases: [{ title: 'Fan out', detail: 'one leg per brief, in parallel' }],
}

// args: { goal, legs: [{ label, brief }], model?: 'sonnet' | 'opus' | 'haiku', effort?: 'low' to 'max', maxItems? }
const a = typeof args === 'string' ? JSON.parse(args) : (args ?? {})
const LEGS = Array.isArray(a.legs) ? a.legs : []
if (!a.goal || !LEGS.length || LEGS.some((l) => !l || !l.label || !l.brief)) {
  throw new Error('fan-out needs args: goal, legs: [{label, brief}, ...]')
}
const ROUTE = { model: 'haiku', effort: 'high' }  // route:fan-out: harness.toml [routing.legs], rendered by Drawer/settings-sync.py
const MODELS = /^(opus|sonnet|haiku)$/  // [gates] allowed_models in harness.toml
const LEVELS = /^(low|medium|high|xhigh|max)$/
const pick = (rx, v) => typeof v === 'string' && rx.test(v)  // a non-string arg never passes a pick
const MODEL = pick(MODELS, a.model) ? a.model : ROUTE.model
const EFFORT = pick(LEVELS, a.effort) ? a.effort : ROUTE.effort
if (a.effort != null && EFFORT !== a.effort) log(`effort ${JSON.stringify(a.effort)} is not low, medium, high, xhigh or max; the route effort stands`)
const MAX = Number(a.maxItems) > 0 ? Math.floor(Number(a.maxItems)) : 20
const modelMismatches = []
let dropped = 0

// A leg can pack its later fields into the model string as '</parameter><parameter name="x">v'
// tool-call text, so a schema that requires them fails all 5 retries. The agent schema requires
// only model; unpack() restores the packed fields, and lacks() fails a leg still missing a field
// its schema requires, or holding one of the wrong type, with a log line.
function unpack(out, schema) {
  if (!out || typeof out.model !== 'string' || !out.model.includes('</parameter>')) return out
  const parts = out.model.split('</parameter>')
  const fixed = { ...out, model: parts[0].trim() }
  for (const part of parts.slice(1)) {
    const m = /^\s*<parameter name="(\w+)">([\s\S]*)$/.exec(part)
    if (!m || (m[1] in fixed && fixed[m[1]] !== undefined)) continue
    let v = m[2].trim()
    const t = schema.properties[m[1]] && schema.properties[m[1]].type
    if (t !== 'string' && /^[[{]|^(true|false|null|-?\d+(\.\d+)?)$/.test(v)) { try { v = JSON.parse(v) } catch (e) { /* keep text */ } }
    fixed[m[1]] = v
  }
  return fixed
}

function lacks(out, schema) {
  const fits = (t, v) => (t === undefined ? v !== undefined : t === 'array' ? Array.isArray(v)
    : t === 'integer' ? Number.isInteger(v) : t === 'object' ? v !== null && typeof v === 'object' && !Array.isArray(v) : typeof v === t)
  return schema.required.filter((k) => !(k in out) || !fits(schema.properties[k] && schema.properties[k].type, out[k]))
}

function leg(prompt, schema, label, ph) {
  const r = agent(prompt, { model: MODEL, effort: EFFORT, agentType: 'workflow-leg', schema: { ...schema, required: ['model'] }, label: label, phase: ph })
  return r.then((out) => unpack(out, schema)).then((out) => {
    if (out && !String(out.model ?? '').toLowerCase().includes(MODEL)) {
      modelMismatches.push({ label, expected: MODEL, got: String(out.model ?? '') })
    }
    const missing = out ? lacks(out, schema) : []
    if (missing.length) {
      log(`leg ${label} lacks ${missing.join(', ')}: ${JSON.stringify(out).slice(0, 200)}`)
      return null
    }
    return out
  })
}

function cap(list, what) {
  const all = Array.isArray(list) ? list : []
  for (const x of all.slice(MAX)) log(`capped ${what}: ${JSON.stringify(x).slice(0, 200)}`)
  dropped += Math.max(0, all.length - MAX)
  return all.slice(0, MAX)
}

const SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['model', 'summary', 'items'],
  properties: {
    model: { type: 'string' },
    summary: { type: 'string' },
    items: { type: 'array', items: { type: 'string' } },
  },
}

const results = await parallel(LEGS.map((l) => () => leg(`Goal of this run: ${a.goal}
You are one of ${LEGS.length} independent legs; yours is "${l.label}". Do only your brief.

Your brief: ${l.brief}

Return: model (your resolved model), summary (at most 3 sentences), items (one short line per finding or result, most important first).`,
  SCHEMA, `leg:${l.label}`, 'Fan out')))

const rows = LEGS.map((l, i) => {
  const out = results[i]
  if (!out) return { label: l.label, status: 'failed', summary: 'the leg returned nothing', items: [] }
  return { label: l.label, status: 'ok', summary: out.summary, items: cap(out.items, `${l.label} item`) }
})
const ok = rows.filter((r) => r.status === 'ok').length
for (const r of rows) if (r.status === 'failed') log(`leg ${r.label} failed`)
const items = cap(rows, 'leg row')
return {
  summary: `${ok}/${LEGS.length} legs returned`,
  counts: { legs: LEGS.length, ok, failed: LEGS.length - ok },
  items,
  dropped,
  modelMismatches,
}
