# Agent Notes & State

Persistent, structured state for the multi-agent hardening/innovation effort on
this repo — external to any model's context window, so it survives a session
ending, a model switch, or a workflow resume. This file answers, directly, the
architecture questions raised by two advisor consultations (an in-session
Opus 5.5 stand-in for Fable, and an external GPT consultation the owner ran
separately). Where a recommendation assumed a capability this environment
doesn't have, that's stated plainly below rather than quietly dropped.

## A. The control loop — task-centric, not round-centric

Corrected after review: rounds 1–2 dispatched their 4 targets with plain JS
`Promise.all()` — a hard barrier, all 4 or nothing, and a fresh `Workflow`
invocation per "round" with a wait in between. That's batch-oriented and it
was a real mistake, not just phrasing: this tool already has `pipeline()`,
which runs each item through stages with **no barrier between stages** —
item A can be at VERIFY while item E is still at HUNT. The unit of
execution is the task, not the round:

```
TASK #n
  ├─ HUNT        (claim only, no code — cheap model, read-only)
  ├─ MERGE+DEDUPE (against ledger.json — drop anything already settled)
  ├─ CHECKPOINT → EVALUATOR (a DIFFERENT model judges the claim)
  │     ├─ pass  → continue to REFUTE
  │     ├─ weak  → retry HUNT once, same model, with the evaluator's
  │     │          correction appended to the prompt
  │     └─ poor  → retire this attempt, retry HUNT once on a DIFFERENT
  │                model tier, seeded from the same checkpoint (the merged/
  │                deduped claim state) — not from scratch
  ├─ REFUTE      (tries to make the claim's test fail on base code; claim
  │               dies here if it can't reproduce or is intended behavior —
  │               the real adversarial stage, not self-critique)
  ├─ CHECKPOINT → EVALUATOR (same pass/weak/poor gate as above, applied to
  │               the refuter's verdict — a weak refutation gets retried
  │               before a claim is allowed to either die or survive on a
  │               shaky call)
  ├─ IMPLEMENT   (survivors only; stronger model; isolated worktree seeded
  │               from the refuter's failing-test branch; cannot edit that
  │               test file)
  ├─ VERIFY      (mechanical: test fails-then-passes, ruff/pyright/module
  │               suite, diff size, files touched — scriptable, no model
  │               judgment needed)
  ├─ CHECKPOINT → EVALUATOR (verify failed? retry IMPLEMENT once on a
  │               different model, seeded from the verifier's failure
  │               output, before giving up on the task)
  ├─ REGRESSION REVIEW (only when the diff touches a hot path, a
  │               timeout/constant, or exceeds ~40 lines — this is what
  │               would have caught the round-0 fingerprint fix that was
  │               worse than its own bug)
  └─ orchestrator commits + appends to ledger.json + performance-registry.json

TASK #n+1  ─── starts its own HUNT while #n is still mid-VERIFY ───
```

**The checkpoint-evaluator gate is now mandatory at every stage boundary**,
not optional or "when I feel like it." Concretely: `pipeline()`'s
per-stage callback receives the previous stage's result, so "retry same
model" vs. "retire and reassign to a different model" is a plain `if` on
the evaluator's verdict inside that callback — not a new Workflow
invocation, and not a live interrupt of a running call (still not possible;
see C). This is the honest version of "swap underperformers": mandatory,
automatic, and scoped to the actual checkpoint the tool provides.

**What this is still not**: a standing daemon. A `Workflow` invocation is
one script execution; "task-centric" means tasks don't wait for a round
boundary *within* that execution, not that there's a process running
between sessions. New tasks get added to the pipeline's input list at the
start of an invocation (informed by the failure-scenario suite + ledger),
not created ad hoc by a task that's already running.

## B. State / checkpoint design

- `ledger.json` — one entry per proposed finding, keyed
  `file:line_range:mechanism`. Status: `landed | rejected | in_progress`.
  Frozen and injected verbatim into every hunter prompt so a later round
  can't re-propose something already settled. Going forward, entries may
  carry an `impact` score and an `evidence` block (see D below) instead of
  a prose-only assessment.
- `performance-registry.json` — one entry per agent call: model, stage,
  task key, tokens, duration, verified outcome, failure mode. This is the
  actual dataset `model-performance.md` should be *derived from*, not
  written from memory.
- `ideas-backlog.md` — proposals that aren't bug fixes. Status-tracked
  the same way as the ledger: `proposed → prototyped → benchmarked →
  accepted | rejected | parked`. Nothing here is implemented from the
  proposal alone.
- `findings-log.md` — human-readable narrative, one entry per round. This
  one stays prose — it's for a person reading back through history, not
  for a machine.
- **Checkpoint = a git commit + a ledger/registry append.** There is no
  separate checkpoint file format beyond what's already durable (git
  history, the JSON state here). A resumed workflow's real checkpoint is
  "which agent() calls already completed with this exact (prompt, opts)" —
  that's the tool's own resume cache, and it only holds for calls that
  finished; an errored call has nothing to resume from, by construction.

## C. Agent replacement mechanism — what's real vs. what isn't

**Not real, and I won't pretend otherwise**: a live, mid-execution swap of
the model underneath an already-running agent call. The tool available here
runs one prompt to completion on one model; there is no checkpoint inside
that execution to hand off from.

**What is real and is now mandatory, not optional**: every stage transition
in section A is gated by an evaluator verdict —
`pass → continue | weak → retry same model with correction | poor → retire,
reassign to a different model, reseed from the same checkpoint`. This
"same checkpoint" is the literal previous stage's structured output
(the merged claim, the refuter's branch, the verifier's failure log) —
never a restart from nothing. It's checkpoint-based reassignment at the
granularity the tool actually supports: real, automatic, and it's the
correct read of "swap the agent, don't wait for a whole round" once "live
mid-call interrupt" is off the table.

## D. Model-performance schema — agent economics, not just per-call cost

Per `performance-registry.json` entry:
```
{
  "task_key": "matches a ledger.json key, or a round/stage id for non-bug work",
  "model": "claude-sonnet-5 | claude-opus-5 | claude-opus-5-5 | claude-haiku-4-5-... | claude-fable-5-1",
  "stage": "hunt | refute | implement | verify | review | advisor",
  "tokens": <int>,
  "duration_ms": <int>,
  "verified_outcome": "landed | refuted | held | error",
  "failure_mode": "<string or null>",
  "first_pass_success": <bool — landed without a retry-with-correction>,
  "recovery_success": <bool|null — if it needed a retry, did the retry land>,
  "tool_failures": <int>,
  "critic_rejection": <bool — did REFUTE kill this before implementation was attempted>,
  "regression_detected": <bool>,
  "evaluator_score": <0-5 or null, see impact scale below>
}
```

The economics metrics this is meant to feed, aggregated across entries in
`model-performance.md` (not per-task, and not yet — see caveat below):
verified success rate, first-pass success rate, recovery success rate,
time/task, tokens/task, tool-failure rate, critic rejection rate, accepted
innovations, regression rate, cost per verified improvement.

**Honesty caveat, stated explicitly rather than glossed over**: the eventual
goal is a router that assigns model tiers by task type from measured
results — "coding/debugging → model X, architecture → model Y, cheap
exploration → model A" — instead of assuming the most expensive model is
always best. That requires enough data per (task-type, model) pair for the
difference to mean anything. As of this write-up the registry has a
handful of entries, mostly one model (Sonnet 5, the session default) on
one task type (single-file bug hunts). **There is no routing table yet,
and asserting one now would be exactly the kind of invented number this
whole restructure exists to stop.** The registry accumulates real entries
across rounds; a routing recommendation gets made once there's enough
volume per pairing to say something a coin flip couldn't also explain —
not before.

**Impact scale (0–5), evidence required, no invented multipliers:**
```
0 = no useful change        3 = major improvement
1 = minor improvement       4 = breakthrough candidate
2 = useful improvement      5 = transformational candidate
```
Every score above 0 must carry: `baseline`, `new_result`,
`measured_improvement`, `confidence`, `reproducible` (bool),
`independent_verification` (which separate stage/model confirmed it — never
self-reported by the same call that made the claim). A number like "20x"
is only ever reported when `measured_improvement` computes to it from a real
baseline and new result — never asserted on its own.

## E. Evaluator/critic design

Tiered, not uniform:
- Cheap model hunts and merges/dedupes (high volume, low individual cost).
- A *different* model refutes — this is the load-bearing critic step. It
  succeeds by killing bad claims, and it runs before any implementation
  token is spent, which is the real answer to "0% tokens wasted": tokens
  go on claims that already survived a runnable check, not on hunting itself.
- The strongest tier (Opus 5.5) is reserved for the regression review gate
  (hot-path/large-diff only) and for bounded advisor escalation (F below,
  triggered by the worker, not by default).

## F. Innovation pipeline

Every hunter gets a second, explicitly optional channel alongside "find one
real defect":
```
idea → evidence (grounded in a named failure mode, not "would be cool")
     → prototype (its own worktree/branch, default behavior unchanged)
     → benchmark (a concrete, falsifiable before/after)
     → adversarial test (a different model tries to break it)
     → accept | reject | park   (owner approval required to merge into default)
```
Recorded in `ideas-backlog.json` (schema: `hypothesis`,
`originating_agent`, `supporting_evidence`, `estimated_upside`,
`implementation_cost`, `dependencies`, `experiment_required`, `status`,
`status_reason`). **Rejected and parked ideas are never deleted** — only
recorded with a reason, so a parked idea can be revisited once its
blocking reason stops applying. "Let them cook" means nothing proposed is
thrown away, not that everything proposed ships.

## G. Hermes / OpenClaw — investigated, not assumed

Checked directly rather than taken on either advisor's word: no
`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/equivalent credentials for any such
service exist in this environment, and there is no installed binary,
config, or reference to a framework by either name anywhere reachable from
this session. (See the dated note appended to `model-performance.md` for
the exact check run and its output.) An external chatbot describing a
project's *feature list* is not the same as that project being installed,
authorized, and reachable from *this* sandboxed container — those are two
different claims, and only the second one determines whether it can be a
worker here. If credentials and a reachable endpoint for either ever exist
in this environment, wiring one in as an additional model tier is
mechanically straightforward (the `agent()` call's `model` parameter is
just a string); until then, this stays a documented gap, not a fabricated
integration.

## H. Files/folders maintained

```
docs/agent-notes/
├── README.md                 (this file — architecture + what's real)
├── ledger.json               (structured, machine-readable, frozen per invocation)
├── performance-registry.json (structured, per-agent-call metrics + economics)
├── ideas-backlog.json        (structured innovation proposals + status)
├── findings-log.md           (human-readable narrative, prose)
├── ideas-backlog.md          (pointer/rules for ideas-backlog.json)
└── model-performance.md      (human summary derived from performance-registry.json)
```

No parallel `agent-state/` tree — one directory, structured where the data
is genuinely structured (ledger, registry), prose where a human is the
actual reader (findings log). Splitting into eight near-duplicate folders
doesn't add real value over this if nothing reads most of them.
