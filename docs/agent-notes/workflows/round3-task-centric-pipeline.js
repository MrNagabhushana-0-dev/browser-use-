export const meta = {
  name: 'round3-task-centric-hardening',
  description: 'Task-centric pipeline with mandatory checkpoint-evaluator gates: hunt -> evaluate -> refute -> evaluate -> implement, model-diverse, plus one innovation measurement task',
  phases: [
    { title: 'Hunt', detail: 'claim only, no code, Sonnet 5' },
    { title: 'Refute', detail: 'a different model (Opus 5) tries to kill the claim' },
    { title: 'Implement', detail: 'survivors only, Opus 5, isolated worktree' },
    { title: 'Measure', detail: 'innovation-backlog idea #3: real usage measurement, not a bug fix' },
  ],
}

const TARGETS = [
  { key: 'default_action_watchdog', file: 'browser_use/browser/watchdogs/default_action_watchdog.py', desc: 'the core action-dispatch watchdog -- likely the busiest per-agent-step code path in the whole system (3752 lines)' },
  { key: 'crash_watchdog', file: 'browser_use/browser/watchdogs/crash_watchdog.py', desc: 'crash detection and recovery for the browser process' },
  { key: 'llm_openai_chat', file: 'browser_use/llm/openai/chat.py', desc: 'the OpenAI chat provider implementation' },
]

const HUNT_SCHEMA = {
  type: 'object',
  properties: {
    implemented_hunt: { type: 'boolean', description: 'true if you found a real, worth-pursuing claim' },
    title: { type: 'string' },
    claim: { type: 'string', description: 'the specific defect, with file:line evidence' },
    mechanism: { type: 'string', description: 'exactly how it fails in practice' },
    severity: { type: 'string', enum: ['low', 'medium', 'high'] },
    confidence: { type: 'string', enum: ['low', 'medium', 'high'] },
    intended_behavior_check: { type: 'string', description: 'your case for why this is NOT intended behavior' },
    reason_if_nothing_found: { type: 'string' },
  },
  required: ['implemented_hunt', 'title', 'claim', 'mechanism', 'severity', 'confidence', 'intended_behavior_check'],
}

const EVAL_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['pass', 'weak', 'poor'] },
    reasoning: { type: 'string' },
    correction: { type: 'string', description: 'if weak: the specific thing to fix before retrying' },
  },
  required: ['verdict', 'reasoning'],
}

const REFUTE_SCHEMA = {
  type: 'object',
  properties: {
    killed: { type: 'boolean', description: 'true if you could NOT reproduce the claim, or determined it is intended behavior -- the claim is dead' },
    reasoning: { type: 'string' },
    test_file_path: { type: 'string', description: 'if not killed: path to the failing regression test you wrote, in your worktree' },
    worktree_path: { type: 'string' },
    repro_command_and_output: { type: 'string', description: 'the exact command you ran against unfixed code and its exact output' },
  },
  required: ['killed', 'reasoning'],
}

const IMPLEMENT_SCHEMA = {
  type: 'object',
  properties: {
    implemented: { type: 'boolean' },
    fix_summary: { type: 'string' },
    verification: { type: 'string', description: 'exact commands and output: new test passes, existing module suite passes, ruff/pyright clean' },
    worktree_path: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
  },
  required: ['implemented', 'fix_summary', 'verification'],
}

async function evaluate(subject, promptContext) {
  return agent(
    `You are the EVALUATOR at a checkpoint in a bug-hunting pipeline. Judge ONLY what's given below -- don't do the work yourself.\n\n${promptContext}\n\nSUBJECT TO JUDGE:\n${JSON.stringify(subject, null, 2)}\n\nVerdict pass: this is solid, let it proceed as-is. weak: promising but has a specific, fixable gap -- name it in 'correction' so it can be retried. poor: fundamentally not worth pursuing (vague, no real evidence, clearly intended behavior, or not reproducible-sounding) -- retire it.`,
    { schema: EVAL_SCHEMA, effort: 'low' }
  )
}

async function huntStage(target) {
  const basePrompt = (model) => `You are hunting for ONE real, reproducible defect in browser-use (an async Python 3.11+ CDP browser-automation library) -- not a style nitpick, not a hypothetical.

YOUR FILE (read-only at this stage -- do not write any code yet, this is a claim, not an implementation):
${target.file} -- ${target.desc}

Read it closely. Find ONE concrete defect: a correctness bug, a resource/connection leak, a race condition, a silent failure contradicting a comment or docstring's own contract, or an unhandled edge case that would visibly break a real agent session. Ground it in specific line numbers. Before finalizing, argue against your own finding: is it actually wrong, or intentional? If after genuinely trying you find nothing real, set implemented_hunt=false and say why -- a forced fake claim wastes the whole pipeline behind it.`

  let result = await agent(basePrompt('claude-sonnet-5'), { label: `hunt:${target.key}`, phase: 'Hunt', schema: HUNT_SCHEMA, model: 'claude-sonnet-5', effort: 'medium' })
  if (!result || !result.implemented_hunt) return { target, claim: null, dead_at: 'hunt', reason: result ? result.reason_if_nothing_found : 'agent error' }

  const verdict = await evaluate(result, 'This is a HUNT claim -- a proposed defect, no code written yet. Judge whether the claim is specific, grounded in real line numbers, and plausibly not intended behavior.')
  if (!verdict || verdict.verdict === 'pass') return { target, claim: result, dead_at: null }

  if (verdict.verdict === 'weak') {
    const retried = await agent(
      `${basePrompt('claude-sonnet-5')}\n\nYou already tried once; the evaluator's correction: ${verdict.correction}\nYour prior claim: ${JSON.stringify(result)}`,
      { label: `hunt:${target.key}:retry`, phase: 'Hunt', schema: HUNT_SCHEMA, model: 'claude-sonnet-5', effort: 'medium' }
    )
    if (retried && retried.implemented_hunt) return { target, claim: retried, dead_at: null }
    return { target, claim: null, dead_at: 'hunt-retry', reason: verdict.reasoning }
  }

  // poor -- reassign to a different model tier, per README section C: checkpoint-based reassignment, not a restart from nothing (same target/instructions, fresh attempt)
  const reassigned = await agent(basePrompt('claude-opus-5'), { label: `hunt:${target.key}:reassigned`, phase: 'Hunt', schema: HUNT_SCHEMA, model: 'claude-opus-5', effort: 'medium' })
  if (reassigned && reassigned.implemented_hunt) return { target, claim: reassigned, dead_at: null }
  return { target, claim: null, dead_at: 'hunt-poor', reason: verdict.reasoning }
}

async function refuteStage(hunted) {
  if (!hunted.claim) return { ...hunted, refutation: null }
  const target = hunted.target
  const basePrompt = `You are the REFUTER. Try to KILL this claim -- you succeed by proving it wrong or unreproducible, not by confirming it.

CLAIM (from a hunter who read ${target.file}):
${JSON.stringify(hunted.claim, null, 2)}

In an isolated git worktree: try to write a test that reproduces this failing on the CURRENT, unfixed code. If you cannot make it fail, or you determine the described behavior is actually intended/documented, set killed=true and explain why. If it genuinely reproduces, set killed=false, keep the failing test in your worktree (do not fix the source yet -- that is a later stage's job), and give the exact repro command + output.`

  let result = await agent(basePrompt, { label: `refute:${target.key}`, phase: 'Refute', schema: REFUTE_SCHEMA, model: 'claude-opus-5', isolation: 'worktree', effort: 'high' })
  if (!result) return { ...hunted, refutation: { killed: true, reasoning: 'refuter agent error' } }

  const verdict = await evaluate(result, 'This is a REFUTE verdict on a hunted claim. Judge whether the reasoning is rigorous (a real repro attempt or a real intended-behavior citation) versus a shallow guess either way.')
  if (!verdict || verdict.verdict === 'pass') return { ...hunted, refutation: result }

  if (verdict.verdict === 'weak') {
    const retried = await agent(
      `${basePrompt}\n\nYou already tried once; the evaluator's correction: ${verdict.correction}\nYour prior verdict: ${JSON.stringify(result)}`,
      { label: `refute:${target.key}:retry`, phase: 'Refute', schema: REFUTE_SCHEMA, model: 'claude-opus-5', isolation: 'worktree', effort: 'high' }
    )
    return { ...hunted, refutation: retried || result }
  }

  // poor -- reassign to a different model
  const reassigned = await agent(basePrompt, { label: `refute:${target.key}:reassigned`, phase: 'Refute', schema: REFUTE_SCHEMA, model: 'claude-sonnet-5', isolation: 'worktree', effort: 'high' })
  return { ...hunted, refutation: reassigned || { killed: true, reasoning: 'both refute attempts failed to reach a confident verdict; treating as killed rather than shipping on a shaky call' } }
}

async function implementStage(refuted) {
  if (!refuted.refutation || refuted.refutation.killed) return { ...refuted, implementation: null }
  const target = refuted.target
  const prompt = `You are implementing a fix for a claim that survived hunting and refutation.

CLAIM: ${JSON.stringify(refuted.claim, null, 2)}
REFUTER'S REPRODUCTION: ${JSON.stringify(refuted.refutation, null, 2)}
Refuter's worktree (their failing test lives here -- read it, do not weaken or rewrite it to fit your fix): ${refuted.refutation.worktree_path || 'not given -- recreate the repro yourself first'}

Repo conventions: tabs, pydantic v2, modern typing (str | None). Implement the minimal fix in your OWN isolated worktree (start fresh from HEAD; port the refuter's test in, don't edit its assertions). Verify for real: new test fails pre-fix and passes post-fix, ruff check, ruff format --check, pyright, and the existing test file(s) for ${target.file}. I will independently re-run everything myself before this lands, so report exact commands and exact output, not a summary.`

  const result = await agent(prompt, { label: `implement:${target.key}`, phase: 'Implement', schema: IMPLEMENT_SCHEMA, model: 'claude-opus-5', isolation: 'worktree', effort: 'high' })
  return { ...refuted, implementation: result }
}

phase('Hunt')
const bugResults = await pipeline(TARGETS, huntStage, refuteStage, implementStage)

phase('Measure')
const MEASURE_SCHEMA = {
  type: 'object',
  properties: {
    measurable_now: { type: 'boolean', description: 'false if no real usage data exists to measure this from -- do not estimate or guess a number if so' },
    findings: { type: 'string' },
    batched_execution_usage_evidence: { type: 'string', description: 'what you actually found: test corpus patterns, action registry usage in examples/, anything real -- or an honest statement that no real production telemetry exists here' },
  },
  required: ['measurable_now', 'findings'],
}
const measureResult = await agent(
  `Innovation-backlog idea #3 (docs/agent-notes/ideas-backlog.json): "measure current usage of the existing batched code-execution action vs single-step actions across real task runs, before proposing to encourage it more."

Investigate honestly whether this is measurable AT ALL from what's actually available in this repo/environment (no production telemetry, no fleet logs of real agent runs exist here). Look at: the evaluate()/code-execution action definition in browser_use/tools/service.py, how examples/ and tests/ci/ use it versus single-step actions, and whether any benchmark/eval infrastructure in this repo records action-type frequency. If there is no real data to measure this from, say so plainly (measurable_now=false) rather than inventing a usage percentage -- an honest "not measurable here" is the correct outcome the ledger's evidence standard requires, not a failure to work around.`,
  { label: 'measure:batched-execution-usage', phase: 'Measure', schema: MEASURE_SCHEMA, model: 'claude-sonnet-5', effort: 'medium' }
)

return { bugResults, measureResult }
