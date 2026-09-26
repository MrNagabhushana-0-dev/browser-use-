# Research & Publication Materials

Honest framing before anything else: **none of the work in `docs/agent-notes/`
is a patentable invention**, and this index is written on that basis, not
around it. Every landed fix corrects behavior against its own already-stated
docstring or contract — that is the legal and technical definition of "not
novel." The one artifact here with any real intellectual content is the
*methodology* (item 1) — a multi-agent verification pipeline — and even that
sits inside an active, well-populated research area (LLM debate, self-refine,
constitutional AI, multi-agent verification), so it is written up as an
engineering case study, not a claim of priority.

## What's here

1. `01-multi-agent-checkpoint-verification.md` — a technical report on the
   task-centric, checkpoint-gated verification pipeline used across this
   effort: the one thing here worth writing up for an external audience.
2. `02-security-advisory-domain-allowlist-bypass.md` — a proper
   security-advisory-style writeup (CWE-697/CWE-284) of the round-2 domain
   allowlist bypass. This is a legitimate, standard artifact for a real
   defect — advisories get published for exactly this class of bug.
3. `03-technical-case-studies.md` — three shorter case studies (XPath/JS
   injection in `scroll_to_text`, a CDP remote-object lifecycle leak, a
   focus-hijack race in dead-code crash recovery) written for an engineering
   blog or internal wiki, not a claim of novelty.
4. `PUBLICATION_AND_PATENT_GUIDANCE.md` — where each of the above could
   honestly go (venue by venue, with real links and real submission steps),
   why patenting is not a live option for any of this today, what would need
   to be true for that to change, and the one thing that matters most if it
   ever does: **file before you disclose, not after.**

## The one instruction I did not follow literally

You asked for this committed and pushed to a public PR. I did that for 1-3
(they're meant to be public — that's the whole point of a technical
report or an advisory). I did **not** treat that as clearance to draft
"ready to submit" patent applications for material that (a) isn't novel and
(b) is about to be publicly disclosed by this very commit, which is the
single most concrete way to guarantee it stays unpatentable. See document 4
for the honest version of that ask.
