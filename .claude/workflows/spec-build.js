// spec-build — one round of a spec built with two agents. A draft leg writes the spec from one line of
// intent, or revises it by the human's decisions. Then a second model reads only that spec, cold, and
// lists each place where a builder must guess. The human decides each finding between rounds; the lead
// calls the next round with the decisions, until a round returns done.
// The leg contract lives in .claude/agents/workflow-leg.md, spawned by agentType. Each leg is read-only:
// the workflow writes no file and no memory row; the lead saves the final spec.

export const meta = {
  name: 'spec-build',
  description: 'One round of a spec: a leg drafts or revises it, a second model attacks it cold; returns the spec and the findings the human decides',
  whenToUse: 'A human asks for a spec or a task card from a line of intent. Show the findings to the human, then call it again with the spec and the decisions, until done is true.',
  phases: [
    { title: 'Draft', detail: 'one leg writes or revises the spec' },
    { title: 'Attack', detail: 'a second model reads only the spec' },
  ],
}

// args: { intent, spec?, decisions?: [{ finding, verdict: 'answer' | 'reject', text?, case? }], round?, maxItems? }
//   Round 1: intent only. A later round: the spec the last round returned, and the human's decision on each
//   finding (text: the answer in the human's words; a reject needs none). A spec with no decisions: the
//   attack leg alone reads it, so an existing card can take a cold attack.
const a = typeof args === 'string' ? JSON.parse(args) : (args ?? {})
const VERDICTS = /^(answer|reject)$/
if (!a.intent || typeof a.intent !== 'string') throw new Error('spec-build needs args: intent')
if (a.spec != null && (typeof a.spec !== 'string' || !a.spec.trim())) throw new Error('spec-build: spec must be the text of a spec')
if (a.decisions != null && !Array.isArray(a.decisions)) throw new Error('spec-build: decisions must be a list')
const DECISIONS = a.decisions ?? []
if (a.decisions != null && !a.spec) throw new Error('spec-build: decisions need the spec of the last round')
if (DECISIONS.some((d) => !d || !d.finding || !VERDICTS.test(String(d.verdict)) || (d.verdict === 'answer' && !d.text))) {
  throw new Error('spec-build: each decision needs finding, verdict answer or reject, and text for an answer')
}
const DRAFT = { model: 'sonnet', effort: 'high' }  // route:spec-draft: harness.toml [routing.legs], rendered by Drawer/settings-sync.py
const ATTACK = { model: 'opus', effort: 'high' }  // route:review-other: a second model reads the spec cold
if (DRAFT.model === ATTACK.model) throw new Error('spec-build: the attack leg must run on a second model; fix [routing.legs] spec-draft or review-other')
const REVISE = Boolean(a.spec)
const ROUND = Number(a.round) > 0 ? Math.max(1, Math.floor(Number(a.round))) : (DECISIONS.length ? 2 : 1)
const MAX = Number(a.maxItems) > 0 ? Math.max(1, Math.floor(Number(a.maxItems))) : 20
const CARD_CAP = 1800  // the mem text lint cap of a task card (Drawer/mem/mem.py)
const CASES = ['state', 'life', 'breaks', 'neighbours', 'data', 'proof', 'sees', 'keeps']
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

function leg(route, prompt, schema, label, ph) {
  const r = agent(prompt, { model: route.model, effort: route.effort, agentType: 'workflow-leg', schema: { ...schema, required: ['model'] }, label: label, phase: ph })
  return r.then((out) => unpack(out, schema)).then((out) => {
    if (out && !String(out.model ?? '').toLowerCase().includes(route.model)) {
      modelMismatches.push({ label, expected: route.model, got: String(out.model ?? '') })
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

const SPEC_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['model', 'spec', 'sources'],
  properties: {
    model: { type: 'string' },
    spec: { type: 'string' },
    sources: { type: 'array', items: { type: 'string' } },
  },
}
const ATTACK_SCHEMA = {
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
        required: ['case', 'question', 'size'],
        properties: {
          case: { enum: CASES },
          question: { type: 'string' },
          size: { enum: ['blocks', 'small'] },
        },
      },
    },
  },
}

const FORM = `Write the spec in the card form of .claude/skills/flight/spec.md, one paragraph, at most 1600 characters:
<name>: <what>. Spec: state <...>; life <...>; breaks <...>; neighbours <...>; data <...>; proof <...>; sees <...>; keeps <...>. Why: <gap>. Done when: <end state>.
The spec holds the cases, never the steps. Write n/a for a case that cannot happen.`

phase('Draft')
const drafted = REVISE && !DECISIONS.length
  ? { spec: a.spec, sources: [] }
  : REVISE
  ? await leg(DRAFT, `You revise one spec by the human's decisions. Read only; change no file and no memory row.

The spec:
${a.spec}

The human's decisions on the last findings:
${DECISIONS.map((d, i) => `${i + 1}. Finding${d.case ? ` (case ${d.case})` : ''}: ${d.finding}\n   ${d.verdict === 'answer' ? `Answer: ${d.text}` : 'Rejected: change nothing for it.'}`).join('\n')}

Put each answer in the case it belongs to, in the human's words, as short as they allow. An answer replaces the OPEN mark of its case. A rejected finding changes nothing. Change no other part of the spec, and decide nothing the human did not decide.
${FORM}
Return: model (your resolved model), spec (the whole revised spec), sources (one line per answer: the case it went to).`,
    SPEC_SCHEMA, 'revise', 'Draft')
  : await leg(DRAFT, `You draft one spec from a human's line of intent. Read only; change no file and no memory row.

The intent: ${a.intent}

1. Search memory on the intent: .venv/bin/python -B Drawer/mem/mem.py search "<words>" --json. Read the design cards, decisions and taste lines that bear on it.
2. Read the code, rows or assets the work touches. Name each neighbour by file.
3. Answer each case from a memory row, the code or the intent. A case that no source answers: write OPEN and the question in that case. Never guess; the human decides an open case.
${FORM}
Return: model (your resolved model), spec, sources (one line per memory row id or file path you used, and what it answered).`,
    SPEC_SCHEMA, 'draft', 'Draft')

if (!drafted || !drafted.spec.trim()) {
  return {
    summary: `round ${ROUND}: the ${REVISE ? 'revise' : 'draft'} leg returned no spec`,
    round: ROUND, spec: a.spec ?? '', sources: [], findings: [], counts: { findings: 0, blocks: 0, small: 0 },
    done: false, failed: REVISE ? 'revise' : 'draft', dropped, modelMismatches,
  }
}

if (drafted.spec.length > CARD_CAP) log(`the spec is ${drafted.spec.length} characters; a task card holds ${CARD_CAP}: split it into two tasks`)

phase('Attack')
// The attack leg gets the spec alone: no intent line, no decisions, no draft notes. A build leg starts
// from the same text, so each gap the attacker meets is a gap the builder meets.
const attacked = await leg(ATTACK, `You read one spec cold. A build agent will build from this text alone, with the repo and its rules. Read only; change no file and no memory row.

The spec:
${drafted.spec}

Your one job is to break it: find each place where the builder must guess. You may read the code and the memory rows the spec names to test a claim. Ask no one, and do not rewrite the spec.
For each finding give: case (one of ${CASES.join(', ')}), question (one sentence that a human can answer in one line), size (blocks: a wrong guess builds the wrong thing or loses work; small: any sound guess will do).
An OPEN mark in the spec is a finding of size blocks. Report no style point and no finding you cannot tie to the text. An empty list is a valid answer.
Return: model (your resolved model), findings (blocks first).`,
  ATTACK_SCHEMA, 'attack', 'Attack')

if (!attacked) {
  return {
    summary: `round ${ROUND}: the attack leg returned nothing; the spec is not checked`,
    round: ROUND, spec: drafted.spec, sources: cap(drafted.sources, 'source'), findings: [],
    counts: { findings: 0, blocks: 0, small: 0 }, done: false, failed: 'attack', dropped, modelMismatches,
  }
}

// A finding with no question is dropped with a log line; a size that is not small counts as blocks.
// Blocks go first, so the cap never hides one behind a small finding.
const sound = attacked.findings.filter((f) => f && typeof f === 'object' && typeof f.question === 'string' && f.question.trim())
if (sound.length < attacked.findings.length) log(`dropped ${attacked.findings.length - sound.length} finding(s) with no question`)
const all = sound.map((f) => ({ case: CASES.includes(f.case) ? f.case : 'breaks', question: f.question, size: f.size === 'small' ? 'small' : 'blocks' }))
  .sort((x, y) => (x.size === y.size ? 0 : x.size === 'blocks' ? -1 : 1))
const blocks = all.filter((f) => f.size === 'blocks').length
const findings = cap(all, 'finding')
const open = /\bOPEN\b/.test(drafted.spec)  // a case the draft left for the human
const wrong = modelMismatches.length > 0  // a leg on the wrong model is no second reader
const done = blocks === 0 && !open && !wrong
return {
  summary: done
    ? `round ${ROUND}: no finding blocks the build; ${all.length} small. Save the spec as a task card, and each answer as a decision row.`
    : wrong ? `round ${ROUND}: a leg ran on the wrong model (modelMismatches); run the round again.`
    : `round ${ROUND}: ${blocks} finding(s) block the build, ${all.length - blocks} small${open ? '; the spec holds an OPEN case' : ''}. The human answers or rejects each; then run round ${ROUND + 1} with spec and decisions.`,
  round: ROUND,
  spec: drafted.spec,
  sources: cap(drafted.sources, 'source'),
  findings,
  counts: { findings: all.length, blocks, small: all.length - blocks },
  done,
  dropped,
  modelMismatches,
}
