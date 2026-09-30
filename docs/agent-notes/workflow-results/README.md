# Workflow results (raw agent outputs)

Everything in this folder was rescued from a sandbox that was about to be
permanently deleted. It is the **raw record of what the worker agents
actually returned** — as opposed to `../ledger.json` and
`../performance-registry.json`, which are my summaries of it. If a summary
and a file here ever disagree, this folder is the source of truth.

## Files

| Run | Journal (`*.journal.jsonl`) | Final result (`*.result.json`) |
|---|---|---|
| Round 0, first attempt (stopped by the owner) | yes | none (never completed) |
| Round 0, lead-driven fleet | yes | yes |
| Round 1 — agent/session/tools/dom | yes | yes |
| Round 2 — security/mcp/downloads/llm-schema | yes | yes |
| Round 3 — task-centric checkpoint-gated pipeline | yes | yes |

- **Journal**: one JSON object per line — `started` / `result` / `failed`
  events in order. Each `result` line carries that agent's *complete*
  return value (claims, refuter verdicts, implementation reports with the
  exact commands and output it ran). This is where the evaluator
  pass/weak/poor verdicts and the refuters' reasoning live in full.
- **Final result**: the aggregated value the workflow script returned,
  including per-agent token counts and durations.
- The scripts that produced these are in `../workflows/`.

## `rejected-round0-patches/` — the one irreplaceable artifact

Round 0's Lead rejected three of nine worker patches. Those were the only
copies anywhere (the worktrees that held them were deleted), so they are
extracted here as plain `git apply`-able patches:

| Patch | Defect | Why the Lead rejected it (from `round0-lead-driven-fleet.result.json`) |
|---|---|---|
| `FIX-2.patch` | `synthesize()` never reads the scanner's loose `controls`, so a form-less search box never becomes a tool | Real bug and correct core fix, but the cache fingerprint ignores loose controls, so a later page with a search box can return a stale empty manifest. Needs the controls folded into `fingerprint()` and a regression test for exactly that cache case. Also: the `driven` dedup block's test "cannot fail". |
| `FIX-3.patch` | Synthesis cache key drifts with scroll position | **Blocking.** The new `shape` payload is uncapped (up to ~220k chars worst case) against a 60,000-char scan limit; when the scan truncates, synthesis returns `{}` — so the patch turns "fingerprint drifts with scroll" into "a crowded page gets zero tools". Needs `shape` collapsed to a small fixed-size digest inside the page script. **Do not apply as-is.** |
| `FIX-6.patch` | `launch_for_human` leaks the Chrome process and its profile lock when the debug port never comes up | The Lead's review says **"No changes needed in `browser_use/cobrowse/service.py`"** — the source fix is sound. What failed review was the *test*: test 2 wasn't a real regression test. It needs a fake browser that ignores SIGTERM (`trap '' TERM; exec sleep 30`) so the old `close()` provably leaves a zombie. This one is close to landable. |

Verified at the time of writing: all three pass `git apply --check` against
a fresh checkout of `main` at `933e53f`. That is **only** an apply check — it
says nothing about lint, types, or tests, and "applies cleanly" is not
"safe to merge" (see FIX-3).

## What was deliberately NOT preserved, and why

- **The full session transcript (~27 MB).** It contains the complete system
  prompt and account details for the person running the session; committing
  that to a repo is publishing it. The work product it would show is already
  captured, structured, in the files above and in `../`.
- **Per-agent raw logs (~29 MB of JSONL).** Redundant with the journals,
  which already hold each agent's final return value.
- **The session scratchpad (~800 MB).** Probe scripts with hardcoded sandbox
  paths, benchmark logs from earlier in the project that I can't vouch for
  without re-reading, virtualenvs, and **browser profile directories**
  (`human-profile`, `persist-profile`). Browser profiles can contain cookies
  and login state and must never be committed; I excluded the whole folder
  rather than sort the dangerous parts from the rest under time pressure.
- **Empty `worktree-*` branch refs.** They point at the base commit; the
  work they once held is in the journals.
