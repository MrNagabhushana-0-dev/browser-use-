# Model performance notes

Purpose: when a worker underperforms (finds nothing real, weak self-critique,
verification claims that don't reproduce), swap that assignment to a
different model tier next round instead of repeating the same pairing.
Real constraint: the Workflow tool runs each agent() call to completion —
there's no live mid-task swap, only a between-rounds decision.

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
