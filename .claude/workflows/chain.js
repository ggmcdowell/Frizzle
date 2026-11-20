// chain — dependent steps in order; after each step an independent leg checks its done state,
// and the chain stops at the first failed check.
// The leg contract lives in .claude/agents/workflow-leg.md, spawned by agentType. The four saved
// templates (one-leg, fan-out, find-verify, chain) share the args, the leg helper and the return
// shape; a script cannot import, so each repeats them.

export const meta = {
  name: 'chain',
  description: 'Dependent workflow-leg steps in order, each checked by an independent leg; stops at the first failed check',
  whenToUse: 'Sequential work where each step needs the one before it to hold: a build then a test, a change then its proof.',
  phases: [
    { title: 'Step', detail: 'one leg per step, in order' },
    { title: 'Check', detail: 'one independent leg per step' },
  ],
}

// args: { goal, steps: [{ label, brief, check }], model?: 'sonnet' | 'opus' | 'haiku', effort?: 'low' to 'max', maxItems? }
//   check — the condition a separate leg proves after the step, in one sentence.
const a = typeof args === 'string' ? JSON.parse(args) : (args ?? {})
const STEPS = Array.isArray(a.steps) ? a.steps : []
if (!a.goal || !STEPS.length || STEPS.some((s) => !s || !s.label || !s.brief || !s.check)) {
  throw new Error('chain needs args: goal, steps: [{label, brief, check}, ...]')
}
const ROUTE = { model: 'sonnet', effort: 'medium' }  // route:chain: harness.toml [routing.legs], rendered by Drawer/settings-sync.py
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

const STEP_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['model', 'summary'],
  properties: { model: { type: 'string' }, summary: { type: 'string' } },
}
const CHECK_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['model', 'pass', 'reason'],
  properties: { model: { type: 'string' }, pass: { type: 'boolean' }, reason: { type: 'string' } },
}

const rows = []
let prior = 'none; this is the first step'
let stopped = false
for (const [i, s] of STEPS.entries()) {
  if (stopped) {
    rows.push({ label: s.label, status: 'skipped', summary: '', reason: 'an earlier check failed' })
    continue
  }
  phase('Step')
  const done = await leg(`Goal of this run: ${a.goal}
You run step ${i + 1} of ${STEPS.length}, "${s.label}". Earlier steps: ${prior}

Your brief: ${s.brief}

Return: model (your resolved model), summary (at most 3 sentences: what you did and where it landed).`,
    STEP_SCHEMA, `step:${s.label}`, 'Step')
  phase('Check')
  const check = done && await leg(`Goal of this run: ${a.goal}
You check step "${s.label}" independently. Read only; change nothing. Do not trust the step's own report.
The step reports: ${done.summary}

Prove or disprove this condition by your own reading or command: ${s.check}
Answer pass true only when you proved it.
Return: model (your resolved model), pass, reason (one sentence).`, CHECK_SCHEMA, `check:${s.label}`, 'Check')
  const ok = Boolean(check && check.pass === true)
  const reason = !done ? 'the step leg returned nothing' : !check ? 'the check leg returned nothing' : check.reason
  rows.push({ label: s.label, status: ok ? 'passed' : 'failed', summary: done ? done.summary : '', reason })
  if (!ok) {
    stopped = true
    log(`chain stopped at ${s.label}: ${reason}`)
  }
  prior = rows.map((r) => `${r.label}: ${r.summary}`).join(' | ')
}

const passed = rows.filter((r) => r.status === 'passed').length
const failed = rows.find((r) => r.status === 'failed')
const items = cap(rows, 'step row')
return {
  summary: failed ? `stopped at ${failed.label}: ${passed}/${STEPS.length} steps passed` : `all ${STEPS.length} steps passed`,
  counts: { steps: STEPS.length, passed, failed: failed ? 1 : 0, skipped: rows.filter((r) => r.status === 'skipped').length },
  items,
  dropped,
  modelMismatches,
}
