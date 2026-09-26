export const meta = {
  name: 'lead-driven-harden',
  description: 'Lead (Opus 5.5) plans + reviews, Advisor (Fable) critiques, workers (Opus 5 / Sonnet 5) implement fixes as patches',
  phases: [
    { title: 'Plan', detail: 'Lead reads the code and drafts a verified fix plan' },
    { title: 'Advise', detail: 'Advisor critiques the plan; Lead revises' },
    { title: 'Implement', detail: 'workers each implement one fix + regression test in a worktree, return a patch' },
    { title: 'Review', detail: 'Lead reviews each patch' },
    { title: 'Critique', detail: 'Advisor final pass over the accepted set' },
  ],
}

const LEAD = 'claude-opus-5-5'
const ADVISOR = 'claude-fable-5-1'
const WORKERS = ['claude-opus-5', 'claude-opus-5', 'claude-opus-5', 'claude-sonnet-5']

const BRIEF =
  'Harden the recently-added modules of browser-use (Python 3.11+ CDP browser-automation library) before more is built ' +
  'on them. Scope ONLY these: browser_use/synthesis/ (service.py, scanner.py, store.py, views.py), ' +
  'browser_use/vision/ (label.py, perceive.py, stream.py, live.py), browser_use/decide/ (service.py, page.py, views.py), ' +
  'browser_use/cobrowse/ (service.py, control.py, __main__.py), browser_use/human/ (input.py, motion.py), ' +
  'browser_use/webmcp/ (service.py, bridge.py, views.py), and browser_use/browser/profile.py. ' +
  'Goal: find and fix REAL, reproducible correctness / security / resource-leak / async defects — a bulletproof foundation. ' +
  'Hard constraints from CLAUDE.md: TABS for indentation (not spaces); modern typing (str | None, list[str]); pydantic v2; ' +
  'in tests NEVER mock anything except the LLM; NEVER use real remote URLs in tests (use pytest-httpserver); tests live in ' +
  'tests/ci/test_*.py; use uv, not pip. Every fix MUST ship a regression test that fails without the fix. ' +
  'Do NOT widen scope into new features. Do NOT disable, skip, or weaken any existing test. ' +
  'Seed findings from an earlier adversarial review — VERIFY each against the current code before trusting it: ' +
  '(1) synthesis RESOLVE_JS (service.py ~line 139) only queries [data-testid] and [data-test-id], but scanner.py locatorFor ' +
  'records testid from data-testid || data-test-id || data-test, so an element identified only by data-test with no id and ' +
  'no accessible name never resolves and its synthesized tool permanently fails; ' +
  '(2) scanner byRelevance (scanner.py ~line 210) ranks affordances by LIVE viewport position when a category exceeds its ' +
  'cap, so the names fed to fingerprint() (store.py) become scroll-dependent, defeating the cross-session cache and ' +
  'verification persistence on large pages; ' +
  '(3) the scanner returns form-less "controls" that synthesize() (service.py) never consumes, so a form-less/SPA search ' +
  'box never becomes a tool.'

const PLAN_SCHEMA = {
  type: 'object',
  properties: {
    plan_summary: { type: 'string' },
    work_items: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          title: { type: 'string' },
          files: { type: 'array', items: { type: 'string' } },
          problem: { type: 'string' },
          approach: { type: 'string' },
          acceptance: { type: 'string' },
          test_idea: { type: 'string' },
        },
        required: ['id', 'title', 'files', 'problem', 'approach'],
      },
    },
  },
  required: ['plan_summary', 'work_items'],
}

const CRITIQUE_SCHEMA = {
  type: 'object',
  properties: {
    refuted_ids: { type: 'array', items: { type: 'string' } },
    risky: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, concern: { type: 'string' } }, required: ['id', 'concern'] } },
    missing: { type: 'array', items: { type: 'object', properties: { title: { type: 'string' }, why: { type: 'string' } }, required: ['title', 'why'] } },
    overall: { type: 'string' },
  },
  required: ['overall'],
}

const PATCH_SCHEMA = {
  type: 'object',
  properties: {
    patch: { type: 'string' },
    summary: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    test_file: { type: 'string' },
    implemented: { type: 'boolean' },
  },
  required: ['implemented', 'summary'],
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    accept: { type: 'boolean' },
    required_changes: { type: 'string' },
    notes: { type: 'string' },
  },
  required: ['accept'],
}

const FINAL_SCHEMA = {
  type: 'object',
  properties: {
    ready: { type: 'boolean' },
    concerns: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
  required: ['ready'],
}

// ---- Plan: the Lead owns it ----
phase('Plan')
const draft = await agent(
  'You are the TECHNICAL LEAD of this fix effort. Here is the full brief from the junior dev:\n\n' + BRIEF +
    '\n\nRead the relevant code yourself with your tools. Produce a prioritized plan of concrete fixes for REAL defects only — ' +
    'each one verified by reading the current code (cite file:line evidence in `problem`). Quality over quantity: aim for the ' +
    'highest-value real bugs, roughly 4 to 8 items. Each work item must be independently implementable and, where possible, ' +
    'touch a DISJOINT set of files from the others (they will be implemented in parallel). Give each a stable id like FIX-1.',
  { model: LEAD, effort: 'high', label: 'lead:plan', phase: 'Plan', schema: PLAN_SCHEMA },
)

// ---- Advise: Lead consults the advisor, then revises ----
phase('Advise')
const critique = await agent(
  'You are the SENIOR ADVISOR. The lead drafted this fix plan for browser-use hardening. Be skeptical and specific. ' +
    'Which items are NOT real bugs (list their ids in refuted_ids)? Which are risky or likely to break other behavior? ' +
    'What high-value real defects are MISSING from the plan within the stated scope?\n\nPLAN:\n' + JSON.stringify(draft),
  { model: ADVISOR, effort: 'high', label: 'advisor:critique', phase: 'Advise', schema: CRITIQUE_SCHEMA },
)

const finalPlan = await agent(
  'You are the LEAD. Revise your plan given the advisor critique. Drop any item you cannot defend against a refutation; ' +
    'add any missing item the advisor raised that you verify is real (read the code to confirm). Keep items disjoint by file ' +
    'where possible. Output the final work_items.\n\nYOUR DRAFT:\n' + JSON.stringify(draft) +
    '\n\nADVISOR CRITIQUE:\n' + JSON.stringify(critique),
  { model: LEAD, effort: 'high', label: 'lead:revise', phase: 'Plan', schema: PLAN_SCHEMA },
)

const items = (finalPlan && finalPlan.work_items) || []
log('Lead finalized ' + items.length + ' work item(s) after advisor review.')

// ---- Implement -> Review, pipelined per item ----
function implement(it, i) {
  return agent(
    'You are implementing EXACTLY ONE fix in an isolated git worktree of the browser-use repo. Follow repo conventions: ' +
      'TABS for indentation, modern typing (str | None), pydantic v2. Add a regression test in tests/ci/ that FAILS without ' +
      'your fix and passes with it — use pytest-httpserver, NEVER a real remote URL, NEVER mock anything except the LLM. ' +
      'Keep the change minimal and correct; do not touch unrelated code or other tests. There is no virtualenv in this ' +
      'worktree, so do NOT try to run the suite — implement carefully; the junior dev validates centrally.\n\n' +
      'When finished, run exactly:  git add -A && git --no-pager diff --cached\n' +
      'Return the FULL patch text in `patch`, a `summary`, `files_changed`, the `test_file` path, and implemented=true. ' +
      'If after reading the code you conclude it is NOT actually a bug, return implemented=false with your reasoning in summary.\n\n' +
      'WORK ITEM ' + it.id + ': ' + it.title + '\nFILES: ' + (it.files || []).join(', ') + '\nPROBLEM: ' + it.problem +
      '\nAPPROACH: ' + (it.approach || '') + '\nACCEPTANCE: ' + (it.acceptance || '') + '\nTEST IDEA: ' + (it.test_idea || ''),
    { model: WORKERS[i % WORKERS.length], effort: 'high', isolation: 'worktree', label: 'worker:' + it.id, phase: 'Implement', schema: PATCH_SCHEMA },
  )
}

function review(patch, it) {
  if (!patch || !patch.implemented || !patch.patch) {
    return Promise.resolve({ accept: false, required_changes: 'not implemented', notes: (patch && patch.summary) || 'no patch' })
  }
  return agent(
    'You are the LEAD reviewing a worker patch for work item ' + it.id + ' (' + it.title + '). Judge: does it correctly and ' +
      'minimally fix the stated problem, follow conventions (tabs, pydantic v2, typing), and include a genuine regression ' +
      'test that would fail without the change and does not mock anything but the LLM or use a real URL? Accept=true ONLY if ' +
      'it is ready to apply as-is. Otherwise accept=false and state the specific required changes.\n\nPROBLEM: ' + it.problem +
      '\n\nPATCH:\n' + patch.patch,
    { model: LEAD, effort: 'high', label: 'lead:review:' + it.id, phase: 'Review', schema: REVIEW_SCHEMA },
  )
}

const processed = await pipeline(
  items.map(function (it, i) { return { it: it, i: i } }),
  function (x) { return implement(x.it, x.i) },
  function (patch, x) { return review(patch, x.it).then(function (rv) { return { item: x.it, patch: patch, review: rv } }) },
)

const done = processed.filter(Boolean)
const accepted = done.filter(function (x) { return x.review && x.review.accept && x.patch && x.patch.patch })
const rejected = done.filter(function (x) { return !(x.review && x.review.accept && x.patch && x.patch.patch) })

// ---- Critique: advisor final pass over accepted patches ----
phase('Critique')
const finalCritique = await agent(
  'You are the SENIOR ADVISOR doing a final pre-merge pass. Here are the accepted patches for browser-use hardening. ' +
    'Is anything missing or risky before merge? Any patch that could break other behavior or interact badly with another? ' +
    'Set ready=true only if the set is safe to apply. Be concise.\n\n' +
    JSON.stringify(accepted.map(function (x) { return { id: x.item.id, title: x.item.title, summary: x.patch.summary, files: x.patch.files_changed } })),
  { model: ADVISOR, effort: 'high', label: 'advisor:final', phase: 'Critique', schema: FINAL_SCHEMA },
)

log('Accepted ' + accepted.length + ' patch(es); ' + rejected.length + ' rejected/unimplemented.')

return {
  plan_summary: finalPlan && finalPlan.plan_summary,
  accepted: accepted.map(function (x) {
    return { id: x.item.id, title: x.item.title, files: x.patch.files_changed, test_file: x.patch.test_file, summary: x.patch.summary, patch: x.patch.patch }
  }),
  rejected: rejected.map(function (x) {
    return { id: x.item.id, title: x.item.title, reason: (x.review && (x.review.required_changes || x.review.notes)) || 'not implemented' }
  }),
  advisor_final: finalCritique,
}