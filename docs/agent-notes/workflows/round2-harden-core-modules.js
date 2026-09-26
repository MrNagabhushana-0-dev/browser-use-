export const meta = {
  name: 'harden-core-modules-round2',
  description: 'Four workers on security/MCP-client/downloads/LLM-schema — each finds and fixes one real agent-browsing defect after self-critiquing it',
  phases: [
    { title: 'Harden', detail: 'security_watchdog, mcp/client, downloads_watchdog, llm/schema — disjoint files, isolated worktrees' },
  ],
}

const OWNERS = [
  { key: 'security', file: 'browser_use/browser/watchdogs/security_watchdog.py', desc: 'enforces domain restrictions and security policy for the agent\'s navigation and actions — a bug here can let the agent reach a domain it should be blocked from' },
  { key: 'mcpclient', file: 'browser_use/mcp/client.py', desc: 'connection management for the agent connecting OUT to external MCP servers (filesystem, GitHub, etc.) to extend its own capabilities' },
  { key: 'downloads', file: 'browser_use/browser/watchdogs/downloads_watchdog.py', desc: 'PDF auto-download detection and file management during browsing' },
  { key: 'llmschema', file: 'browser_use/llm/schema.py', desc: 'shared JSON schema generation for tool/function calling, used by every LLM provider (OpenAI, Anthropic, Google, Groq, etc.) — a bug here affects all of them at once' },
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

1. Read ${owner.file} closely, and whatever it calls into, until you find ONE concrete defect: a correctness bug, a resource/connection leak, a race condition, a security gap (if you're on security_watchdog.py: a bypass of the domain-restriction check is the highest-value class of bug you could find there), a silent failure that contradicts a comment or docstring's own stated contract, or an unhandled edge case that would visibly break a real agent session. Ground it in specific line numbers.

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
