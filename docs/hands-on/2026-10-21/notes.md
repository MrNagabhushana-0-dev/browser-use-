# Hands-on, 2026-10-21: Subway Surfers, played by the model turn by turn

Driven only through the Retinat MCP tools. The servers started through the new `bin/mcp-serve.sh` (as `uv run
--no-sync`) once the session reconnected them, and `retinat_step` appeared in the client's tools.

**What was played.** Subway Surfers on Poki. After the start, every move was mine: a `retinat_step` call played 150-500
ms of game time with the key I chose, then the game stood still while I looked at the frame and decided. No rules,
no script. Clip: `subway-surfers-step-mode-turn-by-turn.mp4` (1.7 MB, frames where nothing changed dropped).

- The game stood still between steps and moved only by the time each step played: it is plain JavaScript (not Unity),
  and the paused clock and the frame gate held it.
- Kept to the middle lane between two trains, then two jumps over barriers on the game's tutorial prompts: score 61
  in 12.4 s of game time.
- The tutorial's next barrier rewound me three times; I never cleared it. Pressing Up a second time mid-jump looks
  like the cause, but this was not proven. The run ended there, still in the tutorial. **Score 61, turn-based.**

**Bugs found:**
1. **Slow on a real page.** Each frame took 0.25-0.5 s of real time against 17 ms on test pages, so play ran ~40x
   slower than live. Measured afterwards (each step now says it): of a 300 ms step's 3.5 s, 2.7 s went waiting for the
   page's clock. Virtual time moved on only after 100 of the page's tasks in a row; at 5 the clock part fell to
   2.1-2.3 s, and a local page with an endless message loop steps twice as fast. What is left looks like the game's
   own work in this GPU-less container.
2. **Full screen made it ~1 s a frame** (500 ms took 28.3 s). My first explanation, that the full-screen frame hid
   the 1 px canvas that keeps frames coming, was wrong: a test with a full-screen game frame steps at full speed. Full
   screen drew 1920x1080 against ~1028x580, 3.4x the pixels without a GPU, which fits ~0.94 s against ~0.3 s a frame.
   Escape left full screen.
3. A click with `expect` cannot read buttons drawn on a canvas ("RESUME" refused; plain click worked). That is as
   designed, but the refusal could say the text may be drawn on a canvas.
