// one-leg — one bounded worker leg; the lead passes short args and gets a summary back.
// The leg contract (model assertion, foreground jobs, compact return, writes by grant) lives in
// .claude/agents/workflow-leg.md, spawned by agentType; this brief carries only the run facts.
// The four saved templates (one-leg, fan-out, find-verify, chain) share the args, the leg helper
// and the return shape; a script cannot import, so each repeats them.

export const meta = {
  name: 'one-leg',
  description: 'One bounded worker leg on the workflow-leg profile; returns a summary only',
  whenToUse: 'One job the lead would otherwise do itself at a high context cost: a sweep, a count, a bounded read or edit.',
  phases: [{ title: 'Run', detail: 'one leg' }],
}

// args: { goal, brief, model?: 'sonnet' | 'opus' | 'haiku', effort?: 'low' to 'max', maxItems?, schema?, profile?: 'reviewer' }
//   schema — optional JSON Schema for the leg's result; without it the leg returns summary + items.
//   profile — 'reviewer' runs the read-only reviewer profile (.claude/agents/reviewer.md); any other
//   value falls back to workflow-leg with a log line.
const a = typeof args === 'string' ? JSON.parse(args) : (args ?? {})
if (!a.goal || !a.brief) throw new Error('one-leg needs args: goal, brief')
const ROUTE = { model: 'haiku', effort: 'high' }  // route:one-leg: harness.toml [routing.legs], rendered by Drawer/settings-sync.py
const MODELS = /^(opus|sonnet|haiku)$/  // [gates] allowed_models in harness.toml
const LEVELS = /^(low|medium|high|xhigh|max)$/
const pick = (rx, v) => typeof v === 'string' && rx.test(v)  // a non-string arg never passes a pick
const MODEL = pick(MODELS, a.model) ? a.model : ROUTE.model
const EFFORT = pick(LEVELS, a.effort) ? a.effort : ROUTE.effort
if (a.effort != null && EFFORT !== a.effort) log(`effort ${JSON.stringify(a.effort)} is not low, medium, high, xhigh or max; the route effort stands`)
const PROFILE = a.profile === 'reviewer' ? 'reviewer' : 'workflow-leg'
if (a.profile != null && PROFILE !== a.profile) log(`profile ${JSON.stringify(a.profile)} is not reviewer; workflow-leg runs`)
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
  const r = agent(prompt, { model: MODEL, effort: EFFORT, agentType: PROFILE, schema: { ...schema, required: ['model'] }, label: label, phase: ph })
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
  required: ['model', 'summary', a.schema ? 'result' : 'items'],
  properties: {
    model: { type: 'string' },
    summary: { type: 'string' },
    ...(a.schema ? { result: a.schema } : { items: { type: 'array', items: { type: 'string' } } }),
  },
}

phase('Run')
const out = await leg(`Goal of this run: ${a.goal}

Your brief: ${a.brief}

Return: model (your resolved model), summary (at most 3 sentences), ${a.schema ? 'result (the schema your brief names)' : 'items (one short line per finding or result, most important first)'}.`,
  SCHEMA, 'leg:one', 'Run')

if (!out) {
  log('the leg returned nothing')
  return { summary: 'the leg failed and returned no result', counts: { legs: 1, ok: 0 }, items: [], dropped: 0, modelMismatches }
}
const items = cap(a.schema ? [out.result] : out.items, 'item')
return { summary: out.summary, counts: { legs: 1, ok: 1, items: items.length + dropped }, items, dropped, modelMismatches }
