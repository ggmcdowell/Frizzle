// card-run — one claimed task card to a result row: a read-only plan leg that names the tier of the build, an implementer work leg (and each resume leg) on that tier's [routing.legs] row, the rules audit
// of a changed AGENTS.md or SKILL.md before its commit (rules-trim.js propose then review, harness skill OPR-17), the
// risky review of a risky change whatever status its work leg returns (risky-review.js as a child workflow; a risky
// land line with no Pass hash of this run faults; an earlier land is delivered),
// a resume implementer leg after each child workflow, one complete leg that runs the complete command card-run builds from a land's CITE log
// (only commits whose subject names this task first), and one row. An implementer leg has no Workflow tool, so card-run runs each child workflow itself.
// Ported from an earlier harness card-run with its Claim and Check legs cut: Flight claims the task first
// (Drawer/rows.py start) and passes the claim_id, and Drawer/rows.py row
// releases a claim the run left doing. Every lifecycle call runs under the session that started the workflow, the
// one the claim names (a leg runs in that session). The plan leg looks for earlier work (a worktree, commits, long jobs,
// notes); when it finds some, or Flight passed notes, the work brief opens with a RESUME block, so a run on a released
// task re-enters its worktree, its commits and its long jobs. Owner: Harness.

export const meta = {
  name: 'card-run',
  description: 'One claimed task card to done on legs: plan, work, risky review, land, complete; returns one result row',
  whenToUse: 'A task Flight has claimed (Drawer/rows.py start): args { task, claim_id, notes? }. One card-run per task; Flight passes the result to rows.py row.',
  phases: [
    { title: 'Plan', detail: 'one read-only leg reads the card and plans the build' },
    { title: 'Work', detail: 'one implementer resumes or builds, checks, lands and completes' },
    { title: 'Trim', detail: 'a changed AGENTS.md or SKILL.md runs rules-trim propose, the cuts, then review on another model' },
    { title: 'Review', detail: 'a risky change runs risky-review whatever its work leg returns; a Block goes back to the implementer' },
    { title: 'Complete', detail: 'card-run builds the --commit list from the land CITE log; one leg runs the complete' },
  ],
}

// args: { task: '<full task uuid>', claim_id: '<the claim begin printed>', notes?: '<Flight answer or revert cause>' }
const a = typeof args === 'string' ? JSON.parse(args) : (args ?? {})
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const TASK = String(a.task ?? '').trim().toLowerCase()
const CLAIM = String(a.claim_id ?? '').trim().toLowerCase()
if (!UUID.test(TASK) || !UUID.test(CLAIM)) {
  throw new Error('card-run needs args: { task: <full task uuid>, claim_id: <the claim_id task-lifecycle begin printed> }')
}
const NOTES = typeof a.notes === 'string' ? a.notes.trim() : ''
const TASK8 = TASK.slice(0, 8)
const PLANNER = { model: 'opus', effort: 'high' }  // route:plan: harness.toml [routing.legs], rendered by Drawer/settings-sync.py
const COMPLETER = { model: 'haiku', effort: 'medium' }  // route:complete: the complete leg runs one command; its receipt is parsed below
// The work and resume legs run on the tier the plan leg returns; a missing or unknown tier takes default.
const TIERS = {
  trivial: { model: 'haiku', effort: 'high' },  // route:tier-trivial
  default: { model: 'sonnet', effort: 'medium' },  // route:tier-default
  hard: { model: 'sonnet', effort: 'high' },  // route:tier-hard
  design: { model: 'opus', effort: 'xhigh' },  // route:tier-design: architecture or save-format migration design only
}
const REVIEW = { model: 'sonnet', effort: 'high' }  // route:review: the rules-trim review leg
const OTHER = { model: 'opus', effort: 'high' }  // route:review-other: the rules-trim review leg when the builder ran on the review row's family
const RULE_FILE = /^(AGENTS\.md|\.claude\/skills\/[^/]+\/SKILL\.md)$/  // Drawer/rules_audit.py is_rule_file
const OWN_ROUNDS = 3  // a Block after round 3 releases
const MAX_ROUNDS = 5  // a tip that keeps moving after review ends in a release, not a loop
const shellSafe = (s) => String(s).replace(/["'`$\\]/g, ' ').slice(0, 300)
// The one log a landing leg runs in each repo it landed: sha and subject; card-run keeps a
// commit only when the first (task <hex8> of its subject, or a leading task <hex8>, is this task (the -F grep also finds a foreign commit that
// names this task).
const CITE = `git fetch --quiet origin main && git log --reverse --format=%H%x09%s -F --grep="task ${TASK8}" refs/remotes/origin/main`

// One line: the leg profiles carry the rest (270 s per call, long-job.py start and wait, skills and memory by file).
const ENV = `Card-run leg: follow this brief only. <hub>: hub checkout, start folder; run Drawer scripts there. <python>: <hub>/.venv/bin/python -B. Claim ${CLAIM} on task ${TASK} is made: never run begin or use another. Long jobs: ${TASK8}-<what><n>.`
// Each implementer brief: a "stop": true note ends the run in a release.
const STOP = `Read the notes (<python> Drawer/mem/mem.py notes ${TASK} --json) before each commit. A "stop": true row: commit and push nothing more, release by <python> Drawer/task-lifecycle.py release ${TASK} --claim-id ${CLAIM} --reason "<the stop text>", and return status released.`

// The RESUME block opens the work brief only when the plan leg found earlier work or Flight passed notes: a re-run
// on a released task picks up where the last one stopped.
const RESUME = `RESUME (do this first, before any edit):
1. Worktree. Hub: run git worktree list in <hub>; a worktree whose folder name ends in -${TASK8} is this task's: re-enter it. Product: <python> Drawer/product_worktree.py add --task ${TASK8} returns <product repo>_wt/${TASK8}, the same tree on a re-run (run it only for product work). None yet: make it by step 3 of the life of the run.
2. Commits: in each worktree you found, git fetch origin main, then git log --oneline origin/main..HEAD: this task's work not yet landed; keep it. Then in <hub> (and in the product repo for product work) git fetch origin main and git log --format=%H -F --grep="task ${TASK8}" origin/main: this task's commits already landed by an earlier run or claim. When they hold the delivery and nothing is left unlanded, build nothing more: run ${CITE} in that repo and return status landed (line '', revlist its stdout); card-run completes under claim ${CLAIM}.
3. Long jobs: list <state_root>/jobs/${TASK8}-*.json (<state_root> is harness.toml [project] state_root) and run <python> Drawer/long-job.py status --name <each name>. A job still running: wait on it, never start it again. A done job: read its log before you rerun anything.
4. Notes: build to each note. ${STOP}
Name what RESUME found in the first line of your cause or evidence.`

const LIST = { type: 'array', items: { type: 'string' } }
const PAIRS = (x, y) => ({
  type: 'array',
  items: { type: 'object', additionalProperties: false, required: [x, y], properties: { [x]: { type: 'string' }, [y]: { type: 'string' } } },
})

// ---- Plan ------------------------------------------------------------------------------------------
phase('Plan')
const PLAN_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['model', 'text', 'earlier_work', 'tags', 'repo', 'skills', 'files', 'steps', 'checks', 'risks', 'decisions'],
  properties: {
    model: { type: 'string', description: 'your resolved model' },
    tier: { type: 'string', enum: Object.keys(TIERS), description: 'the model tier of the build: trivial, default, hard or design' },
    text: { type: 'string', description: 'the card body, byte for byte' },
    earlier_work: { type: 'string', description: "each earlier-work find of step 2, one short line each; '' when there is none" },
    tags: { ...LIST, description: 'the card tags field plus each word of its Tags: line, lower case' },
    repo: { type: 'string', enum: ['hub', 'product', 'both', 'none'], description: 'the repo the change lands in' },
    skills: { ...LIST, description: 'the skills the worker reads, by name' },
    files: { ...PAIRS('path', 'why'), description: 'each file to change, repo-relative with the repo named, and why' },
    steps: { ...LIST, description: 'the build steps, in order' },
    checks: { ...PAIRS('done_when', 'check'), description: 'each part of the Done when and the check that proves it' },
    risks: LIST,
    decisions: {
      type: 'array',
      description: 'each gap the card leaves, settled',
      items: {
        type: 'object', additionalProperties: false, required: ['gap', 'decision', 'reason'],
        properties: { gap: { type: 'string' }, decision: { type: 'string' }, reason: { type: 'string' } },
      },
    },
  },
}
const NOTE_LINE = NOTES ? `\nNotes from Flight for this run (an answer to an earlier NEED, or a revert cause): ${NOTES}\n` : ''
const plan = await agent(`${ENV}

Job: plan task ${TASK} for its one implementer, who builds to your plan. Read-only: change no file, card or claim, and run no task-lifecycle call.
${NOTE_LINE}
1. Read the card: <python> Drawer/mem/mem.py get ${TASK} --json. Return its body as text and its tags (the tags field plus the Tags: line).
2. Earlier work: a worktree whose folder ends in -${TASK8} (git worktree list in <hub>, and in the product repo when harness.toml [product] path is set); commits on origin/main that name task ${TASK8} (git fetch origin main, then git log --format=%H -F --grep="task ${TASK8}" origin/main in each repo); long jobs <state_root>/jobs/${TASK8}-*.json (harness.toml [project] state_root); notes (<python> Drawer/mem/mem.py notes ${TASK} --json). Return each find as earlier_work, or '' when there is none.
3. Search memory on its subject (<python> Drawer/mem/mem.py search "<subject>" --json): build to each design row and taste line it returns. Read the code it leans on and the skills its Skills: line names. Say in steps whether origin/main already holds the delivery.
4. Settle every gap the card leaves by best judgement from the card, memory and the code: each gap is one decision with its reason. Ask no one.
5. Name the check that proves each part of the Done when, and the risks a worker could miss.
6. Pick the tier of the build, the model that builds it: trivial (a mechanical edit of one file, a check proves it), default (most tasks: a bounded change with a clear spec), hard (multi-step reasoning, a cross-file change, a subtle fault), design (architecture or save-format migration design only; the one opus tier, never for code that follows a settled design). Unsure: default.
Return only the JSON: model, tier, text, earlier_work, tags, repo (hub, product, both or none), skills, files (path and why), steps, checks (done_when and check), risks, decisions (gap, decision, reason).`,
  { model: PLANNER.model, effort: PLANNER.effort, agentType: 'workflow-leg', schema: PLAN_SCHEMA, label: `plan:${TASK8}`, phase: 'Plan' })

const list = (x) => (Array.isArray(x) ? x : [])
const TIER = typeof plan?.tier === 'string' && Object.prototype.hasOwnProperty.call(TIERS, plan.tier) ? plan.tier : 'default'
const WORK = TIERS[TIER]
const RISKY = Boolean(plan && list(plan.tags).some((t) => String(t).toLowerCase() === 'risky'))
// RESUME goes in the work brief only on a re-run: the plan leg found earlier work, or Flight passed notes.
const EARLIER = Boolean(plan && (String(plan.earlier_work ?? '').trim() || NOTES))
// The card text goes in each brief once, as written; the plan JSON goes without it.
const PLAN_JSON = plan ? JSON.stringify(Object.fromEntries(Object.entries(plan).filter(([k]) => k !== 'text' && k !== 'model'))) : ''
let fault = ''
if (plan) log(`plan of ${TASK8}: ${plan.repo}, ${list(plan.files).length} files, ${RISKY ? 'risky' : 'not risky'}, tier ${TIER}`)

// ---- Work ------------------------------------------------------------------------------------------
phase('Work')
const WORK_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['model', 'status', 'cause', 'commits', 'receipt', 'worktree', 'rounds', 'need', 'extras', 'review', 'trim', 'land', 'evidence'],
  properties: {
    model: { type: 'string', description: 'your resolved model id, exactly as your system prompt names it' },
    status: { type: 'string', enum: ['done', 'landed', 'released', 'need', 'review', 'trim', 'failed'] },
    cause: { type: 'string', description: 'what RESUME found, when it ran; then, unless done, why in one line' },
    commits: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false, required: ['repo', 'sha'],
        properties: { repo: { type: 'string', enum: ['hub', 'product'] }, sha: { type: 'string' } },
      },
    },
    receipt: { type: 'string', description: 'the JSON line task-lifecycle complete or release printed, word for word; empty for review' },
    worktree: { type: ['string', 'null'], description: 'a worktree you keep (review, trim, released, need, failed), else null' },
    rounds: { type: 'integer', description: 'check rounds run' },
    need: { type: ['string', 'null'], description: 'status need: the question, the options and your pick, in one line' },
    extras: { ...LIST, description: 'found work you did not do, one line each' },
    review: { type: ['string', 'null'], description: 'status review: the JSON line review-diff.py printed, word for word; else null' },
    trim: { ...LIST, description: 'status trim: each changed AGENTS.md or .claude/skills/<name>/SKILL.md, relative to the hub worktree root; else []' },
    land: {
      type: 'array',
      description: 'status landed: one item per repo you landed; else []',
      items: {
        type: 'object', additionalProperties: false, required: ['repo', 'line', 'revlist'],
        properties: {
          repo: { type: 'string', enum: ['hub', 'product'] },
          line: { type: 'string', description: "the hub-land.py or product-push.py JSON line, word for word; '' when RESUME found it already landed" },
          revlist: { type: 'string', description: 'the stdout of the CITE command in that repo, run after the land, word for word: sha and subject lines' },
        },
      },
    },
    evidence: { type: 'string', description: 'status landed: 4+ words naming each plan decision you built to, each departure and why; card-run adds the shas' },
  },
}
const RETURN = 'Return only the JSON: model, status (done, landed, released, need, review, trim or failed), cause, commits (repo hub or product and the full sha you landed or pushed), receipt, worktree, rounds, need, extras, review, trim, land, evidence.'
const LAND = (reviewed) => `Land as a long job, from <hub>: a hub change by <python> Drawer/hub-land.py --name <hub worktree name> --task ${TASK8}${reviewed} (state landed or already goes on; review: return status review with its args, word for word, as review); a product change by <python> Drawer/product-push.py <product worktree> --task ${TASK8}${reviewed} (pushed or already goes on; review: as for hub). State need: release with the reason "NEED steer push" and return status need.`
// A leg that lands completes nothing. card-run builds the --commit list from its CITE log (Complete).
const LANDED = `After the land, complete nothing: in each repo you landed (<hub> for hub, the product worktree for product) run ${CITE} as one shell command. Return status landed with land, one item per landed repo (repo; line: the hub-land.py or product-push.py JSON line word for word, '' when RESUME found it already landed; revlist: that command's stdout word for word), and evidence. card-run builds the commit list and runs the complete.`
const COMPLETE_NONE = `A task that changes no file lands nothing: complete once, from <hub>: <python> Drawer/task-lifecycle.py complete ${TASK} --claim-id ${CLAIM} --evidence '<4+ words: what you checked and why no file changes>' --no-file-changes '<why>'. Result COMPLETED: return status done with that line as receipt. Any refusal: run complete no second time, change no claim id, run no release; return status failed with the refusal line as receipt and cause.`
const COMPLETE = `${LANDED} ${COMPLETE_NONE}`

const RISKY_LINE = RISKY ? `The task is risky: run <python> Drawer/review-diff.py <the worktree> for each worktree you changed. A JSON line: land nothing and complete nothing; keep the worktree and the claim and return status review with that line, word for word, as review. none: go on.` : 'The task is not risky: never return status review.'
const SHIP = `${RISKY_LINE} ${LAND('')}`
const TRIM_LINE = 'A change to the hub AGENTS.md or a hub .claude/skills/<name>/SKILL.md: commit nothing; return status trim with trim set to each such path, relative to the worktree root, and worktree set to the hub worktree. card-run runs the rules audit on them (harness skill OPR-17) and gives the commit back to you. Never run rules_audit.py pass yourself.'

const work = plan && await agent(`${ENV}

You are the one implementer of task ${TASK}. Run it to a result in this one run.
${EARLIER ? `\n${RESUME}\n` : ''}
The task card, as written:
${plan.text}
${NOTE_LINE}
The plan, from this run's plan leg (JSON): ${PLAN_JSON}

Life of the run:
1. Build to the plan: its decisions settle the gaps the card leaves; depart from it only where the code or a check proves it wrong. Read the skills it names, and PRODUCT-CONVENTIONS.md before a product edit.
2. ${EARLIER ? `Confirm origin/main lacks the delivery. It holds it already (RESUME step 2): build nothing, and return status landed with line '' and revlist from ${CITE} (step 6); an earlier run or claim that landed it is no failure.` : `The plan leg found no earlier work: start fresh. ${STOP}`}
3. Worktree, when ${EARLIER ? 'RESUME found' : 'you have'} none: hub, <python> Drawer/git-branch-policy.py worktree-add --name <session uuid8>-${TASK8} (<session uuid8>: the first 8 characters of $CLAUDE_CODE_SESSION_ID); product, <python> Drawer/product_worktree.py add --task ${TASK8}. A task that changes no file needs none. Edit and commit only there.
4. Build, then test: the test files for the files you change, plus a new test that goes red without your change. Red: fix and run again; one run is one round. After 3 red rounds, release by <python> Drawer/task-lifecycle.py release ${TASK} --claim-id ${CLAIM} --reason "<the cause>" and return status released.
5. Green: ${TRIM_LINE} Else commit, with (task ${TASK8}) in each commit SUBJECT line (the first line; CITE reads only subjects). ${SHIP}
6. ${COMPLETE}
7. A question the card and the plan leave open: decide it when it is cheap to undo, and name it in the evidence. When it is not, release with the reason "NEED: <question>" and return status need.
Every return but review, landed and failed ends in a complete or a release. Never touch another session's worktree. Ask no human.
${RETURN}`,
  { model: WORK.model, effort: WORK.effort, agentType: 'implementer', schema: WORK_SCHEMA, label: `work:${TASK8}`, phase: 'Work' })

if (plan && !work) log(`the implementer of ${TASK8} returned nothing`)

// ---- Child workflows -------------------------------------------------------------------------------
// An implementer leg has no Workflow tool, so card-run runs each child workflow and then one resume leg acts on its
// result. A child that gives no verdict (a null, a throw, no workflow() in this host) runs once more.
const childOnce = async (name, wargs) => {
  try {
    if (typeof workflow !== 'function') return null
    return (await workflow(name, wargs)) ?? null
  } catch (err) {
    log(`${name} of ${TASK8} threw: ${String(err?.message ?? err)}`)
    return null
  }
}
const childTwice = async (name, wargs, judged) => {
  const r = await childOnce(name, wargs)
  return judged(r) ? r : childOnce(name, wargs)
}
const RELEASE = (why) => `Land nothing. Release, from <hub>: <python> Drawer/task-lifecycle.py release ${TASK} --claim-id ${CLAIM} --reason "card-run: ${shellSafe(why)}". Keep the worktree. Return status released with that cause.`
const resumeLeg = (after, context, next, label, ph) => agent(`${ENV}

You resume task ${TASK} after ${after}.

The task card, as written:
${plan.text}
${NOTE_LINE}
The plan (JSON): ${PLAN_JSON}

${context}

1. Work only in the worktree that return names (none named: the folder ending in -${TASK8} in git worktree list of <hub> or of the product repo). Never touch another session's worktree. Ask no human.
2. ${STOP}
${next}
${RETURN}`,
  { model: WORK.model, effort: WORK.effort, agentType: 'implementer', schema: WORK_SCHEMA, label, phase: ph })
const before = (x) => `The implementer's return before this step (JSON): ${JSON.stringify(x)}`
let last = work

// ---- Trim ------------------------------------------------------------------------------------------
// Harness skill OPR-17: a changed AGENTS.md or SKILL.md takes rules-trim propose, the cuts (logged), a cold review on
// a model other than the builder's, and only then the rules_audit.py pass rows the pre-commit hook asks for.
let trim = null
if (!fault && last?.status === 'trim') {
  const files = list(last.trim).map((f) => String(f).replace(/\\/g, '/').replace(/^\.\//, ''))  // a Windows leg may return backslashes
  const root = String(last.worktree ?? '').trim()
  if (!files.length || !files.every((f) => RULE_FILE.test(f)) || !root) {
    fault = 'the implementer returned trim with no AGENTS.md or SKILL.md path or no worktree'
  } else {
    phase('Trim')
    const ran = (r) => Boolean(r && list(r.results).length && list(r.results).every((x) => x?.verdict !== 'failed'))
    const proposed = await childTwice('rules-trim', { mode: 'propose', files, root }, ran)
    let verdict = 'none'
    let model = null
    let summary = proposed?.summary ?? 'rules-trim propose returned nothing'
    let lost = []
    if (ran(proposed)) {
      const items = list(proposed.results).flatMap((x) => list(x.items).map((it) => ({ file: x.file, ...it })))
      last = await resumeLeg('rules-trim propose', `${before(last)}

The rules-trim propose result, JSON [{file, line, action, home, ok, why}]: ${JSON.stringify(items)}`,
      `3. Apply each cut and move to its file in the worktree; a move whose home lacks the rule (ok false) adds it there first, as its why names. Log each one, from the worktree root: <python> Drawer/rules_audit.py log --file <f> --action cut|move --line '<text>' --home '<new home>'. Skip an item that would cut a rule your change needs, and name it in cause. Commit nothing. Return status trim with the same trim and worktree.`,
      `trim:${TASK8}:1`, 'Trim')
      if (last?.status === 'trim') {
        const row = String(last.model ?? '').toLowerCase().includes(REVIEW.model) ? OTHER : REVIEW
        model = row.model
        const reviewed = await childTwice('rules-trim', { mode: 'review', files, root, model, effort: row.effort }, ran)
        verdict = !ran(reviewed) ? 'none' : reviewed.verdict === 'pass' ? 'pass' : 'fail'
        summary = reviewed?.summary ?? 'rules-trim review returned nothing'
        lost = ran(reviewed) ? list(reviewed.results).flatMap((x) => list(x.lost).map((it) => ({ file: x.file, line: it.line, why: it.why }))) : []
      } else if (!last) fault = 'the trim implementer returned nothing'
    }
    trim = { verdict, model, summary, lost }
    log(`rules-trim of ${TASK8}: ${verdict} (${model ?? 'no review'}): ${summary}`)
    if (!fault && (last?.status === 'trim' || !ran(proposed))) {
      const why = `rules-trim review ${verdict}: ${lost.map((x) => `${x.file}: ${x.why}`).join('; ') || summary}`
      last = await resumeLeg('its rules audit', `${before(last)}

The rules-trim review result (JSON): ${JSON.stringify(trim)}`,
      verdict === 'pass'
        ? `3. The rules-trim review on ${model} passed. From the worktree root, run ${files.map((f) => `<python> Drawer/rules_audit.py pass --file ${f}`).join(', then ')}, then commit. ${SHIP}
4. ${COMPLETE}`
        : `3. ${RELEASE(ran(proposed) ? why : `rules-trim propose none: ${summary}`)}`,
      `trim:${TASK8}:2`, 'Trim')
      if (!last) fault = 'the trim implementer returned nothing'
    }
  }
}

// ---- Review ----------------------------------------------------------------------------------------
// A risky worker returns status review with its review-diff.py line. risky-review.js reads it; a Needs-context
// runs once more; a null, a throw or a missing workflow() is Needs-context. Then one resume leg fixes a Block and
// returns review again, or lands with --reviewed <hash> and completes, or releases.
const parseLine = (s) => {
  try {
    const x = JSON.parse(String(s ?? ''))
    return x && typeof x === 'object' && /^[0-9a-f]{40}$/.test(String(x.head)) && /^[0-9a-f]{64}$/.test(String(x.hash)) ? x : null
  } catch { return null }
}
const reviewOnce = async (line, round, builder) => {
  try {
    if (typeof workflow !== 'function') return { verdict: 'Needs-context', reason: 'no workflow() in this host' }
    return (await workflow('risky-review', { ...line, round, builder })) ?? { verdict: 'Needs-context', reason: 'risky-review returned nothing' }
  } catch (err) {
    return { verdict: 'Needs-context', reason: String(err?.message ?? err) }
  }
}
// Card-run reviewed only a work leg that returned status review. A risky item that
// returns done or landed with no review this run (an earlier land excepted) gets one resume leg that runs
// review-diff.py: a diff goes to the review loop; none repeats the return.
// A land item an earlier call landed is delivered: line '' (RESUME found it landed), state already, or a
// hub landed line with smoke 'pushed before' (a partial land: the tip was on origin/main before this call, and the
// earlier hub-land call ran the risky gate before that push). It gets no review-first leg and needs no Pass.
const landedBefore = (it) => {
  const text = String(it?.line ?? '').trim()
  if (!text) return true
  const x = (() => { try { return JSON.parse(text) } catch { return null } })()
  return x?.state === 'already' || (it?.repo === 'hub' && x?.state === 'landed' && x?.smoke === 'pushed before')
}
const earlierLand = (x) => x?.status === 'landed' && list(x.land).length > 0 && list(x.land).every(landedBefore)
if (!fault && RISKY && ['done', 'landed'].includes(last?.status) && !earlierLand(last)) {
  last = await resumeLeg('its return, with no risky review yet', before(last),
  `3. The task is risky and no risky review has run in this run. Run <python> Drawer/review-diff.py <the worktree> for each worktree you changed. A JSON line: land and complete nothing; keep the worktree and the claim and return status review with that line, word for word, as review. none: return your earlier return unchanged.`,
  `review-first:${TASK8}`, 'Review')
  if (!last) fault = 'the review-first implementer returned nothing'
}
const reviews = []
let round = 0
while (!fault && last?.status === 'review') {
  const line = parseLine(last.review)
  if (!RISKY || !line) {
    fault = RISKY ? 'the implementer returned review with no review-diff.py line' : 'the implementer returned review on a task not tagged risky'
    break
  }
  if (round >= MAX_ROUNDS) {
    fault = `the diff to land changed after review ${MAX_ROUNDS} times`
    break
  }
  phase('Review')
  round += 1
  const builder = String(last.model ?? '').trim()
  let r = await reviewOnce(line, round, builder)
  if (r.verdict === 'Needs-context') r = await reviewOnce(line, round, builder)
  reviews.push({ head12: String(line.head).slice(0, 12), hash: line.hash, round, verdict: r.verdict, reason: r.reason ?? '' })
  log(`review ${String(line.head).slice(0, 12)} of ${TASK8} round ${round}: ${r.verdict}: ${r.reason ?? ''}`)
  const mode = r.verdict === 'Pass' ? 'finish' : r.verdict === 'Block' && round < OWN_ROUNDS ? 'fix' : 'release'
  const why = r.verdict === 'Block'
    ? `risky review Block after ${OWN_ROUNDS} rounds: ${(r.blocking ?? []).map((f) => `${f.file}:${f.line} ${f.what}`).join('; ')}`
    : `risky review gave no verdict: ${r.reason ?? ''}`
  const next = {
    fix: `3. Fix each blocking finding across its whole class, test, commit, then run <python> Drawer/review-diff.py <the worktree> again. Land and complete nothing: return status review with the new line as review.`,
    finish: `3. The review passed hash ${line.hash}. ${LAND(` --reviewed ${line.hash}`)}
4. ${COMPLETE}`,
    release: `3. ${RELEASE(why)}`,
  }[mode]
  last = await resumeLeg(`its risky review, round ${round}`, `The implementer's return before this review (JSON): ${JSON.stringify(last)}

The review result (JSON): ${JSON.stringify({ verdict: r.verdict, reason: r.reason ?? '', hash: r.hash ?? line.hash, blocking: (r.blocking ?? []).map(({ file, line: at, what, trigger }) => ({ file, line: at, what, trigger })) })}`,
  next, `resume:${TASK8}:${round}`, 'Review')
  if (!last) fault = 'the resume implementer returned nothing'
  if (mode === 'release') break
}
// ---- Complete --------------------------------------------------------------------------------------
// A landed return carries each land line and the raw stdout of CITE. card-run keeps only the full shas,
// checks the landed tip is among them, and one complete leg runs the command card-run built, word for word.
// A CITE line is '<sha>\t<subject>'; the sha is cited only when the first (task <hex8> of the subject, or a
// 'task <hex8>' that starts it (incident 7f44bdcd), is this task. Each repo the plan names needs a land item, and a land line is never '' after this run's risky review.
const FULL = /^[0-9a-f]{40}$/
const LOG_LINE = /^([0-9a-f]{40})\s+(.*)$/
const OWN = /(?:^|\()task ([0-9a-f]{8})(?![0-9a-f])/
const PLAN_REPOS = { hub: ['hub'], product: ['product'], both: ['hub', 'product'] }
const LAND_STATES = { hub: ['landed', 'already'], product: ['pushed', 'already'] }
// A risky item lands only the bytes a Pass of this run reviewed. A landed or pushed line carries
// reviewed (hub-land.py, product-push.py); a risky done with commits and no Pass is a fault too. An earlier land
// (landedBefore) needs no Pass.
const PASSES = reviews.filter((r) => r.verdict === 'Pass').map((r) => r.hash)
if (!fault && RISKY && !PASSES.length && last?.status === 'done' && list(last.commits).length) {
  fault = 'a risky item returned done with commits and no risky-review Pass'
}
if (!fault && last?.status === 'landed') {
  const cites = []
  for (const it of list(last.land)) {
    const repo = String(it?.repo ?? '')
    if (!LAND_STATES[repo]) { fault = `the landed return names repo ${repo || 'none'}`; break }
    const text = String(it.line ?? '').trim()
    let tip = ''
    if (!text && reviews.length) { fault = `the ${repo} land line is empty after this run's risky review`; break }
    if (text) {
      const x = (() => { try { return JSON.parse(text) } catch { return null } })()
      if (!x || !LAND_STATES[repo].includes(x.state)) { fault = `the ${repo} land line is not a landed state: ${shellSafe(text)}`; break }
      if (RISKY && !landedBefore(it) && !PASSES.includes(String(x.reviewed ?? ''))) {
        fault = `a risky land with no risky-review Pass: the ${repo} land line has reviewed ${shellSafe(x.reviewed ?? 'none')}`
        break
      }
      tip = String(x.tip ?? x.head ?? '')
    }
    const shas = String(it.revlist ?? '').split(/\r?\n/).map((s) => s.trim().match(LOG_LINE))
      .filter((m) => m && (m[2].match(OWN) ?? [])[1] === TASK8).map((m) => m[1])
    if (!shas.length) { fault = `the ${repo} log holds no full sha of task ${TASK8}`; break }
    if (FULL.test(tip) && !shas.includes(tip)) { fault = `the ${repo} log lacks the landed tip ${tip.slice(0, 12)}`; break }
    for (const sha of shas) if (!cites.some((c) => c.repo === repo && c.sha === sha)) cites.push({ repo, sha })
  }
  if (!fault && !cites.length) fault = 'the implementer returned landed with no land'
  const missing = fault ? undefined : (PLAN_REPOS[plan.repo] ?? []).find((r) => !cites.some((c) => c.repo === r))
  if (missing) fault = `the plan lands in ${plan.repo} but the return has no ${missing} land`
  if (!fault) {
    phase('Complete')
    const told = String(last.evidence ?? '').replace(/["'`$\\\r\n]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 1500)
    const evidence = `${told} Landed commits: ${cites.map((c) => `${c.repo} ${c.sha}`).join(', ')}`.trim()
    const cmd = `<python> Drawer/task-lifecycle.py complete ${TASK} --claim-id ${CLAIM} --evidence '${evidence}'${cites.map((c) => ` --commit ${c.repo}=${c.sha}`).join('')}`
    const COMPLETE_SCHEMA = { type: 'object', additionalProperties: false, required: ['model', 'receipt'], properties: { model: { type: 'string' }, receipt: { type: 'string' } } }
    const out = await agent(`${ENV}

Run exactly this one command from <hub>, once, as one plain-string shell command (<python> as above), and nothing else: ${cmd}
Return receipt: its JSON stdout line, word for word.`,
    { model: COMPLETER.model, effort: COMPLETER.effort, agentType: 'workflow-leg', schema: COMPLETE_SCHEMA, label: `complete:${TASK8}`, phase: 'Complete' })
    if (!out) fault = 'the complete leg returned nothing'
    else {
      const receipt = String(out.receipt ?? '')
      const rc = (() => { try { return JSON.parse(receipt) } catch { return null } })()
      const ok = rc?.result === 'COMPLETED' && rc?.id === TASK
      last = { ...last, status: ok ? 'done' : 'failed', receipt, commits: cites,
        cause: ok || typeof rc?.error === 'string' ? last.cause : `the complete leg gave no COMPLETED receipt: ${receipt.slice(0, 300)}` }
    }
  }
}
if (fault) log(fault)

// ---- Row -------------------------------------------------------------------------------------------
// land: per landed repo, the land state and its proof (hub smoke, product gated), so Flight cites the land smoke from
// the row. A '' line (RESUME found it landed) is state already: no smoke ran this run.
const landRow = (x) => x?.status === 'done' || x?.status === 'landed' ? list(x.land).map((it) => {
  const text = String(it?.line ?? '').trim()
  const p = (() => { try { return text ? JSON.parse(text) : { state: 'already' } } catch { return {} } })() ?? {}
  return { repo: String(it?.repo ?? ''), ...Object.fromEntries(['state', 'smoke', 'gated'].filter((k) => k in p).map((k) => [k, p[k]])) }
}) : []
// delivered: the implementer said done and its receipt is task-lifecycle's COMPLETED line for this task.
// Flight's check-close reads the card itself; the row never claims more than the receipt.
const end = last ?? work
const receipt = (() => { try { return JSON.parse(String(end?.receipt ?? '')) } catch { return null } })()
const delivered = Boolean(!fault && end?.status === 'done' && receipt?.result === 'COMPLETED' && receipt?.id === TASK)
const said = end?.status
const refusal = receipt && typeof receipt.error === 'string' ? receipt.error : ''
const status = delivered ? 'done'
  : fault || !end || ['done', 'landed', 'review', 'trim'].includes(said) ? 'failed'
  : said
const cause = delivered ? ''
  : !plan ? 'the plan leg returned nothing'
  : fault || (!end ? 'the implementer returned nothing'
    : said === 'done' ? `the implementer said done with no COMPLETED receipt: ${String(end.receipt ?? '').slice(0, 300)}`
    : said === 'failed' && refusal ? `complete refused: ${refusal.slice(0, 300)}${end.cause ? `; ${end.cause}` : ''}`
    : end.cause || `the implementer returned ${said}`)
return {
  task: TASK,
  claim_id: CLAIM,
  status,
  commits: end?.commits ?? [],
  land: landRow(end),
  delivered,
  need: end?.need ?? null,
  extras: end?.extras ?? [],
  cause,
  model: typeof end?.model === 'string' && end.model.trim() ? end.model.trim() : null,
  worktree: end?.worktree ?? null,
  rounds: end?.rounds ?? 0,
  reviews,
  trim,
}
