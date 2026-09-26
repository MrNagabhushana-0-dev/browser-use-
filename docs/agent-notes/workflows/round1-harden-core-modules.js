export const meta = {
  name: 'harden-core-modules',
  description: 'Four workers, one per untouched core module, each finds and fixes one real agent-browsing defect after self-critiquing it',
  phases: [
    { title: 'Harden', detail: 'Agent/BrowserSession/Tools/DomService — disjoint files, isolated worktrees, self-critique before implementing' },
  ],
}

const OWNERS = [
  { key: 'agent', file: 'browser_use/agent/service.py', desc: 'the main orchestrator — task loop, LLM-driven action execution, step/retry handling' },
  { key: 'session', file: 'browser_use/browser/session.py', desc: 'browser lifecycle, CDP connections, watchdog coordination via the bubus event bus' },
  { key: 'tools', file: 'browser_use/tools/service.py', desc: 'the action registry mapping LLM decisions to browser operations (click, type, scroll, etc.)' },
  { key: 'dom', file: 'browser_use/dom/service.py', desc: 'DOM extraction, element highlighting, accessibility tree generation' },
]

const roster = OWNERS.map(o => `- ${o.file} — ${o.desc}`).join('\n')

const SCHEMA = {
  type: 'object',
  properties: {
    module: { type: 'string' },
    implemented: { type: 'boolean' },
    title: { type: 'string' },
    defect: { type: 'string', description: 'what is actually wrong, with file:line evidence' },
    self_critique: { type: 'string', description: 'the case against your own finding, and why it still holds (or why you dropped it for a different one)' },
    fix_summary: { type: 'string' },
    verification: { type: 'string', description: 'exact commands run and their real output — ruff, pyright, the new test failing pre-fix and passing post-fix, the existing test file for this module' },
    worktree_path: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    reason_if_not_implemented: { type: 'string' },
  },
  required: ['module', 'implemented', 'title', 'defect', 'self_critique', 'fix_summary', 'verification'],
}

function workerPrompt(owner) {
  return `You are hardening ONE core module of browser-use against a real, reproducible limitation in agent browsing — not a style nitpick, not a hypothetical, not a feature request.

YOUR MODULE (read and, if warranted, edit ONLY this file plus a new test file you add under tests/ci/):
${owner.file} — ${owner.desc}

THE FULL ROSTER (so you don't duplicate or step on another worker's file — if you spot something wrong in one of these, name it in your report instead of touching it):
${roster}

Do this in order, in an isolated git worktree:

1. Read ${owner.file} closely, and whatever it calls into, until you find ONE concrete defect: a correctness bug, a resource/connection leak, a race condition, a silent failure that contradicts a comment or docstring's own stated contract, or an unhandled edge case that would visibly break a real agent session. Ground it in specific line numbers.

2. Self-critique it before you touch any code — this stands in for asking an advisor, so do it rigorously rather than rubber-stamping your own find: Is this actually wrong, or intentional? Does an existing test already cover it? Would fixing it change documented/public behavior in a way that breaks compatibility? Is there a simpler explanation you're missing? If it doesn't survive this, find a different, more solid defect — don't force a fix on a shaky finding. If after genuinely trying you find nothing real, set implemented=false and say why in reason_if_not_implemented — a forced fake fix wastes everyone's time, which is exactly what you're here to not do.

3. Implement the minimal fix. Repo conventions: tabs for indentation, pydantic v2, modern typing (str | None, not Optional[str]), runtime assertions at function start/end where the codebase already uses that pattern in this file. Add a regression test in tests/ci/ (a NEW file, name it for the bug) that FAILS on the unfixed code and PASSES with your fix — use pytest-httpserver for any page, a real BrowserSession, never a mock except the LLM, never a real remote URL.

4. Verify for real, don't just assert it: run your new test against the unfixed code first (show it fail with the specific error), then with your fix (show it pass). Run the existing test file(s) for this module — nothing that currently passes may start failing. Run ruff check, ruff format --check, and pyright on every file you touched. Put the actual commands and actual output in the verification field — I will independently re-run everything myself and will hold or revert anything I can't reproduce, so don't round up.

Report via the schema. worktree_path is the absolute path to your worktree; files_changed lists every file you touched (source + test).`
}

phase('Harden')
const results = await Promise.all(
  OWNERS.map(o => agent(workerPrompt(o), {
    label: `harden:${o.key}`,
    phase: 'Harden',
    schema: SCHEMA,
    isolation: 'worktree',
    effort: 'high',
  }))
)

return results.filter(Boolean)
