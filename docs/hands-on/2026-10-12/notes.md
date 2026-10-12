# Hands-on, 12 October 2026: Wikipedia and Wikimedia Commons through the Retinat tools

**Recording:** `wikipedia-and-commons.mp4`, 79 s at 5 fps. It shows the browser the Retinat MCP tools drove. The
recorder was a read-only screencast attached to that browser; every action was a Retinat tool call. No scripts and
no Playwright.

## What was tried

1. `retinat_open` on https://en.wikipedia.org/wiki/Computer_vision from the cloud sandbox.
2. `retinat_look`, then `retinat_find "Machine vision"`.
3. `retinat_open` on the Commons file page of *Big Buck Bunny* (CC BY 3.0, Blender Foundation).
4. `retinat_look`, `retinat_find "Play"`, `retinat_zoom` (the whole viewport), `retinat_click` on the play button, and
   `retinat_watch` for 15 s.

## What the eyes saw, and the bugs it showed

| # | Found | Status |
|---|---|---|
| 1 | Every HTTPS page failed with `ERR_CERT_AUTHORITY_INVALID`: the sandbox's proxy re-signs TLS and Chromium reads none of the CA variables. | **Fixed** (`39d5d1f`). The CA is learned from the refused chain, but only if the environment's bundle trusts it and it verifiably signed the chain. The first `retinat_open` then succeeds in one call. |
| 2 | The film played, yet `retinat_watch` said "no video was on screen". The Commons player streams from another origin without CORS, so the page cannot read its pixels. | **Fixed** (`20f32d0`). Pictures now come from the compositor; the sound is said to be unreadable. |
| 3 | `retinat_look` returned no image on a page with a paused `<video>`: it went into watch mode and found nothing playing. | Open. |
| 4 | "Text that appeared" reported the page's own text as it loaded, and the player's ticking clock every second. | Open. |
| 5 | Stopping a Retinat MCP server left its Chromium running (an orphan, its parent PID 1). | Open. |
| 6 | `retinat_find` cannot find icon-only controls such as the player's play button, though its accessible name was "Play video". | Open. |
| 7 | The video recorder wrote one frame per repaint, so 3 s became 0.2 s and almost black. | **Fixed** (`d1f027d`), with `retinat_record` added. |

**Cost, honestly:** one `retinat_zoom` of the whole viewport cost about 2,700 tokens, when a look costs about 330.
The way forward is the event stream and reflexes, not bigger pictures.
