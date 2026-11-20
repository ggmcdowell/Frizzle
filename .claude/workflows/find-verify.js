// find-verify — finders report candidate findings; independent verifiers try to refute each one;
// only the confirmed findings come back. A finding is verified as soon as its finder returns
// (pipeline, no barrier), and a finding another finder already reported at the same file and
// line is skipped before it costs a verifier.
// The leg contract lives in .claude/agents/workflow-leg.md, spawned by agentType. The four saved
// templates (one-leg, fan-out, find-verify, chain) share the args, the leg helper and the return
// shape; a script cannot import, so each repeats them.

export const meta = {
  name: 'find-verify',
  description: 'Finders report findings, refute-framed verifiers vote on each; returns confirmed findings only',
  whenToUse: 'A review, audit or bug hunt where a false positive costs the lead a read: each finding is checked by a separate leg before it comes back.',
  phases: [
    { title: 'Find', detail: 'one leg per finder brief' },
    { title: 'Verify', detail: 'votes refute-framed legs per finding' },
  ],
}

// args: { goal, scope, finders: [{ label, brief }], votes?: 1-3, model?: 'sonnet' | 'opus' | 'haiku',
//         verifyModel?: 'sonnet' | 'opus' | 'haiku', effort?: 'low' to 'max' (the finders),
//         verifyEffort?: 'low' to 'max' (the verifiers), maxItems? }
//   Defaults: each leg's route line below (harness.toml [routing.legs]); a verifier takes the model and
//   effort args before its own route.
//   A finding stands when a strict majority of its votes confirm it.
const a = typeof args === 'string' ? JSON.parse(args) : (args ?? {})
const FINDERS = Array.isArray(a.finders) ? a.finders : []
if (!a.goal || !a.scope || !FINDERS.length || FINDERS.some((f) => !f || !f.label || !f.brief)) {
  throw new Error('find-verify needs args: goal, scope, finders: [{label, brief}, ...]')
}
const ROUTE = { model: 'haiku', effort: 'high' }  // route:find-verify-finder: harness.toml [routing.legs], rendered by Drawer/settings-sync.py
const MODELS = /^(opus|sonnet|haiku)$/  // [gates] allowed_models in harness.toml
const LEVELS = /^(low|medium|high|xhigh|max)$/
const pick = (rx, v) => typeof v === 'string' && rx.test(v)  // a non-string arg never passes a pick
const VERIFY_ROUTE = { model: 'sonnet', effort: 'medium' }  // route:find-verify-verifier
// A verifier takes verifyModel and verifyEffort, else the finders' model and effort args, else its route.
const MODEL = pick(MODELS, a.model) ? a.model : ROUTE.model
const VERIFY_MODEL = pick(MODELS, a.verifyModel) ? a.verifyModel : pick(MODELS, a.model) ? MODEL : VERIFY_ROUTE.model
const EFFORT = pick(LEVELS, a.effort) ? a.effort : ROUTE.effort
if (a.effort != null && EFFORT !== a.effort) log(`effort ${JSON.stringify(a.effort)} is not low, medium, high, xhigh or max; the route effort stands`)
const VERIFY_EFFORT = pick(LEVELS, a.verifyEffort) ? a.verifyEffort : pick(LEVELS, a.effort) ? EFFORT : VERIFY_ROUTE.effort
if (a.verifyEffort != null && VERIFY_EFFORT !== a.verifyEffort) log(`verifyEffort ${JSON.stringify(a.verifyEffort)} is not low, medium, high, xhigh or max; the effort arg or the route stands`)
const VOTES = Math.min(3, Math.max(1, Math.floor(Number(a.votes) || 1)))
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

function leg(model, effort, prompt, schema, label, ph) {
  const r = agent(prompt, { model: model, effort: effort, agentType: 'workflow-leg', schema: { ...schema, required: ['model'] }, label: label, phase: ph })
  return r.then((out) => unpack(out, schema)).then((out) => {
    if (out && !String(out.model ?? '').toLowerCase().includes(model)) {
      modelMismatches.push({ label, expected: model, got: String(out.model ?? '') })
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

const FIND_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['model', 'findings'],
  properties: {
    model: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['file', 'line', 'claim', 'evidence'],
        properties: {
          file: { type: 'string' },
          line: { type: 'integer' },
          claim: { type: 'string' },
          evidence: { type: 'string' },
        },
      },
    },
  },
}
const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['model', 'verdict', 'reason'],
  properties: {
    model: { type: 'string' },
    verdict: { enum: ['confirmed', 'refuted'] },
    reason: { type: 'string' },
  },
}

const seen = new Set()
const counts = { finders: FINDERS.length, findersFailed: 0, found: 0, duplicates: 0, confirmed: 0, refuted: 0 }

const perFinder = await pipeline(FINDERS,
  (f) => leg(MODEL, EFFORT, `Goal of this run: ${a.goal}
Scope: ${a.scope}
You are the finder "${f.label}". Read only; change nothing.

Your brief: ${f.brief}

Report each finding once, with the file, the line, a one-sentence claim and the evidence you read (a short quote or a command result). Report nothing you did not read yourself. An empty list is a valid answer.
Return: model (your resolved model), findings.`, FIND_SCHEMA, `find:${f.label}`, 'Find'),
  (found, f) => {
    if (!found) {
      counts.findersFailed += 1
      log(`finder ${f.label} returned nothing`)
      return []
    }
    const fresh = (found.findings ?? []).filter((x) => {
      const key = `${x.file}:${x.line}`
      if (seen.has(key)) {
        counts.duplicates += 1
        log(`duplicate skipped: ${key} from ${f.label}`)
        return false
      }
      seen.add(key)
      return true
    })
    counts.found += fresh.length
    return parallel(fresh.map((x, n) => async () => {
      const ballots = await parallel(Array.from({ length: VOTES }, (_, v) => () =>
        leg(VERIFY_MODEL, VERIFY_EFFORT, `Goal of this run: ${a.goal}
You are verifier ${v + 1} of ${VOTES}. Read only; change nothing. Try to REFUTE this finding.

Finding: ${x.file}:${x.line} — ${x.claim}
The finder's evidence: ${x.evidence}

Read the file and its callers yourself. Answer confirmed only when your own reading proves the claim; when it does not hold, or you cannot prove it, answer refuted.
Return: model (your resolved model), verdict, reason (one sentence).`, VERDICT_SCHEMA, `verify:${f.label}#${n + 1}.${v + 1}`, 'Verify')))
      const yes = ballots.filter((b) => b && b.verdict === 'confirmed').length
      const ok = yes * 2 > VOTES
      counts[ok ? 'confirmed' : 'refuted'] += 1
      return { ...x, finder: f.label, votes: `${yes}/${VOTES}`, confirmed: ok }
    }))
  })

const confirmed = perFinder.flat().filter((x) => x && x.confirmed).map(({ confirmed: _, ...x }) => x)
const items = cap(confirmed, 'confirmed finding')
return {
  summary: `${counts.confirmed} of ${counts.found} findings confirmed by ${VOTES} vote(s) each; ${counts.findersFailed}/${FINDERS.length} finders failed`,
  counts,
  items,
  dropped,
  modelMismatches,
}
