export const meta = {
  name: 'retinat-deadline-design',
  description: 'Map scan/browse/explore/watch loops and design a sub-60s overall deadline with partial results (read-only)',
  phases: [
    { title: 'Map', detail: 'one reader per long-running Retinat tool path' },
    { title: 'Design', detail: 'synthesize one deadline design + test plan' },
    { title: 'Critique', detail: 'adversarial review of the design' },
  ],
}

const CONTEXT = `Repository: /home/user/browser-use- (python, tabs for indentation, see CLAUDE.md).
Problem (measured live): MCP clients such as Claude Code abandon a tool call at 60 s and report "MCP server \\"retinat\\" tool \\"<name>\\" timed out after 60s".
retinat_scan failed that way twice in a real session; retinat_wait/watch maxima were already lowered to 50 s / 45 s (commit 906ca7c),
but retinat_scan (max_screens up to 80), retinat_browse (items up to 30 x max_seconds up to 45) and retinat_explore (max_pages up to 100)
have no overall deadline and can still run past 60 s, losing all their work.
Goal: each long tool returns what it has done before ~50 s, saying plainly what it covered and how to continue (e.g. "stopped at 50 s after 12 of 25 screens; call again with ... to go on").
The MCP server is browser_use/retinat/server.py (RetinatServer, tool handlers in a big dispatch); the eyes are browser_use/eyes/service.py (Eyes.scan/browse/watch), browser_use/eyes/page.py (scan internals), browser_use/explore/service.py (site explorer).
This is READ-ONLY research: do not edit any file, do not run tests or browsers.`

const MAP_SCHEMA = {
  type: 'object',
  properties: {
    tool: { type: 'string' },
    entry_points: { type: 'array', items: { type: 'string' }, description: 'file:line of the handler and the functions it calls' },
    loop_structure: { type: 'string', description: 'how the long loop is structured, where time goes, what bounds it today' },
    existing_time_limits: { type: 'array', items: { type: 'string' }, description: 'any timeouts/budgets already present, file:line' },
    deadline_insertion_points: { type: 'array', items: { type: 'string' }, description: 'file:line where a deadline check fits, and why' },
    partial_result_shape: { type: 'string', description: 'what a partial result could return and how the caller continues (resume param?)' },
    risks: { type: 'array', items: { type: 'string' } },
    relevant_tests: { type: 'array', items: { type: 'string' }, description: 'tests/ci files and test names that cover this path' },
  },
  required: ['tool', 'entry_points', 'loop_structure', 'deadline_insertion_points', 'partial_result_shape', 'relevant_tests'],
}

const TARGETS = [
  { key: 'scan', prompt: 'retinat_scan: trace from the server handler through Eyes.scan and browser_use/eyes/page.py scan() (scrolling loop, frame collection, choose_page_frames, sheet rendering).' },
  { key: 'browse', prompt: 'retinat_browse and retinat_watch: trace from the server handlers through Eyes.browse / Eyes.watch (per-item watch loop, swipes, until=bored logic, sheet assembly).' },
  { key: 'explore', prompt: 'retinat_explore: trace from the server handler through browser_use/explore/service.py (page crawl loop, retries with RETRY_AFTER_S, probes, report rendering).' },
  { key: 'server', prompt: 'The Retinat server dispatch itself (browser_use/retinat/server.py): how tool calls are run (act(), effect_state, Refused, error paths), whether there is any shared place to impose a per-call deadline (asyncio.timeout around handlers?), how cancellation would interact with browser input in flight and the recorder, and what the existing test for tool duration maxima (test_no_tool_offers_a_wait_longer_than_a_client_waits_for_a_call in tests/ci/test_retinat.py) checks.' },
]

phase('Map')
const maps = await parallel(TARGETS.map(t => () => agent(
  `${CONTEXT}\n\nYour part: ${t.prompt}\nRead the code carefully (use Grep/Read). Report precisely with file:line references.`,
  { label: `map:${t.key}`, phase: 'Map', schema: MAP_SCHEMA },
)))
const good = maps.filter(Boolean)
log(`mapped ${good.length} of ${TARGETS.length} paths`)

phase('Design')
const design = await agent(
  `${CONTEXT}\n\nHere are maps of the long-running paths, from independent readers:\n${JSON.stringify(good, null, 2)}\n\n` +
  `Design ONE coherent change that gives scan, browse (and watch if needed) and explore an overall deadline under the client's 60 s, ` +
  `returning partial results that say what was covered and how to continue. Prefer cooperative deadline checks inside the loops (so ` +
  `work done is kept and no input is left half-sent) over cancelling the whole handler; consider a shared helper (e.g. a Deadline ` +
  `pydantic model or small dataclass in browser_use/eyes/views.py or a new tiny module) and a server-level safety net. ` +
  `Follow CLAUDE.md style (tabs, modern typing, pydantic v2, ActionResult-like structured text, tests in tests/ci with real browsers ` +
  `and pytest-httpserver, NO mocks). Give: (1) the design, (2) exact files/functions to change with what to change, (3) failing tests ` +
  `to write first (names, what page/fixture they serve, what they assert, and how to make them fast — e.g. inject a small deadline ` +
  `rather than waiting 50 s), (4) docs to update (AI.md, .claude/skills/retinat/SKILL.md, tool descriptions), (5) risks.`,
  { label: 'design', phase: 'Design' },
)

phase('Critique')
const critiques = await parallel(['correctness (races, cancellation, partial state, input left held, recorder)', 'testability and speed (can the tests be deterministic and fast with real browsers, no mocks?)', 'user-facing honesty (does the partial result say exactly what was and was not covered, and how to resume?)'].map(lens => () => agent(
  `${CONTEXT}\n\nCritique this design through the lens of ${lens}. Be concrete, cite file:line, and propose fixes. Default to skeptical.\n\nDESIGN:\n${design}`,
  { label: `critique:${lens.split(' ')[0]}`, phase: 'Critique' },
)))

return { maps: good, design, critiques: critiques.filter(Boolean) }
