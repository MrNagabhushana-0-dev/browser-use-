"""Step mode: the page's clock stands still while the agent thinks, and moves only as far as each step asks.

A model's turn takes seconds, a real-time game moves on every 16 ms, so a model cannot play one live; its rules can
(reflexes), but then the rules play, not the model. Benchmarks of models on such games pause the game while the model
thinks (VideoGameBench Lite, Orak, NitroGen's clock-intercepting wrapper) or let it act once every few frames (the
Arcade Learning Environment's frame skip). Here the same is done to a web page through CDP virtual time:

- `pause()` stops the page's clock: `performance.now()`, `Date.now()`, timers and posted messages stand still, in the
  page and in the frames its renderer process draws (a frame from another origin of the same site included, as
  poki.com hosts its games on *.gdn.poki.com). A frame from another site in a process of its own is not covered.
- `step(ms, hold=..., tap=...)` presses keys, then moves the clock one frame (1/60 s) at a time, waiting for each frame
  to be drawn, so the game sees as many even ticks as it would have in that time; then the clock stops again. Held
  keys stay down from step to step until a step leaves them out (the ALE's action repeat); a tap is down for its
  first frames. `charge_thinking=True` first plays the time the agent took to decide, with the keys of the last step
  still held: how the same play would fare live (OmniGameArena's latency-charged mode).
- `resume()` hands the page back to real time. Virtual time cannot be turned off again for a tab (measured: a pause
  outlives the CDP session that set it; Chromium has no way back outside its tests), so from then on a pump moves the
  clock with the real one, frame by frame.

Measured on Chromium (headless=new), and so read in its source: while virtual time stands still,
`requestAnimationFrame` keeps firing at 60 Hz with timestamps on real time, so a game timed by those timestamps, or
one that counts frames, would run on. The page's `requestAnimationFrame` is made to wait for the page clock to move and
to hand out its time. Sound (Web Audio), CSS animations, video and workers keep real time.

A score made in step mode is not real-time play: the game waited for every decision.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from browser_use.eyes.reflex import _key_params

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

FRAME_MS = 1000 / 60
# The most one step may play; a step costs about its own length in real time (each frame is waited for)
STEP_MAX_MS = 5000.0
# The most thinking time `charge_thinking` plays before a step's keys
CHARGE_MAX_MS = 10_000.0
# How long a tap is down for: a game that reads keys on a fixed tick of its own (~50 Hz physics) can miss a shorter one
TAP_MS = 50.0
# The fewest frames a step may draw per second of game time (frame skip, for pages slow to draw)
FPS_MIN = 10.0
# How long a frame's budget may take to be spent before the page is asked whether it still answers
_BUDGET_WAIT_S = 5.0
# Virtual time moves only when the page has no task ready to run: after this many tasks run in a row it is moved on
# anyway, so a page that keeps posting work cannot hold the clock back. Real time does not wait for a page either. At
# 100 a page full of ads and trackers (Poki) held each 17 ms frame ~150 ms; a local message loop, 23 ms (5: 3 ms).
_STARVATION_TASKS = 5
# How long letting go of keys or stopping the beat may take: a page that busy-waits on its stopped clock never answers
_POLITE_S = 1.0
# How long a frame is waited for before our world is made again: a page left for another one sits frozen in the
# back-forward cache, and a frame asked of its world never comes
_FRAME_WAIT_S = 1.0
# How long a page that drew no frame has to answer before a script is taken to hold it: a heavy page on a loaded
# machine took seconds to draw a frame (QWOP), and was stopped as if stuck when it had 1 s; a script waiting on the
# stopped clock never answers, so waiting longer costs time only on a page that is stuck
_ANSWER_S = 12.0
# The most one move of the pump may catch up, how often it checks the beat is still there (a new page has none), and how
# long it keeps failing before it takes the tab to be gone: a pump that stops while its tab lives freezes the tab for
# good, so a page being replaced must never be enough
_PUMP_CATCH_UP_MS = 250.0
_PUMP_BEAT_CHECK = 30
_PUMP_GIVE_UP_S = 30.0
_EXPIRED_EVENT = 'Emulation.virtualTimeBudgetExpired'
_WORLD = 'browser_use_clock'
# requestAnimationFrame on the page's clock, in every frame of the tab, now and on each new document: a callback runs
# only once the clock has moved since the last batch, and is handed the clock's time. CDP does not hold frames back while
# virtual time stands still, and their own timestamps keep real time. As in browsers, a batch is the callbacks asked for
# before it began, and one cancelled by an earlier callback of the same batch is skipped. Ids are kept clear of the
# browser's own, so a frame asked for before this was installed can still be cancelled.
_RAF_ON_PAGE_CLOCK = r"""(() => {
	if (window.__browserUseRaf) return;
	const raf = window.requestAnimationFrame.bind(window), caf = window.cancelAnimationFrame.bind(window);
	window.__browserUseRaf = raf;
	const queue = new Map();
	let next = 1e9, ticking = false, last = NaN;
	const tick = () => {
		ticking = false;
		const now = performance.now();
		if (now !== last && queue.size) {
			last = now;
			for (const id of [...queue.keys()]) {
				const callback = queue.get(id);
				if (!callback) continue;
				queue.delete(id);
				try { callback(now); } catch (e) { reportError(e); }
			}
		}
		if (queue.size && !ticking) { ticking = true; raf(tick); }
	};
	window.requestAnimationFrame = (callback) => {
		const id = ++next;
		queue.set(id, callback);
		if (!ticking) { ticking = true; raf(tick); }
		return id;
	};
	window.cancelAnimationFrame = (id) => { if (!queue.delete(id) && id < 1e9) caf(id); };
})()"""


# Frames keep coming while the clock stands still only if something keeps drawing: once a paused page draws nothing (the
# requestAnimationFrame above holds the game back), Chrome stops making frames, and neither a style change nor a
# screenshot starts them again (measured), so no step could be played. A 1 px canvas, 1% opaque, drawn every frame from
# a world of our own, keeps them coming for as long as the tab is on our clock: in the top frame, on each new page from
# its first moment (a capture made before then waited for good, and the tab drew nothing after it).
# It is no canvas the page can find (in a closed shadow root, moved after <body> once there is one: a page's
# querySelector('canvas') found it when it sat between <head> and <body>), and it puts itself back when the page renders
# itself anew over everything in <html> (which takes it out while this world lives on). `window.__browserUseBeat()` stops it;
# `window.__browserUseBeat.attach()` puts it back if it is out, and says whether it is in.
_BEAT = r"""(() => {
	if (window !== window.top) return;
	if (window.__browserUseBeat) { window.__browserUseBeat.attach(); return; }
	const host = document.createElement('browser-use-beat');
	host.style.cssText = 'display:block;position:fixed;left:0;top:0;width:1px;height:1px;opacity:0.01;pointer-events:none;z-index:2147483647';
	const canvas = document.createElement('canvas');
	canvas.width = canvas.height = 1;
	canvas.style.cssText = 'display:block;width:1px;height:1px';
	host.attachShadow({mode: 'closed'}).append(canvas);
	const g = canvas.getContext('2d');
	let on = true, n = 0, html = null;
	const beat = () => {
		if (!on) return;
		g.fillStyle = n++ % 2 ? '#000' : '#fff';
		g.fillRect(0, 0, 1, 1);
		requestAnimationFrame(beat);
	};
	// in <html> as soon as there is one, moved after <body> when it comes, back in whenever it is taken out
	const attach = () => {
		const root = document.documentElement, body = document.body;
		if (!on || !root) return false;
		if (!host.isConnected || (body && body.compareDocumentPosition(host) & Node.DOCUMENT_POSITION_PRECEDING)) root.append(host);
		return host.isConnected;
	};
	const watching = new MutationObserver(() => { attach(); watchRoot(); });
	const watchRoot = () => {
		if (document.documentElement && document.documentElement !== html) {
			html = document.documentElement;
			watching.observe(html, {childList: true});
		}
	};
	watching.observe(document, {childList: true});
	watchRoot();
	attach();
	requestAnimationFrame(beat);
	window.__browserUseBeat = () => { on = false; watching.disconnect(); host.remove(); delete window.__browserUseBeat; };
	window.__browserUseBeat.attach = attach;
})()"""


class PageStuck(RuntimeError):
	"""The page stopped answering during a step: a script on it never yields."""


class Stepped(BaseModel):
	"""What one step played."""

	model_config = ConfigDict(extra='forbid')

	game_ms: float
	frames: int
	real_s: float
	charged_ms: float = 0.0  # the agent's thinking time played before the step, with charge_thinking
	held: list[str] = Field(default_factory=list)  # keys still down after the step
	draw_s: float = 0.0  # of real_s, waiting for the page to draw each frame
	clock_s: float = 0.0  # of real_s, waiting for the page's clock to take each frame's time


class PageClock:
	"""The clock of the focused tab: pause(), step(), resume()."""

	def __init__(self, browser_session: 'BrowserSession') -> None:
		self.session = browser_session
		self.paused = False
		self.played_ms = 0.0  # game time played by steps since the first pause, thinking charged included
		self.moved_ms = 0.0  # all the game time the clock has moved, by steps and in real time
		self.paused_s = 0.0  # real time the page spent under step mode
		self.target_id: str | None = None
		self.held: list[str] = []
		self._since: float | None = None  # when the current stretch of step mode began (monotonic)
		self._last_step_end: float | None = None
		self._cdp: Any = None
		self._session_id: str | None = None
		self._expired = asyncio.Event()
		self._handler: Any = None
		self._incumbent: Any = None
		self._pump: asyncio.Task | None = None
		self._world: tuple[str, int] | None = None  # (frame id, context id) of our own world in the top frame
		self._waits = {'draw': 0.0, 'clock': 0.0}  # where the current step's real time went

	@property
	def virtual(self) -> bool:
		"""Whether this tab's clock is ours (paused or pumped); it stays so until the tab closes."""
		return self._session_id is not None

	@property
	def slowdown(self) -> float:
		"""Real time per unit of game time under step mode: how much longer the play took than it would have live."""
		spent = self.paused_s + (time.monotonic() - self._since if self._since is not None else 0.0)
		return spent / (self.played_ms / 1000) if self.played_ms else 0.0

	async def pause(self) -> None:
		"""Stop the tab's clock. The first pause takes the tab's clock over for good (see the module docstring)."""
		await self._stop_pump()
		if self._session_id is None:
			cdp = await self.session.get_or_create_cdp_session(focus=False)
			self._cdp, self._session_id, self.target_id = cdp, cdp.session_id, cdp.target_id
			try:
				if self._handler is None:
					registry = getattr(self.session.cdp_client, '_event_registry', None)
					handlers = getattr(registry, '_handlers', None)
					incumbent = handlers.get(_EXPIRED_EVENT) if isinstance(handlers, dict) else None
					self._incumbent = incumbent if getattr(incumbent, '__self__', None) is not self else None
					self._handler = self._on_expired
					self.session.cdp_client.register.Emulation.virtualTimeBudgetExpired(self._handler)
				await self._send(
					'Page', 'addScriptToEvaluateOnNewDocument', {'source': _RAF_ON_PAGE_CLOCK, 'runImmediately': True}
				)
				await self._send('Page', 'addScriptToEvaluateOnNewDocument', {'source': _BEAT, 'worldName': _WORLD})
			except BaseException:
				self._session_id = None  # set up again by the next pause
				raise
		await self._check_beat()  # the beat starts while frames still come; the page may be new since the last pause
		await self._send('Emulation', 'setVirtualTimePolicy', {'policy': 'pause'})
		self.paused = True
		self._since = time.monotonic()
		self._last_step_end = None

	async def step(
		self,
		ms: float,
		hold: list[str] | tuple[str, ...] = (),
		tap: list[str] | tuple[str, ...] = (),
		charge_thinking: bool = False,
		stop_at: float | None = None,
		fps: float = 60.0,
		seen: Callable[[int, float], Awaitable[None]] | None = None,
	) -> Stepped:
		"""Play `ms` of game time a frame at a time with `hold` down throughout (and after, until a step leaves a key
		out) and `tap` down for its first ~50 ms, then stand still again. `charge_thinking` first plays the time since
		the last step ended with the last step's keys. `stop_at` (monotonic) ends the step after the frame playing then.
		`fps` below 60 draws fewer frames per second of game time, each a longer tick (frame skip). `seen(i, game_ms)` is
		awaited once each of the step's frames is drawn, with the frame's index and its game time into the step."""
		assert self.paused, 'pause() first'
		assert 0 < ms <= STEP_MAX_MS, f'a step plays 1-{STEP_MAX_MS:.0f} ms'
		assert FPS_MIN <= fps <= 60, f'fps is {FPS_MIN:.0f}-60'
		frame_ms = 1000 / fps
		began = time.monotonic()
		self._waits = {'draw': 0.0, 'clock': 0.0}
		charged = 0.0
		if charge_thinking and self._last_step_end is not None:
			thought_ms = min(CHARGE_MAX_MS, (began - self._last_step_end) * 1000)
			if thought_ms >= frame_ms:
				charged = await self._play(max(1, round(thought_ms / frame_ms)), stop_at=stop_at, frame_ms=frame_ms)
		hold = list(dict.fromkeys(hold))
		released = [k for k in self.held if k not in hold]
		for key in released:
			await self._key(key, 'keyUp')
		for key in [k for k in hold if k not in self.held]:
			await self._key(key, 'keyDown')
		self.held = hold
		taps = [k for k in dict.fromkeys(tap) if k not in hold]
		frames = max(1, round(ms / frame_ms))
		tap_frames = max(1, round(TAP_MS / frame_ms))
		played = 0.0
		if any(k in released for k in taps) and frames > 1:
			# a key let go and pressed again before the same frame is no new press to a game that polls keys each frame
			played = await self._play(1, stop_at=stop_at, frame_ms=frame_ms, seen=seen)
			frames -= 1
		for key in taps:
			await self._key(key, 'keyDown')
		try:
			played += await self._play(
				frames,
				stop_at=stop_at,
				release_after=(tap_frames, taps),
				first=round(played / frame_ms),
				frame_ms=frame_ms,
				seen=seen,
			)
		finally:
			for key in taps:  # a step shorter than a tap still lets go of it
				await self._key(key, 'keyUp')
		self.played_ms += charged + played
		self._last_step_end = time.monotonic()
		return Stepped(
			game_ms=played,
			frames=round(played / frame_ms),
			real_s=self._last_step_end - began,
			charged_ms=charged,
			held=list(self.held),
			draw_s=self._waits['draw'],
			clock_s=self._waits['clock'],
		)

	async def resume(self) -> None:
		"""Back to real time: held keys go up, and from now on the clock moves with the real one. Also starts the pump
		again if it ever stopped while the tab lived on."""
		await self._politely(self._release_all(), 'let go of held keys')
		if self._since is not None:
			self.paused_s += time.monotonic() - self._since
			self._since = None
		self.paused = False
		if self.virtual and (self._pump is None or self._pump.done()):
			self._pump = asyncio.create_task(self._run_pump(), name='page-clock-pump')

	async def close(self) -> None:
		"""Let go of held keys, stop the pump and the event handler (the tab, if it lives on, keeps the clock where it
		stands)."""
		await self._politely(self._release_all(), 'let go of held keys')
		await self._stop_pump()
		await self._politely(self._stop_beat(), 'stop the beat')
		registry = getattr(self.session.cdp_client, '_event_registry', None) if self._handler is not None else None
		handlers = getattr(registry, '_handlers', None)
		if isinstance(handlers, dict) and handlers.get(_EXPIRED_EVENT) is self._handler:
			if self._incumbent is not None:
				self.session.cdp_client.register.Emulation.virtualTimeBudgetExpired(self._incumbent)
			else:
				handlers.pop(_EXPIRED_EVENT, None)
		self._handler = None

	# -- internals ---------------------------------------------------------------------------------------------------

	async def _politely(self, coro: Any, what: str) -> None:
		"""Cleanup a hung page cannot hold up."""
		try:
			await asyncio.wait_for(coro, _POLITE_S)
		except Exception as e:
			logger.debug(f'clock: could not {what}: {type(e).__name__}: {e}')

	async def _play(
		self,
		frames: int,
		stop_at: float | None = None,
		release_after: tuple[int, list[str]] | None = None,
		first: int = 0,
		frame_ms: float = FRAME_MS,
		seen: Callable[[int, float], Awaitable[None]] | None = None,
	) -> float:
		"""Move the clock `frames` frames of `frame_ms` each, waiting for each to be drawn; the game time played. `first`
		is how many frames of the same step came before, so the milliseconds keep adding up. `seen` as in step()."""
		after, keys = release_after or (0, [])
		played = 0
		for i in range(first, first + frames):
			# whole milliseconds that add up over frames: 17, 16, 17, ... for 1/60 s
			t0 = time.monotonic()
			await self._advance(round((i + 1) * frame_ms) - round(i * frame_ms))
			t1 = time.monotonic()
			if not await self._next_frame():
				await self._answers_or_stuck()
			self._waits['clock'] += t1 - t0
			self._waits['draw'] += time.monotonic() - t1
			played = i + 1 - first
			if seen is not None:
				await seen(i, float(round((i + 1) * frame_ms)))
			if played == after:
				for key in keys:
					await self._key(key, 'keyUp')
				keys = []
			if stop_at is not None and time.monotonic() >= stop_at:
				break
		if release_after is not None:
			release_after[1][:] = keys  # what is still down is let go by the caller
		return float(round((first + played) * frame_ms) - round(first * frame_ms))

	def _on_expired(self, event: dict[str, Any], session_id: str | None = None) -> None:
		if session_id == self._session_id:
			self._expired.set()
		elif self._incumbent is not None:
			self._incumbent(event, session_id)

	async def _send(self, domain: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
		assert self._cdp is not None
		return await getattr(getattr(self._cdp.cdp_client.send, domain), method)(params=params, session_id=self._session_id)

	async def _advance(self, ms: float) -> None:
		self.moved_ms += ms
		self._expired.clear()
		await self._send(
			'Emulation',
			'setVirtualTimePolicy',
			{'policy': 'advance', 'budget': ms, 'maxVirtualTimeTaskStarvationCount': _STARVATION_TASKS},
		)
		try:
			await asyncio.wait_for(self._expired.wait(), _BUDGET_WAIT_S)
		except TimeoutError:
			# the page is still in a task: virtual time moves only between them. A page that answers is slow (a heavy
			# frame on a loaded machine) and spends the budget once its task ends; one that does not is stuck
			await self._answers_or_stuck()
			await asyncio.wait_for(self._expired.wait(), _BUDGET_WAIT_S)

	async def _world_id(self) -> int:
		"""Our own world in the top frame, made (with its beat) on first use and again after the page is replaced."""
		if self._world is None:
			tree = await self._send('Page', 'getFrameTree', {})
			frame_id = tree['frameTree']['frame']['id']
			world = await self._send('Page', 'createIsolatedWorld', {'frameId': frame_id, 'worldName': _WORLD})
			await self._send('Runtime', 'evaluate', {'expression': _BEAT, 'contextId': world['executionContextId']})
			self._world = (frame_id, world['executionContextId'])
		return self._world[1]

	async def _next_frame(self) -> bool:
		"""Wait for the page to draw its next frame, asked in our own world: the page's requestAnimationFrame (ours or its
		own) is not the one waited on. False if none came, after making the world again once."""
		trouble = ''
		for _ in (1, 2):
			try:
				result = await asyncio.wait_for(self._frame_in_world(), _FRAME_WAIT_S)
				if not result.get('exceptionDetails'):
					return True
				trouble = str(result['exceptionDetails'])
			except TimeoutError:
				trouble = f'no frame within {_FRAME_WAIT_S:.0f}s'
			except Exception as e:  # the context is gone with the page it was in
				trouble = f'{type(e).__name__}: {e}'
			self._world = None  # the page was replaced or left: make the world again in the one shown now
		logger.debug(f'clock: no frame to wait for: {trouble}')
		return False

	async def _answers_or_stuck(self) -> None:
		"""No frame came: if the page does not answer either, a script holds it. Such a script mostly waits for its clock
		to move (a busy loop on performance.now()), and this clock moves only between scripts, so it never ends (found on
		Level Devil). The script is stopped, and PageStuck says the page cannot be played this way. A page that answers,
		however late, is only slow: the step goes on."""
		try:
			await asyncio.wait_for(self._send('Runtime', 'evaluate', {'expression': '1', 'returnByValue': True}), _ANSWER_S)
			return
		except Exception as e:
			logger.debug(f'clock: the page does not answer: {type(e).__name__}: {e}')
		await self._politely(self._send('Runtime', 'terminateExecution', {}), 'stop the script holding the page')
		raise PageStuck(
			'the page stopped answering during the step: a script on it ran without yielding. Such a script mostly waits '
			'for its clock to move (a busy loop on performance.now()), and in step mode the clock moves only between '
			'scripts, so it never ends. Retinat stopped that script, but the page will do it again: it cannot be played '
			'turn by turn. Open it again with new_tab: true and play it live, without retinat_step.'
		)

	async def _frame_in_world(self) -> dict[str, Any]:
		return await self._send(
			'Runtime',
			'evaluate',
			{
				'expression': 'new Promise((done) => requestAnimationFrame(() => done(1)))',
				'contextId': await self._world_id(),
				'awaitPromise': True,
			},
		)

	async def _check_beat(self) -> None:
		"""Make our world (and its beat) again if the page shown now has none: it was replaced since. A beat the page took
		out (by writing itself anew) is put back."""
		try:
			result = await asyncio.wait_for(
				self._send(
					'Runtime',
					'evaluate',
					{
						'expression': '!!window.__browserUseBeat && window.__browserUseBeat.attach()',
						'contextId': await self._world_id(),
						'returnByValue': True,
					},
				),
				_FRAME_WAIT_S,
			)
			if result.get('result', {}).get('value') is True:
				return
		except Exception as e:
			logger.debug(f'clock: the beat is gone: {type(e).__name__}: {e}')
		self._world = None
		await self._world_id()

	async def _stop_beat(self) -> None:
		world, self._world = self._world, None
		if world is not None:
			try:
				await self._send(
					'Runtime',
					'evaluate',
					{'expression': 'window.__browserUseBeat && window.__browserUseBeat()', 'contextId': world[1]},
				)
			except Exception as e:  # gone with its page
				logger.debug(f'clock: no beat to stop: {type(e).__name__}: {e}')

	async def _key(self, key: str, kind: str) -> None:
		await self._send('Input', 'dispatchKeyEvent', _key_params(key, kind))

	async def _release_all(self) -> None:
		held, self.held = self.held, []
		if self._session_id is not None:
			for key in held:
				await self._key(key, 'keyUp')

	async def _run_pump(self) -> None:
		"""Real time: every frame or so the clock moves by the real time since its last move."""
		last = time.monotonic()
		failing_since: float | None = None
		moves = 0
		try:
			while failing_since is None or time.monotonic() - failing_since < _PUMP_GIVE_UP_S:
				await asyncio.sleep(FRAME_MS / 1000)
				now = time.monotonic()
				ms = min(_PUMP_CATCH_UP_MS, (now - last) * 1000)
				last = now
				try:
					if ms >= 1:
						await self._advance(round(ms))
					moves += 1
					if moves % _PUMP_BEAT_CHECK == 0:
						await self._check_beat()
					failing_since = None
				except Exception as e:  # a page being replaced, input refused while the person drives, or the tab gone
					failing_since = failing_since or time.monotonic()
					logger.debug(f'clock: the pump missed a move: {type(e).__name__}: {e}')
					await asyncio.sleep(min(1.0, 0.05 * (1 + time.monotonic() - failing_since)))
			logger.debug('clock: the pump stopped: the tab is gone')
		except asyncio.CancelledError:
			raise

	async def _stop_pump(self) -> None:
		if self._pump is not None:
			self._pump.cancel()
			await asyncio.gather(self._pump, return_exceptions=True)
			self._pump = None
