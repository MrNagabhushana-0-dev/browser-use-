# Agent Notes

Working log for the multi-agent hardening/innovation effort on this repo. Kept as
plain notes, not polished docs — a running record so nothing said or found by a
worker gets lost between sessions.

- `findings-log.md` — every real defect a worker found, whether it landed, held, or
  was rejected, and why. Chronological, most recent round on top.
- `ideas-backlog.md` — concepts workers propose beyond bug-fixing (new features,
  architectural ideas). Nothing here is implemented until it's promoted out —
  proposal only, reviewed before any code gets written from it.
- `model-performance.md` — which model handled which assignment, and how it did,
  so later rounds can swap a weak pairing for a different tier instead of
  repeating it.

## How a round works

1. A batch of workers (isolated git worktrees, disjoint files) each get one real
   module and are told to find ONE genuine defect, not invent one.
2. A second agent adversarially reviews the finding before any code is written —
   real critique, not the same agent grading its own work.
3. The worker implements + tests only if the finding survives that critique.
4. I (the orchestrating session) independently re-run everything myself before
   any commit — lint, types, the new test failing-then-passing, the existing
   suite for that module. Nothing lands on a worker's or reviewer's word alone.
5. This file set gets updated with what happened, good or bad.
