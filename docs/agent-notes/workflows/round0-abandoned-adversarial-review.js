export const meta = {
  name: 'harden-merged-code',
  description: 'Adversarial review of the merged browser-use additions; verify each finding before reporting',
  phases: [
    { title: 'Review', detail: 'one finder per module group, hunting real correctness/security bugs' },
    { title: 'Verify', detail: 'adversarially refute each finding; keep only survivors' },
  ],
}

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          line: { type: 'integer' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          category: { type: 'string' },
          summary: { type: 'string' },
          failure_scenario: { type: 'string' },
        },
        required: ['file', 'severity', 'summary', 'failure_scenario'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    isReal: { type: 'boolean' },
    corrected_severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
    reasoning: { type: 'string' },
  },
  required: ['isReal', 'reasoning'],
}

const COMMON =
  'You are reviewing recently-added code in /home/user/browser-use- (a Python 3.11+ CDP browser library; tabs for indent). ' +
  'Read the named files in full with your tools before judging. Report ONLY real, reproducible correctness, security, ' +
  'resource-leak, or async defects with a concrete failure scenario (inputs/state -> wrong result or crash). ' +
  'Do NOT report style, naming, missing-docstring, or speculative nits. If a file is clean, return an empty findings array. ' +
  'Cite file path and line for each finding.'

const DIMENSIONS = [
  { key: 'synthesis', prompt: COMMON + ' Focus: browser_use/synthesis/service.py, scanner.py, store.py, views.py. ' +
    'Hunt especially for JS-injection through page-controlled text spliced into scripts, locator resolution errors, ' +
    'cache/fingerprint keying bugs, and unbounded growth.' },
  { key: 'vision-label', prompt: COMMON + ' Focus: browser_use/vision/label.py and perceive.py. ' +
    'Hunt for off-by-one/out-of-bounds in the grid math, wrong region clamping, decode-failure paths, and division by zero.' },
  { key: 'vision-stream', prompt: COMMON + ' Focus: browser_use/vision/stream.py and live.py. ' +
    'Hunt for async/task leaks, the CDP screencast event-registry coexistence with the recorder, object-identity/tracking ' +
    'bugs, and frames captured or labelled from the wrong session.' },
  { key: 'decide', prompt: COMMON + ' Focus: browser_use/decide/service.py, page.py, views.py. ' +
    'The stated contract is that every failure path (no key, timeout, bad body, low confidence) returns nothing and never ' +
    'raises, and a malformed answer goes missing rather than defaulting. Hunt for any path that violates that, plus parsing/validation bugs.' },
  { key: 'cobrowse', prompt: COMMON + ' Focus: browser_use/cobrowse/service.py, control.py, __main__.py. ' +
    'Hunt for secret/cookie-value leakage (only names should ever be exposed), control-lock bypass, the profile-lock ' +
    'stale-file handling, proxy-CA argv handling, and the free-port TOCTOU.' },
  { key: 'human', prompt: COMMON + ' Focus: browser_use/human/input.py and motion.py. ' +
    'Hunt for wrong virtual-key-code / keypress emission, hold() auto-repeat timing or task leaks, bezier/landing-point ' +
    'out-of-bounds, and the keyUp payload.' },
  { key: 'webmcp-profile', prompt: COMMON + ' Focus: browser_use/webmcp/service.py, bridge.py, views.py and browser/profile.py. ' +
    'Hunt for JS-injection or prompt-injection through page-declared tool metadata/schema, wrong origin keying when the ' +
    'bridge is off, unbounded discovery, and correctness bugs in the headless user-agent or window-size fingerprint flags.' },
]

function findStage(d) {
  return agent(d.prompt, { label: 'review:' + d.key, phase: 'Review', schema: FINDINGS_SCHEMA })
}

function verifyStage(review, d) {
  const findings = (review && review.findings) || []
  const thunks = findings.map(function (f) {
    return function () {
      const prompt =
        'Adversarially verify this reported defect. Read the actual code at the cited location and try hard to REFUTE it. ' +
        'A finding is real ONLY if you can confirm the failure scenario actually occurs in the current code. ' +
        'Default to isReal=false if you cannot reproduce it from the code.\n\n' +
        'File: ' + f.file + (f.line ? ':' + f.line : '') + '\nSeverity: ' + f.severity +
        '\nCategory: ' + (f.category || 'n/a') + '\nClaim: ' + f.summary + '\nFailure scenario: ' + f.failure_scenario
      return agent(prompt, {
        label: 'verify:' + d.key + ':' + f.file.split('/').pop(),
        phase: 'Verify',
        effort: 'high',
        schema: VERDICT_SCHEMA,
      }).then(function (v) {
        return v ? Object.assign({}, f, { verdict: v }) : null
      })
    }
  })
  return parallel(thunks)
}

const results = await pipeline(DIMENSIONS, findStage, verifyStage)

const confirmed = results
  .flat()
  .filter(Boolean)
  .filter(function (f) {
    return f.verdict && f.verdict.isReal
  })
  .map(function (f) {
    return {
      file: f.file,
      line: f.line,
      severity: (f.verdict && f.verdict.corrected_severity) || f.severity,
      category: f.category,
      summary: f.summary,
      failure_scenario: f.failure_scenario,
      why_real: f.verdict.reasoning,
    }
  })

const order = { critical: 0, high: 1, medium: 2, low: 3 }
confirmed.sort(function (a, b) {
  return (order[a.severity] === undefined ? 9 : order[a.severity]) - (order[b.severity] === undefined ? 9 : order[b.severity])
})

log('Confirmed ' + confirmed.length + ' finding(s) after adversarial verification.')
return { confirmed: confirmed, total_raw: results.flat().filter(Boolean).length }