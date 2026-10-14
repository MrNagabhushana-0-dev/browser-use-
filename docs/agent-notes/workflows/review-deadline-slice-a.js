export const meta = {
  name: 'review-deadline-slice-a',
  description: 'Adversarially review commit bdd1d87 (Retinat call budget + scan deadline), read-only, verify each finding',
  phases: [{ title: 'Review' }, { title: 'Verify' }],
}

const COMMON = `You are reviewing git commit bdd1d87 in /home/user/browser-use- (run \`git show bdd1d87\` to see it). It adds a per-call Deadline to the Retinat MCP server (browser_use/retinat/server.py call_tool + _call_retinat), a deadline-aware PageWatcher.scan (browser_use/eyes/page.py), Eyes.scan text/resume (browser_use/eyes/service.py), Deadline/stop_line (browser_use/eyes/views.py), LiveView.clock (browser_use/vision/live.py), and tests in tests/ci/test_eyes.py and tests/ci/test_retinat.py.
STRICT RULES: read-only. Do NOT run pytest, do NOT launch a browser or Chrome, do NOT edit files. Another timing-sensitive test run is in progress on this machine. You may read files, grep, and run tiny pure-python snippets that import nothing heavy.
Already known and planned (do not report these again): retinat_wait not clamped to the deadline; the backstop error text says "it may have scrolled or moved the page" regardless of the effect; scan does not raise Eyes._watching so the archiver competes; collect() does not capture when no frame lands near the current scroll position; the final scrollTo restore gets dl.clamp(2.0) which can be ~0.05 s; Deadline lives in eyes/views.py which pulls numpy (only matters for mcp/server.py later); browse/watch/explore are not deadline-aware yet.
Report only concrete defects in THIS diff with file:line, a failure scenario (inputs/state -> wrong output), and a minimal fix. Severity high/medium/low. If you find nothing real, return an empty list. No style nits.`

const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' }, line: { type: 'integer' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          title: { type: 'string' }, scenario: { type: 'string' }, fix: { type: 'string' },
        },
        required: ['file', 'line', 'severity', 'title', 'scenario', 'fix'],
      },
    },
  },
  required: ['findings'],
}

const LENSES = [
  { key: 'timing', prompt: 'Lens: timing and cancellation correctness. Does every path of PageWatcher.scan and Eyes.scan finish before deadline + a small overrun? Are estimates (step_est, tail_est, the 0.002*len(captured) term) sound, including the first iteration and slow pages (1-3 fps WebGL)? Does asyncio.timeout in call_tool interact badly with TimeoutError raised from inside the tool (e.g. _eval timeouts) or with the existing except/finally, self._waits bookkeeping, effects.begin/end? Python is 3.12.' },
  { key: 'honesty', prompt: 'Lens: honesty of the result. Does the scan text/resume ever claim coverage not seen, give a resume (from_screen, max_screens, keyframes) that skips content, overlaps wrongly, violates the tool schema minimums/maximums, or say "out of time" when the cause was something else (or vice versa)? Check next_y, the covered string, floor-to-0.1 rounding of from_screen against frames, the stop_reason mapping, stuck vs time, a from_screen beyond the page end, and what an agent reading the text would do next.' },
  { key: 'tests', prompt: 'Lens: test validity. For each new test in tests/ci/test_eyes.py (test_a_scan_shows_nothing_from_before_it_began, test_a_scan_stopped_by_max_screens_says_what_it_did_not_see, test_a_scan_out_of_time_says_how_far_it_got_and_where_to_go_on, test_scans_cut_short_by_time_go_on_from_where_they_stopped_without_a_gap) and tests/ci/test_retinat.py (retinat_short fixture, test_a_long_scan_through_mcp_answers_within_the_call_budget, test_every_tool_that_can_run_long_is_under_the_call_budget, test_a_tool_that_runs_past_its_budget_is_stopped_and_says_so): would it fail if the feature were broken (name the mutation it misses, if any)? Is it load-sensitive in a way likely to flake in a 39-minute full CI run on a busy 4-core box? Does it leak state (browser sessions, monkeypatch, LONG_TOOLS mutation) into other tests?' },
]

phase('Review')
const reviews = await parallel(LENSES.map(l => () =>
  agent(`${COMMON}\n\n${l.prompt}`, { label: `review:${l.key}`, phase: 'Review', schema: FINDINGS })
    .then(r => (r ? r.findings.map(f => ({ ...f, lens: l.key })) : []))))
const all = reviews.filter(Boolean).flat()
const seen = new Set()
const deduped = all.filter(f => { const k = `${f.file}:${Math.round(f.line / 5)}:${f.title.slice(0, 30)}`; if (seen.has(k)) return false; seen.add(k); return true })
const rank = { high: 0, medium: 1, low: 2 }
deduped.sort((a, b) => rank[a.severity] - rank[b.severity])
const toVerify = deduped.slice(0, 6)
if (deduped.length > 6) log(`verifying the top 6 of ${deduped.length}; the rest are returned unverified`)

phase('Verify')
const VERDICT = {
  type: 'object',
  properties: { real: { type: 'boolean' }, reasoning: { type: 'string' }, better_fix: { type: 'string' } },
  required: ['real', 'reasoning'],
}
const verified = await parallel(toVerify.map(f => () =>
  agent(`${COMMON}\n\nTry to REFUTE this claimed defect by reading the actual code paths. Default to real=false if the scenario cannot actually happen as described.\n\n${JSON.stringify(f, null, 2)}`,
    { label: `verify:${f.lens}:${f.file.split('/').pop()}:${f.line}`, phase: 'Verify', schema: VERDICT })
    .then(v => ({ ...f, verdict: v }))))
return { confirmed: verified.filter(Boolean).filter(f => f.verdict && f.verdict.real), refuted: verified.filter(Boolean).filter(f => !(f.verdict && f.verdict.real)).map(f => ({ title: f.title, why: f.verdict && f.verdict.reasoning })), unverified: deduped.slice(6) }
