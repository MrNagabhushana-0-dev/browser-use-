# Ideas backlog

Canonical data lives in `ideas-backlog.json` — this file is just the pointer
and the rule: **never discard an entry for being "not immediately useful."**
Park it with a `status_reason` instead of deleting it. A parked idea can be
revisited once its blocking reason no longer applies (a dependency lands, a
scenario in the failure suite starts exercising it, a cheaper approach
appears).

Nothing is implemented from a proposal alone. It moves:
`proposed → prototyped → benchmarked → accepted | rejected | parked`,
and `accepted` still requires owner approval before it merges into default
behavior — cooking on a branch is cheap; landing it is a decision.

Entries live in the JSON; as of this write-up there are six: four proposals derived
from reading a competitor's public writeup, and two built-and-tested prototypes
(`video-seek-bisect-contact-sheet` and `vertical-scroll-from-pixels`, both status
`prototyped`, each with an explicit not-novel-as-individual-techniques assessment and the
benchmarks still owed).
