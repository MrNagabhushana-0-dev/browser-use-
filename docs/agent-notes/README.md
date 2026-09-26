# Agent Notes & State

Persistent, structured state for the multi-agent hardening/innovation effort on
this repo — external to any model's context window, so it survives a session
ending, a model switch, or a workflow resume. This file answers, directly, the
architecture questions raised by two advisor consultations (an in-session
Opus 5.5 stand-in for Fable, and an external GPT consultation the owner ran
separately). Where a recommendation assumed a capability this environment
doesn't have, that's stated plainly below rather than quietly dropped.

## A. The control loop (real, as actually implemented)

```
SELECT TARGETS (evidence: ledger.json + failure-scenario suite, once it exists)
        │
        ▼
   HUNT (claim only, no code — cheap model, read-only)
        │
        ▼
 MERGE + DEDUPE (against ledger.json — drop anything already landed/rejected)
        │
        ▼
  REFUTE (a DIFFERENT model tries to make the claim's test fail on base code;
          claim dies here if it can't reproduce or turns out to be intended
          behavior — this is the real adversarial stage, not self-critique)
        │
        ▼ (survivors only)
  IMPLEMENT (stronger model, isolated worktree, seeded from the refuter's
             failing-test branch — implementer cannot edit that test file)
        │
        ▼
   VERIFY (mechanical: test fails-then-passes, ruff/pyright/module suite,
           diff size, files touched — scriptable, no model judgment needed)
        │
        ▼
REGRESSION REVIEW (only when the diff touches a hot path, a timeout/constant,
                   or exceeds ~40 lines — this is what would have caught the
                   round-0 fingerprint fix that was worse than its own bug)
        │
        ▼
 ORCHESTRATOR COMMITS + APPENDS TO ledger.json + performance-registry.json
```

This is a **pipeline across targets**, not a single monolithic worker per
target — each target flows through independently (`pipeline()`), so a slow
hunt on one target doesn't stall an implementation that's already ready on
another.

**What this is not**: a live, continuously-running process. Each round is one
`Workflow` invocation. Between rounds, the orchestrating session (this one)
reads results, updates state, and decides the next round. That's the real
granularity available — there's no standing daemon here.

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

**What is real and is the actual mechanism used**: replacement happens
*between pipeline stages*, which is exactly where a checkpoint already
exists (the previous stage's structured output). If a hunter's claim comes
back low-confidence, or a refuter can't reach a verdict, or an implementer's
diff fails verification, the **next stage for that item** is assigned to a
different model — informed by `performance-registry.json`, not by "try a
different one and see." This is checkpoint-based reassignment at the
granularity the tool actually supports, not the illusion of a live swap.

## D. Model-performance schema

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
  "evaluator_score": <0-5 or null, see impact scale below>
}
```

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
Rejected and parked ideas stay recorded in `ideas-backlog.md` with the
reason — "let them cook" means nothing proposed is thrown away, not that
everything proposed ships.

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
├── ledger.json               (structured, machine-readable, frozen per round)
├── performance-registry.json (structured, per-agent-call metrics)
├── findings-log.md           (human-readable narrative, prose)
├── ideas-backlog.md          (innovation proposals + their status)
└── model-performance.md      (human summary derived from performance-registry.json)
```

No parallel `agent-state/` tree — one directory, structured where the data
is genuinely structured (ledger, registry), prose where a human is the
actual reader (findings log). Splitting into eight near-duplicate folders
doesn't add real value over this if nothing reads most of them.
