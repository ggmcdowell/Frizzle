// rules-trim — the removal test on the hub's instruction files (harness skill OPR-17).
// The leg contract lives in .claude/agents/workflow-leg.md, spawned by agentType. This template repeats
// the leg helper and the return shape of the general templates; a script cannot import.
// The guidance and its sources: .claude/skills/harness/instruction-files.md.
// mode 'propose': one read-only leg per file applies the removal test to each rule line and lists each cut
// or move with its new home. The session applies them and logs each (Drawer/rules_audit.py log).
// mode 'review': one cold leg per file reads that file's diff and checks that every moved rule exists in
// its new home and every cut passes the removal test. Run it on a model other than the session's.

export const meta = {
  name: 'rules-trim',
  description: 'Removal test on AGENTS.md and SKILL.md files: propose cuts and moves, or cold-review a trim',
  whenToUse: 'On each edit of AGENTS.md or a SKILL.md (the pre-commit hook asks for a pass), or for a full audit of every rules file.',
  phases: [{ title: 'Trim', detail: 'one leg per file: propose or review' }],
}

// args: { mode: 'propose' | 'review', files?: ['AGENTS.md' | '.claude/skills/<name>/SKILL.md', ...],
//         root?: '<abs path of the hub or task worktree>', base?: '<git ref>', model?: 'sonnet' | 'opus' | 'haiku',
//         effort?: 'low' to 'max' }
const a = typeof args === 'string' ? JSON.parse(args) : (args ?? {})
const MODE = a.mode
if (MODE !== 'propose' && MODE !== 'review') throw new Error("rules-trim needs args: mode: 'propose' | 'review'")
const ROUTE = { model: 'sonnet', effort: 'medium' }  // route:rules-trim: harness.toml [routing.legs], rendered by Drawer/settings-sync.py
const MODELS = /^(opus|sonnet|haiku)$/  // [gates] allowed_models in harness.toml
const LEVELS = /^(low|medium|high|xhigh|max)$/
const pick = (rx, v) => typeof v === 'string' && rx.test(v)  // a non-string arg never passes a pick
const MODEL = pick(MODELS, a.model) ? a.model : ROUTE.model
const EFFORT = pick(LEVELS, a.effort) ? a.effort : ROUTE.effort
if (a.effort != null && EFFORT !== a.effort) log(`effort ${JSON.stringify(a.effort)} is not low, medium, high, xhigh or max; the route effort stands`)
const ROOT = typeof a.root === 'string' && a.root ? a.root : 'the hub root (your working folder)'
const BASE = typeof a.base === 'string' && a.base ? a.base : 'origin/main'
const FILES = Array.isArray(a.files) && a.files.length ? a.files.filter((f) => typeof f === 'string' && f).map((f) => f.replace(/\\/g, '/')) : ['AGENTS.md']
const cap = (f) => (f.split('/').pop() === 'AGENTS.md' ? 40 : 500)  // Drawer/rules_audit.py CAPS
const target = (f) => (f.split('/').pop() === 'AGENTS.md' ? 35 : cap(f) - 1)  // an AGENTS.md trim lands near 35
const modelMismatches = []

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

function leg(prompt, schema, label) {
  return agent(prompt, { model: MODEL, effort: EFFORT, agentType: 'workflow-leg', schema: { ...schema, required: ['model'] }, label: label, phase: 'Trim' })
    .then((out) => unpack(out, schema)).then((out) => {
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

const ITEM = {
  type: 'object', additionalProperties: false, required: ['line', 'action', 'home', 'ok', 'why'],
  properties: {
    line: { type: 'string' },                             // the rule line text
    action: { type: 'string', enum: ['cut', 'move'] },    // cut: no session needs it; move: it lives elsewhere
    home: { type: 'string' },                             // a move's new home: path and rule id; '' for a cut
    ok: { type: 'boolean' },  // propose: the home already holds it (a cut: true); review: the rule survives or the cut is safe
    why: { type: 'string' },  // one sentence: why it fails the removal test, and the id it carries
  },
}
const SCHEMA = {
  type: 'object', additionalProperties: false, required: ['model', 'lines', 'verdict', 'items', 'reason'],
  properties: {
    model: { type: 'string' },
    lines: { type: 'integer' },  // the file's lines now (propose) or after the trim (review)
    verdict: { type: 'string', enum: ['pass', 'fail'] },
    items: { type: 'array', items: ITEM },
    reason: { type: 'string' },
  },
}

const GUIDE = `Read the guidance first: ${ROOT}/.claude/skills/harness/instruction-files.md. In short: every line of AGENTS.md loads into every session, and a loaded skill stays in context, so each line costs attention on every task. The removal test for each rule line: would removing it cause a session to make a mistake? A line fails the test when a session could work it out from the code or a tool's own output, when a gate, hook or script already enforces it and prints its own refusal, when it repeats a rule that lives in another file, or when it narrates history or rationale a session does not act on. The root AGENTS.md holds direction and operational rules that bind every session; an incident fix or an improvement moves to the skill, hook, script or manual that enforces or teaches it, with its decision or incident id. A rule lives once (harness skill OPR-10). The line that starts 'Rules sentinel:' always stays.`

function prompt(file) {
  const c = cap(file)
  if (MODE === 'propose') return `Apply the removal test to ${ROOT}/${file} (cap ${c} lines). Read only; write nothing.

${GUIDE}

1. Read the file with line numbers. Count its lines.
2. Test each rule line. Keep a line that passes. A line that fails is a cut (no session needs it anywhere) or a move (one craft, Flight or one tool needs it). A long line may keep a short core and move its detail.
3. For each move, find its home: grep the skills, Drawer/ scripts and docstrings, and PRODUCT-CONVENTIONS.md. ok is true when the home already holds the rule; else why names the exact text to add there.
4. Bring the file to about ${target(file)} lines when it is over ${target(file)}: the weakest lines move first. Under that, list only clear failures: do not cut a rule to save a line.
5. lines: the count now. verdict: pass when the file is within its cap after the items, else fail. reason: the projected count and one sentence.`
  return `Cold-review a trim of ${ROOT}/${file} (cap ${c} lines). Read only; write nothing. You did not make this trim: trust no claim, read the files.

${GUIDE}

1. Run: git -C ${ROOT} diff ${BASE} -- ${file}. Each removed or shortened line is one item.
2. For a moved line, find where its rule now lives: read the whole diff (git -C ${ROOT} diff ${BASE}) and grep the repo at ${ROOT}. Check every clause: each fact, number, tool, flag and id must exist in its new home or in the new text. A clause that exists nowhere is a lost rule: ok false, and why names the lost clause. For a cut line, ok is true only when it fails the removal test.
3. Check the line count (wc -l) against the cap, that the sentinel line stays when the file is AGENTS.md, and that no file still points to a removed rule (git -C ${ROOT} grep -n "${file.split('/').pop()}").
4. lines: the count after the trim. verdict: pass only when every item is ok, the count is within the cap and no stale pointer is left; else fail. reason: one sentence per failure, or one sentence.`
}

phase('Trim')
const outs = await parallel(FILES.map((f) => () => leg(prompt(f), SCHEMA, `${MODE}:${f}`)))
const results = FILES.map((file, i) => {
  const out = outs[i]
  if (!out) return { file, verdict: 'failed', lines: null, items: [], lost: [], reason: 'the leg returned nothing' }
  const items = Array.isArray(out.items) ? out.items : []
  const lost = items.filter((x) => !x.ok)
  const overCap = !Number.isInteger(out.lines) || out.lines > cap(file)
  const verdict = MODE === 'review' && (lost.length || overCap) ? 'fail' : out.verdict
  return { file, verdict, lines: out.lines, items, lost, reason: out.reason }
})
const count = (v) => results.filter((r) => r.verdict === v).length
const verdict = count('pass') === results.length ? 'pass' : 'fail'
return {
  summary: `${MODE}: ${verdict}; ${count('pass')} pass, ${count('fail')} fail, ${count('failed')} legs failed of ${results.length} files; ${results.reduce((n, r) => n + r.items.length, 0)} items`,
  verdict, results, modelMismatches,
}
