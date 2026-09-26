# Model performance notes

This is a human-readable summary **derived from `performance-registry.json`**
— that file is the structured source of truth (per-call tokens, duration,
verified outcome, failure mode); this file is prose written from it, not the
other way around.

Purpose: when a worker underperforms (finds nothing real, weak self-critique,
verification claims that don't reproduce), swap that assignment to a
different model tier — at the next pipeline stage checkpoint (hunt → refute
→ implement → verify), not mid-call. Real constraint, confirmed by an
external advisor consultation on 2026-09-26: the Workflow tool runs each
agent() call to completion — there is no live mid-task swap. Checkpoint-based
reassignment between stages is the actual mechanism; see
`README.md` section C.

**Round 3, the first real cross-model data point.** The task-centric,
checkpoint-gated pipeline (hunt -> evaluator -> refute -> evaluator ->
implement) ran with real model diversity for the first time: Sonnet 5
hunts, Opus 5 refutes and implements, Sonnet 5 evaluates at both
checkpoints. All 3 targets passed every evaluator gate on the first
attempt (verdict=pass, 0 weak retries, 0 poor reassignments) — meaning
this round doesn't yet have failure-path data to compare models on, only
a clean run. What it does show: the refute stage (a different model,
independently reproducing) caught something real in every single case —
corrected the hunter's predicted mechanism on the ScrollToTextEvent bug
(Chrome degrades malformed XPath rather than raising), caught that the
crash_watchdog bug is worse than described (an actual focus hijack via
`focus=True`, not just mis-logging) and that the module is currently dead
code (downgrading its impact score), and found the identical truncation
gap in the Anthropic and Google providers while correctly keeping the
llm/openai/chat.py fix scoped to its claim. Self-critique-in-one-call
never produced findings of this caliber in rounds 1-2; a separate model
with the explicit job of trying to kill the claim did, three times in a
row. This is the strongest evidence yet for the restructure, not just an
argument for it.

**Hermes / OpenClaw, checked 2026-09-26**: no API keys or equivalent
credentials for either exist in this environment (`env | grep`, checked
directly), no installed binary or config references either name anywhere
reachable from this session, and outbound network is allowlisted to a fixed
set of domains (npm, pypi, github, the Anthropic API, cargo, go proxy, jsr)
that doesn't include either. This was investigated, not assumed — see
`README.md` section G.

Available tiers: Opus 5.5 (`claude-opus-5-5`), Opus 5 (`claude-opus-5`),
Sonnet 5 (`claude-sonnet-5`), Haiku 4.5 (`claude-haiku-4-5-20251001`), Fable
5.1 (`claude-fable-5-1`). No non-Claude framework is reachable from here
(no Hermes/OpenClaw API access or hosting).

## Round 1 (harden-core-modules) — all 4 workers on Sonnet 5 (session default)

| Worker | Assignment | Outcome |
|---|---|---|
| harden:agent | agent/service.py | Real, correct, minimal. Also caught and dropped a false lead (a test-harness caplog bug) before committing. |
| harden:session | browser/session.py | Real, correct. Self-critique explicitly named and set aside two weaker candidates (an unreachable AttributeError path, an unreproducible race) rather than forcing them. |
| harden:tools | tools/service.py | Real defect, correct fix, but the guard needed my own extra stress-testing to fully trust (see findings-log — I initially "improved" it wrong, then reverted to the worker's original). Worker's own version was already right; my second-guessing was the actual risk here, not the worker's work. |
| harden:dom | dom/service.py | Real, correct, best-verified of the four (empirically proved the leak by trying to double-release the same CDP handle). Highest real-world impact of the round. |

No swap needed — all four produced real, landable fixes on the first pass.

## Round 2 (harden-core-modules-round2) — first attempt

All 4 workers (security_watchdog, mcp/client, downloads_watchdog, llm/schema)
failed identically: `session limit · resets 7:50pm (UTC)`. Not a quality
signal — a shared account-level usage ceiling hit at the same moment across
all 4 concurrent agents. Resumed after the reset rather than swapped model
tier, since the failure had nothing to do with model choice.

(Updated once round 2 actually produces results.)
