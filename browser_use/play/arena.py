"""Open a game, get into it, play it for real, and measure what happened.

The awkward part is not the playing, it is everything in front of it: a cookie wall, an
age gate, a pre-roll advert, a splash screen that wants one click before the game exists.
A person gets through those without thinking and then starts playing. Anything that skips
that step is testing a loading screen.

The game itself is a cross-origin iframe, so nothing inside it is reachable from the DOM
— no score, no canvas, no state. That is exactly the case trusted input was built for:
CDP dispatches at the browser, not the document, so a keystroke lands in the game frame
the same way it would if a person had typed it. Everything read back out is pixels.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from browser_use.play.strategies import Action, BanditPlayer
from browser_use.play.views import ACTIVE_THRESHOLD, STALL_SECONDS, GameReport, InputEvent
from browser_use.vision.live import LiveView
from browser_use.vision.perceive import Scene, luma_grid, perceive, track

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

# How long to wait after an input before judging what it did. Human reaction time is
# around 250ms; this is the same idea pointed the other way.
REACTION = 0.18

# How often the picture is sampled, independent of the player's pace.
SAMPLE = 0.1

# Frames buffered while playing. Only a few seconds are ever needed — the motion timeline
# is accumulated as we go, so holding a whole session of JPEGs would be pure waste.
PLAY_BUFFER = 60

# Words on the button that stands between you and the game.
# Matched on word boundaries, not as substrings. 'ok' inside 'poki' is not a consent
# button, and clicking the site logo navigates away from the game you came to play.
_CONSENT = ('accept', 'agree', 'consent', 'got it', 'allow all', 'i understand', 'ok')
_START = ('play', 'start', 'tap to', 'click to', 'begin', 'resume')

# Where the button that starts or restarts a game actually sits, as fractions of the play
# surface, most likely first. Measured off real screens rather than assumed, and the
# measurements agree with each other: Drive Mad's Retry sat at 0.87 of the surface height
# and Drift Boss's title-screen Play at 0.79. Both are well below the middle, which is why
# a blind centre click — the obvious implementation — misses every time and leaves a game
# that loaded fine looking like a game that never started.
#
# A column down the middle first, because these buttons are centred horizontally far more
# often than not; then the sides, for the Home / Retry / Next rows that straddle centre.
BUTTON_SPOTS = (
	(0.50, 0.79),
	(0.50, 0.87),
	(0.50, 0.72),
	(0.50, 0.62),
	(0.50, 0.57),
	(0.50, 0.50),
	(0.50, 0.93),
	(0.62, 0.79),
	(0.38, 0.79),
	(0.62, 0.87),
	(0.38, 0.87),
	(0.50, 0.35),
)


class GameArena:
	"""Plays one browser game per call, and reports what it measured."""

	def __init__(self, browser_session: 'BrowserSession', out_dir: Path) -> None:
		self.browser_session = browser_session
		self.out_dir = Path(out_dir)
		self.out_dir.mkdir(parents=True, exist_ok=True)
		self.surface: tuple[float, float, float, float] | None = None
		# The scene descriptions this session produced, so a caller can read what happened
		# as text rather than paging through screenshots.
		self.scenes: list[Scene] = []

	@property
	def human(self):
		return self.browser_session.human

	async def _js(self, code: str, cap: int = 6000):
		result = await self.browser_session.run_page_script(code, max_chars=cap)
		if not result.ok:
			return None
		try:
			return json.loads(result.value)
		except Exception:
			return result.value

	# -- getting into the game --------------------------------------------------------

	async def dismiss_walls(self, report: GameReport) -> None:
		"""Click past consent, age gates and splash screens, with real input.

		The consent dialog is usually a modal, which is the one case the synthesized tool
		surface scopes to deliberately — so ask it first, and fall back to looking for a
		button that says one of the words people put on these.
		"""
		for _ in range(3):
			# Nothing to dismiss if the game is already on screen. Clicking hopefully at a page
			# that is working is how you end up somewhere else.
			if await self.find_surface():
				return
			tools = await self.browser_session.get_webmcp_tools()
			named = [t for t in tools.tools if any(w in t.name.replace('_', ' ').lower() for w in _CONSENT)]
			if tools.modal_note and named:
				report.note = (report.note + f' dismissed modal via {named[0].name};').strip()
				await self.browser_session.call_webmcp_tool(named[0].name, {})
				await asyncio.sleep(1.5)
				continue

			hit = await self._js(
				"""
				const want = %s;
				// Buttons only. An <a> that says "OK" is usually a link somewhere, and following
				// it loses the game; a consent wall is built out of buttons.
				const clickable = [...document.querySelectorAll('button, [role="button"], input[type="submit"]')];
				for (const el of clickable) {
					const label = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().toLowerCase();
					if (!label || label.length > 30) continue;
					// Whole words: 'ok' must not match 'poki'.
					const hit = want.some(w => new RegExp('(^|[^a-z])' + w + '([^a-z]|$)').test(label));
					if (!hit) continue;
					const r = el.getBoundingClientRect();
					if (r.width < 20 || r.height < 12) continue;
					return {label: label.slice(0, 30), x: r.x, y: r.y, w: r.width, h: r.height};
				}
				return null;
				"""
				% json.dumps(list(_CONSENT)),
			)
			if not isinstance(hit, dict):
				return
			await self.human.click_box((hit['x'], hit['y'], hit['w'], hit['h']))
			report.note = (report.note + f' clicked "{hit["label"]}";').strip()
			await asyncio.sleep(1.5)

	async def find_surface(self) -> tuple[str, tuple[float, float, float, float]] | None:
		"""The rectangle the game is actually drawn in."""
		found = await self._js("""
			const rect = (el) => { const r = el.getBoundingClientRect();
				return {x: r.x, y: r.y, w: r.width, h: r.height}; };
			const big = (r) => r.w > 200 && r.h > 150;

			for (const f of document.querySelectorAll('iframe')) {
				const r = rect(f);
				if (big(r)) return {kind: 'iframe', src: (f.src || '').slice(0, 60), ...r};
			}
			let best = null;
			for (const c of document.querySelectorAll('canvas')) {
				const r = rect(c);
				if (big(r) && (!best || r.w * r.h > best.w * best.h)) best = {kind: 'canvas', src: '', ...r};
			}
			return best;
		""")
		if not isinstance(found, dict) or 'x' not in found:
			return None
		return f'{found["kind"]} {found.get("src", "")}'.strip(), (found['x'], found['y'], found['w'], found['h'])

	async def enter_game(self, report: GameReport) -> bool:
		"""Get from a loaded page to a game that is running."""
		await self.dismiss_walls(report)
		located = await self.find_surface()
		if not located:
			report.note = (report.note + ' no play surface found;').strip()
			return False
		report.surface, self.surface = located
		# Two clicks into the middle: most portals want one to focus the frame and one for
		# the game's own splash. A second click on an already-running game is harmless.
		for _ in range(2):
			await self.human.click_box(self.surface)
			await asyncio.sleep(2.0)
		report.loaded = True
		return True

	# -- playing ----------------------------------------------------------------------

	def _spot(self, fx: float, fy: float) -> tuple[float, float, float, float]:
		"""A small target at a fractional position inside the play surface."""
		assert self.surface is not None, 'no play surface'
		x, y, w, h = self.surface
		return (x + w * fx - w * 0.04, y + h * fy - h * 0.04, w * 0.08, h * 0.08)

	def _box(self, where: str) -> tuple[float, float, float, float]:
		assert self.surface is not None, 'no play surface'
		x, y, w, h = self.surface
		if where == 'left':
			return (x + w * 0.10, y + h * 0.35, w * 0.22, h * 0.30)
		if where == 'right':
			return (x + w * 0.68, y + h * 0.35, w * 0.22, h * 0.30)
		if where == 'top':
			return (x + w * 0.35, y + h * 0.12, w * 0.30, h * 0.20)
		if where == 'bottom':
			return (x + w * 0.35, y + h * 0.68, w * 0.30, h * 0.20)
		return (x + w * 0.35, y + h * 0.35, w * 0.30, h * 0.30)

	async def _do(self, action: Action) -> None:
		if action.kind == 'idle':
			await asyncio.sleep(action.seconds)
		elif action.kind == 'hold_key' and action.key:
			await self.human.hold(action.key, action.seconds)
		elif action.kind == 'tap_key' and action.key:
			await self.human.press(action.key)
		elif action.kind == 'click':
			await self.human.click_box(self._box(action.where))
		elif action.kind == 'hold_click':
			await self.human.press_and_hold(self._box(action.where), action.seconds)

	async def _sample(self, view: LiveView, began: float, timeline: list[int], keyframes: list) -> None:
		"""Watch the picture at a steady rate, independent of what the player is doing.

		Sampling once per action, as this first did, produces a timeline at whatever rate
		the player happens to act — which then measures the player's pace rather than the
		game's motion, and starves the reward signal of exactly the frames that show what
		an action did.
		"""
		last_grid = None
		last_at = -1.0
		while True:
			await asyncio.sleep(SAMPLE)
			frames = view.frames
			if not frames:
				continue
			newest = frames[-1]
			# Skip a frame we have already scored. The screencast runs at about 8fps and this
			# loop at 10Hz, so without this most samples compare a frame against itself, score
			# zero, and bury the real motion under a flood of noughts.
			if newest.at == last_at:
				continue
			last_at = newest.at

			grid = luma_grid(newest.data)
			if grid is None:
				continue
			if last_grid is not None:
				scene = perceive(last_grid, grid)
				# Floor at 1 when something identifiable moved. Percent-of-frame is a fine
				# measure of magnitude and a bad measure of aliveness: the sprite that is the
				# whole game can be a rounding error of the frame's area.
				timeline.append(scene.motion or (1 if scene.blobs else 0))
				self.scenes.append(scene)
			last_grid = grid
			at = time.monotonic() - began
			if len(keyframes) < 8 and timeline and timeline[-1] >= 6 and (not keyframes or at - keyframes[-1][0] > 8):
				keyframes.append((at, newest.data))

	async def _recover(self, timeline: list[int], limit: int = 7) -> bool:
		"""Get out of a game-over card, and know whether it worked.

		Clicking the middle of the play surface is the obvious move and it is wrong: a
		restart card puts Home / Retry / Levels in a row low in the frame, and the middle
		is the dimmed artwork above it. Measured on one, Retry sat at 0.87 of the surface
		height — outside the centre box entirely, which is why a session could stall
		fourteen times and never come back.

		So probe instead of guess. Try the places these buttons actually live, plus the
		keys that restart a game, and stop the moment the picture starts moving again.
		That terminates immediately when the first candidate is right and still recovers
		when the layout is one this has never seen.
		"""
		for spot in BUTTON_SPOTS[:limit]:
			before = len(timeline)
			await self.human.click_box(self._spot(*spot))
			await asyncio.sleep(0.4)
			window = timeline[before:]
			if window and (sum(window) / len(window)) >= ACTIVE_THRESHOLD:
				return True

		for key in ('Space', 'Enter', 'r'):
			before = len(timeline)
			await self.human.press(key)
			await asyncio.sleep(0.35)
			window = timeline[before:]
			if window and (sum(window) / len(window)) >= ACTIVE_THRESHOLD:
				return True
		return False

	async def play(self, report: GameReport, seconds: float, player: BanditPlayer | None = None) -> GameReport:
		"""Play for a fixed stretch, learning the controls from the picture as it goes."""
		assert seconds > 0, 'play() needs a positive duration'
		player = player or BanditPlayer()
		view = LiveView(self.browser_session, buffer=PLAY_BUFFER)
		await view.start()

		began = time.monotonic()
		timeline: list[int] = []
		keyframes: list[tuple[float, bytes]] = []
		sampler = asyncio.create_task(self._sample(view, began, timeline, keyframes))
		last_moved = began

		# Many games open on a title screen with a Play button and sit there. Waiting for
		# the stall timer to notice wastes seconds at the front of every such session, and
		# the whole run is a fixed length, so find the button first and use the full budget
		# playing. The wider spot list is affordable here because it happens once.
		await asyncio.sleep(1.2)
		if not timeline or (sum(timeline) / len(timeline)) < ACTIVE_THRESHOLD:
			if await self._recover(timeline, limit=len(BUTTON_SPOTS)):
				report.restarts += 1
			last_moved = time.monotonic()

		try:
			while time.monotonic() - began < seconds:
				action = player.choose()
				before = len(timeline)
				at = time.monotonic() - began

				await self._do(action)
				# Let the consequence arrive before judging the cause.
				await asyncio.sleep(REACTION)

				window = timeline[before:] or [0]
				moved = sum(window) / len(window)
				player.reward(action, min(1.0, moved / 20.0))
				report.inputs.append(InputEvent(at=at, kind=action.kind, detail=action.name))

				if moved >= ACTIVE_THRESHOLD:
					last_moved = time.monotonic()
				elif time.monotonic() - last_moved > STALL_SECONDS:
					report.stalls += 1
					if await self._recover(timeline):
						report.restarts += 1
					last_moved = time.monotonic()
		finally:
			sampler.cancel()
			try:
				await sampler
			except asyncio.CancelledError:
				pass
			await view.stop()

		report.seconds_played = round(time.monotonic() - began, 1)
		report.frames = len(timeline)
		report.motion = timeline
		report.strategy = player.learned()
		track(self.scenes)
		moving = [s for s in self.scenes if not s.static and not s.cut]
		report.scene_sample = [s.describe() for s in moving[:: max(1, len(moving) // 6)]][:6]
		report.cuts = sum(1 for s in self.scenes if s.cut)

		for index, (at, data) in enumerate(keyframes):
			path = self.out_dir / f'{report.name.replace(" ", "_")}_{index:02d}_{at:05.1f}s.jpg'
			path.write_bytes(data)
			report.keyframes.append(str(path))
		return report
