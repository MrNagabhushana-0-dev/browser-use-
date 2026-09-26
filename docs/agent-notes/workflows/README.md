# Workflow scripts

The actual `Workflow` tool scripts run across this hardening effort, rescued
from the session's ephemeral project directory before container teardown —
without this, the code that produced every fix in `ledger.json` would have
been lost, leaving only the fixes themselves with no record of how they were
found. Each is the literal script passed to the `Workflow` tool; none were
edited after copying here.

| File | Round | Status | Shape |
|---|---|---|---|
| `round0-abandoned-adversarial-review.js` | 0 (first attempt) | **Stopped** by the owner before completion | Find -> adversarially verify, one finder per module group. Replaced by the lead-driven fleet below because the owner asked for the Lead/Advisor structure specifically. |
| `round0-lead-driven-fleet.js` | 0 | Completed — 5 of 9 proposed fixes landed | Opus 5.5 Lead plans, Fable Advisor critiques, Opus 5/Sonnet 5 workers implement, Lead reviews each patch. The advisor and one review call hit usage-limit errors mid-run; the Lead's own per-patch accept/reject verdicts are what actually gated what got merged. |
| `round1-harden-core-modules.js` | 1 | Completed — 4/4 landed | First self-contained "harden one file, self-critique inline, implement" worker design. `Promise.all()` across 4 targets — a round barrier, corrected in round 3. |
| `round2-harden-core-modules.js` | 2 | Completed — 4/4 landed (after a resume: the first attempt failed all 4 workers on a shared usage-limit hit) | Same shape as round 1, different targets (security/mcp-client/downloads/llm-schema). |
| `round3-task-centric-pipeline.js` | 3 | Completed — 3/3 landed | The redesigned architecture: `pipeline()` instead of `Promise.all()` (task-centric, no round barrier), a mandatory checkpoint-evaluator gate after HUNT and after REFUTE (pass/weak/poor -> continue/retry-same-model/reassign-different-model), and real model diversity (Sonnet 5 hunts, Opus 5 refutes and implements). Built in direct response to an external advisor critique that self-critique-in-one-call was "mostly theater" — every fix this round was caught or corrected by the refute stage, not by the implementer grading its own work. |

See `../README.md` for the architecture these implement (control loop,
checkpoint design, the evaluator gate) and `../model-performance.md` for the
evidence that round 3's redesign produced qualitatively better results than
rounds 1-2's self-critique approach.

## Re-running one of these

Each is runnable as-is via the `Workflow` tool (`script` or `scriptPath`
input) against a fresh checkout of this repo. They assume: a `.venv` with
`uv sync` already run, a clean git worktree, and (for rounds 1-3) that the
target files named inside each script still exist at those paths. Re-running
round 3's shape against new targets is the recommended way to continue this
effort — swap the `TARGETS` array, keep the pipeline structure.
