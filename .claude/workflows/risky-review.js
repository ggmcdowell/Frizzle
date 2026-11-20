// risky-review — the worker's own check of one committed risky change in the hub or the product repo.
// It is never a push gate by itself: product-push.py and hub-land.py --reviewed <hash> bind the push to the hash a
// Pass returns, and for a task tagged risky they need the Pass this script stores.
// A reviewer leg on a model family the builder did not use reads the committed
// diff cold and returns one checklist row per file and item. Only when it returns blocking findings does one
// refute-framed skeptic, also off the builder's family, read those findings alone, because a false Block costs the
// worker a fix; a Pass runs no skeptic. The script, not a leg,
// decides the verdict from the parsed rows: a missing row, a blocking finding with no trigger, a failed reviewer
// or a leg on the wrong model fails closed. The bar is the two-tier bar of .claude/agents/reviewer.md rule 8.
// Each Pass is stored by one record leg that runs the Drawer/review_pass.py record command this script builds (a
// script has no shell); a Pass the leg did not store is Needs-context. Outside this script: the commit, the args,
// the fix and the push.

export const meta = {
  name: 'risky-review',
  description: "The worker's own review of one committed risky hub or product change; returns Pass, Block or Needs-context with the diff hash",
  whenToUse: 'After you commit a change on a task tagged risky, before the push. Args: the JSON line <python> Drawer/review-diff.py <task worktree> prints, plus builder (the model your session runs on) and round (1, then one more after each fixed Block). Push a Pass with --reviewed <hash>; fix each blocking finding of a Block and run the next round.',
  phases: [
    { title: 'Review', detail: 'one reviewer leg on a family the builder did not use' },
    { title: 'Refute', detail: 'only on blocking findings: one skeptic leg reads them' },
    { title: 'Record', detail: 'one leg stores a Pass for the risky land gate' },
  ],
}

// args: { repo, head, base, hash, diffPath, files, builder, round?, kind?: 'hub' | 'product' }
//   kind defaults to hub when a file is under Drawer/ or .claude/, or is AGENTS.md or harness.toml; else product.
const a = typeof args === 'string' ? JSON.parse(args) : (args ?? {})
const { repo, head, base, hash, diffPath } = a
const files = a.files
const strings = (x) => Array.isArray(x) && x.length > 0 && x.every((s) => typeof s === 'string' && s)
if (typeof repo !== 'string' || !repo || typeof diffPath !== 'string' || !diffPath ||
    !/^[0-9a-f]{40}$/.test(head || '') || !/^[0-9a-f]{64}$/.test(hash || '') || !strings(files)) {
  throw new Error('risky-review needs args: the JSON line <python> Drawer/review-diff.py <task worktree> prints, plus builder and round')
}
if (typeof a.builder !== 'string' || !/^[A-Za-z0-9._-]+$/.test(a.builder)) {
  throw new Error('risky-review needs builder: the model name your session runs on (opus, sonnet, haiku or a full model id)')
}
const round = a.round == null ? 1 : Number(a.round)
if (!Number.isInteger(round) || round < 1) throw new Error(`risky-review: round ${JSON.stringify(a.round)} is not a whole number from 1`)
const HUBFILE = /^(Drawer|\.claude)\/|^(AGENTS\.md|harness\.toml)$/
const kind = a.kind === 'hub' || a.kind === 'product' ? a.kind : files.some((f) => HUBFILE.test(f)) ? 'hub' : 'product'
const h12 = hash.slice(0, 12)

// A family is the model word in a name: claude-opus-5-5 and opus are one family.
const family = (m) => {
  const s = String(m ?? '').toLowerCase()
  const w = /opus|sonnet|haiku|fable/.exec(s)
  return w ? w[0] : s
}
const builder = family(a.builder)
const REVIEW = { model: 'sonnet', effort: 'high' }  // route:review: harness.toml [routing.legs], rendered by Drawer/settings-sync.py
const REFUTE = { model: 'sonnet', effort: 'medium' }  // route:refute
const OTHER = { model: 'opus', effort: 'high' }  // route:review-other: a review or refute leg when the builder ran on its row's family
// Each leg runs off the builder's family; null when no row is left, and the review fails closed.
const away = (route) => (family(route.model) !== builder ? route : family(OTHER.model) !== builder ? OTHER : null)

// The checklist: one row per file and item. Product: bug on every file. Hub: bug, rule, gate and test on every
// file (reviewer.md rule 8).
const ITEMS = {
  bug: 'a critical or high bug in a changed line: a wrong result, a crash, a desync, broken determinism, lost or corrupted saved state, a security hole or a leak. A medium or minor bug with no user impact is report-only.',
  rule: 'a broken rule: a changed line that breaks the root AGENTS.md, a skill, a law file or a rule the change names, or a rule edit that drops or contradicts a binding rule.',
  gate: 'an opened gate: a hook, a gate or a check that now lets through what it refused.',
  test: 'a weakened test: an assertion cut, loosened or skipped, or a test deleted with no replacement for its risk.',
}
const CLASSES = kind === 'hub' ? ['bug', 'rule', 'gate', 'test'] : ['bug']
const items = () => CLASSES

const REVIEW_SCHEMA = {
  type: 'object',
  required: ['model', 'needs_context', 'checklist', 'findings'],
  properties: {
    model: { type: 'string' },
    needs_context: { type: 'string' },
    checklist: {
      type: 'array',
      items: {
        type: 'object',
        required: ['file', 'item', 'result'],
        properties: { file: { type: 'string' }, item: { enum: Object.keys(ITEMS) }, result: { enum: ['clear', 'finding'] } },
      },
    },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['n', 'file', 'line', 'class', 'what', 'trigger', 'blocking'],
        properties: {
          n: { type: 'integer' },
          file: { type: 'string' },
          line: { type: 'integer' },
          class: { enum: [...Object.keys(ITEMS), 'report-only'] },
          what: { type: 'string' },
          trigger: { type: 'string' },
          blocking: { type: 'boolean' },
        },
      },
    },
  },
}
const REFUTE_SCHEMA = {
  type: 'object',
  required: ['model', 'verdicts'],
  properties: {
    model: { type: 'string' },
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        required: ['n', 'refuted', 'reason'],
        properties: { n: { type: 'integer' }, refuted: { type: 'boolean' }, reason: { type: 'string' } },
      },
    },
  },
}

// A leg can pack its later fields into the model string as '</parameter><parameter name="x">v' tool-call
// text; the agent schema requires only model, unpack() restores the packed fields, and lacks() names a field
// still missing or of the wrong type (the saved templates repeat these helpers: a script cannot import).
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

const modelMismatches = []
// One leg on the read-only reviewer profile; null when it failed, lacks a field or ran on another model.
async function leg(route, prompt, schema, label, ph) {
  const out = unpack(await agent(prompt, { model: route.model, effort: route.effort, agentType: 'reviewer',
    schema: { ...schema, required: ['model'] }, label: label, phase: ph }), schema)
  if (!out) return null
  if (!String(out.model ?? '').toLowerCase().includes(route.model)) {
    modelMismatches.push({ label, expected: route.model, got: String(out.model ?? '') })
    log(`leg ${label} ran on ${JSON.stringify(out.model)}, not ${route.model}`)
    return null
  }
  const missing = lacks(out, schema)
  if (missing.length) {
    log(`leg ${label} lacks ${missing.join(', ')}: ${JSON.stringify(out).slice(0, 200)}`)
    return null
  }
  return out
}

const reviewer = away(REVIEW)
const skeptic = away(REFUTE)
const reportOnly = []
const done = (verdict, reason, extra) => {
  log(`round ${round}: ${verdict} — ${reason}`)
  return { verdict, reason, round, hash, head, kind, builder, reviewer: reviewer && reviewer.model,
           blocking: [], refuted: [], reportOnly, modelMismatches, ...extra }
}
// The risky land gate reads the Pass from <state_root>/review/pass/<hash>.json. One workflow-leg (no
// model pick, like card-run's complete leg) runs the record command built here; a receipt that is not recorded for
// this hash turns the Pass into Needs-context.
const RECORD_SCHEMA = { type: 'object', additionalProperties: false, required: ['model', 'receipt'],
  properties: { model: { type: 'string' }, receipt: { type: 'string' } } }
const quote = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`
async function pass(reason, by, extra) {
  const who = /^[A-Za-z0-9._-]+$/.test(String(by ?? '')) ? by : reviewer.model
  const cmd = `<python> Drawer/review_pass.py record --hash ${hash} --head ${head} --diff ${quote(diffPath)} --round ${round} --reviewer ${who}`
  const out = await agent(`Run exactly this one command from the hub main checkout (your working folder at start), once, as one plain-string shell command, and nothing else. <python> is .venv/bin/python -B. ${cmd}
Return receipt: its JSON stdout line, word for word.`, { agentType: 'workflow-leg', schema: RECORD_SCHEMA, label: `record:${h12}`, phase: 'Record' })
  const rc = (() => { try { return JSON.parse(String(out?.receipt ?? '')) } catch (e) { return null } })()
  if (!rc || rc.state !== 'recorded' || rc.hash !== hash) {
    return done('Needs-context', `the Pass was not stored: ${out ? String(out.receipt ?? '').slice(0, 300) || 'no receipt' : 'the record leg returned nothing'}`, extra)
  }
  return done('Pass', reason, { stored: true, ...extra })
}
if (!reviewer) {
  return done('Needs-context', `no review row runs off the builder's family (${builder}); set [routing.legs] review-other to another family`)
}

const facts = `The ${kind} repo is ${repo}; the change is committed at ${head}${base ? ` over base ${base}` : ''}. Its HEAD is already ${head}: read and test it in place, and never move HEAD (no checkout, switch, reset or stash). The diff of the files under review is ${diffPath}. Read a whole file as committed: git -C '${repo}' show ${head}:<path>.`

phase('Review')
const review = await leg(reviewer, `Your role is the reviewer agent profile; this brief carries only run facts. You are the REVIEWER leg of a ${kind} change: judge it on the ${kind} bar of your rule 8.

${facts}

Review only these files: ${files.join(', ')}. Number your findings n = 1, 2, 3 in order. Set a finding's file to one of the review files exactly as listed, its line to the changed line, its class to the checklist item it fails (${CLASSES.join(', ')}) or report-only, and its trigger to the input, state or rule text that makes the line wrong. Set blocking true only for a finding of one of those classes on a changed line; all else (taste, naming, style, performance, comments, old code) is report-only with blocking false.

Walk the checklist before you answer: take each review file in turn and each item below, and read the file against it. Return one {file, item, result} row per pair, result clear or finding; a finding row has a finding on the same file.
${files.map((f) => `- ${f}: ${items(f).join(', ')}`).join('\n')}
Items:
${[...new Set(files.flatMap(items))].map((i) => `- ${i}: ${ITEMS[i]}`).join('\n')}

Return model (your resolved model), needs_context (empty, or what you could not read), checklist and findings.`, REVIEW_SCHEMA, `review:${h12}`, 'Review')
if (!review) return done('Needs-context', 'the reviewer leg returned no usable result, so the review has not happened')
if (review.needs_context.trim()) return done('Needs-context', review.needs_context.trim())
const rows = new Set(review.checklist.map((c) => c && `${c.file}|${c.item}`))
const unchecked = files.flatMap((f) => items(f).filter((i) => !rows.has(`${f}|${i}`)).map((i) => `${f}:${i}`))
if (unchecked.length > 0) {
  return done('Needs-context', `${unchecked.length} checklist row(s) missing: ${unchecked.join(', ')}; run the review again`, { unchecked })
}
// A finding row with no finding of its item's class on its file fails closed: the reviewer saw a fault it did not report.
const orphans = review.checklist.filter((c) => c && c.result === 'finding' && files.includes(c.file) &&
  !review.findings.some((f) => f && f.file === c.file && (f.class === c.item || f.class === 'report-only')))
  .map((c) => `${c.file}:${c.item}`)
if (orphans.length > 0) {
  return done('Needs-context', `${orphans.length} finding row(s) name no finding: ${orphans.join(', ')}; run the review again`, { orphans })
}

// The script decides what blocks: a blocking finding outside the review files or outside the bar's classes is
// report-only; a blocking finding with no trigger fails closed. The script numbers the blocking findings, so
// each skeptic verdict names exactly one.
const blocking = []
for (const f of review.findings) {
  if (!f) continue
  const blocks = f.blocking === true && files.includes(f.file) && CLASSES.includes(f.class)
  if (blocks && !String(f.trigger ?? '').trim()) {
    return done('Needs-context', `blocking finding ${f.n} names no trigger; run the review again`)
  }
  if (blocks) blocking.push({ ...f, n: blocking.length + 1 })
  else {
    reportOnly.push(f)
    log(`report-only: ${`${f.file}:${f.line} ${f.what}`.split(/\s+/).join(' ')}`)
  }
}
if (blocking.length === 0) return pass(`no blocking finding in ${kind} diff ${h12}`, review.model)

// No skeptic row off the builder's family is a failed skeptic: every blocking finding stands.
phase('Refute')
const refute = skeptic && await leg(skeptic, `Your role is the reviewer agent profile; this brief carries only run facts. You are the SKEPTIC leg: try to refute each finding below, because a false Block costs the worker a fix.

${facts}

Set refuted true only when your own reading proves the finding wrong: the line does not do what it claims, the trigger cannot occur, the change does not add or edit the line, or the finding falls below the ${kind} bar of your rule 8. When you cannot prove it wrong, set refuted false. Give every finding a verdict by its n, with a one-sentence reason.

Findings:
${JSON.stringify(blocking, null, 1)}

Return model (your resolved model) and verdicts.`, REFUTE_SCHEMA, `refute:${h12}`, 'Refute')
// A failed skeptic, or a finding it gave no verdict, leaves that finding standing.
const verdicts = refute ? refute.verdicts.filter((v) => v && v.refuted === true) : []
const refuted = blocking.filter((f) => verdicts.some((v) => v.n === f.n))
  .map((f) => ({ ...f, reason: verdicts.find((v) => v.n === f.n).reason }))
const standing = blocking.filter((f) => !verdicts.some((v) => v.n === f.n))
if (standing.length > 0) {
  const why = refute ? `${standing.length} blocking finding(s) stand; fix each and run round ${round + 1}`
    : `the skeptic returned no usable result, so every blocking finding stands; fix each and run round ${round + 1}`
  return done('Block', why, { blocking: standing, refuted })
}
return pass(`every blocking finding in ${kind} diff ${h12} was refuted`, review.model, { refuted })
