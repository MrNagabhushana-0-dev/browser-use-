# P2 morning checklist: Retinat in your own Chrome (about 10 minutes)

You need: Google Chrome, Claude Code (logged in), and a terminal. Works on: macOS (Apple Silicon, macOS 14 or
later) or Linux or Windows on x86_64 for the watching tools; the bridge itself works on any Chromium-based browser.
Nothing below touches anything outside `~/retinat`, `~/retinat-extension`, `~/.config/browseruse` (Retinat's
recordings and logs) and uv; the last section undoes all of it.

## Steps

1. **Run the one setup command** for your OS and leave the terminal open. It installs uv if missing, clones the repo
   into `~/retinat` (or updates it), installs packages (a few minutes the first time), writes the extension to
   `~/retinat-extension`, writes `~/retinat/.retinat.env` (`export RETINAT_BRIDGE=9333`), and waits for Chrome.
   - macOS / Linux:
     ```bash
     /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/MrNagabhushana-0-dev/browser-use-/refs/heads/claude/laughing-hawking-2cj3a6/scripts/p2/setup.sh)"
     ```
   - Windows (PowerShell):
     ```powershell
     powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/MrNagabhushana-0-dev/browser-use-/refs/heads/claude/laughing-hawking-2cj3a6/scripts/p2/setup.ps1 | iex"
     ```
   - Already have `~/retinat`? `cd ~/retinat && git pull && uv sync --extra eyes && uv run python -m browser_use.bridge setup`
2. **Load the extension.** In Chrome open `chrome://extensions`, switch on **Developer mode** (top right), press
   **Load unpacked**, pick the folder the terminal printed (`~/retinat-extension`; in the Mac picker, Cmd+Shift+G
   and paste the path). Pin it: puzzle-piece icon -> pin next to "Retinat bridge".
3. **Watch the self-test (no action needed).** A tab "Retinat bridge self-test" opens by itself. For a second Chrome
   shows the bar *"Retinat bridge" started debugging this browser*; it goes away a few seconds after. PASS looks like:
   - in the tab: the grey button is now green ("Clicked: a trusted click reached this page") and a green line
     *PASS: the AI saw this page, clicked the button, and saw it turn green*;
   - in the terminal: `PASS  self-test through relay -> extension -> tab` with look 1 / click / look 2 times in ms and
     `button mean rgb (158, 158, 158) -> (47, 125, 50)`, and the command ends by itself (exit code 0).

   FAIL prints the reason in both places. Run the command again; it opens the self-test tab itself. Do not share the
   relay's own address by hand: the bridge refuses that. If it says port 9333 is taken, a Claude session is already
   running Retinat: stop it (Ctrl+C) and run the command again. Close the self-test tab when done.
4. **Open the YouTube video you were given.** Turn captions **off** (press `c`, or the CC button until it is not
   underlined). Leave it paused at the start.
5. **Share that tab:** press the Retinat bridge button in the toolbar -> **Share this tab with the AI**. A pill at
   the bottom of the tab says "An AI is working in this tab". Only shared tabs are visible to the AI.
6. **Start Claude** in a new terminal and leave it running:
   ```bash
   cd ~/retinat && claude --remote-control --permission-mode auto
   ```
   `--remote-control` lets you follow (and type into) the same session from the Claude app on your phone;
   `--permission-mode auto` lets the run go on without asking you to approve each command while you sleep. Leave
   that flag out if you would rather approve each step yourself.
   It starts Retinat in bridge mode (it reads `.retinat.env`); the extension connects within about 10 s. The first
   time, Claude may ask whether you trust this folder and whether to use the MCP servers in `.mcp.json`: say yes
   (at least `retinat`).
   The AI works only in the shared YouTube tab.
7. **Start the run.** In that Claude session (the terminal, or the app), paste this one line and press Enter:
   ```text
   Run the RUNBOOK in docs/contract/PROTOCOL_v3_P2.md, then report.
   ```
   It checks it can see your shared YouTube tab, plays the film from 0:00 twice (about 4 minutes each, once the
   Milestone-1 way and once with the live narration), records both, measures them, has fresh sub-agents label and
   score them, and pushes the results to the branch. About 30-40 minutes; you can sleep. It acts only in the shared
   tab; Cancel on the debugging bar stops it at once. This is the first run on a real Chrome: if anything on screen
   looks wrong, press Cancel and tell the session what you saw.

## What each extension permission is for

| Permission | Why | Chrome's wording (Details page) |
|---|---|---|
| `debugger` | Looks (screenshots) and clicks/keys in the tabs you share, through `chrome.debugger`. Attaches only to a shared tab. | "Access the page debugger backend" |
| `tabs` | Knows each tab's address, so a shared tab that moves to a site you have not allowed stops being shared; opens the self-test tab. | "Read your browsing history" |
| `storage` | Remembers which tabs you shared, sites you allowed, and that this version already ran its self-test. Stays in your browser. | none |
| `alarms` | Retries the connection to Retinat once a minute, so you don't have to reload anything. | none |
| content script (`watch.js`, all pages) | Draws the pill in shared tabs, notices your own clicks (the AI pauses while you use the tab), and holds clicks on "pay / place order / delete account"-type buttons until you say yes. It sends nothing anywhere except to the extension itself. | "Read and change all your data on all websites" |

The warning you will see: while the AI uses a shared tab, Chrome shows *"Retinat bridge" started debugging this
browser* with a **Cancel** button. That bar is Chrome's own and cannot be hidden. Loading unpacked shows no install
prompt; Chrome lists the permissions above under the extension's **Details**.

## How the channel is locked down

- **Loopback only.** The relay (inside Retinat, port 9333) listens on 127.0.0.1 and refuses to start on anything else.
  Nothing leaves your laptop through it.
- **Extension side.** `ws://127.0.0.1:9333/extension` accepts only the Origin `chrome-extension://lcdhfliibkimhbimdfhogcmjedlkoemg`,
  the ID fixed by the `key` in the extension's manifest. Web pages cannot fake an Origin.
- **AI side.** Programs talking CDP connect to `ws://127.0.0.1:9333/cdp/<token>`. The token is random and new each
  run, and is given out only by `/json/version`, which answers only with a loopback Host header and no Origin header.
  Browsers always attach an Origin to cross-site requests and to every WebSocket, so a web page can neither read the
  token nor open the socket. The Host check also defeats DNS rebinding.
- **The relay's own address is never shared**, not even by you: the bridge refuses it, so the AI cannot reach the
  relay through a tab.
- **Self-test page.** `/selftest` is a fixed page that cannot be framed. `/selftest/state` returns only the self-test
  result, only on a loopback Host, and refuses other sites' requests (Origin and Sec-Fetch-Site).
- **What it cannot stop.** Any program already running on your laptop can reach loopback and drive the tabs you
  share. That is the same trust as Chrome's own remote debugging port. Share only what you want seen, and unshare when done.
- **What the AI may do in a shared tab:** what you could do from that tab (look, scroll, click, type, navigate to
  sites you allowed). Refused: changing what the browser says it is, rewriting traffic, writing cookies, reading
  other sites' cookies or storage, handing files from your disk. A new site opens a small window asking you first.
  "Pay / place order / delete account"-type clicks are held until you allow that one click.

## Undo, any time

- **Stop now:** press **Cancel** on the "started debugging this browser" bar. Everything is unshared and the AI is
  stopped until you share a tab again.
- **Pause:** Alt+Shift+Z (Option+Shift+Z on a Mac) takes the wheel back; the AI can only look. Press it again, or
  "Hand back" on the pill, to let it continue. Using the shared tab yourself also pauses it for a few seconds.
- **Stop sharing one tab:** extension button -> **Stop sharing this tab** (or Alt+Shift+A).
- **Stop Claude:** Ctrl+C in the terminal running Claude.
- **Remove the extension:** `chrome://extensions` -> Retinat bridge -> **Remove**.
- **Delete everything:** macOS/Linux `rm -rf ~/retinat ~/retinat-extension ~/.config/browseruse`; Windows
  `Remove-Item -Recurse -Force ~\retinat, ~\retinat-extension` (Retinat's recordings and logs on Windows sit where
  its config folder is; not checked on Windows). Claude Code keeps the run's transcripts under `~/.claude/projects`;
  delete the folders named after this run if you want them gone. uv stays installed (`~/.local/bin/uv`); delete it
  and `~/.cache/uv` too if you want it gone.
