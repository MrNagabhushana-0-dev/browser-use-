# browser-use: products in this repo

Local-only. Everything here runs on the owner's machines; nothing is a hosted service. Status below is what
the record shows (docs/contract/LOOP_MEMORY.md, M2_CLOSEOUT.md). "Unmeasured" means no number exists yet.

## Eyes (the vision product)
- **Eye MCP server** (`browser_use/retinat`, MCP name `eye-mcp-unlimited`, tools `eye_*`): the server the AI calls to
  open pages, look, watch video with sound, narrate, record and act. Measured: M1, M2 runs (B2, E3, E4, E5). Tokens
  per minute and scores in M2_CLOSEOUT.md. Startup is slow on a cold disk (see LOOP_MEMORY, connect timeout).
- **Eye core** (`browser_use/eyes`): the retina (frame and sound sampling in the page), motion, clock, hearing and
  speech, keyframe sheets, frame archive, recall and search, the event gate, the reservoir (retroactive commit of
  what played). Status: the reservoir (E4) and the narration are measured; the gate (E5) was reverted on C2.
- **Watch tools for video**: `eye_watch` (seconds up to the call budget, until time/event/bored/item/gate). Measured.

## Browser extension bridge (P2: your own Chrome)
- **Bridge** (`browser_use/bridge`, extension in `browser_use/bridge/extension`): drives tabs you share, through a
  loopback relay. Install self-test PASSES in the container (M2). Not run on the owner's own Chrome yet.
- **Consent holds**: payments, deletes and access grants wait for your yes. Only shared tabs are visible.
- **Cobrowse** (`browser_use/cobrowse`): you sign in, the AI continues in the same browser. Unmeasured.

## Network
- **Tor / proxy routing** (`browser_use/net`): for geo-restricted content only. No bot-detection or CAPTCHA evasion
  (a resolved boundary). Unmeasured as a product.

## Desktop
- **Desktop eyes and control** (`browser_use/desktop`, `browser_use/eyes/desktop.py`): the whole screen and
  mouse/keyboard, off unless `BROWSER_USE_DESKTOP_EYES=1`. Unmeasured: no display was available in the container.

## Browser actions and tools
- **Human input** (`browser_use/human`): pointer paths, wheel, keys as a person sends them.
- **Actor** (`browser_use/actor`): a Playwright-like CDP library, used internally. Not a product: a building block.
- **Browser session** (`browser_use/browser`): launches and attaches Chromium, profiles, downloads, watchdogs.
- **WebMCP** (`browser_use/webmcp`): calls the tools a page declares. **Site tool synthesis**
  (`browser_use/synthesis`): builds a tool surface for a site that declares none. Unmeasured.
- **Site explorer** (`browser_use/explore`): walks a whole site and writes down what is broken. Unmeasured.
- **Decide** (`browser_use/decide`): typed decisions for small page classifications. Unmeasured.
- **Vision watcher** (`browser_use/vision`): watches a page over time and spends tokens only on changed frames.
- **Play** (`browser_use/play`): plays browser games, a test of the input and perception layers.

## Agent and memory
- **Agent** (`browser_use/agent`), **LLM clients** (`browser_use/llm`), **memory** (`browser_use/memory`: reuses
  routes that worked on a site), **skills** (`browser_use/skills`), **sandbox** (`browser_use/sandbox`), **beta**
  (`browser_use/beta`), **integrations** (`browser_use/integrations`). Inherited from upstream browser-use; their
  quality here is unmeasured.

## Contract and evidence (not a product)
- `docs/contract/`: the operating contract, the milestone record, the evidence, the install checklist.
- `docs/hands-on/`: our recordings and notes.

## Not built
- A standalone full-desktop AI with the browser, Tor and eyes in one agent loop, measured end to end. The parts exist;
  the combined run has not been measured.
