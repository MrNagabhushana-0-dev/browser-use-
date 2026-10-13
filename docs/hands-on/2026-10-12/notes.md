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
| 3 | `retinat_look` returned no image on a page with a paused `<video>`: it went into watch mode and found nothing playing. | **Fixed** (`a2d19a5`). Checked live: a 640x360 page frame. |
| 4 | "Text that appeared" reported the page's own text as it loaded, and the player's ticking clock every second. | **Fixed.** Ticking readouts (`a2d19a5`, `2885693`): one line each, noted once in the journal. The page's own text as it loads (`afea835`..`de9a228`) is now one line, "page content filled in as it loaded: N blocks". |
| 5 | Stopping a Retinat MCP server left its Chromium running (an orphan, its parent PID 1). | **Fixed** (`a2d19a5`). Checked live: 0 orphans after a SIGTERM. |
| 6 | `retinat_find` cannot find icon-only controls such as the player's play button, though its accessible name was "Play video". | **Fixed** (`a2d19a5`). Checked live: `find "play video"` gives `button named "Play video"` at (878, 404). |
| 7 | The video recorder wrote one frame per repaint, so 3 s became 0.2 s and almost black. | **Fixed** (`d1f027d`), with `retinat_record` added. |

**Cost, honestly:** one `retinat_zoom` of the whole viewport cost about 2,700 tokens, when a look costs about 330.
The way forward is the event stream and reflexes, not bigger pictures.

## Later the same day: the fixes checked live, and one more bug

**Recordings:**
- `commons-checks-before-the-source-fix.mp4`: the look, find and watch checks.
- `commons-film-seen-from-the-compositor.mp4`: the film watched through the MCP tools after the last fix.

| # | Found | Status |
|---|---|---|
| 8 | After the compositor fix, the Commons film still came back as 0 items. Running the library against the real page showed why: Video.js starts the 480p source, then switches the same `<video>` to 1080p. Taint was remembered per element, so the 1080p item was never reported and the compositor never restarted. | **Fixed** (`5bac6fd`). Taint is now re-tested for each source. |

**Through the MCP tools after the fixes:**
- `retinat_watch` for 25 s: 1 item, 1920x1080, 10:34 long, watched through the compositor.
- Its 4 keyframes are right: the title card, the pink clouds, the stream, the bird on a branch.
- About 870 tokens for 25 s of film.

## Late evening: the page's own text, the server's shutdown, and what the film is called

**Recordings:**
- `commons-page-filling-in-folded.mp4` (40 s): the file page opened, and its load-time text folded into one line.
- `commons-film-named-by-its-page.mp4` (1:56): the film started from its play button and watched.

| # | Found | Status |
|---|---|---|
| 9 | Restarting the Retinat server with SIGTERM, as Claude Code does, left it running for good. It closed its browser, then hung on the thread reading stdin, which no cancellation reaches while the client keeps the pipe open. A Tor it launched would also have outlived it. | **Fixed** (`b669d69`). Live: the server exits 0.2 s after SIGTERM, with all 9 of its browser processes. |
| 10 | The film was journalled as `now watching "Duration: 10 minutes and 35 seconds. 10:35 Subtitles available. CC"`: the player's badges and the screen-reader text behind them. | **Fixed** (`c0817da`). Live: `now watching "File:Big Buck Bunny 4K.webm"`. |
| 11 | In the player dialog, both items were "video, size unknown" with no name, and "on screen" was the player's own "0:00 / -:- Loaded 3.53%". | **Fixed** (`ed8387c`). Named by the dialog's title (`aria-labelledby`), and described again once its metadata arrives. |

**Through the MCP tools after the fill-in fix:** opening the file page now reports 6 load-time blocks as one line.
Two lines still read as "appeared": the "Download all sizes" gadget beside the player (positioned, so treated as
an overlay) and "MOTD captions". A Wiki Loves Monuments banner also appeared; it is a genuine late banner.

**Still open:**
- A fade (title to clouds) is not split into its own shot.
- `find`'s crop of a very large match (the whole 800x450 player) costs about 665 tokens. Seen again tonight.
