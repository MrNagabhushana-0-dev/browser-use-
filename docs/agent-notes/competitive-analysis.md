# Competitive analysis: webcmd (agentrhq/webcmd)

Read-only investigation, per explicit instruction: understand it, learn from
its architecture, **never copy code from it into this repo**. Nothing below
is code lifted from that project — it's my own description of concepts,
for our own independent implementation to draw on if a future round
decides one is worth building.

## What it actually is (not slop — verified by reading the code, not the README)

`agentrhq/webcmd` (npm `@agentrhq/webcmd`, Apache-2.0, v0.8.4) is a real,
maintained TypeScript project: real `.test.ts` files throughout `src/`,
a 66KB `CHANGELOG.md`, `CONTRIBUTING.md`/`SECURITY.md`/release-please
automation, a published npm package, and a Python benchmark harness with
SHA256-pinned dataset hashes for reproducibility. This is not the
"README looks great, code is AI slop" pattern the instruction warned about —
the code backs the claims.

**Dozens of other GitHub accounts** (`techy-ops/webcmd`, `Rachit-Tiwari-7/webcmd`,
`Darshil1532/webcmd`, and more) carry the identical name and description.
Checked one directly: `techy-ops/webcmd`'s README and package.json are
byte-identical to the original. These are mirrors/mass-copies, not
independent forks with their own content — `agentrhq/webcmd` is the one
artifact actually worth analyzing.

## It benchmarks itself directly against browser-use, using our own benchmark

`README.md` cites `BU Bench V1` — **browser-use's own published benchmark**
(`github.com/browser-use/benchmark`) — and claims to beat browser-use on
accuracy (67% vs 66%), cost per completed task ($0.255 vs $0.297), and
agent turns per task (9.8 vs 14.8), with Playwright CLI and agent-browser
further behind on all three. The methodology, per their own writeup,
controls for the same Pi controller, controller model, and judge model
(Codex gpt-5.4) across all tools compared — a real effort at a fair
comparison, not a rigged one.

**Honest caveat, stated plainly**: these are numbers self-reported by a
project with an obvious incentive to look good in them. A real methodology
doesn't make the specific numbers ground truth — it makes them worth
independently reproducing before being treated as fact. Nobody has
reproduced this from the browser-use side yet. That reproduction (running
BU Bench V1 against current browser-use and against webcmd ourselves) is
the concrete, falsifiable next step, not just reading their writeup and
reacting to it.

## Concrete techniques worth studying (concepts, not code, per the constraint)

1. **Priority-ordered, budget-aware snapshot pruning.** Instead of a full
   DOM/AX snapshot every step, keep a character budget and fill it in
   priority order: focused/invalid fields and alerts first, then
   actionable controls, then repeated records (breadth-first across list
   items so the agent sees every result before deep detail on the first
   few), then named sections, then low-value text. Omitted content leaves
   a recoverable `[more ref=...]` marker instead of silently vanishing.
   This is a more deliberate prioritization scheme than what this repo's
   own DOM/vision work has built so far.
2. **Task-aware structural diffs instead of re-snapshotting.** After a
   `browser run` program executes, compare before/after state and return
   the diff (changed values, validation messages, new controls) rather
   than a fresh full snapshot — with an explicit off-switch for tasks
   where a diff isn't the useful signal (their example: open-ended
   research tasks, where the agent's real need is a targeted quote, not
   a structural delta).
3. **Batched code-based execution reduces LLM round trips.** `browser run`
   lets the agent submit one small program executing several browser
   steps in a single turn, which is the stated reason for its lower
   agent-turns-per-task number. This repo already has a real
   code-execution-over-page action (per CLAUDE.md's own history) — worth
   checking whether it's used as aggressively as this number implies
   ours could be.
4. **A broader "site memory" than a tool manifest.** Their sitemap layer
   claims to record not just available actions but "pitfalls and fallback
   paths" observed during real runs — a superset of what
   `browser_use/synthesis`'s per-origin manifest cache currently stores.
   Worth a concrete comparison against `SiteToolSynthesizer`/`ManifestStore`
   once someone reads both closely enough to say precisely what's missing.

## How this feeds the innovation track

Logged as grounded candidates in `ideas-backlog.json` (status: `proposed`,
not implemented) rather than acted on directly. The bar for any of these:
a falsifiable before/after on our own scenario suite or a reproduction of
the relevant slice of BU Bench V1 — not "webcmd's README says this works."
Matching a capability this project already has and benchmarks well is the
floor, not a win; the bar for calling something a breakthrough is
demonstrably exceeding it, with evidence, the same standard the rest of
this ledger already holds fixes to.

No code, comments, strings, or structure were copied from webcmd into this
repo. The two shallow clones used for this read (`techy-ops/webcmd`,
`agentrhq/webcmd`) live outside this repository's working tree and are not
part of any commit here.
